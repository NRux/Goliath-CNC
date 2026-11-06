# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Monotonicity in material and refinement invariance (the project design, section 10.2, properties 1 and 3).

The design hosts these properties with the oracle (unit P21). They are stated
for a verdict function: the oracle's, which exists now, and goliath-prep's
pipeline (``goliath_prep.api.prepare``, unit P17), whose runs skip, naming the
unit, until it exists and run as soon as it does.

1. **Monotonicity in material**: extra removal before a cut never turns a
   refusal into "ready". The extra removal is declared prior removal in
   job.toml. One check is excluded, visibly: T-ATTACH, which more removal can
   lift by removing the loose part the tool would have cut (a design issue
   pinned in test_prep_oracle_model.test_t_attach_is_not_monotone_in_material).
2. **Refinement invariance**: splitting a move into collinear pieces never
   turns a refusal into "ready"; for the oracle, every violation of the
   unsplit program is found again at the same check. Linearising an arc more
   finely moves the path by less than its sagitta, so a sampling checker may
   change only within that band: every violation deeper than
   ``ARC_BAND_MM`` survives the refinement, in both directions.

Hypothesis's example database is not used (the suites run with
``-p no:cacheprovider``). A disagreement hypothesis finds is minimised by it;
add the minimal example as an ``@example`` to the test, so it stays a
regression case.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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
Op = front.Op
Draft = front.Draft

ARC_BAND_MM = 1.5
"""How far a violation may move when an arc is linearised more finely: one sample step plus the sagitta, with room."""

SETTINGS = settings(max_examples=25, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])

VERDICTS = ["oracle", "pipeline"]


def _verdict_function(which: str) -> Any:
    if which == "oracle":
        return front.check_draft
    if importlib.util.find_spec("goliath_prep.api") is None:
        pytest.skip("goliath-prep's pipeline goliath_prep.api.prepare (unit P17) does not exist yet: this property "
                    "runs on it as soon as it does")
    return front.pipeline_check_draft


# ---------------------------------------------------------------------------
# Random jobs: a few short grooves or small closed profiles with forced headings
# ---------------------------------------------------------------------------

coord_x = st.integers(min_value=600, max_value=1840)
coord_y = st.integers(min_value=450, max_value=770)
headings = st.sampled_from([0, 40, 90, 150, 200, 270, 330])
depths = st.sampled_from([1.0, 3.0, 10.0, 18.3])


@st.composite
def ops(draw: Any) -> Op:
    x, y = draw(coord_x), draw(coord_y)
    c = draw(headings)
    depth = draw(depths)
    if draw(st.booleans()):
        dx, dy = draw(st.integers(-160, 160)), draw(st.integers(-160, 160))
        path = [("G1", x + dx, y + dy)]
    else:
        w, h = draw(st.integers(20, 120)), draw(st.integers(20, 120))
        path = [("G1", x + w, y), ("G1", x + w, y + h), ("G1", x, y + h), ("G1", x, y)]
    return Op((x, y), path, depth=depth, c=c, through=depth >= 18.0)


@st.composite
def drafts(draw: Any) -> Draft:
    return Draft(ops=draw(st.lists(ops(), min_size=1, max_size=2)))


@st.composite
def removals(draw: Any) -> tuple[list[list[float]], float]:
    x, y = draw(st.integers(300, 2100)), draw(st.integers(150, 1050))
    w, h = draw(st.integers(4, 200)), draw(st.integers(4, 200))
    depth = draw(st.sampled_from([0.5, 3.0, 18.0]))
    return [[float(x), float(y)], [float(x + w), float(y)], [float(x + w), float(y + h)], [float(x), float(y + h)]], \
        depth


def _gates(draft: Draft) -> dict[str, Any]:
    x, y = draft.ops[0].start
    return front.gates_r8(start=(x, y, draft.ops[0].c))


def _refusing(verdict: Any) -> set[str]:
    return {h.check for h in verdict.violations} - {"T-ATTACH"}


@pytest.mark.parametrize("which", VERDICTS)
@SETTINGS
@given(draft=drafts(), extra=st.lists(removals(), min_size=1, max_size=2))
def test_monotonicity_in_material(tmp_path_factory: Any, which: str, draft: Draft, extra: list) -> None:
    check = _verdict_function(which)
    tmp = tmp_path_factory.mktemp("mono")
    base = check(draft, tmp, gates=_gates(draft))
    more = check(Draft(ops=draft.ops, prior=list(draft.prior) + list(extra)), tmp, gates=_gates(draft))
    if _refusing(base):
        assert not more.ok, "extra removal %r turned the refusal %s into ready" % (extra, sorted(_refusing(base)))


def test_monotonicity_positive_control(tmp_path: Path) -> None:
    # A groove the oracle accepts, and the same groove with a pocket declared under its rear wheel: refused.
    draft = Draft(ops=[Op((1200, 600), [("G1", 1200, 680)], depth=2, c=0)])
    gates = front.gates_r8(start=(1200, 640, 0))
    assert front.check_draft(draft, tmp_path, gates=gates).ok
    pocket = [[1180.0, 180.0], [1220.0, 180.0], [1220.0, 220.0], [1180.0, 220.0]]
    more = front.check_draft(Draft(ops=draft.ops, prior=[(pocket, 3.0)]), tmp_path, gates=gates)
    assert "W-SURF" in more.check_ids()


def _split(draft: Draft, parts: int) -> Draft:
    """Every straight G1 of the draft cut into ``parts`` collinear pieces (the same path)."""
    new_ops = []
    for op in draft.ops:
        path, (px, py) = [], op.start
        for step in op.path:
            if step[0] == "G1":
                _, x, y = step
                for k in range(1, parts + 1):
                    path.append(("G1", px + (x - px) * k / parts, py + (y - py) * k / parts))
                px, py = x, y
            else:
                path.append(step)
                px, py = step[1], step[2]
        new_ops.append(Op(op.start, path, depth=op.depth, c=op.c, through=op.through, kind=op.kind))
    return Draft(W=draft.W, H=draft.H, T=draft.T, ops=new_ops, prior=draft.prior)


GRID_CHECKS = frozenset({"M-ATTACH", "T-ATTACH", "M-TOWERSEP"})
"""Checks the oracle decides on its 2 mm grid: their violations carry no depth, so the arc comparison, which
allows a band of movement, leaves them out (the split comparison keeps them)."""

SPLIT_BAND_MM = 0.01
"""The GI file holds coordinates to 0.001 mm, so a split point may sit up to 0.0005 mm off the line."""


def _hits(verdict: Any, deeper_than: float) -> set[tuple[str, str | None]]:
    """The (check, rule) pairs of the violations deeper than ``deeper_than``, and of those without a depth
    (structure on the grid, gates, program rules)."""
    return {(h.check, h.rule) for h in verdict.violations
            if h.depth_mm is None or h.depth_mm > deeper_than}


@st.composite
def splits(draw: Any) -> tuple[str, Draft, Draft]:
    """A job and the same job with every straight move cut into collinear pieces."""
    draft = draw(drafts())
    return "split", draft, _split(draft, draw(st.integers(min_value=2, max_value=7)))


@st.composite
def arcs(draw: Any) -> tuple[str, Draft, Draft, Draft]:
    """One arc groove three ways: as a G2/G3 arc, linearised coarsely, and linearised finely (forced heading)."""
    cx, cy = draw(st.integers(800, 1640)), draw(st.integers(500, 720))
    r = draw(st.integers(15, 140))
    a0 = draw(st.integers(0, 359))
    sweep = draw(st.integers(20, 300))
    ccw = draw(st.booleans())
    c = draw(headings)
    depth = draw(depths)
    return ("arcs", front.arc_draft(cx, cy, r, a0, sweep, ccw, c, depth, chords=None),
            front.arc_draft(cx, cy, r, a0, sweep, ccw, c, depth, chords=0.2),
            front.arc_draft(cx, cy, r, a0, sweep, ccw, c, depth, chords=0.01))


def _deep(verdict: Any, band: float) -> set[tuple[str, str | None]]:
    return {hit for hit in _hits(verdict, band) if hit[0] not in GRID_CHECKS}


@pytest.mark.parametrize("which", VERDICTS)
@settings(SETTINGS, max_examples=40)
@given(job=st.one_of(splits(), arcs()))
def test_refinement_invariance_split_moves_and_finer_arcs(tmp_path_factory: Any, which: str, job: tuple) -> None:
    check = _verdict_function(which)
    tmp = tmp_path_factory.mktemp("refine")
    if job[0] == "split":
        _, draft, split_draft = job
        base = check(draft, tmp, gates=_gates(draft))
        split = check(split_draft, tmp, gates=_gates(draft))
        if not base.ok:
            assert not split.ok, "splitting the moves into collinear pieces turned a refusal into ready"
        if which == "oracle":
            missing = _hits(base, SPLIT_BAND_MM) - _hits(split, 0.0)
            assert not missing, "the split program lost the violations %s" % sorted(missing)
        return
    _, *versions = job
    arc, coarse, fine = (check(d, tmp, gates=_gates(d)) for d in versions)
    for a, b, what in ((arc, fine, "the arc and its fine linearisation"),
                       (coarse, fine, "a coarse and a finer linearisation")):
        for x, y in ((a, b), (b, a)):
            if _deep(x, ARC_BAND_MM):
                assert not y.ok, "%s: one is refused by %s deeper than %.1f mm, the other is ready" % (
                    what, sorted(_deep(x, ARC_BAND_MM)), ARC_BAND_MM)
            if which == "oracle":
                lost = _deep(x, ARC_BAND_MM) - _hits(y, 0.0)
                assert not lost, "%s disagree on violations deeper than %.1f mm: %s" % (what, ARC_BAND_MM,
                                                                                       sorted(lost))


def test_refinement_positive_control(tmp_path: Path) -> None:
    # An arc whose bulge leaves the tool work area (its lowest point at y = 10 mm) while both ends are well
    # inside: the arc and every linearisation of it are refused by T-AREA.
    for chords in (None, 0.2, 0.01):
        draft = front.arc_draft(1200.0, 160.0, 150.0, 0.0, 180.0, False, 180, 1.0, chords=chords)
        verdict = front.check_draft(draft, tmp_path, gates=_gates(draft))
        assert "T-AREA" in verdict.check_ids(), chords
    split = front.check_draft(_split(Draft(ops=[Op((1200, 600), [("G1", 1200, 10)], depth=1.0, c=0)]), 3),
                              tmp_path, gates=front.gates_r8(start=(1200, 600, 0)))
    assert "T-AREA" in split.check_ids()
