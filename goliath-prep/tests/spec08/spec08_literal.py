# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""A literal transcription of spec 08's heading search, kept for differential tests.

Source: docs/protocol/08-head-angle-and-cut-order.md, sections *Footprint
model*, *Tower keep-out zones* and *Candidate search and selection*. This file
follows the pseudo-code line by line, in the spec's own names and order, and
is deliberately written without any of the production module's structure
(no precomputed tables, no input checks), so that the two can be compared
bit for bit.

Where the spec leaves a detail of IEEE arithmetic open, this file takes the
same reading as goliath_prep.heading_vendor, and the reading is pinned by a
named test in test_prep_heading_vendor.py:

* ``u(α)`` takes cos and sin of ``α·π/180``, evaluated left to right as
  ``(α·π)/180``, with ``α = θ + δ`` not reduced modulo 360
  (test_angle_is_alpha_times_pi_over_180_unreduced);
* ``sum(values of ov)`` adds the values left to right, in the order the
  pseudo-code inserts them (left, right, bottom, top), starting from 0;
  not Python's compensated ``sum`` of 3.12+ (test_overhang_sum_is_plain_left_to_right);
* ``min``/``max`` keep the running value unless the new one is strictly
  smaller/larger (Python's builtins do exactly that).

This module is test code, not a second implementation to call: nothing in
goliath_prep may import it.
"""
from __future__ import annotations

import math

NaN = float("nan")


def u(alpha):
    # u(α) = (cos α, sin α), α in degrees: "compute cos/sin of θ·π/180".
    return (math.cos(alpha * math.pi / 180), math.sin(alpha * math.pi / 180))


def minus(a, b):
    return (a[0] - b[0], a[1] - b[1])


def cross(u_, v):
    # u × v = u.x·v.y − u.y·v.x
    return u_[0] * v[1] - u_[1] * v[0]


def inside(q, triangle):
    a, b, c = triangle
    t = cross(minus(b, a), minus(q, a))
    s = cross(minus(q, a), minus(c, a))
    A = cross(minus(b, a), minus(c, a))
    if (s < 0) != (t < 0):
        return False
    elif A < 0:
        return s <= 0 and s + t >= A
    else:
        return s >= 0 and s + t <= A


def zones(W, H, tower2):
    Z1 = ((0, 0), (0, 150), (150, 0))
    if tower2 == 0:
        Z2 = ((0, H - 150), (0, H), (150, H))
    else:
        Z2 = ((W - 150, 0), (W, 0), (W, 150))
    return Z1, Z2


def evaluate(i, P, W, H, tower2):
    Z1, Z2 = zones(W, H, tower2)
    theta = (i + 90) % 360
    (x0, x1, y0, y1) = (W, 0, H, 0)  # running min x, max x, min y, max y
    hit = False
    for p in P:
        for (r, delta) in ((402, 0), (211, +76), (211, -76)):
            ud = u(theta + delta)
            q = (p[0] - r * ud[0], p[1] - r * ud[1])
            x0 = min(x0, q[0])
            x1 = max(x1, q[0])
            y0 = min(y0, q[1])
            y1 = max(y1, q[1])
            hit = hit or inside(q, Z1) or inside(q, Z2)
    return theta, (x0, x1, y0, y1), hit


def select_heading(P, W, H, B, tower2, F):
    """Returns NaN, or (θ, overhang) with overhang a dict keyed left/right/bottom/top."""
    candidates = [i for i in range(0, 361, 10) if F == -1 or i == F]

    # Pass 1: first fit, no scoring
    for i in candidates:
        theta, (x0, x1, y0, y1), hit = evaluate(i, P, W, H, tower2)
        if not hit and x0 >= 0 and x1 <= W and y0 >= 0 and y1 <= H:
            return theta, {"left": 0, "right": 0, "bottom": 0, "top": 0}

    # Pass 2: allow wheels past non-blocked sides
    best = None
    bestK = 5
    bestS = W
    for i in candidates:
        theta, (x0, x1, y0, y1), hit = evaluate(i, P, W, H, tower2)
        if hit:
            continue
        ov = {}
        if x0 < 0:
            ov["left"] = -x0
        if x1 > W:
            ov["right"] = x1 - W
        if y0 < 0:
            ov["bottom"] = -y0
        if y1 > H:
            ov["top"] = y1 - H
        if any(key in B for key in ov):
            continue
        K = len(ov)
        S = 0
        for value in ov.values():
            S = S + value
        if K < bestK or (K == bestK and S < bestS):
            best = (theta, x0, x1, y0, y1)
            bestK = K
            bestS = S
    if best is None:
        return NaN
    theta_best, x0, x1, y0, y1 = best
    return theta_best, {
        "left": max(0, -x0),
        "right": max(0, x1 - W),
        "bottom": max(0, -y0),
        "top": max(0, y1 - H),
    }
