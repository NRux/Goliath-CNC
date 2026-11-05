# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The red-team suite: jobs goliath-prep MUST refuse, run through its pipeline (unit P20).

One test per case folder under cases/ (README.md here lists them): RT1 to
RT15 and RT19 to RT35 of the project design's red-team list (section 10.3),
some split into lettered variants so that each folder holds one defect. RT16
and RT17 (series jobs) belong to unit P19's suite and RT18 (certificate
replay) to the runner's (unit R08).

Each test runs its case through ``goliath_prep.api.prepare`` and requires the
refusal and witness that expected.json names. While that module does not
exist (unit P17), every test is skipped with that reason; once it exists, a
case that is not refused as expected fails, and so does any mismatch with the
documented interface (prep_redteam_cases.py). No test is marked xfail.
"""
from __future__ import annotations

import importlib.util
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
CASES = harness.load_cases()


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_redteam_case_is_refused_as_expected(case: Any) -> None:
    prepare = harness.pipeline_prepare()
    outcome = harness.run_case(case, prepare)
    problems = harness.case_problems(case, outcome)
    assert not problems, "%s (%s): %s\n  expected: %s\n  rationale: %s" % (
        case.id, case.expected["title"], "\n  ".join(problems), case.expected["check_id"],
        case.expected["rationale"])
