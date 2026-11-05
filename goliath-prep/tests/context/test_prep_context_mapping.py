# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The facts-to-profile mapping is total (unit P22; red-team C-03).

For every combination of the gates the context builder reads, open, closed or
uncertain, with every value an open gate can carry, ``build_context`` gives a
valid context and ``emit_profile_for`` a profile that obeys the design's rules:

* ``split`` rotation lines only while the any-heading safe zone applies or
  when ``rotate_in_place_ok`` is a fact, and never under the fact
  ``rotation.model = during-translation``;
* ``combined`` whenever ``rotation.model`` is ``during-translation``;
* a ``translate-then-rotate`` first line only once the heading map, the
  rotation model, rotate-in-place and the park heading are all facts;
* an uncertain gate (``allowed`` None) counts exactly as a closed one: no
  unknown is read as "yes".
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from prep_context_support import PARK, Gates, job, measured, profile

from goliath_prep.context import (
    build_context,
    emit_profile_for,
    rotation_model_for,
    safe_zone_applies,
    split_rotation_allowed,
)
from goliath_prep.core.params import Param
from goliath_prep.core.types import CheckContext, EmitProfile, Pose

MODELS = ("in-place-shortest", "in-place-numeric", "during-translation", "about-centroid")
EDGES = ("x_min", "x_max", "y_min", "y_max")
STATES = st.sampled_from((True, False, None))

PROFILE = profile()
JOB = job()


@st.composite
def gate_views(draw: st.DrawFn) -> Gates:
    """Any combination of the gates the context builder reads, each open, closed or uncertain."""
    allowed: dict[str, object] = {}
    values: dict[str, object] = {}

    def gate(name: str, value: st.SearchStrategy[object] | None = None) -> None:
        state = draw(STATES)
        allowed[name] = state
        if value is not None and (state is True or draw(st.booleans())):
            values[name] = draw(value)  # a closed gate may carry one too: it must never be used

    gate("heading_map_verified")
    gate("rotation_model", st.sampled_from(MODELS))
    gate("rotate_in_place_ok")
    gate("near_edge_allowed", st.lists(st.sampled_from(EDGES), min_size=1, max_size=4, unique=True).map(tuple))
    gate("start_pose_known", st.sampled_from((PARK, dict(PARK, heading_deg=181.0, tolerance_deg=7.0))))
    gate("max_program_bytes", st.integers(min_value=1, max_value=10**6))
    gate("kerf_crossing", st.fixed_dictionaries({"w_b": st.sampled_from((0.0, 4.0)), "L_ride": st.just(30.0),
                                                 "h_r": st.sampled_from((0.0, 0.5))}))
    gate("tabs_hold", st.sampled_from((None, 2.5, 8.8)))
    return Gates(tuple(sorted(allowed.items())), tuple(sorted(values.items())))


def closed_for_uncertain(view: Gates) -> Gates:
    """The same view with every uncertain gate (None) closed (False)."""
    return Gates(tuple((name, False if state is None else state) for name, state in view.allowed_map), view.value_map)


def summary(ctx: CheckContext, emitted: EmitProfile) -> tuple[object, ...]:
    return (emitted, ctx.rotation_model, ctx.heading_map_verified, ctx.start_pose and (ctx.start_pose.x,
            ctx.start_pose.y, ctx.start_pose.c), {n: p.value for n, p in ctx.params.items()})


@settings(max_examples=150, deadline=None)
@given(view=gate_views(), session=st.booleans())
def test_total_mapping_every_gate_combination(view: Gates, session: bool) -> None:
    recorded = view.value("start_pose_known")
    c = 90 if isinstance(recorded, dict) and recorded.get("heading_deg") == 181.0 else 0
    park = Pose(1220.0, 610.0, c, "position frames after detection (test data)") if session else None
    meas = measured(park=park) if session else None
    ctx = build_context(JOB, PROFILE, view, meas)
    emitted = emit_profile_for(view, ctx)
    assert isinstance(emitted, EmitProfile)

    heading = view.allowed("heading_map_verified") is True
    rip = view.allowed("rotate_in_place_ok") is True
    model = rotation_model_for(view)
    assert ctx.rotation_model == model
    assert model == (view.value("rotation_model") if view.allowed("rotation_model") is True else "unverified")
    assert ctx.heading_map_verified is heading
    zone = safe_zone_applies(view)
    assert zone is (not (heading and model != "unverified"))

    # the rotation style
    if model == "during-translation":
        assert emitted.rotation_style == "combined"
    if emitted.rotation_style == "split":
        assert model != "during-translation" and (zone or rip)
    else:
        assert model == "during-translation" or not (zone or rip)
    assert split_rotation_allowed(view) is (emitted.rotation_style == "split")
    # the first line
    if emitted.first_line == "translate-then-rotate":
        assert heading and rip and model not in ("unverified", "during-translation")
        assert ctx.start_pose is not None and ctx.start_pose.c is not None
    elif heading and rip and model not in ("unverified", "during-translation"):
        assert ctx.start_pose is None or ctx.start_pose.c is None
    # a C word for the park pose only with the heading map
    if ctx.start_pose is not None and not heading:
        assert ctx.start_pose.c is None
    # the edge margins: 10 mm only on an edge rung R9 tested; the baseline (x = 0 here) keeps 50 mm until tested
    tested = view.value("near_edge_allowed") if view.allowed("near_edge_allowed") is True else ()
    for side, edge in (("left", "x_min"), ("right", "x_max"), ("bottom", "y_min"), ("top", "y_max")):
        assert ctx.params["m_edge_%s" % side].value == (10.0 if edge in tested else 50.0), side
    # every choice is a Param with its basis, and the emitted profile is the one the context records
    for name, param in ctx.params.items():
        assert isinstance(param, Param) and param.ref.strip(), name
    assert emitted == EmitProfile(ctx.params["emit_z_line_style"].value, ctx.params["emit_rotation_style"].value,
                                  ctx.params["emit_first_line"].value)
    # an uncertain gate counts exactly as a closed one
    closed = closed_for_uncertain(view)
    ctx_closed = build_context(JOB, PROFILE, closed, meas)
    assert summary(ctx_closed, emit_profile_for(closed, ctx_closed))[:4] == summary(ctx, emitted)[:4]
    assert {n: p.value for n, p in ctx_closed.params.items()} == {n: p.value for n, p in ctx.params.items()}


@settings(max_examples=60, deadline=None)
@given(view=gate_views())
def test_mapping_is_deterministic(view: Gates) -> None:
    first = build_context(JOB, PROFILE, view)
    again = build_context(JOB, PROFILE, view)
    assert first == again and first.to_json() == again.to_json()
    assert emit_profile_for(view, first) == emit_profile_for(view, again)
