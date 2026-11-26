"""The only module that talks to W&B. Read-only; screens depend on the protocol."""

from __future__ import annotations

import tempfile
import threading
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Protocol

from ..formatting import parse_time
from ..series import Point, extract_points, numeric_values, plottable_keys
from ..audio import AudioInfo, audio_entries, parse_audio_steps
from ..images import MAX_SCAN_ROWS, ImageInfo, ImageSeries, image_entries, parse_image_steps
from ..tables import MAX_TABLE_BYTES, TableData, TableInfo, parse_table, table_entries
from .models import (  # noqa: F401  (safe_relative_path is re-exported)
    CopyEstimate, FileInfo, ProjectInfo, RunInfo, RunMetadata, RunRef, download_run_file, is_inside,
    parse_metadata, safe_relative_path,
)
from .copy_run import SKIP_FILES, CopyJob, is_system_artifact

SAMPLES = 1000  # max points fetched per series
# The SDK's own query for a run's sampled history (Run._sampled_history), but with any number of
# specs: one request can carry one spec per metric, which is how a whole panel loads in one round trip.
SAMPLED_HISTORY_GQL = """
query RunSampledHistory($project: String!, $entity: String!, $name: String!, $specs: [JSONString!]!) {
    project(name: $project, entityName: $entity) {
        run(name: $name) { sampledHistory(specs: $specs) }
    }
}
"""
RUNS_PER_PAGE = 100
PROJECTS_PER_PAGE = 100

# One request per page of projects, each with its single most recently active run and
# run counts. Cheaper than several requests per project. Uses only fields/arguments the
# SDK itself uses (Project.runCount(filters:), Project.runs(order:)).
PROJECTS_ACTIVITY_GQL = """
query ProjectsActivity($entity: String, $cursor: String, $perPage: Int = 100, $running: JSONString) {
  models(entityName: $entity, after: $cursor, first: $perPage) {
    pageInfo { endCursor hasNextPage }
    edges { node {
      name
      createdAt
      runCount
      activeRuns: runCount(filters: $running)
      runs(first: 1, order: "-heartbeat_at") { edges { node { heartbeatAt } } }
    } }
  }
}
"""
RUNNING_FILTER = json.dumps({"state": "running"})

def last_run_at(node: dict[str, Any]) -> str:
    edges = ((node.get("runs") or {}).get("edges")) or []
    return (edges[0].get("node") or {}).get("heartbeatAt") or "" if edges else ""


def project_activity(node: dict[str, Any]) -> datetime:
    """Last activity: newest run heartbeat, or creation time for empty projects."""
    times = [parse_time(node.get("createdAt")), parse_time(last_run_at(node))]
    return max((t for t in times if t), default=datetime.min)


def _project_info(node: dict[str, Any]) -> ProjectInfo:
    return ProjectInfo(
        node["name"], last_run_at(node), node.get("runCount"), node.get("activeRuns")
    )


# The in-place edits this app makes (group, name, tags). Each sends ONLY the one field being
# changed (the SDK's own summary write does the same); Run.update() is avoided on purpose
# because it also rewrites config, notes, description and the whole summary.
SET_GROUP_GQL = """
mutation SetRunGroup($id: String, $groupName: String) {
  upsertBucket(input: {id: $id, groupName: $groupName}) { bucket { id } }
}
"""
SET_NAME_GQL = """
mutation SetRunName($id: String, $displayName: String) {
  upsertBucket(input: {id: $id, displayName: $displayName}) { bucket { id } }
}
"""
SET_TAGS_GQL = """
mutation SetRunTags($id: String, $tags: [String!]) {
  upsertBucket(input: {id: $id, tags: $tags}) { bucket { id } }
}
"""


class WandbClient(Protocol):
    def list_entities(self) -> list[str]:
        """Personal entity first, followed by team entities."""
        ...

    def iter_projects(self, entity: str) -> Iterator[ProjectInfo]: ...

    def iter_runs(self, entity: str, project: str) -> Iterator[RunInfo]: ...

    def console_lines(self, ref: RunRef) -> Iterator[str]:
        """The run's terminal output, oldest line first."""
        ...

    def metric_keys(self, ref: RunRef) -> list[str]:
        """Names of plottable (numeric) metrics logged by the run."""
        ...

    def metric_series(self, ref: RunRef, y: str, x: str) -> list[Point]: ...

    def metric_series_many(self, ref: RunRef, names: list[str], x: str) -> dict[str, list[Point]]:
        """Several metrics against one x-axis in a single request (the same points as `metric_series`
        for each). Callers keep `names` to a few dozen."""
        ...

    def metric_values(self, ref: RunRef) -> dict[str, float]:
        """The latest value of each numeric metric (the run summary)."""
        ...

    def run_tables(self, ref: RunRef) -> list[TableInfo]:
        """The wandb.Tables the run logged (latest version of each)."""
        ...

    def table_data(self, ref: RunRef, path: str) -> TableData:
        """One table, formatted for display. `path` comes from `run_tables`."""
        ...

    def run_images(self, ref: RunRef) -> list[ImageInfo]:
        """The keys under which the run logged wandb.Images."""
        ...

    def image_steps(self, ref: RunRef, key: str) -> ImageSeries:
        """The steps at which `key` logged images (files are fetched with `download_file`)."""
        ...

    def run_audios(self, ref: RunRef) -> list[AudioInfo]:
        """The keys under which the run logged wandb.Audio."""
        ...

    def audio_steps(self, ref: RunRef, key: str) -> ImageSeries:
        """The steps at which `key` logged audio (files are fetched with `download_file`)."""
        ...

    def system_rows(self, ref: RunRef) -> list[dict[str, Any]]:
        """Raw system-metric history rows (cpu, memory, gpu, ...)."""
        ...

    def run_details(self, ref: RunRef) -> RunInfo:
        """Fresh state, start/heartbeat time, group and tags."""
        ...

    def run_metadata(self, ref: RunRef) -> RunMetadata: ...

    def run_config(self, ref: RunRef) -> dict[str, Any]: ...

    def iter_files(self, ref: RunRef) -> Iterator[FileInfo]: ...

    def download_file(self, ref: RunRef, name: str, dest: Path) -> Path:
        """Download one file under `dest`; returns its local path."""
        ...

    # ---- the only writes ----
    def set_group(self, ref: RunRef, group: str) -> None: ...

    def add_tags(self, ref: RunRef, tags: list[str]) -> list[str]:
        """Add tags (never removes any); returns the run's resulting tags."""
        ...

    def rename_run(self, ref: RunRef, name: str) -> str:
        """Change the run's display name (its id never changes); returns the new name."""
        ...

    def copy_estimate(self, ref: RunRef) -> CopyEstimate:
        """Rough size of a copy, to show before the user confirms. Read-only."""
        ...

    def start_copy(self, ref: RunRef) -> CopyJob:
        """Prepare a copy of the run into a NEW run of the same project (not started yet)."""
        ...

    def run_url(self, ref: RunRef) -> str:
        """The run's page in the W&B app (the host depends on the account: it is not always wandb.ai)."""
        ...

    def stop_run(self, ref: RunRef) -> None:
        """Ask a running run to stop gracefully (the "Stop run" button of the W&B app). Asynchronous:
        the run's own process shuts down on its next heartbeat."""
        ...

    def delete_run(self, ref: RunRef, delete_artifacts: bool = False) -> None:
        """Delete the run for good. The artifacts the run logged are kept unless `delete_artifacts`
        (which removes them from W&B too, even if other runs use them)."""
        ...


class WandbApiClient:
    def __init__(self, api_key: str) -> None:
        import wandb

        self._api = wandb.Api(api_key=api_key)
        self._api_key = api_key
        self._runs: dict[RunRef, Any] = {}
        self._details_seen: set[RunRef] = set()
        self._lock = threading.Lock()  # guards the dicts only: never held during a network call
        self._ref_locks: dict[RunRef, threading.Lock] = {}

    def _run(self, ref: RunRef):
        """The run, loaded once. Different runs load at the same time (the network call is outside the
        shared lock: it used to hold it, which made loading several runs take turns); two threads
        asking for the same run share one load."""
        with self._lock:
            run = self._runs.get(ref)
            if run is not None:
                return run
            gate = self._ref_locks.setdefault(ref, threading.Lock())
        with gate:
            with self._lock:
                run = self._runs.get(ref)
            if run is None:
                run = self._api.run(ref.path)
                with self._lock:
                    self._runs[ref] = run
        return run

    def list_entities(self) -> list[str]:
        viewer = self._api.viewer  # raises ValueError on an invalid key
        entities = [viewer.entity, *viewer.teams]
        return list(dict.fromkeys(e for e in entities if e))

    def iter_projects(self, entity: str) -> Iterator[ProjectInfo]:
        """Most recently active first. Needs every page before it can sort."""
        try:
            nodes = self._project_nodes(entity)
        except Exception:
            # Activity query unavailable: fall back to creation time via the SDK.
            nodes = [
                {"name": p.name, "createdAt": p._attrs.get("createdAt"), "runCount": None}
                for p in self._api.projects(entity, per_page=PROJECTS_PER_PAGE)
            ]
        nodes.sort(key=lambda n: (project_activity(n), n["name"]), reverse=True)
        return iter([_project_info(n) for n in nodes])

    def _project_nodes(self, entity: str) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        cursor = None
        while True:
            data = self._api._service_api.execute_graphql(
                PROJECTS_ACTIVITY_GQL,
                {
                    "entity": entity,
                    "cursor": cursor,
                    "perPage": PROJECTS_PER_PAGE,
                    "running": RUNNING_FILTER,
                },
            )
            models = data["models"]
            nodes += [edge["node"] for edge in models["edges"]]
            page = models["pageInfo"]
            if not page["hasNextPage"]:
                return nodes
            cursor = page["endCursor"]

    def iter_runs(self, entity: str, project: str) -> Iterator[RunInfo]:
        """Most recently active first (sorted by the server, so pages stream in order)."""
        self._forget_run_listings(f"{entity}/{project}")
        runs = self._api.runs(
            f"{entity}/{project}", order="-heartbeat_at", per_page=RUNS_PER_PAGE
        )
        for run in runs:
            yield RunInfo(
                run.id,
                run.name,
                getattr(run, "state", "") or "",
                getattr(run, "heartbeat_at", "") or "",
                getattr(run, "created_at", "") or "",
                getattr(run, "group", "") or "",
                tuple(getattr(run, "tags", None) or ()),
            )

    def _forget_run_listings(self, path: str) -> None:
        """`Api.runs()` memoizes each listing, with the pages it has already fetched, by path, filters
        and order. Asking again would hand back that same stale listing, so a run started after the
        first load would never show up until the app restarted. Drop it so every listing is fresh."""
        listings = getattr(self._api, "_runs", None)
        if isinstance(listings, dict):
            for key in [k for k in listings if str(k).startswith(path)]:
                listings.pop(key, None)

    def console_lines(self, ref: RunRef) -> Iterator[str]:
        for line in self._run(ref).console_logs():
            yield line.content

    def metric_keys(self, ref: RunRef) -> list[str]:
        return plottable_keys(self._run(ref).summary_metrics)

    def metric_series(self, ref: RunRef, y: str, x: str) -> list[Point]:
        rows = self._run(ref).history(keys=[y], x_axis=x, samples=SAMPLES, pandas=False)
        return extract_points(rows, x, y)

    def metric_series_many(self, ref: RunRef, names: list[str], x: str) -> dict[str, list[Point]]:
        names = list(dict.fromkeys(names))
        if len(names) <= 1:
            return {n: self.metric_series(ref, n, x) for n in names}
        try:
            specs = [json.dumps({"keys": [x, n], "samples": SAMPLES}) for n in names]
            per_spec = self._run(ref)._exec(SAMPLED_HISTORY_GQL, specs=specs)["project"]["run"]["sampledHistory"]
            if len(per_spec) != len(names):
                raise ValueError("unexpected sampledHistory answer")
            return {n: extract_points(rows, x, n) for n, rows in zip(names, per_spec)}
        except Exception:
            # the private query is unavailable or answered differently: ask for each metric by itself
            # through the public call (slower, same data); a real network error then surfaces from it
            return {n: self.metric_series(ref, n, x) for n in names}

    def metric_values(self, ref: RunRef) -> dict[str, float]:
        return numeric_values(self._run(ref).summary_metrics)

    def run_tables(self, ref: RunRef) -> list[TableInfo]:
        return table_entries(self._run(ref).summary_metrics)

    def run_images(self, ref: RunRef) -> list[ImageInfo]:
        return image_entries(self._run(ref).summary_metrics)

    def _key_rows(self, ref: RunRef, key: str):
        for n, row in enumerate(self._run(ref).scan_history(keys=[key], page_size=500)):
            if n >= MAX_SCAN_ROWS:  # a bound, so one huge run cannot stall the screen
                return
            yield row

    def image_steps(self, ref: RunRef, key: str) -> ImageSeries:
        return parse_image_steps(self._key_rows(ref, key), key)

    def run_audios(self, ref: RunRef) -> list[AudioInfo]:
        return audio_entries(self._run(ref).summary_metrics)

    def audio_steps(self, ref: RunRef, key: str) -> ImageSeries:
        return parse_audio_steps(self._key_rows(ref, key), key)

    def table_data(self, ref: RunRef, path: str) -> TableData:
        if not (safe_relative_path(path) and path.endswith(".table.json")):
            raise ValueError(f"Not a table file: {path!r}")
        file = self._run(ref).file(path)
        size = getattr(file, "size", None)
        if isinstance(size, int) and size > MAX_TABLE_BYTES:
            raise ValueError(f"The table file is too large to display ({size // 1024 // 1024} MB).")
        with tempfile.TemporaryDirectory() as tmp:
            file.download(root=tmp, replace=True, exist_ok=True).close()
            target = Path(tmp) / path
            if not is_inside(target, Path(tmp)):
                raise ValueError("The table file would be written outside the download directory.")
            raw = json.loads(target.read_text())
        return parse_table(raw)

    def system_rows(self, ref: RunRef) -> list[dict[str, Any]]:
        return self._run(ref).history(stream="system", samples=SAMPLES, pandas=False)

    def run_details(self, ref: RunRef) -> RunInfo:
        run = self._run(ref)
        if ref in self._details_seen:
            run.load(force=True)  # tags/group may have changed since it was fetched
        self._details_seen.add(ref)
        a = run._attrs
        return RunInfo(
            ref.id,
            a.get("displayName") or ref.id,
            a.get("state") or "",
            a.get("heartbeatAt") or "",
            a.get("createdAt") or "",
            a.get("group") or "",
            tuple(a.get("tags") or ()),
        )

    def run_metadata(self, ref: RunRef) -> RunMetadata:
        run = self._run(ref)
        raw = run.metadata  # wandb-metadata.json; None if missing
        dirty = None
        if isinstance(raw, dict) and isinstance(raw.get("git"), dict) and raw["git"].get("commit"):
            try:  # W&B uploads diff.patch only when the working tree had changes
                dirty = any(True for _ in run.files(names=["diff.patch"]))
            except Exception:
                pass
        return parse_metadata(raw, dirty)

    def run_config(self, ref: RunRef) -> dict[str, Any]:
        return dict(self._run(ref).config)

    def iter_files(self, ref: RunRef) -> Iterator[FileInfo]:
        for f in self._run(ref).files(per_page=100):
            size = getattr(f, "size", None)
            yield FileInfo(
                f.name,
                size if isinstance(size, int) else None,
                getattr(f, "mimetype", "") or "",
                getattr(f, "md5", "") or "",
            )

    def download_file(self, ref: RunRef, name: str, dest: Path) -> Path:
        return download_run_file(self._run(ref), name, dest)

    def set_group(self, ref: RunRef, group: str) -> None:
        group = group.strip()
        if not group:
            raise ValueError("Group name cannot be empty")
        run = self._run(ref)
        self._api._service_api.execute_graphql(
            SET_GROUP_GQL, {"id": run.storage_id, "groupName": group}
        )
        run._attrs["group"] = group

    def rename_run(self, ref: RunRef, name: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("Run name cannot be empty")
        run = self._run(ref)
        self._api._service_api.execute_graphql(
            SET_NAME_GQL, {"id": run.storage_id, "displayName": name}
        )
        run._attrs["displayName"] = name
        return name

    def add_tags(self, ref: RunRef, tags: list[str]) -> list[str]:
        run = self._run(ref)
        run.load(force=True)  # merge with the tags as they are right now
        current = list(run._attrs.get("tags") or [])
        merged = current + [t for t in dict.fromkeys(t.strip() for t in tags) if t and t not in current]
        if merged != current:
            self._api._service_api.execute_graphql(
                SET_TAGS_GQL, {"id": run.storage_id, "tags": merged}
            )
            run._attrs["tags"] = merged
        return merged

    def copy_estimate(self, ref: RunRef) -> CopyEstimate:
        run = self._run(ref)
        rows = run._attrs.get("historyLineCount")
        files = [f for f in self.iter_files(ref) if f.name not in SKIP_FILES]
        artifacts = [
            a for a in run.logged_artifacts()
            if not is_system_artifact(a.type) and not getattr(a, "is_link", False)
        ]
        return CopyEstimate(
            history_rows=rows if isinstance(rows, int) else None,
            files=len(files),
            file_bytes=sum(f.size or 0 for f in files),
            artifacts=len(artifacts),
            artifact_bytes=sum(a.size or 0 for a in artifacts),
        )

    def start_copy(self, ref: RunRef) -> CopyJob:
        return CopyJob(ref, self._api_key)

    def run_url(self, ref: RunRef) -> str:
        return str(self._run(ref).url)

    def stop_run(self, ref: RunRef) -> None:
        self._run(ref).stop()

    def delete_run(self, ref: RunRef, delete_artifacts: bool = False) -> None:
        run = self._run(ref)
        run.delete(delete_artifacts=delete_artifacts)
        with self._lock:
            self._runs.pop(ref, None)
            self._ref_locks.pop(ref, None)
