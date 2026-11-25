"""Chart/panel model: how metrics are grouped into panels. No UI code here."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

from .palette import MAX_SERIES

SYSTEM_PANEL = "System"
OTHER_PANEL = "Other"  # metrics without a "/" in their name
TABLES_PANEL = "Tables"
IMAGES_PANEL = "Images"

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


MAX_SMOOTHING = 0.99


@dataclass(frozen=True)
class ChartOptions:
    """How a plot is drawn. All off by default (auto y-range, raw data, linear axes)."""

    ymin: float | None = None  # fixed y-range ends (None: automatic)
    ymax: float | None = None
    smooth: float = 0.0  # exponential moving average weight, 0 = off (as in W&B: 0.9 is heavy smoothing)
    logy: bool = False
    logx: bool = False

    @property
    def is_default(self) -> bool:
        return self == ChartOptions()

    def range_text(self) -> str:
        """The fixed value range, e.g. "0…1" or "auto…5" ("" if it is automatic)."""
        if self.ymin is None and self.ymax is None:
            return ""
        lo = "auto" if self.ymin is None else f"{self.ymin:g}"
        hi = "auto" if self.ymax is None else f"{self.ymax:g}"
        return f"{lo}…{hi}"

    def describe(self) -> str:
        """The non-default settings in a few words, for a chart's subtitle."""
        parts = []
        if self.range_text():
            parts.append(f"y {self.range_text()}")
        if self.smooth:
            parts.append(f"smoothed {self.smooth:g}")
        if self.logy or self.logx:
            parts.append("log " + " & ".join(a for a, on in (("x", self.logx), ("y", self.logy)) if on))
        return ", ".join(parts)

    def to_json(self) -> dict[str, Any]:
        return {"ymin": self.ymin, "ymax": self.ymax, "smooth": self.smooth, "logy": self.logy, "logx": self.logx}

    @staticmethod
    def from_json(data: Any) -> "ChartOptions":
        """Tolerant reader: anything malformed falls back to the default for that field."""
        if not isinstance(data, dict):
            return ChartOptions()

        def number(key: str) -> float | None:
            v = data.get(key)
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None

        smooth = number("smooth") or 0.0
        out = ChartOptions(
            number("ymin"), number("ymax"), min(max(smooth, 0.0), MAX_SMOOTHING),
            data.get("logy") is True, data.get("logx") is True,
        )
        if out.ymin is not None and out.ymax is not None and out.ymin >= out.ymax:
            out = replace(out, ymin=None, ymax=None)
        return out


def parse_options(ymin: str, ymax: str, smooth: str, logy: bool, logx: bool) -> ChartOptions | str:
    """Options from what the user typed, or a message saying what is wrong. Blank = off/automatic."""

    def number(text: str, what: str) -> float | None | str:
        text = text.strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return f"{what} must be a number (or empty)."
        return value if math.isfinite(value) else f"{what} must be a finite number."

    lo, hi, weight = number(ymin, "Y min"), number(ymax, "Y max"), number(smooth, "Smoothing")
    for v in (lo, hi, weight):
        if isinstance(v, str):
            return v
    if lo is not None and hi is not None and lo >= hi:
        return "Y min must be smaller than Y max."
    if weight is not None and not 0 <= weight <= MAX_SMOOTHING:
        return f"Smoothing must be between 0 (off) and {MAX_SMOOTHING}."
    return ChartOptions(lo, hi, weight or 0.0, logy, logx)  # type: ignore[arg-type]


@dataclass(frozen=True)
class ChartSpec:
    """One chart. `kind` decides what the other fields mean:
    plot:  y + more are the series (at most MAX_SERIES, all run metrics or all system metrics),
           plotted against `x`.
    bar:   y + more are the metrics shown as bars, in the order given (latest value of each);
           `opts.ymin`/`ymax` fix the ends of the value axis.
    table: y is the key of a wandb.Table the run logged.
    image: y is a key under which the run logged wandb.Images (browsed with a step slider).
    `opts` (plots only) are the y-range, smoothing and log axes."""

    y: str
    x: str = STEP  # plots only
    system: bool = False  # plots only: the series come from the system-metrics stream
    kind: str = PLOT
    more: tuple[str, ...] = ()
    opts: ChartOptions = ChartOptions()  # plots only

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


def build_panels(
    metrics: list[str],
    system_metrics: list[str],
    tables: Sequence[str] = (),
    images: Sequence[str] = (),
) -> list[Panel]:
    """System first, then metric-prefix panels (alphabetical), "Other", "Tables" and "Images"."""
    groups: dict[str, list[ChartSpec]] = {}
    for key in metrics:
        groups.setdefault(panel_name(key), []).append(ChartSpec(key))
    system = Panel(
        SYSTEM_PANEL, [ChartSpec(k, RUNTIME, system=True) for k in system_metrics]
    )
    system.charts.extend(groups.pop(SYSTEM_PANEL, []))  # a user panel named "System" merges
    tables_extra = groups.pop(TABLES_PANEL, []) if tables else []
    images_extra = groups.pop(IMAGES_PANEL, []) if images else []
    other = groups.pop(OTHER_PANEL, None)
    panels = [system, *(Panel(n, c) for n, c in sorted(groups.items()))]
    if other:
        panels.append(Panel(OTHER_PANEL, other))
    if tables:
        panels.append(Panel(TABLES_PANEL, [ChartSpec(k, kind=TABLE) for k in tables] + tables_extra))
    if images:
        panels.append(Panel(IMAGES_PANEL, [ChartSpec(k, kind=IMAGE) for k in images] + images_extra))
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
    """Identifies a generated chart inside its panel."""
    if spec.kind != PLOT:
        return f"{spec.kind}:{spec.y}"
    return f"{int(spec.system)}:{spec.y}"


def _spec_to_json(c: ChartSpec) -> dict[str, Any]:
    return {
        "y": c.y, "x": c.x, "system": c.system, "kind": c.kind, "more": list(c.more),
        **({} if c.opts.is_default else {"opts": c.opts.to_json()}),
    }


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
    return ChartSpec(c["y"], c["x"], c["system"], kind, tuple(more), ChartOptions.from_json(c.get("opts")))


@dataclass
class Customization:
    panels: list[str] = field(default_factory=list)  # panels the user created, in order
    added: dict[str, list[ChartSpec]] = field(default_factory=dict)  # charts added, per panel
    x: dict[str, dict[str, str]] = field(default_factory=dict)  # panel -> chart_key -> x-axis
    removed: dict[str, list[str]] = field(default_factory=dict)  # panel -> chart_keys of deleted generated charts
    opts: dict[str, dict[str, ChartOptions]] = field(default_factory=dict)  # panel -> chart_key -> options
    removed_panels: list[str] = field(default_factory=list)  # generated panels the user deleted
    edited: dict[str, dict[str, ChartSpec]] = field(default_factory=dict)  # panel -> chart_key -> edited spec

    @property
    def is_empty(self) -> bool:
        return not (
            self.panels or self.added or self.x or self.removed or self.opts
            or self.removed_panels or self.edited
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "panels": list(self.panels),
            "added": {p: [_spec_to_json(c) for c in specs] for p, specs in self.added.items()},
            "x": {p: dict(m) for p, m in self.x.items()},
            "removed": {p: list(keys) for p, keys in self.removed.items()},
            "opts": {p: {k: o.to_json() for k, o in m.items()} for p, m in self.opts.items()},
            "removed_panels": list(self.removed_panels),
            "edited": {p: {k: _spec_to_json(c) for k, c in m.items()} for p, m in self.edited.items()},
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
        stored = data.get("opts")  # absent in files saved before chart options existed
        if isinstance(stored, dict):
            for panel, overrides in stored.items():
                if isinstance(panel, str) and isinstance(overrides, dict):
                    good_opts = {k: ChartOptions.from_json(v) for k, v in overrides.items() if isinstance(k, str)}
                    good_opts = {k: o for k, o in good_opts.items() if not o.is_default}
                    if good_opts:
                        out.opts[panel] = good_opts
        removed = data.get("removed")  # absent in files saved before deleting existed
        if isinstance(removed, dict):
            for panel, keys in removed.items():
                if isinstance(panel, str) and isinstance(keys, list):
                    good_keys = [k for k in keys if isinstance(k, str)]
                    if good_keys:
                        out.removed[panel] = good_keys
        removed_panels = data.get("removed_panels")
        if isinstance(removed_panels, list):
            out.removed_panels = list(dict.fromkeys(p for p in removed_panels if isinstance(p, str) and p))[:MAX_PANELS]
        edited = data.get("edited")
        if isinstance(edited, dict):
            for panel, overrides in edited.items():
                if isinstance(panel, str) and isinstance(overrides, dict):
                    good_edited = {
                        k: spec for k, v in overrides.items()
                        if isinstance(k, str) and (spec := _spec_from_json(v)) is not None
                    }
                    if good_edited:
                        out.edited[panel] = good_edited
        return out


def _resolve_generated(panel: str, c: ChartSpec, custom: Customization) -> ChartSpec:
    k = chart_key(c)
    if k in custom.edited.get(panel, ()):
        return custom.edited[panel][k]
    return replace(
        c,
        x=custom.x.get(panel, {}).get(k, c.x),
        opts=custom.opts.get(panel, {}).get(k, c.opts),
    )


def apply_customization(default: list[Panel], custom: Customization) -> list[Panel]:
    """The default panels plus the user's changes. Never mutates `default`."""
    panels = [
        Panel(
            p.name,
            [
                _resolve_generated(p.name, c, custom)
                for c in p.charts
                if chart_key(c) not in custom.removed.get(p.name, ())
            ],
        )
        for p in default
        if p.name not in custom.removed_panels
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
        """Create a panel (or bring back / find an existing one); returns its index."""
        if name in self.custom.removed_panels:
            self.custom.removed_panels.remove(name)
            self._rebuild()
        if all(p.name != name for p in self.panels) and name not in self._default_by_name:
            self.custom.panels.append(name)
            self._rebuild()
        return self.index_of(name)

    def remove_panel(self, panel_index: int) -> None:
        """Delete a panel. User panels are dropped; generated panels are remembered as removed."""
        name = self.panels[panel_index].name
        if name in self.custom.panels:
            self.custom.panels.remove(name)
        if name in self._default_by_name and name not in self.custom.removed_panels:
            self.custom.removed_panels.append(name)
        for store in (self.custom.added, self.custom.x, self.custom.opts, self.custom.removed, self.custom.edited):
            store.pop(name, None)
        self._rebuild()

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
        """Change a chart (its definition, x-axis and options). Generated charts record overrides
        or a replacement spec; added charts are edited in place."""
        name = self.panels[panel_index].name
        generated = self._visible_generated(name)
        if chart_index < len(generated):
            original = generated[chart_index]
            key = chart_key(original)
            same_shape = (spec.y, spec.more, spec.system, spec.kind) == (
                original.y, original.more, original.system, original.kind
            )
            if same_shape:
                self._override(self.custom.edited, name, key, None, None)
                self._override(self.custom.x, name, key, spec.x, original.x)
                self._override(self.custom.opts, name, key, spec.opts, original.opts)
            else:
                self._override(self.custom.x, name, key, original.x, original.x)
                self._override(self.custom.opts, name, key, original.opts, original.opts)
                self._override(self.custom.edited, name, key, spec, None)
        else:
            self.custom.added[name][chart_index - len(generated)] = spec
        self._rebuild()

    @staticmethod
    def _override(store: dict, panel: str, key: str, value, default) -> None:
        """Record `value` as the override for a generated chart, or forget it if it is the default."""
        overrides = store.setdefault(panel, {})
        if value == default:
            overrides.pop(key, None)
        else:
            overrides[key] = value
        if not overrides:
            del store[panel]

    def remove_chart(self, panel_index: int, chart_index: int) -> None:
        """Delete a chart from a panel. A generated chart is remembered as removed (it would
        otherwise be rebuilt next time); an added chart is simply dropped."""
        name = self.panels[panel_index].name
        generated = self._visible_generated(name)
        if chart_index < len(generated):
            key = chart_key(generated[chart_index])
            self.custom.removed.setdefault(name, []).append(key)
            for store in (self.custom.x, self.custom.opts, self.custom.edited):  # settings of a deleted chart are moot
                overrides = store.get(name)
                if overrides is not None:
                    overrides.pop(key, None)
                    if not overrides:
                        del store[name]
        else:
            added = self.custom.added[name]
            del added[chart_index - len(generated)]
            if not added:
                del self.custom.added[name]
        self._rebuild()

    def reset(self) -> None:
        self.custom = Customization()
        self._rebuild()
