# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The removal index and the time-exact W-SURF query (unit P06; docs/prep/safety-model.md 7.1 to 7.3).

Each in-material piece j removes R_j = sigma_j + D(r_tool + delta_tool + e_j)
with the deepest depth over the piece. Rem(j) is the prior removal declared in
job.toml and the R_i of the in-material pieces i < j. During piece j a wheel
must stay clear of Rem(j); the piece's own removal is left out, which the time
invariant min_k r_k - Delta > rho + r_tool + delta_tool + 2 e_max makes exact.
"""
from __future__ import annotations

import dataclasses
import logging
import math
from pathlib import Path

import pytest
from prep_removal_support import (
    DELTA_TOOL,
    E_MAX,
    FP,
    JOB,
    R_TOOL,
    REACH,
    RHO,
    context,
    cut_lines,
    cut_piece_indices,
    job,
    offset_of,
    offsets,
    pieces_of,
    program,
    wheel_path,
)
from shapely.geometry import LineString, Point, Polygon

from goliath_prep.core import ids
from goliath_prep.core.findings import Finding
from goliath_prep.core.types import FootprintSet, PriorCut
from goliath_prep.gcode import read_gcode
from goliath_prep.jobspec import JobSpec
from goliath_prep.material import (
    Clearance,
    InvariantError,
    PriorRemoval,
    RemovalIndex,
    assert_time_invariant,
    delta_for,
    derive_delta,
)
from goliath_prep.normalise import normalise

RT01 = Path(__file__).resolve().parents[1] / "redteam" / "cases" / "RT01-rear-wheel-follows-kerf"


def _index(prog, prior=None, delta=50.0, **kwargs):
    pcs = pieces_of(prog, delta)
    return pcs, RemovalIndex.build(pcs, R_TOOL, DELTA_TOOL, prior, footprint=FP, rho=RHO, **kwargs)


def _flags(pcs, index, cut: int, c: int) -> dict[int, list[tuple[int, float, object]]]:
    """For each support k: (piece, slack, witness) of each piece of ``cut`` whose wheel path fails W-SURF at C = c."""
    out: dict[int, list[tuple[int, float, object]]] = {}
    for k, d in offsets(c):
        out[k] = []
        for j in cut_piece_indices(pcs, cut):
            slack, witness = index.clearance(wheel_path(pcs, j, d), j, RHO)
            if witness is not None:
                out[k].append((j, slack, witness))
    return out


# ---------------------------------------------------------------------------
# The unit's tests first
# ---------------------------------------------------------------------------


def test_RT1_rear_wheel_in_own_kerf_flagged() -> None:
    """Red-team RT1, read from its committed case: a 520 mm through slot along +y at C = 0."""
    spec = JobSpec.load(RT01 / "job.toml")
    result = read_gcode((RT01 / "case.gi").read_text(encoding="utf-8"), flavour="gi", job=spec, file="case.gi")
    assert not result.refused
    prog = normalise(result, spec)
    pcs, index = _index(prog)
    slot = cut_piece_indices(pcs, 0)
    assert len(slot) >= 12  # the plunge, eleven pieces of the 520 mm feed, the retract
    flags = _flags(pcs, index, 0, 0)
    # The side supports sit 204.7 mm to either side of the slot: never near it.
    assert flags[2] == [] and flags[3] == []
    rear = flags[1]
    assert rear, "the rear wheel follows its own fresh kerf and must be flagged (safety-model 7.3)"
    # Exactly in time: a piece is flagged exactly when its rear-wheel path comes within reach of the kerf cut
    # before it. The kerf starts at y = 580; the rear wheel is 402 mm behind the tool at C = 0.
    flagged = {p for p, _, _ in rear}
    for j in slot:
        gap = 580.0 - (float(pcs[j]["y1"]) - 402.0)
        cut_before = bool(pcs["in_material"][:j].any())
        assert (j in flagged) == (cut_before and gap <= REACH), (j, gap)
    first, slack, witness = rear[0]
    assert first == min(flagged) and slack <= 0.0
    # The witness: the piece checked (the time), its cut, the nearest point of the kerf (its start), the hazard.
    assert witness.piece == first and witness.cut == 0
    assert witness.point[0] == pytest.approx(1200.0, abs=1e-6)
    assert witness.point[1] == pytest.approx(580.0 - R_TOOL - DELTA_TOOL, abs=1e-6)
    assert "kerf of cut 0" in witness.hazard and "line 11" in witness.hazard and "18.300" in witness.hazard
    assert witness.slack_mm == slack
    # raw = slack + the sum of the margins (safety-model 11.2); the margins are rho, r_tool, delta_tool and e.
    margins = dict(witness.margins)
    assert margins["rho"] == RHO and margins["r_tool"] == R_TOOL and margins["delta_tool"] == DELTA_TOOL
    total = 0.0
    for value in margins.values():
        total += value
    assert witness.raw_mm == pytest.approx(witness.slack_mm + total, abs=1e-9)
    # The witness fills only W-SURF's witness fields: the check files it after adding the wheel and the heading.
    finding = Finding.of(ids.W_SURF, "rear wheel over the fresh kerf",
                         witness=dataclasses.replace(witness, wheel=1, heading_c=0))
    assert finding.severity == "refuse"


def test_mirror_case_not_flagged() -> None:
    """The same slot cut the other way: the rear wheel leads, over material the tool reaches only later."""
    prog = program(cut_lines([(1200.0, 1100.0), (1200.0, 580.0)]))
    pcs, index = _index(prog)
    assert _flags(pcs, index, 0, 0) == {1: [], 2: [], 3: []}
    # Two cuts: during cut 0 the right wheel (204.7 mm to the right, 51 mm back) rolls along the line that cut 1
    # removes afterwards. Not flagged either...
    prog = program(cut_lines([(1000.0, 600.0), (1000.0, 900.0)], [(1204.7, 500.0), (1204.7, 900.0)]))
    pcs, index = _index(prog)
    assert _flags(pcs, index, 0, 0) == {1: [], 2: [], 3: []}
    # ...but once cut 1 has removed it, the same wheel path is over removed material.
    j = cut_piece_indices(pcs, 0)[-2]
    slack, witness = index.clearance(wheel_path(pcs, j, offset_of(2, 0)), len(pcs), RHO)
    assert witness is not None and slack < 0.0 and "kerf of cut 1" in witness.hazard
    assert witness.piece is None and witness.cut is None  # after the last piece: no piece is being cut


def test_time_invariant_asserted_and_delta_derived() -> None:
    # Spec values (safety-model 7.3): floor(211 - 55 - 3 - 0.25 - 0.1 - 1) = 151, so Delta = min(50, 151) = 50.
    assert derive_delta(FP, RHO, R_TOOL, DELTA_TOOL, E_MAX) == 50.0
    assert derive_delta(FP, RHO, 4.0, DELTA_TOOL, E_MAX) == 50.0  # the largest bit, 8 mm
    # A measured side support at 100 mm: the union's min_k r_k is 100: floor(40.65) = 40.
    short = FootprintSet(FP.vendor, ((402.0, 0.0), (100.0, 76.0), None), "union")
    assert derive_delta(short, RHO, R_TOOL, DELTA_TOOL, E_MAX) == 40.0
    # On the exact decimals: 100 - 49.65 - 3 - 0.25 - 2 x 0.05 - 1 = 46 exactly (floats would give 45.999...).
    assert derive_delta(short, 49.65, R_TOOL, DELTA_TOOL, E_MAX) == 46.0
    # Exactly 5 mm is the smallest piece allowed; below it the checker stops with M-INVARIANT (internal error).
    assert derive_delta(short, 90.0, R_TOOL, DELTA_TOOL, E_MAX) == 5.0
    with pytest.raises(InvariantError) as caught:
        derive_delta(short, 91.0, R_TOOL, DELTA_TOOL, E_MAX)
    finding = caught.value.finding
    assert finding.check_id == ids.M_INVARIANT and finding.severity == "internal-error"
    assert finding.witness.value == 4.0 and finding.witness.limit == 5.0
    # The catalogue's slack: min_k r_k - rho - r_tool - delta_tool - 2 e_max - 6 mm.
    assert finding.witness.slack_mm == pytest.approx(100 - 91 - 3 - 0.25 - 0.1 - 6)
    assert Finding.from_json(finding.to_json()) == finding
    # The invariant itself, strict: min_k r_k - Delta > rho + r_tool + delta_tool + 2 e_max (58.35 mm here).
    assert_time_invariant(FP, RHO, R_TOOL, DELTA_TOOL, E_MAX, 50.0)
    assert_time_invariant(short, RHO, R_TOOL, DELTA_TOOL, E_MAX, 41.6)
    for delta in (41.65, 42.0):
        with pytest.raises(InvariantError) as caught:
            assert_time_invariant(short, RHO, R_TOOL, DELTA_TOOL, E_MAX, delta)
        assert caught.value.finding.check_id == ids.M_INVARIANT
    for delta in (50.5, 0.0, -1.0, math.inf, math.nan):  # outside (0, 50]: outside the model
        with pytest.raises((InvariantError, ValueError)):
            assert_time_invariant(FP, RHO, R_TOOL, DELTA_TOOL, E_MAX, delta)
    # Asserted at start-up: an index cannot be built on pieces longer than the active footprint allows.
    prog = program(cut_lines([(1000.0, 600.0), (1000.0, 900.0)]))
    with pytest.raises(InvariantError) as caught:
        RemovalIndex.build(pieces_of(prog, 50.0), R_TOOL, DELTA_TOOL, None, footprint=short, rho=RHO)
    assert caught.value.finding.witness.value == 50.0 and caught.value.finding.witness.limit == 40.0
    built = RemovalIndex.build(pieces_of(prog, 40.0), R_TOOL, DELTA_TOOL, None, footprint=short, rho=RHO)
    assert built.delta_mm == 40.0 and built.e_max_mm == E_MAX
    # A wheel radius the pieces cannot serve: rho = 100 leaves floor(211 - 100 - 3 - 0.25 - 0.1 - 1) = 106 > 50,
    # but rho = 160 leaves floor(46.65) = 46 < 50, so pieces of 50 mm are refused.
    RemovalIndex.build(pieces_of(prog, 50.0), R_TOOL, DELTA_TOOL, None, footprint=FP, rho=100.0)
    with pytest.raises(InvariantError):
        RemovalIndex.build(pieces_of(prog, 50.0), R_TOOL, DELTA_TOOL, None, footprint=FP, rho=160.0)
    # From the context: a DERIVED Param naming every input it was computed from.
    ctx = context()
    param = delta_for(ctx)
    assert param.value == 50.0 and param.unit == "mm" and param.source == "DERIVED"
    assert {p.ref for p in param.inputs} >= {ctx.params[name].ref for name in (
        "r_min", "rho", "tool_diameter", "delta_tool", "check_chord_tol", "piece_length_min")}
    # rho is unmeasured (r_w is DEFAULT_UNMEASURED), and Delta carries that disclosure with it.
    assert "DEFAULT_UNMEASURED:r_w" in {d.id for d in param.all_disclosures()}
    param.to_json()  # on the canonical grid: a report can show it exactly


def test_closed_form_exactness_single_straight_cut_smoke() -> None:
    """Hand-computed cases of the closed form (the hypothesis property is in test_prep_removal_exactness.py).

    Along +x at C = 90 the rear support leads by 402 mm and the side supports trail by 51 mm at 204.7 mm to the
    side: never. At C = 0 a cut along +x has the rear support 402 mm to its side: never. Along +y at C = 0 the
    rear support trails by 402 mm: flagged once L >= 402 - 58.25 = 343.75 mm.
    """
    for c, path, flagged in ((90, [(800.0, 600.0), (1100.0, 600.0)], set()),
                             (0, [(800.0, 600.0), (1100.0, 600.0)], set()),
                             (0, [(1000.0, 200.0), (1000.0, 543.0)], set()),
                             (0, [(1000.0, 200.0), (1000.0, 544.0)], {1})):
        prog = program(cut_lines(path))
        pcs, index = _index(prog)
        got = {k for k, hits in _flags(pcs, index, 0, c).items() if hits}
        assert got == flagged, (c, path, got)


def test_prior_removal_seeds_index() -> None:
    hole = [(1180.0, 380.0), (1220.0, 380.0), (1220.0, 420.0), (1180.0, 420.0)]
    prior = PriorRemoval.from_cuts([PriorCut(tuple(hole), 6.0, "job.toml prior_removal[0]")])
    assert [r.ref for r in prior.regions] == ["job.toml prior_removal[0]"] and prior.regions[0].depth_mm == 6.0
    prog = program(cut_lines([(1200.0, 600.0), (1200.0, 700.0)]))
    pcs, index = _index(prog, prior)
    # Rem(0): before the first piece, the prior hole is already removed material.
    probe = LineString([(1150.0, 300.0), (1150.0, 500.0)])  # 30 mm from the hole: a 55 mm disc meets it
    slack, witness = index.clearance(probe, 0, RHO)
    assert slack == pytest.approx(30.0 - RHO) and witness is not None
    assert "prior removal job.toml prior_removal[0]" in witness.hazard and "6.000" in witness.hazard
    assert witness.point[0] == pytest.approx(1180.0) and 380.0 <= witness.point[1] <= 420.0
    assert witness.piece == 0 and witness.raw_mm == pytest.approx(30.0) and dict(witness.margins) == {"rho": RHO}
    result = index.query(probe, 0, RHO)
    assert result.hazard_prior == "job.toml prior_removal[0]" and result.hazard_piece is None
    # The same probe without prior removal is clear: nothing is cut there by the program.
    _, plain = _index(prog)
    assert plain.clearance(probe, 0, RHO) == (math.inf, None)
    # It is part of the removed region at every time, with its declared depth.
    for j in (0, len(pcs) // 2, len(pcs)):
        assert index.removed_region(j).contains(Point(1200.0, 400.0))
    assert index.removed_region(0, deeper_than=5.9).contains(Point(1200.0, 400.0))
    assert not index.removed_region(0, deeper_than=6.0).contains(Point(1200.0, 400.0))
    assert index.deepest_under(Point(1200.0, 400.0), 0) == (6.0, "job.toml prior_removal[0]")
    assert index.prior == prior
    # Only a PriorRemoval seeds Rem(0); a bare polygon list is refused, not guessed at.
    with pytest.raises(TypeError):
        RemovalIndex.build(pcs, R_TOOL, DELTA_TOOL, [Polygon(hole)], footprint=FP, rho=RHO)  # type: ignore[arg-type]
    # Without prior removal, the index says so: the panel is taken as intact, and that is disclosed.
    assert "DEFAULT:prior_removal" in {d.id for d in plain.disclosures}
    assert "DEFAULT:prior_removal" not in {d.id for d in index.disclosures}


def test_deepest_depth_used() -> None:
    # A ramp from Z 0 to Z -10 over 40 mm (one piece), then from Z -10 back to Z 0 over 100 mm (two pieces).
    lines = ["G21", "G90", "G0 Z5.000", "M3 S18000", "G0 X1000.000 Y600.000", "G1 Z0.000 F200",
             "G1 X1040.000 Y600.000 Z-10.000 F600", "G1 X1140.000 Y600.000 Z0.000 F600", "G0 Z5.000", "M5", "M30"]
    prog = program(lines)
    pcs, index = _index(prog)
    ramp_down = [j for j in range(len(pcs)) if pcs[j]["src_line"] == 7]
    ramp_up = [j for j in range(len(pcs)) if pcs[j]["src_line"] == 8]
    assert len(ramp_down) == 1 and len(ramp_up) == 2
    # Each piece has the deepest depth over the piece itself: not the move's, not the shallow end's.
    assert index.depth(ramp_down[0]) == 10.0
    assert index.depth(ramp_up[0]) == 10.0
    assert index.depth(ramp_up[1]) == 5.0
    after = len(pcs)
    # The ramp down starts at the stock top, but the piece removes material 10 mm deep all along its chord.
    assert index.removed_region(after, deeper_than=9.0).contains(Point(1001.0, 600.0))
    assert not index.removed_region(after, deeper_than=9.0).contains(Point(1139.0, 600.0))
    assert index.removed_region(after, deeper_than=4.0).contains(Point(1139.0, 600.0))
    assert index.deepest_under(Point(1001.0, 600.0), after) == (10.0, ramp_down[0])
    assert index.deepest_under(Point(1139.0, 600.0), after) == (5.0, ramp_up[1])
    assert index.deepest_under(Point(1040.0, 600.0), ramp_down[0]) is None  # nothing removed before it
    # The removal radius of a straight piece: r_tool + delta_tool; the witness names the deepest depth.
    assert index.removal_radius(ramp_down[0]) == R_TOOL + DELTA_TOOL
    slack, witness = index.clearance(Point(1001.0, 560.0), after, RHO)
    assert witness is not None and "depth 10.000 mm" in witness.hazard
    with pytest.raises(ValueError):
        index.depth(0)  # the first piece, the rapid to Z 5, is not in the material: it removes nothing


def test_prior_removal_from_jobspec_seeds_index() -> None:
    spec = job(prior_removal=[{"polygon": [[500.0, 500.0], [700.0, 500.0], [700.0, 650.0]], "depth_mm": 6.0}])
    prior = PriorRemoval.from_job(spec)
    assert [r.ref for r in prior.regions] == ["job.toml prior_removal[0]"] and prior.regions[0].depth_mm == 6.0
    # Through the context, the single place that turns job.toml into what the checks read.
    ctx = context(spec)
    prog = program(cut_lines([(1200.0, 600.0), (1200.0, 700.0)]), job_spec=spec)
    index = RemovalIndex.from_context(pieces_of(prog), ctx)
    assert index.prior == PriorRemoval.from_context(ctx) == prior
    slack, witness = index.clearance(Point(650.0, 560.0), 0, RHO)  # inside the declared triangle
    assert witness is not None and slack == pytest.approx(-RHO) and "job.toml prior_removal[0]" in witness.hazard
    # A job without prior removal: Rem(0) is empty.
    plain = RemovalIndex.from_context(pieces_of(program(cut_lines([(1200.0, 600.0), (1200.0, 700.0)]))), context())
    assert plain.prior.regions == ()
    assert plain.clearance(Point(650.0, 560.0), 0, RHO) == (math.inf, None)
    # The context's tool, runout and rho are the ones used, each recorded with its provenance.
    assert index.r_tool_mm == 3.0 and index.delta_tool_mm == 0.25 and index.rho_mm == RHO
    assert index.params["r_tool"].value == 3.0 and index.params["r_tool"].source == "DERIVED"
    assert index.params["r_tool"].inputs == (ctx.params["tool_diameter"],)
    assert index.params["delta_tool"] is ctx.params["delta_tool"]
    assert index.params["rho"] is ctx.params["rho"]
    assert index.params["piece_length_max"].value == 50.0
    assert index.kerf is None  # R-KERF closed: the desk view opens no gate
    # A raw build records no provenance it was not given.
    assert RemovalIndex.build(pieces_of(prog), R_TOOL, DELTA_TOOL, None, footprint=FP, rho=RHO).params == {}


# ---------------------------------------------------------------------------
# More of the behaviour
# ---------------------------------------------------------------------------


def test_own_piece_removal_left_out_and_guarded() -> None:
    """The piece's own removal is not in Rem(j); a query that would need it is an internal error, never a pass."""
    prog = program(cut_lines([(1000.0, 600.0), (1000.0, 900.0)]))
    pcs, index = _index(prog)
    j = cut_piece_indices(pcs, 0)[3]
    chord = wheel_path(pcs, j, (0.0, 0.0))  # the toolpath itself: inside the piece's own removal
    with pytest.raises(InvariantError) as caught:
        index.clearance(chord, j, RHO)
    assert caught.value.finding.check_id == ids.M_INVARIANT
    assert caught.value.finding.severity == "internal-error"
    # Just outside the reach of the piece's own removal, the query is answered (and is clear of earlier pieces).
    beside = wheel_path(pcs, j, (REACH + 1e-3, 0.0))
    slack, witness = index.clearance(beside, j, RHO)
    assert witness is None and slack == pytest.approx(1e-3, abs=1e-9)
    # Once the piece is over, the toolpath is over removed material.
    slack, witness = index.clearance(chord, j + 1, RHO)
    assert witness is not None and slack < 0.0
    # A link piece removes nothing, so nothing is left out for it.
    link = [i for i in range(len(pcs)) if not pcs[i]["in_material"]][0]
    assert index.clearance(wheel_path(pcs, link, (0.0, 0.0)), link, RHO) == (math.inf, None)


def test_slack_is_exact_and_touching_meets() -> None:
    """The slack is dist(path, sigma_i) - r_i - rho, exact; a path exactly rho + r_i away touches: violated."""
    prog = program(cut_lines([(1000.0, 600.0), (1000.0, 900.0)]))
    pcs, index = _index(prog)
    after = len(pcs)
    for gap in (200.0, 100.0, 58.5, 58.26):
        slack, witness = index.clearance(LineString([(1000.0 + gap, 650.0), (1000.0 + gap, 850.0)]), after, RHO)
        assert slack == pytest.approx(gap - REACH, abs=1e-9) and witness is None
    slack, witness = index.clearance(LineString([(1000.0 + REACH, 650.0), (1000.0 + REACH, 850.0)]), after, RHO)
    assert slack == pytest.approx(0.0, abs=1e-9) and witness is not None
    # A far query still gets the exact slack: the search widens until it is exact.
    slack, _ = index.clearance(Point(2400.0, 50.0), after, RHO)
    expected = math.sqrt((2400.0 - 1000.0) ** 2 + (600.0 - 50.0) ** 2) - REACH
    assert slack == pytest.approx(expected, abs=1e-9)
    # A polygon (a swept region that already holds the wheel discs) with rho = 0.
    region = Polygon([(1010.0, 700.0), (1100.0, 700.0), (1100.0, 800.0), (1010.0, 800.0)])
    slack, witness = index.clearance(region, after, 0.0)
    assert slack == pytest.approx(10.0 - R_TOOL - DELTA_TOOL) and witness is None
    region = Polygon([(990.0, 700.0), (1100.0, 700.0), (1100.0, 800.0), (990.0, 800.0)])
    slack, witness = index.clearance(region, after, 0.0)
    assert slack < 0.0 and witness is not None


def test_nothing_removed_means_no_hazard() -> None:
    prog = program(["G21", "G90", "G0 Z10.000", "G0 X100.000 Y100.000", "G0 X900.000 Y900.000", "M30"])
    pcs, index = _index(prog)
    assert len(index) == 0
    for j in range(len(pcs) + 1):
        assert index.clearance(Point(500.0, 500.0), j, RHO) == (math.inf, None)
        assert index.query(Point(500.0, 500.0), j, RHO).default_slack == math.inf
    assert index.removed_region(len(pcs)).is_empty


def test_arc_piece_removal_and_wheel_radius_include_the_sagitta() -> None:
    lines = ["G21", "G90", "G0 Z5.000", "M3 S18000", "G0 X1000.000 Y600.000", "G1 Z-5.000 F200",
             "G2 X1200.000 Y600.000 I100.000 J0.000 F600", "G0 Z5.000", "M5", "M30"]
    prog = program(lines)
    pcs, index = _index(prog)
    arc = [j for j in range(len(pcs)) if pcs[j]["kind"] == "arc_cw"]
    assert arc
    for j in arc:
        e = float(pcs[j]["sagitta"])
        assert 0.0 < e <= E_MAX
        assert index.removal_radius(j) == R_TOOL + DELTA_TOOL + e
    # The arc's top (y = 700) is removed although no chord vertex need lie on it.
    assert index.removed_region(len(pcs)).contains(Point(1100.0, 700.0 + R_TOOL))
    # W-SURF for a wheel during an arc piece: its centre's chord grown by rho + e_j (the wheel's own sagitta).
    j = arc[-1]
    e_j = float(pcs[j]["sagitta"])
    d = offset_of(1, 0)
    result = index.wheel_clearance(j, d, RHO)
    plain = index.query(wheel_path(pcs, j, d), j, RHO)
    assert result.slack == pytest.approx(plain.slack - e_j, abs=1e-12)
    if result.witness is not None:
        assert dict(result.witness.margins)["e_wheel"] == e_j


def test_wheel_clearance_and_track_match_the_plain_query() -> None:
    prog = program(cut_lines([(1200.0, 580.0), (1200.0, 1100.0)]))
    pcs, index = _index(prog)
    slot = cut_piece_indices(pcs, 0)
    for k, d in offsets(0):
        track = index.wheel_track(slot, d, RHO)
        assert len(track) == len(slot)
        for j, result in zip(slot, track):
            assert isinstance(result, Clearance)
            plain = index.query(wheel_path(pcs, j, d), j, RHO)
            assert (result.slack, result.witness) == (plain.slack, plain.witness)
            assert index.wheel_clearance(j, d, RHO) == result
            assert result.lifted is False and result.default_slack == result.slack
    # A track is consecutive pieces in time order.
    with pytest.raises(ValueError):
        index.wheel_track([slot[0], slot[2]], offset_of(1, 0), RHO)
    with pytest.raises(ValueError):
        index.wheel_track([], offset_of(1, 0), RHO)


def test_query_result_and_validation() -> None:
    prog = program(cut_lines([(1000.0, 600.0), (1000.0, 900.0)]))
    pcs, index = _index(prog)
    after = len(pcs)
    result = index.query(LineString([(1030.0, 650.0), (1030.0, 850.0)]), after, RHO)
    assert isinstance(result, Clearance)
    assert result.slack < 0.0 and result.witness is not None and result.hazard_piece is not None
    assert pcs[result.hazard_piece]["cut"] == 0 and result.hazard_prior is None
    assert result.lifted is False and result.ride_mm == 0.0
    for bad in (after + 1, -1):
        with pytest.raises(ValueError):
            index.clearance(Point(0.0, 0.0), bad, RHO)
    with pytest.raises(TypeError):
        index.clearance(Point(0.0, 0.0), True, RHO)  # type: ignore[arg-type]
    for bad_rho in (-1.0, math.nan, math.inf):
        with pytest.raises(ValueError):
            index.clearance(Point(0.0, 0.0), 0, bad_rho)
    with pytest.raises(TypeError):
        index.clearance((0.0, 0.0), 0, RHO)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        index.clearance(LineString([(0.0, 0.0), (math.inf, 1.0)]), 0, RHO)
    with pytest.raises(ValueError):
        index.clearance(Polygon([(0, 0), (10, 10), (10, 0), (0, 10)]), 0, RHO)  # self-intersecting: not valid
    with pytest.raises(ValueError):
        RemovalIndex.build(pcs, -1.0, DELTA_TOOL, None, footprint=FP, rho=RHO)
    with pytest.raises(ValueError):
        RemovalIndex.build(pcs, R_TOOL, -0.1, None, footprint=FP, rho=RHO)
    with pytest.raises(TypeError):
        RemovalIndex.build(list(pcs), R_TOOL, DELTA_TOOL, None, footprint=FP, rho=RHO)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RemovalIndex.build(pcs, R_TOOL, DELTA_TOOL, None, footprint=FP.vendor, rho=RHO)  # type: ignore[arg-type]


def test_removed_region_grows_and_holds_the_exact_capsules() -> None:
    prog = program(cut_lines([(1000.0, 600.0), (1000.0, 900.0)], [(1300.0, 600.0), (1500.0, 600.0)]))
    pcs, index = _index(prog)
    areas = [index.removed_region(j).area for j in range(len(pcs) + 1)]
    assert areas == sorted(areas) and areas[0] == 0.0
    exact = (300.0 * 6.5 + math.pi * 3.25 ** 2) + (200.0 * 6.5 + math.pi * 3.25 ** 2)
    # Polygons hold the exact capsules: never smaller, at most a little larger.
    assert exact <= areas[-1] <= exact * 1.01
    region = index.removed_region(len(pcs))
    for x, y in ((1000.0 + 3.25, 750.0), (1000.0, 900.0 + 3.25), (1300.0 - 3.25, 600.0), (1500.0, 600.0 - 3.25)):
        assert region.covers(Point(x, y)), (x, y)


def test_job_tool_and_runout_are_used() -> None:
    spec = job(policy={"delta_tool": 0.5}, tool={"d": 8.0})
    ctx = context(spec)
    prog = program(cut_lines([(1000.0, 600.0), (1000.0, 900.0)]), job_spec=spec)
    index = RemovalIndex.from_context(pieces_of(prog), ctx)
    assert index.delta_tool_mm == 0.5 and index.params["delta_tool"].source == "OWNER_JOB"
    assert index.r_tool_mm == 4.0
    assert index.removal_radius(cut_piece_indices(index.pieces, 0)[1]) == 4.5
    assert JOB.policy.delta_tool is None
    # An odd diameter: r_tool = d / 2 rounded up to the 0.001 grid (a larger removal is the safe side).
    odd = job(tool={"d": 6.005})
    index = RemovalIndex.from_context(pieces_of(program(cut_lines([(1000.0, 600.0), (1000.0, 900.0)]),
                                                        job_spec=odd)), context(odd))
    assert index.r_tool_mm == 3.003 and index.params["r_tool"].to_json()["value"] == 3.003
    # Pieces cut longer than the context allows are refused at start-up.
    with pytest.raises(InvariantError):
        RemovalIndex.from_context(pieces_of(prog, 50.0), dataclasses.replace(ctx, rho=200.0, params={
            **ctx.params, "rho": dataclasses.replace(ctx.params["rho"], value=200.0)}))


def test_build_logs_nothing_silently(caplog: pytest.LogCaptureFixture) -> None:
    """Without R-KERF the index logs no relaxation; its only disclosure is the intact-panel default."""
    prog = program(cut_lines([(1000.0, 600.0), (1000.0, 900.0)]))
    with caplog.at_level(logging.INFO, logger="goliath_prep.material.removal"):
        _, index = _index(prog)
    assert "R-KERF" not in caplog.text
    assert [d.id for d in index.disclosures] == ["DEFAULT:prior_removal"]
    assert all(d.severity == "info" for d in index.disclosures)
