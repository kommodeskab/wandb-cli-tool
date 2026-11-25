from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, OptionList, Static
from textual.widgets.option_list import Option

from ..api import CopyEstimate, RunInfo, RunMetadata, RunRef
from .keybar import KeyBar
from ..plain import plain
from ..runfacts import summary_fields
from .charts import ChartsScreen
from .config import ConfigScreen
from .copy import CopyOutcome, CopyProgressScreen, describe_copy
from .dialogs import (
    ConfirmDialog, DeleteChoice, DeleteRunsDialog, GroupDialog, RenameDialog, TagsDialog,
)
from .files import FilesScreen
from .logs import LogsScreen

if TYPE_CHECKING:
    from ..app import WandbCliApp

class RunScreen(Screen):
    """A run: tags and key facts at the top, then a menu (Logs, Charts, Config, Files)."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("g", "set_group", "Set group"),
        Binding("t", "add_tags", "Add tags"),
        Binding("r", "rename", "Rename"),
        Binding("c", "copy_run", "Copy run"),
        Binding("s", "stop_run", "Stop run"),
        Binding("delete", "delete_run", "Delete run"),
        Binding("y", "copy_link", "Copy link"),
    ]
    DEFAULT_CSS = """
    #tags { padding: 1 1 0 1; }
    #info { padding: 0 1; }
    #info-wide { padding: 0 1 1 1; }
    #menu { height: auto; margin: 0 1; }
    """
    app: "WandbCliApp"

    def __init__(
        self,
        ref: RunRef,
        name: str,
        groups: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.ref = ref
        self.run_name = name
        self.known_groups = list(groups or [])
        self.known_tags = list(tags or [])
        self.info: RunInfo | None = None
        self.meta: RunMetadata | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Tags: …", id="tags")
        yield Static("", id="info")
        yield Static("", id="info-wide")
        yield OptionList(
            Option("Logs", id="logs"),
            Option("Charts", id="charts"),
            Option("Config", id="config"),
            Option("Files", id="files"),
            id="menu",
        )
        yield KeyBar()

    def on_mount(self) -> None:
        self.title = f"Run — {self.run_name}"
        self.sub_title = self.ref.path
        menu = self.query_one("#menu", OptionList)
        menu.highlighted = 0
        menu.focus()
        self._render_info()
        self.load_details()
        self.load_metadata()

    # ---- loading -------------------------------------------------------
    @work(thread=True)
    def load_details(self) -> None:
        try:
            info = self.app.client.run_details(self.ref)
        except Exception as exc:
            self.app.call_from_thread(self._load_failed, str(exc))
            return
        self.app.call_from_thread(self._set_info, info)

    @work(thread=True)
    def load_metadata(self) -> None:
        try:
            meta = self.app.client.run_metadata(self.ref)
        except Exception:
            meta = RunMetadata()  # no metadata file: show dashes
        self.app.call_from_thread(self._set_meta, meta)

    def _set_info(self, info: RunInfo) -> None:
        self.info = info
        self._render_info()
        self.refresh_bindings()  # "Stop run" only applies while the run is running

    def _set_meta(self, meta: RunMetadata) -> None:
        self.meta = meta
        self._render_info()

    def _load_failed(self, message: str) -> None:
        self.query_one("#tags", Static).update(plain(f"Could not load run details: {message}"))

    # ---- rendering -----------------------------------------------------
    def _render_info(self) -> None:
        tags = Text("Tags: ", style="bold")
        if self.info is None:
            tags.append("…")
        elif not self.info.tags:
            tags.append("none  (press t to add)", style="dim")
        else:
            for tag in self.info.tags:
                tags.append(f" {tag} ", style="black on cyan")
                tags.append(" ")
        self.query_one("#tags", Static).update(tags)

        short, wide = summary_fields(self.info, self.meta)
        grid = Table.grid(padding=(0, 2))
        for _ in range(4):
            grid.add_column()
        for i in range(0, len(short), 2):
            row = []
            for label, value in short[i : i + 2]:
                row += [Text(label, style="dim"), Text(value)]
            grid.add_row(*row)
        self.query_one("#info", Static).update(grid)

        wide_grid = Table.grid(padding=(0, 2))
        wide_grid.add_column()
        wide_grid.add_column(overflow="fold")
        for label, value in wide:
            wide_grid.add_row(Text(label, style="dim"), Text(value))
        self.query_one("#info-wide", Static).update(wide_grid)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "stop_run":
            return self.info is not None and self.info.state == "running"
        return True

    # ---- navigation ----------------------------------------------------
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        screens = {
            "logs": LogsScreen,
            "charts": ChartsScreen,
            "config": ConfigScreen,
            "files": FilesScreen,
        }
        if event.option.id == "logs":  # a running run's log is followed
            live = self.info is not None and self.info.state == "running"
            self.app.push_screen(LogsScreen(self.ref, self.run_name, live))
            return
        self.app.push_screen(screens[str(event.option.id)](self.ref, self.run_name))

    def _mark_project_edited(self) -> None:
        """Tell the runs list (behind this screen) that its rows are out of date."""
        self.app.touched_projects.add((self.ref.entity, self.ref.project))

    # ---- the writes: group, tags, name, copy ---------------------------
    def action_set_group(self) -> None:
        if self.info is None:
            self.notify("Run details are still loading.", severity="warning")
            return
        self.app.push_screen(GroupDialog(self.known_groups, self.info.group), self._on_group)

    def _on_group(self, group: str | None) -> None:
        if group:
            self.write_group(group)

    @work(thread=True, group="write")
    def write_group(self, group: str) -> None:
        try:
            self.app.client.set_group(self.ref, group)
        except Exception as exc:
            self.app.call_from_thread(self.notify, f"Could not set group: {exc}", severity="error")
            return
        self.app.call_from_thread(self._group_saved, group)

    def _group_saved(self, group: str) -> None:
        if self.info is not None:
            self.info = replace(self.info, group=group)
        if group not in self.known_groups:
            self.known_groups.append(group)
        self._mark_project_edited()
        self._render_info()
        self.notify(f"Group set to “{group}”.")

    def action_rename(self) -> None:
        if self.info is None:
            self.notify("Run details are still loading.", severity="warning")
            return
        self.app.push_screen(RenameDialog(self.info.name), self._on_rename)

    def _on_rename(self, name: str | None) -> None:
        if name:
            self.write_name(name)

    @work(thread=True, group="write")
    def write_name(self, name: str) -> None:
        try:
            saved = self.app.client.rename_run(self.ref, name)
        except Exception as exc:
            self.app.call_from_thread(self.notify, f"Could not rename run: {exc}", severity="error")
            return
        self.app.call_from_thread(self._name_saved, saved)

    def _name_saved(self, name: str) -> None:
        self.run_name = name
        self.title = f"Run — {name}"
        if self.info is not None:
            self.info = replace(self.info, name=name)
        self._mark_project_edited()
        self.notify(f"Run renamed to “{name}”.")

    # ---- copy: estimate, confirm (yes/no), then run in a child process --
    def action_copy_run(self) -> None:
        if self.info is None:
            self.notify("Run details are still loading.", severity="warning")
            return
        self.notify("Checking what would be copied…")
        self.estimate_copy()

    @work(thread=True, group="estimate", exclusive=True)
    def estimate_copy(self) -> None:
        try:
            estimate = self.app.client.copy_estimate(self.ref)
        except Exception:
            estimate = None  # still let the user decide, just without sizes
        self.app.call_from_thread(self._confirm_copy, estimate)

    def _confirm_copy(self, estimate: CopyEstimate | None) -> None:
        if self.info is None:
            return
        body = describe_copy(self.info.name, self.info.state, estimate)
        self.app.push_screen(
            ConfirmDialog("Copy this run?", body, yes="Yes, copy", no="No"), self._on_copy_confirmed
        )

    def _on_copy_confirmed(self, yes: bool | None) -> None:
        if yes and self.info is not None:
            self.app.push_screen(CopyProgressScreen(self.ref, self.info.name), self._on_copy_finished)

    def _on_copy_finished(self, outcome: CopyOutcome | None) -> None:
        if outcome is not None and outcome.run_id:  # a partial run also shows up in the list
            self._mark_project_edited()

    @work(thread=True, group="link", exclusive=True)
    def action_copy_link(self) -> None:
        try:
            url = self.app.client.run_url(self.ref)
        except Exception as exc:
            self.app.call_from_thread(self.notify, f"Could not get the link: {exc}", severity="error")
            return
        self.app.call_from_thread(self.app.copy_link, url)

    # ---- stop and delete -------------------------------------------------------------
    def action_stop_run(self) -> None:
        if self.info is None or self.info.state != "running":
            return
        self.app.push_screen(
            ConfirmDialog(
                "Stop this run?",
                f"“{self.info.name}”\n\n"
                "This asks the run to stop, like the Stop button in W&B: its own process shuts\n"
                "down gracefully on its next heartbeat (a few seconds to a minute). Nothing is\n"
                "deleted. A run that is not listening (e.g. its machine is gone) will not react.",
                yes="Yes, stop it", no="No",
            ),
            self._on_stop_confirmed,
        )

    def _on_stop_confirmed(self, yes: bool | None) -> None:
        if yes:
            self.write_stop()

    @work(thread=True, group="write")
    def write_stop(self) -> None:
        try:
            self.app.client.stop_run(self.ref)
        except Exception as exc:
            self.app.call_from_thread(self.notify, f"Could not stop the run: {exc}", severity="error")
            return
        self.app.call_from_thread(self._stop_requested)

    def _stop_requested(self) -> None:
        self._mark_project_edited()
        self.notify("Stop requested. The run shuts down on its next heartbeat (r in the list refreshes it).")

    def action_delete_run(self) -> None:
        if self.info is None:
            self.notify("Run details are still loading.", severity="warning")
            return
        live = "\n⚠ This run is still running; deleting it does not stop its process.\n" if self.info.state == "running" else ""
        self.app.push_screen(
            DeleteRunsDialog(
                "Delete this run for good?",
                f"“{self.info.name}” ({self.info.id})\n\n"
                "Its metrics, logs, config and files are deleted from W&B and cannot be\n"
                "brought back. Artifacts it logged are kept unless you tick the box below."
                + live,
            ),
            self._on_delete_confirmed,
        )

    def _on_delete_confirmed(self, choice: DeleteChoice | None) -> None:
        if choice is not None:
            self.write_delete(choice.artifacts)

    @work(thread=True, group="write")
    def write_delete(self, artifacts: bool) -> None:
        try:
            self.app.client.delete_run(self.ref, artifacts)
        except Exception as exc:
            self.app.call_from_thread(self.notify, f"Could not delete the run: {exc}", severity="error")
            return
        self.app.call_from_thread(self._deleted, artifacts)

    def _deleted(self, artifacts: bool) -> None:
        self._mark_project_edited()
        self.notify(f"Run “{self.run_name}” deleted" + (" together with its artifacts." if artifacts else "."))
        self.app.pop_screen()  # nothing left to show: back to the list

    def action_add_tags(self) -> None:
        if self.info is None:
            self.notify("Run details are still loading.", severity="warning")
            return
        self.app.push_screen(
            TagsDialog(self.known_tags, list(self.info.tags)), self._on_tags
        )

    def _on_tags(self, tags: list[str] | None) -> None:
        if tags:
            self.write_tags(tags)

    @work(thread=True, group="write")
    def write_tags(self, tags: list[str]) -> None:
        try:
            merged = self.app.client.add_tags(self.ref, tags)
        except Exception as exc:
            self.app.call_from_thread(self.notify, f"Could not add tags: {exc}", severity="error")
            return
        self.app.call_from_thread(self._tags_saved, merged)

    def _tags_saved(self, tags: list[str]) -> None:
        if self.info is not None:
            self.info = replace(self.info, tags=tuple(tags))
        self.known_tags = sorted(set(self.known_tags) | set(tags))
        self._mark_project_edited()
        self._render_info()
        self.notify("Tags added.")
