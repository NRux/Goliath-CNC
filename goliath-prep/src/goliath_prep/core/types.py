# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The interface types shared by the parts of goliath-prep (unit F05; red-team C-06).

Types used by units that are built in parallel have one owner, here, so their
stubs cannot diverge. Each is a frozen value with validation and a JSON round
trip, and nothing else: no geometry, no decisions.

* :class:`Pose` - a tool point and heading with its source;
* :data:`RotationModel` - how the robot turns on a ``C`` change (U-21);
* :class:`FootprintSet` - the vendor footprint, the measured one and which is active;
* :class:`EmitProfile` - the line formatting choices of the emitter (U-31);
* :class:`GateView` - the read-only view of the feature gates a check runs
  under, and :class:`ClosedGateView`, the all-closed desk view;
* :class:`MeasuredGeometry` - the panel measured on the robot, with the raw
  ``INFO;25`` and ``INFO;27`` lines kept verbatim and a content hash;
* :class:`Envelope` - the limits within which a desk result carries over;
* :class:`SessionBinding` - what a final certificate binds to;
* :class:`Obstacle`, :class:`Fixation`, :class:`PriorCut` - job inputs on the panel;
* :class:`CheckContext` - everything a check reads, every number with its provenance;
* :class:`RungExemption` and :class:`ExemptLine` - the D-CSTEP exceptions of a
  rung program, constructible only in ``goliath_prep.rungs``.

**Validation.** Wrong types raise ``TypeError``, wrong values ``ValueError``,
with the field named. Lists are copied into tuples and mappings into
:class:`FrozenMapping`, so a value cannot change after it was checked.
Coordinates and lengths are millimetres, angles degrees.

**JSON.** ``to_json()`` returns plain JSON data for
:func:`goliath_prep.core.canon.canonical_json`; ``from_json()`` accepts exactly
the keys ``to_json()`` writes. Canonical JSON writes floats to 0.001, so, as
for :class:`~goliath_prep.core.params.Param`, ``to_json()`` refuses a float
the rounding would change: a record never shows another value than the one in
use. Quantize explicitly (:func:`goliath_prep.core.canon.quantize`, in the
direction that is conservative for the use), or keep the exact text beside it
(the raw ``INFO;27`` line keeps the robot's four decimals).

**Provenance in a CheckContext.** Every number and choice a check reads from a
:class:`CheckContext` is a :class:`~goliath_prep.core.params.Param` in
``CheckContext.params``, or carries its own source:

* ``rho``, ``r_rot``, ``rotation_model`` and ``heading_map_verified`` equal the
  Param of the same name (:data:`CONTEXT_SCALAR_PARAMS`); ``rho`` and ``r_rot``
  are ``DERIVED``, as the catalogue makes them, so they carry the disclosures
  of their inputs (an unmeasured wheel radius);
* each vendor support ``(r_k, delta_k)`` equals the ``SPEC`` Params
  ``support_rk`` and ``support_deltak`` of the parameter catalogue
  (:data:`VENDOR_SUPPORT_PARAMS`), and each measured one the Params
  ``support_rk_measured`` and ``support_deltak_measured``
  (:data:`MEASURED_SUPPORT_PARAMS`) from a measurement;
* the gates are identified by ``snapshot_sha256()``; a start pose by
  ``Pose.source``; a measured geometry by its raw lines and hash; obstacles,
  fixations and prior cuts by ``ref``.

A context cannot claim what the gate view does not open: ``heading_map_verified``
needs ``gates.allowed("heading_map_verified") is True``, and a rotation model
other than ``unverified`` needs the ``rotation_model`` gate open with that
value, each with a ``FACT`` Param. No unknown is read as "yes".
"""
from __future__ import annotations

import math
import numbers
import re
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fractions import Fraction
from types import MappingProxyType
from typing import Any, ClassVar, Literal, Protocol, TypeVar, get_args, runtime_checkable

from goliath_prep.core.canon import canonical_sha256, quantize
from goliath_prep.core.disclosure import Disclosure
from goliath_prep.core.params import Param

__all__ = [
    "CONTEXT_SCALAR_PARAMS",
    "EMIT_FIRST_LINES",
    "EMIT_ROTATION_STYLES",
    "EMIT_Z_LINE_STYLES",
    "EXEMPTION_RUNGS",
    "EXEMPT_RULES",
    "MEASURED_SUPPORT_PARAMS",
    "ROTATION_MODELS",
    "TOWER2_AXES",
    "VENDOR_SUPPORT_PARAMS",
    "CheckContext",
    "ClosedGateView",
    "EmitFirstLine",
    "EmitProfile",
    "EmitRotationStyle",
    "EmitZLineStyle",
    "Envelope",
    "ExemptLine",
    "ExemptRule",
    "ExemptionRung",
    "Fixation",
    "FootprintSet",
    "FrozenMapping",
    "GateView",
    "MeasuredGeometry",
    "Obstacle",
    "Pose",
    "PriorCut",
    "RotationModel",
    "RungExemption",
    "RungExemptionOriginError",
    "SessionBinding",
    "Tower2",
]

K = TypeVar("K")
V = TypeVar("V")

RotationModel = Literal["unverified", "in-place-shortest", "in-place-numeric", "during-translation", "about-centroid"]
"""How the robot turns on a ``C`` change (U-21). ``unverified`` until rung R3
records the fact ``rotation.model``: every rotation step is then checked as the
full disc of radius ``R_rot + rho`` (safety-model 9.3)."""
ROTATION_MODELS: tuple[str, ...] = get_args(RotationModel)

EmitZLineStyle = Literal["bare", "g00"]
"""The rapid Z line after a ``G00``: ``Z<z>`` alone (the vendor's form) or ``G00 Z<z>``."""
EmitRotationStyle = Literal["split", "combined"]
"""``split``: rotate in place, then translate; ``combined``: ``G00 X Y C`` on one line."""
EmitFirstLine = Literal["combined", "translate-then-rotate"]
"""The program's first line: one combined line, or a translation at the parked
heading followed by a rotation in place (only once the heading map and the park
heading are facts)."""
EMIT_Z_LINE_STYLES: tuple[str, ...] = get_args(EmitZLineStyle)
EMIT_ROTATION_STYLES: tuple[str, ...] = get_args(EmitRotationStyle)
EMIT_FIRST_LINES: tuple[str, ...] = get_args(EmitFirstLine)

Tower2 = Literal["X", "Y"]
"""The axis tower 2 stands on: ``X`` = corner (W, 0), ``SET T2 POSITION;1``;
``Y`` = corner (0, H), ``SET T2 POSITION;0`` (safety-model 5.1)."""
TOWER2_AXES: tuple[str, ...] = get_args(Tower2)

ExemptionRung = Literal["R3", "R3b", "R4"]
"""The rung programs that may carry an exemption (safety-model section 14)."""
EXEMPTION_RUNGS: tuple[str, ...] = get_args(ExemptionRung)

ExemptRule = Literal["D-CSTEP:step-180", "D-CSTEP:xy-and-c"]
"""The D-CSTEP rules a rung program may be exempt from, on named lines only."""
EXEMPT_RULES: Mapping[str, str] = MappingProxyType(
    {
        "D-CSTEP:step-180": "a numeric |dC| of 180 or more on one line: the rung's deliberate single wrap step",
        "D-CSTEP:xy-and-c": "X or Y and C on one line: the rung's combined rotate-and-translate line",
    }
)
if tuple(EXEMPT_RULES) != get_args(ExemptRule):  # pragma: no cover - a guard on this module's own constants
    raise RuntimeError("EXEMPT_RULES and ExemptRule disagree")

VENDOR_SUPPORT_PARAMS: tuple[tuple[str, str], ...] = (
    ("support_r1", "support_delta1"),
    ("support_r2", "support_delta2"),
    ("support_r3", "support_delta3"),
)
"""The context Params of the vendor supports q1 (rear), q2 (right), q3 (left):
the parameter catalogue's names (docs/prep/params.json); source ``SPEC``."""
MEASURED_SUPPORT_PARAMS: tuple[tuple[str, str], ...] = tuple(
    (r + "_measured", delta + "_measured") for r, delta in VENDOR_SUPPORT_PARAMS
)
"""The context Params of the measured supports, aligned with the vendor ones."""
CONTEXT_SCALAR_PARAMS: tuple[str, ...] = ("rho", "r_rot", "rotation_model", "heading_map_verified")
"""CheckContext fields whose value equals the Param of the same name in ``params``."""

_MEASURED_SOURCES = frozenset({"ROBOT_MEASURED", "OWNER_PROFILE", "DERIVED"})
_GATED_SOURCES = frozenset({"GATE", "FACT", "POLICY"})
_HEADING_VALUES = {True: "verified", False: "unverified"}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FIRMWARE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_WKT_POLYGON = re.compile(r"POLYGON ?\(\(.+\)\)\Z", re.DOTALL)
_C_GRID = frozenset(range(0, 360, 10))
_NOISE = 1e-9
_RUNGS_MODULE = "goliath_prep.rungs"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (numbers.Integral, float)):
        raise TypeError("%s must be a number (int or float), got %r" % (what, value))
    try:
        out = float(value)
    except OverflowError:
        raise ValueError("%s = %r is too large for a float" % (what, value)) from None
    if not math.isfinite(out):
        raise ValueError("%s must be finite, got %r" % (what, value))
    return out


def _positive(value: object, what: str) -> float:
    out = _number(value, what)
    if out <= 0:
        raise ValueError("%s must be greater than 0, got %r" % (what, value))
    return out


def _integer(value: object, what: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise TypeError("%s must be an int, got %r" % (what, value))
    if value < minimum:
        raise ValueError("%s must be at least %d, got %r" % (what, minimum, value))
    return int(value)


def _text(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise TypeError("%s must be a string, got %r" % (what, value))
    if not value.strip():
        raise ValueError("%s must not be empty" % what)
    return str.__str__(value)


def _choice(value: object, choices: tuple[str, ...], what: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError("%s must be one of %s, got %r" % (what, ", ".join(choices), value))
    return str.__str__(value)


def _sha256(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise TypeError("%s must be a hex SHA-256 string, got %r" % (what, value))
    if not _SHA256.match(value):
        raise ValueError("%s must be 64 lowercase hex digits, got %r" % (what, value))
    return str.__str__(value)


def _seq(value: object, what: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, (list, tuple)):
        raise TypeError("%s must be a tuple (or a list), got %r" % (what, value))
    return tuple(value)


def _tuple_of(value: object, kind: type, what: str) -> tuple[Any, ...]:
    items = _seq(value, what)
    for item in items:
        if not isinstance(item, kind):
            raise TypeError("%s must hold %s objects, got %r" % (what, kind.__name__, item))
    return items


def _point(value: object, what: str) -> tuple[float, float]:
    items = _seq(value, what)
    if len(items) != 2:
        raise ValueError("%s must be a point (x, y), got %r" % (what, value))
    return (_number(items[0], what + " x"), _number(items[1], what + " y"))


def _polygon(value: object, what: str) -> tuple[tuple[float, float], ...]:
    """At least three vertices, no repeated neighbour (so no closing vertex), simple, area above zero.

    Simple: no two edges meet except neighbours at their shared vertex, and no
    edge runs back along the one before it. The region of a ring that crosses or
    touches itself is ambiguous (geometry libraries resolve it differently), so a
    keep-out or a removal could be under-counted; such a polygon is refused.
    The test is exact (rational arithmetic on the given floats).
    """
    try:
        points = tuple(_point(p, "%s vertex %d" % (what, i)) for i, p in enumerate(_seq(value, what)))
    except ValueError as exc:
        raise ValueError("%s: %s" % (what, exc)) from None
    if len(points) < 3:
        raise ValueError("%s needs at least 3 vertices, got %d" % (what, len(points)))
    for i, p in enumerate(points):
        if p == points[i - 1]:
            raise ValueError(
                "%s repeats vertex %r (list each vertex once; the ring closes itself)" % (what, p)
            )
    contact = _self_contact(points)
    if contact is not None:
        raise ValueError(
            "%s is not simple: %s. Its region is ambiguous; declare it as one outline that does not cross or "
            "touch itself (split it into several entries if needed)" % (what, contact)
        )
    twice_area = sum(
        points[i - 1][0] * points[i][1] - points[i][0] * points[i - 1][1] for i in range(len(points))
    )
    if twice_area == 0:
        raise ValueError("%s has no area" % what)
    return points


def _exact(p: tuple[float, float]) -> tuple[Fraction, Fraction]:
    return (Fraction(p[0]), Fraction(p[1]))


def _orientation(a: tuple[Fraction, Fraction], b: tuple[Fraction, Fraction], c: tuple[Fraction, Fraction]) -> int:
    """The sign of the cross product (b - a) x (c - a): 1 left turn, -1 right turn, 0 collinear (exact)."""
    det = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return (det > 0) - (det < 0)


def _on_segment(a: tuple[Fraction, Fraction], b: tuple[Fraction, Fraction], p: tuple[Fraction, Fraction]) -> bool:
    """Whether ``p``, collinear with a-b, lies on the closed segment a-b."""
    return min(a[0], b[0]) <= p[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])


def _segments_meet(
    p1: tuple[Fraction, Fraction], p2: tuple[Fraction, Fraction], p3: tuple[Fraction, Fraction],
    p4: tuple[Fraction, Fraction],
) -> bool:
    """Whether the closed segments p1-p2 and p3-p4 have a point in common (exact)."""
    d1, d2 = _orientation(p3, p4, p1), _orientation(p3, p4, p2)
    d3, d4 = _orientation(p1, p2, p3), _orientation(p1, p2, p4)
    if d1 * d2 < 0 and d3 * d4 < 0:
        return True
    return (
        (d1 == 0 and _on_segment(p3, p4, p1))
        or (d2 == 0 and _on_segment(p3, p4, p2))
        or (d3 == 0 and _on_segment(p1, p2, p3))
        or (d4 == 0 and _on_segment(p1, p2, p4))
    )


def _self_contact(points: tuple[tuple[float, float], ...]) -> str | None:
    """Where the closed ring through ``points`` crosses, touches or folds back on itself, or None if it is simple.

    Edge k runs from vertex k to vertex k+1 (the last back to vertex 0).
    Neighbouring edges share their vertex and must not overlap beyond it;
    other edges must not meet at all.
    """
    n = len(points)
    exact = [_exact(p) for p in points]
    for k in range(n):
        a, b, c = exact[k - 1], exact[k], exact[(k + 1) % n]
        backwards = (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1]) < 0
        if _orientation(a, b, c) == 0 and backwards:
            return "at vertex %d %r the ring runs back along the edge it arrived on" % (k, points[k])
    boxes = [
        (min(points[k][0], points[(k + 1) % n][0]), max(points[k][0], points[(k + 1) % n][0]),
         min(points[k][1], points[(k + 1) % n][1]), max(points[k][1], points[(k + 1) % n][1]))
        for k in range(n)
    ]
    for i in range(n):
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue  # neighbours through the closing vertex
            bi, bj = boxes[i], boxes[j]
            if bi[1] < bj[0] or bj[1] < bi[0] or bi[3] < bj[2] or bj[3] < bi[2]:
                continue
            if _segments_meet(exact[i], exact[(i + 1) % n], exact[j], exact[(j + 1) % n]):
                return "edge %d (%r to %r) meets edge %d (%r to %r)" % (
                    i, points[i], points[(i + 1) % n], j, points[j], points[(j + 1) % n])
    return None


def _on_grid(value: float, what: str) -> float:
    """``value`` if canonical JSON writes it as itself (to 0.001, within float noise)."""
    if abs(value - quantize(value)) > max(_NOISE, 8 * math.ulp(value)):
        raise ValueError(
            "%s = %r is not on the canonical 0.001 grid, so canonical JSON would show another value than the one "
            "in use. Quantize it explicitly (goliath_prep.core.canon.quantize, in the direction that is "
            "conservative for its use)" % (what, value)
        )
    return value


def _keys(data: object, expected: frozenset[str], what: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping) or set(data) != expected:
        raise ValueError("%s JSON needs exactly the keys %s, got %r" % (what, sorted(expected), data))
    return data


def _utc_timestamp(value: object, what: str) -> str:
    text = _text(value, what)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("%s %r is not an ISO-8601 timestamp" % (what, text)) from None
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ValueError("%s %r must be in UTC (end with Z or +00:00)" % (what, text))
    return text


def _info_line(value: object, code: int, what: str) -> str:
    """One verbatim ``INFO;<code>`` line, as the link received it (without its line end)."""
    text = _text(value, what)
    if not re.match(r"INFO;%d(;|\Z)" % code, text):
        raise ValueError("%s must be an INFO;%d line, got %r" % (what, code, text))
    if "\n" in text or "\r" in text:
        raise ValueError("%s must be one line without a line end, got %r" % (what, text))
    return text


# ---------------------------------------------------------------------------
# FrozenMapping
# ---------------------------------------------------------------------------


class FrozenMapping(Mapping[K, V]):
    """A read-only, hashable mapping, iterated in sorted key order.

    Built from a mapping or from (key, value) pairs; a key given twice is an
    error, never a silent overwrite.
    """

    __slots__ = ("_data",)

    def __init__(self, items: Mapping[K, V] | Iterable[tuple[K, V]] = ()) -> None:
        pairs = list(items.items()) if isinstance(items, Mapping) else list(items)
        data: dict[K, V] = {}
        for key, value in pairs:
            if key in data:
                raise TypeError("FrozenMapping: key %r given twice" % (key,))
            data[key] = value
        object.__setattr__(self, "_data", dict(sorted(data.items(), key=lambda kv: kv[0])))  # type: ignore[arg-type]

    def __getitem__(self, key: K) -> V:
        return self._data[key]

    def __iter__(self) -> Iterator[K]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __hash__(self) -> int:
        return hash(frozenset(self._data.items()))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("FrozenMapping is read-only")

    def __reduce__(self) -> tuple[type[FrozenMapping[K, V]], tuple[tuple[tuple[K, V], ...]]]:
        return (type(self), (tuple(self._data.items()),))

    def __repr__(self) -> str:
        return "FrozenMapping(%r)" % (self._data,)


# ---------------------------------------------------------------------------
# Pose, rotation, footprint, emit profile
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pose:
    """A tool point (x, y) in the panel frame, its heading ``c`` and where it came from.

    ``c`` is the C word, on the 10-degree grid 0 to 350, or None when the
    heading is not known. ``source`` names the origin: in-session position
    frames, the park-pose record of rung R2 (fact ``detection.park_pose``), a
    line of the program, or a placeholder for a desk rehearsal; checks judge a
    pose by it.
    """

    x: float
    y: float
    c: int | None
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _number(self.x, "Pose x"))
        object.__setattr__(self, "y", _number(self.y, "Pose y"))
        if self.c is not None:
            if isinstance(self.c, bool) or not isinstance(self.c, numbers.Integral):
                raise TypeError("Pose c must be an int on the C grid or None, got %r" % (self.c,))
            if self.c not in _C_GRID:
                raise ValueError("Pose c must be on the 10-degree C grid 0..350, got %r" % (self.c,))
            object.__setattr__(self, "c", int(self.c))
        object.__setattr__(self, "source", _text(self.source, "Pose source"))

    def to_json(self) -> dict[str, Any]:
        return {"x": _on_grid(self.x, "Pose x"), "y": _on_grid(self.y, "Pose y"), "c": self.c, "source": self.source}

    @classmethod
    def from_json(cls, data: object) -> Pose:
        d = _keys(data, frozenset({"x", "y", "c", "source"}), "Pose")
        return cls(d["x"], d["y"], d["c"], d["source"])


def _support(value: object, what: str) -> tuple[float, float]:
    items = _seq(value, what)
    if len(items) != 2:
        raise ValueError("%s must be (r_mm, delta_deg), got %r" % (what, value))
    r = _positive(items[0], what + " r")
    delta = _number(items[1], what + " delta")
    if not -180.0 <= delta <= 180.0:
        raise ValueError("%s delta must be within -180..180 degrees, got %r" % (what, delta))
    return (r, delta)


@dataclass(frozen=True)
class FootprintSet:
    """The support positions as (r_k mm, delta_k degrees), for q1 (rear), q2 (right), q3 (left).

    ``vendor`` is the spec footprint (safety-model 4.2). ``measured`` holds the
    positions measured at rung R0b, aligned with ``vendor``; an entry is None
    for a support whose distance or angle reads ``unknown`` (it adds no
    measured position), and ``measured`` is None when nothing is measured.
    ``active`` is ``vendor`` before R0b and ``union`` once any position is
    measured: a measurement adds positions and never replaces the spec ones
    (safety-model 4.3).
    """

    vendor: tuple[tuple[float, float], ...]
    measured: tuple[tuple[float, float] | None, ...] | None
    active: Literal["vendor", "union"]

    def __post_init__(self) -> None:
        vendor = _seq(self.vendor, "FootprintSet vendor")
        if len(vendor) != 3:
            raise ValueError("FootprintSet vendor needs three supports (q1, q2, q3), got %d" % len(vendor))
        object.__setattr__(
            self, "vendor", tuple(_support(s, "FootprintSet vendor q%d" % (k + 1)) for k, s in enumerate(vendor))
        )
        if self.measured is not None:
            measured = _seq(self.measured, "FootprintSet measured")
            if len(measured) != 3:
                raise ValueError(
                    "FootprintSet measured needs three entries aligned with q1, q2, q3 (None for a support not "
                    "measured), got %d" % len(measured)
                )
            measured = tuple(
                None if s is None else _support(s, "FootprintSet measured q%d" % (k + 1))
                for k, s in enumerate(measured)
            )
            if all(s is None for s in measured):
                raise ValueError("FootprintSet measured has no measured support: use None when nothing is measured")
            object.__setattr__(self, "measured", measured)
        active = _choice(self.active, ("vendor", "union"), "FootprintSet active")
        if self.measured is None and active != "vendor":
            raise ValueError("FootprintSet active is 'union' but nothing is measured: the union needs a measurement")
        if self.measured is not None and active != "union":
            raise ValueError("FootprintSet has measured supports, so the union is active (a measurement is never "
                             "ignored), got %r" % active)

    def to_json(self) -> dict[str, Any]:
        def pair(s: tuple[float, float], what: str) -> list[float]:
            return [_on_grid(s[0], what + " r"), _on_grid(s[1], what + " delta")]

        return {
            "vendor": [pair(s, "FootprintSet vendor q%d" % (k + 1)) for k, s in enumerate(self.vendor)],
            "measured": None
            if self.measured is None
            else [None if s is None else pair(s, "FootprintSet measured q%d" % (k + 1))
                  for k, s in enumerate(self.measured)],
            "active": self.active,
        }

    @classmethod
    def from_json(cls, data: object) -> FootprintSet:
        d = _keys(data, frozenset({"vendor", "measured", "active"}), "FootprintSet")
        return cls(d["vendor"], d["measured"], d["active"])


@dataclass(frozen=True)
class EmitProfile:
    """The emitter's line formatting choices (U-31; the project design, sections 4.10.1 and 4.12).

    No field has a default: the context builder chooses each from the facts and
    records the basis.
    """

    z_line_style: EmitZLineStyle
    rotation_style: EmitRotationStyle
    first_line: EmitFirstLine

    def __post_init__(self) -> None:
        _choice(self.z_line_style, EMIT_Z_LINE_STYLES, "EmitProfile z_line_style")
        _choice(self.rotation_style, EMIT_ROTATION_STYLES, "EmitProfile rotation_style")
        _choice(self.first_line, EMIT_FIRST_LINES, "EmitProfile first_line")

    def to_json(self) -> dict[str, Any]:
        return {"z_line_style": self.z_line_style, "rotation_style": self.rotation_style,
                "first_line": self.first_line}

    @classmethod
    def from_json(cls, data: object) -> EmitProfile:
        d = _keys(data, frozenset({"z_line_style", "rotation_style", "first_line"}), "EmitProfile")
        return cls(d["z_line_style"], d["rotation_style"], d["first_line"])


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


@runtime_checkable
class GateView(Protocol):
    """The feature gates a check runs under, read-only (safety-model 1.4; the project design, section 3).

    goliath-prep reads gates only through this protocol and never imports
    goliath-run; goliath-run's gate set provides one, and so does a gate
    snapshot file (``--gates``). Without one, prep uses :class:`ClosedGateView`.

    * ``allowed(name)``: ``True`` only when the gate is on for this robot and
      firmware; ``False`` when it is off; ``None`` when it is uncertain. Only
      ``True`` permits: callers test ``is True``. An unknown name is never True.
    * ``value(name)``: the value an open gate carries (the rotation model, such
      as ``in-place-shortest``; a verified program size), or None when the gate
      has no value in this view. None means the gate's closed value from the
      parameter catalogue applies, with its disclosure.
    * ``snapshot_sha256()``: the hex SHA-256 of the gate and fact snapshot this
      view reads; provisional certificates bind it.
    """

    def allowed(self, name: str) -> bool | None: ...

    def value(self, name: str) -> object: ...

    def snapshot_sha256(self) -> str: ...


def _gate_name(name: object) -> str:
    return _text(name, "gate name")


@dataclass(frozen=True)
class ClosedGateView:
    """The all-closed desk view: every gate off, no value, no robot fact.

    goliath-prep uses it when no gate snapshot is given (the project design,
    section 4.15.1) and the GI conformance examples run under it. It answers
    False for every name, including names it does not know, and None for every
    value. Whoever falls back to it raises :attr:`DISCLOSURE`. Its snapshot
    hash is the SHA-256 of :attr:`SNAPSHOT`, which no robot's snapshot shares.
    """

    SNAPSHOT: ClassVar[Mapping[str, object]] = MappingProxyType(
        {"view": "goliath-prep desk view", "gates": "all closed", "facts": "none", "version": 1}
    )
    DISCLOSURE: ClassVar[Disclosure] = Disclosure(
        id="DESK_NO_ROBOT_FACTS",
        severity="amber",
        text="DESK, NO ROBOT FACTS: no gate snapshot was given, so every feature gate is closed and the result "
        "can only be provisional; every G-GATE refusal names the rung that unlocks it",
        source="goliath-prep desk gate view (no --gates snapshot)",
    )

    def allowed(self, name: str) -> bool | None:
        _gate_name(name)
        return False

    def value(self, name: str) -> object:
        _gate_name(name)
        return None

    def snapshot_sha256(self) -> str:
        return canonical_sha256(self.SNAPSHOT)


# ---------------------------------------------------------------------------
# Measured geometry, envelope, session
# ---------------------------------------------------------------------------

_MEASURED_KEYS = frozenset(
    {"raw_info25", "raw_info27", "hypothesis", "polygon_wkt", "set_panel_size", "tower2", "tower_distance",
     "park_pose"}
)


def _measured_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """The validated, normalised content fields of a MeasuredGeometry (everything but its hash)."""
    raw25 = _seq(fields["raw_info25"], "MeasuredGeometry raw_info25")
    if not raw25:
        raise ValueError("MeasuredGeometry raw_info25 must hold the INFO;25 lines received; it is empty")
    raw27 = fields["raw_info27"]
    size = _seq(fields["set_panel_size"], "MeasuredGeometry set_panel_size")
    if len(size) != 2:
        raise ValueError("MeasuredGeometry set_panel_size must be two ints (x, y), got %r" % (size,))
    wkt = _text(fields["polygon_wkt"], "MeasuredGeometry polygon_wkt")
    if not _WKT_POLYGON.match(wkt):
        raise ValueError("MeasuredGeometry polygon_wkt must be a non-empty WKT POLYGON, got %r" % (wkt,))
    distance = fields["tower_distance"]
    if distance is not None:
        distance = _positive(distance, "MeasuredGeometry tower_distance")
        if raw27 is None:
            raise ValueError("MeasuredGeometry tower_distance needs the raw_info27 line it was read from")
    pose = fields["park_pose"]
    if pose is not None and not isinstance(pose, Pose):
        raise TypeError("MeasuredGeometry park_pose must be a Pose or None, got %r" % (pose,))
    return {
        "raw_info25": tuple(
            _info_line(line, 25, "MeasuredGeometry raw_info25[%d]" % i) for i, line in enumerate(raw25)
        ),
        "raw_info27": None if raw27 is None else _info_line(raw27, 27, "MeasuredGeometry raw_info27"),
        "hypothesis": _text(fields["hypothesis"], "MeasuredGeometry hypothesis"),
        "polygon_wkt": wkt,
        "set_panel_size": (_integer(size[0], "MeasuredGeometry set_panel_size x", 1),
                           _integer(size[1], "MeasuredGeometry set_panel_size y", 1)),
        "tower2": _choice(fields["tower2"], TOWER2_AXES, "MeasuredGeometry tower2"),
        "tower_distance": distance,
        "park_pose": pose,
    }


def _measured_content_json(fields: Mapping[str, Any]) -> dict[str, Any]:
    distance = fields["tower_distance"]
    pose = fields["park_pose"]
    return {
        "raw_info25": list(fields["raw_info25"]),
        "raw_info27": fields["raw_info27"],
        "hypothesis": fields["hypothesis"],
        "polygon_wkt": fields["polygon_wkt"],
        "set_panel_size": list(fields["set_panel_size"]),
        "tower2": fields["tower2"],
        "tower_distance": None if distance is None else _on_grid(distance, "MeasuredGeometry tower_distance"),
        "park_pose": None if pose is None else pose.to_json(),
    }


@dataclass(frozen=True)
class MeasuredGeometry:
    """The panel as measured on the robot after panel detection (safety-model section 12).

    * ``raw_info25``: every ``INFO;25`` line received, verbatim and in order;
      ``raw_info27``: the ``INFO;27`` line, verbatim, or None if none arrived.
      Decoding them is not this type's job: they are the evidence.
    * ``hypothesis``: the one decoding hypothesis that passed (encoding and axis
      map); ``polygon_wkt``: the decoded panel ``P_meas`` as a WKT POLYGON.
    * ``set_panel_size``: the integers sent in ``SET PANEL SIZE``.
    * ``tower2``: the tower-2 axis; ``tower_distance``: the distance read from
      ``raw_info27`` in mm, on the 0.001 grid (the line keeps the robot's
      decimals), or None.
    * ``park_pose``: where the robot parked after detection, if known.
    * ``sha256``: the SHA-256 of the canonical JSON of all the fields above
      (:meth:`content_json`). It is checked on construction, so a record whose
      content was changed is refused. Use :meth:`build` to compute it.
    """

    raw_info25: tuple[str, ...]
    raw_info27: str | None
    hypothesis: str
    polygon_wkt: str
    set_panel_size: tuple[int, int]
    tower2: Tower2
    tower_distance: float | None
    park_pose: Pose | None
    sha256: str

    def __post_init__(self) -> None:
        normal = _measured_fields({name: getattr(self, name) for name in _MEASURED_KEYS})
        for name, value in normal.items():
            object.__setattr__(self, name, value)
        sha = _sha256(self.sha256, "MeasuredGeometry sha256")
        expected = canonical_sha256(_measured_content_json(normal))
        if sha != expected:
            raise ValueError(
                "MeasuredGeometry sha256 %s does not match its content (%s): the record was changed, or the hash "
                "was not computed with MeasuredGeometry.build" % (sha, expected)
            )

    @classmethod
    def build(
        cls,
        *,
        raw_info25: tuple[str, ...],
        raw_info27: str | None,
        hypothesis: str,
        polygon_wkt: str,
        set_panel_size: tuple[int, int],
        tower2: Tower2,
        tower_distance: float | None,
        park_pose: Pose | None,
    ) -> MeasuredGeometry:
        """A MeasuredGeometry with its ``sha256`` computed from the other fields."""
        fields = {"raw_info25": raw_info25, "raw_info27": raw_info27, "hypothesis": hypothesis,
                  "polygon_wkt": polygon_wkt, "set_panel_size": set_panel_size, "tower2": tower2,
                  "tower_distance": tower_distance, "park_pose": park_pose}
        sha = canonical_sha256(_measured_content_json(_measured_fields(fields)))
        return cls(sha256=sha, **fields)

    def content_json(self) -> dict[str, Any]:
        """Every field but ``sha256``, as JSON data: what ``sha256`` is the canonical hash of."""
        return _measured_content_json({name: getattr(self, name) for name in _MEASURED_KEYS})

    def to_json(self) -> dict[str, Any]:
        return {**self.content_json(), "sha256": self.sha256}

    @classmethod
    def from_json(cls, data: object) -> MeasuredGeometry:
        d = _keys(data, _MEASURED_KEYS | {"sha256"}, "MeasuredGeometry")
        pose = d["park_pose"]
        return cls(
            raw_info25=d["raw_info25"],
            raw_info27=d["raw_info27"],
            hypothesis=d["hypothesis"],
            polygon_wkt=d["polygon_wkt"],
            set_panel_size=d["set_panel_size"],
            tower2=d["tower2"],
            tower_distance=d["tower_distance"],
            park_pose=None if pose is None else Pose.from_json(pose),
            sha256=d["sha256"],
        )


@dataclass(frozen=True)
class Envelope:
    """The limits a measured panel must meet for a desk result to carry over, with the reasons shown.

    ``limits`` maps a limit's name to its value (mm unless its name says
    otherwise). An envelope without any limit would read as "anything carries
    over", so it is refused.
    """

    limits: Mapping[str, float]
    messages: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.limits, Mapping):
            raise TypeError("Envelope limits must be a mapping of name to number, got %r" % (self.limits,))
        if not self.limits:
            raise ValueError("Envelope limits is empty: an envelope without a limit would accept any panel")
        limits = FrozenMapping(
            (_text(name, "Envelope limits name"), _number(value, "Envelope limits[%r]" % (name,)))
            for name, value in self.limits.items()
        )
        object.__setattr__(self, "limits", limits)
        messages = _seq(self.messages, "Envelope messages")
        object.__setattr__(self, "messages", tuple(_text(m, "Envelope messages") for m in messages))

    def to_json(self) -> dict[str, Any]:
        return {
            "limits": {name: _on_grid(value, "Envelope limits[%r]" % (name,)) for name, value in self.limits.items()},
            "messages": list(self.messages),
        }

    @classmethod
    def from_json(cls, data: object) -> Envelope:
        d = _keys(data, frozenset({"limits", "messages"}), "Envelope")
        return cls(d["limits"], d["messages"])


_SESSION_KEYS = ("session_id", "serial", "firmware", "panel_sha256", "facts_sha256", "profile_sha256",
                 "material_sha256", "detection_done_at")


@dataclass(frozen=True)
class SessionBinding:
    """What a final certificate binds to: this session, robot, firmware, measured panel, facts, profile, material.

    ``serial`` is the robot's serial number: private, kept local and redacted on
    export. ``firmware`` is a known version (``01.02.00``). The four hashes are
    hex SHA-256 values; ``detection_done_at`` is an ISO-8601 UTC timestamp.
    """

    session_id: str
    serial: str
    firmware: str
    panel_sha256: str
    facts_sha256: str
    profile_sha256: str
    material_sha256: str
    detection_done_at: str

    def __post_init__(self) -> None:
        _text(self.session_id, "SessionBinding session_id")
        _text(self.serial, "SessionBinding serial")
        firmware = _text(self.firmware, "SessionBinding firmware")
        if not _FIRMWARE.match(firmware):
            raise ValueError("SessionBinding firmware must be a known version such as 01.02.00, got %r" % firmware)
        for name in ("panel_sha256", "facts_sha256", "profile_sha256", "material_sha256"):
            _sha256(getattr(self, name), "SessionBinding %s" % name)
        _utc_timestamp(self.detection_done_at, "SessionBinding detection_done_at")

    def to_json(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in _SESSION_KEYS}

    @classmethod
    def from_json(cls, data: object) -> SessionBinding:
        d = _keys(data, frozenset(_SESSION_KEYS), "SessionBinding")
        return cls(**{name: d[name] for name in _SESSION_KEYS})


# ---------------------------------------------------------------------------
# Job inputs on the panel
# ---------------------------------------------------------------------------


def _polygon_json(points: tuple[tuple[float, float], ...], what: str) -> list[list[float]]:
    return [[_on_grid(x, what + " x"), _on_grid(y, what + " y")] for x, y in points]


@dataclass(frozen=True)
class Obstacle:
    """Something on the panel the wheels, body and tool must avoid (safety-model 6.5).

    ``polygon``: its footprint, a simple polygon, vertices in order, each once; ``height_mm``
    above the stock top; ``kind``: what it is ("clamp", "screw head");
    ``ref``: where it was declared (``job.toml obstacles[0]``).
    """

    polygon: tuple[tuple[float, float], ...]
    height_mm: float
    kind: str
    ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "polygon", _polygon(self.polygon, "Obstacle polygon"))
        object.__setattr__(self, "height_mm", _positive(self.height_mm, "Obstacle height_mm"))
        _text(self.kind, "Obstacle kind")
        _text(self.ref, "Obstacle ref")

    def to_json(self) -> dict[str, Any]:
        return {"polygon": _polygon_json(self.polygon, "Obstacle polygon"),
                "height_mm": _on_grid(self.height_mm, "Obstacle height_mm"), "kind": self.kind, "ref": self.ref}

    @classmethod
    def from_json(cls, data: object) -> Obstacle:
        d = _keys(data, frozenset({"polygon", "height_mm", "kind", "ref"}), "Obstacle")
        return cls(d["polygon"], d["height_mm"], d["kind"], d["ref"])


@dataclass(frozen=True)
class Fixation:
    """A fixing point (screw, clamp) holding the panel: a keep-out and an anchor for R-FIXED (safety-model 6.5)."""

    point: tuple[float, float]
    height_mm: float
    ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "point", _point(self.point, "Fixation point"))
        object.__setattr__(self, "height_mm", _positive(self.height_mm, "Fixation height_mm"))
        _text(self.ref, "Fixation ref")

    def to_json(self) -> dict[str, Any]:
        return {"point": [_on_grid(self.point[0], "Fixation point x"), _on_grid(self.point[1], "Fixation point y")],
                "height_mm": _on_grid(self.height_mm, "Fixation height_mm"), "ref": self.ref}

    @classmethod
    def from_json(cls, data: object) -> Fixation:
        d = _keys(data, frozenset({"point", "height_mm", "ref"}), "Fixation")
        return cls(d["point"], d["height_mm"], d["ref"])


@dataclass(frozen=True)
class PriorCut:
    """Material already removed from a panel that is not new: it seeds ``Rem(0)`` (safety-model 7.1).

    ``polygon`` is a simple polygon, as for :class:`Obstacle`. A ring-shaped cut
    may be declared by its filled outline: removal that is over-counted is
    conservative for every use of it.
    """

    polygon: tuple[tuple[float, float], ...]
    depth_mm: float
    ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "polygon", _polygon(self.polygon, "PriorCut polygon"))
        object.__setattr__(self, "depth_mm", _positive(self.depth_mm, "PriorCut depth_mm"))
        _text(self.ref, "PriorCut ref")

    def to_json(self) -> dict[str, Any]:
        return {"polygon": _polygon_json(self.polygon, "PriorCut polygon"),
                "depth_mm": _on_grid(self.depth_mm, "PriorCut depth_mm"), "ref": self.ref}

    @classmethod
    def from_json(cls, data: object) -> PriorCut:
        d = _keys(data, frozenset({"polygon", "depth_mm", "ref"}), "PriorCut")
        return cls(d["polygon"], d["depth_mm"], d["ref"])


# ---------------------------------------------------------------------------
# CheckContext
# ---------------------------------------------------------------------------

_CONTEXT_KEYS = frozenset(
    {"params", "footprint", "rho", "r_rot", "gates_sha256", "rotation_model", "heading_map_verified", "start_pose",
     "measured", "obstacles", "fixations", "prior_removal"}
)


def _param(params: Mapping[str, Param[Any]], name: str, unit: str, why: str) -> Param[Any]:
    param = params.get(name)
    if param is None:
        raise ValueError("CheckContext params has no %r: every context value needs its Param (%s)" % (name, why))
    if param.unit != unit:
        raise ValueError("CheckContext params %r has unit %r, expected %r" % (name, param.unit, unit))
    return param


def _same_number(param: Param[Any], value: float, name: str) -> None:
    if isinstance(param.value, (str, tuple)) or param.value != value:
        raise ValueError("CheckContext %s is %r but params %r holds %r" % (name, value, name, param.value))


@dataclass(frozen=True)
class CheckContext:
    """Everything a check reads, every number and choice with its provenance (see the module documentation).

    ``params`` holds the Params by name; ``footprint``, ``rho``, ``r_rot``,
    ``rotation_model`` and ``heading_map_verified`` must equal their Params.
    ``gates`` is the view the context was built under; it is not part of the
    hash (a view need not be hashable) but is compared, and the JSON form
    carries its snapshot hash. ``start_pose`` is None when no start pose is
    known (W-FIRST then refuses), ``measured`` is None at the desk.
    """

    params: Mapping[str, Param[Any]]
    footprint: FootprintSet
    rho: float
    r_rot: float
    gates: GateView = field(hash=False)
    rotation_model: RotationModel
    heading_map_verified: bool
    start_pose: Pose | None
    measured: MeasuredGeometry | None
    obstacles: tuple[Obstacle, ...]
    fixations: tuple[Fixation, ...]
    prior_removal: tuple[PriorCut, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.params, Mapping):
            raise TypeError("CheckContext params must be a mapping of name to Param, got %r" % (self.params,))
        for name, param in self.params.items():
            if not isinstance(name, str) or not name.strip():
                raise TypeError("CheckContext params names must be non-empty strings, got %r" % (name,))
            if not isinstance(param, Param):
                raise TypeError("CheckContext params[%r] must be a Param, got %r" % (name, param))
        params: FrozenMapping[str, Param[Any]] = FrozenMapping(self.params)
        object.__setattr__(self, "params", params)
        if not isinstance(self.footprint, FootprintSet):
            raise TypeError("CheckContext footprint must be a FootprintSet, got %r" % (self.footprint,))
        object.__setattr__(self, "rho", _positive(self.rho, "CheckContext rho"))
        object.__setattr__(self, "r_rot", _positive(self.r_rot, "CheckContext r_rot"))
        if not isinstance(self.gates, GateView):
            raise TypeError("CheckContext gates must provide the GateView protocol, got %r" % (self.gates,))
        _sha256(self.gates.snapshot_sha256(), "CheckContext gates.snapshot_sha256()")
        _choice(self.rotation_model, ROTATION_MODELS, "CheckContext rotation_model")
        if not isinstance(self.heading_map_verified, bool):
            raise TypeError("CheckContext heading_map_verified must be a bool, got %r" % (self.heading_map_verified,))
        if self.start_pose is not None and not isinstance(self.start_pose, Pose):
            raise TypeError("CheckContext start_pose must be a Pose or None, got %r" % (self.start_pose,))
        if self.measured is not None and not isinstance(self.measured, MeasuredGeometry):
            raise TypeError("CheckContext measured must be a MeasuredGeometry or None, got %r" % (self.measured,))
        object.__setattr__(self, "obstacles", _tuple_of(self.obstacles, Obstacle, "CheckContext obstacles"))
        object.__setattr__(self, "fixations", _tuple_of(self.fixations, Fixation, "CheckContext fixations"))
        object.__setattr__(self, "prior_removal", _tuple_of(self.prior_removal, PriorCut, "CheckContext prior_removal"))
        self._check_provenance(params)
        self._check_rotation_radius()
        self._check_gates(params)

    def _check_provenance(self, params: Mapping[str, Param[Any]]) -> None:
        for name, value, why in (("rho", self.rho, "rho = r_w + m_w"), ("r_rot", self.r_rot, "the rotation radius")):
            param = _param(params, name, "mm", why)
            _same_number(param, value, name)
            if param.source != "DERIVED":
                raise ValueError("CheckContext params %r must be DERIVED, naming the Params it was computed from "
                                 "(docs/prep/params.json: derived, not overridable), got %s: only its inputs carry "
                                 "their disclosures, such as an unmeasured wheel radius" % (name, param.source))
        for k, (r, delta) in enumerate(self.footprint.vendor):
            for name, value, unit in zip(VENDOR_SUPPORT_PARAMS[k], (r, delta), ("mm", "deg")):
                param = _param(params, name, unit, "vendor support q%d" % (k + 1))
                _same_number(param, value, name)
                if param.source != "SPEC":
                    raise ValueError("CheckContext params %r is the spec footprint and must have source SPEC, got %s "
                                     "(a measurement adds a measured position; it never replaces the spec)"
                                     % (name, param.source))
        measured = self.footprint.measured or (None, None, None)
        for k, position in enumerate(measured):
            names = MEASURED_SUPPORT_PARAMS[k]
            if position is None:
                stray = [name for name in names if name in params]
                if stray:
                    raise ValueError("CheckContext params has %s but the footprint has no measured position for q%d: "
                                     "the measurement would be silently ignored" % (", ".join(stray), k + 1))
                continue
            for name, value, unit in zip(names, position, ("mm", "deg")):
                param = _param(params, name, unit, "measured support q%d" % (k + 1))
                _same_number(param, value, name)
                if param.source not in _MEASURED_SOURCES:
                    raise ValueError("CheckContext params %r is a measured position and must come from %s, got %s"
                                     % (name, ", ".join(sorted(_MEASURED_SOURCES)), param.source))
        model = _param(params, "rotation_model", "enum", "the gate rotation_model")
        if model.value != self.rotation_model:
            raise ValueError("CheckContext rotation_model is %r but params 'rotation_model' holds %r"
                             % (self.rotation_model, model.value))
        heading = _param(params, "heading_map_verified", "enum", "the gate heading_map_verified")
        if heading.value != _HEADING_VALUES[self.heading_map_verified]:
            raise ValueError("CheckContext heading_map_verified is %r but params 'heading_map_verified' holds %r"
                             % (self.heading_map_verified, heading.value))
        for name, param in (("rotation_model", model), ("heading_map_verified", heading)):
            if param.source not in _GATED_SOURCES:
                raise ValueError("CheckContext params %r must come from GATE, FACT or POLICY, got %s"
                                 % (name, param.source))

    def _check_rotation_radius(self) -> None:
        positions = self.footprint.vendor + tuple(p for p in (self.footprint.measured or ()) if p is not None)
        farthest = max(r for r, _ in positions)
        if self.r_rot < farthest:
            raise ValueError("CheckContext r_rot %r is smaller than a support distance (%r): the rotation disc must "
                             "hold every support position (safety-model 4.6)" % (self.r_rot, farthest))

    def _check_gates(self, params: Mapping[str, Param[Any]]) -> None:
        if self.heading_map_verified:
            if self.gates.allowed("heading_map_verified") is not True:
                raise ValueError("CheckContext heading_map_verified is True but the gate view does not open "
                                 "heading_map_verified: no unknown is read as yes")
            if params["heading_map_verified"].source != "FACT":
                raise ValueError("CheckContext heading_map_verified is True, so its Param must come from a FACT, got %s"
                                 % params["heading_map_verified"].source)
        if self.rotation_model != "unverified":
            if self.gates.allowed("rotation_model") is not True:
                raise ValueError("CheckContext rotation_model is %r but the gate view does not open rotation_model"
                                 % self.rotation_model)
            recorded = self.gates.value("rotation_model")
            if recorded != self.rotation_model:
                raise ValueError("CheckContext rotation_model is %r but the gate view records %r"
                                 % (self.rotation_model, recorded))
            if params["rotation_model"].source != "FACT":
                raise ValueError("CheckContext rotation_model %r must come from a FACT, got %s"
                                 % (self.rotation_model, params["rotation_model"].source))

    def to_json(self) -> dict[str, Any]:
        return {
            "params": {name: param.to_json() for name, param in self.params.items()},
            "footprint": self.footprint.to_json(),
            "rho": _on_grid(self.rho, "CheckContext rho"),
            "r_rot": _on_grid(self.r_rot, "CheckContext r_rot"),
            "gates_sha256": _sha256(self.gates.snapshot_sha256(), "CheckContext gates.snapshot_sha256()"),
            "rotation_model": self.rotation_model,
            "heading_map_verified": self.heading_map_verified,
            "start_pose": None if self.start_pose is None else self.start_pose.to_json(),
            "measured": None if self.measured is None else self.measured.to_json(),
            "obstacles": [o.to_json() for o in self.obstacles],
            "fixations": [f.to_json() for f in self.fixations],
            "prior_removal": [p.to_json() for p in self.prior_removal],
        }

    @classmethod
    def from_json(cls, data: object, *, gates: GateView) -> CheckContext:
        """Read a context back, under the gate view it was built under (its snapshot hash must match)."""
        d = _keys(data, _CONTEXT_KEYS, "CheckContext")
        if not isinstance(gates, GateView):
            raise TypeError("CheckContext.from_json needs a GateView, got %r" % (gates,))
        if gates.snapshot_sha256() != d["gates_sha256"]:
            raise ValueError("CheckContext was built under gate snapshot %s; the given view is gate snapshot %s"
                             % (d["gates_sha256"], gates.snapshot_sha256()))
        raw_params = d["params"]
        if not isinstance(raw_params, Mapping):
            raise ValueError("CheckContext params JSON must be an object, got %r" % (raw_params,))
        return cls(
            params={name: Param.from_json(value) for name, value in raw_params.items()},
            footprint=FootprintSet.from_json(d["footprint"]),
            rho=d["rho"],
            r_rot=d["r_rot"],
            gates=gates,
            rotation_model=d["rotation_model"],
            heading_map_verified=d["heading_map_verified"],
            start_pose=None if d["start_pose"] is None else Pose.from_json(d["start_pose"]),
            measured=None if d["measured"] is None else MeasuredGeometry.from_json(d["measured"]),
            obstacles=tuple(Obstacle.from_json(o) for o in _seq(d["obstacles"], "CheckContext obstacles")),
            fixations=tuple(Fixation.from_json(f) for f in _seq(d["fixations"], "CheckContext fixations")),
            prior_removal=tuple(PriorCut.from_json(p) for p in _seq(d["prior_removal"], "CheckContext prior_removal")),
        )


# ---------------------------------------------------------------------------
# Rung exemptions
# ---------------------------------------------------------------------------


class RungExemptionOriginError(TypeError):
    """A RungExemption was built outside ``goliath_prep.rungs``."""


@dataclass(frozen=True)
class ExemptLine:
    """One line of a rung program exempt from one D-CSTEP rule (:data:`EXEMPT_RULES`), and why.

    ``line`` is 1-based in the generated program.
    """

    line: int
    rule: ExemptRule
    reason: str

    def __post_init__(self) -> None:
        _integer(self.line, "ExemptLine line", 1)
        if not isinstance(self.rule, str) or self.rule not in EXEMPT_RULES:
            raise ValueError("ExemptLine rule must be one of %s (no other check and no other D-CSTEP rule is ever "
                             "exempt), got %r" % (", ".join(EXEMPT_RULES), self.rule))
        _text(self.reason, "ExemptLine reason")

    def to_json(self) -> dict[str, Any]:
        return {"line": self.line, "rule": self.rule, "reason": self.reason}

    @classmethod
    def from_json(cls, data: object) -> ExemptLine:
        d = _keys(data, frozenset({"line", "rule", "reason"}), "ExemptLine")
        return cls(d["line"], d["rule"], d["reason"])


@dataclass(frozen=True)
class RungExemption:
    """The D-CSTEP exceptions of one rung program (safety-model section 14; the project design, section 4.14.1).

    It names the rung (R3, R3b or R4), the SHA-256 of the generated program
    bytes it applies to, and the exact lines with the rule each is exempt from;
    every other check runs in full on every line. A certification of kind
    ``rung`` refuses it for any other payload.

    **Only** ``goliath_prep.rungs`` **builds one.** The constructor refuses any
    other calling module (:class:`RungExemptionOriginError`), also through
    ``dataclasses.replace`` and ``copy.replace``; an architecture test scans
    goliath_prep and goliath_run for calls outside that module. It is written
    into the certificate (:meth:`to_json`) and never read back: there is no
    ``from_json``, and pickling is refused, because the runner re-certifies
    from the bundle's inputs. The run-time check reads the calling module's
    name; it stops a mistake, not code that forges its own module name.
    """

    rung: ExemptionRung
    payload_sha256: str
    lines: tuple[ExemptLine, ...]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError(
            "RungExemption cannot be subclassed (%s): a subclass could replace the origin check and the "
            "validation; only %s builds a RungExemption" % (cls.__qualname__, _RUNGS_MODULE)
        )

    def __post_init__(self) -> None:
        caller = _constructing_module()
        if caller != _RUNGS_MODULE and not (caller or "").startswith(_RUNGS_MODULE + "."):
            raise RungExemptionOriginError(
                "a RungExemption can only be built in %s (the rung program generator); this one was built in %r"
                % (_RUNGS_MODULE, caller)
            )
        _choice(self.rung, EXEMPTION_RUNGS, "RungExemption rung")
        _sha256(self.payload_sha256, "RungExemption payload_sha256")
        lines = _tuple_of(self.lines, ExemptLine, "RungExemption lines")
        if not lines:
            raise ValueError("RungExemption lines is empty: a rung program without exempt lines needs no exemption")
        seen: set[tuple[int, str]] = set()
        for entry in lines:
            if (entry.line, entry.rule) in seen:
                raise ValueError("RungExemption names line %d with rule %s twice" % (entry.line, entry.rule))
            seen.add((entry.line, entry.rule))
        object.__setattr__(self, "lines", tuple(sorted(lines, key=lambda e: (e.line, e.rule))))

    def to_json(self) -> dict[str, Any]:
        return {"rung": self.rung, "payload_sha256": self.payload_sha256, "lines": [e.to_json() for e in self.lines]}

    def __copy__(self) -> RungExemption:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> RungExemption:
        return self

    def __reduce_ex__(self, protocol: object) -> Any:
        raise TypeError(
            "a RungExemption cannot be pickled: only goliath_prep.rungs builds one; record it with to_json()"
        )


def _constructing_module() -> str | None:
    """The name of the module that called the RungExemption constructor.

    Skips the constructor's own frames and those of ``dataclasses`` and
    ``copy`` (``replace``), so a replace is judged by its caller.
    """
    frame = sys._getframe(1)
    while frame is not None:
        if frame.f_code in _OWN_FRAMES or frame.f_globals.get("__name__") in ("dataclasses", "copy"):
            frame = frame.f_back
            continue
        name = frame.f_globals.get("__name__")
        return name if isinstance(name, str) else None
    return None


_OWN_FRAMES = frozenset(
    {RungExemption.__init__.__code__, RungExemption.__post_init__.__code__, _constructing_module.__code__}
)
