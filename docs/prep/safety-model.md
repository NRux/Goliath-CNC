<!-- SPDX-License-Identifier: Apache-2.0 -->
# The Goliath safety model, version 1

*Normative specification. It defines what a sender MUST check before a program
reaches a Goliath robot: the frames, the robot's footprint, the tower zones, the
work areas, the material removed over time and what still holds together, the
links and rotations between cuts, and the catalogue of checks with their
violation conditions, slack and witness. It is licensed Apache-2.0, like the
protocol specification in [`docs/protocol/`](../protocol/README.md), so that any
sender can implement the same checks ([`LICENSING.md`](../../LICENSING.md)).
Machine-readable companions, equally normative: [`checks.json`](checks.json)
(every check id) and [`params.json`](params.json) (every parameter, with its
provenance); [`parameters.md`](parameters.md) is the parameter table for people.
GI files are read under [`gi-format.md`](gi-format.md).*

**Status.** Nothing in this model has run on a robot. The firmware version of the
owner's robot is unknown until the read-only probe (rung R1), and every behaviour
here is gated and conservative while it is unknown. The model was written from the
protocol specification in `docs/protocol/`, the prior-art review in
`docs/research/` and the project's design review, without opening the vendor's
decompiled code or installers.

## 1. Status, scope and conventions

### 1.1 What this document is

`goliath-prep` reads a CAM program and a job description and either produces the
exact upload bytes with a certificate, or refuses with reasons. This document is
the safety half of that contract: the model the checks use and the checks
themselves. It covers the planning of one program on one panel (sections 3 to 11),
the job-time comparison with the panel the robot measured (section 12), series
jobs (section 13), the bring-up rung programs (section 14) and what the model can
and cannot certify today (section 15).

`checks.json` lists every check id with its family, severity, scope
(`applies_to`), violation condition, slack and witness fields, and the section
that defines it (`defined_in`). The catalogue table in section 11.6 is the same
list; the two are kept identical by a test. `params.json` lists every parameter a
check uses. `goliath-prep` turns both into code constants; a test keeps the code's
check ids and `checks.json` equal.

### 1.2 Words

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are used as in RFC 2119.

- **Refused**: a finding of severity `refuse`. A refused program is never
  emitted for upload, and a refused job is never started.
- **Warned**: severity `warn`: shown in the report and on the screen; it does not
  stop the job.
- **Disclosed**: severity `disclose`: a notice that is shown with the job
  wherever its certificate is shown, and recorded in it.
- **Internal error**: severity `internal-error`: an assertion of the checker
  failed. The job is refused and a bug bundle is saved; it is never a pass.
- **INFERRED** marks a statement this model reasons out instead of reading it in
  a source. Every inferred statement that matters for safety names the unknown
  (`U-nn`, section 1.4) it depends on.
- **This model's reading** marks a place where the project design left a choice
  open or contradicted itself, and this document fixes the safer reading.

Nothing is silent. Every transformation of the input is a normalisation note,
every default carries its provenance (parameters §2), every relaxation is
recorded in the certificate and shown, and every check that cannot be evaluated
says so in the report.

### 1.3 Citations and markers

- **spec NN §Heading** cites `docs/protocol/NN-*.md` under that heading; the
  same form with README in place of the number cites the specification's README.
  These are the only specification texts cited.
- **prior-art review §n** cites [`docs/research/prior-art-review.md`](../research/prior-art-review.md).
- **gi-format §n** cites [`gi-format.md`](gi-format.md); **parameters §n** cites
  [`parameters.md`](parameters.md); **safety-model §n** (used in the JSON files)
  cites this document.
- Rules that came out of the project's design review carry the review finding's
  id, for example (PHYS-07), as in `gi-format.md`.
- Check ids belong to families: `W` wheel, `B` body, `M` material and structure,
  `T` tool, `D` dialect and program, `P` panel geometry, `F` firmware, `G` feature
  gate, `S` series, `N` notices.

### 1.4 Unknowns, gates and rungs this model depends on

**Unknowns.** Each has a conservative default in this model until a bring-up rung
resolves it with evidence.

| Unknown | What is not known | Default in this model | Resolved by |
|---|---|---|---|
| `U-01` | the robot's firmware version | jobs refused while unknown (`F-GATE`) | R1 |
| `U-12` | the path panel detection takes ("traces the panel edge", spec 03 §Full job flow) | series jobs gated; a 450 mm edge band for series | R2, R15 |
| `U-13` | the `INFO;25` encoding, frame and vertex order; what `INFO;27` measures | every decoding tried, exactly one must pass; uploads need the identity frame | R2 |
| `U-15` | how the robot uses `SET PANEL SIZE` | integers, the floor of the validated area; the plan kept inside it | R2, R3 |
| `U-18` | the tool's Z when the program starts | the program raises to `Z_safety` with a Z-only `G01` before any XY move | R3 |
| `U-20` | the physical meaning of `C`: which way `u(θ)` points on the robot | the any-heading safe zone for every tool point (section 8) | R0b, R3 |
| `U-21` | how the robot turns on a `C` change: in place or while translating, which way, about which point | every emitted step below 180°; each rotation checked as a full disc; envelopes over both directions | R3 |
| `U-22` | what the support points physically are; the wheel contact radius; the body outline | spec footprint; `r_w` = 40 mm; body hull = wheel discs grown by 60 mm | R0b |
| `U-23` | Z semantics: zero at the stock top, sign, the separate rapid Z line | air jobs keep Z >= +5 mm until verified | R3 |
| `U-24` | the spindle words and whether motion waits for spin-up | no `M10` until R4; `G4` refused | R4 |
| `U-32` | how the robot executes `G02` and `G03` | arcs written as `G01` | R6 |
| `U-36` | whether the robot moves after the job ends (`INFO;23`) | through cuts and pockets refused; a disclosure on other jobs (`W-PARK`) | R3 |
| `U-37` | safe feed and spindle-speed limits | feed cap 2000 mm/min, rpm cap 27 000 | R5 |
| `U-39` | how large a program the robot stores | 32 KiB | R10 |
| `U-40` | the meaning of "max work area 3500 x 3500, diagonal 3500" (spec README §Key reference constants) | W or H above 3500 mm refused; "diagonal 3500" read as the wires' reach (`P-REACH`) | R0b |
| `U-41` | whether a wheel can cross a narrow kerf or ride a shallow recess | every removal deeper than 0.02 mm is a wheel keep-out (section 15) | R8k |
| `U-42` | how much remaining material holds a part (tabs, grooves) | `t_hold` = T/2 | R7 |
| `U-43` | where the robot's drill calibration touches the panel | series jobs refused | R3, R15 |
| `U-44` | where, and at what heading, the robot parks after panel detection | a start pose is required; without one `W-FIRST` refuses | R2 |
| `U-46` | whether some CAM programs write rapids as feeds | cuts and links classified by Z, never by the G word | owner answer |
| `U-49` | where and how the towers' wires attach to the robot, and how far it may turn while tethered | the whole body hull counts for reach; net rotation within ±350° | R0b |
| `U-50` | positioning accuracy near the line between the towers | the bring-up edge margin kept on that edge; `N-BASELINE` | R9 |
| `U-51` | whether the robot has a dust bin, its capacity, what an over-full one does | `N-DUST` notice and an arming-checklist item | R11 |

**Gates.** A gate is on only when the fact it is computed from has been recorded
for this robot and firmware. An unknown gate is off: no unknown is read as "yes".
`G-GATE` refuses a feature used while its gate is off, naming the gate and the
rung that unlocks it.

| Gate | When on, it allows | Unlocked by |
|---|---|---|
| `jobs_allowed` | any job at all: firmware version known and supported | R1 |
| `panel_vertices_usable` | uploads: the measured panel frame is the identity (section 12) | R2 |
| `start_pose_known` | a first line from the measured park pose (`W-FIRST`) | R2 |
| `heading_map_verified` | checks without the any-heading safe zone (`W-SAFEZONE`) | R3 |
| `rotation_model` | rotations checked as the observed sweep instead of the full disc | R3 |
| `rotate_in_place_ok` | rotate-in-place (split) link lines | R3 |
| `z_semantics_verified` | Z below +5 mm | R3 |
| `post_job_motion_known` | through cuts and pockets as far as `W-PARK` is concerned | R3 |
| `spindle_verified` | `M10` in a program | R4 |
| `drop_dwells` | `G4` dropped with a disclosure instead of refused | R4 |
| `first_cut_done` | programs other than rung programs | R5 |
| `arcs_emit` | `G02` and `G03` in the upload | R6 |
| `through_cuts_allowed` | through cuts | R7 |
| `tabs_hold` | the relaxation R-TABS | R7 |
| `kerf_crossing` | the relaxation R-KERF, with measured values | R8k |
| `pockets_allowed` | pockets | R8 |
| `near_edge_allowed` | the 10 mm wheel edge margin, per tested edge | R9 |
| `max_program_bytes` | programs above 32 KiB, up to the verified size | R10 |
| `m11_emit` | `M11` (dust dump) | R11 |
| `series_allowed` | series jobs and the relaxation R-DETECT | R15 |
| `wire_attachment_measured` | `P-REACH` on the measured attachment point | R0b |

**Rungs.** The bring-up ladder is the ordered list of robot sessions that turn
unknowns into facts. The rungs this model names:

| Rung | What it is | What it can open or change here |
|---|---|---|
| `R0b` | the robot measured with a tape, switched off ([field guide 04](../field-guide/04-measure-your-robot.md)) | measured footprint (union), `r_w`, body hull, tower base, wire attachment |
| `R1` | the read-only probe: identity and firmware version | `jobs_allowed` |
| `R2` | pairing and panel detection, no upload | panel frame, park pose |
| `R3` | an air job: spindle off, Z >= +5 mm, inside the safe zone | heading map, rotation model, rotate in place, Z semantics, post-job motion |
| `R3b` | a physical-stop drill during a job | stop behaviour |
| `R4` | a spindle air job | `spindle_verified`, `drop_dwells` |
| `R5` | the first real cut | `first_cut_done`; the owner sets feed and speed caps |
| `R6` | arcs | `arcs_emit` |
| `R7` | a through cut with an inner hole and tabs | `through_cuts_allowed`, R-TABS |
| `R8k` | a wheel crossing a narrow slot, measured | `kerf_crossing` (R-KERF) |
| `R8` | a shallow pocket | `pockets_allowed` |
| `R9` | air jobs at each edge at the checker's limit | `near_edge_allowed`, per edge; may lower `m_tower` |
| `R10` | a large air job | `max_program_bytes` |
| `R11` | `M11` in an air job | `m11_emit` |
| `R15` | two jobs on one panel | `series_allowed`, R-DETECT |

Rung programs are certified under section 14. Rung results are facts of the robot
they ran on; they never transfer to another robot or another firmware.

## 2. Principles

1. **Refuse rather than guess.** No unknown is read as "yes". A value nobody has
   measured is a conservative default with provenance `DEFAULT_UNMEASURED`, and the
   job shows the amber banner "UNMEASURED MACHINE PROFILE" (parameters §2).
2. **Stricter than the vendor, never looser.** The vendor tested the supports only
   at toolpath vertices, against the panel bounds and the tower triangles
   ([spec 08 §Footprint model](../protocol/08-head-angle-and-cut-order.md#footprint-model)),
   and tested no footprint against removed material
   ([spec 05 §Toolpath ordering and "don't fall into cuts"](../protocol/05-gcode-dialect.md#toolpath-ordering-and-dont-fall-into-cuts)).
   This model tests every wheel along every piece of every move, in time, against
   the panel, the tower zones, the removed material and what still holds together,
   and tests the tool on rapids too. The heading chosen for a cut must also pass
   the vendor's own test (`W-VENDORFLOOR`, section 10).
3. **Check what you ship.** The checks run on the program parsed back from the
   exact upload bytes, not on a planner's internal representation (section 11.5).
4. **Monotone checks.** An implementation MUST make every check monotone: a larger
   panel, smaller keep-out zones or less removal never turn a pass into a fail.
   This is what lets a result computed at the desk carry over to the panel the
   robot measures (section 12).
5. **Owner policy only tightens.** An owner may choose larger margins and lower
   caps. Version 1 marks no parameter as overridable (parameters §2.3).
6. **Every finding points at a line.** Every derived object (piece, sweep,
   witness) keeps a reference to the source line it came from.
7. **Runner rules with parameters.** The runner applies some rules at job time
   with parameters from this catalogue, so their provenance is visible: any tower
   `WARN` blocks, and a tower battery below `battery_ack_below` with an estimated
   job longer than `battery_ack_job_min` needs a typed acknowledgement. They are
   not checks of this model.

## 3. Frames, units and normalisation

### 3.1 The panel frame

- Millimetres and degrees, absolute coordinates. The origin is the corner where
  tower 1 stands and the panel occupies [0, W] x [0, H]
  ([spec 08 §Coordinate frame and inputs](../protocol/08-head-angle-and-cut-order.md#coordinate-frame-and-inputs)).
  Angles are counter-clockwise from +X and `u(α) = (cos α, sin α)`.
- The G-code frame is this panel frame: the vendor's programs were written in it.
  **INFERRED (U-13, U-20):** the frame of the robot's `INFO;25` panel vertices and
  of its position frames is the same frame. Nothing transforms emitted G-code into
  another frame, so uploads need the fact that the measured frame is the identity
  (section 12). A unique match under any other axis map is refused for uploads
  until a design review decides whether a certified, disclosed transform is needed
  (PHYS-01).
- An optional placement shift `[placement] dx, dy` in job.toml is applied and
  disclosed. There is no automatic origin detection.
- A coordinate beyond ±10 000 mm is refused when the file is read (gi-format §8.2).

### 3.2 Z

- Z = 0 is the stock top and cuts are negative
  ([spec 05 §Semantics a replacement must match](../protocol/05-gcode-dialect.md#semantics-a-replacement-must-match)).
  A file with Z = 0 at the stock bottom is refused unless job.toml says so; it is
  then shifted by −T and the shift is disclosed. GI's `ZSHIFT` is never applied
  (gi-format §6).
- `Z_safety` is 10 mm (spec 05). Every rapid travels at exactly `Z_safety`, and
  every descent is a `G01` (`D-ZSAFE`, section 9.1).
- **The tool's Z when the program starts is unknown (U-18).** The program's first
  motion MUST be a Z-only `G01` raise to `Z_safety` before any X or Y motion.
- `T-DEPTH` refuses a tool point lower than z = −(T + `overcut_max`), deeper below
  the stock top than the tool's flute length or than the Z travel (58 mm,
  [spec README §Key reference constants](../protocol/README.md#key-reference-constants-machine-1930--firmware-gfw-010200)),
  or higher than the Z upper limit of +40 mm (INFERRED from the Z travel).
- While the gate `z_semantics_verified` is off (U-23), no tool point goes below
  Z = +5 mm (`z_min_unverified`): programs are air jobs, and `G-GATE` refuses a
  lower point, naming rung R3.
- `T-TOOL` refuses an unknown tool, a diameter above 8 mm and a declared stick-out
  above 58 mm (spec README §Key reference constants). A tool without a declared
  stick-out is checked for diameter and flute length, and the report says that its
  stick-out was not checked.

### 3.3 Cuts and links

- A move is **in material** when its lowest Z is below −`ε_surf` (0.02 mm).
- A **cut** is a maximal run of moves from the move that goes below −`ε_surf` to
  the move that returns to z >= 0, with no XY travel above the surface inside it.
  Everything else is a **link**.
- Classification ignores the G word, because some CAM programs reportedly write
  rapids as feeds (U-46).

### 3.4 Operations and through cuts

- Operations come from GI `OP`/`OPEND` blocks when the file has them; segmentation
  by Z then stays authoritative and the blocks are checked against it. A block
  whose moves do not form whole cuts, or a through flag the geometry contradicts,
  is warned about (`N-OPS-MISMATCH`), and the geometry is used.
- Without blocks the operations are inferred from the segmentation and the report
  says so (`N-INFERRED-OPS`).
- A cut is **through** when its deepest point is at least T − 0.05 mm below the
  stock top.

### 3.5 Arcs and helices

- Arcs are `G2`/`G3` in the XY plane with an I/J centre. An arc whose end equals
  its start at file precision is a full circle; 0 < |end − start| < 0.01 mm is
  refused as ambiguous; start and end radii may differ by at most 0.005 mm
  (gi-format §8.7, PHYS-16). The report shows each arc's sweep.
- Helical arcs are linearised to `G01` within 0.02 mm, with a note.
- For the checks, an arc is cut into sub-arcs. Each sub-arc piece is checked as its
  chord `σ_j` grown by its sagitta `e_j`, and `e_j` is at most `e_max`, the check
  chord tolerance (0.05 mm). A straight piece has `e_j` = 0.
- While the gate `arcs_emit` is off, the emitter writes arcs as `G01` segments
  within 0.02 mm, and the checks run on those segments (section 11.5).

### 3.6 Pieces: the time coordinate

Every move is cut into **pieces** no longer than `Δ` in XY (section 7.3); an arc's
pieces are sub-arcs whose chords are no longer than `Δ`. Pieces are numbered in
program order across cuts and links, and the piece index `j` is the time
coordinate of every check. A Z-only move is one piece of zero XY length.

### 3.7 Normalisation notes

Every transformation is a note in the report: a placement shift, a Z-zero shift, a
helix linearised, `G43` ignored, coolant words, `N` words and comments dropped,
`M2`/`M30` dropped, a `G4` dropped while `drop_dwells` is on, operations inferred.
None is silent.

## 4. The robot model

### 4.1 Heading and the C word

- The heading is θ = (C + 90) mod 360, and `C` takes the values 0, 10, …, 350
  ([spec 08 §Emitting the C word](../protocol/08-head-angle-and-cut-order.md#emitting-the-c-word)).
  `u(θ)` points from the body toward the tool.
- `C` is written only on `G00` lines. What the firmware does with `C` on a feed
  line is unknown (U-21), so a `C` on a feed line is refused (`D-CSTEP`).
- **INFERRED (U-20):** `u(θ)` is the robot's physical forward direction and the
  panel axes are not mirrored on the robot
  ([spec 08 §Caveats](../protocol/08-head-angle-and-cut-order.md#caveats)). Until
  the gate `heading_map_verified` is on, the any-heading safe zone applies to every
  tool point (section 8).

### 4.2 Supports

For tool point `p` and heading θ, support k is at

```
q_k = p + d_k(θ),     d_k(θ) = −r_k · u(θ + δ_k)
r_1 = 402 mm, δ_1 = 0      (rear)
r_2 = 211 mm, δ_2 = +76°   (right side)
r_3 = 211 mm, δ_3 = −76°   (left side)
```

([spec 08 §Constants](../protocol/08-head-angle-and-cut-order.md#constants)). At
C = 0: q1 = p + (0, −402), q2 ≈ p + (204.7, −51.0), q3 ≈ p + (−204.7, −51.0)
(spec 08 §Footprint model); implementations SHOULD keep these as golden tests.
**INFERRED (U-22):** the supports are the wheel modules' contact centres.

### 4.3 Footprint models

- **vendor**: the spec values of section 4.2.
- **measured**: the support positions measured at rung R0b
  ([field guide 04](../field-guide/04-measure-your-robot.md)), entered as typed
  measurements with their tolerance and the operator's name.
- **union**: every check passes for each wheel at both its vendor and its measured
  position. Once R0b has run, the union is the active model; a measurement adds
  positions, it never replaces the spec values.

Before R0b the active model is the vendor model. "The active footprint" below means
every support position of the active model, and `min_k r_k` and `max_k r_k` run
over all of them.

### 4.4 Wheel discs

- Each support is a disc of radius `ρ = r_w + m_w` about its contact centre
  (three omni-wheel modules, "two rollers are always in contact": prior-art review §4).
- `r_w`, the wheel contact radius, is 40 mm (`DEFAULT_UNMEASURED`, U-22) until
  rung R0b measures it. Field guide 04 records a contact radius per module; `r_w`
  is the **maximum** of the three, never the mean, because one `r_w` serves every
  wheel. `m_w` is the wheel margin, 15 mm; `ρ` is 55 mm with the defaults.
- During a piece the heading is fixed, so a wheel centre moves along the piece's
  path translated by `d_k(θ)`: for a straight piece a segment, swept by the disc as
  a capsule; for a sub-arc, its chord grown by `e_j`.

### 4.5 Body hull

- The body rides above the panel: cuts under it do not matter. The body hull is
  checked only against tower bases (`B-TOWER`) and declared obstacles and fixations
  (`B-OBST`).
- Default (`DEFAULT_UNMEASURED`, U-22): the convex hull of the wheel discs grown by
  `m_body` = 60 mm. After rung R0b: the convex hull of the wheel discs and the
  measured body outline.
- `R_body` is the radius of a disc about the tool point that holds the body hull
  during any turn about a pivot between the tool point and the support centroid:
  the largest, over the hull's points h, of |h| and `d_c` + |h − c| (section 4.6;
  under the union model, over both models' centroids c, as for `R_rot`).
  With the default hull that is at most `R_rot + ρ + m_body`, and this model uses
  that bound: 519 mm with the defaults.

### 4.6 Rotation radius

```
R_max = max_k r_k = 402 mm          r_min = min_k r_k = 211 mm
c     = mean of the d_k(θ)          (the support centroid, relative to the tool)
d_c   = |c|            = 168.03 mm  (spec 08: "about 168 mm behind the tool")
R_c   = max_k |d_k − c| = 235.80 mm
R_rot = max(R_max, d_c + R_c), rounded up to whole millimetres = 404 mm
```

`R_rot` holds every support during a turn about any pivot on the segment from the
tool point to the support centroid, **provided the robot turns about such a point
and reaches the commanded XY**. For a pivot at distance s along that segment,
support k stays within s + |d_k − s·c/|c|| of the tool point. That bound is convex
in s, so it is largest at an end: r_k at s = 0, and `d_c` + |d_k − c| at s = `d_c`.

Under the union model (section 4.3) there are two footprints, each with its own
centroid; the mean of all six positions is neither model's centroid. `R_rot` is
then the largest, over both centroids c (the vendor model's and the measured
model's) and over every support position d_k of both models, of r_k and
|c| + |d_k − c|: the bound above for a pivot between the tool point and either
centroid, applied to every position a wheel may have.

A 180° turn about the centroid brings the rear support to 66 mm from the tool point
(234 − 168), inside `r_min − ρ`, so an annulus is not enough during rotations
(PHYS-07). A firmware that turns about another point, or does not reach the
commanded XY, is outside this model. Rung R3 measures the tool's excursion during a
rotate-in-place line (go: within 5 mm), and R3 is an air job on a clear panel.

## 5. Towers

### 5.1 Keep-out zones at both tower-2 placements

```
Z1(L)            = triangle (0, 0), (0, L), (L, 0)
Z2(L), axis Y    = triangle (0, t − L), (0, t), (L, t)      tower 2 at (0, t)
Z2(L), axis X    = triangle (t − L, 0), (t, 0), (t, L)      tower 2 at (t, 0)
```

([spec 08 §Tower keep-out zones](../protocol/08-head-angle-and-cut-order.md#tower-keep-out-zones).)
Tower 2 stands on the Y axis for `SET T2 POSITION;0` (spec 08 `tower2 = 0`, corner
(0, H)) and on the X axis for `;1` (corner (W, 0))
([spec 02 §Tower pairing & geometry](../protocol/02-message-reference.md#tower-pairing--geometry);
[spec 04 §Tower pairing and geometry](../protocol/04-connection-and-provisioning.md#tower-pairing-and-geometry)).

- **Both tower-2 placements.** Tower 2 is placed at the panel corner on its axis
  (t = H or t = W) **and** at the measured `INFO;27` distance along the axis,
  because the vendor located tower 2 two different ways
  ([spec 08 §Caveats](../protocol/08-head-angle-and-cut-order.md#caveats), "Where
  tower 2 is"). At the desk the declared distance takes the place of the measured
  one. Every tower check holds at both placements.
- **Wheel zones** have L = 150 mm (`W-TOWER`); **tool zones** have L = 155 mm and
  apply to every move, rapids included (`T-TOWER`; the vendor tested only the
  toolpath).
- Zones are closed sets, grown by `m_tower` (25 mm), and tested against swept discs
  along whole pieces, never only at vertices. The vendor's exact edge behaviour is
  reproduced only in the vendor reference of section 10; the grown closed sets make
  it irrelevant to these checks.
- At the desk the zones are also grown by `geom_tol` (section 12).

### 5.2 Tower bases

Each tower's base is a disc of radius 120 mm (`DEFAULT_UNMEASURED` until rung R0b)
about the tower point: (0, 0) for tower 1, and tower 2 at both placements.
**INFERRED:** the towers stand at the panel edge ("mounted to the edge of the
workpiece", prior-art review §4). The body hull keeps out of the bases (`B-TOWER`),
and so does the tool (`T-TOWER`).

### 5.3 Anchor points

`a1` is the centroid of Z1(150), (50, 50). `a2` is the centroid of Z2(150), at each
tower-2 placement. Both lie inside the 155 mm tool zones, which `T-TOWER` keeps the
tool out of, so they are never removed. Section 7.4 uses them to tell held material
from loose material.

## 6. Work areas, range and reach

### 6.1 The panel at the desk and at job time

- **At the desk** the plan is checked against the declared rectangle shrunk by
  `geom_tol`: `P_work` = [0, W] x [0, H] ⊖ `geom_tol`.
- **At job time** `P_work` = `P_meas` ∩ [0, x] x [0, y], where `P_meas` is the panel
  the robot measured and x, y are the integers sent in `SET PANEL SIZE` (section 12).

### 6.2 Wheel work area and per-edge margins

```
WA_wheel = (P_work ∪ S_decl) ⊖ m      (m per edge, below)
```

- `S_decl` are support strips the job declares beyond a panel side (relaxation
  R-SUPPORT; the owner confirms them physically at arming). **No overhang by
  default** on any side. The vendor's pass-2 overhang is used only onto declared
  strips (the vendor's starting value of its blocked sides was not determined:
  spec 08 §Caveats).
- The margin m is `m_edge` (50 mm) on every edge until rung R9 (gate
  `near_edge_allowed`) has shown positioning near that edge; then 10 mm, owner
  policy.
- **The tower-baseline edge**, the panel side from tower 1 to tower 2 (x = 0 when
  tower 2 is on the Y axis, y = 0 when on the X axis), keeps 50 mm
  (`m_edge_baseline`) until R9 has tested that edge itself (U-50).
- `W-AREA` refuses a wheel disc that leaves `WA_wheel` during any piece.
- `N-BASELINE` discloses tool points within 300 mm of the tower baseline, where
  positioning accuracy is expected to be lowest (prior-art review §4.1, an inference
  there).

### 6.3 Tool work area

`WA_tool` = `P_work` ⊖ `m_edge_tool` (2 mm). `T-AREA` refuses an in-material piece
whose tool disc, of radius r_tool + `δ_tool` + `e_j`, leaves it.

### 6.4 Range and wire reach

- `P-RANGE` refuses W or H above 3500 mm (spec README §Key reference constants).
- The towers measure the robot's position with spring-loaded wires about 4 m long
  (prior-art review §4), tethered to the robot at a point nobody has inspected yet
  (U-49). **INFERRED (U-40):** the spec's "diagonal 3500" is the wires' usable
  reach. `P-REACH` refuses any piece, links included, at which the wire attachment
  point is farther than `reach_max` = 3500 mm from tower 1 or from tower 2 at either
  placement.
- Until rung R0b measures the attachment, every point of the body hull counts as
  the attachment point (under the any-heading model, the disc of radius `R_body`).
  Afterwards the measured point counts, and that is the tool point when the wires
  attach on the spindle axis.
- For example, the default body hull reaches up to 517 mm from the tool point at a
  fixed heading (`R_max + ρ + m_body`) and 519 mm during a turn (`R_body`), but it
  cannot reach far beyond the panel: `W-AREA` keeps every wheel disc 50 mm inside
  the panel's edges, and the hull is the wheel discs grown by 60 mm, so it reaches
  at most 10 mm beyond the panel's edges (more only where wheels run on declared
  support strips, or after rung R9 has lowered the edge margin). The far corner of
  a 3050 x 1530 mm panel is 3412 mm from tower 1, yet no hull point of a pose that
  `W-AREA` accepts is farther than 3387 mm from tower 1, or 3410 mm from tower 2
  placed 25 mm beyond its corner (the most `P-GEOM` lets the measured distance
  differ): on that panel `P-REACH` refuses nothing that `W-AREA` has not refused
  already. On a 2440 x 1220 mm sheet the hull stays within 2726 mm of either
  tower. `P-REACH` refuses work toward the far end of a longer panel, for example
  a 3500 x 1000 mm panel, whose far corner is 3640 mm from tower 1, and work with
  wheels on support strips beyond the panel.

### 6.5 Obstacles and fixations

- job.toml may declare obstacles and fixations (screws, clamps) with footprints and
  heights, and prior removal (holes, pockets, kerfs with depths) on a panel that is
  not new. Unknown job.toml keys are refused, so a declared obstacle can never be
  ignored silently.
- Obstacles and fixations, and the fixation points of relaxation R-FIXED, are keep-out
  regions for the wheels (`W-OBST`; they belong to `K_wheel`, section 8) and for the
  body hull (`B-OBST`). The tool may pass over one only with at least 3 mm of
  clearance above its height, and `Z_safety` must be at least 3 mm above the
  tallest (`T-OBST`).
- Prior removal seeds `Rem(0)` (section 7.1).
- The bring-up rungs R2 to R5 need a panel with no cut, hole, screw or debris on it
  (PHYS-06).

## 7. Material and structure over time

### 7.1 Removal

Each in-material piece `j`, with XY path `σ_j` and lowest Z `z_j`, removes

```
R_j    = σ_j ⊕ D(r_tool + δ_tool + e_j)        depth d_j = −z_j   (the deepest depth over the piece)
Rem(j) = Rem(0) ∪ ⋃ { R_i : i < j, piece i in material }
```

`D(r)` is the closed disc of radius r about the origin and ⊕ the Minkowski sum.
The deepest depth over the piece is conservative for both uses of `Rem`: it
over-counts what the wheels must avoid and what breaks holding material. Ball and
V bits are modelled as flat, which over-estimates removal. `δ_tool` is the tool's
runout (0.25 mm).

`Rem(0)` is the prior removal declared in job.toml, or, for a series job, the
previous job's material state (section 13). `T-PASS` warns when the step-down from
the deepest earlier removal under the tool exceeds the tool's declared maximum
step-down.

### 7.2 Wheels over removed material

During piece `j`, wheel k sweeps

```
W_{k,j} = (σ_j + d_k(θ_j)) ⊕ D(ρ + e_j)
```

and `W-SURF` requires `W_{k,j} ∩ Rem(j) = ∅` for every wheel and every piece, cuts
and links (a link piece is reported as `W-ROT`, `W-RAPID` or `W-FIRST`). The
heading is fixed during a cut, so this is exact for straight pieces. Every removal
deeper than `ε_surf` is a wheel keep-out: no wheel crosses a kerf or a groove until
the relaxation R-KERF is open (section 7.5).

### 7.3 Why pieces suffice: the exactness invariant

The checker asserts at start-up:

```
min_k r_k − Δ  >  ρ + r_tool + δ_tool + 2·e_max
spec values, 8 mm bit:  211 − 50 = 161  >  55 + 4 + 0.25 + 0.1 = 59.35
```

- Material removed before piece `j` stays removed during it, so any intersection
  with `Rem(j)` is a real violation.
- Material removed *during* piece `j` lies within r_tool + `δ_tool` + `e_j` of
  `σ_j`, while every wheel centre stays at least |d_k| − `Δ` from every point of
  `σ_j`. Under the invariant that material can never be under a wheel during piece
  `j`, so leaving it out of `Rem(j)` is exact; it is in `Rem(j+1)`.
- So the classic hazard is caught: a long cut along the heading with the rear wheel
  following the fresh kerf. The benign mirror case, a wheel passing over material
  that is cut only later, is correctly not flagged.
- `Δ` = min(50, floor(min_k r_k − ρ − r_tool − `δ_tool` − 2·`e_max` − 1)), from the
  active footprint and the program's tool. Below 5 mm the checker refuses with an
  internal error (`M-INVARIANT`): the robot or tool would be outside the model's
  assumptions.

### 7.4 Structure: attached and detached material

```
Rem_hold(j) = Rem(0)_hold ∪ ⋃ { R_i : i < j, d_i > T − t_hold }    removal that breaks holding material
M(j)        = P_work \ Rem_hold(j)                                material that still holds parts together
Anchor(j)   = the connected component of M(j) that contains a1
Detached(j) = (P_work \ Rem(j)) \ Anchor(j)
```

- **Prior removal counts.** `Rem(0)_hold` is the part of the prior removal
  `Rem(0)` (section 7.1) deeper than T − `t_hold`. A part that an earlier job or a
  declared earlier cut has freed is loose before this program's first piece: a
  wheel on it is refused (`M-ATTACH`), so is a tool in it (`T-ATTACH`), and prior
  removal that already separates the towers' corners refuses the program at its
  first piece (`M-TOWERSEP`). *This model's reading:* the project design's formula
  lists only this program's pieces, which would let removal declared in job.toml
  hide a loose part.
- The towers are "mounted to the edge of the workpiece" (prior-art review §4) and
  define the frame, so a part cut free of the towers' piece can shift even when the
  floor supports it.
- `M-TOWERSEP` refuses a program in which, at some piece, `a2` (at either tower-2
  placement) is not in `Anchor(j)`: a cut across the panel between the towers, or a
  full perimeter trim.
- `M-ATTACH` refuses a wheel disc that meets `Detached(j)`; `T-ATTACH` refuses a
  tool that reaches more than 0.1 mm into `Detached(j)`: a loose part is never cut.
- **`t_hold` = T/2** (INFERRED, U-42): a groove shallower than half the stock
  holds; typical tabs (a few millimetres on 18 mm stock) do not, so a tab-held part
  counts as detached. The relaxation R-TABS can lower `t_hold` after rung R7.
- With the relaxation R-FIXED, a component that holds at least two declared
  fixation points counts as anchored.
- Detachment is computed with polygon booleans at cut boundaries. When the anchor
  loses more area than the cut's own removal explains, bisection over the cut's
  pieces finds the exact detaching piece, and repeats for every further detachment
  in the same cut until the anchor's whole loss of area is explained: one
  serpentine cut can free several strips (PHYS-18). The result is a detachment
  timeline of (piece, island, cut), computed once per cut order and independent of
  headings.

### 7.5 Relaxations

Each relaxation is named, recorded in the certificate and shown everywhere the job
is shown. None is on by default.

| Relaxation | What it allows | Available after | Parameters |
|---|---|---|---|
| R-SUPPORT | wheels over declared support strips beyond a panel side | always; the owner confirms the strips at arming | width per side |
| R-TABS | a lower `t_hold`, so tabs of a given thickness hold | rung R7 and a typed reason from the owner | `t_hold` |
| R-FIXED | components holding at least two declared fixation points count as anchored; the fixings are wheel, body and tool keep-outs | always | fixation points |
| R-KERF | a wheel may cross a kerf no wider than `w_b` and ride in it at most `L_ride`; recesses shallower than `h_r` are ignored; wide regions are the morphological opening of `Rem` by `w_b` | rung R8k, measured values only | `w_b`, `L_ride`, `h_r` |
| R-DETECT | a series job with prior removal inside the detection band | rung R15 | band width |

## 8. The any-heading safe zone

### 8.1 Conditions

While the gate `heading_map_verified` or the gate `rotation_model` is off (U-20,
U-21), every tool point `p` at time `j` MUST satisfy:

```
disc(p, R_rot + ρ)                ⊆ WA_wheel                              (panel containment)
annulus(p, r_min − ρ, R_rot + ρ)  ∩ (K_wheel ∪ Rem(j) ∪ Detached(j)) = ∅  (during translations)
disc(p, R_rot + ρ)                ∩ (K_wheel ∪ Rem(j) ∪ Detached(j)) = ∅  (at every rotation step)
disc(p, R_body)                   ∩ tower bases = ∅                       (body hull bound)
```

`K_wheel` is the union of the grown wheel zones at both tower-2 placements and the
declared obstacles and fixations. `W-SAFEZONE` refuses a tool point that breaks any
of the four.

### 8.2 What it guarantees, exactly

- During a translation at any fixed heading, the supports stay between `r_min` and
  `R_max` from the tool point, so the annulus is enough.
- During a rotation, whatever `C` means physically, the supports stay within
  `R_rot` of the tool point **provided the robot turns about a point between the
  tool and its support centroid and reaches the commanded XY** (section 4.6). So
  rotation steps are checked with the full disc until the rotation model and the
  tool's measured excursion are facts. For the rungs R3 to R5 the full disc costs
  nothing: their rotations all happen before any removal.
- For a convex panel the disc and the annulus give the same containment test. For
  removal and tower zones during translations the annulus is enough; a full disc
  for every tool point would refuse every job with more than one cut.

### 8.3 The bring-up panel

On a rectangle at the desk, the zone is the panel shrunk on every side by
`R_rot + ρ + m_edge + geom_tol` = 404 + 55 + 50 + 10 = **519 mm** (the edge margin
of 50 mm applies until R9). So:

- a 1220 x 1600 mm panel leaves a 182 x 562 mm zone, and a 1220 x 2440 mm panel a
  182 x 1402 mm zone: enough for R3 to R5 (the R3 job spans 100 x 150 mm, the R5
  square 100 x 100 mm);
- below 1038 mm on either side there is no zone, and the rung generator refuses
  with that explanation;
- the zone is far from the corner zones: on a 1220 mm side its nearest point is
  628 mm from the Z1 hypotenuse, against the 459 + 25 mm needed, and 734 mm from
  the corner, against the 519 + 120 mm that the body bound and the tower base need.

The bring-up panel MUST be clearly rectangular, with sides differing by at least
200 mm so the frame hypotheses can be told apart (section 12), and free of any cut,
hole, screw or debris (PHYS-06, PHYS-11).

## 9. Links, rotations, the first line and after the last line

### 9.1 The link shape

Between cut k and cut k+1 the emitter writes:

```
G01 Z<Z_safety> F<F_retract>       (L1) retract; the spindle still runs
M05                                (L2) spindle off before every rapid (spec 05 §Codes emitted)
G00 X<xa> Y<ya> C<c'>              (L3) rotate in place at pivot a: X and Y unchanged; repeated so that each numeric |ΔC| < 180
G00 X<xb> Y<yb>                    (L4) translate at the new heading
Z<Z_safety>                        (L5) the rapid's Z line, exactly Z_safety
M10                                (L6) spindle on
G01 Z<z_entry> F<F_entry> S<rpm>   (L7) plunge at the entry feed; F and S written again
G01 … F<F_work>                    (L8) cut k+1 at the work feed
```

- The pivot may be the start of cut k+1 instead (translate at the old heading,
  then rotate); the planner picks the pivot with more slack.
- Spec 05's rapid-Z clamp `min(pointZ, Z_safety)`, read literally, would let a
  rapid go below the safety plane; L5 always writes exactly `Z_safety`, and every
  descent is a `G01` (`D-ZSAFE`).
- `W-RAPID` checks each translation's pieces like cut pieces, at their heading, and
  each combined line with the envelopes of section 9.4: a link that leaves the
  panel is refused. `T-TOWER` and `P-REACH` apply to link pieces too.
- Whether the firmware turns in place on L3 is U-21, tested at rung R3. If it does
  not, the emitter uses the **combined** profile, `G00 X<xb> Y<yb> C<c>` (the
  vendor's own form), a disclosed dialect profile checked with the envelopes of
  section 9.4. The choice follows from the R3 facts and is never silent; `G-GATE`
  refuses rotate-in-place (split) lines while the gate `rotate_in_place_ok` is off
  (C-03).

### 9.2 The 180-degree step rule and the cumulative-rotation bound

- Whether the firmware turns the short way or interpolates `C` numerically (so
  350 → 10 is a 340° swing) is unknown (U-21). With every emitted step at numeric
  |ΔC| < 180, both readings give the same arc, so the swept region is unambiguous.
  Crossing the wrap therefore goes the long way under an unverified model
  (350 → 190 → 30 → 10), and the checker checks the whole concatenated sweep.
- Once rung R3 records `rotation.model = in-place-shortest`, a single short step
  across the wrap is allowed and checked as that arc.
- **Net rotation is bounded**, because the towers' wires are tethered to the robot
  (U-49). While every step is below 180° numerically and `C` stays in [0, 350], the
  net rotation equals C_end − C_start under both readings, so it never exceeds
  ±350°, the envelope the vendor's own jobs reached under the numeric reading. Once
  single wrap steps are allowed, the emitter and `D-CSTEP` track the cumulative
  signed rotation under every allowed model and refuse a program that would take
  the body beyond ±350° of its start heading, until rung R0b shows how the wires
  attach.

### 9.3 Rotation sweeps

A rotation about pivot `p` from θ0 to θ1 sweeps wheel k along the arc of radius
`r_k` about `p`, grown by `ρ`: changing `C` with the tool held swings the body about
the tool point
([spec 08 §Purpose](../protocol/08-head-angle-and-cut-order.md#purpose)). Until the
gate `rotation_model` is on, each rotation step is checked as the **full disc** of
radius `R_rot + ρ` about `p` (any direction, any amount, any pivot between the tool
and the support centroid), and the body as the disc of radius `R_body` against the
tower bases. Once rung R3 records the model and the tool's excursion, the observed
sweep is used. `W-ROT` reports a rotation step that fails a wheel, structure or
body check.

### 9.4 Lines that rotate and translate together

A line that rotates and translates together (the first line, the combined profile)
goes from (`p_a`, θ_a) to (`p_b`, θ_b). It is checked with:

1. **Rotation model unknown** (gate `rotation_model` off): the any-rotation
   envelope, the capsule `capsule(p_a, p_b, R_rot + ρ)` = conv(disc(p_a, R_rot + ρ)
   ∪ disc(p_b, R_rot + ρ)) for the wheels, and the capsule of radius `R_body`
   against the tower bases.
2. **Rotation model known**: the sweep of the observed model. For the model the
   project expects, in which the body turns about the tool point while the tool
   point moves along the straight segment, wheel k stays inside
   `E_k = conv({p_a, p_b} ⊕ arc_k) ⊕ D(ρ)` with `arc_k = { d_k(θ) : θ along the
   rotation }`. Because conv(A ⊕ B) = conv(A) ⊕ conv(B), this holds every motion in
   which the tool point moves along the segment and θ moves monotonically along
   the arc, in any blend. Until the direction of the turn is a fact, `E_k` is taken
   over both directions; afterwards along the observed one.

A combined link line that fails either case is reported as `W-RAPID`, with the
failed rule in the witness; the program's first line is reported as `W-FIRST`
(section 9.5).

**INFERRED (U-21):** the tool point moves along the straight segment and θ moves
monotonically; rung R3 observes both. *This model's reading:* the project design
introduces `E_k` "until the rotation direction is a fact", and elsewhere requires
the any-rotation envelope until the rotation model is a fact; `E_k` assumes the
turn is about the tool point, so case 1 applies until the model is a fact.

### 9.5 The first line (W-FIRST)

The program's first `G00` moves the robot from where it parked after panel detection
to the first cut, at heading `C_1`. Panel detection traces the panel edge
([spec 03 §Full job flow](../protocol/03-session-sequence.md#full-job-flow), step D),
so the robot may park near an edge, and a turn there swings a support up to `R_rot`
from the tool point. (Turning from C90 to C0 with the tool 100 mm from the tower-1
edge puts q3 at x = −104.7 mm: PHYS-03.)

- **The start pose is required (U-44).** It comes from position frames in this
  session, once the frame mapping is a fact, or from the park pose the operator
  measured at rung R2 (fact `detection.park_pose`: the tool position by tape from
  two edges, the body heading by photograph and protractor), confirmed at the
  arming checklist within ±20 mm and ±5°, otherwise measured again. No start pose:
  `W-FIRST` refuses.
- **The start pose has a tolerance** (*this model's reading*: the design states the
  confirmation tolerances but not their use here). A measured park pose is a disc
  of radius `park_pose_tol` (20 mm) about the measured point and a heading interval
  of ±`park_pose_tol_deg` (5°); every check of the first line holds for the whole
  disc and interval.
- **Before the heading map and the rotation model are facts**, the first line is
  checked with the any-rotation envelope conv(disc(start, R_rot + ρ +
  park_pose_tol) ∪ disc(target, R_rot + ρ)) against `WA_wheel`, `K_wheel`,
  obstacles and prior removal, and with `R_body` in place of `R_rot + ρ` against the
  tower bases. In practice the park point must lie inside the any-heading safe zone.
- **After rung R3** (heading map, rotation model and park heading known) the
  observed model is used, and the emitter may write the first line as a
  translation at the parked heading followed by a rotation in place at the target,
  whichever has more slack.
- **Rung R3 itself**, when the robot parks outside the safe zone, is refused with
  that explanation. The project design names one alternative: a design-reviewed
  exception recorded in the R3 rung exemption, in which the first line keeps the
  measured park heading (rounded to the 10° grid), is checked as a translation with
  the heading swept over the measured value ±10° (and the position tolerance
  above), is followed by a slow approach into the zone, and needs the operator's
  typed acknowledgement. *This model's reading:* that exception exists only after
  such a review; version 1 enables nothing of it, and without it the refusal
  stands. If the park heading cannot be measured, R3 is refused.
- **Partly cut panel** (series): the pose must be known and the envelope over both
  directions must pass.

### 9.6 After the last line (W-PARK)

Whether the robot moves after `INFO;23` is unknown (U-36). Until rung R3 records
the fact `post_job.motion` (none, or a path model), through cuts and pockets stay
refused by `W-PARK` (they are gated until R7 and R8 anyway, and R7 needs this fact),
and so does a program on a panel whose prior removal (section 7.1) holds a through
cut or a pocket, because the robot's unknown motion after the job could reach it
as easily (*this model's reading*). Every other program carries the disclosure
"the robot may move after the job; keep clear". Once the fact exists, `W-PARK`
checks the wheels at the final tool point and along the modelled park path against the final removal and the final
detached material, with the usual margins.

## 10. Heading selection

- **Vendor reference.** An implementation keeps an exact reproduction of the
  vendor's selection
  ([spec 08 §Candidate search and selection](../protocol/08-head-angle-and-cut-order.md#candidate-search-and-selection)):
  37 candidates, pass-1 first fit, pass-2 ranking, the exact triangle test with
  the spec's vertex order, IEEE doubles. It explains the vendor's choice and orders
  our candidates; it decides nothing on its own.
- **Our choice, per cut.** Candidates come in the vendor's preference order
  (ascending C for pass 1; pass 2 only with declared supports and overhang within
  the declared widths). The first candidate that passes every W-, M- and B- check
  over the cut's pieces at that time with slack >= `slack_min`, and whose link from
  the previous cut passes, is chosen. On a link failure the next candidate is
  tried, then up to two cuts are revisited. If nothing passes, the job is refused
  with a 36-row table (C, first failing check, slack, witness) drawn as a heading
  rose.
- Where the vendor's choice is safe we pick the same C. Where it is not,
  `N-VENDORC` says why ("vendor would choose C = 30; rear wheel enters the kerf of
  cut 4 at line 812").
- Cuts are never pooled under one heading (the vendor pools non-through pockets and
  groups: [spec 08 §Which cuts are evaluated together](../protocol/08-head-angle-and-cut-order.md#which-cuts-are-evaluated-together));
  each cut is evaluated against its own time, which is never less safe, because
  pooling only adds constraints on a shared heading.
- A forced heading (GI `OP c=`) is the only candidate for its cuts and is still
  fully checked; off the 10° grid it is refused when read (gi-format §7).
- **The vendor floor.** For each cut, the chosen C MUST also pass the vendor's own
  evaluation (pass-1 bounds on the declared or `SET PANEL SIZE` rectangle, or
  pass 2 within declared supports). Our model is a superset, so a failure is a
  defect of the checker: `W-VENDORFLOOR` (internal error) refuses the job and a bug
  bundle is saved. Our check is never looser than spec 08.
- **Goliath-dialect input** (a vendor `.NC`) is verified only: its `C` values are
  checked, never chosen again, and it is never emitted again.

## 11. Checks

### 11.1 Severities and scopes

Every check has one severity: `refuse`, `warn`, `disclose` or `internal-error`
(section 1.2). Its scope, `applies_to` in `checks.json`, is one of:

| Scope | Evaluated for |
|---|---|
| `source-line` | a line of the input file, while it is read |
| `piece` | every piece of every move, cuts and links, every wheel |
| `in-material-piece` | every piece below −`ε_surf` |
| `link` | the pieces of links: rotation steps, translations, the first line |
| `tool-point` | every tool point at its time |
| `program-end` | the final tool point and anything after the last line |
| `cut` | each cut |
| `tool` | each tool |
| `program` | the program as a whole |
| `reparsed-bytes` | the program parsed back from the upload bytes (section 11.5) |
| `panel` | the declared panel |
| `job-time` | the session at the robot: measured panel, firmware |
| `series` | a series of programs on one panel |
| `startup` | the checker's start-up assertions |

### 11.2 Slack

A geometric check yields a **slack** in millimetres: the clearance left after every
margin, negative when violated. For a disc of radius r whose centre sweeps a path
S against a keep-out set K, the slack is dist(S, K) − r; for containment in a set
A, dist(S, outside of A) − r. Each check's `slack.definition` in `checks.json`
states its quantity. The **raw** distance and **each margin** are reported
separately, so that raw = slack + the sum of the margins. A non-geometric check has
no slack (`null`) and reports the offending value and the limit instead. The
certificate records the smallest slack of each check.

### 11.3 Witness

On a failure every finding carries a **witness**. `witness_fields` in
`checks.json` lists the fields each check fills:

| Field | Meaning |
|---|---|
| `piece` | the piece index (the time) |
| `cut` | the cut index |
| `src` | the source line: file, line number and text |
| `column` | the column, for lexical errors |
| `wheel` | the support: 1 rear, 2 right, 3 left |
| `point` | the closest point, [x, y] in the panel frame, mm |
| `hazard` | the hazard object in words, for example "kerf of cut 7, line 1234, depth 18.0 mm, removed at piece 5521" |
| `slack_mm` | the slack (section 11.2) |
| `raw_mm` | the raw distance, without margins |
| `margins` | each margin applied, by name, in mm |
| `heading_c` | the C value of the candidate or line |
| `rule` | for a composite check (`W-ROT`, `W-RAPID`, `W-FIRST`, `W-SAFEZONE`, `N-VENDORC`), the check or condition that failed |
| `tower2_placement` | `corner` or `measured` |
| `gate` | the gate that was off |
| `unlock_rung` | the rung that unlocks it |
| `value` | the offending value |
| `limit` | the limit it broke |
| `sources` | for a conflict, every source with its value |
| `island` | the detached island, from the detachment timeline |
| `fact` | the fact that is missing or was used |

### 11.4 Margins compose without double counting

Each margin has one job, and each check adds each margin once:

| Margin | Its job | Used by |
|---|---|---|
| `m_w` (inside `ρ`) | wheel geometry beyond the contact radius | every wheel check |
| `m_edge`, `m_edge_baseline` | edge quality and detection error | `W-AREA`, the safe zone |
| `m_tower` | tower base size and placement | `W-TOWER`, `T-TOWER` |
| `δ_tool` | tool runout | removal, `T-AREA`, `T-TOWER` |
| `m_edge_tool` | the tool's distance from the panel edge | `T-AREA` |
| `geom_tol` | declared against measured geometry, at the desk only | `P_work`, the zones (section 12) |

### 11.5 Checks on the shipped bytes

Certification checks what is shipped:

1. Normalise, choose the headings, plan the links.
2. Emit the bytes and clean them exactly like the vendor's cleaner
   ([spec 05 §What the robot actually receives](../protocol/05-gcode-dialect.md#what-the-robot-actually-receives)).
3. **Parse the bytes back** with the Goliath-dialect reader and run every check on
   that program.
4. `D-ROUNDTRIP` (the parsed program equals the plan within 0.0005 mm), the vendor
   floor, `G-GATE` and, at job time, `F-GATE`.
5. Build the certificate.

On the parsed program: `D-ZSAFE` (rapids at `Z_safety`, descents by `G01`, no `C`
change below `Z_safety`, the prologue raise first), `D-SPINDLE` (`M10` before
cutting, `M05` before every rapid), `D-CSTEP` (section 9.2), `D-FEED` (feeds and
speeds within the tool's limits and the caps, the entry feed after `M10`, F and S
written again after a rapid: spec 05 §Semantics a replacement must match),
`D-WORDS` (only the emitted vocabulary, ASCII, the cleaned form) and `D-SIZE`
(`max_program_bytes`, 32 KiB until rung R10). `D-SINGLETOOL` refuses a second tool:
a job with two tools is two programs.

Program-level notices: `N-DUST` discloses an estimated removed volume above
`TANK_VOLUME` (1.15 dm³, [spec 08 §Extra moves](../protocol/08-head-angle-and-cut-order.md#extra-moves)):
the vendor would empty its dust bin there, and `M11` is not verified (U-51).

### 11.6 The check catalogue

The same list as `checks.json`, in the same order. The GI reader's findings are
defined in gi-format §12.

| Id | Severity | Applies to | Violation |
|---|---|---|---|
| `W-AREA` | refuse | piece | During piece j, a wheel disc of radius ρ + e_j, swept along the piece at the piece's heading, is not inside WA_wheel: the work panel with any declared support strips (R-SUPPORT), shrunk by the wheel edge margin of each edge (safety-model §6.2). Checked for every wheel of the active footprint model and every piece of every move; on a link piece the finding is reported as W-ROT, W-RAPID or W-FIRST with rule = W-AREA. |
| `W-TOWER` | refuse | piece | During piece j, a wheel disc of radius ρ + e_j, swept along the piece, meets a wheel keep-out zone Z1(150) or Z2(150) inflated by m_tower, with tower 2 at both tower-2 placements: the panel corner on its axis and the measured INFO;27 distance along that axis (safety-model §5.1). Tested along the whole piece, between vertices too. |
| `W-SURF` | refuse | piece | During piece j, wheel k's swept disc W_{k,j} = (σ_j + d_k(θ_j)) ⊕ D(ρ + e_j) meets Rem(j): the material removed by the in-material pieces before j, prior removal declared for the panel included (safety-model §7.2). Exact in time: removal by piece j itself is left out, which the invariant of safety-model §7.3 makes exact (M-INVARIANT). |
| `W-SAFEZONE` | refuse | tool-point | While the gate `heading_map_verified` or the gate `rotation_model` is off, a tool point p at time j breaks one of the four conditions of safety-model §8.1: disc(p, R_rot + ρ) not inside WA_wheel; annulus(p, r_min − ρ, R_rot + ρ) meets K_wheel ∪ Rem(j) ∪ Detached(j) during a translation; disc(p, R_rot + ρ) meets K_wheel ∪ Rem(j) ∪ Detached(j) at a rotation step; disc(p, R_body) meets a tower base. |
| `W-ROT` | refuse | link | A rotation step of a link (a G00 line that changes C and leaves X and Y unchanged) fails a wheel, structure or body check. Until the gate `rotation_model` is on, each step is checked as the full disc of radius R_rot + ρ about the pivot against WA_wheel (containment), K_wheel, Rem(j) and Detached(j), and the disc of radius R_body against the tower bases, whatever the direction, amount and pivot of the turn (safety-model §9.3); afterwards as the observed sweep. The witness names the failed rule (W-AREA, W-TOWER, W-SURF, M-ATTACH, B-TOWER, W-OBST, B-OBST). |
| `W-RAPID` | refuse | link | A translation of a link (a move above the stock at a fixed heading) fails a wheel, structure or body check at that heading, exactly as a cut piece would (safety-model §9.1); or a link line that rotates and translates together (the disclosed combined profile) fails them over its envelope, in both directions while the direction of the turn is not a fact (safety-model §9.4). A link that leaves the panel is refused here. The witness names the failed rule (W-AREA, W-TOWER, W-SURF, M-ATTACH, B-TOWER, W-OBST, B-OBST). |
| `W-FIRST` | refuse | link | The program's first line, from the start pose to the first cut, fails safety-model §9.5. No start pose (neither position frames of this session with the frame mapping a fact, nor the measured park pose, fact `detection.park_pose`, confirmed at the arming checklist): refused. While the gate `heading_map_verified` or `rotation_model` is off, the line is checked with the any-rotation envelope, the capsule of radius R_rot + ρ from the start (enlarged by the park-pose tolerance) to the target, against WA_wheel, K_wheel, obstacles and prior removal, and with radius R_body against the tower bases; afterwards with the observed rotation model. The witness names the failed rule (W-AREA, W-TOWER, W-SURF, M-ATTACH, B-TOWER, W-OBST, B-OBST). |
| `W-PARK` | refuse | program-end | After the last line (safety-model §9.6). While the fact `post_job.motion` is missing, through cuts and pockets stay refused: a program with a through cut or a pocket is refused, and so is a program on a panel whose prior removal (safety-model §7.1) holds a through cut or a pocket, and any other program carries the disclosure 'the robot may move after the job; keep clear'. Once the fact exists: the wheel discs at the final tool point, or along the modelled park path, meet the final removal or the final detached material, with the usual margins. |
| `W-OBST` | refuse | piece | During any piece, cuts and links, a wheel disc of radius ρ + e_j, swept along the piece, meets the footprint of an obstacle or fixation declared in job.toml, or a fixation point of the relaxation R-FIXED (safety-model §6.5). |
| `W-VENDORFLOOR` | internal-error | cut | Heading selection's vendor-floor assertion (safety-model §10): the heading chosen for a cut fails the vendor's own evaluation of that cut (spec 08 §Candidate search and selection: pass-1 bounds on the declared or SET PANEL SIZE rectangle, or pass 2 within declared supports, and the vendor's tower-zone test). Our checks are a superset of the vendor's, so this is a defect in the checker: the job is refused and a bug bundle is saved. |
| `B-TOWER` | refuse | piece | During piece j, the body hull, swept along the piece at its heading, meets a tower base at tower 1 or at either tower-2 placement (safety-model §5.2). For a rotation step, until the gate `rotation_model` is on, the swept region is the disc of radius R_body about the pivot. |
| `B-OBST` | refuse | piece | During any piece, the body hull, swept along the piece, meets the footprint of an obstacle or fixation declared in job.toml, or a fixation point of the relaxation R-FIXED (safety-model §6.5). |
| `M-ATTACH` | refuse | piece | During piece j, a wheel disc of radius ρ + e_j, swept along the piece, meets Detached(j): material no longer held to the towers' piece, whether this program or the prior removal freed it (safety-model §7.4). On a link piece the finding is reported as W-ROT, W-RAPID or W-FIRST with rule = M-ATTACH. |
| `M-TOWERSEP` | refuse | program | For some piece j, the first included, an anchor point a2 (the centroid of Z2(150), at each tower-2 placement) is not in Anchor(j), the component of the holding material M(j) that contains a1: a cut or the prior removal separates the two towers' corners, for example a cut across the panel between the towers or a full perimeter trim (safety-model §7.4). The witness is the separating piece. |
| `M-INVARIANT` | internal-error | startup | At start-up, with the active footprint, ρ, the tool and the check chord tolerance, the piece length Δ = min(50, floor(min_k r_k − ρ − r_tool − δ_tool − 2·e_max − 1)) is below 5 mm, so the exactness invariant min_k r_k − Δ > ρ + r_tool + δ_tool + 2·e_max of safety-model §7.3 cannot hold: the robot or tool is outside the model's assumptions. |
| `T-AREA` | refuse | in-material-piece | During an in-material piece j, the tool disc of radius r_tool + δ_tool + e_j, swept along σ_j, is not inside WA_tool, the work panel shrunk by m_edge_tool (safety-model §6.3). |
| `T-TOWER` | refuse | piece | During any piece, rapids included, the tool disc of radius r_tool + δ_tool + e_j, swept along the piece, meets a tool keep-out zone Z1(155) or Z2(155) inflated by m_tower, at both tower-2 placements, or a tower base (safety-model §5). The vendor tested only the toolpath (spec 08 §Tower keep-out zones); this check covers every move. |
| `T-DEPTH` | refuse | piece | A tool point is lower than z = −(T + overcut_max), deeper below the stock top than the tool's flute length or than the Z travel (58 mm), or higher than the Z upper limit (+40 mm) (safety-model §3.2). |
| `T-ATTACH` | refuse | in-material-piece | During an in-material piece j, the tool reaches more than 0.1 mm into Detached(j), whether this program or the prior removal freed it: a part already cut loose is never cut again (safety-model §7.4). |
| `T-TOOL` | refuse | tool | The program's tool is unknown (no declared diameter or flute length), its diameter exceeds 8 mm, or its declared stick-out exceeds 58 mm (spec README §Key reference constants). A tool without a declared stick-out is checked for diameter and flute length, and the report discloses that its stick-out was not checked. |
| `T-OBST` | refuse | piece | During any piece, the tool disc of radius r_tool + δ_tool meets the footprint of an obstacle or fixation declared in job.toml, or a fixation point of the relaxation R-FIXED, while the tool tip is lower than its height + 3 mm; or Z_safety is lower than the tallest declared obstacle or fixation + 3 mm (safety-model §6.5). |
| `T-PASS` | warn | in-material-piece | During an in-material piece, the step-down from the deepest earlier removal under the tool exceeds the tool's declared maximum step-down. Without a declared maximum the check is not evaluated, and the report discloses that. |
| `P-RANGE` | refuse | panel | The panel's width W or height H exceeds 3500 mm (spec README §Key reference constants). |
| `P-REACH` | refuse | piece | At some piece, links included, the wire attachment point is farther than reach_max (3500 mm) from tower 1, or from tower 2 at both tower-2 placements. Until rung R0b measures the attachment every point of the body hull counts as the attachment point; afterwards the measured point, which is the tool point when it lies on the spindle axis (safety-model §6.4). |
| `P-GEOM` | refuse | job-time | At job time the measured panel geometry fails safety-model §12: no vertex stream observed in this power cycle; fewer than 3 vertices, not a simple polygon, or zero area; Hausdorff distance to the declared rectangle above tol_panel; the INFO;27 distance not within tol_panel of the panel edge along the tower axis; the corner nearest tower 1 not at the origin; not exactly one decoding hypothesis passing (never tie-broken); or, for an upload, a passing hypothesis other than the identity frame. |
| `D-GI-VERSION` | refuse | source-line | GI reader (gi-format §12): line 1 is not a version line in a file with annotations; the version is not 1; a second version line. Reported at: 1, or the second version line. |
| `D-GI-SYNTAX` | refuse | source-line | GI reader (gi-format §12): an annotation-like comment that is not a well-formed annotation line (4.1). Reported at: that line. |
| `D-GI-HEADER` | refuse | source-line | GI reader (gi-format §12): a required header line or key missing; a header line repeated or after the header has ended; a header value of the wrong type or outside its domain. Reported at: that line; for a missing line, the line that ends the header. |
| `D-GI-OP` | refuse | source-line | GI reader (gi-format §12): `OP`/`OPEND` structure or values (section 7); motion outside every block below the stock top or with unknown Z. Reported at: that line; for a block never closed, its `OP` line. A forced heading `c` off the 10° grid is refused here, like the vendor's NaN (safety-model §10). |
| `D-GI-CONFLICT` | refuse | source-line | GI reader (gi-format §12): a header value disagrees with job.toml (section 10). Reported at: the GI header line. |
| `D-SYNTAX` | refuse | source-line | GI reader (gi-format §12): a lexical or line error (8.1 to 8.3, 8.6), non-ASCII outside a comment, a parenthesised comment, a `%` line. Reported at: that line and the column. The same lexical rules apply to generic G-code read with a job.toml and to the Goliath dialect in verify mode. |
| `D-UNSUPPORTED` | refuse | source-line | GI reader (gi-format §12): a word outside the vocabulary (8.5). Reported at: that line. For generic G-code the same vocabulary applies; in Goliath-dialect verify mode M10, M05, C, M11 and M06 are read for verification, and a C on a feed line is refused as D-CSTEP. |
| `D-UNITS` | refuse | source-line | GI reader (gi-format §12): `G20`; a `UNITS` other than `mm`. Reported at: that line. Generic G-code without G21 is refused here unless job.toml says units = "mm". |
| `D-RANGE` | refuse | source-line | GI reader (gi-format §12): a coordinate beyond ±10 000 mm; a negative `F` or `S`. Reported at: that line. |
| `D-ARC` | refuse | source-line | GI reader (gi-format §12): an arc that breaks section 8.7; `K`, `R`; `I` or `J` on a linear move. Reported at: that line. An arc whose end equals its start at file precision is a full circle; 0 < \|end − start\| < 0.01 mm is refused as ambiguous (gi-format §8.7). The same rule applies to generic G-code. |
| `D-AFTER-END` | refuse | source-line | GI reader (gi-format §12): a word or annotation after `M2`/`M30`; `M2`/`M30` sharing its line. Reported at: that line. |
| `D-MARKER` | refuse | source-line | GI reader (gi-format §12): a `;U` or `;S` marker (3.4). Reported at: that line. Outside Goliath-dialect verify mode only (spec 03 §G-code in-stream markers). |
| `D-SINGLETOOL` | refuse | source-line | The program uses more than one tool. GI reader (gi-format §12): a second tool (8.9). Reported at: that line. A job with two tools is two programs, which run as a series job (safety-model §13). |
| `D-ZSAFE` | refuse | reparsed-bytes | In the program parsed back from the upload bytes: a G00 move, or the Z line that follows a G00, below Z_safety; a descent by any move other than G01; a C change while Z is below Z_safety; any X or Y motion before the prologue's Z-only G01 raise to Z_safety (safety-model §11.5). |
| `D-SPINDLE` | refuse | reparsed-bytes | In the program parsed back from the upload bytes: motion in material with the spindle off; a plunge not preceded by M10; a G00 not preceded by M05 (spec 05 §Codes emitted). |
| `D-CSTEP` | refuse | reparsed-bytes | In the program parsed back from the upload bytes: a numeric \|ΔC\| of 180 or more on one line, unless the fact `rotation.model` allows a single short step across the wrap; a C off the 10° grid or outside 0 to 350; a C on a feed line; X or Y and C on one line, except on the first line or in the disclosed combined profile; a cumulative signed rotation beyond ±350° of the start heading under any allowed rotation model. A RungExemption (safety-model §14) lifts one named rule only on the named lines of the rung program whose SHA-256 it carries. |
| `D-FEED` | refuse | reparsed-bytes | In the program parsed back from the upload bytes: an F outside the tool's declared limits or above the feed cap; an S above the rpm cap; a feed move with no F in force; the first move after M10 not at the entry feed, or F and S not written again after a rapid (spec 05 §Semantics a replacement must match). |
| `D-WORDS` | refuse | reparsed-bytes | In the upload bytes: a word outside the emitted vocabulary (G00, G01, G02, G03, X, Y, Z, I, J, C, F, S, M10, M05); a byte outside ASCII; or bytes not in the vendor's cleaned form: no comments, CRLF line endings, ending in an empty line (spec 05 §What the robot actually receives). G02 and G03 while the gate `arcs_emit` is off are G-GATE. |
| `D-ROUNDTRIP` | refuse | reparsed-bytes | The program parsed back from the upload bytes differs from the plan by more than 0.0005 mm in any coordinate, or structurally: lines, words, their order, headings, spindle and feed state (safety-model §11.5). |
| `D-SIZE` | refuse | program | The cleaned upload bytes exceed max_program_bytes: 32 KiB until rung R10 records a larger verified size (safety-model §11.5). |
| `F-GATE` | refuse | job-time | At job time: the certificate's firmware version differs from the session's; the session's firmware version is unknown; or the program uses a feature that is gated off for that firmware (for example M06 below 01.02.02: spec 07 §Firmware gates the vendor app applies) (safety-model §12). |
| `G-GATE` | refuse | program | A feature is used while its gate is off; the finding names the gate and the rung that unlocks it (the gate table of safety-model §1.4). This includes rotate-in-place (split) link lines while the gate `rotate_in_place_ok` is off, M10 while `spindle_verified` is off, a tool point below `z_min_unverified` (Z = +5 mm) while `z_semantics_verified` is off, emitted G02 or G03 while `arcs_emit` is off, through cuts while `through_cuts_allowed` is off, pockets while `pockets_allowed` is off, and any program other than a rung program while `first_cut_done` is off. GI reader (gi-format §12): `G4` while the gate `drop_dwells` is closed. |
| `S-SERIES` | refuse | series | A series job (several programs on one panel) breaks safety-model §13: the gate `series_allowed` is off; the previous job's material state is missing; the re-detected panel differs from the previous one by more than 3 mm (Hausdorff), tower 2 is on another axis, or the tower distance changed by more than 3 mm; removal lies within the 450 mm detection-path band without the relaxation R-DETECT; or the drill-calibration spot is not known to be over intact material with margin. |
| `N-GI-UNKNOWN-KEY` | warn | source-line | GI reader (gi-format §12): an unknown annotation keyword or key, ignored. Reported at: that line. |
| `N-INFERRED-OPS` | disclose | program | The input has no operation blocks (generic G-code, or GI without OP lines): the operations were inferred from the Z segmentation and the report says so (safety-model §3.4). |
| `N-OPS-MISMATCH` | warn | program | The operation blocks of a GI file disagree with the segmentation by Z (a block whose moves do not form whole cuts, or cuts that span two blocks), or a block's through flag is contradicted by the geometry. Segmentation by Z and the geometry are used for every check; the warning names the OP line and the cuts (safety-model §3.4). |
| `N-DUST` | disclose | program | The estimated removed volume (removed area times depth, summed over the pieces) exceeds TANK_VOLUME, 1.15 dm³ (spec 08 §Extra moves): 'the vendor would dump dust here; M11 is not verified (rung R11)'. The arming checklist gains the item 'bin emptied / extraction connected'. |
| `N-VENDORC` | disclose | cut | The vendor's heading for a cut (spec 08 §Candidate search and selection) fails our checks and another heading was chosen; the notice names the vendor's C, the first failing check and its witness, for example 'vendor would choose C = 30; rear wheel enters the kerf of cut 4 at line 812' (safety-model §10). |
| `N-BASELINE` | disclose | tool-point | A tool point lies within 300 mm of the tower baseline, the panel edge between the two towers, where positioning accuracy is expected to be lowest (prior-art review §4.1, an inference there) (safety-model §6.2). |

## 12. Job-time geometry and firmware

The panel is known exactly only on the robot. At the desk the plan is validated
against the declared panel shrunk by `geom_tol`, with tower zones grown by
`geom_tol` and tower 2 at both placements. At job time, after panel detection:

1. **Capture** the raw `INFO;25` lines and `INFO;27`, in order, with times
   ([spec 03 §Full job flow](../protocol/03-session-sequence.md#full-job-flow),
   steps D and D2).
2. **Decode** under every hypothesis still allowed (U-13): the encodings `x;y` and
   `length;angle`, times the eight axis maps that keep tower 1 at the origin
   (identity, swap, the mirrors about the axes through the origin, the half turn,
   and the swapped forms of those).
3. **Validate**: at least 3 vertices, a simple polygon, area above zero; Hausdorff
   distance to the declared rectangle at most `tol_panel`; `INFO;27` within
   `tol_panel` of the panel edge along the tower axis; the corner nearest tower 1 at
   the origin. **Exactly one hypothesis** must pass, with no tie-break, ever. On a
   near-square panel (|W − H| <= 2·`tol_panel` + 10 mm) the operator confirms the
   tower-2 axis at the arming checklist against the photographed placement
   (PHYS-11). **Uploads need the identity hypothesis** (section 3.1).
4. **Derive** `P_meas`, the `SET PANEL SIZE` integers x = floor(max x),
   y = floor(max y), and `P_work` = `P_meas` ∩ [0, x] x [0, y], so that the robot's own
   panel limit, if it enforces one, agrees with ours (U-15).
5. **Revalidate**: carried over (the measured panel contains the desk panel, tower
   distance and origin within `geom_tol`, same tower-2 axis, the desk result
   computed in this session with the same software, facts and profile: by
   monotonicity the proof holds), re-checked on the unchanged bytes, re-planned on
   request with the operator's approval, or refused.

`P-GEOM` refuses a measured geometry that fails steps 3 or 4. **No geometry, no
job:** if the vertex stream was not observed in this power cycle, uploading is
refused.

`F-GATE` refuses a job whose certificate names another firmware version than the
session's, a session whose firmware version is unknown (U-01), and a program that
uses a feature gated off for that firmware (for example `M06` below 01.02.02:
[spec 07 §Firmware gates the vendor app applies](../protocol/07-version-compatibility.md#firmware-gates-the-vendor-app-applies)).

## 13. Series jobs

Several programs on one panel (more jobs, or one program per tool) are a series,
gated by `series_allowed` (rung R15). `S-SERIES` refuses a series job unless:

- job n's input material is job n−1's material state; a job that did not report
  `INFO;23` contributes its **whole planned removal**, not the removal up to its
  last reported line;
- the re-detected panel matches the previous one within 3 mm (Hausdorff), tower 2
  is on the same axis and the tower distance is within 3 mm: otherwise "towers or
  panel moved: the earlier cuts cannot be located";
- no removal lies within 450 mm of the panel edge (INFERRED: detection traces the
  panel edge, U-12) unless the relaxation R-DETECT is open;
- the robot's drill calibration, which happens after upload at a place nobody has
  observed yet (U-43), is predicted over intact material with margin.

## 14. Rung programs and their exemptions

The rung programs (R3, R3b, R4) test behaviours that ordinary programs must not
rely on: a deliberate single wrap step, a combined rotate-and-translate line,
per-line headings, programs with no cut at all. GI cannot express them, and
`D-CSTEP` refuses some of their lines. So:

- Each rung program is generated directly as Goliath-dialect text from a fixed
  template and certified in verify mode: its `C` values are checked, never chosen.
- Exceptions to `D-CSTEP` are carried by a **RungExemption** that only the rung
  generator can construct. It names the rung, the exact line numbers, the rule each
  line is exempt from, and the SHA-256 of the generated bytes. A certification of
  kind `rung` refuses an exemption whose hash differs from the payload's, so it
  cannot be applied to any other program.
- Every other check runs in full on every line: `W-SAFEZONE`, the any-rotation
  envelope for the first line and for the combined line in both directions,
  `W-AREA`, `W-TOWER`, `D-ZSAFE`, `D-SPINDLE`. The R3 first-line alternative of
  section 9.5 is not an exemption of this kind and is not available in version 1.
- The exemption is recorded in the certificate and disclosed on the arming screen
  and in the report. Rung gates are never facts and never enter the fact snapshot.

## 15. What one heading per cut can certify

The default model holds one heading for each cut (as the vendor did:
[spec 08 §Purpose](../protocol/08-head-angle-and-cut-order.md#purpose)) and treats
every removal deeper than 0.02 mm as a wheel keep-out (U-41). Under it, a closed
profile larger than about 200 to 220 mm across cannot be certified at any single
heading.

**The limit is geometric: it is not caused by the unmeasured wheel contact
radius.** With one heading held round a closed outline, each support's path is the
outline translated by a fixed offset, `d_k(θ)`: 402 mm behind the tool for the rear
support, 211 mm at ±76° for the side supports. Once the outline is larger than an
offset, the translated copy crosses the outline, and going round a closed loop a
support then reaches some crossing point after the tool has cut it. In the
computations below that happened for every rectangle tried from 220 x 220 mm up,
at every heading, in both directions and from every start point tried. The side
supports' 211 mm is the shortest offset, which is why the limit sits near 200 to
220 mm across. The project design first explained the limit by the side supports
sitting 51 mm off a cut square to the body against ρ + r_tool + δ_tool = 58.25 mm,
which is incomplete because at ρ = 15 mm the clearance needed is 18.25 mm, less
than 51 mm, and the limit is unchanged. Measuring `r_w` will not lift this limit.

The data, computed from the footprint model, not measured on a robot: closed
rectangular profiles under `W-SURF` with the spec footprint, a 6 mm bit with
0.25 mm runout, removal in time order along the path (0.5 mm steps; 1 mm where
every start point was tried), all 36 headings, both directions. Each cell counts
the headings at which the profile certifies for at least one of the directions and
start points tried.

| Closed rectangle | ρ = 15 mm | ρ = 25 mm | ρ = 35 mm | ρ = 55 mm |
|---|---|---|---|---|
| 100 x 100 mm | 36 of 36 headings | 36 of 36 | 36 of 36 | 36 of 36 |
| 200 x 150 mm | 19 of 36 | 15 of 36 | 15 of 36 | 6 of 36 |
| 220 x 220 mm, 240 x 240 mm | none | not computed | not computed | not computed |
| 250 x 250, 300 x 200, 400 x 300, 700 x 500 mm | none | none | none | none |

Three start points per direction were tried, except at ρ = 15 mm for the four
largest rectangles and for 220 x 220 and 240 x 240 mm, where every start point on
the outline was tried at 1 mm steps (and for 250 x 250 mm at ρ = 55 mm too). An
open, straight cut is different: by the same computation a 500 mm straight cut
certifies at 27 of the 36 headings at ρ = 55 mm.

What follows, stated rather than hidden:

- From the first cut (rung R5) until more evidence exists, version 1 certifies open
  cuts, grooves that the wheels never cross, and closed profiles up to roughly
  200 mm across. Typical cabinet sides are refused.
- There are two paths to larger closed parts, and neither loosens `W-SURF`:
  1. **Split cuts at different headings** (unit P23): a closed profile is split at
     joints into segments with a retract, a rotation in place and a re-plunge, each
     at its own heading, proposed as a plan change the operator approves, fully
     re-certified, with the joint marks disclosed. Whether this makes typical
     profiles certifiable is measured by an acceptance profile, not assumed.
  2. **Kerf crossing** once rung R8k (scheduled right after R7) has shown with
     measured values that a wheel crosses a narrow kerf safely: the relaxation
     R-KERF.

**INFERENCE**, from the specification and the geometry above, not an observation:
the vendor also held one heading per cut and tested no footprint against removed
material ([spec 05 §Toolpath ordering and "don't fall into cuts"](../protocol/05-gcode-dialect.md#toolpath-ordering-and-dont-fall-into-cuts)),
so its robots must routinely have rolled a support over fresh kerf when they cut a
closed part larger than about 21 cm across.

## 16. Changes

| Version | Date | Change |
|---|---|---|
| 1 | 2026-11-04 | First version. |
