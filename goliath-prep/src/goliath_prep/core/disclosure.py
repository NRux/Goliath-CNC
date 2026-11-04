# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Disclosures and the one registry they are shown from (unit F04; the project design, section 8.5).

Every fallback, relaxation, unmeasured default and unverified assumption is a
:class:`Disclosure`. The command line, the interface's top bar, the HTML report
and the certificate all render from one :class:`DisclosureRegistry`, so a
fallback cannot exist without being shown.

How "cannot exist without being shown" is made true:

* **By construction, for values.** A :class:`~goliath_prep.core.params.Param`
  whose source is a fallback (``DEFAULT_UNMEASURED``, ``INFERRED``,
  ``OWNER_PROFILE``) cannot be built without its disclosure, and a ``DERIVED``
  Param carries the disclosures of the values it was derived from. The
  disclosure travels inside the value; :meth:`DisclosureRegistry.bind` raises
  it into the registry being shown.
* **At the boundary, for values.** Whatever shows or certifies a set of values
  calls :meth:`DisclosureRegistry.require_disclosed`, which raises
  :class:`UndisclosedFallbackError` if one of their disclosures is not active
  (never raised, cleared, or active only at a lower severity).
* **For behaviour** (a fallback that is not a Param, such as "upload end
  confirmed by INFO;0 only"), the code that falls back calls
  :meth:`DisclosureRegistry.raise_`, or :meth:`DisclosureRegistry.fallback`,
  which raises the disclosure and returns the fallback value in one
  expression; the tests of that code assert the disclosure. Python cannot
  forbid code to forget; a test can catch it.

Severity is ``info``, ``amber`` or ``red``. ``sticky`` disclosures are never
cleared for the rest of the session ("never auto-dismisses"); a non-sticky one
can be cleared with a stated reason, which is logged and kept.
"""
from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, TypeVar, get_args

if TYPE_CHECKING:
    from goliath_prep.core.findings import Finding
    from goliath_prep.core.params import Param

__all__ = [
    "SEVERITY_RANK",
    "Disclosure",
    "DisclosureRegistry",
    "DisclosureSeverity",
    "StickyDisclosureError",
    "UndisclosedFallbackError",
]

log = logging.getLogger("goliath_prep.disclosure")

V = TypeVar("V")

DisclosureSeverity = Literal["info", "amber", "red"]
SEVERITY_RANK: Mapping[str, int] = MappingProxyType({"info": 0, "amber": 1, "red": 2})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/;=+-]*\Z")
_JSON_KEYS = frozenset({"id", "severity", "text", "source", "sticky"})


class UndisclosedFallbackError(ValueError):
    """A fallback value without its disclosure, or a disclosure that is not shown where it must be."""


class StickyDisclosureError(Exception):
    """An attempt to clear a sticky disclosure."""


def _text(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("Disclosure %s must be a string, got %s" % (name, type(value).__name__))
    if not value.strip():
        raise ValueError("Disclosure %s must not be empty" % name)
    return value


@dataclass(frozen=True)
class Disclosure:
    """One fallback, relaxation, unmeasured default or unverified assumption, as shown to the operator.

    ``id`` is a stable machine id without spaces (for example
    ``DEFAULT_UNMEASURED:r_w``, or a notice's check id such as ``N-DUST``);
    ``text`` says what is in effect and its consequence; ``source`` says where
    it comes from (a file and key, a firmware version, a check).
    """

    id: str
    severity: DisclosureSeverity
    text: str
    source: str
    sticky: bool = True

    def __post_init__(self) -> None:
        _text("id", self.id)
        if not _ID.match(self.id):
            raise ValueError("Disclosure id %r: use letters, digits and . _ : / ; = + - without spaces" % self.id)
        if self.severity not in SEVERITY_RANK:
            raise ValueError("Disclosure severity %r is not one of %s" % (self.severity, get_args(DisclosureSeverity)))
        _text("text", self.text)
        _text("source", self.source)
        if not isinstance(self.sticky, bool):
            raise TypeError("Disclosure sticky must be a bool, got %s" % type(self.sticky).__name__)

    @property
    def rank(self) -> int:
        """0 info, 1 amber, 2 red."""
        return SEVERITY_RANK[self.severity]

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "severity": self.severity, "text": self.text, "source": self.source,
                "sticky": self.sticky}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Disclosure:
        if not isinstance(data, Mapping) or set(data) != _JSON_KEYS:
            raise ValueError("a Disclosure needs exactly the keys %s, got %r" % (sorted(_JSON_KEYS), data))
        return cls(id=data["id"], severity=data["severity"], text=data["text"], source=data["source"],
                   sticky=data["sticky"])

    @classmethod
    def from_finding(cls, finding: Finding, *, severity: DisclosureSeverity, sticky: bool = True) -> Disclosure:
        """The disclosure of a finding of severity ``disclose`` (safety-model 1.2).

        ``severity`` is the caller's choice and has no default: the catalogue
        does not say how loud each notice is.
        """
        if finding.severity != "disclose":
            raise ValueError("only a finding of severity disclose becomes a disclosure; %s is %s"
                             % (finding.check_id, finding.severity))
        where = finding.witness.src if finding.witness is not None else None
        source = finding.check_id if where is None else "%s at %s line %d" % (finding.check_id, where.file, where.line)
        return cls(id=finding.check_id, severity=severity, text=finding.message, source=source, sticky=sticky)


def _key(d: Disclosure) -> tuple[str, str, str]:
    return (d.id, d.source, d.text)


class DisclosureRegistry:
    """Every disclosure in effect, in the order they are shown: red, amber, info; first raised first.

    Raising the same disclosure again (same id, source and text) keeps one
    entry, whose severity becomes the higher of the two and which becomes
    sticky if either was: a disclosure is never lowered or hidden by a repeat.
    The same id with another text or source is another entry. Thread-safe.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str, str], tuple[int, Disclosure]] = {}
        self._cleared: list[tuple[Disclosure, str]] = []
        self._seq = 0

    def raise_(self, d: Disclosure) -> None:
        """Show ``d`` from now on (see the class documentation for repeats)."""
        if not isinstance(d, Disclosure):
            raise TypeError("raise_ takes a Disclosure, got %s" % type(d).__name__)
        key = _key(d)
        with self._lock:
            known = self._entries.get(key)
            if known is None:
                self._entries[key] = (self._seq, d)
                self._seq += 1
                merged, changed = d, True
            else:
                seq, old = known
                merged = Disclosure(id=old.id, severity=old.severity if old.rank >= d.rank else d.severity,
                                    text=old.text, source=old.source, sticky=old.sticky or d.sticky)
                changed = merged != old
                self._entries[key] = (seq, merged)
        if changed:
            log.log(logging.WARNING if merged.rank >= SEVERITY_RANK["amber"] else logging.INFO,
                    "disclosure [%s%s] %s: %s (source: %s)", merged.severity, ", sticky" if merged.sticky else "",
                    merged.id, merged.text, merged.source)

    def fallback(self, d: Disclosure, value: V) -> V:
        """Raise ``d`` and return ``value``, for a fallback that is not a Param.

        Write the fallback as ``x = registry.fallback(disclosure, default)``:
        the expression that falls back is the one that discloses it.
        """
        self.raise_(d)
        return value

    def bind(self, param: Param[Any]) -> Param[Any]:
        """Raise every disclosure ``param`` carries (its own and its inputs'), and return it."""
        for d in _carried(param):
            self.raise_(d)
        return param

    def require_disclosed(self, params: Iterable[Param[Any]]) -> None:
        """Raise :class:`UndisclosedFallbackError` unless every disclosure the params carry is active.

        Active means: an entry with the same id, source and text, at the same
        or a higher severity. Call it wherever values are shown or certified.
        """
        missing: list[str] = []
        with self._lock:
            entries = dict(self._entries)
        for param in params:
            for d in _carried(param):
                known = entries.get(_key(d))
                if known is None or known[1].rank < d.rank:
                    missing.append("%s (%s, %s) of %s" % (d.id, d.severity, "not raised" if known is None
                                                           else "shown as %s" % known[1].severity, param.ref))
        if missing:
            raise UndisclosedFallbackError(
                "values are used without their disclosure being shown: " + "; ".join(missing)
            )

    def clear(self, id: str, reason: str) -> int:
        """Remove the non-sticky disclosures with this id; return how many. Sticky ones refuse.

        Nothing is removed if any entry with this id is sticky. The reason is
        logged and kept in :meth:`cleared`.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("clearing a disclosure needs a reason")
        with self._lock:
            matching = [(key, d) for key, (_, d) in self._entries.items() if d.id == id]
            sticky = [d for _, d in matching if d.sticky]
            if sticky:
                raise StickyDisclosureError("disclosure %s is sticky and stays shown for this session" % id)
            for key, d in matching:
                del self._entries[key]
                self._cleared.append((d, reason))
        for _, d in matching:
            log.info("disclosure %s cleared: %s (it said: %s)", d.id, reason, d.text)
        return len(matching)

    def active(self) -> tuple[Disclosure, ...]:
        """The disclosures in effect: red, then amber, then info; within each, first raised first."""
        with self._lock:
            ordered = sorted(self._entries.values(), key=lambda item: (-item[1].rank, item[0]))
        return tuple(d for _, d in ordered)

    def cleared(self) -> tuple[tuple[Disclosure, str], ...]:
        """Every disclosure cleared so far, with its reason, in order."""
        with self._lock:
            return tuple(self._cleared)

    def to_json(self) -> list[dict[str, Any]]:
        """:meth:`active` as JSON objects, in the same order."""
        return [d.to_json() for d in self.active()]

    @classmethod
    def from_json(cls, data: Iterable[Mapping[str, Any]]) -> DisclosureRegistry:
        registry = cls()
        for entry in data:
            registry.raise_(Disclosure.from_json(entry))
        return registry

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __repr__(self) -> str:
        return "DisclosureRegistry(%r)" % (self.active(),)


def _carried(param: object) -> tuple[Disclosure, ...]:
    from goliath_prep.core.params import Param

    if not isinstance(param, Param):
        raise TypeError("expected a Param, got %s" % type(param).__name__)
    return param.all_disclosures()
