# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The normaliser: the raw program as cuts and links (unit P03; the project design, section 4.4).

``normalise(source, job) -> Program`` turns what the strict reader read
(:mod:`goliath_prep.gcode`) into the intermediate representation of
:mod:`goliath_prep.ir`. In order:

1. **Frame** (safety-model 3.1, 3.2). A GI or generic program is shifted by
   job.toml's ``[placement] dx, dy`` and, only when job.toml says
   ``z_zero = "bottom"`` (written by the owner), by -T in Z, exactly, on the
   decimals as written. ``ZSHIFT`` is never applied (the reader recorded it).
   The Goliath dialect is never shifted: it is the robot's own program, in
   the panel frame with Z = 0 at the stock top (spec 05, spec 08).
2. **Arcs** (safety-model 3.5, red-team PHYS-16). The reader's full-circle
   rule is applied as the reader decided it: a full circle sweeps 360
   degrees and is never re-derived from floats. A helical arc (Z changes
   along it) becomes G01 segments within ``helix_tol`` (0.02 mm). An arc of
   180 degrees or more becomes equal parts of at most 90 degrees: from 180
   degrees on, start, centre and end no longer tell the direction, and spec
   05 says the robot finds it with a three-point test (pinned reading: the
   design says "over 180"; exactly 180 is split too). A full circle becomes
   four quarters. Every new point is rounded to the 0.001 mm grid the robot's
   program is written with, and the tolerance includes that rounding. Every
   arc's sweep is recorded (:class:`~goliath_prep.ir.ArcRecord`).
3. **Cuts and links** (safety-model 3.3), on the normalised moves, by Z only:
   in material when the lowest Z is below -``eps_surf``; a cut runs from the
   move that goes below to the move that returns to z >= 0. A move between
   -``eps_surf`` and 0 inside a cut stays in it (pinned reading).
4. **Operations** (safety-model 3.4). With GI ``OP`` blocks a cut takes its
   block's operation and forced heading; a cut whose moves lie in two blocks,
   or a through flag the geometry contradicts, is an ``N-OPS-MISMATCH``
   warning and the geometry is used. Without blocks the operations are
   inferred (one per cut) and that is a note. ``through`` is max depth >=
   T - ``through_tol`` (0.05 mm), compared exactly.

**Nothing changes silently.** Every transformation and every
reinterpretation is a :class:`~goliath_prep.ir.Note` naming its lines; the
reader's notes and disclosures travel with the program when it is given the
:class:`~goliath_prep.gcode.ReadResult` (the preferred input).

**Refusals.** A program that cannot be normalised safely raises
:class:`NormaliseError`, naming the line: a refused read; a move whose
position is unknown where it matters (it moves in X or Y while Z is unknown,
or goes into the material from an unknown position: unknown is never taken
as safe, as gi-format 7 says for files with operation blocks); a cut that two
operation blocks force to different headings (a forced heading is never
ignored, gi-format 7); ``z_zero = "bottom"`` that the owner did not write; a
job other than the one the file was read with. The only moves allowed from an
unknown position are links: a Z-only move to a known height out of the
material, and an X/Y move at a known height out of the material. They are
noted and have no time pieces.
"""
from __future__ import annotations

import dataclasses
import math
from collections.abc import Iterable, Sequence
from decimal import Decimal, Inexact, localcontext
from fractions import Fraction
from typing import Any

from goliath_prep.core import ids
from goliath_prep.core.disclosure import Disclosure
from goliath_prep.core.findings import Finding, SourceRef, Witness
from goliath_prep.core.params import Param
from goliath_prep.defaults import SPEC_PARAMS
from goliath_prep.gcode.flavours import GIOp
from goliath_prep.gcode.parser import RawEvent, RawMove, RawProgram, ReadNote, ReadResult
from goliath_prep.ir import (
    XYZ,
    ArcRecord,
    Cut,
    Link,
    Move,
    MoveOrigin,
    Note,
    Program,
    ProgramEvent,
    ProgramMeta,
    ToolUse,
)
from goliath_prep.jobspec import JobSpec
from goliath_prep.pieces import ARC_PART_MAX_DEG, ArcPath, length_mm

__all__ = ["ARC_COLLINEAR_MM", "ARC_PART_MAX_DEG", "ARC_SPLIT_FROM_DEG", "GRID_MM", "NormaliseError", "normalise"]

ARC_SPLIT_FROM_DEG = 180.0
"""Arcs sweeping this much or more are split (start, centre and end no longer tell the direction)."""
ARC_COLLINEAR_MM = Decimal("0.01")
"""An arc whose centre lies closer than this to the line through its start and end is split too: so close to
collinear, rounding the program to 0.001 mm could turn a three-point direction test the other way."""
GRID_MM = Decimal("0.001")
"""New points are rounded to this grid: the resolution of the robot's program (spec 05 §Number formatting)."""

_GRID = Fraction(1, 1000)
_ROUND_XYZ = Fraction(866026, 1_000_000_000)  # sqrt(3) * 0.0005 mm, rounded up: a point rounded in X, Y and Z


class NormaliseError(ValueError):
    """The program cannot be normalised safely; ``src`` names the line, ``findings`` a refused read's refusals."""

    def __init__(self, message: str, *, src: SourceRef | None = None, findings: Iterable[Finding] = ()) -> None:
        super().__init__(message)
        self.src = src
        self.findings = tuple(findings)


def normalise(source: ReadResult | RawProgram, job: JobSpec) -> Program:
    """Normalise a program read by :func:`goliath_prep.gcode.read_gcode` (see the module documentation).

    ``source`` is the reader's :class:`~goliath_prep.gcode.ReadResult`
    (preferred: a refused read is refused here, and the reader's notes and
    disclosures are carried) or its bare
    :class:`~goliath_prep.gcode.RawProgram` (the caller then answers for the
    read not being refused, and a note says the reader's notes are elsewhere).
    ``job`` is the job.toml the file was read with.

    Raises :class:`NormaliseError` when the program cannot be normalised
    safely, ``TypeError`` for arguments of the wrong type.
    """
    if not isinstance(job, JobSpec):
        raise TypeError("normalise needs the job as a JobSpec (goliath_prep.jobspec), got %s" % type(job).__name__)
    if isinstance(source, ReadResult):
        if source.refused:
            refusals = source.refusals()
            shown = "; ".join("%s: %s" % (f.check_id, f.message) for f in refusals[:5])
            more = "" if len(refusals) <= 5 else " (and %d more)" % (len(refusals) - 5)
            first = refusals[0].witness.src if refusals[0].witness is not None else None
            raise NormaliseError("the read of %s was refused, so it is never normalised or planned: %s%s"
                                 % (source.program_raw.file, shown, more), src=first, findings=refusals)
        _same_job(source, job)
        return _Normaliser(source.program_raw, job, source.notes, source.disclosures, True).run()
    if isinstance(source, RawProgram):
        return _Normaliser(source, job, (), (), False).run()
    raise TypeError("normalise needs the reader's ReadResult (or its RawProgram), got %s" % type(source).__name__)


def _same_job(result: ReadResult, job: JobSpec) -> None:
    """The values the file was read against (``bound``) are the job's: a different job.toml is refused."""
    params = job.params()
    for name in ("panel_width", "panel_height", "stock_thickness", "z_zero", "placement_dx", "placement_dy",
                 "tower2_axis", "tool_diameter", "tool_flute_length"):
        bound = result.bound.get(name)
        if bound is None:
            continue
        mine = params.get(name)
        if mine is None or mine.value != bound.value:
            raise NormaliseError(
                "%s was read with %s = %r (%s), but normalise was given a job with %s = %r: normalise with the job "
                "the file was read with" % (result.program_raw.file, name, bound.value, bound.ref, name,
                                            None if mine is None else mine.value))


# ---------------------------------------------------------------------------
# Exact helpers
# ---------------------------------------------------------------------------


def _dec(param: Param[Any]) -> Decimal:
    """A catalogue or job.toml number as the decimal it is written as (at most three decimals)."""
    return Decimal(repr(param.value))


def _on_grid(value: Fraction) -> Decimal:
    """``value`` rounded to the 0.001 mm grid, half to even, as an exact Decimal."""
    steps = round(value / _GRID)  # Fraction.__round__: half to even
    return Decimal("%de-3" % steps)  # the constructor is exact, whatever the decimal context


def _exact_add(a: Decimal, b: Decimal) -> Decimal:
    """a + b with no rounding, whatever the number of digits or the caller's decimal context."""
    exponent = min(int(a.as_tuple().exponent), int(b.as_tuple().exponent))
    top = max(a.adjusted(), b.adjusted(), 0)
    with localcontext() as ctx:
        ctx.prec = max(28, top - exponent + 3)
        ctx.traps[Inexact] = True
        return a + b


def _grid_float(value: float) -> Decimal:
    return _on_grid(Fraction(value))


def _needs_split(raw: RawMove, a: XYZ, b: XYZ, centre: tuple[Decimal, Decimal]) -> bool:
    """A full circle, an arc of 180 degrees or more, or one whose centre is within ARC_COLLINEAR_MM of its chord's line.

    Decided exactly on the decimals: with u = start - centre and v = end -
    centre, the cross product u x v is positive for a counter-clockwise turn
    of less than 180 degrees (negative for clockwise), zero at 180 degrees,
    and the centre's distance from the chord's line is |u x v| / |end - start|.
    """
    if raw.full_circle:
        return True
    cx, cy = Fraction(centre[0]), Fraction(centre[1])
    ux, uy = Fraction(a[0]) - cx, Fraction(a[1]) - cy  # type: ignore[arg-type]
    vx, vy = Fraction(b[0]) - cx, Fraction(b[1]) - cy  # type: ignore[arg-type]
    cross = ux * vy - uy * vx
    turn = cross if raw.kind == "arc_ccw" else -cross
    if turn <= 0:
        return True
    chord_sq = (vx - ux) ** 2 + (vy - uy) ** 2
    return cross * cross < Fraction(ARC_COLLINEAR_MM) ** 2 * chord_sq


def _plus(value: Decimal | None, by: Decimal) -> Decimal | None:
    return None if value is None else _exact_add(value, by)


def _lines(numbers: Iterable[int]) -> tuple[int, ...]:
    return tuple(sorted(set(numbers)))


# ---------------------------------------------------------------------------
# The normaliser
# ---------------------------------------------------------------------------


class _Normaliser:
    def __init__(self, raw: RawProgram, job: JobSpec, reader_notes: Sequence[ReadNote],
                 reader_disclosures: Sequence[Disclosure], from_result: bool) -> None:
        self.raw = raw
        self.job = job
        self.reader_notes = tuple(reader_notes)
        self.reader_disclosures = tuple(reader_disclosures)
        self.from_result = from_result
        self.eps_param = SPEC_PARAMS["eps_surf"]
        self.through_param = SPEC_PARAMS["through_tol"]
        self.helix_param = SPEC_PARAMS["helix_tol"]
        self.eps = _dec(self.eps_param)
        self.through_tol = _dec(self.through_param)
        self.helix_tol = float(self.helix_param.value)
        self.thickness = _dec(job.panel.T)
        self.notes: dict[str, tuple[str, set[int]]] = {}
        self.disclosures: list[Disclosure] = []
        self.moves: list[Move] = []
        self.arcs: list[ArcRecord] = []
        self.events: list[ProgramEvent] = []
        self.findings: list[Finding] = []

    # -- notes -----------------------------------------------------------------

    def _note(self, kind: str, text: str, lines: Iterable[int] = ()) -> None:
        entry = self.notes.get(kind)
        if entry is None:
            self.notes[kind] = (text, set(lines))
        else:
            entry[1].update(lines)

    def _disclose(self, disclosures: Iterable[Disclosure]) -> None:
        for d in disclosures:
            if d not in self.disclosures:
                self.disclosures.append(d)

    # -- the frame ---------------------------------------------------------------

    def _frame(self) -> None:
        job = self.job
        file = self.raw.file
        if self.raw.flavour == "dialect":
            self.dx = self.dy = self.dz = Decimal(0)
            self.z_zero = Param("top", "enum", "SPEC", "spec 05 §Semantics a replacement must match: the Goliath "
                                                       "dialect has Z = 0 at the stock top")
            frame_ref = "spec 08 §Coordinate frame and inputs: the Goliath dialect is in the panel frame"
            self.placement = (Param(0.0, "mm", "SPEC", frame_ref), Param(0.0, "mm", "SPEC", frame_ref))
            ignored = []
            if job.z_zero.value == "bottom":
                ignored.append('z_zero = "bottom"')
            if job.placement.dx.value != 0 or job.placement.dy.value != 0:
                ignored.append("[placement] dx = %r mm, dy = %r mm" % (job.placement.dx.value, job.placement.dy.value))
            if ignored:
                self._note("dialect-frame", "%s is read as the Goliath dialect, the robot's own program: it is in the "
                                            "panel frame with Z = 0 at the stock top (spec 05, spec 08), so job.toml's "
                                            "%s, which describe%s a CAM file, %s not applied"
                           % (file, " and ".join(ignored), "" if len(ignored) > 1 else "s",
                              "are" if len(ignored) > 1 else "is"))
            return
        self.placement = (job.placement.dx, job.placement.dy)
        self.dx, self.dy = _dec(job.placement.dx), _dec(job.placement.dy)
        self._disclose(job.placement.dx.all_disclosures())
        self._disclose(job.placement.dy.all_disclosures())
        if self.dx != 0 or self.dy != 0:
            self._note("placement-shift", "every X and Y of the program, arc centres included, shifted by dx = %s mm, "
                                          "dy = %s mm (job.toml [placement]; safety-model 3.1)"
                       % (job.placement.dx.value, job.placement.dy.value))
        self.z_zero = job.z_zero
        self._disclose(job.z_zero.all_disclosures())
        if job.z_zero.value == "bottom":
            if job.z_zero.source != "OWNER_JOB":
                raise NormaliseError(
                    'z_zero = "bottom" comes from %s (%s), not from job.toml: a file is shifted by -T only when the '
                    'owner writes z_zero = "bottom" in job.toml (safety-model 3.2)' % (job.z_zero.source,
                                                                                         job.z_zero.ref))
            self.dz = self.thickness.copy_negate()
            self._note("z-zero-shift", 'every Z shifted by -T = %s mm: job.toml z_zero = "bottom" says Z = 0 is the '
                                       "stock bottom of the T = %s mm panel; after the shift Z = 0 is the stock top "
                                       "and cuts are negative (safety-model 3.2). GI ZSHIFT is never applied"
                       % (self.dz, job.panel.T.value))
        else:
            self.dz = Decimal(0)

    def _shift(self, point: XYZ) -> XYZ:
        return (_plus(point[0], self.dx), _plus(point[1], self.dy), _plus(point[2], self.dz))

    # -- the run -----------------------------------------------------------------

    def run(self) -> Program:
        raw = self.raw
        if raw.units is None or raw.units.value != "mm":
            raise NormaliseError("%s: its units are not established as millimetres (the reader refuses that as "
                                 "D-UNITS); a program is never normalised without them" % raw.file)
        self._frame()
        self._disclose(raw.units.all_disclosures())
        self._disclose(self.reader_disclosures)
        ops = {op.id: op for op in raw.ops}
        for item in raw.items:
            if isinstance(item, RawEvent):
                self.events.append(ProgramEvent(item.kind, item.value, item.src, len(self.moves)))
            else:
                self._move(item)
        cuts, links = self._segment(ops)
        self._program_notes(cuts, ops)
        notes = [Note(n.kind, n.text, _lines(n.lines), "reader") for n in self.reader_notes]
        if not self.from_result:
            self._note("reader-notes-absent", "normalised from a bare RawProgram: the reader's notes and disclosures "
                                              "are in its ReadResult and must be shown with this program")
        for kind, (text, lines) in self.notes.items():
            notes.append(Note(kind, text, _lines(lines), "normalise"))
        meta = ProgramMeta(
            file=raw.file, flavour=raw.flavour, line_count=raw.line_count, end_line=raw.end_line, units=raw.units,
            z_zero=self.z_zero, z_shift_mm=self.dz, placement_dx=self.placement[0], placement_dy=self.placement[1],
            stock_thickness=self.job.panel.T, eps_surf=self.eps_param, through_tol=self.through_param,
            helix_tol=self.helix_param, ops_source="blocks" if ops or any(m.op for m in self.moves) else "inferred",
            reader_notes=self.from_result)
        tool_source = "GI TOOL n and job.toml [tool]" if raw.flavour == "gi" else "job.toml [tool]"
        tools = (ToolUse(raw.tool, self.job.tool, tool_source),)
        return Program(cuts=tuple(cuts), links=tuple(links), tools=tools, meta=meta, notes=tuple(notes),
                       arcs=tuple(self.arcs), events=tuple(self.events), findings=tuple(self.findings),
                       disclosures=tuple(self.disclosures))

    # -- one source move -------------------------------------------------------------

    def _move(self, raw: RawMove) -> None:
        a = self._shift(raw.start)
        b = self._shift(raw.end)
        centre = None
        if raw.centre is not None:
            centre = (_exact_add(raw.centre[0], self.dx), _exact_add(raw.centre[1], self.dy))
        if None in a or None in b:
            self._unknown(raw, a, b)
            self._add(raw, "as-read", (1, 1), a, b, None, None, None, self._below(a[2], b[2]), None)
            return
        if not raw.is_arc:
            self._add(raw, "as-read", (1, 1), a, b, None, None, None, self._below(a[2], b[2]), None)
            return
        assert centre is not None and raw.sweep_deg is not None
        index = len(self.arcs)
        first = len(self.moves)
        cw = raw.kind == "arc_cw"
        path = ArcPath((float(a[0]), float(a[1])), (float(b[0]), float(b[1])),  # type: ignore[arg-type]
                       (float(centre[0]), float(centre[1])), cw, raw.sweep_deg)
        helical = a[2] != b[2]
        if helical:
            treatment = "linearised"
            self._helix(raw, a, b, path, index)
            self._note("helix", "helical arcs (Z changes along the arc) replaced by G01 segments within %s mm of the "
                                "arc (docs/prep/params.json helix_tol); new points on the 0.001 mm grid, which the "
                                "tolerance includes (safety-model 3.5)" % self.helix_param.value, [raw.src.line])
        elif _needs_split(raw, a, b, centre):
            treatment = "split"
            self._split(raw, a, b, centre, path, index)
            if raw.full_circle:
                self._note("full-circle-split", "full circles (end equal to start at file precision: 360 degrees, "
                                                "red-team PHYS-16) split into four quarters; quarter points on the "
                                                "0.001 mm grid", [raw.src.line])
            else:
                self._note("arc-split", "arcs of %d degrees or more, and arcs whose centre lies within %s mm of the "
                                        "line through their start and end, split into equal parts of at most %d "
                                        "degrees: there start, centre and end no longer tell the direction, which the "
                                        "robot finds with a three-point test (spec 05); decided exactly on the "
                                        "decimals; split points on the 0.001 mm grid"
                           % (ARC_SPLIT_FROM_DEG, ARC_COLLINEAR_MM, ARC_PART_MAX_DEG), [raw.src.line])
        else:
            treatment = "kept"
            self._add(raw, "as-read", (1, 1), a, b, centre, raw.sweep_deg, (path.r0, path.r1),
                      self._below(a[2], b[2]), index)
        self.arcs.append(ArcRecord(
            index=index, src=raw.src, direction="cw" if cw else "ccw", sweep_deg=raw.sweep_deg,
            full_circle=raw.full_circle, helical=helical, centre=centre, radius_start=path.r0, radius_end=path.r1,
            treatment=treatment, moves=tuple(range(first, len(self.moves))), cut=None))

    def _below(self, *zs: Decimal | None) -> bool:
        known = [z for z in zs if z is not None]
        return bool(known) and min(known) < self.eps.copy_negate()

    def _add(self, raw: RawMove, origin: MoveOrigin, part: tuple[int, int], a: XYZ, b: XYZ,
             centre: tuple[Decimal, Decimal] | None, sweep: float | None, radius: tuple[float, float] | None,
             in_material: bool, arc: int | None) -> None:
        kind = raw.kind if origin != "helix-segment" else "feed"
        self.moves.append(Move(
            index=len(self.moves), kind=kind, a=a, b=b, centre=centre, sweep_deg=sweep, radius=radius,
            in_material=in_material, c_start=raw.c_start, c_end=raw.c_end, feed=raw.feed, rpm=raw.rpm,
            spindle_on=raw.spindle_on, tool=raw.tool, op=raw.op, src=raw.src, origin=origin, part=part, arc=arc))

    def _unknown(self, raw: RawMove, a: XYZ, b: XYZ) -> None:
        """A move from (or to) a position the program has not fully written: a link, or refused."""
        line = raw.src.line
        where = "%s line %d" % (self.raw.file, line)
        unknown = ", ".join(axis for axis, s, e in zip("XYZ", a, b) if s is None or e is None)
        if self._below(a[2], b[2]):
            low = min(z for z in (a[2], b[2]) if z is not None)
            raise NormaliseError(
                "%s: it goes into the material (down to Z = %s) while the program has not written %s yet: a cut needs "
                "a known position, and unknown is never taken as safe. Make the CAM write X, Y and Z before cutting"
                % (where, low, unknown), src=raw.src)
        xy_written = raw.is_arc or "X" in raw.written or "Y" in raw.written
        if raw.c_end is not None and raw.c_end != raw.c_start and not xy_written:
            raise NormaliseError(
                "%s: it turns the robot (C %s) at a position the program has not written (%s unknown): unknown is "
                "never taken as safe" % (where, raw.c_end, unknown), src=raw.src)
        if xy_written:
            if a[2] is None or b[2] is None:
                raise NormaliseError(
                    "%s: it moves in X or Y while Z is unknown (the program has not written Z yet), and unknown is "
                    "never taken as safe: make the CAM raise Z on a line of its own first" % where, src=raw.src)
        elif b[2] is None:
            raise NormaliseError(
                "%s: it moves (or turns) at a position the program has not written (%s unknown): unknown is never "
                "taken as safe" % (where, unknown), src=raw.src)
        self._note("start-unknown", "moves before the program has written its whole position (X, Y and Z) are links "
                                    "from the robot's unknown start position (U-18): they stay out of the material and "
                                    "have no time pieces; the line from the robot's start pose to the first known "
                                    "position is the program's first line, which W-FIRST checks", [line])

    def _helix(self, raw: RawMove, a: XYZ, b: XYZ, path: ArcPath, index: int) -> None:
        n = path.parts_for(self.helix_tol - float(_ROUND_XYZ))
        za, zb = Fraction(a[2]), Fraction(b[2])  # type: ignore[arg-type]
        low, high = (a[2], b[2]) if za <= zb else (b[2], a[2])
        points: list[XYZ] = [a]
        for k in range(1, n):
            x, y = path.point(k, n)
            # Never deeper or higher than the arc's own ends (which may hold more than three decimals).
            z = min(max(_on_grid(za + (zb - za) * Fraction(k, n)), low), high)  # type: ignore[type-var]
            points.append((_grid_float(x), _grid_float(y), z))
        points.append(b)
        for k in range(n):
            p, q = points[k], points[k + 1]
            self._add(raw, "helix-segment", (k + 1, n), p, q, None, None, None, self._below(p[2], q[2]), index)

    def _split(self, raw: RawMove, a: XYZ, b: XYZ, centre: tuple[Decimal, Decimal], path: ArcPath,
               index: int) -> None:
        n = math.ceil(raw.sweep_deg / ARC_PART_MAX_DEG)  # type: ignore[operator]
        points: list[XYZ] = [a]
        for k in range(1, n):
            x, y = path.point(k, n)
            points.append((_grid_float(x), _grid_float(y), a[2]))
        points.append(b)
        sweep = raw.sweep_deg / n  # type: ignore[operator]
        cx, cy = float(centre[0]), float(centre[1])
        below = self._below(a[2])
        for k in range(n):
            p, q = points[k], points[k + 1]
            r0 = length_mm(float(p[0]) - cx, float(p[1]) - cy)  # type: ignore[arg-type]
            r1 = length_mm(float(q[0]) - cx, float(q[1]) - cy)  # type: ignore[arg-type]
            self._add(raw, "arc-part", (k + 1, n), p, q, centre, sweep, (r0, r1), below, index)

    # -- cuts and links --------------------------------------------------------------

    def _segment(self, ops: dict[str, GIOp]) -> tuple[list[Cut], list[Link]]:
        groups: list[tuple[str, list[Move], bool]] = []  # (cut|link, moves, open_at_end)
        in_cut = False
        for move in self.moves:
            if not move.located:
                if in_cut:  # pragma: no cover - a cut's moves start where a located move ended
                    raise NormaliseError("internal error: a move of unknown position inside a cut", src=move.src)
                self._append(groups, "link", move)
                continue
            if not in_cut and move.in_material:
                groups.append(("cut", [move], False))
                in_cut = True
            elif in_cut:
                groups[-1][1].append(move)
            else:
                self._append(groups, "link", move)
            if in_cut and move.b[2] >= 0:  # type: ignore[operator]
                in_cut = False
        if in_cut:
            kind, moves, _ = groups[-1]
            groups[-1] = (kind, moves, True)
            last = moves[-1]
            self._note("cut-open-at-end", "the program ends with the tool in or near the material (Z = %s at line %d): "
                                          "the end of the program closes the last cut" % (last.b[2], last.src.line),
                       [last.src.line])
        cuts: list[Cut] = []
        links: list[Link] = []
        pending: list[tuple[list[Move], int | None]] = []
        for kind, moves, open_at_end in groups:
            if kind == "cut":
                cuts.append(self._cut(len(cuts), moves, open_at_end, ops))
                for link_moves, after in pending:
                    links.append(Link(len(links), tuple(link_moves), after, cuts[-1].id))
                pending = []
            else:
                pending.append((moves, cuts[-1].id if cuts else None))
        for link_moves, after in pending:
            links.append(Link(len(links), tuple(link_moves), after, None))
        for cut in cuts:
            for move in cut.moves:
                if move.kind == "traverse":
                    self._note("rapid-in-cut", "G0 moves whose lowest point is below -%s mm belong to a cut: cuts and "
                                               "links are told apart by Z, not by the G word (safety-model 3.3)"
                               % self.eps_param.value, [move.src.line])
        for link in links:
            for move in link.moves:
                if move.kind != "traverse":
                    self._note("feed-link", "feed moves (G1, G2, G3) that stay above -%s mm belong to a link: cuts "
                                            "and links are told apart by Z, not by the G word, because some CAM "
                                            "programs reportedly write rapids as feeds (U-46; safety-model 3.3)"
                               % self.eps_param.value, [move.src.line])
        cut_of = {move.index: cut.id for cut in cuts for move in cut.moves}
        self.arcs = [self._with_cut(record, cut_of) for record in self.arcs]
        return cuts, links

    @staticmethod
    def _append(groups: list[tuple[str, list[Move], bool]], kind: str, move: Move) -> None:
        if groups and groups[-1][0] == kind and kind == "link":
            groups[-1][1].append(move)
        else:
            groups.append((kind, [move], False))

    @staticmethod
    def _with_cut(record: ArcRecord, cut_of: dict[int, int]) -> ArcRecord:
        """The record with the cut its moves lie in (a helix may start in a link and descend into a cut)."""
        for index in record.moves:
            if index in cut_of:
                return dataclasses.replace(record, cut=cut_of[index])
        return record

    def _cut(self, cut_id: int, moves: list[Move], open_at_end: bool, ops: dict[str, GIOp]) -> Cut:
        zmin = min(min(m.a[2], m.b[2]) for m in moves)  # type: ignore[type-var]
        max_depth = zmin.copy_negate()
        through = max_depth >= _exact_add(self.thickness, self.through_tol.copy_negate())
        op_ids: list[str] = []
        first_of: dict[str, Move] = {}
        for move in moves:
            if move.op is not None and move.op not in first_of:
                op_ids.append(move.op)
                first_of[move.op] = move
        op: GIOp | None = None
        forced: int | None = None
        if ops and not op_ids:  # pragma: no cover - the reader refuses motion in the material outside every block
            self._warn(cut_id, moves[0].src, "none",
                       "cut %d (lines %d to %d) lies outside every operation block of a file that has them"
                       % (cut_id, moves[0].src.line, moves[-1].src.line))
        if op_ids:
            unknown = [o for o in op_ids if o not in ops]
            if unknown:
                raise NormaliseError("internal error: moves name the operation %s, which the reader did not record"
                                     % unknown[0], src=first_of[unknown[0]].src)
            op = ops[op_ids[0]]
            forced = op.forced_c
            for other in op_ids[1:]:
                if ops[other].forced_c != forced:
                    src = first_of[other].src
                    raise NormaliseError(
                        "%s line %d: cut %d continues from operation %s (forced heading %s) into operation %s (forced "
                        "heading %s) without leaving the material. A cut has one heading, and a forced heading is "
                        "never ignored (gi-format 7): retract between the operations, or give them the same c"
                        % (self.raw.file, src.line, cut_id, op_ids[0], _c_text(forced), other,
                           _c_text(ops[other].forced_c)), src=src)
            if len(op_ids) > 1:
                src = first_of[op_ids[1]].src
                self._warn(cut_id, src, op_ids[1],
                           "cut %d (lines %d to %d) runs through the operation blocks %s without leaving the "
                           "material: a block's moves should form whole cuts. The cut is one cut, segmented by Z, "
                           "and is checked as one" % (cut_id, moves[0].src.line, moves[-1].src.line,
                                                      " and ".join(op_ids)))
            outside = [m for m in moves if m.op is None]
            tail_retract = (len(outside) == 1 and outside[0] is moves[-1] and not outside[0].moves_xy
                            and outside[0].b[2] >= 0)  # type: ignore[operator]
            if outside and not tail_retract:
                self._warn(cut_id, outside[0].src, op_ids[0],
                           "cut %d has moves outside every operation block (line %d) while its other moves lie in %s"
                           % (cut_id, outside[0].src.line, " and ".join(op_ids)))
        return Cut(id=cut_id, moves=tuple(moves), op=op, ops=tuple(op_ids), through=through, zmin=zmin,
                   max_depth=max_depth, forced_c=forced, open_at_end=open_at_end)

    def _warn(self, cut: int | None, src: SourceRef | None, op_id: str, message: str) -> None:
        self.findings.append(Finding.of(ids.N_OPS_MISMATCH, message,
                                        witness=Witness(cut=cut, src=src, value=op_id, hazard=message)))

    # -- program-level notes ----------------------------------------------------------

    def _program_notes(self, cuts: list[Cut], ops: dict[str, GIOp]) -> None:
        limit = _exact_add(self.thickness, self.through_tol.copy_negate())
        for op_id, op in ops.items():
            mine = [c for c in cuts if op_id in c.ops]
            if not mine:
                self._note("op-without-cut", "operation blocks that remove no material: no move of theirs goes below "
                                             "-%s mm (the lines are their OP lines)" % self.eps_param.value, [op.line])
            deepest = max((c.max_depth for c in mine), default=None)
            through = any(c.through for c in mine)
            src = mine[0].moves[0].src if mine else None
            cut_id = mine[0].id if mine else None
            if op.through is True and not through:
                depth = "removes no material" if deepest is None else "reaches %s mm below the stock top at most" % (
                    deepest)
                self._warn(cut_id, src, op_id,
                           "OP %s (line %d) says through=1, but it %s, less than T - %s = %s mm: the geometry is "
                           "used, and its cuts are not through cuts (safety-model 3.4)"
                           % (op_id, op.line, depth, self.through_param.value, limit))
            elif op.through is False and through:
                first = next(c for c in mine if c.through)
                self._warn(first.id, first.moves[0].src, op_id,
                           "OP %s (line %d) says through=0, but its cut %d reaches %s mm below the stock top, at "
                           "least T - %s = %s mm: the geometry is used, and the cut is a through cut "
                           "(safety-model 3.4)" % (op_id, op.line, first.id, first.max_depth,
                                                   self.through_param.value, limit))
        if not ops and not any(m.op for m in self.moves):
            self._note("ops-inferred", "the file has no OP blocks: each cut is its own operation, inferred from the "
                                       "segmentation by Z (N-INFERRED-OPS; safety-model 3.4)")
        if not cuts:
            text = "the program removes no material: no move goes below Z = -%s mm" % self.eps_param.value
            if self.raw.flavour != "dialect" and self.dz == 0:
                text += (' (Z = 0 is taken as the stock top: %s). If the CAM\'s Z zero is the stock bottom, write '
                         'z_zero = "bottom" in job.toml' % self.z_zero.ref)
            elif self.dz != 0:
                text += " after the shift by -T; check the CAM's Z zero against job.toml's z_zero"
            self._note("no-cuts", text)
            if self.raw.flavour != "dialect":
                self._disclose([Disclosure(id="NORMALISE:no-cuts", severity="amber", text=text, source=self.raw.file)])


def _c_text(c: int | None) -> str:
    return "none" if c is None else "c=%d" % c
