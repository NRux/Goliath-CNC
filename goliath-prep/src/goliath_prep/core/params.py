# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Param: a value with its unit, its source and where it came from (unit F04; red-team PHYS-12).

Every number a check uses, and every value a certificate binds, is a
:class:`Param`, so the report and the certificate can say where each one came
from (docs/prep/parameters.md section 1: "A default is never silent").

**Sources** (:data:`Source`). The classes of the parameter catalogue
(docs/prep/parameters.md section 2: ``SPEC``, ``DEFAULT_UNMEASURED``,
``INFERRED``, ``POLICY``, ``GATE``, ``DERIVED``, ``OWNER_JOB``) and the sources
that replace them at run time (section 2.1: ``ROBOT_MEASURED``,
``OWNER_PROFILE``, ``FACT``, ``OWNER_JOB``).

**Fallbacks carry their disclosure.** A value whose source is in
:data:`DISCLOSED_SOURCES` cannot be built without a disclosure at least as
severe as listed there: ``DEFAULT_UNMEASURED`` needs ``amber`` (the banner
"UNMEASURED MACHINE PROFILE"), ``INFERRED`` and ``OWNER_PROFILE`` need one at
any severity. A ``DERIVED`` value names the Params it was computed from
(``inputs``) and so carries their disclosures too (:meth:`Param.all_disclosures`):
a radius computed from an unmeasured wheel radius is still unmeasured.
``dataclasses.replace`` validates again, so it cannot strip a disclosure.

**Exact in canonical JSON.** Canonical JSON writes floats to 0.001
(:mod:`goliath_prep.core.canon`). :meth:`Param.to_json` refuses a float the
rounding would change by more than float noise (a few units in the last
place, at least 1e-9), so a report never shows a value other than the one a
check used. Quantize computed values explicitly
(:func:`goliath_prep.core.canon.quantize`, in the direction that is
conservative for their use), or hold a finer value in a smaller unit
(``um``).
"""
from __future__ import annotations

import math
import numbers
from collections.abc import Iterable, Mapping
from dataclasses import KW_ONLY, dataclass
from types import MappingProxyType
from typing import Any, Generic, Literal, TypeVar, get_args

from goliath_prep.core.canon import quantize
from goliath_prep.core.disclosure import SEVERITY_RANK, Disclosure, UndisclosedFallbackError

__all__ = ["DISCLOSED_SOURCES", "SOURCES", "UNITS", "Param", "Source"]

T = TypeVar("T")

Source = Literal[
    "SPEC",
    "ROBOT_MEASURED",
    "OWNER_PROFILE",
    "OWNER_JOB",
    "FACT",
    "DEFAULT_UNMEASURED",
    "DERIVED",
    "INFERRED",
    "POLICY",
    "GATE",
]
SOURCES: frozenset[str] = frozenset(get_args(Source))

# The least severe disclosure each fallback source must carry (read-only).
DISCLOSED_SOURCES: Mapping[str, str] = MappingProxyType(
    {"DEFAULT_UNMEASURED": "amber", "INFERRED": "info", "OWNER_PROFILE": "info"}
)

# The units of docs/prep/params.json (parameters.md section 4), plus "um" for
# values finer than the canonical 0.001 mm (docs/prep/params.json roundtrip_tol).
UNITS: frozenset[str] = frozenset({"mm", "um", "deg", "mm/min", "rpm", "byte", "dm3", "percent", "min", "enum"})

_NOISE = 1e-9
_JSON_KEYS = frozenset({"value", "unit", "source", "ref", "disclosures", "inputs"})


@dataclass(frozen=True)
class Param(Generic[T]):
    """A value with its ``unit``, its ``source`` (provenance class) and ``ref``, where it came from.

    ``ref`` names the place a reader can check: a file and key
    (``job.toml panel.T``, ``docs/prep/params.json r_w``), a spec section, a
    fact id, or for a derived value its formula. ``value`` is a number (int or
    float, finite), a non-empty tuple of numbers, or for unit ``enum`` a
    non-empty string. A list is stored as a tuple.
    """

    value: T
    unit: str
    source: Source
    ref: str
    _: KW_ONLY
    disclosures: tuple[Disclosure, ...] = ()
    inputs: tuple[Param[Any], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.ref, str):
            raise TypeError("a Param needs a ref (where its value came from) as a string, got %r" % (self.ref,))
        if not self.ref.strip():
            raise ValueError("a Param needs a ref: where its value came from")
        if self.source not in SOURCES:
            raise ValueError("Param %s: source %r is not one of %s" % (self.ref, self.source, sorted(SOURCES)))
        if self.unit not in UNITS:
            raise ValueError("Param %s: unit %r is not one of %s" % (self.ref, self.unit, sorted(UNITS)))
        object.__setattr__(self, "value", _checked_value(self.value, self.unit, self.ref))
        object.__setattr__(self, "disclosures", _tuple_of(self.disclosures, Disclosure, "disclosures", self.ref))
        object.__setattr__(self, "inputs", _tuple_of(self.inputs, Param, "inputs", self.ref))
        if self.source == "DERIVED" and not self.inputs:
            raise ValueError("Param %s: a DERIVED value names the Params it was derived from (inputs)" % self.ref)
        if self.source != "DERIVED" and self.inputs:
            raise ValueError("Param %s: only a DERIVED value has inputs; this one is %s" % (self.ref, self.source))
        needed = DISCLOSED_SOURCES.get(self.source)
        if needed is not None and not any(d.rank >= SEVERITY_RANK[needed] for d in self.disclosures):
            raise UndisclosedFallbackError(
                "Param %s: a %s value cannot exist without a disclosure of severity %s or higher (it has %s)"
                % (self.ref, self.source, needed, [d.severity for d in self.disclosures] or "none")
            )

    @classmethod
    def derived(cls, value: T, unit: str, ref: str, inputs: Iterable[Param[Any]]) -> Param[T]:
        """A ``DERIVED`` value computed from ``inputs``; it carries their disclosures."""
        return cls(value, unit, "DERIVED", ref, inputs=tuple(inputs))

    def all_disclosures(self) -> tuple[Disclosure, ...]:
        """Its own disclosures, then its inputs', depth first, each once."""
        seen: dict[Disclosure, None] = {}
        stack: list[Param[Any]] = [self]
        while stack:
            param = stack.pop(0)
            for d in param.disclosures:
                seen.setdefault(d, None)
            stack[0:0] = param.inputs
        return tuple(seen)

    def to_json(self) -> dict[str, Any]:
        _require_exact(self.value, self.unit, self.ref)
        return {
            "value": list(self.value) if isinstance(self.value, tuple) else self.value,
            "unit": self.unit,
            "source": self.source,
            "ref": self.ref,
            "disclosures": [d.to_json() for d in self.disclosures],
            "inputs": [p.to_json() for p in self.inputs],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Param[Any]:
        if not isinstance(data, Mapping) or set(data) != _JSON_KEYS:
            raise ValueError("a Param needs exactly the keys %s, got %r" % (sorted(_JSON_KEYS), data))
        return cls(
            data["value"],
            data["unit"],
            data["source"],
            data["ref"],
            disclosures=tuple(Disclosure.from_json(d) for d in data["disclosures"]),
            inputs=tuple(cls.from_json(p) for p in data["inputs"]),
        )


def _number(value: object, ref: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (numbers.Integral, float)):
        raise TypeError("Param %s: value %r must be a number (int or float)" % (ref, value))
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Param %s: value %r must be finite" % (ref, value))
        return float(value)
    return int(value)


def _checked_value(value: object, unit: str, ref: str) -> object:
    if value is None:
        raise ValueError("Param %s: no value. A missing value is not a Param: refuse, or disclose a fallback" % ref)
    if unit == "enum":
        if not isinstance(value, str):
            raise TypeError("Param %s: an enum value must be a string, got %r" % (ref, value))
        if not value.strip():
            raise ValueError("Param %s: an enum value must not be empty" % ref)
        return value
    if isinstance(value, (list, tuple)):
        if not value:
            raise ValueError("Param %s: a tuple value must not be empty" % ref)
        return tuple(_number(v, ref) for v in value)
    return _number(value, ref)


def _tuple_of(items: object, kind: type, name: str, ref: str) -> tuple[Any, ...]:
    if not isinstance(items, (list, tuple)):
        raise TypeError("Param %s: %s must be a tuple of %s" % (ref, name, kind.__name__))
    for item in items:
        if not isinstance(item, kind):
            raise TypeError("Param %s: %s must hold %s objects, got %r" % (ref, name, kind.__name__, item))
    return tuple(items)


def _require_exact(value: object, unit: str, ref: str) -> None:
    for v in value if isinstance(value, tuple) else (value,):
        if not isinstance(v, float):
            continue
        # quantize() also refuses what canonical JSON cannot write at all.
        if abs(v - quantize(v)) > max(_NOISE, 8 * math.ulp(v)):
            raise ValueError(
                "Param %s: value %r %s is not on the canonical 0.001 grid, so canonical JSON would show another "
                "value than the one used. Quantize it explicitly (goliath_prep.core.canon.quantize, in the "
                "direction that is conservative for its use) or hold it in a smaller unit" % (ref, v, unit)
            )
