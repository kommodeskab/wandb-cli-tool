"""Charts that compare runs: every run has its own color (its slot in the comparison, shown in the
legend at the top of the screen). A plot overlays one metric's curves; a bar chart draws a group of
bars (one per run) for each metric. A plot whose runs all logged a single value is drawn as bars."""

from __future__ import annotations

from typing import Mapping, Sequence

from rich.align import Align
from rich.console import RenderableType
from rich.text import Text
from textual.widget import Widget
from textual_plotext import PlotextPlot

from ..barchart import layout_bars
from ..charts import axis_label
from ..compare import CompareChart, latest
from ..palette import hex_to_rgb, series_color
from ..plain import plain_title
from ..series import Point
from .chart_widgets import (
    CHART_CLASS, NOTE_HEIGHT, PLOT_HEIGHT, _NAV_BINDINGS, is_dark, note_renderable,
)

Group = tuple[str, Sequence["float | None"]]  # a metric and its value in each run (None = no value)


def render_grouped_bars(groups: Sequence[Group], colors: Sequence[str], width: int) -> Text:
    """One bar per run for each group, all on one scale. Each bar wears its run's color; the group's
    name is written beside its first bar and groups are separated by a blank line."""
    items = [(name if i == 0 else "", value) for name, values in groups for i, value in enumerate(values)]
    layout = layout_bars(items, width)
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
        self.index = index
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
        return render_grouped_bars(self.groups, self._colors(), max(self.content_size.width, 20))


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
        for i, points in sorted(series.items()):
            if points:
                plt.plot(
                    [p[0] for p in points], [p[1] for p in points],
                    marker="braille", color=hex_to_rgb(colors[i]), label=self.run_labels[i],
                )
        plt.xlabel(axis_label(chart.x))
        self.border_subtitle = plain_title(f"x: {axis_label(chart.x)}{note}")
        self.refresh()

    def fail(self, message: str) -> None:
        self.bars = None
        self.plt.clear_figure()
        self.plt.title(f"Error: {message}")
        self.border_subtitle = ""
        self.refresh()
