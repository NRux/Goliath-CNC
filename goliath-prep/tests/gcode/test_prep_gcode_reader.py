# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The strict G-code reader: vocabulary, modal state, end of program, markers, gates, flavours (unit P02).

The reader refuses everything outside a small vocabulary and names line and
column (the project design, section 4.2; docs/prep/gi-format.md section 8).
Red-team PHYS-17: motion after M2/M30 is refused, and the ``;U``/``;S`` markers
are refused outside dialect verify mode. G4 is a gate question: refused while
``drop_dwells`` is closed (the desk view), dropped with a disclosure once open.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from prep_gcode_support import (
    GI_HEADER,
    JOB,
    JOB_MM,
    PREAMBLE,
    assert_clean,
    assert_refused,
    at,
    describe,
    gi_text,
    job,
    lines_text,
    read_dialect,
    read_generic,
    read_gi,
    refusals,
)

from goliath_prep.core.types import ClosedGateView
from goliath_prep.gcode import RawEvent, RawMove, read_gcode

D = Decimal

# ---------------------------------------------------------------------------
# Refused words: each finding names line and column
# ---------------------------------------------------------------------------

# (offending line, finding id, the text the column points at)
REFUSED = [
    ("G20", "D-UNITS", "G20"),
    ("G0 G20 X150.000", "D-UNITS", "G20"),
    ("G91", "D-UNSUPPORTED", "G91"),
    ("G90.1", "D-UNSUPPORTED", "G90.1"),
    ("G2 X120.000 Y100.000 R10.000", "D-ARC", "R10"),
    ("G18", "D-UNSUPPORTED", "G18"),
    ("G19", "D-UNSUPPORTED", "G19"),
    ("G41", "D-UNSUPPORTED", "G41"),
    ("G42", "D-UNSUPPORTED", "G42"),
    ("G28", "D-UNSUPPORTED", "G28"),
    ("G30", "D-UNSUPPORTED", "G30"),
    ("G53 G0 Z10.000", "D-UNSUPPORTED", "G53"),
    ("G92 X0.000", "D-UNSUPPORTED", "G92"),
    *[("G%d" % n, "D-UNSUPPORTED", "G%d" % n) for n in range(80, 90)],
    ("M4 S18000", "D-UNSUPPORTED", "M4"),
    ("M04", "D-UNSUPPORTED", "M04"),
    ("M0", "D-UNSUPPORTED", "M0"),
    ("M1", "D-UNSUPPORTED", "M1"),
    ("G4 P2", "G-GATE", "G4"),
    *[("G%d" % n, "D-UNSUPPORTED", "G%d" % n) for n in range(55, 60)],
    ("G0 X100.000 C90", "D-UNSUPPORTED", "C90"),
    ("G1 A10.000", "D-UNSUPPORTED", "A10"),
    ("G1 B10.000", "D-UNSUPPORTED", "B10"),
    ("M10", "D-UNSUPPORTED", "M10"),
    ("M11", "D-UNSUPPORTED", "M11"),
    ("T2 M6", "D-SINGLETOOL", "T2"),
    ("T1", "D-SINGLETOOL", "T1"),
    ("G1 X110.000 I5.000", "D-ARC", "I5"),
    ("G2 X110.000 Y110.000 I5.000 K0.000", "D-ARC", "K0"),
    ("H1", "D-UNSUPPORTED", "H1"),
    ("P2", "D-UNSUPPORTED", "P2"),
    ("G1 Q5", "D-UNSUPPORTED", "Q5"),
    ("M98", "D-UNSUPPORTED", "M98"),
    ("G38.2 Z-10.000", "D-UNSUPPORTED", "G38.2"),
    ("G1 X10000.001", "D-RANGE", "X10000.001"),
    ("G1 Y-10000.5", "D-RANGE", "Y-10000.5"),
    ("G1 X110.000 F-5", "D-RANGE", "F-5"),
    ("S-1", "D-RANGE", "S-1"),
]


@pytest.mark.parametrize("flavour", ["gi", "generic"])
@pytest.mark.parametrize("line,check_id,token", REFUSED, ids=[case[0] for case in REFUSED])
def test_refused_words_table(flavour: str, line: str, check_id: str, token: str) -> None:
    """Each refused word is refused with its finding id, naming line and column (design 4.2; gi-format 8.5)."""
    column = line.index(token) + 1
    if flavour == "gi":
        result = read_gi([*PREAMBLE, line])
        number = len(GI_HEADER) + len(PREAMBLE) + 1
    else:
        result = read_generic(["G21", *PREAMBLE, line])
        number = len(PREAMBLE) + 2
    finding = assert_refused(result, check_id, number, column)
    assert finding.witness.src.text == line


def test_refused_words_are_never_interpreted() -> None:
    """A refused line changes no modal state: the move after it starts where the last good move ended."""
    result = read_generic(["G21", *PREAMBLE, "G91 G1 X5.000", "G1 Y150.000"])
    assert_refused(result, "D-UNSUPPORTED", 6, 1)
    last = result.program_raw.moves[-1]
    assert last.start[:2] == (D("100.000"), D("100.000"))
    assert last.end[:2] == (D("100.000"), D("150.000"))


def test_second_tool_header_line_is_a_singletool_refusal() -> None:
    header = [*GI_HEADER, ";GOLIATH TOOL n=2 d=3.000 flute=12.000"]
    result = read_gi(PREAMBLE, header=header)
    assert_refused(result, "D-SINGLETOOL", 7)


@pytest.mark.parametrize(
    "lines,line,why",
    [
        (["T1 M6", "T1", "G0 Z10.000"], 2, "a second T"),
        (["M6", "G0 Z10.000"], 1, "M6 without T"),
        (["G0 Z10.000", "M6"], 2, "M6 after motion"),
        (["T1", "M6", "M6", "G0 Z10.000"], 3, "a second M6"),
        (["T2", "G0 Z10.000"], 1, "T naming another tool than TOOL n"),
    ],
)
def test_one_tool_rules(lines: list[str], line: int, why: str) -> None:
    """gi-format 8.9: one T before the first motion naming TOOL n, M6 on or after it; two tools are two files."""
    result = read_gi(lines)
    assert_refused(result, "D-SINGLETOOL", len(GI_HEADER) + line)


def test_tool_select_before_motion_is_accepted() -> None:
    result = read_gi(["T1 M6", "G0 Z10.000"])
    assert_clean(result)
    assert result.program_raw.tool == 1
    kinds = [e.kind for e in result.program_raw.events]
    assert kinds[:2] == ["tool_select", "tool_change"]


# ---------------------------------------------------------------------------
# Accepted no-ops, G43, units
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flavour", ["gi", "generic", "dialect"])
def test_accepted_noops(flavour: str) -> None:
    """G17 G21 G90 G94 G54 G40 assert the defaults: accepted, nothing refused, and a note says so."""
    lines = ["G17 G21 G90 G94 G54 G40", "G17", "G21", "G90", "G94", "G54", "G40", "G0 Z10.000"]
    if flavour == "gi":
        result = read_gi(lines)
    elif flavour == "generic":
        result = read_generic(lines, job_spec=JOB)
    else:
        result = read_dialect(lines)
    assert_clean(result)
    first = (len(GI_HEADER) if flavour == "gi" else 0) + 1
    notes = [n for n in result.notes if n.kind == "noop"]
    assert len(notes) == 1 and notes[0].lines == tuple(range(first, first + 7)), result.notes
    assert len(result.program_raw.moves) == 1


def test_g43_ignored_with_disclosure() -> None:
    """G43 (with or without H) is accepted and ignored, disclosed: our reading that the robot sets Z zero itself."""
    result = read_gi(["G43 H1", "G43", *PREAMBLE])
    assert_clean(result)
    ids = [d.id for d in result.disclosures]
    assert "READER:G43-ignored" in ids, ids
    disclosure = next(d for d in result.disclosures if d.id == "READER:G43-ignored")
    assert disclosure.severity in ("amber", "red")
    assert "line 7" in disclosure.source and "spec 03" in disclosure.text
    assert any(n.kind == "g43-ignored" and n.lines == (7, 8) for n in result.notes)
    # H alone is outside the vocabulary.
    assert_refused(read_gi(["H1"]), "D-UNSUPPORTED", 7, 1)


def test_units_required_without_g21() -> None:
    """Generic G-code needs G21 before its first motion, or units = "mm" in job.toml; G20 is always refused."""
    no_units = job()
    result = read_generic(PREAMBLE, job_spec=no_units)
    finding = assert_refused(result, "D-UNITS", 1)
    assert "G21" in finding.message and 'units = "mm"' in finding.message
    assert result.program_raw.units is None

    result = read_generic(["G21", *PREAMBLE], job_spec=no_units)
    assert_clean(result)
    assert result.program_raw.units.value == "mm" and "G21" in result.program_raw.units.ref

    result = read_generic(["G21 G0 Z10.000", *PREAMBLE[1:]], job_spec=no_units)
    assert_clean(result)

    result = read_generic(PREAMBLE, job_spec=JOB_MM)
    assert_clean(result)
    assert result.program_raw.units.ref == "job.toml units"

    # G21 after the first motion does not cover the motion before it.
    result = read_generic([PREAMBLE[0], "G21", *PREAMBLE[1:]], job_spec=no_units)
    assert_refused(result, "D-UNITS", 1)

    # G20 is refused even when job.toml says mm.
    assert_refused(read_generic(["G20", *PREAMBLE], job_spec=JOB_MM), "D-UNITS", 1, 1)

    # GI states its unit in the header: no G21 needed.
    result = read_gi(PREAMBLE)
    assert_clean(result)
    assert result.program_raw.units.value == "mm" and "GI UNITS" in result.program_raw.units.ref

    # The dialect is millimetres by specification (spec 05).
    result = read_dialect(PREAMBLE[:2], job_spec=no_units)
    assert_clean(result)
    assert result.program_raw.units.source == "SPEC"


# ---------------------------------------------------------------------------
# Modal state
# ---------------------------------------------------------------------------


def _moves(lines: list[str]) -> tuple[RawMove, ...]:
    result = read_generic(lines)
    assert_clean(result)
    return result.program_raw.moves


def test_modal_state_table() -> None:
    """gi-format 8.6: motion mode, X/Y/Z, F and S are modal; I/J are not; axes start unknown, not zero."""
    # Axes start unknown.
    (move,) = _moves(["G0 X1.000"])
    assert move.start == (None, None, None) and move.end == (D("1.000"), None, None)
    assert move.kind == "traverse"

    # The motion mode carries to a line with axis words only; absent axes keep their value.
    first, second = _moves(["G0 X1.000 Y2.000 Z3.000", "X4.000"])
    assert second.kind == "traverse" and second.start == first.end
    assert second.end == (D("4.000"), D("2.000"), D("3.000"))

    # F and S are modal; the spindle state is the program's.
    moves = _moves(["G0 X0.000 Y0.000 Z5.000", "S12000 M3", "G1 Z-1.000 F150", "X10.000", "G0 Z5.000 M5"])
    assert [m.kind for m in moves] == ["traverse", "feed", "feed", "traverse"]
    assert moves[1].feed == D("150") and moves[2].feed == D("150") and moves[2].rpm == D("12000")
    assert [m.spindle_on for m in moves] == [False, True, True, False]

    # Words that are not part of the motion act before it: F, S, then M3/M5.
    (move,) = _moves(["G1 X0.000 Y0.000 Z-5.000 F200 S9000 M3"])
    assert move.spindle_on and move.feed == D("200") and move.rpm == D("9000")

    # I and J are not modal: an arc line without them has no centre.
    result = read_generic(["G0 X0.000 Y0.000 Z1.000", "G2 X10.000 Y0.000 I5.000 J0.000", "X0.000"])
    assert_refused(result, "D-ARC", 3)

    # A G word alone sets the mode; the next axis line moves in it.
    moves = _moves(["G0 X0.000 Y0.000 Z1.000", "G1 F100", "X3.000"])
    assert [m.kind for m in moves] == ["traverse", "feed"]

    # Axis words before any motion mode are refused.
    assert_refused(read_generic(["X1.000"]), "D-SYNTAX", 1, 1)
    assert_refused(read_generic(["F100 Y2.000"]), "D-SYNTAX", 1, 6)

    # Leading zeros: G00 is G0, M03 is M3.
    moves = _moves(["G00 X0.000 Y0.000 Z1.000", "M03 S1000", "G01 Z-1.000 F100", "M05"])
    assert [m.kind for m in moves] == ["traverse", "feed"] and moves[1].spindle_on


@pytest.mark.parametrize(
    "line,column,why",
    [
        ("G0 X1.000 X2.000", 11, "the same letter twice"),
        ("G0 G1 X1.000", 4, "two motion codes"),
        ("G0 G00 X1.000", 4, "the same G number twice"),
        ("M3 M5", 4, "two M words"),
        ("g0 X1.000", 1, "lower-case letter"),
        ("G0 X 1.000", 5, "space inside a word"),
        ("G0 X.5", 5, "no digit before the point"),
        ("G0 X5.", 6, "no digit after the point"),
        ("G0 X1,5", 6, "comma decimal"),
        ("G0 X1e3", 6, "exponent"),
        ("(comment) G0 X1.000", 1, "parenthesised comment"),
        ("%", 1, "percent sentinel"),
        ("G0 X1.000 \u2013", 11, "non-ASCII outside a comment"),
        ("G0 X\u22121.000", 5, "a minus sign that is not ASCII"),
        ("G+1 X1.000", 1, "a signed G number"),
        ("T1.5", 1, "a T with a point"),
        ("N1.5 G0 X1.000", 1, "an N with a point"),
        ("G0 X", 5, "a letter without a number"),
    ],
)
def test_lexical_and_line_errors_name_line_and_column(line: str, column: int, why: str) -> None:
    result = read_generic(["G0 Z10.000", line])
    assert_refused(result, "D-SYNTAX", 2, column)


def test_trailing_spaces_and_comments() -> None:
    result = read_generic(["G0 Z10.000 \t ", "G0 X1.000 ; a note", "; Note: a plain comment", "", "  \t"])
    assert_clean(result)
    assert any(n.kind == "comment-dropped" and n.lines == (2, 3) for n in result.notes), result.notes


def test_n_words_and_coolant_dropped_with_a_note() -> None:
    result = read_generic(["N10 G0 Z10.000", "N20 M8", "N30 M9", "M7"])
    assert_clean(result)
    kinds = {n.kind: n.lines for n in result.notes}
    assert kinds["n-word-dropped"] == (1, 2, 3)
    assert kinds["coolant-dropped"] == (2, 3, 4)


# ---------------------------------------------------------------------------
# Program end (PHYS-17)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("end", ["M30", "M2", "M02", "N100 M30", "M30 ; end"])
def test_motion_after_m30_refused(end: str) -> None:
    """Red-team PHYS-17: after M2/M30 any motion, any word and any annotation is refused, naming the line."""
    for after in ("G0 Z20.000", "X5.000", "M5", "N5", "G4 P1", "M30"):
        result = read_generic([*PREAMBLE, end, "", "; a plain comment", after], job_spec=JOB_MM)
        assert_refused(result, "D-AFTER-END", 8)
        assert result.program_raw.end_line == 5
        assert all(m.src.line < 5 for m in result.program_raw.moves)
    result = read_gi([*PREAMBLE, end, ";GOLIATH OP id=late kind=other through=0 tool=1"])
    assert_refused(result, "D-AFTER-END", len(GI_HEADER) + 6)


def test_end_alone_on_its_line_and_blank_lines_after_it() -> None:
    result = read_generic([*PREAMBLE, "M5", "M30", "", "; end of program", "  ", "\t"], job_spec=JOB_MM)
    assert_clean(result)
    assert result.program_raw.end_line == 6
    assert any(n.kind == "program-end-dropped" and n.lines == (6,) for n in result.notes)
    assert_refused(read_generic([*PREAMBLE, "G0 Z20.000 M30"]), "D-AFTER-END", 5)
    assert_refused(read_generic([*PREAMBLE, "M5 M30"]), "D-SYNTAX", 5)


# ---------------------------------------------------------------------------
# Markers (PHYS-17)
# ---------------------------------------------------------------------------

MARKERS = [";U|1|6mm upcut", ";S", "; Spindle warm-up", ";  u lower-case", ";\tSdust"]


@pytest.mark.parametrize("flavour", ["gi", "generic"])
@pytest.mark.parametrize("marker", MARKERS)
def test_U_and_S_markers_refused_outside_dialect(flavour: str, marker: str) -> None:
    """Red-team PHYS-17: a ;U or ;S marker outside dialect verify mode is refused naming its line, never dropped."""
    for line in (marker, "G0 Z12.000 " + marker):
        if flavour == "gi":
            result = read_gi([*PREAMBLE, line])
            number = len(GI_HEADER) + len(PREAMBLE) + 1
        else:
            result = read_generic([*PREAMBLE, line])
            number = len(PREAMBLE) + 1
        finding = assert_refused(result, "D-MARKER", number, line.index(";") + 1)
        assert "dialect" in finding.message


def test_markers_are_recorded_in_dialect_verify_mode() -> None:
    lines = [";U|1|6mm upcut", "M05", "G00 X100.000 Y100.000 C0", "Z10.000", "; S empty the bin", "M10",
             "G01 Z-2.000 F300 S18000", "M05"]
    result = read_dialect(lines)
    assert_clean(result)
    markers = [e for e in result.program_raw.events if e.kind in ("marker_u", "marker_s")]
    assert [(m.kind, m.src.line) for m in markers] == [("marker_u", 1), ("marker_s", 5)]
    assert any(n.kind == "marker-recorded" for n in result.notes)


def test_dialect_tool_change_marker_after_motion_is_a_second_tool() -> None:
    result = read_dialect(["M05", "G00 X100.000 Y100.000 C0", "Z10.000", ";U|2|3mm", "M06"])
    assert_refused(result, "D-SINGLETOOL", 4)
    assert_refused(result, "D-SINGLETOOL", 5)


# ---------------------------------------------------------------------------
# Goliath dialect, verify only
# ---------------------------------------------------------------------------


def test_dialect_parses_c_m10_m05() -> None:
    """Dialect verify mode reads C, M10, M05 (and M11, M06) for verification; C is modal (spec 05)."""
    lines = [
        "M05",
        "G00 X100.000 Y200.000 C90",
        "Z10.000",
        "M10",
        "G01 Z-5.000 F300 S18000",
        "G01 X200.000 F1200",
        "G02 X250.000 Y250.000 I0.000 J50.000",
        "M05",
        "G00 X400.000 Y200.000 C180",
        "Z10.000",
        "C190",
        "M05",
        "M11",
        "M10",
    ]
    result = read_dialect(lines)
    assert_clean(result)
    moves = result.program_raw.moves
    assert [m.kind for m in moves] == ["traverse", "traverse", "feed", "feed", "arc_cw", "traverse", "traverse",
                                       "traverse"]
    assert moves[0].c_start is None and moves[0].c_end == D("90")
    assert moves[1].c_end == D("90") and "C" not in moves[1].written
    assert moves[2].spindle_on and moves[2].c_end == D("90")
    assert moves[5].c_start == D("90") and moves[5].c_end == D("180")
    assert moves[7].start[:2] == moves[7].end[:2] and moves[7].c_end == D("190") and moves[7].written == {"C"}
    events = [(e.kind, e.src.line) for e in result.program_raw.events]
    assert ("spindle_off", 1) in events and ("spindle_on", 4) in events and ("dust", 13) in events
    assert result.program_raw.flavour == "dialect"


@pytest.mark.parametrize(
    "lines,line",
    [
        (["G00 X0.000 Y0.000 C0", "Z5.000", "G01 Z-1.000 F100", "G01 X10.000 C10"], 4),
        (["G00 X0.000 Y0.000 C0", "Z5.000", "G01 Z-1.000 F100", "C10"], 4),
        (["G00 X0.000 Y0.000 C0", "Z5.000", "G02 X10.000 Y0.000 I5.000 J0.000 C10"], 3),
        (["G00 X0.000 Y0.000 C0", "G03 X10.000 Y0.000 I5.000 C20"], 2),
    ],
)
def test_c_on_feed_line_refused_in_verify_mode(lines: list[str], line: int) -> None:
    """E-B4/U-21: what the firmware does with C on a feed line is unknown, so it is refused (D-CSTEP)."""
    result = read_dialect(lines)
    finding = assert_refused(result, "D-CSTEP", line)
    assert "feed" in finding.message
    assert finding.witness.heading_c is not None


@pytest.mark.parametrize("word", ["M3", "M03", "T1", "M4", "A5", "G4 P1"])
def test_dialect_vocabulary_refuses_what_the_robot_was_never_sent(word: str) -> None:
    result = read_dialect(["G00 X0.000 Y0.000 C0", word])
    assert refusals(result), describe(result)


def test_dialect_m06_resets_the_position_memory() -> None:
    result = read_dialect(["M06", "G00 X0.000 Y0.000 C0", "Z10.000"])
    assert_clean(result)
    result = read_dialect(["G00 X0.000 Y0.000 C0", "Z10.000", "M06"])
    assert_refused(result, "D-SINGLETOOL", 3)


# ---------------------------------------------------------------------------
# Gates: G4 (drop_dwells)
# ---------------------------------------------------------------------------


class GateTable:
    """A GateView over a fixed table (test double)."""

    def __init__(self, table: dict[str, bool | None]) -> None:
        self.table = table

    def allowed(self, name: str) -> bool | None:
        return self.table.get(name, False)

    def value(self, name: str) -> object:
        return None

    def snapshot_sha256(self) -> str:
        return "0" * 64


def test_g4_refused_with_the_default_closed_gate_view_which_is_disclosed() -> None:
    """The reader has no gate argument in the design interface: without one it uses the all-closed desk view."""
    result = read_gi([*PREAMBLE, "G4 P2"])
    finding = assert_refused(result, "G-GATE", 11, 1)
    assert finding.witness.gate == "drop_dwells" and finding.witness.unlock_rung == "R4"
    assert ClosedGateView.DISCLOSURE in result.disclosures
    explicit = read_gi([*PREAMBLE, "G4 P2"], gates=ClosedGateView())
    assert_refused(explicit, "G-GATE", 11, 1)
    assert ClosedGateView.DISCLOSURE in explicit.disclosures


@pytest.mark.parametrize("answer", [False, None])
def test_g4_refused_while_drop_dwells_is_not_true(answer: bool | None) -> None:
    """Only True opens a gate: uncertain (None) is never read as yes."""
    result = read_gi([*PREAMBLE, "G4 P2"], gates=GateTable({"drop_dwells": answer}))
    assert_refused(result, "G-GATE", 11, 1)
    assert ClosedGateView.DISCLOSURE not in result.disclosures


def test_g4_dropped_with_a_disclosure_once_drop_dwells_is_open() -> None:
    result = read_gi([*PREAMBLE, "G4 P2", "G1 X150.000"], gates=GateTable({"drop_dwells": True}))
    assert_clean(result)
    assert any(d.id == "READER:G4-dropped" and "line 11" in d.source for d in result.disclosures)
    assert any(e.kind == "dwell_dropped" and e.src.line == 11 for e in result.program_raw.events)
    assert any(n.kind == "dwell-dropped" and n.lines == (11,) for n in result.notes)
    # P without G4 stays outside the vocabulary, gate or not.
    result = read_gi([*PREAMBLE, "P2"], gates=GateTable({"drop_dwells": True}))
    assert_refused(result, "D-UNSUPPORTED", 11, 1)


def test_gate_view_must_provide_the_protocol() -> None:
    with pytest.raises(TypeError):
        read_gcode(gi_text(PREAMBLE), flavour="gi", job=JOB, gates=object())


# ---------------------------------------------------------------------------
# Flavours
# ---------------------------------------------------------------------------


def test_auto_reads_gi_when_any_line_carries_an_annotation() -> None:
    result = read_gcode(gi_text(PREAMBLE), flavour="auto", job=JOB)
    assert result.flavour == "gi" and result.gi_header is not None
    assert any(n.kind == "flavour" and "GI" in n.text for n in result.notes)
    result = read_gcode(lines_text(["G21", *PREAMBLE]), flavour="auto", job=JOB)
    assert result.flavour == "generic" and result.gi_header is None
    assert any(n.kind == "flavour" and "generic" in n.text for n in result.notes)


def test_auto_never_reads_the_dialect() -> None:
    """Dialect verify mode is chosen explicitly; auto reads a C word as generic G-code and refuses it."""
    line = "G00 X0.000 Y0.000 C90"
    result = read_gcode(lines_text([line, "Z10.000"]), flavour="auto", job=JOB_MM)
    assert result.flavour == "generic"
    finding = assert_refused(result, "D-UNSUPPORTED", 1, line.index("C90") + 1)
    assert "dialect" in finding.message


@pytest.mark.parametrize("flavour", ["generic", "dialect"])
def test_annotations_in_a_file_not_read_as_gi_are_refused(flavour: str) -> None:
    """gi-format 5: a file with an annotation is GI and is never read with its annotations ignored."""
    result = read_gcode(lines_text(["G21", "G0 Z10.000", ";GOLIATH OP id=a kind=other through=0 tool=1"]),
                        flavour=flavour, job=JOB_MM)
    finding = assert_refused(result, "D-GI-VERSION", 1)
    assert "line 3" in finding.message


def test_gi_flavour_requires_the_version_line() -> None:
    result = read_gcode(lines_text(["G21", *PREAMBLE]), flavour="gi", job=JOB)
    assert_refused(result, "D-GI-VERSION", 1)


def test_unknown_flavour_and_wrong_job_type_fail_with_a_clear_error() -> None:
    with pytest.raises(ValueError, match="flavour"):
        read_gcode("G0 Z1.000\n", flavour="iso", job=JOB)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="JobSpec"):
        read_gcode("G0 Z1.000\n", flavour="generic", job={"panel": {}})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="str"):
        read_gcode(b"G0 Z1.000\n", flavour="generic", job=JOB)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# GI files with OP blocks: motion outside every block is air motion
# ---------------------------------------------------------------------------


def test_motion_outside_ops_must_stay_at_or_above_the_stock_top() -> None:
    op = ";GOLIATH OP id=op1 kind=profile through=auto tool=1"
    end = ";GOLIATH OPEND id=op1"
    ok = read_gi(["G0 Z10.000", "G0 X10.000 Y10.000", op, "G1 Z-1.000 F100", "G0 Z5.000", end,
                  "G0 X20.000", "G0 Z0.000"])
    assert_clean(ok)
    assert [m.op for m in ok.program_raw.moves] == [None, None, "op1", "op1", None, None]
    # XY travel from an unknown Z, a move ending below the top, XY travel starting below the top.
    assert_refused(read_gi(["G0 X10.000 Y10.000", op, end]), "D-GI-OP", 7)
    assert_refused(read_gi(["G0 Z10.000", "G1 Z-0.001 F100", op, end]), "D-GI-OP", 8)
    assert_refused(read_gi([op, "G0 Z10.000", "G1 Z-1.000 F100", end, "G0 X50.000"]), "D-GI-OP", 11)
    # Without OP blocks, the reader leaves operations to the later stages.
    assert_clean(read_gi(["G0 X10.000 Y10.000", "G1 Z-1.000 F100"]))


def test_unclosed_and_mismatched_ops() -> None:
    op = ";GOLIATH OP id=op1 kind=profile through=auto tool=1"
    assert_refused(read_gi([op, "G0 Z10.000"]), "D-GI-OP", 7)
    assert_refused(read_gi([op, "G0 Z10.000", ";GOLIATH OPEND id=op2"]), "D-GI-OP", 9)
    assert_refused(read_gi(["G0 Z10.000", ";GOLIATH OPEND id=op1"]), "D-GI-OP", 8)
    assert_refused(read_gi([op, op.replace("op1", "op2")]), "D-GI-OP", 8)


def test_forced_heading_360_is_the_heading_0() -> None:
    """gi-format 7: c=360 is the same heading as 0; the model keeps 360 as written."""
    result = read_gi([";GOLIATH OP id=d kind=drill through=0 tool=1 c=360", "G0 Z10.000", ";GOLIATH OPEND id=d"])
    assert_clean(result)
    (op,) = result.gi_header.ops
    assert op.c == 360 and op.forced_c == 0
    result = read_gi([";GOLIATH OP id=d kind=drill through=0 tool=1 c=90", "G0 Z10.000", ";GOLIATH OPEND id=d"])
    assert result.gi_header.ops[0].forced_c == 90
    assert result.gi_header.ops[0].end_line == 9


def test_ops_and_events_keep_source_references() -> None:
    result = read_gi(PREAMBLE)
    for item in result.program_raw.items:
        assert isinstance(item, (RawMove, RawEvent))
        assert item.src.file == "<input>" and item.src.line >= 7
    result = read_gcode(gi_text(PREAMBLE), flavour="gi", job=JOB, file="part-a.gi")
    assert all(item.src.file == "part-a.gi" for item in result.program_raw.items)


def test_findings_are_from_the_catalogue_and_refusals_carry_witnesses() -> None:
    result = read_gi([*PREAMBLE, "G91", "G4 P1", "T2", "M30", "G0 X1.000"])
    assert refusals(result)
    for finding in result.findings:
        assert finding.witness is not None and finding.witness.src is not None
    assert at(result, "D-AFTER-END", 15)
