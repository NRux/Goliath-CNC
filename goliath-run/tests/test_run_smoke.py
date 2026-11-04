# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""goliath-run installs, imports and declares what the build design says (unit F01)."""
from __future__ import annotations

import importlib.metadata as md
from pathlib import Path
from types import ModuleType

import goliath_run

PACKAGE_DIR = Path(__file__).resolve().parents[1]


def _without_marker(requirement: str) -> str:
    return requirement.split(";", 1)[0].strip()


def test_import_smoke() -> None:
    assert goliath_run.__version__ == "0.0.1"
    # The installed metadata and the module agree (catches a stale install).
    assert md.version("goliath-run") == goliath_run.__version__


def test_imports_this_checkout() -> None:
    # The tests test this checkout's code, not another copy that happens to be
    # installed (nor a foreign package of the same name).
    assert Path(goliath_run.__file__).resolve().is_relative_to(PACKAGE_DIR / "src")


def test_installed_metadata() -> None:
    meta = md.metadata("goliath-run")
    assert meta["License-Expression"] == "GPL-3.0-or-later"
    assert meta["Requires-Python"] == ">=3.11"
    requires = md.requires("goliath-run") or []
    runtime = sorted(r for r in requires if "extra ==" not in r)
    assert runtime == ["goliath-link", "goliath-prep", "platformdirs>=4"]
    dev = sorted(_without_marker(r) for r in requires if 'extra == "dev"' in r)
    assert dev == ["hypothesis>=6", "pytest>=8"]


def test_runtime_dependencies_import() -> None:
    import goliath_link
    import goliath_prep
    import platformdirs

    assert goliath_link.__version__
    assert goliath_prep.__version__ == "0.0.1"
    # The sibling is this checkout's goliath-prep, not another copy.
    assert Path(goliath_prep.__file__).resolve().is_relative_to(PACKAGE_DIR.parent / "goliath-prep" / "src")
    assert int(platformdirs.__version__.split(".")[0]) >= 4


def test_astscan_reads_goliath_run_by_import_name(astscan: ModuleType) -> None:
    # The architecture tests of later units (one upload call site, no raw
    # transport send outside the driver, no never-send literal) scan this
    # package by name.
    versions = astscan.find_string_literals("goliath_run", r"^0\.0\.1$")
    assert ("goliath_run", "string") in {(loc.module, loc.kind) for loc in versions}
    guard = astscan.find_calls("goliath_run", "ImportError")
    assert {loc.module for loc in guard} >= {"goliath_run"}
