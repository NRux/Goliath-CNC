# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""G-code cleaning for upload.

Before upload the vendor app strips comments and normalises line endings
(``docs/protocol/05-gcode-dialect.md``). :func:`clean_gcode` reproduces the
vendor's bytes exactly, because the byte count announced in
``GCODE UPLOAD;<n>`` must equal the bytes streamed, and because the vendor's
stream is the only one known to work on a robot:

1. Split the text into lines the way .NET ``File.ReadAllLines`` does: at
   ``\\r\\n``, ``\\r`` or ``\\n`` (nothing else), with no empty last line for a
   final line terminator.
2. In every line, drop everything from the first ``;`` (comments). Blank and
   comment-only lines are kept as empty lines, so line numbers stay aligned
   with the source file (position frames report the line being cut).
3. Append CRLF to every line.
4. Make the text end in CRLF CRLF (an empty last line): if it does not end in
   CRLF, append CRLF CRLF; if it ends in a single CRLF, append one more.

So ``"G01 X1\\nG01 X2\\n"`` becomes ``b"G01 X1\\r\\nG01 X2\\r\\n\\r\\n"`` (18 bytes),
and an empty or comment-only file becomes ``b"\\r\\n\\r\\n"``. Whether the
firmware needs the final empty line is unknown; the vendor always sent it.

Differences from the vendor, all loud rather than silent: a non-ASCII
character outside a comment raises :class:`GCodeEncodingError` naming the line
(the vendor replaced it with ``?``), and so does a UTF-8 byte-order mark (the
vendor stripped it; open the file with ``encoding="utf-8-sig"``).

The operation-boundary markers ``;U`` (tool change) and ``;S`` (empty dust bin)
are ordinary comments as far as cleaning is concerned and are stripped.

An empty or comment-only program cleans to ``b"\\r\\n\\r\\n"``;
:func:`has_program_content` says whether a cleaned program has anything to
run (the client refuses to upload one that does not).
"""

from __future__ import annotations

from typing import List

CRLF = "\r\n"


class GCodeEncodingError(ValueError):
    """The program contains a character the robot's ASCII stream cannot carry."""


def read_all_lines(text: str) -> List[str]:
    """Split ``text`` like .NET ``File.ReadAllLines``.

    Line terminators are ``\\r\\n``, ``\\r`` and ``\\n`` only (unlike
    :meth:`str.splitlines`). A terminator at the very end does not produce an
    empty last line; an empty text gives no lines.
    """
    lines: List[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n" or ch == "\r":
            lines.append(text[start:i])
            if ch == "\r" and i + 1 < n and text[i + 1] == "\n":
                i += 1
            start = i + 1
        i += 1
    if start < n:
        lines.append(text[start:])
    return lines


def _strip_comment(line: str) -> str:
    semi = line.find(";")
    return line if semi < 0 else line[:semi]


def _encode_line(line: str, number: int) -> bytes:
    try:
        return line.encode("ascii")
    except UnicodeEncodeError as exc:
        ch = line[exc.start]
        if ch == "﻿":
            raise GCodeEncodingError(
                f"line {number}, column {exc.start + 1}: U+FEFF byte-order mark; the "
                "file was read as 'utf-8' but starts with a BOM: open it with "
                "encoding='utf-8-sig'"
            ) from None
        raise GCodeEncodingError(
            f"line {number}, column {exc.start + 1}: character U+{ord(ch):04X} "
            f"({ch!r}) is not ASCII; the robot receives ASCII G-code only (the vendor "
            "app silently replaced such characters with '?')"
        ) from None


def clean_gcode(text: str) -> bytes:
    """Return the exact bytes the vendor app would stream for ``text``.

    See the module docstring for the rules. Raises :class:`GCodeEncodingError`
    (a ``ValueError``) for a non-ASCII character outside a comment.
    """
    parts = []
    for number, line in enumerate(read_all_lines(text), start=1):
        parts.append(_encode_line(_strip_comment(line), number))
        parts.append(b"\r\n")
    out = b"".join(parts)
    if not out.endswith(b"\r\n"):
        out += b"\r\n\r\n"
    elif not out.endswith(b"\r\n\r\n"):
        out += b"\r\n"
    return out


def has_program_content(payload: bytes) -> bool:
    """True if a cleaned program contains anything but whitespace."""
    return bool(bytes(payload).strip())


def check_cleaned(payload: bytes) -> bytes:
    """Return ``payload`` if it is already in cleaned form; raise ``ValueError`` if not.

    Cleaned form is exactly what :func:`clean_gcode` produces: ASCII, no
    comments, CRLF line endings, ending in CRLF CRLF. Use it to check bytes
    prepared elsewhere before they are streamed verbatim.
    """
    data = bytes(payload)
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise GCodeEncodingError(
            f"byte {exc.start}: 0x{data[exc.start]:02X} is not ASCII; G-code bytes for "
            "upload must be cleaned ASCII (use clean_gcode(text))"
        ) from None
    cleaned = clean_gcode(text)
    if cleaned != data:
        at = next(
            (i for i, (a, b) in enumerate(zip(cleaned, data)) if a != b),
            min(len(cleaned), len(data)),
        )
        raise ValueError(
            "these G-code bytes are not in cleaned form (comments stripped, CRLF line "
            f"endings, ending in CRLF CRLF): cleaning would change them at byte {at} "
            f"(got {data[at:at + 12]!r}, cleaned {cleaned[at:at + 12]!r}). Pass the text "
            "instead, or clean_gcode(text)"
        )
    return data
