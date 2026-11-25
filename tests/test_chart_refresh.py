from dataclasses import replace

import pytest

from chart_helpers import add_bar, add_table, new_panel, settle
from conftest import FakeClient, FakePlayer
from test_run_detail_screens import screen_text, to_run
from test_run_screens import open_charts, panel_labels
from wandb_cli_tool import config
from wandb_cli_tool.app import WandbCliApp
from wandb_cli_tool.config import Credentials
from wandb_cli_tool.preferences import Preferences
from wandb_cli_tool.refresh import (
    DEFAULT_REFRESH, PREF_KEY, REFRESH_CHOICES, interval_from_pref, label, next_choice, to_pref,
)
from wandb_cli_tool.screens.chart_widgets import BarView, ChartView, TableView
from wandb_cli_tool.screens.charts import ChartsScreen
from wandb_cli_tool.screens.dialogs import ConfirmDialog
from wandb_cli_tool.tables import TableData

SIZE = (130, 50)
PREDS = "media/table/preds_3_ab12cd34ef56ab78cd90.table.json"


# ---- pure: the interval choices -----------------------------------------------------------


def test_the_key_cycles_through_the_choices_and_back_to_off():
    seen, current = [], None
    for _ in range(len(REFRESH_CHOICES) + 1):
        current = next_choice(current)
        seen.append(current)
    assert seen == [10, 30, 60, 300, None, 10]
    assert next_choice(20) == 30 and next_choice(0.2) == 10 and next_choice(400) is None  # unusual values move on


@pytest.mark.parametrize("seconds, text", [(None, "off"), (0, "off"), (10, "10 s"), (60, "1 min"), (300, "5 min"), (0.2, "0.2 s")])
def test_labels(seconds, text):
    assert label(seconds) == text


@pytest.mark.parametrize("saved, expected", [
    (None, DEFAULT_REFRESH), (0, None), (10, 10), (300, 300),
    (45, DEFAULT_REFRESH), (-5, DEFAULT_REFRESH), (True, DEFAULT_REFRESH), ("x", DEFAULT_REFRESH), (1.5, DEFAULT_REFRESH),
])
def test_a_saved_interval_is_used_only_if_it_is_a_known_choice(saved, expected):
    assert interval_from_pref(saved) == expected
    assert to_pref(None) == 0 and to_pref(30) == 30


# ---- a run that keeps changing -----------------------------------------------------------------


class Live(FakeClient):
    """Like a run that is still logging: every fetch sees newer data."""

    generation = 0
    state = "running"
    fail_series = False
    fail_keys = False
    extra_metrics: list = []

    def __init__(self, api_key):
        super().__init__(api_key)
        self.calls = {"keys": 0, "series": 0, "table": 0}

    def metric_keys(self, ref):
        self.calls["keys"] += 1
        if self.fail_keys:
            raise RuntimeError("keys boom")
        return self.METRICS + self.extra_metrics

    def metric_values(self, ref):
        return {k: v + self.generation for k, v in self.VALUES.items()} | {m: 1.0 for m in self.extra_metrics}

    def metric_series(self, ref, y, x):
        self.calls["series"] += 1
        if self.fail_series:
            raise RuntimeError("series boom")
        return [(float(i), float(i)) for i in range(5 + self.generation)]

    def table_data(self, ref, path):
        self.calls["table"] += 1
        rows = tuple((f"r{i}", f"gen{self.generation}") for i in range(4))
        return TableData(("row", "value"), rows, 4, 2)

    def run_details(self, ref):
        info = super().run_details(ref)
        return replace(info, state=self.state) if ref.id == "id1" else info  # only run-1 is the live one


async def start(make_app=None, client=Live, interval=None):
    config.save(Credentials("k", "me"))
    if interval is not None:
        Preferences().set(PREF_KEY, interval)
    app = WandbCliApp(client_factory=client, audio_player=FakePlayer())
    return app, app.run_test(size=SIZE)


async def charts_of(pilot):
    screen = await open_charts(pilot)  # run-1, which is running
    assert isinstance(screen, ChartsScreen)
    return screen


async def select_trainer(pilot):
    await pilot.press("down")
    await settle(pilot, 5)


def status(screen):
    return " ".join(screen.query_one("#status").render().plain.split())


async def test_refresh_now_updates_the_charts_where_they_are_and_reports_the_time():
    app, ctx = await start(interval=0)
    async with ctx as pilot:
        screen = await charts_of(pilot)
        await select_trainer(pilot)
        before = list(screen.query(ChartView))
        calls = app.client.calls["series"]
        assert len(screen._cache[(False, "trainer/loss", "_step")]) == 5
        app.client.generation = 3
        await pilot.press("r")
        await settle(pilot, 6)
        after = list(screen.query(ChartView))
        assert after == before and all(a is b for a, b in zip(after, before))  # the very same widgets
        assert app.client.calls["series"] == calls + 2  # each chart fetched its data again
        assert len(screen._cache[(False, "trainer/loss", "_step")]) == 8
        assert "updated" in status(screen) and "refreshing" not in status(screen)


async def test_a_new_metric_adds_a_chart_without_losing_the_panel_or_focus():
    app, ctx = await start(interval=0)
    async with ctx as pilot:
        screen = await charts_of(pilot)
        await select_trainer(pilot)
        await pilot.press("right", "down")  # the second chart in "trainer"
        assert app.focused.index == 1
        old = list(screen.query(ChartView))
        Live.extra_metrics = ["trainer/extra"]
        try:
            await pilot.press("r")
            await settle(pilot, 8)
        finally:
            Live.extra_metrics = []
        assert panel_labels(screen)[1] == "trainer (3)" and screen.selected == 1
        new = list(screen.query(ChartView))
        assert len(new) == 3 and not any(n is o for n in new for o in old)  # the panel was rebuilt
        assert app.focused is new[1]  # and focus stays on the same position


async def test_a_new_panel_appearing_before_the_selected_one_does_not_change_what_is_shown():
    app, ctx = await start(interval=0)
    async with ctx as pilot:
        screen = await charts_of(pilot)
        await select_trainer(pilot)
        before = list(screen.query(ChartView))
        Live.extra_metrics = ["aaa/new"]  # sorts before "trainer", so its index moves
        try:
            await pilot.press("r")
            await settle(pilot, 8)
        finally:
            Live.extra_metrics = []
        assert screen.panels[screen.selected].name == "trainer" and panel_labels(screen)[1] == "aaa (1)"
        assert all(a is b for a, b in zip(screen.query(ChartView), before))  # not remounted
        assert screen.query_one("#panels").highlighted == screen.selected


async def test_bars_and_tables_update_and_the_table_keeps_its_cursor():
    app, ctx = await start(interval=0)
    async with ctx as pilot:
        screen = await charts_of(pilot)
        await new_panel(pilot, "mine")
        await add_table(pilot, "preds")
        await add_bar(pilot, (False, "acc"), (False, "val/loss"))
        table, bar = screen.query(TableView).first(), screen.query(BarView).first()
        table.focus()
        await pilot.press("down", "down", "right")
        await settle(pilot)
        assert (table.cursor_coordinate.row, table.cursor_coordinate.column) == (2, 1)
        assert str(table.get_row_at(0)[1]) == "gen0" and bar.items[0] == ("acc", 0.9)
        app.client.generation = 2
        await pilot.press("r")
        await settle(pilot, 6)
        assert str(table.get_row_at(0)[1]) == "gen2"  # fresh rows
        assert bar.items[0][1] == pytest.approx(2.9)  # fresh values
        assert (table.cursor_coordinate.row, table.cursor_coordinate.column) == (2, 1)  # cursor stayed
        assert app.focused is table


async def test_user_customizations_survive_a_refresh():
    app, ctx = await start(interval=0)
    async with ctx as pilot:
        screen = await charts_of(pilot)
        await new_panel(pilot, "mine")
        await add_bar(pilot, (False, "acc"), (False, "val/loss"))
        app.client.generation = 1
        await pilot.press("r")
        await settle(pilot, 6)
        assert panel_labels(screen)[-1] == "mine (1)" and len(screen.query(BarView)) == 1


# ---- the timer ---------------------------------------------------------------------------------


async def test_a_running_run_refreshes_by_itself_and_stops_when_it_finishes():
    app, ctx = await start(interval=0)
    async with ctx as pilot:
        screen = await charts_of(pilot)
        assert screen._timer is None  # interval off: nothing is scheduled
        screen.set_refresh_interval(0.2)
        assert screen._timer is not None
        first = app.client.calls["keys"]
        await pilot.pause(1.2)
        assert app.client.calls["keys"] >= first + 3  # several automatic refreshes
        assert "auto-refresh 0.2 s" in status(screen)
        app.client.state = "finished"  # the training run ends
        await pilot.pause(0.8)
        assert screen._timer is None and "run finished" in status(screen) and not screen.live
        settled = app.client.calls["keys"]
        await pilot.pause(0.8)
        assert app.client.calls["keys"] == settled  # no more requests for a finished run


async def test_a_finished_run_gets_no_timer_but_can_still_be_refreshed_by_hand():
    app, ctx = await start(interval=30)
    async with ctx as pilot:
        await settle(pilot, 4)
        await pilot.press("enter")  # alpha
        await settle(pilot, 5)
        await pilot.press("down", "enter")  # run-2, which is finished
        await settle(pilot, 5)
        await pilot.press("down", "enter")  # Charts
        await settle(pilot, 8)
        screen = app.screen
        assert isinstance(screen, ChartsScreen) and screen.interval == 30 and screen._timer is None
        assert "run finished" in status(screen)
        before = app.client.calls["keys"]
        await pilot.press("r")
        await settle(pilot, 6)
        assert app.client.calls["keys"] == before + 1


async def test_the_f_key_cycles_the_interval_and_it_is_remembered():
    app, ctx = await start(interval=None)  # nothing saved: the default
    async with ctx as pilot:
        screen = await charts_of(pilot)
        assert screen.interval == DEFAULT_REFRESH and screen._timer is not None
        seen = []
        for _ in range(5):
            await pilot.press("f")
            await settle(pilot, 2)
            seen.append((screen.interval, screen._timer is not None))
            assert Preferences().get(PREF_KEY) == to_pref(screen.interval)
        assert seen == [(60, True), (300, True), (None, False), (10, True), (30, True)]  # off means no timer
        assert any("Auto-refresh: off" in n.message for n in app._notifications)
        await pilot.press("f", "f", "f")  # 60, 300, off
        await settle(pilot, 3)
    app2, ctx2 = await start(interval=None)  # next time: off is remembered
    async with ctx2 as pilot:
        screen = await charts_of(pilot)
        assert screen.interval is None and screen._timer is None and "auto-refresh off (f to turn on)" in status(screen)


async def test_leaving_the_screen_stops_refreshing():
    app, ctx = await start(interval=0)
    async with ctx as pilot:
        screen = await charts_of(pilot)
        screen.set_refresh_interval(0.2)
        await pilot.pause(0.5)
        await pilot.press("escape")
        await settle(pilot, 3)
        calls = app.client.calls["keys"]
        await pilot.pause(0.8)
        assert app.client.calls["keys"] == calls and screen._timer is None


async def test_the_timer_does_not_refresh_while_a_dialog_is_open_and_refreshes_never_overlap():
    app, ctx = await start(interval=0)
    async with ctx as pilot:
        screen = await charts_of(pilot)
        await pilot.press("d")  # nothing customized: a notice, not a dialog
        await pilot.press("a")  # the add-chart dialog
        await settle(pilot)
        assert app.screen is not screen
        calls = app.client.calls["keys"]
        screen._tick()
        await settle(pilot, 4)
        assert app.client.calls["keys"] == calls  # skipped: a dialog is on top
        await pilot.press("escape")
        await settle(pilot)
        screen.refresh_now()
        screen.refresh_now()  # a second request while one is in flight is ignored
        await settle(pilot, 6)
        assert app.client.calls["keys"] == calls + 1


# ---- failures ----------------------------------------------------------------------------------


async def test_a_failed_refresh_keeps_the_charts_and_the_next_one_recovers():
    app, ctx = await start(interval=0)
    async with ctx as pilot:
        screen = await charts_of(pilot)
        await select_trainer(pilot)
        views = list(screen.query(ChartView))
        app.client.fail_series = True
        await pilot.press("r")
        await settle(pilot, 6)
        assert "refresh failed: series boom" in status(screen)
        assert all(a is b for a, b in zip(screen.query(ChartView), views))
        assert "Error:" not in screen_text(app)  # the charts were not replaced by error messages
        app.client.fail_series = False
        app.client.fail_keys = True  # and a failure to read the run at all is reported too
        await pilot.press("r")
        await settle(pilot, 6)
        assert "refresh failed: keys boom" in status(screen)
        app.client.fail_keys = False
        await pilot.press("r")
        await settle(pilot, 6)
        assert "refresh failed" not in status(screen) and "updated" in status(screen)


async def test_a_failed_table_refresh_keeps_the_rows():
    app, ctx = await start(interval=0)
    async with ctx as pilot:
        screen = await charts_of(pilot)
        await new_panel(pilot, "mine")
        await add_table(pilot, "preds")
        table = screen.query(TableView).first()
        rows = [tuple(str(c) for c in table.get_row_at(i)) for i in range(table.row_count)]

        def boom(self, ref, path):
            raise RuntimeError("table boom")

        Live.table_data, original = boom, Live.table_data
        try:
            await pilot.press("r")
            await settle(pilot, 6)
        finally:
            Live.table_data = original
        assert "refresh failed: table boom" in status(screen)
        assert [tuple(str(c) for c in table.get_row_at(i)) for i in range(table.row_count)] == rows
