from pathlib import Path

import pytest

from wandb_cli_tool.api import FileInfo
from wandb_cli_tool.api.copy_run import (
    ArtifactSpec, CopyCancelled, SourceInfo, copy_run, is_system_artifact,
)


class FakeSource:
    def __init__(self, **overrides):
        self._info = SourceInfo(
            name="train-7", config={"lr": 0.1}, notes="a note", tags=("a", "b"), group="g",
            job_type="train", history_rows=4,
        )
        self.rows = [
            {"_step": 0, "_runtime": 1, "_timestamp": 9, "loss": 1.0, "acc": None},
            {"_step": 1, "loss": 0.5},
            {"_step": 2, "_runtime": 3},  # only underscore keys: nothing to log
            {"_step": 3, "loss": 0.25, "img": {"_type": "image-file", "path": "media/images/x.png"}},
        ]
        self.summary_values = {"loss": 0.25, "best": 0.2, "_step": 3, "_wandb": {"runtime": 4}}
        self.file_list = [FileInfo("output.log", 5), FileInfo("media/images/x.png", 9)]
        self.outputs, self.inputs = [], []
        self.reads = []
        self.fail_files = set()
        self.__dict__.update(overrides)

    def info(self):
        return self._info

    def history(self):
        yield from self.rows

    def summary(self):
        return dict(self.summary_values)

    def files(self):
        return list(self.file_list)

    def download_file(self, name, dest):
        self.reads.append(("file", name))
        if name in self.fail_files:
            raise OSError("disk full")
        return dest / name

    def output_artifacts(self):
        return list(self.outputs)

    def download_artifact(self, spec, dest):
        self.reads.append(("artifact", spec.name))
        return dest

    def input_artifacts(self):
        return list(self.inputs)


class FakeSink:
    def __init__(self, digests=None, fail_log_at=None):
        self.calls = []
        self.digests = digests or {}
        self.fail_log_at = fail_log_at
        self.finished = None

    def start(self, name, info):
        self.calls.append(("start", name, info))
        return "newrun", "https://wandb.ai/e/p/runs/newrun"

    def log(self, row, step):
        if self.fail_log_at == step:
            raise RuntimeError("server said no")
        self.calls.append(("log", step, row))

    def update_summary(self, values):
        self.calls.append(("summary", values))

    def upload_file(self, path, base):
        self.calls.append(("file", str(path.relative_to(base))))

    def prepare_artifact(self, spec, directory):
        self.calls.append(("prepare", spec.name))
        return self.digests.get(spec.name, spec.digest)

    def log_prepared_artifact(self, spec):
        self.calls.append(("artifact", spec.name))

    def use_artifact(self, spec):
        self.calls.append(("use", spec.name))

    def finish(self, ok):
        self.finished = ok
        self.calls.append(("finish", ok))

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


def run_copy(source=None, sink=None, cancelled=lambda: False, tmp=Path("/tmp/copy-test")):
    events = []
    source, sink = source or FakeSource(), sink or FakeSink()
    result = copy_run(source, sink, events.append, cancelled, tmp)
    return result, sink, events


def test_new_run_is_named_with_copy_suffix_and_keeps_the_source_metadata():
    result, sink, _ = run_copy()
    (_, name, info) = sink.of("start")[0]
    assert name == "train-7 (copy)" and result.name == "train-7 (copy)"
    assert (info.config, info.notes, info.tags, info.group, info.job_type) == (
        {"lr": 0.1}, "a note", ("a", "b"), "g", "train")
    assert result.run_id == "newrun" and result.url.endswith("newrun")


def test_history_is_replayed_with_original_steps_and_without_internal_keys():
    _, sink, _ = run_copy()
    assert [(c[1], c[2]) for c in sink.of("log")] == [
        (0, {"loss": 1.0}),  # None dropped, _runtime/_timestamp dropped
        (1, {"loss": 0.5}),
        (3, {"loss": 0.25, "img": {"_type": "image-file", "path": "media/images/x.png"}}),
    ]  # step 2 had nothing but underscore keys; nested _type dicts are kept intact


def test_duplicate_and_out_of_order_steps_are_skipped_with_a_warning():
    source = FakeSource(rows=[{"_step": 5, "a": 1}, {"_step": 5, "a": 2}, {"_step": 3, "a": 3}, {"a": 4}])
    result, sink, events = run_copy(source)
    assert [(c[1], c[2]) for c in sink.of("log")] == [(5, {"a": 1}), (6, {"a": 4})]  # missing step continues
    assert any("2 history rows were skipped" in w for w in result.warnings)
    assert any(e["event"] == "warning" for e in events)


def test_summary_keeps_extra_keys_but_not_internal_ones():
    _, sink, _ = run_copy()
    assert sink.of("summary") == [("summary", {"loss": 0.25, "best": 0.2})]


def test_files_are_copied_except_the_ones_the_new_run_regenerates():
    source = FakeSource(file_list=[
        FileInfo("output.log"), FileInfo("config.yaml"), FileInfo("wandb-summary.json"),
        FileInfo("media/table/t.table.json"), FileInfo("../evil"), FileInfo("/etc/passwd"),
    ])
    result, sink, _ = run_copy(source)
    assert [c[1] for c in sink.of("file")] == ["output.log", "media/table/t.table.json"]
    assert sum("unsafe name" in w for w in result.warnings) == 2
    assert ("file", "config.yaml") not in source.reads and ("file", "../evil") not in source.reads


def test_one_failing_file_is_reported_but_does_not_stop_the_copy():
    source = FakeSource(fail_files={"output.log"})
    result, sink, _ = run_copy(source)
    assert [c[1] for c in sink.of("file")] == ["media/images/x.png"]
    assert any("output.log" in w and "disk full" in w for w in result.warnings)
    assert sink.finished is True


def art(name, type="model", digest="d1", **kw):
    return ArtifactSpec(name=name, type=type, digest=digest, **kw)


def test_artifacts_with_identical_content_are_duplicated_and_inputs_are_linked():
    source = FakeSource(outputs=[art("model")], inputs=[art("dataset", "dataset")])
    _, sink, _ = run_copy(source)
    assert sink.of("prepare") == [("prepare", "model")]
    assert sink.of("artifact") == [("artifact", "model")]
    assert sink.of("use") == [("use", "dataset")]  # linked, never re-uploaded
    assert ("artifact", "dataset") not in source.reads


def test_artifact_whose_rebuilt_content_would_differ_is_never_logged():
    """Logging different content under the same name would add a version to (and could move
    'latest' of) the user's existing collection."""
    source = FakeSource(outputs=[art("model", digest="original")])
    result, sink, _ = run_copy(source, FakeSink(digests={"model": "different"}))
    assert sink.of("artifact") == []
    assert any("model" in w and "would differ" in w for w in result.warnings)


def test_system_link_and_reference_artifacts_are_not_duplicated():
    source = FakeSource(outputs=[
        art("run-x-table", "run_table"), art("source-code", "code"), art("j", "job"),
        art("h", "wandb-history"), art("linked", is_link=True),
        art("remote", is_reference=True), art("real-model"),
    ])
    result, sink, _ = run_copy(source)
    assert [c[1] for c in sink.of("artifact")] == ["real-model"]
    assert any("remote" in w and "outside W&B" in w for w in result.warnings)
    assert is_system_artifact("run_table") and is_system_artifact("wandb-whatever")
    assert not is_system_artifact("model") and not is_system_artifact("dataset")


def test_a_failing_artifact_is_reported_and_the_rest_continue():
    class Sink(FakeSink):
        def prepare_artifact(self, spec, directory):
            if spec.name == "bad":
                raise RuntimeError("boom")
            return super().prepare_artifact(spec, directory)

    source = FakeSource(outputs=[art("bad"), art("good")], inputs=[art("in", "dataset")])
    result, sink, _ = run_copy(source, Sink())
    assert [c[1] for c in sink.of("artifact")] == ["good"] and sink.of("use") == [("use", "in")]
    assert any("bad" in w and "boom" in w for w in result.warnings)


def test_success_finishes_the_new_run_normally():
    _, sink, _ = run_copy()
    assert sink.finished is True and sink.calls[-1] == ("finish", True)


def test_an_error_midway_finishes_the_new_run_as_failed_and_propagates():
    sink = FakeSink(fail_log_at=1)
    with pytest.raises(RuntimeError, match="server said no"):
        run_copy(FakeSource(), sink)
    assert sink.finished is False
    assert sink.of("file") == []  # nothing after the failure ran


def test_cancel_stops_at_the_next_checkpoint_and_finishes_as_failed():
    sink, seen = FakeSink(), {"n": 0}

    def cancelled():
        seen["n"] += 1
        return seen["n"] > 2  # allow two rows, then stop

    with pytest.raises(CopyCancelled):
        run_copy(FakeSource(), sink, cancelled)
    assert len(sink.of("log")) == 2 and sink.finished is False and sink.of("file") == []


def test_cancelled_before_anything_is_replayed_still_closes_the_created_run():
    sink = FakeSink()
    with pytest.raises(CopyCancelled):
        run_copy(FakeSource(), sink, cancelled=lambda: True)
    assert sink.of("start") and sink.of("log") == [] and sink.finished is False


def test_progress_events_report_stages_and_the_row_total():
    source = FakeSource(rows=[{"_step": i, "x": i} for i in range(450)])
    source._info = SourceInfo(name="n", history_rows=450)
    _, _, events = run_copy(source)
    kinds = [e["event"] for e in events]
    assert kinds[0] == "started"
    history = [e for e in events if e.get("stage") == "history"]
    assert history[0]["done"] == 0 and history[0]["total"] == 450
    assert [e["done"] for e in history if e["done"]][:2] == [200, 400] and history[-1]["done"] == 450
    assert {e.get("stage") for e in events if e["event"] == "progress"} >= {"history", "files", "artifacts", "finish"}

