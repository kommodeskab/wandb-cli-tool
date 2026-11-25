import json
import math

import pytest

from wandb_cli_tool import tables as tables_mod
from wandb_cli_tool.barchart import PARTIAL, clip, format_figure, layout_bars
from wandb_cli_tool.charts import (
    BAR, MAX_BAR_VALUES, PLOT, TABLE, ChartSpec, Customization, display_names,
)
from wandb_cli_tool.palette import MAX_SERIES, SERIES_DARK, SERIES_LIGHT, hex_to_rgb, series_color
from wandb_cli_tool.series import last_values, numeric_values
from wandb_cli_tool.tables import format_cell, parse_table, table_entries


# ---- palette: the skill's reference colors, fixed order, never cycled -----------------


def test_series_colors_follow_the_reference_order_for_dark_and_light():
    assert [series_color(i) for i in range(MAX_SERIES)] == list(SERIES_DARK)
    assert [series_color(i, dark=False) for i in range(MAX_SERIES)] == list(SERIES_LIGHT)
    assert SERIES_DARK[:3] == ("#338ced", "#e76624", "#00a888")  # blue, orange, aqua
    assert MAX_SERIES == 8 and len(set(SERIES_DARK)) == 8 and len(set(SERIES_LIGHT)) == 8


def test_a_ninth_series_is_refused_instead_of_cycling_or_inventing_a_hue():
    for bad in (8, 9, -1):
        with pytest.raises(ValueError, match="at most 8"):
            series_color(bad)


def test_hex_to_rgb():
    assert hex_to_rgb("#338ced") == (0x33, 0x8C, 0xED) and hex_to_rgb("000000") == (0, 0, 0)


# ---- ChartSpec ------------------------------------------------------------------------


def test_series_and_titles_for_each_kind():
    plot = ChartSpec("trainer/loss", more=("trainer/acc",))
    assert plot.series == ("trainer/loss", "trainer/acc") and plot.title == "loss, acc"
    assert ChartSpec("system.cpu", system=True).title == "system.cpu"
    assert ChartSpec("a/b", kind=BAR, more=("c",)).title == "Bar: b, c"
    assert ChartSpec("preds", kind=TABLE).title == "Table: preds"
    assert ChartSpec("trainer/loss").title == "loss"  # unchanged for the common single-value plot


def test_colliding_short_names_fall_back_to_full_names():
    assert display_names(["trainer/loss", "val/loss"]) == ["trainer/loss", "val/loss"]
    assert display_names(["trainer/loss", "val/acc"]) == ["loss", "acc"]
    assert display_names(["system.cpu", "system.mem"], system=True) == ["system.cpu", "system.mem"]


# ---- saved layouts: new fields round-trip, old files still load -------------------------


def test_every_kind_survives_a_json_roundtrip():
    custom = Customization(added={"p": [
        ChartSpec("a", "_runtime", True, PLOT, ("b",)),
        ChartSpec("m", kind=BAR, more=("n", "o")),
        ChartSpec("preds", kind=TABLE),
    ]})
    assert Customization.from_json(json.loads(json.dumps(custom.to_json()))) == custom


def test_layouts_saved_before_chart_kinds_existed_load_as_plain_plots():
    old = {"added": {"p": [{"y": "m", "x": "_step", "system": False}]}}  # no kind, no more
    assert Customization.from_json(old).added == {"p": [ChartSpec("m")]}


@pytest.mark.parametrize("bad", [
    {"y": "m", "x": "_step", "system": False, "kind": "pie"},
    {"y": "m", "x": "_step", "system": False, "kind": "plot", "more": "nope"},
    {"y": "m", "x": "_step", "system": False, "kind": "plot", "more": [1]},
    {"y": "m", "x": "_step", "system": False, "kind": "plot", "more": ["s"] * MAX_SERIES},  # 9 series
    {"y": "m", "x": "_step", "system": False, "kind": "table", "more": ["extra"]},
    {"y": "m", "x": "_step", "system": False, "kind": "bar", "more": ["s"] * MAX_BAR_VALUES},
])
def test_malformed_or_oversized_saved_charts_are_dropped(bad):
    assert Customization.from_json({"added": {"p": [bad]}}).is_empty


# ---- numbers from the summary / system rows ---------------------------------------------


def test_numeric_values_and_last_values():
    assert numeric_values({"a": 1, "b": 2.5, "_step": 3, "img": {"_type": "image"}, "s": "x", "t": True, "n": math.nan}) == {"a": 1.0, "b": 2.5}
    rows = [{"_runtime": 1, "cpu": 10, "mem": 5}, {"_runtime": 2, "cpu": 20}, {"_runtime": 3, "cpu": None, "gpu": 7}]
    assert last_values(rows) == {"cpu": 20.0, "mem": 5.0, "gpu": 7.0}


# ---- wandb.Table parsing ----------------------------------------------------------------


def entry(path, **kw):
    return {"_type": "table-file", "path": path, **kw}


def test_table_entries_finds_only_logged_tables_sorted_by_key():
    summary = {
        "zeta": entry("media/table/zeta_1_aa.table.json", nrows=4, ncols=2),
        "alpha": entry("media/table/alpha_9_bb.table.json"),
        "img": {"_type": "image-file", "path": "media/images/x.png"},
        "loss": 0.5,
        "odd": entry("media/table/odd.json"),  # not a .table.json file
        "nopath": {"_type": "table-file"},
        "bool_rows": entry("media/table/b_1_cc.table.json", nrows=True),
    }
    found = table_entries(summary)
    assert [t.key for t in found] == ["alpha", "bool_rows", "zeta"]
    assert (found[2].rows, found[2].cols) == (4, 2) and found[0].rows is None and found[1].rows is None
    assert found[2].label == "zeta  (4 rows × 2 cols)" and found[0].label == "alpha  (size unknown)"


@pytest.mark.parametrize("value, shown", [
    (None, ""), (True, "True"), (7, "7"), (0.123456789, "0.123457"), (float("nan"), "NaN"),
    ("two\nlines   here", "two lines here"),
    ({"_type": "image-file", "path": "media/images/cat_3.png"}, "[image] cat_3.png"),
    ({"_type": "audio-file"}, "[audio]"),
    ({"_type": "table-file", "path": "t.table.json"}, "[table] t.table.json"),
    ([1, 2, {"a": None}], '[1,2,{"a":null}]'),
    ({"plain": "dict"}, '{"plain":"dict"}'),
    (object, None),  # an unserialisable object still gives text, never an exception
])
def test_format_cell(value, shown):
    out = format_cell(value)
    assert isinstance(out, str)
    if shown is not None:
        assert out == shown


def test_long_cells_are_clipped():
    out = format_cell("x" * 500)
    assert len(out) == tables_mod.MAX_CELL_CHARS and out.endswith("…")


def test_parse_table_formats_rows_and_pads_short_ones():
    data = parse_table({"columns": ["id", "name", 3], "data": [[1, "a", None], [2, "b"], 5]})
    assert data.columns == ("id", "name", "3")
    assert data.rows == (("1", "a", ""), ("2", "b", ""), ("5", "", ""))
    assert (data.total_rows, data.total_cols) == (3, 3)


@pytest.mark.parametrize("raw", [None, [], {"columns": "x", "data": []}, {"columns": [], "data": "x"}, {"data": []}, "text"])
def test_parse_table_rejects_files_that_are_not_tables(raw):
    with pytest.raises(ValueError, match="not a wandb.Table"):
        parse_table(raw)


def test_huge_tables_are_cut_but_report_their_real_size(monkeypatch):
    monkeypatch.setattr(tables_mod, "MAX_TABLE_ROWS", 3)
    monkeypatch.setattr(tables_mod, "MAX_TABLE_COLS", 2)
    data = parse_table({"columns": list("abcde"), "data": [[i] * 5 for i in range(10)]})
    assert len(data.rows) == 3 and data.columns == ("a", "b") and all(len(r) == 2 for r in data.rows)
    assert (data.total_rows, data.total_cols) == (10, 5)


# ---- bar chart layout ---------------------------------------------------------------------


def bar(row, layout):
    return row.negative.rjust(layout.negative_width) + ("|" if layout.negative_width else "") + row.positive.ljust(layout.positive_width)


def test_bars_keep_the_given_order_and_are_proportional():
    layout = layout_bars([("small", 1.0), ("big", 4.0), ("half", 2.0)], 40)
    assert [r.name.strip() for r in layout.rows] == ["small", "big", "half"]  # the user's order, not sorted
    cells = [len(r.positive.replace(PARTIAL.strip(), "")) for r in layout.rows]
    assert layout.negative_width == 0 and layout.positive_width > 0
    assert cells[1] == layout.positive_width  # the largest value fills the bar area
    assert abs(cells[0] * 4 - cells[1]) <= 4 and abs(cells[2] * 2 - cells[1]) <= 2


def test_every_row_has_the_same_width_so_bars_and_figures_line_up():
    layout = layout_bars([("a", 0.25), ("a long metric name", 3.0), ("c", -1.5), ("d", None)], 50)
    widths = {len(r.name) + 2 + len(bar(r, layout).replace("|", " ")) + 2 + len(r.figure) for r in layout.rows}
    assert len(widths) == 1 and widths.pop() <= 50


def test_negative_values_grow_left_of_a_shared_baseline():
    layout = layout_bars([("up", 2.0), ("down", -2.0), ("zero", 0.0)], 40)
    up, down, zero = layout.rows
    assert layout.negative_width > 0 and up.positive and not up.negative
    assert down.negative and not down.positive and not zero.positive and not zero.negative
    assert len(down.negative) == layout.negative_width  # equal magnitude, equal length


def test_missing_zero_and_all_zero_values_draw_no_bar_and_never_crash():
    layout = layout_bars([("a", None), ("b", float("nan")), ("c", 0.0)], 30)
    assert [r.figure.strip() for r in layout.rows] == ["—", "—", "0"]
    assert all(not r.positive and not r.negative for r in layout.rows)
    assert layout_bars([], 30).rows == []


def test_a_tiny_nonzero_value_is_still_visible():
    layout = layout_bars([("tiny", 0.0001), ("huge", 1000.0)], 40)
    assert layout.rows[0].positive != ""


def test_long_names_are_clipped_and_very_narrow_widths_still_work():
    layout = layout_bars([("x" * 80, 1.0), ("y", 2.0)], 24)
    assert layout.rows[0].name.endswith("…") and len(layout.rows[0].name) == len(layout.rows[1].name) <= 24
    layout_bars([("a", 1.0), ("b", -1.0)], 1)  # absurdly narrow: no exception
    assert clip("abcdef", 4) == "abc…" and clip("ab", 4) == "ab"
    assert format_figure(0.1234567) == "0.123457" and format_figure(None) == "—"
