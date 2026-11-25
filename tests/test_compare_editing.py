"""Adding, changing and deleting charts while comparing runs: the same keys and dialogs as in a single
run, acting on the project's one layout (so the single-run screens see every change too)."""

from textual.widgets import Input, OptionList, Select

from chart_helpers import add_bar, add_images, add_plot, add_table, new_panel
from conftest import FakeClient
from test_compare_screen import MediaClient, WideClient, compare, keys_of, make, settle
from wandb_cli_tool.charts import BAR, IMAGE, PLOT, TABLE, ChartOptions, ChartSpec, chart_key
from wandb_cli_tool.layout_store import LayoutStore, project_key
from wandb_cli_tool.screens.chart_dialogs import AxisDialog, ChartOptionsDialog
from wandb_cli_tool.screens.compare_widgets import (
    CompareBarView, CompareImageView, ComparePlotView, CompareTableView,
)
from wandb_cli_tool.screens.dialogs import ConfirmDialog

KEY = project_key("me", "alpha")


def saved():
    return LayoutStore().load(KEY)


def labels(screen):
    options = screen.query_one("#panels", OptionList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


async def to_charts(pilot, *rows, panel=None):
    screen = await compare(pilot, *(rows or (0, 1, 2)))
    await pilot.press("4")
    await settle(pilot, 6)
    if panel:
        await show(pilot, screen, panel)
    return screen


async def show(pilot, screen, name):
    screen.selected = next(i for i, l in enumerate(labels(screen)) if l.startswith(name + " ("))
    await screen._show_panel()
    await settle(pilot, 8)


async def mine(pilot, *rows):
    screen = await to_charts(pilot, *rows)
    await new_panel(pilot, "mine")
    return screen


# ---- the keys -------------------------------------------------------------------------------------------
async def test_the_editing_keys_exist_only_on_the_charts_tab():
    app = make(WideClient)
    async with app.run_test(size=(170, 45)) as pilot:
        screen = await compare(pilot, 0, 1, 2)
        for action in ("new_panel", "add_chart"):
            assert screen.check_action(action, ()) is False  # on the overview: not offered
        await pilot.press("4")
        await settle(pilot, 4)
        assert screen.check_action("add_chart", ()) is True and screen.check_action("new_panel", ()) is True
        bar = keys_of(screen)
        assert "New panel" in bar and "Add chart" in bar
        assert "X-axis" not in bar and "Delete chart" not in bar and "Default layout" not in bar  # nothing focused / customized
        await pilot.press("3")
        await settle(pilot, 4)
        assert "Add chart" not in keys_of(screen) and "Hide identical rows" in keys_of(screen)  # `i` takes the old place of `d` there


# ---- adding ---------------------------------------------------------------------------------------------
async def test_a_new_panel_is_listed_saved_and_empty():
    app = make(WideClient)
    async with app.run_test(size=(150, 45)) as pilot:
        screen = await mine(pilot)
        assert labels(screen)[-1] == "mine (0)" and screen.selected == len(labels(screen)) - 1
        assert saved().panels == ["mine"]
        assert not list(screen.query(".chart-widget"))  # nothing in it yet


async def test_a_plot_with_several_metrics_is_one_layout_chart_drawn_as_one_plot_per_metric():
    app = make(WideClient)
    async with app.run_test(size=(150, 50)) as pilot:
        screen = await mine(pilot)
        await add_plot(pilot, (False, "trainer/loss"), (False, "val/loss"))
        views = list(screen.query(ComparePlotView))
        assert [v.chart.metric for v in views] == ["trainer/loss", "val/loss"]  # small multiples
        assert {v.layout_index for v in views} == {0}  # ... of the one chart that was added
        assert labels(screen)[-1] == "mine (1)"  # counted as the one chart it is
        [spec] = saved().added["mine"]
        assert (spec.kind, spec.series) == (PLOT, ("trainer/loss", "val/loss"))
        assert app.focused is views[0]
        assert all(v.border_subtitle != "loading…" for v in views)  # and their data arrived for all runs


async def test_bars_tables_and_images_can_be_added_too():
    app = make(MediaClient)
    async with app.run_test(size=(150, 60)) as pilot:
        screen = await mine(pilot)
        await add_bar(pilot, (False, "acc"), (False, "val/loss"))
        await add_table(pilot, "preds")
        await add_images(pilot, "samples")
        assert [type(v).__name__ for v in screen.query(".chart-widget")] == ["CompareBarView", "CompareTableView", "CompareImageView"]
        assert [s.kind for s in saved().added["mine"]] == [BAR, TABLE, IMAGE]
        [bars] = screen.query(CompareBarView)
        assert [g[0] for g in bars.groups] == ["acc", "loss"]
        [table] = screen.query(CompareTableView)
        assert table.row_count == 4 and "not in: run-2" in str(table.border_subtitle)


async def test_an_added_chart_is_there_when_a_single_run_is_opened_and_back():
    app = make(WideClient)
    async with app.run_test(size=(150, 50)) as pilot:
        screen = await mine(pilot)
        await add_plot(pilot, (False, "trainer/loss"))
        await pilot.press("escape")  # back to the run list ...
        await settle(pilot, 4)
        await pilot.press("enter")  # ... open run-1, then its charts
        await settle(pilot, 6)
        await pilot.press("down", "enter")
        await settle(pilot, 8)
        from wandb_cli_tool.screens.charts import ChartsScreen
        assert isinstance(app.screen, ChartsScreen)
        assert any(l.startswith("mine (1)") for l in [str(app.screen.query_one("#panels", OptionList).get_option_at_index(i).prompt)
                                                       for i in range(app.screen.query_one("#panels", OptionList).option_count)])


async def test_charts_added_in_a_single_run_show_up_in_the_comparison():
    app = make(WideClient)
    app.layout_store.save(KEY, __import__("wandb_cli_tool.charts", fromlist=["Customization"]).Customization(
        panels=["mine"], added={"mine": [ChartSpec("trainer/loss", kind=BAR, more=("val/loss",))]}))
    async with app.run_test(size=(150, 50)) as pilot:
        screen = await to_charts(pilot, panel="mine")
        assert len(list(screen.query(CompareBarView))) == 1


# ---- changing ---------------------------------------------------------------------------------------------
async def test_the_x_axis_of_a_split_plot_changes_for_all_its_plots():
    app = make(WideClient)
    async with app.run_test(size=(150, 50)) as pilot:
        screen = await mine(pilot)
        await add_plot(pilot, (False, "trainer/loss"), (False, "val/loss"))
        await pilot.press("x")
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, AxisDialog)
        dialog.query_one("#x", Select).value = "_runtime"
        await pilot.click("#ok")
        await settle(pilot, 8)
        views = list(screen.query(ComparePlotView))
        assert [v.chart.x for v in views] == ["_runtime", "_runtime"]
        assert saved().added["mine"][0].x == "_runtime"
        assert app.focused is views[0]


async def test_chart_options_apply_to_every_plot_of_the_chart_and_to_bars():
    app = make(WideClient)
    async with app.run_test(size=(150, 60)) as pilot:
        screen = await mine(pilot)
        await add_plot(pilot, (False, "trainer/loss"), (False, "val/loss"))
        await pilot.press("o")
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, ChartOptionsDialog) and not dialog.is_bar
        dialog.query_one("#ymin", Input).value = "0"
        dialog.query_one("#ymax", Input).value = "5"
        dialog.query_one("#smooth", Input).value = "0.5"
        await pilot.click("#ok")
        await settle(pilot, 8)
        assert all(v.chart.opts == ChartOptions(0.0, 5.0, 0.5) for v in screen.query(ComparePlotView))
        assert "y 0…5" in str(next(iter(screen.query(ComparePlotView))).border_subtitle)
        assert saved().added["mine"][0].opts == ChartOptions(0.0, 5.0, 0.5)
        await add_bar(pilot, (False, "acc"), (False, "val/loss"))
        await pilot.press("o")
        await settle(pilot)
        assert isinstance(app.screen, ChartOptionsDialog) and app.screen.is_bar  # a bar chart: the axis only


async def test_a_generated_charts_settings_are_an_override_not_a_new_chart():
    app = make(WideClient)
    async with app.run_test(size=(150, 50)) as pilot:
        screen = await to_charts(pilot, panel="trainer")
        view = next(iter(screen.query(ComparePlotView)))
        view.focus()
        await pilot.press("o")
        await settle(pilot)
        app.screen.query_one("#ymax", Input).value = "9"
        await pilot.click("#ok")
        await settle(pilot, 8)
        custom = saved()
        assert custom.added == {} and any(o.ymax == 9.0 for panel in custom.opts.values() for o in panel.values())


# ---- deleting and resetting --------------------------------------------------------------------------------
async def test_deleting_a_split_plot_removes_all_of_its_plots_after_asking():
    app = make(WideClient)
    async with app.run_test(size=(150, 50)) as pilot:
        screen = await mine(pilot)
        await add_plot(pilot, (False, "trainer/loss"), (False, "val/loss"))
        await add_bar(pilot, (False, "acc"), (False, "val/loss"))
        screen.query(ComparePlotView).first().focus()
        await pilot.press("delete")
        await settle(pilot)
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("n")  # declined: nothing happens
        await settle(pilot)
        assert len(list(screen.query(ComparePlotView))) == 2 and len(saved().added["mine"]) == 2
        await pilot.press("delete", "y")
        await settle(pilot, 8)
        assert not list(screen.query(ComparePlotView)) and len(list(screen.query(CompareBarView))) == 1
        assert [s.kind for s in saved().added["mine"]] == [BAR]
        assert app.focused is next(iter(screen.query(CompareBarView)))  # the focus moves on to what is left


async def test_a_default_chart_can_be_deleted_and_stays_deleted():
    app = make(WideClient)
    async with app.run_test(size=(150, 50)) as pilot:
        screen = await to_charts(pilot, panel="trainer")
        before = [v.chart.metric for v in screen.query(ComparePlotView)]
        assert before == ["trainer/global_steps", "trainer/loss"]
        screen.query(ComparePlotView).first().focus()
        await pilot.press("delete", "y")
        await settle(pilot, 8)
        assert [v.chart.metric for v in screen.query(ComparePlotView)] == ["trainer/loss"]
        assert saved().removed == {"trainer": [chart_key(ChartSpec("trainer/global_steps"))]}


async def test_d_resets_the_layout_after_asking_and_only_when_something_is_customized():
    app = make(WideClient)
    async with app.run_test(size=(150, 50)) as pilot:
        screen = await to_charts(pilot)
        assert screen.check_action("reset_layout", ()) is False  # nothing to reset
        await new_panel(pilot, "mine")
        assert screen.check_action("reset_layout", ()) is True and "Default layout" in keys_of(screen)
        await pilot.press("d")
        await settle(pilot)
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("y")
        await settle(pilot, 8)
        assert "mine" not in " ".join(labels(screen)) and saved().is_empty
        assert screen.selected == 0


async def test_editing_works_when_the_runs_share_no_metrics_with_the_layout():
    """A chart can name metrics only some of the compared runs logged: the others are simply absent."""
    app = make()  # CompareClient: run-2 has no val/loss
    async with app.run_test(size=(150, 50)) as pilot:
        screen = await mine(pilot, 0, 1)
        await add_plot(pilot, (False, "val/loss"))
        [view] = screen.query(ComparePlotView)
        assert "no data: run-2" in str(view.border_subtitle)
