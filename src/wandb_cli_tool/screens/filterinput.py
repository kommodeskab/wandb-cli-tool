"""The text box that filters a table: Down moves from it into the table's rows (the screen decides
which table), so a filter can be typed and the results navigated without reaching for Enter."""

from __future__ import annotations

from textual.binding import Binding
from textual.widgets import Input


class FilterInput(Input):
    BINDINGS = [Binding("down", "screen.focus_results", "Results", show=False)]
