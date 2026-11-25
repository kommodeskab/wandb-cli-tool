import pytest

from wandb_cli_tool import config


from wandb_cli_tool.api import CopyEstimate, FileInfo, ProjectInfo, RunInfo, RunMetadata
from wandb_cli_tool.api.cache import CACHE_DIR_ENV
from wandb_cli_tool.images import ImageInfo, ImageSeries, ImageStep
from wandb_cli_tool.tables import TableData, TableInfo


class FakeClient:
    ENTITIES = ["me", "team-a", "team-b"]
    PROJECTS = {
        "me": ["alpha", "beta"],
        "team-a": ["proj-x"],
        "team-b": [],
    }
    RUNS = {
        ("me", "alpha"): [
            RunInfo("id1", "run-1", "running", "2024-05-01T12:00:00", "2024-05-01T10:30:00",
                    "sweep-a", ("baseline", "v2")),
            RunInfo("id2", "run-2", "finished", "2024-05-01T09:00:00", "2024-05-01T08:00:00"),
            RunInfo("id3", "run-3", "failed", "2024-04-30T09:00:05", "2024-04-30T09:00:00",
                    "sweep-b", ("v2",)),
        ]
    }
    LOGS = ["epoch 1", "epoch 2", "\x1b[31mred\x1b[0m", "50%\r100% done"]
    METRICS = ["acc", "trainer/global_steps", "trainer/loss", "val/loss"]
    SYSTEM_ROWS = [
        {"_runtime": 1, "system.cpu": 10.0, "system.memory": 40.0},
        {"_runtime": 2, "system.cpu": 20.0, "system.memory": 41.0},
    ]

    series_calls: list = []
    fail_writes = False

    def __init__(self, api_key: str) -> None:
        self.series_calls = []
        self.writes, self.downloads, self.copies_started, self.table_calls = [], [], [], []
        self.image_step_calls = []
        import threading
        self.job_gate = threading.Event()
        self.job_gate.set()  # tests clear it to hold a copy 'in progress'
        self.groups, self.tags, self.names = {}, {}, {}
        self.deleted = set()
        if api_key == "bad":
            self.bad = True
        else:
            self.bad = False

    def list_entities(self):
        if self.bad:
            raise ValueError("invalid API key")
        return list(self.ENTITIES)

    PROJECT_INFO = {
        "alpha": ProjectInfo("alpha", "2024-05-01T12:00:00", 3, 1),
        "beta": ProjectInfo("beta", "2024-03-01T00:00:00", 7, 0),
    }

    def iter_projects(self, entity):
        for name in self.PROJECTS.get(entity, []):
            yield self.PROJECT_INFO.get(name, ProjectInfo(name))

    def iter_runs(self, entity, project):
        from dataclasses import replace

        for r in self.RUNS.get((entity, project), []):  # reflects edits made via the app
            if r.id in self.deleted:
                continue
            yield replace(
                r,
                name=self.names.get(r.id, r.name),
                group=self.groups.get(r.id, r.group),
                tags=tuple(self.tags.get(r.id, r.tags)),
            )

    def console_lines(self, ref):
        yield from self.LOGS

    def metric_keys(self, ref):
        return list(self.METRICS)

    VALUES = {"acc": 0.9, "trainer/global_steps": 100.0, "trainer/loss": 0.25, "val/loss": 0.4}
    TABLES = [
        TableInfo("preds", "media/table/preds_3_ab12cd34ef56ab78cd90.table.json", 3, 3),
        TableInfo("results", "media/table/results_1_0123456789abcdef0123.table.json", 2, 2),
    ]
    TABLE_DATA = {
        "media/table/preds_3_ab12cd34ef56ab78cd90.table.json": TableData(
            ("id", "label", "image"),
            (("1", "cat", "[image] a.png"), ("2", "dog", "[image] b.png"), ("3", "bird", "")),
            3, 3,
        ),
        "media/table/results_1_0123456789abcdef0123.table.json": TableData(("metric", "value"), (("acc", "0.9"), ("f1", "0.8")), 2, 2),
    }

    IMAGES = [ImageInfo("grid", True, 3), ImageInfo("samples", False, 1)]  # sorted by key, like the real client
    IMAGE_STEPS = {
        "samples": [
            ImageStep(s, (f"media/images/samples_{s}_abcd1234abcd1234abcd.png",), (f"sha{s}",), (None,), (f"caption {s}",))
            for s in (10, 20, 30, 40)
        ],
        "grid": [
            ImageStep(s, tuple(f"media/images/grid_{s}_{i}_ffff.png" for i in range(3)), (), (), ("a", "b", "c"))
            for s in (5, 15)
        ],
    }

    def run_images(self, ref):
        return list(self.IMAGES)

    def image_steps(self, ref, key):
        self.image_step_calls.append(key)
        if key in getattr(self, "image_step_errors", {}):
            raise RuntimeError(self.image_step_errors[key])
        steps = list(self.IMAGE_STEPS.get(key, []))
        return ImageSeries(tuple(steps), getattr(self, "image_total", len(steps)))

    def metric_values(self, ref):
        return dict(self.VALUES)

    def run_tables(self, ref):
        return list(self.TABLES)

    def table_data(self, ref, path):
        self.table_calls.append(path)
        if path in getattr(self, "table_errors", {}):
            raise RuntimeError(self.table_errors[path])
        return self.TABLE_DATA[path]

    def metric_series(self, ref, y, x):
        self.series_calls.append((y, x))
        if y == "acc":  # logged exactly once
            return [(3.0, 0.25)]
        scale = 1 + sum(map(ord, y)) % 5  # a different line for each metric
        return [(float(i), float(i * scale)) for i in range(5)]

    def system_rows(self, ref):
        return list(self.SYSTEM_ROWS)

    # ---- run details, files and the two writes ----
    META = RunMetadata(
        host="gpu-box-1", git_remote="git@github.com:me/repo.git", git_commit="abcdef1234567890",
        git_dirty=True, command="python train.py --lr 0.1", gpu="NVIDIA A100 × 2",
        cpu_cores="16 (32 threads)", python="3.10.12",
    )
    CONFIG = {"lr": 0.1, "model": {"name": "resnet", "layers": [64, 128]}, "tags": [], "seed": 1}
    TEXTS = {
        "output.log": "epoch 1\nepoch 2\n",
        "notes.txt": "line one\nline two\n",
        "media/table/results.table.json": '{"columns":["a","b"],"data":[[1,2]]}',
    }
    FILES = [
        FileInfo("output.log", len(TEXTS["output.log"]), "text/plain", "m1"),
        FileInfo("media/images/pred_0.png", 2048, "image/png", "m2"),
        FileInfo("media/audio/audio/sample.wav", 4096, "audio/wav", "m3"),
        FileInfo("media/table/results.table.json", len(TEXTS["media/table/results.table.json"]),
                 "application/json", "m5"),
        FileInfo("notes.txt", len(TEXTS["notes.txt"]), "text/plain", "m6"),
        FileInfo("model.ckpt", 3 * 1024 * 1024 * 1024, "", "m4"),
    ]

    def run_details(self, ref):
        from dataclasses import replace

        info = next(r for r in self.RUNS[("me", "alpha")] if r.id == ref.id)
        return replace(
            info,
            name=self.names.get(ref.id, info.name),
            group=self.groups.get(ref.id, info.group),
            tags=tuple(self.tags.get(ref.id, info.tags)),
        )

    def run_metadata(self, ref):
        return self.META

    def run_config(self, ref):
        return dict(self.CONFIG)

    def iter_files(self, ref):
        yield from self.FILES

    def download_file(self, ref, name, dest):
        import wave

        from PIL import Image

        self.downloads.append(name)
        path = dest / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith(".png"):
            shade = sum(map(ord, name)) * 37 % 256  # a different picture for each file
            Image.new("RGB", (8, 8), (200, shade, 30)).save(path)
        elif name.endswith(".wav"):
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(8000)
                w.writeframes(b"\x00\x00" * 80)
        elif name == "long.txt":
            path.write_text("".join(f"row {i}\n" for i in range(200)))
        else:
            path.write_text(self.TEXTS.get(name, "data"))
        declared = next((f.size for f in self.FILES if f.name == name), None)
        if declared and declared < 10 * 1024 * 1024 and path.stat().st_size < declared:
            with open(path, "ab") as f:  # trailing bytes: images/audio still decode
                f.write(b"\0" * (declared - path.stat().st_size))
        return path

    def set_group(self, ref, group):
        if self.fail_writes:
            raise RuntimeError("permission denied")
        self.writes.append(("group", ref.id, group))
        self.groups[ref.id] = group

    ESTIMATE = CopyEstimate(history_rows=1200, files=5, file_bytes=3 * 1024 * 1024,
                            artifacts=2, artifact_bytes=2 * 1024**3)
    copy_events = None  # tests set a list of events; None = a plain successful copy

    def copy_estimate(self, ref):
        if getattr(self, "estimate_fails", False):
            raise RuntimeError("cannot list artifacts")
        return self.ESTIMATE

    def run_url(self, ref):
        return f"https://wandb.example/{ref.entity}/{ref.project}/runs/{ref.id}"

    def stop_run(self, ref):
        if self.fail_writes:
            raise RuntimeError("permission denied")
        self.writes.append(("stop", ref.id))

    def delete_run(self, ref, delete_artifacts=False):
        if self.fail_writes:
            raise RuntimeError("permission denied")
        self.writes.append(("delete+artifacts" if delete_artifacts else "delete", ref.id))
        self.deleted.add(ref.id)

    def start_copy(self, ref):
        self.copies_started.append(ref)
        events = self.copy_events or [
            {"event": "started", "run_id": "newid", "name": "run-1 (copy)", "url": "https://wandb.ai/me/alpha/runs/newid"},
            {"event": "progress", "stage": "history", "done": 600, "total": 1200, "message": "Copying metric history…"},
            {"event": "progress", "stage": "files", "done": 2, "total": 5, "message": "Copying file 3/5: output.log"},
            {"event": "warning", "message": "Artifact big: skipped"},
            {"event": "done", "run_id": "newid", "name": "run-1 (copy)", "url": "https://wandb.ai/me/alpha/runs/newid", "warnings": ["Artifact big: skipped"]},
        ]
        return FakeJob(events, self.job_gate)

    def rename_run(self, ref, name):
        if self.fail_writes:
            raise RuntimeError("permission denied")
        self.writes.append(("name", ref.id, name))
        self.names[ref.id] = name
        return name

    def add_tags(self, ref, tags):
        if self.fail_writes:
            raise RuntimeError("permission denied")
        current = list(self.tags.get(ref.id, self.run_details(ref).tags))
        merged = current + [t for t in tags if t not in current]
        self.writes.append(("tags", ref.id, list(tags)))
        self.tags[ref.id] = merged
        return merged


@pytest.fixture(autouse=True)
def utc_timezone(monkeypatch):
    """Local-time formatting is deterministic."""
    import time

    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture(autouse=True)
def config_dir(tmp_path, monkeypatch):
    """Never touch the real user config."""
    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(tmp_path / "cfg"))
    monkeypatch.setenv(CACHE_DIR_ENV, str(tmp_path / "cache"))
    return tmp_path / "cfg"


@pytest.fixture
def make_app():
    from wandb_cli_tool.app import WandbCliApp

    return lambda: WandbCliApp(client_factory=FakeClient)


class FakePlayer:
    """Stands in for AudioPlayer: records plays instead of making sound."""

    def __init__(self, available=True):
        self.available = available
        self.playing = False
        self.played = []
        self.stops = 0

    def play(self, path):
        if not self.available:
            return False
        self.played.append(path)
        self.playing = True
        return True

    def stop(self):
        self.stops += 1
        self.playing = False


class FakeJob:
    """Stands in for CopyJob: yields scripted events (optionally held back by a gate)."""

    def __init__(self, events, gate):
        self.events_list, self.gate = events, gate
        self.started = self.cancelled = False

    def start(self):
        self.started = True

    def events(self):
        for event in self.events_list:
            if event["event"] in ("done", "error"):
                self.gate.wait(10)  # a gated job stays "running" until released or cancelled
                if self.cancelled:
                    yield {"event": "cancelled", "run_id": "newid"}
                    return
            yield event

    def cancel(self, grace=30.0):
        self.cancelled = True
        self.gate.set()
