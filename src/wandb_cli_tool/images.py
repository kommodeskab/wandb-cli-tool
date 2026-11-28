"""Images logged with wandb.Image, browsed step by step. UI-free.

An image logged under a key shows up in the run summary (latest value) and in every history row
that logged it as a small record:
  {"_type": "image-file", "path": "media/images/<key>_<step>_<hash>.png", "sha256": ..., "size": ...}
and several images logged at once under one key as
  {"_type": "images/separated", "filenames": [...], "captions": [...], "count": N}
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

MAX_IMAGE_STEPS = 1000  # steps offered on the slider; more are sampled evenly (the last is always kept)
MAX_SCAN_ROWS = 50_000  # history rows read while looking for a key's images
THUMBNAIL_SIDE = 1600  # longest side of the copy that is actually drawn (big images are slow to decode)
IMAGE_TYPES = ("image-file", "images/separated")
IMAGE_ERRORS = (OSError, SyntaxError, ValueError)


@dataclass(frozen=True)
class ImageInfo:
    key: str
    multiple: bool = False  # several images per step
    count: int | None = None  # images in the latest step

    @property
    def label(self) -> str:
        what = f"{self.count} per step" if self.multiple and self.count else "one per step"
        return f"{self.key}  ({what})"


@dataclass(frozen=True)
class ImageStep:
    step: int
    paths: tuple[str, ...]  # run files, one per image logged at this step
    sha256s: tuple[str, ...] = ()
    sizes: tuple[int | None, ...] = ()
    captions: tuple[str, ...] = ()

    def caption(self, index: int) -> str:
        return self.captions[index] if index < len(self.captions) else ""

    def identity(self, index: int) -> str:
        """Something unique to this exact file's content (for the download cache)."""
        return self.sha256s[index] if index < len(self.sha256s) else self.paths[index]

    def size(self, index: int) -> int | None:
        return self.sizes[index] if index < len(self.sizes) else None


@dataclass(frozen=True)
class ImageSeries:
    steps: tuple[ImageStep, ...]
    total: int  # steps that had images before any sampling

    @property
    def sampled(self) -> bool:
        return self.total > len(self.steps)


def _str_list(value: Any) -> list[str]:
    return [str(v) if v is not None else "" for v in value] if isinstance(value, list) else []


def image_entries(summary: Mapping[str, Any]) -> list[ImageInfo]:
    """The image keys a run logged, sorted by key."""
    found = []
    for key, value in summary.items():
        if not (isinstance(value, dict) and value.get("_type") in IMAGE_TYPES):
            continue
        if value["_type"] == "images/separated":
            names = value.get("filenames")
            if not (isinstance(names, list) and names):
                continue
            count = value.get("count") if isinstance(value.get("count"), int) else len(names)
            found.append(ImageInfo(str(key), True, count))
        elif isinstance(value.get("path"), str):
            found.append(ImageInfo(str(key), False, 1))
    return sorted(found, key=lambda i: i.key)


def _step_from(row: Mapping[str, Any], value: Mapping[str, Any]) -> ImageStep | None:
    step = row.get("_step")
    if not isinstance(step, int) or isinstance(step, bool):
        return None
    if value.get("_type") == "images/separated":
        paths = [p for p in _str_list(value.get("filenames")) if p]
        sha = _str_list(value.get("sha256s"))
        sizes = value.get("sizes") if isinstance(value.get("sizes"), list) else []
    elif value.get("_type") == "image-file" and isinstance(value.get("path"), str):
        paths = [value["path"]]
        sha = [str(value["sha256"])] if value.get("sha256") else []
        sizes = [value.get("size")] if isinstance(value.get("size"), int) else []
    else:
        return None
    if not paths:
        return None
    captions = _str_list(value.get("captions")) or ([str(value["caption"])] if value.get("caption") else [])
    return ImageStep(step, tuple(paths), tuple(sha), tuple(s if isinstance(s, int) else None for s in sizes), tuple(captions))


def parse_image_steps(rows: Iterable[Mapping[str, Any]], key: str, limit: int = MAX_IMAGE_STEPS) -> ImageSeries:
    """The steps at which `key` logged images, in step order. Rows without a usable record are skipped."""
    by_step: dict[int, ImageStep] = {}
    for row in rows:
        value = row.get(key)
        if isinstance(value, dict):
            parsed = _step_from(row, value)
            if parsed is not None:
                by_step[parsed.step] = parsed  # a repeated step keeps the latest record
    steps = [by_step[s] for s in sorted(by_step)]
    total = len(steps)
    if total > limit > 1:  # even sampling that always keeps the first and the last step
        picks = sorted({round(i * (total - 1) / (limit - 1)) for i in range(limit)})
        steps = [steps[i] for i in picks]
    return ImageSeries(tuple(steps), total)


@dataclass
class StepCursor:
    """Position on the slider: which of `n` steps is shown."""

    n: int
    index: int = 0

    def __post_init__(self) -> None:
        self.index = max(0, min(self.index, self.n - 1)) if self.n else 0

    def move(self, delta: int) -> bool:
        """Move by `delta` steps (clamped). True if the position changed."""
        return self.jump(self.index + delta)

    def jump(self, index: int) -> bool:
        new = max(0, min(index, self.n - 1)) if self.n else 0
        changed = new != self.index
        self.index = new
        return changed

    @property
    def at_end(self) -> bool:
        return self.n == 0 or self.index == self.n - 1


def slider_geometry(width: int, index: int, n: int) -> tuple[int, int]:
    """(knob column, bar length) for a bar `width` cells wide showing position `index` of `n`."""
    width = max(width, 2)
    if n <= 1:
        return 0, width
    return round(index * (width - 1) / (n - 1)), width


def index_at(x: int, width: int, n: int) -> int:
    """The step a click at column `x` of a bar `width` cells wide selects."""
    if n <= 1:
        return 0
    width = max(width, 2)
    return max(0, min(n - 1, round(max(0, min(x, width - 1)) * (n - 1) / (width - 1))))


def _is_readable_image(path: Path) -> bool:
    """Return True if `path` can be opened and fully decoded as an image."""
    from PIL import Image

    try:
        with Image.open(path) as im:
            im.load()
        return True
    except IMAGE_ERRORS:
        return False


def make_thumbnail(path: Path, max_side: int = THUMBNAIL_SIDE) -> Path:
    """A screen-sized copy of an image next to the original (cached), or the original itself if
    it is small already. Raises if the file is not a readable image."""
    from PIL import Image

    thumb = path.with_name(f"{path.stem}.thumb{max_side}.png")
    if thumb.is_file() and thumb.stat().st_mtime >= path.stat().st_mtime:
        if _is_readable_image(thumb):
            return thumb  # made earlier: no need to read (and checksum) the big original again
        thumb.unlink(missing_ok=True)
    with Image.open(path) as im:
        if max(im.size) <= max_side:
            im.load()
            return path
        im.draft("RGB", (max_side * 2, max_side * 2))  # JPEGs decode at reduced size
        copy = im.copy()
    copy.thumbnail((max_side, max_side))
    fd, tmp_name = tempfile.mkstemp(dir=thumb.parent, prefix=f".{thumb.stem}.", suffix=".tmp.png")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        copy.save(tmp, format="PNG")
        tmp.replace(thumb)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return thumb
