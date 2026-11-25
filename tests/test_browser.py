"""Choosing the command that opens a link, and never letting it touch the terminal."""

import subprocess

from wandb_cli_tool.browser import browser_command, open_in_browser

URL = "https://wandb.ai/e/p/runs/r"


def test_browser_variable_comes_first_with_or_without_a_placeholder(monkeypatch):
    assert browser_command(URL, {"BROWSER": "/helpers/browser.sh"}, "linux") == ["/helpers/browser.sh", URL]
    assert browser_command(URL, {"BROWSER": "firefox --new-tab %s"}, "linux") == ["firefox", "--new-tab", URL]
    assert browser_command(URL, {"BROWSER": "/p with space/open"}, "linux") == ["/p", "with", "space/open", URL]  # shell-style split
    assert browser_command(URL, {"BROWSER": ":/helpers/b.sh"}, "linux") == ["/helpers/b.sh", URL]  # empty entries skipped


def test_platform_defaults_and_no_display_means_no_browser(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    assert browser_command(URL, {}, "darwin") == ["open", URL]
    assert browser_command(URL, {}, "win32")[:3] == ["cmd", "/c", "start"]
    assert browser_command(URL, {"DISPLAY": ":0"}, "linux") == ["xdg-open", URL]
    assert browser_command(URL, {"WAYLAND_DISPLAY": "wayland-0"}, "linux") == ["xdg-open", URL]
    # a bare SSH session: python's webbrowser would pick a text browser that takes over the terminal
    assert browser_command(URL, {"SSH_CONNECTION": "1 2 3 4"}, "linux") is None
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert browser_command(URL, {"DISPLAY": ":0"}, "linux") is None  # no xdg-open installed


def test_the_browser_is_started_detached_from_the_terminal(monkeypatch):
    captured = {}

    class FakePopen:
        def __init__(self, args, **kw):
            captured.update(args=args, **kw)

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    assert open_in_browser(URL, {"BROWSER": "helper"}, "linux") is True
    assert captured["args"] == ["helper", URL]
    assert captured["stdin"] == captured["stdout"] == captured["stderr"] == subprocess.DEVNULL
    assert captured["start_new_session"] is True


def test_a_missing_or_failing_launcher_is_reported_not_raised(monkeypatch):
    assert open_in_browser(URL, {}, "linux") is False

    def boom(*a, **k):
        raise FileNotFoundError("no such helper")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert open_in_browser(URL, {"BROWSER": "nope"}, "linux") is False
