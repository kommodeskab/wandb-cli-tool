"""Textual parses plain `str` content as markup, so a run name, tag, metric name or error message
like "[/x]" crashes the whole app (see plain.py). This scans the screens' source so a new widget
cannot reintroduce the bug unnoticed: text that is not a literal must go through `plain()` /
`Text` / `Content`, or the widget must be created with `markup=False`."""

import ast
from pathlib import Path

SCREENS = sorted((Path(__file__).parent.parent / "src" / "wandb_cli_tool" / "screens").glob("*.py"))
SAFE_WRAPPERS = {"plain", "plain_title", "Text", "Content", "Table", "grid"}


def call_name(node: ast.Call) -> str:
    f = node.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def safe_names(tree: ast.AST) -> set[str]:
    """Variables that are assigned a Rich object (`tags = Text(...)`), which is never parsed as markup."""
    return {
        t.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and call_name(n.value) in SAFE_WRAPPERS
        for t in n.targets
        if isinstance(t, ast.Name)
    }


def is_safe(arg: ast.expr, names: frozenset | set = frozenset()) -> bool:
    if isinstance(arg, ast.Constant):  # a literal written in the source is not untrusted
        return True
    if isinstance(arg, ast.Name) and arg.id in names:
        return True
    return isinstance(arg, ast.Call) and call_name(arg) in SAFE_WRAPPERS


def has_markup_off(call: ast.Call) -> bool:
    return any(k.arg == "markup" and isinstance(k.value, ast.Constant) and k.value.value is False for k in call.keywords)


def is_static_update(call: ast.Call) -> bool:
    """`self.query_one("#x", Static).update(...)`: the receiver is a Static lookup."""
    f = call.func
    if not (isinstance(f, ast.Attribute) and f.attr == "update" and isinstance(f.value, ast.Call)):
        return False
    inner = f.value
    return call_name(inner) == "query_one" and any(isinstance(a, ast.Name) and a.id == "Static" for a in inner.args)


def violations() -> list[str]:
    found = []
    for path in SCREENS:
        tree = ast.parse(path.read_text())
        names = safe_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            where = f"{path.name}:{node.lineno}"
            if call_name(node) in ("Static", "Label") and not has_markup_off(node) and not is_safe(node.args[0], names):
                found.append(f"{where}: {call_name(node)}(...) with non-literal text; wrap it in plain() or pass markup=False")
            if is_static_update(node) and not is_safe(node.args[0], names):
                found.append(f"{where}: Static.update(...) with non-literal text; wrap it in plain()")
    return found


def test_widgets_never_get_untrusted_text_as_a_plain_string():
    assert violations() == []


def test_the_scanner_actually_catches_the_unsafe_patterns(tmp_path, monkeypatch):
    """Guard against the guard silently matching nothing."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "class S:\n"
        "    def f(self, name):\n"
        "        Static(f'hello {name}')\n"
        "        Label(name)\n"
        "        self.query_one('#x', Static).update(name)\n"
        "        Static('literal'); Static(plain(name)); Label(name, markup=False)\n"
        "        self.query_one('#x', Static).update(plain(name))\n"
    )
    monkeypatch.setitem(globals(), "SCREENS", [bad])
    found = violations()
    assert [v.split(":")[1] for v in found] == ["3", "4", "5"]  # exactly the three unsafe lines


def test_no_screen_calls_the_super_class_handler_of_a_message():
    """Textual runs a message handler of EVERY class in the hierarchy, so `super().on_button_pressed(...)`
    runs the base handler twice (a dialog then dismissed two screens). Use `event.prevent_default()`
    to stop the base class from handling an event instead."""
    bad = []
    for path in SCREENS:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr.startswith("on_"):
                if isinstance(node.func.value, ast.Call) and call_name(node.func.value) == "super":
                    bad.append(f"{path.name}:{node.lineno}")
    assert bad == []
