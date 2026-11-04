# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The lexical layer of the reader, and its reuse of goliath-link's line split (unit P02).

Lines are split by ``goliath_link.gcode.read_all_lines`` (the .NET
``File.ReadAllLines`` split goliath-link uses for upload), imported, never
copied: an architecture test checks the reader calls it and splits lines no
other way.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from goliath_link.gcode import read_all_lines
from goliath_prep.gcode import lexer
from goliath_prep.gcode.lexer import Annotation, AnnotationError, lex_annotation, lex_line, split_lines


def test_reader_splits_lines_with_goliath_link(astscan) -> None:
    calls = [c for c in astscan.find_calls("goliath_prep.gcode", "goliath_link.gcode.read_all_lines") if c.exact]
    assert calls, "the reader must split lines with goliath_link.gcode.read_all_lines"
    assert lexer.read_all_lines is read_all_lines
    for name in ("splitlines", "str.splitlines"):
        assert not astscan.find_calls("goliath_prep.gcode", name), "lines are split one way only"
    # The query finds a known positive, so a typo cannot make the absence check pass.
    assert astscan.find_calls("goliath_prep.gcode", "read_all_lines")


@given(st.text(alphabet=st.sampled_from(list("ab;\r\n \t")), max_size=60))
def test_split_matches_goliath_link_exactly(text: str) -> None:
    lines, bom = split_lines(text)
    assert lines == read_all_lines(text) and bom is False
    lines, bom = split_lines("﻿" + text)
    assert lines == read_all_lines(text) and bom is True


def test_words_keep_the_number_as_written() -> None:
    line = lex_line(3, "G01X-1.2500 Y+7\tF800.5 ;comment ; more  ")
    assert line.error is None
    assert [(w.letter, w.text, w.column) for w in line.words] == [
        ("G", "01", 1), ("X", "-1.2500", 4), ("Y", "+7", 13), ("F", "800.5", 17)]
    assert line.words[1].value == Decimal("-1.25") and str(line.words[1].value) == "-1.2500"
    assert line.comment == "comment ; more" and line.comment_column == 24
    assert line.words[0].digits_only and line.words[2].signed and not line.words[3].integral


@pytest.mark.parametrize(
    "raw,column",
    [("G0 X1.000 y2", 11), ("G0X.5", 4), ("G0 X5.", 6), ("G0 X1.2.3", 8), ("G0 X1,5", 6), ("X1e3", 3),
     ("G0 (c)", 4), ("% ", 1), ("G0 X1 é", 7), ("5", 1), ("G0 X", 5), ("G0 X-", 5), ("G0 X-.5", 5),
     ("G0 X 1", 5), ("G0 =", 4)],
)
def test_lex_errors_name_the_column(raw: str, column: int) -> None:
    line = lex_line(1, raw)
    assert line.error is not None and line.error.column == column, line.error


def test_comment_kinds() -> None:
    assert lexer.comment_kind("GOLIATH OP") == "annotation"
    assert lexer.comment_kind("  goliath op") == "annotation"
    assert lexer.comment_kind("U|1|tool") == "marker"
    assert lexer.comment_kind(" s") == "marker"
    assert lexer.comment_kind(" Note: spindle") == "plain"
    assert lexer.comment_kind("") == "plain"


def test_annotation_grammar() -> None:
    parsed = lex_annotation(lex_line(4, ';GOLIATH TOOL n=1 name="6mm \\"2F\\" \\\\ up" 7 kind=flat'))
    assert isinstance(parsed, Annotation) and parsed.keyword == "TOOL" and parsed.keyword_column == 10
    assert [(i.key, i.value, i.quoted) for i in parsed.items] == [
        ("n", "1", False), ("name", '6mm "2F" \\ up', True), (None, "7", False), ("kind", "flat", False)]
    for raw in (';GOLIATH TOOL name="a', ";GOLIATH TOOL n=1 n=1", ";GOLIATH", ";GOLIATH tool", "; GOLIATH TOOL",
                ';GOLIATH TOOL name="a"b', ";GOLIATH TOOL n=", ";GOLIATH TOOL n=é",
                ';GOLIATH TOOL name="tab\there"'):
        assert isinstance(lex_annotation(lex_line(1, raw)), AnnotationError), raw
