"""Getting one logged image onto disk, ready to show: download (cached by content), then a
screen-sized copy. Runs in a worker thread."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..api import FileInfo, RunRef
from ..images import IMAGE_ERRORS, THUMBNAIL_SIDE, ImageStep, make_thumbnail
from ..media import MAX_IMAGE_BYTES
from .files import DOWNLOAD_CACHE_BYTES, DOWNLOAD_NAMESPACE, download_dir

if TYPE_CHECKING:
    from ..app import WandbCliApp


def fetch_image(app: "WandbCliApp", ref: RunRef, step: ImageStep, sub: int) -> tuple[Path, Path]:
    """Return `(thumbnail_path, original_path)` for `step[sub]`. Raises if too large,
    impossible to download, or unreadable."""
    path = step.paths[sub]
    size = step.size(sub)
    if size is not None and size > MAX_IMAGE_BYTES:
        raise ValueError(f"The image is too large to display ({size // 1024 // 1024} MB).")
    folder = download_dir(app.cache.root, ref, FileInfo(path, size, step.identity(sub)))
    target = folder / path
    cached = target.is_file() and (size is None or target.stat().st_size == size)
    if not cached:
        app.cache.prune_dir_sometimes(DOWNLOAD_NAMESPACE, DOWNLOAD_CACHE_BYTES)
        app.client.download_file(ref, path, folder)
    try:
        return make_thumbnail(target), target
    except IMAGE_ERRORS:
        if not cached:
            raise
        target.unlink(missing_ok=True)
        target.with_name(f"{target.stem}.thumb{THUMBNAIL_SIDE}.png").unlink(missing_ok=True)
        app.client.download_file(ref, path, folder)
        return make_thumbnail(target), target


def fetch_thumbnail(app: "WandbCliApp", ref: RunRef, step: ImageStep, sub: int) -> Path:
    """The image `sub` of `step` of the run, as a file a widget can draw. Raises if it is too large
    to display, cannot be downloaded or is not a readable image."""
    return fetch_image(app, ref, step, sub)[0]
