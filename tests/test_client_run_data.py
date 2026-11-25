import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import wandb

from wandb_cli_tool.api import CachedClient, DiskCache, FileInfo, RunRef, WandbApiClient
from wandb_cli_tool.api.client import SET_GROUP_GQL, SET_NAME_GQL, SET_TAGS_GQL, safe_relative_path


class Service:
    def __init__(self):
        self.calls = []

    def execute_graphql(self, query, variables=None):
        self.calls.append((query, variables))
        return {}


class FakeFile:
    def __init__(self, name, size=10, mimetype="text/plain", md5="abc"):
        self.name, self.size, self.mimetype, self.md5 = name, size, mimetype, md5
        self.root = None

    def download(self, root=".", replace=False, exist_ok=False):
        self.root = Path(root)
        target = Path(root) / self.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
        return open(target)


class FakeRun:
    def __init__(self):
        self.storage_id = "STORAGE"
        self._attrs = {
            "displayName": "my-run", "state": "finished", "heartbeatAt": "2024-01-02T00:00:00",
            "createdAt": "2024-01-01T00:00:00", "group": "g1", "tags": ["a"],
        }
        self.loads = 0
        self.updated = False
        self.metadata = {"git": {"commit": "abc", "remote": "r"}, "host": "h"}
        self.config = {"lr": 0.1}
        self.file_obj = None
        self.diff_patch = True

    def load(self, force=False):
        self.loads += 1

    def update(self):  # must never be used: it rewrites config/summary/etc.
        self.updated = True

    def files(self, names=None, per_page=50):
        if names == ["diff.patch"]:
            return [FakeFile("diff.patch")] if self.diff_patch else []
        return [FakeFile("a.png", 5, "image/png", "m5"), FakeFile("b", None, None, None)]

    def file(self, name):
        self.file_obj = FakeFile(name)
        return self.file_obj


@pytest.fixture
def client_and_run(monkeypatch):
    run = FakeRun()
    service = Service()

    class Api:
        def __init__(self, api_key=None):
            self._service_api = service

        def run(self, path):
            self.path = path
            return run

    monkeypatch.setattr(wandb, "Api", Api)
    return WandbApiClient("key"), run, service


REF = RunRef("me", "proj", "abc")


def test_run_details_reads_state_group_tags_and_refreshes_on_reentry(client_and_run):
    client, run, _ = client_and_run
    info = client.run_details(REF)
    assert (info.name, info.state, info.group, info.tags) == ("my-run", "finished", "g1", ("a",))
    assert info.created_at == "2024-01-01T00:00:00"
    assert run.loads == 0  # first fetch is already fresh
    client.run_details(REF)
    assert run.loads == 1  # later visits re-read tags/group


def test_run_metadata_detects_dirty_git_tree_from_diff_patch(client_and_run):
    client, run, _ = client_and_run
    meta = client.run_metadata(REF)
    assert (meta.host, meta.git_commit, meta.git_dirty) == ("h", "abc", True)
    run.diff_patch = False
    assert client.run_metadata(REF).git_dirty is False
    run.metadata = None  # no metadata file at all
    assert client.run_metadata(REF).host == ""


def test_config_and_file_listing(client_and_run):
    client, _, _ = client_and_run
    assert client.run_config(REF) == {"lr": 0.1}
    assert list(client.iter_files(REF)) == [
        FileInfo("a.png", 5, "image/png", "m5"), FileInfo("b", None, "", "")
    ]


def test_set_group_sends_only_the_group_field(client_and_run):
    client, run, service = client_and_run
    client.set_group(REF, "  new-group ")
    assert service.calls == [(SET_GROUP_GQL, {"id": "STORAGE", "groupName": "new-group"})]
    assert run._attrs["group"] == "new-group"
    assert not run.updated  # Run.update() would rewrite config/summary/notes too
    with pytest.raises(ValueError):
        client.set_group(REF, "   ")
    assert len(service.calls) == 1


def test_rename_sends_only_the_display_name_and_never_the_id(client_and_run):
    client, run, service = client_and_run
    assert client.rename_run(REF, "  better name ") == "better name"
    assert service.calls == [(SET_NAME_GQL, {"id": "STORAGE", "displayName": "better name"})]
    assert "input: {id: $id, displayName: $displayName}" in SET_NAME_GQL  # nothing else is sent
    assert run._attrs["displayName"] == "better name"
    assert not run.updated  # Run.update() would rewrite config/summary/notes too
    with pytest.raises(ValueError):
        client.rename_run(REF, "   ")
    assert len(service.calls) == 1


def test_add_tags_only_adds_never_removes_and_merges_with_fresh_tags(client_and_run):
    client, run, service = client_and_run

    server_tags = ["a", "web-tag"]  # someone added "web-tag" in the browser meanwhile

    def load(force=False):
        run._attrs["tags"] = list(server_tags)

    original = service.execute_graphql

    def execute(query, variables=None):
        original(query, variables)
        server_tags[:] = variables["tags"]  # the server keeps what was written

    run.load = load
    service.execute_graphql = execute
    merged = client.add_tags(REF, ["new", "a", " new ", "", "x"])
    assert merged == ["a", "web-tag", "new", "x"]
    assert service.calls == [(SET_TAGS_GQL, {"id": "STORAGE", "tags": merged})]
    assert not run.updated
    # nothing new -> no write at all
    service.calls.clear()
    assert client.add_tags(REF, ["a", "new"]) == merged
    assert service.calls == []


def test_download_stays_inside_destination_and_rejects_unsafe_names(client_and_run, tmp_path):
    client, run, _ = client_and_run
    path = client.download_file(REF, "media/images/a.png", tmp_path / "d")
    assert path == tmp_path / "d" / "media/images/a.png" and path.read_text() == "x"
    for bad in ("../evil", "/etc/passwd", "a/../../b", ""):
        assert not safe_relative_path(bad)
        with pytest.raises(ValueError):
            client.download_file(REF, bad, tmp_path / "d2")
    assert safe_relative_path("media/a b.png")


class Counting:
    def __init__(self):
        self.calls = []

    def run_metadata(self, ref):
        from wandb_cli_tool.api import RunMetadata

        self.calls.append("meta")
        return RunMetadata(host="h", git_dirty=False)

    def run_config(self, ref):
        self.calls.append("config")
        return {"a": {"b": 1}}

    def run_details(self, ref):
        self.calls.append("details")
        return "details"

    def add_tags(self, ref, tags):
        self.calls.append(("tags", tags))
        return tags

    def rename_run(self, ref, name):
        self.calls.append(("rename", name))
        return name

    def set_group(self, ref, group):
        self.calls.append(("group", group))


def test_cached_client_caches_metadata_and_config_of_finished_runs_only(tmp_path):
    inner, cache = Counting(), DiskCache(tmp_path)
    finished, live = RunRef("e", "p", "r", version="v1"), RunRef("e", "p", "r")
    for _ in range(2):
        c = CachedClient(inner, cache)
        assert c.run_metadata(finished).host == "h" and c.run_metadata(finished).git_dirty is False
        assert c.run_config(finished) == {"a": {"b": 1}}
    assert inner.calls == ["meta", "config"]
    c.run_config(live), c.run_config(live)
    assert inner.calls[2:] == ["config", "config"]


def test_cached_client_never_caches_details_or_writes(tmp_path):
    inner, c = Counting(), CachedClient(Counting(), DiskCache(tmp_path))
    c = CachedClient(inner, DiskCache(tmp_path))
    ref = RunRef("e", "p", "r", version="v1")
    c.run_details(ref), c.run_details(ref)
    c.add_tags(ref, ["t"]), c.set_group(ref, "g"), c.rename_run(ref, "n")
    assert inner.calls == ["details", "details", ("tags", ["t"]), ("group", "g"), ("rename", "n")]


# ---- tables and bar values ----------------------------------------------------------------

TABLE_PATH = "media/table/preds_3_ab12cd34ef56ab78cd90.table.json"
TABLE_JSON = {"columns": ["id", "img"], "data": [[1, {"_type": "image-file", "path": "media/images/a.png"}], [2, None]]}


class TableFile:
    def __init__(self, name, size=100, content=None):
        self.name, self.size, self.content = name, size, content
        self.downloaded_to = None

    def download(self, root=".", replace=False, exist_ok=False):
        target = Path(root) / self.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.content if self.content is not None else json.dumps(TABLE_JSON))
        self.downloaded_to = Path(root)
        return open(target)


@pytest.fixture
def table_client(monkeypatch):
    run = FakeRun()
    run.summary_metrics = {
        "loss": 0.5, "acc": 3, "_step": 9, "img": {"_type": "image-file"},
        "preds": {"_type": "table-file", "path": TABLE_PATH, "nrows": 2, "ncols": 2},
    }
    run.files_by_name = {}
    run.file = lambda name: run.files_by_name.setdefault(name, TableFile(name))

    class Api:
        def __init__(self, api_key=None):
            self._service_api = Service()

        def run(self, path):
            return run

    monkeypatch.setattr(wandb, "Api", Api)
    return WandbApiClient("key"), run


def test_values_and_tables_come_from_the_run_summary(table_client):
    client, _ = table_client
    assert client.metric_values(REF) == {"loss": 0.5, "acc": 3.0}  # numbers only
    (table,) = client.run_tables(REF)
    assert (table.key, table.path, table.rows, table.cols) == ("preds", TABLE_PATH, 2, 2)


def test_table_data_downloads_parses_and_cleans_up(table_client):
    client, run = table_client
    data = client.table_data(REF, TABLE_PATH)
    assert data.columns == ("id", "img") and data.rows == (("1", "[image] a.png"), ("2", ""))
    assert not run.files_by_name[TABLE_PATH].downloaded_to.exists()  # temp dir removed


@pytest.mark.parametrize("path", ["../evil.table.json", "/etc/x.table.json", "media/table/not_a_table.json", ""])
def test_table_data_refuses_unsafe_or_non_table_paths(table_client, path):
    client, run = table_client
    with pytest.raises(ValueError):
        client.table_data(REF, path)
    assert run.files_by_name == {}  # nothing was even requested


def test_table_data_refuses_huge_files_and_reports_non_tables(table_client):
    client, run = table_client
    run.files_by_name[TABLE_PATH] = TableFile(TABLE_PATH, size=500 * 1024 * 1024)
    with pytest.raises(ValueError, match="too large"):
        client.table_data(REF, TABLE_PATH)
    run.files_by_name[TABLE_PATH] = TableFile(TABLE_PATH, content='{"hello": "world"}')
    with pytest.raises(ValueError, match="not a wandb.Table"):
        client.table_data(REF, TABLE_PATH)


def test_cached_client_caches_tables_and_values_for_finished_runs_only(tmp_path):
    from wandb_cli_tool.tables import TableData, TableInfo

    class Inner:
        calls = []

        def metric_values(self, ref):
            self.calls.append("values")
            return {"a": 1.0}

        def run_tables(self, ref):
            self.calls.append("tables")
            return [TableInfo("t", "p.table.json", 1, 1)]

        def table_data(self, ref, path):
            self.calls.append(("data", path))
            return TableData(("c",), (("v",),), 1, 1)

    inner = Inner()
    finished, live = RunRef("e", "p", "r", version="v1"), RunRef("e", "p", "r")
    for _ in range(2):
        c = CachedClient(inner, DiskCache(tmp_path))
        assert c.metric_values(finished) == {"a": 1.0}
        assert c.run_tables(finished) == [TableInfo("t", "p.table.json", 1, 1)]
        assert c.table_data(finished, "p.table.json") == TableData(("c",), (("v",),), 1, 1)  # tuples restored
    assert inner.calls == ["values", "tables", ("data", "p.table.json")]
    c.run_tables(live), c.run_tables(live)
    assert inner.calls[3:] == ["tables", "tables"]
