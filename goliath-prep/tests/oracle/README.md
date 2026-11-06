<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# The independent oracle: a second checker for goliath-prep

[`prep_oracle.py`](prep_oracle.py) is a brute-force checker of the safety model
([`docs/prep/safety-model.md`](../../../docs/prep/safety-model.md)), written
apart from goliath-prep's own checker, as the project design asks (section
10.4). It samples the tool's path at most 1 mm apart and every wheel disc at its
centre and 16 rim points, uses spec 08's exact triangle test for the tower
zones, point-to-segment distances for removed material, and its own
connectivity on a 2 mm grid for attached and detached material. It is test
code only.

**The property it serves: anything the real checker accepts, the oracle
accepts.** The oracle tests the safety model's regions at sample points, so a
violation it reports lies inside a region the checker must keep clear. A job
the checker certifies and the oracle refuses is a defect to minimise into a
regression case ([`test_prep_oracle_cross.py`](test_prep_oracle_cross.py)).

## Independence (red-team C-06)

The oracle imports nothing of goliath-prep: it reads job.toml and machine.toml
with `tomllib` and [`params.json`](../../../docs/prep/params.json) with `json`,
derives R_rot, rho, r_min, r_max, R_body and the piece length itself, and reads
the normalised program by its attributes. It never uses P22's `build_context`
or a `CheckContext`. [`test_prep_oracle_arch.py`](test_prep_oracle_arch.py)
holds it to that. Its front end, [`prep_oracle_front.py`](prep_oracle_front.py),
turns a file into a program with the input stage only (the strict reader and
the normaliser), which checker and oracle share by design: the oracle's
interface takes a program.

## Interface

`oracle_check(program, job_toml, machine_toml, params_json, measured=None, *,
gates=None) -> OracleVerdict(ok, violations, notes, mode, headings, table)`.
`measured` holds the raw `INFO;25` lines and the `INFO;27` line of a panel
detection (job time), `gates` the `gates` object of a gate snapshot (None
closes every gate). Each violation is an `OracleHit` (check id, rule, where,
cut, wheel, point, sample time, heading, source line, depth in mm).

* **plan** mode (GI or generic G-code): the oracle searches the headings
  itself (the forced one, or all 36) and the links of goliath-prep's link
  template (a straight translation at Z_safety with the rotation at either end,
  or one combined line), and accepts when some choice passes everything.
* **verify** mode (Goliath dialect): every heading and link as written.

## Where it refuses less, and says so

Where it cannot be exact the oracle takes the reading that refuses less, and
names it in the verdict's notes; it never refuses more than the safety model:

* its grid shrinks holding removal by 2 mm, so it can only find less detached
  material than the exact model;
* a link passes when either pivot passes; an envelope is tested along one
  turning direction; after the rung R3 facts the first line is tested from the
  nominal park pose;
* a measured-panel hypothesis other than the identity counts only when it
  passes by a clear margin;
* support strips abut the work panel (no gap);
* under R-KERF with a kerf width above 0 it reports no wheel over removal;
* the checks it does not evaluate at all (D-FEED, D-WORDS, D-ROUNDTRIP, D-SIZE,
  F-GATE, S-SERIES, W-VENDORFLOOR, T-PASS, notices, and a few gates) are listed
  in every verdict.

A violation counts only beyond 0.05 mm (float noise and the emitter's 0.02 mm
arc tolerance).

## The tests

| File | What |
|---|---|
| `test_prep_oracle_arch.py` | the oracle imports neither the checker nor the context; it derives what the catalogue derives |
| `test_prep_oracle_redteam.py` | it refuses every red-team case for the case's reason, near the case's witness; relaxations lift only their check |
| `test_prep_oracle_model.py` | its parameters and their sources, the robot model, positive controls (jobs it must accept), structure, the job-time panel, the safe zone |
| `test_prep_oracle_properties.py` | monotonicity in material and refinement invariance, on the oracle now and on goliath-prep's pipeline once it exists |
| `test_prep_oracle_cross.py` | anything the pipeline certifies passes the oracle, as planned and as shipped (skipped until the pipeline exists) |

## Design questions this suite pins

* **Gates.** The design's interface has no gate snapshot, yet most checks
  depend on gates; the oracle takes one as a keyword and closes every gate
  without it.
* **Reader cases.** A file the reader refuses has no program; the oracle
  reports the reader's refusal, marked as the reader's.
* **T-ATTACH is not monotone in material.** Removing a loose part entirely
  lifts the T-ATTACH of a cut inside it (cutting air is safe), against the
  safety model's principle 4; the monotonicity property excludes T-ATTACH.
* **Finer arcs.** Linearising an arc moves the path by its sagitta, so
  refinement invariance holds beyond a band (1.5 mm for the oracle), not
  exactly.
