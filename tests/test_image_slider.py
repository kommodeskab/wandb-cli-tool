import time
from pathlib import Path

import pytest
from PIL import Image
from textual.widgets import OptionList, Select

from chart_helpers import add_images, new_panel, settle, start_add
from conftest import FakeClient
from test_run_detail_screens import screen_text
from test_run_screens import open_charts, panel_labels
from wandb_cli_tool import config
from wandb_cli_tool.charts import IMAGE, ChartSpec, Customization
from wandb_cli_tool.config import Credentials
from wandb_cli_tool.images import ImageStep
from wandb_cli_tool.layout_store import LayoutStore, project_key
from wandb_cli_tool.media import MAX_IMAGE_BYTES
from wandb_cli_tool.screens.chart_dialogs import ImageDialog
from wandb_cli_tool.screens.chart_widgets import ImageChartView, StepSlider

SIZE = (130, 50)


def path_of(key, step, i=None):
    return f"media/images/samples_{step}_abcd1234abcd1234abcd.png" if i is None else f"media/images/grid_{step}_{i}_ffff.png"


async def start(make_app):
    config.save(Credentials("k", "me"))
    app = make_app()
    return app, app.run_test(size=SIZE)


async def with_images(pilot, key="samples"):
    """Charts screen -> new panel "mine" -> an image chart for `key`. Returns (screen, view)."""
    screen = await open_charts(pilot)
    await new_panel(pilot, "mine")
    await add_images(pilot, key)
    (view,) = screen.query(ImageChartView)
    await pilot.pause(0.3)  # the first image is fetched right away; give the worker time
    await settle(pilot, 4)
    return screen, view


def info(view):
    return view.query_one("#img-info").render().plain


async def move(pilot, *keys, wait=0.35):
    await pilot.press(*keys)
    await pilot.pause(wait)  # the debounce, then the download
    await settle(pilot, 4)


# ---- adding an image chart -------------------------------------------------------------------------


async def test_the_image_dialog_lists_the_keys_and_the_newest_step_is_shown_first(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await open_charts(pilot)
        await new_panel(pilot, "mine")
        dialog = await start_add(pilot, "image")
        assert isinstance(dialog, ImageDialog)
        assert [str(l) for l, _ in dialog.query_one("#image-key", Select)._options] == ["grid  (3 per step)", "samples  (one per step)"]
        await pilot.press("escape")
        await settle(pilot)
        await add_images(pilot, "samples")
        (view,) = screen.query(ImageChartView)
        await pilot.pause(0.3)
        await settle(pilot, 4)
        assert view.spec.kind == IMAGE and str(view.border_title) == "Images: samples" and panel_labels(screen)[-1] == "mine (1)"
        assert view.cursor.index == 3 and view.current_step.step == 40  # the newest of 10, 20, 30, 40
        assert "step 40 · 4/4 · caption 40" in info(view)
        assert view.path is not None and view.path.name == Path(path_of("samples", 40)).name
        assert view.query_one("#img").image == view.path  # and that file is what is drawn
        assert app.client.downloads == [path_of("samples", 40)] and app.client.image_step_calls == ["samples"]
        assert view.query_one("#slider", StepSlider).index == 3


async def test_the_arrow_keys_step_and_only_the_step_you_settle_on_is_downloaded(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen, view = await with_images(pilot)
        app.client.downloads.clear()
        await move(pilot, "left", "left", "left")  # three quick presses: 40 -> 10
        assert view.cursor.index == 0 and "step 10 · 1/4" in info(view)
        assert app.client.downloads == [path_of("samples", 10)]  # no downloads for 30 and 20
        assert view.path.name == Path(path_of("samples", 10)).name
        await move(pilot, "left")  # at the start: nothing happens
        assert view.cursor.index == 0 and len(app.client.downloads) == 1
        await move(pilot, "right")
        assert "step 20 · 2/4" in info(view) and app.client.downloads[-1] == path_of("samples", 20)


async def test_shift_home_and_end_jump_further_and_are_clamped(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen, view = await with_images(pilot)
        await move(pilot, "shift+left")  # 10 back from the newest: the first
        assert view.cursor.index == 0
        await move(pilot, "end")
        assert view.cursor.index == 3 and "step 40" in info(view)
        await move(pilot, "home")
        assert view.cursor.index == 0
        await move(pilot, "shift+right")
        assert view.cursor.index == 3
        assert view.query_one("#slider", StepSlider).index == 3


async def test_clicking_the_slider_jumps_to_that_step(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen, view = await with_images(pilot)
        slider = view.query_one("#slider", StepSlider)
        await pilot.click(StepSlider, offset=(StepSlider.EDGE, 0))  # the start of the bar
        await pilot.pause(0.35)
        await settle(pilot, 4)
        assert view.cursor.index == 0 and "step 10" in info(view)
        await pilot.click(StepSlider, offset=(0, 0))  # on the arrow, not the bar: ignored
        await pilot.pause(0.2)
        assert view.cursor.index == 0
        await pilot.click(StepSlider, offset=(StepSlider.EDGE + slider.bar_width // 2, 0))  # the middle
        await pilot.pause(0.35)
        await settle(pilot, 4)
        assert view.cursor.index in (1, 2)
        text = "\n".join(s.text for s in screen._compositor.render_strips())
        assert "●" in text and "◀" in text and "▶" in text


async def test_several_images_in_one_step_are_browsed_with_brackets(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen, view = await with_images(pilot, "grid")
        assert view.current_step.step == 15 and "step 15 · 2/2 · image 1/3 · a" in info(view)
        assert str(view.border_subtitle) == ""  # no key hints in the frame: the key bar has the keys
        await move(pilot, "right_square_bracket")
        assert "image 2/3 · b" in info(view) and app.client.downloads[-1] == path_of("grid", 15, 1)
        await move(pilot, "right_square_bracket", "right_square_bracket")  # the last image: no further
        assert "image 3/3 · c" in info(view) and view.sub == 2
        await move(pilot, "left_square_bracket")
        assert view.sub == 1
        await move(pilot, "left")  # another step starts at its first image
        assert view.current_step.step == 5 and view.sub == 0 and "image 1/3" in info(view)
        assert app.client.downloads[-1] == path_of("grid", 5, 0)


async def test_a_single_image_per_step_has_no_image_counter_or_bracket_hint(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen, view = await with_images(pilot)
        assert "image " not in info(view).replace("caption", "") and "[ ]" not in str(view.border_subtitle)
        await move(pilot, "right_square_bracket")  # does nothing
        assert view.sub == 0


async def test_a_slow_response_for_a_step_you_left_does_not_replace_the_current_image(make_app):
    class Slow(FakeClient):
        def download_file(self, ref, name, dest):
            if "_20_" in name:
                time.sleep(0.8)  # this one arrives long after the slider moved on
            return super().download_file(ref, name, dest)

    config.save(Credentials("k", "me"))
    from wandb_cli_tool.app import WandbCliApp
    from conftest import FakePlayer

    app = WandbCliApp(client_factory=Slow, audio_player=FakePlayer())
    async with app.run_test(size=SIZE) as pilot:
        screen, view = await with_images(pilot)
        await move(pilot, "left")  # 30
        await move(pilot, "left", wait=0.25)  # 20: its download is slow
        await move(pilot, "left")  # 10
        assert "step 10" in info(view) and view.path.name == Path(path_of("samples", 10)).name
        await pilot.pause(1.0)  # the slow download for step 20 finishes now
        await settle(pilot, 4)
        assert view.path.name == Path(path_of("samples", 10)).name and "step 10" in info(view)
        assert view.query_one("#img").image == view.path


async def test_a_failed_download_is_shown_and_the_slider_keeps_working(make_app):
    class Flaky(FakeClient):
        def download_file(self, ref, name, dest):
            if "_30_" in name:
                raise RuntimeError("file is gone")
            return super().download_file(ref, name, dest)

    config.save(Credentials("k", "me"))
    from wandb_cli_tool.app import WandbCliApp
    from conftest import FakePlayer

    app = WandbCliApp(client_factory=Flaky, audio_player=FakePlayer())
    async with app.run_test(size=SIZE) as pilot:
        screen, view = await with_images(pilot)
        await move(pilot, "left")
        assert "step 30 · 3/4 · Error: file is gone" in info(view)
        await move(pilot, "left")
        assert "Error" not in info(view) and "step 20" in info(view) and view.path.name == Path(path_of("samples", 20)).name


async def test_huge_files_are_refused_before_downloading(make_app, monkeypatch):
    big = ImageStep(10, (path_of("samples", 10),), ("h",), (MAX_IMAGE_BYTES + 5 * 1024 * 1024,), ())
    monkeypatch.setitem(FakeClient.IMAGE_STEPS, "samples", [big])
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen, view = await with_images(pilot)
        assert "too large to display (55 MB)" in info(view) and app.client.downloads == []


async def test_big_images_are_shown_as_a_screen_sized_copy(make_app, monkeypatch):
    class Big(FakeClient):
        def download_file(self, ref, name, dest):
            path = dest / name
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (2400, 1800), (40, 90, 160)).save(path)
            self.downloads.append(name)
            return path

    config.save(Credentials("k", "me"))
    from wandb_cli_tool.app import WandbCliApp
    from conftest import FakePlayer

    app = WandbCliApp(client_factory=Big, audio_player=FakePlayer())
    async with app.run_test(size=SIZE) as pilot:
        screen, view = await with_images(pilot)
        assert view.path.name.endswith(".thumb1600.png")
        with Image.open(view.path) as im:
            assert im.size == (1600, 1200)  # fast to draw; the original stays in the cache untouched
        assert view.query_one("#img").image == view.path


async def test_downloaded_images_are_reused_when_you_come_back_to_a_step(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen, view = await with_images(pilot)
        await move(pilot, "left")
        await move(pilot, "right")  # back to 40, already on disk
        assert app.client.downloads == [path_of("samples", 40), path_of("samples", 30)]


# ---- when there is nothing to show --------------------------------------------------------------------


async def test_a_key_with_no_logged_steps_says_so(make_app, monkeypatch):
    monkeypatch.setitem(FakeClient.IMAGE_STEPS, "samples", [])
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen, view = await with_images(pilot)
        assert "No images were logged under this key." in info(view) and app.client.downloads == []
        await move(pilot, "left", "right", "end")  # the keys are harmless
        assert view.cursor.index == 0


async def test_an_error_reading_the_steps_is_shown(make_app, monkeypatch):
    monkeypatch.setattr(FakeClient, "image_step_errors", {"samples": "history unavailable"}, raising=False)
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen, view = await with_images(pilot)
        assert "Error: history unavailable" in info(view)


async def test_a_saved_image_chart_whose_key_vanished_says_so(make_app):
    config.save(Credentials("k", "me"))
    LayoutStore().save(project_key("me", "alpha"), Customization(panels=["mine"], added={"mine": [ChartSpec("gone", kind=IMAGE)]}))
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        screen = await open_charts(pilot)
        await pilot.press("down", "down", "down", "down")
        await settle(pilot, 5)
        (view,) = screen.query(ImageChartView)
        assert "no logged images called “gone”" in screen_text(app) and app.client.image_step_calls == []


# ---- the rest of the chart screen ----------------------------------------------------------------------


async def test_image_charts_are_saved_with_the_layout_and_come_back_showing_the_newest_step(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        await with_images(pilot)
    app2 = make_app()
    async with app2.run_test(size=SIZE) as pilot:
        screen = await open_charts(pilot)
        await pilot.press("down", "down", "down", "down")
        await pilot.pause(0.4)
        await settle(pilot, 5)
        (view,) = screen.query(ImageChartView)
        assert view.spec == ChartSpec("samples", kind=IMAGE) and "step 40" in info(view)


async def test_x_does_nothing_for_images_but_delete_works(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen, view = await with_images(pilot)
        assert app.focused is view
        await pilot.press("x")
        await settle(pilot)
        assert any("only applies to plots" in n.message for n in app._notifications)
        await pilot.press("delete")
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot, 6)
        assert not screen.query(ImageChartView) and panel_labels(screen)[-1] == "mine (0)"


async def test_arrow_keys_step_images_and_up_down_still_change_chart(make_app):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen, view = await with_images(pilot)
        await add_images(pilot, "grid")
        first, second = list(screen.query(ImageChartView))
        first.focus()
        await settle(pilot)
        await pilot.press("down")
        await settle(pilot)
        assert app.focused is second
        await pilot.press("up")
        assert app.focused is first
        footer = screen_text(app)
        assert "Previous step" in footer and "Next step" in footer  # the footer teaches the keys


# ---- refreshing --------------------------------------------------------------------------------------


def with_step_50(monkeypatch):
    new = FakeClient.IMAGE_STEPS["samples"] + [ImageStep(50, (path_of("samples", 50),), ("sha50",), (None,), ("caption 50",))]
    monkeypatch.setitem(FakeClient.IMAGE_STEPS, "samples", new)


async def test_a_refresh_follows_new_steps_when_the_slider_is_at_the_end(make_app, monkeypatch):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen, view = await with_images(pilot)
        assert view.current_step.step == 40
        with_step_50(monkeypatch)
        await pilot.press("r")
        await pilot.pause(0.6)
        await settle(pilot, 6)
        assert view.current_step.step == 50 and "step 50 · 5/5" in info(view)
        assert view.path.name == Path(path_of("samples", 50)).name


async def test_a_refresh_leaves_you_where_you_were_when_you_are_looking_at_an_older_step(make_app, monkeypatch):
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen, view = await with_images(pilot)
        await move(pilot, "left", "left")  # step 20
        shown = view.path
        downloads = list(app.client.downloads)
        with_step_50(monkeypatch)
        await pilot.press("r")
        await pilot.pause(0.6)
        await settle(pilot, 6)
        assert view.current_step.step == 20 and "2/5" in info(view)  # same step, the list is just longer
        assert view.path == shown and app.client.downloads == downloads  # and nothing was downloaded again
        assert view.query_one("#slider", StepSlider).n == 5


async def test_sampled_steps_are_announced(make_app, monkeypatch):
    monkeypatch.setattr(FakeClient, "image_total", 5000, raising=False)
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen, view = await with_images(pilot)
        assert "sampled: 4 of 5,000 steps" in info(view)


async def test_the_image_chart_takes_focus_when_added_and_never_shows_textuals_loading_overlay(make_app):
    """Regression: the chart used `loading` for its own flag, which is a Textual property that
    shows a spinner over the widget and makes it unfocusable until the first image arrived."""
    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen = await open_charts(pilot)
        await new_panel(pilot, "mine")
        await add_images(pilot, "samples")  # no extra waiting: the image may not even be here yet
        (view,) = screen.query(ImageChartView)
        assert app.focused is view
        assert view.loading is False and view.focusable
        await pilot.press("left")  # and the keys work straight away
        await pilot.pause(0.4)
        assert view.cursor.index == 2


async def test_the_image_keys_are_in_the_key_bar_only_for_steps_with_several_images(make_app):
    from wandb_cli_tool.screens.keybar import KeyBar

    app, ctx = await start(make_app)
    async with ctx as pilot:
        screen, view = await with_images(pilot, "grid")  # 3 images per step
        assert view.check_action("sub", ()) is True
        bar = screen.query_one(KeyBar).query_one("KeyLine").render().plain.replace("\xa0", " ")
        assert "[ Previous image" in bar and "] Next image" in bar
