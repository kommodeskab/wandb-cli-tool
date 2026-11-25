from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static, Tree
from textual.widgets.tree import TreeNode

from ..api import RunRef
from ..plain import plain

if TYPE_CHECKING:
    from ..app import WandbCliApp

MAX_CHILDREN = 200  # per node, so a huge list can't freeze the UI
MAX_VALUE_CHARS = 300


def _value_text(value: Any) -> str:
    text = str(value)
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "…"


def _is_container(value: Any) -> bool:
    return isinstance(value, (dict, list, tuple)) and len(value) > 0


def populate(node: TreeNode, value: Any, depth: int = 0) -> None:
    """Add `value` under `node`: dicts and lists become branches, the rest leaves."""
    if isinstance(value, dict):
        entries = [(str(k), v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))]
    else:
        entries = [(f"[{i}]", v) for i, v in enumerate(value)]
    for key, child in entries[:MAX_CHILDREN]:
        if _is_container(child):
            kind = "{}" if isinstance(child, dict) else "[]"
            label = Text.assemble((key, "bold"), (f"  {kind} {len(child)}", "dim"))
            populate(node.add(label, expand=depth < 1), child, depth + 1)
        else:
            shown = "{}" if child == {} else "[]" if child in ([], ()) else _value_text(child)
            node.add_leaf(Text.assemble((key, "bold"), ": ", shown))
    if len(entries) > MAX_CHILDREN:
        node.add_leaf(Text(f"… {len(entries) - MAX_CHILDREN} more", style="dim"))


class ConfigPane(Vertical):
    """The run's config as an expandable tree (↑/↓ move, Enter/Space expand)."""

    DEFAULT_CSS = """
    ConfigPane { height: 1fr; }
    ConfigPane #status { color: $text-muted; padding: 0 1; }
    ConfigPane #config { height: 1fr; }
    """
    app: "WandbCliApp"

    def __init__(self, ref: RunRef, name: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.ref = ref
        self.run_name = name
        self._started = False

    def compose(self) -> ComposeResult:
        yield Static("Loading…", id="status")
        yield Tree("config", id="config")

    def activate(self) -> None:
        if not self._started:
            self._started = True
            self.load()

    @work(thread=True, exclusive=True)
    def load(self) -> None:
        try:
            config = self.app.client.run_config(self.ref)
        except Exception as exc:
            self.app.call_from_thread(self._status, f"Error: {exc}")
            return
        self.app.call_from_thread(self._show, config)

    def _status(self, text: str) -> None:
        self.query_one("#status", Static).update(plain(text))

    def _show(self, config: dict[str, Any]) -> None:
        tree = self.query_one("#config", Tree)
        tree.clear()
        tree.root.expand()
        if config:
            populate(tree.root, config)
            self._status(f"{len(config)} config entries")
            tree.move_cursor(tree.root.children[0] if tree.root.children else tree.root)
        else:
            self._status("This run has no config.")
