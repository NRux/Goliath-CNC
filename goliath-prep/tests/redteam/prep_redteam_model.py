# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Witness derivations for the red-team cases (test code only).

Each red-team case claims that a job must be refused by a named check, with a
witness near a named point. This module re-derives those claims from the
normative text of docs/prep/safety-model.md, with the numbers of
docs/prep/params.json, so that a wrong case is caught now, long before the
pipeline exists. It is deliberately simple: it samples every move at about
1 mm, computes the supports with the footprint of safety-model 4.2, and
compares distances with the margins of safety-model 5 to 9. It uses shapely
(a goliath-prep dependency) only for polygons, unions and connected parts.

It is **not** goliath-prep's checker and not the independent oracle of the
project design (section 10.4). It covers the checks the cases use, at the
desk (the declared panel shrunk by geom_tol), under the gate values a case
states; it does not choose headings (a case forces them with GI ``OP c=``,
or the test enumerates all 36) and it reads the support strips seamlessly
(see :meth:`Scene.strips`).

The derivation reports **violations**: a check, the cut or link, the wheel,
the point (the wheel centre for wheel checks, the tool point for tool
checks) and the sample index, which orders events in time like the piece
index of safety-model 3.6.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union

__all__ = [
    "PARAMS",
    "Cut",
    "Sample",
    "Scene",
    "Violation",
    "support_offsets",
    "rotation_path",
]

REPO = Path(__file__).resolve().parents[3]


def _load_params() -> dict[str, Any]:
    data = json.loads((REPO / "docs" / "prep" / "params.json").read_text(encoding="utf-8"))
    return {p["name"]: p["default"] for p in data}


PARAMS: dict[str, Any] = _load_params()
"""Every parameter's default, from docs/prep/params.json (the normative catalogue)."""

SUPPORTS = (
    (1, PARAMS["support_r1"], PARAMS["support_delta1"]),
    (2, PARAMS["support_r2"], PARAMS["support_delta2"]),
    (3, PARAMS["support_r3"], PARAMS["support_delta3"]),
)
RHO = float(PARAMS["rho"])
STEP = 1.0  # sampling step along a path, mm


def support_offsets(c: float) -> dict[int, tuple[float, float]]:
    """d_k(θ) = −r_k·u(θ + δ_k) with θ = (C + 90) mod 360 (safety-model 4.1, 4.2)."""
    theta = (c + PARAMS["heading_offset"]) % 360.0
    out = {}
    for k, r, delta in SUPPORTS:
        a = math.radians(theta + delta)
        out[k] = (-r * math.cos(a), -r * math.sin(a))
    return out


def rotation_path(c0: int, c1: int, model: str) -> list[float]:
    """The C values a rotation passes through, 1 degree apart, under a rotation model.

    ``in-place-shortest``: the short way. Any other model (``in-place-numeric``,
    or unverified with every step below 180, safety-model 9.2): numerically
    from c0 to c1, which crosses the wrap the long way.
    """
    if model == "in-place-shortest":
        delta = ((c1 - c0 + 180) % 360) - 180
    else:
        delta = c1 - c0
    n = max(1, int(abs(delta)))
    return [c0 + delta * i / n for i in range(n + 1)]


@dataclass(frozen=True)
class Sample:
    """A tool position in time: the sample index orders every event of the program."""

    t: int
    x: float
    y: float
    z: float
    where: str  # 'cut', 'link', 'first'
    cut: int | None
    line: int | None
    c: float | None


@dataclass
class Cut:
    """A cut (safety-model 3.3): from the move that goes below −ε_surf to the move back to z >= 0."""

    index: int
    moves: list[Any]
    c: int | None
    samples: list[Sample] = field(default_factory=list)

    @property
    def start(self) -> tuple[float, float]:
        m = self.moves[0]
        return (m.end[0], m.end[1])

    @property
    def end(self) -> tuple[float, float]:
        m = self.moves[-1]
        return (m.end[0], m.end[1])


@dataclass(frozen=True)
class Violation:
    check: str
    where: str
    cut: int | None
    wheel: int | None
    point: tuple[float, float]
    t: int
    pivot: str | None = None
    rule: str | None = None
    line: int | None = None  # the source line of the piece, for cut violations


def _seg_dist(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


class _SegmentHash:
    """Removal segments in a grid, each with its time and depth, for fast distance queries."""

    CELL = 25.0

    def __init__(self) -> None:
        self.cells: dict[tuple[int, int], list[tuple[int, float, float, float, float, float]]] = {}

    def add(self, t: int, ax: float, ay: float, bx: float, by: float, depth: float) -> None:
        keys = {(math.floor(ax / self.CELL), math.floor(ay / self.CELL)),
                (math.floor(bx / self.CELL), math.floor(by / self.CELL))}
        for key in keys:
            self.cells.setdefault(key, []).append((t, ax, ay, bx, by, depth))

    def nearest(self, x: float, y: float, radius: float, before: int, min_depth: float,
                ignore_below: float = 0.0) -> tuple[float, tuple] | None:
        """The closest segment within ``radius`` added before time ``before``, deeper than ``min_depth``.

        Segments shallower than ``ignore_below`` are left out (the recess depth h_r of R-KERF).
        """
        n = int(math.ceil(radius / self.CELL)) + 1
        cx, cy = math.floor(x / self.CELL), math.floor(y / self.CELL)
        best = None
        for i in range(cx - n, cx + n + 1):
            for j in range(cy - n, cy + n + 1):
                for seg in self.cells.get((i, j), ()):
                    if seg[0] >= before or seg[5] <= min_depth or seg[5] < ignore_below:
                        continue
                    d = _seg_dist(x, y, seg[1], seg[2], seg[3], seg[4])
                    if d < radius and (best is None or d < best[0]):
                        best = (d, seg)
        return best


def sample_move(move: Any, step: float = STEP) -> Iterator[tuple[float, float, float]]:
    """Points along a GI or dialect move, excluding its start, at most ``step`` apart in XY."""
    (sx, sy, sz), (ex, ey, ez) = move.start, move.end
    ez = sz if ez is None else ez
    if move.code in (2, 3) and move.centre is not None:
        cx, cy = move.centre
        r = math.hypot(sx - cx, sy - cy)
        a0 = math.atan2(sy - cy, sx - cx)
        a1 = math.atan2(ey - cy, ex - cx)
        if move.code == 3:
            sweep = (a1 - a0) % (2 * math.pi) or 2 * math.pi
        else:
            sweep = -((a0 - a1) % (2 * math.pi) or 2 * math.pi)
        n = max(1, int(math.ceil(abs(sweep) * r / step)))
        for i in range(1, n + 1):
            a = a0 + sweep * i / n
            yield (cx + r * math.cos(a), cy + r * math.sin(a), sz + (ez - sz) * i / n)
        return
    L = math.hypot(ex - sx, ey - sy)
    n = max(1, int(math.ceil(L / step)))
    for i in range(1, n + 1):
        yield (sx + (ex - sx) * i / n, sy + (ey - sy) * i / n, sz + (ez - sz) * i / n)


class Scene:
    """One case's job, program and gates, at the desk, with forced headings per cut.

    ``job`` is the parsed job.toml; ``moves`` the program's moves with
    absolute start and end; ``headings`` maps a cut index to its C;
    ``gates`` maps a gate name to {"allowed": ..., "value": ...};
    ``park`` is the start pose (x, y, c) or None.
    """

    def __init__(self, job: Mapping[str, Any], moves: Sequence[Any], *, headings: Mapping[int, int] | None = None,
                 gates: Mapping[str, Mapping[str, Any]] | None = None,
                 park: tuple[float, float, int] | None = None,
                 machine: Mapping[str, Any] | None = None, step: float = STEP) -> None:
        self.job = job
        self.step = step
        self.machine = dict(machine or {})
        panel = job["panel"]
        self.W, self.H, self.T = float(panel["W"]), float(panel["H"]), float(panel["T"])
        self.tower2 = panel["tower2"]
        self.r_rem = float(job["tool"]["d"]) / 2 + PARAMS["delta_tool"]
        self.gates = dict(gates or {})
        self.park = park
        self.gt = float(PARAMS["geom_tol"])
        self.eps = float(PARAMS["eps_surf"])
        self.t_hold = self._t_hold()
        self.recess_ignore = self._recess_ignore()
        self.cuts = self._segment([m for m in moves if m.end[0] is not None and m.end[1] is not None
                                   and m.start[0] is not None and m.start[1] is not None])
        for cut in self.cuts:
            if headings is not None and cut.index in headings:
                cut.c = headings[cut.index]
        self._removal = _SegmentHash()
        self._t = 0
        self._sample_cuts()

    # -- gates ---------------------------------------------------------------
    def gate(self, name: str) -> bool:
        return self.gates.get(name, {}).get("allowed") is True

    def gate_value(self, name: str) -> Any:
        return self.gates.get(name, {}).get("value")

    def _t_hold(self) -> float:
        if self.gate("tabs_hold"):
            return float(self.gate_value("tabs_hold"))
        return self.T / 2  # params.json t_hold: formula T / 2

    def _recess_ignore(self) -> float:
        """h_r of R-KERF (safety-model 7.5): recesses shallower than it are no wheel keep-out; 0 while closed."""
        if self.gate("kerf_crossing"):
            return float(self.gate_value("kerf_crossing")["recess_ignore_max"])
        return float(PARAMS["recess_ignore_max"])

    @property
    def rotation_model(self) -> str | None:
        return self.gate_value("rotation_model") if self.gate("rotation_model") else None

    # -- geometry of the desk ------------------------------------------------
    def p_work(self) -> Polygon:
        return box(self.gt, self.gt, self.W - self.gt, self.H - self.gt)

    def strips(self) -> list[Polygon]:
        """Declared support strips (R-SUPPORT), read seamlessly: each overlaps P_work by geom_tol."""
        sup = self.job.get("supports", {})
        out = []
        gt = self.gt
        if sup.get("left"):
            out.append(box(-float(sup["left"]), 0.0, gt, self.H))
        if sup.get("right"):
            out.append(box(self.W - gt, 0.0, self.W + float(sup["right"]), self.H))
        if sup.get("bottom"):
            out.append(box(0.0, -float(sup["bottom"]), self.W, gt))
        if sup.get("top"):
            out.append(box(0.0, self.H - gt, self.W, self.H + float(sup["top"])))
        return out

    def wheel_area(self) -> Any:
        """The region where a wheel centre keeps its disc inside WA_wheel (safety-model 6.2), edge margin 50 mm."""
        m = float(PARAMS["m_edge"])
        return unary_union([self.p_work(), *self.strips()]).buffer(-(m + RHO), join_style="mitre")

    def tool_area(self) -> Polygon:
        """Where the tool centre keeps its disc inside WA_tool (safety-model 6.3)."""
        inset = self.gt + PARAMS["m_edge_tool"] + self.r_rem
        return box(inset, inset, self.W - inset, self.H - inset)

    def tower_points(self) -> dict[str, tuple[float, float]]:
        t2 = (0.0, self.H) if self.tower2 == "Y" else (self.W, 0.0)
        return {"tower1": (0.0, 0.0), "tower2": t2}

    def zone(self, leg: float, which: str) -> Polygon:
        """Z1(L), Z2(L) of safety-model 5.1, tower 2 at its corner (the desk placement)."""
        if which == "tower1":
            return Polygon([(0, 0), (0, leg), (leg, 0)])
        if self.tower2 == "Y":
            return Polygon([(0, self.H - leg), (0, self.H), (leg, self.H)])
        return Polygon([(self.W - leg, 0), (self.W, 0), (self.W, leg)])

    def anchors(self) -> tuple[Point, Point]:
        """a1 and a2: the centroids of Z1(150) and Z2(150) (safety-model 5.3)."""
        leg = float(PARAMS["tower_zone_leg_wheel"])
        return self.zone(leg, "tower1").centroid, self.zone(leg, "tower2").centroid

    # -- program -------------------------------------------------------------
    def _segment(self, moves: Sequence[Any]) -> list[Cut]:
        cuts: list[Cut] = []
        current: list[Any] | None = None
        forced: int | None = None
        for m in moves:
            z_end = m.end[2] if m.end[2] is not None else m.start[2]
            if current is None:
                if z_end is not None and z_end < -self.eps:
                    current = [m]
                    forced = getattr(m, "forced_c", None)
                    if forced is None:
                        forced = getattr(m, "c", None)
            else:
                current.append(m)
                if z_end is not None and z_end >= 0:
                    cuts.append(Cut(len(cuts), current, forced))
                    current = None
        if current is not None:
            cuts.append(Cut(len(cuts), current, forced))
        return cuts

    def _tick(self) -> int:
        self._t += 1
        return self._t

    def _sample_cuts(self) -> None:
        """Cut samples get time indices with gaps for the first line and the links in between."""
        self._t = 100000  # the first line lives below this
        for cut in self.cuts:
            first = cut.moves[0]
            sx, sy, sz = first.start
            cut.samples.append(Sample(self._tick(), sx, sy, sz if sz is not None else 0.0, "cut", cut.index,
                                      first.line, cut.c))
            for m in cut.moves:
                for x, y, z in sample_move(m, self.step):
                    cut.samples.append(Sample(self._tick(), x, y, z, "cut", cut.index, m.line, cut.c))
            self._t += 100000  # room for the link after this cut
        for cut in self.cuts:
            prev = None
            for s in cut.samples:
                if prev is not None:
                    depth = max(-prev.z, -s.z)
                    if depth > self.eps:
                        self._removal.add(s.t, prev.x, prev.y, s.x, s.y, depth)
                prev = s

    # -- removal and structure ----------------------------------------------
    def prior_removal(self) -> list[tuple[Polygon, float]]:
        return [(Polygon(p["polygon"]), float(p["depth_mm"])) for p in self.job.get("prior_removal", [])]

    def removal_union(self, before: int, min_depth: float) -> Any:
        """The removal of every sample before ``before`` deeper than ``min_depth``, prior removal included."""
        parts = [poly for poly, depth in self.prior_removal() if depth > min_depth]
        for cut in self.cuts:
            prev = None
            line: list[tuple[float, float]] = []
            for s in cut.samples:
                if s.t >= before:
                    break
                deep = prev is not None and max(-prev.z, -s.z) > min_depth
                if deep:
                    if not line:
                        line.append((prev.x, prev.y))
                    line.append((s.x, s.y))
                elif line:
                    parts.append(LineString(line).buffer(self.r_rem) if len(line) > 1 else
                                 Point(line[0]).buffer(self.r_rem))
                    line = []
                prev = s
            if line:
                parts.append(LineString(line).buffer(self.r_rem) if len(line) > 1 else
                             Point(line[0]).buffer(self.r_rem))
        # Z-only pieces (plunges, tab steps) have no XY length: each removes a disc at its deepest depth.
        for cut in self.cuts:
            for a, b in zip(cut.samples, cut.samples[1:]):
                if b.t < before and a.x == b.x and a.y == b.y and max(-a.z, -b.z) > min_depth:
                    parts.append(Point(b.x, b.y).buffer(self.r_rem))
        return unary_union(parts) if parts else Polygon()

    def structure(self, before: int) -> tuple[Any, Any, bool]:
        """(anchor, detached, towers_separated) from removal before ``before`` (safety-model 7.4)."""
        hold = self.removal_union(before, self.T - self.t_hold)
        material = self.p_work().difference(hold)
        a1, a2 = self.anchors()
        parts = list(material.geoms) if isinstance(material, MultiPolygon) else [material]
        anchor = next((p for p in parts if p.contains(a1)), Polygon())
        separated = not anchor.contains(a2)
        remaining = self.p_work().difference(self.removal_union(before, self.eps))
        detached = remaining.difference(anchor)
        return anchor, detached, separated

    # -- the robot's body ----------------------------------------------------------
    def tower_base_radius(self) -> float:
        """The measured base radius of a machine.toml [tower] (all three readings), else the 120 mm default."""
        tower = self.machine.get("tower", {})
        if all(isinstance(tower.get(k), (int, float)) for k in ("base_radius_mm", "base_length_mm", "base_width_mm")):
            return float(tower["base_radius_mm"])
        return float(PARAMS["tower_base_radius"])

    def body_hull(self, x: float, y: float, c: float) -> Any:
        """The body hull for the tool at (x, y) and heading C (safety-model 4.5).

        Default: the convex hull of the wheel discs grown by m_body. With a
        machine.toml [body] giving all four extents: the convex hull of the
        wheel discs and the box of those extents (ahead of T, behind T, left and
        right of the body axis).
        """
        offsets = support_offsets(c)
        body = self.machine.get("body", {})
        keys = ("ahead_of_tool_mm", "behind_tool_mm", "left_of_axis_mm", "right_of_axis_mm")
        if all(isinstance(body.get(k), (int, float)) for k in keys):
            theta = math.radians((c + PARAMS["heading_offset"]) % 360.0)
            fwd = (math.cos(theta), math.sin(theta))
            right = (fwd[1], -fwd[0])
            a, b, left, r = (float(body[k]) for k in keys)
            corners = [(x + fwd[0] * f + right[0] * s, y + fwd[1] * f + right[1] * s)
                       for f, s in ((a, r), (a, -left), (-b, -left), (-b, r))]
            discs = [Point(x + dx, y + dy).buffer(RHO) for dx, dy in offsets.values()]
            return unary_union([*discs, Polygon(corners)]).convex_hull
        centres = [(x + dx, y + dy) for dx, dy in offsets.values()]
        return Polygon(centres).convex_hull.buffer(RHO + PARAMS["body_hull_margin"])

    # -- per-position checks --------------------------------------------------
    def wheel_checks(self, x: float, y: float, c: float, t: int, *, area: Any = None,
                     detached: Any = None) -> list[tuple[str, int, tuple[float, float]]]:
        """Wheel and body checks for the tool at (x, y) with heading C at time t: (check, wheel, wheel centre)."""
        out = []
        area = self.wheel_area() if area is None else area
        r_surf = RHO + self.r_rem
        tower_margin = RHO + PARAMS["m_tower"] + self.gt
        zones = [self.zone(float(PARAMS["tower_zone_leg_wheel"]), w) for w in ("tower1", "tower2")]
        obstacles = [Polygon(o["polygon"]) for o in self.job.get("obstacles", [])]
        fixations = [Point(f["point"]) for f in self.job.get("fixations", [])]
        prior = self.prior_removal()
        centres = {}
        for k, (dx, dy) in support_offsets(c).items():
            wx, wy = x + dx, y + dy
            centres[k] = (wx, wy)
            p = Point(wx, wy)
            if not area.contains(p):
                out.append(("W-AREA", k, (wx, wy)))
            if any(z.distance(p) < tower_margin for z in zones):
                out.append(("W-TOWER", k, (wx, wy)))
            if self._removal.nearest(wx, wy, r_surf, t, self.eps, self.recess_ignore) is not None or \
                    any(poly.distance(p) < RHO for poly, depth in prior
                        if depth > self.eps and depth >= self.recess_ignore):
                out.append(("W-SURF", k, (wx, wy)))
            if any(o.distance(p) < RHO for o in obstacles) or any(f.distance(p) < RHO for f in fixations):
                out.append(("W-OBST", k, (wx, wy)))
            if detached is not None and not detached.is_empty and detached.distance(p) < RHO:
                out.append(("M-ATTACH", k, (wx, wy)))
        hull = self.body_hull(x, y, c)
        base = self.tower_base_radius()
        for tx, ty in self.tower_points().values():
            if hull.distance(Point(tx, ty)) < base:
                out.append(("B-TOWER", 0, (tx, ty)))
            far = max(hull.exterior.coords, key=lambda q: math.hypot(q[0] - tx, q[1] - ty))
            if math.hypot(far[0] - tx, far[1] - ty) > PARAMS["reach_max"]:
                out.append(("P-REACH", 0, (far[0], far[1])))  # the hull point beyond reach
        if any(hull.intersects(o) for o in obstacles) or any(hull.intersects(f) for f in fixations):
            out.append(("B-OBST", 0, (x, y)))
        return out

    def tool_checks(self, s: Sample, *, detached: Any = None) -> list[tuple[str, tuple[float, float]]]:
        out = []
        p = Point(s.x, s.y)
        in_material = s.z < -self.eps
        if in_material and not self.tool_area().contains(p):
            out.append(("T-AREA", (s.x, s.y)))
        tool_margin = self.r_rem + PARAMS["m_tower"] + self.gt
        for which, (tx, ty) in self.tower_points().items():
            if self.zone(float(PARAMS["tower_zone_leg_tool"]), which).distance(p) < tool_margin or \
                    math.hypot(s.x - tx, s.y - ty) < self.r_rem + self.tower_base_radius():
                out.append(("T-TOWER", (s.x, s.y)))
        flute = float(self.job["tool"]["flute"])
        if s.z < -(self.T + PARAMS["overcut_max"]) or -s.z > flute or s.z > PARAMS["z_upper_limit"]:
            out.append(("T-DEPTH", (s.x, s.y)))
        for f in self.job.get("fixations", []):
            if Point(f["point"]).distance(p) < self.r_rem and \
                    s.z < float(f["height_mm"]) + PARAMS["obstacle_clearance"]:
                out.append(("T-OBST", (s.x, s.y)))
        for o in self.job.get("obstacles", []):
            if Polygon(o["polygon"]).distance(p) < self.r_rem and \
                    s.z < float(o["height_mm"]) + PARAMS["obstacle_clearance"]:
                out.append(("T-OBST", (s.x, s.y)))
        if in_material and detached is not None and not detached.is_empty and \
                detached.intersects(p.buffer(self.r_rem - PARAMS["attach_tol"])):
            out.append(("T-ATTACH", (s.x, s.y)))
        return out

    # -- program-level derivations --------------------------------------------
    def detached_states(self, times: Iterable[int]) -> list[tuple[int, Any]]:
        """(time, detached material from that time on), for :meth:`cut_violations`; independent of headings."""
        return [(t, self.structure(t)[1]) for t in sorted(times)]

    def cut_violations(self, *, structure_times: Iterable[int] | None = None,
                       cuts: Iterable[int] | None = None,
                       states: list[tuple[int, Any]] | None = None) -> list[Violation]:
        """Every wheel, body and tool violation during the cuts, at their forced headings."""
        area = self.wheel_area()
        if states is None:
            states = self.detached_states(structure_times or [])
        wanted = None if cuts is None else set(cuts)
        out: list[Violation] = []
        for cut in self.cuts:
            if wanted is not None and cut.index not in wanted:
                continue
            if cut.c is None:
                raise ValueError("cut %d has no heading: force it with OP c= or pass headings" % cut.index)
            for s in cut.samples:
                detached = None
                for t, d in states:
                    if t <= s.t:
                        detached = d
                for check, k, pt in self.wheel_checks(s.x, s.y, cut.c, s.t, area=area, detached=detached):
                    out.append(Violation(check, "cut", cut.index, k or None, pt, s.t, line=s.line))
                for check, pt in self.tool_checks(s, detached=detached):
                    out.append(Violation(check, "cut", cut.index, None, pt, s.t, line=s.line))
        return out

    def link_violations(self, k: int, pivot: str, *, detached: Any = None) -> list[Violation]:
        """The link from cut k to cut k+1 with its rotation at pivot 'a' (end of k) or 'b' (start of k+1)."""
        a_cut, b_cut = self.cuts[k], self.cuts[k + 1]
        ax, ay = a_cut.end
        bx, by = b_cut.start
        t0 = a_cut.samples[-1].t + 1
        area = self.wheel_area()
        out: list[Violation] = []
        model = self.rotation_model or "unverified"
        if pivot == "a":
            steps = [(ax, ay, c) for c in rotation_path(a_cut.c, b_cut.c, model)]
            line_c = b_cut.c
        else:
            steps = []
            line_c = a_cut.c
        L = math.hypot(bx - ax, by - ay)
        n = max(1, int(math.ceil(L / STEP)))
        trans = [(ax + (bx - ax) * i / n, ay + (by - ay) * i / n, line_c) for i in range(n + 1)]
        if pivot == "b":
            steps_b = [(bx, by, c) for c in rotation_path(a_cut.c, b_cut.c, model)]
            seq = [("translate", p) for p in trans] + [("rotate", p) for p in steps_b]
        else:
            seq = [("rotate", p) for p in steps] + [("translate", p) for p in trans]
        if self.rotation_model is None and a_cut.c != b_cut.c:
            # safety-model 9.3: until the rotation model is a fact, a rotation step is the full disc.
            px, py = (ax, ay) if pivot == "a" else (bx, by)
            radius = PARAMS["r_rot"] + RHO
            disc = Point(px, py).buffer(radius)
            if not area.buffer(RHO).contains(disc):
                out.append(Violation("W-ROT", "link", k, None, (px, py), t0, pivot, "W-AREA"))
            hit = self._removal.nearest(px, py, radius + self.r_rem, t0, self.eps, self.recess_ignore)
            if hit is not None:
                seg = hit[1]
                q = LineString([(seg[1], seg[2]), (seg[3], seg[4])]).interpolate(
                    LineString([(seg[1], seg[2]), (seg[3], seg[4])]).project(Point(px, py)))
                out.append(Violation("W-ROT", "link", k, None, (q.x, q.y), t0, pivot, "W-SURF"))
            seq = [item for item in seq if item[0] == "translate"]
        for i, (kind, (x, y, c)) in enumerate(seq):
            name = "W-ROT" if kind == "rotate" else "W-RAPID"
            for check, wheel, pt in self.wheel_checks(x, y, c, t0 + i, area=area, detached=detached):
                if check == "P-REACH":  # its own check on link pieces too (safety-model 6.4)
                    out.append(Violation(check, "link", k, None, pt, t0 + i, pivot))
                else:
                    out.append(Violation(name, "link", k, wheel or None, pt, t0 + i, pivot, check))
            s = Sample(t0 + i, x, y, PARAMS["z_safety"], "link", None, None, c)
            for check, pt in self.tool_checks(s):
                out.append(Violation(check, "link", k, None, pt, t0 + i, pivot))
        return out

    def combined_envelope(self, k: int) -> dict[int, Any]:
        """safety-model 9.4 case 2 for the link k -> k+1 as one combined line, in the numeric direction only.

        E_k = conv({p_a, p_b} ⊕ arc_k) ⊕ D(ρ): the narrowest reading; any wider reading contains it.
        """
        a_cut, b_cut = self.cuts[k], self.cuts[k + 1]
        pa, pb = a_cut.end, b_cut.start
        out = {}
        path = rotation_path(a_cut.c, b_cut.c, "in-place-numeric")
        for wheel in (1, 2, 3):
            pts = []
            for c in path:
                dx, dy = support_offsets(c)[wheel]
                pts += [(pa[0] + dx, pa[1] + dy), (pb[0] + dx, pb[1] + dy)]
            out[wheel] = Polygon(pts).convex_hull.buffer(RHO)
        return out

    def first_line_violations(self) -> list[Violation]:
        """The program's first line from the start pose to the first cut (safety-model 9.5)."""
        if self.park is None:
            return [Violation("W-FIRST", "first", None, None, (0.0, 0.0), 0, rule="no start pose")]
        px, py, pc = self.park
        tx, ty = self.cuts[0].start
        tc = self.cuts[0].c
        out: list[Violation] = []
        tol = float(PARAMS["park_pose_tol"])
        if not (self.gate("heading_map_verified") and self.gate("rotation_model")):
            radius = PARAMS["r_rot"] + RHO
            env = unary_union([Point(px, py).buffer(radius + tol), Point(tx, ty).buffer(radius),
                               LineString([(px, py), (tx, ty)]).buffer(radius)])
            if not self.wheel_area().buffer(RHO).contains(env):
                out.append(Violation("W-FIRST", "first", None, None, (px, py), 0, rule="W-AREA"))
            return out
        area = self.wheel_area()
        tol_deg = float(PARAMS["park_pose_tol_deg"])
        L = math.hypot(tx - px, ty - py)
        n = max(1, int(math.ceil(L / STEP)))
        for dc in (-tol_deg, 0.0, tol_deg):
            for ox, oy in [(0, 0)] + [(tol * math.cos(math.radians(a)), tol * math.sin(math.radians(a)))
                                      for a in range(0, 360, 45)]:
                for i in range(n + 1):
                    f = i / n
                    x = px + ox * (1 - f) + (tx - px) * f
                    y = py + oy * (1 - f) + (ty - py) * f
                    for check, wheel, pt in self.wheel_checks(x, y, pc + dc, 1 + i, area=area):
                        name = "P-REACH" if check == "P-REACH" else "W-FIRST"
                        out.append(Violation(name, "first", None, wheel or None, pt, 1 + i, rule=check))
        for c in rotation_path(pc, tc, self.rotation_model or "unverified"):
            for check, wheel, pt in self.wheel_checks(tx, ty, c, n + 2, area=area):
                name = "P-REACH" if check == "P-REACH" else "W-FIRST"
                out.append(Violation(name, "first", None, wheel or None, pt, n + 2, rule=check))
        return out

