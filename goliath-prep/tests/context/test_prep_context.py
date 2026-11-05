# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The context builder (unit P22; red-team C-03, C-06, C-16, PHYS-03, PHYS-05, PHYS-07).

``build_context`` is the one place that turns the job, the machine profile,
the gates and the measured panel into the CheckContext every check reads;
``emit_profile_for`` maps the same facts to the emitter's profile and
``rotation_model_for`` to the rotation model. The tests pin the mapping for
the facts of U-20 (heading map), U-21 (rotation), U-22 (footprint) and U-50
(the tower-baseline edge), and that every value carries its provenance.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest
from prep_context_support import (
    MEASURED_PROFILE,
    PARK,
    R3_FACTS,
    exported,
    gates,
    job,
    job_from,
    measured,
    profile,
)

from goliath_prep import context, defaults
from goliath_prep.context import (
    ContextError,
    build_context,
    context_disclosures,
    context_geometry,
    emit_profile_for,
    emit_profile_params,
    rotation_model_for,
    safe_zone_applies,
    split_rotation_allowed,
)
from goliath_prep.core.params import DISCLOSED_SOURCES, Param
from goliath_prep.core.types import CheckContext, ClosedGateView, EmitProfile, Pose
from goliath_prep.geom import footprint_radii, rotation_radius

REPO = Path(__file__).resolve().parents[3]
PARAMS_JSON = REPO / "docs" / "prep" / "params.json"
CONTEXT_FILE = Path(context.__file__).resolve()


def catalogue() -> dict[str, dict]:
    return {p["name"]: p for p in json.loads(PARAMS_JSON.read_text(encoding="utf-8"))}


def margins(ctx: CheckContext) -> dict[str, float]:
    return {side: ctx.params["m_edge_%s" % side].value for side in ("left", "right", "bottom", "top")}


# ---------------------------------------------------------------------------
# Footprint (U-22)
# ---------------------------------------------------------------------------


def test_union_footprint_once_measured() -> None:
    desk = build_context(job(), profile(), ClosedGateView())
    assert desk.footprint.active == "vendor" and desk.footprint.measured is None
    assert desk.r_rot == 404.0 and desk.params["r_rot"].source == "DERIVED"
    assert (desk.params["r_max"].value, desk.params["r_min"].value) == (402.0, 211.0)
    assert desk.params["support_centroid_distance"].value == pytest.approx(168.03, abs=0.001)
    assert desk.params["support_centroid_radius"].value == pytest.approx(235.8, abs=0.001)
    assert "support_r1_measured" not in desk.params

    prof = exported()
    ctx = build_context(job(), prof, ClosedGateView())
    assert ctx.footprint == prof.footprint_models()
    assert ctx.footprint.active == "union"
    assert ctx.footprint.vendor == desk.footprint.vendor  # a measurement adds positions, never replaces the spec
    assert ctx.r_rot == rotation_radius(ctx.footprint) == footprint_radii(ctx.footprint).r_rot
    assert ctx.r_rot >= 405.0 and ctx.r_rot >= desk.r_rot
    assert ctx.params["r_max"].value == 405.0 and ctx.params["r_min"].value == 210.0
    for name in ("support_r1_measured", "support_delta1_measured", "support_r3_measured"):
        assert ctx.params[name].source == "ROBOT_MEASURED", name
    # R_rot names every support position it was computed from, vendor and measured
    inputs = {p.ref for p in ctx.params["r_rot"].inputs}
    assert any("q1_mm" in ref for ref in inputs) and defaults.catalogue_ref("support_r1") in inputs
    assert ctx.rho == prof.params["rho"].value == 38.0 + 15.0

    # a support whose angle is unknown adds no measured position, the others still do
    partial = exported(MEASURED_PROFILE.replace("q2_deg = 75.5", 'q2_deg = "unknown"'))
    ctx = build_context(job(), partial, ClosedGateView())
    assert ctx.footprint.active == "union" and ctx.footprint.measured[1] is None
    assert "support_r2_measured" not in ctx.params


def test_body_radius_and_safe_zone_inset_follow_the_hull_in_use() -> None:
    desk = build_context(job(), profile(), ClosedGateView())
    assert desk.params["r_body"].value == 404.0 + 55.0 + 60.0  # R_rot + rho + m_body: 519 mm
    assert desk.params["safe_zone_inset_desk"].value == 404.0 + 55.0 + 50.0 + 10.0  # safety-model 8.3: 519 mm
    measured_hull = build_context(job(), exported(), ClosedGateView())
    assert measured_hull.params["r_body"].value >= 470.0  # the measured body reaches 470 mm behind T
    assert measured_hull.params["r_body"].source == "DERIVED"
    assert {p.ref for p in measured_hull.params["r_body"].inputs} >= {measured_hull.params["r_rot"].ref}


# ---------------------------------------------------------------------------
# Rotation model (U-21) and the emit profile (C-03)
# ---------------------------------------------------------------------------


def test_unverified_rotation_gives_full_disc_model() -> None:
    for view in (ClosedGateView(), gates(), gates(rotation_model=None), gates(rotation_model=False),
                 gates(rotation_model=(False, "in-place-shortest")), gates(rotation_model=(None, "about-centroid")),
                 gates(rotation_model=("yes", "in-place-shortest"))):
        assert rotation_model_for(view) == "unverified", view
        ctx = build_context(job(), profile(), view)
        assert ctx.rotation_model == "unverified"
        param = ctx.params["rotation_model"]
        assert (param.value, param.source) == ("unverified", "GATE")
        (note,) = [d for d in param.disclosures if d.id == "GATE:rotation_model"]
        # the full disc: radius R_rot + rho about the pivot, both directions
        assert "full disc" in note.text and "459 mm" in note.text and "R3" in param.ref
        assert safe_zone_applies(view)
    for model in ("in-place-shortest", "in-place-numeric", "during-translation", "about-centroid"):
        view = gates(rotation_model=(True, model))
        assert rotation_model_for(view) == model
        ctx = build_context(job(), profile(), view)
        assert ctx.rotation_model == model and ctx.params["rotation_model"].source == "FACT"
    # an open gate with a value nobody defined is a broken snapshot: refused, never read as a model
    for bad in (None, "unverified", "sideways", 42, ("in-place-shortest",)):
        with pytest.raises(ContextError, match="rotation_model"):
            rotation_model_for(gates(rotation_model=(True, bad)))
        with pytest.raises(ContextError, match="rotation_model"):
            build_context(job(), profile(), gates(rotation_model=(True, bad)))


def test_during_translation_fact_gives_combined_profile() -> None:
    for extra in ({}, {"heading_map_verified": True}, {"rotate_in_place_ok": True},
                  {"heading_map_verified": True, "rotate_in_place_ok": True, "start_pose_known": (True, PARK)}):
        view = gates(rotation_model=(True, "during-translation"), **extra)
        ctx = build_context(job(), profile(), view)
        emitted = emit_profile_for(view, ctx)
        assert emitted == EmitProfile("bare", "combined", "combined"), extra
        assert not split_rotation_allowed(view)
        style = ctx.params["emit_rotation_style"]
        assert style.source == "FACT" and "during-translation" in style.ref
        assert any(d.id == "EMIT:combined" for d in style.disclosures)
        if extra.get("rotate_in_place_ok"):
            # the two facts disagree: shown, and the line that holds under both is used
            assert any(d.id == "FACTS_DISAGREE:rotation" and d.severity == "amber" for d in style.disclosures)


def test_split_only_under_safe_zone_or_with_rotate_in_place_fact() -> None:
    # before rung R3: the any-heading safe zone applies, so rotate-in-place lines are checked as full discs
    for view in (ClosedGateView(), gates(rotate_in_place_ok=None), gates(heading_map_verified=True),
                 gates(rotation_model=(True, "in-place-shortest"))):
        ctx = build_context(job(), profile(), view)
        assert safe_zone_applies(view)
        assert emit_profile_for(view, ctx).rotation_style == "split"
        assert split_rotation_allowed(view)
        style = ctx.params["emit_rotation_style"]
        assert style.source == "GATE" and "safe zone" in style.ref
        assert ctx.params["any_heading_safe_zone"].value == "applies"
    # heading map and rotation model are facts but rotate-in-place is not: combined, never split
    for rip in (False, None, "yes"):
        view = gates(heading_map_verified=True, rotation_model=(True, "in-place-shortest"), rotate_in_place_ok=rip)
        ctx = build_context(job(), profile(), view)
        assert not safe_zone_applies(view) and ctx.params["any_heading_safe_zone"].value == "not-required"
        assert emit_profile_for(view, ctx).rotation_style == "combined"
        assert not split_rotation_allowed(view)
    # rotate-in-place is a fact: split, with or without the safe zone
    for extra in ({}, {"heading_map_verified": True, "rotation_model": (True, "in-place-numeric")}):
        view = gates(rotate_in_place_ok=True, **extra)
        ctx = build_context(job(), profile(), view)
        assert emit_profile_for(view, ctx).rotation_style == "split"
        assert ctx.params["emit_rotation_style"].source == "FACT"


def test_first_line_translate_then_rotate_only_with_every_fact() -> None:
    view = gates(**R3_FACTS)
    ctx = build_context(job(), profile(), view)
    assert ctx.start_pose is not None and ctx.start_pose.c == 0
    assert emit_profile_for(view, ctx) == EmitProfile("bare", "split", "translate-then-rotate")
    assert ctx.params["emit_first_line"].source == "FACT"
    # take away any one fact and the first line is one combined line again
    for name in R3_FACTS:
        reduced = {k: v for k, v in R3_FACTS.items() if k != name}
        view = gates(**reduced)
        ctx = build_context(job(), profile(), view)
        first = ctx.params["emit_first_line"]
        assert emit_profile_for(view, ctx).first_line == "combined", name
        assert first.source == "GATE" and "not a fact" in first.ref, name


def test_z_line_style_is_the_spec_reading_and_disclosed() -> None:
    for view in (ClosedGateView(), gates(**R3_FACTS)):
        ctx = build_context(job(), profile(), view)
        param = ctx.params["emit_z_line_style"]
        assert param.value == "bare" and param.source == "SPEC" and "spec 05" in param.ref
        assert any("U-31" in d.text for d in param.disclosures)


def test_emit_profile_for_refuses_another_gate_view() -> None:
    ctx = build_context(job(), profile(), ClosedGateView())
    with pytest.raises(ContextError, match="gate snapshot"):
        emit_profile_for(gates(**R3_FACTS), ctx)
    with pytest.raises(ContextError, match="gate snapshot"):
        emit_profile_params(gates(), ctx)
    with pytest.raises(TypeError):
        emit_profile_for(object(), ctx)  # type: ignore[arg-type]
    assert emit_profile_for(ClosedGateView(), ctx) == EmitProfile("bare", "split", "combined")


# ---------------------------------------------------------------------------
# Edge margins (C-16, U-50)
# ---------------------------------------------------------------------------


def test_tower_edge_margin_kept_until_R9_covers_it() -> None:
    # tower 2 on Y: the tower baseline is the left side (x = 0, edge x_min)
    closed = build_context(job(), profile(), ClosedGateView())
    assert margins(closed) == {"left": 50.0, "right": 50.0, "bottom": 50.0, "top": 50.0}
    assert closed.params["baseline_side"].value == "left"
    others = gates(near_edge_allowed=(True, ("x_max", "y_min", "y_max")))
    ctx = build_context(job(), profile(), others)
    assert margins(ctx) == {"left": 50.0, "right": 10.0, "bottom": 10.0, "top": 10.0}
    assert ctx.params["m_edge_right"].source == "DERIVED"
    assert any(p.source == "FACT" and "R9" in p.ref for p in ctx.params["m_edge_right"].inputs)
    assert any("m_edge_baseline" in p.ref for p in ctx.params["m_edge_left"].inputs)
    every = gates(near_edge_allowed=(True, ["x_min", "x_max", "y_min", "y_max"]))
    assert margins(build_context(job(), profile(), every)) == dict.fromkeys(("left", "right", "bottom", "top"), 10.0)
    # tower 2 on X: the baseline is the bottom side (y = 0, edge y_min)
    on_x = job(tower2="X")
    assert build_context(on_x, profile(), others).params["baseline_side"].value == "bottom"
    assert margins(build_context(on_x, profile(), gates(near_edge_allowed=(True, ("x_min", "x_max", "y_max"))))) == {
        "left": 10.0, "right": 10.0, "bottom": 50.0, "top": 10.0}
    # a closed or uncertain gate keeps every margin, whatever value it carries
    for view in (gates(near_edge_allowed=None), gates(near_edge_allowed=(False, ("x_max",)))):
        assert set(margins(build_context(job(), profile(), view)).values()) == {50.0}
    # owner policy only tightens, and stays in force after rung R9
    owner = profile(policy="m_edge = 60.0\n")
    assert set(margins(build_context(job(), owner, every)).values()) == {60.0}
    baseline = profile(policy="m_edge_baseline = 70.0\n")
    assert margins(build_context(job(), baseline, every)) == {"left": 70.0, "right": 10.0, "bottom": 10.0,
                                                              "top": 10.0}
    # the geometry uses exactly these margins
    geometry = context_geometry(ctx)
    assert dict(geometry.wheel_margins) == margins(ctx)
    assert geometry.wheel_work_area.bounds == pytest.approx((60.0, 20.0, 2420.0, 1200.0), abs=0.05)


def test_m_edge_after_unlock_is_the_catalogue_value() -> None:
    entry = catalogue()["m_edge"]
    assert context.M_EDGE_AFTER_R9 == float(entry["after_unlock"]) and entry["unlock_rung"] == "R9"


def test_malformed_near_edge_value_is_refused() -> None:
    for bad in (None, (), ("x_min", "x_min"), ("left",), "x_min", (1,)):
        with pytest.raises(ContextError, match="near_edge_allowed"):
            build_context(job(), profile(), gates(near_edge_allowed=(True, bad)))


# ---------------------------------------------------------------------------
# Towers, panel and reach (PHYS-05)
# ---------------------------------------------------------------------------


def test_desk_and_job_time_tower_placements_and_zone_margin() -> None:
    desk = build_context(job(), profile(), ClosedGateView())
    assert desk.params["tower2_placements"].value == (1220.0,)
    assert desk.params["tower_zone_margin"].value == 25.0 + 10.0  # m_tower + geom_tol at the desk
    assert desk.params["panel_geometry"].value == "desk" and "tower_distance" not in desk.params
    session = build_context(job(), profile(), ClosedGateView(), measured(info27=1221.5))
    assert session.params["tower2_placements"].value == (1220.0, 1221.5)  # the corner and INFO;27
    assert session.params["tower_distance"].source == "ROBOT_MEASURED"
    assert "INFO;27;1221.5000" in session.params["tower_distance"].ref
    assert session.params["tower_zone_margin"].value == 25.0
    assert session.params["panel_geometry"].source == "ROBOT_MEASURED"
    assert session.measured is not None and session.measured.sha256 in session.params["panel_geometry"].ref
    geometry = context_geometry(session)
    assert len(geometry.tower_points) == 3
    assert geometry.panel.bounds == (0.0, 0.0, 2440.0, 1220.0)
    on_x = build_context(job(tower2="X"), profile(), ClosedGateView(), measured(tower2="X", info27=2439.0))
    assert on_x.params["tower2_placements"].value == (2440.0, 2439.0)
    # the robot's tower-2 axis must be the job's
    with pytest.raises(ContextError, match="tower 2"):
        build_context(job(), profile(), ClosedGateView(), measured(tower2="X"))


def test_wire_attachment_needs_the_gate_and_the_profile() -> None:
    measured_point = exported()
    assert measured_point.wire_attachment == (150.0, -5.0)
    # the profile has a point, but the gate is closed: P-REACH keeps the whole body hull
    ctx = build_context(job(), measured_point, ClosedGateView())
    wire = ctx.params["wire_attachment"]
    assert wire.value == "body-hull" and wire.source == "DEFAULT_UNMEASURED"
    assert any("wire_attachment_measured" in d.text for d in wire.disclosures)
    assert "wire_attachment_back" not in ctx.params
    # the gate is open and agrees with the profile: the measured point counts
    agree = gates(wire_attachment_measured=(True, {"kind": "offset", "back_mm": 150.0, "right_mm": -5.0}))
    ctx = build_context(job(), measured_point, agree)
    assert ctx.params["wire_attachment"].value == "measured-point"
    assert (ctx.params["wire_attachment_back"].value, ctx.params["wire_attachment_right"].value) == (150.0, -5.0)
    # the gate and the profile disagree: refused, the profile must be exported again
    for value in ({"kind": "offset", "back_mm": 140.0, "right_mm": -5.0}, {"kind": "on_spindle_axis"},
                  {"kind": "offset"}, "offset", None):
        with pytest.raises(ContextError, match="wire"):
            build_context(job(), measured_point, gates(wire_attachment_measured=(True, value)))
    # the gate is open but the profile has no point (not exported since rung R0b): body hull, disclosed
    ctx = build_context(job(), profile(), agree)
    wire = ctx.params["wire_attachment"]
    assert wire.value == "body-hull" and any("export" in d.text for d in wire.disclosures)
    assert ctx.params["reach_max"].value == 3500.0
    geometry = context_geometry(ctx)
    assert geometry.reach_region.contains(geometry.panel)  # a 2440 x 1220 sheet is well within reach


# ---------------------------------------------------------------------------
# Start pose (PHYS-03, U-44)
# ---------------------------------------------------------------------------


def test_start_pose_from_the_park_pose_fact() -> None:
    # no recorded pose: none, and the context says W-FIRST refuses
    ctx = build_context(job(), profile(), ClosedGateView())
    assert ctx.start_pose is None
    unknown = ctx.params["start_pose"]
    assert unknown.value == "unknown" and any("W-FIRST" in d.text for d in unknown.disclosures)
    # the R2 park pose: its heading is a C word only once the heading map is a fact (U-20)
    ctx = build_context(job(), profile(), gates(start_pose_known=(True, PARK)))
    assert ctx.start_pose == Pose(1220.0, 610.0, None, ctx.start_pose.source)
    assert "heading_map_verified" in ctx.start_pose.source
    assert ctx.params["start_pose_tol"].value == 20.0  # the larger of park_pose_tol and the measurement's own
    ctx = build_context(job(), profile(), gates(start_pose_known=(True, PARK), heading_map_verified=True))
    assert ctx.start_pose.c == 0  # theta 90 deg: C = theta - 90
    wide = dict(PARK, tolerance_mm=30.0, heading_deg=103.0)
    ctx = build_context(job(), profile(), gates(start_pose_known=(True, wide), heading_map_verified=True))
    assert ctx.start_pose.c == 10 and ctx.params["start_pose_tol"].value == 30.0
    assert ctx.params["start_pose_tol_deg"].value == 5.0 + 3.0  # tolerance plus the rounding to the C grid
    # the Pose form of a gate snapshot written by hand for tests (x, y, c, source)
    pose_form = {"x": 1000.0, "y": 600.0, "c": 90, "source": "detection.park_pose (test data)"}
    ctx = build_context(job(), profile(), gates(start_pose_known=(True, pose_form), heading_map_verified=True))
    assert (ctx.start_pose.x, ctx.start_pose.y, ctx.start_pose.c) == (1000.0, 600.0, 90)
    for bad in ({"x_mm": 1.0}, {**PARK, "extra": 1}, {**PARK, "x_mm": "far"}, {**PARK, "tolerance_mm": -1.0},
                {**PARK, "y_mm": 0.0001}, "here", None, {**pose_form, "c": 95}):
        with pytest.raises(ContextError, match="start_pose_known"):
            build_context(job(), profile(), gates(start_pose_known=(True, bad)))


def test_start_pose_from_the_session_must_agree_with_the_fact() -> None:
    session = Pose(1225.0, 612.0, 0, "position frames after detection (test data)")
    ctx = build_context(job(), profile(), gates(heading_map_verified=True), measured(park=session))
    assert ctx.start_pose == session and ctx.params["start_pose"].source == "ROBOT_MEASURED"
    both = gates(heading_map_verified=True, start_pose_known=(True, PARK))
    assert build_context(job(), profile(), both, measured(park=session)).start_pose == session
    far = Pose(1300.0, 612.0, 0, "position frames after detection (test data)")
    with pytest.raises(ContextError, match="park pose"):
        build_context(job(), profile(), both, measured(park=far))
    turned = Pose(1225.0, 612.0, 90, "position frames after detection (test data)")
    with pytest.raises(ContextError, match="park pose"):
        build_context(job(), profile(), both, measured(park=turned))
    # before the heading map is a fact the session's C word is not used either
    ctx = build_context(job(), profile(), ClosedGateView(), measured(park=session))
    assert ctx.start_pose.c is None and (ctx.start_pose.x, ctx.start_pose.y) == (1225.0, 612.0)


# ---------------------------------------------------------------------------
# Values that gates carry
# ---------------------------------------------------------------------------


def test_gate_valued_parameters() -> None:
    closed = build_context(job(), profile(), ClosedGateView())
    assert closed.params["max_program_bytes"] is defaults.SPEC_PARAMS["max_program_bytes"]
    for name in ("kerf_width_max", "kerf_ride_max", "recess_ignore_max"):
        assert closed.params[name] is defaults.SPEC_PARAMS[name]
    t_hold = closed.params["t_hold"]
    assert (t_hold.value, t_hold.source) == (8.8, "INFERRED") and "U-42" in t_hold.disclosures[0].text
    assert build_context(job(T=Decimal("17.605")), profile(), ClosedGateView()).params["t_hold"].value == 8.803

    view = gates(max_program_bytes=(True, 204800), kerf_crossing=(True, {"w_b": 4.0, "L_ride": 30.0, "h_r": 0.5}),
                 tabs_hold=(True, 2.5))
    ctx = build_context(job(), profile(), view)
    assert (ctx.params["max_program_bytes"].value, ctx.params["max_program_bytes"].source) == (204800, "FACT")
    assert [ctx.params[n].value for n in ("kerf_width_max", "kerf_ride_max", "recess_ignore_max")] == [4.0, 30.0, 0.5]
    assert all(ctx.params[n].source == "FACT" for n in ("kerf_width_max", "kerf_ride_max", "recess_ignore_max"))
    assert any("R-KERF" in d.text for d in ctx.params["kerf_width_max"].disclosures)
    assert (ctx.params["t_hold"].value, ctx.params["t_hold"].source) == (2.5, "FACT")
    assert any("R-TABS" in d.text for d in ctx.params["t_hold"].disclosures)
    # tabs_hold open without a lowered value: T/2 stays, and the context says why
    ctx = build_context(job(), profile(), gates(tabs_hold=True))
    assert ctx.params["t_hold"].value == 8.8 and any("tabs_hold" in d.text for d in ctx.params["t_hold"].disclosures)
    for name, bad in (("max_program_bytes", 0), ("max_program_bytes", "big"), ("max_program_bytes", None),
                      ("kerf_crossing", {"w_b": 4.0}), ("kerf_crossing", {"w_b": -1.0, "L_ride": 1.0, "h_r": 0.0}),
                      ("kerf_crossing", None), ("tabs_hold", 9.0), ("tabs_hold", 0.0), ("tabs_hold", "thin")):
        with pytest.raises(ContextError, match=name):
            build_context(job(), profile(), gates(**{name: (True, bad)}))


# ---------------------------------------------------------------------------
# Provenance (C-06; the owner's rule: nothing defaulted silently)
# ---------------------------------------------------------------------------


def contexts() -> list[tuple[CheckContext, object, object]]:
    obstacle_job = job_from({
        "panel": {"W": 2440, "H": 1220, "T": 18, "tower2": "Y"},
        "tool": {"d": 6, "flute": 22},
        "obstacles": [{"polygon": [[100, 100], [180, 100], [180, 160]], "height_mm": 25, "kind": "clamp"}],
        "fixations": [{"point": [1200, 600], "height_mm": 4.5}],
        "prior_removal": [{"polygon": [[500, 500], [700, 500], [700, 650]], "depth_mm": 6}],
        "supports": {"right": 300},
    })
    out = []
    for jb, prof, view, meas in (
        (job(), profile(), ClosedGateView(), None),
        (obstacle_job, exported(policy="m_edge = 60.0\n"), gates(**R3_FACTS), None),
        (job(), exported(), gates(**R3_FACTS, near_edge_allowed=(True, ("x_max",)),
                                  wire_attachment_measured=(True, {"kind": "offset", "back_mm": 150.0,
                                                                   "right_mm": -5.0})),
         measured(park=Pose(1220.0, 610.0, 0, "position frames (test data)"))),
    ):
        out.append((build_context(jb, prof, view, meas), jb, prof))
    return out


def test_every_ctx_value_has_provenance() -> None:
    for ctx, jb, prof in contexts():
        for name, param in ctx.params.items():
            assert isinstance(param, Param) and param.ref.strip(), name
            if param.source in DISCLOSED_SOURCES:
                assert param.disclosures, name
            if param.source == "DERIVED":
                assert param.inputs, name
        # every catalogue value, every derived value and every job value is there
        assert set(defaults.SPEC_PARAMS) <= set(ctx.params)
        for name in ("r_max", "r_min", "support_centroid_distance", "support_centroid_radius", "r_rot", "rho",
                     "r_body", "safe_zone_inset_desk", "t_hold"):
            assert name in ctx.params, name
        assert set(context.CONTEXT_PARAMS) <= set(ctx.params)
        for name, param in jb.params().items():
            assert ctx.params[name] is param, name
        for name, param in prof.params.items():
            if name not in context.REPLACED_PROFILE_PARAMS:
                assert ctx.params[name] is param, name
        # values with their own provenance
        assert ctx.start_pose is None or ctx.start_pose.source.strip()
        assert all(o.ref for o in ctx.obstacles + ctx.fixations + ctx.prior_removal)
        assert ctx.obstacles == jb.obstacles and ctx.fixations == jb.fixations
        assert ctx.prior_removal == jb.prior_removal
        # the record shows exactly the values in use, and reads back
        data = ctx.to_json()
        assert CheckContext.from_json(json.loads(json.dumps(data)), gates=ctx.gates) == ctx
        # every disclosure of every value is gathered for the banner
        shown = set(context_disclosures(ctx))
        for param in ctx.params.values():
            assert set(param.all_disclosures()) <= shown
    desk = contexts()[0][0]
    assert any(d.id == "DEFAULT_UNMEASURED:r_w" for d in context_disclosures(desk))
    assert any(d.id == "GATE:heading_map_verified" for d in context_disclosures(desk))


def test_closed_gate_values_are_disclosed() -> None:
    ctx = build_context(job(), profile(), ClosedGateView())
    for name in ("rotation_model", "heading_map_verified", "rotate_in_place_ok", "start_pose"):
        param = ctx.params[name]
        assert param.source == "GATE" and param.disclosures, name
    uncertain = build_context(job(), profile(), gates(heading_map_verified=None))
    assert "uncertain" in uncertain.params["heading_map_verified"].ref


def test_inputs_are_type_checked() -> None:
    with pytest.raises(TypeError):
        build_context(object(), profile(), ClosedGateView())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_context(job(), object(), ClosedGateView())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_context(job(), profile(), object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_context(job(), profile(), ClosedGateView(), object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        rotation_model_for(object())  # type: ignore[arg-type]


def test_context_module_imports_neither_the_runner_nor_the_oracle(astscan: ModuleType) -> None:
    here = str(CONTEXT_FILE)
    assert astscan.find_references(here, "goliath_prep.geom")  # positive control
    assert astscan.find_references(here, "goliath_run") == []
    assert astscan.find_references(here, "oracle") == []
    for name in ("sum", "fsum", "math.hypot", "math.dist", "math.cos", "math.sin"):
        assert astscan.find_calls(here, name) == [], name
