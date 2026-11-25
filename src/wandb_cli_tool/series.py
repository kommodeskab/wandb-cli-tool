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


def batches(items: list, size: int) -> list[list]:
    """`items` in consecutive groups of at most `size` (the order is kept)."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def smooth_ema(points: list[Point], weight: float) -> list[Point]:
    """Exponential moving average of the y values, the way W&B draws "smoothing": each value is
    mixed with the running average (weight 0.9 = 90% old), corrected for the start-up bias so the
    first points are not dragged towards zero. `weight` 0 returns the points unchanged."""
    if weight <= 0:
        return list(points)
    out: list[Point] = []
    average, steps = 0.0, 0
    for x, y in points:
        if not math.isfinite(y):
            continue
        steps += 1
        average = average * weight + (1 - weight) * y
        out.append((x, average / (1 - weight**steps)))
    return out


def prepare_points(points: list[Point], opts) -> tuple[list[Point], int]:
    """The points as a plot with `opts` draws them, and how many had to be dropped because a log
    axis cannot show zero or negative values."""
    shown = smooth_ema(points, opts.smooth)
    kept = [p for p in shown if (not opts.logx or p[0] > 0) and (not opts.logy or p[1] > 0)]
    return kept, len(shown) - len(kept)
