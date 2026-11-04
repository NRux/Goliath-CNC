# SPDX-License-Identifier: GPL-3.0-or-later
"""Layout of the application packages goliath-prep and goliath-run (unit F01).

Python 3.9 compatible: the repository-level suite runs on 3.9.
"""
from __future__ import annotations

import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

import pytest

REPO = Path(__file__).resolve().parents[2]
PACKAGES = {"goliath-prep": "goliath_prep", "goliath-run": "goliath_run"}

# Test folders whose modules can share one pytest session. pytest imports test
# modules by their file name, so two files with the same name in two of these
# folders break a run that collects both.
TEST_ROOTS = ("tests", "goliath-prep/tests", "goliath-run/tests")


@pytest.mark.parametrize("folder", sorted(PACKAGES))
def test_src_layout(folder: str) -> None:
    module = PACKAGES[folder]
    assert (REPO / folder / "src" / module / "__init__.py").is_file()
    assert not (REPO / folder / module).exists(), "flat layout: %s/%s" % (folder, module)
    assert (REPO / folder / "pyproject.toml").is_file()
    assert (REPO / folder / "README.md").is_file()
    assert (REPO / folder / "tests").is_dir()


@pytest.mark.parametrize("folder", sorted(PACKAGES))
def test_package_ships_the_gpl_text(folder: str) -> None:
    # A package built from this folder (sdist or wheel) leaves the repository,
    # so it carries the licence text itself, identical to the root LICENSE.
    assert (REPO / folder / "LICENSE").read_bytes() == (REPO / "LICENSE").read_bytes()


@pytest.mark.parametrize("folder", sorted(PACKAGES))
def test_version_0_0_1_in_init_and_pyproject(folder: str) -> None:
    init = (REPO / folder / "src" / PACKAGES[folder] / "__init__.py").read_text(encoding="utf-8")
    assert '\n__version__ = "0.0.1"\n' in init
    pyproject = (REPO / folder / "pyproject.toml").read_text(encoding="utf-8")
    assert '\nversion = "0.0.1"\n' in pyproject


def test_test_module_names_are_unique_across_suites() -> None:
    seen: Dict[str, List[str]] = defaultdict(list)
    for root in TEST_ROOTS:
        for path in (REPO / root).rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            if path.name in ("conftest.py", "__init__.py") or "/fixtures/" in rel:
                continue
            seen[path.name].append(rel)
    clashes = {name: paths for name, paths in seen.items() if len(paths) > 1}
    assert not clashes, "test file names must be unique across %s (prefix them test_prep_ / test_run_): %s" % (
        ", ".join(TEST_ROOTS),
        clashes,
    )


@pytest.mark.skipif(
    sys.version_info >= (3, 11),
    reason="the guard only fires below Python 3.11; the 3.9 runs of this suite exercise it",
)
@pytest.mark.parametrize("folder", sorted(PACKAGES))
def test_old_python_gets_a_clear_error(folder: str) -> None:
    src = REPO / folder / "src"
    code = "import sys; sys.path.insert(0, sys.argv[1]); import %s" % PACKAGES[folder]
    result = subprocess.run([sys.executable, "-c", code, str(src)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "ImportError: %s requires Python >= 3.11" % folder in result.stderr, result.stderr
