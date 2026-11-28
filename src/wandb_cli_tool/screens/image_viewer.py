"""Viewport-cropped, zoomable/draggable image widget and full-screen image viewer."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import MouseDown, MouseMove, MouseScrollDown, MouseScrollUp, MouseUp
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Header, Static
from ..images import IMAGE_ERRORS
from ..imageview import ImageViewport, render_crop
from ..plain import plain
from .keybar import KeyBar
from .sixelimage import BEST_IMAGE

DRAG_THROTTLE_S = 0.05  # coalesce rapid mouse-move frames while dragging
SETTLE_DELAY_S = 0.12  # snap to full crispness after drag pauses


def _cell_pixels() -> tuple[int, int]:
    try:
        from textual_image._terminal import get_cell_size

        cell = get_cell_size()
        return max(1, cell.width), max(1, cell.height)
    except Exception:
        return 10, 20


class ZoomableImage(Vertical):
    """Draws an image fitted to its area (or cropped to `viewport` when zoomed > 100%) and
    supports mouse-drag panning without re-encoding huge off-screen bitmaps."""

    DEFAULT_CSS = """
    ZoomableImage { width: 1fr; height: 1fr; align: center middle; overflow: hidden; }
    """

    class ViewportChanged(Message):
        """Posted when the user drags or scrolls this image (`draft=True` mid-drag)."""

        def __init__(self, view: "ZoomableImage", draft: bool = False) -> None:
            super().__init__()
            self.view = view
            self.draft = draft

    def __init__(
        self,
        viewport: ImageViewport | None = None,
        widget_cls=None,
        allow_wheel_zoom: bool = False,
        interactive: bool = True,
        size_provider: Callable[[], tuple[int, int] | None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.viewport = viewport if viewport is not None else ImageViewport()
        self._widget_cls = widget_cls or BEST_IMAGE
        self.allow_wheel_zoom = allow_wheel_zoom
        self.interactive = interactive  # False: a fitted preview, no dragging
        self._size_provider = size_provider
        self.path: Path | None = None
        self.full_path: Path | None = None
        self._source = None
        self._dragging = False
        self._last_xy: tuple[int, int] = (0, 0)
        self._drag_timer = None
        self._settle_timer = None

    def compose(self) -> ComposeResult:
        yield self._widget_cls(None, id="img")

    @property
    def image_widget(self):
        found = list(self.query("#img"))
        return found[0] if found else None

    @property
    def source_size(self) -> tuple[int, int] | None:
        return self._source.size if self._source is not None else None

    def _stop_timers(self) -> None:
        if self._drag_timer is not None:
            self._drag_timer.stop()
            self._drag_timer = None
        if self._settle_timer is not None:
            self._settle_timer.stop()
            self._settle_timer = None

    def on_unmount(self) -> None:
        self._stop_timers()
        if self._source is not None:
            self._source.close()
            self._source = None

    def set_image(self, path: Path | None, full_path: Path | None = None) -> None:
        from PIL import Image as PILImage

        self._stop_timers()
        if self._source is not None:
            self._source.close()
            self._source = None
        self.path = path
        self.full_path = full_path or path
        if path is None:
            img = self.image_widget
            if img is not None:
                img.image = None
            return
        with PILImage.open(path) as im:
            self._source = im.copy()
        if self.content_size.width > 0 and self.content_size.height > 0:
            self.render_view()
        else:
            img = self.image_widget
            if img is not None:
                img.image = path
            self.call_after_refresh(self.render_view)

    async def set_widget_cls(self, widget_cls) -> None:
        self._widget_cls = widget_cls
        await self.remove_children()
        widget = widget_cls(None, id="img")
        await self.mount(widget)
        if self._source is not None:
            if self.content_size.width > 0 and self.content_size.height > 0:
                self.render_view()
            else:
                widget.image = self.path
                self.call_after_refresh(self.render_view)

    def _viewport_size(self) -> tuple[int, int]:
        vp_w, vp_h = self.content_size.width, self.content_size.height
        if vp_w <= 0 or vp_h <= 0:
            return vp_w, vp_h
        if self._size_provider is not None:
            shared = self._size_provider()
            if shared is not None and shared[0] > 0 and shared[1] > 0:
                return min(vp_w, shared[0]), min(vp_h, shared[1])
        return vp_w, vp_h

    def render_view(self, draft: bool = False) -> None:
        if self._source is None or not self.is_attached:
            return
        vp_w, vp_h = self._viewport_size()
        if vp_w <= 0 or vp_h <= 0:
            return
        img = self.image_widget
        if img is None:
            return
        cell_w, cell_h = _cell_pixels()
        plan = self.viewport.plan(self._source.width, self._source.height, vp_w, vp_h, cell_w, cell_h)
        img.styles.width = plan.disp_w
        img.styles.height = plan.disp_h
        if plan.is_full and not draft and self.path is not None:
            img.image = self.path
        else:
            img.image = render_crop(self._source, plan, cell_w, cell_h, draft=draft)
        img.refresh(layout=True)

    def on_resize(self) -> None:
        if self._source is not None and self.content_size.width > 0 and self.content_size.height > 0:
            self.render_view()

    # ---- mouse dragging & optional wheel zoom ---------------------------------------------------
    def _emit_drag_draft(self) -> None:
        self._drag_timer = None
        if self.is_attached:
            self.post_message(self.ViewportChanged(self, draft=True))

    def _emit_settle(self) -> None:
        self._settle_timer = None
        if self.is_attached:
            self.post_message(self.ViewportChanged(self, draft=False))

    def on_mouse_down(self, event: MouseDown) -> None:
        if not self.interactive or event.button != 1 or self._source is None or self.viewport.zoom <= 1.0:
            return
        self._dragging = True
        self._last_xy = (event.screen_x, event.screen_y)
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event: MouseMove) -> None:
        if not self._dragging or self._source is None:
            return
        event.stop()
        dx = event.screen_x - self._last_xy[0]
        dy = event.screen_y - self._last_xy[1]
        if dx == 0 and dy == 0:
            return
        self._last_xy = (event.screen_x, event.screen_y)
        raw_w, raw_h = self._viewport_size()
        vp_w = max(1, raw_w or 80)
        vp_h = max(1, raw_h or 24)
        cell_w, cell_h = _cell_pixels()
        if not self.viewport.pan_by_cells(
            dx, dy, self._source.width, self._source.height, vp_w, vp_h, cell_w, cell_h
        ):
            return
        if self._drag_timer is None:
            self._drag_timer = self.set_timer(DRAG_THROTTLE_S, self._emit_drag_draft)
        if self._settle_timer is not None:
            self._settle_timer.stop()
        self._settle_timer = self.set_timer(SETTLE_DELAY_S, self._emit_settle)

    def on_mouse_up(self, event: MouseUp) -> None:
        if not self._dragging:
            return
        self._dragging = False
        try:
            self.release_mouse()
        except Exception:
            pass
        event.stop()
        self._stop_timers()
        self.post_message(self.ViewportChanged(self, draft=False))

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        if self.allow_wheel_zoom and self._source is not None:
            event.stop()
            if self.viewport.step_zoom(1):
                self.post_message(self.ViewportChanged(self, draft=False))

    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        if self.allow_wheel_zoom and self._source is not None:
            event.stop()
            if self.viewport.step_zoom(-1):
                self.post_message(self.ViewportChanged(self, draft=False))


class ImageViewerScreen(Screen):
    """Full-screen viewer for one image (single run) or side-by-side images (comparison).
    Zoom (up to 1000%) and pan apply to all panes simultaneously."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("plus,equals_sign", "zoom(1)", "Zoom in"),
        Binding("minus", "zoom(-1)", "Zoom out"),
        Binding("0", "zoom_fit", "Fit"),
        Binding("m", "next_mode", "Render mode"),
        Binding("left", "pan(-0.15, 0)", "Pan left", show=False),
        Binding("right", "pan(0.15, 0)", "Pan right", show=False),
        Binding("up", "pan(0, -0.15)", "Pan up", show=False),
        Binding("down", "pan(0, 0.15)", "Pan down", show=False),
    ]
    DEFAULT_CSS = """
    ImageViewerScreen #viewer-legend { padding: 0 1; height: auto; }
    ImageViewerScreen #viewer-status { padding: 0 1; color: $text-muted; }
    ImageViewerScreen #viewer-cells { height: 1fr; }
    ImageViewerScreen .viewer-cell { width: 1fr; height: 1fr; }
    ImageViewerScreen .viewer-cell-sep { border-left: solid $primary; margin-left: 1; padding-left: 1; }
    ImageViewerScreen .viewer-label { height: 1; padding: 0 1; }
    ImageViewerScreen .viewer-empty { width: 1fr; height: 1fr; content-align: center middle; color: $text-muted; }
    """

    def __init__(
        self,
        items: Sequence[tuple[str, Path | None, str]],
        title: str,
        subtitle: str = "",
        viewport: ImageViewport | None = None,
        legend: Sequence[tuple[str, str]] = (),
    ) -> None:
        super().__init__()
        self.items = list(items)
        self.viewer_title = title
        self.viewer_subtitle = subtitle
        self.viewport = viewport.copy() if viewport is not None else ImageViewport()
        self._legend = list(legend)
        self.mode_index = 0

    def _cell_viewport_size(self) -> tuple[int, int] | None:
        sizes = [
            (p.content_size.width, p.content_size.height)
            for p in self.query(ZoomableImage)
            if p.content_size.width > 0 and p.content_size.height > 0
        ]
        if not sizes:
            return None
        return min(w for w, _ in sizes), min(h for _, h in sizes)

    def _render_modes(self):
        from . import files as files_mod

        return files_mod.RENDER_MODES

    @property
    def mode_name(self) -> str:
        modes = self._render_modes()
        return modes[self.mode_index % len(modes)][0]

    def compose(self) -> ComposeResult:
        modes = self._render_modes()
        widget_cls = modes[self.mode_index % len(modes)][1]
        yield Header()
        if self._legend:
            yield Static("", id="viewer-legend")
        yield Static("", id="viewer-status")
        shared_size = self._cell_viewport_size if len(self.items) > 1 else None
        with Horizontal(id="viewer-cells"):
            for i, (label, path, color) in enumerate(self.items):
                with Vertical(classes="viewer-cell viewer-cell-sep" if i else "viewer-cell"):
                    if len(self.items) > 1:
                        header = Text(no_wrap=True, overflow="ellipsis")
                        if color:
                            header.append("■ ", style=color)
                        header.append(label, style="dim" if self._legend else "")
                        yield Static(header, classes="viewer-label")
                    if path is None:
                        yield Static("not logged", classes="viewer-empty")
                    else:
                        yield ZoomableImage(
                            viewport=self.viewport,
                            widget_cls=widget_cls,
                            allow_wheel_zoom=True,
                            size_provider=shared_size,
                        )
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
        panes = list(self.query(ZoomableImage))
        idx = 0
        for _, path, _ in self.items:
            if path is not None and idx < len(panes):
                try:
                    panes[idx].set_image(path, path)
                except IMAGE_ERRORS as exc:
                    self.notify(f"Could not read image: {exc}", severity="warning")
                idx += 1
        self._update_status()
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "pan":
            return self.viewport.zoom > 1.0
        return True

    def key_label(self, action: str) -> str | None:
        return f"Render mode: {self.mode_name}" if action == "next_mode" else None

    def _update_status(self) -> None:
        dims = ""
        for pane in self.query(ZoomableImage):
            if pane.source_size is not None:
                dims = f"{pane.source_size[0]}×{pane.source_size[1]} · "
                break
        zoom_pct = round(self.viewport.zoom * 100)
        pan_info = f" · pan {round(self.viewport.pan_x * 100)}%,{round(self.viewport.pan_y * 100)}%" if self.viewport.zoom > 1.0 else ""
        self.query_one("#viewer-status", Static).update(
            plain(f"{dims}zoom {zoom_pct}%{pan_info} · render: {self.mode_name}")
        )

    def _render_all(self, draft: bool = False) -> None:
        for pane in self.query(ZoomableImage):
            pane.render_view(draft=draft)
        self._update_status()
        self.refresh_bindings()

    def on_zoomable_image_viewport_changed(self, event: ZoomableImage.ViewportChanged) -> None:
        event.stop()
        self._render_all(draft=event.draft)

    def action_zoom(self, step: int) -> None:
        if self.viewport.step_zoom(step):
            self._render_all()

    def action_zoom_fit(self) -> None:
        if self.viewport.reset():
            self._render_all()

    def action_pan(self, dx: float, dy: float) -> None:
        if self.viewport.pan_by_fraction(dx, dy):
            self._render_all()

    async def action_next_mode(self) -> None:
        modes = self._render_modes()
        self.mode_index = (self.mode_index + 1) % len(modes)
        cls = modes[self.mode_index][1]
        for pane in self.query(ZoomableImage):
            await pane.set_widget_cls(cls)
        self._render_all()
