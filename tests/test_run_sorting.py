import json
import stat
from datetime import datetime, timezone

import pytest
from textual.widgets import DataTable, OptionList

from conftest import FakeClient, FakePlayer
from wandb_cli_tool import config
from wandb_cli_tool.api import RunInfo
from wandb_cli_tool.app import WandbCliApp
from wandb_cli_tool.config import Credentials
from wandb_cli_tool.preferences import PREFS_VERSION, Preferences
from wandb_cli_tool.runsort import (
    COLUMN_LABELS, DEFAULT_DESCENDING, SORT_COLUMNS, SortState, natural_key, sort_items, status_rank,
)
from wandb_cli_tool.screens.browse import Item, items_from_cache
from wandb_cli_tool.screens.dialogs import SortDialog
from wandb_cli_tool.screens.runs import RunsScreen

# ---- pure logic ----------------------------------------------------------------------------


def test_names_sort_naturally_and_ignoring_case():
    names = ["run-10", "Run-2", "run-1", "alpha", "Run-02", "run-100"]
    assert sorted(names, key=natural_key) == ["alpha", "run-1", "Run-2", "Run-02", "run-10", "run-100"]  # Run-2 and Run-02 are equal: input order
    assert natural_key("a1b") < natural_key("a2b") < natural_key("a10b")


def test_status_order_puts_running_first_and_unknown_states_last():
    states = ["killed", "finished", "mystery", "running", "failed", "crashed", "pending"]
    assert sorted(states, key=status_rank) == ["running", "pending", "finished", "failed", "crashed", "killed", "mystery"]


def vo(item, column):
    return item[1]


def names_of(items):
    return [i[0] for i in items]


def test_sort_directions_blanks_last_and_ties_keep_server_order():
    items = [("a", 5.0), ("b", None), ("c", 3600.0), ("d", 5.0), ("e", ""), ("f", 3600.0)]
    assert names_of(sort_items(items, SortState("runtime", True), vo)) == ["c", "f", "a", "d", "b", "e"]
    assert names_of(sort_items(items, SortState("runtime", False), vo)) == ["a", "d", "c", "f", "b", "e"]  # blanks last either way
    assert items[0] == ("a", 5.0)  # the input is not modified


def test_activity_is_the_servers_order_or_its_reverse():
    items = [("new", 0), ("mid", 0), ("old", 0)]
    assert names_of(sort_items(items, SortState("activity", True), vo)) == ["new", "mid", "old"]
    assert names_of(sort_items(items, SortState("activity", False), vo)) == ["old", "mid", "new"]


def test_text_columns_use_natural_order():
    items = [("1", "run-10"), ("2", "run-2"), ("3", None)]
    assert names_of(sort_items(items, SortState("name", False), vo)) == ["2", "1", "3"]
    assert names_of(sort_items(items, SortState("name", True), vo)) == ["1", "2", "3"]


def test_choosing_a_column_flips_the_same_one_and_starts_new_ones_in_their_natural_direction():
    s = SortState()
    assert s.is_server_order and s.label == "Last activity ↓"
    assert s.choose("name") == SortState("name", False)  # text: A to Z first
    assert s.choose("name").choose("name") == SortState("name", True)
    assert s.choose("started") == SortState("started", True)  # newest first
    assert s.choose("runtime") == SortState("runtime", True)  # longest first
    assert s.choose("activity") == SortState("activity", False)  # same column: reverse
    assert DEFAULT_DESCENDING == {"activity", "started", "runtime"}
    assert [c for c, _ in SORT_COLUMNS][0] == "activity" and COLUMN_LABELS["runtime"] == "Run time"


@pytest.mark.parametrize("bad", [None, 3, "x", {}, {"column": "nope", "descending": True}, {"column": "name"}, {"column": "name", "descending": "yes"}])
def test_a_damaged_saved_sort_falls_back_to_the_default(bad):
    assert SortState.from_json(bad) == SortState()


def test_sort_state_roundtrips():
    s = SortState("group", True)
    assert SortState.from_json(json.loads(json.dumps(s.to_json()))) == s


def test_cached_rows_from_before_sorting_existed_still_load():
    old = ["id1", ["n", "finished", "—", "5s", "—", "—", "id1"], "ver"]
    new = ["id2", ["n", "finished", "—", "5s", "—", "—", "id2"], "", ["n", 2, None, 5.0, None, None, "id2"]]
    items = items_from_cache([old, new, ["id3", ["too", "few"], ""]], 7)
    assert [i.key for i in items] == ["id1", "id2"]  # the malformed row is dropped
    assert items[0].sort == () and items[1].sort == ("n", 2, None, 5.0, None, None, "id2")
    assert Item.from_json(items[1].to_json(), 7) == items[1]


def test_rows_without_raw_sort_values_fall_back_to_their_cells():
    row = Item("id", ("run-2", "failed", "2024-05-01 10:30", "1h 00m", "grp", "a, b", "id"))
    f = RunsScreen._sort_value
    assert f(row, "name") == "run-2" and f(row, "status") == status_rank("failed")
    assert f(row, "started") == "2024-05-01 10:30" and f(row, "group") == "grp" and f(row, "tags") == "a, b"
    assert f(row, "runtime") is None  # unknown without the raw number
    blank = Item("id", ("r", "—", "—", "—", "—", "—", "id"))
    assert f(blank, "group") is None and f(blank, "started") is None


# ---- preferences file -------------------------------------------------------------------------


def test_preferences_roundtrip_keep_other_values_and_are_private(tmp_path):
    prefs = Preferences(tmp_path / "p" / "prefs.json")
    assert prefs.get("a") is None and prefs.get("a", 7) == 7
    assert prefs.set("a", {"x": 1}) and prefs.set("b", [1, 2])
    assert prefs.get("a") == {"x": 1} and prefs.get("b") == [1, 2]
    assert stat.S_IMODE(prefs.path.stat().st_mode) == 0o600 and not list(tmp_path.rglob("*.tmp"))


def test_unreadable_or_foreign_preference_files_are_ignored_then_replaced(tmp_path):
    prefs = Preferences(tmp_path / "prefs.json")
    prefs.path.write_text("{nope")
    assert prefs.get("a") is None
    prefs.path.write_text(json.dumps({"version": PREFS_VERSION + 1, "values": {"a": 1}}))
    assert prefs.get("a") is None
    assert prefs.set("a", 2) and prefs.get("a") == 2


def test_preferences_never_raise(tmp_path):
    (tmp_path / "file").write_text("a file, not a folder")
    assert Preferences(tmp_path / "file" / "prefs.json").set("a", 1) is False
    assert Preferences(tmp_path / "ok.json").set("a", object()) is False  # not JSON-serialisable


def test_default_preferences_location_follows_the_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(tmp_path / "cfg"))
    assert Preferences().path == tmp_path / "cfg" / "preferences.json"


# ---- the runs screen ---------------------------------------------------------------------------


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


class SortRuns(FakeClient):
    """Runs in server order (most recently active first), chosen to expose the tricky cases."""

    def __init__(self, api_key):
        super().__init__(api_key)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        self.RUNS = {("me", "alpha"): [
            RunInfo("id1", "run-10", "finished", "2024-05-05T10:00:00", "2024-05-05T09:00:00", "beta", ("x", "y")),   # 1 h
            RunInfo("id2", "Run-2", "failed", "2024-05-04T12:30:00", "2024-05-04T12:00:00", "", ()),               # 30 min
            RunInfo("id3", "alpha", "finished", "2024-05-03T10:00:05", "2024-05-03T10:00:00", "Alpha", ("z",)),     # 5 s
            RunInfo("id4", "run-1", "running", iso(now), iso(now), "beta", ("x",)),                                 # just started
            RunInfo("id5", "zeta", "killed", "2024-05-01T01:00:00", "2024-05-01T00:00:00", "", ()),               # 1 h
        ]}


SERVER = ["run-10", "Run-2", "alpha", "run-1", "zeta"]
EXPECTED = {
    ("activity", True): SERVER,
    ("activity", False): SERVER[::-1],
    ("name", False): ["alpha", "run-1", "Run-2", "run-10", "zeta"],
    ("name", True): ["zeta", "run-10", "Run-2", "run-1", "alpha"],
    ("status", False): ["run-1", "run-10", "alpha", "Run-2", "zeta"],  # running, finished x2 (server order), failed, killed
    ("started", True): ["run-1", "run-10", "Run-2", "alpha", "zeta"],
    ("started", False): ["zeta", "alpha", "Run-2", "run-10", "run-1"],
    ("runtime", True): ["run-10", "zeta", "Run-2", "alpha", "run-1"],  # 1h and 1h keep server order, by seconds not text
    ("runtime", False): ["run-1", "alpha", "Run-2", "run-10", "zeta"],
    ("group", False): ["alpha", "run-10", "run-1", "Run-2", "zeta"],  # Alpha, beta, beta; the empty groups last
    ("group", True): ["run-10", "run-1", "alpha", "Run-2", "zeta"],  # still last when reversed
    ("tags", False): ["run-1", "run-10", "alpha", "Run-2", "zeta"],  # x, "x, y", z; then none
    ("id", False): ["run-10", "Run-2", "alpha", "run-1", "zeta"],
    ("id", True): ["zeta", "run-1", "alpha", "Run-2", "run-10"],
}


async def runs_app(client=SortRuns, size=(140, 30)):
    config.save(Credentials("k", "me"))
    app = WandbCliApp(client_factory=client, audio_player=FakePlayer())
    return app, app.run_test(size=size)


async def settle(pilot, n=4):
    for _ in range(n):
        await pilot.pause()


async def open_runs(pilot):
    await settle(pilot, 4)
    await pilot.press("enter")  # alpha
    await settle(pilot, 5)
    assert isinstance(pilot.app.screen, RunsScreen)
    return pilot.app.screen


def names(screen):
    table = screen.query_one("#items", DataTable)
    return [str(table.get_row_at(i)[0]) for i in range(table.row_count)]


def status(screen):
    return str(screen.query_one("#status").render())


async def sort_by(pilot, column, descending):
    """Open the dialog and pick `column`, repeating until it is in the wanted direction."""
    screen = pilot.app.screen
    for _ in range(3):
        if screen.sort_state.column == column and screen.sort_state.descending == descending:
            return
        await pilot.press("s")
        await settle(pilot)
        dialog = pilot.app.screen
        assert isinstance(dialog, SortDialog)
        options = dialog.query_one("#sort-columns", OptionList)
        options.highlighted = [c for c, _ in SORT_COLUMNS].index(column)
        await pilot.press("enter")
        await settle(pilot, 3)
    raise AssertionError("could not reach the wanted sort")


@pytest.mark.parametrize("column, descending", list(EXPECTED))
async def test_every_sort_orders_the_runs_as_expected(column, descending):
    app, ctx = await runs_app()
    async with ctx as pilot:
        screen = await open_runs(pilot)
        assert names(screen) == SERVER  # the default is the server's order
        await sort_by(pilot, column, descending)
        assert names(screen) == EXPECTED[(column, descending)]


async def test_the_dialog_shows_the_current_sort_and_picking_the_same_column_reverses_it():
    app, ctx = await runs_app()
    async with ctx as pilot:
        screen = await open_runs(pilot)
        assert "sorted by" not in status(screen)  # nothing special while in the default order
        await pilot.press("s")
        await settle(pilot)
        options = app.screen.query_one("#sort-columns", OptionList)
        labels = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        assert [l.split("  ")[0] for l in labels] == [n for _, n in SORT_COLUMNS]
        assert labels[0] == "Last activity  ↓" and options.highlighted == 0
        options.highlighted = 1
        await pilot.press("enter")
        await settle(pilot, 3)
        assert screen.sort_state == SortState("name", False) and "sorted by Name ↑" in status(screen)
        await pilot.press("s")
        await settle(pilot)
        assert "Name  ↑" in [str(app.screen.query_one("#sort-columns", OptionList).get_option_at_index(1).prompt)]
        app.screen.query_one("#sort-columns", OptionList).highlighted = 1
        await pilot.press("enter")
        await settle(pilot, 3)
        assert screen.sort_state == SortState("name", True) and "sorted by Name ↓" in status(screen)


async def test_escape_in_the_dialog_changes_nothing():
    app, ctx = await runs_app()
    async with ctx as pilot:
        screen = await open_runs(pilot)
        await pilot.press("s")
        await settle(pilot)
        await pilot.press("escape")
        await settle(pilot)
        assert app.screen is screen and screen.sort_state == SortState() and Preferences().get("runs_sort") is None


async def test_the_header_marks_the_sorted_column_and_clicking_a_header_sorts_by_it():
    app, ctx = await runs_app()
    async with ctx as pilot:
        screen = await open_runs(pilot)
        table = screen.query_one("#items", DataTable)
        labels = lambda: [str(c.label) for c in table.columns.values()]
        assert not any("↑" in l or "↓" in l for l in labels())  # activity has no column of its own
        await sort_by(pilot, "runtime", True)
        assert labels()[3] == "Run time ↓" and sum("↓" in l or "↑" in l for l in labels()) == 1
        await pilot.click("#items", offset=(3, 0))  # the first header: "Run"
        await settle(pilot, 3)
        assert screen.sort_state == SortState("name", False) and labels()[0] == "Run ↑"
        await pilot.click("#items", offset=(3, 0))  # again: reverses
        await settle(pilot, 3)
        assert screen.sort_state == SortState("name", True) and names(screen)[0] == "zeta"


async def test_the_highlighted_run_stays_highlighted_when_the_order_changes():
    app, ctx = await runs_app()
    async with ctx as pilot:
        screen = await open_runs(pilot)
        await pilot.press("down", "down")  # alpha
        table = screen.query_one("#items", DataTable)
        assert str(table.get_row_at(table.cursor_row)[0]) == "alpha"
        await sort_by(pilot, "name", False)
        assert str(table.get_row_at(table.cursor_row)[0]) == "alpha" and table.cursor_row == 0
        await pilot.press("enter")  # opens the run that is highlighted, wherever it moved
        await settle(pilot, 4)
        assert app.screen.ref.id == "id3"


async def test_the_sort_is_remembered_for_the_next_time_and_in_other_projects():
    app, ctx = await runs_app()
    async with ctx as pilot:
        screen = await open_runs(pilot)
        await sort_by(pilot, "group", False)
    assert Preferences().get("runs_sort") == {"column": "group", "descending": False}
    app2, ctx2 = await runs_app()
    async with ctx2 as pilot:
        screen = await open_runs(pilot)
        assert names(screen) == EXPECTED[("group", False)] and "sorted by Group ↑" in status(screen)
        assert [str(c.label) for c in screen.query_one("#items", DataTable).columns.values()][4] == "Group ↑"


async def test_refreshing_and_editing_keep_the_chosen_sort():
    app, ctx = await runs_app()
    async with ctx as pilot:
        screen = await open_runs(pilot)
        await sort_by(pilot, "name", False)
        await pilot.press("r")  # reload from the server
        await settle(pilot, 6)
        assert names(screen) == EXPECTED[("name", False)] and "sorted by Name ↑" in status(screen)
        await pilot.press("down", "down", "enter")  # a run
        await settle(pilot, 4)
        await pilot.press("r")  # rename it; the list refreshes in place on return
        await settle(pilot)
        app.screen.query_one("#new-name").value = "aaa first"
        await pilot.press("enter")
        await settle(pilot, 4)
        await pilot.press("escape")
        await settle(pilot, 8)
        assert names(screen)[0] == "aaa first"  # re-sorted with the new name


async def test_a_cached_list_without_raw_sort_values_is_sorted_by_its_cells_then_replaced():
    from wandb_cli_tool.api import DiskCache

    old_rows = [[f"id{i}", [n, "finished", "2024-05-01 10:00", "5s", "—", "—", f"id{i}"], ""] for i, n in ((7, "c-run"), (8, "a-run"), (9, "b-run"))]
    DiskCache().set("list", "runs:me/alpha", old_rows)
    Preferences().set("runs_sort", {"column": "name", "descending": False})
    import threading

    gate = threading.Event()

    class Slow(SortRuns):
        def iter_runs(self, entity, project):
            gate.wait(5)
            yield from super().iter_runs(entity, project)

    app, ctx = await runs_app(Slow)
    async with ctx as pilot:
        screen = await open_runs(pilot)
        assert names(screen) == ["a-run", "b-run", "c-run"]  # the old cache, sorted by what it shows
        gate.set()
        await settle(pilot, 8)
        assert names(screen) == EXPECTED[("name", False)]  # replaced by the fresh list, same sort


async def test_a_damaged_preferences_file_means_the_default_order():
    config.save(Credentials("k", "me"))
    Preferences().path.parent.mkdir(parents=True, exist_ok=True)
    Preferences().path.write_text("{broken")
    app, ctx = await runs_app()
    async with ctx as pilot:
        assert names(await open_runs(pilot)) == SERVER
