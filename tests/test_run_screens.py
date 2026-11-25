from textual.widgets import SelectionList, Button, Input, OptionList, RichLog, Select, Static

from wandb_cli_tool import config
from wandb_cli_tool.config import Credentials
from wandb_cli_tool.screens.charts import ChartsScreen, ChartView
from chart_helpers import add_plot, start_add
from wandb_cli_tool.screens.chart_dialogs import AxisDialog, PlotDialog
from wandb_cli_tool.screens.dialogs import NewPanelDialog
from wandb_cli_tool.screens.logs import LogsScreen
from wandb_cli_tool.screens.run import RunScreen
from wandb_cli_tool.screens.runs import RunsScreen


async def settle(pilot, n=3):
    for _ in range(n):
        await pilot.pause()


async def open_run(pilot):
    """Logged in -> alpha -> run-1 (RunScreen)."""
    await settle(pilot)
    await pilot.press("enter")  # alpha
    await settle(pilot)
    assert isinstance(pilot.app.screen, RunsScreen)
    await pilot.press("enter")  # run-1
    await settle(pilot)
    assert isinstance(pilot.app.screen, RunScreen)


def panel_labels(screen):
    ol = screen.query_one("#panels", OptionList)
    return [str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count)]


async def open_charts(pilot):
    await open_run(pilot)
    await pilot.press("down", "enter")  # Charts
    await settle(pilot, 5)
    assert isinstance(pilot.app.screen, ChartsScreen)
    return pilot.app.screen


async def test_run_menu_offers_logs_and_charts(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 45)) as pilot:
        await open_run(pilot)
        menu = app.screen.query_one(OptionList)
        assert [str(menu.get_option_at_index(i).prompt) for i in range(menu.option_count)] == [
            "Logs", "Charts", "Config", "Files"
        ]
        assert app.screen.ref.id == "id1" and app.screen.ref.project == "alpha"


async def test_logs_screen_shows_lines_and_escape_goes_back(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 45)) as pilot:
        await open_run(pilot)
        await pilot.press("enter")  # Logs
        await settle(pilot, 4)
        assert isinstance(app.screen, LogsScreen)
        log = app.screen.query_one(RichLog)
        text = "\n".join(strip.text for strip in log.lines)
        assert "epoch 1" in text and "red" in text and "\x1b" not in text
        assert "100% done" in text and "50%" not in text  # \r keeps the final state
        assert "4 lines" in str(app.screen.query_one("#status", Static).render())
        await pilot.press("escape")
        await settle(pilot)
        assert isinstance(app.screen, RunScreen)


async def test_charts_group_metrics_into_panels_with_system_first(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 45)) as pilot:
        screen = await open_charts(pilot)
        assert panel_labels(screen) == [
            "System (2)", "trainer (2)", "val (1)", "Other (1)"
        ]
        # System panel is shown first, plotted against runtime
        views = list(screen.query(ChartView))
        assert [v.spec.y for v in views] == ["system.cpu", "system.memory"]
        assert all(v.spec.system and v.spec.x == "_runtime" for v in views)
        # arrow down selects "trainer": charts titled without the prefix, x = Step
        await pilot.press("down")
        await settle(pilot, 4)
        views = list(screen.query(ChartView))
        assert [v.border_title for v in views] == ["global_steps", "loss"]
        assert all(v.spec.x == "_step" for v in views)
        assert sorted(app.client.series_calls) == [
            ("trainer/global_steps", "_step"), ("trainer/loss", "_step")
        ]


async def test_new_panel_then_add_chart_with_custom_x(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 45)) as pilot:
        screen = await open_charts(pilot)
        await pilot.press("n")
        await settle(pilot)
        assert isinstance(app.screen, NewPanelDialog)
        app.screen.query_one(Input).value = "mine"
        await pilot.press("enter")
        await settle(pilot, 4)
        assert panel_labels(screen)[-1] == "mine (0)"
        assert "No charts" in str(screen.query_one("#charts").query_one(Static).render())

        await add_plot(pilot, (False, "trainer/loss"), x="trainer/global_steps")
        assert panel_labels(screen)[-1] == "mine (1)"
        (view,) = screen.query(ChartView)
        assert view.spec.y == "trainer/loss" and view.spec.x == "trainer/global_steps"
        assert app.client.series_calls[-1] == ("trainer/loss", "trainer/global_steps")


async def test_change_x_axis_of_existing_chart(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 45)) as pilot:
        screen = await open_charts(pilot)
        await pilot.press("down")  # trainer panel
        await settle(pilot, 4)
        await pilot.press("right")  # focus first chart
        await pilot.press("down")  # second chart (loss)
        assert isinstance(app.focused, ChartView) and app.focused.spec.y == "trainer/loss"
        await pilot.press("x")
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, AxisDialog) and "trainer/loss" in screen_text_of(dialog)
        dialog.query_one("#x", Select).value = "acc"
        await pilot.click("#ok")
        await settle(pilot, 4)
        views = list(screen.query(ChartView))
        assert views[1].spec.x == "acc" and views[0].spec.x == "_step"
        assert app.client.series_calls[-1] == ("trainer/loss", "acc")


async def test_system_chart_x_options_exclude_step(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 45)) as pilot:
        await open_charts(pilot)
        dialog = await start_add(pilot, "plot")
        assert isinstance(dialog, PlotDialog)
        dialog.query_one("#choices", SelectionList).select((True, "system.cpu"))
        await settle(pilot, 3)
        values = [v for _, v in dialog.query_one("#x", Select)._options]
        assert "_step" not in values and "_runtime" in values
        await pilot.press("escape")
        await settle(pilot)
        assert isinstance(app.screen, ChartsScreen)


async def test_single_value_chart_is_shown_as_number_not_plot(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 45)) as pilot:
        screen = await open_charts(pilot)
        await pilot.press("down", "down", "down")  # System, trainer, val, Other
        await settle(pilot, 5)
        (view,) = screen.query(ChartView)
        assert view.spec.y == "acc"
        assert view.single == (3.0, 0.25)
        text = view.render().renderable.plain
        assert "0.25" in text and "Step = 3" in text
        assert view.styles.height.value == 7  # compact, no plot area
        # a multi-point chart still plots
        await pilot.press("up", "up")  # trainer
        await settle(pilot, 5)
        assert all(v.single is None for v in screen.query(ChartView))


def screen_text_of(screen):
    return "\n".join(strip.text for strip in screen._compositor.render_strips())
