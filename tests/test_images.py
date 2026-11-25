import json
import os
from types import SimpleNamespace as NS

import pytest
from PIL import Image

from wandb_cli_tool import images as images_mod
from wandb_cli_tool.api import CachedClient, DiskCache, RunRef, WandbApiClient
from wandb_cli_tool.charts import IMAGE, ChartSpec, Customization
from wandb_cli_tool.images import (
    ImageInfo, ImageSeries, ImageStep, StepCursor, image_entries, index_at, make_thumbnail,
    parse_image_steps, slider_geometry,
)

REF = RunRef("me", "proj", "abc")


def single(step, **extra):
    return {"_type": "image-file", "path": f"media/images/k_{step}_aa.png", **extra}


# ---- finding image keys -----------------------------------------------------------------------


def test_image_entries_finds_single_and_multiple_image_keys():
    summary = {
        "val/samples": single(3, sha256="h", size=10),
        "grid": {"_type": "images/separated", "filenames": ["a.png", "b.png"], "count": 2},
        "grid_nocount": {"_type": "images/separated", "filenames": ["a.png", "b.png", "c.png"]},
        "empty": {"_type": "images/separated", "filenames": []},
        "nofiles": {"_type": "images/separated"},
        "pathless": {"_type": "image-file"},
        "table": {"_type": "table-file", "path": "t.table.json"},
        "loss": 0.5, "text": "x",
    }
    found = image_entries(summary)
    assert [(i.key, i.multiple, i.count) for i in found] == [
        ("grid", True, 2), ("grid_nocount", True, 3), ("val/samples", False, 1),
    ]
    assert found[0].label == "grid  (2 per step)" and found[2].label == "val/samples  (one per step)"
    assert ImageInfo("x", True, None).label == "x  (one per step)"


# ---- the steps of one key ------------------------------------------------------------------------


def row(step, value, key="img"):
    return {"_step": step, key: value}


def test_steps_are_sorted_and_keep_the_details_of_each_image():
    rows = [
        row(30, single(30, sha256="h30", size=300, caption="third")),
        row(10, single(10)),
        row(20, {"_type": "images/separated", "filenames": ["x0.png", "x1.png"], "captions": ["c0", "c1"]}),
    ]
    series = parse_image_steps(rows, "img")
    assert [s.step for s in series.steps] == [10, 20, 30] and series.total == 3 and not series.sampled
    s10, s20, s30 = series.steps
    assert s10.paths == ("media/images/k_10_aa.png",) and s10.identity(0) == s10.paths[0]  # no hash: falls back to the path
    assert s20.paths == ("x0.png", "x1.png") and (s20.caption(0), s20.caption(1), s20.caption(5)) == ("c0", "c1", "")
    assert (s30.identity(0), s30.size(0), s30.caption(0)) == ("h30", 300, "third") and s30.size(3) is None


def test_unusable_rows_are_skipped_and_a_repeated_step_keeps_the_latest():
    rows = [
        {"_step": 1, "other": single(1)},  # key not logged here
        row(2, "not a dict"), row(3, {"_type": "table-file"}), row(4, {"_type": "image-file"}),
        row(5, {"_type": "images/separated", "filenames": []}),
        {"img": single(6)},  # no step
        row(True, single(7)),  # a bool is not a step
        row(8, single(8)), row(8, single(80)),
    ]
    series = parse_image_steps(rows, "img")
    assert [(s.step, s.paths) for s in series.steps] == [(8, ("media/images/k_80_aa.png",))]
    assert parse_image_steps([], "img") == ImageSeries((), 0)


def test_too_many_steps_are_sampled_evenly_keeping_the_first_and_the_last():
    rows = [row(s, single(s)) for s in range(0, 1000, 2)]  # 500 steps
    series = parse_image_steps(rows, "img", limit=11)
    steps = [s.step for s in series.steps]
    assert series.total == 500 and series.sampled and len(steps) == 11
    assert steps[0] == 0 and steps[-1] == 998 and steps == sorted(set(steps))
    gaps = {b - a for a, b in zip(steps, steps[1:])}
    assert max(gaps) - min(gaps) <= 4  # roughly even
    two = parse_image_steps(rows, "img", limit=2)
    assert [s.step for s in two.steps] == [0, 998]
    assert not parse_image_steps(rows, "img", limit=500).sampled


# ---- the slider ----------------------------------------------------------------------------------


def test_cursor_moves_clamps_and_knows_when_it_is_at_the_end():
    c = StepCursor(5, 2)
    assert (c.index, c.at_end) == (2, False)
    assert c.move(1) and c.move(1) and c.index == 4 and c.at_end
    assert not c.move(1) and c.index == 4  # no change at the end
    assert c.move(-100) and c.index == 0 and not c.move(-1)
    assert c.jump(3) and not c.jump(3) and c.jump(99) and c.index == 4
    assert StepCursor(5, 99).index == 4 and StepCursor(5, -3).index == 0


def test_an_empty_cursor_is_safe():
    c = StepCursor(0)
    assert c.index == 0 and c.at_end and not c.move(1) and not c.jump(5) and c.index == 0


@pytest.mark.parametrize("n", [1, 2, 3, 7, 50, 1000])
def test_slider_position_and_click_agree(n):
    width = 40
    for index in range(0, n, max(1, n // 20)):
        knob, bar = slider_geometry(width, index, n)
        assert 0 <= knob < bar == width
        # clicking where the knob is gives back the step (exact when the bar has a column per step)
        if n <= width:
            assert index_at(knob, width, n) == index


def test_clicks_map_across_the_whole_bar_and_clamp():
    assert [index_at(x, 21, 5) for x in (0, 5, 10, 15, 20)] == [0, 1, 2, 3, 4]
    assert index_at(-5, 21, 5) == 0 and index_at(500, 21, 5) == 4 and index_at(3, 21, 1) == 0 and index_at(3, 21, 0) == 0
    assert slider_geometry(21, 0, 1) == (0, 21) and slider_geometry(1, 3, 5)[1] == 2  # degenerate widths still work


# ---- thumbnails ------------------------------------------------------------------------------------


def png(path, size, mode="RGB", color=(10, 20, 30)):
    Image.new(mode, size, color).save(path)
    return path


def test_small_images_are_shown_as_they_are(tmp_path):
    p = png(tmp_path / "a.png", (80, 60))
    assert make_thumbnail(p, 100) == p and not list(tmp_path.glob("*.thumb*"))


def test_big_images_get_a_cached_screen_sized_copy(tmp_path):
    p = png(tmp_path / "big.png", (400, 200))
    thumb = make_thumbnail(p, 100)
    assert thumb != p and thumb.name == "big.thumb100.png"
    with Image.open(thumb) as im:
        assert im.size == (100, 50)  # aspect ratio kept
    with Image.open(p) as im:
        assert im.size == (400, 200)  # the original is untouched
    mtime = thumb.stat().st_mtime_ns
    assert make_thumbnail(p, 100) == thumb and thumb.stat().st_mtime_ns == mtime  # reused, not redone
    assert not list(tmp_path.glob("*.tmp*"))


def test_a_changed_original_refreshes_its_copy(tmp_path):
    p = png(tmp_path / "big.png", (400, 200), color=(1, 1, 1))
    thumb = make_thumbnail(p, 100)
    png(p, (200, 400), color=(250, 250, 250))
    os.utime(p, (thumb.stat().st_mtime + 10, thumb.stat().st_mtime + 10))
    with Image.open(make_thumbnail(p, 100)) as im:
        assert im.size == (50, 100)


def test_transparent_and_jpeg_images_work_and_junk_is_rejected(tmp_path):
    rgba = png(tmp_path / "t.png", (300, 300), "RGBA", (10, 20, 30, 0))
    with Image.open(make_thumbnail(rgba, 100)) as im:
        assert im.size == (100, 100) and im.mode == "RGBA"
    jpg = tmp_path / "j.jpg"
    Image.new("RGB", (300, 150), (5, 5, 5)).save(jpg)
    with Image.open(make_thumbnail(jpg, 100)) as im:
        assert im.size == (100, 50)
    bad = tmp_path / "bad.png"
    bad.write_text("not an image")
    with pytest.raises(Exception):
        make_thumbnail(bad)


# ---- the image chart kind in layouts --------------------------------------------------------------


def test_the_image_kind_titles_and_saves_like_the_others():
    spec = ChartSpec("val/samples", kind=IMAGE)
    assert spec.title == "Images: val/samples" and spec.series == ("val/samples",)
    custom = Customization(added={"p": [spec]})
    assert Customization.from_json(json.loads(json.dumps(custom.to_json()))) == custom
    two = {"y": "a", "x": "_step", "system": False, "kind": "image", "more": ["b"]}
    assert Customization.from_json({"added": {"p": [two]}}).is_empty  # one key only


# ---- the client --------------------------------------------------------------------------------------


class FakeRun:
    def __init__(self, rows=()):
        self.rows, self.asked, self.pages = list(rows), [], []
        self.summary_metrics = {"a": single(1), "loss": 1.0}

    def scan_history(self, keys=None, page_size=1000):
        self.asked.append(keys)
        self.pages.append(page_size)
        return iter(self.rows)


@pytest.fixture
def client(monkeypatch):
    import wandb

    run = FakeRun([row(s, single(s)) for s in (3, 1, 2)])

    class Api:
        def __init__(self, api_key=None):
            self._service_api = None

        def run(self, path):
            return run

    monkeypatch.setattr(wandb, "Api", Api)
    return WandbApiClient("key"), run


def test_the_client_reads_image_keys_from_the_summary_and_steps_from_the_history(client):
    c, run = client
    assert [(i.key, i.multiple) for i in c.run_images(REF)] == [("a", False)]
    series = c.image_steps(REF, "img")
    assert [s.step for s in series.steps] == [1, 2, 3] and run.asked == [["img"]]  # only this key is read


def test_reading_the_history_is_bounded(client, monkeypatch):
    c, run = client
    run.rows = [row(s, single(s)) for s in range(100)]
    monkeypatch.setattr(images_mod, "MAX_SCAN_ROWS", 10)
    import wandb_cli_tool.api.client as client_mod

    monkeypatch.setattr(client_mod, "MAX_SCAN_ROWS", 10)
    assert len(c.image_steps(REF, "img").steps) == 10


def test_cached_client_remembers_image_data_of_finished_runs_only(tmp_path):
    class Inner:
        calls = []

        def run_images(self, ref):
            self.calls.append("images")
            return [ImageInfo("k", True, 2)]

        def image_steps(self, ref, key):
            self.calls.append(("steps", key))
            step = ImageStep(5, ("a.png", "b.png"), ("h1", "h2"), (1, None), ("c1", ""))
            return ImageSeries((step,), 9)

    inner = Inner()
    finished, live = RunRef("e", "p", "r", version="v1"), RunRef("e", "p", "r")
    for _ in range(2):
        c = CachedClient(inner, DiskCache(tmp_path))
        assert c.run_images(finished) == [ImageInfo("k", True, 2)]
        series = c.image_steps(finished, "k")
        assert series == ImageSeries((ImageStep(5, ("a.png", "b.png"), ("h1", "h2"), (1, None), ("c1", "")),), 9)  # tuples restored
    assert inner.calls == ["images", ("steps", "k")]
    c.image_steps(live, "k"), c.image_steps(live, "k")
    assert inner.calls[2:] == [("steps", "k")] * 2
