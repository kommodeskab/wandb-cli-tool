"""Full-screen viewer for plots, bar charts, and tables."""

from __future__ import annotations

from typing import Callable, Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Header, Static

from ..plain import plain
from .keybar import KeyBar


class ChartViewerScreen(Screen):
    """Full-screen viewer for a single plot, bar chart, or table (or its comparison view)."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]
    DEFAULT_CSS = """
    ChartViewerScreen #viewer-legend { padding: 0 1; height: auto; }
    ChartViewerScreen #viewer-status { padding: 0 1; color: $text-muted; height: auto; }
    ChartViewerScreen #viewer-body { height: 1fr; width: 1fr; }
    ChartViewerScreen #viewer-body .viewer-full,
    ChartViewerScreen #viewer-body .viewer-full:focus { border: none; margin: 0; width: 1fr; height: 1fr; }
    ChartViewerScreen #viewer-body .viewer-bars,
    ChartViewerScreen #viewer-body .viewer-bars:focus { border: none; margin: 0; width: 1fr; padding: 1 2; }
    """

    def __init__(
        self,
        widget: Widget,
        title: str,
        *,
        subtitle: str = "",
        status: str = "",
        legend: Sequence[tuple[str, str]] = (),
        scrollable: bool = False,
        focus_widget: bool = False,
        populate: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.viewer_widget = widget
        self.viewer_title = title
        self.viewer_subtitle = subtitle
        self._initial_status = status
        self._legend = list(legend)
        self._scrollable = scrollable
        self._focus_widget = focus_widget
        self._populate = populate

    def compose(self) -> ComposeResult:
        yield Header()
        if self._legend:
            yield Static("", id="viewer-legend")
        yield Static("", id="viewer-status")
        container_cls = VerticalScroll if self._scrollable else Vertical
        with container_cls(id="viewer-body"):
            yield self.viewer_widget
        yield KeyBar()

    def on_mount(self) -> None:
        self.title = self.viewer_title
        self.sub_title = self.viewer_subtitle
        if self._legend:
            legend = Text()
            for label, color in self._legend:
                if legend:
                    legend.append("\n")
                legend.append("■ ", style=color)
                legend.append(label)
            self.query_one("#viewer-legend", Static).update(legend)
        if self._populate is not None:
            self._populate()
        status_text = self._initial_status or str(self.viewer_widget.border_subtitle or "")
        self.set_status(status_text)
        if self._focus_widget:
            self.viewer_widget.focus()
        elif self._scrollable:
            self.query_one("#viewer-body", VerticalScroll).focus()
        self.refresh_bindings()

    def set_status(self, text: str) -> None:
        bar = self.query_one("#viewer-status", Static)
        bar.display = bool(text)
        bar.update(plain(text))
