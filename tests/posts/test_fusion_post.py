# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Tests of the Goliath Fusion post (posts/fusion/goliath.cps, unit PP2).

The post writes GI, the Goliath-intermediate G-code of docs/prep/gi-format.md.
Fusion cannot run here, so the tests run goliath.cps under node in
``fusion_harness.js``, a small stand-in for Fusion's post engine that
implements only the documented post processor API the post uses. Synthetic
jobs go through it and the output is checked line by line against the GI
rules: the header, units, OP blocks, number formats, arcs, the single tool
and the end of the program, and every refusal must fire with its message and
write no program.

Tests that run the post need node; without it they skip and say so. The
tests of the post's source, its README and its samples need nothing.

Real Fusion output is the real test. The owner runs the post in Fusion by the
checklist in posts/fusion/README.md; the files that come back are committed
under posts/fusion/samples/ and checked here: by the GI writing rules below,
and by goliath-prep's reader as soon as it exists
(test_committed_samples_pass_goliath_prep_check).

Python 3.9 compatible: the repository-level suite runs on 3.9.
"""
from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import math
import random
import re
import shutil
import subprocess
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

REPO = Path(__file__).resolve().parents[2]
POST_DIR = REPO / "posts" / "fusion"
POST = POST_DIR / "goliath.cps"
README = POST_DIR / "README.md"
SAMPLES_DIR = POST_DIR / "samples"
HARNESS = Path(__file__).resolve().parent / "fusion_harness.js"
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(
    NODE is None,
    reason="node is not installed: these tests run posts/fusion/goliath.cps in tests/posts/fusion_harness.js "
    "under node (Fusion itself cannot run here). Install Node.js to run them",
)

# --------------------------------------------------------------------------
# Building synthetic jobs for the harness
# --------------------------------------------------------------------------

STOCK = [
    ("stock-lower-x", 0),
    ("stock-lower-y", 0),
    ("stock-lower-z", -18),
    ("stock-upper-x", 2440),
    ("stock-upper-y", 1220),
    ("stock-upper-z", 0),
]
CAM = "Fusion CAM 2.0.21000"


def tool(**overrides: Any) -> Dict[str, Any]:
    fields: Dict[str, Any] = dict(
        number=1, diameter=6.0, fluteLength=22.0, spindleRPM=18000, description="6mm 2F upcut"
    )
    fields.update(overrides)
    return fields


def section(records: List[Any], **overrides: Any) -> Dict[str, Any]:
    fields: Dict[str, Any] = dict(
        strategy="contour2d",
        parameters={"operation-comment": "Part A outline"},
        tool=tool(),
        initial=[100, 200, 15],
        records=records,
    )
    fields.update(overrides)
    return fields


def job(*sections: Dict[str, Any], **overrides: Any) -> Dict[str, Any]:
    fields: Dict[str, Any] = dict(
        properties={"tower2Axis": "Y"},
        globals=[("generated-by", CAM)] + STOCK,
        sections=list(sections),
    )
    fields.update(overrides)
    return fields


def globals_with(**values: Any) -> List[Tuple[str, Any]]:
    """The standard global parameters with some replaced (keys with '_' for '-'); None drops one."""
    wanted = {k.replace("_", "-"): v for k, v in values.items()}
    out = []
    for name, value in [("generated-by", CAM)] + STOCK:
        if name in wanted:
            if wanted[name] is not None:
                out.append((name, wanted[name]))
        else:
            out.append((name, value))
    return out


PROFILE = [
    ["rapid", 100, 200, 5],
    ["linear", 100, 200, -18.5, 200],
    ["linear", 300, 200, -18.5, 800],
    ["circular", False, 300, 250, -18.5, 350, 250, -18.5, 800],
    ["linear", 350, 400, -18.5, 800],
    ["rapid", 350, 400, 15],
]
POCKET = [
    ["rapid", 600, 400, 3],
    ["linear", 600, 400, -4, 200],
    ["linear", 700, 400, -4, 800],
    ["linear", 700, 450, -4, 800],
    ["linear", 600, 450, -4, 800],
    ["linear", 600, 400, -4, 800],
    ["rapid", 600, 400, 15],
]


def pocket_section(**overrides: Any) -> Dict[str, Any]:
    fields: Dict[str, Any] = dict(
        strategy="pocket2d", parameters={"operation-comment": "Hinge pocket"}, initial=[600, 400, 15]
    )
    fields.update(overrides)
    return section(POCKET, **fields)


def standard_job(**overrides: Any) -> Dict[str, Any]:
    return job(section(PROFILE), pocket_section(), **overrides)


# --------------------------------------------------------------------------
# Running the harness
# --------------------------------------------------------------------------


def harness(request: Dict[str, Any]) -> Dict[str, Any]:
    assert NODE is not None
    result = subprocess.run(
        [NODE, str(HARNESS), str(POST)],
        input=json.dumps(request).encode("utf-8"),
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    reply = json.loads(result.stdout.decode("utf-8"))
    if "crash" in reply:
        pytest.fail("the post (or the harness) crashed instead of posting or refusing:\n" + reply["crash"])
    return reply


def post(the_job: Dict[str, Any]) -> str:
    reply = harness({"mode": "post", "job": the_job})
    assert reply["ok"], "the post refused: %s" % reply.get("error")
    text = reply["output"]
    assert gi_problems(text) == [], text
    return text


def refused(the_job: Dict[str, Any], match: str) -> str:
    reply = harness({"mode": "post", "job": the_job})
    assert not reply["ok"], "the post did not refuse; it wrote:\n" + reply["output"]
    message = reply["error"]
    assert message.startswith("Goliath post: "), message
    assert re.search(match, message), message
    assert reply["output"] == "", "a refused job must write no program"
    return message


def call(function: str, *args: Any) -> Any:
    reply = harness({"mode": "call", "function": function, "args": list(args)})
    assert reply["ok"], reply
    return reply["value"]


def call_refused(function: str, *args: Any) -> str:
    reply = harness({"mode": "call", "function": function, "args": list(args)})
    assert not reply["ok"], reply
    return reply["error"]


def body(text: str, op: str = "op1") -> List[str]:
    """The lines inside one OP block (the spindle start included)."""
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith(";GOLIATH OP id=%s " % op))
    end = lines.index(";GOLIATH OPEND id=%s" % op)
    return lines[start + 1 : end]


# --------------------------------------------------------------------------
# The GI writing rules (docs/prep/gi-format.md), as a lint for the post's output
# --------------------------------------------------------------------------

_ITEM = re.compile(r' (?:([a-z][a-z0-9_]*)=)?("(?:[^"\\\x00-\x1f\x7f]|\\["\\])*"|[A-Za-z0-9_.+\-]+)')
_WORD = re.compile(r"([A-Z])([+-]?\d+(?:\.\d+)?)\Z")
_MM_VALUE = re.compile(r"-?\d+\.\d{3}\Z")
_LENGTH = re.compile(r"\d+\.\d{3}\Z")
_HEADER = ("SOURCE", "UNITS", "STOCK", "TOWER2", "TOOL", "ZSHIFT")
_REQUIRED = ("SOURCE", "UNITS", "STOCK", "TOWER2", "TOOL")
_OP_KINDS = ("profile", "pocket", "drill", "engrave", "adaptive", "other")
_G_CODES = {0, 1, 2, 3, 4, 17, 21, 90}
_M_CODES = {3, 5, 30}


def parse_annotation(line: str) -> Tuple[str, Dict[str, str], List[str]]:
    """(keyword, keyed values, positional values) of a well-formed annotation line; ValueError otherwise."""
    match = re.match(r";GOLIATH ([A-Z][A-Z0-9]*)", line)
    if not match:
        raise ValueError("not an annotation")
    keyed: Dict[str, str] = {}
    positional: List[str] = []
    rest, pos = line[match.end() :], 0
    while pos < len(rest):
        item = _ITEM.match(rest, pos)
        if not item:
            raise ValueError("malformed at %r" % rest[pos:])
        key, value = item.groups()
        if key is None:
            positional.append(value)
        elif key in keyed:
            raise ValueError("key %s twice" % key)
        else:
            keyed[key] = value
        pos = item.end()
    return match.group(1), keyed, positional


Point = Tuple[Decimal, Decimal]


def _sweep_deg(code: str, start: Point, centre: Point, end: Point) -> float:
    a0 = math.atan2(float(start[1] - centre[1]), float(start[0] - centre[0]))
    a1 = math.atan2(float(end[1] - centre[1]), float(end[0] - centre[0]))
    ccw = math.degrees(a1 - a0) % 360.0
    return ccw if code == "G3" else (360.0 - ccw) % 360.0


def gi_problems(text: str) -> List[str]:
    """Problems of a post output against the GI rules and the post's own conventions.

    A lint, not a reader: goliath-prep's reader is the authority
    (test_committed_samples_pass_goliath_prep_check). It holds the output to the
    smaller vocabulary the post writes.
    """
    problems: List[str] = []
    if not text.endswith("\n") or "\r" in text:
        problems.append("line endings must be LF with a final newline")
    if not text.isascii():
        problems.append("the post writes ASCII only")
    lines = text[:-1].split("\n")
    if lines[0] != ";GOLIATH-GI 1":
        problems.append("line 1 must be the version line")
    header: Dict[str, Dict[str, str]] = {}
    positional: Dict[str, List[str]] = {}
    header_open = True
    open_op: Optional[str] = None
    ids = set()
    op_feed_stated = False
    mode: Optional[int] = None
    pos: Dict[str, Optional[Decimal]] = {"X": None, "Y": None, "Z": None}
    tool_n: Optional[str] = None

    def close_header(where: str) -> Optional[str]:
        missing = [k for k in _REQUIRED if k not in header]
        if missing:
            problems.append("%s: the header lacks %s" % (where, ", ".join(missing)))
        return header.get("TOOL", {}).get("n")

    for number, line in enumerate(lines[1:], start=2):
        where = "line %d (%s)" % (number, line)
        if line.startswith(";GOLIATH"):
            try:
                keyword, keyed, values = parse_annotation(line)
            except ValueError as exc:
                problems.append("%s: %s" % (where, exc))
                continue
            if keyword in _HEADER:
                if not header_open or keyword in header:
                    problems.append("%s: header line repeated or after the header" % where)
                header[keyword] = keyed
                positional[keyword] = values
                continue
            if keyword not in ("OP", "OPEND"):
                problems.append("%s: unknown keyword" % where)
                continue
            if header_open:
                header_open = False
                tool_n = close_header(where)
            if keyword == "OP":
                if open_op is not None or keyed.get("id") in ids:
                    problems.append("%s: nested or repeated OP" % where)
                open_op = keyed.get("id")
                ids.add(open_op)
                op_feed_stated = False
                if keyed.get("kind") not in _OP_KINDS or keyed.get("through") != "auto":
                    problems.append("%s: kind or through" % where)
                if keyed.get("tool") != tool_n:
                    problems.append("%s: OP tool must be TOOL n" % where)
                if set(keyed) - {"id", "kind", "through", "tool", "label"} or values:
                    problems.append("%s: unexpected keys" % where)
            else:
                if open_op is None or keyed.get("id") != open_op or set(keyed) != {"id"}:
                    problems.append("%s: OPEND does not close the open OP" % where)
                open_op = None
            continue
        if line.startswith(";"):
            if not (line.startswith("; Note: ") or line.startswith("; Fusion: ")):
                problems.append("%s: the post's comments start with '; Note: ' or '; Fusion: '" % where)
            continue
        if header_open:
            header_open = False
            tool_n = close_header(where)
        words: Dict[str, str] = {}
        codes: List[Tuple[str, int]] = []
        for word in line.split(" "):
            match = _WORD.match(word)
            if not match:
                problems.append("%s: %r is not a word" % (where, word))
                continue
            letter, value = match.groups()
            if letter in "GM":
                codes.append((letter, int(value)))
                if letter == "M" and any(c[0] == "M" for c in codes[:-1]):
                    problems.append("%s: two M words" % where)
                continue
            if letter in words:
                problems.append("%s: %s twice" % (where, letter))
            words[letter] = value
        gs = [n for letter, n in codes if letter == "G"]
        ms = [n for letter, n in codes if letter == "M"]
        if any(n not in _G_CODES for n in gs) or any(n not in _M_CODES for n in ms):
            problems.append("%s: a G or M code the post does not write" % where)
        for letter, value in words.items():
            if letter in "XYZIJ":
                if not _MM_VALUE.match(value) or value == "-0.000":
                    problems.append("%s: %s needs three decimals" % (where, letter))
                elif abs(Decimal(value)) > 10000:
                    problems.append("%s: %s out of range" % (where, letter))
            elif letter in "FS":
                if not re.fullmatch(r"[1-9]\d*", value):
                    problems.append("%s: %s needs a whole number above 0" % (where, letter))
            elif letter == "P":
                if not _LENGTH.match(value) or 4 not in gs:
                    problems.append("%s: P belongs to G4, three decimals" % where)
            else:
                problems.append("%s: letter %s" % (where, letter))
        if 30 in ms and (len(codes) != 1 or words):
            problems.append("%s: M30 stands alone" % where)
        motions = [n for n in gs if n in (0, 1, 2, 3)]
        if len(motions) > 1:
            problems.append("%s: two motion codes" % where)
        if set(words) & set("XYZIJ") and not motions:
            problems.append("%s: the post writes the motion code on every move" % where)
        if not motions:
            continue
        mode = motions[0]
        if open_op is None:
            problems.append("%s: motion outside every OP block" % where)
        if mode in (1, 2, 3):
            if "F" in words:
                op_feed_stated = True
            elif not op_feed_stated:
                problems.append("%s: the first feed move of an operation states F" % where)
        elif "F" in words:
            problems.append("%s: F on a rapid" % where)
        if mode in (0, 1) and set(words) & set("IJ"):
            problems.append("%s: I or J on a linear move" % where)
        end = {a: Decimal(words[a]) if a in words else pos[a] for a in "XYZ"}
        if mode in (2, 3):
            if not ("X" in words and "Y" in words and "I" in words and "J" in words):
                problems.append("%s: arcs carry X, Y, I and J" % where)
            elif pos["X"] is None or pos["Y"] is None:
                problems.append("%s: an arc from an unknown position" % where)
            else:
                start = (pos["X"], pos["Y"])
                centre = (start[0] + Decimal(words["I"]), start[1] + Decimal(words["J"]))
                stop = (end["X"], end["Y"])
                r0 = math.hypot(float(start[0] - centre[0]), float(start[1] - centre[1]))
                r1 = math.hypot(float(stop[0] - centre[0]), float(stop[1] - centre[1]))
                chord = math.hypot(float(stop[0] - start[0]), float(stop[1] - start[1]))
                if r0 == 0 or abs(r0 - r1) > 0.005:
                    problems.append("%s: radii differ by more than 0.005 mm" % where)
                if chord < 0.01:
                    problems.append("%s: end within 0.01 mm of the start (full circle or ambiguous)" % where)
                elif _sweep_deg("G%d" % mode, start, centre, stop) > 180 + 1e-6:
                    problems.append("%s: more than 180 degrees" % where)
        pos = end
    if open_op is not None:
        problems.append("OP %s never closed" % open_op)
    if lines[-2:] != ["M5", "M30"]:
        problems.append("the program ends with M5, M30")
    for keyword, keys in (("STOCK", ("x", "y", "t")), ("TOOL", ("d", "flute"))):
        for key in keys:
            value = header.get(keyword, {}).get(key, "")
            if not _LENGTH.match(value) or Decimal(value) <= 0:
                problems.append("%s %s=%r must be a length above 0 with three decimals" % (keyword, key, value))
    stock = header.get("STOCK", {})
    if stock.get("origin") != "t1-corner" or stock.get("zzero") != "top":
        problems.append("STOCK origin=t1-corner zzero=top")
    if header.get("TOWER2", {}).get("axis") not in ("X", "Y"):
        problems.append("TOWER2 axis")
    if positional.get("UNITS") != ["mm"]:
        problems.append("UNITS mm")
    tool_values = header.get("TOOL", {})
    if not re.fullmatch(r"\d+", tool_values.get("n", "")):
        problems.append("TOOL n")
    if "kind" in tool_values and tool_values["kind"] not in ("flat", "ball", "bull", "vbit", "other"):
        problems.append("TOOL kind")
    if "rpm" in tool_values and not re.fullmatch(r"[1-9]\d*", tool_values["rpm"]):
        problems.append("TOOL rpm")
    if "ZSHIFT" in positional and positional["ZSHIFT"] != ["0.000"]:
        problems.append("the Fusion post applies no Z shift")
    source = header.get("SOURCE", {})
    if not source.get("cam") or source.get("post") != '"goliath.cps 0.1.0"':
        problems.append("SOURCE cam and post")
    return problems


GOOD_GI = """\
;GOLIATH-GI 1
;GOLIATH SOURCE cam="Fusion CAM 2.0.21000" post="goliath.cps 0.1.0"
;GOLIATH UNITS mm
;GOLIATH STOCK x=2440.000 y=1220.000 t=18.000 origin=t1-corner zzero=top
;GOLIATH TOWER2 axis=Y
;GOLIATH TOOL n=1 d=6.000 flute=22.000 kind=flat rpm=18000 name="6mm 2F upcut"
;GOLIATH ZSHIFT 0.000
G17 G21 G90
;GOLIATH OP id=op1 kind=profile through=auto tool=1 label="Part A outline"
M3 S18000
G0 Z15.000
G0 X100.000 Y200.000
G1 Z-18.500 F200
G3 X150.000 Y250.000 I0.000 J50.000
G0 Z15.000
;GOLIATH OPEND id=op1
M5
M30
"""


def test_writing_rule_lint_accepts_good_gi_and_catches_what_it_should() -> None:
    assert gi_problems(GOOD_GI) == []
    bad = [
        GOOD_GI.replace("G0 Z15.000\nG0 X", "G0 C90 Z15.000\nG0 X", 1),  # robot dialect
        GOOD_GI.replace("M3 S18000", "M10 S18000"),
        GOOD_GI.replace("M3 S18000", "M3 S18000\n; Spindle warm-up"),  # a ;S marker
        GOOD_GI.replace("\n", "\r\n"),
        GOOD_GI.replace("G0 Z15.000\nG0 X", "G0 Z15.0\nG0 X", 1),
        GOOD_GI.replace("F200", "F200.5"),
        GOOD_GI.replace("I0.000 J50.000", "I0.000 J50.100"),  # not one circle
        GOOD_GI.replace("G3 X150.000 Y250.000", "G3 X100.000 Y200.000"),  # full circle
        GOOD_GI.replace("G3 X150.000 Y250.000", "G2 X150.000 Y250.000"),  # 270 degrees
        GOOD_GI.replace(";GOLIATH OPEND id=op1\n", ""),
        GOOD_GI.replace("M5\nM30\n", "M5\nM30\nG0 Z20.000\n"),
        GOOD_GI.replace("origin=t1-corner", "origin=centre"),
        GOOD_GI.replace("G1 Z-18.500 F200", "G1 Z-18.500"),
        GOOD_GI.replace("G0 X100.000 Y200.000", "G0 X100.000 Y200.000 F5000"),
        GOOD_GI.replace("tool=1 label", "tool=2 label"),
        GOOD_GI.replace(";GOLIATH TOWER2 axis=Y\n", ""),
        GOOD_GI.replace('label="Part A outline"', 'label="Part A outline'),
    ]
    for text in bad:
        assert gi_problems(text), text


# --------------------------------------------------------------------------
# The whole output, line by line
# --------------------------------------------------------------------------

STANDARD_OUTPUT = """\
;GOLIATH-GI 1
;GOLIATH SOURCE cam="Fusion CAM 2.0.21000" post="goliath.cps 0.1.0"
;GOLIATH UNITS mm
;GOLIATH STOCK x=2440.000 y=1220.000 t=18.000 origin=t1-corner zzero=top
;GOLIATH TOWER2 axis=Y
;GOLIATH TOOL n=1 d=6.000 flute=22.000 kind=flat rpm=18000 name="6mm 2F upcut"
;GOLIATH ZSHIFT 0.000
; Note: stock box from Fusion, in the setup's WCS: X 0.000 to 2440.000, Y 0.000 to 1220.000, Z -18.000 to 0.000
G17 G21 G90
;GOLIATH OP id=op1 kind=profile through=auto tool=1 label="Part A outline"
M3 S18000
G0 Z15.000
G0 X100.000 Y200.000
G0 Z5.000
G1 Z-18.500 F200
G1 X300.000 F800
G3 X350.000 Y250.000 I0.000 J50.000
G1 Y400.000
G0 Z15.000
;GOLIATH OPEND id=op1
;GOLIATH OP id=op2 kind=pocket through=auto tool=1 label="Hinge pocket"
M3 S18000
G0 X600.000
G0 Z3.000
G1 Z-4.000 F200
G1 X700.000 F800
G1 Y450.000
G1 X600.000
G1 Y400.000
G0 Z15.000
;GOLIATH OPEND id=op2
M5
M30
"""


@needs_node
def test_standard_job_line_by_line() -> None:
    text = post(standard_job())
    assert text.split("\n") == STANDARD_OUTPUT.split("\n")


@needs_node
def test_header_tower2_x_and_cam_fallback() -> None:
    the_job = standard_job(properties={"tower2Axis": "X"}, globals=STOCK)
    text = post(the_job)
    assert ";GOLIATH TOWER2 axis=X\n" in text
    assert ';GOLIATH SOURCE cam="Autodesk Fusion (version not reported)" post="goliath.cps 0.1.0"\n' in text


@needs_node
def test_stock_size_is_upper_minus_lower_and_writes_three_decimals() -> None:
    the_job = standard_job(globals=globals_with(stock_lower_x=0.0003, stock_upper_x=1220.0004, stock_upper_y=610.25,
                                                 stock_lower_z=-17.6, stock_upper_z=-0.0004))
    text = post(the_job)
    assert ";GOLIATH STOCK x=1220.000 y=610.250 t=17.600 origin=t1-corner zzero=top\n" in text


# --------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------


def _python_mm(value: float) -> str:
    """The FreeCAD post's rule (posts/freecad/gi_format.py): the shortest decimal form, half to even."""
    rounded = Decimal(repr(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_EVEN)
    if rounded.is_zero():
        rounded = rounded.copy_abs()
    return format(rounded, "f")


@needs_node
def test_millimetres_round_half_to_even_on_the_shortest_decimal_form() -> None:
    cases = {
        1.0005: "1.000",
        1.0015: "1.002",
        -1.2345: "-1.234",
        -1.2355: "-1.236",
        -0.0004: "0.000",
        -0.0005: "0.000",
        -0.0015: "-0.002",
        0.1 + 0.2: "0.300",
        2.5e-7: "0.000",
        1e-3: "0.001",
        123456.789: "123456.789",
        2440: "2440.000",
        -18: "-18.000",
    }
    for value, written in cases.items():
        assert call("giMm", value) == written, value
        assert _python_mm(value) == written, value


@needs_node
def test_millimetres_agree_with_the_freecad_post_rule_on_random_values() -> None:
    rng = random.Random(20261104)
    values = [rng.uniform(-10000, 10000) for _ in range(400)]
    values += [round(rng.uniform(-3000, 3000), 4) for _ in range(400)]  # many exact half-way cases
    values += [rng.uniform(-1, 1) * 10 ** rng.randint(-9, 3) for _ in range(200)]
    reply = harness({"mode": "map", "function": "giMm", "args_list": [[v] for v in values]})
    assert reply["ok"], reply
    assert reply["value"] == [_python_mm(v) for v in values]


@needs_node
def test_whole_numbers_round_half_to_even() -> None:
    reply = harness({"mode": "map", "function": "giWhole", "args_list": [[199.5], [200.5], [0.4], [18000.49], [-2.5]]})
    assert reply["value"] == [200, 200, 0, 18000, -2]


@needs_node
def test_numbers_that_are_not_numbers_are_refused() -> None:
    assert "not a number" in call_refused("giMm", None)
    assert "far outside" in call_refused("giMm", 1e21)


@needs_node
def test_text_is_ascii_and_quoted_values_are_escaped() -> None:
    assert call("giQuoted", 'Part "A" \\ B') == '"Part \\"A\\" \\\\ B"'
    assert call("giQuoted", "Au\u00dfen\tkontur\n") == '"Au?en kontur"'
    assert call("giComment", "Fusion", "  Spindle   warm-up ") == "; Fusion: Spindle warm-up"


# --------------------------------------------------------------------------
# Motion
# --------------------------------------------------------------------------


@needs_node
def test_only_changed_axes_are_written_and_zero_length_moves_write_nothing() -> None:
    text = post(job(section([
        ["rapid", 100, 200, 15],  # Fusion's own move to the initial position: already there
        ["rapid", 100, 200, 5],
        ["linear", 100, 200, -1, 200],
        ["linear", 100.0002, 200.0004, -1, 200],  # moves less than the file's resolution
        ["linear", 120, 200, -1, 200],
        ["rapid", 120, 200, 15],
    ])))
    assert body(text) == [
        "M3 S18000",
        "G0 Z15.000",
        "G0 X100.000 Y200.000",
        "G0 Z5.000",
        "G1 Z-1.000 F200",
        "G1 X120.000",
        "G0 Z15.000",
    ]


@needs_node
def test_feeds_are_whole_mm_per_min_written_when_they_change_and_once_per_operation() -> None:
    records = [
        ["rapid", 100, 200, 5],
        ["linear", 100, 200, -1, 199.5],
        ["linear", 110, 200, -1, 800.4],
        ["linear", 120, 200, -1, 799.6],
        ["rapid", 120, 200, 15],
    ]
    text = post(job(section(records), section(records, initial=[100, 200, 15])))
    expected_moves = ["G1 Z-1.000 F200", "G1 X110.000 F800", "G1 X120.000"]
    assert [line for line in body(text, "op1") if line.startswith("G1")] == expected_moves
    # The second operation states its feed again on its first feed move.
    assert [line for line in body(text, "op2") if line.startswith("G1")][0] == "G1 Z-1.000 F200"
    assert not [line for line in text.split("\n") if line.startswith("G0") and " F" in line]


@needs_node
def test_each_operation_starts_at_its_initial_position_up_first_or_across_first() -> None:
    first = section([["rapid", 100, 200, 5], ["linear", 100, 200, -1, 200], ["rapid", 100, 200, 10]])
    higher = section([["rapid", 500, 500, 5], ["linear", 500, 500, -1, 200], ["rapid", 500, 500, 10]],
                     initial=[500, 500, 25])
    lower = section([["rapid", 800, 800, 1], ["linear", 800, 800, -1, 200], ["rapid", 800, 800, 10]],
                    initial=[800, 800, 2])
    feed_first = section([["linear", 900, 900, -1, 300], ["rapid", 900, 900, 10]], initial=[900, 900, 2])
    text = post(job(first, higher, lower, feed_first))
    # The program's first move raises Z before moving in X or Y (gi-format.md 14, point 3).
    assert body(text, "op1")[1:3] == ["G0 Z15.000", "G0 X100.000 Y200.000"]
    # Higher than where the tool is: up first, then across.
    assert body(text, "op2")[1:3] == ["G0 Z25.000", "G0 X500.000 Y500.000"]
    # Lower: across at the current height, then down; never diagonally down.
    assert body(text, "op3")[1:4] == ["G0 X800.000 Y800.000", "G0 Z2.000", "G0 Z1.000"]
    # An operation whose first move is a feed move is positioned the same way, at that feed.
    assert body(text, "op4")[1:4] == ["G1 X900.000 Y900.000 F300", "G1 Z2.000", "G1 Z-1.000"]


@needs_node
def test_personal_use_rapids_arrive_as_feed_moves_and_stay_g1() -> None:
    # Fusion for Personal Use reportedly sends rapids as feed moves. The post writes
    # what Fusion gives: no G0 at all, the positioning moves included.
    text = post(standard_job(rapids_as_feed=1000))
    moves = [line for line in text.split("\n") if re.match(r"G[0-3] ", line)]
    assert not [line for line in moves if line.startswith("G0")]
    assert body(text, "op1")[:5] == [
        "M3 S18000",
        "G1 Z15.000 F1000",
        "G1 X100.000 Y200.000",
        "G1 Z5.000",
        "G1 Z-18.500 F200",
    ]


def test_post_never_looks_at_the_movement_type() -> None:
    # It cannot turn Fusion's feed moves back into G0 if it never asks which ones were rapids.
    source = POST.read_text(encoding="utf-8")
    assert "movement" not in source.lower().replace("movements", "")
    assert "MOVEMENT_" not in source
    assert "highFeed" not in source


# --------------------------------------------------------------------------
# Arcs
# --------------------------------------------------------------------------


def test_post_asks_the_engine_for_xy_arcs_of_at_most_180_degrees() -> None:
    source = POST.read_text(encoding="utf-8")
    for setting in (
        "allowedCircularPlanes = 1 << PLANE_XY;",
        "maximumCircularSweep = toRad(180);",
        "allowHelicalMoves = false;",
        "allowSpiralMoves = false;",
        "minimumChordLength = spatial(0.02, MM);",
    ):
        assert setting in source, setting


@needs_node
def test_kernel_settings_as_the_engine_reads_them() -> None:
    names = [
        "allowedCircularPlanes", "allowHelicalMoves", "allowSpiralMoves", "maximumCircularSweep",
        "minimumChordLength", "maximumCircularRadius", "tolerance", "extension", "capabilities", "unit",
        "properties",
    ]
    reply = harness({"mode": "inspect", "names": names, "job": {"unit": "IN"}})
    value = reply["value"]
    assert value["allowedCircularPlanes"] == 1  # 1 << PLANE_XY in the harness
    assert value["allowHelicalMoves"] is False and value["allowSpiralMoves"] is False
    assert value["maximumCircularSweep"] == pytest.approx(math.pi)
    assert value["minimumChordLength"] == pytest.approx(0.02)
    assert value["maximumCircularRadius"] == pytest.approx(5000)
    assert value["extension"] == "gi"
    assert value["unit"] == 0, "the post must not change the output unit (it refuses inches instead)"
    tower2 = value["properties"]["tower2Axis"]
    assert tower2["value"] == "unset" and [v["id"] for v in tower2["values"]] == ["unset", "Y", "X"]


@needs_node
def test_arc_centre_is_rebased_on_the_written_start() -> None:
    # Start X 10.0004 is written 10.000 and the centre X 13.0006 13.001: I is 3.001,
    # so the centre the reader computes is Fusion's centre rounded once.
    text = post(job(section([
        ["rapid", 10.0004, 0, 5],
        ["linear", 10.0004, 0, -1, 200],
        ["circular", False, 13.0006, 0, -1, 16.0008, 0, -1, 800],
        ["rapid", 16.0008, 0, 15],
    ], initial=[10.0004, 0, 15])))
    assert "G3 X16.001 Y0.000 I3.001 J0.000 F800" in body(text)


@needs_node
def test_arcs_the_engine_linearizes_reach_the_post_as_lines() -> None:
    # A full circle (over 180 degrees), a helix, an arc in the ZX plane and a sliver
    # arc: the harness linearizes each, as the post's settings ask the engine to.
    text = post(job(section([
        ["rapid", 100, 200, 5],
        ["linear", 100, 200, -1, 200],
        ["circular", True, 100, 210, -1, 100, 200, -1, 800],
        ["circular", False, 100, 210, -3, 100, 220, -3, 800],
        ["circular", True, 100, 220, -2, 100, 220, -1, 800, {"plane": "ZX"}],
        ["circular", False, 100, 225, -1, 100.01, 220.00001, -1, 800],
        ["rapid", 100.01, 220.00001, 15],
    ])))
    lines = body(text)
    assert not [line for line in lines if line.startswith(("G2", "G3"))]
    assert len([line for line in lines if line.startswith("G1")]) > 20


@needs_node
@pytest.mark.parametrize(
    "options, match",
    [
        ({"plane": "ZX", "bypass_engine": True}, "outside the XY plane"),
        ({"helical": True, "bypass_engine": True}, "helical arc"),
        ({"spiral": True, "bypass_engine": True}, "spiral arc"),
        ({"sweep_deg": 200, "bypass_engine": True}, "more than 180 degrees"),
        ({"full": True, "bypass_engine": True}, "more than 180 degrees"),
    ],
)
def test_arcs_that_should_never_reach_the_post_are_refused(options: Dict[str, Any], match: str) -> None:
    records = [
        ["rapid", 100, 200, 5],
        ["linear", 100, 200, -1, 200],
        ["circular", False, 100, 210, -1, 110, 210, -1, 800, options],
    ]
    refused(job(section(records)), match)


@needs_node
def test_sliver_and_off_circle_arcs_are_refused_at_the_files_resolution() -> None:
    sliver = [
        ["rapid", 100, 200, 5],
        ["linear", 100, 200, -1, 200],
        ["circular", False, 100, 300, -1, 100.005, 200.0000001, -1, 800, {"bypass_engine": True, "spiral": False}],
    ]
    refused(job(section(sliver)), r"less than 0\.01 mm from its start")
    off_circle = [
        ["rapid", 100, 200, 5],
        ["linear", 100, 200, -1, 200],
        ["circular", False, 100, 210, -1, 110.01, 210, -1, 800, {"bypass_engine": True, "spiral": False}],
    ]
    refused(job(section(off_circle)), "not on one circle")


# --------------------------------------------------------------------------
# Operations, tool and the rest of the vocabulary
# --------------------------------------------------------------------------


@needs_node
@pytest.mark.parametrize(
    "strategy, kind",
    [
        ("contour2d", "profile"),
        ("contour2D", "profile"),
        ("pocket2d", "pocket"),
        ("adaptive2d", "adaptive"),
        ("adaptive", "adaptive"),
        ("drill", "drill"),
        ("engrave", "engrave"),
        ("trace", "engrave"),
        ("face", "other"),
        ("", "other"),
    ],
)
def test_operation_kind_from_the_fusion_strategy(strategy: str, kind: str) -> None:
    text = post(job(section(PROFILE, strategy=strategy)))
    assert ";GOLIATH OP id=op1 kind=%s through=auto tool=1 " % kind in text
    note = '; Note: Fusion strategy "%s" of operation "Part A outline" (operation 1 in the NC program) has no ' \
           "GI operation kind; written as kind=other" % strategy
    assert (note in text.split("\n")) == (kind == "other")


@needs_node
def test_labels_are_quoted_ascii_and_left_out_when_empty() -> None:
    text = post(job(section(PROFILE, parameters={"operation-comment": 'Au\u00dfen "A" \\ 1'}),
                    pocket_section(parameters={})))
    assert ';GOLIATH OP id=op1 kind=profile through=auto tool=1 label="Au?en \\"A\\" \\\\ 1"' in text
    assert ";GOLIATH OP id=op2 kind=pocket through=auto tool=1\n" in text


@needs_node
def test_tool_kind_rpm_and_name_only_when_every_operation_agrees() -> None:
    text = post(job(section(PROFILE, tool=tool(type="TOOL_MILLING_END_BALL"))))
    assert ";GOLIATH TOOL n=1 d=6.000 flute=22.000 kind=ball rpm=18000 name=\"6mm 2F upcut\"\n" in text
    text = post(job(section(PROFILE, tool=tool(type="TOOL_MILLING_CHAMFER", description=""))))
    assert ";GOLIATH TOOL n=1 d=6.000 flute=22.000 rpm=18000\n" in text
    assert "; Note: Fusion's tool type of T1 is not one GI names" in text
    text = post(job(section(PROFILE), pocket_section(tool=tool(spindleRPM=12000, description="other name"))))
    assert ";GOLIATH TOOL n=1 d=6.000 flute=22.000 kind=flat\n" in text
    assert "; Note: the operations run T1 at different spindle speeds (12000, 18000 rpm)" in text
    assert "; Note: the operations describe T1 differently, so TOOL has no name" in text
    assert body(text, "op2")[0] == "M3 S12000"
    text = post(job(section(PROFILE, tool=tool(rampingSpindleRPM=9000))))
    assert ";GOLIATH TOOL n=1 d=6.000 flute=22.000 kind=flat name=\"6mm 2F upcut\"\n" in text


@needs_node
def test_drilling_cycles_are_expanded_by_the_engine_and_noted() -> None:
    cycle = {"clearance": 5, "retract": 2, "stock": 0, "depth": 10, "feedrate": 300, "retractFeedrate": 600,
             "dwell": 0.5, "incrementalDepth": 4, "chipBreakDistance": 0.2}
    holes = [[50, 50, -10], [150, 50, -10]]
    text = post(job(section([["cycle", "drilling", cycle, holes], ["rapid", 150, 50, 15]],
                            strategy="drill", initial=[50, 50, 15])))
    assert body(text) == [
        "M3 S18000",
        "; Note: Fusion drilling cycle, expanded into moves by Fusion's post engine",
        "G0 Z15.000",
        "G0 X50.000 Y50.000",
        "G0 Z5.000",
        "G0 Z2.000",
        "G1 Z-10.000 F300",
        "G0 Z5.000",
        "G0 X150.000",
        "G0 Z2.000",
        "G1 Z-10.000",
        "G0 Z5.000",
        "G0 Z15.000",
    ]
    text = post(job(section([["cycle", "counter-boring", cycle, holes[:1]]], strategy="drill",
                            initial=[50, 50, 15])))
    assert "G4 P0.500" in body(text)
    for kind in ("deep-drilling", "chip-breaking", "reaming"):
        post(job(section([["cycle", kind, cycle, holes]], strategy="drill", initial=[50, 50, 15])))


@needs_node
def test_comments_coolant_and_spindle_words() -> None:
    text = post(job(section([
        ["comment", "Spindle warm-up"],
        ["comment", "GOLIATH OP id=x"],
        ["command", "COMMAND_COOLANT_ON"],
        ["rapid", 100, 200, 5],
        ["spindle_speed", 12000],
        ["linear", 100, 200, -1, 200],
        ["command", "COMMAND_STOP_SPINDLE"],
        ["command", "COMMAND_SPINDLE_CLOCKWISE"],
        ["dwell", 1.5],
        ["command", "COMMAND_COOLANT_OFF"],
    ])))
    assert body(text) == [
        "M3 S18000",
        "; Fusion: Spindle warm-up",
        "; Fusion: GOLIATH OP id=x",
        "; Note: Fusion coolant command not written: the Goliath has no coolant",
        "G0 Z15.000",
        "G0 X100.000 Y200.000",
        "G0 Z5.000",
        "M3 S12000",
        "G1 Z-1.000 F200",
        "M5",
        "M3 S18000",
        "G4 P1.500",
        "; Note: Fusion coolant command not written: the Goliath has no coolant",
    ]


# --------------------------------------------------------------------------
# Refusals: each fires with its message and writes no program
# --------------------------------------------------------------------------

OTHER_WCS = [[0, 0, 1], [1, 0, 0], [0, 1, 0]]


def _refusal_cases() -> List[Tuple[str, Dict[str, Any], str]]:
    cases = [
        ("inch output", standard_job(unit="IN"), r"output unit is inches.*millimetres"),
        ("tower 2 not set", standard_job(properties={}), r"'Tower 2 axis' is not set"),
        ("no operation", job(), "no operation to post"),
        ("second tool", job(section(PROFILE), pocket_section(tool=tool(number=2))),
         r"more than one tool: .*T1.*T2"),
        ("same number, other tool", job(section(PROFILE), pocket_section(tool=tool(diameter=8.0))),
         r"not the same tool \(diameter 6\.000 and 8\.000 mm"),
        ("tool in inches", job(section(PROFILE, tool=tool(unit="IN", diameter=0.25))), "defined in inches"),
        ("counter-clockwise spindle", job(section(PROFILE, tool=tool(clockwise=False))), "counter-clockwise"),
        ("spindle off", job(section(PROFILE, tool=tool(spindleRPM=0))), r"at 0 rpm"),
        ("no flute length", job(section(PROFILE, tool=tool(fluteLength=0))), r"flute length 0\.000 mm"),
        ("no tool number", job(section(PROFILE, tool=tool(number=1.5))), "whole tool number"),
        ("radius compensation", job(section([["rapid", 100, 200, 5],
                                             ["radius_compensation", "RADIUS_COMPENSATION_LEFT"]])),
         r"radius compensation.*In computer"),
        ("tapping", job(section([["cycle", "tapping", {}, [[1, 1, -5]]]], strategy="drill")),
         r"the cycle 'tapping'.*'Drilling - rapid out'"),
        ("boring", job(section([["cycle", "boring", {}, [[1, 1, -5]]]], strategy="drill")), r"the cycle 'boring'"),
        ("probing", job(section([["cycle", "probing-x", {}, [[1, 1, -5]]]])), r"the cycle 'probing-x'"),
        ("origin not at the tower-1 corner",
         standard_job(globals=globals_with(stock_lower_x=-1220, stock_upper_x=1220)),
         r"WCS origin is not at the stock corner where tower 1 stands.*X -1220\.000 to 1220\.000"),
        ("Z zero at the stock bottom",
         standard_job(globals=globals_with(stock_lower_z=0, stock_upper_z=18)),
         r"Z = 0 is not at the stock top.*Z 0\.000 to 18\.000"),
        ("no stock parameter", standard_job(globals=globals_with(stock_upper_z=None)),
         r"did not pass the stock parameter 'stock-upper-z'"),
        ("stock with no thickness", standard_job(globals=globals_with(stock_lower_z=0)), "the stock has no size"),
        ("multi-axis", job(section(PROFILE, multi_axis=True)), "multi-axis toolpath"),
        ("tilted work plane", job(section(PROFILE, work_plane={"right": [1, 0, 0], "up": [0, 0, 1],
                                                              "forward": [0, -1, 0]})),
         "tool orientation other than the setup's"),
        ("own origin", job(section(PROFILE, work_origin=[10, 0, 0])), r"its own origin \(10\.000, 0\.000"),
        ("two setups", job(section(PROFILE), pocket_section(global_work_origin=[0, 0, 18])), "another WCS"),
        ("two setups, rotated", job(section(PROFILE), pocket_section(global_work_plane={
            "right": OTHER_WCS[0], "up": OTHER_WCS[1], "forward": OTHER_WCS[2]})), "another WCS"),
        ("turning", job(section(PROFILE, type="TYPE_TURNING")), "not a milling operation"),
        ("optional", job(section(PROFILE, optional=True)), "marked optional"),
        ("feed per revolution", job(section(PROFILE, feed_mode="FEED_PER_REVOLUTION")), "feed per minute"),
        ("pass through", job(section([["pass_through", "M8"]])), "'Pass through'"),
        ("program stop", job(section(PROFILE + [["command", "COMMAND_STOP"]])), r"program stop \(M0/M1\)"),
        ("tool change command", job(section(PROFILE + [["command", "COMMAND_LOAD_TOOL"]])), "a tool change"),
        ("other command", job(section(PROFILE + [["command", "COMMAND_TOOL_MEASURE"]])),
         r"machine command number 12"),
        ("5-axis move", job(section([["rapid5d", 1, 2, 3, 0, 0, 1]])), "5-axis move"),
        ("empty operation", job(section([])), "has no moves"),
        ("zero feed", job(section([["rapid", 100, 200, 5], ["linear", 100, 200, -1, 0.4]])),
         r"feed rate of 0\.4 mm/min"),
        ("reverse spindle speed", job(section([["spindle_speed", -12000]])), r"spindle speed to -12000 rpm"),
        ("second setup with another stock",
         job(section(PROFILE), pocket_section(parameters={"stock-upper-x": 1220, "operation-comment": "P"})),
         r"setups with different stock \(stock-upper-x is 2440\.000 in the first setup and 1220"),
        ("subprogram call", job(section([["parameter", "call-subprogram", "1001"]])), "'Call program'"),
        ("out of range", job(section([["rapid", 100, 200, 5], ["linear", 20000, 200, -1, 200]])),
         r"X 20000\.000 mm is beyond GI's range"),
    ]
    return cases


REFUSALS = _refusal_cases()


@needs_node
@pytest.mark.parametrize("name, the_job, match", REFUSALS, ids=[c[0] for c in REFUSALS])
def test_refusals_fire_with_their_message_and_write_no_program(name: str, the_job: Dict[str, Any], match: str) -> None:
    refused(the_job, match)


@needs_node
def test_refusal_messages_name_the_operation() -> None:
    message = refused(job(section(PROFILE), pocket_section(tool=tool(clockwise=False))), "counter-clockwise")
    assert 'operation "Hinge pocket" (operation 2 in the NC program)' in message


@needs_node
def test_every_successful_test_job_passes_the_writing_rules() -> None:
    # post() lints every output; this runs the remaining shapes once more as a group.
    for the_job in (standard_job(), standard_job(rapids_as_feed=1000), job(section(PROFILE, tool=tool(number=7)))):
        assert gi_problems(post(copy.deepcopy(the_job))) == []


# --------------------------------------------------------------------------
# The post's source
# --------------------------------------------------------------------------


def _source() -> str:
    return POST.read_text(encoding="utf-8")


def test_post_carries_the_apache_spdx_header_first() -> None:
    assert _source().split("\n")[0] == "// SPDX-License-Identifier: Apache-2.0"


def test_post_defines_the_documented_entry_functions() -> None:
    defined = set(re.findall(r"^function (on[A-Za-z0-9]+)\(", _source(), re.MULTILINE))
    unit_pp2 = {"onOpen", "onSection", "onRapid", "onLinear", "onCircular", "onCyclePoint",
                "onRadiusCompensation", "onSectionEnd", "onClose"}
    assert unit_pp2 <= defined, unit_pp2 - defined
    documented = unit_pp2 | {"onParameter", "onCycle", "onCycleEnd", "onDwell", "onSpindleSpeed", "onCommand",
                             "onComment", "onPassThrough", "onRapid5D", "onLinear5D"}
    assert defined <= documented, defined - documented


def test_post_is_plain_es5_for_the_post_engine() -> None:
    code = re.sub(r'"(?:[^"\\]|\\.)*"', '""', _source())
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", code, flags=re.DOTALL)
    for pattern in (r"\blet\b", r"\bconst\b", r"=>", "`", r"\bclass\b", r"\.\.\.", r"\.includes\(",
                    r"\.padStart\(", r"\bNumber\.is"):
        assert not re.search(pattern, code), pattern


def test_post_is_written_from_scratch_and_says_so() -> None:
    source = _source()
    assert "Autodesk, Inc." not in source
    assert "not\n  derived from any Autodesk library post" in source
    assert 'legal = "Copyright 2026 the Goliath-CNC contributors.' in source


def test_post_writes_nothing_before_onclose() -> None:
    # A refusal anywhere must leave no partial program behind: writeln is used in onClose only.
    source = _source()
    on_close = source.index("function onClose()")
    assert source.count("writeln(") == 1 and source.index("writeln(") > on_close


# --------------------------------------------------------------------------
# README for the owner
# --------------------------------------------------------------------------


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_is_apache_and_says_the_post_has_not_run_in_fusion() -> None:
    text = _readme()
    assert text.split("\n")[0] == "<!-- SPDX-License-Identifier: Apache-2.0 -->"
    assert "has not been run in Fusion yet" in text


def test_readme_covers_install_setup_posting_licence_and_the_first_run_checklist() -> None:
    text = _readme()
    for needle in (
        "Post Library",
        "Tower 2 axis",
        "Stock box point",
        "In computer",
        "Personal Use",
        "G1",
        "First-run checklist",
        "Output folder",
        "dump",
    ):
        assert needle in text, needle
    checklist = text[text.index("## First-run checklist") :]
    for job_name in ("outline", "pocket", "holes"):
        assert job_name in checklist, job_name


def test_readme_links_point_at_files_in_the_repository() -> None:
    text = _readme()
    for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", text):
        if target.startswith(("http://", "https://")):
            continue
        assert (POST_DIR / target).resolve().exists(), target


def test_readme_names_every_refusal_reason() -> None:
    text = _readme().lower()
    for reason in ("inches", "more than one tool", "radius compensation", "tapping", "xy plane", "stock corner",
                   "stock top", "multi-axis", "pass through", "program stop"):
        assert reason in text, reason


# --------------------------------------------------------------------------
# Samples: real Fusion output, produced by the owner's first run
# --------------------------------------------------------------------------


def committed_samples() -> List[Path]:
    return sorted(SAMPLES_DIR.glob("*.gi"))


def test_samples_are_kept_byte_for_byte() -> None:
    attributes = (SAMPLES_DIR / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"^\*\.gi\s+-text\b", attributes, re.MULTILINE)


def _samples_or_skip() -> List[Path]:
    samples = committed_samples()
    if not samples:
        pytest.skip(
            "no real Fusion output is committed under posts/fusion/samples/ yet: the post has not been run "
            "in Fusion. The first-run checklist in posts/fusion/README.md produces the samples"
        )
    return samples


def test_committed_samples_follow_the_gi_writing_rules() -> None:
    for path in _samples_or_skip():
        text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
        assert gi_problems(text) == [], path.name


def _header_values(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in text.splitlines():
        if re.match(r";GOLIATH (STOCK|TOWER2|TOOL) ", line):
            keyword, keyed, _ = parse_annotation(line)
            for key, value in keyed.items():
                values["%s.%s" % (keyword.lower(), key)] = value
    return values


def _sample_job_toml(text: str) -> str:
    """The job.toml that goes with a sample: the same panel, tower axis and tool (gi-format.md 14, point 8).

    PROVISIONAL BRIDGE, the same as tests/posts/test_freecad_post.py's: written
    before goliath-prep's job file format (unit P01) is final. When goliath-prep's
    loader refuses this text, update both to the format goliath-prep documents.
    """
    h = _header_values(text)
    lines = [
        'units = "mm"',
        "",
        "[panel]",
        "W = %s" % Decimal(h["stock.x"]),
        "H = %s" % Decimal(h["stock.y"]),
        "T = %s" % Decimal(h["stock.t"]),
        'tower2 = "%s"' % h["tower2.axis"],
        "",
        "[tool]",
        "d = %s" % Decimal(h["tool.d"]),
        "flute = %s" % Decimal(h["tool.flute"]),
    ]
    if "tool.rpm" in h:
        lines.append("rpm = %s" % h["tool.rpm"])
    return "\n".join(lines) + "\n"


def _goliath_prep_reader() -> Any:
    """goliath-prep's GI reader module, or a pytest skip saying why it cannot be used here."""
    try:
        prep_spec = importlib.util.find_spec("goliath_prep")
    except ImportError as exc:  # the package's own guard: it needs Python >= 3.11
        pytest.skip("goliath-prep cannot be imported on this Python (%s); the CI apps job runs this test" % exc)
    if prep_spec is None:
        pytest.skip(
            "goliath-prep is not installed in this environment (it needs Python >= 3.11); "
            "the CI apps job installs it and runs this test"
        )
    if importlib.util.find_spec("goliath_prep.gcode") is None:
        pytest.skip(
            "goliath-prep's GI reader goliath_prep.gcode (unit P02) does not exist yet; this test runs the "
            "Fusion samples through it as soon as it does"
        )
    return importlib.import_module("goliath_prep.gcode")


def test_committed_samples_pass_goliath_prep_check(tmp_path: Path) -> None:
    samples = _samples_or_skip()
    reader = _goliath_prep_reader()
    jobspec = importlib.import_module("goliath_prep.jobspec")  # unit P01; the reader needs it
    for path in samples:
        text = path.read_bytes().decode("utf-8")
        job_toml = tmp_path / (path.stem + ".toml")
        job_toml.write_text(_sample_job_toml(text), encoding="utf-8")
        result = reader.read_gcode(text, flavour="gi", job=jobspec.JobSpec.load(job_toml))
        refusals = [f for f in result.findings if getattr(f, "severity", None) == "refuse"]
        assert refusals == [], "%s is refused by goliath-prep's reader: %r" % (path.name, refusals)
        assert result.gi_header is not None
