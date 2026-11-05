# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The red-team adapter itself (unit P20; runs now, with a stand-in pipeline).

The suite's pipeline tests skip until goliath_prep.api exists. These tests
pin, before then, what the adapter will do: the arguments it passes, how it
reads the gate snapshot, when it skips and when it fails, and how it compares
a result with expected.json, so that a wrong match can neither pass a case
nor fail it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from goliath_prep.core.findings import Finding, SourceRef, Witness
from goliath_prep.core.types import ClosedGateView, GateView, Pose
from goliath_prep.jobspec import JobSpec
from goliath_prep.machine import MachineProfile

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
CASES = {c.id: c for c in harness.load_cases()}
RT01 = CASES["RT01-rear-wheel-follows-kerf"]
RT03 = CASES["RT03-outline-closed-wheels-inside-then-hole"]
RT12A = CASES["RT12a-g91-incremental"]
RT20F = CASES["RT20-fact-tabs-hold-with-r-tabs"]


class Recorder:
    """A stand-in for prepare(): records its arguments, returns a canned result."""

    def __init__(self, *findings: Finding, ok: bool = False, report: Any = None) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.result = SimpleNamespace(ok=ok, findings=list(findings), report={} if report is None else report)

    def __call__(self, sources: Any, job: Any, profile: Any, gates: Any, prior: Any = (), measured: Any = None) -> Any:
        self.calls.append(((sources, job, profile, gates), {"prior": prior, "measured": measured}))
        return self.result


def w_surf(**fields: Any) -> Finding:
    witness = Witness(hazard="kerf of cut 0", **fields)
    return Finding.of("W-SURF", "rear wheel over the fresh kerf", witness=witness)


def test_pipeline_skips_while_the_module_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "PIPELINE_MODULE", "goliath_prep.no_such_module_for_redteam")
    with pytest.raises(pytest.skip.Exception, match="does not exist yet"):
        harness.pipeline_prepare()


def test_pipeline_fails_when_the_module_exists_without_prepare(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "PIPELINE_MODULE", "goliath_prep.core.canon")
    with pytest.raises(pytest.fail.Exception, match="no callable prepare"):
        harness.pipeline_prepare()


def test_adapter_passes_exact_bytes_typed_inputs_gates_and_measured() -> None:
    recorder = Recorder(w_surf(cut=0, wheel=1, point=(1200.0, 530.0)))
    outcome = harness.run_case(RT01, recorder)
    (sources, job, profile, gates), kwargs = recorder.calls[0]
    assert dict(sources) == {"case.gi": RT01.program.read_bytes()}
    assert isinstance(job, JobSpec) and isinstance(profile, MachineProfile)
    assert isinstance(gates, GateView) and gates.allowed("first_cut_done") is True
    assert gates.value("rotation_model") == "in-place-shortest"
    measured = kwargs["measured"]
    assert measured["info25"] == () and measured["info27"] is None
    assert measured["park_pose"] == Pose(1200.0, 620.0, 0, RT01.measured["park_pose"]["source"])
    assert outcome.ok is False and harness.case_problems(RT01, outcome) == []


def test_adapter_uses_the_desk_view_when_a_case_has_no_gates() -> None:
    recorder = Recorder()
    harness.run_case(RT12A, recorder)
    (_, _, _, gates), kwargs = recorder.calls[0]
    assert isinstance(gates, ClosedGateView) and kwargs["measured"] is None


def test_adapter_fails_loudly_when_prepare_cannot_take_the_measured_data() -> None:
    def prepare(sources: Any, job: Any, profile: Any, gates: Any, prior: Any = ()) -> Any:
        raise AssertionError("must not be called")

    with pytest.raises(pytest.fail.Exception, match="takes no 'measured' argument"):
        harness.run_case(RT01, prepare)


def test_adapter_fails_loudly_on_a_result_of_the_wrong_shape() -> None:
    def prepare(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(ok="no", findings=[], report={})

    with pytest.raises(pytest.fail.Exception, match="not a bool"):
        harness.run_case(RT01, prepare)


def test_case_gate_view_reads_no_unknown_as_yes() -> None:
    view = harness.CaseGateView(RT01.gates)
    assert view.allowed("not_a_gate") is False and view.value("not_a_gate") is None
    assert view.allowed("tabs_hold") is False and view.value("tabs_hold") is None
    view_none = harness.CaseGateView({"gates": {"arcs_emit": {"allowed": None, "value": 1}}})
    assert view_none.allowed("arcs_emit") is None and view_none.value("arcs_emit") is None
    assert view.snapshot_sha256() != ClosedGateView().snapshot_sha256()
    assert view.snapshot_sha256() == harness.CaseGateView(RT01.gates).snapshot_sha256()


def outcome(*findings: Finding, ok: bool = False, report: Any = None) -> Any:
    return harness.Outcome(ok, tuple(findings), {} if report is None else report)


@pytest.mark.parametrize(
    "finding, problem",
    [
        (w_surf(cut=0, wheel=1, point=(1200.0, 560.0)), None),
        (w_surf(cut=0, wheel=2, point=(1200.0, 560.0)), "wheel 2"),
        (w_surf(cut=1, wheel=1, point=(1200.0, 560.0)), "cut 1"),
        (w_surf(cut=0, wheel=1, point=(1200.0, 700.0)), "mm from"),
        (w_surf(cut=0, wheel=1), "no point"),
    ],
)
def test_witness_must_match_wheel_cut_and_point(finding: Finding, problem: str | None) -> None:
    problems = harness.case_problems(RT01, outcome(finding))
    if problem is None:
        assert problems == []
    else:
        assert len(problems) == 1 and problem in problems[0], problems


def test_a_refusal_filed_as_a_warning_or_an_accepted_job_fails_the_case() -> None:
    right = w_surf(cut=0, wheel=1, point=(1200.0, 560.0))
    assert harness.case_problems(RT01, outcome(right, ok=True))
    other = Finding.of("W-AREA", "a wheel leaves the panel",
                       witness=Witness(hazard="edge", cut=0, wheel=1, point=(1200.0, 560.0)))
    problems = harness.case_problems(RT01, outcome(other))
    assert problems and "no refusal W-SURF" in problems[0]


def test_any_of_and_also_refused_are_both_required() -> None:
    m_attach = Finding.of("M-ATTACH", "wheel on the loose part",
                          witness=Witness(hazard="part", wheel=2, point=(1205.0, 700.0)))
    t_attach = Finding.of("T-ATTACH", "tool in the loose part",
                          witness=Witness(hazard="part", cut=1, point=(1020.0, 603.0)))
    assert harness.case_problems(RT03, outcome(m_attach, t_attach)) == []
    assert harness.case_problems(RT03, outcome(m_attach))  # T-ATTACH missing
    wrong = Finding.of("M-ATTACH", "wheel on the loose part",
                       witness=Witness(hazard="part", wheel=3, point=(1205.0, 700.0)))
    assert harness.case_problems(RT03, outcome(wrong, t_attach))


def test_reader_witness_is_the_source_line() -> None:
    src = SourceRef("case.gi", RT12A.expected["witness"]["line"], "G91")
    right = Finding.of("D-UNSUPPORTED", "G91 is not GI", witness=Witness(hazard="G91", src=src))
    assert harness.case_problems(RT12A, outcome(right)) == []
    off = Finding.of("D-UNSUPPORTED", "G91 is not GI",
                     witness=Witness(hazard="G91", src=SourceRef("case.gi", src.line + 1, "G1")))
    assert harness.case_problems(RT12A, outcome(off))


RT27 = CASES["RT27-header-disagrees-with-job"]


def conflict(line: int, gi: Any, job: Any) -> Finding:
    witness = Witness(hazard="GI and job.toml disagree", src=SourceRef("case.gi", line, ";GOLIATH ..."),
                      sources={"GI header": gi, "job.toml panel": job})
    return Finding.of("D-GI-CONFLICT", "GI and job.toml disagree", witness=witness)


@pytest.mark.parametrize("t_gi, t_job", [(18.0, 17.6), ("18.000", "17.6"), ("18.000 mm", "17.600 mm")])
def test_conflict_must_name_both_sources_and_values(t_gi: Any, t_job: Any) -> None:
    both = outcome(conflict(4, t_gi, t_job), conflict(5, "X", "Y"))
    assert harness.case_problems(RT27, both) == []
    assert harness.case_problems(RT27, outcome(conflict(4, t_gi, t_job)))  # the TOWER2 conflict is missing
    assert harness.case_problems(RT27, outcome(conflict(4, 18.0, 18.0), conflict(5, "X", "Y")))
    one_source = Finding.of("D-GI-CONFLICT", "disagree", witness=Witness(
        hazard="x", src=SourceRef("case.gi", 4, "x"), sources={"GI header": 18.0, "GI again": 17.6}))
    assert harness.case_problems(RT27, outcome(one_source, conflict(5, "X", "Y")))


def test_check_lifted_case_fails_when_the_check_is_raised() -> None:
    raised = Finding.of("M-ATTACH", "wheel on the part", witness=Witness(hazard="part", wheel=1))
    assert harness.case_problems(RT20F, outcome(raised))
    other = Finding.of("W-PARK", "after the job", witness=Witness(hazard="park"))
    assert harness.case_problems(RT20F, outcome(other)) == []


def test_heading_table_must_have_all_36_headings() -> None:
    rows = [{"c": c, "first_failing_check": "W-SURF"} for c in range(0, 360, 10)]
    assert harness.heading_table_found({"cuts": [{"headings": rows}]}, 36)
    assert not harness.heading_table_found({"cuts": [{"headings": rows[:-1]}]}, 36)
    assert not harness.heading_table_found({"rows": [{"c": 0}] * 36}, 36)
