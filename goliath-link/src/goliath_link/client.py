# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""High-level Goliath client: send commands, follow the robot, upload, stop.

This sits on :class:`~goliath_link.transport.GoliathTransport` (three
connections; the robot routes its output by connection slot) and
:mod:`~goliath_link.protocol`. The robot runs its own state machine
(``docs/protocol/03-session-sequence.md``): the client sends configuration and
actions and waits for the ``INFO`` the robot reports. Firmware facts from the
disassembly of GFW 01.02.00 win where they disagree with the spec.

Owner rules this module follows: work correctly with real data; otherwise fall
back visibly (a log line, a field in a returned report, an exception); never
degrade silently and never present an unconfirmed outcome as a success.

Events: one bus, many waiters
=============================
The transport has one queue. The client drains it on a background thread
(the *event pump*) and fans every record out to **subscriptions**
(:meth:`GoliathClient.subscribe`). Each waiter has its own subscription, so
waiters never steal events from each other, and every subscription receives
the link's :class:`~goliath_link.transport.TransportClosed`. Once the link is
down, a new subscription receives that record at once (the closed state is
sticky until the transport reconnects).

A subscription only receives records whose receive time (``LinkEvent.t``,
``time.monotonic()``) is at or after its ``since`` time (default: when it was
created). So an event that arrived before a wait began can never satisfy it:
subscribe *before* sending the command whose reply you want, or pass
``since=`` the timestamp :meth:`GoliathClient.send` returned. Records are also
kept in a bounded history (:meth:`GoliathClient.history`), and ``since`` in
the past replays it. :meth:`GoliathClient.request` (and the stop and upload
sequences) subscribe only once the command is about to go out: after the
pacing wait and after the transport has finished any reply still pending on
connection 0. Replies carry no command name, so a reply to the previous
command that arrived during that wait must not count for this one.

Once a client is attached, read events through it, not through
``transport.records``/``transport.incoming`` (the pump consumes that queue).
A transport takes one client at a time: a second live client on the same
transport raises :class:`~goliath_link.transport.RecordsInUseError` (each
record can reach only one reader). Close a client you no longer need; one
that is simply dropped stops its pump once it is garbage-collected.

The pump also watches everything for the operator, whether or not anyone is
waiting: safety errors are logged at ERROR, other ``ERR``/``WARN`` lines,
``INFO;41`` (resume prompt) and framer discards at WARNING, and a message
arriving on a connection other than the one GFW 01.02.00 routes it to is
reported once per kind (:attr:`GoliathClient.routing_anomalies`).

Sending
=======
Every command goes out under the transport's ``send_lock``, paced so that at
least ``command_interval`` (default 0.3 s, must be > 0) separates it from the
transport's previous send, whoever made it
(:attr:`~goliath_link.transport.GoliathTransport.last_send_at`): the robot
parses one received buffer as one command, so two commands must never
coalesce. A G-code upload holds the same lock from its first command until
the robot signals the end of upload mode (``INFO;0``, ``INFO;8`` or work
starting), so nothing -- no keep-alive from another thread, not even a raw
``transport.send`` -- can land inside the program (the firmware stores every
buffer it receives as program data while an upload is in progress). Other
senders wait until then; the rest of the confirmation wait does not hold the
lock, so a stop can go out while the job runs. A transport listener waits
for the lock at most ``listener_send_wait`` (1 s); then its send raises
:class:`~goliath_link.transport.SendBlockedError` instead of freezing event
delivery.

Command safety layer
====================
Before the firmware gate, every text command the client sends (``send``,
``request``, the convenience methods, ``stop_work`` and the commands of
``upload_gcode``) passes three checks, in this order (see
:mod:`goliath_link.policy`):

1. the hard denylist :data:`~goliath_link.policy.NEVER_SEND`, always on:
   spec 09 sections 2a (except ``INFO``), 2b and 3, and ``LOAD``. Every
   spelling counts (case, whitespace, terminators, a second command after a
   line break or NUL), and it is checked once more, on the exact bytes, just
   before they go out;
2. with ``strict_commands=True``, the strict set
   :data:`~goliath_link.policy.STRICT_COMMANDS` (spec 09 section 1 plus
   ``INFO``);
3. the caller's :class:`~goliath_link.policy.CommandPolicy`
   (``command_policy=``, :meth:`GoliathClient.set_command_policy`), which a
   runner swaps per phase and which can bind arguments
   (:class:`~goliath_link.policy.PhasePolicy`, :func:`~goliath_link.policy.bind`).

A refusal raises :class:`~goliath_link.policy.CommandRefusedError` (its
``layer`` names the check) and nothing reaches the wire. ``upload_gcode``
checks every command it would send before it sends the first, so a refused
upload sends nothing and sets no hazard. A command that waited for the send
lock while the policy was swapped is checked again under the new policy. A
swap waits for any send in progress (it takes the send lock), and is refused
with :class:`~goliath_link.policy.PolicySwapRefusedError` while an upload is in
progress (from the start of ``upload_gcode`` until it returns or raises).
The layer does not cover the program bytes of an upload or a raw
``transport.send``.

Firmware gates
==============
:class:`FirmwareCapabilities` turns the firmware version (read with
:meth:`GoliathClient.learn_firmware_version`) into the vendor app's gates.
An unknown version gets the most conservative answers (the GFW 01.02.00 command
set). Every decision is a :class:`GateDecision` with its reason; the client
logs the ones it applies and records them in the reports it returns.
:meth:`GoliathClient.send` refuses commands the firmware is not known to
have (``ABORT WORK`` and the other nine commands GFW 01.02.00 lacks) with
:class:`CommandNotSupportedError`.

Stopping a job
==============
:meth:`GoliathClient.stop_work` sends ``ABORT WORK`` only on firmware newer
than 01.02.00 and then waits for ``INFO;18`` (work cycle exited) or
``INFO;23`` (work complete); without one it raises
:class:`StopNotConfirmedError`. On GFW 01.02.00 or an unknown version it sends
nothing and raises :class:`StopNotSupportedError`: that firmware has no
software stop, and the only way to stop a job is the robot's physical stop.

Uploading
=========
:meth:`GoliathClient.upload_gcode` refuses an empty program, cleans the text
exactly like the vendor (:func:`~goliath_link.gcode.clean_gcode`), subscribes,
then sends ``GCODE CONTINUE;0`` (only where the firmware is known to have it),
``GCODE UPLOAD;<size>``, and the bytes in blocks. Only ``ERR;0`` means the
upload failed; other errors and warnings are collected in the returned
:class:`UploadReport`; safety errors always raise. Success needs ``INFO;8``
received after the last block. Firmware below 01.02.03 may never send it
(GFW 01.02.00 ends an upload with ``INFO;0``), so there a missing ``INFO;8``
raises :class:`UploadUnconfirmedError`, explaining why; pass
``confirm="info8-or-info0"`` to accept the end-of-upload ``INFO;0`` instead,
reported as such. After an upload that may have left the robot in upload mode
(no end-of-upload signal), the client refuses to send anything
(:class:`RobotStateUncertainError`) until :meth:`GoliathClient.note_robot_restarted`
or :meth:`GoliathClient.clear_upload_hazard`.

Timing constants default to the vendor's values; they are starting points to
confirm on hardware.
"""

from __future__ import annotations

import inspect
import logging
import math
import queue
import re
import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Iterable, List, Optional, Sequence, Tuple, Union

from . import protocol
from .framing import Discarded
from .gcode import check_cleaned, clean_gcode, has_program_content
from .policy import (
    VENDOR_SENT_NOT_ON_GFW_01_02_00,
    CommandPolicy,
    CommandRefusedError,
    PolicyDecision,
    PolicySwapRefusedError,
    denylist_decision,
    redact,
    strict_decision,
)
from .protocol import Commands, Error, Info, PositionFrame, TextMessage, TowerFrame, Warning
from .transport import GoliathTransport, LinkEvent, SendBlockedError, TransportClosed

log = logging.getLogger(__name__)

Version = Tuple[int, int, int]

#: Default G-code upload block size (bytes). The vendor reads this from
#: ``Slingshot.ini`` (``GCODE_PACK_SIZE``); 1024 is the shipped 1.9.x value.
DEFAULT_BLOCK_SIZE = 1024
#: Default pause between upload blocks (seconds). The vendor used ~50 ms plus
#: the configurable GCODE_PACK_INTERVAL; 0.075 s is a safe starting point.
DEFAULT_BLOCK_INTERVAL = 0.075
#: Minimum gap between text commands (seconds). The robot parses each received
#: buffer as one command, so commands must not coalesce; the vendor left
#: 250-500 ms between commands.
DEFAULT_COMMAND_INTERVAL = 0.3
#: Vendor waits after ``GCODE CONTINUE;0`` and after the upload header.
DEFAULT_CONTINUE_WAIT = 0.25
DEFAULT_HEADER_WAIT = 0.25
#: How long ``header_ack="info7"`` waits for ``INFO;7``.
DEFAULT_HEADER_ACK_TIMEOUT = 2.0
#: How long to wait for the upload confirmation after the last block.
DEFAULT_DONE_TIMEOUT = 120.0
#: On firmware that may not send ``INFO;8`` (below 01.02.03): how long to keep
#: waiting for it once the robot has signalled the end of the upload (``INFO;0``
#: or work starting). The vendor waited 1 s and then closed its dialog.
DEFAULT_INFO8_GRACE = 2.0
#: How long :meth:`GoliathClient.stop_work` waits for a confirming INFO.
DEFAULT_STOP_TIMEOUT = 10.0
#: Records kept in the client's history.
DEFAULT_HISTORY = 10000
#: How long :meth:`GoliathClient.set_command_policy` waits for a send in
#: progress (it takes the send lock so that no command checked under the old
#: policy goes out after the swap).
DEFAULT_POLICY_SWAP_TIMEOUT = 5.0
#: INFO codes that confirm a stop: EXIT_WORK_CYCLE (18) and WORK_COMPLETE (23).
STOP_CONFIRM_CODES = (int(Info.EXIT_WORK_CYCLE), int(Info.WORK_COMPLETE))

#: ``header_ack`` modes of :meth:`GoliathClient.upload_gcode`.
HEADER_ACK_MODES = ("observe", "info7")
#: ``confirm`` modes of :meth:`GoliathClient.upload_gcode`.
CONFIRM_MODES = ("info8", "info8-or-info0")

GFW_01_02_00: Version = (1, 2, 0)
_INFO8_FROM: Version = (1, 2, 3)
_Z_PLATE_FROM: Version = (1, 2, 2)

#: Commands the vendor app knows that the GFW 01.02.00 command table lacks
#: (spec 09, "Vendor-app commands that are not in the table"). They presumably
#: exist on newer firmware; no image of it survives, so their semantics there
#: are unverified.
NOT_ON_GFW_01_02_00 = VENDOR_SENT_NOT_ON_GFW_01_02_00
_Z_PLATE_COMMANDS = frozenset({"GET Z PLATE OFFSET", "SET Z PLATE OFFSET"})

#: Where GFW 01.02.00 sends each kind of message (connection index = slot).
_EXPECTED_CONN = {"INFO/ERR/WARN lines": 1, "position frames": 2, "tower frames": 0}
_WORK_START_CODES = (int(Info.ENTER_MODE_WORK), int(Info.ENTER_WORK_CYCLE))


# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #


def parse_firmware_version(text: str) -> Optional[Version]:
    """Extract an ``xx.yy.zz`` firmware version from a string like ``GFW 01.02.00``."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def format_version(version: Optional[Sequence[int]]) -> str:
    """``(1, 2, 0)`` -> ``"01.02.00"``; None -> ``"unknown"``."""
    if version is None:
        return "unknown"
    return ".".join("%02d" % int(p) for p in version)


def _as_version(version: Optional[Sequence[int]]) -> Optional[Version]:
    if version is None:
        return None
    v = tuple(int(p) for p in version)
    if len(v) != 3:
        raise ValueError(f"a firmware version has three parts, e.g. (1, 2, 0); got {version!r}")
    return v  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def _error_name(code: Optional[int]) -> str:
    try:
        return Error(code).name  # type: ignore[arg-type]
    except ValueError:
        return "unknown code"


class GoliathError(RuntimeError):
    """The robot reported an ``ERR;<code>`` relevant to the current operation.

    ``event`` is the :class:`~goliath_link.protocol.TextMessage`, ``stage`` the
    operation stage it arrived in (if any) and ``report`` the operation's
    report so far (an :class:`UploadReport` or :class:`StopReport`, if any).
    """

    def __init__(self, code: int, message: str = "", *, event: Optional[TextMessage] = None,
                 stage: Optional[str] = None, report: object = None) -> None:
        super().__init__(message or f"robot error ERR;{code} ({_error_name(code)})")
        self.code = code
        self.event = event
        self.stage = stage
        self.report = report


class GoliathSafetyError(GoliathError):
    """A safety-critical ``ERR`` (see :data:`goliath_link.protocol.SAFETY_ERRORS`).

    Always raised, whatever ``raise_on_error`` says.
    """


class GoliathTimeout(TimeoutError):
    """Expected a response (e.g. a specific INFO) that did not arrive in time."""

    def __init__(self, message: str = "", report: object = None) -> None:
        super().__init__(message)
        self.report = report


class EventBusError(RuntimeError):
    """The client's event pump stopped (client closed, or a bug in the pump)."""


class CommandNotSupportedError(RuntimeError):
    """The firmware is not known to have this command. ``decision`` says why."""

    def __init__(self, message: str, decision: "GateDecision") -> None:
        super().__init__(message)
        self.decision = decision


class StopNotSupportedError(CommandNotSupportedError):
    """No software stop on this firmware (or the version is unknown): use the
    robot's physical stop."""


class StopNotConfirmedError(GoliathTimeout):
    """``ABORT WORK`` was sent but nothing confirmed the job stopped."""


class StopRejectedError(GoliathError):
    """The robot answered ``ABORT WORK`` with ``ERR;1`` (command not executed)."""


class StopBlockedError(RuntimeError):
    """``ABORT WORK`` could not be sent in time: another thread holds the send
    lock (normally a G-code upload, during which the robot stores every byte
    as program data). Nothing was sent."""


class UploadFailedError(GoliathError):
    """The robot rejected the upload (``ERR;0``, or an error reply to the header)."""


class UploadHeaderNotAcknowledgedError(GoliathTimeout):
    """``header_ack="info7"`` was asked for and no ``INFO;7`` came; nothing was streamed."""


class UploadUnconfirmedError(GoliathTimeout):
    """The upload was streamed but no accepted confirmation arrived.

    ``firmware_confirms`` is True when the firmware is expected to send
    ``INFO;8`` (01.02.03 or later), None when it may not (older or unknown).
    ``report`` (an :class:`UploadReport`) says what did arrive: the
    end-of-upload ``INFO;0``, work starting, errors.
    """

    def __init__(self, message: str = "", report: object = None,
                 firmware_confirms: Optional[bool] = None) -> None:
        super().__init__(message, report)
        self.firmware_confirms = firmware_confirms


class RobotStateUncertainError(RuntimeError):
    """An earlier upload may have left the robot in upload mode; nothing is sent."""


class RestartRequiredError(RuntimeError):
    """Second upload without a robot restart on firmware <= 01.02.00 (vendor gate)."""


class EmptyProgramError(ValueError):
    """The program has no G-code (empty, or only comments and blank lines)."""


def is_safety_error(event: object) -> bool:
    """True if ``event`` is an ``ERR`` the controller should treat as critical."""
    return (
        isinstance(event, TextMessage)
        and event.kind == "ERR"
        and event.code is not None
        and event.code in {int(e) for e in protocol.SAFETY_ERRORS}
    )


def _err_error(ev: TextMessage, *, context: str = "", stage: Optional[str] = None,
               report: object = None, cls: Optional[type] = None) -> GoliathError:
    safety = is_safety_error(ev)
    kind = cls or (GoliathSafetyError if safety else GoliathError)
    label = "SAFETY error" if safety else "error"
    msg = f"robot {label} {ev.raw} ({_error_name(ev.code)})"
    if context:
        msg += f" {context}"
    return kind(ev.code if ev.code is not None else -1, msg, event=ev, stage=stage, report=report)


def describe_record(rec: LinkEvent) -> str:
    """A short human description of a record, e.g. ``INFO;8 on connection 1``."""
    ev = rec.event
    if isinstance(ev, TextMessage):
        what = ev.raw
    elif isinstance(ev, TransportClosed):
        what = f"TransportClosed({ev.error!r})"
    else:
        what = type(ev).__name__
    return what if rec.conn is None else f"{what} on connection {rec.conn}"


# --------------------------------------------------------------------------- #
# Firmware capabilities
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GateDecision:
    """One firmware-gate decision: ``allowed`` is True, False or None (uncertain)."""

    feature: str
    allowed: Optional[bool]
    reason: str

    def __str__(self) -> str:
        state = {True: "yes", False: "NO", None: "uncertain"}[self.allowed]
        return f"{self.feature}: {state} -- {self.reason}"


@dataclass(frozen=True)
class FirmwareCapabilities:
    """What the client may assume about a firmware version (the vendor's gates).

    Built with :meth:`for_version`. ``version`` None means unknown, which gets
    the most conservative answer everywhere (the GFW 01.02.00 command set).

    * ``has_abort_work`` -- ``ABORT WORK`` (software stop): firmware > 01.02.00.
    * ``has_gcode_continue`` -- ``GCODE CONTINUE``: firmware > 01.02.00. The
      vendor sent ``GCODE CONTINUE;0`` on >= 01.02.00, but GFW 01.02.00 has no
      such command, so it is sent only where it is presumed to exist.
    * ``confirms_upload_with_info8`` -- True on >= 01.02.03 (the vendor waits
      for ``INFO;8``); None (uncertain) below, where the vendor did not wait.
    * ``requires_restart_between_jobs`` -- True on <= 01.02.00 or unknown: the
      vendor refused a second upload until the robot was restarted.
    * ``has_z_plate_offset`` -- ``GET/SET Z PLATE OFFSET``: >= 01.02.02.
    * ``has_newer_commands`` -- the other commands GFW 01.02.00 lacks
      (``SEND TCP POS``, ``GET/SET Z BACKLASH``, ``SET SPINDLE DISTANCE``,
      ``DISCARD TWRS DISTANCE``, ``CMD SYS RESET``): > 01.02.00.
    * ``decisions`` -- a :class:`GateDecision` per feature, with the reason.
    """

    version: Optional[Version]
    has_abort_work: bool
    has_gcode_continue: bool
    confirms_upload_with_info8: Optional[bool]
    requires_restart_between_jobs: bool
    has_z_plate_offset: bool
    has_newer_commands: bool
    decisions: Tuple[GateDecision, ...] = ()

    @classmethod
    def for_version(cls, version: Optional[Sequence[int]]) -> "FirmwareCapabilities":
        v = _as_version(version)
        vs = format_version(v)
        known = v is not None
        newer = known and v > GFW_01_02_00  # type: ignore[operator]
        unknown_note = (
            "firmware version unknown (call learn_firmware_version()): assuming the "
            "smallest known command set, GFW 01.02.00"
        )

        if newer:
            abort_reason = (
                f"firmware {vs} > 01.02.00: the vendor app enabled its Abort button there "
                "(no image of such firmware survives: what acknowledges ABORT WORK is unverified)"
            )
        else:
            abort_reason = (
                (unknown_note + "; " if not known else f"firmware {vs} <= 01.02.00: ")
                + "GFW 01.02.00 has no ABORT WORK command (the vendor disabled its Abort "
                "button: 'Abort functionality is not available on current firmware "
                "version'); stop a running job with the robot's physical stop"
            )
        if newer:
            cont_reason = (
                f"firmware {vs} > 01.02.00: the vendor app sends GCODE CONTINUE;0 before "
                "each upload (presumed present; unverified)"
            )
        else:
            cont_reason = (
                (unknown_note + "; " if not known else f"firmware {vs}: ")
                + "GCODE CONTINUE is not in the GFW 01.02.00 command table (the vendor's "
                "pre-step was a no-op there); not sent"
            )
        info8: Optional[bool] = True if (known and v >= _INFO8_FROM) else None  # type: ignore[operator]
        if info8:
            info8_reason = (
                f"firmware {vs} >= 01.02.03: the vendor app waits for INFO;8 after an upload"
            )
        else:
            info8_reason = (
                (unknown_note + "; " if not known else f"firmware {vs} < 01.02.03: ")
                + "the vendor app did not wait for INFO;8 there (it slept 1 s); GFW 01.02.00 "
                "ends an upload with INFO;0 and may not send INFO;8 at all"
            )
        restart = not newer
        if restart:
            restart_reason = (
                (unknown_note + "; " if not known else f"firmware {vs} <= 01.02.00: ")
                + "the vendor app refused a second upload until the robot was restarted "
                "('Please restart Goliath before starting another job.'); whether the "
                "firmware needs it is unverified, and a restart means a power cycle"
            )
        else:
            restart_reason = f"firmware {vs} > 01.02.00: the vendor app allowed another upload"
        zplate = known and v >= _Z_PLATE_FROM  # type: ignore[operator]
        zplate_reason = (
            f"firmware {vs} >= 01.02.02: the vendor app ran the tool-change Z-plate dialog"
            if zplate else
            (unknown_note + "; " if not known else f"firmware {vs} < 01.02.02: ")
            + "GET/SET Z PLATE OFFSET are absent from GFW 01.02.00 and the vendor used "
            "them only on >= 01.02.02"
        )
        newer_reason = (
            f"firmware {vs} > 01.02.00: presumed present (the vendor app sends them); "
            "absent from GFW 01.02.00, semantics on newer firmware unverified"
            if newer else
            (unknown_note + "; " if not known else f"firmware {vs}: ")
            + "absent from the GFW 01.02.00 command table"
        )
        decisions = (
            GateDecision("ABORT WORK (software stop)", newer, abort_reason),
            GateDecision("GCODE CONTINUE;0 before an upload", newer, cont_reason),
            GateDecision("INFO;8 upload confirmation", info8, info8_reason),
            GateDecision("restart required before a second upload", restart, restart_reason),
            GateDecision("GET/SET Z PLATE OFFSET", zplate, zplate_reason),
            GateDecision(
                "SEND TCP POS, GET/SET Z BACKLASH, SET SPINDLE DISTANCE, "
                "DISCARD TWRS DISTANCE, CMD SYS RESET",
                newer,
                newer_reason,
            ),
        )
        return cls(
            version=v,
            has_abort_work=newer,
            has_gcode_continue=newer,
            confirms_upload_with_info8=info8,
            requires_restart_between_jobs=restart,
            has_z_plate_offset=zplate,
            has_newer_commands=newer,
            decisions=decisions,
        )

    def _decision(self, prefix: str) -> GateDecision:
        return next(d for d in self.decisions if d.feature.startswith(prefix))

    def supports_command(self, command: str) -> GateDecision:
        """Whether ``command`` may be sent to this firmware, and why."""
        name = command.split(";", 1)[0].strip().upper()
        if name == "ABORT WORK":
            d = self._decision("ABORT WORK")
        elif name == "GCODE CONTINUE":
            d = self._decision("GCODE CONTINUE")
        elif name in _Z_PLATE_COMMANDS:
            d = self._decision("GET/SET Z PLATE")
        elif name in NOT_ON_GFW_01_02_00:
            d = self._decision("SEND TCP POS")
        else:
            return GateDecision(name, True, "not version-gated")
        return GateDecision(name, d.allowed, d.reason)

    def explain(self) -> List[str]:
        """One line per gate decision, headed by the version."""
        return [f"firmware version: {format_version(self.version)}"] + [
            str(d) for d in self.decisions
        ]


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Observed:
    """A record seen during an operation, with the operation stage it belongs to."""

    stage: str
    record: LinkEvent

    def __str__(self) -> str:
        return f"[{self.stage}] {describe_record(self.record)}"


@dataclass
class UploadReport:
    """What happened during :meth:`GoliathClient.upload_gcode`.

    Times are ``time.monotonic()``; ``started_at`` can be passed as ``since=``
    to wait for later events (e.g. ``INFO;23``) without missing any.
    """

    size: int
    blocks_total: int
    firmware_version: Optional[Version]
    capabilities: FirmwareCapabilities
    confirm_mode: str
    header_ack_mode: str
    started_at: float
    decisions: List[str] = field(default_factory=list)
    continue_sent: bool = False
    header_sent_at: Optional[float] = None
    first_block_at: Optional[float] = None
    last_block_at: Optional[float] = None
    blocks_sent: int = 0
    header_ack: Optional[TextMessage] = None
    end_of_upload_ack: Optional[LinkEvent] = None
    confirmation: Optional[LinkEvent] = None
    confirmed_by: Optional[str] = None
    errors: List[Observed] = field(default_factory=list)
    warnings: List[Observed] = field(default_factory=list)
    work_started: List[Observed] = field(default_factory=list)
    ignored: List[str] = field(default_factory=list)
    events: List[Observed] = field(default_factory=list)
    finished_at: Optional[float] = None

    @property
    def confirmed(self) -> bool:
        return self.confirmed_by is not None

    def note(self, text: str) -> None:
        self.decisions.append(text)
        log.info("upload: %s", text)


@dataclass
class StopReport:
    """What happened during :meth:`GoliathClient.stop_work`."""

    firmware_version: Optional[Version]
    decision: GateDecision
    sent_at: float
    confirm_codes: Tuple[int, ...]
    acknowledged: bool = False
    confirmed_by: Optional[TextMessage] = None
    confirmation: Optional[LinkEvent] = None
    errors: List[Observed] = field(default_factory=list)
    warnings: List[Observed] = field(default_factory=list)
    events: List[Observed] = field(default_factory=list)

    @property
    def confirmed(self) -> bool:
        return self.confirmed_by is not None


# --------------------------------------------------------------------------- #
# Event bus
# --------------------------------------------------------------------------- #


class _BusStopped:
    """Queued into subscriptions when the pump stops; get() raises on it."""

    def __init__(self, reason: str, error: Optional[BaseException] = None) -> None:
        self.reason = reason
        self.error = error


class _SyncMarker:
    def __init__(self) -> None:
        self.done = threading.Event()


class Subscription:
    """One waiter's view of the event stream. Use as a context manager.

    Created by :meth:`GoliathClient.subscribe`. Receives every record with
    ``t >= since`` delivered after (or, for a past ``since``, replayed at)
    its creation, plus the session's ``TransportClosed``.
    """

    def __init__(self, bus: "EventBus", since: float, session: object) -> None:
        self._bus = bus
        self.since = since
        self._session = session
        self._q: "queue.Queue[LinkEvent]" = queue.Queue()
        self.closed = False

    def get(self, timeout: Optional[float] = None) -> LinkEvent:
        """Next record; raises ``queue.Empty`` on timeout, :class:`EventBusError`
        if the client's event pump stopped."""
        rec = self._q.get(True, timeout)
        if isinstance(rec.event, _BusStopped):
            self._q.put(rec)  # keep raising for later calls
            raise EventBusError(rec.event.reason) from rec.event.error
        return rec

    def get_nowait(self) -> LinkEvent:
        """Next record without blocking; raises ``queue.Empty`` if there is none."""
        return self._get_nowait()

    def _get_nowait(self) -> LinkEvent:
        rec = self._q.get_nowait()
        if isinstance(rec.event, _BusStopped):
            self._q.put(rec)
            raise EventBusError(rec.event.reason) from rec.event.error
        return rec

    def pending(self) -> List[LinkEvent]:
        """Every record already delivered and not yet read (non-blocking)."""
        out: List[LinkEvent] = []
        while True:
            try:
                out.append(self._get_nowait())
            except queue.Empty:
                return out

    def wait_for_record(
        self,
        predicate: Callable[[object], bool],
        *,
        timeout: float = 30.0,
        raise_on_error: bool = True,
        describe: Optional[str] = None,
        slot_hint: bool = False,
    ) -> LinkEvent:
        """Like :meth:`wait_for` but returns the :class:`LinkEvent` record."""
        what = describe or "the expected message"
        deadline = time.monotonic() + timeout
        seen: List[LinkEvent] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GoliathTimeout(_timeout_message(what, timeout, seen, slot_hint))
            try:
                rec = self.get(timeout=remaining)
            except queue.Empty:
                continue
            ev = rec.event
            if isinstance(ev, TransportClosed):
                raise _closed_error(ev, f"while waiting for {what}")
            if predicate(ev):
                return rec
            seen.append(rec)
            if isinstance(ev, TextMessage) and ev.kind == "ERR" and ev.code is not None:
                if is_safety_error(ev) or raise_on_error:
                    raise _err_error(ev, context=f"while waiting for {what}")

    def wait_for(
        self,
        predicate: Callable[[object], bool],
        *,
        timeout: float = 30.0,
        raise_on_error: bool = True,
        describe: Optional[str] = None,
        slot_hint: bool = False,
    ) -> object:
        """Block until a delivered event satisfies ``predicate``; return the event.

        Raises :class:`GoliathTimeout` on timeout, :class:`ConnectionError` if
        the link goes down, :class:`GoliathSafetyError` on a safety ``ERR``
        (always), and :class:`GoliathError` on any other ``ERR`` when
        ``raise_on_error`` (non-safety errors are skipped otherwise; the client
        logs every one).
        """
        return self.wait_for_record(
            predicate, timeout=timeout, raise_on_error=raise_on_error, describe=describe,
            slot_hint=slot_hint,
        ).event

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._bus._unsubscribe(self)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _timeout_message(what: str, timeout: float, seen: Sequence[LinkEvent], slot_hint: bool) -> str:
    msg = f"timed out after {timeout:g} s waiting for {what}"
    if seen:
        shown = ", ".join(describe_record(r) for r in seen[:5])
        more = f" (+{len(seen) - 5} more)" if len(seen) > 5 else ""
        msg += f"; other records that arrived meanwhile: {shown}{more}"
    else:
        msg += "; nothing else arrived meanwhile"
    if slot_hint and not any(r.conn == 1 for r in seen):
        msg += (
            ". Nothing arrived on connection 1 (firmware slot 1), where GFW 01.02.00 sends "
            "every INFO/ERR/WARN: if another client (an old Slingshot, a phone) holds that "
            "slot, the robot's replies go to it instead; this does not mean the robot is "
            "silent or that the command failed"
        )
    return msg


def _closed_error(ev: TransportClosed, context: str) -> ConnectionError:
    if ev.error is None:
        err = ConnectionError(f"the link was closed locally {context}")
    else:
        where = "" if ev.conn is None else f" (connection {ev.conn})"
        err = ConnectionError(f"the link went down{where} {context}: {ev.error!r}")
    err.__cause__ = ev.error
    return err


def _pump_main(ref: "weakref.ReferenceType[EventBus]", transport: GoliathTransport,
               stop: threading.Event, poll: float) -> None:
    """The event pump. Holds only a weak reference to the bus between records,
    so a client that is dropped without close() does not keep its pump alive."""
    cur = None
    try:
        while not stop.is_set():
            if cur is None:
                cur = transport.records
            try:
                rec = cur.get(True, poll)
            except queue.Empty:
                latest = transport.records
                if latest is not cur:
                    # A new session started. The old queue's last record is its
                    # TransportClosed; deliver anything left before switching.
                    leftovers = []
                    while True:
                        try:
                            leftovers.append(cur.get_nowait())
                        except queue.Empty:
                            break
                    bus = ref()
                    if bus is None:
                        return
                    for r in leftovers:
                        bus._deliver(r, cur)
                    del bus
                    cur = None
                elif ref() is None:
                    return
                continue
            bus = ref()
            if bus is None:
                log.warning(
                    "goliath client: a client was dropped without close(); its event pump "
                    "took %r off the transport's queue and stopped, so that record is lost. "
                    "close() clients you no longer use.", rec)
                return
            bus._deliver(rec, cur)
            del bus
    except BaseException as exc:  # noqa: BLE001 - must reach every waiter
        bus = ref()
        if bus is not None:
            bus._pump_failed(exc)
        else:
            log.error("goliath client event pump failed", exc_info=exc)


class EventBus:
    """Fans the transport's records out to subscriptions; keeps a history.

    Owned by a :class:`GoliathClient` (``client.event_bus``). ``observer`` is
    called on the pump thread for every record before it is delivered.
    """

    def __init__(self, transport: GoliathTransport, *, history_size: int = DEFAULT_HISTORY,
                 observer: Optional[Callable[[LinkEvent], None]] = None,
                 poll_interval: float = 0.05) -> None:
        if int(history_size) < 1:
            raise ValueError("history_size must be >= 1")
        self._t = transport
        # A bound method (the client's monitor) is held weakly: a strong one
        # would make client -> bus -> observer -> client a reference cycle, and
        # a client dropped without close() would keep its pump consuming the
        # transport until the cyclic garbage collector ran.
        self._observer: Optional[Callable[[LinkEvent], None]] = None
        self._observer_ref: Optional["weakref.WeakMethod"] = None
        if observer is not None and inspect.ismethod(observer):
            self._observer_ref = weakref.WeakMethod(observer)
        else:
            self._observer = observer
        self._observer_gone_logged = False
        self._poll = float(poll_interval)
        self._lock = threading.Lock()
        self._subs: List[Subscription] = []
        self._history: Deque[Tuple[int, LinkEvent, object]] = deque(maxlen=int(history_size))
        self._seq = 0
        self._drain_seq = 0
        self._evicted_t: Optional[float] = None
        self._evicted_seq = 0
        self._closed_rec: Optional[LinkEvent] = None
        self._closed_q: object = None
        self._failed: Optional[BaseException] = None
        self._stopped = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.observer_errors: List[BaseException] = []

    @property
    def running(self) -> bool:
        th = self._thread
        return th is not None and th.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._stopped:
                raise EventBusError("the client was closed")
            if self._thread is not None and self._thread.is_alive():
                return
            th = threading.Thread(
                target=_pump_main,
                args=(weakref.ref(self), self._t, self._stop, self._poll),
                name="goliath-client-pump",
                daemon=True,
            )
        # One consumer per transport (raises RecordsInUseError otherwise).
        claim = getattr(self._t, "_claim_records", None)
        if claim is not None:
            claim(self, th)
        with self._lock:
            self._thread = th
        th.start()

    def close(self, timeout: float = 2.0) -> None:
        """Stop the pump and wake every subscription with :class:`EventBusError`."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            subs = list(self._subs)
        self._stop.set()
        th = self._thread
        if th is not None and th is not threading.current_thread():
            th.join(timeout)
            if th.is_alive():
                log.warning("goliath client event pump did not stop within %.1f s", timeout)
        release = getattr(self._t, "_release_records", None)
        if release is not None:
            release(self)
        marker = LinkEvent(None, time.monotonic(), _BusStopped("the client was closed"))
        for sub in subs:
            sub._q.put(marker)

    # -- subscriptions --------------------------------------------------------

    def subscribe(self, since: Optional[float] = None) -> Subscription:
        with self._lock:
            if self._stopped:
                raise EventBusError("the client was closed")
            if self._failed is not None:
                raise EventBusError(
                    f"the client's event pump failed: {self._failed!r}"
                ) from self._failed
            since_v = time.monotonic() if since is None else float(since)
            if since is not None and self._evicted_t is not None and since_v <= self._evicted_t:
                raise ValueError(
                    f"the history no longer covers since={since_v:.3f}: records up to "
                    f"t={self._evicted_t:.3f} were dropped (history_size "
                    f"{self._history.maxlen})"
                )
            session = self._t.records
            sub = Subscription(self, since_v, session)
            replayed_closed = False
            if since is not None:
                for _, rec, q in self._history:
                    if isinstance(rec.event, TransportClosed) and q is not session:
                        continue  # a previous session's close is not this link's state
                    if rec.t >= since_v:
                        sub._q.put(rec)
                        replayed_closed = replayed_closed or rec is self._closed_rec
            if (self._closed_rec is not None and self._closed_q is session
                    and not replayed_closed):
                sub._q.put(self._closed_rec)  # the link is down: sticky
            self._subs.append(sub)
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:  # Subscription.close() calls this only once
                self._subs.remove(sub)

    # -- pump side --------------------------------------------------------------

    def _deliver(self, rec: LinkEvent, q: object) -> None:
        if isinstance(rec.event, _SyncMarker):
            rec.event.done.set()
            return
        observer = self._observer
        if observer is None and self._observer_ref is not None:
            observer = self._observer_ref()
            if observer is None and not self._observer_gone_logged:
                self._observer_gone_logged = True
                log.warning("goliath client: the client that owned this event bus is gone; "
                            "its monitoring (safety-error logging, routing checks) stopped")
        if observer is not None:
            try:
                observer(rec)
            except Exception as exc:  # noqa: BLE001 - logged and kept, delivery goes on
                self.observer_errors.append(exc)
                log.error("goliath client: event observer failed on %r", rec, exc_info=exc)
        with self._lock:
            self._seq += 1
            if len(self._history) == self._history.maxlen:
                old_seq, old, _ = self._history[0]
                self._evicted_t = old.t
                self._evicted_seq = old_seq
            self._history.append((self._seq, rec, q))
            closed = isinstance(rec.event, TransportClosed)
            if closed:
                self._closed_rec = rec
                self._closed_q = q
            for sub in self._subs:
                if rec.t >= sub.since or (closed and sub._session is q):
                    sub._q.put(rec)

    def _pump_failed(self, exc: BaseException) -> None:
        log.error("goliath client event pump failed; waiters will raise", exc_info=exc)
        with self._lock:
            self._failed = exc
            subs = list(self._subs)
        marker = LinkEvent(None, time.monotonic(),
                           _BusStopped(f"the client's event pump failed: {exc!r}", exc))
        for sub in subs:
            sub._q.put(marker)

    # -- history ------------------------------------------------------------------

    def history_since(self, since: Optional[float] = None) -> List[LinkEvent]:
        with self._lock:
            return [rec for _, rec, _ in self._history if since is None or rec.t >= since]

    def sync(self, timeout: float = 1.0) -> bool:
        """Wait until the pump has delivered everything queued so far."""
        if not self.running:
            return False
        marker = _SyncMarker()
        self._t.records.put(LinkEvent(None, time.monotonic(), marker))
        return marker.done.wait(timeout)

    def drain(self) -> List[LinkEvent]:
        """Records delivered since the previous drain() (from the history)."""
        if not self.sync():
            log.warning("goliath client: drain() could not confirm the pump is up to date")
        with self._lock:
            if self._evicted_seq > self._drain_seq:
                log.warning(
                    "goliath client: %d record(s) left the history before drain() read them",
                    self._evicted_seq - self._drain_seq,
                )
            out = [rec for seq, rec, _ in self._history if seq > self._drain_seq]
            self._drain_seq = self._seq
        return out


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #


def _is_info(code: int) -> Callable[[object], bool]:
    return lambda e: isinstance(e, TextMessage) and e.kind == "INFO" and e.code == int(code)


def _positive(name: str, value: float) -> float:
    v = float(value)
    if not (v > 0) or math.isinf(v):
        raise ValueError(f"{name} must be a finite number > 0, not {value!r}")
    return v


def _check_policy_object(policy: object) -> None:
    if policy is not None and not callable(getattr(policy, "check", None)):
        raise TypeError(f"a command policy needs a check(command) -> PolicyDecision method; "
                        f"got {policy!r}")


def _non_negative(name: str, value: float) -> float:
    v = float(value)
    if not (v >= 0) or math.isinf(v):
        raise ValueError(f"{name} must be a finite number >= 0, not {value!r}")
    return v


class GoliathClient:
    """Commands, waits, uploads and stops on top of a :class:`GoliathTransport`.

    One client per transport: constructing a second one while another is
    attached and running raises
    :class:`~goliath_link.transport.RecordsInUseError`.

    ``command_interval`` (seconds, > 0) is the minimum gap between commands
    (measured from the transport's last send, whoever made it).
    ``terminator`` is appended to every text command (the vendor sent none and
    GFW 01.02.00 needs none: a terminator makes the name unknown to it).
    ``firmware_version`` starts unknown; set it with
    :meth:`learn_firmware_version` (or assign it if you know it).

    ``strict_commands`` (a bool, fixed for the client's life) limits the client
    to spec 09 section 1 plus ``INFO``; ``command_policy`` is the first
    :class:`~goliath_link.policy.CommandPolicy` (None: no policy), replaced
    with :meth:`set_command_policy`. The hard denylist is always on. See
    "Command safety layer" in the module docstring.

    Call :meth:`close` (or use the client as a context manager) to stop its
    event pump; closing the client does not close the transport.
    """

    def __init__(
        self,
        transport: GoliathTransport,
        *,
        command_interval: float = DEFAULT_COMMAND_INTERVAL,
        terminator: str = "",
        history_size: int = DEFAULT_HISTORY,
        strict_commands: bool = False,
        command_policy: Optional[CommandPolicy] = None,
    ) -> None:
        if type(strict_commands) is not bool:
            raise TypeError(f"strict_commands must be True or False, not {strict_commands!r}")
        _check_policy_object(command_policy)
        self._strict = strict_commands
        self._policy_lock = threading.Lock()  # never held across I/O or a policy's check()
        self._policy: Optional[CommandPolicy] = command_policy
        self._policy_generation = 0
        self._uploads_in_progress = 0
        self.t = transport
        self._firmware_version: Optional[Version] = None
        self._firmware_source = "not learned yet"
        self.command_interval = command_interval
        self.terminator = terminator
        self._state_lock = threading.Lock()
        self._hazard: Optional[str] = None
        self._uploads_since_restart = 0
        #: The latest ``INFO;34`` identity line seen (pushed at connect or replied).
        self.identity: Optional[TextMessage] = None
        #: Messages that arrived on a connection GFW 01.02.00 does not route them to.
        self.routing_anomalies: List[str] = []
        self._routing_seen: set = set()
        self.event_bus = EventBus(transport, history_size=history_size, observer=self._monitor)
        self.event_bus.start()
        if strict_commands or command_policy is not None:
            log.info("command safety layer: hard denylist on; strict_commands=%s; policy %r",
                     strict_commands, command_policy)

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Stop the event pump (waiters raise :class:`EventBusError`)."""
        self.event_bus.close()

    def __enter__(self) -> "GoliathClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- state ------------------------------------------------------------------

    @property
    def command_interval(self) -> float:
        return self._command_interval

    @command_interval.setter
    def command_interval(self, value: float) -> None:
        self._command_interval = _positive("command_interval", value)

    @property
    def firmware_version(self) -> Optional[Version]:
        """The robot's firmware version, ``(1, 2, 0)`` style, or None (unknown).

        Set by :meth:`learn_firmware_version`; assign it only if you know it.
        Every firmware gate follows it.
        """
        return self._firmware_version

    @firmware_version.setter
    def firmware_version(self, value: Optional[Sequence[int]]) -> None:
        self._firmware_version = _as_version(value)
        self._firmware_source = "assigned by the caller" if value is not None else "cleared"

    @property
    def firmware_version_source(self) -> str:
        """Where :attr:`firmware_version` came from (shown in gate decisions)."""
        return self._firmware_source

    @property
    def capabilities(self) -> FirmwareCapabilities:
        """The gates for :attr:`firmware_version` (conservative while unknown)."""
        return FirmwareCapabilities.for_version(self.firmware_version)

    @property
    def upload_hazard(self) -> Optional[str]:
        """Why sends are refused after an abnormal upload, or None."""
        with self._state_lock:
            return self._hazard

    def note_robot_restarted(self) -> None:
        """Tell the client the robot was power-cycled: clears the upload hazard
        and the one-upload-per-power-cycle count."""
        with self._state_lock:
            self._hazard = None
            self._uploads_since_restart = 0
        log.info("robot restart noted: upload hazard and upload count cleared")

    @property
    def strict_commands(self) -> bool:
        """Whether only spec 09 section 1 plus ``INFO`` may be sent (fixed at construction)."""
        return self._strict

    @property
    def command_policy(self) -> Optional[CommandPolicy]:
        """The :class:`~goliath_link.policy.CommandPolicy` in force, or None."""
        with self._policy_lock:
            return self._policy

    @property
    def upload_in_progress(self) -> bool:
        """True from the start of :meth:`upload_gcode` until it returns or raises."""
        with self._policy_lock:
            return self._uploads_in_progress > 0

    def set_command_policy(self, policy: Optional[CommandPolicy], *,
                           timeout: float = DEFAULT_POLICY_SWAP_TIMEOUT) -> None:
        """Replace the command policy (None: no policy; the denylist stays on).

        The swap takes the transport's send lock, so a command checked under
        the old policy cannot go out after this returns; a command still
        waiting for the lock is checked again under the new one. Raises
        :class:`~goliath_link.policy.PolicySwapRefusedError`, leaving the
        policy unchanged, while an upload is in progress (the policy that
        authorised the upload stays in force until :meth:`upload_gcode`
        returns or raises) or if the send lock is not free within ``timeout``
        seconds.
        """
        _check_policy_object(policy)
        timeout = _positive("timeout", timeout)
        self._refuse_swap_during_upload(policy)
        lock = self.t.send_lock
        busy = (f"could not replace the command policy with {policy!r}: another thread held "
                f"the send lock for more than {timeout:g} s (an upload, or a send waiting for "
                "a reply); the policy is unchanged")
        if not lock.acquire(timeout=timeout):
            log.warning("%s", busy)
            raise PolicySwapRefusedError(busy)
        try:
            # The upload counter is checked again under the send lock: an
            # upload may have started while this call waited for it.
            self._refuse_swap_during_upload(policy)
            with self._policy_lock:
                old, self._policy = self._policy, policy
                self._policy_generation += 1
        finally:
            lock.release()
        log.info("command policy replaced: %r -> %r", old, policy)

    def _refuse_swap_during_upload(self, policy: Optional[CommandPolicy]) -> None:
        with self._policy_lock:
            current, uploading = self._policy, self._uploads_in_progress > 0
        if uploading:
            msg = (f"refusing to replace the command policy with {policy!r} while an upload is "
                   f"in progress: the policy that authorised it ({current!r}) stays in force "
                   "until upload_gcode() returns or raises")
            log.warning("%s", msg)
            raise PolicySwapRefusedError(msg)

    def clear_upload_hazard(self) -> None:
        """Allow sending again although an upload ended abnormally.

        Only do this if you know the robot is not in upload mode; otherwise
        whatever is sent next becomes part of the stored program.
        """
        with self._state_lock:
            hazard, self._hazard = self._hazard, None
        if hazard:
            log.warning("upload hazard cleared by the caller (was: %s)", hazard)

    # -- monitoring (pump thread) ------------------------------------------------

    def _routing(self, rec: LinkEvent, what: str) -> None:
        expected = _EXPECTED_CONN[what]
        if rec.conn is None or rec.conn == expected:
            return
        key = (what, rec.conn)
        if key in self._routing_seen:
            return
        self._routing_seen.add(key)
        msg = (
            f"{what} arrived on connection {rec.conn}, but GFW 01.02.00 sends them to "
            f"connection {expected} (firmware slot {expected}): the slot assignment is not "
            "the assumed one (another client holding slots, a reconnect before the robot "
            "freed its slots, or different firmware)"
        )
        self.routing_anomalies.append(msg)
        log.warning("%s", msg)

    def _monitor(self, rec: LinkEvent) -> None:
        ev = rec.event
        if isinstance(ev, TextMessage):
            if ev.kind in ("INFO", "ERR", "WARN"):
                self._routing(rec, "INFO/ERR/WARN lines")
            if ev.kind == "ERR":
                if is_safety_error(ev):
                    log.error("robot SAFETY error %s (%s) on connection %s",
                              ev.raw, _error_name(ev.code), rec.conn)
                else:
                    log.warning("robot error %s (%s) on connection %s",
                                ev.raw, _error_name(ev.code), rec.conn)
            elif ev.kind == "WARN":
                try:
                    name = Warning(ev.code).name  # type: ignore[arg-type]
                except ValueError:
                    name = "unknown code"
                log.warning("robot warning %s (%s) on connection %s", ev.raw, name, rec.conn)
            elif ev.kind == "INFO" and ev.code == int(Info.APP_INFO):
                self.identity = ev
                log.info("robot identity on connection %s: %s", rec.conn, ev.raw)
            elif ev.kind == "INFO" and ev.code == int(Info.ASK_GCODE_RESTART):
                log.warning(
                    "robot %s: it asks whether to resume the previous G-code or start a new "
                    "one (after a reset); the client does not answer this for you", ev.raw)
        elif isinstance(ev, PositionFrame):
            self._routing(rec, "position frames")
        elif isinstance(ev, TowerFrame):
            self._routing(rec, "tower frames")
        elif isinstance(ev, Discarded):
            log.warning("connection %s: the framer discarded %d byte(s): %s",
                        rec.conn, len(ev.raw), ev.reason)
        elif isinstance(ev, TransportClosed):
            if ev.error is None:
                log.info("goliath link closed locally")
            else:
                log.warning("goliath link down (connection %s): %r", ev.conn, ev.error)

    # -- events -------------------------------------------------------------------

    def subscribe(self, since: Optional[float] = None) -> Subscription:
        """A new :class:`Subscription` (records with ``t >= since``; default now)."""
        return self.event_bus.subscribe(since)

    def history(self, since: Optional[float] = None) -> List[LinkEvent]:
        """Records in the client's bounded history (optionally ``t >= since``)."""
        return self.event_bus.history_since(since)

    def drain(self) -> List[object]:
        """Events received since the previous ``drain()`` (or since the client started)."""
        return [rec.event for rec in self.event_bus.drain()]

    def wait_for(
        self,
        predicate: Callable[[object], bool],
        *,
        timeout: float = 30.0,
        raise_on_error: bool = True,
        since: Optional[float] = None,
        describe: Optional[str] = None,
    ) -> object:
        """Block until an event received at or after ``since`` satisfies ``predicate``.

        ``since`` defaults to now, so an event that arrived before the call is
        never returned; pass the timestamp :meth:`send` returned (or subscribe
        first) to catch a reply. See :meth:`Subscription.wait_for` for errors.
        """
        with self.subscribe(since) as sub:
            return sub.wait_for(predicate, timeout=timeout, raise_on_error=raise_on_error,
                                describe=describe)

    def wait_for_info(self, code: int, *, timeout: float = 30.0, since: Optional[float] = None,
                      raise_on_error: bool = True) -> TextMessage:
        """Wait for ``INFO;<code>`` (received at or after ``since``; default now)."""
        with self.subscribe(since) as sub:
            ev = sub.wait_for(_is_info(code), timeout=timeout, raise_on_error=raise_on_error,
                              describe=f"INFO;{int(code)}", slot_hint=True)
        assert isinstance(ev, TextMessage)
        return ev

    # -- sending --------------------------------------------------------------------

    def _refuse(self, command: str, decision: PolicyDecision, layer: str) -> None:
        level = logging.ERROR if layer == "denylist" else logging.WARNING
        log.log(level, "refusing to send %r (%s): %s", redact(command), layer, decision.reason)
        raise CommandRefusedError(command, decision, layer)

    def _check_static(self, command: str) -> None:
        """The denylist (on the command plus terminator), then the strict set."""
        if not isinstance(command, str):
            raise TypeError(f"a command must be a str, not {type(command).__name__}")
        decision = denylist_decision(command + self.terminator)
        if not decision.allowed:
            self._refuse(command, decision, "denylist")
        if self._strict:
            decision = strict_decision(command)
            if not decision.allowed:
                self._refuse(command, decision, "strict")

    def _check_policy(self, command: str, policy: Optional[CommandPolicy]) -> None:
        if policy is None:
            return
        decision = policy.check(command)
        if not isinstance(decision, PolicyDecision):
            decision = PolicyDecision(
                False, f"the command policy {policy!r} answered {decision!r}, not a "
                "PolicyDecision; refusing (a policy must answer PolicyDecision(True or False, "
                "reason))")
        if not decision.allowed:
            self._refuse(command, decision, "policy")
        log.debug("command policy allows %r: %s", redact(command), decision.reason)

    def _check_layers(self, command: str) -> int:
        """Denylist, strict set, policy: raise :class:`CommandRefusedError` or
        return the policy generation the command was checked under."""
        self._check_static(command)
        with self._policy_lock:
            policy, generation = self._policy, self._policy_generation
        self._check_policy(command, policy)
        return generation

    def _gate(self, command: str, allow_unsupported: bool) -> None:
        decision = self.capabilities.supports_command(command)
        if decision.reason == "not version-gated":
            return
        if decision.allowed:
            log.info("firmware gate for %s: %s", command, decision)
            return
        if allow_unsupported:
            log.warning("sending %r although the firmware is not known to have it "
                        "(allow_unsupported=True): %s", command, decision)
            return
        log.warning("refusing to send %r: %s", command, decision)
        raise CommandNotSupportedError(
            f"refusing to send {command!r}: {decision.reason}", decision
        )

    def _check_hazard(self, command: str, *, stopping: bool = False) -> None:
        with self._state_lock:
            hazard = self._hazard
        if hazard:
            extra = (" A software stop cannot work while the robot may be in upload mode: "
                     "use the robot's physical stop.") if stopping else ""
            raise RobotStateUncertainError(f"refusing to send {command!r}: {hazard}{extra}")

    def _ready_to_send_locked(self) -> None:
        """Pace, then let the transport finish a reply still pending on connection 0.

        The caller holds ``self.t.send_lock``. Pacing is measured from the
        transport's last send, whoever made it (a raw ``transport.send``, an
        upload block).
        """
        last = getattr(self.t, "last_send_at", None)
        if last is not None:
            while True:
                wait = self._command_interval - (time.monotonic() - last)
                if wait <= 0:
                    break
                time.sleep(wait)
        await_replies = getattr(self.t, "await_pending_replies", None)
        if await_replies is not None:
            await_replies()

    def _transmit_locked(self, command: str, generation: int) -> float:
        """Send now (no pacing); return the ``time.monotonic()`` it went out.

        The caller holds ``self.t.send_lock``. The last checks before the
        wire: the denylist on the exact text, and the policy again if it was
        swapped since the command was checked (``generation``). A swap needs
        the send lock, so none can happen between these checks and the send.
        """
        wire = command + self.terminator
        decision = denylist_decision(wire)
        if not decision.allowed:
            self._refuse(command, decision, "denylist")
        with self._policy_lock:
            policy, current = self._policy, self._policy_generation
        if current != generation:
            log.info("the command policy changed while %r waited to be sent: checking it "
                     "again under %r", redact(command), policy)
            self._check_policy(command, policy)
        before = time.monotonic()
        self.t.send(wire)
        sent = getattr(self.t, "last_send_at", None)
        return sent if sent is not None and sent >= before else before

    def _send_locked(self, command: str, generation: int) -> float:
        """Pace and send. The caller holds ``self.t.send_lock``."""
        self._ready_to_send_locked()
        return self._transmit_locked(command, generation)

    def _send_subscribed_locked(self, command: str,
                                generation: int) -> Tuple[float, "Subscription"]:
        """Pace, subscribe, send: the subscription starts just before the send.

        Subscribing earlier would let the reply to the previous command, if it
        arrives during the pacing wait, pass for this command's reply (replies
        carry no command name). The caller holds ``self.t.send_lock`` and owns
        the returned subscription.
        """
        self._ready_to_send_locked()
        sub = self.subscribe()
        try:
            return self._transmit_locked(command, generation), sub
        except BaseException:
            sub.close()
            raise

    def send(self, command: str, *, allow_unsupported: bool = False) -> float:
        """Send one text command; return the ``time.monotonic()`` just before it went out.

        Paced under the transport's send lock (see the module docstring).
        Raises :class:`~goliath_link.policy.CommandRefusedError` for a command
        the denylist, the strict set or the command policy refuses (checked
        first; ``allow_unsupported`` does not touch them),
        :class:`CommandNotSupportedError` for a command the firmware is not
        known to have (unless ``allow_unsupported``), and
        :class:`RobotStateUncertainError` after an abnormal upload.
        """
        generation = self._check_layers(command)
        self._gate(command, allow_unsupported)
        with self.t.send_lock:
            self._check_hazard(command)
            return self._send_locked(command, generation)

    def request(
        self,
        command: str,
        predicate: Callable[[object], bool],
        *,
        timeout: float = 30.0,
        raise_on_error: bool = True,
        describe: Optional[str] = None,
        slot_hint: bool = False,
    ) -> object:
        """Send ``command`` and wait for an event satisfying ``predicate``.

        The wait sees only records that arrived once the command was about to
        go out (after pacing), so the previous command's reply cannot be taken
        for this one. Gates and errors as for :meth:`send` and
        :meth:`Subscription.wait_for`.
        """
        generation = self._check_layers(command)
        self._gate(command, False)
        with self.t.send_lock:
            self._check_hazard(command)
            _, sub = self._send_subscribed_locked(command, generation)
        with sub:
            return sub.wait_for(predicate, timeout=timeout, raise_on_error=raise_on_error,
                                describe=describe or f"the reply to {command!r}",
                                slot_hint=slot_hint)

    # -- convenience commands (each returns the send timestamp) ------------------

    def request_app_info(self) -> float:
        return self.send(Commands.info())

    def get_global_status(self) -> float:
        return self.send(Commands.get_global_sys_status())

    def set_rf_channels(self, t1: int, t2: int) -> float:
        return self.send(Commands.set_rf_channels(t1, t2))

    def set_panel_size(self, x_max: int, y_max: int) -> float:
        return self.send(Commands.set_panel_size(x_max, y_max))

    def enter_jog(self) -> float:
        return self.send(Commands.enter_jog())

    def goto(self, x: float, y: float, z: float, angle: float) -> float:
        return self.send(Commands.goto_target(x, y, z, angle))

    def learn_firmware_version(self, *, timeout: float = 10.0) -> Optional[Version]:
        """Send ``INFO`` and record the firmware version from the ``INFO;34`` reply.

        On GFW 01.02.00 the reply arrives on connection 1. Returns the version,
        or None (logged) if the reply carries none; the gates then stay at
        their most conservative.
        """
        msg = self.request(Commands.info(), _is_info(int(Info.APP_INFO)), timeout=timeout,
                           describe="INFO;34 (the reply to INFO)", slot_hint=True)
        assert isinstance(msg, TextMessage)
        version = parse_firmware_version(msg.params[0]) if msg.params else None
        self._firmware_version = version
        self._firmware_source = (f"learned from {msg.raw!r}" if version is not None
                                 else f"not found in {msg.raw!r}")
        if version is None:
            log.warning("INFO;34 carries no firmware version (%r): the version stays unknown "
                        "and the most conservative firmware gates apply", msg.raw)
        else:
            log.info("robot firmware %s (from %r)", format_version(version), msg.raw)
            for line in FirmwareCapabilities.for_version(version).explain()[1:]:
                log.info("firmware gate: %s", line)
        return version

    # -- stop ------------------------------------------------------------------------

    def stop_work(
        self,
        *,
        timeout: float = DEFAULT_STOP_TIMEOUT,
        confirm_codes: Iterable[int] = STOP_CONFIRM_CODES,
    ) -> StopReport:
        """Stop the running job with ``ABORT WORK`` and wait until the robot confirms.

        Only on firmware newer than 01.02.00. Otherwise (01.02.00 or an unknown
        version) nothing is sent and :class:`StopNotSupportedError` is raised:
        that firmware has no software stop, use the robot's physical stop.

        Confirmation is ``INFO;18`` (work cycle exited) or ``INFO;23`` (work
        complete) received after the command was sent (``confirm_codes``).
        Raises :class:`StopRejectedError` on ``ERR;1``,
        :class:`GoliathSafetyError` on a safety error, :class:`ConnectionError`
        if the link drops, and :class:`StopNotConfirmedError` on timeout. It
        never reports a stop the robot did not confirm.

        Nothing is sent, and the call raises at once, while the robot may be in
        upload mode: :class:`StopBlockedError` if another thread's upload holds
        the send lock for longer than ``timeout``, and
        :class:`RobotStateUncertainError` after an abnormal upload (in upload
        mode ``ABORT WORK`` would only become program data).

        Like every command, ``ABORT WORK`` first passes the command safety
        layer: a strict client (``ABORT WORK`` is outside spec 09 section 1)
        or a command policy that does not allow it raises
        :class:`~goliath_link.policy.CommandRefusedError` before the firmware
        gate, and nothing is sent.
        """
        timeout = _positive("timeout", timeout)
        codes = tuple(int(c) for c in confirm_codes)
        if not codes:
            raise ValueError("confirm_codes must name at least one INFO code")
        generation = self._check_layers(Commands.abort_work())
        fw = self.firmware_version
        decision = FirmwareCapabilities.for_version(fw).supports_command("ABORT WORK")
        if not decision.allowed:
            log.error("software stop refused: %s", decision)
            raise StopNotSupportedError(
                f"cannot stop the job from software on firmware {format_version(fw)}: "
                f"{decision.reason}", decision
            )
        log.warning("stopping the job: sending ABORT WORK (%s)", decision)
        lock = self.t.send_lock
        msg = (f"could not send ABORT WORK within {timeout:g} s: another thread holds the "
               "send lock (a G-code upload in progress?). While an upload is in progress "
               "the robot stores every byte it receives as program data, so a software "
               "stop cannot be sent; nothing was sent. Use the robot's physical stop.")
        try:
            acquired = lock.acquire(timeout=timeout)
        except SendBlockedError as exc:  # called from a transport listener
            log.error("%s", msg)
            raise StopBlockedError(msg + f" ({exc})") from exc
        if not acquired:
            log.error("%s", msg)
            raise StopBlockedError(msg)
        try:
            self._check_hazard("ABORT WORK", stopping=True)
            sent_at, sub = self._send_subscribed_locked(Commands.abort_work(), generation)
        finally:
            lock.release()
        report = StopReport(firmware_version=fw, decision=decision, sent_at=sent_at,
                            confirm_codes=codes)
        expected = " or ".join(f"INFO;{c}" for c in codes)
        with sub:
            deadline = sent_at + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    rec = sub.get(timeout=remaining)
                except queue.Empty:
                    break
                ev = rec.event
                if isinstance(ev, TransportClosed):
                    raise _closed_error(
                        ev, "before the robot confirmed the stop; the job may still be "
                        "running: use the robot's physical stop if it is still moving")
                report.events.append(Observed("stop", rec))
                if not isinstance(ev, TextMessage):
                    continue
                if ev.kind == "INFO" and ev.code in codes:
                    report.confirmed_by = ev
                    report.confirmation = rec
                    log.warning("job stop confirmed by %s", describe_record(rec))
                    return report
                if ev.kind == "INFO" and ev.code == int(Info.OK):
                    report.acknowledged = True
                elif ev.kind == "ERR" and ev.code is not None:
                    if is_safety_error(ev):
                        raise _err_error(ev, context="while stopping the job; the stop is NOT "
                                         "confirmed: check the machine", stage="stop",
                                         report=report)
                    if ev.code == int(Error.CMD_NOT_EXECUTED):
                        raise StopRejectedError(
                            ev.code,
                            "the robot answered ABORT WORK with ERR;1 (command not executed): "
                            "it did not confirm a stop (no job running?). If the machine is "
                            "moving, use its physical stop.",
                            event=ev, stage="stop", report=report,
                        )
                    report.errors.append(Observed("stop", rec))
                elif ev.kind == "WARN":
                    report.warnings.append(Observed("stop", rec))
        ack = (" The robot acknowledged the command (INFO;0) but did not report the work "
               "cycle ending." if report.acknowledged else "")
        msg = (f"ABORT WORK was sent but no {expected} arrived within {timeout:g} s.{ack} The "
               "job may still be running: check the machine and use the robot's physical "
               "stop if it is still moving.")
        log.error("%s", msg)
        raise StopNotConfirmedError(msg, report)

    def abort_work(self, **kwargs: object) -> StopReport:
        """The old name of :meth:`stop_work` (same gate, same confirmation)."""
        return self.stop_work(**kwargs)  # type: ignore[arg-type]

    # -- the upload sequence -----------------------------------------------------------

    def upload_gcode(
        self,
        gcode: Union[str, bytes],
        *,
        firmware_version: Optional[Sequence[int]] = None,
        block_size: int = DEFAULT_BLOCK_SIZE,
        block_interval: float = DEFAULT_BLOCK_INTERVAL,
        continue_wait: float = DEFAULT_CONTINUE_WAIT,
        header_wait: float = DEFAULT_HEADER_WAIT,
        header_ack: str = "observe",
        header_ack_timeout: float = DEFAULT_HEADER_ACK_TIMEOUT,
        done_timeout: float = DEFAULT_DONE_TIMEOUT,
        confirm: str = "info8",
        info8_grace: float = DEFAULT_INFO8_GRACE,
        allow_second_upload: bool = False,
    ) -> UploadReport:
        """Upload a G-code program and wait for the robot to confirm it.

        ``gcode`` is program text (cleaned here, exactly like the vendor) or
        bytes already in cleaned form (checked). An empty or comment-only
        program raises :class:`EmptyProgramError` before anything is sent.

        Sequence, under the transport's send lock: subscribe;
        ``GCODE CONTINUE;0`` if the firmware is known to have it, then wait
        ``continue_wait``; ``GCODE UPLOAD;<size>``; the header window; the
        bytes in ``block_size`` blocks ``block_interval`` apart; the
        confirmation wait. The lock is released as soon as the robot signals
        the end of upload mode (``INFO;0``, ``INFO;8`` or work starting), so
        other senders -- a stop -- need not wait for the rest of the
        confirmation wait.

        The header acknowledgement is uncertain, so it is a choice:
        ``header_ack="observe"`` (default) waits ``header_wait`` (vendor:
        0.25 s), records an ``INFO;7`` if one comes (GFW 01.02.00's header
        handler sends nothing), and stops before streaming if the robot answers
        the header with ``ERR;0`` or ``ERR;1``. ``header_ack="info7"`` requires
        ``INFO;7`` within ``header_ack_timeout`` and otherwise stops before
        streaming (:class:`UploadHeaderNotAcknowledgedError`).

        Confirmation: ``INFO;8`` received after the last block was sent. An
        earlier ``INFO;8`` (e.g. a late one from a previous upload) is ignored
        and listed in ``report.ignored``. On firmware that may not send
        ``INFO;8`` (below 01.02.03, or unknown), once the robot signals the end
        of the upload (``INFO;0`` after the last block, or work starting) the
        wait continues only ``info8_grace`` seconds. With ``confirm="info8"``
        (default) a missing ``INFO;8`` raises :class:`UploadUnconfirmedError`;
        with ``confirm="info8-or-info0"`` the end-of-upload ``INFO;0`` is
        accepted and reported as such in ``confirmed_by``.

        Errors: only ``ERR;0`` means the upload failed
        (:class:`UploadFailedError`). Safety errors always raise
        :class:`GoliathSafetyError`. Other ``ERR``/``WARN`` lines are collected
        in the report. If the upload ends abnormally after the header while the
        robot may still be in upload mode, :attr:`upload_hazard` is set and the
        client refuses to send until :meth:`note_robot_restarted` or
        :meth:`clear_upload_hazard`.

        On firmware <= 01.02.00 (or unknown) a second upload without a robot
        restart raises :class:`RestartRequiredError` (the vendor's gate) unless
        ``allow_second_upload``. ``firmware_version`` overrides the client's
        version for this upload only.

        Before anything is sent, every command the upload would send
        (``GCODE CONTINUE;0`` where the firmware gate sends it, and
        ``GCODE UPLOAD;<size>``) passes the command safety layer; a refusal
        raises :class:`~goliath_link.policy.CommandRefusedError`, sends
        nothing, sets no hazard and does not count as an upload. From then
        until this method returns or raises, :meth:`set_command_policy` is
        refused (:attr:`upload_in_progress`).

        Attribution is by arrival time, which is an inference (replies carry no
        command name): a record belongs to the stage during which it arrived
        (``report.errors`` etc. carry the stage), so a reply delayed past the
        next command is attributed to that command. The vendor's 0.25 s waits
        and the command pacing make that unlikely; keep ``continue_wait`` and
        ``header_wait`` well above the clock tick (about 16 ms for
        ``time.monotonic()`` on Windows with older Pythons). The
        end-of-upload ``INFO;0`` is likewise inferred: it is the first
        ``INFO;0`` after the last block, while the send lock keeps every other
        command of this client off the link.

        Returns the :class:`UploadReport` (``confirmed`` is always True on return).
        """
        block_size = int(block_size)
        if block_size <= 0:
            raise ValueError(f"block_size must be > 0, not {block_size!r}")
        block_interval = _non_negative("block_interval", block_interval)
        continue_wait = _non_negative("continue_wait", continue_wait)
        header_wait = _non_negative("header_wait", header_wait)
        header_ack_timeout = _positive("header_ack_timeout", header_ack_timeout)
        done_timeout = _positive("done_timeout", done_timeout)
        info8_grace = _non_negative("info8_grace", info8_grace)
        if header_ack not in HEADER_ACK_MODES:
            raise ValueError(f"header_ack must be one of {HEADER_ACK_MODES}, not {header_ack!r}")
        if confirm not in CONFIRM_MODES:
            raise ValueError(f"confirm must be one of {CONFIRM_MODES}, not {confirm!r}")
        payload = self._prepare_payload(gcode)
        fw = _as_version(firmware_version) if firmware_version is not None else self.firmware_version
        caps = FirmwareCapabilities.for_version(fw)
        commands = [Commands.gcode_upload(len(payload))]
        if caps.supports_command("GCODE CONTINUE").allowed:
            commands.insert(0, Commands.gcode_continue(False))
        with self._policy_lock:
            self._uploads_in_progress += 1
            policy, generation = self._policy, self._policy_generation
        try:
            for command in commands:  # all of them before the first byte goes out
                self._check_static(command)
                self._check_policy(command, policy)
            return self._upload_checked(
                payload, caps, fw, generation, firmware_version=firmware_version,
                block_size=block_size, block_interval=block_interval,
                continue_wait=continue_wait, header_wait=header_wait, header_ack=header_ack,
                header_ack_timeout=header_ack_timeout, done_timeout=done_timeout,
                confirm=confirm, info8_grace=info8_grace,
                allow_second_upload=allow_second_upload)
        finally:
            with self._policy_lock:
                self._uploads_in_progress -= 1

    def _upload_checked(self, payload: bytes, caps: FirmwareCapabilities,
                        fw: Optional[Version], generation: int, *,
                        firmware_version: Optional[Sequence[int]], block_size: int,
                        block_interval: float, continue_wait: float, header_wait: float,
                        header_ack: str, header_ack_timeout: float, done_timeout: float,
                        confirm: str, info8_grace: float,
                        allow_second_upload: bool) -> UploadReport:
        """:meth:`upload_gcode` once its commands have passed the safety layer."""
        n_blocks = (len(payload) + block_size - 1) // block_size

        lock = self.t.send_lock
        lock.acquire()
        held = [True]

        def release_send_lock() -> None:
            # Called on this thread only (the lock is re-entrant and owned).
            if held[0]:
                held[0] = False
                lock.release()

        try:
            self._check_hazard(Commands.gcode_upload(len(payload)))
            # Pace (and let a pending reply finish) before subscribing, so a
            # reply to an earlier command cannot be taken for part of the upload.
            self._ready_to_send_locked()
            with self.subscribe() as sub:
                report = UploadReport(
                    size=len(payload), blocks_total=n_blocks, firmware_version=fw,
                    capabilities=caps, confirm_mode=confirm, header_ack_mode=header_ack,
                    started_at=sub.since,
                )
                source = ("supplied for this upload by the caller"
                          if firmware_version is not None else self._firmware_source)
                report.note(f"firmware version {format_version(fw)} ({source})")
                self._second_upload_gate(caps, allow_second_upload, report)
                try:
                    return self._upload(sub, payload, caps, report, generation,
                                        block_size=block_size,
                                        block_interval=block_interval,
                                        continue_wait=continue_wait, header_wait=header_wait,
                                        header_ack=header_ack,
                                        header_ack_timeout=header_ack_timeout,
                                        done_timeout=done_timeout, confirm=confirm,
                                        info8_grace=info8_grace,
                                        release_send_lock=release_send_lock)
                except BaseException as exc:
                    report.finished_at = time.monotonic()
                    self._after_failed_upload(report, exc)
                    raise
        finally:
            release_send_lock()

    def _prepare_payload(self, gcode: Union[str, bytes]) -> bytes:
        if isinstance(gcode, (bytes, bytearray, memoryview)):
            payload = check_cleaned(bytes(gcode))
        elif isinstance(gcode, str):
            payload = clean_gcode(gcode)
        else:
            raise TypeError(f"gcode must be str or bytes, not {type(gcode).__name__}")
        if not has_program_content(payload):
            raise EmptyProgramError(
                "refusing to upload a program with no G-code (empty, or only comments and "
                "blank lines); the robot's reaction to an empty upload is unknown, and the "
                "vendor app never sent one"
            )
        return payload

    def _second_upload_gate(self, caps: FirmwareCapabilities, allow: bool,
                            report: UploadReport) -> None:
        decision = caps._decision("restart required")
        with self._state_lock:
            done = self._uploads_since_restart
        if not decision.allowed or done == 0:
            report.note(f"second-upload gate: {decision}; uploads since the last noted "
                        f"restart: {done}")
            return
        if allow:
            log.warning("second upload without a robot restart (allow_second_upload=True) "
                        "although %s", decision.reason)
            report.note(f"second-upload gate overridden by allow_second_upload=True: {decision}")
            return
        raise RestartRequiredError(
            f"{done} program(s) were already uploaded since the last noted robot restart; on "
            f"firmware {format_version(caps.version)} {decision.reason}. Power-cycle the robot "
            "and call note_robot_restarted(), or pass allow_second_upload=True to override."
        )

    def _after_failed_upload(self, report: UploadReport, exc: BaseException) -> None:
        """Set the upload hazard if the robot may still be in upload mode."""
        if report.header_sent_at is None:
            return  # the header never went out: the robot cannot be in upload mode
        if (report.end_of_upload_ack is not None or report.confirmation is not None
                or report.work_started):
            return  # the robot signalled the end of the upload
        if isinstance(exc, UploadFailedError) and exc.stage == "header" and report.blocks_sent == 0:
            answer = exc.event.raw if exc.event is not None else "an error"
            reason = (f"the robot answered the GCODE UPLOAD header with {answer} and no "
                      "program bytes were sent; if it entered upload mode anyway, it is "
                      "waiting for them")
        else:
            reason = (f"the upload of {report.size} bytes ended abnormally after "
                      f"{report.blocks_sent}/{report.blocks_total} block(s) "
                      f"({type(exc).__name__}) with no end-of-upload signal from the robot")
        hazard = (
            f"{reason}. While an upload is in progress the firmware stores every buffer it "
            "receives, from any connection, as program data, so the robot may still be "
            "waiting for program bytes and anything sent now would become part of the "
            "program. Power-cycle the robot and call note_robot_restarted(), or call "
            "clear_upload_hazard() if you know the robot is not in upload mode."
        )
        with self._state_lock:
            self._hazard = hazard
        log.error("upload hazard set: %s", hazard)

    # The stage a record belongs to, by when it arrived.
    @staticmethod
    def _stage_of(rec: LinkEvent, report: UploadReport) -> str:
        if report.header_sent_at is None or rec.t < report.header_sent_at:
            return "continue" if report.continue_sent else "before header"
        if report.first_block_at is None or rec.t < report.first_block_at:
            return "header"
        if report.last_block_at is None or rec.t < report.last_block_at:
            return "stream"
        return "confirm"

    def _upload_record(self, rec: LinkEvent, report: UploadReport) -> Optional[str]:
        """Classify one record seen during an upload. Returns ``"info8"``,
        ``"info0"`` or ``"work"`` for the confirmation logic; raises for
        failures; collects everything else in the report."""
        ev = rec.event
        if isinstance(ev, TransportClosed):
            raise _closed_error(ev, f"during the upload (stage: {self._stage_of(rec, report)})")
        stage = self._stage_of(rec, report)
        obs = Observed(stage, rec)
        report.events.append(obs)
        if isinstance(ev, PositionFrame):
            if stage == "confirm":
                first = not any(isinstance(o.record.event, PositionFrame)
                                for o in report.work_started)
                if first:
                    report.work_started.append(obs)
                    return "work"
            return None
        if not isinstance(ev, TextMessage):
            return None
        if ev.kind == "ERR" and ev.code is not None:
            if is_safety_error(ev):
                raise _err_error(
                    ev, context=f"during the upload (stage: {stage}); "
                    + ("the robot may have accepted the program and start working"
                       if stage == "confirm" else "the upload was stopped"),
                    stage=stage, report=report)
            if ev.code == int(Error.GCODE_UPLOAD):
                raise _err_error(ev, context=f"(stage: {stage}): the robot reports a G-code "
                                 "upload error", stage=stage, report=report,
                                 cls=UploadFailedError)
            if ev.code == int(Error.CMD_NOT_EXECUTED) and stage == "header":
                raise _err_error(ev, context="in reply to the GCODE UPLOAD header: the robot did "
                                 "not accept the upload; nothing was streamed", stage=stage,
                                 report=report, cls=UploadFailedError)
            report.errors.append(obs)
            return None
        if ev.kind == "WARN":
            report.warnings.append(obs)
            return None
        if ev.kind != "INFO":
            return None
        if ev.code == int(Info.ENTER_GCODE_UPLOAD):
            if report.header_ack is None and stage != "continue" and stage != "before header":
                report.header_ack = ev
                report.note(f"upload header acknowledged with {describe_record(rec)} "
                            f"(stage {stage})")
            return None
        if ev.code == int(Info.GCODE_UPLOAD_DONE):
            if stage == "confirm":
                return "info8"
            report.ignored.append(
                f"{describe_record(rec)} at stage '{stage}', before the last block was sent: "
                "not a confirmation of this upload (a late one from an earlier upload?)")
            log.warning("upload: ignoring %s", report.ignored[-1])
            return None
        if ev.code == int(Info.OK):
            if stage == "confirm":
                return "info0"
            report.ignored.append(f"{describe_record(rec)} at stage '{stage}' (not the "
                                  "end-of-upload acknowledgement)")
            return None
        if ev.code in _WORK_START_CODES and stage == "confirm":
            report.work_started.append(obs)
            return "work"
        return None

    def _watch(self, sub: Subscription, report: UploadReport, until: float,
               stop: Optional[Callable[[], bool]] = None) -> None:
        """Process records until ``until`` (or until ``stop()`` is true)."""
        while True:
            if stop is not None and stop():
                return
            remaining = until - time.monotonic()
            if remaining <= 0:
                for rec in sub.pending():
                    self._upload_record(rec, report)
                return
            try:
                rec = sub.get(timeout=remaining)
            except queue.Empty:
                return
            self._upload_record(rec, report)

    def _upload(self, sub: Subscription, payload: bytes, caps: FirmwareCapabilities,
                report: UploadReport, generation: int, *, block_size: int,
                block_interval: float,
                continue_wait: float, header_wait: float, header_ack: str,
                header_ack_timeout: float, done_timeout: float, confirm: str,
                info8_grace: float,
                release_send_lock: Callable[[], None] = lambda: None) -> UploadReport:
        report.note(f"confirmation: confirm={confirm!r}; {caps._decision('INFO;8')}")
        report.note(
            f"header acknowledgement: header_ack={header_ack!r} ("
            + ("INFO;7 required before streaming" if header_ack == "info7" else
               "INFO;7 recorded if it comes; GFW 01.02.00's header handler sends no reply, "
               "and INFO;7 on newer firmware is the vendor app's expectation, unverified")
            + ")")

        # 1. GCODE CONTINUE;0 (only where the firmware is presumed to have it).
        cont = caps.supports_command("GCODE CONTINUE")
        if cont.allowed:
            self._send_locked(Commands.gcode_continue(False), generation)
            report.continue_sent = True
            report.note(f"sent GCODE CONTINUE;0: {cont.reason}")
            self._watch(sub, report, time.monotonic() + continue_wait)
        else:
            report.note(f"GCODE CONTINUE;0 not sent: {cont.reason}")

        # 2. The header. From here the robot may be in upload mode.
        report.header_sent_at = self._send_locked(Commands.gcode_upload(len(payload)),
                                                  generation)
        if header_ack == "info7":
            self._watch(sub, report, report.header_sent_at + header_ack_timeout,
                        stop=lambda: report.header_ack is not None)
            if report.header_ack is None:
                raise UploadHeaderNotAcknowledgedError(
                    f"no INFO;7 within {header_ack_timeout:g} s of the GCODE UPLOAD header "
                    "(header_ack='info7'); nothing was streamed. GFW 01.02.00 sends no "
                    "reply to the header; use header_ack='observe' there.", report)
            self._watch(sub, report, report.header_sent_at + header_wait)
        else:
            self._watch(sub, report, report.header_sent_at + header_wait)
            if report.header_ack is None:
                report.note(f"no INFO;7 within {header_wait:g} s of the header (expected on "
                            "GFW 01.02.00, whose header handler sends nothing); streaming")

        # 3. The program.
        with self._state_lock:
            self._uploads_since_restart += 1
        blocks = [payload[i:i + block_size] for i in range(0, len(payload), block_size)]
        # Every block counts as a send for pacing (transport.last_send_at), so
        # the next command is paced after the last program bytes, too.
        for idx, block in enumerate(blocks):
            for rec in sub.pending():
                self._upload_record(rec, report)
            last = idx == len(blocks) - 1
            now = time.monotonic()
            if idx == 0:
                report.first_block_at = now
            if last:
                report.last_block_at = now
            self.t.send_bytes(block)
            report.blocks_sent += 1
            if not last and block_interval:
                time.sleep(block_interval)
        assert report.last_block_at is not None

        # 4. Confirmation.
        total = report.last_block_at + done_timeout
        deadline = total
        info8_certain = caps.confirms_upload_with_info8 is True
        lock_released = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                rec = sub.get(timeout=remaining)
            except queue.Empty:
                break
            action = self._upload_record(rec, report)
            if action in ("info8", "info0", "work"):
                if not lock_released:
                    lock_released = True
                    release_send_lock()
                    report.note(f"send lock released after {describe_record(rec)}: the robot "
                                "left upload mode, so other commands (a stop) may go out")
            if action == "info8":
                report.confirmation = rec
                report.confirmed_by = "INFO;8"
                break
            if action == "info0" and report.end_of_upload_ack is None:
                report.end_of_upload_ack = rec
                report.note(f"{describe_record(rec)} after the last block: the robot left "
                            "upload mode (GFW 01.02.00 sends INFO;0 at the end of an upload)")
            if action in ("info0", "work") and not info8_certain:
                deadline = min(total, time.monotonic() + info8_grace)

        if report.confirmation is None and confirm == "info8-or-info0" \
                and report.end_of_upload_ack is not None:
            report.confirmation = report.end_of_upload_ack
            report.confirmed_by = ("INFO;0 (end of upload mode; no INFO;8; accepted because "
                                   "confirm='info8-or-info0')")
            log.warning("upload accepted on the end-of-upload INFO;0 without INFO;8 "
                        "(confirm='info8-or-info0')")
        report.finished_at = time.monotonic()
        if report.confirmed:
            log.info("upload of %d bytes confirmed by %s", report.size, report.confirmed_by)
            if report.errors or report.warnings:
                log.warning("upload: errors/warnings during the upload: %s",
                            ", ".join(str(o) for o in report.errors + report.warnings))
            return report
        raise UploadUnconfirmedError(self._unconfirmed_message(report, caps, done_timeout),
                                     report, caps.confirms_upload_with_info8)

    @staticmethod
    def _unconfirmed_message(report: UploadReport, caps: FirmwareCapabilities,
                             done_timeout: float) -> str:
        vs = format_version(caps.version)
        parts = []
        if report.work_started:
            parts.append(
                "WARNING: the robot reports work starting ("
                + ", ".join(describe_record(o.record) for o in report.work_started)
                + "): it appears to be RUNNING the program.")
        parts.append(f"The upload of {report.size} bytes was not confirmed: no INFO;8 arrived "
                     "after the last block.")
        if caps.confirms_upload_with_info8:
            parts.append(f"Firmware {vs} should send INFO;8 (the vendor app waits for it on "
                         f">= 01.02.03); it did not within {done_timeout:g} s.")
        else:
            parts.append(f"Firmware {vs}: below 01.02.03 the vendor app did not wait for INFO;8, "
                         "so this firmware may not send it at all; the upload may still have "
                         "succeeded.")
        if report.end_of_upload_ack is not None:
            parts.append("The robot did send INFO;0 after the last block, which GFW 01.02.00 "
                         "sends when it leaves upload mode; pass confirm='info8-or-info0' to "
                         "accept that as the confirmation.")
        elif not report.work_started:
            parts.append(f"No end-of-upload INFO;0 arrived either within {done_timeout:g} s: "
                         "the robot may still be in upload mode, so the client now refuses to "
                         "send (see upload_hazard).")
        if report.errors:
            parts.append("Errors during the upload: "
                         + ", ".join(str(o) for o in report.errors) + ".")
        return " ".join(parts)
