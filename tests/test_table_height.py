from textual.app import App

from chart_helpers import add_table, new_panel, settle
from conftest import FakeClient
from test_run_screens import open_charts
from wandb_cli_tool import config
from wandb_cli_tool.charts import TABLE, ChartSpec
from wandb_cli_tool.config import Credentials
from wandb_cli_tool.screens.chart_widgets import TABLE_MAX_HEIGHT, TABLE_MIN_HEIGHT, TableView, table_height
from wandb_cli_tool.tables import TableData

PREDS = "media/table/preds_3_ab12cd34ef56ab78cd90.table.json"
RESULTS = "media/table/results_1_0123456789abcdef0123.table.json"


def test_height_is_border_plus_header_plus_rows_within_limits():
    assert table_height(3) == 6 and table_height(10) == 13
    assert table_height(0) == table_height(1) == TABLE_MIN_HEIGHT == 4  # never smaller than border + header + a line
    assert table_height(17) == TABLE_MAX_HEIGHT == 20 and table_height(10_000) == TABLE_MAX_HEIGHT
    assert [table_height(n) for n in range(0, 25)] == sorted(table_height(n) for n in range(0, 25))  # never shrinks as rows grow


def data(rows, cols=2):
    return TableData(tuple(f"c{i}" for i in range(cols)), tuple(tuple(f"r{r}c{c}" for c in range(cols)) for r in range(rows)), rows, cols)


async def mount_table(rows, cols=2, width=100):
    class Host(App):
        def compose(self):
            yield TableView(ChartSpec("t", kind=TABLE), 0)

    app = Host()
    return app, app.run_test(size=(width, 40))


async def test_tables_are_exactly_as_tall_as_their_rows_need():
    for rows, expected in ((1, 4), (2, 5), (5, 8), (12, 15)):
        app, ctx = await mount_table(rows)
        async with ctx as pilot:
            view = app.query_one(TableView)
            view.show_table(data(rows))
            await settle(pilot, 4)
            assert view.outer_size.height == expected, rows  # includes the border


async def test_big_tables_stop_growing_and_scroll_instead():
    app, ctx = await mount_table(500)
    async with ctx as pilot:
        view = app.query_one(TableView)
        view.show_table(data(500))
        await settle(pilot, 4)
        assert view.outer_size.height == TABLE_MAX_HEIGHT and view.row_count == 500


async def test_empty_and_error_tables_are_small():
    app, ctx = await mount_table(0)
    async with ctx as pilot:
        view = app.query_one(TableView)
        view.show_table(TableData((), (), 0, 0))
        await settle(pilot, 3)
        assert view.outer_size.height == TABLE_MIN_HEIGHT and str(view.border_subtitle) == "empty table"
        view.fail("boom")
        await settle(pilot, 3)
        assert view.outer_size.height == table_height(1)


async def test_a_loading_table_is_not_tall():
    app, ctx = await mount_table(0)
    async with ctx as pilot:
        await settle(pilot, 3)
        assert app.query_one(TableView).outer_size.height <= 6  # before any data arrives


async def test_a_very_wide_table_gets_one_more_line_for_its_horizontal_scrollbar():
    app, ctx = await mount_table(3, width=60)
    async with ctx as pilot:
        view = app.query_one(TableView)
        view.show_table(data(3, cols=30))  # far wider than 60 columns
        await settle(pilot, 6)
        assert view.show_horizontal_scrollbar
        assert view.outer_size.height == table_height(3) + 1
        assert view.size.height >= 4  # header + all 3 rows still visible above the scrollbar


async def test_tables_in_the_charts_screen_fit_their_data(make_app, monkeypatch):
    big = data(100, cols=3)
    monkeypatch.setattr(FakeClient, "TABLE_DATA", {PREDS: FakeClient.TABLE_DATA[PREDS], RESULTS: big})
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 60)) as pilot:
        screen = await open_charts(pilot)
        await new_panel(pilot, "mine")
        await add_table(pilot, "preds")    # 3 rows
        await add_table(pilot, "results")  # 100 rows
        small, large = list(screen.query(TableView))
        assert small.outer_size.height == 6  # not a mostly-empty 20-line box
        assert large.outer_size.height == TABLE_MAX_HEIGHT
        text = "\n".join(s.text for s in screen._compositor.render_strips())
        assert "bird" in text  # all three rows of the small table are visible
