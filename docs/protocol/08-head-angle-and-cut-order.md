# 08 — Head angle (C) selection and cut ordering

*Source: static analysis of the decompiled `WorkLogicLibrary` and editor code in Slingshot 1.9.3.0, with the footprint constants and the `C` formula cross-checked in 1.7.12, 1.8.0, 1.9.1 and 1.09.05. Verified: 44 of 47 facts confirmed by an independent re-read of the code; 2 were refuted and are corrected below; 1 is unsupported and listed under Caveats. Written in our own words; no vendor code is reproduced.*

> **Status: informative.** This section describes how the reference host software (Slingshot 1.9.3.0) chooses the `C` value for each cut and the order in which it emits cuts. It was worked out by static analysis and is written independently of the vendor code. The footprint constants, the candidate grid and the `C` formula are the same in 1.7.12, 1.8.0, 1.9.1 and 1.09.05. Nothing here has been checked against captured G-code yet. Open points are listed under *Caveats*.

## Purpose

The machine is a mobile router. It stands on the panel and drives across it on its own wheels, and the spindle sits at a fixed offset from the wheels. The `C` word sets the robot's yaw about the tool axis. With the tool held at one position, changing `C` swings the body, and every wheel with it, around the tool point.

The software models two positioning towers at the two ends of one panel edge. For each cut, the host picks a single heading that is held for the whole cut. At every vertex of the toolpath, that heading must satisfy two conditions:

1. Every wheel rests on the panel. If that is impossible, wheels may hang only over sides where the panel can be extended with support material.
2. No wheel enters the triangular keep-out zone at either tower corner.

The host makes this choice offline, when it generates G-code; the machine just executes the resulting `C`. Consecutive cuts are not kept at similar headings, because no previous-angle input is used.

## Coordinate frame and inputs

- Units are millimetres and degrees. Angles are measured counter-clockwise from +X, and `u(α) = (cos α, sin α)`.
- The origin is the corner where the first tower stands. The panel occupies `[0, W] × [0, H]`.

| Input | Description |
|---|---|
| `W`, `H` | Panel width and height (the work-area dimensions). |
| `P` | Point set: every toolpath vertex of all operations evaluated together (see below). |
| `B` | Blocked sides, a subset of {left, right, bottom, top}. A wheel may never hang over a blocked side. |
| `tower2` | Where tower 2 stands: `0` means corner `(0, H)`, any other value means `(W, 0)`. |
| `F` | Forced `C` value, in `C` units (degrees), or `−1` for none. |

The reference code also takes a "previous angle" argument but never uses it.

`B` comes from panel detection. Each edge the operator flags blocks the *opposite* side, so flagging bottom and left blocks top and right. If both bottom and top are flagged, bottom wins. In practice `B` is empty, one side, or two adjacent sides. If nothing is flagged, `B` keeps its previous value.

The point set `P` is built as follows:

- Only operations whose tool is a router bit contribute points. All other operations contribute none.
- A profile or pocket contributes every stored vertex of its tool-radius-compensated toolpath, which is the same path handed to the post-processor.
- A drill whose diameter equals the bit diameter contributes only its centre point.
- A drill with a different diameter `d` is meant to be replaced by a circle of diameter `d` starting at `(cx − d/2, cy)`. The reference code very likely fails at this step (see *Caveats*).
- An inside-compensated circular profile or pocket whose radius is within `[r − 0.01, r + 0.11]` mm of the bit radius `r` is first turned into a drill, so it contributes only its centre.
- The panel-outline pseudo-operation contributes nothing.
- If `P` is empty, the first candidate is accepted, giving `C = 0`.

## Footprint model

For a candidate index `i` (in `C` units), the heading is:

```text
θ(i) = (i + 90) mod 360
```

For each tool point `p` in `P`, three support points are computed. They are assumed to be wheel contact points (see *Caveats*).

```text
q1 = p − 402 · u(θ)
q2 = p − 211 · u(θ + 76°)
q3 = p − 211 · u(θ − 76°)
```

So `u(θ)` points from the body toward the tool.

The following values are worked out from the formulas above; they are not constants in the source. At `C = 0` (θ = 90°):

- `q1 = p + (0, −402)`
- `q2 ≈ p + (204.7, −51.0)`
- `q3 ≈ p + (−204.7, −51.0)`

These form a nearly equilateral triangle with sides of about 406 to 410 mm, whose centroid is about 168 mm behind the tool. Increasing `C` rotates this triangle counter-clockwise about `p`.

Implementation notes:

- **Precision.** Use IEEE-754 doubles and compute cos/sin of `θ·π/180`. Every comparison below is exact, so tiny residuals decide boundary cases. For example, cos(90°) evaluates to about 6.1·10⁻¹⁷, not 0. A vertex exactly on `x = 0` therefore puts `q1` a few 10⁻¹⁴ mm outside the panel.
- **Sampling.** Only vertices are tested. Because the heading is fixed during a cut, each wheel moves in a straight line between its positions at the two ends of a straight segment. The bounding box is therefore exact for straight segments. However:
  - a wheel can cross a tower zone between two vertices without being detected;
  - arc bulges are checked only if arcs are stored as many small segments.

## Tower keep-out zones

Each tower has a triangular keep-out zone for the wheels, with 150 mm legs:

- `Z1` = `(0, 0), (0, 150), (150, 0)`
- `Z2` = `(0, H−150), (0, H), (150, H)` if `tower2 = 0`; otherwise `(W−150, 0), (W, 0), (W, 150)`

Whether a point exactly on an edge counts as inside depends on the vertex order. A compatible implementation must therefore use this exact test, with vertices `(a, b, c)` in the order listed above and `u × v = u.x·v.y − u.y·v.x`:

```text
t = (b − a) × (q − a)
s = (q − a) × (c − a)
A = (b − a) × (c − a)
if (s < 0) ≠ (t < 0):  outside
elif A < 0:            inside  ⇔  s ≤ 0 and s + t ≥ A
else:                  inside  ⇔  s ≥ 0 and s + t ≤ A
```

Edge behaviour of this test:

- **`Z1` (verified):** the hypotenuse and the corner `(0, 0)` count as inside. The legs on `x = 0` and `y = 0` count as outside, except at the corner itself.
- **`Z2` with `tower2 = 0` (verified):** the edge `y = H` counts as inside. The hypotenuse and the edge `x = 0` count as outside.
- **`Z2` with `tower2 ≠ 0` (worked out from the formula, not verified):** all three edges count as inside, including `y = 0` and `x = W`.

So a wheel sitting exactly on a panel edge inside a tower corner is not flagged at tower 1, nor at tower 2 when it stands at the top-left.

There is also a separate check on the *toolpath*, which runs before any angle search. A cut is refused outright if any part of its toolpath is not strictly outside a 155 mm triangle at a tower corner. That check locates tower 2 from the work-area perimeter rather than from `tower2` (see *Caveats*).

## Candidate search and selection

The candidates are `i = 0, 10, 20, …, 360` in ascending order, 37 values in all. Candidate 360 gives exactly the same geometry as 0, so it can only win when forced.

If `F ≠ −1`, the only candidate is the `i` that equals `F` exactly. If `F` is not on the grid, there is no candidate and the search fails.

```text
function select_heading(P, W, H, B, tower2, F):
    candidates = [i for i in 0, 10, …, 360 if F == −1 or i == F]

    function evaluate(i):
        θ = (i + 90) mod 360
        (x0, x1, y0, y1) = (W, 0, H, 0)              # running min x, max x, min y, max y
        hit = false
        for p in P:
            for (r, δ) in ((402, 0°), (211, +76°), (211, −76°)):
                q  = p − r · u(θ + δ)
                x0 = min(x0, q.x);  x1 = max(x1, q.x)
                y0 = min(y0, q.y);  y1 = max(y1, q.y)
                hit = hit or q ∈ Z1 or q ∈ Z2
        return θ, (x0, x1, y0, y1), hit

    # Pass 1: first fit, no scoring
    for i in candidates:
        θ, (x0, x1, y0, y1), hit = evaluate(i)
        if not hit and x0 ≥ 0 and x1 ≤ W and y0 ≥ 0 and y1 ≤ H:
            return θ, overhang = (left 0, right 0, bottom 0, top 0)

    # Pass 2: allow wheels past non-blocked sides
    best = none;  bestK = 5;  bestS = W
    for i in candidates:
        θ, (x0, x1, y0, y1), hit = evaluate(i)
        if hit: continue
        ov = {}
        if x0 < 0: ov[left]   = −x0
        if x1 > W: ov[right]  = x1 − W
        if y0 < 0: ov[bottom] = −y0
        if y1 > H: ov[top]    = y1 − H
        if any key of ov is in B: continue
        K = count(ov);  S = sum(values of ov)
        if K < bestK or (K == bestK and S < bestS):
            best = (θ, x0, x1, y0, y1);  bestK = K;  bestS = S
    if best is none: return NaN
    return θ_best, overhang = (left   = max(0, −x0),  right = max(0, x1 − W),
                               bottom = max(0, −y0),  top   = max(0, y1 − H))
```

Points to note:

- **Box start values.** The bounding box starts at `(W, 0, H, 0)`. This makes no difference when `P` has points. When `P` is empty, it makes the box count as inside the panel, so pass 1 accepts the first candidate.
- **Pass 1 is first fit.** It does no scoring, so the lowest acceptable `C` wins even if another heading would leave more margin.
- **Pass 2 ranking.** Pass 2 runs only when pass 1 finds nothing. It prefers fewer sides overhung, then smaller total overhang in mm.
  - Comparisons are strict, so on an exact tie the lower `C` wins.
  - Starting `bestK` at 5 guarantees the first surviving candidate is taken.
  - Every survivor overhangs at least one side; otherwise it would have passed pass 1.
- **Tower test.** It applies in both passes. Wheel positions outside the panel only reach the test in pass 2.

## Result, failure and support extension

On success:

- The heading is stored on every cut in the call, together with the per-side overhang values. These are all zero for a pass-1 result.
- The overhang is how far the panel must be extended with support on that side. The work area needs, per side, the largest such value over all non-deleted cuts, or 0 if there are no cuts.

On failure (`NaN`):

- That cut's per-side values are set to 0.
- Once all angles have been computed, generation is refused for the whole job if any cut has a `NaN` angle. The vendor shows an error saying the machine cannot work with the current track position.

## Which cuts are evaluated together

When generating G-code, the reference host works through these steps in order:

1. **Pre-check.** If any cut, ignored cuts included, lies outside the canvas or fails the 155 mm toolpath tower check, generation is refused.
2. **Group mode.** This is a work-area setting. The *Manual sort* checkbox turns it on together with manual ordering. For each group ID, the points of all member cuts are pooled and evaluated once with `F = −1`, and every member gets the result. Groups include ignored cuts and pockets; neither is filtered out.
3. **Single cuts.** Every remaining cut that is not ignored and is not a non-through pocket is evaluated alone, using its own forced angle `F`. In group mode, "remaining" means cuts with no group.
4. **Non-through pockets.** All pockets not flagged as through cuts are pooled, ignored ones included, and evaluated once with `F = −1`. They all get the same heading. This overrides any heading a pocket got from its group, and per-pocket forced angles are ignored.
5. **Refusal.** If any cut's angle is `NaN`, generation is refused.

A cut whose operation cannot be built, for example because its path is empty, gets the angle −∞ rather than `NaN`. It passes step 5, and generation aborts at a later stage instead.

## Emitting the C word

The heading θ is kept internally. The post-processor writes:

```text
C = (θ − 90 + 360) mod 360        printed with no decimal places
```

This always equals `i mod 360`, so every emitted value is a multiple of 10 in `[0, 350]`. To get the heading back from a `C` value, use `θ = (C + 90) mod 360`. The forced-angle input uses the same units as `C`.

`C` is modal: it is written only when its printed text differs from the last `C` written. The remembered value is cleared, and `C` is written again, at three points:

- the start of the program;
- after a tool change (`M06`);
- after a dust dump (`M11`).

The host's manual-jog reachability check uses the same footprint and the same convention, heading = entered rotation + 90°. It is stricter at the boundary: every wheel must lie strictly inside the panel, and no overhang is allowed.

## Cut ordering

Ordering runs after angle selection and never changes the angles. It has seven stages:

1. **Collect.** The host lists the operations in the UI order of the cuts, skipping ignored cuts. The panel-outline pseudo-operation is placed first and then removed.
2. **Rotate into a working frame.**
   - Let θs be the heading (not the `C` value) of the first pocket operation in the list. This pocket may be a through pocket with its own angle.
   - All geometry is rotated about the origin by `φ = 180° − θs`. If the list has no pocket, nothing is rotated.
   - If this rotation is counter-clockwise, the pocket heading becomes 180°, which puts the body on the +X side of the tool. The sign is unverified.
3. **Expand.** Tabs, ramps and depth passes are outside the scope of this section. In addition, each non-through closed pocket is split into clearing strips:
   - The pocket outline is first shrunk inward by `0.75·D`, where `D` is the bit diameter.
   - The shrunk outline is cut into vertical strips at fixed positions in the rotated frame. Strip `k` covers `x ∈ [9970 − 27k, 10000 − 27k]`, for `k = 0, 1, …` down to `x = −10000`. That gives 30 mm strips, 27 mm apart, overlapping by 3 mm. They are created from high x to low x.
   - Each non-empty piece becomes its own clearing operation with priority 100. If no piece results, the whole pocket becomes one clearing operation. The original pocket outline operation stays in the list.
   - The clearing stepover is `D·(1 − min(p, 90)/100)`, where `p` is `OVERLAP_PERC`. The default is 20; 0 means use the tool's own value.
   - If the depth `d` is more than 2 mm, the clearing is cut in `n = ⌈d/2⌉` passes of `d/n` each. These passes are merged into one operation that alternates direction, and its recorded depth becomes 0.0001.
4. **Sort.** All sorting takes place in the rotated frame, so "x" and the reference point below refer to that frame. See *Sorting*.
5. **Insert extra moves.** See *Extra moves*.
6. **Rotate back** by `−φ`.
7. **Post-process** the operations in list order.

### Sorting

**Comparator K1** compares two operations on these keys, in order:

1. Machining-face code, ascending.
2. Tool sort class, ascending. Used only when the face codes are equal and both operations carry tool data.
3. Priority, ascending. The default priority is presumed to be 0; clearing strips have 100.
4. Operation-type code, compared as text.
5. Tool name, using culture-sensitive text comparison.
6. Drills by diameter, ascending. Pockets and profiles by `depth·z`, ascending. Every call made here uses `z = −1`.

**Comparator K2(ref)** compares two operations relative to a reference point `ref`:

1. If either operation has no start point, the two count as equal.
2. Priority, ascending.
3. Start-point x, ascending.
4. Weighted squared distance from `ref` to the start point, ascending: `100000·Δx² + ry·Δy² + Δz²`. `ry` is presumed to be 1.
5. `depth·z`, descending. With `z = −1`, the shallower operation comes first.

Because start x is compared before distance, each group runs in ascending x of the rotated frame. Distance only decides between operations whose start x is exactly equal.

**SORT2(L)** sorts the list with K1, then reorders each run of similar operations with K2:

```text
sort L with K1      # unstable introsort; order among equal keys depends on the algorithm
N = |L|;  i = 0
while i < N:
    g = L[i];  m = 1
    while i + m < N − 1 and L[i+m] has the same face, tool and priority as g:
        m = m + 1
    ref = (−100000, −100000, 0);  cur = g
    for j in 0 .. m − 1:
        end = i + m
        if i + m == N − 1 and type(L[N−1]) == type(cur):
            end = N                                   # the list's last element may join
        sort L[i+j .. end−1] with K2(ref)
        cur = L[i+j]
        ref = end point of cur, or (−100000, −100000, 0) if it has none
    i = i + m
```

**GREEDY(L, n)** reorders the first `n` entries of the list locally. Reproduce it exactly as written to get identical output. It tends to move the operation with the nearest entry point to the front, but it is not a strict nearest-neighbour search.

```text
entry(o, cur) = end point of o   if o is an open path and cur is not a drill
                start point of o otherwise
p = (−100000, −100000, 0)
for i in 0 .. n−1:
    cur  = L[i]
    last = highest index whose operation has the same internal label and tool as cur
           # computed once per i and not updated after moves
    j = i + 1
    while j ≤ last:
        if i > 0:  p = entry(L[i−1], cur)
        if dist(p, entry(cur, cur)) > dist(p, entry(L[j], cur)):
            remove L[i] and insert it at index j    # rotates L[i..j] left by one
            cur = L[i]
        else:
            j = j + 1
```

The window is a range of positions in the list. Operations inside it are not filtered by label or tool. Ordinary cuts appear to have an empty internal label.

**Which sort runs** depends on the *Manual sort* setting and on whether any clearing strips exist:

| Manual sort | Clearing strips | Ordering |
|---|---|---|
| on | either | Other operations keep UI order. The clearing strips are taken out, sorted with SORT2 and appended at the end. |
| off | none | Stable sort by tool name (culture-sensitive), then `GREEDY(L, N)`. |
| off | present | `SORT2` over the whole list, then `GREEDY(L, n)`, where `n` is the position of the first clearing strip. |

In the last case, operations that SORT2 places after the first strip are not reordered by GREEDY. With manual sort on, the host also reorders its UI cut list to match the order it generated.

### Extra moves

After sorting, still in the rotated frame, the host inserts tool-change and dust-dump moves:

```text
T = TANK_VOLUME · 10⁶     # TANK_VOLUME defaults to 1.15 dm³, so T = 1.15·10⁶ mm³
V = 0
for k, o in enumerate(operations):
    V = V + vol(o)
    if k > 0 and tool(o) ≠ tool(previous operation):
        insert a tool-change move before o
    if k > 0 and V ≥ T:
        insert a dust-dump move before o, after any tool-change move
        V = 0
```

`vol(o)` estimates the removed volume by operation type:

| Operation | Estimated volume |
|---|---|
| Profile | length × depth × width |
| Through pocket or clearing strip | area × depth |
| Other closed pocket (e.g. the kept outline of a non-through pocket) | length × depth × 6 |
| Drill | 0 |

The operation that pushes the total past the threshold gets the dump move placed in front of it. Its own volume is then dropped rather than carried into the next count.

Both extra moves are built from `(xe, ye)`, the end point of the previous operation:

- **Tool-change move:** one point at `(xe, ye, 5)`.
- **Dust-dump move:** two points, `(xe − 0.001, ye, 5)` and `(xe, ye − 0.001, 5)`.

When both are inserted, the order is: previous operation, tool-change move, dust-dump move, current operation. The post-processor emits `M06` for a tool change and `M11` for a dust dump.

The `GOLIATH_SECURITY_DIM` setting (400 mm) does not affect either the angle or the order. It is used only for two things:

- an offset of the panel outline, applied only when the outline is larger than 800 mm in both directions, whose result is never used;
- the default target shown in the jog dialog.

## Constants

| Constant | Value | Used in |
|---|---|---|
| Rear support distance | 402 mm | q1, along −u(θ) |
| Side support distance | 211 mm | q2, q3 |
| Side support angle | ±76° from the heading | q2, q3 |
| Candidate grid | 0 to 360 in steps of 10 (37 values) | index `i`, in `C` units |
| Heading offset | +90° | θ = (C + 90) mod 360 |
| Wheel tower zone | 150 mm legs | Z1, Z2 |
| Toolpath tower zone | 155 mm legs | pre-check |
| Pass-2 starting best | K = 5, S = W | selection |
| Sort reference point | (−100000, −100000, 0) | GREEDY, SORT2 |
| X distance weight | 100000 | K2 |
| Depth sign `z` | −1 | K1, K2 |
| Strip width / spacing / overlap | 30 / 27 / 3 mm | pocket split |
| Strip grid extent | ±10000 mm | pocket split |
| Pocket inward offset | 0.75·D | pocket split |
| Clearing priority | 100 | K1, K2 |
| Clearing maximum pass depth | 2 mm | depth passes |
| `TANK_VOLUME` | 1.15 dm³ | dust dump |
| `OVERLAP_PERC` | 20 % | clearing stepover |
| Extra-move Z | 5 mm | inserted moves |
| `GOLIATH_SECURITY_DIM` | 400 mm | no effect on angle or order |

## Caveats

- **What the support points are.** Calling q1–q3 wheels rests only on naming and on a "40cm free area" message in the jog dialog. They could be wheel centres, contact patches or a padded footprint. It is also unconfirmed that `u(θ)` is the robot's physical forward direction.
- **Arcs.** It is unknown whether arcs in the compensated path are stored as many small segments. If they are stored only by their endpoints, arc bulges are not checked. A compatible implementation should break arcs into segments. That can only make it more cautious than the reference, so its results may differ.
- **Drills that don't match the bit.** Replacing such a drill with a circle changes the operation list while the code is looping over it. On .NET this should throw and abort generation. How these drills actually behave is unconfirmed.
- **Point-in-triangle test.** Only part of the reference test's final comparison was visible. The form given here reproduces every verified edge case. Its behaviour for points well outside the triangle, which matters only in pass 2, and the edge behaviour for `tower2 ≠ 0` are worked out, not verified.
- **Where tower 2 is.** The 155 mm toolpath check finds tower 2 from the work-area perimeter. The 150 mm wheel check finds it from `tower2` and W/H. It is not proven that the two always agree.
- **Panel axes.** How the panel axes map onto the physical machine is not established, and with it the physical meaning of "counter-clockwise".
- **Working-frame rotation.** Its sign is unverified. The sign decides whether stepping through strips in ascending x moves the robot toward its own body or away from it.
- **K2 key order.** K2 compares x before distance, so the distance weight only matters for exact ties in x. This matches the analysed code but is unusual and could be an artefact of decompilation. Confirm it against captured G-code.
- **Hidden values in the sorting library.** Several were obfuscated: the default priority (presumed 0), the Y distance weight (presumed 1), the default depth sign, and the operation-type codes and their text forms.
- **Sort ties.** Ties under K1 are broken by an unstable sort, and text comparisons depend on culture. Reproducing the order exactly needs the same sort algorithm and the same text-comparison rules.
- **Distance in GREEDY.** It is unknown whether this distance is in the plane or includes Z.
- **Post-processor order.** The post-processor's path builder was not analysed. It is assumed to keep list order.
- **Group mode with unbuildable cuts.** A group member whose operation cannot be built would be passed as null and would probably crash. The pre-check appears to prevent this, but that is not proven.
- **Startup value of `B`.** The value of `B` at startup, before any panel detection, was not determined.
- **Tabs.** It is not verified that the toolpath used for the angle search includes tabs.
- **Operation labels.** The internal label that bounds the GREEDY window is presumed to be empty for ordinary cuts.
- **Bookkeeping during insertion.** Only the resulting order of the inserted moves was traced. How the code keeps track of list positions while inserting them was not.
