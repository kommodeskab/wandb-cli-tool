"""The UI-free parts of comparing runs: labels, facts, config differences, chart reduction."""

from wandb_cli_tool.api import RunInfo, RunMetadata
from wandb_cli_tool.charts import BAR, PLOT, ChartSpec, Panel
from wandb_cli_tool.compare import (
    compare_charts, diff_config, flatten_config, is_single_value, overview_rows, run_labels,
)
from wandb_cli_tool.screens.compare_widgets import grouped_bars_height, render_grouped_bars


def test_labels_add_the_id_only_where_names_collide():
    assert run_labels(["a", "b", "a"], ["1", "2", "3"]) == ["a (1)", "b", "a (3)"]
    assert run_labels(["a", "b"], ["1", "2"]) == ["a", "b"]


def test_flatten_config_makes_one_entry_per_leaf():
    flat = flatten_config({"lr": 0.1, "model": {"name": "resnet", "layers": [64, 128]}, "empty": {}, "none": None})
    assert flat == {"lr": "0.1", "model.name": "resnet", "model.layers[0]": "64", "model.layers[1]": "128",
                    "empty": "{}", "none": "null"}


def test_flatten_config_keeps_long_lists_as_one_value_and_clips_text():
    flat = flatten_config({"big": list(range(100)), "text": "x" * 500})
    assert list(flat) == ["big", "text"]
    assert len(flat["text"]) <= 60 and flat["text"].endswith("…")


def test_diff_shows_only_what_differs():
    diff = diff_config([{"lr": 0.1, "seed": 1, "a": {"b": 2}}, {"lr": 0.2, "seed": 1, "a": {"b": 2}}])
    assert [(r.path, r.values, r.only_some) for r in diff.rows] == [("lr", ("0.1", "0.2"), False)]
    assert diff.same == 2 and diff.different == 1 and diff.partial == 0


def test_diff_marks_entries_only_some_runs_have():
    diff = diff_config([{"lr": 0.1, "dropout": 0.5}, {"lr": 0.1}, {"lr": 0.1, "dropout": 0.5}])
    [row] = diff.rows
    assert (row.path, row.values, row.only_some) == ("dropout", ("0.5", None, "0.5"), True)
    assert diff.partial == 1 and diff.same == 1


def test_diff_can_list_the_identical_entries_too():
    diff = diff_config([{"a": 1, "b": 1}, {"a": 1, "b": 2}], show_all=True)
    assert [r.path for r in diff.rows] == ["a", "b"] and diff.same == 1


def test_a_run_without_a_config_counts_as_lacking_every_entry():
    diff = diff_config([{"a": 1}, None])
    assert diff.rows[0].values == ("1", None)


def test_overview_marks_the_facts_that_differ():
    infos = [RunInfo("i1", "a", "running", "", "2024-05-01T10:00:00", "g1", ("x", "y")),
             RunInfo("i2", "b", "finished", "2024-05-01T11:00:00", "2024-05-01T10:00:00", "g1", ())]
    rows = {r.label: r for r in overview_rows(infos, [RunMetadata(host="h"), RunMetadata(host="h")])}
    assert rows["Status"].differs and rows["Status"].values == ("● running", "finished")
    assert rows["Group"].values == ("g1", "g1") and not rows["Group"].differs
    assert rows["Tags"].values == ("x, y", "—") and rows["Tags"].differs
    assert rows["Host"].values == ("h", "h") and not rows["Host"].differs
    assert rows["ID"].values == ("i1", "i2")
    assert list(rows)[:5] == ["Status", "Started", "Run time", "Group", "Tags"]


def test_overview_says_loading_for_runs_not_loaded_yet():
    rows = {r.label: r for r in overview_rows([None], [None])}
    assert rows["Status"].values == ("…",) and rows["ID"].values == ("…",)


def test_multi_metric_plots_are_split_into_one_chart_per_metric():
    panel = Panel("p", [ChartSpec("a/x", more=("a/y", "a/z")), ChartSpec("a/q", kind=BAR, more=("a/r",)),
                        ChartSpec("t", kind="table"), ChartSpec("i", kind="image"), ChartSpec("a/solo")])
    charts = compare_charts(panel)
    assert [(c.kind, c.metrics) for c in charts] == [
        (PLOT, ("a/x",)), (PLOT, ("a/y",)), (PLOT, ("a/z",)), (BAR, ("a/q", "a/r")),
        (TABLE, ("t",)), (IMAGE, ("i",)), (PLOT, ("a/solo",)),
    ]
    assert charts[0].subtitle == ChartSpec("a/x", more=("a/y", "a/z")).title and charts[-1].subtitle == ""


def test_single_value_means_every_run_with_data_has_exactly_one_point():
    assert is_single_value([[(3.0, 1.0)], [(5.0, 2.0)], []])
    assert not is_single_value([[(3.0, 1.0)], [(1.0, 1.0), (2.0, 2.0)]])
    assert not is_single_value([[], []])


def test_grouped_bars_give_each_run_its_own_color_and_separate_groups():
    groups = [("loss", [1.0, 2.0]), ("acc", [0.5, None])]
    text = render_grouped_bars(groups, ["#111111", "#222222"], 60)
    lines = text.plain.split("\n")
    assert len(lines) == 5 and lines[2] == ""  # two bars, a blank line, two bars
    assert lines[0].startswith("loss") and lines[1].startswith(" ") and lines[3].startswith("acc")
    assert lines[4].rstrip().endswith("—")  # a run without a value: a dash, no bar
    styles = {str(span.style) for span in text.spans}
    assert {"#111111", "#222222"} <= styles
    assert grouped_bars_height(groups) == 4 + 1 + 2


# ---- metrics, tables, images ------------------------------------------------------------------
from wandb_cli_tool.charts import IMAGE, TABLE
from wandb_cli_tool.compare import merge_tables, metric_rows, nearest_step, union_steps
from wandb_cli_tool.images import ImageSeries, ImageStep
from wandb_cli_tool.tables import TableData


def test_lower_is_better_unless_the_user_says_a_metric_is_higher():
    values = [{"acc": 0.9, "loss": 0.5}, {"acc": 0.8, "loss": 0.2}]
    plain = {r.name: r for r in metric_rows(values)}
    assert plain["acc"].best == 1 and plain["loss"].best == 1 and not plain["acc"].higher  # no guessing by name
    flipped = {r.name: r for r in metric_rows(values, higher={"acc"})}
    assert flipped["acc"].best == 0 and flipped["acc"].higher and flipped["loss"].best == 1 and not flipped["loss"].higher


def test_metric_rows_mark_the_best_run_and_whether_the_runs_differ():
    rows = {r.name: r for r in metric_rows([{"acc": 0.9, "loss": 0.5, "same": 1.0, "solo": 2.0},
                                             {"acc": 0.8, "loss": 0.2, "same": 1.0}], higher={"acc"})}
    assert rows["acc"].best == 0 and rows["loss"].best == 1  # high acc (flipped), low loss (default)
    assert rows["same"].best is None and not rows["same"].differs  # nothing to choose between
    assert rows["solo"].best is None and rows["solo"].differs and rows["solo"].values == (2.0, None)
    assert list(rows) == sorted(rows)


def table(columns, *rows, total=None):
    return TableData(tuple(columns), tuple(tuple(r) for r in rows), total or len(rows), len(columns))


def test_merge_tables_unites_columns_and_keeps_each_runs_rows_together():
    merged = merge_tables([table("ab", ("1", "x"), ("2", "y")), table("bc", ("z", "w")), None])
    assert merged.columns == ("a", "b", "c") and merged.missing == (2,) and not merged.truncated
    assert merged.rows == ((0, ("1", "x", "")), (0, ("2", "y", "")), (1, ("", "z", "w")))


def test_merge_tables_can_line_up_rows_by_their_first_column():
    merged = merge_tables([table("ka", ("b", "1"), ("a", "2")), table("ka", ("a", "3"), ("b", "4"))], by_key=True)
    assert [(i, r[0]) for i, r in merged.rows] == [(0, "a"), (1, "a"), (0, "b"), (1, "b")]


def test_merge_tables_limits_rows_per_run_and_says_so():
    big = table("a", *[(str(i),) for i in range(50)])
    merged = merge_tables([big, big], per_run=10)
    assert len(merged.rows) == 20 and merged.truncated
    assert merge_tables([table("a", ("1",), total=5)]).truncated  # the table itself was cut when downloaded


def steps(*numbers):
    return ImageSeries(tuple(ImageStep(n, (f"p{n}.png",)) for n in numbers), len(numbers))


def test_the_slider_covers_every_step_any_run_has_and_runs_show_their_nearest():
    assert union_steps([steps(10, 20), None, steps(20, 25)]) == [10, 20, 25] and union_steps([None]) == []
    s = steps(10, 20, 30).steps
    assert nearest_step(s, 24).step == 20 and nearest_step(s, 26).step == 30
    assert nearest_step(s, 15).step == 10  # a tie goes to the earlier step
    assert nearest_step(s, 99).step == 30 and nearest_step((), 5) is None


def test_tables_and_images_of_a_layout_are_compared_too():
    panel = Panel("p", [ChartSpec("preds", kind=TABLE), ChartSpec("grid", kind=IMAGE)])
    assert [(c.kind, c.metrics) for c in compare_charts(panel)] == [(TABLE, ("preds",)), (IMAGE, ("grid",))]
