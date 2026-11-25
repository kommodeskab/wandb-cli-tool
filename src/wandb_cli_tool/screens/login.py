from __future__ import annotations

from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option

from ..config import Credentials
from ..plain import plain

if TYPE_CHECKING:
    from ..app import WandbCliApp


class LoginScreen(Screen[Credentials]):
    """Step 1: API key. Step 2: pick an entity from a dropdown of the key's entities."""

    app: "WandbCliApp"

    def compose(self) -> ComposeResult:
        with Vertical(id="login"):
            yield Label("Log in to Weights & Biases")
            yield Label("API key (press Enter to continue):")
            yield Input(password=True, id="api-key")
            yield Static("", id="message")
            with Vertical(id="entity-step"):
                yield Label("Entity:")
                yield Button("Log in", variant="primary", id="submit")

    def on_mount(self) -> None:
        self._set_entity_step(visible=False)
        self.query_one("#api-key", Input).focus()

    def _set_entity_step(self, visible: bool) -> None:
        self.query_one("#entity-step").display = visible

    def _message(self, text: str) -> None:
        self.query_one("#message", Static).update(plain(text))

    def on_input_changed(self, event: Input.Changed) -> None:
        self._set_entity_step(visible=False)  # key edited: entities are stale

    def on_input_submitted(self, event: Input.Submitted) -> None:
        key = event.value.strip()
        if not key:
            self._message("Enter an API key.")
            return
        self._message("Checking API key…")
        self.fetch_entities(key)

    @work(thread=True, exclusive=True)
    def fetch_entities(self, key: str) -> None:
        try:
            entities = self.app.client_factory(key).list_entities()
        except Exception as exc:
            self.app.call_from_thread(self._message, f"Login failed: {exc}")
            return
        self.app.call_from_thread(self._show_entities, entities)

    def _show_entities(self, entities: list[str]) -> None:
        if not entities:
            self._message("No entities found for this API key.")
            return
        self._message("")
        self.query("#entity").remove()
        select = Select([(plain(e), e) for e in entities], allow_blank=False, id="entity")
        self.query_one("#entity-step").mount(select, before="#submit")
        self._set_entity_step(visible=True)
        select.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        selects = self.query("#entity")
        entity = selects.first(Select).value if selects else None
        key = self.query_one("#api-key", Input).value.strip()
        if isinstance(entity, str) and key:
            self.dismiss(Credentials(key, entity))


class EntityScreen(ModalScreen[str]):
    """Pick a different entity (Enter selects, Escape cancels)."""

    BINDINGS = [("escape", "dismiss_none", "Cancel")]
    app: "WandbCliApp"

    def __init__(self, current: str | None) -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="entity-picker"):
            yield Label("Select entity (Enter to choose, Esc to cancel)")
            yield Static("Loading…", id="entity-status")
            yield OptionList(id="entities")

    def on_mount(self) -> None:
        self.query_one("#entities", OptionList).focus()
        self.fetch_entities()

    @work(thread=True, exclusive=True)
    def fetch_entities(self) -> None:
        try:
            entities = self.app.client.list_entities()
        except Exception as exc:
            self.app.call_from_thread(self._fail, str(exc))
            return
        self.app.call_from_thread(self._show, entities)

    def _fail(self, message: str) -> None:
        self.query_one("#entity-status", Static).update(plain(f"Error: {message}"))

    def _show(self, entities: list[str]) -> None:
        self.query_one("#entity-status", Static).update("")
        options = self.query_one("#entities", OptionList)
        options.add_options([Option(plain(e)) for e in entities])
        options.highlighted = entities.index(self.current) if self.current in entities else 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(str(event.option.prompt))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)
