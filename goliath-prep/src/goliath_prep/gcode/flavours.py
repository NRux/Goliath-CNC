# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""What each input flavour accepts, the GI header model, and the GI header against job.toml (unit P02).

Three flavours are read (the project design, section 4.2):

* ``gi`` - GI, the Goliath-intermediate G-code our posts write
  (docs/prep/gi-format.md): millimetres, absolute, XY arcs, plus annotations;
* ``generic`` - plain millimetre, absolute G-code from other CAM programs, with
  the job's job.toml; the same vocabulary as GI without annotations;
* ``dialect`` - the robot's own dialect (a vendor ``.NC`` file, or the upload
  bytes goliath-prep emitted, parsed back for certification), **verify only**:
  ``C``, ``M10``, ``M05``, ``M11`` and ``M06`` are read for verification and
  never re-emitted, and ``C`` on a feed line is refused (``D-CSTEP``, E-B4:
  what the firmware does with it is unknown, U-21).

``auto`` chooses ``gi`` when any line carries an annotation-like comment and
``generic`` otherwise. It never chooses the dialect: verify mode is asked for
explicitly, so a robot program cannot be mistaken for CAM output.

The vocabulary tables say, for every G and M number and every letter, what the
reader does with it and, when it refuses, which finding it reports and why
(gi-format 8.4 and 8.5). Anything not in a table is refused (``D-UNSUPPORTED``).

**The GI header against job.toml** (gi-format 10, red-team PHYS-12).
:func:`compare_with_job` compares every value both declare. Numbers agree when
they differ by less than 0.0005, compared as exact decimals (the GI value as
written; the job value is ``Decimal(repr(value))``, which is the decimal as
written because job.toml numbers have at most three decimals). Each
disagreement is a ``D-GI-CONFLICT`` refusal at the GI header line, naming both
sources and both values. Agreeing values are bound with the job.toml value and
a source that records that GI agreed; a value only one side declares is bound
from that side.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from types import MappingProxyType
from typing import Any, Literal

from goliath_prep.core import ids
from goliath_prep.core.params import Param

__all__ = [
    "FLAVOURS",
    "GI_VERSION",
    "HEADER_KEYWORDS",
    "REQUIRED_HEADER",
    "AGREEMENT_TOLERANCE",
    "Flavour",
    "GIHeader",
    "GIOp",
    "GISource",
    "GIStock",
    "GITool",
    "GITower2",
    "GIUnits",
    "GIZShift",
    "Unbindable",
    "Vocabulary",
    "WordRule",
    "compare_with_job",
    "vocabulary",
]

Flavour = Literal["auto", "gi", "generic", "dialect"]
FLAVOURS: tuple[str, ...] = ("auto", "gi", "generic", "dialect")
GI_VERSION = 1
"""The one GI version this reader knows (gi-format 5)."""

HEADER_KEYWORDS: tuple[str, ...] = ("SOURCE", "UNITS", "STOCK", "TOWER2", "TOOL", "ZSHIFT")
REQUIRED_HEADER: tuple[str, ...] = ("SOURCE", "UNITS", "STOCK", "TOWER2", "TOOL")
AGREEMENT_TOLERANCE = Decimal("0.0005")
"""GI and job.toml numbers agree when they differ by less than this (mm, or rpm; gi-format 10)."""


# ---------------------------------------------------------------------------
# Vocabularies (gi-format 8.4 and 8.5; design 4.2)
# ---------------------------------------------------------------------------

Action = Literal[
    "motion", "noop", "units_mm", "tool_length", "dwell", "spindle_on", "spindle_off", "tool_change", "coolant",
    "end", "dust", "refuse",
]


@dataclass(frozen=True)
class WordRule:
    """What the reader does with a word: an action, or a refusal with its finding id and the reason, in workshop
    words."""

    action: Action
    check_id: str | None = None
    why: str | None = None

    def __post_init__(self) -> None:
        if (self.action == "refuse") != (self.check_id is not None):
            raise ValueError("a refusing rule names its finding id, and only a refusing rule does")
        if self.check_id is not None and self.check_id not in ids.SEVERITY:
            raise ValueError("finding id %r is not in the check catalogue" % self.check_id)


def _refuse(check_id: str, why: str) -> WordRule:
    return WordRule("refuse", check_id, why)


@dataclass(frozen=True)
class Vocabulary:
    """The words one flavour accepts: G numbers, M numbers and letters, each with its rule."""

    name: str
    g: Mapping[int, WordRule]
    m: Mapping[int, WordRule]
    letters: Mapping[str, WordRule]
    other_g: WordRule
    other_m: WordRule
    other_letter: WordRule
    motion_letters: frozenset[str] = field(default=frozenset("XYZIJ"))

    def rule_g(self, number: int) -> WordRule:
        return self.g.get(number, self.other_g)

    def rule_m(self, number: int) -> WordRule:
        return self.m.get(number, self.other_m)

    def rule_letter(self, letter: str) -> WordRule:
        return self.letters.get(letter, self.other_letter)


_MOTION = WordRule("motion")
_NOOP = WordRule("noop")
_G_COMMON: dict[int, WordRule] = {
    0: _MOTION, 1: _MOTION, 2: _MOTION, 3: _MOTION,
    17: _NOOP, 90: _NOOP, 94: _NOOP, 54: _NOOP, 40: _NOOP,
    21: WordRule("units_mm"),
    43: WordRule("tool_length"),
    4: WordRule("dwell"),
    20: _refuse(ids.D_UNITS, "G20 (inches): the input is millimetres only; set your CAM to millimetres"),
    91: _refuse(ids.D_UNSUPPORTED, "G91 (incremental): absolute coordinates only"),
    18: _refuse(ids.D_UNSUPPORTED, "G18: the XY plane only (absolute, I/J centre, XY plane)"),
    19: _refuse(ids.D_UNSUPPORTED, "G19: the XY plane only (absolute, I/J centre, XY plane)"),
    41: _refuse(ids.D_UNSUPPORTED, "G41: cutter compensation belongs in the CAM; turn it off in the post"),
    42: _refuse(ids.D_UNSUPPORTED, "G42: cutter compensation belongs in the CAM; turn it off in the post"),
    28: _refuse(ids.D_UNSUPPORTED, "G28: no homing moves; the origin is the tower-1 corner"),
    30: _refuse(ids.D_UNSUPPORTED, "G30: no homing moves; the origin is the tower-1 corner"),
    53: _refuse(ids.D_UNSUPPORTED, "G53: no machine coordinates; the origin is the tower-1 corner"),
    92: _refuse(ids.D_UNSUPPORTED, "G92: no coordinate offsets; the origin is the tower-1 corner"),
}
for _n in range(55, 60):
    _G_COMMON[_n] = _refuse(ids.D_UNSUPPORTED, "G%d: G54 is the only work offset (a no-op); the origin is the "
                                               "tower-1 corner" % _n)
for _n in range(80, 90):
    _G_COMMON[_n] = _refuse(ids.D_UNSUPPORTED, "G%d: canned cycles are expanded by the CAM or the post" % _n)

_COOLANT = WordRule("coolant")
_END = WordRule("end")
_M_COMMON: dict[int, WordRule] = {
    5: WordRule("spindle_off"),
    7: _COOLANT, 8: _COOLANT, 9: _COOLANT,
    2: _END, 30: _END,
    0: _refuse(ids.D_UNSUPPORTED, "M0 (pause): nothing can resume a paused program from the host on firmware "
                                  "01.02.00 (spec 09)"),
    1: _refuse(ids.D_UNSUPPORTED, "M1 (optional pause): nothing can resume a paused program from the host on "
                                  "firmware 01.02.00 (spec 09)"),
    4: _refuse(ids.D_UNSUPPORTED, "M4 (counter-clockwise spindle): the spindle turns one way"),
}
_LETTERS_COMMON: dict[str, WordRule] = {
    **{letter: _MOTION for letter in "XYZIJ"},
    "F": WordRule("noop"), "S": WordRule("noop"), "N": WordRule("noop"),
    "H": WordRule("noop"), "P": WordRule("noop"), "G": WordRule("noop"), "M": WordRule("noop"),
    "A": _refuse(ids.D_UNSUPPORTED, "A: no rotary axes"),
    "B": _refuse(ids.D_UNSUPPORTED, "B: no rotary axes"),
    "K": _refuse(ids.D_ARC, "K: arcs lie in the XY plane with an I/J centre"),
    "R": _refuse(ids.D_ARC, "R-form arc: the arc centre must be given with I and J"),
}
_OTHER_G = _refuse(ids.D_UNSUPPORTED, "outside the accepted vocabulary")
_OTHER_M = _refuse(ids.D_UNSUPPORTED, "outside the accepted vocabulary")
_OTHER_LETTER = _refuse(ids.D_UNSUPPORTED, "outside the accepted vocabulary")

_GI_M = {
    **_M_COMMON,
    3: WordRule("spindle_on"),
    6: WordRule("tool_change"),
    10: _refuse(ids.D_UNSUPPORTED, "M10 is the robot dialect's spindle on: write M3, goliath-prep translates it "
                                   "(spec 05)"),
    11: _refuse(ids.D_UNSUPPORTED, "M11 (dust discharge) is robot dialect only"),
}
_GI_LETTERS = {
    **_LETTERS_COMMON,
    "T": WordRule("noop"),
    "C": _refuse(ids.D_UNSUPPORTED, "C is the robot heading of the Goliath dialect: goliath-prep chooses it; force "
                                    "a heading with OP c= (a Goliath-dialect program is read in dialect verify "
                                    "mode)"),
}

GI_VOCABULARY = Vocabulary("GI", MappingProxyType(dict(_G_COMMON)), MappingProxyType(_GI_M),
                           MappingProxyType(_GI_LETTERS), _OTHER_G, _OTHER_M, _OTHER_LETTER)
GENERIC_VOCABULARY = Vocabulary("generic G-code", MappingProxyType(dict(_G_COMMON)), MappingProxyType(_GI_M),
                                MappingProxyType(_GI_LETTERS), _OTHER_G, _OTHER_M, _OTHER_LETTER)
DIALECT_VOCABULARY = Vocabulary(
    "Goliath dialect",
    MappingProxyType(dict(_G_COMMON)),
    MappingProxyType({
        **_M_COMMON,
        10: WordRule("spindle_on"),
        6: WordRule("tool_change"),
        11: WordRule("dust"),
        3: _refuse(ids.D_UNSUPPORTED, "M3 is not the robot's spindle word: the Goliath dialect starts the spindle "
                                      "with M10 (spec 05)"),
    }),
    MappingProxyType({
        **_LETTERS_COMMON,
        "C": _MOTION,
        "T": _refuse(ids.D_UNSUPPORTED, "the Goliath dialect has no T word (spec 05: tool changes are M06)"),
    }),
    _OTHER_G, _OTHER_M, _OTHER_LETTER,
    frozenset("XYZIJC"),
)


def vocabulary(flavour: str) -> Vocabulary:
    """The vocabulary of a resolved flavour (``gi``, ``generic`` or ``dialect``)."""
    if flavour == "gi":
        return GI_VOCABULARY
    if flavour == "generic":
        return GENERIC_VOCABULARY
    if flavour == "dialect":
        return DIALECT_VOCABULARY
    raise ValueError("no vocabulary for flavour %r (one of gi, generic, dialect)" % (flavour,))


# ---------------------------------------------------------------------------
# GI annotations: value types and the keys of each keyword (gi-format 4.2, 6, 7)
# ---------------------------------------------------------------------------

ValueType = Literal["number", "length", "positive", "integer", "enum", "token", "text"]


@dataclass(frozen=True)
class KeySpec:
    type: ValueType
    required: bool
    choices: tuple[str, ...] = ()
    what: str = ""


@dataclass(frozen=True)
class KeywordSpec:
    keys: Mapping[str, KeySpec]
    positional: KeySpec | None = None


def _k(type_: ValueType, required: bool, what: str, choices: tuple[str, ...] = ()) -> KeySpec:
    return KeySpec(type_, required, choices, what)


TOOL_KINDS = ("flat", "ball", "bull", "vbit", "other")
OP_KINDS = ("profile", "pocket", "drill", "engrave", "adaptive", "other")

KEYWORDS: Mapping[str, KeywordSpec] = MappingProxyType({
    "SOURCE": KeywordSpec({"cam": _k("text", True, "the CAM program and version"),
                           "post": _k("text", True, "the post-processor and version")}),
    "UNITS": KeywordSpec({}, _k("enum", True, "the unit of every length", ("mm",))),
    "STOCK": KeywordSpec({"x": _k("length", True, "stock size along X"),
                          "y": _k("length", True, "stock size along Y"),
                          "t": _k("length", True, "stock thickness"),
                          "origin": _k("enum", True, "where X = 0, Y = 0 is", ("t1-corner",)),
                          "zzero": _k("enum", True, "where Z = 0 is", ("top", "bottom"))}),
    "TOWER2": KeywordSpec({"axis": _k("enum", True, "the axis tower 2 stands on", ("X", "Y"))}),
    "TOOL": KeywordSpec({"n": _k("integer", True, "tool number"),
                         "d": _k("length", True, "cutting diameter"),
                         "flute": _k("length", True, "flute length"),
                         "stickout": _k("length", False, "stick-out"),
                         "kind": _k("enum", False, "tool shape", TOOL_KINDS),
                         "rpm": _k("positive", False, "spindle speed"),
                         "name": _k("text", False, "the CAM's tool name")}),
    "ZSHIFT": KeywordSpec({}, _k("number", True, "the Z shift the post has already applied")),
    "OP": KeywordSpec({"id": _k("token", True, "operation id"),
                       "kind": _k("enum", True, "operation kind", OP_KINDS),
                       "through": _k("enum", True, "whether it cuts through", ("auto", "1", "0")),
                       "tool": _k("integer", True, "the operation's tool"),
                       "label": _k("text", False, "the CAM's operation name"),
                       "c": _k("integer", False, "forced heading in C units")}),
    "OPEND": KeywordSpec({"id": _k("token", True, "the id of the open OP")}),
})

# ---------------------------------------------------------------------------
# The header model (docs/prep/gi.schema.json)
# ---------------------------------------------------------------------------


def _line(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("%s: a GI line number is an int >= 1, got %r" % (what, value))
    return value


def _decimal(value: object, what: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError("%s must be a finite Decimal (the number as written), got %r" % (what, value))
    if positive and not value > 0:
        raise ValueError("%s must be greater than zero, got %s" % (what, value))
    return value


def _number_json(value: Decimal) -> int | float:
    return float(value)


@dataclass(frozen=True)
class GISource:
    cam: str
    post: str
    line: int

    def __post_init__(self) -> None:
        _line(self.line, "GISource")

    def to_json(self) -> dict[str, Any]:
        return {"cam": self.cam, "post": self.post, "line": self.line}


@dataclass(frozen=True)
class GIUnits:
    value: Literal["mm"]
    line: int

    def __post_init__(self) -> None:
        _line(self.line, "GIUnits")
        if self.value != "mm":
            raise ValueError("GI 1 knows millimetres only, got %r" % (self.value,))

    def to_json(self) -> dict[str, Any]:
        return {"value": self.value, "line": self.line}


@dataclass(frozen=True)
class GIStock:
    """``STOCK``: the panel the program was written for, values exactly as written."""

    x: Decimal
    y: Decimal
    t: Decimal
    origin: Literal["t1-corner"]
    zzero: Literal["top", "bottom"]
    line: int

    def __post_init__(self) -> None:
        for name in ("x", "y", "t"):
            _decimal(getattr(self, name), "GIStock %s" % name, positive=True)
        if self.origin != "t1-corner":
            raise ValueError("GIStock origin is t1-corner, got %r" % (self.origin,))
        if self.zzero not in ("top", "bottom"):
            raise ValueError("GIStock zzero is top or bottom, got %r" % (self.zzero,))
        _line(self.line, "GIStock")

    @property
    def top(self) -> Decimal:
        """The Z of the stock top in the file's frame: 0 with ``zzero=top``, ``t`` with ``zzero=bottom``."""
        return Decimal(0) if self.zzero == "top" else self.t

    def to_json(self) -> dict[str, Any]:
        return {"x": _number_json(self.x), "y": _number_json(self.y), "t": _number_json(self.t),
                "origin": self.origin, "zzero": self.zzero, "line": self.line}


@dataclass(frozen=True)
class GITower2:
    axis: Literal["X", "Y"]
    line: int

    def __post_init__(self) -> None:
        if self.axis not in ("X", "Y"):
            raise ValueError("GITower2 axis is X or Y, got %r" % (self.axis,))
        _line(self.line, "GITower2")

    def to_json(self) -> dict[str, Any]:
        return {"axis": self.axis, "line": self.line}


@dataclass(frozen=True)
class GITool:
    """``TOOL``: the one tool. Optional values are None when the line omits them, never defaulted."""

    n: int
    d: Decimal
    flute: Decimal
    stickout: Decimal | None
    kind: str | None
    rpm: Decimal | None
    name: str | None
    line: int

    def __post_init__(self) -> None:
        if isinstance(self.n, bool) or not isinstance(self.n, int) or self.n < 0:
            raise ValueError("GITool n is an int >= 0, got %r" % (self.n,))
        _decimal(self.d, "GITool d", positive=True)
        _decimal(self.flute, "GITool flute", positive=True)
        if self.stickout is not None:
            _decimal(self.stickout, "GITool stickout", positive=True)
        if self.rpm is not None:
            _decimal(self.rpm, "GITool rpm", positive=True)
        if self.kind is not None and self.kind not in TOOL_KINDS:
            raise ValueError("GITool kind is one of %s, got %r" % (TOOL_KINDS, self.kind))
        _line(self.line, "GITool")

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"n": self.n, "d": _number_json(self.d), "flute": _number_json(self.flute)}
        if self.stickout is not None:
            out["stickout"] = _number_json(self.stickout)
        if self.kind is not None:
            out["kind"] = self.kind
        if self.rpm is not None:
            out["rpm"] = _number_json(self.rpm)
        if self.name is not None:
            out["name"] = self.name
        out["line"] = self.line
        return out


@dataclass(frozen=True)
class GIZShift:
    """``ZSHIFT``: the Z shift the post has **already applied**; recorded, never applied (red-team PHYS-12)."""

    value: Decimal
    line: int
    applied: Literal[False] = False

    def __post_init__(self) -> None:
        _decimal(self.value, "GIZShift value")
        _line(self.line, "GIZShift")
        if self.applied is not False:
            raise ValueError("ZSHIFT is already applied by the post and never applied by the reader "
                             "(gi-format 6, red-team PHYS-12): applied must be False")

    def to_json(self) -> dict[str, Any]:
        return {"value": _number_json(self.value), "applied": False, "line": self.line}


@dataclass(frozen=True)
class GIOp:
    """One ``OP`` ... ``OPEND`` block. ``c`` is the forced heading as written (0 to 360, 360 = 0)."""

    id: str
    kind: str
    through: Literal["auto"] | bool
    tool: int
    label: str | None
    c: int | None
    line: int
    end_line: int

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("GIOp id must be a non-empty token, got %r" % (self.id,))
        if self.kind not in OP_KINDS:
            raise ValueError("GIOp kind is one of %s, got %r" % (OP_KINDS, self.kind))
        if self.through not in ("auto", True, False) or (self.through != "auto" and not isinstance(self.through,
                                                                                                    bool)):
            raise ValueError("GIOp through is 'auto', True or False, got %r" % (self.through,))
        if self.c is not None and (isinstance(self.c, bool) or not isinstance(self.c, int)
                                   or not 0 <= self.c <= 360 or self.c % 10):
            raise ValueError("GIOp c is 0, 10, ..., 360, got %r" % (self.c,))
        _line(self.line, "GIOp")
        _line(self.end_line, "GIOp end_line")
        if self.end_line <= self.line:
            raise ValueError("GIOp ends after it starts: OP line %d, OPEND line %d" % (self.line, self.end_line))

    @property
    def forced_c(self) -> int | None:
        """The forced heading in C units, 0 to 350: ``c=360`` is the same heading as 0 (spec 08)."""
        return None if self.c is None else self.c % 360

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "kind": self.kind, "through": self.through, "tool": self.tool}
        if self.label is not None:
            out["label"] = self.label
        if self.c is not None:
            out["c"] = self.c
        out["line"] = self.line
        out["end_line"] = self.end_line
        return out


@dataclass(frozen=True)
class GIHeader:
    """A GI file's header as read: the model of docs/prep/gi.schema.json, every value with its GI line.

    Built only when every required header line was read without error; a
    refused header gives no model (the findings say why).
    """

    version: int
    source: GISource
    units: GIUnits
    stock: GIStock
    tower2: GITower2
    tool: GITool
    zshift: GIZShift | None
    ops: tuple[GIOp, ...]

    def __post_init__(self) -> None:
        if self.version != GI_VERSION:
            raise ValueError("this reader knows GI version %d only, got %r" % (GI_VERSION, self.version))
        for name, kind in (("source", GISource), ("units", GIUnits), ("stock", GIStock), ("tower2", GITower2),
                           ("tool", GITool)):
            if not isinstance(getattr(self, name), kind):
                raise TypeError("GIHeader %s must be a %s" % (name, kind.__name__))
        if self.zshift is not None and not isinstance(self.zshift, GIZShift):
            raise TypeError("GIHeader zshift must be a GIZShift or None")
        if not isinstance(self.ops, tuple) or not all(isinstance(op, GIOp) for op in self.ops):
            raise TypeError("GIHeader ops must be a tuple of GIOp")

    def to_json(self) -> dict[str, Any]:
        """The header model of docs/prep/gi.schema.json (numbers as JSON numbers; exact values stay in the fields)."""
        out: dict[str, Any] = {
            "version": self.version,
            "source": self.source.to_json(),
            "units": self.units.to_json(),
            "stock": self.stock.to_json(),
            "tower2": self.tower2.to_json(),
            "tool": self.tool.to_json(),
            "ops": [op.to_json() for op in self.ops],
        }
        if self.zshift is not None:
            out["zshift"] = self.zshift.to_json()
        return out


# ---------------------------------------------------------------------------
# The header against job.toml (gi-format 10; red-team PHYS-12)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Conflict:
    """One disagreement: the GI line, the GI side and the job.toml side, each as (name, value as written)."""

    line: int
    what: str
    gi_name: str
    gi_value: str
    job_name: str
    job_value: str
    unit: str
    advice: str


@dataclass(frozen=True)
class Unbindable:
    """A number only the GI header declares that the certificate cannot record exactly (finer than 0.001).

    gi-format 10 binds such a value from GI; the certificate records every
    bound value in canonical JSON, which keeps 0.001 (F04). Binding a finer
    value would show another number than the one used, and rounding it would
    change it silently, so the reader refuses it (``D-GI-HEADER``) instead.
    """

    line: int
    gi_name: str
    gi_value: str
    job_name: str
    unit: str


_RECORDABLE = Fraction(1, 1000)
"""The finest step canonical JSON records (goliath_prep.core.canon, F04)."""


def _recordable(value: Decimal) -> bool:
    return (Fraction(value) / _RECORDABLE).denominator == 1


def _job_decimal(param: Param[Any]) -> Decimal:
    """A job.toml number as the decimal written there (job numbers have at most three decimals: P01)."""
    return Decimal(repr(param.value))


def _differ(gi_value: Decimal, job_value: Decimal) -> bool:
    return abs(Fraction(gi_value) - Fraction(job_value)) >= Fraction(AGREEMENT_TOLERANCE)


def _job_text(param: Param[Any]) -> str:
    value = param.value
    return value if isinstance(value, str) else repr(value)


def _agreed(param: Param[Any], gi_ref: str) -> Param[Any]:
    """The job.toml value, bound with a source that records the GI value that agreed with it."""
    return Param(param.value, param.unit, param.source, "%s = %s" % (param.ref, gi_ref),
                 disclosures=param.disclosures, inputs=param.inputs)


def compare_with_job(*, stock_parts: Mapping[str, Any], stock_line: int | None, tower2: GITower2 | None,
                     tool_parts: Mapping[str, Any], tool_line: int | None,
                     job: Any) -> tuple[list[Conflict], dict[str, Param[Any]], list[Unbindable]]:
    """Compare what the GI header declares with job.toml; return the conflicts, the bound values, the unbindable.

    ``stock_parts`` and ``tool_parts`` hold the values of the ``STOCK`` and
    ``TOOL`` lines that were read without error (a line with an error in one
    key still has its other keys compared). Each value both sides declare and
    that disagrees is one :class:`Conflict`. A number only GI declares is bound
    from GI when the certificate can record it exactly, and is otherwise one
    :class:`Unbindable` (never bound, never rounded).
    """
    conflicts: list[Conflict] = []
    unbindable: list[Unbindable] = []
    bound: dict[str, Param[Any]] = dict(_job_bound(job))

    def number(name: str, gi_key: str, keyword: str, line: int | None, value: Decimal | None,
               param: Param[Any] | None, what: str, unit: str, advice: str, job_key: str) -> None:
        if value is None or line is None:
            return
        gi_ref = "GI %s %s (line %d)" % (keyword, gi_key, line)
        if param is None:
            if _recordable(value):
                bound[name] = Param(float(value), unit, "OWNER_JOB", gi_ref)
            else:
                unbindable.append(Unbindable(line, "GI %s %s" % (keyword, gi_key), _decimal_text(value),
                                             "job.toml %s" % job_key, unit))
            return
        if _differ(value, _job_decimal(param)):
            conflicts.append(Conflict(line, what, "GI %s %s" % (keyword, gi_key), _decimal_text(value),
                                      param.ref, _job_text(param), unit, advice))
            bound.pop(name, None)
        else:
            bound[name] = _agreed(param, gi_ref)

    def choice(name: str, gi_key: str, keyword: str, line: int | None, value: str | None, param: Param[Any] | None,
               what: str, advice: str) -> None:
        if value is None or line is None:
            return
        gi_ref = "GI %s %s (line %d)" % (keyword, gi_key, line)
        if param is None:
            bound[name] = Param(value, "enum", "OWNER_JOB", gi_ref)
            return
        if value != param.value:
            conflicts.append(Conflict(line, what, "GI %s %s" % (keyword, gi_key), value, param.ref,
                                      _job_text(param), "", advice))
            bound.pop(name, None)
        else:
            bound[name] = _agreed(param, gi_ref)

    panel_advice = "Make the CAM stock and job.toml describe the same panel."
    number("panel_width", "x", "STOCK", stock_line, stock_parts.get("x"), job.panel.W, "the panel's size along X",
           "mm", panel_advice, "panel.W")
    number("panel_height", "y", "STOCK", stock_line, stock_parts.get("y"), job.panel.H, "the panel's size along Y",
           "mm", panel_advice, "panel.H")
    number("stock_thickness", "t", "STOCK", stock_line, stock_parts.get("t"), job.panel.T,
           "the panel thickness (job.toml: measured with callipers)", "mm", panel_advice, "panel.T")
    choice("z_zero", "zzero", "STOCK", stock_line, stock_parts.get("zzero"), job.z_zero, "where Z = 0 is",
           "Make the CAM's Z zero and job.toml's z_zero the same.")
    origin = stock_parts.get("origin")
    if origin is not None and stock_line is not None:
        dx, dy = job.placement.dx, job.placement.dy
        gi_ref = "GI STOCK origin (line %d)" % stock_line
        if _job_decimal(dx) != 0 or _job_decimal(dy) != 0:
            conflicts.append(Conflict(
                stock_line, "the program's placement on the panel", "GI STOCK origin",
                "%s (X = 0, Y = 0 at the panel's tower-1 corner, no offset)" % origin, "job.toml placement",
                "dx = %s mm, dy = %s mm" % (_job_text(dx), _job_text(dy)), "",
                "origin=t1-corner agrees only with no placement offset: remove [placement] from job.toml, or "
                "move the origin in the CAM."))
            bound.pop("placement_dx", None)
            bound.pop("placement_dy", None)
        else:
            bound["placement_dx"] = _agreed(dx, gi_ref)
            bound["placement_dy"] = _agreed(dy, gi_ref)
    if tower2 is not None:
        choice("tower2_axis", "axis", "TOWER2", tower2.line, tower2.axis, job.panel.tower2,
               "the axis tower 2 stands on", "Make the CAM post's tower-2 axis and job.toml's panel.tower2 the same.")
    tool_advice = "Make the CAM tool and job.toml's [tool] describe the bit in the router."
    tool = job.tool
    number("tool_diameter", "d", "TOOL", tool_line, tool_parts.get("d"), tool.d, "the tool diameter", "mm",
           tool_advice, "tool.d")
    number("tool_flute_length", "flute", "TOOL", tool_line, tool_parts.get("flute"), tool.flute,
           "the flute length", "mm", tool_advice, "tool.flute")
    number("tool_stickout", "stickout", "TOOL", tool_line, tool_parts.get("stickout"), tool.stickout,
           "the stick-out", "mm", tool_advice, "tool.stickout")
    number("tool_rpm", "rpm", "TOOL", tool_line, tool_parts.get("rpm"), tool.rpm, "the spindle speed", "rpm",
           tool_advice, "tool.rpm")
    choice("tool_kind", "kind", "TOOL", tool_line, tool_parts.get("kind"), tool.kind, "the tool kind", tool_advice)
    return conflicts, bound, unbindable


def _decimal_text(value: Decimal) -> str:
    return str(value) if value.adjusted() < 30 else "%s (as written)" % format(value, "f")[:40]


def _job_bound(job: Any) -> dict[str, Param[Any]]:
    """The values of job.toml the header is compared with, by catalogue name, as job.toml states them."""
    params = job.params()
    names = ("panel_width", "panel_height", "stock_thickness", "tower2_axis", "placement_dx", "placement_dy",
             "z_zero", "tool_diameter", "tool_flute_length", "tool_stickout", "tool_rpm", "tool_kind")
    return {name: params[name] for name in names if name in params}


def job_bound(job: Any) -> dict[str, Param[Any]]:
    """The bound values of a job read without a GI header: job.toml's own (generic G-code, dialect)."""
    return _job_bound(job)
