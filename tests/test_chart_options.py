"""y-limits, smoothing and log axes: the model, the saved format, the plots, the dialog."""

import math

import pytest

from wandb_cli_tool.charts import (
    BAR, IMAGE, PLOT, TABLE, ChartLayout, ChartOptions, ChartSpec, Customization, Panel, apply_customization, chart_key,
    parse_options,
)
from wandb_cli_tool.series import prepare_points, smooth_ema


# ---- smoothing and log axes -----------------------------------------------------------------
def test_no_smoothing_returns_the_points_unchanged():
    pts = [(0.0, 1.0), (1.0, 3.0)]
    assert smooth_ema(pts, 0) == pts and smooth_ema(pts, 0) is not pts


def test_smoothing_is_a_bias_corrected_moving_average():
    out = smooth_ema([(0.0, 10.0), (1.0, 0.0), (2.0, 10.0)], 0.5)
    assert out[0] == (0.0, 10.0)  # the first point is not pulled towards 0
    assert out[1][1] == pytest.approx((0.5 * 0.5 * 10) / (1 - 0.25))  # 0.5*(0.5*10)+0.5*0 over the bias term
    assert [p[0] for p in out] == [0.0, 1.0, 2.0]
    assert all(0 < y < 10 for _, y in out[1:])


def test_a_constant_series_stays_constant_however_smooth():
    out = smooth_ema([(float(i), 4.0) for i in range(20)], 0.99)
    assert all(y == pytest.approx(4.0) for _, y in out)


def test_smoothing_skips_non_finite_values():
    out = smooth_ema([(0.0, 1.0), (1.0, math.nan), (2.0, 1.0)], 0.5)
    assert [p[0] for p in out] == [0.0, 2.0]


def test_log_axes_drop_what_they_cannot_show_and_count_it():
    pts = [(0.0, 1.0), (1.0, 0.0), (2.0, -3.0), (3.0, 5.0)]
    assert prepare_points(pts, ChartOptions(logy=True)) == ([(0.0, 1.0), (3.0, 5.0)], 2)
    assert prepare_points(pts, ChartOptions(logx=True)) == ([(1.0, 0.0), (2.0, -3.0), (3.0, 5.0)], 1)
    assert prepare_points(pts, ChartOptions()) == (pts, 0)


# ---- typed values ----------------------------------------------------------------------------
def test_parse_options_accepts_blank_as_automatic():
    assert parse_options("", " ", "", False, False) == ChartOptions()
    assert parse_options("0", "1.5", "0.9", True, False) == ChartOptions(0.0, 1.5, 0.9, True, False)


@pytest.mark.parametrize("args,word", [
    (("a", "", "", False, False), "Y min"),
    (("", "inf", "", False, False), "Y max"),
    (("2", "1", "", False, False), "smaller"),
    (("1", "1", "", False, False), "smaller"),
    (("", "", "x", False, False), "Smoothing"),
    (("", "", "1", False, False), "between"),
    (("", "", "-0.1", False, False), "between"),
])
def test_parse_options_says_what_is_wrong(args, word):
    result = parse_options(*args)
    assert isinstance(result, str) and word in result


def test_options_describe_themselves_briefly():
    assert ChartOptions().describe() == ""
    assert ChartOptions(0, 1, 0.9, True, True).describe() == "y 0…1, smoothed 0.9, log x & y"
    assert ChartOptions(ymax=5).describe() == "y auto…5"


# ---- saved format ----------------------------------------------------------------------------
def test_options_survive_a_save_and_load_for_added_and_generated_charts():
    custom = Customization(
        added={"P": [ChartSpec("a", opts=ChartOptions(0, 2, 0.5, True, False))]},
        opts={"P": {"0:b": ChartOptions(smooth=0.8)}},
    )
    again = Customization.from_json(custom.to_json())
    assert again.added["P"][0].opts == ChartOptions(0, 2, 0.5, True, False)
    assert again.opts == {"P": {"0:b": ChartOptions(smooth=0.8)}}
    assert not again.is_empty


def test_old_files_without_options_still_load_and_bad_values_are_dropped():
    data = {"added": {"P": [{"y": "a", "x": "_step", "system": False}]}}
    assert Customization.from_json(data).added["P"][0].opts == ChartOptions()
    bad = {"opts": {"P": {"k": {"ymin": "x", "ymax": True, "smooth": 7, "logy": "yes"}, "j": "oops", "d": {}}, "Q": 3}}
    loaded = Customization.from_json(bad)
    assert loaded.opts == {"P": {"k": ChartOptions(smooth=0.99)}}  # junk -> defaults; smoothing clamped; default entries dropped
    assert ChartOptions.from_json({"ymin": 5, "ymax": 1}) == ChartOptions()  # an empty range is ignored


# ---- the layout ------------------------------------------------------------------------------
def layout():
    return ChartLayout([Panel("P", [ChartSpec("a"), ChartSpec("b")])])


def test_options_of_a_generated_chart_are_an_override_and_reset_when_back_to_default():
    lay = layout()
    lay.set_chart(0, 1, ChartSpec("b", opts=ChartOptions(smooth=0.9)))
    assert lay.panels[0].charts[1].opts == ChartOptions(smooth=0.9) and lay.panels[0].charts[0].opts == ChartOptions()
    assert lay.custom.opts == {"P": {chart_key(ChartSpec("b")): ChartOptions(smooth=0.9)}}
    lay.set_chart(0, 1, ChartSpec("b"))
    assert lay.custom.is_empty  # nothing left to remember


def test_options_and_x_axis_are_overridden_independently():
    lay = layout()
    lay.set_chart(0, 0, ChartSpec("a", x="_runtime", opts=ChartOptions(logy=True)))
    lay.set_chart(0, 0, ChartSpec("a", x="_step", opts=ChartOptions(logy=True)))
    assert lay.custom.x == {} and lay.custom.opts != {}


def test_options_of_an_added_chart_live_in_the_chart_and_deleting_cleans_up():
    lay = layout()
    index = lay.add_chart(0, ChartSpec("c"))
    lay.set_chart(0, index, ChartSpec("c", opts=ChartOptions(ymax=1)))
    assert lay.custom.added["P"][0].opts.ymax == 1
    lay.set_chart(0, 0, ChartSpec("a", opts=ChartOptions(smooth=0.5)))
    lay.remove_chart(0, 0)  # deleting the generated chart forgets its options
    assert lay.custom.opts == {}


def test_a_customization_applies_options_over_the_default_layout():
    custom = Customization(opts={"P": {chart_key(ChartSpec("a")): ChartOptions(logx=True)}})
    panels = apply_customization([Panel("P", [ChartSpec("a")])], custom)
    assert panels[0].charts[0].opts.logx


def test_editing_a_generated_charts_series_persists_in_edited_and_cleans_up():
    lay = ChartLayout([Panel("P", [ChartSpec("a"), ChartSpec("b")]), Panel("Tables", [ChartSpec("t1", kind=TABLE)])])
    edited_plot = ChartSpec("a", x="_runtime", more=("b",), opts=ChartOptions(smooth=0.4))
    lay.set_chart(0, 0, edited_plot)
    assert lay.panels[0].charts[0] == edited_plot
    assert lay.custom.edited["P"] == {"0:a": edited_plot} and lay.custom.x == {} and lay.custom.opts == {}
    # roundtrips through JSON
    loaded = Customization.from_json(lay.custom.to_json())
    assert apply_customization(lay.default, loaded)[0].charts[0] == edited_plot
    # reverting back to original metrics drops custom.edited
    lay.set_chart(0, 0, ChartSpec("a", x="_runtime"))
    assert lay.custom.edited == {} and lay.custom.x == {"P": {"0:a": "_runtime"}}
    # editing a generated table key works and removing cleans it up
    lay.set_chart(1, 0, ChartSpec("t2", kind=TABLE))
    assert lay.panels[1].charts[0] == ChartSpec("t2", kind=TABLE)
    lay.remove_chart(1, 0)
    assert lay.custom.edited == {} and lay.custom.removed == {"Tables": ["table:t1"]}


def test_malformed_edited_data_is_ignored():
    bad = {"edited": {"P": {"0:a": "bad", "0:b": {"y": 1}}, "Q": 5}}
    assert Customization.from_json(bad).edited == {}


# ---- in the screens ---------------------------------------------------------------------------
from textual.widgets import Checkbox, Input

from chart_helpers import settle
from conftest import FakeClient
from test_run_screens import open_charts
from wandb_cli_tool import config
from wandb_cli_tool.app import WandbCliApp
from wandb_cli_tool.config import Credentials
from wandb_cli_tool.layout_store import LayoutStore, project_key
from wandb_cli_tool.screens.chart_dialogs import ChartOptionsDialog
from wandb_cli_tool.screens.chart_widgets import ChartView


async def test_options_dialog_changes_the_focused_plot_and_is_shared_by_the_project(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 55)) as pilot:
        screen = await open_charts(pilot)
        assert screen.check_action("chart_options", ()) is False  # nothing focused
        await pilot.press("down", "down")  # a panel with plots ("trainer")
        await settle(pilot, 6)
        await pilot.press("right")
        await settle(pilot)
        view = app.focused
        assert isinstance(view, ChartView) and screen.check_action("chart_options", ()) is True
        await pilot.press("o")
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, ChartOptionsDialog)
        dialog.query_one("#ymin", Input).value = "abc"
        dialog._submit()
        await settle(pilot)
        assert isinstance(app.screen, ChartOptionsDialog)  # invalid: stays open
        assert "Y min" in str(dialog.query_one("#options-error").render())
        dialog.query_one("#ymin", Input).value = "0"
        dialog.query_one("#ymax", Input).value = "10"
        dialog.query_one("#smooth", Input).value = "0.5"
        dialog.query_one("#logx", Checkbox).value = False
        await pilot.click("#ok")
        await settle(pilot, 8)
        shown = app.focused
        assert isinstance(shown, ChartView) and shown.spec.opts == ChartOptions(0.0, 10.0, 0.5, False, False)
        assert "y 0…10" in str(shown.border_subtitle) and "smoothed 0.5" in str(shown.border_subtitle)
    saved = LayoutStore().load(project_key("me", "alpha"))
    assert saved.opts and next(iter(next(iter(saved.opts.values())).values())).smooth == 0.5


async def test_a_log_axis_says_how_many_points_it_could_not_show(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 55)) as pilot:
        screen = await open_charts(pilot)
        await pilot.press("down", "down")
        await settle(pilot, 6)
        await pilot.press("right")
        await settle(pilot)
        view = app.focused
        spec = ChartSpec(view.spec.y, opts=ChartOptions(logy=True))
        view.spec = spec
        view.show({view.spec.y: [(0.0, 0.0), (1.0, 2.0), (2.0, 4.0)]})
        assert "1 point not shown on a log axis" in str(view.border_subtitle) and "log y" in str(view.border_subtitle)


async def test_reset_button_goes_back_to_defaults(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 55)) as pilot:
        screen = await open_charts(pilot)
        await pilot.press("down", "down")
        await settle(pilot, 6)
        await pilot.press("right")
        await settle(pilot)
        index = app.focused.index
        screen.chart_layout.set_chart(screen.selected, index, replace_opts(app.focused.spec, ChartOptions(smooth=0.9)))
        await screen._show_panel(focus=index)
        await settle(pilot, 4)
        await pilot.press("o")
        await settle(pilot)
        await pilot.click("#reset")
        await settle(pilot, 8)
        assert app.focused.spec.opts == ChartOptions() and screen.chart_layout.custom.opts == {}


def replace_opts(spec, opts):
    from dataclasses import replace
    return replace(spec, opts=opts)


# ---- bar charts: a fixed value range ------------------------------------------------------------
from wandb_cli_tool.barchart import layout_bars
from wandb_cli_tool.charts import BAR


def bar_lengths(items, width, low=None, high=None):
    layout = layout_bars(items, width, low, high)
    return [(len(r.negative), len(r.positive.rstrip())) for r in layout.rows], layout


def test_a_fixed_range_scales_the_bars_to_it_and_clips_outside_values():
    items = [("a", 0.5), ("b", 1.0), ("c", 2.0), ("d", 0.0)]
    auto, _ = bar_lengths(items, 40)
    ranged, layout = bar_lengths(items, 40, 0.0, 1.0)
    assert ranged[1][1] == ranged[2][1] == layout.positive_width  # 1.0 fills the axis and 2.0 is clipped to it
    assert ranged[0][1] == round(layout.positive_width / 2) and ranged[3] == (0, 0)
    assert auto[2][1] > auto[1][1]  # without the range 2.0 is twice as long as 1.0


def test_a_range_that_does_not_include_zero_starts_the_bars_at_its_low_end():
    items = [("low", 0.9), ("mid", 0.95), ("top", 1.0), ("below", 0.5)]
    lengths, layout = bar_lengths(items, 40, 0.9, 1.0)
    assert layout.negative_width == 0
    assert lengths[0] == (0, 0)  # at the low end of the axis: no bar
    assert lengths[1][1] == round(layout.positive_width / 2) and lengths[2][1] == layout.positive_width
    assert lengths[3] == (0, 0)  # below the axis: clipped to nothing


def test_one_end_can_stay_automatic_and_an_empty_range_is_ignored():
    items = [("a", 3.0), ("b", 6.0)]
    only_high, _ = bar_lengths(items, 40, None, 12.0)
    auto, _ = bar_lengths(items, 40)
    assert only_high[1][1] < auto[1][1]  # headroom up to 12 makes the 6.0 bar shorter than the full-width one
    assert bar_lengths(items, 40, 5.0, 5.0)[0] == auto == bar_lengths(items, 40, 9.0, 1.0)[0]
    negative, _ = bar_lengths([("n", -2.0), ("p", 2.0)], 40, -4.0, 4.0)
    assert negative[0][0] == negative[1][1] > 0  # symmetric range around zero


def test_the_figure_is_still_the_real_value_when_the_bar_is_clipped():
    layout = layout_bars([("big", 7.5)], 30, 0.0, 1.0)
    assert layout.rows[0].figure.strip() == "7.5"


async def test_options_dialog_for_a_bar_chart_only_asks_for_the_axis(make_app):
    from chart_helpers import add_bar
    from wandb_cli_tool.screens.chart_widgets import BarView

    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 55)) as pilot:
        screen = await open_charts(pilot)
        await add_bar(pilot, (False, "acc"), (False, "trainer/loss"))
        view = app.focused
        assert isinstance(view, BarView) and screen.check_action("chart_options", ()) is True
        await pilot.press("o")
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, ChartOptionsDialog) and dialog.is_bar
        assert not dialog.query("#smooth") and not dialog.query("#logy")  # nothing that does not apply to bars
        dialog.query_one("#ymin", Input).value = "0"
        dialog.query_one("#ymax", Input).value = "1"
        await pilot.click("#ok")
        await settle(pilot, 8)
        shown = app.focused
        assert isinstance(shown, BarView) and shown.spec.opts == ChartOptions(0.0, 1.0)
        assert "axis 0…1" in str(shown.border_subtitle)
    saved = LayoutStore().load(project_key("me", "alpha"))
    assert any(s.kind == BAR and s.opts.ymax == 1.0 for specs in saved.added.values() for s in specs)


async def test_chart_options_edits_plot_metrics_x_bar_order_table_and_images(make_app):
    from textual.widgets import OptionList, Select, SelectionList
    from chart_helpers import add_bar
    from wandb_cli_tool.screens.chart_widgets import BarView, ImageChartView, TableView

    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 50)) as pilot:
        screen = await open_charts(pilot)
        # edit a generated plot's series, x-axis and smoothing via `o`
        await pilot.press("down", "right", "o")  # trainer/global_steps
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, ChartOptionsDialog)
        dialog.query_one("#choices", SelectionList).select((False, "val/loss"))
        dialog.query_one("#x", Select).value = "acc"
        dialog.query_one("#smooth", Input).value = "0.5"
        await pilot.click("#ok")
        await settle(pilot, 8)
        shown = app.focused
        assert isinstance(shown, ChartView)
        assert shown.spec.series == ("trainer/global_steps", "val/loss")
        assert shown.spec.x == "acc" and shown.spec.opts.smooth == 0.5

        # edit a bar chart's metrics & order via `o`
        await pilot.press("left")
        await add_bar(pilot, (False, "acc"), (False, "val/loss"))
        await pilot.press("o")
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, ChartOptionsDialog) and dialog.is_bar
        dialog.query_one("#choices", SelectionList).select((False, "trainer/loss"))
        await settle(pilot)
        order = dialog.query_one("#order", OptionList)
        order.focus()
        order.highlighted = 2
        await pilot.press("ctrl+up")  # move trainer/loss before val/loss
        await settle(pilot)
        await pilot.click("#ok")
        await settle(pilot, 8)
        bar = app.focused
        assert isinstance(bar, BarView)
        assert bar.spec.series == ("acc", "trainer/loss", "val/loss")

        # edit a table in the auto Tables panel
        await pilot.press("left")
        labels = [str(screen.query_one("#panels", OptionList).get_option_at_index(i).prompt)
                  for i in range(screen.query_one("#panels", OptionList).option_count)]
        tables_idx = next(i for i, l in enumerate(labels) if l.startswith("Tables ("))
        screen.selected = tables_idx
        await screen._show_panel()
        await settle(pilot, 6)
        screen.query(TableView).first().focus()
        await settle(pilot)
        await pilot.press("o")
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, ChartOptionsDialog) and dialog.kind == TABLE
        dialog.query_one("#table", Select).value = "results"
        await pilot.click("#ok")
        await settle(pilot, 6)
        assert isinstance(app.focused, TableView) and app.focused.spec.y == "results"

        # edit an image chart in the auto Images panel
        images_idx = next(i for i, l in enumerate(labels) if l.startswith("Images ("))
        screen.selected = images_idx
        await screen._show_panel()
        await settle(pilot, 6)
        screen.query(ImageChartView).first().focus()
        await settle(pilot)
        await pilot.press("o")
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, ChartOptionsDialog) and dialog.kind == IMAGE
        dialog.query_one("#image-key", Select).value = "samples"
        await pilot.click("#ok")
        await settle(pilot, 6)
        assert isinstance(app.focused, ImageChartView) and app.focused.spec.y == "samples"


async def test_editing_a_plot_shows_and_reorders_y_values_with_taller_lists(make_app):
    from textual.widgets import OptionList, SelectionList

    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 50)) as pilot:
        await open_charts(pilot)
        await pilot.press("down", "right", "o")  # trainer/global_steps
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, ChartOptionsDialog)
        assert dialog.query_one("#choices", SelectionList).styles.height.value == 10
        assert dialog.query_one("#order", OptionList).styles.height.value == 8
        order = dialog.query_one("#order", OptionList)
        assert [str(order.get_option_at_index(i).prompt) for i in range(order.option_count)] == [
            "1. trainer/global_steps",
        ]
        dialog.query_one("#choices", SelectionList).select((False, "acc"))
        await settle(pilot, 2)
        assert [str(order.get_option_at_index(i).prompt) for i in range(order.option_count)] == [
            "1. trainer/global_steps", "2. acc",
        ]
        order.focus()
        await pilot.press("ctrl+up")
        await settle(pilot, 2)
        assert [str(order.get_option_at_index(i).prompt) for i in range(order.option_count)] == [
            "1. acc", "2. trainer/global_steps",
        ]
        await pilot.click("#ok")
        await settle(pilot, 8)
        assert app.focused.spec.series == ("acc", "trainer/global_steps")


# ---- x limits and grid -------------------------------------------------------------------------


def test_x_limits_and_grid_are_parsed_saved_and_described():
    from wandb_cli_tool.charts import parse_options

    opts = parse_options("", "", "", False, False, "10", "100", True)
    assert (opts.xmin, opts.xmax, opts.grid) == (10.0, 100.0, True)
    assert opts.describe() == "x 10…100, grid" and ChartOptions.from_json(opts.to_json()) == opts
    assert parse_options("", "", "", False, False, "", "5").describe() == "x auto…5"
    assert "X min" in parse_options("", "", "", False, False, "5", "5")
    assert "X max" in parse_options("", "", "", False, False, "", "abc")
    assert ChartOptions.from_json({"xmin": 5, "xmax": 1}).xmin is None  # an impossible range is dropped
    assert ChartOptions.from_json({"ymin": 0}).grid is False  # files from before these options load


def test_the_axis_helper_applies_x_limits_and_the_grid():
    from wandb_cli_tool.screens.chart_widgets import apply_axis_options

    class Plt:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            return lambda *a: self.calls.append((name, a))

    plt = Plt()
    apply_axis_options(plt, ChartOptions(xmin=2.0, grid=True), [1.0, 2.0], [0.0, 10.0])
    assert ("xlim", (2.0, 10.0)) in plt.calls and ("grid", (True, True)) in plt.calls
    plt = Plt()
    apply_axis_options(plt, ChartOptions(), [1.0], [1.0])
    assert ("grid", (False, False)) in plt.calls and not any(c[0] == "xlim" for c in plt.calls)


async def test_the_options_dialog_sets_x_limits_and_grid(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_charts(pilot)
        await pilot.press("down", "down")
        await settle(pilot, 6)
        await pilot.press("right")
        await settle(pilot)
        await pilot.press("o")
        await settle(pilot)
        dialog = app.screen
        dialog.query_one("#xmin", Input).value = "1"
        dialog.query_one("#xmax", Input).value = "3"
        dialog.query_one("#grid", Checkbox).value = True
        await pilot.click("#ok")  # reachable even in a short terminal: only the fields scroll
        await settle(pilot, 8)
        shown = app.focused
        assert isinstance(shown, ChartView) and (shown.spec.opts.xmin, shown.spec.opts.xmax, shown.spec.opts.grid) == (1.0, 3.0, True)
        assert "x 1…3, grid" in str(shown.border_subtitle)
