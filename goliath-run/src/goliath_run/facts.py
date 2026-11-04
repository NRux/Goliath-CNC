# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Facts about one robot, with provenance, bound to its serial, firmware and fingerprint (unit R01).

The project design, section 3.1: what the runner and goliath-prep may assume
about a robot comes from three sources, the firmware's vendor gates
(``goliath_link.client.FirmwareCapabilities``), the facts observed on this robot
(this module) and the owner's policy (:mod:`goliath_run.capabilities`).

**A fact** (:class:`Fact`) is one value with where it came from: its ``basis``,
the unknown it resolves (``U-nn``), the bring-up rung that recorded it, the
evidence (record files and their SHA-256), the robot's serial, the firmware it
was observed on and when. Bases:

* ``observed`` - seen on this robot through a rung's go criteria (the read-only
  probe of rung R1, the air job of R3, ...). Bound to serial, firmware and
  behavioural fingerprint.
* ``robot-measured`` - measured by hand on the switched-off robot and typed in
  through the measurement entry K9 (:meth:`FactStore.record_measurement`;
  red-team C-13), with unit, tolerance, operator and time. Bound to the serial
  only: a tape measurement does not depend on the firmware.
* ``owner`` - an owner's answer; ``spec-constant``, ``vendor-gate``, ``default``
  - values from the specification, the vendor app's gates or a default.
  ``firmware-static`` - derived from static analysis of the 01.02.00 image
  (single-source; "unverified for this version" on any other version). None of
  these five ever opens a gate (:mod:`goliath_run.capabilities`).

**Binding and staleness.** A :class:`FactStore` holds the facts of one robot
(``<root>/robots/<serial>/facts.json``) and is opened for the serial and the
firmware that IDENTIFY read in this session. Observed facts become **stale** -
shown, never used - when

* the firmware changes (a session opened on another version is recorded, and
  every observed fact from before it stays stale, even if the robot is later
  flashed back);
* the serial differs (a fact copied from another robot's store);
* the behavioural fingerprint is contradicted (U-02): an observation of the
  fingerprint that the version family does not accept makes every observed fact
  recorded before it stale. The run that showed the difference is evidence of
  this build and stays. Any observation can contradict; only observations a
  rung recorded complete the fingerprint (uncertain evidence may restrict,
  never permit).

Two recording rules keep that binding from being undone later (refused with
:class:`FactBindingError`, naming the times): a fact bound to a firmware whose
``recorded_at`` lies before the robot was last seen on *another* firmware is from
an earlier firmware era (an old probe report imported after a reflash cannot
revive what the reflash made stale); and a fact older than the one of the same
id already in the store would replace a newer value (reports imported, or
measurements typed in, out of order).

**Fingerprints per version family** (red-team FW-16, C-07): for 01.02.00 the
expected behaviour is read from the static image (:data:`FINGERPRINTS`,
docs/protocol/09-firmware-01.02.00.md). Any newer version (branch B3) has no
image: the first observation of each part recorded on that version is its
baseline, and a later one that differs is a contradiction. Facts and rules
derived from 01.02.00 static analysis are "unverified for this version" there
(:meth:`FactStore.unverified`, :data:`STATIC_01_02_00_RULES`).

**Rung R1** (design 11.2): :func:`read_probe_report` and
:func:`import_probe_report` turn the read-only probe's JSON report into the R1
facts, only when the run is a go (complete, routing nominal, version parsed and
named alike by every identity line, every warning acknowledged by a named
person); anything else is refused with the reason, never imported in part. The
probe's own notes and the count of lines it could not credit to a command are
carried into the import's notes. The sections of the slot-release sweep and
the idle-keepalive session (rung R1b) are not written by the probe of this
checkout: their facts are listed as absent, and a report that carries either
section in a form this version does not know is refused (it is never guessed).

Storage: canonical JSON (goliath_prep.core.canon), written atomically
(temporary file, fsync, rename, fsync of the folder on POSIX). A file this
version cannot read - corrupt, of a newer schema, of another serial - fails
closed with the file named. A store that changed on disk since it was read
refuses to write (:class:`FactStoreConflictError`), so two writers cannot
silently lose each other's facts. Across processes the per-robot lock of the
design (section 3.4, unit L09) serialises sessions.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, get_args

from goliath_link.client import format_version, parse_firmware_version
from goliath_prep.core.canon import canonical_json, canonical_sha256, quantize

from goliath_run.types import EnterMeasurement, FrozenMap

__all__ = [
    "ABSENT_UNTIL_R1B",
    "FACT_BASES",
    "FINGERPRINTS",
    "FINGERPRINT_FACT_IDS",
    "GATE_BASES",
    "R1_FACT_IDS",
    "STATIC_01_02_00_RULES",
    "STATIC_FAMILY",
    "Contradiction",
    "Fact",
    "FactBasis",
    "FactBindingError",
    "FactStore",
    "FactStoreConflictError",
    "FactStoreError",
    "Fingerprint",
    "FingerprintStatus",
    "ProbeImport",
    "ProbeReportError",
    "check_firmware",
    "check_serial",
    "firmware_tuple",
    "freeze_json",
    "import_probe_report",
    "read_probe_report",
    "thaw",
    "utc_text",
]

log = logging.getLogger("goliath_run.facts")

FactBasis = Literal["observed", "robot-measured", "owner", "spec-constant", "firmware-static", "vendor-gate",
                    "default"]
FACT_BASES: tuple[str, ...] = get_args(FactBasis)
GATE_BASES: frozenset[str] = frozenset({"observed", "robot-measured"})
"""The bases a gate may open on (:mod:`goliath_run.capabilities`); the others can only be shown."""

STATIC_FAMILY = "01.02.00"
"""The one firmware image that survives and was analysed statically."""

SCHEMA = "goliath-run/facts@1"
FACTS_FILE = "facts.json"

_FACT_ID = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+\Z")
_UNKNOWN_ID = re.compile(r"U-\d{2}\Z")
_RUNG = re.compile(r"R\d{1,2}[a-z]?\Z")
_FIRMWARE = re.compile(r"\d{2}\.\d{2}\.\d{2}\Z")
_SERIAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
# Device names Windows will not create as a folder, with or without an extension.
_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)}
                      | {"LPT%d" % i for i in range(1, 10)})
_NOISE = 1e-9


class FactStoreError(Exception):
    """The facts file cannot be read or written as it is (named in the message); nothing is assumed."""


class FactStoreConflictError(FactStoreError):
    """The facts file changed on disk since this store read it; nothing was written."""


class FactBindingError(ValueError):
    """A fact does not belong to this store's robot or firmware."""


class ProbeReportError(ValueError):
    """A probe report that cannot be imported as rung R1 facts (the reason is in the message)."""


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _text(what: str, value: object, *, empty: bool = False) -> str:
    if type(value) is not str:
        raise TypeError("%s must be a str, got %s" % (what, type(value).__name__))
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("%s contains a lone surrogate, which UTF-8 cannot carry (%s)" % (what, exc)) from None
    if not empty and not value.strip():
        raise ValueError("%s must not be empty" % what)
    return value


def check_serial(serial: object) -> str:
    """``serial`` if it is safe as a folder name on Windows and Linux, else ValueError."""
    text = _text("serial", serial)
    if not _SERIAL.match(text) or text.endswith(".") or text.split(".")[0].upper() in _RESERVED:
        raise ValueError(
            "serial %r cannot name a folder safely: use 1 to 64 letters, digits, '.', '_' or '-', starting with a "
            "letter or digit, not ending in '.', and not a reserved device name such as CON or NUL" % (text,)
        )
    return text


def check_firmware(firmware: object, what: str = "firmware") -> str | None:
    """``"01.02.00"`` style text, or None for unknown."""
    if firmware is None:
        return None
    text = _text(what, firmware)
    if not _FIRMWARE.match(text):
        raise ValueError("%s %r: write the version as xx.yy.zz, for example 01.02.00" % (what, text))
    return text


def firmware_tuple(firmware: str) -> tuple[int, int, int]:
    """``"01.02.03"`` -> ``(1, 2, 3)``."""
    text = check_firmware(firmware)
    if text is None:
        raise ValueError("no firmware version")
    a, b, c = (int(p) for p in text.split("."))
    return (a, b, c)


def utc_text(value: object, what: str) -> str:
    """An ISO-8601 UTC time as ``YYYY-MM-DDTHH:MM:SS[.ffffff]Z``; naive and non-UTC times are refused."""
    if isinstance(value, datetime):
        moment = value
    elif type(value) is str:
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("%s %r is not an ISO-8601 time such as 2026-11-04T10:00:00Z" % (what, value)) from None
    else:
        raise TypeError("%s must be an ISO-8601 UTC string or a datetime, got %s" % (what, type(value).__name__))
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ValueError("%s %r must be in UTC (with Z or +00:00): a time without its zone is ambiguous"
                         % (what, value if isinstance(value, str) else value.isoformat()))
    moment = moment.astimezone(timezone.utc)
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        text += ".%06d" % moment.microsecond
    return text + "Z"


def _moment(text: str) -> datetime:
    """A time written by :func:`utc_text`, for comparing (the texts do not sort as times: ``.5Z`` < ``Z``)."""
    return datetime.fromisoformat(text[:-1] + "+00:00")


def freeze_json(value: object, path: str) -> object:
    """A JSON value, frozen (mappings as FrozenMap, arrays as tuples), floats on the canonical 0.001 grid."""
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("%s: %r is not a JSON number" % (path, value))
        if abs(value - quantize(value)) > max(_NOISE, 8 * math.ulp(value)):
            raise ValueError(
                "%s: %r is not on the canonical 0.001 grid, so a record would show another value than the one in "
                "use; quantize it explicitly (in the direction that is conservative for its use) or use a smaller "
                "unit" % (path, value)
            )
        return value
    if type(value) is str:
        return _text(path, value, empty=True)
    if isinstance(value, Mapping):
        for key in value:
            if type(key) is not str:
                raise TypeError("%s: JSON object keys must be strings, got %s key %r" % (path, type(key).__name__, key))
        frozen = {key: freeze_json(item, "%s.%s" % (path, key)) for key, item in value.items()}
        return FrozenMap(frozen, _path=path)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item, "%s[%d]" % (path, i)) for i, item in enumerate(value))
    raise TypeError("%s: %s is not a JSON value (use bool, int, float, str, a list or a mapping with str keys)"
                    % (path, type(value).__name__))


def thaw(value: object) -> object:
    """Plain JSON data (dicts and lists) from a frozen fact value."""
    if isinstance(value, FrozenMap):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


def _keys(what: str, data: object, expected: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise TypeError("%s: expected a JSON object, got %s" % (what, type(data).__name__))
    missing = sorted(expected - set(data))
    extra = sorted(str(k) for k in set(data) - expected)
    if missing or extra:
        raise ValueError("%s: missing key(s) %s, unexpected key(s) %s" % (what, missing or "none", extra or "none"))
    return data


# ---------------------------------------------------------------------------
# Fact
# ---------------------------------------------------------------------------

_FACT_KEYS = frozenset({"id", "value", "basis", "unknown_id", "rung", "evidence", "serial", "firmware",
                        "recorded_at", "note"})


@dataclass(frozen=True)
class Fact:
    """One value about one robot, with its provenance (design 3.1).

    ``value`` is JSON data and never None: an unknown is not a fact, it is the
    absence of one. Mappings are held as read-only ``FrozenMap`` and lists as
    tuples, so a fact cannot change after it was checked; floats must lie on the
    canonical 0.001 grid. ``recorded_at`` is normalised to ISO-8601 UTC with a
    ``Z``. ``serial`` is private: it stays on this computer (exports redact it).
    """

    id: str
    value: Any
    basis: FactBasis
    unknown_id: str | None
    rung: str | None
    evidence: tuple[str, ...]
    serial: str | None
    firmware: str | None
    recorded_at: str
    note: str = ""

    def __post_init__(self) -> None:
        if not _FACT_ID.match(_text("Fact id", self.id)):
            raise ValueError("Fact id %r: use dotted lower-case ids such as panel.vertex_frame" % (self.id,))
        if self.value is None:
            raise ValueError("Fact %s has no value: an unknown is not a fact; record nothing instead" % self.id)
        object.__setattr__(self, "value", freeze_json(self.value, "Fact %s value" % self.id))
        if self.basis not in FACT_BASES:
            raise ValueError("Fact %s basis %r is not one of %s" % (self.id, self.basis, ", ".join(FACT_BASES)))
        if self.unknown_id is not None and not _UNKNOWN_ID.match(_text("Fact unknown_id", self.unknown_id)):
            raise ValueError("Fact %s unknown_id %r: write it as U-nn" % (self.id, self.unknown_id))
        if self.rung is not None and not _RUNG.match(_text("Fact rung", self.rung)):
            raise ValueError("Fact %s rung %r: write it as R1, R0b, R3b, R8k ..." % (self.id, self.rung))
        if not isinstance(self.evidence, (list, tuple)):
            raise TypeError("Fact %s evidence must be a tuple of strings, got %s"
                            % (self.id, type(self.evidence).__name__))
        evidence = tuple(_text("Fact %s evidence" % self.id, e) for e in self.evidence)
        object.__setattr__(self, "evidence", evidence)
        if self.serial is not None:
            check_serial(self.serial)
        check_firmware(self.firmware, "Fact %s firmware" % self.id)
        object.__setattr__(self, "recorded_at", utc_text(self.recorded_at, "Fact %s recorded_at" % self.id))
        _text("Fact %s note" % self.id, self.note, empty=True)

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        # pickle and copy rebuild the fact through the constructor, which validates it.
        return (Fact, (self.id, self.value, self.basis, self.unknown_id, self.rung, self.evidence, self.serial,
                       self.firmware, self.recorded_at, self.note))

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "value": thaw(self.value), "basis": self.basis, "unknown_id": self.unknown_id,
                "rung": self.rung, "evidence": list(self.evidence), "serial": self.serial,
                "firmware": self.firmware, "recorded_at": self.recorded_at, "note": self.note}

    @classmethod
    def from_json(cls, data: object) -> Fact:
        d = _keys("Fact", data, _FACT_KEYS)
        evidence = d["evidence"]
        if not isinstance(evidence, list):
            raise TypeError("Fact evidence must be a JSON array, got %s" % type(evidence).__name__)
        return cls(id=d["id"], value=d["value"], basis=d["basis"], unknown_id=d["unknown_id"], rung=d["rung"],
                   evidence=tuple(evidence), serial=d["serial"], firmware=d["firmware"],
                   recorded_at=d["recorded_at"], note=d["note"])


# ---------------------------------------------------------------------------
# Fingerprints (U-02; red-team FW-16, C-07)
# ---------------------------------------------------------------------------

FINGERPRINT_FACT_IDS: tuple[str, ...] = (
    "link.terminator",
    "fingerprint.unknown_command",
    "fingerprint.status_reply",
    "fingerprint.cage_status",
    "fingerprint.index_reply",
)
"""The behaviours that identify a firmware build: how commands are framed, what an
unknown command gets, the status reply, the shape of ``CAGE STATUS`` and of
``cfg read index`` (U-02, U-03, U-04)."""

_CAGE = re.compile(r"CAGE STATUS: ([A-Z]+)\n?\Z")


@dataclass(frozen=True)
class Fingerprint:
    """What one firmware version family is expected to do, part by part, and where that comes from."""

    version_family: str
    source: str
    expected: Mapping[str, frozenset[str]]

    def accepts(self, fact_id: str, value: object) -> bool:
        """Whether ``value``, observed for ``fact_id``, is what this family does."""
        allowed = self.expected.get(fact_id)
        if allowed is None:
            raise KeyError("%s is not a part of the %s fingerprint" % (fact_id, self.version_family))
        return type(value) is str and value in allowed

    def describe(self, fact_id: str) -> str:
        return " or ".join(repr(v) for v in sorted(self.expected[fact_id]))


FINGERPRINTS: Mapping[str, Fingerprint] = MappingProxyType({
    "01.02.00": Fingerprint(
        version_family="01.02.00",
        source=(
            "docs/protocol/09-firmware-01.02.00.md (static analysis of the GFW 01.02.00 image): How a command is "
            "parsed (the name is compared exactly, so INFO with a terminator is unknown, and unknown commands get "
            "no reply); Direct replies to the requester (the 48-byte GLOBAL STATUS blob; CAGE STATUS from "
            "snprintf(reply, 20, ...), so CLOSED loses its newline); Free-form text on slot 0 (cfg read index : %d)"
        ),
        expected=MappingProxyType({
            "link.terminator": frozenset({"none"}),
            "fingerprint.unknown_command": frozenset({"silent"}),
            "fingerprint.status_reply": frozenset({"blob48"}),
            "fingerprint.cage_status": frozenset({"CAGE STATUS: CLOSED", "CAGE STATUS: OPEN\n"}),
            "fingerprint.index_reply": frozenset({"cfg read index : <n>"}),
        }),
    ),
})
"""Expected fingerprints per version family. Only 01.02.00 has one; on any other
version (branch B3) rung R1 records a new baseline instead (design 11.5)."""

STATIC_01_02_00_RULES: Mapping[str, str] = MappingProxyType({
    "slot_routing": "slot routing: INFO/ERR/WARN lines on the 2nd connection, position frames on the 3rd, direct "
                    "replies to the requester",
    "info0_acks": "the INFO;0 acknowledgements and the expected-reply table",
    "upload_mode": "upload-mode semantics: no timeout or abort path, every connection's bytes stored as program data",
    "unknown_commands_silent": "silent handling of unknown commands",
    "read_only_allowlist": "the read-only allowlist of the probe (handlers disassembled in 01.02.00)",
})
"""Rules derived from static analysis of 01.02.00 that are 'unverified for this version' on any other one (11.5)."""


def _comparable(fact_id: str, value: object) -> object:
    """Observations with the same key are comparable; a cage that is OPEN once and CLOSED later is not a change."""
    if fact_id == "fingerprint.cage_status" and type(value) is str:
        m = _CAGE.match(value)
        return m.group(1) if m else None
    return None


@dataclass(frozen=True)
class Contradiction:
    """One fingerprint observation the version family (or the B3 baseline) does not accept."""

    fact_id: str
    expected: str
    observed: object
    batch: int
    recorded_at: str

    def describe(self) -> str:
        return "%s observed %r at %s; expected %s" % (self.fact_id, thaw(self.observed), self.recorded_at,
                                                       self.expected)


FingerprintState = Literal["matches", "differs", "unverified", "baseline-recorded", "no-model"]


@dataclass(frozen=True)
class FingerprintStatus:
    """Whether this robot behaves like its version family (design 3.1; machine screen "fingerprint: ...").

    ``state``: ``matches`` (01.02.00, every part observed and accepted),
    ``baseline-recorded`` (a newer version: every part observed and recorded as
    its baseline, nothing contradicts it), ``differs`` (a contradiction since the
    firmware was last seen to change), ``unverified`` (parts not observed yet),
    ``no-model`` (firmware unknown, or older than 01.02.00).
    """

    family: str | None
    state: FingerprintState
    missing: tuple[str, ...]
    contradictions: tuple[Contradiction, ...]
    baseline: Mapping[str, object]
    reason: str


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Record:
    batch: int
    fact: Fact

    def to_json(self) -> dict[str, Any]:
        return {"batch": self.batch, "fact": self.fact.to_json()}


@dataclass(frozen=True)
class _Identity:
    """A session that saw the robot on ``firmware`` for the first time since a different one."""

    batch: int
    firmware: str
    at: str

    def to_json(self) -> dict[str, Any]:
        return {"batch": self.batch, "firmware": self.firmware, "at": self.at}


def _batch(value: object, what: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("%s: batch must be a whole number >= 1, got %r" % (what, value))
    return value


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _fsync_dir(folder: Path) -> None:
    if os.name != "posix":
        return  # Windows has no directory handles to fsync; the rename itself is atomic there
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class FactStore:
    """The facts of one robot, read for the serial and firmware of this session. Use :meth:`open`."""

    def __init__(self, *, path: Path, serial: str, firmware: str | None, records: list[_Record],
                 identities: list[_Identity], loaded: bytes | None, clock: Callable[[], datetime]) -> None:
        self._path = path
        self._serial = serial
        self._firmware = firmware
        self._records = records
        self._identities = identities
        self._loaded_sha = _sha(loaded)
        self._clock = clock
        self._lock = threading.Lock()

    # -- opening --------------------------------------------------------------

    @classmethod
    def open(cls, root: str | os.PathLike[str], serial: str, firmware: str | None, *,
             clock: Callable[[], datetime] | None = None) -> FactStore:
        """The store of robot ``serial`` under ``root``, for a session on ``firmware`` (None: unknown).

        A firmware other than the one the store last saw is recorded at once:
        observed facts from before it stay stale from then on.
        """
        serial = check_serial(serial)
        firmware = check_firmware(firmware)
        path = Path(root) / "robots" / serial / FACTS_FILE
        loaded, records, identities = _load(path, serial)
        store = cls(path=path, serial=serial, firmware=firmware, records=records, identities=identities,
                    loaded=loaded, clock=clock or (lambda: datetime.now(timezone.utc)))
        if firmware is not None and (not identities or identities[-1].firmware != firmware):
            with store._lock:
                entry = _Identity(store._next_batch(), firmware, utc_text(store._clock(), "clock"))
                store._write(store._records, store._identities + [entry])
                store._identities = store._identities + [entry]
            if len(store._identities) > 1:
                # The serial is private (design 3.1): log lines name the firmware only.
                log.warning("firmware %s seen on this robot (last seen: %s); its observed facts from before are stale",
                            firmware, store._identities[-2].firmware)
        return store

    @property
    def serial(self) -> str:
        return self._serial

    @property
    def firmware(self) -> str | None:
        return self._firmware

    @property
    def path(self) -> Path:
        return self._path

    # -- writing --------------------------------------------------------------

    def record(self, fact: Fact) -> None:
        """Record one fact (it supersedes an earlier one with the same id)."""
        self.record_many([fact])

    def record_many(self, facts: Iterable[Fact]) -> None:
        """Record facts as one batch, all or nothing (one write)."""
        batch_facts = list(facts)
        if not batch_facts:
            raise ValueError("nothing to record")
        seen: set[str] = set()
        for fact in batch_facts:
            if not isinstance(fact, Fact):
                raise TypeError("record takes Fact values, got %s" % type(fact).__name__)
            if fact.id in seen:
                raise ValueError("fact %s appears twice in one batch: which one holds is ambiguous" % fact.id)
            seen.add(fact.id)
            self._check_binding(fact)
        with self._lock:
            batch = self._next_batch()
            records = self._records + [_Record(batch, f) for f in batch_facts]
            self._write(records, self._identities)
            self._records = records

    def record_measurement(self, fact_id: str, value: float | int | str, unit: str | None,
                           tolerance: float | int | None, operator: str, at: str | datetime, *, rung: str,
                           unknown_id: str | None = None) -> Fact:
        """Record a hand measurement typed in through K9 as a ``robot-measured`` fact (red-team C-13).

        The value is checked like the interface's :class:`goliath_run.types.EnterMeasurement`:
        a number needs its unit and tolerance, an answer that is not a number
        (a choice, text, ``unknown``) has neither. ``operator`` is who measured,
        ``at`` when (UTC), ``rung`` the rung whose procedure it belongs to
        (``R0b`` for field guide 04); none of them is defaulted. The fact's value
        is ``{"value", "unit", "tolerance"}``; who measured and when are its
        evidence, so a gate snapshot (which carries values) names nobody. It is
        bound to the serial only.
        """
        entry = EnterMeasurement(fact_id=fact_id, value=value, unit=unit, tolerance=tolerance, operator=operator)
        recorded_at = utc_text(at, "measurement time")
        if not _RUNG.match(_text("rung", rung)):
            raise ValueError("rung %r: write it as R0b, R2, R3 ..." % (rung,))
        fact = Fact(
            id=entry.fact_id,
            value={"value": entry.value, "unit": entry.unit, "tolerance": entry.tolerance},
            basis="robot-measured", unknown_id=unknown_id, rung=rung,
            evidence=("typed measurement entry K9 by %s at %s" % (entry.operator, recorded_at),),
            serial=self._serial, firmware=None, recorded_at=recorded_at,
            note="measured by hand on the robot; unit %s, tolerance %s" % (entry.unit, entry.tolerance),
        )
        self.record(fact)
        return fact

    def _check_binding(self, fact: Fact) -> None:
        if fact.serial != self._serial:
            raise FactBindingError("fact %s belongs to serial %r, not to this store's robot %r"
                                   % (fact.id, fact.serial, self._serial))
        if fact.basis == "observed":
            if self._firmware is None:
                raise FactBindingError("fact %s: the firmware is unknown in this session, so an observed fact "
                                       "cannot be bound to it" % fact.id)
            if fact.firmware != self._firmware:
                raise FactBindingError("fact %s was observed on firmware %s; this session's firmware is %s"
                                       % (fact.id, fact.firmware, self._firmware))
        elif fact.basis == "firmware-static":
            if fact.firmware != STATIC_FAMILY:
                raise FactBindingError("fact %s is derived from static analysis, which exists only for %s; it says "
                                       "firmware %s" % (fact.id, STATIC_FAMILY, fact.firmware))
        elif fact.firmware not in (None, self._firmware):
            raise FactBindingError("fact %s names firmware %s; this session's firmware is %s"
                                   % (fact.id, fact.firmware, self._firmware))
        recorded = _moment(fact.recorded_at)
        if fact.firmware is not None and fact.basis != "firmware-static":
            # A fact observed before the robot was last seen on another firmware belongs to an
            # earlier firmware era, even if it names the firmware the robot runs again now: an old
            # report imported late must not revive what the reflash made stale.
            later = [i for i in self._identities if i.firmware != fact.firmware and _moment(i.at) > recorded]
            if later:
                raise FactBindingError(
                    "fact %s was recorded at %s on firmware %s, but this robot was seen on firmware %s at %s, "
                    "after that: it is from before a firmware change and cannot be recorded; repeat rung %s"
                    % (fact.id, fact.recorded_at, fact.firmware, later[-1].firmware, later[-1].at,
                       fact.rung or "that recorded it"))
        latest = self._latest().get(fact.id)
        if latest is not None and recorded < _moment(latest.fact.recorded_at):
            raise FactBindingError(
                "fact %s was recorded at %s, which is older than the %s recorded at %s already in this store: "
                "recording it would replace a newer value with an older one (import reports and enter "
                "measurements in the order they were made)"
                % (fact.id, fact.recorded_at, fact.id, latest.fact.recorded_at))

    def _next_batch(self) -> int:
        batches = [r.batch for r in self._records] + [i.batch for i in self._identities]
        return max(batches, default=0) + 1

    def _write(self, records: list[_Record], identities: list[_Identity]) -> None:
        payload = canonical_json({"schema": SCHEMA, "serial": self._serial,
                                  "records": [r.to_json() for r in records],
                                  "identities": [i.to_json() for i in identities]})
        try:
            on_disk: bytes | None = self._path.read_bytes()
        except FileNotFoundError:
            on_disk = None
        if _sha(on_disk) != self._loaded_sha:
            raise FactStoreConflictError("%s changed on disk since this store read it (another session wrote "
                                         "to it); nothing was written: open the store again" % self._path)
        folder = self._path.parent
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".facts-", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        _fsync_dir(folder)
        self._loaded_sha = _sha(payload)

    # -- reading --------------------------------------------------------------

    def _latest(self) -> dict[str, _Record]:
        latest: dict[str, _Record] = {}
        for record in self._records:
            latest[record.fact.id] = record
        return latest

    def _stale_reason(self, record: _Record, contradiction: Contradiction | None) -> str | None:
        fact = record.fact
        if fact.serial != self._serial:
            return "recorded for another robot (serial %r), not this one" % (fact.serial,)
        if fact.basis == "firmware-static":
            return None  # about the 01.02.00 image, not this robot's firmware: see _unverified_reason
        if fact.basis == "observed" or fact.firmware is not None:
            # Bound to the firmware it was recorded on (an observed fact always is).
            if self._firmware is None:
                return "firmware unknown in this session: this fact holds only for firmware %s" % fact.firmware
            if fact.firmware != self._firmware:
                return "firmware changed from %s to %s" % (fact.firmware, self._firmware)
            later = [i for i in self._identities if i.batch > record.batch]
            if later:
                return "firmware changed from %s to %s after this was recorded (now %s again)" % (
                    fact.firmware, later[0].firmware, self._firmware)
        if fact.basis == "observed" and contradiction is not None and record.batch < contradiction.batch:
            return "fingerprint behaviour contradicted since it was recorded: %s" % contradiction.describe()
        return None

    def _first_contradiction(self) -> Contradiction | None:
        status = self.fingerprint_status()
        return min(status.contradictions, key=lambda c: c.batch, default=None)

    def _unverified_reason(self, record: _Record) -> str | None:
        fact = record.fact
        if fact.basis != "firmware-static" or self._firmware == STATIC_FAMILY:
            return None
        return "unverified for this version: derived from static analysis of %s, and this robot runs %s" % (
            STATIC_FAMILY, self._firmware or "an unknown firmware")

    def _classified(self) -> list[tuple[_Record, str | None, str | None]]:
        contradiction = self._first_contradiction()
        return [(r, self._stale_reason(r, contradiction), self._unverified_reason(r))
                for r in self._latest().values()]

    def get(self, fact_id: str) -> Fact | None:
        """The fact in force for ``fact_id``: the latest one, unless it is stale or unverified (then None)."""
        record = self._latest().get(fact_id)
        if record is None:
            return None
        contradiction = self._first_contradiction()
        if self._stale_reason(record, contradiction) or self._unverified_reason(record):
            return None
        return record.fact

    def current(self) -> Mapping[str, Fact]:
        """Every fact in force, by id."""
        return MappingProxyType({r.fact.id: r.fact for r, stale, unverified in self._classified()
                                 if stale is None and unverified is None})

    def stale(self) -> list[Fact]:
        """The facts that are stale: shown with the reason (:meth:`why_stale`), never used."""
        return [r.fact for r, stale, _ in self._classified() if stale is not None]

    def unverified(self) -> list[Fact]:
        """Facts derived from 01.02.00 static analysis while this robot runs another version (11.5)."""
        return [r.fact for r, stale, unverified in self._classified() if stale is None and unverified is not None]

    def why_stale(self, fact_id: str) -> str:
        record = self._latest().get(fact_id)
        reason = None if record is None else self._stale_reason(record, self._first_contradiction())
        if reason is None:
            raise ValueError("fact %s is not stale in this store" % fact_id)
        return reason

    def why_unverified(self, fact_id: str) -> str:
        record = self._latest().get(fact_id)
        reason = None if record is None else self._unverified_reason(record)
        if reason is None:
            raise ValueError("fact %s is not unverified in this store" % fact_id)
        return reason

    def firmware_history(self) -> list[tuple[str, str]]:
        """``(firmware, first seen at)`` for every change of firmware this store has seen, oldest first."""
        return [(i.firmware, i.at) for i in self._identities]

    def stale_summary(self) -> str | None:
        """One line for the red disclosure, such as "firmware changed from X to Y: 14 facts stale; repeat rungs R1"."""
        stale = [(r, reason) for r, reason, _ in self._classified() if reason is not None]
        if not stale:
            return None
        rungs = sorted({r.fact.rung for r, _ in stale if r.fact.rung}, key=_rung_order)
        changed = [r.fact.firmware for r, _ in stale if r.fact.firmware and r.fact.firmware != self._firmware]
        if changed and self._firmware is not None:
            cause = "firmware changed from %s to %s" % (sorted(set(changed))[0], self._firmware)
        else:
            cause = stale[0][1]
        text = "%s: %d facts stale" % (cause, len(stale))
        if rungs:
            text += "; repeat rungs %s" % ", ".join(rungs)
        return text

    def sha256(self) -> str:
        """SHA-256 of the facts in force (canonical JSON): what a session binding names."""
        return canonical_sha256({"serial": self._serial, "firmware": self._firmware,
                                 "facts": {fid: f.to_json() for fid, f in sorted(self.current().items())}})

    # -- fingerprint ----------------------------------------------------------

    def fingerprint_status(self) -> FingerprintStatus:
        """Whether this robot behaves like its version family, from the observations since the last firmware change.

        Every observation can contradict the fingerprint, but only observations
        recorded through a rung's go criteria (``rung`` set) complete it:
        uncertain evidence may restrict, never permit (design 3.1).
        """
        firmware = self._firmware
        if firmware is None:
            return FingerprintStatus(None, "no-model", FINGERPRINT_FACT_IDS, (), MappingProxyType({}),
                                     "firmware unknown: no fingerprint can be compared")
        if firmware_tuple(firmware) < firmware_tuple(STATIC_FAMILY):
            return FingerprintStatus(None, "no-model", FINGERPRINT_FACT_IDS, (), MappingProxyType({}),
                                     "firmware %s is older than %s: no model of its behaviour (branch B4)"
                                     % (firmware, STATIC_FAMILY))
        since = self._identities[-1].batch if self._identities else 0
        observations = [r for r in self._records
                        if r.fact.id in FINGERPRINT_FACT_IDS and r.fact.basis == "observed"
                        and r.fact.serial == self._serial and r.fact.firmware == firmware and r.batch >= since]
        latest: dict[str, object] = {}
        contradictions: list[Contradiction] = []
        family = FINGERPRINTS.get(firmware)
        if family is not None:
            for r in observations:
                latest[r.fact.id] = r.fact.value
                if not family.accepts(r.fact.id, r.fact.value):
                    contradictions.append(Contradiction(r.fact.id, family.describe(r.fact.id), r.fact.value,
                                                        r.batch, r.fact.recorded_at))
            baseline: dict[str, object] = dict(latest)
            name = family.version_family
        else:
            baselines: dict[tuple[str, object], _Record] = {}
            baseline = {}
            for r in observations:
                key = (r.fact.id, _comparable(r.fact.id, r.fact.value))
                first = baselines.get(key)
                if first is None:
                    baselines[key] = r
                    baseline.setdefault(r.fact.id, r.fact.value)
                elif r.fact.value != first.fact.value:
                    expected = "%r (the baseline recorded at %s)" % (thaw(first.fact.value), first.fact.recorded_at)
                    contradictions.append(Contradiction(r.fact.id, expected, r.fact.value, r.batch,
                                                        r.fact.recorded_at))
                latest[r.fact.id] = r.fact.value
            name = "baseline:%s" % firmware
        vouched = {r.fact.id for r in observations if r.fact.rung is not None}
        missing = tuple(fid for fid in FINGERPRINT_FACT_IDS if fid not in vouched)
        if contradictions:
            state: FingerprintState = "differs"
            reason = "the fingerprint differs: " + "; ".join(c.describe() for c in contradictions)
        elif missing:
            state = "unverified"
            reason = "not observed through a rung yet: " + ", ".join(missing)
        elif family is not None:
            state = "matches"
            reason = "every part matches the %s image" % family.version_family
        else:
            state = "baseline-recorded"
            reason = "a new baseline for %s is recorded and nothing since contradicts it" % firmware
        return FingerprintStatus(name, state, missing, tuple(contradictions), MappingProxyType(baseline), reason)


def _rung_order(rung: str) -> tuple[int, str]:
    m = re.match(r"R(\d+)(.*)", rung)
    return (int(m.group(1)), m.group(2)) if m else (999, rung)


def _load(path: Path, serial: str) -> tuple[bytes | None, list[_Record], list[_Identity]]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None, [], []
    except OSError as exc:
        raise FactStoreError("cannot read the facts file %s: %s" % (path, exc)) from exc
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FactStoreError("the facts file %s is not readable JSON (%s); nothing is assumed: restore it or move "
                             "it aside, and the facts must be recorded again" % (path, exc)) from exc
    if not isinstance(doc, dict):
        raise FactStoreError("the facts file %s does not hold a JSON object" % path)
    if doc.get("schema") != SCHEMA:
        raise FactStoreError("the facts file %s has schema %r; this version of goliath-run reads %s only"
                             % (path, doc.get("schema"), SCHEMA))
    try:
        _keys("facts file %s" % path, doc, frozenset({"schema", "serial", "records", "identities"}))
        if doc["serial"] != serial:
            raise ValueError("it belongs to serial %r, not %r" % (doc["serial"], serial))
        if not isinstance(doc["records"], list) or not isinstance(doc["identities"], list):
            raise TypeError("records and identities must be JSON arrays")
        records = []
        for i, item in enumerate(doc["records"]):
            d = _keys("record %d" % i, item, frozenset({"batch", "fact"}))
            records.append(_Record(_batch(d["batch"], "record %d" % i), Fact.from_json(d["fact"])))
        identities = []
        for i, item in enumerate(doc["identities"]):
            d = _keys("identity %d" % i, item, frozenset({"batch", "firmware", "at"}))
            fw = check_firmware(d["firmware"], "identity %d firmware" % i)
            if fw is None:
                raise ValueError("identity %d has no firmware" % i)
            identities.append(_Identity(_batch(d["batch"], "identity %d" % i), fw, utc_text(d["at"], "identity at")))
        order = [r.batch for r in records]
        if order != sorted(order) or [i.batch for i in identities] != sorted(i.batch for i in identities):
            raise ValueError("batches are out of order")
    except (TypeError, ValueError, KeyError) as exc:
        raise FactStoreError("the facts file %s is not valid (%s); nothing is assumed" % (path, exc)) from exc
    return data, records, identities


# ---------------------------------------------------------------------------
# Rung R1: the read-only probe's report (design 11.2 R1; red-team FW-08, FW-10, C-13)
# ---------------------------------------------------------------------------

R1_FACT_IDS: tuple[str, ...] = (
    "firmware.version",
    "link.terminator",
    "fingerprint.unknown_command",
    "fingerprint.status_reply",
    "fingerprint.cage_status",
    "fingerprint.index_reply",
    "replay.fresh_boot",
    "status_blob.baseline.idle",
    "sanity.baseline.idle",
    "index.baseline",
    "frames.at_idle",
    "rung.r1.go",
)
"""The facts rung R1 can record from one probe report (design 11.2), plus ``rung.r1.go``."""

ABSENT_UNTIL_R1B: Mapping[str, str] = MappingProxyType({
    "slot.release_wait_s": (
        "the slot-release sweep (section 'slot_release' of the probe report) is rung R1b's; the probe of this "
        "checkout does not run it, so the runner keeps its default slot-release wait (U-05)"
    ),
    "link.keepalive_ok": (
        "the idle-keepalive session (section 'keepalive' of the probe report) is rung R1b's; the probe of this "
        "checkout does not run it, so keepalive timing stays disclosed on the setup screens (U-38)"
    ),
})
"""Facts whose sections the probe report does not carry yet (red-team FW-10, C-13), with why."""

_UNKNOWNS: Mapping[str, str | None] = MappingProxyType({
    "firmware.version": "U-01",
    "link.terminator": "U-03",
    "fingerprint.unknown_command": "U-02",
    "fingerprint.status_reply": "U-02",
    "fingerprint.cage_status": "U-02",
    "fingerprint.index_reply": "U-02",
    "replay.fresh_boot": "U-06",
    "status_blob.baseline.idle": "U-07",
    "sanity.baseline.idle": "U-08",
    "index.baseline": "U-09",
    "frames.at_idle": "U-52",
    "rung.r1.go": None,
})

_REPORT_TOOL = "goliath_link.probe"
_REPORT_FORMAT = 2
_STAMP = re.compile(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z\Z")
_INDEX = re.compile(r"cfg read index\s*:\s*(-?\d+)\Z")


@dataclass(frozen=True)
class ProbeImport:
    """What one probe report gives rung R1: the facts, the robot they bind to, the branch and what is missing.

    ``branch`` is the design's R1 branch (11.2): ``B1`` (01.02.00, the
    fingerprint matches), ``B2`` (01.02.00, it differs), ``B3`` (a newer version:
    a new baseline), ``B4`` (older than 01.02.00), or ``unverified`` (01.02.00
    with fingerprint parts not observed). ``notes`` say what was not recorded and
    why; ``absent`` lists the facts the report cannot give.
    """

    facts: tuple[Fact, ...]
    serial: str
    firmware: str
    branch: str
    notes: tuple[str, ...]
    absent: Mapping[str, str]
    report_sha256: str


def read_probe_report(path: str | os.PathLike[str], *, warnings_acknowledged_by: str | None = None) -> ProbeImport:
    """Read a JSON report of ``python -m goliath_link.probe`` as rung R1 facts.

    Refused with :class:`ProbeReportError` unless the run is a go (design 11.2
    R1): the probe's own report of format 2, not interrupted, complete, no slot
    routing errors (branch B5), no errors, a firmware version, a serial, a chosen
    terminator. A run that finished with warnings (exit code 3) is a go only when
    a named person has read every warning: pass ``warnings_acknowledged_by``; the
    warnings and the name go into the facts' notes.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ProbeReportError("cannot read the probe report %s: %s" % (path, exc)) from exc
    sha = hashlib.sha256(data).hexdigest()
    try:
        report = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProbeReportError("the probe report %s is not JSON (%s)" % (path, exc)) from exc
    if not isinstance(report, dict):
        raise ProbeReportError("the probe report %s is not a JSON object" % path)
    return _ProbeReader(path, sha, report, warnings_acknowledged_by).read()


def import_probe_report(path: str | os.PathLike[str], *, warnings_acknowledged_by: str | None = None) -> list[Fact]:
    """:func:`read_probe_report`'s facts; every note and absent fact is logged as a warning (it is never silent)."""
    imported = read_probe_report(path, warnings_acknowledged_by=warnings_acknowledged_by)
    name = Path(path).name
    for note in imported.notes:
        log.warning("probe report %s: %s", name, note)
    for fact_id, why in imported.absent.items():
        log.warning("probe report %s: no fact %s: %s", name, fact_id, why)
    log.info("probe report %s: %d facts on firmware %s (branch %s)", name, len(imported.facts),
             imported.firmware, imported.branch)
    return list(imported.facts)


def _walk(node: object) -> Iterable[Mapping[str, Any]]:
    """Every JSON object in the report, depth first."""
    if isinstance(node, Mapping):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


class _ProbeReader:
    def __init__(self, path: Path, sha: str, report: dict[str, Any], acknowledged_by: str | None) -> None:
        self.path = path
        self.sha = sha
        self.r = report
        self.acknowledged_by = acknowledged_by
        self.notes: list[str] = []
        self.absent: dict[str, str] = dict(ABSENT_UNTIL_R1B)
        if acknowledged_by is not None:
            _text("warnings_acknowledged_by", acknowledged_by)

    def fail(self, why: str) -> ProbeReportError:
        return ProbeReportError("probe report %s cannot be imported as rung R1 facts: %s" % (self.path.name, why))

    # -- the go criteria --------------------------------------------------------

    def check_go(self) -> tuple[str, str, str, int]:
        r = self.r
        if r.get("tool") != _REPORT_TOOL:
            raise self.fail("it was not written by %s (tool %r)" % (_REPORT_TOOL, r.get("tool")))
        if r.get("report_format") != _REPORT_FORMAT:
            raise self.fail("report_format %r; this version of goliath-run reads format %d"
                            % (r.get("report_format"), _REPORT_FORMAT))
        for section, fact_id in (("slot_release", "slot.release_wait_s"), ("keepalive", "link.keepalive_ok")):
            if section in r:
                raise self.fail(
                    "it carries a section %r, whose format this version of goliath-run does not know (it arrives "
                    "with rung R1b's probe; fact %s); nothing is imported rather than guessed: update goliath-run"
                    % (section, fact_id))
        if r.get("interrupted") is not False:
            raise self.fail("the probe was interrupted (interrupted=%r): run it again" % r.get("interrupted"))
        if r.get("routing"):
            raise self.fail(
                "branch B5, slot routing errors: %s. Another client may hold slots, or the robot was left in "
                "upload mode: power-cycle the robot, close every other app, and run the probe again"
                % "; ".join(str(e) for e in (r.get("errors") or r["routing"])))
        errors = r.get("errors")
        if not isinstance(errors, list):
            raise self.fail("it has no list of errors")
        if errors or r.get("exit_code") == 1:
            raise self.fail("the probe reported errors (exit code %r): %s"
                            % (r.get("exit_code"), "; ".join(str(e) for e in errors) or "none listed"))
        if r.get("completed") is not True:
            raise self.fail("the probe did not complete (completed=%r)" % r.get("completed"))
        exit_code = r.get("exit_code")
        if exit_code not in (0, 3):
            raise self.fail("exit code %r is neither 0 nor 3" % (exit_code,))
        warnings = r.get("warnings")
        if not isinstance(warnings, list):
            raise self.fail("it has no list of warnings")
        if warnings and self.acknowledged_by is None:
            raise self.fail(
                "the probe finished with %d warning(s): %s. Rung R1 is a go with warnings only when every one is "
                "understood: name the person who read them (warnings_acknowledged_by)"
                % (len(warnings), " | ".join(str(w) for w in warnings)))
        firmware = r.get("firmware_version")
        try:
            firmware = check_firmware(firmware, "firmware_version")
        except (TypeError, ValueError) as exc:
            raise self.fail("no usable firmware version (%s): without one nothing can be bound (branch B4 or a "
                            "failed identity read)" % exc) from None
        if firmware is None:
            raise self.fail("no firmware version was read (branch B4: no model; or the identity line was lost)")
        if r.get("chosen_terminator") not in ("none", "lf", "crlf"):
            raise self.fail("no terminator was chosen (chosen_terminator=%r)" % r.get("chosen_terminator"))
        m = _STAMP.match(str(r.get("started_utc")))
        if m is None:
            raise self.fail("started_utc %r is not a probe time stamp such as 20261104T090000Z"
                            % r.get("started_utc"))
        recorded_at = "%s-%s-%sT%s:%s:%sZ" % m.groups()
        try:
            utc_text(recorded_at, "started_utc")
        except ValueError as exc:
            raise self.fail("started_utc %r is not a valid time: %s" % (r.get("started_utc"), exc)) from None
        return firmware, recorded_at, self.serial(firmware), exit_code

    def serial(self, firmware: str) -> str:
        """The serial every INFO;34 identity line names; each must also name ``firmware``."""
        serials: set[str] = set()
        versions: set[str] = set()
        for node in _walk(self.r):
            if node.get("type") == "text" and node.get("kind") == "INFO" and node.get("code") == 34:
                params = node.get("params")
                if isinstance(params, list) and len(params) >= 4 and isinstance(params[3], str):
                    serials.add(params[3].strip())
                if isinstance(params, list) and params and isinstance(params[0], str):
                    version = parse_firmware_version(params[0])
                    if version is not None:
                        versions.add(format_version(version))
        if len(versions) > 1:
            raise self.fail("the identity lines name different firmware versions (%s): two robots, a reflash "
                            "during the run, or a garbled line; facts cannot be bound to either: run the probe "
                            "again with only this robot on the network" % ", ".join(sorted(versions)))
        if versions and versions != {firmware}:
            raise self.fail("firmware_version %s disagrees with the identity lines, which name %s"
                            % (firmware, versions.pop()))
        if not serials:
            raise self.fail("no INFO;34 identity line with a serial number: facts cannot be bound to a robot")
        if len(serials) > 1:
            raise self.fail("the identity lines name %d different serial numbers: two robots, or a garbled line; "
                            "run the probe again with only this robot on the network" % len(serials))
        serial = serials.pop()
        try:
            return check_serial(serial)
        except ValueError as exc:
            raise self.fail(str(exc)) from None

    # -- the facts --------------------------------------------------------------

    def read(self) -> ProbeImport:
        firmware, recorded_at, serial, exit_code = self.check_go()
        # What the probe noted, and the lines it could not credit to a command, are carried
        # over: nothing in the report that a reader should see is dropped by the import.
        for note in self.r.get("notes") or []:
            self.notes.append("the probe noted: %s" % note)
        unattributed = self.r.get("unattributed") or []
        if unattributed:
            self.notes.append("%d message(s) the probe could not credit to any command (listed under "
                              "'unattributed' in the report, each with the reason); no fact rests on them"
                              % len(unattributed))
        evidence = self.evidence()
        go_note = "probe result %s, exit code %d" % (self.r.get("result"), exit_code)
        if self.r["warnings"]:
            go_note += "; %d warning(s) read and understood by %s: %s" % (
                len(self.r["warnings"]), self.acknowledged_by, " | ".join(str(w) for w in self.r["warnings"]))
        facts: list[Fact] = []

        def add(fact_id: str, value: object, note: str) -> None:
            facts.append(Fact(id=fact_id, value=value, basis="observed", unknown_id=_UNKNOWNS[fact_id], rung="R1",
                              evidence=evidence, serial=serial, firmware=firmware, recorded_at=recorded_at,
                              note=note))

        add("firmware.version", firmware, "read from %s" % self.r.get("firmware_version_source"))
        self.terminator(add)
        self.unknown_command(add)
        self.status(add)
        self.cage(add)
        self.index(add)
        self.sanity(add)
        self.replay(add)
        self.frames(add)
        add("rung.r1.go", True, go_note)
        branch = self.branch(firmware, facts)
        return ProbeImport(tuple(facts), serial, firmware, branch, tuple(self.notes),
                           MappingProxyType(dict(self.absent)), self.sha)

    def evidence(self) -> tuple[str, ...]:
        out = ["%s sha256=%s (probe report)" % (self.path.name, self.sha)]
        # The probe may have run on Windows or Linux: take the name after either separator.
        capture = re.split(r"[\\/]", str(self.r.get("capture_file") or ""))[-1]
        if not capture:
            out.append("raw capture: the report names none")
            self.notes.append("the report names no raw capture file: the facts rest on the JSON report alone")
            return tuple(out)
        capture_path = self.path.parent / capture
        try:
            out.append("%s sha256=%s (raw capture)" % (capture, hashlib.sha256(capture_path.read_bytes()).hexdigest()))
        except FileNotFoundError:
            out.append("%s (raw capture) not found next to the report" % capture)
            self.notes.append("raw capture %s not found next to the report: the facts rest on the JSON report alone"
                              % capture)
        return tuple(out)

    def session_events(self, name: str) -> list[Mapping[str, Any]]:
        for session in self.r.get("sessions") or []:
            if isinstance(session, Mapping) and session.get("name") == name:
                return [e for e in session.get("events") or [] if isinstance(e, Mapping)]
        return []

    def sweep_part(self, command: str, part: str) -> Mapping[str, Any] | None:
        for entry in self.r.get("sweep") or []:
            if isinstance(entry, Mapping) and entry.get("command") == command:
                for reply in entry.get("replies") or []:
                    if isinstance(reply, Mapping) and reply.get("part") == part:
                        return reply
        return None

    def terminator(self, add: Callable[[str, object, str], None]) -> None:
        chosen = self.r["chosen_terminator"]
        tests = [e for e in self.r.get("terminator_tests") or [] if isinstance(e, Mapping)]
        entry = next((e for e in tests if e.get("mode") == chosen), None)
        if entry is None or entry.get("uncertain"):
            why = "its verdict is uncertain: %s" % entry.get("uncertain") if entry else "no test entry for it"
            self.absent["link.terminator"] = "terminator %r was chosen, but %s" % (chosen, why)
            self.notes.append("link.terminator not recorded: " + self.absent["link.terminator"])
            return
        answered = [e.get("mode") for e in tests if e.get("responded")]
        add("link.terminator", chosen, "variants answered: %s; tested: %s"
            % (", ".join(map(str, answered)) or "none", ", ".join(str(e.get("mode")) for e in tests)))

    def unknown_command(self, add: Callable[[str, object, str], None]) -> None:
        tests = [e for e in self.r.get("terminator_tests") or [] if isinstance(e, Mapping)]
        unanswered = [e for e in tests if e.get("identity_after_send") is not None and e.get("responded") is False]
        # A variant whose verdict the probe marked uncertain, or whose session failed, shows
        # nothing certain about unknown commands: it may restrict, never permit (design 3.1).
        doubtful = [e for e in unanswered if e.get("uncertain") or e.get("error")]
        unanswered = [e for e in unanswered if e not in doubtful]
        if not unanswered:
            why = "no terminator variant went unanswered with a certain verdict, so what the robot does with an " \
                  "unknown command was not seen"
            if doubtful:
                why += " (%s: %s)" % (", ".join(repr(e.get("mode")) for e in doubtful),
                                      "; ".join(str(e.get("uncertain") or e.get("error")) for e in doubtful))
            self.absent["fingerprint.unknown_command"] = why
            self.notes.append("fingerprint.unknown_command not recorded: " + self.absent["fingerprint.unknown_command"])
            return
        replies = [e for t in unanswered for e in self.session_events(str(t.get("session")))
                   if e.get("type") == "text" and e.get("phase") in ("after INFO", "trailing")]
        value = "silent" if not replies else "reply:%s" % replies[0].get("raw")
        add("fingerprint.unknown_command", value,
            "after %s (unknown to firmware that compares names exactly): %s"
            % (", ".join(repr(t.get("sent")) for t in unanswered),
               "no reply" if not replies else ", ".join(repr(e.get("raw")) for e in replies)))

    def status(self, add: Callable[[str, object, str], None]) -> None:
        gs = self.r.get("global_status")
        if not isinstance(gs, Mapping) or not isinstance(gs.get("raw"), str):
            for fid in ("fingerprint.status_reply", "status_blob.baseline.idle"):
                self.absent[fid] = "no global-status reply was decoded"
                self.notes.append("%s not recorded: no global-status reply was decoded" % fid)
            return
        size = len(gs["raw"].split())
        kind = {("global_status_blob", 48): "blob48", ("global_status", 19): "gs19"}.get((gs.get("type"), size))
        shape = kind or "other:%s/%d bytes" % (gs.get("type"), size)
        add("fingerprint.status_reply", shape, "the reply to GET GLOBAL SYS STATUS: %s, %d bytes"
            % (gs.get("type"), size))
        baseline: dict[str, object] = {"kind": kind or "other", "raw": gs["raw"]}
        if gs.get("type") == "global_status_blob" and isinstance(gs.get("fields"), list):
            baseline["fields"] = list(gs["fields"])
        add("status_blob.baseline.idle", baseline,
            "the robot idle at R1; the meaning of the status bytes is an assumption (U-07): recorded raw and used "
            "only to restrict (a difference from the baseline blocks, a match permits nothing)")

    def cage(self, add: Callable[[str, object, str], None]) -> None:
        reply = self.sweep_part("GET SPINDLE CAGE STATUS", "cage")
        if reply is None or not isinstance(reply.get("raw"), str):
            self.absent["fingerprint.cage_status"] = "no CAGE STATUS reply"
            self.notes.append("fingerprint.cage_status not recorded: no CAGE STATUS reply")
            return
        value = reply["raw"] + ("\n" if reply.get("terminated") else "")
        add("fingerprint.cage_status", value,
            "the direct reply to GET SPINDLE CAGE STATUS; a trailing newline here stands for 'terminated' in the "
            "report (a newline or NUL padding followed); 01.02.00 sends CLOSED without one")

    def index(self, add: Callable[[str, object, str], None]) -> None:
        reply = self.sweep_part("READ INDEX CALIBRATION", "value")
        raw = reply.get("raw") if reply is not None else None
        if not isinstance(raw, str):
            for fid in ("fingerprint.index_reply", "index.baseline"):
                self.absent[fid] = "no reply to READ INDEX CALIBRATION"
                self.notes.append("%s not recorded: no reply to READ INDEX CALIBRATION" % fid)
            return
        add("fingerprint.index_reply", re.sub(r"-?\d+", "<n>", raw), "the reply to READ INDEX CALIBRATION: %r" % raw)
        m = _INDEX.match(raw)
        if m is None:
            self.absent["index.baseline"] = "the reply %r carries no number in the expected form" % raw
            self.notes.append("index.baseline not recorded: " + self.absent["index.baseline"])
            return
        add("index.baseline", int(m.group(1)),
            "read at R1; spec 04 gives 27000-30100 for writes only, so a range for reads is INFERRED (U-09): a "
            "change from this baseline or a value outside that range needs acknowledgement")

    def sanity(self, add: Callable[[str, object, str], None]) -> None:
        reply = self.sweep_part("PRINT SYS REPORT", "report")
        if reply is None or not isinstance(reply.get("params"), list) or not isinstance(reply.get("raw"), str):
            self.absent["sanity.baseline.idle"] = "no INFO;40 reply to PRINT SYS REPORT"
            self.notes.append("sanity.baseline.idle not recorded: no INFO;40 reply to PRINT SYS REPORT")
            return
        values = [str(p) for p in reply["params"]]
        if values and values[-1] == "END":
            values = values[:-1]
        add("sanity.baseline.idle", {"values": values, "raw": reply["raw"]},
            "INFO;40 with the robot idle at R1; the meaning of the %d values is unknown (U-08): any difference "
            "from this baseline in the same state blocks" % len(values))

    def replay(self, add: Callable[[str, object, str], None]) -> None:
        events = [e for e in self.r.get("passive") or [] if isinstance(e, Mapping)
                  and e.get("type") == "text" and e.get("kind") == "INFO" and isinstance(e.get("code"), int)]
        if not events:
            self.absent["replay.fresh_boot"] = ("no connect-time replay was seen, so the fresh-boot replay signature "
                                                "is unavailable as evidence of a power cycle (design 7.7)")
            self.notes.append("replay.fresh_boot not recorded: " + self.absent["replay.fresh_boot"])
            return
        codes = sorted({e["code"] for e in events})
        connections = sorted({str(e.get("conn")) for e in events})
        note = ("INFO codes the robot pushed when connections opened, in %d session(s), the robot freshly powered "
                "on for R1" % len({e.get("session") for e in events}))
        if 8 in codes:
            note += ("; it includes INFO;8 (a program was left on the robot), so INFO;8 cannot serve as evidence "
                     "about a restart on this robot (design 7.7)")
        add("replay.fresh_boot", {"codes": codes, "connections": connections}, note)

    def frames(self, add: Callable[[str, object, str], None]) -> None:
        unsolicited: list[Mapping[str, Any]] = []
        solicited = 0
        sessions = [s for s in self.r.get("sessions") or [] if isinstance(s, Mapping)]
        for session in sessions:
            for event in session.get("events") or []:
                if isinstance(event, Mapping) and event.get("type") == "position":
                    if event.get("reply_to") == "SEND TCP POS":
                        solicited += 1
                    else:
                        unsolicited.append(event)
        add("frames.at_idle",
            {"unsolicited": len(unsolicited), "solicited": solicited,
             "connections": sorted({str(e.get("conn")) for e in unsolicited}), "sessions": len(sessions)},
            "position frames seen while the robot was idle during the probe (U-52); not evidence that frames do or "
            "do not stream outside work, which is the R2 fact frames.stream_outside_work; restrict-only")

    def branch(self, firmware: str, facts: list[Fact]) -> str:
        version = firmware_tuple(firmware)
        if version < firmware_tuple(STATIC_FAMILY):
            self.notes.append("firmware %s is older than %s: branch B4, no model of its command set; probe only, "
                              "design review" % (firmware, STATIC_FAMILY))
            return "B4"
        if version > firmware_tuple(STATIC_FAMILY):
            self.notes.append("firmware %s is newer than %s: branch B3; these observations become its fingerprint "
                              "baseline, and nothing derived from %s static analysis is a fact on it"
                              % (firmware, STATIC_FAMILY, STATIC_FAMILY))
            return "B3"
        family = FINGERPRINTS[STATIC_FAMILY]
        seen = {f.id: f.value for f in facts if f.id in FINGERPRINT_FACT_IDS}
        differs = [fid for fid, value in seen.items() if not family.accepts(fid, value)]
        for fid in differs:
            self.notes.append("%s: observed %r, the %s image gives %s (branch B2: jobs refused, design review)"
                              % (fid, seen[fid], STATIC_FAMILY, family.describe(fid)))
        if differs:
            return "B2"
        if set(seen) != set(FINGERPRINT_FACT_IDS):
            self.notes.append("fingerprint parts not observed: %s; the fingerprint stays unverified"
                              % ", ".join(sorted(set(FINGERPRINT_FACT_IDS) - set(seen))))
            return "unverified"
        return "B1"

