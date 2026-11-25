"""Copy a run into a new run of the same project by replaying its data.

W&B has no "copy run" call, so the copy is rebuilt: a new run is created through the logging
SDK and fed the source run's config, history, summary, files and artifacts. It runs in a
separate process (`python -m wandb_cli_tool.api.copy_run`) so the SDK's global state, console
output and signal handling cannot disturb the TUI, and so it can be cancelled.

Known gaps (a faithful copy is not possible with the public API): original timestamps of
history rows, and the system-metrics stream (CPU/GPU curves) are not carried over.

The source run is only ever READ. The only thing written is the new run.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from .models import FileInfo, RunRef, download_run_file, safe_relative_path

COPY_SUFFIX = " (copy)"
EVENT_PREFIX = "@@wbt@@ "  # marks our JSON lines among any other output of the child
# Files the new run regenerates itself.
SKIP_FILES = frozenset({"wandb-summary.json", "config.yaml"})
# Artifacts W&B creates automatically (tables, code, launch jobs, ...); history rows keep
# pointing at the originals, so they are not duplicated.
SYSTEM_ARTIFACT_TYPES = frozenset({"run_table", "job", "code"})
HISTORY_REPORT_EVERY = 200  # rows between progress events


def is_system_artifact(artifact_type: str) -> bool:
    return artifact_type in SYSTEM_ARTIFACT_TYPES or artifact_type.startswith("wandb-")


class CopyCancelled(Exception):
    """The user asked to stop; the partial copy is finished as a failed run."""


@dataclass(frozen=True)
class SourceInfo:
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    tags: tuple[str, ...] = ()
    group: str = ""
    job_type: str = ""
    history_rows: int | None = None


@dataclass(frozen=True)
class ArtifactSpec:
    name: str  # collection name, without the :version
    type: str
    digest: str = ""  # manifest digest of the source version
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    size: int | None = None
    is_reference: bool = False  # files live outside W&B, so they cannot be re-uploaded
    is_link: bool = False
    handle: Any = None  # the SDK object, opaque to the orchestrator


class RunSource(Protocol):
    def info(self) -> SourceInfo: ...
    def history(self) -> Iterator[dict[str, Any]]: ...
    def summary(self) -> dict[str, Any]: ...
    def files(self) -> list[FileInfo]: ...
    def download_file(self, name: str, dest: Path) -> Path: ...
    def output_artifacts(self) -> list[ArtifactSpec]: ...
    def download_artifact(self, spec: ArtifactSpec, dest: Path) -> Path: ...
    def input_artifacts(self) -> list[ArtifactSpec]: ...


class RunSink(Protocol):
    def start(self, name: str, info: SourceInfo) -> tuple[str, str]:
        """Create the new run; returns (run id, url)."""
        ...

    def log(self, row: dict[str, Any], step: int) -> None: ...
    def update_summary(self, values: dict[str, Any]) -> None: ...
    def upload_file(self, path: Path, base: Path) -> None: ...
    def prepare_artifact(self, spec: ArtifactSpec, directory: Path) -> str:
        """Build the artifact from `directory`; returns the digest it would have."""
        ...

    def log_prepared_artifact(self, spec: ArtifactSpec) -> None: ...
    def use_artifact(self, spec: ArtifactSpec) -> None: ...
    def finish(self, ok: bool) -> None: ...


Report = Callable[[dict[str, Any]], None]


@dataclass
class CopyResult:
    run_id: str
    name: str
    url: str
    warnings: list[str] = field(default_factory=list)


def copy_run(
    source: RunSource,
    sink: RunSink,
    report: Report,
    cancelled: Callable[[], bool],
    workdir: Path,
) -> CopyResult:
    """Replay `source` into a new run made by `sink`. The new run is always finished: as
    a normal run on success, as a failed one on error or cancel."""
    info = source.info()
    name = f"{info.name}{COPY_SUFFIX}"
    warnings: list[str] = []

    def warn(message: str) -> None:
        warnings.append(message)
        report({"event": "warning", "message": message})

    def check() -> None:
        if cancelled():
            raise CopyCancelled()

    run_id, url = sink.start(name, info)
    report({"event": "started", "run_id": run_id, "name": name, "url": url})
    ok = False
    try:
        _copy_history(source, sink, info, report, check, warn)
        summary = {k: v for k, v in source.summary().items() if not k.startswith("_")}
        if summary:
            sink.update_summary(summary)
        _copy_files(source, sink, workdir / "files", report, check, warn)
        _copy_artifacts(source, sink, workdir / "artifacts", report, check, warn)
        ok = True
    finally:
        report({"event": "progress", "stage": "finish", "message": "Finishing the new run…"})
        sink.finish(ok)
    return CopyResult(run_id, name, url, warnings)


def _copy_history(source, sink, info, report, check, warn) -> None:
    total = info.history_rows
    last_step, done, skipped = -1, 0, 0
    report({"event": "progress", "stage": "history", "done": 0, "total": total,
            "message": "Copying metric history…"})
    for row in source.history():
        check()
        done += 1
        step = row.get("_step")
        step = step if isinstance(step, int) and not isinstance(step, bool) else last_step + 1
        data = {k: v for k, v in row.items() if not k.startswith("_") and v is not None}
        if data:
            if step <= last_step:  # the SDK only accepts increasing steps
                skipped += 1
            else:
                sink.log(data, step)
                last_step = step
        if done % HISTORY_REPORT_EVERY == 0:
            report({"event": "progress", "stage": "history", "done": done, "total": total,
                    "message": "Copying metric history…"})
    report({"event": "progress", "stage": "history", "done": done, "total": total or done,
            "message": "Metric history copied."})
    if skipped:
        warn(f"{skipped} history rows were skipped because their steps were duplicated or out of order.")


def _copy_files(source, sink, directory: Path, report, check, warn) -> None:
    files = [f for f in source.files() if f.name not in SKIP_FILES]
    for i, info in enumerate(files):
        check()
        report({"event": "progress", "stage": "files", "done": i, "total": len(files),
                "message": f"Copying file {i + 1}/{len(files)}: {info.name}"})
        if not safe_relative_path(info.name):
            warn(f"Skipped file with an unsafe name: {info.name!r}")
            continue
        try:
            sink.upload_file(source.download_file(info.name, directory), directory)
        except Exception as exc:  # one bad file should not lose the whole copy
            warn(f"Could not copy file {info.name}: {exc}")
    report({"event": "progress", "stage": "files", "done": len(files), "total": len(files),
            "message": "Files copied."})


def _copy_artifacts(source, sink, directory: Path, report, check, warn) -> None:
    outputs = [
        a for a in source.output_artifacts()
        if not a.is_link and not is_system_artifact(a.type)
    ]
    for i, spec in enumerate(outputs):
        check()
        report({"event": "progress", "stage": "artifacts", "done": i, "total": len(outputs),
                "message": f"Copying artifact {i + 1}/{len(outputs)}: {spec.name} ({spec.type})"})
        if spec.is_reference:
            warn(f"Artifact {spec.name} only references files outside W&B; not copied.")
            continue
        try:
            local = source.download_artifact(spec, directory / f"{i}")
            digest = sink.prepare_artifact(spec, local)
            if digest != spec.digest:
                # Logging different content under the same name would add a version to
                # (and could move 'latest' of) your existing collection. Never do that.
                warn(f"Artifact {spec.name}: re-uploaded content would differ from the "
                     "original, so it was skipped to avoid adding a version to the collection.")
                continue
            sink.log_prepared_artifact(spec)
        except Exception as exc:
            warn(f"Could not copy artifact {spec.name}: {exc}")
    for spec in source.input_artifacts():  # inputs are linked, not duplicated (lineage stays right)
        check()
        try:
            sink.use_artifact(spec)
        except Exception as exc:
            warn(f"Could not link input artifact {spec.name}: {exc}")
    report({"event": "progress", "stage": "artifacts", "done": len(outputs), "total": len(outputs),
            "message": "Artifacts copied."})


# ---- the real W&B adapters (only used inside the child process) ----------------------


def _artifact_spec(art) -> ArtifactSpec:
    manifest = art.manifest
    entries = getattr(manifest, "entries", {}) or {}
    return ArtifactSpec(
        name=art.name.rsplit(":", 1)[0],
        type=art.type,
        digest=art.digest or "",
        description=art.description or "",
        metadata=dict(art.metadata or {}),
        size=art.size,
        is_reference=any(getattr(e, "ref", None) for e in entries.values()),
        is_link=bool(getattr(art, "is_link", False)),
        handle=art,
    )


class WandbSource:
    def __init__(self, api, ref: RunRef) -> None:
        self._run = api.run(ref.path)

    def info(self) -> SourceInfo:
        run = self._run
        config = {k: v for k, v in dict(run.config).items() if not str(k).startswith("_wandb")}
        rows = run._attrs.get("historyLineCount")
        return SourceInfo(
            name=run.name or run.id,
            config=config,
            notes=run.notes or "",
            tags=tuple(run.tags or ()),
            group=run.group or "",
            job_type=getattr(run, "job_type", "") or "",
            history_rows=rows if isinstance(rows, int) else None,
        )

    def history(self) -> Iterator[dict[str, Any]]:
        yield from self._run.scan_history(page_size=1000)

    def summary(self) -> dict[str, Any]:
        return dict(self._run.summary_metrics)

    def files(self) -> list[FileInfo]:
        out = []
        for f in self._run.files(per_page=100):
            size = getattr(f, "size", None)
            out.append(FileInfo(f.name, size if isinstance(size, int) else None,
                                getattr(f, "mimetype", "") or "", getattr(f, "md5", "") or ""))
        return out

    def download_file(self, name: str, dest: Path) -> Path:
        return download_run_file(self._run, name, dest)

    def output_artifacts(self) -> list[ArtifactSpec]:
        return [_artifact_spec(a) for a in self._run.logged_artifacts()]

    def download_artifact(self, spec: ArtifactSpec, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        return Path(spec.handle.download(root=str(dest)))

    def input_artifacts(self) -> list[ArtifactSpec]:
        return [_artifact_spec(a) for a in self._run.used_artifacts()]


class WandbSink:
    def __init__(self, ref: RunRef, workdir: Path) -> None:
        self._ref = ref
        self._workdir = workdir
        self._run = None
        self._prepared: dict[str, Any] = {}

    def start(self, name: str, info: SourceInfo) -> tuple[str, str]:
        import wandb

        settings = wandb.Settings(
            silent=True, quiet=True, show_info=False, console="off",
            # The new run must not record THIS machine's stats, git state or code.
            x_disable_stats=True, x_disable_meta=True, x_disable_machine_info=True,
            x_save_requirements=False, save_code=False, disable_git=True, disable_code=True,
            disable_job_creation=True,
        )
        self._run = wandb.init(
            entity=self._ref.entity, project=self._ref.project,
            name=name,  # no id: wandb.init makes a fresh random one, so it can never resume the source
            notes=info.notes or None, tags=list(info.tags) or None,
            group=info.group or None, job_type=info.job_type or None,
            config=info.config, dir=str(self._workdir), settings=settings,
        )
        return self._run.id, self._run.url

    def log(self, row: dict[str, Any], step: int) -> None:
        self._run.log(row, step=step)

    def update_summary(self, values: dict[str, Any]) -> None:
        self._run.summary.update(values)

    def upload_file(self, path: Path, base: Path) -> None:
        self._run.save(str(path), base_path=str(base), policy="now")

    def prepare_artifact(self, spec: ArtifactSpec, directory: Path) -> str:
        import wandb

        algorithm = getattr(spec.handle.manifest, "digest_algorithm", None)
        kwargs = {"digest_algorithm": getattr(algorithm, "value", algorithm)} if algorithm else {}
        art = wandb.Artifact(
            name=spec.name, type=spec.type, description=spec.description or None,
            metadata=spec.metadata or None, **kwargs,
        )
        art.add_dir(str(directory))
        self._prepared[spec.name] = art
        return art.manifest.digest()

    def log_prepared_artifact(self, spec: ArtifactSpec) -> None:
        logged = self._run.log_artifact(self._prepared.pop(spec.name))
        logged.wait()  # uploaded before the temporary files are removed

    def use_artifact(self, spec: ArtifactSpec) -> None:
        self._run.use_artifact(spec.handle)

    def finish(self, ok: bool) -> None:
        if self._run is not None:
            self._run.finish(exit_code=0 if ok else 1)


# ---- child process entry point ---------------------------------------------------------


def _emit(event: dict[str, Any]) -> None:
    sys.stdout.write(EVENT_PREFIX + json.dumps(event) + "\n")
    sys.stdout.flush()


def child_main(argv: list[str] | None = None) -> int:
    """Entry point of the copy process. Argument: JSON {"entity", "project", "run_id"};
    the API key comes from WANDB_API_KEY in the environment (never from the command line)."""
    args = json.loads((argv if argv is not None else sys.argv[1:])[0])
    ref = RunRef(args["entity"], args["project"], args["run_id"])
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())  # cooperative cancel
    workdir = Path(tempfile.mkdtemp(prefix="wandb-cli-tool-copy-"))
    started: dict[str, Any] = {}

    def report(event: dict[str, Any]) -> None:
        if event.get("event") == "started":
            started.update(event)
        _emit(event)

    try:
        import wandb

        source = WandbSource(wandb.Api(), ref)
        sink = WandbSink(ref, workdir)
        result = copy_run(source, sink, report, stop.is_set, workdir)
        _emit({"event": "done", "run_id": result.run_id, "name": result.name,
               "url": result.url, "warnings": result.warnings})
        return 0
    except (CopyCancelled, KeyboardInterrupt):
        _emit({"event": "cancelled", "run_id": started.get("run_id"), "url": started.get("url")})
        return 130
    except Exception as exc:
        _emit({"event": "error", "message": str(exc) or type(exc).__name__,
               "run_id": started.get("run_id"), "url": started.get("url")})
        return 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---- parent side: run the child and read its events ----------------------------------


class CopyJob:
    """Runs a copy in a child process. `events()` yields its progress as dicts and always
    ends with one of: done, error, cancelled."""

    def __init__(self, ref: RunRef, api_key: str, command: list[str] | None = None) -> None:
        self.ref = ref
        self._api_key = api_key
        self._command = command  # overridable for tests
        self._proc: subprocess.Popen | None = None
        self._cancelled = False

    def start(self) -> None:
        args = json.dumps({"entity": self.ref.entity, "project": self.ref.project, "run_id": self.ref.id})
        command = self._command or [
            sys.executable, "-c",
            "import sys; from wandb_cli_tool.api.copy_run import child_main; sys.exit(child_main())",
            args,
        ]
        env = dict(os.environ, WANDB_API_KEY=self._api_key, PYTHONUNBUFFERED="1",
                   WANDB_SILENT="true", WANDB_CONSOLE="off")
        self._proc = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, env=env,
        )

    def events(self) -> Iterator[dict[str, Any]]:
        assert self._proc is not None and self._proc.stdout is not None
        tail: list[str] = []
        finished = False
        for line in self._proc.stdout:
            if line.startswith(EVENT_PREFIX):
                try:
                    event = json.loads(line[len(EVENT_PREFIX):])
                except ValueError:
                    continue
                finished = finished or event.get("event") in ("done", "error", "cancelled")
                yield event
            else:
                tail = (tail + [line.rstrip()])[-8:]
        code = self._proc.wait()
        if not finished:  # the child died without saying why
            if self._cancelled:
                yield {"event": "cancelled", "run_id": None}
            else:
                detail = " | ".join(t for t in tail if t) or f"exit code {code}"
                yield {"event": "error", "message": f"The copy process stopped unexpectedly: {detail}",
                       "run_id": None}

    def cancel(self, grace: float = 30.0) -> None:
        """Ask the child to stop (it finishes the new run as failed); force it after `grace`."""
        self._cancelled = True
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.send_signal(signal.SIGINT)
        force = threading.Timer(grace, lambda: proc.poll() is None and proc.terminate())
        force.daemon = True  # must not keep the app alive after it quits
        force.start()


if __name__ == "__main__":
    sys.exit(child_main())
