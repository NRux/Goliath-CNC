# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""Typed readings of the robot's replies, and what each command is expected to answer.

Protocol knowledge every sender needs, kept here so that every client of this
library reads the robot the same way (no I/O; strings and bytes in, values out):

* parsers for the replies that carry values:

  - :func:`parse_vertex` -- ``INFO;25`` (a detected panel vertex), in its two-field
    GFW 01.02.00 form or the vendor table's three-field form, saying which;
  - :func:`parse_tower_distance` -- ``INFO;27`` (inter-tower distance);
  - :func:`parse_identity` -- ``INFO;34`` (project name, HW/SW revision, serial
    number and the firmware version);
  - :func:`parse_sanity` -- ``INFO;40`` (the sanity report: 15 values, then ``END``);
  - :func:`parse_cage`, :func:`parse_index`, :func:`parse_spindle_distance` -- the
    free-form replies ``CAGE STATUS: ...``, ``cfg read index : <n>`` and
    ``spindle distance: <x>``, which have no newline on GFW 01.02.00;
  - :class:`StatusBlob` -- the 48-byte ``GLOBAL STATUS`` blob, whose field
    names are **assumed**: every accessor returns an :class:`AssumedField` that
    says so;

* the **per-command expected-reply table** :data:`EXPECTED_REPLY` and
  :func:`expected_reply`: for every name in the GFW 01.02.00 command table, what
  the handler sends (reply kind, connection, terminator, how long to wait, and
  the firmware basis), transcribed from the "Handler sends" column of
  ``docs/protocol/09-firmware-01.02.00.md``. Not every command is acknowledged
  with ``INFO;0``: ``INFO`` and ``PRINT SYS REPORT``, for example, never send
  one. For any other firmware the table has no basis and
  :func:`expected_reply` returns None.

Rules every parser follows:

* **A reply that does not have a documented shape is an error, never a guess.**
  :class:`ReadingParseError` names what was being read, why it failed, and the
  line itself. Numbers must be plain decimals as ``printf`` writes them: no
  ``nan``, ``inf``, exponents, spaces or thousands separators.
* **A line the framer marked as unterminated is cut short, not a reading**
  (spec 01 §Inbound: the framer marks text it had to flush without a
  terminator). Every ``INFO`` line ends in a newline, and the slot-0 replies
  ``cfg read index : <n>`` and ``spindle distance: <x>`` end at the NUL padding
  of their 512-byte block (spec 09 §Senders), so a :class:`TextMessage` of
  those with ``terminated=False`` was cut by a stall, a lost link or a late TCP
  segment, and its last number may be cut short (``285`` for ``28500``). It
  raises. Only the ``GET SPINDLE CAGE STATUS`` reply has no terminator by
  design (``CLOSED``), and a cut-short one never matches. A ``str`` or
  ``bytes`` argument carries no such mark and is taken as complete: pass the
  framer's ``TextMessage`` where you have it.
* **Where two shapes are documented, the reading says which one it saw**
  (:attr:`Vertex.form`, :attr:`Identity.form`), with the documented basis.
* **Values are reported, not interpreted** where their meaning is open: the
  ``INFO;25`` numbers stay in wire order (``x;y`` versus ``length;angle`` is not
  settled), ``INFO;40`` values are returned as sent (never coerced to ok/fail),
  and ``INFO;27`` is returned even when negative (range checks belong to the
  caller).
* The free-form parsers return None when the text is **not** that kind of reply
  (another prefix), and raise when it starts like one but does not match.

Provenance: every documented format this module reads is an entry of
:data:`FORMATS`, and every expected reply a :class:`ReplySpec`, each with its
``basis`` and ``observed = False``: **nothing here has been observed on a
robot yet.** Everything is from static analysis of firmware GFW 01.02.00 or from
the vendor app's tables, as documented in ``docs/protocol/02-message-reference.md``
("spec 02") and ``docs/protocol/09-firmware-01.02.00.md`` ("spec 09"). When the
first read-only probe of a real robot reports what it saw, these entries are
the place to update (or to add a :class:`ReplyTable` for another firmware).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Dict, Literal, Mapping, Optional, Tuple, Union

from .protocol import (
    GLOBAL_BLOB_FIELD_NAMES_UNVERIFIED,
    GLOBAL_BLOB_HEAD,
    GLOBAL_BLOB_LEN,
    GLOBAL_BLOB_TALE,
    TextMessage,
    parse_text_message,
)

__all__ = [
    "ACK_CONN",
    "DEFAULT_REPLY_TIMEOUT_S",
    "EXPECTED_REPLY",
    "EXPECTED_REPLY_FIRMWARE",
    "FORMATS",
    "REPLY_KINDS",
    "REPLY_TABLES",
    "SANITY_VALUE_COUNT",
    "STATUS_FIELD_NAMES_ASSUMED",
    "AssumedField",
    "Identity",
    "ReadingParseError",
    "ReplyFormat",
    "ReplySpec",
    "ReplyTable",
    "StatusBlob",
    "Vertex",
    "expected_reply",
    "parse_cage",
    "parse_identity",
    "parse_index",
    "parse_sanity",
    "parse_spindle_distance",
    "parse_tower_distance",
    "parse_vertex",
]

Version = Tuple[int, int, int]
Message = Union[TextMessage, str, bytes, bytearray, memoryview]

#: Every statement in this module rests on this, unless its basis says otherwise.
_NOT_OBSERVED = "not yet observed on a robot"
_STATIC = "static analysis of GFW 01.02.00, " + _NOT_OBSERVED


class ReadingParseError(ValueError):
    """A reply did not have a documented shape. Never a guess: the caller decides.

    ``what`` names the reading (``"INFO;25"``, ``"cage status"``, ...), ``reason``
    says what did not match, and ``line`` is the text (or, for the binary blob,
    the hex bytes) that was received.
    """

    def __init__(self, what: str, reason: str, line: str) -> None:
        self.what = what
        self.reason = reason
        self.line = line
        super().__init__(f"{what}: {reason}: {line!r}")


# --------------------------------------------------------------------------- #
# Documented reply formats and their basis
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReplyFormat:
    """One documented shape of a reply, with where it comes from.

    ``field_count`` is the number of ``;``-separated values after ``INFO;<n>``
    (None for free-form and binary replies). ``observed`` turns True only when a
    real robot has been seen sending this shape.
    """

    key: str
    reply: str
    field_count: Optional[int]
    basis: str
    note: str = ""
    observed: bool = False


def _formats(*entries: ReplyFormat) -> Mapping[str, ReplyFormat]:
    return MappingProxyType({f.key: f for f in entries})


#: Every reply shape this module parses, keyed by the ``form`` the parsers report.
#: A form names the documented *format* a reply matched, not the firmware that
#: sent it (a newer robot may well use the 01.02.00 format).
FORMATS: Mapping[str, ReplyFormat] = _formats(
    ReplyFormat(
        "info25-gfw-01.02.00", "INFO;25;%.4f;%.4f\\n", 2,
        "firmware 01.02.00 sender at 0x080238e0: spec 09 §Senders (single pass †); "
        "spec 02 INFO 25 ('On 01.02.00 the format is INFO;25;%.4f;%.4f -- two values'); "
        + _NOT_OBSERVED,
        note="values not interpreted: whether they are x;y or length;angle, and in which "
             "frame, is open (spec 03 row D; SET VERTEX takes length;angle)",
    ),
    ReplyFormat(
        "info25-vendor-table", "INFO;25;x;y;z", 3,
        "vendor table: spec 02 INFO 25 ('Vendor table: INFO;25;x;y;z'); no surviving "
        "firmware sends it; " + _NOT_OBSERVED,
        note="values not interpreted (see info25-gfw-01.02.00)",
    ),
    ReplyFormat(
        "info27-gfw-01.02.00", "INFO;27;%.4f\\n", 1,
        "firmware 01.02.00 sender at 0x0800d326: spec 09 §Senders (single pass †); "
        "spec 02 INFO 27; " + _NOT_OBSERVED,
        note="the inter-tower distance; range checks belong to the caller",
    ),
    ReplyFormat(
        "info34-gfw-01.02.00", "INFO;34;%s;%d;%d;%04X%04X%04X\\n", 4,
        "firmware 01.02.00 builder 0x08009c84: spec 09 §Senders; spec 02 INFO 34 "
        "('prj_name is GFW 01.02.00 padded with spaces, HW/SW are decimal, SN is 12 "
        "hex digits'); " + _NOT_OBSERVED,
    ),
    ReplyFormat(
        "info34-vendor-table", "INFO;34;<prj_name>;<HW rev>;<SW rev>;<SN>", 4,
        "vendor table: spec 02 INFO 34 (four fields; no format given for the serial "
        "number); " + _NOT_OBSERVED,
        note="read when the line has four fields but not the exact 01.02.00 format; "
             "Identity.notes says what differs",
    ),
    ReplyFormat(
        "info40-gfw-01.02.00", "INFO;40;{15 x %d};END\\n", 16,
        "firmware 01.02.00 sender at 0x08013e3c: spec 09 §Senders; the meaning of the "
        "values (0 = fail, 1 = ok) is the vendor table's, spec 02 INFO 40; "
        + _NOT_OBSERVED,
        note="values returned as sent; what each of the 15 means is open",
    ),
    ReplyFormat(
        "cage-get-gfw-01.02.00", "CAGE STATUS: OPEN\\n | CAGE STATUS: CLOSED", None,
        "firmware 01.02.00 GET SPINDLE CAGE STATUS handler: direct reply formatted with "
        "snprintf(reply, 20, ...), so 'CLOSED' loses its newline: spec 09 §Direct "
        "replies (single pass †), spec 02 §Free-form replies; " + _NOT_OBSERVED,
    ),
    ReplyFormat(
        "cage-poll-gfw-01.02.00", "CAGE STATUS: %s\\n (slot 0, padded)", None,
        "firmware 01.02.00 POLL CAGE timer callback 0x0800b8d8: spec 09 §Free-form text "
        "on slot 0 (single pass †); " + _NOT_OBSERVED,
    ),
    ReplyFormat(
        "index-gfw-01.02.00", "cfg read index : %d (no newline)", None,
        "firmware 01.02.00 READ INDEX CALIBRATION handler, printf at 0x0800c434: spec 09 "
        "§Free-form text on slot 0; spec 02 §Free-form replies (a space on both sides "
        "of the colon); " + _NOT_OBSERVED,
    ),
    ReplyFormat(
        "spindle-gfw-01.02.00", "spindle distance: %.2f (no newline)", None,
        "firmware 01.02.00 READ SPINDLE CALIB handler, printf at 0x0800c1c4: spec 09 "
        "§Free-form text on slot 0; spec 02 §Free-form replies; " + _NOT_OBSERVED,
    ),
    ReplyFormat(
        "status-blob-gfw-01.02.00", "GLOBAL STATUS HEAD\\0 + 10 status bytes + "
        "GLOBAL STATUS TALE\\0 (48 bytes)", None,
        "firmware 01.02.00 builder 0x0802697c: spec 09 §The GLOBAL STATUS blob; the "
        "field names are assumed, matched by position to the vendor's INFO;1002 "
        "template (spec 02 §Global status); " + _NOT_OBSERVED,
    ),
)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

_INT = re.compile(r"-?[0-9]+")
_DECIMAL = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")


def _as_text(msg: object, what: str) -> str:
    """The text of ``msg`` with outer whitespace and NUL padding removed."""
    if isinstance(msg, TextMessage):
        text = msg.raw
    elif isinstance(msg, str):
        text = msg
    elif isinstance(msg, (bytes, bytearray, memoryview)):
        text = bytes(msg).decode("latin-1")
    else:
        raise TypeError(f"{what}: expected a TextMessage, str or bytes, not {type(msg).__name__}")
    text = text.strip(" \t\r\n\x00")
    if "\x00" in text:
        raise ReadingParseError(what, "NUL byte inside the reply (something follows the "
                                      "padding, or two replies were joined)", text)
    return text


_CUT_SHORT = ("no terminator: the framer ended this text at a pause, a stall or the end of "
              "the stream, not at {end}, so it may be cut short")


def _info_line(msg: Message, code: int) -> Tuple[str, Tuple[str, ...]]:
    """``(line, fields after INFO;<code>)`` of an INFO line; raise if it is not one.

    A str or bytes line is normalised like :func:`parse_text_message` (and so like
    the framer's ``TextMessage.raw``), after NUL padding is removed. A
    ``TextMessage`` the framer marked ``terminated=False`` raises: every INFO
    line ends in a newline (spec 09 §Senders), so it was cut short.
    """
    what = f"INFO;{code}"
    line = _as_text(msg, what)
    if not isinstance(msg, TextMessage):
        line = parse_text_message(line).raw
    parts = line.split(";")
    if len(parts) < 2 or parts[0] != "INFO" or parts[1] != str(code):
        raise ReadingParseError(what, f"not an INFO;{code} line", line)
    if isinstance(msg, TextMessage) and not msg.terminated:
        raise ReadingParseError(what, _CUT_SHORT.format(
            end="the newline every INFO line ends with (spec 09 §Senders)"), line)
    return line, tuple(parts[2:])


def _decimal(value: str, what: str, line: str) -> float:
    if not _DECIMAL.fullmatch(value):
        raise ReadingParseError(what, f"{value!r} is not a decimal number", line)
    number = float(value)
    if not math.isfinite(number):  # second guard: the pattern already admits no nan/inf
        raise ReadingParseError(what, f"{value!r} is not finite", line)
    return number


def _integer(value: str, what: str, line: str) -> int:
    if not _INT.fullmatch(value):
        raise ReadingParseError(what, f"{value!r} is not a whole number", line)
    return int(value)


# --------------------------------------------------------------------------- #
# INFO;25, INFO;27
# --------------------------------------------------------------------------- #


class Vertex(tuple):
    """The numbers of one ``INFO;25`` line, in wire order (a ``tuple`` of floats).

    :attr:`form` is the :data:`FORMATS` key of the shape it matched
    (``"info25-gfw-01.02.00"``: two values; ``"info25-vendor-table"``: three) and
    :attr:`basis` that entry's basis. The numbers are not interpreted: whether
    they are ``x;y`` or ``length;angle``, and in which frame, is open.
    """

    form: str
    basis: str

    def __new__(cls, values: Tuple[float, ...], form: str) -> "Vertex":
        self = super().__new__(cls, values)
        self.form = form
        self.basis = FORMATS[form].basis
        return self

    def __reduce__(self):  # keep form through pickling and copying
        return (Vertex, (tuple(self), self.form))

    def __repr__(self) -> str:
        return f"Vertex({tuple(self)!r}, form={self.form!r})"


_VERTEX_FORMS = {2: "info25-gfw-01.02.00", 3: "info25-vendor-table"}


def parse_vertex(msg: Message) -> Vertex:
    """Read an ``INFO;25`` line: two values (GFW 01.02.00) or three (vendor table).

    Any other number of fields, or a field that is not a plain decimal, raises
    :class:`ReadingParseError` naming the line.
    """
    what = "INFO;25"
    line, fields = _info_line(msg, 25)
    form = _VERTEX_FORMS.get(len(fields))
    if form is None:
        raise ReadingParseError(
            what, f"{len(fields)} value field(s); the documented forms have 2 (GFW 01.02.00, "
                  "spec 09) or 3 (vendor table, spec 02)", line)
    return Vertex(tuple(_decimal(f, what, line) for f in fields), form)


def parse_tower_distance(msg: Message) -> float:
    """Read an ``INFO;27`` line (``INFO;27;%.4f``): the inter-tower distance, as sent."""
    what = "INFO;27"
    line, fields = _info_line(msg, 27)
    if len(fields) != 1:
        raise ReadingParseError(what, f"{len(fields)} value field(s); expected 1 "
                                      "(INFO;27;%.4f, spec 09)", line)
    return _decimal(fields[0], what, line)


# --------------------------------------------------------------------------- #
# INFO;34
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Identity:
    """The robot's ``INFO;34`` identity line.

    ``prj`` is the project name without its space padding (``"GFW 01.02.00"``),
    ``hw``/``sw`` the hardware and software revision numbers, ``serial`` the
    serial number as sent, and ``version`` the firmware version read from a
    project name of the form ``GFW xx.yy.zz`` (None when the name has no such
    version: the version is then unknown). ``form`` is ``"info34-gfw-01.02.00"``
    when the line has exactly the GFW 01.02.00 format, else
    ``"info34-vendor-table"`` with ``notes`` saying what differs.

    The serial number identifies one robot: keep it out of shared reports
    unless the owner agrees. It is left out of the repr (as is ``raw``, the
    whole line), so that logging an Identity does not spread it.
    """

    prj: str
    hw: int
    sw: int
    serial: str = field(repr=False)
    version: Optional[Version]
    form: str
    basis: str
    notes: Tuple[str, ...] = ()
    raw: str = field(default="", repr=False)


_PRJ_STRICT = re.compile(r"GFW [0-9]{2}\.[0-9]{2}\.[0-9]{2} *")
_PRJ_VERSION = re.compile(r"GFW ([0-9]+)\.([0-9]+)\.([0-9]+) *")
_SERIAL_STRICT = re.compile(r"[0-9A-F]{12}")


def parse_identity(msg: Message) -> Identity:
    """Read an ``INFO;34`` line: ``INFO;34;<prj_name>;<HW>;<SW>;<SN>``.

    Four fields are required, HW and SW must be whole numbers, and the project
    name and serial must not be empty; anything else raises
    :class:`ReadingParseError` naming the line.
    """
    what = "INFO;34"
    line, fields = _info_line(msg, 34)
    if len(fields) != 4:
        raise ReadingParseError(what, f"{len(fields)} field(s); expected 4: "
                                      "<prj_name>;<HW>;<SW>;<SN> (spec 02 INFO 34)", line)
    prj_field, hw_field, sw_field, sn_field = fields
    prj, serial = prj_field.strip(" "), sn_field.strip(" ")
    if not prj:
        raise ReadingParseError(what, "empty project name", line)
    if not serial:
        raise ReadingParseError(what, "empty serial number", line)
    hw = _integer(hw_field, what, line)
    sw = _integer(sw_field, what, line)

    notes = []
    m = _PRJ_VERSION.fullmatch(prj_field)
    version: Optional[Version] = None
    if m:
        version = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if not _PRJ_STRICT.fullmatch(prj_field):
            notes.append(f"project name {prj!r} is not 'GFW xx.yy.zz' with two digits per "
                         "part, as GFW 01.02.00 sends it (spec 02 INFO 34)")
    else:
        notes.append(f"no firmware version: the project name {prj!r} does not have the form "
                     "'GFW xx.yy.zz' (spec 02 INFO 34), so the version is unknown")
    if not _SERIAL_STRICT.fullmatch(sn_field):
        notes.append("the serial number is not 12 upper-case hexadecimal digits "
                     "(%04X%04X%04X), the GFW 01.02.00 format (spec 02 INFO 34)")
    form = "info34-gfw-01.02.00" if not notes else "info34-vendor-table"
    return Identity(prj=prj, hw=hw, sw=sw, serial=serial, version=version, form=form,
                    basis=FORMATS[form].basis, notes=tuple(notes), raw=line)


# --------------------------------------------------------------------------- #
# INFO;40
# --------------------------------------------------------------------------- #

#: Number of values in an ``INFO;40`` sanity report on GFW 01.02.00
#: (``INFO;40;{15 x %d};END``, spec 09 §Senders; spec 02 INFO 40). Not yet
#: observed on a robot: pass ``count=`` to :func:`parse_sanity` for another firmware.
SANITY_VALUE_COUNT = 15


def parse_sanity(msg: Message, *, count: int = SANITY_VALUE_COUNT) -> Tuple[int, ...]:
    """Read an ``INFO;40`` sanity report: ``count`` whole numbers, then ``END``.

    The values are returned exactly as sent. The vendor table reads 0 as fail
    and 1 as ok; what each position checks is not known, so compare reports
    with a baseline rather than reading them. A report without the closing
    ``END`` (cut short) or with another number of values raises
    :class:`ReadingParseError` naming the line.
    """
    what = "INFO;40"
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError(f"count must be a whole number >= 1, not {count!r}")
    line, fields = _info_line(msg, 40)
    if not fields or fields[-1] != "END":
        raise ReadingParseError(what, "the report does not end with 'END' (cut short or "
                                      "another format)", line)
    values = fields[:-1]
    if len(values) != count:
        raise ReadingParseError(what, f"{len(values)} value(s) before END; expected {count}",
                                line)
    return tuple(_integer(v, what, line) for v in values)


# --------------------------------------------------------------------------- #
# Free-form replies (no INFO;<n>)
# --------------------------------------------------------------------------- #

_CAGE = re.compile(r"CAGE STATUS: (OPEN|CLOSED)")
_INDEX = re.compile(r"cfg read index : (-?[0-9]+)")
_SPINDLE = re.compile(r"spindle distance: (-?[0-9]+(?:\.[0-9]+)?)")


#: Where the padded slot-0 replies end on GFW 01.02.00 (spec 09 §Free-form text on slot 0).
_SLOT0_END = "the NUL padding of its 512-byte block (spec 09 §Free-form text on slot 0)"


def _free_form(text: object, what: str, prefix: str, pattern: "re.Pattern[str]",
               expected: str, *, padded_end: Optional[str] = None) -> Optional["re.Match[str]"]:
    """Match a free-form reply; None when ``text`` does not start like one.

    ``padded_end``: where the reply ends on the wire when it has a terminator by
    design; then a ``TextMessage`` marked ``terminated=False`` raises (cut short).
    """
    if isinstance(text, TextMessage) and text.kind != "TEXT":
        return None  # an INFO/ERR/WARN line is not a free-form reply
    s = _as_text(text, what)
    if not s.upper().startswith(prefix.upper()):
        return None
    if padded_end is not None and isinstance(text, TextMessage) and not text.terminated:
        raise ReadingParseError(what, _CUT_SHORT.format(end=padded_end), s)
    m = pattern.fullmatch(s)
    if m is None:
        raise ReadingParseError(what, f"starts like a {what} reply but is not {expected}", s)
    return m


def parse_cage(text: object) -> Optional[Literal["OPEN", "CLOSED"]]:
    """Read a ``CAGE STATUS: OPEN`` / ``CAGE STATUS: CLOSED`` reply.

    Accepts both GFW 01.02.00 shapes: the direct reply to
    ``GET SPINDLE CAGE STATUS`` (``CLOSED`` with **no** newline, so the framer
    delivers it with ``terminated=False``, as expected) and the padded
    ``POLL CAGE`` line (with a newline). Returns None for text that is not a
    cage reply; anything that starts like one but is not exactly ``OPEN`` or
    ``CLOSED`` (cut short, joined to another reply, other case) raises
    :class:`ReadingParseError`: it is never read as ``CLOSED``.
    """
    m = _free_form(text, "cage status", "CAGE STATUS", _CAGE,
                   "'CAGE STATUS: OPEN' or 'CAGE STATUS: CLOSED' (spec 02 §Free-form replies)")
    return None if m is None else m.group(1)  # type: ignore[return-value]


def parse_index(text: object) -> Optional[int]:
    """Read ``cfg read index : <n>`` (a space on both sides of the colon, no newline).

    Returns None for text that is not an index reply. The form of an earlier
    spec revision (``cfg read index :<n>``) is not the firmware's and raises
    :class:`ReadingParseError`, as does any other variant, and a
    ``TextMessage`` the framer marked ``terminated=False`` (the reply ends at
    its NUL padding, so it was cut short: ``285`` for ``28500``).
    """
    m = _free_form(text, "index calibration", "cfg read index", _INDEX,
                   "'cfg read index : <whole number>' (spec 02 §Free-form replies)",
                   padded_end=_SLOT0_END)
    return None if m is None else int(m.group(1))


def parse_spindle_distance(text: object) -> Optional[float]:
    """Read ``spindle distance: <x>`` (``%.2f``, no newline). None for other text.

    Like :func:`parse_index`, a ``TextMessage`` marked ``terminated=False``
    raises: the reply ends at its NUL padding, so it was cut short.
    """
    m = _free_form(text, "spindle distance", "spindle distance", _SPINDLE,
                   "'spindle distance: <decimal>' (spec 02 §Free-form replies)",
                   padded_end=_SLOT0_END)
    return None if m is None else float(m.group(1))


# --------------------------------------------------------------------------- #
# The 48-byte GLOBAL STATUS blob
# --------------------------------------------------------------------------- #

#: The ten status-byte names, in blob order. ASSUMED: matched by position to the
#: vendor's INFO;1002 template (spec 02 §Global status). Same tuple as
#: :data:`goliath_link.protocol.GLOBAL_BLOB_FIELD_NAMES_UNVERIFIED`.
STATUS_FIELD_NAMES_ASSUMED: Tuple[str, ...] = GLOBAL_BLOB_FIELD_NAMES_UNVERIFIED

_BLOB_STATUS_START = len(GLOBAL_BLOB_HEAD)  # 19
#: Byte offset, in the firmware's status structure, of the 32-bit field whose low
#: byte is written at blob offset 19 + i (spec 09 §The GLOBAL STATUS blob). A
#: firmware fact (static); only the names are assumed.
_STRUCT_OFFSETS = (8, 0, 4, 16, 12, 20, 24, 28, 32, 36)
#: What corroborates a name, by structure offset (spec 09 §Connect and disconnect).
_CORROBORATION = {
    8: "the connect-time replay sends INFO;4 (pairing done) when status-structure field "
       "+8 is 3 (spec 09 §Connect and disconnect, static analysis); this byte is only "
       "that field's low byte",
    16: "the connect-time replay sends INFO;6 (panel detection done) when status-structure "
        "field +16 is 3 (spec 09 §Connect and disconnect, static analysis); this byte is "
        "only that field's low byte",
    20: "the connect-time replay sends INFO;8 (G-code received) when status-structure field "
        "+20 is non-zero (spec 09 §Connect and disconnect, static analysis); this byte is "
        "only that field's low byte",
}
assert len(STATUS_FIELD_NAMES_ASSUMED) == len(_STRUCT_OFFSETS) == 10
assert GLOBAL_BLOB_LEN == 48


@dataclass(frozen=True)
class AssumedField:
    """One status byte of the blob, under a name that is **assumed**.

    ``value`` is the byte as received (the low byte of a 32-bit status field);
    its encoding is unknown except where ``corroboration`` says otherwise.
    ``assumed`` is always True and ``observed`` False until a capture in known
    machine states confirms the name.
    """

    name: str
    value: int
    blob_offset: int
    struct_offset: int
    basis: str
    corroboration: Optional[str] = None
    assumed: bool = True
    observed: bool = False


@dataclass(frozen=True)
class StatusBlob:
    """The 48-byte reply to ``GET GLOBAL SYS STATUS`` on GFW 01.02.00.

    ``raw10`` are the ten status bytes (any values, NULs included);
    ``assumed_names`` their names in blob order, which are assumptions (see
    :data:`STATUS_FIELD_NAMES_ASSUMED`). Read a byte with :meth:`field` or the
    accessors :attr:`pairing`, :attr:`pd_status`, :attr:`gcode_uploaded`,
    :attr:`work_status` and :attr:`resume_after_reset`: each returns an
    :class:`AssumedField`, never a bare number, so the assumption travels with
    the value. The blob has no tower firmware versions.
    """

    raw10: bytes
    assumed_names: Tuple[str, ...] = STATUS_FIELD_NAMES_ASSUMED

    def __post_init__(self) -> None:
        if not isinstance(self.raw10, bytes) or len(self.raw10) != 10:
            raise ValueError(f"raw10 must be 10 bytes, not {self.raw10!r}")
        names = tuple(self.assumed_names)
        if len(names) != 10 or len(set(names)) != 10:
            raise ValueError(f"assumed_names must be 10 distinct names, not {names!r}")
        object.__setattr__(self, "assumed_names", names)

    @classmethod
    def from_bytes(cls, b: Union[bytes, bytearray, memoryview]) -> "StatusBlob":
        """Decode the blob: exactly ``HEAD\\0`` + 10 bytes + ``TALE\\0`` (48 bytes).

        Any other length or sentinel raises :class:`ReadingParseError` with the
        bytes in hex. (The 19-byte ``G...S`` frame of other firmware is
        :class:`goliath_link.protocol.GlobalStatusFrame`, not this.)
        """
        what = "GLOBAL STATUS blob"
        if not isinstance(b, (bytes, bytearray, memoryview)):
            raise TypeError(f"{what}: expected bytes, not {type(b).__name__}")
        data = bytes(b)
        shown = data.hex(" ")
        if len(data) != GLOBAL_BLOB_LEN:
            raise ReadingParseError(what, f"{len(data)} bytes; the GFW 01.02.00 blob has "
                                          f"{GLOBAL_BLOB_LEN} (spec 09)", shown)
        if not data.startswith(GLOBAL_BLOB_HEAD):
            raise ReadingParseError(what, "does not start with 'GLOBAL STATUS HEAD' and a NUL",
                                    shown)
        if not data.endswith(GLOBAL_BLOB_TALE):
            raise ReadingParseError(what, "does not end with 'GLOBAL STATUS TALE' and a NUL",
                                    shown)
        return cls(raw10=data[_BLOB_STATUS_START:_BLOB_STATUS_START + 10])

    def to_bytes(self) -> bytes:
        return GLOBAL_BLOB_HEAD + self.raw10 + GLOBAL_BLOB_TALE

    def field(self, name: str) -> AssumedField:
        """The status byte under the assumed ``name`` (KeyError for another name)."""
        try:
            i = self.assumed_names.index(name)
        except ValueError:
            raise KeyError(f"no status byte is assumed to be called {name!r}; the assumed "
                           f"names are {self.assumed_names}") from None
        struct_offset = _STRUCT_OFFSETS[i]
        return AssumedField(
            name=name,
            value=self.raw10[i],
            blob_offset=_BLOB_STATUS_START + i,
            struct_offset=struct_offset,
            basis=(f"name {name!r} is assumed: matched by position to the vendor's INFO;1002 "
                   "template (spec 02 §Global status); the value encoding is unknown; the "
                   f"blob carries only the low byte of the 32-bit status field "
                   f"+{struct_offset} (spec 09 §The GLOBAL STATUS blob); " + _NOT_OBSERVED),
            corroboration=_CORROBORATION.get(struct_offset),
        )

    @property
    def pairing(self) -> AssumedField:
        return self.field("pairing")

    @property
    def pd_status(self) -> AssumedField:
        return self.field("pd_status")

    @property
    def gcode_uploaded(self) -> AssumedField:
        return self.field("gcode_uploaded")

    @property
    def work_status(self) -> AssumedField:
        return self.field("work_status")

    @property
    def resume_after_reset(self) -> AssumedField:
        return self.field("resume_after_reset")


# --------------------------------------------------------------------------- #
# The per-command expected-reply table
# --------------------------------------------------------------------------- #

#: Reply kinds (what a command's handler sends):
#:
#: ``info0``         ``INFO;0`` on the 2nd connection (the acknowledgement);
#: ``info0+direct``  ``INFO;0`` plus a direct reply to the connection that sent the
#:                   command (unpadded, explicit length);
#: ``info0+printf``  ``INFO;0`` plus free-form text sent to slot 0 (a 512-byte
#:                   NUL-padded block);
#: ``info34``        only ``INFO;34`` (identity), no ``INFO;0``;
#: ``info40``        only ``INFO;40`` (sanity report), no ``INFO;0``;
#: ``info-last``     re-sends the most recent ``INFO`` code, whatever it was;
#: ``direct``        only a direct reply to the sender, no ``INFO;0``;
#: ``stream``        a sequence of ``INFO`` lines (``INFO;31`` START ... END);
#: ``none``          nothing at all.
REPLY_KINDS: Tuple[str, ...] = ("info0", "info0+direct", "info0+printf", "info34", "info40",
                                "info-last", "direct", "stream", "none")
ReplyKind = Literal["info0", "info0+direct", "info0+printf", "info34", "info40",
                    "info-last", "direct", "stream", "none"]

#: Connection index that receives every INFO/ERR/WARN line (firmware slot 1).
ACK_CONN = 1
#: Connection of each kind's distinctive reply. Assumes the vendor arrangement:
#: S1/S2/S3 hold firmware slots 0/1/2 and commands are sent on S1 (spec 01
#: §Three connections and slot routing; spec 03 step 5). Direct replies go to
#: whichever connection sent the command (spec 09 §Direct replies to the
#: requester); with commands sent on S1 that is connection 0.
_KIND_CONN: Dict[str, Optional[int]] = {
    "info0": 1, "info0+direct": 0, "info0+printf": 0, "info34": 1, "info40": 1,
    "info-last": 1, "direct": 0, "stream": 1, "none": None,
}
#: Why each kind's reply arrives on its ``conn``: slot routing is static analysis
#: only, and the first probe of a real robot checks it, so every table entry
#: carries this as its ``conn_basis``.
_ARRANGEMENT = ("when S1/S2/S3 were opened in that order and hold firmware slots 0/1/2, and "
                "commands are sent on S1 (spec 01 §Three connections and slot routing; spec 03 "
                "§Connecting, step 5); " + _STATIC)
_SLOT1 = ("every INFO/ERR/WARN line goes to firmware slot 1 (spec 09 §Senders), which is S2 "
          "(connection 1) ")
_ACK_ON_S2 = "; its INFO;0 goes to slot 1, S2 (connection 1)"
_KIND_CONN_BASIS: Dict[str, str] = {
    "info0": _SLOT1 + _ARRANGEMENT,
    "info34": _SLOT1 + _ARRANGEMENT,
    "info40": _SLOT1 + _ARRANGEMENT,
    "info-last": _SLOT1 + _ARRANGEMENT,
    "stream": _SLOT1 + _ARRANGEMENT,
    "info0+direct": ("a direct reply goes to the connection that sent the command, the requester "
                     "(spec 09 §Direct replies to the requester), which is S1 (connection 0) "
                     + _ARRANGEMENT + _ACK_ON_S2),
    "direct": ("a direct reply goes to the connection that sent the command, the requester "
               "(spec 09 §Direct replies to the requester), which is S1 (connection 0) "
               + _ARRANGEMENT),
    "info0+printf": ("free-form printf text goes to firmware slot 0 (spec 09 §Senders), which is "
                     "S1 (connection 0) " + _ARRANGEMENT + _ACK_ON_S2),
    "none": "nothing is sent, so there is no connection to wait on",
}
#: What a ReplySpec built without a stated timeout or connection basis says: it
#: never borrows the provenance of this module's tables.
_NOT_STATED = ("not stated separately by whoever built this ReplySpec: it rests on the "
               "ReplySpec's basis")

#: Seconds to wait for an expected reply before declaring it missing.
DEFAULT_REPLY_TIMEOUT_S = 5.0
_TIMEOUT_BASIS = (
    "project default, not a firmware fact: no reply timing is documented for GFW 01.02.00 "
    "(spec 03 §Caveats: the vendor's timings are starting points), so 5 s, the read-only "
    "probe's default reply timeout, is used until a probe of a real robot measures replies"
)
_NO_WAIT_BASIS = "not a firmware fact either: nothing is expected, so nothing is waited for"


@dataclass(frozen=True)
class ReplySpec:
    """What one command is expected to produce, and on what that rests.

    ``kind`` is one of :data:`REPLY_KINDS`. ``conn`` is the connection index of
    the distinctive reply (0 = S1, 1 = S2) under the vendor arrangement (see
    :data:`ACK_CONN`), None for ``none``, justified by ``conn_basis``.
    ``terminator`` is how that reply ends on the wire (``"\\n"``), None when it
    has no terminator (it ends at NUL padding, has an explicit length, or
    depends on the value -- see ``note``) or when nothing is sent.
    ``timeout_s`` is how long to wait before declaring the reply missing (0 for
    ``none``), justified by ``timeout_basis``. ``basis`` is where the
    expectation comes from; ``observed`` turns True only once a real robot has
    been seen doing it. When ``timeout_basis`` or ``conn_basis`` is not given,
    it says so (``"not stated separately ..."``) rather than borrowing the
    provenance of this module's tables.

    ``err1_possible``: the handler may send ``ERR;1`` (CMD_NOT_EXECUTED) instead
    of ``INFO;0`` when a precondition fails, or the command is unknown and the
    reply to an unknown command is not established. ``reply`` is the format of
    the distinctive reply; ``table_record`` and ``handler`` locate the command in
    the firmware's table (None when it is not in it).

    Never read a missing reply as success (spec 01 §Outbound): with kind
    ``none`` there is nothing that could confirm the command at all.
    """

    kind: ReplyKind
    conn: Optional[int]
    terminator: Optional[str]
    timeout_s: float
    basis: str
    err1_possible: bool = False
    reply: Optional[str] = None
    table_record: Optional[int] = None
    handler: Optional[str] = None
    note: str = ""
    timeout_basis: Optional[str] = None
    observed: bool = False
    conn_basis: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("timeout_basis", "conn_basis"):
            value = getattr(self, name)
            if value is None:
                object.__setattr__(self, name, _NOT_STATED)
            elif not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must say where the value comes from, or be None, "
                                 f"not {value!r}")
        if self.kind not in REPLY_KINDS:
            raise ValueError(f"kind must be one of {REPLY_KINDS}, not {self.kind!r}")
        if self.kind == "none":
            if self.conn is not None:
                raise ValueError(f"conn must be None for kind 'none', not {self.conn!r}")
        elif isinstance(self.conn, bool) or self.conn not in (0, 1, 2):
            raise ValueError(f"conn must be a connection index 0-2 for kind {self.kind!r}, "
                             f"not {self.conn!r}")
        if self.terminator is not None and not isinstance(self.terminator, str):
            raise ValueError(f"terminator must be a string or None, not {self.terminator!r}")
        t = self.timeout_s
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or t < 0:
            raise ValueError(f"timeout_s must be a finite number of seconds >= 0, not {t!r}")
        if not isinstance(self.basis, str) or not self.basis.strip():
            raise ValueError("basis must say where the expectation comes from")

    @property
    def expects_info0(self) -> bool:
        """True when the handler acknowledges with ``INFO;0`` (on :data:`ACK_CONN`)."""
        return self.kind.startswith("info0")

    @property
    def ack_conn(self) -> Optional[int]:
        """Where the ``INFO;0`` arrives, or None when none is sent."""
        return ACK_CONN if self.expects_info0 else None


def _as_firmware(firmware: object) -> Optional[Version]:
    if firmware is None:
        return None
    if isinstance(firmware, (str, bytes)) or not isinstance(firmware, (tuple, list)):
        raise TypeError(f"firmware must be a version tuple such as (1, 2, 0), or None, "
                        f"not {firmware!r}")
    parts = tuple(firmware)
    if len(parts) != 3 or any(isinstance(p, bool) or not isinstance(p, int) or p < 0
                              for p in parts):
        raise ValueError(f"firmware must be three whole numbers >= 0, such as (1, 2, 0), "
                         f"not {firmware!r}")
    return parts  # type: ignore[return-value]


@dataclass(frozen=True)
class ReplyTable:
    """The expected replies of one firmware version.

    ``specs`` maps an upper-case command name to its :class:`ReplySpec`;
    ``unknown`` is what a name outside ``specs`` gets (None: no basis).
    Commands starting with one of ``no_basis_prefixes`` have no basis at all.
    """

    firmware: Version
    basis: str
    specs: Mapping[str, ReplySpec]
    unknown: Optional[ReplySpec]
    no_basis_prefixes: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        fw = _as_firmware(self.firmware)
        if fw is None:
            raise ValueError("a reply table belongs to one firmware version, not None")
        object.__setattr__(self, "firmware", fw)
        specs = dict(self.specs)
        for name, spec in specs.items():
            if not isinstance(name, str) or not isinstance(spec, ReplySpec):
                raise TypeError(f"specs must map command names to ReplySpec, not "
                                f"{name!r}: {spec!r}")
            if name != name.upper():
                raise ValueError(f"command names are upper case (the firmware upper-cases "
                                 f"the name), not {name!r}")
        if self.unknown is not None and not isinstance(self.unknown, ReplySpec):
            raise TypeError(f"unknown must be a ReplySpec or None, not {self.unknown!r}")
        object.__setattr__(self, "specs", MappingProxyType(specs))
        object.__setattr__(self, "no_basis_prefixes", tuple(self.no_basis_prefixes))


# -- GFW 01.02.00, transcribed from spec 09 "The command table" ------------- #

_CMD_TABLE = "spec 09 §The command table"
_UNTRACED = "messages from routines the handler calls were not traced"
_SINGLE = "single pass (†)"

# Distinctive replies: (reply format, terminator).
_R_INFO0 = ("INFO;0\\n on the 2nd connection", "\n")

# (record, name, handler, kind, err1, reply, terminator, single_pass, note)
_Row = Tuple[int, str, Optional[str], str, bool, Optional[str], Optional[str], bool, str]
_EMPTY = "empty handler on this firmware: does nothing, replies nothing"
_ROWS: Tuple[_Row, ...] = (
    # 0-3: matched, not dispatched (§3)
    (0, "COMMAND MODE ENTERED", None, "none", False, None, None, False, ""),
    (1, "EXIT COMMAND MODE", None, "none", False, None, None, False, ""),
    (2, "SYS RESET", None, "none", False, None, None, False,
     "no handler on 01.02.00 (not a remote reboot)"),
    (3, "SET PID", None, "none", False, None, None, False, ""),
    # 4-87: dispatched (§1, §2a, §2b)
    (4, "ENABLE SPINDLE", "0x0800c60e", "info0", False, *_R_INFO0, False, "spindle on"),
    (5, "DISABLE SPINDLE", "0x0800c61c", "info0", False, *_R_INFO0, False, ""),
    (6, "INFO", "0x0800c732", "info34", False, "INFO;34;%s;%d;%d;%04X%04X%04X\\n", "\n", False,
     "formats INFO;34 from constants (builder 0x08009c84); read it with parse_identity"),
    (7, "SET RTC TIME", "0x0800c740", "info0", True, *_R_INFO0, False, ""),
    (8, "SET RTC DATE", "0x0800c870", "info0", True, *_R_INFO0, False, ""),
    (9, "GET RTC DATE", "0x0800c91c", "info0", False, *_R_INFO0, False, ""),
    (10, "BOOT", "0x0800cc02", "none", False, None, None, False, _EMPTY),
    (11, "LOAD", "0x0800c08e", "direct", False,
     "\\n! Waiting for a hex file with %d lines \\r\\n", "\r\n", False,
     "enters the Wi-Fi firmware-load mode; never outside a prepared flash"),
    (12, "READ BACKUP", "0x0800cc02", "none", False, None, None, False, _EMPTY),
    (13, "EFLASH ERASE", "0x0800c92e", "info0", True, *_R_INFO0, False, ""),
    (14, "MOVE Z", "0x0800cb7c", "info0", True, *_R_INFO0, False, ""),
    (15, "RUN STEPPER", "0x0800c9d6", "info0", True, *_R_INFO0, False, ""),
    (16, "STOP STEPPER", "0x0800ca4a", "info0", True, *_R_INFO0, False, ""),
    (17, "GOTO POS STEPPER", "0x0800ca8c", "info0", True, *_R_INFO0, False, ""),
    (18, "START MOTORS", "0x0800c56a", "info0", False, *_R_INFO0, False, ""),
    (19, "MOVE STEPPER", "0x0800caf0", "direct", True, "Moving stepper!", None, False,
     "unterminated direct reply"),
    (20, "SET STEP CLOCK MODE", "0x0800cce2", "info0", True, *_R_INFO0, False, ""),
    (21, "SPINDLE START", "0x0800cc02", "none", False, None, None, False, _EMPTY),
    (22, "SPINDLE STOP", "0x0800cc02", "none", False, None, None, False, _EMPTY),
    (23, "GCODE UPLOAD", "0x0800cd40", "none", False, None, None, True,
     "the header starts upload mode: every later buffer, from any connection, is stored as "
     "program data until the upload completes, then the firmware sends INFO;0 "
     "(spec 09 §G-code upload mode)"),
    (24, "GCODE START", "0x0800cbec", "info0", True, *_R_INFO0, False, ""),
    (25, "GCODE STOP", "0x0800cc04", "info0", False, *_R_INFO0, False, ""),
    (26, "GCODE RESTART", "0x0800cc10", "info0", False, *_R_INFO0, False, ""),
    (27, "GCODE READ", "0x0800c71c", "direct", False, "count: %d", None, False,
     "unterminated direct reply"),
    (28, "ZERO SET", "0x0800cb66", "info0", False, *_R_INFO0, False, ""),
    (29, "GOTO TARGET", "0x0800c646", "info0", False, *_R_INFO0, False,
     "INFO;32 (target reached) follows when the move ends (spec 02 Jog); it is not sent by "
     "the handler body"),
    (30, "EXIT JOG", "0x0800c6dc", "info0", True, *_R_INFO0, False, ""),
    (31, "SET SETUP PHASE", "0x0800c7d6", "info0", False, *_R_INFO0, False, ""),
    (32, "SET WORK PHASE", "0x0800c7fe", "info0", False, *_R_INFO0, False, ""),
    (33, "SET VERTEX", "0x0800c944", "info0", False, *_R_INFO0, False, ""),
    (34, "GET VERTEXES", "0x0800c9a2", "info0", False, *_R_INFO0, False, ""),
    (35, "SET PANEL SIZE", "0x0800cc18", "info0", False, *_R_INFO0, False, ""),
    (36, "GET PANEL SIZE", "0x0800cc90", "info0+direct", False, "xmax=%.4f \\n\\rymax=%.4f",
     None, False, "unpadded, unterminated direct reply"),
    (37, "ENABLE WORK MON", "0x0800cd6a", "info0", False, *_R_INFO0, False, ""),
    (38, "ENABLE INFO LOG", "0x0800cd90", "info0", False, *_R_INFO0, False, ""),
    (39, "TOWERS POSITION", "0x0800cdb0", "info0", False, *_R_INFO0, False, ""),
    (40, "GET TOWERS DISTANCE", "0x0800ceac", "info0", False, *_R_INFO0, False, ""),
    (41, "SET START POS", "0x0800cebc", "none", False, None, None, False, _EMPTY),
    (42, "GET START POS", "0x0800cc02", "none", False, None, None, False, _EMPTY),
    (43, "WORK SPEED MOD", "0x0800cebe", "info0", False, *_R_INFO0, False, ""),
    (44, "REQ UPD POS", "0x0800cc02", "none", False, None, None, False, _EMPTY),
    (45, "SET RF CHANNELS", "0x0800c524", "info0", False, *_R_INFO0, False,
     "pairing then runs in the robot's state machine: INFO;3, tower frames, INFO;4 "
     "(spec 03 row B); not sent by the handler body"),
    (46, "SET T2 POSITION", "0x0800c4b8", "info0", False, *_R_INFO0, False, ""),
    (47, "BTN CLICK", "0x0800c4ac", "info0", False, *_R_INFO0, False, ""),
    (48, "RESET NET KEY", "0x0800c49e", "info0", False, *_R_INFO0, False, ""),
    (49, "ENABLE MANUAL JOG", "0x0800c488", "info0", False, *_R_INFO0, False, ""),
    (50, "ENTER JOG", "0x0800c47c", "info0", False, *_R_INFO0, False,
     "the vendor app then waits for INFO;35 (jog mode, spec 02 Jog); not sent by the "
     "handler body"),
    (51, "ENABLE ASPIRATION", "0x0800c62a", "info0", False, *_R_INFO0, False, ""),
    (52, "DISABLE ASPIRATION", "0x0800c638", "info0", False, *_R_INFO0, False, ""),
    (53, "ENTER ENCODER CALIBRATION", "0x0800cc02", "none", False, None, None, False, _EMPTY),
    (54, "EXIT ENCODER CALIBRATION", "0x0800cc02", "none", False, None, None, False, _EMPTY),
    (55, "SCAN WIFI", "0x0800c440", "info0", False, *_R_INFO0, False, ""),
    (56, "ENTER MANUAL ENC CALIBRATION", "0x0800c3ec", "info0", False, *_R_INFO0, False, ""),
    (57, "EXIT MANUAL ENC CALIBRATION", "0x0800c3f8", "info0", False, *_R_INFO0, False, ""),
    (58, "SET INDEX CALIBRATION VALUE", "0x0800c404", "info0", False, *_R_INFO0, False, ""),
    (59, "JOG STOP GOLIATH", "0x0800cd8a", "none", False, None, None, False,
     "stops a jog move; nothing confirms it"),
    (60, "DISABLE MANUAL JOG", "0x0800c2ca", "info0", False, *_R_INFO0, False, ""),
    (61, "SPINDLE ON", "0x0800c296", "info0", False, *_R_INFO0, False, ""),
    (62, "SPINDLE OFF", "0x0800c2a2", "info0", False, *_R_INFO0, False, ""),
    (63, "ASP ON", "0x0800c2ae", "info0", False, *_R_INFO0, False, ""),
    (64, "ASP OFF", "0x0800c2bc", "info0", False, *_R_INFO0, False, ""),
    (65, "READ INDEX CALIBRATION", "0x0800c42a", "info0+printf", False,
     "cfg read index : %d", None, True,
     "slot-0 text without a newline (ends at the NUL padding); read it with parse_index"),
    (66, "POLL CAGE", "0x0800cef4", "info0", False, *_R_INFO0, False,
     "also starts a 500 ms timer that prints 'CAGE STATUS: %s\\n' to slot 0 until STOP POLL "
     "CAGE (spec 09 §Free-form text on slot 0, single pass)"),
    (67, "STOP POLL CAGE", "0x0800cf12", "info0", False, *_R_INFO0, False, ""),
    (68, "ENTER TEST PHASE", "0x0800c264", "info0", False, *_R_INFO0, False, ""),
    (69, "EXIT TEST PHASE", "0x0800c27c", "info0", False, *_R_INFO0, False, ""),
    (70, "PRINT SYS REPORT", "0x0800c25e", "info40", False, "INFO;40;{15 x %d};END\\n", "\n",
     False, "prints stored sanity bytes (0x08013e3c) and does not run SANITY CHECK; read it "
            "with parse_sanity"),
    (71, "POLL ENCODER", "0x0800c240", "none", False, None, None, False,
     "by its name it starts periodic output; what it prints was not traced"),
    (72, "STOP POLL ENCODER", "0x0800c256", "none", False, None, None, False, ""),
    (73, "TEST Z CAL", "0x0800c20e", "info0", False, *_R_INFO0, False, ""),
    (74, "TEST RF", "0x0800c22c", "info0", False, *_R_INFO0, False, ""),
    (75, "SPINDLE CALIB VALUE", "0x0800c1d0", "info0", True, *_R_INFO0, False, ""),
    (76, "READ SPINDLE CALIB", "0x0800c1ac", "info0+printf", False,
     "spindle distance: %.2f", None, True,
     "slot-0 text without a newline (ends at the NUL padding); read it with "
     "parse_spindle_distance"),
    (77, "PRINT ENCODER CAN", "0x0800c192", "info0+printf", False, "ENC Test Started", None,
     True, "starts an encoder test"),
    (78, "LAST STATUS", "0x0800cf28", "info-last", False,
     "INFO;%d\\n (the most recent INFO code, re-sent)", "\n", True, ""),
    (79, "FREE Z AXIS", "0x0800cbe6", "none", False, None, None, False,
     "raises Z; nothing confirms it"),
    (80, "SANITY CHECK", "0x0800cc02", "none", False, None, None, False, _EMPTY),
    (81, "LOAD WIFI", "0x0800cc02", "none", False, None, None, False, _EMPTY),
    (82, "GET SSIDS", "0x0800c18c", "stream", False,
     "INFO;31;START\\n, INFO;31;%s\\n per network, INFO;31;END\\n", "\n", False,
     "a Wi-Fi scan: how long it takes is unknown"),
    (83, "GET SPINDLE CAGE STATUS", "0x0800c168", "info0+direct", False,
     "CAGE STATUS: OPEN\\n (18 bytes) | CAGE STATUS: CLOSED (19 bytes, no newline)", None,
     True, "the terminator depends on the value: OPEN ends in a newline, CLOSED has none; "
           "read it with parse_cage"),
    (84, "GET PARAM MEMORY", "0x0800c0e4", "info0", False, *_R_INFO0, False, ""),
    (85, "SET PARAM MEMORY", "0x0800c118", "info0+printf", False,
     "Recived Parameters are INVALID | Successfull Store of Parameters (sic)", None, True,
     "a persistent parameter write"),
    (86, "GET GLOBAL SYS STATUS", "0x0800c07a", "info0+direct", False,
     "the 48-byte GLOBAL STATUS blob (explicit length, unpadded)", None, False,
     "read it with StatusBlob.from_bytes"),
    (87, "RESET PARAM MEMORY", "0x0800c06c", "info0", False, *_R_INFO0, False, ""),
    # 88-93: matched, not dispatched (§3)
    (88, "", None, "none", False, None, None, False, "record 88: an empty name (prefix \\r\\n)"),
    (89, "OK", None, "none", False, None, None, False, ""),
    (90, "INVALID COMMAND", None, "none", False, None, None, False,
     "nothing dispatches to it; unknown commands get no reply"),
    (91, "INVALID VALUE", None, "none", False, None, None, False, ""),
    (92, "ERROR", None, "none", False, None, None, False, ""),
    (93, "FIRST BACKUP", None, "none", False, None, None, False, ""),
)
assert [r[0] for r in _ROWS] == list(range(94))

#: Vendor-app commands that GFW 01.02.00 does not have (spec 02, spec 09 §Vendor-app
#: commands that are not in the table).
_ABSENT_ON_01_02_00 = (
    "ABORT WORK", "GCODE CONTINUE", "SEND TCP POS", "GET Z BACKLASH", "SET Z BACKLASH",
    "GET Z PLATE OFFSET", "SET Z PLATE OFFSET", "SET SPINDLE DISTANCE",
    "DISCARD TWRS DISTANCE", "CMD SYS RESET",
)

_UNKNOWN_BASIS = (
    "statically no reply and no effect (spec 01 §Outbound, spec 09 §How a command is "
    "parsed); spec 01 says to treat an error reply as possible as well; " + _NOT_OBSERVED
)


def _spec_from_row(row: _Row) -> Tuple[str, ReplySpec]:
    record, name, handler, kind, err1, reply, terminator, single, note = row
    if handler is None:
        basis = (f"{_CMD_TABLE}, record {record}: matched but not dispatched (§3), so no "
                 f"effect and no reply; {_STATIC}")
    else:
        sends = {
            "none": "nothing",
            "info0": "INFO;0",
            "info0+direct": f"INFO;0, and to the requester the direct reply {reply}",
            "info0+printf": f"INFO;0, and printf to slot 0: {reply}",
            "direct": f"no INFO;0; to the requester the direct reply {reply}",
        }.get(kind, f"no INFO;0; {reply}")
        if err1:
            sends += "; ERR;1 when a precondition fails"
        empty = " (empty handler)" if note == _EMPTY else ""
        basis = (f"{_CMD_TABLE}, record {record}, handler {handler}{empty}: 'Handler sends' "
                 f"{sends}" + (f"; {_SINGLE}" if single else "") + f"; {_UNTRACED}; {_STATIC}")
    nothing = kind == "none"
    return name, ReplySpec(
        kind=kind,  # type: ignore[arg-type]
        conn=_KIND_CONN[kind],
        terminator=terminator,
        timeout_s=0.0 if nothing else DEFAULT_REPLY_TIMEOUT_S,
        basis=basis,
        err1_possible=err1,
        reply=reply,
        table_record=record,
        handler=handler,
        note=note,
        timeout_basis=_NO_WAIT_BASIS if nothing else _TIMEOUT_BASIS,
        conn_basis=_KIND_CONN_BASIS[kind],
    )


def _absent_spec(name: str) -> ReplySpec:
    return ReplySpec(
        kind="none", conn=None, terminator=None, timeout_s=0.0,
        basis=(f"{name!r} is absent from the GFW 01.02.00 command table (spec 02; spec 09 "
               "§Vendor-app commands that are not in the table): an unknown command there, "
               + _UNKNOWN_BASIS),
        err1_possible=True, timeout_basis=_NO_WAIT_BASIS, conn_basis=_KIND_CONN_BASIS["none"],
        note="exists only on newer firmware, if at all; it does nothing on 01.02.00",
    )


_UNKNOWN_01_02_00 = ReplySpec(
    kind="none", conn=None, terminator=None, timeout_s=0.0,
    basis="not in the GFW 01.02.00 command table: an unknown command, " + _UNKNOWN_BASIS,
    err1_possible=True, timeout_basis=_NO_WAIT_BASIS, conn_basis=_KIND_CONN_BASIS["none"],
    note="a terminator ('INFO\\n') or two commands in one buffer also make an unknown name",
)


def _gfw_01_02_00_specs() -> Dict[str, ReplySpec]:
    specs = dict(_spec_from_row(r) for r in _ROWS)
    for name in _ABSENT_ON_01_02_00:
        assert name not in specs
        specs[name] = _absent_spec(name)
    return specs


#: The firmware :data:`EXPECTED_REPLY` describes. No other firmware has a basis.
EXPECTED_REPLY_FIRMWARE: Version = (1, 2, 0)

_GFW_01_02_00 = ReplyTable(
    firmware=EXPECTED_REPLY_FIRMWARE,
    basis="spec 09 §The command table ('Handler sends'), static analysis of the only "
          "surviving firmware image; " + _NOT_OBSERVED,
    specs=_gfw_01_02_00_specs(),
    unknown=_UNKNOWN_01_02_00,
    # The Wi-Fi provisioning string is not a table command; it "presumably goes
    # through the provisioning-mode path (not traced)" (spec 09).
    no_basis_prefixes=("SSID=",),
)

#: Expected replies on GFW 01.02.00, by upper-case command name: all 94 names of
#: the firmware's command table (record 88 is the empty name) plus the ten
#: vendor-app commands the firmware does not have. Read-only.
EXPECTED_REPLY: Mapping[str, ReplySpec] = _GFW_01_02_00.specs

#: Reply tables by firmware version. Only GFW 01.02.00 has one: for newer (or
#: older) firmware there is no basis. Read-only; pass ``tables=`` to
#: :func:`expected_reply` to use others.
REPLY_TABLES: Mapping[Version, ReplyTable] = MappingProxyType({EXPECTED_REPLY_FIRMWARE: _GFW_01_02_00})


def expected_reply(command: str, firmware: Optional[Version], *,
                   tables: Optional[Mapping[Version, ReplyTable]] = None) -> Optional[ReplySpec]:
    """What ``command`` is expected to produce on ``firmware``, or None (no basis).

    The name is read as the firmware reads it: the text before the first ``;``,
    upper-cased (``"SET PANEL SIZE;1200;800"`` -> ``SET PANEL SIZE``). A name the
    table does not have gets the table's rule for unknown commands (on GFW
    01.02.00: no reply, ``err1_possible``); a terminator makes a name unknown.

    Returns None -- no basis, so expect nothing and assume nothing -- when
    ``firmware`` is None (the version has not been read: never assume 01.02.00),
    when no table exists for it (any firmware but 01.02.00, unless ``tables``
    provides one), or for the Wi-Fi provisioning string. Raises TypeError or
    ValueError for a malformed ``firmware``, and ValueError for a command that
    is not ASCII or contains a NUL byte: commands are ASCII bytes (spec 01
    §Outbound; the transport sends ASCII only), and how the firmware would read
    such a name is not documented (Python's ``str.upper()`` turns ``'ınfo'``
    into ``'INFO'``; the firmware compares bytes, and its C-string name ends at
    a NUL).

    While a G-code upload is in progress the firmware parses nothing: every
    buffer is program data and no command gets any reply (spec 09 §G-code upload
    mode). That state is the caller's to track.
    """
    if not isinstance(command, str):
        raise TypeError(f"command must be a string, not {type(command).__name__}")
    if not command.isascii():
        raise ValueError(f"command {command!r} is not ASCII: commands are ASCII bytes (spec 01 "
                         "§Outbound), and how the firmware would read other bytes is not "
                         "documented, so no reply can be predicted")
    if "\x00" in command:
        raise ValueError(f"command {command!r} contains a NUL byte: the firmware's name ends "
                         "there as a C string, which is not documented (spec 09 §How a command "
                         "is parsed), so no reply can be predicted")
    fw = _as_firmware(firmware)
    if fw is None:
        return None
    table = (REPLY_TABLES if tables is None else tables).get(fw)
    if table is None:
        return None
    if not isinstance(table, ReplyTable):
        raise TypeError(f"tables must map firmware versions to ReplyTable, not {table!r}")
    if any(command.upper().startswith(p.upper()) for p in table.no_basis_prefixes):
        return None
    name = command.split(";", 1)[0].upper()
    spec = table.specs.get(name)
    return spec if spec is not None else table.unknown

