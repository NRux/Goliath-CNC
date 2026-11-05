# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""One heading held around a closed outline: the geometric fact behind safety-model section 15 (unit P04).

The project design (section 4.20, red-team PHYS-15) found that, with one
heading per cut, closed profiles larger than about 200 to 220 mm across do not
certify. The cause is not the 40 mm unmeasured wheel contact radius: the
failure persists with rho = 15 mm, for every heading. It is a property of
holding one heading around a closed outline with this footprint. At a fixed
heading each support's path is the outline translated by that support's fixed
offset d_k (the side supports' offsets are 211 mm long), and once the outline
is larger than that offset some support's path crosses the outline.

These tests pin the kernel against that computation: a 250 mm square at
rho = 15 mm has, at every one of the 36 headings, a support whose wheel-disc
path meets the outline; a 100 mm square has a heading where none does (in fact
all 36: any 211 mm offset moves at least 149 mm along X or Y).
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from shapely.geometry import LinearRing

from goliath_prep.core.types import FootprintSet
from goliath_prep.defaults import vendor_footprint
from goliath_prep.geom.footprint import C_GRID
from goliath_prep.geom.sweeps import wheel_capsules


def square(x0: float, y0: float, side: float) -> list[tuple[float, float]]:
    return [(x0, y0), (x0 + side, y0), (x0 + side, y0 + side), (x0, y0 + side)]


def crossing_supports(outline: list[tuple[float, float]], c: int, fp: FootprintSet, rho: float) -> set[str]:
    """The supports whose wheel-disc path, with the tool once around ``outline`` at C = c, meets the outline."""
    ring = LinearRing(outline)
    hits = set()
    for a, b in zip(outline, outline[1:] + outline[:1]):
        for position, path in wheel_capsules(a, b, c, fp, rho):
            if path.intersects(ring):
                hits.add(position.label)
    return hits


@pytest.mark.parametrize("rho", [15.0, 55.0])
def test_250_mm_square_has_a_crossing_support_at_every_heading(rho: float) -> None:
    outline = square(1000.0, 600.0, 250.0)
    for c in C_GRID:
        assert crossing_supports(outline, c, vendor_footprint(), rho), c
    assert len(C_GRID) == 36


def test_100_mm_square_has_a_heading_with_no_crossing_support() -> None:
    outline = square(1000.0, 600.0, 100.0)
    clean = [c for c in C_GRID if not crossing_supports(outline, c, vendor_footprint(), 15.0)]
    assert clean
    assert clean == list(C_GRID)


@settings(max_examples=25, deadline=None)
@given(x0=st.floats(-3000.0, 3000.0), y0=st.floats(-3000.0, 3000.0), side=st.floats(212.0, 1500.0),
       c=st.sampled_from(C_GRID))
def test_any_square_wider_than_the_side_offset_is_crossed_at_any_heading(
    x0: float, y0: float, side: float, c: int
) -> None:
    # The side supports sit 211 mm from the tool: their offset moves the outline by at most 211 mm along
    # X and along Y, so the translated outline crosses a square more than 211 mm wide at every heading.
    assert crossing_supports(square(x0, y0, side), c, vendor_footprint(), 15.0)


@settings(max_examples=25, deadline=None)
@given(x0=st.floats(-3000.0, 3000.0), y0=st.floats(-3000.0, 3000.0), side=st.floats(5.0, 100.0))
def test_any_square_up_to_100_mm_has_a_clean_heading(x0: float, y0: float, side: float) -> None:
    clean = [c for c in C_GRID if not crossing_supports(square(x0, y0, side), c, vendor_footprint(), 15.0)]
    assert clean
