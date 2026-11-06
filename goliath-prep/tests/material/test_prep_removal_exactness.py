# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Exactness in time against the closed form (unit P06; the project design, section 10.2 property 4).

For one straight cut from A to B (length L, direction u, normal n) at a fixed
heading, support k sits at the offset d_k from the tool point; write
a = d_k . u (along the motion) and b = d_k . n (across it). While the tool is
at A + s u the wheel centre is at A + (s + a) u + b n, and the material removed
so far is the segment from A to A + s u grown by R = r_tool + delta_tool; the
wheel disc of radius rho meets it when the wheel centre comes within
rho + R of that segment. A leading support (a > 0) stays |d_k| >= 211 mm away.
A trailing one (a <= 0) is nearest at the end of the cut: |b| away when
L >= -a, else sqrt((L + a)^2 + b^2) away. So W-SURF must flag support k
exactly when

    a <= 0  and  that nearest distance <= rho + R.

The removal index checks piece by piece and leaves each piece's own removal
out; the time invariant (safety-model 7.3) is what makes that the same answer
as the continuous one, so the property compares the two and also asks for the
exact slack when the support is clear.
"""
from __future__ import annotations

import math

import pytest
from hypothesis import HealthCheck, assume, event, given, settings
from hypothesis import strategies as st
from prep_removal_support import (
    DELTA_TOOL,
    E_MAX,
    FP,
    R_TOOL,
    REACH,
    RHO,
    cut_lines,
    cut_piece_indices,
    offsets,
    pieces_of,
    program,
)

from goliath_prep.defaults import C_GRID
from goliath_prep.material import RemovalIndex, derive_delta


def _closed_form(a: float, b: float, length: float) -> tuple[bool, float, float]:
    """(flagged, nearest distance of the wheel centre to the removed segment, distance from the boundary)."""
    if a > 0.0:
        nearest = math.sqrt(a * a + b * b)
    elif length >= -a:
        nearest = abs(b)
    else:
        nearest = math.sqrt((length + a) ** 2 + b * b)
    return nearest <= REACH, nearest, abs(nearest - REACH)


def _check(ax: float, ay: float, bx: float, by: float, c: int) -> None:
    """The removal index's W-SURF along one straight cut from (ax, ay) to (bx, by) at C = c, against the closed form."""
    dx, dy = bx - ax, by - ay
    real_length = math.sqrt(dx * dx + dy * dy)
    assume(real_length >= 0.5)
    ux, uy = dx / real_length, dy / real_length
    prog = program(cut_lines([(ax, ay), (bx, by)], depth=5.0))
    delta = derive_delta(FP, RHO, R_TOOL, DELTA_TOOL, E_MAX)
    pcs = pieces_of(prog, delta)
    index = RemovalIndex.build(pcs, R_TOOL, DELTA_TOOL, None, footprint=FP, rho=RHO)
    cut = cut_piece_indices(pcs, 0)
    for k, d in offsets(c):
        a = d[0] * ux + d[1] * uy
        b = -d[0] * uy + d[1] * ux
        expected, nearest, margin = _closed_form(a, b, real_length)
        assume(margin > 1e-6)  # the boundary itself is a matter of float rounding, not of the model
        results = index.wheel_track(cut, d, RHO)
        slacks = [r.slack for r in results]
        flagged = any(r.witness is not None for r in results)
        event("support flagged" if expected else ("support trailing, clear" if a <= 0.0 else "support leading"))
        assert flagged == expected, (k, c, (ax, ay), (bx, by), a, b, real_length, min(slacks))
        if not expected and a <= 0.0:
            # Clear, and the smallest slack is the closed form's clearance: exact, not merely on the safe side.
            assert min(slacks) == pytest.approx(nearest - REACH, abs=1e-6)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(x0=st.integers(min_value=700_000, max_value=1_700_000), y0=st.integers(min_value=400_000, max_value=800_000),
       angle=st.floats(min_value=0.0, max_value=360.0, allow_nan=False),
       length=st.floats(min_value=1.0, max_value=700.0), c=st.sampled_from(C_GRID))
def test_closed_form_exactness_single_straight_cut(x0: int, y0: int, angle: float, length: float, c: int) -> None:
    ax, ay = x0 / 1000.0, y0 / 1000.0
    bx = round(ax + length * math.cos(math.radians(angle)), 3)
    by = round(ay + length * math.sin(math.radians(angle)), 3)
    _check(ax, ay, bx, by, c)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(c=st.sampled_from(C_GRID), k=st.sampled_from((1, 2, 3)), phi=st.floats(min_value=-0.45, max_value=0.45),
       length=st.floats(min_value=100.0, max_value=480.0))
def test_closed_form_exactness_in_the_trailing_band(c: int, k: int, phi: float, length: float) -> None:
    """The same property where it matters: cuts aimed so that support k trails close to the cut line."""
    d = dict(offsets(c))[k]
    r = math.sqrt(d[0] * d[0] + d[1] * d[1])
    back = math.atan2(-d[1], -d[0]) + phi  # the cut direction: away from the support, turned by phi
    ax, ay = 1220.0, 610.0
    bx = round(ax + length * math.cos(back), 3)
    by = round(ay + length * math.sin(back), 3)
    assert r > 0.0
    _check(ax, ay, bx, by, c)
