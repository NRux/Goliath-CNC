<!-- SPDX-License-Identifier: Apache-2.0 -->
# The Goliath safety model: parameter catalogue, version 1

*Normative. Every number a check of the [safety model](safety-model.md) uses, with
its default, unit, provenance and source. The machine-readable form is
[`params.json`](params.json); this page is the same catalogue for people, and a
test keeps the two equal. Licensed Apache-2.0, like the safety model and
[`docs/protocol/`](../protocol/README.md) ([`LICENSING.md`](../../LICENSING.md)).*

## 1. What this catalogue is

A check never uses a number that is not in this catalogue, and the report of every
job lists each number it used with its provenance. A default is never silent: its
class says where it comes from, and the report and the certificate carry that class
and the source. A value that nobody has measured raises the amber banner
"UNMEASURED MACHINE PROFILE" until it is measured.

## 2. Provenance classes

Each parameter's `provenance` is the class of the value this catalogue ships:

| Class | Meaning | Shown as | Replaced by |
|---|---|---|---|
| `SPEC` | stated in the protocol specification in `docs/protocol/`; `source_citation` names the section, and the section contains the value | the spec section | for the footprint, nothing: a measurement adds to it (section 2.2) |
| `DEFAULT_UNMEASURED` | a physical property of the robot that nobody has measured; the default is a conservative guess | amber banner "UNMEASURED MACHINE PROFILE" | `ROBOT_MEASURED`, from field guide 04 at rung R0b |
| `INFERRED` | a value this project reasoned out because no source states it; tied to an unknown (`unknown`) | the report's parameter table, with the unknown | the rung named in `unlock_rung`, or the owner where noted |
| `POLICY` | a margin or limit this project chose; the owner may only tighten it | the report's parameter table | an owner profile value (`OWNER_PROFILE`, disclosed), never looser |
| `GATE` | a value that depends on a feature gate: the default holds until the rung in `unlock_rung` records its fact, and `after_unlock` (when given) applies after | the gate table | `FACT`, from that rung |
| `DERIVED` | computed from other parameters by `formula`; the default is the value with the other defaults | the formula and its inputs | recomputed from its inputs |
| `OWNER_JOB` | an input of the job (job.toml, compared with the GI header: gi-format §10); `required` says whether a job without it is refused | the job's value and its source | the measured panel, at job time, for the panel size |

### 2.1 The value a check uses

At run time each value a check uses carries one of these sources: the class above,
or the source that replaced it: `ROBOT_MEASURED` (a typed measurement of this
robot, with tolerance and the operator's name), `OWNER_PROFILE` (a hand-edited
machine profile: disclosed, and it opens no gate), `FACT` (evidence recorded at a
rung for this robot and firmware) or `OWNER_JOB`. The certificate records the
source of every value it binds.

### 2.2 Measured at rung R0b

Field guide 04 ([`04-measure-your-robot.md`](../field-guide/04-measure-your-robot.md))
records the robot's measurements under stable field ids
([`04-measure-your-robot.fields.json`](../field-guide/04-measure-your-robot.fields.json)).
`measured_by` names the fields that measure a parameter, and `measured_rule` says
how they become its value:

- **Wheel contact radius `r_w`**: the **maximum** of
  `wheel.q1.contact_radius_mm`, `wheel.q2.contact_radius_mm` and
  `wheel.q3.contact_radius_mm`, never the mean, because one `r_w` serves every
  wheel. The three readings are kept with it. If any of the three reads
  `unknown`, the module nobody measured could be the widest: `r_w` stays
  `DEFAULT_UNMEASURED` with the amber banner, and is the larger of the 40 mm
  default and the readings that are known.
- **Support distances and angles**: `footprint.q1_mm`, `footprint.q2_mm`,
  `footprint.q3_mm`, `footprint.q1_deg`, `footprint.q2_deg`, `footprint.q3_deg`
  add a measured footprint; the checks then use the union of the spec footprint
  and the measured one (safety-model §4.3). The spec values stay. A support whose
  distance or angle reads `unknown` adds no measured position.
- **Body hull**: the body extents (`body.ahead_of_tool_mm`, `body.behind_tool_mm`,
  `body.left_of_axis_mm`, `body.right_of_axis_mm`, `body.max_radius_mm`) or the
  outline (`body.outline_points`); the hull becomes the convex hull of the wheel
  discs and the measured outline. Without an outline, one `unknown` extent keeps
  the default hull.
- **Tower base**: `tower.base_radius_mm` replaces the 120 mm default; `unknown`
  keeps it.
- **Wire attachment**: `wire.attachment`, `wire.same_point`, `wire.back_mm`,
  `wire.right_mm`; until they are measured, `P-REACH` counts the whole body hull,
  and an `unknown` among them keeps it so.

Field guide 04 asks for a value or the word `unknown` on every line, and
`unknown` keeps the cautious default: a partial measurement is never read as a
complete one.

### 2.3 Overrides

`overridable` says whether the owner may set a value less conservative than the
default through a typed, recorded override. In version 1 no parameter is
overridable: the project design lets owner policy only tighten, except where it
marks an item overridable, and it marks no parameter. Gated values change only
through the facts of their rung.

## 3. The parameters

Values are the defaults with the spec footprint. "none" means no default: a
required job input is refused when missing; an optional one, when missing, leaves
the check that uses it unevaluated, and the report says so. The source, the
measured fields, the formula and notes of each parameter are in `params.json`.

### 3.1 Footprint and heading

| Name | Symbol | Default | Unit | Provenance | Unlock rung |
|---|---|---|---|---|---|
| `support_r1` | r_1 | 402 | mm | SPEC | R0b |
| `support_r2` | r_2 | 211 | mm | SPEC | R0b |
| `support_r3` | r_3 | 211 | mm | SPEC | R0b |
| `support_delta1` | δ_1 | 0 | deg | SPEC | R0b |
| `support_delta2` | δ_2 | 76 | deg | SPEC | R0b |
| `support_delta3` | δ_3 | -76 | deg | SPEC | R0b |
| `heading_offset` | θ = (C + 90) mod 360 | 90 | deg | SPEC | R3 |
| `c_grid_step` | C grid | 10 | deg | SPEC | - |
| `r_max` | R_max | 402 | mm | DERIVED | - |
| `r_min` | r_min | 211 | mm | DERIVED | - |
| `support_centroid_distance` | d_c | 168.03 | mm | DERIVED | - |
| `support_centroid_radius` | R_c | 235.8 | mm | DERIVED | - |
| `r_rot` | R_rot | 404 | mm | DERIVED | - |

### 3.2 Wheels and body

| Name | Symbol | Default | Unit | Provenance | Unlock rung |
|---|---|---|---|---|---|
| `r_w` | r_w | 40 | mm | DEFAULT_UNMEASURED | R0b |
| `m_w` | m_w | 15 | mm | POLICY | - |
| `rho` | ρ | 55 | mm | DERIVED | - |
| `body_hull_margin` | m_body | 60 | mm | DEFAULT_UNMEASURED | R0b |
| `r_body` | R_body | 519 | mm | DERIVED | - |
| `wire_attachment` | wire attachment | body-hull | enum | DEFAULT_UNMEASURED | R0b |

### 3.3 Margins and tolerances

| Name | Symbol | Default | Unit | Provenance | Unlock rung |
|---|---|---|---|---|---|
| `m_edge` | m_edge | 50 | mm | GATE | R9 |
| `m_edge_baseline` | m_edge on the tower baseline | 50 | mm | GATE | R9 |
| `m_edge_tool` | m_edge_tool | 2 | mm | POLICY | - |
| `m_tower` | m_tower | 25 | mm | POLICY | R9 |
| `geom_tol` | geom_tol | 10 | mm | POLICY | - |
| `tol_panel` | tol_panel | 25 | mm | POLICY | - |
| `near_square_extra` | near-square margin | 10 | mm | POLICY | - |
| `delta_tool` | δ_tool | 0.25 | mm | POLICY | - |
| `overcut_max` | overcut_max | 0.5 | mm | POLICY | - |
| `slack_min` | slack_min | 0 | mm | POLICY | - |
| `attach_tol` | T-ATTACH tolerance | 0.1 | mm | POLICY | - |
| `obstacle_clearance` | obstacle clearance | 3 | mm | POLICY | - |
| `roundtrip_tol` | round-trip tolerance | 0.0005 | mm | POLICY | - |
| `park_pose_tol` | park-pose tolerance | 20 | mm | POLICY | - |
| `park_pose_tol_deg` | park-heading tolerance | 5 | deg | POLICY | - |
| `r3_park_heading_sweep` | R3 park-heading sweep | 10 | deg | POLICY | - |

### 3.4 Towers, panel and reach

| Name | Symbol | Default | Unit | Provenance | Unlock rung |
|---|---|---|---|---|---|
| `tower_zone_leg_wheel` | wheel zone leg | 150 | mm | SPEC | - |
| `tower_zone_leg_tool` | tool zone leg | 155 | mm | SPEC | - |
| `tower_base_radius` | tower base radius | 120 | mm | DEFAULT_UNMEASURED | R0b |
| `panel_max_side` | max W, H | 3500 | mm | SPEC | - |
| `reach_max` | reach_max | 3500 | mm | INFERRED | - |
| `safe_zone_inset_desk` | safe-zone inset | 519 | mm | DERIVED | - |
| `baseline_band` | baseline band | 300 | mm | INFERRED | R9 |

### 3.5 Tool and Z

| Name | Symbol | Default | Unit | Provenance | Unlock rung |
|---|---|---|---|---|---|
| `z_safety` | Z_safety | 10 | mm | SPEC | - |
| `z_upper_limit` | Z upper limit | 40 | mm | INFERRED | - |
| `z_min_unverified` | lowest Z before z_semantics_verified | 5 | mm | GATE | R3 |
| `z_travel` | Z travel | 58 | mm | SPEC | - |
| `tool_max_diameter` | max bit Ø | 8 | mm | SPEC | - |
| `tool_max_length` | max bit length | 58 | mm | SPEC | - |
| `feed_cap` | feed cap | 2000 | mm/min | INFERRED | R5 |
| `rpm_cap` | rpm cap | 27000 | rpm | INFERRED | R5 |

### 3.6 Material, structure and pieces

| Name | Symbol | Default | Unit | Provenance | Unlock rung |
|---|---|---|---|---|---|
| `eps_surf` | ε_surf | 0.02 | mm | POLICY | - |
| `through_tol` | through tolerance | 0.05 | mm | POLICY | - |
| `t_hold` | t_hold | none | mm | INFERRED | R7 |
| `piece_length_max` | Δ | 50 | mm | DERIVED | - |
| `piece_length_min` | Δ_min | 5 | mm | POLICY | - |
| `check_chord_tol` | e_max | 0.05 | mm | POLICY | - |
| `emit_arc_tol` | emit arc tolerance | 0.02 | mm | POLICY | - |
| `helix_tol` | helix tolerance | 0.02 | mm | POLICY | - |
| `arc_radius_tol` | arc radius tolerance | 0.005 | mm | POLICY | - |
| `arc_closure_tol` | arc closure tolerance | 0.01 | mm | POLICY | - |
| `coordinate_limit` | coordinate limit | 10000 | mm | POLICY | - |

### 3.7 Headings and rotation

| Name | Symbol | Default | Unit | Provenance | Unlock rung |
|---|---|---|---|---|---|
| `c_step_max` | \|ΔC\| limit | 180 | deg | POLICY | R3 |
| `cumulative_rotation_max` | net rotation bound | 350 | deg | INFERRED | R0b |

### 3.8 Relaxations (closed by default)

| Name | Symbol | Default | Unit | Provenance | Unlock rung |
|---|---|---|---|---|---|
| `kerf_width_max` | w_b | 0 | mm | GATE | R8k |
| `kerf_ride_max` | L_ride | 0 | mm | GATE | R8k |
| `recess_ignore_max` | h_r | 0 | mm | GATE | R8k |

### 3.9 Program, upload and notices

| Name | Symbol | Default | Unit | Provenance | Unlock rung |
|---|---|---|---|---|---|
| `max_program_bytes` | max_program_bytes | 32768 | byte | GATE | R10 |
| `upload_block` | upload block | 1024 | byte | SPEC | - |
| `tank_volume` | TANK_VOLUME | 1.15 | dm3 | SPEC | - |
| `battery_ack_below` | tower battery threshold | 50 | percent | POLICY | - |
| `battery_ack_job_min` | job length for the battery rule | 15 | min | POLICY | - |

### 3.10 Series jobs (gated)

| Name | Symbol | Default | Unit | Provenance | Unlock rung |
|---|---|---|---|---|---|
| `series_band` | detection-path band | 450 | mm | INFERRED | R15 |
| `series_redetect_tol` | re-detection tolerance | 3 | mm | POLICY | - |

### 3.11 Job inputs (job.toml and the GI header)

| Name | Symbol | Default | Unit | Provenance | Unlock rung |
|---|---|---|---|---|---|
| `panel_width` | W | none | mm | OWNER_JOB | - |
| `panel_height` | H | none | mm | OWNER_JOB | - |
| `stock_thickness` | T | none | mm | OWNER_JOB | - |
| `tower2_axis` | tower-2 axis | none | enum | OWNER_JOB | - |
| `tool_diameter` | 2·r_tool | none | mm | OWNER_JOB | - |
| `tool_flute_length` | flute length | none | mm | OWNER_JOB | - |
| `tool_stickout` | stick-out | none | mm | OWNER_JOB | - |
| `tool_max_stepdown` | max step-down | none | mm | OWNER_JOB | - |
| `support_strip_width` | support strip width | 0 | mm | OWNER_JOB | - |
| `placement_dx` | dx | 0 | mm | OWNER_JOB | - |
| `placement_dy` | dy | 0 | mm | OWNER_JOB | - |

## 4. Fields of params.json

`params.json` is a JSON list with one object per parameter:

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | the parameter's name, unique |
| `default` | yes | the shipped value; `null` only for a job input or a value with a `formula` over one |
| `unit` | yes | `mm`, `deg`, `mm/min`, `rpm`, `byte`, `dm3`, `percent`, `min`, or `enum` (with `choices`) |
| `provenance` | yes | the class, section 2 |
| `source_citation` | yes | where the value comes from: spec sections, the prior-art review, gi-format and safety-model sections, field guide 04 |
| `overridable` | yes | section 2.3 |
| `unlock_rung` | yes | the rung whose evidence can change the value, or `null` |
| `symbol` | no | the symbol the safety model uses |
| `formula` | for `DERIVED` | how the value is computed |
| `measured_by`, `measured_rule` | together | field guide 04 field ids, and how they become the value |
| `unknown` | no | the unknowns (`U-nn`, safety-model §1.4) the value stands in for |
| `required` | for `OWNER_JOB` | whether a job without the input is refused |
| `after_unlock` | no | the value once the gate of `unlock_rung` is on |
| `choices` | for `enum` | the allowed values |
| `note` | no | anything else a reader needs |

## 5. Changes

| Version | Date | Change |
|---|---|---|
| 1 | 2026-11-04 | First version. |
