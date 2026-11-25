"""Pure helpers for turning W&B history rows into plottable points."""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

Point = tuple[float, float]


def is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def extract_points(rows: Iterable[Mapping[str, Any]], x: str, y: str) -> list[Point]:
    """(x, y) pairs from rows that contain a finite number for both keys."""
    return [
        (float(row[x]), float(row[y]))
        for row in rows
        if is_number(row.get(x)) and is_number(row.get(y))
    ]


def plottable_keys(values: Mapping[str, Any]) -> list[str]:
    """Sorted user-metric names whose value is a plain number (no media/histograms)."""
    return sorted(k for k, v in values.items() if not k.startswith("_") and is_number(v))


def numeric_values(values: Mapping[str, Any]) -> dict[str, float]:
    """Plain-number entries of a summary (the latest value of each metric)."""
    return {k: float(v) for k, v in values.items() if not k.startswith("_") and is_number(v)}


def last_values(rows: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """The most recent numeric value of every non-internal key across history rows."""
    out: dict[str, float] = {}
    for row in rows:
        for k, v in row.items():
            if not k.startswith("_") and is_number(v):
                out[k] = float(v)
    return out
