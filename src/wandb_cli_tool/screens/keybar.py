"""A footer that wraps onto more lines instead of scrolling sideways when the keys do not fit.

It lists the same bindings Textual's Footer would (the active, `show=True` ones), so what a screen
shows is decided by its bindings and its `check_action` (return False to hide a key that does not
apply right now).

A key that switches something shows what it is set to. The node that owns the binding (a screen or a
widget) can define either or both of:
  toggle_state(action) -> bool | None   an on/off key: shown as "☑ label" / "☐ label" (None: not a toggle)
  key_label(action)    -> str | None    a key that cycles a value: the label to show instead, e.g. "Auto-refresh: 30 s"
and must call `refresh_bindings()` whenever the answer changes."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Footer, Static

NBSP = " "  # keeps "key label" together when the line wraps
MAX_LINES = 3


class KeyLine(Static):
    DEFAULT_CSS = "KeyLine { width: 1fr; height: auto; padding: 0 1; }"


class KeyBar(Footer):
    DEFAULT_CSS = """
    KeyBar { layout: vertical; height: auto; max-height: 3; overflow: hidden; }
    """

    def _colors(self) -> tuple[str, str]:
        try:
            css = self.app.get_css_variables()
            return css.get("footer-key-foreground", "yellow"), css.get("footer-description-foreground", "white")
        except Exception:
            return "yellow", "white"

    def compose(self) -> ComposeResult:
        if not self._bindings_ready:
            return
        key_color, label_color = self._colors()
        seen: set[str] = set()
        text = Text()
        for node, binding, enabled, _ in self.screen.active_bindings.values():
            if not binding.show or binding.action in seen:
                continue
            seen.add(binding.action)
            if text:
                text.append("  ")
            style = "" if enabled else "dim"
            label = binding.description
            custom = getattr(node, "key_label", None)
            if callable(custom):
                label = custom(binding.action) or label
            state = getattr(node, "toggle_state", None)
            on = state(binding.action) if callable(state) else None
            if on is not None:
                label = ("☑ " if on else "☐ ") + label
            text.append(self.app.get_key_display(binding).replace(" ", NBSP), style=f"bold {key_color} {style}".strip())
            text.append(NBSP + label.replace(" ", NBSP), style=f"{label_color} {style}".strip())
        yield KeyLine(text)
