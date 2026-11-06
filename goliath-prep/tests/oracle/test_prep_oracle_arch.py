# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Architecture of the independent oracle (unit P21; red-team C-06; the project design, sections 10.4 and 10.7).

The oracle is worth something only while it shares nothing with the checker
it cross-checks:

* the oracle itself (``prep_oracle.py``) imports nothing of goliath-prep at
  all: not the checker units (``checks``, ``material``, ``links``,
  ``heading``, ``heading_vendor``, ``geom``, ``api``), not P22's ``context``
  (``build_context``, ``CheckContext``), not the parameter readers
  (``defaults``, ``jobspec``, ``machine``) and not shapely. It reads job.toml
  and machine.toml with ``tomllib`` and docs/prep/params.json with ``json``,
  and works on the program by its attributes alone, so a bug in building the
  checker's context cannot be shared by checker and oracle;
* the front end (``prep_oracle_front.py``) turns a file into a program with
  goliath-prep's input stage only (the strict reader P02 and the normaliser
  P03, which every consumer shares by design) and imports no checker unit and
  no context either.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent
ORACLE = HERE / "prep_oracle.py"
FRONT = HERE / "prep_oracle_front.py"

ORACLE_IMPORTS = frozenset({"__future__", "dataclasses", "decimal", "json", "math", "pathlib", "tomllib", "typing",
                            "numpy"})
"""Every module the oracle may import: the standard library's readers and numpy, nothing of goliath-prep."""

FORBIDDEN_MODULES = ("goliath_prep.context", "goliath_prep.checks", "goliath_prep.material", "goliath_prep.links",
                     "goliath_prep.heading", "goliath_prep.heading_vendor", "goliath_prep.geom", "goliath_prep.api",
                     "goliath_prep.certify", "goliath_prep.pieces", "shapely")
FORBIDDEN_NAMES = ("build_context", "CheckContext", "context_geometry", "emit_profile_for", "rotation_model_for",
                   "safe_zone_applies", "split_rotation_allowed", "SPEC_PARAMS", "JobSpec", "MachineProfile")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "%s:%d: a relative import" % (path, node.lineno)
            out.add(node.module or "")
    return out


def _exact(locations: list) -> list:
    return [loc for loc in locations if loc.exact]


def test_oracle_imports_neither_context_nor_checker(astscan: ModuleType) -> None:
    imported = _imports(ORACLE)
    # Positive controls: the scan reads the file and finds what the oracle does use.
    assert {"numpy", "tomllib", "json"} <= imported, imported
    assert astscan.find_calls(str(ORACLE), "tomllib.load")
    assert astscan.find_calls(str(ORACLE), "json.loads")
    assert imported <= ORACLE_IMPORTS, "the oracle imports %s" % sorted(imported - ORACLE_IMPORTS)
    assert not any(name == "goliath_prep" or name.startswith("goliath_prep.") for name in imported)
    for name in FORBIDDEN_MODULES + FORBIDDEN_NAMES:
        assert _exact(astscan.find_references(str(ORACLE), name)) == [], name
    # No dynamic import of a goliath-prep module or of shapely by name.
    assert astscan.find_string_literals(str(ORACLE), r"^(goliath_prep|shapely)(\.|$)") == []
    assert astscan.find_calls(str(ORACLE), "importlib.import_module") == []
    assert astscan.find_calls(str(ORACLE), "__import__") == []


def _catalogue_reads(path: Path) -> set[str]:
    """The parameter names the oracle reads from params.json: the literal first argument of every ``_cat`` call."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_cat":
            arg = node.args[1] if len(node.args) > 1 else None
            assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), \
                "%s:%d: _cat must name its parameter literally" % (path.name, node.lineno)
            names.add(arg.value)
    return names


def test_oracle_derives_what_the_catalogue_derives() -> None:
    # R_rot, rho, r_min, r_max, R_body, the safe-zone inset and the piece length are derived by the oracle from
    # the support geometry and the margins, never read from params.json (whose defaults hold the values for the
    # spec footprint and the default wheel radius only).
    reads = _catalogue_reads(ORACLE)
    for derived in ("r_rot", "rho", "r_body", "r_min", "r_max", "safe_zone_inset_desk", "piece_length_max",
                    "support_centroid_distance", "support_centroid_radius"):
        assert derived not in reads, "the oracle reads the derived catalogue value %s" % derived
    for read in ("support_r1", "support_delta2", "r_w", "m_w", "m_edge", "m_edge_baseline", "tower_zone_leg_wheel",
                 "tower_zone_leg_tool", "reach_max", "tower_base_radius", "body_hull_margin", "eps_surf"):
        assert read in reads, "the oracle should read %s from params.json" % read


def test_front_end_uses_only_the_input_stage(astscan: ModuleType) -> None:
    imported = _imports(FRONT)
    assert "goliath_prep.gcode" in imported and "goliath_prep.normalise" in imported, imported
    assert astscan.find_calls(str(FRONT), "read_gcode")
    assert astscan.find_calls(str(FRONT), "normalise")
    for name in FORBIDDEN_MODULES:
        assert not any(i == name or i.startswith(name + ".") for i in imported), name
    for name in FORBIDDEN_MODULES + FORBIDDEN_NAMES[:-2]:
        assert _exact(astscan.find_references(str(FRONT), name)) == [], name


def test_no_test_module_of_the_oracle_imports_the_checker(astscan: ModuleType) -> None:
    files = sorted(HERE.glob("*.py"))
    assert ORACLE in files and FRONT in files and len(files) >= 6
    for path in files:
        imported = _imports(path)
        for name in FORBIDDEN_MODULES:
            if name == "goliath_prep.api":
                continue  # the cross-check imports the pipeline by name (importlib), never at module level
            assert not any(i == name or i.startswith(name + ".") for i in imported), (path.name, name)
