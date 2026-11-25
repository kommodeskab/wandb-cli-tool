"""Pinned projects, runs, metrics and config keys: what the user wants to see first. UI-free.

Pins are kept in the preferences (per entity for projects, per project for runs, metrics
and config keys), in the order they were pinned."""

from __future__ import annotations

from typing import Any, Callable, Sequence, TypeVar

from .preferences import Preferences

T = TypeVar("T")
KINDS = ("projects", "runs", "metrics", "config")
PREF_KEY = "pins"


class Pins:
    """The pins of one scope (entity for projects; project for runs, metrics and config)."""

    def __init__(self, prefs: Preferences, project: str) -> None:
        self._prefs = prefs
        self._project = project

    def _all(self) -> dict[str, Any]:
        stored = self._prefs.get(PREF_KEY)
        return stored if isinstance(stored, dict) else {}

    def get(self, kind: str) -> list[str]:
        """The pinned names of `kind` ("projects", "runs", "metrics" or "config"), oldest pin first."""
        by_project = self._all().get(kind)
        names = by_project.get(self._project) if isinstance(by_project, dict) else None
        return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []

    def set(self, kind: str, names: Sequence[str]) -> None:
        stored = {k: dict(v) if isinstance(v, dict) else {} for k, v in self._all().items()}
        by_project = stored.setdefault(kind, {})
        if names:
            by_project[self._project] = list(dict.fromkeys(names))
        else:
            by_project.pop(self._project, None)
        self._prefs.set(PREF_KEY, stored)

    def toggle(self, kind: str, *names: str) -> bool:
        """Pin `names`, or unpin them if all of them are pinned already. True if they are pinned now."""
        current = self.get(kind)
        pin = not all(n in current for n in names)
        self.set(kind, [*current, *names] if pin else [n for n in current if n not in names])
        return pin


def pinned_first(items: Sequence[T], pinned: Sequence[str], name: Callable[[T], str]) -> list[T]:
    """The pinned items first (in the order they were pinned), then the others in their own order."""
    rank = {n: i for i, n in enumerate(pinned)}
    first = sorted((i for i in items if name(i) in rank), key=lambda i: rank[name(i)])
    return first + [i for i in items if name(i) not in rank]


def matches_filter(name: str, text: str) -> bool:
    """Every word of `text` occurs in `name` (case does not matter); empty text matches everything."""
    lowered = name.lower()
    return all(word in lowered for word in text.lower().split())
