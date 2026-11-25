"""Comparing several runs of one project. UI-free.

Three things are compared: the facts shown at the top of a run (`overview_rows`), the configs
(`diff_config`: only what differs is interesting) and the charts (`compare_charts`: the
project's chart layout, with every chart reduced to what can be overlaid across runs).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Collection, Iterable, Sequence

from .api import RunInfo, RunMetadata
from .charts import BAR, IMAGE, PLOT, TABLE, ChartOptions, ChartSpec, Panel, display_names
from .images import ImageSeries, ImageStep
from .palette import MAX_SERIES
from .runfacts import summary_fields
from .runsort import natural_key
from .series import Point
from .tables import TableData

MAX_RUNS = MAX_SERIES  # one palette color per run, never cycled
MAX_LIST_ITEMS = 20  # a longer list in a config is shown as one value instead of one row per item
MAX_CONFIG_KEYS = 5000  # a pathological config cannot make the table huge
VALUE_WIDTH = 60
MISSING = "—"


# ---- labels ------------------------------------------------------------------------------
def run_labels(names: Sequence[str], ids: Sequence[str]) -> list[str]:
    """Names to tell the runs apart by: the run name, plus the id where two runs share a name."""
    counts = {n: names.count(n) for n in names}
    return [f"{n} ({i})" if counts[n] > 1 else n for n, i in zip(names, ids)]


# ---- overview ------------------------------------------------------------------------------
@dataclass(frozen=True)
class FieldRow:
    label: str
    values: tuple[str, ...]  # one per run
    differs: bool  # not every run has the same value


def _row(label: str, values: Sequence[str]) -> FieldRow:
    return FieldRow(label, tuple(values), len(set(values)) > 1)


def overview_rows(
    infos: Sequence[RunInfo | None],
    metas: Sequence[RunMetadata | None],
    now: datetime | None = None,
) -> list[FieldRow]:
    """The run screen's header facts, one row per fact and one value per run (None = still loading)."""
    per_run = [summary_fields(i, m, now) for i, m in zip(infos, metas)]
    short = [dict(s) for s, _ in per_run]
    wide = [dict(w) for _, w in per_run]
    rows = [_row(label, [s[label] for s in short]) for label in short[0]] if short else []
    tags = [("…" if i is None else ", ".join(i.tags) or MISSING) for i in infos]
    rows.insert(4, _row("Tags", tags))  # after Status, Started, Run time, Group
    rows += [_row(label, [w[label] for w in wide]) for label in wide[0]] if wide else []
    rows.append(_row("ID", [i.id if i else "…" for i in infos]))
    return rows


# ---- configs -------------------------------------------------------------------------------
def _leaf(value: Any) -> str:
    text = "null" if value is None else str(value)
    return text if len(text) <= VALUE_WIDTH else text[: VALUE_WIDTH - 1] + "…"


def flatten_config(config: Any, prefix: str = "") -> dict[str, str]:
    """A nested config as `{"optimizer.lr": "0.001", "layers[0]": "64"}`: one entry per leaf.
    Empty containers and long lists are single values. Capped at MAX_CONFIG_KEYS entries."""
    out: dict[str, str] = {}
    if not config:  # no config at all has no entries (not one entry reading "{}")
        return out

    def walk(value: Any, path: str) -> None:
        if len(out) >= MAX_CONFIG_KEYS:
            return
        if isinstance(value, dict) and value:
            for key in sorted(value, key=str):
                walk(value[key], f"{path}.{key}" if path else str(key))
        elif isinstance(value, (list, tuple)) and 0 < len(value) <= MAX_LIST_ITEMS:
            for i, item in enumerate(value):
                walk(item, f"{path}[{i}]")
        else:
            out[path or "(config)"] = _leaf(value)

    walk(config, prefix)
    return out


@dataclass(frozen=True)
class ConfigRow:
    path: str
    values: tuple[str | None, ...]  # per run; None = this run's config has no such entry
    only_some: bool  # some runs lack the entry (otherwise all have it, with different values)


@dataclass(frozen=True)
class ConfigDiff:
    rows: list[ConfigRow]  # the entries that differ, by path
    same: int  # how many entries are identical in every run (hidden by default)

    @property
    def different(self) -> int:
        return sum(1 for r in self.rows if not r.only_some)

    @property
    def partial(self) -> int:
        return sum(1 for r in self.rows if r.only_some)


def diff_config(
    configs: Sequence[dict[str, Any] | None], show_all: bool = False, pinned: Collection[str] = ()
) -> ConfigDiff:
    """What differs between the configs: entries with different values, and entries only some of the
    runs have. Entries every run has with the same value are only counted (or listed with `show_all`,
    or when pinned: a pinned entry is always listed)."""
    flat = [flatten_config(c or {}) for c in configs]
    paths = sorted({p for f in flat for p in f})
    rows, same = [], 0
    for path in paths:
        values = tuple(f.get(path) for f in flat)
        missing = any(v is None for v in values)
        if not missing and len(set(values)) == 1:
            same += 1
            if not show_all and path not in pinned:
                continue
        rows.append(ConfigRow(path, values, missing))
    return ConfigDiff(rows, same)


# ---- charts --------------------------------------------------------------------------------
@dataclass(frozen=True)
class CompareChart:
    """One chart of the comparison.
    plot: one metric's curves overlaid, one color per run (drawn as bars instead when every run
          logged a single value). bar: `metrics` as groups of bars, one bar per run in each group.
    A multi-metric plot of the project's layout becomes one plot per metric (small multiples)
    rather than runs x metrics lines in one chart."""

    kind: str  # PLOT, BAR, TABLE or IMAGE
    metrics: tuple[str, ...]  # (the key of the table / images for those kinds)
    x: str = "_step"
    system: bool = False
    title: str = ""
    subtitle: str = ""  # the layout chart a split-up plot came from
    opts: ChartOptions = ChartOptions()  # y-range, smoothing, log axes: the same as in the run screens
    source: ChartSpec | None = None  # the layout chart this was made from (editing acts on that one)
    index: int = 0  # its position in the layout's panel; a split-up plot's charts all share it

    @property
    def metric(self) -> str:
        return self.metrics[0]


def compare_charts(panel: Panel) -> list[CompareChart]:
    """The comparable charts of a layout panel (every kind: tables and images are compared too)."""
    out: list[CompareChart] = []
    for index, spec in enumerate(panel.charts):
        if spec.kind == PLOT:
            labels = display_names(spec.series, spec.system)
            multi = len(spec.series) > 1
            out += [
                CompareChart(PLOT, (m,), spec.x, spec.system, label, spec.title if multi else "", spec.opts, spec, index)
                for m, label in zip(spec.series, labels)
            ]
        elif spec.kind == BAR:
            out.append(CompareChart(BAR, spec.series, title=spec.title, opts=spec.opts, source=spec, index=index))
        elif spec.kind in (TABLE, IMAGE):
            out.append(CompareChart(spec.kind, (spec.y,), title=spec.title, source=spec, index=index))
    return out


def is_single_value(per_run: Iterable[list[Point]]) -> bool:
    """Every run that has data has exactly one point: a number per run, so bars rather than lines."""
    counts = [len(points) for points in per_run if points]
    return bool(counts) and all(c == 1 for c in counts)


def latest(points: list[Point]) -> float | None:
    return points[-1][1] if points and math.isfinite(points[-1][1]) else None


# ---- metrics: the latest value of each metric, per run -----------------------------------------
@dataclass(frozen=True)
class MetricRow:
    name: str
    values: tuple[float | None, ...]  # per run
    higher: bool  # a higher value is better (otherwise a lower one is)
    best: int | None  # which run has the best value (None: fewer than two runs have one, or all equal)
    differs: bool


def metric_rows(values: Sequence[dict[str, float]], higher: Collection[str] = ()) -> list[MetricRow]:
    """One row per metric any run has (sorted by name) with the run that is best at it. Lower is
    better unless the metric's name is in `higher`: what "better" means is for the user to say."""
    rows = []
    for name in sorted({k for v in values for k in v}):
        per_run = tuple(v.get(name) for v in values)
        have = [(x, i) for i, x in enumerate(per_run) if x is not None]
        differs = len({x for x, _ in have}) > 1 or len(have) < len(per_run)
        best = None
        if len(have) > 1 and len({x for x, _ in have}) > 1:
            pick = max if name in higher else min
            best = pick(have, key=lambda t: t[0])[1]
        rows.append(MetricRow(name, per_run, name in higher, best, differs))
    return rows


# ---- tables: every run's table in one ---------------------------------------------------------
MAX_COMPARE_ROWS = 300  # rows shown per run


@dataclass(frozen=True)
class StackedTable:
    columns: tuple[str, ...]  # the union of the runs' columns (first table's order first)
    rows: tuple[tuple[int, tuple[str, ...]], ...]  # (run position, cells in `columns`)
    missing: tuple[int, ...]  # runs that have no such table
    truncated: bool  # some run has more rows than are shown


def merge_tables(tables: Sequence[TableData | None], by_key: bool = False, per_run: int = MAX_COMPARE_ROWS) -> StackedTable:
    """The runs' tables as one: columns are united (a run without a column shows blanks) and each
    row says which run it came from. By default the rows of each run stay together; `by_key` puts
    rows with the same first-column value next to each other instead, so they can be compared."""
    columns: list[str] = []
    for table in tables:
        for column in (table.columns if table else ()):
            if column not in columns:
                columns.append(column)
    rows: list[tuple[int, tuple[str, ...]]] = []
    truncated = False
    for i, table in enumerate(tables):
        if table is None:
            continue
        where = {c: j for j, c in enumerate(table.columns)}
        truncated = truncated or len(table.rows) > per_run or table.total_rows > len(table.rows)
        for row in table.rows[:per_run]:
            rows.append((i, tuple(row[where[c]] if c in where and where[c] < len(row) else "" for c in columns)))
    if by_key and columns:
        rows.sort(key=lambda r: (natural_key(r[1][0]), r[0]))
    return StackedTable(tuple(columns), tuple(rows), tuple(i for i, t in enumerate(tables) if t is None), truncated)


# ---- images: the same step in every run -------------------------------------------------------
def union_steps(series: Sequence[ImageSeries | None]) -> list[int]:
    """Every step at which any run logged an image, ascending: the positions of a shared slider."""
    return sorted({step.step for s in series if s for step in s.steps})


def nearest_step(steps: Sequence[ImageStep], target: int) -> ImageStep | None:
    """The run's step closest to `target` (the earlier one on a tie), None if it has no images."""
    return min(steps, key=lambda s: (abs(s.step - target), s.step), default=None)
