# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The machine ledger: what the runner must remember about a robot across app restarts (unit R02).

The project design, sections 3.3, 7.5 and 7.7; red-team FW-07, C-08 and PHYS-02.
``GoliathClient`` keeps its upload hazard and its uploads-since-restart count
in memory only, so an app that crashed or was closed would forget that the
robot may be in upload mode, or running a job. The ledger keeps them on disk:

* ``upload_in_flight`` (job id, payload SHA-256): set by :meth:`Ledger.begin_upload`,
  which the driver calls from the upload's ``header-about-to-send`` stage, so it
  is on disk before any header byte is sent; cleared only by an end-of-upload
  signal plus ``INFO;23`` (:meth:`Ledger.end_upload`) or by a power-cycle
  clearing (:meth:`Ledger.attest_power_cycle`);
* ``hazards``: an upload cut short, or the robot silent after the first command
  ("suspect upload mode", red-team PHYS-02);
* ``pending_power_cycle`` reasons; ``uploads_since_attested_power_cycle``;
* ``session_active`` (pid, address, start), written at CONNECTING;
  ``last_known_phase`` and whether the robot may have been moving in it;
* rung results and the restart baselines per robot state (design 7.7 A).

**Where.** ``<data_dir>/robots/<serial>/ledger.json`` per robot, plus a
robot-independent ``<data_dir>/robots/pointer.json`` naming the address and
serial last seen. The serial is known only after IDENTIFY, so a session opens
its ledger **by address** (:meth:`Ledger.open_for_address`): until then it
writes to ``<data_dir>/robots/_unbound/<address>/ledger.json``, and
:meth:`Ledger.bind_serial` merges that into the robot's own ledger. Before
connecting, the runner scans **every** ledger (:func:`scan_ledgers`): any
ledger that needs a power cycle counts, because any robot may be the one at
the address (every Goliath answers at 10.0.0.1).

**How it is written.** Atomically: a temporary file in the same folder,
written, fsync'd, renamed over the ledger with ``os.replace``, then the folder
fsync'd on POSIX (:func:`atomic_write_bytes`). A process killed at any moment
leaves either the previous complete ledger or the new one, never a torn one. If
it was killed before the rename, its temporary file is the only trace of what it
was recording (an upload hazard? a pending power cycle?): the scan fails closed
on it, no later write removes it, and it is moved aside (never deleted) only
after a power cycle shown by positive evidence. Windows cannot fsync
a folder, so there a power loss of the laptop just after a write may bring
back the previous ledger; :func:`durability_disclosures` says so. Every write is a
read-modify-write under the robot lock (the RobotLock of unit L09, injected as
any object with ``held() -> bool``): the file is re-read first, and a ledger
that changed on disk behind the session's back is refused, never overwritten.
On Windows a ledger file held open by another program cannot be replaced; that
is :class:`LedgerBusyError`, raised at once with the remedy (no silent retry
loop), and the ledger keeps its previous state.

**Fail closed.** A ledger or pointer that cannot be read, is partly written,
fails validation or was written by a newer version of this program
(``schema_version``) is :class:`LedgerUnreadableError` naming the file; the
scan lists it as unreadable and its requirement is the strictest one
(``upload-mode``: nothing is known about it). So is the temporary file of an
interrupted ledger write. :func:`quarantine_unreadable` moves such a file
aside, never deletes it, after a power cycle shown by positive power-loss
evidence (:meth:`Ledger.bind_serial` does it for the robot's and the address's
own folders once such a power cycle was shown in the session).

**Clearing** (design 7.7). :func:`restart_verdict` is the rule;
:meth:`Ledger.attest_power_cycle` applies it. When the robot may be in upload
mode (a hazard, or an upload whose end was never recorded) attestation alone
never clears: it needs an observed link drop while the controller was off, or
the fresh-boot replay signature recorded at R1. Otherwise attestation clears
unless the passive replay contradicts the baseline for the robot's state.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol, get_args, runtime_checkable

from goliath_prep.core.canon import canonical_json
from goliath_prep.core.disclosure import Disclosure
from goliath_run.types import BASELINE_STATES, PHASE_INFO, BaselineState, FrozenMap, Phase, RestartEvidence

__all__ = [
    "CLEARING_ROUTES",
    "HAZARD_KINDS",
    "LEDGER_NAME",
    "POINTER_NAME",
    "REQUIREMENTS",
    "SCHEMA_VERSION",
    "Clearing",
    "ClearingRoute",
    "Hazard",
    "HazardKind",
    "Ledger",
    "LedgerBusyError",
    "LedgerError",
    "LedgerLockError",
    "LedgerScan",
    "LedgerState",
    "LedgerStateError",
    "LedgerUnreadableError",
    "LockLike",
    "PendingPowerCycle",
    "Pointer",
    "Requirement",
    "RestartBaseline",
    "SessionActive",
    "UploadInFlight",
    "Verdict",
    "atomic_write_bytes",
    "default_data_dir",
    "durability_disclosures",
    "quarantine_unreadable",
    "restart_verdict",
    "scan_ledgers",
]

log = logging.getLogger("goliath_run.ledger")

SCHEMA_VERSION = 1
"""The ledger and pointer format this version writes and reads. A higher number fails closed."""

LEDGER_NAME = "ledger.json"
POINTER_NAME = "pointer.json"
ROBOTS_DIR = "robots"
UNBOUND_DIR = "_unbound"
QUARANTINE_MARK = ".unreadable-"

HazardKind = Literal["upload-cut-short", "silent-after-send"]
"""``upload-cut-short``: an upload ended before its last block (design 7.5); ``silent-after-send``: the robot
did not answer the first command after connecting, so it may be in upload mode (red-team PHYS-02)."""
HAZARD_KINDS: tuple[str, ...] = get_args(HazardKind)

Requirement = Literal["none", "after-job", "upload-mode"]
"""What clearing a ledger needs (design 7.7): nothing; rule A (attestation plus a replay that does not contradict
the baseline for the robot's state); rule B (positive evidence that the controller lost power)."""
REQUIREMENTS: tuple[str, ...] = get_args(Requirement)
_RANK: Mapping[str, int] = MappingProxyType({"none": 0, "after-job": 1, "upload-mode": 2})

ClearingRoute = Literal["link-drop", "fresh-boot-replay", "replay-consistent", "attestation-only"]
CLEARING_ROUTES: tuple[str, ...] = get_args(ClearingRoute)

# The order in which a merged view picks the baseline state: the most eventful wins.
_STATE_RANK: Mapping[str, int] = MappingProxyType({"fresh": 0, "after-complete": 1, "after-interrupt": 2})
_BASELINE_RUNG: Mapping[str, str] = MappingProxyType({
    "fresh": "R1", "after-complete": "R3", "after-interrupt": "R3b (its aftermath)"})

# INFO codes of the connect-time replay (spec 02, INFO table; spec 09, section Connect and disconnect).
_INFO_IDENTITY = 34
_INFO_PANEL_DETECTED = 6
_INFO_PROGRAM_LOADED = 8
_INFO_RESUME = 41
_CONTRADICTING = (_INFO_PROGRAM_LOADED, _INFO_RESUME)

_DIRECTORY_FSYNC = os.name == "posix"
"""Folders can be opened and fsync'd on POSIX only. Elsewhere (Windows) the rename is still atomic, but whether it
survives a power loss of the laptop right after it rests on the file system's own metadata journal:
:func:`durability_disclosures` says so."""

_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SERIAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SAFE_ADDRESS = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}\Z")
_RUNG = re.compile(r"R[0-9]{1,2}[a-z]?\Z")
# What shows that the robot left upload mode (design 7.5: INFO;0, INFO;8, or work starting with INFO;10-17), and
# what ends the job. Nothing else may lift the upload-mode requirement: an upload error (ERR;0), position frames
# (only a fact may make them count: red-team FW-04) or an unknown name would be an unknown read as "yes".
_STREAM_END_SIGNALS: frozenset[str] = frozenset(["INFO;0", "INFO;8"] + ["INFO;%d" % c for c in range(10, 18)])
_JOB_END_SIGNAL = "INFO;23"
_WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"] + ["COM%d" % i for i in range(1, 10)] + ["LPT%d" % i for i in range(1, 10)])


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LedgerError(Exception):
    """Base of the ledger's errors."""


class LedgerUnreadableError(LedgerError):
    """A ledger or pointer file that cannot be trusted: fail closed (POWER_CYCLE_REQUIRED naming the file)."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = Path(path)
        self.reason = reason
        super().__init__(
            "ledger file %s cannot be used: %s. The runner treats this as 'the robot may be in upload mode' "
            "(power cycle with evidence required); after the power cycle the file can be moved aside "
            "(quarantined), never silently ignored" % (self.path, reason))


class LedgerLockError(LedgerError):
    """A ledger read-modify-write without the robot lock held by this process."""


class LedgerBusyError(LedgerError):
    """The ledger file could not be replaced, typically because another program has it open (Windows)."""

    def __init__(self, path: Path, error: OSError, *, action: str = "replace", outcome: str | None = None) -> None:
        self.path = Path(path)
        self.error = error
        self.outcome = outcome or "Nothing was changed: the ledger still holds its previous state"
        super().__init__(
            "could not %s %s: %s. On Windows this happens when another program (an editor, a backup or "
            "antivirus tool, a file viewer) has the file open: close it and try again. %s"
            % (action, self.path, error, self.outcome))


class LedgerStateError(LedgerError):
    """A call the ledger's current state does not allow (an upload while one is in flight, another robot, ...)."""


@runtime_checkable
class LockLike(Protocol):
    """The robot lock as the ledger sees it: ``held()`` is True while this process holds it (unit L09)."""

    def held(self) -> bool: ...


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _text(owner: str, name: str, value: object) -> str:
    if type(value) is not str:
        raise TypeError("%s.%s must be a str, got %s" % (owner, name, type(value).__name__))
    if not value.strip():
        raise ValueError("%s.%s must not be empty" % (owner, name))
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("%s.%s cannot be stored as UTF-8 (%s)" % (owner, name, exc)) from None
    return value


def _optional_text(owner: str, name: str, value: object) -> str | None:
    return None if value is None else _text(owner, name, value)


def _timestamp(owner: str, name: str, value: object) -> str:
    _text(owner, name, value)
    if not _TS.match(value):  # type: ignore[arg-type]
        raise ValueError("%s.%s %r is not a UTC time like 2026-11-04T12:00:00.000000Z" % (owner, name, value))
    datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")  # type: ignore[arg-type]
    return value  # type: ignore[return-value]


def _optional_timestamp(owner: str, name: str, value: object) -> str | None:
    return None if value is None else _timestamp(owner, name, value)


def _pattern(owner: str, name: str, value: object, pattern: re.Pattern[str], what: str) -> str:
    _text(owner, name, value)
    if not pattern.match(value):  # type: ignore[arg-type]
        raise ValueError("%s.%s %r is not %s" % (owner, name, value, what))
    return value  # type: ignore[return-value]


def _bool(owner: str, name: str, value: object) -> bool:
    if type(value) is not bool:
        raise TypeError("%s.%s must be a bool, got %s" % (owner, name, type(value).__name__))
    return value


def _count(owner: str, name: str, value: object, *, minimum: int = 0) -> int:
    if type(value) is not int:
        raise TypeError("%s.%s must be an int, got %s" % (owner, name, type(value).__name__))
    if value < minimum:
        raise ValueError("%s.%s must be >= %d, got %d" % (owner, name, minimum, value))
    return value


def _choice(owner: str, name: str, value: object, allowed: tuple[str, ...]) -> str:
    if type(value) is not str or value not in allowed:
        raise ValueError("%s.%s %r is not one of %s" % (owner, name, value, ", ".join(allowed)))
    return value


def _keys(owner: str, data: object, expected: set[str]) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise TypeError("%s: expected a JSON object, got %s" % (owner, type(data).__name__))
    missing = sorted(expected - set(data))
    extra = sorted(str(k) for k in set(data) - expected)
    if missing:
        raise ValueError("%s: missing key(s) %s" % (owner, ", ".join(missing)))
    if extra:
        raise ValueError("%s: unexpected key(s) %s" % (owner, ", ".join(extra)))
    return data


def _list(owner: str, name: str, value: object) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("%s.%s must be a list, got %s" % (owner, name, type(value).__name__))
    return tuple(value)


def _codes(owner: str, value: object) -> tuple[int, ...]:
    codes = _list(owner, "codes", value)
    for code in codes:
        if type(code) is not int or code < 0:
            raise ValueError("%s.codes must hold INFO codes (ints >= 0), got %r" % (owner, code))
    return codes


def _check_serial(serial: object) -> str:
    """A robot serial that is safe as a folder name on Windows and POSIX (12 hex digits on 01.02.00)."""
    if type(serial) is not str:
        raise TypeError("serial must be a str, got %s" % type(serial).__name__)
    if not _SERIAL.match(serial) or serial.endswith("."):
        raise ValueError("serial %r: use 1-64 letters, digits, '.', '_' or '-', starting with a letter or digit "
                         "and not ending with '.'" % serial)
    if serial.split(".", 1)[0].upper() in _WINDOWS_RESERVED or serial.lower() == POINTER_NAME:
        raise ValueError("serial %r is a reserved file name" % serial)
    return serial


def _check_address(address: object) -> str:
    if type(address) is not str:
        raise TypeError("address must be a str, got %s" % type(address).__name__)
    if not address.strip() or address != address.strip() or len(address) > 255:
        raise ValueError("address %r: give the robot's address without surrounding spaces" % address)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in address):
        raise ValueError("address %r contains a control character" % address)
    return address


def _address_key(address: str) -> str:
    """The folder name of an address's unbound ledger: the address itself when it is a safe name, else a hash."""
    if (_SAFE_ADDRESS.match(address) and not address.endswith(".")
            and address.split(".", 1)[0].upper() not in _WINDOWS_RESERVED):
        return address
    return "addr-" + hashlib.sha256(address.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Atomic, durable file writes
# ---------------------------------------------------------------------------


def _fsync_directory(directory: Path) -> None:
    if not _DIRECTORY_FSYNC:
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_dir(directory: Path) -> None:
    """Create ``directory`` and missing parents; on POSIX each new entry is fsync'd into its parent."""
    missing: list[Path] = []
    probe = directory
    while not probe.exists():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    for folder in reversed(missing):
        try:
            folder.mkdir()
        except FileExistsError:
            pass  # created meanwhile by another thread; is_dir() below checks what it is
        _fsync_directory(folder.parent)
    if not directory.is_dir():
        raise NotADirectoryError("%s is not a folder" % directory)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("write to a temporary ledger file made no progress")
        view = view[written:]


def _temp_prefix(path: Path) -> str:
    return ".%s." % path.name


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` atomically and durably.

    A temporary file next to ``path`` is written (binary, so no newline
    translation on Windows), fsync'd and closed, then renamed over ``path``
    with ``os.replace``; on POSIX the folder is fsync'd afterwards so the rename
    itself survives a power loss. When this returns, the new content is on
    disk. On any error the temporary file is removed and the error raised;
    ``path`` then holds its previous content. A ``PermissionError`` from the
    rename (on Windows: the file is open in another program) is
    :class:`LedgerBusyError`.
    """
    path = Path(path)
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("data must be bytes, got %s" % type(data).__name__)
    _ensure_dir(path.parent)
    tmp = path.parent / ("%s%d.%s.tmp" % (_temp_prefix(path), os.getpid(), secrets.token_hex(4)))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        try:
            _write_all(fd, bytes(data))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(tmp, path)
        except PermissionError as exc:
            raise LedgerBusyError(path, exc) from exc
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass  # never created, or already renamed
        except OSError as cleanup:
            log.warning("could not remove the temporary file %s after a failed write: %s", tmp, cleanup)
        raise
    _fsync_directory(path.parent)


_NO_DIRECTORY_FSYNC = Disclosure(
    id="LEDGER:no-directory-fsync", severity="amber",
    text="on this operating system the machine ledger's folder cannot be fsync'd: each ledger write is atomic and "
         "its content is on disk before the upload header is sent, but if the laptop itself loses power within "
         "seconds of a write, the file system may bring back the previous ledger. After a laptop power loss "
         "during a job, treat the robot as possibly in upload mode: power-cycle it with evidence before using it",
    source="the project design, section 3.3 (directory fsync on POSIX only)", sticky=True)


def durability_disclosures() -> tuple[Disclosure, ...]:
    """What the runner must show about how durable ledger writes are on this computer (empty on POSIX).

    Where folders cannot be fsync'd (Windows), a rename is atomic but its durability across a power loss of the
    laptop rests on the file system: disclosed, never silent (the owner's rule).
    """
    return () if _DIRECTORY_FSYNC else (_NO_DIRECTORY_FSYNC,)


_INTERRUPTED_REASON = (
    "a write of the ledger in this folder was interrupted (the program was killed, or the laptop lost power, while "
    "writing it): the ledger itself is the previous complete version, but what the interrupted write was recording "
    "(an upload hazard? a pending power cycle?) is unknown. It is moved aside after a power cycle shown by positive "
    "power-loss evidence")


def _is_ledger_temp(path: Path) -> bool:
    """A temporary file of a ledger write (``.ledger.json.<pid>.<hex>.tmp``)."""
    return path.name.startswith(_temp_prefix(Path(LEDGER_NAME))) and path.name.endswith(".tmp")


def _ledger_temps(folder: Path) -> list[Path]:
    return sorted(folder.glob(_temp_prefix(Path(LEDGER_NAME)) + "*.tmp"))


def _move_aside(path: Path, stamp: str) -> Path:
    """Rename an unreadable ledger, pointer or interrupted-write file to ``<ledger or pointer>.unreadable-...``."""
    if _is_ledger_temp(path):
        aside = path.with_name(LEDGER_NAME + QUARANTINE_MARK + stamp + "-" + path.name.lstrip("."))
    else:
        aside = path.with_name(path.name + QUARANTINE_MARK + stamp)
    os.replace(path, aside)
    _fsync_directory(path.parent)
    return aside


def _quarantine_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _union(first: tuple[Any, ...], second: tuple[Any, ...]) -> tuple[Any, ...]:
    """``first`` then the entries of ``second`` not already in it (a retried merge adds nothing twice)."""
    return first + tuple(item for item in second if item not in first)


def _json_bytes(obj: object) -> bytes:
    return canonical_json(obj)


def _parse_json(path: Path, raw: bytes) -> Any:
    if not raw.strip():
        raise LedgerUnreadableError(path, "the file is empty (partly written?)")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LedgerUnreadableError(path, "not UTF-8 text (%s)" % exc) from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LedgerUnreadableError(path, "not valid JSON (truncated or partly written?): %s" % exc) from None


def _check_schema(path: Path, data: object) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise LedgerUnreadableError(path, "not a JSON object")
    version = data.get("schema_version")
    if type(version) is not int:
        raise LedgerUnreadableError(path, "no valid schema_version (got %r)" % (version,))
    if version > SCHEMA_VERSION:
        raise LedgerUnreadableError(
            path, "written by a newer version of goliath-run (schema_version %d; this version reads %d): use "
                  "that version" % (version, SCHEMA_VERSION))
    if version < 1:
        raise LedgerUnreadableError(path, "schema_version %d is not a version this program ever wrote" % version)
    return data


# ---------------------------------------------------------------------------
# Ledger contents
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadInFlight:
    """An upload whose header may have been sent: set before the header, cleared by INFO;23 or a power cycle.

    ``stream_end`` is the end-of-upload signal once seen (``INFO;8``, ``INFO;0``): the robot then left upload
    mode and the job runs. While it is None the robot may still be in upload mode.
    """

    job_id: str
    sha256: str
    began_at: str
    stream_end: str | None = None
    stream_end_at: str | None = None

    def __post_init__(self) -> None:
        o = "UploadInFlight"
        _pattern(o, "job_id", self.job_id, _JOB_ID, "a job id (letters, digits, '.', '_', '-'; at most 64)")
        _pattern(o, "sha256", self.sha256, _SHA256, "a lowercase hex SHA-256")
        _timestamp(o, "began_at", self.began_at)
        _optional_text(o, "stream_end", self.stream_end)
        _optional_timestamp(o, "stream_end_at", self.stream_end_at)
        if (self.stream_end is None) != (self.stream_end_at is None):
            raise ValueError("UploadInFlight: stream_end and stream_end_at go together")

    def to_json(self) -> dict[str, Any]:
        return {"job_id": self.job_id, "sha256": self.sha256, "began_at": self.began_at,
                "stream_end": self.stream_end, "stream_end_at": self.stream_end_at}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> UploadInFlight:
        d = _keys("UploadInFlight", data, {"job_id", "sha256", "began_at", "stream_end", "stream_end_at"})
        return cls(**d)


@dataclass(frozen=True)
class Hazard:
    """The robot may be in upload mode: anything sent could become program data (design 7.7 B)."""

    kind: HazardKind
    reason: str
    at: str

    def __post_init__(self) -> None:
        _choice("Hazard", "kind", self.kind, HAZARD_KINDS)
        _text("Hazard", "reason", self.reason)
        _timestamp("Hazard", "at", self.at)

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": self.reason, "at": self.at}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Hazard:
        return cls(**_keys("Hazard", data, {"kind", "reason", "at"}))


@dataclass(frozen=True)
class PendingPowerCycle:
    """One reason why the robot must be power-cycled before the next job (design 7.7 A)."""

    reason: str
    at: str

    def __post_init__(self) -> None:
        _text("PendingPowerCycle", "reason", self.reason)
        _timestamp("PendingPowerCycle", "at", self.at)

    def to_json(self) -> dict[str, Any]:
        return {"reason": self.reason, "at": self.at}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> PendingPowerCycle:
        return cls(**_keys("PendingPowerCycle", data, {"reason", "at"}))


@dataclass(frozen=True)
class SessionActive:
    """A session that began (CONNECTING) and has not ended; still set after a crash."""

    pid: int
    address: str
    started_at: str

    def __post_init__(self) -> None:
        _count("SessionActive", "pid", self.pid, minimum=1)
        _check_address(self.address)
        _timestamp("SessionActive", "started_at", self.started_at)

    def to_json(self) -> dict[str, Any]:
        return {"pid": self.pid, "address": self.address, "started_at": self.started_at}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SessionActive:
        return cls(**_keys("SessionActive", data, {"pid", "address", "started_at"}))


@dataclass(frozen=True)
class RestartBaseline:
    """The INFO codes the passive reconnect after a power cycle showed in one robot state (design 7.7 A)."""

    codes: tuple[int, ...]
    rung: str
    at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "codes", _codes("RestartBaseline", self.codes))
        _pattern("RestartBaseline", "rung", self.rung, _RUNG, "a rung id such as R1 or R3b")
        _timestamp("RestartBaseline", "at", self.at)

    def to_json(self) -> dict[str, Any]:
        return {"codes": list(self.codes), "rung": self.rung, "at": self.at}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RestartBaseline:
        d = _keys("RestartBaseline", data, {"codes", "rung", "at"})
        return cls(codes=_codes("RestartBaseline", d["codes"]), rung=d["rung"], at=d["at"])


@dataclass(frozen=True)
class Verdict:
    """Whether an attested power cycle clears what is pending (design 7.7), and why.

    * ``rule`` - what was required: ``none``, ``after-job`` (7.7 A) or ``upload-mode`` (7.7 B);
    * ``route`` - how it was cleared (None when not): ``link-drop`` (the runner saw the link go down while the
      controller was off), ``fresh-boot-replay`` (the replay equals the fresh-boot signature recorded at R1),
      ``replay-consistent`` (a replay that does not contradict the baseline) or ``attestation-only``;
    * ``positive_power_loss`` - the evidence shows the controller lost power (a link drop or the fresh-boot
      signature), whatever the rule;
    * ``reasons`` - what decided it; ``remedy`` - what to do when it did not clear;
    * ``disclosures`` - to show from now on (for example the sticky "restart verified by attestation only").
    """

    cleared: bool
    rule: Requirement
    route: ClearingRoute | None
    positive_power_loss: bool
    reasons: tuple[str, ...]
    remedy: str | None
    disclosures: tuple[Disclosure, ...]
    baseline_state: BaselineState

    def __post_init__(self) -> None:
        _bool("Verdict", "cleared", self.cleared)
        _choice("Verdict", "rule", self.rule, REQUIREMENTS)
        if self.cleared != (self.route is not None):
            raise ValueError("Verdict: a cleared verdict names its route, a refused one has none")
        if self.route is not None:
            _choice("Verdict", "route", self.route, CLEARING_ROUTES)
        _bool("Verdict", "positive_power_loss", self.positive_power_loss)
        object.__setattr__(self, "reasons", tuple(_text("Verdict", "reasons[]", r) for r in self.reasons))
        if not self.cleared:
            _text("Verdict", "remedy", self.remedy)
        object.__setattr__(self, "disclosures", tuple(self.disclosures))
        for d in self.disclosures:
            if not isinstance(d, Disclosure):
                raise TypeError("Verdict.disclosures must hold Disclosures")
        _choice("Verdict", "baseline_state", self.baseline_state, BASELINE_STATES)


@dataclass(frozen=True)
class Clearing:
    """A power-cycle clearing as recorded in the ledger: the verdict, the evidence and the attestation verbatim."""

    at: str
    rule: Requirement
    route: ClearingRoute
    positive_power_loss: bool
    attestation: str
    evidence: RestartEvidence
    disclosures: tuple[Disclosure, ...]
    note: str | None = None

    def __post_init__(self) -> None:
        _timestamp("Clearing", "at", self.at)
        _choice("Clearing", "rule", self.rule, REQUIREMENTS)
        _choice("Clearing", "route", self.route, CLEARING_ROUTES)
        _bool("Clearing", "positive_power_loss", self.positive_power_loss)
        _text("Clearing", "attestation", self.attestation)
        if not isinstance(self.evidence, RestartEvidence):
            raise TypeError("Clearing.evidence must be a RestartEvidence")
        object.__setattr__(self, "disclosures", tuple(self.disclosures))
        _optional_text("Clearing", "note", self.note)

    def to_json(self) -> dict[str, Any]:
        return {"at": self.at, "rule": self.rule, "route": self.route,
                "positive_power_loss": self.positive_power_loss, "attestation": self.attestation,
                "evidence": self.evidence.to_json(), "disclosures": [d.to_json() for d in self.disclosures],
                "note": self.note}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Clearing:
        d = _keys("Clearing", data, {"at", "rule", "route", "positive_power_loss", "attestation", "evidence",
                                     "disclosures", "note"})
        return cls(at=d["at"], rule=d["rule"], route=d["route"], positive_power_loss=d["positive_power_loss"],
                   attestation=d["attestation"], evidence=RestartEvidence.from_json(d["evidence"]),
                   disclosures=tuple(Disclosure.from_json(x) for x in _list("Clearing", "disclosures",
                                                                            d["disclosures"])),
                   note=d["note"])


_STATE_KEYS = {"schema_version", "address", "serial", "updated_at", "upload_in_flight", "pending_power_cycle",
               "hazards", "uploads_since_attested_power_cycle", "session_active", "last_known_phase",
               "last_may_be_moving", "rung_results", "restart_baselines", "last_clearing"}


@dataclass(frozen=True)
class LedgerState:
    """One ledger's content (immutable). ``path`` is where it was read from; it is not part of the JSON."""

    address: str
    serial: str | None
    updated_at: str | None
    upload_in_flight: UploadInFlight | None
    pending_power_cycle: tuple[PendingPowerCycle, ...]
    hazards: tuple[Hazard, ...]
    uploads_since_attested_power_cycle: int
    session_active: SessionActive | None
    last_known_phase: str | None
    last_may_be_moving: bool
    rung_results: FrozenMap
    restart_baselines: Mapping[str, RestartBaseline]
    last_clearing: Clearing | None
    path: Path | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        o = "LedgerState"
        _check_address(self.address)
        if self.serial is not None:
            _check_serial(self.serial)
        _optional_timestamp(o, "updated_at", self.updated_at)
        if self.upload_in_flight is not None and not isinstance(self.upload_in_flight, UploadInFlight):
            raise TypeError("LedgerState.upload_in_flight must be an UploadInFlight")
        object.__setattr__(self, "pending_power_cycle", tuple(self.pending_power_cycle))
        object.__setattr__(self, "hazards", tuple(self.hazards))
        for p in self.pending_power_cycle:
            if not isinstance(p, PendingPowerCycle):
                raise TypeError("LedgerState.pending_power_cycle must hold PendingPowerCycle values")
        for h in self.hazards:
            if not isinstance(h, Hazard):
                raise TypeError("LedgerState.hazards must hold Hazard values")
        _count(o, "uploads_since_attested_power_cycle", self.uploads_since_attested_power_cycle)
        if self.session_active is not None and not isinstance(self.session_active, SessionActive):
            raise TypeError("LedgerState.session_active must be a SessionActive")
        if self.last_known_phase is not None:
            _choice(o, "last_known_phase", self.last_known_phase, tuple(p.name for p in Phase))
            if PHASE_INFO[Phase[self.last_known_phase]].may_be_moving and not self.last_may_be_moving:
                raise ValueError("LedgerState: phase %s may be moving, so last_may_be_moving must be true"
                                 % self.last_known_phase)
        _bool(o, "last_may_be_moving", self.last_may_be_moving)
        if not isinstance(self.rung_results, FrozenMap):
            object.__setattr__(self, "rung_results", FrozenMap(self.rung_results))
        for rung, result in self.rung_results.items():
            _pattern(o, "rung_results key", rung, _RUNG, "a rung id such as R1 or R3b")
            if not isinstance(result, Mapping):
                raise TypeError("LedgerState.rung_results[%s] must be a JSON object" % rung)
        baselines = dict(self.restart_baselines)
        for state, baseline in baselines.items():
            _choice(o, "restart_baselines key", state, BASELINE_STATES)
            if not isinstance(baseline, RestartBaseline):
                raise TypeError("LedgerState.restart_baselines[%s] must be a RestartBaseline" % state)
        object.__setattr__(self, "restart_baselines", MappingProxyType(baselines))
        if self.last_clearing is not None and not isinstance(self.last_clearing, Clearing):
            raise TypeError("LedgerState.last_clearing must be a Clearing")
        if self.path is not None:
            object.__setattr__(self, "path", Path(self.path))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LedgerState):
            return NotImplemented
        return self.to_json() == other.to_json()

    def __hash__(self) -> int:
        return hash(_json_bytes(self.to_json()))

    @classmethod
    def empty(cls, address: str, serial: str | None = None) -> LedgerState:
        return cls(address=address, serial=serial, updated_at=None, upload_in_flight=None, pending_power_cycle=(),
                   hazards=(), uploads_since_attested_power_cycle=0, session_active=None, last_known_phase=None,
                   last_may_be_moving=False, rung_results=FrozenMap({}), restart_baselines={}, last_clearing=None)

    # -- what it means -------------------------------------------------------

    @property
    def may_be_in_upload_mode(self) -> bool:
        """A hazard, or an upload whose end was never recorded: anything sent could become program data."""
        return bool(self.hazards) or (self.upload_in_flight is not None and self.upload_in_flight.stream_end is None)

    @property
    def unclosed_session_may_be_moving(self) -> bool:
        return self.session_active is not None and self.last_may_be_moving

    @property
    def requirement(self) -> Requirement:
        """What clearing this ledger needs (design 7.7): ``upload-mode`` (rule B), ``after-job`` (rule A) or none."""
        if self.may_be_in_upload_mode:
            return "upload-mode"
        if (self.upload_in_flight is not None or self.pending_power_cycle
                or self.uploads_since_attested_power_cycle > 0 or self.unclosed_session_may_be_moving):
            return "after-job"
        return "none"

    @property
    def restart_baseline_state(self) -> BaselineState:
        """The state the robot is in before the next power cycle: the baseline its replay is compared with."""
        if self.upload_in_flight is not None:
            return "after-interrupt"
        if self.uploads_since_attested_power_cycle > 0:
            return "after-complete"
        return "fresh"

    def attention(self) -> tuple[str, ...]:
        """One sentence per reason this ledger needs a power cycle (empty when it needs none)."""
        out: list[str] = []
        up = self.upload_in_flight
        if up is not None:
            if up.stream_end is None:
                out.append("the upload of job %s (SHA-256 %s...) began at %s and its end was never recorded: the "
                           "robot may still be in upload mode" % (up.job_id, up.sha256[:12], up.began_at))
            else:
                out.append("the upload of job %s ended (%s at %s) but the job's end (INFO;23) was never recorded: "
                           "the job may still be running" % (up.job_id, up.stream_end, up.stream_end_at))
        for h in self.hazards:
            out.append("upload hazard (%s): %s (since %s)" % (h.kind, h.reason, h.at))
        for p in self.pending_power_cycle:
            out.append("power cycle required: %s (since %s)" % (p.reason, p.at))
        if self.uploads_since_attested_power_cycle and up is None and not self.pending_power_cycle:
            out.append("%d upload(s) since the last attested power cycle: one upload per power cycle"
                       % self.uploads_since_attested_power_cycle)
        if self.unclosed_session_may_be_moving:
            out.append(_unclosed_reason(self))
        return tuple(out)

    # -- JSON ----------------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "address": self.address,
            "serial": self.serial,
            "updated_at": self.updated_at,
            "upload_in_flight": None if self.upload_in_flight is None else self.upload_in_flight.to_json(),
            "pending_power_cycle": [p.to_json() for p in self.pending_power_cycle],
            "hazards": [h.to_json() for h in self.hazards],
            "uploads_since_attested_power_cycle": self.uploads_since_attested_power_cycle,
            "session_active": None if self.session_active is None else self.session_active.to_json(),
            "last_known_phase": self.last_known_phase,
            "last_may_be_moving": self.last_may_be_moving,
            "rung_results": self.rung_results.to_json(),
            "restart_baselines": {k: v.to_json() for k, v in sorted(self.restart_baselines.items())},
            "last_clearing": None if self.last_clearing is None else self.last_clearing.to_json(),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any], *, path: Path | None = None) -> LedgerState:
        d = _keys("LedgerState", data, _STATE_KEYS)
        version = d["schema_version"]
        if type(version) is not int or version != SCHEMA_VERSION:
            raise ValueError("LedgerState: schema_version %r is not %d" % (version, SCHEMA_VERSION))
        up = d["upload_in_flight"]
        sa = d["session_active"]
        lc = d["last_clearing"]
        baselines = d["restart_baselines"]
        if not isinstance(baselines, Mapping):
            raise TypeError("LedgerState.restart_baselines must be a JSON object")
        rung_results = d["rung_results"]
        if not isinstance(rung_results, Mapping):
            raise TypeError("LedgerState.rung_results must be a JSON object")
        return cls(
            address=d["address"], serial=d["serial"], updated_at=d["updated_at"],
            upload_in_flight=None if up is None else UploadInFlight.from_json(up),
            pending_power_cycle=tuple(PendingPowerCycle.from_json(x)
                                      for x in _list("LedgerState", "pending_power_cycle",
                                                     d["pending_power_cycle"])),
            hazards=tuple(Hazard.from_json(x) for x in _list("LedgerState", "hazards", d["hazards"])),
            uploads_since_attested_power_cycle=d["uploads_since_attested_power_cycle"],
            session_active=None if sa is None else SessionActive.from_json(sa),
            last_known_phase=d["last_known_phase"], last_may_be_moving=d["last_may_be_moving"],
            rung_results=FrozenMap(rung_results),
            restart_baselines={k: RestartBaseline.from_json(v) for k, v in baselines.items()},
            last_clearing=None if lc is None else Clearing.from_json(lc),
            path=path,
        )


def _unclosed_reason(state: LedgerState) -> str:
    sa = state.session_active
    if sa is None:
        raise ValueError("_unclosed_reason needs a ledger with an active session")
    return ("the session of pid %d (started %s) did not close; its last phase was %s, where the robot may be "
            "moving" % (sa.pid, sa.started_at, state.last_known_phase or "unknown"))


@dataclass(frozen=True)
class Pointer:
    """The robot last seen: its address and serial (robot-independent, ``robots/pointer.json``)."""

    address: str
    serial: str
    updated_at: str

    def __post_init__(self) -> None:
        _check_address(self.address)
        _check_serial(self.serial)
        _timestamp("Pointer", "updated_at", self.updated_at)

    def to_json(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "address": self.address, "serial": self.serial,
                "updated_at": self.updated_at}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Pointer:
        d = _keys("Pointer", data, {"schema_version", "address", "serial", "updated_at"})
        if d["schema_version"] != SCHEMA_VERSION or type(d["schema_version"]) is not int:
            raise ValueError("Pointer: schema_version %r is not %d" % (d["schema_version"], SCHEMA_VERSION))
        return cls(address=d["address"], serial=d["serial"], updated_at=d["updated_at"])


# ---------------------------------------------------------------------------
# Reading files
# ---------------------------------------------------------------------------


def _robots(data_dir: Path) -> Path:
    return Path(data_dir) / ROBOTS_DIR


def _serial_path(data_dir: Path, serial: str) -> Path:
    return _robots(data_dir) / serial / LEDGER_NAME


def _unbound_path(data_dir: Path, address: str) -> Path:
    return _robots(data_dir) / UNBOUND_DIR / _address_key(address) / LEDGER_NAME


def _pointer_path(data_dir: Path) -> Path:
    return _robots(data_dir) / POINTER_NAME


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise
    except PermissionError as exc:
        raise LedgerUnreadableError(
            path, "it cannot be read: %s (on Windows another program, such as an editor, a backup or antivirus "
                  "tool or a file viewer, may have it open: close it and scan again)" % exc) from exc
    except OSError as exc:
        raise LedgerUnreadableError(path, "it cannot be read: %s" % exc) from exc


def _read_state(path: Path, *, serial: str | None = None, address_key: str | None = None) -> LedgerState:
    """The ledger at ``path``; FileNotFoundError if there is none; LedgerUnreadableError if it cannot be trusted.

    ``serial``: the folder says it belongs to this serial; ``address_key``: it is the unbound ledger of the
    address with this folder name. A ledger that disagrees with its folder is unreadable too.
    """
    data = _check_schema(path, _parse_json(path, _read_bytes(path)))
    try:
        state = LedgerState.from_json(data, path=path)
    except (TypeError, ValueError, KeyError) as exc:
        raise LedgerUnreadableError(path, "it does not validate (partly written or edited by hand?): %s" % exc) \
            from None
    if serial is not None and state.serial != serial:
        raise LedgerUnreadableError(path, "it is in the folder of robot %s but names robot %r" % (serial, state.serial))
    if address_key is not None:
        if state.serial is not None:
            raise LedgerUnreadableError(path, "an address ledger that names robot %r" % state.serial)
        if _address_key(state.address) != address_key:
            raise LedgerUnreadableError(path, "it is in the folder of address %s but names address %r"
                                        % (address_key, state.address))
        # Before IDENTIFY the ledger refuses uploads, rung results and baselines, so an address ledger never
        # holds them; one that does could not be merged into the robot's ledger without losing them.
        robot_only = [name for name, present in (
            ("upload_in_flight", state.upload_in_flight is not None),
            ("uploads_since_attested_power_cycle", state.uploads_since_attested_power_cycle != 0),
            ("rung_results", bool(state.rung_results)),
            ("restart_baselines", bool(state.restart_baselines))) if present]
        if robot_only:
            raise LedgerUnreadableError(path, "an address ledger (robot not identified) that records %s, which only "
                                              "a robot's own ledger holds" % ", ".join(robot_only))
    return state


def _read_pointer(path: Path) -> Pointer:
    data = _check_schema(path, _parse_json(path, _read_bytes(path)))
    try:
        return Pointer.from_json(data)
    except (TypeError, ValueError, KeyError) as exc:
        raise LedgerUnreadableError(path, "it does not validate (partly written or edited by hand?): %s" % exc) \
            from None


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerScan:
    """Every ledger of a data folder, read before connecting (design 3.3; red-team FW-07, C-08).

    * ``pending`` - readable ledgers that need a power cycle (``requirement`` other than ``none``);
    * ``unreadable`` - ledger or pointer files that cannot be trusted, with ``reasons[path]``: fail closed;
    * ``ledgers`` - every readable ledger; ``pointer`` - the robot last seen (None if none or unreadable);
    * ``interrupted_writes`` - temporary files left by a killed writer (the ledgers themselves are complete). A
      ledger's is also in ``unreadable`` (fail closed: what it was recording is unknown); a pointer's is not (the
      pointer records no robot state);
    * ``quarantined`` - unreadable files moved aside by :func:`quarantine_unreadable`.
    """

    pending: tuple[LedgerState, ...]
    unreadable: tuple[Path, ...]
    reasons: Mapping[Path, str]
    ledgers: tuple[LedgerState, ...]
    pointer: Pointer | None
    interrupted_writes: tuple[Path, ...]
    quarantined: tuple[Path, ...]

    @property
    def clean(self) -> bool:
        return not self.pending and not self.unreadable

    @property
    def any_ledger(self) -> bool:
        """False on first use, another laptop or a reinstall: K1 then needs the fresh-power-on line (design 7.4)."""
        return bool(self.ledgers or self.unreadable)

    @property
    def requirement(self) -> Requirement:
        """The strictest clearing any ledger needs; an unreadable file needs the strictest of all."""
        if self.unreadable:
            return "upload-mode"
        return _strictest(s.requirement for s in self.pending)

    def attention(self) -> tuple[str, ...]:
        out = ["%s: %s" % (p, self.reasons[p]) for p in self.unreadable]
        for state in self.pending:
            who = "robot %s" % state.serial if state.serial else "the robot at %s (not identified)" % state.address
            out.extend("%s: %s" % (who, reason) for reason in state.attention())
        return tuple(out)


def _strictest(requirements: Iterable[str]) -> Requirement:
    best = "none"
    for r in requirements:
        if _RANK[r] > _RANK[best]:
            best = r
    return best  # type: ignore[return-value]


def scan_ledgers(data_dir: Path) -> LedgerScan:
    """Read every ledger and the pointer under ``data_dir`` (read-only; needs no lock)."""
    robots = _robots(Path(data_dir))
    ledgers: list[LedgerState] = []
    unreadable: dict[Path, str] = {}
    temps: list[Path] = []
    quarantined: list[Path] = []

    def scan_folder(folder: Path, *, serial: str | None = None, address_key: str | None = None) -> None:
        for leftover in _ledger_temps(folder):
            temps.append(leftover)
            unreadable[leftover] = _INTERRUPTED_REASON  # fail closed: what it was recording is unknown
        quarantined.extend(sorted(folder.glob(LEDGER_NAME + QUARANTINE_MARK + "*")))
        path = folder / LEDGER_NAME
        try:
            ledgers.append(_read_state(path, serial=serial, address_key=address_key))
        except FileNotFoundError:
            return
        except LedgerUnreadableError as exc:
            unreadable[path] = exc.reason

    if robots.exists() and not robots.is_dir():
        reason = "it is not a folder: the ledgers belong in it, so nothing can be said about them"
        log.error("ledger folder %s cannot be used: %s", robots, reason)
        return LedgerScan(pending=(), unreadable=(robots,), reasons=MappingProxyType({robots: reason}), ledgers=(),
                          pointer=None, interrupted_writes=(), quarantined=())
    if robots.is_dir():
        for entry in sorted(robots.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name == UNBOUND_DIR:
                for sub in sorted(entry.iterdir()):
                    if sub.is_dir():
                        scan_folder(sub, address_key=sub.name)
            else:
                scan_folder(entry, serial=entry.name)
        temps.extend(sorted(robots.glob(_temp_prefix(Path(POINTER_NAME)) + "*.tmp")))
        quarantined.extend(sorted(robots.glob(POINTER_NAME + QUARANTINE_MARK + "*")))

    pointer: Pointer | None = None
    pointer_path = robots / POINTER_NAME
    try:
        pointer = _read_pointer(pointer_path)
    except FileNotFoundError:
        pointer = None
    except LedgerUnreadableError as exc:
        unreadable[pointer_path] = exc.reason
    if pointer is not None and not (robots / pointer.serial / LEDGER_NAME).exists():
        unreadable[pointer_path] = ("it names robot %s, whose ledger %s is missing (deleted or moved?)"
                                    % (pointer.serial, robots / pointer.serial / LEDGER_NAME))
        pointer = None

    for leftover in temps:
        log.warning("an interrupted write left %s (the file next to it is the previous complete version)", leftover)
    for path, reason in unreadable.items():
        log.error("ledger file %s cannot be used: %s", path, reason)
    readable = tuple(ledgers)
    return LedgerScan(
        pending=tuple(s for s in readable if s.requirement != "none"),
        unreadable=tuple(sorted(unreadable)),
        reasons=MappingProxyType(dict(unreadable)),
        ledgers=readable,
        pointer=pointer,
        interrupted_writes=tuple(temps),
        quarantined=tuple(quarantined),
    )


# ---------------------------------------------------------------------------
# The clearing rule (design 7.7)
# ---------------------------------------------------------------------------

_ATTESTATION_ONLY = Disclosure(
    id="RESTART:attestation-only", severity="amber",
    text="restart verified by attestation only: no replay or link drop was observed after the power cycle",
    source="the project design, section 7.7 A", sticky=True)
_INFO8_UNAVAILABLE = Disclosure(
    id="RESTART:info8-unavailable", severity="amber",
    text="INFO;8 restart evidence unavailable on this robot: its fresh-boot baseline (rung R1) already has INFO;8 "
         "(a program left by another app), so a replayed INFO;8 cannot show that a job was left behind",
    source="the project design, section 7.7 A", sticky=True)
_REMEDY_B = ("connect passively first (connecting sends nothing), then switch the robot's controller off at its "
             "power switch and wait until this program reports the link drop (up to about 25 s); switch it on, "
             "wait for it to boot, reconnect and attest again. An e-stop that leaves the controller on is not a "
             "power cycle")


def restart_verdict(need: Requirement, expected_state: BaselineState, evidence: RestartEvidence, *,
                    attestation: str, baselines: Mapping[str, RestartBaseline] | None) -> Verdict:
    """Whether an attested power cycle clears ``need`` (design 7.7; red-team FW-02, FW-06, PHYS-02). Pure.

    ``attestation`` is the operator's typed K6 text, verbatim (the core checks the phrase). ``baselines`` are
    the robot's restart baselines, or None when the robot is not identified (no baseline is then trusted).

    * ``upload-mode`` (7.7 B): cleared only by positive power-loss evidence: an observed link drop, or a replay
      whose codes equal the fresh-boot baseline, which must itself be the signature (INFO;34 without INFO;6 and
      INFO;8). The replay cannot block this route: a genuine power cycle must not deadlock.
    * ``after-job`` and ``none`` (7.7 A): cleared unless the replay has INFO;8 or INFO;41 that the baseline for
      ``expected_state`` lacks (no baseline: any INFO;8 or INFO;41 blocks). INFO;8 is not evidence when the
      fresh-boot baseline has it (disclosed). No replay and no link drop: cleared, with the sticky disclosure
      "restart verified by attestation only". Evidence taken against another baseline state is refused.
    """
    _choice("restart_verdict", "need", need, REQUIREMENTS)
    _choice("restart_verdict", "expected_state", expected_state, BASELINE_STATES)
    if not isinstance(evidence, RestartEvidence):
        raise TypeError("evidence must be a RestartEvidence, got %s" % type(evidence).__name__)
    _text("restart_verdict", "attestation", attestation)
    codes = set(evidence.replay_codes)
    fresh = None if baselines is None else baselines.get("fresh")
    fresh_codes = set(fresh.codes) if fresh is not None else None
    signature_usable = (fresh_codes is not None and _INFO_IDENTITY in fresh_codes
                        and _INFO_PANEL_DETECTED not in fresh_codes and _INFO_PROGRAM_LOADED not in fresh_codes)
    replay_is_fresh = signature_usable and bool(codes) and codes == fresh_codes
    positive = evidence.link_drop_observed or replay_is_fresh

    def verdict(cleared: bool, route: str | None, reasons: list[str], remedy: str | None,
                disclosures: tuple[Disclosure, ...] = ()) -> Verdict:
        return Verdict(cleared=cleared, rule=need, route=route, positive_power_loss=positive,  # type: ignore[arg-type]
                       reasons=tuple(reasons), remedy=remedy, disclosures=disclosures, baseline_state=expected_state)

    if need == "upload-mode":
        if evidence.link_drop_observed:
            return verdict(True, "link-drop", ["the link dropped while the controller was off: it lost power"], None)
        if replay_is_fresh:
            return verdict(True, "fresh-boot-replay",
                           ["the replay %s equals the fresh-boot signature recorded at rung %s"
                            % (_fmt(codes), fresh.rung)], None)  # type: ignore[union-attr]
        reasons = ["the robot may be in upload mode, so attestation alone cannot clear it (design 7.7 B)",
                   "no link drop was observed while the controller was off"]
        if fresh_codes is None:
            reasons.append("no fresh-boot replay signature is recorded for this robot (rung R1), or the robot "
                           "is not identified yet")
        elif not signature_usable:
            reasons.append("this robot's fresh-boot baseline %s is not a usable signature (it needs INFO;34 "
                           "without INFO;6 and INFO;8)" % _fmt(fresh_codes))
        else:
            reasons.append("the replay %s is not the fresh-boot signature %s" % (_fmt(codes), _fmt(fresh_codes)))
        return verdict(False, None, reasons, _REMEDY_B)

    if evidence.baseline_state != expected_state:
        return verdict(False, None, [
            "the evidence was compared with the %r baseline, but the robot's state before the power cycle was %r"
            % (evidence.baseline_state, expected_state)],
            "gather the evidence again against the %r baseline" % expected_state)
    disclosures: list[Disclosure] = []
    info8_unavailable = fresh_codes is not None and _INFO_PROGRAM_LOADED in fresh_codes
    if info8_unavailable:
        disclosures.append(_INFO8_UNAVAILABLE)
    base = None if baselines is None else baselines.get(expected_state)
    contradictions: list[str] = []
    for code in _CONTRADICTING:
        if code not in codes:
            continue
        if code == _INFO_PROGRAM_LOADED and info8_unavailable:
            continue
        if base is None:
            contradictions.append(
                "the replay has INFO;%d and no %r restart baseline is recorded for this robot (rung %s records it; "
                "before the robot is identified no baseline is trusted)"
                % (code, expected_state, _BASELINE_RUNG[expected_state]))
        elif code not in base.codes:
            contradictions.append("the replay has INFO;%d, which the %r baseline %s (rung %s) does not"
                                  % (code, expected_state, _fmt(set(base.codes)), base.rung))
    if contradictions:
        return verdict(False, None, contradictions,
                       "power-cycle again at the controller's power switch and wait 10 s before switching on; if "
                       "this persists, the %r baseline is wrong or missing: repeat rung %s"
                       % (expected_state, _BASELINE_RUNG[expected_state]), tuple(disclosures))
    if evidence.link_drop_observed:
        return verdict(True, "link-drop", ["the link dropped while the controller was off, and the replay "
                                           "%s does not contradict the baseline" % _fmt(codes)],
                       None, tuple(disclosures))
    if codes:
        return verdict(True, "replay-consistent",
                       ["the replay %s does not contradict the %r baseline" % (_fmt(codes), expected_state)],
                       None, tuple(disclosures))
    disclosures.append(_ATTESTATION_ONLY)
    return verdict(True, "attestation-only",
                   ["the operator attested the power cycle; no replay arrived (a missing replay never blocks)"],
                   None, tuple(disclosures))


def _signal_code(name: str) -> int:
    return int(name.split(";", 1)[1])


def _fmt(codes: Iterable[int]) -> str:
    items = sorted(set(codes))
    return "{%s}" % ", ".join("INFO;%d" % c for c in items) if items else "{} (empty)"


def _satisfies(verdict: Verdict, need: Requirement) -> bool:
    """Whether a clearing made before the robot was identified also clears a ledger that needs ``need``."""
    if not verdict.cleared:
        return False
    if need == "upload-mode":
        return verdict.positive_power_loss
    if need == "after-job":
        return _RANK[verdict.rule] >= _RANK["after-job"] or verdict.positive_power_loss
    return True


def _cleared(state: LedgerState, clearing: Clearing) -> LedgerState:
    # The robot was power-cycled: whatever phase was last noted is over (the session notes its next one).
    return replace(state, upload_in_flight=None, pending_power_cycle=(), hazards=(),
                   uploads_since_attested_power_cycle=0, last_known_phase=None, last_may_be_moving=False,
                   last_clearing=clearing)


# ---------------------------------------------------------------------------
# The ledger of one session
# ---------------------------------------------------------------------------


def _require_lock(lock: object) -> None:
    held = getattr(lock, "held", None)
    if not callable(held):
        raise TypeError("lock must have a held() method (the RobotLock, unit L09), got %s" % type(lock).__name__)
    if held() is not True:
        raise LedgerLockError("the robot lock is not held by this process: the ledger is read-modify-written only "
                              "under it (the project design, section 3.4); nothing was read or written")


class Ledger:
    """The ledger a session writes: opened by address, bound to the robot's serial after IDENTIFY.

    Create it with :meth:`open_for_address`. Every method that writes takes the robot lock check first, re-reads
    the file and refuses a file changed behind the session's back. Methods are safe to call from several threads.
    """

    def __init__(self, *, data_dir: Path, address: str, lock: LockLike, state: LedgerState, path: Path,
                 on_disk: bool, pending_elsewhere: tuple[LedgerState, ...],
                 unreadable_elsewhere: tuple[tuple[Path, str], ...]) -> None:
        self._data_dir = Path(data_dir)
        self._address = address
        self._lock = lock
        self._state = state
        self._path = path
        self._on_disk = on_disk
        self._serial: str | None = None
        self._pending_elsewhere = pending_elsewhere
        self._unreadable_elsewhere = unreadable_elsewhere
        self._session_clearing: Verdict | None = None
        self._session_begun = False
        self._mutex = threading.RLock()

    @classmethod
    def open_for_address(cls, data_dir: Path, address: str, *, lock: LockLike) -> Ledger:
        """The session's ledger for the robot at ``address``, before its serial is known.

        Scans every ledger: the readable ones that need a power cycle are :attr:`pending_elsewhere` and the
        unreadable ones (interrupted writes included, this address's own too) :attr:`unreadable_elsewhere`;
        until :meth:`bind_serial`, :meth:`requirement` is the
        strictest of them all, since any of those robots may be the one at this address. Raises
        :class:`LedgerUnreadableError` if this address's own ledger cannot be trusted.
        """
        _check_address(address)
        _require_lock(lock)
        data_dir = Path(data_dir)
        path = _unbound_path(data_dir, address)
        scan = scan_ledgers(data_dir)
        if path in scan.reasons:
            raise LedgerUnreadableError(path, scan.reasons[path])
        own = next((s for s in scan.ledgers if s.path == path), None)
        return cls(data_dir=data_dir, address=address, lock=lock,
                   state=own if own is not None else LedgerState.empty(address), path=path,
                   on_disk=own is not None,
                   pending_elsewhere=tuple(s for s in scan.pending if s.path != path),
                   unreadable_elsewhere=tuple((p, scan.reasons[p]) for p in scan.unreadable))

    # -- views ---------------------------------------------------------------

    @property
    def state(self) -> LedgerState:
        """This ledger's content (the address ledger before binding, the robot's after)."""
        with self._mutex:
            return self._state

    @property
    def path(self) -> Path:
        return self._path

    @property
    def address(self) -> str:
        return self._address

    @property
    def serial(self) -> str | None:
        return self._serial

    @property
    def pending_elsewhere(self) -> tuple[LedgerState, ...]:
        """Other ledgers that needed a power cycle when this one was opened (any of them may be this robot)."""
        return self._pending_elsewhere

    @property
    def unreadable_elsewhere(self) -> tuple[tuple[Path, str], ...]:
        return self._unreadable_elsewhere

    def requirement(self) -> Requirement:
        """What clearing needs now: this ledger's own once bound; before that the strictest of every ledger.

        Before binding, a power cycle already accepted in this session covers the other ledgers it is strong
        enough for (it is applied to the robot's own ledger when the robot identifies itself).
        """
        with self._mutex:
            if self._serial is not None:
                return self._state.requirement
            clearing = self._session_clearing
            parts = [self._state.requirement]
            parts += [s.requirement for s in self._pending_elsewhere
                      if clearing is None or not _satisfies(clearing, s.requirement)]
            if self._unreadable_elsewhere and (clearing is None or not clearing.positive_power_loss):
                parts.append("upload-mode")
            return _strictest(parts)

    def _expected_state(self) -> BaselineState:
        if self._serial is not None:
            return self._state.restart_baseline_state
        states = [self._state.restart_baseline_state] + [s.restart_baseline_state for s in self._pending_elsewhere]
        return max(states, key=lambda s: _STATE_RANK[s])

    # -- writing ------------------------------------------------------------

    def _check_unchanged(self) -> None:
        try:
            disk: LedgerState | None = _read_state(self._path)
        except FileNotFoundError:
            disk = None
        expected = self._state if self._on_disk else None
        if (None if disk is None else disk.to_json()) != (None if expected is None else expected.to_json()):
            raise LedgerStateError(
                "the ledger %s changed on disk outside this session (another program, a second session without "
                "the robot lock, or a hand edit); nothing was written. Re-open the ledger to see what it says now"
                % self._path)

    def _mutate(self, change: Callable[[LedgerState], LedgerState]) -> LedgerState:
        with self._mutex:
            _require_lock(self._lock)
            self._check_unchanged()
            new = replace(change(self._state), updated_at=_utc_now(), path=self._path)
            try:
                atomic_write_bytes(self._path, _json_bytes(new.to_json()))
            except BaseException:
                self._resync(new)
                raise
            self._state = new
            self._on_disk = True
            return new

    def _resync(self, new: LedgerState) -> None:
        """After a failed write: if the rename happened (a later fsync failed), memory follows the disk."""
        try:
            disk = _read_state(self._path)
        except (FileNotFoundError, LedgerUnreadableError) as exc:
            log.warning("after a failed write, %s could not be re-read: %s", self._path, exc)
            return
        if disk.to_json() == new.to_json():
            self._state = new
            self._on_disk = True

    def bind_serial(self, serial: str) -> None:
        """Bind the session to the robot that identified itself (INFO;34), as soon as its serial is known.

        Loads the robot's own ledger (an unreadable one raises: fail closed), carries over this session and
        everything the address ledger recorded before IDENTIFY, applies a clearing made before IDENTIFY if it
        is strong enough for this robot, writes the robot's ledger and the pointer, then removes the address
        ledger. Binding again to the same serial does nothing; another serial is refused.

        An interrupted write of the robot's ledger or of this address's ledger (a temporary file a killed writer
        left) is an unknown: without a power cycle shown by positive evidence in this session the binding is
        refused (:class:`LedgerUnreadableError` naming the file, nothing written); with one, the file is moved
        aside after the binding (an ERROR is logged if that fails; the next scan then still fails closed on it).
        """
        _check_serial(serial)
        with self._mutex:
            _require_lock(self._lock)
            if self._serial == serial:
                return
            if self._serial is not None:
                raise LedgerStateError(
                    "this session's ledger is bound to robot %s, but robot %s answered: a different robot is at "
                    "%s. End this session; nothing was changed" % (self._serial, serial, self._address))
            self._check_unchanged()
            target = _serial_path(self._data_dir, serial)
            leftovers = _ledger_temps(target.parent) + _ledger_temps(self._path.parent)
            clearing = self._session_clearing
            if leftovers and not (clearing is not None and clearing.positive_power_loss):
                raise LedgerUnreadableError(leftovers[0], _INTERRUPTED_REASON + "; nothing was bound: power-cycle "
                                            "the robot with positive evidence (attest_power_cycle) first")
            try:
                base: LedgerState = _read_state(target, serial=serial)
                existed = True
            except FileNotFoundError:
                base = LedgerState.empty(self._address, serial)
                existed = False
            now = _utc_now()
            merged = self._merge(base, now)
            merged = replace(merged, updated_at=now, path=target)
            atomic_write_bytes(target, _json_bytes(merged.to_json()))
            pointer = Pointer(address=self._address, serial=serial, updated_at=now)
            atomic_write_bytes(_pointer_path(self._data_dir), _json_bytes(pointer.to_json()))
            unbound, unbound_on_disk = self._path, self._on_disk
            # The robot's ledger and the pointer are written: from here on the session is bound.
            self._serial = serial
            self._path = target
            self._state = merged
            self._on_disk = True
            log.info("ledger bound to robot %s at %s (%s)", serial, self._address,
                     "existing ledger" if existed else "new ledger")
            if leftovers:
                stamp = _quarantine_stamp()
                for leftover in leftovers:
                    try:
                        aside = _move_aside(leftover, stamp)
                    except OSError as exc:
                        log.error("could not move %s aside after the power cycle shown in this session (%s): %s. "
                                  "The next scan fails closed on it again", leftover, clearing.route, exc)
                        continue
                    log.warning("interrupted write %s moved aside to %s: the power cycle shown in this session (%s) "
                                "covers whatever it was recording", leftover, aside, clearing.route)
            if unbound_on_disk:
                try:
                    unbound.unlink()
                except PermissionError as exc:
                    # Its content is in the robot's ledger now; left in place it only adds caution to the next
                    # scan, but say so, and say what did change.
                    raise LedgerBusyError(
                        unbound, exc, action="remove",
                        outcome="The session is bound to robot %s all the same: its ledger %s and the pointer were "
                                "written. The leftover address ledger is merged again (nothing twice) when a session "
                                "at %s binds next; until then every scan reads it as well"
                                % (serial, target, self._address)
                    ) from exc
                _fsync_directory(unbound.parent)

    def _merge(self, base: LedgerState, now: str) -> LedgerState:
        pending = list(base.pending_power_cycle)
        if base.unclosed_session_may_be_moving:
            pending.append(PendingPowerCycle(reason=_unclosed_reason(base), at=now))
        merged = replace(base, pending_power_cycle=tuple(pending), session_active=None)
        clearing = self._session_clearing
        need = merged.requirement
        if clearing is not None and need != "none":
            if _satisfies(clearing, need):
                own = self._state.last_clearing
                if own is None:
                    raise LedgerStateError("internal inconsistency: a clearing was accepted before IDENTIFY but the "
                                           "address ledger %s does not record it; nothing was bound" % self._path)
                merged = _cleared(merged, replace(own, note="made before the robot was identified; applied when "
                                                            "it identified itself as %s" % base.serial))
                log.warning("the power cycle attested before IDENTIFY clears robot %s's ledger (%s)",
                            base.serial, clearing.route)
            else:
                log.warning("the power cycle attested before IDENTIFY is not enough for robot %s, which needs %s "
                            "(positive power-loss evidence): its ledger stays pending", base.serial, need)
        unbound = self._state
        return replace(
            merged, address=self._address,
            hazards=_union(merged.hazards, unbound.hazards),
            pending_power_cycle=_union(merged.pending_power_cycle, unbound.pending_power_cycle),
            session_active=unbound.session_active,
            last_known_phase=unbound.last_known_phase if unbound.session_active else merged.last_known_phase,
            last_may_be_moving=(unbound.last_may_be_moving if unbound.session_active
                                else merged.last_may_be_moving))

    def begin_session(self, pid: int, address: str) -> None:
        """Record ``session_active`` at CONNECTING. A previous session that never closed while the robot may
        have been moving becomes a pending power cycle."""
        _count("begin_session", "pid", pid, minimum=1)
        _check_address(address)
        if address != self._address:
            raise ValueError("begin_session: address %r is not this ledger's address %r" % (address, self._address))
        with self._mutex:
            if self._session_begun:
                raise LedgerStateError("a session is already active on this ledger; end it first")
            now = _utc_now()

            def change(s: LedgerState) -> LedgerState:
                pending = s.pending_power_cycle
                if s.unclosed_session_may_be_moving:
                    pending += (PendingPowerCycle(reason=_unclosed_reason(s), at=now),)
                elif s.session_active is not None:
                    log.warning("the previous session of pid %d (started %s) did not close; its last phase %s was "
                                "not one where the robot may be moving", s.session_active.pid,
                                s.session_active.started_at, s.last_known_phase)
                return replace(s, pending_power_cycle=pending,
                               session_active=SessionActive(pid=pid, address=address, started_at=now),
                               last_known_phase=Phase.CONNECTING.name, last_may_be_moving=False)

            self._mutate(change)
            self._session_begun = True

    def note_phase(self, phase: Phase, *, may_be_moving: bool) -> None:
        """Record the session's phase and whether the robot may be moving (the snapshot's flag)."""
        if not isinstance(phase, Phase):
            raise TypeError("phase must be a Phase, got %s" % type(phase).__name__)
        _bool("note_phase", "may_be_moving", may_be_moving)
        if PHASE_INFO[phase].may_be_moving and not may_be_moving:
            raise ValueError("in phase %s the robot may be moving; it cannot be recorded as not moving" % phase.name)
        self._mutate(lambda s: replace(s, last_known_phase=phase.name, last_may_be_moving=may_be_moving))

    def end_session(self, reason: str) -> None:
        """Clear ``session_active``. Ending while the robot may be moving leaves a pending power cycle."""
        _text("end_session", "reason", reason)
        with self._mutex:
            now = _utc_now()

            def change(s: LedgerState) -> LedgerState:
                if s.session_active is None:
                    raise LedgerStateError("no session is active in %s" % self._path)
                pending = s.pending_power_cycle
                if s.last_may_be_moving:
                    pending += (PendingPowerCycle(
                        reason="the session ended (%s) while the robot may be moving (last phase %s)"
                               % (reason, s.last_known_phase), at=now),)
                return replace(s, session_active=None, pending_power_cycle=pending)

            self._mutate(change)
            self._session_begun = False

    def begin_upload(self, job_id: str, sha256: str) -> None:
        """Record the upload in flight; the new ledger is fsync'd and renamed into place before this returns.

        Call it from the upload's ``header-about-to-send`` stage: if it raises, no byte is sent. Refused before
        the ledger is bound to the robot's serial, with an upload already in flight, with a hazard, with a
        pending power cycle, and after an upload since the last attested power cycle (one upload per power
        cycle until rung R12).
        """
        _pattern("begin_upload", "job_id", job_id, _JOB_ID, "a job id (letters, digits, '.', '_', '-'; at most 64)")
        _pattern("begin_upload", "sha256", sha256, _SHA256, "a lowercase hex SHA-256")
        with self._mutex:
            _require_lock(self._lock)
            if self._serial is None:
                raise LedgerStateError("the ledger is not bound to the robot's serial yet: an upload needs IDENTIFY "
                                       "first")
            now = _utc_now()

            def change(s: LedgerState) -> LedgerState:
                if s.upload_in_flight is not None:
                    raise LedgerStateError("an upload is already in flight (job %s since %s)"
                                           % (s.upload_in_flight.job_id, s.upload_in_flight.began_at))
                if s.hazards:
                    raise LedgerStateError("an upload hazard is recorded (%s): power-cycle with evidence first"
                                           % "; ".join(h.reason for h in s.hazards))
                if s.pending_power_cycle:
                    raise LedgerStateError("a power cycle is pending (%s): power-cycle and attest first"
                                           % "; ".join(p.reason for p in s.pending_power_cycle))
                if s.uploads_since_attested_power_cycle > 0:
                    raise LedgerStateError("%d upload(s) since the last attested power cycle: one upload per power "
                                           "cycle (vendor rule; only rung R12 can lift it)"
                                           % s.uploads_since_attested_power_cycle)
                return replace(s, upload_in_flight=UploadInFlight(job_id=job_id, sha256=sha256, began_at=now),
                               uploads_since_attested_power_cycle=s.uploads_since_attested_power_cycle + 1)

            self._mutate(change)
            log.warning("ledger %s: upload of job %s in flight (fsync'd before the header)", self._path, job_id)

    def end_upload(self, signal: str) -> None:
        """Record the end of the upload and of the job: ``"INFO;8"``, ``"INFO;0"`` or a work-start code
        ``"INFO;10"``-``"INFO;17"`` when the robot leaves upload mode (design 7.5), ``"INFO;23"`` when the job ends,
        or both at once (``"INFO;8+INFO;23"``). Any other name is refused (ValueError) and nothing is recorded:
        only these show that the robot is no longer in upload mode.

        ``upload_in_flight`` clears only once an end-of-upload signal and INFO;23 are both recorded; the robot
        then needs a power cycle before the next job (one upload per power cycle).
        """
        _text("end_upload", "signal", signal)
        tokens = [t.strip() for t in signal.split("+")]
        for token in tokens:
            if token not in _STREAM_END_SIGNALS and token != _JOB_END_SIGNAL:
                raise ValueError(
                    "end_upload: %r is not an end-of-upload signal (%s) or the job's end (%s); nothing was recorded, "
                    "the upload stays in flight" % (token, ", ".join(sorted(_STREAM_END_SIGNALS, key=_signal_code)),
                                                     _JOB_END_SIGNAL))
        done = _JOB_END_SIGNAL in tokens
        stream = [t for t in tokens if t != _JOB_END_SIGNAL]
        with self._mutex:
            now = _utc_now()

            def change(s: LedgerState) -> LedgerState:
                up = s.upload_in_flight
                if up is None:
                    raise LedgerStateError("no upload is in flight in %s" % self._path)
                if stream and up.stream_end is None:
                    up = replace(up, stream_end="+".join(stream), stream_end_at=now)
                elif stream:
                    log.warning("end-of-upload signal %s recorded again (first: %s)", "+".join(stream),
                                up.stream_end)
                if not done:
                    return replace(s, upload_in_flight=up)
                if up.stream_end is None:
                    raise LedgerStateError(
                        "INFO;23 without a recorded end-of-upload signal (INFO;8 or INFO;0): the upload's end is "
                        "not established, so the upload stays in flight; record the end-of-upload signal first")
                reason = ("job %s completed (%s, then INFO;23): one upload per power cycle (vendor rule, spec 03; "
                          "only rung R12 can lift it)" % (up.job_id, up.stream_end))
                return replace(s, upload_in_flight=None,
                               pending_power_cycle=s.pending_power_cycle + (PendingPowerCycle(reason=reason, at=now),))

            self._mutate(change)

    def require_power_cycle(self, reason: str) -> None:
        """Record that the robot must be power-cycled before anything else (design 7.7 A)."""
        _text("require_power_cycle", "reason", reason)
        now = _utc_now()
        self._mutate(lambda s: replace(s, pending_power_cycle=s.pending_power_cycle
                                       + (PendingPowerCycle(reason=reason, at=now),)))
        log.warning("ledger %s: power cycle required: %s", self._path, reason)

    def mark_upload_hazard(self, reason: str, kind: HazardKind) -> None:
        """Record that the robot may be in upload mode (design 7.7 B; red-team PHYS-02)."""
        _text("mark_upload_hazard", "reason", reason)
        _choice("mark_upload_hazard", "kind", kind, HAZARD_KINDS)
        now = _utc_now()
        self._mutate(lambda s: replace(s, hazards=s.hazards + (Hazard(kind=kind, reason=reason, at=now),)))
        log.error("ledger %s: upload hazard (%s): %s", self._path, kind, reason)

    def attest_power_cycle(self, evidence: RestartEvidence, *, attestation: str) -> Verdict:
        """Apply :func:`restart_verdict` to what is pending; on success clear it and record the clearing.

        ``attestation`` is the operator's typed K6 text, kept verbatim. Before :meth:`bind_serial` the rule is
        judged against every pending ledger and no baseline is trusted; the robot's own ledger is cleared when
        it identifies itself, if the clearing is strong enough for what it needs.
        """
        if not isinstance(evidence, RestartEvidence):
            raise TypeError("evidence must be a RestartEvidence, got %s" % type(evidence).__name__)
        _text("attest_power_cycle", "attestation", attestation)
        with self._mutex:
            _require_lock(self._lock)
            bound = self._serial is not None
            verdict = restart_verdict(self.requirement(), self._expected_state(), evidence, attestation=attestation,
                                      baselines=self._state.restart_baselines if bound else None)
            if not verdict.cleared:
                log.warning("power cycle not accepted: %s. Remedy: %s", "; ".join(verdict.reasons), verdict.remedy)
                return verdict
            clearing = Clearing(at=_utc_now(), rule=verdict.rule, route=verdict.route,  # type: ignore[arg-type]
                                positive_power_loss=verdict.positive_power_loss, attestation=attestation,
                                evidence=evidence, disclosures=verdict.disclosures,
                                note=None if bound else "made before the robot was identified")
            self._mutate(lambda s: _cleared(s, clearing))
            if not bound:
                self._session_clearing = verdict
            log.warning("power cycle accepted (%s, %s): %s", verdict.rule, verdict.route, "; ".join(verdict.reasons))
            return verdict

    def record_rung_result(self, rung: str, result: Mapping[str, Any]) -> None:
        """Record a rung's result (a JSON object) for this robot, with the time."""
        _pattern("record_rung_result", "rung", rung, _RUNG, "a rung id such as R1 or R3b")
        if not isinstance(result, Mapping):
            raise TypeError("result must be a JSON object, got %s" % type(result).__name__)
        entry = FrozenMap({**dict(result), "at": _utc_now()})
        with self._mutex:
            self._require_bound("a rung result")
            self._mutate(lambda s: replace(s, rung_results=FrozenMap({**s.rung_results.to_json(),
                                                                      rung: entry.to_json()})))

    def record_restart_baseline(self, state: BaselineState, replay_codes: Iterable[int], *, rung: str) -> None:
        """Record the replay codes a passive reconnect showed after a power cycle in ``state`` (design 7.7 A)."""
        _choice("record_restart_baseline", "state", state, BASELINE_STATES)
        baseline = RestartBaseline(codes=tuple(replay_codes), rung=rung, at=_utc_now())
        with self._mutex:
            self._require_bound("a restart baseline")
            old = self._state.restart_baselines.get(state)
            if old is not None and old.codes != baseline.codes:
                log.warning("restart baseline %r of robot %s changes from %s (rung %s) to %s (rung %s)", state,
                            self._serial, _fmt(old.codes), old.rung, _fmt(baseline.codes), rung)
            self._mutate(lambda s: replace(s, restart_baselines={**s.restart_baselines, state: baseline}))

    def _require_bound(self, what: str) -> None:
        _require_lock(self._lock)
        if self._serial is None:
            raise LedgerStateError("%s belongs to a robot: bind the ledger to the robot's serial first" % what)


# ---------------------------------------------------------------------------
# Moving an unreadable file aside
# ---------------------------------------------------------------------------


def quarantine_unreadable(data_dir: Path, path: Path, evidence: RestartEvidence, *, attestation: str,
                          lock: LockLike) -> Verdict:
    """Move an unreadable ledger or pointer aside after a power cycle with positive power-loss evidence.

    Nothing is known about what the file recorded, so the rule is the strictest one (design 7.7 B: an observed
    link drop or the fresh-boot replay; no baseline is trusted). The file is renamed to
    ``<name>.unreadable-<UTC>`` next to where it was (an interrupted write's temporary file to
    ``ledger.json.unreadable-<UTC>-<its name>``), never deleted. A file that reads fine is refused. When the
    file is a robot's ledger and the pointer names that robot, the pointer is moved aside with it (the same power
    cycle covers it), so the recovery does not leave a pointer to a missing ledger, which the scan refuses.
    """
    _require_lock(lock)
    data_dir, path = Path(data_dir), Path(path)
    robots = _robots(data_dir).resolve()
    resolved = path.resolve()
    if (resolved.name not in (LEDGER_NAME, POINTER_NAME) and not _is_ledger_temp(resolved)) \
            or robots not in resolved.parents:
        raise ValueError("%s is not a ledger, pointer or interrupted-write file of %s" % (path, data_dir))
    scan = scan_ledgers(data_dir)
    if not any(p.resolve() == resolved for p in scan.unreadable):
        if not path.exists():
            raise FileNotFoundError("%s does not exist" % path)
        raise LedgerStateError("%s reads fine: there is nothing to quarantine" % path)
    verdict = restart_verdict("upload-mode", evidence.baseline_state, evidence, attestation=attestation,
                              baselines=None)
    if not verdict.cleared:
        return verdict
    stamp = _quarantine_stamp()
    try:
        aside = _move_aside(path, stamp)
    except PermissionError as exc:
        raise LedgerBusyError(path, exc) from exc
    log.warning("unreadable file %s moved aside to %s after a power cycle (%s); attestation: %r", path, aside,
                verdict.route, attestation)
    pointer = scan.pointer
    if (resolved.name == LEDGER_NAME and resolved.parent.parent == robots and pointer is not None
            and pointer.serial == resolved.parent.name):
        pointer_path = _pointer_path(data_dir)
        try:
            pointer_aside = _move_aside(pointer_path, stamp)
        except PermissionError as exc:
            raise LedgerBusyError(
                pointer_path, exc, action="move aside",
                outcome="The unreadable ledger %s was moved aside to %s; the pointer still names robot %s, so the "
                        "next scan reports it as a pointer to a missing ledger: quarantine it with the same "
                        "evidence" % (path, aside, pointer.serial)) from exc
        log.warning("pointer %s named robot %s, whose ledger was moved aside: moved aside to %s as well",
                    pointer_path, pointer.serial, pointer_aside)
    return verdict


def default_data_dir() -> Path:
    """The per-user data folder for ledgers, facts and sessions (``platformdirs``; Windows and Linux)."""
    import platformdirs

    return Path(platformdirs.user_data_dir("goliath-run", appauthor=False))
