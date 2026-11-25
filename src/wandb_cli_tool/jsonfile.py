"""Small helpers for the JSON files the app keeps (preferences, chart layouts, cache entries)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def read_versioned(path: Path, version: int, field: str) -> dict[str, Any]:
    """`{"version": N, field: {...}}` -> that inner dict, or {} if the file is missing, damaged,
    in another format version, or has no such field. Never raises."""
    try:
        data = json.loads(path.read_text())
        if data.get("version") == version and isinstance(data.get(field), dict):
            return dict(data[field])
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def atomic_write_json(path: Path, payload: Any, dir_mode: int | None = None) -> bool:
    """Write `payload` as JSON so a crash never leaves a half-written file (temp file, then
    rename), readable only by the owner. Returns False if it could not be written; never raises."""
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        if dir_mode is None:
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True, mode=dir_mode)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # e.g. the parent is not a directory: nothing to clean up
            pass
        return False
