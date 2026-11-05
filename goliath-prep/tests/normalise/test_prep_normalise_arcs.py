# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Arcs in the normaliser: helices, arcs of 180 degrees and more, full circles, sweeps (unit P03).

The project design, sections 4.2, 4.4 and 4.12.2; docs/prep/safety-model.md
section 3.5; red-team PHYS-16. Helical arcs become G01 segments within
0.02 mm; arcs of 180 degrees or more and full circles are split into parts of
at most 90 degrees, so a three-point direction test can never be degenerate;
the reader's full-circle rule (end equal to start at file precision is 360
degrees) is applied as the reader decided it; every arc's sweep is recorded.
New points lie on the 0.001 mm grid the robot's program is written with.
"""
from __future__ import annotations

import math
from decimal import Decimal

import numpy as np
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from prep_normalise_support import (
    JOB_MM,
    distance_to_polyline,
    f,
    note_lines,
    notes_of,
    on_grid,
    programs,
    read,
    true_arc_points,
)

from goliath_prep.core.canon import canonical_json
from goliath_prep.ir import Move, Program
from goliath_prep.normalise import normalise

D = Decimal
START = ["G21", "G0 Z5.000", "G0 X1000.000 Y500.000", "M3 S18000", "G1 Z-3.000 F200"]  # lines 1-5


def _moves_of_line(program: Program, line: int) -> list[Move]:
    return [m for m in program.moves if m.src.line == line]


def _xyz(point: tuple) -> tuple[float, float, float]:
    return (f(point[0]), f(point[1]), f(point[2]))


# ---------------------------------------------------------------------------
# Helices
# ---------------------------------------------------------------------------


def _assert_helix(program: Program, line: int, raw_move, tol: float = 0.02) -> list[Move]:
    segments = _moves_of_line(program, line)
    assert len(segments) >= 2
    assert all(s.kind == "feed" and s.origin == "helix-segment" for s in segments)
    assert [s.part for s in segments] == [(k + 1, len(segments)) for k in range(len(segments))]
    # The ends are the arc's own, exactly; every new point is on the 0.001 grid.
    assert segments[0].a == raw_move.start and segments[-1].b == raw_move.end
    for left, right in zip(segments, segments[1:]):
        assert left.b == right.a
        assert all(on_grid(v) for v in left.b)
    vertices = np.array([_xyz(segments[0].a)] + [_xyz(s.b) for s in segments])
    truth = true_arc_points(_xyz(raw_move.start), _xyz(raw_move.end), (f(raw_move.centre[0]), f(raw_move.centre[1])),
                            raw_move.kind == "arc_cw", raw_move.sweep_deg, 4001)
    assert float(distance_to_polyline(truth, vertices).max()) <= tol + 1e-9
    return segments


def test_helix_linearised_within_0p02() -> None:
    lines = [*START,
             "G2 X1000.000 Y500.000 Z-6.000 I0.000 J50.000 F600",   # 6: a full-circle helix, r = 50
             "G3 X1300.000 Y800.000 Z-9.000 I300.000 J0.000",       # 7: a quarter helix, r = 300
             "G0 Z5.000"]
    result = read(lines)
    program = normalise(result, JOB_MM)
    raw = {m.src.line: m for m in result.program_raw.moves}
    full = _assert_helix(program, 6, raw[6])
    quarter = _assert_helix(program, 7, raw[7])
    assert all(s.in_material for s in full + quarter)
    assert note_lines(program, "helix") == {6, 7}
    (note,) = notes_of(program, "helix")
    assert "0.02" in note.text
    records = {r.src.line: r for r in program.arcs}
    assert records[6].treatment == "linearised" and records[6].full_circle and records[6].sweep_deg == 360.0
    assert records[6].helical and records[6].parts == len(full)
    assert records[7].treatment == "linearised" and records[7].parts == len(quarter)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(radius_um=st.integers(min_value=50, max_value=900_000), sweep=st.integers(min_value=1, max_value=360),
       start=st.integers(min_value=0, max_value=359), dz_um=st.integers(min_value=-20_000, max_value=20_000),
       cw=st.booleans())
def test_helix_property_every_point_within_0p02(radius_um: int, sweep: int, start: int, dz_um: int,
                                                 cw: bool) -> None:
    if dz_um == 0:
        dz_um = 1000
    x0, y0 = 1_200_000, 600_000
    a0 = math.radians(start)
    i_um, j_um = -round(radius_um * math.cos(a0)), -round(radius_um * math.sin(a0))
    if i_um == 0 and j_um == 0:
        i_um = radius_um
    cx, cy = x0 + i_um, y0 + j_um
    r = math.hypot(x0 - cx, y0 - cy)
    if sweep == 360:
        ex, ey = x0, y0
    else:
        a_end = math.atan2(y0 - cy, x0 - cx) + math.radians(sweep) * (-1.0 if cw else 1.0)
        ex, ey = round(cx + r * math.cos(a_end)), round(cy + r * math.sin(a_end))
        if math.hypot(ex - x0, ey - y0) < 10 or abs(math.hypot(ex - cx, ey - cy) - r) > 4:
            return
    lines = ["G21", "G0 Z5.000", "G0 X%.3f Y%.3f" % (x0 / 1000, y0 / 1000), "G1 Z-1.000 F100",
             "%s X%.3f Y%.3f Z%.3f I%.3f J%.3f F600" % ("G2" if cw else "G3", ex / 1000, ey / 1000,
                                                       (-1000 + dz_um) / 1000, i_um / 1000, j_um / 1000),
             "G0 Z5.000"]
    result = read(lines)
    program = normalise(result, JOB_MM)
    raw = result.program_raw.move_at(5)
    segments = _moves_of_line(program, 5)
    if len(segments) == 1:  # short enough to be one chord
        (segment,) = segments
        assert segment.a == raw.start and segment.b == raw.end
        vertices = np.array([_xyz(segment.a), _xyz(segment.b)])
        truth = true_arc_points(_xyz(raw.start), _xyz(raw.end), (f(raw.centre[0]), f(raw.centre[1])), cw,
                                raw.sweep_deg, 2001)
        assert float(distance_to_polyline(truth, vertices).max()) <= 0.02 + 1e-9
    else:
        _assert_helix(program, 5, raw)


# ---------------------------------------------------------------------------
# Arcs of 180 degrees and more, full circles
# ---------------------------------------------------------------------------

ARCS = [*START,
        "G3 X1000.000 Y500.000 I50.000 J0.000 F600",  # 6: full circle ccw about (1050, 500)
        "G2 X1100.000 Y500.000 I50.000 J0.000",       # 7: 180 degrees cw (from 1000,500 to 1100,500)
        "G3 X1150.000 Y550.000 I0.000 J50.000",       # 8: 90 degrees ccw: kept
        "G2 X1100.000 Y500.000 I0.000 J-50.000",      # 9: 270 degrees cw about (1150, 500)
        "G3 X1150.000 Y500.000 I25.000 J0.000",       # 10: 180 degrees ccw about (1125, 500), r = 25
        "G0 Z5.000"]


def _cross_sign(move: Move) -> float:
    cx, cy = f(move.centre[0]), f(move.centre[1])
    ax, ay = f(move.a[0]) - cx, f(move.a[1]) - cy
    bx, by = f(move.b[0]) - cx, f(move.b[1]) - cy
    return ax * by - ay * bx


def test_arc_over_180_and_full_circle_split() -> None:
    result = read(ARCS)
    program = normalise(result, JOB_MM)
    raw = {m.src.line: m for m in result.program_raw.moves}
    assert [raw[n].sweep_deg for n in (6, 7, 8, 9, 10)] == [360.0, 180.0, 90.0, 270.0, 180.0]
    expected_parts = {6: 4, 7: 2, 8: 1, 9: 3, 10: 2}
    for line, count in expected_parts.items():
        parts = _moves_of_line(program, line)
        assert len(parts) == count, (line, parts)
        assert all(p.kind == raw[line].kind for p in parts)
        assert all(p.centre == raw[line].centre for p in parts)
        assert parts[0].a == raw[line].start and parts[-1].b == raw[line].end
        for left, right in zip(parts, parts[1:]):
            assert left.b == right.a and on_grid(left.b[0]) and on_grid(left.b[1])
        total = 0.0
        for part in parts:
            assert 0.0 < part.sweep_deg <= 90.0
            total += part.sweep_deg
            # No part is degenerate for a three-point direction test, and each turns the way the source does.
            sign = _cross_sign(part)
            assert abs(sign) > 1.0 and (sign > 0) == (part.kind == "arc_ccw"), (line, part)
            r0, r1 = part.radius
            source_r = math.hypot(f(raw[line].start[0]) - f(raw[line].centre[0]),
                                  f(raw[line].start[1]) - f(raw[line].centre[1]))
            assert abs(r0 - r1) <= 0.005 and abs(r0 - source_r) <= 0.001
        assert math.isclose(total, raw[line].sweep_deg, abs_tol=1e-9)
        if count > 1:
            assert [p.part for p in parts] == [(k + 1, count) for k in range(count)]
            assert all(p.origin == "arc-part" for p in parts)
        else:
            assert parts[0].origin == "as-read" and parts[0].part == (1, 1)
    assert note_lines(program, "full-circle-split") == {6}
    assert note_lines(program, "arc-split") == {7, 9, 10}
    assert not (note_lines(program, "arc-split") & {8})
    # The quarter points of the full circle are the circle's own, on the grid.
    assert [p.b[:2] for p in _moves_of_line(program, 6)] == [
        (D("1050.000"), D("450.000")), (D("1100.000"), D("500.000")), (D("1050.000"), D("550.000")),
        (D("1000.000"), D("500.000"))]


def test_arc_sweep_angles_recorded() -> None:
    result = read(ARCS)
    program = normalise(result, JOB_MM)
    raw = [m for m in result.program_raw.moves if m.is_arc]
    assert len(program.arcs) == len(raw) == 5
    for index, (record, source) in enumerate(zip(program.arcs, raw)):
        assert record.index == index
        assert record.src == source.src
        assert record.sweep_deg == source.sweep_deg
        assert record.full_circle == source.full_circle
        assert record.direction == ("cw" if source.kind == "arc_cw" else "ccw")
        assert record.cut == 0 and not record.helical
        moves = [program.moves[i] for i in record.moves]
        assert all(m.arc == index for m in moves)
        assert record.parts == len(moves)
        assert math.isclose(math.fsum(m.sweep_deg for m in moves), record.sweep_deg, abs_tol=1e-9)
        assert record.treatment == ("kept" if record.parts == 1 else "split")
        data = record.to_json()
        assert data["sweep_deg"] == record.sweep_deg and data["line"] == source.src.line
        canonical_json(data)
    # A move that is not an arc points at no record.
    assert all(m.arc is None for m in program.moves if not m.is_arc and m.origin == "as-read")


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(lines=programs())
def test_every_arc_has_exactly_one_record_and_full_circles_stay_full(lines: list[str]) -> None:
    result = read(lines)
    program = normalise(result, JOB_MM)
    raw_arcs = [m for m in result.program_raw.moves if m.is_arc]
    assert [r.src.line for r in program.arcs] == [m.src.line for m in raw_arcs]
    for record, source in zip(program.arcs, raw_arcs):
        assert record.full_circle == source.full_circle
        assert record.sweep_deg == source.sweep_deg
        if source.full_circle and not record.helical:
            assert record.parts == 4
        if not record.helical:
            moves = [program.moves[i] for i in record.moves]
            assert all(m.sweep_deg < 180.0 for m in moves)


def test_split_decided_exactly_and_near_collinear_arcs_split_too() -> None:
    """Pinned reading: 180 degrees exactly is split, decided on the decimals, and so is an arc whose centre lies
    within 0.01 mm of the line through its ends (rounding could turn a three-point test there)."""
    lines = [*START,
             "G3 X1060.000 Y580.000 I30.000 J40.000 F600",   # 6: exactly 180 degrees, diameter not on an axis
             "G3 X960.000 Y580.009 I-50.000 J0.000",         # 7: 179.99 degrees: centre 0.0045 mm off the chord
             "G1 X1500.000 Y500.000",                        # 8
             "G3 X1301.519 Y517.365 I-100.000 J0.000",       # 9: 170 degrees: kept
             "G0 Z5.000"]
    result = read(lines)
    program = normalise(result, JOB_MM)
    raw = {m.src.line: m for m in result.program_raw.moves}
    assert raw[7].sweep_deg < 180.0 and raw[9].sweep_deg < 180.0
    assert len(_moves_of_line(program, 6)) == 2
    assert len(_moves_of_line(program, 7)) == 2
    assert len(_moves_of_line(program, 9)) == 1
    assert note_lines(program, "arc-split") == {6, 7}
    records = {r.src.line: r.treatment for r in program.arcs}
    assert records == {6: "split", 7: "split", 9: "kept"}


def test_helix_entering_from_above_starts_its_cut_where_it_enters_the_material() -> None:
    """Segmentation runs on the normalised moves: the helix's segments above the surface belong to the link."""
    program = normalise(read(["G21", "G0 Z5.000", "G0 X1000.000 Y500.000", "G1 Z1.000 F200",
                              "G2 X1000.000 Y500.000 Z-3.000 I50.000 J0.000 F600", "G0 Z5.000"]), JOB_MM)
    segments = _moves_of_line(program, 5)
    entering = [s.in_material for s in segments].index(True)
    assert entering > 0
    (cut,) = program.cuts
    assert cut.moves[0] is segments[entering]
    assert all(s in program.links[0].moves for s in segments[:entering])
    assert 5 in note_lines(program, "feed-link")
    assert program.arcs[0].cut == 0
