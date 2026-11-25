"""The facts shown at the top of a run (status, times, machine, git, command). UI-free."""

from __future__ import annotations

from datetime import datetime

from .api import RunInfo, RunMetadata
from .formatting import fmt_duration, fmt_time

Fields = list[tuple[str, str]]


def git_state(meta: RunMetadata) -> str:
    if not meta.git_commit:
        return "—"
    state = {True: " (uncommitted changes)", False: " (clean)", None: ""}[meta.git_dirty]
    return meta.git_commit[:10] + state


def summary_fields(
    info: RunInfo | None, meta: RunMetadata | None, now: datetime | None = None
) -> tuple[Fields, Fields]:
    """(short, wide) label/value pairs. None for info/meta means 'still loading'."""

    def v(value: str | None, loading: bool) -> str:
        return "…" if loading else (value or "—")

    li, lm = info is None, meta is None
    status = ("● " if info and info.state == "running" else "") + (info.state if info else "")
    short: Fields = [
        ("Status", v(status, li)),
        ("Started", v(info and fmt_time(info.created_at), li)),
        ("Run time", v(info and fmt_duration(info.runtime_seconds(now)), li)),
        ("Group", v(info and info.group, li)),
        ("Host", v(meta and meta.host, lm)),
        ("GPU", v(meta and meta.gpu, lm)),
        ("CPU cores", v(meta and meta.cpu_cores, lm)),
        ("Python", v(meta and meta.python, lm)),
    ]
    wide: Fields = [
        ("Git repo", v(meta and meta.git_remote, lm)),
        ("Git state", "…" if lm else git_state(meta)),
        ("Command", v(meta and meta.command, lm)),
    ]
    return short, wide
