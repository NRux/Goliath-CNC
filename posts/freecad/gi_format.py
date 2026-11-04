# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 the Goliath-CNC contributors.
"""GI formatting for the Goliath FreeCAD post: pure Python, no FreeCAD.

GI (Goliath-intermediate G-code) is the contract between a CAM post-processor
and goliath-prep, specified in docs/prep/gi-format.md of the Goliath-CNC
repository: ordinary millimetre, absolute, XY G-code plus ``;GOLIATH``
annotations that carry the stock, the tower-2 axis, the one tool and the
operation boundaries. Everything Goliath-specific (headings, spindle words,
Z safety, every safety check) happens later, in goliath-prep.

This module turns plain data (:class:`StockData`, :class:`ToolData`,
:class:`OpData`) and FreeCAD-like path commands (objects with ``Name`` and
``Parameters``, as ``Path.Command`` has) into GI text: :func:`format_gi`.
``goliath_post.py`` reads the FreeCAD job into that data; keeping the
formatting here lets it be tested without FreeCAD.

What it writes, and why:

* **Frame.** X and Y are written as FreeCAD has them: the stock's minimum
  corner must be at X = 0, Y = 0 (the corner where tower 1 stands), otherwise
  the post refuses and says how to fix it. Z is shifted by ``ZSHIFT = -ZMax``
  of the stock, so Z = 0 is the stock top, and the shift is declared on the
  ``ZSHIFT`` line; goliath-prep never applies it again.
* **Units.** FreeCAD's path commands hold lengths in millimetres and feed rates
  in millimetres per second (its internal speed unit); GI writes millimetres
  with three decimals and feeds in whole millimetres per minute (F x 60). A
  feed that comes out below 1 mm/min is refused, never written as F0.
* **Rapids.** FreeCAD writes its rapid rate as F on ``G0`` lines. GI has no
  rapid speed (goliath-prep decides traverse), so F on ``G0`` is not written and
  does not change the feed of later cuts.
* **Arcs.** ``G2``/``G3`` with ``I``/``J`` in the XY plane; ``I`` and ``J`` are
  re-based on the start point as written, so the written centre is the true
  centre rounded once. A ``K`` other than 0 and an arc without ``I``/``J`` are
  refused, and so is a sliver arc (less than a half turn) whose end would be
  written equal to its start, because GI reads that as a full circle (PHYS-16).
* **Canned drilling cycles** (``G73``, ``G81``, ``G82``, ``G83``, ``G85``, as
  FreeCAD's Drilling operation writes them) are expanded into ``G0``/``G1``
  moves by the RS274/NGC rules (NIST RS274/NGC version 3, section 3.5.16):
  preliminary motion, the cycle, and the retract to R (``G99``) or to the
  higher of the start height and R (``G98``). A peck (``G73``, ``G83``) backs
  off 0.254 mm above the hole bottom before the next peck, the distance
  LinuxCNC documents. Each expanded cycle is noted in the file. A cycle with no
  ``G98``/``G99`` before it is refused, never given a default.
* **Words outside the vocabulary** are refused with the operation, the command's
  position in its path and the reason: cutter compensation (``G41``/``G42``),
  incremental or inch mode, other planes, homing, machine coordinates, work
  offsets other than ``G54``, pauses, program ends, ``M4``, tool changes inside
  an operation, rotary axes, and anything else this module does not know.
* **Comments** from FreeCAD are written as ``; FreeCAD: <text>`` and notes of
  the post as ``; Note: <text>``. Neither prefix can be read as a ``;GOLIATH``
  annotation or a ``;U``/``;S`` marker.

Python 3.9 or newer, standard library only: FreeCAD runs posts in its own
Python interpreter.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# goliath_post.py checks this before it uses a module named gi_format.
GI_FORMAT_MODULE_ID = "goliath-cnc posts/freecad gi_format"

POST_NAME = "goliath_post.py"
POST_VERSION = "0.1.0"
GI_VERSION_LINE = ";GOLIATH-GI 1"

OP_KINDS = ("profile", "pocket", "drill", "engrave", "adaptive", "other")
TOOL_KINDS = ("flat", "ball", "bull", "vbit", "other")
TOWER2_AXES = ("X", "Y")
COOLANT_WORDS = {"None": None, "Flood": "M8", "Mist": "M7"}

# The stock's minimum corner counts as X = 0, Y = 0 when it is closer than half
# the file's resolution (three decimals): the file could not show a smaller offset.
ORIGIN_TOLERANCE_MM = Decimal("0.0005")
# through=1 when the operation's final depth is at most this far above the stock
# bottom: the allowance gi-format.md gives through=auto (T - 0.05 mm below the top).
THROUGH_ALLOWANCE_MM = Decimal("0.05")
# RS274/NGC peck cycles back off "a bit" above the hole bottom; LinuxCNC documents 0.010 inch.
PECK_BACKOFF_MM = Decimal("0.254")
# More pecks than this for one hole is refused rather than written line by line.
MAX_PECKS_PER_HOLE = 1000
# FreeCAD CAM's own tolerance for "the same point" (Path.Geom.Tolerance): FreeCAD writes a full
# circle as one arc whose end is its start within it. An arc whose end is farther from its start
# than this, but rounds onto it at three decimals, is not a full circle to FreeCAD (gi-format.md 8.7).
FREECAD_COINCIDENT_MM = Decimal("0.000001")
_ROTARY_AXES = frozenset("ABCUVW")
# FreeCAD's internal speed unit is mm/s; GI writes mm/min.
SECONDS_PER_MINUTE = Decimal(60)

_MM = Decimal("0.001")
_ONE = Decimal(1)
_TOKEN = re.compile(r"[A-Za-z0-9_.+\-]+\Z")
_CODE = re.compile(r"([GM])(\d+)(?:\.(\d+))?\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DIGITS = re.compile(r"[0-9]+\Z")


class GoliathPostError(Exception):
    """The post refuses to write GI. The message says what was found and how to fix it."""


# --------------------------------------------------------------------------
# The data the post reads from FreeCAD
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StockData:
    """The job's stock: its axis-aligned bounds in FreeCAD's document frame, mm."""

    x_min: float
    y_min: float
    z_min: float
    x_max: float
    y_max: float
    z_max: float
    source: str = "the job's stock"


@dataclass(frozen=True)
class ToolData:
    """The tool of one FreeCAD tool controller. ``rpm`` is the speed its path starts the spindle at."""

    number: int
    diameter: float
    flute: float
    rpm: float
    kind: Optional[str] = None
    name: Optional[str] = None
    source: str = "a tool controller"


@dataclass(frozen=True)
class OpData:
    """One FreeCAD operation.

    ``commands`` are FreeCAD path commands (``Name``, ``Parameters``) in the
    document frame. ``final_depth`` is the operation's final depth in the same
    frame, or None when the operation has none. ``spindle_rpm`` is the speed its
    tool controller starts the spindle at. ``notes`` are written as plain
    comments before the ``OP`` line.
    """

    id: str
    kind: str
    label: str
    tool: int
    spindle_rpm: float
    commands: Sequence[Any]
    final_depth: Optional[float] = None
    coolant: str = "None"
    notes: Tuple[str, ...] = ()


# --------------------------------------------------------------------------
# Numbers and text
# --------------------------------------------------------------------------


def number(value: Any, what: str) -> Decimal:
    """``value`` as an exact decimal (a float through its shortest repr), or a refusal naming ``what``."""
    if isinstance(value, bool):
        raise GoliathPostError("%s is %r, not a number" % (what, value))
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise GoliathPostError("%s is %r" % (what, value))
        result = Decimal(repr(value))
    else:
        raise GoliathPostError("%s is %r, not a number" % (what, value))
    if not result.is_finite():
        raise GoliathPostError("%s is %s" % (what, result))
    return result


def mm(value: Decimal) -> str:
    """Three decimals, never ``-0.000``."""
    rounded = value.quantize(_MM, rounding=ROUND_HALF_EVEN)
    if rounded.is_zero():
        rounded = rounded.copy_abs()
    return format(rounded, "f")


def whole(value: Decimal) -> int:
    return int(value.quantize(_ONE, rounding=ROUND_HALF_EVEN))


def quoted(text: Any, what: str) -> str:
    """A GI quoted value: backslash and double quote escaped; control characters refused."""
    if not isinstance(text, str):
        raise GoliathPostError("%s is %r, not text" % (what, text))
    bad = _CONTROL.search(text)
    if bad:
        raise GoliathPostError(
            "%s %r contains the control character U+%04X, which a GI value cannot carry; rename it"
            % (what, text, ord(bad.group()))
        )
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def plain_comment(prefix: str, text: str) -> str:
    """``; <prefix>: <text>``. The prefix keeps the comment from reading as an annotation or a marker."""
    if prefix[:1] in "UuSs" or prefix.upper().startswith("GOLIATH"):
        raise ValueError("comment prefix %r could be read as a marker or an annotation" % prefix)
    clean = " ".join(_CONTROL.sub(" ", text).split())
    return "; %s: %s" % (prefix, clean)


def command_code(name: Any) -> Optional[str]:
    """``"G01"`` -> ``"G1"``, ``"M03"`` -> ``"M3"``; None for comments and names that are not a plain G or M code."""
    if not isinstance(name, str):
        return None
    match = _CODE.match(name.strip())
    if match is None or match.group(3) is not None:
        return None
    return "%s%d" % (match.group(1), int(match.group(2)))


def freecad_cam_text(version: Sequence[Any]) -> str:
    """The SOURCE ``cam`` value from ``FreeCAD.Version()``: ``"FreeCAD 1.1.4"``, or what FreeCAD reported."""
    parts = [str(p).strip() for p in list(version)[:3]]
    if len(parts) == 3 and all(_DIGITS.match(p) for p in parts):
        return "FreeCAD %s.%s.%s" % tuple(parts)
    shown = " ".join(p for p in parts if p)
    return "FreeCAD %s" % shown if shown else "FreeCAD (version not reported)"


# --------------------------------------------------------------------------
# FreeCAD names -> GI enumerations
# --------------------------------------------------------------------------

# The last component of an operation proxy's module: Path.Op.<Name> since FreeCAD
# 0.21, PathScripts.Path<Name> before. Operations not listed are written as
# kind=other, with a note.
_FREECAD_OP_KINDS = {
    "Profile": "profile",
    "ProfileContour": "profile",
    "ProfileEdges": "profile",
    "ProfileFaces": "profile",
    "Pocket": "pocket",
    "PocketShape": "pocket",
    "Drilling": "drill",
    "Engrave": "engrave",
    "Vcarve": "engrave",
    "Adaptive": "adaptive",
}

# FreeCAD 1.1 tool bit ShapeType names and their aliases; older bits name a shape file.
_FREECAD_SHAPE_KINDS = {
    "endmill": "flat",
    "ballend": "ball",
    "bullnose": "bull",
    "torus": "bull",
    "vbit": "vbit",
    "v-bit": "vbit",
}


def op_kind_for(module: str) -> Optional[str]:
    """GI kind for an operation proxy defined in ``module``, or None when it has no kind of its own."""
    last = (module or "").rsplit(".", 1)[-1]
    if last not in _FREECAD_OP_KINDS and last.startswith("Path"):
        last = last[4:]
    return _FREECAD_OP_KINDS.get(last)


def tool_kind_for(shape: Optional[str]) -> Optional[str]:
    """GI tool kind for a FreeCAD bit shape (``"Endmill"``, ``"v-bit"``, ``".../ballend.fcstd"``); None if unknown."""
    if not shape or not isinstance(shape, str):
        return None
    key = re.split(r"[\\/]", shape.strip())[-1]
    if key.lower().endswith(".fcstd"):
        key = key[: -len(".fcstd")]
    key = key.strip().lower()
    if not key:
        return None
    return _FREECAD_SHAPE_KINDS.get(key, "other")


# --------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------


def format_gi(
    ops: Iterable[OpData],
    stock: StockData,
    tools: Iterable[ToolData],
    *,
    tower2: str,
    cam: str,
    notes: Sequence[str] = (),
) -> str:
    """The GI text for ``ops`` on ``stock``.

    ``tools`` holds the tool of every tool controller the operations use; GI
    carries one tool, so they must all be the same tool. ``tower2`` is the
    tower-2 axis, ``"X"`` or ``"Y"``; ``cam`` the SOURCE ``cam`` value; ``notes``
    are written as plain comments in the header. Raises GoliathPostError, with
    the reason and the fix, for anything GI cannot carry exactly.
    """
    if tower2 not in TOWER2_AXES:
        raise GoliathPostError("tower2 must be X or Y, not %r" % (tower2,))
    ops = list(ops)
    if not ops:
        raise GoliathPostError("there is no active operation to write")
    tool, rpm, name, tool_notes = _one_tool(list(tools))
    frame = _Frame(stock)

    lines = [
        GI_VERSION_LINE,
        ";GOLIATH SOURCE cam=%s post=%s"
        % (quoted(cam, "the CAM name"), quoted("%s %s" % (POST_NAME, POST_VERSION), "the post name")),
        ";GOLIATH UNITS mm",
        ";GOLIATH STOCK x=%s y=%s t=%s origin=t1-corner zzero=top" % (mm(frame.x), mm(frame.y), mm(frame.t)),
        ";GOLIATH TOWER2 axis=%s" % tower2,
        _tool_line(tool, rpm, name),
        ";GOLIATH ZSHIFT %s" % mm(frame.zshift),
    ]
    lines.extend(plain_comment("Note", note) for note in list(tool_notes) + list(notes))
    lines.append("G17 G21 G90")
    lines.append("T%d M6" % tool.number)

    writer = _BodyWriter(frame.zshift, lines)
    seen = set()
    for op in ops:
        _check_op(op, tool.number, seen)
        lines.extend(plain_comment("Note", note) for note in op.notes)
        head = ";GOLIATH OP id=%s kind=%s through=%s tool=%d" % (
            op.id,
            op.kind,
            frame.through(op),
            op.tool,
        )
        if op.label:
            head += " label=%s" % quoted(op.label, "operation %r label" % op.id)
        lines.append(head)
        coolant = COOLANT_WORDS[op.coolant]
        if coolant:
            lines.append(coolant)
        spindle = whole(number(op.spindle_rpm, "%s spindle speed" % _op_name(op)))
        if spindle < 1:
            raise GoliathPostError(
                "%s: spindle speed %s rpm; set the tool controller's spindle speed" % (_op_name(op), op.spindle_rpm)
            )
        lines.append("M3 S%d" % spindle)
        moves = writer.write_operation(op)
        if moves == 0:
            raise GoliathPostError(
                "%s has no moves: its path is empty. Recompute the job and check FreeCAD's report view "
                "for errors, or deactivate the operation" % _op_name(op)
            )
        if coolant:
            lines.append("M9")
        lines.append(";GOLIATH OPEND id=%s" % op.id)
    lines.append("M5")
    lines.append("M30")
    return "\n".join(lines) + "\n"


def _op_name(op: OpData) -> str:
    return "operation %r (%s)" % (op.label, op.id)


def _check_op(op: OpData, tool_number: int, seen: set) -> None:
    if not isinstance(op.id, str) or not _TOKEN.match(op.id):
        raise GoliathPostError(
            "operation id %r is not a GI token (letters, digits, '_', '.', '+', '-')" % (op.id,)
        )
    if op.id in seen:
        raise GoliathPostError("operation id %r appears twice; GI operation ids are unique" % op.id)
    seen.add(op.id)
    if op.kind not in OP_KINDS:
        raise GoliathPostError("%s: kind %r is not one of %s" % (_op_name(op), op.kind, ", ".join(OP_KINDS)))
    if op.tool != tool_number:
        raise GoliathPostError("%s uses tool %s, but the file's tool is %d" % (_op_name(op), op.tool, tool_number))
    if op.coolant not in COOLANT_WORDS:
        raise GoliathPostError(
            "%s: coolant mode %r is not one of %s" % (_op_name(op), op.coolant, ", ".join(COOLANT_WORDS))
        )


class _Frame:
    """Stock size, the Z shift and the through rule, from the stock's bounds."""

    def __init__(self, stock: StockData) -> None:
        where = stock.source
        x_min = number(stock.x_min, "%s XMin" % where)
        y_min = number(stock.y_min, "%s YMin" % where)
        z_min = number(stock.z_min, "%s ZMin" % where)
        x_max = number(stock.x_max, "%s XMax" % where)
        y_max = number(stock.y_max, "%s YMax" % where)
        z_max = number(stock.z_max, "%s ZMax" % where)
        if abs(x_min) >= ORIGIN_TOLERANCE_MM or abs(y_min) >= ORIGIN_TOLERANCE_MM:
            raise GoliathPostError(
                "the job origin is not at the tower-1 corner: the minimum corner of %s is at X = %s, Y = %s. "
                "GI needs it at X = 0, Y = 0, the stock corner where tower 1 stands, with the stock along +X "
                "and +Y. Move the model and the stock together in the FreeCAD job so that this corner is at "
                "the document origin, then recompute and post again" % (where, mm(x_min), mm(y_min))
            )
        self.x = x_max - x_min
        self.y = y_max - y_min
        self.t = z_max - z_min
        for size, name in ((self.x, "size along X"), (self.y, "size along Y"), (self.t, "thickness")):
            if mm(size) == "0.000" or size < 0:
                raise GoliathPostError(
                    "%s has no %s (%s mm); give the job a box stock the size of the panel" % (where, name, mm(size))
                )
        self.z_min = z_min
        self.zshift = -z_max

    def through(self, op: OpData) -> str:
        """``1`` when the final depth reaches the stock bottom; otherwise ``auto`` (no claim: the geometry decides)."""
        if op.final_depth is None:
            return "auto"
        depth = number(op.final_depth, "%s final depth" % _op_name(op))
        return "1" if depth <= self.z_min + THROUGH_ALLOWANCE_MM else "auto"


def _one_tool(tools: List[ToolData]) -> Tuple[ToolData, Optional[int], Optional[str], List[str]]:
    """(the file's one tool, its rpm, its name, notes): rpm and name only where every tool controller agrees."""
    if not tools:
        raise GoliathPostError("no tool controller: every operation needs one, and GI needs its tool")
    for tool in tools:
        if isinstance(tool.number, bool) or not isinstance(tool.number, int) or tool.number < 0:
            raise GoliathPostError("%s has tool number %r; GI needs a whole number" % (tool.source, tool.number))
    numbers = sorted({t.number for t in tools})
    if len(numbers) > 1:
        raise GoliathPostError(
            "GI carries one tool, but the operations use tools %s (%s). Make one FreeCAD job per tool "
            "(each written to its own GI file), or use one tool for every operation"
            % (", ".join(str(n) for n in numbers), "; ".join("%s: tool %d" % (t.source, t.number) for t in tools))
        )
    first = tools[0]
    first_values = _tool_values(first)
    for other in tools[1:]:
        for field, value in _tool_values(other).items():
            if value != first_values[field]:
                raise GoliathPostError(
                    "%s and %s both use tool %d, but with a different %s (%s and %s). Make them the same tool bit"
                    % (first.source, other.source, first.number, field, first_values[field], value)
                )
    notes = []
    rpms = sorted({whole(number(t.rpm, "%s spindle speed" % t.source)) for t in tools})
    for rpm in rpms:
        if rpm < 1:
            raise GoliathPostError("a tool controller of tool %d has spindle speed %d rpm" % (first.number, rpm))
    rpm_value: Optional[int] = rpms[0]
    if len(rpms) > 1:
        rpm_value = None
        notes.append(
            "the tool controllers of tool %d run at different spindle speeds (%s rpm); TOOL rpm is left out "
            "and each operation starts the spindle at its own speed" % (first.number, ", ".join(str(r) for r in rpms))
        )
    names = sorted({t.name for t in tools if t.name})
    name = names[0] if len(names) == 1 and all(t.name for t in tools) else None
    if len(names) > 1:
        notes.append(
            "the tool controllers of tool %d name their bit differently (%s); TOOL name is left out"
            % (first.number, ", ".join(names))
        )
    return first, rpm_value, name, notes


def _tool_values(tool: ToolData) -> Dict[str, str]:
    """The tool's physical values as the file writes them; tool controllers must agree on these."""
    values = {
        "diameter": mm(number(tool.diameter, "%s diameter" % tool.source)),
        "flute length": mm(number(tool.flute, "%s flute length" % tool.source)),
        "kind": str(tool.kind),
    }
    if tool.kind is not None and tool.kind not in TOOL_KINDS:
        raise GoliathPostError("%s: tool kind %r is not one of %s" % (tool.source, tool.kind, ", ".join(TOOL_KINDS)))
    return values


def _tool_line(tool: ToolData, rpm: Optional[int], name: Optional[str]) -> str:
    values = _tool_values(tool)
    for field in ("diameter", "flute length"):
        if values[field] == "0.000" or values[field].startswith("-"):
            raise GoliathPostError(
                "%s has %s %s mm; GI needs a length greater than 0" % (tool.source, field, values[field])
            )
    parts = ["n=%d" % tool.number, "d=%s" % values["diameter"], "flute=%s" % values["flute length"]]
    if tool.kind is not None:
        parts.append("kind=%s" % tool.kind)
    if rpm is not None:
        parts.append("rpm=%d" % rpm)
    if name:
        parts.append("name=%s" % quoted(name, "%s tool name" % tool.source))
    return ";GOLIATH TOOL " + " ".join(parts)


# --------------------------------------------------------------------------
# The body: FreeCAD path commands -> GI lines
# --------------------------------------------------------------------------

_PASS_THROUGH = frozenset({"G17", "G21", "G40", "G54", "G90", "G94", "M5", "M7", "M8", "M9"})
_CYCLE_WORDS = {
    "G81": "XYZRFL",
    "G82": "XYZRFLP",
    "G83": "XYZRFLQ",
    "G73": "XYZRFLQ",
    "G85": "XYZRFL",
}
_REFUSED = {
    "G20": "inch units: set FreeCAD's CAM output to millimetres; GI is millimetres only",
    "G91": "incremental coordinates: GI is absolute (G90) only",
    "G18": "an arc plane other than XY: GI arcs lie in the XY plane (G17)",
    "G19": "an arc plane other than XY: GI arcs lie in the XY plane (G17)",
    "G41": "cutter radius compensation (G41/G42): turn on 'Use Compensation' in the operation so that FreeCAD "
    "offsets the path itself",
    "G42": "cutter radius compensation (G41/G42): turn on 'Use Compensation' in the operation so that FreeCAD "
    "offsets the path itself",
    "G28": "a move to a machine home position: the Goliath has none; GI's origin is the tower-1 corner",
    "G30": "a move to a machine home position: the Goliath has none; GI's origin is the tower-1 corner",
    "G53": "machine coordinates: GI coordinates are the panel frame from the tower-1 corner",
    "G92": "a coordinate offset: GI coordinates are the panel frame from the tower-1 corner",
    "G84": "a tapping cycle: the Goliath cannot tap",
    "G74": "a tapping cycle: the Goliath cannot tap",
    "M0": "a program pause: nothing can resume a paused program on the Goliath",
    "M1": "a program pause: nothing can resume a paused program on the Goliath",
    "M2": "a program end inside an operation",
    "M30": "a program end inside an operation",
    "M4": "counter-clockwise spindle: the Goliath spindle turns one way (M3)",
    "M6": "a tool change inside an operation: GI carries one tool",
}
for _n in range(55, 60):
    _REFUSED["G%d" % _n] = (
        "a work offset other than G54: the Goliath has no work offsets; use the single fixture G54 in the job"
    )
for _n in (76, 86, 87, 88, 89):
    _REFUSED["G%d" % _n] = "a canned cycle this post does not expand"


def command_text(command: Any) -> str:
    """A FreeCAD command as text, for messages."""
    name = getattr(command, "Name", None)
    params = getattr(command, "Parameters", None)
    text = str(name) if name is not None else repr(command)
    if isinstance(params, Mapping) and params:
        text += " " + " ".join("%s%s" % (k, v) for k, v in params.items())
    return text


class _BodyWriter:
    """Writes the commands of each operation; keeps the modal state of one program across operations."""

    def __init__(self, zshift: Decimal, lines: List[str]) -> None:
        self.zshift = zshift
        self.lines = lines
        # Position in FreeCAD's frame; None until the program first sets the axis.
        self.x: Optional[Decimal] = None
        self.y: Optional[Decimal] = None
        self.z: Optional[Decimal] = None
        self.feed: Optional[int] = None  # mm/min, FreeCAD's current feed
        self.feed_written: Optional[int] = None
        self.retract: Optional[str] = None  # G98 or G99
        self.cycle: Optional[str] = None
        self.sticky: Dict[str, Decimal] = {}
        self.moves = 0
        self.op: Optional[OpData] = None
        self.where = ""

    def write_operation(self, op: OpData) -> int:
        self.op = op
        self.moves = 0
        # Each OP block states its feed on its first feed move, so it reads the same on its own.
        self.feed_written = None
        for index, command in enumerate(op.commands, start=1):
            self.where = "%s, path command %d %r" % (_op_name(op), index, command_text(command))
            self._command(command)
        return self.moves

    # -- helpers -----------------------------------------------------------

    def _refuse(self, reason: str) -> GoliathPostError:
        return GoliathPostError("%s: %s" % (self.where, reason))

    def _params(self, command: Any) -> Dict[str, Decimal]:
        raw = getattr(command, "Parameters", None)
        if not isinstance(raw, Mapping):
            raise self._refuse("its parameters are %r, not a mapping of letters to numbers" % (raw,))
        params = {}
        for key, value in raw.items():
            if not (isinstance(key, str) and len(key) == 1 and "A" <= key <= "Z"):
                raise self._refuse("parameter %r is not an upper-case letter" % (key,))
            params[key] = number(value, "%s: parameter %s" % (self.where, key))
        return params

    def _only(self, code: str, params: Mapping[str, Decimal], allowed: str) -> None:
        extra = sorted(set(params) - set(allowed))
        if extra:
            if code in ("G2", "G3") and "R" in extra:
                why = "R gives an arc by its radius; GI arcs need an I/J centre"
            elif _ROTARY_AXES.intersection(extra):
                why = "GI has no rotary or extra axes"
            else:
                why = "GI has no such word on %s" % code
            raise self._refuse(
                "%s carries %s, which this post does not translate on %s (%s)" % (code, ", ".join(extra), code, why)
            )

    def _set_feed(self, value: Decimal) -> None:
        per_minute = whole(value * SECONDS_PER_MINUTE)
        if per_minute < 1:
            raise self._refuse(
                "feed rate %s mm/s is %d mm/min (FreeCAD stores feeds in mm/s; GI writes mm/min). Set the "
                "feed rates of the operation's tool controller" % (value, per_minute)
            )
        self.feed = per_minute

    def _move(
        self,
        code: str,
        axes: Mapping[str, Decimal],
        feed: bool = False,
        centre: Optional[Tuple[Decimal, Decimal]] = None,
    ) -> None:
        words = [code]
        for axis in "XYZ":
            if axis in axes:
                value = axes[axis] + self.zshift if axis == "Z" else axes[axis]
                words.append(axis + mm(value))
        if centre is not None:
            words.append("I" + mm(centre[0]))
            words.append("J" + mm(centre[1]))
        if feed:
            if self.feed is None:
                raise self._refuse("a feed move with no feed rate before it; set the tool controller's feed rates")
            if self.feed != self.feed_written:
                words.append("F%d" % self.feed)
                self.feed_written = self.feed
        self.lines.append(" ".join(words))
        if "X" in axes:
            self.x = axes["X"]
        if "Y" in axes:
            self.y = axes["Y"]
        if "Z" in axes:
            self.z = axes["Z"]
        self.moves += 1

    def _leave_cycle(self) -> None:
        self.cycle = None
        self.sticky = {}

    # -- dispatch ----------------------------------------------------------

    def _command(self, command: Any) -> None:
        name = getattr(command, "Name", None)
        if not isinstance(name, str):
            raise self._refuse("it is not a FreeCAD path command (it has no Name)")
        stripped = name.strip()
        if stripped.startswith("("):
            text = stripped[1:-1] if stripped.endswith(")") else stripped[1:]
            if text.strip().upper().startswith("MC_RUN_COMMAND"):
                raise self._refuse(
                    "MC_RUN_COMMAND asks the post to pass a raw controller command; GI carries none "
                    "(goliath-prep writes every machine word)"
                )
            self.lines.append(plain_comment("FreeCAD", text))
            return
        code = command_code(stripped)
        if code is None or code in _REFUSED:
            raise self._refuse(_REFUSED.get(code or "", "outside what this post translates into GI"))
        params = self._params(command)
        if code in ("G0", "G1"):
            self._linear(code, params)
        elif code in ("G2", "G3"):
            self._arc(code, params)
        elif code in _CYCLE_WORDS:
            self._cycle(code, params)
        elif code == "G4":
            self._only(code, params, "P")
            if "P" not in params or params["P"] < 0:
                raise self._refuse("a dwell needs P, its time in seconds, at least 0")
            self.lines.append("G4 P%s" % mm(params["P"]))
        elif code == "G80":
            self._only(code, params, "")
            self._leave_cycle()
        elif code in ("G98", "G99"):
            self._only(code, params, "")
            self.retract = code
        elif code == "M3":
            self._only(code, params, "S")
            if "S" in params:
                rpm = whole(params["S"])
                if rpm < 1:
                    raise self._refuse("M3 with spindle speed %s rpm" % params["S"])
                self.lines.append("M3 S%d" % rpm)
            else:
                self.lines.append("M3")
        elif code in _PASS_THROUGH:
            self._only(code, params, "")
            self.lines.append(code)
        else:
            raise self._refuse("outside what this post translates into GI")

    def _linear(self, code: str, params: Dict[str, Decimal]) -> None:
        self._only(code, params, "XYZF")
        self._leave_cycle()
        if code == "G1" and "F" in params:
            self._set_feed(params["F"])
        axes = {a: params[a] for a in "XYZ" if a in params}
        if axes:
            self._move(code, axes, feed=(code == "G1"))

    def _arc(self, code: str, params: Dict[str, Decimal]) -> None:
        self._only(code, params, "XYZIJKF")
        self._leave_cycle()
        if "K" in params and not params["K"].is_zero():
            raise self._refuse("an arc with K %s is not an arc in the XY plane" % params["K"])
        if "I" not in params and "J" not in params:
            raise self._refuse("an arc without an I/J centre")
        if "F" in params:
            self._set_feed(params["F"])
        i = params.get("I", Decimal(0))
        j = params.get("J", Decimal(0))
        self._refuse_sliver_written_as_full_circle(code, params, i, j)
        # Re-base the centre on the start point as it is written, so the written
        # centre is the true centre rounded once.
        if self.x is not None:
            i = _round(self.x + i) - _round(self.x)
        if self.y is not None:
            j = _round(self.y + j) - _round(self.y)
        axes = {a: params[a] for a in "XYZ" if a in params}
        self._move(code, axes, feed=True, centre=(i, j))

    def _refuse_sliver_written_as_full_circle(
        self, code: str, params: Mapping[str, Decimal], i: Decimal, j: Decimal
    ) -> None:
        """PHYS-16: an arc whose written end equals its written start is a full circle in GI.

        Rounding to three decimals must not turn a sliver FreeCAD asked for into
        a full circle. An arc that sweeps most of a circle is a full circle to
        within the file's resolution and is written as one; an arc of less than
        a half turn is refused. (An arc from an unknown start is refused by the
        reader, gi-format.md 8.7.)
        """
        if self.x is None or self.y is None:
            return
        end_x = params.get("X", self.x)
        end_y = params.get("Y", self.y)
        if _round(end_x) != _round(self.x) or _round(end_y) != _round(self.y):
            return
        gap = ((end_x - self.x) ** 2 + (end_y - self.y) ** 2).sqrt()
        if gap <= FREECAD_COINCIDENT_MM:
            return  # the same point to FreeCAD: a full circle
        cx, cy = self.x + i, self.y + j
        a0 = math.atan2(float(self.y - cy), float(self.x - cx))
        a1 = math.atan2(float(end_y - cy), float(end_x - cx))
        ccw = (a1 - a0) % (2 * math.pi)
        sweep = math.degrees(ccw if code == "G3" else (2 * math.pi - ccw) % (2 * math.pi))
        if sweep < 180.0:
            raise self._refuse(
                "an arc that sweeps %.4f degrees ends %.2g mm from its start. With the three decimals GI "
                "writes, its end equals its start, and GI reads such an arc as a full circle (360 degrees, "
                "gi-format.md section 8.7). Remove the tiny arc from the path: it usually comes from a corner "
                "between two nearly straight segments, so simplify that geometry" % (sweep, float(gap))
            )

    def _cycle(self, code: str, params: Dict[str, Decimal]) -> None:
        self._only(code, params, _CYCLE_WORDS[code])
        if self.retract is None:
            raise self._refuse(
                "a canned cycle with no G98/G99 before it, so its retract height is not known"
            )
        if self.cycle != code:
            self.sticky = {}
        self.cycle = code
        needed = ["Z", "R"] + (["Q"] if code in ("G73", "G83") else []) + (["P"] if code == "G82" else [])
        names = {"Z": "the hole bottom", "R": "the retract plane", "Q": "the peck depth", "P": "the dwell time"}
        for key in needed:
            if key in params:
                self.sticky[key] = params[key]
            if key not in self.sticky:
                raise self._refuse("%s without %s (%s)" % (code, key, names[key]))
        bottom, r = self.sticky["Z"], self.sticky["R"]
        q = self.sticky.get("Q")
        p = self.sticky.get("P")
        if "F" in params:
            self._set_feed(params["F"])
        if self.feed is None:
            raise self._refuse("a canned cycle with no feed rate; set the tool controller's feed rates")
        x = params.get("X", self.x)
        y = params.get("Y", self.y)
        if x is None or y is None:
            raise self._refuse("a canned cycle whose X and Y are not known")
        if self.z is None:
            raise self._refuse("a canned cycle whose start Z is not known (no move set Z before it)")
        if bottom >= r:
            raise self._refuse("the hole bottom Z %s is not below the retract plane R %s" % (bottom, r))
        repeats = params.get("L", _ONE)
        if repeats != repeats.to_integral_value() or repeats < 1:
            raise self._refuse("repeat count L %s is not a whole number of at least 1" % repeats)
        if q is not None:
            if q <= 0:
                raise self._refuse("peck depth Q %s must be greater than 0" % q)
            pecks = int(((r - bottom) / q).to_integral_value(rounding=ROUND_CEILING))
            if pecks > MAX_PECKS_PER_HOLE:
                raise self._refuse(
                    "peck depth Q %s makes %d pecks per hole (more than %d); use a larger peck depth"
                    % (q, pecks, MAX_PECKS_PER_HOLE)
                )
        if p is not None and p < 0:
            raise self._refuse("dwell P %s is negative" % p)

        details = ["bottom Z%s" % mm(bottom + self.zshift), "R%s" % mm(r + self.zshift)]
        if q is not None:
            details.append("Q%s" % mm(q))
        if p is not None:
            details.append("P%s" % mm(p))
        if repeats != 1:
            details.append("L%d" % int(repeats))
        details.append(self.retract)
        note = "FreeCAD %s cycle at X%s Y%s (%s) expanded into moves by the RS274/NGC rules" % (
            code,
            mm(x),
            mm(y),
            ", ".join(details),
        )
        if q is not None:
            note += "; pecks back off %s mm" % mm(PECK_BACKOFF_MM)
        self.lines.append(plain_comment("Note", note))

        for _ in range(int(repeats)):
            clear = r if self.retract == "G99" else max(self.z, r)
            # Preliminary motion: up to R if below it, across to X, Y, down to R.
            if self.z < r:
                self._move("G0", {"Z": r})
            if self.x != x or self.y != y:
                self._move("G0", {"X": x, "Y": y})
            if self.z != r:
                self._move("G0", {"Z": r})
            if q is None:
                self._move("G1", {"Z": bottom}, feed=True)
                if code == "G82":
                    self.lines.append("G4 P%s" % mm(p))  # type: ignore[arg-type]
                if code == "G85":
                    self._move("G1", {"Z": r}, feed=True)
            else:
                self._pecks(code, bottom, r, q)
            if self.z != clear:
                self._move("G0", {"Z": clear})

    def _pecks(self, code: str, bottom: Decimal, r: Decimal, q: Decimal) -> None:
        depth = r
        first = True
        while depth > bottom:
            target = max(depth - q, bottom)
            if not first:
                resume = min(depth + PECK_BACKOFF_MM, r)
                if self.z != resume:
                    self._move("G0", {"Z": resume})
            self._move("G1", {"Z": target}, feed=True)
            first = False
            depth = target
            if depth > bottom:
                # G83 retracts to R between pecks; G73 only backs off to break the chip.
                self._move("G0", {"Z": r if code == "G83" else min(depth + PECK_BACKOFF_MM, r)})


def _round(value: Decimal) -> Decimal:
    return value.quantize(_MM, rounding=ROUND_HALF_EVEN)
