# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The robot's footprint: heading, supports, footprint models and the rotation radius (unit P04).

Spec: docs/protocol/08-head-angle-and-cut-order.md (*Footprint model*,
*Emitting the C word*, *Constants*); docs/prep/safety-model.md sections 4.1
to 4.6. Pure arithmetic, no shapely: the shapes built on it are in
:mod:`goliath_prep.geom.sweeps`.

* **Heading.** ``theta(C) = (C + 90) mod 360`` for ``C`` on the 10-degree grid
  0..350 (:func:`theta`); angles are degrees counter-clockwise from +X and
  ``u(a) = (cos a, sin a)``; ``u(theta)`` points from the body toward the tool.
* **Supports.** Support k at tool point p is ``q_k = p - r_k * u(theta + delta_k)``,
  so its offset from the tool point is ``d_k(theta) = -r_k * u(theta + delta_k)``.
  The spec footprint is (402 mm, 0), (211 mm, +76 deg), (211 mm, -76 deg).
* **Footprint models** (safety-model 4.3). ``vendor`` is the spec footprint;
  ``measured`` the positions measured at rung R0b, with the spec position kept
  for a support nobody measured; under the **union** model (active once anything
  is measured) every check holds for each wheel at both its spec and its
  measured position (:func:`support_positions`).
* **Rotation radius** (safety-model 4.6, red-team PHYS-07). ``R_rot`` holds
  every support during a turn about any pivot between the tool point and the
  support centroid: ``max(R_max, d_c + R_c)``, rounded up to whole millimetres,
  404 mm with the spec values.

**Union model: which centroids** (*this kernel's reading*, the safest of the
possible ones). Each wheel may be at its spec or at its measured position,
independently, so the robot's true footprint is one of the combinations of
those positions, and its centroid is that combination's. :func:`footprint_radii`
takes every combination's centroid (2 to 8 of them) against every support
position of both models. That holds safety-model 4.6's union formula (both
models' centroids against every position) and more, so it is never smaller.

**The same numbers everywhere.** cos and sin come from
:func:`goliath_prep.heading_vendor.correctly_rounded_cos_sin` of ``alpha * pi / 180``
(the C library's differ in the last bit between platforms); sums are plain and
left to right, and lengths use ``math.sqrt`` (IEEE 754 rounds it correctly), never
``sum`` (compensated since Python 3.12) or ``math.hypot``. For the spec
footprint, :func:`supports` returns the same doubles as
:func:`goliath_prep.heading_vendor.support_points`, which the vendor-floor
assertion (the project design, section 4.11) compares with.

Inputs are refused, never guessed: a ``C`` off the grid, a non-finite
coordinate, a support with a distance that is not positive.
"""
from __future__ import annotations

import itertools
import math
import numbers
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from goliath_prep.core.types import FootprintSet
from goliath_prep.defaults import C_GRID, SPEC_PARAMS
from goliath_prep.heading_vendor import correctly_rounded_cos_sin

__all__ = [
    "C_GRID",
    "HEADING_OFFSET_DEG",
    "Footprint",
    "FootprintRadii",
    "ModelName",
    "SupportPosition",
    "Vec2",
    "body_point",
    "c_word",
    "centroid_offsets",
    "footprint_models",
    "footprint_radii",
    "rotation_radius",
    "support_offset",
    "support_offsets",
    "support_positions",
    "supports",
    "theta",
    "unit",
]

Vec2 = tuple[float, float]
ModelName = Literal["vendor", "measured"]
MODEL_NAMES: tuple[str, ...] = ("vendor", "measured")

HEADING_OFFSET_DEG: float = float(SPEC_PARAMS["heading_offset"].value)
"""The +90 deg of theta = (C + 90) mod 360 (spec 08 §Constants; docs/prep/params.json heading_offset, SPEC)."""

#: Above this |alpha| (degrees) :func:`unit` reduces the angle modulo 360 first (exact for doubles), so
#: the radian argument stays inside the correctly rounded routine's range. Below it, the argument is
#: spec 08's own ``alpha * pi / 180`` without reduction (the vendor reference's doubles).
_UNREDUCED_MAX_DEG = 720.0
#: Whole millimetres for R_rot are rounded up from the computed value plus this, so float noise in the
#: last bits can only add a millimetre, never lose one.
_ROUND_UP_GUARD_MM = 1e-9


# ---------------------------------------------------------------------------
# Validation


def _real(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError("%s must be a real number, got %r" % (what, value))
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
    return out + 0.0  # never -0.0


def _point(value: object, what: str) -> Vec2:
    if isinstance(value, (str, bytes)) or not hasattr(value, "__len__") or not hasattr(value, "__getitem__"):
        raise TypeError("%s must be an (x, y) pair of numbers in mm, got %r" % (what, value))
    if len(value) != 2:  # type: ignore[arg-type]
        raise TypeError("%s must be an (x, y) pair of numbers in mm, got %r" % (what, value))
    return (_real(value[0], what + " x"), _real(value[1], what + " y"))  # type: ignore[index]


def _c(value: object, what: str = "C") -> int:
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise TypeError("%s must be an int on the 10-degree grid 0..350, got %r" % (what, value))
    c = int(value)
    if c not in C_GRID:
        raise ValueError("%s must be on the 10-degree grid 0..350 (spec 08 §Emitting the C word), got %d" % (what, c))
    return c


def _support_pair(value: object, what: str) -> tuple[float, float]:
    if isinstance(value, (str, bytes)) or not hasattr(value, "__len__") or len(value) != 2:  # type: ignore[arg-type]
        raise ValueError("%s must be (r_mm, delta_deg), got %r" % (what, value))
    r = _positive(value[0], what + " r")  # type: ignore[index]
    delta = _real(value[1], what + " delta")  # type: ignore[index]
    if not -180.0 <= delta <= 180.0:
        raise ValueError("%s delta must be within -180..180 degrees, got %r" % (what, delta))
    return (r, delta)


# ---------------------------------------------------------------------------
# Heading and unit vectors


def theta(c: int) -> float:
    """The heading theta = (C + 90) mod 360 in degrees, for ``C`` on the grid 0, 10, ..., 350 (spec 08)."""
    return float((_c(c) + int(HEADING_OFFSET_DEG)) % 360)


def c_word(theta_deg: float) -> int:
    """The ``C`` the post-processor writes for a heading on the grid: (theta - 90 + 360) mod 360 (spec 08)."""
    t = _real(theta_deg, "theta")
    if not (0.0 <= t < 360.0 and t == math.floor(t)):
        raise ValueError("theta must be a whole number of degrees in [0, 360) on the C grid, got %r" % theta_deg)
    c = (int(t) - int(HEADING_OFFSET_DEG) + 360) % 360
    if c not in C_GRID:
        raise ValueError("theta %r is not on the 10-degree grid: C would be %d" % (theta_deg, c))
    return c


@lru_cache(maxsize=4096)
def _unit_cached(alpha: float) -> Vec2:
    if abs(alpha) > _UNREDUCED_MAX_DEG:
        alpha = math.fmod(alpha, 360.0)  # exact for doubles
    return correctly_rounded_cos_sin(alpha * math.pi / 180.0)


def unit(alpha_deg: float) -> Vec2:
    """u(alpha) = (cos, sin) of ``alpha * pi / 180``, each correctly rounded (the same on every platform)."""
    return _unit_cached(_real(alpha_deg, "angle") + 0.0)


# ---------------------------------------------------------------------------
# Footprint models and supports


@dataclass(frozen=True)
class Footprint:
    """One footprint model: ``supports`` = (r_k mm, delta_k deg) of q1 (rear), q2 (right), q3 (left).

    ``name`` is ``vendor`` (the spec footprint) or ``measured`` (the positions
    measured at rung R0b, with the spec position for a support nobody measured).
    Make them with :func:`footprint_models`.
    """

    name: ModelName
    supports: tuple[tuple[float, float], tuple[float, float], tuple[float, float]]

    def __post_init__(self) -> None:
        if self.name not in MODEL_NAMES:
            raise ValueError("Footprint name must be one of %s, got %r" % (MODEL_NAMES, self.name))
        items = self.supports
        if isinstance(items, (str, bytes)) or not hasattr(items, "__len__") or len(items) != 3:
            raise ValueError("Footprint needs three supports (q1, q2, q3), got %r" % (items,))
        object.__setattr__(self, "supports", tuple(
            _support_pair(s, "Footprint %s q%d" % (self.name, k + 1)) for k, s in enumerate(items)))


@dataclass(frozen=True)
class SupportPosition:
    """One position a wheel may have: support ``k`` (1 rear, 2 right, 3 left) of model ``model``."""

    k: int
    model: ModelName
    r: float
    delta: float

    def __post_init__(self) -> None:
        if isinstance(self.k, bool) or self.k not in (1, 2, 3):
            raise ValueError("SupportPosition k must be 1, 2 or 3, got %r" % (self.k,))
        if self.model not in MODEL_NAMES:
            raise ValueError("SupportPosition model must be one of %s, got %r" % (MODEL_NAMES, self.model))
        r, delta = _support_pair((self.r, self.delta), "SupportPosition q%d" % self.k)
        object.__setattr__(self, "r", r)
        object.__setattr__(self, "delta", delta)

    @property
    def label(self) -> str:
        """``q2`` for a spec position, ``q2 (measured)`` for a measured one: how a witness names the wheel."""
        return "q%d" % self.k if self.model == "vendor" else "q%d (measured)" % self.k


def _footprint_set(fp: object) -> FootprintSet:
    if not isinstance(fp, FootprintSet):
        raise TypeError("expected a FootprintSet (goliath_prep.core.types), got %r" % (fp,))
    return fp


def footprint_models(fp: FootprintSet) -> tuple[Footprint, ...]:
    """The models of ``fp``: ``vendor``, then ``measured`` when anything was measured (safety-model 4.3).

    The measured model keeps the spec position of a support whose distance or
    angle was not measured: a measurement adds a position, it never removes one.
    """
    fp = _footprint_set(fp)
    models = [Footprint("vendor", fp.vendor)]  # type: ignore[arg-type]
    if fp.measured is not None:
        models.append(Footprint("measured", tuple(  # type: ignore[arg-type]
            spec if measured is None else measured for spec, measured in zip(fp.vendor, fp.measured))))
    return tuple(models)


def support_positions(fp: FootprintSet) -> tuple[SupportPosition, ...]:
    """Every position a wheel may have under the active model: the three spec positions, then each measured one.

    Under the union model each check must hold at all of them. A measured
    position identical to the spec one adds nothing and is left out.
    """
    fp = _footprint_set(fp)
    out = [SupportPosition(k + 1, "vendor", r, delta) for k, (r, delta) in enumerate(fp.vendor)]
    for k, measured in enumerate(fp.measured or ()):
        if measured is not None and tuple(measured) != tuple(fp.vendor[k]):
            out.append(SupportPosition(k + 1, "measured", measured[0], measured[1]))
    return tuple(out)


def support_offset(r: float, delta: float, theta_deg: float) -> Vec2:
    """d(theta) = -r * u(theta + delta): a support's offset from the tool point at heading ``theta_deg``."""
    r, delta = _support_pair((r, delta), "support")
    ux, uy = unit(_real(theta_deg, "theta") + delta)
    return (-r * ux, -r * uy)


def supports(p: Vec2, c: int, model: Footprint) -> tuple[Vec2, Vec2, Vec2]:
    """q1, q2, q3 of ``model`` for tool point ``p`` at ``C = c``: ``q_k = p - r_k * u(theta + delta_k)``."""
    if not isinstance(model, Footprint):
        raise TypeError("supports() takes one Footprint model (see footprint_models()), got %r" % (model,))
    px, py = _point(p, "tool point")
    t = theta(c)
    out = []
    for r, delta in model.supports:
        ux, uy = unit(t + delta)
        out.append((px - r * ux, py - r * uy))
    return (out[0], out[1], out[2])


def support_offsets(c: int, fp: FootprintSet) -> tuple[tuple[SupportPosition, Vec2], ...]:
    """(position, offset from the tool point) for every position of :func:`support_positions` at ``C = c``."""
    t = theta(c)
    return tuple((pos, support_offset(pos.r, pos.delta, t)) for pos in support_positions(fp))


# ---------------------------------------------------------------------------
# Centroids and radii (safety-model 4.6)


def _length(v: Vec2) -> float:
    return math.sqrt(v[0] * v[0] + v[1] * v[1])


def _combinations(fp: FootprintSet) -> list[tuple[tuple[float, float], ...]]:
    choices = []
    for k, spec in enumerate(fp.vendor):
        options = [spec]
        if fp.measured is not None and fp.measured[k] is not None and tuple(fp.measured[k]) != tuple(spec):
            options.append(fp.measured[k])
        choices.append(options)
    return [tuple(combo) for combo in itertools.product(*choices)]


def centroid_offsets(fp: FootprintSet, c: int) -> tuple[Vec2, ...]:
    """The support centroid, relative to the tool point at ``C = c``, of every footprint the robot may have.

    One for the vendor model; under the union model one per combination of spec
    and measured positions (each wheel independently at either), spec positions
    first, in the order of ``itertools.product``.
    """
    fp = _footprint_set(fp)
    t = theta(c)
    out = []
    for combo in _combinations(fp):
        d1, d2, d3 = (support_offset(r, delta, t) for r, delta in combo)
        out.append(((d1[0] + d2[0] + d3[0]) / 3.0, (d1[1] + d2[1] + d3[1]) / 3.0))
    return tuple(out)


@dataclass(frozen=True)
class FootprintRadii:
    """The radii of a footprint set, in mm (safety-model 4.6; docs/prep/params.json, DERIVED).

    * ``r_max``, ``r_min``: the largest and smallest support distance over every position;
    * ``d_c``: the distance from the tool point to the centroid that gives ``r_rot_exact``,
      and ``r_c`` the farthest support position from that centroid;
    * ``r_rot_exact``: max(r_max, max over centroids c and positions d of |c| + |d - c|);
    * ``r_rot``: ``r_rot_exact`` rounded up to whole millimetres (the value the checks use).

    ``r_max`` and ``r_min`` are support distances as given; the other values are
    computed doubles: quantize them (:func:`goliath_prep.core.canon.quantize`,
    upwards) before putting them in a Param.
    """

    r_max: float
    r_min: float
    d_c: float
    r_c: float
    r_rot_exact: float
    r_rot: float


def footprint_radii(fp: FootprintSet) -> FootprintRadii:
    """R_max, r_min, d_c, R_c and R_rot of ``fp`` (see :class:`FootprintRadii` and the module documentation)."""
    fp = _footprint_set(fp)
    positions = support_positions(fp)
    r_max = max(pos.r for pos in positions)
    r_min = min(pos.r for pos in positions)
    t = theta(0)  # the radii do not depend on the heading
    offsets = [support_offset(pos.r, pos.delta, t) for pos in positions]
    best = (-1.0, 0.0, 0.0)  # (d_c + R_c, d_c, R_c)
    for centre in centroid_offsets(fp, 0):
        d_c = _length(centre)
        r_c = max(_length((d[0] - centre[0], d[1] - centre[1])) for d in offsets)
        if d_c + r_c > best[0]:
            best = (d_c + r_c, d_c, r_c)
    exact = max(r_max, best[0])
    return FootprintRadii(r_max=r_max, r_min=r_min, d_c=best[1], r_c=best[2], r_rot_exact=exact,
                          r_rot=float(math.ceil(exact + _ROUND_UP_GUARD_MM)))


def rotation_radius(fp: FootprintSet) -> float:
    """R_rot = max(R_max, d_c + R_c), rounded up to whole millimetres: 404 mm with the spec footprint.

    It holds every support during a turn about any pivot on the segment from the
    tool point to the support centroid, provided the robot turns about such a
    point and reaches the commanded XY (safety-model 4.6; red-team PHYS-07).
    """
    return footprint_radii(fp).r_rot


# ---------------------------------------------------------------------------
# Body frame


def body_point(back: float, right: float) -> Vec2:
    """A point given in the body frame (``back`` mm behind T, ``right`` mm right of the body axis; machine.toml).

    Returned in the kernel's local frame: the offset from the tool point at
    ``C = 0`` (theta = 90 deg, the body facing +Y, its right along +X), so
    ``(right, -back)``. :func:`goliath_prep.geom.sweeps.body_hull` turns local
    shapes to a pose.
    """
    return (_real(right, "right") + 0.0, -_real(back, "back") + 0.0)
