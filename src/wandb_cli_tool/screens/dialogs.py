from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from ..plain import plain
from rich.text import Text
from textual.widgets.option_list import Option
from textual.widgets import Button, Checkbox, Input, Label, OptionList, Select, SelectionList, Static



class CancellableDialog(ModalScreen):
    """A modal that closes with `None` on Escape. Its button row is: the button with id "ok" calls
    `_submit()` (which should `dismiss(result)`), any other button cancels."""

    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    CancellableDialog { align: center middle; }
    CancellableDialog Horizontal { height: auto; margin-top: 1; }
    """

    def _submit(self) -> None:
        raise NotImplementedError

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._submit() if event.button.id == "ok" else self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class NewPanelDialog(CancellableDialog):
    """Ask for a panel name (Enter confirms, Esc cancels)."""

    DEFAULT_CSS = """
    NewPanelDialog > Vertical { width: 50; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("New panel name (Enter to create, Esc to cancel)")
            yield Input(id="panel-name")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        name = event.value.strip()
        if name:
            self.dismiss(name)


class SortDialog(CancellableDialog):
    """Pick what to sort the runs by. Picking the column already in use reverses it."""

    DEFAULT_CSS = """
    SortDialog > Vertical { width: 56; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    SortDialog OptionList { height: auto; margin-top: 1; }
    """

    def __init__(self, current) -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        from ..runsort import SORT_COLUMNS

        def label(column: str, name: str) -> Text:
            arrow = ("  ↓" if self.current.descending else "  ↑") if column == self.current.column else ""
            return Text(name + arrow, style="bold" if arrow else "")

        with Vertical():
            yield Label("Sort runs by  (Enter to choose; again on the same column to reverse)")
            yield OptionList(*[Option(label(c, n), id=c) for c, n in SORT_COLUMNS], id="sort-columns")

    def on_mount(self) -> None:
        from ..runsort import SORT_COLUMNS

        options = self.query_one("#sort-columns", OptionList)
        options.focus()
        options.highlighted = [c for c, _ in SORT_COLUMNS].index(self.current.column)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(self.current.choose(str(event.option.id)))


class GroupDialog(CancellableDialog):
    """Choose an existing group or type a new one. A run belongs to exactly one group."""

    DEFAULT_CSS = """
    GroupDialog > Vertical { width: 64; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    #group-error { color: $error; }
    """

    def __init__(self, groups: list[str], current: str = "") -> None:
        super().__init__()
        self.groups = sorted(set(groups) | ({current} if current else set()))
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Set group (a run can only be in one group)")
            yield Label(f"Current group: {self.current or 'none'}", markup=False)
            if self.groups:
                yield Label("Pick an existing group:")
                yield Select(
                    [(plain(g), g) for g in self.groups],
                    prompt="Choose a group…",
                    value=self.current if self.current else Select.NULL,
                    id="group",
                )
            yield Label("…or write a new group name:")
            yield Input(placeholder="new group name", id="new-group")
            yield Static("", id="group-error")
            with Horizontal():
                yield Button("Set group", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#group" if self.groups else "#new-group").focus()

    def _submit(self) -> None:
        new = self.query_one("#new-group", Input).value.strip()
        picked = self.query_one("#group", Select).value if self.groups else None
        group = new or (picked if isinstance(picked, str) else "")
        if not group:
            self.query_one("#group-error", Static).update("Pick a group or write a new one.")
        else:
            self.dismiss(group)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()



def split_tags(text: str) -> list[str]:
    return list(dict.fromkeys(t.strip() for t in text.split(",") if t.strip()))


class TagsDialog(CancellableDialog):
    """Add tags: tick existing ones (Space) and/or write new ones, comma-separated."""

    DEFAULT_CSS = """
    TagsDialog > Vertical { width: 64; height: auto; max-height: 90%; border: round $accent; padding: 1 2; background: $surface; }
    TagsDialog SelectionList { height: auto; max-height: 10; }
    #tags-error { color: $error; }
    """

    def __init__(self, available: list[str], current: list[str]) -> None:
        super().__init__()
        self.current = list(current)
        self.available = sorted(set(available) - set(current))

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Add tags (existing tags are kept)")
            yield Label(f"Current tags: {', '.join(self.current) or 'none'}", markup=False)
            if self.available:
                yield Label("Existing tags (↑/↓, Space to tick):")
                yield SelectionList[str](*[(plain(t), t) for t in self.available], id="tags")
            yield Label("New tags (comma-separated):")
            yield Input(placeholder="e.g. baseline, v2", id="new-tags")
            yield Static("", id="tags-error")
            with Horizontal():
                yield Button("Add tags", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#tags" if self.available else "#new-tags").focus()

    def _submit(self) -> None:
        picked = list(self.query_one("#tags", SelectionList).selected) if self.available else []
        new = split_tags(self.query_one("#new-tags", Input).value)
        tags = [t for t in dict.fromkeys([*picked, *new]) if t not in self.current]
        if not tags:
            self.query_one("#tags-error", Static).update("Tick or write at least one new tag.")
        else:
            self.dismiss(tags)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()



class RenameDialog(CancellableDialog):
    """Edit a run's display name (Enter saves, Esc cancels). The run id never changes."""

    DEFAULT_CSS = """
    RenameDialog > Vertical { width: 70; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    #rename-error { color: $error; }
    """

    def __init__(self, current: str) -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Rename run (Enter to save, Esc to cancel)")
            yield Input(value=self.current, id="new-name")
            yield Static("", id="rename-error")
            with Horizontal():
                yield Button("Rename", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        field = self.query_one("#new-name", Input)
        field.focus()
        field.select_all()

    def _submit(self) -> None:
        name = self.query_one("#new-name", Input).value.strip()
        if not name:
            self.query_one("#rename-error", Static).update("The name cannot be empty.")
        elif name == self.current:
            self.dismiss(None)  # nothing to change, so nothing is written
        else:
            self.dismiss(name)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()



class TypeToConfirmDialog(CancellableDialog):
    """For what cannot be undone: the confirm button only works once `phrase` has been typed, so a
    stray Enter or a habit of pressing "y" can never delete anything. Dismisses with True."""

    DEFAULT_CSS = """
    TypeToConfirmDialog > Vertical { width: 76; height: auto; max-height: 90%; border: round $error; padding: 1 2; background: $surface; }
    TypeToConfirmDialog Input { margin-top: 1; }
    """

    def __init__(self, title: str, body: str, phrase: str = "delete", ok: str = "Delete") -> None:
        super().__init__()
        self.title_text, self.body_text, self.phrase, self.ok_label = title, body, phrase, ok

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.title_text, markup=False)
            yield Static(plain(self.body_text), id="confirm-body")
            yield from self.compose_extra()
            yield Label(f"Type “{self.phrase}” to confirm:", markup=False, id="phrase-label")
            yield Input(id="phrase")
            with Horizontal():
                yield Button(self.ok_label, variant="error", id="ok", disabled=True)
                yield Button("Cancel", id="cancel")

    def compose_extra(self) -> ComposeResult:
        """Hook: more widgets between the text and the typing field."""
        yield from ()

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def _typed(self) -> bool:
        return self.query_one("#phrase", Input).value.strip().lower() == self.phrase.lower()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.query_one("#ok", Button).disabled = not self._typed()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    def _submit(self) -> None:
        if self._typed():
            self.dismiss(True)


@dataclass(frozen=True)
class DeleteChoice:
    """What the user agreed to when deleting runs."""

    artifacts: bool  # also delete the artifacts the runs logged


class DeleteRunsDialog(TypeToConfirmDialog):
    """Delete runs for good. The runs' artifacts are kept unless the box is ticked, which is a
    second, separate agreement: the word to type then becomes "delete artifacts". Dismisses with a
    `DeleteChoice`."""

    ARTIFACTS_PHRASE = "delete artifacts"

    def __init__(self, title: str, body: str, ok: str = "Delete") -> None:
        super().__init__(title, body, phrase="delete", ok=ok)

    def compose_extra(self) -> ComposeResult:
        yield Checkbox("Also delete the artifacts these runs logged (models, datasets, …)", id="artifacts")
        yield Static("", id="artifact-warning")

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        event.stop()
        with_artifacts = event.value
        self.phrase = self.ARTIFACTS_PHRASE if with_artifacts else "delete"
        self.query_one("#phrase-label", Label).update(f"Type “{self.phrase}” to confirm:")
        warning = Text("")
        if with_artifacts:
            warning = Text(
                "⚠ The artifacts are removed from W&B too, for good, even if other runs or\n"
                "  collections use them.",
                style="bold red",
            )
        self.query_one("#artifact-warning", Static).update(warning)
        self.query_one("#ok", Button).disabled = not self._typed()

    def _submit(self) -> None:
        if self._typed():
            self.dismiss(DeleteChoice(self.query_one("#artifacts", Checkbox).value))


class RunActionsDialog(CancellableDialog):
    """What to do with the selected runs. Dismisses with "stop", "group", "tags" or "delete"."""

    DEFAULT_CSS = """
    RunActionsDialog > Vertical { width: 60; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    RunActionsDialog OptionList { height: auto; margin-top: 1; }
    """

    def __init__(self, count: int, running: int) -> None:
        super().__init__()
        self.count, self.running = count, running

    def compose(self) -> ComposeResult:
        runs = f"{self.count} run{'s' * (self.count != 1)}"
        with Vertical():
            yield Label(f"Actions on {runs}", markup=False)
            yield OptionList(
                Option(f"Stop the running ones ({self.running})", id="stop", disabled=self.running == 0),
                Option("Set group…", id="group"),
                Option("Add tags…", id="tags"),
                Option("Delete…", id="delete"),
                id="actions",
            )

    def on_mount(self) -> None:
        options = self.query_one("#actions", OptionList)
        options.focus()
        options.highlighted = 0 if self.running else 1

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(str(event.option.id))


class ConfirmDialog(ModalScreen[bool]):
    """A yes/no question. Focus starts on "No", so a stray Enter never confirms.
    y = yes, n or Esc = no."""

    BINDINGS = [("y", "yes", "Yes"), ("n", "no", "No"), ("escape", "no", "No")]
    DEFAULT_CSS = """
    ConfirmDialog { align: center middle; }
    ConfirmDialog > Vertical { width: 84; height: auto; max-height: 90%; border: round $warning; padding: 1 2; background: $surface; }
    ConfirmDialog Horizontal { height: auto; margin-top: 1; }
    """

    def __init__(self, title: str, body: str, yes: str = "Yes", no: str = "No") -> None:
        super().__init__()
        self.title_text, self.body_text, self.yes_label, self.no_label = title, body, yes, no

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.title_text, markup=False)
            yield Static(plain(self.body_text), id="confirm-body")
            with Horizontal():
                yield Button(f"{self.yes_label} (y)", variant="warning", id="yes")
                yield Button(f"{self.no_label} (n)", id="no")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)
