# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Helpers shared by the tests of the strict G-code reader (unit P02).

Programs are built from lines so each test states the line it expects a
finding on. The job is the conformance examples' job (docs/prep/gi-examples/
manifest.json), read with P01's constructor ``JobSpec.from_mapping``.
"""
from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from goliath_prep.core.findings import Finding
from goliath_prep.gcode import ReadResult, read_gcode
from goliath_prep.jobspec import JobSpec

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "docs" / "prep" / "gi-examples"
MANIFEST = EXAMPLES / "manifest.json"
GI_SCHEMA = REPO / "docs" / "prep" / "gi.schema.json"

# The job of the reference GI examples (manifest.json), as job.toml would state it.
JOB_MAPPING: dict[str, Any] = {
    "panel": {"W": 2440.0, "H": 1220.0, "T": 18.0, "tower2": "Y"},
    "placement": {"dx": 0.0, "dy": 0.0},
    "z_zero": "top",
    "tool": {"d": 6.0, "flute": 22.0, "stickout": 40.0, "rpm": 18000},
}

GI_HEADER = [
    ";GOLIATH-GI 1",
    ';GOLIATH SOURCE cam="Test CAM 1.0" post="test-post 0.1"',
    ";GOLIATH UNITS mm",
    ";GOLIATH STOCK x=2440.000 y=1220.000 t=18.000 origin=t1-corner zzero=top",
    ";GOLIATH TOWER2 axis=Y",
    ';GOLIATH TOOL n=1 d=6.000 flute=22.000 stickout=40.000 kind=flat rpm=18000 name="6mm 2F upcut"',
]
"""Six header lines: a GI body built with :func:`gi_text` starts at line 7."""

# A body that moves, plunges and cuts: the state most refused words are tested in.
PREAMBLE = [
    "G0 Z10.000",
    "G0 X100.000 Y100.000",
    "M3 S18000",
    "G1 Z-2.000 F200",
]


def job(**overrides: Any) -> JobSpec:
    """The examples' job with top-level keys replaced (``units="mm"``) or sections merged (``tool={...}``)."""
    mapping = copy.deepcopy(JOB_MAPPING)
    for key, value in overrides.items():
        if value is None:
            mapping.pop(key, None)
        elif isinstance(value, Mapping) and isinstance(mapping.get(key), Mapping):
            merged = dict(mapping[key])
            for sub, sub_value in value.items():
                if sub_value is None:
                    merged.pop(sub, None)
                else:
                    merged[sub] = sub_value
            mapping[key] = merged
        else:
            mapping[key] = value
    return JobSpec.from_mapping(mapping)


JOB = job()
JOB_MM = job(units="mm")


def gi_text(body: Sequence[str], header: Sequence[str] = GI_HEADER) -> str:
    return "\n".join([*header, *body]) + "\n"


def lines_text(lines: Iterable[str]) -> str:
    return "\n".join(lines) + "\n"


def read_gi(body: Sequence[str], *, header: Sequence[str] = GI_HEADER, job_spec: JobSpec = JOB,
            **kwargs: Any) -> ReadResult:
    return read_gcode(gi_text(body, header), flavour="gi", job=job_spec, **kwargs)


def read_generic(lines: Sequence[str], *, job_spec: JobSpec = JOB_MM, **kwargs: Any) -> ReadResult:
    return read_gcode(lines_text(lines), flavour="generic", job=job_spec, **kwargs)


def read_dialect(lines: Sequence[str], *, job_spec: JobSpec = JOB, **kwargs: Any) -> ReadResult:
    return read_gcode(lines_text(lines), flavour="dialect", job=job_spec, **kwargs)


def refusals(result: ReadResult) -> list[Finding]:
    return [f for f in result.findings if f.severity == "refuse"]


def at(result: ReadResult, check_id: str, line: int) -> list[Finding]:
    """The findings ``check_id`` on ``line``."""
    return [f for f in result.findings
            if f.check_id == check_id and f.witness is not None and f.witness.src is not None
            and f.witness.src.line == line]


def describe(result: ReadResult) -> str:
    return "\n".join("%s %s: %s" % (f.check_id, f.severity, f.message) for f in result.findings) or "(no findings)"


def assert_clean(result: ReadResult) -> None:
    assert not refusals(result), describe(result)


def assert_refused(result: ReadResult, check_id: str, line: int, column: int | None = None) -> Finding:
    """A refusal ``check_id`` on ``line`` that names line and column (witness or message)."""
    found = [f for f in at(result, check_id, line) if f.severity == "refuse"]
    assert found, "expected %s on line %d; got:\n%s" % (check_id, line, describe(result))
    finding = found[0]
    if column is not None:
        named = [f for f in found if f.witness.column == column or ("column %d" % column) in f.message]
        assert named, "expected %s on line %d to name column %d; got:\n%s" % (check_id, line, column,
                                                                             describe(result))
        finding = named[0]
    assert ("line %d" % line) in finding.message, finding.message
    return finding


def manifest() -> list[dict[str, Any]]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(data, list) and data, "manifest.json must be a non-empty list"
    return data


def example_text(rel: str) -> str:
    """An example's text exactly as stored (UTF-8; a byte-order mark is left for the reader to handle)."""
    return (EXAMPLES / rel).read_bytes().decode("utf-8")
