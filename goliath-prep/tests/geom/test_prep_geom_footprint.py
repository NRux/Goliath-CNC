# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The geometry kernel's robot model: heading, supports, footprint models, rotation radius (unit P04).

Spec: docs/protocol/08-head-angle-and-cut-order.md (*Footprint model*,
*Emitting the C word*, *Constants*) and docs/prep/safety-model.md sections 4.1
to 4.6. The expected values here are worked out in the tests from the spec's
constants (402 mm, 211 mm, +-76 deg, theta = (C + 90) mod 360), typed below
from the spec text, never read from the code under test.
"""
from __future__ import annotations

import itertools
import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from goliath_prep import heading_vendor
from goliath_prep.core.types import FootprintSet
from goliath_prep.defaults import vendor_footprint
from goliath_prep.geom.footprint import (
    C_GRID,
    Footprint,
    SupportPosition,
    body_point,
    c_word,
    centroid_offsets,
    footprint_models,
    footprint_radii,
    rotation_radius,
    support_offset,
    support_offsets,
    support_positions,
    supports,
    theta,
    unit,
)

# Spec 08 §Constants, typed from the spec.
REAR_MM = 402.0
SIDE_MM = 211.0
SIDE_DEG = 76.0
SPEC_SUPPORTS = ((REAR_MM, 0.0), (SIDE_MM, SIDE_DEG), (SIDE_MM, -SIDE_DEG))


def spec_offsets(
    theta_deg: float, footprint: tuple[tuple[float, float], ...] = SPEC_SUPPORTS
) -> list[tuple[float, float]]:
    """d_k(theta) = -r_k * u(theta + delta_k), with the platform's trigonometry (test arithmetic only)."""
    out = []
    for r, delta in footprint:
        a = math.radians(theta_deg + delta)
        out.append((-r * math.cos(a), -r * math.sin(a)))
    return out


def vendor_model() -> Footprint:
    (model,) = footprint_models(vendor_footprint())
    return model


# ---------------------------------------------------------------------------
# Heading


def test_theta_formula_on_grid() -> None:
    assert C_GRID == tuple(range(0, 360, 10))
    for c in C_GRID:
        t = theta(c)
        assert type(t) is float
        assert t == float((c + 90) % 360)  # spec 08 §Footprint model
        assert (t - 90 + 360) % 360 == c  # spec 08 §Emitting the C word: the C the post-processor writes
        assert c_word(t) == c
    assert (theta(0), theta(90), theta(180), theta(270), theta(350)) == (90.0, 180.0, 270.0, 0.0, 80.0)


@pytest.mark.parametrize("bad", [5, 360, -10, 365, 1000])
def test_theta_refuses_c_off_the_grid(bad: int) -> None:
    with pytest.raises(ValueError, match="C"):
        theta(bad)


@pytest.mark.parametrize("bad", [10.0, True, None, "10"])
def test_theta_refuses_what_is_not_an_int(bad: object) -> None:
    with pytest.raises(TypeError):
        theta(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [95.0, 360.0, -10.0, float("nan"), float("inf")])
def test_c_word_refuses_a_heading_off_the_grid(bad: float) -> None:
    with pytest.raises(ValueError):
        c_word(bad)


def test_unit_is_the_correctly_rounded_vector_of_alpha_pi_over_180() -> None:
    for alpha in (0.0, 76.0, 90.0, 166.0, 359.5, -76.0, 436.0):
        assert unit(alpha) == heading_vendor.correctly_rounded_cos_sin(alpha * math.pi / 180.0)
    with pytest.raises(ValueError):
        unit(float("nan"))


# ---------------------------------------------------------------------------
# Supports


@pytest.mark.parametrize("p", [(0.0, 0.0), (1000.0, 600.0), (-12.5, 3333.25)])
def test_supports_at_c0_golden(p: tuple[float, float]) -> None:
    px, py = p
    q1, q2, q3 = supports(p, 0, vendor_model())
    # spec 08 §Footprint model, worked values at C = 0
    assert q1 == pytest.approx((px, py - 402.0), abs=1e-9)
    assert q2 == pytest.approx((px + 204.7, py - 51.0), abs=0.06)
    assert q3 == pytest.approx((px - 204.7, py - 51.0), abs=0.06)
    # the same values exactly: 211 sin 76 deg across, 211 cos 76 deg back
    across, back = SIDE_MM * math.sin(math.radians(SIDE_DEG)), SIDE_MM * math.cos(math.radians(SIDE_DEG))
    assert q2 == pytest.approx((px + across, py - back), abs=1e-9)
    assert q3 == pytest.approx((px - across, py - back), abs=1e-9)
    # "a nearly equilateral triangle with sides of about 406 to 410 mm, centroid about 168 mm behind the tool"
    sides = [math.dist(a, b) for a, b in ((q1, q2), (q2, q3), (q3, q1))]
    assert all(405.0 < s < 411.0 for s in sides), sides
    centroid = ((q1[0] + q2[0] + q3[0]) / 3, (q1[1] + q2[1] + q3[1]) / 3)
    assert math.dist(p, centroid) == pytest.approx(168.0, abs=0.1)
    assert centroid[1] < py  # behind the tool: u(theta) = +Y at C = 0 points from the body to the tool


def test_increasing_c_turns_the_footprint_counter_clockwise() -> None:
    model = vendor_model()
    for c in C_GRID:
        got = supports((0.0, 0.0), c, model)
        expected = spec_offsets(c + 90.0)
        for q, d in zip(got, expected):
            assert q == pytest.approx(d, abs=1e-9)
    # C = 90: the heading is 180 (body to tool along -X), the rear support 402 mm along +X
    assert supports((0.0, 0.0), 90, model)[0] == pytest.approx((402.0, 0.0), abs=1e-9)


def test_supports_match_the_vendor_reference_bit_for_bit() -> None:
    # The vendor-floor assertion (design 4.11) compares our heading with spec 08's own test, so the
    # kernel's vendor supports are the same doubles as goliath_prep.heading_vendor's.
    model = vendor_model()
    for c in C_GRID:
        for p in ((0.0, 0.0), (0.1, 1219.9), (2439.0, 3.0), (-7.25, 1e4)):
            assert supports(p, c, model) == heading_vendor.support_points(p, c)


def test_cos90_residual_at_x0() -> None:
    # Spec 08 §Footprint model, *Precision*: cos(90 deg) evaluates to about 6.1e-17, not 0, so a vertex
    # exactly on x = 0 puts q1 a few 1e-14 mm outside the panel. The kernel keeps that arithmetic.
    residual = heading_vendor.correctly_rounded_cos_sin(90.0 * math.pi / 180.0)[0]
    assert 6.0e-17 < residual < 6.2e-17
    q1, _, _ = supports((0.0, 500.0), 0, vendor_model())
    assert q1[0] == -402.0 * residual
    assert -1e-13 < q1[0] < 0.0
    # the same double as the vendor reference, so the vendor-floor assertion sees what spec 08 sees
    assert heading_vendor.support_points((0.0, 500.0), 0)[0][0] == q1[0]
    # the kernel's own checks never decide on it: a grown, closed work area keeps every wheel tens of mm
    # inside the panel, so a residual of 1e-14 mm cannot turn a pass into a refusal or back


def test_support_offset_is_minus_r_u_theta_plus_delta() -> None:
    for theta_deg in (0.0, 90.0, 137.5, 270.0):
        for r, delta in SPEC_SUPPORTS + ((405.0, 0.5), (212.0, 75.5)):
            ux, uy = unit(theta_deg + delta)
            assert support_offset(r, delta, theta_deg) == (-r * ux, -r * uy)


def test_supports_validates_its_inputs() -> None:
    model = vendor_model()
    with pytest.raises(ValueError, match="finite"):
        supports((float("nan"), 0.0), 0, model)
    with pytest.raises(TypeError):
        supports((0.0,), 0, model)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        supports((0.0, 0.0), 5, model)
    with pytest.raises(TypeError, match="Footprint"):
        supports((0.0, 0.0), 0, vendor_footprint())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Footprint("vendor", ((402.0, 0.0), (211.0, 76.0)))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Footprint("vendor", ((402.0, 0.0), (-211.0, 76.0), (211.0, -76.0)))
    with pytest.raises(ValueError):
        Footprint("guessed", SPEC_SUPPORTS)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Footprint models: vendor, measured, union (safety-model 4.3)

MEASURED_PARTIAL = ((405.0, 0.5), None, (210.0, -76.5))


def union_footprint(measured: tuple = MEASURED_PARTIAL) -> FootprintSet:
    return FootprintSet(vendor=SPEC_SUPPORTS, measured=measured, active="union")


def test_footprint_models_vendor_only() -> None:
    models = footprint_models(vendor_footprint())
    assert [m.name for m in models] == ["vendor"]
    assert models[0].supports == SPEC_SUPPORTS
    assert [(p.k, p.model, p.r, p.delta) for p in support_positions(vendor_footprint())] == [
        (1, "vendor", 402.0, 0.0), (2, "vendor", 211.0, 76.0), (3, "vendor", 211.0, -76.0)]


def test_footprint_models_union_keeps_the_spec_where_nothing_was_measured() -> None:
    models = footprint_models(union_footprint())
    assert [m.name for m in models] == ["vendor", "measured"]
    # q2 was not measured: the measured model keeps its spec position (a measurement adds, never replaces)
    assert models[1].supports == ((405.0, 0.5), (211.0, 76.0), (210.0, -76.5))
    positions = support_positions(union_footprint())
    assert [(p.k, p.model) for p in positions] == [
        (1, "vendor"), (2, "vendor"), (3, "vendor"), (1, "measured"), (3, "measured")]
    assert [p.label for p in positions] == ["q1", "q2", "q3", "q1 (measured)", "q3 (measured)"]
    # every position of the active model, at a heading
    offsets = support_offsets(30, union_footprint())
    assert [pos for pos, _ in offsets] == list(positions)
    for pos, d in offsets:
        assert d == support_offset(pos.r, pos.delta, theta(30))


def test_a_measured_position_equal_to_the_spec_adds_nothing() -> None:
    fp = union_footprint(((402.0, 0.0), None, (210.0, -76.5)))
    assert [(p.k, p.model) for p in support_positions(fp)] == [(1, "vendor"), (2, "vendor"), (3, "vendor"),
                                                                 (3, "measured")]


def test_support_position_validates() -> None:
    with pytest.raises(ValueError):
        SupportPosition(4, "vendor", 211.0, 76.0)
    with pytest.raises(ValueError):
        SupportPosition(1, "vendor", 0.0, 0.0)


# ---------------------------------------------------------------------------
# Rotation radius (safety-model 4.6; red-team PHYS-07)


def test_r_rot_spec_values_about_404() -> None:
    offsets = spec_offsets(90.0)
    cx = (offsets[0][0] + offsets[1][0] + offsets[2][0]) / 3
    cy = (offsets[0][1] + offsets[1][1] + offsets[2][1]) / 3
    d_c = math.hypot(cx, cy)
    r_c = max(math.hypot(x - cx, y - cy) for x, y in offsets)
    assert d_c == pytest.approx(168.03, abs=0.005)
    assert r_c == pytest.approx(235.80, abs=0.005)
    assert d_c + r_c == pytest.approx(403.83, abs=0.01)

    radii = footprint_radii(vendor_footprint())
    assert (radii.r_max, radii.r_min) == (402.0, 211.0)
    assert radii.d_c == pytest.approx(d_c, abs=1e-9)
    assert radii.r_c == pytest.approx(r_c, abs=1e-9)
    assert radii.r_rot_exact == pytest.approx(d_c + r_c, abs=1e-9)
    assert radii.r_rot == 404.0  # rounded up to whole millimetres
    assert rotation_radius(vendor_footprint()) == 404.0


def test_half_turn_about_the_centroid_breaks_the_annulus() -> None:
    # Red-team PHYS-07: turning 180 deg about the centroid brings the rear support to 66 mm of the tool
    # point (234 - 168), inside r_min - rho = 156 mm: an annulus is not enough during rotations.
    (c,) = centroid_offsets(vendor_footprint(), 0)
    rear = spec_offsets(90.0)[0]
    turned = (2 * c[0] - rear[0], 2 * c[1] - rear[1])
    assert math.hypot(*turned) == pytest.approx(66.0, abs=0.1)
    assert math.hypot(*turned) < footprint_radii(vendor_footprint()).r_min - 55.0


def _combinations(fp: FootprintSet) -> list[tuple[tuple[float, float], ...]]:
    choices = []
    for k in range(3):
        options = [fp.vendor[k]]
        if fp.measured is not None and fp.measured[k] is not None:
            options.append(fp.measured[k])
        choices.append(options)
    return [tuple(combo) for combo in itertools.product(*choices)]


def _centroid(offsets: list[tuple[float, float]]) -> tuple[float, float]:
    return ((offsets[0][0] + offsets[1][0] + offsets[2][0]) / 3, (offsets[0][1] + offsets[1][1] + offsets[2][1]) / 3)


measured_support = st.tuples(st.floats(150.0, 480.0), st.floats(-179.0, 179.0))
maybe_measured = st.one_of(st.none(), measured_support)
measured_sets = st.tuples(maybe_measured, maybe_measured, maybe_measured).filter(
    lambda m: any(s is not None for s in m))


@settings(max_examples=150, deadline=None)
@given(measured=measured_sets, s=st.floats(0.0, 1.0), turn=st.floats(-720.0, 720.0), c=st.sampled_from(C_GRID))
def test_rotation_radius_holds_every_support_turning_about_any_pivot(
    measured: tuple, s: float, turn: float, c: int
) -> None:
    # Union model: each wheel is at its spec or its measured position, independently, so the robot's
    # true footprint is one of the combinations, and it turns about a pivot between the tool point and
    # that footprint's centroid (safety-model 4.6). Every support of it stays within R_rot of the tool.
    fp = union_footprint(measured)
    r_rot = rotation_radius(fp)
    assert r_rot == math.ceil(r_rot)
    every_position = spec_offsets(theta(c), tuple(fp.vendor) + tuple(m for m in fp.measured if m is not None))
    exact = 0.0  # the kernel's reading: every combination's centroid against every support position
    for combo in _combinations(fp):
        offsets = spec_offsets(theta(c), combo)
        cx, cy = _centroid(offsets)
        exact = max(exact, max(math.hypot(cx, cy) + math.hypot(x - cx, y - cy) for x, y in every_position))
        px, py = s * cx, s * cy
        a = math.radians(turn)
        for x, y in offsets:
            vx, vy = x - px, y - py
            qx = px + vx * math.cos(a) - vy * math.sin(a)
            qy = py + vx * math.sin(a) + vy * math.cos(a)
            assert math.hypot(qx, qy) <= r_rot + 1e-9
    # whole millimetres, rounded up, and never more than that reading needs
    assert exact <= r_rot < exact + 1.0 + 1e-6
    assert footprint_radii(fp).r_rot_exact == pytest.approx(exact, abs=1e-9)


@settings(max_examples=60, deadline=None)
@given(measured=measured_sets)
def test_union_rotation_radius_covers_the_safety_model_formula(measured: tuple) -> None:
    # safety-model 4.6: under the union model, the largest over both models' centroids c and every
    # support position d_k of both models of r_k and |c| + |d_k - c|. The kernel's value is never below it.
    fp = union_footprint(measured)
    vendor = list(fp.vendor)
    measured_model = [m if m is not None else v for m, v in zip(fp.measured, fp.vendor)]  # type: ignore[arg-type]
    positions = spec_offsets(90.0, tuple(vendor)) + spec_offsets(90.0, tuple(m for m in fp.measured if m is not None))
    bound = max(r for r, _ in vendor + [m for m in fp.measured if m is not None])
    for model in (vendor, measured_model):
        cx, cy = _centroid(spec_offsets(90.0, tuple(model)))
        bound = max(bound, max(math.hypot(cx, cy) + math.hypot(x - cx, y - cy) for x, y in positions))
    assert rotation_radius(fp) >= bound - 1e-9
    radii = footprint_radii(fp)
    all_r = [r for r, _ in vendor] + [m[0] for m in fp.measured if m is not None]
    assert radii.r_max == max(all_r)
    assert radii.r_min == min(all_r)


def test_centroid_offsets_one_per_combination() -> None:
    assert len(centroid_offsets(vendor_footprint(), 0)) == 1
    fp = union_footprint()  # q1 and q3 measured: 2 x 1 x 2 combinations
    assert len(centroid_offsets(fp, 0)) == 4
    for (cx, cy), combo in zip(centroid_offsets(fp, 0), _combinations(fp)):
        ex, ey = _centroid(spec_offsets(90.0, combo))
        assert (cx, cy) == pytest.approx((ex, ey), abs=1e-9)


# ---------------------------------------------------------------------------
# Body frame (machine.toml: back_mm behind T, right_mm right of the body axis)


def test_body_point_matches_the_support_convention() -> None:
    # A support (r, delta) lies r*cos(delta) behind T and r*sin(delta) to its right (machine.toml
    # footprint: "angle from the backward body axis, positive to the right"); at C = 0 the kernel's
    # local frame is the panel frame.
    for r, delta in SPEC_SUPPORTS + ((405.0, 0.5), (212.0, 75.5)):
        back = r * math.cos(math.radians(delta))
        right = r * math.sin(math.radians(delta))
        assert body_point(back, right) == pytest.approx(support_offset(r, delta, theta(0)), abs=1e-9)
    assert body_point(470.0, 255.0) == (255.0, -470.0)
    with pytest.raises(ValueError):
        body_point(float("inf"), 0.0)
