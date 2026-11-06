# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The removal index and the time-exact W-SURF query (unit P06; the project design, sections 4.7.1 to 4.7.3 and
4.7.5; docs/prep/safety-model.md sections 7.1 to 7.3 and 7.5; red-team PHYS-06, C-10).

**Removal** (safety-model 7.1). Each in-material piece j of a
:class:`~goliath_prep.pieces.PieceArray` (chord sigma_j, sagitta e_j, lowest Z
z_j) removes ``R_j = sigma_j + D(r_tool + delta_tool + e_j)`` with depth
``d_j = -z_j``, the deepest depth over the piece itself (conservative for every
use: it over-counts what the wheels must avoid and what breaks holding
material). ``Rem(j)`` is the prior removal declared for the panel
(:class:`PriorRemoval`, job.toml ``[[prior_removal]]``: red-team PHYS-06) and
the ``R_i`` of the in-material pieces ``i < j``. Pieces out of the material
remove nothing. An STRtree over the in-material chords, in time order, answers
the queries.

**The query** (W-SURF, safety-model 7.2). :meth:`RemovalIndex.clearance` takes
a geometry during piece j (a wheel centre's path, a ``LineString``, or a
``Point`` for a wheel standing still, grown by ``rho``; or a ``Polygon`` region
that already holds the wheel discs, usually with ``rho = 0``) and returns
``(slack, witness or None)`` against ``Rem(j)``. The slack is
``dist(geometry, sigma_i) - (r_tool + delta_tool + e_i) - rho`` for the nearest
removing piece i, or ``dist(geometry, polygon) - rho`` for prior removal: the
distance to the removal itself whenever the geometry is clear, so exact there
(the removal is a union of capsules, and the distance to a capsule is the
distance to its chord minus its radius; no polygon stands in for it). The
search widens until the nearest earlier removal is found, so the slack is exact
however far away it is (``math.inf`` when nothing was removed before piece j).
*This module's readings* (the safe ones): a slack at or below
:data:`TOUCH_TOL_MM` (1 nm) counts as meeting, because the wheel disc and the
removal are closed sets and touching is meeting (the catalogue writes
"negative = violated"); and when the geometry overlaps a kerf the slack is
measured to its toolpath, so it is more negative than the distance to the
removal set alone could show. ``raw = slack + sum(margins)`` always holds
(safety-model 11.2). The witness holds the W-SURF fields the index knows:
``piece`` and ``cut`` of the piece checked (the time; none after the last
piece), ``point`` on the hazard, ``hazard`` in words, ``slack_mm``, ``raw_mm``
and ``margins``. The check adds ``wheel``, ``src`` and ``heading_c``.
:meth:`RemovalIndex.wheel_clearance` and :meth:`RemovalIndex.wheel_track` are
the W-SURF form for a support at a fixed offset from the tool: the wheel
centre's chord, grown by ``rho + e_j``, the wheel's own sagitta during an arc
piece (margin ``e_wheel``).

**Exact in time** (safety-model 7.3; fixes the per-move timing defect E-B1).
The piece's own removal is left out of ``Rem(j)``. That is exact because the
checker asserts at start-up, with the active footprint, ``rho``, the tool and
``e_max``::

    min_k r_k - Delta  >  rho + r_tool + delta_tool + 2 e_max

and pieces are at most Delta long, ``Delta = min(50, floor(min_k r_k - rho -
r_tool - delta_tool - 2 e_max - 1))`` (:func:`derive_delta`, on the exact
decimals of the inputs; below 5 mm it is an internal error, M-INVARIANT). The
index cannot be built without the assertion passing on the very pieces it
serves (:meth:`RemovalIndex.build` takes the footprint and rho for that), and
every query checks it again for its own geometry: a geometry that reaches the
removal of the piece being checked raises :class:`InvariantError`, never a
pass. So a rear wheel trailing into the kerf of the cut that is making it is
caught (red-team RT1), and a wheel rolling over material that is cut only later
is not flagged.

**R-KERF** (safety-model 7.5; U-41; red-team C-10, RT24). Closed by default:
every removal deeper than eps_surf is a wheel keep-out. It is in force only
with the R8k fact: :class:`KerfRelaxation` from the context's FACT values
``kerf_width_max`` (w_b), ``kerf_ride_max`` (L_ride) and ``recess_ignore_max``
(h_r), which the context gives only while the gate ``kerf_crossing`` is open.
While it is in force the index carries its amber disclosure
``RELAXATION:R-KERF`` (:attr:`RemovalIndex.disclosures`), logs it when built,
and marks every answer it lifted (:attr:`Clearance.lifted`). With it:

* removal **shallower** than h_r is ignored (a recess exactly h_r deep still
  counts);
* the wheel disc must stay clear of the **wide** removal, the morphological
  opening ``open(Rem, w_b)`` by a disc of diameter w_b (a kerf exactly w_b
  wide counts as wide). The opening near a wheel depends only on the removal
  within w_b of it, so it is computed from the removal near the wheel and is
  exact within the search window (:data:`SEARCH_MM`); beyond it the reported
  slack is that lower bound. The time invariant gains w_b (in
  :func:`derive_delta` and :func:`assert_time_invariant`, and the query guard):
  the removal of the piece being cut must stay more than w_b from the wheel
  disc, or the opening there would not be exact in time;
* along a wheel's **track** (its path over consecutive pieces at one heading,
  :meth:`RemovalIndex.wheel_track`) the disc may meet the rest, the **narrow**
  removal, over a run shorter than L_ride. A run is measured along the wheel
  centre's path while its disc of radius rho meets the removal (narrow or
  wide: wide contact is refused anyway), so a right-angle crossing of a kerf w
  wide is a run of w + 2 rho, plus 2 e_j for every arc piece in it (an arc is
  at most that much longer than its chord). A run equal to L_ride is refused
  like a touch, and a run that reaches either end of the track may go on
  beyond it, so it is never lifted: a single-piece query never lifts a
  crossing, and regions (rotation discs, envelopes, the safe-zone annulus) get
  no crossing, only the h_r rule.

Polygons drawn here (capsules for :meth:`RemovalIndex.removed_region` and
R-KERF's opening) hold the exact sets: GEOS's arcs are drawn with radii
enlarged by ``1 / cos`` of the largest half arc step it uses, so every
approximation errs towards removal, and towards refusing.

**Other queries.** :meth:`RemovalIndex.removed_region` (the union of the
removal before piece j, optionally only deeper than a depth: the holding
material of the structure model, unit P07) and
:meth:`RemovalIndex.deepest_under` (the deepest earlier removal a region
reaches: T-PASS).

The numbers are the same on every platform: lengths with ``math.sqrt`` and
GEOS, plain left-to-right sums, cos from the correctly rounded
:func:`goliath_prep.geom.footprint.unit`.
"""
from __future__ import annotations

import logging
import math
import numbers
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Any

import numpy as np
import shapely
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from goliath_prep.core import ids
from goliath_prep.core.canon import quantize
from goliath_prep.core.disclosure import Disclosure
from goliath_prep.core.findings import Finding, Witness
from goliath_prep.core.params import Param
from goliath_prep.core.types import CheckContext, FootprintSet, FrozenMapping, PriorCut
from goliath_prep.defaults import SPEC_PARAMS
from goliath_prep.geom.footprint import footprint_radii, unit
from goliath_prep.jobspec import JobSpec
from goliath_prep.pieces import DELTA_MAX_MM, PieceArray

__all__ = [
    "KERF_PARAMS",
    "SEARCH_MM",
    "TOUCH_TOL_MM",
    "Clearance",
    "InvariantError",
    "KerfRelaxation",
    "PriorRegion",
    "PriorRemoval",
    "RemovalIndex",
    "assert_time_invariant",
    "delta_for",
    "derive_delta",
    "r_tool_param",
]

log = logging.getLogger("goliath_prep.material.removal")

TOUCH_TOL_MM = 1e-9
"""A slack at or below this counts as meeting: closed sets that touch meet, and float noise is not clearance."""

SEARCH_MM = 64.0
"""The first search radius beyond the reach that matters; the default search grows fourfold until it is exact.
Under R-KERF it is also the window in which the opening is computed exactly (a lower bound beyond it)."""

KERF_PARAMS: tuple[str, str, str] = ("kerf_width_max", "kerf_ride_max", "recess_ignore_max")
"""The catalogue names of R-KERF's w_b, L_ride and h_r (docs/prep/params.json)."""

KERF_DISCLOSURE_ID = "RELAXATION:R-KERF"
PRIOR_DEFAULT_ID = "DEFAULT:prior_removal"

_QUAD_SEGS = 16
# GEOS draws an arc in steps of at most 1.5 x (90 / quad_segs) degrees (a fillet of nSegs = round(angle / step)
# equal steps), so a polygon whose arcs have radius r / cos(0.75 x 90 / quad_segs) holds the true circle.
_INFLATE = 1.0 / unit(0.75 * 90.0 / _QUAD_SEGS)[0]
_PAD_MM = 1e-9
_JOIN_TOL_MM = 1e-6  # pieces of a track meet within this; contact runs closer than this are one run
_DELTA_CAP = Decimal(repr(DELTA_MAX_MM))
_GEOM_TYPES = (LineString, Point, Polygon, MultiPolygon)


class InvariantError(RuntimeError):
    """The time invariant of safety-model 7.3 does not hold: an internal error (M-INVARIANT), never a pass.

    ``finding`` is the M-INVARIANT finding (severity ``internal-error``) with its witness.
    """

    def __init__(self, finding: Finding) -> None:
        super().__init__("%s: %s" % (finding.check_id, finding.message))
        self.finding = finding


def _invariant_error(message: str, hazard: str, **witness: Any) -> InvariantError:
    return InvariantError(Finding(ids.M_INVARIANT, "internal-error", message, Witness(hazard=hazard, **witness),
                                  None))


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _real(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError("%s must be a number of millimetres, got %r" % (what, value))
    out = float(value)
    if not math.isfinite(out):
        raise ValueError("%s must be finite, got %r" % (what, value))
    return out


def _positive(value: object, what: str) -> float:
    out = _real(value, what)
    if out <= 0.0:
        raise ValueError("%s must be above zero, got %r" % (what, value))
    return out


def _non_negative(value: object, what: str) -> float:
    out = _real(value, what)
    if out < 0.0:
        raise ValueError("%s must not be negative, got %r" % (what, value))
    return out + 0.0


def _exact(value: float) -> Decimal:
    """The decimal a float was written as (``repr`` is the shortest round-trip form)."""
    return Decimal(repr(float(value)))


def _footprint(value: object) -> FootprintSet:
    if not isinstance(value, FootprintSet):
        raise TypeError("footprint must be a FootprintSet (the active footprint model), got %r" % (value,))
    return value


def _mm(value: float | Decimal) -> str:
    return "%.3f" % float(value)


def _total(values: Iterable[float]) -> float:
    """A plain left-to-right sum (never the builtin, which is compensated since Python 3.12)."""
    out = 0.0
    for value in values:
        out = out + value
    return out


# ---------------------------------------------------------------------------
# Delta and the time invariant (safety-model 7.3)
# ---------------------------------------------------------------------------


def _margin(rho: float, r_tool: float, delta_tool: float, e_max: float, w_b: float) -> Decimal:
    """rho + r_tool + delta_tool + 2 e_max (+ w_b under R-KERF), on the exact decimals of the floats."""
    return (_exact(_positive(rho, "rho")) + _exact(_positive(r_tool, "r_tool"))
            + _exact(_non_negative(delta_tool, "delta_tool")) + 2 * _exact(_non_negative(e_max, "e_max"))
            + _exact(_non_negative(w_b, "w_b")))


def derive_delta(footprint: FootprintSet, rho: float, r_tool: float, delta_tool: float, e_max: float, *,
                 w_b: float = 0.0, minimum: float | None = None) -> float:
    """Delta = min(50, floor(min_k r_k - rho - r_tool - delta_tool - 2 e_max - 1)), in mm (safety-model 7.3).

    ``min_k r_k`` runs over every support position of the active footprint
    model (the union once anything is measured). ``w_b`` is R-KERF's measured
    kerf width while the relaxation is in force (0 otherwise; it joins the
    margin, see the module documentation). Computed on the exact decimals of
    the inputs, so the floor is never off by float noise. Below ``minimum``
    (docs/prep/params.json ``piece_length_min``, 5 mm, when None) the robot or
    tool is outside the model's assumptions: :class:`InvariantError`
    (M-INVARIANT), whose witness slack is the catalogue's
    ``min_k r_k - rho - r_tool - delta_tool - 2 e_max - 6 mm``.
    """
    r_min = _exact(footprint_radii(_footprint(footprint)).r_min)
    margin = _margin(rho, r_tool, delta_tool, e_max, w_b)
    least = _exact(_positive(SPEC_PARAMS["piece_length_min"].value if minimum is None else minimum,
                             "the minimum piece length"))
    before_cap = (r_min - margin - 1).to_integral_value(rounding=ROUND_FLOOR)
    delta = min(_DELTA_CAP, before_cap)
    if delta < least:
        raise _invariant_error(
            "the piece length Delta = min(50, floor(min_k r_k - rho - r_tool - delta_tool - 2 e_max%s - 1)) = "
            "floor(%s - %s - 1) = %s mm is below the %s mm minimum (min_k r_k = %s mm, rho = %s mm, r_tool = %s mm, "
            "delta_tool = %s mm, e_max = %s mm%s): the robot or tool is outside the model's assumptions, so W-SURF "
            "cannot be exact in time (safety-model 7.3)"
            % (" - w_b" if w_b else "", _mm(r_min), _mm(margin), delta, _mm(least), _mm(r_min), _mm(rho),
               _mm(r_tool), _mm(delta_tool), _mm(e_max), ", w_b = %s mm" % _mm(w_b) if w_b else ""),
            "piece length Delta below the minimum: the robot or tool is outside the model's assumptions",
            value=float(delta), limit=float(least), slack_mm=float(r_min - margin - 1 - least))
    return float(delta)


def assert_time_invariant(footprint: FootprintSet, rho: float, r_tool: float, delta_tool: float, e_max: float,
                          delta: float, *, w_b: float = 0.0) -> None:
    """Raise :class:`InvariantError` unless ``min_k r_k - delta > rho + r_tool + delta_tool + 2 e_max (+ w_b)``.

    Under it, material removed during a piece at most ``delta`` long can never
    be under a wheel during that piece, so leaving it out of Rem(j) is exact
    (safety-model 7.3). ``delta`` must lie in (0, 50] mm, the design's pieces.
    Exact decimal arithmetic; the inequality is strict.
    """
    r_min = _exact(footprint_radii(_footprint(footprint)).r_min)
    margin = _margin(rho, r_tool, delta_tool, e_max, w_b)
    d = _exact(_real(delta, "the piece length delta"))
    if not Decimal(0) < d <= _DELTA_CAP:
        raise _invariant_error(
            "the piece length %s mm is outside (0, %s] mm, the pieces the model is made for (safety-model 7.3)"
            % (_mm(d), _mm(_DELTA_CAP)), "piece length outside the model", value=float(d), limit=float(_DELTA_CAP))
    if not r_min - d > margin:
        raise _invariant_error(
            "the time invariant fails: min_k r_k - Delta = %s - %s = %s mm is not above rho + r_tool + delta_tool + "
            "2 e_max%s = %s mm, so a wheel could be over material removed during its own piece (safety-model 7.3)"
            % (_mm(r_min), _mm(d), _mm(r_min - d), " + w_b" if w_b else "", _mm(margin)),
            "the time invariant of safety-model 7.3 fails for these pieces",
            value=float(d), limit=float(r_min - margin))


def r_tool_param(diameter: Param[Any]) -> Param[float]:
    """r_tool = d / 2 as a DERIVED Param, rounded up to the 0.001 grid (a larger removal is the safe side)."""
    if not isinstance(diameter, Param) or diameter.unit != "mm":
        raise TypeError("r_tool_param needs the tool diameter as a Param in mm, got %r" % (diameter,))
    d = _positive(diameter.value, "the tool diameter")
    value = quantize(float(_exact(d) / 2), "ceiling")
    return Param.derived(value, "mm", "r_tool = d / 2 = %s / 2, rounded up to 0.001 mm (safety-model 7.1; %s)"
                         % (_mm(d), diameter.ref), (diameter,))


def _ctx_param(ctx: CheckContext, name: str) -> Param[Any]:
    param = ctx.params.get(name)
    if param is None:
        raise ValueError("the context has no %r: the removal index reads every value from the context" % name)
    return param


def delta_for(ctx: CheckContext) -> Param[float]:
    """Delta for the context's active footprint, rho, tool, runout and check chord tolerance, as a DERIVED Param.

    Its inputs are the context's Params, so Delta carries their disclosures
    (an unmeasured wheel radius makes Delta unmeasured too). While R-KERF is in
    force its w_b joins the margin. Raises :class:`InvariantError` below the
    minimum piece length.
    """
    if not isinstance(ctx, CheckContext):
        raise TypeError("delta_for needs the CheckContext (goliath_prep.context.build_context), got %r" % (ctx,))
    r_min, rho = _ctx_param(ctx, "r_min"), _ctx_param(ctx, "rho")
    diameter, runout = _ctx_param(ctx, "tool_diameter"), _ctx_param(ctx, "delta_tool")
    e_max, least = _ctx_param(ctx, "check_chord_tol"), _ctx_param(ctx, "piece_length_min")
    computed = footprint_radii(ctx.footprint).r_min
    if r_min.value != computed:
        raise ValueError("the context's r_min %r is not its footprint's %r: the context is inconsistent"
                         % (r_min.value, computed))
    kerf = KerfRelaxation.from_context(ctx)
    r_tool = r_tool_param(diameter)
    w_b = kerf.w_b_mm if kerf is not None else 0.0
    value = derive_delta(ctx.footprint, ctx.rho, r_tool.value, runout.value, e_max.value, w_b=w_b,
                         minimum=least.value)
    ref = ("Delta = min(50, floor(min_k r_k - rho - r_tool - delta_tool - 2 e_max%s - 1)) = min(50, floor(%s - %s - "
           "%s - %s - 2 x %s%s - 1)) = %s mm, r_tool = d / 2 rounded up (safety-model 7.3)"
           % (" - w_b" if kerf is not None else "", _mm(r_min.value), _mm(rho.value), _mm(r_tool.value),
              _mm(runout.value), _mm(e_max.value), " - %s" % _mm(w_b) if kerf is not None else "", _mm(value)))
    inputs = [r_min, rho, diameter, runout, e_max, least]
    if kerf is not None:
        inputs.append(kerf.w_b)
    return Param.derived(value, "mm", ref, inputs)


# ---------------------------------------------------------------------------
# Prior removal (Rem(0))
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriorRegion:
    """Material removed before this program: a region (panel frame, mm), its depth and where it was declared."""

    polygon: Polygon | MultiPolygon
    depth_mm: float
    ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.polygon, (Polygon, MultiPolygon)) or self.polygon.is_empty:
            raise TypeError("PriorRegion polygon must be a non-empty shapely Polygon or MultiPolygon, got %r"
                            % (self.polygon,))
        if not np.isfinite(shapely.get_coordinates(self.polygon)).all():
            raise ValueError("PriorRegion %r: a coordinate is not finite" % (self.ref,))
        if not self.polygon.is_valid:
            raise ValueError("PriorRegion %r: the polygon is not valid (%s): its region would be ambiguous"
                             % (self.ref, shapely.is_valid_reason(self.polygon)))
        object.__setattr__(self, "depth_mm", _positive(self.depth_mm, "PriorRegion depth_mm"))
        if not isinstance(self.ref, str) or not self.ref.strip():
            raise ValueError("PriorRegion needs a ref: where the removal was declared")


@dataclass(frozen=True)
class PriorRemoval:
    """The removal that seeds Rem(0): material already removed from a panel that is not new (safety-model 7.1).

    For a single job it is job.toml's ``[[prior_removal]]`` (red-team PHYS-06,
    :meth:`from_job`, :meth:`from_context`); a series job's previous material
    state can be given the same way (safety-model 13). Over-counted removal is
    conservative for every use. No regions: the panel is taken as intact.
    """

    regions: tuple[PriorRegion, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.regions, tuple) or not all(isinstance(r, PriorRegion) for r in self.regions):
            raise TypeError("PriorRemoval regions must be a tuple of PriorRegion")

    @classmethod
    def from_cuts(cls, cuts: Iterable[PriorCut]) -> PriorRemoval:
        """From :class:`~goliath_prep.core.types.PriorCut` entries (the job's or the context's), in order."""
        regions = []
        for cut in cuts:
            if not isinstance(cut, PriorCut):
                raise TypeError("prior removal entries must be PriorCut, got %r" % (cut,))
            regions.append(PriorRegion(Polygon(cut.polygon), cut.depth_mm, cut.ref))
        return cls(tuple(regions))

    @classmethod
    def from_job(cls, job: JobSpec) -> PriorRemoval:
        """job.toml's ``[[prior_removal]]`` (no entry: no regions)."""
        if not isinstance(job, JobSpec):
            raise TypeError("from_job needs a JobSpec (job.toml, read), got %r" % (job,))
        return cls.from_cuts(job.prior_removal)

    @classmethod
    def from_context(cls, ctx: CheckContext) -> PriorRemoval:
        """The context's prior removal (the job's, carried by the context builder)."""
        if not isinstance(ctx, CheckContext):
            raise TypeError("from_context needs a CheckContext, got %r" % (ctx,))
        return cls.from_cuts(ctx.prior_removal)


# ---------------------------------------------------------------------------
# R-KERF
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KerfRelaxation:
    """R-KERF's measured values w_b, L_ride and h_r: FACT Params from the R8k fact, nothing else.

    Make it with :meth:`from_context`, which gives None while the gate
    ``kerf_crossing`` is not open (closed or uncertain): R-KERF is closed by
    default. Each value must be a ``FACT`` Param in mm, at least 0, so a
    catalogue default, a policy or a guess cannot open it.
    """

    w_b: Param[float]
    l_ride: Param[float]
    h_r: Param[float]

    def __post_init__(self) -> None:
        for name, param in zip(KERF_PARAMS, (self.w_b, self.l_ride, self.h_r)):
            if not isinstance(param, Param):
                raise TypeError("KerfRelaxation %s must be a Param (the R8k fact), got %r" % (name, param))
            if param.source != "FACT":
                raise ValueError("KerfRelaxation %s comes from %s (%s): R-KERF opens only with the measured R8k fact "
                                 "(gate kerf_crossing)" % (name, param.source, param.ref))
            if param.unit != "mm":
                raise ValueError("KerfRelaxation %s must be in mm, got %s" % (name, param.unit))
            _non_negative(param.value, "KerfRelaxation %s" % name)

    @property
    def w_b_mm(self) -> float:
        return float(self.w_b.value)

    @property
    def l_ride_mm(self) -> float:
        return float(self.l_ride.value)

    @property
    def h_r_mm(self) -> float:
        return float(self.h_r.value)

    @property
    def params(self) -> dict[str, Param[float]]:
        """The three Params by their catalogue names."""
        return dict(zip(KERF_PARAMS, (self.w_b, self.l_ride, self.h_r)))

    @property
    def disclosures(self) -> tuple[Disclosure, ...]:
        """The values' disclosures, and the relaxation's own amber one when none of them names it."""
        seen: dict[Disclosure, None] = {}
        for param in (self.w_b, self.l_ride, self.h_r):
            for d in param.all_disclosures():
                seen.setdefault(d, None)
        if not any(d.id == KERF_DISCLOSURE_ID for d in seen):
            seen.setdefault(Disclosure(
                id=KERF_DISCLOSURE_ID, severity="amber",
                text="R-KERF in force: a wheel may cross a kerf up to w_b = %s mm wide over a run of less than "
                     "L_ride = %s mm, and roll over removal shallower than h_r = %s mm"
                     % (_mm(self.w_b_mm), _mm(self.l_ride_mm), _mm(self.h_r_mm)),
                source="goliath_prep.material.removal (fact kerf.crossing, rung R8k)"), None)
        return tuple(seen)

    @classmethod
    def from_context(cls, ctx: CheckContext) -> KerfRelaxation | None:
        """The relaxation when the context's gate ``kerf_crossing`` is open with its facts; None otherwise.

        A context whose values are facts while its gate is not open, or whose
        gate is open while its values are not facts, contradicts itself:
        ``ValueError``.
        """
        if not isinstance(ctx, CheckContext):
            raise TypeError("from_context needs a CheckContext, got %r" % (ctx,))
        params = [_ctx_param(ctx, name) for name in KERF_PARAMS]
        facts = [p.source == "FACT" for p in params]
        if ctx.gates.allowed("kerf_crossing") is not True:
            if any(facts):
                raise ValueError("the context holds R-KERF facts (%s) but its gate kerf_crossing is not open: the "
                                 "context contradicts itself" % ", ".join(n for n, f in zip(KERF_PARAMS, facts) if f))
            return None
        if not all(facts):
            raise ValueError("the gate kerf_crossing is open but the context's %s are not the R8k facts: the context "
                             "contradicts itself" % ", ".join(n for n, f in zip(KERF_PARAMS, facts) if not f))
        return cls(*params)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Clearance:
    """The full answer of a W-SURF query for one piece.

    * ``slack``: mm; violated at or below :data:`TOUCH_TOL_MM`, then ``witness``
      is set (None otherwise); ``math.inf`` when nothing was removed before the
      piece;
    * ``hazard_piece`` / ``hazard_prior``: the removing piece (its index) or the
      prior removal (its ref) the slack was measured to;
    * ``default_slack``: the slack with every removal a keep-out (R-KERF off),
      equal to ``slack`` without R-KERF; ``lifted`` is True when R-KERF lifted a
      hazard the default model refuses;
    * ``ride_mm``: under R-KERF, the longest run of contact through this piece
      along the track (0 without contact or without R-KERF).
    """

    slack: float
    witness: Witness | None
    hazard_piece: int | None
    hazard_prior: str | None
    default_slack: float
    lifted: bool
    ride_mm: float = 0.0


@dataclass(frozen=True)
class _Hit:
    """The nearest hazard: an index entry (``pos``) or a prior region (``prior``), raw distance, slack, point."""

    pos: int | None
    prior: int | None
    raw: float
    slack: float
    point: tuple[float, float]
    margins: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class _Item:
    """One element of a track: the path during piece ``j``, its wheel margins and its sagitta allowance."""

    geom: LineString | Point
    j: int
    margins: tuple[tuple[str, float], ...]
    radius: float
    sagitta: float


def _geometry(value: object) -> BaseGeometry:
    if not isinstance(value, _GEOM_TYPES):
        raise TypeError("the wheel geometry must be a shapely LineString or Point (the wheel centre's path) or a "
                        "Polygon or MultiPolygon (a region holding the wheel discs), got %r" % (value,))
    if value.is_empty:
        raise ValueError("the wheel geometry is empty")
    if not np.isfinite(shapely.get_coordinates(value)).all():
        raise ValueError("the wheel geometry has a coordinate that is not finite")
    if not value.is_valid:
        raise ValueError("the wheel geometry is not valid (%s)" % shapely.is_valid_reason(value))
    return value


def _ends(geom: LineString | Point) -> tuple[tuple[float, float], tuple[float, float]]:
    coords = shapely.get_coordinates(geom)
    return (float(coords[0][0]), float(coords[0][1])), (float(coords[-1][0]), float(coords[-1][1]))


def _path_frame(geom: LineString | Point) -> tuple[float, float, float, float, float]:
    """(ax, ay, vx, vy, length): a path's start, unit direction (0, 0 for a point) and length."""
    (ax, ay), (bx, by) = _ends(geom)
    if isinstance(geom, Point):
        return ax, ay, 0.0, 0.0, 0.0
    if len(shapely.get_coordinates(geom)) != 2:
        raise ValueError("a wheel path is one straight segment, got %d vertices" % len(shapely.get_coordinates(geom)))
    dx, dy = bx - ax, by - ay
    length = math.sqrt(dx * dx + dy * dy)
    return ax, ay, dx / length, dy / length, length


def _linear_between(f0: float, f1: float, lo: float, hi: float) -> tuple[float, float] | None:
    """The t with lo <= f0 + f1 t <= hi."""
    if f1 == 0.0:
        return (-math.inf, math.inf) if lo <= f0 <= hi else None
    t1, t2 = (lo - f0) / f1, (hi - f0) / f1
    return (t1, t2) if t1 <= t2 else (t2, t1)


def _capsule_interval(frame: tuple[float, float, float, float, float], q0: tuple[float, float],
                      q1: tuple[float, float], radius: float) -> tuple[float, float] | None:
    """The t in [0, length] where the path point ``a + t v`` is within ``radius`` of the segment q0-q1, or None.

    The capsule is convex, so the set is one interval: the union of the
    intervals of its two end discs and of its rectangle. ``radius`` is widened
    by 1 nm, on the safe side.
    """
    ax, ay, vx, vy, length = frame
    r = radius + _PAD_MM
    lo, hi = math.inf, -math.inf
    for cx, cy in (q0, q1):
        wx, wy = ax - cx, ay - cy
        beta = vx * wx + vy * wy
        gamma = wx * wx + wy * wy - r * r
        disc = beta * beta - gamma
        if disc >= 0.0:
            root = math.sqrt(disc)
            lo, hi = min(lo, -beta - root), max(hi, -beta + root)
    ux, uy = q1[0] - q0[0], q1[1] - q0[1]
    span = math.sqrt(ux * ux + uy * uy)
    if span > 0.0:
        ux, uy = ux / span, uy / span
        wx, wy = ax - q0[0], ay - q0[1]
        along = _linear_between(wx * ux + wy * uy, vx * ux + vy * uy, 0.0, span)
        across = _linear_between(-wx * uy + wy * ux, -vx * uy + vy * ux, -r, r)
        if along is not None and across is not None:
            t0, t1 = max(along[0], across[0]), min(along[1], across[1])
            if t0 <= t1:
                lo, hi = min(lo, t0), max(hi, t1)
    if lo > hi:
        return None
    lo, hi = max(lo, 0.0), min(hi, length)
    if lo > hi:
        return None
    return lo, hi


def _rings(geom: Polygon | MultiPolygon) -> list[np.ndarray]:
    parts = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
    out = []
    for part in parts:
        out.append(np.asarray(part.exterior.coords))
        out.extend(np.asarray(ring.coords) for ring in part.interiors)
    return out


def _merge(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    spans = sorted(spans)
    merged: list[tuple[float, float]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + _JOIN_TOL_MM:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _polygon_intervals(path: LineString | Point, frame: tuple[float, float, float, float, float],
                       region: Polygon | MultiPolygon, radius: float) -> list[tuple[float, float]]:
    """The intervals of t where the path is within ``radius`` of a region: inside it, or near an edge of it."""
    spans: list[tuple[float, float]] = []
    for ring in _rings(region):
        for k in range(len(ring) - 1):
            iv = _capsule_interval(frame, (float(ring[k][0]), float(ring[k][1])),
                                   (float(ring[k + 1][0]), float(ring[k + 1][1])), radius)
            if iv is not None:
                spans.append(iv)
    inside = path.intersection(region)
    if not inside.is_empty:
        parts = list(inside.geoms) if hasattr(inside, "geoms") else [inside]
        for part in parts:
            ts = [float(path.project(Point(xy))) for xy in shapely.get_coordinates(part)]
            if ts:
                spans.append((min(ts), max(ts)))
    return _merge(spans)


def _buffer(geom: Any, radius: Any) -> Any:
    """A GEOS buffer whose arcs hold the true circles of ``radius`` (radius enlarged for GEOS's chords)."""
    return shapely.buffer(geom, np.asarray(radius, dtype=float) * _INFLATE + _PAD_MM, quad_segs=_QUAD_SEGS)


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------


class RemovalIndex:
    """What the pieces of a program remove, over time, and how close a wheel comes to it (see the module docs).

    Build it with :meth:`build` (raw values) or :meth:`from_context` (the
    context's values, recorded with their provenance in :attr:`params`).
    Immutable once built; queries do not change it.
    """

    def __init__(self, *, pieces: PieceArray, r_tool: float, delta_tool: float, prior: PriorRemoval,
                 prior_declared_in: str, footprint: FootprintSet, rho: float, kerf: KerfRelaxation | None,
                 params: Mapping[str, Param[Any]]) -> None:
        self.pieces = pieces
        self.r_tool_mm = r_tool
        self.delta_tool_mm = delta_tool
        self.prior = prior
        self.footprint = footprint
        self.rho_mm = rho
        self.kerf = kerf
        self.params: Mapping[str, Param[Any]] = FrozenMapping(dict(params))
        self.delta_mm = float(pieces.delta_mm)
        self.e_max_mm = float(pieces.e_max.value)
        self._prior_declared_in = prior_declared_in
        data = pieces.data
        self._n = len(data)
        self._cut = np.asarray(data["cut"], dtype=np.int64)
        self._line = np.asarray(data["src_line"], dtype=np.int64)
        self._sag = np.asarray(data["sagitta"], dtype=float)
        self._x0, self._y0 = np.asarray(data["x0"], dtype=float), np.asarray(data["y0"], dtype=float)
        self._x1, self._y1 = np.asarray(data["x1"], dtype=float), np.asarray(data["y1"], dtype=float)
        material = np.nonzero(np.asarray(data["in_material"], dtype=bool))[0]
        self._idx = material.astype(np.int64)
        self._entry_of = np.full(self._n, -1, dtype=np.int64)
        self._entry_of[self._idx] = np.arange(len(self._idx), dtype=np.int64)
        self._geoms = np.array([self._chord(int(j), (0.0, 0.0)) for j in self._idx], dtype=object)
        self._r = (r_tool + delta_tool) + self._sag[self._idx]
        self._depth = -np.asarray(data["zmin"], dtype=float)[self._idx] + 0.0
        self._tree = STRtree(self._geoms) if len(self._geoms) else None
        self._r_max = float(self._r.max()) if len(self._r) else 0.0
        self._prior_geoms = np.array([r.polygon for r in prior.regions], dtype=object)
        self._prior_depth = np.array([r.depth_mm for r in prior.regions], dtype=float)
        self._prior_ref = tuple(r.ref for r in prior.regions)
        bounds = [shapely.total_bounds(self._geoms)] if len(self._geoms) else []
        if len(self._prior_geoms):
            bounds.append(shapely.total_bounds(self._prior_geoms))
        self._extent = None if not bounds else (min(b[0] for b in bounds), min(b[1] for b in bounds),
                                                max(b[2] for b in bounds), max(b[3] for b in bounds))
        self._capsules: np.ndarray | None = None
        if kerf is not None:
            log.warning("R-KERF in force for this removal index: w_b = %s mm, L_ride = %s mm, h_r = %s mm (fact "
                        "kerf.crossing, rung R8k)", _mm(kerf.w_b_mm), _mm(kerf.l_ride_mm), _mm(kerf.h_r_mm))

    # -- construction --------------------------------------------------------

    @classmethod
    def build(cls, pieces: PieceArray, r_tool: float, delta_tool: float, prior: PriorRemoval | None, *,
              footprint: FootprintSet, rho: float, kerf: KerfRelaxation | None = None,
              params: Mapping[str, Param[Any]] | None = None) -> RemovalIndex:
        """Index the removal of ``pieces`` (an STRtree over the in-material chords), seeded with ``prior``.

        ``footprint`` (the active model) and ``rho`` (the wheel disc radius)
        are needed to assert the time invariant at start-up on these very
        pieces: their length bound must be at most :func:`derive_delta`'s Delta
        and pass :func:`assert_time_invariant`, every piece must be within it
        and every sagitta within ``e_max``; otherwise :class:`InvariantError`.
        ``prior`` None means no prior removal (disclosed: the panel is taken as
        intact). ``kerf`` is R-KERF (None: closed). ``params`` are the Params
        the values came from, recorded as given (:meth:`from_context` fills it).
        """
        return cls._build(pieces, r_tool, delta_tool, prior, footprint=footprint, rho=rho, kerf=kerf,
                          params=params, prior_declared_in="the removal index's caller (no prior removal given)")

    @classmethod
    def _build(cls, pieces: PieceArray, r_tool: float, delta_tool: float, prior: PriorRemoval | None, *,
               footprint: FootprintSet, rho: float, kerf: KerfRelaxation | None,
               params: Mapping[str, Param[Any]] | None, prior_declared_in: str) -> RemovalIndex:
        if not isinstance(pieces, PieceArray):
            raise TypeError("pieces must be a PieceArray (goliath_prep.pieces.pieces), got %r" % (pieces,))
        r_tool = _positive(r_tool, "r_tool")
        delta_tool = _non_negative(delta_tool, "delta_tool")
        if prior is None:
            prior = PriorRemoval(())
        elif not isinstance(prior, PriorRemoval):
            raise TypeError("prior must be a PriorRemoval or None, got %r" % (prior,))
        footprint = _footprint(footprint)
        rho = _positive(rho, "rho")
        if kerf is not None and not isinstance(kerf, KerfRelaxation):
            raise TypeError("kerf must be a KerfRelaxation (from the R8k fact) or None, got %r" % (kerf,))
        recorded = {} if params is None else dict(params)
        for name, param in recorded.items():
            if not isinstance(param, Param):
                raise TypeError("params[%r] must be a Param, got %r" % (name, param))
        cls._assert_pieces(pieces, footprint, rho, r_tool, delta_tool, kerf.w_b_mm if kerf is not None else 0.0)
        return cls(pieces=pieces, r_tool=r_tool, delta_tool=delta_tool, prior=prior,
                   prior_declared_in=prior_declared_in, footprint=footprint, rho=rho, kerf=kerf, params=recorded)

    @staticmethod
    def _assert_pieces(pieces: PieceArray, footprint: FootprintSet, rho: float, r_tool: float, delta_tool: float,
                       w_b: float) -> None:
        e_max = _non_negative(pieces.e_max.value, "the pieces' e_max")
        delta = _positive(pieces.delta_mm, "the pieces' delta")
        allowed = derive_delta(footprint, rho, r_tool, delta_tool, e_max, w_b=w_b)
        if delta > allowed:
            raise _invariant_error(
                "the pieces were cut with Delta = %s mm, but the active footprint, rho = %s mm and the tool allow at "
                "most %s mm (safety-model 7.3): cut them again with the derived Delta"
                % (_mm(delta), _mm(rho), _mm(allowed)), "pieces longer than the derived Delta",
                value=delta, limit=allowed)
        assert_time_invariant(footprint, rho, r_tool, delta_tool, e_max, delta, w_b=w_b)
        data = pieces.data
        if len(data):
            dx = np.asarray(data["x1"], dtype=float) - np.asarray(data["x0"], dtype=float)
            dy = np.asarray(data["y1"], dtype=float) - np.asarray(data["y0"], dtype=float)
            longest = float(np.sqrt(dx * dx + dy * dy).max())
            if longest > delta + _PAD_MM:
                raise _invariant_error("a piece is %r mm long, above the pieces' Delta = %s mm" % (longest, _mm(delta)),
                                       "a piece longer than Delta", value=longest, limit=delta)
            widest = float(np.asarray(data["sagitta"], dtype=float).max())
            if widest > e_max:
                raise _invariant_error("a piece's sagitta is %r mm, above e_max = %s mm" % (widest, _mm(e_max)),
                                       "a piece's sagitta above e_max", value=widest, limit=e_max)

    @classmethod
    def from_context(cls, pieces: PieceArray, ctx: CheckContext) -> RemovalIndex:
        """The index for ``pieces`` with every value from the context (the tool, runout, rho, footprint, prior
        removal, R-KERF), each recorded in :attr:`params` with its provenance."""
        if not isinstance(ctx, CheckContext):
            raise TypeError("from_context needs a CheckContext (goliath_prep.context.build_context), got %r" % (ctx,))
        if not isinstance(pieces, PieceArray):
            raise TypeError("pieces must be a PieceArray (goliath_prep.pieces.pieces), got %r" % (pieces,))
        delta = delta_for(ctx)
        if pieces.delta_mm > delta.value:
            raise _invariant_error("the pieces were cut with Delta = %s mm, but the context allows at most %s mm (%s)"
                                   % (_mm(pieces.delta_mm), _mm(delta.value), delta.ref),
                                   "pieces longer than the derived Delta",
                                   value=float(pieces.delta_mm), limit=float(delta.value))
        r_tool = r_tool_param(_ctx_param(ctx, "tool_diameter"))
        runout = _ctx_param(ctx, "delta_tool")
        prior = PriorRemoval.from_context(ctx)
        kerf = KerfRelaxation.from_context(ctx)
        params: dict[str, Param[Any]] = {"r_tool": r_tool, "delta_tool": runout, "piece_length_max": delta,
                                         "rho": _ctx_param(ctx, "rho"),
                                         "check_chord_tol": _ctx_param(ctx, "check_chord_tol")}
        if kerf is not None:
            params.update(kerf.params)
        return cls._build(pieces, r_tool.value, runout.value, prior, footprint=ctx.footprint, rho=ctx.rho, kerf=kerf,
                          params=params, prior_declared_in="job.toml (no [[prior_removal]] entry)")

    # -- simple views --------------------------------------------------------

    def __len__(self) -> int:
        """The number of pieces that remove material."""
        return len(self._idx)

    @property
    def disclosures(self) -> tuple[Disclosure, ...]:
        """What the index's answers rely on, to be shown wherever they are: R-KERF while it is in force (amber),
        and the intact-panel default when no prior removal is declared (info)."""
        out: list[Disclosure] = []
        if self.kerf is not None:
            out.extend(self.kerf.disclosures)
        for param in self.params.values():
            for d in param.all_disclosures():
                if d.id == KERF_DISCLOSURE_ID and d not in out:
                    out.append(d)
        if not self.prior.regions:
            out.append(Disclosure(
                id=PRIOR_DEFAULT_ID, severity="info",
                text="no prior removal is declared, so Rem(0) is empty: the checks take the panel surface within "
                     "the robot's reach as intact (no holes, pockets or kerfs); the arming checklist has the operator "
                     "confirm it (red-team PHYS-06)",
                source=self._prior_declared_in))
        return tuple(out)

    def _piece(self, j: object) -> int:
        if isinstance(j, bool) or not isinstance(j, numbers.Integral):
            raise TypeError("a piece index is an int, got %r" % (j,))
        if not 0 <= int(j) < self._n:
            raise ValueError("piece %r is not a piece of this program (0..%d)" % (j, self._n - 1))
        return int(j)

    def _entry(self, j: object) -> int:
        piece = self._piece(j)
        entry = int(self._entry_of[piece])
        if entry < 0:
            raise ValueError("piece %d is not in the material: it removes nothing" % piece)
        return entry

    def depth(self, j: int) -> float:
        """d_j: the deepest depth of in-material piece ``j``, in mm below the stock top."""
        return float(self._depth[self._entry(j)])

    def removal_radius(self, j: int) -> float:
        """r_tool + delta_tool + e_j of in-material piece ``j``, in mm."""
        return float(self._r[self._entry(j)])

    def _before(self, before_piece: object) -> int:
        if isinstance(before_piece, bool) or not isinstance(before_piece, numbers.Integral):
            raise TypeError("before_piece is a piece index (an int), got %r" % (before_piece,))
        j = int(before_piece)
        if not 0 <= j <= self._n:
            raise ValueError("before_piece must be within 0..%d (the number of pieces means after the last one), "
                             "got %d" % (self._n, j))
        return j

    def _upto(self, j: int) -> int:
        """The number of index entries whose piece is before ``j`` (entries are in time order)."""
        return int(np.searchsorted(self._idx, j, side="left"))

    def _chord(self, j: int, offset: tuple[float, float]) -> LineString | Point:
        a = (float(self._x0[j]) + offset[0], float(self._y0[j]) + offset[1])
        b = (float(self._x1[j]) + offset[0], float(self._y1[j]) + offset[1])
        return Point(a) if a == b else LineString([a, b])

    def _capsule_polys(self) -> np.ndarray:
        if self._capsules is None:
            self._capsules = _buffer(self._geoms, self._r) if len(self._geoms) else np.array([], dtype=object)
        return self._capsules

    # -- removed region and depths -------------------------------------------

    def removed_region(self, before_piece: int, *, deeper_than: float | None = None) -> BaseGeometry:
        """Rem(before_piece) as polygons that hold the exact set; only removal deeper than ``deeper_than`` if given.

        Prior removal included; R-KERF's h_r does not apply (this is the
        material, not a wheel rule). An empty Polygon when nothing is removed.
        """
        j = self._before(before_piece)
        k = self._upto(j)
        limit = None if deeper_than is None else _real(deeper_than, "deeper_than")
        keep = np.ones(k, dtype=bool) if limit is None else self._depth[:k] > limit
        shapes = list(self._capsule_polys()[:k][keep])
        prior_keep = (np.ones(len(self._prior_geoms), dtype=bool) if limit is None
                      else self._prior_depth > limit)
        shapes.extend(self._prior_geoms[prior_keep])
        if not shapes:
            return Polygon()
        return shapely.union_all(np.array(shapes, dtype=object))

    def deepest_under(self, geom: BaseGeometry, before_piece: int) -> tuple[float, int | str] | None:
        """The deepest removal before ``before_piece`` that ``geom`` reaches: (depth mm, piece index or prior ref).

        None when it reaches none; among equal depths the earliest. For T-PASS
        (the step-down from the deepest earlier removal under the tool), not a
        wheel rule.
        """
        geom = _geometry(geom)
        k = self._upto(self._before(before_piece))
        best: tuple[float, int | str] | None = None
        if k and self._tree is not None:
            pos = self._tree.query(geom, predicate="dwithin", distance=self._r_max)
            pos = np.sort(pos[pos < k])
            if pos.size:
                pos = pos[shapely.distance(geom, self._geoms[pos]) <= self._r[pos] + TOUCH_TOL_MM]
            for p in pos:
                if best is None or self._depth[p] > best[0]:
                    best = (float(self._depth[p]), int(self._idx[p]))
        if len(self._prior_geoms):
            hit = shapely.distance(geom, self._prior_geoms) <= TOUCH_TOL_MM
            for i in np.nonzero(hit)[0]:
                if best is None or self._prior_depth[i] > best[0]:
                    best = (float(self._prior_depth[i]), self._prior_ref[i])
        return best

    # -- the W-SURF query ----------------------------------------------------

    def clearance(self, wheel_geom: BaseGeometry, before_piece: int, rho: float) -> tuple[float, Witness | None]:
        """(slack, witness or None) of ``wheel_geom`` grown by ``rho`` against Rem(``before_piece``) (module docs)."""
        result = self.query(wheel_geom, before_piece, rho)
        return result.slack, result.witness

    def query(self, wheel_geom: BaseGeometry, before_piece: int, rho: float) -> Clearance:
        """The full :class:`Clearance` of one geometry during piece ``before_piece``.

        ``before_piece`` is the time: j in 0..n-1 means during piece j (its own
        removal left out, under the guard of the time invariant), n means after
        the last piece. A path (LineString or Point) is a track of one piece.
        """
        geom = _geometry(wheel_geom)
        j = self._before(before_piece)
        radius = _non_negative(rho, "rho")
        margins = (("rho", radius),)
        if isinstance(geom, (LineString, Point)) and self.kerf is not None and self.kerf.w_b_mm > 0.0:
            _path_frame(geom)  # a crossing is judged along one straight path
            return self._kerf_track([_Item(geom, j, margins, radius, 0.0)])[0]
        return self._single(geom, j, margins, radius)

    def wheel_clearance(self, piece: int, offset: tuple[float, float], rho: float) -> Clearance:
        """W-SURF for one support during piece ``piece``: its centre's chord (the piece's chord moved by ``offset``,
        the support's offset d_k from the tool at the cut's heading), grown by ``rho + e_j``."""
        return self.wheel_track([piece], offset, rho)[0]

    def wheel_track(self, pieces: Sequence[int], offset: tuple[float, float], rho: float) -> list[Clearance]:
        """W-SURF for one support over consecutive pieces at one heading (a cut), in time order.

        Each piece's path is its chord moved by ``offset``, grown by
        ``rho + e_j`` (margin ``e_wheel``). Without R-KERF, or with its w_b = 0,
        every piece is answered alone, exactly as :meth:`wheel_clearance`. With
        a w_b above 0 the runs of contact are chained along the track, each
        piece is judged by the longest run through it, and a run that reaches
        either end of the track is never lifted (module documentation).
        """
        if isinstance(pieces, (str, bytes, Mapping)) or not isinstance(pieces, Iterable):
            raise TypeError("a track is a sequence of consecutive piece indices, got %r" % (pieces,))
        js = [self._piece(j) for j in pieces]
        if not js:
            raise ValueError("a track holds at least one piece")
        for left, right in zip(js, js[1:]):
            if right != left + 1:
                raise ValueError("a track is consecutive pieces in time order: piece %d after piece %d" % (right, left))
        if isinstance(offset, (str, bytes)) or not hasattr(offset, "__len__") or len(offset) != 2:
            raise TypeError("offset is the support's (dx, dy) from the tool, in mm, got %r" % (offset,))
        d = (_real(offset[0], "offset dx"), _real(offset[1], "offset dy"))
        rho = _non_negative(rho, "rho")
        items = []
        for j in js:
            e_j = float(self._sag[j])
            items.append(_Item(self._chord(j, d), j, (("rho", rho), ("e_wheel", e_j)), rho + e_j, e_j))
        for left, right in zip(items, items[1:]):
            end, start = _ends(left.geom)[1], _ends(right.geom)[0]
            if abs(end[0] - start[0]) > _JOIN_TOL_MM or abs(end[1] - start[1]) > _JOIN_TOL_MM:
                raise ValueError("a track is one continuous path: piece %d starts at %r, but piece %d ended at %r"
                                 % (right.j, start, left.j, end))
        return self._evaluate(items)

    # -- internals: the plain query ------------------------------------------

    def _evaluate(self, items: list[_Item]) -> list[Clearance]:
        if self.kerf is None or self.kerf.w_b_mm == 0.0:
            return [self._single(item.geom, item.j, item.margins, item.radius) for item in items]
        return self._kerf_track(items)

    def _single(self, geom: BaseGeometry, j: int, margins: tuple[tuple[str, float], ...],
                radius: float) -> Clearance:
        """The default answer (with R-KERF only its h_r rule): every removal left is a keep-out."""
        self._guard(geom, j, radius, 0.0)
        k = self._upto(j)
        default = self._nearest(geom, k, radius, margins, None)
        relaxed = default if self.kerf is None else self._nearest(geom, k, radius, margins, self.kerf.h_r_mm)
        return self._plain(relaxed, j, default)

    def _guard(self, geom: BaseGeometry, j: int, radius: float, extra: float) -> None:
        """Re-check the time invariant for this query: the geometry must stay clear of piece j's own removal."""
        if j >= self._n:
            return
        entry = int(self._entry_of[j])
        if entry < 0:
            return
        raw = float(shapely.distance(geom, self._geoms[entry]))
        margin = radius + float(self._r[entry]) + extra
        if raw - margin <= TOUCH_TOL_MM:
            raise _invariant_error(
                "the geometry checked during piece %d comes %s mm from that piece's own toolpath, within rho + r_tool "
                "+ delta_tool + e_j%s = %s mm: the piece's own removal could be under it, which W-SURF leaves out on "
                "the strength of the time invariant (safety-model 7.3)"
                % (j, _mm(raw), " + w_b" if extra else "", _mm(margin), ),
                "a query geometry within reach of the removal of the piece it is checked during",
                value=raw, limit=margin)

    def _bound(self, geom: BaseGeometry) -> float:
        """A distance beyond which no removal lies from ``geom`` (the diagonal of both extents together)."""
        if self._extent is None:
            return 0.0
        gx0, gy0, gx1, gy1 = geom.bounds
        x0, y0 = min(gx0, self._extent[0]), min(gy0, self._extent[1])
        x1, y1 = max(gx1, self._extent[2]), max(gy1, self._extent[3])
        return math.sqrt((x1 - x0) * (x1 - x0) + (y1 - y0) * (y1 - y0)) + self._r_max + 1.0

    def _nearest(self, geom: BaseGeometry, k: int, radius: float, margins: tuple[tuple[str, float], ...],
                 min_depth: float | None) -> _Hit | None:
        """The nearest removal by the first ``k`` entries and the prior removal (at least ``min_depth`` deep)."""
        best: _Hit | None = None
        if k and self._tree is not None:
            depth_ok = None if min_depth is None else self._depth >= min_depth
            if depth_ok is None or depth_ok[:k].any():
                reach = radius + self._r_max + SEARCH_MM
                bound = self._bound(geom)
                while True:
                    pos = self._tree.query(geom, predicate="dwithin", distance=reach)
                    pos = np.sort(pos[pos < k])
                    if depth_ok is not None:
                        pos = pos[depth_ok[pos]]
                    if pos.size:
                        raw = shapely.distance(geom, self._geoms[pos])
                        dist = raw - self._r[pos]
                        i = int(np.argmin(dist))  # the first minimum: the earliest piece among equals
                        if dist[i] <= reach - self._r_max or reach >= bound:
                            best = self._piece_hit(geom, int(pos[i]), float(raw[i]), radius, margins)
                            break
                    elif reach >= bound:
                        break
                    reach *= 4.0
        if len(self._prior_geoms):
            keep = np.arange(len(self._prior_geoms))
            if min_depth is not None:
                keep = keep[self._prior_depth >= min_depth]
            if keep.size:
                raw = shapely.distance(geom, self._prior_geoms[keep])
                i = int(np.argmin(raw))
                if best is None or float(raw[i]) - radius < best.slack:
                    best = self._prior_hit(geom, int(keep[i]), float(raw[i]), radius, margins)
        return best

    def _piece_hit(self, geom: BaseGeometry, pos: int, raw: float, radius: float,
                   margins: tuple[tuple[str, float], ...]) -> _Hit:
        r = float(self._r[pos])
        line = shapely.shortest_line(geom, self._geoms[pos])
        (px, py), (qx, qy) = shapely.get_coordinates(line)
        if raw > r:  # the point of the removal nearest the geometry: from the toolpath towards it by r
            point = (float(qx + (px - qx) * (r / raw)), float(qy + (py - qy) * (r / raw)))
        else:
            point = (float(px), float(py))
        sag = r - (self.r_tool_mm + self.delta_tool_mm)
        all_margins = margins + (("r_tool", self.r_tool_mm), ("delta_tool", self.delta_tool_mm),
                                 ("e_removal", max(sag, 0.0)))
        return _Hit(pos=pos, prior=None, raw=raw, slack=raw - r - radius, point=point, margins=all_margins)

    def _prior_hit(self, geom: BaseGeometry, i: int, raw: float, radius: float,
                   margins: tuple[tuple[str, float], ...]) -> _Hit:
        line = shapely.shortest_line(geom, self._prior_geoms[i])
        qx, qy = shapely.get_coordinates(line)[-1]
        return _Hit(pos=None, prior=i, raw=raw, slack=raw - radius, point=(float(qx), float(qy)), margins=margins)

    def _describe(self, hit: _Hit) -> str:
        if hit.prior is not None:
            return "prior removal %s, depth %s mm, removed from the panel before this program" % (
                self._prior_ref[hit.prior], _mm(self._prior_depth[hit.prior]))
        assert hit.pos is not None
        piece = int(self._idx[hit.pos])
        cut = int(self._cut[piece])
        what = ("kerf of cut %d" % cut) if cut >= 0 else "removal"
        return "%s, line %d, depth %s mm, removed at piece %d" % (what, int(self._line[piece]),
                                                                  _mm(self._depth[hit.pos]), piece)

    def _where(self, j: int) -> dict[str, Any]:
        if j >= self._n:
            return {}
        cut = int(self._cut[j])
        return {"piece": j, "cut": cut if cut >= 0 else None}

    def _source(self, hit: _Hit | None) -> tuple[int | None, str | None]:
        if hit is None:
            return None, None
        if hit.prior is not None:
            return None, self._prior_ref[hit.prior]
        assert hit.pos is not None
        return int(self._idx[hit.pos]), None

    def _witness(self, hit: _Hit, j: int, hazard: str, *, slack: float | None = None, raw: bool = True) -> Witness:
        return Witness(**self._where(j), point=hit.point, hazard=hazard,
                       slack_mm=hit.slack if slack is None else slack,
                       raw_mm=hit.raw if raw else None, margins=hit.margins if raw else None)

    def _plain(self, hit: _Hit | None, j: int, default: _Hit | None) -> Clearance:
        """A Clearance from the nearest hazard (``default``: the nearest under the default model)."""
        slack = math.inf if hit is None else hit.slack
        default_slack = math.inf if default is None else default.slack
        witness = None
        if hit is not None and hit.slack <= TOUCH_TOL_MM:
            witness = self._witness(hit, j, self._describe(hit))
        piece, prior = self._source(hit)
        return Clearance(slack=slack, witness=witness, hazard_piece=piece, hazard_prior=prior,
                         default_slack=default_slack, lifted=default_slack <= TOUCH_TOL_MM and witness is None)

    # -- internals: R-KERF with w_b > 0 --------------------------------------

    def _contacts(self, item: _Item, k: int, h_r: float) -> list[tuple[float, float]]:
        """The intervals, in mm along the item's path, where its disc meets removal at least h_r deep."""
        frame = _path_frame(item.geom)
        spans: list[tuple[float, float]] = []
        if k and self._tree is not None:
            pos = self._tree.query(item.geom, predicate="dwithin", distance=item.radius + self._r_max + 1.0)
            pos = np.sort(pos[pos < k])
            pos = pos[self._depth[pos] >= h_r]
            for p in pos:
                (q0x, q0y), (q1x, q1y) = _ends(self._geoms[p])
                iv = _capsule_interval(frame, (q0x, q0y), (q1x, q1y), item.radius + float(self._r[p]))
                if iv is not None:
                    spans.append(iv)
        for i in range(len(self._prior_geoms)):
            if self._prior_depth[i] >= h_r and float(shapely.distance(item.geom, self._prior_geoms[i])) \
                    <= item.radius + 1.0:
                spans.extend(_polygon_intervals(item.geom, frame, self._prior_geoms[i], item.radius))
        return _merge(spans)

    def _wide(self, item: _Item, k: int) -> tuple[_Hit | None, float]:
        """The nearest wide removal (the opening by w_b) within the search window, and the wide slack.

        Returns (hit, slack): the hit when wide removal lies within the window
        (its slack exact), else (None, :data:`SEARCH_MM`), a lower bound.
        """
        assert self.kerf is not None
        w_b, h_r = self.kerf.w_b_mm, self.kerf.h_r_mm
        window = item.radius + w_b + SEARCH_MM
        shapes: list[Any] = []
        local: list[int] = []
        if k and self._tree is not None:
            pos = self._tree.query(item.geom, predicate="dwithin", distance=window + self._r_max)
            pos = np.sort(pos[pos < k])
            pos = pos[self._depth[pos] >= h_r]
            if pos.size:
                pos = pos[shapely.distance(item.geom, self._geoms[pos]) - self._r[pos] <= window]
            local = [int(p) for p in pos]
            shapes.extend(self._capsule_polys()[pos])
        priors = [i for i in range(len(self._prior_geoms)) if self._prior_depth[i] >= h_r
                  and float(shapely.distance(item.geom, self._prior_geoms[i])) <= window]
        shapes.extend(self._prior_geoms[i] for i in priors)
        if not shapes:
            return None, SEARCH_MM
        half = w_b / 2.0
        eroded = shapely.buffer(shapely.union_all(np.array(shapes, dtype=object)), -(half - _PAD_MM),
                                quad_segs=_QUAD_SEGS)
        if eroded.is_empty:
            return None, SEARCH_MM
        dist = float(shapely.distance(item.geom, eroded))
        if dist > window - half:
            return None, SEARCH_MM
        line = shapely.shortest_line(item.geom, eroded)
        (px, py), (ex, ey) = shapely.get_coordinates(line)
        if dist > half:  # the point of the wide removal nearest the path: from the eroded set towards it by w_b/2
            point = (float(ex + (px - ex) * (half / dist)), float(ey + (py - ey) * (half / dist)))
        else:
            point = (float(px), float(py))
        nearest = self._nearest_of(point, local, priors)
        margins = item.margins + (("w_b_half", half),)
        hit = _Hit(pos=nearest[0], prior=nearest[1], raw=dist, slack=dist - half - item.radius, point=point,
                   margins=margins)
        return hit, hit.slack

    def _nearest_of(self, point: tuple[float, float], entries: list[int], priors: list[int]) -> tuple[int | None,
                                                                                                      int | None]:
        """(entry, prior) of the removal nearest ``point`` among ``entries`` and ``priors``: what to name."""
        p = Point(point)
        best: tuple[float, int | None, int | None] | None = None
        for e in entries:
            d = float(shapely.distance(p, self._geoms[e])) - float(self._r[e])
            if best is None or d < best[0]:
                best = (d, e, None)
        for i in priors:
            d = float(shapely.distance(p, self._prior_geoms[i]))
            if best is None or d < best[0]:
                best = (d, None, i)
        assert best is not None
        return best[1], best[2]

    def _kerf_track(self, items: list[_Item]) -> list[Clearance]:
        assert self.kerf is not None
        w_b, l_ride, h_r = self.kerf.w_b_mm, self.kerf.l_ride_mm, self.kerf.h_r_mm
        for item in items:
            self._guard(item.geom, item.j, item.radius, w_b)
        ks = [self._upto(item.j) for item in items]
        defaults = [self._nearest(item.geom, k, item.radius, item.margins, None) for item, k in zip(items, ks)]
        relaxed = [self._nearest(item.geom, k, item.radius, item.margins, h_r) for item, k in zip(items, ks)]
        contacts: list[list[tuple[float, float]]] = []
        for item, k, hit in zip(items, ks, relaxed):
            spans = self._contacts(item, k, h_r)
            if not spans and hit is not None and hit.slack <= TOUCH_TOL_MM:
                # The nearest-distance test says the disc touches; the intervals' arithmetic missed it by float
                # noise. Count the touch at the nearest point of the path.
                t = float(item.geom.project(Point(shapely.get_coordinates(
                    shapely.shortest_line(item.geom, self._hazard_geom(hit)))[0])))
                spans = [(t, t)]
            contacts.append(spans)
        lengths = [_path_frame(item.geom)[4] for item in items]
        offsets: list[float] = []
        total = 0.0
        for length in lengths:
            offsets.append(total)
            total = total + length
        spans_all: list[tuple[float, float, int]] = []
        for n, spans in enumerate(contacts):
            for a, b in spans:
                spans_all.append((offsets[n] + a, offsets[n] + b, n))
        spans_all.sort()
        runs: list[list[float]] = []
        owners: list[set[int]] = []
        for a, b, n in spans_all:
            if runs and a <= runs[-1][1] + _JOIN_TOL_MM:
                runs[-1][1] = max(runs[-1][1], b)
                owners[-1].add(n)
            else:
                runs.append([a, b])
                owners.append({n})
        ride = [0.0] * len(items)
        open_run = [False] * len(items)
        for (a, b), who in zip(runs, owners):
            allowance = 0.0
            for n in sorted(who):
                if items[n].sagitta > 0.0:
                    allowance = allowance + 2.0 * items[n].sagitta
            length = (b - a) + allowance
            is_open = a <= _JOIN_TOL_MM or b >= total - _JOIN_TOL_MM
            for n in who:
                ride[n] = max(ride[n], length)
                open_run[n] = open_run[n] or is_open
        out = []
        for n, item in enumerate(items):
            default_slack = math.inf if defaults[n] is None else defaults[n].slack
            if not contacts[n]:
                result = self._plain(relaxed[n], item.j, defaults[n])
                out.append(result)
                continue
            out.append(self._kerf_judge(item, ks[n], relaxed[n], default_slack, ride[n], open_run[n], w_b, l_ride))
        return out

    def _hazard_geom(self, hit: _Hit) -> BaseGeometry:
        if hit.prior is not None:
            return self._prior_geoms[hit.prior]
        assert hit.pos is not None
        return self._geoms[hit.pos]

    def _kerf_judge(self, item: _Item, k: int, narrow: _Hit | None, default_slack: float, ride: float,
                    is_open: bool, w_b: float, l_ride: float) -> Clearance:
        """One track piece in contact with removal under R-KERF: wide, open run, ride length."""
        assert narrow is not None  # a contact means removal at least h_r deep lies within reach
        j = item.j
        wide, wide_slack = self._wide(item, k)
        candidates: list[tuple[float, Witness | None, _Hit]] = []
        if wide is not None:
            witness = None
            if wide.slack <= TOUCH_TOL_MM:
                witness = self._witness(wide, j, "removal wider than w_b = %s mm (R-KERF: a wheel never crosses it): "
                                                 "%s" % (_mm(w_b), self._describe(wide)))
            candidates.append((wide.slack, witness, wide))
        if is_open:
            # The contact intervals widen the radius by 1 nm, so the nearest slack can be just above 0 here: a touch.
            touch = narrow.slack <= 0.0
            witness = self._witness(
                narrow, j, "the wheel disc meets removal narrower than w_b = %s mm on a run that reaches the end of "
                           "the wheel track checked, so the run cannot be judged against L_ride (R-KERF lifts a "
                           "crossing only within a whole track): %s" % (_mm(w_b), self._describe(narrow)),
                slack=narrow.slack if touch else 0.0, raw=touch)
            candidates.append((narrow.slack if touch else 0.0, witness, narrow))
        else:
            slack = l_ride - ride
            witness = None
            if slack <= TOUCH_TOL_MM:
                witness = self._witness(
                    narrow, j, "the wheel disc meets removal narrower than w_b = %s mm over a run of %s mm, not less "
                               "than L_ride = %s mm (R-KERF, fact kerf.crossing, rung R8k): %s"
                               % (_mm(w_b), _mm(ride), _mm(l_ride), self._describe(narrow)), slack=slack, raw=False)
            candidates.append((slack, witness, narrow))
        violated = [c for c in candidates if c[1] is not None]
        if violated:
            slack, witness, hit = min(violated, key=lambda c: c[0])
        else:
            slack, witness, hit = min(candidates, key=lambda c: c[0])
            slack = min(slack, wide_slack)
        piece, prior = self._source(hit)
        lifted = default_slack <= TOUCH_TOL_MM and witness is None
        if lifted:
            log.debug("R-KERF lifted W-SURF at piece %d: %s", j, self._describe(narrow))
        return Clearance(slack=slack, witness=witness, hazard_piece=piece, hazard_prior=prior,
                         default_slack=default_slack, lifted=lifted, ride_mm=ride)
