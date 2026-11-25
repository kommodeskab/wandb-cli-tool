from textual.widgets import DataTable, Input, OptionList, Select, Static

from wandb_cli_tool import config
from wandb_cli_tool.api import RunRef
from wandb_cli_tool.config import Credentials
from wandb_cli_tool.screens.login import EntityScreen, LoginScreen
from wandb_cli_tool.screens.projects import ProjectsScreen
from wandb_cli_tool.screens.runs import RunsScreen


def prompts(screen, selector="#items"):
    """Names in a list: first column of a table, or the options of an OptionList."""
    widget = screen.query_one(selector)
    if isinstance(widget, DataTable):
        return [str(widget.get_row_at(i)[0]) for i in range(widget.row_count)]
    return [str(widget.get_option_at_index(i).prompt) for i in range(widget.option_count)]


async def log_in(pilot, key="secret"):
    app = pilot.app
    await pilot.pause()
    assert isinstance(app.screen, LoginScreen)
    app.screen.query_one("#api-key", Input).value = key
    await pilot.press("enter")
    await pilot.pause()
    await pilot.pause()


async def test_first_run_shows_login_with_entity_dropdown(make_app):
    app = make_app()
    async with app.run_test() as pilot:
        await log_in(pilot)
        select = app.screen.query_one("#entity", Select)
        assert app.screen.query_one("#entity-step").display
        assert select.value == "me"
        assert [v for _, v in select._options if v is not Select.BLANK] == [
            "me", "team-a", "team-b"
        ]


async def test_invalid_key_shows_error_and_no_entity_step(make_app):
    app = make_app()
    async with app.run_test() as pilot:
        await log_in(pilot, key="bad")
        assert not app.screen.query_one("#entity-step").display
        assert "invalid API key" in str(app.screen.query_one("#message", Static).render())
        assert config.load() is None


async def test_login_stores_credentials_and_lists_projects(make_app):
    app = make_app()
    async with app.run_test() as pilot:
        await log_in(pilot)
        await pilot.click("#submit")
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, ProjectsScreen)
        assert prompts(app.screen) == ["alpha", "beta"]
    assert config.load() == Credentials("secret", "me")


async def test_stored_credentials_skip_login(make_app):
    config.save(Credentials("secret", "team-a"))
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, ProjectsScreen)
        assert prompts(app.screen) == ["proj-x"]


async def test_arrow_keys_enter_project_and_escape_back(make_app):
    config.save(Credentials("secret", "me"))
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.press("down", "up", "enter")  # alpha
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, RunsScreen)
        assert prompts(app.screen) == ["run-1", "run-2", "run-3"]
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ProjectsScreen)


async def test_down_selects_second_project(make_app, monkeypatch):
    config.save(Credentials("secret", "me"))
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.press("down", "enter")  # beta, which has no runs
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, RunsScreen)
        assert app.screen.project == "beta"
        assert "No runs" in str(app.screen.query_one("#status", Static).render())


async def test_change_entity_reloads_projects_and_persists(make_app):
    config.save(Credentials("secret", "me"))
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, EntityScreen)
        assert prompts(app.screen, "#entities") == ["me", "team-a", "team-b"]
        await pilot.press("down", "enter")  # team-a
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, ProjectsScreen)
        assert prompts(app.screen) == ["proj-x"]
    assert config.load() == Credentials("secret", "team-a")


async def test_change_entity_escape_cancels(make_app):
    config.save(Credentials("secret", "me"))
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ProjectsScreen)
    assert config.load() == Credentials("secret", "me")


async def test_logout_clears_credentials_and_shows_login(make_app):
    config.save(Credentials("secret", "me"))
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)
        assert config.load() is None
        # logging in again works after logout
        await log_in(pilot)
        await pilot.click("#submit")
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, ProjectsScreen)


async def test_logout_from_runs_screen(make_app):
    config.save(Credentials("secret", "me"))
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)
        assert len(app.screen_stack) == 2  # default screen + login
        assert config.load() is None


# ---- list cache (stale-while-revalidate) and ordering ----------------------------


import threading

from wandb_cli_tool.api import DiskCache
from wandb_cli_tool.api import ProjectInfo, RunInfo as _RunInfo


class GatedClient:
    """FakeClient whose project/run listing blocks until the test releases it."""

    gate = threading.Event()
    fail = False

    def __init__(self, api_key):
        from conftest import FakeClient

        self.inner = FakeClient(api_key)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def iter_projects(self, entity):
        self.gate.wait(5)
        if self.fail:
            raise RuntimeError("network down")
        yield from [ProjectInfo(n) for n in ["fresh-1", "fresh-2", "alpha"]]

    def iter_runs(self, entity, project):
        self.gate.wait(5)
        yield _RunInfo("idA", "run-A", "finished", "2024-01-01T00:00:00")
        yield _RunInfo("idB", "run-B", "running", "2024-01-02T00:00:00")


async def test_cached_project_list_is_shown_instantly_then_replaced(make_app):
    from wandb_cli_tool.app import WandbCliApp

    config.save(Credentials("k", "me"))
    DiskCache().set("list", "projects:me", [["stale-1", ["stale-1", "", "", ""], ""], ["alpha", ["alpha", "", "", ""], ""]])
    GatedClient.gate, GatedClient.fail = threading.Event(), False
    app = WandbCliApp(client_factory=GatedClient)
    async with app.run_test() as pilot:
        await settle(pilot)
        assert prompts(app.screen) == ["stale-1", "alpha"]  # from disk, fetch still blocked
        assert "cached" in str(app.screen.query_one("#status", Static).render())
        await pilot.press("down")  # user highlights "alpha" while refresh is pending
        GatedClient.gate.set()
        await settle(pilot, 5)
        assert prompts(app.screen) == ["fresh-1", "fresh-2", "alpha"]
        assert app.screen.query_one("#items", DataTable).cursor_row == 2  # still on alpha, which moved
        assert "cached" not in str(app.screen.query_one("#status", Static).render())
    # and the fresh list was written back for next time
    assert [r[0] for r in DiskCache().get("list", "projects:me")] == ["fresh-1", "fresh-2", "alpha"]


async def test_fetch_error_keeps_showing_cached_list(make_app):
    from wandb_cli_tool.app import WandbCliApp

    config.save(Credentials("k", "me"))
    DiskCache().set("list", "projects:me", [["stale-1", ["stale-1", "", "", ""], ""]])
    GatedClient.gate, GatedClient.fail = threading.Event(), True
    GatedClient.gate.set()
    app = WandbCliApp(client_factory=GatedClient)
    async with app.run_test() as pilot:
        await settle(pilot, 5)
        assert prompts(app.screen) == ["stale-1"]
        status = str(app.screen.query_one("#status", Static).render())
        assert "network down" in status and "cached" in status


async def test_lists_keep_server_order_and_runs_pass_cache_version(make_app):
    from wandb_cli_tool.app import WandbCliApp

    config.save(Credentials("k", "me"))
    GatedClient.gate, GatedClient.fail = threading.Event(), False
    GatedClient.gate.set()
    app = WandbCliApp(client_factory=GatedClient)
    async with app.run_test() as pilot:
        await settle(pilot, 5)
        assert prompts(app.screen) == ["fresh-1", "fresh-2", "alpha"]  # order preserved
        await pilot.press("down", "down", "enter")  # alpha
        await settle(pilot, 5)
        assert isinstance(app.screen, RunsScreen)
        assert prompts(app.screen) == ["run-A", "run-B"]
        # finished run carries its version into RunRef; the running one does not
        await pilot.press("enter")
        await settle(pilot)
        assert app.screen.ref.version == "2024-01-01T00:00:00"
        await pilot.press("escape", "down", "enter")  # back to runs, pick run-B
        await settle(pilot)
        assert app.screen.ref.version is None


async def test_logout_clears_the_cache(make_app):
    config.save(Credentials("k", "me"))
    DiskCache().set("list", "projects:me", [["x", ["x", "", "", ""], ""]])
    app = make_app()
    async with app.run_test() as pilot:
        await settle(pilot)
        assert DiskCache().get("list", "projects:me") is not None
        await pilot.press("l")
        await settle(pilot)
    assert DiskCache().get("list", "projects:me") is None


async def settle(pilot, n=3):
    for _ in range(n):
        await pilot.pause()


async def test_cached_rows_with_an_outdated_layout_are_ignored(make_app):
    config.save(Credentials("k", "me"))
    DiskCache().set("list", "projects:me", [["old", ["old"], ""], "garbage", ["k", "v", ""]])
    app = make_app()
    async with app.run_test() as pilot:
        await settle(pilot, 4)
        assert prompts(app.screen) == ["alpha", "beta"]  # shows the fresh list, no crash


def cell_text(table, row):
    return [str(c) for c in table.get_row_at(row)]


async def test_project_list_shows_last_run_run_count_and_active_runs(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test() as pilot:
        await settle(pilot, 4)
        table = app.screen.query_one("#items", DataTable)
        assert [str(c.label) for c in table.columns.values()] == [
            "Project", "Last run", "Runs", "Active"
        ]
        assert cell_text(table, 0) == ["alpha", "2024-05-01 12:00", "3", "1"]
        assert cell_text(table, 1) == ["beta", "2024-03-01 00:00", "7", "0"]


async def test_run_list_shows_status_start_runtime_group_and_id(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test() as pilot:
        await settle(pilot, 4)
        await pilot.press("enter")
        await settle(pilot, 4)
        table = app.screen.query_one("#items", DataTable)
        assert [str(c.label) for c in table.columns.values()] == [
            "Run", "Status", "Started", "Run time", "Group", "Tags", "ID"
        ]
        # running: runtime is measured until now, so only check its shape here
        running = cell_text(table, 0)
        assert running[:3] == ["run-1", "● running", "2024-05-01 10:30"]
        assert running[3].endswith(("d", "h", "m", "s")) or "d " in running[3] or "h " in running[3]
        assert running[4:] == ["sweep-a", "baseline, v2", "id1"]
        assert cell_text(table, 1) == ["run-2", "finished", "2024-05-01 08:00", "1h 00m", "—", "—", "id2"]
        assert cell_text(table, 2) == ["run-3", "failed", "2024-04-30 09:00", "5s", "sweep-b", "v2", "id3"]


async def test_duplicate_rows_from_shifting_pages_are_shown_once(make_app):
    from wandb_cli_tool.app import WandbCliApp

    class Dupes(GatedClient):
        def iter_projects(self, entity):
            yield from [ProjectInfo("a"), ProjectInfo("b"), ProjectInfo("a")]

    config.save(Credentials("k", "me"))
    app = WandbCliApp(client_factory=Dupes)
    async with app.run_test() as pilot:
        await settle(pilot, 4)
        assert prompts(app.screen) == ["a", "b"]


async def test_pinning_projects_moves_them_first_keeps_cursor_and_persists(make_app):
    from wandb_cli_tool.app import WandbCliApp

    class ThreeProjects(GatedClient):
        def iter_projects(self, entity):
            if entity == "team-a":
                yield from [ProjectInfo("proj-x"), ProjectInfo("proj-y")]
                return
            yield from [ProjectInfo("alpha"), ProjectInfo("beta"), ProjectInfo("gamma")]

    config.save(Credentials("k", "me"))
    app = WandbCliApp(client_factory=ThreeProjects)
    async with app.run_test() as pilot:
        await settle(pilot, 4)
        table = app.screen.query_one("#items", DataTable)
        assert prompts(app.screen) == ["alpha", "beta", "gamma"]
        await pilot.press("down", "p")  # pin beta -> jumps to top; cursor stays on row 1
        await settle(pilot)
        assert prompts(app.screen) == ["★ beta", "alpha", "gamma"]
        assert table.cursor_row == 1 and prompts(app.screen)[table.cursor_row] == "alpha"
        await pilot.press("down", "p")  # pin gamma -> second pin after beta
        await settle(pilot)
        assert prompts(app.screen) == ["★ beta", "★ gamma", "alpha"]
        assert app.preferences.get("pins")["projects"] == {"me": ["beta", "gamma"]}
        await pilot.press("r")  # refresh keeps pinned order
        await settle(pilot, 4)
        assert prompts(app.screen) == ["★ beta", "★ gamma", "alpha"]
        # switching entity uses that entity's own pins
        await pilot.press("e")
        await settle(pilot)
        await pilot.press("down", "enter")  # team-a
        await settle(pilot, 4)
        assert prompts(app.screen) == ["proj-x", "proj-y"]
        await pilot.press("down", "p")
        await settle(pilot)
        assert prompts(app.screen) == ["★ proj-y", "proj-x"]
        # unpin works too
        await pilot.press("up", "p")
        await settle(pilot)
        assert prompts(app.screen) == ["proj-x", "proj-y"]

    # a new session starts with the saved pins (switch back to me to verify me's pins persisted)
    config.save(Credentials("k", "me"))
    app2 = WandbCliApp(client_factory=ThreeProjects)
    async with app2.run_test() as pilot:
        await settle(pilot, 4)
        assert prompts(app2.screen) == ["★ beta", "★ gamma", "alpha"]
        assert app2.screen.pinned == ["beta", "gamma"]


async def test_cached_and_hostile_project_names_work_with_pins(make_app):
    from wandb_cli_tool.app import WandbCliApp

    class HostileProjects(GatedClient):
        def iter_projects(self, entity):
            self.gate.wait(5)
            yield from [ProjectInfo("alpha"), ProjectInfo("[bold]proj [/x]")]

    config.save(Credentials("k", "me"))
    DiskCache().set(
        "list",
        "projects:me",
        [["alpha", ["alpha", "", "", ""], ""], ["[bold]proj [/x]", ["[bold]proj [/x]", "", "", ""], ""]],
    )
    GatedClient.gate, GatedClient.fail = threading.Event(), False
    app = WandbCliApp(client_factory=HostileProjects)
    async with app.run_test() as pilot:
        await settle(pilot)
        assert prompts(app.screen) == ["alpha", "[bold]proj [/x]"]
        await pilot.press("down", "p")  # pin hostile bracketed name
        await settle(pilot)
        assert prompts(app.screen) == ["★ [bold]proj [/x]", "alpha"]
        GatedClient.gate.set()  # fresh replacement keeps pinned project first
        await settle(pilot, 5)
        assert prompts(app.screen) == ["★ [bold]proj [/x]", "alpha"]

    # new session reads cache with pin already applied
    GatedClient.gate = threading.Event()
    app2 = WandbCliApp(client_factory=HostileProjects)
    async with app2.run_test() as pilot:
        await settle(pilot)
        assert prompts(app2.screen) == ["★ [bold]proj [/x]", "alpha"]
        GatedClient.gate.set()
        await settle(pilot, 5)
        assert prompts(app2.screen) == ["★ [bold]proj [/x]", "alpha"]
