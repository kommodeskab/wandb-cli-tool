from __future__ import annotations

import os
import shlex
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..formatting import parse_time, utcnow

# Terminal run states: the run's data can no longer change (unless it is resumed,
# which also changes its heartbeat and therefore its cache version).
FINISHED_STATES = frozenset({"finished", "failed", "crashed", "killed"})


@dataclass(frozen=True)
class RunRef:
    entity: str
    project: str
    id: str
    # Set only for finished runs. Part of the disk-cache key, so a run that changed
    # since it was cached never matches. None means "never cache this run's data".
    version: str | None = None

    @property
    def path(self) -> str:
        return f"{self.entity}/{self.project}/{self.id}"


@dataclass(frozen=True)
class ProjectInfo:
    name: str
    last_run_at: str = ""  # newest run's last heartbeat (UTC ISO), "" if no runs
    run_count: int | None = None  # None: unknown
    active_runs: int | None = None


@dataclass(frozen=True)
class RunInfo:
    id: str
    name: str
    state: str = ""
    heartbeat_at: str = ""
    created_at: str = ""  # when the run started (UTC ISO)
    group: str = ""
    tags: tuple[str, ...] = ()

    def runtime_seconds(self, now: datetime | None = None) -> float | None:
        """Wall-clock time since start: until now if running, else until the last
        heartbeat. Derived from list fields; resumed runs include their idle gaps."""
        start = parse_time(self.created_at)
        end = (now or utcnow()) if self.state == "running" else parse_time(self.heartbeat_at)
        if start is None or end is None:
            return None
        return max(0.0, (end - start).total_seconds())

    @property
    def version(self) -> str | None:
        return (self.heartbeat_at or "-") if self.state in FINISHED_STATES else None


@dataclass(frozen=True)
class RunMetadata:
    """Machine/code information from the run's wandb-metadata.json. "" = unknown."""

    host: str = ""
    git_remote: str = ""
    git_commit: str = ""
    git_dirty: bool | None = None  # uncommitted changes at launch; None = unknown
    command: str = ""
    gpu: str = ""
    cpu_cores: str = ""
    python: str = ""


def _text(value: object) -> str:
    return str(value) if value not in (None, "") else ""


def parse_metadata(raw: dict | None, git_dirty: bool | None = None) -> RunMetadata:
    raw = raw if isinstance(raw, dict) else {}
    git = raw.get("git") if isinstance(raw.get("git"), dict) else {}

    program = _text(raw.get("program") or raw.get("codePath"))
    command = ""
    if program:
        exe = os.path.basename(_text(raw.get("executable"))) or "python"
        args = raw.get("args") if isinstance(raw.get("args"), list) else []
        command = shlex.join([exe, program, *map(str, args)])

    gpu_names = [
        _text(g.get("name")) for g in (raw.get("gpu_nvidia") or []) if isinstance(g, dict)
    ]
    if not any(gpu_names) and raw.get("gpu") and raw.get("gpu_count") != 0:
        gpu_names = [_text(raw["gpu"])] * (int(raw["gpu_count"]) if _is_int(raw.get("gpu_count")) else 1)
    gpu = ", ".join(
        f"{name} × {n}" if n > 1 else name for name, n in Counter(filter(None, gpu_names)).items()
    )

    physical, logical = raw.get("cpu_count"), raw.get("cpu_count_logical")
    if physical and logical and physical != logical:
        cpu = f"{physical} ({logical} threads)"
    else:
        cpu = _text(physical or logical)

    return RunMetadata(
        host=_text(raw.get("host")),
        git_remote=_text(git.get("remote")),
        git_commit=_text(git.get("commit")),
        git_dirty=git_dirty if git.get("commit") else None,
        command=command,
        gpu=gpu,
        cpu_cores=cpu,
        python=_text(raw.get("python")),
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class FileInfo:
    name: str
    size: int | None = None
    mimetype: str = ""
    md5: str = ""


def safe_relative_path(name: str) -> bool:
    """A file name from the server that can't escape the directory it is saved under."""
    parts = Path(name).parts
    return bool(name) and not Path(name).is_absolute() and ".." not in parts


def is_inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def download_run_file(run, name: str, dest: Path) -> Path:
    """Download one file of a W&B run under `dest`. A name that could escape `dest` is refused
    both before downloading and (via symlinks and the like) after it."""
    if not safe_relative_path(name):
        raise ValueError(f"Refusing to download file with unsafe name: {name!r}")
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / name
    with tempfile.TemporaryDirectory(dir=dest, prefix=".dl-") as stage_dir:
        stage = Path(stage_dir)
        run.file(name).download(root=str(stage), replace=True, exist_ok=True).close()
        staged = stage / name
        if not is_inside(staged, stage):
            staged.unlink(missing_ok=True)
            raise ValueError(f"Refusing file outside the download directory: {name!r}")
        if not is_inside(path, dest):
            raise ValueError(f"Refusing file outside the download directory: {name!r}")
        path.parent.mkdir(parents=True, exist_ok=True)
        staged.replace(path)
    return path


@dataclass(frozen=True)
class CopyEstimate:
    """Rough size of what copying a run would upload (None/0 where unknown)."""

    history_rows: int | None = None
    files: int = 0
    file_bytes: int = 0
    artifacts: int = 0  # user artifacts that would be duplicated
    artifact_bytes: int = 0
