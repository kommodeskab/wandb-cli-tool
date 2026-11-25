from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, Static

from ..api import CopyEstimate, RunRef
from ..formatting import fmt_size

if TYPE_CHECKING:
    from ..app import WandbCliApp


def describe_copy(name: str, state: str, estimate: CopyEstimate | None) -> str:
    """The text of the confirmation: what will be created, uploaded, and what will not."""
    lines = [
        f"Copy run “{name}” to a NEW run “{name} (copy)” in the same project?",
        "",
        "This uploads to your W&B account:",
        "  • config, tags, group and notes",
    ]
    if estimate is None:
        lines += ["  • the metric history, summary, files and artifacts", "    (their size could not be determined)"]
    else:
        rows = f"{estimate.history_rows:,}" if estimate.history_rows is not None else "all"
        lines += [
            f"  • {rows} metric history rows and the summary",
            f"  • {estimate.files:,} files, {fmt_size(estimate.file_bytes)}  (media, logs, …)",
            f"  • {estimate.artifacts:,} artifacts (models, datasets), {fmt_size(estimate.artifact_bytes)}"
            ", downloaded and",
            "    uploaded again",
        ]
    lines += [
        "",
        "Not copied: system metrics (CPU/GPU curves) and the original",
        "timestamps of history rows. Artifacts the run only used are linked,",
        "not duplicated. The original run is not changed.",
    ]
    if state == "running":
        lines += ["", "⚠ This run is still running: the copy only contains what", "  it has logged so far."]
    return "\n".join(lines)


@dataclass
class CopyOutcome:
    kind: str  # done | error | cancelled
    run_id: str | None = None
    name: str | None = None
    url: str | None = None
    message: str = ""
    warnings: list[str] = field(default_factory=list)


class CopyProgressScreen(ModalScreen[CopyOutcome]):
    """Runs the copy in a child process and shows its progress. Esc asks it to stop."""

    BINDINGS = [
        Binding("escape", "cancel_or_close", "Cancel"),
        Binding("enter", "close", "Close", show=False),
    ]
    DEFAULT_CSS = """
    CopyProgressScreen { align: center middle; }
    CopyProgressScreen > Vertical { width: 90; height: auto; max-height: 90%; border: round $accent; padding: 1 2; background: $surface; }
    #copy-stage, #copy-counts, #copy-warnings, #copy-result { padding: 0 0 1 0; }
    #copy-warnings { color: $warning; }
    """
    app: "WandbCliApp"

    def __init__(self, ref: RunRef, name: str) -> None:
        super().__init__()
        self.ref = ref
        self.run_name = name
        self.job = None
        self.outcome: CopyOutcome | None = None
        self.new_run_id: str | None = None
        self.warnings: list[str] = []
        self._cancelling = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Copying “{self.run_name}”", markup=False)
            yield Static("Starting…", id="copy-stage")
            yield Static("", id="copy-counts")
            yield Static("", id="copy-warnings")
            yield Static("", id="copy-result")
            yield Static("Esc: cancel", id="copy-hint")

    def on_mount(self) -> None:
        self.run_copy()

    def on_unmount(self) -> None:
        if self.job is not None and self.outcome is None:
            self.job.cancel()  # never leave a copy running after the app is gone

    @work(thread=True, exclusive=True)
    def run_copy(self) -> None:
        try:
            self.job = self.app.client.start_copy(self.ref)
            self.job.start()
            for event in self.job.events():
                self.app.call_from_thread(self._on_event, event)
        except Exception as exc:
            self.app.call_from_thread(
                self._on_event, {"event": "error", "message": str(exc), "run_id": self.new_run_id}
            )

    # ---- events from the child -----------------------------------------
    def _on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "started":
            self.new_run_id = event.get("run_id")
            self._set("#copy-stage", f"Created new run “{event.get('name')}” ({self.new_run_id})")
        elif kind == "progress":
            self._set("#copy-stage", str(event.get("message", "")))
            done, total = event.get("done"), event.get("total")
            if done is not None:
                self._set("#copy-counts", f"{event.get('stage', '')}: {done:,}" + (f" / {total:,}" if total else ""))
        elif kind == "warning":
            self.warnings.append(str(event.get("message", "")))
            shown = "\n".join(f"⚠ {w}" for w in self.warnings[-3:])
            more = f"\n(+{len(self.warnings) - 3} more)" if len(self.warnings) > 3 else ""
            self._set("#copy-warnings", shown + more)
        elif kind in ("done", "error", "cancelled"):
            self._finish(event)

    def _set(self, selector: str, text: str) -> None:
        self.query_one(selector, Static).update(Text(text))

    def _finish(self, event: dict[str, Any]) -> None:
        kind = event["event"]
        run_id = event.get("run_id") or self.new_run_id
        partial = f"A partial run ({run_id}) was left as a failed run; delete it in W&B if unwanted." if run_id else ""
        if kind == "done":
            message = f"Done. New run “{event.get('name')}” ({run_id}).\n{event.get('url') or ''}"
            self.warnings = list(event.get("warnings") or self.warnings)
            if self.warnings:
                message += f"\n{len(self.warnings)} warning(s) — see above."
        elif kind == "cancelled":
            message = f"Cancelled. {partial}"
        else:
            message = f"Copy failed: {event.get('message', 'unknown error')}\n{partial}"
        self.outcome = CopyOutcome(kind, run_id, event.get("name"), event.get("url"), message, self.warnings)
        self._set("#copy-stage", "")
        self._set("#copy-result", message)
        self._set("#copy-hint", "Enter or Esc: close")
        self.refresh_bindings()

    # ---- keys ----------------------------------------------------------
    def action_cancel_or_close(self) -> None:
        if self.outcome is not None:
            self.dismiss(self.outcome)
        elif self.job is not None and not self._cancelling:
            self._cancelling = True
            self._set("#copy-hint", "Cancelling… the new run is finished as failed first.")
            self.job.cancel()

    def action_close(self) -> None:
        if self.outcome is not None:
            self.dismiss(self.outcome)
