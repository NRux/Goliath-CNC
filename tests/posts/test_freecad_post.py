# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Tests of the Goliath FreeCAD post (posts/freecad, unit PP1).

The post writes GI, the Goliath-intermediate G-code of docs/prep/gi-format.md.
It has two parts: ``gi_format.py``, a pure module that turns plain data and
FreeCAD-like path commands into GI text, and ``goliath_post.py``, the script
post FreeCAD loads (``export(objectslist, filename, argstring)``), which reads
the FreeCAD job. FreeCAD cannot run here, so both are tested with the stand-in
objects of ``freecad_fakes.py``; nothing below needs FreeCAD.

The committed samples (posts/freecad/samples/) must be exactly what the post
writes for the stand-in jobs, must follow the GI writing rules, and must pass
goliath-prep's reader once it exists (test_committed_samples_pass_goliath_prep_check).

Python 3.9 compatible: the repository-level suite runs on 3.9.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import math
import os
import re
import shutil
import subprocess
import sys
import textwrap
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import freecad_fakes as ff
from freecad_fakes import FakeCommand, cmds

goliath_post, gi_format = ff.import_post()
GoliathPostError = gi_format.GoliathPostError

STOCK = gi_format.StockData(0.0, 0.0, 0.0, 2440.0, 1220.0, 18.0, source="stock 'Stock'")
TOOL = gi_format.ToolData(
    number=1, diameter=6.0, flute=22.0, rpm=18000.0, kind="flat", name="6mm 2F upcut", source="tool controller 'TC'"
)
F200 = ff.per_second(200.0)
F800 = ff.per_second(800.0)


def op_data(commands: List[Any], **overrides: Any) -> Any:
    fields: Dict[str, Any] = dict(
        id="op3",
        kind="profile",
        label="Part A outline",
        tool=1,
        spindle_rpm=18000.0,
        commands=tuple(commands),
        final_depth=-0.5,
    )
    fields.update(overrides)
    return gi_format.OpData(**fields)


def fmt(commands: List[Any], stock: Any = STOCK, tools: Optional[List[Any]] = None, **op_overrides: Any) -> str:
    tools = tools if tools is not None else [TOOL]
    return gi_format.format_gi([op_data(commands, **op_overrides)], stock, tools, tower2="Y", cam="FreeCAD 1.1.4")


def body(text: str) -> List[str]:
    """The lines between the OP line and its OPEND line (the spindle start included)."""
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith(";GOLIATH OP "))
    end = next(i for i, line in enumerate(lines) if line.startswith(";GOLIATH OPEND "))
    return lines[start + 1 : end]


def refused(commands: List[Any], match: str, **kwargs: Any) -> str:
    with pytest.raises(GoliathPostError, match=match) as info:
        fmt(commands, **kwargs)
    return str(info.value)


# --------------------------------------------------------------------------
# The pure formatter
# --------------------------------------------------------------------------


def test_format_with_fake_commands() -> None:
    text = fmt(
        cmds(
            "(Profile)",
            "G0 Z23.0 F50.0",
            "G0 X100.0 Y200.0",
            "G0 Z21.0",
            "G1 Z-0.5 F%r" % F200,
            "G1 X300.0 F%r" % F800,
            "G2 X350.0 Y250.0 I0.0 J50.0 K0.0",
            "G0 Z28.0",
        )
    )
    assert text == textwrap.dedent(
        """\
        ;GOLIATH-GI 1
        ;GOLIATH SOURCE cam="FreeCAD 1.1.4" post="goliath_post.py 0.1.0"
        ;GOLIATH UNITS mm
        ;GOLIATH STOCK x=2440.000 y=1220.000 t=18.000 origin=t1-corner zzero=top
        ;GOLIATH TOWER2 axis=Y
        ;GOLIATH TOOL n=1 d=6.000 flute=22.000 kind=flat rpm=18000 name="6mm 2F upcut"
        ;GOLIATH ZSHIFT -18.000
        G17 G21 G90
        T1 M6
        ;GOLIATH OP id=op3 kind=profile through=1 tool=1 label="Part A outline"
        M3 S18000
        ; FreeCAD: Profile
        G0 Z5.000
        G0 X100.000 Y200.000
        G0 Z3.000
        G1 Z-18.500 F200
        G1 X300.000 F800
        G2 X350.000 Y250.000 I0.000 J50.000
        G0 Z10.000
        ;GOLIATH OPEND id=op3
        M5
        M30
        """
    )


def test_output_is_lf_ascii_and_ends_with_a_newline() -> None:
    text = fmt(cmds("(Teil \u00c4 \u2014 Au\u00dfenkontur)", "G0 Z23.0", "G1 Z10.0 F%r" % F200), label="Teil \u00c4")
    assert "\r" not in text and text.endswith("\n")
    for line in text.split("\n"):
        code = line.split(";", 1)[0]
        assert code.isascii(), line


def test_zshift_is_minus_stock_top_and_applied_to_every_z_once() -> None:
    # FreeCAD stock from Z 1.05 to 19.05: Z = 0 at the stock top means a shift of -19.05,
    # which need not equal -t (gi-format.md, ZSHIFT).
    stock = gi_format.StockData(0.0, 0.0, 1.05, 1000.0, 500.0, 19.05)
    text = fmt(cmds("G0 Z25.05", "G1 Z1.05 F%r" % F200, "G1 Z0.55"), stock=stock, final_depth=1.05)
    assert ";GOLIATH STOCK x=1000.000 y=500.000 t=18.000 origin=t1-corner zzero=top" in text
    assert ";GOLIATH ZSHIFT -19.050" in text
    assert body(text)[1:] == ["G0 Z6.000", "G1 Z-18.000 F200", "G1 Z-18.500"]


def test_zero_shift_is_written_without_a_minus_sign() -> None:
    stock = gi_format.StockData(0.0, 0.0, -18.0, 2440.0, 1220.0, 0.0)
    text = fmt(cmds("G0 Z5.0", "G1 Z-0.0001 F%r" % F200), stock=stock)
    assert ";GOLIATH ZSHIFT 0.000" in text
    assert body(text)[1:] == ["G0 Z5.000", "G1 Z0.000 F200"]


def test_refuses_origin_not_at_tower1_corner() -> None:
    for xmin, ymin in ((10.0, 0.0), (0.0, -5.0), (0.0005, 0.0), (0.0, -0.0005)):
        stock = gi_format.StockData(xmin, ymin, 0.0, 2440.0 + xmin, 1220.0 + ymin, 18.0, source="stock 'Stock'")
        with pytest.raises(GoliathPostError) as info:
            fmt(cmds("G0 Z23.0"), stock=stock)
        message = str(info.value)
        assert "tower-1 corner" in message and "stock 'Stock'" in message
        assert "X = 0, Y = 0" in message
    # Below half the file resolution the corner is at the origin as far as the file can tell.
    stock = gi_format.StockData(0.0004, -0.0004, 0.0, 2440.0004, 1219.9996, 18.0)
    assert ";GOLIATH STOCK x=2440.000 y=1220.000 t=18.000" in fmt(cmds("G0 Z23.0", "G1 Z10.0 F1"), stock=stock)


def test_refuses_stock_without_extent() -> None:
    stock = gi_format.StockData(0.0, 0.0, 0.0, 2440.0, 1220.0, 0.0004)
    with pytest.raises(GoliathPostError, match="thickness"):
        fmt(cmds("G0 Z5.0"), stock=stock)


def test_through_is_claimed_only_when_the_final_depth_reaches_the_stock_bottom() -> None:
    def through(final_depth: Optional[float]) -> str:
        text = fmt(cmds("G0 Z23.0", "G1 Z10.0 F1"), final_depth=final_depth)
        return re.search(r" through=(\S+) ", text).group(1)  # type: ignore[union-attr]

    assert through(-0.2) == "1"
    assert through(0.0) == "1"
    assert through(0.05) == "1"  # the allowance gi-format.md gives through=auto
    assert through(0.051) == "auto"
    assert through(14.0) == "auto"
    assert through(None) == "auto"  # no final depth: the post makes no claim


def test_feeds_are_converted_from_freecad_mm_per_second() -> None:
    text = fmt(
        cmds(
            "G0 Z23.0 F50.0",  # a rapid rate: GI has none, the F is not written
            "G0 X10.0 Y10.0 F50.0",
            "G1 Z10.0 F%r" % F200,
            "G1 X20.0 F%r" % F800,
            "G1 Y20.0",  # modal feed
            "G1 X10.0 F%r" % F800,  # same feed: not repeated
            "G0 Z23.0 F50.0",
            "G1 Z10.0 F%r" % F200,  # back to the plunge feed: written again
        )
    )
    assert body(text)[1:] == [
        "G0 Z5.000",
        "G0 X10.000 Y10.000",
        "G1 Z-8.000 F200",
        "G1 X20.000 F800",
        "G1 Y20.000",
        "G1 X10.000",
        "G0 Z5.000",
        "G1 Z-8.000 F200",
    ]


def test_each_operation_states_its_feed_on_its_first_feed_move() -> None:
    # The feed is modal across operations, as in any G-code; the second block restates it anyway,
    # so that each OP block reads the same on its own.
    text = gi_format.format_gi(
        [op_data(cmds("G0 Z23.0", "G1 Z10.0 F%r" % F800)), op_data(cmds("G0 Z23.0", "G1 Z12.0"), id="op4")],
        STOCK,
        [TOOL],
        tower2="Y",
        cam="FreeCAD 1.1.4",
    )
    assert "G1 Z-8.000 F800\n" in text and "G1 Z-6.000 F800\n" in text


def test_feed_only_commands_carry_the_feed_to_the_next_feed_move() -> None:
    text = fmt(cmds("G0 Z23.0", "G0 F99.0", "G1 F%r" % F800, "G1 Z10.0"))
    assert body(text)[1:] == ["G0 Z5.000", "G1 Z-8.000 F800"]


def test_refuses_feed_moves_without_a_usable_feed() -> None:
    message = refused(cmds("G0 Z23.0", "G1 Z10.0"), "no feed rate")
    assert "operation 'Part A outline' (op3)" in message and "path command 2" in message
    message = refused(cmds("G0 Z23.0", "G1 Z10.0 F0.0"), "0 mm/min")
    assert "tool controller" in message  # says how to fix it
    refused(cmds("G0 Z23.0", "G1 Z10.0 F0.004"), "0 mm/min")
    refused(cmds("G0 Z23.0", "G1 Z10.0 F-3.0"), "mm/min")


def test_arc_centre_is_kept_exact_to_the_written_start() -> None:
    # Start X 0.0004 is written as 0.000; the centre 10.0004 as 10.000: I is 10.000, not 10.0004 rounded on its own.
    text = fmt(cmds("G0 X0.0004 Y0.0 Z23.0", "G1 Z10.0 F1", "G3 X20.0004 Y0.0 I10.0 J0.0"))
    assert body(text)[-1] == "G3 X20.000 Y0.000 I10.000 J0.000"
    text = fmt(cmds("G0 X0.0006 Y0.0 Z23.0", "G1 Z10.0 F1", "G3 X20.0006 Y0.0 I10.0 J0.0"))
    assert body(text)[-1] == "G3 X20.001 Y0.000 I10.000 J0.000"
    # A full circle (no X, Y) and a helix keep their form; I and J are always both written.
    text = fmt(cmds("G0 X5.0 Y0.0 Z23.0", "G1 Z10.0 F1", "G2 Z9.0 I-5.0", "G3 X5.0 Y0.0 Z8.0 J2.5"))
    assert body(text)[-2:] == ["G2 Z-9.000 I-5.000 J0.000", "G3 X5.000 Y0.000 Z-10.000 I0.000 J2.500"]


def test_refuses_arcs_outside_the_xy_plane_or_without_centre() -> None:
    refused(cmds("G0 X0.0 Y0.0 Z23.0", "G1 Z10.0 F1", "G2 X10.0 Y0.0 I5.0 J0.0 K1.0"), "K")
    refused(cmds("G0 X0.0 Y0.0 Z23.0", "G1 Z10.0 F1", "G2 X10.0 Y0.0"), "I/J centre")


def _arc_to(code: str, angle_rad: float) -> List[Any]:
    """From (10, 0) about the origin (radius 10) to the point at ``angle_rad``, on the circle."""
    end = {"X": 10.0 * math.cos(angle_rad), "Y": 10.0 * math.sin(angle_rad), "I": -10.0, "J": 0.0, "K": 0.0}
    return [ff.cmd("G0 X10.0 Y0.0 Z23.0"), ff.cmd("G1 Z10.0 F1"), FakeCommand(code, end)]


def test_refuses_a_sliver_arc_that_three_decimals_would_turn_into_a_full_circle() -> None:
    # gi-format.md 8.7 (PHYS-16): an arc whose end equals its start at file precision is a full circle.
    # FreeCAD asks for an arc of 0.0006 degrees (a 0.0001 mm sliver); written with three decimals its
    # end would equal its start, and the reader would cut a full circle of radius 10 mm instead.
    for code, angle in (("G3", 1e-5), ("G2", -1e-5)):
        message = refused(_arc_to(code, angle), "full circle")
        assert "path command 3" in message and "0.0006" in message, message
    # Just above FreeCAD's own coincidence tolerance (0.000001 mm) it is still a sliver, still refused.
    refused(_arc_to("G3", 2e-7), "full circle")


def test_keeps_full_circles_and_near_full_arcs_whose_end_rounds_onto_the_start() -> None:
    # FreeCAD writes a full circle as one arc whose end is its start within its own tolerance (0.000001 mm).
    for code in ("G2", "G3"):
        lines = body(fmt(_arc_to(code, 5e-8)))
        assert lines[-1] == "%s X10.000 Y0.000 I-10.000 J0.000" % code
    # An arc that sweeps all but 0.0006 degrees is a full circle to within the file's resolution.
    assert body(fmt(_arc_to("G2", 1e-5)))[-1] == "G2 X10.000 Y0.000 I-10.000 J0.000"
    assert body(fmt(_arc_to("G3", -1e-5)))[-1] == "G3 X10.000 Y0.000 I-10.000 J0.000"
    # An ordinary small arc whose end is written apart from its start is unaffected.
    assert body(fmt(_arc_to("G3", 0.1)))[-1] == "G3 X9.950 Y0.998 I-10.000 J0.000"


def test_refusal_messages_name_the_real_reason_for_an_extra_word() -> None:
    message = refused(cmds("G0 X0.0 Y0.0 Z23.0", "G1 Z10.0 F1", "G2 X10.0 Y0.0 R5.0"), "I/J centre")
    assert "rotary" not in message
    message = refused(cmds("G0 Z25.0", "G98", "G0 X10.0 Y10.0", "G81 X10.0 Y10.0 Z-0.5 R20.0 Q2.0 F2.5"), "Q")
    assert "rotary" not in message
    message = refused(cmds("G0 Z23.0", "G1 X1.0 P3.0 F1.0"), "P")
    assert "rotary" not in message
    assert "rotary" in refused(cmds("G0 Z23.0", "G0 X1.0 A90.0"), "A, which")


@pytest.mark.parametrize(
    "command, reason",
    [
        ("G41 D1", "compensation"),
        ("G42", "compensation"),
        ("G91", "absolute"),
        ("G20", "millimetres"),
        ("G18", "XY plane"),
        ("G19", "XY plane"),
        ("G28", "home"),
        ("G53 Z0", "machine coordinates"),
        ("G92 X0", "offset"),
        ("G55", "work offset"),
        ("G59", "work offset"),
        ("G43 H1", "outside what this post translates"),
        ("G84 X1 Y1 Z1 R5", "tapping"),
        ("G86 X1 Y1 Z1 R5", "canned cycle"),
        ("G38.2 Z-10", "outside what this post translates"),
        ("M4 S10000", "one way"),
        ("M6 T2", "tool change"),
        ("M0", "pause"),
        ("M1", "pause"),
        ("M2", "end"),
        ("M30", "end"),
        ("M10", "outside what this post translates"),
        ("T2", "outside what this post translates"),
        ("G0 X1 A90", "A, which"),
        ("G1 X1 C90 F1", "C, which"),
        ("message", "outside what this post translates"),
        ("(MC_RUN_COMMAND: M10)", "raw controller command"),
    ],
)
def test_refuses_commands_outside_the_vocabulary(command: str, reason: str) -> None:
    command_object = ff.cmd(command) if command != "message" else FakeCommand("message", {"text": 1.0})
    message = refused([ff.cmd("G0 Z23.0"), ff.cmd("G1 Z10.0 F1"), command_object], re.escape(reason))
    assert "operation 'Part A outline' (op3), path command 3" in message


@pytest.mark.parametrize("code", ["G17", "G21", "G40", "G54", "G90", "G94", "M5", "M7", "M8", "M9"])
def test_accepted_modal_words_pass_through(code: str) -> None:
    assert body(fmt([ff.cmd("G0 Z23.0"), ff.cmd(code), ff.cmd("G1 Z10.0 F1")]))[2] == code


def test_spindle_and_dwell_words_inside_an_operation() -> None:
    text = fmt(cmds("G0 Z23.0", "M3 S16000.4", "G4 P0.5", "G1 Z10.0 F1"))
    assert body(text)[2:4] == ["M3 S16000", "G4 P0.500"]
    refused(cmds("G0 Z23.0", "M3 S0.2"), "spindle speed")
    refused(cmds("G0 Z23.0", "G4 P-1.0"), "dwell")
    refused(cmds("G0 Z23.0", "G4"), "dwell")


def test_refuses_malformed_commands() -> None:
    class NoName:
        Parameters: Dict[str, float] = {}

    refused([ff.cmd("G0 Z23.0"), NoName()], "not a FreeCAD path command")
    refused([ff.cmd("G0 Z23.0"), FakeCommand("G1", {"x": 1.0})], "parameter")
    refused([ff.cmd("G0 Z23.0"), FakeCommand("G1", {"X": float("nan"), "F": 1.0})], "nan")
    refused([ff.cmd("G0 Z23.0"), FakeCommand("G1", {"X": True})], "not a number")
    refused([ff.cmd("G0 Z23.0"), FakeCommand("g1", {"X": 1.0})], "outside what this post translates")


@pytest.mark.parametrize(
    "freecad_text, written",
    [
        ("(Begin Drilling)", "; FreeCAD: Begin Drilling"),
        ("(GOLIATH OP id=x kind=profile)", "; FreeCAD: GOLIATH OP id=x kind=profile"),
        ("(S: empty the dust bin)", "; FreeCAD: S: empty the dust bin"),
        ("(U|tool change)", "; FreeCAD: U|tool change"),
        ("(two\nlines\r)", "; FreeCAD: two lines"),
    ],
)
def test_freecad_comments_never_become_annotations_or_markers(freecad_text: str, written: str) -> None:
    lines = body(fmt([FakeCommand(freecad_text), ff.cmd("G0 Z23.0"), ff.cmd("G1 Z10.0 F1")]))
    assert lines[1] == written


def test_empty_operation_is_refused() -> None:
    refused(cmds("(Profile)", "G90", "G0 F12.0"), "no moves")


def test_inactive_and_other_notes_are_written_as_plain_comments() -> None:
    text = gi_format.format_gi(
        [op_data(cmds("G0 Z23.0", "G1 Z10.0 F1"), notes=("kept as kind=other",))],
        STOCK,
        [TOOL],
        tower2="X",
        cam="FreeCAD 1.1.4",
        notes=("operation 'Old' (Profile001) is inactive and was not written",),
    )
    lines = text.split("\n")
    assert "; Note: operation 'Old' (Profile001) is inactive and was not written" in lines
    assert lines[lines.index("; Note: kept as kind=other") + 1].startswith(";GOLIATH OP ")
    assert ";GOLIATH TOWER2 axis=X" in lines


def test_coolant_words_bracket_the_operation() -> None:
    lines = body(fmt(cmds("G0 Z23.0", "G1 Z10.0 F1"), coolant="Flood"))
    assert lines[0] == "M8" and lines[1] == "M3 S18000" and lines[-1] == "M9"
    lines = body(fmt(cmds("G0 Z23.0", "G1 Z10.0 F1"), coolant="Mist"))
    assert lines[0] == "M7" and lines[-1] == "M9"
    with pytest.raises(GoliathPostError, match="coolant"):
        fmt(cmds("G0 Z23.0"), coolant="Air")


def test_labels_and_names_are_quoted_and_control_characters_refused() -> None:
    text = fmt(cmds("G0 Z23.0", "G1 Z10.0 F1"), label='say "hi" \\ bye')
    assert 'label="say \\"hi\\" \\\\ bye"' in text
    with pytest.raises(GoliathPostError, match="control character"):
        fmt(cmds("G0 Z23.0"), label="tab\there")
    text = fmt(cmds("G0 Z23.0", "G1 Z10.0 F1"), label="")
    assert re.search(r";GOLIATH OP id=op3 kind=profile through=1 tool=1\n", text)


def test_operation_ids_kinds_and_tools_are_checked() -> None:
    with pytest.raises(GoliathPostError, match="id"):
        fmt(cmds("G0 Z23.0"), id="has space")
    with pytest.raises(GoliathPostError, match="kind"):
        fmt(cmds("G0 Z23.0"), kind="facing")
    with pytest.raises(GoliathPostError, match="tool 2"):
        fmt(cmds("G0 Z23.0"), tool=2)
    with pytest.raises(GoliathPostError, match="twice"):
        gi_format.format_gi(
            [op_data(cmds("G0 Z23.0", "G1 Z10.0 F1")), op_data(cmds("G0 Z23.0", "G1 Z10.0 F1"))],
            STOCK,
            [TOOL],
            tower2="Y",
            cam="FreeCAD 1.1.4",
        )
    with pytest.raises(GoliathPostError, match="no active operation"):
        gi_format.format_gi([], STOCK, [TOOL], tower2="Y", cam="FreeCAD 1.1.4")
    with pytest.raises(GoliathPostError, match="tower2"):
        gi_format.format_gi([op_data(cmds("G0 Z1.0"))], STOCK, [TOOL], tower2="Z", cam="FreeCAD 1.1.4")


def test_one_tool_per_file() -> None:
    other_number = gi_format.ToolData(3, 6.0, 22.0, 18000.0, "flat", "6mm", source="tool controller 'TC2'")
    with pytest.raises(GoliathPostError, match="one tool") as info:
        fmt(cmds("G0 Z23.0"), tools=[TOOL, other_number])
    assert "1, 3" in str(info.value) and "one FreeCAD job per tool" in str(info.value)
    other_bit = gi_format.ToolData(1, 8.0, 22.0, 18000.0, "flat", "8mm", source="tool controller 'TC2'")
    with pytest.raises(GoliathPostError, match="diameter") as info:
        fmt(cmds("G0 Z23.0"), tools=[TOOL, other_bit])
    assert "tool controller 'TC'" in str(info.value) and "tool controller 'TC2'" in str(info.value)
    with pytest.raises(GoliathPostError, match="no tool controller"):
        fmt(cmds("G0 Z23.0"), tools=[])


def test_same_tool_at_two_speeds_leaves_tool_rpm_out_and_says_so() -> None:
    slower = gi_format.ToolData(1, 6.0, 22.0, 12000.0, "flat", "6mm 2F upcut001", source="tool controller 'TC2'")
    text = gi_format.format_gi(
        [
            op_data(cmds("G0 Z23.0", "G1 Z10.0 F1")),
            op_data(cmds("G0 Z23.0", "G1 Z10.0 F1"), id="op4", spindle_rpm=12000.0),
        ],
        STOCK,
        [TOOL, slower],
        tower2="Y",
        cam="FreeCAD 1.1.4",
    )
    assert ";GOLIATH TOOL n=1 d=6.000 flute=22.000 kind=flat\n" in text
    assert "12000, 18000 rpm" in text and "name their bit differently (6mm 2F upcut, 6mm 2F upcut001)" in text
    assert text.count("M3 S18000") == 1 and text.count("M3 S12000") == 1


def test_tool_values_are_checked() -> None:
    for bad, match in (
        (gi_format.ToolData(1, 0.0004, 22.0, 18000.0), "diameter"),
        (gi_format.ToolData(1, 6.0, 0.0, 18000.0), "flute"),
        (gi_format.ToolData(1, 6.0, 22.0, 0.2), "spindle speed"),
        (gi_format.ToolData(1, 6.0, 22.0, 18000.0, kind="spoon"), "kind"),
        (gi_format.ToolData(-1, 6.0, 22.0, 18000.0), "tool number"),
    ):
        with pytest.raises(GoliathPostError, match=match):
            fmt(cmds("G0 Z23.0"), tools=[bad], tool=bad.number)


# --------------------------------------------------------------------------
# Canned drilling cycles (FreeCAD's Drilling operation writes them)
# --------------------------------------------------------------------------


def drill(*commands: str, **kwargs: Any) -> List[str]:
    lines = body(fmt(cmds("G0 Z25.0", *commands), final_depth=-0.5, **kwargs))
    return [line for line in lines[2:] if not line.startswith("; Note: ")]


def test_g81_with_g98_retracts_to_the_start_height() -> None:
    assert drill("G98", "G0 X10.0 Y10.0", "G0 Z23.0", "G81 X10.0 Y10.0 Z-0.5 R20.0 F2.5", "G81 X20.0 Y10.0") == [
        "G0 X10.000 Y10.000",
        "G0 Z5.000",
        "G0 Z2.000",
        "G1 Z-18.500 F150",
        "G0 Z5.000",
        "G0 X20.000 Y10.000",
        "G0 Z2.000",
        "G1 Z-18.500",
        "G0 Z5.000",
    ]


def test_g81_with_g99_retracts_to_r_and_rises_to_r_first_when_below() -> None:
    assert drill("G99", "G0 X10.0 Y10.0", "G0 Z15.0", "G81 X20.0 Y10.0 Z-0.5 R20.0 F2.5", "G80", "G0 Z25.0") == [
        "G0 X10.000 Y10.000",
        "G0 Z-3.000",
        "G0 Z2.000",
        "G0 X20.000 Y10.000",
        "G1 Z-18.500 F150",
        "G0 Z2.000",
        "G0 Z7.000",
    ]


def test_g82_dwells_and_g85_feeds_out() -> None:
    assert drill("G98", "G0 X10.0 Y10.0", "G82 X10.0 Y10.0 Z-0.5 R20.0 P0.25 F2.5") == [
        "G0 X10.000 Y10.000",
        "G0 Z2.000",
        "G1 Z-18.500 F150",
        "G4 P0.250",
        "G0 Z7.000",
    ]
    assert drill("G98", "G0 X10.0 Y10.0", "G85 X10.0 Y10.0 Z-0.5 R20.0 F2.5") == [
        "G0 X10.000 Y10.000",
        "G0 Z2.000",
        "G1 Z-18.500 F150",
        "G1 Z2.000",
        "G0 Z7.000",
    ]


def test_g83_pecks_back_to_r_and_g73_breaks_chips() -> None:
    assert drill("G99", "G0 X10.0 Y10.0", "G83 X10.0 Y10.0 Z8.0 R20.0 Q5.0 F2.5") == [
        "G0 X10.000 Y10.000",
        "G0 Z2.000",
        "G1 Z-3.000 F150",
        "G0 Z2.000",
        "G0 Z-2.746",
        "G1 Z-8.000",
        "G0 Z2.000",
        "G0 Z-7.746",
        "G1 Z-10.000",
        "G0 Z2.000",
    ]
    assert drill("G98", "G0 X10.0 Y10.0", "G73 X10.0 Y10.0 Z8.0 R20.0 Q5.0 F2.5") == [
        "G0 X10.000 Y10.000",
        "G0 Z2.000",
        "G1 Z-3.000 F150",
        "G0 Z-2.746",
        "G1 Z-8.000",
        "G0 Z-7.746",
        "G1 Z-10.000",
        "G0 Z7.000",
    ]


def test_cycle_repeat_and_sticky_values() -> None:
    assert drill("G99", "G0 X10.0 Y10.0", "G81 X10.0 Y10.0 Z10.0 R20.0 L2 F2.5", "G81 X30.0") == [
        "G0 X10.000 Y10.000",
        "G0 Z2.000",
        "G1 Z-8.000 F150",
        "G0 Z2.000",
        "G1 Z-8.000",
        "G0 Z2.000",
        "G0 X30.000 Y10.000",
        "G1 Z-8.000",
        "G0 Z2.000",
    ]


def test_each_expanded_cycle_is_noted_in_the_file() -> None:
    lines = body(fmt(cmds("G0 Z25.0", "G99", "G0 X10.0 Y10.0", "G83 X10.0 Y10.0 Z8.0 R20.0 Q5.0 F2.5")))
    notes = [line for line in lines if line.startswith("; Note: ")]
    assert notes == [
        "; Note: FreeCAD G83 cycle at X10.000 Y10.000 (bottom Z-10.000, R2.000, Q5.000, G99) "
        "expanded into moves by the RS274/NGC rules; pecks back off 0.254 mm"
    ]


@pytest.mark.parametrize(
    "commands, reason",
    [
        (("G0 X10.0 Y10.0", "G81 X10.0 Y10.0 Z-0.5 R20.0 F2.5"), "G98/G99"),
        (("G98", "G81 X10.0 Y10.0 Z-0.5 F2.5"), "without R"),
        (("G98", "G81 X10.0 Y10.0 R20.0 F2.5"), "without Z"),
        (("G98", "G81 X10.0 Y10.0 Z20.0 R20.0 F2.5"), "not below"),
        (("G98", "G83 X10.0 Y10.0 Z-0.5 R20.0 Q0.0 F2.5"), "Q"),
        (("G98", "G83 X10.0 Y10.0 Z-0.5 R20.0 Q0.001 F2.5"), "pecks"),
        (("G98", "G81 X10.0 Y10.0 Z-0.5 R20.0 L0 F2.5"), "L"),
        (("G98", "G81 X10.0 Y10.0 Z-0.5 R20.0 L1.5 F2.5"), "L"),
        (("G98", "G81 X10.0 Y10.0 Z-0.5 R20.0"), "no feed rate"),
        (("G98", "G81 X10.0 Y10.0 Z-0.5 R20.0 Q2.0 F2.5"), "Q"),
        (("G98", "G82 X10.0 Y10.0 Z-0.5 R20.0 F2.5"), "without P"),
    ],
)
def test_refuses_cycles_it_cannot_expand_exactly(commands: Any, reason: str) -> None:
    refused(cmds("G0 Z25.0", *commands), re.escape(reason))


def test_refuses_cycle_from_unknown_position() -> None:
    refused(cmds("G98", "G81 X10.0 Y10.0 Z-0.5 R20.0 F2.5"), "start Z")
    refused(cmds("G0 Z25.0", "G98", "G81 Z-0.5 R20.0 F2.5"), "X and Y")


# --------------------------------------------------------------------------
# Mapping FreeCAD names
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module, kind",
    [
        ("Path.Op.Profile", "profile"),
        ("Path.Op.Pocket", "pocket"),
        ("Path.Op.PocketShape", "pocket"),
        ("Path.Op.Drilling", "drill"),
        ("Path.Op.Engrave", "engrave"),
        ("Path.Op.Vcarve", "engrave"),
        ("Path.Op.Adaptive", "adaptive"),
        ("PathScripts.PathProfile", "profile"),
        ("PathScripts.PathAdaptive", "adaptive"),
        ("Path.Op.MillFace", None),
        ("Path.Op.Helix", None),
        ("Path.Op.Slot", None),
        ("Path.Dressup.Tags", None),
        ("", None),
    ],
)
def test_operation_kinds(module: str, kind: Optional[str]) -> None:
    assert gi_format.op_kind_for(module) == kind


@pytest.mark.parametrize(
    "shape, kind",
    [
        ("Endmill", "flat"),
        ("endmill", "flat"),
        ("Ballend", "ball"),
        ("Bullnose", "bull"),
        ("torus", "bull"),
        ("VBit", "vbit"),
        ("v-bit", "vbit"),
        ("Drill", "other"),
        ("Chamfer", "other"),
        ("endmill.fcstd", "flat"),
        ("C:\\Tools\\Shape\\ballend.fcstd", "ball"),
        ("/home/u/Bit/v-bit.fcstd", "vbit"),
        ("", None),
        (None, None),
    ],
)
def test_tool_kinds(shape: Optional[str], kind: Optional[str]) -> None:
    assert gi_format.tool_kind_for(shape) == kind


def test_freecad_cam_text() -> None:
    assert gi_format.freecad_cam_text(["1", "1", "4", "43532 (Git)", "url"]) == "FreeCAD 1.1.4"
    assert gi_format.freecad_cam_text(["1", "0"]) == "FreeCAD 1 0"
    assert gi_format.freecad_cam_text(ff.STAND_IN_VERSION) == "FreeCAD (stand-in objects, not a FreeCAD run)"
    assert gi_format.freecad_cam_text([]) == "FreeCAD (version not reported)"


# --------------------------------------------------------------------------
# The script post: export(objectslist, filename, argstring)
# --------------------------------------------------------------------------


@pytest.fixture
def freecad(monkeypatch: pytest.MonkeyPatch) -> Any:
    module = ff.fake_freecad_module(["1", "1", "4", "43532 (Git)"])
    monkeypatch.setitem(sys.modules, "FreeCAD", module)
    return module


def simple_job(**job_kwargs: Any) -> Any:
    job = ff.box_job(**job_kwargs)
    tc = ff.FakeToolController()
    op = ff.add_op(job, ff.FakeOp("Profile", cmds("(Profile)", "G0 Z23.0", "G1 Z-0.2 F%r" % F200), tc))
    return job, tc, op


def test_export_writes_the_file_and_returns_the_text(freecad: Any, tmp_path: Path) -> None:
    job, tc, op = simple_job()
    out = tmp_path / "job.gi"
    text = goliath_post.export(ff.post_list(job, tc, op), str(out), "--tower2=Y")
    assert out.read_bytes() == text.encode("utf-8")
    assert text.startswith(';GOLIATH-GI 1\n;GOLIATH SOURCE cam="FreeCAD 1.1.4" post="goliath_post.py 0.1.0"\n')
    assert ';GOLIATH TOOL n=1 d=6.000 flute=22.000 kind=flat rpm=18000 name="6 mm Endmill"\n' in text
    assert ';GOLIATH OP id=Profile kind=profile through=1 tool=1 label="Profile"\n' in text
    # "-" means: return the text, write no file.
    assert goliath_post.export(ff.post_list(job, tc, op), "-", "--tower2 Y") == text
    assert sorted(p.name for p in tmp_path.iterdir()) == ["job.gi"]


def test_export_refuses_origin_not_at_tower1_corner(freecad: Any, tmp_path: Path) -> None:
    job, tc, op = simple_job(xmin=-10.0, xmax=2430.0)
    out = tmp_path / "job.gi"
    with pytest.raises(GoliathPostError, match="tower-1 corner") as info:
        goliath_post.export(ff.post_list(job, tc, op), str(out), "--tower2=Y")
    assert "X = -10.000" in str(info.value) and "job 'Job'" in str(info.value)
    assert not out.exists(), "a refusal writes no file"
    assert len(freecad.Console.errors) == 1 and "tower-1 corner" in freecad.Console.errors[0]


@pytest.mark.parametrize(
    "args, match",
    [
        ("", "--tower2"),
        ("--tower2", "--tower2"),
        ("--tower2=Z", "X or Y"),
        ("--tower2=Y --tower2=X", "more than once"),
        ("--tower2=Y --no-show-editor", "--no-show-editor"),
        ("--tower2='Y", "quotation"),
    ],
)
def test_export_requires_the_tower2_axis_from_the_job_arguments(freecad: Any, args: str, match: str) -> None:
    job, tc, op = simple_job()
    with pytest.raises(GoliathPostError, match=re.escape(match)):
        goliath_post.export(ff.post_list(job, tc, op), "-", args)


def test_export_accepts_lower_case_axis_and_none_args(freecad: Any) -> None:
    job, tc, op = simple_job()
    assert ";GOLIATH TOWER2 axis=X\n" in goliath_post.export(ff.post_list(job, tc, op), "-", "--tower2=x")
    with pytest.raises(GoliathPostError, match="--tower2"):
        goliath_post.export(ff.post_list(job, tc, op), "-", None)


def test_export_takes_stock_from_the_job_only(freecad: Any) -> None:
    job, tc, op = simple_job(xmax=1220.0, ymax=610.0, zmin=1.05, zmax=19.05)
    text = goliath_post.export(ff.post_list(job, tc, op), "-", "--tower2=X")
    assert ";GOLIATH STOCK x=1220.000 y=610.000 t=18.000 origin=t1-corner zzero=top\n" in text
    assert ";GOLIATH ZSHIFT -19.050\n" in text


def test_export_refuses_stock_that_is_not_a_box(freecad: Any) -> None:
    job, tc, op = simple_job()
    job.Stock.Shape.Volume *= 0.785  # a cylinder in the same bounding box
    with pytest.raises(GoliathPostError, match="not a box"):
        goliath_post.export(ff.post_list(job, tc, op), "-", "--tower2=Y")
    job.Stock = None
    with pytest.raises(GoliathPostError, match="no stock"):
        goliath_post.export(ff.post_list(job, tc, op), "-", "--tower2=Y")


def test_export_skips_inactive_operations_visibly(freecad: Any) -> None:
    job, tc, op = simple_job()
    old = ff.add_op(job, ff.FakeOp("Profile001", cmds("(inactive operation)"), tc, label="Old outline", active=False))
    text = goliath_post.export(ff.post_list(job, tc, op, old), "-", "--tower2=Y")
    assert "; Note: FreeCAD operation 'Old outline' (Profile001) is inactive and was not written\n" in text
    assert "id=Profile001" not in text
    with pytest.raises(GoliathPostError, match="no active operation"):
        goliath_post.export(ff.post_list(job, tc, old), "-", "--tower2=Y")


def test_export_refuses_work_offsets(freecad: Any) -> None:
    job, tc, op = simple_job(fixtures=("G54", "G55"))
    with pytest.raises(GoliathPostError, match="G54, G55"):
        goliath_post.export(ff.post_list(job, tc, op), "-", "--tower2=Y")
    job, tc, op = simple_job()
    with pytest.raises(GoliathPostError, match="G56"):
        goliath_post.export([ff.FakeFixture("G56"), tc, op], "-", "--tower2=Y")


def test_export_reads_the_spindle_from_the_tool_controller_path(freecad: Any) -> None:
    for tc, match in (
        (ff.FakeToolController(direction="Reverse"), "counter-clockwise"),
        (ff.FakeToolController(direction="None"), "starts no spindle"),
        (ff.FakeToolController(rpm=0.0), "starts no spindle"),
    ):
        job = ff.box_job()
        op = ff.add_op(job, ff.FakeOp("Profile", cmds("G0 Z23.0", "G1 Z-0.2 F1.0"), tc))
        with pytest.raises(GoliathPostError, match=match) as info:
            goliath_post.export(ff.post_list(job, tc, op), "-", "--tower2=Y")
        assert "tool controller 'TC: 6 mm Endmill'" in str(info.value)


def test_export_refuses_tool_without_flute_length(freecad: Any) -> None:
    job = ff.box_job()
    tc = ff.FakeToolController(ff.FakeToolBit(flute=None))
    op = ff.add_op(job, ff.FakeOp("Profile", cmds("G0 Z23.0", "G1 Z-0.2 F1.0"), tc))
    with pytest.raises(GoliathPostError, match="CuttingEdgeHeight") as info:
        goliath_post.export(ff.post_list(job, tc, op), "-", "--tower2=Y")
    assert "such as Endmill" in str(info.value)
    op.ToolController = None
    with pytest.raises(GoliathPostError, match="no tool controller"):
        goliath_post.export(ff.post_list(job, tc, op), "-", "--tower2=Y")
    # A dressup on such an operation is still recognised as an operation, and refused for the same reason.
    dressup = ff.add_op(job, ff.FakeDressup("RampDressup", op, cmds("G0 Z23.0", "G1 Z-0.2 F1.0")))
    with pytest.raises(GoliathPostError, match="RampDressup.*no tool controller"):
        goliath_post.export(ff.post_list(job, tc, dressup), "-", "--tower2=Y")


def test_export_unknown_tool_shape_leaves_kind_out(freecad: Any) -> None:
    job = ff.box_job()
    tc = ff.FakeToolController(ff.FakeToolBit(shape_type=None))
    op = ff.add_op(job, ff.FakeOp("Profile", cmds("G0 Z23.0", "G1 Z-0.2 F1.0"), tc))
    text = goliath_post.export(ff.post_list(job, tc, op), "-", "--tower2=Y")
    assert ';GOLIATH TOOL n=1 d=6.000 flute=22.000 rpm=18000 name="6 mm Endmill"\n' in text


def test_export_follows_a_dressup_to_its_base_operation(freecad: Any) -> None:
    job = ff.box_job()
    tc = ff.FakeToolController()
    base = ff.FakeOp("Profile", cmds("G0 Z23.0", "G1 Z-0.2 F1.0"), tc, label="Outline", final_depth=0.0)
    dressup = ff.add_op(job, ff.FakeDressup("TagDressup", base, cmds("G0 Z23.0", "G1 Z-0.2 F1.0", "G1 Z5.0")))
    text = goliath_post.export(ff.post_list(job, tc, dressup), "-", "--tower2=Y")
    assert ';GOLIATH OP id=TagDressup kind=profile through=1 tool=1 label="TagDressup"\n' in text
    assert "G1 Z-13.000\n" in text  # the dressup's own path is written


def test_export_writes_unrecognised_operations_as_other_with_a_note(freecad: Any) -> None:
    job = ff.box_job()
    tc = ff.FakeToolController()
    op = ff.add_op(job, ff.FakeOp("Slot", cmds("G0 Z23.0", "G1 Z-0.2 F1.0"), tc, proxy=ff.SLOT, label="Groove"))
    text = goliath_post.export(ff.post_list(job, tc, op), "-", "--tower2=Y")
    assert (
        "; Note: FreeCAD operation 'Groove' (Slot) is a Path.Op.Slot.ObjectSlot, which has no GI kind of its own; "
        "written as kind=other\n;GOLIATH OP id=Slot kind=other " in text
    )


def test_export_refuses_objects_it_does_not_know(freecad: Any) -> None:
    job, tc, op = simple_job()
    stranger = ff.FakeObject("Body", "Body")
    with pytest.raises(GoliathPostError, match="'Body'"):
        goliath_post.export(ff.post_list(job, tc, op, stranger), "-", "--tower2=Y")
    orphan = ff.FakeOp("Profile002", cmds("G0 Z23.0", "G1 Z-0.2 F1.0"), tc)
    with pytest.raises(GoliathPostError, match="not inside a CAM job"):
        goliath_post.export(ff.post_list(job, tc, orphan), "-", "--tower2=Y")
    other_job, other_tc, other_op = simple_job()
    with pytest.raises(GoliathPostError, match="more than one job"):
        goliath_post.export(ff.post_list(job, tc, op, other_op), "-", "--tower2=Y")


def test_export_refuses_an_operation_placement_outside_freecad(freecad: Any) -> None:
    job, tc, op = simple_job()
    op.Placement = ff.FakePlacement(identity=True)
    goliath_post.export(ff.post_list(job, tc, op), "-", "--tower2=Y")
    op.Placement = ff.FakePlacement(identity=False)
    with pytest.raises(GoliathPostError, match="placement"):
        goliath_post.export(ff.post_list(job, tc, op), "-", "--tower2=Y")


def test_export_outside_freecad_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    job, tc, op = simple_job()
    monkeypatch.delitem(sys.modules, "FreeCAD", raising=False)
    if importlib.util.find_spec("FreeCAD") is not None:
        pytest.skip("a real FreeCAD module is importable here; the outside-FreeCAD refusal cannot be shown")
    with pytest.raises(GoliathPostError, match="inside FreeCAD"):
        goliath_post.export(ff.post_list(job, tc, op), "-", "--tower2=Y")


# --------------------------------------------------------------------------
# How FreeCAD loads the post
# --------------------------------------------------------------------------

LOAD_LIKE_FREECAD = textwrap.dedent(
    """
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("goliath_post", sys.argv[1])
    post = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(post)  # FreeCAD 1.1 loads a script post like this, without sys.path or sys.modules
    g = post.gi_format
    class C:
        def __init__(self, name, params):
            self.Name, self.Parameters = name, params
    op = g.OpData("a", "profile", "A", 1, 1000.0, (C("G0", {"Z": 5.0}), C("G1", {"Z": -1.0, "F": 10.0})))
    text = g.format_gi([op], g.StockData(0, 0, 0, 10, 10, 2), [g.ToolData(1, 3.0, 9.0, 1000.0)], tower2="Y", cam="x")
    print(g.__name__, g.__file__)
    print(text.splitlines()[-4])
    """
)


def test_post_loads_its_formatter_like_freecad_does(tmp_path: Path) -> None:
    # A foreign module named gi_format earlier on the path must be neither used nor executed.
    (tmp_path / "gi_format.py").write_text("raise RuntimeError('foreign gi_format executed')\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", LOAD_LIKE_FREECAD, str(ff.POST_DIR / "goliath_post.py")],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=dict(os.environ, PYTHONPATH=str(tmp_path)),
    )
    assert result.returncode == 0, result.stderr
    name, origin = result.stdout.splitlines()[0].split(" ", 1)
    assert name != "gi_format" and Path(origin).resolve() == (ff.POST_DIR / "gi_format.py").resolve()
    assert result.stdout.splitlines()[1] == "G1 Z-3.000 F600"


FIND_POST_LIKE_FREECAD = textwrap.dedent(
    """
    import importlib.util, os, sys, types
    errors = []
    sys.modules["FreeCAD"] = types.SimpleNamespace(Console=types.SimpleNamespace(PrintError=errors.append))
    spec = importlib.util.spec_from_file_location("goliath_post", os.path.join(sys.argv[1], "goliath_post.py"))
    post = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(post)
    except ImportError:
        pass  # FreeCAD 1.1.4's post search skips a post that raises ImportError and tries the next folder
    print(repr(errors))
    """
)


def test_post_without_its_formatter_says_so_in_the_report_view(tmp_path: Path) -> None:
    # FreeCAD swallows the ImportError while it searches for posts, so the report view is the only
    # place the owner can learn that gi_format.py was not copied next to goliath_post.py.
    shutil.copyfile(str(ff.POST_DIR / "goliath_post.py"), str(tmp_path / "goliath_post.py"))
    result = subprocess.run(
        [sys.executable, "-c", FIND_POST_LIKE_FREECAD, str(tmp_path)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    errors = ast.literal_eval(result.stdout.strip())
    assert len(errors) == 1, errors
    assert "gi_format.py" in errors[0] and str(tmp_path.resolve()) in errors[0] and errors[0].endswith("\n")


LOAD_TWICE_LIKE_FREECAD = textwrap.dedent(
    """
    import importlib.util, os, sys, types
    errors = []
    sys.modules["FreeCAD"] = types.SimpleNamespace(Console=types.SimpleNamespace(PrintError=errors.append))
    outcomes = []
    for attempt in (1, 2):  # FreeCAD searches for and loads the post again on every Post Process
        spec = importlib.util.spec_from_file_location("goliath_post", os.path.join(sys.argv[1], "goliath_post.py"))
        post = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(post)
            outcomes.append("loaded")
        except ImportError:
            outcomes.append("ImportError")
    print(repr((outcomes, len(errors))))
    """
)


def test_post_never_uses_a_foreign_gi_format_next_to_it(tmp_path: Path) -> None:
    shutil.copyfile(str(ff.POST_DIR / "goliath_post.py"), str(tmp_path / "goliath_post.py"))
    (tmp_path / "gi_format.py").write_text("class GoliathPostError(Exception):\n    pass\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", LOAD_TWICE_LIKE_FREECAD, str(tmp_path)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert ast.literal_eval(result.stdout.strip()) == (["ImportError", "ImportError"], 2)


def test_post_declares_what_freecad_reads() -> None:
    assert goliath_post.UNITS == "G21"  # FreeCAD 1.1 reads it to choose metric
    assert "--tower2" in goliath_post.TOOLTIP_ARGS and goliath_post.TOOLTIP
    assert gi_format.POST_NAME == "goliath_post.py" and re.match(r"^\d+\.\d+\.\d+$", gi_format.POST_VERSION)


def _parsed(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_post_never_reads_job_toml_or_any_other_file() -> None:
    # Red-team PHYS-12: STOCK and TOWER2 come from the FreeCAD job only, so a conflict
    # with job.toml reaches goliath-prep's reader, which refuses it naming both.
    opens = []
    for name in ("goliath_post.py", "gi_format.py"):
        tree = _parsed(ff.POST_DIR / name)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for module in modules:
                    assert module.split(".")[0] not in {"tomllib", "tomli", "toml", "json", "configparser", "os"}, (
                        name,
                        module,
                    )
            if isinstance(node, ast.Call):
                func = node.func
                called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if called in {"open", "read_text", "read_bytes", "load", "loads"}:
                    opens.append((name, called, ast.unparse(node) if hasattr(ast, "unparse") else called))
    assert len(opens) == 1 and opens[0][0] == "goliath_post.py" and opens[0][1] == "open", opens
    if hasattr(ast, "unparse"):
        assert "'w'" in opens[0][2] and "encoding='utf-8'" in opens[0][2] and "newline='\\n'" in opens[0][2]


def test_gi_format_imports_no_freecad() -> None:
    tree = _parsed(ff.POST_DIR / "gi_format.py")
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "dataclasses", "decimal", "math", "re", "typing"}, imported


# --------------------------------------------------------------------------
# Committed samples
# --------------------------------------------------------------------------

SAMPLE_NAMES = sorted(ff.SAMPLES)


def test_readme_says_dwells_wait_for_the_dwell_gate() -> None:
    # The post writes G4 (FreeCAD's dwell, and each expanded G82); gi-format.md 8.5 refuses G4
    # while the gate drop_dwells is closed. The owner must learn that from the README, not from a refusal.
    readme = (ff.POST_DIR / "README.md").read_text(encoding="utf-8")
    assert "`G4`" in readme and "drop_dwells" in readme and "R4" in readme


def test_samples_folder_holds_exactly_the_samples() -> None:
    on_disk = sorted(p.name for p in ff.SAMPLES_DIR.glob("*.gi"))
    assert on_disk == SAMPLE_NAMES
    attributes = (ff.SAMPLES_DIR / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"^\*\.gi\s+-text\b", attributes, re.MULTILINE), "samples must be stored byte for byte"


@pytest.mark.parametrize("name", SAMPLE_NAMES)
def test_committed_samples_are_what_the_post_writes(name: str) -> None:
    committed = (ff.SAMPLES_DIR / name).read_bytes()
    assert committed == ff.render_sample(name).encode("utf-8"), (
        "%s is stale: run python tests/posts/freecad_fakes.py --write-samples and review the diff" % name
    )


_WORD = re.compile(r"([A-Z])([+-]?\d+(?:\.\d+)?)")
_KEYWORDS = {"SOURCE", "UNITS", "STOCK", "TOWER2", "TOOL", "ZSHIFT", "OP", "OPEND"}
_G_CODES = {0, 1, 2, 3, 4, 17, 21, 90}
_M_CODES = {3, 5, 6, 7, 8, 9, 30}


def gi_writing_rule_problems(text: str) -> List[str]:
    """Problems against gi-format.md section 14 ("Writing a post") and the post's own conventions.

    A lint, not a reader: goliath-prep's reader is the authority
    (test_committed_samples_pass_goliath_prep_check).
    """
    problems = []
    if "\r" in text or not text.endswith("\n"):
        problems.append("line endings must be LF with a final newline")
    lines = text[:-1].split("\n")
    if lines[0] != ";GOLIATH-GI 1":
        problems.append("line 1 must be the version line")
    open_op = None
    ids = set()
    after_end = False
    for number, line in enumerate(lines[1:], start=2):
        where = "line %d: %s" % (number, line)
        if after_end:
            problems.append("%s: after M30" % where)
        if line.startswith(";GOLIATH"):
            match = re.match(r";GOLIATH ([A-Z0-9]+)( |$)", line)
            if not match or match.group(1) not in _KEYWORDS:
                problems.append("%s: not a GI annotation" % where)
                continue
            keyword = match.group(1)
            if keyword == "OP":
                op_id = re.search(r" id=(\S+)", line).group(1)  # type: ignore[union-attr]
                if open_op is not None or op_id in ids:
                    problems.append("%s: nested or repeated OP" % where)
                open_op = op_id
                ids.add(op_id)
            elif keyword == "OPEND":
                if line != ";GOLIATH OPEND id=%s" % open_op:
                    problems.append("%s: OPEND does not close the open OP" % where)
                open_op = None
            continue
        if line.startswith(";"):
            if not (line.startswith("; Note: ") or line.startswith("; FreeCAD: ")):
                problems.append("%s: the post's comments start with '; Note: ' or '; FreeCAD: '" % where)
            continue
        if ";" in line or "(" in line or "%" in line or not line.isascii():
            problems.append("%s: code lines carry no comment, parenthesis, percent sign or non-ASCII" % where)
            continue
        words = line.split(" ")
        motion = False
        for word in words:
            match = _WORD.fullmatch(word)
            if not match:
                problems.append("%s: %r is not a word" % (where, word))
                continue
            letter, value = match.groups()
            if letter == "G":
                if int(value) not in _G_CODES:
                    problems.append("%s: G%s" % (where, value))
                motion = motion or int(value) in (0, 1, 2, 3)
            elif letter == "M":
                if int(value) not in _M_CODES:
                    problems.append("%s: M%s" % (where, value))
                after_end = after_end or int(value) == 30
            elif letter in "XYZIJ":
                if not re.fullmatch(r"-?\d+\.\d{3}", value):
                    problems.append("%s: %s needs three decimals" % (where, word))
            elif letter in "FST":
                if not re.fullmatch(r"\d+", value):
                    problems.append("%s: %s needs a whole number" % (where, word))
            elif letter == "P":
                if not re.fullmatch(r"\d+\.\d{3}", value):
                    problems.append("%s: %s" % (where, word))
            else:
                problems.append("%s: letter %s" % (where, letter))
        if motion and open_op is None:
            problems.append("%s: motion outside every OP block" % where)
    if open_op is not None:
        problems.append("OP %s never closed" % open_op)
    return problems


@pytest.mark.parametrize("name", SAMPLE_NAMES)
def test_committed_samples_follow_the_gi_writing_rules(name: str) -> None:
    text = (ff.SAMPLES_DIR / name).read_bytes().decode("utf-8")
    assert gi_writing_rule_problems(text) == []
    assert 'cam="FreeCAD (stand-in objects, not a FreeCAD run)"' in text, "samples say they are not a FreeCAD run"


def test_writing_rule_lint_catches_what_it_should() -> None:
    good = (ff.SAMPLES_DIR / SAMPLE_NAMES[0]).read_bytes().decode("utf-8")
    assert gi_writing_rule_problems(good.replace("G0 Z5.000", "G0 C90 Z5.000", 1))
    assert gi_writing_rule_problems(good.replace("M3 S", "M10 S", 1))
    assert gi_writing_rule_problems(good.replace("; FreeCAD: ", "; S", 1))
    assert gi_writing_rule_problems(good.replace("\n", "\r\n"))
    assert gi_writing_rule_problems(good.replace("G0 Z5.000", "G0 Z5.0", 1))


def _header_values(text: str) -> Dict[str, Any]:
    header: Dict[str, Any] = {}
    for line in text.split("\n"):
        match = re.match(r";GOLIATH (STOCK|TOWER2|TOOL) (.*)$", line)
        if match:
            for key, value in re.findall(r'(\w+)=("[^"]*"|\S+)', match.group(2)):
                header["%s.%s" % (match.group(1).lower(), key)] = value
    return header


def _sample_job_toml(text: str) -> str:
    """The job.toml that goes with a sample: the same panel, tower axis and tool (gi-format.md section 14, point 8).

    PROVISIONAL BRIDGE. This was written before goliath-prep's job file format
    (unit P01) existed. The keys follow the project design's description of
    job.toml (panel W, H, T and tower2; tool d, flute, rpm; units). When
    goliath-prep's loader refuses this text, update this function to the job
    file format goliath-prep documents; do not weaken the assertion below.
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
    if importlib.util.find_spec("goliath_prep.cli") is not None:
        pytest.fail(
            "goliath-prep's check command exists now (goliath_prep.cli, unit P17): extend this test to run "
            "`goliath-prep check` on every sample with its job.toml, as the design asks"
        )
    if importlib.util.find_spec("goliath_prep.gcode") is None:
        pytest.skip(
            "goliath-prep's GI reader goliath_prep.gcode (unit P02) and its check command (unit P17) do not "
            "exist yet; this test runs the samples through them as soon as they do"
        )
    return importlib.import_module("goliath_prep.gcode")


@pytest.mark.parametrize("name", SAMPLE_NAMES)
def test_committed_samples_pass_goliath_prep_check(name: str, tmp_path: Path) -> None:
    reader = _goliath_prep_reader()
    text = (ff.SAMPLES_DIR / name).read_bytes().decode("utf-8")
    job_toml = tmp_path / "job.toml"
    job_toml.write_text(_sample_job_toml(text), encoding="utf-8")
    jobspec = importlib.import_module("goliath_prep.jobspec")  # unit P01; the reader needs it
    job = jobspec.JobSpec.load(job_toml)
    result = reader.read_gcode(text, flavour="gi", job=job)
    refusals = [f for f in result.findings if getattr(f, "severity", None) == "refuse"]
    assert refusals == [], "%s is refused by goliath-prep's reader: %r" % (name, refusals)
    assert result.gi_header is not None
