"""Small on-disk JSON cache (private to the user, safe to delete at any time)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from platformdirs import user_cache_dir

from ..jsonfile import atomic_write_json

CACHE_DIR_ENV = "WANDB_CLI_TOOL_CACHE_DIR"
PRUNE_EVERY = 50  # writes between size checks


def cache_dir() -> Path:
    return Path(os.environ.get(CACHE_DIR_ENV) or user_cache_dir("wandb-cli-tool"))


class DiskCache:
    """`get`/`set` JSON-serialisable values under a (namespace, key). Never raises."""

    def __init__(self, root: Path | None = None, max_entries: int = 2000) -> None:
        self.root = root if root is not None else cache_dir()
        self.max_entries = max_entries
        self._writes = 0
        self._dir_prunes = 0

    def _path(self, namespace: str, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()[:40]
        return self.root / namespace / f"{digest}.json"

    def get(self, namespace: str, key: str) -> Any | None:
        """The stored value, or None if missing/unreadable."""
        try:
            data = json.loads(self._path(namespace, key).read_text())
            return data["value"] if data["key"] == key else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def set(self, namespace: str, key: str, value: Any) -> None:
        if not atomic_write_json(self._path(namespace, key), {"key": key, "value": value}, dir_mode=0o700):
            return
        self._writes += 1
        if self._writes % PRUNE_EVERY == 0:
            self.prune()

    def prune(self) -> None:
        """Drop the oldest entries beyond `max_entries`."""
        try:
            files = sorted(self.root.rglob("*.json"), key=lambda p: p.stat().st_mtime)
            for path in files[: max(0, len(files) - self.max_entries)]:
                path.unlink(missing_ok=True)
        except OSError:
            pass

    def prune_dir(self, namespace: str, max_bytes: int) -> None:
        """Delete the oldest entries (files or folders) of `namespace` until it fits."""
        base = self.root / namespace
        try:
            entries = sorted(base.iterdir(), key=lambda p: p.stat().st_mtime)
            sizes = {
                e: sum(f.stat().st_size for f in e.rglob("*") if f.is_file()) if e.is_dir()
                else e.stat().st_size
                for e in entries
            }
            total = sum(sizes.values())
            for entry in entries:
                if total <= max_bytes:
                    break
                shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink(missing_ok=True)
                total -= sizes[entry]
        except OSError:
            pass

    def prune_dir_sometimes(self, namespace: str, max_bytes: int) -> None:
        """`prune_dir`, but only on the first call and then every PRUNE_EVERY-th one: it walks and
        stats the whole folder, which is wasteful to repeat before every single download."""
        if self._dir_prunes % PRUNE_EVERY == 0:
            self.prune_dir(namespace, max_bytes)
        self._dir_prunes += 1

    def clear(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
