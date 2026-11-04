<!-- SPDX-License-Identifier: Apache-2.0 -->
# Goliath post-processor for FreeCAD CAM

`goliath_post.py` turns a FreeCAD CAM job into **GI**, the Goliath-intermediate
G-code defined in [`docs/prep/gi-format.md`](../../docs/prep/gi-format.md).
GI is not what the robot runs: `goliath-prep` reads it, checks the job against
the safety model and writes the robot's program. The post is thin on purpose;
it holds no safety logic.

**Status.** Tested with stand-in objects that carry the attributes FreeCAD
1.1.4's public CAM sources define (see "Tests and samples" below). It has not
yet been run inside FreeCAD. The first run in FreeCAD is the real test: if the
post refuses something it should accept, or writes something unexpected,
please report it with the job file.

## Install

1. Copy **both** files, `goliath_post.py` and `gi_format.py`, side by side into
   FreeCAD's macro folder: the folder set as the macro path in FreeCAD's
   preferences (in FreeCAD's Python console, `FreeCAD.getUserMacroDir()` prints
   the default). FreeCAD's CAM workbench looks for post-processors there.
2. In the CAM job, on the output settings, choose the post-processor
   **goliath** and set its arguments to `--tower2=Y` or `--tower2=X` (below).
   If `goliath` is not offered, enable it in the post-processor list of the CAM
   preferences.

The post runs in FreeCAD's own Python; it needs Python 3.9 or newer and nothing
outside the standard library.

## Setting up the job

| What | How | Why |
|---|---|---|
| Stock | A **box** the size of the panel. | GI describes the panel as a box: STOCK `x`, `y`, `t` come from its bounding box. Any other stock shape is refused. |
| Origin | The stock's **minimum corner** at the document origin (X = 0, Y = 0), with the panel along +X and +Y. That corner is where **tower 1** stands. | GI's frame starts at the tower-1 corner (`origin=t1-corner`). A stock anywhere else is refused, with the corner's position in the message. Move the model and the stock together. |
| Z | Anywhere. | The post shifts every Z by minus the stock top (`ZSHIFT`), so Z = 0 is the stock top in the file. |
| Tower 2 | Post-processor argument `--tower2=Y` when tower 2 stands at X = 0, Y = panel height; `--tower2=X` when it stands at X = panel width, Y = 0. | There is no default: a missing or other value is refused. |
| Tool | **One tool per job**: every operation's tool controller uses the same tool number and the same bit. The bit needs a diameter and a cutting edge height (written as the flute length): describe it with a shape such as Endmill, not FreeCAD's Drill shape, which has no cutting edge height. Spindle direction **Forward**, a speed above 0, and feed rates above 0. | GI carries one tool. A job with two tools is two jobs, each written to its own file. |
| Fixtures | `G54` only. | The Goliath has no work offsets. |
| Compensation | On ("Use Compensation"), so FreeCAD offsets the path itself. | `G41`/`G42` are refused. |

The tower axis, stock and tool must also match the job's `job.toml` for
`goliath-prep`. The post never reads `job.toml`: it writes what the FreeCAD
job says, and `goliath-prep` refuses any disagreement, naming both values.

## What it writes

- The GI header: `SOURCE` (the FreeCAD version and this post's version),
  `UNITS mm`, `STOCK`, `TOWER2`, `TOOL`, `ZSHIFT`.
- `G17 G21 G90`, then `T<n> M6`.
- One `;GOLIATH OP` ... `;GOLIATH OPEND` block per active operation, in the
  order FreeCAD passes them: `id` is the operation's internal name, `label` its
  label, `kind` from its type (Profile, Pocket, Drilling, Engrave, V-carve,
  Adaptive; any other type is written as `other`, with a note), and `through=1`
  when the operation's final depth reaches the stock bottom (otherwise
  `through=auto`, so the geometry decides). Each block starts the spindle with
  its tool controller's speed (`M3 S...`), states its feed on its first feed
  move, and brackets coolant (`M7`/`M8` ... `M9`) when the operation asks for
  it; goliath-prep drops coolant words with a note.
- The operation's path: `G0`, `G1`, `G2`/`G3` with `I`/`J`, three decimals,
  feeds in whole mm/min (FreeCAD stores mm/s). FreeCAD's rapid rate on `G0`
  lines is not written: GI has no rapid speed.
- Canned drilling cycles (`G73`, `G81`, `G82`, `G83`, `G85`) expanded into
  moves by the RS274/NGC rules, each with a `; Note:` line saying so.
- FreeCAD's comments as `; FreeCAD: ...`, the post's own notes as
  `; Note: ...` (for example an inactive operation that was left out).
- `M5`, `M30` at the end.

## What it refuses

Every refusal names the operation, the position of the command in its path
and the reason, is printed to FreeCAD's report view, and writes no file:
a stock that is not a box or not at the tower-1 corner; no or a bad
`--tower2`; more than one tool; a bit without diameter or cutting edge height;
a spindle that turns counter-clockwise or does not start; a feed that comes out
below 1 mm/min; fixtures other than `G54`; cutter compensation; inch or
incremental mode; arcs outside the XY plane or without `I`/`J`; homing,
machine coordinates and offsets; pauses and program ends inside an operation;
tool changes inside an operation; rotary axes; a canned cycle without
`G98`/`G99` before it, or one it cannot expand exactly (tapping, boring with
spindle stop); an operation whose path has no moves; any command it does not
know.

## Tests and samples

FreeCAD cannot run in every test job, so the tests
([`tests/posts/test_freecad_post.py`](../../tests/posts/test_freecad_post.py))
drive the post with stand-in objects
([`tests/posts/freecad_fakes.py`](../../tests/posts/freecad_fakes.py)) and
compare its output with the samples in [`samples/`](samples/):

| Sample | Shows |
|---|---|
| [`profile-and-pocket.gi`](samples/profile-and-pocket.gi) | a pocket and a through profile with arcs, tower 2 on Y |
| [`drilling-cycles.gi`](samples/drilling-cycles.gi) | holes drilled with an endmill: `G81` (with `G98`) and `G83` pecks (with `G99`) expanded; stock from Z 1.05 to 19.05, so `ZSHIFT -19.050`; coolant; tower 2 on X |
| [`adaptive-helix-two-speeds.gi`](samples/adaptive-helix-two-speeds.gi) | a helical entry and a full circle; one tool at two spindle speeds, so `TOOL` carries no `rpm` |

The samples say in their `SOURCE` line that they come from stand-in objects,
not from a FreeCAD run. They are checked against the GI writing rules, and by
goliath-prep's reader as soon as it exists. After a deliberate change to the
post, rewrite them and review the difference:

```
python tests/posts/freecad_fakes.py --write-samples
```

## Licence

Apache-2.0, as recommended for `posts/` and pending the owner's decision
([`LICENSING.md`](../../LICENSING.md)). The post is written from scratch; it
copies no code from FreeCAD's own post-processors.
