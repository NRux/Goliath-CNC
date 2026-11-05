# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Helpers shared by the tests of the normaliser and the time pieces (unit P03).

Programs are written as G-code lines and read with the strict reader (P02), so
every test goes through the same path a real file takes: text, reader,
normaliser, pieces. The job is the conformance examples' job
(docs/prep/gi-examples/manifest.json), read with P01's ``JobSpec.from_mapping``.

``programs()`` is a hypothesis strategy for random generic programs: plunges,
straight moves, ramps, arcs (clockwise, counter-clockwise, full circles,
helices) and retracts, every number written with three decimals as a CAM post
writes them.
"""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

import numpy as np
from hypothesis import strategies as st

from goliath_prep.gcode import ReadResult, read_gcode
from goliath_prep.ir import Move, Program
from goliath_prep.jobspec import JobSpec
from goliath_prep.normalise import normalise

D = Decimal
Q = D("0.001")

JOB_MAPPING: dict[str, Any] = {
    "panel": {"W": 2440.0, "H": 1220.0, "T": 18.0, "tower2": "Y"},
    "placement": {"dx": 0.0, "dy": 0.0},
    "z_zero": "top",
    "tool": {"d": 6.0, "flute": 22.0, "stickout": 40.0, "rpm": 18000},
}

GI_HEADER = [
    ";GOLIATH-GI 1",
    ';GOLIATH SOURCE cam="Test CAM 1.0" post="test-post 0.1"',
    ";GOLIATH UNITS mm",
    ";GOLIATH STOCK x=2440.000 y=1220.000 t=18.000 origin=t1-corner zzero=top",
    ";GOLIATH TOWER2 axis=Y",
    ';GOLIATH TOOL n=1 d=6.000 flute=22.000 stickout=40.000 kind=flat rpm=18000 name="6mm 2F upcut"',
]
"""Six header lines: a GI body built with :func:`gi_text` starts at line 7."""


def job(**overrides: Any) -> JobSpec:
    """The examples' job with top-level keys replaced (``units="mm"``) or sections merged (``panel={...}``)."""
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
JOB_MM = job(units="mm")


def text_of(lines: Sequence[str]) -> str:
    return "\n".join(lines) + "\n"


def gi_text(body: Sequence[str], header: Sequence[str] = GI_HEADER) -> str:
    return text_of([*header, *body])


def describe(result: ReadResult) -> str:
    return "\n".join("%s %s: %s" % (f.check_id, f.severity, f.message) for f in result.findings) or "(none)"


def read(lines: Sequence[str], *, flavour: str = "generic", job_spec: JobSpec = JOB_MM) -> ReadResult:
    result = read_gcode(text_of(lines), flavour=flavour, job=job_spec)  # type: ignore[arg-type]
    assert not result.refused, describe(result)
    return result


def read_gi(body: Sequence[str], *, header: Sequence[str] = GI_HEADER, job_spec: JobSpec = JOB) -> ReadResult:
    return read_gcode(gi_text(body, header), flavour="gi", job=job_spec)


def norm(lines: Sequence[str], *, flavour: str = "generic", job_spec: JobSpec = JOB_MM) -> Program:
    """Read ``lines`` (the read must not be refused) and normalise the result."""
    return normalise(read(lines, flavour=flavour, job_spec=job_spec), job_spec)


def norm_gi(body: Sequence[str], *, header: Sequence[str] = GI_HEADER, job_spec: JobSpec = JOB) -> Program:
    result = read_gi(body, header=header, job_spec=job_spec)
    assert not result.refused, describe(result)
    return normalise(result, job_spec)


def lines_of(moves: Sequence[Move]) -> list[int]:
    out: list[int] = []
    for move in moves:
        if not out or out[-1] != move.src.line:
            out.append(move.src.line)
    return out


def notes_of(program: Program, kind: str) -> list[Any]:
    return [note for note in program.notes if note.kind == kind]


def note_lines(program: Program, kind: str) -> set[int]:
    lines: set[int] = set()
    for note in notes_of(program, kind):
        lines.update(note.lines)
    return lines


def f(value: Decimal | None) -> float:
    assert value is not None
    return float(value)


def on_grid(value: Decimal | None) -> bool:
    return value is not None and value == value.quantize(Q)


# ---------------------------------------------------------------------------
# The true path of an arc as the reader reads it (computed independently with
# the platform's maths: the tests may use what the library avoids)
# ---------------------------------------------------------------------------


def true_arc_points(move_start: Sequence[float], move_end: Sequence[float], centre: Sequence[float], cw: bool,
                    sweep_deg: float, samples: int) -> np.ndarray:
    """Points of the arc from start to end about centre (radius interpolated linearly in angle, Z linear too)."""
    sx, sy, sz = move_start
    ex, ey, ez = move_end
    cx, cy = centre
    r0 = math.hypot(sx - cx, sy - cy)
    r1 = math.hypot(ex - cx, ey - cy)
    a0 = math.atan2(sy - cy, sx - cx)
    sweep = math.radians(sweep_deg) * (-1.0 if cw else 1.0)
    t = np.linspace(0.0, 1.0, samples)
    r = r0 + (r1 - r0) * t
    a = a0 + sweep * t
    return np.column_stack([cx + r * np.cos(a), cy + r * np.sin(a), sz + (ez - sz) * t])


def distance_to_polyline(points: np.ndarray, vertices: np.ndarray) -> np.ndarray:
    """For each point (n x 3), the distance to the polyline through ``vertices`` (m x 3), in 3-D."""
    a = vertices[:-1][None, :, :]
    b = vertices[1:][None, :, :]
    p = points[:, None, :]
    ab = b - a
    denom = np.einsum("ijk,ijk->ij", ab, ab)
    t = np.where(denom > 0, np.einsum("ijk,ijk->ij", p - a, ab) / np.where(denom > 0, denom, 1.0), 0.0)
    t = np.clip(t, 0.0, 1.0)
    closest = a + t[..., None] * ab
    d = np.sqrt(np.einsum("ijk,ijk->ij", p - closest, p - closest))
    return d.min(axis=1)


# ---------------------------------------------------------------------------
# Random programs
# ---------------------------------------------------------------------------

_COORD = st.integers(min_value=200_000, max_value=2_200_000)  # um: 200 to 2200 mm
_DEPTH = st.integers(min_value=0, max_value=20_000)           # um below the stock top
_STEP = st.integers(min_value=-300_000, max_value=300_000)    # um


def _mm(um: int) -> str:
    return "%.3f" % (um / 1000.0)


@st.composite
def programs(draw: st.DrawFn, *, max_steps: int = 12) -> list[str]:
    """A generic millimetre program that the reader accepts: Z first, then XY, then random moves."""
    x, y = draw(_COORD), draw(_COORD)
    z = 10_000
    lines = ["G21", "G90", "G0 Z%s" % _mm(z), "G0 X%s Y%s" % (_mm(x), _mm(y)), "M3 S18000"]
    for _ in range(draw(st.integers(min_value=1, max_value=max_steps))):
        kind = draw(st.sampled_from(["plunge", "line", "ramp", "arc", "arc", "helix", "retract", "rapid"]))
        if kind == "plunge":
            z = -draw(_DEPTH)
            lines.append("G1 Z%s F200" % _mm(z))
        elif kind == "retract":
            z = draw(st.integers(min_value=0, max_value=15_000))
            lines.append("G0 Z%s" % _mm(z))
        elif kind in ("line", "rapid", "ramp"):
            x = min(2_400_000, max(10_000, x + draw(_STEP)))
            y = min(1_200_000, max(10_000, y + draw(_STEP)))
            word = "G0" if kind == "rapid" else "G1"
            if kind == "ramp":
                z = draw(st.integers(min_value=-20_000, max_value=5_000))
                lines.append("%s X%s Y%s Z%s F800" % (word, _mm(x), _mm(y), _mm(z)))
            else:
                lines.append("%s X%s Y%s F800" % (word, _mm(x), _mm(y)))
        else:
            radius = draw(st.integers(min_value=100, max_value=600_000))  # um
            start_angle = draw(st.integers(min_value=0, max_value=359))
            full = draw(st.booleans()) and draw(st.booleans())
            sweep = 360 if full else draw(st.integers(min_value=1, max_value=359))
            cw = draw(st.booleans())
            a0 = math.radians(start_angle)
            cx = x - radius * math.cos(a0)
            cy = y - radius * math.sin(a0)
            i_um = round(cx - x)
            j_um = round(cy - y)
            if i_um == 0 and j_um == 0:
                i_um = radius
            cx, cy = x + i_um, y + j_um
            r = math.hypot(x - cx, y - cy)
            if full:
                ex, ey = x, y
            else:
                a_start = math.atan2(y - cy, x - cx)
                a_end = a_start + math.radians(sweep) * (-1.0 if cw else 1.0)
                ex, ey = round(cx + r * math.cos(a_end)), round(cy + r * math.sin(a_end))
                if (ex, ey) == (x, y) or math.hypot(ex - x, ey - y) < 10:
                    continue  # too close to a full circle to write unambiguously at 0.001 mm
                if abs(math.hypot(ex - cx, ey - cy) - r) > 4:
                    continue
            words = "%s X%s Y%s" % ("G2" if cw else "G3", _mm(ex), _mm(ey))
            if kind == "helix":
                z = draw(st.integers(min_value=-20_000, max_value=5_000))
                words += " Z%s" % _mm(z)
            lines.append("%s I%s J%s F600" % (words, _mm(i_um), _mm(j_um)))
            x, y = ex, ey
    lines.append("G0 Z10.000")
    lines.append("M5")
    lines.append("M30")
    return lines
