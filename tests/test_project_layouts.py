"""Chart layouts belong to the PROJECT: a change made in one run shows in every run of it. Runs
differ in what they logged, so shared charts must cope with metrics, x-axes, tables and images
that a particular run does not have."""

import copy
import json
from dataclasses import replace

import pytest
from textual.widgets import DataTable, OptionList, Select

from chart_helpers import add_bar, add_images, add_plot, add_table, new_panel, settle, start_add
from conftest import FakeClient, FakePlayer
from test_run_detail_screens import screen_text
from test_run_screens import panel_labels
from wandb_cli_tool import config
from wandb_cli_tool.api import RunInfo
from wandb_cli_tool.app import WandbCliApp
from wandb_cli_tool.charts import (
    IMAGE, TABLE, ChartSpec, Customization, apply_customization, build_panels, chart_key,
)
from wandb_cli_tool.config import Credentials
from wandb_cli_tool.layout_store import LayoutStore, project_key
from wandb_cli_tool.preferences import Preferences
from wandb_cli_tool.refresh import PREF_KEY
from wandb_cli_tool.screens.chart_widgets import (
    IMAGE_CHART_HEIGHT, NOTE_HEIGHT, PLOT_HEIGHT, BarView, ChartView, ImageChartView, TableView,
)
from wandb_cli_tool.screens.charts import ChartsPane

SIZE = (130, 50)
FULL = ["acc", "trainer/global_steps", "trainer/loss", "val/loss"]


class Multi(FakeClient):
    """Project alpha has three runs that logged different things; project beta has one."""

    RUNS = {
        ("me", "alpha"): [
            RunInfo("id1", "full", "finished", "2024-05-03T10:00:00", "2024-05-03T09:00:00"),
            RunInfo("id2", "partial", "finished", "2024-05-02T10:00:00", "2024-05-02T09:00:00"),
            RunInfo("id3", "unrelated", "finished", "2024-05-01T10:00:00", "2024-05-01T09:00:00"),
        ],
        ("me", "beta"): [RunInfo("idb", "other-project", "finished", "2024-05-01T10:00:00", "2024-05-01T09:00:00")],
    }
    PER_RUN = {
        "id1": dict(metrics=FULL, system=True, tables=True, images=True),
        "id2": dict(metrics=["acc", "trainer/loss"], system=True, tables=False, images=False),  # no val/loss, no global_steps
        "id3": dict(metrics=["other/z"], system=False, tables=False, images=False),  # nothing in common
        "idb": dict(metrics=FULL, system=True, tables=True, images=True),
    }

    def __init__(self, api_key):
        super().__init__(api_key)
        self.per_run = copy.deepcopy(self.PER_RUN)

    def _of(self, ref):
        return self.per_run[ref.id]

    def run_details(self, ref):
        info = next(r for runs in self.RUNS.values() for r in runs if r.id == ref.id)
        return replace(info, name=self.names.get(ref.id, info.name))

    def metric_keys(self, ref):
        return list(self._of(ref)["metrics"])

    def metric_values(self, ref):
        return {m: self.VALUES.get(m, 1.0) for m in self._of(ref)["metrics"]}

    def system_rows(self, ref):
        return list(self.SYSTEM_ROWS) if self._of(ref)["system"] else []

    def run_tables(self, ref):
        return list(self.TABLES) if self._of(ref)["tables"] else []

    def run_images(self, ref):
        return list(self.IMAGES) if self._of(ref)["images"] else []


async def start():
    config.save(Credentials("k", "me"))
    Preferences().set(PREF_KEY, 0)  # no automatic refresh: the tests refresh by hand
    app = WandbCliApp(client_factory=Multi, audio_player=FakePlayer())
    return app, app.run_test(size=SIZE)


async def pick_row(pilot, row):
    table = pilot.app.screen.query_one("#items", DataTable)
    table.move_cursor(row=row)
    await settle(pilot, 2)
    await pilot.press("enter")
    await settle(pilot, 5)


async def open_run_charts(pilot, project=0, run=0):
    """From the projects list: project -> run -> its Charts tab."""
    await settle(pilot, 4)
    await pick_row(pilot, project)
    await pick_row(pilot, run)
    await pilot.press("3")  # Charts tab
    await settle(pilot, 8)
    screen = pilot.app.screen.charts_pane
    assert isinstance(screen, ChartsPane)
    return screen


async def back_to_projects(pilot):
    await pilot.press("escape", "escape")
    await settle(pilot, 4)
    assert pilot.app.screen.query("#items")


def panel_index(screen, name):
    return next(i for i, l in enumerate(panel_labels(screen)) if l.startswith(name + " ("))


async def select_panel(pilot, screen, name):
    screen.query_one("#panels", OptionList).highlighted = panel_index(screen, name)
    await settle(pilot, 5)


def saved():
    path = LayoutStore().path
    return json.loads(path.read_text())["projects"] if path.exists() else {}


# ---- the same layout in every run of the project ---------------------------------------------


async def test_a_chart_added_in_one_run_appears_in_every_other_run_of_the_project():
    app, ctx = await start()
    async with ctx as pilot:
        screen = await open_run_charts(pilot, run=0)
        await new_panel(pilot, "mine")
        await add_plot(pilot, (False, "trainer/loss"), (False, "val/loss"))
        assert panel_labels(screen)[-1] == "mine (1)"
        assert list(saved()) == ["me/alpha"]  # one entry for the project, not one per run
        await back_to_projects(pilot)

        screen2 = await open_run_charts(pilot, run=1)  # a different run of the same project
        assert panel_labels(screen2)[-1] == "mine (1)"
        await select_panel(pilot, screen2, "mine")
        (view,) = screen2.query(ChartView)
        assert view.spec.series == ("trainer/loss", "val/loss")
        assert "custom layout" not in screen_text(app) and "shared" not in screen_text(app)  # no remark about it

        await back_to_projects(pilot)
        screen3 = await open_run_charts(pilot, run=2)  # even the run that shares nothing gets the panel
        assert panel_labels(screen3)[-1] == "mine (1)"


async def test_other_projects_are_not_affected():
    app, ctx = await start()
    async with ctx as pilot:
        await open_run_charts(pilot, project=0, run=0)
        await new_panel(pilot, "mine")
        await back_to_projects(pilot)
        other = await open_run_charts(pilot, project=1, run=0)
        assert "mine" not in " ".join(panel_labels(other))  # project beta keeps its own (default) layout
        await new_panel(pilot, "beta-only")
        assert set(saved()) == {"me/alpha", "me/beta"}
        assert saved()["me/alpha"]["panels"] == ["mine"] and saved()["me/beta"]["panels"] == ["beta-only"]


async def test_each_run_still_gets_default_charts_from_its_own_metrics_with_the_changes_on_top():
    app, ctx = await start()
    async with ctx as pilot:
        full = await open_run_charts(pilot, run=0)
        assert panel_labels(full)[:4] == ["System (2)", "trainer (2)", "val (1)", "Other (1)"]
        await new_panel(pilot, "mine")
        await back_to_projects(pilot)
        partial = await open_run_charts(pilot, run=1)
        assert panel_labels(partial) == ["System (2)", "trainer (1)", "Other (1)", "mine (0)"]  # fewer metrics, so fewer charts
        await back_to_projects(pilot)
        unrelated = await open_run_charts(pilot, run=2)
        assert panel_labels(unrelated) == ["System (0)", "other (1)", "mine (0)"]


async def test_deleting_a_default_chart_removes_it_in_every_run_that_has_that_metric():
    app, ctx = await start()
    async with ctx as pilot:
        full = await open_run_charts(pilot, run=0)
        await select_panel(pilot, full, "trainer")
        await pilot.press("right", "down", "delete")  # trainer/loss, the second chart
        await settle(pilot)
        assert " ".join(app.screen.query_one("#confirm-body").render().plain.split()) == "“loss”"
        await pilot.press("y")
        await settle(pilot, 6)
        assert panel_labels(full)[panel_index(full, "trainer")] == "trainer (1)"
        await back_to_projects(pilot)

        partial = await open_run_charts(pilot, run=1)  # it had trainer/loss as its only trainer chart
        assert panel_labels(partial) == ["System (2)", "Other (1)"] or "trainer (0)" in panel_labels(partial)
        assert "trainer/loss" not in {v.spec.y for v in partial.query(ChartView)}
        await back_to_projects(pilot)
        unrelated = await open_run_charts(pilot, run=2)  # irrelevant there, and harmless
        assert panel_labels(unrelated) == ["System (0)", "other (1)"]


async def test_an_x_axis_choice_applies_to_every_run_that_has_the_metric():
    app, ctx = await start()
    async with ctx as pilot:
        full = await open_run_charts(pilot, run=0)
        await select_panel(pilot, full, "trainer")
        await pilot.press("right", "down", "o")  # trainer/loss
        await settle(pilot)
        app.screen.query_one("#x", Select).value = "acc"
        await pilot.click("#ok")
        await settle(pilot, 5)
        assert saved()["me/alpha"]["x"] == {"trainer": {"0:trainer/loss": "acc"}}
        await back_to_projects(pilot)
        partial = await open_run_charts(pilot, run=1)
        await select_panel(pilot, partial, "trainer")
        (view,) = partial.query(ChartView)
        assert view.spec.y == "trainer/loss" and view.spec.x == "acc"


async def test_reset_in_one_run_resets_every_run_of_the_project():
    app, ctx = await start()
    async with ctx as pilot:
        await open_run_charts(pilot, run=0)
        await new_panel(pilot, "mine")
        await back_to_projects(pilot)
        partial = await open_run_charts(pilot, run=1)
        await pilot.press("d")
        await settle(pilot)
        text = " ".join(app.screen.query_one("#confirm-body").render().plain.split())
        assert "alpha" not in text and "ALL" not in text  # no remarks about scope
        await pilot.press("y")
        await settle(pilot, 6)
        assert "mine" not in " ".join(panel_labels(partial)) and saved() == {}
        await back_to_projects(pilot)
        full = await open_run_charts(pilot, run=0)
        assert "mine" not in " ".join(panel_labels(full))


async def test_changing_the_layout_never_announces_its_scope():
    app, ctx = await start()
    async with ctx as pilot:
        await open_run_charts(pilot, run=0)
        await new_panel(pilot, "one")
        await add_bar(pilot, (False, "acc"), (False, "val/loss"))
        assert not [n for n in app._notifications if "every run" in n.message]


# ---- shared charts in runs that lack their data -------------------------------------------------


async def share_then_open(pilot, add, run):
    """Add a chart in the full run (via `add`), then open run index `run` of the same project."""
    screen = await open_run_charts(pilot, run=0)
    await new_panel(pilot, "mine")
    await add(pilot)
    await back_to_projects(pilot)
    pilot.app.client.series_calls.clear()  # only what the second run asks for is of interest
    screen = await open_run_charts(pilot, run=run)
    await select_panel(pilot, screen, "mine")
    return screen


async def test_a_plot_with_some_metrics_missing_draws_the_rest_and_never_requests_the_missing(monkeypatch):
    app, ctx = await start()
    async with ctx as pilot:
        screen = await share_then_open(pilot, lambda p: add_plot(p, (False, "trainer/loss"), (False, "val/loss")), run=1)
        (view,) = screen.query(ChartView)
        assert view.spec.series == ("trainer/loss", "val/loss") and view.note is None  # a real plot, not a note
        assert "not logged in this run: val/loss" in str(view.border_subtitle)
        asked = {y for y, _x in app.client.series_calls}
        assert asked == {"trainer/loss"}  # val/loss was never requested for a run that cannot have it
        assert view.outer_size.height == PLOT_HEIGHT


async def test_a_plot_where_no_metric_exists_collapses_to_a_short_note():
    app, ctx = await start()
    async with ctx as pilot:
        screen = await open_run_charts(pilot, run=0)
        await new_panel(pilot, "mine")
        await add_plot(pilot, (False, "trainer/loss"), (False, "val/loss"))
        await back_to_projects(pilot)
        app.client.series_calls.clear()
        screen = await open_run_charts(pilot, run=2)  # "unrelated"
        await select_panel(pilot, screen, "mine")
        (view,) = screen.query(ChartView)
        assert view.note == "Not logged in this run: trainer/loss, val/loss"
        assert view.outer_size.height == NOTE_HEIGHT
        assert app.client.series_calls == []  # nothing was requested for metrics that cannot exist
        assert "Not logged in this run: trainer/loss, val/loss" in screen_text(app)
        # it is still a normal chart for editing purposes
        view.focus()
        await settle(pilot)
        await pilot.press("delete")
        await settle(pilot)
        assert app.screen.query_one("#confirm-body")


async def test_an_x_axis_the_run_never_logged_is_explained_instead_of_drawing_nothing():
    app, ctx = await start()
    async with ctx as pilot:
        screen = await share_then_open(pilot, lambda p: add_plot(p, (False, "acc"), x="trainer/global_steps"), run=1)
        (view,) = screen.query(ChartView)
        assert view.note == "The x-axis “trainer/global_steps” was not logged in this run."
        assert view.spec.x == "trainer/global_steps"  # the shared setting itself is untouched
        assert app.client.series_calls == []


async def test_a_system_plot_in_a_run_without_system_metrics_says_so():
    app, ctx = await start()
    async with ctx as pilot:
        screen = await share_then_open(pilot, lambda p: add_plot(p, (True, "system.cpu")), run=2)
        (view,) = screen.query(ChartView)
        assert view.note == "Not logged in this run: system.cpu" and view.outer_size.height == NOTE_HEIGHT


async def test_a_bar_chart_shows_missing_values_as_a_dash_and_collapses_when_all_are_missing():
    app, ctx = await start()
    async with ctx as pilot:
        screen = await share_then_open(pilot, lambda p: add_bar(p, (False, "acc"), (False, "val/loss")), run=1)
        (bar,) = screen.query(BarView)
        assert bar.items == [("acc", 0.9), ("val/loss", None)] and bar.note is None
        assert "—" in screen_text(app)
        await back_to_projects(pilot)
        screen = await open_run_charts(pilot, run=2)
        await select_panel(pilot, screen, "mine")
        (bar,) = screen.query(BarView)
        assert bar.note == "None of these metrics were logged in this run." and bar.outer_size.height == NOTE_HEIGHT


async def test_tables_and_images_missing_from_a_run_are_notes_not_errors_and_cost_nothing():
    app, ctx = await start()

    async def add(p):
        await add_table(p, "preds")
        await add_images(p, "samples")

    async with ctx as pilot:
        screen = await open_run_charts(pilot, run=0)
        await new_panel(pilot, "mine")
        await add(pilot)
        await pilot.pause(0.3)
        await back_to_projects(pilot)
        app.client.table_calls.clear()
        app.client.image_step_calls.clear()
        screen = await open_run_charts(pilot, run=1)  # logged no tables and no images
        await select_panel(pilot, screen, "mine")
        table, images = screen.query(TableView).first(), screen.query(ImageChartView).first()
        assert [str(c.label) for c in table.columns.values()] == ["Not in this run"]
        assert "no logged table called “preds”" in screen_text(app)
        assert images.unavailable == "This run has no logged images called “samples”." and images.outer_size.height == NOTE_HEIGHT
        assert app.client.table_calls == [] and app.client.image_step_calls == []


async def test_when_the_run_later_logs_the_missing_data_a_refresh_brings_the_chart_to_life():
    app, ctx = await start()
    async with ctx as pilot:
        screen = await open_run_charts(pilot, run=0)
        await new_panel(pilot, "mine")
        await add_plot(pilot, (False, "val/loss"))
        await add_images(pilot, "samples")
        await pilot.pause(0.3)
        await back_to_projects(pilot)
        screen = await open_run_charts(pilot, run=1)  # has neither val/loss nor images yet
        await select_panel(pilot, screen, "mine")
        plot, images = screen.query(ChartView).first(), screen.query(ImageChartView).first()
        assert plot.note and images.unavailable
        app.client.per_run["id2"]["metrics"].append("val/loss")  # the run logs them in the meantime
        app.client.per_run["id2"]["images"] = True
        await pilot.press("r")
        await pilot.pause(0.6)
        await settle(pilot, 6)
        assert plot.note is None and plot.outer_size.height == PLOT_HEIGHT
        assert images.unavailable is None and images.outer_size.height == IMAGE_CHART_HEIGHT and images.series is not None


async def test_a_shared_panel_name_that_a_run_also_generates_merges_with_it():
    app, ctx = await start()
    async with ctx as pilot:
        await open_run_charts(pilot, run=2)  # "unrelated" has no "val" panel
        await new_panel(pilot, "val")
        await add_plot(pilot, (False, "other/z"))
        await back_to_projects(pilot)
        full = await open_run_charts(pilot, run=0)  # has a generated "val" panel with val/loss
        assert panel_labels(full)[panel_index(full, "val")] == "val (2)"  # its own chart + the shared one
        assert panel_labels(full).count("val (2)") == 1  # one panel, not two


# ---- the stored data -------------------------------------------------------------------------------


def test_customizations_for_things_a_run_does_not_have_are_harmless():
    default = build_panels(["a/x", "b/y"], [])
    custom = Customization(
        panels=["mine"],
        added={"gone-panel": [ChartSpec("nope")]},
        x={"nope-panel": {"0:nope": "acc"}, "a": {"0:not-here": "acc"}},
        removed={"nope-panel": ["0:nope"], "a": ["1:no-such-system-metric"]},
    )
    panels = {p.name: p for p in apply_customization(default, custom)}
    assert [c.y for c in panels["a"].charts] == ["a/x"] and [c.y for c in panels["b"].charts] == ["b/y"]
    assert [c.y for c in panels["gone-panel"].charts] == ["nope"]  # shared charts are kept even if no run here has them
    assert "mine" in panels


async def test_layout_files_from_when_layouts_were_per_run_are_ignored_and_replaced():
    config.save(Credentials("k", "me"))
    path = LayoutStore().path
    path.parent.mkdir(parents=True, exist_ok=True)
    old = Customization(panels=["old-panel"]).to_json()
    path.write_text(json.dumps({"version": 1, "runs": {"me/alpha/id1": old}}))
    app, ctx = await start()
    async with ctx as pilot:
        screen = await open_run_charts(pilot, run=0)
        assert "old-panel" not in " ".join(panel_labels(screen))  # the development-time reset
        await new_panel(pilot, "fresh")
    data = json.loads(path.read_text())
    assert data["version"] == 2 and list(data["projects"]) == ["me/alpha"]
