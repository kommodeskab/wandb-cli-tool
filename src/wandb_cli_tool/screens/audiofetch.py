"""Getting one logged audio clip onto disk and reading its waveform. Runs in a worker thread."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..api import FileInfo, RunRef
from ..audio import Waveform, read_waveform
from ..images import ImageStep
from ..media import MAX_AUDIO_BYTES
from .files import DOWNLOAD_CACHE_BYTES, DOWNLOAD_NAMESPACE, download_dir

if TYPE_CHECKING:
    from ..app import WandbCliApp


def fetch_audio(app: "WandbCliApp", ref: RunRef, step: ImageStep, sub: int) -> tuple[Path, Waveform | None, str]:
    """`(file, waveform, note)` for `step[sub]`. The waveform is None, with a note saying why, when the
    clip can be played but not drawn. Raises if it is too large or cannot be downloaded."""
    path = step.paths[sub]
    size = step.size(sub)
    if size is not None and size > MAX_AUDIO_BYTES:
        raise ValueError(f"The audio is too large to load ({size // 1024 // 1024} MB).")
    folder = download_dir(app.cache.root, ref, FileInfo(path, size, step.identity(sub)))
    target = folder / path
    if not (target.is_file() and (size is None or target.stat().st_size == size)):
        app.cache.prune_dir_sometimes(DOWNLOAD_NAMESPACE, DOWNLOAD_CACHE_BYTES)
        app.client.download_file(ref, path, folder)
    try:
        return target, read_waveform(target), ""
    except ValueError as exc:
        return target, None, str(exc)
