# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Fixtures for the tests of the local interface server (unit U01; the project design, section 8.2).

The test doubles and clients live in run_ui_harness.py. It is loaded here from
its path and registered under its own name, so ``from run_ui_harness import
...`` works under every pytest import mode and can never pick up another module
of the same name.
"""
from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

HARNESS = Path(__file__).resolve().with_name("run_ui_harness.py")


def _load_harness() -> ModuleType:
    loaded = sys.modules.get("run_ui_harness")
    if loaded is not None:
        if Path(getattr(loaded, "__file__", "")).resolve() != HARNESS:
            raise RuntimeError("a different module named run_ui_harness is already imported: %r" % loaded)
        return loaded
    spec = importlib.util.spec_from_file_location("run_ui_harness", HARNESS)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load %s" % HARNESS)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_ui_harness"] = module
    spec.loader.exec_module(module)
    return module


harness = _load_harness()


@pytest.fixture
def source() -> Iterator[harness.FakeSource]:
    s = harness.FakeSource()
    yield s
    s.end()


@pytest.fixture
def sink() -> harness.RecordingSink:
    return harness.RecordingSink()


@pytest.fixture
def ui(source: harness.FakeSource, sink: harness.RecordingSink) -> Iterator[harness.Harness]:
    from goliath_run.ui import serve

    server = serve(source, sink, port=0)
    try:
        yield harness.Harness(server, source, sink)
    finally:
        server.close()
        source.end()
