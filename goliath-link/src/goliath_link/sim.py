# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""A Goliath robot simulator (a fake robot on TCP), modelled on firmware GFW 01.02.00.

Run it and point a client (this library, the probe, a GUI) at it::

    python -m goliath_link.sim --profile gfw-01.02.00 --port 8080
    python -m goliath_link.probe --host 127.0.0.1 --port 8080

**It is a simulator, not the robot.** Passing against it shows that a client
copes with the wire behaviour documented in ``docs/protocol/`` (chiefly
``09-firmware-01.02.00.md``); it proves nothing about real hardware. Every reply
whose exact text or timing the firmware facts do not pin down is marked
``# GUESS`` in this file.

Profiles
========

``gfw-01.02.00`` (the CLI default)
    Reproduces what static disassembly of the only surviving firmware image
    established:

    * **Six client slots.** Each accepted TCP connection takes the first free
      slot; a slot is freed when the peer closes (receive returns <= 0). A
      connection that arrives while all six are taken is accepted and closed
      at once.
    * **Output is sent by slot, not to whoever asked.** Slot 0 (normally the
      1st connection) gets free-form ``printf`` text such as
      ``cfg read index : 28500`` and the 4-byte tower frames; slot 1 (2nd
      connection) gets every ``INFO``/``ERR``/``WARN`` line, including
      ``INFO;34`` (identity), ``INFO;40`` and the ``INFO;0`` acknowledgement;
      slot 2 (3rd connection) gets position frames. A message for an empty slot
      is **dropped** (the firmware has no fallback) and logged in
      :attr:`SimulatedGoliath.events` as ``dropped``.
    * **Wire shapes.** Every slot message is one **512-byte** block: the
      message, then NUL padding. Tower frames are exactly 4 bytes. A few
      *direct replies* go to the connection that sent the command, with their
      exact length and no terminator added: the 48-byte ``GLOBAL STATUS`` blob,
      ``CAGE STATUS: OPEN\\n`` / ``CAGE STATUS: CLOSED`` (no ``\\n``),
      ``xmax=... \\n\\rymax=...``, ``count: ...``. ``cfg read index : %d`` and
      ``spindle distance: %.2f`` have no newline either (they end at the
      padding).
    * **One received buffer = one command.** The text up to the first NUL is
      split like C ``strtok(buf, ";")``; the name is upper-cased and compared
      **exactly** with the 94-entry 01.02.00 command table
      (:data:`GFW_01_02_00_TABLE`). A terminator (``INFO\\n``) or two
      commands coalesced into one buffer make an unknown name.
    * **Unknown names** follow the ``unknown_command`` policy: ``"silent"``
      (default; the static reading: no reply, no effect) or ``"error"``
      (``ERR;1`` on slot 1, a GUESS). The real behaviour is not established.
      Table entries outside the dispatch table (``SYS RESET``, ``OK``,
      ``INVALID COMMAND`` ...) do nothing, whatever the policy.
    * **G-code upload.** ``GCODE UPLOAD;<n>`` turns the parser off: every
      buffer, from **any** connection, is stored as program data until <n>
      bytes have arrived; then the firmware sends ``INFO;0``. Bytes after the
      size in the header's own buffer are not program data.
    * Commands in the table that the simulator does not model (motor, flash,
      test-mode ... commands the vendor app never sends) are logged as
      ``unmodelled`` and answered only with the ``INFO;0`` their handler is
      known to send; no effect is simulated.

``vendor-app-newer``
    Firmware **newer** than 01.02.00, of which no image survives. Modelled from
    what the vendor app (Slingshot 1.9.3.0) *expects*, **not** from firmware:
    the 01.02.00 table plus ``ABORT WORK``, ``GCODE CONTINUE``,
    ``SEND TCP POS``, ``GET/SET Z BACKLASH``, ``GET/SET Z PLATE OFFSET``,
    ``SET SPINDLE DISTANCE``, ``DISCARD TWRS DISTANCE`` and ``CMD SYS RESET``;
    identity ``GFW 01.02.03``; the 19-byte ``G...S`` global-status frame;
    ``INFO;7`` on the upload header and ``INFO;8``/``INFO;9`` when it completes.
    Slot routing and wire shapes are the same as 01.02.00 (nothing says they
    changed). Every reply specific to this profile is a GUESS.

``legacy-spec``
    The simulator's earlier, pre-correction model: every reply goes to the
    connection that sent the command, nothing is padded, unknown commands get
    ``ERR;1``. **GFW 01.02.00 contradicts this model.** It is kept only so
    tests written for the single-connection transport keep running.
    ``SimulatedGoliath()`` *without* ``profile=`` falls back to it and emits a
    :class:`LegacyProfileWarning`; pass ``profile=`` explicitly.

Hazard options (for tests)
==========================

``occupied_slots``
    slots already held by another client (an old Slingshot, a phone) when the
    simulator starts; output routed there is logged as ``to-other-client``.
    :meth:`~SimulatedGoliath.occupy_slot` / :meth:`~SimulatedGoliath.release_slot`
    change this at run time.
``slot_release_delay``
    seconds a closed connection keeps its slot (how fast the real robot frees
    one is unknown), so a quick reconnect lands in an unexpected slot.
``drop_on_accept`` / :meth:`~SimulatedGoliath.drop`
    the robot closes a connection (by accept order, or by slot, at any time).
``coalesce_window``
    after a receive, keep reading for this long and treat everything as one
    buffer, like a slow Wi-Fi module; back-to-back commands then become one
    unknown name.

Observing the simulator
=======================

* :attr:`~SimulatedGoliath.received` lists every parsed command as a
  :class:`ReceivedCommand`: a ``str`` (so it compares equal to the command
  text) that also carries ``.conn`` (accept order, from 0) and ``.slot``.
* :attr:`~SimulatedGoliath.events` is the simulator's own log
  (:class:`SimEvent`): accepts, closes, every message sent, dropped or routed
  to another client, unknown commands, upload stages, and ``done`` after each
  received buffer has been fully handled.
"""

from __future__ import annotations

import argparse
import re
import select
import socket
import struct
import sys
import threading
import time
import traceback
import warnings
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

# --------------------------------------------------------------------------- #
# Firmware facts (docs/protocol/09-firmware-01.02.00.md)
# --------------------------------------------------------------------------- #

SLOT_COUNT = 6
SLOT_BLOCK = 512  # send_slot always sends this many bytes
DIRECT_REPLY_MAX = 1024  # set_response copies at most this many bytes
STATUS_BLOB_HEAD = b"GLOBAL STATUS HEAD\x00"
STATUS_BLOB_TALE = b"GLOBAL STATUS TALE\x00"

#: Slot holder shown by :meth:`SimulatedGoliath.slot_holders` for a slot taken
#: by a client outside the test (``occupied_slots``).
OTHER_CLIENT = "other-client"

#: The 94 names of the GFW 01.02.00 command table, in table order. Records
#: 4-87 have handlers; 0-3 and 88-93 are matched but not dispatched.
GFW_01_02_00_TABLE: Tuple[str, ...] = (
    "COMMAND MODE ENTERED", "EXIT COMMAND MODE", "SYS RESET", "SET PID",
    "ENABLE SPINDLE", "DISABLE SPINDLE", "INFO", "SET RTC TIME", "SET RTC DATE",
    "GET RTC DATE", "BOOT", "LOAD", "READ BACKUP", "EFLASH ERASE", "MOVE Z",
    "RUN STEPPER", "STOP STEPPER", "GOTO POS STEPPER", "START MOTORS",
    "MOVE STEPPER", "SET STEP CLOCK MODE", "SPINDLE START", "SPINDLE STOP",
    "GCODE UPLOAD", "GCODE START", "GCODE STOP", "GCODE RESTART", "GCODE READ",
    "ZERO SET", "GOTO TARGET", "EXIT JOG", "SET SETUP PHASE", "SET WORK PHASE",
    "SET VERTEX", "GET VERTEXES", "SET PANEL SIZE", "GET PANEL SIZE",
    "ENABLE WORK MON", "ENABLE INFO LOG", "TOWERS POSITION", "GET TOWERS DISTANCE",
    "SET START POS", "GET START POS", "WORK SPEED MOD", "REQ UPD POS",
    "SET RF CHANNELS", "SET T2 POSITION", "BTN CLICK", "RESET NET KEY",
    "ENABLE MANUAL JOG", "ENTER JOG", "ENABLE ASPIRATION", "DISABLE ASPIRATION",
    "ENTER ENCODER CALIBRATION", "EXIT ENCODER CALIBRATION", "SCAN WIFI",
    "ENTER MANUAL ENC CALIBRATION", "EXIT MANUAL ENC CALIBRATION",
    "SET INDEX CALIBRATION VALUE", "JOG STOP GOLIATH", "DISABLE MANUAL JOG",
    "SPINDLE ON", "SPINDLE OFF", "ASP ON", "ASP OFF", "READ INDEX CALIBRATION",
    "POLL CAGE", "STOP POLL CAGE", "ENTER TEST PHASE", "EXIT TEST PHASE",
    "PRINT SYS REPORT", "POLL ENCODER", "STOP POLL ENCODER", "TEST Z CAL",
    "TEST RF", "SPINDLE CALIB VALUE", "READ SPINDLE CALIB", "PRINT ENCODER CAN",
    "LAST STATUS", "FREE Z AXIS", "SANITY CHECK", "LOAD WIFI", "GET SSIDS",
    "GET SPINDLE CAGE STATUS", "GET PARAM MEMORY", "SET PARAM MEMORY",
    "GET GLOBAL SYS STATUS", "RESET PARAM MEMORY",
    "", "OK", "INVALID COMMAND", "INVALID VALUE", "ERROR", "FIRST BACKUP",
)
assert len(GFW_01_02_00_TABLE) == 94

#: Commands the vendor app knows that GFW 01.02.00 does not have.
VENDOR_NEWER_EXTRA_COMMANDS = frozenset({
    "ABORT WORK", "GCODE CONTINUE", "SEND TCP POS", "GET Z BACKLASH",
    "SET Z BACKLASH", "GET Z PLATE OFFSET", "SET Z PLATE OFFSET",
    "SET SPINDLE DISTANCE", "DISCARD TWRS DISTANCE", "CMD SYS RESET",
})

#: Dispatched 01.02.00 handlers whose own body sends no ``INFO;0`` (spec 09,
#: "Handler sends"). Used for commands the simulator does not model.
_NO_INFO0_HANDLERS = frozenset({
    "INFO", "BOOT", "LOAD", "READ BACKUP", "MOVE STEPPER", "SPINDLE START",
    "SPINDLE STOP", "GCODE UPLOAD", "GCODE READ", "SET START POS", "GET START POS",
    "REQ UPD POS", "ENTER ENCODER CALIBRATION", "EXIT ENCODER CALIBRATION",
    "JOG STOP GOLIATH", "PRINT SYS REPORT", "POLL ENCODER", "STOP POLL ENCODER",
    "LAST STATUS", "FREE Z AXIS", "SANITY CHECK", "LOAD WIFI", "GET SSIDS",
})


@dataclass(frozen=True)
class Profile:
    """What one simulated firmware does. See the module docstring."""

    name: str
    basis: str  # where this behaviour comes from
    firmware: Tuple[int, int, int]
    dispatched: frozenset  # names that reach a handler
    not_dispatched: frozenset  # names in the table with no handler
    global_status: str  # "blob48" | "gs19"
    upload_acks: str  # "info0" | "info0+info8" | "vendor" | "legacy"
    slot_routed: bool  # False only for legacy-spec


_GFW_DISPATCHED = frozenset(GFW_01_02_00_TABLE[4:88])
_GFW_NOT_DISPATCHED = frozenset(GFW_01_02_00_TABLE[:4] + GFW_01_02_00_TABLE[88:])

PROFILES: Dict[str, Profile] = {
    "gfw-01.02.00": Profile(
        name="gfw-01.02.00",
        basis="static disassembly of the GFW 01.02.00 firmware image "
              "(docs/protocol/09-firmware-01.02.00.md)",
        firmware=(1, 2, 0),
        dispatched=_GFW_DISPATCHED,
        not_dispatched=_GFW_NOT_DISPATCHED,
        global_status="blob48",
        upload_acks="info0",
        slot_routed=True,
    ),
    "vendor-app-newer": Profile(
        name="vendor-app-newer",
        basis="the vendor app's expectations (Slingshot 1.9.3.0) for firmware newer "
              "than 01.02.00; no image of such firmware survives, so every reply "
              "specific to this profile is a guess",
        firmware=(1, 2, 3),  # GUESS: the highest version the vendor app gates on
        dispatched=_GFW_DISPATCHED | VENDOR_NEWER_EXTRA_COMMANDS,
        not_dispatched=_GFW_NOT_DISPATCHED,
        global_status="gs19",
        upload_acks="vendor",
        slot_routed=True,
    ),
    "legacy-spec": Profile(
        name="legacy-spec",
        basis="the pre-correction spec model (every reply on the requesting "
              "connection, no padding); GFW 01.02.00 contradicts it",
        firmware=(1, 2, 0),
        dispatched=_GFW_DISPATCHED | VENDOR_NEWER_EXTRA_COMMANDS,
        not_dispatched=frozenset(),
        global_status="gs19",
        upload_acks="legacy",
        slot_routed=False,
    ),
}

DEFAULT_PROFILE_CLI = "gfw-01.02.00"


class LegacyProfileWarning(UserWarning):
    """``SimulatedGoliath`` was built without ``profile=`` and uses ``legacy-spec``."""


@dataclass(frozen=True)
class SimEvent:
    """One entry of the simulator's own log (:attr:`SimulatedGoliath.events`).

    ``conn`` is the connection's accept order (from 0), ``slot`` its firmware
    slot. For ``sent``/``dropped``/``to-other-client`` events, ``data`` is the
    message *without* the NUL padding and ``slot`` is the slot it was routed to
    (for a direct reply: the requester's slot); ``note`` says ``slot``,
    ``tower`` or ``direct`` for ``sent`` events, and why for the others.
    """

    t: float  # seconds since the simulator was created
    kind: str
    conn: Optional[int] = None
    slot: Optional[int] = None
    data: bytes = b""
    note: str = ""

    def describe(self) -> str:
        where = "".join(
            f" {k}={v}" for k, v in (("conn", self.conn), ("slot", self.slot)) if v is not None
        )
        data = f" {self.data[:72]!r}" if self.data else ""
        note = f" ({self.note})" if self.note else ""
        return f"+{self.t:.3f}s {self.kind}{where}{data}{note}"


class ReceivedCommand(str):
    """A received command: equal to its text, plus where it came from.

    ``conn`` is the connection's accept order (from 0), ``slot`` the firmware
    slot it held. On a fresh robot with the vendor's connection order the two
    are the same.
    """

    conn: int
    slot: Optional[int]

    def __new__(cls, text: str, conn: int, slot: Optional[int]) -> "ReceivedCommand":
        obj = super().__new__(cls, text)
        obj.conn = conn
        obj.slot = slot
        return obj

    def __repr__(self) -> str:
        return f"ReceivedCommand({str.__repr__(self)}, conn={self.conn}, slot={self.slot})"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

_ANY = object()
_COORD_RE = re.compile(r"([XYZC])(-?\d+(?:\.\d+)?)")
_ATOI_RE = re.compile(rb"[ \t\n\v\f\r]*([+-]?\d+)")
_ATOF_RE = re.compile(rb"[ \t\n\v\f\r]*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")
_LEGACY_UPLOAD_RE = re.compile(rb"^GCODE UPLOAD;(\d+)")


def _atoi(field: bytes) -> int:
    m = _ATOI_RE.match(field)
    return int(m.group(1)) if m else 0


def _atof(field: bytes) -> float:
    m = _ATOF_RE.match(field)
    return float(m.group(1)) if m else 0.0


def _fw_text(v: Tuple[int, int, int]) -> str:
    return "{0:02d}.{1:02d}.{2:02d}".format(*v)


def _encode_position(pos: Sequence[float], line: int, *, full: bool, temperature: int,
                     t1_byte: int, t2_byte: int) -> bytes:
    """Position frame: '{' x y z angle (float32 LE) line (int32 LE) '}' (22 bytes),
    or the 58-byte form with temperature (int32) and two tower bytes at 21-26."""
    body = struct.pack("<ffffi", pos[0], pos[1], pos[2], pos[3], line)
    if full:
        # GUESS: bytes 27-56 of the full frame are undecoded; sent as zero.
        body += struct.pack("<i", temperature) + bytes([t1_byte, t2_byte]) + bytes(30)
    return b"{" + body + b"}"


def _encode_tower(tower: int, status_byte: int) -> bytes:
    return bytes([0x3C, 0xAA if tower == 1 else 0xBB, status_byte, 0x3E])


def _encode_gs19(t1: Tuple[int, int, int], t2: Tuple[int, int, int],
                 flags: bytes = bytes(11)) -> bytes:
    """19-byte 'G'...'S' global-status frame (vendor decoder; not sent by 01.02.00)."""
    return (b"G" + flags + bytes([t1[2], t1[1], t1[0], t2[2], t2[1], t2[0]]) + b"S")


def _print_ascii(line: str) -> None:
    """Print without ever failing on a narrow console encoding (e.g. cp1252)."""
    print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def _close_socket(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # not connected any more (the peer already closed): nothing to shut down
    sock.close()


class _Conn:
    """One accepted TCP connection."""

    def __init__(self, sock: socket.socket, index: int) -> None:
        self.sock = sock
        self.index = index
        self.slot: Optional[int] = None
        self.send_lock = threading.Lock()
        self.peer_closed = False  # the client closed it (slot may not be freed yet)
        self.robot_closed = False  # the simulator closed it
        self.line_buf = bytearray()  # line framing only
        self.legacy_upload_remaining = 0  # legacy-spec only
        self.legacy_upload_data = bytearray()


class _Upload:
    def __init__(self, expected: int, conn: int) -> None:
        self.expected = expected
        self.conn = conn
        self.data = bytearray()
        self.chunks = 0


# Options that shape firmware-profile output; legacy-spec does not support them.
_OUTPUT_DEFAULTS: Dict[str, object] = dict(
    unknown_command=None, connect_replay=None, upload_acks=None, global_status=None,
    cage="CLOSED", cage_reply="direct", cage_poll_interval=0.5, status_bytes=None,
    tower_bytes=(0x38, 0x34), index_calibration=28500, spindle_distance=12.34,
    position_frame="short", temperature=38, ssids=("SimNet",), sanity=(1,) * 15,
)


# --------------------------------------------------------------------------- #
# The simulator
# --------------------------------------------------------------------------- #

class SimulatedGoliath:
    """A fake Goliath robot on TCP. See the module docstring for the profiles."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        profile: Optional[str] = None,
        framing: str = "segment",
        firmware: Optional[Tuple[int, int, int]] = None,
        tower_firmware: Tuple[int, int, int] = (1, 2, 0),
        work_line_delay: float = 0.01,
        auto_start_work: bool = True,
        verbose: bool = False,
        # -- hazards ---------------------------------------------------------
        occupied_slots: Iterable[int] = (),
        slot_release_delay: float = 0.0,
        drop_on_accept: Iterable[int] = (),
        coalesce_window: float = 0.0,
        # -- firmware-profile output -------------------------------------------
        # The machine values below (calibration, cage, tower bytes, SSIDs,
        # sanity results, temperature) are placeholders, not readings of any
        # robot; only the message *formats* come from the firmware.
        unknown_command: Optional[str] = None,
        connect_replay: Optional[bool] = None,
        upload_acks: Optional[str] = None,
        global_status: Optional[str] = None,
        cage: str = "CLOSED",
        cage_reply: str = "direct",
        cage_poll_interval: float = 0.5,
        status_bytes: Optional[bytes] = None,
        tower_bytes: Tuple[int, int] = (0x38, 0x34),
        index_calibration: int = 28500,
        spindle_distance: float = 12.34,
        position_frame: str = "short",
        temperature: int = 38,
        ssids: Sequence[str] = ("SimNet",),
        sanity: Sequence[int] = (1,) * 15,
    ) -> None:
        implicit = profile is None
        if implicit:
            profile = "legacy-spec"
        if profile not in PROFILES:
            raise ValueError(f"profile must be one of {sorted(PROFILES)}, not {profile!r}")
        self.profile: Profile = PROFILES[profile]
        p = self.profile

        given = dict(
            unknown_command=unknown_command, connect_replay=connect_replay,
            upload_acks=upload_acks, global_status=global_status, cage=cage,
            cage_reply=cage_reply, cage_poll_interval=cage_poll_interval,
            status_bytes=status_bytes, tower_bytes=tuple(tower_bytes),
            index_calibration=index_calibration, spindle_distance=spindle_distance,
            position_frame=position_frame, temperature=temperature, ssids=tuple(ssids),
            sanity=tuple(sanity),
        )
        if not p.slot_routed:
            unsupported = sorted(k for k, v in given.items() if v != _OUTPUT_DEFAULTS[k])
            if unsupported:
                raise ValueError(
                    f"{', '.join(unsupported)} not supported by the legacy-spec profile; "
                    "use profile='gfw-01.02.00' or 'vendor-app-newer'"
                )

        _check(framing in ("segment", "line"), "framing must be 'segment' or 'line'")
        _check(unknown_command in (None, "silent", "error"),
               "unknown_command must be 'silent' or 'error'")
        _check(upload_acks in (None, "info0", "info0+info8", "vendor"),
               "upload_acks must be 'info0', 'info0+info8' or 'vendor'")
        _check(global_status in (None, "blob48", "gs19"),
               "global_status must be 'blob48' or 'gs19'")
        _check(cage in ("OPEN", "CLOSED"), "cage must be 'OPEN' or 'CLOSED'")
        _check(cage_reply in ("direct", "slot0"), "cage_reply must be 'direct' or 'slot0'")
        _check(position_frame in ("short", "full"), "position_frame must be 'short' or 'full'")
        occupied = tuple(occupied_slots)
        _check(all(isinstance(s, int) and 0 <= s < SLOT_COUNT for s in occupied),
               f"occupied_slots must be slot numbers 0-{SLOT_COUNT - 1}, not {occupied!r}")
        drops = frozenset(drop_on_accept)
        _check(all(isinstance(i, int) and i >= 0 for i in drops),
               "drop_on_accept must be accept indices (0 = first connection)")
        _check(status_bytes is None or len(status_bytes) == 10, "status_bytes must be 10 bytes")
        _check(len(given["tower_bytes"]) == 2 and all(0 <= b <= 255 for b in given["tower_bytes"]),
               "tower_bytes must be two byte values")
        _check(len(given["sanity"]) == 15, "sanity must hold 15 values")
        for name, value in (("work_line_delay", work_line_delay),
                            ("slot_release_delay", slot_release_delay),
                            ("coalesce_window", coalesce_window)):
            _check(value >= 0, f"{name} must be >= 0")
        _check(cage_poll_interval > 0, "cage_poll_interval must be > 0")

        self.host = host
        self.port = port
        self.framing = framing
        self.firmware: Tuple[int, int, int] = tuple(firmware) if firmware else p.firmware
        self.tower_firmware = tuple(tower_firmware)
        self.work_line_delay = work_line_delay
        self.auto_start_work = auto_start_work
        self.verbose = verbose
        self.occupied_slots = occupied
        self.slot_release_delay = slot_release_delay
        self.drop_on_accept = drops
        self.coalesce_window = coalesce_window
        self.unknown_command = unknown_command or ("silent" if p.slot_routed else "error")
        self.connect_replay = p.slot_routed if connect_replay is None else connect_replay
        self.upload_acks = upload_acks or p.upload_acks
        self.global_status = global_status or p.global_status
        self.cage = cage
        self.cage_reply = cage_reply
        self.cage_poll_interval = cage_poll_interval
        self.status_bytes = bytes(status_bytes) if status_bytes is not None else None
        self.tower_bytes = given["tower_bytes"]
        self.index_calibration = index_calibration
        self.spindle_distance = spindle_distance
        self.position_frame = position_frame
        self.temperature = temperature
        self.ssids = given["ssids"]
        self.sanity = given["sanity"]

        #: Every parsed command, in order, with its connection (for tests).
        self.received: List[ReceivedCommand] = []
        #: The simulator's own log (see :class:`SimEvent`).
        self.events: List[SimEvent] = []
        #: Exceptions raised inside simulator threads (a simulator bug).
        self.errors: List[BaseException] = []
        #: The last program received by ``GCODE UPLOAD`` (None before any).
        self.program: Optional[bytes] = None
        self.pos = [0.0, 0.0, 10.0, 0.0]  # x, y, z, angle

        self._lock = threading.RLock()  # slots, events, received, machine state
        self._cond = threading.Condition(self._lock)
        self._cmd_lock = threading.Lock()  # the firmware handles one buffer at a time
        self._t0 = time.monotonic()
        self._server: Optional[socket.socket] = None
        self._threads: List[threading.Thread] = []
        self._timers: List[threading.Timer] = []
        self._stop = threading.Event()
        self._slots: List[Union[None, str, _Conn]] = [None] * SLOT_COUNT
        for s in occupied:
            self._slots[s] = OTHER_CLIENT
        self._conns: List[_Conn] = []
        self._accepts = 0
        self._accept_done = 0
        # machine state (firmware profiles)
        self._upload: Optional[_Upload] = None
        self._upload_chunks = 0
        self._gcode_uploaded = False
        self._paired = False
        self._panel_detected = False  # panel detection is not simulated
        self._panel_size = (0.0, 0.0)
        self._jog = False
        self._working = False
        self._work_abort = threading.Event()
        self._work_wake = threading.Event()
        self._poll_stop: Optional[threading.Event] = None
        self._last_info: Optional[int] = None
        self._z_backlash = 0.0
        self._z_plate_offset = 0.0
        self._abort = False  # legacy-spec only

        self._handlers: Dict[str, Callable[[_Conn, List[bytes], bytes], Optional[bytes]]] = {
            "INFO": self._cmd_info,
            "GET GLOBAL SYS STATUS": self._cmd_global_status,
            "PRINT SYS REPORT": self._cmd_sys_report,
            "GET SPINDLE CAGE STATUS": self._cmd_cage_status,
            "READ INDEX CALIBRATION": self._cmd_read_index,
            "READ SPINDLE CALIB": self._cmd_read_spindle,
            "SET INDEX CALIBRATION VALUE": self._cmd_set_index,
            "SET RF CHANNELS": self._cmd_set_rf_channels,
            "SET PANEL SIZE": self._cmd_set_panel_size,
            "GET PANEL SIZE": self._cmd_get_panel_size,
            "GCODE UPLOAD": self._cmd_gcode_upload,
            "GCODE READ": self._cmd_gcode_read,
            "ENTER JOG": self._cmd_enter_jog,
            "EXIT JOG": self._cmd_exit_jog,
            "GOTO TARGET": self._cmd_goto_target,
            "POLL CAGE": self._cmd_poll_cage,
            "STOP POLL CAGE": self._cmd_stop_poll_cage,
            "GET SSIDS": self._cmd_get_ssids,
            "LAST STATUS": self._cmd_last_status,
            # handlers that only acknowledge (spec 09: INFO;0)
            "SET T2 POSITION": self._cmd_ack, "ENABLE SPINDLE": self._cmd_ack,
            "DISABLE SPINDLE": self._cmd_ack, "ENABLE ASPIRATION": self._cmd_ack,
            "DISABLE ASPIRATION": self._cmd_ack, "ENABLE MANUAL JOG": self._cmd_ack,
            "DISABLE MANUAL JOG": self._cmd_ack, "RESET NET KEY": self._cmd_ack,
            "ENABLE INFO LOG": self._cmd_ack,
            # handlers that send nothing (spec 09: "-" (nothing))
            "JOG STOP GOLIATH": self._cmd_nothing, "FREE Z AXIS": self._cmd_nothing,
            # vendor-app-newer only (dispatched only in that profile)
            "ABORT WORK": self._cmd_abort_work,
            "GCODE CONTINUE": self._cmd_gcode_continue,
            "SEND TCP POS": self._cmd_send_tcp_pos,
            "GET Z BACKLASH": self._cmd_get_z_backlash,
            "SET Z BACKLASH": self._cmd_set_z_backlash,
            "GET Z PLATE OFFSET": self._cmd_get_z_plate,
            "SET Z PLATE OFFSET": self._cmd_set_z_plate,
            "SET SPINDLE DISTANCE": self._cmd_set_spindle_distance,
            "DISCARD TWRS DISTANCE": self._cmd_ack,  # GUESS: plain acknowledgement
            "CMD SYS RESET": self._cmd_sys_reset,
        }

        if implicit:
            msg = (
                "SimulatedGoliath() without profile= falls back to the 'legacy-spec' model "
                "(every reply on the requesting connection, no 512-byte padding), which "
                "firmware GFW 01.02.00 contradicts; pass profile='gfw-01.02.00' "
                "(or 'vendor-app-newer', or 'legacy-spec' to silence this)"
            )
            warnings.warn(msg, LegacyProfileWarning, stacklevel=2)
            self._event("config", note="implicit default: " + msg)
        else:
            self._event("config", note=f"profile {p.name}: {p.basis}")
        if occupied:
            self._event("config", note=f"slots {list(occupied)} held by another client")

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> Tuple[str, int]:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform != "win32":
            # On Windows SO_REUSEADDR would let a second simulator share the port.
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(SLOT_COUNT + 2)
        srv.settimeout(0.2)
        self._server = srv
        self.port = srv.getsockname()[1]
        self._spawn(self._accept_loop, "sim-accept")
        return self.host, self.port

    def stop(self) -> None:
        self._stop.set()
        self._work_wake.set()
        if self._server is not None:
            # shutdown() wakes a thread blocked in accept() on Linux (close() alone
            # does not); other platforms reject it for a listening socket, and
            # the accept loop's 0.2 s timeout covers them.
            _close_socket(self._server)
        with self._lock:
            conns = list(self._conns)
            timers = list(self._timers)
            poll = self._poll_stop
        for t in timers:
            t.cancel()
        if poll is not None:
            poll.set()
        for c in conns:
            _close_socket(c.sock)
        for th in list(self._threads):
            if th is not threading.current_thread():
                th.join(timeout=2.0)

    def __enter__(self) -> "SimulatedGoliath":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- observation (for tests) ----------------------------------------------

    @property
    def accept_count(self) -> int:
        """Connections fully accepted (slotted, rejected or dropped) so far."""
        with self._lock:
            return self._accept_done

    def events_of(self, kind: str) -> List[SimEvent]:
        with self._lock:
            return [e for e in self.events if e.kind == kind]

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
        """Wait until ``predicate()`` is true (re-checked on every event)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                if predicate():
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 0.05))

    def wait_for_event(self, kind: str, timeout: float = 3.0, *, conn: object = _ANY,
                       slot: object = _ANY,
                       where: Optional[Callable[[SimEvent], bool]] = None) -> SimEvent:
        """Return the first logged event (past or future) that matches; raise on timeout."""
        found: List[SimEvent] = []

        def match() -> bool:
            for e in self.events:
                if (e.kind == kind and (conn is _ANY or e.conn == conn)
                        and (slot is _ANY or e.slot == slot) and (where is None or where(e))):
                    found.append(e)
                    return True
            return False

        if not self.wait_for(match, timeout):
            with self._lock:
                tail = "\n  ".join(e.describe() for e in self.events[-12:])
            raise TimeoutError(
                f"no {kind!r} event (conn={'any' if conn is _ANY else conn}, "
                f"slot={'any' if slot is _ANY else slot}) within {timeout}s; last events:\n  {tail}"
            )
        return found[0]

    def slot_holders(self) -> Tuple[Union[None, int, str], ...]:
        """Per slot: None (empty), a connection's accept index, or :data:`OTHER_CLIENT`."""
        with self._lock:
            return tuple(
                h.index if isinstance(h, _Conn) else h for h in self._slots
            )

    # -- hazards at run time ------------------------------------------------

    def drop(self, slot: int) -> None:
        """The robot closes the connection in ``slot`` (and frees the slot)."""
        with self._lock:
            holder = self._slots[slot]
        if not isinstance(holder, _Conn):
            raise ValueError(f"slot {slot} holds no simulator connection ({holder!r})")
        self._robot_drop(holder, "drop() called")

    def occupy_slot(self, slot: int) -> None:
        """Another client (outside the test) takes the free ``slot``."""
        with self._lock:
            if self._slots[slot] is not None:
                raise ValueError(f"slot {slot} is not free")
            self._slots[slot] = OTHER_CLIENT
            self._event("other-client-slot", slot=slot, note="occupied by another client")

    def release_slot(self, slot: int) -> None:
        """The other client holding ``slot`` disconnects."""
        with self._lock:
            if self._slots[slot] is not OTHER_CLIENT:
                raise ValueError(f"slot {slot} is not held by another client")
            self._slots[slot] = None
            self._event("slot-freed", slot=slot, note="the other client left")

    # -- internals: logging -------------------------------------------------

    def _event(self, kind: str, *, conn: Optional[int] = None, slot: Optional[int] = None,
               data: bytes = b"", note: str = "") -> SimEvent:
        ev = SimEvent(time.monotonic() - self._t0, kind, conn, slot, bytes(data), note)
        with self._cond:
            self.events.append(ev)
            self._cond.notify_all()
        if self.verbose:
            _print_ascii(f"[sim] {ev.describe()}")
        return ev

    def _record(self, text: str, conn: _Conn) -> None:
        with self._cond:
            self.received.append(ReceivedCommand(text, conn.index, conn.slot))
            self._cond.notify_all()
        if self.verbose:
            _print_ascii(f"[sim] <- conn={conn.index} slot={conn.slot} {text!r}")

    def _spawn(self, target: Callable[..., None], name: str, *args: object) -> None:
        def run() -> None:
            try:
                target(*args)
            except Exception as exc:  # a simulator bug: make it loud, then re-raise
                self.errors.append(exc)
                self._event("sim-error", note=f"{name}: {traceback.format_exc()}")
                raise

        th = threading.Thread(target=run, name=name, daemon=True)
        with self._lock:
            self._threads.append(th)
        th.start()

    # -- internals: connections and slots -----------------------------------

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                sock, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if self._stop.is_set():
                    return
                self._event("accept-failed", note=repr(exc))
                time.sleep(0.05)
                continue
            self._on_accept(sock)

    def _on_accept(self, sock: socket.socket) -> None:
        with self._lock:
            conn = _Conn(sock, self._accepts)
            self._accepts += 1
            slot = next((i for i, h in enumerate(self._slots) if h is None), None)
            if slot is None:
                self._event("reject-full", conn=conn.index,
                            note=f"all {SLOT_COUNT} slots taken: accepted and closed at once")
                self._accept_done += 1
            else:
                conn.slot = slot
                self._slots[slot] = conn
                self._conns.append(conn)
                self._event("accept", conn=conn.index, slot=slot)
        if slot is None:
            _close_socket(sock)
            return
        if conn.index in self.drop_on_accept:
            self._robot_drop(conn, "drop_on_accept")
            with self._lock:
                self._accept_done += 1
            return
        sock.settimeout(5.0)  # bounds a blocked sendall; receives are select()ed
        self._spawn(self._serve, f"sim-conn{conn.index}", conn)
        with self._lock:
            self._accept_done += 1
        if self.profile.slot_routed and self.connect_replay and slot == 1:
            self._connect_replay()

    def _serve(self, conn: _Conn) -> None:
        sock = conn.sock
        reason = "peer closed"
        try:
            while not self._stop.is_set():
                try:
                    readable, _, _ = select.select([sock], [], [], 0.2)
                    if not readable:
                        continue
                    data = sock.recv(4096)
                except (OSError, ValueError) as exc:
                    reason = f"receive failed: {exc!r}"
                    break
                if not data:
                    break
                eof = False
                if self.coalesce_window > 0:
                    data, eof = self._coalesce(conn, data)
                try:
                    self._on_chunk(conn, data)
                except Exception as exc:
                    # A simulator bug: close the connection so the client sees a
                    # failure, then let _spawn log and re-raise it.
                    self._robot_drop(conn, f"simulator error: {exc!r}")
                    raise
                if eof:
                    break
        finally:
            self._on_peer_gone(conn, reason)

    def _coalesce(self, conn: _Conn, data: bytes) -> Tuple[bytes, bool]:
        buf = bytearray(data)
        reads = 1
        eof = False
        deadline = time.monotonic() + self.coalesce_window
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            readable, _, _ = select.select([conn.sock], [], [], remaining)
            if not readable:
                break
            more = conn.sock.recv(4096)
            if not more:
                eof = True
                break
            buf.extend(more)
            reads += 1
        if reads > 1:
            self._event("coalesced", conn=conn.index, slot=conn.slot, data=bytes(buf),
                        note=f"{reads} reads within {self.coalesce_window}s -> one buffer")
        return bytes(buf), eof

    def _on_peer_gone(self, conn: _Conn, reason: str) -> None:
        with self._lock:
            already = conn.robot_closed or conn.peer_closed or self._stop.is_set()
            if not already:
                conn.peer_closed = True
        if already:
            return
        self._event("peer-closed", conn=conn.index, slot=conn.slot, note=reason)
        _close_socket(conn.sock)
        if self.slot_release_delay > 0:
            timer = threading.Timer(self.slot_release_delay, self._free_slot,
                                    args=(conn, f"released {self.slot_release_delay}s after close"))
            timer.daemon = True
            with self._lock:
                self._timers.append(timer)
            timer.start()
        else:
            self._free_slot(conn, "freed on close (receive returned <= 0)")

    def _free_slot(self, conn: _Conn, note: str) -> None:
        with self._lock:
            if conn.slot is not None and self._slots[conn.slot] is conn:
                self._slots[conn.slot] = None
                self._event("slot-freed", conn=conn.index, slot=conn.slot, note=note)

    def _robot_drop(self, conn: _Conn, note: str) -> None:
        with self._lock:
            if conn.robot_closed:
                return
            conn.robot_closed = True
            if conn.slot is not None and self._slots[conn.slot] is conn:
                self._slots[conn.slot] = None
            self._event("robot-dropped", conn=conn.index, slot=conn.slot, note=note)
        _close_socket(conn.sock)

    # -- internals: output paths --------------------------------------------

    def _write(self, conn: _Conn, wire: bytes, logical: bytes, how: str, slot: Optional[int]) -> bool:
        try:
            with conn.send_lock:
                conn.sock.sendall(wire)
        except OSError as exc:
            self._event("send-failed", conn=conn.index, slot=slot, data=logical,
                        note=f"{how}: {exc!r}")
            return False
        self._event("sent", conn=conn.index, slot=slot, data=logical, note=how)
        return True

    def _route(self, slot: int, wire: bytes, logical: bytes, how: str) -> None:
        """send_slot / the tower sender: look the slot up, send or drop."""
        with self._lock:
            holder = self._slots[slot]
        if holder is None:
            self._event("dropped", slot=slot, data=logical,
                        note=f"slot {slot} empty: send_slot has no fallback")
        elif holder is OTHER_CLIENT:
            self._event("to-other-client", slot=slot, data=logical, note=how)
        elif isinstance(holder, _Conn) and (holder.peer_closed or holder.robot_closed):
            self._event("dropped", conn=holder.index, slot=slot, data=logical,
                        note=f"slot {slot}: peer closed, slot not yet released")
        else:
            assert isinstance(holder, _Conn)
            self._write(holder, wire, logical, how, slot)

    def _slot_message(self, slot: int, msg: bytes) -> None:
        """A 512-byte block: zeroed buffer, message, send all 512 bytes."""
        if len(msg) > SLOT_BLOCK - 1:  # snprintf into 512 bytes keeps 511 + NUL
            self._event("truncated", slot=slot, data=msg, note="longer than 511 bytes")
            msg = msg[: SLOT_BLOCK - 1]
        self._route(slot, msg + bytes(SLOT_BLOCK - len(msg)), msg, "slot")

    def _info(self, code: int, *params: object) -> None:
        text = "INFO;%d" % code + "".join(f";{p}" for p in params) + "\n"
        with self._lock:
            self._last_info = code
        self._slot_message(1, text.encode("latin-1"))

    def _err(self, code: int) -> None:
        self._slot_message(1, b"ERR;%d\n" % code)

    def _printf(self, text: bytes) -> None:
        self._slot_message(0, text)

    def _tower_frames(self) -> None:
        for tower, byte in ((1, self.tower_bytes[0]), (2, self.tower_bytes[1])):
            frame = _encode_tower(tower, byte)
            self._route(0, frame, frame, "tower")  # exactly 4 bytes, not padded

    def _position_block(self, line: int) -> None:
        with self._lock:
            pos = list(self.pos)
        frame = _encode_position(
            pos, line, full=self.position_frame == "full", temperature=self.temperature,
            t1_byte=self.tower_bytes[0], t2_byte=self.tower_bytes[1],
        )
        self._route(2, frame + bytes(SLOT_BLOCK - len(frame)), frame, "slot")

    def _direct(self, conn: _Conn, data: bytes) -> None:
        """The common tail: send the pending direct reply to the requester."""
        if not data:
            return  # set_response does nothing for length 0
        if len(data) > DIRECT_REPLY_MAX:
            self._event("truncated", conn=conn.index, slot=conn.slot, data=data,
                        note=f"direct reply longer than {DIRECT_REPLY_MAX} bytes")
            data = data[:DIRECT_REPLY_MAX]
        if conn.robot_closed or conn.peer_closed:
            self._event("dropped", conn=conn.index, slot=conn.slot, data=data,
                        note="direct reply: requester already closed")
            return
        if not self._write(conn, data, data, "direct", conn.slot):
            self._free_slot(conn, "direct reply failed to send (remove_client)")

    # -- internals: firmware-profile command processing ----------------------

    def _on_chunk(self, conn: _Conn, data: bytes) -> None:
        try:
            if self.profile.slot_routed:
                self._fw_chunk(conn, data)
            else:
                self._legacy_chunk(conn, data)
        finally:
            self._event("done", conn=conn.index, slot=conn.slot, data=data)

    def _upload_active(self) -> bool:
        with self._lock:
            return self._upload is not None

    def _fw_chunk(self, conn: _Conn, data: bytes) -> None:
        if self.framing == "segment" or self._upload_active():
            self._fw_segment(conn, data)
            return
        # Line framing: a hypothetical robot that waits for '\n'. Not 01.02.00.
        conn.line_buf.extend(data)
        while True:
            if self._upload_active():
                rest = bytes(conn.line_buf)
                conn.line_buf.clear()
                if rest:
                    self._fw_segment(conn, rest)
                return
            i = conn.line_buf.find(b"\n")
            if i < 0:
                return
            line = bytes(conn.line_buf[:i])
            del conn.line_buf[: i + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if line:
                self._fw_segment(conn, line)

    def _fw_segment(self, conn: _Conn, seg: bytes) -> None:
        with self._cmd_lock:
            with self._lock:
                upload = self._upload
            if upload is not None:
                self._upload_chunk(conn, upload, seg)
                return
            text = seg.split(b"\x00", 1)[0]  # copied and NUL-terminated: a C string
            fields = [f for f in text.split(b";") if f]  # strtok(buf, ";") semantics
            name = fields[0].upper().decode("latin-1") if fields else ""
            args = fields[1:]
            self._record(text.decode("latin-1"), conn)
            if name and name in self.profile.dispatched:
                handler = self._handlers.get(name)
                if handler is None:
                    reply = self._unmodelled(conn, name, text)
                else:
                    reply = handler(conn, args, text)
                if reply:
                    self._direct(conn, reply)
            elif name and name in self.profile.not_dispatched:
                self._event("not-dispatched", conn=conn.index, slot=conn.slot, data=text,
                            note="in the command table, outside the dispatch table: "
                                 "no effect, no reply")
            else:
                self._unknown(conn, text)

    def _unknown(self, conn: _Conn, text: bytes) -> None:
        if self.unknown_command == "error":
            self._event("unknown-command", conn=conn.index, slot=conn.slot, data=text,
                        note=f"not in the {self.profile.name} command table; "
                             "policy 'error': ERR;1 (GUESS)")
            self._err(1)  # GUESS: the reply to an unknown command is not established
        else:
            self._event("unknown-command", conn=conn.index, slot=conn.slot, data=text,
                        note=f"not in the {self.profile.name} command table; "
                             "policy 'silent': no reply (static reading of 01.02.00)")

    def _unmodelled(self, conn: _Conn, name: str, text: bytes) -> Optional[bytes]:
        sends_info0 = name not in _NO_INFO0_HANDLERS
        self._event("unmodelled", conn=conn.index, slot=conn.slot, data=text,
                    note="table command not simulated (the vendor app never sends it); "
                         + ("only its INFO;0 is sent, no effect" if sends_info0
                            else "its handler sends no INFO;0; nothing is sent"))
        if sends_info0:
            self._info(0)  # GUESS: assumes the handler's precondition passes
        return None

    # -- handlers (each returns the direct reply, if any) --------------------

    def _identity_line(self) -> bytes:
        # Format from the firmware: INFO;%d;%s;%d;%d;%04X%04X%04X\n with the
        # project name "GFW xx.yy.zz" padded with spaces.
        # GUESS: the padding width, the HW/SW numbers and the unique id.
        prj = "GFW %s   " % _fw_text(self.firmware)
        return b"INFO;34;%s;%d;%d;%04X%04X%04X\n" % (prj.encode("ascii"), 1, 2, 0, 0, 1)

    def _cmd_info(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        self._slot_message(1, self._identity_line())  # own builder, not send_info
        return None

    def _status_bytes(self) -> bytes:
        if self.status_bytes is not None:
            return self.status_bytes
        # GUESS: value encodings. Only pairing==3 (-> replay INFO;4), pd_status==3
        # (-> INFO;6) and gcode_uploaded!=0 (-> INFO;8) are corroborated.
        with self._lock:
            paired, panel = self._paired, self._panel_detected
            uploaded, working = self._gcode_uploaded, self._working
        return bytes([
            3 if paired else 0, 1 if paired else 0, 1 if paired else 0,
            3 if panel else 0, 1 if panel else 0, 1 if uploaded else 0,
            1 if working else 0, 0, 0, 1,
        ])

    def _cmd_global_status(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        self._info(0)
        if self.global_status == "blob48":
            return STATUS_BLOB_HEAD + self._status_bytes() + STATUS_BLOB_TALE
        # GUESS: the 19-byte frame's flag bytes (undecoded by anyone) are zero, and
        # it is sent as a direct reply like the blob.
        return _encode_gs19(self.tower_firmware, self.tower_firmware)

    def _cmd_sys_report(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        self._info(40, *self.sanity, "END")  # GUESS: the 15 values (1 = ok)
        return None

    def _cmd_cage_status(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        self._info(0)
        line = b"CAGE STATUS: %s\n" % self.cage.encode("ascii")
        if self.cage_reply == "slot0":
            # The firmware brief placed this reply on slot 0 (tcp_printf).
            self._printf(line)
            return None
        # Spec 09 (single pass): snprintf(reply, 20, ...) to the requester, so
        # "CAGE STATUS: CLOSED\n" loses its '\n'.
        return line[:19]

    def _cmd_read_index(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        self._printf(b"cfg read index : %d" % self.index_calibration)  # no '\n' (firmware)
        self._info(0)  # GUESS: order relative to the printf
        return None

    def _cmd_read_spindle(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        self._printf(b"spindle distance: %.2f" % self.spindle_distance)  # no '\n' (firmware)
        self._info(0)  # GUESS: order relative to the printf
        return None

    def _cmd_set_index(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        with self._lock:
            self.index_calibration = _atoi(args[0]) if args else 0  # GUESS: no range check
        self._info(0)
        return None

    def _cmd_set_rf_channels(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        self._info(0)
        # GUESS: the pairing sequence. The handler only acknowledges; pairing runs
        # in the firmware's state machine. The vendor app waits for INFO;3,
        # tower frames and INFO;4, which the simulator sends at once.
        self._info(3)
        self._tower_frames()
        self._info(4)
        with self._lock:
            self._paired = True
        return None

    def _cmd_set_panel_size(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        x = _atof(args[0]) if len(args) > 0 else 0.0
        y = _atof(args[1]) if len(args) > 1 else 0.0
        with self._lock:
            self._panel_size = (x, y)
        self._info(0)
        return None

    def _cmd_get_panel_size(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        self._info(0)
        with self._lock:
            x, y = self._panel_size
        return b"xmax=%.4f \n\rymax=%.4f" % (x, y)  # format from the firmware; unterminated

    def _cmd_gcode_upload(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        field = args[0] if args else b""
        m = _ATOI_RE.match(field)
        size = int(m.group(1)) if m else 0
        extra = field[m.end():] if m else field
        if len(args) > 1:
            extra += b";" + b";".join(args[1:])
        with self._lock:
            self._upload = _Upload(size, conn.index)
        note = f"expecting {size} bytes; parser off until then, for every connection"
        if size <= 0:
            note += " (GUESS: with size <= 0 the next buffer completes the upload)"
        self._event("upload-start", conn=conn.index, slot=conn.slot, data=text, note=note)
        if extra:
            self._event("upload-header-extra-ignored", conn=conn.index, slot=conn.slot,
                        data=extra, note="the handler reads only the size from its buffer")
        if self.upload_acks == "vendor":
            self._info(7)  # GUESS: vendor app expects ENTER GCODE_UPLOAD
        return None  # the 01.02.00 header handler sends nothing itself

    def _upload_chunk(self, conn: _Conn, upload: _Upload, seg: bytes) -> None:
        upload.data.extend(seg)
        upload.chunks += 1
        self._event("upload-data", conn=conn.index, slot=conn.slot, data=seg,
                    note=f"{len(upload.data)}/{upload.expected} bytes")
        if len(upload.data) < upload.expected:
            return
        # GUESS: the firmware's two end-of-upload checks are not decoded; the
        # simulator completes when the announced size has arrived.
        program = bytes(upload.data)
        with self._lock:
            self._upload = None
            self.program = program
            self._upload_chunks = upload.chunks
            self._gcode_uploaded = True
        if len(program) > upload.expected:
            self._event("upload-overrun", data=program[upload.expected:],
                        note="GUESS: more bytes than announced; all are kept")
        self._event("upload-complete", data=program,
                    note=f"{len(program)} bytes in {upload.chunks} buffers")
        self._info(0)  # firmware: leaves upload mode and sends INFO;0
        if self.upload_acks in ("info0+info8", "vendor"):
            self._info(8)  # GUESS: not established on 01.02.00
        if self.upload_acks == "vendor":
            self._info(9)  # GUESS
        if self.auto_start_work:
            self._start_work(program)

    def _cmd_gcode_read(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        with self._lock:
            chunks = self._upload_chunks
        return b"count: %d" % chunks  # format from the firmware; GUESS: what it counts

    def _cmd_enter_jog(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        with self._lock:
            self._jog = True
        self._info(0)
        self._info(35)  # GUESS: the vendor app waits for JOG_MODE
        return None

    def _cmd_exit_jog(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        with self._lock:
            was = self._jog
            self._jog = False
        if was:
            self._info(0)
        else:
            self._err(1)  # GUESS: the handler's precondition ("in jog")
        return None

    def _cmd_goto_target(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        vals = [_atof(a) for a in args[:4]] + [0.0] * (4 - min(len(args), 4))
        with self._lock:
            self.pos = vals
        self._info(0)
        self._info(32)  # GUESS: target reached at once
        return None

    def _cmd_poll_cage(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        self._info(0)
        with self._lock:
            if self._poll_stop is None:
                self._poll_stop = threading.Event()
                stop = self._poll_stop
            else:
                stop = None
        if stop is not None:
            self._spawn(self._poll_cage_loop, "sim-poll-cage", stop)
        return None

    def _poll_cage_loop(self, stop: threading.Event) -> None:
        while not stop.wait(self.cage_poll_interval) and not self._stop.is_set():
            self._printf(b"CAGE STATUS: %s\n" % self.cage.encode("ascii"))  # padded, with '\n'

    def _cmd_stop_poll_cage(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        with self._lock:
            stop, self._poll_stop = self._poll_stop, None
        if stop is not None:
            stop.set()
        self._info(0)
        return None

    def _cmd_get_ssids(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        self._slot_message(1, b"INFO;31;START\n")
        for ssid in self.ssids:  # GUESS: the networks found
            self._slot_message(1, b"INFO;31;%s\n" % ssid.encode("ascii", "replace"))
        self._slot_message(1, b"INFO;31;END\n")
        return None

    def _cmd_last_status(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        with self._lock:
            last = self._last_info
        if last is None:
            self._event("unmodelled", conn=conn.index, slot=conn.slot, data=text,
                        note="LAST STATUS before any INFO: nothing sent (GUESS)")
        else:
            self._info(last)  # re-sends the most recent INFO code (spec 09, single pass)
        return None

    def _cmd_ack(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        self._info(0)
        return None

    def _cmd_nothing(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        return None

    # vendor-app-newer only: every reply below is a GUESS (no firmware image).

    def _cmd_abort_work(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        with self._lock:
            working = self._working
        if not working:
            self._err(1)  # GUESS
            return None
        self._info(0)  # GUESS: acknowledgement, before the work thread reacts
        self._work_abort.set()
        self._work_wake.set()
        return None

    def _cmd_gcode_continue(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        if args and _atoi(args[0]) == 0:
            with self._lock:
                self._gcode_uploaded = False  # GUESS: discard the loaded program
        return None  # GUESS: no reply (the vendor app waits for none)

    def _cmd_send_tcp_pos(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        self._position_block(0)  # GUESS: one frame on slot 2, line 0
        return None

    def _cmd_get_z_backlash(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        # GUESS: printf on slot 0 like the 01.02.00 calibration reads; the vendor
        # app parses the "Z const offset:" prefix.
        self._printf(b"Z const offset: %.2f" % self._z_backlash)
        self._info(0)
        return None

    def _cmd_set_z_backlash(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        self._z_backlash = _atof(args[0]) if args else 0.0
        self._info(0)  # GUESS
        return None

    def _cmd_get_z_plate(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        self._printf(b"Z plate offset: %.2f" % self._z_plate_offset)  # GUESS: text unknown
        self._info(0)
        return None

    def _cmd_set_z_plate(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        self._z_plate_offset = _atof(args[0]) if args else 0.0
        self._info(0)  # GUESS (the vendor dialog later waits for INFO;43)
        return None

    def _cmd_set_spindle_distance(self, conn: _Conn, args: List[bytes],
                                  text: bytes) -> Optional[bytes]:
        self.spindle_distance = _atof(args[0]) if args else 0.0
        self._info(0)  # GUESS
        return None

    def _cmd_sys_reset(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        # GUESS: a reboot drops every connection and forgets the session state.
        self._event("reboot", conn=conn.index, slot=conn.slot, note="CMD SYS RESET")
        with self._lock:
            conns = [c for c in self._conns if not (c.robot_closed or c.peer_closed)]
            self._upload = None
            self._paired = self._jog = False
        self._work_abort.set()
        self._work_wake.set()
        for c in conns:
            self._robot_drop(c, "reboot")
        return None

    # -- connect-time replay, work cycle --------------------------------------

    def _connect_replay(self) -> None:
        # GUESS (trigger): spec 09 runs the replay for the socket whose WINC1500
        # descriptor is 2, in some machine states. The simulator runs it whenever
        # a connection takes slot 1 (the 2nd connection, in the vendor's order).
        with self._cmd_lock:
            self._slot_message(1, self._identity_line())
            with self._lock:
                uploaded, panel, paired = self._gcode_uploaded, self._panel_detected, self._paired
            if uploaded:
                self._info(8)
            if panel:
                self._info(6)
            if paired:
                self._info(4)

    def _start_work(self, program: bytes) -> None:
        with self._lock:
            self._working = True
        self._work_abort.clear()
        self._work_wake.clear()
        self._spawn(self._run_work, "sim-work", program)

    def _run_work(self, program: bytes) -> None:
        # GUESS: the whole work cycle (INFO codes, timing, one position frame per
        # line). The 01.02.00 work state machine was not traced.
        self._event("work-start", note=f"{len(program)} bytes")
        self._info(15)
        self._info(17)
        outcome = "completed"
        for n, line in enumerate(program.decode("latin-1").splitlines(), start=1):
            if self._stop.is_set():
                outcome = "simulator stopped"
                break
            if self._work_abort.is_set():
                outcome = "aborted"
                break
            with self._lock:
                for axis, value in _COORD_RE.findall(line.upper()):
                    self.pos["XYZC".index(axis)] = float(value)
            self._position_block(n)
            if self.work_line_delay:
                self._work_wake.wait(self.work_line_delay)
        if outcome == "completed" and self._work_abort.is_set():
            outcome = "aborted"
        with self._lock:
            self._working = False
        if outcome == "completed":
            self._info(18)
            self._info(16)
            self._info(23)
        elif outcome == "aborted":
            self._info(18)  # GUESS: what an abort sends
            self._info(16)
        self._event("work-end", note=outcome)

    # -- legacy-spec: the pre-correction model, unchanged ---------------------
    # Every reply in this section is a GUESS from before the firmware was
    # disassembled, and several are now known to be wrong (routing, padding,
    # newlines, the 19-byte status frame, ERR;1 for unknown commands).

    def _legacy_chunk(self, conn: _Conn, chunk: bytes) -> None:
        def send(data: bytes) -> None:
            self._write(conn, data, data, "direct", conn.slot)

        if conn.legacy_upload_remaining > 0:
            take = chunk[: conn.legacy_upload_remaining]
            conn.legacy_upload_data.extend(take)
            conn.legacy_upload_remaining -= len(take)
            if conn.legacy_upload_remaining == 0:
                self._legacy_finish_upload(conn, bytes(conn.legacy_upload_data))
            return

        commands: List[bytes] = []
        if self.framing == "segment":
            commands = [chunk]
        else:
            conn.line_buf.extend(chunk)
            while b"\n" in conn.line_buf:
                line, _, rest = bytes(conn.line_buf).partition(b"\n")
                conn.line_buf[:] = rest
                commands.append(line)

        for raw in commands:
            m = _LEGACY_UPLOAD_RE.match(raw)
            if m:
                # GUESS: lenient on purpose; anything after the header (+ CR/LF)
                # counts as payload.
                size = int(m.group(1))
                self._record(raw[: m.end()].decode("ascii", "replace"), conn)
                send(b"INFO;7\n")
                rest = raw[m.end():]
                if rest.startswith(b"\r\n"):
                    rest = rest[2:]
                elif rest[:1] in (b"\n", b"\r"):
                    rest = rest[1:]
                conn.legacy_upload_data = bytearray(rest[:size])
                conn.legacy_upload_remaining = size - len(conn.legacy_upload_data)
                if conn.legacy_upload_remaining == 0:
                    self._legacy_finish_upload(conn, bytes(conn.legacy_upload_data))
                continue
            cmd = raw.decode("ascii", "replace").strip("\r\n").strip()
            if not cmd:
                continue
            self._record(cmd, conn)
            if cmd == "CMD SYS RESET":
                self._robot_drop(conn, "legacy-spec CMD SYS RESET: reboot")
                return
            for reply in self._legacy_handle(cmd):
                send(reply)

    def _legacy_position_frame(self, line: int) -> bytes:
        with self._lock:
            pos = list(self.pos)
        return _encode_position(pos, line, full=True, temperature=38, t1_byte=0x38, t2_byte=0x34)

    def _legacy_handle(self, cmd: str) -> List[bytes]:
        parts = cmd.split(";")
        head = parts[0]
        fw = _fw_text(self.firmware)
        if cmd == "INFO":
            return ["INFO;34;GFW {0};HW 1.0;SW {0};SN SIM0001\n".format(fw).encode()]
        if cmd == "GET GLOBAL SYS STATUS":
            return [_encode_gs19(self.tower_firmware, self.tower_firmware)]
        if cmd == "PRINT SYS REPORT":
            return [b"INFO;40;" + b";".join([b"1"] * 15) + b";END\n"]  # GUESS: values
        if cmd == "GET SPINDLE CAGE STATUS":
            return [b"CAGE STATUS: CLOSED\n"]  # pre-correction shape
        if cmd == "GET Z BACKLASH":
            return [b"Z const offset: 0.00\n"]  # GUESS
        if cmd == "READ INDEX CALIBRATION":
            return [b"cfg read index :28500\n"]  # pre-correction shape
        if cmd == "READ SPINDLE CALIB":
            return [b"spindle distance: 0.00\n"]  # pre-correction shape
        if cmd == "GET Z PLATE OFFSET":
            return [b"Z plate offset: 0.00\n"]  # GUESS: reply text unknown
        if cmd == "SEND TCP POS":
            return [self._legacy_position_frame(0)]  # GUESS: full vs short frame
        if head == "SET RF CHANNELS" and len(parts) == 3:
            return [
                b"INFO;3\n", _encode_tower(1, 0x38), _encode_tower(2, 0x34), b"INFO;4\n",
            ]
        if head in ("SET T2 POSITION", "SET PANEL SIZE", "GCODE CONTINUE",
                    "ENABLE SPINDLE", "DISABLE SPINDLE", "ENABLE ASPIRATION",
                    "DISABLE ASPIRATION", "JOG STOP GOLIATH", "EXIT JOG",
                    "ENABLE MANUAL JOG", "DISABLE MANUAL JOG", "FREE Z AXIS",
                    "SET Z BACKLASH", "SET Z PLATE OFFSET", "SET INDEX CALIBRATION VALUE",
                    "SET SPINDLE DISTANCE", "DISCARD TWRS DISTANCE"):
            return []
        if cmd == "ENTER JOG":
            return [b"INFO;35\n"]
        if head == "GOTO TARGET" and len(parts) == 5:
            try:
                vals = [float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])]
            except ValueError:
                return [b"ERR;1\n"]
            with self._lock:
                self.pos = vals
            return [b"INFO;32\n"]
        if cmd == "ABORT WORK":
            self._abort = True
            return [b"INFO;18\n"]
        return [b"ERR;1\n"]  # CMD_NOT_EXECUTED

    def _legacy_finish_upload(self, conn: _Conn, payload: bytes) -> None:
        with self._lock:
            self.program = payload
        self._write(conn, b"INFO;8\n", b"INFO;8\n", "direct", conn.slot)
        self._write(conn, b"INFO;9\n", b"INFO;9\n", "direct", conn.slot)
        if self.auto_start_work:
            self._abort = False
            self._spawn(self._legacy_run_work, "sim-work", conn, payload)

    def _legacy_run_work(self, conn: _Conn, payload: bytes) -> None:
        def send(data: bytes) -> None:
            self._write(conn, data, data, "direct", conn.slot)

        self._event("work-start", note=f"{len(payload)} bytes (legacy-spec)")
        send(b"INFO;15\n")
        send(b"INFO;17\n")
        for n, line in enumerate(payload.decode("ascii", "replace").splitlines(), start=1):
            if self._stop.is_set() or self._abort:
                self._event("work-end", note="aborted" if self._abort else "simulator stopped")
                return
            with self._lock:
                for axis, value in _COORD_RE.findall(line.upper()):
                    self.pos["XYZC".index(axis)] = float(value)
            send(self._legacy_position_frame(n))
            if self.work_line_delay:
                time.sleep(self.work_line_delay)
        send(b"INFO;18\n")
        send(b"INFO;16\n")
        send(b"INFO;23\n")
        self._event("work-end", note="completed")


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _nonneg_float(text: str) -> float:
    value = float(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, not {text}")
    return value


def _pos_float(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, not {text}")
    return value


def _int_list(text: str) -> Tuple[int, ...]:
    try:
        return tuple(int(p) for p in text.split(",") if p.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected comma-separated integers, not {text!r}")


def _build_parser() -> argparse.ArgumentParser:
    profiles = "\n".join(f"  {p.name:<17} {p.basis}" for p in PROFILES.values())
    ap = argparse.ArgumentParser(
        prog="python -m goliath_link.sim",
        description="Run a fake Goliath robot on TCP. It is a simulator, not the robot.",
        epilog="profiles:\n" + profiles,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080, help="0 picks a free port")
    ap.add_argument("--profile", choices=sorted(PROFILES), default=DEFAULT_PROFILE_CLI,
                    help=f"firmware to simulate (default {DEFAULT_PROFILE_CLI})")
    ap.add_argument("--framing", choices=["segment", "line"], default="segment",
                    help="segment = one receive buffer is one command (the firmware); "
                         "line = a hypothetical robot that waits for '\\n'")
    ap.add_argument("--unknown-command", choices=["silent", "error"], default=None,
                    help="reply to commands not in the table (default silent; error = ERR;1)")
    ap.add_argument("--cage", choices=["OPEN", "CLOSED"], default="CLOSED")
    ap.add_argument("--no-connect-replay", action="store_true",
                    help="do not push INFO;34 when a connection takes slot 1")
    ap.add_argument("--occupied-slots", type=_int_list, default=(),
                    help="slots held by another client, e.g. 0,1")
    ap.add_argument("--slot-release-delay", type=_nonneg_float, default=0.0,
                    help="seconds a closed connection keeps its slot")
    ap.add_argument("--drop-on-accept", type=_int_list, default=(),
                    help="accept indices the robot closes at once, e.g. 1")
    ap.add_argument("--coalesce-window", type=_nonneg_float, default=0.0,
                    help="merge reads within this many seconds into one command buffer")
    ap.add_argument("--line-delay", type=_nonneg_float, default=0.05,
                    help="seconds per G-code line in the simulated work cycle")
    ap.add_argument("--duration", type=_pos_float, default=None,
                    help="stop after this many seconds (default: until Ctrl+C)")
    ap.add_argument("--quiet", action="store_true", help="do not print the event log")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)
    kwargs: Dict[str, object] = dict(
        profile=args.profile, framing=args.framing, work_line_delay=args.line_delay,
        verbose=not args.quiet, occupied_slots=args.occupied_slots,
        slot_release_delay=args.slot_release_delay, drop_on_accept=args.drop_on_accept,
        coalesce_window=args.coalesce_window,
    )
    if args.unknown_command is not None:
        kwargs["unknown_command"] = args.unknown_command
    if args.cage != "CLOSED":
        kwargs["cage"] = args.cage
    if args.no_connect_replay:
        kwargs["connect_replay"] = False
    try:
        sim = SimulatedGoliath(args.host, args.port, **kwargs)  # type: ignore[arg-type]
    except ValueError as exc:
        ap.error(str(exc))
    host, port = sim.start()
    _print_ascii(f"Simulated Goliath (profile {sim.profile.name}) listening on {host}:{port} "
                 f"(framing={sim.framing}). Ctrl+C to stop.")
    _print_ascii(f"Basis: {sim.profile.basis}.")
    _print_ascii("This is a SIMULATOR, not the robot: replies marked GUESS in sim.py "
                 "are stand-ins.")
    try:
        if args.duration is not None:
            time.sleep(args.duration)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
