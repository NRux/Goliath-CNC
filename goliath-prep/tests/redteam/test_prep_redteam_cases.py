# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The red-team cases themselves are well formed (unit P20; these tests run now, without the pipeline).

Every case folder holds exactly the files of the case format (README.md);
expected.json names a check of docs/prep/checks.json with a witness of the
fields the suite understands; gates.json names every gate of safety-model 1.4;
measured.json holds raw INFO;25/27 lines and a valid park pose; job.toml and
machine.toml load with goliath-prep's own readers; the program reads as GI
(its header valid under docs/prep/gi.schema.json) or as the vendor's cleaned
dialect; a reader case carries exactly the defect it names, at its line, and
a geometric case carries none. The suite's GI reader is itself checked
against the conformance examples of docs/prep/gi-examples.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from goliath_prep.core import ids
from goliath_prep.core.types import Pose
from goliath_prep.jobspec import JobSpec
from goliath_prep.machine import MachineProfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PREP_DOCS = REPO / "docs" / "prep"


def _helper(name: str) -> Any:
    key = "goliath_prep_tests_redteam_" + name
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / (name + ".py"))
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


harness = _helper("prep_redteam_cases")
gi = _helper("prep_redteam_gi")
derive = _helper("prep_redteam_derive")
CASES = harness.load_cases()
IDS = [c.id for c in CASES]
CHECKS = {c["id"]: c for c in json.loads((PREP_DOCS / "checks.json").read_text(encoding="utf-8"))}
DESIGN_CASES = {"RT%d" % n for n in list(range(1, 16)) + list(range(19, 36))}
FOLDER = re.compile(r"RT(\d{2})([a-z]|-fact)?-[a-z0-9.-]+\Z")
CASE_FILES = {"case.gi", "case.nc", "job.toml", "expected.json", "measured.json", "gates.json", "machine.toml"}
EXPECTED_KEYS = {"schema", "case", "variant", "title", "verdict", "check_id", "witness", "also_refused",
                 "heading_table_rows", "co_occurring", "rationale", "defined_in", "review"}
WITNESS_KEYS = {"cut", "wheel", "near", "tol_mm", "line", "column", "rule", "gate", "unlock_rung", "fact",
                "source_values", "text", "any_of"}
ROTATION_MODELS = ("in-place-shortest", "in-place-numeric", "during-translation", "about-centroid")


def gate_names() -> list[str]:
    """The gates of safety-model 1.4, in the order of its table."""
    text = (PREP_DOCS / "safety-model.md").read_text(encoding="utf-8")
    table = text.split("| Gate | When on, it allows | Unlocked by |", 1)[1].split("\n\n", 1)[0]
    return re.findall(r"^\| `([a-z0-9_]+)` \|", table, flags=re.M)


GATES = gate_names()


def slug(heading: str) -> str:
    """The anchor a Markdown heading gets on the repository host (lower case, punctuation dropped)."""
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(doc: str) -> set[str]:
    text = (PREP_DOCS / doc).read_text(encoding="utf-8")
    return {slug(m) for m in re.findall(r"^#+ (.+)$", text, flags=re.M)}


# ---------------------------------------------------------------------------
# A small JSON Schema checker for the keywords gi.schema.json uses
# ---------------------------------------------------------------------------


def schema_errors(instance: Any, schema: dict[str, Any], root: dict[str, Any], where: str = "$") -> list[str]:
    if "$ref" in schema:
        node: Any = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        return schema_errors(instance, node, root, where)
    errors = []
    if "const" in schema and instance != schema["const"]:
        errors.append("%s: %r is not %r" % (where, instance, schema["const"]))
    if "enum" in schema and instance not in schema["enum"]:
        errors.append("%s: %r not in %r" % (where, instance, schema["enum"]))
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float)}
    if kind is not None and (not isinstance(instance, types[kind]) or isinstance(instance, bool)):
        return errors + ["%s: %r is not of type %s" % (where, instance, kind)]
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append("%s: missing %s" % (where, key))
        props = schema.get("properties", {})
        for key, value in instance.items():
            if key in props:
                errors += schema_errors(value, props[key], root, "%s.%s" % (where, key))
            elif schema.get("additionalProperties") is False:
                errors.append("%s: unexpected key %s" % (where, key))
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            errors += schema_errors(item, schema["items"], root, "%s[%d]" % (where, i))
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append("%s: %r < %r" % (where, instance, schema["minimum"]))
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append("%s: %r > %r" % (where, instance, schema["maximum"]))
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            errors.append("%s: %r <= %r" % (where, instance, schema["exclusiveMinimum"]))
        if "multipleOf" in schema and instance % schema["multipleOf"]:
            errors.append("%s: %r not a multiple of %r" % (where, instance, schema["multipleOf"]))
    if isinstance(instance, str) and "pattern" in schema and not re.search(schema["pattern"], instance):
        errors.append("%s: %r does not match %s" % (where, instance, schema["pattern"]))
    return errors


def test_schema_checker_rejects_what_it_should() -> None:
    root = json.loads((PREP_DOCS / "gi.schema.json").read_text(encoding="utf-8"))
    good = {"version": 1, "source": {"cam": "a", "post": "b", "line": 2}, "units": {"value": "mm", "line": 3},
            "stock": {"x": 1.0, "y": 1.0, "t": 1.0, "origin": "t1-corner", "zzero": "top", "line": 4},
            "tower2": {"axis": "Y", "line": 5}, "tool": {"n": 1, "d": 6.0, "flute": 22.0, "line": 6}, "ops": []}
    assert schema_errors(good, root, root) == []
    for broken in ({**good, "version": 2}, {**good, "extra": 1}, {**good, "tower2": {"axis": "Z", "line": 5}},
                   {**good, "ops": [{"id": "a", "kind": "profile", "through": "auto", "tool": 1, "line": 9,
                                     "end_line": 10, "c": 15}]}):
        assert schema_errors(broken, root, root), broken


# ---------------------------------------------------------------------------
# The inventory
# ---------------------------------------------------------------------------


def test_every_design_case_is_covered_and_none_of_another_unit() -> None:
    covered = {c.design_id for c in CASES}
    assert covered == DESIGN_CASES, "missing %s, extra %s" % (sorted(DESIGN_CASES - covered),
                                                             sorted(covered - DESIGN_CASES))
    # RT16 and RT17 (series) are unit P19's, RT18 (certificate replay) the runner's (unit R08).
    assert not covered & {"RT16", "RT17", "RT18"}


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_folder_name_matches_the_case_id(case: Any) -> None:
    m = FOLDER.match(case.id)
    assert m, case.id
    assert case.design_id == "RT%d" % int(m.group(1))
    variant = m.group(2)
    assert case.expected["variant"] == (None if variant is None else variant.lstrip("-"))


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_case_holds_exactly_the_files_of_the_format(case: Any) -> None:
    names = {p.name for p in case.path.iterdir()}
    assert names <= CASE_FILES, "unexpected files %s (series cases with prior.json are unit P19's)" % (
        sorted(names - CASE_FILES))
    assert {"job.toml", "expected.json"} <= names
    assert len(names & {"case.gi", "case.nc"}) == 1
    assert not case.has_prior


# ---------------------------------------------------------------------------
# expected.json
# ---------------------------------------------------------------------------


def _witness_spec_ok(spec: dict[str, Any], check_id: str) -> None:
    assert set(spec) <= WITNESS_KEYS, set(spec) - WITNESS_KEYS
    fields = set(CHECKS[check_id]["witness_fields"])
    needs = {"cut": "cut", "wheel": "wheel", "near": "point", "line": "src", "column": "column", "rule": "rule",
             "gate": "gate", "unlock_rung": "unlock_rung", "fact": "fact", "source_values": "sources"}
    for key, field in needs.items():
        if key in spec:
            assert field in fields, "%s: witness key %s needs the field %s, not in checks.json %s" % (
                check_id, key, field, sorted(fields))
    if "near" in spec:
        assert len(spec["near"]) == 2 and spec["tol_mm"] > 0
    if "wheel" in spec:
        assert spec["wheel"] in (1, 2, 3)
    if "cut" in spec:
        assert isinstance(spec["cut"], int) and spec["cut"] >= harness.CUT_INDEX_BASE
    if "rule" in spec:
        assert spec["rule"] in CHECKS
    for alternative in spec.get("any_of", []):
        _witness_spec_ok(alternative, check_id)


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_expected_json_is_well_formed(case: Any) -> None:
    e = case.expected
    assert set(e) <= EXPECTED_KEYS, set(e) - EXPECTED_KEYS
    assert e["schema"] == "goliath-prep-redteam-expected/1"
    assert e["verdict"] in ("refused", "check-lifted")
    assert e["check_id"] in CHECKS, "%s is not in docs/prep/checks.json" % e["check_id"]
    assert e["check_id"] in ids.SEVERITY, "%s is not in goliath_prep.core.ids" % e["check_id"]
    if e["verdict"] == "refused":
        assert CHECKS[e["check_id"]]["severity"] == "refuse"
    _witness_spec_ok(e["witness"], e["check_id"])
    for extra in e.get("also_refused", []):
        assert set(extra) == {"check_id", "witness"}
        assert CHECKS[extra["check_id"]]["severity"] == "refuse"
        _witness_spec_ok(extra["witness"], extra["check_id"])
    for other in e["co_occurring"]:
        assert other in CHECKS, other
    assert e["title"].strip() and len(e["rationale"]) > 40
    assert e["defined_in"], "a case cites the normative text it rests on"
    for ref in e["defined_in"]:
        doc, _, anchor = ref.partition("#")
        assert (PREP_DOCS / doc).is_file(), ref
        assert anchor in anchors(doc), "%s: no heading with that anchor in docs/prep/%s" % (ref, doc)
    for review in e["review"]:
        assert re.fullmatch(r"(PHYS|FW|C)-\d{2}|E-[ABC]\d+", review), review
    if "heading_table_rows" in e:
        assert e["heading_table_rows"] == 36


def test_no_case_cites_the_design_file_or_a_scratch_path() -> None:
    for case in CASES:
        text = (case.path / "expected.json").read_text(encoding="utf-8")
        for banned in ("scratchpad", "/tmp", "design-final", "brief §"):
            assert banned not in text, (case.id, banned)


# ---------------------------------------------------------------------------
# gates.json and measured.json
# ---------------------------------------------------------------------------


def test_the_gate_table_was_read() -> None:
    assert len(GATES) == 21 and GATES[0] == "jobs_allowed" and "wire_attachment_measured" in GATES


@pytest.mark.parametrize("case", [c for c in CASES if c.gates is not None], ids=[c.id for c in CASES if c.gates])
def test_gates_json_names_every_gate(case: Any) -> None:
    g = case.gates
    assert set(g) == {"schema", "description", "gates"} and g["schema"] == "goliath-prep-redteam-gates/1"
    assert list(g["gates"]) == GATES, "every gate of safety-model 1.4, in its order, none implied"
    for name, entry in g["gates"].items():
        assert entry["allowed"] in (True, False, None), name
        assert set(entry) <= {"allowed", "value"}, name
        if "value" in entry:
            assert entry["allowed"] is True, "%s: only an open gate carries a value" % name
    gates = g["gates"]
    if gates["rotation_model"]["allowed"] is True:
        assert gates["rotation_model"]["value"] in ROTATION_MODELS
    if gates["tabs_hold"]["allowed"] is True:
        assert 0 < gates["tabs_hold"]["value"] < float(case.job_document()["panel"]["T"]) / 2
    if gates["kerf_crossing"]["allowed"] is True:
        value = gates["kerf_crossing"]["value"]
        assert set(value) == {"kerf_width_max", "kerf_ride_max", "recess_ignore_max"}
        assert all(isinstance(v, float) and v >= 0 for v in value.values())
    if gates["post_job_motion_known"]["allowed"] is True:
        assert gates["post_job_motion_known"]["value"] == "none"
    start = gates["start_pose_known"]
    if start["allowed"] is True:
        assert case.measured is not None and start["value"] == case.measured["park_pose"], \
            "the start-pose fact and measured.json's park pose are one record"
    else:
        assert case.measured is None or case.measured["park_pose"] is None


@pytest.mark.parametrize("case", [c for c in CASES if c.measured is not None],
                         ids=[c.id for c in CASES if c.measured is not None])
def test_measured_json_is_raw_lines_and_a_pose(case: Any) -> None:
    m = case.measured
    assert set(m) == {"schema", "info25", "info27", "park_pose"} and m["schema"] == "goliath-prep-redteam-measured/1"
    for line in m["info25"]:  # 01.02.00: INFO;25;%.4f;%.4f (spec 02, spec 09)
        assert re.fullmatch(r"INFO;25;-?\d+\.\d{4};-?\d+\.\d{4}", line), line
    if m["info25"]:
        assert len(m["info25"]) >= 3
        assert re.fullmatch(r"INFO;27;\d+\.\d{4}", m["info27"]), m["info27"]
    else:
        assert m["info27"] is None, "a desk case has no detection lines"
    if m["park_pose"] is not None:
        pose = Pose.from_json(m["park_pose"])
        assert pose.c is not None and "park_pose" in pose.source
    assert case.park_pose() == (None if m["park_pose"] is None else Pose.from_json(m["park_pose"]))


# ---------------------------------------------------------------------------
# job.toml, machine.toml and the program
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_job_toml_loads_with_goliath_preps_reader(case: Any) -> None:
    job = JobSpec.load(case.job_path)
    doc = case.job_document()
    assert job.panel.W.value == pytest.approx(float(doc["panel"]["W"]))
    assert len(job.obstacles) == len(doc.get("obstacles", [])) and \
        len(job.prior_removal) == len(doc.get("prior_removal", []))


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_machine_toml_loads_and_a_measured_one_is_stamped(case: Any) -> None:
    profile = MachineProfile.load(case.machine_path)
    assert profile.serial == "TEST-SERIAL-0001"
    if case.machine_path.parent == case.path:
        assert profile.origin == "exported", "a measured test profile must verify its export stamp"


@pytest.mark.parametrize("case", [c for c in CASES if not c.is_dialect], ids=[c.id for c in CASES if not c.is_dialect])
def test_gi_program_reads_as_the_case_says(case: Any) -> None:
    raw = case.program.read_bytes()
    text = raw.decode("utf-8")
    assert b"\r" not in raw and text.endswith("\n")
    prog = gi.read_gi(text, job=derive.reader_job(case.job_document()))
    root = json.loads((PREP_DOCS / "gi.schema.json").read_text(encoding="utf-8"))
    e = case.expected
    if derive.kind(case) == "reader":
        assert prog.refusals, "a reader case must be refused by the reader"
        first = prog.refusals[0]
        assert (first.id, first.line) == (e["check_id"], e["witness"]["line"]), prog.refusals
        if "column" in e["witness"]:
            assert first.column == e["witness"]["column"]
        for extra in e.get("also_refused", []):
            assert any((f.id, f.line) == (extra["check_id"], extra["witness"]["line"]) for f in prog.refusals)
        reported = {(f.id, f.line) for f in prog.refusals}
        wanted = {(e["check_id"], e["witness"]["line"])} | {(x["check_id"], x["witness"]["line"])
                                                          for x in e.get("also_refused", [])}
        assert reported == wanted, "the file must carry exactly the defects the case names: %s" % prog.refusals
    else:
        assert not prog.findings, "a geometric case must read cleanly: %s" % prog.findings
    if prog.header is not None:
        assert schema_errors(prog.header, root, root) == []
    assert text.rstrip("\n").endswith("; Licence: SPDX-License-Identifier: GPL-3.0-or-later")


@pytest.mark.parametrize("case", [c for c in CASES if c.is_dialect], ids=[c.id for c in CASES if c.is_dialect])
def test_dialect_program_is_cleaned_and_carries_one_defect(case: Any) -> None:
    prog = gi.read_dialect(case.program.read_bytes())
    assert not prog.problems, "a dialect case is in the vendor's cleaned form (spec 05): %s" % prog.problems
    found = derive.dialect_findings(case)
    assert found, "the dialect case carries no defect"
    first = min(found, key=lambda f: f[1])
    assert first == (case.expected["check_id"], case.expected["witness"]["line"]), found
    assert {f[0] for f in found} == {case.expected["check_id"]}, found


def test_reader_agrees_with_the_conformance_examples() -> None:
    """The suite's own reader passes docs/prep/gi-examples, so its verdicts on the cases can be trusted."""
    root = PREP_DOCS / "gi-examples"
    for entry in json.loads((root / "manifest.json").read_text(encoding="utf-8")):
        text = (root / entry["file"]).read_bytes().decode("utf-8")
        prog = gi.read_gi(text, job=entry["job"])
        if entry["expect"] == "ok":
            assert not prog.refusals, (entry["file"], prog.refusals)
            if "finding_id" in entry:
                assert any((f.id, f.line) == (entry["finding_id"], entry["line"]) for f in prog.findings)
        else:
            assert any((f.id, f.line) == (entry["finding_id"], entry["line"]) for f in prog.refusals), \
                (entry["file"], prog.refusals)


def test_case_programs_are_kept_byte_exact() -> None:
    attributes = (HERE / "cases" / ".gitattributes").read_text(encoding="utf-8")
    assert "*.gi -text" in attributes and "*.nc -text" in attributes
    for case in CASES:
        if case.is_dialect:
            data = case.program.read_bytes()
            assert data.endswith(b"\r\n\r\n") and b"\n" not in data.replace(b"\r\n", b"")


def test_readme_lists_every_case_and_its_links_resolve() -> None:
    text = (HERE / "README.md").read_text(encoding="utf-8")
    listed = re.findall(r"^\| \[`([^`]+)`\]\(cases/\1/\) \|", text, flags=re.M)
    assert sorted(listed) == sorted(IDS) and len(listed) == len(set(listed))
    for target in re.findall(r"\]\(([^)#\s]+)\)", text):
        assert (HERE / target).exists(), target


def test_no_case_holds_a_real_identifier() -> None:
    for case in CASES:
        for path in case.path.iterdir():
            text = path.read_bytes().decode("utf-8", errors="replace")
            for serial in re.findall(r'serial\s*=\s*"([^"]*)"', text):
                assert serial.startswith("TEST-SERIAL-"), (path, serial)
            assert not re.search(r"(?i)ssid|passw|wifi|wi-fi", text), path
