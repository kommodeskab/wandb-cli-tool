import math

from wandb_cli_tool.charts import (
    OTHER_PANEL, RUNTIME, STEP, SYSTEM_PANEL, ChartSpec, build_panels, panel_name, x_options,
)
from wandb_cli_tool.series import extract_points, plottable_keys


def test_panel_name_uses_prefix_before_first_slash():
    assert panel_name("trainer/global_steps") == "trainer"
    assert panel_name("a/b/c") == "a"
    assert panel_name("acc") == OTHER_PANEL


def test_chart_title_drops_panel_prefix():
    assert ChartSpec("trainer/global_steps").title == "global_steps"
    assert ChartSpec("a/b/c").title == "b/c"
    assert ChartSpec("system.cpu", system=True).title == "system.cpu"


def test_build_panels_groups_and_orders():
    panels = build_panels(["val/loss", "acc", "trainer/loss", "trainer/acc"], ["system.cpu"])
    assert [p.name for p in panels] == [SYSTEM_PANEL, "trainer", "val", OTHER_PANEL]
    assert [c.y for c in panels[2 - 1].charts] == ["trainer/loss", "trainer/acc"]
    assert all(c.x == STEP for p in panels[1:] for c in p.charts)
    assert panels[0].charts == [ChartSpec("system.cpu", RUNTIME, system=True)]


def test_system_panel_always_present_and_user_system_prefix_merges():
    assert [p.name for p in build_panels([], [])] == [SYSTEM_PANEL]
    panels = build_panels(["System/foo"], ["system.cpu"])
    assert [p.name for p in panels] == [SYSTEM_PANEL]
    assert [c.y for c in panels[0].charts] == ["system.cpu", "System/foo"]


def test_x_options_system_vs_metric():
    system = [v for _, v in x_options(["m"], system=True)]
    metric = [v for _, v in x_options(["m"], system=False)]
    assert STEP not in system and system[0] == RUNTIME
    assert metric[0] == STEP and "m" in metric


def test_extract_points_skips_missing_nan_and_bools():
    rows = [
        {"_step": 0, "y": 1.0},
        {"_step": 1},
        {"_step": 2, "y": math.nan},
        {"_step": 3, "y": True},
        {"_step": 4, "y": "s"},
        {"_step": 5, "y": 2},
    ]
    assert extract_points(rows, "_step", "y") == [(0.0, 1.0), (5.0, 2.0)]


def test_plottable_keys_filters_non_numeric_and_private():
    values = {"b": 1, "a": 2.5, "_step": 3, "img": {"_type": "image"}, "s": "x", "t": True}
    assert plottable_keys(values) == ["a", "b"]
