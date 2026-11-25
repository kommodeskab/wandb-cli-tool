from __future__ import annotations

from typing import TYPE_CHECKING, Iterator

from rich.console import RenderableType
from rich.text import Text
from textual.binding import Binding

from ..formatting import fmt_count, fmt_time
from .browse import BrowseScreen, Item, items_from_cache
from .runs import RunsScreen

if TYPE_CHECKING:
    from ..app import WandbCliApp


class ProjectsScreen(BrowseScreen):
    BINDINGS = [Binding("r", "reload", "Refresh")]
    columns = ("Project", "Last run", "Runs", "Active")
    empty_text = "No projects for this entity."
    app: "WandbCliApp"

    def __init__(self, entity: str) -> None:
        super().__init__()
        self.entity = entity
        self.title_text = f"Projects — {entity}"

    def fetch(self) -> Iterator[Item]:
        for p in self.app.client.iter_projects(self.entity):
            yield Item(
                p.name,
                (p.name, fmt_time(p.last_run_at), fmt_count(p.run_count), fmt_count(p.active_runs)),
            )

    def render_cells(self, item: Item) -> tuple[RenderableType, ...]:
        name, last, runs, active = item.cells
        busy = active.isdigit() and int(active) > 0
        return (
            name,
            last,
            Text(runs, justify="right"),
            Text(active, justify="right", style="bold green" if busy else ""),
        )

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

