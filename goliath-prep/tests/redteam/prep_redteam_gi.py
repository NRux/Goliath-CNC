# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""A small, independent reader for the red-team suite's own input files (test code only).

It reads GI (docs/prep/gi-format.md) and the Goliath dialect (docs/protocol/05)
far enough to check the red-team cases themselves, before goliath-prep's
pipeline exists:

* a geometric case must read cleanly, so that its expected refusal cannot come
  from the reader instead of from the check the case is about;
* a reader case must carry exactly the defect it names, at the line it names,
  and nothing earlier that a reader could stop at (gi-format section 11: a
  reader may stop at the first refusal);
* the GI header becomes the model of docs/prep/gi.schema.json, which the suite
  validates against the schema.

It was written from the documents only and shares no code with goliath-prep:
it is not goliath-prep's reader and does not stand in for it. It covers the
rules the cases exercise; where a rule of gi-format.md is not implemented
here, the reader says so instead of passing the line silently
(:class:`NotCovered`).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

__all__ = [
    "DialectMove",
    "DialectProgram",
    "GIMove",
    "GIProgram",
    "NotCovered",
    "ReaderFinding",
    "read_dialect",
    "read_gi",
    "split_lines",
]


class NotCovered(Exception):
    """The file uses a GI rule this test reader does not implement: the case must be checked by hand."""


@dataclass(frozen=True)
class ReaderFinding:
    """A refusal or warning of the test reader: the finding id of gi-format section 12 and the GI line."""

    id: str
    line: int
    column: int | None
    text: str


@dataclass(frozen=True)
class GIMove:
    """One motion line, with absolute coordinates in mm (as written; ZSHIFT never applied)."""

    line: int
    code: int  # 0, 1, 2 or 3
    start: tuple[float | None, float | None, float | None]
    end: tuple[float, float, float | None]
    centre: tuple[float, float] | None  # arcs only
    sweep_deg: float | None  # arcs only
    op: str | None  # the id of the OP block the line is in
    forced_c: int | None  # that block's forced heading


@dataclass
class GIProgram:
    """What :func:`read_gi` read."""

    header: dict[str, Any] | None
    moves: list[GIMove] = field(default_factory=list)
    findings: list[ReaderFinding] = field(default_factory=list)
    ops: list[dict[str, Any]] = field(default_factory=list)

    @property
    def refusals(self) -> list[ReaderFinding]:
        return [f for f in self.findings if f.id != "N-GI-UNKNOWN-KEY"]


def split_lines(text: str) -> list[str]:
    """Split like .NET File.ReadAllLines: at CRLF, CR or LF; a final terminator starts no new line."""
    lines = re.split(r"\r\n|\r|\n", text)
    if lines and lines[-1] == "" and re.search(r"(\r\n|\r|\n)\Z", text):
        lines.pop()
    return lines


# ---------------------------------------------------------------------------
# GI
# ---------------------------------------------------------------------------

_NUMBER = r"[+-]?\d+(?:\.\d+)?"
_WORD = re.compile(r"[ \t]*([A-Z])(%s)" % _NUMBER)
_TOKEN = re.compile(r"[A-Za-z0-9_.+-]+")
_KEY = re.compile(r"[a-z][a-z0-9_]*\Z")
_KEYWORD = re.compile(r"[A-Z][A-Z0-9]*\Z")
_HEADER_KEYWORDS = ("SOURCE", "UNITS", "STOCK", "TOWER2", "TOOL", "ZSHIFT")
_REQUIRED_HEADER = ("SOURCE", "UNITS", "STOCK", "TOWER2", "TOOL")
_KEYS = {
    "SOURCE": {"cam": "text", "post": "text"},
    "UNITS": {},
    "STOCK": {"x": "length", "y": "length", "t": "length", "origin": ("t1-corner",), "zzero": ("top", "bottom")},
    "TOWER2": {"axis": ("X", "Y")},
    "TOOL": {"n": "integer", "d": "length", "flute": "length", "stickout": "length",
             "kind": ("flat", "ball", "bull", "vbit", "other"), "rpm": "positive", "name": "text"},
    "ZSHIFT": {},
    "OP": {"id": "token", "kind": ("profile", "pocket", "drill", "engrave", "adaptive", "other"),
           "through": ("auto", "1", "0"), "tool": "integer", "label": "text", "c": "integer"},
    "OPEND": {"id": "token"},
}
_REQUIRED_KEYS = {
    "SOURCE": ("cam", "post"),
    "STOCK": ("x", "y", "t", "origin", "zzero"),
    "TOWER2": ("axis",),
    "TOOL": ("n", "d", "flute"),
    "OP": ("id", "kind", "through", "tool"),
    "OPEND": ("id",),
}
_NOOP_G = frozenset({17, 21, 90, 94, 54, 40})
_UNSUPPORTED_G = frozenset({91, 18, 19, 41, 42, 28, 30, 53, 92, 55, 56, 57, 58, 59} | set(range(80, 90)))
_UNSUPPORTED_M = frozenset({0, 1, 4, 10, 11})
_COORD_LIMIT = Decimal(10000)


def _items(rest: str, line_no: int) -> tuple[list[tuple[str | None, str, bool]], str | None]:
    """Split the items of an annotation; returns (items, error). An item is (key, value, quoted)."""
    items: list[tuple[str | None, str, bool]] = []
    i = 0
    n = len(rest)
    while i < n:
        while i < n and rest[i] in " \t":
            i += 1
        if i >= n:
            break
        key = None
        m = re.match(r"([a-z][a-z0-9_]*)=", rest[i:])
        if m:
            key = m.group(1)
            i += m.end()
        if i < n and rest[i] == '"':
            j = i + 1
            value = []
            while True:
                if j >= n:
                    return items, "unterminated quoted value"
                ch = rest[j]
                if ch == '"':
                    break
                if ch == "\\":
                    if j + 1 < n and rest[j + 1] in '"\\':
                        value.append(rest[j + 1])
                        j += 2
                        continue
                    return items, "a backslash sequence other than \\\" or \\\\"
                if ord(ch) < 0x20 or ord(ch) == 0x7F:
                    return items, "a control character in a quoted value"
                value.append(ch)
                j += 1
            items.append((key, "".join(value), True))
            i = j + 1
            if i < n and rest[i] not in " \t":
                return items, "no space after a quoted value"
        else:
            m = _TOKEN.match(rest, i)
            if not m:
                return items, "not a token at column %d" % (i + 1)
            items.append((key, m.group(0), False))
            i = m.end()
            if i < n and rest[i] not in " \t":
                return items, "unexpected character %r" % rest[i]
    return items, None


def _typed(kind: Any, value: str, quoted: bool) -> Any:
    """The value of a key as the schema records it, or raises ValueError."""
    if isinstance(kind, tuple):
        if quoted or value not in kind:
            raise ValueError("not one of %s" % (kind,))
        return value
    if kind == "text":
        return value
    if kind == "token":
        if quoted:
            raise ValueError("a token is never quoted")
        return value
    if quoted:
        raise ValueError("a number is never quoted")
    if kind == "integer":
        if not re.fullmatch(r"\d+", value):
            raise ValueError("not an integer")
        return int(value)
    if not re.fullmatch(_NUMBER, value):
        raise ValueError("not a number")
    number = Decimal(value)
    if kind in ("length", "positive") and number <= 0:
        raise ValueError("must be greater than zero")
    return number


def _dec_float(value: Decimal) -> float | int:
    return int(value) if value == value.to_integral_value() and "." not in str(value) else float(value)


def read_gi(text: str, *, job: Mapping[str, Any] | None = None) -> GIProgram:
    """Read a GI file; ``job`` (the job object: panel, placement, z_zero, tool) enables the conflict rules."""
    if text.startswith("﻿"):
        text = text[1:]
    lines = split_lines(text)
    prog = GIProgram(header=None)
    refuse = prog.findings.append

    has_annotation = any(";" in ln and ln.split(";", 1)[1].lstrip(" \t").upper().startswith("GOLIATH")
                         for ln in lines)
    # "In a file with at least one OP line" (gi-format section 7) is a property of the whole file.
    has_op = any(re.match(r";GOLIATH[ \t]+OP([ \t]|\Z)", ln) for ln in lines)
    if not lines or not re.fullmatch(r";GOLIATH-GI[ \t]+(0|[1-9]\d*)[ \t]*", lines[0]):
        if has_annotation:
            refuse(ReaderFinding("D-GI-VERSION", 1, None, "line 1 is not the version line"))
            return prog
        raise NotCovered("not a GI file (no annotations): generic G-code is not covered by this reader")
    if lines[0].split()[1] != "1":
        refuse(ReaderFinding("D-GI-VERSION", 1, None, "unknown GI version"))
        return prog

    header: dict[str, Any] = {}
    header_open = True
    header_end_line: int | None = None
    ops: list[dict[str, Any]] = []
    open_op: dict[str, Any] | None = None
    ended = False
    mode: int | None = None
    pos: list[Decimal | None] = [None, None, None]
    tool_word_seen = False
    m6_seen = False
    motion_seen = False

    def end_header(line_no: int) -> None:
        nonlocal header_open, header_end_line
        if not header_open:
            return
        header_open = False
        header_end_line = line_no
        for keyword in _REQUIRED_HEADER:
            if keyword not in header:
                refuse(ReaderFinding("D-GI-HEADER", line_no, None, "missing %s line" % keyword))

    for line_no, raw in enumerate(lines[1:], start=2):
        line = raw.rstrip(" \t")
        code, _, comment = line.partition(";")
        has_comment = ";" in line
        bad = [i for i, ch in enumerate(code) if ord(ch) > 0x7F]
        if bad:
            refuse(ReaderFinding("D-SYNTAX", line_no, bad[0] + 1, "non-ASCII character outside a comment"))
            continue
        kind = None
        if has_comment:
            stripped = comment.lstrip(" \t")
            if stripped.upper().startswith("GOLIATH"):
                kind = "annotation"
            elif stripped[:1] in ("U", "u", "S", "s"):
                kind = "marker"
            else:
                kind = "plain"
        if kind == "marker":
            refuse(ReaderFinding("D-MARKER", line_no, None, "a ;U or ;S marker in a GI file"))
            continue
        if kind == "annotation":
            if ended:
                refuse(ReaderFinding("D-AFTER-END", line_no, None, "an annotation after M2/M30"))
                continue
            if code.strip() != "" or not re.match(r";GOLIATH(-GI)?([ \t]|\Z)", line):
                refuse(ReaderFinding("D-GI-SYNTAX", line_no, None, "not a well-formed annotation line"))
                continue
            if line.startswith(";GOLIATH-GI"):
                refuse(ReaderFinding("D-GI-VERSION", line_no, None, "a second version line"))
                continue
            rest = line[len(";GOLIATH"):]
            m = re.match(r"[ \t]+([A-Z][A-Z0-9]*)", rest)
            if not m:
                refuse(ReaderFinding("D-GI-SYNTAX", line_no, None, "no keyword"))
                continue
            keyword = m.group(1)
            items, error = _items(rest[m.end():], line_no)
            if error:
                refuse(ReaderFinding("D-GI-SYNTAX", line_no, None, error))
                continue
            keys = [k for k, _, _ in items if k is not None]
            if len(keys) != len(set(keys)):
                refuse(ReaderFinding("D-GI-SYNTAX", line_no, None, "a key given twice"))
                continue
            if keyword not in _KEYS:
                prog.findings.append(ReaderFinding("N-GI-UNKNOWN-KEY", line_no, None, "unknown keyword %s" % keyword))
                continue
            finding = "D-GI-OP" if keyword in ("OP", "OPEND") else "D-GI-HEADER"
            spec = _KEYS[keyword]
            values: dict[str, Any] = {}
            positional = [(v, q) for k, v, q in items if k is None]
            problem = None
            for key, value, quoted in items:
                if key is None:
                    continue
                if key not in spec:
                    prog.findings.append(ReaderFinding("N-GI-UNKNOWN-KEY", line_no, None, "unknown key %s" % key))
                    continue
                try:
                    values[key] = _typed(spec[key], value, quoted)
                except ValueError as exc:
                    problem = "%s=%s: %s" % (key, value, exc)
                    break
            if problem is None:
                for key in _REQUIRED_KEYS.get(keyword, ()):
                    if key not in values:
                        problem = "missing key %s" % key
                        break
            if problem is None and keyword == "UNITS":
                if len(positional) != 1:
                    problem = "UNITS takes one positional value"
                elif positional[0][0] != "mm":
                    refuse(ReaderFinding("D-UNITS", line_no, None, "units %s, not mm" % positional[0][0]))
                    continue
                else:
                    values["value"] = "mm"
            elif problem is None and keyword == "ZSHIFT":
                if len(positional) != 1 or positional[0][1] or not re.fullmatch(_NUMBER, positional[0][0]):
                    problem = "ZSHIFT takes one number"
                else:
                    values = {"value": Decimal(positional[0][0]), "applied": False}
            elif problem is None and positional:
                problem = "unexpected positional value"
            if problem is not None:
                refuse(ReaderFinding(finding, line_no, None, problem))
                continue
            if keyword in _HEADER_KEYWORDS:
                if not header_open:
                    refuse(ReaderFinding("D-GI-HEADER", line_no, None, "a header line after the header ended"))
                    continue
                if keyword in header:
                    refuse(ReaderFinding("D-SINGLETOOL" if keyword == "TOOL" else "D-GI-HEADER", line_no, None,
                                         "a second %s line" % keyword))
                    continue
                values["line"] = line_no
                header[keyword] = values
                continue
            end_header(line_no)
            if keyword == "OP":
                if open_op is not None:
                    refuse(ReaderFinding("D-GI-OP", line_no, None, "OP blocks do not nest"))
                    continue
                if any(op["id"] == values["id"] for op in ops):
                    refuse(ReaderFinding("D-GI-OP", line_no, None, "duplicate OP id"))
                    continue
                if "TOOL" in header and values["tool"] != header["TOOL"]["n"]:
                    refuse(ReaderFinding("D-GI-OP", line_no, None, "OP tool names no declared tool"))
                    continue
                if "c" in values and (values["c"] % 10 or values["c"] > 360):
                    refuse(ReaderFinding("D-GI-OP", line_no, None, "forced c off the 10-degree grid"))
                    continue
                values["line"] = line_no
                open_op = values
                ops.append(values)
            else:  # OPEND
                if open_op is None or open_op["id"] != values["id"]:
                    refuse(ReaderFinding("D-GI-OP", line_no, None, "OPEND without the matching OP"))
                    continue
                open_op["end_line"] = line_no
                open_op = None
            continue
        if code.strip() == "":
            continue
        # A line with G-code.
        if ended:
            refuse(ReaderFinding("D-AFTER-END", line_no, None, "a word after M2/M30"))
            continue
        end_header(line_no)
        words: list[tuple[str, str, int]] = []
        i = 0
        syntax = None
        while i < len(code):
            if code[i] in " \t":
                i += 1
                continue
            m = _WORD.match(code, i)
            if not m:
                syntax = i + 1
                break
            col = i + 1 + (len(m.group(0)) - len(m.group(0).lstrip(" \t")))
            words.append((m.group(1), m.group(2), col))
            i = m.end()
            if i < len(code) and code[i] not in " \t" and not ("A" <= code[i] <= "Z"):
                syntax = i + 1
                break
        if syntax is not None:
            refuse(ReaderFinding("D-SYNTAX", line_no, syntax, "not a word"))
            continue
        letters = [w[0] for w in words]
        g_codes: list[int] = []
        m_codes: list[int] = []
        problem: tuple[str, str] | None = None
        for letter, number, _col in words:
            if letter in "GMTHN" and not re.fullmatch(r"\d+", number):
                problem = ("D-UNSUPPORTED" if letter in "GM" else "D-SYNTAX", "%s%s" % (letter, number))
                break
            if letter == "G":
                g_codes.append(int(number))
            elif letter == "M":
                m_codes.append(int(number))
        if problem is None:
            for letter in set(letters):
                if letter != "G" and letters.count(letter) > 1:
                    problem = ("D-SYNTAX", "the letter %s twice" % letter)
            if len([g for g in g_codes if g in (0, 1, 2, 3)]) > 1 or len(m_codes) > 1:
                problem = ("D-SYNTAX", "two motion codes or two M words")
        if problem is None:
            for g in g_codes:
                if g == 20:
                    problem = ("D-UNITS", "G20")
                elif g == 4:
                    problem = ("G-GATE", "G4 while drop_dwells is closed")
                elif g in _UNSUPPORTED_G or (g not in (0, 1, 2, 3, 43) and g not in _NOOP_G):
                    problem = ("D-UNSUPPORTED", "G%d" % g)
                if problem:
                    break
        if problem is None:
            for mm in m_codes:
                if mm in _UNSUPPORTED_M or mm not in (2, 3, 5, 6, 7, 8, 9, 30):
                    problem = ("D-UNSUPPORTED", "M%d" % mm)
                    break
        if problem is None:
            for letter in letters:
                if letter in "KR":
                    problem = ("D-ARC", "%s word" % letter)
                elif letter in "CAB":
                    problem = ("D-UNSUPPORTED", "%s word" % letter)
                elif letter == "H" and 43 not in g_codes:
                    problem = ("D-UNSUPPORTED", "H without G43")
                elif letter == "P":
                    problem = ("D-UNSUPPORTED", "P without G4")
                elif letter not in "GMXYZIJFSTNH":
                    problem = ("D-UNSUPPORTED", "%s word" % letter)
                if problem:
                    break
        if problem is None:
            for letter, number, _col in words:
                if letter in "XYZIJ" and abs(Decimal(number)) > _COORD_LIMIT:
                    problem = ("D-RANGE", "%s beyond 10000 mm" % letter)
                elif letter in "FS" and Decimal(number) < 0:
                    problem = ("D-RANGE", "negative %s" % letter)
        if problem is None and (2 in m_codes or 30 in m_codes):
            if [w for w in letters if w not in ("M", "N")]:
                problem = ("D-AFTER-END", "M2/M30 shares its line")
            else:
                ended = True
                continue
        if problem is None:
            if "T" in letters or 6 in m_codes:
                tool_n = header.get("TOOL", {}).get("n")
                t_words = [int(n) for letter, n, _ in words if letter == "T"]
                if motion_seen or (6 in m_codes and not t_words and not tool_word_seen) or \
                        (t_words and (tool_word_seen or t_words[0] != tool_n)) or (6 in m_codes and m6_seen):
                    problem = ("D-SINGLETOOL", "a second tool, or T/M6 after motion")
                tool_word_seen = tool_word_seen or bool(t_words)
                m6_seen = m6_seen or 6 in m_codes
        if problem is not None:
            refuse(ReaderFinding(problem[0], line_no, None, problem[1]))
            continue
        motion = [g for g in g_codes if g in (0, 1, 2, 3)]
        if motion:
            mode = motion[0]
        axis = {letter: Decimal(n) for letter, n, _ in words if letter in "XYZIJ"}
        if not axis:
            continue
        if mode is None:
            refuse(ReaderFinding("D-SYNTAX", line_no, None, "axis words with no motion mode"))
            continue
        if mode in (0, 1) and ("I" in axis or "J" in axis):
            refuse(ReaderFinding("D-ARC", line_no, None, "I or J on a linear move"))
            continue
        start = tuple(pos)
        end = [axis.get("X", pos[0]), axis.get("Y", pos[1]), axis.get("Z", pos[2])]
        centre = None
        sweep = None
        if mode in (2, 3):
            if "I" not in axis and "J" not in axis:
                refuse(ReaderFinding("D-ARC", line_no, None, "an arc without I and J"))
                continue
            if pos[0] is None or pos[1] is None:
                refuse(ReaderFinding("D-ARC", line_no, None, "an arc from an unknown X or Y"))
                continue
            cx = pos[0] + axis.get("I", Decimal(0))
            cy = pos[1] + axis.get("J", Decimal(0))
            r0 = math.hypot(float(pos[0] - cx), float(pos[1] - cy))
            r1 = math.hypot(float(end[0] - cx), float(end[1] - cy))
            if r0 == 0:
                refuse(ReaderFinding("D-ARC", line_no, None, "an arc of zero radius"))
                continue
            if abs(r0 - r1) > 0.005:
                refuse(ReaderFinding("D-ARC", line_no, None, "start and end radii differ by more than 0.005 mm"))
                continue
            gap = math.hypot(float(end[0] - pos[0]), float(end[1] - pos[1]))
            if end[0] == pos[0] and end[1] == pos[1]:
                sweep = 360.0
            elif gap < 0.01:
                refuse(ReaderFinding("D-ARC", line_no, None, "ambiguous arc: end within 0.01 mm of its start"))
                continue
            else:
                a0 = math.degrees(math.atan2(float(pos[1] - cy), float(pos[0] - cx)))
                a1 = math.degrees(math.atan2(float(end[1] - cy), float(end[0] - cx)))
                sweep = ((a1 - a0) if mode == 3 else (a0 - a1)) % 360.0
            centre = (float(cx), float(cy))
        zzero = header.get("STOCK", {}).get("zzero", "top")
        top = Decimal(0) if zzero == "top" else header.get("STOCK", {}).get("t", Decimal(0))
        if has_op and open_op is None:
            moves_xy = end[0] != pos[0] or end[1] != pos[1] or mode in (2, 3)
            if (end[2] is not None and end[2] < top) or (moves_xy and (pos[2] is None or pos[2] < top)):
                refuse(ReaderFinding("D-GI-OP", line_no, None, "motion outside every block below the stock top"))
                continue
        if end[0] is None or end[1] is None:
            if "X" in axis or "Y" in axis:
                pass
            elif end[2] is None:
                continue
        motion_seen = True
        prog.moves.append(GIMove(
            line=line_no,
            code=mode,
            start=tuple(None if v is None else float(v) for v in start),  # type: ignore[arg-type]
            end=(None if end[0] is None else float(end[0]), None if end[1] is None else float(end[1]),  # type: ignore
                 None if end[2] is None else float(end[2])),
            centre=centre,
            sweep_deg=sweep,
            op=None if open_op is None else open_op["id"],
            forced_c=None if open_op is None else open_op.get("c"),
        ))
        pos = end
    if open_op is not None:
        refuse(ReaderFinding("D-GI-OP", open_op["line"], None, "a block never closed"))
    if header_open:
        end_header(len(lines))
    prog.ops = ops
    if all(k in header for k in _REQUIRED_HEADER):
        prog.header = _model(header, ops)
        if job is not None:
            for finding in _conflicts(header, job):
                refuse(finding)
    prog.findings.sort(key=lambda f: f.line)
    return prog


def _model(header: Mapping[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any]:
    """The header as docs/prep/gi.schema.json records it."""

    def plain(values: Mapping[str, Any]) -> dict[str, Any]:
        return {k: (_dec_float(v) if isinstance(v, Decimal) else v) for k, v in values.items()}

    model: dict[str, Any] = {
        "version": 1,
        "source": plain(header["SOURCE"]),
        "units": plain(header["UNITS"]),
        "stock": plain(header["STOCK"]),
        "tower2": plain(header["TOWER2"]),
        "tool": plain(header["TOOL"]),
        "ops": [],
    }
    if "ZSHIFT" in header:
        model["zshift"] = plain(header["ZSHIFT"])
    for op in ops:
        entry = plain(op)
        entry["through"] = {"auto": "auto", "1": True, "0": False}[op["through"]]
        model["ops"].append(entry)
    return model


def _conflicts(header: Mapping[str, Any], job: Mapping[str, Any]) -> list[ReaderFinding]:
    """gi-format section 10: every value both declare must agree within 0.0005 (as decimals)."""
    out: list[ReaderFinding] = []

    def num(a: Any, b: Any) -> bool:
        return abs(Decimal(str(a)) - Decimal(str(b))) >= Decimal("0.0005")

    stock = header["STOCK"]
    panel = job["panel"]
    for key, other in (("x", "W"), ("y", "H"), ("t", "T")):
        if num(stock[key], panel[other]):
            out.append(ReaderFinding("D-GI-CONFLICT", stock["line"], None,
                                     "GI STOCK %s=%s, job.toml panel %s=%s" % (key, stock[key], other, panel[other])))
    placement = job.get("placement", {"dx": 0, "dy": 0})
    if num(placement["dx"], 0) or num(placement["dy"], 0):
        out.append(ReaderFinding("D-GI-CONFLICT", stock["line"], None, "origin t1-corner needs placement (0, 0)"))
    if stock["zzero"] != job.get("z_zero", "top"):
        out.append(ReaderFinding("D-GI-CONFLICT", stock["line"], None, "zzero"))
    if header["TOWER2"]["axis"] != panel["tower2"]:
        out.append(ReaderFinding("D-GI-CONFLICT", header["TOWER2"]["line"], None,
                                 "GI TOWER2 axis=%s, job.toml tower2=%s" % (header["TOWER2"]["axis"], panel["tower2"])))
    tool = header["TOOL"]
    for key in ("d", "flute", "stickout", "rpm"):
        if key in tool and key in job["tool"] and num(tool[key], job["tool"][key]):
            out.append(ReaderFinding("D-GI-CONFLICT", tool["line"], None, "TOOL %s" % key))
    if "kind" in tool and "kind" in job["tool"] and tool["kind"] != job["tool"]["kind"]:
        out.append(ReaderFinding("D-GI-CONFLICT", tool["line"], None, "TOOL kind"))
    return out


# ---------------------------------------------------------------------------
# Goliath dialect (docs/protocol/05), verify-mode cases only
# ---------------------------------------------------------------------------

_DIALECT_WORD = re.compile(r"([A-Z])(%s)" % _NUMBER)


@dataclass(frozen=True)
class DialectMove:
    """One line of a dialect program that moves or changes state."""

    line: int
    code: int | None  # 0, 1, 2, 3 or None for a bare Z line under the modal code
    start: tuple[float | None, float | None, float | None]
    end: tuple[float | None, float | None, float | None]
    c: int | None
    feed: int | None
    spindle_on: bool
    centre: tuple[float, float] | None


@dataclass
class DialectProgram:
    lines: list[str]
    moves: list[DialectMove]
    problems: list[str]
    words: set[str]


def read_dialect(data: bytes) -> DialectProgram:
    """Read a Goliath-dialect program in the vendor's cleaned form (ASCII, CRLF, ending in an empty line).

    The words allowed are the emitted vocabulary of safety-model 11.5
    (G00 G01 G02 G03 X Y Z I J C F S M10 M05); anything else is a problem.
    """
    problems: list[str] = []
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return DialectProgram([], [], ["not ASCII"], set())
    if ";" in text:
        problems.append("a comment: the cleaned form has none")
    if re.search(r"(?<!\r)\n|\r(?!\n)", text):
        problems.append("a line ending other than CRLF")
    if not text.endswith("\r\n\r\n"):
        problems.append("does not end in an empty line (CRLF CRLF)")
    lines = split_lines(text)
    moves: list[DialectMove] = []
    words_seen: set[str] = set()
    mode: int | None = None
    pos: list[float | None] = [None, None, None]
    c: int | None = None
    feed: int | None = None
    spindle = False
    for line_no, line in enumerate(lines, start=1):
        if line.strip() == "":
            continue
        tokens = line.split(" ")
        if "".join(tokens) == "" or any(not _DIALECT_WORD.fullmatch(t) for t in tokens if t):
            problems.append("line %d: not words" % line_no)
            continue
        words = [(_DIALECT_WORD.fullmatch(t).group(1), _DIALECT_WORD.fullmatch(t).group(2))  # type: ignore[union-attr]
                 for t in tokens if t]
        values: dict[str, str] = {}
        for letter, number in words:
            key = letter + number if letter in "GM" else letter
            words_seen.add(key)
            if letter in "GM" and key not in ("G00", "G01", "G02", "G03", "M10", "M05"):
                problems.append("line %d: %s outside the emitted vocabulary" % (line_no, key))
            if letter not in "GMXYZIJCFS":
                problems.append("line %d: letter %s outside the emitted vocabulary" % (line_no, letter))
            values[letter] = number
        if "M" in values:
            spindle = values["M"] == "10"
        if "G" in values:
            mode = int(values["G"])
        if "C" in values:
            c = int(values["C"])
        if "F" in values:
            feed = int(values["F"])
        if not any(k in values for k in "XYZ"):
            continue
        start = tuple(pos)
        end = [float(values["X"]) if "X" in values else pos[0], float(values["Y"]) if "Y" in values else pos[1],
               float(values["Z"]) if "Z" in values else pos[2]]
        centre = None
        if mode in (2, 3) and start[0] is not None and start[1] is not None:
            centre = (start[0] + float(values.get("I", "0")), start[1] + float(values.get("J", "0")))
        moves.append(DialectMove(line_no, mode if "G" in values else None if mode is None else mode,
                                 start, tuple(end), c, feed, spindle, centre))  # type: ignore[arg-type]
        pos = end
    return DialectProgram(lines, moves, problems, words_seen)
