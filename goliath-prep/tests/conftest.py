# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Fixtures shared by goliath-prep's tests.

Test file names must be unique across goliath-prep/tests, goliath-run/tests and
tests/ (the suites run in one pytest session): prefix them ``test_prep_``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ASTSCAN = Path(__file__).resolve().parents[2] / "tests" / "arch" / "astscan.py"


def load_astscan() -> ModuleType:
    """The repository's AST helpers (tests/arch/astscan.py), loaded from this checkout."""
    if not ASTSCAN.is_file():
        raise FileNotFoundError(
            "%s is missing: architecture tests need the whole repository checkout, not the package alone" % ASTSCAN
        )
    loaded = sys.modules.get("astscan")
    if loaded is not None:
        if Path(getattr(loaded, "__file__", "")).resolve() != ASTSCAN:
            raise RuntimeError("a different module named astscan is already imported: %r" % loaded)
        return loaded
    spec = importlib.util.spec_from_file_location("astscan", ASTSCAN)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load %s" % ASTSCAN)
    module = importlib.util.module_from_spec(spec)
    sys.modules["astscan"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def astscan() -> ModuleType:
    """tests/arch/astscan.py: find_calls, find_references, find_string_literals."""
    return load_astscan()
