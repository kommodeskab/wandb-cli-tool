from __future__ import annotations

from typing import TYPE_CHECKING, Iterator

from rich.console import RenderableType
from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable

from ..api import RunRef
from ..formatting import fmt_duration, fmt_time
from ..plain import plain
from ..runsort import (
    HEADER_TO_COLUMN, SORT_FIELDS, SortState, sort_items, status_rank,
)
from .browse import BrowseScreen, Item, items_from_cache
from .dialogs import SortDialog
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


class RunsScreen(BrowseScreen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("r", "reload", "Refresh"),
        Binding("s", "sort", "Sort"),
    ]
    columns = ("Run", "Status", "Started", "Run time", "Group", "Tags", "ID")
    empty_text = "No runs in this project."
    app: "WandbCliApp"

    def __init__(self, entity: str, project: str) -> None:
        super().__init__()
        self.entity = entity
        self.project = project
        self.title_text = f"Runs — {entity}/{project}"
        self.sort_state = SortState()

    def prepare(self) -> None:
        self.sort_state = SortState.from_json(self.app.preferences.get("runs_sort"))
        self._mark_headers()

    # ---- sorting ----------------------------------------------------------------------
    def display_order(self, items: list[Item]) -> list[Item]:
        return sort_items(items, self.sort_state, self._sort_value)

    def has_custom_order(self) -> bool:
        return not self.sort_state.is_server_order

    def status_suffix(self) -> str:
        return "" if self.sort_state.is_server_order else f"  ·  sorted by {self.sort_state.label}"

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

    def fetch(self) -> Iterator[Item]:
        for r in self.app.client.iter_runs(self.entity, self.project):
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
        status = Text("● " + state if state == "running" else state, style=STATE_STYLES.get(state, ""))
        return (name, status, started, Text(runtime, justify="right"), group, tags, run_id)

    @property
    def _cache_key(self) -> str:
        return f"runs:{self.entity}/{self.project}"

    def cached_items(self) -> list[Item] | None:
        return items_from_cache(
            self.app.cache.get("list", self._cache_key), len(self.columns)
        )

    def store_items(self, items: list[Item]) -> None:
        self.app.cache.set("list", self._cache_key, [i.to_json() for i in items])

    def open_item(self, item: Item) -> None:
        # meta is the run's cache version; empty (not finished) means "don't cache"
        ref = RunRef(self.entity, self.project, item.key, item.meta or None)
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

    def on_screen_resume(self) -> None:
        """Back from a run whose group/tags changed: refresh the list in place."""
        key = (self.entity, self.project)
        if key in self.app.touched_projects:
            self.app.touched_projects.discard(key)
            self.refresh_in_place()

    def action_reload(self) -> None:
        self.reload()  # bypasses the cache
