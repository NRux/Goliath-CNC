# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Findings of the safety checks, their witnesses and source references (unit F04).

A :class:`Finding` is what a check reports: its check id from the catalogue
(``docs/prep/checks.json``, as constants in :mod:`goliath_prep.core.ids`), the
catalogue's severity, a message in workshop words, a :class:`Witness` and
optionally the hazard's geometry as WKT.

The catalogue is the contract, and a Finding enforces it when built:

* the check id must be in the catalogue;
* the severity must be the catalogue's severity for that check, or
  ``internal-error`` (any check can fail as an assertion of the checker). A
  refusal can never be filed as a warning or a notice, and nothing can be
  filed more leniently than the catalogue says;
* a finding of severity ``refuse`` or ``internal-error`` carries a witness
  (safety-model 11.3: "On a failure every finding carries a witness");
* the witness fills only fields the check lists in ``witness_fields``. A listed
  field may be None when it is genuinely unknown (``W-FIRST`` without a start
  pose has no slack); never put a placeholder in it.

The witness fields are those of safety-model section 11.3; the first seven
(``piece``, ``cut``, ``src``, ``wheel``, ``point``, ``hazard``, ``slack_mm``)
are the project design's, the others are the catalogue's. Every field is
keyword-only, so two integers (piece, cut) cannot be swapped by position.

Numbers in a witness are written to canonical JSON rounded to 0.001
(:mod:`goliath_prep.core.canon`); that is the documented form of a report.
"""
from __future__ import annotations

import math
import numbers
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any, Literal, get_args

from goliath_prep.core import ids

__all__ = ["FINDING_SEVERITIES", "WITNESS_FIELDS", "Finding", "FindingSeverity", "SourceRef", "Witness"]

FindingSeverity = Literal["refuse", "warn", "disclose", "internal-error"]
FINDING_SEVERITIES: tuple[str, ...] = get_args(FindingSeverity)
_NEEDS_WITNESS = frozenset({"refuse", "internal-error"})
_FINDING_KEYS = frozenset({"check_id", "severity", "message", "witness", "geometry_wkt"})
_SOURCE_KEYS = frozenset({"file", "line", "text"})

Scalar = str | int | float | bool


@dataclass(frozen=True)
class SourceRef:
    """A line of an input file: its name, 1-based line number (counted like .NET File.ReadAllLines) and text."""

    file: str
    line: int
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.file, str):
            raise TypeError("SourceRef file must be a string, got %r" % (self.file,))
        if not self.file.strip():
            raise ValueError("SourceRef file must not be empty")
        if isinstance(self.line, bool) or not isinstance(self.line, int):
            raise TypeError("SourceRef line must be an int, got %r" % (self.line,))
        if self.line < 1:
            raise ValueError("SourceRef line is 1-based, got %d" % self.line)
        if not isinstance(self.text, str):
            raise TypeError("SourceRef text must be a string, got %r" % (self.text,))

    def to_json(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "text": self.text}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SourceRef:
        if not isinstance(data, Mapping) or set(data) != _SOURCE_KEYS:
            raise ValueError("a SourceRef needs exactly the keys %s, got %r" % (sorted(_SOURCE_KEYS), data))
        return cls(data["file"], data["line"], data["text"])


@dataclass(frozen=True, kw_only=True)
class Witness:
    """What a failed check points at (safety-model 11.3). Only ``hazard`` is required.

    ``margins`` and ``sources`` may be given as mappings; they are stored as
    tuples of (name, value) pairs sorted by name, so a Witness is hashable.
    """

    piece: int | None = None
    cut: int | None = None
    src: SourceRef | None = None
    wheel: int | None = None
    point: tuple[float, float] | None = None
    hazard: str
    slack_mm: float | None = None
    column: int | None = None
    raw_mm: float | None = None
    margins: tuple[tuple[str, float], ...] | None = None
    heading_c: int | float | None = None
    rule: str | None = None
    tower2_placement: Literal["corner", "measured"] | None = None
    gate: str | None = None
    unlock_rung: str | None = None
    value: Scalar | None = None
    limit: Scalar | None = None
    sources: tuple[tuple[str, Scalar], ...] | None = None
    island: int | str | None = None
    fact: str | None = None

    def __post_init__(self) -> None:
        _index(self.piece, "piece", minimum=0)
        _index(self.cut, "cut", minimum=0)
        _index(self.column, "column", minimum=1)
        if self.src is not None and not isinstance(self.src, SourceRef):
            raise TypeError("Witness src must be a SourceRef, got %r" % (self.src,))
        if self.wheel is not None:
            _index(self.wheel, "wheel", minimum=1)
            if self.wheel not in (1, 2, 3):
                raise ValueError("Witness wheel is the support 1 (rear), 2 (right) or 3 (left), got %d" % self.wheel)
        if self.point is not None:
            if not isinstance(self.point, (list, tuple)) or len(self.point) != 2:
                raise ValueError("Witness point must be [x, y], got %r" % (self.point,))
            object.__setattr__(self, "point", tuple(_real(v, "point") for v in self.point))
        _words(self.hazard, "hazard", required=True)
        for name in ("slack_mm", "raw_mm"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _real(value, name))
        if self.margins is not None:
            object.__setattr__(self, "margins", _pairs(self.margins, "margins", _real))
        if self.heading_c is not None:
            heading = _real(self.heading_c, "heading_c")
            object.__setattr__(self, "heading_c", int(self.heading_c) if isinstance(self.heading_c, numbers.Integral)
                               else heading)
        for name in ("rule", "gate", "unlock_rung", "fact"):
            _words(getattr(self, name), name, required=False)
        if self.tower2_placement is not None and self.tower2_placement not in ("corner", "measured"):
            raise ValueError("Witness tower2_placement is corner or measured, got %r" % (self.tower2_placement,))
        for name in ("value", "limit"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _scalar(value, name))
        if self.sources is not None:
            object.__setattr__(self, "sources", _pairs(self.sources, "sources", _scalar))
        if self.island is not None:
            if isinstance(self.island, str):
                _words(self.island, "island", required=True)
            else:
                _index(self.island, "island", minimum=0)

    def fields_set(self) -> frozenset[str]:
        """The names of the fields that are not None."""
        return frozenset(f.name for f in fields(self) if getattr(self, f.name) is not None)

    def to_json(self) -> dict[str, Any]:
        """The fields that are set; ``src`` as an object, ``point`` as [x, y], ``margins``/``sources`` as objects."""
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if value is None:
                continue
            if f.name == "src":
                value = value.to_json()
            elif f.name == "point":
                value = list(value)
            elif f.name in ("margins", "sources"):
                value = dict(value)
            out[f.name] = value
        return out

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Witness:
        if not isinstance(data, Mapping):
            raise ValueError("a Witness is a JSON object, got %r" % (data,))
        unknown = sorted(set(data) - set(WITNESS_FIELDS))
        if unknown:
            raise ValueError("unknown witness field(s) %s" % unknown)
        if "hazard" not in data:
            raise ValueError("a Witness needs a hazard")
        kwargs = dict(data)
        if kwargs.get("src") is not None:
            kwargs["src"] = SourceRef.from_json(kwargs["src"])
        return cls(**kwargs)


WITNESS_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(Witness))
"""Every witness field, in declaration order (safety-model 11.3)."""


@dataclass(frozen=True)
class Finding:
    """One result of a check: check id, severity, message, witness and optional hazard geometry (WKT).

    Prefer :meth:`of`, which takes the severity from the catalogue.
    """

    check_id: str
    severity: FindingSeverity
    message: str
    witness: Witness | None
    geometry_wkt: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.check_id, str) or self.check_id not in ids.SEVERITY:
            raise ValueError("Finding check id %r is not in the check catalogue (docs/prep/checks.json)"
                             % (self.check_id,))
        if self.severity not in FINDING_SEVERITIES:
            raise ValueError("Finding severity %r is not one of %s" % (self.severity, FINDING_SEVERITIES))
        catalogue = ids.SEVERITY[self.check_id]
        if self.severity not in (catalogue, "internal-error"):
            raise ValueError("Finding %s: severity %s, but the catalogue's severity for it is %s "
                             "(only internal-error may differ)" % (self.check_id, self.severity, catalogue))
        if not isinstance(self.message, str):
            raise TypeError("Finding message must be a string, got %r" % (self.message,))
        if not self.message.strip():
            raise ValueError("Finding message must not be empty")
        if self.witness is not None and not isinstance(self.witness, Witness):
            raise TypeError("Finding witness must be a Witness, got %r" % (self.witness,))
        if self.witness is None and self.severity in _NEEDS_WITNESS:
            raise ValueError("Finding %s: a %s finding carries a witness (safety-model 11.3)"
                             % (self.check_id, self.severity))
        if self.witness is not None:
            extra = sorted(self.witness.fields_set() - set(ids.WITNESS_FIELDS[self.check_id]))
            if extra:
                raise ValueError("Finding %s: witness field(s) %s are not among the check's witness_fields %s "
                                 "(docs/prep/checks.json)" % (self.check_id, extra,
                                                              list(ids.WITNESS_FIELDS[self.check_id])))
        if self.geometry_wkt is not None:
            if not isinstance(self.geometry_wkt, str):
                raise TypeError("Finding geometry_wkt must be a string, got %r" % (self.geometry_wkt,))
            if not self.geometry_wkt.strip():
                raise ValueError("Finding geometry_wkt must not be empty (use None for no geometry)")

    @classmethod
    def of(cls, check_id: str, message: str, *, witness: Witness | None, geometry_wkt: str | None = None) -> Finding:
        """A finding with the catalogue's severity for ``check_id``."""
        if not isinstance(check_id, str) or check_id not in ids.SEVERITY:
            raise ValueError("Finding check id %r is not in the check catalogue (docs/prep/checks.json)"
                             % (check_id,))
        return cls(check_id, ids.SEVERITY[check_id], message, witness, geometry_wkt)  # type: ignore[arg-type]

    def to_json(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "severity": self.severity,
            "message": self.message,
            "witness": None if self.witness is None else self.witness.to_json(),
            "geometry_wkt": self.geometry_wkt,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Finding:
        if not isinstance(data, Mapping) or set(data) != _FINDING_KEYS:
            raise ValueError("a Finding needs exactly the keys %s, got %r" % (sorted(_FINDING_KEYS), data))
        witness = None if data["witness"] is None else Witness.from_json(data["witness"])
        return cls(data["check_id"], data["severity"], data["message"], witness, data["geometry_wkt"])


def _index(value: object, name: str, *, minimum: int) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Witness %s must be an int, got %r" % (name, value))
    if value < minimum:
        raise ValueError("Witness %s must be at least %d, got %d" % (name, minimum, value))


def _real(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (numbers.Integral, float)):
        raise TypeError("Witness %s must be a number, got %r" % (name, value))
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Witness %s must be finite, got %r" % (name, value))
    return result


def _scalar(value: object, name: str) -> Scalar:
    if isinstance(value, (bool, str)):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Witness %s must be finite, got %r" % (name, value))
        return float(value)
    raise TypeError("Witness %s must be a string, number or bool, got %r" % (name, value))


def _words(value: object, name: str, *, required: bool) -> None:
    if value is None and not required:
        return
    if not isinstance(value, str):
        raise TypeError("Witness %s must be a string, got %r" % (name, value))
    if not value.strip():
        raise ValueError("Witness %s must not be empty" % name)


def _pairs(value: object, name: str, convert: Any) -> tuple[tuple[str, Any], ...]:
    items = value.items() if isinstance(value, Mapping) else value
    if not isinstance(items, (list, tuple)) and not isinstance(value, Mapping):
        raise TypeError("Witness %s must be a mapping of name to value, got %r" % (name, value))
    pairs: dict[str, Any] = {}
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise TypeError("Witness %s must be a mapping of name to value, got %r" % (name, value))
        key, entry = item
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Witness %s names must be non-empty strings, got %r" % (name, key))
        if key in pairs:
            raise ValueError("Witness %s names %r twice" % (name, key))
        try:
            pairs[key] = convert(entry, name)
        except ValueError as exc:
            raise ValueError("Witness %s %r: %s" % (name, key, exc)) from None
        except TypeError as exc:
            raise TypeError("Witness %s %r: %s" % (name, key, exc)) from None
    return tuple(sorted(pairs.items()))
