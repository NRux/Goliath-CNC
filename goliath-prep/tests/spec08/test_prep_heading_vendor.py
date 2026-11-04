# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""goliath_prep.heading_vendor reproduces spec 08's heading search exactly (unit P05).

Spec: docs/protocol/08-head-angle-and-cut-order.md, sections *Footprint model*,
*Tower keep-out zones* and *Candidate search and selection*. The differential
tests against the literal transcription are in
test_prep_heading_vendor_differential.py; this file holds the named cases.
"""
from __future__ import annotations

import math
from pathlib import Path
from types import ModuleType

import pytest

from goliath_prep import heading_vendor as hv
from goliath_prep.heading_vendor import (
    VendorResult,
    candidate_table,
    candidates,
    evaluate,
    select_heading_vendor,
    support_points,
    tower_zones,
    triangle_contains,
)

NO_SIDES: frozenset[str] = frozenset()
ALL_SIDES = frozenset({"left", "right", "bottom", "top"})
ZERO_OVERHANG = {"left": 0.0, "right": 0.0, "bottom": 0.0, "top": 0.0}
GRID = tuple(range(0, 361, 10))
MODULE_FILE = Path(hv.__file__).resolve()


def _ov(box: tuple[float, float, float, float], W: float, H: float) -> dict[str, float]:
    """Spec 08 pass 2's ``ov``: the sides strictly overhung, in the pseudo-code's order."""
    x0, x1, y0, y1 = box
    ov = {}
    if x0 < 0:
        ov["left"] = -x0
    if x1 > W:
        ov["right"] = x1 - W
    if y0 < 0:
        ov["bottom"] = -y0
    if y1 > H:
        ov["top"] = y1 - H
    return ov


def _plain_sum(values) -> float:
    total = 0.0
    for value in values:
        total = total + value
    return total


def _pass2_keys(P, W, H, B, tower2, summer=_plain_sum) -> dict[int, tuple[int, float]]:
    """(K, S) of every pass-2 survivor, computed from evaluate() as spec 08 states it."""
    keys = {}
    for i in GRID:
        _theta, box, hit = evaluate(i, P, W, H, tower2)
        if hit:
            continue
        ov = _ov(box, W, H)
        assert ov, "this helper is for inputs where pass 1 fails for every candidate"
        if any(side in B for side in ov):
            continue
        keys[i] = (len(ov), summer(list(ov.values())))
    return keys


def _point_with_support_at(target: tuple[float, float], i: int, k: int) -> tuple[float, float]:
    """A tool point whose support k at candidate i lands on (or within a few ulps of) target."""
    q = support_points((0.0, 0.0), i)[k]  # = -r·u(θ+δ)
    return (target[0] - q[0], target[1] - q[1])


# --------------------------------------------------------------------------
# The tests the build design names first


def test_empty_P_gives_C0() -> None:
    # Box start (W, 0, H, 0): an empty P counts as inside, so pass 1 accepts
    # the first candidate whatever B and tower2 are.
    for tower2 in (0, 1):
        for blocked in (NO_SIDES, ALL_SIDES):
            result = select_heading_vendor([], 2440.0, 1220.0, blocked, tower2, -1)
            assert result == VendorResult(theta=90.0, c=0, overhang=ZERO_OVERHANG, pass_=1, candidate=0)
            assert result.found
        assert evaluate(0, [], 2440.0, 1220.0, tower2) == (90.0, (2440.0, 0.0, 1220.0, 0.0), False)
    # A forced on-grid heading is the only candidate, and an empty P accepts it.
    forced = select_heading_vendor([], 2440.0, 1220.0, NO_SIDES, 0, 30)
    assert (forced.c, forced.candidate, forced.theta, forced.pass_) == (30, 30, 120.0, 1)


@pytest.mark.parametrize("forced", [5, 45, 355, 365, 370, 1000, -2, -10, -360])
def test_forced_off_grid_gives_nan(forced: int) -> None:
    assert candidates(forced) == ()
    # Even an empty P (which accepts any candidate) cannot rescue it: there is no candidate.
    for P in ([], [(1220.0, 610.0)]):
        result = select_heading_vendor(P, 2440.0, 1220.0, NO_SIDES, 0, forced)
        assert math.isnan(result.theta)
        assert result.c is None and result.candidate is None and result.pass_ is None
        # select_heading returns NaN alone; no overhang that could be read as "none needed".
        assert result.overhang == {}
        assert not result.found


def test_pass2_ties_go_to_lower_C() -> None:
    # 300 x 300 panel, one point at its centre: no candidate fits, and C = 130,
    # 230 and 310 tie exactly (bit for bit) on the fewest sides and the smallest
    # total. Strict comparisons keep the first of them: C = 130.
    W = H = 300.0
    P = [(150.0, 150.0)]
    keys = _pass2_keys(P, W, H, NO_SIDES, 0)
    best = min(keys.values())
    tied = [i for i, key in keys.items() if key == best]
    assert tied == [130, 230, 310]
    result = select_heading_vendor(P, W, H, NO_SIDES, 0, -1)
    assert (result.pass_, result.candidate, result.c, result.theta) == (2, 130, 130, 220.0)

    # Blocking the winner's side moves the choice to the next tie member, again the lowest.
    blocked = select_heading_vendor(P, W, H, frozenset({"right"}), 0, -1)
    assert (blocked.pass_, blocked.candidate) == (2, 230)

    # C = 360 repeats C = 0 exactly, so when C = 0 wins pass 2 the two tie and C = 0 is kept.
    P0 = [(210.0, 200.0)]
    keys0 = _pass2_keys(P0, W, H, NO_SIDES, 0)
    assert keys0[0] == keys0[360] == min(keys0.values())
    zero = select_heading_vendor(P0, W, H, NO_SIDES, 0, -1)
    assert (zero.pass_, zero.candidate, zero.c, zero.theta) == (2, 0, 0, 90.0)


def test_c360_only_when_forced() -> None:
    cases = [
        ([(1220.0, 610.0)], 2440.0, 1220.0),  # pass 1 at C = 0
        ([(210.0, 200.0)], 300.0, 300.0),  # pass 2 at C = 0
        ([], 2440.0, 1220.0),  # empty P
    ]
    for P, W, H in cases:
        assert evaluate(360, P, W, H, 0) == evaluate(0, P, W, H, 0)
        unforced = select_heading_vendor(P, W, H, NO_SIDES, 0, -1)
        assert unforced.candidate == 0 and unforced.c == 0
        forced = select_heading_vendor(P, W, H, NO_SIDES, 0, 360)
        # The candidate is 360; the C the vendor writes is (θ − 90 + 360) mod 360 = 0.
        assert forced.candidate == 360
        assert forced.c == 0 and forced.theta == 90.0
        assert (forced.overhang, forced.pass_) == (unforced.overhang, unforced.pass_)


# --------------------------------------------------------------------------
# Footprint model


def test_footprint_worked_values_at_C0() -> None:
    p = (1000.0, 1000.0)
    q1, q2, q3 = support_points(p, 0)
    assert q1 == (pytest.approx(1000.0, abs=1e-9), 598.0)
    assert q2 == (pytest.approx(1204.7, abs=0.1), pytest.approx(949.0, abs=0.1))
    assert q3 == (pytest.approx(795.3, abs=0.1), pytest.approx(949.0, abs=0.1))
    # Nearly equilateral, sides of about 406 to 410 mm, centroid about 168 mm behind the tool.
    sides = sorted(math.dist(a, b) for a, b in ((q1, q2), (q2, q3), (q3, q1)))
    assert 405.5 < sides[0] and sides[-1] < 410.5
    centroid = ((q1[0] + q2[0] + q3[0]) / 3, (q1[1] + q2[1] + q3[1]) / 3)
    assert math.dist(p, centroid) == pytest.approx(168.0, abs=0.5)
    # With a large panel the box is the supports' own extent.
    assert evaluate(0, [p], 3000.0, 3000.0, 0) == (
        90.0,
        (min(q[0] for q in (q1, q2, q3)), max(q[0] for q in (q1, q2, q3)), 598.0, max(q2[1], q3[1])),
        False,
    )


def test_increasing_C_rotates_counter_clockwise() -> None:
    p = (1000.0, 1000.0)
    rear = {c: support_points(p, c)[0] for c in (0, 90, 180, 270)}
    assert rear[0] == (pytest.approx(1000.0), 598.0)  # body below the tool
    assert rear[90] == (1402.0, pytest.approx(1000.0))  # rotated 90° counter-clockwise
    assert rear[180] == (pytest.approx(1000.0), 1402.0)
    assert rear[270] == (598.0, 1000.0)


def test_cos90_residual_puts_a_vertex_on_x0_outside() -> None:
    # Spec 08 Implementation notes: cos(90°) is about 6.1e-17, so the rear
    # support of a vertex exactly on x = 0 lies a few 1e-14 mm outside.
    q1 = support_points((0.0, 610.0), 0)[0]
    assert -1e-13 < q1[0] < 0.0
    assert q1[0] == 0.0 - 402.0 * math.cos(90.0 * math.pi / 180.0)


def test_theta_and_c_formulas_over_the_grid() -> None:
    for i in GRID:
        theta, _box, _hit = evaluate(i, [], 1000.0, 1000.0, 0)
        assert theta == float((i + 90) % 360)
        result = select_heading_vendor([], 1000.0, 1000.0, NO_SIDES, 0, i)
        assert result.candidate == i
        assert result.c == i % 360 == int((theta - 90 + 360) % 360)
        assert result.c in range(0, 360, 10)


def test_angle_is_alpha_times_pi_over_180_unreduced() -> None:
    # Pinned reading (spec 08: "compute cos/sin of θ·π/180", u(θ + δ)): the angle
    # α = θ + δ is not reduced modulo 360, and α·π/180 is evaluated left to right.
    readings = {
        "(α·π)/180": lambda alpha: alpha * math.pi / 180,
        "α·(π/180)": lambda alpha: alpha * (math.pi / 180),
        "math.radians": math.radians,
        "(α mod 360)·π/180": lambda alpha: (alpha % 360) * math.pi / 180,
    }
    ours = "(α·π)/180"
    differs = dict.fromkeys(readings, False)
    origin = (0.0, 0.0)  # q = −r·u exactly, so a last-bit difference in r·u stays visible
    for i in GRID:
        theta = float((i + 90) % 360)
        expected = {}
        for name, radians in readings.items():
            expected[name] = tuple(
                (origin[0] - r * math.cos(radians(theta + delta)), origin[1] - r * math.sin(radians(theta + delta)))
                for r, delta in ((402.0, 0.0), (211.0, 76.0), (211.0, -76.0))
            )
            differs[name] = differs[name] or expected[name] != expected[ours]
        qs = expected[ours]
        assert support_points(origin, i) == qs
        box = (
            min([3000.0] + [q[0] for q in qs]),
            max([0.0] + [q[0] for q in qs]),
            min([3000.0] + [q[1] for q in qs]),
            max([0.0] + [q[1] for q in qs]),
        )
        assert evaluate(i, [origin], 3000.0, 3000.0, 0)[:2] == (theta, box)
    # The pin is not vacuous: every other reading moves some support in the last bits.
    del differs[ours]
    assert all(differs.values()), differs


# cos and sin of (α·π)/180, correctly rounded to the nearest double, for every
# α = θ + δ the search uses (θ = (i + 90) mod 360 over the grid, δ = 0, +76, -76:
# 108 angles). Computed with 300-bit arithmetic, outside this suite.
CORRECTLY_ROUNDED_COS_SIN: dict[int, tuple[str, str]] = {
    -76: ("0x1.ef74bf2e4b923p-3", "-0x1.f0ca99f79ba25p-1"),
    -66: ("0x1.a07f921061ad1p-2", "-0x1.d3bc3aeff7f95p-1"),
    -56: ("0x1.1e4e88411fd12p-1", "-0x1.a8779cda8eea5p-1"),
    -46: ("0x1.63aa430e07311p-1", "-0x1.704d4e6a54d38p-1"),
    -36: ("0x1.9e3779b97f4a8p-1", "-0x1.2cf2304755a5ep-1"),
    -26: ("0x1.cc2ebbb5638cap-1", "-0x1.c0e45dabe05c8p-2"),
    -16: ("0x1.ec2a7e35e7b80p-1", "-0x1.1a40add328e29p-2"),
    -6: ("0x1.fd31f94f867c6p-1", "-0x1.ac2609b3c576bp-4"),
    0: ("0x1.0000000000000p+0", "0x0.0p+0"),
    4: ("0x1.fec0b7170fff6p-1", "0x1.1db8f6d6a5128p-4"),
    10: ("0x1.f838b8c811c17p-1", "0x1.63a1a7e0b7389p-3"),
    14: ("0x1.f0ca99f79ba25p-1", "0x1.ef74bf2e4b91dp-3"),
    20: ("0x1.e11f642522d1cp-1", "0x1.5e3a8748a0bf5p-2"),
    24: ("0x1.d3bc3aeff7f95p-1", "0x1.a07f921061ad0p-2"),
    30: ("0x1.bb67ae8584cabp-1", "0x1.fffffffffffffp-2"),
    34: ("0x1.a8779cda8eea4p-1", "0x1.1e4e88411fd13p-1"),
    40: ("0x1.8836fa2cf5039p-1", "0x1.491b7523c161cp-1"),
    44: ("0x1.704d4e6a54d39p-1", "0x1.63aa430e07310p-1"),
    50: ("0x1.491b7523c161dp-1", "0x1.8836fa2cf5039p-1"),
    54: ("0x1.2cf2304755a5ep-1", "0x1.9e3779b97f4a8p-1"),
    60: ("0x1.0000000000001p-1", "0x1.bb67ae8584caap-1"),
    64: ("0x1.c0e45dabe05c9p-2", "0x1.cc2ebbb5638cap-1"),
    70: ("0x1.5e3a8748a0bf7p-2", "0x1.e11f642522d1bp-1"),
    74: ("0x1.1a40add328e29p-2", "0x1.ec2a7e35e7b80p-1"),
    76: ("0x1.ef74bf2e4b923p-3", "0x1.f0ca99f79ba25p-1"),
    80: ("0x1.63a1a7e0b738cp-3", "0x1.f838b8c811c17p-1"),
    84: ("0x1.ac2609b3c576bp-4", "0x1.fd31f94f867c6p-1"),
    86: ("0x1.1db8f6d6a5133p-4", "0x1.fec0b7170fff6p-1"),
    90: ("0x1.1a62633145c07p-54", "0x1.0000000000000p+0"),
    94: ("-0x1.1db8f6d6a512ap-4", "0x1.fec0b7170fff6p-1"),
    96: ("-0x1.ac2609b3c5762p-4", "0x1.fd31f94f867c7p-1"),
    100: ("-0x1.63a1a7e0b7388p-3", "0x1.f838b8c811c17p-1"),
    104: ("-0x1.ef74bf2e4b91fp-3", "0x1.f0ca99f79ba25p-1"),
    106: ("-0x1.1a40add328e27p-2", "0x1.ec2a7e35e7b80p-1"),
    110: ("-0x1.5e3a8748a0bf5p-2", "0x1.e11f642522d1cp-1"),
    114: ("-0x1.a07f921061acep-2", "0x1.d3bc3aeff7f96p-1"),
    116: ("-0x1.c0e45dabe05cap-2", "0x1.cc2ebbb5638c9p-1"),
    120: ("-0x1.ffffffffffffcp-2", "0x1.bb67ae8584cabp-1"),
    124: ("-0x1.1e4e88411fd11p-1", "0x1.a8779cda8eea5p-1"),
    126: ("-0x1.2cf2304755a5dp-1", "0x1.9e3779b97f4a8p-1"),
    130: ("-0x1.491b7523c161dp-1", "0x1.8836fa2cf5039p-1"),
    134: ("-0x1.63aa430e0730ep-1", "0x1.704d4e6a54d3bp-1"),
    136: ("-0x1.704d4e6a54d39p-1", "0x1.63aa430e0730fp-1"),
    140: ("-0x1.8836fa2cf5038p-1", "0x1.491b7523c161ep-1"),
    144: ("-0x1.9e3779b97f4a7p-1", "0x1.2cf2304755a5fp-1"),
    146: ("-0x1.a8779cda8eea4p-1", "0x1.1e4e88411fd13p-1"),
    150: ("-0x1.bb67ae8584cabp-1", "0x1.fffffffffffffp-2"),
    154: ("-0x1.cc2ebbb5638cap-1", "0x1.c0e45dabe05c6p-2"),
    156: ("-0x1.d3bc3aeff7f94p-1", "0x1.a07f921061ad5p-2"),
    160: ("-0x1.e11f642522d1bp-1", "0x1.5e3a8748a0bf8p-2"),
    164: ("-0x1.ec2a7e35e7b7ep-1", "0x1.1a40add328e32p-2"),
    166: ("-0x1.f0ca99f79ba25p-1", "0x1.ef74bf2e4b91dp-3"),
    170: ("-0x1.f838b8c811c17p-1", "0x1.63a1a7e0b7387p-3"),
    174: ("-0x1.fd31f94f867c6p-1", "0x1.ac2609b3c577fp-4"),
    176: ("-0x1.fec0b7170fff6p-1", "0x1.1db8f6d6a5138p-4"),
    180: ("-0x1.0000000000000p+0", "0x1.1a62633145c07p-53"),
    184: ("-0x1.fec0b7170fff7p-1", "-0x1.1db8f6d6a5106p-4"),
    186: ("-0x1.fd31f94f867c7p-1", "-0x1.ac2609b3c574ep-4"),
    190: ("-0x1.f838b8c811c17p-1", "-0x1.63a1a7e0b738ep-3"),
    194: ("-0x1.f0ca99f79ba25p-1", "-0x1.ef74bf2e4b915p-3"),
    196: ("-0x1.ec2a7e35e7b80p-1", "-0x1.1a40add328e26p-2"),
    200: ("-0x1.e11f642522d1cp-1", "-0x1.5e3a8748a0bf4p-2"),
    204: ("-0x1.d3bc3aeff7f97p-1", "-0x1.a07f921061acap-2"),
    206: ("-0x1.cc2ebbb5638cbp-1", "-0x1.c0e45dabe05c2p-2"),
    210: ("-0x1.bb67ae8584caap-1", "-0x1.0000000000001p-1"),
    214: ("-0x1.a8779cda8eea6p-1", "-0x1.1e4e88411fd11p-1"),
    216: ("-0x1.9e3779b97f4a9p-1", "-0x1.2cf2304755a5dp-1"),
    220: ("-0x1.8836fa2cf5039p-1", "-0x1.491b7523c161cp-1"),
    224: ("-0x1.704d4e6a54d38p-1", "-0x1.63aa430e07311p-1"),
    226: ("-0x1.63aa430e07313p-1", "-0x1.704d4e6a54d36p-1"),
    230: ("-0x1.491b7523c161ep-1", "-0x1.8836fa2cf5038p-1"),
    234: ("-0x1.2cf2304755a5fp-1", "-0x1.9e3779b97f4a7p-1"),
    236: ("-0x1.1e4e88411fd16p-1", "-0x1.a8779cda8eea2p-1"),
    240: ("-0x1.0000000000004p-1", "-0x1.bb67ae8584ca8p-1"),
    244: ("-0x1.c0e45dabe05cep-2", "-0x1.cc2ebbb5638c8p-1"),
    246: ("-0x1.a07f921061acfp-2", "-0x1.d3bc3aeff7f96p-1"),
    250: ("-0x1.5e3a8748a0c01p-2", "-0x1.e11f642522d1ap-1"),
    254: ("-0x1.1a40add328e24p-2", "-0x1.ec2a7e35e7b81p-1"),
    256: ("-0x1.ef74bf2e4b91fp-3", "-0x1.f0ca99f79ba25p-1"),
    260: ("-0x1.63a1a7e0b7389p-3", "-0x1.f838b8c811c17p-1"),
    264: ("-0x1.ac2609b3c5764p-4", "-0x1.fd31f94f867c7p-1"),
    266: ("-0x1.1db8f6d6a513cp-4", "-0x1.fec0b7170fff6p-1"),
    270: ("-0x1.a79394c9e8a0ap-53", "-0x1.0000000000000p+0"),
    274: ("0x1.1db8f6d6a5122p-4", "-0x1.fec0b7170fff7p-1"),
    276: ("0x1.ac2609b3c5749p-4", "-0x1.fd31f94f867c7p-1"),
    280: ("0x1.63a1a7e0b737cp-3", "-0x1.f838b8c811c18p-1"),
    286: ("0x1.1a40add328e2dp-2", "-0x1.ec2a7e35e7b7fp-1"),
    290: ("0x1.5e3a8748a0bebp-2", "-0x1.e11f642522d1dp-1"),
    296: ("0x1.c0e45dabe05c8p-2", "-0x1.cc2ebbb5638cap-1"),
    300: ("0x1.0000000000001p-1", "-0x1.bb67ae8584caap-1"),
    306: ("0x1.2cf2304755a5cp-1", "-0x1.9e3779b97f4a9p-1"),
    310: ("0x1.491b7523c161cp-1", "-0x1.8836fa2cf503ap-1"),
    316: ("0x1.704d4e6a54d36p-1", "-0x1.63aa430e07313p-1"),
    320: ("0x1.8836fa2cf5037p-1", "-0x1.491b7523c161fp-1"),
    326: ("0x1.a8779cda8eea2p-1", "-0x1.1e4e88411fd17p-1"),
    330: ("0x1.bb67ae8584ca8p-1", "-0x1.0000000000004p-1"),
    336: ("0x1.d3bc3aeff7f96p-1", "-0x1.a07f921061ad0p-2"),
    340: ("0x1.e11f642522d1cp-1", "-0x1.5e3a8748a0bf3p-2"),
    346: ("0x1.f0ca99f79ba25p-1", "-0x1.ef74bf2e4b922p-3"),
    350: ("0x1.f838b8c811c16p-1", "-0x1.63a1a7e0b73abp-3"),
    356: ("0x1.fec0b7170fff7p-1", "-0x1.1db8f6d6a5101p-4"),
    366: ("0x1.fd31f94f867c7p-1", "0x1.ac2609b3c5745p-4"),
    376: ("0x1.ec2a7e35e7b7fp-1", "0x1.1a40add328e2cp-2"),
    386: ("0x1.cc2ebbb5638cap-1", "0x1.c0e45dabe05c7p-2"),
    396: ("0x1.9e3779b97f4a4p-1", "0x1.2cf2304755a62p-1"),
    406: ("0x1.63aa430e07313p-1", "0x1.704d4e6a54d36p-1"),
    416: ("0x1.1e4e88411fd11p-1", "0x1.a8779cda8eea6p-1"),
    426: ("0x1.a07f921061ad1p-2", "0x1.d3bc3aeff7f95p-1"),
}


def test_platform_cos_sin_are_the_correctly_rounded_doubles() -> None:
    # The search is bit-exact only if math.cos and math.sin (the C library's)
    # return these doubles. Several lie within a hundredth of an ulp of a rounding
    # midpoint (cos 194°, cos 114°, sin 274°, ...); a library that rounds just
    # those differently already breaks the exact ties of
    # test_pass2_ties_go_to_lower_C and changes the winner of
    # test_overhang_sum_is_plain_left_to_right. If this fails, this platform's
    # vendor reference is not the one tested on the others: report it, and do not
    # edit the expected values to match.
    angles = sorted({float((i + 90) % 360) + delta for i in GRID for delta in (0.0, 76.0, -76.0)})
    assert [int(alpha) for alpha in angles] == sorted(CORRECTLY_ROUNDED_COS_SIN)
    differ = []
    for alpha in angles:
        x = alpha * math.pi / 180
        got = (math.cos(x).hex(), math.sin(x).hex())
        if got != CORRECTLY_ROUNDED_COS_SIN[int(alpha)]:
            differ.append((alpha, got, CORRECTLY_ROUNDED_COS_SIN[int(alpha)]))
    assert differ == [], "this platform's math.cos/math.sin are not correctly rounded here: %r" % differ
    # The module's footprint is built from exactly these doubles.
    for i in GRID:
        theta = float((i + 90) % 360)
        for q, (r, delta) in zip(support_points((0.0, 0.0), i), ((402.0, 0.0), (211.0, 76.0), (211.0, -76.0))):
            cos_hex, sin_hex = CORRECTLY_ROUNDED_COS_SIN[int(theta + delta)]
            assert q == (0.0 - r * float.fromhex(cos_hex), 0.0 - r * float.fromhex(sin_hex))


# --------------------------------------------------------------------------
# Tower keep-out zones


def test_tower_zones_vertex_order() -> None:
    W, H = 2440.0, 1220.0
    assert tower_zones(W, H, 0) == (
        ((0.0, 0.0), (0.0, 150.0), (150.0, 0.0)),
        ((0.0, 1070.0), (0.0, 1220.0), (150.0, 1220.0)),
    )
    assert tower_zones(W, H, 1) == (
        ((0.0, 0.0), (0.0, 150.0), (150.0, 0.0)),
        ((2290.0, 0.0), (2440.0, 0.0), (2440.0, 150.0)),
    )


def test_triangle_edges_tower1_verified() -> None:
    z1 = tower_zones(2440.0, 1220.0, 0)[0]
    inside = [(0.0, 0.0), (75.0, 75.0), (100.0, 50.0), (30.0, 30.0)]  # corner, hypotenuse, interior
    outside = [(0.0, 75.0), (75.0, 0.0), (0.0, 150.0), (150.0, 0.0), (100.0, 100.0), (-1.0, 10.0), (10.0, -1.0)]
    assert [triangle_contains(q, z1) for q in inside] == [True] * len(inside)
    assert [triangle_contains(q, z1) for q in outside] == [False] * len(outside)


def test_triangle_edges_tower2_top_left_verified() -> None:
    H = 1220.0
    z2 = tower_zones(2440.0, H, 0)[1]
    inside = [(75.0, H), (30.0, H - 10.0), (149.0, H)]  # edge y = H, interior
    outside = [(75.0, H - 75.0), (0.0, H - 75.0), (100.0, H - 100.0)]  # hypotenuse, edge x = 0, beyond
    assert [triangle_contains(q, z2) for q in inside] == [True] * len(inside)
    assert [triangle_contains(q, z2) for q in outside] == [False] * len(outside)


def test_triangle_vertices_follow_the_formula() -> None:
    # Spec 08 states the edge behaviour for edge interiors (and Z1's corner); a
    # vertex where an "inside" and an "outside" edge meet is decided by the exact
    # test, which spec 08 makes normative. Pinned here, vertex by vertex.
    W, H = 2440.0, 1220.0
    z1, z2_top_left = tower_zones(W, H, 0)
    z2_bottom_right = tower_zones(W, H, 1)[1]
    assert [triangle_contains(v, z1) for v in z1] == [True, False, False]
    # (0, H − 150) is on the edge x = 0 and on the hypotenuse, yet inside;
    # (150, H) is on the edge y = H, yet outside.
    assert [triangle_contains(v, z2_top_left) for v in z2_top_left] == [True, False, False]
    assert [triangle_contains(v, z2_bottom_right) for v in z2_bottom_right] == [True, True, True]


def test_triangle_edges_tower2_bottom_right_worked_out() -> None:
    W = 2440.0
    z2 = tower_zones(W, 1220.0, 1)[1]
    # All three edges and corners count as inside (worked out from the formula, spec 08).
    inside = [(W - 75.0, 0.0), (W, 75.0), (W - 75.0, 75.0), (W, 0.0), (W - 150.0, 0.0), (W, 150.0), (W - 10.0, 10.0)]
    outside = [(W - 75.0, -0.001), (W + 0.001, 75.0), (W - 76.0, 76.0), (W - 151.0, 0.0)]
    assert [triangle_contains(q, z2) for q in inside] == [True] * len(inside)
    assert [triangle_contains(q, z2) for q in outside] == [False] * len(outside)


def test_triangle_formula_counts_a_ray_beyond_vertex_a_as_inside() -> None:
    # Spec 08's exact test, applied as written: when A < 0 (Z1, and Z2 at
    # (0, H)), a point with s = 0 exactly passes the sign check whatever t is,
    # so the half-line from vertex a pointing away from c counts as inside. For
    # Z1 that is y = 0, x < 0. Spec 08 marks the test's behaviour well outside
    # the triangle as worked out, not verified; this module follows the formula.
    W, H = 2440.0, 1220.0
    z1, z2_top_left = tower_zones(W, H, 0)
    assert triangle_contains((-402.0, 0.0), z1)
    assert triangle_contains((-0.001, 0.0), z1)
    assert not triangle_contains((-402.0, 1e-9), z1)
    assert not triangle_contains((-402.0, -1e-9), z1)
    assert triangle_contains((-100.0, H - 250.0), z2_top_left)  # beyond (0, H − 150), away from (150, H)
    assert not triangle_contains((-100.0, H - 249.0), z2_top_left)
    # A > 0 (Z2 at (W, 0)): no such ray; beyond a along either edge is outside.
    z2_bottom_right = tower_zones(W, H, 1)[1]
    assert not triangle_contains((W - 300.0, 0.0), z2_bottom_right)
    assert not triangle_contains((W - 300.0, -150.0), z2_bottom_right)
    # Through evaluate: at C = 270 the rear support of a tool point on y = 0 lies
    # on that ray (sin 0 = 0 exactly), and counts as a tower hit.
    assert evaluate(270, [(300.0, 0.0)], W, H, 0)[2] is True
    assert evaluate(270, [(300.0, 0.001)], W, H, 0)[2] is False


def test_tower2_selects_the_zone_that_is_tested() -> None:
    W, H = 2440.0, 1220.0
    near_bottom_right = _point_with_support_at((W - 40.0, 40.0), 200, 0)
    near_top_left = _point_with_support_at((40.0, H - 40.0), 20, 0)
    assert evaluate(200, [near_bottom_right], W, H, 1)[2] is True
    assert evaluate(200, [near_bottom_right], W, H, 0)[2] is False
    assert evaluate(20, [near_top_left], W, H, 0)[2] is True
    assert evaluate(20, [near_top_left], W, H, 1)[2] is False


def test_tower_hit_refuses_in_both_passes() -> None:
    W, H = 2440.0, 1220.0
    # Forced C = 320: the rear support lands in Z1 at (40, 40); the others are on the panel.
    in_zone = _point_with_support_at((40.0, 40.0), 320, 0)
    _theta, (x0, x1, y0, y1), hit = evaluate(320, [in_zone], W, H, 0)
    assert hit and x0 >= 0 and y0 >= 0 and x1 <= W and y1 <= H
    refused = select_heading_vendor([in_zone], W, H, NO_SIDES, 0, 320)
    assert math.isnan(refused.theta) and refused.pass_ is None
    # Just past the hypotenuse it is accepted in pass 1.
    clear = _point_with_support_at((80.0, 80.0), 320, 0)
    accepted = select_heading_vendor([clear], W, H, NO_SIDES, 0, 320)
    assert (accepted.pass_, accepted.c) == (1, 320)
    # Pass 2 skips a hit too: a wheel in the zone and another past the left edge.
    both = [in_zone, (100.0, 610.0)]
    assert evaluate(320, both, W, H, 0)[1][0] < 0
    assert math.isnan(select_heading_vendor(both, W, H, NO_SIDES, 0, 320).theta)


# --------------------------------------------------------------------------
# Candidate search and selection


def test_candidates() -> None:
    assert candidates(-1) == GRID and len(GRID) == 37
    assert candidates(0) == (0,) and candidates(360) == (360,) and candidates(350) == (350,)


def test_pass1_is_first_fit_not_best_margin() -> None:
    # C = 0 puts the rear support 2 mm past the bottom edge; C = 10 fits with
    # 4 mm to spare, and wins although C = 180 would leave far more margin.
    W, H = 2440.0, 1220.0
    P = [(1220.0, 400.0)]
    assert evaluate(0, P, W, H, 0)[1][2] < 0
    result = select_heading_vendor(P, W, H, NO_SIDES, 0, -1)
    assert (result.pass_, result.c, result.overhang) == (1, 10, ZERO_OVERHANG)
    assert 0 < evaluate(10, P, W, H, 0)[1][2] < 5
    assert evaluate(180, P, W, H, 0)[1][2] > 300


def test_pass2_prefers_fewer_sides_then_smaller_total() -> None:
    W, H = 500.0, 500.0
    P = [(250.0, 250.0)]
    keys = _pass2_keys(P, W, H, NO_SIDES, 0)
    assert {k for k, _s in keys.values()} >= {1, 2}  # both one-side and two-side survivors exist
    best_i = min(keys, key=lambda i: (keys[i], i))
    result = select_heading_vendor(P, W, H, NO_SIDES, 0, -1)
    assert result.pass_ == 2 and result.candidate == best_i
    assert keys[best_i][0] == 1
    # The returned overhang is max(0, ·) on every side, and only the overhung side is non-zero.
    _theta, box, _hit = evaluate(best_i, P, W, H, 0)
    x0, x1, y0, y1 = box
    assert result.overhang == {
        "left": max(0.0, -x0),
        "right": max(0.0, x1 - W),
        "bottom": max(0.0, -y0),
        "top": max(0.0, y1 - H),
    }
    assert sum(1 for v in result.overhang.values() if v > 0) == 1


def test_blocked_sides_exclude_candidates() -> None:
    W, H = 500.0, 500.0
    P = [(250.0, 250.0)]
    free = select_heading_vendor(P, W, H, NO_SIDES, 0, -1)
    side = next(s for s, v in free.overhang.items() if v > 0)
    blocked = select_heading_vendor(P, W, H, frozenset({side}), 0, -1)
    assert blocked.pass_ == 2 and blocked.candidate != free.candidate
    assert blocked.overhang[side] == 0.0
    # Every pass-2 survivor overhangs at least one side, so blocking all four leaves none.
    nothing = select_heading_vendor(P, W, H, ALL_SIDES, 0, -1)
    assert math.isnan(nothing.theta) and nothing.overhang == {} and not nothing.found


def test_overhang_sum_is_plain_left_to_right() -> None:
    # Pinned reading of "S = sum(values of ov)": IEEE doubles added left to right
    # in the order the pseudo-code inserts them (left, right, bottom, top). Here a
    # correctly rounded sum (math.fsum, or Python's own sum() since 3.12) picks
    # another heading, so the reading is observable.
    W, H = 182.8, 184.0
    P = [(91.4, 177.6), (91.4, -16.9)]
    plain = _pass2_keys(P, W, H, NO_SIDES, 1)
    exact = _pass2_keys(P, W, H, NO_SIDES, 1, summer=math.fsum)
    assert min(plain, key=lambda i: (plain[i], i)) == 240
    assert min(exact, key=lambda i: (exact[i], i)) == 60
    result = select_heading_vendor(P, W, H, NO_SIDES, 1, -1)
    assert (result.pass_, result.candidate) == (2, 240)


def test_candidate_table_explains_the_choice() -> None:
    W, H = 300.0, 300.0
    P = [(150.0, 150.0)]
    rows = candidate_table(P, W, H, frozenset({"right"}), 0, -1)
    assert [row.candidate for row in rows] == list(GRID)
    for row in rows:
        assert (row.theta, row.box, row.tower_hit) == evaluate(row.candidate, P, W, H, 0)
        assert row.overhang == _ov(row.box, W, H)
        assert row.blocked == tuple(s for s in ("left", "right", "bottom", "top") if s in row.overhang and s == "right")
        assert row.fits_panel == (not row.tower_hit and not row.overhang)
        assert row.pass2_survivor == (not row.tower_hit and not row.blocked)
        assert row.sides_overhung == len(row.overhang)
        assert row.total_overhang == _plain_sum(row.overhang.values())
    survivors = [row for row in rows if row.pass2_survivor]
    winner = min(survivors, key=lambda row: (row.sides_overhung, row.total_overhang, row.candidate))
    assert select_heading_vendor(P, W, H, frozenset({"right"}), 0, -1).candidate == winner.candidate == 230
    assert candidate_table(P, W, H, NO_SIDES, 0, 7) == ()
    assert [row.candidate for row in candidate_table(P, W, H, NO_SIDES, 0, 360)] == [360]


def test_integer_inputs_equal_float_inputs() -> None:
    as_float = select_heading_vendor([(1220.0, 400.0)], 2440.0, 1220.0, NO_SIDES, 0, -1)
    as_int = select_heading_vendor([(1220, 400)], 2440, 1220, NO_SIDES, 0, -1)
    assert as_int == as_float
    assert evaluate(10, [(1220, 400)], 2440, 1220, 0) == evaluate(10, [(1220.0, 400.0)], 2440.0, 1220.0, 0)


# --------------------------------------------------------------------------
# Invalid input fails with a clear error; nothing is guessed


@pytest.mark.parametrize(
    "kwargs, error, words",
    [
        ({"B": frozenset({"Left"})}, ValueError, "unknown side"),
        ({"B": frozenset({"north"})}, ValueError, "unknown side"),
        ({"B": "left"}, TypeError, "set of side names"),
        ({"B": frozenset({1})}, TypeError, "side names"),
        ({"tower2": 2}, ValueError, "tower2"),
        ({"tower2": -1}, ValueError, "tower2"),
        ({"tower2": True}, TypeError, "tower2"),
        ({"tower2": 1.0}, TypeError, "tower2"),
        ({"F": 10.0}, TypeError, "F"),
        ({"F": True}, TypeError, "F"),
        ({"F": None}, TypeError, "F"),
        ({"W": 0.0}, ValueError, "W"),
        ({"W": -1.0}, ValueError, "W"),
        ({"H": math.nan}, ValueError, "H"),
        ({"H": math.inf}, ValueError, "H"),
        ({"W": True}, TypeError, "W"),
        ({"W": "2440"}, TypeError, "W"),
        ({"P": [(1.0, math.nan)]}, ValueError, "P[0]"),
        ({"P": [(1.0, 2.0), (math.inf, 2.0)]}, ValueError, "P[1]"),
        ({"P": [(1.0, 2.0, 3.0)]}, ValueError, "P[0]"),
        ({"P": [(1.0,)]}, ValueError, "P[0]"),
        ({"P": [("1", 2.0)]}, TypeError, "P[0]"),
        ({"P": [(False, 2.0)]}, TypeError, "P[0]"),
    ],
)
def test_invalid_input_is_refused(kwargs: dict, error: type, words: str) -> None:
    args = {"P": [(1220.0, 610.0)], "W": 2440.0, "H": 1220.0, "B": NO_SIDES, "tower2": 0, "F": -1}
    args.update(kwargs)
    with pytest.raises(error, match=r"(?i)" + words.replace("[", r"\[").replace("]", r"\]")):
        select_heading_vendor(args["P"], args["W"], args["H"], args["B"], args["tower2"], args["F"])


@pytest.mark.parametrize(
    "i, error", [(5, ValueError), (370, ValueError), (-10, ValueError), (10.0, TypeError), (True, TypeError)]
)
def test_evaluate_refuses_a_candidate_off_the_grid(i: object, error: type) -> None:
    with pytest.raises(error, match="candidate"):
        evaluate(i, [(1220.0, 610.0)], 2440.0, 1220.0, 0)  # type: ignore[arg-type]


def test_support_points_refuses_off_grid_and_non_finite() -> None:
    with pytest.raises(ValueError, match="candidate"):
        support_points((0.0, 0.0), 15)
    with pytest.raises(ValueError, match="p"):
        support_points((math.nan, 0.0), 0)


def test_point_set_may_be_any_iterable_read_once() -> None:
    points = [(1220.0, 400.0)]
    assert select_heading_vendor(iter(points), 2440.0, 1220.0, NO_SIDES, 0, -1) == select_heading_vendor(
        points, 2440.0, 1220.0, NO_SIDES, 0, -1
    )


# --------------------------------------------------------------------------
# Architecture: pure Python, own arithmetic, no test code in the package


def test_module_is_pure_standard_library(astscan: ModuleType) -> None:
    module = str(MODULE_FILE)
    # Positive control: the scan sees the module's imports.
    assert astscan.find_references(module, "math")
    for name in ("shapely", "numpy", "goliath_link"):
        assert astscan.find_references(module, name) == [], name


def test_no_compensated_or_builtin_sum(astscan: ModuleType) -> None:
    # Python's sum() of floats is compensated since 3.12, and math.fsum always:
    # either would change spec 08's pass-2 ties (test_overhang_sum_is_plain_left_to_right).
    module = str(MODULE_FILE)
    assert astscan.find_calls(module, "cos")  # positive control
    assert astscan.find_calls(module, "sum") == []
    assert astscan.find_calls(module, "fsum") == []


def test_package_never_imports_the_literal_transcription(astscan: ModuleType) -> None:
    name = "spec08_literal"
    # Positive control (tests/arch/astscan.py: an absence guard must show that its
    # query finds a known example): the conftest loads the transcription by its
    # file name, and the same query finds that string there. A typo in ``name``
    # now fails here instead of making the two absence checks pass vacuously.
    conftest = str(Path(__file__).resolve().parent / "conftest.py")
    assert any(loc.text == name + ".py" for loc in astscan.find_string_literals(conftest, name))
    assert astscan.find_references("goliath_prep", name) == []
    assert astscan.find_string_literals("goliath_prep", name) == []
