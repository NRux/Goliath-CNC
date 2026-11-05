# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The red-team cases on disk and the adapter that runs them through goliath-prep (test code only).

A case is a folder under ``cases/`` (README.md in this folder describes the
format). This module loads one, gives its gates as a ``GateView``, runs it
through the pipeline entry point and compares the result with its
``expected.json``.

**The adapter is the one place that knows the pipeline's API.** It follows the
documented interface of unit P17 (the project design, section 12):
``goliath_prep.api.prepare(sources, job, profile, gates, prior=())`` returning
a ``CertResult`` with ``ok`` and ``findings`` (a list of
:class:`goliath_prep.core.findings.Finding`) and ``report``. Two choices are
this suite's, stated here so the pipeline's author can meet them:

* ``sources`` is a mapping from the input file's name (``case.gi`` or, for a
  Goliath-dialect program verified as written, ``case.nc``) to its exact bytes;
* a job-time case (``measured.json`` with the raw lines of a panel
  detection) passes them as ``measured=``: a read-only mapping with
  ``info25`` (the raw ``INFO;25`` lines, in order), ``info27`` (the raw
  ``INFO;27`` line or None) and ``park_pose`` (a
  :class:`goliath_prep.core.types.Pose` or None). The interface of P17 has no
  parameter for these yet; a pipeline without one fails such a case loudly
  instead of dropping the data.

A desk case runs with P17's documented arguments only. Its ``measured.json``
holds no vertex stream, only the park pose, which is the fact
``detection.park_pose`` and reaches the pipeline as the gate snapshot's
``start_pose_known`` value; the adapter checks that the two are one record
and fails otherwise. (Passed as measured data, an empty ``INFO;25`` list would
read as "no vertex stream observed in this power cycle", a job-time refusal,
safety-model 12, instead of the desk.)

Until ``goliath_prep.api`` exists, :func:`pipeline_prepare` skips with that
reason. Once the module exists, every problem fails loudly: an import error
inside it, a missing ``prepare``, a parameter it does not take, a result of an
unexpected shape.
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

import pytest

from goliath_prep.core.canon import canonical_sha256
from goliath_prep.core.findings import Finding
from goliath_prep.core.types import ClosedGateView, GateView, Pose
from goliath_prep.jobspec import JobSpec
from goliath_prep.machine import MachineProfile

__all__ = [
    "CASES_DIR",
    "CUT_INDEX_BASE",
    "PIPELINE_MODULE",
    "Case",
    "CaseGateView",
    "Outcome",
    "case_problems",
    "load_case",
    "load_cases",
    "pipeline_prepare",
    "run_case",
]

HERE = Path(__file__).resolve().parent
CASES_DIR = HERE / "cases"
DEFAULT_MACHINE = HERE / "profiles" / "unmeasured.toml"
PIPELINE_MODULE = "goliath_prep.api"
CUT_INDEX_BASE = 0
"""Witness ``cut`` values count cuts in program order from 0, like piece indices (the Witness type allows 0)."""

NUMBER_TOL = 0.0005


@dataclass(frozen=True)
class Case:
    """One case folder, read."""

    path: Path
    expected: Mapping[str, Any]
    program: Path
    gates: Mapping[str, Any] | None
    measured: Mapping[str, Any] | None
    has_prior: bool

    @property
    def id(self) -> str:
        return self.path.name

    @property
    def design_id(self) -> str:
        return self.expected["case"]

    @property
    def job_path(self) -> Path:
        return self.path / "job.toml"

    @property
    def machine_path(self) -> Path:
        own = self.path / "machine.toml"
        return own if own.is_file() else DEFAULT_MACHINE

    @property
    def is_dialect(self) -> bool:
        return self.program.suffix == ".nc"

    def job_document(self) -> dict[str, Any]:
        with self.job_path.open("rb") as fh:
            return tomllib.load(fh)

    def machine_document(self) -> dict[str, Any]:
        with self.machine_path.open("rb") as fh:
            return tomllib.load(fh)

    def sources(self) -> Mapping[str, bytes]:
        return MappingProxyType({self.program.name: self.program.read_bytes()})

    def gate_view(self) -> GateView:
        """The case's gate snapshot, or goliath-prep's all-closed desk view when it has none."""
        return ClosedGateView() if self.gates is None else CaseGateView(self.gates)

    def park_pose(self) -> Pose | None:
        if self.measured is None or self.measured.get("park_pose") is None:
            return None
        return Pose.from_json(self.measured["park_pose"])

    @property
    def has_vertex_stream(self) -> bool:
        """A job-time case: its measured.json holds the raw INFO;25 lines (and INFO;27) of a panel detection.

        A desk case's measured.json holds only the park pose, the fact
        ``detection.park_pose`` that its gate snapshot carries as well.
        """
        return self.measured is not None and (bool(self.measured["info25"]) or self.measured["info27"] is not None)

    def measured_record(self) -> Mapping[str, Any]:
        assert self.measured is not None
        return MappingProxyType({
            "info25": tuple(self.measured["info25"]),
            "info27": self.measured["info27"],
            "park_pose": self.park_pose(),
        })


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_case(path: Path) -> Case:
    programs = sorted(p for p in path.iterdir() if p.name in ("case.gi", "case.nc"))
    if len(programs) != 1:
        raise ValueError("%s: a case holds exactly one of case.gi and case.nc, found %s"
                         % (path.name, [p.name for p in programs]))
    gates = path / "gates.json"
    measured = path / "measured.json"
    return Case(
        path=path,
        expected=_json(path / "expected.json"),
        program=programs[0],
        gates=_json(gates) if gates.is_file() else None,
        measured=_json(measured) if measured.is_file() else None,
        has_prior=(path / "prior.json").is_file(),
    )


def load_cases() -> list[Case]:
    return [load_case(p) for p in sorted(CASES_DIR.iterdir()) if p.is_dir()]


class CaseGateView:
    """A ``GateView`` over a case's gates.json (safety-model 1.4: no unknown is read as "yes").

    ``allowed`` returns the snapshot's value for a gate it names (True, False
    or None) and False for any other name. ``value`` is the open gate's value,
    or None when the gate is not open or carries none.
    """

    def __init__(self, document: Mapping[str, Any]) -> None:
        self._document = document
        self._gates: Mapping[str, Mapping[str, Any]] = document["gates"]

    def allowed(self, name: str) -> bool | None:
        entry = self._gates.get(name)
        return False if entry is None else entry["allowed"]

    def value(self, name: str) -> object:
        entry = self._gates.get(name)
        if entry is None or entry["allowed"] is not True:
            return None
        return entry.get("value")

    def snapshot_sha256(self) -> str:
        return canonical_sha256(self._document)


@dataclass(frozen=True)
class Outcome:
    ok: bool
    findings: tuple[Finding, ...]
    report: Any


def pipeline_prepare() -> Callable[..., Any]:
    """goliath_prep.api.prepare; skips only while the module does not exist."""
    if importlib.util.find_spec(PIPELINE_MODULE) is None:
        pytest.skip("the pipeline entry point %s.prepare (unit P17) does not exist yet: the red-team case runs, "
                    "and must pass, as soon as it does" % PIPELINE_MODULE)
    module = importlib.import_module(PIPELINE_MODULE)
    prepare = getattr(module, "prepare", None)
    if not callable(prepare):
        pytest.fail("%s exists but has no callable prepare(): the red-team suite cannot run" % PIPELINE_MODULE)
    return prepare


def run_case(case: Case, prepare: Callable[..., Any]) -> Outcome:
    """Run one case through ``prepare`` with the documented arguments; fail loudly on any mismatch."""
    if case.has_prior:
        pytest.fail("%s has prior.json: series cases belong to unit P19's suite, and this adapter has no series "
                    "input; nothing is dropped silently" % case.id)
    params = inspect.signature(prepare).parameters
    open_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    kwargs: dict[str, Any] = {}
    gates = case.gate_view()
    if case.has_vertex_stream:
        if "measured" not in params and not open_kwargs:
            pytest.fail("%s carries measured.json (raw INFO;25/27 lines and the park pose), but prepare%s takes no "
                        "'measured' argument: the case cannot be run without it, and the data is never dropped"
                        % (case.id, inspect.signature(prepare)))
        kwargs["measured"] = case.measured_record()
    elif case.measured is not None:
        # A desk case: its park pose is the fact detection.park_pose, which reaches the pipeline as the gate
        # snapshot's start_pose_known value. Check it is the same record, so that nothing is dropped.
        carried = gates.value("start_pose_known") if gates.allowed("start_pose_known") is True else None
        if case.measured["park_pose"] != carried:
            pytest.fail("%s: measured.json's park pose %r is not the gate snapshot's start_pose_known value %r: the "
                        "desk case would reach the pipeline without it" % (case.id, case.measured["park_pose"],
                                                                         carried))
    job = JobSpec.load(case.job_path)
    profile = MachineProfile.load(case.machine_path)
    result = prepare(case.sources(), job, profile, gates, **kwargs)
    for name in ("ok", "findings", "report"):
        if not hasattr(result, name):
            pytest.fail("%s: prepare() returned %r without '%s' (expected a CertResult with ok, findings, report)"
                        % (case.id, type(result).__name__, name))
    if not isinstance(result.ok, bool):
        pytest.fail("%s: CertResult.ok is %r, not a bool" % (case.id, result.ok))
    findings = tuple(result.findings)
    for f in findings:
        if not isinstance(f, Finding):
            pytest.fail("%s: a finding is %r, not a goliath_prep.core.findings.Finding" % (case.id, f))
    return Outcome(result.ok, findings, result.report)


# ---------------------------------------------------------------------------
# Comparing a result with expected.json
# ---------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    """A number, or a string that is one with an optional unit after it ("18.000", "17.6 mm"), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        head = value.strip().split(" ")[0]
        try:
            return float(head)
        except ValueError:
            return None
    return None


def _same(found: Any, want: Any) -> bool:
    """A witness value equals an expected one: numbers within 0.0005 (gi-format 10), anything else exactly."""
    if isinstance(want, (int, float)) and not isinstance(want, bool):
        number = _number(found)
        return number is not None and abs(number - float(want)) < NUMBER_TOL
    return found == want


def witness_problem(finding: Finding, spec: Mapping[str, Any]) -> str | None:
    """None when the finding's witness meets the spec, else why not."""
    if "any_of" in spec:
        rest = {k: v for k, v in spec.items() if k != "any_of"}
        reasons = []
        for alternative in spec["any_of"]:
            why = witness_problem(finding, {**rest, **alternative})
            if why is None:
                return None
            reasons.append(why)
        return "no alternative matches (%s)" % "; ".join(reasons)
    w = finding.witness
    if w is None:
        return "no witness"
    if "cut" in spec and w.cut != spec["cut"]:
        return "cut %r, expected %r" % (w.cut, spec["cut"])
    for key in ("wheel", "column", "rule", "gate", "unlock_rung", "fact"):
        if key in spec and getattr(w, key) != spec[key]:
            return "%s %r, expected %r" % (key, getattr(w, key), spec[key])
    if "line" in spec and (w.src is None or w.src.line != spec["line"]):
        return "line %r, expected %r" % (None if w.src is None else w.src.line, spec["line"])
    if "near" in spec:
        if w.point is None:
            return "no point, expected one near %r" % (spec["near"],)
        d = math.hypot(w.point[0] - spec["near"][0], w.point[1] - spec["near"][1])
        if d > spec["tol_mm"]:
            return "point %r is %.1f mm from %r (tolerance %.1f mm)" % (w.point, d, spec["near"], spec["tol_mm"])
    if "source_values" in spec:
        sources = dict(w.sources or ())
        for want in spec["source_values"]:
            if not any(_same(v, want) for v in sources.values()):
                return "sources %r do not include %r" % (sources, want)
        names = list(sources)
        if not (any("job.toml" in n for n in names) and any("job.toml" not in n for n in names)):
            return "sources %r do not name both the GI file and job.toml" % (names,)
    if "text" in spec:
        words = ("%s %s" % (finding.message, w.hazard)).lower()
        for word in spec["text"]:
            if word.lower() not in words:
                return "message and hazard do not mention %r" % word
    return None


def _refusals(outcome: Outcome, check_id: str) -> list[Finding]:
    return [f for f in outcome.findings if f.check_id == check_id and f.severity == "refuse"]


def _describe(findings: Iterable[Finding]) -> str:
    return "; ".join("%s/%s %s" % (f.check_id, f.severity, None if f.witness is None else f.witness.to_json())
                     for f in findings) or "none"


def _rows(report: Any) -> Iterable[Sequence[Any]]:
    """Every list in a report (nested mappings and lists), for the heading-table check."""
    if isinstance(report, Mapping):
        for v in report.values():
            yield from _rows(v)
    elif isinstance(report, (list, tuple)):
        yield report
        for v in report:
            yield from _rows(v)


def heading_table_found(report: Any, rows: int) -> bool:
    """A list of ``rows`` mappings, each naming its C (``c``, ``C`` or ``heading_c``), covering 0 to 350."""
    for table in _rows(report):
        if len(table) != rows or not all(isinstance(r, Mapping) for r in table):
            continue
        cs = set()
        for r in table:
            for key in ("c", "C", "heading_c"):
                if key in r:
                    cs.add(r[key])
        if cs == set(range(0, 360, 10)):
            return True
    return False


def case_problems(case: Case, outcome: Outcome) -> list[str]:
    """Every way the outcome misses the case's expected.json; empty when it meets it."""
    e = case.expected
    problems = []
    if e["verdict"] == "refused":
        if outcome.ok is not False:
            problems.append("the job was not refused (ok=%r)" % outcome.ok)
        wanted = [{"check_id": e["check_id"], "witness": e["witness"]}, *e.get("also_refused", [])]
        for want in wanted:
            candidates = _refusals(outcome, want["check_id"])
            if not candidates:
                problems.append("no refusal %s; findings: %s" % (want["check_id"], _describe(outcome.findings)))
                continue
            reasons = [witness_problem(f, want["witness"]) for f in candidates]
            if all(r is not None for r in reasons):
                problems.append("refusal %s found, but no witness matches %r: %s"
                                % (want["check_id"], want["witness"], "; ".join(r for r in reasons if r)))
        rows = e.get("heading_table_rows")
        if rows is not None and not heading_table_found(outcome.report, rows):
            problems.append("the report holds no %d-row heading table (C 0 to 350 with the first failing check)"
                            % rows)
    elif e["verdict"] == "check-lifted":
        raised = [f for f in outcome.findings if f.check_id == e["check_id"]]
        if raised:
            problems.append("%s must be lifted by the gate snapshot, but was raised: %s"
                            % (e["check_id"], _describe(raised)))
        crashed = [f for f in outcome.findings if f.severity == "internal-error"]
        if crashed:
            problems.append("the pipeline reported an internal error, so it has not shown that the gate snapshot "
                            "lifts %s: %s" % (e["check_id"], _describe(crashed)))
    else:
        problems.append("unknown verdict %r" % e["verdict"])
    return problems
