# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The GI reader against the GI format specification and its conformance examples (unit P02).

``test_gi_conformance_examples`` executes every entry of
docs/prep/gi-examples/manifest.json against the reader, as GI, against the
entry's job (built with P01's ``JobSpec.from_mapping``), with every gate
closed (docs/prep/gi-format.md section 13). The other tests pin the rules of
red-team PHYS-12 (the GI header against job.toml, ZSHIFT never applied) and the
strict readings the specification fixes: upper-case annotations at column 1,
';' comments only, version line 1, the 0.0005 agreement tolerance, the
byte-order mark, ``c=360`` as heading 0.
"""
from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from prep_gcode_support import (
    EXAMPLES,
    GI_HEADER,
    GI_SCHEMA,
    JOB,
    PREAMBLE,
    assert_clean,
    assert_refused,
    at,
    describe,
    example_text,
    gi_text,
    job,
    manifest,
    read_gi,
    refusals,
)

from goliath_prep.core.types import ClosedGateView
from goliath_prep.gcode import GIZShift, read_gcode
from goliath_prep.jobspec import JobSpec

D = Decimal
MOVE_TOLERANCE = D("0.0005")
SWEEP_TOLERANCE_DEG = 0.001


def _entry_id(entry: dict[str, Any]) -> str:
    return entry["file"]


@pytest.mark.parametrize("entry", manifest(), ids=_entry_id)
def test_gi_conformance_examples(entry: dict[str, Any]) -> None:
    """Every docs/prep/gi-examples/manifest.json entry, read as GI with every gate closed (gi-format 13)."""
    job_spec = JobSpec.from_mapping(entry["job"])
    result = read_gcode(example_text(entry["file"]), flavour="gi", job=job_spec, file=entry["file"])
    assert ClosedGateView.DISCLOSURE in result.disclosures
    refused = refusals(result)
    if entry["expect"] == "refused":
        assert refused, "%s must be refused; got:\n%s" % (entry["file"], describe(result))
        named = [f for f in at(result, entry["finding_id"], entry["line"]) if f.severity == "refuse"]
        assert named, "%s must be refused with %s on line %d; got:\n%s" % (
            entry["file"], entry["finding_id"], entry["line"], describe(result))
        return
    assert entry["expect"] == "ok"
    assert not refused, "%s must read without refusal; got:\n%s" % (entry["file"], describe(result))
    if "finding_id" in entry:
        warned = [f for f in at(result, entry["finding_id"], entry["line"]) if f.severity == "warn"]
        assert warned, "%s must warn %s on line %d; got:\n%s" % (
            entry["file"], entry["finding_id"], entry["line"], describe(result))
    assert result.gi_header is not None
    _assert_schema_shape(result.gi_header.to_json())
    for expected in entry.get("assert_moves", []):
        move = result.program_raw.move_at(expected["line"])
        assert move is not None, "%s: no move read from line %d" % (entry["file"], expected["line"])
        for got, want in zip(move.end, expected["end"]):
            assert got is not None and abs(got - D(repr(want))) <= MOVE_TOLERANCE, (move.end, expected)
        if "sweep_deg" in expected:
            assert move.sweep_deg is not None
            assert math.isclose(move.sweep_deg, expected["sweep_deg"], abs_tol=SWEEP_TOLERANCE_DEG), (
                move.sweep_deg, expected)


def _assert_schema_shape(model: dict[str, Any]) -> None:
    """The header model has exactly the properties gi.schema.json allows, and every required one."""
    schema = json.loads(GI_SCHEMA.read_text(encoding="utf-8"))
    defs = schema["$defs"]

    def check(instance: dict[str, Any], node: dict[str, Any], where: str) -> None:
        props = node["properties"]
        assert set(instance) <= set(props), "%s: unexpected %s" % (where, sorted(set(instance) - set(props)))
        missing = set(node.get("required", [])) - set(instance)
        assert not missing, "%s: missing %s" % (where, sorted(missing))
        for key, value in instance.items():
            sub = props[key]
            if "$ref" in sub:
                sub = defs[sub["$ref"].rsplit("/", 1)[-1]]
            if isinstance(value, dict):
                check(value, sub, "%s.%s" % (where, key))
            elif isinstance(value, list):
                item = sub["items"]
                item = defs[item["$ref"].rsplit("/", 1)[-1]] if "$ref" in item else item
                for i, element in enumerate(value):
                    check(element, item, "%s.%s[%d]" % (where, key, i))
            if "enum" in sub:
                assert value in sub["enum"], "%s.%s = %r" % (where, key, value)
            if "const" in sub:
                assert value == sub["const"] and type(value) is type(sub["const"]), "%s.%s" % (where, key)

    check(model, schema, "$")


def test_every_manifest_entry_was_executed() -> None:
    """The conformance test is parametrized over the whole manifest: none can silently go unexecuted."""
    entries = manifest()
    assert len(entries) >= 100
    assert {e["expect"] for e in entries} == {"ok", "refused"}


# ---------------------------------------------------------------------------
# Header against job.toml (PHYS-12)
# ---------------------------------------------------------------------------

CONFLICTS = [
    # (header line index in GI_HEADER, replaced by, job override, GI words, job.toml words)
    (3, ";GOLIATH STOCK x=2400.000 y=1220.000 t=18.000 origin=t1-corner zzero=top", {},
     ("GI STOCK x = 2400.000",), ("job.toml panel.W", "2440")),
    (3, ";GOLIATH STOCK x=2440.000 y=1250.000 t=18.000 origin=t1-corner zzero=top", {},
     ("GI STOCK y = 1250.000",), ("job.toml panel.H", "1220")),
    (3, ";GOLIATH STOCK x=2440.000 y=1220.000 t=18.000 origin=t1-corner zzero=top", {"panel": {"T": 17.6}},
     ("GI STOCK t = 18.000",), ("job.toml panel.T", "17.6")),
    (3, ";GOLIATH STOCK x=2440.000 y=1220.000 t=18.000 origin=t1-corner zzero=bottom", {},
     ("GI STOCK zzero = bottom",), ("job.toml z_zero", "top")),
    (3, ";GOLIATH STOCK x=2440.000 y=1220.000 t=18.000 origin=t1-corner zzero=top", {"placement": {"dx": 100.0}},
     ("GI STOCK origin = t1-corner",), ("job.toml placement", "dx = 100")),
    (4, ";GOLIATH TOWER2 axis=X", {}, ("GI TOWER2 axis = X",), ("job.toml panel.tower2", "Y")),
    (5, ";GOLIATH TOOL n=1 d=8.000 flute=22.000", {}, ("GI TOOL d = 8.000",), ("job.toml tool.d", "6")),
    (5, ";GOLIATH TOOL n=1 d=6.000 flute=32.000", {}, ("GI TOOL flute = 32.000",), ("job.toml tool.flute", "22")),
    (5, ";GOLIATH TOOL n=1 d=6.000 flute=22.000 stickout=45.000", {},
     ("GI TOOL stickout = 45.000",), ("job.toml tool.stickout", "40")),
    (5, ";GOLIATH TOOL n=1 d=6.000 flute=22.000 rpm=16000", {}, ("GI TOOL rpm = 16000",), ("job.toml tool.rpm",
                                                                                            "18000")),
    (5, ";GOLIATH TOOL n=1 d=6.000 flute=22.000 kind=ball", {"tool": {"kind": "flat"}},
     ("GI TOOL kind = ball",), ("job.toml tool.kind", "flat")),
]


@pytest.mark.parametrize("index,line,overrides,gi_words,job_words", CONFLICTS,
                         ids=[c[1].split()[1] + ":" + "/".join(c[3]) for c in CONFLICTS])
def test_gi_jobtoml_conflict_refused_naming_both(index: int, line: str, overrides: dict[str, Any],
                                                 gi_words: tuple[str, ...], job_words: tuple[str, ...]) -> None:
    """Red-team PHYS-12: a disagreement is refused at the GI header line naming both sources and both values."""
    header = list(GI_HEADER)
    header[index] = line
    result = read_gi(PREAMBLE, header=header, job_spec=job(**overrides))
    finding = assert_refused(result, "D-GI-CONFLICT", index + 1)
    for words in (*gi_words, *job_words):
        assert words in finding.message, finding.message
    sources = dict(finding.witness.sources)
    assert any(k.startswith("GI ") for k in sources) and any(k.startswith("job.toml") for k in sources), sources
    assert len(at(result, "D-GI-CONFLICT", index + 1)) == 1, describe(result)


def test_each_disagreeing_value_is_its_own_finding() -> None:
    header = list(GI_HEADER)
    header[3] = ";GOLIATH STOCK x=2400.000 y=1250.000 t=19.000 origin=t1-corner zzero=bottom"
    result = read_gi(PREAMBLE, header=header)
    assert len(at(result, "D-GI-CONFLICT", 4)) == 4, describe(result)


@pytest.mark.parametrize(
    "gi_t,job_t,agree",
    [("18.000", 18.0, True), ("18", 18.0, True), ("18.0004", 18.0, True), ("17.9996", 18.0, True),
     ("18.0005", 18.0, False), ("17.9995", 18.0, False), ("18.000", 17.6, False), ("17.6", 17.6, True)],
)
def test_agreement_tolerance_is_0_0005_as_written(gi_t: str, job_t: float, agree: bool) -> None:
    """gi-format 10: numbers agree when they differ by less than 0.0005, compared as decimals as written."""
    header = list(GI_HEADER)
    header[3] = ";GOLIATH STOCK x=2440.000 y=1220.000 t=%s origin=t1-corner zzero=top" % gi_t
    result = read_gi(PREAMBLE, header=header, job_spec=job(panel={"T": job_t}))
    assert bool(at(result, "D-GI-CONFLICT", 4)) is (not agree), describe(result)


def test_optional_tool_values_compared_only_when_both_declare_them() -> None:
    header = list(GI_HEADER)
    header[5] = ";GOLIATH TOOL n=1 d=6.000 flute=22.000 stickout=45.000 rpm=16000 kind=ball"
    result = read_gi(PREAMBLE, header=header, job_spec=job(tool={"stickout": None, "rpm": None}))
    assert_clean(result)
    bound = result.bound
    assert bound["tool_stickout"].value == 45.0 and bound["tool_stickout"].ref == "GI TOOL stickout (line 6)"
    assert bound["tool_kind"].value == "ball" and bound["tool_kind"].source == "OWNER_JOB"


def test_bound_values_name_their_source() -> None:
    """gi-format 10: when both agree the job.toml value is bound and its source says GI agreed."""
    result = read_gi(PREAMBLE)
    assert_clean(result)
    bound = result.bound
    assert bound["stock_thickness"].value == 18.0
    assert bound["stock_thickness"].ref == "job.toml panel.T = GI STOCK t (line 4)"
    assert bound["tool_diameter"].ref == "job.toml tool.d = GI TOOL d (line 6)"
    assert bound["tower2_axis"].value == "Y"
    assert bound["z_zero"].value == "top" and "GI STOCK zzero (line 4)" in bound["z_zero"].ref
    # The job's disclosures travel with the bound value.
    assert bound["placement_dx"].all_disclosures() == JOB.placement.dx.all_disclosures()
    # Without GI, the job's values are bound as they are.
    generic = read_gcode("G21\nG0 Z10.000\n", flavour="generic", job=JOB)
    assert generic.bound["stock_thickness"] is JOB.panel.T


def test_conflicting_values_are_not_bound() -> None:
    header = list(GI_HEADER)
    header[4] = ";GOLIATH TOWER2 axis=X"
    result = read_gi(PREAMBLE, header=header)
    assert "tower2_axis" not in result.bound


# ---------------------------------------------------------------------------
# ZSHIFT (PHYS-12)
# ---------------------------------------------------------------------------


def test_zshift_never_applied() -> None:
    """ZSHIFT is 'already applied by the post; informational': every Z is read as written."""
    result = read_gcode(example_text("valid/zshift-not-applied.gi"), flavour="gi", job=JOB)
    assert_clean(result)
    zshift = result.gi_header.zshift
    assert zshift.value == D("-19.050") and zshift.applied is False and zshift.line == 7
    assert result.gi_header.to_json()["zshift"] == {"value": -19.05, "applied": False, "line": 7}
    plunge = result.program_raw.move_at(13)
    assert plunge.end[2] == D("-6.000")
    # Every Z in the program is a Z written in the file.
    written = {D(word[1:]) for line in example_text("valid/zshift-not-applied.gi").splitlines()
               if not line.startswith(";") for word in line.split() if word.startswith("Z")}
    zs = {m.end[2] for m in result.program_raw.moves}
    assert zs <= written, (zs, written)
    # The same program without ZSHIFT reads to the same moves.
    text = example_text("valid/zshift-not-applied.gi").replace(";GOLIATH ZSHIFT -19.050\n", "; Note: none\n")
    without = read_gcode(text, flavour="gi", job=JOB)
    assert [m.end for m in without.program_raw.moves] == [m.end for m in result.program_raw.moves]
    assert any(n.kind == "zshift-recorded" and n.lines == (7,) for n in result.notes)


def test_zshift_model_cannot_claim_it_was_applied() -> None:
    with pytest.raises(ValueError, match="never applied"):
        GIZShift(value=D("-18"), line=7, applied=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Strict readings fixed by gi-format
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line,check_id",
    [
        (";goliath ZSHIFT -18.000", "D-GI-SYNTAX"),
        ("; GOLIATH ZSHIFT -18.000", "D-GI-SYNTAX"),
        ("  ;GOLIATH ZSHIFT -18.000", "D-GI-SYNTAX"),
        (";GOLIATH zshift -18.000", "D-GI-SYNTAX"),
        (";GOLIATHZSHIFT -18.000", "D-GI-SYNTAX"),
        (';GOLIATH FIXTURE note="open', "D-GI-SYNTAX"),
        (';GOLIATH FIXTURE note="a\\tb"', "D-GI-SYNTAX"),
        (";GOLIATH FIXTURE k=1 k=1", "D-GI-SYNTAX"),
        (";GOLIATH FIXTURE K=1", "D-GI-SYNTAX"),
        (";GOLIATH FIXTURE k = 1", "D-GI-SYNTAX"),
        (";GOLIATH-GI 1", "D-GI-VERSION"),
        (";GOLIATH ZSHIFT -18.000 -1.000", "D-GI-HEADER"),
        (";GOLIATH ZSHIFT", "D-GI-HEADER"),
        (';GOLIATH ZSHIFT "-18.000"', "D-GI-HEADER"),
        (";GOLIATH ZSHIFT 1e3", "D-GI-HEADER"),
    ],
)
def test_annotation_form_is_strict(line: str, check_id: str) -> None:
    """gi-format 4.1: ';GOLIATH' upper case in column 1, keyword upper case, keys lower case, strict quoting."""
    result = read_gi([], header=[*GI_HEADER, line])
    assert_refused(result, check_id, 7)


def test_unknown_keyword_and_key_are_warned_and_the_rest_is_read() -> None:
    header = list(GI_HEADER)
    header[5] = ';GOLIATH TOOL n=1 d=6.000 flute=22.000 coating="TiN"'
    result = read_gi(PREAMBLE, header=[*header, ";GOLIATH FIXTURE kind=vacuum"])
    assert_clean(result)
    assert [f.witness.src.line for f in result.findings if f.check_id == "N-GI-UNKNOWN-KEY"] == [6, 7]
    assert result.gi_header.tool.d == D("6.000")


def test_parenthesised_comments_are_refused() -> None:
    assert_refused(read_gi(["(a CAM comment)"]), "D-SYNTAX", 7, 1)


@pytest.mark.parametrize("version", ["1.0", "01", "2", "one", "1 extra"])
def test_version_line_is_exactly_gi_1(version: str) -> None:
    header = [";GOLIATH-GI " + version, *GI_HEADER[1:]]
    result = read_gi(PREAMBLE, header=header)
    assert_refused(result, "D-GI-VERSION", 1)
    # Nothing else in the file is interpreted.
    assert [f.check_id for f in refusals(result)] == ["D-GI-VERSION"]
    assert result.gi_header is None and not result.program_raw.items


def test_version_line_trailing_spaces_and_bom() -> None:
    result = read_gcode("﻿" + gi_text(PREAMBLE).replace(";GOLIATH-GI 1\n", ";GOLIATH-GI 1 \t\n", 1),
                        flavour="gi", job=JOB)
    assert_clean(result)
    assert any(n.kind == "bom" and n.lines == (1,) for n in result.notes)
    # One BOM only: a second one is the first character of line 1.
    result = read_gcode("﻿﻿" + gi_text(PREAMBLE), flavour="gi", job=JOB)
    assert_refused(result, "D-GI-VERSION", 1)


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_line_numbers_follow_the_dotnet_split(ending: str) -> None:
    text = ending.join([*GI_HEADER, *PREAMBLE, "G91"]) + ending
    result = read_gcode(text, flavour="gi", job=JOB)
    assert_refused(result, "D-UNSUPPORTED", 11, 1)
    # A final terminator starts no further line; a missing one changes nothing.
    assert result.program_raw.line_count == 11
    assert read_gcode(text[: -len(ending)], flavour="gi", job=JOB).program_raw.line_count == 11


def test_header_missing_line_is_reported_where_the_header_ends() -> None:
    header = [line for line in GI_HEADER if "TOWER2" not in line]
    result = read_gi([";GOLIATH OP id=a kind=other through=0 tool=1", "G0 Z10.000", ";GOLIATH OPEND id=a"],
                     header=header)
    finding = assert_refused(result, "D-GI-HEADER", 6)
    assert "TOWER2" in finding.message
    # A header that never ends: reported at the last line of the file.
    result = read_gcode("\n".join(header) + "\n", flavour="gi", job=JOB)
    assert_refused(result, "D-GI-HEADER", 5)


def test_header_values_are_never_defaulted() -> None:
    header = list(GI_HEADER)
    header[3] = ";GOLIATH STOCK x=2440.000 y=1220.000 origin=t1-corner zzero=top"
    result = read_gi(PREAMBLE, header=header)
    finding = assert_refused(result, "D-GI-HEADER", 4)
    assert "required key t " in finding.message, finding.message
    assert result.gi_header is None
    assert "stock_thickness" not in result.bound or result.bound["stock_thickness"].ref == "job.toml panel.T"


def test_header_model_round_trip_and_types() -> None:
    result = read_gcode(example_text("valid/three-ops-forced-headings.gi"), flavour="gi", job=JOB)
    assert_clean(result)
    model = result.gi_header.to_json()
    _assert_schema_shape(model)
    assert model["version"] == 1
    assert [op["through"] for op in model["ops"]] == [False, True, "auto"]
    assert [op.get("c") for op in model["ops"]] == [None, 90, 360]
    assert model["ops"][0]["label"] == "Tasche für Scharnier"
    assert model["tool"]["n"] == 1 and isinstance(model["tool"]["n"], int)


# ---------------------------------------------------------------------------
# What our posts write reads cleanly
# ---------------------------------------------------------------------------

POST_SAMPLES = sorted((EXAMPLES.parents[2] / "posts" / "freecad" / "samples").glob("*.gi"))


@pytest.mark.parametrize("path", POST_SAMPLES, ids=[p.name for p in POST_SAMPLES])
def test_freecad_post_samples_read_without_refusal(path: Path) -> None:
    """The FreeCAD post's samples (posts/freecad/samples) read cleanly against a job.toml stating the same panel."""
    text = path.read_bytes().decode("utf-8")
    header = read_gcode(text, flavour="gi", job=JOB).gi_header
    assert header is not None
    stated: dict[str, Any] = {
        "panel": {"W": float(header.stock.x), "H": float(header.stock.y), "T": float(header.stock.t),
                  "tower2": header.tower2.axis},
        "z_zero": header.stock.zzero,
        "tool": {"d": float(header.tool.d), "flute": float(header.tool.flute)},
    }
    result = read_gcode(text, flavour="gi", job=JobSpec.from_mapping(stated), file=path.name)
    assert_clean(result)
    assert result.program_raw.moves
    # The same file against callipers that read 0.4 mm less is refused, naming both.
    stated["panel"]["T"] = float(header.stock.t) - 0.4
    result = read_gcode(text, flavour="gi", job=JobSpec.from_mapping(stated), file=path.name)
    assert_refused(result, "D-GI-CONFLICT", header.stock.line)


def test_post_samples_exist() -> None:
    assert len(POST_SAMPLES) >= 3, POST_SAMPLES
