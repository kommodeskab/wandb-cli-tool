"""A key binding whose action method does not exist does nothing, silently (it once happened when
an edit removed `action_open_in_browser` but left its `o` binding). Check every binding of every screen
and widget of the app."""

import importlib
import inspect
import pkgutil

from textual.binding import Binding
from textual.dom import DOMNode

import wandb_cli_tool.screens as screens_pkg


def all_classes():
    for info in pkgutil.iter_modules(screens_pkg.__path__):
        module = importlib.import_module(f"wandb_cli_tool.screens.{info.name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, DOMNode) and cls.__module__ == module.__name__:
                yield cls
    from wandb_cli_tool.app import WandbCliApp
    yield WandbCliApp


def test_every_binding_has_an_action_to_run():
    missing = []
    for cls in all_classes():
        for entry in cls.__dict__.get("BINDINGS", []):
            binding = entry if isinstance(entry, Binding) else Binding(*entry)
            action = binding.action
            if "." in action.split("(")[0]:  # "app.quit", "screen.focus_chart": handled elsewhere
                target, _, name = action.split("(")[0].partition(".")
                if target == "app":
                    from wandb_cli_tool.app import WandbCliApp
                    cls_to_check, method = WandbCliApp, f"action_{name}"
                else:
                    continue  # "screen.x" is resolved on whichever screen is showing: checked there
            else:
                cls_to_check, method = cls, f"action_{action.split('(')[0]}"
            if not hasattr(cls_to_check, method) and not method.startswith(("action_quit", "action_pop_screen")):
                missing.append(f"{cls.__name__}: {binding.key} -> {action}")
    assert missing == []


def test_widget_bindings_that_call_the_screen_exist_on_every_screen_that_shows_them():
    """`screen.focus_chart` and `screen.focus_panels` (chart widgets) need those actions on the screens."""
    from wandb_cli_tool.screens.charts import ChartsScreen
    from wandb_cli_tool.screens.compare import CompareScreen

    for screen in (ChartsScreen, CompareScreen):
        for action in ("focus_chart", "focus_panels", "cycle_focus"):
            assert hasattr(screen, f"action_{action}"), (screen.__name__, action)
