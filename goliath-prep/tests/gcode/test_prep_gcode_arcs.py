# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Arcs in the strict reader (unit P02; red-team PHYS-16; docs/prep/gi-format.md section 8.7).

An arc whose end equals its start at file precision is a full circle, never an
empty arc; 0 < |end - start| < 0.01 mm is refused as ambiguous; start and end
radii may differ by at most 0.005 mm. The comparisons are exact, on the
decimals as written: the property tests build ends exactly on either side of
each boundary.
"""
from __future__ import annotations

import math
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from prep_gcode_support import assert_clean, assert_refused, read_generic

D = Decimal
START = ["G0 X1200.000 Y500.000 Z5.000", "G1 Z-3.000 F200"]


def _arc(line: str):
    result = read_generic([*START, line])
    return result, result.program_raw.move_at(3)


def test_arc_end_equal_start_full_circle_and_near_equal_refused() -> None:
    """Red-team PHYS-16 and RT30: end == start is 360 degrees; 0.004 mm away is ambiguous and refused."""
    for line in ("G2 X1200.000 Y500.000 I0.000 J50.000", "G2 X1200 Y500.0 I0 J50", "G3 I0.000 J50.000",
                 "G3 J50.000", "G2 X1200.0000 I-50.000"):
        result, move = _arc(line)
        assert_clean(result)
        assert move.full_circle and move.sweep_deg == 360.0, line
        assert move.end[:2] == move.start[:2]
    for dx in ("0.004", "0.009", "0.0099999", "0.001"):
        result, _ = _arc("G2 X%s Y500.000 I0.000 J50.000" % (D("1200") + D(dx)))
        finding = assert_refused(result, "D-ARC", 3)
        assert "ambiguous" in finding.message
    result, move = _arc("G2 X1200.010 Y500.000 I0.000 J50.000")
    assert_clean(result)
    assert not move.full_circle and math.isclose(move.sweep_deg, 360 - math.degrees(0.01 / 50), abs_tol=1e-4)
    result, move = _arc("G3 X1200.010 Y500.000 I0.000 J50.000")
    assert_clean(result)
    assert math.isclose(move.sweep_deg, math.degrees(0.01 / 50), abs_tol=1e-4)


@pytest.mark.parametrize(
    "line,why",
    [
        ("G2 X1250.000 Y550.000", "neither I nor J"),
        ("G2 X1200.000 Y500.000 I0.000 J0.000", "zero radius"),
        ("G2 X1250.000 Y550.000 I0.000 J50.010", "radii differ by 0.010"),
        ("G2 X1250.006 Y550.000 I0.000 J50.000", "radii differ by 0.006"),
    ],
)
def test_arcs_refused(line: str, why: str) -> None:
    result, _ = _arc(line)
    assert_refused(result, "D-ARC", 3)


def test_arc_from_an_unknown_position_is_refused() -> None:
    result = read_generic(["G2 X10.000 Y0.000 I5.000 J0.000"])
    finding = assert_refused(result, "D-ARC", 1)
    assert "unknown" in finding.message


def test_radius_tolerance_boundary_is_exact() -> None:
    # Start radius 50 (J50); an end on the X axis through the centre at 50.005 is exactly at the limit.
    result, move = _arc("G2 X1250.005 Y550.000 I0.000 J50.000")
    assert_clean(result)
    assert move.sweep_deg is not None and math.isclose(move.sweep_deg, 270.0, abs_tol=0.01)
    result, _ = _arc("G2 X1250.0051 Y550.000 I0.000 J50.000")
    assert_refused(result, "D-ARC", 3)


def test_sweeps_and_helix() -> None:
    result, move = _arc("G2 X1250.000 Y550.000 I0.000 J50.000")
    assert move.kind == "arc_cw" and math.isclose(move.sweep_deg, 270.0, abs_tol=1e-9)
    assert move.centre == (D("1200.000"), D("550.000"))
    result, move = _arc("G3 X1250.000 Y550.000 I0.000 J50.000")
    assert move.kind == "arc_ccw" and math.isclose(move.sweep_deg, 90.0, abs_tol=1e-9)
    result, move = _arc("G2 I0.000 J50.000 Z-6.000")
    assert_clean(result)
    assert move.full_circle and move.start[2] == D("-3.000") and move.end[2] == D("-6.000")


offsets = st.decimals(min_value=D("-0.02"), max_value=D("0.02"), places=4, allow_nan=False, allow_infinity=False)


@settings(max_examples=300, deadline=None)
@given(dx=offsets, dy=offsets, ccw=st.booleans())
def test_end_versus_start_rule_holds_exactly(dx: Decimal, dy: Decimal, ccw: bool) -> None:
    """For any end near the start: equal is a full circle, < 0.01 mm refused, >= 0.01 mm an ordinary arc."""
    end_x = D("1200.000") + dx
    end_y = D("500.000") + dy
    line = "G%d X%s Y%s I0.000 J50.000" % (3 if ccw else 2, end_x, end_y)
    result, move = _arc(line)
    distance_sq = dx * dx + dy * dy
    radius_gap = abs(math.hypot(float(dx), float(dy) - 50.0) - 50.0)
    if distance_sq == 0:
        assert_clean(result)
        assert move.full_circle and move.sweep_deg == 360.0
    elif distance_sq < D("0.0001"):
        assert_refused(result, "D-ARC", 3)
        assert move is None
    elif radius_gap > 0.005 + 1e-9:
        assert_refused(result, "D-ARC", 3)
    elif radius_gap < 0.005 - 1e-9:
        assert_clean(result)
        assert not move.full_circle and 0.0 < move.sweep_deg < 360.0


@settings(max_examples=200, deadline=None)
@given(angle=st.floats(min_value=0.5, max_value=359.5), ccw=st.booleans())
def test_sweep_of_points_on_the_circle(angle: float, ccw: bool) -> None:
    """The sweep is measured from the start to the end about the centre, in the arc's direction."""
    radius = 50.0
    cx, cy = 1200.0, 550.0  # start (1200, 500) is at -90 degrees
    theta = math.radians(-90.0 + angle)
    end_x = D("%.4f" % (cx + radius * math.cos(theta)))
    end_y = D("%.4f" % (cy + radius * math.sin(theta)))
    result, move = _arc("G%d X%s Y%s I0.000 J50.000" % (3 if ccw else 2, end_x, end_y))
    assert_clean(result)
    expected = angle if ccw else 360.0 - angle
    assert math.isclose(move.sweep_deg, expected, abs_tol=0.01), (move.sweep_deg, expected)
