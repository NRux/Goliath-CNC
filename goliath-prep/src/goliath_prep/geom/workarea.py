# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Work areas: the panel at the desk and at job time, per-edge margins and support strips (unit P04).

docs/prep/safety-model.md sections 6.1 to 6.3 and 12; the project design,
section 4.6 (*Work areas*); red-team C-16 (the tower-baseline edge keeps its
own margin, U-50).

* **The panel.** At the desk, the declared rectangle shrunk by ``geom_tol``
  (:func:`desk_panel`); at job time, the measured panel clipped to the
  integers sent in ``SET PANEL SIZE`` (:func:`job_panel`).
* **Sides.** Every edge of a panel faces one of the frame's sides ``left``
  (-X), ``right`` (+X), ``bottom`` (-Y) or ``top`` (+Y): the one its outward
  normal points closest to (an edge at exactly 45 degrees faces both, and gets
  the larger margin). The **tower baseline** is the side from tower 1 to
  tower 2: ``left`` (x = 0) when tower 2 stands on the Y axis, ``bottom``
  (y = 0) on the X axis (:func:`baseline_side`).
* **Per-edge margins.** ``WA = (P u S_decl)`` minus, for every edge e, the
  points closer than its side's margin m_e to that edge (as a segment). With
  one margin on every side that is the erosion ``(P u S_decl) - m``; with
  different margins it keeps each edge's own (:func:`edge_margins`: the
  baseline edge keeps ``m_edge_baseline`` until rung R9 has tested that edge).
  Edges of a hole in the panel keep the largest margin.
* **Support strips** (relaxation R-SUPPORT): ``S_decl`` extends the panel
  beyond a side by the declared width, along every edge facing that side (so
  a slanted measured edge leaves no gap). No overhang by default: no strip
  unless declared.
* **Wheel and tool.** The wheel work area (W-AREA) uses the wheel margins and
  the strips; the tool work area (T-AREA) the tool margin and no strip.

A work area is a containment region: the polygon lies inside the exact set,
at most :data:`~goliath_prep.geom.sweeps.APPROX_TOL_MM` short of it (the bands
removed hold the exact bands). A work area may be empty, or fall apart in
pieces: that is a result, which the checks then refuse against, not an error.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType

from shapely.geometry import MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

from goliath_prep.core.types import TOWER2_AXES, Tower2
from goliath_prep.geom.footprint import Vec2, _non_negative, _positive, _real
from goliath_prep.geom.sweeps import APPROX_TOL_MM, capsule

__all__ = [
    "SIDES",
    "baseline_side",
    "desk_panel",
    "edge_margins",
    "job_panel",
    "support_strips",
    "work_area_tool",
    "work_area_wheel",
]

SIDES: tuple[str, ...] = ("left", "right", "bottom", "top")
"""The frame's sides: left (x = 0), right (x = W), bottom (y = 0), top (y = H) (job.toml [supports])."""

_DIRECTION: Mapping[str, Vec2] = MappingProxyType(
    {"left": (-1.0, 0.0), "right": (1.0, 0.0), "bottom": (0.0, -1.0), "top": (0.0, 1.0)})
_COS45 = math.sqrt(0.5)
_FACING_EPS = 1e-12
_MARGIN_KEYS = frozenset({"m_edge", "m_edge_baseline"})


# ---------------------------------------------------------------------------
# Inputs


def _panel(value: object, what: str = "panel") -> Polygon:
    if not isinstance(value, Polygon):
        raise TypeError("%s must be a shapely Polygon, got %r" % (what, value))
    if value.is_empty or value.area <= 0.0:
        raise ValueError("%s is empty" % what)
    if not value.is_valid:
        raise ValueError("%s is not a valid polygon (for example a self-intersecting outline)" % what)
    return orient(value, sign=1.0)


def _side_margins(m_edge: object) -> dict[str, float]:
    if isinstance(m_edge, Mapping):
        unknown = sorted(set(m_edge) - set(SIDES))
        missing = [side for side in SIDES if side not in m_edge]
        if unknown or missing:
            raise ValueError("margins per edge need exactly the sides %s (missing %s, unknown %s)"
                             % (", ".join(SIDES), missing, unknown))
        return {side: _non_negative(m_edge[side], "margin on the %s side" % side) for side in SIDES}
    m = _non_negative(m_edge, "edge margin")
    return {side: m for side in SIDES}


def _facing(a: Vec2, b: Vec2, *, strictly: bool = False) -> tuple[str, ...]:
    """The sides an edge a->b of a counter-clockwise ring faces (by its outward normal)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.sqrt(dx * dx + dy * dy)
    nx, ny = dy / length, -dx / length
    bound = _COS45 + _FACING_EPS if strictly else _COS45 - _FACING_EPS
    return tuple(side for side in SIDES if nx * _DIRECTION[side][0] + ny * _DIRECTION[side][1] >= bound)


def _edges(ring: object) -> list[tuple[Vec2, Vec2]]:
    coords = [(float(x), float(y)) for x, y in ring.coords]  # type: ignore[attr-defined]
    return [(a, b) for a, b in zip(coords, coords[1:]) if a != b]


def _parts(geom: BaseGeometry) -> list[Polygon]:
    if isinstance(geom, Polygon):
        return [] if geom.is_empty else [geom]
    if isinstance(geom, MultiPolygon):
        return [g for g in geom.geoms if not g.is_empty]
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]


# ---------------------------------------------------------------------------
# Sides and margins


def baseline_side(tower2: Tower2) -> str:
    """The side the tower baseline lies on: ``left`` (x = 0) with tower 2 on Y, ``bottom`` (y = 0) on X."""
    if not isinstance(tower2, str) or tower2 not in TOWER2_AXES:
        raise ValueError("tower2 must be 'X' or 'Y', got %r" % (tower2,))
    return "left" if tower2 == "Y" else "bottom"


def edge_margins(panel: Polygon, tower2: Tower2,
                 margins: Mapping[str, float | Mapping[str, float]]) -> Mapping[str, float]:
    """The wheel edge margin per side of ``panel`` (red-team C-16; safety-model 1.4 and 6.2).

    ``margins`` holds exactly ``m_edge`` and ``m_edge_baseline`` (the
    tower-baseline edge, until rung R9 has tested it). ``m_edge`` is one margin
    for every side, or a mapping with each of :data:`SIDES`, because rung R9
    lowers the margin per tested edge (the gate ``near_edge_allowed`` lists the
    edges it covered). The baseline side gets the larger of its ``m_edge`` and
    ``m_edge_baseline``: an owner who tightened ``m_edge`` tightens the baseline
    edge too. A panel without an edge facing the baseline side is not a panel
    the towers can stand on, and is refused.
    """
    poly = _panel(panel)
    if not isinstance(margins, Mapping):
        raise TypeError("margins must map m_edge and m_edge_baseline to mm, got %r" % (margins,))
    if set(margins) != _MARGIN_KEYS:
        raise ValueError("margins needs exactly m_edge and m_edge_baseline, got %s" % sorted(margins))
    per_side = _side_margins(margins["m_edge"])
    m_baseline = _non_negative(margins["m_edge_baseline"], "m_edge_baseline")
    base = baseline_side(tower2)
    if not any(base in _facing(a, b) for a, b in _edges(poly.exterior)):
        raise ValueError("the panel has no edge facing the %s side, where the tower baseline runs with tower 2 on "
                         "the %s axis: check the panel outline and the tower-2 axis" % (base, tower2))
    out = dict(per_side)
    out[base] = max(per_side[base], m_baseline)
    return MappingProxyType(out)


# ---------------------------------------------------------------------------
# Panels


def desk_panel(W: float, H: float, geom_tol: float) -> Polygon:
    """``P_work`` at the desk: the declared rectangle [0, W] x [0, H] shrunk by ``geom_tol`` (safety-model 6.1)."""
    width, height = _positive(W, "panel width W"), _positive(H, "panel height H")
    tol = _non_negative(geom_tol, "geom_tol")
    if width <= 2 * tol or height <= 2 * tol:
        raise ValueError("a %r x %r mm panel shrunk by geom_tol = %r mm on every side leaves nothing"
                         % (W, H, geom_tol))
    return box(tol, tol, width - tol, height - tol)


def job_panel(measured: Polygon, set_panel_size: tuple[int, int]) -> Polygon:
    """``P_work`` at job time: the measured panel clipped to [0, x] x [0, y], x and y the ``SET PANEL SIZE``
    integers (safety-model 12, step 4)."""
    poly = _panel(measured, "the measured panel")
    if not isinstance(set_panel_size, (tuple, list)) or len(set_panel_size) != 2:
        raise TypeError("set_panel_size must be the two integers (x, y) sent, got %r" % (set_panel_size,))
    for value in set_panel_size:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("set_panel_size must be the two integers (x, y) sent, got %r" % (set_panel_size,))
        if value < 1:
            raise ValueError("set_panel_size values must be at least 1 mm, got %r" % (set_panel_size,))
    clipped = poly.intersection(box(0.0, 0.0, float(set_panel_size[0]), float(set_panel_size[1])))
    pieces = _parts(clipped)
    if len(pieces) != 1:
        raise ValueError("the measured panel clipped to SET PANEL SIZE %r is %s, not one piece"
                         % (tuple(set_panel_size), "empty" if not pieces else "%d pieces" % len(pieces)))
    return orient(pieces[0], sign=1.0)


def support_strips(panel: Polygon, supports_decl: Mapping[str, float] | None) -> Polygon | MultiPolygon:
    """The declared support strips beyond ``panel``'s sides (relaxation R-SUPPORT); empty when none is declared.

    ``supports_decl`` maps a side to the strip's width in mm (0 or absent: no
    strip; the values of job.toml ``[supports]``). Each edge strictly facing a
    side with a strip is extended by the width in that side's direction; a
    strip declared on a side that no edge faces is refused, never dropped.
    """
    poly = _panel(panel)
    if supports_decl is None:
        return Polygon()
    if not isinstance(supports_decl, Mapping):
        raise TypeError("supports_decl must map sides to widths in mm, got %r" % (supports_decl,))
    unknown = sorted(set(supports_decl) - set(SIDES))
    if unknown:
        raise ValueError("support strips on unknown sides %s; the sides are %s" % (unknown, ", ".join(SIDES)))
    widths = {side: _non_negative(supports_decl.get(side, 0.0), "support strip width on the %s side" % side)
              for side in SIDES}
    quads = []
    extended = set()
    for a, b in _edges(poly.exterior):
        for side in _facing(a, b, strictly=True):
            w = widths[side]
            if w > 0.0:
                dx, dy = _DIRECTION[side][0] * w, _DIRECTION[side][1] * w
                quads.append(Polygon([a, b, (b[0] + dx, b[1] + dy), (a[0] + dx, a[1] + dy)]))
                extended.add(side)
    unused = [side for side in SIDES if widths[side] > 0.0 and side not in extended]
    if unused:
        # a declared strip that nothing would use must not vanish silently
        raise ValueError("support strips are declared on the %s side%s, but no edge of the panel faces %s"
                         % (", ".join(unused), "" if len(unused) == 1 else "s", "it" if len(unused) == 1 else "them"))
    if not quads:
        return Polygon()
    return unary_union(quads)


def _work_area(region: BaseGeometry, margins: Mapping[str, float], tol: float) -> Polygon | MultiPolygon:
    largest = max(margins.values())
    bands = []
    for part in _parts(region):
        part = orient(part, sign=1.0)
        for a, b in _edges(part.exterior):
            m = max(margins[side] for side in _facing(a, b))
            if m > 0.0:
                bands.append(capsule(a, b, m, tol=tol))
        for hole in part.interiors:
            if largest > 0.0:
                bands.extend(capsule(a, b, largest, tol=tol) for a, b in _edges(hole))
    if not bands:
        return region  # type: ignore[return-value]
    area = region.difference(unary_union(bands))
    pieces = _parts(area)
    if not pieces:
        return Polygon()
    return pieces[0] if len(pieces) == 1 else MultiPolygon(pieces)


def work_area_wheel(P: Polygon, supports_decl: Mapping[str, float] | None, m_edge: float | Mapping[str, float], *,
                    tol: float = APPROX_TOL_MM) -> Polygon | MultiPolygon:
    """``WA_wheel = (P u S_decl)`` less each edge's margin (W-AREA; safety-model 6.2).

    ``m_edge`` is one margin for every side, or a mapping with each of
    :data:`SIDES` (:func:`edge_margins`). Lies inside the exact set.
    """
    poly = _panel(P)
    margins = _side_margins(m_edge)
    strips = support_strips(poly, supports_decl)
    region = poly if strips.is_empty else unary_union([poly, strips])
    return _work_area(region, margins, _positive(tol, "tol"))


def work_area_tool(P: Polygon, m_edge_tool: float, *, tol: float = APPROX_TOL_MM) -> Polygon | MultiPolygon:
    """``WA_tool = P - m_edge_tool`` on every side, no strip (T-AREA; safety-model 6.3). Lies inside the exact set."""
    return _work_area(_panel(P), _side_margins(_real(m_edge_tool, "m_edge_tool")), _positive(tol, "tol"))
