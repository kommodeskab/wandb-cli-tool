from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import RichLog, Static
from textual.worker import get_current_worker

from ..api import RunRef
from ..plain import plain

if TYPE_CHECKING:
    from ..app import WandbCliApp

BATCH_SIZE = 200
FOLLOW_SECONDS = 5  # how often a followed (live) log is checked for new lines


class LogsPane(Vertical):
    """Terminal output of a run. Scroll with arrows/PageUp/PageDown/Home/End. A run that is still
    running is followed: new lines are added (and shown) every few seconds; `f` turns that off."""

    DEFAULT_CSS = """
    LogsPane { height: 1fr; }
    LogsPane #status { color: $text-muted; padding: 0 1; }
    LogsPane #log { height: 1fr; }
    """
    app: "WandbCliApp"

    def __init__(self, ref: RunRef, name: str, live: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.ref = ref
        self.run_name = name
        self.live = live  # the run is running, so more lines may come
        self.follow = live
        self._lines = 0
        self._loading = False
        self._started = False
        self._timer = None

    def compose(self) -> ComposeResult:
        yield Static("Loading…", id="status")
        yield RichLog(id="log", wrap=True, markup=False, highlight=False, auto_scroll=False)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def activate(self, live: bool | None = None) -> None:
        if live is not None:
            self.set_live(live)
        if not self._started:
            self._started = True
            self.load()
            self._apply_timer()
        elif self.live and self.follow and not self._loading:
            self.load()

    def set_live(self, live: bool) -> None:
        if live == self.live:
            return
        self.live = live
        self.follow = live
        if self._started:
            self._apply_timer()
            if not self._loading:
                self._finish()
                if live:
                    self.load()
        if self.is_attached:
            self.screen.refresh_bindings()

    def _apply_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self.live:
            self._timer = self.set_interval(FOLLOW_SECONDS, self._tick)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "toggle_follow":
            return self.live  # nothing new comes from a run that is not running
        return True

    def _tick(self) -> None:
        if (
            self.follow
            and not self._loading
            and self.is_attached
            and self.app.screen is self.screen
            and self.screen.active_tab == "logs"
        ):
            self.load()  # only the lines after the ones already shown are added

    def toggle_state(self, action: str) -> bool | None:
        return self.follow if action == "toggle_follow" else None

    def action_toggle_follow(self) -> None:
        self.follow = not self.follow
        if self.is_attached:
            self.screen.refresh_bindings()  # the key shows the new state
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
