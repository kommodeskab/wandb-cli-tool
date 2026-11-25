"""Chart/panel model: how metrics are grouped into panels. No UI code here."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .palette import MAX_SERIES

SYSTEM_PANEL = "System"
OTHER_PANEL = "Other"  # metrics without a "/" in their name

STEP = "_step"
RUNTIME = "_runtime"
WALL_TIME = "_timestamp"

AXIS_LABELS = {STEP: "Step", RUNTIME: "Runtime (s)", WALL_TIME: "Wall time"}

PLOT, BAR, TABLE, IMAGE = "plot", "bar", "table", "image"
KINDS = (PLOT, BAR, TABLE, IMAGE)
MAX_BAR_VALUES = 40


def short_name(name: str, system: bool = False) -> str:
    """A metric without its panel prefix ('trainer/loss' -> 'loss'); system names stay whole."""
    return name if system else name.split("/", 1)[-1]


def display_names(names: tuple[str, ...] | list[str], system: bool = False) -> list[str]:
    """Labels for several metrics: the short form, unless two would read the same
    ('trainer/loss' and 'val/loss'), in which case every full name is used."""
    shorts = [short_name(n, system) for n in names]
    return shorts if len(set(shorts)) == len(shorts) else list(names)


@dataclass(frozen=True)
class ChartSpec:
    """One chart. `kind` decides what the other fields mean:
    plot:  y + more are the series (at most MAX_SERIES, all run metrics or all system metrics),
           plotted against `x`.
    bar:   y + more are the metrics shown as bars, in the order given (latest value of each).
    table: y is the key of a wandb.Table the run logged.
    image: y is a key under which the run logged wandb.Images (browsed with a step slider)."""

    y: str
    x: str = STEP  # plots only
    system: bool = False  # plots only: the series come from the system-metrics stream
    kind: str = PLOT
    more: tuple[str, ...] = ()

    @property
    def series(self) -> tuple[str, ...]:
        return (self.y, *self.more)

    @property
    def title(self) -> str:
        if self.kind == TABLE:
            return f"Table: {self.y}"
        if self.kind == IMAGE:
            return f"Images: {self.y}"
        names = ", ".join(display_names(self.series, self.system and self.kind == PLOT))
        return f"Bar: {names}" if self.kind == BAR else names


@dataclass
class Panel:
    name: str
    charts: list[ChartSpec] = field(default_factory=list)


def panel_name(metric: str) -> str:
    return metric.split("/", 1)[0] if "/" in metric else OTHER_PANEL


def axis_label(x: str) -> str:
    return AXIS_LABELS.get(x, x)


def x_options(metrics: list[str], system: bool) -> list[tuple[str, str]]:
    """(label, value) choices for the x-axis dropdown."""
    options = [(axis_label(RUNTIME), RUNTIME), (axis_label(WALL_TIME), WALL_TIME)]
    if system:
        return options
    return [(axis_label(STEP), STEP), *options, *((m, m) for m in metrics)]


def build_panels(metrics: list[str], system_metrics: list[str]) -> list[Panel]:
    """System first, then one panel per metric prefix (alphabetical), "Other" last."""
    groups: dict[str, list[ChartSpec]] = {}
    for key in metrics:
        groups.setdefault(panel_name(key), []).append(ChartSpec(key))
    system = Panel(
        SYSTEM_PANEL, [ChartSpec(k, RUNTIME, system=True) for k in system_metrics]
    )
    system.charts.extend(groups.pop(SYSTEM_PANEL, []))  # a user panel named "System" merges
    other = groups.pop(OTHER_PANEL, None)
    panels = [system, *(Panel(n, c) for n, c in sorted(groups.items()))]
    if other:
        panels.append(Panel(OTHER_PANEL, other))
    return panels


# ---- user customizations on top of the default layout --------------------------------
#
# Only what the user changed is stored (panels they created, charts they added, x-axis
# changes); it is applied on top of a freshly built default each time. So metrics logged
# later still show up, and "reset to default" is just forgetting the customizations.

LAYOUT_VERSION = 2  # bump when the stored format changes; older files are then ignored
# (2: layouts are saved per PROJECT instead of per run; version-1 files are dropped on purpose)
MAX_PANELS, MAX_CHARTS = 200, 1000  # sanity limits when reading a file


def chart_key(spec: ChartSpec) -> str:
    """Identifies a generated chart inside its panel (used for x-axis overrides)."""
    return f"{int(spec.system)}:{spec.y}"


def _spec_from_json(c: Any) -> ChartSpec | None:
    """A saved chart, or None if it is malformed. Files from before chart kinds existed have
    no kind/more and load as plain plots."""
    if not (isinstance(c, dict) and isinstance(c.get("y"), str)
            and isinstance(c.get("x"), str) and isinstance(c.get("system"), bool)):
        return None
    kind, more = c.get("kind", PLOT), c.get("more", [])
    if kind not in KINDS or not (isinstance(more, list) and all(isinstance(m, str) for m in more)):
        return None
    limit = {PLOT: MAX_SERIES, BAR: MAX_BAR_VALUES, TABLE: 1, IMAGE: 1}[kind]
    if 1 + len(more) > limit:
        return None
    return ChartSpec(c["y"], c["x"], c["system"], kind, tuple(more))


@dataclass
class Customization:
    panels: list[str] = field(default_factory=list)  # panels the user created, in order
    added: dict[str, list[ChartSpec]] = field(default_factory=dict)  # charts added, per panel
    x: dict[str, dict[str, str]] = field(default_factory=dict)  # panel -> chart_key -> x-axis
    removed: dict[str, list[str]] = field(default_factory=dict)  # panel -> chart_keys of deleted generated charts

    @property
    def is_empty(self) -> bool:
        return not (self.panels or self.added or self.x or self.removed)

    def to_json(self) -> dict[str, Any]:
        return {
            "panels": list(self.panels),
            "added": {
                p: [
                    {"y": c.y, "x": c.x, "system": c.system, "kind": c.kind, "more": list(c.more)}
                    for c in specs
                ]
                for p, specs in self.added.items()
            },
            "x": {p: dict(m) for p, m in self.x.items()},
            "removed": {p: list(keys) for p, keys in self.removed.items()},
        }

    @staticmethod
    def from_json(data: Any) -> "Customization":
        """Tolerant reader: anything malformed is dropped rather than raised."""
        out = Customization()
        if not isinstance(data, dict):
            return out
        panels = data.get("panels")
        if isinstance(panels, list):
            out.panels = [p for p in panels if isinstance(p, str) and p][:MAX_PANELS]
        added = data.get("added")
        if isinstance(added, dict):
            budget = MAX_CHARTS
            for panel, specs in added.items():
                if not (isinstance(panel, str) and isinstance(specs, list)):
                    continue
                good = [spec for spec in (_spec_from_json(c) for c in specs) if spec][:budget]
                budget -= len(good)
                if good:
                    out.added[panel] = good
        xs = data.get("x")
        if isinstance(xs, dict):
            for panel, overrides in xs.items():
                if isinstance(panel, str) and isinstance(overrides, dict):
                    good_x = {k: v for k, v in overrides.items() if isinstance(k, str) and isinstance(v, str)}
                    if good_x:
                        out.x[panel] = good_x
        removed = data.get("removed")  # absent in files saved before deleting existed
        if isinstance(removed, dict):
            for panel, keys in removed.items():
                if isinstance(panel, str) and isinstance(keys, list):
                    good_keys = [k for k in keys if isinstance(k, str)]
                    if good_keys:
                        out.removed[panel] = good_keys
        return out


def apply_customization(default: list[Panel], custom: Customization) -> list[Panel]:
    """The default panels plus the user's changes. Never mutates `default`."""
    panels = [
        Panel(
            p.name,
            [
                replace(c, x=custom.x.get(p.name, {}).get(chart_key(c), c.x))
                for c in p.charts
                if chart_key(c) not in custom.removed.get(p.name, ())
            ],
        )
        for p in default
    ]
    by_name = {p.name: p for p in panels}

    def panel_named(name: str) -> Panel:
        if name not in by_name:  # a user panel (or one whose generated panel is gone)
            by_name[name] = Panel(name)
            panels.append(by_name[name])
        return by_name[name]

    for name in custom.panels:
        panel_named(name)
    for name, specs in custom.added.items():
        panel_named(name).charts.extend(specs)
    return panels


class ChartLayout:
    """The panels shown for one run: a default plus the user's customizations.

    Every edit goes through here so it is recorded in `custom` (which is what gets saved)."""

    def __init__(self, default: list[Panel], custom: Customization | None = None) -> None:
        self.default = default
        self._default_by_name = {p.name: p for p in default}
        self.custom = custom or Customization()
        self.panels = apply_customization(default, self.custom)

    @property
    def is_customized(self) -> bool:
        return not self.custom.is_empty

    def _rebuild(self) -> None:
        self.panels = apply_customization(self.default, self.custom)

    def index_of(self, name: str) -> int:
        return next((i for i, p in enumerate(self.panels) if p.name == name), 0)

    def add_panel(self, name: str) -> int:
        """Create a panel (or just find the existing one); returns its index."""
        if all(p.name != name for p in self.panels):
            self.custom.panels.append(name)
            self._rebuild()
        return self.index_of(name)

    def add_chart(self, panel_index: int, spec: ChartSpec) -> int:
        """Append a chart to a panel; returns its index within the panel."""
        name = self.panels[panel_index].name
        self.custom.added.setdefault(name, []).append(spec)
        self._rebuild()
        return len(self.panels[panel_index].charts) - 1

    def _visible_generated(self, name: str) -> list[ChartSpec]:
        """The panel's generated charts that have not been deleted, un-overridden. A chart's
        index in the panel is its index here, or (after these) its index among the added ones."""
        removed = self.custom.removed.get(name, ())
        generated = self._default_by_name[name].charts if name in self._default_by_name else []
        return [c for c in generated if chart_key(c) not in removed]

    def set_chart(self, panel_index: int, chart_index: int, spec: ChartSpec) -> None:
        """Change a chart (only its x-axis can change). Generated charts record an override,
        added charts are edited in place."""
        name = self.panels[panel_index].name
        generated = self._visible_generated(name)
        if chart_index < len(generated):
            original = generated[chart_index]
            overrides = self.custom.x.setdefault(name, {})
            if spec.x == original.x:  # back to the default: forget the override
                overrides.pop(chart_key(original), None)
                if not overrides:
                    del self.custom.x[name]
            else:
                overrides[chart_key(original)] = spec.x
        else:
            self.custom.added[name][chart_index - len(generated)] = spec
        self._rebuild()

    def remove_chart(self, panel_index: int, chart_index: int) -> None:
        """Delete a chart from a panel. A generated chart is remembered as removed (it would
        otherwise be rebuilt next time); an added chart is simply dropped."""
        name = self.panels[panel_index].name
        generated = self._visible_generated(name)
        if chart_index < len(generated):
            key = chart_key(generated[chart_index])
            self.custom.removed.setdefault(name, []).append(key)
            overrides = self.custom.x.get(name)
            if overrides is not None:  # an x-axis choice for a deleted chart is moot
                overrides.pop(key, None)
                if not overrides:
                    del self.custom.x[name]
        else:
            added = self.custom.added[name]
            del added[chart_index - len(generated)]
            if not added:
                del self.custom.added[name]
        self._rebuild()

    def reset(self) -> None:
        self.custom = Customization()
        self._rebuild()
