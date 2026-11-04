# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""goliath-prep installs, imports and declares what the build design says (unit F01)."""
from __future__ import annotations

import importlib.metadata as md
from pathlib import Path
from types import ModuleType

import goliath_prep

PACKAGE_DIR = Path(__file__).resolve().parents[1]


def _without_marker(requirement: str) -> str:
    return requirement.split(";", 1)[0].strip()


def test_import_smoke() -> None:
    assert goliath_prep.__version__ == "0.0.1"
    # The installed metadata and the module agree (catches a stale install).
    assert md.version("goliath-prep") == goliath_prep.__version__


def test_imports_this_checkout() -> None:
    # The tests test this checkout's code, not another copy that happens to be
    # installed (nor a foreign package of the same name).
    assert Path(goliath_prep.__file__).resolve().is_relative_to(PACKAGE_DIR / "src")


def test_installed_metadata() -> None:
    meta = md.metadata("goliath-prep")
    assert meta["License-Expression"] == "GPL-3.0-or-later"
    assert meta["Requires-Python"] == ">=3.11"
    requires = md.requires("goliath-prep") or []
    runtime = sorted(r for r in requires if "extra ==" not in r)
    assert runtime == ["goliath-link", "shapely>=2.2"]
    dev = sorted(_without_marker(r) for r in requires if 'extra == "dev"' in r)
    assert dev == ["hypothesis>=6", "pytest>=8"]


def test_runtime_dependencies_import() -> None:
    import goliath_link
    import numpy
    import shapely

    assert goliath_link.__version__
    # goliath-link is not on PyPI: the one that resolved must be this checkout's.
    assert Path(goliath_link.__file__).resolve().is_relative_to(PACKAGE_DIR.parent / "goliath-link" / "src")
    assert numpy.__version__
    major, minor = (int(part) for part in shapely.__version__.split(".")[:2])
    assert (major, minor) >= (2, 2), shapely.__version__


def test_astscan_reads_goliath_prep_by_import_name(astscan: ModuleType) -> None:
    # The architecture tests of later units scan this package by name.
    versions = astscan.find_string_literals("goliath_prep", r"^0\.0\.1$")
    assert ("goliath_prep", "string") in {(loc.module, loc.kind) for loc in versions}
    guard = astscan.find_calls("goliath_prep", "ImportError")
    assert {loc.module for loc in guard} >= {"goliath_prep"}
