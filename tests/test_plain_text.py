"""Text from W&B (names, tags, groups, metrics, table cells, errors) is arbitrary. It must show
literally and never crash the app: Textual parses plain strings as markup, so `[/x]` used to
raise MarkupError and `run [v2]` lost its `[v2]`."""

from dataclasses import replace

from rich.text import Text
from textual.widgets import DataTable, OptionList, Select, Static, Tree

from chart_helpers import add_bar, add_plot, add_table, new_panel, settle, start_add
from conftest import FakeClient, FakePlayer
from test_run_detail_screens import screen_text, to_run
from test_run_screens import open_charts, panel_labels
from wandb_cli_tool import config
from wandb_cli_tool.api import FileInfo, ProjectInfo, RunInfo
from wandb_cli_tool.app import WandbCliApp
from wandb_cli_tool.config import Credentials
from wandb_cli_tool.plain import plain, plain_title
from wandb_cli_tool.tables import TableData, TableInfo

SIZE = (150, 50)
RUN = "run [v2] [/x]"
GROUP, TAG1, TAG2 = "g[0] [/g]", "t [/y]", "[bold]z"
METRICS = ["weights[0]/grad [/x]", "[bold]loss", "acc"]
TABLE_PATH = "media/table/bad_1_0123456789abcdef0123.table.json"


class Hostile(FakeClient):
    PROJECTS = {"me": ["alpha", "proj [/oops]"]}
    RUNS = {("me", "alpha"): [RunInfo("id1", RUN, "finished", "2024-05-01T09:00:00", "2024-05-01T08:00:00", GROUP, (TAG1, TAG2))]}
    CONFIG = {"lr [/o]": 0.1, "nested[0]": {"k [/k]": "v [/v]"}}
    LOGS = ["[ERROR] boom [/oops]", "plain"]
    FILES = [FileInfo("a[0] [bold].png", 10, "image/png", "m"), FileInfo("dir [x]/y.txt", 5, "text/plain", "n")]
    METRICS = METRICS
    VALUES = {"weights[0]/grad [/x]": 1.5, "[bold]loss": 0.25, "acc": 0.9}
    TABLES = [TableInfo("bad [/key]", TABLE_PATH, 2, 2)]
    TABLE_DATA = {TABLE_PATH: TableData(("[CLS] col [/s]", "n"), (("[CLS] hi [/s]", "[1, 2]"), ("a [b]", "x[0]")), 2, 2)}

    def run_details(self, ref):
        return replace(self.RUNS[("me", "alpha")][0], name=self.names.get(ref.id, RUN))

    def series_for(self, y):
        return [(0.0, 1.0), (1.0, 2.0)]

    def metric_series(self, ref, y, x):
        return [(0.0, 1.0), (1.0, 2.0)]


async def hostile_app(client=Hostile, size=SIZE):
    config.save(Credentials("k", "me"))
    app = WandbCliApp(client_factory=client, audio_player=FakePlayer())
    return app, app.run_test(size=size)


def test_plain_shows_text_literally():
    assert plain("a [/x] b[0]").plain == "a [/x] b[0]" and isinstance(plain("x"), Text)
    assert str(plain_title("[/x]")) == "[/x]"
    assert plain(5).plain == "5"


async def test_lists_show_bracketed_names_groups_and_tags_exactly(make_app):
    app, ctx = await hostile_app()
    async with ctx as pilot:
        await settle(pilot, 5)
        projects = app.screen.query_one("#items", DataTable)
        assert [str(projects.get_row_at(i)[0]) for i in range(projects.row_count)] == ["alpha", "proj [/oops]"]
        await pilot.press("enter")  # alpha
        await settle(pilot, 5)
        row = [str(c) for c in app.screen.query_one("#items", DataTable).get_row_at(0)]
        assert row[0] == RUN and row[4] == GROUP and row[5] == f"{TAG1}, {TAG2}"  # nothing swallowed
        assert RUN in screen_text(app) and TAG1 in screen_text(app)


async def test_run_screen_title_tags_group_and_dialogs_with_bracketed_text(make_app):
    app, ctx = await hostile_app()
    async with ctx as pilot:
        await to_run(pilot)
        text = screen_text(app)
        assert f"Run — {RUN}" in text and TAG1 in text and TAG2 in text and GROUP in text
        await pilot.press("g")  # dialogs list the hostile group
        await settle(pilot)
        assert GROUP in screen_text(app) and GROUP in [str(l) for l, _ in app.screen.query_one("#group", Select)._options]
        await pilot.press("escape")
        await pilot.press("t")
        await settle(pilot)
        assert TAG1 in screen_text(app)
        await pilot.press("escape")
        await pilot.press("c")  # the copy confirmation names the run
        await settle(pilot, 6)
        assert RUN in " ".join(screen_text(app).split())


async def test_config_files_logs_and_metrics_with_bracketed_text(make_app):
    app, ctx = await hostile_app()
    async with ctx as pilot:
        await to_run(pilot)
        await pilot.press("down", "down", "enter")  # Config
        await settle(pilot, 4)
        tree = app.screen.query_one("#config", Tree)
        assert [str(c.label) for c in tree.root.children][0] == "lr [/o]: 0.1"
        await pilot.press("escape", "up", "up", "enter")  # Logs
        await settle(pilot, 5)
        assert "[ERROR] boom [/oops]" in screen_text(app)
        await pilot.press("escape")
        await settle(pilot)
        menu = app.screen.query_one("#menu", OptionList)
        menu.highlighted = 3  # Files
        await pilot.press("enter")
        await settle(pilot, 8)
        files = app.screen.query_one("#files", Tree)
        labels = [str(c.label) for c in files.root.children]
        assert any(l.startswith("dir [x]/") for l in labels) and any(l.startswith("a[0] [bold].png") for l in labels), labels


async def test_error_messages_containing_brackets_are_shown_not_parsed(make_app):
    class Failing(Hostile):
        def iter_projects(self, entity):
            raise RuntimeError("[Errno 111] [/oops] connection refused")
            yield

        def console_lines(self, ref):
            raise RuntimeError("[{'message': 'bad'}] [/x]")
            yield

        def run_config(self, ref):
            raise RuntimeError("[/oops] config")

    app, ctx = await hostile_app(Failing)
    async with ctx as pilot:
        await settle(pilot, 5)
        assert "Error: [Errno 111] [/oops] connection refused" in screen_text(app)  # projects list status


async def test_charts_with_bracketed_metric_panel_and_table_text(make_app):
    app, ctx = await hostile_app()
    async with ctx as pilot:
        screen = await open_charts(pilot)
        await new_panel(pilot, "mine [/oops]")
        assert panel_labels(screen)[-1] == "mine [/oops] (0)"
        await add_plot(pilot, (False, METRICS[0]), (False, METRICS[1]))
        assert "grad [/x], [bold]loss" in screen_text(app)  # the chart title: short names, nothing swallowed
        await add_bar(pilot, (False, METRICS[0]), (False, METRICS[2]))
        assert "weights[0]/grad [/x]" in screen_text(app)
        await add_table(pilot, "bad [/key]")
        text = screen_text(app)
        assert "[CLS] hi [/s]" in text and "[1, 2]" in text and "x[0]" in text and "[CLS] col [/s]" in text
        assert "Table: bad [/key]" in text


async def test_dialog_lists_show_bracketed_metric_names(make_app):
    app, ctx = await hostile_app()
    async with ctx as pilot:
        await open_charts(pilot)
        await new_panel(pilot, "mine")
        dialog = await start_add(pilot, "plot")
        text = screen_text(app)
        assert "weights[0]/grad [/x]" in text and "[bold]loss" in text
        x_labels = [str(l) for l, _ in dialog.query_one("#x", Select)._options]
        assert METRICS[0] in x_labels  # x-axis choices include other metrics


async def test_notifications_with_bracketed_text_do_not_crash(make_app):
    app, ctx = await hostile_app()
    async with ctx as pilot:
        await to_run(pilot)
        app.notify("Group set to “g [/oops]” and [bold]stuff[/]")  # used to be parsed as markup
        await pilot.press("g")
        await settle(pilot)
        app.screen.query_one("#new-group").value = "new [/group]"
        app.screen.query_one("#new-group").focus()
        await pilot.press("enter")
        await settle(pilot, 5)
        assert any("new [/group]" in n.message for n in app._notifications)
        assert "new [/group]" in screen_text(app)
