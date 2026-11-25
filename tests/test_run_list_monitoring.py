"""Finding runs (filter), watching running ones (notifications) and copying a run's link."""

from dataclasses import replace

from textual.widgets import DataTable, Input

from conftest import FakeClient
from wandb_cli_tool import config
from wandb_cli_tool.api import RunInfo
from wandb_cli_tool.app import WandbCliApp
from wandb_cli_tool.config import Credentials
from wandb_cli_tool.screens import runs as runs_module
from wandb_cli_tool.screens.run import RunScreen
from wandb_cli_tool.screens.runs import RunsScreen


async def settle(pilot, n=4):
    for _ in range(n):
        await pilot.pause()


def make(client=FakeClient):
    config.save(Credentials("k", "me"))
    return WandbCliApp(client_factory=client)


async def open_runs(pilot):
    await settle(pilot, 4)
    await pilot.press("enter")
    await settle(pilot, 5)
    assert isinstance(pilot.app.screen, RunsScreen)
    return pilot.app.screen


def names(screen):
    table = screen.query_one("#items", DataTable)
    return [str(table.get_row_at(i)[0]).removeprefix("✓ ") for i in range(table.row_count)]


def status(screen):
    return str(screen.query_one("#status").render())


# ---- filter -----------------------------------------------------------------------------------
async def test_the_filter_narrows_the_list_as_you_type():
    app = make()
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_runs(pilot)
        box = screen.query_one("#filter", Input)
        assert not box.display  # hidden until asked for
        await pilot.press("slash")
        await settle(pilot)
        assert box.display and app.focused is box
        await pilot.press(*"sweep")
        await settle(pilot)
        assert names(screen) == ["run-1", "run-3"]  # their groups are sweep-a / sweep-b
        assert "filter “sweep”: 2 shown" in status(screen)
        await pilot.press(*" failed")
        await settle(pilot)
        assert names(screen) == ["run-3"]  # every word must match: sweep AND failed


async def test_filter_matches_tags_and_ids_and_ignores_case():
    app = make()
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_runs(pilot)
        await pilot.press("slash", *"BASELINE")
        await settle(pilot)
        assert names(screen) == ["run-1"]
        screen.query_one("#filter", Input).value = "id2"
        await settle(pilot)
        assert names(screen) == ["run-2"]
        screen.query_one("#filter", Input).value = "nothing like this"
        await settle(pilot)
        assert names(screen) == []


async def test_enter_keeps_the_filter_and_escape_clears_it_before_going_back():
    app = make()
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_runs(pilot)
        await pilot.press("slash", *"sweep", "enter")
        await settle(pilot)
        assert app.focused is screen.query_one("#items", DataTable) and names(screen) == ["run-1", "run-3"]
        await pilot.press("down", "enter")  # opens the filtered list's second run: run-3, not run-2
        await settle(pilot, 6)
        assert isinstance(app.screen, RunScreen) and app.screen.run_name == "run-3"
        await pilot.press("escape")
        await settle(pilot)
        assert app.screen is screen and names(screen) == ["run-1", "run-3"]  # still filtered
        await pilot.press("escape")  # first Esc: clear the filter
        await settle(pilot)
        assert app.screen is screen and names(screen) == ["run-1", "run-2", "run-3"]
        assert not screen.query_one("#filter", Input).display
        await pilot.press("escape")  # the next one goes back
        await settle(pilot)
        assert app.screen is not screen


async def test_select_all_shown_only_takes_the_runs_the_filter_shows():
    app = make()
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_runs(pilot)
        await pilot.press("slash", *"v2", "enter")  # tags: run-1 and run-3
        await settle(pilot)
        await pilot.press("ctrl+a")
        await settle(pilot)
        assert screen.selected == ["id1", "id3"]
        await pilot.press("ctrl+a")  # again: nothing new
        await settle(pilot)
        assert screen.selected == ["id1", "id3"]


async def test_the_filter_survives_a_refresh():
    app = make()
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_runs(pilot)
        await pilot.press("slash", *"failed", "enter")
        await settle(pilot)
        await pilot.press("r")
        await settle(pilot, 8)
        assert names(screen) == ["run-3"]


# ---- watching ---------------------------------------------------------------------------------
class Evolving(FakeClient):
    """run-1 is running at first; from the second listing on it has finished and a new run appeared."""

    listings = 0

    def iter_runs(self, entity, project):
        Evolving.listings += 1
        for r in super().iter_runs(entity, project):
            if Evolving.listings > 1 and r.id == "id1":
                r = replace(r, state="finished")
            yield r
        if Evolving.listings > 1:
            yield RunInfo("id9", "run-9", "running", "2024-05-02T12:00:00", "2024-05-02T10:30:00")


async def test_a_run_that_finishes_or_starts_is_announced(monkeypatch):
    Evolving.listings = 0
    monkeypatch.setattr(runs_module, "WATCH_SECONDS", 0.3)
    app = make(Evolving)
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_runs(pilot)
        assert "watching 1 running" in status(screen)
        for _ in range(40):
            await pilot.pause(0.1)
            if any("run-9 started" in n.message for n in app._notifications):
                break
        messages = [n.message for n in app._notifications]
        assert "run-1 finished" in messages and "run-9 started" in messages
        assert "run-9" in names(screen)


async def test_nothing_is_watched_unless_the_list_is_on_screen_and_something_runs(monkeypatch):
    Evolving.listings = 0
    monkeypatch.setattr(runs_module, "WATCH_SECONDS", 0.2)
    app = make(Evolving)
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_runs(pilot)
        await pilot.press("enter")  # into a run: the list is behind it now
        await settle(pilot, 6)
        before = Evolving.listings
        await pilot.pause(0.8)
        assert Evolving.listings == before  # no background checks from behind another screen


async def test_a_failing_background_check_is_silent(monkeypatch):
    monkeypatch.setattr(runs_module, "WATCH_SECONDS", 0.2)

    class Breaks(FakeClient):
        calls = 0

        def iter_runs(self, entity, project):
            Breaks.calls += 1
            if Breaks.calls > 1:
                raise RuntimeError("network down")
            yield from super().iter_runs(entity, project)

    Breaks.calls = 0
    app = make(Breaks)
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_runs(pilot)
        await pilot.pause(0.8)
        assert Breaks.calls > 1 and names(screen) == ["run-1", "run-2", "run-3"]  # list untouched
        assert "Error" not in status(screen)


# ---- copy link --------------------------------------------------------------------------------
async def test_y_copies_the_link_of_the_run_under_the_cursor(monkeypatch):
    app = make()
    copied = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    async with app.run_test(size=(130, 40)) as pilot:
        await open_runs(pilot)
        await pilot.press("down", "y")
        await settle(pilot, 6)
        assert copied == ["https://wandb.example/me/alpha/runs/id2"]
        assert any("Link copied: https://wandb.example/me/alpha/runs/id2" in n.message for n in app._notifications)


async def test_y_in_a_run_copies_its_link_too(monkeypatch):
    app = make()
    copied = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    async with app.run_test(size=(130, 40)) as pilot:
        await open_runs(pilot)
        await pilot.press("enter")
        await settle(pilot, 6)
        assert isinstance(app.screen, RunScreen)
        await pilot.press("y")
        await settle(pilot, 6)
        assert copied == ["https://wandb.example/me/alpha/runs/id1"]


# ---- following a live log ---------------------------------------------------------------------
from wandb_cli_tool.screens import logs as logs_module
from wandb_cli_tool.screens.logs import LogsScreen


class Growing(FakeClient):
    """Each listing of the log has one more line than the one before."""

    reads = 0

    def console_lines(self, ref):
        Growing.reads += 1
        yield from (f"line {i}" for i in range(2 + Growing.reads))


def log_text(screen):
    return "\n".join(strip.text.rstrip() for strip in screen.query_one("#log").lines)


async def open_logs(pilot, row=0):
    await open_runs(pilot)
    await pilot.press(*["down"] * row, "enter")
    await settle(pilot, 6)
    await pilot.press("enter")  # the menu's first entry: Logs
    await settle(pilot, 6)
    assert isinstance(pilot.app.screen, LogsScreen)
    return pilot.app.screen


async def test_the_log_of_a_running_run_is_followed_and_only_new_lines_are_added(monkeypatch):
    Growing.reads = 0
    monkeypatch.setattr(logs_module, "FOLLOW_SECONDS", 0.2)
    app = make(Growing)
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_logs(pilot, 0)  # run-1 is running
        assert screen.live and screen.follow and "following" in str(screen.query_one("#status").render())
        for _ in range(40):
            await pilot.pause(0.1)
            if screen._lines >= 6:
                break
        assert screen._lines >= 6
        lines = log_text(screen).splitlines()
        assert lines == [f"line {i}" for i in range(len(lines))]  # no line twice, none skipped


async def test_f_stops_and_resumes_following(monkeypatch):
    Growing.reads = 0
    monkeypatch.setattr(logs_module, "FOLLOW_SECONDS", 0.2)
    app = make(Growing)
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_logs(pilot, 0)
        await pilot.press("f")
        await settle(pilot)
        assert not screen.follow and "not following" in str(screen.query_one("#status").render())
        reads = Growing.reads
        await pilot.pause(0.7)
        assert Growing.reads == reads  # not asked again
        await pilot.press("f")
        await settle(pilot, 6)
        assert screen.follow and Growing.reads > reads


async def test_a_finished_runs_log_is_not_followed_and_has_no_follow_key():
    Growing.reads = 0
    app = make(Growing)
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_logs(pilot, 1)  # run-2 finished
        assert not screen.live and not screen.follow and screen.check_action("toggle_follow", ()) is False
        assert "following" not in str(screen.query_one("#status").render())


async def test_r_reloads_the_whole_log():
    Growing.reads = 0
    app = make(Growing)
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_logs(pilot, 1)
        first = log_text(screen).splitlines()
        await pilot.press("r")
        await settle(pilot, 8)
        again = log_text(screen).splitlines()
        assert len(again) == len(first) + 1 and again[: len(first)] == first  # the log grew by a line; nothing doubled
