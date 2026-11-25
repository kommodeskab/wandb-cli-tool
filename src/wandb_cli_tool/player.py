"""Audio playback by delegating to whichever command-line player is installed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

# (executable, quiet/no-window flags) in order of preference.
PLAYERS: list[tuple[str, list[str]]] = [
    ("mpv", ["--no-video", "--really-quiet"]),
    ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet"]),
    ("afplay", []),
    ("paplay", []),
    ("play", ["-q"]),
    ("aplay", ["-q"]),
]

INSTALL_HINT = "No audio player found. Install mpv or ffmpeg (ffplay) to play audio."


class AudioPlayer:
    def __init__(self, which: Callable[[str], str | None] = shutil.which) -> None:
        self._which = which
        self._proc: subprocess.Popen | None = None

    def command(self, path: Path) -> list[str] | None:
        for exe, flags in PLAYERS:
            found = self._which(exe)
            if found:
                return [found, *flags, str(path)]
        return None

    @property
    def available(self) -> bool:
        return any(self._which(exe) for exe, _ in PLAYERS)

    @property
    def playing(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def play(self, path: Path) -> bool:
        """Start playing (replacing anything playing). False if no player or it failed."""
        self.stop()
        cmd = self.command(path)
        if cmd is None:
            return False
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            self._proc = None
            return False
        return True

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
