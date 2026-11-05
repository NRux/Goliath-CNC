# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Each red-team case's claim holds under the safety model's own formulas (unit P20; runs now).

The suite re-derives every geometric case with prep_redteam_model.py
(docs/prep/safety-model.md and params.json, sampled at about 1 mm, shapely
for areas; not goliath-prep's checker) and requires:

* the expected check fails, and its earliest violation matches the expected
  witness (wheel, cut, rule, near the stated point);
* every other check that fails is one the case lists (``also_refused`` or
  ``co_occurring``), so the case is about what it says;
* a 'check-lifted' variant does not fail its check.

An unforced cut must fail its check at all 36 headings; a job-time case's raw
vertex stream must pass exactly the decoding hypotheses its rationale names.
"""
from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent


def _helper(name: str) -> Any:
    key = "goliath_prep_tests_redteam_" + name
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / (name + ".py"))
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


harness = _helper("prep_redteam_cases")
derive = _helper("prep_redteam_derive")
CASES = harness.load_cases()
GEOMETRIC = [c for c in CASES if derive.kind(c) in ("geometric", "dialect", "job-time")]
JOB_TIME = [c for c in CASES if derive.kind(c) == "job-time"]

HYPOTHESES = {
    "RT15a-measured-panel-40mm-narrower": [],
    "RT15b-tower-distance-mismatch": [],
    "RT15c-vertices-fit-two-hypotheses": [("xy", "identity"), ("xy", "swap")],
    "RT25-mirror-y-frame": [("xy", "mirror-y")],
    "RT26-square-panel-identity-and-swap": [("xy", "identity"), ("xy", "swap")],
}
"""The hypotheses (encoding, axis map) each job-time case's vertex stream passes: never exactly the identity."""


def _alternatives(spec: dict[str, Any]) -> list[dict[str, Any]]:
    rest = {k: v for k, v in spec.items() if k != "any_of"}
    return [{**rest, **alt} for alt in spec["any_of"]] if "any_of" in spec else [spec]


def _claim_holds(violations: list[Any], check_id: str, spec: dict[str, Any]) -> None:
    """Each alternative's earliest violation (of its wheel) matches its cut, rule and point."""
    found = sorted((v for v in violations if v.check == check_id), key=lambda v: v.t)
    assert found, "the derivation finds no %s" % check_id
    for alt in _alternatives(spec):
        mine = [v for v in found if "wheel" not in alt or v.wheel == alt["wheel"]]
        if "rule" in alt:
            mine = [v for v in mine if v.rule == alt["rule"]]
        assert mine, "no %s for %r: %s" % (check_id, alt, found[:3])
        v = mine[0]
        if "cut" in alt:
            assert v.cut == alt["cut"], (v, alt)
        if "line" in alt:
            assert v.line == alt["line"], (v, alt)
        if "near" in alt:
            d = math.hypot(v.point[0] - alt["near"][0], v.point[1] - alt["near"][1])
            assert d <= alt["tol_mm"], "%s first at %r, %.1f mm from %r (tolerance %.1f)" % (
                check_id, v.point, d, alt["near"], alt["tol_mm"])


def _allowed(case: Any) -> set[str]:
    e = case.expected
    return {e["check_id"]} | {x["check_id"] for x in e.get("also_refused", [])} | set(e["co_occurring"])


@pytest.mark.parametrize("case", GEOMETRIC, ids=[c.id for c in GEOMETRIC])
def test_case_claim_follows_from_the_safety_model(case: Any) -> None:
    e = case.expected
    if derive.unforced(case):
        table = derive.derive_headings(case)
        assert len(table) == 36
        missing = [c for c, failing in table.items() if e["check_id"] not in failing]
        assert not missing, "%s does not fail at C = %s" % (e["check_id"], missing)
        extra = set().union(*table.values()) - _allowed(case)
        assert not extra, "unlisted checks fail: %s" % sorted(extra)
        return
    violations = derive.derive(case)
    failing = {v.check for v in violations}
    if e["verdict"] == "check-lifted":
        assert e["check_id"] not in failing, [v for v in violations if v.check == e["check_id"]][:3]
    elif derive.kind(case) in ("geometric",):
        _claim_holds(violations, e["check_id"], e["witness"])
        for extra in e.get("also_refused", []):
            _claim_holds(violations, extra["check_id"], extra["witness"])
    extra = failing - _allowed(case)
    assert not extra, "unlisted checks fail: %s" % sorted((v.check, v.rule, v.wheel, v.point) for v in violations
                                                          if v.check in extra)[:5]


@pytest.mark.parametrize("case", JOB_TIME, ids=[c.id for c in JOB_TIME])
def test_job_time_case_passes_the_hypotheses_its_rationale_names(case: Any) -> None:
    assert derive.hypotheses(case) == HYPOTHESES[case.id]
    assert ("xy", "identity") not in derive.hypotheses(case) or len(derive.hypotheses(case)) > 1


def test_every_job_time_case_has_a_hypotheses_entry() -> None:
    assert sorted(HYPOTHESES) == sorted(c.id for c in JOB_TIME)


def test_hypotheses_control_the_declared_rectangle_passes_identity_only() -> None:
    """A control: the stream of the declared rectangle itself passes exactly one hypothesis, the identity."""
    case = next(c for c in JOB_TIME if c.id.startswith("RT15b"))
    stream = ["INFO;25;0.0000;0.0000", "INFO;25;2440.0000;0.0000", "INFO;25;2440.0000;1220.0000",
              "INFO;25;0.0000;1220.0000"]
    control = harness.Case(case.path, case.expected, case.program, case.gates,
                           {**case.measured, "info25": stream, "info27": "INFO;27;1220.0000"}, False)
    assert derive.hypotheses(control) == [("xy", "identity")]


def test_rt9_long_swing_is_the_hazard_not_the_short_turn() -> None:
    """Under the short-way rotation model the same job has no W-ROT: the case tests the numeric reading."""
    case = next(c for c in CASES if c.id.startswith("RT09"))
    sc = derive.scene(case)
    sc.gates["rotation_model"] = {"allowed": True, "value": "in-place-shortest"}
    assert not sc.link_violations(0, "a") and not sc.link_violations(0, "b")


def test_rt35_an_observed_in_place_turn_would_not_cross_the_kerf() -> None:
    """With an observed rotation model the wheels stay beyond the kerf: only the full disc catches it."""
    case = next(c for c in CASES if c.id.startswith("RT35"))
    sc = derive.scene(case)
    sc.gates["rotation_model"] = {"allowed": True, "value": "in-place-shortest"}
    assert not sc.link_violations(0, "a") and not sc.link_violations(0, "b")


def test_rt7_vendor_sampling_sees_both_vertices_outside_the_triangle() -> None:
    """spec 08 tests the supports only at vertices: both of the rear wheel's are outside Z1, the path is not."""
    from shapely.geometry import LineString, Point

    case = next(c for c in CASES if c.id.startswith("RT07"))
    sc = derive.scene(case)
    cut = sc.cuts[0]
    dx, dy = derive.model.support_offsets(cut.c)[1]
    z1 = sc.zone(150.0, "tower1")
    ends = [Point(x + dx, y + dy) for x, y in (cut.start, cut.end)]
    assert not any(z1.contains(p) for p in ends)
    assert LineString(ends).intersects(z1)
