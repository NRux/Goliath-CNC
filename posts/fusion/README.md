<!-- SPDX-License-Identifier: Apache-2.0 -->
# Goliath post-processor for Autodesk Fusion

[`goliath.cps`](goliath.cps) is a post-processor for Fusion's CAM (the
Manufacture workspace). It turns a Fusion setup into **GI**, the
Goliath-intermediate G-code defined in
[`docs/prep/gi-format.md`](../../docs/prep/gi-format.md). GI is not what the
robot runs: `goliath-prep` reads it, checks the job and writes the robot's
program. The post is thin on purpose and holds no safety logic.

> **Status: this post has not been run in Fusion yet.** It is written from
> Autodesk's public post-processor documentation and tested here with a
> stand-in for Fusion's post engine (section 8). Fusion itself is the real test.
> Please run the [first-run checklist](#first-run-checklist) at the end of this
> page and send back what it asks for. If the post refuses something it should
> accept, or writes something odd, that is exactly what the checklist is for.

The post is written from scratch; it is not derived from any Autodesk library
post. **Licence:** Apache-2.0, as recommended for `posts/` and pending the
owner's decision ([`LICENSING.md`](../../LICENSING.md)).

## 1. Install the post (once)

1. Save [`goliath.cps`](goliath.cps) on your PC. Check that Windows kept the
   name `goliath.cps` (not `goliath.cps.txt`).
2. In Fusion, switch to the **Manufacture** workspace. On the **Milling** tab,
   open **Manage > Post Library**.
3. In the Post Library, select **My posts > Local**, click **Import** and pick
   `goliath.cps`. It is listed as *Goliath CNC: GI for goliath-prep*.

Fusion keeps the Local posts in a folder on your PC, usually
`%APPDATA%\Autodesk\Fusion 360 CAM\Posts`; copying `goliath.cps` there does
the same as Import. You can also right-click **Linked > Link Folder** in the
Post Library to use a folder of your own. To update the post later, import the
new file the same way.

## 2. Set up a job (once per design)

| What | How | Why |
|---|---|---|
| Units | The design in **millimetres**. | GI is millimetres only. The post refuses inch output instead of converting it, because Fusion hands some values (the stock and the tools) in the unit they were made in. |
| Setup | Operation type **Milling**, one setup per panel. | One GI file is one panel. |
| WCS orientation | **Z up**, out of the top face of the panel; X along one panel edge and Y along the other. | The Goliath works from above, on the panel's own X and Y. |
| WCS origin | **Stock box point**: the corner of the stock box where **tower 1** stands (lowest X and Y), on the **top** face. | GI's origin is the tower-1 corner with Z = 0 at the stock top (`origin=t1-corner`, `zzero=top`). The post refuses any other origin and shows the stock box it saw. |
| Stock | **Fixed size box**, the size of the panel: width, depth, thickness as in the job's `job.toml`. | The STOCK line is the stock box; `goliath-prep` refuses a STOCK that differs from `job.toml`. |
| Tool | **One tool** for every operation you post together, defined in **millimetres**, with its flute length set, spindle **clockwise**, speed above 0. A flat, ball or bull-nose end mill gives GI a tool kind; other types are fine, then the TOOL line has no kind. | GI carries one tool and the Goliath has no tool changer. A job with two tools is two NC programs. |
| Compensation | **Compensation type: In computer** (Passes tab of 2D Contour and similar). | GI carries the tool-centre path; "In control", "Wear" and "Inverse wear" need the controller to offset the tool, which the Goliath does not do. |
| Holes | Drill operation with cycle **Drilling - rapid out** (or Deep drilling - full retract, Chip breaking - partial retract, Reaming - feed out, Counterboring - dwell and rapid out). For bigger holes use 2D Bore or Circular. | Fusion expands these cycles into plain moves for the post. Tapping, the boring cycles, bore milling, thread milling and probing are refused: they reverse, stop or orient the spindle, or need compensation in the control. Counterboring's dwell is written as `G4`, which `goliath-prep` refuses until bring-up rung R4 opens its dwell gate `drop_dwells`; use a cycle without dwell until then. |
| Tool orientation | Off in every operation (no tilted planes, no multi-axis). | GI has X, Y and Z only, in the setup's WCS. |

## 3. Post a job

1. Right-click the setup (or the operations) and choose **Create NC Program**,
   or use **Post Process**.
2. **Post**: click the folder button next to the field and choose the Goliath
   post under **Local**.
3. **Name or number** and **File name**: for example `outline`.
4. **Output folder**: a folder you will find again, for example
   `Documents\Goliath jobs`. Fusion writes `<Output folder>\<File name>.gi`.
   Tick **Open NC file in editor** if you want to look at it.
5. **Unit**: *Document unit* (with a millimetre design) or *Millimeters*.
6. Post properties: **Tower 2 axis** = **Y** if tower 2 stands at X = 0,
   Y = panel height, or **X** if it stands at X = panel width, Y = 0, the same
   as in `job.toml`. It has no default: left at *Not set*, the post refuses.
7. Click **Post**.

If Fusion shows an error, the message starting with `Goliath post:` says what
was found and what to change. The post then wrote **no program**. If a file of
the same name is left from an earlier run, it is old: delete it so that it is
not used by mistake.

## 4. Fusion for Personal Use

Forum reports (community reports, not checked by us) say that with a Fusion
for Personal Use licence Fusion writes rapid moves as feed moves: `G1` at a
feed rate, no `G0`. The post writes exactly what Fusion gives it. It does
**not** turn those `G1` moves back into `G0`, because that may breach the
Personal Use terms. Its own positioning move at the start of each operation
follows the same rule: it is a `G1` at the same feed whenever the operation's
moves arrive as feed moves. The post never looks at which moves were rapids.

`goliath-prep` does not need `G0`: it tells cuts from travel by their Z heights,
not by the G word ([gi-format.md 8.4](../../docs/prep/gi-format.md#84-accepted-words)).
Whether `goliath-prep` may later run that travel at rapid speed under Personal
Use is a licence-terms question for you as the owner (owner question Q14), not
decided here.

## 5. What the post writes

- The GI header: `SOURCE` (Fusion's version and this post's), `UNITS mm`,
  `STOCK` (the stock box, `origin=t1-corner zzero=top`), `TOWER2`, `TOOL`
  (number, diameter, flute length, and kind, rpm and name when every operation
  agrees), `ZSHIFT 0.000` (the post shifts nothing), a note with the stock box
  as Fusion gave it, then `G17 G21 G90`.
- One `;GOLIATH OP` ... `;GOLIATH OPEND` block per operation: `id=op1`,
  `op2`, ... in posting order, `kind` from the operation type (below),
  `through=auto` (`goliath-prep` decides from the depths), `label` = the
  operation's name.
- In each block: `M3 S<rpm>`, the move to the operation's start position (up
  first then across, or across first then down, never diagonally down), then
  Fusion's moves: `G0`, `G1`, `G2`/`G3` with `I`/`J`. Three decimals for X, Y,
  Z, I, J; whole mm/min for F, written on the first feed move of each operation
  and when it changes. Only the axes that change are written; a move too small
  to show at three decimals is not written.
- Arcs: XY plane only, at most 180 degrees. The post asks Fusion to turn every
  other arc (helices, spirals, arcs over 180 degrees, arcs in other planes,
  tiny arcs) into short straight moves within 0.01 mm.
- Drilling cycles expanded by Fusion into moves, with a `; Note:` line; dwells
  as `G4 P<seconds>`.
- Fusion's comments as `; Fusion: ...`, the post's notes as `; Note: ...`.
  Neither can be read as a GI annotation or a `;U`/`;S` marker.
- `M5`, `M30` at the end. The file is written only when the whole job has been
  read without a refusal.

| Fusion operation | GI `kind` |
|---|---|
| 2D Contour | `profile` |
| 2D Pocket | `pocket` |
| 2D Adaptive, Adaptive Clearing | `adaptive` |
| Drill | `drill` |
| Engrave, Trace | `engrave` |
| anything else (Face, Bore, Slot, 3D finishing, ...) | `other`, with a note |

## 6. What the post refuses

Each refusal stops the post with a message that names the operation and the
fix, and writes no program:

- output in **inches**, or a tool defined in **inches**;
- **Tower 2 axis** not set;
- **more than one tool** (or one tool number with two different tools), a
  counter-clockwise spindle, spindle speed 0, no flute length;
- a WCS origin that is not the **stock corner** where tower 1 stands, Z = 0 not
  at the **stock top**, no stock;
- an operation that starts below the stock top where the post would have to
  move the tool across the panel to get there (its own move to each
  operation's start never cuts): set the **clearance height** and retract
  height above the stock top;
- **multi-axis** operations, a tool orientation other than the setup's,
  operations of setups with another WCS or another stock, non-milling
  operations, optional operations, feed per revolution;
- **radius compensation** in the control (anything but *In computer* or *Off*);
- cycles other than those in section 2: **tapping**, the boring cycles, bore
  milling, thread milling, probing;
- arcs that should never reach the post (outside the **XY plane**, over 180
  degrees, helical, spiral, or so small that GI would misread them): Fusion is
  asked to linearize those, so this means something unexpected; please report
  it;
- Manual NC **pass through**, call program, **program stop**, tool change and
  other machine commands; 5-axis moves; an operation without moves; a feed below
  1 mm/min; coordinates beyond +/-10000 mm.

Coolant commands are not written (the Goliath has none); a note says so.

## 7. Names still to confirm in Fusion

The post reads Fusion's stock box from the global parameters `stock-lower-x`,
`stock-lower-y`, `stock-lower-z`, `stock-upper-x`, `stock-upper-y` and
`stock-upper-z`, Fusion's version from `generated-by`, the operation name from
`operation-comment` and the operation type from the operation's strategy.
These names come from Autodesk's Post Processor Training Guide; the post
assumes the stock box is given in the setup's WCS. The first run confirms them.
What a wrong name does:

- a stock name: the post stops with a message naming the parameter;
- `generated-by`: `SOURCE` says `cam="Autodesk Fusion (version not reported)"`;
- `operation-comment`: the `OP` lines have no `label`;
- a strategy name: the operation is written as `kind=other`, with a note.

The assumption that the stock box is in the setup's WCS is the one the origin
and stock-top refusals rest on, so the checklist below tests it with a
deliberately misplaced WCS (step 4).

## 8. Tests and samples

Fusion cannot run in the test jobs. The tests
([`tests/posts/test_fusion_post.py`](../../tests/posts/test_fusion_post.py))
run `goliath.cps` under Node.js in a stand-in for Fusion's post engine
([`tests/posts/fusion_harness.js`](../../tests/posts/fusion_harness.js)) that
implements only the documented functions the post uses. They post synthetic
jobs, compare the output line by line with the GI rules and fire every refusal.
Without Node.js these tests skip and say so.

Real Fusion output from the first-run checklist goes into
[`samples/`](samples/), unchanged. The tests then check each sample against
the GI rules and, as soon as `goliath-prep`'s GI reader exists, with
`goliath-prep` itself (`test_committed_samples_pass_goliath_prep_check`).
Until a sample is committed, those tests skip and say why.

## First-run checklist

About an hour. It produces the first real samples and confirms the names in
section 7.

**Prepare** (section 1 and 2): install the post. Make a test design in
millimetres: a panel 600 x 400 x 18 mm with a 200 x 100 mm rectangle with
10 mm corner radii on it, a 100 x 50 mm pocket 5 mm deep, and four holes of
6 mm. One setup: Fixed size box stock 600 x 400 x 18 mm, WCS origin at the
stock box corner on the top face, Z up. One 6 mm flat end mill defined in mm.

1. **outline**: a 2D Contour around the rectangle, through the panel,
   compensation *In computer*. Post it alone as `outline` with Tower 2 axis Y.
2. **pocket**: a 2D Pocket in the pocket outline, 5 mm deep. Post it alone as
   `pocket`.
3. **holes**: a Drill operation on the four holes with the same end mill,
   cycle *Drilling - rapid out*. Post it alone as `holes`.
4. **One refusal, on purpose**: in the setup, move the WCS origin to the
   **centre** of the stock box's top face, post `outline` again and copy the
   error message. It must start with
   `Goliath post: the WCS origin is not at the stock corner` and show the stock
   box as `X -300.000 to 300.000, Y -200.000 to 200.000`. If the post writes a
   file instead, stop: the post reads the stock box in another frame than the
   toolpath and must not be used; send that file with the rest. Afterwards put
   the WCS origin back on the corner.
5. **Parameters**: post the three operations together with Fusion's own
   *Dumper* post (`dump.cps`, in Fusion's post library) and keep the `.dmp` file
   it writes.

**Send back**: `outline.gi`, `pocket.gi`, `holes.gi`, the `.dmp` file, the
error message text, your Fusion version (Help > About), whether your licence
is Personal Use, and, if you can, the design (File > Export, `.f3d`). If any
step stopped with a `Goliath post:` error instead, send that message and the
`.dmp` file; that is just as useful.

What happens next: the `.gi` files are committed under `samples/`, checked by
the tests and by `goliath-prep`, and the post is fixed where the real output
differs from what is expected.
