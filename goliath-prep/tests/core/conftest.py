# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Fixtures for the tests of goliath_prep.core (unit F04).

``codegen`` is prep_ids_codegen.py, the generator of goliath_prep/core/ids.py.
It is loaded from its path, so it works under every pytest import mode and can
never be confused with a module of the same name elsewhere.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

CODEGEN = Path(__file__).resolve().parent / "prep_ids_codegen.py"


def load_codegen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("goliath_prep_tests_core_ids_codegen", CODEGEN)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load %s" % CODEGEN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def codegen() -> ModuleType:
    """The generator of goliath_prep/core/ids.py (test code only)."""
    return load_codegen()
