from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Iterator

from rich.console import RenderableType
from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.app import ComposeResult
from textual.widgets import DataTable, Input

from ..api import RunRef
from ..formatting import fmt_duration, fmt_time
from ..layout_store import project_key
from ..pins import Pins, pinned_first
from ..plain import plain
from ..runsort import (
    HEADER_TO_COLUMN, SORT_FIELDS, SortState, sort_items, status_rank,
)
from textual.widgets.data_table import RowDoesNotExist

from .browse import CACHE_LIMIT, BrowseScreen, Item, items_from_cache
from ..compare import MAX_RUNS
from .compare import CompareScreen
from .filterinput import FilterInput
from .dialogs import ConfirmDialog, DeleteRunsDialog, GroupDialog, RunActionsDialog, SortDialog, TagsDialog
from .run import RunScreen

if TYPE_CHECKING:
    from ..app import WandbCliApp

STATE_STYLES = {
    "running": "bold green",
    "finished": "",
    "failed": "red",
    "crashed": "red",
    "killed": "yellow",
}


BULK_WORKERS = 4  # writes in flight at once when an action covers several runs
WATCH_SECONDS = 30  # how often a list with running runs checks for changes


class RunsScreen(BrowseScreen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("slash", "filter", "Filter"),
        Binding("r", "reload", "Refresh"),
        Binding("s", "sort", "Sort"),
        Binding("space", "toggle_select", "Select run"),
        Binding("ctrl+a", "select_shown", "Select all shown", key_display="Ctrl+A"),
        Binding("c", "compare", "Compare selected"),
        Binding("u", "clear_selection", "Clear selection"),
        Binding("p", "pin", "Pin"),
        Binding("a", "actions", "Actions"),
        Binding("o", "open_in_browser", "Open in browser"),
    ]
    DEFAULT_CSS = "#filter { display: none; margin: 0 1; }"
    columns = ("Run", "Status", "Started", "Run time", "Group", "Tags", "ID")
    empty_text = "No runs in this project."
    app: "WandbCliApp"

    def __init__(self, entity: str, project: str) -> None:
        super().__init__()
        self.entity = entity
        self.project = project
        self.title_text = f"Runs — {entity}/{project}"
        self.sort_state = SortState()
        self.selected: list[str] = []  # run ids marked for comparison, in the order they were marked
        self.filter_text = ""
        self.pinned: list[str] = []  # run ids shown first, in the order they were pinned
        self._watching = False  # a refresh started by the timer (not by the user) is under way

    def compose_before_table(self) -> ComposeResult:
        yield FilterInput(placeholder="Filter: name, group, tag, status or id", id="filter")

    def prepare(self) -> None:
        self.sort_state = SortState.from_json(self.app.preferences.get("runs_sort"))
        self.pins = Pins(self.app.preferences, project_key(self.entity, self.project))
        self.pinned = self.pins.get("runs")
        self._mark_headers()
        self.set_interval(WATCH_SECONDS, self._watch_tick)

    # ---- sorting ----------------------------------------------------------------------
    def display_order(self, items: list[Item]) -> list[Item]:
        return pinned_first(sort_items(items, self.sort_state, self._sort_value), self.pinned, lambda i: i.key)

    def has_custom_order(self) -> bool:
        return not self.sort_state.is_server_order or bool(self.filter_text) or bool(self.pinned)

    def status_suffix(self) -> str:
        sort = "" if self.sort_state.is_server_order else f"  ·  sorted by {self.sort_state.label}"
        filtered = (
            f"  ·  filter “{self.filter_text}”: {sum(self._matches(i) for i in self._items.values())} shown"
            if self.filter_text else ""
        )
        running = sum(i.cells[1] == "running" for i in self._items.values())
        watching = f"  ·  watching {running} running" if running else ""
        return sort + filtered + watching + (f"  ·  {len(self.selected)} selected" if self.selected else "")

    # ---- filtering ----------------------------------------------------------------------
    def _matches(self, item: Item) -> bool:
        """Every word typed in the filter occurs in the run's name, status, group, tags, id, …"""
        text = "\n".join(item.cells).lower()
        return all(word in text for word in self.filter_text.lower().split())

    def _add_row(self, item: Item) -> None:
        if self._matches(item):
            super()._add_row(item)

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.display = True
        box.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            event.stop()
            self.filter_text = event.value.strip()
            self.resort()
            self._status_done()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter":
            event.stop()
            self.query_one("#items", DataTable).focus()  # the filter stays; Esc clears it

    def action_focus_results(self) -> None:
        self.query_one("#items", DataTable).focus()

    def action_back(self) -> None:
        box = self.query_one("#filter", Input)
        if box.display:  # first Esc closes the filter, the next one goes back
            box.value = ""
            box.display = False
            self.query_one("#items", DataTable).focus()
        else:
            self.app.pop_screen()

    # ---- selecting runs to compare ----------------------------------------------------
    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "compare":
            return len(self.selected) >= 2
        if action == "clear_selection":
            return bool(self.selected)
        return True

    def action_pin(self) -> None:
        """Pin the selected runs (or the run under the cursor) to the top of the list; again unpins."""
        targets = [i.key for i in self._targets()]
        if targets:
            table = self.query_one("#items", DataTable)
            row = table.cursor_row
            self.pins.toggle("runs", *targets)
            self.pinned = self.pins.get("runs")
            self.resort()
            if table.row_count:  # the pinned run jumps up; the cursor stays on its row number, so the
                table.move_cursor(row=min(row, table.row_count - 1), animate=False)  # next run is one Down away
            self._status_done()

    def action_toggle_select(self) -> None:
        key = self._cursor_key()
        if key is None or key not in self._items:
            return
        if key in self.selected:
            self.selected.remove(key)
        else:
            self.selected.append(key)
        self._selection_changed([key])

    def action_select_shown(self) -> None:
        """Select every run the filter currently shows (handy before an action on all of them)."""
        added = [i.key for i in self._items.values() if self._matches(i) and i.key not in self.selected]
        self.selected += added
        self._selection_changed(added)

    def action_clear_selection(self) -> None:
        keys, self.selected = self.selected, []
        self._selection_changed(keys)

    def _selection_changed(self, keys: list[str]) -> None:
        table = self.query_one("#items", DataTable)
        for key in keys:  # redraw just the name cell of the rows that changed
            item = self._items.get(key)
            if item is not None:
                table.update_cell(key, self.columns[0], self._cell(self.render_cells(item)[0]), update_width=True)
        self.refresh_bindings()
        self._status_done()

    # ---- actions on the selected runs (or the run under the cursor) ------------------
    def _targets(self) -> list[Item]:
        keys = self.selected or [k for k in [self._cursor_key()] if k]
        return [self._items[k] for k in keys if k in self._items]

    def action_actions(self) -> None:
        targets = self._targets()
        if targets:
            running = sum(1 for i in targets if i.cells[1] == "running")
            self.app.push_screen(RunActionsDialog(len(targets), running), self._on_action)

    @staticmethod
    def _names(items: list[Item], limit: int = 8) -> str:
        shown = "\n".join(f"  • {i.label} ({i.key})" for i in items[:limit])
        return shown + (f"\n  … and {len(items) - limit} more" if len(items) > limit else "")

    def _on_action(self, action: str | None) -> None:
        targets = self._targets()
        client = self.app.client
        if not action or not targets:
            return
        if action == "stop":
            running = [i for i in targets if i.cells[1] == "running"]
            self.app.push_screen(
                ConfirmDialog(
                    f"Stop {len(running)} run{'s' * (len(running) != 1)}?",
                    self._names(running) + "\n\nEach run is asked to stop gracefully. Nothing is deleted.",
                    yes="Yes, stop", no="No",
                ),
                lambda yes: self._maybe(yes, "Stop", client.stop_run, running),
            )
        elif action == "group":
            groups, _ = self.known_groups_and_tags()
            self.app.push_screen(
                GroupDialog(groups), lambda g: self._maybe(g, "Set group", lambda ref: client.set_group(ref, g), targets)
            )
        elif action == "tags":
            _, tags = self.known_groups_and_tags()
            self.app.push_screen(
                TagsDialog(tags, []), lambda t: self._maybe(t, "Add tags", lambda ref: client.add_tags(ref, t), targets)
            )
        elif action == "delete":
            self.app.push_screen(
                DeleteRunsDialog(
                    f"Delete {len(targets)} run{'s' * (len(targets) != 1)} for good?",
                    self._names(targets) + "\n\nTheir metrics, logs, config and files are deleted from W&B and cannot\n"
                    "be brought back. Artifacts they logged are kept unless you tick the box below.",
                    ok=f"Delete {len(targets)}",
                ),
                lambda choice: choice and self._maybe(
                    choice, "Delete" + (" (+ artifacts)" if choice.artifacts else ""),
                    lambda ref: client.delete_run(ref, choice.artifacts), targets,
                ),
            )

    def _maybe(self, chosen, label: str, write, items: list[Item]) -> None:
        """Start the writes if the dialog was confirmed (a dialog callback must return nothing)."""
        if chosen:
            self._apply(label, write, items)

    @work(thread=True, group="bulk")
    def _apply(self, label: str, write, items: list[Item]) -> None:
        """Run one write per run, a few at a time, and report how many worked and why the rest did not."""

        def one(item: Item) -> tuple[str, str | None]:
            try:
                write(self._ref(item))
                return item.key, None
            except Exception as exc:
                return item.key, f"{item.label}: {exc}"

        done: list[str] = []
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=min(len(items), BULK_WORKERS)) as pool:
            for n, (key, error) in enumerate(pool.map(one, items), 1):
                self.app.call_from_thread(self._status, f"{label}: {n}/{len(items)}…")
                if error is None:
                    done.append(key)
                else:
                    failures.append(error)
        self.app.call_from_thread(self._applied, label, len(items), done, failures)

    def _applied(self, label: str, total: int, done: list[str], failures: list[str]) -> None:
        if label.startswith("Delete"):
            self.app.deleted_runs.update((self.entity, self.project, k) for k in done)
            self._drop_deleted()
        if failures:
            more = f" (+{len(failures) - 1} more)" if len(failures) > 1 else ""
            self.notify(f"{label}: {len(done)} of {total} done. Failed — {failures[0]}{more}", severity="error")
        else:
            self.notify(f"{label}: done for {len(done)} run{'s' * (len(done) != 1)}.")
        self.refresh_bindings()
        self.refresh_in_place()

    # ---- watching the running runs ---------------------------------------------------------
    def _watch_tick(self) -> None:
        """While runs are running and this list is on screen, check on them from time to time."""
        if self.app.screen is not self or self._watching:
            return
        if any(i.cells[1] == "running" for i in self._items.values()):
            self._watching = True
            self.refresh_in_place()

    def _announce_changes(self, before: dict[str, str], fresh: list[Item]) -> None:
        """Tell the user about runs that ended or began since the last check."""
        news = []
        for item in fresh:
            old, new = before.get(item.key), item.cells[1]
            if old == "running" and new != "running":
                news.append((f"{item.label} {new}", "information" if new == "finished" else "warning"))
            elif old is None and before and new == "running":
                news.append((f"{item.label} started", "information"))
        for text, severity in news[:5]:
            self.notify(text, severity=severity)
        if len(news) > 5:
            self.notify(f"… and {len(news) - 5} more changes")
        if any(sev == "warning" for _, sev in news):
            self.app.bell()  # a run that did not finish well: worth a beep in a background tab

    def _fail(self, message: str) -> None:
        if self._watching:  # a failed background check is not worth an error: the list stays as it was
            self._watching = False
            self._status_done()
            return
        super()._fail(message)

    def action_open_in_browser(self) -> None:
        key = self._cursor_key()
        if key in self._items:
            self._open_link(self._ref(self._items[key]))

    @work(thread=True, group="link", exclusive=True)
    def _open_link(self, ref: RunRef) -> None:
        try:
            url = self.app.client.run_url(ref)
        except Exception as exc:
            self.app.call_from_thread(self.notify, f"Could not get the link: {exc}", severity="error")
            return
        self.app.call_from_thread(self.app.open_link, url)

    def _prune_selection(self) -> None:
        """A run that is gone from the list (deleted here or elsewhere) cannot stay selected."""
        kept = [k for k in self.selected if k in self._items]
        if kept != self.selected:
            self.selected = kept
            self.refresh_bindings()
            self._status_done()

    def _replace(self, items) -> None:
        before = {k: i.cells[1] for k, i in self._items.items()}
        super()._replace(items)
        if self._watching:
            self._watching = False
            self._announce_changes(before, items)
        self._prune_selection()

    def _finish(self) -> None:
        super()._finish()
        self._prune_selection()

    def action_compare(self) -> None:
        chosen = [self._items[k] for k in self.selected if k in self._items]
        if len(chosen) < 2:
            return
        if len(chosen) > MAX_RUNS:  # one palette color per run: more would have to repeat colors
            self.notify(f"At most {MAX_RUNS} runs can be compared at once ({len(chosen)} are selected).", severity="warning")
            return
        runs = [(self._ref(item), item.label) for item in chosen]
        self.app.push_screen(CompareScreen(self.entity, self.project, runs))

    @staticmethod
    def _cell(value):
        return plain(value) if isinstance(value, str) else value

    def _ref(self, item: Item) -> RunRef:
        # meta is the run's cache version; empty (not finished) means "don't cache"
        return RunRef(self.entity, self.project, item.key, item.meta or None)

    @staticmethod
    def _sort_value(item: Item, column: str):
        """The raw value of `column`. Rows cached before sorting existed carry none, so fall back
        to what their cells show (run time is then unknown and sorts last)."""
        index = SORT_FIELDS.index(column)
        if index < len(item.sort):
            return item.sort[index]
        cell = item.cells[{"name": 0, "status": 1, "started": 2, "group": 4, "tags": 5, "id": 6}.get(column, 3)]
        if column == "runtime":
            return None
        if column == "status":
            return status_rank(cell)
        return None if cell == "—" else cell

    def action_sort(self) -> None:
        self.app.push_screen(SortDialog(self.sort_state), self._on_sort)

    def _on_sort(self, state: SortState | None) -> None:
        if state is not None:
            self.set_sort(state)

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        """Clicking a column header sorts by it (again: reverses)."""
        event.stop()
        column = HEADER_TO_COLUMN.get(str(event.column_key.value))
        if column is not None:
            self.set_sort(self.sort_state.choose(column))

    def set_sort(self, state: SortState) -> None:
        self.sort_state = state
        self.app.preferences.set("runs_sort", state.to_json())
        self.resort()
        self._mark_headers()
        self._status_done()

    def _mark_headers(self) -> None:
        """Show ↑/↓ on the header of the column being sorted by."""
        table = self.query_one("#items", DataTable)
        for label, column in HEADER_TO_COLUMN.items():
            arrow = f" {'↓' if self.sort_state.descending else '↑'}" if column == self.sort_state.column else ""
            if label in [str(k.value) for k in table.columns]:
                table.columns[next(k for k in table.columns if str(k.value) == label)].label = plain(label + arrow)
        table.refresh(layout=True)

    def _is_deleted(self, run_id: str) -> bool:
        return (self.entity, self.project, run_id) in self.app.deleted_runs

    def fetch(self) -> Iterator[Item]:
        for r in self.app.client.iter_runs(self.entity, self.project):
            if self._is_deleted(r.id):  # the server may still list a run that was just deleted
                continue
            cells = (
                r.name,
                r.state or "—",
                fmt_time(r.created_at),
                fmt_duration(r.runtime_seconds()),
                r.group or "—",
                ", ".join(r.tags) or "—",
                r.id,
            )
            sort = (  # raw values, so run time sorts as a number and "—" cells count as empty
                r.name, status_rank(r.state), r.created_at or None, r.runtime_seconds(),
                r.group or None, ", ".join(r.tags) or None, r.id,
            )
            yield Item(r.id, cells, r.version or "", sort)

    def render_cells(self, item: Item) -> tuple[RenderableType, ...]:
        name, state, started, runtime, group, tags, run_id = item.cells
        if item.key in self.pinned or item.key in self.selected:
            name = Text.assemble(
                *([("★ ", "bold yellow")] if item.key in self.pinned else []),
                *([("✓ ", "bold green")] if item.key in self.selected else []),
                name,
            )
        status = Text("● " + state if state == "running" else state, style=STATE_STYLES.get(state, ""))
        return (name, status, started, Text(runtime, justify="right"), group, tags, run_id)

    @property
    def _cache_key(self) -> str:
        return f"runs:{self.entity}/{self.project}"

    def cached_items(self) -> list[Item] | None:
        items = items_from_cache(self.app.cache.get("list", self._cache_key), len(self.columns))
        return [i for i in items if not self._is_deleted(i.key)] if items else items

    def store_items(self, items: list[Item]) -> None:
        self.app.cache.set("list", self._cache_key, [i.to_json() for i in items])

    def open_item(self, item: Item) -> None:
        ref = self._ref(item)
        groups, tags = self.known_groups_and_tags()
        self.app.push_screen(RunScreen(ref, item.label, groups, tags))

    def known_groups_and_tags(self) -> tuple[list[str], list[str]]:
        """Groups and tags in use in this project (from the loaded run list)."""
        groups, tags = set(), set()
        for item in self._items.values():
            group, tag_text = item.cells[4], item.cells[5]
            if group != "—":
                groups.add(group)
            tags.update(t for t in tag_text.split(", ") if t and t != "—")
        return sorted(groups), sorted(tags)

    def _drop_deleted(self) -> None:
        """Take the runs deleted in this session off the screen at once (and out of the cached list),
        rather than when the next refresh has finished."""
        gone = [k for k in self._items if self._is_deleted(k)]
        if not gone:
            return
        table = self.query_one("#items", DataTable)
        for key in gone:
            del self._items[key]
            try:
                table.remove_row(key)
            except RowDoesNotExist:  # not shown (the filter hides it)
                pass
        self._count = len(self._items)
        self.store_items(list(self._items.values())[:CACHE_LIMIT])
        self._prune_selection()
        self._status_done()

    def on_screen_resume(self) -> None:
        """Back from a run that was edited or deleted: refresh the list in place."""
        self._drop_deleted()
        key = (self.entity, self.project)
        if key in self.app.touched_projects:
            self.app.touched_projects.discard(key)
            self.refresh_in_place()

    def action_reload(self) -> None:
        self.reload()  # bypasses the cache
