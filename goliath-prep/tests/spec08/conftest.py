# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Fixtures for the spec 08 conformance tests of goliath_prep.heading_vendor.

``literal`` is spec08_literal.py, the line-by-line transcription of the spec's
pseudo-code that the differential tests compare against. It is loaded from
its path, so it works under every pytest import mode and can never be
confused with a module of the same name elsewhere.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

LITERAL = Path(__file__).resolve().parent / "spec08_literal.py"


def load_literal() -> ModuleType:
    spec = importlib.util.spec_from_file_location("goliath_prep_tests_spec08_literal", LITERAL)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load %s" % LITERAL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def literal() -> ModuleType:
    """The literal transcription of spec 08's select_heading (test code only)."""
    return load_literal()
