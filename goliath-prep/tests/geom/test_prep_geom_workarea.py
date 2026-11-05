# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Work areas with per-edge margins, support strips and the desk and job-time panels (unit P04).

docs/prep/safety-model.md sections 6.1 to 6.3 and 8.3; red-team C-16 (the
tower-baseline edge keeps its own margin). A work area is a containment region,
so the kernel's polygon may fall short of the exact set (by at most its
tolerance), never exceed it.
"""
from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from shapely.geometry import LineString, Point, Polygon, box

from goliath_prep.defaults import vendor_footprint
from goliath_prep.geom.footprint import rotation_radius
from goliath_prep.geom.sweeps import APPROX_TOL_MM
from goliath_prep.geom.workarea import (
    SIDES,
    baseline_side,
    desk_panel,
    edge_margins,
    job_panel,
    support_strips,
    work_area_tool,
    work_area_wheel,
)

W, H = 2440.0, 1220.0
SLOP = APPROX_TOL_MM + 1e-6


def parts(geom):  # type: ignore[no-untyped-def]
    return list(getattr(geom, "geoms", [geom]))


def test_sides_and_baseline() -> None:
    assert SIDES == ("left", "right", "bottom", "top")
    # the tower baseline runs from tower 1 to tower 2: x = 0 when tower 2 is on the Y axis, y = 0 on X
    assert baseline_side("Y") == "left"
    assert baseline_side("X") == "bottom"
    with pytest.raises(ValueError):
        baseline_side("Z")  # type: ignore[arg-type]


def test_edge_margins_keep_the_baseline_margin() -> None:
    panel = box(0.0, 0.0, W, H)
    after_r9 = {"m_edge": 10.0, "m_edge_baseline": 50.0}
    assert dict(edge_margins(panel, "Y", after_r9)) == {"left": 50.0, "right": 10.0, "bottom": 10.0, "top": 10.0}
    assert dict(edge_margins(panel, "X", after_r9)) == {"left": 10.0, "right": 10.0, "bottom": 50.0, "top": 10.0}
    # the baseline edge never gets less than every other edge (an owner may tighten m_edge to 60)
    tighter = edge_margins(panel, "Y", {"m_edge": 60.0, "m_edge_baseline": 50.0})
    assert tighter["left"] == 60.0
    with pytest.raises(TypeError):
        tighter["left"] = 0.0  # type: ignore[index]


@pytest.mark.parametrize(
    "margins",
    [{"m_edge": 10.0}, {"m_edge": 10.0, "m_edge_baseline": 50.0, "m_edge_tool": 2.0},
     {"m_edge": -1.0, "m_edge_baseline": 50.0}, {"m_edge": float("nan"), "m_edge_baseline": 50.0}],
)
def test_edge_margins_refuse_missing_unknown_and_bad_values(margins: dict) -> None:
    with pytest.raises((TypeError, ValueError)):
        edge_margins(box(0.0, 0.0, W, H), "Y", margins)


def test_edge_margins_refuse_a_panel_without_the_baseline_edge() -> None:
    # A panel whose boundary has no edge facing the baseline side (here: none faces -X, while tower 2
    # stands on the Y axis) is not a panel the towers can stand on: refused, not given a margin.
    wedge = Polygon([(0.0, 0.0), (1000.0, 0.0), (1000.0, 400.0)])
    margins = {"m_edge": 10.0, "m_edge_baseline": 50.0}
    with pytest.raises(ValueError, match="baseline"):
        edge_margins(wedge, "Y", margins)
    assert edge_margins(wedge, "X", margins)["bottom"] == 50.0


def test_work_area_wheel_rectangle_per_edge() -> None:
    margins = {"left": 50.0, "right": 10.0, "bottom": 20.0, "top": 30.0}
    wa = work_area_wheel(box(0.0, 0.0, W, H), None, margins)
    assert wa.is_valid and isinstance(wa, Polygon)
    minx, miny, maxx, maxy = wa.bounds
    assert 50.0 <= minx <= 50.0 + SLOP
    assert 20.0 <= miny <= 20.0 + SLOP
    assert W - 10.0 - SLOP <= maxx <= W - 10.0
    assert H - 30.0 - SLOP <= maxy <= H - 30.0
    # inside the exact set, and it holds the exact set shrunk by the tolerance
    assert box(50.0, 20.0, W - 10.0, H - 30.0).covers(wa)
    assert wa.covers(box(50.0 + SLOP, 20.0 + SLOP, W - 10.0 - SLOP, H - 30.0 - SLOP))


def test_work_area_wheel_uniform_margin_equals_per_edge() -> None:
    panel = box(0.0, 0.0, W, H)
    uniform = work_area_wheel(panel, None, 50.0)
    per_edge = work_area_wheel(panel, None, {side: 50.0 for side in SIDES})
    assert uniform.equals(per_edge)


def test_no_overhang_by_default_and_declared_strips_extend_the_area() -> None:
    panel = box(0.0, 0.0, W, H)
    assert support_strips(panel, None).is_empty
    assert support_strips(panel, {"left": 0.0, "right": 0.0, "bottom": 0.0, "top": 0.0}).is_empty
    strips = support_strips(panel, {"right": 300.0, "top": 0.0})
    assert strips.equals(box(W, 0.0, W + 300.0, H))
    wa = work_area_wheel(panel, {"right": 300.0}, 50.0)
    minx, miny, maxx, maxy = wa.bounds
    assert W + 250.0 - SLOP <= maxx <= W + 250.0
    # no gap between the panel and its strip: the wheel may cross the panel's right edge there
    assert wa.contains(Point(W, H / 2))
    assert wa.contains(LineString([(W - 100.0, H / 2), (W + 200.0, H / 2)]))
    # but nothing beyond the corner the strip does not cover
    assert not wa.intersects(Point(W + 100.0, H - 10.0))
    with pytest.raises(ValueError):
        support_strips(panel, {"right": -5.0})
    with pytest.raises(ValueError):
        support_strips(panel, {"outside": 5.0})
    # a strip declared on a side no edge faces is refused, not dropped without a word
    wedge = Polygon([(0.0, 0.0), (1000.0, 0.0), (1000.0, 400.0)])
    with pytest.raises(ValueError, match="left"):
        work_area_wheel(wedge, {"left": 200.0}, 50.0)


def test_strip_follows_a_slanted_measured_edge_without_a_gap() -> None:
    panel = Polygon([(0.0, 0.0), (2440.0, 0.0), (2444.0, 1220.0), (0.0, 1220.0)])  # right edge leans 4 mm
    wa = work_area_wheel(panel, {"right": 300.0}, 50.0)
    assert isinstance(wa, Polygon)
    assert wa.contains(LineString([(2300.0, 30.0 + 50.0), (2600.0, 30.0 + 50.0)]))
    assert wa.contains(LineString([(2300.0, 1150.0), (2600.0, 1150.0)]))


def test_work_area_tool_is_the_panel_shrunk_by_2_mm() -> None:
    wa = work_area_tool(box(0.0, 0.0, W, H), 2.0)
    assert box(2.0, 2.0, W - 2.0, H - 2.0).covers(wa)
    assert wa.covers(box(2.0 + SLOP, 2.0 + SLOP, W - 2.0 - SLOP, H - 2.0 - SLOP))
    with pytest.raises(ValueError):
        work_area_tool(box(0.0, 0.0, W, H), -2.0)


def test_desk_panel_and_job_panel() -> None:
    assert desk_panel(W, H, 10.0).equals(box(10.0, 10.0, W - 10.0, H - 10.0))
    with pytest.raises(ValueError, match="geom_tol"):
        desk_panel(15.0, H, 10.0)
    measured = Polygon([(0.0, 0.0), (2441.7, 0.4), (2442.9, 1221.6), (-0.3, 1220.8)])
    p_work = job_panel(measured, (2442, 1221))
    assert box(0.0, 0.0, 2442.0, 1221.0).covers(p_work)
    assert measured.buffer(1e-9).covers(p_work)  # GEOS rounds the new corners on the measured edges
    assert p_work.area == pytest.approx(measured.intersection(box(0.0, 0.0, 2442.0, 1221.0)).area)
    with pytest.raises(ValueError):
        job_panel(measured, (0, 1221))
    with pytest.raises(TypeError):
        job_panel(measured, (2442.0, 1221))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        job_panel(Polygon([(0, 0), (10, 10), (10, 0), (0, 10)]), (10, 10))  # a self-intersecting ring


@pytest.mark.parametrize("w, h, zone_w, zone_h", [(1220.0, 1600.0, 182.0, 562.0), (1220.0, 2440.0, 182.0, 1402.0)])
def test_bring_up_safe_zone_numbers(w: float, h: float, zone_w: float, zone_h: float) -> None:
    # safety-model 8.3: the zone is the desk panel shrunk by R_rot + rho + m_edge + geom_tol = 519 mm.
    rho = 55.0
    reach = rotation_radius(vendor_footprint()) + rho
    wa = work_area_wheel(desk_panel(w, h, 10.0), None, 50.0)

    def disc_fits(p: tuple[float, float]) -> bool:
        return wa.contains(Point(p)) and wa.exterior.distance(Point(p)) >= reach

    lo, eps = 519.0, 2 * SLOP
    for corner in ((lo + eps, lo + eps), (lo + zone_w - eps, lo + eps), (lo + eps, lo + zone_h - eps),
                   (lo + zone_w - eps, lo + zone_h - eps)):
        assert disc_fits(corner), corner
    assert not disc_fits((lo - 1.0, lo + zone_h / 2))
    assert not disc_fits((lo + zone_w + 1.0, lo + zone_h / 2))
    assert not disc_fits((lo + zone_w / 2, lo + zone_h + 1.0))


def _edge_distances(panel: Polygon, q: tuple[float, float]) -> list[tuple[str, float]]:
    """(side, distance to the edge) for every edge of a near-rectangular counter-clockwise panel."""
    coords = list(panel.exterior.coords)
    out = []
    for a, b in zip(coords, coords[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = (dy, -dx)
        side = max((("right", n[0]), ("left", -n[0]), ("top", n[1]), ("bottom", -n[1])), key=lambda s: s[1])[0]
        out.append((side, LineString([a, b]).distance(Point(q))))
    return out


jitter = st.floats(-30.0, 30.0)
margin = st.floats(0.0, 120.0)


@settings(max_examples=80, deadline=None)
@given(
    j=st.tuples(jitter, jitter, jitter, jitter, jitter, jitter, jitter, jitter),
    m=st.tuples(margin, margin, margin, margin),
    u=st.floats(0.0, 1.0),
    v=st.floats(0.0, 1.0),
)
def test_work_area_is_inside_the_exact_per_edge_erosion(j: tuple, m: tuple, u: float, v: float) -> None:
    w, h = 1800.0, 1100.0
    panel = Polygon([(j[0], j[1]), (w + j[2], j[3]), (w + j[4], h + j[5]), (j[6], h + j[7])])
    margins = dict(zip(SIDES, m))
    wa = work_area_wheel(panel, None, margins)
    assert wa.is_valid
    # every vertex of the work area keeps each edge's margin (minus float noise only)
    for poly in parts(wa):
        for ring in [poly.exterior, *poly.interiors]:
            for q in ring.coords:
                assert panel.buffer(1e-6).contains(Point(q))
                for side, dist in _edge_distances(panel, q):
                    assert dist >= margins[side] - 1e-6, (side, dist, q)
    # and a panel point that keeps every margin by more than the tolerance is in it
    minx, miny, maxx, maxy = panel.bounds
    q = (minx + u * (maxx - minx), miny + v * (maxy - miny))
    if panel.contains(Point(q)) and all(d >= margins[s] + SLOP for s, d in _edge_distances(panel, q)):
        assert wa.contains(Point(q))


def test_work_area_inputs_are_refused_not_guessed() -> None:
    panel = box(0.0, 0.0, W, H)
    with pytest.raises(ValueError):
        work_area_wheel(panel, None, {"left": 50.0})  # every side needs its margin
    with pytest.raises(ValueError):
        work_area_wheel(panel, None, -1.0)
    with pytest.raises(TypeError):
        work_area_wheel([(0, 0), (1, 0), (1, 1)], None, 50.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        work_area_wheel(Polygon(), None, 50.0)
    # a margin that leaves nothing is an empty work area, not an error: every wheel check then refuses
    assert work_area_wheel(box(0.0, 0.0, 90.0, 90.0), None, 50.0).is_empty


def test_math_of_the_band_reading() -> None:
    # the per-edge reading: a point keeps margin m_e from edge e (as a segment), for every edge
    panel = box(0.0, 0.0, 1000.0, 1000.0)
    wa = work_area_wheel(panel, None, {"left": 100.0, "right": 10.0, "bottom": 10.0, "top": 10.0})
    assert wa.contains(Point(100.0 + SLOP, 10.0 + SLOP))
    assert not wa.contains(Point(100.0 - 0.5, 500.0))
    assert math.isclose(wa.bounds[0], 100.0, abs_tol=SLOP)
