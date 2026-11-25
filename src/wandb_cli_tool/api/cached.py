"""Transparent caching of slow, immutable run data in front of any WandbClient."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterator

from ..series import Point
from ..images import ImageInfo, ImageSeries, ImageStep
from ..tables import TableData, TableInfo
from .cache import DiskCache
from .client import WandbClient
from .copy_run import CopyJob
from .models import CopyEstimate, FileInfo, ProjectInfo, RunInfo, RunMetadata, RunRef

NAMESPACE = "run"


class CachedClient:
    """Caches chart data (metric keys, series, system rows) of *finished* runs.

    Runs without a `version` (still running, or state unknown) always go to W&B.
    Everything else is passed straight through; logs are streamed and not cached.
    """

    def __init__(self, inner: WandbClient, cache: DiskCache) -> None:
        self._inner = inner
        self._cache = cache

    def list_entities(self) -> list[str]:
        return self._inner.list_entities()

    def iter_projects(self, entity: str) -> Iterator[ProjectInfo]:
        return self._inner.iter_projects(entity)

    def iter_runs(self, entity: str, project: str) -> Iterator[RunInfo]:
        return self._inner.iter_runs(entity, project)

    def console_lines(self, ref: RunRef) -> Iterator[str]:
        return self._inner.console_lines(ref)

    # Not cached: tags/group change without the run changing, files are listed live.
    def run_details(self, ref: RunRef) -> RunInfo:
        return self._inner.run_details(ref)

    def iter_files(self, ref: RunRef) -> Iterator[FileInfo]:
        return self._inner.iter_files(ref)

    def download_file(self, ref: RunRef, name: str, dest: Path) -> Path:
        return self._inner.download_file(ref, name, dest)

    def set_group(self, ref: RunRef, group: str) -> None:
        self._inner.set_group(ref, group)

    def add_tags(self, ref: RunRef, tags: list[str]) -> list[str]:
        return self._inner.add_tags(ref, tags)

    def rename_run(self, ref: RunRef, name: str) -> str:
        return self._inner.rename_run(ref, name)

    def copy_estimate(self, ref: RunRef) -> CopyEstimate:
        return self._inner.copy_estimate(ref)

    def start_copy(self, ref: RunRef) -> CopyJob:
        return self._inner.start_copy(ref)

    def run_metadata(self, ref: RunRef) -> RunMetadata:
        data = self._memo(ref, "metadata", lambda: asdict(self._inner.run_metadata(ref)))
        return RunMetadata(**data)

    def run_config(self, ref: RunRef) -> dict[str, Any]:
        return self._memo(ref, "config", lambda: self._inner.run_config(ref))

    def _memo(self, ref: RunRef, name: str, fetch: Callable[[], Any]) -> Any:
        if ref.version is None:
            return fetch()
        key = f"{ref.path}@{ref.version}:{name}"
        hit = self._cache.get(NAMESPACE, key)
        if hit is not None:
            return hit
        value = fetch()
        self._cache.set(NAMESPACE, key, value)
        return value

    def metric_keys(self, ref: RunRef) -> list[str]:
        return self._memo(ref, "keys", lambda: self._inner.metric_keys(ref))

    def metric_series(self, ref: RunRef, y: str, x: str) -> list[Point]:
        rows = self._memo(
            ref, f"series:{x}:{y}", lambda: self._inner.metric_series(ref, y, x)
        )
        return [(p[0], p[1]) for p in rows]  # JSON turns tuples into lists

    def metric_values(self, ref: RunRef) -> dict[str, float]:
        return self._memo(ref, "values", lambda: self._inner.metric_values(ref))

    def run_tables(self, ref: RunRef) -> list[TableInfo]:
        data = self._memo(ref, "tables", lambda: [asdict(t) for t in self._inner.run_tables(ref)])
        return [TableInfo(**d) for d in data]

    def run_images(self, ref: RunRef) -> list[ImageInfo]:
        data = self._memo(ref, "images", lambda: [asdict(i) for i in self._inner.run_images(ref)])
        return [ImageInfo(**d) for d in data]

    def image_steps(self, ref: RunRef, key: str) -> ImageSeries:
        def fetch():
            series = self._inner.image_steps(ref, key)
            return {"steps": [asdict(s) for s in series.steps], "total": series.total}

        d = self._memo(ref, f"imgsteps:{key}", fetch)
        steps = tuple(
            ImageStep(s["step"], tuple(s["paths"]), tuple(s["sha256s"]), tuple(s["sizes"]), tuple(s["captions"]))
            for s in d["steps"]
        )
        return ImageSeries(steps, d["total"])

    def table_data(self, ref: RunRef, path: str) -> TableData:
        d = self._memo(ref, f"table:{path}", lambda: asdict(self._inner.table_data(ref, path)))
        return TableData(tuple(d["columns"]), tuple(tuple(r) for r in d["rows"]), d["total_rows"], d["total_cols"])

    def system_rows(self, ref: RunRef) -> list[dict[str, Any]]:
        return self._memo(ref, "system", lambda: self._inner.system_rows(ref))
