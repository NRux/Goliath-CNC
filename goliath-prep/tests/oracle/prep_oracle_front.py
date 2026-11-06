# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The oracle's front end (unit P21; test code only): files to programs, the red-team cases, test jobs.

The oracle (``prep_oracle.py``) checks a normalised program. This module
makes one from a file with goliath-prep's input stage only, the strict reader
(unit P02) and the normaliser (unit P03), which the checker and the oracle
share by design: the oracle's interface takes a program. It imports no checker
unit and no context. When the reader refuses a file there is no program; the
verdict then carries the reader's refusals, marked ``where == "reader"`` and
mode ``reader-refused``, so a reader refusal is never mistaken for one the
oracle derived itself.

It also writes small GI jobs for the tests (:class:`Draft`), builds gate
snapshots (:func:`gates_r8`: the red-team suite's rung-R8 baseline) and,
for the cross-check, runs goliath-prep's pipeline (unit P17) once it exists.
"""
from __future__ import annotations

import importlib
import importlib.util
import itertools
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from goliath_prep.core.canon import canonical_sha256
from goliath_prep.core.types import ClosedGateView
from goliath_prep.gcode import read_gcode
from goliath_prep.jobspec import JobSpec
from goliath_prep.normalise import normalise

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PARAMS_JSON = REPO / "docs" / "prep" / "params.json"
REDTEAM = HERE.parent / "redteam"
DEFAULT_MACHINE = REDTEAM / "profiles" / "unmeasured.toml"
PIPELINE_MODULE = "goliath_prep.api"


def _helper(path: Path, key: str) -> Any:
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


oracle = _helper(HERE / "prep_oracle.py", "goliath_prep_tests_oracle_prep_oracle")
OracleHit = oracle.OracleHit
OracleVerdict = oracle.OracleVerdict


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


class MappingGates:
    """goliath-prep's GateView over a gate mapping (gate name to {"allowed", "value"}), for the reader."""

    def __init__(self, gates: Mapping[str, Any]) -> None:
        inner = gates.get("gates")
        self._gates: Mapping[str, Mapping[str, Any]] = inner if isinstance(inner, Mapping) else gates
        self._document = {"gates": {k: dict(v) for k, v in self._gates.items()}}

    def allowed(self, name: str) -> bool | None:
        entry = self._gates.get(name)
        return False if entry is None else entry.get("allowed")

    def value(self, name: str) -> object:
        entry = self._gates.get(name)
        if entry is None or entry.get("allowed") is not True:
            return None
        return entry.get("value")

    def snapshot_sha256(self) -> str:
        return canonical_sha256(self._document)


def _view(gates: Mapping[str, Any] | None) -> Any:
    return ClosedGateView() if gates is None else MappingGates(gates)


R8_OPEN = ("jobs_allowed", "panel_vertices_usable", "start_pose_known", "heading_map_verified", "rotation_model",
           "rotate_in_place_ok", "z_semantics_verified", "post_job_motion_known", "spindle_verified",
           "first_cut_done", "arcs_emit", "through_cuts_allowed", "pockets_allowed")


def gates_r8(start: tuple[float, float, int | None] | None, **overrides: Any) -> dict[str, dict[str, Any]]:
    """The red-team suite's baseline snapshot: a robot brought up to rung R8 (README of the red-team suite).

    ``start`` is the recorded park pose (x, y, c), or None for no start pose
    (the gate start_pose_known closed). An override ``name=False`` closes a
    gate, ``name=True`` opens it, any other value opens it with that value.
    """
    gates: dict[str, dict[str, Any]] = {}
    for name in oracle.GATES:
        gates[name] = {"allowed": name in R8_OPEN}
    gates["rotation_model"]["value"] = "in-place-shortest"
    gates["post_job_motion_known"]["value"] = "none"
    if start is None:
        gates["start_pose_known"] = {"allowed": False}
    else:
        gates["start_pose_known"]["value"] = {"x": float(start[0]), "y": float(start[1]), "c": start[2],
                                              "source": "detection.park_pose (oracle test data)"}
    for name, value in overrides.items():
        if name not in gates:
            raise KeyError("no gate %r" % name)
        if value is False:
            gates[name] = {"allowed": False}
        elif value is True:
            gates[name] = {"allowed": True, **({"value": gates[name]["value"]} if "value" in gates[name] else {})}
        else:
            gates[name] = {"allowed": True, "value": value}
    return gates


# ---------------------------------------------------------------------------
# Files to programs
# ---------------------------------------------------------------------------


def _reader_hits(result: Any) -> list[Any]:
    out = []
    for f in result.refusals():
        w = f.witness
        out.append(OracleHit(f.check_id, rule=None if w is None else (w.gate or w.rule), where="reader",
                             line=None if w is None or w.src is None else w.src.line, detail=f.message))
    return out


def read_source(path: Path, job_toml: Path, gates: Mapping[str, Any] | None) -> tuple[Any, list[Any]]:
    """(program, []) when the input stage reads the file, (None, the reader's refusals) when it refuses it."""
    text = Path(path).read_bytes().decode("utf-8")
    job = JobSpec.load(job_toml)
    flavour = "dialect" if Path(path).suffix.lower() == ".nc" else "auto"
    result = read_gcode(text, flavour=flavour, job=job, gates=_view(gates), file=Path(path).name)
    if result.refused:
        return None, _reader_hits(result)
    return normalise(result, job), []


def read_program(path: Path, job_toml: Path, gates: Mapping[str, Any] | None) -> Any:
    program, refusals = read_source(path, job_toml, gates)
    if program is None:
        raise AssertionError("the reader refused %s: %s" % (path, "; ".join(h.describe() for h in refusals)))
    return program


def check_file(path: Path, job_toml: Path, machine_toml: Path, *, gates: Mapping[str, Any] | None = None,
               measured: Mapping[str, Any] | None = None) -> Any:
    program, refusals = read_source(path, job_toml, gates)
    if program is None:
        return OracleVerdict(False, refusals, ("the input stage (the strict reader, unit P02, shared with the checker "
                                               "by design) refused the file: there is no program to check",),
                             "reader-refused")
    return oracle.oracle_check(program, job_toml, machine_toml, PARAMS_JSON, measured, gates=gates)


# ---------------------------------------------------------------------------
# The red-team cases (unit P20)
# ---------------------------------------------------------------------------


cases_module = _helper(REDTEAM / "prep_redteam_cases.py", "goliath_prep_tests_redteam_prep_redteam_cases")


@dataclass
class RedTeamCase:
    """A red-team case folder, with whether the input stage refuses its file (found by reading it)."""

    case: Any
    _reader: bool | None = None

    @property
    def id(self) -> str:
        return self.case.id

    @property
    def expected(self) -> Mapping[str, Any]:
        return self.case.expected

    @property
    def gates(self) -> Mapping[str, Any] | None:
        return None if self.case.gates is None else self.case.gates["gates"]

    @property
    def measured(self) -> Mapping[str, Any] | None:
        if not self.case.has_vertex_stream:
            return None
        m = self.case.measured
        return {"info25": tuple(m["info25"]), "info27": m["info27"], "park_pose": m.get("park_pose")}

    @property
    def is_reader(self) -> bool:
        if self._reader is None:
            program, refusals = read_source(self.case.program, self.case.job_path, self.gates)
            self._reader = program is None
        return self._reader


def redteam_cases() -> list[RedTeamCase]:
    return [RedTeamCase(c) for c in cases_module.load_cases()]


def oracle_check_case(case: RedTeamCase) -> Any:
    return check_file(case.case.program, case.case.job_path, case.case.machine_path, gates=case.gates,
                      measured=case.measured)


# ---------------------------------------------------------------------------
# Test jobs
# ---------------------------------------------------------------------------


@dataclass
class Op:
    """One GI operation: plunge at ``start`` to ``depth``, follow ``path`` (G1 x y, or G2/G3 x y i j), retract."""

    start: tuple[float, float]
    path: Sequence[tuple]
    depth: float
    c: int | None = None
    through: bool = False
    kind: str | None = None


_COUNTER = itertools.count()


def _n(value: float) -> str:
    return "%.3f" % value


@dataclass
class Draft:
    """A small GI job and its job.toml (panel W x H x T, tower 2 on ``tower2``, a 6 mm bit)."""

    W: float = 2440.0
    H: float = 1220.0
    T: float = 18.0
    tower2: str = "Y"
    ops: list[Op] = field(default_factory=list)
    prior: list[tuple[list[list[float]], float]] = field(default_factory=list)
    obstacles: list[tuple[list[list[float]], float]] = field(default_factory=list)
    fixations: list[tuple[tuple[float, float], float]] = field(default_factory=list)
    supports: dict[str, float] = field(default_factory=dict)

    def gi_text(self) -> str:
        lines = [";GOLIATH-GI 1", ';GOLIATH SOURCE cam="oracle tests" post="hand-written 1"', ";GOLIATH UNITS mm",
                 ";GOLIATH STOCK x=%s y=%s t=%s origin=t1-corner zzero=top" % (_n(self.W), _n(self.H), _n(self.T)),
                 ";GOLIATH TOWER2 axis=%s" % self.tower2,
                 ';GOLIATH TOOL n=1 d=6.000 flute=22.000 stickout=40.000 kind=flat rpm=18000 name="6mm 2F upcut"',
                 "G0 Z10.000", "M3 S18000"]
        for i, op in enumerate(self.ops):
            kind = op.kind or ("profile" if op.through else "engrave")
            head = ";GOLIATH OP id=op%d kind=%s through=%d tool=1" % (i, kind, 1 if op.through else 0)
            if op.c is not None:
                head += " c=%d" % op.c
            lines += [head, "G0 X%s Y%s" % (_n(op.start[0]), _n(op.start[1])),
                      "G1 Z%s F200" % _n(-op.depth)]
            for step in op.path:
                if step[0] == "G1":
                    lines.append("G1 X%s Y%s F800" % (_n(step[1]), _n(step[2])))
                else:
                    word, x, y, i_, j_ = step
                    lines.append("%s X%s Y%s I%s J%s F800" % (word, _n(x), _n(y), _n(i_), _n(j_)))
            lines += ["G0 Z10.000", ";GOLIATH OPEND id=op%d" % i]
        lines += ["M5", "M30", "; Licence: SPDX-License-Identifier: GPL-3.0-or-later"]
        return "\n".join(lines) + "\n"

    def job_text(self) -> str:
        lines = ['units = "mm"', 'z_zero = "top"', "", "[panel]", "W = %r" % float(self.W), "H = %r" % float(self.H),
                 "T = %r" % float(self.T), 'tower2 = "%s"' % self.tower2, "", "[placement]", "dx = 0.0", "dy = 0.0",
                 "", "[tool]", "d = 6.0", "flute = 22.0", "stickout = 40.0", "rpm = 18000", 'kind = "flat"']
        if self.supports:
            lines += ["", "[supports]"] + ["%s = %r" % (k, float(v)) for k, v in self.supports.items()]
        for poly, height in self.obstacles:
            lines += ["", "[[obstacles]]", "polygon = %s" % _toml_points(poly), "height_mm = %r" % float(height),
                      'kind = "clamp"']
        for (x, y), height in self.fixations:
            lines += ["", "[[fixations]]", "point = [%r, %r]" % (float(x), float(y)), "height_mm = %r" % float(height)]
        for poly, depth in self.prior:
            lines += ["", "[[prior_removal]]", "polygon = %s" % _toml_points(poly), "depth_mm = %r" % float(depth)]
        return "\n".join(lines) + "\n"

    def write(self, tmp_path: Path) -> tuple[Path, Path]:
        folder = Path(tmp_path) / ("draft-%d" % next(_COUNTER))
        folder.mkdir(parents=True)
        gi = folder / "job.gi"
        job = folder / "job.toml"
        gi.write_text(self.gi_text(), encoding="utf-8", newline="\n")
        job.write_text(self.job_text(), encoding="utf-8", newline="\n")
        return gi, job


def _toml_points(poly: Sequence[Sequence[float]]) -> str:
    return "[" + ", ".join("[%r, %r]" % (float(x), float(y)) for x, y in poly) + "]"


def check_draft(draft: Draft, tmp_path: Path, *, gates: Mapping[str, Any] | None,
                measured: Mapping[str, Any] | None = None, machine: Path = DEFAULT_MACHINE) -> Any:
    gi, job = draft.write(tmp_path)
    return check_file(gi, job, machine, gates=gates, measured=measured)


def arc_draft(cx: float, cy: float, r: float, a0: float, sweep: float, ccw: bool, c: int, depth: float, *,
              chords: float | None) -> Draft:
    """One arc groove about (cx, cy) of radius r from angle a0 through ``sweep`` degrees: as a G2/G3 arc
    (``chords`` None) or linearised into G1 chords whose sagitta is at most ``chords`` mm."""
    sign = 1.0 if ccw else -1.0
    a0r = math.radians(a0)
    sx, sy = cx + r * math.cos(a0r), cy + r * math.sin(a0r)
    a1r = a0r + sign * math.radians(sweep)
    ex, ey = cx + r * math.cos(a1r), cy + r * math.sin(a1r)
    if chords is None:
        path = [("G3" if ccw else "G2", ex, ey, cx - sx, cy - sy)]
    else:
        step = 2.0 * math.acos(max(-1.0, 1.0 - chords / r))
        n = max(1, int(math.ceil(math.radians(sweep) / step)))
        path = [("G1", cx + r * math.cos(a0r + sign * math.radians(sweep) * k / n),
                 cy + r * math.sin(a0r + sign * math.radians(sweep) * k / n)) for k in range(1, n + 1)]
    return Draft(ops=[Op((sx, sy), path, depth=depth, c=c, through=depth >= 18.0)])


# ---------------------------------------------------------------------------
# goliath-prep's pipeline (unit P17), for the cross-check
# ---------------------------------------------------------------------------


def _pipeline() -> Any:
    if importlib.util.find_spec(PIPELINE_MODULE) is None:
        raise RuntimeError("%s (unit P17) does not exist yet" % PIPELINE_MODULE)
    module = importlib.import_module(PIPELINE_MODULE)
    prepare = getattr(module, "prepare", None)
    if not callable(prepare):
        raise AssertionError("%s exists but has no callable prepare()" % PIPELINE_MODULE)
    return prepare


def run_pipeline(draft: Draft, tmp_path: Path, *, gates: Mapping[str, Any] | None,
                 machine: Path = DEFAULT_MACHINE) -> Any:
    """goliath_prep.api.prepare(sources, job, profile, gates) on the draft (P17's documented interface)."""
    from goliath_prep.machine import MachineProfile

    gi, job = draft.write(tmp_path)
    prepare = _pipeline()
    result = prepare({gi.name: gi.read_bytes()}, JobSpec.load(job), MachineProfile.load(machine), _view(gates))
    for name in ("ok", "findings"):
        if not hasattr(result, name):
            raise AssertionError("prepare() returned %r without '%s'" % (type(result).__name__, name))
    result.draft_files = (gi, job)
    return result


def upload_bytes(result: Any) -> bytes:
    """The certified upload bytes of a CertResult (``upload``: bytes, or an object carrying them)."""
    upload = getattr(result, "upload", None)
    for candidate in (upload, getattr(upload, "data", None), getattr(upload, "payload", None),
                      getattr(upload, "bytes", None)):
        if isinstance(candidate, (bytes, bytearray)):
            return bytes(candidate)
    raise AssertionError("the pipeline certified the job but its CertResult.upload is %r: no upload bytes to "
                         "check as shipped" % (upload,))


def check_upload(upload: bytes, draft: Draft, tmp_path: Path, *, gates: Mapping[str, Any] | None,
                 machine: Path = DEFAULT_MACHINE) -> Any:
    """The oracle on the shipped bytes, read back in Goliath-dialect verify mode."""
    gi, job = draft.write(tmp_path)
    nc = gi.with_name("upload.nc")
    nc.write_bytes(upload)
    return check_file(nc, job, machine, gates=gates)


def pipeline_check_draft(draft: Draft, tmp_path: Path, *, gates: Mapping[str, Any] | None,
                         measured: Mapping[str, Any] | None = None, machine: Path = DEFAULT_MACHINE) -> Any:
    """The pipeline's verdict on a draft, in the oracle's verdict shape (for the properties)."""
    if measured is not None:
        raise AssertionError("the properties run at the desk only")
    result = run_pipeline(draft, tmp_path, gates=gates, machine=machine)
    hits = [OracleHit(f.check_id, rule=None if f.witness is None else f.witness.rule, where="pipeline",
                      depth_mm=None if f.witness is None or f.witness.slack_mm is None else -float(f.witness.slack_mm))
            for f in result.findings if f.severity in ("refuse", "internal-error")]
    return OracleVerdict(bool(result.ok), hits, ("goliath-prep's pipeline (unit P17)",), "pipeline")
