# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The oracle's own model, checked against the numbers docs/prep/safety-model.md states (unit P21).

These tests pin what the oracle computes for itself (parameters with their
sources, supports, rotation radius, the spec triangle test, attached and
detached material on its 2 mm grid, the job-time panel, the safe zone) and
give positive controls: jobs the oracle must accept, so that "it refuses every
red-team case" cannot be met by an oracle that refuses everything.
"""
from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent


def _helper(name: str) -> Any:
    key = "goliath_prep_tests_oracle_" + name
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / (name + ".py"))
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


front = _helper("prep_oracle_front")
oracle = front.oracle
Op = front.Op
Draft = front.Draft


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _shown(verdict: Any) -> str:
    return "\n".join(h.describe() for h in verdict.violations[:30]) + "\nnotes: " + "; ".join(verdict.notes)


GROOVE = [Op((1200, 600), [("G1", 1200, 680)], depth=2, c=0)]


# ---------------------------------------------------------------------------
# Parameters: from the three files, each with its source
# ---------------------------------------------------------------------------


def test_parameters_come_from_the_three_files_with_their_source(tmp_path: Path) -> None:
    _, job = Draft(ops=GROOVE).write(tmp_path)
    p = oracle.resolve_params(job, front.DEFAULT_MACHINE, front.PARAMS_JSON)
    assert (p.W, p.H, p.T, p.tower2) == (2440.0, 1220.0, 18.0, "Y")
    assert p.r_w == 40.0 and p.rho == 55.0
    assert "params.json r_w" in p.source["r_w"] and "DEFAULT_UNMEASURED" in p.source["r_w"]
    assert "job.toml panel.T" in p.source["T"]
    assert p.r_cap == pytest.approx(3.25)  # r_tool + delta_tool, 6 mm bit
    assert p.t_hold == pytest.approx(9.0) and "T/2" in p.source["t_hold"]
    # Derived by the oracle itself, never read from params.json (whose 404 is the spec footprint's value).
    assert p.r_rot == 404.0 and p.r_min == pytest.approx(211.0) and p.r_max == pytest.approx(402.0)
    assert p.r_body == pytest.approx(519.0)
    assert p.piece_length == 50.0
    assert p.m_edge_side == {"left": 50.0, "right": 50.0, "bottom": 50.0, "top": 50.0}
    for name in ("r_rot", "rho", "r_body", "piece_length"):
        assert "derived" in p.source[name], (name, p.source[name])


def test_derived_values_follow_a_measured_footprint(tmp_path: Path) -> None:
    machine = _write(tmp_path, "machine.toml", "\n".join([
        "profile_version = 1", 'serial = "TEST-SERIAL-0001"', "[footprint]", "q1_mm = 430.0", "q1_deg = 0.0",
        "q2_mm = 211.0", "q2_deg = 76.0", "q3_mm = 211.0", "q3_deg = -76.0"]) + "\n")
    _, job = Draft(ops=GROOVE).write(tmp_path)
    p = oracle.resolve_params(job, machine, front.PARAMS_JSON)
    # The union model: the spec rear support and the measured one both count.
    assert sorted(s.r for s in p.supports) == [211.0, 211.0, 211.0, 211.0, 402.0, 430.0]
    assert p.r_max == 430.0 and p.r_min == 211.0
    assert p.r_rot >= 430.0 and p.r_rot == math.ceil(p.r_rot)
    assert "ROBOT_MEASURED" in p.source["supports"] or "OWNER_PROFILE" in p.source["supports"]


@pytest.mark.parametrize("radii, expected", [((38.0, 41.0, 39.0), 41.0), ((38.0, 39.0, None), 40.0),
                                             ((30.0, 31.0, 32.0), 32.0), ((44.0, None, None), 44.0)])
def test_wheel_contact_radius_is_the_maximum_never_the_mean(tmp_path: Path, radii: tuple, expected: float) -> None:
    lines = ["profile_version = 1", 'serial = "TEST-SERIAL-0001"']
    for k, r in enumerate(radii, start=1):
        if r is not None:
            lines += ["[wheel.q%d]" % k, "contact_radius_mm = %s" % r]
    machine = _write(tmp_path, "machine.toml", "\n".join(lines) + "\n")
    _, job = Draft(ops=GROOVE).write(tmp_path)
    p = oracle.resolve_params(job, machine, front.PARAMS_JSON)
    assert p.r_w == expected
    assert p.rho == expected + 15.0
    assert ("measured maximum" if None not in radii else "DEFAULT_UNMEASURED") in p.source["r_w"]


def test_owner_policy_only_tightens(tmp_path: Path) -> None:
    machine = _write(tmp_path, "machine.toml",
                     'profile_version = 1\nserial = "TEST-SERIAL-0001"\n[policy]\nm_edge = 70.0\nm_tower = 30.0\n')
    _, job = Draft(ops=GROOVE).write(tmp_path)
    p = oracle.resolve_params(job, machine, front.PARAMS_JSON)
    assert p.m_tower == 30.0 and min(p.m_edge_side.values()) == 70.0
    looser = _write(tmp_path, "looser.toml",
                    'profile_version = 1\nserial = "TEST-SERIAL-0001"\n[policy]\nm_edge = 20.0\n')
    with pytest.raises(oracle.OracleError, match="m_edge"):
        oracle.resolve_params(job, looser, front.PARAMS_JSON)


def test_near_edge_gate_lowers_only_the_tested_edges(tmp_path: Path) -> None:
    _, job = Draft(ops=GROOVE).write(tmp_path)
    gates = front.gates_r8(start=(1200, 640, 0), near_edge_allowed=["x_max", "y_min", "x_min"])
    p = oracle.resolve_params(job, front.DEFAULT_MACHINE, front.PARAMS_JSON, gates=gates)
    # x_min is the tower baseline (tower 2 on the Y axis): it keeps m_edge_baseline unless tested, and is tested.
    assert p.m_edge_side == {"left": 10.0, "right": 10.0, "bottom": 10.0, "top": 50.0}
    p = oracle.resolve_params(job, front.DEFAULT_MACHINE, front.PARAMS_JSON,
                              gates=front.gates_r8(start=(1200, 640, 0), near_edge_allowed=["x_max", "y_max"]))
    assert p.m_edge_side == {"left": 50.0, "right": 10.0, "bottom": 50.0, "top": 10.0}


def test_malformed_inputs_fail_loudly(tmp_path: Path) -> None:
    job = _write(tmp_path, "job.toml", '[panel]\nW = 2440.0\nH = 1220.0\ntower2 = "Y"\n[tool]\nd = 6.0\nflute = 22.0\n')
    with pytest.raises(oracle.OracleError, match="panel.T"):
        oracle.resolve_params(job, front.DEFAULT_MACHINE, front.PARAMS_JSON)
    job = _write(tmp_path, "job2.toml", '[panel]\nW = 2440.0\nH = 1220.0\nT = 18.0\ntower2 = "Y"\n'
                                       '[tool]\nd = 6.0\nflute = 22.0\n[mystery]\nx = 1\n')
    with pytest.raises(oracle.OracleError, match="mystery"):
        oracle.resolve_params(job, front.DEFAULT_MACHINE, front.PARAMS_JSON)
    _, good = Draft(ops=GROOVE).write(tmp_path)
    machine = _write(tmp_path, "m.toml", 'profile_version = 1\nserial = "TEST-SERIAL-0001"\n[wheels]\nq1 = 3\n')
    with pytest.raises(oracle.OracleError, match="wheels"):
        oracle.resolve_params(good, machine, front.PARAMS_JSON)
    with pytest.raises(oracle.OracleError, match="gate"):
        oracle.resolve_params(good, front.DEFAULT_MACHINE, front.PARAMS_JSON,
                              gates={"rotation_model": {"allowed": True, "value": "sideways"}})


# ---------------------------------------------------------------------------
# The robot model and the towers
# ---------------------------------------------------------------------------


def test_golden_supports_at_c0(tmp_path: Path) -> None:
    # safety-model 4.2: at C = 0, q1 = p + (0, -402), q2 = p + (204.7, -51.0), q3 = p + (-204.7, -51.0)
    _, job = Draft(ops=GROOVE).write(tmp_path)
    p = oracle.resolve_params(job, front.DEFAULT_MACHINE, front.PARAMS_JSON)
    (x1, y1), (x2, y2), (x3, y3) = oracle.support_offsets(p, 0)[:3]
    assert (round(x1, 6), round(y1, 6)) == (0.0, -402.0)
    assert (round(x2, 1), round(y2, 1)) == (204.7, -51.0)
    assert (round(x3, 1), round(y3, 1)) == (-204.7, -51.0)
    # Increasing C turns the body counter-clockwise: at C = 90 the rear support is at +x.
    (x1, y1) = oracle.support_offsets(p, 90)[0]
    assert (round(x1, 6), round(y1, 6)) == (402.0, 0.0)


def test_spec_triangle_test_edge_behaviour() -> None:
    def inside(tri: tuple, q: tuple) -> bool:
        return bool(oracle.in_triangle_spec(np.array([q[0]], float), np.array([q[1]], float), *tri)[0])

    z1 = ((0.0, 0.0), (0.0, 150.0), (150.0, 0.0))
    assert inside(z1, (75.0, 75.0)) and inside(z1, (0.0, 0.0)) and inside(z1, (10.0, 10.0))
    assert not inside(z1, (0.0, 50.0)) and not inside(z1, (50.0, 0.0))  # the legs: outside (spec 08)
    h = 1220.0
    z2 = ((0.0, h - 150.0), (0.0, h), (150.0, h))
    assert inside(z2, (50.0, h)) and not inside(z2, (0.0, h - 50.0)) and not inside(z2, (75.0, h - 75.0))
    assert inside(z2, (20.0, h - 20.0)) and not inside(z2, (100.0, h - 100.0))


def test_constants() -> None:
    assert oracle.SAMPLE_MM == 1.0 and oracle.GRID_MM == 2.0 and oracle.RIM_POINTS == 16
    assert 0.0 < oracle.HIT_TOL_MM <= 0.05


# ---------------------------------------------------------------------------
# Positive controls: what the oracle must accept
# ---------------------------------------------------------------------------


def test_a_short_groove_in_the_middle_is_accepted(tmp_path: Path) -> None:
    verdict = front.check_draft(Draft(ops=GROOVE), tmp_path, gates=front.gates_r8(start=(1200, 640, 0)))
    assert verdict.ok, _shown(verdict)
    assert verdict.mode == "plan" and verdict.headings == {0: 0}


def test_unforced_closed_100mm_square_finds_a_heading(tmp_path: Path) -> None:
    square = [("G1", 1250, 600), ("G1", 1250, 700), ("G1", 1150, 700), ("G1", 1150, 600)]
    verdict = front.check_draft(Draft(ops=[Op((1150, 600), square, depth=2)]), tmp_path,
                                gates=front.gates_r8(start=(1200, 650, 0)))
    assert verdict.ok, _shown(verdict)
    assert len(verdict.table[0]) == 36 and sum(1 for failed in verdict.table[0].values() if not failed) >= 1
    assert verdict.headings is not None and verdict.headings[0] in range(0, 360, 10)


def _wsurf_headings(tmp_path: Path, side_x: float, side_y: float, start_corner: int, ccw: bool) -> int:
    """Headings at which a closed rectangular groove passes W-SURF (safety-model 15's computation)."""
    cx, cy = 1500.0, 1500.0
    corners = [(cx - side_x / 2, cy - side_y / 2), (cx + side_x / 2, cy - side_y / 2),
               (cx + side_x / 2, cy + side_y / 2), (cx - side_x / 2, cy + side_y / 2)]
    order = corners if ccw else list(reversed(corners))
    order = order[start_corner:] + order[:start_corner]
    path = [("G1", x, y) for (x, y) in order[1:] + order[:1]]
    draft = Draft(W=3000.0, H=3000.0, ops=[Op(order[0], path, depth=2)])
    verdict = front.check_draft(draft, tmp_path, gates=front.gates_r8(start=(cx, cy, 0)))
    assert len(verdict.table[0]) == 36
    return sum(1 for failed in verdict.table[0].values() if "W-SURF" not in failed)


def test_closed_profiles_match_safety_model_section_15(tmp_path: Path) -> None:
    # safety-model 15: a closed 100 x 100 mm profile certifies at 36 of 36 headings with rho = 55 mm,
    # a 250 x 250 mm one at none, whatever the start point and direction.
    assert _wsurf_headings(tmp_path, 100.0, 100.0, 0, True) == 36
    for corner in range(4):
        for ccw in (True, False):
            assert _wsurf_headings(tmp_path, 250.0, 250.0, corner, ccw) == 0, (corner, ccw)
    # 200 x 150 mm: some headings, never all (the table: 6 of 36 over the start points it tried).
    counts = [_wsurf_headings(tmp_path, 200.0, 150.0, corner, ccw) for corner in range(4) for ccw in (True, False)]
    assert 0 < max(counts) < 36, counts


# ---------------------------------------------------------------------------
# Gates, start pose, notes
# ---------------------------------------------------------------------------


def test_no_start_pose_is_w_first(tmp_path: Path) -> None:
    verdict = front.check_draft(Draft(ops=GROOVE), tmp_path, gates=front.gates_r8(start=None))
    assert not verdict.ok and "W-FIRST" in verdict.check_ids()
    assert any(h.check == "W-FIRST" and h.rule == "no start pose" for h in verdict.violations)


def test_a_park_pose_seen_in_the_session_is_never_the_start_pose(tmp_path: Path) -> None:
    measured = {"info25": ("INFO;25;0.0000;0.0000", "INFO;25;2440.0000;0.0000", "INFO;25;2440.0000;1220.0000",
                           "INFO;25;0.0000;1220.0000"),
                "info27": "INFO;27;1220.0000", "park_pose": {"x": 1200.0, "y": 640.0, "c": 0, "source": "frames"}}
    verdict = front.check_draft(Draft(ops=GROOVE), tmp_path, gates=front.gates_r8(start=None), measured=measured)
    assert "W-FIRST" in verdict.check_ids(), _shown(verdict)


def test_without_gates_every_gate_is_closed(tmp_path: Path) -> None:
    verdict = front.check_draft(Draft(ops=GROOVE), tmp_path, gates=None)
    assert not verdict.ok
    gates = {h.rule for h in verdict.violations if h.check == "G-GATE"}
    assert {"first_cut_done", "z_semantics_verified", "spindle_verified"} <= gates
    assert any("all gates closed" in n for n in verdict.notes)
    assert "W-FIRST" in verdict.check_ids()  # no start pose either


def test_an_uncertain_gate_is_closed(tmp_path: Path) -> None:
    gates = front.gates_r8(start=(1200, 640, 0))
    gates["first_cut_done"] = {"allowed": None}
    verdict = front.check_draft(Draft(ops=GROOVE), tmp_path, gates=gates)
    assert any(h.check == "G-GATE" and h.rule == "first_cut_done" for h in verdict.violations), _shown(verdict)


def test_the_oracle_names_what_it_does_not_evaluate(tmp_path: Path) -> None:
    verdict = front.check_draft(Draft(ops=GROOVE), tmp_path, gates=front.gates_r8(start=(1200, 640, 0)))
    text = " ".join(verdict.notes)
    for check_id in ("D-FEED", "D-WORDS", "D-ROUNDTRIP", "D-SIZE", "F-GATE", "S-SERIES", "W-VENDORFLOOR", "T-PASS"):
        assert check_id in text, check_id


# ---------------------------------------------------------------------------
# Structure: attached and detached material on the 2 mm grid
# ---------------------------------------------------------------------------


def _world(tmp_path: Path, draft: Any, gates: Any = None) -> Any:
    gi, job = draft.write(tmp_path)
    gates = gates or front.gates_r8(start=(1200, 400, 0))
    program = front.read_program(gi, job, gates)
    return oracle.build_world(program, job, front.DEFAULT_MACHINE, front.PARAMS_JSON, gates=gates)


SQUARE = [("G1", 1300, 600), ("G1", 1300, 700), ("G1", 1200, 700), ("G1", 1200, 600)]


def test_a_closed_through_outline_detaches_its_inside_when_it_closes(tmp_path: Path) -> None:
    world = _world(tmp_path, Draft(ops=[Op((1200, 600), SQUARE, depth=18.3, through=True, c=0)]))
    end = world.end_time
    assert world.is_detached(1250.0, 650.0, end)
    assert not world.is_detached(1250.0, 650.0, 0)  # before the cut
    assert not world.is_detached(1100.0, 650.0, end)  # outside the outline
    last_side = world.time_at_line(world.line_of_point(1200.0, 640.0))
    assert not world.is_detached(1250.0, 650.0, last_side)  # the last side is not cut yet
    assert world.tower_separation() is None


def test_a_shallow_groove_holds(tmp_path: Path) -> None:
    world = _world(tmp_path, Draft(ops=[Op((1200, 600), SQUARE, depth=6.0, c=0)]))
    assert not world.is_detached(1250.0, 650.0, world.end_time)


def test_a_3mm_tab_holds_only_with_r_tabs(tmp_path: Path) -> None:
    # The outline is cut through except a 15 mm stretch left 3 mm thick (a tab) on 18 mm stock.
    ops = [Op((1200, 600), [("G1", 1300, 600), ("G1", 1300, 700), ("G1", 1200, 700), ("G1", 1200, 615)],
              depth=18.3, through=True, c=0),
           Op((1200, 615), [("G1", 1200, 600)], depth=15.0, c=0)]
    world = _world(tmp_path, Draft(ops=ops))
    assert world.is_detached(1250.0, 650.0, world.end_time)
    relaxed = _world(tmp_path, Draft(ops=ops), gates=front.gates_r8(start=(1200, 400, 0), tabs_hold=2.5))
    assert not relaxed.is_detached(1250.0, 650.0, relaxed.end_time)
    assert any("R-TABS" in n for n in relaxed.notes)


def test_two_fixations_anchor_a_freed_part(tmp_path: Path) -> None:
    ops = [Op((1200, 600), SQUARE, depth=18.3, through=True, c=0)]
    world = _world(tmp_path, Draft(ops=ops, fixations=[((1230.0, 630.0), 4.0)]))
    assert world.is_detached(1250.0, 650.0, world.end_time)  # one fixation does not hold it (R-FIXED needs two)
    world = _world(tmp_path, Draft(ops=ops, fixations=[((1230.0, 630.0), 4.0), ((1270.0, 670.0), 4.0)]))
    assert not world.is_detached(1250.0, 650.0, world.end_time)


def test_prior_removal_frees_a_part_before_the_first_piece(tmp_path: Path) -> None:
    ring = [[[1000.0, 1000.0], [1100.0, 1000.0], [1100.0, 1006.5], [1000.0, 1006.5]],
            [[1093.5, 1000.0], [1100.0, 1000.0], [1100.0, 1100.0], [1093.5, 1100.0]],
            [[1000.0, 1093.5], [1100.0, 1093.5], [1100.0, 1100.0], [1000.0, 1100.0]],
            [[1000.0, 1000.0], [1006.5, 1000.0], [1006.5, 1100.0], [1000.0, 1100.0]]]
    draft = Draft(ops=GROOVE, prior=[(p, 18.0) for p in ring])
    world = _world(tmp_path, draft)
    assert world.is_detached(1050.0, 1050.0, 0)
    assert not world.is_detached(1150.0, 1050.0, 0)
    shallow = _world(tmp_path, Draft(ops=GROOVE, prior=[(p, 6.0) for p in ring]))
    assert not shallow.is_detached(1050.0, 1050.0, 0)


def test_a_cut_across_the_panel_separates_the_towers(tmp_path: Path) -> None:
    # A through cut from near the left edge to near the right edge, joined to the edges by prior saw cuts.
    draft = Draft(ops=[Op((40, 610), [("G1", 2400, 610)], depth=18.3, through=True, c=90)],
                  prior=[([[0.0, 606.75], [45.0, 606.75], [45.0, 613.25], [0.0, 613.25]], 18.0)])
    world = _world(tmp_path, draft)
    assert world.tower_separation() is None  # the right edge is still joined
    draft.prior.append(([[2395.0, 606.75], [2440.0, 606.75], [2440.0, 613.25], [2395.0, 613.25]], 18.0))
    world = _world(tmp_path, draft)
    when = world.tower_separation()
    assert when is not None and 0 < when <= world.end_time


def test_t_attach_is_not_monotone_in_material(tmp_path: Path) -> None:
    """A design issue the oracle pins (safety-model 2, principle 4, against the T-ATTACH definition).

    Cutting a hole inside a part that an earlier cut freed is T-ATTACH. Declare
    the same part as removed already (prior through removal) and the hole is
    cut in air: no material, nothing detached, no T-ATTACH. More removal turned
    a refusal into a pass for this one check, so "every check is monotone in
    material" does not hold for T-ATTACH as defined; the oracle keeps the
    definition (it never loosens a check) and the monotonicity property below
    excludes it, visibly.
    """
    outline = Op((1200, 600), SQUARE, depth=18.3, through=True, c=0)
    hole = Op((1240, 640), [("G1", 1260, 640)], depth=2.0, c=0)
    gates = front.gates_r8(start=(1250, 1000, 0))
    base = front.check_draft(Draft(ops=[outline, hole]), tmp_path, gates=gates)
    assert "T-ATTACH" in base.check_ids(), _shown(base)
    inside = [[1203.25, 603.25], [1296.75, 603.25], [1296.75, 696.75], [1203.25, 696.75]]
    freed = front.check_draft(Draft(ops=[outline, hole], prior=[(inside, 18.0)]), tmp_path, gates=gates)
    assert "T-ATTACH" not in freed.check_ids(), _shown(freed)


# ---------------------------------------------------------------------------
# Job-time panel (P-GEOM)
# ---------------------------------------------------------------------------


RECT = ("INFO;25;0.0000;0.0000", "INFO;25;2445.5000;0.0000", "INFO;25;2445.5000;1221.2000",
        "INFO;25;0.0000;1221.2000")


def test_a_measured_rectangle_passes_p_geom_and_becomes_the_work_panel(tmp_path: Path) -> None:
    measured = {"info25": RECT, "info27": "INFO;27;1221.0000", "park_pose": None}
    verdict = front.check_draft(Draft(ops=GROOVE), tmp_path, gates=front.gates_r8(start=(1200, 640, 0)),
                                measured=measured)
    assert verdict.ok, _shown(verdict)
    assert any("SET PANEL SIZE 2445 x 1221" in n for n in verdict.notes), verdict.notes
    assert verdict.params.panel_source == "measured"


def test_no_vertex_stream_is_no_job(tmp_path: Path) -> None:
    measured = {"info25": (), "info27": None, "park_pose": None}
    verdict = front.check_draft(Draft(ops=GROOVE), tmp_path, gates=front.gates_r8(start=(1200, 640, 0)),
                                measured=measured)
    assert not verdict.ok and "P-GEOM" in verdict.check_ids()


def test_a_vertex_frame_fact_tries_only_the_identity(tmp_path: Path) -> None:
    square = ("INFO;25;0.0000;0.0000", "INFO;25;1220.0000;0.0000", "INFO;25;1220.0000;1220.0000",
              "INFO;25;0.0000;1220.0000")
    draft = Draft(W=1220.0, H=1220.0, ops=[Op((610, 600), [("G1", 610, 640)], depth=2, c=0)])
    measured = {"info25": square, "info27": "INFO;27;1220.0000", "park_pose": None}
    closed = front.gates_r8(start=(610, 620, 0), panel_vertices_usable=False)
    assert "P-GEOM" in front.check_draft(draft, tmp_path, gates=closed, measured=measured).check_ids()
    verdict = front.check_draft(draft, tmp_path, gates=front.gates_r8(start=(610, 620, 0)), measured=measured)
    assert "P-GEOM" not in verdict.check_ids(), _shown(verdict)


# ---------------------------------------------------------------------------
# The any-heading safe zone
# ---------------------------------------------------------------------------


def test_the_safe_zone_refuses_a_second_cut_near_the_first(tmp_path: Path) -> None:
    # With the heading map unverified, a kerf 300 mm from a later tool point lies in its annulus (156 to 459 mm).
    draft = Draft(W=2440.0, H=1600.0, ops=[Op((1200, 700), [("G1", 1200, 720)], depth=2, c=0),
                                           Op((1200, 1000), [("G1", 1200, 1020)], depth=2, c=0)])
    gates = front.gates_r8(start=(1200, 800, 0), heading_map_verified=False)
    verdict = front.check_draft(draft, tmp_path, gates=gates)
    assert "W-SAFEZONE" in verdict.check_ids(), _shown(verdict)
    assert any(h.check == "W-SAFEZONE" and h.rule == "annulus" for h in verdict.violations)
    one = Draft(W=2440.0, H=1600.0, ops=[Op((1200, 790), [("G1", 1200, 810)], depth=2, c=0)])
    verdict = front.check_draft(one, tmp_path, gates=gates)
    assert verdict.ok, _shown(verdict)


def test_the_safe_zone_needs_room_for_the_whole_rotation_disc(tmp_path: Path) -> None:
    # On a 1220 mm wide panel the zone is 182 mm wide (safety-model 8.3): x from 519 to 701.
    gates = front.gates_r8(start=(610, 800, 0), heading_map_verified=False)
    inside = Draft(W=1220.0, H=1600.0, ops=[Op((600, 790), [("G1", 600, 810)], depth=2, c=0)])
    assert front.check_draft(inside, tmp_path, gates=gates).ok
    outside = Draft(W=1220.0, H=1600.0, ops=[Op((490, 790), [("G1", 490, 810)], depth=2, c=0)])
    verdict = front.check_draft(outside, tmp_path, gates=gates)
    assert any(h.check == "W-SAFEZONE" and h.rule == "containment" for h in verdict.violations), _shown(verdict)
