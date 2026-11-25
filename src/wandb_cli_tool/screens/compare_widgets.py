"""Charts that compare runs: every run has its own color (its slot in the comparison, shown in the
legend at the top of the screen). A plot overlays one metric's curves; a bar chart draws a group of
bars (one per run) for each metric. A plot whose runs all logged a single value is drawn as bars."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from rich.align import Align
from rich.console import RenderableType
from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import DataTable, Static
from textual_image.widget import Image as BestImage
from textual_plotext import PlotextPlot

from ..barchart import layout_bars
from ..charts import axis_label
from ..compare import MAX_COMPARE_ROWS, CompareChart, latest, merge_tables, nearest_step, union_steps
from ..images import ImageSeries, ImageStep, StepCursor
from ..palette import hex_to_rgb, series_color
from ..plain import plain, plain_title
from ..series import Point, prepare_points
from ..tables import TableData
from .chart_widgets import (
    CHART_CLASS, DEBOUNCE, NOTE_HEIGHT, StepSlider, table_height, PLOT_HEIGHT, _NAV_BINDINGS, apply_axis_options, is_dark, note_renderable, plot_note,
)

Group = tuple[str, Sequence["float | None"]]  # a metric and its value in each run (None = no value)


def render_grouped_bars(
    groups: Sequence[Group], colors: Sequence[str], width: int, low: float | None = None, high: float | None = None
) -> Text:
    """One bar per run for each group, all on one scale. Each bar wears its run's color; the group's
    name is written beside its first bar and groups are separated by a blank line."""
    items = [(name if i == 0 else "", value) for name, values in groups for i, value in enumerate(values)]
    layout = layout_bars(items, width, low, high)
    lines: list[Text] = []
    row_index = 0
    for g, (_name, values) in enumerate(groups):
        if g:
            lines.append(Text())  # a blank line between groups
        for i in range(len(values)):
            row = layout.rows[row_index]
            row_index += 1
            line = Text(no_wrap=True, overflow="crop")
            line.append(row.name + "  ")
            line.append(" " * (layout.negative_width - len(row.negative)))
            line.append(row.negative, style=colors[i])
            if layout.negative_width:
                line.append("│", style="dim")
            line.append(row.positive, style=colors[i])
            line.append(" " * (layout.positive_width - len(row.positive)))
            line.append("  " + row.figure)
            lines.append(line)
    return Text("\n").join(lines)


def grouped_bars_height(groups: Sequence[Group]) -> int:
    return sum(len(values) for _, values in groups) + max(len(groups) - 1, 0) + 2  # + border


class CompareBase:
    """What the comparison charts share: the chart description, a position, a title."""

    chart: CompareChart
    index: int

    def _init_compare(self, chart: CompareChart, index: int, run_labels: Sequence[str]) -> None:
        self.chart = chart
        self.index = index  # position among the drawn charts (focus order)
        self.spec = chart.source  # the layout chart this belongs to ...
        self.layout_index = chart.index  # ... and its position in the layout's panel
        self.run_labels = list(run_labels)
        self.add_class(CHART_CLASS)  # type: ignore[attr-defined]
        self.border_title = plain_title(chart.title)  # type: ignore[attr-defined]
        if chart.subtitle:
            self.border_subtitle = plain_title(f"in “{chart.subtitle}”")  # type: ignore[attr-defined]

    def _colors(self) -> list[str]:
        dark = is_dark(self)  # type: ignore[arg-type]
        return [series_color(slot, dark) for slot in range(len(self.run_labels))]


class CompareBarView(CompareBase, Widget):
    """Bars side by side: for each metric, one bar per run."""

    can_focus = True
    BINDINGS = _NAV_BINDINGS
    DEFAULT_CSS = """
    CompareBarView { height: 8; border: round $primary; margin-bottom: 1; padding: 0 1; }
    CompareBarView:focus { border: round $accent; }
    """

    def __init__(self, chart: CompareChart, index: int, run_labels: Sequence[str]) -> None:
        super().__init__()
        self._init_compare(chart, index, run_labels)
        self.groups: list[Group] | None = None
        self.note: str | None = None
        self.error = ""

    def show(self, groups: list[Group]) -> None:
        self.groups, self.note, self.error = groups, None, ""
        self.styles.height = grouped_bars_height(groups)
        self.border_subtitle = plain_title(f"axis {self.chart.opts.range_text()}") if self.chart.opts.range_text() else ""
        self.refresh()

    def show_unavailable(self, message: str) -> None:
        self.note, self.groups, self.error = message, None, ""
        self.styles.height = NOTE_HEIGHT
        self.refresh()

    def fail(self, message: str) -> None:
        self.error = message
        self.refresh()

    def render(self) -> RenderableType:
        if self.note is not None:
            return note_renderable(self.note)
        if self.error:
            return Text(f"Error: {self.error}", style="red")
        if self.groups is None:
            return Text("loading…", style="dim")
        opts = self.chart.opts
        return render_grouped_bars(self.groups, self._colors(), max(self.content_size.width, 20), opts.ymin, opts.ymax)


class ComparePlotView(CompareBase, PlotextPlot):
    """One metric's curves, one per run, overlaid. If every run logged a single value it becomes bars."""

    can_focus = True
    BINDINGS = _NAV_BINDINGS
    DEFAULT_CSS = """
    ComparePlotView { height: 20; border: round $primary; margin-bottom: 1; }
    ComparePlotView:focus { border: round $accent; }
    """

    def __init__(self, chart: CompareChart, index: int, run_labels: Sequence[str]) -> None:
        super().__init__()
        self._init_compare(chart, index, run_labels)
        self.border_subtitle = self.border_subtitle or "loading…"
        self.bars: list[Group] | None = None  # set when every run has a single value
        self.note: str | None = None

    def render(self) -> RenderableType:
        if self.note is not None:
            return note_renderable(self.note)
        if self.bars is not None:
            return render_grouped_bars(self.bars, self._colors(), max(self.content_size.width, 20))
        return super().render()

    def show_unavailable(self, message: str) -> None:
        self.note, self.bars = message, None
        self.styles.height = NOTE_HEIGHT
        self.border_subtitle = ""
        self.refresh()

    def show(self, series: Mapping[int, list[Point]], single: bool) -> None:
        """`series` maps a run's position to its points (runs without data are left out)."""
        chart, plt = self.chart, self.plt
        self.note = None
        plt.clear_figure()
        missing = [self.run_labels[i] for i in range(len(self.run_labels)) if not series.get(i)]
        note = f"  ·  no data: {', '.join(missing)}" if missing else ""
        if single:  # one number per run: a bar each, not a line
            self.bars = [(chart.title, [latest(series.get(i, [])) for i in range(len(self.run_labels))])]
            self.styles.height = grouped_bars_height(self.bars)
            self.border_subtitle = plain_title(note.removeprefix("  ·  "))
            self.refresh()
            return
        self.bars = None
        self.styles.height = PLOT_HEIGHT
        colors = self._colors()
        dropped, drawn = 0, []
        for i, points in sorted(series.items()):
            points, gone = prepare_points(points, chart.opts)  # smoothing / log axes, per run
            dropped += gone
            if points:
                drawn += [p[1] for p in points]
                plt.plot(
                    [p[0] for p in points], [p[1] for p in points],
                    marker="braille", color=hex_to_rgb(colors[i]), label=self.run_labels[i],
                )
        plt.xlabel(axis_label(chart.x))
        apply_axis_options(plt, chart.opts, drawn)
        self.border_subtitle = plain_title(f"x: {axis_label(chart.x)}{plot_note(chart.opts, dropped)}{note}")
        self.refresh()

    def fail(self, message: str) -> None:
        self.bars = None
        self.plt.clear_figure()
        self.plt.title(f"Error: {message}")
        self.border_subtitle = ""
        self.refresh()


# ---- tables --------------------------------------------------------------------------------------
class CompareTableView(CompareBase, DataTable):
    """The runs' versions of one wandb.Table in one table, each row marked with its run's color.
    `v` switches between keeping each run's rows together and lining up rows with the same first column."""

    can_focus = True
    BINDINGS = [Binding("v", "toggle_group", "Line up rows by first column")]
    DEFAULT_CSS = """
    CompareTableView { height: 8; border: round $primary; margin-bottom: 1; }
    CompareTableView:focus { border: round $accent; }
    """

    def __init__(self, chart: CompareChart, index: int, run_labels: Sequence[str]) -> None:
        DataTable.__init__(self, cursor_type="cell", zebra_stripes=True)
        self._init_compare(chart, index, run_labels)
        self.border_subtitle = "loading…"
        self.tables: list[TableData | None] | None = None
        self.by_key = False

    def show_tables(self, tables: list[TableData | None]) -> None:
        self.tables = tables
        self._render_table()

    def _render_table(self) -> None:
        if self.tables is None:
            return
        merged = merge_tables(self.tables, self.by_key)
        colors = self._colors()
        self.clear(columns=True)
        self.add_columns("Run", *[plain(c) for c in merged.columns])
        for run, cells in merged.rows:
            self.add_row(Text.assemble(("■ ", colors[run]), self.run_labels[run]), *[plain(c) for c in cells])
        self.styles.height = table_height(len(merged.rows))
        notes = [f"{len(merged.rows):,} rows"]
        if merged.truncated:
            notes.append(f"first {MAX_COMPARE_ROWS} of each run")
        if merged.missing:
            notes.append("not in: " + ", ".join(self.run_labels[i] for i in merged.missing))
        notes.append("lined up by first column" if self.by_key else "grouped by run")
        self.border_subtitle = plain_title(" · ".join(notes))
        self.refresh()

    def toggle_state(self, action: str) -> bool | None:
        return self.by_key if action == "toggle_group" else None

    def action_toggle_group(self) -> None:
        self.by_key = not self.by_key
        self._render_table()
        self.refresh_bindings()  # the key shows the new state

    def show_unavailable(self, message: str) -> None:
        self.tables = None
        self.clear(columns=True)
        self.add_column("Not in the compared runs")
        self.add_row(plain(message))
        self.styles.height = table_height(1)
        self.border_subtitle = ""

    def fail(self, message: str) -> None:
        self.tables = None
        self.clear(columns=True)
        self.add_column("Error")
        self.add_row(plain(message))
        self.styles.height = table_height(1)
        self.border_subtitle = ""


# ---- images --------------------------------------------------------------------------------------
IMAGE_COMPARE_HEIGHT = 26


class CompareImageView(CompareBase, Vertical):
    """The images logged under one key by every run, side by side at the same step. One slider
    moves all runs together; a run without an image at that exact step shows its nearest one."""

    can_focus = True
    BINDINGS = [
        Binding("left", "step(-1)", "Previous step"),
        Binding("right", "step(1)", "Next step"),
        Binding("shift+left", "step(-10)", "10 steps back", show=False),
        Binding("shift+right", "step(10)", "10 steps on", show=False),
        Binding("home", "jump_to(0)", "First step", show=False),
        Binding("end", "jump_to(-1)", "Last step", show=False),
        Binding("left_square_bracket", "sub(-1)", "Previous image", key_display="["),
        Binding("right_square_bracket", "sub(1)", "Next image", key_display="]"),
        Binding("up", "screen.focus_chart(-1)", "Prev chart", show=False),
        Binding("down", "screen.focus_chart(1)", "Next chart", show=False),
    ]
    DEFAULT_CSS = """
    CompareImageView { height: 26; border: round $primary; margin-bottom: 1; }
    CompareImageView:focus { border: round $accent; }
    CompareImageView #cells { height: 1fr; }
    CompareImageView .cell { width: 1fr; height: 1fr; }
    CompareImageView .cell-label { height: 1; padding: 0 1; }
    CompareImageView .cell-img { height: 1fr; align: center middle; }
    CompareImageView #img-info { height: 1; color: $text-muted; padding: 0 1; }
    """

    class Wanted(Message):
        """The images for the current step should be fetched and shown."""

        def __init__(self, view: "CompareImageView") -> None:
            super().__init__()
            self.view = view

    def __init__(self, chart: CompareChart, index: int, run_labels: Sequence[str]) -> None:
        super().__init__()
        self._init_compare(chart, index, run_labels)
        self.series: list[ImageSeries | None] = []
        self.steps: list[int] = []  # every step any run has, ascending: the slider's positions
        self.cursor = StepCursor(0)
        self.sub = 0
        self.unavailable: str | None = None
        self.errors: dict[int, str] = {}
        self._debounce = None
        self._shown: dict[int, Path] = {}

    def compose(self):
        with Horizontal(id="cells"):
            for _ in self.run_labels:
                with Vertical(classes="cell"):
                    yield Static("", classes="cell-label")
                    with Vertical(classes="cell-img"):
                        image = BestImage(None)
                        image.styles.width = "auto"
                        image.styles.height = "auto"
                        yield image
        yield StepSlider(id="slider")
        yield Static("loading…", id="img-info")

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "sub":  # only offered when some run's step has several images
            return any((s := self.step_for(r)) is not None and len(s.paths) > 1 for r in range(len(self.run_labels)))
        return True

    # ---- state -----------------------------------------------------------------------------------
    @property
    def target(self) -> int | None:
        return self.steps[self.cursor.index] if self.steps else None

    def step_for(self, run: int) -> ImageStep | None:
        """The run's image step shown at the current slider position."""
        series = self.series[run] if run < len(self.series) else None
        return nearest_step(series.steps, self.target) if series and self.target is not None else None

    def token(self) -> tuple[int, int]:
        return (self.target if self.target is not None else -1, self.sub)

    def set_series(self, series: list[ImageSeries | None], keep_position: bool = False) -> None:
        """The runs' image steps. First time the newest step is shown; on a refresh the slider stays."""
        previous = self.target
        was_end = self.cursor.at_end
        self.series, self.steps = series, union_steps(series)
        n = len(self.steps)
        if not keep_position or was_end or previous is None:
            self.cursor = StepCursor(n, n - 1)
        else:
            self.cursor = StepCursor(n, min(range(n), key=lambda i: abs(self.steps[i] - previous)))
        self._sync(immediate=True)

    def show_unavailable(self, message: str) -> None:
        self.unavailable = message
        self.query_one("#cells").display = False
        self.query_one("#slider").display = False
        self.styles.height = NOTE_HEIGHT
        self.query_one("#img-info", Static).update(plain(message))

    # ---- showing -----------------------------------------------------------------------------------
    def _labels(self) -> None:
        colors = self._colors()
        for run, cell in enumerate(self.query(".cell-label")):
            step = self.step_for(run)
            text = Text.assemble(("■ ", colors[run]), self.run_labels[run])
            if step is None:
                text.append("  not logged", style="dim")
            else:
                text.append(f"  step {step.step}", style="dim" if step.step == self.target else "yellow")
            if run in self.errors:
                text.append(f"  {self.errors[run]}", style="red")
            cell.update(text)

    def _sync(self, immediate: bool = False) -> None:
        self.query_one("#slider", StepSlider).set_position(self.cursor.index, len(self.steps))
        self.errors = {}
        self._labels()
        self.refresh_bindings()
        info = f"step {self.target} · {self.cursor.index + 1}/{len(self.steps)}" if self.steps else "No images were logged."
        self.query_one("#img-info", Static).update(plain(info))
        if self.steps:
            if self._debounce is not None:
                self._debounce.stop()
                self._debounce = None
            if immediate:
                self.post_message(self.Wanted(self))
            else:
                self._debounce = self.set_timer(DEBOUNCE, lambda: self.post_message(self.Wanted(self)))

    def show_image(self, run: int, path: Path, token: tuple[int, int]) -> None:
        if token != self.token():  # the slider has moved on since this was requested
            return
        self._shown[run] = path
        self.query(BestImage)[run].image = path

    def fail(self, run: int, message: str, token: tuple[int, int]) -> None:
        if token == self.token():
            self.errors[run] = message
            self._labels()

    # ---- keys ----------------------------------------------------------------------------------------
    def action_step(self, delta: int) -> None:
        if self.cursor.move(delta):
            self.sub = 0
            self._sync()

    def action_jump_to(self, index: int) -> None:
        if self.cursor.jump(self.cursor.n - 1 if index < 0 else index):
            self.sub = 0
            self._sync()

    def action_sub(self, delta: int) -> None:
        most = max((len(s.paths) for r in range(len(self.run_labels)) if (s := self.step_for(r))), default=1)
        if 0 <= self.sub + delta < most:
            self.sub += delta
            self._sync()

    def on_step_slider_jumped(self, event: StepSlider.Jumped) -> None:
        event.stop()
        if self.cursor.jump(event.index):
            self.sub = 0
            self._sync()
