"""Series colors for charts: the dataviz skill's reference categorical palette, unchanged.

Rules that go with it (see the dataviz skill): hues are assigned in this fixed order and
never cycled (so at most MAX_SERIES per plot); a series keeps its color for as long as it
is in the chart (the slot is its position in the chart, not a rank among visible series);
one series or one bar set uses slot 1. Dark values are stepped for dark surfaces, not an
automatic flip.
"""

from __future__ import annotations

SERIES_DARK = ("#338ced", "#e76624", "#00a888", "#d28e05", "#e24d8b", "#0aac23", "#9887ef", "#eb6263")
SERIES_LIGHT = ("#257de0", "#f47230", "#00b894", "#f2a600", "#ee7fac", "#008a17", "#6a4ce2", "#ec4647")
MAX_SERIES = len(SERIES_DARK)  # a 9th hue is never generated


def series_color(slot: int, dark: bool = True) -> str:
    """Hex color of series `slot` (0-based). Raises instead of cycling past the palette."""
    if not 0 <= slot < MAX_SERIES:
        raise ValueError(f"A chart can show at most {MAX_SERIES} series (asked for slot {slot + 1}).")
    return (SERIES_DARK if dark else SERIES_LIGHT)[slot]


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
