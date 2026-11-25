"""The widgets a chart can be: a line plot, a bar chart, or a table."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from rich.align import Align
from rich.console import RenderableType, RenderResult
from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Click
from textual.message import Message
from textual.widget import Widget
from textual.widgets import DataTable, Static
from textual_image.widget import Image as BestImage
from textual_plotext import PlotextPlot

from ..barchart import layout_bars
from ..charts import ChartSpec, axis_label, display_names
from ..images import ImageSeries, StepCursor, index_at, slider_geometry
from ..palette import hex_to_rgb, series_color
from ..plain import plain, plain_title
from ..series import Point
from ..tables import TableData

PLOT_HEIGHT = 20
NOTE_HEIGHT = 5  # a chart with nothing to show in this run collapses to a short note
SINGLE_HEIGHT = 7  # one number instead of a plot
TABLE_MAX_HEIGHT = 20  # taller tables scroll inside this
TABLE_MIN_HEIGHT = 4  # border + header + one line


def table_height(rows: int) -> int:
    """Height of a table widget that fits `rows` rows: border (2) + header (1) + the rows."""
    return max(TABLE_MIN_HEIGHT, min(rows + 3, TABLE_MAX_HEIGHT))
CHART_CLASS = "chart-widget"


def note_renderable(note: str) -> RenderableType:
    """The dim, centred message a chart shows when this run has nothing for it."""
    return Align.center(Text(note, style="dim"), vertical="middle")


def is_dark(widget: Widget) -> bool:
    """Whether the app theme is dark (series colors are stepped separately for dark/light)."""
    try:
        return bool(widget.app.current_theme.dark)
    except Exception:
        return True


class ChartBase:
    """Marks a chart widget and gives them a common shape: `spec`, `index`, `fail()`."""

    spec: ChartSpec
    index: int

    def _init_chart(self, spec: ChartSpec, index: int) -> None:
        self.spec = spec
        self.index = index
        self.add_class(CHART_CLASS)  # type: ignore[attr-defined]
        self.border_title = plain_title(spec.title)  # type: ignore[attr-defined]


_NAV_BINDINGS = [
    Binding("up", "screen.focus_chart(-1)", "Prev chart", show=False),
    Binding("down", "screen.focus_chart(1)", "Next chart", show=False),
    Binding("left", "screen.focus_panels", "Panels", show=False),
]


class ChartView(ChartBase, PlotextPlot):
    """One or more series against an x-axis. One series with one point is shown as a number."""

    can_focus = True
    BINDINGS = _NAV_BINDINGS
    DEFAULT_CSS = """
    ChartView { height: 20; border: round $primary; margin-bottom: 1; }
    ChartView:focus { border: round $accent; }
    """

    def __init__(self, spec: ChartSpec, index: int) -> None:
        super().__init__()
        self._init_chart(spec, index)
        self.border_subtitle = "loading…"
        self.single: Point | None = None  # one data point: shown as a number
        self.note: str | None = None  # nothing to plot in this run: say why instead

    def render(self) -> RenderableType:
        if self.note is not None:
            return note_renderable(self.note)
        if self.single is not None:
            x, y = self.single
            body = Text(justify="center")
            body.append(f"{y:.6g}\n", style="bold")
            body.append(f"{axis_label(self.spec.x)} = {x:.6g}", style="dim")
            return Align.center(body, vertical="middle")
        return super().render()

    def show_unavailable(self, message: str) -> None:
        """This run has none of what the chart needs (e.g. its metrics were never logged here)."""
        self.note, self.single = message, None
        self.styles.height = NOTE_HEIGHT
        self.border_subtitle = ""
        self.refresh()

    def show(self, series: Mapping[str, list[Point]], absent: tuple[str, ...] = ()) -> None:
        """`series` maps each series name of the spec to its points. `absent` names are series
        this run never logged (they keep their color slot but are not drawn)."""
        plt, spec = self.plt, self.spec
        names = list(spec.series)
        self.note = None
        plt.clear_figure()
        first = series.get(names[0], []) if names else []
        self.single = first[0] if len(names) == 1 and len(first) == 1 else None
        self.styles.height = SINGLE_HEIGHT if self.single else PLOT_HEIGHT
        if self.single:
            self.border_subtitle = ""
            self.refresh()
            return
        drawn = [n for n in names if series.get(n)]
        if not drawn:
            plt.title(f"No data for x = {axis_label(spec.x)}")
            self.border_subtitle = ""
        else:
            dark = is_dark(self)
            labels = display_names(names, spec.system)
            for slot, (name, label) in enumerate(zip(names, labels)):
                points = series.get(name)
                if not points:
                    continue
                options = {"marker": "braille", "color": hex_to_rgb(series_color(slot, dark))}
                if len(names) > 1:  # a legend whenever there are two or more series
                    options["label"] = label
                plt.plot([p[0] for p in points], [p[1] for p in points], **options)
            plt.xlabel(axis_label(spec.x))
            no_data = [lab for n, lab in zip(names, labels) if n not in absent and not series.get(n)]
            not_here = [lab for n, lab in zip(names, labels) if n in absent]
            note = (f"  ·  no data: {', '.join(no_data)}" if no_data else "") + (
                f"  ·  not logged in this run: {', '.join(not_here)}" if not_here else ""
            )
            self.border_subtitle = plain_title(f"x: {axis_label(spec.x)}{note}")
        self.refresh()

    def fail(self, message: str) -> None:
        self.single = None
        self.plt.clear_figure()
        self.plt.title(f"Error: {message}")
        self.border_subtitle = ""
        self.refresh()


class BarView(ChartBase, Widget):
    """The latest value of each chosen metric as horizontal bars, in the chosen order."""

    can_focus = True
    BINDINGS = _NAV_BINDINGS
    DEFAULT_CSS = """
    BarView { height: 8; border: round $primary; margin-bottom: 1; padding: 0 1; }
    BarView:focus { border: round $accent; }
    """

    def __init__(self, spec: ChartSpec, index: int) -> None:
        super().__init__()
        self._init_chart(spec, index)
        self.items: list[tuple[str, float | None]] | None = None  # None until loaded
        self.error = ""
        self.note: str | None = None
        self.styles.height = min(len(spec.series) + 2, 42)

    def show(self, values: list[tuple[str, float | None]]) -> None:
        self.items, self.error, self.note = values, "", None
        self.styles.height = min(len(self.spec.series) + 2, 42)
        self.refresh()

    def show_unavailable(self, message: str) -> None:
        self.note, self.error, self.items = message, "", None
        self.styles.height = NOTE_HEIGHT
        self.refresh()

    def fail(self, message: str) -> None:
        self.error = message
        self.refresh()

    def render(self) -> RenderResult:
        if self.note is not None:
            return note_renderable(self.note)
        if self.error:
            return Text(f"Error: {self.error}", style="red")
        if self.items is None:
            return Text("loading…", style="dim")
        if len(self.items) == 1:  # a one-bar bar chart is just a number
            name, value = self.items[0]
            body = Text(justify="center")
            body.append(f"{'—' if value is None else f'{value:.6g}'}\n", style="bold")
            body.append(name, style="dim")
            return Align.center(body, vertical="middle")
        width = max(self.content_size.width, 20)
        layout = layout_bars(self.items, width)
        color = series_color(0, is_dark(self))  # one series of bars: one hue, slot 1
        out = Text(no_wrap=True, overflow="crop")
        for i, row in enumerate(layout.rows):
            if i:
                out.append("\n")
            out.append(row.name + "  ")  # names, like figures, wear the text color
            out.append(" " * (layout.negative_width - len(row.negative)))
            out.append(row.negative, style=color)
            if layout.negative_width:
                out.append("│", style="dim")
            out.append(row.positive, style=color)
            out.append(" " * (layout.positive_width - len(row.positive)))
            out.append("  " + row.figure)
        return out


class TableView(ChartBase, DataTable):
    """A wandb.Table. Arrow keys move around it; Tab goes to the next chart."""

    DEFAULT_CSS = """
    TableView { height: 6; border: round $primary; margin-bottom: 1; }
    TableView:focus { border: round $accent; }
    """

    def __init__(self, spec: ChartSpec, index: int) -> None:
        DataTable.__init__(self, cursor_type="cell", zebra_stripes=True)
        self._init_chart(spec, index)
        self.border_subtitle = "loading…"
        self._scrollbar_counted = False

    def _fit_height(self, rows: int) -> None:
        """Size to the content: no empty space for small tables, scrolling for big ones."""
        self.styles.height = table_height(rows)
        self._scrollbar_counted = False
        self.call_after_refresh(self._account_for_scrollbar)

    def _account_for_scrollbar(self) -> None:
        """A horizontal scrollbar (very wide tables) takes one line from the rows."""
        if self.show_horizontal_scrollbar and not self._scrollbar_counted:
            self._scrollbar_counted = True
            current = self.styles.height.value if self.styles.height else TABLE_MIN_HEIGHT
            self.styles.height = min(int(current) + 1, TABLE_MAX_HEIGHT)

    def show_table(self, data: TableData) -> None:
        cursor = self.cursor_coordinate if self.row_count else None  # kept across a refresh
        self.clear(columns=True)
        if not data.columns:
            self.border_subtitle = "empty table"
            self._fit_height(0)
            return
        self.add_columns(*[plain(c) for c in data.columns])  # cells are literal text, never markup
        self.add_rows([[plain(c) for c in row] for row in data.rows])
        self._fit_height(len(data.rows))
        if cursor is not None and data.rows:
            self.move_cursor(
                row=min(cursor.row, len(data.rows) - 1),
                column=min(cursor.column, len(data.columns) - 1),
                animate=False,
            )
        note = f"{len(data.rows):,} of {data.total_rows:,} rows" if data.total_rows > len(data.rows) else f"{data.total_rows:,} rows"
        if data.total_cols > len(data.columns):
            note += f" · first {len(data.columns)} of {data.total_cols} columns"
        self.border_subtitle = note

    def fail(self, message: str, heading: str = "Error") -> None:
        self.clear(columns=True)
        self.add_column(plain(heading))
        self.add_row(plain(message))
        self.border_subtitle = ""
        self._fit_height(1)

    def show_unavailable(self, message: str) -> None:
        """The table is not in this run (not an error)."""
        self.fail(message, heading="Not in this run")


IMAGE_CHART_HEIGHT = 26
DEBOUNCE = 0.12  # seconds to wait after the last slider move before fetching the image


class StepSlider(Widget):
    """A one-line slider:  ◀ ━━━━●━━━━ ▶   Clicking the bar jumps to that step."""

    DEFAULT_CSS = "StepSlider { height: 1; }"
    EDGE = 2  # cells taken by "◀ " in front of the bar; "▶" follows it

    class Jumped(Message):
        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.index = 0
        self.n = 0

    def set_position(self, index: int, n: int) -> None:
        self.index, self.n = index, n
        self.refresh()

    @property
    def bar_width(self) -> int:
        return max(self.size.width - 2 * self.EDGE, 2)

    def render(self) -> RenderResult:
        width = self.bar_width
        knob, _ = slider_geometry(width, self.index, self.n)
        out = Text(no_wrap=True, overflow="crop")
        out.append("◀ ", style="dim")
        if self.n:
            out.append("━" * knob, style="dim")
            out.append("●", style=series_color(0, is_dark(self)))  # one slider, one hue
            out.append("━" * (width - knob - 1), style="dim")
        else:
            out.append("━" * width, style="dim")
        out.append(" ▶", style="dim")
        return out

    def on_click(self, event: Click) -> None:
        event.stop()
        x = event.x - self.EDGE
        if self.n and 0 <= x < self.bar_width:
            self.post_message(self.Jumped(index_at(x, self.bar_width, self.n)))


class ImageChartView(ChartBase, Vertical):
    """Images logged under one key, browsed with a step slider (newest step first)."""

    can_focus = True
    BINDINGS = [
        Binding("left", "step(-1)", "Previous step"),
        Binding("right", "step(1)", "Next step"),
        Binding("shift+left", "step(-10)", "10 steps back", show=False),
        Binding("shift+right", "step(10)", "10 steps on", show=False),
        Binding("home", "jump_to(0)", "First step", show=False),
        Binding("end", "jump_to(-1)", "Last step", show=False),
        Binding("left_square_bracket", "sub(-1)", "Previous image", show=False),
        Binding("right_square_bracket", "sub(1)", "Next image", show=False),
        Binding("up", "screen.focus_chart(-1)", "Prev chart", show=False),
        Binding("down", "screen.focus_chart(1)", "Next chart", show=False),
    ]
    DEFAULT_CSS = """
    ImageChartView { height: 26; border: round $primary; margin-bottom: 1; }
    ImageChartView:focus { border: round $accent; }
    ImageChartView #img-area { height: 1fr; align: center middle; }
    ImageChartView #img-info { height: 1; color: $text-muted; padding: 0 1; }
    """

    class Wanted(Message):
        """The image for the current step should be fetched and shown."""

        def __init__(self, view: "ImageChartView") -> None:
            super().__init__()
            self.view = view

    def __init__(self, spec: ChartSpec, index: int) -> None:
        super().__init__()
        self._init_chart(spec, index)
        self.series: ImageSeries | None = None
        self.cursor = StepCursor(0)
        self.sub = 0  # which image, when a step logged several
        self.path: Path | None = None  # the file now on screen
        self.waiting = True  # not "loading": Textual's Widget.loading shows a spinner and blocks focus
        self.error = ""
        self._wanted: tuple[int, int] | None = None
        self._shown: tuple[int, int] | None = None
        self._debounce = None
        self.unavailable: str | None = None  # this run never logged this key

    def compose(self):
        with Vertical(id="img-area"):
            image = BestImage(None, id="img")
            image.styles.width = "auto"
            image.styles.height = "auto"
            yield image
        yield StepSlider(id="slider")
        yield Static("loading…", id="img-info")

    # ---- state ---------------------------------------------------------------------------
    @property
    def steps(self):
        return self.series.steps if self.series else ()

    @property
    def current_step(self):
        return self.steps[self.cursor.index] if self.steps else None

    def show_unavailable(self, message: str) -> None:
        """This run never logged this key: collapse to a short note."""
        self.unavailable = message
        self.query_one("#img-area").display = False
        self.query_one("#slider").display = False
        self.styles.height = NOTE_HEIGHT
        self.border_subtitle = ""
        self.query_one("#img-info", Static).update(plain(message))

    def _leave_unavailable(self) -> None:
        if self.unavailable is not None:  # the key appeared (e.g. on refresh): back to full size
            self.unavailable = None
            self.query_one("#img-area").display = True
            self.query_one("#slider").display = True
            self.styles.height = IMAGE_CHART_HEIGHT

    def set_steps(self, series: ImageSeries, keep_position: bool = False) -> None:
        """New list of steps. First time: show the newest. When refreshing: stay on the same
        step, unless the slider was at the end, in which case follow the newest."""
        self._leave_unavailable()
        previous = self.current_step.step if self.current_step else None
        was_end = self.cursor.at_end
        self.series = series
        n = len(series.steps)
        if not keep_position or was_end or previous is None:
            self.cursor = StepCursor(n, n - 1)
            if not keep_position:
                self.sub = 0
        else:
            nearest = min(range(n), key=lambda i: abs(series.steps[i].step - previous)) if n else 0
            self.cursor = StepCursor(n, nearest)
        step = self.current_step
        self.sub = min(self.sub, len(step.paths) - 1) if step else 0
        self.error = ""
        self._sync(immediate=True)

    def _key(self) -> tuple[int, int] | None:
        step = self.current_step
        return (step.step, self.sub) if step else None

    def _sync(self, immediate: bool = False) -> None:
        """Update the slider and text, and ask for the image if the one shown is not the right one."""
        self.query_one("#slider", StepSlider).set_position(self.cursor.index, len(self.steps))
        key = self._key()
        if key is not None and key == self._shown and self.path is not None:
            self.waiting = False  # already showing exactly this
        elif key is not None:
            self.waiting = True
            self._wanted = key
            self._request(immediate)
        self._update_info()

    def _request(self, immediate: bool) -> None:
        if self._debounce is not None:
            self._debounce.stop()
            self._debounce = None
        if immediate:
            self.post_message(self.Wanted(self))
        else:
            self._debounce = self.set_timer(DEBOUNCE, lambda: self.post_message(self.Wanted(self)))

    def _update_info(self) -> None:
        step = self.current_step
        if self.series is None:
            text = "loading…"
        elif step is None:
            text = "No images were logged under this key."
        elif self.error:
            text = f"step {step.step} · {self.cursor.index + 1}/{len(self.steps)} · Error: {self.error}"
        else:
            parts = [f"step {step.step}", f"{self.cursor.index + 1}/{len(self.steps)}"]
            if len(step.paths) > 1:
                parts.append(f"image {self.sub + 1}/{len(step.paths)}")
            if self.waiting:
                parts.append("loading…")
            if step.caption(self.sub):
                parts.append(step.caption(self.sub))
            if self.series and self.series.sampled:
                parts.append(f"sampled: {len(self.steps):,} of {self.series.total:,} steps")
            text = " · ".join(parts)
        self.query_one("#img-info", Static).update(plain(text))
        multi = step is not None and len(step.paths) > 1
        self.border_subtitle = plain_title("←/→ step · Shift ±10 · Home/End" + (" · [ ] image" if multi else ""))

    # ---- results from the screen -----------------------------------------------------------
    def show_image(self, path: Path, key: tuple[int, int]) -> None:
        if key != self._wanted:  # the slider has moved on since this was requested
            return
        self.path, self._shown, self.waiting, self.error = path, key, False, ""
        self.query_one("#img", BestImage).image = path
        self._update_info()

    def fail(self, message: str, key: tuple[int, int] | None = None) -> None:
        if key is not None and key != self._wanted:
            return
        self.error, self.waiting = message, False
        if self.series is None:  # the steps themselves could not be read
            self.query_one("#img-info", Static).update(plain(f"Error: {message}"))
        else:
            self._update_info()

    # ---- keys -------------------------------------------------------------------------------
    def action_step(self, delta: int) -> None:
        if self.cursor.move(delta):
            self.sub = 0
            self._sync()

    def action_jump_to(self, index: int) -> None:
        if self.cursor.jump(self.cursor.n - 1 if index < 0 else index):
            self.sub = 0
            self._sync()

    def action_sub(self, delta: int) -> None:
        step = self.current_step
        if step is not None and 0 <= self.sub + delta < len(step.paths):
            self.sub += delta
            self._sync()

    def on_step_slider_jumped(self, event: StepSlider.Jumped) -> None:
        event.stop()
        if self.cursor.jump(event.index):
            self.sub = 0
            self._sync()


def make_chart_widget(spec: ChartSpec, index: int) -> Widget:
    from ..charts import BAR, IMAGE, TABLE

    return {BAR: BarView, TABLE: TableView, IMAGE: ImageChartView}.get(spec.kind, ChartView)(spec, index)
