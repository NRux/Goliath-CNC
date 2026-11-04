# SPDX-License-Identifier: Apache-2.0
from goliath_link.gcode import clean_gcode


def test_strips_comments_and_uses_crlf():
    src = "G01 X10.000 Y20.000 ;layer|PROFILO\nM05 ;stop\n"
    out = clean_gcode(src)
    assert out == b"G01 X10.000 Y20.000 \r\nM05 \r\n"


def test_ends_with_single_crlf_and_no_trailing_blank_lines():
    out = clean_gcode("G00 X0\n\n\n")
    assert out == b"G00 X0\r\n"


def test_empty_input_is_empty_bytes():
    assert clean_gcode("") == b""
    assert clean_gcode(";only a comment\n") == b""


def test_handles_crlf_input():
    assert clean_gcode("G00 X1\r\nG00 X2\r\n") == b"G00 X1\r\nG00 X2\r\n"


def test_upload_size_is_cleaned_length():
    # The GCODE UPLOAD;<n> count must be len(clean_gcode(...)).
    cleaned = clean_gcode("G01 X1 ;c\nG01 X2 ;c\n")
    assert len(cleaned) == len(b"G01 X1 \r\nG01 X2 \r\n")
