from wandb_cli_tool.pins import Pins, matches_filter, pinned_first
from wandb_cli_tool.preferences import Preferences


def test_pins_are_remembered_per_project_and_kind_in_pin_order(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    a, b = Pins(prefs, "me/alpha"), Pins(prefs, "me/beta")
    assert a.get("runs") == []
    assert a.toggle("runs", "r2") is True and a.toggle("runs", "r1") is True
    assert a.get("runs") == ["r2", "r1"]  # the order they were pinned in
    assert b.get("runs") == [] and a.get("metrics") == []
    assert Pins(Preferences(tmp_path / "p.json"), "me/alpha").get("runs") == ["r2", "r1"]  # saved to disk


def test_toggling_unpins_and_a_mixed_group_is_pinned_whole(tmp_path):
    pins = Pins(Preferences(tmp_path / "p.json"), "p")
    pins.toggle("metrics", "a")
    assert pins.toggle("metrics", "a") is False and pins.get("metrics") == []
    pins.toggle("metrics", "a")
    assert pins.toggle("metrics", "a", "b") is True and pins.get("metrics") == ["a", "b"]  # one was missing: pin both
    assert pins.toggle("metrics", "a", "b") is False and pins.get("metrics") == []
    assert pins.get("metrics") == [] and Preferences(tmp_path / "p.json").get("pins") == {"metrics": {}}


def test_junk_in_the_preferences_is_ignored(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    prefs.set("pins", {"runs": {"p": ["ok", 3, None]}, "metrics": "oops", "config": {"p": "nope"}})
    pins = Pins(prefs, "p")
    assert pins.get("runs") == ["ok"] and pins.get("metrics") == [] and pins.get("config") == []
    pins.toggle("metrics", "m")  # still works on top of the junk
    assert pins.get("metrics") == ["m"]
    prefs.set("pins", ["not a dict"])
    assert Pins(prefs, "p").get("runs") == []


def test_pinned_items_come_first_in_pin_order_and_the_rest_keep_their_order():
    items = ["a", "b", "c", "d", "e"]
    assert pinned_first(items, ["d", "b"], lambda x: x) == ["d", "b", "a", "c", "e"]
    assert pinned_first(items, [], lambda x: x) == items
    assert pinned_first(items, ["zzz"], lambda x: x) == items  # pins of things that are not there


def test_project_pins_are_remembered_per_entity_without_colliding_with_runs(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    me, team = Pins(prefs, "me"), Pins(prefs, "team-a")
    assert me.toggle("projects", "beta") is True and me.toggle("projects", "alpha") is True
    assert me.get("projects") == ["beta", "alpha"]
    assert team.get("projects") == [] and me.get("runs") == []
    assert Pins(Preferences(tmp_path / "p.json"), "me").get("projects") == ["beta", "alpha"]


def test_filter_needs_every_word_and_ignores_case():
    assert matches_filter("train/Loss", "loss") and matches_filter("train/Loss", "TRAIN loss")
    assert not matches_filter("train/loss", "val loss") and matches_filter("anything", "") and matches_filter("x", "  ")
