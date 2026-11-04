<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# G-code from any CAM program: the settings goliath-prep needs

> **Not released yet.** `goliath-prep`, which reads your CAM program's G-code,
> checks it and writes the program the robot runs, is being written. The rules
> this guide follows are fixed already: they are the G-code rules of the GI
> format, [`docs/prep/gi-format.md`](../prep/gi-format.md). Every setting below
> names the section of that document that defines it, so you can check it there.

**Who this is for:** you design parts in a CAM program that has no Goliath
post-processor of its own (Kiri:Moto, Vectric VCarve, Aspire or Cut2D, Estlcam,
Carbide Create or any other program that writes grbl-style G-code), and you
want `goliath-prep` to accept what it writes.

**You need:** the CAM program; a plain text editor to look at the exported file
(Notepad on Windows, any text editor on Linux); the panel's width and height
and its thickness measured with callipers; the bit's diameter and flute length.

## 1. How goliath-prep reads a file from another CAM program

- A file without any `;GOLIATH` comment is a **plain G-code file**. It says
  nothing about the panel, tower 2 or the tool, so `goliath-prep` takes all of
  that from the job's **job file** (section 3).
- This guide holds plain files to the G-code rules GI sets for its files
  ([gi-format.md section 3](../prep/gi-format.md#3-file-and-lines) and
  [section 8](../prep/gi-format.md#8-the-g-code-body)). What those rules refuse
  in a GI file, expect `goliath-prep` to refuse in a plain file. Nothing is
  read "roughly": a word outside the rules stops the job with a finding that
  names the line (section 6).
- `goliath-prep` finds the operations itself, from the Z heights (a cut starts
  where the bit goes below the stock top), and says so in its report: notice
  `N-INFERRED-OPS` ([gi-format.md section 7](../prep/gi-format.md#7-operations)).
- Do not add `;GOLIATH` lines by hand. A file with any of them is a GI file,
  which must start with `;GOLIATH-GI 1` and carry a full header
  ([gi-format.md section 5](../prep/gi-format.md#5-the-version-line-and-versioning));
  otherwise it is refused at line 1.
- The robot's own programs (`.NC` files from the vendor's software) are not
  CAM output: `goliath-prep` only verifies them, and this guide does not apply
  to them.

For FreeCAD and Fusion, the Goliath post-processors being written in this
repository write GI, which carries the stock, the tool and the operations and is
cross-checked against the job file. Their ordinary grbl output is a plain file,
and this guide applies to it.

## 2. The settings

"Otherwise" says what `goliath-prep` does with a file that breaks the setting.
The codes in brackets are the findings it reports (section 6).

| Setting | Set the CAM to | Otherwise | Defined in |
|---|---|---|---|
| Units | Millimetres. The file contains `G21`. | `G20` (inches) is refused (`D-UNITS`). | [8.4](../prep/gi-format.md#84-accepted-words), [8.5](../prep/gi-format.md#85-refused-words) |
| Coordinates | Absolute (`G90`). | `G91` (incremental) is refused (`D-UNSUPPORTED`). | [8.5](../prep/gi-format.md#85-refused-words) |
| X and Y zero | X 0, Y 0 at the stock corner where tower 1 stands, with the stock at positive X and Y: in the CAM's top view, the stock's lower-left corner (smallest X and Y). No offset. | Read as written. Nothing in the file tells `goliath-prep` where your zero was, so check it yourself (section 4). | [STOCK `origin`](../prep/gi-format.md#stock), [9](../prep/gi-format.md#9-the-frame) |
| Z zero | The top of the stock, so cuts go to negative Z. | Read as written, in the frame the job file names. A file zeroed at the stock bottom with a job file that says top cuts little or nothing: every cut lies T mm too high. The other way round, a file zeroed at the top with a job file that says bottom cuts every cut T mm too deep. Keep both at the top. | [STOCK `zzero`](../prep/gi-format.md#stock), [9](../prep/gi-format.md#9-the-frame) |
| Arcs | The XY plane (`G17`). Arcs as `G2`/`G3` with `I` and `J`, measured from the arc's start; or arcs written as short straight lines. | Arcs with `R` or `K`, and `I` or `J` on a straight move, are refused (`D-ARC`), and so is an arc whose end is less than 0.01 mm from its start without being equal to it. `G90.1` (absolute centres), `G18` and `G19` (other planes) are refused (`D-UNSUPPORTED`). An arc whose end equals its start is a full circle. | [8.7](../prep/gi-format.md#87-arcs-phys-16), [8.5](../prep/gi-format.md#85-refused-words) |
| Drilling | Holes as plain moves (`G0` above the hole, `G1` down and up): no canned cycles. | `G80` to `G89` are refused (`D-UNSUPPORTED`). | [8.5](../prep/gi-format.md#85-refused-words) |
| Cutter compensation | Off: the CAM offsets the path by the bit radius itself. | `G41` and `G42` are refused (`D-UNSUPPORTED`). | [8.5](../prep/gi-format.md#85-refused-words) |
| Tools | One tool per file. At most one `T` word, with or without `M6`, before the first move. | A second tool, or a `T` or `M6` after the first move, is refused (`D-SINGLETOOL`). | [8.9](../prep/gi-format.md#89-one-tool) |
| Spindle | `M3` with an `S` speed to start it, `M5` to stop it. | `M4` (reverse) is refused (`D-UNSUPPORTED`). | [8.4](../prep/gi-format.md#84-accepted-words), [8.5](../prep/gi-format.md#85-refused-words) |
| Pauses | None: no program stop, no dwell. | `M0` and `M1` are refused (`D-UNSUPPORTED`). `G4` is refused until the robot has passed bring-up rung R4 (`G-GATE`), and dropped with a disclosure after that. | [8.5](../prep/gi-format.md#85-refused-words) |
| Homing and offsets | None. `G54`, the first work offset, is accepted. | `G28`, `G30`, `G53`, `G92` and `G55` to `G59` are refused (`D-UNSUPPORTED`). | [8.4](../prep/gi-format.md#84-accepted-words), [8.5](../prep/gi-format.md#85-refused-words) |
| Tool length offset | Not needed, as far as is known: by our reading of the protocol specification the robot sets its Z zero itself in its drill phases, not yet verified on a robot. | `G43` (with its `H`) is accepted and ignored, with a disclosure that says so. | [8.4](../prep/gi-format.md#84-accepted-words) |
| Comments | Off, if the CAM lets you. Kept comments start with `;`, and their text after the `;` and any spaces starts with neither U, S nor the word GOLIATH. | Comments in parentheses are refused (`D-SYNTAX`). A `;` comment whose first character after the `;` and any spaces or tabs is U or S, in either case, is refused as an in-stream marker (`D-MARKER`). A comment whose text starts with GOLIATH, in any letter case, makes the file a GI file, refused unless it starts with `;GOLIATH-GI 1` (`D-GI-VERSION`). | [3.3](../prep/gi-format.md#33-comments), [3.4](../prep/gi-format.md#34-the-in-stream-markers-u-and-s-phys-17), [5](../prep/gi-format.md#5-the-version-line-and-versioning) |
| Start and end | No `%` lines. `M30` (or `M2`) is optional; if written, it stands alone and is the last word in the file. | A `%` line is refused (`D-SYNTAX`). Anything but blank lines and plain comments after `M30` or `M2`, or `M30` sharing its line, is refused (`D-AFTER-END`). | [8.1](../prep/gi-format.md#81-words), [8.8](../prep/gi-format.md#88-program-end-phys-17) |
| Numbers | A decimal point, never a comma; no exponent; a digit on each side of the point. Three decimals for X, Y, Z, I and J are best. | `1,5`, `.5`, `5.` and `1e3` are refused (`D-SYNTAX`). A coordinate beyond 10 000 mm either way, or a negative feed or speed, is refused (`D-RANGE`). | [8.2](../prep/gi-format.md#82-numbers) |
| Characters | Plain ASCII outside comments. | Any other character there is refused (`D-SYNTAX`); a typical cause is a typographic dash in place of a minus sign. | [3.1](../prep/gi-format.md#31-encoding) |
| Axes | X, Y and Z only. | `A` and `B` (rotary axes) are refused (`D-UNSUPPORTED`), and so is `C`: `goliath-prep` chooses the robot's heading itself. | [8.5](../prep/gi-format.md#85-refused-words) |
| Coolant, line numbers | Not needed. | `M7`, `M8`, `M9` and `N` words are dropped, with a note in the report. | [8.4](../prep/gi-format.md#84-accepted-words) |
| Feeds | `F` in millimetres per minute (`G94`). | Any other feed mode is a G number outside the accepted set, refused (`D-UNSUPPORTED`). | [8.4](../prep/gi-format.md#84-accepted-words), [8.5](../prep/gi-format.md#85-refused-words) |

## 3. The job file

Next to the G-code file, every job has a job file (`job.toml`). For a plain file
it is the only place `goliath-prep` learns these values from, so they must
describe the panel on the floor and the bit in the router, and agree with what
you set in the CAM:

| Value | What it is | Must match in the CAM |
|---|---|---|
| panel width W, height H | millimetres, along X and along Y | the stock's X and Y size |
| panel thickness T | millimetres, measured with callipers at the panel | the stock's height |
| tower-2 axis | `Y`: tower 2 stands at X 0, Y = H. `X`: tower 2 stands at X = W, Y 0 | nothing in the CAM; it says where you will put tower 2 |
| Z zero | the top of the panel | the CAM's Z zero |
| placement | none: the file's X 0, Y 0 is the tower-1 corner | the CAM's X and Y zero |
| bit diameter, flute length | millimetres | the CAM's tool |
| stick-out, bit kind, spindle speed | optional | the CAM's tool, when you state them |

These are the values a GI header would carry and compare
([gi-format.md section 6](../prep/gi-format.md#6-the-header) and
[section 10](../prep/gi-format.md#10-the-header-against-jobtoml-phys-12)). With a
plain file nothing compares them for you: the stock and tool in the CAM and the
job file must describe the same panel and the same bit, and you are the one who
checks that.

The job file's exact layout (its section and key names, and what else it can
hold, for example obstacles on the panel) is defined by `goliath-prep`'s job
file description, which is not published yet. Once it is, section 8 shows the
sample's complete job file.

## 4. Check the exported file

Open the exported file in a text editor and check, before you take it to
`goliath-prep`:

1. `G21` near the top, and no `G20` anywhere.
2. `G90` near the top, and no `G91`.
3. No `(`, no line that is only `%`, and no `;` comment whose text (after any
   spaces) starts with U, S or GOLIATH, in any letter case. Simplest: no
   comments at all.
4. Every `G2` and `G3` line has `I` or `J` (or both), and none has `R`.
5. None of `G41`, `G42`, `G28`, `G30`, `G53`, `G92`, `G55` to `G59`, `G80` to
   `G89`, `G4`, `M0`, `M1`, `M4`.
6. At most one `T` word, before the first move.
7. The first move raises Z (for example `G0 Z5.000`) before any X or Y move,
   as GI asks of its writers
   ([gi-format.md section 14](../prep/gi-format.md#14-writing-a-post-informative), item 3).
8. Cutting moves have negative Z, and moves between cuts positive Z. Only a
   cut meant to go through the panel goes below minus the panel thickness, and
   then only a little, into the board underneath: the GI example in
   [gi-format.md section 2](../prep/gi-format.md#2-an-example) cuts to Z -18.5
   in an 18 mm panel. `goliath-prep` limits that overcut.
9. Every X lies between 0 and W, every Y between 0 and H. While cutting, the
   whole bit stays on the panel: its centre at least half the bit diameter
   inside those limits. `goliath-prep` adds a margin of its own, and until
   the robot's bring-up is done it keeps the bit much farther from the edges
   than this (section 7).

The sample in section 8 passes all nine. Once `goliath-prep` is released, its
planned command `goliath-prep check` does all of this and much more, naming the
line of every problem; this list lets you catch the common ones in the CAM,
where they are easy to fix.

## 5. Program by program

### Kiri:Moto

Use the machine profile in [`posts/kirimoto/`](../../posts/kirimoto/README.md).
Kiri:Moto cannot write a GI header (its README explains why), so its files are
plain files like those of the programs below. Its README also lists the
settings of each Kiri:Moto job.

### Vectric (VCarve, Aspire, Cut2D), Estlcam, Carbide Create and others

There is no Goliath post-processor for these programs, and none is needed: a
plain millimetre file does. This guide names no menus or dialog boxes for them,
because we could not check them in each program and version. Use each
program's own help to find the settings of section 2. Look for them in three
places: the job or material setup (units, stock size, the X and Y zero position
and the Z zero), the choice of post-processor or machine (pick output for grbl
in millimetres, if offered), and the toolpath or output options (arcs,
comments).

grbl itself runs no canned cycles and no cutter compensation, so output meant
for grbl does not use them. What such output may still contain, and
`goliath-prep` refuses: comments in parentheses, a `%` line at the start or the
end, arcs with `R`, a move to a home or machine position at the end (`G28`,
`G30` or `G53`), and inch units. Check the file (section 4). Comment lines and
`%` lines can be deleted in a text editor. Change everything else in the CAM
and export again; never edit the moves by hand.

## 6. When goliath-prep refuses a file

Every refusal names the line of the file (and, for `D-SYNTAX`, the column).

| Finding | What `goliath-prep` found | What to change |
|---|---|---|
| `D-UNITS` | inch units (`G20`) | set the CAM, or its output, to millimetres and export again |
| `D-UNSUPPORTED` | a word outside the accepted set: incremental coordinates, absolute arc centres, another plane, cutter compensation, homing or offsets, canned cycles, a program stop, a reversed spindle, a rotary axis or `C` | find the word in the table of section 2, change that setting and export again |
| `D-ARC` | an arc with `R` or `K`; `I` or `J` on a straight move; a centre that does not fit both ends within 0.005 mm; an end less than 0.01 mm from the start without being equal to it | arcs with `I` and `J`, or arcs as lines; if it persists, a finer arc tolerance in the CAM, or arcs as lines |
| `D-SYNTAX` | something outside the G-code grammar: parentheses, a `%` line, a comma or an exponent in a number, a lower-case letter, a character that is not ASCII | comments off; delete `%` lines and comment lines; numbers with a decimal point |
| `D-MARKER` | a `;` comment whose first character after any spaces is U or S | comments off, or delete that comment line |
| `D-GI-VERSION` | a comment starting with GOLIATH, in any letter case, in a file that does not start with `;GOLIATH-GI 1` | delete that comment |
| `D-GI-SYNTAX` | a comment starting with GOLIATH that is not a well-formed GI annotation | delete that comment |
| `D-SINGLETOOL` | a second tool, or a tool word after the first move | one file per tool, one job per file |
| `D-AFTER-END` | something after `M30` or `M2`, or `M30` sharing its line | end the program at `M30` |
| `D-RANGE` | a coordinate beyond 10 000 mm either way, or a negative feed or speed | check the units and the zero; speeds and feeds positive |
| `G-GATE` | a dwell (`G4`) before the robot has passed bring-up rung R4 | set dwell times to zero in the CAM |
| `N-INFERRED-OPS` | not a refusal: a notice that the operations were found from Z | nothing; the report lists the operations it found |

## 7. What no CAM setting changes

- **One tool per file.** A job with two bits is two files and two jobs.
- **Room around the parts.** The robot drives on the panel: its wheels must
  stay on it, with a margin, and away from the towers. `goliath-prep` refuses
  cuts that would need a wheel off the panel or too close to a tower, however
  well the CAM is set up. Leave room between your parts and the panel edges,
  most of all near the towers.
- **The safe zone, until the bring-up is done.** Until the robot has passed
  bring-up rung R3, which shows which way it faces for each heading and how it
  turns, `goliath-prep` cannot know where the wheels will be while the robot
  turns. So it keeps every position of the bit inside the *any-heading safe
  zone*: at least 519 mm from every panel edge, and more than 639 mm from each
  tower. The 519 mm are 404 mm within which, in the design's model, every
  wheel stays while the robot turns, 55 mm for a wheel (its contact radius,
  40 mm until it is measured, plus a 15 mm margin), a 50 mm edge margin
  (10 mm after bring-up rung R9) and 10 mm for the panel's size. A panel
  needs more than 1038 mm on each side to have such a zone at all; a
  2440 × 1220 mm sheet leaves a strip of about 1402 × 182 mm in its middle. Earlier cuts count too: while the bit moves,
  no earlier cut may lie between 156 and 459 mm from it (where a wheel can be
  at some heading), and where the robot turns between two cuts, none within
  459 mm. `goliath-prep check` at the desk, without facts from the robot,
  applies the same zone (the project design, section 4.8). That is why the
  sample in section 8 sits in the middle of a full sheet, with its cuts close
  together.
- **Large closed outlines.** With the planner as designed today, `goliath-prep`
  refuses closed outlines larger than about 200 mm across, such as the outer
  profile of a cabinet side. The robot holds one heading for the whole of a
  cut, and going round a closed outline that is larger than the distances
  between the robot's wheels and its tool, one of the wheels crosses the
  outline after the bit has cut it. Open cuts and small closed shapes are not
  affected. This comes from the robot's wheel layout and the one-heading cut,
  not from a CAM setting, and measuring your robot does not change it. Ways to
  lift it are planned: splitting an outline into separately checked pieces,
  and a bring-up test showing that a wheel can cross a narrow groove safely.
- **Operations are inferred** from Z for a plain file, and the report says so
  (`N-INFERRED-OPS`).

## 8. A sample file

This file was written by hand for this guide, to show what a good plain file
looks like. No CAM program wrote it.

<!-- goliath-sample-job {"panel": {"W": 2440, "H": 1220, "T": 18, "tower2": "Y"}, "placement": {"dx": 0, "dy": 0}, "z_zero": "top", "tool": {"d": 6, "flute": 22}} -->
Its job: a panel 2440 × 1220 mm, 18 mm thick, with tower 2 on the Y axis (at
X 0, Y 1220); a flat bit of 6 mm diameter and 22 mm flute length; Z zero at the
top of the panel; no placement offset. The cuts sit in the middle of the
panel, inside the safe zone of section 7. The job file for it will be shown here
once `goliath-prep`'s job file layout is published (section 3).

<!-- goliath-sample {"id": "small-part", "expect": "ok"} -->
```gcode
G21
G90
G17
G0 Z5.000
G0 X1200.000 Y600.000
M3 S18000
G1 Z-3.000 F300
G1 X1260.000 F1200
G1 Y640.000
G1 X1200.000
G1 Y600.000
G0 Z5.000
G0 X1240.000 Y620.000
G1 Z-2.000 F300
G2 X1240.000 Y620.000 I-10.000 J0.000 F1200
G0 Z5.000
M5
M30
```

Line by line:

- Lines 1 to 3: millimetres, absolute coordinates, the XY plane.
- Line 4: the first move raises the bit to 5 mm above the panel, before any X
  or Y move.
- Line 5: travel, above the panel, to the start of the first groove.
- Line 6: spindle on, clockwise, 18 000 rpm.
- Line 7: plunge 3 mm into the panel, as a feed (`G1`), at 300 mm/min.
- Lines 8 to 11: a 60 × 40 mm rectangle, 3 mm deep, at 1200 mm/min. From the
  plunge on line 7 to the climb on line 12, this is one cut.
- Lines 12 to 14: up to 5 mm, over to the start of the circle, plunge 2 mm.
- Line 15: `G2` with its end equal to its start. That is a full circle, never an
  empty arc ([gi-format.md section 8.7](../prep/gi-format.md#87-arcs-phys-16)):
  20 mm across, around X 1230, Y 620, because `I-10.000` puts the centre
  10 mm to the left of the start.
- Lines 16 to 18: up, spindle off, end of the program.
