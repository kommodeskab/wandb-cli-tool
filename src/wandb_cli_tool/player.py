"""Audio playback by delegating to whichever command-line player is installed."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

# (executable, quiet/no-window flags) in order of preference.
PLAYERS: list[tuple[str, list[str]]] = [
    ("mpv", ["--no-video", "--msg-level=all=error"]),
    ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "error"]),
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
        self.path: Path | None = None  # the file that was started last
        self._started = 0.0  # monotonic time the current stretch of playing began
        self._elapsed = 0.0  # seconds played before that (before the last pause)
        self._paused = False
        self._errors = None  # the player's stderr, read when it ends by itself
        self.failure: str | None = None  # why the last clip ended badly (set once it has ended by itself)
        self.ended_by_itself = False

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
        """Something is started and not finished (a paused clip still counts)."""
        return self._proc is not None and self._proc.poll() is None

    def check_finished(self) -> bool:
        """Call regularly. When the player process has ended on its own, remember whether it failed
        (`failure`: its last error line, or its exit code) and return True once."""
        proc = self._proc
        if proc is None or proc.poll() is None:
            return False
        self._proc, self._paused = None, False
        self.ended_by_itself = True
        if proc.returncode != 0:
            message = ""
            try:
                self._errors.seek(0)  # type: ignore[union-attr]
                lines = [l.strip() for l in self._errors.read().decode(errors="replace").splitlines() if l.strip()]  # type: ignore[union-attr]
                message = lines[-1] if lines else ""
            except (OSError, ValueError, AttributeError):
                pass
            self.failure = message or f"the player exited with code {proc.returncode}"
        return True

    @property
    def paused(self) -> bool:
        return self._paused and self.playing

    @property
    def can_pause(self) -> bool:
        """Pausing freezes the player process (POSIX); without that the clip can only be stopped."""
        return hasattr(signal, "SIGSTOP")

    @property
    def position(self) -> float:
        """Seconds played so far (the player is only a process, so this is time kept here). 0 when nothing is playing."""
        if not self.playing:
            return 0.0
        return self._elapsed if self._paused else self._elapsed + time.monotonic() - self._started

    def is_playing(self, path: Path | None) -> bool:
        """`path` is the file being played right now (paused or not)."""
        return self.playing and path is not None and self.path == path

    def pause(self) -> bool:
        if not (self.playing and not self._paused and self.can_pause):
            return False
        try:
            os.kill(self._proc.pid, signal.SIGSTOP)  # type: ignore[union-attr]
        except OSError:
            return False
        self._elapsed += time.monotonic() - self._started
        self._paused = True
        return True

    def resume(self) -> bool:
        if not self.paused:
            return False
        try:
            os.kill(self._proc.pid, signal.SIGCONT)  # type: ignore[union-attr]
        except OSError:
            return False
        self._started = time.monotonic()
        self._paused = False
        return True

    def play(self, path: Path) -> bool:
        """Start playing (replacing anything playing). False if no player or it failed."""
        self.stop()
        self.failure, self.ended_by_itself = None, False
        cmd = self.command(path)
        if cmd is None:
            return False
        try:
            self._errors = tempfile.TemporaryFile()
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=self._errors,
            )
        except OSError:
            self._proc = None
            return False
        self.path, self._started, self._elapsed, self._paused = path, time.monotonic(), 0.0, False
        return True

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            if self._paused:  # a frozen process only dies of SIGTERM once it is allowed to run
                try:
                    os.kill(proc.pid, signal.SIGCONT)
                except OSError:
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._paused = False
