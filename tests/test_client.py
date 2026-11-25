import json
from types import SimpleNamespace as NS

import wandb

from wandb_cli_tool.api import ProjectInfo, RunInfo, WandbApiClient


class FakeService:
    """Stands in for wandb's service API: serves project pages of 2 and records calls."""

    def __init__(self, nodes, fail=False):
        self.nodes, self.fail, self.calls = nodes, fail, []

    def execute_graphql(self, query, variables=None):
        self.calls.append(variables)
        if self.fail:
            raise RuntimeError("Cannot query field")
        start = int(variables["cursor"] or 0)
        chunk = self.nodes[start : start + 2]
        more = start + 2 < len(self.nodes)
        return {
            "models": {
                "pageInfo": {"hasNextPage": more, "endCursor": str(start + 2)},
                "edges": [{"node": n} for n in chunk],
            }
        }


def node(name, created, heartbeat=None, count=0, active=0):
    runs = {"edges": [{"node": {"heartbeatAt": heartbeat}}]} if heartbeat else {"edges": []}
    return {"name": name, "createdAt": created, "runs": runs, "runCount": count, "activeRuns": active}


class FakeApi:
    service_nodes = []
    service_fails = False
    runs_calls = []

    def __init__(self, api_key=None):
        self.api_key = api_key
        self.viewer = NS(entity="me", teams=["team-a", "me"])
        self._service_api = FakeService(self.service_nodes, self.service_fails)

    def projects(self, entity, per_page=50):
        return [
            NS(name=n["name"], _attrs={"createdAt": n["createdAt"]}) for n in self.service_nodes
        ]

    def runs(self, path, order="+created_at", per_page=50):
        type(self).runs_calls.append((path, order))
        return [
            NS(id="id1", name=f"{path}:r1", state="finished", heartbeat_at="2024-01-02T00:00:00",
               created_at="2024-01-01T22:00:00", group="g1"),
            NS(id="id2", name="r2", state="running", heartbeat_at="2024-01-03T00:00:00",
               created_at="2024-01-03T00:00:00", group=None),
        ]


def test_client_maps_wandb_objects_to_names(monkeypatch):
    monkeypatch.setattr(wandb, "Api", FakeApi)
    FakeApi.service_nodes, FakeApi.service_fails, FakeApi.runs_calls = [], False, []
    client = WandbApiClient("key")
    assert client._api.api_key == "key"
    assert client.list_entities() == ["me", "team-a"]  # personal first, deduplicated


def test_runs_are_requested_newest_activity_first_and_carry_state(monkeypatch):
    monkeypatch.setattr(wandb, "Api", FakeApi)
    FakeApi.runs_calls = []
    runs = list(WandbApiClient("key").iter_runs("me", "p"))
    assert FakeApi.runs_calls == [("me/p", "-heartbeat_at")]  # sorted by the server
    assert runs[0] == RunInfo(
        "id1", "me/p:r1", "finished", "2024-01-02T00:00:00", "2024-01-01T22:00:00", "g1"
    )
    assert runs[1].group == ""  # a missing group is an empty string, not None
    assert runs[0].version == "2024-01-02T00:00:00"  # finished: cacheable
    assert runs[1].version is None  # running: never cached


def test_projects_sorted_by_latest_run_activity_across_pages(monkeypatch):
    monkeypatch.setattr(wandb, "Api", FakeApi)
    FakeApi.service_fails = False
    FakeApi.service_nodes = [
        node("old-empty", "2020-01-01T00:00:00"),
        node("busy", "2021-01-01T00:00:00", heartbeat="2024-06-01T12:00:00", count=40, active=2),
        node("quiet", "2022-01-01T00:00:00", heartbeat="2022-02-01T00:00:00", count=5),
        node("new-empty", "2023-05-05T00:00:00"),
        node("recent", "2019-01-01T00:00:00", heartbeat="2024-07-01T00:00:00Z", count=1),
    ]
    client = WandbApiClient("key")
    projects = list(client.iter_projects("me"))
    assert [p.name for p in projects] == ["recent", "busy", "new-empty", "quiet", "old-empty"]
    # 5 projects at 2 per page = 3 requests, not one per project
    calls = client._api._service_api.calls
    assert len(calls) == 3
    assert json.loads(calls[0]["running"]) == {"state": "running"}
    busy = projects[1]
    assert (busy.last_run_at, busy.run_count, busy.active_runs) == ("2024-06-01T12:00:00", 40, 2)
    # empty projects: no "last run" (their creation date only affects ordering)
    assert projects[2] == ProjectInfo("new-empty", "", 0, 0)


def test_projects_fall_back_to_creation_time_if_activity_query_fails(monkeypatch):
    monkeypatch.setattr(wandb, "Api", FakeApi)
    FakeApi.service_fails = True
    FakeApi.service_nodes = [node("a", "2020-01-01T00:00:00"), node("b", "2022-01-01T00:00:00")]
    projects = list(WandbApiClient("key").iter_projects("me"))
    assert projects == [ProjectInfo("b"), ProjectInfo("a")]  # counts unknown: None
    FakeApi.service_fails = False


class FakeRun:
    def __init__(self):
        self.history_calls = []
        self.summary_metrics = {"loss": 1.0, "a/b": 2, "_step": 9, "img": {"_type": "image"}}

    def console_logs(self):
        return [NS(content="line 1"), NS(content="line 2")]

    def history(self, **kwargs):
        self.history_calls.append(kwargs)
        if kwargs.get("stream") == "system":
            return [{"_runtime": 1, "system.cpu": 3.0}]
        return [{"_step": 0, "loss": 1.0}, {"_step": 1}, {"_step": 2, "loss": 0.5}]


def test_client_run_data_methods(monkeypatch):
    from wandb_cli_tool.api import RunRef

    run = FakeRun()
    paths = []

    class Api(FakeApi):
        def run(self, path):
            paths.append(path)
            return run

    monkeypatch.setattr(wandb, "Api", Api)
    client = WandbApiClient("key")
    ref = RunRef("me", "proj", "abc")

    assert list(client.console_lines(ref)) == ["line 1", "line 2"]
    assert client.metric_keys(ref) == ["a/b", "loss"]
    assert client.metric_series(ref, "loss", "_step") == [(0.0, 1.0), (2.0, 0.5)]
    assert run.history_calls[-1]["keys"] == ["loss"] and run.history_calls[-1]["x_axis"] == "_step"
    assert client.system_rows(ref) == [{"_runtime": 1, "system.cpu": 3.0}]
    assert run.history_calls[-1]["stream"] == "system"
    assert paths == ["me/proj/abc"]  # the run object is fetched once and cached


class MemoizingApi(FakeApi):
    """Like the real `Api.runs()`: the first listing for a path/order is kept, with the runs it
    fetched, and handed back on every later call."""

    available = [NS(id="id1", name="r1", state="finished", heartbeat_at="2024-01-02T00:00:00",
                    created_at="2024-01-01T00:00:00", group=None)]

    def __init__(self, api_key=None):
        super().__init__(api_key)
        self._runs = {}

    def runs(self, path, order="+created_at", per_page=50):
        key = path + str(None) + str(order)
        if not self._runs.get(key):
            self._runs[key] = list(type(self).available)  # the pages are fetched once, here
        return self._runs[key]


def test_a_run_started_after_the_first_listing_shows_up_in_the_next_one(monkeypatch):
    monkeypatch.setattr(wandb, "Api", MemoizingApi)
    MemoizingApi.available = [MemoizingApi.available[0]]
    client = WandbApiClient("key")
    assert [r.id for r in client.iter_runs("me", "p")] == ["id1"]
    MemoizingApi.available = [
        *MemoizingApi.available,
        NS(id="id9", name="new", state="running", heartbeat_at="2024-01-05T00:00:00",
           created_at="2024-01-05T00:00:00", group=None),
    ]  # a run is started while the app is open
    assert [r.id for r in client.iter_runs("me", "p")] == ["id1", "id9"]


def test_only_the_listing_of_that_project_is_dropped(monkeypatch):
    monkeypatch.setattr(wandb, "Api", MemoizingApi)
    client = WandbApiClient("key")
    list(client.iter_runs("me", "p"))
    client._api._runs["me/other" + "None" + "-heartbeat_at"] = ["kept"]
    list(client.iter_runs("me", "p"))
    assert client._api._runs["me/other" + "None" + "-heartbeat_at"] == ["kept"]
