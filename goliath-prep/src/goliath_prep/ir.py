# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The intermediate representation: the program as cuts and links (unit P03; the project design, section 4.3).

:func:`goliath_prep.normalise.normalise` builds a :class:`Program` from the
raw program the reader read; :func:`goliath_prep.pieces.pieces` cuts its moves
into the time pieces every check runs on. Everything here is an immutable
value; every move, note and event carries the source line it came from, so
every later finding can point at a line of the input file.

**Coordinates.** Millimetres in the panel frame (origin at the tower-1 corner,
safety-model 3.1), Z = 0 at the stock top and cuts negative (safety-model 3.2),
after the placement and Z-zero shifts (each a note). They are
:class:`~decimal.Decimal`: the value as written in the file, shifted exactly,
or a new point the normaliser made (a helix segment end, an arc's split
point), rounded to the 0.001 mm grid the robot's program is written with, so
what the checks see is what can be emitted. ``None`` is a coordinate the
program has not written yet: unknown, never zero (gi-format 8.6). The
``*_f`` accessors give floats for geometry.

**Cuts and links** (safety-model 3.3). A move is *in material* when its lowest
Z is below -``eps_surf`` (0.02 mm), decided exactly on the decimals. A cut runs
from the move that goes below -``eps_surf`` to the move that returns to
z >= 0, both included; every other move belongs to a link. The G word is never
consulted. Each move belongs to exactly one cut or link; cuts and links are
numbered from 0 in program order and alternate.

**Notes.** Every transformation the normaliser makes is a :class:`Note`
(kinds in :data:`NOTE_KINDS`), and so is every reinterpretation of a move; the
reader's own notes travel with them (``stage="reader"``).
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal, get_args

from goliath_prep.core.disclosure import Disclosure
from goliath_prep.core.findings import Finding, SourceRef
from goliath_prep.core.params import Param
from goliath_prep.gcode.flavours import GIOp
from goliath_prep.jobspec import Tool

__all__ = [
    "MOVE_KINDS",
    "MOVE_ORIGINS",
    "NOTE_KINDS",
    "ArcRecord",
    "Cut",
    "Link",
    "Move",
    "MoveKind",
    "MoveOrigin",
    "Note",
    "Program",
    "ProgramEvent",
    "ProgramMeta",
    "ToolUse",
    "XYZ",
]

MoveKind = Literal["traverse", "feed", "arc_cw", "arc_ccw"]
MOVE_KINDS: tuple[str, ...] = get_args(MoveKind)
MoveOrigin = Literal["as-read", "helix-segment", "arc-part"]
MOVE_ORIGINS: tuple[str, ...] = get_args(MoveOrigin)

XYZ = tuple[Decimal | None, Decimal | None, Decimal | None]

NOTE_KINDS: Mapping[str, str] = MappingProxyType({
    "placement-shift": "job.toml [placement] moved every X and Y of the program",
    "z-zero-shift": "job.toml z_zero = \"bottom\": every Z was shifted by -T",
    "dialect-frame": "the Goliath dialect is never shifted: it is in the panel frame with Z = 0 at the stock top",
    "helix": "helical arcs replaced by G01 segments within the helix tolerance",
    "arc-split": "arcs of 180 degrees or more, or nearly collinear, split into parts of at most 90 degrees",
    "full-circle-split": "full circles split into four quarters",
    "rapid-in-cut": "G0 moves whose lowest point is in the material belong to a cut (classified by Z)",
    "feed-link": "feed moves (G1, G2, G3) that stay out of the material belong to a link (classified by Z)",
    "start-unknown": "moves from a position the program has not written yet: links from the unknown start",
    "xy-before-z": "X/Y moves before any Z is written: links from the unknown start, never run as written",
    "cut-open-at-end": "the program ends inside the material: its end closes the last cut",
    "ops-inferred": "no operation blocks: each cut is its own inferred operation",
    "op-without-cut": "an operation block that removes no material",
    "no-cuts": "the program removes no material",
    "reader-notes-absent": "normalised from a bare raw program: the reader's notes are in its ReadResult",
})
"""The kinds of note the normaliser writes, each with what it means (read-only)."""


def _line_no(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("%s: a source line number is an int >= 1, got %r" % (what, value))
    return value


def _index(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("%s must be an int >= 0, got %r" % (what, value))
    return value


def _opt_index(value: object, what: str) -> int | None:
    return None if value is None else _index(value, what)


def _decimal_or_none(value: object, what: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError("%s must be a finite Decimal or None, got %r" % (what, value))
    return value


def _xyz(value: object, what: str) -> XYZ:
    if not isinstance(value, tuple) or len(value) != 3:
        raise TypeError("%s must be a tuple (x, y, z) of Decimal or None, got %r" % (what, value))
    return tuple(_decimal_or_none(v, "%s[%d]" % (what, i)) for i, v in enumerate(value))  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Notes and events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Note:
    """A normalisation note (safety-model 3.7): what was changed or reinterpreted, and on which lines.

    ``lines`` are the source lines it applies to, ascending and each once;
    empty when it applies to the whole program (a shift of every
    coordinate). ``stage`` is ``normalise`` for the normaliser's notes and
    ``reader`` for the reader's, which travel with the program.
    """

    kind: str
    text: str
    lines: tuple[int, ...]
    stage: Literal["reader", "normalise"]

    def __post_init__(self) -> None:
        if self.stage not in ("reader", "normalise"):
            raise ValueError("Note stage is reader or normalise, got %r" % (self.stage,))
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("Note kind must be a non-empty string, got %r" % (self.kind,))
        if self.stage == "normalise" and self.kind not in NOTE_KINDS:
            raise ValueError("unknown normalisation note kind %r (one of %s)" % (self.kind, sorted(NOTE_KINDS)))
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Note text must say what was done, got %r" % (self.text,))
        lines = tuple(_line_no(n, "Note lines") for n in self.lines)
        if list(lines) != sorted(set(lines)):
            raise ValueError("Note lines must be ascending and each once, got %r" % (lines,))
        object.__setattr__(self, "lines", lines)

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "lines": list(self.lines), "stage": self.stage}


@dataclass(frozen=True)
class ProgramEvent:
    """A line's effect that is not motion (spindle, tool, dust, marker, dwell, program end), in program order.

    ``before_move`` is the index of the first move after it in
    :attr:`Program.moves` (the number of moves when nothing follows).
    """

    kind: str
    value: str | None
    src: SourceRef
    before_move: int

    def __post_init__(self) -> None:
        if not isinstance(self.src, SourceRef):
            raise TypeError("ProgramEvent src must be a SourceRef, got %r" % (self.src,))
        _index(self.before_move, "ProgramEvent before_move")


# ---------------------------------------------------------------------------
# Moves, cuts, links
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Move:
    """One straight line or arc of the normalised program.

    ``kind`` is the G word as written (``traverse`` G0, ``feed`` G1,
    ``arc_cw`` G2, ``arc_ccw`` G3); a helix segment is a ``feed``. ``a`` and
    ``b`` are its start and end, ``centre`` an arc's centre, ``sweep_deg`` the
    angle it turns through (an arc part's share of the source arc's sweep)
    and ``radius`` its distance from the centre at the start and at the end
    (they may differ by up to 0.005 mm; the path between them is taken with
    the radius changing linearly with the angle). ``in_material`` is decided
    exactly (see the module documentation). ``c_start``/``c_end`` are the
    dialect heading before and after the line, as written (None outside the
    dialect or before the first ``C``). ``origin`` says whether the move is as
    read or was made by the normaliser, ``part`` is (k, n) for part k of the n
    moves made from the source line (``(1, 1)`` as read), and ``arc`` the
    index of the source arc's :class:`ArcRecord`.
    """

    index: int
    kind: MoveKind
    a: XYZ
    b: XYZ
    centre: tuple[Decimal, Decimal] | None
    sweep_deg: float | None
    radius: tuple[float, float] | None
    in_material: bool
    c_start: Decimal | None
    c_end: Decimal | None
    feed: Decimal | None
    rpm: Decimal | None
    spindle_on: bool
    tool: int | None
    op: str | None
    src: SourceRef
    origin: MoveOrigin
    part: tuple[int, int]
    arc: int | None

    def __post_init__(self) -> None:
        _index(self.index, "Move index")
        if self.kind not in MOVE_KINDS:
            raise ValueError("Move kind is one of %s, got %r" % (MOVE_KINDS, self.kind))
        object.__setattr__(self, "a", _xyz(self.a, "Move a"))
        object.__setattr__(self, "b", _xyz(self.b, "Move b"))
        if self.origin not in MOVE_ORIGINS:
            raise ValueError("Move origin is one of %s, got %r" % (MOVE_ORIGINS, self.origin))
        if not isinstance(self.src, SourceRef):
            raise TypeError("Move src must be a SourceRef, got %r" % (self.src,))
        k, n = self.part
        if not (isinstance(k, int) and isinstance(n, int) and 1 <= k <= n):
            raise ValueError("Move part is (k, n) with 1 <= k <= n, got %r" % (self.part,))
        if self.origin == "as-read" and self.part != (1, 1):
            raise ValueError("a move as read is part (1, 1), got %r" % (self.part,))
        if self.is_arc:
            if self.centre is None or self.sweep_deg is None or self.radius is None:
                raise ValueError("an arc move needs its centre, sweep and radius (line %d)" % self.src.line)
            if not (0.0 < self.sweep_deg <= 360.0):
                raise ValueError("an arc's sweep is in (0, 360] degrees, got %r" % (self.sweep_deg,))
            if not self.located:
                raise ValueError("an arc move needs a known start and end (line %d)" % self.src.line)
        elif self.centre is not None or self.sweep_deg is not None or self.radius is not None:
            raise ValueError("a straight move has no centre, sweep or radius (line %d)" % self.src.line)
        _opt_index(self.arc, "Move arc")

    @property
    def is_arc(self) -> bool:
        return self.kind in ("arc_cw", "arc_ccw")

    @property
    def located(self) -> bool:
        """True when its start and end are fully known."""
        return None not in self.a and None not in self.b

    @property
    def moves_xy(self) -> bool:
        """True when X or Y changes (an arc always does), or when they are unknown at either end."""
        return self.is_arc or self.a[0] != self.b[0] or self.a[1] != self.b[1] or None in self.a[:2] \
            or None in self.b[:2]

    @property
    def c_changes(self) -> bool:
        """True when the line writes a heading other than the one before it (dialect)."""
        return self.c_end is not None and self.c_end != self.c_start

    @property
    def a_f(self) -> tuple[float, float, float]:
        """The start as floats (located moves only)."""
        return _floats(self.a, self)

    @property
    def b_f(self) -> tuple[float, float, float]:
        """The end as floats (located moves only)."""
        return _floats(self.b, self)

    @property
    def centre_f(self) -> tuple[float, float]:
        if self.centre is None:
            raise ValueError("line %d: a straight move has no centre" % self.src.line)
        return float(self.centre[0]), float(self.centre[1])


def _floats(point: XYZ, move: Move) -> tuple[float, float, float]:
    if None in point:
        raise ValueError("line %d: the move's position is not fully known (%s); it has no float form"
                         % (move.src.line, ", ".join("unknown" if v is None else str(v) for v in point)))
    return float(point[0]), float(point[1]), float(point[2])  # type: ignore[arg-type]


def _moves(value: object, what: str) -> tuple[Move, ...]:
    if not isinstance(value, tuple) or not value or not all(isinstance(m, Move) for m in value):
        raise TypeError("%s must be a non-empty tuple of Move" % what)
    for left, right in zip(value, value[1:]):
        if right.index != left.index + 1:
            raise ValueError("%s: moves must be consecutive in program order (%d then %d)"
                             % (what, left.index, right.index))
    return value


@dataclass(frozen=True)
class Cut:
    """A cut (safety-model 3.3): from the move that goes below -eps_surf to the move that returns to z >= 0.

    ``op`` is its GI operation when its moves lie in one ``OP`` block (None
    when the file has none); ``ops`` lists every block its moves lie in
    (more than one is an ``N-OPS-MISMATCH`` warning). ``zmin`` is its lowest Z
    and ``max_depth`` = -zmin, exact; ``through`` is max_depth >= T - 0.05 mm,
    from the geometry only (safety-model 3.4). ``forced_c`` is the forced
    heading of its operation in C units (0 to 350). ``open_at_end`` is True
    when the program ended before the cut returned to z >= 0.
    """

    id: int
    moves: tuple[Move, ...]
    op: GIOp | None
    ops: tuple[str, ...]
    through: bool
    zmin: Decimal
    max_depth: Decimal
    forced_c: int | None
    open_at_end: bool

    def __post_init__(self) -> None:
        _index(self.id, "Cut id")
        _moves(self.moves, "Cut %d" % self.id)
        if not self.moves[0].in_material:
            raise ValueError("cut %d must start with a move in the material" % self.id)
        if not all(m.located for m in self.moves):
            raise ValueError("every move of cut %d must have a known position" % self.id)
        if not isinstance(self.zmin, Decimal) or not isinstance(self.max_depth, Decimal) \
                or self.max_depth != self.zmin.copy_negate():
            raise ValueError("Cut max_depth is -zmin")
        if self.op is not None and not isinstance(self.op, GIOp):
            raise TypeError("Cut op must be a GIOp or None")
        if self.forced_c is not None and (isinstance(self.forced_c, bool) or self.forced_c not in range(0, 360, 10)):
            raise ValueError("Cut forced_c is 0, 10, ..., 350, got %r" % (self.forced_c,))

    @property
    def lines(self) -> tuple[int, int]:
        """The first and last source line of the cut."""
        return self.moves[0].src.line, self.moves[-1].src.line


@dataclass(frozen=True)
class Link:
    """A maximal run of moves that are not in any cut.

    ``after_cut`` is the cut it follows and ``before_cut`` the cut it leads
    to (None at the start or the end of the program). ``located`` is False
    when it holds moves from the unknown start (they have no time pieces).
    """

    id: int
    moves: tuple[Move, ...]
    after_cut: int | None
    before_cut: int | None

    def __post_init__(self) -> None:
        _index(self.id, "Link id")
        _moves(self.moves, "Link %d" % self.id)
        _opt_index(self.after_cut, "Link after_cut")
        _opt_index(self.before_cut, "Link before_cut")

    @property
    def located(self) -> bool:
        return all(m.located for m in self.moves)


# ---------------------------------------------------------------------------
# Arcs, tools, the program
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArcRecord:
    """One arc of the source, for the report (red-team PHYS-16): its sweep and what was done with it.

    ``sweep_deg`` is the reader's (360 for a full circle: end equal to start
    at file precision). ``treatment`` is ``kept`` (one arc move), ``split``
    (arc parts of at most 90 degrees) or ``linearised`` (a helix, G01
    segments); ``moves`` are the indices of the moves made from it.
    """

    index: int
    src: SourceRef
    direction: Literal["cw", "ccw"]
    sweep_deg: float
    full_circle: bool
    helical: bool
    centre: tuple[Decimal, Decimal]
    radius_start: float
    radius_end: float
    treatment: Literal["kept", "split", "linearised"]
    moves: tuple[int, ...]
    cut: int | None

    def __post_init__(self) -> None:
        _index(self.index, "ArcRecord index")
        if self.direction not in ("cw", "ccw"):
            raise ValueError("ArcRecord direction is cw or ccw, got %r" % (self.direction,))
        if self.treatment not in ("kept", "split", "linearised"):
            raise ValueError("ArcRecord treatment is kept, split or linearised, got %r" % (self.treatment,))
        if not self.moves:
            raise ValueError("an arc record names the moves made from it")
        if self.full_circle != (self.sweep_deg == 360.0):
            raise ValueError("a full circle sweeps 360 degrees, and only a full circle does")

    @property
    def parts(self) -> int:
        return len(self.moves)

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "line": self.src.line,
            "src": self.src.to_json(),
            "direction": self.direction,
            "sweep_deg": self.sweep_deg,
            "full_circle": self.full_circle,
            "helical": self.helical,
            "centre": [str(self.centre[0]), str(self.centre[1])],
            "radius_start": self.radius_start,
            "radius_end": self.radius_end,
            "treatment": self.treatment,
            "parts": self.parts,
            "cut": self.cut,
        }


@dataclass(frozen=True)
class ToolUse:
    """The program's one tool: its number in the file (None when the file names none) and job.toml's tool."""

    number: int | None
    tool: Tool
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.tool, Tool):
            raise TypeError("ToolUse tool must be a jobspec.Tool, got %r" % (self.tool,))


@dataclass(frozen=True)
class ProgramMeta:
    """Where the program came from and every value the normaliser used, each with its source."""

    file: str
    flavour: Literal["gi", "generic", "dialect"]
    line_count: int
    end_line: int | None
    units: Param[str]
    z_zero: Param[str]
    z_shift_mm: Decimal
    placement_dx: Param[float]
    placement_dy: Param[float]
    stock_thickness: Param[float]
    eps_surf: Param[float]
    through_tol: Param[float]
    helix_tol: Param[float]
    ops_source: Literal["blocks", "inferred"]
    reader_notes: bool

    def params(self) -> dict[str, Param[Any]]:
        return {
            "units": self.units,
            "z_zero": self.z_zero,
            "placement_dx": self.placement_dx,
            "placement_dy": self.placement_dy,
            "stock_thickness": self.stock_thickness,
            "eps_surf": self.eps_surf,
            "through_tol": self.through_tol,
            "helix_tol": self.helix_tol,
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "flavour": self.flavour,
            "line_count": self.line_count,
            "end_line": self.end_line,
            "z_shift_mm": str(self.z_shift_mm),
            "ops_source": self.ops_source,
            "reader_notes": self.reader_notes,
            "params": {name: param.to_json() for name, param in self.params().items()},
        }


@dataclass(frozen=True)
class Program:
    """The normalised program: cuts and links in program order, and everything about how they were made.

    ``findings`` holds what normalisation warns about (``N-OPS-MISMATCH``);
    it never holds a refusal: a program that cannot be normalised is not a
    Program (:class:`goliath_prep.normalise.NormaliseError`). ``disclosures``
    are those of the values the normalisation relied on and the reader's;
    they must be raised wherever the program is shown.
    """

    cuts: tuple[Cut, ...]
    links: tuple[Link, ...]
    tools: tuple[ToolUse, ...]
    meta: ProgramMeta
    notes: tuple[Note, ...]
    arcs: tuple[ArcRecord, ...] = ()
    events: tuple[ProgramEvent, ...] = ()
    findings: tuple[Finding, ...] = ()
    disclosures: tuple[Disclosure, ...] = ()
    _moves: tuple[Move, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name, kind in (("cuts", Cut), ("links", Link), ("tools", ToolUse), ("notes", Note),
                           ("arcs", ArcRecord), ("events", ProgramEvent), ("findings", Finding),
                           ("disclosures", Disclosure)):
            items = getattr(self, name)
            if not isinstance(items, tuple) or not all(isinstance(i, kind) for i in items):
                raise TypeError("Program %s must be a tuple of %s" % (name, kind.__name__))
        if not isinstance(self.meta, ProgramMeta):
            raise TypeError("Program meta must be a ProgramMeta")
        if [c.id for c in self.cuts] != list(range(len(self.cuts))):
            raise ValueError("cuts are numbered 0, 1, ... in program order")
        if [k.id for k in self.links] != list(range(len(self.links))):
            raise ValueError("links are numbered 0, 1, ... in program order")
        segments = sorted([*self.cuts, *self.links], key=lambda s: s.moves[0].index)
        moves = tuple(m for s in segments for m in s.moves)
        if [m.index for m in moves] != list(range(len(moves))):
            raise ValueError("every move belongs to exactly one cut or link, in program order")
        for left, right in zip(segments, segments[1:]):
            if isinstance(left, Cut) and isinstance(right, Cut) and not left.moves[-1].b[2] >= 0:
                raise ValueError("cut %d is followed by cut %d without returning to z >= 0" % (left.id, right.id))
            if isinstance(left, Link) and isinstance(right, Link):
                raise ValueError("links %d and %d are adjacent: they are one link" % (left.id, right.id))
        for event in self.events:
            if event.before_move > len(moves):
                raise ValueError("an event points past the last move")
        if any(f.severity in ("refuse", "internal-error") for f in self.findings):
            raise ValueError("a Program holds no refusal: a program that cannot be normalised is not a Program")
        object.__setattr__(self, "_moves", moves)

    @property
    def moves(self) -> tuple[Move, ...]:
        """Every move in program order (``moves[i].index == i``)."""
        return self._moves

    def segments(self) -> Iterator[Cut | Link]:
        """Cuts and links in program order."""
        return iter(sorted([*self.cuts, *self.links], key=lambda s: s.moves[0].index))

    def cut_of(self, move: Move) -> int | None:
        """The id of the cut ``move`` belongs to, or None when it belongs to a link."""
        for cut in self.cuts:
            if cut.moves[0].index <= move.index <= cut.moves[-1].index:
                return cut.id
        return None
