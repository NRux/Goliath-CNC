# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The relaxation R-KERF in the removal index (unit P06; safety-model 7.5; U-41; red-team C-10, RT24).

R-KERF is closed by default: every removal deeper than eps_surf is a wheel
keep-out. Only the R8k fact opens it (the gate ``kerf_crossing``, whose
measured w_b, L_ride and h_r the context turns into FACT Params with the amber
disclosure ``RELAXATION:R-KERF``). With it, removal shallower than h_r is
ignored, the wheel must stay clear of the wide removal (the morphological
opening of the removal by w_b), and it may meet the narrow rest over a run of
less than L_ride along a track that holds the whole run.

The geometry: at C = 90 the left support (q3) sits 51.0 mm behind and 204.7 mm
to the side of the tool, so a groove cut along +y at x = 1000 drives the left
wheel across a line along y = 600 at x = 1051.
"""
from __future__ import annotations

import dataclasses
import logging
import math

import pytest
from prep_removal_support import (
    DELTA_TOOL,
    FP,
    R_TOOL,
    REACH,
    RHO,
    Gates,
    context,
    cut_piece_indices,
    kerf_gates,
    offset_of,
    pieces_of,
    program,
)
from shapely.geometry import LineString, Polygon

from goliath_prep.core import ids
from goliath_prep.core.params import Param
from goliath_prep.core.types import FootprintSet
from goliath_prep.material import InvariantError, KerfRelaxation, RemovalIndex, delta_for, derive_delta

LEFT_C90 = offset_of(3, 90)


def _lines(line_depth: float, groove_end: float = 870.0, *, extra: list[str] | None = None) -> list[str]:
    """Cut 0: a line along y = 600 from x = 800 to 1600, ``line_depth`` deep; cut 1: a 3 mm groove along +y."""
    return ["G21", "G90", "G0 Z10.000", "M3 S18000",
            "G0 X800.000 Y600.000", "G1 Z%.3f F200" % -line_depth, "G1 X1600.000 Y600.000 F800", "G0 Z10.000",
            *(extra or []),
            "G0 X1000.000 Y700.000", "G1 Z-3.000 F200", "G1 X1000.000 Y%.3f F800" % groove_end, "G0 Z10.000",
            "M5", "M30"]


def _groove_cut(pcs) -> int:
    return int(max(pcs["cut"]))


def _left_track(index: RemovalIndex) -> list:
    pcs = index.pieces
    return index.wheel_track(cut_piece_indices(pcs, _groove_cut(pcs)), LEFT_C90, RHO)


def _index(lines: list[str], gates=None) -> RemovalIndex:
    ctx = context(gates=gates)
    return RemovalIndex.from_context(pieces_of(program(lines)), ctx)


def test_left_wheel_crosses_the_line_at_c90() -> None:
    assert LEFT_C90[0] == pytest.approx(51.0, abs=0.1) and LEFT_C90[1] == pytest.approx(-204.7, abs=0.1)


def test_rkerf_only_with_fact() -> None:
    """Red-team RT24: the left wheel crosses a 0.3 mm engraving. Refused unless the R8k fact ignores it."""
    lines = _lines(0.3)
    # Closed by default (the desk view opens no gate), closed while uncertain, closed when the gate says no.
    for gates in (None, kerf_gates(0.0, 0.0, 0.5, allowed=None), kerf_gates(0.0, 0.0, 0.5, allowed=False), Gates()):
        index = _index(lines, gates)
        assert index.kerf is None
        assert "RELAXATION:R-KERF" not in {d.id for d in index.disclosures}
        track = _left_track(index)
        hits = [r for r in track if r.witness is not None]
        assert hits, gates
        witness = hits[0].witness
        assert witness.cut == 1 and "kerf of cut 0" in witness.hazard and "depth 0.300 mm" in witness.hazard
        assert math.dist(witness.point, (1051.0, 600.0)) <= 60.0
        assert all(not r.lifted for r in track)
    # With the fact, h_r = 0.5 mm: recesses shallower than h_r are ignored (safety-model 7.5).
    index = _index(lines, kerf_gates(0.0, 0.0, 0.5))
    assert index.kerf is not None and index.kerf.h_r_mm == 0.5
    assert index.params["recess_ignore_max"].source == "FACT"
    track = _left_track(index)
    assert all(r.witness is None for r in track)
    lifted = [r for r in track if r.lifted]
    assert lifted and all(r.default_slack <= 0.0 < r.slack for r in lifted)
    # The relaxation is disclosed whenever it is in force (amber), with the measured values.
    kerf_notes = [d for d in index.disclosures if d.id == "RELAXATION:R-KERF"]
    assert kerf_notes and kerf_notes[0].severity == "amber" and "h_r = 0.5" in kerf_notes[0].text
    # "Shallower than h_r": a recess exactly h_r deep still counts (this module's reading, the safe one).
    index = _index(lines, kerf_gates(0.0, 0.0, 0.3))
    assert any(r.witness is not None for r in _left_track(index))
    # Deeper removal is never ignored: the same crossing over a 3 mm groove is refused with h_r = 0.5 mm.
    index = _index(_lines(3.0), kerf_gates(0.0, 0.0, 0.5))
    assert any(r.witness is not None for r in _left_track(index))


def test_rkerf_logged_when_in_force(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="goliath_prep.material.removal"):
        _index(_lines(0.3), kerf_gates(8.0, 150.0, 0.5))
    assert "R-KERF in force" in caplog.text and "w_b = 8.000" in caplog.text


def test_narrow_kerf_crossing_within_l_ride_is_lifted() -> None:
    """A through kerf 6.5 mm wide (a 6 mm bit with 0.25 mm runout), crossed at right angles by the left wheel."""
    lines = _lines(18.3)
    closed = _index(lines)
    assert any(r.witness is not None for r in _left_track(closed))
    # w_b = 8 mm: the kerf is narrow. The run along the wheel centre's path while its 55 mm disc meets the kerf is
    # 6.5 + 2 x 55 = 116.5 mm, inside the track, below L_ride = 150 mm: lifted, with slack L_ride - run.
    index = _index(lines, kerf_gates(8.0, 150.0, 0.0))
    track = _left_track(index)
    assert all(r.witness is None for r in track)
    crossing = [r for r in track if r.ride_mm > 0.0]
    assert crossing and all(r.lifted for r in crossing)
    for r in crossing:
        assert r.ride_mm == pytest.approx(6.5 + 2 * RHO, abs=1e-6)
        assert r.slack == pytest.approx(150.0 - 116.5, abs=1e-6)
    # A run equal to L_ride is refused like a touch; a longer one too.
    for l_ride in (116.5, 100.0):
        track = _left_track(_index(lines, kerf_gates(8.0, l_ride, 0.0)))
        hits = [r for r in track if r.witness is not None]
        assert hits and "L_ride" in hits[0].witness.hazard
        assert hits[0].witness.slack_mm == pytest.approx(l_ride - 116.5, abs=1e-6)


def test_wide_removal_is_never_crossed() -> None:
    lines = _lines(18.3)
    # w_b = 6 mm: the 6.5 mm kerf is wide. A kerf exactly w_b wide counts as wide (this module's reading).
    for w_b in (6.0, 6.5):
        track = _left_track(_index(lines, kerf_gates(w_b, 500.0, 0.0)))
        hits = [r for r in track if r.witness is not None]
        assert hits, w_b
        assert "wider than w_b" in hits[0].witness.hazard and hits[0].witness.slack_mm <= 0.0
    assert all(r.witness is None for r in _left_track(_index(lines, kerf_gates(6.6, 500.0, 0.0))))
    # A second pass beside the first makes a 12.5 mm slot: wide for w_b = 8 mm, although each pass is narrow.
    extra = ["G0 X800.000 Y606.000", "G1 Z-18.300 F200", "G1 X1600.000 Y606.000 F800", "G0 Z10.000"]
    track = _left_track(_index(_lines(18.3, extra=extra), kerf_gates(8.0, 500.0, 0.0)))
    assert any(r.witness is not None and "wider than w_b" in r.witness.hazard for r in track)


def test_open_run_and_single_piece_queries_never_lift_a_crossing() -> None:
    # The groove ends while the left wheel is still on the kerf: the run reaches the end of the track.
    index = _index(_lines(18.3, groove_end=760.0), kerf_gates(8.0, 500.0, 0.0))
    track = _left_track(index)
    hits = [r for r in track if r.witness is not None]
    assert hits and "end of the wheel track" in hits[-1].witness.hazard
    # One piece alone cannot hold a 116.5 mm run: a single-piece query on the crossing is refused.
    index = _index(_lines(18.3), kerf_gates(8.0, 500.0, 0.0))
    pcs = index.pieces
    crossing = [j for j, r in zip(cut_piece_indices(pcs, _groove_cut(pcs)), _left_track(index)) if r.ride_mm > 0]
    assert crossing
    for j in crossing:
        assert index.wheel_clearance(j, LEFT_C90, RHO).witness is not None
    # A region (a swept disc, an envelope) gets no crossing: only the h_r rule applies to it.
    region = Polygon([(1040.0, 590.0), (1060.0, 590.0), (1060.0, 610.0), (1040.0, 610.0)])
    assert index.clearance(region, len(pcs), 0.0)[1] is not None


def test_kerf_width_enters_the_invariant_and_the_guard() -> None:
    """The opening near a wheel depends on removal within w_b of it, so w_b joins the time invariant's margin."""
    short = FootprintSet(FP.vendor, ((402.0, 0.0), (100.0, 76.0), None), "union")
    assert derive_delta(short, RHO, R_TOOL, DELTA_TOOL, 0.05) == 40.0
    assert derive_delta(short, RHO, R_TOOL, DELTA_TOOL, 0.05, w_b=8.0) == 32.0
    # The spec footprint leaves room for any plausible kerf: Delta stays 50 mm.
    assert derive_delta(FP, RHO, R_TOOL, DELTA_TOOL, 0.05, w_b=8.0) == 50.0
    ctx = context(gates=kerf_gates(8.0, 150.0, 0.0))
    param = delta_for(ctx)
    assert param.value == 50.0 and ctx.params["kerf_width_max"] in param.inputs
    # Pieces of 40 mm suit the short footprint without R-KERF, not with a measured w_b of 8 mm.
    kerf = KerfRelaxation.from_context(ctx)
    pcs = pieces_of(program(_lines(18.3)), 40.0)
    RemovalIndex.build(pcs, R_TOOL, DELTA_TOOL, None, footprint=short, rho=RHO)
    with pytest.raises(InvariantError):
        RemovalIndex.build(pcs, R_TOOL, DELTA_TOOL, None, footprint=short, rho=RHO, kerf=kerf)
    # The query guard: within rho + r_j + w_b of the piece's own path, a crossing query cannot be exact.
    pcs = pieces_of(program(_lines(18.3)))
    plain = RemovalIndex.build(pcs, R_TOOL, DELTA_TOOL, None, footprint=FP, rho=RHO)
    relaxed = RemovalIndex.build(pcs, R_TOOL, DELTA_TOOL, None, footprint=FP, rho=RHO, kerf=kerf)
    j = cut_piece_indices(pcs, 0)[3]
    x0, y0, x1 = float(pcs[j]["x0"]), float(pcs[j]["y0"]), float(pcs[j]["x1"])
    beside = LineString([(x0, y0 + REACH + 1.0), (x1, y0 + REACH + 1.0)])
    plain.query(beside, j, RHO)
    with pytest.raises(InvariantError) as caught:
        relaxed.query(beside, j, RHO)
    assert caught.value.finding.check_id == ids.M_INVARIANT


def test_kerf_relaxation_needs_the_fact() -> None:
    ctx = context(gates=kerf_gates(8.0, 150.0, 0.5))
    kerf = KerfRelaxation.from_context(ctx)
    assert kerf is not None
    assert (kerf.w_b_mm, kerf.l_ride_mm, kerf.h_r_mm) == (8.0, 150.0, 0.5)
    assert {d.id for d in kerf.disclosures} == {"RELAXATION:R-KERF"}
    assert KerfRelaxation.from_context(context()) is None
    # A catalogue default, owner policy or a guess cannot open it: only FACT Params in mm.
    gate_default = context().params["kerf_width_max"]
    with pytest.raises(ValueError):
        KerfRelaxation(gate_default, kerf.l_ride, kerf.h_r)
    with pytest.raises(ValueError):
        KerfRelaxation(Param(8.0, "mm", "POLICY", "a guess"), kerf.l_ride, kerf.h_r)
    with pytest.raises(ValueError):
        KerfRelaxation(Param(-1.0, "mm", "FACT", "fact kerf.crossing w_b"), kerf.l_ride, kerf.h_r)
    with pytest.raises(TypeError):
        KerfRelaxation(8.0, kerf.l_ride, kerf.h_r)  # type: ignore[arg-type]
    # FACT values without the open gate, or the open gate without FACT values: the context contradicts itself.
    closed = context()
    forged = dataclasses.replace(closed, params={**closed.params, "kerf_width_max": kerf.w_b})
    with pytest.raises(ValueError):
        KerfRelaxation.from_context(forged)
    opened = dataclasses.replace(ctx, params={**ctx.params, "kerf_ride_max": closed.params["kerf_ride_max"]})
    with pytest.raises(ValueError):
        KerfRelaxation.from_context(opened)
    # Built from raw FACT Params without a disclosure, the relaxation still discloses itself.
    bare = KerfRelaxation(*(Param(v, "mm", "FACT", "fact kerf.crossing %s" % n)
                            for v, n in ((8.0, "w_b"), (150.0, "L_ride"), (0.5, "h_r"))))
    notes = [d for d in bare.disclosures if d.id == "RELAXATION:R-KERF"]
    assert len(notes) == 1 and notes[0].severity == "amber"
