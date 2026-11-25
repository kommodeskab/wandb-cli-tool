import json

from textual.widgets import Input, OptionList, Select

from conftest import FakeClient
from test_run_screens import open_charts, panel_labels, settle
from wandb_cli_tool import config
from wandb_cli_tool.charts import LAYOUT_VERSION
from wandb_cli_tool.config import Credentials
from wandb_cli_tool.layout_store import LayoutStore
from wandb_cli_tool.screens.charts import ChartView
from chart_helpers import add_plot
from wandb_cli_tool.screens.chart_dialogs import AxisDialog
from wandb_cli_tool.screens.dialogs import ConfirmDialog

DEFAULT_PANELS = ["System (2)", "trainer (2)", "val (1)", "Other (1)"]


def saved_file():
    return LayoutStore().path


def saved_runs():
    return json.loads(saved_file().read_text())["projects"] if saved_file().exists() else {}


async def customize(pilot):
    """Add panel "mine" with one chart (trainer/loss vs trainer/global_steps)."""
    await pilot.press("n")
    await settle(pilot)
    pilot.app.screen.query_one(Input).value = "mine"
    await pilot.press("enter")
    await settle(pilot, 5)
    await add_plot(pilot, (False, "trainer/loss"), x="trainer/global_steps")


async def test_added_panels_and_charts_survive_closing_and_reopening_the_app(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 45)) as pilot:
        screen = await open_charts(pilot)
        assert panel_labels(screen) == DEFAULT_PANELS
        await customize(pilot)
        assert panel_labels(screen)[-1] == "mine (1)"
    # the app is closed; a brand-new one starts
    app2 = make_app()
    async with app2.run_test(size=(130, 45)) as pilot:
        screen = await open_charts(pilot)
        assert panel_labels(screen) == DEFAULT_PANELS + ["mine (1)"]
        await pilot.press("down", "down", "down", "down")  # select "mine"
        await settle(pilot, 5)
        (view,) = screen.query(ChartView)
        assert (view.spec.y, view.spec.x) == ("trainer/loss", "trainer/global_steps")
        assert "custom layout shared by all runs in this project" in " ".join(screen.query_one("#status").render().plain.split())


async def test_changes_are_written_immediately_not_only_on_exit(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 45)) as pilot:
        await open_charts(pilot)
        assert saved_runs() == {}  # opening the screen alone saves nothing
        await customize(pilot)
        (record,) = saved_runs().values()  # while the app is still running
        assert record["panels"] == ["mine"]
        assert record["added"]["mine"] == [
            {"y": "trainer/loss", "x": "trainer/global_steps", "system": False, "kind": "plot", "more": []}
        ]
        assert json.loads(saved_file().read_text())["version"] == LAYOUT_VERSION


async def test_an_x_axis_change_on_a_generated_chart_persists(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 45)) as pilot:
        screen = await open_charts(pilot)
        await pilot.press("down")  # trainer
        await settle(pilot, 4)
        await pilot.press("right", "down", "x")  # second chart: trainer/loss
        await settle(pilot)
        assert isinstance(app.screen, AxisDialog)
        app.screen.query_one("#x", Select).value = "acc"
        await pilot.click("#ok")
        await settle(pilot, 5)
    app2 = make_app()
    async with app2.run_test(size=(130, 45)) as pilot:
        screen = await open_charts(pilot)
        await pilot.press("down")
        await settle(pilot, 5)
        specs = {v.spec.y: v.spec.x for v in screen.query(ChartView)}
        assert specs == {"trainer/global_steps": "_step", "trainer/loss": "acc"}


async def test_new_metrics_still_appear_next_to_saved_customizations(make_app, monkeypatch):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 45)) as pilot:
        await open_charts(pilot)
        await customize(pilot)
    monkeypatch.setattr(FakeClient, "METRICS", FakeClient.METRICS + ["trainer/extra"])  # logged since
    app2 = make_app()
    async with app2.run_test(size=(130, 45)) as pilot:
        screen = await open_charts(pilot)
        assert panel_labels(screen) == ["System (2)", "trainer (3)", "val (1)", "Other (1)", "mine (1)"]


async def test_reset_asks_first_and_then_restores_the_default(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 45)) as pilot:
        screen = await open_charts(pilot)
        await pilot.press("d")  # nothing customized yet
        await settle(pilot)
        assert not isinstance(app.screen, ConfirmDialog)
        assert any("already uses the default layout" in n.message for n in app._notifications)

        await customize(pilot)
        await pilot.press("d")  # works from wherever the focus is on this screen
        await settle(pilot)
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("n")  # declined: nothing changes
        await settle(pilot)
        assert panel_labels(screen)[-1] == "mine (1)" and saved_runs()

        await pilot.press("d")
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot, 6)
        assert panel_labels(screen) == DEFAULT_PANELS
        assert saved_runs() == {}  # the saved record is gone, not just hidden
        assert "custom layout" not in " ".join(screen.query_one("#status").render().plain.split())
    app2 = make_app()
    async with app2.run_test(size=(130, 45)) as pilot:  # and it stays reset
        assert panel_labels(await open_charts(pilot)) == DEFAULT_PANELS


async def test_reset_also_undoes_x_axis_choices(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 45)) as pilot:
        screen = await open_charts(pilot)
        await pilot.press("down", "right", "x")
        await settle(pilot)
        app.screen.query_one("#x", Select).value = "acc"
        await pilot.click("#ok")
        await settle(pilot, 5)
        assert saved_runs()
        await pilot.press("d")
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot, 6)
        assert all(v.spec.x == ("_runtime" if v.spec.system else "_step") for v in screen.query(ChartView))
        assert saved_runs() == {}


async def test_damaged_or_outdated_settings_never_break_the_screen(make_app):
    config.save(Credentials("k", "me"))
    for content in ("{definitely not json", json.dumps({"version": 0, "runs": {"x": {}}}),
                    json.dumps({"version": LAYOUT_VERSION, "projects": {"me/alpha": {"panels": 5, "added": "?"}}})):
        saved_file().parent.mkdir(parents=True, exist_ok=True)
        saved_file().write_text(content)
        app = make_app()
        async with app.run_test(size=(130, 45)) as pilot:
            assert panel_labels(await open_charts(pilot)) == DEFAULT_PANELS


async def test_a_failed_save_is_reported_but_the_session_keeps_working(make_app, monkeypatch):
    config.save(Credentials("k", "me"))
    monkeypatch.setattr(LayoutStore, "save", lambda self, key, custom: False)
    app = make_app()
    async with app.run_test(size=(130, 45)) as pilot:
        screen = await open_charts(pilot)
        await customize(pilot)
        assert any("Could not save the chart layout" in n.message for n in app._notifications)
        assert panel_labels(screen)[-1] == "mine (1)"  # still shown for this session


async def test_logging_out_keeps_chart_settings(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 45)) as pilot:
        await open_charts(pilot)
        await customize(pilot)
        await pilot.press("l")  # logout wipes the key and cache, not display preferences
        await settle(pilot)
    assert len(saved_runs()) == 1
