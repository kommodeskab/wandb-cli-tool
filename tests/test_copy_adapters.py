import io
import json
import signal
import subprocess
import sys
import time
from enum import Enum
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import wandb

from test_copy_run import FakeSink, FakeSource, art
from wandb_cli_tool.api import RunRef
from wandb_cli_tool.api import copy_run as cr
from wandb_cli_tool.api.copy_run import (
    EVENT_PREFIX, ArtifactSpec, CopyJob, SourceInfo, WandbSink, WandbSource, child_main,
)

REF = RunRef("me", "proj", "srcid")


# ---- WandbSink: how the new run is created and fed -----------------------------------


class Algo(Enum):
    MD5 = "MD5"


@pytest.fixture
def fake_wandb(monkeypatch):
    calls = NS(init=None, logs=[], saves=[], artifacts=[], logged=[], used=[], finished=[], summary=[])

    class Run:
        id, url = "newid", "https://wandb.ai/me/proj/runs/newid"
        summary = NS(update=lambda self_or_values, *a: calls.summary.append(self_or_values))

        def log(self, row, step=None):
            calls.logs.append((row, step))

        def save(self, path, base_path=None, policy="live"):
            calls.saves.append((path, base_path, policy))

        def log_artifact(self, art):
            calls.logged.append(art)
            return NS(wait=lambda: calls.logged.append("waited"))

        def use_artifact(self, handle):
            calls.used.append(handle)

        def finish(self, exit_code=None):
            calls.finished.append(exit_code)

    class Artifact:
        def __init__(self, **kw):
            self.kw, self.dirs = kw, []
            self.manifest = NS(digest=lambda: "local-digest")

        def add_dir(self, path):
            self.dirs.append(path)

    def init(**kw):
        calls.init = kw
        return Run()

    monkeypatch.setattr(wandb, "init", init)
    monkeypatch.setattr(wandb, "Settings", lambda **kw: NS(**kw))
    monkeypatch.setattr(wandb, "Artifact", Artifact)
    return calls


def test_sink_creates_a_new_run_that_does_not_record_this_machine(fake_wandb, tmp_path):
    sink = WandbSink(REF, tmp_path)
    info = SourceInfo("n", {"lr": 1}, notes="", tags=("a",), group="g", job_type="")
    assert sink.start("n (copy)", info) == ("newid", "https://wandb.ai/me/proj/runs/newid")
    kw = fake_wandb.init
    assert (kw["entity"], kw["project"], kw["name"]) == ("me", "proj", "n (copy)")
    assert "id" not in kw  # wandb.init picks a fresh random id, so it can never resume the source
    assert (kw["config"], kw["tags"], kw["group"]) == ({"lr": 1}, ["a"], "g")
    assert kw["notes"] is None and kw["job_type"] is None  # empty means "unset", not ""
    assert "resume" not in kw and "fork_from" not in kw and "resume_from" not in kw
    s = kw["settings"]
    assert s.x_disable_stats and s.x_disable_meta and s.x_disable_machine_info
    assert s.disable_git and s.disable_code and not s.save_code and s.silent and s.console == "off"
    assert kw["dir"] == str(tmp_path)


def test_sink_forwards_history_files_summary_and_finishes_with_the_right_exit_code(fake_wandb, tmp_path):
    sink = WandbSink(REF, tmp_path)
    sink.start("x", SourceInfo("x"))
    sink.log({"loss": 1}, 4)
    sink.upload_file(tmp_path / "files" / "a.png", tmp_path / "files")
    sink.update_summary({"best": 1})
    assert fake_wandb.logs == [({"loss": 1}, 4)]
    assert fake_wandb.saves == [(str(tmp_path / "files" / "a.png"), str(tmp_path / "files"), "now")]
    assert fake_wandb.summary == [{"best": 1}]
    sink.finish(True)
    sink.finish(False)
    assert fake_wandb.finished == [0, 1]


def test_sink_builds_the_artifact_with_the_sources_digest_algorithm_and_waits_for_upload(fake_wandb, tmp_path):
    sink = WandbSink(REF, tmp_path)
    sink.start("x", SourceInfo("x"))
    spec = ArtifactSpec("model", "model", "d", "desc", {"k": 1}, handle=NS(manifest=NS(digest_algorithm=Algo.MD5)))
    assert sink.prepare_artifact(spec, tmp_path / "dl") == "local-digest"
    sink.log_prepared_artifact(spec)
    built = fake_wandb.logged[0]
    assert built.kw == dict(name="model", type="model", description="desc", metadata={"k": 1}, digest_algorithm="MD5")
    assert built.dirs == [str(tmp_path / "dl")]
    assert fake_wandb.logged[1] == "waited"  # uploaded before temp files are deleted
    sink.use_artifact(spec)
    assert fake_wandb.used == [spec.handle]


def test_sink_finish_is_safe_if_the_run_was_never_created(tmp_path):
    WandbSink(REF, tmp_path).finish(False)  # start() failed earlier: nothing to finish


# ---- WandbSource: reading the original (read-only) -----------------------------------


class FakeEntry:
    def __init__(self, ref=None):
        self.ref = ref


def fake_public_artifact(name="model:v3", type_="model", refs=False, link=False):
    return NS(
        name=name, type=type_, digest="dig", description=None, metadata={"m": 1}, size=100,
        is_link=link, manifest=NS(entries={"a": FakeEntry("s3://bucket/a" if refs else None)}),
    )


class FakeSrcRun:
    id, name = "srcid", None
    config = {"lr": 0.1, "_wandb": {"x": 1}}
    notes, tags, group, job_type = None, ["t"], None, "train"
    _attrs = {"historyLineCount": 42}
    summary_metrics = {"loss": 1}

    def scan_history(self, page_size=1000):
        yield {"_step": 0, "loss": 1}

    def files(self, per_page=100):
        return [NS(name="a.txt", size=3, mimetype="text/plain", md5="m"), NS(name="b", size=None, mimetype=None, md5=None)]

    def file(self, name):
        def download(root=".", replace=False, exist_ok=False):
            (Path(root) / name).parent.mkdir(parents=True, exist_ok=True)
            (Path(root) / name).write_text("x")
            return io.StringIO()
        return NS(download=download)

    def logged_artifacts(self):
        return [fake_public_artifact(), fake_public_artifact("remote:v1", refs=True)]

    def used_artifacts(self):
        return [fake_public_artifact("data:v2", "dataset")]


def test_source_reads_run_metadata_history_files_and_artifacts(tmp_path):
    source = WandbSource(NS(run=lambda path: FakeSrcRun()), REF)
    info = source.info()
    assert (info.name, info.config, info.notes, info.group, info.tags, info.job_type, info.history_rows) == (
        "srcid", {"lr": 0.1}, "", "", ("t",), "train", 42)  # no name -> falls back to id; _wandb dropped
    assert list(source.history()) == [{"_step": 0, "loss": 1}]
    assert source.summary() == {"loss": 1}
    assert [(f.name, f.size, f.mimetype) for f in source.files()] == [("a.txt", 3, "text/plain"), ("b", None, "")]
    assert source.download_file("sub/a.txt", tmp_path).read_text() == "x"
    outs = source.output_artifacts()
    assert [(o.name, o.type, o.is_reference, o.size) for o in outs] == [("model", "model", False, 100), ("remote", "model", True, 100)]
    assert outs[0].description == "" and outs[0].metadata == {"m": 1}
    assert [(i.name, i.type) for i in source.input_artifacts()] == [("data", "dataset")]


# ---- child process: event protocol, in-process -----------------------------------------


@pytest.fixture
def child(monkeypatch, capsys):
    saved = signal.getsignal(signal.SIGINT)
    monkeypatch.setattr(wandb, "Api", lambda *a, **k: object())
    yield lambda **parts: _run_child(monkeypatch, capsys, **parts)
    signal.signal(signal.SIGINT, saved)


def _run_child(monkeypatch, capsys, source=None, sink=None):
    monkeypatch.setattr(cr, "WandbSource", lambda api, ref: source or FakeSource())
    monkeypatch.setattr(cr, "WandbSink", lambda ref, workdir: sink or FakeSink())
    code = child_main([json.dumps({"entity": "me", "project": "proj", "run_id": "srcid"})])
    out = capsys.readouterr().out
    events = [json.loads(l[len(EVENT_PREFIX):]) for l in out.splitlines() if l.startswith(EVENT_PREFIX)]
    return code, events


def test_child_reports_started_progress_and_done(child):
    code, events = child()
    assert code == 0
    assert events[0]["event"] == "started" and events[0]["run_id"] == "newrun"
    assert events[-1]["event"] == "done" and events[-1]["name"] == "train-7 (copy)"
    assert any(e["event"] == "progress" and e["stage"] == "history" for e in events)


def test_child_reports_errors_with_the_partial_run_id_and_finishes_the_run_as_failed(child):
    sink = FakeSink(fail_log_at=0)
    code, events = child(sink=sink)
    assert code == 1 and sink.finished is False
    assert events[-1]["event"] == "error" and "server said no" in events[-1]["message"]
    assert events[-1]["run_id"] == "newrun"


def test_child_treats_an_interrupt_as_cancel(child):
    class Source(FakeSource):
        def history(self):
            yield {"_step": 0, "a": 1}
            raise KeyboardInterrupt

    sink = FakeSink()
    code, events = child(source=Source(), sink=sink)
    assert code == 130 and sink.finished is False
    assert events[-1] == {"event": "cancelled", "run_id": "newrun", "url": "https://wandb.ai/e/p/runs/newrun"}


def test_child_removes_its_temporary_directory(child, monkeypatch, tmp_path):
    import tempfile
    made = []
    real = tempfile.mkdtemp
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **kw: made.append(real(dir=tmp_path, **kw)) or made[-1])
    child()
    assert made and not Path(made[0]).exists()


# ---- parent side: CopyJob with real subprocesses ----------------------------------------


def job_for(script: str, **kw):
    return CopyJob(REF, "k-secret", command=[sys.executable, "-c", script], **kw)


def emit(event):  # code for a child script
    return f"print({EVENT_PREFIX!r} + json.dumps({event!r}), flush=True)"


def test_job_parses_prefixed_events_and_ignores_other_output():
    script = "import json\nprint('noise from wandb')\n" + emit({"event": "started", "run_id": "r"}) + "\nprint('more noise')\n" + emit({"event": "done", "run_id": "r"})
    job = job_for(script)
    job.start()
    assert [e["event"] for e in job.events()] == ["started", "done"]


def test_job_turns_a_silent_crash_into_an_error_with_the_last_output():
    job = job_for("import sys\nprint('Traceback...')\nprint('ValueError: nope')\nsys.exit(3)")
    job.start()
    events = list(job.events())
    assert [e["event"] for e in events] == ["error"]
    assert "ValueError: nope" in events[0]["message"] and "unexpectedly" in events[0]["message"]


def test_job_cancel_sends_sigint_and_the_child_can_finish_cleanly():
    script = (
        "import json, signal, sys, time\n"
        "signal.signal(signal.SIGINT, lambda *a: (print(" + repr(EVENT_PREFIX) + " + json.dumps({'event': 'cancelled', 'run_id': 'r'}), flush=True), sys.exit(130)))\n"
        + emit({"event": "started", "run_id": "r"}) + "\ntime.sleep(60)"
    )
    job = job_for(script)
    job.start()
    seen = []
    for event in job.events():
        seen.append(event["event"])
        if event["event"] == "started":
            job.cancel(grace=10)
    assert seen == ["started", "cancelled"]


def test_job_cancel_forces_a_child_that_ignores_sigint():
    script = "import json, signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\n" + emit({"event": "started"}) + "\ntime.sleep(60)"
    job = job_for(script)
    job.start()
    t0, seen = time.time(), []
    for event in job.events():
        seen.append(event["event"])
        if event["event"] == "started":
            job.cancel(grace=0.5)
    assert seen == ["started", "cancelled"] and time.time() - t0 < 15


def test_job_passes_the_api_key_by_environment_only(monkeypatch):
    captured = {}

    class FakePopen:
        def __init__(self, args, **kw):
            captured.update(args=args, kw=kw)
            self.stdout = io.StringIO("")

        def wait(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    job = CopyJob(REF, "k-secret")
    job.start()
    assert "k-secret" not in " ".join(captured["args"])  # not visible in `ps`
    assert captured["kw"]["env"]["WANDB_API_KEY"] == "k-secret"
    assert captured["kw"]["env"]["WANDB_SILENT"] == "true" and captured["kw"]["stdin"] == subprocess.DEVNULL
    assert json.loads(captured["args"][-1]) == {"entity": "me", "project": "proj", "run_id": "srcid"}
    assert captured["args"][0] == sys.executable and "child_main" in captured["args"][2]
