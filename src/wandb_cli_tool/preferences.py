"""Small persistent user preferences (sort order, refresh interval, ...) in the config dir.

Like the chart layouts this is settings data, not a cache, and while the tool is in development
its format may change: a file with another PREFS_VERSION (or one that cannot be read) is simply
ignored and replaced, never an error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import config
from .jsonfile import atomic_write_json, read_versioned

PREFS_VERSION = 1
FILE_NAME = "preferences.json"


class Preferences:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else config.config_path().parent / FILE_NAME

    def get(self, key: str, default: Any = None) -> Any:
        return read_versioned(self.path, PREFS_VERSION, "values").get(key, default)

    def set(self, key: str, value: Any) -> bool:
        """Save one preference. Returns False if it could not be written; never raises."""
        values = read_versioned(self.path, PREFS_VERSION, "values")
        values[key] = value
        return atomic_write_json(self.path, {"version": PREFS_VERSION, "values": values})
