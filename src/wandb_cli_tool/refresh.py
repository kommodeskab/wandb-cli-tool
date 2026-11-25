"""Auto-refresh interval choices for the Charts screen. UI-free."""

from __future__ import annotations

from typing import Any

REFRESH_CHOICES: tuple[int | None, ...] = (None, 10, 30, 60, 300)  # seconds; None = off
DEFAULT_REFRESH = 30
PREF_KEY = "chart_refresh_seconds"  # stored as seconds, 0 meaning off


def label(seconds: float | None) -> str:
    if not seconds:
        return "off"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60)} min"
    return f"{seconds:g} s"


def next_choice(current: float | None) -> int | None:
    """The interval after `current` when cycling with the key (off -> 10 s -> 30 s -> ... -> off)."""
    if current in REFRESH_CHOICES:
        return REFRESH_CHOICES[(REFRESH_CHOICES.index(current) + 1) % len(REFRESH_CHOICES)]  # type: ignore[arg-type]
    larger = [c for c in REFRESH_CHOICES if c and current and c > current]
    return larger[0] if larger else None  # an unusual value moves on to the next step (or off)


def interval_from_pref(value: Any) -> int | None:
    """The saved interval, or the default if nothing valid is saved."""
    if value is None:
        return DEFAULT_REFRESH
    if isinstance(value, int) and not isinstance(value, bool):
        return None if value == 0 else (value if value in REFRESH_CHOICES else DEFAULT_REFRESH)
    return DEFAULT_REFRESH


def to_pref(seconds: float | None) -> int:
    return 0 if not seconds else int(seconds)
