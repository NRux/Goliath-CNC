# SPDX-License-Identifier: GPL-3.0-or-later
"""The root goliath-cnc project offers extras [prep] and [run] (red-team C-11).

They name the sibling distributions of this repository, which are not on
PyPI: pip resolves them against goliath-prep and goliath-run installed from
this checkout (editable), in the order their READMEs give. The CI jobs for the
applications prove the resolution itself with `pip install -e ".[dev,prep,run]"`
after the siblings; this test pins the declarations that make it work.

Needs tomllib (Python >= 3.11); on older Pythons the module is skipped with
that reason, and the 3.11 and 3.14 CI jobs run it.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

tomllib = pytest.importorskip(
    "tomllib", reason="tomllib is in the standard library from Python 3.11; the 3.11 and 3.14 CI jobs run this check"
)

REPO = Path(__file__).resolve().parents[2]
SIBLINGS = ("goliath-link", "goliath-prep", "goliath-run")


def _project(folder: str) -> Dict[str, Any]:
    with (REPO / folder / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]


def _name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    assert match, "unparsable requirement %r" % requirement
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _all_requirements(project: Dict[str, Any]) -> List[str]:
    reqs = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        reqs.extend(extra)
    return reqs


def test_root_extras_resolve() -> None:
    root = _project(".")
    extras = root["optional-dependencies"]
    for extra, folder in (("prep", "goliath-prep"), ("run", "goliath-run")):
        sibling = _project(folder)
        # A bare name: no URL (a file URL breaks on Windows and in other
        # checkouts) and no environment marker (a marker would make the extra
        # install nothing on Python < 3.11 instead of failing loudly).
        assert extras.get(extra) == [sibling["name"]], (extra, extras.get(extra))
        assert sibling["name"] == folder
        assert sibling["requires-python"] == ">=3.11"
    # The root keeps what the existing CI job installs on Python 3.9.
    assert root["requires-python"] == ">=3.9"
    assert extras["dev"] == ["pytest>=8"]
    assert root["dependencies"] == ["pyserial>=3.5"]


def test_every_goliath_requirement_names_a_distribution_of_this_repository() -> None:
    names = {_project(folder)["name"] for folder in SIBLINGS}
    assert names == set(SIBLINGS)
    for folder in (".",) + SIBLINGS:
        for requirement in _all_requirements(_project(folder)):
            if _name(requirement).startswith("goliath"):
                assert _name(requirement) in names, (folder, requirement)
                assert "@" not in requirement and ";" not in requirement, (folder, requirement)


def test_sibling_dependencies_follow_the_design() -> None:
    # Build design 9.2: prep on link and shapely; run on link, prep and platformdirs.
    prep = [_name(r) for r in _project("goliath-prep")["dependencies"]]
    assert prep == ["goliath-link", "shapely"]
    run = [_name(r) for r in _project("goliath-run")["dependencies"]]
    assert run == ["goliath-link", "goliath-prep", "platformdirs"]


def test_root_distribution_declares_no_packages() -> None:
    # Without an explicit empty list, setuptools' automatic discovery registers
    # every top-level folder with an identifier name as a package of
    # goliath-cnc. old_software/ (third-party vendor installers, see
    # LICENSING.md) was declared that way in the wheel's top_level.txt. The
    # root distribution only carries the dev/prep/run extras.
    with (REPO / "pyproject.toml").open("rb") as fh:
        root = tomllib.load(fh)
    setuptools_cfg = root.get("tool", {}).get("setuptools", {})
    assert setuptools_cfg.get("packages") == [], (
        "root pyproject.toml must set [tool.setuptools] packages = [] so no folder is published as a package")
    assert "py-modules" not in setuptools_cfg or setuptools_cfg["py-modules"] == []
