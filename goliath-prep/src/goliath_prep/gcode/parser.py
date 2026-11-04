# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The strict G-code reader: :func:`read_gcode` (unit P02; the project design, section 4.2).

A refusing parser for a deliberately small vocabulary. It reads GI (the
Goliath-intermediate G-code of docs/prep/gi-format.md), generic millimetre
G-code with a job.toml, and the Goliath dialect in verify mode, and returns a
:class:`ReadResult`: the raw program (every move and event with its source
line), the GI header model, the findings, the normalisation notes and the
disclosures. It never raises on the content of a file: everything it cannot
accept is a refusal naming the line and, where the catalogue's witness has a
column, the column (the message always says "line N, column C").

**Refused lines are not interpreted.** A line with a refusal changes no modal
state and records no move; later lines may report what follows from that
(gi-format 11 allows it). A refused result must never be planned or uploaded.

**Gates.** The design's interface has no gate argument; the reader needs one
gate, ``drop_dwells`` (``G4``). ``read_gcode`` takes an optional ``gates``
view (:class:`~goliath_prep.core.types.GateView`) and, without one, uses the
all-closed desk view :class:`~goliath_prep.core.types.ClosedGateView` and
raises its disclosure. Only ``allowed("drop_dwells") is True`` drops a ``G4``
(with a disclosure); False or None refuses it (``G-GATE``, naming rung R4).

**Exact numbers.** Every coordinate is the :class:`~decimal.Decimal` written in
the file; comparisons (the end of an arc against its start, the radius
tolerance, the 0.0005 agreement with job.toml) are exact, in
:class:`~fractions.Fraction` arithmetic. Only the reported sweep angle is a
float.

What the reader records, and what is left to later stages:

* ``ZSHIFT`` is recorded and never applied (red-team PHYS-12);
* coordinates not yet written are ``None`` (unknown), never zero;
* the spindle state of a move is the program's (``M3``/``M10`` seen and no
  ``M5`` since), not a claim about the robot;
* in dialect mode the ``C`` values are recorded as written; the grid, range and
  step rules of ``D-CSTEP`` belong to the certification of the parsed program.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, Inexact, localcontext
from fractions import Fraction
from types import MappingProxyType
from typing import Any, Literal

from goliath_prep.core import ids
from goliath_prep.core.disclosure import Disclosure
from goliath_prep.core.findings import Finding, SourceRef, Witness
from goliath_prep.core.params import Param
from goliath_prep.core.types import ClosedGateView, GateView
from goliath_prep.defaults import MissingValueError
from goliath_prep.gcode import flavours as fl
from goliath_prep.gcode.flavours import GIHeader, GIOp, GISource, GIStock, GITool, GITower2, GIUnits, GIZShift
from goliath_prep.gcode.lexer import (
    Annotation,
    AnnotationError,
    AnnotationItem,
    LexedLine,
    Word,
    comment_kind,
    is_annotation_like,
    lex_annotation,
    lex_line,
    split_lines,
    version_line_token,
)
from goliath_prep.jobspec import JobSpec

__all__ = [
    "COORDINATE_LIMIT",
    "ReadNote",
    "ReadResult",
    "RawEvent",
    "RawMove",
    "RawProgram",
    "read_gcode",
]

COORDINATE_LIMIT = Decimal(10000)
"""X, Y, Z, I and J beyond plus or minus this many millimetres are refused (gi-format 8.2)."""
ARC_AMBIGUOUS_SQ = Fraction(1, 10000)
"""0 < |end - start|^2 < 0.01^2 mm^2: the arc is refused as ambiguous (PHYS-16)."""
ARC_RADIUS_TOLERANCE = Fraction(5, 1000)
"""Start and end radii of an arc may differ by at most 0.005 mm (gi-format 8.7)."""
ANNOTATION_NUMBER_LIMIT = Decimal(10) ** 9
"""An annotation number at or beyond this magnitude is outside every domain (no length or speed of a job is near)."""
DWELL_GATE = "drop_dwells"
DWELL_RUNG = "R4"

ResolvedFlavour = Literal["gi", "generic", "dialect"]
Coord = Decimal | None
XYZ = tuple[Coord, Coord, Coord]
MoveKind = Literal["traverse", "feed", "arc_cw", "arc_ccw"]
EventKind = Literal["spindle_on", "spindle_off", "tool_select", "tool_change", "dust", "marker_u", "marker_s",
                    "dwell_dropped", "program_end"]
_KIND: Mapping[int, MoveKind] = MappingProxyType({0: "traverse", 1: "feed", 2: "arc_cw", 3: "arc_ccw"})
_VERSION = re.compile(r"[1-9][0-9]*")
_DIGITS = re.compile(r"[0-9]+")
_NUMBER = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?")
_LONGEST_INT = 18


# ---------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawMove:
    """One motion line as read: where it starts and ends (X, Y, Z as written; None = not yet known).

    ``kind`` follows the G word: ``traverse`` (G0), ``feed`` (G1), ``arc_cw``
    (G2), ``arc_ccw`` (G3); cut or link is decided later, by Z (design 4.4).
    For arcs, ``centre`` is the start plus (I, J), ``full_circle`` says the end
    equals the start at file precision (360 degrees, PHYS-16) and ``sweep_deg``
    is the swept angle in the arc's direction. ``c_start``/``c_end`` are the
    dialect heading before and after the line (None outside dialect mode or
    before the first ``C``). ``written`` holds the motion letters the line wrote.
    """

    kind: MoveKind
    start: XYZ
    end: XYZ
    centre: tuple[Decimal, Decimal] | None
    full_circle: bool
    sweep_deg: float | None
    c_start: Decimal | None
    c_end: Decimal | None
    feed: Decimal | None
    rpm: Decimal | None
    spindle_on: bool
    tool: int | None
    op: str | None
    written: frozenset[str]
    src: SourceRef

    @property
    def is_arc(self) -> bool:
        return self.kind in ("arc_cw", "arc_ccw")


@dataclass(frozen=True)
class RawEvent:
    """A line's effect that is not motion.

    Spindle on or off, tool select or change, dust discharge, a dialect marker,
    a dropped dwell, the program end.
    """

    kind: EventKind
    value: str | None
    src: SourceRef


@dataclass(frozen=True)
class RawProgram:
    """The program as read, in file order: moves and events, each with its source line.

    ``units`` is the unit and where it came from (the GI ``UNITS`` line,
    job.toml, the file's ``G21``, or spec 05 for the dialect); None when it
    could not be established (then the result carries a ``D-UNITS`` refusal).
    ``ops`` are the GI operation blocks that were read without error.
    """

    file: str
    flavour: ResolvedFlavour
    line_count: int
    items: tuple[RawMove | RawEvent, ...]
    ops: tuple[GIOp, ...]
    tool: int | None
    units: Param[str] | None
    end_line: int | None

    @property
    def moves(self) -> tuple[RawMove, ...]:
        return tuple(item for item in self.items if isinstance(item, RawMove))

    @property
    def events(self) -> tuple[RawEvent, ...]:
        return tuple(item for item in self.items if isinstance(item, RawEvent))

    def move_at(self, line: int) -> RawMove | None:
        """The move read from ``line``, or None (a line holds at most one move)."""
        for move in self.moves:
            if move.src.line == line:
                return move
        return None


@dataclass(frozen=True)
class ReadNote:
    """A normalisation note (safety-model 3.7): what the reader dropped, ignored or recorded, and on which lines."""

    kind: str
    text: str
    lines: tuple[int, ...]


@dataclass(frozen=True)
class ReadResult:
    """What :func:`read_gcode` returns.

    ``findings`` are catalogue findings (``goliath_prep.core.findings``);
    ``refused`` is True when any of them refuses, and then the program must
    not be planned. ``bound`` holds the values the GI header and job.toml
    declare, each a Param whose source names where it came from (gi-format 10).
    ``disclosures`` must be raised wherever the result is shown.
    """

    program_raw: RawProgram
    gi_header: GIHeader | None
    findings: tuple[Finding, ...]
    notes: tuple[ReadNote, ...]
    disclosures: tuple[Disclosure, ...]
    bound: Mapping[str, Param[Any]]
    flavour: ResolvedFlavour

    @property
    def refused(self) -> bool:
        return any(f.severity in ("refuse", "internal-error") for f in self.findings)

    def refusals(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity in ("refuse", "internal-error"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def read_gcode(text: str, *, flavour: fl.Flavour, job: JobSpec, gates: GateView | None = None,
               file: str = "<input>") -> ReadResult:
    """Read G-code text strictly (design 4.2). Never raises on file content; see the module documentation.

    ``flavour`` is ``auto`` (GI when any line carries a GI annotation, else
    generic; never the dialect), ``gi``, ``generic`` or ``dialect`` (verify
    only). ``job`` is the job's job.toml (P01). ``gates`` is the gate view the
    reader consults for ``G4``; None means the all-closed desk view, which is
    disclosed. ``file`` names the input in source references.

    Raises ``TypeError``/``ValueError`` only for wrong arguments.
    """
    if not isinstance(text, str):
        raise TypeError("read_gcode needs the G-code as a str (decode the file as UTF-8), got %s" % type(text).__name__)
    if flavour not in fl.FLAVOURS:
        raise ValueError("unknown flavour %r: one of %s" % (flavour, ", ".join(fl.FLAVOURS)))
    if not isinstance(job, JobSpec):
        raise TypeError("read_gcode needs the job as a JobSpec (goliath_prep.jobspec), got %s" % type(job).__name__)
    if gates is not None and not isinstance(gates, GateView):
        raise TypeError("gates must provide the GateView protocol (goliath_prep.core.types), got %r" % (gates,))
    if not isinstance(file, str) or not file.strip():
        raise ValueError("file must name the input (a non-empty string), got %r" % (file,))
    return _Reader(text, flavour, job, gates, file).read()


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


def _surrogate_column(raw: str) -> int | None:
    if raw.isascii():
        return None
    for i, ch in enumerate(raw):
        if "\ud800" <= ch <= "\udfff":
            return i + 1
    return None


def _safe_text(raw: str) -> str:
    """The line for a source reference; a lone surrogate (not UTF-8) is written as an escape so reports can show it."""
    if _surrogate_column(raw) is None:
        return raw
    return raw.encode("utf-8", "backslashreplace").decode("utf-8")


def _frac(value: Decimal) -> Fraction:
    return Fraction(value)


def _exact_add(a: Decimal, b: Decimal) -> Decimal:
    """a + b with no rounding, whatever the number of digits."""
    exponent = min(a.as_tuple().exponent, b.as_tuple().exponent)  # type: ignore[type-var]
    top = max(a.adjusted(), b.adjusted(), 0)
    with localcontext() as ctx:
        ctx.prec = max(28, top - int(exponent) + 3)
        ctx.traps[Inexact] = True
        return a + b


def _witness_number(value: Decimal) -> int | float | None:
    """A Decimal for a witness field: an int when integral and short, a finite float otherwise, else None."""
    if value == value.to_integral_value() and value.adjusted() < _LONGEST_INT:
        return int(value)
    as_float = float(value)
    return as_float if math.isfinite(as_float) else None


class _OpenOp:
    def __init__(self, op_id: str | None, line: int, values: dict[str, Any], valid: bool) -> None:
        self.id = op_id
        self.line = line
        self.values = values
        self.valid = valid


class _Reader:
    def __init__(self, text: str, flavour: str, job: JobSpec, gates: GateView | None, file: str) -> None:
        self.text = text
        self.asked = flavour
        self.job = job
        self.file = file
        if gates is None or isinstance(gates, ClosedGateView):
            self.gates: GateView = gates if gates is not None else ClosedGateView()
            self.desk_view = True
        else:
            self.gates = gates
            self.desk_view = False
        self.findings: list[Finding] = []
        self.notes: dict[str, tuple[str, list[int]]] = {}
        self.disclosures: list[Disclosure] = []
        self.items: list[RawMove | RawEvent] = []
        self.lexed: list[LexedLine] = []
        self.flavour: ResolvedFlavour = "generic"
        self.vocab = fl.GENERIC_VOCABULARY
        # modal state
        self.mode: int | None = None
        self.x: Coord = None
        self.y: Coord = None
        self.z: Coord = None
        self.c: Coord = None
        self.feed: Decimal | None = None
        self.rpm: Decimal | None = None
        self.spindle_on = False
        self.motion_seen = False
        self.first_motion_line: int | None = None
        self.g21_line: int | None = None
        self.t_line: int | None = None
        self.t_number: int | None = None
        self.m6_line: int | None = None
        self.ended_at: int | None = None
        # GI
        self.header_open = False
        self.header_end: int | None = None
        self.header_seen: dict[str, int] = {}
        self.source: GISource | None = None
        self.units: GIUnits | None = None
        self.stock: GIStock | None = None
        self.stock_parts: dict[str, Any] = {}
        self.stock_line: int | None = None
        self.tower2: GITower2 | None = None
        self.tool: GITool | None = None
        self.tool_parts: dict[str, Any] = {}
        self.tool_line: int | None = None
        self.tool_n: int | None = None
        self.zshift: GIZShift | None = None
        self.zshift_bad = False
        self.has_op_lines = False
        self.open_op: _OpenOp | None = None
        self.op_ids: set[str] = set()
        self.ops: list[GIOp] = []
        self.bound: dict[str, Param[Any]] = {}
        self.unit_param: Param[str] | None = None

    # -- reporting -----------------------------------------------------------

    def _src(self, number: int) -> SourceRef:
        raw = self.lexed[number - 1].raw if 1 <= number <= len(self.lexed) else ""
        return SourceRef(self.file, number, _safe_text(raw))

    def _report(self, check_id: str, number: int, column: int | None, reason: str, *,
                severity: str | None = None, **extra: Any) -> Finding:
        where = "line %d, column %d" % (number, column) if column is not None else "line %d" % number
        allowed = ids.WITNESS_FIELDS[check_id]
        fields: dict[str, Any] = {"src": self._src(number), "hazard": reason}
        if column is not None and "column" in allowed:
            fields["column"] = column
        for name, value in extra.items():
            if name not in allowed:
                raise ValueError("internal: witness field %s is not listed for %s" % (name, check_id))
            if value is not None:
                fields[name] = value
        message = "%s: %s" % (where, reason)
        witness = Witness(**fields)
        if severity is None:
            finding = Finding.of(check_id, message, witness=witness)
        else:
            finding = Finding(check_id, severity, message, witness, None)  # type: ignore[arg-type]
        self.findings.append(finding)
        return finding

    def _refused(self) -> bool:
        return any(f.severity in ("refuse", "internal-error") for f in self.findings)

    def _require_refused(self, check_id: str, number: int, what: str) -> None:
        """A check that cannot run because an earlier refusal took its input: make sure that refusal exists."""
        if not self._refused():
            self._report(check_id, number, None, "internal error: %s could not be checked and nothing refused the "
                                                 "file" % what, severity="internal-error")

    def _note(self, kind: str, number: int, text: str) -> None:
        entry = self.notes.get(kind)
        if entry is None:
            self.notes[kind] = (text, [number])
        elif entry[1][-1] != number:
            entry[1].append(number)

    def _disclose(self, disclosure: Disclosure) -> None:
        if disclosure not in self.disclosures:
            self.disclosures.append(disclosure)

    # -- top level -----------------------------------------------------------

    def read(self) -> ReadResult:
        lines, had_bom = split_lines(self.text)
        self.lexed = [lex_line(i + 1, raw) for i, raw in enumerate(lines)]
        if self.desk_view:
            self._disclose(ClosedGateView.DISCLOSURE)
        if had_bom:
            self._note("bom", 1, "a UTF-8 byte-order mark at the start of the file was ignored (gi-format 3.1)")
        first_annotation = next((line.number for line in self.lexed if is_annotation_like(line.comment)), None)
        if self.asked == "auto":
            if first_annotation is not None:
                self.flavour = "gi"
                self._note("flavour", first_annotation, "read as GI: line %d carries a GI annotation (gi-format 5)"
                           % first_annotation)
            else:
                self.flavour = "generic"
                self._note("flavour", 1, "read as generic G-code: no line carries a GI annotation (the Goliath "
                                         "dialect is read only when asked for)")
        else:
            self.flavour = self.asked  # type: ignore[assignment]
            self._note("flavour", 1, "read as %s, as asked" % {"gi": "GI", "generic": "generic G-code",
                                                                "dialect": "the Goliath dialect (verify only)"}[
                self.flavour])
        self.vocab = fl.vocabulary(self.flavour)
        if self.flavour == "gi":
            if not self._version_line(first_annotation):
                return self._result(stopped=True)
            self.has_op_lines = any(self._is_op_line(line) for line in self.lexed[1:])
            self.header_open = True
            body = self.lexed[1:]
        else:
            if first_annotation is not None:
                self._report(ids.D_GI_VERSION, 1, None,
                             "this file is GI: line %d carries a GI annotation, and a file with annotations is a GI "
                             "file (gi-format 5). It was asked to be read as %s, which would ignore its "
                             "annotations; read it as GI" % (first_annotation, self.vocab.name))
                return self._result(stopped=True)
            body = self.lexed
        for line in body:
            self._line(line)
        self._finish()
        return self._result(stopped=False)

    def _is_op_line(self, line: LexedLine) -> bool:
        if line.has_code or line.comment is None or not is_annotation_like(line.comment):
            return False
        parsed = lex_annotation(line)
        return isinstance(parsed, Annotation) and parsed.keyword == "OP"

    def _version_line(self, first_annotation: int | None) -> bool:
        if not self.lexed:
            self._report(ids.D_GI_VERSION, 1, None, "the file is empty: line 1 of a GI file is the version line "
                                                    "';GOLIATH-GI 1'")
            return False
        first = self.lexed[0]
        surrogate = _surrogate_column(first.raw)
        token = version_line_token(first) if surrogate is None else None
        if token is None:
            why = ("this file carries GI annotations (line %d), so " % first_annotation
                   if first_annotation is not None else "")
            self._report(ids.D_GI_VERSION, 1, 1, "%sline 1 must be the version line ';GOLIATH-GI 1' (gi-format 5); "
                                                 "the file is not read as generic G-code with its annotations "
                                                 "ignored" % why)
            return False
        column = first.raw.index(token) + 1 if token else 1
        if not _VERSION.fullmatch(token):
            self._report(ids.D_GI_VERSION, 1, column, "malformed GI version %r: the version is a whole number "
                                                      "without a point or leading zeros" % token[:40])
            return False
        if token != str(fl.GI_VERSION):
            self._report(ids.D_GI_VERSION, 1, column, "GI version %s is unknown to this reader, which knows "
                                                      "version 1: refused, not guessed, and nothing else in the "
                                                      "file is interpreted" % token[:40])
            return False
        return True

    def _line(self, line: LexedLine) -> None:
        surrogate = _surrogate_column(line.raw)
        if surrogate is not None:
            self._report(ids.D_SYNTAX, line.number, surrogate, "the text is not UTF-8 (a lone surrogate character)")
            return
        if self.ended_at is not None:
            self._after_end(line)
            return
        if line.blank:
            return
        if not line.has_code:
            self._comment_line(line)
            return
        if self.header_open:
            self._close_header(line.number)
        self._code_line(line)
        if line.comment is not None:
            self._trailing_comment(line)

    # -- comments, annotations, markers --------------------------------------

    def _comment_line(self, line: LexedLine) -> None:
        comment = line.comment
        assert comment is not None
        if self.flavour == "gi" and version_line_token(line) is not None:
            self._report(ids.D_GI_VERSION, line.number, 1, "a second version line: the version line is line 1 only")
            return
        kind = comment_kind(comment)
        if kind == "annotation":
            parsed = lex_annotation(line)
            if isinstance(parsed, AnnotationError):
                self._report(ids.D_GI_SYNTAX, line.number, parsed.column, "a malformed annotation (gi-format 4.1): "
                                                                          "%s" % parsed.reason)
                return
            self._annotation(line, parsed)
        elif kind == "marker":
            self._marker(line)
        else:
            self._note("comment-dropped", line.number, "comments dropped: the robot is sent no comments "
                                                       "(GI annotations were read first)")

    def _trailing_comment(self, line: LexedLine) -> None:
        comment = line.comment
        assert comment is not None
        kind = comment_kind(comment)
        if kind == "annotation":
            self._report(ids.D_GI_SYNTAX, line.number, line.comment_column, "an annotation is a line of its own: "
                                                                            "';GOLIATH' in column 1 with no G-code "
                                                                            "before it (gi-format 4.1)")
        elif kind == "marker":
            self._marker(line)
        else:
            self._note("comment-dropped", line.number, "comments dropped: the robot is sent no comments "
                                                       "(GI annotations were read first)")

    def _marker(self, line: LexedLine) -> None:
        comment = line.comment or ""
        letter = comment.lstrip(" \t")[:1].upper()
        what = "a ;U tool-change marker" if letter == "U" else "a ;S empty-the-dust-bin marker"
        if self.flavour != "dialect":
            self._report(ids.D_MARKER, line.number, line.comment_column,
                         "%s (spec 03 §G-code in-stream markers) is read only in dialect verify mode; here it would "
                         "be dropped, silently discarding what it asks for, so it is refused. Any comment whose first "
                         "character that is not a space is U or S counts: start comments with another character, "
                         "for example '; Note: ...'" % what)
            return
        if letter == "U" and self.motion_seen:
            self._report(ids.D_SINGLETOOL, line.number, line.comment_column,
                         "%s after the first motion (line %d): a tool change during the program; one tool per "
                         "program" % (what, self.first_motion_line or 0), value=";" + comment.strip()[:40])
            return
        self.items.append(RawEvent("marker_u" if letter == "U" else "marker_s", comment.strip(),
                                   self._src(line.number)))
        self._note("marker-recorded", line.number, "dialect markers ;U/;S recorded for verification (spec 03 "
                                                   "§G-code in-stream markers); the robot acts on them")

    def _annotation(self, line: LexedLine, annotation: Annotation) -> None:
        keyword = annotation.keyword
        if keyword in fl.HEADER_KEYWORDS:
            self._header_line(line, annotation)
        elif keyword == "OP":
            if self.header_open:
                self._close_header(line.number)
            self._op(line, annotation)
        elif keyword == "OPEND":
            self._opend(line, annotation)
        else:
            self._report(ids.N_GI_UNKNOWN_KEY, line.number, annotation.keyword_column,
                         "unknown annotation keyword %s: warned and ignored (within GI version 1 anything new is "
                         "informational; a change a reader must understand needs version 2)" % keyword)

    def _values(self, line: LexedLine, annotation: Annotation, check_id: str) -> tuple[dict[str, Any], bool]:
        """The values of an annotation's known keys, typed (gi-format 4.2); report every error. Returns (values, ok)."""
        spec = fl.KEYWORDS[annotation.keyword]
        values: dict[str, Any] = {}
        ok = True
        unknown: list[str] = []
        positional_taken = False
        for item in annotation.items:
            if item.key is None:
                if spec.positional is None or positional_taken:
                    self._report(check_id, line.number, item.column, "%s: unexpected positional value %r"
                                 % (annotation.keyword, item.value[:40]))
                    ok = False
                    continue
                positional_taken = True
                key, key_spec = "value", spec.positional
            elif item.key in spec.keys:
                key, key_spec = item.key, spec.keys[item.key]
            else:
                unknown.append(item.key)
                continue
            parsed = self._typed(item, key_spec, annotation.keyword, key)
            if isinstance(parsed, _Bad):
                self._report(parsed.check_id or check_id, line.number, item.column, parsed.reason)
                ok = False
            else:
                values[key] = parsed
        if unknown:
            self._report(ids.N_GI_UNKNOWN_KEY, line.number, None,
                         "%s: unknown key%s %s warned and ignored; the rest of the line is read" % (
                             annotation.keyword, "" if len(unknown) == 1 else "s", ", ".join(unknown)))
        for key, key_spec in spec.keys.items():
            if key_spec.required and key not in values and not any(i.key == key for i in annotation.items):
                self._report(check_id, line.number, annotation.keyword_column,
                             "%s is missing the required key %s (%s): required, never defaulted"
                             % (annotation.keyword, key, key_spec.what))
                ok = False
        if spec.positional is not None and spec.positional.required and not positional_taken:
            self._report(check_id, line.number, annotation.keyword_column,
                         "%s needs its value (%s): required, never defaulted" % (annotation.keyword,
                                                                                 spec.positional.what))
            ok = False
        return values, ok

    def _typed(self, item: AnnotationItem, spec: fl.KeySpec, keyword: str, key: str) -> Any:
        name = "%s %s" % (keyword, key if key != "value" else "value")
        shown = item.value[:40]
        if spec.type == "text":
            return item.value
        if item.quoted:
            return _Bad("%s: %r is quoted, but a %s is written bare (gi-format 4.2)" % (name, shown, spec.type))
        value = item.value
        if spec.type == "token":
            return value
        if spec.type == "enum":
            if value in spec.choices:
                return value
            check = ids.D_UNITS if keyword == "UNITS" else None
            extra = ": set the CAM to millimetres" if keyword == "UNITS" else ""
            return _Bad("%s: %r is not one of %s%s" % (name, shown, ", ".join(spec.choices), extra), check)
        if spec.type == "integer":
            if not _DIGITS.fullmatch(value):
                return _Bad("%s: %r is not an integer (digits only)" % (name, shown))
            if len(value) > _LONGEST_INT:
                return _Bad("%s: %r is outside its domain (more than %d digits)" % (name, shown, _LONGEST_INT))
            return int(value)
        if not _NUMBER.fullmatch(value):
            return _Bad("%s: %r is not a number (an optional sign, digits, and a point with digits on both sides)"
                        % (name, shown))
        number = Decimal(value)
        if not (-ANNOTATION_NUMBER_LIMIT < number < ANNOTATION_NUMBER_LIMIT):
            return _Bad("%s: %r is outside its domain" % (name, shown))
        if spec.type in ("length", "positive") and not number > 0:
            return _Bad("%s: %s must be greater than zero" % (name, shown))
        return number

    def _header_line(self, line: LexedLine, annotation: Annotation) -> None:
        keyword = annotation.keyword
        if not self.header_open:
            self._report(ids.D_GI_HEADER, line.number, annotation.keyword_column,
                         "header line %s after the header ended at line %d (the first OP line or G-code line ends "
                         "it)" % (keyword, self.header_end or 0))
            if keyword == "TOOL" and "TOOL" in self.header_seen:
                self._report(ids.D_SINGLETOOL, line.number, annotation.keyword_column,
                             "a second TOOL line (the first is line %d): one tool per program; a job with two tools "
                             "is two GI files" % self.header_seen["TOOL"], value="TOOL")
            return
        if keyword in self.header_seen:
            if keyword == "TOOL":
                self._report(ids.D_SINGLETOOL, line.number, annotation.keyword_column,
                             "a second TOOL line (the first is line %d): one tool per program; a job with two tools "
                             "is two GI files" % self.header_seen[keyword], value="TOOL")
            else:
                self._report(ids.D_GI_HEADER, line.number, annotation.keyword_column,
                             "%s appears twice (first at line %d): each header line appears once, even with equal "
                             "values" % (keyword, self.header_seen[keyword]))
            return
        self.header_seen[keyword] = line.number
        values, ok = self._values(line, annotation, ids.D_GI_HEADER)
        number = line.number
        if keyword == "SOURCE":
            if ok:
                self.source = GISource(values["cam"], values["post"], number)
        elif keyword == "UNITS":
            if ok:
                self.units = GIUnits(values["value"], number)
        elif keyword == "STOCK":
            self.stock_parts = values
            self.stock_line = number
            if ok:
                self.stock = GIStock(values["x"], values["y"], values["t"], values["origin"], values["zzero"],
                                     number)
        elif keyword == "TOWER2":
            if ok:
                self.tower2 = GITower2(values["axis"], number)
        elif keyword == "TOOL":
            self.tool_parts = values
            self.tool_line = number
            self.tool_n = values.get("n")
            if ok:
                self.tool = GITool(values["n"], values["d"], values["flute"], values.get("stickout"),
                                   values.get("kind"), values.get("rpm"), values.get("name"), number)
        elif keyword == "ZSHIFT":
            if ok:
                self.zshift = GIZShift(values["value"], number)
                self._note("zshift-recorded", number, "ZSHIFT recorded and never applied: the post has already "
                                                      "applied it, so every Z is read as written (gi-format 6, "
                                                      "red-team PHYS-12)")
            else:
                self.zshift_bad = True

    def _close_header(self, at: int) -> None:
        self.header_open = False
        self.header_end = at
        for keyword in fl.REQUIRED_HEADER:
            if keyword not in self.header_seen:
                self._report(ids.D_GI_HEADER, at, None,
                             "the required header line %s is missing (the header ends here); header values are "
                             "never defaulted" % keyword)
        conflicts, self.bound = fl.compare_with_job(stock_parts=self.stock_parts, stock_line=self.stock_line,
                                                    tower2=self.tower2, tool_parts=self.tool_parts,
                                                    tool_line=self.tool_line, job=self.job)
        for conflict in conflicts:
            unit = " %s" % conflict.unit if conflict.unit else ""
            reason = ("%s = %s%s disagrees with %s = %s%s (%s). %s" % (
                conflict.gi_name, conflict.gi_value, unit, conflict.job_name, conflict.job_value, unit,
                conflict.what, conflict.advice))
            sources = {"%s (line %d)" % (conflict.gi_name, conflict.line): conflict.gi_value,
                       conflict.job_name: conflict.job_value}
            self._report(ids.D_GI_CONFLICT, conflict.line, None, reason, sources=sources)

    def _op(self, line: LexedLine, annotation: Annotation) -> None:
        values, ok = self._values(line, annotation, ids.D_GI_OP)
        op_id = values.get("id")
        column = annotation.keyword_column
        if self.open_op is not None:
            self._report(ids.D_GI_OP, line.number, column,
                         "an OP inside the open OP %s (line %d): operations do not nest; close it with ;GOLIATH "
                         "OPEND first" % (self.open_op.id, self.open_op.line))
            return
        if op_id is not None:
            if op_id in self.op_ids:
                self._report(ids.D_GI_OP, line.number, column, "a second operation with the id %s: ids are unique"
                             % op_id)
                ok = False
            self.op_ids.add(op_id)
        if "tool" in values:
            if self.tool_n is None:
                self._report(ids.D_GI_OP, line.number, column, "tool=%d names no declared TOOL (the header has no "
                                                               "readable TOOL n)" % values["tool"])
                ok = False
            elif values["tool"] != self.tool_n:
                self._report(ids.D_GI_OP, line.number, column, "tool=%d names no declared TOOL (the header "
                                                               "declares TOOL n=%d)" % (values["tool"], self.tool_n))
                ok = False
        if "c" in values:
            c = values["c"]
            if c > 360:
                self._report(ids.D_GI_OP, line.number, column, "forced heading c=%d is outside 0 to 360" % c)
                ok = False
            elif c % 10:
                self._report(ids.D_GI_OP, line.number, column,
                             "forced heading c=%d is off the 10-degree grid of spec 08 (allowed: 0, 10, ..., 360, "
                             "where 360 is the same heading as 0); the vendor's search finds no candidate" % c)
                ok = False
        self.open_op = _OpenOp(op_id, line.number, values, ok)

    def _opend(self, line: LexedLine, annotation: Annotation) -> None:
        values, ok = self._values(line, annotation, ids.D_GI_OP)
        column = annotation.keyword_column
        if self.open_op is None:
            self._report(ids.D_GI_OP, line.number, column, "OPEND with no open OP")
            return
        open_op = self.open_op
        self.open_op = None
        if values.get("id") != open_op.id or open_op.id is None:
            self._report(ids.D_GI_OP, line.number, column, "OPEND id=%s does not close the open OP %s (line %d)"
                         % (values.get("id"), open_op.id, open_op.line))
            return
        if ok and open_op.valid:
            v = open_op.values
            through = {"auto": "auto", "1": True, "0": False}[v["through"]]
            self.ops.append(GIOp(v["id"], v["kind"], through, v["tool"], v.get("label"), v.get("c"), open_op.line,
                                 line.number))

    # -- after the program end -----------------------------------------------

    def _after_end(self, line: LexedLine) -> None:
        if line.blank:
            return
        end = "after the program end (M2/M30 at line %d) only blank lines and plain comments may follow" % (
            self.ended_at or 0)
        if line.has_code:
            column = len(line.code) - len(line.code.lstrip(" \t")) + 1
            self._report(ids.D_AFTER_END, line.number, column, "%s: this line has G-code, which is refused, not "
                                                               "dropped (a reader cannot know what the CAM meant)"
                         % end)
            return
        kind = comment_kind(line.comment or "")
        if kind == "annotation":
            self._report(ids.D_AFTER_END, line.number, line.comment_column, "%s: this line is an annotation" % end)
        elif kind == "marker":
            if self.flavour == "dialect":
                self._report(ids.D_AFTER_END, line.number, line.comment_column, "%s: this line is a ;U/;S marker"
                             % end)
            else:
                self._marker(line)
        else:
            self._note("comment-dropped", line.number, "comments dropped: the robot is sent no comments "
                                                       "(GI annotations were read first)")

    # -- G-code ---------------------------------------------------------------

    def _code_line(self, line: LexedLine) -> None:
        number = line.number
        if line.error is not None:
            self._report(ids.D_SYNTAX, number, line.error.column, line.error.reason)
            return
        words = line.words
        if not self._line_rules(number, words):
            return
        refused = False
        motion_g: Word | None = None
        noops: list[Word] = []
        g21 = g43 = None
        dwell: Word | None = None
        m_word: Word | None = None
        m_action: str | None = None
        letters: dict[str, Word] = {}
        for word in words:
            rule = self._rule(word)
            if isinstance(rule, _Bad):
                self._report(rule.check_id or ids.D_UNSUPPORTED, number, word.column, rule.reason)
                refused = True
                continue
            if rule.action == "refuse":
                self._report(rule.check_id or ids.D_UNSUPPORTED, number, word.column, "%s: %s" % (word, rule.why))
                refused = True
                continue
            if word.letter == "G":
                if rule.action == "motion":
                    motion_g = word
                elif rule.action == "noop":
                    noops.append(word)
                elif rule.action == "units_mm":
                    g21 = word
                elif rule.action == "tool_length":
                    g43 = word
                elif rule.action == "dwell":
                    dwell = word
            elif word.letter == "M":
                m_word, m_action = word, rule.action
            else:
                letters[word.letter] = word
        if refused:
            return
        if not self._ranges(number, letters):
            return
        if "H" in letters and g43 is None:
            self._report(ids.D_UNSUPPORTED, number, letters["H"].column, "H without G43: outside the accepted "
                                                                         "vocabulary")
            return
        if "P" in letters and dwell is None:
            self._report(ids.D_UNSUPPORTED, number, letters["P"].column, "P without G4: outside the accepted "
                                                                         "vocabulary")
            return
        if m_action == "end":
            assert m_word is not None
            others = [w for w in words if w is not m_word and w.letter != "N"]
            self.ended_at = number
            if others:
                self._report(ids.D_AFTER_END, number, others[0].column,
                             "%s stands alone on its line (an N word and a comment are allowed); with any other "
                             "word the line is refused" % m_word)
                return
            if "N" in letters:
                self._note("n-word-dropped", number, "N line numbers dropped")
            self.items.append(RawEvent("program_end", str(m_word), self._src(number)))
            self._note("program-end-dropped", number, "M2/M30 program end dropped: the robot is sent no M30 "
                                                      "(spec 05); nothing after it is read")
            return
        if dwell is not None and not self._dwell(number, dwell):
            return
        if not self._tool_rules(number, letters, m_word, m_action):
            return
        move = self._motion(number, motion_g, letters, m_action)
        if move is False:
            return
        # Accepted: apply in RS274 order (gi-format 8.3): F, S, then the M word, then the motion.
        if "N" in letters:
            self._note("n-word-dropped", number, "N line numbers dropped")
        if noops or g21 is not None:
            self._note("noop", number, "no-op words accepted: G17 G21 G90 G94 G54 G40 assert the defaults (XY "
                                       "plane, millimetres, absolute, feed per minute, first work offset, no "
                                       "compensation)")
        if g21 is not None:
            if self.g21_line is None:
                self.g21_line = number
        if g43 is not None:
            self._g43(number)
        if "F" in letters:
            self.feed = letters["F"].value
        if "S" in letters:
            self.rpm = letters["S"].value
        if "T" in letters:
            self.t_line = number
            self.t_number = int(letters["T"].text)
            self.items.append(RawEvent("tool_select", letters["T"].text.lstrip("0") or "0", self._src(number)))
        if m_action is not None:
            self._m_word(number, m_word, m_action)
        if motion_g is not None:
            self.mode = int(motion_g.text)
        if isinstance(move, RawMove):
            self.items.append(move)
            self.x, self.y, self.z = move.end
            self.c = move.c_end
            if not self.motion_seen:
                self.motion_seen = True
                self.first_motion_line = number

    def _line_rules(self, number: int, words: tuple[Word, ...]) -> bool:
        """gi-format 8.3: each letter once (G several times with different numbers), one motion code."""
        seen: dict[str, Word] = {}
        g_seen: dict[str, Word] = {}
        motion: Word | None = None
        for word in words:
            if word.letter == "G":
                key = (word.text.lstrip("+-").lstrip("0") or "0") if word.integral else word.text
                if key in g_seen:
                    self._report(ids.D_SYNTAX, number, word.column, "%s repeats %s on one line" % (word,
                                                                                                    g_seen[key]))
                    return False
                g_seen[key] = word
                if word.digits_only and len(word.text) <= _LONGEST_INT and int(word.text) in (0, 1, 2, 3):
                    if motion is not None:
                        self._report(ids.D_SYNTAX, number, word.column, "two motion codes on one line (%s and %s)"
                                     % (motion, word))
                        return False
                    motion = word
                continue
            if word.letter in seen:
                if word.letter == "M":
                    reason = "at most one M word per line (%s and %s), so the order of actions is never ambiguous" % (
                        seen["M"], word)
                else:
                    reason = "the letter %s appears twice on one line (first at column %d)" % (
                        word.letter, seen[word.letter].column)
                self._report(ids.D_SYNTAX, number, word.column, reason)
                return False
            seen[word.letter] = word
        return True

    def _rule(self, word: Word) -> fl.WordRule | _Bad:
        letter = word.letter
        if letter in ("G", "M"):
            if word.signed:
                return _Bad("%s: G and M numbers are digits only" % word, ids.D_SYNTAX)
            if not word.integral:
                return _Bad("%s: decimal %s codes are not accepted (absolute coordinates, I/J centres and the XY "
                            "plane only)" % (word, letter), ids.D_UNSUPPORTED)
            if len(word.text) > _LONGEST_INT:
                return self.vocab.other_g if letter == "G" else self.vocab.other_m
            number = int(word.text)
            return self.vocab.rule_g(number) if letter == "G" else self.vocab.rule_m(number)
        rule = self.vocab.rule_letter(letter)
        if rule.action != "refuse" and letter in ("T", "N", "H") and not word.digits_only:
            return _Bad("%s: %s takes digits only" % (word, letter), ids.D_SYNTAX)
        if rule.action != "refuse" and letter == "T" and len(word.text) > _LONGEST_INT:
            return _Bad("%s: the tool number is outside its domain (more than %d digits)" % (word, _LONGEST_INT),
                        ids.D_SINGLETOOL)
        return rule

    def _ranges(self, number: int, letters: Mapping[str, Word]) -> bool:
        ok = True
        for letter in ("X", "Y", "Z", "I", "J"):
            word = letters.get(letter)
            if word is not None and (word.value > COORDINATE_LIMIT or word.value < -COORDINATE_LIMIT):
                self._report(ids.D_RANGE, number, word.column, "%s is beyond plus or minus 10 000 mm" % word)
                ok = False
        if self.flavour == "dialect" and "C" in letters:
            word = letters["C"]
            if word.value > COORDINATE_LIMIT or word.value < -COORDINATE_LIMIT:
                self._report(ids.D_RANGE, number, word.column, "%s is beyond plus or minus 10 000" % word)
                ok = False
        for letter in ("F", "S"):
            word = letters.get(letter)
            if word is not None and word.value < 0:
                self._report(ids.D_RANGE, number, word.column, "%s: a negative %s" % (
                    word, "feed" if letter == "F" else "spindle speed"))
                ok = False
        return ok

    def _dwell(self, number: int, word: Word) -> bool:
        allowed = self.gates.allowed(DWELL_GATE)
        if allowed is True:
            self.items.append(RawEvent("dwell_dropped", str(word), self._src(number)))
            self._note("dwell-dropped", number, "G4 dwell dropped: the gate drop_dwells is open")
            self._disclose(Disclosure(
                id="READER:G4-dropped", severity="info",
                text="G4 dwell dropped: the gate drop_dwells is open (rung R4 recorded the spindle's spin-up "
                     "behaviour)",
                source="%s line %d (gate snapshot %s)" % (self.file, number, self.gates.snapshot_sha256())))
            return True
        state = "closed" if allowed is False else "uncertain"
        self._report(ids.G_GATE, number, word.column,
                     "%s: a G4 dwell is refused while the gate drop_dwells is %s (rung R4, a spindle air job, opens "
                     "it by recording the spindle's spin-up behaviour; then G4 is dropped with a disclosure)"
                     % (word, state), gate=DWELL_GATE, unlock_rung=DWELL_RUNG)
        return False

    def _g43(self, number: int) -> None:
        self._note("g43-ignored", number, "G43 tool length offset accepted and ignored: the robot sets its Z zero "
                                          "itself in its drill phases")
        if not any(d.id == "READER:G43-ignored" for d in self.disclosures):
            self._disclose(Disclosure(
                id="READER:G43-ignored", severity="amber",
                text="G43 (tool length offset) ignored: the robot sets its Z zero itself in its drill phases. This "
                     "is our reading of spec 03 §The governing principle, not yet verified on a robot",
                source="%s line %d" % (self.file, number)))

    def _tool_rules(self, number: int, letters: Mapping[str, Word], m_word: Word | None,
                    m_action: str | None) -> bool:
        """gi-format 8.9 (one tool): returns False when the line is refused."""
        t_word = letters.get("T")
        if t_word is not None:
            if self.motion_seen:
                self._report(ids.D_SINGLETOOL, number, t_word.column,
                             "%s after the first motion (line %d): a tool change during the program, even naming "
                             "the same tool; one tool per program" % (t_word, self.first_motion_line or 0),
                             value=str(t_word))
                return False
            if self.t_line is not None:
                self._report(ids.D_SINGLETOOL, number, t_word.column,
                             "a second T (%s; the first is at line %d): one tool per program" % (t_word, self.t_line),
                             value=str(t_word))
                return False
            if self.flavour == "gi":
                if self.tool_n is None:
                    self._require_refused(ids.D_SINGLETOOL, number, "the T word against the header's TOOL n")
                    return False
                if int(t_word.text) != self.tool_n:
                    self._report(ids.D_SINGLETOOL, number, t_word.column,
                                 "%s names a tool the header does not declare (TOOL n=%d at line %d): one tool per "
                                 "program" % (t_word, self.tool_n, self.tool_line or 0), value=str(t_word))
                    return False
        if m_action == "tool_change":
            assert m_word is not None
            if self.motion_seen:
                self._report(ids.D_SINGLETOOL, number, m_word.column,
                             "%s after the first motion (line %d): a tool change during the program; one tool per "
                             "program" % (m_word, self.first_motion_line or 0), value=str(m_word))
                return False
            if self.m6_line is not None:
                self._report(ids.D_SINGLETOOL, number, m_word.column, "a second %s (the first is at line %d): one "
                                                                      "tool per program" % (m_word, self.m6_line),
                             value=str(m_word))
                return False
            if self.flavour != "dialect" and t_word is None and self.t_line is None:
                self._report(ids.D_SINGLETOOL, number, m_word.column, "%s with no T: name the tool with a T word on "
                                                                      "this line or before it" % m_word,
                             value=str(m_word))
                return False
        return True

    def _m_word(self, number: int, word: Word | None, action: str) -> None:
        assert word is not None
        src = self._src(number)
        if action == "spindle_on":
            self.spindle_on = True
            self.items.append(RawEvent("spindle_on", str(word), src))
        elif action == "spindle_off":
            self.spindle_on = False
            self.items.append(RawEvent("spindle_off", str(word), src))
        elif action == "coolant":
            self._note("coolant-dropped", number, "coolant words M7/M8/M9 dropped: the robot has no coolant")
        elif action == "tool_change":
            self.m6_line = number
            self.items.append(RawEvent("tool_change", str(word), src))
            if self.flavour == "dialect":
                # spec 05: M06 resets the modal position memory.
                self.x = self.y = self.z = None
        elif action == "dust":
            self.items.append(RawEvent("dust", str(word), src))
        else:  # pragma: no cover - the vocabulary tables name no other M action
            raise AssertionError("internal: unhandled M action %r" % action)

    def _motion(self, number: int, motion_g: Word | None, letters: Mapping[str, Word],
                m_action: str | None) -> RawMove | None | bool:
        """The move of this line (None when it does not move, False when it is refused)."""
        motion_words = [w for letter, w in letters.items() if letter in self.vocab.motion_letters]
        if not motion_words:
            return None
        mode = int(motion_g.text) if motion_g is not None else self.mode
        first = min(motion_words, key=lambda w: w.column)
        if mode is None:
            self._report(ids.D_SYNTAX, number, first.column, "%s before any G0, G1, G2 or G3 has set a motion mode"
                         % first)
            return False
        ij = [letters[k] for k in ("I", "J") if k in letters]
        if mode in (0, 1) and ij:
            self._report(ids.D_ARC, number, ij[0].column, "%s on a G%d move: I and J belong to arcs (G2, G3)"
                         % (ij[0], mode))
            return False
        c_word = letters.get("C")
        if c_word is not None and mode != 0:
            self._report(ids.D_CSTEP, number, c_word.column,
                         "%s on a feed line (G%d): C is written only on G00 lines; what the firmware does with C on a "
                         "feed line is unknown (U-21), so it is refused" % (c_word, mode),
                         value=str(c_word), heading_c=_witness_number(c_word.value))
            return False
        start: XYZ = (self.x, self.y, self.z)
        end: XYZ = (
            letters["X"].value if "X" in letters else self.x,
            letters["Y"].value if "Y" in letters else self.y,
            letters["Z"].value if "Z" in letters else self.z,
        )
        centre = None
        full = False
        sweep = None
        if mode in (2, 3):
            arc = self._arc(number, mode, motion_g or first, letters, start, end)
            if arc is None:
                return False
            centre, full, sweep = arc
        if not self._outside_op_ok(number, mode, letters, start, end):
            return False
        return RawMove(
            kind=_KIND[mode], start=start, end=end, centre=centre, full_circle=full, sweep_deg=sweep,
            c_start=self.c, c_end=c_word.value if c_word is not None else self.c,
            feed=letters["F"].value if "F" in letters else self.feed,
            rpm=letters["S"].value if "S" in letters else self.rpm,
            spindle_on=self._spindle_after(m_action), tool=self._tool_number(letters),
            op=self.open_op.id if self.open_op is not None else None,
            written=frozenset(w.letter for w in motion_words), src=self._src(number))

    def _spindle_after(self, m_action: str | None) -> bool:
        """The spindle state the motion runs with: the M word of the line acts before the motion (gi-format 8.3)."""
        if m_action == "spindle_on":
            return True
        if m_action == "spindle_off":
            return False
        return self.spindle_on

    def _tool_number(self, letters: Mapping[str, Word]) -> int | None:
        if self.flavour == "gi":
            return self.tool_n
        if "T" in letters:
            return int(letters["T"].text)
        return self.t_number

    def _arc(self, number: int, mode: int, anchor: Word, letters: Mapping[str, Word], start: XYZ,
             end: XYZ) -> tuple[tuple[Decimal, Decimal], bool, float] | None:
        i_word, j_word = letters.get("I"), letters.get("J")
        if i_word is None and j_word is None:
            self._report(ids.D_ARC, number, anchor.column, "an arc needs its centre: at least one of I and J")
            return None
        sx, sy = start[0], start[1]
        if sx is None or sy is None:
            self._report(ids.D_ARC, number, anchor.column, "an arc from an unknown X or Y: the start of the arc is "
                                                           "not known yet")
            return None
        ex, ey = end[0], end[1]
        assert ex is not None and ey is not None
        i = i_word.value if i_word is not None else Decimal(0)
        j = j_word.value if j_word is not None else Decimal(0)
        if i == 0 and j == 0:
            self._report(ids.D_ARC, number, (i_word or j_word or anchor).column, "an arc of zero radius (I0 J0)")
            return None
        fsx, fsy, fex, fey = _frac(sx), _frac(sy), _frac(ex), _frac(ey)
        cx, cy = fsx + _frac(i), fsy + _frac(j)
        r1 = _frac(i) ** 2 + _frac(j) ** 2
        r2 = (fex - cx) ** 2 + (fey - cy) ** 2
        distance = (fex - fsx) ** 2 + (fey - fsy) ** 2
        full = distance == 0
        if not full and distance < ARC_AMBIGUOUS_SQ:
            self._report(ids.D_ARC, number, anchor.column,
                         "ambiguous arc: its end is %.4f mm from its start (0 < |end - start| < 0.01 mm), so it reads "
                         "equally as a full circle or as a sliver (red-team PHYS-16). Write the end equal to the "
                         "start for a full circle" % math.sqrt(float(distance)))
            return None
        tolerance = ARC_RADIUS_TOLERANCE ** 2
        lhs = r1 + r2 - tolerance
        if lhs > 0 and lhs * lhs > 4 * r1 * r2:
            self._report(ids.D_ARC, number, anchor.column,
                         "the start radius %.4f mm and the end radius %.4f mm differ by more than 0.005 mm"
                         % (math.sqrt(float(r1)), math.sqrt(float(r2))))
            return None
        centre = (_exact_add(sx, i), _exact_add(sy, j))
        if full:
            return centre, True, 360.0
        a0 = math.atan2(float(fsy - cy), float(fsx - cx))
        a1 = math.atan2(float(fey - cy), float(fex - cx))
        sweep = math.degrees(a1 - a0 if mode == 3 else a0 - a1) % 360.0
        if not 0.0 < sweep < 360.0:
            self._report(ids.D_ARC, number, anchor.column, "internal error: the sweep of an arc whose end differs "
                                                           "from its start computed as %r degrees" % sweep,
                         severity="internal-error")
            return None
        return centre, False, sweep

    def _stock_top(self) -> Decimal | None:
        zzero = self.stock_parts.get("zzero")
        if zzero == "top":
            return Decimal(0)
        if zzero == "bottom":
            return self.stock_parts.get("t")
        return None

    def _outside_op_ok(self, number: int, mode: int, letters: Mapping[str, Word], start: XYZ, end: XYZ) -> bool:
        """gi-format 7: in a file with OP lines, motion outside every block stays at or above the stock top."""
        if self.flavour != "gi" or not self.has_op_lines or self.open_op is not None:
            return True
        top = self._stock_top()
        if top is None:
            self._require_refused(ids.D_GI_OP, number, "motion outside OP blocks against the stock top")
            return False
        moves_xy = mode in (2, 3) or any(
            letter in letters and (start[k] is None or start[k] != end[k]) for k, letter in ((0, "X"), (1, "Y")))
        start_z, end_z = start[2], end[2]
        where = "the stock top (Z = %s with zzero=%s)" % (top, self.stock_parts.get("zzero"))
        if end_z is None:
            reason = "it ends at an unknown Z (Z is unknown until the program first writes it, and unknown is never " \
                     "taken as safe)"
        elif end_z < top:
            reason = "it ends at Z = %s, below %s" % (end_z, where)
        elif moves_xy and start_z is None:
            reason = "it moves in X or Y while Z is still unknown (never taken as safe); raise Z first"
        elif moves_xy and start_z < top:
            reason = "it moves in X or Y starting at Z = %s, below %s" % (start_z, where)
        else:
            return True
        self._report(ids.D_GI_OP, number, None,
                     "motion outside every OP block is air motion and must stay at or above %s: %s" % (where, reason))
        return False

    # -- the end --------------------------------------------------------------

    def _finish(self) -> None:
        last = max(1, len(self.lexed))
        if self.flavour == "gi":
            if self.header_open:
                self._close_header(last)
            if self.open_op is not None:
                self._report(ids.D_GI_OP, self.open_op.line, None, "OP %s is never closed by ;GOLIATH OPEND id=%s"
                             % (self.open_op.id, self.open_op.id))
                self.open_op = None
        elif self.flavour == "generic":
            g21_ok = self.g21_line is not None and (self.first_motion_line is None
                                                    or self.g21_line <= self.first_motion_line)
            try:
                self.unit_param = self.job.units_for(file_has_g21=g21_ok)
            except MissingValueError:
                at = self.first_motion_line or 1
                late = (" (its G21 at line %d comes after the first motion)" % self.g21_line
                        if self.g21_line is not None else "")
                self._report(ids.D_UNITS, at, None,
                             "the file has no G21 before its first motion%s and job.toml has no units: the unit is "
                             "unknown. Set the CAM to write G21, or write units = \"mm\" in job.toml" % late)
                self.unit_param = None

    def _units_param(self) -> Param[str] | None:
        if self.flavour == "gi":
            if self.units is None:
                return None
            return Param("mm", "enum", "OWNER_JOB", "GI UNITS (line %d)" % self.units.line)
        if self.flavour == "dialect":
            return Param("mm", "enum", "SPEC", "spec 05 §Number formatting: the Goliath dialect is millimetres, "
                                               "implicit")
        return self.unit_param

    def _result(self, *, stopped: bool) -> ReadResult:
        header = None
        if not stopped and self.flavour == "gi":
            parts = (self.source, self.units, self.stock, self.tower2, self.tool)
            if all(part is not None for part in parts) and not self.zshift_bad:
                header = GIHeader(fl.GI_VERSION, self.source, self.units, self.stock, self.tower2,  # type: ignore[arg-type]
                                  self.tool, self.zshift, tuple(self.ops))  # type: ignore[arg-type]
        if self.flavour == "gi":
            bound = self.bound if not stopped else {}
            tool = self.tool_n
        else:
            bound = fl.job_bound(self.job) if not stopped else {}
            tool = self.t_number
        program = RawProgram(
            file=self.file, flavour=self.flavour, line_count=len(self.lexed), items=tuple(self.items),
            ops=tuple(self.ops), tool=tool, units=None if stopped else self._units_param(), end_line=self.ended_at)
        notes = tuple(ReadNote(kind, text, tuple(lines)) for kind, (text, lines) in self.notes.items())
        return ReadResult(program_raw=program, gi_header=header, findings=tuple(self.findings), notes=notes,
                          disclosures=tuple(self.disclosures), bound=MappingProxyType(dict(bound)),
                          flavour=self.flavour)


@dataclass(frozen=True)
class _Bad:
    """A refusal found while classifying a word or typing an annotation value."""

    reason: str
    check_id: str | None = None
