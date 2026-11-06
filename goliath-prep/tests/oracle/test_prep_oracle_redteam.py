# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The independent oracle against the red-team suite (unit P21; the project design, sections 10.3 and 10.4).

Every red-team case (goliath-prep/tests/redteam/cases, unit P20) is a job the
safety model says MUST be refused. The oracle is a second checker, written
apart from goliath-prep's, so it must refuse every one of them, and for the
reason the case names: the case's check is among the oracle's violations
(for a link or first-line check, with the case's rule), and where the case
gives a witness point the oracle finds a violation of that check near it. A
case that opens a relaxation through its gate snapshot (verdict
``check-lifted``) must not be refused by that check, the relaxation must be
named in the oracle's notes, and its sibling case without the relaxation must
be refused by exactly that check.

Reader cases (RT12, RT14, RT27, RT30, RT31) never become a program: the input
stage refuses them before any check runs. The oracle shares that input stage
with the checker by design (its interface takes a program), so for them it
reports the reader's refusal under the reader's check id, marked as the
reader's (``where == "reader"``), and a test below keeps that set of cases
exactly the reader cases.
"""
from __future__ import annotations

import importlib.util
import math
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent


def _helper(name: str) -> Any:
    key = "goliath_prep_tests_oracle_" + name
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / (name + ".py"))
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


front = _helper("prep_oracle_front")
CASES = front.redteam_cases()
REFUSED = [c for c in CASES if c.expected["verdict"] == "refused"]
LIFTED = [c for c in CASES if c.expected["verdict"] == "check-lifted"]
WITNESSED = [c for c in REFUSED if not c.is_reader and ("near" in c.expected["witness"]
                                                        or "any_of" in c.expected["witness"])]
READER_IDS = frozenset({"D-GI-VERSION", "D-GI-SYNTAX", "D-GI-HEADER", "D-GI-OP", "D-GI-CONFLICT", "D-SYNTAX",
                        "D-UNSUPPORTED", "D-UNITS", "D-RANGE", "D-ARC", "D-AFTER-END", "D-MARKER", "D-SINGLETOOL"})


@lru_cache(maxsize=None)
def _verdict(case_id: str) -> Any:
    case = next(c for c in CASES if c.id == case_id)
    return front.oracle_check_case(case)


def _explain(case: Any, verdict: Any) -> str:
    return "%s (%s)\n  expected: %s %s\n  oracle: ok=%s mode=%s\n  %s\n  notes: %s" % (
        case.id, case.expected["title"], case.expected["check_id"], case.expected["witness"], verdict.ok,
        verdict.mode, "\n  ".join(h.describe() for h in verdict.violations[:40]) or "no violations",
        "; ".join(verdict.notes))


def test_the_red_team_suite_is_there() -> None:
    # Positive control: an empty case list would make every test below pass vacuously.
    assert len(CASES) >= 50, [c.id for c in CASES]
    assert len(REFUSED) >= 48
    assert {c.expected["case"] for c in LIFTED} == {"RT20", "RT24"}
    assert len(WITNESSED) >= 15


def test_reader_cases_are_exactly_the_cases_the_input_stage_refuses() -> None:
    readers = {c.id for c in CASES if c.is_reader}
    assert readers == {c.id for c in REFUSED if c.expected["check_id"] in READER_IDS
                       or (c.expected["check_id"] == "G-GATE" and c.expected["witness"].get("gate") == "drop_dwells")}
    assert len(readers) == 14, sorted(readers)  # RT12a-g, RT14a-b, RT27a-b, RT30, RT31a-b
    for case in CASES:
        verdict = _verdict(case.id)
        from_reader = [h for h in verdict.violations if h.where == "reader"]
        assert bool(from_reader) == case.is_reader, _explain(case, verdict)
        if case.is_reader:
            assert verdict.mode == "reader-refused" and all(h.where == "reader" for h in verdict.violations)


@pytest.mark.parametrize("case", REFUSED, ids=[c.id for c in REFUSED])
def test_oracle_refuses_every_redteam_case(case: Any) -> None:
    verdict = _verdict(case.id)
    assert verdict.ok is False, _explain(case, verdict)
    named = verdict.check_ids()
    wanted = [case.expected["check_id"]] + [a["check_id"] for a in case.expected.get("also_refused", [])]
    for check_id in wanted:
        assert check_id in named, "the oracle refused, but not by %s\n%s" % (check_id, _explain(case, verdict))
    witness = case.expected["witness"]
    for key in ("rule", "gate"):
        if key in witness:
            assert any(h.check == case.expected["check_id"] and h.rule == witness[key] for h in verdict.violations), \
                "no %s violation with %s %s\n%s" % (case.expected["check_id"], key, witness[key],
                                                    _explain(case, verdict))
    if "line" in witness:
        assert any(h.check == case.expected["check_id"] and h.line == witness["line"] for h in verdict.violations), \
            "no %s violation at line %s\n%s" % (case.expected["check_id"], witness["line"], _explain(case, verdict))


def _matches(hit: Any, spec: dict[str, Any]) -> bool:
    if "wheel" in spec and hit.wheel != spec["wheel"]:
        return False
    if "rule" in spec and hit.rule != spec["rule"]:
        return False
    if "cut" in spec and hit.cut is not None and hit.cut != spec["cut"]:
        return False
    if "near" in spec:
        if hit.point is None:
            return False
        return math.hypot(hit.point[0] - spec["near"][0], hit.point[1] - spec["near"][1]) <= spec["tol_mm"]
    return True


@pytest.mark.parametrize("case", WITNESSED, ids=[c.id for c in WITNESSED])
def test_oracle_witness_lies_where_the_case_says(case: Any) -> None:
    witness = dict(case.expected["witness"])
    alternatives = witness.pop("any_of", None) or [{}]
    verdict = _verdict(case.id)
    candidates = [h for h in verdict.violations if h.check == case.expected["check_id"]]
    assert any(_matches(h, {**witness, **alt}) for h in candidates for alt in alternatives), (
        "no %s violation matches the witness %s\n%s" % (case.expected["check_id"], case.expected["witness"],
                                                         _explain(case, verdict)))


@pytest.mark.parametrize("case", LIFTED, ids=[c.id for c in LIFTED])
def test_oracle_lifts_only_what_the_relaxation_lifts(case: Any) -> None:
    verdict = _verdict(case.id)
    lifted = case.expected["check_id"]
    assert lifted not in verdict.check_ids(), _explain(case, verdict)
    relaxation = {"RT20": "R-TABS", "RT24": "R-KERF"}[case.expected["case"]]
    assert any(relaxation in note for note in verdict.notes), _explain(case, verdict)
    # The same job without the relaxation is refused by exactly that check (its sibling case).
    sibling = next(c for c in REFUSED if c.expected["case"] == case.expected["case"])
    assert lifted in _verdict(sibling.id).check_ids(), _explain(sibling, _verdict(sibling.id))
