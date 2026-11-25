"""Compare several runs of one project: their facts, their configs (only what differs) and their charts.

Every run has a color (its slot in the legend at the top) that follows it everywhere: the column
headers, the curves of a plot, the bars of a bar chart."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Header, OptionList, Static, TabbedContent, TabPane
from textual.widgets.option_list import Option

from ..api import RunInfo, RunMetadata, RunRef
from ..charts import BAR, RUNTIME, STEP, WALL_TIME, apply_customization, build_panels, display_names
from ..compare import (
    CompareChart, ConfigDiff, compare_charts, diff_config, is_single_value, overview_rows, run_labels,
)
from ..layout_store import project_key
from ..palette import series_color
from ..plain import plain
from ..series import Point, extract_points, is_number, last_values
from .chart_widgets import CHART_CLASS, is_dark
from .charts import SERIES_WORKERS, _or_default
from .compare_widgets import CompareBarView, CompareBase, ComparePlotView
from .keybar import KeyBar

if TYPE_CHECKING:
    from ..app import WandbCliApp

CELL_WIDTH = 80


def _clip(text: str) -> str:
    return text if len(text) <= CELL_WIDTH else text[: CELL_WIDTH - 1] + "…"


@dataclass
class RunData:
    """Everything fetched for one run of the comparison."""

    info: RunInfo
    meta: RunMetadata = field(default_factory=RunMetadata)
    config: dict[str, Any] | None = None
    metrics: list[str] = field(default_factory=list)
    values: dict[str, float] = field(default_factory=dict)
    system_rows: list[dict] = field(default_factory=list)
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


def fetch_run(client, ref: RunRef, name: str) -> RunData:
    """All the data for one run (worker thread). Parts that fail are left empty and reported in `error`."""
    errors: list[str] = []

    def part(fetch, default):
        try:
            return fetch()
        except Exception as exc:
            errors.append(str(exc))
            return default

    info = part(partial(client.run_details, ref), RunInfo(ref.id, name, "unavailable"))
    meta = _or_default(partial(client.run_metadata, ref), RunMetadata())  # no metadata file: dashes
    config = part(partial(client.run_config, ref), None)
    metrics = part(partial(client.metric_keys, ref), [])
    values = _or_default(partial(client.metric_values, ref), {})
    rows = _or_default(partial(client.system_rows, ref), [])
    return RunData(info, meta, config, metrics, values, rows, "; ".join(errors))


class CompareScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("1", "tab('overview')", "Overview"),
        Binding("2", "tab('config')", "Config"),
        Binding("3", "tab('charts')", "Charts"),
        Binding("d", "toggle_identical", "Toggle identical rows"),
        Binding("r", "reload", "Refresh"),
        Binding("tab", "cycle_focus(1)", "Next chart"),
        Binding("shift+tab", "cycle_focus(-1)", "Previous", show=False),
        Binding("right", "focus_chart(0)", "Charts", show=False),
    ]
    DEFAULT_CSS = """
    #legend { padding: 0 1; height: auto; }
    #status { color: $text-muted; padding: 0 1; }
    CompareScreen TabPane { padding: 0; }
    #overview, #config-table { height: 1fr; }
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
        self.overview_only_diff = False
        self.config_show_all = False
        self.panels: list[tuple[str, list[CompareChart]]] = []
        self.selected = 0
        self._shown_panel: int | None = None
        self._render_lock = asyncio.Lock()
        self._series_cache: dict[tuple[int, bool, str, str], list[Point]] = {}
        self._loaded_count = 0

    # ---- layout ----------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="legend")
        yield Static("Loading runs…", id="status")
        with TabbedContent(initial="overview-pane", id="tabs"):
            with TabPane("Overview", id="overview-pane"):
                yield DataTable(id="overview", cursor_type="row", zebra_stripes=True)
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
        target = {"overview": "#overview", "config": "#config-table", "charts": "#panels"}.get(self.active_tab)
        if target:
            self.query_one(target).focus()
        self.refresh_bindings()
        if self.ready:
            self._update_status()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "toggle_identical":
            return self.active_tab in ("overview", "config")
        if action in ("cycle_focus", "focus_chart", "focus_panels"):
            return self.active_tab == "charts"
        return True

    def _colors(self) -> list[str]:
        dark = is_dark(self)
        return [series_color(slot, dark) for slot in range(len(self.runs))]

    def _header(self, i: int) -> Text:
        return Text.assemble(("■ ", self._colors()[i]), self.labels[i])

    def _status(self, text: str) -> None:
        self.query_one("#status", Static).update(plain(text))

    # ---- loading ---------------------------------------------------------------------
    @work(thread=True, exclusive=True, group="load")
    def load(self) -> None:
        client = self.app.client

        def one(entry: tuple[RunRef, str]) -> RunData:
            data = fetch_run(client, *entry)
            self.app.call_from_thread(self._progress)
            return data

        with ThreadPoolExecutor(max_workers=min(len(self.runs), SERIES_WORKERS)) as pool:
            data = list(pool.map(one, self.runs))
        self.app.call_from_thread(self._loaded, data)

    def action_reload(self) -> None:
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
        self.data = data
        self.ready = True
        self._render_overview()
        self._render_config()
        self._build_panels()
        self._refresh_panel_list()
        await self._show_panel()
        problems = [f"{self.labels[i]}: {d.error}" for i, d in enumerate(data) if d.error]
        if problems:
            self.notify("Some data could not be loaded — " + "; ".join(problems), severity="warning")
        self._update_status()

    def _update_status(self) -> None:
        tab = self.active_tab
        if tab == "config":
            self._status(self._config_summary)
        elif tab == "charts":
            self._status(
                f"{len(self.panels)} panels from this project's chart layout, one color per run  ·  "
                "tables and images are not compared"
            )
        else:
            self._status(f"{len(self.runs)} runs  ·  ≠ marks facts that differ")

    # ---- overview --------------------------------------------------------------------
    def _fill(self, table: DataTable, first: str) -> None:
        table.clear(columns=True)
        table.add_column(first, key="label")
        for i in range(len(self.runs)):
            table.add_column(self._header(i), key=str(i))

    def _render_overview(self) -> None:
        table = self.query_one("#overview", DataTable)
        self._fill(table, "")
        rows = overview_rows([d.info for d in self.data], [d.meta for d in self.data])
        for row in rows:
            if self.overview_only_diff and not row.differs:
                continue
            label = Text(("≠ " if row.differs else "  ") + row.label, style="bold" if row.differs else "dim")
            table.add_row(label, *(plain(_clip(v)) for v in row.values))

    # ---- config ----------------------------------------------------------------------
    _config_summary = ""

    def _render_config(self) -> None:
        table = self.query_one("#config-table", DataTable)
        self._fill(table, "Config entry")
        diff: ConfigDiff = diff_config([d.config for d in self.data], show_all=self.config_show_all)
        for row in diff.rows:
            identical = len({v for v in row.values}) == 1 and None not in row.values
            style = "dim" if identical else "bold yellow" if row.only_some else "bold"
            table.add_row(
                Text(row.path, style=style),
                *(Text("—", style="dim") if v is None else plain(v) for v in row.values),
            )
        if not diff.rows and diff.same:
            self._config_summary = f"The configs are identical ({diff.same} entries)."
        elif not diff.rows:
            self._config_summary = "None of the runs has a config."
        else:
            self._config_summary = (
                f"{diff.different} entries differ, {diff.partial} are only in some runs (yellow), "
                f"{diff.same} are identical" + ("" if self.config_show_all else " (hidden: d shows them)")
            )

    def action_toggle_identical(self) -> None:
        if not self.ready:
            return
        if self.active_tab == "overview":
            self.overview_only_diff = not self.overview_only_diff
            self._render_overview()
        elif self.active_tab == "config":
            self.config_show_all = not self.config_show_all
            self._render_config()
        self._update_status()

    # ---- charts ----------------------------------------------------------------------
    def _build_panels(self) -> None:
        """The project's chart layout (what the user set up in the run screens) over the metrics of
        all the compared runs, reduced to the charts that can be overlaid."""
        metrics = sorted({m for d in self.data for m in d.metrics})
        system = sorted({m for d in self.data for m in d.system_metrics})
        custom = self.app.layout_store.load(project_key(self.entity, self.project))
        panels = apply_customization(build_panels(metrics, system), custom)
        self.panels = [(p.name, c) for p in panels if (c := compare_charts(p))]
        self.selected = 0

    def _refresh_panel_list(self) -> None:
        options = self.query_one("#panels", OptionList)
        options.clear_options()
        options.add_options([Option(plain(f"{n} ({len(c)})"), id=str(i)) for i, (n, c) in enumerate(self.panels)])
        if self.panels:
            options.highlighted = self.selected

    async def _show_panel(self) -> None:
        async with self._render_lock:
            self._shown_panel = self.selected
            container = self.query_one("#charts", VerticalScroll)
            await container.remove_children()
            if not self.panels:
                await container.mount(Static("None of the runs logged any metrics.", markup=False))
                return
            views = [
                (CompareBarView if c.kind == BAR else ComparePlotView)(c, i, self.labels)
                for i, c in enumerate(self.panels[self.selected][1])
            ]
            await container.mount_all(views)
            for view in views:
                self._load_chart(view)

    async def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id != "panels" or not self.ready:
            return
        if event.option_index != self._shown_panel:
            self.selected = event.option_index
            await self._show_panel()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_focus_chart(0)

    def _load_chart(self, view: CompareBase) -> None:
        chart = view.chart
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
        have = [i for i, d in enumerate(self.data) if d.has(chart.metric, chart.system) and d.has_axis(chart.x, chart.system)]
        if not have:
            view.show_unavailable(f"No compared run logged “{chart.metric}” against this x-axis.")  # type: ignore[attr-defined]
        else:
            self._fetch_plot(view, have)

    def _points(self, i: int, chart: CompareChart) -> list[Point]:
        key = (i, chart.system, chart.metric, chart.x)
        if key not in self._series_cache:
            if chart.system:  # system series come from the rows already loaded
                self._series_cache[key] = extract_points(self.data[i].system_rows, chart.x, chart.metric)
            else:
                self._series_cache[key] = self.app.client.metric_series(self.runs[i][0], chart.metric, chart.x)
        return self._series_cache[key]

    @work(thread=True)
    def _fetch_plot(self, view: ComparePlotView, have: list[int]) -> None:
        chart = view.chart
        try:
            with ThreadPoolExecutor(max_workers=min(len(have), SERIES_WORKERS)) as pool:  # independent requests
                series = dict(zip(have, pool.map(lambda i: self._points(i, chart), have)))
        except Exception as exc:
            self.app.call_from_thread(view.fail, str(exc))
            return
        self.app.call_from_thread(self._plot_ready, view, series)

    def _plot_ready(self, view: ComparePlotView, series: dict[int, list[Point]]) -> None:
        if not view.is_attached:
            return
        if not any(series.values()):
            view.show_unavailable(f"No data for “{view.chart.metric}” in the compared runs.")
        else:
            view.show(series, single=is_single_value(series.values()))

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
