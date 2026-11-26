from textual.geometry import Region
from textual.widgets import DataTable, Input, OptionList, Select, SelectionList, Static, Tree
from textual_image.widget import AutoImage, HalfcellImage, UnicodeImage

from conftest import FakePlayer
from wandb_cli_tool import config
from wandb_cli_tool.api import FileInfo, RunRef
from wandb_cli_tool.app import WandbCliApp
from wandb_cli_tool.config import Credentials
from wandb_cli_tool.player import INSTALL_HINT
from wandb_cli_tool.screens.config import MAX_CHILDREN, ConfigPane, populate
from wandb_cli_tool.screens.dialogs import GroupDialog, RenameDialog, TagsDialog
from wandb_cli_tool.screens.files import FileScreen, FilesPane
from wandb_cli_tool.screens.run import RunScreen
from wandb_cli_tool.screens.runs import RunsScreen

from conftest import FakeClient

SIZE = (130, 45)


def make(player=None):
    config.save(Credentials("k", "me"))
    return WandbCliApp(client_factory=FakeClient, audio_player=player or FakePlayer())


async def settle(pilot, n=4):
    for _ in range(n):
        await pilot.pause()


def screen_text(app):
    # the key bar joins "key label" with non-breaking spaces so that pairs wrap whole
    return "\n".join(s.text.rstrip() for s in app.screen._compositor.render_strips()).replace("\xa0", " ")


def tags_line(app):
    """Words on the 'Tags:' line, e.g. ['Tags:', 'baseline', 'v2']."""
    line = next(l for l in screen_text(app).splitlines() if l.strip().startswith("Tags:"))
    return line.split()


async def to_run(pilot, index=0):
    """Projects -> alpha -> the run at `index` (run-1, run-2, run-3)."""
    await settle(pilot)
    await pilot.press("enter")
    await settle(pilot)
    await pilot.press(*(["down"] * index), "enter")
    await settle(pilot)
    assert isinstance(pilot.app.screen, RunScreen)


# ---- run header -------------------------------------------------------------------


async def test_run_screen_shows_tags_at_top_then_run_facts(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_run(pilot)
        text = screen_text(app)
        assert text.index("Tags:") < text.index("Status")  # tags come first
        assert tags_line(app) == ["Tags:", "baseline", "v2"]
        for expected in (
            "● running", "2024-05-01 10:30", "sweep-a", "gpu-box-1",
            "NVIDIA A100 × 2", "16 (32 threads)", "3.10.12", "git@github.com:me/repo.git",
            "abcdef1234 (uncommitted changes)", "python train.py --lr 0.1",
        ):
            assert expected in text, expected
        assert "Run time" in text and "Started" in text and "GPU" in text and "Git state" in text


async def test_run_without_tags_or_metadata_shows_placeholders(make_app):
    class NoMeta(FakeClient):
        def run_metadata(self, ref):
            raise RuntimeError("no metadata file")

    config.save(Credentials("k", "me"))
    app = WandbCliApp(client_factory=NoMeta, audio_player=FakePlayer())
    async with app.run_test(size=SIZE) as pilot:
        await to_run(pilot, 1)  # run-2 has no tags and no group
        text = screen_text(app)
        assert "none" in text and "press t" not in text
        assert "finished" in text and "gpu-box-1" not in text
        assert "…" not in text  # nothing stuck on "loading"


async def test_menu_opens_logs_charts_config_and_files(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_run(pilot)
        assert app.screen.active_tab == "overview"
        await pilot.press("4")
        await settle(pilot)
        assert app.screen.active_tab == "config" and isinstance(app.screen.config_pane, ConfigPane)
        await pilot.press("5")
        await settle(pilot)
        assert app.screen.active_tab == "files" and isinstance(app.screen.files_pane, FilesPane)
        await pilot.press("2")
        await settle(pilot)
        assert app.screen.active_tab == "logs"
        await pilot.press("3")
        await settle(pilot)
        assert app.screen.active_tab == "charts"
        await pilot.press("1")
        await settle(pilot)
        assert app.screen.active_tab == "overview"


# ---- group ------------------------------------------------------------------------


async def test_group_dialog_lists_groups_in_project_and_picking_one_writes_it(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_run(pilot)
        await pilot.press("g")
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, GroupDialog)
        select = dialog.query_one("#group", Select)
        assert select.value == "sweep-a"  # the run's current group is preselected
        assert [v for _, v in select._options if isinstance(v, str)] == ["sweep-a", "sweep-b"]
        select.value = "sweep-b"
        await pilot.click("#ok")
        await settle(pilot)
        assert app.client.writes == [("group", "id1", "sweep-b")]
        assert "sweep-b" in screen_text(app)  # header updated


async def test_group_dialog_new_name_wins_and_run_list_refreshes_on_return(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_run(pilot)
        await pilot.press("g")
        await settle(pilot)
        app.screen.query_one("#group", Select).value = "sweep-b"
        app.screen.query_one("#new-group", Input).value = "  brand-new  "
        app.screen.query_one("#new-group", Input).focus()
        await pilot.press("enter")
        await settle(pilot)
        assert app.client.writes == [("group", "id1", "brand-new")]
        assert ("me", "alpha") in app.touched_projects
        await pilot.press("escape")  # back to the runs list
        await settle(pilot, 6)
        assert isinstance(app.screen, RunsScreen)
        table = app.screen.query_one("#items", DataTable)
        assert str(table.get_row_at(0)[4]) == "brand-new"  # list refreshed in place
        assert ("me", "alpha") not in app.touched_projects
        # the next dialog offers the new group too
        await pilot.press("enter")
        await settle(pilot)
        await pilot.press("g")
        await settle(pilot)
        assert "brand-new" in [v for _, v in app.screen.query_one("#group", Select)._options]


async def test_group_dialog_requires_a_choice_and_escape_cancels(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_run(pilot, 1)  # run-2: no group
        await pilot.press("g")
        await settle(pilot)
        assert app.screen.query_one("#group", Select).value is Select.NULL
        await pilot.click("#ok")
        await settle(pilot)
        assert isinstance(app.screen, GroupDialog)
        assert "Pick a group" in screen_text(app)
        await pilot.press("escape")
        await settle(pilot)
        assert isinstance(app.screen, RunScreen)
        assert app.client.writes == []


# ---- tags -------------------------------------------------------------------------


async def test_tags_dialog_offers_existing_tags_and_new_ones_and_only_adds(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_run(pilot, 1)  # run-2: no tags; project has baseline, v2
        await pilot.press("t")
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, TagsDialog)
        tags = dialog.query_one("#tags", SelectionList)
        assert [tags.get_option_at_index(i).value for i in range(tags.option_count)] == ["baseline", "v2"]
        tags.select("v2")
        dialog.query_one("#new-tags", Input).value = "extra, v2 , x,"
        await pilot.click("#ok")
        await settle(pilot)
        assert app.client.writes == [("tags", "id2", ["v2", "extra", "x"])]
        assert tags_line(app) == ["Tags:", "v2", "extra", "x"]


async def test_tags_dialog_hides_tags_the_run_already_has_and_rejects_empty(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_run(pilot)  # run-1 already has baseline and v2
        await pilot.press("t")
        await settle(pilot)
        assert not app.screen.query("#tags")  # nothing left to pick, only "new tags"
        app.screen.query_one("#new-tags", Input).value = "v2"  # already on the run
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, TagsDialog) and "at least one new tag" in screen_text(app)
        app.screen.query_one("#new-tags", Input).value = "fresh"
        await pilot.press("enter")
        await settle(pilot)
        assert app.client.writes == [("tags", "id1", ["fresh"])]
        assert tags_line(app) == ["Tags:", "baseline", "v2", "fresh"]


async def test_failed_write_reports_error_and_leaves_run_unchanged(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_run(pilot)
        app.client.fail_writes = True
        await pilot.press("g")
        await settle(pilot)
        app.screen.query_one("#new-group", Input).value = "nope"
        app.screen.query_one("#new-group", Input).focus()
        await pilot.press("enter")
        await settle(pilot)
        assert any("Could not set group: permission denied" in n.message for n in app._notifications)
        assert "sweep-a" in screen_text(app) and "nope" not in screen_text(app)
        assert ("me", "alpha") not in app.touched_projects


# ---- rename -----------------------------------------------------------------------


async def test_rename_prefills_the_current_name_and_saves_the_new_one(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_run(pilot)
        await pilot.press("r")
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, RenameDialog)
        field = dialog.query_one("#new-name", Input)
        assert field.value == "run-1" and field.has_focus
        field.value = "  run-1 final  "
        await pilot.press("enter")
        await settle(pilot)
        assert app.client.writes == [("name", "id1", "run-1 final")]
        text = screen_text(app)
        assert "Run — run-1 final" in text and ("me", "alpha") in app.touched_projects
        await pilot.press("escape")  # back to the runs list, which refreshes in place
        await settle(pilot, 6)
        assert isinstance(app.screen, RunsScreen)
        table = app.screen.query_one("#items", DataTable)
        assert str(table.get_row_at(0)[0]) == "run-1 final"
        assert str(table.get_row_at(0)[-1]) == "id1"  # the id is untouched


async def test_rename_unchanged_empty_cancelled_and_failed_do_not_change_anything(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_run(pilot)
        # unchanged: closes without writing
        await pilot.press("r")
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, RunScreen) and app.client.writes == []
        # empty: stays open with an error
        await pilot.press("r")
        await settle(pilot)
        app.screen.query_one("#new-name", Input).value = "   "
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, RenameDialog) and "cannot be empty" in screen_text(app)
        # cancelled
        app.screen.query_one("#new-name", Input).value = "other"
        await pilot.press("escape")
        await settle(pilot)
        assert isinstance(app.screen, RunScreen) and app.client.writes == []
        # failed write: error shown, name and list marker untouched
        app.client.fail_writes = True
        await pilot.press("r")
        await settle(pilot)
        app.screen.query_one("#new-name", Input).value = "will fail"
        await pilot.press("enter")
        await settle(pilot)
        assert any("Could not rename run: permission denied" in n.message for n in app._notifications)
        assert "Run — run-1" in screen_text(app) and "will fail" not in screen_text(app)
        assert ("me", "alpha") not in app.touched_projects


# ---- config -----------------------------------------------------------------------


def labels(node):
    return [str(c.label) for c in node.children]


async def test_config_is_shown_as_an_expandable_tree(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_run(pilot)
        await pilot.press("4")
        await settle(pilot)
        tree = app.screen.query_one("#config", Tree)
        assert labels(tree.root) == ["lr: 0.1", "model  {} 2", "seed: 1", "tags: []"]
        model = tree.root.children[1]
        assert model.is_expanded and labels(model) == ["layers  [] 2", "name: resnet"]
        layers = model.children[0]
        assert not layers.is_expanded  # deeper levels start collapsed
        assert labels(layers) == ["[0]: 64", "[1]: 128"]
        assert "4 config entries" in screen_text(app)


async def test_empty_config_and_huge_lists_are_handled(make_app):
    class Empty(FakeClient):
        def run_config(self, ref):
            return {}

    config.save(Credentials("k", "me"))
    app = WandbCliApp(client_factory=Empty, audio_player=FakePlayer())
    async with app.run_test(size=SIZE) as pilot:
        await to_run(pilot)
        await pilot.press("4")
        await settle(pilot)
        assert "no config" in screen_text(app)

    tree = Tree("t")
    populate(tree.root, {"big": list(range(MAX_CHILDREN + 25)), "s": "x" * 1000})
    big = tree.root.children[0]
    assert len(big.children) == MAX_CHILDREN + 1 and str(big.children[-1].label) == "… 25 more"
    assert len(str(tree.root.children[1].label)) < 400  # long values are truncated


# ---- files ------------------------------------------------------------------------


async def to_files(pilot):
    await to_run(pilot)
    await pilot.press("5")
    await settle(pilot)
    assert isinstance(pilot.app.screen, RunScreen) and pilot.app.screen.active_tab == "files"


def find(node, basename):
    """The file node whose path ends with `basename`."""
    for child in node.children:
        if isinstance(child.data, FileInfo) and child.data.name.endswith(basename):
            return child
        found = find(child, basename)
        if found:
            return found
    return None


async def open_file(pilot, basename):
    """Expand the folders above `basename` in the tree and press Enter on it."""
    tree = pilot.app.screen.query_one("#files", Tree)
    node = find(tree.root, basename)
    assert node is not None, basename
    parent = node.parent
    while parent is not None:
        parent.expand()
        parent = parent.parent
    await settle(pilot)
    tree.move_cursor(node)
    await settle(pilot)
    await pilot.press("enter")
    await settle(pilot, 8)


async def test_files_are_shown_as_a_tree_by_folder_with_slashless_names_in_the_root(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_files(pilot)
        tree = app.screen.query_one("#files", Tree)
        root = [str(c.label) for c in tree.root.children]
        assert [r.split("  ")[0] for r in root] == ["media/", "model.ckpt", "notes.txt", "output.log"]
        assert root[0] == "media/  3 files" and "3.0 GB" in root[1]
        media = tree.root.children[0]
        assert not media.is_expanded  # folders start collapsed
        assert [str(c.label).split("  ")[0] for c in media.children] == ["audio/", "images/", "table/"]
        inner = media.children[0].children[0]  # media/audio/audio/
        assert str(inner.label).startswith("audio/") and not inner.allow_expand is False
        assert str(inner.children[0].label).startswith("sample.wav") and "audio" in str(inner.children[0].label)
        assert "6 files" in screen_text(app)


async def test_enter_on_a_folder_expands_it_and_does_not_open_a_file(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_files(pilot)
        tree = app.screen.query_one("#files", Tree)
        assert tree.cursor_node is tree.root.children[0]  # media/
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, RunScreen) and app.screen.active_tab == "files" and tree.root.children[0].is_expanded
        assert "images/" in screen_text(app)
        await pilot.press("enter")
        await settle(pilot)
        assert not tree.root.children[0].is_expanded


def test_build_file_tree_handles_odd_names():
    from wandb_cli_tool.screens.files import build_file_tree

    root = build_file_tree(
        [FileInfo("a"), FileInfo("d/e/f.txt"), FileInfo("d//g.txt"), FileInfo("/lead.txt"), FileInfo("")]
    )
    assert root.count == 4  # the empty name is ignored
    assert [n for n, _ in root.files] == ["a", "lead.txt"]
    assert sorted(root.dirs["d"].dirs) == ["e"] and [n for n, _ in root.dirs["d"].files] == ["g.txt"]
    assert root.dirs["d"].count == 2


async def test_image_file_is_downloaded_once_and_displayed(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_files(pilot)
        await open_file(pilot, "pred_0.png")
        assert isinstance(app.screen, FileScreen)
        assert len(app.screen.query(AutoImage)) == 1
        assert "PNG 8×8" in screen_text(app) and "zoom 100%" in screen_text(app)
        assert app.client.downloads == ["media/images/pred_0.png"]
        await pilot.press("escape")
        await settle(pilot)
        await pilot.press("enter")  # reopen the same file
        await settle(pilot, 8)
        assert len(app.screen.query(AutoImage)) == 1
        assert app.client.downloads == ["media/images/pred_0.png"]  # served from disk


async def test_image_zoom_changes_displayed_size_and_fit_restores_it(make_app):
    from textual.events import MouseDown, MouseMove, MouseUp

    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_files(pilot)
        await open_file(pilot, "pred_0.png")
        screen = app.screen
        pane = screen.query_one("#image-scroll")
        image = screen.image_widget
        vp_w, vp_h = pane.content_size.width, pane.content_size.height
        # Fit fills the viewport preserving aspect ratio (no 25% top-left thumbnail)
        assert max(image.size.width / vp_w, image.size.height / vp_h) > 0.8
        assert image.size.width <= vp_w and image.size.height <= vp_h

        await pilot.press("plus")
        await settle(pilot)
        assert "zoom 150%" in screen_text(app)
        assert image.size.width <= vp_w and image.size.height <= vp_h  # cropped to viewport

        # Clamps at 1000%
        await pilot.press(*["plus"] * 12)
        await settle(pilot)
        assert "zoom 1000%" in screen_text(app)
        assert image.size.width <= vp_w and image.size.height <= vp_h

        # Mouse drag pans toward bottom-left (drag right + up) and arrow keys pan too
        before = (screen.viewport.pan_x, screen.viewport.pan_y)
        pane.on_mouse_down(MouseDown(pane, 10, 10, 0, 0, 1, False, False, False, screen_x=10, screen_y=10))
        pane.on_mouse_move(MouseMove(pane, 25, 2, -15, -8, 1, False, False, False, screen_x=25, screen_y=2))
        pane.on_mouse_up(MouseUp(pane, 25, 2, 0, 0, 1, False, False, False, screen_x=25, screen_y=2))
        await settle(pilot)
        assert screen.viewport.pan_x < before[0] and screen.viewport.pan_y > before[1]
        await pilot.press("right")
        await settle(pilot)
        assert "pan " in screen_text(app)

        # Clamps at 25%
        await pilot.press(*["minus"] * 15)
        await settle(pilot)
        assert "zoom 25%" in screen_text(app)

        await pilot.press("0")
        await settle(pilot)
        assert "zoom 100%" in screen_text(app) and not screen.viewport.is_panned


async def test_status_names_the_renderer_auto_resolved_to(make_app):
    from wandb_cli_tool.screens.files import DETECTED, LOW_RES

    assert DETECTED in ("sixel", "tgp", "halfcell", "unicode")
    app = make()
    async with app.run_test(size=(180, 45)) as pilot:
        await to_files(pilot)
        await open_file(pilot, "pred_0.png")
        text = screen_text(app)
        assert f"render: auto → {DETECTED}" in text
        assert ("low resolution" in text) == LOW_RES  # only warn when graphics are missing


async def test_render_mode_cycles_and_keeps_zoom(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_files(pilot)
        await open_file(pilot, "pred_0.png")
        await pilot.press("plus")
        await settle(pilot)
        width = app.screen.image_widget.styles.width.value
        seen = [type(app.screen.image_widget)]
        for expected in ("half-cell", "unicode", "auto"):
            await pilot.press("m")
            await settle(pilot, 6)
            seen.append(type(app.screen.image_widget))
            assert f"render: {expected}" in screen_text(app)
            assert "zoom 150%" in screen_text(app)
            assert app.screen.image_widget.styles.width.value == width
        assert seen == [AutoImage, HalfcellImage, UnicodeImage, AutoImage]
        assert len(app.screen.query("#image-scroll > *")) == 1  # old widget removed


async def test_zoom_and_mode_keys_are_not_offered_for_non_images(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_files(pilot)
        await open_file(pilot, "notes.txt")
        assert isinstance(app.screen, FileScreen)
        assert "Zoom" not in screen_text(app) and "Render mode" not in screen_text(app)
        await pilot.press("plus", "m")  # harmless no-ops
        await settle(pilot)
        assert isinstance(app.screen, FileScreen)


async def test_txt_file_is_shown_as_text(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_files(pilot)
        await open_file(pilot, "notes.txt")
        text = screen_text(app)
        assert "line one" in text and "line two" in text and "2 lines" in text
        assert app.client.downloads == ["notes.txt"]


async def test_json_file_is_pretty_printed(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_files(pilot)
        await open_file(pilot, "results.table.json")
        text = screen_text(app)
        assert '"columns": [' in text and '"data": [' in text  # re-indented, one key per line
        assert "pretty-printed JSON" in text


async def test_invalid_json_oversized_text_and_long_text_are_handled(make_app, monkeypatch):
    from wandb_cli_tool.screens import files as files_mod

    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_files(pilot)
        ref = RunRef("me", "alpha", "id1")
        await app.push_screen(FileScreen(ref, FileInfo("bad.json", 4, "application/json", "q")))
        await settle(pilot, 8)
        assert "data" in screen_text(app) and "not valid JSON" in screen_text(app)

        await app.push_screen(FileScreen(ref, FileInfo("huge.txt", 99 * 1024 * 1024, "text/plain", "r")))
        await settle(pilot)
        assert "too large" in screen_text(app)

        monkeypatch.setattr(files_mod, "MAX_TEXT_LINES", 50)
        await app.push_screen(FileScreen(ref, FileInfo("long.txt", 0, "text/plain", "s")))
        await settle(pilot, 10)
        text = screen_text(app)
        assert "200 lines" in text and "showing the first 50" in text and "row 0" in text
        assert "row 199" not in text


async def test_audio_file_shows_a_waveform_and_can_be_played_paused_and_continued(make_app):
    player = FakePlayer()
    app = make(player)
    async with app.run_test(size=SIZE) as pilot:
        await to_files(pilot)
        await open_file(pilot, "sample.wav")
        text = screen_text(app)
        assert "sample.wav" in text and "space Play" in text  # the key bar says what it will do
        assert "■ 0:00 / 0:00" in text and "♪" not in text  # a player line, not a music note
        await pilot.press("space")
        await settle(pilot)
        assert [p.name for p in player.played] == ["sample.wav"] and player.playing
        assert "▶" in screen_text(app) and "space Pause" in screen_text(app)
        await pilot.press("space")
        await settle(pilot)
        assert player.paused and player.pauses == 1 and "⏸" in screen_text(app) and "space Continue" in screen_text(app)
        await pilot.press("space")
        await settle(pilot)
        assert player.playing and not player.paused and player.resumes == 1 and player.played.count(player.played[0]) == 1
        stops = player.stops
        await pilot.press("escape")  # leaving the screen silences it
        await settle(pilot)
        assert player.stops > stops and not player.playing


async def test_audio_without_a_player_explains_what_to_install(make_app):
    player = FakePlayer(available=False)
    app = make(player)
    async with app.run_test(size=SIZE) as pilot:
        await to_files(pilot)
        await open_file(pilot, "sample.wav")
        assert "mpv" in screen_text(app)
        await pilot.press("space")
        await settle(pilot)
        assert INSTALL_HINT[:30] in screen_text(app) and player.played == []


async def test_unsupported_oversized_and_corrupt_files_get_placeholders(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_files(pilot)
        await open_file(pilot, "model.ckpt")
        assert "No preview available" in screen_text(app)
        assert app.client.downloads == []  # nothing downloaded for unsupported types
        ref = RunRef("me", "alpha", "id1")
        await app.push_screen(FileScreen(ref, FileInfo("big.png", 60 * 1024 * 1024, "image/png", "z")))
        await settle(pilot)
        assert "too large" in screen_text(app) and app.client.downloads == []
        await app.push_screen(FileScreen(ref, FileInfo("bad.jpg", 4, "image/jpeg", "y")))
        await settle(pilot, 8)
        assert "Could not load this file" in screen_text(app)


# ---- regression: sixel terminals must get the dedicated sixel widget ------------------


def test_auto_mode_uses_the_librarys_best_widget_not_autoimage():
    """`AutoImage` wraps the plain sixel renderer, which Textual drops, leaving a blank (dark)
    area on sixel terminals. "auto" is the dedicated sixel widget there (our fixed subclass)."""
    import textual_image.widget as tiw
    from wandb_cli_tool.screens import files as files_mod
    from wandb_cli_tool.screens.sixelimage import SafeSixelImage

    expected = SafeSixelImage if tiw.Image is tiw.SixelImage else tiw.Image
    assert files_mod.RENDER_MODES[0] == ("auto", expected)


def _set_cell_size(monkeypatch, width: int, height: int) -> None:
    from textual_image import _terminal

    cell = _terminal.CellSize(width, height)
    monkeypatch.setattr(_terminal.get_cell_size, "_result", cell, raising=False)
    if (probe := getattr(_terminal, "probe_terminal", None)) is not None:
        caps = getattr(probe, "_result", None)
        updated = (
            caps._replace(cell_size=cell)
            if caps is not None
            else _terminal.TerminalCapabilities(cell, sixel=False, tgp=False)
        )
        monkeypatch.setattr(probe, "_result", updated, raising=False)


async def test_the_sixel_widget_never_draws_outside_its_cells(make_app, monkeypatch):
    """textual-image 0.12.0 left the cursor one row below the image (Textual then wrote the rest of
    that line there: the key bar was blanked, or the terminal scrolled and showed the bars twice),
    left the image's rows uncleared (old images lingered after zooming) and its last 6-pixel sixel
    band could run into the next row."""
    import re

    from wandb_cli_tool.screens import files as files_mod
    from wandb_cli_tool.screens.sixelimage import SafeSixelImage, _SafeSixelImpl

    monkeypatch.setattr(files_mod, "RENDER_MODES", (("auto", SafeSixelImage),) + files_mod.RENDER_MODES[1:])
    _set_cell_size(monkeypatch, 7, 17)
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_files(pilot)
        await open_file(pilot, "pred_0.png")
        await settle(pilot, 8)
        for zoom in ("", "plus"):  # fitted, then zoomed
            if zoom:
                await pilot.press(zoom)
                await settle(pilot, 6)
            image = app.screen.image_widget
            assert isinstance(image, SafeSixelImage)
            impl = image.query_one(_SafeSixelImpl)
            region = app.screen.find_widget(impl).visible_region
            assert impl.region == image.region  # fills the (centred) widget
            lines = impl.render_lines(Region(0, 0, region.width, region.height))
            assert len(lines) == region.height
            assert all(line.cell_length == region.width for line in lines)  # every row cleared
            assert all(line.text.strip() == "" for line in lines[:-1])
            segments = [s.text for s in lines[-1] if "\x1b" in s.text]
            assert segments[0] == f"\x1b[{region.y + 1};{region.x + 1}H"  # its own top-left cell
            assert not segments[1].endswith("-\x1b\\")
            assert segments[-1] == f"\x1b[{region.bottom};{region.right + 1}H"  # its own LAST row
            px_w, px_h = map(int, re.search(r'"1;1;(\d+);(\d+)', segments[1]).groups())
            assert px_w == region.width * 7 and px_h % 6 == 0 and region.height * 17 - 6 < px_h <= region.height * 17


async def test_sixel_widget_really_renders_at_the_measured_cell_size(make_app, monkeypatch):
    from textual_image.widget import SixelImage
    from textual_image.widget import sixel as sixel_mod
    from wandb_cli_tool.screens import files as files_mod

    monkeypatch.setattr(files_mod, "RENDER_MODES", (("auto", SixelImage),) + files_mod.RENDER_MODES[1:])
    _set_cell_size(monkeypatch, 7, 17)
    encoded = []
    original = sixel_mod._ImageSixelImpl._image_to_sixels

    def spy(self, image, options=None, background=None):
        encoded.append((image.size, tuple(self.content_size)))
        return original(self, image, options, background)

    monkeypatch.setattr(sixel_mod._ImageSixelImpl, "_image_to_sixels", spy)
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_files(pilot)
        await open_file(pilot, "pred_0.png")
        await settle(pilot, 8)
        image = app.screen.image_widget
        assert isinstance(image, SixelImage)
        # the dedicated widget has its inner child, and that child was actually rendered
        assert len(image.query(sixel_mod._ImageSixelImpl)) == 1
        assert encoded, "the sixel widget never rendered, so the area would stay blank"
        (px_w, px_h), (cells_w, cells_h) = encoded[-1]
        assert (px_w, px_h) == (cells_w * 7, cells_h * 17)  # fills exactly the area reserved for it
        assert "cell 7×17px" in screen_text(app) or "low resolution" in screen_text(app)


def test_in_a_sixel_terminal_auto_mode_is_the_dedicated_sixel_widget():
    """The real guard for the 'completely dark image' bug: import the module in a pty that
    answers the sixel query, and check which widget class 'auto' mode ends up using."""
    import fcntl, os, pty, select, struct, sys, termios, time

    code = (
        "import wandb_cli_tool.screens.files as f; from textual_image.widget import SixelImage;"
        "c = f.RENDER_MODES[0][1];"
        "print('RESULT', issubclass(c, SixelImage), f.DETECTED, flush=True)"
    )
    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 840, 680))
        os.execv(sys.executable, [sys.executable, "-W", "ignore", "-c", code])
    out, answered, deadline = b"", False, time.time() + 30
    while time.time() < deadline and b"RESULT" not in out:
        if select.select([fd], [], [], 0.05)[0]:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            out += chunk
            if not answered and b"\x1b[c" in out:
                os.write(fd, b"\x1b[?1;2;4c")  # "I support sixel"
                answered = True
    os.close(fd)
    os.waitpid(pid, 0)
    line = next(l for l in out.decode(errors="replace").splitlines() if "RESULT" in l)
    _, is_sixel, detected = line.split()[-3:]
    assert detected == "sixel"
    assert is_sixel == "True"  # AutoImage would not be the sixel widget (blank area)


async def test_a_clip_that_ends_by_itself_turns_the_key_back_to_play(make_app):  # (it says Pause while playing)
    player = FakePlayer()
    app = make(player)
    async with app.run_test(size=SIZE) as pilot:
        await to_files(pilot)
        await open_file(pilot, "sample.wav")
        await pilot.press("space")
        await settle(pilot)
        assert "space Pause" in screen_text(app)
        player.playing = False  # the clip finished on its own
        for _ in range(20):
            await pilot.pause(0.1)
            if "space Play" in screen_text(app):
                break
        assert "space Play" in screen_text(app)


async def test_the_render_mode_key_shows_the_mode_in_use(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_files(pilot)
        await open_file(pilot, "pred_0.png")
        assert "m Render mode: auto" in screen_text(app)
        await pilot.press("m")
        await settle(pilot, 6)
        assert "m Render mode: half-cell" in screen_text(app)
