import fcntl
import os
import pty
import struct
import termios
import threading

from wandb_cli_tool.terminal import (
    apply_cell_size, cell_size_from_winsize, detect_cell_size, parse_cell_size_reply,
)


def test_cell_size_from_winsize():
    assert cell_size_from_winsize(50, 200, 1600, 900) == (8, 18)
    assert cell_size_from_winsize(50, 200, 0, 0) is None  # pty that reports no pixels
    assert cell_size_from_winsize(0, 0, 100, 100) is None


def test_parse_cell_size_reply_is_height_then_width():
    assert parse_cell_size_reply(b"\x1b[6;18;8t") == (8, 18)
    assert parse_cell_size_reply(b"junk\x1b[6;20;10tmore") == (10, 20)
    assert parse_cell_size_reply(b"\x1b[6;0;0t") is None
    assert parse_cell_size_reply(b"") is None


def _pty_with_size(rows, cols, xpix, ypix):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, xpix, ypix))
    return master, slave


def _answering(master, reply, delay=0.0):
    """Plays the terminal: answers CSI 16 t with `reply` after `delay` seconds."""
    def terminal():
        data = b""
        while b"\x1b[16t" not in data:
            data += os.read(master, 64)
        threading.Event().wait(delay)
        os.write(master, reply)

    thread = threading.Thread(target=terminal)
    thread.start()
    return thread


def test_the_terminals_answer_wins_over_the_window_size():
    """VS Code on a high-DPI screen reports CSS pixels in the window size but draws sixels in
    device pixels; its CSI 16 t answer is what sixels are drawn at."""
    master, slave = _pty_with_size(40, 100, 800, 720)  # 8x18 per cell by the ioctl
    thread = _answering(master, b"\x1b[6;36;16t")
    try:
        assert detect_cell_size(slave, slave, timeout=2.0) == (16, 36)
    finally:
        thread.join()
        os.close(master), os.close(slave)


def test_the_window_size_is_the_fallback_when_the_terminal_does_not_answer():
    master, slave = _pty_with_size(40, 100, 800, 720)
    try:
        assert detect_cell_size(slave, slave, timeout=0.2) == (8, 18)
    finally:
        os.close(master), os.close(slave)


def test_detect_asks_the_terminal_and_tolerates_a_slow_reply():
    master, slave = _pty_with_size(40, 100, 0, 0)  # no pixel info: must query
    thread = _answering(master, b"\x1b[6;18;8t", delay=0.3)  # > the library's 0.1 s
    try:
        assert detect_cell_size(slave, slave, timeout=2.0) == (8, 18)
    finally:
        thread.join()
        os.close(master), os.close(slave)


def test_detect_returns_none_when_the_terminal_never_answers_and_restores_it():
    master, slave = _pty_with_size(40, 100, 0, 0)
    try:
        before = termios.tcgetattr(slave)
        assert detect_cell_size(slave, slave, timeout=0.2) is None
        assert termios.tcgetattr(slave) == before  # echo/line mode put back
    finally:
        os.close(master), os.close(slave)


def test_detect_returns_none_without_a_terminal(tmp_path):
    fd = os.open(tmp_path / "f", os.O_CREAT | os.O_RDWR)
    try:
        assert detect_cell_size(fd, fd, timeout=0.1) is None
    finally:
        os.close(fd)


def test_apply_cell_size_is_used_by_the_image_library():
    from textual_image import _terminal

    saved_cell = getattr(_terminal.get_cell_size, "_result", None)
    probe = getattr(_terminal, "probe_terminal", None)
    saved_probe = getattr(probe, "_result", None) if probe is not None else None
    try:
        apply_cell_size((8, 18))
        assert tuple(_terminal.get_cell_size()) == (8, 18)
    finally:
        if saved_cell is None:
            _terminal.get_cell_size.__dict__.pop("_result", None)
        else:
            _terminal.get_cell_size._result = saved_cell
        if probe is not None:
            if saved_probe is None:
                probe.__dict__.pop("_result", None)
            else:
                probe._result = saved_probe
