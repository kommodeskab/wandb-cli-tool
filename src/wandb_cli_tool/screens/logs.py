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
FOLLOW_SECONDS = 5  # how often a followed (live) log is checked for new lines


class LogsScreen(Screen):
    """Terminal output of a run. Scroll with arrows/PageUp/PageDown/Home/End. A run that is still
    running is followed: new lines are added (and shown) every few seconds; `f` turns that off."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("r", "reload", "Reload"),
        Binding("f", "toggle_follow", "Follow"),
    ]
    app: "WandbCliApp"

    def __init__(self, ref: RunRef, name: str, live: bool = False) -> None:
        super().__init__()
        self.ref = ref
        self.run_name = name
        self.live = live  # the run is running, so more lines may come
        self.follow = live
        self._lines = 0
        self._loading = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Loading…", id="status")
        yield RichLog(id="log", wrap=True, markup=False, highlight=False, auto_scroll=False)
        yield KeyBar()

    def on_mount(self) -> None:
        self.title = f"Logs — {self.run_name}"
        self.query_one("#log", RichLog).focus()
        self.load()
        if self.live:
            self.set_interval(FOLLOW_SECONDS, self._tick)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "toggle_follow":
            return self.live  # nothing new comes from a run that is not running
        return True

    def _tick(self) -> None:
        if self.follow and not self._loading and self.app.screen is self:
            self.load()  # only the lines after the ones already shown are added

    def action_toggle_follow(self) -> None:
        self.follow = not self.follow
        self._finish()
        if self.follow:
            self.load()

    def action_reload(self) -> None:
        self.query_one("#log", RichLog).clear()
        self._lines = 0
        self.query_one("#status", Static).update("Loading…")
        self.load()

    @work(thread=True, exclusive=True)
    def load(self) -> None:
        worker = get_current_worker()
        batch: list[str] = []
        skip = self._lines  # lines already on screen from an earlier load
        self._loading = True
        try:
            for number, line in enumerate(self.app.client.console_lines(self.ref)):
                if worker.is_cancelled:
                    return
                if number < skip:
                    continue
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
        finally:
            self._loading = False

    def _append(self, lines: list[str]) -> None:
        log = self.query_one("#log", RichLog)
        for line in lines:
            log.write(Text.from_ansi(line), scroll_end=False)
        if self.follow:
            log.scroll_end(animate=False)
        self._lines += len(lines)
        self.query_one("#status", Static).update(plain(f"Loading… {self._lines} lines"))

    def _finish(self) -> None:
        text = f"{self._lines} lines" if self._lines else "No logs for this run."
        if self.live:
            text += f"  ·  following (checks every {FOLLOW_SECONDS} s)" if self.follow else "  ·  not following (f)"
        self.query_one("#status", Static).update(plain(text))

    def _fail(self, message: str) -> None:
        self.query_one("#status", Static).update(plain(f"Error: {message}"))
