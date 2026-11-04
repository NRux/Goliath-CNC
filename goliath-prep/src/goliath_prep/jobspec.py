# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""job.toml: the job file (unit P01; the project design, sections 4.2 and 4.4; red-team C-01, PHYS-06, PHYS-12).

Every job has a job file next to its G-code. It describes the panel on the
floor and the bit in the router, which the GI header is compared with
(docs/prep/gi-format.md section 10), and what is on the panel. Format,
version 1 (a key not listed is refused, so a declared value can never be
ignored)::

    units = "mm"          # optional; required when the G-code file has no G21 (only "mm")
    z_zero = "top"        # optional: "top" (absent: the stock top, spec 05) or "bottom"

    [panel]               # required
    W = 2440.0            # size along X, mm
    H = 1220.0            # size along Y, mm
    T = 17.6              # thickness, measured with callipers, mm
    tower2 = "Y"          # the axis tower 2 stands on: "Y" = corner (0, H), "X" = corner (W, 0)

    [placement]           # optional; absent: no shift. Both keys when present
    dx = 0.0              # the program's offset on the panel, mm (GI origin=t1-corner agrees only with 0, 0)
    dy = 0.0

    [tool]                # required
    d = 6.0               # cutting diameter, mm
    flute = 22.0          # flute length, mm
    stickout = 40.0       # optional: collet to tip, mm (absent: T-TOOL does not check it)
    rpm = 18000           # optional: spindle speed
    kind = "flat"         # optional: flat | ball | bull | vbit | other
    max_stepdown = 3.0    # optional: the bit's largest step-down, mm (absent: T-PASS is not evaluated)

    [supports]            # optional (relaxation R-SUPPORT): support strip width beyond each side, mm
    left = 0.0            # left (x = 0), right (x = W), bottom (y = 0), top (y = H); absent: no strip
    right = 300.0

    [feeds]               # optional: the owner's caps, only lower than the catalogue's
    feed_cap = 1500.0     # mm/min
    rpm_cap = 20000

    [policy]              # optional: the owner's per-job margins, only tighter than the catalogue's
    obstacle_clearance = 5.0
    overcut_max = 0.3
    delta_tool = 0.3

    [[obstacles]]         # optional, any number: something on the panel (panel frame, mm)
    polygon = [[100.0, 100.0], [180.0, 100.0], [180.0, 160.0], [100.0, 160.0]]
    height_mm = 25.0      # above the stock top
    kind = "clamp"

    [[fixations]]         # optional, any number: a screw or clamp holding the panel
    point = [1200.0, 600.0]
    height_mm = 4.5

    [[prior_removal]]     # optional, any number: material already removed from a panel that is not new
    polygon = [[500.0, 500.0], [700.0, 500.0], [700.0, 650.0]]
    depth_mm = 6.0

    [series]              # optional (series jobs, gated by series_allowed, rung R15)
    prior_material = "../job-1/material-after.json"   # the previous job's material state
    prior_sha256 = "<64 hex digits>"

The names of ``panel``, ``placement``, ``z_zero`` and ``tool`` are those of the
``job`` object of docs/prep/gi-examples/manifest.json, so
:meth:`JobSpec.from_mapping` reads that object as it stands: one reader, one
set of names, no mapping between them.

Coordinates of obstacles, fixations and prior removal are in the panel frame
(origin at the tower-1 corner), not shifted by ``placement``: they are on the
panel, not in the program.

**Values and their sources.** Every value is a
:class:`~goliath_prep.core.params.Param`: ``OWNER_JOB`` with the reference
``job.toml <key>`` when written. A value the file leaves out is a disclosed
default (an info disclosure, and a reference that says it was absent): no
placement shift, Z zero at the stock top (``SPEC``, spec 05 §Semantics a
replacement must match), no support strip. Nothing else has a default: a
missing ``panel.W``, ``H``, ``T``, ``tower2``, ``tool.d`` or ``tool.flute`` is
refused (:class:`~goliath_prep.defaults.MissingValueError`), and so are
missing ``units`` for a G-code file without ``G21``
(:meth:`JobSpec.units_for`). Numbers have at most three decimals, so
``Decimal(repr(value))`` is the decimal as written (GI agreement compares
decimals as written, gi-format section 10). The references name the file's
role, ``job.toml``, whatever the file is called.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from goliath_prep.core.disclosure import Disclosure
from goliath_prep.core.params import Param
from goliath_prep.core.types import TOWER2_AXES, Fixation, Obstacle, PriorCut
from goliath_prep.defaults import (
    SET_T2_POSITION,
    MissingValueError,
    StrictTable,
    catalogue_ref,
    load_toml,
    owner_value,
)

__all__ = [
    "FEED_KEYS",
    "JOB_POLICY_KEYS",
    "LABEL",
    "PANEL_KEYS",
    "PLACEMENT_KEYS",
    "SIDES",
    "TOOL_KEYS",
    "TOOL_KINDS",
    "TOP_LEVEL_KEYS",
    "UNITS",
    "Z_ZEROS",
    "Feeds",
    "JobPolicy",
    "JobSpec",
    "Panel",
    "Placement",
    "SeriesPrior",
    "Supports",
    "Tool",
]

LABEL = "job.toml"
"""The file's role: the references of its values start with it, whatever the file is called."""

TOP_LEVEL_KEYS = ("units", "z_zero", "panel", "placement", "tool", "supports", "feeds", "policy", "obstacles",
                  "fixations", "prior_removal", "series")
PANEL_KEYS = ("W", "H", "T", "tower2")
PLACEMENT_KEYS = ("dx", "dy")
TOOL_KEYS = ("d", "flute", "stickout", "rpm", "kind", "max_stepdown")
TOOL_KINDS = ("flat", "ball", "bull", "vbit", "other")
Z_ZEROS = ("top", "bottom")
UNITS = ("mm",)
SIDES = ("left", "right", "bottom", "top")
FEED_KEYS = ("feed_cap", "rpm_cap")
JOB_POLICY_KEYS = ("obstacle_clearance", "overcut_max", "delta_tool")
_OBSTACLE_KEYS = ("polygon", "height_mm", "kind")
_FIXATION_KEYS = ("point", "height_mm")
_PRIOR_KEYS = ("polygon", "depth_mm")
_SERIES_KEYS = ("prior_material", "prior_sha256")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _ref(key: str) -> str:
    return "%s %s" % (LABEL, key)


def _check(owner: object, name: str, unit: str, *, optional: bool = False) -> None:
    """A field of ``owner`` holds a Param of ``unit`` (or None where optional): never a bare number."""
    value = getattr(owner, name)
    where = "%s.%s" % (type(owner).__name__, name)
    if value is None and optional:
        return
    if not isinstance(value, Param):
        raise TypeError("%s must be a Param (a value with its source), got %r" % (where, value))
    if value.unit != unit:
        raise ValueError("%s must be in %s, got %s" % (where, unit, value.unit))


@dataclass(frozen=True)
class Panel:
    """The panel: ``W`` along X, ``H`` along Y, thickness ``T`` (callipers), and the axis tower 2 stands on."""

    W: Param[float]
    H: Param[float]
    T: Param[float]
    tower2: Param[str]

    def __post_init__(self) -> None:
        for name in ("W", "H", "T"):
            _check(self, name, "mm")
        _check(self, "tower2", "enum")
        if self.tower2.value not in TOWER2_AXES:
            raise ValueError("Panel.tower2 must be X or Y, got %r" % (self.tower2.value,))

    @property
    def along_x(self) -> bool:
        """True when tower 2 stands on the X axis (corner (W, 0)), False on the Y axis (corner (0, H))."""
        return self.tower2.value == "X"

    @property
    def set_t2_position(self) -> int:
        """The number of ``SET T2 POSITION;<n>``: 1 for X, 0 for Y (spec 02 and spec 04)."""
        return SET_T2_POSITION[self.tower2.value]


@dataclass(frozen=True)
class Placement:
    """The program's offset on the panel (``[placement] dx, dy``), mm."""

    dx: Param[float]
    dy: Param[float]

    def __post_init__(self) -> None:
        _check(self, "dx", "mm")
        _check(self, "dy", "mm")


@dataclass(frozen=True)
class Tool:
    """The one tool: diameter ``d`` and ``flute`` length are required, the rest optional (None when absent)."""

    d: Param[float]
    flute: Param[float]
    stickout: Param[float] | None
    rpm: Param[float] | None
    kind: Param[str] | None = None
    max_stepdown: Param[float] | None = None

    def __post_init__(self) -> None:
        _check(self, "d", "mm")
        _check(self, "flute", "mm")
        _check(self, "stickout", "mm", optional=True)
        _check(self, "rpm", "rpm", optional=True)
        _check(self, "kind", "enum", optional=True)
        _check(self, "max_stepdown", "mm", optional=True)


@dataclass(frozen=True)
class Supports:
    """Support strip width beyond each panel side (relaxation R-SUPPORT), mm; 0 = no strip."""

    left: Param[float]
    right: Param[float]
    bottom: Param[float]
    top: Param[float]

    def __post_init__(self) -> None:
        for side in SIDES:
            _check(self, side, "mm")


@dataclass(frozen=True)
class Feeds:
    """The owner's feed and spindle caps for this job, each lower than the catalogue's, or None."""

    feed_cap: Param[float] | None
    rpm_cap: Param[float] | None

    def __post_init__(self) -> None:
        _check(self, "feed_cap", "mm/min", optional=True)
        _check(self, "rpm_cap", "rpm", optional=True)


@dataclass(frozen=True)
class JobPolicy:
    """The owner's per-job margins, each tighter than the catalogue's, or None."""

    obstacle_clearance: Param[float] | None
    overcut_max: Param[float] | None
    delta_tool: Param[float] | None

    def __post_init__(self) -> None:
        for name in JOB_POLICY_KEYS:
            _check(self, name, "mm", optional=True)


@dataclass(frozen=True)
class SeriesPrior:
    """The previous job's material state, for a series job: its path as written and its SHA-256."""

    material: str
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.material, str) or not self.material.strip():
            raise ValueError("SeriesPrior.material must be a path as text, got %r" % (self.material,))
        if not isinstance(self.sha256, str) or not _SHA256.match(self.sha256):
            raise ValueError("SeriesPrior.sha256 must be 64 lowercase hex digits, got %r" % (self.sha256,))


@dataclass(frozen=True)
class JobSpec:
    """The job file, read (see the module documentation)."""

    panel: Panel
    supports: Supports
    tool: Tool
    feeds: Feeds
    policy: JobPolicy
    units: Param[str] | None
    obstacles: tuple[Obstacle, ...]
    fixations: tuple[Fixation, ...]
    prior_removal: tuple[PriorCut, ...]
    series_prior: SeriesPrior | None
    placement: Placement
    z_zero: Param[str]

    def __post_init__(self) -> None:
        for name, kind in (("panel", Panel), ("supports", Supports), ("tool", Tool), ("feeds", Feeds),
                           ("policy", JobPolicy), ("placement", Placement)):
            if not isinstance(getattr(self, name), kind):
                raise TypeError("JobSpec.%s must be a %s, got %r" % (name, kind.__name__, getattr(self, name)))
        _check(self, "units", "enum", optional=True)
        _check(self, "z_zero", "enum")
        if self.z_zero.value not in Z_ZEROS:
            raise ValueError("JobSpec.z_zero must be top or bottom, got %r" % (self.z_zero.value,))
        for name, kind in (("obstacles", Obstacle), ("fixations", Fixation), ("prior_removal", PriorCut)):
            items = getattr(self, name)
            if not isinstance(items, tuple) or not all(isinstance(i, kind) for i in items):
                raise TypeError("JobSpec.%s must be a tuple of %s, got %r" % (name, kind.__name__, items))
        if self.series_prior is not None and not isinstance(self.series_prior, SeriesPrior):
            raise TypeError("JobSpec.series_prior must be a SeriesPrior or None, got %r" % (self.series_prior,))

    @classmethod
    def load(cls, path: str | Path) -> JobSpec:
        """Read job.toml at ``path``. Raises :class:`~goliath_prep.defaults.InputFileError` (and its kinds)."""
        return cls.from_mapping(load_toml(path, label=LABEL), path=str(path))

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any], *, path: str | None = None) -> JobSpec:
        """Read a job from a mapping with job.toml's layout.

        The mapping is what ``tomllib`` returns, or the ``job`` object of
        docs/prep/gi-examples/manifest.json, which uses job.toml's names. The
        same rules apply: unknown keys and missing required values refuse.
        """
        return _read(document, path)

    def units_for(self, *, file_has_g21: bool) -> Param[str]:
        """The units of a G-code file: job.toml's ``units``, else the file's ``G21``, else refused.

        Raises :class:`~goliath_prep.defaults.MissingValueError` when job.toml
        has no ``units`` and the file has no ``G21`` (the reader reports it as
        ``D-UNITS``). A GI file states its units in its ``UNITS`` line.
        """
        if self.units is not None:
            return self.units
        if file_has_g21:
            return Param("mm", "enum", "OWNER_JOB", "the G-code file's G21 (millimetres)")
        raise MissingValueError(
            'missing: the G-code file has no G21, so job.toml must say units = "mm" (or set the CAM to write G21)',
            label=LABEL, key="units")

    def params(self) -> dict[str, Param[Any]]:
        """Every value of the job as a Param, by name (catalogue names where the catalogue has one)."""
        out: dict[str, Param[Any]] = {
            "panel_width": self.panel.W,
            "panel_height": self.panel.H,
            "stock_thickness": self.panel.T,
            "tower2_axis": self.panel.tower2,
            "placement_dx": self.placement.dx,
            "placement_dy": self.placement.dy,
            "z_zero": self.z_zero,
            "tool_diameter": self.tool.d,
            "tool_flute_length": self.tool.flute,
        }
        optional = {
            "units": self.units,
            "tool_stickout": self.tool.stickout,
            "tool_rpm": self.tool.rpm,
            "tool_kind": self.tool.kind,
            "tool_max_stepdown": self.tool.max_stepdown,
            "feed_cap": self.feeds.feed_cap,
            "rpm_cap": self.feeds.rpm_cap,
            "obstacle_clearance": self.policy.obstacle_clearance,
            "overcut_max": self.policy.overcut_max,
            "delta_tool": self.policy.delta_tool,
        }
        out.update((name, param) for name, param in optional.items() if param is not None)
        for side in SIDES:
            out["support_strip_width_%s" % side] = getattr(self.supports, side)
        return out

    def disclosures(self) -> tuple[Disclosure, ...]:
        """Every disclosure the job's values carry, each once, in a stable order."""
        seen: dict[Disclosure, None] = {}
        params = self.params()
        for name in sorted(params):
            for d in params[name].all_disclosures():
                seen.setdefault(d, None)
        return tuple(seen)

    def to_json(self) -> dict[str, Any]:
        def p(param: Param[Any] | None) -> dict[str, Any] | None:
            return None if param is None else param.to_json()

        return {
            "panel": {key: p(getattr(self.panel, key)) for key in PANEL_KEYS},
            "placement": {key: p(getattr(self.placement, key)) for key in PLACEMENT_KEYS},
            "z_zero": p(self.z_zero),
            "units": p(self.units),
            "tool": {key: p(getattr(self.tool, key)) for key in TOOL_KEYS},
            "supports": {side: p(getattr(self.supports, side)) for side in SIDES},
            "feeds": {key: p(getattr(self.feeds, key)) for key in FEED_KEYS},
            "policy": {key: p(getattr(self.policy, key)) for key in JOB_POLICY_KEYS},
            "obstacles": [o.to_json() for o in self.obstacles],
            "fixations": [f.to_json() for f in self.fixations],
            "prior_removal": [c.to_json() for c in self.prior_removal],
            "series_prior": None if self.series_prior is None else {
                "material": self.series_prior.material, "sha256": self.series_prior.sha256},
        }


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


def _written(value: float | str, unit: str, key: str) -> Param[Any]:
    return Param(value, unit, "OWNER_JOB", _ref(key))


def _read(document: Mapping[str, Any], path: str | None) -> JobSpec:
    top = StrictTable(document, TOP_LEVEL_KEYS, label=LABEL, path=path)

    units_value = top.choice("units", UNITS)
    units = None if units_value is None else _written(units_value, "enum", "units")
    z_zero = _z_zero(top.choice("z_zero", Z_ZEROS))

    panel_table = top.table("panel", PANEL_KEYS, required=True,
                            why="Describe the panel under [panel]: W, H, T (callipers) and tower2")
    assert panel_table is not None
    panel = Panel(
        W=_written(_number(panel_table, "W", "the panel's size along X, mm"), "mm", "panel.W"),
        H=_written(_number(panel_table, "H", "the panel's size along Y, mm"), "mm", "panel.H"),
        T=_written(_number(panel_table, "T", "the panel's thickness, measured with callipers, mm"), "mm", "panel.T"),
        tower2=_written(panel_table.choice(  # type: ignore[arg-type]
            "tower2", TOWER2_AXES, required=True,
            why='the axis tower 2 stands on: "Y" (corner 0, H) or "X" (corner W, 0)'), "enum", "panel.tower2"),
    )
    panel_table.done()

    placement = _placement(top.table("placement", PLACEMENT_KEYS))
    tool = _tool(top.table("tool", TOOL_KEYS, required=True,
                           why="Describe the bit under [tool]: d (diameter) and flute (flute length), in mm"))
    supports = _supports(top.table("supports", SIDES))
    feeds_table = top.table("feeds", FEED_KEYS)
    feeds = Feeds(*(_owner(feeds_table, name, "feeds") for name in FEED_KEYS))
    if feeds_table is not None:
        feeds_table.done()
    policy_table = top.table("policy", JOB_POLICY_KEYS)
    policy = JobPolicy(*(_owner(policy_table, name, "policy") for name in JOB_POLICY_KEYS))
    if policy_table is not None:
        policy_table.done()

    obstacles = []
    for table in top.tables("obstacles", _OBSTACLE_KEYS):
        polygon = table.points("polygon", required=True, why="the obstacle's footprint, [[x, y], ...] in mm")
        height = table.number("height_mm", required=True, above=0.0, why="its height above the stock top, mm")
        kind = table.text("kind", required=True, why='what it is, for example "clamp"')
        table.done()
        obstacles.append(_entry(table, Obstacle, polygon, height, kind, ref=_ref(table.prefix)))
    fixations = []
    for table in top.tables("fixations", _FIXATION_KEYS):
        point = table.point("point", required=True, why="where it is, [x, y] in mm")
        height = table.number("height_mm", required=True, above=0.0, why="its height above the stock top, mm")
        table.done()
        fixations.append(_entry(table, Fixation, point, height, ref=_ref(table.prefix)))
    prior = []
    for table in top.tables("prior_removal", _PRIOR_KEYS):
        polygon = table.points("polygon", required=True, why="the removed area, [[x, y], ...] in mm")
        depth = table.number("depth_mm", required=True, above=0.0, why="its depth below the stock top, mm")
        table.done()
        prior.append(_entry(table, PriorCut, polygon, depth, ref=_ref(table.prefix)))

    series_table = top.table("series", _SERIES_KEYS)
    series = None
    if series_table is not None:
        why = "a series job names the previous job's material state and its SHA-256"
        material = series_table.text("prior_material", required=True, why=why)
        digest = series_table.sha256("prior_sha256", required=True, why=why)
        series_table.done()
        series = SeriesPrior(material=material, sha256=digest)  # type: ignore[arg-type]
    top.done()

    return JobSpec(panel=panel, supports=supports, tool=tool, feeds=feeds, policy=policy, units=units,
                   obstacles=tuple(obstacles), fixations=tuple(fixations), prior_removal=tuple(prior),
                   series_prior=series, placement=placement, z_zero=z_zero)


def _number(table: StrictTable, key: str, what: str) -> float:
    value = table.number(key, required=True, above=0.0, why="Write %s as %s under [%s]" % (what, key, table.prefix))
    assert value is not None
    return value


def _entry(table: StrictTable, kind: type, *args: Any, ref: str) -> Any:
    try:
        return kind(*args, ref=ref)
    except (TypeError, ValueError) as exc:
        raise table.error(None, str(exc)) from exc


def _z_zero(value: str | None) -> Param[str]:
    if value is None:
        note = Disclosure(id="DEFAULT:job.z_zero", severity="info",
                          text="job.toml has no z_zero: Z = 0 is the stock top, as spec 05 (Semantics a replacement "
                               "must match) has it", source=LABEL)
        return Param("top", "enum", "SPEC", "%s z_zero (absent: spec 05 §Semantics a replacement must match, Z = 0 "
                                            "at the stock top)" % LABEL, disclosures=(note,))
    if value == "bottom":
        note = Disclosure(id="JOB:z_zero=bottom", severity="info",
                          text="job.toml says Z = 0 is the stock bottom: Z values are shifted by -T before the checks",
                          source=_ref("z_zero"))
        return Param(value, "enum", "OWNER_JOB", _ref("z_zero"), disclosures=(note,))
    return _written(value, "enum", "z_zero")


def _placement(table: StrictTable | None) -> Placement:
    if table is None:
        note = Disclosure(id="DEFAULT:job.placement", severity="info",
                          text="job.toml has no [placement]: the program is not shifted on the panel (dx = dy = 0 mm)",
                          source=LABEL)
        return Placement(*(Param(0.0, "mm", "OWNER_JOB", "%s placement.%s (absent: no shift; %s)"
                                 % (LABEL, key, catalogue_ref("placement_%s" % key)), disclosures=(note,))
                           for key in PLACEMENT_KEYS))
    why = "[placement] needs both dx and dy (remove the table for no shift)"
    dx = table.number("dx", required=True, why=why)
    dy = table.number("dy", required=True, why=why)
    table.done()
    assert dx is not None and dy is not None
    if dx == 0 and dy == 0:
        return Placement(_written(dx, "mm", "placement.dx"), _written(dy, "mm", "placement.dy"))
    note = Disclosure(id="JOB:placement", severity="info",
                      text="the program is shifted on the panel by dx = %s mm, dy = %s mm (job.toml [placement])"
                      % (dx, dy), source=_ref("placement"))
    return Placement(Param(dx, "mm", "OWNER_JOB", _ref("placement.dx"), disclosures=(note,)),
                     Param(dy, "mm", "OWNER_JOB", _ref("placement.dy"), disclosures=(note,)))


def _tool(table: StrictTable | None) -> Tool:
    assert table is not None
    d = _number(table, "d", "the bit's cutting diameter, mm")
    flute = _number(table, "flute", "the bit's flute length, mm")
    stickout = table.number("stickout", above=0.0)
    rpm = table.number("rpm", above=0.0)
    kind = table.choice("kind", TOOL_KINDS)
    stepdown = table.number("max_stepdown", above=0.0)
    table.done()
    return Tool(
        d=_written(d, "mm", "tool.d"),
        flute=_written(flute, "mm", "tool.flute"),
        stickout=None if stickout is None else _written(stickout, "mm", "tool.stickout"),
        rpm=None if rpm is None else _written(rpm, "rpm", "tool.rpm"),
        kind=None if kind is None else _written(kind, "enum", "tool.kind"),
        max_stepdown=None if stepdown is None else _written(stepdown, "mm", "tool.max_stepdown"),
    )


def _supports(table: StrictTable | None) -> Supports:
    values = {side: None if table is None else table.number(side, at_least=0.0) for side in SIDES}
    if table is not None:
        table.done()
    absent = [side for side in SIDES if values[side] is None]
    note = None if not absent else Disclosure(
        id="DEFAULT:job.supports", severity="info",
        text="job.toml declares no support strip on the %s side%s: no wheel may leave the panel there"
        % (", ".join(absent), "" if len(absent) == 1 else "s"), source=LABEL)
    out = {}
    for side in SIDES:
        value = values[side]
        if value is None:
            out[side] = Param(0.0, "mm", "OWNER_JOB", "%s supports.%s (absent: no support strip; %s)"
                              % (LABEL, side, catalogue_ref("support_strip_width")), disclosures=(note,))
        else:
            out[side] = _written(value, "mm", "supports.%s" % side)
    return Supports(**out)


def _owner(table: StrictTable | None, name: str, section: str) -> Param[float] | None:
    if table is None:
        return None
    value = table.number(name)
    if value is None:
        return None
    key = "%s.%s" % (section, name)
    return owner_value(name, value, source="OWNER_JOB", ref=_ref(key), label=LABEL, path=table.path, key=key)
