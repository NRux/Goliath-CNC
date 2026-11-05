# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""Command safety layer: which text commands a client may send at all.

:class:`~goliath_link.client.GoliathClient` runs every text command it sends
through three checks, in this order, *before* the firmware-version gate
(:meth:`~goliath_link.client.FirmwareCapabilities.supports_command`). A refusal
raises :class:`CommandRefusedError` and nothing reaches the wire.

1. **The denylist** (:data:`NEVER_SEND`), always on. From
   ``docs/protocol/09-firmware-01.02.00.md``, section "The command table":

   * section 2a except ``INFO`` -- in the vendor app's command table but never
     sent by it (``SET SETUP PHASE``, ``SET VERTEX``, ``GET PANEL SIZE`` ...);
   * all of section 2b -- 48 commands the vendor app does not know, with
     unknown semantics (``MOVE Z``, ``GCODE START``, ``EFLASH ERASE``,
     ``SET PARAM MEMORY`` ...);
   * ``LOAD`` from section 1 -- the vendor sent it only to flash the Wi-Fi
     module ("never outside a deliberate, prepared flash", spec 02);
   * section 3 -- the ten response and console strings in the table
     (``SYS RESET``, ``SET PID``, ``OK``, the empty name ...). They have no
     handler on GFW 01.02.00, the vendor app never sends them, and what they
     do on any other firmware is unknown. The project design lists only 2a,
     2b and ``LOAD``; section 3 is added here as the safer reading (nothing
     legitimate needs these names, and ``SYS RESET`` and ``SET PID`` read as
     hazards on newer firmware).

   Matching is deliberately broader than the firmware's: the command is split
   at NUL and line breaks (any of them could end or separate a command on some
   firmware), and in every piece the name is read both ways a firmware might
   -- the text before the first ``;`` (spec 09) and the first C ``strtok``
   token, which skips leading ``;`` (the simulator) -- and compared with
   surrounding whitespace removed, inner whitespace runs collapsed to one space
   and case folded. So ``move z;1``, ``" MOVE  Z\\r\\n"``, ``";MOVE Z"`` and
   ``"INFO\\nMOVE Z"`` are all refused. Broader matching can only refuse more.
   Names outside the GFW 01.02.00 table (the vendor's commands for newer
   firmware, a typo) are not on the denylist: the firmware gate, the strict
   set and the policy decide about them.

   The robot reads one receive buffer as one command (spec 01), and TCP does
   not keep sends apart, so the always-on layer also checks what the robot
   could read across send boundaries (:func:`wire_decision`):

   * a command must be ASCII (the table is ASCII; spec 01: send ASCII bytes),
     and the client sends exactly those bytes, whatever the transport's
     encoding, so the text checked is the text on the wire;
   * a command may be at most :data:`MAX_COMMAND_BYTES` long: a longer one
     could be cut by the robot's receive buffer, and its tail parsed as a
     command of its own;
   * a denylisted name split over several sends (``"MOVE"``, then ``" Z;1"``)
     is refused: the robot may receive them as one buffer
     (:class:`RecentSends` keeps the sends a later one could still join);
   * a G-code program may not contain a denylisted name anywhere in a line
     (:func:`program_decision`): outside upload mode -- an upload that ended
     early, or a header the robot did not take (GFW 01.02.00 does not
     acknowledge it) -- every block is parsed as a command, and a block may
     start or end anywhere. Goliath dialect words are one letter and a number
     (spec 05), so no dialect program is affected.

2. **The strict set** (:data:`STRICT_COMMANDS`), when the client is built with
   ``strict_commands=True``: only spec 09 section 1 (the commands the vendor
   app sends) plus ``INFO``, spelled the way GFW 01.02.00 compares names
   (printable ASCII, the name exactly a table name in any case). This
   excludes the commands the vendor app sends only to firmware newer than
   01.02.00 (``ABORT WORK``, ``GCODE CONTINUE`` ...: they are not in section
   1), so a strict client cannot stop a job from software or upload to such
   firmware (``upload_gcode`` sends ``GCODE CONTINUE;0`` there first).

3. **The command policy** (:class:`CommandPolicy`), injected by the caller and
   swapped per phase (:meth:`GoliathClient.set_command_policy`). It sees the
   full command, so it can bind arguments: :class:`PhasePolicy` with
   :func:`bind` allows, for example, ``SET PANEL SIZE`` only as
   ``SET PANEL SIZE;1200;800``.

The layer covers the text commands a :class:`GoliathClient` sends (``send``,
``request``, the convenience methods, ``stop_work`` and the commands of
``upload_gcode``) and, through the denylist only, the program an upload
streams. It does not cover a raw
:meth:`GoliathTransport.send <goliath_link.transport.GoliathTransport.send>`,
which bypasses the client entirely, or what other clients send to the same
robot. The read-only probe has its own, narrower allowlist
(:data:`goliath_link.probe.READ_ONLY_COMMANDS`).

The tables describe firmware **GFW 01.02.00**, the only image that survives.
Nothing in them has been observed on a robot yet.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from types import MappingProxyType
from typing import (Deque, Dict, Iterable, Mapping, Optional, Protocol, Sequence, Tuple, Union,
                    runtime_checkable)

# --------------------------------------------------------------------------- #
# The GFW 01.02.00 command table, by section of spec 09 (index: name)
# --------------------------------------------------------------------------- #

_SPEC09 = "docs/protocol/09-firmware-01.02.00.md"

#: Spec 09 section 1: the 26 table commands the vendor app sends.
SPEC09_VENDOR_SENT: Mapping[int, str] = MappingProxyType({
    4: "ENABLE SPINDLE", 5: "DISABLE SPINDLE", 11: "LOAD", 23: "GCODE UPLOAD",
    29: "GOTO TARGET", 30: "EXIT JOG", 35: "SET PANEL SIZE", 45: "SET RF CHANNELS",
    46: "SET T2 POSITION", 48: "RESET NET KEY", 49: "ENABLE MANUAL JOG", 50: "ENTER JOG",
    51: "ENABLE ASPIRATION", 52: "DISABLE ASPIRATION", 58: "SET INDEX CALIBRATION VALUE",
    59: "JOG STOP GOLIATH", 60: "DISABLE MANUAL JOG", 65: "READ INDEX CALIBRATION",
    66: "POLL CAGE", 67: "STOP POLL CAGE", 70: "PRINT SYS REPORT", 76: "READ SPINDLE CALIB",
    79: "FREE Z AXIS", 82: "GET SSIDS", 83: "GET SPINDLE CAGE STATUS",
    86: "GET GLOBAL SYS STATUS",
})

#: Spec 09 section 2a: in the vendor app's command table, never sent by it.
SPEC09_VENDOR_TABLE_NEVER_SENT: Mapping[int, str] = MappingProxyType({
    6: "INFO", 31: "SET SETUP PHASE", 32: "SET WORK PHASE", 33: "SET VERTEX",
    34: "GET VERTEXES", 36: "GET PANEL SIZE", 37: "ENABLE WORK MON", 38: "ENABLE INFO LOG",
    39: "TOWERS POSITION", 40: "GET TOWERS DISTANCE",
})

#: Spec 09 section 2b: unknown to the vendor app, semantics unknown (48 commands).
SPEC09_UNKNOWN_TO_VENDOR: Mapping[int, str] = MappingProxyType({
    7: "SET RTC TIME", 8: "SET RTC DATE", 9: "GET RTC DATE", 10: "BOOT", 12: "READ BACKUP",
    13: "EFLASH ERASE", 14: "MOVE Z", 15: "RUN STEPPER", 16: "STOP STEPPER",
    17: "GOTO POS STEPPER", 18: "START MOTORS", 19: "MOVE STEPPER", 20: "SET STEP CLOCK MODE",
    21: "SPINDLE START", 22: "SPINDLE STOP", 24: "GCODE START", 25: "GCODE STOP",
    26: "GCODE RESTART", 27: "GCODE READ", 28: "ZERO SET", 41: "SET START POS",
    42: "GET START POS", 43: "WORK SPEED MOD", 44: "REQ UPD POS", 47: "BTN CLICK",
    53: "ENTER ENCODER CALIBRATION", 54: "EXIT ENCODER CALIBRATION", 55: "SCAN WIFI",
    56: "ENTER MANUAL ENC CALIBRATION", 57: "EXIT MANUAL ENC CALIBRATION", 61: "SPINDLE ON",
    62: "SPINDLE OFF", 63: "ASP ON", 64: "ASP OFF", 68: "ENTER TEST PHASE",
    69: "EXIT TEST PHASE", 71: "POLL ENCODER", 72: "STOP POLL ENCODER", 73: "TEST Z CAL",
    74: "TEST RF", 75: "SPINDLE CALIB VALUE", 77: "PRINT ENCODER CAN", 78: "LAST STATUS",
    80: "SANITY CHECK", 81: "LOAD WIFI", 84: "GET PARAM MEMORY", 85: "SET PARAM MEMORY",
    87: "RESET PARAM MEMORY",
})

#: Spec 09 section 3: table entries with no handler on GFW 01.02.00 (response
#: and console strings; record 88 is the empty name).
SPEC09_NOT_DISPATCHED: Mapping[int, str] = MappingProxyType({
    0: "COMMAND MODE ENTERED", 1: "EXIT COMMAND MODE", 2: "SYS RESET", 3: "SET PID",
    88: "", 89: "OK", 90: "INVALID COMMAND", 91: "INVALID VALUE", 92: "ERROR",
    93: "FIRST BACKUP",
})

#: Spec 09 "Vendor-app commands that are not in the table": the vendor app
#: sends these to firmware newer than 01.02.00 (no image of which survives).
#: Not denylisted; the firmware-version gate decides whether they may be sent.
VENDOR_SENT_NOT_ON_GFW_01_02_00: frozenset = frozenset({
    "ABORT WORK", "GCODE CONTINUE", "SEND TCP POS", "GET Z BACKLASH", "SET Z BACKLASH",
    "GET Z PLATE OFFSET", "SET Z PLATE OFFSET", "SET SPINDLE DISTANCE",
    "DISCARD TWRS DISTANCE", "CMD SYS RESET",
})

#: The names the vendor app sends from the GFW 01.02.00 table (spec 09 section 1).
VENDOR_SENT: frozenset = frozenset(SPEC09_VENDOR_SENT.values())

_REASON_2A = (f"spec 09 section 2a ({_SPEC09}): in the vendor app's command table but never "
              "sent by it; its behaviour has not been validated by anyone (only INFO, verified "
              "read-only, is exempt)")
_REASON_2B = (f"spec 09 section 2b ({_SPEC09}): present in firmware GFW 01.02.00 but unknown "
              "to the vendor app; its semantics are unknown")
_REASON_LOAD = (f"spec 09 section 1 ({_SPEC09}): LOAD switches the robot into Wi-Fi "
                "firmware-load mode; the vendor app sent it only for an over-Wi-Fi flash "
                "(spec 02: never outside a deliberate, prepared flash)")
_REASON_3 = (f"spec 09 section 3 ({_SPEC09}): a response or console string in the GFW 01.02.00 "
             "command table with no handler there; the vendor app never sends it and what it "
             "does on any other firmware is unknown")
_REASON_EMPTY = ("an empty command name (nothing before the first ';'; record 88 of the GFW "
                 f"01.02.00 table, spec 09 section 3, {_SPEC09}): nothing a robot should be sent")


def _never_send_reasons() -> Dict[str, str]:
    reasons: Dict[str, str] = {}
    for name in SPEC09_VENDOR_TABLE_NEVER_SENT.values():
        if name != "INFO":
            reasons[name] = _REASON_2A
    for name in SPEC09_UNKNOWN_TO_VENDOR.values():
        reasons[name] = _REASON_2B
    reasons["LOAD"] = _REASON_LOAD
    for name in SPEC09_NOT_DISPATCHED.values():
        reasons[name] = _REASON_EMPTY if name == "" else _REASON_3
    return reasons


#: Why each denylisted name is refused (the spec section it comes from).
NEVER_SEND_REASONS: Mapping[str, str] = MappingProxyType(_never_send_reasons())

#: The hard denylist: never sent, whatever the caller, policy or firmware.
NEVER_SEND: frozenset = frozenset(NEVER_SEND_REASONS)

#: ``strict_commands=True``: spec 09 section 1 plus ``INFO``, minus the denylist.
STRICT_COMMANDS: frozenset = (VENDOR_SENT | {"INFO"}) - NEVER_SEND

assert len(SPEC09_VENDOR_SENT) == 26 and len(SPEC09_UNKNOWN_TO_VENDOR) == 48
assert sorted(list(SPEC09_VENDOR_SENT) + list(SPEC09_VENDOR_TABLE_NEVER_SENT)
              + list(SPEC09_UNKNOWN_TO_VENDOR) + list(SPEC09_NOT_DISPATCHED)) == list(range(94))
assert not (VENDOR_SENT_NOT_ON_GFW_01_02_00 & NEVER_SEND)
assert "INFO" in STRICT_COMMANDS and "LOAD" not in STRICT_COMMANDS

# --------------------------------------------------------------------------- #
# Reading a command the way a robot might
# --------------------------------------------------------------------------- #

#: Characters that end or separate a command on some conceivable firmware: NUL
#: (GFW 01.02.00 parses a C string), line breaks and the other control
#: characters except tab, and the Unicode line separators.
_SEGMENT_BREAK = re.compile("[\x00-\x08\x0a-\x1f\x7f\x85\u2028\u2029]")
_LEADING = re.compile(r"^[\s;]+")
_PRINTABLE_ASCII = re.compile(r"[\x20-\x7e]*")
_CANONICAL_NAME = re.compile(r"[A-Z0-9][A-Z0-9 ]*[A-Z0-9]|[A-Z0-9]")
_PLAIN_ARG = re.compile(r"[\x21-\x3a\x3c-\x7e]+")  # printable ASCII, no space, no ';'
_PASSWORD = re.compile(r"(PWD=)[^&;\s]*", re.IGNORECASE)


def _plain_name(segment: str) -> str:
    """The text before the first ';' (spec 09's reading), whitespace normalised."""
    return " ".join(segment.split(";", 1)[0].split()).upper()


def _segment_name(segment: str) -> str:
    """The first ``strtok(buf, ";")`` token (the simulator's reading), whitespace
    normalised; leading whitespace is skipped as well as leading ';'."""
    return _plain_name(_LEADING.sub("", segment))


def _segments(command: str) -> Tuple[str, ...]:
    return tuple(seg for seg in _SEGMENT_BREAK.split(command) if seg.strip())


def normalized_names(command: str) -> Tuple[str, ...]:
    """Every command name some robot could read in ``command``, normalised.

    The text is split at NUL, line breaks and other control characters; in
    each non-blank piece the name is the text before the first ``;`` (leading
    ``;`` and whitespace skipped), with whitespace runs collapsed to one space
    and upper-cased. A command with no name at all gives ``("",)``.
    """
    names = tuple(_segment_name(seg) for seg in _segments(command))
    return names or ("",)


def command_name(command: str) -> str:
    """The name exactly as GFW 01.02.00 compares it: the text before the
    first ``;``, upper-cased, nothing else removed."""
    return command.split(";", 1)[0].upper()


def redact(command: str) -> str:
    """``command`` with a Wi-Fi key (``PWD=...`` of the provisioning string) hidden."""
    return _PASSWORD.sub(r"\1<redacted>", command)


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PolicyDecision:
    """Whether a command may be sent, and why.

    ``allowed`` must be a real ``bool``: an unknown is never read as "yes", so
    ``None``, ``1`` or ``"yes"`` raise :class:`TypeError`. ``reason`` must be
    a non-empty explanation (it ends up in logs and errors).
    """

    allowed: bool
    reason: str

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool:
            raise TypeError(f"PolicyDecision.allowed must be True or False, not {self.allowed!r}")
        if not isinstance(self.reason, str):
            raise TypeError(f"PolicyDecision.reason must be a str, not {type(self.reason).__name__}")
        if not self.reason.strip():
            raise ValueError("PolicyDecision.reason must explain the decision")

    def __str__(self) -> str:
        return f"{'allowed' if self.allowed else 'REFUSED'}: {self.reason}"


@runtime_checkable
class CommandPolicy(Protocol):
    """What the client asks before sending a text command.

    ``check(command)`` gets the command exactly as the caller passed it
    (arguments included, the client's terminator not yet appended) and
    returns a :class:`PolicyDecision`. It must answer from the command and the
    policy's own state only and have no side effects: the client calls it once
    before waiting for the send lock and again if the policy was swapped while
    the command waited. An exception it raises propagates to the caller, and
    nothing is sent.
    """

    def check(self, command: str) -> PolicyDecision:
        ...  # pragma: no cover


LAYERS = ("denylist", "strict", "policy")


class CommandRefusedError(RuntimeError):
    """The client refused to send ``command``; nothing reached the wire.

    ``decision`` is the refusing :class:`PolicyDecision`; ``layer`` says which
    check refused it: ``"denylist"``, ``"strict"`` or ``"policy"``.
    """

    def __init__(self, command: str, decision: PolicyDecision, layer: str = "policy") -> None:
        if layer not in LAYERS:
            raise ValueError(f"layer must be one of {LAYERS}, not {layer!r}")
        if not isinstance(decision, PolicyDecision) or decision.allowed:
            raise ValueError(f"CommandRefusedError needs a refusing PolicyDecision, got {decision!r}")
        self.command = command
        self.decision = decision
        self.layer = layer
        super().__init__(f"refusing to send {redact(command)!r} ({layer}): {decision.reason}")


class PolicySwapRefusedError(RuntimeError):
    """:meth:`GoliathClient.set_command_policy` was called while an upload was in progress."""


def denylist_decision(command: str) -> PolicyDecision:
    """The always-on check: refuse if any name in ``command`` is in :data:`NEVER_SEND`.

    Each piece of :func:`normalized_names` is read both ways a firmware might:
    as the first ``strtok`` token and as the text before the first ``;`` (so a
    leading ``;`` gives the empty name, which is refused too).
    """
    name = _denylisted_name(command)
    if name is not None:
        return PolicyDecision(False, f"{_what(name)} is on the hard denylist: {NEVER_SEND_REASONS[name]}")
    return PolicyDecision(True, "not on the hard denylist")


def _denylisted_name(command: str) -> Optional[str]:
    """The first denylisted name some robot could read in ``command``, or None."""
    segments = _segments(command)
    names = [_segment_name(seg) for seg in segments] + [_plain_name(seg) for seg in segments]
    for name in names or [""]:
        if name in NEVER_SEND:
            return name
    return None


def _what(name: str) -> str:
    return "the empty command" if name == "" else name


# --------------------------------------------------------------------------- #
# What the robot can read across send boundaries
# --------------------------------------------------------------------------- #

#: The longest text command (bytes on the wire, terminator included) the
#: client sends. Spec 01: one receive buffer is one command, so a command the
#: robot's receive buffer cuts in two becomes two commands, the second being
#: whatever the tail says. The robot's receive-buffer size is not established
#: (not in spec 01 or 09; not yet observed on a robot). The longest command
#: the vendor app builds is the Wi-Fi provisioning string, at most 214 bytes
#: (spec 04: SSID and key hex-encoded, two digits per byte; an SSID has at most
#: 32 bytes and a WPA key at most 64 characters), and the vendor streamed
#: program blocks of 1024 bytes. 256 leaves room for a terminator.
MAX_COMMAND_BYTES = 256

#: How many earlier sends :class:`RecentSends` keeps. Every piece of a split
#: name must pass the denylist on its own, so it carries at least one
#: non-space character of the name; the longest name has 25, so no denylisted
#: name can be spread over more sends than this.
COALESCE_SENDS = 32

_COALESCE_BASIS = ("TCP may deliver several sends in one receive buffer, and one receive "
                   "buffer is one command (spec 01, docs/protocol/01-transport-and-framing.md)")


class RecentSends:
    """The recent sends whose text a later send could still join into one command.

    The robot reads one receive buffer as one command, and TCP may deliver
    several sends in one buffer (spec 01). A buffer starts where a send
    starts, and a name ends at a NUL, a line break or another segment break
    (:func:`normalized_names`), so only the text after the last break can
    still join a later send: :meth:`note` keeps that much, for at most
    :data:`COALESCE_SENDS` sends. Not thread-safe: the client uses it under
    the transport's send lock.
    """

    def __init__(self, maxlen: int = COALESCE_SENDS) -> None:
        self._texts: Deque[str] = deque(maxlen=maxlen)

    @property
    def texts(self) -> Tuple[str, ...]:
        """The texts, oldest first, that the next send could join."""
        return tuple(self._texts)

    def note(self, text: str) -> None:
        """Record ``text`` as sent."""
        parts = _SEGMENT_BREAK.split(text)
        if len(parts) > 1:
            self._texts.clear()
        if parts[-1]:
            self._texts.append(parts[-1])

    def clear(self) -> None:
        self._texts.clear()


def wire_decision(wire: str, previous: Sequence[str] = ()) -> PolicyDecision:
    """The always-on checks on the exact text about to go on the wire.

    ``wire`` is the command with its terminator; ``previous`` are the earlier
    sends it could join (:attr:`RecentSends.texts`). Refuses a command that is
    not ASCII, is longer than :data:`MAX_COMMAND_BYTES`, contains a denylisted
    name (:func:`denylist_decision`), or forms one together with the sends
    before it if the robot receives them as one buffer.
    """
    if not isinstance(wire, str):
        raise TypeError(f"a command must be a str, not {type(wire).__name__}")
    if not wire.isascii():
        return PolicyDecision(False, f"{redact(wire)!r} is not ASCII: the GFW 01.02.00 command "
                                     "table is ASCII and spec 01 says to send ASCII bytes only; "
                                     "the client sends exactly the ASCII text it checked")
    if len(wire) > MAX_COMMAND_BYTES:
        return PolicyDecision(False, f"the command is {len(wire)} bytes, more than "
                                     f"MAX_COMMAND_BYTES ({MAX_COMMAND_BYTES}): the robot's receive "
                                     "buffer could cut it, and its tail would be parsed as a "
                                     "command of its own (spec 01: one receive buffer is one "
                                     "command; the buffer size is not established)")
    decision = denylist_decision(wire)
    if not decision.allowed:
        return decision
    for k in range(len(previous)):
        joined = "".join(previous[k:]) + wire
        name = _denylisted_name(joined)
        if name is not None:
            n = len(previous) - k
            shown = redact(joined)
            if len(shown) > 120:
                shown = shown[:57] + "..." + shown[-60:]
            return PolicyDecision(False, f"{_what(name)} is on the hard denylist, and the robot may "
                                         f"read it from this send joined to the {n} send(s) "
                                         f"before it ({shown!r}): {_COALESCE_BASIS}. "
                                         f"{NEVER_SEND_REASONS[name]}")
    return PolicyDecision(True, "not on the hard denylist, alone or joined to the sends before it")


def _program_pattern() -> "re.Pattern[bytes]":
    names = sorted(NEVER_SEND - {""}, key=len, reverse=True)
    alternatives = (rb"[ \t]+".join(re.escape(word.encode("ascii")) for word in name.split(" "))
                    for name in names)
    return re.compile(b"|".join(alternatives), re.IGNORECASE)  # bytes: ASCII case folding only


_PROGRAM_NAMES = _program_pattern()
#: Every denylisted name has two letters in a row; a program without any
#: (every Goliath dialect program) needs no full search.
_TWO_LETTERS = re.compile(rb"[A-Za-z]{2}")
assert all(_TWO_LETTERS.search(n.encode("ascii")) for n in NEVER_SEND if n)


def program_decision(payload: bytes) -> PolicyDecision:
    """The always-on check on the bytes of a G-code program before an upload.

    Refuses a program in which any line contains a denylisted name anywhere
    (any case, any run of spaces or tabs between its words). While the robot
    is in upload mode the program is data; outside it -- the upload ended
    early (the end checks are not decoded: spec 09 "G-code upload mode"), or
    the robot never took the header (GFW 01.02.00 does not acknowledge it) --
    each block is parsed as one command (spec 01), and since a receive buffer
    may start or end anywhere in a line, any substring of a line could be one.
    Goliath dialect words are one letter followed by a number (spec 05) and
    every denylisted name has two letters in a row, so no dialect program is
    refused.
    """
    data = bytes(payload)
    m = _PROGRAM_NAMES.search(data) if _TWO_LETTERS.search(data) else None
    if m is None:
        return PolicyDecision(True, "no denylisted command name in the program")
    line_no = data.count(b"\n", 0, m.start()) + 1
    name = " ".join(m.group(0).decode("ascii").split()).upper()
    start = data.rfind(b"\n", 0, m.start()) + 1
    end = data.find(b"\n", m.end())
    line = data[start:end if end >= 0 else len(data)].rstrip(b"\r").decode("latin-1")
    return PolicyDecision(False, f"program line {line_no} ({line[:80]!r}) contains {name}, which "
                                 f"is on the hard denylist: outside upload mode (an upload that "
                                 "ended early, or a header the robot did not take) every program "
                                 "block is parsed as a command, and a block may start or end "
                                 f"anywhere in a line. {NEVER_SEND_REASONS[name]}")


def strict_decision(command: str) -> PolicyDecision:
    """The ``strict_commands=True`` check: only :data:`STRICT_COMMANDS`, plainly spelled."""
    basis = "the strict set (spec 09 section 1 plus INFO)"
    if not _PRINTABLE_ASCII.fullmatch(command):
        return PolicyDecision(False, f"{basis} admits one plain command: {redact(command)!r} "
                                     "contains a line break, NUL, other control character or "
                                     "non-ASCII character")
    name = command_name(command)
    if name in STRICT_COMMANDS:
        return PolicyDecision(True, f"{name} is in {basis}")
    names = normalized_names(command)
    if names[0] in STRICT_COMMANDS:
        why = (f"it is not spelled the way the firmware compares names ({name!r} is not "
               f"{names[0]!r}: leading or trailing whitespace, doubled spaces or a leading ';')")
    elif names[0] in VENDOR_SENT_NOT_ON_GFW_01_02_00:
        why = (f"the vendor app sends {names[0]} only to firmware newer than 01.02.00, and it is "
               "not in spec 09 section 1")
    else:
        why = f"{redact(name)!r} is not in spec 09 section 1 and is not INFO"
    return PolicyDecision(False, f"refused by {basis}: {why}")


# --------------------------------------------------------------------------- #
# A ready-made policy: one phase's allowlist, with bound arguments
# --------------------------------------------------------------------------- #


def _check_name(name: object) -> str:
    if not isinstance(name, str):
        raise TypeError(f"a command name must be a str, not {type(name).__name__}")
    if not _CANONICAL_NAME.fullmatch(name) or "  " in name:
        raise ValueError(f"{name!r} is not a command name as the firmware table spells it "
                         "(upper-case ASCII words separated by single spaces, no ';')")
    if name in NEVER_SEND:
        raise ValueError(f"a policy cannot allow {name}: it is on the hard denylist, which is "
                         f"checked first and always refuses it ({NEVER_SEND_REASONS[name]})")
    return name


def _arg_text(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError(f"a bound argument must be an int or the exact wire text (str), not "
                        f"{value!r}: floats and bools are ambiguous on the wire")
    text = str(value)
    if not _PLAIN_ARG.fullmatch(text):
        raise ValueError(f"bound argument {text!r} must be non-empty printable ASCII without "
                         "spaces or ';'")
    return text


@dataclass(frozen=True)
class AllowedCommand:
    """One entry of a :class:`PhasePolicy`.

    ``args`` None allows the command with any arguments; a tuple binds them:
    only ``name;arg1;arg2...`` exactly (``()``: the bare name only). Use
    :func:`bind` to build bound entries from integers.
    """

    name: str
    args: Optional[Tuple[str, ...]] = None

    def __post_init__(self) -> None:
        _check_name(self.name)
        if self.args is not None:
            if not isinstance(self.args, tuple):
                raise TypeError(f"args must be a tuple of str or None, not {self.args!r}")
            for a in self.args:
                if not isinstance(a, str):
                    raise TypeError(f"bound arguments are wire text (str); use bind() for ints: "
                                    f"{a!r}")
                _arg_text(a)

    @property
    def wire(self) -> Optional[str]:
        """The one command text this entry allows, or None (any arguments)."""
        if self.args is None:
            return None
        return ";".join((self.name,) + self.args)


def bind(name: str, *args: Union[int, str]) -> AllowedCommand:
    """An entry allowing ``name`` only with exactly these arguments.

    ``bind("SET PANEL SIZE", 1200, 800)`` allows ``SET PANEL SIZE;1200;800``
    and nothing else (the text :meth:`Commands.set_panel_size
    <goliath_link.protocol.Commands.set_panel_size>` builds).
    """
    return AllowedCommand(name, tuple(_arg_text(a) for a in args))


class PhasePolicy:
    """A :class:`CommandPolicy` for one phase: an allowlist with bound arguments.

    ``allowed`` lists names (any arguments) and :class:`AllowedCommand`
    entries (:func:`bind`). Everything else is refused, and so is any command
    with a line break, NUL or other control character in it (one command per
    send). An empty list sends nothing. Names on the hard denylist are refused
    at construction (:class:`ValueError`): the denylist would refuse them
    anyway, so allowing one is a mistake to report, not to carry.

    Matching: an unbound entry matches the command's normalised name (case
    and surrounding whitespace do not matter, as for the firmware's name
    comparison); a bound entry matches only its exact text
    (:attr:`AllowedCommand.wire`), so ``SET PANEL SIZE;1200.0;800`` does not
    pass for ``SET PANEL SIZE;1200;800``.
    """

    def __init__(self, phase: str, allowed: Iterable[Union[str, AllowedCommand]] = (), *,
                 basis: str = "") -> None:
        if not isinstance(phase, str) or not phase.strip():
            raise ValueError(f"a phase policy needs a phase name, not {phase!r}")
        if not isinstance(basis, str):
            raise TypeError(f"basis must be a str, not {type(basis).__name__}")
        if isinstance(allowed, (str, bytes)):
            raise TypeError("allowed must be a list of commands, not a single str")
        entries = []
        for item in allowed:
            if isinstance(item, AllowedCommand):
                entries.append(item)
            elif isinstance(item, str):
                entries.append(AllowedCommand(item))
            else:
                raise TypeError(f"a policy entry must be a command name or an AllowedCommand, "
                                f"not {item!r}")
        self._phase = phase
        self._basis = basis
        self._allowed: Tuple[AllowedCommand, ...] = tuple(entries)
        by_name: Dict[str, Tuple[AllowedCommand, ...]] = {}
        for e in entries:
            by_name[e.name] = by_name.get(e.name, ()) + (e,)
        for name, group in by_name.items():
            if any(e.args is None for e in group) and any(e.args is not None for e in group):
                raise ValueError(f"phase {phase}: {name} is listed both with bound arguments "
                                 "and with any arguments; the unbound entry would make the "
                                 "binding meaningless")
        self._by_name = by_name

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def basis(self) -> str:
        return self._basis

    @property
    def allowed(self) -> Tuple[AllowedCommand, ...]:
        return self._allowed

    def describe(self) -> str:
        """The allowlist in words, for logs and refusal reasons."""
        if not self._allowed:
            return "no commands"
        return ", ".join(e.wire if e.wire is not None else f"{e.name} (any arguments)"
                         for e in self._allowed)

    def __repr__(self) -> str:
        return f"PhasePolicy({self._phase!r}: {self.describe()})"

    def check(self, command: str) -> PolicyDecision:
        if not isinstance(command, str):
            raise TypeError(f"a command must be a str, not {type(command).__name__}")
        where = f"phase {self._phase}" + (f" ({self._basis})" if self._basis else "")
        names = normalized_names(command)
        if len(names) != 1 or _SEGMENT_BREAK.search(command):
            return PolicyDecision(False, f"{where} allows one plain command per send: "
                                         f"{redact(command)!r} contains a line break, NUL or "
                                         "other control character")
        name = names[0]
        if _plain_name(command) != name:
            return PolicyDecision(False, f"{where} allows plain commands only: "
                                         f"{redact(command)!r} starts with ';'")
        entries = self._by_name.get(name)
        if not entries:
            return PolicyDecision(False, f"{where} does not allow {redact(name) or 'an empty name'} "
                                         f"(it allows {self.describe()})")
        for e in entries:
            if e.wire is None:
                return PolicyDecision(True, f"{where} allows {name} (any arguments)")
            if command == e.wire:
                return PolicyDecision(True, f"{where} allows {e.wire!r} (bound arguments)")
        bound = " or ".join(repr(e.wire) for e in entries)
        return PolicyDecision(False, f"{where} allows {name} only as {bound} (bound arguments); "
                                     f"got {redact(command)!r}")
