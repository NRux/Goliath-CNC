# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Helpers shared by the tests of the removal index (unit P06).

Programs are written as G-code lines and go the way a real file goes: the
strict reader (P02), the normaliser and the time pieces (P03), then the
removal index. The job is a 2440 x 1220 x 18 mm sheet with a 6 mm bit
(r_tool + delta_tool = 3.25 mm); the machine profile is the minimal one (spec
footprint, wheel contact radius unmeasured: rho = 55 mm). Gate views here are
test data, never a robot's snapshot; serials are obviously fake.
"""
from __future__ import annotations

import copy
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from shapely.geometry import LineString, Point

from goliath_prep.context import build_context
from goliath_prep.core.types import CheckContext, ClosedGateView, FootprintSet
from goliath_prep.defaults import vendor_footprint
from goliath_prep.gcode import ReadResult, read_gcode
from goliath_prep.geom import support_offsets
from goliath_prep.ir import Program
from goliath_prep.jobspec import JobSpec
from goliath_prep.machine import MachineProfile
from goliath_prep.normalise import normalise
from goliath_prep.pieces import PieceArray, pieces

R_TOOL = 3.0
DELTA_TOOL = 0.25
RHO = 55.0
E_MAX = 0.05
REACH = RHO + R_TOOL + DELTA_TOOL
"""58.25 mm: a wheel centre this close to the path of a straight kerf puts its disc on the kerf."""

FP = vendor_footprint()

JOB_MAPPING: dict[str, Any] = {
    "units": "mm",
    "panel": {"W": 2440.0, "H": 1220.0, "T": 18.0, "tower2": "Y"},
    "tool": {"d": 6.0, "flute": 22.0, "stickout": 40.0, "rpm": 18000},
}

MINIMAL_PROFILE = 'profile_version = 1\nserial = "TEST-SERIAL-0001"\n'

GI_HEADER = [
    ";GOLIATH-GI 1",
    ';GOLIATH SOURCE cam="removal tests" post="hand-written 1"',
    ";GOLIATH UNITS mm",
    ";GOLIATH STOCK x=2440.000 y=1220.000 t=18.000 origin=t1-corner zzero=top",
    ";GOLIATH TOWER2 axis=Y",
    ';GOLIATH TOOL n=1 d=6.000 flute=22.000 stickout=40.000 kind=flat rpm=18000 name="6mm 2F upcut"',
]


def job(**overrides: Any) -> JobSpec:
    """The test job with top-level keys replaced (``prior_removal=[...]``) or sections merged (``tool={...}``)."""
    mapping = copy.deepcopy(JOB_MAPPING)
    for key, value in overrides.items():
        if value is None:
            mapping.pop(key, None)
        elif isinstance(value, Mapping) and isinstance(mapping.get(key), Mapping):
            merged = dict(mapping[key])
            merged.update(value)
            mapping[key] = merged
        else:
            mapping[key] = value
    return JobSpec.from_mapping(mapping)


JOB = job()


def profile(text: str = MINIMAL_PROFILE) -> MachineProfile:
    return MachineProfile.from_mapping(tomllib.loads(text, parse_float=Decimal))


@dataclass(frozen=True)
class Gates:
    """A GateView over fixed tables, in the form the context builder reads (kerf values as {w_b, L_ride, h_r}).

    A name missing from ``allowed_map`` is closed. Test data, never a robot's snapshot.
    """

    allowed_map: tuple[tuple[str, object], ...] = ()
    value_map: tuple[tuple[str, object], ...] = ()

    def allowed(self, name: str) -> bool | None:
        return dict(self.allowed_map).get(name, False)  # type: ignore[return-value]

    def value(self, name: str) -> object:
        return dict(self.value_map).get(name)

    def snapshot_sha256(self) -> str:
        return "cd" * 32


def kerf_gates(w_b: float, l_ride: float, h_r: float, *, allowed: bool | None = True) -> Gates:
    """The gate view with ``kerf_crossing`` in the state ``allowed``, carrying the R8k values."""
    return Gates(allowed_map=(("kerf_crossing", allowed),),
                 value_map=(("kerf_crossing", {"w_b": w_b, "L_ride": l_ride, "h_r": h_r}),))


def context(job_spec: JobSpec = JOB, gates: Any = None) -> CheckContext:
    return build_context(job_spec, profile(), ClosedGateView() if gates is None else gates)


def text_of(lines: Sequence[str]) -> str:
    return "\n".join(lines) + "\n"


def describe(result: ReadResult) -> str:
    return "\n".join("%s %s: %s" % (f.check_id, f.severity, f.message) for f in result.findings) or "(none)"


def program(lines: Sequence[str], *, job_spec: JobSpec = JOB) -> Program:
    """Generic G-code ``lines``, read (never refused) and normalised from the ReadResult."""
    result = read_gcode(text_of(lines), flavour="generic", job=job_spec)
    assert not result.refused, describe(result)
    return normalise(result, job_spec)


def program_gi(body: Sequence[str], *, job_spec: JobSpec = JOB) -> Program:
    """A GI file: the test header, then ``body``."""
    result = read_gcode(text_of([*GI_HEADER, *body]), flavour="gi", job=job_spec)
    assert not result.refused, describe(result)
    return normalise(result, job_spec)


def cut_lines(*paths: Sequence[tuple[float, float]], depth: float = 18.3, z_safe: float = 10.0) -> list[str]:
    """Generic G-code: one cut per path (a plunge at its first point to -depth, feeds along it, a retract)."""
    lines = ["G21", "G90", "G0 Z%.3f" % z_safe, "M3 S18000"]
    for path in paths:
        x, y = path[0]
        lines.append("G0 X%.3f Y%.3f" % (x, y))
        lines.append("G1 Z%.3f F200" % -depth)
        for x, y in path[1:]:
            lines.append("G1 X%.3f Y%.3f F800" % (x, y))
        lines.append("G0 Z%.3f" % z_safe)
    lines += ["M5", "M30"]
    return lines


def pieces_of(prog: Program, delta: float = 50.0) -> PieceArray:
    return pieces(prog, delta)


def wheel_path(pcs: PieceArray, j: int, offset: tuple[float, float]) -> LineString | Point:
    """The wheel centre's path during piece ``j`` for a support at ``offset`` from the tool (heading fixed)."""
    row = pcs[j]
    a = (float(row["x0"]) + offset[0], float(row["y0"]) + offset[1])
    b = (float(row["x1"]) + offset[0], float(row["y1"]) + offset[1])
    return Point(a) if a == b else LineString([a, b])


def offsets(c: int, fp: FootprintSet | None = None) -> list[tuple[int, tuple[float, float]]]:
    """(support number k, offset d_k) of every support position at ``C = c`` (the spec footprint by default)."""
    return [(pos.k, d) for pos, d in support_offsets(c, FP if fp is None else fp)]


def offset_of(k: int, c: int) -> tuple[float, float]:
    """The offset of spec support ``k`` (1 rear, 2 right, 3 left) at ``C = c``."""
    return dict(offsets(c))[k]


def cut_piece_indices(pcs: PieceArray, cut: int) -> list[int]:
    return [int(i) for i in pcs["idx"][pcs["cut"] == cut]]
