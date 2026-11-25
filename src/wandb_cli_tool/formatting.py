"""Display formatting for list columns. Pure functions."""

from __future__ import annotations

from datetime import datetime, timezone


def parse_time(value: object) -> datetime | None:
    """W&B timestamps are naive ISO-8601 in UTC (sometimes with a trailing Z)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).rstrip("Z"))
    except ValueError:
        return None


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fmt_time(value: object) -> str:
    """UTC timestamp -> local 'YYYY-MM-DD HH:MM', or an em dash if unknown."""
    dt = parse_time(value)
    if dt is None:
        return "—"
    return dt.replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def fmt_duration(seconds: float | None) -> str:
    """'45s', '12m 05s', '3h 07m', '2d 4h'. Two units at most."""
    if seconds is None:
        return "—"
    s = max(0, int(seconds))
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, s = divmod(s, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {s:02d}s"
    return f"{s}s"


def fmt_count(n: int | None) -> str:
    return "—" if n is None else str(n)


def fmt_size(n: int | None) -> str:
    """Bytes as '812 B', '1.5 KB', '3.2 MB'..."""
    if n is None:
        return "—"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"
