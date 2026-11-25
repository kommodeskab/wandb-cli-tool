from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, RichLog, Static
from textual.worker import get_current_worker

from ..api import RunRef
from .keybar import KeyBar
from ..plain import plain

if TYPE_CHECKING:
    from ..app import WandbCliApp

BATCH_SIZE = 200


class LogsScreen(Screen):
    """Terminal output of a run. Scroll with arrows/PageUp/PageDown/Home/End."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]
    app: "WandbCliApp"

    def __init__(self, ref: RunRef, name: str) -> None:
        super().__init__()
        self.ref = ref
        self.run_name = name
        self._lines = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Loading…", id="status")
        yield RichLog(id="log", wrap=True, markup=False, highlight=False, auto_scroll=False)
        yield KeyBar()

    def on_mount(self) -> None:
        self.title = f"Logs — {self.run_name}"
        self.query_one("#log", RichLog).focus()
        self.load()

    @work(thread=True, exclusive=True)
    def load(self) -> None:
        worker = get_current_worker()
        batch: list[str] = []
        try:
            for line in self.app.client.console_lines(self.ref):
                if worker.is_cancelled:
                    return
                # progress bars rewrite a line with \r; keep what ends up visible
                batch.append(line.split("\r")[-1])
                if len(batch) >= BATCH_SIZE:
                    self.app.call_from_thread(self._append, batch)
                    batch = []
            if batch:
                self.app.call_from_thread(self._append, batch)
            self.app.call_from_thread(self._finish)
        except Exception as exc:
            if not worker.is_cancelled:
                self.app.call_from_thread(self._fail, str(exc))

    def _append(self, lines: list[str]) -> None:
        log = self.query_one("#log", RichLog)
        for line in lines:
            log.write(Text.from_ansi(line), scroll_end=False)
        self._lines += len(lines)
        self.query_one("#status", Static).update(plain(f"Loading… {self._lines} lines"))

    def _finish(self) -> None:
        text = f"{self._lines} lines" if self._lines else "No logs for this run."
        self.query_one("#status", Static).update(plain(text))

    def _fail(self, message: str) -> None:
        self.query_one("#status", Static).update(plain(f"Error: {message}"))
