from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Header, Static, TabbedContent, TabPane

from ..api import CopyEstimate, RunInfo, RunMetadata, RunRef
from .keybar import KeyBar
from ..plain import plain
from ..runfacts import summary_fields
from .charts import ChartsPane
from .config import ConfigPane
from .copy import CopyOutcome, CopyProgressScreen, describe_copy
from .dialogs import (
    ConfirmDialog, DeleteChoice, DeleteRunsDialog, GroupDialog, RenameDialog, TagsDialog,
)
from .files import FilesPane
from .logs import LogsPane

if TYPE_CHECKING:
    from ..app import WandbCliApp

CHARTS_ACTIONS = (
    "new_panel",
    "add_chart",
    "delete_item",
    "reset_layout",
    "cycle_focus",
    "focus_chart",
    "focus_panels",
)


class RunScreen(Screen):
    """A run: five tabs (Overview, Logs, Charts, Config, Files) switched with 1–5."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("1", "tab('overview')", "Overview"),
        Binding("2", "tab('logs')", "Logs"),
        Binding("3", "tab('charts')", "Charts"),
        Binding("4", "tab('config')", "Config"),
        Binding("5", "tab('files')", "Files"),
        Binding("g", "set_group", "Set group"),
        Binding("t", "add_tags", "Add tags"),
        Binding("r", "run_r", "Rename"),
        Binding("c", "copy_run", "Copy run"),
        Binding("s", "stop_run", "Stop run"),
        Binding("delete", "run_delete", "Delete run"),
        Binding("backspace", "delete_item", "Delete chart", show=False),
        Binding("o", "run_o", "Open in browser"),
        Binding("f", "run_f", "Follow"),
        Binding("n", "new_panel", "New panel"),
        Binding("a", "add_chart", "Add chart"),
        Binding("d", "reset_layout", "Default layout"),
        Binding("tab", "cycle_focus(1)", "Next chart"),
        Binding("shift+tab", "cycle_focus(-1)", "Previous", show=False),
        Binding("right", "focus_chart(0)", "Charts", show=False),
    ]
    DEFAULT_CSS = """
    #tabs { height: 1fr; }
    RunScreen TabPane { height: 1fr; padding: 0; }
    #overview { height: 1fr; }
    #tags { padding: 1 1 0 1; }
    #info { padding: 0 1; }
    #info-wide { padding: 0 1 1 1; }
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
        with TabbedContent(initial="overview-pane", id="tabs"):
            with TabPane("Overview", id="overview-pane"):
                with VerticalScroll(id="overview"):
                    yield Static("Tags: …", id="tags")
                    yield Static("", id="info")
                    yield Static("", id="info-wide")
            with TabPane("Logs", id="logs-pane"):
                yield LogsPane(self.ref, self.run_name, id="logs-view")
            with TabPane("Charts", id="charts-pane"):
                yield ChartsPane(self.ref, self.run_name, id="charts-view")
            with TabPane("Config", id="config-pane"):
                yield ConfigPane(self.ref, self.run_name, id="config-view")
            with TabPane("Files", id="files-pane"):
                yield FilesPane(self.ref, self.run_name, id="files-view")
        yield KeyBar()

    @property
    def active_tab(self) -> str:
        try:
            return (self.query_one("#tabs", TabbedContent).active or "").removesuffix("-pane")
        except Exception:
            return "overview"

    @property
    def logs_pane(self) -> LogsPane:
        return self.query_one("#logs-view", LogsPane)

    @property
    def charts_pane(self) -> ChartsPane:
        return self.query_one("#charts-view", ChartsPane)

    @property
    def config_pane(self) -> ConfigPane:
        return self.query_one("#config-view", ConfigPane)

    @property
    def files_pane(self) -> FilesPane:
        return self.query_one("#files-view", FilesPane)

    def on_mount(self) -> None:
        self.title = f"Run — {self.run_name}"
        self.sub_title = self.ref.path
        self.query_one("#overview").focus()
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
        self.logs_pane.set_live(info.state == "running")
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
            tags.append("none", style="dim")
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
        tab = self.active_tab
        if action == "stop_run":
            return tab == "overview" and self.info is not None and self.info.state == "running"
        if action in ("set_group", "add_tags", "copy_run"):
            return tab == "overview"
        if action == "run_r":
            return tab in ("overview", "logs", "charts", "files")
        if action == "run_delete":
            if tab == "overview":
                return True
            return self.charts_pane.check_action("delete_item", ()) if tab == "charts" else False
        if action == "run_o":
            if tab == "overview":
                return True
            return self.charts_pane.check_action("chart_options", ()) if tab == "charts" else False
        if action == "run_f":
            if tab == "logs":
                return self.logs_pane.check_action("toggle_follow", ())
            return False
        if action in CHARTS_ACTIONS:
            return self.charts_pane.check_action(action, parameters) if tab == "charts" else False
        return True

    def key_label(self, action: str) -> str | None:
        tab = self.active_tab
        if action == "run_r":
            return {"overview": "Rename", "logs": "Reload", "charts": "Refresh", "files": "Refresh"}.get(tab)
        if action == "run_delete":
            return self.charts_pane.chart_key_label("delete_item") if tab == "charts" else "Delete run"
        if action == "run_o":
            return "Chart options" if tab == "charts" else "Open in browser"
        if action == "run_f":
            return "Follow"
        return None

    def toggle_state(self, action: str) -> bool | None:
        if action == "run_f" and self.active_tab == "logs":
            return self.logs_pane.toggle_state("toggle_follow")
        return None

    # ---- tab switching and shared bindings -----------------------------
    def action_tab(self, name: str) -> None:
        self.query_one("#tabs", TabbedContent).active = f"{name}-pane"

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        event.stop()
        tab = self.active_tab
        if tab == "logs":
            self.logs_pane.activate(self.info.state == "running" if self.info is not None else None)
            self.query_one("#log").focus()
        elif tab == "charts":
            self.charts_pane.activate()
            self.query_one("#panels").focus()
        elif tab == "config":
            self.config_pane.activate()
            self.query_one("#config").focus()
        elif tab == "files":
            self.files_pane.activate()
            self.query_one("#files").focus()
        else:
            self.query_one("#overview").focus()
        self.refresh_bindings()

    def action_run_r(self) -> None:
        tab = self.active_tab
        if tab == "overview":
            self.action_rename()
        elif tab == "logs":
            self.logs_pane.action_reload()
        elif tab == "charts":
            self.charts_pane.action_refresh_now()
        elif tab == "files":
            self.files_pane.action_reload()

    def action_run_delete(self) -> None:
        if self.active_tab == "charts":
            self.charts_pane.action_delete_item()
        elif self.active_tab == "overview":
            self.action_delete_run()

    def action_run_o(self) -> None:
        if self.active_tab == "charts":
            self.charts_pane.action_chart_options()
        elif self.active_tab == "overview":
            self.action_open_in_browser()

    def action_run_f(self) -> None:
        if self.active_tab == "logs":
            self.logs_pane.action_toggle_follow()

    def action_new_panel(self) -> None:
        if self.active_tab == "charts":
            self.charts_pane.action_new_panel()

    def action_add_chart(self) -> None:
        if self.active_tab == "charts":
            self.charts_pane.action_add_chart()

    def action_delete_item(self) -> None:
        if self.active_tab == "charts":
            self.charts_pane.action_delete_item()

    def action_reset_layout(self) -> None:
        if self.active_tab == "charts":
            self.charts_pane.action_reset_layout()

    def action_focus_chart(self, delta: int) -> None:
        if self.active_tab == "charts":
            self.charts_pane.action_focus_chart(delta)

    def action_focus_panels(self) -> None:
        if self.active_tab == "charts":
            self.charts_pane.action_focus_panels()

    def action_cycle_focus(self, delta: int) -> None:
        if self.active_tab == "charts":
            self.charts_pane.action_cycle_focus(delta)

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
    def action_open_in_browser(self) -> None:
        try:
            url = self.app.client.run_url(self.ref)
        except Exception as exc:
            self.app.call_from_thread(self.notify, f"Could not get the link: {exc}", severity="error")
            return
        self.app.call_from_thread(self.app.open_link, url)

    # ---- stop and delete -------------------------------------------------------------
    def action_stop_run(self) -> None:
        if self.info is None or self.info.state != "running":
            return
        self.app.push_screen(
            ConfirmDialog(
                "Stop this run?",
                f"“{self.info.name}”\n\n"
                "The run is asked to stop gracefully, like the Stop button in W&B. Nothing is deleted.",
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
        self.notify("Stop requested.")

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
        self.app.deleted_runs.add((self.ref.entity, self.ref.project, self.ref.id))
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
