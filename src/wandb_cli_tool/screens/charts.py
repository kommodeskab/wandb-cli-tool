from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Callable, TypeVar

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, OptionList, Static
from textual.widgets.option_list import Option

from ..api import RunRef
from ..charts import (
    BAR, IMAGE, PLOT, RUNTIME, STEP, TABLE, WALL_TIME, ChartLayout, ChartSpec, Panel, axis_label, build_panels,
)
from ..images import ImageInfo, ImageSeries, ImageStep, make_thumbnail
from ..media import MAX_IMAGE_BYTES
from ..api import FileInfo
from ..layout_store import project_key
from ..plain import plain
from ..refresh import PREF_KEY, interval_from_pref, label as interval_label, next_choice, to_pref
from ..series import Point, extract_points, is_number, last_values
from ..tables import TableData, TableInfo
from .chart_dialogs import AxisDialog, BarDialog, ChartKindDialog, ImageDialog, PlotDialog, TableDialog
from .chart_widgets import (  # noqa: F401
    CHART_CLASS, BarView, ChartBase, ChartView, ImageChartView, TableView, make_chart_widget,
)
from .files import DOWNLOAD_CACHE_BYTES, DOWNLOAD_NAMESPACE, download_dir
from .dialogs import ConfirmDialog, NewPanelDialog

if TYPE_CHECKING:
    from ..app import WandbCliApp

T = TypeVar("T")
SERIES_WORKERS = 4  # series of one chart fetched at once


def _or_default(fetch: Callable[[], T], default: T) -> T:
    """`fetch()`, or `default` if it fails: the run can still be browsed without that part."""
    try:
        return fetch()
    except Exception:
        return default


@dataclass
class ChartData:
    """Everything the screen shows, as fetched from W&B in one go."""

    metrics: list[str]
    rows: list[dict]
    note: str
    values: dict[str, float]
    tables: list[TableInfo]
    state: str  # the run's state ("running", "finished", ...), "" if unknown
    images: list[ImageInfo] = field(default_factory=list)


class ChartsScreen(Screen):
    """Panels on the left, the selected panel's charts on the right."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("n", "new_panel", "New panel"),
        Binding("a", "add_chart", "Add chart"),
        Binding("x", "change_x", "X-axis"),
        Binding("delete", "delete_chart", "Delete chart"),
        Binding("backspace", "delete_chart", "Delete chart", show=False),  # laptops without a Delete key
        Binding("d", "reset_layout", "Default layout"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("f", "cycle_refresh", "Auto-refresh"),
        Binding("tab", "cycle_focus(1)", "Next chart"),
        Binding("shift+tab", "cycle_focus(-1)", "Previous", show=False),
        Binding("right", "focus_chart(0)", "Charts", show=False),
    ]
    DEFAULT_CSS = """
    #panels { width: 30; border: round $primary; }
    #panels:focus { border: round $accent; }
    #charts { width: 1fr; }
    #status { color: $text-muted; padding: 0 1; }
    """
    app: "WandbCliApp"

    def __init__(self, ref: RunRef, name: str) -> None:
        super().__init__()
        self.ref = ref
        self.run_name = name
        self.ready = False
        self.metrics: list[str] = []
        self._metric_set: set[str] = set()  # what THIS run logged: shared charts may ask for more
        self._system_set: set[str] = set()
        self.system_metrics: list[str] = []
        self.system_rows: list[dict] = []
        self.values: dict[str, float] = {}  # latest value of each metric (for bar charts)
        self.system_last: dict[str, float] = {}
        self.tables: list[TableInfo] = []
        self.images: list[ImageInfo] = []
        self._table_cache: dict[str, TableData] = {}
        self.chart_layout: ChartLayout | None = None
        self._status_base = ""
        self.selected = 0
        self._panel_labels: list[str] = []
        self._shown_panel: int | None = None
        self._cache: dict[tuple[bool, str, str], list[Point]] = {}
        self._render_lock = asyncio.Lock()
        # periodic refresh
        self.state = ""
        self.live = False  # the run is still running, so new data may arrive
        self.interval: float | None = None
        self._timer = None
        self._refreshing = False
        self._last_refresh: datetime | None = None
        self._refresh_error = ""
        self._told_scope = False

    @property
    def panels(self) -> list[Panel]:
        return self.chart_layout.panels if self.chart_layout else []

    @property
    def _layout_key(self) -> str:
        """Layouts belong to the project: every run in it shows the same customizations."""
        return project_key(self.ref.entity, self.ref.project)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Loading metrics…", id="status")
        with Horizontal():
            yield OptionList(id="panels")
            yield VerticalScroll(id="charts")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"Charts — {self.run_name}"
        self.interval = interval_from_pref(self.app.preferences.get(PREF_KEY))
        self.query_one("#panels", OptionList).focus()
        self.load()

    def on_unmount(self) -> None:
        self._stop_timer()

    # ---- loading -------------------------------------------------------
    def _fetch_data(self) -> ChartData:
        """Fetch everything shown (runs in a worker thread). Raises if the metrics cannot be read."""
        client, ref = self.app.client, self.ref
        metrics = client.metric_keys(ref)  # also loads the run, so what follows is cheap...
        values = _or_default(partial(client.metric_values, ref), {})
        tables = _or_default(partial(client.run_tables, ref), [])
        images = _or_default(partial(client.run_images, ref), [])
        with ThreadPoolExecutor(max_workers=2) as pool:  # ...except these two network calls: run them together
            rows_job = pool.submit(client.system_rows, ref)
            state_job = pool.submit(lambda: client.run_details(ref).state)
            note = ""
            try:
                rows = rows_job.result()
            except Exception as exc:  # the run can still be browsed without system data
                rows, note = [], f" (system metrics unavailable: {exc})"
            state = _or_default(state_job.result, "")
        return ChartData(metrics, rows, note, values, tables, state, images)

    @work(thread=True, exclusive=True, group="load")
    def load(self) -> None:
        try:
            data = self._fetch_data()
        except Exception as exc:
            self.app.call_from_thread(self._status, f"Error: {exc}")
            return
        self.app.call_from_thread(self._loaded, data)

    def _apply_data(self, data: ChartData) -> None:
        self.metrics = data.metrics
        self._metric_set = set(data.metrics)
        self.values = data.values
        self.tables = data.tables
        self.images = data.images
        self.system_rows = data.rows
        self.system_last = last_values(data.rows)
        self.system_metrics = sorted(
            {k for r in data.rows for k, v in r.items() if not k.startswith("_") and is_number(v)}
        )
        self._system_set = set(self.system_metrics)
        self.state = data.state
        self.live = data.state == "running"
        self._status_base = (
            f"{len(data.metrics)} metrics, {len(self.system_metrics)} system metrics, "
            f"{len(data.tables)} table{'s' * (len(data.tables) != 1)}, "
            f"{len(data.images)} image key{'s' * (len(data.images) != 1)}{data.note}"
        )

    async def _loaded(self, data: ChartData) -> None:
        self._apply_data(data)
        # the generated panels, plus whatever the user customized in an earlier session
        self.chart_layout = ChartLayout(
            build_panels(self.metrics, self.system_metrics),
            self.app.layout_store.load(self._layout_key),
        )
        self.ready = True
        self._last_refresh = datetime.now()
        self._apply_timer()
        self._update_status()
        self._refresh_panel_list()
        await self._show_panel()
        self.query_one("#panels", OptionList).focus()

    # ---- refreshing ----------------------------------------------------
    def action_refresh_now(self) -> None:
        self.refresh_now()

    def refresh_now(self) -> None:
        """Fetch fresh data and update what is shown, in place where nothing structural changed."""
        if not self.ready or self._refreshing:
            return
        self._refreshing = True
        self._update_status()
        self._refresh_worker()

    @work(thread=True, exclusive=True, group="refresh")
    def _refresh_worker(self) -> None:
        try:
            data = self._fetch_data()
        except Exception as exc:
            self.app.call_from_thread(self._refresh_failed, str(exc))
            return
        self.app.call_from_thread(self._refreshed, data)

    def _refresh_failed(self, message: str) -> None:
        self._refreshing = False
        self._refresh_error = message
        self._update_status()  # what is on screen stays; only the status line mentions it

    async def _refreshed(self, data: ChartData) -> None:
        self._refreshing = False
        self._refresh_error = ""
        panel = self.panels[self.selected].name if self.panels else None
        focused = self.focused
        focus_index = focused.index if isinstance(focused, ChartBase) else None
        shown_before = list(self.panels[self.selected].charts) if self.panels else []

        self._apply_data(data)
        self._cache.clear()  # old points are stale; charts fetch again
        self._table_cache.clear()
        # defaults are rebuilt from the new metrics; the user's customizations are kept as they are
        self.chart_layout = ChartLayout(build_panels(self.metrics, self.system_metrics), self.chart_layout.custom)
        self.selected = self.chart_layout.index_of(panel) if panel else 0
        self._shown_panel = self.selected  # the same panel stays shown: no remount just for a new number
        self._last_refresh = datetime.now()
        self._apply_timer()  # stops itself once the run has finished
        self._update_status()
        self._refresh_panel_list()
        if list(self.panels[self.selected].charts) == shown_before:
            for view in self.query(f".{CHART_CLASS}"):  # same charts: just update their data
                self._load_view(view, quiet=True)
        else:  # a new metric/table appeared in this panel: rebuild it
            await self._show_panel(focus=focus_index)

    # ---- the automatic timer -------------------------------------------
    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _apply_timer(self) -> None:
        """Auto-refresh runs only while the run is still running and an interval is chosen."""
        self._stop_timer()
        if self.interval and self.live:
            self._timer = self.set_interval(self.interval, self._tick)

    def _tick(self) -> None:
        if self.app.screen is self:  # not while a dialog is open on top
            self.refresh_now()

    def set_refresh_interval(self, seconds: float | None) -> None:
        self.interval = seconds or None
        self._apply_timer()
        self._update_status()

    def action_cycle_refresh(self) -> None:
        self.set_refresh_interval(next_choice(self.interval))
        self.app.preferences.set(PREF_KEY, to_pref(self.interval))
        if self.interval and not self.live and self.ready:
            self.notify(f"Auto-refresh set to {interval_label(self.interval)}; it only runs while the run is running.")
        else:
            self.notify(f"Auto-refresh: {interval_label(self.interval)}")

    def _status(self, text: str) -> None:
        self.query_one("#status", Static).update(plain(text))

    def _refresh_status(self) -> str:
        if self._refreshing:
            return "refreshing…"
        parts = []
        if self._last_refresh:
            parts.append(f"updated {self._last_refresh:%H:%M:%S}")
        if self.live:
            parts.append(f"auto-refresh {interval_label(self.interval)}" + ("" if self.interval else " (f to turn on)"))
        elif self.state:
            parts.append(f"run {self.state}")
        if self._refresh_error:
            parts.append(f"refresh failed: {self._refresh_error}")
        return "  ·  ".join(parts)

    def _update_status(self) -> None:
        custom = (
            "custom layout shared by all runs in this project (d: back to default)"
            if self.chart_layout and self.chart_layout.is_customized
            else ""
        )
        self._status("  ·  ".join(p for p in (self._status_base, custom, self._refresh_status()) if p))

    def _save_layout(self) -> None:
        """Persist the customizations right away, so nothing is lost if the app is killed."""
        if not self.app.layout_store.save(self._layout_key, self.chart_layout.custom):
            self.notify("Could not save the chart layout (it still works for this session).", severity="warning")
        elif not self._told_scope and self.chart_layout.is_customized:
            self._told_scope = True  # say it once: this is easy to miss and surprising
            self.notify(f"Chart changes apply to every run in “{self.ref.project}”.")
        self._update_status()

    # ---- rendering -----------------------------------------------------
    def _refresh_panel_list(self) -> None:
        options = self.query_one("#panels", OptionList)
        labels = [f"{p.name} ({len(p.charts)})" for p in self.panels]
        if labels != self._panel_labels:  # an unchanged list (most refreshes) is not redrawn
            self._panel_labels = labels
            options.clear_options()
            options.add_options([Option(plain(text), id=str(i)) for i, text in enumerate(labels)])
        options.highlighted = self.selected

    async def _show_panel(self, focus: int | None = None) -> None:
        async with self._render_lock:
            self._shown_panel = self.selected
            container = self.query_one("#charts", VerticalScroll)
            await container.remove_children()
            panel = self.panels[self.selected]
            if not panel.charts:
                hint = "No charts in this panel. Press 'a' to add one."
                await container.mount(Static(hint, markup=False))
                return
            views = [make_chart_widget(spec, i) for i, spec in enumerate(panel.charts)]
            await container.mount_all(views)
            for view in views:
                self._load_view(view)
            if focus is not None:
                views[min(focus, len(views) - 1)].focus()

    async def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        if event.option_list.id != "panels" or not self.ready:
            return
        if event.option_index != self._shown_panel:
            self.selected = event.option_index
            await self._show_panel()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_focus_chart(0)

    # ---- data for each kind of chart -----------------------------------
    def _load_view(self, view, quiet: bool = False) -> None:
        """Fill a chart with data. `quiet` (used when refreshing) keeps what is shown if the
        fetch fails, instead of replacing it with an error."""
        kind = view.spec.kind
        if kind == BAR:
            self._load_bars(view)
        elif kind == IMAGE:
            self._load_images(view, quiet)
        elif kind == TABLE:
            self._load_table(view, quiet)
        else:
            self._load_plot(view, quiet)

    def _in_this_run(self, name: str, system: bool) -> bool:
        return name in (self._system_set if system else self._metric_set)

    def _x_in_this_run(self, spec: ChartSpec) -> bool:
        if spec.x in (STEP, RUNTIME, WALL_TIME):
            return not (spec.system and spec.x == STEP)  # system readings have no steps
        return not spec.system and spec.x in self._metric_set  # another metric as the x-axis

    def _load_plot(self, view: ChartView, quiet: bool = False) -> None:
        """Layouts are shared by all runs of a project, so a plot may ask for metrics (or an
        x-axis) this run never logged: draw what exists, and never request what cannot exist."""
        spec = view.spec
        here = [n for n in spec.series if self._in_this_run(n, spec.system)]
        if not self._x_in_this_run(spec):
            view.show_unavailable(f"The x-axis “{axis_label(spec.x)}” was not logged in this run.")
            return
        if not here:
            view.show_unavailable("Not logged in this run: " + ", ".join(spec.series))
            return
        keys = [(spec.system, name, spec.x) for name in here]
        for key in keys:
            if key[0] and key not in self._cache:  # system series come from the rows already loaded
                self._cache[key] = extract_points(self.system_rows, spec.x, key[1])
        to_fetch = [name for (system, name, _x) in keys if (system, name, spec.x) not in self._cache]
        if to_fetch:
            self._fetch_series(view, to_fetch, quiet)
        else:
            self._show_plot(view)

    def _show_plot(self, view: ChartView) -> None:
        spec = view.spec
        here = [n for n in spec.series if (spec.system, n, spec.x) in self._cache and self._in_this_run(n, spec.system)]
        absent = tuple(n for n in spec.series if not self._in_this_run(n, spec.system))
        view.show({name: self._cache[(spec.system, name, spec.x)] for name in here}, absent)

    def _deliver(self, view, quiet: bool, fetch: Callable[[], T], ready: Callable[[T], None]) -> None:
        """Run `fetch` (call this from a worker thread) and hand its result to `ready` on the UI
        thread. If it fails while refreshing (`quiet`) the chart keeps what it shows and the status
        line reports it; otherwise the chart itself shows the error."""
        try:
            result = fetch()
        except Exception as exc:
            self.app.call_from_thread(self._refresh_failed if quiet else view.fail, str(exc))
            return
        self.app.call_from_thread(ready, result)

    def _series_batch(self, spec: ChartSpec, names: list[str]) -> dict[tuple[bool, str, str], list[Point]]:
        def one(name: str):
            return (spec.system, name, spec.x), self.app.client.metric_series(self.ref, name, spec.x)

        if len(names) == 1:
            return dict([one(names[0])])
        with ThreadPoolExecutor(max_workers=min(len(names), SERIES_WORKERS)) as pool:  # independent requests
            return dict(pool.map(one, names))

    @work(thread=True)
    def _fetch_series(self, view: ChartView, names: list[str], quiet: bool = False) -> None:
        self._deliver(view, quiet, partial(self._series_batch, view.spec, names), partial(self._series_ready, view))

    def _series_ready(self, view: ChartView, fetched: dict[tuple[bool, str, str], list[Point]]) -> None:
        self._cache.update(fetched)
        if view.is_attached:
            self._show_plot(view)

    def _load_images(self, view: ImageChartView, quiet: bool = False) -> None:
        if view.spec.y not in {i.key for i in self.images}:
            view.show_unavailable(f"This run has no logged images called “{view.spec.y}”.")
        else:
            self._fetch_steps(view, quiet)

    @work(thread=True)
    def _fetch_steps(self, view: ImageChartView, quiet: bool) -> None:
        self._deliver(
            view, quiet, partial(self.app.client.image_steps, self.ref, view.spec.y), partial(self._steps_ready, view, quiet)
        )

    def _steps_ready(self, view: ImageChartView, quiet: bool, series: ImageSeries) -> None:
        if view.is_attached:
            view.set_steps(series, keep_position=quiet)

    def on_image_chart_view_wanted(self, event: ImageChartView.Wanted) -> None:
        """A slider settled on a step: fetch that image."""
        event.stop()
        view = event.view
        step, sub = view.current_step, view.sub
        if step is not None and view.is_attached:
            self._fetch_image(view, step, sub, (step.step, sub))

    @work(thread=True, group="images")
    def _fetch_image(self, view: ImageChartView, step: ImageStep, sub: int, token: tuple[int, int]) -> None:
        path = step.paths[sub]
        size = step.size(sub)
        try:
            if size is not None and size > MAX_IMAGE_BYTES:
                raise ValueError(f"The image is too large to display ({size // 1024 // 1024} MB).")
            folder = download_dir(self.app.cache.root, self.ref, FileInfo(path, size, step.identity(sub)))
            target = folder / path
            if not (target.is_file() and (size is None or target.stat().st_size == size)):
                self.app.cache.prune_dir_sometimes(DOWNLOAD_NAMESPACE, DOWNLOAD_CACHE_BYTES)
                self.app.client.download_file(self.ref, path, folder)
            shown = make_thumbnail(target)  # a screen-sized copy: huge images are slow to decode
        except Exception as exc:
            self.app.call_from_thread(view.fail, str(exc), token)
            return
        self.app.call_from_thread(view.show_image, shown, token)

    def _load_bars(self, view: BarView) -> None:
        items = [(name, self.values.get(name, self.system_last.get(name))) for name in view.spec.series]
        if all(value is None for _, value in items):
            view.show_unavailable("None of these metrics were logged in this run.")
        else:
            view.show(items)  # metrics this run lacks show as "—" with no bar

    def _load_table(self, view: TableView, quiet: bool = False) -> None:
        info = next((t for t in self.tables if t.key == view.spec.y), None)
        if info is None:
            view.show_unavailable(f"This run has no logged table called “{view.spec.y}”.")
        elif info.path in self._table_cache:
            view.show_table(self._table_cache[info.path])
        else:
            self._fetch_table(view, info, quiet)

    @work(thread=True)
    def _fetch_table(self, view: TableView, info: TableInfo, quiet: bool = False) -> None:
        self._deliver(
            view, quiet, partial(self.app.client.table_data, self.ref, info.path), partial(self._table_ready, view, info.path)
        )

    def _table_ready(self, view: TableView, path: str, data: TableData) -> None:
        self._table_cache[path] = data
        if view.is_attached:
            view.show_table(data)

    # ---- actions -------------------------------------------------------
    def action_focus_chart(self, delta: int) -> None:
        views = list(self.query(f".{CHART_CLASS}"))
        if not views:
            return
        current = self.focused
        index = current.index + delta if isinstance(current, ChartBase) else 0
        views[max(0, min(index, len(views) - 1))].focus()

    def action_cycle_focus(self, delta: int) -> None:
        """Tab / Shift+Tab: panel list -> each chart in turn -> back to the panel list.
        (The way out of a table, where the arrow keys move inside it.)"""
        stops = [self.query_one("#panels", OptionList), *self.query(f".{CHART_CLASS}")]
        current = self.focused
        here = stops.index(current) if current in stops else 0
        stops[(here + delta) % len(stops)].focus()

    def action_focus_panels(self) -> None:
        self.query_one("#panels", OptionList).focus()

    def action_new_panel(self) -> None:
        if self.ready:
            self.app.push_screen(NewPanelDialog(), self._on_new_panel)

    async def _on_new_panel(self, name: str | None) -> None:
        if not name:
            return
        self.selected = self.chart_layout.add_panel(name)
        self._save_layout()
        self._refresh_panel_list()
        await self._show_panel()

    def action_add_chart(self) -> None:
        """Step 1: what kind of chart? Step 2 (below) asks for that kind's details."""
        if not self.ready:
            return
        values = len(self.metrics) + len(self.system_metrics)
        if not (values or self.tables or self.images):
            self.notify("This run has no metrics, tables or images to chart.", severity="warning")
            return
        self.app.push_screen(
            ChartKindDialog(
                plot_ok=values >= 1,
                bar_ok=values >= 2,
                table_ok=bool(self.tables),
                image_ok=bool(self.images),
            ),
            self._on_kind,
        )

    def _on_kind(self, kind: str | None) -> None:
        if kind == PLOT:
            self.app.push_screen(PlotDialog(self.metrics, self.system_metrics), self._on_add_chart)
        elif kind == BAR:
            self.app.push_screen(BarDialog(self.metrics, self.system_metrics), self._on_add_chart)
        elif kind == TABLE:
            self.app.push_screen(TableDialog(self.tables), self._on_add_chart)
        elif kind == IMAGE:
            self.app.push_screen(ImageDialog(self.images), self._on_add_chart)

    async def _on_add_chart(self, spec: ChartSpec | None) -> None:
        if not spec:
            return
        index = self.chart_layout.add_chart(self.selected, spec)
        self._save_layout()
        self._refresh_panel_list()
        await self._show_panel(focus=index)

    def action_change_x(self) -> None:
        view = self.focused
        if not isinstance(view, ChartBase):
            return
        if view.spec.kind != PLOT:
            self.notify("The x-axis only applies to plots.")
            return
        self.app.push_screen(
            AxisDialog(self.metrics, view.spec),
            lambda spec: self._on_change_x(view.index, spec),
        )

    async def _on_change_x(self, index: int, spec: ChartSpec | None) -> None:
        if not spec:
            return
        self.chart_layout.set_chart(self.selected, index, spec)
        self._save_layout()
        await self._show_panel(focus=index)

    def action_delete_chart(self) -> None:
        """Delete the focused chart or table from this panel (after confirming)."""
        view = self.focused
        if not self.ready:
            return
        if not isinstance(view, ChartBase):
            self.notify("Focus a chart first (→ or Tab), then press Delete.")
            return
        self.app.push_screen(
            ConfirmDialog(
                "Delete this chart?",
                f"“{view.spec.title}”\n\n"
                f"It is removed from this panel in ALL runs of “{self.ref.project}”;\n"
                "no run's data is touched.\n"
                "(Press d to bring back all the default charts.)",
                yes="Yes, delete", no="No",
            ),
            lambda yes: self._on_delete(view.index, yes),
        )

    async def _on_delete(self, index: int, yes: bool | None) -> None:
        if not yes:
            return
        self.chart_layout.remove_chart(self.selected, index)
        self._save_layout()
        self._refresh_panel_list()
        remaining = len(self.panels[self.selected].charts)
        await self._show_panel(focus=min(index, remaining - 1) if remaining else None)
        if not remaining:
            self.action_focus_panels()  # nothing left to focus in this panel

    def action_reset_layout(self) -> None:
        """Forget all panels, charts and x-axis choices made for this run (after confirming)."""
        if not self.ready:
            return
        if not self.chart_layout.is_customized:
            self.notify("This run already uses the default layout.")
            return
        self.app.push_screen(
            ConfirmDialog(
                "Back to the default layout?",
                "This removes the panels and charts you added, brings back charts\n"
                "you deleted, and undoes x-axis changes, for ALL runs in\n"
                f"“{self.ref.project}”. No run's data is touched.",
                yes="Yes, reset", no="No",
            ),
            self._on_reset,
        )

    async def _on_reset(self, yes: bool | None) -> None:
        if not yes:
            return
        self.chart_layout.reset()
        self.app.layout_store.reset(self._layout_key)
        self.selected = 0
        self._update_status()
        self._refresh_panel_list()
        await self._show_panel()
        self.notify("Charts reset to the default layout.")
