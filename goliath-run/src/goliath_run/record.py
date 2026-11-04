# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The session record: the black box of one job (unit R02; the project design, section 7.9; red-team C-18).

One folder per session, ``<root>/<UTC>-<job id>/`` (``root`` is normally
``<data_dir>/sessions``)::

    record.json         job id, creation time, recorder policy, software version
    wire/S1.bin S2.bin S3.bin + index.jsonl
                        every byte per connection, through GoliathTransport(recorder=record.recorder);
                        index.jsonl locates each chunk (connection, direction, offset, length,
                        monotonic time t, wall time)
    events.jsonl        decoded link records (event())
    states.jsonl        phase transitions with their cause (state())
    operator.jsonl      operator confirmations, verbatim with the text shown (operator())
    positions.csv       t, x, y, z, angle, line, cut, deviation, wheel slack (position())
    private.json        serials and SSIDs seen (note_private()): the values export_diag redacts; never exported
    summary.json        outcome class, reasons, disclosures, wire statistics (finish())
    ...                 other files the runner adds with write_file() (upload.nc, certificate.json, ...)

Append-only: nothing is overwritten. State transitions and operator answers
are fsync'd before their method returns; everything else at least once a
second while events arrive (design 7.9).

**Size** (red-team C-18). Every 01.02.00 message, position frames included, is
NUL-padded to 512 bytes and the frame rate is unknown, so the raw bytes are kept
under a per-session cap (:class:`RecorderPolicy`). When the cap is reached the
raw bytes stop being recorded, a red sticky disclosure is raised (once; also
passed to ``on_disclosure``) and an ERROR is logged; decoded events, states and
operator answers are still recorded, and the summary counts the bytes dropped.
Old sessions are pruned only after export or the operator's confirmation
(:func:`prune_sessions`). Before arming, :func:`free_disk_ok` checks the free
disk for the estimated duration at 200 MiB per hour plus a 1 GiB reserve.

The recorder hook's signature is the transport's: ``recorder(conn: int,
direction: str, data: bytes, t: float) -> None``. If it raises, the transport
takes the link down with that error (a capture that silently misses data is
worse); it raises only for a programming error or a failing disk.
"""
from __future__ import annotations

import csv
import dataclasses
import enum
import io
import json
import logging
import math
import os
import re
import shutil
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import IO, Any

from goliath_link.protocol import TextMessage
from goliath_prep.core.canon import canonical_json
from goliath_prep.core.disclosure import Disclosure
from goliath_run import __version__
from goliath_run.ledger import atomic_write_bytes
from goliath_run.types import Phase

__all__ = [
    "EXPORTS_NAME",
    "OUTCOMES",
    "PRIVATE_NAME",
    "RECORDER_RATE_BYTES_PER_HOUR",
    "RECORDER_RESERVE_BYTES",
    "RECORD_NAME",
    "SUMMARY_NAME",
    "WIRE_DIR",
    "DiskCheck",
    "JobRecord",
    "PruneReport",
    "RecordClosedError",
    "RecorderPolicy",
    "free_disk_ok",
    "mark_exported",
    "prune_sessions",
]

log = logging.getLogger("goliath_run.record")

MiB = 1024 * 1024
RECORDER_RATE_BYTES_PER_HOUR = 200 * MiB
"""Disk budget per hour of job for the free-disk check (the project design, section 7.9; red-team C-18). At the
100 Hz frame rate of press reports, 512-byte frames would need about 184 MB per hour."""
RECORDER_RESERVE_BYTES = 1024 * MiB
"""Free disk kept in reserve on top of the per-hour budget (design 7.9)."""
_DISK_BASIS = "the project design, section 7.9 (red-team C-18): 200 MiB per hour of job plus a 1 GiB reserve"

OUTCOMES: tuple[str, ...] = (
    "COMPLETED",
    "COMPLETED_UNCONFIRMED_UPLOAD",
    "ENDED_LINK_LOST",
    "FAULTED_ROBOT_ERR",
    "STOPPED_PHYSICALLY",
    "COMPLETED_END_UNCONFIRMED",
    "REFUSED_BEFORE_UPLOAD",
    "ABORTED_BEFORE_UPLOAD",
)
"""The outcome classes of a session (design 7.9)."""

RECORD_NAME = "record.json"
SUMMARY_NAME = "summary.json"
PRIVATE_NAME = "private.json"
EXPORTS_NAME = "exports.jsonl"
WIRE_DIR = "wire"
_EVENTS = "events.jsonl"
_STATES = "states.jsonl"
_OPERATOR = "operator.jsonl"
_POSITIONS = "positions.csv"
_INDEX = "index.jsonl"
_RESERVED = frozenset({RECORD_NAME, SUMMARY_NAME, PRIVATE_NAME, EXPORTS_NAME, WIRE_DIR, _EVENTS, _STATES, _OPERATOR,
                       _POSITIONS})
_POSITION_COLUMNS = ("t", "x", "y", "z", "angle", "line", "cut", "deviation", "wheel_slack")
_CONNECTIONS = (0, 1, 2)
_DIRECTIONS = ("<", ">")
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"] + ["COM%d" % i for i in range(1, 10)] + ["LPT%d" % i for i in range(1, 10)])
_PRIVATE_KINDS = {"serial": "serials", "ssid": "ssids"}
_SYNC_INTERVAL_S = 1.0


class RecordClosedError(RuntimeError):
    """A write to a session record after :meth:`JobRecord.finish`."""


def _wall(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _text(name: str, value: object, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise TypeError("%s must be a str, got %s" % (name, type(value).__name__))
    if not allow_empty and not value.strip():
        raise ValueError("%s must not be empty" % name)
    return value


def _finite(name: str, value: object) -> float:
    if type(value) not in (int, float):
        raise TypeError("%s must be a number, got %s" % (name, type(value).__name__))
    if not math.isfinite(value):  # type: ignore[arg-type]
        raise ValueError("%s must be finite, got %r" % (name, value))
    return float(value)  # type: ignore[arg-type]


def _job_id(value: object) -> str:
    _text("job_id", value)
    if not _JOB_ID.match(value) or value.endswith(".") or value.split(".", 1)[0].upper() in _WINDOWS_RESERVED:  # type: ignore[union-attr]
        raise ValueError("job_id %r: use 1-64 letters, digits, '.', '_' or '-', starting with a letter or digit"
                         % (value,))
    return value  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecorderPolicy:
    """The recorder's size cap and retention, with where the numbers come from.

    * ``max_session_mb`` - raw wire bytes kept per session, in MiB; beyond it the raw bytes stop (red disclosure);
    * ``keep_sessions`` - the newest sessions :func:`prune_sessions` never removes;
    * ``source`` - provenance of these values (settings, owner, or the project default): never implicit.
    """

    max_session_mb: int
    keep_sessions: int
    source: str

    def __post_init__(self) -> None:
        for name in ("max_session_mb", "keep_sessions"):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError("RecorderPolicy.%s must be an int, got %s" % (name, type(value).__name__))
            if value < 1:
                raise ValueError("RecorderPolicy.%s must be at least 1, got %d" % (name, value))
        _text("RecorderPolicy.source", self.source)

    @property
    def max_session_bytes(self) -> int:
        return self.max_session_mb * MiB

    @classmethod
    def project_default(cls) -> RecorderPolicy:
        """The project's default: an INFERRED choice (the design gives no number). Disclose it (:meth:`disclosure`)."""
        return cls(max_session_mb=4096, keep_sessions=20,
                   source="INFERRED project default (the project design, section 7.9, sets no number): 4096 MiB "
                          "holds about 20 hours at the 200 MiB per hour the free-disk check budgets; the newest "
                          "20 sessions are always kept")

    def disclosure(self) -> Disclosure:
        default = self.source.startswith("INFERRED")
        return Disclosure(
            id="DEFAULT:recorder_policy" if default else "RECORDER:policy", severity="info",
            text="session recorder: raw bytes capped at %d MiB per session; the newest %d sessions are never pruned"
                 % (self.max_session_mb, self.keep_sessions),
            source=self.source, sticky=True)

    def to_json(self) -> dict[str, Any]:
        return {"max_session_mb": self.max_session_mb, "keep_sessions": self.keep_sessions, "source": self.source}


# ---------------------------------------------------------------------------
# JSON conversion of link records
# ---------------------------------------------------------------------------


class _Unserialisable(TypeError):
    pass


def _jsonable(value: object) -> object:
    if value is None or type(value) in (bool, int, str):
        return value
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else {"float": repr(float(value))}
    if isinstance(value, enum.Enum):
        return value.name
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"hex": bytes(value).hex()}
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {"type": type(value).__name__, "fields": _fields(value)}
    if isinstance(value, tuple) and hasattr(value, "_asdict"):
        return {"type": type(value).__name__, "fields": {k: _jsonable(v) for k, v in value._asdict().items()}}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise _Unserialisable("a mapping key %r is not a str" % (key,))
            out[key] = _jsonable(item)
        return out
    raise _Unserialisable("%s is not a JSON value" % type(value).__name__)


def _fields(value: object) -> dict[str, object]:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, tuple) and hasattr(value, "_asdict"):
        return {k: _jsonable(v) for k, v in value._asdict().items()}  # type: ignore[attr-defined]
    if isinstance(value, Mapping):
        return _jsonable(value)  # type: ignore[return-value]
    raise _Unserialisable("%s has no fields" % type(value).__name__)


def _line(obj: Mapping[str, object]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, allow_nan=False, sort_keys=False) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


class JobRecord:
    """The black box of one session. Create it with :meth:`create`; every method is thread-safe."""

    def __init__(self, directory: Path, job_id: str, policy: RecorderPolicy, *,
                 on_disclosure: Callable[[Disclosure], None] | None, clock: Callable[[], datetime],
                 monotonic: Callable[[], float], created_at: str) -> None:
        self.dir = directory
        self.job_id = job_id
        self.policy = policy
        self.created_at = created_at
        self._on_disclosure = on_disclosure
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._closed = False
        self._files: dict[str, IO[bytes]] = {}
        self._offsets = [0, 0, 0]
        self._recorded = [0, 0, 0]
        self._dropped = [0, 0, 0]
        self._cap_reached = False
        self._disclosures: list[Disclosure] = []
        self._private: dict[str, list[str]] = {"serials": [], "ssids": []}
        self._last_sync = monotonic()
        self._warned_types: set[str] = set()

    @classmethod
    def create(cls, root: Path, job_id: str, *, policy: RecorderPolicy,
               on_disclosure: Callable[[Disclosure], None] | None = None,
               clock: Callable[[], datetime] | None = None,
               monotonic: Callable[[], float] | None = None) -> JobRecord:
        """A new session folder ``<root>/<UTC>-<job_id>/``; an existing one is never reused (FileExistsError).

        ``policy`` is required: the size cap and retention are never implicit. ``on_disclosure`` receives the
        red disclosure when the cap is reached (it is called from the transport's thread). ``clock`` (an aware
        datetime, default UTC now) and ``monotonic`` (default ``time.monotonic``) are for tests.
        """
        _job_id(job_id)
        if not isinstance(policy, RecorderPolicy):
            raise TypeError("policy must be a RecorderPolicy, got %s" % type(policy).__name__)
        clock = clock or (lambda: datetime.now(timezone.utc))
        now = clock()
        if now.tzinfo is None:
            raise ValueError("the clock must return an aware datetime (UTC)")
        stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        directory = root / ("%s-%s" % (stamp, job_id))
        directory.mkdir()
        (directory / WIRE_DIR).mkdir()
        created_at = _wall(now)
        meta = {"schema_version": 1, "job_id": job_id, "created_at": created_at, "policy": policy.to_json(),
                "software": {"goliath_run": __version__}}
        atomic_write_bytes(directory / RECORD_NAME, canonical_json(meta))
        log.info("session record %s created (%s)", directory, policy.disclosure().text)
        return cls(directory, job_id, policy, on_disclosure=on_disclosure, clock=clock,
                   monotonic=monotonic or time.monotonic, created_at=created_at)

    # -- views ---------------------------------------------------------------

    @property
    def disclosures(self) -> tuple[Disclosure, ...]:
        """Disclosures the record itself raised (the recorder cap)."""
        with self._lock:
            return tuple(self._disclosures)

    @property
    def cap_reached(self) -> bool:
        with self._lock:
            return self._cap_reached

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    # -- internals -----------------------------------------------------------

    def _now(self) -> str:
        return _wall(self._clock())

    def _check_open(self) -> None:
        if self._closed:
            raise RecordClosedError("the session record %s is finished; nothing more can be written" % self.dir)

    def _file(self, name: str) -> IO[bytes]:
        f = self._files.get(name)
        if f is None:
            path = self.dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            f = open(path, "ab")  # binary: no newline translation on Windows
            self._files[name] = f
        return f

    def _append(self, name: str, data: bytes) -> None:
        f = self._file(name)
        f.write(data)
        f.flush()

    def _sync_locked(self) -> None:
        for f in self._files.values():
            f.flush()
            os.fsync(f.fileno())
        self._last_sync = self._monotonic()

    def _maybe_sync_locked(self) -> None:
        if self._monotonic() - self._last_sync >= _SYNC_INTERVAL_S:
            self._sync_locked()

    def sync(self) -> None:
        """Flush and fsync every open file of the record."""
        with self._lock:
            self._check_open()
            self._sync_locked()

    # -- the transport's recorder hook ------------------------------------------

    def recorder(self, conn: int, direction: str, data: bytes, t: float) -> None:
        """Record one chunk: ``conn`` 0-2 (S1-S3), ``direction`` ``'<'`` received or ``'>'`` sent, monotonic ``t``."""
        if type(conn) is not int or conn not in _CONNECTIONS:
            raise ValueError("recorder: conn must be 0, 1 or 2, got %r" % (conn,))
        if direction not in _DIRECTIONS or type(direction) is not str:
            raise ValueError("recorder: direction must be '<' or '>', got %r" % (direction,))
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("recorder: data must be bytes, got %s" % type(data).__name__)
        t = _finite("recorder: t", t)
        raised: Disclosure | None = None
        with self._lock:
            self._check_open()
            n = len(data)
            if self._cap_reached or sum(self._recorded) + n > self.policy.max_session_bytes:
                if not self._cap_reached:
                    raised = self._reach_cap_locked(t)
                self._dropped[conn] += n
            else:
                offset = self._offsets[conn]
                self._append("%s/S%d.bin" % (WIRE_DIR, conn + 1), bytes(data))
                self._offsets[conn] += n
                self._recorded[conn] += n
                self._append("%s/%s" % (WIRE_DIR, _INDEX), _line(
                    {"conn": conn, "dir": direction, "off": offset, "len": n, "t": t, "wall": self._now()}))
                self._maybe_sync_locked()
        if raised is not None and self._on_disclosure is not None:
            self._on_disclosure(raised)

    def _reach_cap_locked(self, t: float) -> Disclosure:
        self._cap_reached = True
        d = Disclosure(
            id="RECORDER:cap-reached", severity="red",
            text="the session recorder reached its cap of %d MiB: raw link bytes after this point are not recorded "
                 "(decoded events, states and operator answers still are)" % self.policy.max_session_mb,
            source="recorder policy: %s" % self.policy.source, sticky=True)
        self._disclosures.append(d)
        self._append("%s/%s" % (WIRE_DIR, _INDEX), _line({"cap_reached": True, "t": t, "wall": self._now(),
                                                          "max_session_mb": self.policy.max_session_mb}))
        self._sync_locked()
        log.error("session record %s: recorder cap of %d MiB reached; raw bytes are no longer recorded", self.dir,
                  self.policy.max_session_mb)
        return d

    # -- decoded records, states, operator answers, positions ---------------

    def event(self, rec: Any) -> None:
        """Record a decoded link record: a ``LinkEvent`` ``(conn, t, event)``, or a JSON object of the runner's."""
        if all(hasattr(rec, a) for a in ("conn", "t", "event")):
            conn, t, ev = rec.conn, rec.t, rec.event
            entry: dict[str, object] = {"wall": "", "t": _jsonable(t), "conn": conn, "type": type(ev).__name__}
        elif isinstance(rec, Mapping):
            ev = rec
            entry = {"wall": "", "t": None, "conn": None, "type": "runner"}
        else:
            raise TypeError("event() takes a LinkEvent or a JSON object, got %s" % type(rec).__name__)
        try:
            entry["fields"] = _fields(ev)
        except _Unserialisable as exc:
            entry["repr"] = repr(ev)[:4000]
            entry["unserialisable"] = True
            name = type(ev).__name__
            if name not in self._warned_types:
                self._warned_types.add(name)
                log.warning("session record %s: events of type %s cannot be recorded field by field (%s); their "
                            "repr is recorded instead, marked unserialisable", self.dir, name, exc)
        with self._lock:
            self._check_open()
            entry["wall"] = self._now()
            self._append(_EVENTS, _line(entry))
            self._maybe_sync_locked()
        if isinstance(ev, TextMessage):
            self._harvest(ev)

    def _harvest(self, msg: TextMessage) -> None:
        if msg.kind != "INFO":
            return
        if msg.code == 34:
            parts = msg.raw.split(";")
            if len(parts) >= 6 and parts[5].strip():
                self.note_private("serial", parts[5].strip())
        elif msg.code == 31:
            parts = msg.raw.split(";", 2)
            if len(parts) == 3 and parts[2].strip() and parts[2].strip() not in ("START", "END"):
                self.note_private("ssid", parts[2])

    def state(self, old: Phase | None, new: Phase, cause: str) -> None:
        """Record a phase transition with its cause; fsync'd before returning."""
        if old is not None and not isinstance(old, Phase):
            raise TypeError("old must be a Phase or None, got %s" % type(old).__name__)
        if not isinstance(new, Phase):
            raise TypeError("new must be a Phase, got %s" % type(new).__name__)
        _text("cause", cause)
        with self._lock:
            self._check_open()
            self._append(_STATES, _line({"wall": self._now(), "t": self._monotonic(),
                                         "old": None if old is None else old.name, "new": new.name,
                                         "cause": cause}))
            self._sync_locked()

    def operator(self, kind: str, shown: str, answer: str) -> None:
        """Record an operator confirmation verbatim, with the text shown; fsync'd before returning."""
        _text("kind", kind)
        _text("shown", shown)
        _text("answer", answer, allow_empty=True)
        with self._lock:
            self._check_open()
            self._append(_OPERATOR, _line({"wall": self._now(), "t": self._monotonic(), "kind": kind,
                                           "shown": shown, "answer": answer}))
            self._sync_locked()

    def position(self, *, t: float, x: float, y: float, z: float, angle: float, line: int, cut: int | None = None,
                 deviation: float | None = None, wheel_slack: float | None = None) -> None:
        """Append one row to positions.csv (empty cells for unknown values)."""
        values: list[object] = [_finite(n, v) for n, v in (("t", t), ("x", x), ("y", y), ("z", z),
                                                           ("angle", angle))]
        if type(line) is not int:
            raise TypeError("line must be an int, got %s" % type(line).__name__)
        values.append(line)
        if cut is not None and type(cut) is not int:
            raise TypeError("cut must be an int or None, got %s" % type(cut).__name__)
        values.append("" if cut is None else cut)
        values.extend("" if v is None else _finite(n, v) for n, v in (("deviation", deviation),
                                                                       ("wheel_slack", wheel_slack)))
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        with self._lock:
            self._check_open()
            if not (self.dir / _POSITIONS).exists():
                writer.writerow(_POSITION_COLUMNS)
            writer.writerow(values)
            self._append(_POSITIONS, buf.getvalue().encode("utf-8"))
            self._maybe_sync_locked()

    def note_private(self, kind: str, value: str) -> None:
        """Remember a robot serial (``kind='serial'``) or an SSID (``'ssid'``) so the export redacts it."""
        key = _PRIVATE_KINDS.get(kind) if type(kind) is str else None
        if key is None:
            raise ValueError("kind must be 'serial' or 'ssid', got %r" % (kind,))
        _text("value", value)
        with self._lock:
            self._check_open()
            if value in self._private[key]:
                return
            self._private[key].append(value)
            atomic_write_bytes(self.dir / PRIVATE_NAME, canonical_json(self._private))

    def write_file(self, relpath: str, data: bytes) -> Path:
        """Add a file to the record (upload.nc, certificate.json, sources/...); an existing one is never replaced."""
        _text("relpath", relpath)
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes, got %s" % type(data).__name__)
        if "\\" in relpath or ":" in relpath:
            raise ValueError("relpath %r: use '/' between folders, no drive or backslash" % relpath)
        pure = PurePosixPath(relpath)
        if pure.is_absolute() or any(part in ("", ".", "..") for part in relpath.split("/")):
            raise ValueError("relpath %r must stay inside the record" % relpath)
        if pure.parts[0] in _RESERVED:
            raise ValueError("relpath %r is a file the record itself writes" % relpath)
        target = self.dir.joinpath(*pure.parts)
        with self._lock:
            self._check_open()
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "xb") as f:
                f.write(bytes(data))
                f.flush()
                os.fsync(f.fileno())
        return target

    # -- the end -------------------------------------------------------------

    def finish(self, outcome: str, reasons: Sequence[str], *, disclosures: Iterable[Disclosure] = ()) -> Path:
        """Write summary.json (outcome class, reasons, disclosures, wire statistics) and close the record."""
        if type(outcome) is not str or outcome not in OUTCOMES:
            raise ValueError("outcome %r is not one of %s" % (outcome, ", ".join(OUTCOMES)))
        if isinstance(reasons, str) or not isinstance(reasons, Sequence):
            raise TypeError("reasons must be a sequence of str")
        reasons = [_text("reasons[]", r) for r in reasons]
        extra = list(disclosures)
        for d in extra:
            if not isinstance(d, Disclosure):
                raise TypeError("disclosures must hold Disclosures, got %s" % type(d).__name__)
        with self._lock:
            self._check_open()
            shown = [self.policy.disclosure()] + self._disclosures + extra
            summary = {
                "schema_version": 1,
                "job_id": self.job_id,
                "outcome": outcome,
                "reasons": reasons,
                "created_at": self.created_at,
                "finished_at": self._now(),
                "policy": self.policy.to_json(),
                "wire": {
                    "bytes_recorded": sum(self._recorded),
                    "bytes_dropped": sum(self._dropped),
                    "per_connection": {"S%d" % (c + 1): {"recorded": self._recorded[c], "dropped": self._dropped[c]}
                                       for c in _CONNECTIONS},
                    "cap_reached": self._cap_reached,
                },
                "disclosures": [d.to_json() for d in shown],
            }
            self._sync_locked()
            for f in self._files.values():
                f.close()
            self._files.clear()
            path = self.dir / SUMMARY_NAME
            atomic_write_bytes(path, canonical_json(summary))
            self._closed = True
        log.info("session record %s finished: %s", self.dir, outcome)
        return path


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def mark_exported(job_dir: Path, file_name: str, sha256: str) -> None:
    """Note in ``exports.jsonl`` that the session was exported (only the archive's name and hash, no path)."""
    _text("file_name", file_name)
    if "/" in file_name or "\\" in file_name:
        raise ValueError("file_name must be a bare file name, got %r" % file_name)
    if type(sha256) is not str or not _SHA256.match(sha256):
        raise ValueError("sha256 must be a lowercase hex SHA-256")
    line = _line({"at": _wall(datetime.now(timezone.utc)), "file": file_name, "sha256": sha256})
    with open(Path(job_dir) / EXPORTS_NAME, "ab") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


@dataclass(frozen=True)
class PruneReport:
    """What :func:`prune_sessions` did: ``removed``, ``kept`` (the newest), ``skipped`` and ``failed`` with reasons."""

    removed: tuple[Path, ...]
    kept: tuple[Path, ...]
    skipped: tuple[tuple[Path, str], ...]
    failed: tuple[tuple[Path, str], ...]


def prune_sessions(root: Path, policy: RecorderPolicy, *, confirmation: str | None) -> PruneReport:
    """Remove session records beyond the newest ``policy.keep_sessions``, only when it is safe.

    Without ``confirmation`` (the operator's typed confirmation, recorded in the log) an older session is
    removed only if it finished and was exported; unfinished ones (the black box of an interrupted session)
    and unexported ones are kept and reported. With it, every older session goes. The newest are always kept.
    A removal that fails is reported in ``failed`` and logged; it does not stop the others.
    """
    if not isinstance(policy, RecorderPolicy):
        raise TypeError("policy must be a RecorderPolicy")
    if confirmation is not None:
        _text("confirmation", confirmation)
    root = Path(root)
    skipped: list[tuple[Path, str]] = []
    sessions: list[Path] = []
    for entry in sorted(root.iterdir()) if root.is_dir() else []:
        if not entry.is_dir():
            continue
        if (entry / RECORD_NAME).is_file():
            sessions.append(entry)
        else:
            skipped.append((entry, "not a session record (no %s)" % RECORD_NAME))
    keep = sessions[-policy.keep_sessions:]
    older = sessions[: len(sessions) - len(keep)]
    removed: list[Path] = []
    failed: list[tuple[Path, str]] = []
    for session in older:
        if confirmation is None:
            if not (session / SUMMARY_NAME).is_file():
                skipped.append((session, "not finished (no %s): the record of an interrupted session is kept until "
                                         "the operator confirms" % SUMMARY_NAME))
                continue
            if not (session / EXPORTS_NAME).is_file():
                skipped.append((session, "not exported: kept until it is exported or the operator confirms"))
                continue
        try:
            shutil.rmtree(session)
        except OSError as exc:
            failed.append((session, str(exc)))
            log.warning("could not remove session record %s: %s", session, exc)
            continue
        removed.append(session)
        log.warning("removed session record %s (%s)", session,
                    "exported" if confirmation is None else "operator confirmed: %r" % confirmation)
    return PruneReport(removed=tuple(removed), kept=tuple(keep), skipped=tuple(skipped), failed=tuple(failed))


# ---------------------------------------------------------------------------
# Free disk
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiskCheck:
    """The free-disk check before arming: ``ok`` when ``free_bytes >= needed_bytes``; ``message`` says it all."""

    ok: bool
    path: Path
    path_checked: Path
    free_bytes: int
    needed_bytes: int
    est_duration_s: float
    message: str
    basis: str


def free_disk_ok(path: Path, est_duration_s: float) -> DiskCheck:
    """Whether the disk holding ``path`` has room to record a job of ``est_duration_s`` seconds.

    Needed: 200 MiB per hour of the estimate plus a 1 GiB reserve (design 7.9). A folder that does not exist yet
    is measured at its nearest existing parent (``path_checked``).
    """
    if type(est_duration_s) not in (int, float):
        raise TypeError("est_duration_s must be a number of seconds, got %s" % type(est_duration_s).__name__)
    if not math.isfinite(est_duration_s) or est_duration_s < 0:
        raise ValueError("est_duration_s must be finite and >= 0, got %r" % (est_duration_s,))
    path = Path(path)
    probe = path
    while not probe.exists():
        if probe.parent == probe:
            raise FileNotFoundError("no existing folder above %s to measure the free disk of" % path)
        probe = probe.parent
    free = int(shutil.disk_usage(probe).free)
    needed = int(RECORDER_RATE_BYTES_PER_HOUR * est_duration_s / 3600) + RECORDER_RESERVE_BYTES
    ok = free >= needed
    message = ("free disk at %s: %d MiB; needed %d MiB (200 MiB per hour of the estimated %.0f min job plus a "
               "1024 MiB reserve)" % (probe, free // MiB, -(-needed // MiB), est_duration_s / 60))
    if not ok:
        message += ": free some space or choose another data folder before arming"
        log.warning(message)
    return DiskCheck(ok=ok, path=path, path_checked=probe, free_bytes=free, needed_bytes=needed,
                     est_duration_s=float(est_duration_s), message=message, basis=_DISK_BASIS)
