import json

import pytest

from chart_helpers import add_bar, add_plot, add_table, new_panel, settle
from conftest import FakeClient
from test_run_screens import open_charts, panel_labels
from wandb_cli_tool import config
from wandb_cli_tool.charts import (
    ChartLayout, ChartSpec, Customization, TABLE, apply_customization, build_panels,
)
from wandb_cli_tool.config import Credentials
from wandb_cli_tool.layout_store import LayoutStore
from wandb_cli_tool.screens.chart_widgets import BarView, ChartView, TableView
from wandb_cli_tool.screens.dialogs import ConfirmDialog

SIZE = (130, 50)


def default():
    return build_panels(["a/x", "a/y", "a/z", "acc"], ["system.cpu"])


def ys(layout, name):
    return [c.y for c in layout.panels[layout.index_of(name)].charts]


# ---- the layout model ---------------------------------------------------------------------


def test_deleting_a_generated_chart_hides_it_and_is_remembered_by_key():
    layout = ChartLayout(default())
    a = layout.index_of("a")
    layout.remove_chart(a, 1)
    assert ys(layout, "a") == ["a/x", "a/z"] and layout.custom.removed == {"a": ["0:a/y"]}
    assert layout.is_customized
    # the defaults themselves are untouched, so a fresh layout with the same record still hides it
    assert [c.y for c in apply_customization(default(), layout.custom)[a].charts] == ["a/x", "a/z"]
    assert [c.y for c in default()[a].charts] == ["a/x", "a/y", "a/z"]


def test_indexes_stay_correct_after_a_deletion_so_later_edits_hit_the_right_chart():
    layout = ChartLayout(default())
    a = layout.index_of("a")
    layout.add_chart(a, ChartSpec("acc"))  # a/x, a/y, a/z, acc(added)
    layout.remove_chart(a, 0)  # a/y, a/z, acc
    layout.set_chart(a, 0, ChartSpec("a/y", "acc"))  # index 0 is now a/y
    assert layout.custom.x == {"a": {"0:a/y": "acc"}} and ys(layout, "a") == ["a/y", "a/z", "acc"]
    layout.set_chart(a, 2, ChartSpec("acc", "a/x"))  # the added chart, now at index 2
    assert layout.custom.added["a"] == [ChartSpec("acc", "a/x")]
    layout.remove_chart(a, 2)  # delete the added chart
    assert ys(layout, "a") == ["a/y", "a/z"] and "a" not in layout.custom.added


def test_deleting_a_chart_forgets_its_x_axis_choice():
    layout = ChartLayout(default())
    a = layout.index_of("a")
    layout.set_chart(a, 0, ChartSpec("a/x", "acc"))
    layout.remove_chart(a, 0)
    assert layout.custom.x == {} and layout.custom.removed == {"a": ["0:a/x"]}


def test_deleting_from_user_panels_and_the_last_chart_of_a_panel():
    layout = ChartLayout(default())
    mine = layout.add_panel("mine")
    layout.add_chart(mine, ChartSpec("acc"))
    layout.add_chart(mine, ChartSpec("preds", kind=TABLE))
    layout.remove_chart(mine, 0)
    assert layout.panels[mine].charts == [ChartSpec("preds", kind=TABLE)]
    layout.remove_chart(mine, 0)
    assert layout.panels[mine].charts == [] and layout.custom.added == {}
    assert layout.custom.panels == ["mine"]  # the empty panel itself stays
    system = layout.index_of("System")
    layout.remove_chart(system, 0)  # a generated chart in the first panel
    assert ys(layout, "System") == []


def test_new_metrics_still_appear_but_deleted_ones_stay_deleted():
    custom = Customization(removed={"a": ["0:a/y"]})
    later = build_panels(["a/aaa_new", "a/x", "a/y", "a/z"], [])
    a = next(p for p in apply_customization(later, custom) if p.name == "a")
    assert [c.y for c in a.charts] == ["a/aaa_new", "a/x", "a/z"]


def test_the_same_metric_as_a_system_chart_is_a_different_chart():
    layout = ChartLayout(build_panels(["system.cpu"], ["system.cpu"]))  # a run metric and a system metric, same name
    system = layout.index_of("System")
    n = len(layout.panels[system].charts)
    layout.remove_chart(system, 0)  # the system one comes first
    assert len(layout.panels[system].charts) == n - 1
    assert layout.custom.removed == {"System": ["1:system.cpu"]}  # keyed with the system flag


def test_reset_brings_everything_back():
    layout = ChartLayout(default())
    layout.remove_chart(layout.index_of("a"), 0)
    layout.reset()
    assert not layout.is_customized and ys(layout, "a") == ["a/x", "a/y", "a/z"]


# ---- saving ------------------------------------------------------------------------------------


def test_removed_charts_survive_a_json_roundtrip_and_old_files_load():
    custom = Customization(removed={"a": ["0:a/y", "1:system.cpu"]}, panels=["p"])
    assert Customization.from_json(json.loads(json.dumps(custom.to_json()))) == custom
    assert Customization.from_json({"panels": ["p"]}).removed == {}  # saved before deleting existed
    assert not Customization(removed={"a": ["k"]}).is_empty  # a deletion alone counts as a customization


@pytest.mark.parametrize("bad", [None, 3, "x", [], {"a": "no"}, {"a": [1, None]}, {3: ["k"]}])
def test_malformed_removed_data_is_ignored(bad):
    assert Customization.from_json({"removed": bad}).removed == {}


def test_partially_valid_removed_data_keeps_the_good_keys():
    assert Customization.from_json({"removed": {"a": ["k1", 5, "k2"]}}).removed == {"a": ["k1", "k2"]}


def test_store_roundtrips_deletions(tmp_path):
    store = LayoutStore(tmp_path / "l.json")
    store.save("r", Customization(removed={"a": ["0:a/y"]}))
    assert store.load("r").removed == {"a": ["0:a/y"]}
    store.reset("r")
    assert store.load("r").is_empty


# ---- screen: deleting charts ---------------------------------------------------------------------


async def start(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    return app, app.run_test(size=SIZE)


def saved():
    path = LayoutStore().path
    runs = json.loads(path.read_text())["projects"] if path.exists() else {}
    return next(iter(runs.values()), None)


async def select_panel(pilot, name):
    labels = panel_labels(pilot.app.screen)
    index = next(i for i, l in enumerate(labels) if l.startswith(name + " ("))
    await pilot.press(*["down"] * index)
    await settle(pilot, 5)


async def test_delete_needs_a_focused_chart(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await open_charts(pilot)  # focus is on the panel list
        assert screen.check_action("delete_chart", ()) is False  # not offered in the key bar either
        await pilot.press("delete")
        await settle(pilot)
        assert app.screen is screen  # nothing opened, nothing deleted
        assert saved() is None


async def test_deleting_asks_first_and_no_changes_nothing(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await open_charts(pilot)
        await select_panel(pilot, "trainer")
        await pilot.press("right", "delete")
        await settle(pilot)
        assert isinstance(app.screen, ConfirmDialog)
        text = " ".join(app.screen.query_one("#confirm-body").render().plain.split())
        assert text == "“global_steps”"  # just what is about to be deleted
        await pilot.press("enter")  # focus starts on "No"
        await settle(pilot)
        for key in ("n", "escape"):
            await pilot.press("delete")
            await settle(pilot)
            await pilot.press(key)
            await settle(pilot)
        assert panel_labels(screen)[1] == "trainer (2)" and saved() is None


async def test_a_generated_chart_can_be_deleted_and_stays_deleted(make_app, monkeypatch):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await open_charts(pilot)
        await select_panel(pilot, "trainer")
        await pilot.press("right", "delete")
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot, 6)
        assert panel_labels(screen)[1] == "trainer (1)"
        (view,) = screen.query(ChartView)
        assert view.spec.y == "trainer/loss"
        assert app.focused is view  # focus moves on to the chart that took its place
        assert saved()["removed"] == {"trainer": ["0:trainer/global_steps"]}
    monkeypatch.setattr(FakeClient, "METRICS", FakeClient.METRICS + ["trainer/extra"])  # logged since
    app2 = make_app()
    async with app2.run_test(size=SIZE) as pilot:
        screen = await open_charts(pilot)
        assert panel_labels(screen)[1] == "trainer (2)"  # new metric appears; the deleted one does not
        await pilot.press("down")
        await settle(pilot, 5)
        assert {v.spec.y for v in screen.query(ChartView)} == {"trainer/extra", "trainer/loss"}  # global_steps stays gone


async def test_backspace_also_deletes_and_focus_stays_at_the_same_position(make_app, monkeypatch):
    monkeypatch.setattr(FakeClient, "METRICS", ["m/a", "m/b", "m/c"])
    monkeypatch.setattr(FakeClient, "VALUES", {"m/a": 1.0, "m/b": 2.0, "m/c": 3.0})
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await open_charts(pilot)
        await select_panel(pilot, "m")
        await pilot.press("right", "down", "backspace")  # the middle chart
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot, 6)
        assert [v.spec.y for v in screen.query(ChartView)] == ["m/a", "m/c"]
        assert app.focused.index == 1 and app.focused.spec.y == "m/c"
        await pilot.press("backspace")  # delete the last one: focus falls back to the one before
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot, 6)
        assert app.focused.spec.y == "m/a" and app.focused.index == 0


async def test_added_charts_bars_and_tables_can_be_deleted(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await open_charts(pilot)
        await new_panel(pilot, "mine")
        await add_plot(pilot, (False, "trainer/loss"))
        await add_table(pilot, "preds")
        await add_bar(pilot, (False, "acc"), (False, "val/loss"))
        assert panel_labels(screen)[-1] == "mine (3)"
        table = screen.query(TableView).first()
        table.focus()
        await settle(pilot)
        await pilot.press("delete")
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot, 6)
        assert panel_labels(screen)[-1] == "mine (2)" and not screen.query(TableView)
        assert [c["kind"] for c in saved()["added"]["mine"]] == ["plot", "bar"]
        assert saved()["removed"] == {}


async def test_deleting_the_last_chart_leaves_an_empty_panel_and_moves_focus_to_the_list(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await open_charts(pilot)
        await new_panel(pilot, "mine")
        await add_plot(pilot, (False, "acc"))
        await pilot.press("delete")
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot, 6)
        assert panel_labels(screen)[-1] == "mine (0)"
        assert "No charts in this panel" in "\n".join(s.text for s in screen._compositor.render_strips())
        assert app.focused.id == "panels"
        assert saved()["panels"] == ["mine"] and saved()["added"] == {}  # the empty panel is still there


async def test_the_x_axis_key_after_a_deletion_changes_the_right_chart(make_app):
    from textual.widgets import Select

    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await open_charts(pilot)
        await select_panel(pilot, "trainer")
        await pilot.press("right", "delete")  # delete trainer/global_steps, loss is now first
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot, 6)
        await pilot.press("x")
        await settle(pilot)
        app.screen.query_one("#x", Select).value = "acc"
        await pilot.click("#ok")
        await settle(pilot, 5)
        (view,) = screen.query(ChartView)
        assert view.spec.y == "trainer/loss" and view.spec.x == "acc"
        assert saved()["x"] == {"trainer": {"0:trainer/loss": "acc"}}


async def test_reset_brings_deleted_charts_back_and_clears_the_saved_record(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await open_charts(pilot)
        await select_panel(pilot, "trainer")
        await pilot.press("right", "delete")
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot, 6)
        assert panel_labels(screen)[1] == "trainer (1)"
        await pilot.press("d")
        await settle(pilot)
        assert "brings back the charts you deleted" in " ".join(app.screen.query_one("#confirm-body").render().plain.split())
        await pilot.press("y")
        await settle(pilot, 6)
        assert panel_labels(screen)[1] == "trainer (2)" and saved() is None
