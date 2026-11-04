# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""Goliath control-protocol message layer (no I/O).

This module is a clean-room implementation of the Goliath robot's wire protocol
as documented in ``docs/protocol/`` of this repository. It contains:

* command builders (PC -> robot), returning the exact ASCII wire string;
* the INFO / ERROR / WARNING response code tables;
* a parser for inbound text messages (``INFO;<n>;...`` etc.); and
* decoders for the binary messages: position frames, tower frames, the 19-byte
  ``G...S`` global-status frame and the 48-byte ``GLOBAL STATUS HEAD/TALE``
  blob.

Everything here is pure (strings and bytes in, dataclasses out) so it can be
unit-tested without a socket or a real machine. Transport and framing live in
``transport.py`` and ``framing.py``.

Spec references are given as ``[NN]`` pointing at ``docs/protocol/NN-*.md``.
Where the firmware facts compiled from GFW 01.02.00 disagree with the spec,
the firmware facts win (they are summarised in the ``framing`` and
``transport`` module docstrings).

Public message types (all frozen dataclasses):

* :class:`TextMessage` -- ``kind`` ('INFO'/'ERR'/'WARN'/'TEXT'), ``raw``,
  ``code``, ``params``, ``terminated`` (False when the message boundary was
  inferred from quiet time or from the start of the next frame, because the
  robot sent no ``\\n``/NUL after it).
* :class:`PositionFrame` -- 22-byte (GFW 01.02.00) or 58-byte position frame.
* :class:`TowerFrame` -- 4-byte tower status/battery frame.
* :class:`GlobalStatusBlob` -- the 48-byte reply to ``GET GLOBAL SYS STATUS``
  on GFW 01.02.00: ten raw status bytes, no tower firmware.
* :class:`GlobalStatusFrame` -- the 19-byte ``G...S`` frame described for
  other firmware (no builder for it exists in GFW 01.02.00).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum

# --------------------------------------------------------------------------- #
# Commands (PC -> robot). See docs/protocol/02-message-reference.md.
#
# The robot accepts plain ASCII, ';'-separated. No terminator is appended by the
# host (the robot frames inbound commands itself); callers that want to be safe
# can send one command per TCP segment and pace them, which is what the vendor
# app effectively does.
# --------------------------------------------------------------------------- #


def _fmt(template: str, *args: object) -> str:
    """Format a command the way the vendor app does (invariant, '.' decimals).

    Floats are rendered with ``repr``-free fixed formatting is intentionally
    *not* applied here: the protocol carries integers for most arguments and the
    caller is responsible for rounding coordinates. We only guarantee a '.'
    decimal separator by relying on Python's default str()/format which is
    locale-independent.
    """
    return template.format(*args)


class Commands:
    """Builders for every command the robot understands.

    Each method returns the exact wire string. Grouping mirrors the message
    reference. Methods are static; this is a namespace, not a stateful object.
    """

    # -- connection / system -------------------------------------------------
    @staticmethod
    def info() -> str:
        return "INFO"

    @staticmethod
    def sys_reset() -> str:
        return "CMD SYS RESET"

    @staticmethod
    def get_global_sys_status() -> str:
        return "GET GLOBAL SYS STATUS"

    @staticmethod
    def print_sys_report() -> str:
        return "PRINT SYS REPORT"

    @staticmethod
    def enable_info_log(enabled: bool) -> str:
        return _fmt("ENABLE INFO LOG;{0}", 1 if enabled else 0)

    @staticmethod
    def get_ssids() -> str:
        return "GET SSIDS"

    # -- Wi-Fi provisioning (raw strings, not in the eCMD table) -------------
    @staticmethod
    def provision_network(ssid: str, password: str, auth_type: int) -> str:
        """``SSID=<hex>&PWD=<hex>&AUTH_TYPE=<n>``. auth_type: 1=open, 2=WPA/WPA2, 3=WEP."""
        if auth_type not in (1, 2, 3):
            raise ValueError("auth_type must be 1 (open), 2 (WPA/WPA2) or 3 (WEP)")
        return "SSID={0}&PWD={1}&AUTH_TYPE={2}".format(
            _hex_each(ssid), _hex_each(password), auth_type
        )

    # -- tower pairing & geometry -------------------------------------------
    @staticmethod
    def set_rf_channels(t1_channel: int, t2_channel: int) -> str:
        return _fmt("SET RF CHANNELS;{0};{1}", int(t1_channel), int(t2_channel))

    @staticmethod
    def set_t2_position(along_x: bool) -> str:
        return _fmt("SET T2 POSITION;{0}", 1 if along_x else 0)

    @staticmethod
    def discard_towers_distance() -> str:
        return "DISCARD TWRS DISTANCE"

    # -- panel & calibration -------------------------------------------------
    @staticmethod
    def set_panel_size(x_max: int, y_max: int) -> str:
        return _fmt("SET PANEL SIZE;{0};{1}", int(round(x_max)), int(round(y_max)))

    @staticmethod
    def get_panel_size() -> str:
        return "GET PANEL SIZE"

    @staticmethod
    def read_index_calibration() -> str:
        return "READ INDEX CALIBRATION"

    @staticmethod
    def set_index_calibration_value(value: int) -> str:
        return _fmt("SET INDEX CALIBRATION VALUE;{0}", int(value))

    @staticmethod
    def read_spindle_calib() -> str:
        return "READ SPINDLE CALIB"

    @staticmethod
    def set_spindle_distance(value: float) -> str:
        return _fmt("SET SPINDLE DISTANCE;{0}", value)

    @staticmethod
    def get_cage_status() -> str:
        return "GET SPINDLE CAGE STATUS"

    @staticmethod
    def get_z_backlash() -> str:
        return "GET Z BACKLASH"

    @staticmethod
    def set_z_backlash(value: float) -> str:
        return _fmt("SET Z BACKLASH;{0}", value)

    @staticmethod
    def get_z_plate_offset() -> str:
        return "GET Z PLATE OFFSET"

    @staticmethod
    def set_z_plate_offset(value: float) -> str:
        return _fmt("SET Z PLATE OFFSET;{0}", value)

    # -- jog -----------------------------------------------------------------
    @staticmethod
    def enter_jog() -> str:
        return "ENTER JOG"

    @staticmethod
    def exit_jog() -> str:
        return "EXIT JOG"

    @staticmethod
    def enable_manual_jog() -> str:
        return "ENABLE MANUAL JOG"

    @staticmethod
    def disable_manual_jog() -> str:
        return "DISABLE MANUAL JOG"

    @staticmethod
    def goto_target(x: float, y: float, z: float, angle: float) -> str:
        return _fmt("GOTO TARGET;{0};{1};{2};{3}", x, y, z, angle)

    @staticmethod
    def jog_stop() -> str:
        return "JOG STOP GOLIATH"

    @staticmethod
    def free_z_axis() -> str:
        return "FREE Z AXIS"

    # -- spindle / dust / work ----------------------------------------------
    @staticmethod
    def enable_spindle() -> str:
        return "ENABLE SPINDLE"

    @staticmethod
    def disable_spindle() -> str:
        return "DISABLE SPINDLE"

    @staticmethod
    def enable_aspiration() -> str:
        return "ENABLE ASPIRATION"

    @staticmethod
    def disable_aspiration() -> str:
        return "DISABLE ASPIRATION"

    @staticmethod
    def gcode_upload(filesize_bytes: int) -> str:
        return _fmt("GCODE UPLOAD;{0}", int(filesize_bytes))

    @staticmethod
    def gcode_continue(resume: bool) -> str:
        """``GCODE CONTINUE;1`` to resume a loaded program, ``;0`` to discard/wait."""
        return _fmt("GCODE CONTINUE;{0}", 1 if resume else 0)

    @staticmethod
    def abort_work() -> str:
        return "ABORT WORK"

    @staticmethod
    def send_pos() -> str:
        return "SEND TCP POS"


def _hex_each(text: str) -> str:
    """Per-character uppercase hex, as the provisioning handoff expects."""
    return "".join("{0:02X}".format(b) for b in text.encode("utf-8"))


# --------------------------------------------------------------------------- #
# Response code tables. See docs/protocol/02-message-reference.md.
# Codes are the WIRE values (which differ from any enum ordinal).
# --------------------------------------------------------------------------- #


class Info(IntEnum):
    OK = 0
    ENTER_SETUP = 1
    EXIT_SETUP = 2
    ENTER_PAIRING = 3
    PAIRING_DONE = 4
    ENTER_PANEL_DETECTION = 5
    PANEL_DETECTION_DONE = 6
    ENTER_GCODE_UPLOAD = 7
    GCODE_UPLOAD_DONE = 8
    EXIT_GCODE_UPLOAD = 9
    ENTER_DRILL_CAL = 10
    EXIT_DRILL_CAL = 11
    ENTER_DRILL_ZERO_CHECK = 12
    ZERO_VALUE_SET = 13
    EXIT_DRILL_ZERO_CHECK = 14
    ENTER_MODE_WORK = 15
    EXIT_MODE_WORK = 16
    ENTER_WORK_CYCLE = 17
    EXIT_WORK_CYCLE = 18
    ENTER_WORK_PAUSED = 19
    EXIT_WORK_PAUSED = 20
    ENTER_WORK_HALTED = 21
    EXIT_WORK_HALTED = 22
    WORK_COMPLETE = 23
    ENCODER_CALIBRATED = 24
    VERT = 25
    PANEL_SIZE = 26
    TWRS_DISTANCE = 27
    SYS_INFO = 28
    AP = 31
    JOG_TARGET_REACHED = 32
    NEED_ENCODER_CALIBRATION = 33
    APP_INFO = 34
    JOG_MODE = 35
    SANITY_CHECK = 40
    ASK_GCODE_RESTART = 41
    PANEL_SIZE_NOT_RECEIVED = 42
    Z_CALIBRATION_FINISHED = 43
    SMART_POS = 1000
    SMART_TWR = 1001
    SMART_GLOBAL_STATUS = 1002


class Error(IntEnum):
    GCODE_UPLOAD = 0
    CMD_NOT_EXECUTED = 1
    CAGE_OPENED = 2
    T1_DISCONNECTED = 3
    T2_DISCONNECTED = 4
    REFERENCE_DIVERGENCE = 5
    T1_WRONG_LENGTH = 6
    T2_WRONG_LENGTH = 7
    SUM_TWRS_LESS_THAN_DISTANCE = 8
    POS_OVER_THRESH = 9
    MOTOR_STALL = 10
    CANNOT_LIFT_Z = 11
    DRILL_NOT_COMPLIANT = 12
    MOTOR_OCD = 13
    MOTOR_THERMAL_WARN = 14
    MOTOR_NOT_CALIBRATED = 15
    POS_OUT_OF_PANEL = 16
    GOLIATH_IN_HALT = 17
    STALL_NOT_DETECTED = 18
    MESSAGE_NOT_CONSTRUCTED = 19
    Z_OFFSET_VALUE = 20
    TEMPERATURE_HIGH = 21


class Warning(IntEnum):
    T1_UNDER_20 = 0
    T2_UNDER_20 = 1
    T1_UNDER_10 = 2
    T2_UNDER_10 = 3


class TowerStatus(IntEnum):
    OK = 0
    CHARGING = 1
    CHARGED = 2
    SHUTDOWN_RISK = 3


#: Errors a controller should treat as safety-critical (stop, surface loudly).
SAFETY_ERRORS = frozenset(
    {
        Error.CAGE_OPENED,
        Error.POS_OVER_THRESH,
        Error.CANNOT_LIFT_Z,
        Error.POS_OUT_OF_PANEL,
        Error.GOLIATH_IN_HALT,
        Error.TEMPERATURE_HIGH,
        Error.MOTOR_OCD,
        Error.MOTOR_THERMAL_WARN,
    }
)


# --------------------------------------------------------------------------- #
# Parsed message types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TextMessage:
    """A text response from the robot.

    ``kind`` is 'INFO', 'ERR', 'WARN', or 'TEXT' (free-form reply, e.g.
    ``cfg read index : 28500`` or a cage-status line). For INFO/ERR/WARN,
    ``code`` is the integer wire code and ``params`` the ';'-separated tail.

    ``terminated`` is True when the robot ended the message on the wire, with
    ``\\n`` or with the NUL padding of a 512-byte send. It is False when no
    terminator arrived and the framer cut the message because the connection
    went quiet, or because a recognisable binary frame started right after
    it. GFW 01.02.00 sends several free-form replies without any terminator
    (``count: %d``, ``Moving stepper!``, ``xmax=... ymax=...``), so False is
    expected for those; it means "boundary inferred", not "damaged".
    """

    kind: str
    raw: str
    code: int | None = None
    params: list[str] = field(default_factory=list)
    terminated: bool = True


def parse_text_message(line: str, *, terminated: bool = True) -> TextMessage:
    """Parse one inbound text line (without the trailing newline).

    ``terminated`` is recorded on the result (see :class:`TextMessage`).
    """
    text = line.strip("\r\n").lstrip("\x00").strip()
    for kind in ("INFO", "ERR", "WARN"):
        if text.startswith(kind):
            parts = text.split(";")
            code: int | None = None
            params: list[str] = []
            if len(parts) >= 2:
                try:
                    code = int(parts[1])
                except ValueError:
                    code = None
                params = parts[2:]
            return TextMessage(kind=kind, raw=text, code=code, params=params,
                               terminated=terminated)
    return TextMessage(kind="TEXT", raw=text, terminated=terminated)


# --------------------------------------------------------------------------- #
# Binary telemetry frames. See docs/protocol/01-transport-and-framing.md and
# the GFW 01.02.00 firmware facts (framing module docstring), which win where
# they disagree with the spec.
# --------------------------------------------------------------------------- #

POS_START, POS_END = 0x7B, 0x7D  # '{' '}'
TWR_START, TWR_END = 0x3C, 0x3E  # '<' '>'
GLOBAL_START, GLOBAL_END = 0x47, 0x53  # 'G' 'S'

POS_LEN_FULL = 58
POS_LEN_SHORT = 22
TWR_LEN = 4
GLOBAL_LEN = 19

#: Tower id bytes of a tower frame: 0xAA = T1, 0xBB = T2.
TOWER_IDS = {0xAA: 1, 0xBB: 2}

#: The reply to ``GET GLOBAL SYS STATUS`` on GFW 01.02.00 (firmware facts):
#: ``HEAD`` (19 bytes, NUL included) + 10 status bytes + ``TALE`` (19 bytes).
GLOBAL_BLOB_HEAD = b"GLOBAL STATUS HEAD\x00"
GLOBAL_BLOB_TALE = b"GLOBAL STATUS TALE\x00"
GLOBAL_BLOB_FIELD_COUNT = 10
GLOBAL_BLOB_LEN = len(GLOBAL_BLOB_HEAD) + GLOBAL_BLOB_FIELD_COUNT + len(GLOBAL_BLOB_TALE)  # 48

#: The ten status names of the vendor's ``INFO;1002`` text template, in template
#: order ([01] "Frame 3"). PER VENDOR TEMPLATE, UNVERIFIED: the firmware writes
#: ten bytes (from struct offsets 8, 0, 4, 16, 12, 20, 24, 28, 32, 36 of one
#: status struct) and the template has ten names, but nothing ties byte N to
#: name N: the vendor app never decoded these bytes (it hard-coded the
#: fields). Confirm by capturing blobs in known machine states before relying
#: on any name.
GLOBAL_BLOB_FIELD_NAMES_UNVERIFIED = (
    "pairing",
    "t1_paired",
    "t2_paired",
    "pd_status",
    "panel_detected",
    "gcode_uploaded",
    "work_status",
    "conn_network",
    "resume_after_reset",
    "encoder_calibrated",
)


def _status_and_battery(byte: int) -> tuple[int, float]:
    """Decode a tower status+battery byte.

    Per the spec (verified): status is the two most-significant bits
    (``byte >> 6``), battery percent is the low six bits (``byte & 0x3F``)
    times 1.5625.
    """
    status = (byte >> 6) & 0x03
    battery = (byte & 0x3F) * 1.5625
    return status, battery


def _status_battery_byte(status: int, battery: float) -> int:
    """Inverse of :func:`_status_and_battery` (battery is quantised to 1.5625 %)."""
    level = max(0, min(63, int(round(battery / 1.5625))))
    return ((status & 0x03) << 6) | level


@dataclass(frozen=True)
class PositionFrame:
    """SMART_INFO_POS (id 1000): live pose + current G-code line (+ optional temp/battery)."""

    x: float
    y: float
    z: float
    angle: float
    gcode_line: int
    temperature: int | None = None
    t1_status: int | None = None
    t1_battery: float | None = None
    t2_status: int | None = None
    t2_battery: float | None = None

    @classmethod
    def decode(cls, data: bytes) -> "PositionFrame":
        if len(data) not in (POS_LEN_FULL, POS_LEN_SHORT):
            raise ValueError(f"position frame must be {POS_LEN_SHORT} or {POS_LEN_FULL} bytes")
        if data[0] != POS_START or data[-1] != POS_END:
            raise ValueError("position frame sentinels missing ('{' ... '}')")
        x, y, z, angle = struct.unpack_from("<ffff", data, 1)
        (line,) = struct.unpack_from("<i", data, 17)
        if len(data) == POS_LEN_SHORT:
            return cls(x=x, y=y, z=z, angle=angle, gcode_line=line)
        (temp,) = struct.unpack_from("<i", data, 21)
        t1s, t1b = _status_and_battery(data[25])
        t2s, t2b = _status_and_battery(data[26])
        return cls(
            x=x, y=y, z=z, angle=angle, gcode_line=line, temperature=temp,
            t1_status=t1s, t1_battery=t1b, t2_status=t2s, t2_battery=t2b,
        )

    def encode(self) -> bytes:
        """Serialise to the wire format (58 bytes if temperature is set, else 22).

        Used by the simulator and tests. Bytes 27-56 of the full frame carry
        fields not yet identified; they are emitted as zero.
        """
        full = self.temperature is not None
        buf = bytearray(POS_LEN_FULL if full else POS_LEN_SHORT)
        buf[0] = POS_START
        buf[-1] = POS_END
        struct.pack_into("<ffff", buf, 1, self.x, self.y, self.z, self.angle)
        struct.pack_into("<i", buf, 17, self.gcode_line)
        if full:
            struct.pack_into("<i", buf, 21, self.temperature)
            buf[25] = _status_battery_byte(self.t1_status or 0, self.t1_battery or 0.0)
            buf[26] = _status_battery_byte(self.t2_status or 0, self.t2_battery or 0.0)
        return bytes(buf)


@dataclass(frozen=True)
class TowerFrame:
    """SMART_INFO_TWR (id 1001): one tower's status + battery."""

    tower: int  # 1 or 2
    status: int
    battery: float

    @classmethod
    def decode(cls, data: bytes) -> "TowerFrame":
        if len(data) != TWR_LEN or data[0] != TWR_START or data[-1] != TWR_END:
            raise ValueError("tower frame must be 4 bytes '<' id status '>'")
        tower = TOWER_IDS.get(data[1])
        if tower is None:
            raise ValueError(f"unknown tower id byte 0x{data[1]:02X}")
        status, battery = _status_and_battery(data[2])
        return cls(tower=tower, status=status, battery=battery)

    def encode(self) -> bytes:
        tower_byte = {1: 0xAA, 2: 0xBB}[self.tower]
        return bytes(
            [TWR_START, tower_byte, _status_battery_byte(self.status, self.battery), TWR_END]
        )


@dataclass(frozen=True)
class GlobalStatusBlob:
    """The 48-byte reply to ``GET GLOBAL SYS STATUS`` on GFW 01.02.00.

    Wire form: ``b"GLOBAL STATUS HEAD\\0"`` + status bytes +
    ``b"GLOBAL STATUS TALE\\0"``, sent with an explicit length (no padding,
    no terminator) to the connection that asked. GFW 01.02.00 writes exactly
    :data:`GLOBAL_BLOB_FIELD_COUNT` (10) status bytes; :meth:`decode` accepts
    another count so that a different firmware's blob is still delivered
    (check ``len(fields)``).

    ``fields`` are the status bytes in wire order, as plain integers. Their
    meaning is not established: see :data:`GLOBAL_BLOB_FIELD_NAMES_UNVERIFIED`
    and :meth:`fields_by_vendor_name_unverified`. Unlike the 19-byte
    :class:`GlobalStatusFrame`, this blob carries **no tower firmware
    version**; tower firmware is unavailable from GFW 01.02.00 by this route.
    """

    raw: bytes
    fields: tuple[int, ...]

    @classmethod
    def decode(cls, data: bytes) -> "GlobalStatusBlob":
        head, tale = GLOBAL_BLOB_HEAD, GLOBAL_BLOB_TALE
        if len(data) < len(head) + len(tale):
            raise ValueError("global-status blob shorter than HEAD + TALE")
        if not data.startswith(head):
            raise ValueError("global-status blob must start with 'GLOBAL STATUS HEAD\\0'")
        if not data.endswith(tale):
            raise ValueError("global-status blob must end with 'GLOBAL STATUS TALE\\0'")
        return cls(raw=bytes(data), fields=tuple(data[len(head) : len(data) - len(tale)]))

    @classmethod
    def build(cls, fields: tuple[int, ...]) -> "GlobalStatusBlob":
        """Construct a blob (for the simulator and tests)."""
        return cls.decode(GLOBAL_BLOB_HEAD + bytes(fields) + GLOBAL_BLOB_TALE)

    def encode(self) -> bytes:
        return self.raw

    def fields_by_vendor_name_unverified(self) -> dict[str, int] | None:
        """Map ``fields`` onto the vendor ``INFO;1002`` template names.

        UNVERIFIED: pairing byte N with template name N is an inference from
        the counts matching (ten and ten), not an observation. Use it to label
        captures for comparison, never to drive behaviour. Returns None when
        the blob does not carry exactly ten status bytes.
        """
        if len(self.fields) != len(GLOBAL_BLOB_FIELD_NAMES_UNVERIFIED):
            return None
        return dict(zip(GLOBAL_BLOB_FIELD_NAMES_UNVERIFIED, self.fields))


@dataclass(frozen=True)
class GlobalStatusFrame:
    """SMART_INFO_GLOBAL_STATUS (id 1002): machine flags + tower firmware.

    **Not sent by GFW 01.02.00**, which answers ``GET GLOBAL SYS STATUS`` with
    the 48-byte :class:`GlobalStatusBlob` and contains no builder for this
    frame. It is kept because the spec documents it for other firmware.

    The 19-byte binary frame is ``G`` + 17 bytes + ``S``. Only the two tower
    firmware versions are known: t1 = bytes [14,13,12], t2 = bytes [17,16,15]
    (major, minor, patch).

    **Bytes 1-11 are not decoded by anyone yet.** The vendor app's own decoder
    reads only the tower firmware and hard-codes every other field it reports
    (pairing, panel detected, G-code uploaded, work status, ...) to 0/false. So
    the documented field *names* exist but their byte positions are unknown.
    They are exposed raw as :attr:`flags_raw`; compare frames captured in
    different machine states to learn the layout.
    """

    raw: bytes
    t1_firmware: tuple[int, int, int]
    t2_firmware: tuple[int, int, int]

    @property
    def flags_raw(self) -> bytes:
        """The 11 undecoded bytes between the 'G' sentinel and the firmware fields."""
        return self.raw[1:12]

    @classmethod
    def decode(cls, data: bytes) -> "GlobalStatusFrame":
        if len(data) != GLOBAL_LEN or data[0] != GLOBAL_START or data[-1] != GLOBAL_END:
            raise ValueError("global-status frame must be 19 bytes 'G' ... 'S'")
        t1 = (data[14], data[13], data[12])
        t2 = (data[17], data[16], data[15])
        return cls(raw=bytes(data), t1_firmware=t1, t2_firmware=t2)

    @classmethod
    def build(
        cls,
        t1_firmware: tuple[int, int, int],
        t2_firmware: tuple[int, int, int],
        flags_raw: bytes = bytes(11),
    ) -> "GlobalStatusFrame":
        """Construct a frame (for the simulator and tests)."""
        if len(flags_raw) != 11:
            raise ValueError("flags_raw must be 11 bytes")
        buf = bytearray(GLOBAL_LEN)
        buf[0] = GLOBAL_START
        buf[1:12] = flags_raw
        buf[12], buf[13], buf[14] = t1_firmware[2], t1_firmware[1], t1_firmware[0]
        buf[15], buf[16], buf[17] = t2_firmware[2], t2_firmware[1], t2_firmware[0]
        buf[18] = GLOBAL_END
        return cls.decode(bytes(buf))

    def encode(self) -> bytes:
        return self.raw
