# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Time pieces (unit P03; the project design, sections 4.4 and 4.7.3; docs/prep/safety-model.md 3.5, 3.6).

Every move is cut into pieces no longer than delta in XY; an arc into sub-arcs
whose chords are no longer than delta and whose sagitta is at most e_max
(0.05 mm), so the arc lies within the piece's chord grown by its sagitta. The
piece index is the time coordinate of every check: pieces are numbered in
program order across cuts and links, and a Z-only move is one piece of zero XY
length.
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from prep_normalise_support import JOB_MM, f, norm, programs, read, true_arc_points

from goliath_prep.ir import Program
from goliath_prep.normalise import normalise
from goliath_prep.pieces import PIECE_DTYPE, PieceArray, pieces

E_MAX = 0.05
REQUIRED_FIELDS = ("idx", "cut", "kind", "x0", "y0", "x1", "y1", "zmin", "sagitta", "src_line")

DELTAS = st.one_of(st.sampled_from([50.0, 25.0, 5.0, 1.0]), st.floats(min_value=0.5, max_value=50.0))


def _program(lines: list[str]) -> Program:
    return normalise(read(lines), JOB_MM)


def test_piece_dtype_has_the_designed_fields() -> None:
    for name in REQUIRED_FIELDS:
        assert name in PIECE_DTYPE.names
    for name in ("link", "move", "z0", "z1", "zmax", "in_material", "part", "parts"):
        assert name in PIECE_DTYPE.names


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(lines=programs(), delta=DELTAS)
def test_piece_length_le_delta(lines: list[str], delta: float) -> None:
    program = _program(lines)
    array = pieces(program, delta)
    data = array.data
    assert data.dtype == PIECE_DTYPE
    length = np.sqrt((data["x1"] - data["x0"]) ** 2 + (data["y1"] - data["y0"]) ** 2)
    assert bool((length <= delta).all()), float(length.max())
    # The time coordinate: 0, 1, 2, ... in program order.
    assert list(data["idx"]) == list(range(len(data)))
    assert list(data["move"]) == sorted(data["move"])
    # Each located move is covered by its pieces, end to end, from its start to its end.
    for move in program.moves:
        mine = data[data["move"] == move.index]
        if not move.located:
            assert len(mine) == 0 and move.index in array.unplaced
            continue
        assert len(mine) >= 1
        assert list(mine["part"]) == list(range(1, len(mine) + 1)) and set(mine["parts"]) == {len(mine)}
        assert (mine["x0"][0], mine["y0"][0]) == (f(move.a[0]), f(move.a[1]))
        assert (mine["x1"][-1], mine["y1"][-1]) == (f(move.b[0]), f(move.b[1]))
        assert bool((mine["x1"][:-1] == mine["x0"][1:]).all() and (mine["y1"][:-1] == mine["y0"][1:]).all())
        assert set(mine["src_line"]) == {move.src.line}
        assert set(mine["kind"]) == {move.kind}
        assert bool((mine["zmin"] <= mine["zmax"]).all())
        if not move.is_arc:
            assert set(mine["sagitta"]) == {0.0}
        # A piece is in material only inside an in-material move.
        if not move.in_material:
            assert not mine["in_material"].any()
    # Cut and link indices agree with the program.
    for cut in program.cuts:
        for move in cut.moves:
            mine = data[data["move"] == move.index]
            assert set(mine["cut"]) <= {cut.id} and set(mine["link"]) <= {-1}
    for link in program.links:
        for move in link.moves:
            mine = data[data["move"] == move.index]
            assert set(mine["link"]) <= {link.id} and set(mine["cut"]) <= {-1}


def _assert_arc_pieces_bound(program: Program, array: PieceArray) -> int:
    data = array.data
    checked = 0
    for move in program.moves:
        if not move.is_arc:
            continue
        mine = data[data["move"] == move.index]
        assert bool((mine["sagitta"] <= E_MAX).all()) and bool((mine["sagitta"] > 0).all())
        samples = 64 * len(mine) + 1
        truth = true_arc_points((f(move.a[0]), f(move.a[1]), f(move.a[2])), (f(move.b[0]), f(move.b[1]),
                                f(move.b[2])), (f(move.centre[0]), f(move.centre[1])), move.kind == "arc_cw",
                                move.sweep_deg, samples)
        for k, piece in enumerate(mine):
            lo, hi = k * 64, (k + 1) * 64
            seg = truth[lo:hi + 1, :2]
            ax, ay, bx, by = piece["x0"], piece["y0"], piece["x1"], piece["y1"]
            dx, dy = bx - ax, by - ay
            denom = dx * dx + dy * dy
            t = np.clip(((seg[:, 0] - ax) * dx + (seg[:, 1] - ay) * dy) / denom, 0.0, 1.0)
            dist = np.sqrt((seg[:, 0] - (ax + t * dx)) ** 2 + (seg[:, 1] - (ay + t * dy)) ** 2)
            assert float(dist.max()) <= piece["sagitta"] + 1e-9, (move, k, float(dist.max()), piece["sagitta"])
            checked += 1
    return checked


def test_sagitta_bound() -> None:
    lines = ["G21", "G0 Z5.000", "G0 X1000.000 Y500.000", "G1 Z-3.000 F200",
             "G3 X1000.000 Y500.000 I400.000 J0.000 F600",   # a full circle of radius 400: 4 parts
             "G2 X1010.000 Y500.000 I5.000 J0.000",          # 180 degrees, radius 5
             "G3 X1010.300 Y500.000 I0.150 J0.000",          # a tiny half circle
             "G2 X1310.300 Y800.000 I300.000 J0.000",        # 90 degrees, radius 300
             "G0 Z5.000"]
    program = _program(lines)
    for delta in (50.0, 10.0, 1.0):
        array = pieces(program, delta)
        assert _assert_arc_pieces_bound(program, array) > 0
        assert array.e_max.value == E_MAX and array.e_max.ref.endswith("check_chord_tol")
        assert array.delta_mm == delta


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(lines=programs(), delta=DELTAS)
def test_sagitta_bound_random_programs(lines: list[str], delta: float) -> None:
    program = _program(lines)
    _assert_arc_pieces_bound(program, pieces(program, delta))


def test_in_material_per_piece_is_exact() -> None:
    lines = ["G21", "G0 Z5.000", "G0 X100.000 Y100.000",
             "G1 Z0.000 F200",                    # 4 link
             "G1 X300.000 Z-0.020",               # 5 link: its lowest point is exactly -0.02
             "G1 X500.000 Z-3.000",               # 6 cut: a ramp from -0.02 to -3 over 200 mm
             "G1 X700.000 Z1.000",                # 7 cut: a ramp out, from -3 to +1
             "G0 Z5.000"]
    program = _program(lines)
    data = pieces(program, 50.0).data
    ramp_in = data[data["src_line"] == 6]
    assert list(ramp_in["zmin"]) == [-0.765, -1.51, -2.255, -3.0]
    assert list(ramp_in["zmax"]) == [-0.02, -0.765, -1.51, -2.255]
    assert ramp_in["in_material"].all()
    ramp_out = data[data["src_line"] == 7]
    assert list(ramp_out["zmin"]) == [-3.0, -2.0, -1.0, 0.0]
    assert list(ramp_out["zmax"]) == [-2.0, -1.0, 0.0, 1.0]
    assert list(ramp_out["in_material"]) == [True, True, True, False]
    assert not data[data["src_line"] == 5]["in_material"].any()
    assert set(data[data["src_line"] == 5]["cut"]) == {-1}


def test_z_only_and_zero_length_moves_are_one_piece_of_zero_length() -> None:
    lines = ["M05", "G01 Z10.000 F300", "G00 X100.000 Y100.000 C90", "Z10.000", "G00 X100.000 Y100.000 C120",
             "Z10.000", "M10", "G01 Z-3.000 F200 S18000", "G01 Z10.000", "M05"]
    from goliath_prep.gcode import read_gcode

    result = read_gcode("\n".join(lines) + "\n", flavour="dialect", job=JOB_MM)
    assert not result.refused
    program = normalise(result, JOB_MM)
    array = pieces(program, 50.0)
    data = array.data
    for line in (4, 5, 6, 8, 9):
        (piece,) = data[data["src_line"] == line]
        assert (piece["x0"], piece["y0"]) == (piece["x1"], piece["y1"])
    rotation = [m for m in program.moves if m.src.line == 5][0]
    assert rotation.c_start == 90 and rotation.c_end == 120 and rotation.located
    assert array.unplaced == (0, 1)


def test_piece_array_is_read_only_and_behaves_like_the_array() -> None:
    program = norm(["G21", "G0 Z5.000", "G0 X100.000 Y100.000", "G1 Z-2.000 F100", "G1 X260.000", "G0 Z5.000"])
    array = pieces(program, 50.0)
    assert len(array) == len(array.data) == 6  # the plunge, four pieces of 40 mm, the retract
    assert array["x0"].tolist() == array.data["x0"].tolist()
    with pytest.raises(ValueError):
        array.data["x0"][0] = 1.0
    with pytest.raises(ValueError):
        array["x0"][0] = 1.0
    cut_pieces = array.data[array.data["cut"] == 0]
    assert [round(float(x), 3) for x in cut_pieces["x1"]] == [100.0, 140.0, 180.0, 220.0, 260.0, 260.0]


@pytest.mark.parametrize("delta", [0.0, -1.0, float("nan"), float("inf"), 50.0001, True, "50"])
def test_delta_must_be_a_positive_length_at_most_50(delta: object) -> None:
    program = norm(["G21", "G0 Z5.000", "G0 X100.000 Y100.000", "G1 Z-2.000 F100", "G0 Z5.000"])
    with pytest.raises((ValueError, TypeError)):
        pieces(program, delta)  # type: ignore[arg-type]


def test_pieces_needs_a_program() -> None:
    with pytest.raises(TypeError):
        pieces("G1 X1", 50.0)  # type: ignore[arg-type]


def test_arc_piece_count_is_the_smallest_that_meets_both_bounds() -> None:
    """Large radius: the sagitta decides; small radius: the chord decides (minimal and deterministic)."""
    program = _program(["G21", "G0 Z5.000", "G0 X1000.000 Y500.000", "G1 Z-3.000 F200",
                        "G2 X1800.000 Y1300.000 I800.000 J0.000 F600", "G0 Z5.000"])
    data = pieces(program, 50.0).data
    arc = data[data["src_line"] == 5]
    n = len(arc)
    r = 800.0
    step = math.radians(90.0 / n)
    assert 2 * r * math.sin(step / 4) ** 2 <= E_MAX
    wider = math.radians(90.0 / (n - 1))
    assert 2 * r * math.sin(wider / 4) ** 2 > E_MAX - 2e-9
