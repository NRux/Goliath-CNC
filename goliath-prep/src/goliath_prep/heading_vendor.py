# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The vendor host software's heading search, reproduced exactly (spec 08).

Spec: docs/protocol/08-head-angle-and-cut-order.md, sections *Footprint
model*, *Tower keep-out zones* and *Candidate search and selection*. This
module reproduces ``select_heading(P, W, H, B, tower2, F)`` as the spec states
it: the 37 candidates ``i = 0, 10, ..., 360`` in ascending order, pass 1 as
first fit, pass 2 ranked by fewer sides overhung and then smaller total
overhang with strict comparisons (so an exact tie goes to the lower ``C``),
the bounding box started at ``(W, 0, H, 0)`` (so an empty ``P`` gives
``C = 0``), the exact point-in-triangle test with the spec's vertex order, and
IEEE-754 doubles throughout, with cos and sin taken of ``θ·π/180``.

**What it is for.** It tells what the vendor's software would have chosen:
to explain that choice next to ours, as the order in which goliath-prep's own
selector tries headings, and for the vendor-floor assertion (every heading we
choose must also pass the vendor's own test). The project design, sections
4.11 and 10.1, describes those uses.

**What it is not.** It is not a safety check. It uses spec 08's constants
(402 mm, 211 mm, ±76°, 150 mm zones), never a measured footprint; it samples
only the vertices it is given; and spec 08 lists what is unconfirmed about
it (whether the support points are wheels, arcs, tower 2's location). A
heading that passes here is not thereby safe. Pure Python with no shapely,
so that every boundary decision is the spec's own arithmetic.

Readings pinned where spec 08 leaves details open (tests in
goliath-prep/tests/spec08/; the differential tests there compare every double
bit for bit with a literal transcription of the spec's pseudo-code):

* ``u(θ + δ)`` is ``(cos, sin)`` of ``(θ + δ)·π/180``, evaluated left to right
  as ``((θ + δ)·π)/180``, with ``θ + δ`` not reduced modulo 360.
* cos and sin are the platform's ``math.cos``/``math.sin``. The results are
  the tested ones only where these are correctly rounded at the search's 108
  angles; several of those values lie within a hundredth of an ulp of a
  rounding midpoint, and rounding them otherwise changes exact ties. A test
  pins the 216 doubles, so a platform that differs fails it visibly.
* ``S = sum(values of ov)`` adds the overhang values left to right in the
  order the pseudo-code inserts them (left, right, bottom, top), starting
  from 0. Not ``math.fsum`` and not Python's builtin ``sum``, which has been
  compensated (more exact) for floats since Python 3.12: either can turn an
  exact tie into a strict win and change the chosen heading.
* ``min``/``max`` keep the running value unless the new one is strictly
  smaller/larger. Only the sign of a zero can depend on this, and no
  comparison in spec 08 does.
* The triangle test is applied exactly as written, including where it is
  surprising: when ``A < 0`` (Z1, and Z2 at ``(0, H)``) a point with ``s = 0``
  passes the sign check whatever ``t`` is, so the half-line from vertex ``a``
  pointing away from ``c`` counts as inside (for Z1: ``y = 0, x < 0``). Spec
  08 marks the test's behaviour well outside the triangle as unverified.
* :attr:`VendorResult.c` is the ``C`` the vendor writes, ``(θ − 90 + 360) mod
  360``, always in ``0..350``; the grid index (which is 360 only when forced)
  is :attr:`VendorResult.candidate`.
* A failed search (spec 08's ``NaN``) returns no overhang at all (an empty
  mapping), not zeros: the vendor's caller stores zeros, but zeros here could
  be read as "no support needed".

Inputs are checked and refused with a clear error rather than guessed:
non-finite or non-numeric coordinates, a panel side that is not positive,
side names other than left/right/bottom/top, ``tower2`` other than 0 or 1
(spec 08 reads any non-zero value as 1; a stray value here is a bug, not a
placement), and a forced heading that is not an integer.
"""
from __future__ import annotations

import math
import numbers
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, TypeAlias

__all__ = [
    "GRID",
    "NO_FORCED_ANGLE",
    "SIDES",
    "VendorCandidate",
    "VendorResult",
    "Vec2",
    "candidate_table",
    "candidates",
    "evaluate",
    "select_heading_vendor",
    "support_points",
    "tower_zones",
    "triangle_contains",
]

Vec2: TypeAlias = tuple[float, float]
#: (x0, x1, y0, y1): running min x, max x, min y, max y.
Box: TypeAlias = tuple[float, float, float, float]
Triangle: TypeAlias = tuple[Vec2, Vec2, Vec2]

#: Panel sides, in the order spec 08's pass 2 inserts them into ``ov``.
SIDES: tuple[str, ...] = ("left", "right", "bottom", "top")
#: The candidate grid in C units: 0, 10, ..., 360 (37 values), in search order.
GRID: tuple[int, ...] = tuple(range(0, 361, 10))
#: ``F`` meaning "no forced heading".
NO_FORCED_ANGLE = -1

# Spec 08 §Constants. Deliberately the vendor's values, not a measured footprint.
HEADING_OFFSET = 90  # θ = (i + 90) mod 360
REAR_SUPPORT_MM = 402.0
SIDE_SUPPORT_MM = 211.0
SIDE_SUPPORT_DEG = 76.0
WHEEL_ZONE_LEG_MM = 150.0
PASS2_START_K = 5  # bestK; bestS starts at W

# (r, δ) of q1, q2, q3, in the spec's order.
_SUPPORTS: tuple[tuple[float, float], ...] = (
    (REAR_SUPPORT_MM, 0.0),
    (SIDE_SUPPORT_MM, +SIDE_SUPPORT_DEG),
    (SIDE_SUPPORT_MM, -SIDE_SUPPORT_DEG),
)


@dataclass(frozen=True)
class VendorResult:
    """What spec 08's ``select_heading`` returns, with the pass that decided it.

    ``theta`` is the heading in degrees (``NaN`` when the search fails); ``c``
    the ``C`` value the vendor writes for it (``None`` on failure);
    ``overhang`` how far the panel must be extended with support on each side
    (all four sides; all zero after pass 1; empty on failure); ``pass_`` 1 or
    2, or ``None`` on failure; ``candidate`` the grid index ``i`` that won
    (360 only when forced; ``None`` on failure).
    """

    theta: float
    c: int | None
    overhang: dict[str, float]
    pass_: Literal[1, 2] | None
    candidate: int | None

    @property
    def found(self) -> bool:
        """False when the vendor's search fails (its ``NaN``)."""
        return self.pass_ is not None


@dataclass(frozen=True)
class VendorCandidate:
    """One candidate as spec 08's search sees it, to explain the vendor's choice.

    ``overhang`` is pass 2's ``ov``: only the sides strictly overhung, in
    :data:`SIDES` order. ``blocked`` are those of them that ``B`` blocks.
    """

    candidate: int
    theta: float
    box: Box
    tower_hit: bool
    overhang: dict[str, float]
    blocked: tuple[str, ...]

    @property
    def fits_panel(self) -> bool:
        """Accepted by pass 1: no tower hit and every support on the panel."""
        return not self.tower_hit and not self.overhang

    @property
    def pass2_survivor(self) -> bool:
        """Ranked by pass 2 (which runs only when no candidate fits the panel)."""
        return not self.tower_hit and not self.blocked

    @property
    def sides_overhung(self) -> int:
        """Pass 2's ``K``."""
        return len(self.overhang)

    @property
    def total_overhang(self) -> float:
        """Pass 2's ``S``, summed as spec 08 reads (left to right, IEEE doubles)."""
        return _plain_sum(self.overhang.values())


# --------------------------------------------------------------------------
# Input checks


def _real(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError("%s must be a real number, not %r (%s)" % (what, value, type(value).__name__))
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("%s must be finite, not %r" % (what, value))
    return result


def _integer(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise TypeError("%s must be an integer, not %r (%s)" % (what, value, type(value).__name__))
    return int(value)


def _panel_side(value: object, name: str) -> float:
    result = _real(value, "panel %s" % name)
    if not result > 0.0:
        raise ValueError("panel %s must be positive, not %r" % (name, value))
    return result


def _point(value: object, what: str) -> Vec2:
    try:
        x, y = value  # type: ignore[misc]
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be an (x, y) pair, not %r" % (what, value)) from exc
    return (_real(x, "%s x" % what), _real(y, "%s y" % what))


def _points(P: Iterable[Vec2]) -> tuple[Vec2, ...]:
    return tuple(_point(p, "P[%d]" % n) for n, p in enumerate(P))


def _blocked_sides(B: Iterable[str]) -> frozenset[str]:
    if isinstance(B, (str, bytes)):
        raise TypeError("B must be a set of side names (%s), not the string %r" % (", ".join(SIDES), B))
    sides = frozenset(B)
    for side in sides:
        if not isinstance(side, str):
            raise TypeError("B must hold side names (%s), not %r" % (", ".join(SIDES), side))
        if side not in SIDES:
            raise ValueError("B: unknown side %r; the sides are %s" % (side, ", ".join(SIDES)))
    return sides


def _tower2(value: object) -> int:
    tower2 = _integer(value, "tower2")
    if tower2 not in (0, 1):
        raise ValueError("tower2 must be 0 (tower 2 at (0, H)) or 1 (tower 2 at (W, 0)), not %r" % (value,))
    return tower2


def _grid_index(value: object) -> int:
    i = _integer(value, "candidate i")
    if i not in _FOOTPRINT:
        raise ValueError("candidate i must be on spec 08's grid 0, 10, ..., 360, not %r" % (value,))
    return i


# --------------------------------------------------------------------------
# Footprint and zones (spec 08 §Footprint model, §Tower keep-out zones)


def _unit(alpha: float) -> Vec2:
    # u(α) = (cos α, sin α) of α·π/180, evaluated left to right.
    return (math.cos(alpha * math.pi / 180.0), math.sin(alpha * math.pi / 180.0))


def _footprint(i: int) -> tuple[float, tuple[tuple[float, float, float], ...]]:
    theta = float((i + HEADING_OFFSET) % 360)
    return theta, tuple((r,) + _unit(theta + delta) for r, delta in _SUPPORTS)


# θ and (r, cos, sin) of each support, per grid index: the same doubles the
# spec's formulas give, computed once.
_FOOTPRINT = {i: _footprint(i) for i in GRID}


def support_points(p: Vec2, i: int) -> tuple[Vec2, Vec2, Vec2]:
    """q1, q2, q3 of spec 08 for tool point ``p`` at grid candidate ``i``: ``q = p − r·u(θ + δ)``."""
    px, py = _point(p, "p")
    _theta, supports = _FOOTPRINT[_grid_index(i)]
    q1, q2, q3 = ((px - r * ux, py - r * uy) for r, ux, uy in supports)
    return q1, q2, q3


def tower_zones(W: float, H: float, tower2: int) -> tuple[Triangle, Triangle]:
    """Z1 and Z2, the wheel keep-out triangles, with their vertices in spec 08's order."""
    W = _panel_side(W, "width W")
    H = _panel_side(H, "height H")
    return _zones(W, H, _tower2(tower2))


def _zones(W: float, H: float, tower2: int) -> tuple[Triangle, Triangle]:
    leg = WHEEL_ZONE_LEG_MM
    z1 = ((0.0, 0.0), (0.0, leg), (leg, 0.0))
    if tower2 == 0:
        z2 = ((0.0, H - leg), (0.0, H), (leg, H))
    else:
        z2 = ((W - leg, 0.0), (W, 0.0), (W, leg))
    return z1, z2


def triangle_contains(q: Vec2, triangle: Triangle) -> bool:
    """Spec 08's exact point-in-triangle test; edge behaviour depends on the vertex order."""
    qx, qy = _point(q, "q")
    a, b, c = (_point(v, "triangle vertex") for v in triangle)
    return _inside(qx, qy, _prepared(a, b, c))


# (ax, ay, (b − a).x, (b − a).y, (c − a).x, (c − a).y, A)
_Prepared: TypeAlias = tuple[float, float, float, float, float, float, float]


def _prepared(a: Vec2, b: Vec2, c: Vec2) -> _Prepared:
    bax, bay = b[0] - a[0], b[1] - a[1]
    cax, cay = c[0] - a[0], c[1] - a[1]
    area = bax * cay - bay * cax  # A = (b − a) × (c − a)
    return (a[0], a[1], bax, bay, cax, cay, area)


def _inside(qx: float, qy: float, tri: _Prepared) -> bool:
    ax, ay, bax, bay, cax, cay, area = tri
    qax = qx - ax
    qay = qy - ay
    t = bax * qay - bay * qax  # (b − a) × (q − a)
    s = qax * cay - qay * cax  # (q − a) × (c − a)
    if (s < 0) != (t < 0):
        return False
    if area < 0:
        return s <= 0 and s + t >= area
    return s >= 0 and s + t <= area


# --------------------------------------------------------------------------
# Evaluation and search (spec 08 §Candidate search and selection)


def evaluate(i: int, P: Iterable[Vec2], W: float, H: float, tower2: int) -> tuple[float, Box, bool]:
    """Spec 08's ``evaluate(i)``: (θ, (x0, x1, y0, y1), hit) for grid candidate ``i``.

    The box starts at ``(W, 0, H, 0)`` and takes every support of every point;
    ``hit`` is True when any support lies in Z1 or Z2.
    """
    i = _grid_index(i)
    W = _panel_side(W, "width W")
    H = _panel_side(H, "height H")
    zones = _zones(W, H, _tower2(tower2))
    return _evaluate(i, _points(P), W, H, tuple(_prepared(*z) for z in zones))


def _evaluate(
    i: int, points: tuple[Vec2, ...], W: float, H: float, zones: tuple[_Prepared, ...]
) -> tuple[float, Box, bool]:
    theta, supports = _FOOTPRINT[i]
    z1, z2 = zones
    x0, x1, y0, y1 = W, 0.0, H, 0.0
    hit = False
    for px, py in points:
        for r, ux, uy in supports:
            qx = px - r * ux
            qy = py - r * uy
            # min/max that keep the running value unless the new one is strictly beyond it.
            if qx < x0:
                x0 = qx
            if qx > x1:
                x1 = qx
            if qy < y0:
                y0 = qy
            if qy > y1:
                y1 = qy
            if not hit and (_inside(qx, qy, z1) or _inside(qx, qy, z2)):
                hit = True
    return theta, (x0, x1, y0, y1), hit


def _ov(box: Box, W: float, H: float) -> dict[str, float]:
    x0, x1, y0, y1 = box
    ov: dict[str, float] = {}
    if x0 < 0:
        ov["left"] = -x0
    if x1 > W:
        ov["right"] = x1 - W
    if y0 < 0:
        ov["bottom"] = -y0
    if y1 > H:
        ov["top"] = y1 - H
    return ov


def _plain_sum(values: Iterable[float]) -> float:
    total = 0.0
    for value in values:
        total = total + value
    return total


def candidates(F: int) -> tuple[int, ...]:
    """Spec 08's candidate list: the whole grid when ``F == -1``, else the grid value equal to ``F`` (or none)."""
    forced = _integer(F, "F (the forced C)")
    return tuple(i for i in GRID if forced == NO_FORCED_ANGLE or i == forced)


def _c_of(theta: float) -> int:
    # C = (θ − 90 + 360) mod 360, printed with no decimal places (spec 08 §Emitting the C word).
    return int((theta - HEADING_OFFSET + 360) % 360)


def _prepare(P, W, H, B, tower2, F):
    points = _points(P)
    W = _panel_side(W, "width W")
    H = _panel_side(H, "height H")
    blocked = _blocked_sides(B)
    zones = tuple(_prepared(*z) for z in _zones(W, H, _tower2(tower2)))
    return points, W, H, blocked, zones, candidates(F)


def select_heading_vendor(
    P: Iterable[Vec2], W: float, H: float, B: Iterable[str], tower2: int, F: int
) -> VendorResult:
    """Spec 08's ``select_heading(P, W, H, B, tower2, F)``, exactly.

    ``P`` the tool points (read once), ``W`` x ``H`` the panel, ``B`` the
    blocked sides, ``tower2`` 0 (tower 2 at (0, H)) or 1 (at (W, 0)), ``F``
    the forced C or -1. A failed search returns ``theta = NaN`` and
    ``found = False``; it does not raise.
    """
    points, W, H, blocked, zones, order = _prepare(P, W, H, B, tower2, F)
    evaluated: dict[int, tuple[float, Box, bool]] = {}

    def evaluate_once(i: int) -> tuple[float, Box, bool]:
        if i not in evaluated:
            evaluated[i] = _evaluate(i, points, W, H, zones)
        return evaluated[i]

    # Pass 1: first fit, no scoring.
    for i in order:
        theta, (x0, x1, y0, y1), hit = evaluate_once(i)
        if not hit and x0 >= 0 and x1 <= W and y0 >= 0 and y1 <= H:
            return VendorResult(theta, _c_of(theta), dict.fromkeys(SIDES, 0.0), 1, i)

    # Pass 2: allow wheels past non-blocked sides.
    best: tuple[int, float, Box] | None = None
    best_k: int = PASS2_START_K
    best_s: float = W
    for i in order:
        theta, box, hit = evaluate_once(i)
        if hit:
            continue
        ov = _ov(box, W, H)
        if any(side in blocked for side in ov):
            continue
        k = len(ov)
        s = _plain_sum(ov.values())
        if k < best_k or (k == best_k and s < best_s):
            best = (i, theta, box)
            best_k = k
            best_s = s
    if best is None:
        return VendorResult(math.nan, None, {}, None, None)
    i, theta, (x0, x1, y0, y1) = best
    overhang = {
        "left": max(0.0, -x0),
        "right": max(0.0, x1 - W),
        "bottom": max(0.0, -y0),
        "top": max(0.0, y1 - H),
    }
    return VendorResult(theta, _c_of(theta), overhang, 2, i)


def candidate_table(
    P: Iterable[Vec2], W: float, H: float, B: Iterable[str], tower2: int, F: int
) -> tuple[VendorCandidate, ...]:
    """Every candidate spec 08 would consider, in search order, with what decides it.

    Pass 1 accepts the first row that :attr:`~VendorCandidate.fits_panel`;
    otherwise pass 2 takes, among :attr:`~VendorCandidate.pass2_survivor`
    rows, the fewest sides overhung, then the smallest total, then the
    earliest row. :func:`select_heading_vendor` is the authority; this table
    is for explaining its result.
    """
    points, W, H, blocked, zones, order = _prepare(P, W, H, B, tower2, F)
    rows = []
    for i in order:
        theta, box, hit = _evaluate(i, points, W, H, zones)
        ov = _ov(box, W, H)
        rows.append(VendorCandidate(i, theta, box, hit, ov, tuple(side for side in ov if side in blocked)))
    return tuple(rows)
