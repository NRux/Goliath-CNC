# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Cuts and links by Z, through cuts, the Z zero, unknown positions and operations (unit P03).

The project design, section 4.4, and docs/prep/safety-model.md sections 3.2 to
3.4: a move is in material when its lowest Z is below -0.02 mm; a cut runs from
the move that goes below -0.02 mm to the move that returns to z >= 0;
everything else is a link; the G word is never consulted. A cut is through
when its deepest point is at least T - 0.05 mm below the stock top, compared
exactly. Z = 0 at the stock bottom is shifted by -T only when job.toml says so.
"""
from __future__ import annotations

import dataclasses
from decimal import Decimal, localcontext

import pytest
from prep_normalise_support import (
    GI_HEADER,
    JOB,
    JOB_MM,
    job,
    lines_of,
    norm,
    norm_gi,
    note_lines,
    notes_of,
    read,
    read_gi,
)

from goliath_prep.core.params import Param
from goliath_prep.gcode import read_gcode
from goliath_prep.ir import Program
from goliath_prep.normalise import NormaliseError, normalise
from goliath_prep.pieces import pieces

D = Decimal


def _cut_lines(program: Program) -> list[list[int]]:
    return [lines_of(cut.moves) for cut in program.cuts]


def _link_lines(program: Program) -> list[list[int]]:
    return [lines_of(link.moves) for link in program.links]


# ---------------------------------------------------------------------------
# Cuts and links by Z
# ---------------------------------------------------------------------------

SEGMENTED = [
    "G21",                  # 1
    "G90",                  # 2
    "G0 Z10.000",           # 3  link: Z-only raise from the unknown start
    "G0 X100.000 Y100.000",  # 4  link: XY from the unknown start
    "G1 Z5.000 F500",       # 5  link: a feed above the surface
    "G1 Z-3.000 F200",      # 6  cut 0 starts: the move that goes below -0.02
    "G1 X200.000",          # 7  cut 0
    "G0 X250.000",          # 8  cut 0: a G0 in the material is a cut move
    "G1 Z1.000",            # 9  cut 0 ends: back to z >= 0
    "G1 X400.000 F2000",    # 10 link: a rapid written as a feed
    "G0 Z-0.020",           # 11 link: exactly -0.02 is not in the material
    "G0 X420.000",          # 12 link at -0.02
    "G0 Z-0.021",           # 13 cut 1 starts: a rapid below -0.02
    "G1 X440.000",          # 14 cut 1
    "G0 Z10.000",           # 15 cut 1 ends
    "M30",                  # 16
]


def test_cut_segmentation_by_z_not_gword() -> None:
    program = norm(SEGMENTED)
    assert _cut_lines(program) == [[6, 7, 8, 9], [13, 14, 15]]
    assert _link_lines(program) == [[3, 4, 5], [10, 11, 12]]
    assert [c.id for c in program.cuts] == [0, 1]
    assert [link.id for link in program.links] == [0, 1]
    assert (program.links[0].after_cut, program.links[0].before_cut) == (None, 0)
    assert (program.links[1].after_cut, program.links[1].before_cut) == (0, 1)
    # The G word is kept as written; only Z decides cut or link.
    by_line = {m.src.line: m for m in program.moves}
    assert by_line[8].kind == "traverse" and by_line[8].in_material
    assert by_line[10].kind == "feed" and not by_line[10].in_material
    assert by_line[11].kind == "traverse" and not by_line[11].in_material
    assert by_line[13].in_material
    # Reinterpretations are notes: G0 moves inside cuts, feed moves inside links (U-46).
    assert {8, 13, 15} <= note_lines(program, "rapid-in-cut")
    assert {5, 10} <= note_lines(program, "feed-link")
    assert 6 not in note_lines(program, "rapid-in-cut")
    # Moves are numbered in program order and each belongs to exactly one cut or link.
    assert [m.index for m in program.moves] == list(range(len(program.moves)))
    assert [c.zmin for c in program.cuts] == [D("-3.000"), D("-0.021")]


def test_band_between_minus_eps_and_zero_stays_in_the_cut() -> None:
    """Pinned reading: a move between -0.02 and 0 inside a cut does not end it (the cut ends at z >= 0)."""
    program = norm(["G21", "G0 Z5.000", "G0 X100.000 Y100.000", "G1 Z-2.000 F100", "G1 Z-0.010",
                    "G1 X300.000", "G1 Z-2.000", "G1 X400.000", "G0 Z5.000"])
    assert _cut_lines(program) == [[4, 5, 6, 7, 8, 9]]
    by_line = {m.src.line: m for m in program.moves}
    assert not by_line[6].in_material  # skims at -0.01: in the cut, but removes nothing
    assert by_line[5].in_material  # rises from -2 to -0.01: its lowest point is in the material


def test_a_cut_open_at_the_program_end_is_closed_with_a_note() -> None:
    program = norm(["G21", "G0 Z5.000", "G0 X100.000 Y100.000", "G1 Z-2.000 F100", "G1 X300.000", "M30"])
    assert _cut_lines(program) == [[4, 5]]
    assert program.cuts[0].open_at_end
    assert note_lines(program, "cut-open-at-end") == {5}


def test_a_program_without_cuts_is_noted_and_disclosed() -> None:
    program = norm(["G21", "G0 Z5.000", "G0 X100.000 Y100.000", "G1 Z0.000 F100", "G1 X300.000", "G0 Z5.000"])
    assert program.cuts == ()
    assert notes_of(program, "no-cuts")
    disclosure = [d for d in program.disclosures if d.id == "NORMALISE:no-cuts"]
    assert disclosure and disclosure[0].severity == "amber"


# ---------------------------------------------------------------------------
# Through cuts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "thickness, depth, through",
    [
        ("18", "17.950", True),
        ("18", "17.949", False),
        ("18", "18.300", True),
        # 3.2 - 0.05 is 3.1500000000000004 in binary floating point: only an exact comparison says through.
        ("3.2", "3.150", True),
        ("3.2", "3.149", False),
        ("17.6", "17.550", True),
        ("17.6", "17.549", False),
    ],
)
def test_through_classification_depth_ge_T_minus_0p05(thickness: str, depth: str, through: bool) -> None:
    spec = job(units="mm", panel={"T": float(thickness)})
    program = norm(["G21", "G0 Z5.000", "G0 X100.000 Y100.000", "G1 Z-%s F100" % depth, "G1 X300.000",
                    "G0 Z5.000"], job_spec=spec)
    (cut,) = program.cuts
    assert cut.max_depth == D(depth)
    assert cut.through is through
    assert program.meta.through_tol.value == 0.05
    assert program.meta.stock_thickness.value == float(thickness)


def _gi_through(flag: str, depth: str) -> list[str]:
    return [
        ";GOLIATH OP id=op1 kind=profile through=%s tool=1" % flag,  # 7
        "G0 Z10.000",
        "G0 X100.000 Y100.000",
        "M3 S18000",
        "G1 Z-%s F200" % depth,                                      # 11
        "G1 X300.000 F800",
        "G0 Z10.000",
        ";GOLIATH OPEND id=op1",
        "M5",
    ]


def test_gi_through_flag_contradicted_by_geometry_warns_and_geometry_wins() -> None:
    claimed = norm_gi(_gi_through("1", "6.000"))
    assert claimed.cuts[0].through is False
    (warning,) = [f for f in claimed.findings if f.check_id == "N-OPS-MISMATCH"]
    assert warning.severity == "warn"
    assert warning.witness.cut == 0 and warning.witness.value == "op1"
    assert warning.witness.src is not None and warning.witness.src.line == 11
    assert "through=1" in warning.message and "line 7" in warning.message
    denied = norm_gi(_gi_through("0", "18.100"))
    assert denied.cuts[0].through is True
    assert [f.check_id for f in denied.findings] == ["N-OPS-MISMATCH"]
    agreed = norm_gi(_gi_through("1", "18.100"))
    assert agreed.cuts[0].through is True and agreed.findings == ()
    auto = norm_gi(_gi_through("auto", "6.000"))
    assert auto.cuts[0].through is False and auto.findings == ()


# ---------------------------------------------------------------------------
# Z zero
# ---------------------------------------------------------------------------

BOTTOM_ZERO = ["G21", "G0 Z23.000", "G0 X100.000 Y100.000", "G1 Z12.000 F200", "G1 X200.000", "G0 Z23.000"]
"""A program written with Z = 0 at the stock bottom of an 18 mm panel: it cuts 6 mm deep."""


def test_zzero_bottom_requires_explicit_option() -> None:
    # job.toml silent: no shift and no guessing; the program then cuts nothing, which is noted and disclosed,
    # and the default it relied on (Z = 0 at the stock top) is disclosed too.
    silent = norm(BOTTOM_ZERO, job_spec=job(units="mm", z_zero=None))
    assert silent.cuts == ()
    assert silent.meta.z_shift_mm == 0
    assert silent.meta.z_zero.value == "top" and silent.meta.z_zero.source == "SPEC"
    assert not notes_of(silent, "z-zero-shift")
    (no_cuts,) = notes_of(silent, "no-cuts")
    assert 'z_zero = "bottom"' in no_cuts.text
    assert {"NORMALISE:no-cuts", "DEFAULT:job.z_zero"} <= {d.id for d in silent.disclosures}
    # job.toml says bottom: every Z is shifted by -T exactly, noted and disclosed.
    bottom = job(units="mm", z_zero="bottom")
    shifted = norm(BOTTOM_ZERO, job_spec=bottom)
    (cut,) = shifted.cuts
    assert cut.zmin == D("-6.000") and cut.max_depth == D("6.000")
    assert shifted.meta.z_shift_mm == D("-18.0")
    assert shifted.meta.z_zero.source == "OWNER_JOB"
    by_line = {m.src.line: m for m in shifted.moves}
    assert by_line[2].b[2] == D("5.000") and by_line[4].b[2] == D("-6.000")
    (note,) = notes_of(shifted, "z-zero-shift")
    assert note.lines == () and "-18" in note.text and "z_zero" in note.text
    assert "JOB:z_zero=bottom" in {d.id for d in shifted.disclosures}


def test_gi_bottom_zero_needs_job_toml_to_say_so() -> None:
    header = [line.replace("zzero=top", "zzero=bottom") for line in GI_HEADER]
    body = ["G0 Z23.000", "G0 X100.000 Y100.000", "G1 Z12.000 F200", "G1 X200.000", "G0 Z23.000"]
    refused = read_gi(body, header=header, job_spec=JOB)
    assert refused.refused and any(f.check_id == "D-GI-CONFLICT" for f in refused.findings)
    with pytest.raises(NormaliseError, match="D-GI-CONFLICT"):
        normalise(refused, JOB)
    bottom = job(z_zero="bottom")
    program = norm_gi(body, header=header, job_spec=bottom)
    assert program.cuts[0].zmin == D("-6.000")
    assert notes_of(program, "z-zero-shift")


def test_bottom_zero_not_written_by_the_owner_is_refused() -> None:
    forged = dataclasses.replace(JOB_MM, z_zero=Param("bottom", "enum", "SPEC", "a value nobody wrote"))
    result = read(BOTTOM_ZERO, job_spec=forged)
    with pytest.raises(NormaliseError, match="z_zero"):
        normalise(result, forged)


def test_dialect_is_never_shifted() -> None:
    """The robot's dialect has Z = 0 at the stock top (spec 05) and is in the panel frame: no shift, a note."""
    spec = job(z_zero="bottom", placement={"dx": 10.0, "dy": 5.0})
    lines = ["M05", "G01 Z10.000 F300", "G00 X100.000 Y100.000 C90", "Z10.000", "M10", "G01 Z-3.000 F200 S18000",
             "G01 X200.000 F800", "G01 Z10.000", "M05"]
    result = read_gcode("\n".join(lines) + "\n", flavour="dialect", job=spec)
    assert not result.refused
    program = normalise(result, spec)
    assert program.meta.z_shift_mm == 0
    assert program.meta.placement_dx.value == 0 and program.meta.placement_dy.value == 0
    assert program.meta.z_zero.value == "top" and program.meta.z_zero.source == "SPEC"
    assert program.cuts[0].zmin == D("-3.000")
    assert program.cuts[0].moves[0].a[:2] == (D("100.000"), D("100.000"))
    assert notes_of(program, "dialect-frame")
    assert not notes_of(program, "z-zero-shift") and not notes_of(program, "placement-shift")
    # Headings are carried as written for verification.
    by_line = {m.src.line: m for m in program.moves}
    assert by_line[3].c_start is None and by_line[3].c_end == D("90")
    assert by_line[7].c_start == D("90") and by_line[7].c_end == D("90")


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


def test_placement_shift_applied_exactly_with_a_note() -> None:
    spec = job(units="mm", placement={"dx": 10.5, "dy": -20.25})
    program = norm(["G21", "G0 Z5.000", "G0 X100.000 Y100.000", "G1 Z-2.000 F100",
                    "G2 X200.000 Y100.000 I50.000 J0.000", "G0 Z5.000"], job_spec=spec)
    parts = [m for m in program.moves if m.src.line == 5]  # 180 degrees: two parts
    assert parts[0].a[:2] == (D("110.500"), D("79.750"))
    assert parts[0].b[:2] == (D("160.500"), D("129.750"))
    assert parts[-1].b[:2] == (D("210.500"), D("79.750"))
    assert all(p.centre == (D("160.500"), D("79.750")) for p in parts)
    (note,) = notes_of(program, "placement-shift")
    assert note.lines == () and "10.5" in note.text and "-20.25" in note.text
    assert program.meta.placement_dx.value == 10.5
    assert "JOB:placement" in {d.id for d in program.disclosures}


# ---------------------------------------------------------------------------
# Unknown positions (unknown is never taken as safe)
# ---------------------------------------------------------------------------


def test_moves_from_the_unknown_start_are_links_with_a_note() -> None:
    program = norm(["G21", "G0 Z10.000", "G0 X100.000 Y100.000", "G1 Z-1.000 F100", "G0 Z10.000"])
    first = program.links[0]
    assert lines_of(first.moves) == [2, 3] and not first.located
    assert not any(m.located for m in first.moves)
    assert first.moves[0].a == (None, None, None) and first.moves[0].b == (None, None, D("10.000"))
    assert note_lines(program, "start-unknown") == {2, 3}
    array = pieces(program, 50.0)
    assert array.unplaced == (0, 1)
    assert set(int(m) for m in array["move"]) == {2, 3}
    assert not notes_of(program, "xy-before-z")


@pytest.mark.parametrize(
    "lines, xy_lines",
    [
        # The project design's own GI example, and CAM posts that position X and Y before Z, do this.
        (["G21", "G0 X100.000 Y100.000", "G0 Z10.000", "G1 Z-1.000 F100", "G0 Z10.000"], {2}),
        (["G21", "G0 X0.000 Y0.000 Z15.000", "G0 X100.000 Y100.000", "G1 Z-1.000 F100", "G0 Z10.000"], {2}),
    ],
)
def test_xy_moves_before_any_z_are_links_noted_and_disclosed(lines: list[str], xy_lines: set[int]) -> None:
    """Pinned reading: nothing is concluded from them; the shipped program raises Z first (U-18, D-ZSAFE)."""
    program = norm(lines)
    assert note_lines(program, "xy-before-z") == xy_lines
    assert xy_lines <= note_lines(program, "start-unknown")
    (disclosure,) = [d for d in program.disclosures if d.id == "NORMALISE:xy-before-z"]
    assert disclosure.severity == "info" and "raises Z" in disclosure.text
    assert all(m.index in pieces(program, 50.0).unplaced for m in program.moves if m.src.line in xy_lines)
    assert len(program.cuts) == 1 and program.cuts[0].moves[0].located


@pytest.mark.parametrize(
    "lines, line, words",
    [
        (["G21", "G0 Z10.000", "G1 Z-2.000 F100"], 3, "unknown"),
        (["G21", "G0 Z10.000", "G0 X100.000", "G1 Z-2.000 F100"], 4, "unknown"),
        (["G21", "G0 X100.000 Y100.000", "G1 Z-2.000 F100"], 3, "has not written Z"),
        (["G21", "G0 X100.000 Y100.000 Z-1.000"], 2, "into the material"),
        (["G21", "G0 X100.000 Y100.000", "G2 X200.000 I50.000 J0.000"], 3, "an arc"),
    ],
)
def test_unknown_positions_where_they_matter_are_refused(lines: list[str], line: int, words: str) -> None:
    result = read(lines)
    with pytest.raises(NormaliseError) as info:
        normalise(result, JOB_MM)
    error = info.value
    assert error.src is not None and error.src.line == line
    assert ("line %d" % line) in str(error) and words in str(error)


def test_a_turn_at_an_unknown_position_is_refused() -> None:
    """Dialect: a C change on a line that writes no X or Y, before the position is known, swings the body anywhere."""
    result = read_gcode("M05\nG01 Z10.000 F300\nG00 C90\n", flavour="dialect", job=JOB)
    assert not result.refused
    with pytest.raises(NormaliseError, match="turns the robot") as info:
        normalise(result, JOB)
    assert info.value.src is not None and info.value.src.line == 3


def test_exact_whatever_the_callers_decimal_context() -> None:
    """Shifts and comparisons never round, even under a caller's coarse decimal context."""
    spec = job(units="mm", z_zero="bottom", placement={"dx": 0.001, "dy": 0.0})
    lines = ["G21", "G0 Z23.000", "G0 X1234.5678912345678912345678912 Y100.000", "G1 Z0.0199 F200",
             "G1 X1300.000", "G0 Z23.000"]
    reference = norm(lines, job_spec=spec)
    with localcontext() as ctx:
        ctx.prec = 3
        coarse = norm(lines, job_spec=spec)
    assert coarse.cuts[0].zmin == reference.cuts[0].zmin == D("-17.9801")
    assert coarse.moves[2].b[0] == reference.moves[2].b[0] == D("1234.5688912345678912345678912")
    assert coarse.cuts[0].max_depth == D("17.9801")


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _two_ops(c1: str, c2: str) -> list[str]:
    return [
        ";GOLIATH OP id=op1 kind=profile through=auto tool=1%s" % c1,  # 7
        "G0 Z10.000",
        "G0 X100.000 Y100.000",
        "M3 S18000",
        "G1 Z-3.000 F200",                                               # 11
        "G1 X200.000 F800",
        ";GOLIATH OPEND id=op1",                                         # 13
        ";GOLIATH OP id=op2 kind=profile through=auto tool=1%s" % c2,  # 14
        "G1 X300.000",                                                   # 15
        "G0 Z10.000",
        ";GOLIATH OPEND id=op2",
        "M5",
    ]


def test_ops_blocks_give_the_cut_its_operation_and_forced_heading() -> None:
    program = norm_gi([
        ";GOLIATH OP id=op1 kind=pocket through=0 tool=1 c=90 label=\"Pocket\"",
        "G0 Z10.000", "G0 X100.000 Y100.000", "M3 S18000", "G1 Z-3.000 F200", "G1 X200.000 F800",
        ";GOLIATH OPEND id=op1",
        "G0 Z10.000",  # a pure Z move outside every block: allowed by the reader, a link here
        "M5",
    ])
    (cut,) = program.cuts
    assert cut.op is not None and cut.op.id == "op1" and cut.op.kind == "pocket"
    assert cut.forced_c == 90 and cut.ops == ("op1",)
    assert program.meta.ops_source == "blocks"
    assert program.findings == ()
    assert not notes_of(program, "ops-inferred")


def test_trailing_retract_outside_the_block_is_not_a_mismatch() -> None:
    program = norm_gi([
        ";GOLIATH OP id=op1 kind=profile through=auto tool=1",
        "G0 Z10.000", "G0 X100.000 Y100.000", "M3 S18000", "G1 Z-3.000 F200", "G1 X200.000 F800",
        ";GOLIATH OPEND id=op1",
        "G0 Z10.000",
        "M5",
    ])
    assert lines_of(program.cuts[0].moves) == [11, 12, 14]
    assert program.cuts[0].ops == ("op1",)
    assert program.findings == ()


def test_cut_spanning_two_blocks_warns() -> None:
    program = norm_gi(_two_ops(" c=90", " c=90"))
    (cut,) = program.cuts
    assert cut.ops == ("op1", "op2")
    assert cut.forced_c == 90
    (warning,) = program.findings
    assert warning.check_id == "N-OPS-MISMATCH" and warning.severity == "warn"
    assert warning.witness.cut == 0 and warning.witness.src.line == 15
    assert "op1" in warning.message and "op2" in warning.message


@pytest.mark.parametrize("c1, c2", [(" c=90", " c=180"), (" c=90", ""), ("", " c=0")])
def test_conflicting_forced_headings_in_one_cut_are_refused(c1: str, c2: str) -> None:
    """A forced heading is never ignored (gi-format 7): one cut cannot hold two of them."""
    result = read_gi(_two_ops(c1, c2))
    assert not result.refused
    with pytest.raises(NormaliseError, match="forced heading") as info:
        normalise(result, JOB)
    assert info.value.src is not None and info.value.src.line == 15


def test_ops_inferred_without_blocks() -> None:
    program = norm(SEGMENTED)
    assert program.meta.ops_source == "inferred"
    assert all(cut.op is None and cut.ops == () and cut.forced_c is None for cut in program.cuts)
    assert notes_of(program, "ops-inferred")


# ---------------------------------------------------------------------------
# Inputs, events, tools
# ---------------------------------------------------------------------------


def test_refused_read_is_never_normalised() -> None:
    result = read_gcode("G21\nG0 Z10\nG91\nG0 X1\n", flavour="generic", job=JOB_MM)
    assert result.refused
    with pytest.raises(NormaliseError, match="refused") as info:
        normalise(result, JOB_MM)
    assert info.value.findings and all(f.severity in ("refuse", "internal-error") for f in info.value.findings)


def test_reader_notes_and_disclosures_are_carried() -> None:
    result = read(["G21", "G0 Z10.000 ; raise the tool", "G0 X100.000 Y100.000", "G1 Z-1.000 F100", "G0 Z10.000",
                   "M30"])
    program = normalise(result, JOB_MM)
    reader = [n for n in program.notes if n.stage == "reader"]
    assert {n.kind for n in reader} == {n.kind for n in result.notes}
    assert set(result.disclosures) <= set(program.disclosures)
    assert program.meta.reader_notes


def test_bare_raw_program_says_its_reader_notes_are_elsewhere() -> None:
    result = read(["G21", "G0 Z10.000", "G0 X100.000 Y100.000", "G1 Z-1.000 F100", "G0 Z10.000"])
    program = normalise(result.program_raw, JOB_MM)
    assert not program.meta.reader_notes
    assert notes_of(program, "reader-notes-absent")
    assert not [n for n in program.notes if n.stage == "reader"]


def test_another_job_than_the_one_read_with_is_refused() -> None:
    result = read(["G21", "G0 Z10.000", "G0 X100.000 Y100.000", "G1 Z-1.000 F100", "G0 Z10.000"])
    other = job(units="mm", panel={"T": 17.0})
    with pytest.raises(NormaliseError, match="stock_thickness"):
        normalise(result, other)


def test_events_keep_their_place_between_moves() -> None:
    program = norm(["G21", "G0 Z10.000", "G0 X100.000 Y100.000", "M3 S18000", "G1 Z-1.000 F100",
                    "G0 Z10.000", "M5", "M30"])
    kinds = [(e.kind, e.before_move, e.src.line) for e in program.events]
    assert kinds == [("spindle_on", 2, 4), ("spindle_off", 4, 7), ("program_end", 4, 8)]


def test_tools_and_units_have_their_sources() -> None:
    program = norm(["G21", "T1 M6", "G0 Z10.000", "G0 X100.000 Y100.000", "G1 Z-1.000 F100", "G0 Z10.000"])
    (use,) = program.tools
    assert use.number == 1 and use.tool is JOB_MM.tool
    assert program.meta.units.value == "mm"
    for name in ("eps_surf", "through_tol", "helix_tol"):
        param = getattr(program.meta, name)
        assert param.ref.startswith("docs/prep/params.json")
    assert program.meta.eps_surf.value == 0.02


def test_op_without_cut_is_noted_and_a_through_claim_warned() -> None:
    program = norm_gi([
        ";GOLIATH OP id=op1 kind=engrave through=1 tool=1",  # 7
        "G0 Z10.000", "G0 X100.000 Y100.000", "G1 Z0.000 F200", "G1 X200.000", "G0 Z10.000",
        ";GOLIATH OPEND id=op1",
        "M5",
    ])
    assert program.cuts == ()
    assert note_lines(program, "op-without-cut") == {7}
    (warning,) = program.findings
    assert warning.check_id == "N-OPS-MISMATCH" and warning.witness.cut is None
    assert "removes no material" in warning.message
