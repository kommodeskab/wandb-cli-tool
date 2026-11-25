"""Base screen: a keyboard-navigable table filled by a background worker.

With a disk cache the last known list is shown immediately while fresh data loads in
the background; when the fresh list is complete it replaces the cached one.
"""

from __future__ import annotations

from typing import Any, Iterator, NamedTuple

from rich.console import RenderableType
from textual import work
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Header, Static
from textual.widgets.data_table import RowDoesNotExist

from .keybar import KeyBar
from ..plain import plain
from textual.worker import get_current_worker

BATCH_SIZE = 50
CACHE_LIMIT = 500  # items remembered per list


class Item(NamedTuple):
    key: str  # unique identifier (e.g. a run id)
    cells: tuple[str, ...]  # one plain string per column
    meta: str = ""  # extra payload, not shown (e.g. a run's cache version)
    sort: tuple = ()  # raw values to sort by (numbers stay numbers), if the list is sortable

    @property
    def label(self) -> str:
        return self.cells[0]

    def to_json(self) -> list[Any]:
        return [self.key, list(self.cells), self.meta, list(self.sort)]

    @staticmethod
    def from_json(row: Any, columns: int) -> "Item | None":
        try:
            key, cells, meta, *rest = row  # lists cached before sorting existed have no 4th part
            sort = rest[0] if rest else []
            if (
                isinstance(key, str)
                and isinstance(meta, str)
                and isinstance(cells, list)
                and len(cells) == columns
                and isinstance(sort, list)
            ):
                return Item(key, tuple(str(c) for c in cells), meta, tuple(sort))
        except (TypeError, ValueError):
            pass
        return None


class BrowseScreen(Screen):
    """Subclasses set `title_text`/`columns`, implement `fetch()`/`open_item()`, and may
    implement `cached_items()`/`store_items()` to enable the list cache."""

    title_text = ""
    columns: tuple[str, ...] = ("Name",)
    empty_text = "Nothing here."

    def __init__(self) -> None:
        super().__init__()
        self._count = 0
        self._items: dict[str, Item] = {}  # items currently in the table, by key
        self._showing_cache = False

    def fetch(self) -> Iterator[Item]:
        raise NotImplementedError

    def open_item(self, item: Item) -> None:
        raise NotImplementedError

    def cached_items(self) -> list[Item] | None:
        return None

    def store_items(self, items: list[Item]) -> None:
        pass

    def render_cells(self, item: Item) -> tuple[RenderableType, ...]:
        """Cells as displayed (override to add styling)."""
        return item.cells

    # ---- ordering (a subclass with a sort order overrides these three) ----------------
    def display_order(self, items: list[Item]) -> list[Item]:
        """`items` (in the order they arrived) in the order they should be shown."""
        return items

    def has_custom_order(self) -> bool:
        """False while the arrival order is what is shown (so no re-ordering is needed)."""
        return False

    def status_suffix(self) -> str:
        return ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Loading…", id="status")
        yield DataTable(id="items", cursor_type="row", zebra_stripes=True)
        yield KeyBar()

    def on_mount(self) -> None:
        self.title = self.title_text
        table = self.query_one("#items", DataTable)
        for label in self.columns:
            table.add_column(plain(label), key=label)  # keyed by label, so a header click says which
        self.prepare()
        table.focus()
        self.reload(use_cache=True)

    def prepare(self) -> None:
        """Hook: the columns exist and nothing is loaded yet. (Do not override on_mount in a
        subclass and call super(): Textual already runs every class's on_mount.)"""

    def reload(self, use_cache: bool = False) -> None:
        """Refetch. Without `use_cache` the table is cleared and streams in fresh."""
        self._reset_table()
        cached = self.cached_items() if use_cache else None
        self._showing_cache = bool(cached)
        if cached:
            self._show_all(cached)
            self._status(f"{len(cached)} items (cached), refreshing…")
        else:
            self._status("Loading…")
        self.load(self._showing_cache)

    def refresh_in_place(self) -> None:
        """Reload while keeping the current rows on screen (and the highlighted one)."""
        self._showing_cache = True
        self._status("Refreshing…")
        self.load(True)

    def _reset_table(self) -> None:
        self._count = 0
        self._items.clear()
        self.query_one("#items", DataTable).clear()

    def _cursor_key(self) -> str | None:
        table = self.query_one("#items", DataTable)
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def _restore_cursor(self, key: str | None) -> None:
        if key is None:
            return
        try:
            self.query_one("#items", DataTable).move_cursor(row=self.query_one("#items", DataTable).get_row_index(key), animate=False)
        except RowDoesNotExist:
            pass

    def _apply_order(self) -> None:
        """Show the rows in the user's order (only needed when it differs from arrival order)."""
        if self.has_custom_order():
            self.resort()

    def resort(self) -> None:
        """Re-draw the table in the current order, keeping the highlighted row highlighted."""
        table = self.query_one("#items", DataTable)
        key = self._cursor_key()
        table.clear()
        for item in self.display_order(list(self._items.values())):
            self._add_row(item)
        self._restore_cursor(key)

    def _status(self, text: str) -> None:
        self.query_one("#status", Static).update(plain(text))

    @work(thread=True, exclusive=True)
    def load(self, buffer: bool) -> None:
        """`buffer`: keep the displayed (cached) list until the fresh one is complete."""
        worker = get_current_worker()
        fresh: list[Item] = []
        seen: set[str] = set()
        batch: list[Item] = []
        try:
            for item in self.fetch():
                if worker.is_cancelled:
                    return
                if item.key in seen:  # live data can shift between pages
                    continue
                seen.add(item.key)
                fresh.append(item)
                if buffer:
                    continue
                batch.append(item)
                if len(batch) >= BATCH_SIZE:
                    self.app.call_from_thread(self._append, batch)
                    batch = []
            if worker.is_cancelled:
                return
            self.store_items(fresh[:CACHE_LIMIT])
            if buffer:
                self.app.call_from_thread(self._replace, fresh)
            else:
                if batch:
                    self.app.call_from_thread(self._append, batch)
                self.app.call_from_thread(self._finish)
        except Exception as exc:  # network/auth errors surface in the UI
            if not worker.is_cancelled:
                self.app.call_from_thread(self._fail, str(exc))

    def _add_row(self, item: Item) -> None:
        cells = [plain(c) if isinstance(c, str) else c for c in self.render_cells(item)]
        self.query_one("#items", DataTable).add_row(*cells, key=item.key)

    def _append(self, items: list[Item], draw: bool = True) -> None:
        for item in items:
            if item.key in self._items:
                continue
            self._items[item.key] = item
            if draw:
                self._add_row(item)
        self._count = len(self._items)
        if not self._showing_cache:
            self._status(f"Loading… {self._count} so far")

    def _show_all(self, items: list[Item]) -> None:
        """Show a complete list in the user's order, drawing each row once (rather than in
        arrival order first and then again sorted)."""
        custom = self.has_custom_order()
        self._append(items, draw=not custom)
        if custom:
            self.resort()

    def _replace(self, items: list[Item]) -> None:
        """Swap the cached list for the fresh one, keeping the highlighted entry."""
        key = self._cursor_key()
        self._reset_table()
        self._showing_cache = False
        self._show_all(items)
        self._restore_cursor(key)
        self._status_done()

    def _finish(self) -> None:
        self._apply_order()  # a list that streamed in is re-ordered once, when complete
        self._status_done()

    def _status_done(self) -> None:
        self._status(f"{self._count} items{self.status_suffix()}" if self._count else self.empty_text)

    def _fail(self, message: str) -> None:
        suffix = " (showing cached list)" if self._showing_cache else ""
        self._status(f"Error: {message}{suffix}")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        item = self._items.get(str(event.row_key.value))
        if item is not None:
            self.open_item(item)


def items_from_cache(data: Any, columns: int) -> list[Item] | None:
    """Rebuild cached items; rows in an older/unknown layout are ignored."""
    items = [i for i in (Item.from_json(r, columns) for r in data or []) if i]
    return items or None
