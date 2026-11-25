"""Selecting runs and comparing them: the runs list, the compare screen, the key bar."""

from textual.widgets import DataTable, OptionList, TabbedContent

from conftest import FakeClient
from wandb_cli_tool import config
from wandb_cli_tool.app import WandbCliApp
from wandb_cli_tool.config import Credentials
from wandb_cli_tool.screens import runs as runs_module
from wandb_cli_tool.screens.compare import CompareScreen
from wandb_cli_tool.screens.compare_widgets import CompareBarView, ComparePlotView
from wandb_cli_tool.screens.keybar import KeyBar
from wandb_cli_tool.screens.run import RunScreen
from wandb_cli_tool.screens.runs import RunsScreen


class CompareClient(FakeClient):
    """Runs that differ: in config, in what they logged, and in their latest values."""

    CONFIGS = {
        "id1": {"lr": 0.1, "seed": 1, "model": {"name": "resnet"}, "dropout": 0.5},
        "id2": {"lr": 0.01, "seed": 1, "model": {"name": "resnet"}},
        "id3": {"lr": 0.1, "seed": 1, "model": {"name": "vit"}},
    }
    METRICS_BY_RUN = {
        "id1": ["acc", "trainer/loss", "val/loss"],
        "id2": ["acc", "trainer/loss"],
        "id3": ["acc", "trainer/loss", "val/loss", "only/here"],
    }
    VALUES_BY_RUN = {"id1": {"acc": 0.9, "val/loss": 0.4}, "id2": {"acc": 0.8}, "id3": {"acc": 0.7, "val/loss": 0.2}}

    def run_config(self, ref):
        return dict(self.CONFIGS[ref.id])

    def metric_keys(self, ref):
        return list(self.METRICS_BY_RUN[ref.id])

    def metric_values(self, ref):
        return dict(self.VALUES_BY_RUN[ref.id])

    def metric_series(self, ref, y, x):
        self.series_calls.append((ref.id, y, x))
        if y in ("acc", "only/here"):  # logged once
            return [(3.0, 0.25)]
        shift = {"id1": 0, "id2": 1, "id3": 2}[ref.id]
        return [(float(i), float(i + shift)) for i in range(5)]


async def settle(pilot, n=4):
    for _ in range(n):
        await pilot.pause()


async def open_runs(pilot):
    await settle(pilot, 4)
    await pilot.press("enter")  # alpha
    await settle(pilot, 5)
    assert isinstance(pilot.app.screen, RunsScreen)
    return pilot.app.screen


def make(client=CompareClient):
    config.save(Credentials("k", "me"))
    return WandbCliApp(client_factory=client)


def key_labels(screen) -> str:
    return screen.query_one(KeyBar).query_one("KeyLine").render().plain.replace("\xa0", " ")


async def select_runs(pilot, *rows):
    """Mark the rows at these positions (the list is id1, id2, id3 in this order)."""
    for position in range(max(rows) + 1):
        if position in rows:
            await pilot.press("space")
        await pilot.press("down")
    await settle(pilot, 2)


async def compare(pilot, *rows):
    screen = await open_runs(pilot)
    await select_runs(pilot, *rows)
    await pilot.press("c")
    await settle(pilot, 8)
    assert isinstance(pilot.app.screen, CompareScreen)
    return pilot.app.screen


# ---- selecting ---------------------------------------------------------------------------------
async def test_space_marks_runs_and_compare_needs_two():
    app = make()
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_runs(pilot)
        assert screen.check_action("compare", ()) is False and "Compare" not in key_labels(screen)
        await pilot.press("space")
        await settle(pilot)
        assert screen.selected == ["id1"] and screen.check_action("compare", ()) is False
        assert "✓" in str(screen.query_one("#items", DataTable).get_row_at(0)[0])
        await pilot.press("c")  # only one: nothing happens
        await settle(pilot)
        assert app.screen is screen
        await pilot.press("down", "space")
        await settle(pilot)
        assert screen.selected == ["id1", "id2"] and screen.check_action("compare", ()) is True
        assert "Compare selected" in key_labels(screen)
        assert "2 selected" in str(screen.query_one("#status").render())
        await pilot.press("space")  # again: unmark
        await settle(pilot)
        assert screen.selected == ["id1"]
        assert "✓" not in str(screen.query_one("#items", DataTable).get_row_at(1)[0])


async def test_any_number_can_be_selected_but_only_a_few_compared(monkeypatch):
    monkeypatch.setattr(runs_module, "MAX_RUNS", 2)
    app = make()
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_runs(pilot)
        await select_runs(pilot, 0, 1, 2)
        assert screen.selected == ["id1", "id2", "id3"]  # bulk actions have no such limit
        await pilot.press("c")
        await settle(pilot)
        assert app.screen is screen
        assert any("At most 2 runs can be compared at once (3 are selected)" in n.message for n in app._notifications)
        await pilot.press("u")
        await settle(pilot)
        assert screen.selected == [] and screen.check_action("clear_selection", ()) is False


async def test_the_selection_survives_a_refresh():
    app = make()
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_runs(pilot)
        await select_runs(pilot, 0, 2)
        await pilot.press("r")
        await settle(pilot, 8)
        assert screen.selected == ["id1", "id3"]
        table = screen.query_one("#items", DataTable)
        assert [("✓" in str(table.get_row_at(i)[0])) for i in range(3)] == [True, False, True]


# ---- the compare screen --------------------------------------------------------------------------
async def test_overview_has_one_column_per_run_and_marks_differences():
    app = make()
    async with app.run_test(size=(150, 40)) as pilot:
        screen = await compare(pilot, 0, 2)
        table = screen.query_one("#overview", DataTable)
        assert [str(c.label) for c in table.columns.values()] == ["", "■ run-1", "■ run-3"]
        rows = {str(table.get_row_at(i)[0]).strip(" ≠"): [str(c) for c in table.get_row_at(i)[1:]] for i in range(table.row_count)}
        assert rows["Status"] == ["● running", "failed"]
        assert rows["Group"] == ["sweep-a", "sweep-b"] and rows["Tags"] == ["baseline, v2", "v2"]
        assert rows["Host"] == ["gpu-box-1", "gpu-box-1"] and rows["ID"] == ["id1", "id3"]
        labels = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
        assert any(l.startswith("≠ Status") for l in labels) and any(l.startswith("  Host") for l in labels)
        await pilot.press("d")  # only the differences
        await settle(pilot)
        assert "Host" not in [str(table.get_row_at(i)[0]).strip(" ≠") for i in range(table.row_count)]


async def test_legend_names_every_run():
    app = make()
    async with app.run_test(size=(150, 40)) as pilot:
        screen = await compare(pilot, 0, 1, 2)
        legend = screen.query_one("#legend").render().plain
        assert "run-1" in legend and "run-2" in legend and "run-3" in legend


async def test_config_tab_lists_only_what_differs():
    app = make()
    async with app.run_test(size=(150, 40)) as pilot:
        screen = await compare(pilot, 0, 1, 2)
        await pilot.press("3")
        await settle(pilot)
        assert screen.active_tab == "config"
        table = screen.query_one("#config-table", DataTable)
        rows = {str(table.get_row_at(i)[0]): [str(c) for c in table.get_row_at(i)[1:]] for i in range(table.row_count)}
        assert rows == {
            "dropout": ["0.5", "—", "—"],  # only run 1 has it
            "lr": ["0.1", "0.01", "0.1"],
            "model.name": ["resnet", "resnet", "vit"],
        }
        assert "2 entries differ, 1 are only in some runs" in str(screen.query_one("#status").render())
        await pilot.press("d")  # show the identical ones too
        await settle(pilot)
        assert "seed" in [str(table.get_row_at(i)[0]) for i in range(table.row_count)]


async def test_identical_configs_say_so():
    class Same(CompareClient):
        CONFIGS = {k: {"lr": 0.1} for k in ("id1", "id2", "id3")}

    app = make(Same)
    async with app.run_test(size=(150, 40)) as pilot:
        screen = await compare(pilot, 0, 1)
        await pilot.press("3")
        await settle(pilot)
        assert screen.query_one("#config-table", DataTable).row_count == 0
        assert "identical" in str(screen.query_one("#status").render())


async def test_charts_overlay_the_runs_and_show_single_values_as_bars():
    app = make()
    async with app.run_test(size=(150, 50)) as pilot:
        screen = await compare(pilot, 0, 1, 2)
        await pilot.press("4")
        await settle(pilot, 6)
        assert screen.active_tab == "charts"
        names = [str(screen.query_one("#panels", OptionList).get_option_at_index(i).prompt) for i in range(screen.query_one("#panels", OptionList).option_count)]
        assert names[0].startswith("System")  # the system panel comes first, as in a single run
        panel = {n.split(" (")[0]: i for i, n in enumerate(names)}
        # the "Other" panel holds acc: logged once per run, so bars (one per run), not lines
        screen.selected = panel["Other"]
        await screen._show_panel()
        await settle(pilot, 8)
        [view] = screen.query(ComparePlotView)
        assert view.bars is not None and len(view.bars[0][1]) == 3 and view.bars[0][1] == [0.25, 0.25, 0.25]
        # the "trainer" panel: curves, one per run
        screen.selected = panel["trainer"]
        await screen._show_panel()
        await settle(pilot, 8)
        views = list(screen.query(ComparePlotView))
        assert views and all(v.bars is None for v in views)
        client_calls = {(rid, y) for rid, y, _x in app.client.series_calls}
        assert {("id1", "trainer/loss"), ("id2", "trainer/loss"), ("id3", "trainer/loss")} <= client_calls


async def test_a_metric_only_some_runs_logged_is_drawn_for_those_runs():
    app = make()
    async with app.run_test(size=(150, 50)) as pilot:
        screen = await compare(pilot, 0, 1)  # id1 and id2: neither has val/loss for id2
        await pilot.press("4")
        await settle(pilot, 6)
        names = [str(screen.query_one("#panels", OptionList).get_option_at_index(i).prompt).split(" (")[0]
                 for i in range(screen.query_one("#panels", OptionList).option_count)]
        screen.selected = names.index("val")
        await screen._show_panel()
        await settle(pilot, 8)
        [view] = screen.query(ComparePlotView)
        assert ("id2", "val/loss", "_step") not in {tuple(c) for c in app.client.series_calls}  # never requested
        assert "run-2" in str(view.border_subtitle)  # named as having no data
        assert {c for c in app.client.series_calls if c[0] == "id1"} >= {("id1", "val/loss", "_step")}


async def test_bar_charts_show_a_bar_per_run_for_each_metric():
    from wandb_cli_tool.charts import BAR, ChartSpec
    from wandb_cli_tool.layout_store import LayoutStore, project_key
    from wandb_cli_tool.charts import Customization

    app = make()
    custom = Customization(panels=["Summary"], added={"Summary": [ChartSpec("acc", kind=BAR, more=("val/loss",))]})
    app.layout_store.save(project_key("me", "alpha"), custom)
    async with app.run_test(size=(150, 50)) as pilot:
        screen = await compare(pilot, 0, 1, 2)
        await pilot.press("4")
        await settle(pilot, 6)
        names = [str(screen.query_one("#panels", OptionList).get_option_at_index(i).prompt).split(" (")[0]
                 for i in range(screen.query_one("#panels", OptionList).option_count)]
        screen.selected = names.index("Summary")
        await screen._show_panel()
        await settle(pilot, 6)
        [view] = screen.query(CompareBarView)
        assert view.groups == [("acc", [0.9, 0.8, 0.7]), ("loss", [0.4, None, 0.2])] or view.groups[0] == ("acc", [0.9, 0.8, 0.7])
        assert view.groups[1][1] == [0.4, None, 0.2]


async def test_the_compare_screen_is_not_reachable_from_a_run_and_run_keys_stay_clean():
    app = make()
    async with app.run_test(size=(150, 40)) as pilot:
        await open_runs(pilot)
        await pilot.press("enter")
        await settle(pilot, 6)
        assert isinstance(app.screen, RunScreen)
        bar = key_labels(app.screen)
        assert "Logout" not in bar and "Change entity" not in bar and "Quit" not in bar
        assert "Set group" in bar and "Rename" in bar


async def test_logout_and_entity_are_listed_only_on_the_projects_page():
    app = make()
    async with app.run_test(size=(150, 40)) as pilot:
        await settle(pilot, 4)
        bar = key_labels(app.screen)
        assert "Logout" in bar and "Change entity" in bar and "Quit" in bar
        await pilot.press("enter")
        await settle(pilot, 5)
        assert "Logout" not in key_labels(app.screen)
        assert app.check_action("logout", ()) is True  # still works everywhere, just not listed


async def test_the_key_bar_wraps_instead_of_scrolling():
    app = make()
    async with app.run_test(size=(60, 40)) as pilot:
        screen = await open_runs(pilot)
        await pilot.press("space", "down", "space")
        await settle(pilot)
        bar = screen.query_one(KeyBar)
        assert 1 < bar.size.height <= 3  # more than a line, but bounded


# ---- metrics, tables, images -----------------------------------------------------------------------
from wandb_cli_tool.charts import IMAGE, TABLE, ChartSpec, Customization
from wandb_cli_tool.images import ImageInfo, ImageSeries, ImageStep
from wandb_cli_tool.layout_store import project_key
from wandb_cli_tool.screens.compare_widgets import CompareImageView, CompareTableView
from wandb_cli_tool.tables import TableData, TableInfo


class MediaClient(CompareClient):
    """id1 and id3 logged a table and images; id2 logged neither. Their contents differ."""

    def run_tables(self, ref):
        return [TableInfo("preds", f"media/table/{ref.id}.table.json", 2, 2)] if ref.id != "id2" else []

    def table_data(self, ref, path):
        rows = {"id1": (("a", "1"), ("b", "2")), "id3": (("b", "9"), ("c", "3"))}[ref.id]
        return TableData(("key", "value"), rows, len(rows), 2)

    def run_images(self, ref):
        return [ImageInfo("samples")] if ref.id != "id2" else []

    def image_steps(self, ref, key):
        numbers = {"id1": (10, 20, 30), "id3": (20, 40)}[ref.id]
        return ImageSeries(tuple(ImageStep(n, (f"media/images/{ref.id}_{n}.png",), (f"sha{ref.id}{n}",), (None,), ()) for n in numbers), len(numbers))


def media_layout(app):
    app.layout_store.save(project_key("me", "alpha"), Customization(
        panels=["Media"], added={"Media": [ChartSpec("preds", kind=TABLE), ChartSpec("samples", kind=IMAGE)]}))


async def show_panel(pilot, screen, name):
    await pilot.press("4")
    await settle(pilot, 6)
    options = screen.query_one("#panels", OptionList)
    labels = [str(options.get_option_at_index(i).prompt).split(" (")[0] for i in range(options.option_count)]
    screen.selected = labels.index(name)
    await screen._show_panel()
    await settle(pilot, 10)


def metric_cells(screen):
    table = screen.query_one("#metrics", DataTable)
    return {str(table.get_row_at(i)[0]): [str(c) for c in table.get_row_at(i)[1:]] for i in range(table.row_count)}


async def test_metrics_tab_marks_the_best_run_lower_is_better_until_flipped():
    app = make()
    async with app.run_test(size=(150, 40)) as pilot:
        screen = await compare(pilot, 0, 1, 2)
        await pilot.press("2")
        await settle(pilot)
        assert screen.active_tab == "metrics"
        table = screen.query_one("#metrics", DataTable)
        assert [str(c.label) for c in table.columns.values()] == ["Metric", "■ run-1", "■ run-2", "■ run-3", "Better", "Best"]
        rows = metric_cells(screen)
        assert rows["acc"] == ["0.9", "0.8", "0.7", "↓ lower", " ■ run-3"]  # the default for every metric
        assert rows["val/loss"] == ["0.4", "—", "0.2", "↓ lower", " ■ run-3"]
        assert "b flips the one under the cursor" in str(screen.query_one("#status").render())
        await pilot.press("b")  # the cursor is on "acc": higher is better for it
        await settle(pilot)
        rows = metric_cells(screen)
        assert rows["acc"] == ["0.9", "0.8", "0.7", "↑ higher", " ■ run-1"]
        assert rows["val/loss"][3] == "↓ lower"  # only that metric changed
        assert table.cursor_row == 0  # the cursor stays on the row
        await pilot.press("b")  # and back
        await settle(pilot)
        assert metric_cells(screen)["acc"][3:] == ["↓ lower", " ■ run-3"]


async def test_b_flips_all_metrics_and_the_choice_is_remembered_for_the_project():
    app = make()
    async with app.run_test(size=(150, 40)) as pilot:
        screen = await compare(pilot, 0, 1, 2)
        await pilot.press("2")
        await settle(pilot)
        await pilot.press("down", "b")  # val/loss: higher
        await settle(pilot)
        assert screen.higher == {"val/loss"}
        await pilot.press("B")  # flip all: acc becomes higher, val/loss lower again
        await settle(pilot)
        assert screen.higher == {"acc"}
        assert app.preferences.get("compare_higher_is_better") == {"me/alpha": ["acc"]}
    # a later comparison in the same project starts from that choice
    app2 = make()
    async with app2.run_test(size=(150, 40)) as pilot:
        screen = await compare(pilot, 0, 1)
        await pilot.press("2")
        await settle(pilot)
        assert screen.higher == {"acc"} and metric_cells(screen)["acc"][2] == "↑ higher"  # two runs: run, run, Better, Best
        await pilot.press("b")  # flipping it back forgets it
        await settle(pilot)
        assert app2.preferences.get("compare_higher_is_better") == {}


async def test_the_flip_keys_belong_to_the_metrics_tab_only():
    app = make()
    async with app.run_test(size=(150, 40)) as pilot:
        screen = await compare(pilot, 0, 1)
        assert screen.check_action("flip_best", ()) is False  # on the overview
        await pilot.press("2")
        await settle(pilot)
        assert screen.check_action("flip_best", ()) is True and "Flip lower/higher" in keys_of(screen)


def keys_of(screen):
    from wandb_cli_tool.screens.keybar import KeyBar
    return screen.query_one(KeyBar).query_one("KeyLine").render().plain.replace("\xa0", " ")


async def test_tables_of_all_runs_appear_in_one_table_with_a_color_per_run():
    app = make(MediaClient)
    media_layout(app)
    async with app.run_test(size=(150, 50)) as pilot:
        screen = await compare(pilot, 0, 1, 2)
        await show_panel(pilot, screen, "Media")
        [view] = screen.query(CompareTableView)
        rows = [[str(c) for c in view.get_row_at(i)] for i in range(view.row_count)]
        assert [str(c.label) for c in view.columns.values()] == ["Run", "key", "value"]
        assert rows == [["■ run-1", "a", "1"], ["■ run-1", "b", "2"], ["■ run-3", "b", "9"], ["■ run-3", "c", "3"]]
        assert "not in: run-2" in str(view.border_subtitle) and "grouped by run" in str(view.border_subtitle)
        view.focus()
        await pilot.press("v")  # line the rows up by key
        await settle(pilot)
        rows = [[str(c) for c in view.get_row_at(i)][:2] for i in range(view.row_count)]
        assert rows == [["■ run-1", "a"], ["■ run-1", "b"], ["■ run-3", "b"], ["■ run-3", "c"]]  # b's sit together
        assert "lined up by first column" in str(view.border_subtitle)


async def test_images_of_all_runs_share_one_slider():
    app = make(MediaClient)
    media_layout(app)
    async with app.run_test(size=(150, 50)) as pilot:
        screen = await compare(pilot, 0, 1, 2)
        await show_panel(pilot, screen, "Media")
        [view] = screen.query(CompareImageView)
        assert view.steps == [10, 20, 30, 40] and view.target == 40  # starts at the newest step of any run
        assert view.step_for(0).step == 30 and view.step_for(2).step == 40 and view.step_for(1) is None
        labels = [l.render().plain for l in view.query(".cell-label")]
        assert "step 30" in labels[0] and "not logged" in labels[1] and "step 40" in labels[2]
        assert sorted(app.client.downloads) == ["media/images/id1_30.png", "media/images/id3_40.png"]  # one per run, not run-2
        view.focus()
        await pilot.press("left", "left")  # step 20: both runs have it exactly
        await settle(pilot, 8)
        assert view.target == 20 and view.step_for(0).step == 20 and view.step_for(2).step == 20
        assert sorted(app.client.downloads)[:2] == ["media/images/id1_20.png", "media/images/id1_30.png"] or "media/images/id3_20.png" in app.client.downloads
        assert "media/images/id1_20.png" in app.client.downloads and "media/images/id3_20.png" in app.client.downloads


async def test_media_missing_from_every_run_is_a_short_note():
    class NoMedia(CompareClient):
        TABLES, IMAGES = [], []

    app = make(NoMedia)  # these runs logged no tables or images
    media_layout(app)
    async with app.run_test(size=(150, 50)) as pilot:
        screen = await compare(pilot, 0, 1)
        await show_panel(pilot, screen, "Media")
        [table_view] = screen.query(CompareTableView)
        [image_view] = screen.query(CompareImageView)
        assert "No compared run logged a table called “preds”" in str(table_view.get_row_at(0)[0])
        assert "No compared run logged images called “samples”" in str(image_view.query_one("#img-info").render())


async def test_the_legend_and_status_stay_in_view_and_the_tabs_fill_the_rest():
    """Without a fixed height the whole screen scrolled and pushed the legend off the top."""
    app = make()
    async with app.run_test(size=(120, 30)) as pilot:
        screen = await compare(pilot, 0, 1, 2)
        for key in ("1", "2", "3", "4"):
            await pilot.press(key)
            await settle(pilot, 4)
            assert screen.scroll_y == 0
            assert screen.query_one("#legend").region.y < screen.query_one("#tabs").region.y
            assert screen.query_one("#legend").region.height >= 1 and screen.query_one("#status").region.height >= 1
            assert screen.query_one("#tabs").region.bottom <= screen.size.height


async def test_a_selected_run_keeps_its_whole_name_visible():
    app = make()
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_runs(pilot)
        await pilot.press("space")
        await settle(pilot)
        table = screen.query_one("#items", DataTable)
        column = next(iter(table.columns.values()))
        assert column.get_render_width(table) >= len("✓ run-1")  # the ✓ did not cut the name
