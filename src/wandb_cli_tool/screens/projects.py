from __future__ import annotations

from typing import TYPE_CHECKING, Iterator

from rich.console import RenderableType
from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable

from ..formatting import fmt_count, fmt_time
from ..pins import Pins, pinned_first
from .browse import BrowseScreen, Item, items_from_cache
from .runs import RunsScreen

if TYPE_CHECKING:
    from ..app import WandbCliApp


class ProjectsScreen(BrowseScreen):
    BINDINGS = [
        Binding("r", "reload", "Refresh"),
        Binding("p", "pin", "Pin"),
        Binding("e", "app.change_entity", "Change entity"),
        Binding("l", "app.logout", "Logout"),
        Binding("q", "app.quit", "Quit"),
    ]
    columns = ("Project", "Last run", "Runs", "Active")
    empty_text = "No projects for this entity."
    app: "WandbCliApp"

    def __init__(self, entity: str) -> None:
        super().__init__()
        self.entity = entity
        self.title_text = f"Projects — {entity}"
        self.pinned: list[str] = []

    def prepare(self) -> None:
        self.pins = Pins(self.app.preferences, self.entity)
        self.pinned = self.pins.get("projects")

    def display_order(self, items: list[Item]) -> list[Item]:
        return pinned_first(items, self.pinned, lambda i: i.key)

    def has_custom_order(self) -> bool:
        return bool(self.pinned)

    def fetch(self) -> Iterator[Item]:
        for p in self.app.client.iter_projects(self.entity):
            yield Item(
                p.name,
                (p.name, fmt_time(p.last_run_at), fmt_count(p.run_count), fmt_count(p.active_runs)),
            )

    def render_cells(self, item: Item) -> tuple[RenderableType, ...]:
        name, last, runs, active = item.cells
        busy = active.isdigit() and int(active) > 0
        if item.key in self.pinned:
            name = Text.assemble(("★ ", "bold yellow"), name)
        return (
            name,
            last,
            Text(runs, justify="right"),
            Text(active, justify="right", style="bold green" if busy else ""),
        )

    def action_pin(self) -> None:
        """Pin the project under the cursor to the top of the list; again unpins."""
        key = self._cursor_key()
        if key is None or key not in self._items:
            return
        table = self.query_one("#items", DataTable)
        row = table.cursor_row
        self.pins.toggle("projects", key)
        self.pinned = self.pins.get("projects")
        self.resort()
        if table.row_count:  # the pinned project jumps up; keep the cursor on its row number
            table.move_cursor(row=min(row, table.row_count - 1), animate=False)

    @property
    def _cache_key(self) -> str:
        return f"projects:{self.entity}"

    def cached_items(self) -> list[Item] | None:
        return items_from_cache(
            self.app.cache.get("list", self._cache_key), len(self.columns)
        )

    def store_items(self, items: list[Item]) -> None:
        self.app.cache.set("list", self._cache_key, [i.to_json() for i in items])

    def open_item(self, item: Item) -> None:
        self.app.push_screen(RunsScreen(self.entity, item.key))

    def action_reload(self) -> None:
        self.reload()  # bypasses the cache

