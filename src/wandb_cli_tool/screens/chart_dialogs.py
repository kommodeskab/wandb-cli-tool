"""Dialogs for adding and editing charts: pick a kind, then its details."""

from __future__ import annotations

from dataclasses import replace

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Input, Label, OptionList, Select, SelectionList, Static
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from ..charts import BAR, IMAGE, MAX_BAR_VALUES, PLOT, TABLE, ChartSpec, x_options
from ..images import ImageInfo
from ..palette import MAX_SERIES
from ..plain import plain
from ..tables import TableInfo
from .dialogs import CancellableDialog

Value = tuple[bool, str]  # (is a system metric, name)

def _x_choices(metrics: list[str], system: bool) -> list[tuple[object, str]]:
    """x-axis choices for a Select; labels may contain metric names, so they are literal text."""
    return [(plain(label), value) for label, value in x_options(metrics, system)]


def metric_options(metrics: list[str], system_metrics: list[str]) -> list[tuple[str, Value]]:
    return [(m, (False, m)) for m in metrics] + [(f"{m}  [system]", (True, m)) for m in system_metrics]


class ChartKindDialog(CancellableDialog):
    """First step of adding a chart: what kind? Enter picks, Esc cancels."""

    DEFAULT_CSS = """
    ChartKindDialog > Vertical { width: 84; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    ChartKindDialog OptionList { height: auto; margin-top: 1; }
    """

    def __init__(self, plot_ok: bool, bar_ok: bool, table_ok: bool, image_ok: bool = False) -> None:
        super().__init__()
        self.available = {PLOT: plot_ok, BAR: bar_ok, TABLE: table_ok, IMAGE: image_ok}

    def compose(self) -> ComposeResult:
        def option(kind: str, text: str, why_not: str) -> Option:
            ok = self.available[kind]
            return Option(text if ok else f"{text}  (not available: {why_not})", id=kind, disabled=not ok)

        with Vertical():
            yield Label("What kind of chart? (↑/↓, Enter to choose, Esc to cancel)")
            yield OptionList(
                option(PLOT, "Plot — one or more values against an x-axis", "this run has no metrics"),
                option(BAR, "Bar chart — the latest value of several metrics, in the order you choose", "needs two or more metrics"),
                option(TABLE, "Table — a wandb.Table logged by this run", "this run logged no tables"),
                option(IMAGE, "Images — wandb.Images logged by this run, with a step slider", "this run logged no images"),
                id="kinds",
            )

    def on_mount(self) -> None:
        options = self.query_one("#kinds", OptionList)
        options.focus()
        options.highlighted = next((i for i, k in enumerate((PLOT, BAR, TABLE, IMAGE)) if self.available[k]), 0)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(str(event.option.id))


class MetricPicker(Vertical):
    """A filterable multi-select list that remembers the ORDER things were picked in."""

    DEFAULT_CSS = """
    MetricPicker { height: auto; }
    MetricPicker SelectionList { height: 6; }
    """

    class Changed(Message):
        """The chosen set or its order changed."""

    class LimitReached(Message):
        """The user tried to pick more than `limit`."""

    def __init__(self, options: list[tuple[str, Value]], limit: int | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.options = options
        self.limit = limit
        self.chosen: list[Value] = []  # pick order

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Filter metrics… (Tab: go to the list, Space: tick)", id="filter")
        yield SelectionList[Value](id="choices")

    def on_mount(self) -> None:
        self._fill()

    def _fill(self) -> None:
        needle = self.query_one("#filter", Input).value.strip().lower()
        shown = [(label, value) for label, value in self.options if needle in label.lower()]
        choices = self.query_one("#choices", SelectionList)
        choices.clear_options()
        choices.add_options([Selection(plain(label), value, value in self.chosen) for label, value in shown])

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._fill()

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        """Keep `chosen` in step with the list: new ticks are appended (so it records the order
        they were picked in), unticked ones removed. Values hidden by the filter are left alone."""
        event.stop()
        choices = self.query_one("#choices", SelectionList)
        visible = {choices.get_option_at_index(i).value for i in range(choices.option_count)}
        selected = list(choices.selected)
        changed = False
        for value in [v for v in self.chosen if v in visible and v not in selected]:
            self.chosen.remove(value)
            changed = True
        for value in selected:
            if value in self.chosen:
                continue
            if self.limit is not None and len(self.chosen) >= self.limit:
                choices.deselect(value)
                self.post_message(self.LimitReached())
            else:
                self.chosen.append(value)
                changed = True
        if changed:
            self.post_message(self.Changed())

    def move(self, index: int, delta: int) -> int:
        """Move the chosen item at `index` up/down in the order; returns its new index."""
        new = max(0, min(len(self.chosen) - 1, index + delta))
        self.chosen.insert(new, self.chosen.pop(index))
        self.post_message(self.Changed())
        return new


class PlotDialog(CancellableDialog):
    """Add a plot: pick one or more metrics (up to 8) and the x-axis they share."""

    DEFAULT_CSS = """
    PlotDialog > VerticalScroll { width: 80; height: auto; max-height: 100%; border: round $accent; padding: 1 2; background: $surface; }
    #plot-error { color: $error; }
    """

    def __init__(self, metrics: list[str], system_metrics: list[str]) -> None:
        super().__init__()
        self.metrics = metrics
        self.system_metrics = system_metrics
        self._system_x = False

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label(f"Add a plot: tick one or more values (at most {MAX_SERIES})", markup=False)
            yield MetricPicker(metric_options(self.metrics, self.system_metrics), limit=MAX_SERIES, id="picker")
            yield Label("X-axis (shared by all the values):")
            yield Select(_x_choices(self.metrics, False), allow_blank=False, id="x")
            yield Static("", id="plot-error")
            with Horizontal():
                yield Button("Add plot", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#filter", Input).focus()

    def _error(self, text: str) -> None:
        self.query_one("#plot-error", Static).update(plain(text))

    def on_metric_picker_limit_reached(self, event: MetricPicker.LimitReached) -> None:
        self._error(f"A plot can show at most {MAX_SERIES} values.")

    def on_metric_picker_changed(self, event: MetricPicker.Changed) -> None:
        self._error("")
        chosen = self.query_one("#picker", MetricPicker).chosen
        system = any(is_system for is_system, _ in chosen)
        if system != self._system_x:  # system metrics only have time-based x-axes
            self._system_x = system
            x = self.query_one("#x", Select)
            previous = x.value
            options = x_options(self.metrics, system)
            x.set_options([(plain(label), value) for label, value in options])
            if previous in [v for _, v in options]:
                x.value = previous

    def _submit(self) -> None:
        chosen = self.query_one("#picker", MetricPicker).chosen
        if not chosen:
            self._error("Tick at least one value.")
        elif len({is_system for is_system, _ in chosen}) > 1:
            self._error("Pick either run metrics or system metrics in one plot, not both.")
        else:
            names = [name for _, name in chosen]
            x = str(self.query_one("#x", Select).value)
            self.dismiss(ChartSpec(names[0], x, chosen[0][0], PLOT, tuple(names[1:])))



class BarDialog(CancellableDialog):
    """Add a bar chart: tick the metrics, then arrange them (Ctrl+↑/↓ in the order list)."""

    BINDINGS = [
        ("ctrl+up", "move(-1)", "Move up"),
        ("ctrl+down", "move(1)", "Move down"),
    ]
    DEFAULT_CSS = """
    BarDialog > VerticalScroll { width: 80; height: auto; max-height: 100%; border: round $accent; padding: 1 2; background: $surface; }
    BarDialog OptionList { height: 6; }
    #bar-error { color: $error; }
    """

    def __init__(self, metrics: list[str], system_metrics: list[str]) -> None:
        super().__init__()
        self.options = metric_options(metrics, system_metrics)
        self.labels = {value: label for label, value in self.options}
        self._cursor: int | None = None  # where the order list's cursor should go after a move

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("Add a bar chart: tick the metrics (each bar is its latest value)")
            yield MetricPicker(self.options, limit=MAX_BAR_VALUES, id="picker")
            yield Label("Bars appear top to bottom in this order  (focus the list, then Ctrl+↑ / Ctrl+↓ to move):")
            yield OptionList(id="order")
            yield Static("", id="bar-error")
            with Horizontal():
                yield Button("Add bar chart", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#filter", Input).focus()

    def _error(self, text: str) -> None:
        self.query_one("#bar-error", Static).update(plain(text))

    def _refresh_order(self, highlight: int | None = None) -> None:
        order = self.query_one("#order", OptionList)
        chosen = self.query_one("#picker", MetricPicker).chosen
        order.clear_options()
        order.add_options([Option(plain(f"{i + 1}. {self.labels[v]}")) for i, v in enumerate(chosen)])
        if chosen:
            order.highlighted = highlight if highlight is not None else len(chosen) - 1

    def on_metric_picker_changed(self, event: MetricPicker.Changed) -> None:
        self._error("")
        cursor, self._cursor = self._cursor, None  # a move keeps the cursor on the moved item
        self._refresh_order(cursor)

    def on_metric_picker_limit_reached(self, event: MetricPicker.LimitReached) -> None:
        self._error(f"A bar chart can show at most {MAX_BAR_VALUES} values.")

    def action_move(self, delta: int) -> None:
        order = self.query_one("#order", OptionList)
        if order.highlighted is None or not order.has_focus:
            return
        self._cursor = self.query_one("#picker", MetricPicker).move(order.highlighted, delta)
        # the list is redrawn by on_metric_picker_changed, which uses self._cursor

    def _submit(self) -> None:
        chosen = self.query_one("#picker", MetricPicker).chosen
        if len(chosen) < 2:
            self._error("Pick at least two values. A single value is better shown as a number.")
        else:
            names = [name for _, name in chosen]
            self.dismiss(ChartSpec(names[0], kind=BAR, more=tuple(names[1:])))


class KeyPickerDialog(CancellableDialog):
    """Pick one key from a dropdown and add a chart of `kind` for it (tables, images)."""

    DEFAULT_CSS = """
    KeyPickerDialog > Vertical { width: 76; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """
    heading = ""
    ok_label = ""
    select_id = ""
    kind = ""

    def __init__(self, options: list[tuple[object, str]]) -> None:
        super().__init__()
        self._options = options

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.heading, markup=False)
            yield Select(self._options, allow_blank=False, id=self.select_id)
            with Horizontal():
                yield Button(self.ok_label, variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one(f"#{self.select_id}", Select).focus()

    def _submit(self) -> None:
        self.dismiss(ChartSpec(str(self.query_one(f"#{self.select_id}", Select).value), kind=self.kind))


class TableDialog(KeyPickerDialog):
    """Pick one of the wandb.Tables the run logged."""

    heading = "Add a table (the latest version of the one you pick is shown):"
    ok_label, select_id, kind = "Add table", "table", TABLE

    def __init__(self, tables: list[TableInfo]) -> None:
        super().__init__([(plain(t.label), t.key) for t in tables])


class ImageDialog(KeyPickerDialog):
    """Pick one of the keys under which the run logged images."""

    heading = "Add images (browse the steps with a slider; the newest step is shown first):"
    ok_label, select_id, kind = "Add images", "image-key", IMAGE

    def __init__(self, images: list[ImageInfo]) -> None:
        super().__init__([(plain(i.label), i.key) for i in images])


class AxisDialog(CancellableDialog):
    """Change the x-axis of a plot."""

    DEFAULT_CSS = """
    AxisDialog > Vertical { width: 76; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """

    def __init__(self, metrics: list[str], spec: ChartSpec) -> None:
        super().__init__()
        self.metrics = metrics
        self.spec = spec

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Change the x-axis of: " + ", ".join(self.spec.series), markup=False)
            yield Select(_x_choices(self.metrics, self.spec.system), allow_blank=False, id="x")
            with Horizontal():
                yield Button("OK", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        x = self.query_one("#x", Select)
        x.value = self.spec.x
        x.focus()

    def _submit(self) -> None:
        self.dismiss(replace(self.spec, x=str(self.query_one("#x", Select).value)))
