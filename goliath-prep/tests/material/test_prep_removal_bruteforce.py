# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The removal index against a brute force of its own (unit P06).

Random programs (straight cuts, ramps and quarter arcs, through the strict
reader and the normaliser) at a random heading: for every support and every
piece of every cut, the index's W-SURF slack must equal the brute force's,
which samples the wheel centre's path at 21 points and measures, with its own
point-to-segment arithmetic (no shapely), the distance to every earlier
in-material chord minus its removal radius and rho. The continuous minimum
lies between the sampled one and the sampled one minus half the sample spacing,
so the index can be neither looser (it would miss a hazard) nor tighter. The
time invariant holds for every wheel path of a real program, so the query
guard never fires.
"""
from __future__ import annotations

import math

import numpy as np
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from prep_removal_support import DELTA_TOOL, FP, R_TOOL, RHO, cut_piece_indices, offsets, pieces_of, program

from goliath_prep.defaults import C_GRID
from goliath_prep.material import TOUCH_TOL_MM, RemovalIndex, derive_delta

SAMPLES = 21


def _point_segment(px: np.ndarray, py: np.ndarray, ax: np.ndarray, ay: np.ndarray, bx: np.ndarray,
                   by: np.ndarray) -> np.ndarray:
    """Distances from points (column vectors) to segments (row vectors), by projection and clamping."""
    dx, dy = bx - ax, by - ay
    span = dx * dx + dy * dy
    safe = np.where(span == 0.0, 1.0, span)
    t = np.where(span == 0.0, 0.0, np.clip(((px - ax) * dx + (py - ay) * dy) / safe, 0.0, 1.0))
    qx, qy = ax + t * dx, ay + t * dy
    return np.sqrt((px - qx) ** 2 + (py - qy) ** 2)


@st.composite
def programs(draw: st.DrawFn) -> list[str]:
    """Two to four cuts: a polyline of straight moves, ramps and quarter arcs, written with three decimals."""
    lines = ["G21", "G90", "G0 Z10.000", "M3 S18000"]
    for _ in range(draw(st.integers(2, 4))):
        x = draw(st.integers(600, 1800)) * 1.0
        y = draw(st.integers(300, 900)) * 1.0
        depth = draw(st.sampled_from((0.5, 3.0, 18.3)))
        lines += ["G0 X%.3f Y%.3f" % (x, y), "G1 Z%.3f F200" % -depth]
        for _ in range(draw(st.integers(1, 4))):
            kind = draw(st.sampled_from(("line", "line", "ramp", "arc")))
            if kind == "arc":
                r = draw(st.integers(20, 150)) * 1.0
                cw = draw(st.booleans())
                # From the leftmost point of a circle about (x + r, y): up to its top, clockwise or not.
                lines.append("%s X%.3f Y%.3f I%.3f J0.000 F600" % ("G2" if cw else "G3", x + r, y + (r if cw else -r),
                                                                  r))
                x, y = x + r, y + (r if cw else -r)
            else:
                nx = x + draw(st.integers(-300, 300))
                ny = y + draw(st.integers(-300, 300))
                if (nx, ny) == (x, y):
                    nx += 10.0
                z = "" if kind == "line" else " Z%.3f" % -draw(st.sampled_from((0.5, 2.0, 6.0)))
                lines.append("G1 X%.3f Y%.3f%s F800" % (nx, ny, z))
                x, y = nx, ny
        lines.append("G0 Z10.000")
    return lines + ["M5", "M30"]


@settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(lines=programs(), c=st.sampled_from(C_GRID))
def test_index_matches_the_brute_force(lines: list[str], c: int) -> None:
    prog = program(lines)
    pcs = pieces_of(prog, derive_delta(FP, RHO, R_TOOL, DELTA_TOOL, 0.05))
    index = RemovalIndex.build(pcs, R_TOOL, DELTA_TOOL, None, footprint=FP, rho=RHO)
    data = pcs.data
    material = np.asarray(data["idx"][data["in_material"]], dtype=np.int64)
    for cut in sorted({int(v) for v in data["cut"] if v >= 0}):
        track = cut_piece_indices(pcs, cut)
        for _k, (dx, dy) in offsets(c):
            results = index.wheel_track(track, (dx, dy), RHO)  # never an InvariantError: the guard holds
            for j, result in zip(track, results):
                row = data[j]
                ax, ay = float(row["x0"]) + dx, float(row["y0"]) + dy
                bx, by = float(row["x1"]) + dx, float(row["y1"]) + dy
                radius = RHO + float(row["sagitta"])
                earlier = material[material < j]
                best = math.inf
                if earlier.size:
                    t = np.linspace(0.0, 1.0, SAMPLES)[:, None]
                    px, py = ax + t * (bx - ax), ay + t * (by - ay)
                    q = data[earlier]
                    r_i = R_TOOL + DELTA_TOOL + np.asarray(q["sagitta"], dtype=float)[None, :]
                    d = _point_segment(px, py, *(np.asarray(q[f], dtype=float)[None, :] for f in ("x0", "y0", "x1",
                                                                                                  "y1")))
                    best = float(np.min(d - r_i - radius))
                spacing = math.sqrt((bx - ax) ** 2 + (by - ay) ** 2) / (SAMPLES - 1)
                if best == math.inf:
                    assert result.slack == math.inf and result.witness is None, (cut, j)
                    continue
                assert result.slack <= best + 1e-9, (cut, j, result.slack, best)
                assert result.slack >= best - spacing / 2.0 - 1e-9, (cut, j, result.slack, best, spacing)
                assert (result.witness is not None) == (result.slack <= TOUCH_TOL_MM)
                event("piece flagged" if result.witness is not None else "piece clear")
                if (np.asarray(data[material[material < j]]["sagitta"]) > 0.0).any():
                    event("an arc removed earlier")
