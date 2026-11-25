from textual.widgets import Button

from test_run_detail_screens import SIZE, make, screen_text, settle, to_run
from wandb_cli_tool.api import CopyEstimate
from wandb_cli_tool.screens.copy import CopyProgressScreen, describe_copy
from wandb_cli_tool.screens.dialogs import ConfirmDialog
from wandb_cli_tool.screens.run import RunScreen

STARTED = {"event": "started", "run_id": "newid", "name": "run-1 (copy)", "url": "https://wandb.ai/me/alpha/runs/newid"}


async def open_confirm(pilot, index=0):
    await to_run(pilot, index)
    await pilot.press("c")
    await settle(pilot, 6)
    assert isinstance(pilot.app.screen, ConfirmDialog)


async def test_confirmation_states_what_will_be_uploaded_and_what_will_not(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await open_confirm(pilot)
        text = screen_text(app)
        for expected in (
            "Copy run “run-1” to a NEW run “run-1 (copy)” in the same project?",
            "1,200 metric history rows", "5 files, 3.0 MB", "2 artifacts", "2.0 GB",
            "Not copied: system metrics", "timestamps of history rows", "not duplicated. The original run is not changed",
        ):
            assert expected in " ".join(text.split()), expected
        assert "still running" in text  # run-1 is running: the copy would be partial
        assert app.screen.query_one("#no", Button).has_focus  # a stray Enter must not confirm


async def test_a_finished_run_has_no_still_running_warning(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await open_confirm(pilot, 1)  # run-2 is finished
        assert "Copy run “run-2”" in screen_text(app) and "still running" not in screen_text(app)


async def test_no_enter_n_and_escape_never_start_a_copy(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await open_confirm(pilot)
        await pilot.press("enter")  # focused button is "No"
        await settle(pilot)
        assert isinstance(app.screen, RunScreen)
        for key in ("n", "escape"):
            await pilot.press("c")
            await settle(pilot, 6)
            assert isinstance(app.screen, ConfirmDialog)
            await pilot.press(key)
            await settle(pilot)
            assert isinstance(app.screen, RunScreen)
        assert app.client.copies_started == [] and not app.touched_projects


async def test_yes_runs_the_copy_shows_progress_and_the_result(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_run(pilot)
        app.client.job_gate.clear()  # hold the job so progress can be observed
        await pilot.press("c")
        await settle(pilot, 6)
        await pilot.press("y")
        await settle(pilot, 8)
        assert isinstance(app.screen, CopyProgressScreen)
        text = screen_text(app)
        assert "Copying file 3/5: output.log" in text and "files: 2 / 5" in text
        assert "⚠ Artifact big: skipped" in text and "Esc: cancel" in text
        assert [r.id for r in app.client.copies_started] == ["id1"]
        app.client.job_gate.set()
        await settle(pilot, 8)
        text = " ".join(screen_text(app).split())
        assert "Done. New run “run-1 (copy)” (newid)" in text and "https://wandb.ai/me/alpha/runs/newid" in text
        assert "1 warning(s)" in text and "Enter or Esc: close" in text
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, RunScreen)
        assert ("me", "alpha") in app.touched_projects  # the runs list will refresh to show the new run


async def test_escape_while_running_asks_the_copy_to_stop_and_reports_the_partial_run(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_run(pilot)
        app.client.job_gate.clear()
        await pilot.press("c")
        await settle(pilot, 6)
        await pilot.press("y")
        await settle(pilot, 8)
        screen = app.screen
        await pilot.press("escape")
        await settle(pilot, 8)
        assert screen.job.cancelled
        text = " ".join(screen_text(app).split())
        assert "Cancelled. A partial run (newid) was left as a failed run" in text
        assert isinstance(app.screen, CopyProgressScreen)  # stays open until the user closes it
        await pilot.press("escape")
        await settle(pilot)
        assert isinstance(app.screen, RunScreen) and ("me", "alpha") in app.touched_projects


async def test_failures_are_reported_and_a_partial_run_is_mentioned_only_if_one_exists(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_run(pilot)
        app.client.copy_events = [STARTED, {"event": "error", "message": "server exploded", "run_id": "newid"}]
        await pilot.press("c")
        await settle(pilot, 6)
        await pilot.press("y")
        await settle(pilot, 8)
        text = " ".join(screen_text(app).split())
        assert "Copy failed: server exploded" in text and "partial run (newid)" in text
        await pilot.press("enter")
        await settle(pilot)
        assert ("me", "alpha") in app.touched_projects

    app = make()
    async with app.run_test(size=SIZE) as pilot:  # failed before any run was created
        await to_run(pilot)
        app.client.copy_events = [{"event": "error", "message": "not authorised", "run_id": None}]
        await pilot.press("c")
        await settle(pilot, 6)
        await pilot.press("y")
        await settle(pilot, 8)
        assert "Copy failed: not authorised" in screen_text(app) and "partial run" not in screen_text(app)
        await pilot.press("escape")
        await settle(pilot)
        assert not app.touched_projects  # nothing was created, nothing to refresh


async def test_confirmation_still_appears_if_the_size_cannot_be_determined(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_run(pilot)
        app.client.estimate_fails = True
        await pilot.press("c")
        await settle(pilot, 6)
        assert isinstance(app.screen, ConfirmDialog)
        assert "size could not be determined" in " ".join(screen_text(app).split())


async def test_leaving_the_app_screen_cancels_a_running_copy(make_app):
    app = make()
    async with app.run_test(size=SIZE) as pilot:
        await to_run(pilot)
        app.client.job_gate.clear()
        await pilot.press("c")
        await settle(pilot, 6)
        await pilot.press("y")
        await settle(pilot, 8)
        job = app.screen.job
        app.pop_screen()  # e.g. logout or quit while copying
        await settle(pilot)
        assert job.cancelled


def test_describe_copy_without_an_estimate_and_for_unknown_row_counts():
    text = describe_copy("x", "finished", None)
    assert "size could not be determined" in " ".join(text.split()) and "still running" not in text
    assert "• all metric history rows" in describe_copy("x", "finished", CopyEstimate(history_rows=None))
