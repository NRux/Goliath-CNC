# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Differential and property tests: heading_vendor against the literal transcription of spec 08.

The comparison is bit for bit: every double the production module returns
must have the same IEEE-754 bit pattern as the transcription's (an int 0 in
the transcription counts as +0.0, which is what float(0) is).

The inputs are built to land on the boundaries where exact arithmetic
decides: tool points whose supports fall exactly on a panel edge or a tower
zone edge (within the rounding of the footprint maths), points on the edges
themselves, tied configurations, blocked sides, forced headings on and off
the grid.
"""
from __future__ import annotations

import math
import struct
from types import ModuleType

import pytest
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st

from goliath_prep.heading_vendor import candidate_table, evaluate, select_heading_vendor

SIDES = ("left", "right", "bottom", "top")
GRID = tuple(range(0, 361, 10))
SUPPORTS = ((402.0, 0.0), (211.0, 76.0), (211.0, -76.0))

SETTINGS = settings(
    max_examples=400,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


def bits(value: float) -> bytes:
    return struct.pack("<d", float(value))


def offset(i: int, k: int) -> tuple[float, float]:
    """r·u(θ+δ) for support k at candidate i, computed as spec 08 states it (input generation only)."""
    r, delta = SUPPORTS[k]
    alpha = float((i + 90) % 360) + delta
    return (r * math.cos(alpha * math.pi / 180), r * math.sin(alpha * math.pi / 180))


@st.composite
def length(draw: st.DrawFn) -> float:
    return draw(
        st.one_of(
            st.sampled_from([150.0, 182.8, 300.0, 402.0, 500.0, 804.0]),
            st.sampled_from([1220.0, 1250.0, 1530.0, 2440.0, 2500.0, 3050.0]),
            st.floats(min_value=1.0, max_value=4000.0, allow_nan=False, allow_infinity=False),
            st.floats(min_value=800.0, max_value=3100.0, allow_nan=False, allow_infinity=False),
        )
    )


@st.composite
def coordinate(draw: st.DrawFn, extent: float) -> float:
    return draw(
        st.one_of(
            st.sampled_from([0.0, -0.0, extent, 75.0, 150.0, extent - 150.0, extent - 75.0, extent / 2]),
            st.floats(min_value=-500.0, max_value=extent + 500.0, allow_nan=False, allow_infinity=False),
            st.integers(min_value=-5, max_value=int(extent) + 5).map(float),
        )
    )


@st.composite
def boundary_target(draw: st.DrawFn, W: float, H: float) -> tuple[float, float]:
    """A point on a panel edge or a tower-zone edge, where exact comparisons decide."""
    t = draw(st.sampled_from([0.0, 0.25, 0.5, 0.75, 1.0]) | st.floats(min_value=0.0, max_value=1.0))
    along_x, along_y = t * W, t * H
    leg = t * 150.0
    return draw(
        st.sampled_from(
            [
                (0.0, along_y),  # left edge
                (W, along_y),  # right edge
                (along_x, 0.0),  # bottom edge
                (along_x, H),  # top edge
                (leg, 150.0 - leg),  # Z1 hypotenuse
                (0.0, H - 150.0 + leg),  # Z2 (top left) edge x = 0
                (leg, H),  # Z2 (top left) edge y = H
                (leg, H - 150.0 + leg),  # Z2 (top left) hypotenuse
                (W - 150.0 + leg, 0.0),  # Z2 (bottom right) edge y = 0
                (W, leg),  # Z2 (bottom right) edge x = W
                (W - 150.0 + leg, leg),  # Z2 (bottom right) hypotenuse
            ]
        )
    )


@st.composite
def tool_point(draw: st.DrawFn, W: float, H: float) -> tuple[float, float]:
    kind = draw(st.sampled_from(["inside", "free", "support-on-boundary", "support-on-boundary", "on-boundary"]))
    if kind == "inside":
        x = draw(st.floats(min_value=0.0, max_value=W, allow_nan=False, allow_infinity=False))
        return (x, draw(st.floats(min_value=0.0, max_value=H, allow_nan=False, allow_infinity=False)))
    if kind == "free":
        return (draw(coordinate(W)), draw(coordinate(H)))
    target = draw(boundary_target(W, H))
    if kind == "on-boundary":
        return target
    i = draw(st.sampled_from(GRID))
    k = draw(st.sampled_from([0, 1, 2]))
    dx, dy = offset(i, k)
    return (target[0] + dx, target[1] + dy)


@st.composite
def scenario(draw: st.DrawFn) -> dict:
    W = draw(length())
    H = draw(length())
    small = st.integers(min_value=0, max_value=3)
    n = draw(st.one_of(small, small, small, st.integers(min_value=0, max_value=40)))
    P = [draw(tool_point(W, H)) for _ in range(n)]
    unforced = st.just(-1)
    off_grid = st.integers(min_value=-30, max_value=400)
    forced = draw(st.one_of(unforced, unforced, unforced, st.sampled_from(GRID), off_grid))
    return {
        "P": P,
        "W": W,
        "H": H,
        "B": draw(st.frozensets(st.sampled_from(SIDES))),
        "tower2": draw(st.sampled_from([0, 1])),
        "F": forced,
    }


def _assert_same_result(result, expected) -> None:
    if not isinstance(expected, tuple):
        assert math.isnan(expected)
        assert math.isnan(result.theta)
        assert (result.c, result.candidate, result.pass_, result.overhang) == (None, None, None, {})
        return
    theta, overhang = expected
    assert bits(result.theta) == bits(theta)
    assert list(result.overhang) == list(overhang) == list(SIDES)
    for side in SIDES:
        assert bits(result.overhang[side]) == bits(overhang[side]), side
    # The C the vendor writes, from θ (spec 08 §Emitting the C word).
    assert result.c == int((theta - 90 + 360) % 360) == result.candidate % 360


# Pinned cases: the exact ties, the 0/360 tie, the summation-order case, an empty P.
@example(case={"P": [(150.0, 150.0)], "W": 300.0, "H": 300.0, "B": frozenset(), "tower2": 0, "F": -1})
@example(case={"P": [(210.0, 200.0)], "W": 300.0, "H": 300.0, "B": frozenset(), "tower2": 0, "F": -1})
@example(case={"P": [(91.4, 177.6), (91.4, -16.9)], "W": 182.8, "H": 184.0, "B": frozenset(), "tower2": 1, "F": -1})
@example(case={"P": [(0.0, 610.0)], "W": 2440.0, "H": 1220.0, "B": frozenset(), "tower2": 0, "F": -1})
@example(case={"P": [], "W": 2440.0, "H": 1220.0, "B": frozenset(SIDES), "tower2": 1, "F": 360})
@given(case=scenario())
@SETTINGS
def test_differential_vs_literal_transcription(literal: ModuleType, case: dict) -> None:
    args = (case["P"], case["W"], case["H"], case["B"], case["tower2"], case["F"])
    result = select_heading_vendor(*args)
    _assert_same_result(result, literal.select_heading(*args))
    event("pass %s, %s" % (result.pass_, "unforced" if case["F"] == -1 else "forced"))
    on_edge = hits = 0
    for i in GRID:
        theta, box, hit = evaluate(i, case["P"], case["W"], case["H"], case["tower2"])
        l_theta, l_box, l_hit = literal.evaluate(i, case["P"], case["W"], case["H"], case["tower2"])
        assert bits(theta) == bits(l_theta)
        assert [bits(v) for v in box] == [bits(v) for v in l_box]
        assert hit is bool(l_hit)
        hits += hit
        on_edge += case["P"] != [] and (box[0] == 0 or box[1] == case["W"] or box[2] == 0 or box[3] == case["H"])
    event("some candidate has a support exactly on a panel edge: %s" % (on_edge > 0))
    event("some candidate hits a tower zone: %s" % (hits > 0))


@given(case=scenario())
@SETTINGS
def test_triangle_test_matches_literal(literal: ModuleType, case: dict) -> None:
    from goliath_prep.heading_vendor import tower_zones, triangle_contains

    W, H = case["W"], case["H"]
    ours = tower_zones(W, H, case["tower2"])
    theirs = literal.zones(W, H, case["tower2"])
    for q in case["P"]:
        for z_ours, z_theirs in zip(ours, theirs):
            assert triangle_contains(q, z_ours) is literal.inside(q, z_theirs)


@given(case=scenario())
@SETTINGS
def test_unforced_never_picks_360_and_ties_go_to_lower_c(case: dict) -> None:
    args = (case["P"], case["W"], case["H"], case["B"], case["tower2"], -1)
    result = select_heading_vendor(*args)
    assert result.candidate != 360
    rows = candidate_table(*args)
    if result.pass_ == 1:
        first = next(row for row in rows if row.fits_panel)
        assert result.candidate == first.candidate
        assert result.overhang == dict.fromkeys(SIDES, 0.0)
    elif result.pass_ == 2:
        assert not any(row.fits_panel for row in rows)
        survivors = [row for row in rows if row.pass2_survivor]
        best = min((row.sides_overhung, row.total_overhang) for row in survivors)
        tied = [row.candidate for row in survivors if (row.sides_overhung, row.total_overhang) == best]
        assert result.candidate == tied[0] == min(tied)
        assert all(result.overhang[side] == 0.0 for side in case["B"])
        assert 1 <= sum(1 for v in result.overhang.values() if v > 0) <= 4
    else:
        assert not any(row.fits_panel or row.pass2_survivor for row in rows)


@given(case=scenario(), forced=st.sampled_from(GRID))
@SETTINGS
def test_forced_on_grid_is_that_candidate_or_nan(case: dict, forced: int) -> None:
    result = select_heading_vendor(case["P"], case["W"], case["H"], case["B"], case["tower2"], forced)
    assert result.candidate in (forced, None)
    if result.candidate is None:
        assert math.isnan(result.theta)
    # C = 360 is exactly C = 0.
    if forced == 360:
        zero = select_heading_vendor(case["P"], case["W"], case["H"], case["B"], case["tower2"], 0)
        assert (bits(result.theta), result.overhang, result.pass_, result.c) == (
            bits(zero.theta),
            zero.overhang,
            zero.pass_,
            zero.c,
        )


def _boundary_targets(W: float, H: float) -> list[tuple[float, float]]:
    targets = []
    for t in (0.0, 0.5, 1.0):
        x, y, leg = t * W, t * H, t * 150.0
        targets += [(0.0, y), (W, y), (x, 0.0), (x, H)]  # panel edges
        targets += [(leg, 150.0 - leg), (0.0, H - 150.0 + leg), (leg, H), (leg, H - 150.0 + leg)]  # Z1, Z2 top left
        targets += [(W - 150.0 + leg, 0.0), (W, leg), (W - 150.0 + leg, leg)]  # Z2 bottom right
    return targets


@pytest.mark.parametrize("W, H", [(2440.0, 1220.0), (1250.0, 2500.0), (804.0, 804.0)])
def test_differential_with_a_support_exactly_on_each_boundary(literal: ModuleType, W: float, H: float) -> None:
    # Deterministic sweep: for every candidate and support, a tool point whose
    # support lands on (or within an ulp of) each panel edge and tower-zone edge.
    exact_edges = 0
    for i in GRID:
        for k in range(3):
            dx, dy = offset(i, k)
            for target in _boundary_targets(W, H):
                P = [(target[0] + dx, target[1] + dy)]
                for tower2 in (0, 1):
                    for forced in (i, -1):
                        args = (P, W, H, frozenset(), tower2, forced)
                        _assert_same_result(select_heading_vendor(*args), literal.select_heading(*args))
                    theta, box, hit = evaluate(i, P, W, H, tower2)
                    l_theta, l_box, l_hit = literal.evaluate(i, P, W, H, tower2)
                    assert [bits(v) for v in (theta,) + box] == [bits(v) for v in (l_theta,) + l_box]
                    assert hit is bool(l_hit)
                    exact_edges += box[0] == 0.0 or box[1] == W or box[2] == 0.0 or box[3] == H
    # Not vacuous: many of these boxes touch a panel edge exactly.
    assert exact_edges > 500, exact_edges
