# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""G-code cleaning for upload.

Before upload the vendor app strips comments and normalises line endings
(``GCodeCleaner.GetGCodeForGoliath``; see ``docs/protocol/05-gcode-dialect.md``):
everything from the first ``;`` on each line to end-of-line is removed, lines are
joined with CRLF, and the text ends with CRLF. The byte length announced in
``GCODE UPLOAD;<n>`` must be the length of this cleaned text.

The operation-boundary markers ``;U`` (tool change) and ``;S`` (empty dust bin)
are ordinary comments as far as cleaning is concerned — they are stripped here;
the robot recognises the operation boundaries from the motion stream itself.
"""

from __future__ import annotations


def clean_gcode(text: str) -> bytes:
    """Return the exact bytes to stream to the robot for ``text``.

    Comments (``;`` to EOL) are removed from each line, lines are joined with
    CRLF, and a trailing CRLF is guaranteed.
    """
    out_lines = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        semi = raw_line.find(";")
        line = raw_line if semi < 0 else raw_line[:semi]
        out_lines.append(line)
    # Drop a single trailing empty line produced by a trailing newline so we
    # don't emit a spurious blank line, then re-append exactly one CRLF ending.
    while out_lines and out_lines[-1] == "":
        out_lines.pop()
    cleaned = "\r\n".join(out_lines)
    if cleaned:
        cleaned += "\r\n"
    return cleaned.encode("ascii")
