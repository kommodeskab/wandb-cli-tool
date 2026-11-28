"""Audio logged with wandb.Audio, browsed step by step, and the waveform drawn for it. UI-free.

An audio clip logged under a key shows up in the run summary (latest value) and in every history row
that logged it as a small record:
  {"_type": "audio-file", "path": "media/audio/<key>_<step>_<hash>.wav", "sha256": ..., "size": ...,
   "sampling_rate": ..., "caption": ...}
Several clips logged at once are a record of type "audio" with a list of such records under "audio".
Steps reuse `ImageStep`/`ImageSeries` (paths, hashes, sizes, captions), they are just not images.
"""

from __future__ import annotations

import array
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .images import MAX_IMAGE_STEPS, ImageSeries, ImageStep

AUDIO_TYPES = ("audio-file", "audio")
PEAKS = 600  # amplitude samples kept per clip (a terminal is never wider than this)
FFMPEG_RATE = 8000  # sample rate a clip is converted to when the wave module cannot read it


@dataclass(frozen=True)
class AudioInfo:
    key: str
    multiple: bool = False
    count: int | None = None

    @property
    def label(self) -> str:
        what = f"{self.count} per step" if self.multiple and self.count else "one per step"
        return f"{self.key}  ({what})"


def _records(value: Any) -> list[dict]:
    """The file records inside one logged value (one for "audio-file", several for "audio")."""
    if not isinstance(value, dict):
        return []
    if value.get("_type") == "audio-file":
        return [value] if isinstance(value.get("path"), str) else []
    if value.get("_type") == "audio" and isinstance(value.get("audio"), list):
        return [r for r in value["audio"] if isinstance(r, dict) and isinstance(r.get("path"), str)]
    return []


def audio_entries(summary: Mapping[str, Any]) -> list[AudioInfo]:
    """The audio keys a run logged, sorted by key."""
    found = []
    for key, value in summary.items():
        records = _records(value)
        if records:
            found.append(AudioInfo(str(key), len(records) > 1, len(records)))
    return sorted(found, key=lambda i: i.key)


def parse_audio_steps(rows: Iterable[Mapping[str, Any]], key: str, limit: int = MAX_IMAGE_STEPS) -> ImageSeries:
    """The steps at which `key` logged audio, in step order. Rows without a usable record are skipped."""
    by_step: dict[int, ImageStep] = {}
    for row in rows:
        step = row.get("_step")
        records = _records(row.get(key))
        if not records or not isinstance(step, int) or isinstance(step, bool):
            continue
        by_step[step] = ImageStep(
            step,
            tuple(r["path"] for r in records),
            tuple(str(r["sha256"]) for r in records if r.get("sha256")),
            tuple(r["size"] if isinstance(r.get("size"), int) else None for r in records),
            tuple(str(r.get("caption") or "") for r in records),
        )
    steps = [by_step[s] for s in sorted(by_step)]
    total = len(steps)
    if total > limit > 1:
        picks = sorted({round(i * (total - 1) / (limit - 1)) for i in range(limit)})
        steps = [steps[i] for i in picks]
    return ImageSeries(tuple(steps), total)


# ---- the waveform ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Waveform:
    duration: float  # seconds
    peaks: tuple[float, ...]  # loudness over time, 0..1, `PEAKS` values or fewer for a very short clip


def _peaks_of(samples: "array.array", full_scale: float, buckets: int) -> tuple[float, ...]:
    n = len(samples)
    if n == 0:
        return ()
    buckets = min(buckets, n)
    out = []
    for i in range(buckets):
        chunk = samples[i * n // buckets : max((i + 1) * n // buckets, i * n // buckets + 1)]
        out.append(max(abs(max(chunk)), abs(min(chunk))) / full_scale)
    top = max(out)
    return tuple(min(p / top, 1.0) for p in out) if top > 0 else tuple(0.0 for _ in out)


def _read_wav(path: Path) -> Waveform:
    with wave.open(str(path), "rb") as w:
        channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        if width not in (1, 2, 4) or rate <= 0 or channels <= 0:
            raise wave.Error(f"unsupported sample format ({width * 8}-bit)")
        raw = w.readframes(frames)
    code = {1: "B", 2: "h", 4: "i"}[width]
    samples = array.array(code)
    samples.frombytes(raw[: len(raw) - len(raw) % (width * channels)])
    if width == 1:  # 8-bit WAV is unsigned
        samples = array.array("h", (s - 128 for s in samples))
    if channels > 1:  # the first channel is drawn (fast; the channels of a clip rarely differ in shape)
        samples = samples[0::channels]
    full = {1: 128.0, 2: 32768.0, 4: 2147483648.0}[width]
    return Waveform(frames / rate, _peaks_of(samples, full, PEAKS))


def read_waveform(path: Path) -> Waveform:
    """Duration and loudness over time of an audio file. WAV is read directly; anything else (or a WAV the
    standard library cannot read) is converted with ffmpeg if it is installed. Raises ValueError otherwise."""
    try:
        return _read_wav(path)
    except (wave.Error, EOFError, OSError, ValueError, MemoryError):
        pass
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ValueError("The waveform needs a WAV file or ffmpeg to read this audio format.")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "clip.wav"
        try:
            subprocess.run(
                [ffmpeg, "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(FFMPEG_RATE), "-acodec", "pcm_s16le", str(out)],
                check=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=120,
            )
            return _read_wav(out)
        except (subprocess.SubprocessError, OSError, wave.Error, EOFError, ValueError) as exc:
            raise ValueError("This audio could not be read.") from exc


def columns_for(peaks: tuple[float, ...], width: int) -> list[float]:
    """`peaks` resampled to `width` columns (the loudest value in each column's span)."""
    width = max(width, 1)
    if not peaks:
        return [0.0] * width
    n = len(peaks)
    return [max(peaks[i * n // width : max((i + 1) * n // width, i * n // width + 1)]) for i in range(width)]


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else f"{seconds // 60}:{seconds % 60:02d}"


def progress_bar(width: int, fraction: float) -> tuple[int, int]:
    """(filled cells, bar width) for a progress bar `width` cells wide."""
    width = max(width, 1)
    return round(max(0.0, min(fraction, 1.0)) * width), width


__all__ = [
    "AUDIO_TYPES", "AudioInfo", "Waveform",
    "audio_entries", "columns_for", "format_time", "waveform_rows", "parse_audio_steps", "progress_bar", "read_waveform",
]


def waveform_rows(columns: list[float], rows: int) -> list[str]:
    """The waveform as text: `rows` (even, at least 2) lines, one bar per column, mirrored around the middle
    line. Loudness is drawn in half-cell steps; a silent column is a thin line, so the clip's length is visible."""
    rows = max(2, rows - rows % 2)
    half = rows // 2  # cells above (and below) the middle
    lines = [[" "] * len(columns) for _ in range(rows)]
    for x, amp in enumerate(columns):
        units = max(1, round(max(0.0, min(amp, 1.0)) * half * 2))  # half-cells above the middle line
        for i in range(half):
            full = units >= 2 * i + 2
            partial = units == 2 * i + 1
            if full:
                lines[half - 1 - i][x] = lines[half + i][x] = "█"
            elif partial:
                lines[half - 1 - i][x] = "▄"
                lines[half + i][x] = "▀"
    return ["".join(line) for line in lines]
