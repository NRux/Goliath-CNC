# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The strict G-code reader of goliath-prep (unit P02; the project design, section 4.2).

``read_gcode(text, *, flavour, job, gates=None, file="<input>") -> ReadResult``
reads GI (docs/prep/gi-format.md), generic millimetre G-code with a job.toml,
or the Goliath dialect in verify mode. It refuses everything outside a small
vocabulary, naming line and column, and never raises on file content.

* :mod:`goliath_prep.gcode.lexer` - lines (goliath-link's .NET-compatible
  split), words, comments, annotations;
* :mod:`goliath_prep.gcode.flavours` - the vocabulary of each flavour, the GI
  header model, the GI header against job.toml;
* :mod:`goliath_prep.gcode.parser` - :func:`read_gcode` and the raw program.
"""
from goliath_prep.gcode.flavours import (
    FLAVOURS,
    Flavour,
    GIHeader,
    GIOp,
    GISource,
    GIStock,
    GITool,
    GITower2,
    GIUnits,
    GIZShift,
)
from goliath_prep.gcode.parser import RawEvent, RawMove, RawProgram, ReadNote, ReadResult, read_gcode

__all__ = [
    "FLAVOURS",
    "Flavour",
    "GIHeader",
    "GIOp",
    "GISource",
    "GIStock",
    "GITool",
    "GITower2",
    "GIUnits",
    "GIZShift",
    "RawEvent",
    "RawMove",
    "RawProgram",
    "ReadNote",
    "ReadResult",
    "read_gcode",
]
