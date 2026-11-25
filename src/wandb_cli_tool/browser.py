"""Opening a URL in the user's browser, without ever touching the terminal the TUI is drawn in.

Python's `webbrowser` may pick a text-mode browser (lynx, w3m) that takes over the terminal, and
some launchers print to it. So the command is chosen here and run with all of stdin/stdout/stderr
detached. `$BROWSER` comes first: VS Code's remote terminals and many SSH setups set it to a helper
that forwards the link to the browser on the user's own machine."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from typing import Mapping


def browser_command(url: str, env: Mapping[str, str] | None = None, platform: str | None = None) -> list[str] | None:
    """The command that opens `url`, or None if there is no way to open a browser from here
    (e.g. an SSH session with no display and no $BROWSER)."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    for entry in env.get("BROWSER", "").split(os.pathsep):
        if entry.strip():
            parts = shlex.split(entry)
            if any("%s" in p for p in parts):
                return [p.replace("%s", url) for p in parts]
            return [*parts, url]
    if platform == "darwin":
        return ["open", url]
    if platform.startswith("win"):
        return ["cmd", "/c", "start", "", url]
    if (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")) and shutil.which("xdg-open"):
        return ["xdg-open", url]
    return None


def open_in_browser(url: str, env: Mapping[str, str] | None = None, platform: str | None = None) -> bool:
    """Start opening `url`. True if a browser command was started (it may still fail on its own)."""
    command = browser_command(url, env, platform)
    if command is None:
        return False
    try:
        subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    return True
