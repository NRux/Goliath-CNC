# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The normaliser and the pieces on real files (unit P03).

Every conformance example the reader accepts (docs/prep/gi-examples,
manifest.json) normalises and cuts into pieces with the manifest's job; every
example the reader refuses is refused by the normaliser too, never planned.
The FreeCAD post's samples (posts/freecad/samples), which hold helices, full
circles and several operations, normalise against a job.toml stating the same
panel.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from goliath_prep.gcode import read_gcode
from goliath_prep.jobspec import JobSpec
from goliath_prep.normalise import NormaliseError, normalise
from goliath_prep.pieces import pieces

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "docs" / "prep" / "gi-examples"
MANIFEST: list[dict[str, Any]] = json.loads((EXAMPLES / "manifest.json").read_text(encoding="utf-8"))
SAMPLES = sorted((REPO / "posts" / "freecad" / "samples").glob("*.gi"))


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


@pytest.mark.parametrize("entry", MANIFEST, ids=[e["file"] for e in MANIFEST])
def test_conformance_examples(entry: dict[str, Any]) -> None:
    job = JobSpec.from_mapping(entry["job"])
    result = read_gcode(_text(EXAMPLES / entry["file"]), flavour="gi", job=job, file=entry["file"])
    if entry["expect"] == "refused":
        assert result.refused
        with pytest.raises(NormaliseError, match="refused"):
            normalise(result, job)
        return
    program = normalise(result, job)
    array = pieces(program, 50.0)
    assert len(array) + len(array.unplaced) >= len(program.moves)
    assert all(note.text for note in program.notes)
    assert [r.src.line for r in program.arcs] == [m.src.line for m in result.program_raw.moves if m.is_arc]


@pytest.mark.parametrize("path", SAMPLES, ids=[p.name for p in SAMPLES])
def test_freecad_post_samples(path: Path) -> None:
    text = _text(path)
    header = read_gcode(text, flavour="gi", job=JobSpec.from_mapping(MANIFEST[0]["job"])).gi_header
    assert header is not None
    job = JobSpec.from_mapping({
        "panel": {"W": float(header.stock.x), "H": float(header.stock.y), "T": float(header.stock.t),
                  "tower2": header.tower2.axis},
        "z_zero": header.stock.zzero,
        "tool": {"d": float(header.tool.d), "flute": float(header.tool.flute)},
    })
    result = read_gcode(text, flavour="gi", job=job, file=path.name)
    assert not result.refused
    program = normalise(result, job)
    assert program.cuts and program.meta.ops_source == "blocks"
    for cut in program.cuts:
        assert cut.op is not None and cut.ops == (cut.op.id,)
    array = pieces(program, 50.0)
    assert bool((array["sagitta"] <= 0.05).all())
    assert array.unplaced == tuple(m.index for m in program.moves if not m.located)


def test_samples_exist() -> None:
    assert len(SAMPLES) >= 3
    assert any(e["expect"] == "ok" for e in MANIFEST) and any(e["expect"] == "refused" for e in MANIFEST)
