"""Measure the terminal's cell size in pixels, before Textual takes over the keyboard.

textual-image needs it to size sixel/kitty images. Inside a running Textual app its own
query can't work (Textual's input thread swallows the reply) and it only waits 0.1 s, so it
falls back to a guessed 10x20 px. Wrong cells make images spill outside their area, and a
sixel running past the bottom of the terminal makes the terminal scroll.
"""

from __future__ import annotations

import os
import re
import select
import sys
import time

CELL_SIZE_REPLY = re.compile(rb"\x1b\[6;(\d+);(\d+)t")  # reply to CSI 16 t: height;width


def cell_size_from_winsize(rows: int, cols: int, xpixel: int, ypixel: int) -> tuple[int, int] | None:
    """(width, height) of one cell from a window size, or None if pixels are unreported."""
    if rows and cols and xpixel and ypixel:
        return xpixel // cols, ypixel // rows
    return None


def parse_cell_size_reply(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a terminal's reply to CSI 16 t."""
    match = CELL_SIZE_REPLY.search(data)
    if match and int(match.group(1)) > 0 and int(match.group(2)) > 0:
        return int(match.group(2)), int(match.group(1))
    return None


def detect_cell_size(
    in_fd: int | None = None, out_fd: int | None = None, timeout: float = 1.0
) -> tuple[int, int] | None:
    """(width, height) of a terminal cell in pixels, or None if it can't be determined.

    Tries the window-size ioctl first, then asks the terminal (CSI 16 t) and waits up to
    `timeout` seconds. Must run before the Textual app starts.
    """
    try:
        import fcntl
        import struct
        import termios
        import tty
    except ImportError:  # not a POSIX system
        return None
    in_fd = sys.__stdin__.fileno() if in_fd is None and sys.__stdin__ else in_fd
    out_fd = sys.__stdout__.fileno() if out_fd is None and sys.__stdout__ else out_fd
    if in_fd is None or out_fd is None or not (os.isatty(in_fd) and os.isatty(out_fd)):
        return None

    try:
        rows, cols, xpixel, ypixel = struct.unpack(
            "HHHH", fcntl.ioctl(out_fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
        )
        size = cell_size_from_winsize(rows, cols, xpixel, ypixel)
        if size:
            return size
    except OSError:
        pass

    saved = termios.tcgetattr(in_fd)
    try:
        tty.setcbreak(in_fd)  # no echo, no line buffering, so the reply can be read
        termios.tcflush(in_fd, termios.TCIFLUSH)
        os.write(out_fd, b"\x1b[16t")
        deadline = time.monotonic() + timeout
        reply = b""
        while not reply.endswith(b"t"):
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([in_fd], [], [], left)[0]:
                return None
            reply += os.read(in_fd, 64)
        return parse_cell_size_reply(reply)
    except OSError:
        return None
    finally:
        termios.tcsetattr(in_fd, termios.TCSADRAIN, saved)


def apply_cell_size(size: tuple[int, int]) -> None:
    """Give textual-image the measured size so it never has to guess."""
    from textual_image._terminal import CellSize, get_cell_size

    # get_cell_size caches its answer in this attribute and skips the query once it is set.
    get_cell_size._result = CellSize(*size)  # type: ignore[attr-defined]


def setup_cell_size() -> tuple[int, int] | None:
    size = detect_cell_size()
    if size:
        apply_cell_size(size)
    return size
