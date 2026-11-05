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

Setup phases (``setup_model=True``)
===================================

Off by default: without it the simulator keeps its earlier, simpler model
(``SET RF CHANNELS`` pairs with any channel pair at once, no encoder report,
panel detection never runs, an upload needs no ``SET PANEL SIZE``), and says
so in a ``config`` event. With ``setup_model=True`` it models spec 03 steps
B-F (``docs/protocol/03-session-sequence.md``). The firmware's setup state
machine was not traced, so nearly every behaviour below is a guess: each one
is a constructor option (:data:`SETUP_KNOBS`) with a CLI flag, and every
value in use, given or default, is disclosed in a ``config`` event with its
basis.

* **Pairing** (B). ``SET RF CHANNELS;c1;c2`` -> ``INFO;0``, ``INFO;3``; then,
  only if ``(c1, c2) == rf_pair`` (in that order), both tower frames and
  ``INFO;4`` after ``pairing_delay`` seconds. Any other pair: no tower
  answers, no ``INFO;4`` (a ``pairing-no-towers`` event).
* **Encoder** (C). Once paired, ``GET GLOBAL SYS STATUS`` (or, with
  ``encoder_trigger="pairing"``, the pairing itself) brings ``INFO;24``, or
  ``INFO;33`` when ``encoder_calibrated=False``; reported once per power cycle.
* **Panel detection** (D-D3). What starts it on the robot is unknown (the PC
  sends nothing), so it starts when the test calls
  :meth:`~SimulatedGoliath.start_panel_detection` (the operator at the
  robot), or ``detection_autostart`` seconds after ``INFO;24``. It sends
  ``INFO;5``, then one ``INFO;25`` per vertex of ``panel_polygon`` every
  ``detection_step_delay`` seconds, then ``INFO;27;<tower_distance>``, waits
  for ``SET T2 POSITION`` (``wait_t2_position``; with ``info27_requires_t2``
  ``INFO;27`` itself waits for it, which exercises a runner's ``INFO;27``
  timeout), sends ``INFO;6`` and moves to ``park_pose``. A
  ``SET T2 POSITION`` that arrives while the robot is not waiting for it is
  acknowledged and, by default, ignored by the state machine
  (``t2_before_wait``). ``detection_faults`` drop, repeat or add messages,
  stall the trace or drop the link (:data:`DETECTION_FAULTS`).
  :meth:`~SimulatedGoliath.reboot` interrupts it.
* **Vertices** (``INFO;25``). Each vertex is measured (``vertex_noise_mm``,
  uniform per axis, seeded by ``vertex_noise_seed``), mapped by
  ``vertex_frame`` (one of the eight origin-fixing axis maps in
  :data:`VERTEX_FRAMES`) and encoded as ``vertex_encoding``: ``"xy"`` (the
  01.02.00 format ``INFO;25;%.4f;%.4f``), ``"polar"`` (length and angle about
  tower 1, the form ``SET VERTEX`` takes; angle in ``polar_angle_unit``) or
  ``"xyz"`` (the vendor table's three fields; z from the polygon, else 0).
* **Position frames outside work** (``frames_outside_work``): ``True`` streams
  a frame (line 0) every ``idle_frame_interval`` seconds whenever no job runs
  and no detection is under way; ``"after-detection"`` only once ``INFO;6``
  has been sent. ``detection_frames`` adds one frame per traced vertex and
  one at the park pose. Position frames carry the pose in the panel frame,
  as the work cycle's do: ``vertex_frame`` applies to ``INFO;25`` only
  (whether position frames share the vertex stream's frame is not known).
* **Upload before the panel size** (``require_panel_size_before_upload``):
  ``INFO;42`` at the header or after the upload (``info42_at``). The upload
  mode is entered either way (the 01.02.00 header handler sets the flag
  unconditionally) and the program is kept. Whether the robot then runs it is
  not known: by default no work cycle starts; ``info42_starts_work=True``
  runs it anyway, so a client that reads ``INFO;42`` as "nothing will move"
  can be caught.
* **Connect-time replay** (``replay_setup``): ``INFO;6`` if panel detection
  is done, ``INFO;4`` if paired, then ``INFO;24`` if the encoder was reported
  calibrated, after ``INFO;34`` (and ``INFO;8``), in spec 09's order.
* :meth:`~SimulatedGoliath.reboot` power-cycles the robot: every connection
  drops and the setup state (pairing, encoder report, panel detection, the
  T2 axis, the panel size, an upload in progress) is forgotten. The
  loaded-program flag that drives the ``INFO;8`` replay is kept, as
  ``CMD SYS RESET`` always did here; what really survives a power cycle is
  not established.
* :meth:`~SimulatedGoliath.setup_state` returns the state machine's view.

Observing the simulator
=======================

* :attr:`~SimulatedGoliath.received` lists every parsed command as a
  :class:`ReceivedCommand`: a ``str`` (so it compares equal to the command
  text) that also carries ``.conn`` (accept order, from 0) and ``.slot``.
* :attr:`~SimulatedGoliath.events` is the simulator's own log
  (:class:`SimEvent`): accepts, closes, every message sent, dropped or routed
  to another client, unknown commands, upload stages, and ``done`` after each
  received buffer has been fully handled. With the setup model also
  ``pairing``, ``pairing-no-towers``, ``encoder``, ``detection-start``,
  ``detection-not-started``, ``vertex``, ``detection-wait-t2``,
  ``t2-position``, ``setup-out-of-order``, ``fault``, ``detection-stalled``,
  ``detection-interrupted``, ``parked``, ``panel-size-missing``,
  ``work-not-started``, ``work-without-panel-size``, ``replay`` (setup replay
  off) and ``reboot``.
"""

from __future__ import annotations

import argparse
import contextlib
import math
import random
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
        self.panel_size_missing = False  # setup model: no SET PANEL SIZE before the header


# Options that shape firmware-profile output; legacy-spec does not support them.
_OUTPUT_DEFAULTS: Dict[str, object] = dict(
    unknown_command=None, connect_replay=None, upload_acks=None, global_status=None,
    cage="CLOSED", cage_reply="direct", cage_poll_interval=0.5, status_bytes=None,
    tower_bytes=(0x38, 0x34), index_calibration=28500, spindle_distance=12.34,
    position_frame="short", temperature=38, ssids=("SimNet",), sanity=(1,) * 15,
)


# --------------------------------------------------------------------------- #
# Setup phases (spec 03 steps B-F), modelled only with setup_model=True
# --------------------------------------------------------------------------- #

#: The eight axis maps that keep tower 1 at the origin, as integer matrices
#: ``((a, b), (c, d))``: a measured panel-frame point ``(x, y)`` is reported as
#: ``(a*x + b*y, c*x + d*y)``. The swapped forms are the map's own output with
#: its two values exchanged: ``swap-mirror-x`` reports ``(y, -x)``,
#: ``swap-mirror-y`` ``(-y, x)``, ``swap-rot180`` ``(-y, -x)``. Which one the
#: robot uses is unknown (identity is only inferred).
VERTEX_FRAMES: Dict[str, Tuple[Tuple[int, int], Tuple[int, int]]] = {
    "identity": ((1, 0), (0, 1)),
    "swap": ((0, 1), (1, 0)),
    "mirror-x": ((-1, 0), (0, 1)),
    "mirror-y": ((1, 0), (0, -1)),
    "rot180": ((-1, 0), (0, -1)),
    "swap-mirror-x": ((0, 1), (-1, 0)),
    "swap-mirror-y": ((0, -1), (1, 0)),
    "swap-rot180": ((0, -1), (-1, 0)),
}

#: ``INFO;25`` encodings: ``xy`` is the 01.02.00 format (two values, spec 09,
#: single pass); ``polar`` is length and angle about tower 1 (the form
#: ``SET VERTEX`` takes); ``xyz`` is the vendor table's three fields.
VERTEX_ENCODINGS = ("xy", "polar", "xyz")

#: Placeholder panel (mm), counter-clockwise from tower 1 at the origin. Not a
#: measurement of any panel.
DEFAULT_PANEL_POLYGON: Tuple[Tuple[float, float], ...] = (
    (0.0, 0.0), (1200.0, 0.0), (1200.0, 800.0), (0.0, 800.0),
)

#: Panel-detection faults (``detection_faults``), by name, with the integer
#: arguments each takes. N counts traced vertices from 1.
#:
#: * ``no-info5``, ``no-info27``, ``no-info6``: that message is not sent (as if
#:   lost); the robot carries on as usual.
#: * ``skip-vertex:N``: vertex N's ``INFO;25`` is not sent (a gap in the stream).
#: * ``duplicate-vertex:N``: vertex N's ``INFO;25`` is sent twice.
#: * ``stall-after:N``: the trace stops after vertex N; nothing more is sent.
#: * ``link-drop-after:N``: the robot closes every connection after vertex N
#:   and goes on detecting (its messages are dropped).
#: * ``info-after:N:CODE`` / ``err-after:N:CODE``: an extra ``INFO;CODE`` /
#:   ``ERR;CODE`` after vertex N (unknown, out-of-order or error messages).
DETECTION_FAULTS: Dict[str, int] = {
    "no-info5": 0, "no-info27": 0, "no-info6": 0, "skip-vertex": 1, "duplicate-vertex": 1,
    "stall-after": 1, "link-drop-after": 1, "info-after": 2, "err-after": 2,
}

_FRAMES_OUTSIDE_WORK_MODES = ("off", "always", "after-detection")

#: Every setup-model option with its default. Each is a guess about the robot
#: (or a placeholder value), disclosed with its basis in a ``config`` event.
_SETUP_DEFAULTS: Dict[str, object] = dict(
    rf_pair=(101, 114), pairing_delay=0.0, encoder_calibrated=True,
    encoder_trigger="global-status", detection_autostart=None, panel_polygon=None,
    vertex_encoding=None, vertex_frame="identity", vertex_noise_mm=0.0, vertex_noise_seed=0,
    polar_angle_unit="deg", tower_distance=None, wait_t2_position=True,
    info27_requires_t2=False, t2_before_wait="ignored", detection_step_delay=0.01,
    detection_frames=False, frames_outside_work="off", idle_frame_interval=0.5, park_pose=None,
    detection_faults=(), require_panel_size_before_upload=True, info42_at="header",
    info42_starts_work=False, replay_setup=True,
)

#: The setup-model options (constructor keywords), in a stable order.
SETUP_KNOBS: Tuple[str, ...] = tuple(_SETUP_DEFAULTS)

# Where each setup default comes from. Shown with the value in use.
_SETUP_BASIS: Dict[str, str] = dict(
    rf_pair="GUESS: placeholder pair; the owner's towers' pair is not known (the vendor "
            "app took it from a CONFIG_n entry)",
    pairing_delay="GUESS: how long pairing takes is not known",
    encoder_calibrated="GUESS: whether this robot reports INFO;24 or INFO;33",
    encoder_trigger="GUESS: spec 03 step C lists INFO;24 after GET GLOBAL SYS STATUS; "
                    "what triggers it was not traced",
    detection_autostart="GUESS: what starts panel detection is not known (the PC sends "
                        "nothing); None waits for start_panel_detection(), the operator",
    panel_polygon="placeholder panel, not a measurement",
    vertex_encoding="xy = INFO;25;%.4f;%.4f (spec 09, single pass); xyz = the vendor table",
    vertex_frame="GUESS: identity is only inferred",
    vertex_noise_mm="GUESS: no measurement error",
    vertex_noise_seed="fixed seed so runs repeat",
    polar_angle_unit="GUESS: the angle unit of a length;angle vertex is not known",
    tower_distance="GUESS: None = the polygon's extent along X (tower 2 along X)",
    wait_t2_position="spec 03 D3: INFO;6 after SET T2 POSITION (vendor app order, not traced "
                     "in the firmware)",
    info27_requires_t2="spec 03 D2/D3: INFO;27 before SET T2 POSITION (vendor app order)",
    t2_before_wait="GUESS: a SET T2 POSITION before the robot waits for it is not used",
    detection_step_delay="GUESS: timing; a real trace is much slower",
    detection_frames="GUESS: whether frames stream during detection is not known",
    frames_outside_work="GUESS: whether frames stream outside work is not known (spec 02: "
                        "'robot streams anyway')",
    idle_frame_interval="GUESS: frame period outside work",
    park_pose="GUESS: None = the robot stays at the last traced vertex",
    detection_faults="none injected",
    require_panel_size_before_upload="GUESS: INFO;42 PANEL_SIZE_NOT_RECEIVED (spec 02) for "
                                      "an upload before SET PANEL SIZE",
    info42_at="GUESS: when INFO;42 is sent; spec 09 finds no send in the 01.02.00 GCODE UPLOAD "
              "handler itself (the routines it calls were not traced), so 'header' assumes "
              "one of them sends it",
    info42_starts_work="GUESS: whether the robot still runs a program uploaded before SET "
                       "PANEL SIZE (after INFO;42) is not known; False keeps it, unrun",
    replay_setup="spec 09: the replay sends INFO;6/INFO;4 by status field, INFO;24 on a "
                 "second event (single pass; conditions not fully decoded)",
)
assert set(_SETUP_BASIS) == set(_SETUP_DEFAULTS)


class _Faults:
    """Parsed ``detection_faults`` (see :data:`DETECTION_FAULTS`)."""

    def __init__(self, specs: Iterable[str], vertex_count: int) -> None:
        if isinstance(specs, (str, bytes)):
            raise ValueError("detection_faults must be a sequence of fault names, "
                             "for example ('no-info5', 'skip-vertex:2')")
        self.specs: Tuple[str, ...] = tuple(specs)
        self._flags: Dict[str, str] = {}
        self._at: Dict[int, List[Tuple[str, Tuple[int, ...], str]]] = {}
        for text in self.specs:
            if not isinstance(text, str):
                raise ValueError(f"detection fault {text!r} is not a string")
            name, *rest = text.split(":")
            if name not in DETECTION_FAULTS:
                raise ValueError(f"unknown detection fault {text!r}; known: "
                                 f"{', '.join(sorted(DETECTION_FAULTS))}")
            nargs = DETECTION_FAULTS[name]
            if nargs == 0:
                if rest:
                    raise ValueError(f"detection fault {text!r}: {name} takes no argument")
                self._flags[name] = text
                continue
            if len(rest) != nargs:
                what = "a vertex number" if nargs == 1 else "a vertex number and a code"
                example = f"{name}:2" if nargs == 1 else f"{name}:2:3"
                raise ValueError(f"detection fault {text!r}: {name} needs {what}, as in {example}")
            values = []
            for r in rest:
                try:
                    values.append(int(r))
                except ValueError as exc:
                    raise ValueError(f"detection fault {text!r}: expected an integer, "
                                     f"not {r!r}") from exc
            if not 1 <= values[0] <= vertex_count:
                raise ValueError(f"detection fault {text!r}: the vertex number must be "
                                 f"between 1 and {vertex_count} (the polygon's vertices)")
            if nargs == 2 and values[1] < 0:
                raise ValueError(f"detection fault {text!r}: the code must be >= 0")
            self._at.setdefault(values[0], []).append((name, tuple(values[1:]), text))

    def flag(self, name: str) -> Optional[str]:
        return self._flags.get(name)

    def at(self, vertex: int, name: str) -> List[Tuple[Tuple[int, ...], str]]:
        return [(args, text) for n, args, text in self._at.get(vertex, ()) if n == name]

    def extra_messages(self, vertex: int) -> List[Tuple[str, int, str]]:
        """The ``info-after``/``err-after`` faults at ``vertex``, in the order given."""
        return [("INFO" if n == "info-after" else "ERR", args[0], text)
                for n, args, text in self._at.get(vertex, ()) if n in ("info-after", "err-after")]


def _finite_floats(value: object, name: str, lengths: Tuple[int, ...]) -> Tuple[float, ...]:
    try:
        out = tuple(float(v) for v in value)  # type: ignore[union-attr]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be {' or '.join(map(str, lengths))} numbers, "
                         f"not {value!r}") from exc
    if len(out) not in lengths or not all(math.isfinite(v) for v in out):
        raise ValueError(f"{name} must be {' or '.join(map(str, lengths))} finite numbers, "
                         f"not {value!r}")
    return out


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
        # -- setup phases (spec 03 steps B-F); see the module docstring --------
        # Every option below needs setup_model=True and every default is a
        # GUESS or a placeholder (disclosed in a "config" event).
        setup_model: bool = False,
        rf_pair: Tuple[int, int] = (101, 114),
        pairing_delay: float = 0.0,
        encoder_calibrated: bool = True,
        encoder_trigger: str = "global-status",
        detection_autostart: Optional[float] = None,
        panel_polygon: Optional[Sequence[Sequence[float]]] = None,
        vertex_encoding: Optional[str] = None,
        vertex_frame: str = "identity",
        vertex_noise_mm: float = 0.0,
        vertex_noise_seed: int = 0,
        polar_angle_unit: str = "deg",
        tower_distance: Optional[float] = None,
        wait_t2_position: bool = True,
        info27_requires_t2: bool = False,
        t2_before_wait: str = "ignored",
        detection_step_delay: float = 0.01,
        detection_frames: bool = False,
        frames_outside_work: Union[bool, str] = False,
        idle_frame_interval: float = 0.5,
        park_pose: Optional[Sequence[float]] = None,
        detection_faults: Iterable[str] = (),
        require_panel_size_before_upload: bool = True,
        info42_at: str = "header",
        info42_starts_work: bool = False,
        replay_setup: bool = True,
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

        # -- setup model: validate every option, then refuse any that would be
        # ignored (setup_model=False) rather than ignore it silently.
        pair = tuple(rf_pair)
        _check(len(pair) == 2 and all(isinstance(c, int) and not isinstance(c, bool) and c >= 0
                                      for c in pair),
               f"rf_pair must be two channel numbers (T1, T2) >= 0, not {rf_pair!r}")
        _check(encoder_trigger in ("global-status", "pairing"),
               "encoder_trigger must be 'global-status' or 'pairing'")
        polygon: Optional[Tuple[Tuple[float, ...], ...]] = None
        if panel_polygon is not None:
            polygon = tuple(_finite_floats(p, "each panel_polygon vertex", (2, 3))
                            for p in panel_polygon)
            _check(len(polygon) >= 3,
                   f"panel_polygon needs at least 3 vertices, not {len(polygon)}")
        _check(vertex_encoding is None or vertex_encoding in VERTEX_ENCODINGS,
               f"vertex_encoding must be one of {', '.join(VERTEX_ENCODINGS)}")
        _check(vertex_frame in VERTEX_FRAMES,
               f"vertex_frame must be one of {', '.join(VERTEX_FRAMES)}")
        _check(polar_angle_unit in ("deg", "rad"), "polar_angle_unit must be 'deg' or 'rad'")
        _check(isinstance(vertex_noise_seed, int), "vertex_noise_seed must be an integer")
        _check(t2_before_wait in ("ignored", "accepted"),
               "t2_before_wait must be 'ignored' or 'accepted'")
        _check(info42_at in ("header", "upload-end"), "info42_at must be 'header' or 'upload-end'")
        if isinstance(frames_outside_work, bool):
            frames_mode = "always" if frames_outside_work else "off"
        else:
            frames_mode = str(frames_outside_work)
        _check(frames_mode in _FRAMES_OUTSIDE_WORK_MODES,
               "frames_outside_work must be False, True, 'off', 'always' or 'after-detection'")
        pose = None if park_pose is None else _finite_floats(park_pose, "park_pose (x, y, z, angle)",
                                                             (4,))
        for name, value in (("pairing_delay", pairing_delay),
                            ("vertex_noise_mm", vertex_noise_mm),
                            ("detection_step_delay", detection_step_delay)):
            _check(math.isfinite(value) and value >= 0, f"{name} must be >= 0")
        _check(detection_autostart is None
               or (math.isfinite(detection_autostart) and detection_autostart >= 0),
               "detection_autostart must be None or >= 0 seconds")
        _check(tower_distance is None or (math.isfinite(tower_distance) and tower_distance > 0),
               "tower_distance must be None or > 0 mm")
        _check(math.isfinite(idle_frame_interval) and idle_frame_interval > 0,
               "idle_frame_interval must be > 0")
        _check(not (info27_requires_t2 and not wait_t2_position),
               "info27_requires_t2=True needs wait_t2_position=True (INFO;27 waits for "
               "SET T2 POSITION, so the robot waits for it)")
        _check(not (info42_starts_work and not require_panel_size_before_upload),
               "info42_starts_work=True needs require_panel_size_before_upload=True (without "
               "it there is no INFO;42, and the option would be ignored)")
        faults = _Faults(detection_faults, len(polygon or DEFAULT_PANEL_POLYGON))
        setup_given: Dict[str, object] = dict(
            rf_pair=pair, pairing_delay=pairing_delay, encoder_calibrated=encoder_calibrated,
            encoder_trigger=encoder_trigger, detection_autostart=detection_autostart,
            panel_polygon=polygon, vertex_encoding=vertex_encoding, vertex_frame=vertex_frame,
            vertex_noise_mm=vertex_noise_mm, vertex_noise_seed=vertex_noise_seed,
            polar_angle_unit=polar_angle_unit, tower_distance=tower_distance,
            wait_t2_position=wait_t2_position, info27_requires_t2=info27_requires_t2,
            t2_before_wait=t2_before_wait, detection_step_delay=detection_step_delay,
            detection_frames=detection_frames, frames_outside_work=frames_mode,
            idle_frame_interval=idle_frame_interval, park_pose=pose,
            detection_faults=faults.specs,
            require_panel_size_before_upload=require_panel_size_before_upload,
            info42_at=info42_at, info42_starts_work=info42_starts_work,
            replay_setup=replay_setup,
        )
        assert set(setup_given) == set(_SETUP_DEFAULTS)
        changed = sorted(k for k, v in setup_given.items() if v != _SETUP_DEFAULTS[k])
        if not setup_model and changed:
            raise ValueError(
                f"{', '.join(changed)}: only with setup_model=True (without it the setup "
                "phases are not modelled, and the option would be ignored)"
            )
        if setup_model and not p.slot_routed:
            raise ValueError("setup_model is not supported by the legacy-spec profile; "
                             "use profile='gfw-01.02.00' or 'vendor-app-newer'")

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
        # setup model (see the module docstring); every value is a GUESS
        self.setup_model = setup_model
        self.rf_pair: Tuple[int, int] = pair  # type: ignore[assignment]
        self.pairing_delay = pairing_delay
        self.encoder_calibrated = encoder_calibrated
        self.encoder_trigger = encoder_trigger
        self.detection_autostart = detection_autostart
        self.panel_polygon: Tuple[Tuple[float, ...], ...] = polygon or DEFAULT_PANEL_POLYGON
        # GUESS (newer profile): the vendor table's three-field INFO;25.
        self.vertex_encoding = vertex_encoding or ("xyz" if p.name == "vendor-app-newer" else "xy")
        self.vertex_frame = vertex_frame
        self.vertex_noise_mm = vertex_noise_mm
        self.vertex_noise_seed = vertex_noise_seed
        self.polar_angle_unit = polar_angle_unit
        xs = [v[0] for v in self.panel_polygon]
        # GUESS: towers at the two ends of the panel's edge along X.
        self.tower_distance = tower_distance if tower_distance is not None else max(xs) - min(xs)
        self.wait_t2_position = wait_t2_position
        self.info27_requires_t2 = info27_requires_t2
        self.t2_before_wait = t2_before_wait
        self.detection_step_delay = detection_step_delay
        self.detection_frames = detection_frames
        self.frames_outside_work = frames_mode
        self.idle_frame_interval = idle_frame_interval
        self.park_pose: Optional[Tuple[float, ...]] = pose
        self.detection_faults = faults.specs
        self.require_panel_size_before_upload = require_panel_size_before_upload
        self.info42_at = info42_at
        self.info42_starts_work = info42_starts_work
        self.replay_setup = replay_setup
        self._faults = faults
        self._noise = random.Random(vertex_noise_seed)
        effective = dict(setup_given, panel_polygon=self.panel_polygon,
                         vertex_encoding=self.vertex_encoding, tower_distance=self.tower_distance)

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
        # setup-model state. Lock order: _cmd_lock, then _setup_lock, then _lock.
        # _setup_lock makes "is this power cycle still current? then send" atomic
        # with reboot(), so nothing from before a reboot is sent after it.
        self._setup_lock = threading.Lock()
        self._setup_gen = 0  # power cycles (reboot() increments it)
        self._pairing_state = "idle"  # idle | pairing | paired
        self._pairing_attempt = 0
        self._rf_channels: Optional[Tuple[int, int]] = None
        self._encoder_state: Optional[str] = None  # None | calibrated | needs-calibration
        self._detection_state = "idle"  # idle | running | wait-t2 | done | stalled
        self._t2_event = threading.Event()
        self._t2_latched = False
        self._t2_position: Optional[int] = None
        self._panel_size_received = False
        self._measured_vertices: List[Tuple[float, float]] = []
        self._vertex_lines: List[bytes] = []
        self._deferred: List[Callable[[], None]] = []  # run after a command's direct reply

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
            "SET T2 POSITION": self._cmd_set_t2_position, "ENABLE SPINDLE": self._cmd_ack,
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
        if setup_model:
            self._event("config", note="setup model on (spec 03 steps B-F): the firmware's setup "
                                       "state machine was not traced, so every value below is a "
                                       "GUESS or a placeholder unless its basis says otherwise")
            for name in SETUP_KNOBS:
                source = "given" if name in changed else "default"
                self._event("config", note=f"setup {name}={effective[name]!r} ({source}; "
                                           f"{_SETUP_BASIS[name]})")
        elif p.slot_routed:
            self._event("config", note="setup phases not modelled (setup_model=False): SET RF "
                                       "CHANNELS pairs with any channel pair at once, there is no "
                                       "encoder report, panel detection never runs and an upload "
                                       "needs no SET PANEL SIZE; setup_model=True models spec 03 "
                                       "steps B-F")

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
        if self.setup_model and self.frames_outside_work != "off":
            self._spawn(self._idle_frames, "sim-idle-frames")
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

    def reboot(self, note: str = "reboot() called: the robot is power-cycled") -> None:
        """Power-cycle the robot: every connection drops, the setup state is forgotten.

        Interrupts a panel detection or a delayed pairing in progress; nothing
        they would have sent is sent afterwards. The loaded-program flag (the
        ``INFO;8`` replay) is kept, as ``CMD SYS RESET`` always did here; what
        really survives a power cycle is not established.
        """
        self._reboot(note)

    # -- setup phases (setup_model=True) --------------------------------------

    def start_panel_detection(self) -> bool:
        """The operator starts panel detection at the robot.

        What starts it on the real robot is not known: the PC sends nothing
        (spec 03 step D). Returns False, with a ``detection-not-started``
        event saying why, when the robot is not ready (not paired, no
        ``INFO;24``, or a detection already under way).
        """
        self._require_setup("start_panel_detection()")
        return self._start_detection("start_panel_detection(): the operator started it at the robot")

    def setup_state(self) -> Dict[str, object]:
        """The setup state machine's view (for tests): a snapshot dict.

        ``pairing`` (idle | pairing | paired), ``rf_channels`` (last
        ``SET RF CHANNELS`` pair or None), ``encoder`` (None | calibrated |
        needs-calibration), ``detection`` (idle | running | wait-t2 | done |
        stalled), ``panel_detected``, ``t2_position`` (last value or None),
        ``panel_size`` (None until ``SET PANEL SIZE``), ``vertices`` (the
        measured panel-frame points of the last trace, before the axis map),
        ``vertex_lines`` (the ``INFO;25`` lines sent), ``tower_distance`` and
        ``power_cycles``.
        """
        self._require_setup("setup_state()")
        with self._lock:
            return dict(
                pairing=self._pairing_state, rf_channels=self._rf_channels,
                encoder=self._encoder_state, detection=self._detection_state,
                panel_detected=self._panel_detected, t2_position=self._t2_position,
                panel_size=self._panel_size if self._panel_size_received else None,
                vertices=list(self._measured_vertices), vertex_lines=list(self._vertex_lines),
                tower_distance=self.tower_distance, power_cycles=self._setup_gen,
            )

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
                self._deferred.clear()
                handler = self._handlers.get(name)
                if handler is None:
                    reply = self._unmodelled(conn, name, text)
                else:
                    reply = handler(conn, args, text)
                if reply:
                    self._direct(conn, reply)
                # What the robot's state machine does next (setup model), after
                # the handler and its direct reply.
                while self._deferred:
                    self._deferred.pop(0)()
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
        # _setup_lock (setup model): never half-way through a setup step, so the
        # bytes agree with the messages already sent.
        with self._setup_lock if self.setup_model else contextlib.nullcontext(), self._lock:
            paired, panel = self._paired, self._panel_detected
            uploaded, working = self._gcode_uploaded, self._working
            pairing, detection, encoder = (self._pairing_state, self._detection_state,
                                           self._encoder_state)
        if self.setup_model:
            # GUESS: 1 = in progress for pairing and pd_status; encoder_calibrated
            # is 1 once INFO;24 has been reported (0 at a fresh boot).
            return bytes([
                3 if paired else (1 if pairing == "pairing" else 0),
                1 if paired else 0, 1 if paired else 0,
                3 if panel else (1 if detection in ("running", "wait-t2", "stalled") else 0),
                1 if panel else 0, 1 if uploaded else 0, 1 if working else 0, 0, 0,
                1 if encoder == "calibrated" else 0,
            ])
        return bytes([
            3 if paired else 0, 1 if paired else 0, 1 if paired else 0,
            3 if panel else 0, 1 if panel else 0, 1 if uploaded else 0,
            1 if working else 0, 0, 0, 1,
        ])

    def _cmd_global_status(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        self._info(0)
        if self.setup_model and self.encoder_trigger == "global-status":
            # GUESS (spec 03 step C): once paired, the status request is followed
            # by the encoder report, once per power cycle.
            with self._setup_lock, self._lock:
                gen = self._setup_gen
                due = self._paired and self._encoder_state is None
            if due:
                self._deferred.append(lambda: self._encoder_report(gen))
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
        if self.setup_model:
            self._setup_rf_channels(args)
            return None
        # GUESS: the pairing sequence. The handler only acknowledges; pairing runs
        # in the firmware's state machine. The vendor app waits for INFO;3,
        # tower frames and INFO;4, which the simulator sends at once.
        self._info(3)
        self._tower_frames()
        self._info(4)
        with self._lock:
            self._paired = True
        return None

    def _cmd_set_t2_position(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        self._info(0)  # spec 09: the handler sends INFO;0
        if not self.setup_model:
            return None
        value = _atoi(args[0]) if args else 0  # GUESS: any value is taken as given
        # Under _setup_lock: a detection step (a message and the state change it
        # brings) is atomic, so the state read here is the one the PC has seen.
        with self._setup_lock, self._lock:
            self._t2_position = value
            state = self._detection_state
            latch = self.t2_before_wait == "accepted" and state in ("idle", "running")
            if latch:
                self._t2_latched = True
        cmd = f"SET T2 POSITION;{value}"
        if state == "wait-t2":
            self._event("t2-position", conn=conn.index, slot=conn.slot,
                        note=f"{cmd}: panel detection was waiting for it")
            self._deferred.append(self._t2_event.set)  # after the INFO;0 above
        elif latch:
            self._event("t2-position", conn=conn.index, slot=conn.slot,
                        note=f"{cmd} while panel detection is {state}: kept for when the "
                             "robot waits for it (t2_before_wait='accepted', GUESS)")
        else:
            self._event("setup-out-of-order", conn=conn.index, slot=conn.slot,
                        note=f"{cmd} while panel detection is {state}, not waiting for it: "
                             "acknowledged, not used by the setup state machine "
                             f"(t2_before_wait={self.t2_before_wait!r}, GUESS)")
        return None

    def _cmd_set_panel_size(self, conn: _Conn, args: List[bytes], text: bytes) -> Optional[bytes]:
        x = _atof(args[0]) if len(args) > 0 else 0.0
        y = _atof(args[1]) if len(args) > 1 else 0.0
        with self._lock:
            self._panel_size = (x, y)
            self._panel_size_received = True
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
        upload = _Upload(size, conn.index)
        with self._lock:
            upload.panel_size_missing = (self.setup_model and self.require_panel_size_before_upload
                                         and not self._panel_size_received)
            self._upload = upload
        note = f"expecting {size} bytes; parser off until then, for every connection"
        if size <= 0:
            note += " (GUESS: with size <= 0 the next buffer completes the upload)"
        self._event("upload-start", conn=conn.index, slot=conn.slot, data=text, note=note)
        if extra:
            self._event("upload-header-extra-ignored", conn=conn.index, slot=conn.slot,
                        data=extra, note="the handler reads only the size from its buffer")
        if self.upload_acks == "vendor":
            self._info(7)  # GUESS: vendor app expects ENTER GCODE_UPLOAD
        if upload.panel_size_missing:
            # GUESS: INFO;42 PANEL_SIZE_NOT_RECEIVED (spec 02). The 01.02.00 header
            # handler sets the upload flag unconditionally (spec 09), so the
            # upload mode is entered all the same; whether the work cycle then
            # starts is info42_starts_work. Spec 09 finds no send in the handler
            # body itself: info42_at="header" assumes a routine it calls sends it.
            self._event("panel-size-missing", conn=conn.index, slot=conn.slot, data=text,
                        note=f"upload before SET PANEL SIZE: INFO;42 "
                             f"{'now' if self.info42_at == 'header' else 'after the upload'} "
                             f"(info42_at={self.info42_at!r}); upload mode entered anyway")
            if self.info42_at == "header":
                self._info(42)
        return None  # the 01.02.00 header handler body sends nothing (spec 09)

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
        if upload.panel_size_missing:
            if self.info42_at == "upload-end":
                self._info(42)  # GUESS: see _cmd_gcode_upload
            if not self.info42_starts_work:
                self._event("work-not-started",
                            note="no SET PANEL SIZE before the upload (INFO;42): GUESS "
                                 "(info42_starts_work=False): the robot keeps the program "
                                 "and does not start the work cycle")
                return
            self._event("work-without-panel-size",
                        note="no SET PANEL SIZE before the upload (INFO;42): GUESS "
                             "(info42_starts_work=True): the robot runs the program anyway")
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
        self._reboot("CMD SYS RESET", conn)
        return None

    def _reboot(self, note: str, conn: Optional[_Conn] = None) -> None:
        # GUESS: a reboot drops every connection and forgets the session state
        # (the setup state included); the loaded-program flag is kept.
        with self._setup_lock:
            self._setup_gen += 1  # nothing from the old power cycle is sent after this
            with self._lock:
                conns = [c for c in self._conns if not (c.robot_closed or c.peer_closed)]
                self._upload = None
                self._paired = self._jog = False
                self._pairing_state = "idle"
                self._rf_channels = None
                self._encoder_state = None
                self._detection_state = "idle"
                self._panel_detected = False
                self._panel_size = (0.0, 0.0)
                self._panel_size_received = False
                self._t2_position = None
                self._t2_latched = False
        self._event("reboot", conn=None if conn is None else conn.index,
                    slot=None if conn is None else conn.slot, note=note)
        self._work_abort.set()
        self._work_wake.set()
        for c in conns:
            self._robot_drop(c, "reboot")

    # -- setup phases (setup_model=True): spec 03 steps B-D3 -------------------
    # The firmware's setup state machine was not traced: every step below is a
    # GUESS shaped by the vendor app's order (spec 03) and made configurable.

    def _require_setup(self, what: str) -> None:
        if not self.setup_model:
            raise RuntimeError(f"{what}: the setup phases are modelled only with setup_model=True")

    def _alive(self, gen: int) -> bool:
        """Still the same power cycle, and the simulator still running."""
        return self._setup_gen == gen and not self._stop.is_set()

    def _emit(self, gen: int, step: Callable[[], None]) -> bool:
        """Run ``step`` (which sends) unless the robot rebooted or stopped meanwhile."""
        with self._setup_lock:
            if not self._alive(gen):
                return False
            step()
            return True

    def _pause(self, gen: int, seconds: float) -> bool:
        """Sleep ``seconds``; False as soon as the power cycle ends."""
        deadline = time.monotonic() + seconds
        while self._alive(gen):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(remaining, 0.02))
        return False

    def _fault(self, text: str, what: str) -> None:
        self._event("fault", note=f"{text}: {what} (detection_faults)")

    def _setup_rf_channels(self, args: List[bytes]) -> None:
        channels = (_atoi(args[0]) if args else 0, _atoi(args[1]) if len(args) > 1 else 0)
        with self._setup_lock:
            gen = self._setup_gen
            with self._lock:
                self._pairing_attempt += 1
                attempt = self._pairing_attempt
                self._rf_channels = channels
                self._pairing_state = "pairing"
                self._paired = False  # GUESS: every SET RF CHANNELS pairs afresh
        self._info(3)  # GUESS: ENTER_PAIRING for every SET RF CHANNELS
        if self.pairing_delay > 0:
            self._spawn(self._pairing_after_delay, "sim-pairing", gen, attempt, channels)
        else:
            self._finish_pairing(gen, attempt, channels)

    def _pairing_after_delay(self, gen: int, attempt: int, channels: Tuple[int, int]) -> None:
        if self._pause(gen, self.pairing_delay):
            self._finish_pairing(gen, attempt, channels)

    def _finish_pairing(self, gen: int, attempt: int, channels: Tuple[int, int]) -> None:
        cmd = "SET RF CHANNELS;%d;%d" % channels
        outcome: List[str] = []

        def step() -> None:
            with self._lock:
                if self._pairing_attempt != attempt:
                    outcome.append("superseded")
                    return
            if channels != self.rf_pair:
                # GUESS: towers listening on other channels never answer.
                self._event("pairing-no-towers",
                            note=f"{cmd}: not the towers' pair {self.rf_pair} (rf_pair): no tower "
                                 "answers, no tower frames, no INFO;4 (GUESS)")
                outcome.append("no towers")
                return
            self._tower_frames()
            self._info(4)
            with self._lock:
                self._paired = True
                self._pairing_state = "paired"
            self._event("pairing", note=f"{cmd}: both towers paired (INFO;4)")
            outcome.append("paired")

        if self._emit(gen, step) and outcome == ["paired"] and self.encoder_trigger == "pairing":
            self._encoder_report(gen)

    def _encoder_report(self, gen: int) -> None:
        """INFO;24 (or INFO;33), once per power cycle."""
        code = 24 if self.encoder_calibrated else 33
        with self._setup_lock:
            if not self._alive(gen):
                return
            with self._lock:
                if self._encoder_state is not None:
                    return
                self._encoder_state = "calibrated" if self.encoder_calibrated else "needs-calibration"
            self._info(code)
        self._event("encoder", note=f"INFO;{code}: encoder_calibrated={self.encoder_calibrated} "
                                    f"(encoder_trigger={self.encoder_trigger!r}, GUESS)")
        if self.encoder_calibrated and self.detection_autostart is not None:
            self._spawn(self._autostart, "sim-autostart", gen)

    def _autostart(self, gen: int) -> None:
        if self._pause(gen, float(self.detection_autostart or 0.0)):
            self._start_detection(f"autostart {self.detection_autostart}s after the encoder "
                                  "report (detection_autostart, GUESS)", gen)

    def _start_detection(self, how: str, gen: Optional[int] = None) -> bool:
        reason: Optional[str] = None
        with self._setup_lock:
            if gen is not None and not self._alive(gen):
                return False
            gen = self._setup_gen
            with self._lock:
                state = self._detection_state
                if not self._paired:
                    reason = "the robot is not paired (no INFO;4)"
                elif self._encoder_state is None:
                    reason = "no encoder report yet (INFO;24)"
                elif self._encoder_state != "calibrated":
                    reason = "the encoder needs calibration (INFO;33)"
                elif state in ("running", "wait-t2"):
                    reason = f"panel detection is already under way ({state})"
                else:
                    self._detection_state = "running"
                    self._panel_detected = False
                    self._t2_event.clear()
                    self._measured_vertices = []
                    self._vertex_lines = []
        if reason is not None:
            self._event("detection-not-started",
                        note=f"{how}: {reason}; GUESS: the robot does nothing")
            return False
        self._event("detection-start", note=how)
        self._spawn(self._run_detection, "sim-detect", gen)
        return True

    def _run_detection(self, gen: int) -> None:
        outcome = self._detect(gen)
        if outcome == "interrupted":
            self._event("detection-interrupted",
                        note="the robot was power-cycled (or the simulator stopped) during panel "
                             "detection: nothing more is sent")

    def _measure(self, point: Tuple[float, ...]) -> Tuple[float, ...]:
        """The robot's measurement of a vertex (panel frame), with noise."""
        n = self.vertex_noise_mm
        if n <= 0:
            return point
        # GUESS: independent uniform error per axis.
        return (point[0] + self._noise.uniform(-n, n), point[1] + self._noise.uniform(-n, n)) + point[2:]

    def _vertex_fields(self, point: Tuple[float, ...]) -> Tuple[str, ...]:
        (a, b), (c, d) = VERTEX_FRAMES[self.vertex_frame]
        fx = a * point[0] + b * point[1]
        fy = c * point[0] + d * point[1]
        if self.vertex_encoding == "polar":
            length = math.hypot(fx, fy)
            angle = math.atan2(fy, fx) if length else 0.0  # GUESS: 0 for the origin vertex
            if self.polar_angle_unit == "deg":
                angle = math.degrees(angle)
            values: Tuple[float, ...] = (length, angle)
        elif self.vertex_encoding == "xyz":
            values = (fx, fy, point[2] if len(point) > 2 else 0.0)  # GUESS: z = 0 when not given
        else:
            values = (fx, fy)
        return tuple("%.4f" % v for v in values)  # %.4f as in the 01.02.00 format

    def _send_vertex(self, n: int, point: Tuple[float, ...]) -> None:
        fields = self._vertex_fields(point)
        line = ("INFO;25;" + ";".join(fields) + "\n").encode("ascii")
        self._info(25, *fields)
        with self._lock:
            self._vertex_lines.append(line)
        self._event("vertex", data=line,
                    note=f"vertex {n}: measured ({point[0]:.4f}, {point[1]:.4f}) in the panel "
                         f"frame -> vertex_frame={self.vertex_frame!r}, "
                         f"vertex_encoding={self.vertex_encoding!r}")

    def _detect(self, gen: int) -> str:
        """The panel-detection script; returns done | stalled | interrupted."""
        faults = self._faults
        flag = faults.flag("no-info5")
        if flag:
            self._fault(flag, "INFO;5 not sent")
        elif not self._emit(gen, lambda: self._info(5)):
            return "interrupted"
        for n, vertex in enumerate(self.panel_polygon, start=1):
            if not self._pause(gen, self.detection_step_delay):
                return "interrupted"
            point = self._measure(vertex)

            def step(n: int = n, point: Tuple[float, ...] = point) -> None:
                with self._lock:
                    self.pos[0], self.pos[1] = point[0], point[1]
                    self._measured_vertices.append((point[0], point[1]))
                if self.detection_frames:
                    self._position_block(0)  # GUESS: line 0 outside work
                skipped = faults.at(n, "skip-vertex")
                if skipped:
                    self._fault(skipped[0][1], f"vertex {n}'s INFO;25 not sent")
                else:
                    self._send_vertex(n, point)
                    for _, text in faults.at(n, "duplicate-vertex"):
                        self._fault(text, f"vertex {n}'s INFO;25 sent twice")
                        self._send_vertex(n, point)
                for kind, code, text in faults.extra_messages(n):
                    self._fault(text, f"{kind};{code} after vertex {n}")
                    if kind == "INFO":
                        self._info(code)
                    else:
                        self._err(code)
                for _, text in faults.at(n, "link-drop-after"):
                    self._fault(text, f"the robot closes every connection after vertex {n} "
                                      "and goes on detecting")
                    self._drop_all("detection fault " + text)
                for _, text in faults.at(n, "stall-after"):
                    with self._lock:
                        self._detection_state = "stalled"
                    self._fault(text, f"the trace stops after vertex {n}")
                    self._event("detection-stalled", note=f"after vertex {n}: nothing more is sent")

            if not self._emit(gen, step):
                return "interrupted"
            if faults.at(n, "stall-after"):
                return "stalled"
        if not self._pause(gen, self.detection_step_delay):
            return "interrupted"
        if self.info27_requires_t2:
            if not self._wait_t2(gen) or not self._emit(gen, self._send_info27):
                return "interrupted"
        elif self.wait_t2_position:
            # One step: from the moment INFO;27 is on the wire the robot waits
            # for SET T2 POSITION, so a PC that answers INFO;27 at once (spec 03
            # D2/D3) is heard however this thread is scheduled.
            if not self._wait_t2(gen, then_send=self._send_info27):
                return "interrupted"
        elif not self._emit(gen, self._send_info27):
            return "interrupted"

        def done() -> None:
            lost = faults.flag("no-info6")
            if lost:
                self._fault(lost, "INFO;6 not sent (detection is done)")
            else:
                self._info(6)
            with self._lock:  # "done" once INFO;6 (if any) has been sent
                self._panel_detected = True
                self._detection_state = "done"
            self._park()

        return "done" if self._emit(gen, done) else "interrupted"

    def _send_info27(self) -> None:
        lost = self._faults.flag("no-info27")
        if lost:
            self._fault(lost, "INFO;27 not sent")
        else:
            self._info(27, "%.4f" % self.tower_distance)  # 01.02.00 format INFO;27;%.4f

    def _wait_t2(self, gen: int, then_send: Optional[Callable[[], None]] = None) -> bool:
        """Wait for SET T2 POSITION; ``then_send`` runs in the same step as the
        move to ``wait-t2``, after it, so it is sent before the state shows."""
        with self._setup_lock:
            if not self._alive(gen):
                return False
            if then_send is not None:
                then_send()
            with self._lock:
                self._detection_state = "wait-t2"
                latched, self._t2_latched = self._t2_latched, False
        if latched:
            self._event("detection-wait-t2", note="uses the SET T2 POSITION received earlier "
                                                  "(t2_before_wait='accepted', GUESS)")
            return True
        self._event("detection-wait-t2", note="waiting for SET T2 POSITION (spec 03 D3); "
                                              "the robot waits without a timeout (GUESS)")
        while not self._t2_event.wait(0.02):
            if not self._alive(gen):
                return False
        return self._alive(gen)

    def _park(self) -> None:
        """The move after detection. Its ``parked`` event is the last thing a
        completed detection logs: everything it sends was sent before it."""
        if self.park_pose is None:
            with self._lock:
                pos = tuple(self.pos)
            self._event("parked", note=f"no park pose configured: the robot stays at the last "
                                       f"traced vertex, pose {pos} (GUESS)")
            return
        with self._lock:
            self.pos = list(self.park_pose)
        if self.detection_frames:
            self._position_block(0)
        self._event("parked", note=f"park pose {self.park_pose} after panel detection (GUESS)")

    def _drop_all(self, note: str) -> None:
        with self._lock:
            conns = [c for c in self._conns if not (c.robot_closed or c.peer_closed)]
        for c in conns:
            self._robot_drop(c, note)

    def _idle_frames(self) -> None:
        """Position frames outside work (frames_outside_work), line 0 (GUESS)."""
        while not self._stop.wait(self.idle_frame_interval):
            with self._setup_lock:
                with self._lock:
                    send = (not self._working
                            and self._detection_state not in ("running", "wait-t2")
                            and (self.frames_outside_work == "always" or self._panel_detected))
                if send:
                    self._position_block(0)

    # -- connect-time replay, work cycle --------------------------------------

    def _connect_replay(self) -> None:
        # GUESS (trigger): spec 09 runs the replay for the socket whose WINC1500
        # descriptor is 2, in some machine states. The simulator runs it whenever
        # a connection takes slot 1 (the 2nd connection, in the vendor's order).
        # _setup_lock: the replay never runs half-way through a setup step (a
        # message and the state change it brings), so it agrees with what the
        # robot has already sent.
        with self._cmd_lock, self._setup_lock:
            self._slot_message(1, self._identity_line())
            with self._lock:
                uploaded, panel, paired = self._gcode_uploaded, self._panel_detected, self._paired
                encoder = self._encoder_state
            if uploaded:
                self._info(8)
            if self.setup_model and not self.replay_setup:
                self._event("replay", note="setup replay off (replay_setup=False): no INFO;6, "
                                           "INFO;4 or INFO;24 whatever the robot's state")
                return
            if panel:
                self._info(6)
            if paired:
                self._info(4)
            if self.setup_model and encoder == "calibrated":
                self._info(24)  # spec 09: "a second event leads to INFO;24" (GUESS: at once)

    def _start_work(self, program: bytes) -> None:
        with self._setup_lock:  # atomic with an idle position frame (setup model)
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
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected comma-separated integers, not {text!r}") from exc


def _float_list(text: str) -> Tuple[float, ...]:
    try:
        return tuple(float(p) for p in text.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected comma-separated numbers, not {text!r}") from exc


def _rf_pair_arg(text: str) -> Tuple[int, ...]:
    pair = _int_list(text)
    if len(pair) != 2:
        raise argparse.ArgumentTypeError(f"expected two channels C1,C2, not {text!r}")
    return pair


def _pose_arg(text: str) -> Tuple[float, ...]:
    pose = _float_list(text)
    if len(pose) != 4:
        raise argparse.ArgumentTypeError(f"expected X,Y,Z,ANGLE, not {text!r}")
    return pose


def _polygon_arg(text: str) -> Tuple[Tuple[float, ...], ...]:
    return tuple(_float_list(vertex) for vertex in text.split(";") if vertex.strip())


# Setup-model flags that map one-to-one onto a constructor option.
_SETUP_VALUE_FLAGS = (
    "rf_pair", "pairing_delay", "encoder_trigger", "detection_autostart", "panel_polygon",
    "vertex_encoding", "vertex_frame", "vertex_noise_mm", "vertex_noise_seed",
    "polar_angle_unit", "tower_distance", "t2_before_wait", "detection_step_delay",
    "frames_outside_work", "idle_frame_interval", "park_pose", "info42_at",
)


def _add_setup_arguments(ap: argparse.ArgumentParser) -> None:
    g = ap.add_argument_group(
        "setup phases (spec 03 steps B-F)",
        "Every value is a GUESS about the robot or a placeholder. Every flag in this group "
        "except --setup-model needs --setup-model.")
    g.add_argument("--setup-model", action="store_true",
                   help="model pairing, the encoder report, panel detection and INFO;42")
    g.add_argument("--rf-pair", type=_rf_pair_arg, metavar="C1,C2",
                   help="the towers' RF channel pair; any other SET RF CHANNELS does not pair "
                        "(default 101,114)")
    g.add_argument("--pairing-delay", type=_nonneg_float, metavar="S",
                   help="seconds from INFO;3 to the tower frames and INFO;4 (default 0)")
    g.add_argument("--encoder-not-calibrated", action="store_true",
                   help="report INFO;33 instead of INFO;24")
    g.add_argument("--encoder-trigger", choices=["global-status", "pairing"],
                   help="what brings INFO;24: GET GLOBAL SYS STATUS once paired (default) or "
                        "the pairing itself")
    g.add_argument("--detection-autostart", type=_nonneg_float, metavar="S",
                   help="start panel detection S seconds after INFO;24 (default: never; this "
                        "CLI has no operator to start it)")
    g.add_argument("--panel-polygon", type=_polygon_arg, metavar="X,Y[,Z];...",
                   help="panel vertices in mm (default 0,0;1200,0;1200,800;0,800)")
    g.add_argument("--vertex-encoding", choices=list(VERTEX_ENCODINGS),
                   help="INFO;25 encoding (default xy; xyz on vendor-app-newer)")
    g.add_argument("--vertex-frame", choices=list(VERTEX_FRAMES),
                   help="axis map applied to the vertices (default identity)")
    g.add_argument("--vertex-noise-mm", type=_nonneg_float, metavar="MM",
                   help="uniform measurement error per axis (default 0)")
    g.add_argument("--vertex-noise-seed", type=int, metavar="N", help="noise seed (default 0)")
    g.add_argument("--polar-angle-unit", choices=["deg", "rad"],
                   help="angle unit of polar vertices (default deg)")
    g.add_argument("--tower-distance", type=_pos_float, metavar="MM",
                   help="INFO;27 value (default: the polygon's extent along X)")
    g.add_argument("--no-wait-t2-position", action="store_true",
                   help="send INFO;6 without waiting for SET T2 POSITION")
    g.add_argument("--info27-requires-t2", action="store_true",
                   help="hold INFO;27 until SET T2 POSITION arrives (exercises a timeout)")
    g.add_argument("--t2-before-wait", choices=["ignored", "accepted"],
                   help="what a SET T2 POSITION before the robot waits for it does "
                        "(default ignored)")
    g.add_argument("--detection-step-delay", type=_nonneg_float, metavar="S",
                   help="seconds between detection messages (default 0.01)")
    g.add_argument("--detection-frames", action="store_true",
                   help="position frames during detection and at the park pose")
    g.add_argument("--frames-outside-work", choices=list(_FRAMES_OUTSIDE_WORK_MODES),
                   help="position frames outside work (default off)")
    g.add_argument("--idle-frame-interval", type=_pos_float, metavar="S",
                   help="seconds between frames outside work (default 0.5)")
    g.add_argument("--park-pose", type=_pose_arg, metavar="X,Y,Z,ANGLE",
                   help="where the robot parks after panel detection (default: the last vertex)")
    g.add_argument("--detection-fault", action="append", metavar="FAULT",
                   help="inject a panel-detection fault; repeatable. One of: "
                        + ", ".join(f"{k}{'' if n == 0 else ':N' if n == 1 else ':N:CODE'}"
                                    for k, n in DETECTION_FAULTS.items()))
    g.add_argument("--no-require-panel-size", action="store_true",
                   help="no INFO;42 for an upload before SET PANEL SIZE")
    g.add_argument("--info42-at", choices=["header", "upload-end"],
                   help="when INFO;42 is sent (default header)")
    g.add_argument("--info42-starts-work", action="store_true",
                   help="run a program uploaded before SET PANEL SIZE anyway, after INFO;42 "
                        "(default: kept, not run)")
    g.add_argument("--no-replay-setup", action="store_true",
                   help="the connect-time replay never sends INFO;6, INFO;4 or INFO;24")


def _setup_kwargs(args: argparse.Namespace) -> Dict[str, object]:
    """The setup-model constructor options the CLI flags ask for (only those given)."""
    kw: Dict[str, object] = {}
    if args.setup_model:
        kw["setup_model"] = True
    for name in _SETUP_VALUE_FLAGS:
        value = getattr(args, name)
        if value is not None:
            kw[name] = value
    if args.encoder_not_calibrated:
        kw["encoder_calibrated"] = False
    if args.no_wait_t2_position:
        kw["wait_t2_position"] = False
    if args.info27_requires_t2:
        kw["info27_requires_t2"] = True
    if args.detection_frames:
        kw["detection_frames"] = True
    if args.detection_fault:
        kw["detection_faults"] = tuple(args.detection_fault)
    if args.no_require_panel_size:
        kw["require_panel_size_before_upload"] = False
    if args.info42_starts_work:
        kw["info42_starts_work"] = True
    if args.no_replay_setup:
        kw["replay_setup"] = False
    return kw


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
    _add_setup_arguments(ap)
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
    kwargs.update(_setup_kwargs(args))
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
    if sim.setup_model:
        _print_ascii("Setup phases modelled (spec 03 steps B-F); every setup value is a GUESS "
                     "(listed in the config lines of the event log).")
        if sim.detection_autostart is None:
            _print_ascii("Panel detection starts only when started at the robot, and this CLI "
                         "has no operator: pass --detection-autostart SECONDS to start it.")
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
