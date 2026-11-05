# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Re-derive each red-team case's claim from the safety model, without goliath-prep's checker (test code only).

:func:`derive` runs a case's program through :mod:`prep_redteam_model` at
the case's forced headings, links and first line, and returns every
violation it finds; :func:`derive_headings` does so for each of the 36
headings of an unforced cut; :func:`dialect_findings` applies the
shipped-bytes rules a Goliath-dialect case exercises; :func:`hypotheses`
decodes a job-time case's raw ``INFO;25`` lines under every hypothesis of
safety-model section 12. The tests compare these with expected.json.
"""
from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from typing import Any

from shapely.geometry import Point, Polygon, box

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


gi = _helper("prep_redteam_gi")
model = _helper("prep_redteam_model")
PARAMS = model.PARAMS

READER_FINDINGS = frozenset({
    "D-GI-VERSION", "D-GI-SYNTAX", "D-GI-HEADER", "D-GI-OP", "D-GI-CONFLICT", "N-GI-UNKNOWN-KEY", "D-SYNTAX",
    "D-UNSUPPORTED", "D-UNITS", "D-RANGE", "D-ARC", "D-AFTER-END", "D-MARKER", "D-SINGLETOOL", "G-GATE",
})
"""The finding ids a GI reader emits (gi-format section 12)."""


def kind(case: Any) -> str:
    """'reader', 'dialect', 'job-time' or 'geometric'."""
    if case.is_dialect:
        return "dialect"
    if case.expected["check_id"] in READER_FINDINGS:
        return "reader"
    if case.measured is not None and case.measured["info25"]:
        return "job-time"
    return "geometric"


def reader_job(job: dict[str, Any]) -> dict[str, Any]:
    """The job.toml values a GI header is compared with (gi-format section 10)."""
    return {"panel": job["panel"], "placement": job.get("placement", {"dx": 0.0, "dy": 0.0}),
            "z_zero": job.get("z_zero", "top"), "tool": job["tool"]}


def scene(case: Any, *, headings: dict[int, int] | None = None, step: float = model.STEP) -> Any:
    job = case.job_document()
    if case.is_dialect:
        moves = gi.read_dialect(case.program.read_bytes()).moves
    else:
        prog = gi.read_gi(case.program.read_bytes().decode("utf-8"), job=reader_job(job))
        moves = prog.moves
    park = case.park_pose()
    return model.Scene(job, moves, headings=headings, gates=(case.gates or {}).get("gates", {}),
                       park=None if park is None else (park.x, park.y, park.c), machine=case.machine_document(),
                       step=step)


def _structure_times(sc: Any) -> list[int] | None:
    """Every in-material move end, when some removal could break holding material (safety-model 7.4)."""
    breaks = sc.T - sc.t_hold
    deep_prior = any(depth > breaks for _, depth in sc.prior_removal())
    deep_cut = any(-s.z > breaks for cut in sc.cuts for s in cut.samples)
    if not (deep_prior or deep_cut):
        return None
    times = [sc.cuts[0].samples[0].t]
    for cut in sc.cuts:
        prev = None
        for s in cut.samples:
            if prev is not None and s.line != prev.line:
                times.append(prev.t + 1)
            prev = s
        times.append(cut.samples[-1].t + 1)
    return sorted(set(times))


def derive(case: Any) -> list[Any]:
    """Every violation of the case's program at its forced headings: cuts, links, the first line, the end."""
    sc = scene(case)
    times = _structure_times(sc)
    out = list(sc.cut_violations(structure_times=times))
    if times is not None:
        out.extend(_towersep(sc, times))
    combined = not sc.gate("rotate_in_place_ok")
    # A dialect program is verified as written: its links are its own lines, not the planner's.
    for k in range(0 if case.is_dialect else len(sc.cuts) - 1):
        detached = None
        if times is not None:
            detached = sc.structure(sc.cuts[k].samples[-1].t + 1)[1]
        if combined and sc.cuts[k].c != sc.cuts[k + 1].c:
            out.extend(_combined(sc, k))
            continue
        a = sc.link_violations(k, "a", detached=detached)
        b = sc.link_violations(k, "b", detached=detached)
        if a and b:  # the planner picks the pivot with more slack; the link fails only if both fail
            out.extend(a)
            out.extend(b)
    out.extend(sc.first_line_violations())
    out.extend(sc.park_violations())
    return out


def _towersep(sc: Any, times: list[int]) -> list[Any]:
    """M-TOWERSEP at the first sample after which a2 is no longer in a1's part (safety-model 7.4)."""
    if sc.structure(times[0])[2]:
        s = sc.cuts[0].samples[0]
        return [model.Violation("M-TOWERSEP", "cut", 0, None, (s.x, s.y), s.t, line=s.line)]
    samples = [s for cut in sc.cuts for s in cut.samples]
    lo, hi = 0, len(samples) - 1
    if not sc.structure(samples[hi].t + 1)[2]:
        return []
    while hi - lo > 1:  # bisection over the samples, like safety-model 7.4's over pieces
        mid = (lo + hi) // 2
        if sc.structure(samples[mid].t + 1)[2]:
            hi = mid
        else:
            lo = mid
    s = samples[hi]
    return [model.Violation("M-TOWERSEP", "cut", s.cut, None, (s.x, s.y), s.t, line=s.line)]


def _combined(sc: Any, k: int) -> list[Any]:
    """The disclosed combined profile (safety-model 9.4, rotation model known): envelopes in the numeric direction."""
    out = []
    area = sc.wheel_area().buffer(model.RHO)
    removal = sc.removal_union(sc.cuts[k + 1].samples[0].t, sc.eps)
    t0 = sc.cuts[k].samples[-1].t + 1
    for wheel, envelope in sc.combined_envelope(k).items():
        if not area.contains(envelope):
            outside = envelope.difference(area)
            far = max(outside.exterior.coords if outside.geom_type == "Polygon" else
                      [c for g in outside.geoms for c in g.exterior.coords],
                      key=lambda p: area.exterior.distance(Point(p)))
            out.append(model.Violation("W-RAPID", "link", k, wheel, (far[0], far[1]), t0, None, "W-AREA"))
        if not removal.is_empty and envelope.intersects(removal):
            p = envelope.intersection(removal).representative_point()
            out.append(model.Violation("W-RAPID", "link", k, wheel, (p.x, p.y), t0, None, "W-SURF"))
    return out


def derive_headings(case: Any) -> dict[int, set[str]]:
    """For a single unforced cut: the checks that fail at each of the 36 headings."""
    out = {}
    first = scene(case, headings={0: 0})
    length = sum(math.hypot(b.x - a.x, b.y - a.y) for cut in first.cuts for a, b in zip(cut.samples, cut.samples[1:]))
    step = max(model.STEP, length / 1000.0)  # 36 headings of a long cut: about 1000 samples each is enough
    first = scene(case, headings={0: 0}, step=step)
    times = _structure_times(first)
    states = first.detached_states(times or [])  # the structure does not depend on the heading
    for c in range(0, 360, 10):
        sc = scene(case, headings={0: c}, step=step)
        out[c] = {v.check for v in sc.cut_violations(states=states)}
    return out


def unforced(case: Any) -> bool:
    return any(cut.c is None for cut in scene(case).cuts)


# ---------------------------------------------------------------------------
# Goliath dialect: the shipped-bytes rules of safety-model 11.5 the cases use
# ---------------------------------------------------------------------------


def dialect_findings(case: Any) -> list[tuple[str, int]]:
    """(check id, line) for D-SPINDLE, D-ZSAFE, G-GATE (arcs) and T-TOWER on rapids in a dialect program."""
    prog = gi.read_dialect(case.program.read_bytes())
    gates = (case.gates or {}).get("gates", {})
    arcs_open = gates.get("arcs_emit", {}).get("allowed") is True
    z_safety = float(PARAMS["z_safety"])
    eps = float(PARAMS["eps_surf"])
    sc = scene(case)
    out: list[tuple[str, int]] = list(("PROBLEM:" + p, 0) for p in prog.problems)
    for m in prog.moves:
        z0 = m.start[2]
        z1 = m.end[2]
        in_material = (z1 is not None and z1 < -eps) or (z0 is not None and z0 < -eps)
        if in_material and not m.spindle_on:
            out.append(("D-SPINDLE", m.line))
        if m.code == 0 and z1 is not None and z1 < z_safety:
            out.append(("D-ZSAFE", m.line))
        if m.code in (2, 3) and not arcs_open:
            out.append(("G-GATE", m.line))
        if m.code == 0 and None not in m.start and None not in m.end:
            for x, y, z in model.sample_move(m):
                s = model.Sample(0, x, y, z, "link", None, m.line, m.c)
                if any(check == "T-TOWER" for check, _ in sc.tool_checks(s)):
                    out.append(("T-TOWER", m.line))
                    break
    return out


# ---------------------------------------------------------------------------
# Job-time geometry: the hypotheses of safety-model section 12
# ---------------------------------------------------------------------------

AXIS_MAPS = {
    "identity": lambda x, y: (x, y),
    "swap": lambda x, y: (y, x),
    "mirror-x": lambda x, y: (-x, y),
    "mirror-y": lambda x, y: (x, -y),
    "rot180": lambda x, y: (-x, -y),
    "swap-mirror-x": lambda x, y: (-y, x),
    "swap-mirror-y": lambda x, y: (y, -x),
    "swap-rot180": lambda x, y: (-y, -x),
}
"""The eight axis maps that keep tower 1 at the origin (safety-model 12, step 2)."""

ENCODINGS = {
    "xy": lambda a, b: (a, b),
    "polar-deg": lambda a, b: (a * math.cos(math.radians(b)), a * math.sin(math.radians(b))),
    "polar-rad": lambda a, b: (a * math.cos(b), a * math.sin(b)),
}
"""x;y and length;angle; the angle's unit is not documented, so both degrees and radians are tried."""


def _hausdorff(poly: Polygon, rect: Polygon) -> float:
    return poly.exterior.hausdorff_distance(rect.exterior)


def allowed_axis_maps(case: Any) -> dict[str, Any]:
    """The axis maps still allowed (safety-model 12 step 2): only the identity once the frame is a recorded fact.

    The gate ``panel_vertices_usable`` is on only when rung R2 has recorded
    the fact ``panel.vertex_frame = identity`` (safety-model 1.4); U-13's
    default, every decoding tried, holds until then.
    """
    gates = (case.gates or {}).get("gates", {})
    if gates.get("panel_vertices_usable", {}).get("allowed") is True:
        return {"identity": AXIS_MAPS["identity"]}
    return AXIS_MAPS


def hypotheses(case: Any) -> list[tuple[str, str]]:
    """The (encoding, axis map) pairs still allowed under which the raw stream passes safety-model 12 step 3."""
    job = case.job_document()
    W, H = float(job["panel"]["W"]), float(job["panel"]["H"])
    tower_axis = job["panel"]["tower2"]
    tol = float(PARAMS["tol_panel"])
    raw = [tuple(float(v) for v in line.split(";")[2:4]) for line in case.measured["info25"]]
    d27 = float(case.measured["info27"].split(";")[2])
    rect = box(0, 0, W, H)
    passing = []
    for enc_name, enc in ENCODINGS.items():
        for map_name, axis_map in allowed_axis_maps(case).items():
            pts = [axis_map(*enc(a, b)) for a, b in raw]
            if len(pts) < 3:
                continue
            poly = Polygon(pts)
            if not poly.is_valid or poly.area <= 0:
                continue
            if _hausdorff(poly, rect) > tol:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            extent = (max(ys) - min(ys)) if tower_axis == "Y" else (max(xs) - min(xs))
            if abs(d27 - extent) > tol:
                continue
            nearest = min(pts, key=lambda p: math.hypot(*p))
            if math.hypot(*nearest) > tol:
                continue
            passing.append((enc_name, map_name))
    return passing
