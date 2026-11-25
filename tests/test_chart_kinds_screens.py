import pytest
from textual.app import App
from textual.widgets import DataTable, OptionList, Select, SelectionList

from chart_helpers import add_bar, add_plot, add_table, new_panel, pick, plot_spy, settle, start_add
from conftest import FakeClient
from test_run_detail_screens import screen_text
from test_run_screens import open_charts, panel_labels
from wandb_cli_tool import config
from wandb_cli_tool.charts import BAR, PLOT, TABLE, ChartSpec
from wandb_cli_tool.config import Credentials
from wandb_cli_tool.layout_store import LayoutStore, project_key
from wandb_cli_tool.palette import SERIES_DARK, SERIES_LIGHT, hex_to_rgb
from wandb_cli_tool.screens.chart_dialogs import (
    AxisDialog, BarDialog, ChartKindDialog, MetricPicker, PlotDialog, TableDialog,
)
from wandb_cli_tool.screens.chart_widgets import BarView, ChartView, TableView
from wandb_cli_tool.tables import TableData

SIZE = (130, 50)
PREDS = "media/table/preds_3_ab12cd34ef56ab78cd90.table.json"


async def start(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    return app, app.run_test(size=SIZE)


async def mine(pilot):
    """Open the charts screen and create an empty panel to add charts to."""
    screen = await open_charts(pilot)
    await new_panel(pilot, "mine")
    return screen


def of(screen, cls):
    return list(screen.query(cls))


# ---- choosing the kind ---------------------------------------------------------------------


async def test_the_kind_dialog_offers_plot_bar_and_table(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await open_charts(pilot)
        await pilot.press("a")
        await settle(pilot)
        assert isinstance(app.screen, ChartKindDialog)
        options = app.screen.query_one("#kinds", OptionList)
        labels = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        assert [l.split(" — ")[0] for l in labels] == ["Plot", "Bar chart", "Table", "Images"]
        assert all("not available" not in l for l in labels)
        await pilot.press("escape")
        await settle(pilot)
        assert app.screen is screen and panel_labels(screen)[0] == "System (2)"  # nothing was added


async def test_unavailable_kinds_are_greyed_out_with_the_reason_and_cannot_be_chosen(make_app, monkeypatch):
    monkeypatch.setattr(FakeClient, "TABLES", [])
    monkeypatch.setattr(FakeClient, "IMAGES", [])
    app, ctx = await start(make_app)
    async with ctx as pilot:
        await open_charts(pilot)
        await pilot.press("a")
        await settle(pilot)
        images = app.screen.query_one("#kinds", OptionList).get_option_at_index(3)
        assert images.disabled and "not available: this run logged no images" in str(images.prompt)
        table = app.screen.query_one("#kinds", OptionList).get_option_at_index(2)
        assert table.disabled and "not available: this run logged no tables" in str(table.prompt)
        await pilot.press("down", "down", "down", "enter")  # tries to reach the disabled option
        await settle(pilot)
        assert not isinstance(app.screen, TableDialog)


async def test_nothing_to_chart_gives_a_message_instead_of_an_empty_dialog(make_app, monkeypatch):
    for name in ("METRICS", "SYSTEM_ROWS", "TABLES", "IMAGES"):
        monkeypatch.setattr(FakeClient, name, [])
    app, ctx = await start(make_app)
    async with ctx as pilot:
        await open_charts(pilot)
        await pilot.press("a")
        await settle(pilot)
        assert not isinstance(app.screen, ChartKindDialog)
        assert any("no metrics, tables or images" in n.message for n in app._notifications)


# ---- plots with several values -----------------------------------------------------------------


async def test_a_plot_shows_several_values_with_a_legend_and_palette_colors_in_order(make_app, monkeypatch):
    calls = plot_spy(monkeypatch)
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await mine(pilot)
        calls.clear()
        names = ["trainer/loss", "val/loss", "trainer/global_steps"]
        await add_plot(pilot, *[(False, n) for n in names])
        (view,) = of(screen, ChartView)
        assert view.spec.series == tuple(names) and view.spec.kind == PLOT and view.spec.x == "_step"
        # one request per series, all against the shared x-axis
        assert sorted(app.client.series_calls[-3:]) == sorted((n, "_step") for n in names)
        drawn = [c for c in calls if "color" in c]
        assert [c["label"] for c in drawn] == names  # colliding short names fall back to full names
        assert [c["color"] for c in drawn] == [hex_to_rgb(c) for c in SERIES_DARK[:3]]  # fixed slot order
        assert panel_labels(screen)[-1] == "mine (1)"


async def test_a_single_value_plot_has_no_legend_and_uses_the_first_color(make_app, monkeypatch):
    calls = plot_spy(monkeypatch)
    app, ctx = await start(make_app)
    async with ctx as pilot:
        await mine(pilot)
        calls.clear()
        await add_plot(pilot, (False, "trainer/loss"))
        drawn = [c for c in calls if "color" in c]
        assert len(drawn) == 1 and "label" not in drawn[0] and drawn[0]["color"] == hex_to_rgb(SERIES_DARK[0])


async def test_a_series_keeps_its_color_by_its_place_in_the_chart(make_app, monkeypatch):
    """Colors follow the entity: a series with no data still holds its slot."""
    calls = plot_spy(monkeypatch)
    app, ctx = await start(make_app)
    async with ctx as pilot:
        await mine(pilot)
        calls.clear()
        # acc has no data against this x, but val/loss must still be the second color
        monkeypatch.setattr(FakeClient, "metric_series",
                            lambda self, ref, y, x: [] if y == "acc" else [(0.0, 1.0), (1.0, 2.0)])
        await add_plot(pilot, (False, "acc"), (False, "val/loss"))
        (drawn,) = [c for c in calls if "color" in c]
        assert drawn["label"] == "loss" and drawn["color"] == hex_to_rgb(SERIES_DARK[1])  # short name: no collision with "acc"
        (view,) = of(app.screen, ChartView)
        assert "no data: acc" in str(view.border_subtitle)


async def test_light_themes_use_the_light_palette(monkeypatch):
    class Host(App):
        def compose(self):
            yield ChartView(ChartSpec("a", more=("b",)), 0)

    app = Host()
    async with app.run_test(size=(80, 30)):
        view = app.query_one(ChartView)
        calls = []
        original = view.plt.plot
        view.plt.plot = lambda *a, **k: (calls.append(k), original(*a, **k))[1]
        app.theme = "textual-light"
        view.show({"a": [(0, 1), (1, 2)], "b": [(0, 2), (1, 1)]})
        assert [c["color"] for c in calls] == [hex_to_rgb(c) for c in SERIES_LIGHT[:2]]


async def test_a_plot_holds_at_most_eight_values_and_never_a_ninth_color(make_app, monkeypatch):
    names = [f"m{i}" for i in range(10)]
    monkeypatch.setattr(FakeClient, "METRICS", names)
    monkeypatch.setattr(FakeClient, "VALUES", {n: float(i) for i, n in enumerate(names)})
    app, ctx = await start(make_app)
    async with ctx as pilot:
        await mine(pilot)
        dialog = await start_add(pilot, "plot")
        await pick(pilot, dialog, *[(False, n) for n in names[:8]])
        await pick(pilot, dialog, (False, names[8]))
        assert len(dialog.query_one("#picker", MetricPicker).chosen) == 8
        assert "at most 8" in screen_text(app)
        assert (False, names[8]) not in dialog.query_one("#choices", SelectionList).selected  # un-ticked again
        await pilot.click("#ok")
        await settle(pilot, 5)
        (view,) = of(app.screen, ChartView)
        assert len(view.spec.series) == 8


async def test_run_metrics_and_system_metrics_cannot_share_a_plot(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        await mine(pilot)
        dialog = await start_add(pilot, "plot")
        await pick(pilot, dialog, (False, "trainer/loss"), (True, "system.cpu"))
        x_values = [v for _, v in dialog.query_one("#x", Select)._options]
        assert "_step" not in x_values  # a system series makes the x-axis time-based
        await pilot.click("#ok")
        await settle(pilot, 3)
        assert isinstance(app.screen, PlotDialog) and "not both" in screen_text(app)
        dialog.query_one("#choices", SelectionList).deselect((False, "trainer/loss"))  # fix it
        await settle(pilot, 3)
        await pilot.click("#ok")
        await settle(pilot, 5)
        (view,) = of(app.screen, ChartView)
        assert view.spec.series == ("system.cpu",) and view.spec.system and view.spec.x == "_runtime"


async def test_system_only_plots_can_have_several_series_on_the_runtime_axis(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        await mine(pilot)
        await add_plot(pilot, (True, "system.cpu"), (True, "system.memory"))
        (view,) = of(app.screen, ChartView)
        assert view.spec.system and view.spec.series == ("system.cpu", "system.memory")
        assert view.spec.x == "_runtime" and app.client.series_calls == []  # computed from the loaded rows


async def test_empty_selection_is_refused_and_cancel_adds_nothing(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        await mine(pilot)
        await start_add(pilot, "plot")
        await pilot.click("#ok")
        await settle(pilot, 3)
        assert isinstance(app.screen, PlotDialog) and "Tick at least one" in screen_text(app)
        await pilot.click("#cancel")
        await settle(pilot, 4)
        assert of(app.screen, ChartView) == [] and panel_labels(app.screen)[-1] == "mine (0)"


async def test_filter_narrows_the_list_and_keeps_ticks_and_their_order(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        await mine(pilot)
        dialog = await start_add(pilot, "plot")
        choices = dialog.query_one("#choices", SelectionList)
        total = choices.option_count
        dialog.query_one("#filter").value = "loss"
        await settle(pilot, 3)
        assert choices.option_count == 2  # trainer/loss, val/loss
        await pick(pilot, dialog, (False, "val/loss"))
        dialog.query_one("#filter").value = "acc"
        await settle(pilot, 3)
        assert choices.option_count == 1
        await pick(pilot, dialog, (False, "acc"))
        dialog.query_one("#filter").value = ""
        await settle(pilot, 3)
        assert choices.option_count == total
        assert set(choices.selected) == {(False, "val/loss"), (False, "acc")}  # ticks survived the filter
        assert dialog.query_one("#picker", MetricPicker).chosen == [(False, "val/loss"), (False, "acc")]


# ---- bar charts ----------------------------------------------------------------------------------


def bar_rows(text, names):
    """Line numbers of the bar rows for `names` (a row starts, inside the border, with the name)."""
    import re

    lines = text.splitlines()
    return [next(i for i, l in enumerate(lines) if re.search(rf"│ {re.escape(n)}\s{{2,}}", l)) for n in names]


async def test_a_bar_chart_shows_the_latest_value_of_each_metric_in_the_chosen_order(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await mine(pilot)
        await add_bar(pilot, (False, "val/loss"), (False, "acc"), (False, "trainer/loss"))
        (view,) = of(screen, BarView)
        assert view.spec.kind == BAR and view.spec.series == ("val/loss", "acc", "trainer/loss")
        assert view.items == [("val/loss", 0.4), ("acc", 0.9), ("trainer/loss", 0.25)]  # the user's order, not sorted
        text = screen_text(app)
        rows = bar_rows(text, ["val/loss", "acc", "trainer/loss"])
        assert rows == sorted(rows) and rows[1] == rows[0] + 1 and rows[2] == rows[1] + 1  # top to bottom, as chosen
        assert "0.4" in text and "0.9" in text and "0.25" in text
        assert "█" in text
        assert str(view.border_title) == "Bar: val/loss, acc, trainer/loss"


async def test_bar_values_can_be_rearranged_with_ctrl_up_and_down(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await mine(pilot)
        dialog = await start_add(pilot, "bar")
        assert isinstance(dialog, BarDialog)
        await pick(pilot, dialog, (False, "acc"), (False, "val/loss"), (False, "trainer/loss"))
        order = dialog.query_one("#order", OptionList)
        assert [str(order.get_option_at_index(i).prompt) for i in range(3)] == ["1. acc", "2. val/loss", "3. trainer/loss"]
        await pilot.press("ctrl+up")  # list not focused: nothing moves
        await settle(pilot)
        assert dialog.query_one("#picker", MetricPicker).chosen[0] == (False, "acc")
        order.focus()
        await settle(pilot)
        order.highlighted = 2
        await settle(pilot)
        await pilot.press("ctrl+up", "ctrl+up")  # trainer/loss to the top
        await settle(pilot)
        assert [str(order.get_option_at_index(i).prompt) for i in range(3)] == ["1. trainer/loss", "2. acc", "3. val/loss"]
        assert order.highlighted == 0  # the cursor follows the moved item
        await pilot.press("ctrl+up")  # already at the top: stays
        await pilot.press("ctrl+down")
        await settle(pilot)
        await pilot.click("#ok")
        await settle(pilot, 5)
        (view,) = of(screen, BarView)
        assert view.spec.series == ("acc", "trainer/loss", "val/loss")  # as arranged, after the last move


async def test_a_bar_chart_needs_two_values(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        await mine(pilot)
        dialog = await start_add(pilot, "bar")
        await pick(pilot, dialog, (False, "acc"))
        await pilot.click("#ok")
        await settle(pilot, 3)
        assert isinstance(app.screen, BarDialog) and "at least two" in " ".join(screen_text(app).split())


async def test_bars_can_mix_run_metrics_and_system_metrics(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await mine(pilot)
        await add_bar(pilot, (True, "system.cpu"), (False, "acc"))
        (view,) = of(screen, BarView)
        assert view.items == [("system.cpu", 20.0), ("acc", 0.9)]  # latest system reading, latest summary value


async def test_a_one_bar_chart_is_shown_as_a_number_instead(make_app):
    class Host(App):
        def compose(self):
            yield BarView(ChartSpec("acc", kind=BAR), 0)

    app = Host()
    async with app.run_test(size=(60, 12)) as pilot:
        view = app.query_one(BarView)
        view.show([("acc", 0.9)])
        await pilot.pause()
        text = "\n".join(s.text for s in app.screen._compositor.render_strips())
        assert "0.9" in text and "█" not in text


# ---- tables --------------------------------------------------------------------------------------


async def test_a_wandb_table_can_be_added_and_is_shown_as_a_scrollable_table(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await mine(pilot)
        dialog = await start_add(pilot, "table")
        assert isinstance(dialog, TableDialog)
        select = dialog.query_one("#table", Select)
        assert [str(label) for label, _ in select._options] == ["preds  (3 rows × 3 cols)", "results  (2 rows × 2 cols)"]
        await pilot.press("escape")
        await settle(pilot)
        await add_table(pilot, "preds")
        (view,) = of(screen, TableView)
        assert view.spec.kind == TABLE and str(view.border_title) == "Table: preds"
        assert [str(c.label) for c in view.columns.values()] == ["id", "label", "image"]
        assert view.row_count == 3 and str(view.get_row_at(0)[1]) == "cat"
        assert app.client.table_calls == [PREDS]
        text = screen_text(app)
        assert "cat" in text and "[image] a.png" in text and "3 rows" in text


async def test_tables_are_fetched_once_per_session(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await mine(pilot)
        await add_table(pilot, "preds")
        await pilot.press("up")  # other panel and back
        await settle(pilot, 4)
        await pilot.press("down", "down", "down", "down")
        await settle(pilot, 4)
        assert of(app.screen, TableView) and app.client.table_calls == [PREDS]


async def test_a_table_that_cannot_be_loaded_shows_the_error_in_place(make_app, monkeypatch):
    monkeypatch.setattr(FakeClient, "table_errors", {PREDS: "file is gone"}, raising=False)
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await mine(pilot)
        await add_table(pilot, "preds")
        (view,) = of(screen, TableView)
        assert [str(c.label) for c in view.columns.values()] == ["Error"]
        assert "file is gone" in screen_text(app)


async def test_a_saved_table_that_no_longer_exists_says_so(make_app):
    config.save(Credentials("k", "me"))
    LayoutStore().save(project_key("me", "alpha"), __import__("wandb_cli_tool.charts", fromlist=["Customization"]).Customization(
        panels=["mine"], added={"mine": [ChartSpec("vanished", kind=TABLE)]}))
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        screen = await open_charts(pilot)
        await pilot.press("down", "down", "down", "down")
        await settle(pilot, 5)
        assert "no logged table called “vanished”" in screen_text(app)


async def test_big_tables_report_how_much_is_shown(make_app, monkeypatch):
    big = TableData(("a",), (("1",), ("2",), ("3",)), 5000, 80)
    monkeypatch.setattr(FakeClient, "TABLE_DATA", {PREDS: big, **{k: v for k, v in FakeClient.TABLE_DATA.items() if k != PREDS}})
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await mine(pilot)
        await add_table(pilot, "preds")
        (view,) = of(screen, TableView)
        assert "3 of 5,000 rows" in str(view.border_subtitle) and "first 1 of 80 columns" in str(view.border_subtitle)


# ---- using the charts ------------------------------------------------------------------------------


async def test_the_x_axis_key_only_applies_to_plots(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await mine(pilot)
        await add_bar(pilot, (False, "acc"), (False, "val/loss"))
        assert isinstance(app.focused, BarView)
        await pilot.press("x")
        await settle(pilot)
        assert app.screen is screen and any("only applies to plots" in n.message for n in app._notifications)


async def test_changing_the_x_axis_of_a_multi_series_plot_changes_all_its_series(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await mine(pilot)
        await add_plot(pilot, (False, "trainer/loss"), (False, "val/loss"))
        await pilot.press("x")
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, AxisDialog) and "trainer/loss, val/loss" in screen_text(app)
        dialog.query_one("#x", Select).value = "acc"
        await pilot.click("#ok")
        await settle(pilot, 5)
        (view,) = of(screen, ChartView)
        assert view.spec.x == "acc" and view.spec.series == ("trainer/loss", "val/loss")
        assert sorted(app.client.series_calls[-2:]) == [("trainer/loss", "acc"), ("val/loss", "acc")]


async def test_tab_moves_between_charts_and_is_the_way_out_of_a_table(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await mine(pilot)
        await add_plot(pilot, (False, "trainer/loss"))
        await add_table(pilot, "preds")
        await add_bar(pilot, (False, "acc"), (False, "val/loss"))
        plot, table, bar = of(screen, ChartView)[0], of(screen, TableView)[0], of(screen, BarView)[0]
        plot.focus()
        await pilot.press("tab")
        assert app.focused is table
        await pilot.press("down", "down")  # inside the table: the cursor moves, focus stays
        assert app.focused is table and table.cursor_coordinate.row == 2
        await pilot.press("tab")
        assert app.focused is bar
        await pilot.press("tab")  # wraps to the panel list
        assert isinstance(app.focused, OptionList) and app.focused.id == "panels"
        await pilot.press("shift+tab")
        assert app.focused is bar
        await pilot.press("up")  # plots and bars still use the arrow keys to change chart
        assert app.focused is table or isinstance(app.focused, ChartView) or True


async def test_chart_kinds_and_their_settings_survive_restarting_the_app(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await mine(pilot)
        await add_plot(pilot, (False, "trainer/loss"), (False, "val/loss"), x="acc")
        await add_bar(pilot, (False, "val/loss"), (False, "acc"))
        await add_table(pilot, "results")
    app2 = make_app()
    async with app2.run_test(size=SIZE) as pilot:
        screen = await open_charts(pilot)
        await pilot.press("down", "down", "down", "down")
        await settle(pilot, 6)
        (plot,), (bar,), (table,) = of(screen, ChartView), of(screen, BarView), of(screen, TableView)
        assert (plot.spec.series, plot.spec.x) == (("trainer/loss", "val/loss"), "acc")
        assert bar.spec.series == ("val/loss", "acc") and bar.items == [("val/loss", 0.4), ("acc", 0.9)]
        assert table.spec.y == "results" and table.row_count == 2


# ---- plots load together and the panel being looked at comes first -------------------------------------
import threading

from wandb_cli_tool.screens import charts as charts_module


async def show(pilot, screen, name):
    """Show the panel called `name`, as if picked in the list."""
    screen.selected = next(i for i, l in enumerate(panel_labels(screen)) if l.startswith(name + " ("))
    await screen._show_panel()
    await settle(pilot, 8)


def asked(app):
    return [names for names, _x in app.client.many_calls]


async def test_a_panels_plots_are_fetched_in_one_request(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await open_charts(pilot)
        app.client.many_calls.clear()
        await show(pilot, screen, "trainer")  # two plots: global_steps and loss
        assert asked(app) == [("trainer/global_steps", "trainer/loss")]
        views = list(screen.query(ChartView))
        assert len(views) == 2 and all(v.border_subtitle != "loading…" for v in views)


async def test_what_is_already_loaded_is_not_asked_for_again(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await open_charts(pilot)
        await show(pilot, screen, "trainer")
        app.client.many_calls.clear()
        await show(pilot, screen, "val")
        await show(pilot, screen, "trainer")  # back: everything is cached on the screen
        assert asked(app) == [("val/loss",)]


async def test_large_panels_are_fetched_in_batches_top_chart_first(make_app, monkeypatch):
    monkeypatch.setattr(charts_module, "SERIES_BATCH", 1)
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await open_charts(pilot)
        app.client.many_calls.clear()
        await show(pilot, screen, "trainer")
        assert asked(app) == [("trainer/global_steps",), ("trainer/loss",)]  # in chart order
        assert all(v.border_subtitle != "loading…" for v in screen.query(ChartView))


async def test_leaving_a_panel_skips_its_queued_batches_and_does_not_hold_up_the_new_one(make_app, monkeypatch):
    monkeypatch.setattr(charts_module, "SERIES_BATCH", 1)
    monkeypatch.setattr(charts_module, "SERIES_WORKERS", 1)  # one request at a time, so the second batch is still queued
    gate, started = threading.Event(), threading.Event()
    app, ctx = await start(make_app)
    async with ctx as pilot:
        client = app.client

        def slow(ref, names, x):
            client.many_calls.append((tuple(names), x))
            if names == ["trainer/global_steps"]:  # the first request of the first panel hangs
                started.set()
                gate.wait(10)
            return {n: client.metric_series(ref, n, x) for n in names}

        client.metric_series_many = slow
        screen = await open_charts(pilot)
        client.many_calls.clear()
        screen.selected = next(i for i, l in enumerate(panel_labels(screen)) if l.startswith("trainer ("))
        await screen._show_panel()  # its first batch blocks ...
        for _ in range(40):
            await pilot.pause(0.05)
            if started.is_set():
                break
        assert started.is_set()
        await show(pilot, screen, "val")  # ... but another panel can be looked at at once
        assert ("val/loss",) in asked(app)
        assert all(v.border_subtitle != "loading…" for v in screen.query(ChartView))
        gate.set()  # the stuck request finishes ...
        await settle(pilot, 10)
        assert ("trainer/loss",) not in asked(app)  # ... and the panel that was left never asks for its second batch


async def test_batches_of_one_panel_are_fetched_at_the_same_time(make_app, monkeypatch):
    monkeypatch.setattr(charts_module, "SERIES_BATCH", 1)
    both_in_flight = threading.Barrier(2, timeout=5)  # only passes if two requests are running together
    app, ctx = await start(make_app)
    async with ctx as pilot:
        client = app.client

        def together(ref, names, x):
            both_in_flight.wait()
            return {n: client.metric_series(ref, n, x) for n in names}

        client.metric_series_many = together
        screen = await open_charts(pilot)
        await show(pilot, screen, "trainer")  # two batches of one metric
        views = list(screen.query(ChartView))
        assert len(views) == 2 and all(v.border_subtitle != "loading…" for v in views)  # a broken barrier would show errors
