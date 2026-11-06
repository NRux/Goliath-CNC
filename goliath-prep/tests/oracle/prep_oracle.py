# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""An independent brute-force oracle for goliath-prep's safety checker (unit P21; test code only).

The project design (section 10.4) asks for a second checker written apart
from the first: it samples every path at 1 mm and every wheel disc at 16 rim
points plus the centre, uses the exact triangle test of spec 08, point-to-
segment distances for removed material and its own connectivity on a 2 mm
grid for attached material. **Anything the real checker accepts, the oracle
must accept**: the oracle tests the safety model's regions at sample points
only, so every violation it reports lies inside a region the checker has to
keep clear, and a job it refuses that the checker certified is a defect of one
of them (test_prep_oracle_cross.py).

**Independence (red-team C-06).** This module imports nothing of goliath-prep.
It reads job.toml and machine.toml with :mod:`tomllib` and the parameter
catalogue docs/prep/params.json with :mod:`json`, and derives what the
catalogue derives (R_rot, rho, r_min, r_max, R_body, the piece length) from
the support geometry itself. The program it checks is goliath-prep's
normalised program (unit P03), read by its attributes only (cuts, links,
moves with Decimal coordinates). The gates are a plain mapping, the
``gates`` object of a gate snapshot file (gate name to
``{"allowed": true | false | null, "value": ...}``); only ``true`` opens a
gate, so no unknown is read as "yes".

**Two modes.**

* ``plan`` (GI and generic G-code): the planner chooses the headings and the
  links, so the oracle searches them itself: every heading of an unforced cut
  (the forced one only, when the GI file forces one), each link as goliath-
  prep's link template writes it (safety-model 9.1: a straight translation
  between the cuts at Z_safety, the rotation at either end, or one combined
  line when rotation in place is not allowed), the first line from the start
  pose, and the wheels where the program ends. The job passes when some
  choice passes everything (a dynamic programme over the cuts); any choice
  the checker can make is among them.
* ``verify`` (Goliath dialect): every heading and link as written.

**Laxer where it cannot be exact.** Where the oracle cannot reproduce a rule
exactly it takes the reading that refuses less, and says so in the verdict's
notes, never silently: the grid structure is read so that it can only find
less detached material than the exact one; a link is accepted when either
pivot passes; an envelope is tested along one turning direction; a
measured-panel hypothesis other than the identity counts only when it passes
by a clear margin. Checks the oracle does not evaluate at all are listed in
:data:`NOT_EVALUATED` and in every verdict's notes. A violation must exceed
:data:`HIT_TOL_MM`, which also covers the emitter's 0.02 mm arc tolerance.

**Time.** The tool's path is sampled at most :data:`SAMPLE_MM` apart, on a
grid of path length along each cut, so that splitting a move only adds
samples. At sample i, the removed material is everything the tool passed
before it, prior removal included: physically exact, and never more than the
checker's Rem(j) at a wheel (safety-model 7.3).
"""
from __future__ import annotations

import json
import math
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

__all__ = [
    "GATES",
    "GRID_MM",
    "HIT_TOL_MM",
    "NOT_EVALUATED",
    "RIM_POINTS",
    "SAMPLE_MM",
    "OracleError",
    "OracleHit",
    "OracleParams",
    "OracleVerdict",
    "Support",
    "World",
    "build_world",
    "in_triangle_spec",
    "oracle_check",
    "resolve_params",
    "support_offsets",
]

SAMPLE_MM = 1.0
"""Largest distance between two samples of the tool's path, mm."""
RIM_POINTS = 16
"""Points on the rim of each disc looked up on the structure grid (plus its centre)."""
GRID_MM = 2.0
"""Spacing of the oracle's own structure grid, mm."""
GRID_SHRINK_MM = 2.0
"""Holding removal is shrunk by this much on the grid, so the grid connects wherever the exact model does."""
HIT_TOL_MM = 0.05
"""A violation counts only when it exceeds this, mm (float noise, and the emitter's 0.02 mm arc tolerance)."""
CHECKPOINT_MM = 25.0
"""Within a cut, attached material is recomputed at least this often (mm of path), and at every move's end."""
ROT_STEP_DEG = 0.25
"""Heading step when a turn is swept along its arc (1.75 mm at the 402 mm rear support)."""
HITS_PER_GROUP = 3
"""Violations kept per check, rule, wheel and stage (the first ones and the deepest)."""

GATES: tuple[str, ...] = (
    "jobs_allowed", "panel_vertices_usable", "start_pose_known", "heading_map_verified", "rotation_model",
    "rotate_in_place_ok", "z_semantics_verified", "post_job_motion_known", "spindle_verified", "drop_dwells",
    "first_cut_done", "arcs_emit", "through_cuts_allowed", "tabs_hold", "kerf_crossing", "pockets_allowed",
    "near_edge_allowed", "max_program_bytes", "m11_emit", "series_allowed", "wire_attachment_measured")
"""The gates of safety-model 1.4, in its order."""

UNLOCK_RUNG: Mapping[str, str] = {
    "jobs_allowed": "R1", "panel_vertices_usable": "R2", "start_pose_known": "R2", "heading_map_verified": "R3",
    "rotation_model": "R3", "rotate_in_place_ok": "R3", "z_semantics_verified": "R3", "post_job_motion_known": "R3",
    "spindle_verified": "R4", "drop_dwells": "R4", "first_cut_done": "R5", "arcs_emit": "R6",
    "through_cuts_allowed": "R7", "tabs_hold": "R7", "kerf_crossing": "R8k", "pockets_allowed": "R8",
    "near_edge_allowed": "R9", "max_program_bytes": "R10", "m11_emit": "R11", "series_allowed": "R15",
    "wire_attachment_measured": "R0b"}

ROTATION_MODELS = ("in-place-shortest", "in-place-numeric", "during-translation")
EDGES = {"x_min": "left", "x_max": "right", "y_min": "bottom", "y_max": "top"}
SIDES = ("left", "right", "bottom", "top")
SIDE_DIRECTION = {"left": (-1.0, 0.0), "right": (1.0, 0.0), "bottom": (0.0, -1.0), "top": (0.0, 1.0)}

NOT_EVALUATED: Mapping[str, str] = {
    "D-FEED": "feeds and speeds are the emitter's (re-parsed bytes)",
    "D-WORDS": "the upload's vocabulary and cleaned form are not seen by the oracle",
    "D-ROUNDTRIP": "the oracle has no plan to compare the bytes with",
    "D-SIZE": "the oracle has no upload bytes",
    "F-GATE": "firmware versions are the runner's, at job time",
    "S-SERIES": "series jobs are not modelled",
    "W-VENDORFLOOR": "the vendor reference is the checker's own assertion",
    "T-PASS": "a warning, not a refusal",
    "N-*": "notices refuse nothing",
    "G-GATE jobs_allowed, max_program_bytes, m11_emit, series_allowed, drop_dwells":
        "the runner's firmware gate, the upload size, M11, series and G4 (refused by the reader) are not seen here",
    "D-CSTEP (X or Y and C on one line)": "whether a line may be combined is the emit profile's; the oracle checks "
                                          "the C grid, C on feed lines, the 180-degree step and the cumulative turn",
}
"""The checks the oracle does not evaluate, and why: a job the checker refuses only for these, the oracle accepts."""

_BIG = np.int64(2 ** 62)
_RIM = np.stack([np.cos(np.arange(RIM_POINTS) * 2.0 * math.pi / RIM_POINTS),
                 np.sin(np.arange(RIM_POINTS) * 2.0 * math.pi / RIM_POINTS)], axis=1)
_HALF_DIAGONAL = GRID_MM / math.sqrt(2.0) * 1.0001


class OracleError(Exception):
    """The oracle cannot check this job: a malformed input, or a case it does not model. Never a verdict."""


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OracleHit:
    """One violation the oracle found.

    ``check`` is the safety model's check id; ``rule`` the failed rule of a
    composite check (W-ROT, W-RAPID, W-FIRST, W-PARK, W-SAFEZONE) or the gate
    of a G-GATE. ``where`` is ``cut``, ``link``, ``first``, ``end``,
    ``program``, ``job-time`` or ``reader``. ``point`` is the wheel centre (or
    tool point) where it was found, ``time`` its sample index, ``depth_mm``
    how far into the keep-out (None for a grid or program rule).
    """

    check: str
    rule: str | None = None
    where: str = "program"
    cut: int | None = None
    wheel: int | None = None
    point: tuple[float, float] | None = None
    time: int | None = None
    heading_c: int | None = None
    line: int | None = None
    depth_mm: float | None = None
    detail: str = ""

    def describe(self) -> str:
        parts = [self.check]
        if self.rule:
            parts.append("rule=%s" % self.rule)
        parts.append(self.where)
        for name in ("cut", "wheel", "heading_c", "line", "time"):
            value = getattr(self, name)
            if value is not None:
                parts.append("%s=%s" % (name, value))
        if self.point is not None:
            parts.append("at (%.1f, %.1f)" % self.point)
        if self.depth_mm is not None:
            parts.append("by %.2f mm" % self.depth_mm)
        if self.detail:
            parts.append("- " + self.detail)
        return " ".join(parts)


@dataclass(frozen=True)
class OracleVerdict:
    """The oracle's answer: ``ok`` is True only when it found no violation at all."""

    ok: bool
    violations: list[OracleHit]
    notes: tuple[str, ...]
    mode: str
    headings: Mapping[int, int] | None = None
    table: Mapping[int, Mapping[int, tuple[str, ...]]] = field(default_factory=dict)
    params: Any = None

    def check_ids(self) -> set[str]:
        return {h.check for h in self.violations}


# ---------------------------------------------------------------------------
# Reading the three files
# ---------------------------------------------------------------------------


def _load_toml(path: Path, what: str) -> dict[str, Any]:
    try:
        with Path(path).open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        raise OracleError("%s %s does not exist" % (what, path)) from exc
    except tomllib.TOMLDecodeError as exc:
        raise OracleError("%s %s is not valid TOML: %s" % (what, path, exc)) from exc


def _keys(table: Any, allowed: Sequence[str], what: str) -> dict[str, Any]:
    if not isinstance(table, dict):
        raise OracleError("%s must be a table, got %r" % (what, table))
    unknown = sorted(set(table) - set(allowed))
    if unknown:
        raise OracleError("%s has keys the oracle does not know: %s (a declared value is never ignored)"
                          % (what, ", ".join(unknown)))
    return table


def _number(table: Mapping[str, Any], key: str, what: str, *, required: bool = True) -> float | None:
    if key not in table:
        if required:
            raise OracleError("%s.%s is required" % (what, key))
        return None
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise OracleError("%s.%s must be a number, got %r" % (what, key, value))
    return float(value)


def _measured(table: Mapping[str, Any], key: str, what: str) -> float | None:
    """A machine.toml reading: a number, or the word "unknown" (or absent), which is None."""
    if key not in table or table[key] == "unknown":
        return None
    return _number(table, key, what)


def _catalogue(params_json: Path) -> dict[str, Mapping[str, Any]]:
    try:
        data = json.loads(Path(params_json).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OracleError("the parameter catalogue %s does not exist" % params_json) from exc
    except json.JSONDecodeError as exc:
        raise OracleError("the parameter catalogue %s is not JSON: %s" % (params_json, exc)) from exc
    if not isinstance(data, list) or not all(isinstance(e, dict) and isinstance(e.get("name"), str) for e in data):
        raise OracleError("the parameter catalogue %s is not a list of named parameters" % params_json)
    return {entry["name"]: entry for entry in data}


def _cat(catalogue: Mapping[str, Mapping[str, Any]], name: str) -> float:
    """A number from the catalogue's ``default``; a missing or non-numeric one is an error, never a guess."""
    entry = catalogue.get(name)
    if entry is None:
        raise OracleError("docs/prep/params.json has no parameter %s" % name)
    value = entry.get("default")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OracleError("docs/prep/params.json %s has no numeric default (%r)" % (name, value))
    return float(value)


def _cat_after_unlock(catalogue: Mapping[str, Mapping[str, Any]], name: str) -> float:
    entry = catalogue.get(name) or {}
    try:
        return float(entry["after_unlock"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OracleError("docs/prep/params.json %s has no numeric after_unlock value" % name) from exc


class _Gates:
    """The gate snapshot: only ``allowed is True`` opens a gate; an absent snapshot closes every gate."""

    def __init__(self, gates: Mapping[str, Any] | None) -> None:
        self.given = gates is not None
        entries: Mapping[str, Any] = {}
        if gates is not None:
            if not isinstance(gates, Mapping):
                raise OracleError("the gate snapshot must be a mapping, got %r" % (gates,))
            inner = gates.get("gates")
            entries = inner if isinstance(inner, Mapping) else gates
        self.entries: dict[str, Mapping[str, Any]] = {}
        for name, entry in entries.items():
            if not isinstance(entry, Mapping) or entry.get("allowed") not in (True, False, None):
                raise OracleError("gate %s must be {\"allowed\": true | false | null, ...}, got %r" % (name, entry))
            self.entries[name] = entry

    def open(self, name: str) -> bool:
        entry = self.entries.get(name)
        return entry is not None and entry.get("allowed") is True

    def value(self, name: str) -> Any:
        return self.entries[name].get("value") if self.open(name) else None


@dataclass(frozen=True)
class Support:
    """One support position: wheel 1 rear, 2 right, 3 left; distance ``r`` and angle ``delta`` (safety-model 4.2)."""

    wheel: int
    r: float
    delta: float
    model: str


class OracleParams:
    """Every number the oracle uses, each with where it came from (``source``), and what it must disclose about
    them (``notes``, carried into every verdict)."""

    def __init__(self) -> None:
        self.source: dict[str, str] = {}
        self.notes: list[str] = []

    def set(self, name: str, value: Any, source: str) -> None:
        setattr(self, name, value)
        self.source[name] = source


_JOB_KEYS = ("units", "z_zero", "panel", "placement", "tool", "supports", "feeds", "policy", "obstacles",
             "fixations", "prior_removal", "series")
_MACHINE_KEYS = ("profile_version", "serial", "footprint", "wheel", "body", "tower", "wire", "stop", "rf", "policy",
                 "export")
_MACHINE_POLICY = {"m_w": "max", "m_edge": "max", "m_edge_baseline": "max", "m_edge_tool": "max", "m_tower": "max",
                   "reach_max": "min", "battery_ack_below": "max", "battery_ack_job_min": "min"}


def _tighter(value: float, catalogue_value: float, rule: str, name: str, where: str) -> float:
    if (rule == "max" and value < catalogue_value) or (rule == "min" and value > catalogue_value):
        raise OracleError("%s %s = %s is looser than the catalogue's %s: owner policy only tightens"
                          % (where, name, value, catalogue_value))
    return value


def _polygon(value: Any, what: str) -> np.ndarray:
    if not isinstance(value, list) or len(value) < 3:
        raise OracleError("%s must be a list of at least three [x, y] points" % what)
    pts = []
    for p in value:
        if not isinstance(p, list) or len(p) != 2 or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in p):
            raise OracleError("%s has a point that is not [x, y]: %r" % (what, p))
        pts.append((float(p[0]), float(p[1])))
    return _ccw(np.array(pts, dtype=float))


def _point(value: Any, what: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2 or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        raise OracleError("%s must be [x, y], got %r" % (what, value))
    return float(value[0]), float(value[1])


def resolve_params(job_toml: Path, machine_toml: Path, params_json: Path, gates: Mapping[str, Any] | None = None,
                   ) -> OracleParams:
    """Every parameter the oracle uses, from the three files and the gate snapshot (red-team C-06)."""
    cat = _catalogue(params_json)
    job = _keys(_load_toml(job_toml, "job.toml"), _JOB_KEYS, "job.toml")
    machine = _keys(_load_toml(machine_toml, "machine.toml"), _MACHINE_KEYS, "machine.toml")
    g = _Gates(gates)
    p = OracleParams()
    p.gates = g

    # --- the job -----------------------------------------------------------------------------------------------
    if "series" in job:
        raise OracleError("job.toml [series]: series jobs are not modelled by the oracle")
    panel = _keys(job.get("panel"), ("W", "H", "T", "tower2"), "job.toml panel") if "panel" in job else None
    if panel is None:
        raise OracleError("job.toml panel is required")
    for key in ("W", "H", "T"):
        p.set(key, _number(panel, key, "job.toml panel"), "job.toml panel.%s (OWNER_JOB)" % key)
    tower2 = panel.get("tower2")
    if tower2 not in ("X", "Y"):
        raise OracleError("job.toml panel.tower2 must be \"X\" or \"Y\", got %r" % (tower2,))
    p.set("tower2", tower2, "job.toml panel.tower2 (OWNER_JOB)")
    if "tool" not in job:
        raise OracleError("job.toml tool is required")
    tool = _keys(job["tool"], ("d", "flute", "stickout", "rpm", "kind", "max_stepdown"), "job.toml tool")
    p.set("tool_d", _number(tool, "d", "job.toml tool"), "job.toml tool.d (OWNER_JOB)")
    p.set("flute", _number(tool, "flute", "job.toml tool"), "job.toml tool.flute (OWNER_JOB)")
    p.set("stickout", _number(tool, "stickout", "job.toml tool", required=False),
          "job.toml tool.stickout (OWNER_JOB; absent: not checked)")
    policy = _keys(job.get("policy", {}), ("obstacle_clearance", "overcut_max", "delta_tool"), "job.toml policy")
    for name, base, rule in (("obstacle_clearance", _cat(cat, "obstacle_clearance"), "max"),
                             ("overcut_max", _cat(cat, "overcut_max"), "min"),
                             ("delta_tool", _cat(cat, "delta_tool"), "max")):
        value = _number(policy, name, "job.toml policy", required=False)
        if value is None:
            p.set(name, base, "params.json %s (catalogue default)" % name)
        else:
            p.set(name, _tighter(value, base, rule, name, "job.toml policy"), "job.toml policy.%s (OWNER_JOB)" % name)
    p.set("r_tool", p.tool_d / 2.0, "derived: job.toml tool.d / 2")
    p.set("r_cap", p.r_tool + p.delta_tool, "derived: r_tool + delta_tool (removal and tool disc radius)")
    supports_t = _keys(job.get("supports", {}), SIDES, "job.toml supports")
    p.set("strips", {s: (_number(supports_t, s, "job.toml supports", required=False) or 0.0) for s in SIDES},
          "job.toml supports (R-SUPPORT; absent: no strip)")
    obstacles = []
    for i, entry in enumerate(job.get("obstacles", [])):
        e = _keys(entry, ("polygon", "height_mm", "kind"), "job.toml obstacles[%d]" % i)
        obstacles.append((_polygon(e.get("polygon"), "job.toml obstacles[%d].polygon" % i),
                          _number(e, "height_mm", "job.toml obstacles[%d]" % i)))
    p.set("obstacles", obstacles, "job.toml obstacles")
    fixations = []
    for i, entry in enumerate(job.get("fixations", [])):
        e = _keys(entry, ("point", "height_mm"), "job.toml fixations[%d]" % i)
        fixations.append((_point(e.get("point"), "job.toml fixations[%d].point" % i),
                          _number(e, "height_mm", "job.toml fixations[%d]" % i)))
    p.set("fixations", fixations, "job.toml fixations (also the fixation points of R-FIXED)")
    prior = []
    for i, entry in enumerate(job.get("prior_removal", [])):
        e = _keys(entry, ("polygon", "depth_mm"), "job.toml prior_removal[%d]" % i)
        prior.append((_polygon(e.get("polygon"), "job.toml prior_removal[%d].polygon" % i),
                      _number(e, "depth_mm", "job.toml prior_removal[%d]" % i)))
    p.set("prior", prior, "job.toml prior_removal (Rem(0))")

    # --- the catalogue -----------------------------------------------------------------------------------------
    p.set("heading_offset", _cat(cat, "heading_offset"), "params.json heading_offset (SPEC)")
    p.set("m_body", _cat(cat, "body_hull_margin"), "params.json body_hull_margin (DEFAULT_UNMEASURED)")
    p.set("geom_tol", _cat(cat, "geom_tol"), "params.json geom_tol")
    p.set("tol_panel", _cat(cat, "tol_panel"), "params.json tol_panel")
    p.set("attach_tol", _cat(cat, "attach_tol"), "params.json attach_tol")
    p.set("park_pose_tol", _cat(cat, "park_pose_tol"), "params.json park_pose_tol")
    p.set("park_pose_tol_deg", _cat(cat, "park_pose_tol_deg"), "params.json park_pose_tol_deg")
    p.set("leg_wheel", _cat(cat, "tower_zone_leg_wheel"), "params.json tower_zone_leg_wheel (SPEC)")
    p.set("leg_tool", _cat(cat, "tower_zone_leg_tool"), "params.json tower_zone_leg_tool (SPEC)")
    p.set("panel_max", _cat(cat, "panel_max_side"), "params.json panel_max_side (SPEC)")
    p.set("z_safety", _cat(cat, "z_safety"), "params.json z_safety (SPEC)")
    p.set("z_upper", _cat(cat, "z_upper_limit"), "params.json z_upper_limit")
    p.set("z_min_unverified", _cat(cat, "z_min_unverified"), "params.json z_min_unverified")
    p.set("z_travel", _cat(cat, "z_travel"), "params.json z_travel (SPEC)")
    p.set("tool_max_d", _cat(cat, "tool_max_diameter"), "params.json tool_max_diameter (SPEC)")
    p.set("tool_max_len", _cat(cat, "tool_max_length"), "params.json tool_max_length (SPEC)")
    p.set("eps_surf", _cat(cat, "eps_surf"), "params.json eps_surf")
    p.set("through_tol", _cat(cat, "through_tol"), "params.json through_tol")
    p.set("e_max", _cat(cat, "check_chord_tol"), "params.json check_chord_tol")
    p.set("piece_min", _cat(cat, "piece_length_min"), "params.json piece_length_min")
    p.set("c_step_max", _cat(cat, "c_step_max"), "params.json c_step_max")
    p.set("cumulative_max", _cat(cat, "cumulative_rotation_max"), "params.json cumulative_rotation_max")
    p.set("c_grid", _cat(cat, "c_grid_step"), "params.json c_grid_step (SPEC)")

    mpolicy = _keys(machine.get("policy", {}), tuple(_MACHINE_POLICY), "machine.toml policy")

    def policy_value(name: str, catalogue_value: float, label: str) -> tuple[float, str]:
        value = _number(mpolicy, name, "machine.toml policy", required=False)
        if value is None:
            return catalogue_value, "params.json %s" % label
        return _tighter(value, catalogue_value, _MACHINE_POLICY[name], name, "machine.toml policy"), \
            "machine.toml policy.%s (OWNER_PROFILE)" % name

    m_w, src = policy_value("m_w", _cat(cat, "m_w"), "m_w")
    p.set("m_w", m_w, src)
    m_edge_tool, src = policy_value("m_edge_tool", _cat(cat, "m_edge_tool"), "m_edge_tool")
    p.set("m_edge_tool", m_edge_tool, src)
    m_tower, src = policy_value("m_tower", _cat(cat, "m_tower"), "m_tower")
    p.set("m_tower", m_tower, src)
    reach, src = policy_value("reach_max", _cat(cat, "reach_max"), "reach_max (INFERRED, U-40)")
    p.set("reach_max", reach, src)

    # --- the robot: supports, wheels, body, tower base, wire ---------------------------------------------------
    spec = [Support(1, _cat(cat, "support_r1"), _cat(cat, "support_delta1"), "vendor"),
            Support(2, _cat(cat, "support_r2"), _cat(cat, "support_delta2"), "vendor"),
            Support(3, _cat(cat, "support_r3"), _cat(cat, "support_delta3"), "vendor")]
    foot = _keys(machine.get("footprint", {}), tuple("q%d_%s" % (k, s) for k in (1, 2, 3) for s in ("mm", "deg")),
                 "machine.toml footprint")
    measured_model = []
    extra = []
    for s in spec:
        mm = _measured(foot, "q%d_mm" % s.wheel, "machine.toml footprint")
        deg = _measured(foot, "q%d_deg" % s.wheel, "machine.toml footprint")
        if mm is not None and deg is not None:
            m = Support(s.wheel, mm, deg, "measured")
            measured_model.append(m)
            extra.append(m)
        else:
            measured_model.append(s)
    provenance = "ROBOT_MEASURED" if "export" in machine else "OWNER_PROFILE"
    p.set("supports", tuple(spec + extra),
          "params.json support_r1..3, support_delta1..3 (SPEC)" + (
              "; union with the machine.toml footprint (%s, safety-model 4.3)" % provenance if extra else ""))
    p.set("models", (tuple(spec), tuple(measured_model)) if extra else (tuple(spec),), "footprint models in use")

    wheels = _keys(machine.get("wheel", {}), ("q1", "q2", "q3"), "machine.toml wheel")
    radii = []
    for k in (1, 2, 3):
        t = _keys(wheels.get("q%d" % k, {}), ("contact_radius_mm",), "machine.toml wheel.q%d" % k)
        radii.append(_measured(t, "contact_radius_mm", "machine.toml wheel.q%d" % k))
    r_w_default = _cat(cat, "r_w")
    known = [r for r in radii if r is not None]
    if len(known) == 3:
        p.set("r_w", max(known), "machine.toml wheel.q1..q3 contact_radius_mm: the measured maximum %s of %s "
              "(never the mean; %s)" % (max(known), known, provenance))
    else:
        p.set("r_w", max([r_w_default] + known), "params.json r_w = %s (DEFAULT_UNMEASURED, U-22)%s"
              % (r_w_default, "" if not known else "; the larger of it and the known readings %s" % known))
    p.set("rho", p.r_w + p.m_w, "derived: rho = r_w + m_w")

    body = _keys(machine.get("body", {}), ("ahead_of_tool_mm", "behind_tool_mm", "left_of_axis_mm",
                                           "right_of_axis_mm", "max_radius_mm", "outline_points"),
                 "machine.toml body")
    outline = None
    pts = body.get("outline_points")
    if isinstance(pts, list) and pts:
        outline = np.array([_point(v, "machine.toml body.outline_points") for v in pts], dtype=float)
    else:
        ext = [_measured(body, k, "machine.toml body") for k in ("ahead_of_tool_mm", "behind_tool_mm",
                                                                  "left_of_axis_mm", "right_of_axis_mm")]
        if None not in ext:
            ahead, behind, left, right = ext  # type: ignore[misc]
            outline = np.array([(-ahead, -left), (-ahead, right), (behind, right), (behind, -left)], dtype=float)
    p.set("body_outline", outline, "machine.toml body (%s): the hull of the wheel discs and this outline" % provenance
          if outline is not None else "params.json body_hull_margin: the default hull, wheel discs grown by %s mm"
          % p.m_body)

    tower = _keys(machine.get("tower", {}), ("base_radius_mm", "base_length_mm", "base_width_mm"),
                  "machine.toml tower")
    t_r, t_l, t_w = (_measured(tower, k, "machine.toml tower") for k in ("base_radius_mm", "base_length_mm",
                                                                          "base_width_mm"))
    base_default = _cat(cat, "tower_base_radius")
    if t_r is not None and t_l is not None and t_w is not None:
        if t_r < max(t_l, t_w) / 2.0:
            raise OracleError("machine.toml tower.base_radius_mm %s is less than half the base's larger side" % t_r)
        p.set("base_radius", t_r, "machine.toml tower.base_radius_mm (%s)" % provenance)
    else:
        p.set("base_radius", max([base_default] + [v for v in (t_r,) if v is not None]
                                 + [v / 2.0 for v in (t_l, t_w) if v is not None]),
              "params.json tower_base_radius (DEFAULT_UNMEASURED), or larger readings")

    wire = _keys(machine.get("wire", {}), ("attachment", "same_point", "back_mm", "right_mm"), "machine.toml wire")
    wire_point = None
    if g.open("wire_attachment_measured") and wire.get("same_point") == "yes":
        if wire.get("attachment") == "on_spindle_axis":
            wire_point = (0.0, 0.0)
        elif wire.get("attachment") == "offset":
            back, right = _measured(wire, "back_mm", "machine.toml wire"), _measured(wire, "right_mm",
                                                                                     "machine.toml wire")
            if back is not None and right is not None:
                wire_point = (back, right)
    p.set("wire_point", wire_point, "machine.toml wire with the gate wire_attachment_measured open"
          if wire_point is not None else "the whole body hull (wire attachment unmeasured, U-49)")

    # --- derived from the geometry, never read ----------------------------------------------------------------
    all_r = [s.r for s in p.supports]
    p.set("r_min", min(all_r), "derived: the nearest support of the active footprint")
    p.set("r_max", max(all_r), "derived: the farthest support of the active footprint")
    bound = p.r_max
    for model in p.models:
        cx, cy = _centroid(model, p.heading_offset)
        dc = math.sqrt(cx * cx + cy * cy)
        for s in p.supports:
            dx, dy = _offset(s, 0.0, p.heading_offset)
            bound = max(bound, dc + math.sqrt((dx - cx) ** 2 + (dy - cy) ** 2))
    p.set("r_rot", float(math.ceil(bound - 1e-9)), "derived: ceil(max(R_max, d_c + R_c)) = ceil(%.3f) over every "
          "footprint model in use (safety-model 4.6)" % bound)
    if p.body_outline is None:
        p.set("r_body", p.r_rot + p.rho + p.m_body, "derived: R_rot + rho + m_body (default hull bound, "
              "safety-model 4.5)")
    else:
        rb = bound + p.rho
        for model in p.models:
            cx, cy = _centroid(model, p.heading_offset)
            dc = math.sqrt(cx * cx + cy * cy)
            for hx, hy in _outline_offsets(p.body_outline, 0.0, p.heading_offset):
                rb = max(rb, math.sqrt(hx * hx + hy * hy), dc + math.sqrt((hx - cx) ** 2 + (hy - cy) ** 2))
        p.set("r_body", rb, "derived: the largest |h| and |c| + |h - c| over the measured hull (safety-model 4.5)")
    delta = min(50.0, math.floor(p.r_min - p.rho - p.r_tool - p.delta_tool - 2.0 * p.e_max - 1.0))
    p.set("piece_length", delta, "derived: min(50, floor(r_min - rho - r_tool - delta_tool - 2 e_max - 1)) "
          "(safety-model 7.3)")

    # --- gates that carry values --------------------------------------------------------------------------------
    half = p.T / 2.0
    if g.open("tabs_hold"):
        value = g.value("tabs_hold")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 < value <= half:
            raise OracleError("the gate tabs_hold is open but carries %r, not a t_hold in (0, T/2]" % (value,))
        p.set("t_hold", float(value), "gate tabs_hold: R-TABS lowers t_hold to %s mm (rung R7)" % value)
    else:
        p.set("t_hold", half, "T/2 (INFERRED, U-42)")
    kerf = None
    if g.open("kerf_crossing"):
        value = g.value("kerf_crossing")
        names = ("kerf_width_max", "kerf_ride_max", "recess_ignore_max")
        if not isinstance(value, Mapping) or set(value) != set(names) or not all(
                isinstance(value[n], (int, float)) and not isinstance(value[n], bool) and value[n] >= 0
                for n in names):
            raise OracleError("the gate kerf_crossing is open but carries %r, not {%s}" % (value, ", ".join(names)))
        kerf = tuple(float(value[n]) for n in names)
    p.set("kerf", kerf, "gate kerf_crossing (R-KERF, rung R8k)" if kerf else "R-KERF closed: every removal deeper "
          "than eps_surf is a wheel keep-out")
    tested: set[str] = set()
    if g.open("near_edge_allowed"):
        value = g.value("near_edge_allowed")
        if not isinstance(value, (list, tuple)) or not value or not all(v in EDGES for v in value) \
                or len(set(value)) != len(value):
            raise OracleError("the gate near_edge_allowed is open but carries %r, not a list of tested edges from %s"
                              % (value, ", ".join(EDGES)))
        tested = {EDGES[v] for v in value}
    m_edge, m_src = policy_value("m_edge", _cat(cat, "m_edge"), "m_edge (until rung R9)")
    m_base, b_src = policy_value("m_edge_baseline", _cat(cat, "m_edge_baseline"), "m_edge_baseline (U-50)")
    after = _cat_after_unlock(cat, "m_edge")
    policy_edge = _number(mpolicy, "m_edge", "machine.toml policy", required=False)
    policy_base = _number(mpolicy, "m_edge_baseline", "machine.toml policy", required=False)
    baseline = "left" if p.tower2 == "Y" else "bottom"
    margins = {}
    for side in SIDES:
        if side in tested:
            value = max([after] + ([policy_edge] if policy_edge is not None else []))
        else:
            value = m_edge
        if side == baseline and side not in tested:
            value = max(value, m_base)
        elif side == baseline and policy_base is not None:
            value = max(value, policy_base)
        margins[side] = value
    p.set("m_edge_side", margins, "m_edge per side: %s; %s; rung R9 tested %s; the tower baseline is %s"
          % (m_src, b_src, sorted(tested) or "no edge", baseline))
    p.set("baseline_side", baseline, "the side from tower 1 to tower 2")
    rm = g.value("rotation_model")
    if g.open("rotation_model") and rm not in ROTATION_MODELS:
        raise OracleError("the gate rotation_model is open but records %r, not one of %s" % (rm, ROTATION_MODELS))
    p.set("rotation_model", rm if g.open("rotation_model") else None,
          "gate rotation_model" if g.open("rotation_model") else "unverified (gate rotation_model closed)")
    p.set("heading_map", g.open("heading_map_verified"), "gate heading_map_verified")
    p.set("safe_zone", not (p.heading_map and p.rotation_model is not None),
          "safety-model 8.1: while the heading map or the rotation model is not a fact")
    split = p.rotation_model != "during-translation" and (g.open("rotate_in_place_ok") or p.safe_zone)
    p.set("split_allowed", split, "rotate-in-place link lines: never under during-translation; with "
          "rotate_in_place_ok, or while the safe zone applies (safety-model 9.1)")
    start = None
    start_source = "no start pose (gate start_pose_known closed)"
    if g.open("start_pose_known"):
        value = g.value("start_pose_known")
        if not isinstance(value, Mapping) or not all(
                isinstance(value.get(k), (int, float)) and not isinstance(value.get(k), bool) for k in ("x", "y")):
            raise OracleError("the gate start_pose_known is open but carries %r, not a park pose" % (value,))
        c = value.get("c")
        if c is not None and (isinstance(c, bool) or not isinstance(c, int) or c % 10 or not 0 <= c <= 350):
            raise OracleError("the gate start_pose_known carries a park heading %r off the C grid" % (c,))
        # safety-model 9.5: a measured park pose is a disc of radius park_pose_tol about the measured point. A
        # box recorded with the fact never shrinks that disc; a larger one is disclosed, not used (the checker
        # may take the larger disc, so the oracle refuses less there, never more).
        tol = p.park_pose_tol
        start_source = ("gate start_pose_known (fact detection.park_pose, rung R2); the start disc has the radius "
                        "park_pose_tol = %g mm (safety-model 9.5)" % tol)
        given = value.get("tolerance_mm")
        if given is not None:
            if isinstance(given, bool) or not isinstance(given, (int, float)) or not math.isfinite(given) \
                    or given < 0:
                raise OracleError("the gate start_pose_known carries a tolerance_mm %r that is not a length in mm"
                                  % (given,))
            box = float(given) * math.sqrt(2.0)
            if box > tol:
                p.notes.append("W-FIRST: the park pose's own box of +-%g mm needs a larger disc (%.3f mm) than "
                               "park_pose_tol = %g mm; the oracle keeps the safety model's disc of park_pose_tol "
                               "(safety-model 9.5), so it refuses less here, never more" % (given, box, tol))
        start = (float(value["x"]), float(value["y"]), c if p.heading_map else None, tol)
    p.set("start", start, start_source)
    return p


# ---------------------------------------------------------------------------
# The robot model
# ---------------------------------------------------------------------------


def _offset(s: Support, c: float, heading_offset: float) -> tuple[float, float]:
    a = math.radians(c + heading_offset + s.delta)
    return -s.r * math.cos(a), -s.r * math.sin(a)


def _centroid(model: Sequence[Support], heading_offset: float) -> tuple[float, float]:
    """The support centroid c at C = 0, in the frame of :func:`_offset` with the same heading offset."""
    xs, ys = zip(*(_offset(s, 0.0, heading_offset) for s in model))
    return sum(xs) / len(xs), sum(ys) / len(ys)


def _outline_offsets(outline: np.ndarray, c: float, heading_offset: float) -> np.ndarray:
    """Body-frame points [back, right] at heading C, as offsets from the tool point (panel frame)."""
    th = math.radians(c + heading_offset)
    fx, fy = math.cos(th), math.sin(th)       # forward, u(theta)
    rx, ry = math.cos(th - math.pi / 2), math.sin(th - math.pi / 2)  # right, u(theta - 90)
    return np.stack([-outline[:, 0] * fx + outline[:, 1] * rx, -outline[:, 0] * fy + outline[:, 1] * ry], axis=1)


def support_offsets(p: OracleParams, c: float) -> list[tuple[float, float]]:
    """d_k(theta) = -r_k u(theta + delta_k), theta = C + 90, for every support position in use (vendor first)."""
    return [_offset(s, c, p.heading_offset) for s in p.supports]


# ---------------------------------------------------------------------------
# Geometry (numpy, no shapely)
# ---------------------------------------------------------------------------


def _ccw(poly: np.ndarray) -> np.ndarray:
    x, y = poly[:, 0], poly[:, 1]
    area2 = float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))
    if abs(area2) < 1e-12:
        raise OracleError("a polygon has no area: %r" % poly.tolist())
    return poly if area2 > 0 else poly[::-1].copy()


def _seg_dist(px: np.ndarray, py: np.ndarray, ax: Any, ay: Any, bx: Any, by: Any) -> np.ndarray:
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    safe = np.where(l2 > 0, l2, 1.0)
    t = np.clip(((px - ax) * dx + (py - ay) * dy) / safe, 0.0, 1.0)
    t = np.where(l2 > 0, t, 0.0)
    ex, ey = px - (ax + t * dx), py - (ay + t * dy)
    return np.sqrt(ex * ex + ey * ey)


def _in_poly(px: np.ndarray, py: np.ndarray, poly: np.ndarray) -> np.ndarray:
    inside = np.zeros(np.broadcast(px, py).shape, dtype=bool)
    n = len(poly)
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        if ay == by:
            continue
        cond = (ay > py) != (by > py)
        xint = ax + (py - ay) * (bx - ax) / (by - ay)
        inside ^= cond & (px < xint)
    return inside


def _edge_dists(px: np.ndarray, py: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """Distances from each point to each edge of ``poly``: shape points + (edges,)."""
    a = poly
    b = np.roll(poly, -1, axis=0)
    return _seg_dist(px[..., None], py[..., None], a[:, 0], a[:, 1], b[:, 0], b[:, 1])


def _poly_dist(px: np.ndarray, py: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """Distance to the polygon region (0 inside)."""
    d = _edge_dists(px, py, poly).min(axis=-1)
    return np.where(_in_poly(px, py, poly), 0.0, d)


def _signed_dist(px: np.ndarray, py: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """Distance to the polygon's boundary, negative inside it (how deep a point lies within the region)."""
    d = _edge_dists(px, py, poly).min(axis=-1)
    return np.where(_in_poly(px, py, poly), -d, d)


def in_triangle_spec(qx: np.ndarray, qy: np.ndarray, a: Sequence[float], b: Sequence[float],
                     c: Sequence[float]) -> np.ndarray:
    """Spec 08's exact triangle test (Tower keep-out zones), vertices in the spec's order."""
    def cross(ux: Any, uy: Any, vx: Any, vy: Any) -> Any:
        return ux * vy - uy * vx
    t = cross(b[0] - a[0], b[1] - a[1], qx - a[0], qy - a[1])
    s = cross(qx - a[0], qy - a[1], c[0] - a[0], c[1] - a[1])
    area = cross(b[0] - a[0], b[1] - a[1], c[0] - a[0], c[1] - a[1])
    same = (s < 0) == (t < 0)
    if area < 0:
        inside = (s <= 0) & (s + t >= area)
    else:
        inside = (s >= 0) & (s + t <= area)
    return same & inside


def _tri_dist(px: np.ndarray, py: np.ndarray, tri: tuple) -> np.ndarray:
    d = _edge_dists(px, py, np.array(tri, dtype=float)).min(axis=-1)
    return np.where(in_triangle_spec(px, py, *tri), 0.0, d)


def _hull(points: np.ndarray) -> np.ndarray:
    pts = sorted(set(map(tuple, np.round(points, 9).tolist())))
    if len(pts) <= 2:
        return np.array(pts, dtype=float)

    def cross(o: tuple, a: tuple, b: tuple) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower: list[tuple] = []
    for q in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], q) <= 0:
            lower.pop()
        lower.append(q)
    upper: list[tuple] = []
    for q in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], q) <= 0:
            upper.pop()
        upper.append(q)
    return np.array(lower[:-1] + upper[:-1], dtype=float)


def _clip_box(poly: np.ndarray, x1: float, y1: float) -> np.ndarray:
    """Sutherland-Hodgman: ``poly`` clipped to [0, x1] x [0, y1]."""
    out = [tuple(p) for p in poly]
    for axis, limit, keep_below in ((0, 0.0, False), (0, x1, True), (1, 0.0, False), (1, y1, True)):
        src, out = out, []
        if not src:
            break
        for i, cur in enumerate(src):
            prev = src[i - 1]
            cin = cur[axis] <= limit if keep_below else cur[axis] >= limit
            pin = prev[axis] <= limit if keep_below else prev[axis] >= limit
            if cin != pin:
                f = (limit - prev[axis]) / (cur[axis] - prev[axis])
                out.append((prev[0] + f * (cur[0] - prev[0]), prev[1] + f * (cur[1] - prev[1])))
            if cin:
                out.append(cur)
    if len(out) < 3:
        raise OracleError("the measured panel clipped to SET PANEL SIZE is empty")
    return _ccw(np.array(out, dtype=float))


def _segments_cross(a: np.ndarray, b: np.ndarray, c: np.ndarray, d: np.ndarray) -> np.ndarray:
    """Whether segment a-b crosses c-d (proper or touching), broadcasting over leading axes."""
    def orient(p: np.ndarray, q: np.ndarray, r: np.ndarray) -> np.ndarray:
        return (q[..., 0] - p[..., 0]) * (r[..., 1] - p[..., 1]) - (q[..., 1] - p[..., 1]) * (r[..., 0] - p[..., 0])
    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    return (o1 * o2 <= 0) & (o3 * o4 <= 0)


class _Area:
    """A containment region: a polygon with a margin per edge (safety-model 6.2)."""

    def __init__(self, poly: np.ndarray, margins: np.ndarray) -> None:
        self.poly = poly
        self.margins = margins

    def slack(self, px: np.ndarray, py: np.ndarray, radius: Any) -> np.ndarray:
        d = _edge_dists(px, py, self.poly)
        inside = _in_poly(px, py, self.poly)
        s_in = (d - self.margins).min(axis=-1) - radius
        s_out = -(d.min(axis=-1) + radius)
        return np.where(inside, s_in, s_out)


def _edge_margins(poly: np.ndarray, side_margin: Mapping[str, float]) -> np.ndarray:
    out = []
    for i in range(len(poly)):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % len(poly)]
        nx, ny = by - ay, -(bx - ax)
        norm = math.sqrt(nx * nx + ny * ny) or 1.0
        nx, ny = nx / norm, ny / norm
        dots = {s: nx * SIDE_DIRECTION[s][0] + ny * SIDE_DIRECTION[s][1] for s in SIDES}
        best = max(dots.values())
        out.append(max(side_margin[s] for s, v in dots.items() if v >= best - 1e-12))
    return np.array(out, dtype=float)


def _with_strips(x0: float, y0: float, x1: float, y1: float, strips: Mapping[str, float]) -> np.ndarray:
    pts = [(x0, y0)]
    if strips["bottom"] > 0:
        pts += [(x0, y0 - strips["bottom"]), (x1, y0 - strips["bottom"])]
    pts.append((x1, y0))
    if strips["right"] > 0:
        pts += [(x1 + strips["right"], y0), (x1 + strips["right"], y1)]
    pts.append((x1, y1))
    if strips["top"] > 0:
        pts += [(x1, y1 + strips["top"]), (x0, y1 + strips["top"])]
    pts.append((x0, y1))
    if strips["left"] > 0:
        pts += [(x0 - strips["left"], y1), (x0 - strips["left"], y0)]
    return np.array(pts, dtype=float)


# ---------------------------------------------------------------------------
# The measured panel (P-GEOM, safety-model 12)
# ---------------------------------------------------------------------------

_AXIS_MAPS = {
    "identity": (1, 0, 0, 1), "swap": (0, 1, 1, 0), "mirror-x": (-1, 0, 0, 1), "mirror-y": (1, 0, 0, -1),
    "half-turn": (-1, 0, 0, -1), "swap-mirror-x": (0, -1, 1, 0), "swap-mirror-y": (0, 1, -1, 0),
    "swap-half-turn": (0, -1, -1, 0)}


def _parse_info(line: Any, code: str, count: int) -> tuple[float, ...]:
    if not isinstance(line, str):
        raise OracleError("a measured line must be text, got %r" % (line,))
    parts = line.strip().split(";")
    if len(parts) != 2 + count or parts[0] != "INFO" or parts[1] != code:
        raise OracleError("%r is not an INFO;%s line with %d values" % (line, code, count))
    try:
        values = tuple(float(v) for v in parts[2:])
    except ValueError as exc:
        raise OracleError("%r holds a value that is not a number" % (line,)) from exc
    if not all(math.isfinite(v) for v in values):
        raise OracleError("%r holds a value that is not finite" % (line,))
    return values


def _simple(poly: np.ndarray) -> bool:
    n = len(poly)
    for i in range(n):
        for j in range(i + 1, n):
            if abs(i - j) <= 1 or (i == 0 and j == n - 1):
                continue
            if bool(_segments_cross(poly[i], poly[(i + 1) % n], poly[j], poly[(j + 1) % n])):
                return False
    return True


def _boundary_samples(poly: np.ndarray) -> np.ndarray:
    out = []
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        n = max(1, int(math.ceil(math.hypot(*(b - a)) / SAMPLE_MM)))
        f = np.arange(n) / n
        out.append(a + f[:, None] * (b - a))
    return np.concatenate(out)


def _hausdorff_lower(a: np.ndarray, b: np.ndarray) -> float:
    """A lower bound of the region Hausdorff distance (boundary samples, 0 inside the other region)."""
    sa, sb = _boundary_samples(a), _boundary_samples(b)
    return float(max(_poly_dist(sa[:, 0], sa[:, 1], b).max(), _poly_dist(sb[:, 0], sb[:, 1], a).max()))


def _p_geom(p: OracleParams, measured: Mapping[str, Any]) -> tuple[np.ndarray | None, float | None, list[OracleHit],
                                                                      list[str]]:
    """Decode and validate the measured panel. Returns (P_meas or None, the INFO;27 distance, hits, notes)."""
    notes: list[str] = []
    raw = measured.get("info25")
    if not raw:
        return None, None, [OracleHit("P-GEOM", where="job-time", detail="no vertex stream observed in this power "
                                      "cycle: no geometry, no job")], notes
    values = [_parse_info(line, "25", 2) for line in raw]
    info27 = measured.get("info27")
    distance = _parse_info(info27, "27", 1)[0] if info27 is not None else None
    if distance is None:
        return None, None, [OracleHit("P-GEOM", where="job-time", detail="no INFO;27 tower distance")], notes
    rect = np.array([(0.0, 0.0), (p.W, 0.0), (p.W, p.H), (0.0, p.H)])
    frame_fact = p.gates.open("panel_vertices_usable")
    hypotheses = [("xy", "identity")] if frame_fact else \
        [(enc, name) for enc in ("xy", "polar") for name in _AXIS_MAPS]
    passing_clear, identity = [], None
    for enc, name in hypotheses:
        if enc == "xy":
            pts = np.array(values, dtype=float)
        else:
            pts = np.array([(v[0] * math.cos(math.radians(v[1])), v[0] * math.sin(math.radians(v[1])))
                            for v in values], dtype=float)
        m = _AXIS_MAPS[name]
        pts = np.stack([m[0] * pts[:, 0] + m[1] * pts[:, 1], m[2] * pts[:, 0] + m[3] * pts[:, 1]], axis=1)
        verdict = _hypothesis(p, pts, rect, distance)
        if enc == "xy" and name == "identity":
            identity = (pts, verdict)
        elif verdict == "clear":
            passing_clear.append("%s/%s" % (enc, name))
    hits: list[OracleHit] = []
    assert identity is not None
    pts, verdict = identity
    if verdict == "fails":
        others = " (it fits %s: a frame transform needs a design review, PHYS-01)" % ", ".join(passing_clear) \
            if passing_clear else ""
        hits.append(OracleHit("P-GEOM", where="job-time", detail="the identity frame fails validation%s" % others))
    elif passing_clear:
        hits.append(OracleHit("P-GEOM", where="job-time", detail="more than one decoding hypothesis passes "
                              "(identity and %s): never tie-broken" % ", ".join(passing_clear)))
    if not frame_fact:
        hits.append(OracleHit("G-GATE", rule="panel_vertices_usable", where="job-time",
                              detail="uploads need the vertex frame fact (rung R2)"))
    if verdict == "fails":
        return None, distance, hits, notes
    poly = _ccw(pts)
    xi, yi = math.floor(float(poly[:, 0].max())), math.floor(float(poly[:, 1].max()))
    notes.append("job time: the identity hypothesis of the measured panel is used; SET PANEL SIZE %d x %d; "
                 "P_work = P_meas clipped to it (safety-model 12)" % (xi, yi))
    return _clip_box(poly, xi, yi), distance, hits, notes


def _hypothesis(p: OracleParams, pts: np.ndarray, rect: np.ndarray, distance: float) -> str:
    """'fails' (surely fails), 'clear' (passes with a clear margin) or 'unsure'."""
    if len(pts) < 3:
        return "fails"
    x, y = pts[:, 0], pts[:, 1]
    area2 = float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))
    if abs(area2) <= 1e-9 or not _simple(pts):
        return "fails"
    poly = _ccw(pts)
    h = _hausdorff_lower(poly, rect)
    extent = float(poly[:, 1].max()) if p.tower2 == "Y" else float(poly[:, 0].max())
    side = p.H if p.tower2 == "Y" else p.W
    gap27 = min(abs(distance - extent), abs(distance - side))
    corner = float(np.sqrt((poly ** 2).sum(axis=1)).min())
    if h > p.tol_panel + 1e-9 or gap27 > p.tol_panel + 1e-9 or corner > p.tol_panel + 1e-9:
        return "fails"
    if h + SAMPLE_MM <= p.tol_panel and max(abs(distance - extent), abs(distance - side)) <= p.tol_panel \
            and corner <= 1e-6:
        return "clear"
    return "unsure"


# ---------------------------------------------------------------------------
# The world: panel, zones, the sampled tool path, removal and structure
# ---------------------------------------------------------------------------

_FIRST, _CUT, _LINK = 0, 1, 2


class _Path:
    def __init__(self) -> None:
        self.x: list[float] = []
        self.y: list[float] = []
        self.z: list[float] = []
        self.line: list[int] = []
        self.part: list[int] = []
        self.cut: list[int] = []
        self.move_end: list[bool] = []
        self.s = 0.0

    def __len__(self) -> int:
        return len(self.x)

    def add(self, x: float, y: float, z: float, line: int, part: int, cut: int, end: bool) -> None:
        self.x.append(x)
        self.y.append(y)
        self.z.append(z)
        self.line.append(line)
        self.part.append(part)
        self.cut.append(cut)
        self.move_end.append(end)

    def straight(self, a: tuple, b: tuple, line: int, part: int, cut: int, *, include_start: bool) -> None:
        ax, ay, az = a
        bx, by, bz = b
        length = math.hypot(bx - ax, by - ay)
        if include_start:
            self.add(ax, ay, az, line, part, cut, False)
        if length > 0:
            k0, k1 = math.floor(self.s / SAMPLE_MM) + 1, math.ceil((self.s + length) / SAMPLE_MM) - 1
            for k in range(k0, k1 + 1):
                f = (k * SAMPLE_MM - self.s) / length
                if 1e-12 < f < 1 - 1e-12:
                    self.add(ax + (bx - ax) * f, ay + (by - ay) * f, az + (bz - az) * f, line, part, cut, False)
        self.add(bx, by, bz, line, part, cut, True)
        self.s += length

    def arc(self, move: Any, line: int, part: int, cut: int, *, include_start: bool) -> None:
        ax, ay, az = move.a_f
        bx, by, bz = move.b_f
        cx, cy = move.centre_f
        r0, r1 = move.radius
        sign = 1.0 if move.kind == "arc_ccw" else -1.0
        sweep = math.radians(move.sweep_deg)
        phi0 = math.atan2(ay - cy, ax - cx)
        length = (r0 + r1) / 2.0 * sweep
        if include_start:
            self.add(ax, ay, az, line, part, cut, False)
        k0, k1 = math.floor(self.s / SAMPLE_MM) + 1, math.ceil((self.s + length) / SAMPLE_MM) - 1
        for k in range(k0, k1 + 1):
            f = (k * SAMPLE_MM - self.s) / length
            if 1e-12 < f < 1 - 1e-12:
                phi = phi0 + sign * sweep * f
                r = r0 + (r1 - r0) * f
                self.add(cx + r * math.cos(phi), cy + r * math.sin(phi), az + (bz - az) * f, line, part, cut, False)
        self.add(bx, by, bz, line, part, cut, True)
        self.s += length


@dataclass
class _Event:
    """A stage of the motion: a cut, a translation, a rotation in place, a combined line or the first line."""

    kind: str               # cut | translate | rotate | combined | first
    i0: int
    i1: int
    c0: int | None = None
    c1: int | None = None
    cut: int | None = None
    line: int | None = None
    pa: tuple[float, float] | None = None
    pb: tuple[float, float] | None = None


def _f(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _z_only(move: Any) -> bool:
    """The line writes no X or Y: its X and Y are the same at both ends, known or not (a Z-only raise from the
    unknown start is Z-only too)."""
    return not move.is_arc and move.a[0] == move.b[0] and move.a[1] == move.b[1]


class World:
    """The panel, the zones, the sampled path, removal in time and the structure, for one program."""

    def __init__(self, program: Any, p: OracleParams, measured: Mapping[str, Any] | None) -> None:
        self.p = p
        self.program = program
        self.notes: list[str] = []
        self.hits: list[OracleHit] = []
        flavour = program.meta.flavour
        self.mode = "verify" if flavour == "dialect" else "plan"
        self._panel(measured)
        self._zones()
        self.path = _Path()
        self.events: list[_Event] = []
        self.cut_range: dict[int, tuple[int, int]] = {}
        if self.mode == "plan":
            self._plan_path()
        else:
            self._verify_path()
        self._arrays()
        self._removal()
        self.structure = _Structure(self)
        if self.structure.limit_note:
            self.notes.append(self.structure.limit_note)

    # -- panel and zones ----------------------------------------------------------------------------------------

    def _panel(self, measured: Mapping[str, Any] | None) -> None:
        p = self.p
        self.tower_distance: float | None = None
        if measured is not None:
            poly, distance, hits, notes = _p_geom(p, measured)
            self.hits.extend(hits)
            self.notes.extend(notes)
            if poly is None:
                raise _NoPanel()
            self.p_work = poly
            self.tower_distance = distance
            p.panel_source = "measured"
            self.inflate = p.m_tower
            xs, ys = poly[:, 0], poly[:, 1]
            rect = len(poly) == 4 and set(np.round(xs, 9)) <= {round(xs.min(), 9), round(xs.max(), 9)} \
                and set(np.round(ys, 9)) <= {round(ys.min(), 9), round(ys.max(), 9)}
            if any(v > 0 for v in p.strips.values()):
                if not rect:
                    raise OracleError("support strips on a measured panel that is not a rectangle are not modelled")
                wheel_poly = _with_strips(xs.min(), ys.min(), xs.max(), ys.max(), p.strips)
            else:
                wheel_poly = poly
        else:
            g = p.geom_tol
            x0, y0, x1, y1 = g, g, p.W - g, p.H - g
            if x1 <= x0 or y1 <= y0:
                raise OracleError("the panel %s x %s has no work area after geom_tol" % (p.W, p.H))
            self.p_work = np.array([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], dtype=float)
            p.panel_source = "desk"
            self.inflate = p.m_tower + p.geom_tol
            wheel_poly = _with_strips(x0, y0, x1, y1, p.strips)
            self.notes.append("desk: P_work is the declared %s x %s mm panel shrunk by geom_tol = %s mm; tower zones "
                              "grown by m_tower + geom_tol = %s mm" % (p.W, p.H, g, self.inflate))
        wheel_poly = _ccw(wheel_poly)
        self.wa_wheel = _Area(wheel_poly, _edge_margins(wheel_poly, p.m_edge_side))
        self.wa_tool = _Area(self.p_work, np.full(len(self.p_work), p.m_edge_tool))

    def _zones(self) -> None:
        p = self.p
        if p.panel_source == "desk":
            ts = [p.H if p.tower2 == "Y" else p.W]
        else:
            corner = float(self.p_work[:, 1].max() if p.tower2 == "Y" else self.p_work[:, 0].max())
            ts = sorted({round(corner, 6), round(float(self.tower_distance), 6)})
        self.towers = [("tower 1", (0.0, 0.0))]
        self.wheel_zones, self.tool_zones = [], []
        self.anchors2 = []
        for i, t in enumerate(ts):
            name = "tower 2 (%s)" % ("corner" if i == 0 or len(ts) == 1 else "measured")
            point = (0.0, t) if p.tower2 == "Y" else (t, 0.0)
            self.towers.append((name, point))
            self.anchors2.append(((50.0, t - 50.0) if p.tower2 == "Y" else (t - 50.0, 50.0), name))
        for leg, zones in ((p.leg_wheel, self.wheel_zones), (p.leg_tool, self.tool_zones)):
            zones.append(("Z1(%g)" % leg, ((0.0, 0.0), (0.0, leg), (leg, 0.0))))
            for t in ts:
                tri = ((0.0, t - leg), (0.0, t), (leg, t)) if p.tower2 == "Y" else ((t - leg, 0.0), (t, 0.0), (t, leg))
                zones.append(("Z2(%g) at %g" % (leg, t), tri))
        self.anchor1 = (50.0, 50.0)

    # -- the sampled path ---------------------------------------------------------------------------------------

    def _plan_path(self) -> None:
        p, prog = self.p, self.program
        cuts = prog.cuts
        if not cuts:
            raise OracleError("the program has no cuts: the oracle plans the links between cuts and does not model "
                              "a GI program without one")
        path = self.path
        z_s = p.z_safety
        start = p.start
        first_target = cuts[0].moves[0].a_f
        if start is not None:
            i0 = len(path)
            path.s = 0.0
            path.straight((start[0], start[1], z_s), (first_target[0], first_target[1], z_s), 0, _FIRST, -1,
                          include_start=True)
            self.events.append(_Event("first", i0, len(path) - 1, pa=(start[0], start[1]),
                                      pb=(first_target[0], first_target[1])))
        for k, cut in enumerate(cuts):
            i0 = len(path)
            path.s = 0.0
            for j, move in enumerate(cut.moves):
                if move.is_arc:
                    path.arc(move, move.src.line, _CUT, k, include_start=(j == 0))
                else:
                    path.straight(move.a_f, move.b_f, move.src.line, _CUT, k, include_start=(j == 0))
            self.cut_range[k] = (i0, len(path) - 1)
            self.events.append(_Event("cut", i0, len(path) - 1, cut=k, line=cut.moves[0].src.line))
            if k + 1 < len(cuts):
                a = cut.moves[-1].b_f
                b = cuts[k + 1].moves[0].a_f
                i0 = len(path)
                path.s = 0.0
                path.straight((a[0], a[1], z_s), (b[0], b[1], z_s), 0, _LINK, -1, include_start=True)
                self.events.append(_Event("translate", i0, len(path) - 1, cut=k, pa=(a[0], a[1]), pb=(b[0], b[1])))
        last = cuts[-1].moves[-1].b_f
        self.final = (last[0], last[1])
        for link in prog.links:
            if link.after_cut == len(cuts) - 1 and link.before_cut is None and any(m.moves_xy for m in link.moves):
                self.notes.append("plan: the moves after the last cut (line %d on) are not checked: whether the "
                                  "planner emits them is the planner's; W-PARK is checked at the last cut's end"
                                  % link.moves[0].src.line)

    def _verify_path(self) -> None:
        p, prog = self.p, self.program
        path = self.path
        c: int | None = None
        cut_of = {}
        for cut in prog.cuts:
            for m in cut.moves:
                cut_of[m.index] = cut.id
        prev_end: tuple | None = None
        cut_start: dict[int, int] = {}
        for move in prog.moves:
            line = move.src.line
            if move.c_end is not None:
                c_after = int(move.c_end)
            else:
                c_after = c
            if not move.located:
                if move.moves_xy and move.b[0] is not None and move.b[1] is not None:
                    target = (float(move.b[0]), float(move.b[1]))
                    zb = _f(move.b[2])
                    if p.start is not None:
                        i0 = len(path)
                        path.s = 0.0
                        path.straight((p.start[0], p.start[1], p.z_safety), (target[0], target[1],
                                      zb if zb is not None else p.z_safety), line, _FIRST, -1, include_start=True)
                        self.events.append(_Event("first", i0, len(path) - 1, c1=c_after, line=line,
                                                  pa=(p.start[0], p.start[1]), pb=target))
                    else:
                        self.events.append(_Event("first", len(path), len(path) - 1, c1=c_after, line=line,
                                                  pb=target))
                    prev_end = (target[0], target[1], zb if zb is not None else p.z_safety)
                c = c_after
                continue
            k = cut_of.get(move.index, -1)
            part = _CUT if k >= 0 else _LINK
            if move.kind == "traverse" and move.c_changes and not move.moves_xy:
                t = max(len(path) - 1, 0)
                self.events.append(_Event("rotate", t, t, c0=c, c1=c_after, line=line, pa=move.a_f[:2]))
                c = c_after
                continue
            if c_after is None:
                raise OracleError("line %d moves the robot before any C word: its heading is unknown" % line)
            contiguous = prev_end is not None and all(abs(u - v) < 1e-9 for u, v in zip(prev_end, move.a_f))
            i0 = len(path) - 1 if contiguous else len(path)
            if k >= 0 and k not in cut_start:
                cut_start[k] = i0
                path.s = 0.0
            elif k < 0:
                path.s = 0.0
            if move.is_arc:
                path.arc(move, line, part, k, include_start=not contiguous)
            else:
                path.straight(move.a_f, move.b_f, line, part, k, include_start=not contiguous)
            prev_end = move.b_f
            if move.kind == "traverse" and move.c_changes and move.moves_xy:
                self.events.append(_Event("combined", i0, len(path) - 1, c0=c, c1=c_after, line=line,
                                          pa=move.a_f[:2], pb=move.b_f[:2]))
            else:
                self.events.append(_Event("cut" if k >= 0 else "translate", i0, len(path) - 1, c0=c_after,
                                          c1=c_after, cut=k if k >= 0 else None, line=line))
            if k >= 0:
                self.cut_range[k] = (cut_start[k], len(path) - 1)
            c = c_after
        if prev_end is None:
            raise OracleError("the dialect program never moves the robot")
        self.final = (prev_end[0], prev_end[1])
        self.final_c = c

    def _arrays(self) -> None:
        path = self.path
        self.x = np.array(path.x, dtype=float)
        self.y = np.array(path.y, dtype=float)
        self.z = np.array(path.z, dtype=float)
        self.line = np.array(path.line, dtype=np.int64)
        self.part = np.array(path.part, dtype=np.int64)
        self.cutidx = np.array(path.cut, dtype=np.int64)
        self.move_end = np.array(path.move_end, dtype=bool)
        self.end_time = len(path)
        n = len(path)
        # pos: the length of the sampled path (the XY polyline through the samples) up to each sample, mm;
        # splitting a move into collinear pieces leaves it unchanged. seg_move_start: for the segment from sample
        # i to i + 1, the sample its move starts from (the last move end at or before i; 0 at the start).
        steps = np.hypot(np.diff(self.x), np.diff(self.y)) if n > 1 else np.zeros(0)
        self.pos = np.concatenate([[0.0], np.cumsum(steps)]) if n else np.zeros(0)
        ends = np.where(self.move_end, np.arange(n), 0) if n else np.zeros(0, dtype=np.int64)
        self.seg_move_start = np.maximum.accumulate(ends)[:max(n - 1, 0)].astype(np.int64) if n else \
            np.zeros(0, dtype=np.int64)

    # -- removal ------------------------------------------------------------------------------------------------

    def _removal(self) -> None:
        p = self.p
        n = len(self.x)
        if n < 2:
            self.seg = np.zeros((0, 6))
            return
        same_cut = (self.part[:-1] == _CUT) & (self.part[1:] == _CUT) & (self.cutidx[:-1] == self.cutidx[1:])
        zmin = np.minimum(self.z[:-1], self.z[1:])
        mat = same_cut & (zmin < -p.eps_surf)
        idx = np.nonzero(mat)[0]
        # columns: ax, ay, bx, by, depth, index (the segment from sample i to i + 1 is removed at time i + 1)
        self.seg = np.stack([self.x[idx], self.y[idx], self.x[idx + 1], self.y[idx + 1], -zmin[idx],
                             idx.astype(float)], axis=1)
        self.seg_line = self.line[idx]

    def wheel_removal(self) -> np.ndarray:
        """The segments a wheel must keep clear of: deeper than eps_surf and, under R-KERF, not shallower than h_r."""
        if self.p.kerf is not None and self.p.kerf[2] > 0:
            return self.seg[self.seg[:, 4] >= self.p.kerf[2]]
        return self.seg

    def wheel_prior(self) -> list[tuple[np.ndarray, float]]:
        p = self.p
        out = []
        for poly, depth in p.prior:
            if depth <= p.eps_surf:
                continue
            if p.kerf is not None and depth < p.kerf[2]:
                continue
            out.append((poly, depth))
        return out

    # -- the source lines ---------------------------------------------------------------------------------------

    def time_at_line(self, line: int) -> int:
        """The first sample of the given source line."""
        hits = np.nonzero(self.line == line)[0]
        if len(hits) == 0:
            raise OracleError("no sample belongs to line %d" % line)
        return int(hits[0])

    def line_of_point(self, x: float, y: float) -> int:
        """The source line of the cut sample nearest (x, y)."""
        cut = self.part == _CUT
        d = np.where(cut, np.hypot(self.x - x, self.y - y), np.inf)
        return int(self.line[int(np.argmin(d))])

    def is_detached(self, x: float, y: float, t: int) -> bool:
        return bool(self.structure.detached(np.array([x], float), np.array([y], float), np.array([t]))[0])

    def tower_separation(self) -> int | None:
        return self.structure.separation

    def cut_end_time(self, k: int) -> int:
        return self.cut_range[k][1] + 1


class _NoPanel(Exception):
    """The measured panel failed P-GEOM so badly that no work panel exists: the job is refused by P-GEOM."""


# ---------------------------------------------------------------------------
# Structure on the 2 mm grid (safety-model 7.4)
# ---------------------------------------------------------------------------


class _Structure:
    """Attached and detached material in time, on the oracle's own grid.

    Two removal grids: ``t_rem``, the time each grid point is removed (closed
    discs of radius r_tool + delta_tool about the tool's path, prior removal
    at time -1), used exactly; and ``t_hold``, the removal that breaks holding
    material (deeper than T - t_hold), shrunk by GRID_SHRINK_MM, used for
    connectivity. Shrinking the holding removal and taking the connectivity
    domain GRID_SHRINK_MM beyond P_work can only connect more than the exact
    model, so the grid's anchor holds every grid point the exact anchor holds:
    the oracle finds at most the material the exact model finds detached. The
    anchor is recomputed at every move's end and at least every
    CHECKPOINT_MM within a cut; a sample uses the latest one before it, whose
    anchor can only be larger than the exact one at that time.
    """

    def __init__(self, world: World) -> None:
        self.world = world
        p = world.p
        self.separation: int | None = None
        self.sep_hit: OracleHit | None = None
        self.active = False
        self._cand: dict[tuple, Any] = {}
        breaks = p.T - p.t_hold
        deep_seg = world.seg[world.seg[:, 4] > breaks] if len(world.seg) else world.seg
        deep_prior = [(poly, d) for poly, d in p.prior if d > breaks]
        self.hold_idx = np.sort(deep_seg[:, 5].astype(np.int64)) if len(deep_seg) else np.zeros(0, dtype=np.int64)
        self.limit_note: str | None = None
        shrunk = p.r_cap - GRID_SHRINK_MM
        if len(deep_seg) and 2.0 * shrunk < GRID_MM:
            # A holding kerf narrower than the grid after the shrink can slip between the grid's points, and one no
            # wider than the shrink removes nothing on the grid: the oracle cannot see what such a kerf frees.
            self.limit_note = (
                "the oracle's structure grid cannot resolve this tool's holding kerfs: it shrinks holding removal by "
                "%g mm on a %g mm grid, and a kerf of radius r_tool + delta_tool = %g mm %s, so M-ATTACH, T-ATTACH "
                "and M-TOWERSEP may find less or no material that this program's cuts detach (the oracle refuses "
                "less here, never more)" % (GRID_SHRINK_MM, GRID_MM, p.r_cap,
                                            "removes nothing on the grid" if shrunk <= 0 else
                                            "is %g mm wide after the shrink, narrower than the grid" % (2.0 * shrunk)))
        if len(deep_seg) == 0 and not deep_prior:
            return
        self.active = True
        g = GRID_SHRINK_MM
        box = world.p_work
        x0, y0 = box[:, 0].min() - g - GRID_MM, box[:, 1].min() - g - GRID_MM
        x1, y1 = box[:, 0].max() + g + GRID_MM, box[:, 1].max() + g + GRID_MM
        self.gx0, self.gy0 = x0, y0
        gx = x0 + GRID_MM * np.arange(int(math.ceil((x1 - x0) / GRID_MM)) + 1)
        gy = y0 + GRID_MM * np.arange(int(math.ceil((y1 - y0) / GRID_MM)) + 1)
        self.gx, self.gy = gx, gy
        X, Y = np.meshgrid(gx, gy)
        self.dom_exact = _in_poly(X, Y, world.p_work)
        self.dom_conn = self.dom_exact | (_poly_dist(X, Y, world.p_work) <= g)
        self.t_rem = np.full(X.shape, _BIG, dtype=np.int64)
        self.t_hold = np.full(X.shape, _BIG, dtype=np.int64)
        for poly, depth in p.prior:
            if depth <= p.eps_surf:
                continue
            inside = _in_poly(X, Y, poly)
            self.t_rem[inside] = -1
            if depth > breaks:
                deep = inside & (_edge_dists(X, Y, poly).min(axis=-1) >= g)
                self.t_hold[deep] = -1
        self._raster(world.seg, p.r_cap, self.t_rem)
        if p.r_cap - g > 0 and len(deep_seg):
            self._raster(deep_seg, p.r_cap - g, self.t_hold)
        self.lost = np.full(X.shape, _BIG, dtype=np.int64)
        self.fix_cells = [self._cell(fx, fy) for (fx, fy), _ in p.fixations]
        self._timeline()

    def _cell(self, x: float, y: float) -> tuple[int, int] | None:
        ix = int(round((x - self.gx0) / GRID_MM))
        iy = int(round((y - self.gy0) / GRID_MM))
        if 0 <= iy < len(self.gy) and 0 <= ix < len(self.gx):
            return iy, ix
        return None

    def _raster(self, seg: np.ndarray, radius: float, grid: np.ndarray) -> None:
        for start in range(0, len(seg), 64):
            chunk = seg[start:start + 64]
            ax, ay, bx, by, _, t = (chunk[:, i] for i in range(6))
            lo_x, hi_x = min(ax.min(), bx.min()) - radius, max(ax.max(), bx.max()) + radius
            lo_y, hi_y = min(ay.min(), by.min()) - radius, max(ay.max(), by.max()) + radius
            i0 = max(0, int(math.floor((lo_x - self.gx0) / GRID_MM)))
            i1 = min(len(self.gx) - 1, int(math.ceil((hi_x - self.gx0) / GRID_MM)))
            j0 = max(0, int(math.floor((lo_y - self.gy0) / GRID_MM)))
            j1 = min(len(self.gy) - 1, int(math.ceil((hi_y - self.gy0) / GRID_MM)))
            if i1 < i0 or j1 < j0:
                continue
            X, Y = np.meshgrid(self.gx[i0:i1 + 1], self.gy[j0:j1 + 1])
            d = _seg_dist(X[..., None], Y[..., None], ax, ay, bx, by)
            times = np.where(d <= radius, t.astype(np.int64), _BIG).min(axis=-1)
            sub = grid[j0:j1 + 1, i0:i1 + 1]
            np.minimum(sub, times, out=sub)

    def _timeline(self) -> None:
        world = self.world
        cps = {0}
        for i0, i1 in world.cut_range.values():
            ends = np.nonzero(world.move_end[i0:i1 + 1])[0] + i0 + 1
            cps.update(int(v) for v in ends)
            step = max(1, int(CHECKPOINT_MM / SAMPLE_MM))
            cps.update(range(i0 + step, i1 + 1, step))
            cps.add(i1 + 1)
        cps.add(world.end_time)
        held = np.unique(self.t_hold[self.t_hold < _BIG])
        a1 = self._cell(*world.anchor1)
        a2 = [(self._cell(*pt), name) for pt, name in world.anchors2]
        previous = None
        for tau in sorted(cps):
            changed = int(np.searchsorted(held, tau)) if len(held) else 0
            if previous is not None and changed == previous:
                continue
            previous = changed
            m = self.dom_conn & ~(self.t_hold < tau)
            anchor = self._anchor(m, a1)
            newly = self.dom_conn & ~anchor & (self.lost == _BIG)
            self.lost[newly] = tau
            if self.separation is None:
                for cell, name in a2:
                    if cell is None or not anchor[cell]:
                        self.separation = tau
                        line = int(world.line[max(0, min(tau - 1, len(world.line) - 1))])
                        cut = int(world.cutidx[max(0, min(tau - 1, len(world.cutidx) - 1))])
                        self.sep_hit = OracleHit(
                            "M-TOWERSEP", where="program", cut=cut if cut >= 0 else None, time=tau,
                            line=line or None, point=None,
                            detail="the anchor point a2 of %s is cut off from tower 1's material %s" % (
                                name, "by the prior removal" if tau == 0 else "at line %d" % line))
                        break

    def _anchor(self, m: np.ndarray, a1: tuple[int, int] | None) -> np.ndarray:
        rows, starts, ends = [], [], []
        for iy in range(m.shape[0]):
            row = m[iy]
            if not row.any():
                continue
            d = np.diff(np.concatenate([[0], row.view(np.int8), [0]]))
            s, e = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
            rows.extend([iy] * len(s))
            starts.extend(s.tolist())
            ends.extend(e.tolist())
        n = len(rows)
        parent = list(range(n))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        by_row: dict[int, list[int]] = {}
        for i, r in enumerate(rows):
            by_row.setdefault(r, []).append(i)
        for r, ids in by_row.items():
            above = by_row.get(r - 1)
            if not above:
                continue
            j = 0
            for i in ids:
                while j < len(above) and ends[above[j]] <= starts[i]:
                    j += 1
                k = j
                while k < len(above) and starts[above[k]] < ends[i]:
                    ri, rk = find(i), find(above[k])
                    if ri != rk:
                        parent[ri] = rk
                    k += 1
        anchored: set[int] = set()

        def run_of(cell: tuple[int, int] | None) -> int | None:
            if cell is None or not m[cell]:
                return None
            for i in by_row.get(cell[0], []):
                if starts[i] <= cell[1] < ends[i]:
                    return i
            return None
        r1 = run_of(a1)
        if r1 is not None:
            anchored.add(find(r1))
        counts: dict[int, int] = {}
        for cell in self.fix_cells:
            r = run_of(cell)
            if r is not None:
                counts[find(r)] = counts.get(find(r), 0) + 1
        anchored.update(root for root, cnt in counts.items() if cnt >= 2)
        out = np.zeros(m.shape, dtype=bool)
        for i in range(n):
            if find(i) in anchored:
                out[rows[i], starts[i]:ends[i]] = True
        return out

    def detached(self, px: np.ndarray, py: np.ndarray, t: np.ndarray) -> np.ndarray:
        if not self.active:
            return np.zeros(np.shape(px), dtype=bool)
        ix = np.rint((px - self.gx0) / GRID_MM).astype(np.int64)
        iy = np.rint((py - self.gy0) / GRID_MM).astype(np.int64)
        ok = (ix >= 0) & (ix < len(self.gx)) & (iy >= 0) & (iy < len(self.gy))
        ixc, iyc = np.clip(ix, 0, len(self.gx) - 1), np.clip(iy, 0, len(self.gy) - 1)
        tt = np.broadcast_to(t, np.shape(px))
        return ok & self.dom_exact[iyc, ixc] & (self.t_rem[iyc, ixc] >= tt) & (self.lost[iyc, ixc] <= tt)

    def cut_off(self, px: np.ndarray, py: np.ndarray, tau: np.ndarray) -> np.ndarray:
        """Whether each point lies outside the exact anchor that held at time ``tau``: the grid point nearest it
        (within the half-diagonal) left the grid's anchor at a checkpoint no later than tau. The grid's anchor
        holds every grid point within GRID_SHRINK_MM of an exactly anchored point, so such a point is not anchored
        either. Whether the point is removed is the caller's to check, exactly."""
        if not self.active:
            return np.zeros(np.shape(px), dtype=bool)
        ix = np.rint((px - self.gx0) / GRID_MM).astype(np.int64)
        iy = np.rint((py - self.gy0) / GRID_MM).astype(np.int64)
        ok = (ix >= 0) & (ix < len(self.gx)) & (iy >= 0) & (iy < len(self.gy))
        ixc, iyc = np.clip(ix, 0, len(self.gx) - 1), np.clip(iy, 0, len(self.gy) - 1)
        return ok & (self.lost[iyc, ixc] <= np.broadcast_to(tau, np.shape(px)))

    def holding_from(self, m: np.ndarray) -> np.ndarray:
        """For each sample m, the first holding segment at or after it (end_time + 1 when there is none): the
        anchor at that time is exactly the anchor at m, since nothing between them breaks holding material."""
        last = self.world.end_time + 1
        if len(self.hold_idx) == 0:
            return np.full(np.shape(m), last, dtype=np.int64)
        k = np.searchsorted(self.hold_idx, m, side="left")
        return np.where(k < len(self.hold_idx), self.hold_idx[np.minimum(k, len(self.hold_idx) - 1)], last)

    def candidates(self, stride: int = 2) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
        """Every grid point (every ``stride``-th) that is ever detached: x, y, when lost, when removed."""
        if not self.active:
            return None
        key = ("cand", stride)
        if key not in self._cand:
            lost = self.lost[::stride, ::stride]
            mask = self.dom_exact[::stride, ::stride] & (lost < _BIG)
            iy, ix = np.nonzero(mask)
            self._cand[key] = (self.gx[::stride][ix], self.gy[::stride][iy], lost[iy, ix],
                               self.t_rem[::stride, ::stride][iy, ix])
        return self._cand[key]

    def detached_points(self, t: int, stride: int = 2) -> np.ndarray:
        cand = self.candidates(stride)
        if cand is None:
            return np.zeros((0, 2))
        x, y, lost, t_rem = cand
        live = (lost <= t) & (t_rem >= t)
        return np.stack([x[live], y[live]], axis=1)


def build_world(program: Any, job_toml: Path, machine_toml: Path, params_json: Path, *,
                gates: Mapping[str, Any] | None = None, measured: Mapping[str, Any] | None = None) -> World:
    """The oracle's world for a program (the panel, the zones, the sampled path, removal, structure)."""
    p = resolve_params(job_toml, machine_toml, params_json, gates)
    world = World(program, p, measured)
    world.notes.extend(p.notes)
    world.notes.extend(_relaxation_notes(p))
    return world


def _relaxation_notes(p: OracleParams) -> list[str]:
    out = []
    if p.gates.open("tabs_hold"):
        out.append("relaxation R-TABS: t_hold = %s mm (%s)" % (p.t_hold, p.source["t_hold"]))
    if p.kerf is not None:
        out.append("relaxation R-KERF: recesses shallower than h_r = %s mm are no wheel keep-out" % p.kerf[2])
        if p.kerf[0] > 0:
            out.append("R-KERF with kerf_width_max %s mm: kerf crossing is not modelled by the oracle, so it reports "
                       "no wheel over removed material at all (it refuses less, never more)" % p.kerf[0])
    if any(v > 0 for v in p.strips.values()):
        out.append("relaxation R-SUPPORT: support strips %s" % {k: v for k, v in p.strips.items() if v > 0})
    if p.fixations:
        out.append("relaxation R-FIXED: a component holding two declared fixation points counts as anchored")
    return out


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


class _Collector:
    """Hits grouped by (check, rule, wheel, stage): the first HITS_PER_GROUP and the deepest of each group."""

    def __init__(self) -> None:
        self.groups: dict[tuple, list[OracleHit]] = {}

    def add(self, hit: OracleHit) -> None:
        key = (hit.check, hit.rule, hit.wheel, hit.where, hit.cut, hit.heading_c)
        group = self.groups.setdefault(key, [])
        if len(group) < HITS_PER_GROUP:
            group.append(hit)
        elif hit.depth_mm is not None and all(h.depth_mm is None or hit.depth_mm > h.depth_mm for h in group[1:]):
            group[-1] = hit

    def extend(self, hits: Sequence[OracleHit]) -> None:
        for h in hits:
            self.add(h)

    def hits(self) -> list[OracleHit]:
        return [h for group in self.groups.values() for h in group]


class _Checker:
    def __init__(self, world: World) -> None:
        self.w = world
        self.p = world.p
        p = self.p
        self.removal = world.wheel_removal()
        self.prior = world.wheel_prior()
        if p.kerf is not None and p.kerf[0] > 0:
            self.removal, self.prior = self.removal[:0], []
        self.fix_pts = np.array([pt for pt, _ in p.fixations], dtype=float).reshape(-1, 2)
        self.obstacles = [poly for poly, _ in p.obstacles]
        self.index = _SegIndex(self.removal, p.rho + p.r_cap + 1.0)

    # -- primitives ---------------------------------------------------------------------------------------------

    def _removal_viol(self, px: np.ndarray, py: np.ndarray, t: np.ndarray, radius: Any) -> np.ndarray:
        """How far a disc of ``radius`` (a number or one per point) reaches into removal made before time t:
        point-to-segment distances to the tool's path (grown by r_tool + delta_tool) and to prior removal."""
        out = np.full(px.shape, -np.inf)
        radius = np.broadcast_to(np.asarray(radius, dtype=float), px.shape)
        seg = self.removal
        if len(seg) and len(px) and float(radius.max()) + self.p.r_cap <= self.index.reach:
            out = self.index.reach_into(px, py, t, radius + self.p.r_cap)
        elif len(seg) and len(px):
            reach = radius + self.p.r_cap
            big = float(reach.max())
            lo_x, hi_x, lo_y, hi_y = px.min() - big, px.max() + big, py.min() - big, py.max() + big
            keep = (np.maximum(seg[:, 0], seg[:, 2]) >= lo_x) & (np.minimum(seg[:, 0], seg[:, 2]) <= hi_x) & \
                   (np.maximum(seg[:, 1], seg[:, 3]) >= lo_y) & (np.minimum(seg[:, 1], seg[:, 3]) <= hi_y) & \
                   (seg[:, 5] < t.max())
            s = seg[keep]
            if len(s):
                step = max(1, 2_000_000 // max(1, len(s)))
                for a in range(0, len(px), step):
                    b = a + step
                    d = _seg_dist(px[a:b, None], py[a:b, None], s[:, 0], s[:, 1], s[:, 2], s[:, 3])
                    d = np.where(s[:, 5][None, :] < t[a:b, None], d, np.inf)
                    out[a:b] = np.maximum(out[a:b], reach[a:b] - d.min(axis=1))
        for poly, _ in self.prior:
            out = np.maximum(out, radius - _poly_dist(px, py, poly))
        return out

    def _zone_viol(self, px: np.ndarray, py: np.ndarray, radius: Any, zones: Sequence) -> tuple[np.ndarray, list]:
        """How far a disc of ``radius`` reaches into the grown zones: the distance to each triangle, 0 inside it by
        the spec's exact triangle test."""
        best = np.full(px.shape, -np.inf)
        names = np.full(px.shape, -1)
        for i, (_name, tri) in enumerate(zones):
            v = radius + self.w.inflate - _tri_dist(px, py, tri)
            better = v > best
            best = np.where(better, v, best)
            names = np.where(better, i, names)
        return best, [zones[i][0] if i >= 0 else None for i in names.tolist()]

    def _obst_viol(self, px: np.ndarray, py: np.ndarray, radius: Any) -> np.ndarray:
        out = np.full(px.shape, -np.inf)
        for poly in self.obstacles:
            out = np.maximum(out, radius - _poly_dist(px, py, poly))
        for fx, fy in self.fix_pts:
            out = np.maximum(out, radius - np.hypot(px - fx, py - fy))
        return out

    def _detached_disc(self, cx: np.ndarray, cy: np.ndarray, t: np.ndarray, radius: float) -> np.ndarray:
        """Whether the disc meets detached material: its centre and 16 rim points, each within the disc on the grid."""
        st = self.w.structure
        if not st.active:
            return np.zeros(cx.shape, dtype=bool)
        hit = st.detached(cx, cy, t)
        r = radius - _HALF_DIAGONAL
        if r > 0:
            for ux, uy in _RIM:
                hit |= st.detached(cx + r * ux, cy + r * uy, t)
        return hit

    def _hull_offsets(self, c: float) -> np.ndarray:
        """The body hull at heading C as a polygon of offsets from the tool point, and its outward reach."""
        p = self.p
        offs = np.array(support_offsets(p, c))
        if p.body_outline is None:
            return _hull(offs)
        pts = [offs[i] + p.rho * _RIM for i in range(len(offs))]
        pts.append(_outline_offsets(p.body_outline, c, p.heading_offset))
        return _hull(np.concatenate(pts))

    def _hull_grow(self) -> float:
        return self.p.rho + self.p.m_body if self.p.body_outline is None else 0.0

    # -- the wheels at fixed headings ---------------------------------------------------------------------------

    def wheels(self, px: np.ndarray, py: np.ndarray, cdeg: np.ndarray, t: np.ndarray, line: np.ndarray, *,
               where: str, cut: int | None, composite: str | None, heading: int | None,
               only: frozenset[str] | None = None) -> list[OracleHit]:
        """Every wheel, body and reach check at tool points px, py with headings cdeg at times t."""
        p, w = self.p, self.w
        out: list[OracleHit] = []

        def report(check: str, mask: np.ndarray, depth: np.ndarray | None, wx: np.ndarray, wy: np.ndarray,
                   wheel: int | None, detail: Any = "") -> None:
            if only is not None and check not in only:
                return
            idx = np.nonzero(mask)[0]
            if len(idx) == 0:
                return
            picks = list(idx[:HITS_PER_GROUP])
            if depth is not None:
                picks.append(int(idx[np.argmax(depth[idx])]))
            for i in dict.fromkeys(int(v) for v in picks):
                name, rule = (composite, check) if composite else (check, None)
                if check == "P-REACH":
                    name, rule = "P-REACH", None
                text = detail[i] if isinstance(detail, list) else detail
                out.append(OracleHit(name, rule=rule, where=where, cut=cut, wheel=wheel,
                                     point=(float(wx[i]), float(wy[i])), time=int(t[i]), heading_c=heading,
                                     line=int(line[i]) or None, depth_mm=None if depth is None else float(depth[i]),
                                     detail=text))

        theta = np.radians(cdeg + p.heading_offset)
        for s in p.supports:
            a = theta + math.radians(s.delta)
            wx, wy = px - s.r * np.cos(a), py - s.r * np.sin(a)
            v = -w.wa_wheel.slack(wx, wy, p.rho)
            report("W-AREA", v > HIT_TOL_MM, v, wx, wy, s.wheel, "the wheel disc leaves the wheel work area")
            v, names = self._zone_viol(wx, wy, p.rho, w.wheel_zones)
            report("W-TOWER", v > HIT_TOL_MM, v, wx, wy, s.wheel, ["meets %s grown by %g mm" % (n, w.inflate)
                                                                    for n in names])
            if only is None or "W-SURF" in only:
                v = self._removal_viol(wx, wy, t, p.rho)
                report("W-SURF", v > HIT_TOL_MM, v, wx, wy, s.wheel, "the wheel disc meets material removed before")
            if only is None or "M-ATTACH" in only:
                hit = self._detached_disc(wx, wy, t, p.rho)
                report("M-ATTACH", hit, None, wx, wy, s.wheel, "the wheel disc meets detached material")
            if self.obstacles or len(self.fix_pts):
                v = self._obst_viol(wx, wy, p.rho)
                report("W-OBST", v > HIT_TOL_MM, v, wx, wy, s.wheel, "the wheel disc meets a declared obstacle")
        if only is not None and not ({"B-TOWER", "B-OBST", "P-REACH"} & only):
            return out
        # The body hull turns with the heading: hull(C) = R(C) hull(0). A point X meets it where R(-C)(X - p)
        # meets hull(0), so every heading of a sweep is one vectorised query against one polygon.
        grow = self._hull_grow()
        hull0 = self._hull0()
        ang = np.radians(cdeg)
        cos_a, sin_a = np.cos(ang), np.sin(ang)

        def local(x: Any, y: Any) -> tuple[np.ndarray, np.ndarray]:
            rx, ry = x - px, y - py
            return rx * cos_a + ry * sin_a, -rx * sin_a + ry * cos_a
        for name, (tx, ty) in w.towers:
            qx, qy = local(tx, ty)
            v = p.base_radius + grow - _signed_dist(qx, qy, hull0)
            report("B-TOWER", v > HIT_TOL_MM, v, px, py, None, "the body hull meets the base of %s" % name)
            if p.wire_point is None:
                far = np.max(np.hypot(hull0[:, 0][None, :] - qx[:, None], hull0[:, 1][None, :] - qy[:, None]),
                             axis=1) + grow
            else:
                off = _outline_offsets(np.array([p.wire_point]), 0.0, p.heading_offset)[0]
                far = np.hypot(off[0] - qx, off[1] - qy)
            v = far - p.reach_max
            report("P-REACH", v > HIT_TOL_MM, v, px, py, None,
                   "the wire attachment is farther than reach_max from %s" % name)
        for fx, fy in self.fix_pts:
            qx, qy = local(fx, fy)
            v = grow - _signed_dist(qx, qy, hull0)
            report("B-OBST", v > HIT_TOL_MM, v, px, py, None, "the body hull meets a declared fixation")
        for poly in self.obstacles:
            v = np.empty(len(px))
            for i in range(len(px)):
                rx, ry = poly[:, 0] - px[i], poly[:, 1] - py[i]
                local_poly = np.stack([rx * cos_a[i] + ry * sin_a[i], -rx * sin_a[i] + ry * cos_a[i]], axis=1)
                v[i] = grow + _poly_poly_pen(hull0, local_poly)
            report("B-OBST", v > HIT_TOL_MM, v, px, py, None, "the body hull meets a declared obstacle")
        return out

    def _hull0(self) -> np.ndarray:
        if not hasattr(self, "_hull_cache"):
            self._hull_cache = self._hull_offsets(0.0)
        return self._hull_cache

    # -- any-heading discs: rotations, envelopes, the safe zone ---------------------------------------------------

    def discs(self, px: np.ndarray, py: np.ndarray, radius: np.ndarray, body: np.ndarray, t: int, *, where: str,
              composite: str, cut: int | None, line: int | None, containment: bool = True,
              removal: bool = True, detached: bool = True, keepout_only: bool = False) -> list[OracleHit]:
        """Discs of ``radius`` (any heading, any turn) and of ``body`` against every wheel and body keep-out."""
        p, w = self.p, self.w
        out: list[OracleHit] = []
        tt = np.full(px.shape, t, dtype=np.int64)

        def report(rule: str, v: np.ndarray, detail: str, check: str | None = None) -> None:
            idx = np.nonzero(v > HIT_TOL_MM)[0]
            if len(idx) == 0:
                return
            for i in dict.fromkeys([int(idx[0]), int(idx[np.argmax(v[idx])])]):
                name = check or composite
                out.append(OracleHit(name, rule=None if check else rule, where=where, cut=cut,
                                     point=(float(px[i]), float(py[i])), time=t, line=line, depth_mm=float(v[i]),
                                     detail=detail))
        if containment and not keepout_only:
            report("W-AREA" if composite != "W-SAFEZONE" else "containment", -w.wa_wheel.slack(px, py, radius),
                   "the any-heading disc leaves the wheel work area")
        zone = np.full(px.shape, -np.inf)
        for _name, tri in w.wheel_zones:
            zone = np.maximum(zone, radius + w.inflate - _tri_dist(px, py, tri))
        report("W-TOWER" if composite != "W-SAFEZONE" else "rotation", zone, "the any-heading disc meets a tower zone")
        if self.obstacles or len(self.fix_pts):
            report("W-OBST" if composite != "W-SAFEZONE" else "rotation", self._obst_viol(px, py, radius),
                   "the any-heading disc meets an obstacle")
        if removal:
            report("W-SURF" if composite != "W-SAFEZONE" else "rotation", self._removal_viol(px, py, tt, radius),
                   "the any-heading disc meets removed material")
        if detached and w.structure.active:
            pts = w.structure.detached_points(t)
            if len(pts):
                d = np.min(np.hypot(pts[:, 0][None, :] - px[:, None], pts[:, 1][None, :] - py[:, None]), axis=1)
                report("M-ATTACH" if composite != "W-SAFEZONE" else "rotation", radius - d - _HALF_DIAGONAL,
                       "the any-heading disc meets detached material")
        if keepout_only:
            return out
        base = np.full(px.shape, -np.inf)
        reach = np.full(px.shape, -np.inf)
        for _name, (tx, ty) in w.towers:
            dist = np.hypot(px - tx, py - ty)
            base = np.maximum(base, body + p.base_radius - dist)
            if p.wire_point is None:
                reach = np.maximum(reach, dist + body - p.reach_max)
            else:
                rr = math.hypot(*p.wire_point)
                reach = np.maximum(reach, dist + rr - p.reach_max)
        report("B-TOWER" if composite != "W-SAFEZONE" else "body", base, "the any-heading body disc meets a tower base")
        if composite != "W-SAFEZONE":
            report("", reach, "the any-heading attachment is farther than reach_max", check="P-REACH")
        if (self.obstacles or len(self.fix_pts)) and composite != "W-SAFEZONE":
            report("B-OBST", self._obst_viol(px, py, body), "the any-heading body disc meets an obstacle")
        return out

    def annulus(self, idx: np.ndarray) -> list[OracleHit]:
        """W-SAFEZONE during translations: the annulus r_min - rho .. R_rot + rho against K_wheel, Rem and Detached."""
        p, w = self.p, self.w
        inner, outer = p.r_min - p.rho, p.r_rot + p.rho
        px, py, t = w.x[idx], w.y[idx], idx.astype(np.int64)
        out: list[OracleHit] = []
        viol = np.full(px.shape, -np.inf)
        kind = np.full(px.shape, "", dtype=object)

        def merge(v: np.ndarray, what: str) -> None:
            nonlocal viol
            better = v > viol
            viol = np.where(better, v, viol)
            kind[better] = what
        for name, tri in w.wheel_zones:
            dmin = _tri_dist(px, py, tri) - w.inflate
            dmax = np.max(np.hypot(np.array(tri)[:, 0][None, :] - px[:, None],
                                   np.array(tri)[:, 1][None, :] - py[:, None]), axis=1) + w.inflate
            merge(np.minimum(outer - dmin, dmax - inner), "a tower zone (%s)" % name)
        seg = self.removal
        if len(seg):
            for a in range(0, len(px), 256):
                sl = slice(a, a + 256)
                dmin = _seg_dist(px[sl, None], py[sl, None], seg[:, 0], seg[:, 1], seg[:, 2], seg[:, 3]) - p.r_cap
                dmax = np.maximum(np.hypot(seg[:, 0] - px[sl, None], seg[:, 1] - py[sl, None]),
                                  np.hypot(seg[:, 2] - px[sl, None], seg[:, 3] - py[sl, None])) + p.r_cap
                v = np.minimum(outer - dmin, dmax - inner)
                v = np.where(seg[:, 5][None, :] < t[sl, None], v, -np.inf)
                merge(_place(viol, v.max(axis=1), sl), "removed material")
        for poly, _ in self.prior:
            dmin = _poly_dist(px, py, poly)
            dmax = np.max(np.hypot(poly[:, 0][None, :] - px[:, None], poly[:, 1][None, :] - py[:, None]), axis=1)
            merge(np.minimum(outer - dmin, dmax - inner), "prior removal")
        for poly in self.obstacles:
            dmin = _poly_dist(px, py, poly)
            dmax = np.max(np.hypot(poly[:, 0][None, :] - px[:, None], poly[:, 1][None, :] - py[:, None]), axis=1)
            merge(np.minimum(outer - dmin, dmax - inner), "an obstacle")
        for fx, fy in self.fix_pts:
            d = np.hypot(px - fx, py - fy)
            merge(np.minimum(outer - d, d - inner), "a fixation")
        if w.structure.active:
            cand = w.structure.candidates(stride=3)
            if cand is not None:
                cx, cy, lost, t_rem = cand
                for i in range(len(px)):
                    live = (lost <= t[i]) & (t_rem >= t[i])
                    if live.any():
                        d = np.hypot(cx[live] - px[i], cy[live] - py[i])
                        band = (d <= outer - _HALF_DIAGONAL) & (d >= inner + _HALF_DIAGONAL)
                        if band.any():
                            merge(_place(viol, np.array([HIT_TOL_MM * 2]), slice(i, i + 1)), "detached material")

        def report(rule: str, v: np.ndarray, what: Any) -> None:
            hits = np.nonzero(v > HIT_TOL_MM)[0]
            if len(hits) == 0:
                return
            for i in dict.fromkeys([int(k) for k in hits[:HITS_PER_GROUP]] + [int(hits[np.argmax(v[hits])])]):
                cut = int(w.cutidx[idx[i]])
                out.append(OracleHit("W-SAFEZONE", rule=rule, where="cut" if cut >= 0 else "link",
                                     cut=cut if cut >= 0 else None, point=(float(px[i]), float(py[i])),
                                     time=int(t[i]), line=int(w.line[idx[i]]) or None, depth_mm=float(v[i]),
                                     detail=what[i] if isinstance(what, np.ndarray) else what))
        report("annulus", viol, np.array(["the safe-zone annulus meets %s" % k for k in kind], dtype=object))
        # Containment and the body bound, at every tool point (safety-model 8.1).
        report("containment", -w.wa_wheel.slack(px, py, outer), "the disc of radius R_rot + rho leaves the wheel "
               "work area")
        body = np.full(px.shape, -np.inf)
        for _name, (tx, ty) in w.towers:
            body = np.maximum(body, p.r_body + p.base_radius - np.hypot(px - tx, py - ty))
        report("body", body, "the disc of radius R_body meets a tower base")
        return out


class _SegIndex:
    """Removal segments bucketed on a square grid, each in every cell within ``reach`` of it.

    A query point within ``reach`` of a segment lies in a cell the segment
    is registered in, so looking only at the query's own cell finds every
    segment that can reach it: the same answer as comparing with all of them.
    """

    CELL_MM = 32.0

    def __init__(self, seg: np.ndarray, reach: float) -> None:
        self.seg = seg
        self.reach = reach
        if len(seg) == 0:
            self.keys = np.zeros(0, dtype=np.int64)
            return
        c = self.CELL_MM
        x0 = np.floor((np.minimum(seg[:, 0], seg[:, 2]) - reach) / c).astype(np.int64)
        x1 = np.floor((np.maximum(seg[:, 0], seg[:, 2]) + reach) / c).astype(np.int64)
        y0 = np.floor((np.minimum(seg[:, 1], seg[:, 3]) - reach) / c).astype(np.int64)
        y1 = np.floor((np.maximum(seg[:, 1], seg[:, 3]) + reach) / c).astype(np.int64)
        cells, owners = [], []
        for i in range(len(seg)):
            xs, ys = np.arange(x0[i], x1[i] + 1), np.arange(y0[i], y1[i] + 1)
            gx, gy = np.meshgrid(xs, ys)
            cells.append(self._key(gx.ravel(), gy.ravel()))
            owners.append(np.full(gx.size, i, dtype=np.int64))
        keys, own = np.concatenate(cells), np.concatenate(owners)
        order = np.argsort(keys, kind="stable")
        self.keys, self.own = keys[order], own[order]

    @staticmethod
    def _key(ix: np.ndarray, iy: np.ndarray) -> np.ndarray:
        return (ix + (1 << 20)) * (1 << 21) + (iy + (1 << 20))

    def reach_into(self, px: np.ndarray, py: np.ndarray, t: np.ndarray, reach: np.ndarray) -> np.ndarray:
        """max over segments removed before t of (reach - distance), per query point (-inf when none is near)."""
        out = np.full(px.shape, -np.inf)
        if len(self.keys) == 0:
            return out
        c = self.CELL_MM
        q = self._key(np.floor(px / c).astype(np.int64), np.floor(py / c).astype(np.int64))
        order = np.argsort(q, kind="stable")
        qs = q[order]
        bounds = np.nonzero(np.diff(qs))[0] + 1
        starts = np.concatenate([[0], bounds])
        ends = np.concatenate([bounds, [len(qs)]])
        lo = np.searchsorted(self.keys, qs[starts], side="left")
        hi = np.searchsorted(self.keys, qs[starts], side="right")
        s = self.seg
        for a, b, l0, l1 in zip(starts.tolist(), ends.tolist(), lo.tolist(), hi.tolist()):
            if l1 <= l0:
                continue
            members = order[a:b]
            cand = s[self.own[l0:l1]]
            d = _seg_dist(px[members, None], py[members, None], cand[:, 0], cand[:, 1], cand[:, 2], cand[:, 3])
            d = np.where(cand[:, 5][None, :] < t[members, None], d, np.inf)
            out[members] = reach[members] - d.min(axis=1)
        return out


def _place(full: np.ndarray, part: np.ndarray, sl: slice) -> np.ndarray:
    out = np.full(full.shape, -np.inf)
    out[sl] = part
    return out


def _poly_poly_pen(a: np.ndarray, b: np.ndarray) -> float:
    """How deep two polygons overlap (positive), or minus the gap between them.

    The depth is a lower bound: the deepest of each polygon's boundary points
    (sampled at SAMPLE_MM) inside the other, measured to the other's
    boundary; a crossing of edges with no sampled point inside counts as 0.
    """
    sa, sb = _boundary_samples(a), _boundary_samples(b)
    depth = max(float(-_signed_dist(sa[:, 0], sa[:, 1], b).min()), float(-_signed_dist(sb[:, 0], sb[:, 1], a).min()))
    if depth > 0:
        return depth
    a1, a2 = a[:, None, :], np.roll(a, -1, axis=0)[:, None, :]
    b1, b2 = b[None, :, :], np.roll(b, -1, axis=0)[None, :, :]
    if _segments_cross(a1, a2, b1, b2).any():
        return 0.0
    return -float(min(_edge_dists(a[:, 0], a[:, 1], b).min(), _edge_dists(b[:, 0], b[:, 1], a).min()))


# ---------------------------------------------------------------------------
# Turning: the headings a rotation passes through
# ---------------------------------------------------------------------------


def _turn(c0: int, c1: int, model: str | None) -> np.ndarray:
    """The headings of a turn from c0 to c1, ROT_STEP_DEG apart: the short way under in-place-shortest, else
    numerically (every emitted step below 180 degrees: the long way across the wrap, safety-model 9.2)."""
    if model == "in-place-shortest":
        delta = ((c1 - c0 + 180) % 360) - 180
    else:
        delta = c1 - c0
    n = max(1, int(math.ceil(abs(delta) / ROT_STEP_DEG)))
    return c0 + delta * np.arange(n + 1) / n


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


class _Run:
    def __init__(self, world: World) -> None:
        self.w = world
        self.p = world.p
        self.ck = _Checker(world)
        self.cache: dict[tuple, list[OracleHit]] = {}

    # -- stages at a heading ------------------------------------------------------------------------------------

    def _range(self, i0: int, i1: int) -> np.ndarray:
        return np.arange(i0, i1 + 1)

    def fixed(self, i0: int, i1: int, c: int, *, where: str, cut: int | None, composite: str | None,
              only: frozenset[str] | None = None) -> list[OracleHit]:
        key = ("fixed", i0, i1, c, where, composite, only)
        if key not in self.cache:
            idx = self._range(i0, i1)
            w = self.w
            self.cache[key] = self.ck.wheels(w.x[idx], w.y[idx], np.full(len(idx), float(c)), idx.astype(np.int64),
                                             w.line[idx], where=where, cut=cut, composite=composite, heading=c,
                                             only=only)
        return self.cache[key]

    def rotation(self, pivot: tuple[float, float], t: int, c0: int, c1: int, *, where: str, cut: int | None,
                 line: int | None) -> list[OracleHit]:
        """A rotation in place at ``pivot`` (W-ROT), and the safe zone's full disc while it applies."""
        key = ("rot", pivot, t, c0, c1, where)
        if key in self.cache:
            return self.cache[key]
        p = self.p
        px, py = np.array([pivot[0]]), np.array([pivot[1]])
        out: list[OracleHit] = []
        if p.rotation_model is None:
            out += self.ck.discs(px, py, np.array([p.r_rot + p.rho]), np.array([p.r_body]), t, where=where,
                                 composite="W-ROT", cut=cut, line=line)
        else:
            cs = _turn(c0, c1, p.rotation_model)
            n = len(cs)
            out += self.ck.wheels(np.full(n, pivot[0]), np.full(n, pivot[1]), cs, np.full(n, t, dtype=np.int64),
                                  np.full(n, line or 0, dtype=np.int64), where=where, cut=cut, composite="W-ROT",
                                  heading=c1)
        if p.safe_zone:
            out += self.ck.discs(px, py, np.array([p.r_rot + p.rho]), np.array([p.r_body]), t, where=where,
                                 composite="W-SAFEZONE", cut=cut, line=line, containment=False, keepout_only=True)
        self.cache[key] = out
        return out

    def combined(self, pa: tuple, pb: tuple, t: int, c0: int, c1: int, *, composite: str, where: str,
                 cut: int | None, line: int | None, start_tol: float = 0.0) -> list[OracleHit]:
        """A line that turns and translates together: the any-rotation capsule, or E_k along one direction."""
        key = ("comb", pa, pb, t, c0, c1, composite, start_tol)
        if key in self.cache:
            return self.cache[key]
        p = self.p
        length = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
        n = max(1, int(math.ceil(length / SAMPLE_MM)))
        f = np.arange(n + 1) / n
        px, py = pa[0] + (pb[0] - pa[0]) * f, pa[1] + (pb[1] - pa[1]) * f
        if p.rotation_model is None or c0 is None or (composite == "W-FIRST" and not p.heading_map):
            r = p.r_rot + p.rho + start_tol * (1.0 - f)
            body = p.r_body + start_tol * (1.0 - f)
            first = composite == "W-FIRST"
            out = self.ck.discs(px, py, r, body, t, where=where, composite=composite, cut=cut, line=line,
                                removal=True, detached=not first)
        else:
            cs = _turn(c0, c1, p.rotation_model)
            m = len(cs)
            xs = np.concatenate([np.full(m, pa[0]), np.full(m, pb[0]), px, px, pa[0] + (pb[0] - pa[0]) *
                                 np.linspace(0, 1, m)])
            ys = np.concatenate([np.full(m, pa[1]), np.full(m, pb[1]), py, py, pa[1] + (pb[1] - pa[1]) *
                                 np.linspace(0, 1, m)])
            hs = np.concatenate([cs, cs, np.full(len(px), float(c0)), np.full(len(px), float(c1)), cs])
            out = self.ck.wheels(xs, ys, hs, np.full(len(xs), t, dtype=np.int64),
                                 np.full(len(xs), line or 0, dtype=np.int64), where=where, cut=cut,
                                 composite=composite, heading=c1)
        self.cache[key] = out
        return out

    # -- plan mode: the dynamic programme over headings ----------------------------------------------------------

    def plan(self) -> tuple[list[OracleHit], dict[int, int] | None, dict[int, dict[int, tuple[str, ...]]]]:
        w = self.w
        cuts = w.program.cuts
        cut_events = [e for e in w.events if e.kind == "cut"]
        links = [e for e in w.events if e.kind == "translate"]
        first = next((e for e in w.events if e.kind == "first"), None)
        table: dict[int, dict[int, tuple[str, ...]]] = {}
        feasible: list[list[int]] = []
        all_hits: dict[tuple[int, int], list[OracleHit]] = {}
        for k, e in enumerate(cut_events):
            forced = cuts[k].forced_c
            cands = [forced] if forced is not None else list(range(0, 360, 10))
            table[k] = {}
            ok = []
            for c in cands:
                hits = self.fixed(e.i0, e.i1, c, where="cut", cut=k, composite=None)
                all_hits[(k, c)] = hits
                table[k][c] = tuple(sorted({h.check for h in hits}))
                if not hits:
                    ok.append(c)
            feasible.append(ok)
        if all(len(table[k]) == 1 for k in table):
            # Every heading is forced: one path, so every stage is checked and every violation reported.
            hs = {k: next(iter(table[k])) for k in table}
            out = [h for k in table for h in all_hits[(k, hs[k])]]
            out += self.first_line(first, hs[0])
            for k, link in enumerate(links):
                out += self.link(link, k, hs[k], hs[k + 1])
            out += self.park(hs[len(cut_events) - 1])
            return out, (None if out else hs), table
        blocked = [k for k, ok in enumerate(feasible) if not ok]
        if blocked:
            out = [h for k in blocked for c in table[k] for h in all_hits[(k, c)]]
            return out, None, table
        # The first line.
        reach: list[dict[int, int | None]] = []
        failures: list[OracleHit] = []
        first_ok: dict[int, int | None] = {}
        for c in feasible[0]:
            hits = self.first_line(first, c)
            if hits:
                failures.extend(hits)
            else:
                first_ok[c] = None
        if not first_ok:
            return failures, None, table
        reach.append(first_ok)
        for k, link in enumerate(links):
            nxt: dict[int, int | None] = {}
            fails: list[OracleHit] = []
            for b in feasible[k + 1]:
                for a in reach[k]:
                    hits = self.link(link, k, a, b)
                    if not hits:
                        nxt[b] = a
                        break
                    fails.extend(hits)
            if not nxt:
                return fails, None, table
            reach.append(nxt)
        park_fail: list[OracleHit] = []
        chosen_last = None
        for c in reach[-1]:
            hits = self.park(c)
            if not hits:
                chosen_last = c
                break
            park_fail.extend(hits)
        if chosen_last is None:
            return park_fail, None, table
        headings = {len(reach) - 1: chosen_last}
        for k in range(len(reach) - 1, 0, -1):
            prev = reach[k][headings[k]]
            assert prev is not None
            headings[k - 1] = prev
        return [], dict(sorted(headings.items())), table

    def first_line(self, first: _Event | None, c1: int) -> list[OracleHit]:
        p = self.p
        if p.start is None or first is None:
            return [OracleHit("W-FIRST", rule="no start pose", where="first", detail="no start pose: neither "
                              "position frames with the frame mapping a fact nor the measured park pose")]
        key = ("first", c1)
        if key in self.cache:
            return self.cache[key]
        _, _, sc, tol = p.start
        pa, pb = first.pa, first.pb
        hits = self.combined(pa, pb, 0, sc, c1, composite="W-FIRST", where="first", cut=None, line=first.line,
                             start_tol=tol if (sc is None or not p.heading_map or p.rotation_model is None) else 0.0)
        if hits and sc is not None and p.heading_map and p.rotation_model is not None and p.split_allowed:
            # The other form the emitter may write: translate at the parked heading, then turn in place.
            alt = self.fixed(first.i0, first.i1, sc, where="first", cut=None, composite="W-FIRST")
            if sc != c1:
                alt = alt + self.rotation(pb, 0, sc, c1, where="first", cut=None, line=first.line)
                alt = [OracleHit("W-FIRST", rule=h.rule, where="first", wheel=h.wheel, point=h.point, time=h.time,
                                 heading_c=h.heading_c, line=h.line, depth_mm=h.depth_mm, detail=h.detail)
                       if h.check == "W-ROT" else h for h in alt]
            hits = [] if not alt else hits + alt
        self.cache[key] = hits
        return hits

    def link(self, e: _Event, k: int, a: int, b: int) -> list[OracleHit]:
        p = self.p
        t = e.i0
        if a == b:
            return self.fixed(e.i0, e.i1, a, where="link", cut=k, composite="W-RAPID")
        if not p.split_allowed:
            return self.combined(e.pa, e.pb, t, a, b, composite="W-RAPID", where="link", cut=k, line=None)
        piv_a = self.rotation(e.pa, t, a, b, where="link", cut=k, line=None) + \
            self.fixed(e.i0, e.i1, b, where="link", cut=k, composite="W-RAPID")
        if not piv_a:
            return []
        piv_b = self.fixed(e.i0, e.i1, a, where="link", cut=k, composite="W-RAPID") + \
            self.rotation(e.pb, e.i1, a, b, where="link", cut=k, line=None)
        if not piv_b:
            return []
        return piv_a + piv_b

    def park(self, c: int) -> list[OracleHit]:
        p, w = self.p, self.w
        if not p.gates.open("post_job_motion_known"):
            return self._park_unknown()
        x, y = w.final
        end = w.end_time
        hits = self.ck.wheels(np.array([x]), np.array([y]), np.array([float(c)]), np.array([end], dtype=np.int64),
                              np.array([0]), where="end", cut=None, composite="W-PARK", heading=c,
                              only=frozenset({"W-SURF", "M-ATTACH"}))
        return hits

    def _park_unknown(self) -> list[OracleHit]:
        p, w = self.p, self.w
        out = []
        for cut in w.program.cuts:
            through = float(cut.max_depth) >= p.T - p.through_tol
            pocket = cut.op is not None and cut.op.kind == "pocket"
            if through or pocket:
                out.append(OracleHit("W-PARK", rule="post_job.motion unknown", where="end", cut=cut.id,
                                     line=cut.moves[0].src.line, detail="a %s while the fact post_job.motion is "
                                     "missing (U-36)" % ("through cut" if through else "pocket")))
        for _poly, depth in p.prior:
            if depth >= p.T - p.through_tol:
                out.append(OracleHit("W-PARK", rule="post_job.motion unknown", where="end",
                                     detail="prior removal holds a through cut while post_job.motion is missing"))
        return out[:HITS_PER_GROUP]

    # -- verify mode ----------------------------------------------------------------------------------------------

    def verify(self) -> list[OracleHit]:
        w, p = self.w, self.p
        out: list[OracleHit] = []
        for e in w.events:
            if e.kind == "first":
                if p.start is None:
                    out.append(OracleHit("W-FIRST", rule="no start pose", where="first", line=e.line,
                                         detail="no start pose"))
                    continue
                if e.c1 is None:
                    raise OracleError("line %d: the first line writes no C word, so its heading is unknown" % e.line)
                out.extend(self.first_line(e, int(e.c1)))
            elif e.kind == "cut":
                out.extend(self.fixed(e.i0, e.i1, int(e.c0), where="cut", cut=e.cut, composite=None))
            elif e.kind == "translate":
                out.extend(self.fixed(e.i0, e.i1, int(e.c0), where="link", cut=None, composite="W-RAPID"))
            elif e.kind == "rotate":
                if e.c0 is None:
                    raise OracleError("line %d turns from an unknown heading (no C word before it)" % e.line)
                out.extend(self.rotation(e.pa, e.i0, int(e.c0), int(e.c1), where="link", cut=None, line=e.line))
            elif e.kind == "combined":
                if e.c0 is None:
                    raise OracleError("line %d turns from an unknown heading" % e.line)
                out.extend(self.combined(e.pa, e.pb, e.i0, int(e.c0), int(e.c1), composite="W-RAPID", where="link",
                                         cut=None, line=e.line))
        if p.gates.open("post_job_motion_known"):
            if w.final_c is not None:
                out.extend(self.park(int(w.final_c)))
        else:
            out.extend(self._park_unknown())
        return out

    # -- heading-independent rules --------------------------------------------------------------------------------

    def tool_and_program(self) -> list[OracleHit]:
        w, p = self.w, self.p
        out: list[OracleHit] = []
        n = len(w.x)
        if n == 0:
            return out
        seg_mat = np.zeros(n, dtype=bool)
        if len(w.seg):
            s = w.seg[:, 5].astype(np.int64)
            seg_mat[s] = True
            seg_mat[s + 1] = True
        x, y, z = w.x, w.y, w.z

        def report(check: str, mask: np.ndarray, depth: np.ndarray | None, detail: str,
                   rule: str | None = None) -> None:
            hits = np.nonzero(mask)[0]
            if len(hits) == 0:
                return
            picks = list(hits[:HITS_PER_GROUP]) + ([int(hits[np.argmax(depth[hits])])] if depth is not None else [])
            for i in dict.fromkeys(int(v) for v in picks):
                cut = int(w.cutidx[i])
                out.append(OracleHit(check, rule=rule, where={_FIRST: "first", _CUT: "cut", _LINK: "link"}[int(
                    w.part[i])], cut=cut if cut >= 0 else None, point=(float(x[i]), float(y[i])), time=int(i),
                    line=int(w.line[i]) or None, depth_mm=None if depth is None else float(depth[i]), detail=detail))
        v = -w.wa_tool.slack(x, y, p.r_cap)
        report("T-AREA", seg_mat & (v > HIT_TOL_MM), v, "the tool disc leaves the tool work area")
        v, _ = self.ck._zone_viol(x, y, p.r_cap, w.tool_zones)
        report("T-TOWER", v > HIT_TOL_MM, v, "the tool disc meets a tool zone (grown by %g mm)" % w.inflate)
        base = np.full(n, -np.inf)
        for _name, (tx, ty) in w.towers:
            base = np.maximum(base, p.r_cap + p.base_radius - np.hypot(x - tx, y - ty))
        report("T-TOWER", base > HIT_TOL_MM, base, "the tool disc meets a tower base")
        st = w.structure
        report("T-ATTACH", self.t_attach(), None, "the tool reaches more than attach_tol into detached material "
               "that its path had not removed")
        for poly, height in p.obstacles:
            v = p.r_cap - _poly_dist(x, y, poly)
            report("T-OBST", (v > HIT_TOL_MM) & (z < height + p.obstacle_clearance), v,
                   "the tool passes an obstacle %s mm high too low" % height)
        for (fx, fy), height in p.fixations:
            v = p.r_cap - np.hypot(x - fx, y - fy)
            report("T-OBST", (v > HIT_TOL_MM) & (z < height + p.obstacle_clearance), v,
                   "the tool passes a fixation %s mm high too low" % height)
        tallest = max([h for _, h in p.obstacles] + [h for _, h in p.fixations], default=None)
        if tallest is not None and p.z_safety < tallest + p.obstacle_clearance - 1e-9:
            out.append(OracleHit("T-OBST", detail="Z_safety %s mm is lower than the tallest obstacle %s mm + %s mm"
                                 % (p.z_safety, tallest, p.obstacle_clearance)))
        depth = -z
        report("T-DEPTH", z < -(p.T + p.overcut_max) - 1e-9, -(z + p.T + p.overcut_max), "below -(T + overcut_max)")
        report("T-DEPTH", depth > p.flute + 1e-9, depth - p.flute, "deeper than the flute length")
        report("T-DEPTH", depth > p.z_travel + 1e-9, depth - p.z_travel, "deeper than the Z travel")
        report("T-DEPTH", z > p.z_upper + 1e-9, z - p.z_upper, "above the Z upper limit")
        if p.tool_d > p.tool_max_d:
            out.append(OracleHit("T-TOOL", detail="tool diameter %s mm above %s mm" % (p.tool_d, p.tool_max_d)))
        if p.stickout is not None and p.stickout > p.tool_max_len:
            out.append(OracleHit("T-TOOL", detail="stick-out %s mm above %s mm" % (p.stickout, p.tool_max_len)))
        if max(p.W, p.H) > p.panel_max:
            out.append(OracleHit("P-RANGE", where="program", detail="panel %s x %s above %s mm" % (p.W, p.H,
                                                                                                 p.panel_max)))
        if p.piece_length < p.piece_min:
            out.append(OracleHit("M-INVARIANT", detail="piece length %s mm below %s mm" % (p.piece_length,
                                                                                         p.piece_min)))
        if st.sep_hit is not None:
            out.append(st.sep_hit)
        if p.safe_zone:
            sel = np.nonzero(w.part != _FIRST)[0]
            if len(sel):
                out.extend(self.ck.annulus(sel))
        out.extend(self._gates())
        if w.mode == "verify":
            out.extend(self._dialect())
        if len(w.program.tools) > 1:
            out.append(OracleHit("D-SINGLETOOL", detail="%d tools" % len(w.program.tools)))
        return out

    def t_attach(self) -> np.ndarray:
        """T-ATTACH at each sample: the tool reaches more than attach_tol into Detached(j) (safety-model 7.4).

        At a sample the tool's own disc is already removed (removal at sample t
        is the path up to t), so the check looks at what the tool reaches that
        its path had not removed one sample step (SAMPLE_MM of path length)
        before, at the point B. Probes: the tool point and 16 rim points at
        r_tool + delta_tool - 2 attach_tol - HIT_TOL_MM about the end of every
        in-material segment (one attach_tol of room for where the checker's
        piece boundary falls between B and the sample). A probe counts when it
        lies in P_work, in no declared prior removal, farther than r_tool +
        delta_tool + e_max from the path up to B, and outside the anchor that
        held when the move through B began (the grid's anchor up to the first
        holding segment from that move's start on, which is the same anchor).
        The checker's pieces never span moves, so the piece that reaches the
        probe sees no larger anchor. B is a path length, so splitting a move
        into collinear pieces only adds hits. A plunge into a loose part and a
        cut that runs across a kerf into one are both found.
        """
        w, p = self.w, self.p
        n = len(w.x)
        out = np.zeros(n, dtype=bool)
        st = w.structure
        r_q = p.r_cap - 2.0 * p.attach_tol - HIT_TOL_MM
        if not st.active or len(w.seg) == 0 or r_q <= 0:
            return out
        seg_idx = w.seg[:, 5].astype(np.int64)
        t = seg_idx + 1
        # B, one sample step back along the path: it lies on the segment from sample u to u + 1 (u = -1: before
        # the path began, so nothing was removed by the path yet).
        back = w.pos[t] - SAMPLE_MM
        u = np.searchsorted(w.pos, back, side="right") - 1
        m = np.where(u >= 0, w.seg_move_start[np.clip(u, 0, max(n - 2, 0))], 0)
        tau = st.holding_from(m)
        offs = np.concatenate([np.zeros((1, 2)), r_q * _RIM])
        k = len(offs)
        qx = (w.x[t][:, None] + offs[None, :, 0]).ravel()
        qy = (w.y[t][:, None] + offs[None, :, 1]).ravel()
        tq, uq, bq = np.repeat(t, k), np.repeat(u, k), np.repeat(back, k)
        cand = st.cut_off(qx, qy, np.repeat(tau, k)) & _in_poly(qx, qy, w.p_work)
        for poly, _depth in p.prior:
            cand &= ~_in_poly(qx, qy, poly)
        idx = np.nonzero(cand)[0]
        if len(idx) == 0:
            return out
        qx, qy, tq, uq, bq = qx[idx], qy[idx], tq[idx], uq[idx], bq[idx]
        reach = p.r_cap + p.e_max
        index = _SegIndex(w.seg, reach + 1.0)
        removed = index.reach_into(qx, qy, uq, np.full(len(qx), reach)) >= -1e-9
        # The segment through B, up to B, when it is in the material.
        in_mat = np.zeros(max(n, 1), dtype=bool)
        in_mat[seg_idx] = True
        part = np.nonzero((uq >= 0) & in_mat[np.maximum(uq, 0)])[0]
        if len(part):
            uu = uq[part]
            span = w.pos[uu + 1] - w.pos[uu]
            f = np.clip((bq[part] - w.pos[uu]) / np.where(span > 0, span, 1.0), 0.0, 1.0)
            bx = w.x[uu] + f * (w.x[uu + 1] - w.x[uu])
            by = w.y[uu] + f * (w.y[uu + 1] - w.y[uu])
            removed[part] |= _seg_dist(qx[part], qy[part], w.x[uu], w.y[uu], bx, by) <= reach + 1e-9
        out[tq[~removed]] = True
        return out

    def _gates(self) -> list[OracleHit]:
        p, w = self.p, self.w
        g = p.gates
        out = []

        def gate(name: str, detail: str, line: int | None = None) -> None:
            out.append(OracleHit("G-GATE", rule=name, where="program", line=line, detail="%s while the gate %s is "
                                 "off (unlocked by rung %s)" % (detail, name, UNLOCK_RUNG[name])))
        moves = w.program.moves
        if not g.open("first_cut_done"):
            gate("first_cut_done", "a program other than a rung program")
        spindle = [m for m in moves if m.spindle_on]
        if spindle and not g.open("spindle_verified"):
            gate("spindle_verified", "the spindle runs (M10 in the upload)", spindle[0].src.line)
        low = [m for m in moves if m.located and min(float(m.a[2]), float(m.b[2])) < p.z_min_unverified - 1e-9]
        if low and not g.open("z_semantics_verified"):
            gate("z_semantics_verified", "a tool point below Z = %s mm" % p.z_min_unverified, low[0].src.line)
        through = [c for c in w.program.cuts if float(c.max_depth) >= p.T - p.through_tol]
        if through and not g.open("through_cuts_allowed"):
            gate("through_cuts_allowed", "a through cut", through[0].moves[0].src.line)
        pockets = [c for c in w.program.cuts if c.op is not None and c.op.kind == "pocket"]
        if pockets and not g.open("pockets_allowed"):
            gate("pockets_allowed", "a pocket", pockets[0].moves[0].src.line)
        if w.mode == "verify":
            arcs = [m for m in moves if m.is_arc]
            if arcs and not g.open("arcs_emit"):
                gate("arcs_emit", "G02/G03 in the upload", arcs[0].src.line)
            split = [m for m in moves if m.kind == "traverse" and m.c_changes and m.located and not m.moves_xy]
            if split and not p.split_allowed:
                gate("rotate_in_place_ok", "a rotate-in-place line", split[0].src.line)
        return out

    def _dialect(self) -> list[OracleHit]:
        p, w = self.p, self.w
        out = []
        zs = p.z_safety - 1e-9
        raised = False
        cumulative = 0.0
        for m in w.program.moves:
            line = m.src.line
            az, bz = _f(m.a[2]), _f(m.b[2])
            if m.kind == "traverse":
                if (az is not None and az < zs) or (bz is not None and bz < zs):
                    out.append(OracleHit("D-ZSAFE", line=line, detail="a G00 below Z_safety"))
                if m.spindle_on:
                    out.append(OracleHit("D-SPINDLE", line=line, detail="a G00 with the spindle on (no M05 before)"))
            if az is not None and bz is not None and bz < az - 1e-9 and m.kind != "feed":
                out.append(OracleHit("D-ZSAFE", line=line, detail="a descent by a move other than G01"))
            if m.in_material and not m.spindle_on:
                out.append(OracleHit("D-SPINDLE", line=line, detail="motion in the material with the spindle off"))
            if m.c_changes and ((az is not None and az < zs) or (bz is not None and bz < zs)):
                out.append(OracleHit("D-ZSAFE", line=line, detail="a C change below Z_safety"))
            if m.kind == "feed" and _z_only(m) and bz is not None and bz >= zs:
                raised = True
            elif not _z_only(m) and not raised:
                out.append(OracleHit("D-ZSAFE", line=line, detail="X/Y motion before the prologue's Z-only G01 raise"))
                raised = True
            if m.c_end is not None:
                cv = float(m.c_end)
                if cv != int(cv) or int(cv) % int(p.c_grid) or not 0 <= cv <= 350:
                    out.append(OracleHit("D-CSTEP", line=line, detail="C%s off the grid or outside 0..350" % cv))
            if m.c_changes and m.kind != "traverse":
                out.append(OracleHit("D-CSTEP", line=line, detail="a C on a feed line"))
            if m.c_changes and m.c_start is not None:
                d = float(m.c_end) - float(m.c_start)
                short = ((d + 180.0) % 360.0) - 180.0
                if abs(d) >= p.c_step_max and not (p.rotation_model == "in-place-shortest" and abs(short) < 180):
                    out.append(OracleHit("D-CSTEP", line=line, detail="a C step of %s degrees" % d))
                cumulative += short if p.rotation_model == "in-place-shortest" else d
                if abs(cumulative) > p.cumulative_max + 1e-9:
                    out.append(OracleHit("D-CSTEP", line=line, detail="cumulative rotation %s degrees" % cumulative))
        return out


def oracle_check(program: Any, job_toml: Path, machine_toml: Path, params_json: Path,
                 measured: Mapping[str, Any] | None = None, *, gates: Mapping[str, Any] | None = None
                 ) -> OracleVerdict:
    """Check ``program`` (goliath-prep's normalised Program) independently of goliath-prep's checker.

    ``job_toml``, ``machine_toml`` and ``params_json`` are the job file, the
    robot profile and docs/prep/params.json: every parameter comes from them
    (and from ``gates``). ``measured`` holds the raw ``INFO;25`` lines
    (``info25``) and the ``INFO;27`` line (``info27``) of a panel detection
    for a job-time check, None at the desk. ``gates`` is a gate snapshot
    mapping (the ``gates`` object of a snapshot file); None closes every gate.

    Raises :class:`OracleError` for an input it cannot check (never a verdict).
    """
    p = resolve_params(job_toml, machine_toml, params_json, gates)
    notes = ["not evaluated by the oracle: " + "; ".join("%s (%s)" % kv for kv in NOT_EVALUATED.items())]
    if not p.gates.given:
        notes.append("no gate snapshot: all gates closed (the desk view, no robot facts)")
    try:
        world = World(program, p, measured)
    except _NoPanel:
        hits = _p_geom(p, measured)[2] if measured is not None else []
        return OracleVerdict(False, hits, tuple(notes), "job-time", params=p)
    notes.extend(world.notes)
    notes.extend(p.notes)
    notes.extend(_relaxation_notes(p))
    motion = p.gates.value("post_job_motion_known")
    if p.gates.open("post_job_motion_known") and motion != "none":
        notes.append("W-PARK: the fact post_job.motion is %r, a park path the oracle does not model: it checks the "
                     "wheels at the final tool point only (it refuses less, never more)" % (motion,))
    if world.structure.active:
        notes.append("structure: the oracle's %s mm grid (connectivity of the holding material, shrunk by %s mm; "
                     "recomputed at every move's end and every %s mm; declared prior removal narrower than about "
                     "%s mm may not cut it, and T-ATTACH uses the anchor held when the move began): it can only "
                     "find less detached material than the safety model" % (GRID_MM, GRID_SHRINK_MM, CHECKPOINT_MM,
                                                                           2.0 * GRID_SHRINK_MM + GRID_MM))
    run = _Run(world)
    collector = _Collector()
    collector.extend(world.hits)
    collector.extend(run.tool_and_program())
    headings = None
    table: dict[int, dict[int, tuple[str, ...]]] = {}
    if world.mode == "plan":
        hits, headings, table = run.plan()
        collector.extend(hits)
        notes.append("plan: headings searched per cut (forced ones only), links as goliath-prep's template writes "
                     "them with either pivot (or one combined line), the first line from the start pose")
    else:
        collector.extend(run.verify())
    if p.rotation_model is not None:
        notes.append("turns: with the rotation model %r a fact, lines that turn and translate together are tested "
                     "with E_k along one turning direction%s (the oracle refuses less here, never more)"
                     % (p.rotation_model, ", and the first line from the nominal park pose (no heading interval)"
                        if p.heading_map and p.start is not None and p.start[2] is not None else ""))
    violations = collector.hits()
    return OracleVerdict(not violations, violations, tuple(notes), world.mode, headings=headings, table=table, params=p)
