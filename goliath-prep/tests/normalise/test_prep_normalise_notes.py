# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Every transformation is a normalisation note (unit P03; the project design, section 4.4; safety-model 3.7).

Owner rule: nothing changes silently. The normaliser moves coordinates (the
placement shift, the Z-zero shift), replaces moves (helices by G01 segments,
arcs of 180 degrees or more and full circles by parts) and reinterprets them
(a G0 inside a cut, a feed inside a link, moves from the unknown start): each
of these is a note naming its lines, or the whole program when it applies to
every coordinate. The property test compares every move of the normalised
program with the move the reader read and requires a note for each difference.
"""
from __future__ import annotations

import re
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from prep_normalise_support import job, note_lines, notes_of, programs, read

from goliath_prep.core.canon import canonical_json
from goliath_prep.gcode.parser import RawMove
from goliath_prep.ir import NOTE_KINDS, Move, Program
from goliath_prep.normalise import normalise

D = Decimal

EVERYTHING = [
    "G21",                                          # 1
    "G0 Z10.000",                                   # 2  start-unknown
    "G0 X1000.000 Y500.000",                        # 3  start-unknown
    "M3 S18000",                                    # 4
    "G1 Z5.000 F500",                               # 5  feed-link
    "G1 Z-3.000 F200",                              # 6
    "G2 X1000.000 Y500.000 I50.000 J0.000 F600",    # 7  full-circle-split
    "G2 X1100.000 Y500.000 I50.000 J0.000",         # 8  arc-split (180 degrees)
    "G3 X1150.000 Y550.000 Z-6.000 I0.000 J50.000",  # 9  helix
    "G0 X1200.000",                                 # 10 rapid-in-cut
    "G0 Z10.000",                                   # 11 rapid-in-cut (retract)
    "G0 X1300.000 Y600.000",                        # 12
    "G1 Z-2.000 F200",                              # 13
    "G1 X1400.000",                                 # 14 cut-open-at-end
    "M30",                                          # 15
]


def lift_z(lines: list[str], by: str) -> list[str]:
    """The same program written with Z = 0 ``by`` mm lower (at the stock bottom of a panel ``by`` mm thick)."""
    return [_Z_WORD.sub(lambda m: "Z%s" % (D(m.group(1)) + D(by)).quantize(D("0.001")), line) for line in lines]


_Z_WORD = re.compile(r"Z(-?[0-9]+\.[0-9]+)")
BOTTOM_EVERYTHING = lift_z(EVERYTHING, "18")


def test_every_transform_has_a_note() -> None:
    spec = job(units="mm", z_zero="bottom", placement={"dx": 10.0, "dy": -5.0})
    program = normalise(read(BOTTOM_EVERYTHING, job_spec=spec), spec)
    assert note_lines(program, "start-unknown") == {2, 3}
    assert note_lines(program, "feed-link") == {5}
    assert note_lines(program, "full-circle-split") == {7}
    assert note_lines(program, "arc-split") == {8}
    assert note_lines(program, "helix") == {9}
    assert note_lines(program, "rapid-in-cut") == {10, 11}
    assert note_lines(program, "cut-open-at-end") == {14}
    assert note_lines(program, "ops-inferred") == set()  # a note about the whole program
    for kind in ("placement-shift", "z-zero-shift", "ops-inferred"):
        (note,) = notes_of(program, kind)
        assert note.lines == ()
    # The reader's own notes travel with the program (G21 accepted, M30 dropped).
    assert {"noop", "program-end-dropped"} <= {n.kind for n in program.notes if n.stage == "reader"}
    for note in program.notes:
        assert note.kind in NOTE_KINDS or note.stage == "reader"
        assert note.text.strip()
        assert list(note.lines) == sorted(set(note.lines))
        canonical_json(note.to_json())


def _expected_kinds(program: Program, move: Move, raw: RawMove) -> set[str]:
    """The note kinds that must cover ``move``'s line, from how it differs from the move the reader read."""
    kinds: set[str] = set()
    if move.origin == "helix-segment":
        kinds.add("helix")
    elif move.origin == "arc-part":
        kinds.add("full-circle-split" if raw.full_circle else "arc-split")
    if not move.located:
        kinds.add("start-unknown")
    in_cut = any(move in cut.moves for cut in program.cuts)
    if in_cut and move.kind == "traverse":
        kinds.add("rapid-in-cut")
    if not in_cut and move.kind != "traverse":
        kinds.add("feed-link")
    return kinds


def _shifted(value: Decimal | None, by: Decimal) -> Decimal | None:
    return None if value is None else value + by


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(lines=programs(), dx=st.sampled_from([0.0, 12.5]), bottom=st.booleans())
def test_every_difference_from_the_read_program_has_a_note(lines: list[str], dx: float, bottom: bool) -> None:
    overrides = {"units": "mm", "placement": {"dx": dx, "dy": 0.0}}
    if bottom:
        overrides["z_zero"] = "bottom"
    spec = job(**overrides)
    result = read(lift_z(lines, "18") if bottom else lines, job_spec=spec)
    program = normalise(result, spec)
    raw_by_line = {m.src.line: m for m in result.program_raw.moves}
    shift_x = D(repr(dx))
    shift_z = D("-18.0") if bottom else D(0)
    assert bool(notes_of(program, "placement-shift")) == (dx != 0.0)
    assert bool(notes_of(program, "z-zero-shift")) == bottom
    for move in program.moves:
        raw = raw_by_line[move.src.line]
        for kind in _expected_kinds(program, move, raw):
            assert move.src.line in note_lines(program, kind), (kind, move)
        if move.origin == "as-read":
            # A move kept as read differs from the read move only by the shifts, which are whole-program notes.
            assert move.kind == raw.kind
            assert move.a == (_shifted(raw.start[0], shift_x), raw.start[1], _shifted(raw.start[2], shift_z))
            assert move.b == (_shifted(raw.end[0], shift_x), raw.end[1], _shifted(raw.end[2], shift_z))
        else:
            assert move.src.line in (note_lines(program, "helix") | note_lines(program, "arc-split")
                                     | note_lines(program, "full-circle-split"))
    # Lines that the reader read as one move and the normaliser keeps as one move get no transformation note.
    transformed = note_lines(program, "helix") | note_lines(program, "arc-split") | note_lines(
        program, "full-circle-split")
    for line in transformed:
        made = [m for m in program.moves if m.src.line == line]
        assert len(made) >= 2 or made[0].origin == "helix-segment", made
