"""Compare several runs of one project: their facts, their configs (only what differs) and their charts.

Every run has a color (its slot in the legend at the top) that follows it everywhere: the column
headers, the curves of a plot, the bars of a bar chart."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

from rich.cells import cell_len
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.worker import get_current_worker
from textual.widgets import DataTable, Header, Input, OptionList, Static, TabbedContent, TabPane
from textual.widgets.option_list import Option

from ..api import RunInfo, RunMetadata, RunRef
from ..charts import (
    BAR, IMAGE, RUNTIME, STEP, TABLE, WALL_TIME, ChartLayout, ChartSpec, Panel, build_panels, display_names,
)
from ..compare import (
    MAX_OVERVIEW_LINES, CompareChart, ConfigDiff, compare_charts, diff_config, is_single_value, metric_rows,
    overview_column_widths, overview_rows, run_labels,
)
from ..images import ImageInfo, ImageSeries, ImageStep
from ..tables import TableData, TableInfo
from ..layout_store import project_key
from ..pins import Pins, matches_filter, pinned_first
from ..palette import series_color
from ..plain import plain
from ..series import Point, batches, extract_points, is_number, last_values
from .chart_widgets import CHART_CLASS, is_dark
from .charts import SERIES_BATCH, SERIES_WORKERS, _or_default
from .chart_editing import EDIT_ACTIONS, ChartEditing
from .compare_widgets import (
    CompareBarView, CompareBase, CompareImageView, ComparePlotView, CompareTableView,
)
from .imagefetch import fetch_image
from .filterinput import FilterInput
from .keybar import KeyBar

if TYPE_CHECKING:
    from ..app import WandbCliApp

CELL_WIDTH = 80
LOAD_WORKERS = 8  # requests in flight while the runs of a comparison load


def _clip(text: str, limit: int = CELL_WIDTH) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


def _clip_cells(text: str, max_cells: int) -> str:
    if cell_len(text) <= max_cells:
        return text
    if max_cells <= 1:
        return "…"[:max_cells]
    out, used = [], 0
    for ch in text:
        w = cell_len(ch)
        if used + w > max_cells - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def _chart_identity(chart: CompareChart) -> tuple:
    return (chart.kind, chart.metric, chart.metrics, chart.system, chart.x)


@dataclass
class RunData:
    """Everything fetched for one run of the comparison."""

    info: RunInfo
    meta: RunMetadata = field(default_factory=RunMetadata)
    config: dict[str, Any] | None = None
    metrics: list[str] = field(default_factory=list)
    values: dict[str, float] = field(default_factory=dict)
    system_rows: list[dict] = field(default_factory=list)
    tables: list[TableInfo] = field(default_factory=list)
    images: list[ImageInfo] = field(default_factory=list)
    error: str = ""

    def __post_init__(self) -> None:
        self.metric_set = set(self.metrics)
        self.system_last = last_values(self.system_rows)
        self.system_metrics = sorted(
            {k for r in self.system_rows for k, v in r.items() if not k.startswith("_") and is_number(v)}
        )
        self.system_set = set(self.system_metrics)

    def has(self, name: str, system: bool) -> bool:
        return name in (self.system_set if system else self.metric_set)

    def has_axis(self, x: str, system: bool) -> bool:
        if x in (STEP, RUNTIME, WALL_TIME):
            return not (system and x == STEP)  # system readings have no steps
        return not system and x in self.metric_set  # another metric as the x-axis


def fetch_run(client, ref: RunRef, name: str, pool: ThreadPoolExecutor | None = None) -> RunData:
    """All the data for one run (worker thread). Parts that fail are left empty and reported in `error`.
    The parts that need their own request (details, metadata, system metrics, and the run's summary
    with config/metrics/tables/images) are fetched at the same time, on `pool` if given."""
    errors: list[str] = []

    def part(fetch, default):
        try:
            return fetch()
        except Exception as exc:
            errors.append(str(exc))
            return default

    def summary() -> tuple:
        """Everything read from the loaded run (one request for the run, the rest is local)."""
        config = part(partial(client.run_config, ref), None)
        metrics = part(partial(client.metric_keys, ref), [])
        values = _or_default(partial(client.metric_values, ref), {})
        tables = _or_default(partial(client.run_tables, ref), [])
        images = _or_default(partial(client.run_images, ref), [])
        return config, metrics, values, tables, images

    own = pool is None
    pool = pool or ThreadPoolExecutor(max_workers=4)
    try:
        info_job = pool.submit(part, partial(client.run_details, ref), RunInfo(ref.id, name, "unavailable"))
        meta_job = pool.submit(_or_default, partial(client.run_metadata, ref), RunMetadata())  # no file: dashes
        rows_job = pool.submit(_or_default, partial(client.system_rows, ref), [])
        summary_job = pool.submit(summary)
        config, metrics, values, tables, images = summary_job.result()
        return RunData(info_job.result(), meta_job.result(), config, metrics, values, rows_job.result(),
                       tables, images, "; ".join(errors))
    finally:
        if own:
            pool.shutdown()


class CompareScreen(ChartEditing, Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("1", "tab('overview')", "Overview"),
        Binding("2", "tab('metrics')", "Metrics"),
        Binding("3", "tab('config')", "Config"),
        Binding("4", "tab('charts')", "Charts"),
        Binding("slash", "filter", "Filter"),
        Binding("p", "pin", "Pin"),
        Binding("i", "toggle_identical", "Hide identical rows"),
        Binding("n", "new_panel", "New panel"),
        Binding("a", "add_chart", "Add chart"),
        Binding("o", "chart_options", "Chart options"),
        Binding("delete", "delete_item", "Delete chart"),
        Binding("backspace", "delete_item", "Delete chart", show=False),  # laptops without a Delete key
        Binding("d", "reset_layout", "Default layout"),
        Binding("b", "flip_best", "Flip lower/higher is better"),
        Binding("B", "flip_all_best", "Flip all", show=False),
        Binding("r", "reload", "Refresh"),
        Binding("tab", "cycle_focus(1)", "Next chart"),
        Binding("shift+tab", "cycle_focus(-1)", "Previous", show=False),
        Binding("right", "focus_chart(0)", "Charts", show=False),
    ]
    DEFAULT_CSS = """
    #legend { padding: 0 1; height: auto; }
    #status { color: $text-muted; padding: 0 1; }
    #compare-filter { display: none; margin: 0 1; }
    #tabs { height: 1fr; }
    CompareScreen TabPane { height: 1fr; padding: 0; }
    #overview, #metrics, #config-table { height: 1fr; }
    #panels { width: 30; border: round $primary; }
    #panels:focus { border: round $accent; }
    #charts { width: 1fr; }
    """
    app: "WandbCliApp"

    def __init__(self, entity: str, project: str, runs: list[tuple[RunRef, str]]) -> None:
        super().__init__()
        self.entity, self.project = entity, project
        self.runs = runs
        self.labels = run_labels([n for _, n in runs], [r.id for r, _ in runs])
        self.data: list[RunData] = []
        self.ready = False
        # per table: hide the rows that are the same in every run (the config table starts that way)
        self.hide_identical = {"overview": False, "metrics": False, "config": True}
        self._hidden_overview_rows = 0
        self._overview_width = 0
        self.higher: set[str] = set()  # metrics where a higher value is better (the default: lower is)
        self.filters = {"metrics": "", "config": ""}  # what the user typed to narrow each table by key
        self.pinned: dict[str, list[str]] = {"metrics": [], "config": []}  # keys shown first
        self.chart_layout: ChartLayout | None = None  # the project's layout over the compared runs' metrics
        self.metrics: list[str] = []  # what the runs logged (the union): the choices when adding a chart
        self.system_metrics: list[str] = []
        self.tables: list[TableInfo] = []
        self.images: list[ImageInfo] = []
        self.selected = 0
        self._panel_labels: list[str] = []
        self._shown_panel: int | None = None
        self._render_lock = asyncio.Lock()
        self._series_cache: dict[tuple[int, bool, str, str], list[Point]] = {}
        self._loaded_count = 0

    # ---- layout ----------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="legend")
        yield Static("Loading runs…", id="status")
        yield FilterInput(placeholder="Filter by key", id="compare-filter")
        with TabbedContent(initial="overview-pane", id="tabs"):
            with TabPane("Overview", id="overview-pane"):
                yield DataTable(id="overview", cursor_type="row", zebra_stripes=True)
            with TabPane("Metrics", id="metrics-pane"):
                yield DataTable(id="metrics", cursor_type="row", zebra_stripes=True)
            with TabPane("Config", id="config-pane"):
                yield DataTable(id="config-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Charts", id="charts-pane"):
                with Horizontal():
                    yield OptionList(id="panels")
                    yield VerticalScroll(id="charts")
        yield KeyBar()

    def on_mount(self) -> None:
        self.title = f"Compare — {len(self.runs)} runs"
        self.sub_title = f"{self.entity}/{self.project}"
        legend = Text()  # every run's color and name, kept in view above all the tabs
        for color, label in zip(self._colors(), self.labels):
            if legend:
                legend.append("   ")
            legend.append("■ ", style=color)
            legend.append(label)
        self.query_one("#legend", Static).update(legend)
        self._load_higher()
        self.pins = Pins(self.app.preferences, project_key(self.entity, self.project))
        self.pinned = {"metrics": self.pins.get("metrics"), "config": self.pins.get("config")}
        self.query_one("#overview", DataTable).focus()
        self.load()

    # `id="overview-pane"` etc. are the TabbedContent's names for the tabs
    def action_tab(self, name: str) -> None:
        self.query_one("#tabs", TabbedContent).active = f"{name}-pane"

    @property
    def active_tab(self) -> str:
        return (self.query_one("#tabs", TabbedContent).active or "").removesuffix("-pane")

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        event.stop()
        target = {"overview": "#overview", "metrics": "#metrics", "config": "#config-table", "charts": "#panels"}.get(self.active_tab)
        if target:
            self.query_one(target).focus()
        box = self.query_one("#compare-filter", Input)
        shown = self.filters.get(self.active_tab, "")
        box.value = shown  # each table keeps its own filter
        box.display = bool(shown)
        self.refresh_bindings()
        if self.ready:
            self._update_status()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "toggle_identical":
            return self.active_tab in ("overview", "metrics", "config")
        if action in ("flip_best", "flip_all_best"):
            return self.active_tab == "metrics"
        if action in ("filter", "pin"):
            return self.active_tab in ("metrics", "config")
        if action in ("cycle_focus", "focus_chart", "focus_panels"):
            return self.active_tab == "charts"
        if action in EDIT_ACTIONS:
            return self.active_tab == "charts" and self.check_chart_action(action)
        return True

    def key_label(self, action: str) -> str | None:
        return self.chart_key_label(action) if self.active_tab == "charts" else None

    def _colors(self) -> list[str]:
        dark = is_dark(self)
        return [series_color(slot, dark) for slot in range(len(self.runs))]

    def _header(self, i: int, max_width: int | None = None) -> Text:
        label = self.labels[i]
        if max_width is not None:
            label = _clip_cells(label, max(1, max_width - 2))  # "■ " takes 2 cells
        return Text.assemble(("■ ", self._colors()[i]), label)

    def _status(self, text: str) -> None:
        self.query_one("#status", Static).update(plain(text))

    # ---- loading ---------------------------------------------------------------------
    @work(thread=True, exclusive=True, group="load")
    def load(self) -> None:
        client = self.app.client

        # one pool shared by every run: all runs and all their parts load at the same time, but never
        # more than LOAD_WORKERS requests are in flight; one thread per run only waits for its parts
        with ThreadPoolExecutor(max_workers=LOAD_WORKERS) as requests:
            def one(entry: tuple[RunRef, str]) -> RunData:
                data = fetch_run(client, *entry, pool=requests)
                self.app.call_from_thread(self._progress)
                return data

            with ThreadPoolExecutor(max_workers=len(self.runs)) as waiting:
                data = list(waiting.map(one, self.runs))
        self.app.call_from_thread(self._loaded, data)

    def action_reload(self) -> None:
        if not self.ready:
            return
        self.ready = False
        self._series_cache.clear()
        self._loaded_count = 0
        self._status("Loading runs…")
        self.load()

    def _progress(self) -> None:
        self._loaded_count += 1
        if not self.ready:
            self._status(f"Loading runs… {self._loaded_count}/{len(self.runs)}")

    async def _loaded(self, data: list[RunData]) -> None:
        panel = self.panels[self.selected].name if self.panels else None
        focused = self.focused
        focused_chart = (focused.chart, focused.index) if isinstance(focused, CompareBase) else None
        shown_before = (
            compare_charts(self.panels[self.selected])
            if self.panels and self._shown_panel == self.selected
            else None
        )

        self.data = data
        self.ready = True
        self._render_overview()
        self._render_metrics()
        self._render_config()
        self._build_panels(keep_panel=panel)
        mounted = list(self.query(f".{CHART_CLASS}"))
        same_panel = (
            bool(self.panels)
            and shown_before is not None
            and compare_charts(self.panels[self.selected]) == shown_before
            and bool(mounted)
        )
        self._shown_panel = self.selected  # keep OptionHighlighted from remounting on reload
        self._refresh_panel_list()
        if same_panel:
            self._reload_views(mounted, keep_position=True)  # same charts: update in place keeping focus & scroll
        else:
            focus_view: int | None = None
            if focused_chart is not None and self.panels:
                old_chart, old_index = focused_chart
                new_charts = compare_charts(self.panels[self.selected])
                if new_charts:
                    focus_view = next(
                        (i for i, c in enumerate(new_charts) if _chart_identity(c) == _chart_identity(old_chart)),
                        min(old_index, len(new_charts) - 1),
                    )
            await self._show_panel(focus_view=focus_view)
            if focused_chart is not None and focus_view is None:
                self.action_focus_panels()
        problems = [f"{self.labels[i]}: {d.error}" for i, d in enumerate(data) if d.error]
        if problems:
            self.notify("Some data could not be loaded — " + "; ".join(problems), severity="warning")
        self._update_status()

    def _update_status(self) -> None:
        if self.is_attached:
            self.refresh_bindings()
        tab = self.active_tab
        if tab == "config":
            self._status(self._config_summary)
        elif tab == "metrics":
            self._status(self._metrics_summary)
        elif tab == "charts":
            self._status(f"{len(self.panels)} panels")
        else:
            hidden = f"  ·  {self._hidden_overview_rows} identical rows hidden" if self._hidden_overview_rows else ""
            self._status(f"{len(self.runs)} runs{hidden}")

    # ---- overview --------------------------------------------------------------------
    def _fill(self, table: DataTable, first: str) -> None:
        table.clear(columns=True)
        table.add_column(first, key="label")
        for i in range(len(self.runs)):
            table.add_column(self._header(i), key=str(i))

    def _overview_available_width(self) -> int:
        table = self.query_one("#overview", DataTable)
        width = table.size.width or self.size.width
        return max(0, width - table.styles.scrollbar_size_vertical)

    def on_resize(self) -> None:
        if self.ready and self._overview_available_width() != self._overview_width:
            self._render_overview()

    def _render_overview(self) -> None:
        table = self.query_one("#overview", DataTable)
        cursor = table.cursor_row if table.row_count else 0
        rows = overview_rows([d.info for d in self.data], [d.meta for d in self.data])
        shown = [r for r in rows if r.differs or not self.hide_identical["overview"]]
        self._hidden_overview_rows = len(rows) - len(shown)

        label_texts = [("≠ " if r.differs else "  ") + r.label for r in shown]
        label_width = max((cell_len(t) for t in label_texts), default=0)
        naturals = [
            max([cell_len(f"■ {self.labels[i]}"), *(cell_len(r.values[i]) for r in shown)], default=0)
            for i in range(len(self.runs))
        ]
        available = self._overview_available_width()
        self._overview_width = available
        widths = overview_column_widths(
            label_width, naturals, available, padding=table.cell_padding, fallback_max=CELL_WIDTH
        )

        table.clear(columns=True)
        table.add_column("", width=label_width or None, key="label")
        for i, width in enumerate(widths):
            table.add_column(self._header(i, width), width=width or None, key=str(i))
        for row, label_text in zip(shown, label_texts):
            label = Text(label_text, style="bold" if row.differs else "dim")
            cells = [plain(_clip(v, max(w * MAX_OVERVIEW_LINES, 1))) for v, w in zip(row.values, widths)]
            table.add_row(label, *cells, height=None)
        self._place_cursor(table, cursor)

    # ---- metrics -----------------------------------------------------------------------
    _metrics_summary = ""
    PREF_HIGHER = "compare_higher_is_better"  # per project: the metrics where higher is better

    @property
    def _higher_key(self) -> str:
        return project_key(self.entity, self.project)

    def _load_higher(self) -> None:
        stored = self.app.preferences.get(self.PREF_HIGHER)
        names = stored.get(self._higher_key) if isinstance(stored, dict) else None
        self.higher = {n for n in names if isinstance(n, str)} if isinstance(names, list) else set()

    def _save_higher(self) -> None:
        stored = self.app.preferences.get(self.PREF_HIGHER)
        stored = dict(stored) if isinstance(stored, dict) else {}
        if self.higher:
            stored[self._higher_key] = sorted(self.higher)
        else:
            stored.pop(self._higher_key, None)
        self.app.preferences.set(self.PREF_HIGHER, stored)

    def _render_metrics(self) -> None:
        """The latest value of every metric, per run, with the best run of each marked."""
        table = self.query_one("#metrics", DataTable)
        cursor = table.cursor_row if table.row_count else 0
        self._fill(table, "Metric")
        table.add_column("Better", key="better")
        table.add_column("Best", key="best")
        colors = self._colors()
        rows = metric_rows([d.values for d in self.data], self.higher)
        pinned = self.pinned["metrics"]
        wanted = [
            r for r in rows
            if matches_filter(r.name, self.filters["metrics"])
            and (r.differs or not self.hide_identical["metrics"] or r.name in pinned)  # a pin is always listed
        ]
        for row in pinned_first(wanted, pinned, lambda r: r.name):
            cells = [
                Text("—", style="dim") if v is None
                else Text(f"{v:.6g}", style="bold green" if i == row.best else "")
                for i, v in enumerate(row.values)
            ]
            best = Text.assemble((" ■ ", colors[row.best]), self.labels[row.best]) if row.best is not None else Text("")
            table.add_row(
                self._key_cell(row.name, row.name in pinned, "bold" if row.differs else "dim"), *cells,
                Text("↑ higher" if row.higher else "↓ lower", style="dim"), best, key=row.name,
            )
        self._place_cursor(table, cursor)
        self._metrics_summary = self._count_text(len(wanted), len(rows), self.filters["metrics"], "metrics")

    @staticmethod
    def _key_cell(name: str, pinned: bool, style: str) -> Text:
        """A metric / config key as a cell; pinned ones are marked with a star."""
        return Text.assemble(*([("★ ", "bold yellow")] if pinned else []), (name, style))

    @staticmethod
    def _count_text(shown: int, total: int, text: str, what: str) -> str:
        if not total:
            return f"No {what}."
        return f"{shown} of {total} {what}" + (f"  ·  filter “{text}”" if text else "")

    @staticmethod
    def _place_cursor(table: DataTable, cursor: int) -> None:
        """Keep the cursor on its row number after a redraw (rows may have moved under it)."""
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1), animate=False)

    def _metric_under_cursor(self) -> str | None:
        table = self.query_one("#metrics", DataTable)
        if not table.row_count:
            return None
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    def action_flip_best(self) -> None:
        """Lower <-> higher is better, for the metric under the cursor (remembered for the project)."""
        name = self._metric_under_cursor()
        if name is not None:
            self.higher ^= {name}
            self._save_higher()
            self._render_metrics()
            self._update_status()

    def action_flip_all_best(self) -> None:
        names = {r.name for r in metric_rows([d.values for d in self.data])}
        self.higher = names - self.higher
        self._save_higher()
        self._render_metrics()
        self._update_status()

    # ---- config ----------------------------------------------------------------------
    _config_summary = ""

    def _render_config(self) -> None:
        table = self.query_one("#config-table", DataTable)
        cursor = table.cursor_row if table.row_count else 0
        self._fill(table, "Config entry")
        pinned = self.pinned["config"]
        diff: ConfigDiff = diff_config([d.config for d in self.data], show_all=not self.hide_identical["config"], pinned=pinned)
        shown = [r for r in diff.rows if matches_filter(r.path, self.filters["config"])]
        for row in pinned_first(shown, pinned, lambda r: r.path):
            identical = len({v for v in row.values}) == 1 and None not in row.values
            style = "dim" if identical else "bold yellow" if row.only_some else "bold"
            table.add_row(
                self._key_cell(row.path, row.path in pinned, style),
                *(Text("—", style="dim") if v is None else plain(v) for v in row.values),
                key=row.path,
            )
        self._place_cursor(table, cursor)
        if self.filters["config"]:
            self._config_summary = self._count_text(len(shown), len(diff.rows), self.filters["config"], "entries")
        elif not diff.rows and diff.same:
            self._config_summary = f"The configs are identical ({diff.same} entries)"
        elif not diff.rows:
            self._config_summary = "None of the runs has a config"
        else:
            self._config_summary = f"{diff.different} differ  ·  {diff.partial} only in some runs  ·  {diff.same} identical"

    # ---- filtering and pinning (metrics and config) ------------------------------------------
    def _render_tab(self, tab: str) -> None:
        if tab == "metrics":
            self._render_metrics()
        elif tab == "config":
            self._render_config()

    def action_filter(self) -> None:
        box = self.query_one("#compare-filter", Input)
        box.display = True
        box.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "compare-filter":
            return
        event.stop()
        tab, text = self.active_tab, event.value.strip()
        if tab in self.filters and self.filters[tab] != text:
            self.filters[tab] = text
            self._render_tab(tab)
            self._update_status()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "compare-filter":
            event.stop()
            self._focus_table()  # the filter stays; Esc clears it

    def action_focus_results(self) -> None:
        self._focus_table()

    def _focus_table(self) -> None:
        target = {"metrics": "#metrics", "config": "#config-table"}.get(self.active_tab)
        if target:
            self.query_one(target).focus()

    def action_back(self) -> None:
        box = self.query_one("#compare-filter", Input)
        if box.display:  # first Esc closes the filter, the next one goes back
            box.value = ""
            box.display = False
            self._focus_table()
        else:
            self.app.pop_screen()

    def action_pin(self) -> None:
        """Pin the metric / config key under the cursor to the top of its table; again unpins."""
        tab = self.active_tab
        if not self.ready or tab not in ("metrics", "config"):
            return
        table = self.query_one("#metrics" if tab == "metrics" else "#config-table", DataTable)
        if not table.row_count:
            return
        key = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        self.pins.toggle(tab, key)
        self.pinned[tab] = self.pins.get(tab)
        self._render_tab(tab)  # the cursor stays on its row number: the row that was just above the pin

    def toggle_state(self, action: str) -> bool | None:
        """Shown on the key: whether identical rows are hidden in the table being looked at."""
        return self.hide_identical.get(self.active_tab) if action == "toggle_identical" else None

    def action_toggle_identical(self) -> None:
        tab = self.active_tab
        if not self.ready or tab not in self.hide_identical:
            return
        self.hide_identical[tab] = not self.hide_identical[tab]
        if tab == "overview":
            self._render_overview()
        else:
            self._render_tab(tab)
        self.refresh_bindings()  # the key shows the new state
        self._update_status()

    # ---- charts ----------------------------------------------------------------------
    empty_note = "None of these runs has metrics, tables or images to chart."

    @property
    def panels(self) -> list[Panel]:
        return self.chart_layout.panels if self.chart_layout else []

    @property
    def _layout_key(self) -> str:
        return project_key(self.entity, self.project)

    def _build_panels(self, keep_panel: str | None = None) -> None:
        """The project's chart layout (what the user set up in the run screens, and here) over the
        metrics of all the compared runs."""
        self.metrics = sorted({m for d in self.data for m in d.metrics})
        self.system_metrics = sorted({m for d in self.data for m in d.system_metrics})
        self.tables = sorted({t.key: t for d in self.data for t in d.tables}.values(), key=lambda t: t.key)
        self.images = sorted({i.key: i for d in self.data for i in d.images}.values(), key=lambda i: i.key)
        self.chart_layout = ChartLayout(
            build_panels(self.metrics, self.system_metrics, [t.key for t in self.tables], [i.key for i in self.images]),
            self.app.layout_store.load(self._layout_key),
        )
        self.selected = self.chart_layout.index_of(keep_panel) if keep_panel and self.panels else 0

    def _focused_chart(self) -> tuple[ChartSpec, int] | None:
        """The focused chart's layout chart and its position in the panel (a plot with several metrics
        is drawn as several charts, but is one chart of the layout)."""
        view = self.focused
        return (view.spec, view.layout_index) if isinstance(view, CompareBase) and view.spec else None

    def _refresh_panel_list(self) -> None:
        options = self.query_one("#panels", OptionList)
        labels = [f"{p.name} ({len(p.charts)})" for p in self.panels]
        if labels != self._panel_labels:  # an unchanged list (most reloads) is not redrawn
            self._panel_labels = labels
            options.clear_options()
            options.add_options([Option(plain(text), id=str(i)) for i, text in enumerate(labels)])
        if self.panels:
            options.highlighted = self.selected

    def _reload_views(self, views: list, keep_position: bool = False) -> None:
        self._load_plots([v for v in views if isinstance(v, ComparePlotView)])  # together: one batch per run
        for view in views:
            if isinstance(view, CompareImageView):
                self._load_images(view, keep_position=keep_position)
            elif not isinstance(view, ComparePlotView):
                self._load_chart(view)

    async def _show_panel(self, focus: int | None = None, *, focus_view: int | None = None) -> None:
        async with self._render_lock:
            self.workers.cancel_group(self, "plots")  # nothing is fetched for a panel that is no longer shown
            self._shown_panel = self.selected
            container = self.query_one("#charts", VerticalScroll)
            await container.remove_children()
            if not self.panels:
                note = "No panels." if self.chart_layout and self.chart_layout.is_customized else "None of the runs logged any metrics."
                await container.mount(Static(note, markup=False))
                return
            charts = compare_charts(self.panels[self.selected])
            if not charts:
                await container.mount(Static("No charts in this panel.", markup=False))
                return
            views = [self._make_view(c, i) for i, c in enumerate(charts)]
            await container.mount_all(views)
            self._reload_views(views)
            if focus_view is not None:
                views[min(focus_view, len(views) - 1)].focus()
            elif focus is not None:  # a layout chart: its first drawn chart gets the focus
                target = next((v for v in views if v.layout_index == focus), views[-1])
                target.focus()

    async def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id != "panels" or not self.ready or not self.panels:
            return
        if event.option_index != self._shown_panel:
            self.selected = event.option_index
            await self._show_panel()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_focus_chart(0)

    def _make_view(self, chart: CompareChart, index: int) -> CompareBase:
        kinds = {BAR: CompareBarView, TABLE: CompareTableView, IMAGE: CompareImageView}
        return kinds.get(chart.kind, ComparePlotView)(chart, index, self.labels)

    def _load_chart(self, view: CompareBase) -> None:
        chart = view.chart
        if chart.kind == TABLE:
            self._load_tables(view)
            return
        if chart.kind == IMAGE:
            self._load_images(view)
            return
        if chart.kind == BAR:
            names = display_names(chart.metrics, chart.system)
            groups = [
                (label, [d.values.get(m, d.system_last.get(m)) for d in self.data])
                for m, label in zip(chart.metrics, names)
            ]
            if all(v is None for _, values in groups for v in values):
                view.show_unavailable("None of these metrics were logged in the compared runs.")  # type: ignore[attr-defined]
            else:
                view.show(groups)  # type: ignore[attr-defined]
            return
        self._load_plots([view])  # (a panel's plots are normally loaded together: see _show_panel)

    def _plot_runs(self, chart: CompareChart) -> list[int]:
        """The runs that can draw this plot at all."""
        return [i for i, d in enumerate(self.data) if d.has(chart.metric, chart.system) and d.has_axis(chart.x, chart.system)]

    def _load_plots(self, views: list[ComparePlotView]) -> None:
        """Fill the plots of a panel. Each run is asked once per x-axis for ALL the metrics the panel's
        plots need from it (one request carries a whole batch), and a plot is drawn as soon as every
        run that has it has answered."""
        needs: dict[tuple[int, str], list[str]] = {}  # (run, x-axis) -> metrics to fetch, in chart order
        waiting: list[ComparePlotView] = []
        for view in views:
            chart = view.chart
            have = self._plot_runs(chart)
            if not have:
                view.show_unavailable(f"No compared run logged “{chart.metric}” against this x-axis.")
                continue
            missing = False
            for i in have:
                key = (i, chart.system, chart.metric, chart.x)
                if key in self._series_cache:
                    continue
                if chart.system:  # system series come from the rows already loaded
                    self._series_cache[key] = extract_points(self.data[i].system_rows, chart.x, chart.metric)
                else:
                    missing = True
                    names = needs.setdefault((i, chart.x), [])
                    if chart.metric not in names:
                        names.append(chart.metric)
            if missing:
                waiting.append(view)
            else:
                self._show_plot(view)
        if waiting:
            self._fetch_plots(waiting, needs)

    def _show_plot(self, view: ComparePlotView) -> None:
        """Draw a plot if every run that has it has answered."""
        chart = view.chart
        have = self._plot_runs(chart)
        keys = {i: (i, chart.system, chart.metric, chart.x) for i in have}
        if not view.is_attached or any(k not in self._series_cache for k in keys.values()):
            return
        series = {i: self._series_cache[k] for i, k in keys.items()}
        if not any(series.values()):
            view.show_unavailable(f"No data for “{chart.metric}” in the compared runs.")
        else:
            view.show(series, single=is_single_value(series.values()))

    @work(thread=True, group="plots", exclusive=True)
    def _fetch_plots(self, views: list[ComparePlotView], needs: dict[tuple[int, str], list[str]]) -> None:
        """exclusive: showing another panel cancels this, and batches not yet asked for are skipped."""
        worker = get_current_worker()
        stop = threading.Event()
        tasks = [(run, x, chunk) for (run, x), names in needs.items() for chunk in batches(names, SERIES_BATCH)]

        def one(run: int, x: str, chunk: list[str]):
            if worker.is_cancelled or stop.is_set():
                return None
            return self.app.client.metric_series_many(self.runs[run][0], chunk, x)

        # every batch of every run goes out together (never more than LOAD_WORKERS at once)
        with ThreadPoolExecutor(max_workers=min(len(tasks), LOAD_WORKERS)) as pool:
            futures = [pool.submit(one, *task) for task in tasks]
            for (run, x, _chunk), future in zip(tasks, futures):
                try:
                    found = future.result()
                except Exception as exc:
                    stop.set()
                    self.app.call_from_thread(self._plots_failed, views, str(exc))
                    return
                if found is None or worker.is_cancelled:
                    return
                self.app.call_from_thread(self._plots_ready, views, run, x, found)

    def _plots_ready(self, views: list[ComparePlotView], run: int, x: str, found: dict[str, list[Point]]) -> None:
        for name, points in found.items():
            self._series_cache[(run, False, name, x)] = points
        for view in views:
            self._show_plot(view)

    def _plots_failed(self, views: list[ComparePlotView], message: str) -> None:
        for view in views:
            if view.is_attached:
                view.fail(message)

    # ---- tables and images ----------------------------------------------------------------
    def _load_tables(self, view: CompareTableView) -> None:
        key = view.chart.metric
        infos = [next((t for t in d.tables if t.key == key), None) for d in self.data]
        if not any(infos):
            view.show_unavailable(f"No compared run logged a table called “{key}”.")
        else:
            self._fetch_tables(view, infos)

    @work(thread=True)
    def _fetch_tables(self, view: CompareTableView, infos: list[TableInfo | None]) -> None:
        def one(i: int) -> TableData | None:
            return self.app.client.table_data(self.runs[i][0], infos[i].path) if infos[i] else None

        try:
            with ThreadPoolExecutor(max_workers=min(len(infos), SERIES_WORKERS)) as pool:
                tables = list(pool.map(one, range(len(infos))))
        except Exception as exc:
            self.app.call_from_thread(view.fail, str(exc))
            return
        self.app.call_from_thread(self._tables_ready, view, tables)

    def _tables_ready(self, view: CompareTableView, tables: list[TableData | None]) -> None:
        if view.is_attached:
            view.show_tables(tables)

    def _load_images(self, view: CompareImageView, keep_position: bool = False) -> None:
        key = view.chart.metric
        have = [i for i, d in enumerate(self.data) if any(img.key == key for img in d.images)]
        if not have:
            view.show_unavailable(f"No compared run logged images called “{key}”.")
        else:
            self._fetch_steps(view, key, have, keep_position)

    @work(thread=True)
    def _fetch_steps(self, view: CompareImageView, key: str, have: list[int], keep_position: bool = False) -> None:
        def one(i: int) -> ImageSeries:
            return self.app.client.image_steps(self.runs[i][0], key)

        try:
            with ThreadPoolExecutor(max_workers=min(len(have), SERIES_WORKERS)) as pool:
                found = dict(zip(have, pool.map(one, have)))
        except Exception as exc:
            self.app.call_from_thread(view.show_unavailable, f"Could not read the images: {exc}")
            return
        series = [found.get(i) for i in range(len(self.runs))]
        self.app.call_from_thread(self._steps_ready, view, series, keep_position)

    def _steps_ready(self, view: CompareImageView, series: list[ImageSeries | None], keep_position: bool = False) -> None:
        if view.is_attached:
            view.set_series(series, keep_position=keep_position)

    def on_compare_image_view_wanted(self, event: CompareImageView.Wanted) -> None:
        view = event.view
        if not view.is_attached:
            return
        token = view.token()
        for run in range(len(self.runs)):
            step = view.step_for(run)
            if step is not None:
                self._fetch_image(view, run, step, min(view.sub, len(step.paths) - 1), token)

    @work(thread=True, group="images")
    def _fetch_image(self, view: CompareImageView, run: int, step: ImageStep, sub: int, token: tuple[int, int]) -> None:
        try:
            thumb, original = fetch_image(self.app, self.runs[run][0], step, sub)
        except Exception as exc:
            self.app.call_from_thread(view.fail, run, str(exc), token)
            return
        self.app.call_from_thread(view.show_image, run, thumb, token, original)

    # ---- focus -----------------------------------------------------------------------
    def action_focus_chart(self, delta: int) -> None:
        views = list(self.query(f".{CHART_CLASS}"))
        if not views:
            return
        current = self.focused
        index = current.index + delta if isinstance(current, CompareBase) else 0
        views[max(0, min(index, len(views) - 1))].focus()

    def action_cycle_focus(self, delta: int) -> None:
        stops = [self.query_one("#panels", OptionList), *self.query(f".{CHART_CLASS}")]
        current = self.focused
        here = stops.index(current) if current in stops else 0
        stops[(here + delta) % len(stops)].focus()

    def action_focus_panels(self) -> None:
        self.query_one("#panels", OptionList).focus()
