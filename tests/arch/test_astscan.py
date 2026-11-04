# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tests/arch/astscan.py, the AST helpers of the architecture tests.

The expected answers come from marker comments in the fixture package
(tests/arch/fixtures/astscan_fixture/, described in its __init__.py), so a
fixture edit cannot leave a stale line number in this file.

Python 3.9 compatible: the repository-level suite runs on 3.9.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

import astscan
from astscan import AstScanError, Location, find_calls, find_references, find_string_literals

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_PKG = FIXTURES / "astscan_fixture"

QUERIES = {
    "upload": "goliath_link.client.GoliathClient.upload_gcode",
    "run": "subprocess.run",
    "helper": "astscan_fixture.helpers.run_thing",
    "system": "os.system",
}
COMMAND_PATTERN = r"EFLASH ERASE"

_MARK = re.compile(r"#\s*MARK:\s*(\w+)\s+(exact|name)\b")
_REF = re.compile(r"#\s*REF:\s*(\w+)\s+(\w+)\s+(exact|name)\b")
_LIT = re.compile(r"#\s*LIT:\s*([a-z]+(?:,[a-z]+)*)")


def _fixture_lines() -> List[Tuple[str, int, str]]:
    """(module, line number, text) of every fixture line."""
    out = []
    for path in sorted(FIXTURE_PKG.rglob("*.py")):
        rel = path.relative_to(FIXTURES).with_suffix("").parts
        module = ".".join(p for p in rel if p != "__init__")
        for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            out.append((module, number, text))
    return out


def _expected_calls(key: str) -> Set[Tuple[str, int, bool]]:
    found = set()
    for module, number, text in _fixture_lines():
        for match in _MARK.finditer(text):
            if match.group(1) == key:
                found.add((module, number, match.group(2) == "exact"))
    assert found, "the fixture has no MARK for %r: the test would prove nothing" % key
    return found


def _expected_refs(key: str) -> Set[Tuple[str, int, str, bool]]:
    found = {(m, n, "call", exact) for m, n, exact in _expected_calls(key)}
    for module, number, text in _fixture_lines():
        for match in _REF.finditer(text):
            if match.group(1) == key:
                found.add((module, number, match.group(2), match.group(3) == "exact"))
    return found


def _expected_literals() -> Dict[Tuple[str, int], List[str]]:
    found: Dict[Tuple[str, int], List[str]] = {}
    for module, number, text in _fixture_lines():
        match = _LIT.search(text)
        if match:
            found[(module, number)] = sorted(match.group(1).split(","))
    assert found
    return found


def _calls(locations: List[Location]) -> Set[Tuple[str, int, bool]]:
    return {(loc.module, loc.line, loc.exact) for loc in locations}


# --------------------------------------------------------------------------
# find_calls
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(QUERIES))
def test_astscan_finds_call_in_fixture_module(key: str) -> None:
    hits = find_calls(str(FIXTURE_PKG), QUERIES[key])
    assert all(loc.kind == "call" for loc in hits)
    assert _calls(hits) == _expected_calls(key)
    # One Location per call site: no duplicates.
    assert len(hits) == len(_calls(hits))


def test_find_calls_by_import_name_gives_the_same_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(FIXTURES))
    for key, query in QUERIES.items():
        assert find_calls("astscan_fixture", query) == find_calls(str(FIXTURE_PKG), query), key
    # Locating a top-level package by name reads its files without importing it.
    assert "astscan_fixture" not in sys.modules


def test_find_calls_reports_paths_modules_and_resolved_names() -> None:
    hits = find_calls(str(FIXTURE_PKG), QUERIES["run"])
    by_exact = {loc.exact: loc for loc in hits}
    exact, other = by_exact[True], by_exact[False]
    assert exact.text == "subprocess.run"  # `sp.run` after `import subprocess as sp`
    assert other.text == "asyncio.run"
    assert Path(exact.path) == FIXTURE_PKG / "calls.py"
    assert exact.module == "astscan_fixture.calls"
    assert exact.col >= 0 and exact.line > 0
    assert str(exact).startswith("%s:%d:%d: call " % (exact.path, exact.line, exact.col + 1))
    assert "(not exact)" in str(other)


def test_relative_imports_resolve_against_the_package() -> None:
    hits = find_calls(str(FIXTURE_PKG), QUERIES["helper"])
    texts = {(loc.module, loc.text) for loc in hits if loc.exact}
    assert ("astscan_fixture.calls", "astscan_fixture.helpers.run_thing") in texts  # `rt()`
    assert ("astscan_fixture.subpkg", "astscan_fixture.helpers.run_thing") in texts  # in __init__.py
    assert ("astscan_fixture.subpkg.deep", "astscan_fixture.helpers.run_thing") in texts


def test_bare_method_name_matches_every_receiver_exactly() -> None:
    hits = find_calls(str(FIXTURE_PKG), "upload_gcode")
    assert {(m, n) for m, n, _ in _calls(hits)} == {(m, n) for m, n, _ in _expected_calls("upload")}
    assert all(loc.exact for loc in hits)


def test_find_calls_does_not_match_longer_or_shorter_names() -> None:
    texts = {loc.text for loc in find_calls(str(FIXTURE_PKG), "upload_gcode")}
    assert not any(t.endswith(("upload_gcode_later", ".upload")) for t in texts)


def test_scanning_a_single_file() -> None:
    hits = find_calls(str(FIXTURE_PKG / "subpkg" / "deep.py"), QUERIES["helper"])
    assert {loc.module for loc in hits} == {"astscan_fixture.subpkg.deep"}
    assert all(loc.exact for loc in hits)
    assert len(hits) == 3


# --------------------------------------------------------------------------
# find_references
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["upload", "helper"])
def test_find_references_adds_bare_references_stores_and_imports(key: str) -> None:
    hits = find_references(str(FIXTURE_PKG), QUERIES[key])
    got = {(loc.module, loc.line, loc.kind, loc.exact) for loc in hits}
    assert got == _expected_refs(key)
    calls = [loc for loc in hits if loc.kind == "call"]
    assert calls == find_calls(str(FIXTURE_PKG), QUERIES[key])


# --------------------------------------------------------------------------
# find_string_literals
# --------------------------------------------------------------------------


def test_find_string_literals_sees_every_literal_shape() -> None:
    hits = find_string_literals(str(FIXTURE_PKG), COMMAND_PATTERN)
    got: Dict[Tuple[str, int], List[str]] = {}
    for loc in hits:
        got.setdefault((loc.module, loc.line), []).append(loc.kind)
    assert {k: sorted(v) for k, v in got.items()} == _expected_literals()
    assert all(loc.exact for loc in hits)


def test_find_string_literals_values() -> None:
    hits = find_string_literals(str(FIXTURE_PKG / "literals.py"), COMMAND_PATTERN)
    values = {(loc.kind, loc.text) for loc in hits}
    assert ("bytes", "EFLASH ERASE\n") in values  # bytes are decoded as latin-1
    assert ("concat", "SEND EFLASH ERASE;") in values  # the whole chain, folded once
    assert ("fstring", "EFLASH ERASE {...}") in values
    placeholders = find_string_literals(str(FIXTURE_PKG / "literals.py"), r"^EFLASH \{\.\.\.\}$")
    assert [loc.kind for loc in placeholders] == ["fstring"]


def test_find_string_literals_finds_the_dynamic_access_find_calls_misses() -> None:
    hits = find_string_literals(str(FIXTURE_PKG), r"^upload_gcode$")
    source = (FIXTURE_PKG / "calls.py").read_text(encoding="utf-8").splitlines()
    expected = {n for n, line in enumerate(source, start=1) if '"upload_gcode"' in line}
    assert len(expected) == 2
    assert {(loc.module, loc.line) for loc in hits} == {("astscan_fixture.calls", n) for n in expected}


# --------------------------------------------------------------------------
# Errors are never an empty answer
# --------------------------------------------------------------------------


def test_unknown_package_name_raises() -> None:
    with pytest.raises(AstScanError, match="not importable"):
        find_calls("no_such_package_for_astscan_tests", "x")


def test_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(AstScanError, match="does not exist"):
        find_calls(str(tmp_path / "missing"), "x")


def test_folder_without_python_files_raises(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("nothing here\n", encoding="utf-8")
    with pytest.raises(AstScanError, match="no Python files"):
        find_string_literals(str(tmp_path), "x")


def test_module_without_source_raises() -> None:
    with pytest.raises(AstScanError, match="no Python source"):
        find_calls("sys", "exit")


def test_unparsable_file_raises_naming_it(tmp_path: Path) -> None:
    pkg = tmp_path / "broken_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "bad.py").write_text("def f(:\n", encoding="utf-8")
    with pytest.raises(AstScanError, match=r"bad\.py"):
        find_calls(str(pkg), "f")


def test_invalid_queries_raise() -> None:
    with pytest.raises(ValueError):
        find_calls(str(FIXTURE_PKG), "upload-gcode")
    with pytest.raises(ValueError):
        find_calls(str(FIXTURE_PKG), "")
    with pytest.raises(re.error):
        find_string_literals(str(FIXTURE_PKG), "(unclosed")
    with pytest.raises(AstScanError):
        find_calls("", "x")


def test_source_encoding_declaration_is_honoured(tmp_path: Path) -> None:
    pkg = tmp_path / "latin_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_bytes(b"# -*- coding: latin-1 -*-\nNAME = 'caf\xe9'\n")
    hits = find_string_literals(str(pkg), "café")
    assert [(loc.module, loc.text) for loc in hits] == [("latin_pkg", "café")]


def test_package_files_lists_every_module() -> None:
    files = astscan.package_files(str(FIXTURE_PKG))
    assert [path for path, _ in files] == sorted(path for path, _ in files)
    modules = [module for _, module in files]
    assert len(set(modules)) == len(modules)
    assert set(modules) == {
        "astscan_fixture",
        "astscan_fixture.calls",
        "astscan_fixture.helpers",
        "astscan_fixture.literals",
        "astscan_fixture.subpkg",
        "astscan_fixture.subpkg.deep",
    }
