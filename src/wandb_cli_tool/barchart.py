"""Layout of a horizontal bar chart in character cells. UI-free.

One row per value, in the order given: name, bar, figure. All bars share one baseline; values
below zero grow to the left of it. Bars use eighth-block glyphs for sub-cell precision.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

PARTIAL = " ▏▎▍▌▋▊▉"  # index = eighths of a cell; a full cell is "█"
MAX_NAME_WIDTH = 24
GAP = 2


@dataclass(frozen=True)
class BarRow:
    name: str  # already clipped/padded to the name column
    figure: str  # the value as text, right-aligned to the figure column
    negative: str  # bar glyphs left of the baseline, right-aligned in the negative area
    positive: str  # bar glyphs right of the baseline, left-aligned in the positive area


@dataclass(frozen=True)
class BarLayout:
    rows: list[BarRow]
    negative_width: int  # cells left of the baseline (0 when no value is negative)
    positive_width: int


def format_figure(value: float | None) -> str:
    return "—" if value is None or not math.isfinite(value) else f"{value:.6g}"


def clip(name: str, width: int) -> str:
    return name if len(name) <= width else name[: max(width - 1, 1)] + "…"


def _glyphs(eighths: int) -> str:
    return "█" * (eighths // 8) + PARTIAL[eighths % 8].strip()


def layout_bars(items: list[tuple[str, float | None]], width: int) -> BarLayout:
    """Lay out `items` (name, value) in `width` cells. Missing values get a figure of '—' and no bar."""
    values = [v for _, v in items if v is not None and math.isfinite(v)]
    name_w = min(max((len(n) for n, _ in items), default=1), MAX_NAME_WIDTH, max(width // 3, 8))
    figures = [format_figure(v) for _, v in items]
    fig_w = max((len(f) for f in figures), default=1)
    bar_total = max(width - name_w - fig_w - 2 * GAP, 8)

    lo, hi = min([0.0, *values]), max([0.0, *values])
    span = hi - lo
    has_negative = lo < 0
    area = bar_total - (1 if has_negative else 0)  # the baseline takes one cell
    if span == 0:
        pos_w, neg_w = area, 0
    else:
        pos_w = round(area * hi / span) if hi > 0 else 0
        neg_w = area - pos_w if has_negative else 0
        if hi > 0 and pos_w == 0:
            pos_w, neg_w = 1, neg_w - 1
        if has_negative and neg_w == 0:
            neg_w, pos_w = 1, pos_w - 1

    rows = []
    for (name, value), figure in zip(items, figures):
        pos = neg = ""
        if value is not None and math.isfinite(value) and value != 0 and span:
            if value > 0:
                pos = _glyphs(max(1, round(value / hi * pos_w * 8)))
            else:
                neg = "█" * max(1, round(-value / -lo * neg_w))  # whole cells: no left-growing partial glyphs
        rows.append(BarRow(clip(name, name_w).ljust(name_w), figure.rjust(fig_w), neg, pos))
    return BarLayout(rows, neg_w, pos_w)
