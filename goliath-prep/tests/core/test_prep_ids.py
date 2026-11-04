# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""goliath_prep.core.ids is generated from docs/prep/checks.json and stays equal to it (unit F04)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[3]
IDS_PY = REPO / "goliath-prep" / "src" / "goliath_prep" / "core" / "ids.py"
CHECKS_JSON = REPO / "docs" / "prep" / "checks.json"

# The GI reader's ids (docs/prep/gi-format.md section 12), written out here so a
# change to that table is a visible change to this test too.
GI_READER = (
    "D-GI-VERSION",
    "D-GI-SYNTAX",
    "D-GI-HEADER",
    "D-GI-OP",
    "D-GI-CONFLICT",
    "N-GI-UNKNOWN-KEY",
    "D-SYNTAX",
    "D-UNSUPPORTED",
    "D-UNITS",
    "D-RANGE",
    "D-ARC",
    "D-AFTER-END",
    "D-MARKER",
    "D-SINGLETOOL",
    "G-GATE",
)


def test_ids_generated_from_checks_json(codegen: ModuleType) -> None:
    # The committed module is exactly what the generator writes from today's
    # documents (read as text, so a checkout with CRLF line endings compares equal).
    expected = codegen.generate()
    actual = IDS_PY.read_text(encoding="utf-8")
    assert actual == expected, (
        "goliath_prep/core/ids.py is stale against docs/prep/checks.json or gi-format.md section 12; "
        "regenerate it from the repository root: python goliath-prep/tests/core/prep_ids_codegen.py"
    )

    from goliath_prep.core import ids

    checks = json.loads(CHECKS_JSON.read_text(encoding="utf-8"))
    assert ids.ALL == tuple(entry["id"] for entry in checks)
    for entry in checks:
        check_id = entry["id"]
        assert getattr(ids, check_id.replace("-", "_")) == check_id
        assert ids.SEVERITY[check_id] == entry["severity"]
        assert ids.APPLIES_TO[check_id] == entry["applies_to"]
        assert ids.WITNESS_FIELDS[check_id] == tuple(entry["witness_fields"])
        assert ids.SLACK_UNIT[check_id] == (None if entry["slack"] is None else entry["slack"]["unit"])
        assert ids.DEFINED_IN[check_id] == entry["defined_in"]
    for mapping in (ids.SEVERITY, ids.APPLIES_TO, ids.WITNESS_FIELDS, ids.SLACK_UNIT, ids.DEFINED_IN):
        assert tuple(mapping) == ids.ALL


def test_catalogue_hash_is_the_canonical_json_of_checks_json() -> None:
    from goliath_prep.core import ids
    from goliath_prep.core.canon import canonical_json

    checks = json.loads(CHECKS_JSON.read_text(encoding="utf-8"))
    assert ids.CATALOGUE_SHA256 == hashlib.sha256(canonical_json(checks)).hexdigest()


def test_gi_reader_ids_are_catalogue_ids() -> None:
    from goliath_prep.core import ids

    assert ids.GI_READER_IDS == GI_READER
    assert set(ids.GI_READER_IDS) <= set(ids.ALL)
    for check_id in ids.GI_READER_IDS:
        expected = "warn" if check_id == "N-GI-UNKNOWN-KEY" else "refuse"
        assert ids.SEVERITY[check_id] == expected, check_id


def test_id_tables_are_read_only() -> None:
    from goliath_prep.core import ids

    with pytest.raises(TypeError):
        ids.SEVERITY["W-SURF"] = "warn"  # type: ignore[index]
    with pytest.raises(TypeError):
        ids.WITNESS_FIELDS["W-SURF"] = ()  # type: ignore[index]


def test_codegen_check_mode_agrees(codegen: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert codegen.main(["--check"]) == 0
    assert "up to date" in capsys.readouterr().out


def _catalogue(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "checks.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _entry(**overrides: object) -> dict:
    entry = {
        "id": "W-AREA",
        "family": "W",
        "severity": "refuse",
        "applies_to": "piece",
        "condition": "a wheel leaves the work area",
        "slack": {"unit": "mm", "definition": "distance"},
        "witness_fields": ["piece", "hazard"],
        "defined_in": "safety-model.md#x",
    }
    entry.update(overrides)
    return entry


@pytest.mark.parametrize(
    "entries, message",
    [
        ([_entry(id="R-KERF", family="R")], "not of the form"),
        ([_entry(id="W-area")], "not of the form"),
        ([_entry(), _entry()], "repeated"),
        ([_entry(family="B")], "disagrees with id"),
        ([_entry(severity="error")], "is not one of"),
        ([_entry(overridable=False)], "needs this generator updated"),
        ([{k: v for k, v in _entry().items() if k != "slack"}], "expected exactly"),
        ([_entry(slack={"unit": "mm"})], "slack of W-AREA"),
        ([_entry(witness_fields=[])], "witness_fields"),
        ([_entry(witness_fields=["piece", "piece"])], "witness_fields"),
        ([_entry(applies_to="")], "applies_to"),
        ([], "non-empty JSON list"),
    ],
)
def test_codegen_refuses_a_malformed_catalogue(codegen: ModuleType, tmp_path: Path, entries: list, message: str) -> None:
    with pytest.raises(codegen.CatalogueError, match=message):
        codegen.load_checks(_catalogue(tmp_path, entries))


@pytest.mark.parametrize(
    "table, message",
    [
        ("| `D-NOPE` | refuse | x | y |\n", "not in checks.json"),
        ("| `W-AREA` | warn | x | y |\n", "severity warn, checks.json refuse"),
        ("| `W-AREA` | refuse | x | y |\n| `W-AREA` | refuse | x | y |\n", "twice"),
        ("no table here\n", "no finding table"),
    ],
)
def test_codegen_cross_checks_gi_format_section_12(
    codegen: ModuleType, tmp_path: Path, table: str, message: str
) -> None:
    gi = tmp_path / "gi-format.md"
    gi.write_text("# GI\n\n## 12. Findings\n\n| Id | Severity | a | b |\n|---|---|---|---|\n" + table
                  + "\n## 13. Next\n| `D-LATER` | refuse | x | y |\n", encoding="utf-8")
    with pytest.raises(codegen.CatalogueError, match=message):
        codegen.load_gi_reader_ids([_entry()], gi)


def test_codegen_reads_only_section_12(codegen: ModuleType, tmp_path: Path) -> None:
    gi = tmp_path / "gi-format.md"
    gi.write_text("## 11. Before\n| `W-TOWER` | refuse | x | y |\n## 12. Findings\n| `W-AREA` | refuse | x | y |\n"
                  "## 13. After\n| `W-TOWER` | refuse | x | y |\n", encoding="utf-8")
    assert codegen.load_gi_reader_ids([_entry(), _entry(id="W-TOWER")], gi) == ["W-AREA"]
