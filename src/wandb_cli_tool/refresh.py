"""The charts' automatic refresh interval. UI-free."""

from __future__ import annotations

AUTO_REFRESH_SECONDS = 30  # a running run's charts refresh this often; not configurable


def label(seconds: float | None) -> str:
    if not seconds:
        return "off"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60)} min"
    return f"{seconds:g} s"
