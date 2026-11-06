# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""Read-only first-contact probe for a real Goliath robot.

    python -m goliath_link.probe                 # robot's own Wi-Fi AP (10.0.0.1:80)
    python -m goliath_link.probe --host 192.168.1.50

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
2. **Sweep.** With the first variant that got an identity reply, and only if
   the firmware gate (below) is open, one more session: passive listen, then
   each allowlisted command, at least ``--pace`` seconds apart, each waiting up
   to ``--reply-timeout`` for its expected reply; then a trailing listen.
3. Writes a **raw capture** (every byte sent and received, per connection,
   timestamped, in hex and ASCII) and a **JSON report** (every decoded event,
   tagged with its session, connection and phase, including what arrived during
   connect, in the passive windows and while closing), **shareable copies** of
   both (see "Privacy"), prints a summary and returns an exit code.

The firmware gate
=================

Every command below was checked against GFW 01.02.00 only. The first real
robot probed (rung R1, ``probe-results/README.md``) runs GFW 01.00.13, on
which what they do is unknown. So the probe reads the firmware version from
every identity line the robot sends (``INFO;34``, pushed on connect or sent in
reply to ``INFO``, or ``INFO;28``), on any connection and in any session, the
moment it arrives. The version is the ``GFW NN.NN.NN`` token at the start of
the project name, followed by a space or nothing: ``GFW 01.02.00   `` and the
dated 01.00.13 form ``GFW 01.00.13 +b \\x01\\x02Jul  2 2021`` both parse
(the rest is not part of the version). Anything else is unknown.

* Before any identity line has arrived, only ``INFO`` (the identity query) may
  be sent: the terminator test runs as before.
* Once the robot has identified itself as firmware **older than 01.02.00**,
  the probe sends **nothing more in the whole run**: no further terminator
  variant (it opens no further session), no sweep, no ``PRINT SYS REPORT``, no
  ``--extended``. If the identity arrives during the first passive listen, the
  probe sends no byte at all.
* The same holds when an identity line states no version the probe can read,
  or when identity lines state different versions (two robots, a reflash
  during the run, a garbled line): the firmware is then unknown.
* The sweep, and ``--extended``, run only once the robot has identified itself
  consistently as 01.02.00 or newer; on such firmware the probe behaves exactly
  as it did before the gate existed.

The gate is enforced twice: by the procedure, and at the send choke point
:func:`_checked_send`, which refuses every command the identities received so
far do not permit, whichever code path calls it. A stopped run says in the
report (``firmware_gate``) and in the summary why it stopped (firmware X below
01.02.00, identity unknown, identities contradictory), where, and which
commands it did not send; it prints a ``PROBE STOPPED`` line and exits with 4.

Privacy: the serial number and nearby Wi-Fi names
=================================================

The robot reports its serial number in every identity line, and GFW 01.00.13
lists the names of nearby Wi-Fi networks (an ``INFO;31`` scan) on every
connect; together they can locate the robot. The raw report
(``goliath-probe-<time>.json``) and the raw capture
(``goliath-probe-<time>.capture.txt``) keep everything as received: they are
the evidence and stay on the computer that ran the probe. Next to them the
probe writes **shareable copies**, ``goliath-probe-<time>.shareable.json`` and
``goliath-probe-<time>.shareable.capture.txt``, and it prints its summary
(everything on stdout and stderr) through the same redaction:

* the serial number becomes ``X`` repeated to its length;
* each distinct Wi-Fi name, numbered in the order it first arrived, becomes
  ``SSID01``, ``SSID02``, ... padded with ``_`` to its length. A name shorter
  than its placeholder gets the placeholder's last characters instead (``abc``,
  the third name, becomes ``D03``), or ``#`` repeated where that would be the
  name itself or another network's name: a placeholder never contains the name
  it replaces. Empty names (hidden networks) stay empty;
* the capture's hex and ASCII columns get the same substitution, applied to
  each connection's byte stream, so every message keeps its length and the
  robot's 512-byte NUL-padded blocks are unchanged.

A name that also occurs elsewhere in what the robot sent is replaced there too;
with a very short name this can hide unrelated robot text in the shareable
copies (the raw files keep it). What the probe writes itself (JSON keys,
connection and session labels, phases, command names, times, file names) is not
changed. The shareable report says that it is one (``shareable_copy``), and the
rung R1 importer refuses it: its facts could not be bound to the robot.

Safety: what it sends, and on what basis
========================================

* Only what the firmware gate (above) permits, and of that, by default, only
  these six commands (:data:`READ_ONLY_COMMANDS`): ``INFO``,
  ``GET GLOBAL SYS STATUS``, ``PRINT SYS REPORT``, ``GET SPINDLE CAGE STATUS``,
  ``READ INDEX CALIBRATION``, ``READ SPINDLE CALIB``. Their handlers in GFW
  01.02.00 were disassembled: none calls a motor, spindle, flash-erase or
  state-setting routine. (``READ INDEX CALIBRATION`` reads through the
  firmware's normal configuration getter, which rewrites its flash copy only
  if its two stored copies disagree; the vendor app triggers the same read
  routinely.) On firmware newer than 01.02.00 they are expected, not verified,
  to behave the same; to older or unknown firmware they are not sent.
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
* Every send goes through :func:`_checked_send`, which refuses anything else,
  and anything the firmware gate does not permit, at run time (not with an
  ``assert``). Nothing else in this module writes to a socket. On GFW
  01.02.00, connecting and disconnecting do not move the robot or change its
  work state (the accept and close paths only queue INFO notifications and
  free slots).

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
  an uncertain terminator verdict, ``--extended`` not honoured). 4 when the
  firmware gate stopped it (firmware older than 01.02.00, or an identity that
  is unknown or contradictory): **not** a success, and nothing of this robot's
  command set is learnt from it. 1 on errors (an error wins over 4 and 3).
  130 when interrupted with Ctrl+C.
  Every reason is listed in the summary and in the JSON. Invalid options exit
  with 2 before anything is sent.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import re
import sys
import textwrap
import threading
import time
import traceback
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

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
    DEFAULT_SETTLE,
    GoliathTransport,
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
#: The firmware gate (module docstring): anything but the identity query needs
#: the robot to identify itself as at least this version. Every command above
#: was checked on GFW 01.02.00 only.
SWEEP_MIN_FIRMWARE: Tuple[int, int, int] = (1, 2, 0)
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
#: The firmware gate stopped the run (module docstring): not a success.
EXIT_STOPPED = 4
EXIT_INTERRUPTED = 130

#: Where a stopped run points the owner (printed with the PROBE STOPPED line).
STOP_READING = ("probe-results/README.md (rung R1: the first robot probed runs GFW 01.00.13) and "
                "docs/field-guide/02-first-probe.md")

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


class UnsafeCommandError(RuntimeError):
    """Raised if anything tries to send a non-allowlisted command through the probe."""


class FirmwareGateClosed(UnsafeCommandError):
    """Raised by :func:`_checked_send` for a command the robot's identity does not permit."""


def _extended_permitted(firmware: object) -> bool:
    if not (isinstance(firmware, tuple) and len(firmware) == 3
            and all(isinstance(x, int) for x in firmware)):
        return False
    return firmware > EXTENDED_NEWER_THAN


# --------------------------------------------------------------------------- #
# The firmware gate (module docstring)
# --------------------------------------------------------------------------- #

Version = Tuple[int, int, int]

#: The version in an INFO;34 project name: the ``GFW NN.NN.NN`` token at its
#: start, followed by whitespace or nothing. ``GFW 01.02.00   `` (01.02.00) and
#: ``GFW 01.00.13 +b \x01\x02Jul  2 2021`` (01.00.13, rung R1) both match.
_GFW_VERSION = re.compile(r"[ \t]*GFW[ \t]+([0-9]{2})\.([0-9]{2})\.([0-9]{2})(?![^ \t])")
#: INFO;28 (SYS_INFO), vendor table: ``... Version:[...] ...``; no robot has sent one yet.
_SYS_INFO_VERSION = re.compile(r"Version:[ \t]*\[[ \t]*(?:GFW[ \t]+)?([0-9]{2})\.([0-9]{2})\.([0-9]{2})[ \t]*\]")
#: Gate verdicts that stop the probe for the rest of the run.
STOP_VERDICTS = frozenset({"older", "unknown", "contradictory"})


def identity_version(event: object) -> Optional[Version]:
    """The firmware version an identity line (INFO;34 or INFO;28) states, or None.

    None means unknown: the line is no identity line, or it states no version
    in the form above (``GFW 1.2.0``, ``GFW 01.02.003``, ``XYZ 01.02.00`` and
    ``GFW unknown`` are all unknown).
    """
    if not _is_identity(event):
        return None
    params = event.params  # type: ignore[attr-defined]
    if event.code == int(Info.APP_INFO):  # type: ignore[attr-defined]
        m = _GFW_VERSION.match(params[0]) if params else None
    else:
        m = _SYS_INFO_VERSION.search(";".join(params))
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


class _FirmwareGate:
    """What the robot has said about its firmware so far, and what that permits.

    Every identity line of the run is observed the moment a transport thread
    decodes it (any session, connection or phase), so the gate is current at
    each send; thread-safe. Verdicts:

    * ``none``: no identity line yet; only INFO (the identity query) is permitted;
    * ``ok``: one version, 01.02.00 or newer, and no unreadable identity line:
      the six read-only commands, and the extended ones on firmware newer than
      01.02.00 when ``--extended`` was given;
    * ``older``: firmware older than 01.02.00;
    * ``unknown``: an identity line states no version this probe can read;
    * ``contradictory``: identity lines state different versions.

    The last three (:data:`STOP_VERDICTS`) permit nothing, for the rest of the run.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: List[Tuple[Optional[Version], str]] = []  # (version or None, raw line)

    def observe(self, event: object) -> None:
        if _is_identity(event):
            entry = (identity_version(event), str(event.raw))  # type: ignore[attr-defined]
            with self._lock:
                self._seen.append(entry)

    def state(self) -> Tuple[str, Optional[Version], List[str], List[str]]:
        """(verdict, the version if exactly one was stated, versions stated, unreadable lines)."""
        with self._lock:
            seen = list(self._seen)
        versions = sorted({v for v, _ in seen if v is not None})
        unreadable = [raw for v, raw in seen if v is None]
        if not seen:
            verdict = "none"
        elif len(versions) > 1:
            verdict = "contradictory"
        elif versions and versions[0] < SWEEP_MIN_FIRMWARE:
            verdict = "older"
        elif unreadable:
            verdict = "unknown"
        else:
            verdict = "ok"
        version = versions[0] if len(versions) == 1 else None
        return verdict, version, [_fw_text(v) for v in versions], unreadable

    # The procedure asks these two; the choke point asks refusal().
    def stopped(self) -> bool:
        return self.state()[0] in STOP_VERDICTS

    def sweep_open(self) -> bool:
        return self.state()[0] == "ok"

    def stop_reason(self) -> Optional[str]:
        """Why the probe sends nothing more, or None while the gate has not stopped it."""
        verdict, version, versions, unreadable = self.state()
        minimum = _fw_text(SWEEP_MIN_FIRMWARE)
        if verdict == "older":
            assert version is not None
            return (f"the robot identified itself as firmware {_fw_text(version)}, older than "
                    f"{minimum}; every command of this probe was checked on {minimum} only, so "
                    "what they do on this firmware is unknown and the probe sends nothing more")
        if verdict == "contradictory":
            return (f"the robot's identity lines state different firmware versions "
                    f"({', '.join(versions)}): two robots, a reflash during the run, or a garbled "
                    "line; the firmware is unknown, so the probe sends nothing more")
        if verdict == "unknown":
            return (f"the robot's identity line {unreadable[0]!r} states no firmware version this "
                    "probe can read (GFW NN.NN.NN); the firmware is unknown, so the probe sends "
                    "nothing more")
        return None

    def refusal(self, command: str,
                extended_firmware: Optional[Version] = None) -> Optional[str]:
        """None if ``command`` may be sent now, else why not. Used by :func:`_checked_send`."""
        verdict, version, _versions, _unreadable = self.state()
        if verdict in STOP_VERDICTS:
            return self.stop_reason()
        if verdict == "none":
            if command == "INFO":
                return None
            return (f"{command} needs the robot to identify itself first as firmware "
                    f"{_fw_text(SWEEP_MIN_FIRMWARE)} or newer; only INFO may be sent before that")
        assert version is not None  # verdict "ok"
        if command in READ_ONLY_COMMANDS:
            return None
        if (command in EXTENDED_COMMANDS and extended_firmware is not None
                and tuple(extended_firmware) == version and _extended_permitted(version)):
            return None
        return (f"{command} needs --extended and firmware newer than "
                f"{_fw_text(EXTENDED_NEWER_THAN)}; the robot identified itself as "
                f"{_fw_text(version)}")


def _checked_send(transport, command: str, terminator: str, *, gate: _FirmwareGate,
                  extended_firmware: Optional[Tuple[int, int, int]] = None) -> None:
    """The probe's only path to the robot. Refuses anything not allowlisted or not permitted.

    :data:`READ_ONLY_COMMANDS` pass the allowlist. :data:`EXTENDED_COMMANDS`
    pass it only when ``extended_firmware`` is the robot's firmware version
    and it is newer than 01.02.00 (callers pass it only when ``--extended``
    was given). The terminator must be ``""``, ``"\\n"`` or ``"\\r\\n"``. Then
    ``gate`` (the run's :class:`_FirmwareGate`, required) must permit the
    command for the identities received so far; if not,
    :class:`FirmwareGateClosed` is raised and nothing is sent.
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
    if not isinstance(gate, _FirmwareGate):
        raise UnsafeCommandError(f"probe refuses {command!r}: no firmware gate to check it against")
    why = gate.refusal(command, extended_firmware)
    if why is not None:
        raise FirmwareGateClosed(f"probe refuses {command!r}: {why}")
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


_CAPTURE_HEADER = (
    "# goliath_link.probe raw capture. '>' = sent to the robot, '<' = received.\n"
    "# Each session (one terminator test, or the sweep) opens three connections:\n"
    "#   S1 = 1st (commands; free-form and direct replies), S2 = 2nd (INFO/ERR/WARN),\n"
    "#   S3 = 3rd (position frames) -- firmware slots 0/1/2 if nothing else is connected.\n"
    "# columns: seconds_since_start  direction  session  connection  hex  |  ascii\n"
)


def _note_line(rel_t: float, text: str) -> str:
    return f"# {rel_t:9.3f}  {text}\n"


def _data_line(rel_t: float, direction: str, session: str, conn: int, data: bytes) -> str:
    ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return f"{rel_t:9.3f}  {direction}  {session:<10}  {_tag(conn)}  {data.hex(' ')}  |  {ascii_}\n"


class _CaptureLog:
    """Timestamped, thread-safe log of every byte sent and received (UTF-8).

    The raw file is written as things happen. The entries are also kept, so
    that :meth:`shareable_text` can write the redacted copy at the end.
    """

    def __init__(self, path: str, t0: float) -> None:
        self.path = path
        self.t0 = t0
        self._lock = threading.Lock()
        #: ("note", rel_t, text) or ("data", rel_t, direction, session, conn, bytes), in order.
        self._entries: List[tuple] = []
        self._fh = open(path, "w", encoding="utf-8", newline="\n")
        self._fh.write(_CAPTURE_HEADER)
        self._fh.flush()

    def note(self, text: str) -> None:
        with self._lock:
            rel_t = time.monotonic() - self.t0
            self._entries.append(("note", rel_t, text))
            self._fh.write(_note_line(rel_t, text))
            self._fh.flush()

    def record(self, t: float, direction: str, session: str, conn: int, data: bytes) -> None:
        data = bytes(data)
        with self._lock:
            self._entries.append(("data", t - self.t0, direction, session, conn, data))
            self._fh.write(_data_line(t - self.t0, direction, session, conn, data))
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            self._fh.close()

    def streams(self) -> Dict[Tuple[str, int, str], Tuple[List[int], bytes]]:
        """Per (session, connection, direction): the entry indices and their bytes, joined."""
        with self._lock:
            entries = list(self._entries)
        parts: Dict[Tuple[str, int, str], Tuple[List[int], List[bytes]]] = {}
        for i, e in enumerate(entries):
            if e[0] == "data":
                indices, chunks = parts.setdefault((e[3], e[4], e[2]), ([], []))
                indices.append(i)
                chunks.append(e[5])
        return {key: (indices, b"".join(chunks)) for key, (indices, chunks) in parts.items()}

    def shareable_text(self, redactor: "_Redactor", raw_name: str) -> str:
        """The capture with the serial and Wi-Fi names replaced (module docstring, "Privacy").

        Each connection's byte stream is redacted as a whole, so a name split
        across two reads is still found, then cut back into the original
        pieces: every line keeps its byte count, every message its length.
        """
        with self._lock:
            entries = list(self._entries)
        redacted: Dict[int, bytes] = {}
        for indices, joined in self.streams().values():
            masked = redactor.data(joined)
            pos = 0
            for i in indices:
                n = len(entries[i][5])
                redacted[i] = masked[pos:pos + n]
                pos += n
        out = [f"# SHAREABLE COPY of {raw_name}: the robot's serial number and the names of nearby\n"
               "# Wi-Fi networks are replaced by placeholders of the same length (XXXX..., SSID01_...),\n"
               "# in the hex and the ascii columns alike. The raw capture keeps them; keep that one private.\n",
               _CAPTURE_HEADER]
        for i, e in enumerate(entries):
            if e[0] == "note":
                out.append(_note_line(e[1], redactor.text(e[2])))
            else:
                out.append(_data_line(e[1], e[2], e[3], e[4], redacted[i]))
        return "".join(out)


# --------------------------------------------------------------------------- #
# Privacy: the serial number and nearby Wi-Fi names (module docstring)
# --------------------------------------------------------------------------- #

#: A Wi-Fi name in an INFO;31 scan line (decoded text; the robot sends ``INFO;31;<name>``).
_SSID_TEXT = re.compile(r"INFO;31[; ](.*)\Z", re.DOTALL)
#: The same in the received bytes, up to the line's end, CR or NUL padding.
_SSID_BYTES = re.compile(rb"INFO;31[; ]([^\n\r\x00]*)")
#: An INFO;34 identity line in the received bytes: prj;HW;SW;serial.
_IDENTITY_BYTES = re.compile(rb"INFO;34;([^\n\r\x00]*)")
#: The scan's own markers, not network names.
_SCAN_MARKERS = frozenset({"START", "END"})
#: Report keys whose string values are the probe's own vocabulary or this
#: computer's file names, never robot data: left as they are.
_KEEP_KEYS = frozenset({
    "type", "kind", "conn", "phase", "session", "name", "mode", "terminator", "command", "part",
    "expected_conn", "identity_conn", "closed_conn", "possibly_for", "reply_to", "tool",
    "result", "chosen_terminator", "started_utc", "verdict", "host", "report_file",
    "capture_file", "shareable_report_file", "shareable_capture_file",
})
#: Event types whose "raw" is robot bytes in hex.
_HEX_EVENT_TYPES = frozenset({"discarded", "global_status_blob", "global_status"})


def ssid_placeholder(number: int, name: str, avoid: Sequence[str] = ()) -> str:
    """The placeholder for the ``number``-th distinct Wi-Fi ``name`` (module docstring).

    ``SSID01`` padded with ``_`` to the name's length; for a shorter name the
    placeholder's last characters (``abc`` as the 3rd name: ``D03``). Never
    the name itself, nor any name in ``avoid``: then ``#`` (or ``*``, ``~``,
    ``-``) repeated. Same length as ``name`` always; an empty name stays empty.
    """
    n = len(name)
    if n == 0:
        return ""
    base = "SSID%02d" % number
    candidate = base + "_" * (n - len(base)) if n >= len(base) else base[-n:]
    for fill in "#*~-":
        if candidate != name and candidate not in avoid:
            return candidate
        candidate = fill * n
    return candidate


def _text_forms(text: str) -> List[str]:
    """``text`` as it may appear inside the report's strings: as is, repr()'d, escaped."""
    escaped = text.encode("unicode_escape").decode("ascii")
    forms = [text, repr(text)[1:-1], escaped, repr(escaped)[1:-1]]
    return [f for i, f in enumerate(forms) if f and f not in forms[:i]]


def _events_in_order(report: dict) -> Iterator[dict]:
    """Every JSON object of the report: the sessions' events first, in the order they arrived."""
    for session in report.get("sessions") or []:
        if isinstance(session, dict):
            for event in session.get("events") or []:
                if isinstance(event, dict):
                    yield event
    stack: List[object] = [report]
    while stack:
        node = stack.pop(0)
        if isinstance(node, dict):
            yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


class _Redactor:
    """Masks the robot's serial number and the names of nearby Wi-Fi networks.

    Collect first (:meth:`collect_report`, then :meth:`collect_received` for
    each received byte stream), then apply (:meth:`text`, :meth:`data`,
    :meth:`report_copy`). Names are numbered in the order they were first
    collected, so the summary, the shareable report and the shareable capture
    use the same placeholders.
    """

    def __init__(self) -> None:
        self._serials: Dict[str, List[bytes]] = {}  # text -> byte forms, in first-seen order
        self._ssids: Dict[str, List[bytes]] = {}
        self._tables: Optional[Tuple[List[Tuple[str, str]], List[Tuple[bytes, bytes]]]] = None

    # -- collecting -----------------------------------------------------------

    def _add(self, table: Dict[str, List[bytes]], text: str, raw: Optional[bytes] = None) -> None:
        if not text:
            return
        forms = table.setdefault(text, [])
        if raw is not None and raw not in forms:
            forms.append(raw)
        self._tables = None

    def collect_report(self, report: dict) -> None:
        """The serials (INFO;34) and Wi-Fi names (INFO;31) in the report's decoded events."""
        for event in _events_in_order(report):
            raw = event.get("raw")
            if event.get("type") != "text" or not isinstance(raw, str):
                continue
            if raw.startswith("INFO;34;"):
                params = event.get("params")
                fields = params if isinstance(params, list) else raw.split(";")[2:]
                for field in fields[3:]:
                    if isinstance(field, str):
                        self._add(self._serials, field.strip())
            m = _SSID_TEXT.match(raw)
            if m and m.group(1) not in _SCAN_MARKERS:
                self._add(self._ssids, m.group(1))

    def collect_received(self, data: bytes) -> None:
        """The same, from one connection's received bytes (finds what the decoder cut or replaced)."""
        for m in _SSID_BYTES.finditer(data):
            # The decoder shows each non-ASCII byte as U+FFFD and drops trailing whitespace.
            text = m.group(1).decode("ascii", "replace").rstrip()
            if text and text not in _SCAN_MARKERS:
                self._add(self._ssids, text, m.group(1)[: len(text)])
        for m in _IDENTITY_BYTES.finditer(data):
            for field in m.group(1).split(b";")[3:]:
                field = field.strip()
                if field:
                    self._add(self._serials, field.decode("ascii", "replace"), field)

    @property
    def counts(self) -> Tuple[int, int]:
        """(serial numbers, Wi-Fi names) masked."""
        return len(self._serials), len(self._ssids)

    # -- applying -------------------------------------------------------------

    def _build(self) -> Tuple[List[Tuple[str, str]], List[Tuple[bytes, bytes]]]:
        if self._tables is None:
            avoid = list(self._serials) + list(self._ssids)
            texts: List[Tuple[str, str]] = []
            blobs: List[Tuple[bytes, bytes]] = []
            placeholders = [(s, "X" * len(s), forms) for s, forms in self._serials.items()]
            placeholders += [(s, ssid_placeholder(k, s, avoid), forms)
                             for k, (s, forms) in enumerate(self._ssids.items(), 1)]
            for original, placeholder, forms in placeholders:
                texts.extend((form, placeholder) for form in _text_forms(original))
                raw_forms = list(forms)
                if original.isascii():
                    raw_forms.append(original.encode("ascii"))
                blobs.extend((raw, placeholder.encode("ascii")) for raw in raw_forms
                             if len(raw) == len(placeholder))
            # Longest first: a name inside a longer one is replaced with the longer one.
            texts.sort(key=lambda pair: -len(pair[0]))
            blobs.sort(key=lambda pair: -len(pair[0]))
            self._tables = (texts, blobs)
        return self._tables

    def text(self, text: str) -> str:
        for original, placeholder in self._build()[0]:
            if original in text:
                text = text.replace(original, placeholder)
        return text

    def data(self, data: bytes) -> bytes:
        for original, placeholder in self._build()[1]:
            if original in data:
                data = data.replace(original, placeholder)
        return data

    def _hex(self, value: str) -> str:
        try:
            raw = bytes.fromhex(value)
        except ValueError:
            return self.text(value)
        return self.data(raw).hex(" ")

    def report_copy(self, node: object) -> object:
        """A copy of a report (or part of one) with every robot string masked."""
        if isinstance(node, dict):
            hexed = node.get("type") in _HEX_EVENT_TYPES
            out = {}
            for key, value in node.items():
                if isinstance(value, str) and key in _KEEP_KEYS:
                    out[key] = value
                elif hexed and isinstance(value, str) and key in ("raw", "flags_raw"):
                    out[key] = self._hex(value)
                else:
                    out[key] = self.report_copy(value)
            raw = out.get("raw")
            if (node.get("type") == "text" and node.get("kind") in ("INFO", "ERR", "WARN")
                    and isinstance(raw, str) and raw != node.get("raw") and "params" in out):
                # The parameters again from the masked line, as the decoder splits it: a
                # name with ';' in it spans several parameters.
                out["params"] = raw.split(";")[2:]
            return out
        if isinstance(node, (list, tuple)):
            return [self.report_copy(v) for v in node]
        if isinstance(node, str):
            return self.text(node)
        return node


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
            settle=run.settle, listener=self._listen,
        )

    # transport hooks (called on transport threads)
    def _open(self, host: str, port: int, timeout: float):
        sock = self.run.opener(host, port, timeout)
        self.opened += 1
        return sock

    def _listen(self, link: LinkEvent) -> None:
        # Every identity line reaches the firmware gate the moment it is decoded,
        # before the main thread reads it from the queue: the next send sees it.
        self.run.gate.observe(link.event)

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


class _GateStop(Exception):
    """The firmware gate refused a send: the probe stops sending (not an error)."""


class _Run:
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
        self.gate = _FirmwareGate()
        self.modes: Tuple[Tuple[str, str], ...] = TERMINATORS if terminator == "auto" else tuple(
            m for m in TERMINATORS if m[0] == terminator)
        #: Sweep commands still planned when the firmware gate stopped the sweep.
        self.sweep_not_sent: List[str] = []
        if not extended:
            report["extended"]["reason"] = "not requested (--extended not given)"

    # -- reporting ------------------------------------------------------------

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
        for i, (mode, term) in enumerate(self.modes):
            if self.gate.stopped():
                # The robot identified itself as firmware the probe must not talk to:
                # no further session, so no further connect either.
                self.capture.note(f"firmware gate: no terminator test '{mode}' or later: "
                                  f"{self.gate.stop_reason()}")
                break
            if i > 0:
                self.wait_for_slot_release()
            self.terminator_test(mode, term)
            if self.unreachable:
                break
        self.choose_terminator()
        chosen = self.report["chosen_terminator"]
        if chosen is not None and self.gate.sweep_open():
            self.wait_for_slot_release()
            self.sweep(chosen, dict(TERMINATORS)[chosen])
        elif chosen is not None:
            self.capture.note(f"firmware gate: no sweep: {self.gate.stop_reason()}")
        self.finished = True

    def send_checked(self, sess: _Session, command: str, term: str,
                     extended_firmware: Optional[Tuple[int, int, int]] = None) -> Optional[str]:
        """Send through the gates. Returns None on success, else why it failed.

        Raises :class:`_GateStop`, having sent nothing, when the firmware gate
        refuses the command (the identities received so far do not permit it).
        """
        self.phase = f"{sess.name}: sending {command}"
        if not sess.transport.connected:
            return f"{command} was not sent: {self.describe_loss(sess)}"
        if self.gate.refusal(command, extended_firmware) is None:
            sess.run.capture.note(f"[{sess.label}] sending {command + term!r}")
        sent_t = time.monotonic()
        try:
            _checked_send(sess.transport, command, term, gate=self.gate,
                          extended_firmware=extended_firmware)
        except FirmwareGateClosed as exc:
            sess.run.capture.note(f"[{sess.label}] not sent: {command + term!r}: {exc}")
            raise _GateStop(str(exc)) from None
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
            if self.gate.stopped():
                # Gate A: the robot identified itself while the probe only listened.
                raise _GateStop(str(self.gate.stop_reason()))
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
        except _GateStop as stop:
            entry["not_sent"] = f"INFO was not sent: {stop}"
            sess.run.capture.note(f"[{sess.label}] firmware gate: {entry['not_sent']}")
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
                checked = ("no sweep checks it: the firmware gate stopped the probe"
                           if self.gate.stopped() else "the sweep checks it")
                self.note(f"terminator {entry['mode']!r} chosen although its answer is "
                          f"uncertain (see the warnings); {checked}")
            return
        if self.gate.stopped():
            return  # nothing went unanswered: the firmware gate stopped the sends (firmware_gate)
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
            if not self.gate.sweep_open():
                # The sweep's own connect-time identity closed the gate (a contradiction).
                raise _GateStop(str(self.gate.stop_reason()))
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
        except _GateStop as stop:
            if self.windows and self.windows[-1].sent_t is None:
                self.windows.pop()  # the refused command was never sent: listed in firmware_gate
            self.sweep_not_sent = [c for c in planned if c not in done]
            if self.extended:
                self.sweep_not_sent += [c for c in EXTENDED_ORDER
                                        if c not in done and c not in self.sweep_not_sent]
                if not any(c in done for c in EXTENDED_ORDER):
                    self.report["extended"]["sent"] = False
                    self.report["extended"]["reason"] = "not sent: the firmware gate stopped the probe"
            sess.run.capture.note(f"[{sess.label}] firmware gate: not sent: "
                                  f"{', '.join(self.sweep_not_sent)}: {stop}")
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
        parsed = [(src, r, identity_version(r.event)) for _, src, r in candidates]
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
        verdict, gate_fw, gate_versions, _unreadable = self.gate.state()
        if len(versions) > 1 or verdict == "contradictory":
            reason = ("not sent: the robot reported different firmware versions ("
                      + ", ".join(gate_versions if len(gate_versions) > 1 else versions) + ")")
        elif fw is None or verdict != "ok" or gate_fw != fw:
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
            self.conclude_gate()
            self.result()
            return
        self.conclude_firmware()
        self.conclude_gate()
        self.conclude_sweep()
        self.conclude_terminators()
        self.conclude_routing()
        self.conclude_messages()
        r["completed"] = (self.finished and not self.aborted and not r["interrupted"]
                          and not r["firmware_gate"]["stopped"])
        self.result()

    def conclude_firmware(self) -> None:
        r = self.report
        if not any(s.connected for s in self.sessions):
            return
        fw, src, raw, versions = self.firmware_now()
        r["firmware_identity_raw"] = raw
        r["firmware_version_source"] = src
        verdict, _gate_fw, _versions, unreadable = self.gate.state()
        if verdict in ("unknown", "contradictory"):
            # Not an error: the firmware gate stopped the probe, and firmware_gate says why.
            r["firmware_version"] = None
            if raw is None and unreadable:
                r["firmware_identity_raw"] = unreadable[0]
            return
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

    def conclude_gate(self) -> None:
        """The report's ``firmware_gate``: open or stopped, why, where, and what was not sent."""
        g = self.report["firmware_gate"]
        verdict, version, versions, unreadable = self.gate.state()
        stopped = verdict in STOP_VERDICTS
        g.update(verdict=verdict, version=_fw_text(version) if version else None,
                 versions=versions, unreadable_identity_lines=unreadable, stopped=stopped)
        if stopped:
            g["reason"] = self.gate.stop_reason()
            g["stopped_at"] = self.stop_point()
            g["not_sent"] = self.not_sent()
            ext = self.report["extended"]
            if self.extended and not ext["sent"]:
                ext["reason"] = "not sent: the firmware gate stopped the probe"
        elif verdict == "ok":
            assert version is not None
            g["reason"] = (f"open: the robot identified itself as firmware {_fw_text(version)}, "
                           f"{_fw_text(SWEEP_MIN_FIRMWARE)} or newer")
        else:
            g["reason"] = "no identity line was received, so nothing but INFO was permitted"

    def stop_point(self) -> Optional[str]:
        """Where the gate stopped the run: the identity line that closed it, replayed in order."""
        records = sorted(((r, s) for s in self.sessions for r in s.identity_records()),
                         key=lambda pair: pair[0].t)
        replay = _FirmwareGate()
        for rec, sess in records:
            replay.observe(rec.event)
            if replay.stopped():
                before = [c for c, t in sess.sends if t <= rec.t]
                sent = (f"after sending {', '.join(repr(c) for c in before)} there" if before
                        else "before the probe had sent anything in that session")
                return (f"{rec.data['session']}, {rec.data['phase']}: the identity line on "
                        f"{rec.data['conn']}, {sent}")
        return None

    def not_sent(self) -> List[str]:
        """Every planned command the run did not send once the gate had stopped it."""
        out: List[str] = []
        tests = {e["mode"]: e for e in self.report["terminator_tests"]}
        for mode, term in self.modes:
            entry = tests.get(mode)
            if entry is None or entry.get("not_sent"):
                out.append(f"{'INFO' + term!r} (terminator test '{mode}')")
        if self.sweep_session is None:
            sweep = list(SWEEP_ORDER) + (list(EXTENDED_ORDER) if self.extended else [])
        else:
            sweep = list(self.sweep_not_sent)
        out += [f"{c!r} ({'--extended' if c in EXTENDED_COMMANDS else 'sweep'})" for c in sweep]
        return out

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
                        f"{CONN_TAGS[expected]} (first: {_brief(first.data, limit=None)!r}, "
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

    def result(self) -> None:
        r = self.report
        if r["interrupted"]:
            r["result"], r["exit_code"] = "interrupted", EXIT_INTERRUPTED
        elif r["errors"]:
            r["result"], r["exit_code"] = "failed", EXIT_FAILED
        elif (r.get("firmware_gate") or {}).get("stopped"):
            r["result"], r["exit_code"] = "stopped", EXIT_STOPPED
        elif r["warnings"]:
            r["result"], r["exit_code"] = "partial", EXIT_PARTIAL
        else:
            r["result"], r["exit_code"] = "ok", EXIT_OK


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

    Four files: the raw report and capture, and their shareable copies
    (``shareable_report_file``, ``shareable_capture_file``; module docstring,
    "Privacy"). If the copies cannot be made, those two keys are None and
    ``shareable_error`` says why; the raw files are written regardless.
    """
    return _run_probe(host, port, out_dir, terminator=terminator, listen=listen,
                      reply_timeout=reply_timeout, pace=pace, connect_timeout=connect_timeout,
                      slot_release_wait=slot_release_wait, extended=extended,
                      connect_gaps=connect_gaps, settle=settle, opener=opener, stamp=stamp)[0]


def _run_probe(host: str, port: int, out_dir: str, *, terminator: str, listen: float,
               reply_timeout: float, pace: float, connect_timeout: float,
               slot_release_wait: float, extended: bool, connect_gaps: Sequence[float],
               settle: float, opener, stamp: Optional[str]) -> Tuple[dict, Optional[_Redactor]]:
    """:func:`run_probe`, also returning the redaction its shareable copies used (None if none)."""
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
    shareable_capture_path = os.path.join(out_dir, f"goliath-probe-{stamp}.shareable.capture.txt")
    shareable_report_path = os.path.join(out_dir, f"goliath-probe-{stamp}.shareable.json")
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
        "firmware_gate": {
            "rule": (f"anything but the identity query (INFO) is sent only once the robot has "
                     f"identified itself as firmware {_fw_text(SWEEP_MIN_FIRMWARE)} or newer; on "
                     "older firmware, or an identity that is unreadable or contradictory, the "
                     "probe sends nothing more in the whole run"),
            "minimum": _fw_text(SWEEP_MIN_FIRMWARE),
            "verdict": None, "version": None, "versions": [], "unreadable_identity_lines": [],
            "stopped": False, "reason": None, "stopped_at": None, "not_sent": [],
        },
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
        "privacy": ("this report and its capture contain the robot's serial number and may contain "
                    "the names of nearby Wi-Fi networks (GFW 01.00.13 lists them whenever a "
                    "connection opens): keep them private, and share the shareable copies instead"),
        "shareable_report_file": shareable_report_path,
        "shareable_capture_file": shareable_capture_path,
    }
    capture = _CaptureLog(capture_path, t0)
    runner = _Run(host, port, capture, report, terminator=terminator, listen=listen,
                  reply_timeout=reply_timeout, pace=pace, slot_release_wait=slot_release_wait,
                  connect_timeout=connect_timeout, extended=bool(extended),
                  connect_gaps=gaps[:2], settle=float(settle), opener=opener)
    escaped: Optional[BaseException] = None
    redactor: Optional[_Redactor] = None
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
            redactor = _write_shareable(report, capture)
            with open(report_path, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2)
    if escaped is not None:
        raise escaped
    return report, redactor


def _write_shareable(report: dict, capture: _CaptureLog) -> Optional[_Redactor]:
    """Write the shareable copies (module docstring, "Privacy"); the redaction, or None.

    Never raises: if the copies cannot be made, the report says so
    (``shareable_error``) and names no shareable file.
    """
    try:
        redactor = _Redactor()
        redactor.collect_report(report)
        for (_session, _conn, direction), (_indices, data) in capture.streams().items():
            if direction == "<":
                redactor.collect_received(data)
        text = capture.shareable_text(redactor, os.path.basename(capture.path))
        serials, names = redactor.counts
        shared = redactor.report_copy(report)
        assert isinstance(shared, dict)
        shareable = {"shareable_copy": {
            "of": os.path.basename(str(report["report_file"])),
            "masked": (f"{serials} serial number(s), each character replaced by 'X', and {names} "
                       "Wi-Fi network name(s), each replaced by SSID01, SSID02, ... padded with "
                       "'_' to its length (a shorter name by a shorter placeholder of the same "
                       "length; empty names stay empty), in the capture's hex and ascii columns "
                       "too; every message keeps its length"),
            "not_importable": ("its facts cannot be bound to the robot: rung R1 imports the raw "
                               "report, on the computer that ran the probe"),
        }}
        shareable.update(shared)
        shareable["privacy"] = ("shareable copy: the robot's serial number and the names of nearby "
                                "Wi-Fi networks are replaced by placeholders")
        with open(str(report["shareable_capture_file"]), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        with open(str(report["shareable_report_file"]), "w", encoding="utf-8") as fh:
            json.dump(shareable, fh, indent=2)
        return redactor
    except Exception as exc:  # the raw files are written regardless
        for key in ("shareable_report_file", "shareable_capture_file"):
            path = report.get(key)
            report[key] = None
            if path:
                try:
                    os.remove(str(path))
                except OSError:
                    pass
        report["shareable_error"] = (f"no shareable copies were written ({type(exc).__name__}): "
                                     "do not share the report or the capture")
        return None


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


def _robot_text(text: str, limit: Optional[int] = 90) -> str:
    """Robot-originated text, with every control or non-ASCII character escaped."""
    s = text.encode("unicode_escape").decode("ascii")
    return s if limit is None or len(s) <= limit else s[: limit - 3] + "..."


def _brief(d: dict, limit: Optional[int] = 90) -> str:
    kind = d.get("type")
    if kind == "text":
        s = _robot_text(d.get("raw", ""), limit)
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


def _wrapped(text: str) -> None:
    for line in textwrap.wrap(text, width=78, initial_indent=" ", subsequent_indent=" ",
                              break_long_words=False, break_on_hyphens=False):
        _out(line)


def _print_summary(r: dict, redactor: Optional[_Redactor] = None) -> None:
    """Print the summary of report ``r`` with the serial and Wi-Fi names masked (module docstring).

    ``redactor`` is the one the shareable copies used (:func:`_run_probe`);
    without one, one is made from ``r``. If masking fails, only what holds no
    robot text is printed.
    """
    try:
        if redactor is None:
            redactor = _Redactor()
            redactor.collect_report(r)
        shown = redactor.report_copy(r)
        assert isinstance(shown, dict)
    except Exception as exc:
        _out()
        _out(f" Goliath probe: {str(r.get('result') or 'unknown').upper()} (exit code "
             f"{r.get('exit_code')}). The summary cannot be shown with the robot's serial number "
             f"and Wi-Fi names masked ({type(exc).__name__}); read it in the report. Do not share "
             "the report or the capture: they contain both.")
        _out(f" Report:  {r.get('report_file')}")
        _out(f" Capture: {r.get('capture_file')}")
        return
    try:
        _print_masked_summary(shown)
    except Exception as exc:  # a bug here must not print an exception that quotes robot text
        _out(f" (the summary stopped early: {type(exc).__name__}; the files are complete)")


def _print_masked_summary(r: dict) -> None:
    def row(label: str, value: str) -> None:
        _out(f" {label:<19}{value}")

    bar = "=" * 72
    gate = r.get("firmware_gate") or {}
    _out()
    _out(bar)
    _out(f" Goliath probe summary: {str(r.get('result') or 'unknown').upper()} "
         f"(exit code {r.get('exit_code')})")
    _out(bar)
    row("Target:", f"{r['host']}:{r['port']}")
    row("Connections:", "S1 = 1st (commands, free-form/direct replies), "
                        "S2 = 2nd (INFO/ERR/WARN), S3 = 3rd (positions)")
    fw = r.get("firmware_version")
    raw = r.get("firmware_identity_raw")
    row("Firmware:", f"{fw}  ({r.get('firmware_version_source')})" if fw else
        "unknown" + (f" (identity line: {_robot_text(str(raw), 70)})" if raw else ""))
    if gate.get("stopped"):
        row("Firmware gate:", "STOPPED: " + _robot_text(str(gate.get("reason")), None))
    elif gate.get("verdict") == "ok":
        row("Firmware gate:", "open (" + str(gate.get("version")) + ")")
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
        if t.get("not_sent"):
            state = "NOT SENT (firmware gate)"
        elif t.get("error") and not t.get("responded"):
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
    row("Chosen terminator:", str(r.get("chosen_terminator")
                                  or ("none chosen" if gate.get("stopped") else "NONE WORKED")))
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
    if gate.get("stopped"):
        row("Not sent:", ", ".join(str(c) for c in gate.get("not_sent") or []) or "-")
        if gate.get("stopped_at"):
            row("Stopped at:", str(gate["stopped_at"]))
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
    if gate.get("stopped"):
        _wrapped(f"PROBE STOPPED (exit code {EXIT_STOPPED}, not a success): "
                 + _robot_text(str(gate.get("reason")), None)
                 + ": no further terminator test, no sweep, no PRINT SYS REPORT, no --extended "
                 "(each one is listed under 'Not sent' above). Read " + STOP_READING
                 + " before anything else is sent to this robot.")
        _out(bar)
    if r.get("shareable_report_file") and r.get("shareable_capture_file"):
        _out(" Safe to share (the robot's serial number and nearby Wi-Fi names are replaced by")
        _out(" placeholders):")
        _out(f"   {r['shareable_report_file']}")
        _out(f"   {r['shareable_capture_file']}")
        _wrapped("Keep the Report and the Capture above private: they contain your robot's "
                 "serial number and may contain the names of nearby Wi-Fi networks, which can "
                 "locate it. None of the files contains a Wi-Fi password.")
    else:
        _wrapped(str(r.get("shareable_error") or "No shareable copies were written.")
                 + ". The Report and the Capture contain your robot's serial number and may "
                 "contain the names of nearby Wi-Fi networks: do not share them.")


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


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m goliath_link.probe",
        description="READ-ONLY first-contact probe for a Goliath robot. By default it sends "
                    "only: " + ", ".join(SWEEP_ORDER) + ", and anything but INFO only once the "
                    "robot has identified itself as firmware 01.02.00 or newer: on older firmware "
                    "or an unreadable identity it stops (exit code 4). Close Slingshot and any "
                    "phone app first: nothing else may be connected to the robot.",
    )
    ap.add_argument("--host", default="10.0.0.1", help="robot address (default: its own AP, 10.0.0.1)")
    ap.add_argument("--port", type=_port_arg, default=80,
                    help="robot TCP port, 1-65535 (default: 80)")
    ap.add_argument("--out", default="probe-results",
                    help="directory for the report, the capture and their shareable copies")
    ap.add_argument("--terminator", choices=["auto", "none", "lf", "crlf"], default="auto",
                    help="test all three variants (auto) or only one")
    ap.add_argument("--listen", type=_positive_arg, default=DEFAULT_LISTEN,
                    help=f"seconds of passive listening after each connect (default {DEFAULT_LISTEN:g})")
    ap.add_argument("--reply-timeout", type=_positive_arg, default=DEFAULT_REPLY_TIMEOUT,
                    help=f"seconds to wait for each expected reply (default {DEFAULT_REPLY_TIMEOUT:g})")
    ap.add_argument("--pace", type=_pace_arg, default=DEFAULT_PACE,
                    help=f"minimum seconds between sweep commands, at least {MIN_PACE:g} "
                         f"(default {DEFAULT_PACE:g})")
    ap.add_argument("--slot-release-wait", type=_non_negative_arg,
                    default=DEFAULT_SLOT_RELEASE_WAIT,
                    help="seconds to wait between sessions so the robot frees its connection "
                         f"slots (default {DEFAULT_SLOT_RELEASE_WAIT:g})")
    ap.add_argument("--extended", action="store_true",
                    help="also send GET Z BACKLASH and SEND TCP POS, but only if the robot "
                         "reports firmware newer than 01.02.00 (01.02.00 does not have them). "
                         "NOT verified read-only: no newer firmware image survives")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    _out("Goliath READ-ONLY probe. Sends only: " + ", ".join(SWEEP_ORDER)
         + (" (+ GET Z BACKLASH, SEND TCP POS if the firmware is newer than 01.02.00)"
            if args.extended else ""))
    _out("It stops, sending nothing more, if the robot reports firmware older than 01.02.00 "
         "or an identity it cannot read.")
    _out("Nothing else connected to the robot (Slingshot, phone apps)? Bit removed and "
         "robot on blocks? Ctrl+C now if not.")
    _out()
    report, redactor = _run_probe(
        args.host, args.port, args.out,
        terminator=args.terminator, listen=args.listen, reply_timeout=args.reply_timeout,
        pace=args.pace, connect_timeout=DEFAULT_CONNECT_TIMEOUT,
        slot_release_wait=args.slot_release_wait, extended=args.extended,
        connect_gaps=DEFAULT_CONNECT_GAPS, settle=DEFAULT_SETTLE, opener=_default_opener, stamp=None,
    )
    _print_summary(report, redactor)
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
