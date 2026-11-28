from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from rich.syntax import Syntax
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Header, RichLog, Static, Tree
from textual.widgets.tree import TreeNode
from textual.worker import get_current_worker
from textual_image.widget import AutoImage, HalfcellImage, UnicodeImage

from .sixelimage import BEST_IMAGE

from ..api import FileInfo, RunRef
from ..formatting import fmt_size
from ..images import IMAGE_ERRORS
from ..imageview import FIT, ZOOMS, ImageViewport
from .image_viewer import ZoomableImage
from .audio_widgets import AudioPlayerView, play_label, toggle_playback
from .keybar import KeyBar
from ..plain import plain
from ..audio import Waveform, read_waveform
from ..media import MAX_AUDIO_BYTES, MAX_IMAGE_BYTES, MAX_TEXT_BYTES, file_kind
from ..player import INSTALL_HINT

if TYPE_CHECKING:
    from ..app import WandbCliApp

DOWNLOAD_NAMESPACE = "files"
DOWNLOAD_CACHE_BYTES = 500 * 1024 * 1024
MAX_TEXT_LINES = 20_000  # shown per file; more would freeze the UI
MAX_HIGHLIGHT_BYTES = 200 * 1024  # JSON syntax colouring above this is plain text
MAX_PRETTY_BYTES = 1024 * 1024  # JSON re-indenting above this is skipped
# How images are drawn. "auto" uses sixel/kitty graphics where the terminal supports them.
# What "auto" resolved to when the app started: sixel / tgp (real pixels) or halfcell /
# unicode (character blocks, low resolution). Class names are all "Image"; the module differs.
DETECTED = AutoImage._Renderable.__module__.rsplit(".", 1)[-1]
# IMPORTANT: the widget for "auto" must be the dedicated sixel widget on sixel terminals (our
# `SafeSixelImage`, a fixed subclass of textual_image's), NOT `AutoImage`: that wraps the plain
# sixel renderer, which Textual drops, leaving a blank (dark) area.
LOW_RES = DETECTED in ("halfcell", "unicode")
RENDER_MODES = (("auto", BEST_IMAGE), ("half-cell", HalfcellImage), ("unicode", UnicodeImage))


# ---- file tree (pure) ----------------------------------------------------------------


@dataclass
class DirNode:
    dirs: dict[str, "DirNode"] = field(default_factory=dict)
    files: list[tuple[str, FileInfo]] = field(default_factory=list)  # (basename, info)
    count: int = 0  # files in this folder and below


def build_file_tree(files: Iterable[FileInfo]) -> DirNode:
    """Group files by the folders in their names ('media/audio/x.wav'). Names without a
    slash live in the root."""
    root = DirNode()
    for info in files:
        parts = [p for p in info.name.split("/") if p]
        if not parts:
            continue
        node = root
        node.count += 1
        for folder in parts[:-1]:
            node = node.dirs.setdefault(folder, DirNode())
            node.count += 1
        node.files.append((parts[-1], info))
    return root


def populate_tree(node: TreeNode, d: DirNode) -> None:
    """Folders first, then files; each alphabetically (case-insensitive)."""
    for name in sorted(d.dirs, key=str.casefold):
        sub = d.dirs[name]
        label = Text.assemble((f"{name}/", "bold"), (f"  {sub.count} file{'s' * (sub.count != 1)}", "dim"))
        populate_tree(node.add(label, expand=False), sub)
    for basename, info in sorted(d.files, key=lambda f: f[0].casefold()):
        kind = file_kind(info.name, info.mimetype)
        meta = fmt_size(info.size) + ("" if kind == "other" else f"  {kind}")
        node.add_leaf(Text.assemble(basename, (f"  {meta}", "dim")), data=info)


class FilesPane(Vertical):
    """Every file logged by the run, as a folder tree. Enter opens a file (or a folder)."""

    DEFAULT_CSS = """
    FilesPane { height: 1fr; }
    FilesPane #status { color: $text-muted; padding: 0 1; }
    FilesPane #files { height: 1fr; }
    """
    app: "WandbCliApp"

    def __init__(self, ref: RunRef, name: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.ref = ref
        self.run_name = name
        self._started = False

    def compose(self) -> ComposeResult:
        yield Static("Loading…", id="status")
        yield Tree("files", id="files")

    def on_mount(self) -> None:
        self.query_one("#files", Tree).show_root = False

    def activate(self) -> None:
        if not self._started:
            self._started = True
            self.load()

    def action_reload(self) -> None:
        self.query_one("#files", Tree).clear()
        self._status("Loading…")
        self.load()

    def _status(self, text: str) -> None:
        self.query_one("#status", Static).update(plain(text))

    @work(thread=True, exclusive=True)
    def load(self) -> None:
        files: list[FileInfo] = []
        try:
            for info in self.app.client.iter_files(self.ref):
                files.append(info)
                if len(files) % 100 == 0:
                    self.app.call_from_thread(self._status, f"Loading… {len(files)} files")
        except Exception as exc:
            self.app.call_from_thread(self._status, f"Error: {exc}")
            return
        self.app.call_from_thread(self._show, files)

    def _show(self, files: list[FileInfo]) -> None:
        tree = self.query_one("#files", Tree)
        tree.clear()
        tree.root.expand()
        populate_tree(tree.root, build_file_tree(files))
        self._status(f"{len(files)} files" if files else "This run has no files.")
        if tree.root.children:
            tree.move_cursor(tree.root.children[0])

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if isinstance(event.node.data, FileInfo):  # folders just expand/collapse
            event.stop()
            self.app.push_screen(FileScreen(self.ref, event.node.data))


def download_dir(root: Path, ref: RunRef, info: FileInfo) -> Path:
    """Content-addressed (name + md5), so a cached download is never stale."""
    digest = hashlib.sha256(f"{ref.path}|{info.name}|{info.md5}".encode()).hexdigest()[:32]
    return root / DOWNLOAD_NAMESPACE / digest


class FileScreen(Screen):
    """One file: images are drawn (zoomable), audio can be played, json/txt are shown as
    text, anything else gets a placeholder."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("space", "toggle_play", "Play"),
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
    #preview { height: 1fr; padding: 0 1; }
    #image-scroll { height: 1fr; width: 1fr; }
    #text { height: 1fr; }
    #file-status { padding: 0 1; color: $text-muted; }
    """
    app: "WandbCliApp"

    def __init__(self, ref: RunRef, info: FileInfo) -> None:
        super().__init__()
        self.ref = ref
        self.info = info
        self.kind = file_kind(info.name, info.mimetype)
        self.path: Path | None = None
        self.viewport = ImageViewport()
        self.mode_index = 0
        self.image_detail = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="file-status")
        yield Vertical(id="preview")
        yield KeyBar()

    def on_mount(self) -> None:
        self.title = self.info.name
        self.sub_title = fmt_size(self.info.size)
        self._was_playing = False
        self.waveform: Waveform | None = None
        if self.kind == "audio":
            self.set_interval(0.5, self._watch_playback)
        limit = {
            "image": MAX_IMAGE_BYTES, "audio": MAX_AUDIO_BYTES, "text": MAX_TEXT_BYTES
        }.get(self.kind)
        if limit is None:
            self._placeholder(
                f"No preview available yet for this file type ({self.info.mimetype or 'unknown'})."
            )
        elif self.info.size and self.info.size > limit:
            self._placeholder(f"File too large to preview ({fmt_size(self.info.size)}).")
        else:
            self._status("Downloading…")
            self.fetch_file()

    def on_unmount(self) -> None:
        self.app.audio_player.stop()

    def key_label(self, action: str) -> str | None:
        """What the key will do / is set to, shown in the key bar."""
        if action == "toggle_play":
            views = list(self.query(AudioPlayerView))
            return play_label(views[0].state) if views else "Play"
        if action == "next_mode":
            return f"Render mode: {self.mode_name}"
        return None

    def _watch_playback(self) -> None:
        """A clip ends by itself: keep the key saying "Stop" only while something plays."""
        playing = self.app.audio_player.playing
        if playing != self._was_playing:
            self._was_playing = playing
            self.refresh_bindings()
            if not playing:
                self._status("")

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # False hides the key from the footer (None would only dim it)
        ready = self.path is not None
        if action == "toggle_play":
            return ready and self.kind == "audio"
        if action in ("zoom", "zoom_fit", "next_mode"):
            return ready and self.kind == "image"
        if action == "pan":
            return ready and self.kind == "image" and self.viewport.zoom > 1.0
        return True

    def _status(self, text: str) -> None:
        self.query_one("#file-status", Static).update(plain(text))

    def _placeholder(self, text: str) -> None:
        self._status("")
        self.query_one("#preview", Vertical).mount(Static(Text(text, style="dim"), id="placeholder"))

    # ---- download ------------------------------------------------------
    @work(thread=True, exclusive=True)
    def fetch_file(self) -> None:
        worker = get_current_worker()
        cache = self.app.cache
        target = download_dir(cache.root, self.ref, self.info)
        path = target / self.info.name
        try:
            complete = path.is_file() and (not self.info.size or path.stat().st_size == self.info.size)
            if not complete:
                cache.prune_dir_sometimes(DOWNLOAD_NAMESPACE, DOWNLOAD_CACHE_BYTES)
                path = self.app.client.download_file(self.ref, self.info.name, target)
            if worker.is_cancelled:
                return
            if self.kind == "text":
                text, note = self._read_text(path)
            elif self.kind == "audio":
                text, note = "", self._read_audio(path)
            else:
                text, note = "", self._inspect_image(path) if self.kind == "image" else ""
        except Exception as exc:
            if not worker.is_cancelled:
                self.app.call_from_thread(self._failed, str(exc))
            return
        if not worker.is_cancelled:
            self.app.call_from_thread(self._ready, path, text, note)

    def _inspect_image(self, path: Path) -> str:
        from PIL import Image as PILImage

        with PILImage.open(path) as im:
            im.load()  # raises on corrupt/unsupported data
            return f"{im.format} {im.width}×{im.height}"

    def _read_audio(self, path: Path) -> str:
        """Reads the waveform (kept in `self.waveform`); the note says why there is none."""
        try:
            self.waveform = read_waveform(path)
            return ""
        except ValueError as exc:
            self.waveform = None
            return str(exc)

    def _read_text(self, path: Path) -> tuple[str, str]:
        """Contents, and a note about any transformation (e.g. 'pretty-printed')."""
        text = path.read_bytes().decode("utf-8", errors="replace")
        if path.suffix.lower() == ".json" and len(text) <= MAX_PRETTY_BYTES:
            try:
                return json.dumps(json.loads(text), indent=2, ensure_ascii=False), "pretty-printed JSON"
            except ValueError:
                return text, "not valid JSON, shown as plain text"
        return text, ""

    def _failed(self, message: str) -> None:
        if not self.is_attached:
            return
        self._placeholder(f"Could not load this file: {message}")

    async def _ready(self, path: Path, text: str, note: str) -> None:
        if not self.is_attached:
            return
        self.path = path
        preview = self.query_one("#preview", Vertical)
        if self.kind == "image":
            self.image_detail = note
            await self._mount_image()
        elif self.kind == "text":
            await self._show_text(preview, path, text, note)
        else:
            self._status("" if self.app.audio_player.available else INSTALL_HINT)
            view = AudioPlayerView(id="audio-view")
            await preview.mount(view)
            view.set_clip(path, self.waveform, note)
        self.refresh_bindings()

    # ---- text ----------------------------------------------------------
    async def _show_text(self, preview: Vertical, path: Path, text: str, note: str) -> None:
        log = RichLog(id="text", wrap=False, markup=False, highlight=False, auto_scroll=False)
        await preview.mount(log)
        log.focus()
        lines = text.splitlines()
        shown = lines[:MAX_TEXT_LINES]
        if path.suffix.lower() == ".json" and len(text) <= MAX_HIGHLIGHT_BYTES and not note.startswith("not valid"):
            log.write(Syntax(text, "json", theme="ansi_dark", word_wrap=False), scroll_end=False)
        else:
            for start in range(0, len(shown), 500):  # chunks keep the UI responsive
                for line in shown[start : start + 500]:
                    log.write(Text(line), scroll_end=False)
                await asyncio.sleep(0)
        parts = [f"{len(lines)} lines"]
        if len(lines) > MAX_TEXT_LINES:
            parts.append(f"showing the first {MAX_TEXT_LINES}")
        if note:
            parts.append(note)
        self._status(" · ".join(parts))

    # ---- image ---------------------------------------------------------
    @property
    def zoom_index(self) -> int:
        return self.viewport.zoom_index

    @zoom_index.setter
    def zoom_index(self, value: int) -> None:
        self.viewport.zoom_index = value

    @property
    def zoom(self) -> float:
        return self.viewport.zoom

    @property
    def mode_name(self) -> str:
        return RENDER_MODES[self.mode_index][0]

    async def _mount_image(self) -> None:
        preview = self.query_one("#preview", Vertical)
        await preview.remove_children()
        pane = ZoomableImage(
            viewport=self.viewport,
            widget_cls=RENDER_MODES[self.mode_index][1],
            allow_wheel_zoom=True,
            id="image-scroll",
        )
        pane.can_focus = True
        await preview.mount(pane)
        pane.focus()
        try:
            pane.set_image(self.path, self.path)
        except IMAGE_ERRORS as exc:
            self.path = None
            await preview.remove_children()
            self._failed(str(exc))
            return
        self._update_image_status()

    @property
    def image_widget(self):
        found = list(self.query("#image-scroll"))
        return found[0].image_widget if found else None

    def _update_image_status(self) -> None:
        render = f"auto → {DETECTED}" if self.mode_name == "auto" else self.mode_name
        hint = " · no terminal graphics detected, so this is low resolution" if self.mode_name == "auto" and LOW_RES else ""
        cell = ""
        if self.mode_name == "auto" and not LOW_RES:  # pixel size of one cell, for diagnosing blurry/oversized images
            from textual_image._terminal import get_cell_size

            cell = f" · cell {get_cell_size().width}×{get_cell_size().height}px"
        pan = (
            f" · pan {round(self.viewport.pan_x * 100)}%,{round(self.viewport.pan_y * 100)}%"
            if self.viewport.zoom > 1.0
            else ""
        )
        self._status(f"{self.image_detail} · zoom {round(self.zoom * 100)}%{pan} · render: {render}{cell}{hint}")

    def _redraw_image(self, draft: bool = False) -> None:
        found = list(self.query("#image-scroll"))
        if found:
            found[0].render_view(draft=draft)
        self._update_image_status()
        self.refresh_bindings()

    def on_zoomable_image_viewport_changed(self, event: ZoomableImage.ViewportChanged) -> None:
        event.stop()
        self._redraw_image(draft=event.draft)

    def action_zoom(self, step: int) -> None:
        if self.viewport.step_zoom(step):
            self._redraw_image()

    def action_zoom_fit(self) -> None:
        if self.viewport.reset():
            self._redraw_image()

    def action_pan(self, dx: float, dy: float) -> None:
        if self.viewport.pan_by_fraction(dx, dy):
            self._redraw_image()

    async def action_next_mode(self) -> None:
        self.mode_index = (self.mode_index + 1) % len(RENDER_MODES)
        found = list(self.query("#image-scroll"))
        if found:
            await found[0].set_widget_cls(RENDER_MODES[self.mode_index][1])
        self._redraw_image()
        self.refresh_bindings()  # the key shows the mode now in use

    # ---- audio ---------------------------------------------------------
    def action_toggle_play(self) -> None:
        views = list(self.query(AudioPlayerView))
        if not views:
            return
        message = toggle_playback(self.app, views[0])
        self._status(message or "")
        self._was_playing = self.app.audio_player.playing
        views[0].refresh()
        self.refresh_bindings()  # the key now says Play, Pause or Continue

    def on_audio_player_view_state_changed(self, event: AudioPlayerView.StateChanged) -> None:
        event.stop()
        self.refresh_bindings()
