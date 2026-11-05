# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Time pieces: the time coordinate of every check (unit P03; the project design, sections 4.4 and 4.7.3).

``pieces(program, delta_mm) -> PieceArray`` cuts every move of a normalised
:class:`~goliath_prep.ir.Program` into pieces no longer than ``delta_mm`` in
XY (docs/prep/safety-model.md sections 3.5, 3.6 and 7.3):

* a straight move into the fewest equal pieces whose lengths are at most
  delta; a Z-only or zero-length move (a rotation in place) is one piece of
  zero XY length;
* an arc into the fewest equal sub-arcs, each at most 90 degrees, whose
  chords are at most delta and whose **sagitta** ``e_j`` is at most
  ``e_max`` (docs/prep/params.json ``check_chord_tol``, 0.05 mm). Each piece
  is its chord, and ``sagitta`` bounds the distance from the arc to that
  chord (the circle's sagitta, plus the radius change along the piece, plus
  how far the computed path ends from the arc's written end, plus 1 nm for
  floating-point error), so the arc lies inside the chord grown by it;
* a straight piece has ``sagitta`` 0.

Pieces are numbered in program order across cuts and links (``idx``); a piece
is ``in_material`` when its own lowest Z is below -``eps_surf``, decided
exactly on the program's decimals, and only inside an in-material move. Moves
whose position the program had not written yet (the links from the unknown
start, :attr:`goliath_prep.ir.Move.located` False) have no pieces: their
indices are in :attr:`PieceArray.unplaced`, and the line from the robot's start
pose to the first known position is the program's first line (W-FIRST).

``delta_mm`` comes from the caller (the removal index derives it from the
active footprint, unit P06: min(50, ...)); this module refuses a delta that is
not a finite length in (0, 50] mm, the design's largest piece. The numbers are
the same on every platform: cos and sin from the correctly rounded
:func:`goliath_prep.geom.footprint.unit`, lengths with ``math.sqrt``.
"""
from __future__ import annotations

import math
import numbers
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any

import numpy as np

from goliath_prep.core.params import Param
from goliath_prep.defaults import SPEC_PARAMS
from goliath_prep.geom.footprint import unit
from goliath_prep.ir import Move, Program

__all__ = ["ARC_PART_MAX_DEG", "DELTA_MAX_MM", "PIECE_DTYPE", "ArcPath", "PieceArray", "length_mm", "pieces"]

DELTA_MAX_MM = 50.0
"""The longest piece the design allows (safety-model 7.3: delta = min(50, ...))."""

PIECE_DTYPE = np.dtype([
    ("idx", "<i8"),          # the time coordinate: 0, 1, 2, ... in program order
    ("cut", "<i4"),          # the cut it belongs to, -1 for a link piece
    ("link", "<i4"),         # the link it belongs to, -1 for a cut piece
    ("move", "<i4"),         # the index of its move in Program.moves
    ("kind", "<U8"),         # the move's kind: traverse, feed, arc_cw, arc_ccw
    ("x0", "<f8"), ("y0", "<f8"), ("x1", "<f8"), ("y1", "<f8"),  # the chord, mm
    ("z0", "<f8"), ("z1", "<f8"),                                # Z at its start and end, mm
    ("zmin", "<f8"), ("zmax", "<f8"),                            # its lowest and highest Z, mm
    ("sagitta", "<f8"),      # e_j: the arc lies within this distance of the chord; 0 for a straight piece
    ("in_material", "?"),    # its lowest Z is below -eps_surf
    ("src_line", "<i4"),     # the source line of its move
    ("part", "<i4"),         # 1-based number of the piece within its move
    ("parts", "<i4"),        # the number of pieces of its move
])
"""The fields of a :class:`PieceArray` (the design's idx, cut, kind, x0, y0, x1, y1, zmin, sagitta, src_line,
and the rest)."""

ARC_PART_MAX_DEG = 90.0
"""The largest arc part, helix segment and arc piece, in degrees: well clear of 180, where start, centre and end
no longer tell an arc's direction."""

_FP = 1e-9  # mm: allowance for floating-point error in a computed bound


def length_mm(dx: float, dy: float) -> float:
    """sqrt(dx^2 + dy^2), correctly rounded by IEEE 754 (not math.hypot, whose algorithm changed between versions)."""
    return math.sqrt(dx * dx + dy * dy)


# ---------------------------------------------------------------------------
# Arc geometry (shared with goliath_prep.normalise)
# ---------------------------------------------------------------------------


class ArcPath:
    """An arc from ``a`` to ``b`` about ``c``: the radius changes linearly with the angle, from |a - c| to |b - c|.

    Points are computed by turning the start direction through the angle with
    the correctly rounded :func:`goliath_prep.geom.footprint.unit`, so they are
    the same on every platform. ``mismatch`` is how far the end computed this
    way lies from ``b`` (the reader's sweep is a float; a split part's end is
    rounded to the grid): every bound built on the path adds it.
    """

    def __init__(self, a: tuple[float, float], b: tuple[float, float], c: tuple[float, float], cw: bool,
                 sweep_deg: float) -> None:
        self.a, self.b, self.c = a, b, c
        self.sign = -1.0 if cw else 1.0
        self.sweep = sweep_deg
        self.r0 = length_mm(a[0] - c[0], a[1] - c[1])
        self.r1 = length_mm(b[0] - c[0], b[1] - c[1])
        if not (self.r0 > 0 and self.r1 > 0):
            raise ValueError("internal error: an arc of zero radius")
        self.ux, self.uy = (a[0] - c[0]) / self.r0, (a[1] - c[1]) / self.r0
        end = self._point(1, 1)
        self.mismatch = length_mm(end[0] - b[0], end[1] - b[1])
        self.r_hi = self.r0 if self.r0 >= self.r1 else self.r1
        self.dr = abs(self.r1 - self.r0)

    def _point(self, k: int, n: int) -> tuple[float, float]:
        cos_t, sin_t = unit(self.sign * self.sweep * k / n)
        r = self.r0 + (self.r1 - self.r0) * k / n
        return (self.c[0] + r * (self.ux * cos_t - self.uy * sin_t),
                self.c[1] + r * (self.ux * sin_t + self.uy * cos_t))

    def point(self, k: int, n: int) -> tuple[float, float]:
        """The point k/n of the way along (the start for k = 0, exactly ``b`` for k = n)."""
        if k == 0:
            return self.a
        if k == n:
            return self.b
        return self._point(k, n)

    def deviation(self, n: int) -> float:
        """A bound on the distance from the path to the chords of n equal parts (each at most 90 degrees)."""
        _, sin_q = unit(self.sweep / n / 4.0)
        return 2.0 * self.r_hi * sin_q * sin_q + 2.0 * self.dr / n + self.mismatch + _FP

    def chord_bound(self, n: int) -> float:
        """A bound on the length of each chord of n equal parts."""
        _, sin_h = unit(self.sweep / n / 2.0)
        return 2.0 * self.r_hi * sin_h + self.dr / n + 2.0 * self.mismatch + _FP

    def parts_for(self, deviation_max: float, chord_max: float | None = None) -> int:
        """The smallest number of equal parts, each at most 90 degrees, within ``deviation_max`` (and ``chord_max``).

        The answer is found by testing ``deviation`` (and ``chord_bound``),
        which is monotone in n, so it does not depend on the first estimate.
        """
        least = max(1, math.ceil(self.sweep / ARC_PART_MAX_DEG))
        room = deviation_max - 2.0 * self.dr - self.mismatch - _FP
        if room <= 0.0:
            raise ValueError("internal error: an arc of radius %.4f mm cannot be held within %.4f mm (its end lies "
                             "%.6f mm off the computed path)" % (self.r_hi, deviation_max, self.mismatch))
        # Small-angle estimate: 2 r sin^2(phi/4) <= room for phi <= 4 sqrt(room / (2 r)) radians (asin(x) >= x).
        estimate = self.sweep * math.pi / 180.0 / (4.0 * math.sqrt(room / (2.0 * self.r_hi)))
        n = max(least, math.ceil(estimate))

        def ok(k: int) -> bool:
            if k < least or self.deviation(k) > deviation_max:
                return False
            return chord_max is None or self.chord_bound(k) <= chord_max

        while n > least and ok(n - 1):
            n -= 1
        while not ok(n):
            n += 1
            if n > 10_000_000:
                raise ValueError("internal error: no subdivision of an arc of radius %.4f mm meets %.4f mm"
                                 % (self.r_hi, deviation_max))
        return n


@dataclass(frozen=True, eq=False)
class PieceArray:
    """The pieces of a program: a read-only numpy structured array of :data:`PIECE_DTYPE` and how it was made.

    Indexing and ``len`` go to :attr:`data` (``pieces["x0"]``).
    ``delta_mm`` is the longest piece asked for, ``e_max`` the largest sagitta
    allowed (with its source), ``unplaced`` the indices of the moves that have
    no pieces (positions not yet written: the links from the unknown start).
    """

    data: np.ndarray
    delta_mm: float
    e_max: Param[float]
    unplaced: tuple[int, ...]

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, key: Any) -> Any:
        return self.data[key]

    def __iter__(self) -> Any:
        return iter(self.data)


def _delta(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError("delta_mm must be a number of millimetres, got %r" % (value,))
    delta = float(value)
    if not (math.isfinite(delta) and 0.0 < delta <= DELTA_MAX_MM):
        raise ValueError("delta_mm must be a finite length in (0, %g] mm (safety-model 7.3), got %r"
                         % (DELTA_MAX_MM, value))
    return delta


def pieces(program: Program, delta_mm: float) -> PieceArray:
    """Cut every move of ``program`` into time pieces no longer than ``delta_mm`` (see the module documentation)."""
    if not isinstance(program, Program):
        raise TypeError("pieces needs a normalised Program (goliath_prep.normalise.normalise), got %s"
                        % type(program).__name__)
    delta = _delta(delta_mm)
    e_max_param = SPEC_PARAMS["check_chord_tol"]
    e_max = float(e_max_param.value)
    eps = Fraction(Decimal(repr(program.meta.eps_surf.value)))
    cut_of: dict[int, int] = {}
    link_of: dict[int, int] = {}
    for cut in program.cuts:
        for move in cut.moves:
            cut_of[move.index] = cut.id
    for link in program.links:
        for move in link.moves:
            link_of[move.index] = link.id
    rows: list[tuple[Any, ...]] = []
    unplaced: list[int] = []
    for move in program.moves:
        if not move.located:
            unplaced.append(move.index)
            continue
        if move.is_arc:
            chords, sagitta = _arc_pieces(move, delta, e_max)
        else:
            chords, sagitta = _line_pieces(move, delta), 0.0
        n = len(chords)
        za, zb = Fraction(move.a[2]), Fraction(move.b[2])  # type: ignore[arg-type]
        for k, (x0, y0, x1, y1) in enumerate(chords):
            z0 = za + (zb - za) * Fraction(k, n)
            z1 = za + (zb - za) * Fraction(k + 1, n)
            low, high = (z0, z1) if z0 <= z1 else (z1, z0)
            rows.append((len(rows), cut_of.get(move.index, -1), link_of.get(move.index, -1), move.index, move.kind,
                         x0, y0, x1, y1, float(z0), float(z1), float(low), float(high), sagitta,
                         bool(move.in_material and low < -eps), move.src.line, k + 1, n))
    data = np.array(rows, dtype=PIECE_DTYPE)
    data.setflags(write=False)
    return PieceArray(data=data, delta_mm=delta, e_max=e_max_param, unplaced=tuple(unplaced))


def _line_pieces(move: Move, delta: float) -> list[tuple[float, float, float, float]]:
    ax, ay, _ = move.a_f
    bx, by, _ = move.b_f
    dx, dy = bx - ax, by - ay
    length = math.sqrt(dx * dx + dy * dy)
    n = max(1, math.ceil(length / delta))
    while True:
        points = [(ax, ay)] + [(ax + dx * k / n, ay + dy * k / n) for k in range(1, n)] + [(bx, by)]
        chords = [(p[0], p[1], q[0], q[1]) for p, q in zip(points, points[1:])]
        if all(length_mm(c[2] - c[0], c[3] - c[1]) <= delta for c in chords):
            return chords
        n += 1


def _arc_pieces(move: Move, delta: float, e_max: float) -> tuple[list[tuple[float, float, float, float]], float]:
    ax, ay, az = move.a_f
    bx, by, bz = move.b_f
    if az != bz:  # pragma: no cover - the normaliser linearises every helix
        raise ValueError("line %d: an arc move whose Z changes reached the pieces (helices are linearised)"
                         % move.src.line)
    assert move.sweep_deg is not None
    path = ArcPath((ax, ay), (bx, by), move.centre_f, move.kind == "arc_cw", move.sweep_deg)
    n = path.parts_for(e_max, delta)
    while True:
        points = [path.point(k, n) for k in range(n + 1)]
        chords = [(p[0], p[1], q[0], q[1]) for p, q in zip(points, points[1:])]
        if all(length_mm(c[2] - c[0], c[3] - c[1]) <= delta for c in chords):
            sagitta = path.deviation(n)
            if not sagitta <= e_max:  # pragma: no cover - parts_for chose n so that it is
                raise ValueError("internal error: line %d: sagitta %r above e_max" % (move.src.line, sagitta))
            return chords, sagitta
        n += 1


