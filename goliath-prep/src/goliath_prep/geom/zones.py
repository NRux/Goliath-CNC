# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Tower keep-out zones at both tower-2 placements, tower bases and anchor points (unit P04).

Spec: docs/protocol/08-head-angle-and-cut-order.md *Tower keep-out zones*;
docs/prep/safety-model.md sections 5.1 to 5.3.

* **Placements.** Tower 1 stands at the origin. Tower 2 stands on the Y axis
  (``tower2 = "Y"``: ``SET T2 POSITION;0``, spec 08 ``tower2 = 0``, corner
  (0, H)) or on the X axis (``"X"``: ``;1``, corner (W, 0)). Because the vendor
  located tower 2 two different ways (spec 08 §Caveats, "Where tower 2 is"),
  every zone is placed at the panel corner on that axis **and** at the
  distance ``d_t`` along the axis (the measured ``INFO;27`` distance at job
  time; at the desk, a declared one, or None for the corner alone).
* **Zones.** ``Z1(L) = (0, 0), (0, L), (L, 0)``; ``Z2(L)`` at placement t is
  ``(0, t - L), (0, t), (L, t)`` on Y and ``(t - L, 0), (t, 0), (t, L)`` on X,
  in spec 08's vertex order (:func:`zone_triangles`). Wheels keep out of the
  150 mm zones (W-TOWER), the tool out of the 155 mm ones (T-TOWER).
* **Grown, closed.** The kernel's zones are the triangles grown by a margin
  (``m_tower``; at the desk also ``geom_tol``), as closed sets that hold the
  exact grown triangles (:mod:`goliath_prep.geom.sweeps`). The vendor's edge
  behaviour (which edges count as inside, by vertex order) is irrelevant to
  them; :data:`vendor_triangle_contains` keeps spec 08's exact test for the
  vendor-floor module.
* **Tower bases** are discs about each tower point; **anchor points** are the
  centroids of the 150 mm triangles (safety-model 5.3).

``margin`` and ``radius`` have no defaults: the caller passes the value in use
(with its provenance in the context). The legs default to spec 08's values
(docs/prep/params.json ``tower_zone_leg_wheel`` and ``tower_zone_leg_tool``,
SPEC).
"""
from __future__ import annotations

from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from goliath_prep.core.types import TOWER2_AXES, Tower2
from goliath_prep.defaults import SPEC_PARAMS
from goliath_prep.geom.footprint import Vec2, _non_negative, _positive
from goliath_prep.geom.sweeps import APPROX_TOL_MM, disc, grown_hull
from goliath_prep.heading_vendor import triangle_contains

__all__ = [
    "TOWER_ZONE_LEG_TOOL_MM",
    "TOWER_ZONE_LEG_WHEEL_MM",
    "Triangle",
    "anchor_points",
    "tool_zones",
    "tower2_placements",
    "tower_bases",
    "tower_points",
    "vendor_triangle_contains",
    "wheel_zones",
    "zone_triangles",
]

Triangle = tuple[Vec2, Vec2, Vec2]

TOWER_ZONE_LEG_WHEEL_MM: float = float(SPEC_PARAMS["tower_zone_leg_wheel"].value)
"""150 mm: the wheel zone's legs (spec 08 §Tower keep-out zones; SPEC)."""
TOWER_ZONE_LEG_TOOL_MM: float = float(SPEC_PARAMS["tower_zone_leg_tool"].value)
"""155 mm: the toolpath zone's legs (spec 08 §Tower keep-out zones, the pre-check; SPEC)."""

vendor_triangle_contains = triangle_contains
"""Spec 08's exact point-in-triangle test, edge behaviour by vertex order: the one in
:mod:`goliath_prep.heading_vendor`, kept for the vendor-floor module (not for the kernel's own checks)."""


def _axis(tower2: object) -> str:
    if not isinstance(tower2, str):
        raise TypeError("tower2 must be 'X' or 'Y' (the axis tower 2 stands on), got %r" % (tower2,))
    if tower2 not in TOWER2_AXES:
        raise ValueError("tower2 must be 'X' (corner (W, 0)) or 'Y' (corner (0, H)), got %r" % (tower2,))
    return tower2


def tower2_placements(W: float, H: float, tower2: Tower2, d_t: float | None) -> tuple[float, ...]:
    """Where tower 2 stands along its axis: the panel corner (W on X, H on Y), then ``d_t`` if it differs."""
    width = _positive(W, "panel width W")
    height = _positive(H, "panel height H")
    corner = width if _axis(tower2) == "X" else height
    if d_t is None:
        return (corner,)
    distance = _positive(d_t, "tower-2 distance d_t")
    return (corner,) if distance == corner else (corner, distance)


def tower_points(W: float, H: float, tower2: Tower2, d_t: float | None) -> tuple[Vec2, ...]:
    """Tower 1 at the origin, then tower 2 at each placement of :func:`tower2_placements`."""
    on_x = _axis(tower2) == "X"
    return ((0.0, 0.0),) + tuple((t, 0.0) if on_x else (0.0, t) for t in tower2_placements(W, H, tower2, d_t))


def zone_triangles(W: float, H: float, tower2: Tower2, d_t: float | None, *, leg: float) -> tuple[Triangle, ...]:
    """Z1(leg), then Z2(leg) at each tower-2 placement, with their vertices in spec 08's order."""
    L = _positive(leg, "zone leg")
    out: list[Triangle] = [((0.0, 0.0), (0.0, L), (L, 0.0))]
    on_x = _axis(tower2) == "X"
    for t in tower2_placements(W, H, tower2, d_t):
        out.append(((t - L, 0.0), (t, 0.0), (t, L)) if on_x else ((0.0, t - L), (0.0, t), (L, t)))
    return tuple(out)


def _grown(triangles: tuple[Triangle, ...], margin: object, tol: float) -> Polygon | MultiPolygon:
    m = _non_negative(margin, "zone margin")
    if m == 0.0:
        return unary_union([Polygon(tri) for tri in triangles])
    return unary_union([grown_hull(tri, m, tol=tol) for tri in triangles])


def wheel_zones(W: float, H: float, tower2: Tower2, d_t: float | None, *, leg: float = TOWER_ZONE_LEG_WHEEL_MM,
                margin: float, tol: float = APPROX_TOL_MM) -> Polygon | MultiPolygon:
    """The wheel keep-out zones Z1 and Z2 at both tower-2 placements, grown by ``margin`` (W-TOWER).

    A closed set holding every point within ``margin`` of a triangle (it may
    exceed that by at most ``tol``). Z1 and Z2 usually do not touch, so the
    result is a MultiPolygon.
    """
    return _grown(zone_triangles(W, H, tower2, d_t, leg=leg), margin, tol)


def tool_zones(W: float, H: float, tower2: Tower2, d_t: float | None, *, leg: float = TOWER_ZONE_LEG_TOOL_MM,
               margin: float, tol: float = APPROX_TOL_MM) -> Polygon | MultiPolygon:
    """The tool keep-out zones (155 mm legs) at both tower-2 placements, grown by ``margin`` (T-TOWER)."""
    return _grown(zone_triangles(W, H, tower2, d_t, leg=leg), margin, tol)


def tower_bases(W: float, H: float, tower2: Tower2, d_t: float | None, *, radius: float,
                tol: float = APPROX_TOL_MM) -> Polygon | MultiPolygon:
    """A disc of ``radius`` about tower 1 and about tower 2 at each placement (B-TOWER, T-TOWER); holds them."""
    r = _positive(radius, "tower base radius")
    return unary_union([disc(t, r, tol=tol) for t in tower_points(W, H, tower2, d_t)])


def anchor_points(W: float, H: float, tower2: Tower2, d_t: float | None) -> tuple[Vec2, tuple[Vec2, ...]]:
    """``a1``, the centroid of Z1(150), and ``a2``, the centroid of Z2(150) at each tower-2 placement.

    They lie inside the 155 mm tool zones, so they are never removed; the
    structure model uses them to tell held material from loose material
    (safety-model 5.3, 7.4).
    """
    third = TOWER_ZONE_LEG_WHEEL_MM / 3.0
    on_x = _axis(tower2) == "X"
    a2 = tuple((t - third, third) if on_x else (third, t - third) for t in tower2_placements(W, H, tower2, d_t))
    return (third, third), a2
