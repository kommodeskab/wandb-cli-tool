"""Stopping and deleting runs: one run (run screen) and many (run list)."""

from textual.widgets import Button, DataTable, Input, OptionList, Select

from conftest import FakeClient
from wandb_cli_tool import config
from wandb_cli_tool.app import WandbCliApp
from wandb_cli_tool.config import Credentials
from wandb_cli_tool.screens.dialogs import (
    ConfirmDialog, GroupDialog, RunActionsDialog, TagsDialog, TypeToConfirmDialog,
)
from wandb_cli_tool.screens.run import RunScreen
from wandb_cli_tool.screens.runs import RunsScreen


async def settle(pilot, n=4):
    for _ in range(n):
        await pilot.pause()


def make():
    config.save(Credentials("k", "me"))
    return WandbCliApp(client_factory=FakeClient)


async def open_runs(pilot):
    await settle(pilot, 4)
    await pilot.press("enter")
    await settle(pilot, 5)
    assert isinstance(pilot.app.screen, RunsScreen)
    return pilot.app.screen


async def open_run(pilot, row=0):
    screen = await open_runs(pilot)
    await pilot.press(*["down"] * row, "enter")
    await settle(pilot, 6)
    assert isinstance(pilot.app.screen, RunScreen)
    return pilot.app.screen


async def choose(pilot, option_id):
    """Pick an entry of the actions dialog by its id."""
    options = pilot.app.screen.query_one(OptionList)
    options.highlighted = options.get_option_index(option_id)
    await settle(pilot, 2)
    await pilot.press("enter")
    await settle(pilot)


def keys(screen):
    from wandb_cli_tool.screens.keybar import KeyBar
    return screen.query_one(KeyBar).query_one("KeyLine").render().plain.replace("\xa0", " ")


def names(screen):
    table = screen.query_one("#items", DataTable)
    return [str(table.get_row_at(i)[0]) for i in range(table.row_count)]


# ---- stop ---------------------------------------------------------------------------------
async def test_stop_is_only_offered_for_a_running_run_and_asks_first():
    app = make()
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_run(pilot, 0)  # run-1 is running
        assert "Stop run" in keys(screen)
        await pilot.press("s")
        await settle(pilot)
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("n")
        await settle(pilot)
        assert app.client.writes == []  # declined: nothing sent
        await pilot.press("s", "y")
        await settle(pilot, 6)
        assert app.client.writes == [("stop", "id1")]
        assert any("Stop requested" in n.message for n in app._notifications)
        assert ("me", "alpha") in app.touched_projects


async def test_a_finished_run_cannot_be_stopped():
    app = make()
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_run(pilot, 1)  # run-2 finished
        assert "Stop run" not in keys(screen) and screen.check_action("stop_run", ()) is False
        await pilot.press("s")
        await settle(pilot)
        assert app.screen is screen and app.client.writes == []


# ---- delete -------------------------------------------------------------------------------
async def test_delete_needs_the_word_typed_and_then_leaves_the_run():
    app = make()
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_run(pilot, 1)
        await pilot.press("delete")
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, TypeToConfirmDialog)
        ok = dialog.query_one("#ok", Button)
        assert ok.disabled
        await pilot.press("enter")  # Enter alone does nothing
        await settle(pilot)
        assert app.screen is dialog and app.client.writes == []
        await pilot.press(*"delet")
        await settle(pilot)
        assert ok.disabled  # not yet the whole word
        await pilot.press("e")
        await settle(pilot)
        assert not ok.disabled
        await pilot.press("enter")
        await settle(pilot, 8)
        assert app.client.writes == [("delete", "id2")]
        assert isinstance(app.screen, RunsScreen)  # back in the list ...
        assert "run-2" not in names(app.screen)  # ... which no longer has it


async def test_cancelling_the_delete_dialog_deletes_nothing():
    app = make()
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_run(pilot, 1)
        await pilot.press("delete", *"delete", "escape")
        await settle(pilot)
        assert app.screen is screen and app.client.writes == []


async def test_a_failed_delete_keeps_you_in_the_run_and_says_why():
    app = make()
    app.client_factory = lambda key: _failing(FakeClient(key))
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_run(pilot, 1)
        await pilot.press("delete", *"delete", "enter")
        await settle(pilot, 6)
        assert app.screen is screen
        assert any("Could not delete the run: permission denied" in n.message for n in app._notifications)


def _failing(client):
    client.fail_writes = True
    return client


# ---- actions on many runs ----------------------------------------------------------------
async def test_actions_apply_to_the_selection_or_the_run_under_the_cursor():
    app = make()
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_runs(pilot)
        await pilot.press("a")  # nothing selected: just run-1, which is running
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, RunActionsDialog) and (dialog.count, dialog.running) == (1, 1)
        await pilot.press("escape")
        await settle(pilot)
        await pilot.press("space", "down", "down", "space", "a")  # run-1 and run-3
        await settle(pilot)
        assert isinstance(app.screen, RunActionsDialog) and (app.screen.count, app.screen.running) == (2, 1)


async def test_bulk_stop_only_touches_the_running_ones():
    app = make()
    async with app.run_test(size=(130, 40)) as pilot:
        await open_runs(pilot)
        await pilot.press("space", "down", "space", "down", "space", "a")  # all three
        await settle(pilot)
        await choose(pilot, "stop")
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("y")
        await settle(pilot, 8)
        assert app.client.writes == [("stop", "id1")]


async def test_bulk_group_and_tags_write_to_every_selected_run():
    app = make()
    async with app.run_test(size=(130, 45)) as pilot:
        screen = await open_runs(pilot)
        await pilot.press("space", "down", "space", "a")
        await settle(pilot)
        await choose(pilot, "group")
        dialog = app.screen
        assert isinstance(dialog, GroupDialog)
        dialog.query_one("#new-group", Input).value = "team-x"
        await pilot.click("#ok")
        await settle(pilot, 8)
        assert app.client.writes == [("group", "id1", "team-x"), ("group", "id2", "team-x")]
        await pilot.press("a")
        await settle(pilot)
        await choose(pilot, "tags")
        dialog = app.screen
        assert isinstance(dialog, TagsDialog)
        dialog.query_one("#new-tags", Input).value = "new-tag"
        await pilot.click("#ok")
        await settle(pilot, 8)
        assert app.client.writes[2:] == [("tags", "id1", ["new-tag"]), ("tags", "id2", ["new-tag"])]


async def test_bulk_delete_removes_the_runs_and_their_selection():
    app = make()
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_runs(pilot)
        await pilot.press("down", "space", "down", "space", "a")  # run-2 and run-3
        await settle(pilot)
        await choose(pilot, "delete")
        dialog = app.screen
        assert isinstance(dialog, TypeToConfirmDialog) and "run-2" in dialog.body_text and "run-3" in dialog.body_text
        assert not any(w[0] == "delete" for w in app.client.writes)
        await pilot.press(*"delete", "enter")
        await settle(pilot, 10)
        assert sorted(w[1] for w in app.client.writes if w[0] == "delete") == ["id2", "id3"]
        assert names(app.screen) == ["run-1"] and screen.selected == []


async def test_bulk_failures_are_reported_and_the_rest_still_done():
    class Flaky(FakeClient):
        def delete_run(self, ref, delete_artifacts=False):
            if ref.id == "id2":
                raise RuntimeError("locked")
            super().delete_run(ref, delete_artifacts)

    config.save(Credentials("k", "me"))
    app = WandbCliApp(client_factory=Flaky)
    async with app.run_test(size=(130, 40)) as pilot:
        screen = await open_runs(pilot)
        await pilot.press("down", "space", "down", "space", "a")
        await settle(pilot)
        await choose(pilot, "delete")
        await pilot.press(*"delete", "enter")
        await settle(pilot, 10)
        assert [w[1] for w in app.client.writes if w[0] == "delete"] == ["id3"]
        assert any("1 of 2 done" in n.message and "run-2: locked" in n.message for n in app._notifications)
        assert names(app.screen) == ["run-1", "✓ run-2"]  # the failed one stays, still selected


# ---- deleting the artifacts too ------------------------------------------------------------------
from textual.widgets import Checkbox

from wandb_cli_tool.screens.dialogs import DeleteChoice, DeleteRunsDialog


async def test_artifacts_are_kept_unless_the_box_is_ticked_and_a_second_phrase_typed():
    app = make()
    async with app.run_test(size=(130, 45)) as pilot:
        screen = await open_run(pilot, 1)
        await pilot.press("delete")
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, DeleteRunsDialog)
        box = dialog.query_one("#artifacts", Checkbox)
        assert box.value is False  # off by default
        dialog.query_one("#artifacts", Checkbox).value = True
        await settle(pilot)
        assert dialog.phrase == "delete artifacts" and "delete artifacts" in str(dialog.query_one("#phrase-label").render())
        assert "removed from W&B too" in str(dialog.query_one("#artifact-warning").render())
        await pilot.press(*"delete")
        await settle(pilot)
        assert dialog.query_one("#ok", Button).disabled  # the plain word no longer suffices
        await pilot.press(*" artifacts")
        await settle(pilot)
        assert not dialog.query_one("#ok", Button).disabled
        await pilot.press("enter")
        await settle(pilot, 8)
        assert app.client.writes == [("delete+artifacts", "id2")]
        assert any("deleted together with its artifacts" in n.message for n in app._notifications)
        assert isinstance(app.screen, RunsScreen)


async def test_unticking_the_box_goes_back_to_the_plain_phrase():
    app = make()
    async with app.run_test(size=(130, 45)) as pilot:
        await open_run(pilot, 1)
        await pilot.press("delete")
        await settle(pilot)
        dialog = app.screen
        dialog.query_one("#artifacts", Checkbox).value = True
        await settle(pilot)
        await pilot.press(*"delete artifacts")
        await settle(pilot)
        assert not dialog.query_one("#ok", Button).disabled
        dialog.query_one("#artifacts", Checkbox).value = False  # changed their mind
        await settle(pilot)
        assert dialog.phrase == "delete" and dialog.query_one("#ok", Button).disabled  # "delete artifacts" != "delete"
        assert str(dialog.query_one("#artifact-warning").render()) == ""
        await pilot.press("escape")
        await settle(pilot)
        assert app.client.writes == []


async def test_a_plain_delete_still_keeps_the_artifacts():
    app = make()
    async with app.run_test(size=(130, 45)) as pilot:
        await open_run(pilot, 1)
        await pilot.press("delete", *"delete", "enter")
        await settle(pilot, 8)
        assert app.client.writes == [("delete", "id2")]  # no artifacts flag
        assert any(n.message == "Run “run-2” deleted." for n in app._notifications)


async def test_bulk_delete_can_take_the_artifacts_along():
    app = make()
    async with app.run_test(size=(130, 45)) as pilot:
        screen = await open_runs(pilot)
        await pilot.press("down", "space", "down", "space", "a")
        await settle(pilot)
        await choose(pilot, "delete")
        dialog = app.screen
        assert isinstance(dialog, DeleteRunsDialog)
        dialog.query_one("#artifacts", Checkbox).value = True
        await settle(pilot)
        await pilot.press(*"delete artifacts", "enter")
        await settle(pilot, 10)
        assert sorted(app.client.writes) == [("delete+artifacts", "id2"), ("delete+artifacts", "id3")]
        assert names(app.screen) == ["run-1"] and screen.selected == []
        assert any("Delete (+ artifacts): done for 2 runs" in n.message for n in app._notifications)
