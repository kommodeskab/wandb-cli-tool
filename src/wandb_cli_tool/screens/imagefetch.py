"""Getting one logged image onto disk, ready to show: download (cached by content), then a
screen-sized copy. Runs in a worker thread."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..api import FileInfo, RunRef
from ..images import ImageStep, make_thumbnail
from ..media import MAX_IMAGE_BYTES
from .files import DOWNLOAD_CACHE_BYTES, DOWNLOAD_NAMESPACE, download_dir

if TYPE_CHECKING:
    from ..app import WandbCliApp


def fetch_thumbnail(app: "WandbCliApp", ref: RunRef, step: ImageStep, sub: int) -> Path:
    """The image `sub` of `step` of the run, as a file a widget can draw. Raises if it is too large
    to display, cannot be downloaded or is not a readable image."""
    path = step.paths[sub]
    size = step.size(sub)
    if size is not None and size > MAX_IMAGE_BYTES:
        raise ValueError(f"The image is too large to display ({size // 1024 // 1024} MB).")
    folder = download_dir(app.cache.root, ref, FileInfo(path, size, step.identity(sub)))
    target = folder / path
    if not (target.is_file() and (size is None or target.stat().st_size == size)):
        app.cache.prune_dir_sometimes(DOWNLOAD_NAMESPACE, DOWNLOAD_CACHE_BYTES)
        app.client.download_file(ref, path, folder)
    return make_thumbnail(target)  # a screen-sized copy: huge images are slow to decode
