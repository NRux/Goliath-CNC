# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The context builder: facts to parameters, footprint, rotation model and emit profile (unit P22).

Red-team C-03 and C-06: this module is the single place that turns the job
(:class:`~goliath_prep.jobspec.JobSpec`), the machine profile
(:class:`~goliath_prep.machine.MachineProfile`), the gates
(:class:`~goliath_prep.core.types.GateView`) and the panel the robot measured
(:class:`~goliath_prep.core.types.MeasuredGeometry`, None at the desk) into the
:class:`~goliath_prep.core.types.CheckContext` every check reads, and the same
facts into the emitter's :class:`~goliath_prep.core.types.EmitProfile`. No other
module chooses these values, so a check, the planner and the emitter cannot
disagree about them. The independent oracle (unit P21) must not use this
module: it resolves its parameters itself, so a bug here cannot hide in both.

**Every value is a Param with its basis.** :func:`build_context` layers the
catalogue's shipped values (:data:`goliath_prep.defaults.SPEC_PARAMS`), the
profile's values (measured, or owner policy that only tightens) and the job's
values, then adds the values below, each with its source and a reference that
says why it has that value. A gate that is closed or uncertain gives its
closed value with a disclosure; an open gate gives a ``FACT``. A gate that is
open with a value nobody defined (a broken snapshot), facts that contradict
each other, and a measured panel that contradicts the job raise
:class:`ContextError`: nothing is guessed. ``allowed`` counts only when it is
``True``; ``None`` (uncertain) counts as closed, so no unknown is read as
"yes".

The values this module adds (:data:`CONTEXT_PARAMS`, plus ``tower_distance``
at job time and ``start_pose_tol``, ``start_pose_tol_deg`` with a start
pose):

* **Footprint** (U-22, safety-model 4.3 to 4.6). The profile's footprint set:
  the spec footprint, and the union with the R0b measurement once any support
  is measured. ``r_max``, ``r_min``, ``support_centroid_distance`` (d_c),
  ``support_centroid_radius`` (R_c) and ``r_rot`` over every support position
  of the active model (P04's kernel), ``r_body`` for the body hull in use
  (default or measured), and ``safe_zone_inset_desk``. ``rho`` is the
  profile's (r_w the maximum of the three measured contact radii, never the
  mean).
* **Rotation** (U-20, U-21). ``rotation_model`` (:func:`rotation_model_for`:
  ``unverified`` until rung R3 records ``rotation.model``, and every rotation
  step is then checked as the full disc of radius R_rot + rho),
  ``heading_map_verified`` (``verified`` or ``unverified``),
  ``rotate_in_place_ok`` and ``any_heading_safe_zone`` (``applies`` while the
  heading map or the rotation model is not a fact, safety-model 8.1).
* **Edge margins** (red-team C-16, U-50). ``m_edge_left``, ``_right``,
  ``_bottom``, ``_top``: the wheel margin per panel side. The catalogue's
  50 mm ``m_edge`` lapses to 10 mm (:data:`M_EDGE_AFTER_R9`) only on an edge
  the gate ``near_edge_allowed`` lists as tested at rung R9; the tower-baseline
  side (``baseline_side``) keeps ``m_edge_baseline`` until R9 has tested that
  edge itself. Owner policy (``machine.toml [policy]``) only tightens, and
  stays in force after R9. ``m_edge`` itself keeps its value before the rung;
  checks use the per-side values.
* **Towers and panel** (safety-model 5.1, 6.1, 12). ``tower2_placements``: at
  the desk the declared corner (job.toml declares no tower distance); at job
  time the measured panel's corner and the ``INFO;27`` distance
  (``tower_distance``; a measured panel without that distance is refused, as
  P-GEOM refuses it). ``tower_zone_margin``: m_tower, plus geom_tol at the
  desk. ``panel_geometry``: ``desk`` (the declared rectangle shrunk by
  geom_tol) or ``measured``.
* **Reach** (red-team PHYS-05, U-49). ``wire_attachment`` counts the measured
  point only when the profile has one (exported from the facts) **and** the
  gate ``wire_attachment_measured`` is open with the same point; otherwise the
  whole body hull counts, disclosed. ``reach_max`` is the profile's.
* **Values gates carry.** ``max_program_bytes`` (gate ``max_program_bytes``,
  rung R10), ``kerf_width_max``, ``kerf_ride_max``, ``recess_ignore_max``
  (gate ``kerf_crossing``, rung R8k: the relaxation R-KERF, disclosed),
  ``t_hold`` (T/2, INFERRED, U-42; lowered only by the gate ``tabs_hold``'s
  value, R-TABS, disclosed).
* **Start pose** (red-team PHYS-03, U-44). The recorded park pose (gate
  ``start_pose_known``, measured at rung R2). A park pose seen in this session
  (``MeasuredGeometry.park_pose``) is never the start pose on its own:
  position frames count only once their frame mapping is a fact (rung R3,
  ``position.frame_mapping``), and no gate carries that fact (safety-model
  9.5, W-FIRST). It only restricts: it must agree with the recorded pose
  within the tolerance, and alone it is disclosed and not used. Its heading
  is a C word only once the heading map is a fact. No recorded pose:
  ``start_pose`` is ``unknown`` and W-FIRST refuses. ``start_pose_tol`` is a
  disc that holds the measurement's box of +-``tolerance_mm`` on each axis.
* **Emit profile** (U-31; red-team C-03): ``emit_z_line_style``,
  ``emit_rotation_style``, ``emit_first_line`` (:func:`emit_profile_params`).

**The emit profile, a total mapping from facts.** For every combination of
the gates (``heading_map_verified``, ``rotation_model``,
``rotate_in_place_ok``) and the start pose, the first row that applies:

========================================== ==================== ==========================
Facts                                      ``rotation_style``   ``first_line``
========================================== ==================== ==========================
``rotation.model = during-translation``    ``combined``         ``combined``
``rotate_in_place_ok`` open                ``split``            see below
safe zone applies, rotate-in-place unknown ``split``            ``combined``
heading map and model facts, no in-place   ``combined``         ``combined``
========================================== ==================== ==========================

``first_line`` is ``translate-then-rotate`` exactly when the heading map, a
rotation model other than ``during-translation``, rotation in place and the
park heading (a start pose with a C word) are all facts; otherwise
``combined``, checked with the any-rotation envelope. ``z_line_style`` is
``bare`` (spec 05's separate ``Z`` line): no gate carries the fact that would
select ``g00`` (U-31). :func:`split_rotation_allowed` gives G-GATE the same
rule for rotate-in-place lines.

**Geometry.** A CheckContext holds Params, not shapes; :func:`context_geometry`
builds the panel, the work areas, the tower zones and bases and the reach
region from a context's Params alone, with P04's kernel.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from types import MappingProxyType
from typing import Any

import shapely.wkt
from shapely.errors import ShapelyError
from shapely.geometry import MultiPolygon, Polygon

from goliath_prep.core.canon import quantize
from goliath_prep.core.disclosure import Disclosure
from goliath_prep.core.params import Param
from goliath_prep.core.types import (
    MEASURED_SUPPORT_PARAMS,
    ROTATION_MODELS,
    VENDOR_SUPPORT_PARAMS,
    CheckContext,
    EmitProfile,
    FootprintSet,
    FrozenMapping,
    GateView,
    MeasuredGeometry,
    Pose,
    RotationModel,
)
from goliath_prep.defaults import SPEC_PARAMS, catalogue_ref
from goliath_prep.geom import (
    SIDES,
    Vec2,
    baseline_side,
    body_radius,
    desk_panel,
    edge_margins,
    footprint_radii,
    job_panel,
    reach_region,
    tool_zones,
    tower2_placements,
    tower_bases,
    tower_points,
    wheel_zones,
    work_area_tool,
    work_area_wheel,
)
from goliath_prep.jobspec import JobSpec
from goliath_prep.machine import MachineProfile

__all__ = [
    "CONTEXT_PARAMS",
    "EDGE_SIDES",
    "M_EDGE_AFTER_R9",
    "REPLACED_PROFILE_PARAMS",
    "ContextError",
    "ContextGeometry",
    "build_context",
    "context_disclosures",
    "context_geometry",
    "emit_profile_for",
    "emit_profile_params",
    "rotation_model_for",
    "safe_zone_applies",
    "split_rotation_allowed",
]

EDGE_SIDES: Mapping[str, str] = MappingProxyType(
    {"x_min": "left", "x_max": "right", "y_min": "bottom", "y_max": "top"})
"""The panel edges as goliath-run's gate ``near_edge_allowed`` names them (fact ``edges.near_verified``), and the
panel side each is (goliath_prep.geom.SIDES: left x = 0, right x = W, bottom y = 0, top y = H)."""

M_EDGE_AFTER_R9 = 10.0
"""The wheel edge margin on an edge rung R9 has tested, mm (docs/prep/params.json ``m_edge`` ``after_unlock``)."""

CONTEXT_PARAMS: tuple[str, ...] = (
    "any_heading_safe_zone", "baseline_side", "emit_first_line", "emit_rotation_style", "emit_z_line_style",
    "heading_map_verified", "m_edge_bottom", "m_edge_left", "m_edge_right", "m_edge_top", "panel_geometry", "r_body",
    "r_max", "r_min", "r_rot", "rotate_in_place_ok", "rotation_model", "safe_zone_inset_desk", "start_pose",
    "support_centroid_distance", "support_centroid_radius", "t_hold", "tower2_placements", "tower_zone_margin",
)
"""The Params :func:`build_context` adds to every context (see the module documentation)."""

REPLACED_PROFILE_PARAMS: tuple[str, ...] = ("wire_attachment", "wire_attachment_back", "wire_attachment_right")
"""Profile Params the gates can override: a measured wire attachment counts only with the gate
``wire_attachment_measured`` open (otherwise the body hull, disclosed, and the point's offsets are left out)."""

_GATED_CATALOGUE = frozenset({"max_program_bytes", "kerf_width_max", "kerf_ride_max", "recess_ignore_max"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PARK_MEASURED_KEYS = frozenset({"x_mm", "y_mm", "heading_deg", "tolerance_mm", "tolerance_deg"})
_PARK_POSE_KEYS = frozenset({"x", "y", "c", "source"})
_KERF_KEYS = (("w_b", "kerf_width_max"), ("L_ride", "kerf_ride_max"), ("h_r", "recess_ignore_max"))
_SIDE_EDGE: Mapping[str, str] = MappingProxyType({side: edge for edge, side in EDGE_SIDES.items()})
_SIDE_WHERE: Mapping[str, str] = MappingProxyType(
    {"left": "x = 0", "right": "x = W", "bottom": "y = 0", "top": "y = H"})

if tuple(sorted(EDGE_SIDES.values())) != tuple(sorted(SIDES)):  # pragma: no cover - a guard on this module's table
    raise RuntimeError("EDGE_SIDES must name each panel side once")


class ContextError(ValueError):
    """The inputs cannot make a context: a gate open with a value nobody defined, facts that contradict each other,
    a measured panel that contradicts the job, or a value that cannot be recorded exactly."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _require_view(gates: object) -> None:
    if not isinstance(gates, GateView):
        raise TypeError("gates must provide the GateView protocol (allowed, value, snapshot_sha256), got %r"
                        % (gates,))


def _snapshot(gates: GateView) -> str:
    """The first 12 hex digits of the gate snapshot hash, for references (the hash is checked)."""
    sha = gates.snapshot_sha256()
    if not isinstance(sha, str) or not _SHA256.match(sha):
        raise ContextError("the gate view's snapshot_sha256() is %r, not 64 lowercase hex digits" % (sha,))
    return sha[:12]


def _state(gates: GateView, name: str) -> tuple[bool, str]:
    """Whether gate ``name`` is open (``allowed`` is True, nothing else), and its state in words."""
    raw = gates.allowed(name)
    if raw is True:
        return True, "open"
    if raw is False:
        return False, "closed"
    if raw is None:
        return False, "uncertain (treated as closed)"
    return False, "allowed = %r, not True (treated as closed)" % (raw,)


def _show(value: float) -> str:
    return format(Decimal(repr(float(value))).normalize(), "f")


def _grid_sum(*values: float) -> float:
    """The exact sum of values on the 0.001 grid (no float noise in a report)."""
    total = Decimal(0)
    for value in values:
        total += Decimal(repr(float(value)))
    return float(total) + 0.0


def _fact_number(value: object, what: str, *, minimum: float | None = None, above: float | None = None) -> float:
    """A number an open gate carries: finite, within the bounds, on the 0.001 grid a record keeps."""
    if isinstance(value, bool) or type(value) not in (int, float):
        raise ContextError("%s must be a number, got %r" % (what, value))
    try:
        number = float(value)  # type: ignore[arg-type]
        on_grid = math.isfinite(number) and abs(number - quantize(number)) <= max(1e-9, 8 * math.ulp(number))
    except (OverflowError, ValueError):
        on_grid = False
        number = math.inf
    if not math.isfinite(number):
        raise ContextError("%s must be a finite number below 10^12, got %r" % (what, value))
    if minimum is not None and number < minimum:
        raise ContextError("%s must be at least %s, got %r" % (what, _show(minimum), value))
    if above is not None and not number > above:
        raise ContextError("%s must be greater than %s, got %r" % (what, _show(above), value))
    if not on_grid:
        raise ContextError("%s = %r is not on the 0.001 grid a record keeps, or too large for one; the gate "
                           "snapshot is malformed" % (what, value))
    return number


def _add(params: dict[str, Param[Any]], new: Mapping[str, Param[Any]]) -> None:
    """Add Params that are not there yet (a name given twice is a programming error, never a silent overwrite)."""
    for name, param in new.items():
        if name in params:  # pragma: no cover - a guard on this module's own names
            raise RuntimeError("context parameter %s given twice" % name)
        params[name] = param


def _replace(params: dict[str, Param[Any]], new: Mapping[str, Param[Any]], drop: Iterable[str] = ()) -> None:
    """Replace the Params a gate overrides (only names this module declares replaceable)."""
    for name in tuple(new) + tuple(drop):
        if name not in REPLACED_PROFILE_PARAMS and name not in _GATED_CATALOGUE:
            raise RuntimeError("context parameter %s is not replaceable" % name)  # pragma: no cover
    for name in drop:
        params.pop(name, None)
    params.update(new)


# ---------------------------------------------------------------------------
# Rotation model and emit profile (U-20, U-21, U-31; red-team C-03)
# ---------------------------------------------------------------------------


def rotation_model_for(gates: GateView) -> RotationModel:
    """The rotation model the checks use: the fact ``rotation.model`` while the gate ``rotation_model`` is open,
    ``unverified`` otherwise (every rotation step is then checked as the full disc of radius R_rot + rho).

    An open gate whose value is not a known model other than ``unverified``
    raises :class:`ContextError` (a broken snapshot is never read as a model).
    A value a closed gate may carry is never used.
    """
    _require_view(gates)
    if gates.allowed("rotation_model") is not True:
        return "unverified"
    value = gates.value("rotation_model")
    if type(value) is not str or value not in ROTATION_MODELS or value == "unverified":
        raise ContextError(
            "the gate rotation_model is open but records %r, not one of %s: an open gate carries the fact "
            "rotation.model of rung R3, so the gate snapshot is malformed; export the gates again"
            % (value, ", ".join(m for m in ROTATION_MODELS if m != "unverified")))
    return value  # type: ignore[return-value]


def safe_zone_applies(gates: GateView) -> bool:
    """Whether the any-heading safe zone applies: while the heading map or the rotation model is not a fact
    (safety-model 8.1)."""
    _require_view(gates)
    return not (gates.allowed("heading_map_verified") is True and rotation_model_for(gates) != "unverified")


def split_rotation_allowed(gates: GateView) -> bool:
    """Whether rotate-in-place (``split``) link lines are allowed under ``gates``: never under the fact
    ``rotation.model = during-translation``; otherwise when ``rotate_in_place_ok`` is a fact, or while the
    any-heading safe zone applies (it checks every rotation step as the full disc). G-GATE refuses split lines
    whenever this is False (red-team C-03)."""
    if rotation_model_for(gates) == "during-translation":
        return False
    return gates.allowed("rotate_in_place_ok") is True or safe_zone_applies(gates)


_COMBINED_NOTE = Disclosure(
    id="EMIT:combined", severity="info",
    text="EMIT PROFILE combined: links turn and translate on one G00 X Y C line, the vendor's own form, checked with "
         "the envelopes of safety-model 9.4 (both turning directions until the direction is a fact)",
    source="goliath_prep.context emit profile (red-team C-03)")
_SPLIT_UNVERIFIED_NOTE = Disclosure(
    id="EMIT:split-unverified", severity="info",
    text="EMIT PROFILE split under the any-heading safe zone: links rotate in place (G00 at an unchanged X Y), then "
         "translate, although rotation in place is not yet a fact (U-21, rung R3); every rotation step is checked "
         "as the full disc of radius R_rot + rho about its pivot",
    source="goliath_prep.context emit profile (red-team C-03)")
_Z_LINE_NOTE = Disclosure(
    id="EMIT:z-line-bare", severity="info",
    text="EMIT PROFILE z line bare: the rapid Z line after a G00 is written without a G word, as spec 05 reads the "
         "vendor's programs; the vendor's exact byte layout is unverified (U-31) and no gate selects the g00 form",
    source="spec 05 §Semantics a replacement must match")


def _emit_choices(gates: GateView, start_pose: Pose | None) -> dict[str, Param[Any]]:
    """The three emit choices as Params, from the gates and the start pose (the module documentation's table)."""
    snap = _snapshot(gates)
    model = rotation_model_for(gates)
    heading, heading_text = _state(gates, "heading_map_verified")
    rip, rip_text = _state(gates, "rotate_in_place_ok")
    zone = not (heading and model != "unverified")
    z_line = Param("bare", "enum", "SPEC",
                   "spec 05 §Semantics a replacement must match ('Rapids are split': the M05 line, the G00 line, then "
                   "a separate Z line at the safety height): the rapid Z line has no G word; no gate selects g00",
                   disclosures=(_Z_LINE_NOTE,))
    if model == "during-translation":
        notes = [_COMBINED_NOTE]
        if rip:
            notes.append(Disclosure(
                id="FACTS_DISAGREE:rotation", severity="amber",
                text="the facts disagree: rotation.model is during-translation, but the gate rotate_in_place_ok is "
                     "open. Links use the combined line, which the envelopes of safety-model 9.4 check under either "
                     "reading, never rotate-in-place lines; review the rung R3 record",
                source="gates rotation_model and rotate_in_place_ok (snapshot %s)" % snap))
        style = Param("combined", "enum", "FACT",
                      "fact rotation.model = during-translation (gate rotation_model open, snapshot %s): the robot "
                      "turns while it translates, so links turn and translate on one G00 X Y C line, the vendor's "
                      "form (the project design, section 4.10.1)" % snap, disclosures=tuple(notes))
    elif rip:
        style = Param("split", "enum", "FACT",
                      "fact rotation.in_place (gate rotate_in_place_ok open, snapshot %s): a G00 at an unchanged X Y "
                      "turns the robot in place, so links rotate in place, then translate (safety-model 9.1)" % snap)
    elif zone:
        style = Param("split", "enum", "GATE",
                      "the any-heading safe zone applies (heading_map_verified %s, rotation model %s; snapshot %s): "
                      "links rotate in place, then translate, although rotation in place is unverified "
                      "(rotate_in_place_ok %s, U-21); the safe zone checks every rotation step as the full disc of "
                      "radius R_rot + rho (safety-model 8 and 9.3)" % (heading_text, model, snap, rip_text),
                      disclosures=(_SPLIT_UNVERIFIED_NOTE,))
    else:
        style = Param("combined", "enum", "GATE",
                      "the heading map and the rotation model (%s) are facts, so the safe zone no longer covers "
                      "rotate-in-place lines, and rotate_in_place_ok is %s (snapshot %s): links use the combined "
                      "G00 X Y C line, the vendor's form; split lines would be refused (G-GATE, red-team C-03)"
                      % (model, rip_text, snap), disclosures=(_COMBINED_NOTE,))

    missing = []
    if not heading:
        missing.append("the heading map (heading_map_verified %s)" % heading_text)
    if model == "unverified":
        missing.append("the rotation model (rotation_model closed)")
    elif model == "during-translation":
        missing.append("rotation in place (rotation.model is during-translation)")
    if not rip:
        missing.append("rotation in place (rotate_in_place_ok %s)" % rip_text)
    if start_pose is None:
        missing.append("the start pose (no park pose recorded: gate start_pose_known)")
    elif start_pose.c is None:
        missing.append("the park heading (the start pose has no C word: %s)" % start_pose.source)
    if not missing:
        assert start_pose is not None and start_pose.c is not None
        first = Param("translate-then-rotate", "enum", "FACT",
                      "the heading map, the rotation model %s, rotation in place and the park heading C%d are facts "
                      "(start pose: %s; snapshot %s): the first line may translate at the parked heading, then "
                      "rotate in place at the first cut (safety-model 9.5)" % (model, start_pose.c, start_pose.source,
                                                                                snap))
    else:
        gated = not heading or model == "unverified" or not rip or start_pose is None
        first = Param("combined", "enum", "GATE" if gated else "FACT",
                      "the first line is one combined G00 X Y C line, checked with the any-rotation envelope until "
                      "every fact it needs is in (safety-model 9.5); not a fact yet: %s (snapshot %s)"
                      % ("; ".join(missing), snap))
    return {"emit_z_line_style": z_line, "emit_rotation_style": style, "emit_first_line": first}


def emit_profile_params(gates: GateView, ctx: CheckContext) -> Mapping[str, Param[Any]]:
    """The emit choices as Params with their basis: ``emit_z_line_style``, ``emit_rotation_style``,
    ``emit_first_line``.

    ``gates`` must be the view ``ctx`` was built under (the same snapshot
    hash, rotation model and heading map); otherwise :class:`ContextError`. A
    context from :func:`build_context` already holds these Params; they must
    be the same.
    """
    _require_view(gates)
    if not isinstance(ctx, CheckContext):
        raise TypeError("ctx must be a CheckContext, got %r" % (ctx,))
    built, given = ctx.gates.snapshot_sha256(), gates.snapshot_sha256()
    if built != given:
        raise ContextError("the context was built under gate snapshot %s, but this gate view is gate snapshot %s: "
                           "build the context again under the gates the program is emitted for" % (built, given))
    model = rotation_model_for(gates)
    heading = gates.allowed("heading_map_verified") is True
    if model != ctx.rotation_model or heading != ctx.heading_map_verified:
        raise ContextError("the gate view (rotation model %s, heading map %s) is not the one the context records "
                           "(%s, %s), although its gate snapshot hash is the same"
                           % (model, heading, ctx.rotation_model, ctx.heading_map_verified))
    choices = _emit_choices(gates, ctx.start_pose)
    for name, param in choices.items():
        recorded = ctx.params.get(name)
        if recorded is not None and recorded != param:
            raise ContextError("the context records %s = %r (%s), but its gates give %r (%s)"
                               % (name, recorded.value, recorded.ref, param.value, param.ref))
    return FrozenMapping(choices)


def emit_profile_for(gates: GateView, ctx: CheckContext) -> EmitProfile:
    """The emitter's profile for ``ctx`` under ``gates`` (the module documentation's table; red-team C-03).

    The basis of each choice is in :func:`emit_profile_params` and in the
    context's Params of the same names.
    """
    params = emit_profile_params(gates, ctx)
    return EmitProfile(z_line_style=params["emit_z_line_style"].value,
                       rotation_style=params["emit_rotation_style"].value,
                       first_line=params["emit_first_line"].value)


# ---------------------------------------------------------------------------
# The context
# ---------------------------------------------------------------------------


def build_context(job: JobSpec, profile: MachineProfile, gates: GateView,
                  measured: MeasuredGeometry | None = None) -> CheckContext:
    """The CheckContext for ``job`` on the robot of ``profile`` under ``gates`` (see the module documentation).

    ``measured`` is the panel the robot measured in this session (job time),
    or None at the desk. Raises ``TypeError`` for inputs of the wrong type and
    :class:`ContextError` for inputs that cannot make a context.
    """
    if not isinstance(job, JobSpec):
        raise TypeError("job must be a JobSpec (job.toml, read), got %r" % (job,))
    if not isinstance(profile, MachineProfile):
        raise TypeError("profile must be a MachineProfile (machine.toml, read), got %r" % (profile,))
    _require_view(gates)
    if measured is not None and not isinstance(measured, MeasuredGeometry):
        raise TypeError("measured must be a MeasuredGeometry or None (at the desk), got %r" % (measured,))
    _snapshot(gates)
    axis = job.panel.tower2.value
    if measured is not None and measured.tower2 != axis:
        raise ContextError(
            "the robot measured the panel with tower 2 on the %s axis, but job.toml says tower 2 stands on the %s "
            "axis (panel.tower2): place tower 2 as the job says, or correct the job" % (measured.tower2, axis))

    params = _base_params(job, profile)
    footprint = profile.footprint_models()
    _add(params, _footprint_params(footprint, params, profile))
    r_rot, rho = params["r_rot"], params["rho"]
    _add(params, _rotation_params(gates, r_rot, rho))
    _add(params, _margin_params(params, axis, gates))
    _add(params, _tower_params(params, axis, measured))
    _add(params, _panel_param(params, measured))
    _add(params, {"safe_zone_inset_desk": _safe_zone_inset(params)})
    wire, drop = _wire_params(params, profile, gates)
    _replace(params, wire, drop)
    _replace(params, _gate_value_params(params, gates))
    _add(params, {"t_hold": _t_hold(params, gates)})
    pose, pose_params = _start_pose(params, gates, measured)
    _add(params, pose_params)
    _add(params, _emit_choices(gates, pose))

    try:
        ctx = CheckContext(
            params=params, footprint=footprint, rho=rho.value, r_rot=r_rot.value, gates=gates,
            rotation_model=rotation_model_for(gates),
            heading_map_verified=gates.allowed("heading_map_verified") is True,
            start_pose=pose, measured=measured, obstacles=job.obstacles, fixations=job.fixations,
            prior_removal=job.prior_removal)
    except (TypeError, ValueError) as exc:
        raise ContextError("the context cannot be built: %s" % exc) from exc
    _check_geometry(ctx)
    try:
        ctx.to_json()
    except ValueError as exc:
        raise ContextError("the context cannot be recorded exactly (a certificate binds it): %s" % exc) from exc
    return ctx


def _base_params(job: JobSpec, profile: MachineProfile) -> dict[str, Param[Any]]:
    """The catalogue's values, then the profile's in their place (measured, or owner policy), then the job's."""
    params: dict[str, Param[Any]] = dict(SPEC_PARAMS)
    params.update(profile.params)
    job_params = job.params()
    clash = sorted(set(job_params) & set(profile.params))
    if clash:
        raise ContextError("machine.toml and job.toml both set %s: a value has one source" % ", ".join(clash))
    params.update(job_params)
    return params


def _footprint_params(fp: FootprintSet, params: Mapping[str, Param[Any]],
                      profile: MachineProfile) -> dict[str, Param[Any]]:
    inputs = [params[name] for pair in VENDOR_SUPPORT_PARAMS for name in pair]
    for k, position in enumerate(fp.measured or ()):
        if position is not None:
            try:
                inputs.extend(params[name] for name in MEASURED_SUPPORT_PARAMS[k])
            except KeyError as exc:
                raise ContextError("the footprint has a measured position for q%d but machine.toml gives no %s"
                                   % (k + 1, exc.args[0])) from None
    model = ("the union of the spec footprint and the one measured at rung R0b" if fp.active == "union"
             else "the spec footprint (nothing measured yet: rung R0b)")
    radii = footprint_radii(fp)
    r_rot = Param.derived(radii.r_rot, "mm", "R_rot = ceil(max(R_max, d_c + R_c)) in whole millimetres over every "
                          "support position of %s (safety-model 4.6; red-team PHYS-07)" % model, inputs)
    out: dict[str, Param[Any]] = {
        "r_max": Param.derived(radii.r_max, "mm", "R_max = max_k r_k over %s (safety-model 4.6)" % model, inputs),
        "r_min": Param.derived(radii.r_min, "mm", "r_min = min_k r_k over %s (safety-model 4.6)" % model, inputs),
        "support_centroid_distance": Param.derived(
            quantize(radii.d_c, "ceiling"), "mm",
            "d_c = |c|, the tool point to the support centroid that gives R_rot, over %s, rounded up to 0.001 mm "
            "(safety-model 4.6)" % model, inputs),
        "support_centroid_radius": Param.derived(
            quantize(radii.r_c, "ceiling"), "mm",
            "R_c = max |d_k - c| from that centroid, over %s, rounded up to 0.001 mm (safety-model 4.6)" % model,
            inputs),
        "r_rot": r_rot,
    }
    rho, hull = params["rho"], params["body_hull"]
    try:
        if hull.value == "default":
            margin = params["body_hull_margin"]
            out["r_body"] = Param.derived(
                quantize(body_radius(fp, rho.value, hull_margin=margin.value), "ceiling"), "mm",
                "R_body = R_rot + rho + m_body with the default body hull (safety-model 4.5)",
                (r_rot, rho, hull, margin))
        else:
            if profile.body_outline is None:
                raise ContextError("machine.toml's body hull is %r, but the profile holds no outline" % (hull.value,))
            out["r_body"] = Param.derived(
                body_radius(fp, rho.value, outline=profile.body_outline), "mm",
                "R_body: the farthest point of the %s body hull during any turn about a pivot between the tool point "
                "and a support centroid, rounded up to whole millimetres (safety-model 4.5)" % hull.value,
                (r_rot, rho, hull))
    except ValueError as exc:
        if isinstance(exc, ContextError):
            raise
        raise ContextError("the body hull cannot be built: %s" % exc) from exc
    return out


def _rotation_params(gates: GateView, r_rot: Param[Any], rho: Param[Any]) -> dict[str, Param[Any]]:
    snap = _snapshot(gates)
    model = rotation_model_for(gates)
    _, model_state = _state(gates, "rotation_model")
    disc = _show(_grid_sum(r_rot.value, rho.value))
    if model == "unverified":
        p_model = Param("unverified", "enum", "GATE",
                        "gate rotation_model %s (snapshot %s; unlocked by rung R3, U-21): every rotation step is "
                        "checked as the full disc of radius R_rot + rho = %s mm (safety-model 9.3)"
                        % (model_state, snap, disc),
                        disclosures=(Disclosure(
                            id="GATE:rotation_model", severity="info",
                            text="ROTATION MODEL UNVERIFIED (U-21): every rotation step is checked as the full disc "
                                 "of radius R_rot + rho = %s mm about its pivot, in both directions, and a line that "
                                 "turns and moves together with the any-rotation envelope, until rung R3 records "
                                 "rotation.model" % disc,
                            source="gate rotation_model (snapshot %s)" % snap),))
    else:
        p_model = Param(model, "enum", "FACT",
                        "fact rotation.model = %s (gate rotation_model open, snapshot %s; rung R3)" % (model, snap))
    heading, heading_state = _state(gates, "heading_map_verified")
    if heading:
        p_heading = Param("verified", "enum", "FACT",
                          "fact heading map verified (gate heading_map_verified open, snapshot %s; rungs R0b, R3)"
                          % snap)
    else:
        p_heading = Param("unverified", "enum", "GATE",
                          "gate heading_map_verified %s (snapshot %s; unlocked by rung R3, U-20)"
                          % (heading_state, snap),
                          disclosures=(Disclosure(
                              id="GATE:heading_map_verified", severity="info",
                              text="HEADING MAP UNVERIFIED (U-20): what C means on the robot is not a fact yet, so "
                                   "the any-heading safe zone applies to every tool point (safety-model 8) until "
                                   "rung R3 verifies the heading map",
                              source="gate heading_map_verified (snapshot %s)" % snap),))
    rip, rip_state = _state(gates, "rotate_in_place_ok")
    if rip:
        p_rip = Param("verified", "enum", "FACT",
                      "fact rotation.in_place (gate rotate_in_place_ok open, snapshot %s; rung R3)" % snap)
    else:
        p_rip = Param("unverified", "enum", "GATE",
                      "gate rotate_in_place_ok %s (snapshot %s; unlocked by rung R3, U-21)" % (rip_state, snap),
                      disclosures=(Disclosure(
                          id="GATE:rotate_in_place_ok", severity="info",
                          text="ROTATION IN PLACE UNVERIFIED (U-21): a G00 at an unchanged X Y is not known to turn "
                               "the robot in place; rotate-in-place link lines are allowed only while the any-heading "
                               "safe zone applies, and rung R3 records the fact",
                          source="gate rotate_in_place_ok (snapshot %s)" % snap),))
    zone = not (heading and model != "unverified")
    p_zone = Param.derived(
        "applies" if zone else "not-required", "enum",
        "the any-heading safe zone applies while the heading map or the rotation model is not a fact (safety-model "
        "8.1): %s" % ("it applies" if zone else "both are facts, so it is not required"), (p_heading, p_model))
    return {"rotation_model": p_model, "heading_map_verified": p_heading, "rotate_in_place_ok": p_rip,
            "any_heading_safe_zone": p_zone}


def _margin_params(params: Mapping[str, Param[Any]], axis: str, gates: GateView) -> dict[str, Param[Any]]:
    """The wheel edge margin per side (red-team C-16; U-50): see the module documentation."""
    snap = _snapshot(gates)
    is_open, _ = _state(gates, "near_edge_allowed")
    tested: frozenset[str] = frozenset()
    if is_open:
        value = gates.value("near_edge_allowed")
        if (not isinstance(value, (tuple, list)) or not value or len(set(value)) != len(value)
                or not all(type(v) is str and v in EDGE_SIDES for v in value)):
            raise ContextError(
                "the gate near_edge_allowed is open but carries %r: it must list the edges rung R9 tested, each once, "
                "from %s (fact edges.near_verified); the gate snapshot is malformed" % (value, ", ".join(EDGE_SIDES)))
        tested = frozenset(value)
    base = baseline_side(axis)  # type: ignore[arg-type]
    out: dict[str, Param[Any]] = {"baseline_side": Param.derived(
        base, "enum", "the tower baseline, from tower 1 to tower 2, runs along the %s side (%s) with tower 2 on the %s "
        "axis (safety-model 6.2)" % (base, _SIDE_WHERE[base], axis), (params["tower2_axis"],))}
    m_edge, m_base = params["m_edge"], params["m_edge_baseline"]
    for side in SIDES:
        edge = _SIDE_EDGE[side]
        done = edge in tested
        inputs: list[Param[Any]] = []
        if done and m_edge.source == "GATE":
            inputs.append(Param(M_EDGE_AFTER_R9, "mm", "FACT",
                                "gate near_edge_allowed (snapshot %s): rung R9 tested the %s edge (%s), so its wheel "
                                "margin is %s mm (%s after_unlock)"
                                % (snap, edge, _SIDE_WHERE[side], _show(M_EDGE_AFTER_R9), catalogue_ref("m_edge"))))
        else:
            inputs.append(m_edge)
        if side == base and not (done and m_base.source == "GATE"):
            inputs.append(m_base)
        value = max(float(p.value) for p in inputs)
        why = ("rung R9 tested this edge" if done else "rung R9 has not tested this edge")
        if side == base:
            why += "; it is the tower baseline, which keeps m_edge_baseline until R9 has tested it (U-50)"
        out["m_edge_%s" % side] = Param.derived(
            value, "mm", "the wheel edge margin on the %s side (%s, edge %s): the largest of %s; %s (safety-model 6.2; "
            "red-team C-16)" % (side, _SIDE_WHERE[side], edge, ", ".join(p.ref for p in inputs), why), inputs)
    return out


def _measured_polygon(measured: MeasuredGeometry) -> Polygon:
    try:
        shape = shapely.wkt.loads(measured.polygon_wkt)
    except (ShapelyError, ValueError) as exc:
        raise ContextError("the measured panel (MeasuredGeometry %s) is not a readable WKT polygon: %s"
                           % (measured.sha256[:12], exc)) from exc
    if not isinstance(shape, Polygon) or shape.is_empty or not shape.is_valid or shape.area <= 0.0:
        raise ContextError("the measured panel (MeasuredGeometry %s) is not one valid polygon with an area"
                           % measured.sha256[:12])
    return shape


def _tower_params(params: Mapping[str, Param[Any]], axis: str,
                  measured: MeasuredGeometry | None) -> dict[str, Param[Any]]:
    m_tower, geom_tol = params["m_tower"], params["geom_tol"]
    width, height = params["panel_width"], params["panel_height"]
    along = "W" if axis == "X" else "H"
    if measured is None:
        corner = width if axis == "X" else height
        return {
            "tower_zone_margin": Param.derived(
                _grid_sum(m_tower.value, geom_tol.value), "mm",
                "the tower zones at the desk are grown by m_tower + geom_tol (safety-model 5.1 and 12)",
                (m_tower, geom_tol)),
            "tower2_placements": Param.derived(
                tower2_placements(width.value, height.value, axis, None), "mm",  # type: ignore[arg-type]
                "tower 2 along the %s axis at the declared panel corner (%s = %s mm); job.toml declares no tower "
                "distance, so at the desk the corner is the one placement (safety-model 5.1)"
                % (axis, along, _show(corner.value)), (corner, params["tower2_axis"])),
        }
    shape = _measured_polygon(measured)
    extent = quantize(shape.bounds[2] if axis == "X" else shape.bounds[3])
    p_corner = Param(extent, "mm", "ROBOT_MEASURED",
                     "the measured panel's extent along the %s axis, to 0.001 mm (MeasuredGeometry %s, hypothesis %s)"
                     % (axis, measured.sha256[:12], measured.hypothesis))
    if measured.tower_distance is None:
        # Safety-model 5.1: tower 2 stands at the corner AND at the INFO;27 distance, and every tower check holds
        # at both; P-GEOM needs INFO;27 (safety-model 12, step 3). The corner alone would be a silent guess.
        raise ContextError(
            "the panel the robot measured (MeasuredGeometry %s) has no tower distance: %s, so the second tower-2 "
            "placement is unknown and the tower checks cannot hold at both (safety-model 5.1); P-GEOM refuses such "
            "a detection (safety-model 12, step 3). Power-cycle the robot and run panel detection again with "
            "goliath-run connected"
            % (measured.sha256[:12], "no INFO;27 line arrived" if measured.raw_info27 is None else
               "no distance was read from the line %s" % measured.raw_info27))
    p_distance = Param(measured.tower_distance, "mm", "ROBOT_MEASURED",
                       "the tower distance of the line %s (MeasuredGeometry %s)"
                       % (measured.raw_info27, measured.sha256[:12]))
    inputs: list[Param[Any]] = [p_corner, params["tower2_axis"], p_distance]
    out: dict[str, Param[Any]] = {"tower_distance": p_distance}
    distance = p_distance.value
    w_arg, h_arg = (extent, height.value) if axis == "X" else (width.value, extent)
    try:
        placements = tower2_placements(w_arg, h_arg, axis, distance)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ContextError("tower 2 cannot be placed from the measured panel: %s" % exc) from exc
    out["tower2_placements"] = Param.derived(
        placements, "mm", "tower 2 along the %s axis at the measured panel's corner and at the INFO;27 distance%s "
        "(safety-model 5.1: the vendor located tower 2 both ways, so every tower check holds at both)"
        % (axis, " (the same place)" if len(placements) == 1 else ""), inputs)
    out["tower_zone_margin"] = Param.derived(
        m_tower.value, "mm", "the tower zones at job time are grown by m_tower: the measured geometry takes the place "
        "of geom_tol (safety-model 5.1 and 12)", (m_tower,))
    return out


def _panel_param(params: Mapping[str, Param[Any]], measured: MeasuredGeometry | None) -> dict[str, Param[Any]]:
    if measured is None:
        return {"panel_geometry": Param.derived(
            "desk", "enum", "P_work at the desk: the declared rectangle W x H shrunk by geom_tol on every side "
            "(safety-model 6.1)", (params["panel_width"], params["panel_height"], params["geom_tol"]))}
    return {"panel_geometry": Param(
        "measured", "enum", "ROBOT_MEASURED",
        "P_work at job time: the measured panel (MeasuredGeometry %s: %d INFO;25 line(s), hypothesis %s) clipped to "
        "SET PANEL SIZE %d x %d (safety-model 6.1 and 12)"
        % (measured.sha256, len(measured.raw_info25), measured.hypothesis, measured.set_panel_size[0],
           measured.set_panel_size[1]))}


def _safe_zone_inset(params: Mapping[str, Param[Any]]) -> Param[float]:
    sides = [params["m_edge_%s" % side] for side in SIDES]
    widest = max(sides, key=lambda p: float(p.value))
    inputs = (params["r_rot"], params["rho"], widest, params["geom_tol"])
    return Param.derived(
        _grid_sum(*(p.value for p in inputs)), "mm",
        "R_rot + rho + m_edge + geom_tol, with the largest edge margin in use (%s): how far the any-heading zone lies "
        "inside each side of a rectangular panel at the desk (safety-model 8.3)" % widest.ref, inputs)


def _wire_point(value: object) -> Vec2:
    """The attachment point an open gate ``wire_attachment_measured`` carries, in the body frame (back, right)."""
    if isinstance(value, Mapping) and set(value) == {"kind"} and value["kind"] == "on_spindle_axis":
        return (0.0, 0.0)
    if isinstance(value, Mapping) and set(value) == {"kind", "back_mm", "right_mm"} and value["kind"] == "offset":
        return (_fact_number(value["back_mm"], "the gate wire_attachment_measured back_mm"),
                _fact_number(value["right_mm"], "the gate wire_attachment_measured right_mm"))
    raise ContextError("the gate wire_attachment_measured is open but carries %r, not {kind: on_spindle_axis} or "
                       "{kind: offset, back_mm, right_mm}: the gate snapshot is malformed" % (value,))


def _wire_params(params: Mapping[str, Param[Any]], profile: MachineProfile,
                 gates: GateView) -> tuple[dict[str, Param[Any]], tuple[str, ...]]:
    """The wire attachment P-REACH counts (red-team PHYS-05; U-49): replacements and dropped names."""
    snap = _snapshot(gates)
    is_open, state = _state(gates, "wire_attachment_measured")
    point = profile.wire_attachment
    current = params["wire_attachment"]
    if is_open:
        fact = _wire_point(gates.value("wire_attachment_measured"))
        if point is None:
            note = Disclosure(
                id="WIRE:profile-not-exported", severity="amber",
                text="the gate wire_attachment_measured is open (rung R0b measured the wire attachment), but "
                     "machine.toml holds no measured attachment point: export the machine profile again from the "
                     "facts; until then P-REACH counts the whole body hull",
                source="gate wire_attachment_measured (snapshot %s), machine.toml" % snap)
            return {"wire_attachment": Param(current.value, current.unit, current.source, current.ref,
                                             disclosures=current.disclosures + (note,))}, ()
        if fact != point:
            raise ContextError(
                "the wire attachment in machine.toml (%s mm back, %s mm right of T) is not the one the facts record "
                "(%s mm back, %s mm right; gate wire_attachment_measured, snapshot %s): the profile is out of date, "
                "export it again from the facts" % (_show(point[0]), _show(point[1]), _show(fact[0]), _show(fact[1]),
                                                    snap))
        return {}, ()
    if point is None:
        return {}, ()
    default = SPEC_PARAMS["wire_attachment"]
    note = Disclosure(
        id="WIRE:gate-closed", severity="amber",
        text="machine.toml gives the wire attachment as the point %s mm back, %s mm right of T, but the gate "
             "wire_attachment_measured is %s: P-REACH counts the whole body hull until the facts record the "
             "attachment (rung R0b)" % (_show(point[0]), _show(point[1]), state),
        source="gate wire_attachment_measured (snapshot %s), machine.toml" % snap)
    return ({"wire_attachment": Param(default.value, default.unit, default.source, default.ref,
                                      disclosures=default.disclosures + (note,))},
            ("wire_attachment_back", "wire_attachment_right"))


def _gate_value_params(params: Mapping[str, Param[Any]], gates: GateView) -> dict[str, Param[Any]]:
    """The catalogue's gated values that an open gate replaces with its fact (``max_program_bytes``, R-KERF)."""
    snap = _snapshot(gates)
    out: dict[str, Param[Any]] = {}
    if gates.allowed("max_program_bytes") is True:
        value = gates.value("max_program_bytes")
        if type(value) is not int or value <= 0:
            raise ContextError("the gate max_program_bytes is open but carries %r, not a whole number of bytes above "
                               "0 (fact upload.max_verified_bytes): the gate snapshot is malformed" % (value,))
        out["max_program_bytes"] = Param(value, "byte", "FACT",
                                         "fact upload.max_verified_bytes = %d (gate max_program_bytes open, snapshot "
                                         "%s; rung R10)" % (value, snap))
    if gates.allowed("kerf_crossing") is True:
        value = gates.value("kerf_crossing")
        if not isinstance(value, Mapping) or set(value) != {key for key, _ in _KERF_KEYS}:
            raise ContextError("the gate kerf_crossing is open but carries %r, not the measured {w_b, L_ride, h_r} "
                               "(fact kerf.crossing): the gate snapshot is malformed" % (value,))
        note = Disclosure(
            id="RELAXATION:R-KERF", severity="amber",
            text="R-KERF: a wheel may cross a kerf up to w_b = %s mm wide over L_ride = %s mm and ride a recess up to "
                 "h_r = %s mm deep, as measured at rung R8k (gate kerf_crossing)"
                 % tuple(_show(_fact_number(value[key], "the gate kerf_crossing %s" % key, minimum=0.0))
                         for key, _ in _KERF_KEYS),
            source="gate kerf_crossing (snapshot %s)" % snap)
        for key, name in _KERF_KEYS:
            out[name] = Param(_fact_number(value[key], "the gate kerf_crossing %s" % key, minimum=0.0), "mm", "FACT",
                              "fact kerf.crossing %s (gate kerf_crossing open, snapshot %s; rung R8k)" % (key, snap),
                              disclosures=(note,))
    return out


def _t_hold(params: Mapping[str, Param[Any]], gates: GateView) -> Param[float]:
    """t_hold = T/2 (INFERRED, U-42), or the lowered value of the gate ``tabs_hold`` (R-TABS)."""
    snap = _snapshot(gates)
    thickness = params["stock_thickness"]
    half = quantize(float(Decimal(repr(float(thickness.value))) / 2), "ceiling")
    inferred = Disclosure(
        id="INFERRED:t_hold", severity="info",
        text="t_hold = T/2 = %s mm, the remaining thickness that holds a part, is inferred (U-42): grooves shallower "
             "than half the stock hold, typical tabs do not; rung R7 with the owner's consent (R-TABS) may lower it"
             % _show(half), source=catalogue_ref("t_hold"))
    ref = "%s: T/2 with %s = %s mm, rounded up to 0.001 mm" % (catalogue_ref("t_hold"), thickness.ref,
                                                              _show(thickness.value))
    if gates.allowed("tabs_hold") is not True:
        return Param(half, "mm", "INFERRED", ref, disclosures=(inferred,))
    value = gates.value("tabs_hold")
    if value is None:
        note = Disclosure(id="GATE_VALUE:tabs_hold", severity="info",
                          text="the gate tabs_hold is open but carries no lowered t_hold, so t_hold stays T/2 = %s mm"
                               % _show(half), source="gate tabs_hold (snapshot %s)" % snap)
        return Param(half, "mm", "INFERRED", ref, disclosures=(inferred, note))
    lowered = _fact_number(value, "the gate tabs_hold value (t_hold, mm)", above=0.0)
    if lowered > half:
        raise ContextError("the gate tabs_hold carries t_hold = %s mm, more than T/2 = %s mm: the relaxation R-TABS "
                           "only lowers t_hold; the gate snapshot is malformed" % (_show(lowered), _show(half)))
    return Param(lowered, "mm", "FACT", "the gate tabs_hold (snapshot %s; rung R7 with the owner's consent)" % snap,
                 disclosures=(Disclosure(
                     id="RELAXATION:R-TABS", severity="amber",
                     text="R-TABS: t_hold lowered to %s mm from T/2 = %s mm by the fact of rung R7 with the owner's "
                          "consent (gate tabs_hold)" % (_show(lowered), _show(half)),
                     source="gate tabs_hold (snapshot %s)" % snap),))


# ---------------------------------------------------------------------------
# The start pose (red-team PHYS-03; U-44)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Recorded:
    pose: Pose
    tolerance_mm: Param[float] | None
    tolerance_deg: Param[float] | None
    rounding_deg: Param[float] | None


def _recorded_pose(value: object, heading: bool, snap: str) -> _Recorded:
    """The park pose an open gate ``start_pose_known`` carries.

    goliath-run's form ``{x_mm, y_mm, heading_deg, tolerance_mm,
    tolerance_deg}`` (fact ``detection.park_pose``, typed in at rung R2), or a
    :class:`~goliath_prep.core.types.Pose` written as JSON ``{x, y, c,
    source}``. ``heading_deg`` is read as spec 08's heading theta (from the body
    toward the tool, degrees counter-clockwise from +X), so ``C = theta - 90``
    rounded to the 10-degree grid, and the rounding is added to the heading
    tolerance. The C word is kept only once the heading map is a fact.
    """
    what = "the gate start_pose_known"
    if not isinstance(value, Mapping):
        raise ContextError("%s is open but carries %r, not a park pose: the gate snapshot is malformed" % (what, value))
    keys = set(value)
    if keys == _PARK_MEASURED_KEYS:
        x = _fact_number(value["x_mm"], what + " x_mm")
        y = _fact_number(value["y_mm"], what + " y_mm")
        theta = _fact_number(value["heading_deg"], what + " heading_deg")
        tol_mm = _fact_number(value["tolerance_mm"], what + " tolerance_mm", minimum=0.0)
        tol_deg = _fact_number(value["tolerance_deg"], what + " tolerance_deg", minimum=0.0)
        exact = (Decimal(repr(theta)) - 90) % 360
        if exact < 0:
            exact += 360
        step = int((exact / 10).to_integral_value(rounding=ROUND_HALF_EVEN))
        c_word = (step * 10) % 360
        rounding = float(abs(exact - step * 10))
        recorded = ("fact detection.park_pose (gate start_pose_known open, snapshot %s; measured at rung R2): "
                    "x %s mm, y %s mm, body heading %s deg" % (snap, _show(x), _show(y), _show(theta)))
        if heading:
            source = ("%s = C%d by spec 08's theta = (C + 90) mod 360, rounded to the 10-degree grid"
                      % (recorded, c_word))
        else:
            source = "%s, not used as a C word while heading_map_verified is not open (U-20)" % recorded
        fact_ref = "gate start_pose_known (snapshot %s)" % snap
        rounding_param = None
        if heading and rounding > 0:
            heading_param = Param(theta, "deg", "FACT", "%s heading_deg" % fact_ref)
            rounding_param = Param.derived(
                quantize(rounding, "ceiling"), "deg",
                "the rounding of the measured heading %s deg to C%d" % (_show(theta), c_word), (heading_param,))
        try:
            pose = Pose(x, y, c_word if heading else None, source)
        except (TypeError, ValueError) as exc:  # pragma: no cover - the numbers were checked above
            raise ContextError("%s: %s" % (what, exc)) from exc
        # tolerance_mm is per axis (x and y taped from two edges; goliath-run gives the larger of the two), so
        # the pose lies in a box of +-tolerance_mm, and the disc that holds it has the radius tolerance_mm * sqrt(2)
        per_axis = Param(tol_mm, "mm", "FACT", "%s tolerance_mm (per axis)" % fact_ref)
        radius = Param.derived(
            quantize(tol_mm * math.sqrt(2.0), "ceiling"), "mm",
            "the disc that holds the box of +-tolerance_mm on each axis: tolerance_mm * sqrt(2), rounded up to "
            "0.001 mm", (per_axis,))
        return _Recorded(pose, radius, Param(tol_deg, "deg", "FACT", "%s tolerance_deg" % fact_ref), rounding_param)
    if keys == _PARK_POSE_KEYS:
        try:
            given = Pose(value["x"], value["y"], value["c"], value["source"])
            given.to_json()
        except (TypeError, ValueError) as exc:
            raise ContextError("%s is open but its park pose %r is not a valid pose: %s" % (what, value, exc)) from exc
        source = "%s (gate start_pose_known open, snapshot %s)" % (given.source, snap)
        if given.c is not None and not heading:
            source += "; its C word %d is not used while heading_map_verified is not open (U-20)" % given.c
        return _Recorded(Pose(given.x, given.y, given.c if heading else None, source), None, None, None)
    raise ContextError("%s is open but carries the keys %s, not {%s} (goliath-run's park pose) or {%s}: the gate "
                       "snapshot is malformed" % (what, sorted(map(str, keys)), ", ".join(sorted(_PARK_MEASURED_KEYS)),
                                                  ", ".join(sorted(_PARK_POSE_KEYS))))


def _start_pose(params: Mapping[str, Param[Any]], gates: GateView,
                measured: MeasuredGeometry | None) -> tuple[Pose | None, dict[str, Param[Any]]]:
    """The start pose: the recorded park pose (gate ``start_pose_known``), never a pose seen in this session alone.

    Position frames of this session count as the start pose only once their
    frame mapping is a fact (safety-model 9.5; W-FIRST; the R3 fact
    ``position.frame_mapping``), and no gate carries that fact. So a park pose
    in ``measured`` (MeasuredGeometry.park_pose) only restricts: it must agree
    with the recorded one, and without a recorded one there is no start pose,
    disclosed (design 3.1: uncertain evidence may restrict, never permit).
    """
    snap = _snapshot(gates)
    heading, _ = _state(gates, "heading_map_verified")
    is_open, state = _state(gates, "start_pose_known")
    recorded = _recorded_pose(gates.value("start_pose_known"), heading, snap) if is_open else None
    seen = None if measured is None else measured.park_pose
    where = "" if measured is None else "MeasuredGeometry %s" % measured.sha256[:12]
    if recorded is None:
        notes = [Disclosure(
            id="GATE:start_pose", severity="amber",
            text="NO START POSE (U-44): no park pose is recorded (gate start_pose_known %s), so W-FIRST refuses the "
                 "program's first line; rung R2 records the park pose (tape and protractor, measurement entry)"
                 % state,
            source="gate start_pose_known (snapshot %s)" % snap)]
        if seen is not None:
            notes.append(Disclosure(
                id="START_POSE:session-not-used", severity="amber",
                text="a park pose was seen in this session (%s, %s mm%s: %s), but it is not used as the start pose: "
                     "position frames count only once their frame mapping is a fact (rung R3, fact "
                     "position.frame_mapping), and no gate carries that fact; W-FIRST refuses until the park pose "
                     "measured at rung R2 is recorded (gate start_pose_known)"
                     % (_show(seen.x), _show(seen.y), "" if seen.c is None else ", C%d" % seen.c, seen.source),
                source="%s park_pose" % where))
        return None, {"start_pose": Param(
            "unknown", "enum", "GATE",
            "gate start_pose_known %s (snapshot %s; unlocked by rung R2, U-44)%s"
            % (state, snap, "" if seen is None else "; the park pose seen in this session is not used alone"),
            disclosures=tuple(notes))}

    tol_mm_inputs: list[Param[Any]] = [params["park_pose_tol"]]
    tol_deg_inputs: list[Param[Any]] = [params["park_pose_tol_deg"]]
    tol_mm_inputs += [p for p in (recorded.tolerance_mm,) if p is not None]
    tol_deg_inputs += [p for p in (recorded.tolerance_deg,) if p is not None]
    tol_mm = max(float(p.value) for p in tol_mm_inputs)
    tol_deg = max(float(p.value) for p in tol_deg_inputs)
    pose = recorded.pose
    cross_check = ""
    if seen is not None:
        session = seen if seen.c is None or heading else Pose(
            seen.x, seen.y, None,
            "%s; its C word %d is not used while heading_map_verified is not open (U-20)" % (seen.source, seen.c))
        _agree(session, pose, tol_mm, tol_deg)
        cross_check = ("; the park pose seen in this session (%s, %s mm%s: %s, %s) agrees with it within the "
                       "tolerance, a cross-check only (its frame mapping is not a fact)"
                       % (_show(session.x), _show(session.y), "" if session.c is None else ", C%d" % session.c,
                          session.source, where))
    rounding = recorded.rounding_deg
    deg_inputs = tuple(tol_deg_inputs) + (() if rounding is None else (rounding,))
    out: dict[str, Param[Any]] = {
        "start_pose": Param("known", "enum", "FACT", "the start pose: %s%s" % (pose.source, cross_check)),
        "start_pose_tol": Param.derived(
            tol_mm, "mm", "the start pose's position tolerance, a disc about the recorded point: the larger of "
            "park_pose_tol and the disc that holds the measurement's own box of +-tolerance_mm on each axis "
            "(safety-model 9.5)", tol_mm_inputs),
        "start_pose_tol_deg": Param.derived(
            _grid_sum(tol_deg, 0.0 if rounding is None else rounding.value), "deg",
            "the start pose's heading tolerance: the larger of park_pose_tol_deg and the measurement's own%s "
            "(safety-model 9.5)" % ("" if rounding is None else ", plus the rounding of the heading to the C grid"),
            deg_inputs),
    }
    return pose, out


def _agree(session: Pose, recorded: Pose, tol_mm: float, tol_deg: float) -> None:
    """The park pose seen in this session must be the recorded one, within the tolerance (K3 confirms it)."""
    dx, dy = session.x - recorded.x, session.y - recorded.y
    distance = math.sqrt(dx * dx + dy * dy)
    if distance > tol_mm:
        raise ContextError(
            "the park pose observed in this session (%s, %s mm: %s) is %.1f mm from the recorded park pose (%s, %s mm: "
            "%s), more than %s mm: measure the park pose again (K9) before the first line can be checked"
            % (_show(session.x), _show(session.y), session.source, distance, _show(recorded.x), _show(recorded.y),
               recorded.source, _show(tol_mm)))
    if session.c is not None and recorded.c is not None:
        turn = abs(session.c - recorded.c) % 360
        turn = min(turn, 360 - turn)
        if turn > tol_deg:
            raise ContextError(
                "the park pose observed in this session has the heading C%d, the recorded park pose C%d: %d degrees "
                "apart, more than %s: measure the park pose again (K9)" % (session.c, recorded.c, turn, _show(tol_deg)))


# ---------------------------------------------------------------------------
# Geometry from a context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextGeometry:
    """The shapes a context's Params describe, built with P04's kernel (:func:`context_geometry`).

    ``panel`` is P_work (the desk rectangle shrunk by geom_tol, or the
    measured panel clipped to SET PANEL SIZE); ``wheel_margins`` the margin per
    side; ``support_strips`` the declared strip width per side;
    ``wheel_work_area`` and ``tool_work_area`` (W-AREA, T-AREA);
    ``tower_points`` tower 1 and tower 2 at each placement; ``wheel_zones`` and
    ``tool_zones`` grown by ``tower_zone_margin`` (W-TOWER, T-TOWER);
    ``tower_bases`` (B-TOWER, T-TOWER); ``reach_region``, the points within
    ``reach_max`` of every tower (P-REACH).
    """

    panel: Polygon
    wheel_margins: Mapping[str, float]
    support_strips: Mapping[str, float]
    wheel_work_area: Polygon | MultiPolygon
    tool_work_area: Polygon | MultiPolygon
    tower_points: tuple[Vec2, ...]
    wheel_zones: Polygon | MultiPolygon
    tool_zones: Polygon | MultiPolygon
    tower_bases: Polygon | MultiPolygon
    reach_region: Polygon


def _work_panel(ctx: CheckContext) -> Polygon:
    p = ctx.params
    if ctx.measured is None:
        return desk_panel(p["panel_width"].value, p["panel_height"].value, p["geom_tol"].value)
    return job_panel(_measured_polygon(ctx.measured), ctx.measured.set_panel_size)


def _wheel_margins(ctx: CheckContext) -> dict[str, float]:
    return {side: float(ctx.params["m_edge_%s" % side].value) for side in SIDES}


def _tower_layout(ctx: CheckContext) -> tuple[float, float, str, float | None]:
    """W, H, the tower-2 axis and d_t for the kernel's tower functions, from the context's placements."""
    p = ctx.params
    axis = p["tower2_axis"].value
    placements = p["tower2_placements"].value
    corner = placements[0]
    distance = placements[1] if len(placements) > 1 else None
    if axis == "X":
        return corner, p["panel_height"].value, axis, distance
    return p["panel_width"].value, corner, axis, distance


def _check_geometry(ctx: CheckContext) -> None:
    """The panel the context describes exists and has an edge on the tower baseline, with the margins in use."""
    margins = _wheel_margins(ctx)
    base = ctx.params["baseline_side"].value
    try:
        panel = _work_panel(ctx)
        checked = edge_margins(panel, ctx.params["tower2_axis"].value,
                               {"m_edge": margins, "m_edge_baseline": margins[base]})
    except ValueError as exc:
        if isinstance(exc, ContextError):
            raise
        raise ContextError("the panel cannot be used: %s" % exc) from exc
    if dict(checked) != margins:  # pragma: no cover - the kernel and this module disagree about a margin
        raise RuntimeError("edge margins disagree: %r and %r" % (dict(checked), margins))


def context_geometry(ctx: CheckContext) -> ContextGeometry:
    """The panel, work areas, tower zones and bases and reach region of ``ctx`` (from its Params alone)."""
    if not isinstance(ctx, CheckContext):
        raise TypeError("ctx must be a CheckContext, got %r" % (ctx,))
    p = ctx.params
    panel = _work_panel(ctx)
    margins = _wheel_margins(ctx)
    strips = {side: float(p["support_strip_width_%s" % side].value) for side in SIDES}
    width, height, axis, distance = _tower_layout(ctx)
    towers = tower_points(width, height, axis, distance)  # type: ignore[arg-type]
    zone_margin = p["tower_zone_margin"].value
    return ContextGeometry(
        panel=panel,
        wheel_margins=MappingProxyType(margins),
        support_strips=MappingProxyType(strips),
        wheel_work_area=work_area_wheel(panel, strips, margins),
        tool_work_area=work_area_tool(panel, p["m_edge_tool"].value),
        tower_points=towers,
        wheel_zones=wheel_zones(width, height, axis, distance, margin=zone_margin),  # type: ignore[arg-type]
        tool_zones=tool_zones(width, height, axis, distance, margin=zone_margin),  # type: ignore[arg-type]
        tower_bases=tower_bases(width, height, axis, distance,  # type: ignore[arg-type]
                                radius=p["tower_base_radius"].value),
        reach_region=reach_region(towers, p["reach_max"].value),
    )


def context_disclosures(ctx: CheckContext) -> tuple[Disclosure, ...]:
    """Every disclosure the context's Params carry (their own and their inputs'), each once, in name order:
    what the banner, the report and the certificate must show for this context."""
    if not isinstance(ctx, CheckContext):
        raise TypeError("ctx must be a CheckContext, got %r" % (ctx,))
    seen: dict[Disclosure, None] = {}
    for name in sorted(ctx.params):
        for disclosure in ctx.params[name].all_disclosures():
            seen.setdefault(disclosure, None)
    return tuple(seen)
