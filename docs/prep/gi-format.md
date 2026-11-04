<!-- SPDX-License-Identifier: Apache-2.0 -->
# GI: the Goliath-intermediate G-code format, version 1

*Normative specification. GI is the contract between a CAM post-processor and
`goliath-prep`: what a post writes, and what a reader accepts, warns about or
refuses. It is licensed Apache-2.0, like the protocol specification in
[`docs/protocol/`](../protocol/README.md), so that anyone can write a post or a
reader against it ([`LICENSING.md`](../../LICENSING.md)). Machine-readable
companions: [`gi.schema.json`](gi.schema.json), the header model a reader
records, and [`gi-examples/`](gi-examples/manifest.json), the conformance
examples with their expected outcomes.*

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are used as in RFC 2119.

- **Refused** means the reader reports a finding of severity `refuse` (see
  [Findings](#12-findings)) naming the file, the line and, for lexical errors,
  the column. A refused file is never planned, emitted or uploaded.
- **Warned** means a finding of severity `warn`: shown in the report and on the
  screen, it does not stop the job.
- **With a note** means a normalisation note in the report.

Nothing a reader does to a GI file is silent. Rules that came out of the
project's design review carry the review finding's id, for example (PHYS-12).

## 1. Why GI exists

CAM programs are moving targets: FreeCAD cannot run in every test job, Fusion's
post engine is proprietary, and both APIs change. So the posts are thin on
purpose. They write ordinary millimetre, absolute, XY G-code plus a few
annotations in comments, and `goliath-prep` is the one place that knows the
Goliath: the robot heading `C`, the `M10`/`M05` spindle words, the rapid template,
Z safety, cleaning and every safety check
([spec 05](../protocol/05-gcode-dialect.md)).

GI is **not** the robot's dialect. A GI file never contains `C`, `M10`, `M11` or
the `;U`/`;S` markers. A Goliath-dialect program (a vendor `.NC` file, or
`goliath-prep`'s own output) is read in *dialect verify mode*, which checks it
and never re-emits it; that mode is not GI and is not specified here.

## 2. An example

```
;GOLIATH-GI 1
;GOLIATH SOURCE cam="FreeCAD 1.1.4" post="goliath_post.py 0.1.0"
;GOLIATH UNITS mm
;GOLIATH STOCK x=2440.000 y=1220.000 t=18.000 origin=t1-corner zzero=top
;GOLIATH TOWER2 axis=Y
;GOLIATH TOOL n=1 d=6.000 flute=22.000 stickout=40.000 kind=flat rpm=18000 name="6mm 2F upcut"
;GOLIATH ZSHIFT -18.000
;GOLIATH OP id=op3 kind=profile through=auto tool=1 label="Part A outline"
G0 X100.000 Y200.000
G0 Z5.000
M3 S18000
G1 Z-18.500 F200
G1 X300.000 F800
G2 X350.000 Y250.000 I0.000 J50.000
G0 Z10.000
;GOLIATH OPEND id=op3
```

- Line 1 says this is GI version 1. Lines 2 to 7 are the **header**: who wrote
  the file, the unit, the panel, where tower 2 stands, the one tool, and the Z
  shift the post has already applied.
- `OP` and `OPEND` bracket one CAM operation.
- The body is plain G-code in the panel frame: X and Y from the tower-1 corner,
  Z = 0 at the stock top. `G0 Z5.000` is 5 mm above the stock top as written;
  the reader never applies `ZSHIFT` again.

This file is [`gi-examples/valid/design-5-1-example.gi`](gi-examples/valid/design-5-1-example.gi).

## 3. File and lines

### 3.1 Encoding

- A GI file is UTF-8 text. Outside comments it MUST be ASCII; any other
  character there is refused (`D-SYNTAX`, naming line and column), because the
  robot receives ASCII only ([spec 05 §What the robot actually receives](../protocol/05-gcode-dialect.md#what-the-robot-actually-receives)).
  A typical cause is a typographic dash pasted in place of a minus sign.
- Inside comments, and so inside annotation values, any UTF-8 is allowed
  (labels, tool names). Reports escape it before display.
- A reader ignores one UTF-8 byte-order mark at the very start of the file, with
  a note; line 1 is then the text after it.

### 3.2 Lines

- Lines are split exactly like .NET `File.ReadAllLines`: at CRLF, CR or LF; a
  terminator at the very end does not start another line. Lines are numbered
  from 1. This is how `goliath-link` splits a program for upload, so a line
  number means the same line in a finding, the report and the robot's position
  frames.
- Trailing spaces and tabs are ignored. Blank lines may appear anywhere after
  line 1.

### 3.3 Comments

- A comment runs from the first `;` on a line to its end. Parenthesised comments
  are not GI: a `(` is refused (`D-SYNTAX`). Posts write `;` comments.
- The text of a comment (after the `;`) decides what it is:
  - an **annotation** if, ignoring leading spaces and tabs, it starts with
    `GOLIATH` in any letter case (section 4);
  - otherwise a **marker** if its first character that is not a space or a tab
    is `U`, `u`, `S` or `s` (section 3.4);
  - otherwise a **plain comment**, dropped with a note.

### 3.4 The in-stream markers `;U` and `;S` (PHYS-17)

In vendor programs, `;U|…` marks a tool change and `;S…` an "empty the dust
bin" stop ([spec 03 §G-code in-stream markers](../protocol/03-session-sequence.md#g-code-in-stream-markers)).
They are read only in dialect verify mode. In a GI file (never in that mode) a
marker is **refused** (`D-MARKER`) naming its line, wherever it stands on the
line. It is never dropped: dropping it would silently discard a tool change or a
stop the program asked for.

Recognition is deliberately broad, because it is not known how strictly the
vendor's software matched these markers: any comment whose first character that
is not a space or a tab is `U` or `S`, in either case, counts. So
`; Spindle warm-up` is refused too. Posts start their comments with another
character, for example `; Note: spindle warm-up`.

## 4. Annotations

### 4.1 Recognition and form

An annotation-like comment (section 3.3) MUST be a well-formed annotation line;
otherwise it is refused (`D-GI-SYNTAX`). This catches a mistyped annotation that
would otherwise become a plain comment and vanish. A well-formed annotation line:

- is a line of its own: `;` in column 1, with no G-code before it;
- starts with `;GOLIATH-GI` (the version line, section 5) or with `;GOLIATH`
  followed by a space or a tab, in upper case, with no space between `;` and
  `GOLIATH`;
- follows this grammar (`WSP` is a space or a tab):

```
annotation = ";GOLIATH" 1*WSP keyword *(1*WSP item) *WSP
keyword    = UPPER *(UPPER / DIGIT)
item       = key "=" value / value             ; a value without a key is positional
key        = LOWER *(LOWER / DIGIT / "_")
value      = token / quoted
token      = 1*(ALPHA / DIGIT / "_" / "." / "+" / "-")
quoted     = DQUOTE *(qchar / "\" DQUOTE / "\" "\") DQUOTE
qchar      = any character except DQUOTE, "\" and the controls U+0000-U+001F, U+007F
```

Keys may come in any order. A key given twice on one line (even with the same
value), an unterminated quoted value or any other backslash sequence is refused
(`D-GI-SYNTAX`).

### 4.2 Value types

| Type | Written as |
|---|---|
| number | a token in the number grammar of section 8.2, never quoted |
| length | a number greater than zero, in millimetres |
| integer | digits only, never quoted |
| enumeration | exactly one of the listed tokens, case-sensitive, never quoted |
| token | a token |
| text | a quoted value or a token; any UTF-8 inside the quotes |

A value of the wrong type or outside its domain, a missing required key and an
unexpected positional value are refused: `D-GI-HEADER` on header lines, `D-GI-OP`
on `OP` and `OPEND` lines (`D-UNITS` for a unit other than `mm`).

### 4.3 Unknown keywords and keys are warned, not refused

An annotation with an unknown keyword, or a known annotation with an unknown key,
is warned about (`N-GI-UNKNOWN-KEY`) and the unknown part is ignored; the rest of
the line is read. The line must still be well formed (4.1). Unknown *values* of
known keys are refused, not warned (section 5).

## 5. The version line and versioning

- Line 1 of a GI file MUST be the version line `;GOLIATH-GI 1` (trailing spaces
  and tabs allowed). The version is a whole number without leading zeros.
- A file that contains any annotation-like comment is a GI file and MUST start
  with the version line. If line 1 is anything else, the file is refused at
  line 1 (`D-GI-VERSION`). It is never read as generic G-code with its
  annotations ignored.
- An unknown version is refused at line 1 (`D-GI-VERSION`) and nothing else in
  the file is interpreted. A second version line is refused at its line.

How GI evolves:

- **Within version 1**, new keywords and keys may be added only if a reader that
  ignores them still reads the file correctly and safely. Such additions are
  informational by definition; a version 1 reader warns about them (4.3).
- **Anything a reader must understand** to cut safely needs a new version
  number, which every older reader refuses: a new unit, origin or Z zero, a new
  required key, a new value of an existing key, a changed meaning, a new G-code.

## 6. The header

The header is every annotation from line 2 up to the first `OP` line or the first
line with G-code, whichever comes first. That line **ends the header**. Blank
lines and plain comments may stand in the header.

- `SOURCE`, `UNITS`, `STOCK`, `TOWER2` and `TOOL` are required, once each.
  `ZSHIFT` is optional, at most once. Lines may come in any order.
- A missing required line is refused (`D-GI-HEADER`) at the line that ends the
  header, or at the last line of a file whose header never ends.
- A second `TOOL` line is refused as `D-SINGLETOOL` (section 8.9); any other
  repeated header line as `D-GI-HEADER` at the repeat, even with equal values.
- A header keyword after the header has ended is refused (`D-GI-HEADER`) at that
  line.

The tables below are the keys of each line. Values that a line omits are absent,
never defaulted. [`gi.schema.json`](gi.schema.json) is the same information as a
JSON model, with the GI line each value came from.

### `SOURCE`

`;GOLIATH SOURCE cam="FreeCAD 1.1.4" post="goliath_post.py 0.1.0"`. Provenance,
recorded in the report and the certificate.

| Key | Type | Required | Meaning | Compared with job.toml |
|---|---|---|---|---|
| `cam` | text | yes | CAM program and version | no |
| `post` | text | yes | post-processor, or device profile, and its version | no |

### `UNITS`

`;GOLIATH UNITS mm`.

| Key | Type | Required | Meaning | Compared with job.toml |
|---|---|---|---|---|
| *(positional)* | enumeration: `mm` | yes | the unit of every length in the file; any other value is refused (`D-UNITS`): set the CAM to millimetres | no |

### `STOCK`

`;GOLIATH STOCK x=2440.000 y=1220.000 t=18.000 origin=t1-corner zzero=top`. The
panel the program was written for.

| Key | Type | Required | Meaning | Compared with job.toml |
|---|---|---|---|---|
| `x` | length | yes | stock size along X | panel width W |
| `y` | length | yes | stock size along Y | panel height H |
| `t` | length | yes | stock thickness | panel thickness T (measured with callipers) |
| `origin` | enumeration: `t1-corner` | yes | X = 0, Y = 0 is the stock corner where tower 1 stands; the stock occupies [0, x] × [0, y] ([spec 08 §Coordinate frame and inputs](../protocol/08-head-angle-and-cut-order.md#coordinate-frame-and-inputs)) | the program's placement on the panel: agrees only with no offset |
| `zzero` | enumeration: `top`, `bottom` | yes | where Z = 0 is: the stock top (cuts are negative, [spec 05 §Semantics a replacement must match](../protocol/05-gcode-dialect.md#semantics-a-replacement-must-match)) or the stock bottom | the job's Z zero |

### `TOWER2`

`;GOLIATH TOWER2 axis=Y`.

| Key | Type | Required | Meaning | Compared with job.toml |
|---|---|---|---|---|
| `axis` | enumeration: `X`, `Y` | yes | the axis tower 2 stands on, seen from tower 1: `Y` puts it at the corner (0, H), `X` at (W, 0); these are `SET T2 POSITION;0` and `;1` ([spec 02 §Tower pairing & geometry](../protocol/02-message-reference.md#tower-pairing--geometry); [spec 08](../protocol/08-head-angle-and-cut-order.md#coordinate-frame-and-inputs)) | the job's tower-2 axis |

### `TOOL`

`;GOLIATH TOOL n=1 d=6.000 flute=22.000 stickout=40.000 kind=flat rpm=18000 name="6mm 2F upcut"`.
The one tool of the program.

| Key | Type | Required | Meaning | Compared with job.toml |
|---|---|---|---|---|
| `n` | integer | yes | tool number; `T` words and `OP tool=` must name it | no |
| `d` | length | yes | cutting diameter | tool diameter |
| `flute` | length | yes | flute (cutting) length | flute length |
| `stickout` | length | no | length from the collet to the tip | stick-out, when job.toml declares one |
| `kind` | enumeration: `flat`, `ball`, `bull`, `vbit`, `other` | no | tool shape | tool kind, when job.toml declares one |
| `rpm` | number greater than zero | no | spindle speed the CAM planned, revolutions per minute | spindle speed, when job.toml declares one |
| `name` | text | no | the CAM's name for the tool | no |

### `ZSHIFT`

`;GOLIATH ZSHIFT -18.000`.

| Key | Type | Required | Meaning | Compared with job.toml |
|---|---|---|---|---|
| *(positional)* | number | yes, on a `ZSHIFT` line | the shift in Z, in mm, that the post has **already applied** to every Z in the file | no |

`ZSHIFT` is normative as **"already applied by the post; informational"**
(PHYS-12). The reader **never applies** it, to Z values or to anything else: every
Z in the file is read as written, in the frame `zzero` names. The reader records
it (in the model as `applied: false`) and shows it in the report. It need not
equal `-t`: a FreeCAD job whose stock spans Z = 1.05 to 19.05 writes
`ZSHIFT -19.050` with `t=18.000`. The rule exists because applying a shift twice
moves every cut by the shift, into the spoilboard or into the air
([`zshift-not-applied.gi`](gi-examples/valid/zshift-not-applied.gi)).

## 7. Operations

An operation is a block from an `OP` line to the `OPEND` line with the same id.

### `OP`

`;GOLIATH OP id=op3 kind=profile through=auto tool=1 label="Part A outline"`, and
optionally `c=90`.

| Key | Type | Required | Meaning |
|---|---|---|---|
| `id` | token | yes | unique in the file |
| `kind` | enumeration: `profile`, `pocket`, `drill`, `engrave`, `adaptive`, `other` | yes | the CAM operation's kind |
| `through` | enumeration: `auto`, `1`, `0` | yes | whether the operation cuts through the stock: `auto` lets the geometry decide (deepest point at or below T − 0.05 mm); `1` and `0` are the post's claim, and a claim the geometry contradicts is a warning of a later stage, where the geometry wins |
| `tool` | integer | yes | the tool; MUST equal `TOOL n` |
| `label` | text | no | the CAM's name for the operation |
| `c` | integer | no | forced heading for the operation's cuts, in `C` units (below) |

### `OPEND`

`;GOLIATH OPEND id=op3`.

| Key | Type | Required | Meaning |
|---|---|---|---|
| `id` | token | yes | the id of the open `OP` |

Rules, all refused as `D-GI-OP` unless noted:

- Blocks do not nest. Each `OP` is closed by an `OPEND` with its id before the
  next `OP`; an `OPEND` with no open block, or with another id, is refused at
  the `OPEND`; a block never closed is refused at its `OP` line. Ids are unique.
- `OP` blocks are optional. Without any, later stages infer the operations from
  Z and disclose that they did (`N-INFERRED-OPS`); a device profile that can
  only write a header (Kiri:Moto) produces such files. With blocks, segmentation
  by Z stays authoritative and the blocks are checked against it by later stages.
- **In a file with at least one `OP` line**, motion outside every block is link
  motion in the air. A motion line outside every block is refused when its end
  Z is below the stock top, or when it moves in X or Y (an arc always does)
  while its start Z is unknown or below the stock top. Z is unknown until the
  program first writes it, and unknown is never taken as safe. The stock top is
  Z = 0 with `zzero=top` and Z = t with `zzero=bottom`. A pure Z move up from an
  unknown height (the usual first line) is allowed.
- `c` forces the heading of the operation's cuts, with the vendor's
  forced-angle semantics ([spec 08 §Candidate search and selection](../protocol/08-head-angle-and-cut-order.md#candidate-search-and-selection)):
  heading θ = (C + 90) mod 360 ([spec 08 §Emitting the C word](../protocol/08-head-angle-and-cut-order.md#emitting-the-c-word)),
  allowed values 0, 10, 20, …, 360, where 360 is the same heading as 0. Any
  other value is refused. A forced heading is the only candidate for those cuts;
  it is still fully checked, and the job is refused if it fails. It never
  relaxes a check.

## 8. The G-code body

### 8.1 Words

The code of a line (the part before any `;`) is a sequence of **words**: an
upper-case letter immediately followed by a number. Spaces and tabs between words
are optional. Anything else is refused (`D-SYNTAX`): a lower-case letter, a space
between a letter and its number, a `%` line, a `(`, any other character.

### 8.2 Numbers

```
number = ["+" / "-"] 1*DIGIT ["." 1*DIGIT]
```

- The decimal separator is a point; there is no thousands separator and no
  exponent; a point has a digit on each side. `.5`, `5.`, `1e3` and `1,5` are
  refused (`D-SYNTAX`). This is the number format of
  [spec 05 §Number formatting](../protocol/05-gcode-dialect.md#number-formatting),
  which forces the invariant culture.
- Writers SHOULD use the dialect's layout (spec 05): X, Y, Z, I and J with three
  decimals, F and S as integers, one space before each word, `G X Y Z I J F S`
  order. Readers accept any number of decimals; `goliath-prep`'s emitter writes
  three.
- Values are exact decimals. A reader compares them as written (`10.5` equals
  `10.500`), never as binary floating point.
- `G` and `M` numbers are digits only; leading zeros are allowed (`G00` is `G0`,
  `M03` is `M3`). A decimal `G` or `M` number (`G90.1`, `G38.2`) is refused
  (`D-UNSUPPORTED`). `T`, `H` and `N` take digits only.
- `X`, `Y`, `Z`, `I` and `J` beyond ±10 000 mm, and a negative `F` or `S`, are
  refused (`D-RANGE`).

### 8.3 One line

- Each letter appears at most once on a line, except `G`, which may appear
  several times with different numbers (`G17 G21 G90`); at most one motion code
  (`G0`, `G1`, `G2`, `G3`) and at most one `M` word per line, so the order of
  actions is never ambiguous. Otherwise `D-SYNTAX`.
- `M2` and `M30` stand alone on their line (an `N` word and a comment are
  allowed); with any other word the line is refused (`D-AFTER-END`).

### 8.4 Accepted words

| Word | Meaning | Reader |
|---|---|---|
| `G0`, `G1` | rapid, linear feed | accepted; cut or link is decided by Z, not by the G word (some CAM writes rapids as feeds) |
| `G2`, `G3` | clockwise, counter-clockwise arc in the XY plane | accepted under section 8.7 |
| `G17`, `G21`, `G90`, `G94`, `G54`, `G40` | XY plane, millimetres, absolute, feed per minute, first work offset, no compensation | accepted no-ops asserting the defaults |
| `G43`, with an optional `H` | tool length offset | accepted and ignored, with a disclosure: the robot sets its Z zero itself |
| `M3`, `M5` | spindle on (clockwise), spindle off | accepted; `M3` becomes `M10` on emission ([spec 05 §Codes emitted](../protocol/05-gcode-dialect.md#codes-emitted)) |
| `S` | spindle speed, rpm | accepted |
| `F` | feed, mm/min | accepted |
| `T`, with an optional `M6` | tool selection | section 8.9 |
| `M7`, `M8`, `M9` | coolant | dropped with a note |
| `M2`, `M30` | program end | section 8.8 |
| `N` | line number | dropped with a note |
| `X`, `Y`, `Z` | absolute position, mm, in the panel frame | modal |
| `I`, `J` | arc centre, incremental from the arc's start | arc lines only |

### 8.5 Refused words

| Word | Finding | Why |
|---|---|---|
| `G20`; a `UNITS` other than `mm` | `D-UNITS` | GI is millimetres: set the CAM to millimetres |
| `G91`, `G90.1` and every decimal G code, `G18`, `G19` | `D-UNSUPPORTED` | absolute coordinates, I/J centres, XY plane only |
| `G41`, `G42` | `D-UNSUPPORTED` | cutter compensation belongs in the CAM |
| `G28`, `G30`, `G53`, `G92`, `G55` to `G59` | `D-UNSUPPORTED` | no homing moves, machine coordinates or offsets: the origin is the tower-1 corner |
| `G80` to `G89` | `D-UNSUPPORTED` | canned cycles are expanded by the CAM or the post |
| `G4` (with its `P`) | `G-GATE` | refused while the gate `drop_dwells` is closed (it opens at bring-up rung R4, which shows the spindle's spin-up behaviour); with the gate open, dropped with a disclosure |
| `M0`, `M1` | `D-UNSUPPORTED` | nothing can resume a paused program from the host on firmware 01.02.00 (`GCODE CONTINUE` is not in its table: [spec 09](../protocol/09-firmware-01.02.00.md#vendor-app-commands-that-are-not-in-the-table)) |
| `M4` | `D-UNSUPPORTED` | the spindle turns one way |
| `M10`, `M11`, `C` | `D-UNSUPPORTED` | robot dialect: `goliath-prep` chooses the heading and writes the spindle words; force a heading with `OP c=` |
| `A`, `B` | `D-UNSUPPORTED` | no rotary axes |
| `K`, `R`; `I` or `J` on a `G0` or `G1` move | `D-ARC` | arcs lie in the XY plane with an I/J centre |
| `H` without `G43`, `P` without `G4`, every other G or M number and every other letter | `D-UNSUPPORTED` | outside the vocabulary |
| a second tool (section 8.9) | `D-SINGLETOOL` | one tool per program |

### 8.6 Modal state

- The motion mode (`G0` to `G3`) is modal. A line with axis words and no motion
  code moves in the current mode; with no mode set yet it is refused
  (`D-SYNTAX`).
- `X`, `Y` and `Z` are modal: an absent word keeps the previous value. Before a
  program first writes an axis, its value is **unknown**, not zero.
- `F` and `S` are modal. `I` and `J` are not: on an arc line an absent one is 0.

### 8.7 Arcs (PHYS-16)

- `G2` is clockwise and `G3` counter-clockwise, seen from +Z, in the XY plane.
  The centre is the start plus (I, J); at least one of `I` and `J` is required.
  An arc with neither, an arc from an unknown X or Y, and an arc of zero radius
  are refused (`D-ARC`).
- The distances from the centre to the start and to the end may differ by at
  most 0.005 mm; more is refused (`D-ARC`).
- The end point is the line's X and Y, each taken from the modal position when
  absent. It is compared with the start **exactly**, as decimals:
  - end equal to start at file precision: the arc is a **full circle** (360°),
    never an empty arc;
  - 0 < |end − start| < 0.01 mm: refused (`D-ARC`) as ambiguous, because it
    reads equally as a full circle or as a sliver;
  - |end − start| ≥ 0.01 mm: an ordinary arc; the direction decides the sweep.
- The sweep is the angle from the start to the end about the centre, measured
  with `atan2` from the centre: for `G3`, (a_end − a_start) mod 360°; for `G2`,
  (a_start − a_end) mod 360°; 360° for a full circle. The report shows it.
- Helical arcs (Z changes along the arc) are accepted; later stages linearise
  them within 0.02 mm with a note. Arcs over 180° and full circles are accepted;
  the emitter splits them ([spec 05](../protocol/05-gcode-dialect.md#semantics-a-replacement-must-match)
  explains that the robot finds the direction with a three-point test).

### 8.8 Program end (PHYS-17)

`M2` or `M30` ends the program. It is optional; it is dropped with a note, because
the robot is sent no `M30` (spec 05). After it, only blank lines and plain
comments may follow. Any later line with a word or an annotation is refused
(`D-AFTER-END`): motion, and everything else too, because a reader cannot know
what the CAM meant by it.

### 8.9 One tool

- The header declares exactly one `TOOL`. A `T` word is optional; if present it
  appears once, before the first motion, and names `TOOL n`. `M6` is optional,
  on the line of the `T` or after it, and before the first motion.
- Refused as `D-SINGLETOOL`: a second `TOOL` line, a `T` naming another number,
  a second `T`, a `T` or `M6` after the first motion (even naming the same tool),
  an `M6` with no `T`. A job with two tools is two GI files.

## 9. The frame

- X and Y are the panel frame of [spec 08 §Coordinate frame and inputs](../protocol/08-head-angle-and-cut-order.md#coordinate-frame-and-inputs):
  millimetres, origin at the tower-1 corner, panel in [0, W] × [0, H]. This is
  the frame the robot's own programs use.
- Z = 0 is where `zzero` says. With `bottom`, later stages shift Z by −T and
  disclose it; job.toml must say the same (section 10).
- `ZSHIFT` is informational and never applied (section 6, `ZSHIFT`).

## 10. The header against job.toml (PHYS-12)

`goliath-prep` reads both the GI header and the job's `job.toml`. Neither wins
silently: every value both declare must agree, and any disagreement is refused
(`D-GI-CONFLICT`) at the GI header line, **naming both** sources and both values,
for example:

> line 4: GI STOCK t = 18.000 mm disagrees with job.toml panel thickness
> T = 17.600 mm. Make the CAM stock and job.toml describe the same panel.

| GI value | job.toml value | Compared |
|---|---|---|
| `STOCK x`, `y`, `t` | panel width W, height H, thickness T | always |
| `STOCK origin` (`t1-corner`) | the program's placement on the panel | always: `t1-corner` agrees only with a placement offset of (0, 0) |
| `STOCK zzero` | Z zero | always |
| `TOWER2 axis` | tower-2 axis | always |
| `TOOL d`, `flute` | tool diameter, flute length | always |
| `TOOL stickout`, `rpm`, `kind` | stick-out, spindle speed, tool kind | when both declare one |

- Numbers agree when they differ by less than 0.0005 (mm, or rpm), so values
  equal at the three decimals a post writes agree, and anything else does not.
  Compare the decimals as written.
- Each disagreeing value is its own finding.
- A value only one side declares is used with that side as its source. The
  report and the certificate record the source of every bound value (for example
  "job.toml = GI TOOL d").
- How job.toml spells each value is defined by `goliath-prep`'s job file
  specification; the conformance examples state the job as a JSON object
  (section 13).

## 11. What a reader does (informative)

A reader reads the header into the model of [`gi.schema.json`](gi.schema.json),
warns about unknown keywords and keys, refuses what this document refuses,
compares the header with job.toml, records `ZSHIFT` without applying it, and
keeps a source reference (file, line, text) on everything it reads, so every
later finding points at a line. Everything Goliath-specific (headings, the
spindle words, the rapid template, Z safety, cleaning, the safety checks) happens
after reading, in `goliath-prep`.

A reader may stop at the first refusal or go on. It MUST report at least the
finding these rules name for the line they name. It MAY also report what follows
from it: a malformed `TOOL` line, for example, also leaves `OP tool=1` naming no
declared tool.

## 12. Findings

The ids belong to the check catalogue's families: `D-` (dialect and program),
`G-` (feature gates) and `N-` (notices). `D-SINGLETOOL` and `G-GATE` are the
catalogue's existing checks; the others are the reader's.

| Id | Severity | Raised when | Line reported |
|---|---|---|---|
| `D-GI-VERSION` | refuse | line 1 is not a version line in a file with annotations; the version is not 1; a second version line | 1, or the second version line |
| `D-GI-SYNTAX` | refuse | an annotation-like comment that is not a well-formed annotation line (4.1) | that line |
| `D-GI-HEADER` | refuse | a required header line or key missing; a header line repeated or after the header has ended; a header value of the wrong type or outside its domain | that line; for a missing line, the line that ends the header |
| `D-GI-OP` | refuse | `OP`/`OPEND` structure or values (section 7); motion outside every block below the stock top or with unknown Z | that line; for a block never closed, its `OP` line |
| `D-GI-CONFLICT` | refuse | a header value disagrees with job.toml (section 10) | the GI header line |
| `N-GI-UNKNOWN-KEY` | warn | an unknown annotation keyword or key, ignored | that line |
| `D-SYNTAX` | refuse | a lexical or line error (8.1 to 8.3, 8.6), non-ASCII outside a comment, a parenthesised comment, a `%` line | that line and the column |
| `D-UNSUPPORTED` | refuse | a word outside the vocabulary (8.5) | that line |
| `D-UNITS` | refuse | `G20`; a `UNITS` other than `mm` | that line |
| `D-RANGE` | refuse | a coordinate beyond ±10 000 mm; a negative `F` or `S` | that line |
| `D-ARC` | refuse | an arc that breaks section 8.7; `K`, `R`; `I` or `J` on a linear move | that line |
| `D-AFTER-END` | refuse | a word or annotation after `M2`/`M30`; `M2`/`M30` sharing its line | that line |
| `D-MARKER` | refuse | a `;U` or `;S` marker (3.4) | that line |
| `D-SINGLETOOL` | refuse | a second tool (8.9) | that line |
| `G-GATE` | refuse | `G4` while the gate `drop_dwells` is closed | that line |

Later stages of `goliath-prep` add their own findings (for example
`N-INFERRED-OPS` for a file without `OP` blocks); they are not reader findings.

## 13. Conformance examples

[`gi-examples/`](gi-examples/manifest.json) holds `valid/*.gi`, `invalid/*.gi`
and `manifest.json`, a list with one entry per example file
([`manifest.schema.json`](gi-examples/manifest.schema.json)):

| Field | Meaning |
|---|---|
| `file` | path relative to `gi-examples/`; `valid/` files expect `ok`, `invalid/` files `refused` |
| `expect` | `ok`: the reader reports no finding of severity `refuse`. `refused`: it reports at least one |
| `finding_id`, `line` | with `refused`: a refusing finding the reader MUST report, on that line. With `ok`: a warning it MUST report there |
| `description` | what the example shows |
| `job` | the job.toml values the header is compared with: `panel` (`W`, `H`, `T`, `tower2`), `placement` (`dx`, `dy`), `z_zero`, `tool` (`d`, `flute`, and optionally `stickout`, `rpm`, `kind`). Nothing is defaulted |
| `assert_moves` | moves the reader's raw program MUST contain: the move read from `line` ends at `end` (X, Y, Z as written, within 0.0005 mm; `ZSHIFT` is never applied) and, for an arc, sweeps `sweep_deg` (within 0.001°) |

- A reader's test suite runs every entry as GI, against its `job`, with every
  gate closed (the desk view). `goliath-prep`'s suite does so in
  `test_gi_conformance_examples`; the repository suite checks that the examples,
  the manifest, the schemas and this document agree.
- Each invalid example has exactly one defect; a reader may also report its
  consequences (section 11).
- The examples are byte-exact: `.gitattributes` keeps their CRLF and CR line
  endings, the byte-order mark and the missing final newline that some of them
  test. Each ends with a plain comment carrying its licence identifier.

## 14. Writing a post (informative)

A post that follows this list writes GI that reads cleanly:

1. Line 1 `;GOLIATH-GI 1`, then `SOURCE`, `UNITS mm`, `STOCK`, `TOWER2`, `TOOL`
   and, if the post shifted Z, `ZSHIFT`, before any G-code.
2. Millimetres, absolute, origin at the stock corner where tower 1 stands,
   Z = 0 at the stock top. Refuse in the post, with a clear message, a job origin
   anywhere else.
3. One `OP` … `OPEND` block per operation; air moves between blocks stay at or
   above the stock top, and the first move raises Z before moving in X or Y.
4. `G0`, `G1`, `G2`/`G3` with `I`/`J` in the XY plane; `M3 S…`, `M5`, `F`. Expand
   cycles, linearise or keep helices, turn compensation off.
5. Never `C`, `M10`, `M11`, `;U` or `;S` markers, parentheses, `%`, `G20`, `G91`,
   `G41`/`G42`, canned cycles, a second tool.
6. Comments start with `;` and with a character other than `U`, `S` or the word
   `GOLIATH` (for example `; Note: …`).
7. Three decimals for X, Y, Z, I, J; integers for F and S.
8. Write the same stock, tower axis and tool the job's job.toml states. `M30`, if
   written, is the last word in the file.

## 15. Changes

| Version | Date | Change |
|---|---|---|
| 1 | 2026-11-04 | First version. |
