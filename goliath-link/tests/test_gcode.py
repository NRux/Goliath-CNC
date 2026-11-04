# SPDX-License-Identifier: Apache-2.0
"""G-code cleaning: the bytes streamed to the robot must match the vendor's.

The reference below is written from the cleaning rules in
``docs/protocol/05-gcode-dialect.md`` (not from vendor code): split the text
into lines like .NET ``File.ReadAllLines`` (``\\r\\n``, ``\\r`` or ``\\n``; a
final terminator does not create an empty last line), drop everything from the
first ``;`` of each line, append CRLF to every line, then make the text end in
CRLF CRLF (append CRLF CRLF if it does not end in CRLF, one more CRLF if it
ends in a single CRLF).
"""
import pytest

from goliath_link import gcode as gcode_mod
from goliath_link.gcode import clean_gcode


def read_all_lines(text):
    lines, buf, i = [], "", 0
    while i < len(text):
        ch = text[i]
        if ch in "\r\n":
            lines.append(buf)
            buf = ""
            if ch == "\r" and i + 1 < len(text) and text[i + 1] == "\n":
                i += 1
        else:
            buf += ch
        i += 1
    if buf:
        lines.append(buf)
    return lines


def spec_reference(text):
    out = "".join((line.split(";", 1)[0]) + "\r\n" for line in read_all_lines(text))
    if not out.endswith("\r\n"):
        out += "\r\n\r\n"
    elif not out.endswith("\r\n\r\n"):
        out += "\r\n"
    return out.encode("ascii")


CASES = {
    "simple": "G01 X1\nG01 X2\n",
    "no final newline": "G01 X1\nG01 X2",
    "comments stripped": "G01 X10.000 Y20.000 ;layer|PROFILO\nM05 ;stop\n",
    "blank footer line kept": "G01 X1\nM05\n\n",
    "several trailing blank lines kept": "G00 X0\n\n\n",
    "trailing ;S marker becomes an empty line": "G01 X1\nM05\n;S|1\n",
    "comment-only": ";h\n;U|t|\n",
    "empty": "",
    "crlf input": "G00 X1\r\nG00 X2\r\n",
    "cr-only input": "G00 X1\rG00 X2\r",
    "mixed endings": "G00 X1\r\nG00 X2\rG00 X3\nG00 X4",
    "lone semicolon": ";\n",
    "parentheses kept": "G01 X1 (keep me)\n",
    "interior blank and comment lines kept": "G00 X0\n\n;c\nG01 X1\n",
}


# MED-5: the vendor stream always ends with CRLF CRLF.
@pytest.mark.parametrize("name", sorted(CASES))
def test_med5_clean_matches_the_spec_reference(name):
    assert clean_gcode(CASES[name]) == spec_reference(CASES[name])


def test_med5_simple_program_ends_with_crlf_crlf_and_counts_18_bytes():
    out = clean_gcode("G01 X1\nG01 X2\n")
    assert out == b"G01 X1\r\nG01 X2\r\n\r\n"
    assert len(out) == 18


def test_med5_empty_and_comment_only_become_crlf_crlf_like_the_vendor():
    assert clean_gcode("") == b"\r\n\r\n"
    assert clean_gcode(";only a comment\n") == b"\r\n\r\n"


def test_strips_comments_and_uses_crlf():
    src = "G01 X10.000 Y20.000 ;layer|PROFILO\nM05 ;stop\n"
    assert clean_gcode(src) == b"G01 X10.000 Y20.000 \r\nM05 \r\n\r\n"


def test_upload_size_is_cleaned_length():
    cleaned = clean_gcode("G01 X1 ;c\nG01 X2 ;c\n")
    assert cleaned == b"G01 X1 \r\nG01 X2 \r\n\r\n"


def test_unicode_line_separators_are_not_line_breaks():
    # str.splitlines() would split on these; ReadAllLines does not. They are
    # not ASCII either, so cleaning must refuse them loudly.
    with pytest.raises(ValueError, match="line 1"):
        clean_gcode("G01 X1 G01 X2\n")


def test_non_ascii_is_a_clear_error_naming_the_line():
    with pytest.raises(ValueError) as ei:
        clean_gcode("G01 X1\nG01 X2 °\n")
    msg = str(ei.value)
    assert "line 2" in msg and "U+00B0" in msg


def test_byte_order_mark_error_says_how_to_fix_it():
    with pytest.raises(ValueError, match="utf-8-sig"):
        clean_gcode("﻿G01 X1\n")


def test_non_ascii_inside_a_comment_is_stripped_not_an_error():
    assert clean_gcode("G01 X1 ;café\n") == b"G01 X1 \r\n\r\n"


# LOW-3 helpers: an empty or comment-only program has no content to upload.
def test_has_program_content():
    assert gcode_mod.has_program_content(clean_gcode("G01 X1\n"))
    assert not gcode_mod.has_program_content(clean_gcode(""))
    assert not gcode_mod.has_program_content(clean_gcode(";a\n;b\n   \n\t\n"))


# Bytes given to upload must already be in cleaned form.
def test_check_cleaned_accepts_cleaned_bytes():
    cleaned = clean_gcode("G01 X1 ;c\nM05\n")
    assert gcode_mod.check_cleaned(cleaned) == cleaned


@pytest.mark.parametrize(
    "raw, why",
    [
        (b"G01 X1\nG01 X2\n", "LF line endings"),
        (b";U|tool 1|\r\nG01 X1\r\n\r\n", "comment"),
        (b"G01 X1\r\n", "single CRLF ending"),
        (b"G01 X1 \xb0\r\n\r\n", "non-ASCII"),
    ],
)
def test_check_cleaned_rejects_uncleaned_bytes(raw, why):
    with pytest.raises(ValueError):
        gcode_mod.check_cleaned(raw)
