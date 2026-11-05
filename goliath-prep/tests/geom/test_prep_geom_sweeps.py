# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Discs, capsules, annuli, rotation sweeps, envelopes, reach and the body hull (unit P04).

docs/prep/safety-model.md sections 4.4 to 4.6, 6.4, 8 and 9.3 to 9.4; red-team
PHYS-05 and PHYS-07. Every shape of the robot (a wheel disc, its sweep, an
envelope) is a keep-in-mind region the checks test against keep-outs, so the
kernel's polygon must hold the exact set; it may exceed it by at most the
kernel's tolerance. Reach regions are containment regions: they may only fall
short.
"""
from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from shapely.geometry import LineString, MultiPoint, Point, Polygon

from goliath_prep.core.types import FootprintSet
from goliath_prep.defaults import vendor_footprint
from goliath_prep.geom.footprint import (
    C_GRID,
    centroid_offsets,
    footprint_radii,
    rotation_radius,
    support_offset,
    support_positions,
    theta,
)
from goliath_prep.geom.sweeps import (
    APPROX_TOL_MM,
    annulus,
    any_rotation_envelope,
    arc_hull_points,
    arc_sweep,
    body_hull,
    body_hull_local,
    body_radius,
    capsule,
    convex_envelope,
    disc,
    grown_hull,
    reach_region,
    rotation_disc,
    support_arc_hull,
    wheel_capsules,
    wheel_discs,
)
from goliath_prep.geom.zones import tower_points
from goliath_prep.machine import MachineProfile

SLOP = APPROX_TOL_MM + 1e-6
coord = st.floats(-4000.0, 4000.0)
point = st.tuples(coord, coord)


def holds_disc(poly, q: tuple[float, float], r: float) -> bool:  # type: ignore[no-untyped-def]
    """The exact test 'the disc of radius r about q lies inside poly' (poly may have holes)."""
    return poly.contains(Point(q)) and poly.boundary.distance(Point(q)) >= r - 1e-7


def vertices(poly):  # type: ignore[no-untyped-def]
    for part in getattr(poly, "geoms", [poly]):
        for ring in [part.exterior, *part.interiors]:
            yield from ring.coords


# ---------------------------------------------------------------------------
# Discs, capsules, annuli


@settings(max_examples=150, deadline=None)
@given(p=point, r=st.floats(0.5, 3600.0))
def test_outer_disc_holds_the_disc_and_little_more(p: tuple[float, float], r: float) -> None:
    poly = disc(p, r)
    assert poly.is_valid
    assert holds_disc(poly, p, r)
    assert all(math.dist(v, p) <= r + SLOP for v in vertices(poly))


@settings(max_examples=150, deadline=None)
@given(p=point, r=st.floats(0.5, 3600.0))
def test_inner_disc_lies_inside_the_disc(p: tuple[float, float], r: float) -> None:
    poly = disc(p, r, outer=False)
    assert poly.is_valid
    assert all(math.dist(v, p) <= r for v in vertices(poly))
    assert holds_disc(poly, p, r - SLOP)


def test_disc_refuses_bad_radius_and_point() -> None:
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            disc((0.0, 0.0), bad)
    with pytest.raises(ValueError):
        disc((float("inf"), 0.0), 5.0)
    with pytest.raises(ValueError):
        disc((0.0, 0.0), 5.0, tol=0.0)


@settings(max_examples=100, deadline=None)
@given(a=point, b=point, r=st.floats(0.5, 600.0), t=st.floats(0.0, 1.0))
def test_capsule_holds_every_disc_along_the_segment(a: tuple, b: tuple, r: float, t: float) -> None:
    poly = capsule(a, b, r)
    assert poly.is_valid
    q = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
    assert holds_disc(poly, q, r)
    segment = LineString([a, b]) if a != b else Point(a)
    assert all(segment.distance(Point(v)) <= r + SLOP for v in vertices(poly))


@settings(max_examples=120, deadline=None)
@given(
    pts=st.lists(st.tuples(st.floats(-800.0, 800.0), st.floats(-800.0, 800.0)), min_size=1, max_size=30),
    r=st.floats(0.5, 500.0),
    weights=st.lists(st.floats(0.0, 1.0), min_size=30, max_size=30),
)
def test_grown_hull_holds_the_grown_convex_hull(pts: list, r: float, weights: list) -> None:
    poly = grown_hull(pts, r)
    assert poly.is_valid
    # a convex combination of the points, and every point itself, keeps a disc of radius r inside
    total = 0.0
    for w in weights[: len(pts)]:
        total += w
    if total > 0.0:
        q = (0.0, 0.0)
        for w, p in zip(weights, pts):
            q = (q[0] + w / total * p[0], q[1] + w / total * p[1])
        assert holds_disc(poly, q, r - 1e-6)
    for p in pts:
        assert holds_disc(poly, p, r)
    # and no vertex lies farther than r (plus the tolerance) from the hull of the points
    core = MultiPoint(pts).convex_hull
    assert all(core.distance(Point(v)) <= r + SLOP for v in vertices(poly))


def test_annulus() -> None:
    ring = annulus((100.0, 200.0), 156.0, 459.0)
    assert ring.is_valid and len(ring.interiors) == 1
    for a in range(0, 360, 7):
        u = (math.cos(math.radians(a)), math.sin(math.radians(a)))
        for radius in (156.0, 300.0, 459.0 - 1e-6):
            assert ring.intersects(Point(100.0 + radius * u[0], 200.0 + radius * u[1])), (a, radius)
        assert not ring.intersects(Point(100.0 + (156.0 - SLOP) * u[0], 200.0 + (156.0 - SLOP) * u[1]))
    # a non-positive inner radius leaves no hole: the full disc
    assert annulus((0.0, 0.0), -5.0, 100.0).equals(disc((0.0, 0.0), 100.0))
    assert annulus((0.0, 0.0), 0.0, 100.0).equals(disc((0.0, 0.0), 100.0))
    with pytest.raises(ValueError):
        annulus((0.0, 0.0), 100.0, 100.0)


# ---------------------------------------------------------------------------
# Rotation arc sweeps (safety-model 9.3)


def _arc_distance(q: tuple[float, float], pivot: tuple, radius: float, ang0: float, sweep: float) -> float:
    """Exact distance from q to the arc (test arithmetic)."""
    ends = [(pivot[0] + radius * math.cos(math.radians(a)), pivot[1] + radius * math.sin(math.radians(a)))
            for a in (ang0, ang0 + sweep)]
    best = min(math.dist(q, e) for e in ends)
    if abs(sweep) >= 360.0:
        return abs(math.dist(q, pivot) - radius)
    rel = math.degrees(math.atan2(q[1] - pivot[1], q[0] - pivot[0]))
    offset = (rel - ang0) % 360.0 if sweep >= 0 else (ang0 - rel) % 360.0
    if offset <= abs(sweep) and q != pivot:
        best = min(best, abs(math.dist(q, pivot) - radius))
    return best


@settings(max_examples=150, deadline=None)
@given(
    pivot=point,
    radius=st.floats(0.0, 650.0),
    ang0=st.floats(-720.0, 720.0),
    sweep=st.floats(-400.0, 400.0),
    rho=st.floats(1.0, 80.0),
    ts=st.lists(st.floats(0.0, 1.0), min_size=1, max_size=12),
)
def test_arc_sweep_contains_sampled_arc(pivot, radius, ang0, sweep, rho, ts) -> None:  # type: ignore[no-untyped-def]
    poly = arc_sweep(pivot, radius, ang0, ang0 + sweep, rho)
    assert poly.is_valid and isinstance(poly, Polygon)
    for t in [0.0, 1.0] + ts:
        a = math.radians(ang0 + t * sweep)
        q = (pivot[0] + radius * math.cos(a), pivot[1] + radius * math.sin(a))
        assert holds_disc(poly, q, rho), (t, q)
    # tight: no vertex lies farther than rho (plus the tolerance) from the arc
    for v in vertices(poly):
        assert _arc_distance(v, pivot, radius, ang0, sweep) <= rho + SLOP + 1e-6


def test_arc_sweep_of_a_wheel_turning_a_quarter_about_the_tool() -> None:
    # The rear support (402 mm) swung from C = 0 to C = 90 about the tool at the origin, grown by rho.
    rho = 55.0
    d0 = support_offset(402.0, 0.0, theta(0))
    d1 = support_offset(402.0, 0.0, theta(90))
    a0 = math.degrees(math.atan2(d0[1], d0[0]))
    poly = arc_sweep((0.0, 0.0), 402.0, a0, a0 + 90.0, rho)
    for q in (d0, d1, (402.0 * math.cos(math.radians(a0 + 45)), 402.0 * math.sin(math.radians(a0 + 45)))):
        assert holds_disc(poly, q, rho)
    assert not poly.intersects(Point(0.0, 0.0))  # the inner edge: 402 - 55 = 347 mm from the pivot
    assert not poly.intersects(Point(-300.0, -100.0))  # the other way round is not swept


def test_arc_sweep_full_turn_is_the_annulus() -> None:
    assert arc_sweep((0.0, 0.0), 211.0, 10.0, 370.0, 55.0).equals(annulus((0.0, 0.0), 156.0, 266.0))
    assert arc_sweep((0.0, 0.0), 30.0, 0.0, -400.0, 55.0).equals(disc((0.0, 0.0), 85.0))


# ---------------------------------------------------------------------------
# Lines that rotate and translate together (safety-model 9.4)


def test_arc_hull_points_hold_the_arc() -> None:
    pts = arc_hull_points((10.0, -20.0), 300.0, 30.0, 250.0)
    hull = MultiPoint(pts).convex_hull
    for k in range(0, 101):
        a = math.radians(30.0 + 2.2 * k)
        q = (10.0 + 300.0 * math.cos(a), -20.0 + 300.0 * math.sin(a))
        assert hull.buffer(1e-9).contains(Point(q))


def _both_ways(theta_a: float, theta_b: float) -> tuple[float, float]:
    ccw = (theta_b - theta_a) % 360.0
    return ccw, ccw - 360.0 if ccw else 0.0


fp_union = FootprintSet(vendor=vendor_footprint().vendor, measured=((405.0, 0.5), None, (210.0, -76.5)),
                        active="union")


@settings(max_examples=60, deadline=None)
@given(
    pa=point, pb=point, ca=st.sampled_from(C_GRID), cb=st.sampled_from(C_GRID),
    t_move=st.floats(0.0, 1.0), t_turn=st.floats(0.0, 1.0), rho=st.floats(15.0, 80.0),
    which=st.sampled_from(["vendor", "union"]),
)
def test_convex_envelope_contains_sampled_blends(pa, pb, ca, cb, t_move, t_turn, rho, which) -> None:  # type: ignore[no-untyped-def]
    # The tool moves along pa -> pb while the heading moves monotonically from theta(ca) to theta(cb), in
    # any blend; until the direction is a fact, both directions (design 4.10.4, safety-model 9.4 case 2).
    fp = vendor_footprint() if which == "vendor" else fp_union
    ta, tb = theta(ca), theta(cb)
    m = (pa[0] + t_move * (pb[0] - pa[0]), pa[1] + t_move * (pb[1] - pa[1]))
    for pos in support_positions(fp):
        offsets: list[tuple[float, float]] = []
        for sweep in _both_ways(ta, tb):
            offsets.extend(support_arc_hull(pos.r, pos.delta, ta, ta + sweep))
        envelope = convex_envelope(pa, pb, offsets, rho)
        assert envelope.is_valid
        for sweep in _both_ways(ta, tb):
            a = math.radians(ta + t_turn * sweep + pos.delta)
            q = (m[0] - pos.r * math.cos(a), m[1] - pos.r * math.sin(a))
            assert holds_disc(envelope, q, rho), (pos.label, sweep)


def test_one_direction_envelope_misses_the_other_direction() -> None:
    # C 0 -> 180 in place: turning counter-clockwise the rear support passes +X, clockwise -X.
    rear = support_positions(vendor_footprint())[0]
    p = (1000.0, 1000.0)
    ccw = convex_envelope(p, p, support_arc_hull(rear.r, rear.delta, 90.0, 270.0), 55.0)
    both = convex_envelope(p, p, support_arc_hull(rear.r, rear.delta, 90.0, 270.0)
                           + support_arc_hull(rear.r, rear.delta, 90.0, -90.0), 55.0)
    assert holds_disc(ccw, (1402.0, 1000.0), 55.0) and not ccw.intersects(Point(600.0, 1000.0))
    assert holds_disc(both, (1402.0, 1000.0), 55.0) and holds_disc(both, (598.0, 1000.0), 55.0)


def test_convex_envelope_refuses_no_offsets() -> None:
    with pytest.raises(ValueError):
        convex_envelope((0.0, 0.0), (1.0, 1.0), [], 55.0)


# ---------------------------------------------------------------------------
# Rotation disc and the any-rotation envelope (safety-model 4.6, 8, 9.4 case 1; PHYS-07)


def test_rotation_disc_radius_is_r_rot_plus_rho() -> None:
    poly = rotation_disc((700.0, 800.0), vendor_footprint(), 55.0)
    assert holds_disc(poly, (700.0, 800.0), 459.0)
    assert all(math.dist(v, (700.0, 800.0)) <= 459.0 + SLOP for v in vertices(poly))


@settings(max_examples=120, deadline=None)
@given(
    pa=point, pb=point, theta_a=st.floats(-720.0, 720.0), theta_b=st.floats(-720.0, 720.0),
    theta_t=st.floats(-720.0, 720.0), s=st.floats(0.0, 1.0), t=st.floats(0.0, 1.0),
    rho=st.floats(15.0, 80.0), which=st.sampled_from(["vendor", "union"]), combo=st.integers(0, 3),
)
def test_any_rotation_envelope_contains_rotation_about_centroid(
    pa, pb, theta_a, theta_b, theta_t, s, t, rho, which, combo  # type: ignore[no-untyped-def]
) -> None:
    # The robot turns about a body point between the tool and its support centroid (fraction s), any
    # amount, any direction, while that pivot moves straight from its start to its end position, and it
    # reaches the commanded XY. Every wheel disc stays in conv(disc(pa, R_rot + rho) | disc(pb, R_rot + rho)).
    fp = vendor_footprint() if which == "vendor" else fp_union
    envelope = any_rotation_envelope(pa, pb, fp, rho)
    assert envelope.is_valid
    # one combination of spec and measured positions is the robot's true footprint
    choices = [[fp.vendor[k]] + ([fp.measured[k]] if fp.measured and fp.measured[k] else []) for k in range(3)]
    picked = [options[(combo >> k) % len(options)] for k, options in enumerate(choices)]

    def body(theta_deg: float) -> tuple[list[tuple[float, float]], tuple[float, float]]:
        ds = [(-r * math.cos(math.radians(theta_deg + d)), -r * math.sin(math.radians(theta_deg + d)))
              for r, d in picked]
        c = ((ds[0][0] + ds[1][0] + ds[2][0]) / 3, (ds[0][1] + ds[1][1] + ds[2][1]) / 3)
        return ds, c

    _, ca = body(theta_a)
    _, cb = body(theta_b)
    start_pivot = (pa[0] + s * ca[0], pa[1] + s * ca[1])
    end_pivot = (pb[0] + s * cb[0], pb[1] + s * cb[1])
    pivot = (start_pivot[0] + t * (end_pivot[0] - start_pivot[0]), start_pivot[1] + t * (end_pivot[1] - start_pivot[1]))
    ds, c = body(theta_t)
    for d in ds:
        q = (pivot[0] + d[0] - s * c[0], pivot[1] + d[1] - s * c[1])
        assert holds_disc(envelope, q, rho)


def test_half_turn_about_the_centroid_stays_in_the_rotation_disc_not_the_annulus() -> None:
    # Red-team PHYS-07, worked: C 0 -> 180 about the centroid, the tool commanded to stay at p.
    p, rho = (1500.0, 900.0), 55.0
    fp = vendor_footprint()
    (c,) = centroid_offsets(fp, 0)
    rear = support_offset(402.0, 0.0, theta(0))
    turned = (p[0] + 2 * c[0] - rear[0], p[1] + 2 * c[1] - rear[1])
    assert holds_disc(rotation_disc(p, fp, rho), turned, rho)
    r_min = footprint_radii(fp).r_min
    assert not holds_disc(annulus(p, r_min - rho, rotation_radius(fp) + rho), turned, rho)


# ---------------------------------------------------------------------------
# Wheel discs and capsules; rho from the machine profile (P01: r_w is the maximum of the modules)


def test_wheel_discs_use_the_profile_rho_from_the_largest_wheel() -> None:
    profile = MachineProfile.from_mapping({
        "profile_version": 1,
        "serial": "TEST-SERIAL-0001",
        "wheel": {"q1": {"contact_radius_mm": 30.0}, "q2": {"contact_radius_mm": 38.0},
                  "q3": {"contact_radius_mm": 35.0}},
    })
    assert profile.params["r_w"].value == 38.0  # the maximum, never the mean
    rho = profile.params["rho"].value
    assert rho == 53.0  # r_w + m_w
    p = (1000.0, 600.0)
    discs = wheel_discs(p, 30, profile.footprint_models(), rho)
    assert [pos.label for pos, _ in discs] == ["q1", "q2", "q3"]
    for pos, poly in discs:
        d = support_offset(pos.r, pos.delta, theta(30))
        centre = (p[0] + d[0], p[1] + d[1])
        assert holds_disc(poly, centre, rho)
        assert all(math.dist(v, centre) <= rho + SLOP for v in vertices(poly))


def test_wheel_capsules_follow_the_tool_path_translated_by_each_support() -> None:
    a, b, rho = (500.0, 500.0), (900.0, 650.0), 55.0
    caps = wheel_capsules(a, b, 120, fp_union, rho)
    assert [pos.label for pos, _ in caps] == ["q1", "q2", "q3", "q1 (measured)", "q3 (measured)"]
    for pos, poly in caps:
        d = support_offset(pos.r, pos.delta, theta(120))
        for t in (0.0, 0.3, 1.0):
            q = (a[0] + d[0] + t * (b[0] - a[0]), a[1] + d[1] + t * (b[1] - a[1]))
            assert holds_disc(poly, q, rho)


# ---------------------------------------------------------------------------
# Wire reach (P-REACH; safety-model 6.4; red-team PHYS-05)


def test_reach_region_far_corner_3000x2500() -> None:
    for axis in ("X", "Y"):
        towers = tower_points(3000.0, 2500.0, axis, None)
        region = reach_region(towers, 3500.0)
        assert region.is_valid
        # the far corner is 3905 mm from tower 1: out of reach, whichever axis tower 2 stands on
        assert math.dist((3000.0, 2500.0), (0.0, 0.0)) == pytest.approx(3905.1, abs=0.1)
        assert not region.intersects(Point(3000.0, 2500.0))
        assert region.contains(Point(1500.0, 1250.0))
        # with tower 2 on X at (3000, 0), the corner (0, 2500) is 3905 mm from it: out of reach too
        assert region.intersects(Point(0.0, 2500.0)) == (axis == "Y")
    # both tower-2 placements count
    one = reach_region(tower_points(3000.0, 2500.0, "Y", None), 3500.0)
    two = reach_region(tower_points(3000.0, 2500.0, "Y", 2530.0), 3500.0)
    assert two.within(one.buffer(1e-6))
    # 3500.3 mm from tower 2 at 2530, 3482.4 mm from it at the corner, 2832.7 mm from tower 1
    probe = (2800.0, 2530.0 - math.sqrt(3500.0**2 - 2800.0**2) - 0.5)
    assert one.contains(Point(probe)) and not two.intersects(Point(probe))


@settings(max_examples=150, deadline=None)
@given(q=st.tuples(st.floats(-1000.0, 4500.0), st.floats(-1000.0, 4000.0)), axis=st.sampled_from(["X", "Y"]))
def test_reach_region_is_inside_every_tower_disc(q: tuple[float, float], axis: str) -> None:
    towers = tower_points(3050.0, 1530.0, axis, None)
    region = reach_region(towers, 3500.0)
    far = max(math.dist(q, t) for t in towers)
    if far > 3500.0:
        assert not region.intersects(Point(q))
    elif far <= 3500.0 - SLOP:
        assert region.contains(Point(q))


def test_reach_region_refuses_no_tower() -> None:
    with pytest.raises(ValueError):
        reach_region([], 3500.0)
    with pytest.raises(ValueError):
        reach_region([(0.0, 0.0)], 0.0)


# ---------------------------------------------------------------------------
# Body hull (safety-model 4.5)


def test_default_body_hull_and_its_radius() -> None:
    fp, rho = vendor_footprint(), 55.0
    local = body_hull_local(fp, rho, hull_margin=60.0)
    for pos in support_positions(fp):
        d = support_offset(pos.r, pos.delta, theta(0))
        assert holds_disc(local, d, rho + 60.0)
    # R_body with the default hull: the bound R_rot + rho + m_body (safety-model 4.5): 519 mm
    assert body_radius(fp, rho, hull_margin=60.0) == 519.0


def test_measured_body_hull_from_the_outline() -> None:
    fp, rho = vendor_footprint(), 55.0
    outline_back_right = [(-90.0, -260.0), (-90.0, 255.0), (470.0, 255.0), (470.0, -260.0)]
    local = body_hull_local(fp, rho, outline=outline_back_right)
    for back, right in outline_back_right:
        assert local.intersects(Point(right, -back))
    for pos in support_positions(fp):
        assert holds_disc(local, support_offset(pos.r, pos.delta, theta(0)), rho)
    # R_body: the largest over hull points h of |h| and |c| + |h - c| (c the support centroid)
    (c,) = centroid_offsets(fp, 0)
    exact = max(max(math.hypot(*h), math.hypot(*c) + math.dist(h, c)) for h in local.exterior.coords)
    corners = [(right, -back) for back, right in outline_back_right]
    exact_corners = max(math.hypot(*c) + math.dist(h, c) for h in corners)
    assert exact_corners <= exact
    r_body = body_radius(fp, rho, outline=outline_back_right)
    assert r_body == math.ceil(r_body) and exact <= r_body < exact + 1.0 + 1e-6
    with pytest.raises(ValueError):
        body_hull_local(fp, rho)  # neither the margin nor an outline
    with pytest.raises(ValueError):
        body_hull_local(fp, rho, hull_margin=60.0, outline=outline_back_right)  # both


def test_body_hull_is_placed_at_the_pose() -> None:
    fp, rho = vendor_footprint(), 55.0
    local = body_hull_local(fp, rho, hull_margin=60.0)
    for c in (0, 90, 230):
        placed = body_hull((800.0, 900.0), c, local)
        for pos in support_positions(fp):
            d = support_offset(pos.r, pos.delta, theta(c))
            assert holds_disc(placed, (800.0 + d[0], 900.0 + d[1]), rho + 60.0 - 1e-6)
    assert body_hull((0.0, 0.0), 0, local).equals(local)
