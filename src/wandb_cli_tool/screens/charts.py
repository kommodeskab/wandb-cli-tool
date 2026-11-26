from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Callable, TypeVar

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.worker import get_current_worker
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ..api import RunRef
from ..audio import AudioInfo
from ..charts import (
    AUDIO, BAR, IMAGE, PLOT, RUNTIME, STEP, TABLE, WALL_TIME, ChartLayout, ChartSpec, Panel, axis_label, build_panels,
)
from ..images import ImageInfo, ImageSeries, ImageStep
from ..layout_store import project_key
from ..plain import plain
from ..refresh import AUTO_REFRESH_SECONDS, label as interval_label
from ..series import Point, batches, extract_points, is_number, last_values
from ..tables import TableData, TableInfo
from .chart_editing import EDIT_ACTIONS, ChartEditing
from .chart_widgets import (  # noqa: F401
    CHART_CLASS, BarView, ChartBase, ChartView, ImageChartView, TableView, make_chart_widget,
)
from .audio_widgets import AudioChartView
from .audiofetch import fetch_audio
from .imagefetch import fetch_image

if TYPE_CHECKING:
    from ..app import WandbCliApp

T = TypeVar("T")
SERIES_WORKERS = 4  # requests in flight at once (compare screen: runs)
SERIES_BATCH = 20  # metrics asked for in one request


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
    audios: list[AudioInfo] = field(default_factory=list)


class ChartsPane(ChartEditing, Vertical):
    """Panels on the left, the selected panel's charts on the right."""

    DEFAULT_CSS = """
    ChartsPane { height: 1fr; }
    ChartsPane #panels { width: 30; border: round $primary; }
    ChartsPane #panels:focus { border: round $accent; }
    ChartsPane #charts { width: 1fr; }
    ChartsPane #status { color: $text-muted; padding: 0 1; }
    """
    app: "WandbCliApp"

    def __init__(self, ref: RunRef, name: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.ref = ref
        self.run_name = name
        self.ready = False
        self._started = False
        self.metrics: list[str] = []
        self._metric_set: set[str] = set()  # what THIS run logged: shared charts may ask for more
        self._system_set: set[str] = set()
        self.system_metrics: list[str] = []
        self.system_rows: list[dict] = []
        self.values: dict[str, float] = {}  # latest value of each metric (for bar charts)
        self.system_last: dict[str, float] = {}
        self.tables: list[TableInfo] = []
        self.images: list[ImageInfo] = []
        self.audios: list[AudioInfo] = []
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
        self.interval: float | None = AUTO_REFRESH_SECONDS
        self._timer = None
        self._refreshing = False
        self._last_refresh: datetime | None = None
        self._refresh_error = ""

    @property
    def panels(self) -> list[Panel]:
        return self.chart_layout.panels if self.chart_layout else []

    @property
    def _layout_key(self) -> str:
        """Layouts belong to the project: every run in it shows the same customizations."""
        return project_key(self.ref.entity, self.ref.project)

    def compose(self) -> ComposeResult:
        yield Static("Loading metrics…", id="status")
        with Horizontal():
            yield OptionList(id="panels")
            yield VerticalScroll(id="charts")

    def activate(self) -> None:
        if not self._started:
            self._started = True
            self.load()

    def on_unmount(self) -> None:
        self._stop_timer()

    # ---- loading -------------------------------------------------------
    def _fetch_data(self) -> ChartData:
        """Fetch everything shown (runs in a worker thread). Raises if the metrics cannot be read."""
        client, ref = self.app.client, self.ref
        with ThreadPoolExecutor(max_workers=3) as pool:  # the three requests this needs go out together
            keys_job = pool.submit(client.metric_keys, ref)  # (loading the run is shared, once)
            rows_job = pool.submit(client.system_rows, ref)
            state_job = pool.submit(lambda: client.run_details(ref).state)
            metrics = keys_job.result()  # raises if the metrics cannot be read
            values = _or_default(partial(client.metric_values, ref), {})  # these read the loaded run: cheap
            tables = _or_default(partial(client.run_tables, ref), [])
            images = _or_default(partial(client.run_images, ref), [])
            audios = _or_default(partial(client.run_audios, ref), [])
            note = ""
            try:
                rows = rows_job.result()
            except Exception as exc:  # the run can still be browsed without system data
                rows, note = [], f" (system metrics unavailable: {exc})"
            state = _or_default(state_job.result, "")
        return ChartData(metrics, rows, note, values, tables, state, images, audios)

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
        self.audios = data.audios
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
            f"{len(data.images)} image key{'s' * (len(data.images) != 1)}, "
            f"{len(data.audios)} audio key{'s' * (len(data.audios) != 1)}{data.note}"
        )

    async def _loaded(self, data: ChartData) -> None:
        self._apply_data(data)
        # the generated panels, plus whatever the user customized in an earlier session
        self.chart_layout = ChartLayout(
            build_panels(
                self.metrics, self.system_metrics, [t.key for t in self.tables],
                [i.key for i in self.images], [a.key for a in self.audios],
            ),
            self.app.layout_store.load(self._layout_key),
        )
        self.ready = True
        self._last_refresh = datetime.now()
        self._apply_timer()
        self._update_status()
        self._refresh_panel_list()
        await self._show_panel()
        if self.is_attached and self.screen.active_tab == "charts":
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
        focused = self.screen.focused
        focus_index = focused.index if isinstance(focused, ChartBase) else None
        focus_spec = focused.spec if isinstance(focused, ChartBase) else None
        shown_before = list(self.panels[self.selected].charts) if self.panels else []

        self._apply_data(data)
        self._cache.clear()  # old points are stale; charts fetch again
        self._table_cache.clear()
        # defaults are rebuilt from the new metrics; the user's customizations are kept as they are
        self.chart_layout = ChartLayout(
            build_panels(
                self.metrics, self.system_metrics, [t.key for t in self.tables],
                [i.key for i in self.images], [a.key for a in self.audios],
            ),
            self.chart_layout.custom,
        )
        self.selected = self.chart_layout.index_of(panel) if panel and self.panels else 0
        self._shown_panel = self.selected  # the same panel stays shown: no remount just for a new number
        self._last_refresh = datetime.now()
        self._apply_timer()  # stops itself once the run has finished
        self._update_status()
        self._refresh_panel_list()
        new_charts = list(self.panels[self.selected].charts) if self.panels else []
        if new_charts == shown_before and self.panels:
            self._load_views(list(self.query(f".{CHART_CLASS}")), quiet=True)  # same charts: just update their data
        else:  # a new metric/table appeared in this panel: rebuild it
            target = (
                next((i for i, c in enumerate(new_charts) if c == focus_spec), focus_index)
                if focus_spec is not None
                else None
            )
            await self._show_panel(focus=target)
            if focus_spec is not None and not new_charts:
                self.action_focus_panels()

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
        if self.is_attached and self.app.screen is self.screen and self.screen.active_tab == "charts":  # not while a dialog is open on top
            self.refresh_now()

    def set_refresh_interval(self, seconds: float | None) -> None:
        """Only tests change the interval (to something short); the app always uses 30 seconds."""
        self.interval = seconds or None
        self._apply_timer()
        self._update_status()

    def _status(self, text: str) -> None:
        self.query_one("#status", Static).update(plain(text))

    def _refresh_status(self) -> str:
        if self._refreshing:
            return "refreshing…"
        parts = []
        if self._last_refresh:
            parts.append(f"updated {self._last_refresh:%H:%M:%S}")
        if self.live:
            parts.append(f"auto-refresh {interval_label(self.interval)}")
        elif self.state:
            parts.append(f"run {self.state}")
        if self._refresh_error:
            parts.append(f"refresh failed: {self._refresh_error}")
        return "  ·  ".join(parts)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Only list the keys that do something right now (the key bar shows what is left)."""
        if action in EDIT_ACTIONS:
            return self.check_chart_action(action)
        if action == "cycle_focus":
            return bool(self.ready and self.panels and self.panels[self.selected].charts)
        return True

    def _focused_chart(self) -> tuple[ChartSpec, int] | None:
        """The focused chart's spec and its position in the panel."""
        view = self.screen.focused
        return (view.spec, view.index) if isinstance(view, ChartBase) else None

    def _update_status(self) -> None:
        if self.is_attached:
            self.screen.refresh_bindings()  # the live/customized state may have changed what keys apply
        self._status("  ·  ".join(p for p in (self._status_base, self._refresh_status()) if p))

    # ---- rendering -----------------------------------------------------
    def _refresh_panel_list(self) -> None:
        options = self.query_one("#panels", OptionList)
        labels = [f"{p.name} ({len(p.charts)})" for p in self.panels]
        if labels != self._panel_labels:  # an unchanged list (most refreshes) is not redrawn
            self._panel_labels = labels
            options.clear_options()
            options.add_options([Option(plain(text), id=str(i)) for i, text in enumerate(labels)])
        if self.panels:
            self.selected = min(self.selected, len(self.panels) - 1)
            options.highlighted = self.selected

    async def _show_panel(self, focus: int | None = None) -> None:
        async with self._render_lock:
            self.app.workers.cancel_group(self, "series")  # nothing is fetched for a panel that is no longer shown
            self._shown_panel = self.selected
            container = self.query_one("#charts", VerticalScroll)
            await container.remove_children()
            if not self.panels:
                await container.mount(Static("No panels.", markup=False))
                return
            panel = self.panels[self.selected]
            if not panel.charts:
                hint = "No charts in this panel."
                await container.mount(Static(hint, markup=False))
                return
            views = [make_chart_widget(spec, i) for i, spec in enumerate(panel.charts)]
            await container.mount_all(views)
            self._load_views(views)
            if focus is not None:
                views[min(focus, len(views) - 1)].focus()

    async def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        if event.option_list.id != "panels" or not self.ready or not self.panels:
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
        elif kind == AUDIO:
            self._load_audio(view, quiet)
        elif kind == TABLE:
            self._load_table(view, quiet)
        else:
            self._load_views([view], quiet)

    def _in_this_run(self, name: str, system: bool) -> bool:
        return name in (self._system_set if system else self._metric_set)

    def _x_in_this_run(self, spec: ChartSpec) -> bool:
        if spec.x in (STEP, RUNTIME, WALL_TIME):
            return not (spec.system and spec.x == STEP)  # system readings have no steps
        return not spec.system and spec.x in self._metric_set  # another metric as the x-axis

    def _plot_needs(self, view: ChartView) -> list[str] | None:
        """What a plot still has to fetch (empty: it can be drawn now), or None when this run cannot
        draw it at all (the chart then says so). Layouts are shared by all runs of a project, so a plot
        may ask for metrics (or an x-axis) this run never logged: only what exists is ever requested."""
        spec = view.spec
        here = [n for n in spec.series if self._in_this_run(n, spec.system)]
        if not self._x_in_this_run(spec):
            view.show_unavailable(f"The x-axis “{axis_label(spec.x)}” was not logged in this run.")
            return None
        if not here:
            view.show_unavailable("Not logged in this run: " + ", ".join(spec.series))
            return None
        keys = [(spec.system, name, spec.x) for name in here]
        for key in keys:
            if key[0] and key not in self._cache:  # system series come from the rows already loaded
                self._cache[key] = extract_points(self.system_rows, spec.x, key[1])
        return [name for (system, name, _x) in keys if (system, name, spec.x) not in self._cache]

    def _load_views(self, views, quiet: bool = False) -> None:
        """Fill charts with data. All the plots of the panel that need data share one request per
        batch of metrics (not one request per metric), top chart first."""
        waiting: list[ChartView] = []
        needed: dict[str, list[str]] = {}  # x-axis -> metrics to fetch, in chart order
        for view in views:
            if view.spec.kind != PLOT:
                self._load_view(view, quiet)
                continue
            missing = self._plot_needs(view)
            if missing is None:
                continue
            if not missing:
                self._show_plot(view)
                continue
            waiting.append(view)
            for name in missing:
                if name not in needed.setdefault(view.spec.x, []):
                    needed[view.spec.x].append(name)
        if waiting:
            self._fetch_panel_series(waiting, needed, quiet)

    def _show_plot(self, view: ChartView) -> None:
        spec = view.spec
        here = [n for n in spec.series if (spec.system, n, spec.x) in self._cache and self._in_this_run(n, spec.system)]
        absent = tuple(n for n in spec.series if not self._in_this_run(n, spec.system))
        view.show({name: self._cache[(spec.system, name, spec.x)] for name in here}, absent)

    def _plot_complete(self, view: ChartView) -> bool:
        spec = view.spec
        return all((spec.system, n, spec.x) in self._cache for n in spec.series if self._in_this_run(n, spec.system))

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

    @work(thread=True, group="series", exclusive=True)
    def _fetch_panel_series(self, views: list[ChartView], needed: dict[str, list[str]], quiet: bool) -> None:
        """Fetch what the shown plots need, a batch at a time, and draw each plot as soon as all of its
        series are in. `exclusive`: showing another panel cancels this (batches not yet asked for are
        skipped), so the panel being looked at is never queued behind one that was left."""
        worker = get_current_worker()
        stop = threading.Event()  # set on the first failure, so batches not yet started are skipped
        tasks = [(x, chunk) for x, names in needed.items() for chunk in batches(names, SERIES_BATCH)]

        def one(x: str, chunk: list[str]):
            if worker.is_cancelled or stop.is_set():
                return None
            return self.app.client.metric_series_many(self.ref, chunk, x)

        with ThreadPoolExecutor(max_workers=min(len(tasks), SERIES_WORKERS)) as pool:  # batches go out together
            futures = [pool.submit(one, x, chunk) for x, chunk in tasks]
            for (x, chunk), future in zip(tasks, futures):  # drawn in chart order, as each one is in
                try:
                    fetched = future.result()
                except Exception as exc:
                    stop.set()
                    self.app.call_from_thread(self._series_failed, views, str(exc), quiet)
                    return
                if fetched is None or worker.is_cancelled:
                    return
                self.app.call_from_thread(self._series_ready, views, {(False, n, x): fetched.get(n, []) for n in chunk})

    def _series_ready(self, views: list[ChartView], fetched: dict[tuple[bool, str, str], list[Point]]) -> None:
        self._cache.update(fetched)
        for view in views:
            if view.is_attached and self._plot_complete(view) and any(
                (view.spec.system, n, view.spec.x) in fetched for n in view.spec.series
            ):
                self._show_plot(view)

    def _series_failed(self, views: list[ChartView], message: str, quiet: bool) -> None:
        if quiet:
            self._refresh_failed(message)
            return
        for view in views:
            if view.is_attached and not self._plot_complete(view):
                view.fail(message)

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
        try:
            thumb, original = fetch_image(self.app, self.ref, step, sub)
        except Exception as exc:
            self.app.call_from_thread(view.fail, str(exc), token)
            return
        self.app.call_from_thread(view.show_image, thumb, token, original)

    def _load_audio(self, view: AudioChartView, quiet: bool = False) -> None:
        if view.spec.y not in {a.key for a in self.audios}:
            view.show_unavailable(f"This run has no logged audio called “{view.spec.y}”.")
        else:
            self._fetch_audio_steps(view, quiet)

    @work(thread=True)
    def _fetch_audio_steps(self, view: AudioChartView, quiet: bool) -> None:
        self._deliver(
            view, quiet, partial(self.app.client.audio_steps, self.ref, view.spec.y), partial(self._audio_steps_ready, view, quiet)
        )

    def _audio_steps_ready(self, view: AudioChartView, quiet: bool, series: ImageSeries) -> None:
        if view.is_attached:
            view.set_steps(series, keep_position=quiet)

    def on_audio_chart_view_wanted(self, event: AudioChartView.Wanted) -> None:
        """A slider settled on a step: fetch that clip and read its waveform."""
        event.stop()
        view = event.view
        step, sub = view.current_step, view.sub
        if step is not None and view.is_attached:
            self._fetch_clip(view, step, sub, (step.step, sub))

    @work(thread=True, group="audio")
    def _fetch_clip(self, view: AudioChartView, step: ImageStep, sub: int, token: tuple[int, int]) -> None:
        try:
            path, waveform, note = fetch_audio(self.app, self.ref, step, sub)
        except Exception as exc:
            self.app.call_from_thread(view.fail, str(exc), token)
            return
        self.app.call_from_thread(view.show_clip, path, waveform, token, note)

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
        current = self.screen.focused
        index = current.index + delta if isinstance(current, ChartBase) else 0
        views[max(0, min(index, len(views) - 1))].focus()

    def action_cycle_focus(self, delta: int) -> None:
        """Tab / Shift+Tab: panel list -> each chart in turn -> back to the panel list.
        (The way out of a table, where the arrow keys move inside it.)"""
        stops = [self.query_one("#panels", OptionList), *self.query(f".{CHART_CLASS}")]
        current = self.screen.focused
        here = stops.index(current) if current in stops else 0
        stops[(here + delta) % len(stops)].focus()

    def action_focus_panels(self) -> None:
        self.query_one("#panels", OptionList).focus()
