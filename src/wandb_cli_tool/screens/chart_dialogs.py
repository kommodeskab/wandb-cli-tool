"""Dialogs for adding and editing charts: pick a kind, then its details."""

from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Checkbox, Input, Label, OptionList, Select, SelectionList, Static
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from ..charts import BAR, IMAGE, MAX_BAR_VALUES, PLOT, TABLE, ChartOptions, ChartSpec, parse_options, x_options
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


def _metric_options_with(
    metrics: Sequence[str], system_metrics: Sequence[str], extra: Sequence[Value]
) -> list[tuple[str, Value]]:
    """Available metrics plus any `extra` values missing from the current run (shared charts)."""
    options = metric_options(list(metrics), list(system_metrics))
    seen = {value for _, value in options}
    for value in extra:
        if value not in seen:
            is_sys, name = value
            options.append((f"{name}  [system]" if is_sys else name, value))
            seen.add(value)
    return options


def _x_choices_with(metrics: Sequence[str], system: bool, current: str) -> list[tuple[object, str]]:
    """x-axis choices plus `current` when a shared chart uses a metric not logged in this run."""
    choices = _x_choices(list(metrics), system)
    if current not in {value for _, value in choices}:
        choices.append((plain(current), current))
    return choices


def _key_choices(items: Sequence[TableInfo] | Sequence[ImageInfo], current: str) -> list[tuple[object, str]]:
    choices = [(plain(item.label), item.key) for item in items]
    if current not in {value for _, value in choices}:
        choices.append((plain(current), current))
    return choices


def _refresh_order_list(
    order: OptionList, chosen: Sequence[Value], labels: dict[Value, str], highlight: int | None = None
) -> None:
    order.clear_options()
    order.add_options([Option(plain(f"{i + 1}. {labels[v]}")) for i, v in enumerate(chosen)])
    if chosen:
        order.highlighted = highlight if highlight is not None else len(chosen) - 1


def _move_order_item(picker: "MetricPicker", order: OptionList, delta: int) -> int | None:
    if order.highlighted is None or not order.has_focus:
        return None
    return picker.move(order.highlighted, delta)


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
            yield Label("What kind of chart?")
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
    MetricPicker SelectionList { height: 12; }
    """

    class Changed(Message):
        """The chosen set or its order changed."""

    class LimitReached(Message):
        """The user tried to pick more than `limit`."""

    def __init__(
        self,
        options: list[tuple[str, Value]],
        limit: int | None = None,
        initial: Sequence[Value] = (),
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.options = options
        self.limit = limit
        self.chosen: list[Value] = list(initial)  # pick order

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
    """Add a plot: pick one or more metrics (up to 8), arrange draw priority (Ctrl+↑/↓), and pick the x-axis."""

    BINDINGS = [
        ("ctrl+up", "move(-1)", "Move up"),
        ("ctrl+down", "move(1)", "Move down"),
    ]
    DEFAULT_CSS = """
    PlotDialog > VerticalScroll { width: 80; height: auto; max-height: 100%; border: round $accent; padding: 1 2; background: $surface; }
    PlotDialog #order { height: 10; }
    #plot-error { color: $error; }
    """

    def __init__(self, metrics: list[str], system_metrics: list[str]) -> None:
        super().__init__()
        self.metrics = metrics
        self.system_metrics = system_metrics
        self.options = metric_options(metrics, system_metrics)
        self.labels = {value: label for label, value in self.options}
        self._system_x = False
        self._cursor: int | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label(f"Add a plot (at most {MAX_SERIES} values)", markup=False)
            yield MetricPicker(self.options, limit=MAX_SERIES, id="picker")
            yield Label("Plot order (top draws on top; Ctrl+↑ / Ctrl+↓ moves):")
            yield OptionList(id="order")
            yield Label("X-axis:")
            yield Select(_x_choices(self.metrics, False), allow_blank=False, id="x")
            yield Static("", id="plot-error")
            with Horizontal():
                yield Button("Add plot", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#filter", Input).focus()

    def _error(self, text: str) -> None:
        self.query_one("#plot-error", Static).update(plain(text))

    def _refresh_order(self, highlight: int | None = None) -> None:
        _refresh_order_list(self.query_one("#order", OptionList), self.query_one("#picker", MetricPicker).chosen, self.labels, highlight)

    def on_metric_picker_limit_reached(self, event: MetricPicker.LimitReached) -> None:
        self._error(f"A plot can show at most {MAX_SERIES} values.")

    def on_metric_picker_changed(self, event: MetricPicker.Changed) -> None:
        self._error("")
        cursor, self._cursor = self._cursor, None
        self._refresh_order(cursor)
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

    def action_move(self, delta: int) -> None:
        self._cursor = _move_order_item(self.query_one("#picker", MetricPicker), self.query_one("#order", OptionList), delta)

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
    BarDialog #order { height: 10; }
    #bar-error { color: $error; }
    """

    def __init__(self, metrics: list[str], system_metrics: list[str]) -> None:
        super().__init__()
        self.options = metric_options(metrics, system_metrics)
        self.labels = {value: label for label, value in self.options}
        self._cursor: int | None = None  # where the order list's cursor should go after a move

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("Add a bar chart")
            yield MetricPicker(self.options, limit=MAX_BAR_VALUES, id="picker")
            yield Label("Bar order (Ctrl+↑ / Ctrl+↓ moves the highlighted one):")
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
        _refresh_order_list(self.query_one("#order", OptionList), self.query_one("#picker", MetricPicker).chosen, self.labels, highlight)

    def on_metric_picker_changed(self, event: MetricPicker.Changed) -> None:
        self._error("")
        cursor, self._cursor = self._cursor, None  # a move keeps the cursor on the moved item
        self._refresh_order(cursor)

    def on_metric_picker_limit_reached(self, event: MetricPicker.LimitReached) -> None:
        self._error(f"A bar chart can show at most {MAX_BAR_VALUES} values.")

    def action_move(self, delta: int) -> None:
        self._cursor = _move_order_item(self.query_one("#picker", MetricPicker), self.query_one("#order", OptionList), delta)
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


class ChartOptionsDialog(CancellableDialog):
    """Edit how a chart is built and drawn (metrics, x-axis / order / key, and axis options)."""

    BINDINGS = [
        ("ctrl+up", "move(-1)", "Move up"),
        ("ctrl+down", "move(1)", "Move down"),
    ]
    DEFAULT_CSS = """
    ChartOptionsDialog > VerticalScroll { width: 82; height: auto; max-height: 100%; border: round $accent; padding: 1 2; background: $surface; }
    ChartOptionsDialog MetricPicker SelectionList { height: 10; }
    ChartOptionsDialog #order { height: 8; margin-bottom: 0; }
    ChartOptionsDialog #opts-row { height: auto; margin-top: 0; }
    ChartOptionsDialog .opt-col { width: 1fr; height: auto; margin-right: 1; }
    ChartOptionsDialog #log-row { height: auto; margin-bottom: 0; }
    ChartOptionsDialog #log-row Checkbox { width: 1fr; }
    ChartOptionsDialog Input { margin-bottom: 0; }
    ChartOptionsDialog #options-error { color: $error; }
    """

    def __init__(
        self,
        spec: ChartSpec,
        metrics: Sequence[str] = (),
        system_metrics: Sequence[str] = (),
        tables: Sequence[TableInfo] = (),
        images: Sequence[ImageInfo] = (),
    ) -> None:
        super().__init__()
        self.spec = spec
        self.kind = spec.kind
        self.is_bar = spec.kind == BAR
        self.metrics = list(metrics)
        self.system_metrics = list(system_metrics)
        self.tables = list(tables)
        self.images = list(images)
        self._system_x = spec.system
        self._cursor: int | None = None
        if self.kind == PLOT:
            self._initial: list[Value] = [(spec.system, m) for m in spec.series]
            self._picker_options = _metric_options_with(self.metrics, self.system_metrics, self._initial)
            self._labels = {value: label for label, value in self._picker_options}
        elif self.kind == BAR:
            metric_set, system_set = set(self.metrics), set(self.system_metrics)
            self._initial = [(m not in metric_set and m in system_set, m) for m in spec.series]
            self._picker_options = _metric_options_with(self.metrics, self.system_metrics, self._initial)
            self._labels = {value: label for label, value in self._picker_options}

    def compose(self) -> ComposeResult:
        o = self.spec.opts

        def text(v: float | None) -> str:
            return "" if v is None else f"{v:.10g}"

        heading = {
            PLOT: f"Plot options: {', '.join(self.spec.series)}",
            BAR: f"Bar chart options: {', '.join(self.spec.series)}",
            TABLE: f"Table options: {self.spec.y}",
            IMAGE: f"Image options: {self.spec.y}",
        }[self.kind]
        with VerticalScroll():
            yield Label(heading, markup=False)
            if self.kind == TABLE:
                yield Label("Table:")
                yield Select(_key_choices(self.tables, self.spec.y), value=self.spec.y, allow_blank=False, id="table")
            elif self.kind == IMAGE:
                yield Label("Image key:")
                yield Select(_key_choices(self.images, self.spec.y), value=self.spec.y, allow_blank=False, id="image-key")
            elif self.kind == PLOT:
                yield MetricPicker(self._picker_options, limit=MAX_SERIES, initial=self._initial, id="picker")
                yield Label("Plot order (top draws on top; Ctrl+↑ / Ctrl+↓ moves):")
                yield OptionList(id="order")
                yield Label("X-axis:")
                yield Select(
                    _x_choices_with(self.metrics, self.spec.system, self.spec.x),
                    value=self.spec.x, allow_blank=False, id="x",
                )
                with Horizontal(id="opts-row"):
                    with Vertical(classes="opt-col"):
                        yield Label("Y min (empty = auto)")
                        yield Input(text(o.ymin), id="ymin")
                    with Vertical(classes="opt-col"):
                        yield Label("Y max (empty = auto)")
                        yield Input(text(o.ymax), id="ymax")
                    with Vertical(classes="opt-col"):
                        yield Label("Smoothing (0 to 0.99)")
                        yield Input(text(o.smooth or None), id="smooth")
                with Horizontal(id="log-row"):
                    yield Checkbox("Logarithmic y-axis", o.logy, id="logy")
                    yield Checkbox("Logarithmic x-axis", o.logx, id="logx")
            else:  # BAR
                yield MetricPicker(self._picker_options, limit=MAX_BAR_VALUES, initial=self._initial, id="picker")
                yield Label("Bar order (Ctrl+↑ / Ctrl+↓ moves the highlighted one):")
                yield OptionList(id="order")
                with Horizontal(id="opts-row"):
                    with Vertical(classes="opt-col"):
                        yield Label("Axis min (empty = auto)")
                        yield Input(text(o.ymin), id="ymin")
                    with Vertical(classes="opt-col"):
                        yield Label("Axis max (empty = auto)")
                        yield Input(text(o.ymax), id="ymax")
            if self.kind in (PLOT, BAR):
                yield Static("", id="options-error")
            with Horizontal():
                yield Button("Apply", variant="primary", id="ok")
                if self.kind in (PLOT, BAR):
                    yield Button("Back to defaults", id="reset")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        if self.kind in (PLOT, BAR):
            self._refresh_order()
        target = {TABLE: "#table", IMAGE: "#image-key"}.get(self.kind, "#ymin")
        self.query_one(target).focus()

    def _error(self, message: str) -> None:
        if self.kind in (PLOT, BAR):
            self.query_one("#options-error", Static).update(plain(message))

    def _refresh_order(self, highlight: int | None = None) -> None:
        _refresh_order_list(self.query_one("#order", OptionList), self.query_one("#picker", MetricPicker).chosen, self._labels, highlight)

    def on_metric_picker_changed(self, event: MetricPicker.Changed) -> None:
        self._error("")
        if self.kind in (PLOT, BAR):
            cursor, self._cursor = self._cursor, None
            self._refresh_order(cursor)
        if self.kind != PLOT:
            return
        chosen = self.query_one("#picker", MetricPicker).chosen
        system = any(is_system for is_system, _ in chosen)
        if system != self._system_x:
            self._system_x = system
            x = self.query_one("#x", Select)
            previous = x.value
            options = x_options(self.metrics, system)
            x.set_options([(plain(label), value) for label, value in options])
            if previous in [v for _, v in options]:
                x.value = previous

    def on_metric_picker_limit_reached(self, event: MetricPicker.LimitReached) -> None:
        limit = MAX_BAR_VALUES if self.is_bar else MAX_SERIES
        what = "bar chart" if self.is_bar else "plot"
        self._error(f"A {what} can show at most {limit} values.")

    def action_move(self, delta: int) -> None:
        if self.kind not in (PLOT, BAR):
            return
        self._cursor = _move_order_item(self.query_one("#picker", MetricPicker), self.query_one("#order", OptionList), delta)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # Textual also runs CancellableDialog's handler (OK / cancel); only Reset is ours, so it
        # must keep that one from running (and from dismissing the screen twice).
        if event.button.id == "reset":
            event.prevent_default()
            event.stop()
            self.dismiss(replace(self.spec, opts=ChartOptions()))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("ymin", "ymax", "smooth"):
            event.stop()
            self._submit()

    def _submit(self) -> None:
        if self.kind == TABLE:
            self.dismiss(ChartSpec(str(self.query_one("#table", Select).value), kind=TABLE))
            return
        if self.kind == IMAGE:
            self.dismiss(ChartSpec(str(self.query_one("#image-key", Select).value), kind=IMAGE))
            return
        chosen = self.query_one("#picker", MetricPicker).chosen
        if self.kind == PLOT:
            if not chosen:
                self._error("Tick at least one value.")
                return
            if len({is_system for is_system, _ in chosen}) > 1:
                self._error("Pick either run metrics or system metrics in one plot, not both.")
                return
            smooth = self.query_one("#smooth", Input).value
            logy, logx = self.query_one("#logy", Checkbox).value, self.query_one("#logx", Checkbox).value
            result = parse_options(
                self.query_one("#ymin", Input).value, self.query_one("#ymax", Input).value, smooth, logy, logx,
            )
            if isinstance(result, str):
                self._error(result)
                return
            names = [name for _, name in chosen]
            x = str(self.query_one("#x", Select).value)
            self.dismiss(ChartSpec(names[0], x, chosen[0][0], PLOT, tuple(names[1:]), result))
            return
        if len(chosen) < 2:
            self._error("Pick at least two values. A single value is better shown as a number.")
            return
        result = parse_options(
            self.query_one("#ymin", Input).value, self.query_one("#ymax", Input).value, "", False, False,
        )
        if isinstance(result, str):
            self._error(result)
            return
        names = [name for _, name in chosen]
        self.dismiss(ChartSpec(names[0], kind=BAR, more=tuple(names[1:]), opts=result))
