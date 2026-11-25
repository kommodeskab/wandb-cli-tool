"""y-limits, smoothing and log axes: the model, the saved format, the plots, the dialog."""

import math

import pytest

from wandb_cli_tool.charts import (
    ChartLayout, ChartOptions, ChartSpec, Customization, Panel, apply_customization, chart_key, parse_options,
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
    async with app.run_test(size=(130, 45)) as pilot:
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
    async with app.run_test(size=(130, 45)) as pilot:
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
    async with app.run_test(size=(130, 45)) as pilot:
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
