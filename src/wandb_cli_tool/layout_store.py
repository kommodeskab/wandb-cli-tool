"""Saves the user's chart customizations per PROJECT, in the user config directory.

Every run of a project shows the same customizations (panels, added and deleted charts,
x-axis choices) on top of the default charts generated from that run's own metrics.

This is user preference data, not a cache: it is kept across logouts and cache clears. While the
tool is in development the format may change; files with another LAYOUT_VERSION (or that cannot
be read) are simply ignored and overwritten, so a stale file can never break the app.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import config
from .charts import LAYOUT_VERSION, Customization
from .jsonfile import atomic_write_json, read_versioned

FILE_NAME = "chart_layouts.json"
MAX_PROJECTS = 200  # oldest layouts are dropped beyond this


def project_key(entity: str, project: str) -> str:
    return f"{entity}/{project}"


class LayoutStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        # resolved on use so WANDB_CLI_TOOL_CONFIG_DIR (tests) is honoured
        return self._path if self._path is not None else config.config_path().parent / FILE_NAME

    def _read_projects(self) -> dict[str, dict]:
        stored = read_versioned(self.path, LAYOUT_VERSION, "projects")
        return {k: v for k, v in stored.items() if isinstance(v, dict)}

    def load(self, key: str) -> Customization:
        """The saved customizations for a project (empty if none, or if the file is unusable)."""
        return Customization.from_json(self._read_projects().get(key))

    def save(self, key: str, custom: Customization) -> bool:
        """Save (or, if nothing is customized, forget) a project's customizations.
        Returns False if the file could not be written; never raises."""
        projects = self._read_projects()
        if custom.is_empty:
            if key not in projects:
                return True  # nothing to forget: do not even create the file
            del projects[key]
        else:
            projects[key] = {**custom.to_json(), "saved_at": time.time()}
        newest = sorted(projects.items(), key=lambda kv: kv[1].get("saved_at", 0), reverse=True)[:MAX_PROJECTS]
        return self._write(dict(newest))

    def reset(self, key: str) -> bool:
        return self.save(key, Customization())

    def _write(self, projects: dict[str, dict]) -> bool:
        return atomic_write_json(self.path, {"version": LAYOUT_VERSION, "projects": projects})
