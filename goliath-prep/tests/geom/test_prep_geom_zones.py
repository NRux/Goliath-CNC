# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Tower keep-out zones at both tower-2 placements, tower bases and anchors (unit P04).

Spec: docs/protocol/08-head-angle-and-cut-order.md *Tower keep-out zones* and
docs/prep/safety-model.md sections 5.1 to 5.3. The vendor's exact triangle
test (edge behaviour by vertex order) is kept for the vendor-floor module; the
kernel's own zones are closed sets grown by a margin, so the edge quirks
cannot decide anything.
"""
from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from shapely.geometry import Point, Polygon

from goliath_prep import heading_vendor
from goliath_prep.geom.sweeps import APPROX_TOL_MM
from goliath_prep.geom.zones import (
    anchor_points,
    tool_zones,
    tower2_placements,
    tower_bases,
    tower_points,
    vendor_triangle_contains,
    wheel_zones,
    zone_triangles,
)

W, H = 2440.0, 1220.0
SLOP = APPROX_TOL_MM + 1e-6  # the most a kernel polygon may exceed the exact set by


def test_z1_edge_cases() -> None:
    z1 = zone_triangles(W, H, "Y", None, leg=150.0)[0]
    assert z1 == ((0.0, 0.0), (0.0, 150.0), (150.0, 0.0))  # spec 08's vertex order
    assert vendor_triangle_contains((75.0, 75.0), z1)  # hypotenuse: inside
    assert vendor_triangle_contains((0.0, 0.0), z1)  # the corner: inside
    assert vendor_triangle_contains((10.0, 10.0), z1)
    assert not vendor_triangle_contains((0.0, 50.0), z1)  # leg x = 0: outside
    assert not vendor_triangle_contains((50.0, 0.0), z1)  # leg y = 0: outside
    assert not vendor_triangle_contains((100.0, 100.0), z1)
    # The kernel's zones are closed sets: every one of those points is in them, legs included.
    zones = wheel_zones(W, H, "Y", None, margin=0.0)
    for q in ((75.0, 75.0), (0.0, 0.0), (0.0, 50.0), (50.0, 0.0), (10.0, 10.0)):
        assert zones.intersects(Point(q)), q
    assert not zones.intersects(Point(100.0, 100.0))


def test_z2_tower2_0_edges() -> None:
    z2 = zone_triangles(W, H, "Y", None, leg=150.0)[1]
    assert z2 == ((0.0, H - 150.0), (0.0, H), (150.0, H))
    assert vendor_triangle_contains((75.0, H), z2)  # edge y = H: inside
    assert not vendor_triangle_contains((75.0, H - 75.0), z2)  # hypotenuse: outside
    assert not vendor_triangle_contains((0.0, H - 50.0), z2)  # edge x = 0: outside
    zones = wheel_zones(W, H, "Y", None, margin=0.0)
    for q in ((75.0, H), (75.0, H - 75.0), (0.0, H - 50.0)):
        assert zones.intersects(Point(q)), q


@pytest.mark.parametrize("axis, vendor_tower2", [("Y", 0), ("X", 1)])
def test_zone_triangles_are_spec_08s_with_the_set_t2_position_mapping(axis: str, vendor_tower2: int) -> None:
    # SET T2 POSITION;0 = along Y = spec 08 tower2 = 0 = corner (0, H); ;1 = along X = corner (W, 0)
    assert zone_triangles(W, H, axis, None, leg=150.0) == heading_vendor.tower_zones(W, H, vendor_tower2)
    for tri in zone_triangles(W, H, axis, None, leg=150.0):
        for q in ((5.0, 5.0), (75.0, 75.0), (W - 5.0, 5.0), (5.0, H - 5.0), (75.0, H)):
            assert vendor_triangle_contains(q, tri) == heading_vendor.triangle_contains(q, tri)


@pytest.mark.parametrize("axis", ["X", "Y"])
def test_tower2_both_placements(axis: str) -> None:
    corner = W if axis == "X" else H
    d_t = corner + 20.0  # INFO;27 puts tower 2 20 mm beyond the panel corner

    def at(t: float, along: float, across: float) -> tuple[float, float]:
        """A point `along` mm before the tower-2 point t on its axis, `across` mm into the panel."""
        return (t - along, across) if axis == "X" else (across, t - along)

    assert tower2_placements(W, H, axis, d_t) == (corner, d_t)
    assert tower2_placements(W, H, axis, None) == (corner,)
    assert tower2_placements(W, H, axis, corner) == (corner,)
    assert len(zone_triangles(W, H, axis, d_t, leg=150.0)) == 3
    assert tower_points(W, H, axis, d_t) == ((0.0, 0.0), at(corner, 0.0, 0.0), at(d_t, 0.0, 0.0))

    zones = wheel_zones(W, H, axis, d_t, margin=25.0)
    corner_only = wheel_zones(W, H, axis, None, margin=25.0)
    for t in (corner, d_t):
        assert zones.contains(Point(at(t, 1.0, 1.0)))  # inside the triangle at t
        assert zones.contains(Point(at(t, 149.0, 0.5)))  # near its far vertex
        assert zones.contains(Point(at(t, -24.0, 0.0)))  # 24 mm beyond the tower point: within m_tower
    assert corner_only.contains(Point(at(corner, -24.0, 0.0)))
    assert not corner_only.contains(Point(at(d_t, -24.0, 0.0)))  # 44 mm beyond the corner placement
    assert not zones.contains(Point(at(d_t, -26.0 - SLOP, 0.0)))
    # tower 2 measured inside the corner too
    inside = wheel_zones(W, H, axis, corner - 15.0, margin=0.0)
    assert inside.intersects(Point(at(corner - 15.0, 150.0, 0.0)))
    assert inside.intersects(Point(at(corner, 150.0, 0.0)))


def test_tool_zones_have_155_mm_legs() -> None:
    tool = tool_zones(W, H, "Y", None, margin=0.0)
    wheel = wheel_zones(W, H, "Y", None, margin=0.0)
    assert tool.intersects(Point(0.0, 155.0)) and not wheel.intersects(Point(0.0, 155.0))
    assert tool.intersects(Point(77.5, 77.5)) and not wheel.intersects(Point(77.5, 77.5))
    assert not tool.intersects(Point(0.0, 155.5))
    assert zone_triangles(W, H, "Y", None, leg=155.0)[0] == ((0.0, 0.0), (0.0, 155.0), (155.0, 0.0))


def _exact_distance(q: tuple[float, float], triangles: tuple) -> float:
    return min(Polygon(t).distance(Point(q)) for t in triangles)


@settings(max_examples=120, deadline=None)
@given(
    axis=st.sampled_from(["X", "Y"]),
    d_extra=st.one_of(st.none(), st.floats(-40.0, 40.0)),
    margin=st.floats(0.0, 60.0),
    leg=st.sampled_from([150.0, 155.0]),
    angle=st.floats(0.0, 360.0),
    fraction=st.floats(0.0, 1.0),
    which=st.integers(0, 2),
)
def test_grown_zones_hold_every_point_within_the_margin(
    axis: str, d_extra: float | None, margin: float, leg: float, angle: float, fraction: float, which: int
) -> None:
    corner = W if axis == "X" else H
    d_t = None if d_extra is None else corner + d_extra
    triangles = zone_triangles(W, H, axis, d_t, leg=leg)
    tri = triangles[min(which, len(triangles) - 1)]
    zones = wheel_zones(W, H, axis, d_t, leg=leg, margin=margin)
    assert zones.is_valid
    # a point at distance <= margin from a triangle vertex or edge is in the grown zone
    a, b = tri[which % 3], tri[(which + 1) % 3]
    base = (a[0] + fraction * (b[0] - a[0]), a[1] + fraction * (b[1] - a[1]))
    q = (base[0] + margin * math.cos(math.radians(angle)), base[1] + margin * math.sin(math.radians(angle)))
    if _exact_distance(q, triangles) <= margin - 1e-9:
        assert zones.intersects(Point(q))
    # and no point of the grown zone lies farther than margin (plus the kernel's tolerance) from a triangle
    geoms = getattr(zones, "geoms", [zones])
    for g in geoms:
        for v in g.exterior.coords:
            assert _exact_distance(v, triangles) <= margin + SLOP


def test_tower_bases_at_both_placements() -> None:
    bases = tower_bases(W, H, "Y", H + 20.0, radius=120.0)
    for centre in ((0.0, 0.0), (0.0, H), (0.0, H + 20.0)):
        for a in range(0, 360, 15):
            q = (centre[0] + 119.99 * math.cos(math.radians(a)), centre[1] + 119.99 * math.sin(math.radians(a)))
            assert bases.contains(Point(q)), (centre, a)
    assert not bases.intersects(Point(120.0 + SLOP, 0.0))
    assert not bases.intersects(Point(0.0, H + 20.0 + 120.0 + SLOP))


def test_anchor_points_are_the_150_mm_triangle_centroids() -> None:
    a1, a2 = anchor_points(W, H, "Y", None)
    assert a1 == (50.0, 50.0)
    assert a2 == ((50.0, H - 50.0),)
    a1, a2 = anchor_points(W, H, "X", W + 12.0)
    assert a1 == (50.0, 50.0)
    assert a2 == ((W - 50.0, 50.0), (W + 12.0 - 50.0, 50.0))
    # both lie inside the 155 mm tool zones, which the tool never enters (safety-model 5.3)
    tool = tool_zones(W, H, "X", W + 12.0, margin=0.0)
    for a in (a1,) + a2:
        assert tool.contains(Point(a))


@pytest.mark.parametrize(
    "call",
    [
        lambda: wheel_zones(W, H, "Z", None, margin=25.0),  # type: ignore[arg-type]
        lambda: wheel_zones(W, H, 0, None, margin=25.0),  # type: ignore[arg-type]
        lambda: wheel_zones(0.0, H, "Y", None, margin=25.0),
        lambda: wheel_zones(W, float("nan"), "Y", None, margin=25.0),
        lambda: wheel_zones(W, H, "Y", -5.0, margin=25.0),
        lambda: wheel_zones(W, H, "Y", float("inf"), margin=25.0),
        lambda: wheel_zones(W, H, "Y", None, margin=-1.0),
        lambda: wheel_zones(W, H, "Y", None, leg=0.0, margin=25.0),
        lambda: tower_bases(W, H, "Y", None, radius=0.0),
        lambda: wheel_zones(W, H, "Y", None),  # type: ignore[call-arg]  # the margin has no default
    ],
)
def test_zone_inputs_are_refused_not_guessed(call) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises((TypeError, ValueError)):
        call()


def test_vendor_triangle_contains_is_the_vendor_reference() -> None:
    # the kernel keeps spec 08's own test (goliath_prep.heading_vendor), it does not write a second one
    assert vendor_triangle_contains is heading_vendor.triangle_contains
