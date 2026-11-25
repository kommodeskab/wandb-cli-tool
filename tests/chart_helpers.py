"""Drive the add-chart dialogs the way a user does."""

from textual.widgets import Input, OptionList, Select, SelectionList

from wandb_cli_tool.screens.chart_dialogs import BarDialog, ChartKindDialog, ImageDialog, PlotDialog, TableDialog

KIND_KEYS = {"plot": [], "bar": ["down"], "table": ["down", "down"], "image": ["down", "down", "down"]}


async def settle(pilot, n=4):
    for _ in range(n):
        await pilot.pause()


async def start_add(pilot, kind):
    """Press `a` and choose a kind; returns the dialog for that kind."""
    await pilot.press("a")
    await settle(pilot)
    assert isinstance(pilot.app.screen, ChartKindDialog)
    await pilot.press(*KIND_KEYS[kind], "enter")
    await settle(pilot)
    return pilot.app.screen


async def pick(pilot, dialog, *values):
    """Tick values one at a time, so the order they are picked in is the order recorded."""
    choices = dialog.query_one("#choices", SelectionList)
    for value in values:
        choices.select(value)
        await settle(pilot, 2)


async def add_plot(pilot, *values, x=None):
    """values are (is_system, name) tuples."""
    dialog = await start_add(pilot, "plot")
    assert isinstance(dialog, PlotDialog)
    await pick(pilot, dialog, *values)
    if x is not None:
        dialog.query_one("#x", Select).value = x
    await pilot.click("#ok")
    await settle(pilot, 5)


async def add_bar(pilot, *values):
    dialog = await start_add(pilot, "bar")
    assert isinstance(dialog, BarDialog)
    await pick(pilot, dialog, *values)
    await pilot.click("#ok")
    await settle(pilot, 5)


async def add_table(pilot, key):
    dialog = await start_add(pilot, "table")
    assert isinstance(dialog, TableDialog)
    dialog.query_one("#table", Select).value = key
    await pilot.click("#ok")
    await settle(pilot, 5)


async def add_images(pilot, key):
    dialog = await start_add(pilot, "image")
    assert isinstance(dialog, ImageDialog)
    dialog.query_one("#image-key", Select).value = key
    await pilot.click("#ok")
    await settle(pilot, 6)


async def new_panel(pilot, name):
    await pilot.press("n")
    await settle(pilot)
    pilot.app.screen.query_one(Input).value = name
    await pilot.press("enter")
    await settle(pilot, 5)


def plot_spy(monkeypatch):
    """Record the keyword arguments (label, color, ...) of every plot() call of every chart."""
    from textual_plotext.plot import Plot

    calls, original = [], Plot.plot

    def spy(self, *args, **kwargs):
        calls.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Plot, "plot", spy)
    return calls
