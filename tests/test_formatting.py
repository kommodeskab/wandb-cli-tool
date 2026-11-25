from datetime import datetime

from wandb_cli_tool.api import RunInfo
from wandb_cli_tool.formatting import fmt_count, fmt_duration, fmt_time, parse_time


def test_fmt_duration_uses_two_units_at_most():
    assert fmt_duration(None) == "—"
    assert fmt_duration(0) == "0s"
    assert fmt_duration(59.9) == "59s"
    assert fmt_duration(65) == "1m 05s"
    assert fmt_duration(3 * 3600 + 7 * 60 + 30) == "3h 07m"
    assert fmt_duration(2 * 86400 + 4 * 3600 + 59) == "2d 4h"
    assert fmt_duration(-5) == "0s"


def test_fmt_time_converts_utc_to_local_and_handles_missing(monkeypatch):
    import time

    monkeypatch.setenv("TZ", "Asia/Tokyo")  # UTC+9
    time.tzset()
    assert fmt_time("2024-05-01T22:30:00") == "2024-05-02 07:30"
    assert fmt_time("2024-05-01T22:30:00Z") == "2024-05-02 07:30"
    assert fmt_time("") == fmt_time(None) == fmt_time("garbage") == "—"


def test_parse_time_and_count():
    assert parse_time("2024-01-02T03:04:05") == datetime(2024, 1, 2, 3, 4, 5)
    assert parse_time("nope") is None
    assert fmt_count(None) == "—" and fmt_count(0) == "0"


def test_runtime_runs_to_now_when_running_else_to_last_heartbeat():
    start, beat = "2024-01-01T00:00:00", "2024-01-01T02:00:00"
    now = datetime(2024, 1, 1, 5, 0, 0)
    assert RunInfo("i", "n", "running", beat, start).runtime_seconds(now) == 5 * 3600
    assert RunInfo("i", "n", "finished", beat, start).runtime_seconds(now) == 2 * 3600
    assert RunInfo("i", "n", "finished", "", start).runtime_seconds(now) is None
    assert RunInfo("i", "n", "finished", beat, "").runtime_seconds(now) is None
    assert RunInfo("i", "n", "finished", start, beat).runtime_seconds(now) == 0  # clamped
