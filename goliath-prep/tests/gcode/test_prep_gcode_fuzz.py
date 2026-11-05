# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The reader never raises on any text: it reports findings (unit P02).

Whatever a file holds, the reader returns a result, and everything it cannot
accept is a refusal that names a line of the file. Inputs are random text over
a G-code-heavy alphabet (with non-ASCII, control characters and the three line
terminators) and random edits of the conformance examples.
"""
from __future__ import annotations

from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from prep_gcode_support import JOB, JOB_MM, example_text, manifest

from goliath_prep.core.canon import canonical_json
from goliath_prep.core.findings import Finding
from goliath_prep.gcode import ReadResult, read_gcode

ALPHABET = st.sampled_from(
    list("GMXYZIJKRCFSTNHPABE0123456789.+-;() \t\r\n%=\"\\,eg")
    + ["G0 ", "G1 ", "G2 ", "G3 ", "M30", "M3", "M5", ";GOLIATH ", ";GOLIATH-GI 1", ";U", ";S", "OP ", "OPEND ",
       "id=a ", "x=1 ", "\ufeff", "\u2013", "\u00e9", "\x00", "\x7f", "\ud800", "\U0001f600", "1" * 30]
)
TEXTS = st.lists(ALPHABET, max_size=120).map("".join)
FLAVOURS = st.sampled_from(["auto", "gi", "generic", "dialect"])
EXAMPLES = sorted(entry["file"] for entry in manifest())


def check_result(result: ReadResult, text: str) -> None:
    assert isinstance(result, ReadResult)
    lines = max(1, result.program_raw.line_count)
    for finding in result.findings:
        assert isinstance(finding, Finding)
        assert finding.witness is not None and finding.witness.src is not None, finding
        assert 1 <= finding.witness.src.line <= lines, (finding, lines)
        # Every finding can be written to a report (canonical JSON refuses what is not UTF-8).
        canonical_json(finding.to_json())
    for item in result.program_raw.items:
        assert 1 <= item.src.line <= lines
        canonical_json(item.src.to_json())


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(text=TEXTS, flavour=FLAVOURS, units=st.booleans())
def test_fuzz_never_raises_only_findings(text: str, flavour: Any, units: bool) -> None:
    result = read_gcode(text, flavour=flavour, job=JOB_MM if units else JOB)
    check_result(result, text)


@st.composite
def edited_examples(draw: st.DrawFn) -> str:
    text = example_text(draw(st.sampled_from(EXAMPLES)))
    for _ in range(draw(st.integers(min_value=1, max_value=4))):
        at = draw(st.integers(min_value=0, max_value=len(text)))
        action = draw(st.sampled_from(["insert", "delete", "replace"]))
        piece = draw(TEXTS)
        if action == "insert":
            text = text[:at] + piece + text[at:]
        elif action == "delete":
            text = text[:at] + text[at + draw(st.integers(min_value=1, max_value=20)):]
        else:
            text = text[:at] + piece + text[at + len(piece):]
    return text


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(text=edited_examples(), flavour=FLAVOURS)
def test_fuzz_edited_examples_never_raise(text: str, flavour: Any) -> None:
    result = read_gcode(text, flavour=flavour, job=JOB)
    check_result(result, text)


def test_huge_numbers_are_findings_not_crashes() -> None:
    big = "9" * 5000
    text = ";GOLIATH-GI 1\n;GOLIATH TOOL n=%s d=1 flute=1\nT%s\nN%s G0 X%s\nF%s\nG2 I%s\n" % (big, big, big, big, big,
                                                                                         big)
    for flavour in ("gi", "generic", "dialect", "auto"):
        result = read_gcode(text, flavour=flavour, job=JOB_MM)
        check_result(result, text)
        assert any(f.severity == "refuse" for f in result.findings)


def test_empty_text_is_read_without_raising() -> None:
    for flavour in ("auto", "gi", "generic", "dialect"):
        result = read_gcode("", flavour=flavour, job=JOB_MM)
        check_result(result, "")
        assert not result.program_raw.items
    assert any(f.check_id == "D-GI-VERSION" for f in read_gcode("", flavour="gi", job=JOB).findings)
