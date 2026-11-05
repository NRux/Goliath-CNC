<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# The red-team suite: jobs goliath-prep must refuse

Each folder under [`cases/`](cases/) is a job that `goliath-prep` **must refuse**,
with the check that must refuse it and where. The cases are RT1 to RT15 and RT19
to RT35 of the project design's red-team list (section 10.3); a case the list
states as several defects is split into lettered variants, one defect per folder.
RT16 and RT17 (series jobs) belong to unit P19's suite, RT18 (certificate replay)
to the runner's (unit R08).

The cases were written from the normative documents only:
[`docs/prep/safety-model.md`](../../../docs/prep/safety-model.md) and
[`checks.json`](../../../docs/prep/checks.json),
[`docs/prep/gi-format.md`](../../../docs/prep/gi-format.md) and
[`docs/protocol/`](../../../docs/protocol/README.md), never from goliath-prep's
check code. Every case cites the sections it rests on (`defined_in`).

## Running it

The suite runs with goliath-prep's tests (Python 3.11 or newer):

```
python -m pytest goliath-prep/tests/redteam
```

* `test_prep_redteam.py`: one test per case folder. It runs the case through
  goliath-prep's pipeline and requires the refusal and witness of
  `expected.json`. **While the pipeline entry point `goliath_prep.api` does not
  exist (unit P17), each of these tests is skipped with that reason**; once it
  exists, they run and a case that is not refused as expected fails. Nothing is
  marked xfail.
* `test_prep_redteam_cases.py` (runs now): every case is well formed. The files
  are those of the format below; `expected.json` names a check of `checks.json`
  with witness fields that check fills; `gates.json` names every gate of
  safety-model 1.4; `measured.json` holds raw `INFO;25`/`INFO;27` lines and a valid
  park pose; `job.toml` and `machine.toml` load with goliath-prep's own readers;
  a GI program's header is valid under [`gi.schema.json`](../../../docs/prep/gi.schema.json);
  a reader case carries exactly the defect it names, at its line, and a geometric
  case reads cleanly. The suite's small GI reader is checked against the
  conformance examples of [`docs/prep/gi-examples/`](../../../docs/prep/gi-examples/manifest.json).
* `test_prep_redteam_derivations.py` (runs now): each geometric case's claim is
  re-derived from the safety model's own formulas and the numbers of
  `params.json` (supports, wheel discs, zones, removal in time, detachment,
  links, the first line), sampled at about 1 mm. The expected check must fail
  where `expected.json` says; every other check that fails must be listed in
  the case. An unforced cut must fail its check at all 36 headings.
* `test_prep_redteam_harness.py` (runs now): the adapter itself, with a stand-in
  pipeline: the arguments it passes, when it skips and when it fails, and how
  it compares a result with `expected.json`.

`prep_redteam_model.py` and `prep_redteam_derive.py` are test code: a
deliberately simple re-derivation, not goliath-prep's checker and not the
independent oracle of the project design (section 10.4).

## The case format

| File | Required | What it holds |
|---|---|---|
| `case.gi` | one of the two | the program in GI ([`gi-format.md`](../../../docs/prep/gi-format.md)), LF line ends, ending in a plain licence comment |
| `case.nc` | one of the two | a Goliath-dialect program, verified as written (never emitted again): the vendor's cleaned form, ASCII, CRLF, ending in an empty line ([spec 05](../../../docs/protocol/05-gcode-dialect.md)) |
| `job.toml` | yes | the job file, version 1 (goliath-prep's `jobspec`): panel, placement, Z zero, units, tool, and any supports, obstacles, fixations and prior removal |
| `expected.json` | yes | the expected outcome (below) |
| `gates.json` | no | the gate snapshot the case runs under; without it, goliath-prep's all-closed desk view |
| `measured.json` | no | what the robot measured: raw `INFO;25` lines, the `INFO;27` line, the park pose |
| `machine.toml` | no | the robot profile; without it, [`profiles/unmeasured.toml`](profiles/unmeasured.toml) (nothing measured) |
| `prior.json` | no | series state; no case here has one (series cases are unit P19's), and the adapter fails on one rather than drop it |

### expected.json

```json
{
  "schema": "goliath-prep-redteam-expected/1",
  "case": "RT9", "variant": null,
  "title": "Heading change 350 -> 0 next to a kerf crossed by the long swing",
  "verdict": "refused",
  "check_id": "W-ROT",
  "witness": {"wheel": 3, "near": [1170.0, 811.0], "tol_mm": 60.0, "rule": "W-SURF"},
  "also_refused": [],
  "co_occurring": [],
  "rationale": "...", "defined_in": ["safety-model.md#93-rotation-sweeps"], "review": []
}
```

* `verdict`: `refused` — the job is not accepted (`CertResult.ok` is false) and a
  finding of severity `refuse` with `check_id` has a witness that meets
  `witness`; each entry of `also_refused` must be found the same way.
  `check-lifted` — the variant runs under a gate snapshot that opens a
  relaxation, and `check_id` must not be raised at all (the job may still be
  refused for other reasons).
* `witness` keys, each optional: `cut` (the cut index, counted from 0 in program
  order), `wheel` (1 rear, 2 right, 3 left), `near` and `tol_mm` (the witness
  point lies within `tol_mm` of `near`, panel frame, mm), `line` and `column`
  (of the witness's source line), `rule` (for `W-ROT`, `W-RAPID`, `W-FIRST`),
  `gate` and `unlock_rung` (for `G-GATE`), `source_values` (a conflict names
  both sources, one of them `job.toml`, with these values), `text` (words the
  finding's message or hazard must contain), and `any_of` (a list of
  alternatives, any one of which must match). Only witness fields the check
  lists in `checks.json` are used.
* `heading_table_rows` (RT2): the report must hold the 36-row heading table of
  safety-model 10: one row per C from 0 to 350.
* `co_occurring`: checks that also fail by construction (for example `W-PARK`
  after a through cut). They are documentation; the derivation tests require
  every other failing check to be listed here.

### gates.json

`{"schema": "goliath-prep-redteam-gates/1", "description": ..., "gates": {...}}`
with **every** gate of safety-model 1.4, in its order, each
`{"allowed": true | false | null}` and, for an open gate, an optional `value`:
`rotation_model` the observed model (`in-place-shortest`, `in-place-numeric`,
...), `post_job_motion_known` the fact `post_job.motion` (`none`),
`start_pose_known` the park pose (the same record as `measured.json`'s),
`tabs_hold` the lowered `t_hold` in mm, `kerf_crossing` the measured
`kerf_width_max`, `kerf_ride_max` and `recess_ignore_max` (params.json names).
Most cases run under a "brought-up robot" snapshot: every capability gate open,
every relaxation closed, the edge margin at 50 mm.

### measured.json

`{"schema": "goliath-prep-redteam-measured/1", "info25": [...], "info27": ..., "park_pose": {...}}`:
the raw lines as firmware 01.02.00 sends them (`INFO;25;%.4f;%.4f`,
`INFO;27;%.4f`: spec 02 and spec 09), and the park pose in the form of
goliath-prep's `Pose` (`x`, `y`, `c`, `source`). A desk case has an empty
`info25` and only the park pose (the fact `detection.park_pose` of rung R2).

## The adapter (what the pipeline must accept)

[`prep_redteam_cases.py`](prep_redteam_cases.py) is the one place that knows
the pipeline's API. It follows unit P17's documented interface,
`goliath_prep.api.prepare(sources, job, profile, gates, prior=())` returning a
`CertResult` with `ok`, `findings` (goliath-prep `Finding` objects) and
`report`, and makes two choices of its own:

* `sources` maps the input file's name (`case.gi`, or `case.nc` for a dialect
  program) to its exact bytes;
* a case with `measured.json` passes it as `measured=`, a read-only mapping
  with `info25` (tuple of lines), `info27` (line or None) and `park_pose` (a
  `Pose` or None). P17's interface has no such parameter yet; until the
  pipeline takes one, those cases fail loudly instead of running without
  their measurements.

## The cases

| Folder | Must be refused | Expected | Files |
|---|---|---|---|
| [`RT01-rear-wheel-follows-kerf`](cases/RT01-rear-wheel-follows-kerf/) | Long straight through cut along +y at C = 0; the rear wheel follows the kerf | W-SURF | case.gi, gates, measured |
| [`RT02-every-heading-puts-a-wheel-in-the-pocket`](cases/RT02-every-heading-puts-a-wheel-in-the-pocket/) | A pocket, then a profile whose every feasible heading puts a wheel in the pocket | W-SURF, 36-row table | case.gi, gates, measured |
| [`RT03-outline-closed-wheels-inside-then-hole`](cases/RT03-outline-closed-wheels-inside-then-hole/) | Outline closed with wheels inside the part; then an inside hole after the outline | M-ATTACH, T-ATTACH | case.gi, gates, measured |
| [`RT04-perimeter-trim-frees-the-interior`](cases/RT04-perimeter-trim-frees-the-interior/) | Full perimeter trim: the trimmed interior, where the robot stands, is cut free of the towers' frame | M-ATTACH, T-ATTACH | case.gi, gates, measured |
| [`RT04b-perimeter-trim-10mm-from-the-edges`](cases/RT04b-perimeter-trim-10mm-from-the-edges/) | Full perimeter trim 10 mm from the edges, as the design's case list states it | T-AREA | case.gi, gates, measured |
| [`RT05-cut-across-separates-the-towers`](cases/RT05-cut-across-separates-the-towers/) | A straight through cut across the panel separating the tower corners | M-TOWERSEP | case.gi, gates, measured |
| [`RT06a-toolpath-enters-the-155mm-triangle`](cases/RT06a-toolpath-enters-the-155mm-triangle/) | A toolpath entering the 155 mm triangle at tower 1 | T-TOWER | case.gi, gates, measured |
| [`RT06b-rapid-over-the-155mm-triangle`](cases/RT06b-rapid-over-the-155mm-triangle/) | A rapid passing over the 155 mm triangle (Goliath dialect, verify mode) | T-TOWER | case.nc, gates, measured |
| [`RT07-wheel-crosses-tower-zone-between-vertices`](cases/RT07-wheel-crosses-tower-zone-between-vertices/) | A wheel crossing tower 1's triangle between two vertices that are outside it | W-TOWER | case.gi, gates, measured, machine |
| [`RT08-arc-bulge-into-tower-zone`](cases/RT08-arc-bulge-into-tower-zone/) | An arc whose bulge carries a wheel into tower 1's zone, with both ends outside | W-TOWER | case.gi, gates, measured, machine |
| [`RT09-heading-350-to-0-long-swing-over-kerf`](cases/RT09-heading-350-to-0-long-swing-over-kerf/) | Heading change 350 -> 0 next to a kerf crossed by the long swing | W-ROT | case.gi, gates, measured |
| [`RT10-rapid-drags-wheel-across-slot`](cases/RT10-rapid-drags-wheel-across-slot/) | A rapid whose wheel track crosses an earlier slot | W-RAPID | case.gi, gates, measured |
| [`RT11-depth-t-plus-2`](cases/RT11-depth-t-plus-2/) | A cut at depth -(T + 2) | T-DEPTH | case.gi, gates, measured |
| [`RT12a-g91-incremental`](cases/RT12a-g91-incremental/) | G91 in a GI file | D-UNSUPPORTED | case.gi |
| [`RT12b-g20-inch`](cases/RT12b-g20-inch/) | G20 in a GI file | D-UNITS | case.gi |
| [`RT12c-g41-compensation`](cases/RT12c-g41-compensation/) | G41 in a GI file | D-UNSUPPORTED | case.gi |
| [`RT12d-g81-drill-cycle`](cases/RT12d-g81-drill-cycle/) | G81 in a GI file | D-UNSUPPORTED | case.gi |
| [`RT12e-m4-reverse-spindle`](cases/RT12e-m4-reverse-spindle/) | M4 in a GI file | D-UNSUPPORTED | case.gi |
| [`RT12f-g4-dwell`](cases/RT12f-g4-dwell/) | G4 in a GI file while the gate drop_dwells is closed | G-GATE | case.gi |
| [`RT12g-non-ascii-byte`](cases/RT12g-non-ascii-byte/) | A non-ASCII character outside a comment | D-SYNTAX | case.gi |
| [`RT13a-cut-with-spindle-off`](cases/RT13a-cut-with-spindle-off/) | A cut with the spindle off (Goliath dialect, verify mode) | D-SPINDLE | case.nc, gates, measured |
| [`RT13b-rapid-at-z2`](cases/RT13b-rapid-at-z2/) | A rapid at Z = 2 (Goliath dialect, verify mode) | D-ZSAFE | case.nc, gates, measured |
| [`RT14a-two-tool-lines`](cases/RT14a-two-tool-lines/) | Two tools: a second TOOL line in the header | D-SINGLETOOL | case.gi |
| [`RT14b-tool-change-mid-program`](cases/RT14b-tool-change-mid-program/) | Two tools: a T2 M6 tool change after the first operation | D-SINGLETOOL | case.gi |
| [`RT15a-measured-panel-40mm-narrower`](cases/RT15a-measured-panel-40mm-narrower/) | Measured panel 40 mm narrower than declared | P-GEOM | case.gi, gates, measured |
| [`RT15b-tower-distance-mismatch`](cases/RT15b-tower-distance-mismatch/) | Tower distance (INFO;27) does not match the panel edge | P-GEOM | case.gi, gates, measured |
| [`RT15c-vertices-fit-two-hypotheses`](cases/RT15c-vertices-fit-two-hypotheses/) | Vertices plausible under two decoding hypotheses | P-GEOM | case.gi, gates, measured |
| [`RT19-offcut-beyond-trim-touches-boundary`](cases/RT19-offcut-beyond-trim-touches-boundary/) | An offcut beyond a trim cut that touches the panel boundary, with a wheel on it | M-ATTACH | case.gi, gates, measured |
| [`RT20-part-on-3mm-tabs-with-wheel-on-it`](cases/RT20-part-on-3mm-tabs-with-wheel-on-it/) | A part held by a 3 mm tab on 18 mm stock, with wheels on it | M-ATTACH | case.gi, gates, measured |
| [`RT20-fact-tabs-hold-with-r-tabs`](cases/RT20-fact-tabs-hold-with-r-tabs/) | The same tab-held part with the R-TABS fact (t_hold = 2.5 mm): the tab holds | no M-ATTACH (lifted) | case.gi, gates, measured |
| [`RT21-half-a-wheel-disc-past-the-edge`](cases/RT21-half-a-wheel-disc-past-the-edge/) | Half a wheel disc past the edge, its centre inside the panel | W-AREA | case.gi, gates, measured |
| [`RT22-combined-line-envelope-leaves-panel`](cases/RT22-combined-line-envelope-leaves-panel/) | A combined rotate-and-translate line whose envelope (either direction) leaves the panel | W-RAPID | case.gi, gates, measured |
| [`RT23-arc-while-arcs-emit-closed`](cases/RT23-arc-while-arcs-emit-closed/) | An arc while arcs_emit is closed (Goliath dialect, verify mode) | G-GATE | case.nc, gates, measured |
| [`RT24-wheel-crosses-0.3mm-engraving`](cases/RT24-wheel-crosses-0.3mm-engraving/) | A wheel crossing a 0.3 mm engraving | W-SURF | case.gi, gates, measured |
| [`RT24-fact-engraving-ignored-with-r-kerf`](cases/RT24-fact-engraving-ignored-with-r-kerf/) | The same crossing with an R-KERF fact, h_r = 0.5 mm: the engraving is ignored | no W-SURF (lifted) | case.gi, gates, measured |
| [`RT25-mirror-y-frame`](cases/RT25-mirror-y-frame/) | The measured panel decodes uniquely under mirror-y (raw y from 0 to -H) | P-GEOM | case.gi, gates, measured |
| [`RT26-square-panel-identity-and-swap`](cases/RT26-square-panel-identity-and-swap/) | Square 1220 x 1220 panel: identity and swap both fit | P-GEOM | case.gi, gates, measured |
| [`RT27-header-disagrees-with-job`](cases/RT27-header-disagrees-with-job/) | GI TOWER2 axis=X and STOCK t=18 against job.toml tower2 Y and T = 17.6 | D-GI-CONFLICT (both lines) | case.gi |
| [`RT28a-clamp-under-a-wheel-path`](cases/RT28a-clamp-under-a-wheel-path/) | A declared clamp under a wheel path | W-OBST | case.gi, gates, measured, machine |
| [`RT28b-screw-on-the-toolpath`](cases/RT28b-screw-on-the-toolpath/) | A declared screw on the toolpath | T-OBST | case.gi, gates, measured |
| [`RT29-serpentine-frees-two-parts`](cases/RT29-serpentine-frees-two-parts/) | One serpentine through cut frees two parts; a wheel on the second between its detachment and the end of the cut | M-ATTACH | case.gi, gates, measured |
| [`RT30-arc-end-0.004mm-from-start`](cases/RT30-arc-end-0.004mm-from-start/) | G2 whose end is 0.004 mm from its start | D-ARC | case.gi |
| [`RT31a-motion-after-m30`](cases/RT31a-motion-after-m30/) | A move after M30 | D-AFTER-END | case.gi |
| [`RT31b-tool-change-marker-in-gi`](cases/RT31b-tool-change-marker-in-gi/) | A ;U tool-change marker in a GI file | D-MARKER | case.gi |
| [`RT32-far-end-of-a-3500mm-panel-wires-unmeasured`](cases/RT32-far-end-of-a-3500mm-panel-wires-unmeasured/) | Work at the far end of a 3500 x 1000 mm panel with the wire attachment unmeasured | P-REACH | case.gi, gates, measured |
| [`RT32b-far-corner-of-3050x1530`](cases/RT32b-far-corner-of-3050x1530/) | A cut in the far corner of a 3050 x 1530 mm panel (the design's case list) | W-AREA | case.gi, gates, measured |
| [`RT33-park-pose-100mm-from-tower-1-edge`](cases/RT33-park-pose-100mm-from-tower-1-edge/) | First line from a recorded park pose 100 mm from the tower-1 edge, before the heading map is a fact | W-FIRST | case.gi, gates, measured |
| [`RT34-no-start-pose`](cases/RT34-no-start-pose/) | A job with no start pose (no frames, no park-pose record) | W-FIRST | case.gi, gates |
| [`RT35-rotation-120mm-from-kerf-full-disc`](cases/RT35-rotation-120mm-from-kerf-full-disc/) | A link rotating at a pivot 120 mm from an earlier kerf, before the rotation fact | W-ROT | case.gi, gates, measured |

## Where a case departs from the design's wording

The normative documents decide. Where the design's one-line case description
cannot be built as written, or contradicts docs/prep, the case follows the
documents, says so in its rationale, and a test pins the reading:

* **RT4** (perimeter trim 10 mm from the edges): at 10 mm the tool leaves the
  desk tool area and the wheels cannot follow (T-AREA, W-AREA), so the literal
  case is RT04b; the structural hazard (the trim frees the interior the robot
  stands on) is RT04, with the trim completed across a bridge.
* **RT5, RT19 and the bridges of RT3, RT4, RT20, RT29**: the tool never reaches
  a panel edge (the desk tool area keeps it 12 mm in plus its radius), so an
  edge-to-edge separation, or a trim that touches the boundary, is completed by
  declared prior removal (earlier saw cuts) that this program joins. While
  cutting a straight line the two side supports can never both stand clear of
  it on one side (their offsets from the line sum to at most 102 mm against the
  116.5 mm two clear wheels need), and a wheel that stays on a part moves with
  the tool, so the part-closing cuts are short bridges.
* **RT6b** and **RT23**, **RT13**: rapid paths, spindle words and arcs in the
  upload are written by goliath-prep itself for GI input (safety-model 9.1, 3.5),
  so these cases are vendor-dialect programs verified as written.
* **RT7, RT8, RT28a** run on a profile with a measured 40 mm tower base and
  body box: with the default 120 mm base and 60 mm hull margin, B-TOWER and
  B-OBST would fail before the wheel check the case is about.
* **RT15** "25 mm narrower" equals `tol_panel` and passes "at most tol_panel":
  the case uses 40 mm. "Vertices plausible under two encodings" cannot be built
  for a real panel with the documented encodings; the case uses two axis maps
  on a near-square panel.
* **RT24-fact** uses `h_r` = 0.5 mm: safety-model 7.5 ignores recesses
  "shallower than h_r", so the design's `h_r >= 0.3` would not lift a 0.3 mm
  engraving at exactly 0.3.
* **RT32**: safety-model 6.4 shows that on a 3050 x 1530 mm panel P-REACH
  refuses nothing W-AREA has not refused; RT32 uses the document's own
  3500 x 1000 mm example and RT32b pins the 3050 x 1530 reading (W-AREA).

## Adding a case

Write the folder by hand, from the documents. Run the suite: the case tests
must show the files well formed, a reader case its one defect, and a geometric
case its claim under the derivation (add a co-occurring check only when the
derivation shows it fails by construction, and say why in the rationale).
