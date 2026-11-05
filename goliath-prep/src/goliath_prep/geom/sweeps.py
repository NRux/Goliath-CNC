# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Shapes of the robot: discs, capsules, annuli, rotation sweeps, envelopes, reach and the body hull (unit P04).

docs/prep/safety-model.md sections 4.4 to 4.6, 6.4, 8 and 9.3 to 9.4; the
project design, sections 4.6, 4.8 and 4.10; red-team PHYS-05 and PHYS-07.
Polygons are shapely geometries (GEOS does the booleans).

**Every polygon errs on the safe side.** A curved set is drawn with straight
edges, so the kernel says which way each shape may err, and by how much:

* the robot's shapes (wheel discs, capsules, rotation sweeps, envelopes, the
  body hull) and keep-out regions built from discs **hold the exact set**: arcs
  are drawn with circumscribed edges (tangent to the circle), so a polygon may
  exceed the exact set by at most :data:`APPROX_TOL_MM`, never fall short;
* containment regions (:func:`reach_region`; the work areas of
  :mod:`goliath_prep.geom.workarea`) **lie inside the exact set**: arcs drawn
  with inscribed edges, at most :data:`APPROX_TOL_MM` short of it;
* an annulus's hole is drawn inscribed (a smaller hole, a larger annulus).

So a check that tests a robot shape against a keep-out, or for containment in
a work area, can only be stricter than the exact geometry, by at most the
tolerance (a slack reported from these polygons is low by at most that much).
Every radius also carries a pad of 1e-9 mm against the rounding of the
coordinates themselves. :data:`APPROX_TOL_MM` is a representation choice of
the kernel, not a safety parameter: whatever its value, the error is on the
safe side.

**The same numbers everywhere.** Directions come from a fixed table of unit
vectors at multiples of 360/8192 degrees, computed once with
:func:`goliath_prep.geom.footprint.unit` (correctly rounded, the same on every
platform) and extended by exact symmetries; a circle of radius r uses the
coarsest power-of-two division (at least 8 edges) that keeps r within the
tolerance. Arc end points use :func:`~goliath_prep.geom.footprint.unit` of the
end angles; the tangent points between two directions a and b are
``(a + b) / (1 + a.b)`` scaled by the radius: only + - * / and ``math.sqrt``,
which IEEE 754 rounds the same on every platform. GEOS results may differ
between GEOS versions in their vertex order, not in the sets they describe.

**Angles** are degrees, counter-clockwise from +X. An arc from ``ang0`` to
``ang1`` runs counter-clockwise when ``ang1 > ang0`` and clockwise when
``ang1 < ang0``; a sweep of 360 degrees or more is the whole circle.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from functools import lru_cache

import numpy as np
import shapely
from shapely.geometry import MultiPoint, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

from goliath_prep.core.types import FootprintSet
from goliath_prep.geom.footprint import (
    SupportPosition,
    Vec2,
    _non_negative,
    _point,
    _positive,
    _real,
    _ROUND_UP_GUARD_MM,
    body_point,
    centroid_offsets,
    rotation_radius,
    support_offset,
    support_offsets,
    support_positions,
    theta,
    unit,
)

__all__ = [
    "APPROX_TOL_MM",
    "annulus",
    "any_rotation_envelope",
    "arc_hull_points",
    "arc_sweep",
    "body_hull",
    "body_hull_local",
    "body_radius",
    "capsule",
    "convex_envelope",
    "disc",
    "grown_hull",
    "reach_region",
    "rotation_disc",
    "support_arc_hull",
    "wheel_capsules",
    "wheel_discs",
]

APPROX_TOL_MM = 0.02
"""The most a kernel polygon may differ from the exact set, in mm, always on the safe side (module documentation)."""

_PAD_MM = 1e-9  # against the rounding of coordinates
_GRID_N = 8192  # the finest division of the circle; a power of two
_MIN_EDGES = 8
_FULL_TURN_DEG = 360.0 - 1e-6  # a sweep this long or longer is drawn as the whole circle
_END_GUARD_DEG = 1e-7  # grid directions this close to an arc's end are left out (the end point is kept)
_TINY_ARC_MM = 1e-6  # (sweep in degrees) x radius below this: drawn as a capsule grown by this much


# ---------------------------------------------------------------------------
# Directions


@lru_cache(maxsize=1)
def _grid() -> np.ndarray:
    """Unit vectors at 360*i/N degrees, i = 0..N-1 (N = 8192), from the first octant by exact symmetries."""
    n = _GRID_N
    quarter, eighth = n // 4, n // 8
    octant = [unit(360.0 * i / n) for i in range(eighth + 1)]
    out = np.empty((n, 2), dtype=float)
    for i in range(n):
        q, j = divmod(i, quarter)
        if j <= eighth:
            x, y = octant[j]
        else:  # reflect about 45 degrees: u(90 - b) = (sin b, cos b)
            y, x = octant[quarter - j]
        for _ in range(q):  # turn by 90 degrees: (x, y) -> (-y, x), exact
            x, y = -y, x
        out[i, 0], out[i, 1] = x, y
    out.setflags(write=False)
    return out


def _edges_for(radius: float, tol: float) -> int:
    """The number of edges (a power of two, at least 8) that keeps a circle of ``radius`` within ``tol``.

    A regular n-gon circumscribed about the circle exceeds it by r (1/cos(pi/n) - 1),
    at most r (pi/n)^2 for pi/n <= 1; n >= pi sqrt(r / tol) makes that at most tol.
    """
    need = math.pi * math.sqrt(radius / tol)
    n = _MIN_EDGES
    while n < need:
        n *= 2
    if n > _GRID_N:
        raise ValueError("a radius of %r mm is too large for the kernel's tolerance of %r mm (at most %d edges per "
                         "circle)" % (radius, tol, _GRID_N))
    return n


def _tol(tol: object) -> float:
    return _positive(tol, "tol")


def _tangent_point(a: Sequence[float], b: Sequence[float]) -> Vec2:
    """Where the tangents to the unit circle at directions a and b meet: (a + b) / (1 + a.b)."""
    k = 1.0 + (a[0] * b[0] + a[1] * b[1])
    return ((a[0] + b[0]) / k, (a[1] + b[1]) / k)


def _circle_dirs(n: int) -> np.ndarray:
    return _grid()[:: _GRID_N // n]


def _outer_ring(centre: Vec2, radius: float, n: int) -> list[Vec2]:
    """The vertices of a regular n-gon circumscribed about the circle (it holds the disc)."""
    dirs = _circle_dirs(n)
    r = radius + _PAD_MM
    cx, cy = centre
    out = []
    for i in range(n):
        tx, ty = _tangent_point(dirs[i], dirs[(i + 1) % n])
        out.append((cx + r * tx, cy + r * ty))
    return out


def _inner_ring(centre: Vec2, radius: float, n: int) -> list[Vec2]:
    """The vertices of a regular n-gon inscribed in the circle (it lies inside the disc)."""
    r = radius - _PAD_MM if radius > 2 * _PAD_MM else radius / 2.0
    cx, cy = centre
    return [(cx + r * x, cy + r * y) for x, y in _circle_dirs(n)]


def _arc_dirs(ang0: float, ang1: float, n: int) -> list[Vec2]:
    """Unit directions along the arc from ang0 to ang1 (|ang1 - ang0| < 360): u(ang0), the n-grid
    directions strictly between, u(ang1)."""
    step = 360.0 / n  # exact: n is a power of two
    stride = _GRID_N // n
    grid = _grid()
    dirs: list[Vec2] = [unit(ang0)]
    if ang1 > ang0:
        k, last = math.floor(ang0 / step) + 1, math.ceil(ang1 / step) - 1
        ks = range(k, last + 1)
    else:
        k, last = math.ceil(ang0 / step) - 1, math.floor(ang1 / step) + 1
        ks = range(k, last - 1, -1)
    for k in ks:
        angle = k * step
        if abs(angle - ang0) < _END_GUARD_DEG or abs(ang1 - angle) < _END_GUARD_DEG:
            continue
        v = grid[(k * stride) % _GRID_N]
        dirs.append((float(v[0]), float(v[1])))
    dirs.append(unit(ang1))
    return dirs


def _outer_chain(centre: Vec2, radius: float, dirs: list[Vec2]) -> list[Vec2]:
    """The arc's end points joined by tangent segments: the chain lies on or outside the circle."""
    cx, cy = centre
    r = radius + _PAD_MM
    out = [(cx + radius * dirs[0][0], cy + radius * dirs[0][1])]
    for a, b in zip(dirs, dirs[1:]):
        tx, ty = _tangent_point(a, b)
        out.append((cx + r * tx, cy + r * ty))
    out.append((cx + radius * dirs[-1][0], cy + radius * dirs[-1][1]))
    return out


def _hull(points: Sequence[Vec2] | np.ndarray) -> BaseGeometry:
    return MultiPoint(np.asarray(points, dtype=float)).convex_hull


def _polygon(geom: BaseGeometry, what: str) -> Polygon:
    if not isinstance(geom, Polygon) or geom.is_empty:
        raise RuntimeError("%s did not come out as one polygon (%s): a kernel bug, not an input error"
                           % (what, geom.geom_type))
    return geom


# ---------------------------------------------------------------------------
# Discs, capsules, annuli


def disc(p: Vec2, r: float, *, outer: bool = True, tol: float = APPROX_TOL_MM) -> Polygon:
    """The disc of radius ``r`` about ``p``: circumscribed (holds the disc) or, with ``outer=False``, inscribed."""
    centre = _point(p, "disc centre")
    radius = _positive(r, "disc radius")
    n = _edges_for(radius, _tol(tol))
    ring = _outer_ring(centre, radius, n) if outer else _inner_ring(centre, radius, n)
    return Polygon(ring)


def grown_hull(points: Sequence[Vec2], r: float, *, tol: float = APPROX_TOL_MM) -> Polygon:
    """conv(points) grown by ``r``: every point within ``r`` of the convex hull of ``points``; holds the exact set.

    The Minkowski sum of a convex set and a disc. :func:`capsule` is the hull
    of two points grown, a tower zone the hull of a triangle's corners grown.
    """
    pts = [_point(q, "point") for q in points]
    if not pts:
        raise ValueError("grown_hull needs at least one point")
    radius = _positive(r, "radius")
    ring = _outer_ring((0.0, 0.0), radius, _edges_for(radius, _tol(tol)))
    return _polygon(_hull(_minkowski_convex(_ccw_corners(_hull(pts)), ring)), "grown hull")


def _ccw_corners(hull: BaseGeometry) -> list[Vec2]:
    """The corners of a convex hull (a polygon, a segment or a point), counter-clockwise, without repeats."""
    if isinstance(hull, Polygon):
        hull = orient(hull, sign=1.0)
        coords = list(hull.exterior.coords)[:-1]
    else:
        coords = list(shapely.get_coordinates(hull))
    out: list[Vec2] = []
    for x, y in coords:
        q = (float(x), float(y))
        if not out or q != out[-1]:
            out.append(q)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def _minkowski_convex(p: list[Vec2], q: list[Vec2]) -> list[Vec2]:
    """The corners of P + Q for convex P and Q given counter-clockwise: the classic merge of their edges by
    direction, O(n + m). Every corner is a sum p_i + q_j, so the hull of the result is the Minkowski sum."""
    def lowest(poly: list[Vec2]) -> list[Vec2]:
        k = min(range(len(poly)), key=lambda i: (poly[i][1], poly[i][0]))
        return poly[k:] + poly[:k]

    p, q = lowest(p), lowest(q)
    n, m = len(p), len(q)
    out: list[Vec2] = []
    i = j = 0
    while i < n or j < m:
        a, b = p[i % n], q[j % m]
        out.append((a[0] + b[0], a[1] + b[1]))
        ex, ey = p[(i + 1) % n][0] - a[0], p[(i + 1) % n][1] - a[1]
        fx, fy = q[(j + 1) % m][0] - b[0], q[(j + 1) % m][1] - b[1]
        cross = ex * fy - ey * fx
        if j >= m or (i < n and cross > 0.0):
            i += 1
        elif i >= n or cross < 0.0:
            j += 1
        else:
            i += 1
            j += 1
    return out


def capsule(a: Vec2, b: Vec2, r: float, *, tol: float = APPROX_TOL_MM) -> Polygon:
    """Every point within ``r`` of the segment a-b (a wheel disc swept along a straight piece); holds the exact set."""
    pa, pb = _point(a, "capsule start"), _point(b, "capsule end")
    radius = _positive(r, "capsule radius")
    if pa == pb:
        return Polygon(_outer_ring(pa, radius, _edges_for(radius, _tol(tol))))
    return grown_hull([pa, pb], radius, tol=tol)


def annulus(p: Vec2, r_in: float, r_out: float, *, tol: float = APPROX_TOL_MM) -> Polygon:
    """The points at a distance from ``p`` between ``r_in`` and ``r_out``; holds the exact set.

    A non-positive ``r_in`` (r_min - rho <= 0) leaves no hole: the disc of radius ``r_out``.
    """
    centre = _point(p, "annulus centre")
    inner = _real(r_in, "annulus inner radius")
    outer = _positive(r_out, "annulus outer radius")
    if inner >= outer:
        raise ValueError("annulus inner radius %r must be below its outer radius %r" % (r_in, r_out))
    tol = _tol(tol)
    shell = _outer_ring(centre, outer, _edges_for(outer, tol))
    if inner <= 0.0:
        return Polygon(shell)
    hole = _inner_ring(centre, inner, _edges_for(inner, tol))
    return Polygon(shell, [hole])


# ---------------------------------------------------------------------------
# Rotation sweeps (safety-model 9.3) and envelopes (9.4)


def arc_hull_points(centre: Vec2, radius: float, ang0: float, ang1: float, *,
                    tol: float = APPROX_TOL_MM) -> tuple[Vec2, ...]:
    """Points whose convex hull holds the arc of ``radius`` about ``centre`` from ``ang0`` to ``ang1``.

    The arc's end points and the tangent points between them; a sweep of 360
    degrees or more gives a circumscribed polygon of the whole circle.
    """
    c = _point(centre, "arc centre")
    radius = _positive(radius, "arc radius")
    a0, a1 = _real(ang0, "arc start angle"), _real(ang1, "arc end angle")
    n = _edges_for(radius, _tol(tol))
    if abs(a1 - a0) >= _FULL_TURN_DEG:
        return tuple(_outer_ring(c, radius, n))
    if a1 == a0:
        e = unit(a0)
        return ((c[0] + radius * e[0], c[1] + radius * e[1]),)
    return tuple(_outer_chain(c, radius, _arc_dirs(a0, a1, n)))


def support_arc_hull(r: float, delta: float, theta0: float, theta1: float, *,
                     tol: float = APPROX_TOL_MM) -> tuple[Vec2, ...]:
    """Offsets whose convex hull holds a support's offsets d(theta) = -r u(theta + delta) for theta from
    ``theta0`` to ``theta1`` (counter-clockwise when ``theta1 > theta0``): the ``arc_k`` of safety-model 9.4."""
    support_offset(r, delta, 0.0)  # validates r and delta
    shift = _real(delta, "delta") + 180.0  # -u(a) = u(a + 180)
    return arc_hull_points((0.0, 0.0), r, _real(theta0, "theta0") + shift, _real(theta1, "theta1") + shift, tol=tol)


def arc_sweep(pivot: Vec2, radius: float, ang0: float, ang1: float, rho: float, *,
              tol: float = APPROX_TOL_MM) -> Polygon:
    """The region a disc of radius ``rho`` sweeps while its centre runs along the arc of ``radius`` about
    ``pivot`` from ``ang0`` to ``ang1`` (a wheel during a rotation about ``pivot``); holds the exact set.

    The exact set is the annular sector between ``radius - rho`` and
    ``radius + rho`` over the swept angles, with a disc of radius ``rho`` at
    each end of the arc.
    """
    c = _point(pivot, "pivot")
    radius = _non_negative(radius, "sweep radius")
    a0, a1 = _real(ang0, "sweep start angle"), _real(ang1, "sweep end angle")
    rho = _positive(rho, "rho")
    tol = _tol(tol)
    if radius == 0.0:
        return disc(c, rho, tol=tol)
    if abs(a1 - a0) * radius < _TINY_ARC_MM:  # no turn to speak of: the capsule between the ends holds it
        e0, e1 = unit(a0), unit(a1)
        return capsule((c[0] + radius * e0[0], c[1] + radius * e0[1]), (c[0] + radius * e1[0], c[1] + radius * e1[1]),
                       rho + _TINY_ARC_MM, tol=tol)
    if abs(a1 - a0) >= _FULL_TURN_DEG:
        if radius - rho <= 0.0:
            return disc(c, radius + rho, tol=tol)
        return annulus(c, radius - rho, radius + rho, tol=tol)
    n = _edges_for(radius + rho, tol)
    dirs = _arc_dirs(a0, a1, n)
    ring = _outer_chain(c, radius + rho, dirs)
    inner = radius - rho - _PAD_MM
    if inner > 0.0:
        ring.extend((c[0] + inner * x, c[1] + inner * y) for x, y in reversed(dirs))
    else:
        ring.append(c)
    ends = [disc((c[0] + radius * d[0], c[1] + radius * d[1]), rho, tol=tol) for d in (dirs[0], dirs[-1])]
    return _polygon(unary_union([Polygon(ring)] + ends), "arc sweep")


def convex_envelope(pa: Vec2, pb: Vec2, offsets: Sequence[Vec2], rho: float, *,
                    tol: float = APPROX_TOL_MM) -> Polygon:
    """conv({pa, pb} + offsets) grown by ``rho``: the envelope E_k of a line that rotates and translates together.

    ``offsets`` must be a set whose convex hull holds the support's offsets
    along the rotation (:func:`support_arc_hull`, over both directions while the
    direction is not a fact). Since conv(A + B) = conv(A) + conv(B), the result
    holds every motion in which the tool point runs along the segment pa-pb and
    the heading runs along the arc, in any blend (safety-model 9.4).
    """
    a, b = _point(pa, "pa"), _point(pb, "pb")
    pts = [_point(o, "offset") for o in offsets]
    if not pts:
        raise ValueError("convex_envelope needs at least one offset")
    return grown_hull([(a[0] + x, a[1] + y) for x, y in pts] + [(b[0] + x, b[1] + y) for x, y in pts], rho, tol=tol)


# ---------------------------------------------------------------------------
# Wheels, rotation disc and the any-rotation envelope (safety-model 4.4, 4.6, 8, 9.4)


def wheel_discs(p: Vec2, c: int, fp: FootprintSet, rho: float, *,
                tol: float = APPROX_TOL_MM) -> tuple[tuple[SupportPosition, Polygon], ...]:
    """The wheel disc of radius ``rho`` at every support position of ``fp``, tool point ``p``, ``C = c``."""
    px, py = _point(p, "tool point")
    return tuple((pos, disc((px + d[0], py + d[1]), rho, tol=tol)) for pos, d in support_offsets(c, fp))


def wheel_capsules(a: Vec2, b: Vec2, c: int, fp: FootprintSet, rho: float, *,
                   tol: float = APPROX_TOL_MM) -> tuple[tuple[SupportPosition, Polygon], ...]:
    """Each wheel's disc swept while the tool point runs straight from ``a`` to ``b`` at ``C = c``.

    The heading is fixed, so each wheel's centre runs along the segment
    translated by its offset d_k (safety-model 4.4).
    """
    ax, ay = _point(a, "piece start")
    bx, by = _point(b, "piece end")
    return tuple((pos, capsule((ax + d[0], ay + d[1]), (bx + d[0], by + d[1]), rho, tol=tol))
                 for pos, d in support_offsets(c, fp))


def rotation_disc(p: Vec2, fp: FootprintSet, rho: float, *, tol: float = APPROX_TOL_MM) -> Polygon:
    """disc(p, R_rot + rho): every wheel during a turn at ``p``, whatever the turn (safety-model 8, 9.3)."""
    return disc(p, rotation_radius(fp) + _positive(rho, "rho"), tol=tol)


def any_rotation_envelope(pa: Vec2, pb: Vec2, fp: FootprintSet, rho: float, *,
                          tol: float = APPROX_TOL_MM) -> Polygon:
    """conv(disc(pa, R_rot + rho) | disc(pb, R_rot + rho)): every wheel on a line that turns and moves together
    while the rotation model is not a fact (safety-model 9.4 case 1; 9.5)."""
    return capsule(pa, pb, rotation_radius(fp) + _positive(rho, "rho"), tol=tol)


# ---------------------------------------------------------------------------
# Wire reach (safety-model 6.4; red-team PHYS-05)


def reach_region(towers: Sequence[Vec2], reach_max: float, *, tol: float = APPROX_TOL_MM) -> Polygon:
    """The points within ``reach_max`` of every tower (tower 1 and tower 2 at each placement); lies inside the
    exact set. Empty when no point is within reach of all of them."""
    centres = [_point(t, "tower") for t in towers]
    if not centres:
        raise ValueError("reach_region needs at least one tower")
    reach = _positive(reach_max, "reach_max")
    n = _edges_for(reach, _tol(tol))
    region: BaseGeometry = Polygon(_inner_ring(centres[0], reach, n))
    for centre in centres[1:]:
        region = region.intersection(Polygon(_inner_ring(centre, reach, n)))
    if region.is_empty:
        return Polygon()
    return _polygon(region, "reach region")


# ---------------------------------------------------------------------------
# Body hull (safety-model 4.5)


def _hull_inputs(hull_margin: float | None, outline: Sequence[Vec2] | None) -> tuple[float | None, list[Vec2] | None]:
    if (hull_margin is None) == (outline is None):
        raise ValueError("give exactly one of hull_margin (the default hull: the wheel discs grown by it) and "
                         "outline (the measured body outline, (back, right) pairs in mm)")
    if hull_margin is not None:
        return _non_negative(hull_margin, "hull_margin"), None
    points = [_point(q, "outline point") for q in outline]  # type: ignore[union-attr]
    if not points:
        raise ValueError("the measured outline has no point")
    return None, [body_point(back, right) for back, right in points]


def body_hull_local(fp: FootprintSet, rho: float, *, hull_margin: float | None = None,
                    outline: Sequence[Vec2] | None = None, tol: float = APPROX_TOL_MM) -> Polygon:
    """The body hull in the kernel's local frame (offsets from the tool point at ``C = 0``).

    Default (``hull_margin``, DEFAULT_UNMEASURED until rung R0b): the convex
    hull of the wheel discs grown by the margin. Measured (``outline``, the body
    outline as (back, right) pairs from T, machine.toml): the convex hull of the
    wheel discs and the outline. Every support position of ``fp`` counts.
    Place it with :func:`body_hull`.
    """
    margin, local_outline = _hull_inputs(hull_margin, outline)
    rho = _positive(rho, "rho")
    radius = rho + (margin or 0.0)
    n = _edges_for(radius, _tol(tol))
    t = theta(0)
    points: list[Vec2] = []
    for pos in support_positions(fp):
        points.extend(_outer_ring(support_offset(pos.r, pos.delta, t), radius, n))
    points.extend(local_outline or ())
    return _polygon(_hull(points), "body hull")


def body_hull(p: Vec2, c: int, local: Polygon) -> Polygon:
    """The local body hull placed at tool point ``p`` and ``C = c`` (turned by ``c`` degrees about the tool)."""
    px, py = _point(p, "tool point")
    theta(c)  # validates c
    if not isinstance(local, Polygon) or local.is_empty:
        raise TypeError("body_hull needs the local hull Polygon of body_hull_local(), got %r" % (local,))
    ux, uy = unit(float(c))

    def place(ring: Sequence[Sequence[float]]) -> list[Vec2]:
        return [(px + (x * ux - y * uy), py + (x * uy + y * ux)) for x, y in ring]

    return Polygon(place(local.exterior.coords), [place(r.coords) for r in local.interiors])


def body_radius(fp: FootprintSet, rho: float, *, hull_margin: float | None = None,
                outline: Sequence[Vec2] | None = None, tol: float = APPROX_TOL_MM) -> float:
    """R_body: a disc about the tool point that holds the body hull during any turn about a pivot between the
    tool point and the support centroid (safety-model 4.5).

    With the default hull, the bound R_rot + rho + m_body (519 mm with the
    defaults). With a measured outline, the largest over the hull's vertices h
    and every support centroid c of |h| and |c| + |h - c|, rounded up to whole
    millimetres.
    """
    margin, _ = _hull_inputs(hull_margin, outline)
    rho = _positive(rho, "rho")
    if margin is not None:
        return rotation_radius(fp) + rho + margin
    hull = body_hull_local(fp, rho, outline=outline, tol=tol)
    best = 0.0
    for cx, cy in centroid_offsets(fp, 0):
        d_c = math.sqrt(cx * cx + cy * cy)
        for hx, hy in hull.exterior.coords:
            far = d_c + math.sqrt((hx - cx) * (hx - cx) + (hy - cy) * (hy - cy))
            best = max(best, far, math.sqrt(hx * hx + hy * hy))
    return float(math.ceil(best + _ROUND_UP_GUARD_MM))
