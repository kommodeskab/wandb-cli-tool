import stat

from wandb_cli_tool.api import CachedClient, DiskCache, RunRef


def test_get_set_roundtrip_and_miss(tmp_path):
    cache = DiskCache(tmp_path)
    assert cache.get("ns", "k") is None
    cache.set("ns", "k", {"a": [1, 2]})
    assert cache.get("ns", "k") == {"a": [1, 2]}
    assert cache.get("other", "k") is None
    cache.set("ns", "empty", [])
    assert cache.get("ns", "empty") == []  # falsy values are real hits


def test_files_are_private(tmp_path):
    cache = DiskCache(tmp_path / "c")
    cache.set("ns", "k", 1)
    (path,) = (tmp_path / "c").rglob("*.json")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_corrupt_or_unserialisable_entries_are_ignored(tmp_path):
    cache = DiskCache(tmp_path)
    cache.set("ns", "k", 1)
    (path,) = tmp_path.rglob("*.json")
    path.write_text("{not json")
    assert cache.get("ns", "k") is None
    cache.set("ns", "bad", object())  # not JSON: silently skipped
    assert cache.get("ns", "bad") is None
    assert not list(tmp_path.rglob("*.tmp"))


def test_set_never_raises_even_when_the_location_is_unusable(tmp_path):
    (tmp_path / "blocked").write_text("a file where a directory should be")
    cache = DiskCache(tmp_path / "blocked")
    cache.set("ns", "k", 1)  # must not raise
    assert cache.get("ns", "k") is None


def test_prune_keeps_newest_entries(tmp_path):
    import os

    cache = DiskCache(tmp_path, max_entries=3)
    for i in range(6):
        cache.set("ns", f"k{i}", i)
        os.utime(cache._path("ns", f"k{i}"), (1000 + i, 1000 + i))  # k5 is newest
    cache.prune()
    assert [cache.get("ns", f"k{i}") for i in range(6)] == [None, None, None, 3, 4, 5]


def test_clear_removes_everything(tmp_path):
    cache = DiskCache(tmp_path / "c")
    cache.set("ns", "k", 1)
    cache.clear()
    cache.clear()
    assert cache.get("ns", "k") is None


class CountingClient:
    def __init__(self):
        self.calls = []

    def metric_keys(self, ref):
        self.calls.append("keys")
        return ["a"]

    def metric_series(self, ref, y, x):
        self.calls.append(("series", y, x))
        return [(0.0, 1.5), (1.0, 2.5)]

    def system_rows(self, ref):
        self.calls.append("system")
        return [{"_runtime": 1, "system.cpu": 2.0}]

    def list_entities(self):
        return ["me"]


def test_finished_run_data_is_fetched_once_then_served_from_disk(tmp_path):
    inner, cache = CountingClient(), DiskCache(tmp_path)
    ref = RunRef("e", "p", "r", version="2024-01-01")
    for _ in range(2):
        client = CachedClient(inner, cache)  # new instance: proves it is on disk
        assert client.metric_keys(ref) == ["a"]
        assert client.metric_series(ref, "loss", "_step") == [(0.0, 1.5), (1.0, 2.5)]
        assert client.system_rows(ref) == [{"_runtime": 1, "system.cpu": 2.0}]
    assert inner.calls == ["keys", ("series", "loss", "_step"), "system"]


def test_series_cache_distinguishes_y_x_and_run_version(tmp_path):
    inner, cache = CountingClient(), DiskCache(tmp_path)
    client = CachedClient(inner, cache)
    ref = RunRef("e", "p", "r", version="v1")
    client.metric_series(ref, "loss", "_step")
    client.metric_series(ref, "loss", "acc")
    client.metric_series(ref, "acc", "_step")
    client.metric_series(RunRef("e", "p", "r", version="v2"), "loss", "_step")  # run changed
    assert len(inner.calls) == 4
    client.metric_series(ref, "loss", "acc")
    assert len(inner.calls) == 4


def test_unfinished_runs_are_never_cached(tmp_path):
    inner, cache = CountingClient(), DiskCache(tmp_path)
    client = CachedClient(inner, cache)
    live = RunRef("e", "p", "r")  # version None
    client.metric_keys(live)
    client.metric_keys(live)
    assert inner.calls == ["keys", "keys"]
    assert not list(tmp_path.rglob("*.json"))


def test_prune_dir_sometimes_walks_the_folder_on_the_first_call_and_then_only_now_and_then(tmp_path, monkeypatch):
    from wandb_cli_tool.api import cache as cache_mod

    cache = DiskCache(tmp_path)
    calls = []
    monkeypatch.setattr(cache, "prune_dir", lambda ns, max_bytes: calls.append((ns, max_bytes)))
    for _ in range(cache_mod.PRUNE_EVERY + 1):
        cache.prune_dir_sometimes("files", 123)
    assert calls == [("files", 123), ("files", 123)]  # calls 1 and PRUNE_EVERY + 1 only
