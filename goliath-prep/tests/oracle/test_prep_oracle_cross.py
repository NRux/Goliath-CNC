# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Anything goliath-prep's checker accepts, the oracle accepts (the project design, section 10.4; unit P21).

The oracle samples where the checker computes exactly, so its violations are
real ones: a violation it finds at a sample is inside the region the checker
must keep clear. A job the checker certifies and the oracle refuses is
therefore a checker defect (or an oracle defect): either way a disagreement
to minimise and keep as a regression case (add hypothesis's minimal example as
an ``@example`` below).

Two comparisons per accepted job:

* the job as planned: the oracle searches the headings itself (every heading
  of an unforced cut, the links between cuts with either pivot) and must find
  the job acceptable;
* the job as shipped: the upload bytes the checker certified, parsed back in
  Goliath-dialect verify mode, every heading and link as written, must pass
  the oracle too ("check what you ship", safety-model 11.5).

The checker is goliath-prep's pipeline, ``goliath_prep.api.prepare`` (unit
P17). Until it exists these tests skip with that reason; they run as soon as
it does.
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

SETTINGS = settings(max_examples=40, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])


def _require_pipeline() -> None:
    if importlib.util.find_spec("goliath_prep.api") is None:
        pytest.skip("goliath-prep's pipeline goliath_prep.api.prepare (unit P17) does not exist yet: the cross-check "
                    "runs on it as soon as it does")


GATE_PROFILES: dict[str, dict[str, Any]] = {
    "rung R8": {},
    "heading map unverified (the any-heading safe zone)": {"heading_map_verified": False},
    "rotation model unverified (full-disc rotations)": {"rotation_model": False},
}
"""The gate sets the cross-check draws from: the red-team suite's rung-R8 baseline, and the same robot with one of
the two rung-R3 facts withheld, so that the safe zone and the full-disc rotations are cross-checked too."""


@st.composite
def jobs(draw: Any) -> tuple[Draft, str]:
    """A few grooves or small closed profiles, through cuts among them (so attached and detached material is
    cross-checked too), and one of the gate sets above."""
    count = draw(st.integers(1, 3))
    ops = []
    for _ in range(count):
        x, y = draw(st.integers(560, 1880)), draw(st.integers(440, 780))
        c = draw(st.one_of(st.none(), st.sampled_from(list(range(0, 360, 10)))))
        depth = draw(st.sampled_from([0.5, 2.0, 6.0, 18.3]))
        if draw(st.booleans()):
            path = [("G1", x + draw(st.integers(-150, 150)), y + draw(st.integers(-150, 150)))]
        else:
            w, h = draw(st.integers(20, 110)), draw(st.integers(20, 110))
            path = [("G1", x + w, y), ("G1", x + w, y + h), ("G1", x, y + h), ("G1", x, y)]
        ops.append(Op((x, y), path, depth=depth, c=c, through=depth >= 18.0))
    return Draft(ops=ops), draw(st.sampled_from(sorted(GATE_PROFILES)))


def _gates(draft: Draft, profile: str = "rung R8") -> dict[str, Any]:
    x, y = draft.ops[0].start
    return front.gates_r8(start=(x, y, draft.ops[0].c if draft.ops[0].c is not None else 0),
                          **GATE_PROFILES[profile])


@SETTINGS
@given(job=jobs())
def test_checker_accept_implies_oracle_accept(tmp_path_factory: Any, job: tuple[Draft, str]) -> None:
    _require_pipeline()
    draft, profile = job
    tmp = tmp_path_factory.mktemp("cross")
    gates = _gates(draft, profile)
    result = front.run_pipeline(draft, tmp, gates=gates)
    if not result.ok:
        return
    planned = front.check_draft(draft, tmp, gates=gates)
    assert planned.ok, "the checker certified a job the oracle refuses as planned (%s):\n%s" % (profile, "\n".join(
        h.describe() for h in planned.violations[:20]))
    shipped = front.check_upload(front.upload_bytes(result), draft, tmp, gates=gates)
    assert shipped.ok, "the checker certified upload bytes the oracle refuses as shipped (%s):\n%s" % (
        profile, "\n".join(h.describe() for h in shipped.violations[:20]))


def test_cross_check_is_not_vacuous(tmp_path: Path) -> None:
    # The property above passes vacuously if the pipeline refuses every job hypothesis draws. A short groove in
    # the middle of the panel, which the oracle accepts (test_prep_oracle_model), is a job the safety model
    # certifies under the rung-R8 gates: the pipeline must certify it, and its upload must pass the oracle.
    _require_pipeline()
    draft = Draft(ops=[Op((1200, 600), [("G1", 1200, 680)], depth=2, c=0)])
    gates = front.gates_r8(start=(1200, 640, 0))
    planned = front.check_draft(draft, tmp_path, gates=gates)
    assert planned.ok, "\n".join(h.describe() for h in planned.violations[:20])
    result = front.run_pipeline(draft, tmp_path, gates=gates)
    assert result.ok, "the pipeline refuses the cross-check's positive control, so the cross-check is vacuous:\n%s" % (
        "\n".join(str(f) for f in list(result.findings)[:20]))
    shipped = front.check_upload(front.upload_bytes(result), draft, tmp_path, gates=gates)
    assert shipped.ok, "\n".join(h.describe() for h in shipped.violations[:20])


def test_cross_check_skips_only_while_the_pipeline_is_missing() -> None:
    # A visible guard: the skip above must name unit P17 and disappear once goliath_prep.api exists.
    if importlib.util.find_spec("goliath_prep.api") is None:
        with pytest.raises(pytest.skip.Exception, match="P17"):
            _require_pipeline()
    else:
        _require_pipeline()
