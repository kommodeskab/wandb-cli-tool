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
from textual.containers import ScrollableContainer, Vertical
from textual.screen import Screen
from textual.widgets import Header, RichLog, Static, Tree
from textual.widgets.tree import TreeNode
from textual_image.widget import AutoImage, HalfcellImage, UnicodeImage
from textual_image.widget import Image as BestImage

from ..api import FileInfo, RunRef
from ..formatting import fmt_size
from .keybar import KeyBar
from ..plain import plain
from ..media import MAX_AUDIO_BYTES, MAX_IMAGE_BYTES, MAX_TEXT_BYTES, file_kind
from ..player import INSTALL_HINT

if TYPE_CHECKING:
    from ..app import WandbCliApp

DOWNLOAD_NAMESPACE = "files"
DOWNLOAD_CACHE_BYTES = 500 * 1024 * 1024
MAX_TEXT_LINES = 20_000  # shown per file; more would freeze the UI
MAX_HIGHLIGHT_BYTES = 200 * 1024  # JSON syntax colouring above this is plain text
MAX_PRETTY_BYTES = 1024 * 1024  # JSON re-indenting above this is skipped
ZOOMS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0)
FIT = ZOOMS.index(1.0)
# How images are drawn. "auto" uses sixel/kitty graphics where the terminal supports them.
# What "auto" resolved to when the app started: sixel / tgp (real pixels) or halfcell /
# unicode (character blocks, low resolution). Class names are all "Image"; the module differs.
DETECTED = AutoImage._Renderable.__module__.rsplit(".", 1)[-1]
# IMPORTANT: the widget for "auto" must be textual_image's `Image`, NOT `AutoImage`. For sixel
# terminals `Image` is a dedicated widget that injects the sixel data into Textual's rendering;
# `AutoImage` wraps the plain sixel renderer, which Textual drops, leaving a blank (dark) area.
LOW_RES = DETECTED in ("halfcell", "unicode")
RENDER_MODES = (("auto", BestImage), ("half-cell", HalfcellImage), ("unicode", UnicodeImage))


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
    ]
    DEFAULT_CSS = """
    #preview { height: 1fr; padding: 0 1; }
    #image-scroll { height: 1fr; }
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
        self.zoom_index = FIT
        self.mode_index = 0
        self.image_detail = ""
        self._fit_cells: tuple[int, int] | None = None  # image size in cells at "fit"

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="file-status")
        yield Vertical(id="preview")
        yield KeyBar()

    def on_mount(self) -> None:
        self.title = self.info.name
        self.sub_title = fmt_size(self.info.size)
        self._was_playing = False
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
            return "Stop" if self.app.audio_player.playing else "Play"
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
        return True

    def _status(self, text: str) -> None:
        self.query_one("#file-status", Static).update(plain(text))

    def _placeholder(self, text: str) -> None:
        self._status("")
        self.query_one("#preview", Vertical).mount(Static(Text(text, style="dim"), id="placeholder"))

    # ---- download ------------------------------------------------------
    @work(thread=True, exclusive=True)
    def fetch_file(self) -> None:
        cache = self.app.cache
        target = download_dir(cache.root, self.ref, self.info)
        path = target / self.info.name
        try:
            complete = path.is_file() and (not self.info.size or path.stat().st_size == self.info.size)
            if not complete:
                cache.prune_dir_sometimes(DOWNLOAD_NAMESPACE, DOWNLOAD_CACHE_BYTES)
                path = self.app.client.download_file(self.ref, self.info.name, target)
            if self.kind == "text":
                text, note = self._read_text(path)
            else:
                text, note = "", self._inspect_image(path) if self.kind == "image" else ""
        except Exception as exc:
            self.app.call_from_thread(self._failed, str(exc))
            return
        self.app.call_from_thread(self._ready, path, text, note)

    def _inspect_image(self, path: Path) -> str:
        from PIL import Image as PILImage

        with PILImage.open(path) as im:
            im.verify()  # raises on corrupt/unsupported data
        with PILImage.open(path) as im:
            return f"{im.format} {im.width}×{im.height}"

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
        self._placeholder(f"Could not load this file: {message}")

    async def _ready(self, path: Path, text: str, note: str) -> None:
        self.path = path
        preview = self.query_one("#preview", Vertical)
        if self.kind == "image":
            self.image_detail = note
            await self._mount_image()
        elif self.kind == "text":
            await self._show_text(preview, path, text, note)
        else:
            hint = "" if self.app.audio_player.available else INSTALL_HINT
            self._status("")
            await preview.mount(Static(Text(f"♪  {self.info.name}" + (f"\n\n{hint}" if hint else "")), id="audio-info"))
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
    def zoom(self) -> float:
        return ZOOMS[self.zoom_index]

    @property
    def mode_name(self) -> str:
        return RENDER_MODES[self.mode_index][0]

    async def _mount_image(self) -> None:
        preview = self.query_one("#preview", Vertical)
        await preview.remove_children()
        widget = RENDER_MODES[self.mode_index][1](self.path)
        widget.styles.width = "auto"
        widget.styles.height = "auto"
        scroll = ScrollableContainer(widget, id="image-scroll")
        await preview.mount(scroll)
        scroll.focus()
        self._fit_cells = None
        self.call_after_refresh(self._after_layout)

    @property
    def image_widget(self):
        found = list(self.query("#image-scroll > *"))
        return found[0] if found else None

    def _after_layout(self) -> None:
        widget = self.image_widget
        if widget is None:
            return
        if self._fit_cells is None and self.zoom_index == FIT:
            self._fit_cells = (widget.size.width, widget.size.height)
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        widget = self.image_widget
        if widget is None:
            return
        if self.zoom_index == FIT or not self._fit_cells or not self._fit_cells[0]:
            widget.styles.width = "auto"
            widget.styles.height = "auto"
        else:
            widget.styles.width = max(2, round(self._fit_cells[0] * self.zoom))
            widget.styles.height = "auto"  # keeps the aspect ratio
        widget.refresh(layout=True)
        render = f"auto → {DETECTED}" if self.mode_name == "auto" else self.mode_name
        hint = " · no terminal graphics detected, so this is low resolution" if self.mode_name == "auto" and LOW_RES else ""
        cell = ""
        if self.mode_name == "auto" and not LOW_RES:  # pixel size of one cell, for diagnosing blurry/oversized images
            from textual_image._terminal import get_cell_size

            cell = f" · cell {get_cell_size().width}×{get_cell_size().height}px"
        self._status(f"{self.image_detail} · zoom {round(self.zoom * 100)}% · render: {render}{cell}{hint}")

    def action_zoom(self, step: int) -> None:
        new = max(0, min(len(ZOOMS) - 1, self.zoom_index + step))
        if new != self.zoom_index:
            if self._fit_cells is None or self.zoom_index == FIT:  # measure the fit size as it is now:
                # the layout can still have changed since it was first recorded (e.g. the key bar grew)
                self._fit_cells = (self.image_widget.size.width, self.image_widget.size.height)
            self.zoom_index = new
        self._apply_zoom()

    def action_zoom_fit(self) -> None:
        self.zoom_index = FIT
        self._apply_zoom()

    async def action_next_mode(self) -> None:
        self.mode_index = (self.mode_index + 1) % len(RENDER_MODES)
        keep = self._fit_cells
        await self._mount_image()
        self._fit_cells = keep  # same image, same fit size: keep the current zoom
        self._apply_zoom()
        self.refresh_bindings()  # the key shows the mode now in use

    # ---- audio ---------------------------------------------------------
    def action_toggle_play(self) -> None:
        player = self.app.audio_player
        if player.playing:
            player.stop()
            self._status("Stopped.")
        elif self.path is not None and player.play(self.path):
            self._status("Playing…")
        else:
            self._status(INSTALL_HINT)
        self._was_playing = player.playing
        self.refresh_bindings()  # the key now says Play or Stop
