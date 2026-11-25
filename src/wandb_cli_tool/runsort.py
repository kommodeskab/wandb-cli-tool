"""Sorting the runs list. UI-free.

Sort values come from the raw run data (run time as seconds, not "2h 41m"), names sort
"naturally" (run-2 before run-10), empty values always go last, and ties keep the order the
server gave (most recently active first), so sorts are stable and predictable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Sequence, TypeVar

T = TypeVar("T")

# (column id, label shown to the user). "activity" is the order the server delivers.
SORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("activity", "Last activity"),
    ("name", "Name"),
    ("status", "Status"),
    ("started", "Started"),
    ("runtime", "Run time"),
    ("group", "Group"),
    ("tags", "Tags"),
    ("id", "ID"),
)
COLUMN_LABELS = dict(SORT_COLUMNS)
# The runs table's column headers -> sort column (clicking a header sorts by it)
HEADER_TO_COLUMN = {
    "Run": "name", "Status": "status", "Started": "started", "Run time": "runtime",
    "Group": "group", "Tags": "tags", "ID": "id",
}
SORT_FIELDS = ("name", "status", "started", "runtime", "group", "tags", "id")  # order in Item.sort
# Columns whose natural first direction is "largest / newest first"
DEFAULT_DESCENDING = frozenset({"activity", "started", "runtime"})
STATUS_ORDER = {"running": 0, "pending": 1, "finished": 2, "failed": 3, "crashed": 4, "killed": 5}


@dataclass(frozen=True)
class SortState:
    column: str = "activity"
    descending: bool = True  # activity descending = newest first = the server's own order

    @property
    def is_server_order(self) -> bool:
        return self.column == "activity" and self.descending

    @property
    def label(self) -> str:
        return f"{COLUMN_LABELS[self.column]} {'↓' if self.descending else '↑'}"

    def choose(self, column: str) -> "SortState":
        """What picking `column` does: the same column flips direction, a new one starts in
        its natural direction."""
        if column == self.column:
            return SortState(column, not self.descending)
        return SortState(column, column in DEFAULT_DESCENDING)

    def to_json(self) -> dict[str, Any]:
        return {"column": self.column, "descending": self.descending}

    @staticmethod
    def from_json(data: Any) -> "SortState":
        if isinstance(data, dict) and data.get("column") in COLUMN_LABELS and isinstance(data.get("descending"), bool):
            return SortState(data["column"], data["descending"])
        return SortState()


def natural_key(text: str) -> tuple[tuple[int, Any], ...]:
    """Case-insensitive key where digit runs compare as numbers: run-2 < run-10."""
    parts = re.split(r"(\d+)", text.casefold())
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts if p != "")


def status_rank(state: str) -> int:
    return STATUS_ORDER.get(state, len(STATUS_ORDER))


def _key(column: str, value: Any) -> Any:
    if column in ("runtime", "status"):
        return value
    return natural_key(str(value))


def sort_items(items: Sequence[T], state: SortState, value_of: Callable[[T, str], Any]) -> list[T]:
    """`items` in the user's order. `value_of(item, column)` returns the raw sort value of an
    item (None/"" = empty). `items` must be in server order (newest activity first)."""
    ordered = list(items)
    if state.column == "activity":
        return ordered if state.descending else ordered[::-1]
    present: list[tuple[Any, T]] = []
    empty: list[T] = []
    for item in ordered:
        value = value_of(item, state.column)
        if value is None or value == "":
            empty.append(item)
        else:
            present.append((_key(state.column, value), item))
    present.sort(key=lambda pair: pair[0], reverse=state.descending)  # stable either way
    return [item for _, item in present] + empty
