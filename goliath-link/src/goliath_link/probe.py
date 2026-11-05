# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""Read-only first-contact probe for a real Goliath robot.

    python -m goliath_link.probe                 # robot's own Wi-Fi AP (10.0.0.1:80)
    python -m goliath_link.probe --host 192.168.1.50
    python -m goliath_link.probe --slot-release-sweep   # rung R1b (see the end)

**Before running it, make sure nothing else is connected to the robot**: close
Slingshot on every computer and any phone app, and power-cycle the robot if
anything was connected since it was switched on. The robot routes its replies
by connection slot (below). Another client holding a slot gets those replies
instead, and the probe can only report that it saw nothing.

Three connections
=================

GFW 01.02.00 (the only firmware image that survives; disassembled) gives each
accepted TCP connection the first free of six slots, and sends most of its
output **by slot**, not to whoever asked. So every probe session opens three
connections the way the vendor app does (S1, wait 0.25 s, S2, wait 0.25 s, S3,
settle 0.5 s; see :mod:`goliath_link.transport`) and reads all three:

====  =====================  ==================================================
tag   expected slot          what GFW 01.02.00 sends there
====  =====================  ==================================================
S1    0 (1st connection)     free-form replies (``cfg read index : n``,
                             ``spindle distance: x``), tower frames, and the
                             *direct* replies to the requester (the 48-byte
                             ``GLOBAL STATUS`` blob, ``CAGE STATUS: ...``).
                             Commands are sent on S1 only.
S2    1 (2nd connection)     every ``INFO;``/``ERR;``/``WARN;`` line:
                             ``INFO;34`` (identity), ``INFO;40``, ``INFO;0``
S3    2 (3rd connection)     position frames
====  =====================  ==================================================

The slot mapping is an assumption. It holds only if nothing else is connected
and the robot has released the slots of the previous session. The probe checks
it and reports any violation (see "Reading the results").

What it does, in order
======================

1. **Terminator test.** For each variant (no terminator, ``\\n``, ``\\r\\n``)
   a fresh three-connection session: listen passively for ``--listen``
   seconds (GFW 01.02.00 may push an ``INFO;34`` to S2 on connect), send ``INFO``
   plus the variant on S1, and wait up to ``--reply-timeout`` for an identity
   reply (``INFO;34`` or ``INFO;28``) that arrives *after the send*. If none
   arrives, the session stays open ``min(--listen, --reply-timeout)`` seconds
   more, so that a slow reply is reported as late instead of being lost. Between
   sessions the probe waits ``--slot-release-wait`` seconds so the robot can
   free the slots.
2. **Sweep.** With the first variant that got an identity reply, one more
   session: passive listen, then each allowlisted command, at least ``--pace``
   seconds apart, each waiting up to ``--reply-timeout`` for its expected
   reply; then a trailing listen.
3. Writes a **raw capture** (every byte sent and received, per connection,
   timestamped, in hex and ASCII) and a **JSON report** (every decoded event,
   tagged with its session, connection and phase, including what arrived during
   connect, in the passive windows and while closing), prints a summary and
   returns an exit code.

Safety: what it sends, and on what basis
========================================

* By default only these six commands (:data:`READ_ONLY_COMMANDS`): ``INFO``,
  ``GET GLOBAL SYS STATUS``, ``PRINT SYS REPORT``, ``GET SPINDLE CAGE STATUS``,
  ``READ INDEX CALIBRATION``, ``READ SPINDLE CALIB``. Their handlers in GFW
  01.02.00 were disassembled: none calls a motor, spindle, flash-erase or
  state-setting routine. (``READ INDEX CALIBRATION`` reads through the
  firmware's normal configuration getter, which rewrites its flash copy only
  if its two stored copies disagree; the vendor app triggers the same read
  routinely.) On firmware other than 01.02.00 they are expected, not verified,
  to behave the same.
* In the terminator test ``INFO`` is also sent as ``INFO\\n`` and ``INFO\\r\\n``.
  GFW 01.02.00 compares the received buffer exactly, so these match no command
  and are ignored.
* ``--extended`` adds ``GET Z BACKLASH`` and ``SEND TCP POS``
  (:data:`EXTENDED_COMMANDS`), and only once the robot has identified itself as
  firmware **newer than 01.02.00** (01.02.00 does not have them). No newer
  firmware image survives, so these two are **not verified**: the only basis is
  that the vendor app used them as reads.
* ``GET Z PLATE OFFSET`` is never sent: the vendor app sent it only inside an
  in-job tool-change dialog, and what it does is unknown.
* Every send goes through :func:`_checked_send`, which refuses anything else at
  run time (not with an ``assert``). Nothing else in this module writes to a
  socket. On GFW 01.02.00, connecting and disconnecting do not move the robot
  or change its work state (the accept and close paths only queue INFO
  notifications and free slots).

Even so: remove the bit and put the robot on blocks. This is the first time
anything other than the vendor app speaks to the machine.

Reading the results
===================

* **Attribution.** Each sweep command has a window, from its send to the next
  send. A message in that window is credited to the command only if it has the
  shape of one of the command's expected replies (:data:`EXPECTED_REPLIES`); it
  is flagged if it arrived on an unexpected connection. A message matching a
  reply that an *earlier* command is still missing is listed under ``late``
  with ``possibly_for``. A message that could belong to two commands, or to
  none, is listed under ``unattributed``. Nothing is credited by time alone.
* **Terminator verdicts.** An identity line after the send may also be the
  robot's connect-time ``INFO;34`` push arriving late (at most one per
  session). A variant whose session got no push before the send and only one
  identity line after it is marked ``uncertain`` (a warning) when the other
  sessions show that the robot pushes; the summary shows it as UNCERTAIN, not
  ANSWERED. The first variant with a certain answer is chosen. ``ERR;1`` after a
  variant that got no answer is noted as a possible "unknown command" reply;
  any other ERR/WARN there is a warning (GFW 01.02.00 does not answer unknown
  commands, so it is probably a fault the robot reported on its own).
* **Slot-routing check.** INFO/ERR/WARN lines anywhere but S2, position frames
  anywhere but S3, tower frames, status replies or free-form text anywhere but
  S1, and a sweep in which S2 stays silent while S1 gets replies, are reported
  as slot-routing errors, with the likely causes.
* **Exit code.** 0 only when a terminator was chosen, the firmware version was
  parsed, the sweep got replies and nothing else went wrong. 3 when it finished
  with warnings (a command without a reply, late replies, ERR/WARN messages,
  an uncertain terminator verdict, ``--extended`` not honoured). 1 on errors.
  130 when interrupted with Ctrl+C.
  Every reason is listed in the summary and in the JSON. Invalid options exit
  with 2 before anything is sent.

Slot-release sweep and idle keepalive (rung R1b)
================================================

``--slot-release-sweep`` runs instead of the steps above, once rung R1 has
established how the robot answers. Power-cycle the robot first and keep
everything else disconnected, as for the first probe. It sends **only**
``INFO`` (:data:`SLOT_RELEASE_COMMAND`), one per session, through
:func:`_checked_send` like every other send, and answers two questions the
firmware analysis leaves open (docs/protocol/09: "how quickly a slot is freed
after a disconnect"; and whether the robot's WINC1500 answers TCP keepalive
probes, without which the transport drops a silent link after about 25 s).

1. **Slot-release sweep** (JSON section ``slot_release``). Sessions of
   connect -> ``INFO`` -> close, with shrinking gaps between closing one
   session and opening the next (``--gaps``, default 5, 2, 1, 0.5, 0.25,
   0 s). Each session listens passively for ``--listen`` seconds after
   connecting, sends ``INFO``, and waits up to ``--reply-timeout`` for
   ``INFO;34`` (if none came, it listens the shorter of the two once more, so
   that a slow reply is seen as late rather than lost). A gap *works* when
   that session's ``INFO;34`` still lands on S2
   (firmware slot 1, docs/protocol/09 "Senders"): the robot had freed the
   previous session's slots in time. Each gap is measured from a session whose
   ``INFO;34`` landed on S2: an opening reference session, the previous gap if
   it worked, or else a fresh reference session opened after a recovery wait
   (the largest gap, at least 2 s). If a reference session gets no ``INFO;34``
   on S2 the sweep **stops** and sends nothing more: another client may hold a
   slot, or the robot may be in G-code upload mode, where every buffer it
   receives is stored as program data. The result, ``threshold_s``, is the
   smallest gap that worked with every larger gap working too; it becomes the
   fact ``slot.release_wait_s`` only when the sweep completed and was
   consistent (no smaller gap worked below one that failed).
2. **Idle keepalive session** (JSON section ``keepalive``). After a recovery
   wait, one session opens the three connections, sends ``INFO`` once, and
   then sends nothing for ``--keepalive-idle`` seconds (default 60) while it
   records whether the link stays up. The fact ``link.keepalive_ok`` is
   ``false`` if the link went down, ``true`` only if it stayed up, the idle
   lasted at least ``max(60 s, 2 x the keepalive teardown time this
   computer's sockets were given)``, and at least one connection received
   nothing during it; otherwise it is ``null``, with the reason.

The report (``goliath-probe-slot-release-<time>.json``, ``mode``
``slot-release-sweep``) also lists every command sent and the bytes sent per
connection. Exit codes as above: a gap that did not work is a measurement,
not a failure; an idle link that went down, or a keepalive result that cannot
set the fact, is a warning (3); a sweep that stopped, or found no working gap,
is an error (1).
"""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import queue
import re
import sys
import threading
import time
import traceback
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .client import parse_firmware_version
from .framing import Discarded
from .protocol import (
    Error,
    GlobalStatusBlob,
    GlobalStatusFrame,
    Info,
    PositionFrame,
    TextMessage,
    TowerFrame,
)
from .protocol import Warning as WarnCode
from .transport import (
    DEFAULT_CONNECT_GAPS,
    DEFAULT_KEEPALIVE,
    DEFAULT_SETTLE,
    GoliathTransport,
    Keepalive,
    LinkEvent,
    ListenerFailed,
    NotConnectedError,
    TransportClosed,
    _default_opener,
)

# --------------------------------------------------------------------------- #
# What the probe may send
# --------------------------------------------------------------------------- #

#: The commands the probe sends by default. Each one's handler in GFW 01.02.00
#: was disassembled and found read-only (module docstring, "Safety").
READ_ONLY_COMMANDS: frozenset = frozenset(
    {
        "INFO",
        "GET GLOBAL SYS STATUS",
        "PRINT SYS REPORT",
        "GET SPINDLE CAGE STATUS",
        "READ INDEX CALIBRATION",
        "READ SPINDLE CALIB",
    }
)

#: Sweep order (INFO first: its reply identifies the firmware).
SWEEP_ORDER: Tuple[str, ...] = (
    "INFO",
    "GET GLOBAL SYS STATUS",
    "PRINT SYS REPORT",
    "GET SPINDLE CAGE STATUS",
    "READ INDEX CALIBRATION",
    "READ SPINDLE CALIB",
)

#: Sent only with ``--extended``, and only to firmware newer than 01.02.00.
#: NOT verified: GFW 01.02.00 does not have them and no newer image survives.
EXTENDED_COMMANDS: frozenset = frozenset({"GET Z BACKLASH", "SEND TCP POS"})
EXTENDED_ORDER: Tuple[str, ...] = ("GET Z BACKLASH", "SEND TCP POS")
#: Extended commands need firmware strictly newer than this.
EXTENDED_NEWER_THAN: Tuple[int, int, int] = (1, 2, 0)
#: Commands the vendor app has that the probe never sends, and why.
NEVER_SENT: Dict[str, str] = {
    "GET Z PLATE OFFSET": "sent by the vendor app only inside an in-job tool-change "
                          "dialog; what it does is unknown",
}

TERMINATORS: Tuple[Tuple[str, str], ...] = (("none", ""), ("lf", "\n"), ("crlf", "\r\n"))
_TERMINATOR_STRINGS = frozenset(term for _, term in TERMINATORS)

#: Shortest gap between two sweep commands. The robot parses each received
#: buffer as one command, so commands sent closer together can merge into one
#: unknown command; the vendor app left 250-500 ms.
MIN_PACE = 0.3
DEFAULT_LISTEN = 3.0
DEFAULT_REPLY_TIMEOUT = 5.0
DEFAULT_PACE = 2.0
#: Seconds between closing one session and opening the next, so the robot can
#: free the slots (how fast it does is unknown).
DEFAULT_SLOT_RELEASE_WAIT = 2.0
DEFAULT_CONNECT_TIMEOUT = 10.0
#: After a command's required replies have arrived, keep listening this long
#: for the rest (and for an unterminated reply to be resolved by the framer).
REPLY_GRACE = 0.3

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PARTIAL = 3
EXIT_INTERRUPTED = 130

#: Identity replies: INFO;34 (app info, carries "GFW xx.yy.zz") and INFO;28.
IDENTITY_CODES = (int(Info.APP_INFO), int(Info.SYS_INFO))

CONN_TAGS = ("S1", "S2", "S3")
SLOT_ROLES: Dict[str, str] = {
    "S1": "1st connection, expected firmware slot 0: commands are sent here; free-form "
          "replies, tower frames and direct replies to the requester (status blob, "
          "CAGE STATUS) arrive here",
    "S2": "2nd connection, expected firmware slot 1: every INFO/ERR/WARN line "
          "(identity INFO;34, INFO;40, INFO;0 acknowledgements)",
    "S3": "3rd connection, expected firmware slot 2: position frames",
}

ROUTING_HINT = (
    "The robot routes its replies by connection slot, so the probe's S1/S2/S3 must be "
    "the 1st/2nd/3rd connections the robot accepted. Likely causes: another client may "
    "be connected to the robot (close Slingshot on every computer and any phone app, "
    "then power-cycle the robot and run the probe again), or the robot had not yet "
    "released the previous session's connections (run again with a longer "
    "--slot-release-wait, e.g. 5)."
)

assert set(SWEEP_ORDER) == READ_ONLY_COMMANDS
assert set(EXTENDED_ORDER) == EXTENDED_COMMANDS
assert not (EXTENDED_COMMANDS & READ_ONLY_COMMANDS) and not (set(NEVER_SENT) & EXTENDED_COMMANDS)

# Slot-release sweep and idle keepalive (rung R1b; module docstring, last section).

#: The one command the slot-release sweep and the keepalive session send.
SLOT_RELEASE_COMMAND = "INFO"
#: Gaps (s) between closing one sweep session and opening the next, largest
#: first (the project design, rung R1b; unit L07).
DEFAULT_SWEEP_GAPS: Tuple[float, ...] = (5.0, 2.0, 1.0, 0.5, 0.25, 0.0)
#: Seconds the keepalive session stays idle after its INFO (the project design,
#: red-team finding FW-10: "at least 60 s").
DEFAULT_KEEPALIVE_IDLE = 60.0
#: An idle shorter than this never sets ``link.keepalive_ok`` to true; nor does
#: one shorter than twice the keepalive teardown time of the probe's sockets.
KEEPALIVE_MIN_IDLE = 60.0
#: Version of the ``slot_release`` and ``keepalive`` report sections.
SLOT_RELEASE_SECTION_FORMAT = 1
assert SLOT_RELEASE_COMMAND in READ_ONLY_COMMANDS


class UnsafeCommandError(RuntimeError):
    """Raised if anything tries to send a non-allowlisted command through the probe."""


def _extended_permitted(firmware: object) -> bool:
    if not (isinstance(firmware, tuple) and len(firmware) == 3
            and all(isinstance(x, int) for x in firmware)):
        return False
    return firmware > EXTENDED_NEWER_THAN


def _checked_send(transport, command: str, terminator: str, *,
                  extended_firmware: Optional[Tuple[int, int, int]] = None) -> None:
    """The probe's only path to the robot. Refuses anything not allowlisted.

    :data:`READ_ONLY_COMMANDS` always pass. :data:`EXTENDED_COMMANDS` pass only
    when ``extended_firmware`` is the robot's firmware version and it is newer
    than 01.02.00 (callers pass it only when ``--extended`` was given). The
    terminator must be ``""``, ``"\\n"`` or ``"\\r\\n"``.
    """
    if command in READ_ONLY_COMMANDS:
        pass
    elif command in EXTENDED_COMMANDS:
        if not _extended_permitted(extended_firmware):
            raise UnsafeCommandError(
                f"probe refuses {command!r}: extended commands need firmware newer than "
                f"01.02.00, got {extended_firmware!r}"
            )
    else:
        raise UnsafeCommandError(f"probe refuses to send non-allowlisted command: {command!r}")
    if terminator not in _TERMINATOR_STRINGS:
        raise UnsafeCommandError(f"unexpected terminator {terminator!r}")
    transport.send(command + terminator)


# --------------------------------------------------------------------------- #
# Expected replies (for attribution)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReplyPart:
    """One message a command is expected to produce."""

    name: str
    conn: Optional[int]  #: expected connection index (0 = S1), None = not established
    required: bool  #: False: absence is noted, not warned about
    describe: str
    match: Callable[[object], bool]


def _info_code(code: int) -> Callable[[object], bool]:
    return lambda ev: isinstance(ev, TextMessage) and ev.kind == "INFO" and ev.code == code


def _is_identity(ev: object) -> bool:
    return isinstance(ev, TextMessage) and ev.kind == "INFO" and ev.code in IDENTITY_CODES


def _text_prefix(prefix: str) -> Callable[[object], bool]:
    want = prefix.upper()
    return lambda ev: (isinstance(ev, TextMessage) and ev.kind == "TEXT"
                       and ev.raw.upper().startswith(want))


def _is_status(ev: object) -> bool:
    return isinstance(ev, (GlobalStatusBlob, GlobalStatusFrame))


def _is_position(ev: object) -> bool:
    return isinstance(ev, PositionFrame)


_ACK = ReplyPart("ack", 1, False, "INFO;0 acknowledgement on S2", _info_code(0))

#: What each command is expected to produce on GFW 01.02.00 (firmware facts
#: brief). Optional parts are not established for every command.
EXPECTED_REPLIES: Dict[str, Tuple[ReplyPart, ...]] = {
    "INFO": (ReplyPart("identity", 1, True, "INFO;34 identity line on S2", _is_identity),),
    "GET GLOBAL SYS STATUS": (
        ReplyPart("status", 0, True,
                  "global-status reply on S1 (the 48-byte blob on GFW 01.02.00)", _is_status),
        _ACK,
    ),
    "PRINT SYS REPORT": (ReplyPart("report", 1, True, "INFO;40 sanity report on S2",
                                   _info_code(int(Info.SANITY_CHECK))),),
    "GET SPINDLE CAGE STATUS": (
        ReplyPart("cage", 0, True, "'CAGE STATUS: ...' on S1", _text_prefix("CAGE STATUS")),
        _ACK,
    ),
    "READ INDEX CALIBRATION": (
        ReplyPart("value", 0, True, "'cfg read index : <n>' on S1",
                  _text_prefix("cfg read index")),
        _ACK,
    ),
    "READ SPINDLE CALIB": (
        ReplyPart("value", 0, True, "'spindle distance: <x>' on S1",
                  _text_prefix("spindle distance")),
        _ACK,
    ),
    # Extended (firmware newer than 01.02.00; replies are the vendor app's parse).
    "GET Z BACKLASH": (
        ReplyPart("value", None, True,
                  "'Z const offset: <x>' (the vendor app's parse; connection not established)",
                  _text_prefix("Z const offset")),
        _ACK,
    ),
    "SEND TCP POS": (ReplyPart("position", 2, True, "a position frame on S3", _is_position),),
}
assert set(EXPECTED_REPLIES) == READ_ONLY_COMMANDS | EXTENDED_COMMANDS


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _tag(conn: Optional[int]) -> Optional[str]:
    return None if conn is None else f"S{conn + 1}"


def _fw_text(v: Sequence[int]) -> str:
    return "{0:02d}.{1:02d}.{2:02d}".format(*v)


def _finite(value: object, name: str) -> float:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number of seconds, not {value!r}") from None
    if not math.isfinite(v):
        raise ValueError(f"{name} must be a finite number of seconds, not {value!r}")
    return v


def _validate_port(port: object) -> int:
    """``port`` as an int in 1..65535, else ValueError.

    Checked here because the C library does not: glibc's getaddrinfo wraps
    70000 to 4464 without an error, so an out-of-range port would connect
    somewhere other than what was typed.
    """
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"port must be a whole number from 1 to 65535, not {port!r}")
    return port


def _validate_timing(*, listen: float, reply_timeout: float, pace: float,
                     slot_release_wait: float, connect_timeout: float) -> None:
    if not _finite(pace, "pace") >= MIN_PACE:
        raise ValueError(f"pace must be at least {MIN_PACE} s (got {pace!r}): the robot "
                         "parses each received buffer as one command, so commands sent "
                         "closer together can merge into one unknown command")
    for name, value in (("listen", listen), ("reply_timeout", reply_timeout),
                        ("connect_timeout", connect_timeout)):
        if not _finite(value, name) > 0:
            raise ValueError(f"{name} must be > 0 s, not {value!r}")
    if not _finite(slot_release_wait, "slot_release_wait") >= 0:
        raise ValueError(f"slot_release_wait must be >= 0 s, not {slot_release_wait!r}")


def _err_name(ev: TextMessage) -> str:
    table = {"ERR": Error, "WARN": WarnCode, "INFO": Info}.get(ev.kind)
    if table is None or ev.code is None:
        return ""
    try:
        return table(ev.code).name
    except ValueError:
        return "unknown code"


def _firmware_from(ev: TextMessage) -> Optional[Tuple[int, int, int]]:
    """The ``xx.yy.zz`` version in an identity line (INFO;34: its first field)."""
    if ev.code == int(Info.APP_INFO):
        return parse_firmware_version(ev.params[0]) if ev.params else None
    return parse_firmware_version(" ".join(ev.params))


def summarize(event: object) -> dict:
    """A JSON-friendly description of one transport event."""
    if isinstance(event, TextMessage):
        return {"type": "text", "kind": event.kind, "code": event.code,
                "params": list(event.params), "raw": event.raw, "terminated": event.terminated}
    if isinstance(event, GlobalStatusBlob):
        return {"type": "global_status_blob", "fields": list(event.fields),
                "fields_by_vendor_name_unverified": event.fields_by_vendor_name_unverified(),
                "raw": event.raw.hex(" ")}
    if isinstance(event, GlobalStatusFrame):
        return {"type": "global_status", "t1_firmware": list(event.t1_firmware),
                "t2_firmware": list(event.t2_firmware),
                "flags_raw": event.flags_raw.hex(" "), "raw": event.raw.hex(" ")}
    if isinstance(event, PositionFrame):
        return {"type": "position", **asdict(event)}
    if isinstance(event, TowerFrame):
        return {"type": "tower", **asdict(event)}
    if isinstance(event, Discarded):
        return {"type": "discarded", "reason": event.reason, "length": len(event.raw),
                "raw": event.raw.hex(" ")}
    if isinstance(event, TransportClosed):
        return {"type": "closed", "error": repr(event.error) if event.error else None,
                "closed_conn": _tag(event.conn)}
    if isinstance(event, ListenerFailed):
        return {"type": "listener_failed", "error": repr(event.error)}
    return {"type": "unknown", "repr": repr(event)}


# --------------------------------------------------------------------------- #
# Raw capture
# --------------------------------------------------------------------------- #


class _CaptureLog:
    """Timestamped, thread-safe log of every byte sent and received (UTF-8)."""

    def __init__(self, path: str, t0: float,
                 sessions: str = "one terminator test, or the sweep") -> None:
        self.path = path
        self.t0 = t0
        self._lock = threading.Lock()
        self._fh = open(path, "w", encoding="utf-8", newline="\n")
        self._fh.write(
            "# goliath_link.probe raw capture. '>' = sent to the robot, '<' = received.\n"
            f"# Each session ({sessions}) opens three connections:\n"
            "#   S1 = 1st (commands; free-form and direct replies), S2 = 2nd (INFO/ERR/WARN),\n"
            "#   S3 = 3rd (position frames) -- firmware slots 0/1/2 if nothing else is connected.\n"
            "# columns: seconds_since_start  direction  session  connection  hex  |  ascii\n"
        )
        self._fh.flush()

    def note(self, text: str) -> None:
        with self._lock:
            self._fh.write(f"# {time.monotonic() - self.t0:9.3f}  {text}\n")
            self._fh.flush()

    def record(self, t: float, direction: str, session: str, conn: int, data: bytes) -> None:
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
        with self._lock:
            self._fh.write(f"{t - self.t0:9.3f}  {direction}  {session:<10}  {_tag(conn)}  "
                           f"{data.hex(' ')}  |  {ascii_}\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            self._fh.close()


# --------------------------------------------------------------------------- #
# One three-connection session
# --------------------------------------------------------------------------- #


@dataclass
class _Rec:
    event: object
    conn: Optional[int]
    t: float  # time.monotonic()
    data: dict  # the JSON form; attribution adds keys


class _Session:
    def __init__(self, run: "_Run", name: str, kind: str, label: str, mode: str) -> None:
        self.run = run
        self.name = name
        self.kind = kind
        self.label = label
        self.mode = mode
        self.opened = 0
        self.connected = False
        self.connect_error: Optional[str] = None
        self.records: List[_Rec] = []
        self.closed: Optional[_Rec] = None  # the TransportClosed record
        self.connect_done_t: Optional[float] = None
        self.sends: List[Tuple[str, float]] = []
        self.window_end: Optional[float] = None
        self.close_t: Optional[float] = None
        self.close_confirmed = False
        self.on_record: Optional[Callable[[_Rec], None]] = None
        self.transport = GoliathTransport(
            run.host, run.port, timeout=run.connect_timeout, opener=self._open,
            recorder=self._capture, connections=3, connect_gaps=run.connect_gaps,
            settle=run.settle, keepalive=run.link_keepalive,
        )

    # transport hooks (called on transport threads)
    def _open(self, host: str, port: int, timeout: float):
        sock = self.run.opener(host, port, timeout)
        self.opened += 1
        return sock

    def _capture(self, conn: int, direction: str, data: bytes, t: float) -> None:
        self.run.capture.record(t, direction, self.label, conn, data)

    # lifecycle
    def connect(self) -> bool:
        self.run.capture.note(f"[{self.label}] connecting S1, S2, S3 to "
                              f"{self.run.host}:{self.run.port}")
        try:
            self.transport.connect()
        except Exception as exc:
            self.connect_done_t = time.monotonic()
            self.connect_error = repr(exc)
            self.run.capture.note(f"[{self.label}] connect failed: {exc!r}")
            self.drain()
            self.run.connect_failed(self, exc)
            return False
        self.connect_done_t = time.monotonic()
        self.connected = True
        self.run.capture.note(f"[{self.label}] connected")
        return True

    def close(self) -> None:
        if self.close_t is None:
            self.close_t = time.monotonic()
            self.run.capture.note(f"[{self.label}] closing")
        try:
            self.transport.close()
        finally:
            self.drain()
            if self.closed is None:
                self.run.warn(f"{self.name}: the link did not confirm its close (a reader "
                              "thread may still be running); anything it received after "
                              "this point is missing from the report")

    # receiving
    def phase_at(self, t: float) -> str:
        if self.close_t is not None and t >= self.close_t:
            return "at close"
        if self.connect_done_t is None or t < self.connect_done_t:
            return "connect"
        if not self.sends or t < self.sends[0][1]:
            return "passive"
        if self.window_end is not None and t >= self.window_end:
            return "trailing"
        cmd = [c for c, st in self.sends if st <= t][-1]
        return f"after {cmd}"

    def _add(self, link: LinkEvent) -> _Rec:
        data = {"t": round(link.t - self.run.capture.t0, 4), "session": self.name,
                "conn": _tag(link.conn), "phase": self.phase_at(link.t)}
        data.update(summarize(link.event))
        rec = _Rec(link.event, link.conn, link.t, data)
        self.records.append(rec)
        if isinstance(link.event, TransportClosed):
            self.closed = rec
            self.close_confirmed = True
        elif self.on_record is not None:
            self.on_record(rec)
        return rec

    def pump(self, deadline: Callable[[], float]) -> bool:
        """Receive until ``deadline()`` (re-evaluated). False if the link went down."""
        q = self.transport.records
        while self.closed is None:
            remaining = deadline() - time.monotonic()
            if remaining <= 0:
                return True
            try:
                link = q.get(timeout=remaining)
            except queue.Empty:
                return True
            self._add(link)
        return False

    def pump_for(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        return self.pump(lambda: end)

    def drain(self) -> None:
        q = self.transport.records
        while True:
            try:
                link = q.get_nowait()
            except queue.Empty:
                return
            self._add(link)

    def identity_records(self) -> List[_Rec]:
        return [r for r in self.records if _is_identity(r.event)]

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "terminator": self.mode,
            "opened": self.opened,
            "connected": self.connected,
            "connect_error": self.connect_error,
            "connections": [
                {"conn": _tag(c.index), "bytes_received": c.bytes_received,
                 "bytes_sent": c.bytes_sent, "socket_options": c.socket_options}
                for c in self.transport.connection_info()
            ],
            "events": [r.data for r in self.records],
        }


class _IdleSession(_Session):
    """A session with an idle window (the keepalive session): its events are phase 'idle'."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.idle_start: Optional[float] = None
        self.idle_end: Optional[float] = None

    def phase_at(self, t: float) -> str:
        if (self.idle_start is not None and t >= self.idle_start
                and (self.close_t is None or t < self.close_t)):
            return "idle"
        return super().phase_at(t)


class _Window:
    """One sweep command: its send time and what arrived for it."""

    def __init__(self, command: str) -> None:
        self.command = command
        self.parts = EXPECTED_REPLIES[command]
        self.sent_t: Optional[float] = None
        self.end_t: Optional[float] = None
        self.send_error: Optional[str] = None
        self.filled: Dict[str, _Rec] = {}
        self.late: Dict[str, _Rec] = {}
        self.other: List[_Rec] = []
        self.complete_t: Optional[float] = None

    def missing(self) -> List[ReplyPart]:
        return [p for p in self.parts if p.name not in self.filled and p.name not in self.late]

    def required_done(self) -> bool:
        return all(p.name in self.filled for p in self.parts if p.required)


# --------------------------------------------------------------------------- #
# The probe run
# --------------------------------------------------------------------------- #


class _Reporting:
    """What the probe run and the slot-release sweep share: report lists, the send, link-loss text.

    Subclasses set ``report``, ``capture``, ``phase`` and ``aborted``, plus the
    connection settings :class:`_Session` reads (``host``, ``port``,
    ``connect_timeout``, ``connect_gaps``, ``settle``, ``opener``), and
    implement ``connect_failed(session, exc)``.
    """

    report: dict
    capture: _CaptureLog
    phase: str
    aborted: List[str]
    #: TCP keepalive timing for every connection (the transport's default).
    link_keepalive: Keepalive = DEFAULT_KEEPALIVE

    def error(self, msg: str) -> None:
        self.report["errors"].append(msg)
        self.capture.note(f"ERROR: {msg}")

    def warn(self, msg: str) -> None:
        self.report["warnings"].append(msg)
        self.capture.note(f"WARNING: {msg}")

    def note(self, msg: str) -> None:
        self.report["notes"].append(msg)
        self.capture.note(f"note: {msg}")

    def unexpected(self, where: str, exc: BaseException) -> None:
        tb = traceback.extract_tb(exc.__traceback__)
        at = f" (at {os.path.basename(tb[-1].filename)}:{tb[-1].lineno})" if tb else ""
        self.error(f"unexpected error while {self.phase}: {exc!r}{at}")
        self.aborted.append(where)

    def describe_loss(self, sess: _Session) -> str:
        t = sess.transport
        err, conn = t.closed_error, t.closed_conn
        if err is None and sess.closed is not None:
            ev = sess.closed.event
            err, conn = ev.error, ev.conn  # type: ignore[attr-defined]
        where = _tag(conn) or "the link"
        if err is None:
            return "the link was closed locally"
        by_robot = (isinstance(err, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError))
                    or "closed by the robot" in str(err))
        if by_robot:
            return f"the robot closed connection {where} ({err})"
        return f"connection {where} failed ({err!r})"

    def send_checked(self, sess: _Session, command: str, term: str,
                     extended_firmware: Optional[Tuple[int, int, int]] = None) -> Optional[str]:
        """Send through the gate. Returns None on success, else why it failed."""
        self.phase = f"{sess.name}: sending {command}"
        if not sess.transport.connected:
            return f"{command} was not sent: {self.describe_loss(sess)}"
        sess.run.capture.note(f"[{sess.label}] sending {command + term!r}")
        sent_t = time.monotonic()
        try:
            _checked_send(sess.transport, command, term, extended_firmware=extended_firmware)
        except NotConnectedError:
            return f"{command} was not sent: {self.describe_loss(sess)}"
        except OSError as exc:
            return (f"sending {command} failed: {exc!r}. The connection broke during the "
                    "send; the robot may have received part of it")
        # The transport may hold a send back briefly until text pending on S1
        # is complete; the reply window starts when the bytes actually left.
        actual = sess.transport.last_send_at
        if actual is not None and actual >= sent_t:
            sent_t = actual
        sess.sends.append((command, sent_t))
        return None

    def result(self) -> None:
        r = self.report
        if r["interrupted"]:
            r["result"], r["exit_code"] = "interrupted", EXIT_INTERRUPTED
        elif r["errors"]:
            r["result"], r["exit_code"] = "failed", EXIT_FAILED
        elif r["warnings"]:
            r["result"], r["exit_code"] = "partial", EXIT_PARTIAL
        else:
            r["result"], r["exit_code"] = "ok", EXIT_OK


class _Run(_Reporting):
    def __init__(self, host: str, port: int, capture: _CaptureLog, report: dict, *,
                 terminator: str, listen: float, reply_timeout: float, pace: float,
                 slot_release_wait: float, connect_timeout: float, extended: bool,
                 connect_gaps: Sequence[float], settle: float, opener) -> None:
        self.host = host
        self.port = port
        self.capture = capture
        self.report = report
        self.terminator = terminator
        self.listen = listen
        self.reply_timeout = reply_timeout
        self.pace = pace
        self.slot_release_wait = slot_release_wait
        self.connect_timeout = connect_timeout
        self.extended = extended
        self.connect_gaps = tuple(connect_gaps)
        self.settle = settle
        self.opener = opener
        self.sessions: List[_Session] = []
        self.windows: List[_Window] = []
        self.sweep_session: Optional[_Session] = None
        self.phase = "starting"
        self.unreachable = False
        self.aborted: List[str] = []
        self.finished = False
        if not extended:
            report["extended"]["reason"] = "not requested (--extended not given)"

    # -- reporting (error, warn, note, unexpected, describe_loss: _Reporting) ---

    def connect_failed(self, sess: _Session, exc: BaseException) -> None:
        target = f"{self.host}:{self.port}"
        t = sess.transport
        cause = exc.__cause__ if exc.__cause__ is not None else exc
        earlier = [s.name for s in self.sessions if s is not sess and s.connected]
        ever_opened = any(s.opened for s in self.sessions if s is not sess)
        self.aborted.append(sess.name)
        if sess.opened == 0 and not ever_opened:
            self.unreachable = True
            self.error(
                f"{sess.name}: could not connect to {target}: {cause!r}. Is this computer "
                "joined to the robot's Goliath_xxxxxx Wi-Fi (or on the same network as it), "
                "and is the robot switched on?"
            )
        elif t.closed_conn is not None and t.closed_conn >= sess.opened:
            n = sess.opened
            have = f"opened {', '.join(CONN_TAGS[:n])} but " if n else ""
            since = (f", although {', '.join(earlier)} connected earlier" if earlier
                     else ", although earlier connections to it succeeded")
            self.error(
                f"{sess.name}: {have}could not open {CONN_TAGS[t.closed_conn]} "
                f"(its {('1st', '2nd', '3rd')[t.closed_conn]} connection) to {target}: "
                f"{cause!r}{since}. The robot may have stopped accepting connections (it "
                "rebooted, or the wireless link dropped) or not yet released the earlier "
                "ones; run again with a longer --slot-release-wait."
            )
        else:
            lost = _tag(t.closed_conn) or "a connection"
            self.error(
                f"{sess.name}: the robot accepted {lost} and then closed it during the "
                f"three-connection handshake ({t.closed_error}). The robot closes "
                "connections it has no free slot for (it has 6): another client may be "
                "connected to the robot (close Slingshot and any phone app, then "
                "power-cycle the robot), or the previous session's connections were not "
                "yet released (run again with a longer --slot-release-wait)."
            )

    # -- the procedure ----------------------------------------------------------

    def new_session(self, name: str, kind: str, label: str, mode: str) -> _Session:
        sess = _Session(self, name, kind, label, mode)
        self.sessions.append(sess)
        return sess

    def wait_for_slot_release(self) -> None:
        if self.slot_release_wait > 0:
            self.phase = f"waiting {self.slot_release_wait:g} s for the robot to release slots"
            self.capture.note(self.phase)
            time.sleep(self.slot_release_wait)

    def run(self) -> None:
        modes = TERMINATORS if self.terminator == "auto" else tuple(
            m for m in TERMINATORS if m[0] == self.terminator)
        for i, (mode, term) in enumerate(modes):
            if i > 0:
                self.wait_for_slot_release()
            self.terminator_test(mode, term)
            if self.unreachable:
                break
        self.choose_terminator()
        chosen = self.report["chosen_terminator"]
        if chosen is not None:
            self.wait_for_slot_release()
            self.sweep(chosen, dict(TERMINATORS)[chosen])
        self.finished = True

    # (send_checked: _Reporting)

    # -- 1. terminator test -------------------------------------------------------

    def terminator_test(self, mode: str, term: str) -> None:
        sess = self.new_session(f"terminator test '{mode}'", "terminator-test", f"test-{mode}",
                                mode)
        entry: dict = {"mode": mode, "session": sess.name, "sent": "INFO" + term,
                       "responded": False, "identity_conn": None, "replies": [],
                       "error": None}
        self.report["terminator_tests"].append(entry)
        try:
            self.phase = f"{sess.name}: connecting"
            if not sess.connect():
                entry["error"] = sess.connect_error
                return
            self.phase = f"{sess.name}: passive listen"
            if not sess.pump_for(self.listen):
                entry["error"] = f"link lost before INFO was sent: {self.describe_loss(sess)}"
                self.error(f"{sess.name}: {entry['error']}")
                self.aborted.append(sess.name)
                return
            why = self.send_checked(sess, "INFO", term)
            if why is not None:
                entry["error"] = why
                self.error(f"{sess.name}: {why}")
                self.aborted.append(sess.name)
                return
            sent_t = sess.sends[-1][1]
            self.phase = f"{sess.name}: waiting for the reply to INFO"

            def deadline() -> float:
                ident = [r for r in sess.identity_records() if r.t >= sent_t]
                if ident:
                    return min(sent_t + self.reply_timeout, ident[0].t + REPLY_GRACE + 0.2)
                return sent_t + self.reply_timeout

            alive = sess.pump(deadline)
            sess.window_end = time.monotonic()
            if not alive:
                entry["error"] = (f"link lost while waiting for the reply to INFO: "
                                  f"{self.describe_loss(sess)}")
                self.error(f"{sess.name}: {entry['error']}")
                self.aborted.append(sess.name)
            elif not any(r.t >= sent_t for r in sess.identity_records()):
                # Keep the connections open a little longer: a reply that comes
                # just after the window must still reach the probe (the robot
                # drops what it sends to a closed slot), so it can be reported
                # as late instead of being lost (finish_terminator_entry).
                trailing = self.trailing_listen()
                self.phase = f"{sess.name}: trailing listen after the reply window"
                sess.run.capture.note(f"[{sess.label}] no identity reply within "
                                      f"{self.reply_timeout:g} s; listening {trailing:g} s more")
                if not sess.pump_for(trailing):
                    self.error(f"{sess.name}: link lost during the trailing listen after the "
                               f"reply window: {self.describe_loss(sess)}")
                    self.aborted.append(sess.name)
        except Exception as exc:  # recorded, then the next terminator variant runs
            entry["error"] = repr(exc)
            self.unexpected(sess.name, exc)
        finally:
            try:
                sess.close()
            finally:
                self.finish_terminator_entry(sess, entry)

    def trailing_listen(self) -> float:
        """Seconds a terminator session that got no identity stays open after its window."""
        return min(self.listen, self.reply_timeout)

    def finish_terminator_entry(self, sess: _Session, entry: dict) -> None:
        window = [r for r in sess.records if r.data["phase"] == "after INFO"]
        entry["replies"] = [r.data for r in window]
        ident = [r for r in window if _is_identity(r.event)]
        entry["responded"] = bool(ident)
        entry["identity_conn"] = _tag(ident[0].conn) if ident else None
        # Evidence for judging the verdict once every variant has run
        # (judge_terminators): identity lines before and after the send.
        sent_t = sess.sends[0][1] if sess.sends else None
        ids = sess.identity_records()
        entry["identity_before_send"] = sum(1 for r in ids if sent_t is None or r.t < sent_t)
        entry["identity_after_send"] = (None if sent_t is None
                                        else sum(1 for r in ids if r.t >= sent_t))
        entry["uncertain"] = None
        late = [r for r in sess.records
                if r.data["phase"] in ("trailing", "at close") and _is_identity(r.event)]
        if late and not ident:
            if entry["identity_before_send"]:
                why = ("the 'no answer' for this variant may be a timing artifact: the robot "
                       "may answer more slowly than --reply-timeout (run again with "
                       "--reply-timeout 10)")
            else:
                why = ("either the robot answered more slowly than --reply-timeout, so the "
                       "'no answer' for this variant may be a timing artifact (run again with "
                       "--reply-timeout 10), or it is the robot's connect-time INFO;34 push "
                       "arriving late (run again with --listen 10)")
            self.warn(
                f"{sess.name}: an identity line arrived after the {self.reply_timeout:g} s "
                f"reply window ({late[0].data['raw']!r} on {late[0].data['conn']}, "
                f"{late[0].data['phase']}): {why}"
            )

    @staticmethod
    def _uncertainty(entry: dict, others: List[dict]) -> Optional[str]:
        """Why an identity line after ``entry``'s send may not be an answer, or None.

        The robot may push one INFO;34 on connect (at most one per session: it
        is tied to one accepted connection). If that push arrives after the
        send, it looks exactly like an answer. ``others`` are the other
        variants that were sent; their identity counts tell how this robot
        pushes.
        """
        if entry["identity_before_send"]:
            return None  # this session's push came before the send
        if (entry["identity_after_send"] or 0) >= 2:
            return None  # the push and an answer: at least one is an answer
        early = [o for o in others if o["identity_before_send"]]
        if early:
            return (f"no connect-time INFO;34 arrived before the send (unlike "
                    f"{early[0]['session']}), so the identity line after the send may be that "
                    f"push arriving late rather than an answer to {entry['sent']!r}")
        late = [o for o in others if (o["identity_after_send"] or 0) >= 2]
        if late:
            o = late[0]
            return (f"{o['session']} received {o['identity_after_send']} INFO;34 identity lines "
                    "after its send and none before, so this robot's connect-time INFO;34 push "
                    "arrives after the --listen window; the one identity line after "
                    f"{entry['sent']!r} may be that push rather than an answer")
        silent = [o for o in others if not o["identity_before_send"]
                  and not o["identity_after_send"]]
        if silent:
            return None  # the robot pushed nothing at all in that session
        return ("no session shows whether the robot pushes an INFO;34 on connect (none arrived "
                "before any send), so the one identity line after "
                f"{entry['sent']!r} may be a late connect-time push rather than an answer")

    def judge_terminators(self) -> None:
        """Mark each answered variant whose answer may be a late connect-time push."""
        tested = [e for e in self.report["terminator_tests"]
                  if e.get("identity_after_send") is not None]
        for e in tested:
            if e["responded"]:
                e["uncertain"] = self._uncertainty(e, [o for o in tested if o is not e])

    def choose_terminator(self) -> None:
        tests = self.report["terminator_tests"]
        self.judge_terminators()
        answered = [e for e in tests if e["responded"]]
        certain = [e for e in answered if not e.get("uncertain")]
        if certain or answered:
            entry = (certain or answered)[0]
            self.report["chosen_terminator"] = entry["mode"]
            if not certain:
                self.note(f"terminator {entry['mode']!r} chosen although its answer is "
                          "uncertain (see the warnings); the sweep checks it")
            return
        if any(s.connected for s in self.sessions):
            per_conn = Counter()
            for s in self.sessions:
                for c in s.transport.connection_info():
                    per_conn[_tag(c.index)] += c.bytes_received
            nothing_on_s2 = per_conn["S2"] == 0
            tested = ", ".join(e["mode"] for e in tests)
            self.error(
                "no terminator variant got an INFO;34 identity reply on any connection, so "
                "the sweep was skipped. GFW 01.02.00 sends that reply only to the robot's "
                "2nd connection slot (S2 here). If another client (Slingshot on any "
                "computer, a phone app) is connected to the robot, it holds that slot and "
                "receives the reply instead: close every other app, power-cycle the robot, "
                "and run the probe again. If nothing else was connected, the robot may have "
                f"answered more slowly than --reply-timeout ({self.reply_timeout:g} s; run "
                "again with --reply-timeout 10), or it may frame commands differently from "
                f"the variant(s) tested ({tested}): the capture file shows every byte that "
                "came back. Bytes received over the terminator tests: "
                + ", ".join(f"{tag} {per_conn[tag]}" for tag in CONN_TAGS)
                + (" (nothing at all on S2)." if nothing_on_s2 else ".")
            )
        else:
            self.error("the sweep was skipped: no terminator test could connect to the robot")

    # -- 2. sweep -----------------------------------------------------------------

    def sweep(self, mode: str, term: str) -> None:
        sess = self.new_session("sweep", "sweep", "sweep", mode)
        self.sweep_session = sess
        sess.on_record = lambda rec: self.attribute(sess, rec)
        planned = list(SWEEP_ORDER)
        done: List[str] = []
        try:
            self.phase = "sweep: connecting"
            if not sess.connect():
                return
            self.phase = "sweep: passive listen"
            if not sess.pump_for(self.listen):
                self.error(f"sweep: link lost before the first command: {self.describe_loss(sess)}")
                self.abort_sweep(planned, done)
                return
            i = 0
            ext_fw: Optional[Tuple[int, int, int]] = None
            while i < len(planned):
                cmd = planned[i]
                win = _Window(cmd)
                self.windows.append(win)
                why = self.send_checked(
                    sess, cmd, term,
                    extended_firmware=ext_fw if cmd in EXTENDED_COMMANDS else None)
                if why is not None:
                    win.send_error = why
                    self.error(f"sweep: {why}")
                    self.abort_sweep(planned, done + [cmd])
                    return
                done.append(cmd)
                win.sent_t = sess.sends[-1][1]
                self.phase = f"sweep: waiting for the reply to {cmd}"

                def deadline(w: _Window = win) -> float:
                    assert w.sent_t is not None
                    if w.complete_t is not None:
                        return max(w.sent_t + self.pace, w.complete_t + REPLY_GRACE)
                    return w.sent_t + max(self.pace, self.reply_timeout)

                alive = sess.pump(deadline)
                win.end_t = time.monotonic()
                if not alive:
                    self.error(f"sweep: {self.describe_loss(sess)} while waiting for the "
                               f"reply to {cmd}")
                    self.abort_sweep(planned, done)
                    return
                i += 1
                if i == len(SWEEP_ORDER) and cmd == SWEEP_ORDER[-1]:
                    ext_fw = self.extended_decision()
                    if ext_fw is not None:
                        planned.extend(EXTENDED_ORDER)
            self.phase = "sweep: trailing listen"
            sess.window_end = time.monotonic()
            alive = sess.pump_for(self.listen)
            if not alive:
                self.error(f"sweep: {self.describe_loss(sess)} during the trailing listen "
                           "after the last command")
                self.aborted.append("sweep")
        except Exception as exc:
            self.unexpected("sweep", exc)
        finally:
            try:
                if sess.window_end is None:
                    sess.window_end = time.monotonic()
                sess.close()
            finally:
                self.finish_sweep(sess)

    def abort_sweep(self, planned: List[str], attempted: List[str]) -> None:
        self.aborted.append("sweep")
        rest = [c for c in planned if c not in attempted]
        if rest:
            self.error("sweep aborted; not sent: " + ", ".join(rest))
        if self.report["extended"]["reason"] is None:
            self.report["extended"]["reason"] = "not sent: the sweep was aborted before them"

    def attribute(self, sess: _Session, rec: _Rec) -> None:
        """Credit ``rec`` to a sweep command, or list it as late/unattributed."""
        sent = [w for w in self.windows if w.sent_t is not None]
        if not sent or rec.t < sent[0].sent_t:  # type: ignore[operator]
            return  # connect/passive: reported as such
        idx = max(i for i, w in enumerate(sent) if w.sent_t <= rec.t)  # type: ignore[operator]
        cur = sent[idx]
        in_window = cur.end_t is None or rec.t < cur.end_t
        ev = rec.event
        here = [p for p in cur.missing() if p.match(ev)] if in_window else []
        before = sent[:idx] if in_window else sent[: idx + 1]
        earlier = [(w, p) for w in before for p in w.missing() if p.match(ev)]
        if here and not earlier:
            part = here[0]
            cur.filled[part.name] = rec
            rec.data.update(reply_to=cur.command, part=part.name,
                            expected_conn=_tag(part.conn),
                            conn_ok=part.conn is None or part.conn == rec.conn)
            if cur.complete_t is None and cur.required_done():
                cur.complete_t = rec.t
            return
        if here and earlier:
            names = sorted({w.command for w, _ in earlier})
            self.unattributed(rec, f"arrived after {cur.command} and matches its expected "
                                   f"{here[0].describe}, but {', '.join(names)} (sent "
                                   "earlier) is still missing the same kind of message: "
                                   "it cannot be told which one it belongs to")
            if in_window:
                cur.other.append(rec)
            return
        if earlier:
            w, p = earlier[0]  # the robot answers in order: the oldest one still waiting
            w.late[p.name] = rec
            rec.data.update(late=True, possibly_for=w.command, part=p.name,
                            candidates=sorted({x.command for x, _ in earlier}))
            self.report["late"].append(rec.data)
            return
        if in_window:
            dup = [p for p in cur.parts if p.name in cur.filled and p.match(ev)]
            why = (f"a second {dup[0].describe} after {cur.command}" if dup else
                   f"arrived after {cur.command} but matches none of its expected replies")
            cur.other.append(rec)
        else:
            why = "arrived after the last command's reply window and matches no missing reply"
        if not isinstance(ev, TransportClosed):
            self.unattributed(rec, why)

    def unattributed(self, rec: _Rec, why: str) -> None:
        rec.data["unattributed"] = why
        self.report["unattributed"].append(rec.data)

    def firmware_now(self) -> Tuple[Optional[Tuple[int, int, int]], Optional[str], Optional[str],
                                    List[str]]:
        """(version, source, raw identity line, conflicting versions) known so far."""
        candidates: List[Tuple[int, str, _Rec]] = []
        for w in self.windows:
            if w.command == "INFO" and "identity" in w.filled:
                r = w.filled["identity"]
                candidates.append((0, f"reply to INFO in the sweep, on {r.data['conn']}", r))
        chosen = self.report["chosen_terminator"]
        for s in self.sessions:
            for r in s.identity_records():
                ph = r.data["phase"]
                if s.kind == "terminator-test" and ph == "after INFO":
                    rank = 1 if s.mode == chosen else 2
                    candidates.append((rank, f"reply to INFO in {s.name}, on {r.data['conn']}", r))
                elif ph in ("connect", "passive"):
                    candidates.append((3, f"pushed at connect in {s.name}, on {r.data['conn']}", r))
        candidates.sort(key=lambda c: c[0])
        parsed = [(src, r, _firmware_from(r.event)) for _, src, r in candidates]  # type: ignore[arg-type]
        versions = sorted({_fw_text(v) for _, _, v in parsed if v is not None})
        for src, r, v in parsed:
            if v is not None:
                return v, src, r.data["raw"], versions
        if parsed:
            src, r, _ = parsed[0]
            return None, src, r.data["raw"], versions
        return None, None, None, versions

    def extended_decision(self) -> Optional[Tuple[int, int, int]]:
        ext = self.report["extended"]
        if not self.extended:
            return None
        fw, _src, _raw, versions = self.firmware_now()
        if len(versions) > 1:
            reason = ("not sent: the robot reported different firmware versions ("
                      + ", ".join(versions) + ")")
        elif fw is None:
            reason = ("not sent: the firmware version is unknown, and GFW 01.02.00 does not "
                      "have these commands")
        elif not _extended_permitted(fw):
            reason = (f"not sent: the robot runs firmware {_fw_text(fw)}; GFW 01.02.00 does "
                      "not have GET Z BACKLASH or SEND TCP POS, so they are only sent to "
                      "newer firmware")
        else:
            ext["sent"] = True
            ext["reason"] = (f"sent: firmware {_fw_text(fw)} is newer than 01.02.00 (these "
                             "commands are NOT verified read-only: no image of newer firmware "
                             "survives)")
            return fw
        ext["reason"] = reason
        self.warn(f"--extended was given but GET Z BACKLASH and SEND TCP POS were {reason}")
        return None

    def finish_sweep(self, sess: _Session) -> None:
        out = self.report["sweep"]
        for w in self.windows:
            entry: dict = {"command": w.command, "terminator": sess.mode, "sent": w.sent_t is not None}
            if w.send_error is not None:
                entry["send_error"] = w.send_error
            elif w.sent_t is None:
                entry["send_error"] = "not sent: the probe stopped before the send completed"
            if w.sent_t is not None:
                end = w.end_t if w.end_t is not None else sess.close_t
                entry["sent_t"] = round(w.sent_t - self.capture.t0, 4)
                entry["window_s"] = round(end - w.sent_t, 3) if end is not None else None
            entry["answered"] = bool(w.filled)
            entry["complete"] = w.required_done()
            entry["replies"] = [r.data for r in sorted(w.filled.values(), key=lambda r: r.t)]
            entry["missing"] = [p.describe for p in w.parts
                                if p.required and p.name not in w.filled]
            entry["missing_optional"] = [p.describe for p in w.parts
                                         if not p.required and p.name not in w.filled]
            entry["late"] = [r.data for r in w.late.values()]
            entry["other_events"] = [r.data for r in w.other]
            out.append(entry)

    # -- 3. conclusions -------------------------------------------------------------

    def conclude(self) -> None:
        r = self.report
        r["sessions"] = [s.to_json() for s in self.sessions]
        r["passive"] = [rec.data for s in self.sessions for rec in s.records
                        if rec.data["phase"] in ("connect", "passive")
                        and not isinstance(rec.event, TransportClosed)]
        if self.unreachable:
            r["completed"] = False
            self.result()
            return
        self.conclude_firmware()
        self.conclude_sweep()
        self.conclude_terminators()
        self.conclude_routing()
        self.conclude_messages()
        r["completed"] = self.finished and not self.aborted and not r["interrupted"]
        self.result()

    def conclude_firmware(self) -> None:
        r = self.report
        if not any(s.connected for s in self.sessions):
            return
        fw, src, raw, versions = self.firmware_now()
        r["firmware_identity_raw"] = raw
        r["firmware_version_source"] = src
        if fw is not None:
            r["firmware_version"] = _fw_text(fw)
            if len(versions) > 1:
                self.warn("the robot reported different firmware versions: " + ", ".join(versions))
        elif raw is not None:
            self.error(f"firmware version could not be parsed: the identity line {raw!r} "
                       f"({src}) contains no xx.yy.zz version")
        else:
            self.error("firmware version unknown: no INFO;34 identity line was received on "
                       "any connection")

    def conclude_sweep(self) -> None:
        r = self.report
        if r["chosen_terminator"] is None or self.sweep_session is None:
            return
        if not self.sweep_session.connected:
            return  # the connect error says why
        sent = [w for w in self.windows if w.sent_t is not None]
        if sent and not any(w.filled for w in sent):
            self.error(f"the sweep got no reply to any command ({len(sent)} sent); see the "
                       "capture file for anything the robot sent that could not be decoded")
        for w in sent:
            waited = (w.end_t or w.sent_t) - w.sent_t  # type: ignore[operator]
            late = "; a matching message arrived late (see 'late')" if w.late else ""
            if w.command == "INFO" and "identity" not in w.filled:
                self.error(f"sweep: INFO got no INFO;34 identity reply within {waited:.1f} s"
                           f"{late}")
            elif not w.filled:
                need = "; ".join(p.describe for p in w.parts if p.required)
                self.warn(f"sweep: {w.command}: no reply within {waited:.1f} s (expected: "
                          f"{need}){late}")
            elif not w.required_done():
                need = "; ".join(p.describe for p in w.parts
                                 if p.required and p.name not in w.filled)
                self.warn(f"sweep: {w.command}: incomplete reply, missing {need}{late}")
        n_late = len(r["late"])
        if n_late:
            self.warn(f"{n_late} message(s) arrived after the reply window of the command they "
                      "probably answer (listed under 'late'); run again with a longer --pace "
                      "and --reply-timeout")
        status = [w.filled["status"] for w in sent if "status" in w.filled]
        if status:
            ev = status[0].event
            r["global_status"] = status[0].data
            if isinstance(ev, GlobalStatusFrame):
                r["tower_firmware"] = {
                    "t1": "{0}.{1}.{2}".format(*ev.t1_firmware),
                    "t2": "{0}.{1}.{2}".format(*ev.t2_firmware),
                }
                r["tower_firmware_note"] = "from the 19-byte G...S global-status frame"
            else:
                r["tower_firmware_note"] = ("not available: the 48-byte GLOBAL STATUS blob "
                                            "(GFW 01.02.00) carries no tower firmware")
        else:
            r["tower_firmware_note"] = "unknown: no global-status reply was decoded"
        if r["extended"]["reason"] is None:
            r["extended"]["reason"] = "not reached: the sweep ended before them"

    def conclude_terminators(self) -> None:
        """Warn about every terminator verdict that is still uncertain."""
        chosen = self.report["chosen_terminator"]
        confirm = [w for w in self.windows
                   if any(not _is_identity(rec.event) for rec in w.filled.values())]
        for e in self.report["terminator_tests"]:
            if not e.get("uncertain"):
                continue
            if e["mode"] == chosen and confirm:
                # A reply that no connect-time push can imitate: the robot
                # accepted commands framed this way.
                e["confirmed_by_sweep"] = (f"{confirm[0].command} was answered with terminator "
                                           f"{chosen!r} in the sweep")
                self.note(f"{e['session']}: {e['uncertain']}; but the sweep confirms that the "
                          f"robot accepts this framing ({e['confirmed_by_sweep']})")
                e["uncertain"] = None
                continue
            self.warn(f"{e['session']}: {e['uncertain']}: the verdict for this variant is "
                      "uncertain (run again with a longer --listen, e.g. 10, so that the push "
                      "arrives before the send)")

    def conclude_routing(self) -> None:
        findings: List[str] = []
        roles = (
            ("INFO/ERR/WARN line", 1,
             lambda ev: isinstance(ev, TextMessage) and ev.kind in ("INFO", "ERR", "WARN")),
            ("free-form text", 0, lambda ev: isinstance(ev, TextMessage) and ev.kind == "TEXT"),
            ("position frame", 2, lambda ev: isinstance(ev, PositionFrame)),
            ("tower frame", 0, lambda ev: isinstance(ev, TowerFrame)),
            ("global-status reply", 0, _is_status),
        )
        for s in self.sessions:
            for what, expected, test in roles:
                bad = [rec for rec in s.records
                       if rec.conn is not None and rec.conn != expected and test(rec.event)]
                if bad:
                    where = ", ".join(f"{tag} x{n}" for tag, n in
                                      sorted(Counter(rec.data["conn"] for rec in bad).items()))
                    first = bad[0]
                    findings.append(
                        f"{s.name}: {len(bad)} {what}(s) arrived on {where} instead of "
                        f"{CONN_TAGS[expected]} (first: {_brief(first.data)!r}, "
                        f"{first.data['phase']})"
                    )
        sweep = self.sweep_session
        if sweep is not None and sweep.sends:
            first_send = sweep.sends[0][1]
            after = [rec for rec in sweep.records
                     if rec.t >= first_send and rec.conn is not None
                     and not isinstance(rec.event, (TransportClosed, ListenerFailed))]
            on_s2 = [rec for rec in after if rec.conn == 1]
            others = [rec for rec in after if rec.conn != 1]
            if not on_s2 and others:
                findings.append(
                    f"sweep: nothing at all arrived on S2 after the first command, while "
                    f"{', '.join(sorted({rec.data['conn'] for rec in others}))} received "
                    f"{len(others)} message(s): every INFO/ERR/WARN reply (INFO;34, INFO;40, "
                    "INFO;0) went to another connection slot"
                )
        pushes = [(s, any(rec.conn == 1 and rec.data["phase"] in ("connect", "passive")
                          for rec in s.identity_records()))
                  for s in self.sessions if s.connected]
        if pushes and pushes[0][1]:
            for s, pushed in pushes[1:]:
                if not pushed:
                    self.warn(f"{s.name}: no connect-time INFO;34 arrived on S2, although "
                              f"{pushes[0][0].name} got one: S2 may not have landed in firmware "
                              "slot 1 (were the previous session's slots released?)")
        self.report["routing"] = findings
        for f in findings:
            self.error("SLOT ROUTING: " + f)
        if findings:
            self.error("SLOT ROUTING: " + ROUTING_HINT)

    def conclude_messages(self) -> None:
        """ERR/WARN lines and discarded bytes anywhere become warnings.

        One exception: ``ERR;1`` (CMD_NOT_EXECUTED) after a terminator variant
        that got no identity is plausibly the robot's answer to a command it
        did not recognise, so it is a note, not a warning. Every other ERR/WARN
        there is a warning: GFW 01.02.00 sends nothing for an unknown command,
        so such a line is most likely unsolicited (a fault the robot reported
        on its own) and must not pass as a protocol fact.
        """
        tests = {e["session"]: e for e in self.report["terminator_tests"]}
        for s in self.sessions:
            test = tests.get(s.name) if s.kind == "terminator-test" else None
            for rec in s.records:
                ev = rec.event
                if isinstance(ev, TextMessage) and ev.kind in ("ERR", "WARN"):
                    name = _err_name(ev)
                    what = f"{ev.raw!r}{' (' + name + ')' if name else ''}"
                    where = f"on {rec.data['conn']} ({rec.data['phase']})"
                    in_unanswered_test = (test is not None and not test["responded"]
                                          and rec.data["phase"] in ("after INFO", "trailing"))
                    if (in_unanswered_test and ev.kind == "ERR"
                            and ev.code == int(Error.CMD_NOT_EXECUTED)):
                        self.note(f"{s.name}: the robot sent {what} {where}, probably its "
                                  f"answer to {test['sent']!r}, a command it did not "
                                  "recognise. GFW 01.02.00 sends nothing for an unknown "
                                  "command, so either this firmware differs there or the "
                                  "error was unsolicited")
                    elif in_unanswered_test:
                        self.warn(f"{s.name}: the robot sent {what} {where}. It is not an "
                                  f"answer to {test['sent']!r}: GFW 01.02.00 does not answer "
                                  "unknown commands, and this code does not mean 'unknown "
                                  "command', so it is probably unsolicited (the robot "
                                  "reported a fault or warning of its own)")
                    else:
                        self.warn(f"{s.name}: the robot sent {what} {where}")
                elif isinstance(ev, Discarded):
                    self.warn(f"{s.name}: {len(ev.raw)} byte(s) on {rec.data['conn']} could not "
                              f"be framed and were discarded ({ev.reason}); they are in the "
                              "capture file")

    # (result: _Reporting)


# --------------------------------------------------------------------------- #
# Slot-release sweep and idle keepalive (rung R1b)
# --------------------------------------------------------------------------- #

#: How :func:`goliath_link.transport.configure_socket` describes keepalive
#: timing it could set completely.
_KEEPALIVE_TIMING_RE = re.compile(r"\(idle (\d+) s, interval (\d+) s, count (\d+)\)")

#: Verdicts of a gap trial that mean "this gap was not shown to work".
_FAILED_GAP_VERDICTS = ("silent", "misrouted", "connect_failed", "link_lost", "send_failed")

_SWEEP_ROUTING_HINT = (
    "The robot routes its replies by connection slot, so each session's S1/S2/S3 must be the "
    "1st/2nd/3rd connections it accepted. Likely causes: another client may be connected to "
    "the robot (close Slingshot on every computer and any phone app, then power-cycle the "
    "robot and run the sweep again), or the robot had not yet released an earlier session's "
    "connections (wait 10 s and run it again)."
)


def _slot_release_guard(command: str) -> None:
    """Refuse anything but INFO in the sweep (checked before :func:`_checked_send`'s own gate)."""
    if command != "INFO" or command not in READ_ONLY_COMMANDS:
        raise UnsafeCommandError(f"the slot-release sweep sends only INFO, not {command!r}")


def _validate_gaps(gaps: object) -> Tuple[float, ...]:
    """``gaps`` as a tuple of floats: at least one, finite, >= 0, strictly decreasing."""
    try:
        values = tuple(float(g) for g in gaps)  # type: ignore[union-attr]
    except (TypeError, ValueError):
        raise ValueError(f"gaps must be numbers of seconds, not {gaps!r}") from None
    if not values:
        raise ValueError("gaps needs at least one value")
    for g in values:
        if not (math.isfinite(g) and g >= 0):
            raise ValueError(f"every gap must be a finite number of seconds >= 0, not {g!r}")
    if any(b >= a for a, b in zip(values, values[1:])):
        raise ValueError("gaps must be strictly decreasing, largest first (each gap is measured "
                         f"from a session the previous gap left working), not {list(values)!r}")
    return values


def _keepalive_teardown(options: Sequence[str]) -> Optional[float]:
    """Seconds after which this computer drops a silent connection whose keepalive probes go unanswered.

    ``idle + interval x count``, read from the transport's description of each
    connection's socket options (:func:`goliath_link.transport.configure_socket`),
    the largest over the connections. None unless every connection states all
    three: with the OS default probe count, or timing that could not be set,
    the teardown time is unknown here.
    """
    if not options:
        return None
    worst = 0.0
    for text in options:
        m = _KEEPALIVE_TIMING_RE.search(text)
        if m is None:
            return None
        idle, interval, count = (int(x) for x in m.groups())
        worst = max(worst, float(idle + interval * count))
    return worst


def _link_loss_cause(error: Optional[BaseException]) -> str:
    """A plain-words cause for the error that took a connection down."""
    if error is None:
        return "closed locally"
    if isinstance(error, TimeoutError) or getattr(error, "errno", None) == errno.ETIMEDOUT:
        return (f"timed out ({error!r}): this computer gave up on the connection; unanswered "
                "TCP keepalive probes end a connection this way, and so does a lost Wi-Fi link")
    if isinstance(error, (ConnectionResetError, ConnectionAbortedError)):
        return f"reset ({error!r}): the robot or its Wi-Fi module ended the connection"
    if "closed by the robot" in str(error):
        return f"closed by the robot ({error})"
    return f"failed: {error!r}"


def _keepalive_fact(*, ran: bool, stayed_up: Optional[bool], idle_s: Optional[float],
                    teardown_s: Optional[float], silent_conns: Sequence[str],
                    down_text: Optional[str], min_idle: float) -> Tuple[Optional[bool], str]:
    """The value of fact ``link.keepalive_ok`` (True, False or None) and why.

    True only when the observation can carry it: the link stayed up through an
    idle of at least ``max(min_idle, 2 x teardown_s)`` seconds with at least
    one connection receiving nothing. A link that went down is an observed
    False, whatever the cause.
    """
    if not ran or stayed_up is None:
        return None, ("the keepalive session did not get to the end of its idle period, so "
                      "nothing was observed about an idle link")
    if not stayed_up:
        return False, f"the link went down during the idle ({down_text}): an idle link did not survive"
    idle = float(idle_s or 0.0)
    if idle < min_idle:
        drop = (f" (this computer drops a connection whose keepalive probes go unanswered after "
                f"about {teardown_s:g} s)" if teardown_s is not None else "")
        return None, (f"the link stayed up, but the idle lasted only {idle:.1f} s; at least "
                      f"{min_idle:g} s are needed to show anything{drop}: run the sweep with "
                      f"--keepalive-idle {min_idle:g} or more")
    if teardown_s is None:
        return None, (f"the link stayed up for {idle:.1f} s, but this computer's TCP keepalive "
                      "probe count is not known (see socket_options), so it cannot be said that "
                      "a connection whose probes went unanswered would have been dropped by then")
    required = max(min_idle, 2.0 * teardown_s)
    if idle < required:
        return None, (f"the link stayed up, but the idle lasted only {idle:.1f} s; at least "
                      f"{required:g} s are needed: twice the {teardown_s:g} s after which this "
                      "computer drops a connection whose keepalive probes go unanswered (run "
                      f"the sweep with --keepalive-idle {required:g} or more)")
    if not silent_conns:
        return None, ("every connection received bytes during the idle, so none was silent long "
                      "enough for keepalive probes to matter: the link survived, but whether the "
                      "robot answers keepalive probes is not shown")
    return True, (f"{', '.join(silent_conns)} received nothing for {idle:.1f} s and stayed up; "
                  "this computer would have dropped a connection whose keepalive probes went "
                  f"unanswered after about {teardown_s:g} s, so the robot answers them")


class _SlotReleaseRun(_Reporting):
    """The slot-release sweep and the idle keepalive session (module docstring, last section)."""

    def __init__(self, host: str, port: int, capture: _CaptureLog, report: dict, *,
                 term_mode: str, listen: float, reply_timeout: float, connect_timeout: float,
                 gaps: Sequence[float], recovery_wait: float, keepalive_idle: float,
                 connect_gaps: Sequence[float], settle: float, link_keepalive: Keepalive,
                 opener) -> None:
        self.host = host
        self.port = port
        self.capture = capture
        self.report = report
        self.term_mode = term_mode
        self.terminator = dict(TERMINATORS)[term_mode]
        self.listen = listen
        self.reply_timeout = reply_timeout
        self.connect_timeout = connect_timeout
        self.gaps = tuple(gaps)
        self.recovery_wait = recovery_wait
        self.keepalive_idle = keepalive_idle
        self.connect_gaps = tuple(connect_gaps)
        self.settle = settle
        self.link_keepalive = link_keepalive
        self.opener = opener
        self.sessions: List[_Session] = []
        self.phase = "starting"
        self.aborted: List[str] = []
        self.finished = False
        self.unreachable = False
        self.sr: dict = report["slot_release"]
        self.ka: dict = report["keepalive"]

    # -- _Session hook ------------------------------------------------------------

    def connect_failed(self, sess: _Session, exc: BaseException) -> None:
        """Only "never reached the robot" is reported here.

        Any other failed connect is judged by the caller: in a gap trial it is
        a gap that did not work (the robot closes connections it has no free
        slot for); in a reference or keepalive session it stops the run.
        """
        if any(s.opened for s in self.sessions):
            return
        self.unreachable = True
        cause = exc.__cause__ if exc.__cause__ is not None else exc
        self.error(
            f"{sess.name}: could not connect to {self.host}:{self.port}: {cause!r}. Is this "
            "computer joined to the robot's Goliath_xxxxxx Wi-Fi (or on the same network as "
            "it), and is the robot switched on?"
        )

    # -- one session: connect -> listen -> INFO -> wait for INFO;34 ---------------

    def new(self, cls, name: str, kind: str, label: str) -> _Session:
        sess = cls(self, name, kind, label, self.term_mode)
        self.sessions.append(sess)
        return sess

    def wait_since(self, since: Optional[float], seconds: float, why: str) -> None:
        self.phase = why
        self.capture.note(f"{why}: {seconds:g} s from the previous close")
        if since is None:
            since = time.monotonic()
        remaining = since + seconds - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    def send_info(self, sess: _Session) -> Optional[str]:
        """The sweep's only send: INFO, through the guard and :meth:`send_checked`."""
        _slot_release_guard(SLOT_RELEASE_COMMAND)
        why = self.send_checked(sess, SLOT_RELEASE_COMMAND, self.terminator)
        if why is None:
            self.report["commands_sent"].append({
                "session": sess.name, "command": SLOT_RELEASE_COMMAND,
                "terminator": self.term_mode, "conn": "S1",
                "t": round(sess.sends[-1][1] - self.capture.t0, 4),
            })
        return why

    def exchange(self, sess: _Session) -> dict:
        """connect -> passive listen -> INFO -> wait for INFO;34. The session stays open.

        Returns ``{"connect_t": float, "problem": None or (kind, detail)}``.
        """
        out: dict = {"connect_t": time.monotonic(), "problem": None}
        self.phase = f"{sess.name}: connecting"
        if not sess.connect():
            out["problem"] = ("connect_failed", f"could not connect: {sess.connect_error}")
            return out
        self.phase = f"{sess.name}: passive listen"
        if not sess.pump_for(self.listen):
            out["problem"] = ("link_lost",
                              f"the link was lost before INFO was sent: {self.describe_loss(sess)}")
            return out
        why = self.send_info(sess)
        if why is not None:
            out["problem"] = ("send_failed", why)
            return out
        sent_t = sess.sends[-1][1]
        self.phase = f"{sess.name}: waiting for INFO;34"

        def deadline() -> float:
            after = [r for r in sess.identity_records() if r.t >= sent_t]
            if after:
                return min(sent_t + self.reply_timeout, after[0].t + REPLY_GRACE + 0.2)
            return sent_t + self.reply_timeout

        alive = sess.pump(deadline)
        sess.window_end = time.monotonic()
        if alive and not any(r.t >= sent_t for r in sess.identity_records()):
            # A reply slower than --reply-timeout must still reach the probe
            # (the robot drops what it sends to a closed slot), so that it is
            # reported as late instead of as lost.
            self.phase = f"{sess.name}: trailing listen after the reply window"
            alive = sess.pump_for(min(self.listen, self.reply_timeout))
        if not alive:
            out["problem"] = ("link_lost",
                              f"the link was lost while waiting for INFO;34: {self.describe_loss(sess)}")
        return out

    def close(self, sess: _Session) -> float:
        try:
            sess.close()
        finally:
            closed_t = time.monotonic()
        return closed_t

    @staticmethod
    def routing(sess: _Session) -> Tuple[str, List[dict], bool]:
        """(verdict, identity lines, late) from what the session received.

        ``routed``: INFO;34 on S2 after the INFO and nowhere else.
        ``misrouted``: INFO;34 on S1 or S3 (the connections took other slots).
        ``unanswered``: INFO;34 on S2 only before the INFO (the connect-time
        push): S2 holds slot 1, but the robot did not answer.
        ``silent``: no INFO;34 anywhere (it went to a slot no connection of
        this session holds).
        """
        sent_t = sess.sends[0][1] if sess.sends else None
        lines = [{"conn": r.data["conn"], "phase": r.data["phase"], "t": r.data["t"],
                  "after_send": sent_t is not None and r.t >= sent_t, "raw": r.data["raw"]}
                 for r in sess.identity_records()]
        s2_after = [x for x in lines if x["conn"] == "S2" and x["after_send"]]
        if any(x["conn"] != "S2" for x in lines):
            return "misrouted", lines, False
        if s2_after:
            return "routed", lines, s2_after[0]["phase"] in ("trailing", "at close")
        if lines:
            return "unanswered", lines, False
        return "silent", lines, False

    def outcome(self, sess: _Session, ex: dict) -> dict:
        verdict, lines, late = self.routing(sess)
        detail: Optional[str] = None
        problem = ex["problem"]
        if problem is not None:
            kind, detail = problem
            # A link lost after INFO;34 already arrived does not undo where it arrived.
            if kind != "link_lost" or not sess.sends or verdict not in ("routed", "misrouted"):
                verdict = kind
        routed: Optional[bool] = True if verdict == "routed" else (
            False if verdict in _FAILED_GAP_VERDICTS else None)
        return {"session": sess.name, "verdict": verdict, "routed": routed, "late": late,
                "identity_lines": lines, "detail": detail}

    def report_late(self, o: dict) -> None:
        if o["late"]:
            self.warn(f"{o['session']}: INFO;34 arrived on S2 only after the "
                      f"{self.reply_timeout:g} s reply window: the robot answered more slowly than "
                      "--reply-timeout (run the sweep again with --reply-timeout 10)")

    def stop(self, o: dict) -> None:
        """A session the next step depends on did not work: record why; nothing more is sent."""
        v, name = o["verdict"], o["session"]
        self.sr["stopped"] = f"{name}: {v}"
        if v == "silent":
            msg = (f"{name}: no INFO;34 arrived on any connection after INFO. Another client "
                   "(Slingshot on any computer, a phone app) may hold the robot's 2nd connection "
                   "slot, the robot may not yet have released an earlier session's slots, or it "
                   "may be in G-code upload mode, where every buffer it receives is stored as "
                   "program data. The sweep stops here rather than send more: close every other "
                   "app, power-cycle the robot, and run it again.")
        elif v == "misrouted":
            where = ", ".join(sorted({x["conn"] for x in o["identity_lines"] if x["conn"] != "S2"}))
            msg = (f"SLOT ROUTING: {name}: INFO;34 arrived on {where} instead of S2, so this "
                   "session's connections did not land in firmware slots 0, 1, 2. "
                   + _SWEEP_ROUTING_HINT + " The sweep stops here.")
        elif v == "unanswered":
            msg = (f"{name}: the robot did not answer INFO, although its connect-time INFO;34 "
                   "arrived on S2 (so S2 held slot 1). This sweep sends INFO with terminator "
                   f"{self.term_mode!r}: pass the one rung R1's report chose (--terminator). Or "
                   "the robot is in G-code upload mode, where every buffer it receives is stored "
                   "as program data. The sweep stops here: power-cycle the robot and run it again.")
        elif v == "connect_failed" and self.unreachable:
            return  # reported by connect_failed, with the Wi-Fi hint
        else:
            msg = f"{name}: {o['detail']}. The sweep stops here."
        self.error(msg)

    def skip_keepalive(self, reason: str) -> None:
        self.ka["skipped_reason"] = reason
        self.ka["fact"]["why"] = f"the keepalive session did not run: {reason}"

    # -- the procedure --------------------------------------------------------------

    def reference(self, after: str, since: Optional[float]) -> Tuple[Optional[str], float]:
        """A session whose INFO;34 must land on S2 before a gap is measured from it.

        Returns (its name if it did, else None; when it was closed).
        """
        n = len(self.sr["references"]) + 1
        name = f"reference {n}"
        if since is not None:
            self.wait_since(since, self.recovery_wait, f"recovery wait before {name}")
        sess = self.new(_Session, name, "slot-release-reference", f"ref-{n}")
        ex: dict = {"connect_t": None, "problem": None}
        try:
            ex = self.exchange(sess)
        finally:
            closed_t = self.close(sess)
        o = self.outcome(sess, ex)
        self.sr["references"].append(dict(o, after=after,
                                          waited_s=None if since is None else self.recovery_wait))
        if o["verdict"] == "routed":
            self.report_late(o)
            return name, closed_t
        self.stop(o)
        return None, closed_t

    def trial(self, gap: float, ref_name: str, ref_closed: float) -> Tuple[dict, float]:
        name = f"gap {gap:g} s"
        self.wait_since(ref_closed, gap, f"gap of {gap:g} s before {name}")
        sess = self.new(_Session, name, "slot-release-trial", f"gap-{gap:g}")
        ex: dict = {"connect_t": None, "problem": None}
        try:
            ex = self.exchange(sess)
        finally:
            closed_t = self.close(sess)
        o = self.outcome(sess, ex)
        entry = dict(gap_s=gap, actual_gap_s=round(ex["connect_t"] - ref_closed, 3),
                     reference_session=ref_name, **o)
        self.sr["trials"].append(entry)
        self.report_late(o)
        if o["verdict"] in ("connect_failed", "link_lost", "send_failed"):
            self.warn(f"{name}: {o['detail']}; counted as a gap that did not work")
        elif o["verdict"] == "routed" and o["detail"]:
            self.warn(f"{name}: {o['detail']} (after INFO;34 had landed on S2)")
        return entry, closed_t

    def run(self) -> None:
        ref, closed_t = self.reference("start", None)
        if ref is None:
            self.sr["untested_gaps_s"] = list(self.gaps)
            self.skip_keepalive("the sweep stopped at its first reference session")
            self.finished = True
            return
        last_closed = closed_t
        failed_gap: Optional[float] = None
        for i, gap in enumerate(self.gaps):
            if ref is None:
                ref, closed_t = self.reference(f"gap {failed_gap:g} s did not work", last_closed)
                last_closed = closed_t
                if ref is None:
                    self.sr["untested_gaps_s"] = list(self.gaps[i:])
                    self.skip_keepalive("the sweep stopped at a reference session")
                    self.finished = True
                    return
            entry, last_closed = self.trial(gap, ref, closed_t)
            if entry["routed"] is None:
                self.stop(entry)
                self.sr["untested_gaps_s"] = list(self.gaps[i + 1:])
                self.skip_keepalive(f"the sweep stopped at {entry['session']}")
                self.finished = True
                return
            if entry["routed"]:
                ref, closed_t = entry["session"], last_closed
            else:
                ref, failed_gap = None, gap
        self.sr["complete"] = True
        self.keepalive_session(last_closed)
        self.finished = True

    def keepalive_session(self, since: float) -> None:
        self.wait_since(since, self.recovery_wait, "recovery wait before the keepalive session")
        sess = _IdleSession(self, "keepalive session", "keepalive", "keepalive", self.term_mode)
        self.sessions.append(sess)
        self.ka["ran"] = True
        self.ka["session"] = sess.name
        ex: dict = {"connect_t": None, "problem": None}
        before: Dict[int, int] = {}
        stayed_up: Optional[bool] = None
        try:
            ex = self.exchange(sess)
            if ex["problem"] is None:
                before = {c.index: c.bytes_received for c in sess.transport.connection_info()}
                sess.idle_start = time.monotonic()
                self.phase = f"keepalive session: idle for {self.keepalive_idle:g} s"
                self.capture.note(f"[{sess.label}] idle for {self.keepalive_idle:g} s; nothing is sent")
                alive = sess.pump_for(self.keepalive_idle)
                sess.idle_end = time.monotonic()
                stayed_up = alive and sess.transport.connected
        finally:
            self.close(sess)
        self.finish_keepalive(sess, ex, before, stayed_up)

    def finish_keepalive(self, sess: _IdleSession, ex: dict, before: Dict[int, int],
                         stayed_up: Optional[bool]) -> None:
        ka = self.ka
        info = sess.transport.connection_info()
        ka["socket_options"] = [c.socket_options for c in info]
        teardown = _keepalive_teardown(ka["socket_options"])
        ka["keepalive_teardown_s"] = teardown
        ka["idle_required_for_fact_s"] = (None if teardown is None
                                          else max(KEEPALIVE_MIN_IDLE, 2.0 * teardown))
        o = self.outcome(sess, ex)
        ka["routing_verdict"] = o["verdict"]
        ka["identity_lines"] = o["identity_lines"]
        ka["commands_sent"] = [c for c, _ in sess.sends]
        down_text: Optional[str] = None
        if sess.idle_start is not None and sess.idle_end is not None:
            end = sess.idle_end
            closed = sess.closed
            if not stayed_up:
                ev = closed.event if closed is not None else None
                error = getattr(ev, "error", None)
                if closed is not None and error is not None:
                    ka["went_down_after_s"] = round(max(0.0, closed.t - sess.idle_start), 3)
                    ka["went_down_conn"] = _tag(getattr(ev, "conn", None))
                    ka["went_down_error"] = repr(error)
                    ka["went_down_cause"] = _link_loss_cause(error)
                    end = min(end, closed.t)
                    down_text = (f"{self.describe_loss(sess)}, {ka['went_down_after_s']:g} s into "
                                 f"the idle; {ka['went_down_cause']}")
                else:
                    down_text = "the transport reported the link down without naming a cause"
            ka["link_stayed_up"] = bool(stayed_up)
            ka["idle_observed_s"] = round(max(0.0, end - sess.idle_start), 3)
            during = {_tag(c.index): c.bytes_received - before.get(c.index, 0) for c in info}
            ka["bytes_received_during_idle"] = during
            ka["silent_connections"] = [tag for tag, n in during.items() if n == 0] if stayed_up else []
            ka["events_during_idle"] = [r.data for r in sess.records if r.data["phase"] == "idle"]
        value, why = _keepalive_fact(
            ran=True, stayed_up=ka["link_stayed_up"], idle_s=ka["idle_observed_s"],
            teardown_s=teardown, silent_conns=ka["silent_connections"], down_text=down_text,
            min_idle=KEEPALIVE_MIN_IDLE)
        ka["fact"] = {"id": "link.keepalive_ok", "value": value, "why": why}

        if ex["problem"] is not None and sess.idle_start is None:
            if not (ex["problem"][0] == "connect_failed" and self.unreachable):
                self.error(f"keepalive session: {ex['problem'][1]}; nothing was observed about an "
                           "idle link")
        elif o["verdict"] != "routed":
            if o["verdict"] == "misrouted":
                where = ", ".join(sorted({x["conn"] for x in o["identity_lines"]
                                          if x["conn"] != "S2"}))
                self.error(f"SLOT ROUTING: keepalive session: INFO;34 arrived on {where} instead "
                           "of S2. " + _SWEEP_ROUTING_HINT)
            else:
                self.error(f"keepalive session: INFO got no INFO;34 on S2 ({o['verdict']}); the "
                           "link's idle behaviour was still recorded")
        else:
            self.report_late(o)
        if value is True:
            self.note(f"keepalive session: link.keepalive_ok = true: {why}")
        elif value is False:
            self.warn(f"keepalive session: the idle link went down ({down_text}): "
                      "link.keepalive_ok = false. The runner must expect a silent link to drop")
        elif sess.idle_start is not None:
            self.warn(f"keepalive session: link.keepalive_ok is not set: {why}")

    # -- conclusions ----------------------------------------------------------------

    def conclude(self) -> None:
        r = self.report
        r["sessions"] = [s.to_json() for s in self.sessions]
        totals: Counter = Counter()
        for s in self.sessions:
            for c in s.transport.connection_info():
                totals[_tag(c.index)] += c.bytes_sent
        r["bytes_sent"] = {tag: totals[tag] for tag in CONN_TAGS}
        if not self.ka["ran"] and self.ka["skipped_reason"] is None:
            self.skip_keepalive("not reached: the run ended before it")
        self.conclude_firmware()
        self.conclude_slot_release()
        self.conclude_messages()
        r["completed"] = (self.finished and not self.aborted and not r["interrupted"]
                          and self.sr["complete"] and self.ka["link_stayed_up"] is not None)
        self.result()

    def conclude_firmware(self) -> None:
        r = self.report
        seen: List[Tuple[Optional[Tuple[int, int, int]], str]] = []
        for s in self.sessions:
            for rec in s.identity_records():
                seen.append((_firmware_from(rec.event), rec.data["raw"]))  # type: ignore[arg-type]
        versions = sorted({_fw_text(v) for v, _ in seen if v is not None})
        r["firmware_versions_seen"] = versions
        parsed = [(v, raw) for v, raw in seen if v is not None]
        if parsed:
            r["firmware_version"] = _fw_text(parsed[0][0])  # type: ignore[arg-type]
            r["firmware_identity_raw"] = parsed[0][1]
            if len(versions) > 1:
                self.warn("the robot reported different firmware versions: " + ", ".join(versions))
        elif seen:
            r["firmware_identity_raw"] = seen[0][1]
            self.warn(f"firmware version could not be parsed: the identity line {seen[0][1]!r} "
                      "contains no xx.yy.zz version, so these results cannot be tied to a "
                      "firmware version")

    def conclude_slot_release(self) -> None:
        sr = self.sr
        tested = [(t["gap_s"], t["routed"]) for t in sr["trials"]]
        threshold: Optional[float] = None
        for gap, ok in tested:
            if ok is not True:
                break
            threshold = gap
        failing = [gap for gap, ok in tested if ok is False]
        largest_failing = max(failing) if failing else None
        monotonic: Optional[bool] = None
        if tested:
            monotonic = largest_failing is None or all(
                gap > largest_failing for gap, ok in tested if ok is True)
        sr["threshold_s"] = threshold
        sr["largest_failing_gap_s"] = largest_failing
        sr["monotonic"] = monotonic
        if tested and tested[0][1] is False:
            self.error(f"no gap worked: even after the largest gap, {tested[0][0]:g} s, the next "
                       "session's INFO;34 did not land on S2, so the robot needs longer than that "
                       "to free a closed session's slots (or something else holds them). Run the "
                       "sweep again with larger gaps, e.g. --gaps 20,10,5,2")
        if monotonic is False:
            below = ", ".join(f"{gap:g} s" for gap, ok in tested
                              if ok is True and gap < largest_failing)  # type: ignore[operator]
            self.warn(f"the sweep is inconsistent: the gap of {largest_failing:g} s did not work, "
                      f"but the smaller gap(s) {below} did. Freeing a slot is not a simple delay "
                      "on this robot, or something else interfered. threshold_s is the smallest "
                      "gap above every one that failed; slot.release_wait_s is not set: run the "
                      "sweep again")
        fact = sr["fact"]
        if sr["stopped"]:
            fact["why"] = f"the sweep stopped before it finished ({sr['stopped']})"
        elif not tested:
            fact["why"] = "no gap was tried"
        elif threshold is None:
            fact["why"] = "no gap worked"
        elif not monotonic:
            fact["why"] = ("the sweep was not consistent: a smaller gap worked below one that "
                           "failed, so no single threshold describes it")
        elif not sr["complete"]:
            fact["why"] = "the sweep did not try every gap"
        else:
            fact["value"] = threshold
            fact["why"] = (f"the smallest gap at which INFO;34 still landed on S2, every larger gap "
                           f"working too (gaps tried: {', '.join(f'{g:g}' for g, _ in tested)} s). "
                           "Observed once: the project design (rung R1b) has the runner wait "
                           "max(2 x threshold, 1 s), and never less than its default until two "
                           "sweeps agree")
            if threshold == 0:
                self.note("every gap worked, down to 0 s: the robot freed the slots before the "
                          "probe could reconnect")

    def conclude_messages(self) -> None:
        """ERR/WARN lines and discarded bytes anywhere become warnings."""
        for s in self.sessions:
            for rec in s.records:
                ev = rec.event
                if isinstance(ev, TextMessage) and ev.kind in ("ERR", "WARN"):
                    name = _err_name(ev)
                    self.warn(f"{s.name}: the robot sent {ev.raw!r}"
                              f"{' (' + name + ')' if name else ''} on {rec.data['conn']} "
                              f"({rec.data['phase']})")
                elif isinstance(ev, Discarded):
                    self.warn(f"{s.name}: {len(ev.raw)} byte(s) on {rec.data['conn']} could not "
                              f"be framed and were discarded ({ev.reason}); they are in the "
                              "capture file")


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def run_probe(
    host: str,
    port: int,
    out_dir: str,
    *,
    terminator: str = "auto",
    listen: float = DEFAULT_LISTEN,
    reply_timeout: float = DEFAULT_REPLY_TIMEOUT,
    pace: float = DEFAULT_PACE,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    slot_release_wait: float = DEFAULT_SLOT_RELEASE_WAIT,
    extended: bool = False,
    connect_gaps: Sequence[float] = DEFAULT_CONNECT_GAPS,
    settle: float = DEFAULT_SETTLE,
    opener=_default_opener,
    stamp: Optional[str] = None,
) -> dict:
    """Run the probe; return the report dict (also written to ``out_dir``).

    Raises ValueError for invalid arguments (including a port outside
    1..65535) before connecting or writing anything. Errors during the run never escape: they are recorded in the
    report, the run goes on where that makes sense, and ``exit_code`` /
    ``result`` say how it went. Ctrl+C (KeyboardInterrupt) stops the run, is
    recorded (``interrupted``, exit code 130) and the files are still written;
    it is not re-raised. Other BaseExceptions (SystemExit) are recorded and
    re-raised after the files are written.
    """
    if terminator not in ("auto",) + tuple(m for m, _ in TERMINATORS):
        raise ValueError("terminator must be auto, none, lf or crlf")
    _validate_port(port)
    _validate_timing(listen=listen, reply_timeout=reply_timeout, pace=pace,
                     slot_release_wait=slot_release_wait, connect_timeout=connect_timeout)
    gaps = tuple(float(g) for g in connect_gaps)
    if len(gaps) < 2 or not all(math.isfinite(g) and g >= 0 for g in gaps):
        raise ValueError("connect_gaps needs two finite values >= 0")
    if not (math.isfinite(float(settle)) and settle >= 0):
        raise ValueError("settle must be a finite value >= 0")

    os.makedirs(out_dir, exist_ok=True)
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    capture_path = os.path.join(out_dir, f"goliath-probe-{stamp}.capture.txt")
    report_path = os.path.join(out_dir, f"goliath-probe-{stamp}.json")
    t0 = time.monotonic()
    report: dict = {
        "tool": "goliath_link.probe",
        "report_format": 2,
        "started_utc": stamp,
        "host": host,
        "port": port,
        "settings": {
            "terminator": terminator, "listen": listen, "reply_timeout": reply_timeout,
            "pace": pace, "slot_release_wait": slot_release_wait,
            "connect_timeout": connect_timeout, "extended": bool(extended),
            "connect_gaps": list(gaps[:2]), "settle": settle,
        },
        "read_only_commands": list(SWEEP_ORDER),
        "extended_commands": list(EXTENDED_ORDER),
        "never_sent": dict(NEVER_SENT),
        "allowlist_basis": (
            "read_only_commands: handlers disassembled in GFW 01.02.00 and found read-only; "
            "extended_commands: only with --extended and only on firmware newer than "
            "01.02.00, NOT verified (no newer firmware image survives)"
        ),
        "slot_roles": dict(SLOT_ROLES),
        "sessions": [],
        "passive": [],
        "terminator_tests": [],
        "chosen_terminator": None,
        "firmware_version": None,
        "firmware_version_source": None,
        "firmware_identity_raw": None,
        "tower_firmware": None,
        "tower_firmware_note": None,
        "global_status": None,
        "sweep": [],
        "extended": {"requested": bool(extended), "sent": False, "reason": None},
        "late": [],
        "unattributed": [],
        "routing": [],
        "errors": [],
        "warnings": [],
        "notes": [],
        "completed": False,
        "interrupted": False,
        "result": None,
        "exit_code": None,
        "capture_file": capture_path,
        "report_file": report_path,
    }
    capture = _CaptureLog(capture_path, t0)
    runner = _Run(host, port, capture, report, terminator=terminator, listen=listen,
                  reply_timeout=reply_timeout, pace=pace, slot_release_wait=slot_release_wait,
                  connect_timeout=connect_timeout, extended=bool(extended),
                  connect_gaps=gaps[:2], settle=float(settle), opener=opener)
    escaped: Optional[BaseException] = None
    try:
        runner.run()
    except KeyboardInterrupt:
        report["interrupted"] = True
        runner.error(f"interrupted by the user (Ctrl+C) while {runner.phase}; the results "
                     "are partial")
    except Exception as exc:  # a probe bug outside the per-phase handlers
        runner.unexpected("probe", exc)
    except BaseException as exc:  # SystemExit and the like: record, write, re-raise
        runner.error(f"stopped by {exc!r} while {runner.phase}")
        report["interrupted"] = True
        escaped = exc
    finally:
        try:
            runner.conclude()
        except Exception as exc:
            runner.unexpected("summarising the report", exc)
            runner.result()
        finally:
            capture.close()
            with open(report_path, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2)
    if escaped is not None:
        raise escaped
    return report


def run_slot_release_sweep(
    host: str,
    port: int,
    out_dir: str,
    *,
    gaps: Sequence[float] = DEFAULT_SWEEP_GAPS,
    keepalive_idle: float = DEFAULT_KEEPALIVE_IDLE,
    terminator: str = "auto",
    listen: float = DEFAULT_LISTEN,
    reply_timeout: float = DEFAULT_REPLY_TIMEOUT,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    recovery_wait: Optional[float] = None,
    connect_gaps: Sequence[float] = DEFAULT_CONNECT_GAPS,
    settle: float = DEFAULT_SETTLE,
    link_keepalive: Keepalive = DEFAULT_KEEPALIVE,
    opener=_default_opener,
    stamp: Optional[str] = None,
) -> dict:
    """Run the slot-release sweep and the idle keepalive session (rung R1b).

    Sends only INFO (module docstring, last section). Returns the report dict,
    also written to ``out_dir`` as ``goliath-probe-slot-release-<stamp>.json``
    with a raw capture next to it. ``terminator`` ``"auto"`` means ``"none"``
    (disclosed in the report). ``recovery_wait`` (seconds waited after a gap
    that did not work, and before the keepalive session) defaults to the
    largest gap, at least :data:`DEFAULT_SLOT_RELEASE_WAIT`. ``link_keepalive``
    is the TCP keepalive timing of every connection (the transport's default).

    Raises ValueError for invalid arguments before connecting or writing
    anything. Errors during the run are recorded in the report, never raised;
    Ctrl+C is recorded as ``interrupted`` (exit code 130) and the files are
    still written. Other BaseExceptions are re-raised after the files are written.
    """
    if terminator not in ("auto",) + tuple(m for m, _ in TERMINATORS):
        raise ValueError("terminator must be auto, none, lf or crlf")
    _validate_port(port)
    gap_values = _validate_gaps(gaps)
    for name, value in (("listen", listen), ("reply_timeout", reply_timeout),
                        ("connect_timeout", connect_timeout), ("keepalive_idle", keepalive_idle)):
        if not _finite(value, name) > 0:
            raise ValueError(f"{name} must be > 0 s, not {value!r}")
    if recovery_wait is None:
        recovery = max(gap_values[0], DEFAULT_SLOT_RELEASE_WAIT)
        recovery_source = (f"default: the largest gap, at least {DEFAULT_SLOT_RELEASE_WAIT:g} s "
                           "(the slot-release wait the transport and the first probe assume)")
    else:
        recovery = _finite(recovery_wait, "recovery_wait")
        if recovery < 0:
            raise ValueError(f"recovery_wait must be >= 0 s, not {recovery_wait!r}")
        recovery_source = "given by the caller"
    cgaps = tuple(float(g) for g in connect_gaps)
    if len(cgaps) < 2 or not all(math.isfinite(g) and g >= 0 for g in cgaps):
        raise ValueError("connect_gaps needs two finite values >= 0")
    if not (math.isfinite(float(settle)) and settle >= 0):
        raise ValueError("settle must be a finite value >= 0")
    if not isinstance(link_keepalive, Keepalive):
        raise ValueError(f"link_keepalive must be a Keepalive, not {link_keepalive!r}")
    if terminator == "auto":
        term_mode = "none"
        term_source = ("default for --slot-release-sweep: GFW 01.02.00 compares each received "
                       "buffer exactly, so a bare INFO with no terminator is what it parses "
                       "(docs/protocol/09, 'How a command is parsed'); rung R1's report records "
                       "the terminator the robot accepted: pass --terminator to use another")
    else:
        term_mode = terminator
        term_source = "given (--terminator)"

    os.makedirs(out_dir, exist_ok=True)
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    capture_path = os.path.join(out_dir, f"goliath-probe-slot-release-{stamp}.capture.txt")
    report_path = os.path.join(out_dir, f"goliath-probe-slot-release-{stamp}.json")
    t0 = time.monotonic()
    ka_settings = {"idle_s": float(link_keepalive.idle), "interval_s": float(link_keepalive.interval),
                   "count": link_keepalive.count,
                   "source": ("goliath_link.transport.DEFAULT_KEEPALIVE"
                              if link_keepalive == DEFAULT_KEEPALIVE else "given by the caller")}
    report: dict = {
        "tool": "goliath_link.probe",
        "report_format": 2,
        "mode": "slot-release-sweep",
        "started_utc": stamp,
        "host": host,
        "port": port,
        "settings": {
            "terminator": term_mode, "terminator_source": term_source,
            "listen": listen, "reply_timeout": reply_timeout, "connect_timeout": connect_timeout,
            "gaps_s": list(gap_values),
            "gaps_source": ("default (the project design, rung R1b)"
                            if gap_values == DEFAULT_SWEEP_GAPS else "given (--gaps)"),
            "recovery_wait_s": recovery, "recovery_wait_source": recovery_source,
            "keepalive_idle_s": float(keepalive_idle),
            "keepalive_idle_default_s": DEFAULT_KEEPALIVE_IDLE,
            "keepalive_idle_source": ("default" if float(keepalive_idle) == DEFAULT_KEEPALIVE_IDLE
                                      else "given (--keepalive-idle)"),
            "keepalive_min_idle_for_fact_s": KEEPALIVE_MIN_IDLE,
            "link_keepalive": ka_settings,
            "connect_gaps": list(cgaps[:2]), "settle": settle,
        },
        "read_only_commands": list(SWEEP_ORDER),
        "sends_only": [SLOT_RELEASE_COMMAND],
        "slot_roles": dict(SLOT_ROLES),
        "sessions": [],
        "commands_sent": [],
        "bytes_sent": {},
        "firmware_version": None,
        "firmware_versions_seen": [],
        "firmware_identity_raw": None,
        "slot_release": {
            "format": SLOT_RELEASE_SECTION_FORMAT,
            "command": SLOT_RELEASE_COMMAND,
            "check": ("a gap works when the next session's INFO;34 arrives on S2 (its 2nd "
                      "connection) after its INFO, and on no other connection"),
            "basis": ("docs/protocol/09: INFO;34 is sent to firmware slot 1 ('Senders') and each "
                      "accepted connection takes the first free slot ('Client slots'); GFW "
                      "01.02.00 static analysis, not yet observed on a robot"),
            "gap_meaning": ("seconds from the end of closing a session whose INFO;34 landed on S2 "
                            "to the start of opening the next session's first connection "
                            "(actual_gap_s is the measured value)"),
            "gaps_planned_s": list(gap_values),
            "recovery_wait_s": recovery,
            "references": [],
            "trials": [],
            "untested_gaps_s": [],
            "threshold_s": None,
            "largest_failing_gap_s": None,
            "monotonic": None,
            "complete": False,
            "stopped": None,
            "fact": {"id": "slot.release_wait_s", "value": None, "why": "the sweep did not run"},
        },
        "keepalive": {
            "format": SLOT_RELEASE_SECTION_FORMAT,
            "command": SLOT_RELEASE_COMMAND,
            "ran": False,
            "skipped_reason": None,
            "session": None,
            "idle_requested_s": float(keepalive_idle),
            "idle_default_s": DEFAULT_KEEPALIVE_IDLE,
            "idle_required_for_fact_s": None,
            "idle_observed_s": None,
            "link_keepalive": ka_settings,
            "keepalive_teardown_s": None,
            "socket_options": [],
            "routing_verdict": None,
            "identity_lines": [],
            "commands_sent": [],
            "link_stayed_up": None,
            "went_down_after_s": None,
            "went_down_conn": None,
            "went_down_error": None,
            "went_down_cause": None,
            "bytes_received_during_idle": {},
            "silent_connections": [],
            "events_during_idle": [],
            "fact": {"id": "link.keepalive_ok", "value": None,
                     "why": "the keepalive session did not run"},
        },
        "errors": [],
        "warnings": [],
        "notes": [],
        "completed": False,
        "interrupted": False,
        "result": None,
        "exit_code": None,
        "capture_file": capture_path,
        "report_file": report_path,
    }
    capture = _CaptureLog(capture_path, t0,
                          sessions="a reference, a gap trial or the keepalive session")
    runner = _SlotReleaseRun(
        host, port, capture, report, term_mode=term_mode, listen=listen,
        reply_timeout=reply_timeout, connect_timeout=connect_timeout, gaps=gap_values,
        recovery_wait=recovery, keepalive_idle=float(keepalive_idle), connect_gaps=cgaps[:2],
        settle=float(settle), link_keepalive=link_keepalive, opener=opener)
    if terminator == "auto":
        runner.note(f"terminator: INFO is sent with no terminator ('none'); {term_source}")
    escaped: Optional[BaseException] = None
    try:
        runner.run()
    except KeyboardInterrupt:
        report["interrupted"] = True
        runner.error(f"interrupted by the user (Ctrl+C) while {runner.phase}; the results "
                     "are partial")
    except Exception as exc:  # a probe bug outside the per-session handling
        runner.unexpected("slot-release sweep", exc)
    except BaseException as exc:  # SystemExit and the like: record, write, re-raise
        runner.error(f"stopped by {exc!r} while {runner.phase}")
        report["interrupted"] = True
        escaped = exc
    finally:
        try:
            runner.conclude()
        except Exception as exc:
            runner.unexpected("summarising the report", exc)
            runner.result()
        finally:
            capture.close()
            with open(report_path, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2)
    if escaped is not None:
        raise escaped
    return report


# --------------------------------------------------------------------------- #
# Console summary (never fails on a narrow console; robot text always escaped)
# --------------------------------------------------------------------------- #


def _out(line: str = "") -> None:
    """Print ``line``; never fails on a narrow console encoding.

    If the console cannot encode the line (cp1252 with output redirected on
    Windows, an ASCII locale ...), it is printed with ``\\u....`` escapes
    instead. Robot text is escaped before it gets here (:func:`_robot_text`),
    whatever the console.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        line.encode(encoding)
    except (UnicodeError, LookupError):
        line = line.encode("ascii", "backslashreplace").decode("ascii")
    print(line)


def _robot_text(text: str, limit: int = 90) -> str:
    """Robot-originated text, with every control or non-ASCII character escaped."""
    s = text.encode("unicode_escape").decode("ascii")
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _brief(d: dict) -> str:
    kind = d.get("type")
    if kind == "text":
        s = _robot_text(d.get("raw", ""))
        if not d.get("terminated", True):
            s += " [no terminator]"
    elif kind == "global_status_blob":
        s = "status blob [" + " ".join(f"{b:02x}" for b in d.get("fields", [])) + "]"
    elif kind == "global_status":
        s = (f"status frame, tower firmware {'.'.join(map(str, d.get('t1_firmware', [])))}"
             f" / {'.'.join(map(str, d.get('t2_firmware', [])))}")
    elif kind == "position":
        s = (f"position x={d.get('x', 0):.1f} y={d.get('y', 0):.1f} z={d.get('z', 0):.1f} "
             f"angle={d.get('angle', 0):.1f} line={d.get('gcode_line')}")
    elif kind == "tower":
        s = f"tower T{d.get('tower')} status {d.get('status')} battery {d.get('battery', 0):.0f}%"
    elif kind == "discarded":
        s = f"{d.get('length')} discarded byte(s) ({d.get('reason')})"
    elif kind == "closed":
        s = "link closed" + (f": {d['error']}" if d.get("error") else "")
    else:
        s = str(kind)
    return f"{d.get('conn') or '-'} {s}"


def _print_summary(r: dict) -> None:
    def row(label: str, value: str) -> None:
        _out(f" {label:<19}{value}")

    bar = "=" * 72
    _out()
    _out(bar)
    _out(f" Goliath probe summary: {str(r.get('result') or 'unknown').upper()} "
         f"(exit code {r.get('exit_code')})")
    _out(bar)
    row("Target:", f"{r['host']}:{r['port']}")
    row("Connections:", "S1 = 1st (commands, free-form/direct replies), "
                        "S2 = 2nd (INFO/ERR/WARN), S3 = 3rd (positions)")
    fw = r.get("firmware_version")
    row("Firmware:", f"{fw}  ({r.get('firmware_version_source')})" if fw else "unknown")
    tw = r.get("tower_firmware")
    row("Tower firmware:", f"T1 {tw['t1']}, T2 {tw['t2']}" if tw
        else str(r.get("tower_firmware_note") or "unknown"))
    gs = r.get("global_status")
    if gs:
        row("Global status:", _brief(gs))
    passive = list(r.get("passive", []))
    kinds = Counter(
        f"{d.get('conn') or '-'} "
        + (_robot_text(d.get("raw", ""), 24) if d.get("type") == "text" else str(d.get("type")))
        for d in passive
    )
    row("Unprompted msgs:", f"{len(passive)}" + (
        "  (" + ", ".join(f"{k} x{n}" for k, n in sorted(kinds.items())) + ")" if kinds else ""))
    for t in r.get("terminator_tests", []):
        if t.get("error") and not t.get("responded"):
            state = "NOT TESTED (" + _robot_text(str(t["error"]), 60) + ")"
        elif t.get("responded") and t.get("uncertain"):
            state = (f"UNCERTAIN (identity on {t.get('identity_conn')}, but it may be the "
                     "robot's late connect-time push; see WARNINGS)")
        elif t.get("responded"):
            state = f"ANSWERED (identity on {t.get('identity_conn')})"
        else:
            other = [_brief(x) for x in t.get("replies", []) if x.get("type") != "closed"]
            state = "no answer" + (("; got " + "; ".join(other[:3])) if other else "")
        row(f"Terminator {t['mode']:<5}:", state)
    row("Chosen terminator:", str(r.get("chosen_terminator") or "NONE WORKED"))
    if r.get("sweep"):
        _out(f" Sweep (terminator {r.get('chosen_terminator')}):")
        for s in r["sweep"]:
            if not s.get("sent"):
                text = "NOT SENT: " + _robot_text(str(s.get("send_error")), 70)
            else:
                parts = [_brief(x) + ("" if x.get("conn_ok", True) else " [UNEXPECTED CONNECTION]")
                         for x in s.get("replies", [])]
                waited = s.get("window_s")
                text = "; ".join(parts) if parts else (
                    f"(no reply in {waited:.1f} s)" if isinstance(waited, (int, float))
                    else "(no reply)")
                if s.get("late"):
                    text += "; LATE: " + "; ".join(_brief(x) for x in s["late"])
                if s.get("other_events"):
                    text += "; also in window: " + "; ".join(_brief(x) for x in s["other_events"][:3])
            _out(f"   {s['command']:<24} -> {text}")
    ext = r.get("extended", {})
    row("Extended commands:", str(ext.get("reason") or "not requested"))
    if r.get("late"):
        row("Late messages:", f"{len(r['late'])} (JSON 'late', with 'possibly_for')")
    if r.get("unattributed"):
        row("Unattributed:", f"{len(r['unattributed'])} (JSON 'unattributed', with the reason)")
    for label, key in (("NOTES", "notes"), ("WARNINGS", "warnings"), ("ERRORS", "errors")):
        if r.get(key):
            _out(f" {label}:")
            for e in r[key]:
                _out(f"   - {e}")
    _out(f" Report:  {r['report_file']}")
    _out(f" Capture: {r['capture_file']}")
    _out(bar)
    _out(" Please send both files back. They contain no Wi-Fi passwords, but they DO")
    _out(" include your robot's serial number: share them privately, not in a public issue.")


_VERDICT_TEXT = {
    "routed": "ROUTED (INFO;34 on S2)",
    "silent": "SILENT (INFO;34 on no connection: the previous session's slots were "
              "probably still held)",
    "misrouted": "MISROUTED (INFO;34 on {where}, not S2: the connections took other slots)",
    "unanswered": "UNANSWERED (connect-time INFO;34 on S2, but no reply to INFO)",
    "connect_failed": "CONNECT FAILED",
    "link_lost": "LINK LOST",
    "send_failed": "SEND FAILED",
}


def _verdict_text(entry: dict) -> str:
    v = str(entry.get("verdict"))
    where = ", ".join(sorted({x["conn"] for x in entry.get("identity_lines", [])
                              if x.get("conn") != "S2"})) or "-"
    text = _VERDICT_TEXT.get(v, v.upper()).format(where=where)
    if entry.get("late"):
        text += " [late]"
    if entry.get("detail") and v != "routed":
        text += ": " + _robot_text(str(entry["detail"]), 70)
    return text


def _print_sweep_summary(r: dict) -> None:
    def row(label: str, value: str) -> None:
        _out(f" {label:<19}{value}")

    bar = "=" * 72
    s = r["settings"]
    sr, ka = r["slot_release"], r["keepalive"]
    _out()
    _out(bar)
    _out(f" Goliath slot-release sweep summary: {str(r.get('result') or 'unknown').upper()} "
         f"(exit code {r.get('exit_code')})")
    _out(bar)
    row("Target:", f"{r['host']}:{r['port']}")
    sent = r.get("bytes_sent") or {}
    row("Sent:", f"only INFO: {len(r.get('commands_sent', []))} time(s), one per session; bytes "
                 + ", ".join(f"{tag} {sent.get(tag, 0)}" for tag in CONN_TAGS))
    def origin(source: str) -> str:
        return "default" if source.startswith("default") else "given"

    gaps = ",".join(f"{g:g}" for g in s["gaps_s"])
    row("Settings:", f"terminator {s['terminator']} ({origin(s['terminator_source'])}); "
                     f"gaps {gaps} s ({origin(s['gaps_source'])}); "
                     f"recovery wait {s['recovery_wait_s']:g} s ({origin(s['recovery_wait_source'])})")
    row("", f"keepalive idle {s['keepalive_idle_s']:g} s ({origin(s['keepalive_idle_source'])}; "
            f"default {s['keepalive_idle_default_s']:g} s)")
    row("Firmware:", str(r.get("firmware_version") or "unknown"))
    for ref in sr["references"]:
        row(f"{ref['session'].capitalize()}:", _verdict_text(ref))
    for t in sr["trials"]:
        actual = t.get("actual_gap_s")
        measured = f" (measured {actual:.2f} s)" if isinstance(actual, (int, float)) else ""
        row(f"Gap {t['gap_s']:g} s:", _verdict_text(t) + measured)
    if sr.get("untested_gaps_s"):
        row("Not tried:", ", ".join(f"{g:g} s" for g in sr["untested_gaps_s"]))
    thr = sr.get("threshold_s")
    row("Threshold:", (f"{thr:g} s" if thr is not None else "NONE")
        + "; fact slot.release_wait_s = "
        + ("not set" if sr["fact"]["value"] is None else f"{sr['fact']['value']:g} s"))
    if not ka["ran"]:
        row("Keepalive:", "NOT RUN: " + str(ka.get("skipped_reason")))
    else:
        if ka.get("link_stayed_up") is True:
            state = f"link stayed up through {ka['idle_observed_s']:.1f} s of idle"
        elif ka.get("link_stayed_up") is False:
            state = (f"link WENT DOWN {ka.get('went_down_after_s')} s into the idle "
                     f"({ka.get('went_down_conn') or 'the link'})")
        else:
            state = "no idle observed"
        value = ka["fact"]["value"]
        row("Keepalive:", f"{state}; link.keepalive_ok = "
            + ("not set" if value is None else str(value).lower()))
    for label, key in (("NOTES", "notes"), ("WARNINGS", "warnings"), ("ERRORS", "errors")):
        if r.get(key):
            _out(f" {label}:")
            for e in r[key]:
                _out(f"   - {e}")
    _out(f" Report:  {r['report_file']}")
    _out(f" Capture: {r['capture_file']}")
    _out(bar)
    _out(" Please send both files back. They contain no Wi-Fi passwords, but they DO")
    _out(" include your robot's serial number: share them privately, not in a public issue.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _seconds(text: str) -> float:
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of seconds: {text!r}") from None
    if not math.isfinite(v):
        raise argparse.ArgumentTypeError(f"must be a finite number of seconds, not {text!r}")
    return v


def _pace_arg(text: str) -> float:
    v = _seconds(text)
    if v < MIN_PACE:
        raise argparse.ArgumentTypeError(
            f"must be at least {MIN_PACE} s, not {text}: the robot parses each received "
            "buffer as one command, so commands sent closer together can merge into one "
            "unknown command")
    return v


def _positive_arg(text: str) -> float:
    v = _seconds(text)
    if v <= 0:
        raise argparse.ArgumentTypeError(f"must be more than 0 s, not {text}")
    return v


def _non_negative_arg(text: str) -> float:
    v = _seconds(text)
    if v < 0:
        raise argparse.ArgumentTypeError(f"must be 0 s or more, not {text}")
    return v


def _port_arg(text: str) -> int:
    try:
        return _validate_port(int(text, 10))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"must be a whole number from 1 to 65535, not {text!r}") from None


def _gaps_arg(text: str) -> Tuple[float, ...]:
    try:
        values = [float(part) for part in text.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected comma-separated seconds, e.g. 5,2,1,0.5,0.25,0, not {text!r}") from None
    try:
        return _validate_gaps(values)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m goliath_link.probe",
        description="READ-ONLY first-contact probe for a Goliath robot. By default it sends "
                    "only: " + ", ".join(SWEEP_ORDER) + ". Close Slingshot and any phone app "
                    "first: nothing else may be connected to the robot.",
    )
    ap.add_argument("--host", default="10.0.0.1", help="robot address (default: its own AP, 10.0.0.1)")
    ap.add_argument("--port", type=_port_arg, default=80,
                    help="robot TCP port, 1-65535 (default: 80)")
    ap.add_argument("--out", default="probe-results", help="directory for the report and capture")
    ap.add_argument("--terminator", choices=["auto", "none", "lf", "crlf"], default="auto",
                    help="test all three variants (auto) or only one")
    ap.add_argument("--listen", type=_positive_arg, default=DEFAULT_LISTEN,
                    help=f"seconds of passive listening after each connect (default {DEFAULT_LISTEN:g})")
    ap.add_argument("--reply-timeout", type=_positive_arg, default=DEFAULT_REPLY_TIMEOUT,
                    help=f"seconds to wait for each expected reply (default {DEFAULT_REPLY_TIMEOUT:g})")
    # --pace and --slot-release-wait default to None so that main() can refuse
    # them with --slot-release-sweep (which would otherwise ignore them silently).
    ap.add_argument("--pace", type=_pace_arg, default=None,
                    help=f"minimum seconds between sweep commands, at least {MIN_PACE:g} "
                         f"(default {DEFAULT_PACE:g})")
    ap.add_argument("--slot-release-wait", type=_non_negative_arg, default=None,
                    help="seconds to wait between sessions so the robot frees its connection "
                         f"slots (default {DEFAULT_SLOT_RELEASE_WAIT:g})")
    ap.add_argument("--extended", action="store_true",
                    help="also send GET Z BACKLASH and SEND TCP POS, but only if the robot "
                         "reports firmware newer than 01.02.00 (01.02.00 does not have them). "
                         "NOT verified read-only: no newer firmware image survives")
    default_gaps = ",".join(f"{g:g}" for g in DEFAULT_SWEEP_GAPS)
    ap.add_argument("--slot-release-sweep", action="store_true",
                    help="rung R1b, instead of the steps above: sessions of connect, INFO, "
                         "close with shrinking gaps, each of which must still get its INFO;34 "
                         "on S2 (how fast the robot frees its connection slots), then one "
                         "session that sends INFO once and stays idle (whether an idle link "
                         "stays up). Sends only INFO, one per session. Writes the JSON "
                         "sections 'slot_release' and 'keepalive'. --terminator defaults to "
                         "none here")
    ap.add_argument("--gaps", type=_gaps_arg, default=None, metavar="SECONDS,...",
                    help="with --slot-release-sweep: strictly decreasing seconds between closing "
                         f"one session and opening the next (default {default_gaps})")
    teardown = DEFAULT_KEEPALIVE.idle + DEFAULT_KEEPALIVE.interval * DEFAULT_KEEPALIVE.count
    ap.add_argument("--keepalive-idle", type=_positive_arg, default=None, metavar="SECONDS",
                    help="with --slot-release-sweep: seconds the keepalive session stays idle "
                         f"after its one INFO (default {DEFAULT_KEEPALIVE_IDLE:g} s; at least "
                         f"{KEEPALIVE_MIN_IDLE:g} s are needed to set link.keepalive_ok, because "
                         "the link drops a silent connection whose keepalive probes go "
                         f"unanswered after about {teardown:g} s)")
    return ap


def _main_sweep(args: argparse.Namespace) -> int:
    gaps = DEFAULT_SWEEP_GAPS if args.gaps is None else args.gaps
    idle = DEFAULT_KEEPALIVE_IDLE if args.keepalive_idle is None else args.keepalive_idle
    _out("Goliath READ-ONLY slot-release sweep (rung R1b). Sends only: INFO, one per session.")
    _out(f"Gaps {','.join(f'{g:g}' for g in gaps)} s"
         + (" (default)" if args.gaps is None else "")
         + f", then one session idle for {idle:g} s"
         + (" (default)" if args.keepalive_idle is None else "")
         + (", terminator none (default here)" if args.terminator == "auto"
            else f", terminator {args.terminator}") + ".")
    _out("Robot power-cycled, nothing else connected (Slingshot, phone apps), bit removed and "
         "robot on blocks? Ctrl+C now if not.")
    _out()
    report = run_slot_release_sweep(
        args.host, args.port, args.out, gaps=gaps, keepalive_idle=idle,
        terminator=args.terminator, listen=args.listen, reply_timeout=args.reply_timeout,
    )
    _print_sweep_summary(report)
    return int(report["exit_code"])


def main(argv: Optional[List[str]] = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)
    if args.slot_release_sweep:
        for flag, given in (("--extended", args.extended), ("--pace", args.pace is not None),
                            ("--slot-release-wait", args.slot_release_wait is not None)):
            if given:
                ap.error(f"{flag} cannot be combined with --slot-release-sweep, which sends one "
                         "INFO per session and measures the slot-release wait itself (--gaps)")
        return _main_sweep(args)
    for flag, value in (("--gaps", args.gaps), ("--keepalive-idle", args.keepalive_idle)):
        if value is not None:
            ap.error(f"{flag} needs --slot-release-sweep")
    pace = DEFAULT_PACE if args.pace is None else args.pace
    slot_release_wait = (DEFAULT_SLOT_RELEASE_WAIT if args.slot_release_wait is None
                         else args.slot_release_wait)
    _out("Goliath READ-ONLY probe. Sends only: " + ", ".join(SWEEP_ORDER)
         + (" (+ GET Z BACKLASH, SEND TCP POS if the firmware is newer than 01.02.00)"
            if args.extended else ""))
    _out("Nothing else connected to the robot (Slingshot, phone apps)? Bit removed and "
         "robot on blocks? Ctrl+C now if not.")
    _out()
    report = run_probe(
        args.host, args.port, args.out,
        terminator=args.terminator, listen=args.listen, reply_timeout=args.reply_timeout,
        pace=pace, slot_release_wait=slot_release_wait, extended=args.extended,
    )
    _print_summary(report)
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
