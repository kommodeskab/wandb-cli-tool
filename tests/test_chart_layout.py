import json
import os
import stat

import pytest

from wandb_cli_tool import charts as charts_mod
from wandb_cli_tool import layout_store as store_mod
from wandb_cli_tool.charts import (
    ChartLayout, ChartSpec, Customization, Panel, apply_customization, build_panels, chart_key,
)
from wandb_cli_tool.layout_store import LayoutStore, project_key


def default():
    return build_panels(["acc", "trainer/loss", "trainer/steps"], ["system.cpu"])


# ---- applying customizations ---------------------------------------------------------


def test_no_customization_is_exactly_the_default():
    d = default()
    assert [(p.name, p.charts) for p in apply_customization(d, Customization())] == [(p.name, p.charts) for p in d]


def test_customizations_are_added_on_top_without_touching_the_default():
    d = default()
    before = [(p.name, list(p.charts)) for p in d]
    custom = Customization(
        panels=["mine"],
        added={"mine": [ChartSpec("acc", "trainer/steps")], "trainer": [ChartSpec("acc")]},
        x={"trainer": {chart_key(ChartSpec("trainer/loss")): "trainer/steps"}},
    )
    panels = {p.name: p for p in apply_customization(d, custom)}
    assert list(panels) == ["System", "trainer", "Other", "mine"]  # user panels come last
    assert [(c.y, c.x) for c in panels["trainer"].charts] == [
        ("trainer/loss", "trainer/steps"),  # override applied by chart key
        ("trainer/steps", "_step"),
        ("acc", "_step"),  # added charts go after the generated ones
    ]
    assert panels["mine"].charts == [ChartSpec("acc", "trainer/steps")]
    assert [(p.name, p.charts) for p in d] == [(n, c) for n, c in before]  # default untouched


def test_charts_for_a_panel_that_no_longer_exists_are_kept_in_a_panel_of_that_name():
    panels = apply_customization(default(), Customization(added={"gone": [ChartSpec("acc")]}))
    assert panels[-1].name == "gone" and panels[-1].charts == [ChartSpec("acc")]


def test_overrides_follow_the_chart_not_its_position_so_new_metrics_do_not_shift_them():
    custom = Customization(x={"trainer": {chart_key(ChartSpec("trainer/loss")): "acc"}})
    later = build_panels(["trainer/aaa_new", "trainer/loss", "trainer/steps"], [])  # sorts before loss
    trainer = next(p for p in apply_customization(later, custom) if p.name == "trainer")
    assert [(c.y, c.x) for c in trainer.charts] == [("trainer/aaa_new", "_step"), ("trainer/loss", "acc"), ("trainer/steps", "_step")]


# ---- the edit operations -------------------------------------------------------------


def test_add_panel_is_idempotent_and_does_not_record_existing_generated_panels():
    layout = ChartLayout(default())
    index = layout.add_panel("mine")
    assert layout.panels[index].name == "mine" and layout.custom.panels == ["mine"]
    assert layout.add_panel("mine") == index and layout.custom.panels == ["mine"]
    assert layout.add_panel("trainer") == 1 and layout.custom.panels == ["mine"]  # already generated


def test_add_chart_returns_its_index_and_is_recorded():
    layout = ChartLayout(default())
    trainer = layout.index_of("trainer")
    assert layout.add_chart(trainer, ChartSpec("acc")) == 2
    assert layout.custom.added == {"trainer": [ChartSpec("acc")]} and layout.is_customized


def test_changing_the_x_axis_of_a_generated_chart_records_an_override_and_undoing_it_forgets_it():
    layout = ChartLayout(default())
    trainer = layout.index_of("trainer")
    layout.set_chart(trainer, 1, ChartSpec("trainer/steps", "acc"))
    assert layout.panels[trainer].charts[1].x == "acc"
    assert layout.custom.x == {"trainer": {"0:trainer/steps": "acc"}}
    layout.set_chart(trainer, 1, ChartSpec("trainer/steps", "_step"))  # back to the default x
    assert layout.custom.x == {} and not layout.is_customized


def test_changing_the_x_axis_of_an_added_chart_edits_it_in_place():
    layout = ChartLayout(default())
    mine = layout.add_panel("mine")
    layout.add_chart(mine, ChartSpec("acc"))
    layout.add_chart(mine, ChartSpec("trainer/loss"))
    layout.set_chart(mine, 1, ChartSpec("trainer/loss", "acc"))
    assert layout.custom.added["mine"] == [ChartSpec("acc"), ChartSpec("trainer/loss", "acc")]
    assert layout.custom.x == {}  # added charts are not stored as overrides
    # an added chart in a generated panel is edited in place too
    trainer = layout.index_of("trainer")
    layout.add_chart(trainer, ChartSpec("acc"))
    layout.set_chart(trainer, 2, ChartSpec("acc", "trainer/steps"))
    assert layout.custom.added["trainer"] == [ChartSpec("acc", "trainer/steps")]


def test_reset_returns_to_the_default():
    layout = ChartLayout(default())
    layout.add_chart(layout.add_panel("mine"), ChartSpec("acc"))
    layout.set_chart(1, 0, ChartSpec("trainer/loss", "acc"))
    layout.reset()
    assert not layout.is_customized
    assert [(p.name, p.charts) for p in layout.panels] == [(p.name, p.charts) for p in default()]


# ---- (de)serialising -----------------------------------------------------------------


def test_json_roundtrip():
    custom = Customization(panels=["a"], added={"a": [ChartSpec("m", "x", True)]}, x={"p": {"0:m": "x"}})
    assert Customization.from_json(json.loads(json.dumps(custom.to_json()))) == custom


@pytest.mark.parametrize("garbage", [None, 5, "x", [], {"panels": 3, "added": [], "x": "no"},
                                      {"panels": [1, None, ""], "added": {"p": "no", 3: []}, "x": {"p": [1]}}])
def test_malformed_data_becomes_an_empty_customization_instead_of_raising(garbage):
    assert Customization.from_json(garbage).is_empty


def test_only_the_valid_parts_of_a_damaged_record_survive():
    data = {
        "panels": ["ok", 7],
        "added": {"ok": [{"y": "m", "x": "_step", "system": False}, {"y": 1}, "junk", {"y": "m", "x": "_step", "system": "yes"}]},
        "x": {"p": {"0:m": "acc", "1:m": 9}},
    }
    c = Customization.from_json(data)
    assert c.panels == ["ok"] and c.added == {"ok": [ChartSpec("m")]} and c.x == {"p": {"0:m": "acc"}}


def test_absurdly_large_files_are_truncated(monkeypatch):
    monkeypatch.setattr(charts_mod, "MAX_PANELS", 3)
    monkeypatch.setattr(charts_mod, "MAX_CHARTS", 2)
    spec = {"y": "m", "x": "_step", "system": False}
    c = Customization.from_json({"panels": list("abcdef"), "added": {"p": [spec] * 5, "q": [spec] * 5}})
    assert c.panels == ["a", "b", "c"] and sum(len(v) for v in c.added.values()) == 2


# ---- the file store ------------------------------------------------------------------


def sample():
    return Customization(panels=["mine"], added={"mine": [ChartSpec("acc")]})


def test_store_roundtrip_and_per_project_isolation(tmp_path):
    store = LayoutStore(tmp_path / "layouts.json")
    a, b = project_key("e", "p1"), project_key("e", "p2")
    assert store.save(a, sample())
    assert store.load(a) == sample() and store.load(b).is_empty
    store.save(b, Customization(panels=["other"]))
    assert store.load(a) == sample() and store.load(b).panels == ["other"]


def test_nothing_customized_creates_no_file_and_forgetting_keeps_other_runs(tmp_path):
    store = LayoutStore(tmp_path / "layouts.json")
    assert store.save("k", Customization()) and not store.path.exists()
    store.save("a", sample())
    store.save("b", sample())
    store.reset("a")
    assert store.load("a").is_empty and store.load("b") == sample()


def test_missing_corrupt_and_foreign_version_files_are_ignored_then_replaced(tmp_path):
    store = LayoutStore(tmp_path / "layouts.json")
    assert store.load("k").is_empty  # missing
    store.path.write_text("{not json")
    assert store.load("k").is_empty  # corrupt
    store.path.write_text(json.dumps({"version": 999, "runs": {"k": sample().to_json()}}))
    assert store.load("k").is_empty  # older/newer format: development-time reset
    assert store.save("k", sample()) and store.load("k") == sample()
    assert json.loads(store.path.read_text())["version"] == charts_mod.LAYOUT_VERSION


def test_store_keeps_only_the_newest_layouts(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "MAX_PROJECTS", 3)
    store = LayoutStore(tmp_path / "layouts.json")
    for i in range(6):
        store.save(f"run{i}", sample())
        data = json.loads(store.path.read_text())
        data["projects"][f"run{i}"]["saved_at"] = 1000 + i
        store.path.write_text(json.dumps(data))
    store.save("run6", sample())
    assert sorted(json.loads(store.path.read_text())["projects"]) == ["run4", "run5", "run6"]


def test_store_file_is_private_atomic_and_never_raises(tmp_path):
    store = LayoutStore(tmp_path / "sub" / "layouts.json")
    assert store.save("k", sample())
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert not list(tmp_path.rglob("*.tmp"))
    blocked = LayoutStore(tmp_path / "file" / "layouts.json")
    (tmp_path / "file").write_text("i am a file, not a directory")
    assert blocked.save("k", sample()) is False  # reported, not raised
    assert blocked.load("k").is_empty


def test_default_location_follows_the_config_directory(tmp_path, monkeypatch):
    from wandb_cli_tool import config

    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(tmp_path / "cfg"))
    assert LayoutStore().path == tmp_path / "cfg" / "chart_layouts.json"
