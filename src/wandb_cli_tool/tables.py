"""wandb.Table support: finding the tables a run logged and turning one into display rows.

UI-free. A logged table appears in the run summary as
{"_type": "table-file", "path": "media/table/<key>_<step>_<hash>.table.json", "nrows": .., "ncols": ..}
and the file holds {"columns": [...], "data": [[...], ...]}.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Mapping

MAX_TABLE_ROWS = 1000  # rows kept for display
MAX_TABLE_COLS = 60
MAX_CELL_CHARS = 80
MAX_TABLE_BYTES = 20 * 1024 * 1024  # larger table files are not downloaded for display


@dataclass(frozen=True)
class TableInfo:
    key: str  # the name it was logged under
    path: str  # file in the run's files, e.g. media/table/preds_5_ab12.table.json
    rows: int | None = None
    cols: int | None = None

    @property
    def label(self) -> str:
        shape = f"{self.rows:,} rows × {self.cols} cols" if self.rows is not None and self.cols is not None else "size unknown"
        return f"{self.key}  ({shape})"


@dataclass(frozen=True)
class TableData:
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]  # already formatted for display
    total_rows: int  # rows in the table, may exceed len(rows)
    total_cols: int


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def table_entries(summary: Mapping[str, Any]) -> list[TableInfo]:
    """The tables a run logged (latest version of each key), sorted by key."""
    found = []
    for key, value in summary.items():
        if (
            isinstance(value, dict)
            and value.get("_type") == "table-file"
            and isinstance(value.get("path"), str)
            and value["path"].endswith(".table.json")
        ):
            found.append(TableInfo(str(key), value["path"], _int_or_none(value.get("nrows")), _int_or_none(value.get("ncols"))))
    return sorted(found, key=lambda t: t.key)


def _clip(text: str) -> str:
    text = " ".join(text.split())  # one line
    return text if len(text) <= MAX_CELL_CHARS else text[: MAX_CELL_CHARS - 1] + "…"


def format_cell(value: Any) -> str:
    """A table cell as short single-line text. Media cells become a marker with the file name."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return "NaN" if math.isnan(value) else f"{value:.6g}"
    if isinstance(value, str):
        return _clip(value)
    if isinstance(value, dict) and isinstance(value.get("_type"), str):
        kind = value["_type"].removesuffix("-file")
        path = value.get("path")
        return _clip(f"[{kind}] {path.rsplit('/', 1)[-1]}" if isinstance(path, str) else f"[{kind}]")
    try:
        return _clip(json.dumps(value, separators=(",", ":"), default=str))
    except (TypeError, ValueError):
        return _clip(str(value))


def parse_table(raw: Any) -> TableData:
    """Turn the contents of a .table.json file into display data. Raises ValueError if it
    is not a table."""
    if not isinstance(raw, dict) or not isinstance(raw.get("columns"), list) or not isinstance(raw.get("data"), list):
        raise ValueError("This file is not a wandb.Table (no columns/data).")
    all_columns = [str(c) for c in raw["columns"]]
    columns = all_columns[:MAX_TABLE_COLS]
    rows = []
    for row in raw["data"][:MAX_TABLE_ROWS]:
        cells = row if isinstance(row, list) else [row]
        rows.append(tuple(format_cell(cells[i]) if i < len(cells) else "" for i in range(len(columns))))
    return TableData(tuple(columns), tuple(rows), len(raw["data"]), len(all_columns))
