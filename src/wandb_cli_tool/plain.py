"""Show text exactly as it is.

Textual parses plain `str` content as markup, so anything with square brackets is mangled
(`run [v2]` loses its `[v2]`) and a stray closing tag (`[/x]`) raises MarkupError and
crashes the whole app. Run names, tags, groups, metric names, table cells and server error
messages are arbitrary text, so every widget that shows them gets `plain(...)` instead of a str.
"""

from __future__ import annotations

from rich.text import Text
from textual.content import Content


def plain(text: object) -> Text:
    """For Static/Label content, DataTable cells, Option/Selection/Select labels."""
    return Text(str(text))


def plain_title(text: object) -> Content:
    """For `border_title` / `border_subtitle`, which want Content."""
    return Content(str(text))
