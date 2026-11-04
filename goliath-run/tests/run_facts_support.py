# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Shared helpers for the tests of unit R01 (facts store and gate computation).

The probe reports under ``fixtures/probe_reports/`` were written by goliath-link's
probe run against its simulator (``fixtures/probe_reports/make_probe_reports.py``);
nothing in them is typed by hand. ``SIM_SERIAL`` is the simulator's placeholder
serial number, not a robot's.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from goliath_run.facts import Fact, FactStore, read_probe_report

HERE = Path(__file__).resolve().parent
PROBE_REPORTS = HERE / "fixtures" / "probe_reports"
REPORT_NAME = "goliath-probe-20261104T090000Z.json"
SIM_SERIAL = "000000000001"
TEST_SERIAL = "TEST-SERIAL-0001"
OPERATOR = "Test Operator"
AT = "2026-11-04T10:00:00Z"


def report_path(scenario: str) -> Path:
    path = PROBE_REPORTS / scenario / REPORT_NAME
    assert path.is_file(), "missing committed probe report %s (run make_probe_reports.py)" % path
    return path


def load_generator() -> ModuleType:
    """fixtures/probe_reports/make_probe_reports.py, the generator of the committed reports."""
    name = "make_probe_reports"
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(name, PROBE_REPORTS / "make_probe_reports.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def r1_store(root: Path, scenario: str, **kwargs: Any) -> FactStore:
    """A fact store for the simulated robot of ``scenario`` holding that probe report's R1 facts."""
    imported = read_probe_report(report_path(scenario), **kwargs)
    store = FactStore.open(root, imported.serial, imported.firmware)
    store.record_many(imported.facts)
    return store


def observed(fact_id: str, value: Any, *, serial: str = SIM_SERIAL, firmware: str | None = "01.02.00",
             rung: str | None = "R2", basis: str = "observed", unknown_id: str | None = None,
             recorded_at: str = AT, note: str = "") -> Fact:
    """A fact as a rung would record it (tests only)."""
    return Fact(id=fact_id, value=value, basis=basis, unknown_id=unknown_id, rung=rung,
                evidence=("test evidence for %s" % fact_id,), serial=serial, firmware=firmware,
                recorded_at=recorded_at, note=note)
