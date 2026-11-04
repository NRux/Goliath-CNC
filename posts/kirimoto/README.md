<!-- SPDX-License-Identifier: Apache-2.0 -->
# Kiri:Moto machine profile for the Goliath

A set of Kiri:Moto machine (device) settings with which Kiri:Moto's CAM mode
writes G-code that `goliath-prep` can read: a plain millimetre G-code file, with
the panel, the position of tower 2 and the tool stated in the job's job file.
[`goliath-device.json`](goliath-device.json) holds the settings in the layout of
Kiri:Moto's own machine definitions; section 3 lists the same values for typing
in, each with the reason for it.

For everything that is not specific to Kiri:Moto (the job file, how to check an
exported file, what each refusal means) see the guide for any CAM program,
[`docs/user-guide/cam-generic.md`](../../docs/user-guide/cam-generic.md).

> **Not released yet.** `goliath-prep` is being written. The rules this profile
> follows are fixed in the GI format specification,
> [`docs/prep/gi-format.md`](../../docs/prep/gi-format.md), whose G-code rules
> the generic guide applies to plain files too.

**Licence:** Apache-2.0, recommended for `posts/` and pending the owner's
decision (see [`LICENSING.md`](../../LICENSING.md)).

## 1. What the profile does

- It makes Kiri:Moto write millimetres (`G21`), absolute coordinates (`G90`)
  and the XY plane (`G17`) at the top of every file, and no comments at all.
- It keeps the lines that let `goliath-prep` see what it must refuse: a second
  tool and a dwell reach it as `M6 T…` and `G4` lines instead of being dropped
  without a trace.
- It writes no GI annotation (no `;GOLIATH` line). Section 2 says why.

## 2. Why the file has no GI header

GI, the format the Goliath post-processors for FreeCAD and Fusion (being
written) produce, carries the stock, the tower-2 axis, the tool and the
operations in `;GOLIATH` comment lines, and its first line must be
`;GOLIATH-GI 1`
([gi-format.md section 5](../../docs/prep/gi-format.md#5-the-version-line-and-versioning)).
Kiri:Moto offers header and footer macros, not post-processor scripting. We read
its CAM exporter (Kiri:Moto is open source) on 2026-11-04, in its development
version of that date. It cannot produce that first line:

- With **Strip Comments** off, the exporter first writes a comment block of its
  own (the Kiri:Moto version, the date, the bed, the stock size, the target,
  every process setting and the tools) and only then the Header macro. A
  version line in the Header macro lands several lines down, and `goliath-prep`
  refuses the file at line 1 (`D-GI-VERSION`). That block also contains a line
  `; Stock X:…`, and every operation starts with a comment `; starting … op`:
  comments whose first letter is an S, which `goliath-prep` refuses as in-stream
  markers (`D-MARKER`,
  [gi-format.md section 3.4](../../docs/prep/gi-format.md#34-the-in-stream-markers-u-and-s-phys-17)).
- With **Strip Comments** on, the exporter cuts every line of every macro at its
  first `;` and drops the lines left empty. GI annotations are comments, so a GI
  header in the Header macro disappears completely.

Kiri:Moto's documentation lists a directive that turns its comment block off
for 3D printing; the CAM exporter we read does not use it.

So this profile turns Strip Comments on and writes a plain file with no
annotation. `goliath-prep` reads it with the **job file** (`job.toml`) as the
only source for the panel, tower 2 and the tool, and finds the operations
itself from the Z heights, which its report discloses (`N-INFERRED-OPS`,
[gi-format.md section 7](../../docs/prep/gi-format.md#7-operations)). What a GI
header would add is the cross-check of the CAM's stock and tool against the job
file ([gi-format.md section 10](../../docs/prep/gi-format.md#10-the-header-against-jobtoml-phys-12)).
With Kiri:Moto you make that check yourself: the stock and tool in Kiri:Moto
and the job file must describe the same panel and the same bit.

If a later Kiri:Moto behaves differently, the check in section 5 shows it.

## 3. The settings

| Kiri:Moto setting | Key in `goliath-device.json` | Value | Why |
|---|---|---|---|
| machine mode | `mode` | `"CAM"` | Kiri:Moto's milling mode. |
| machine name | `deviceName` | `"Goliath for goliath-prep (plain mm G-code)"` | Any name works; this one says what the files are for. |
| Workspace X (width) | `bedWidth` | `3500` | Millimetres. The largest panel the protocol specification names ([key reference constants](../../docs/protocol/README.md#key-reference-constants-machine-1930--firmware-gfw-010200): 3500 × 3500 mm). It only lets Kiri:Moto lay out a whole panel; it does not mean the robot reaches every point of one. `goliath-prep` checks the real panel and the reach. |
| Workspace Y (depth) | `bedDepth` | `3500` | The same, for Y. |
| Workspace Z (height) | `maxHeight` | `58` | Millimetres: the router's Z travel (same table). |
| origin at the bed centre | `originCenter` | `false` | The origin is a corner, not the centre: X 0, Y 0 is the stock corner where tower 1 stands (section 4). |
| Max Spindle | `spindleMax` | `27000` | Revolutions per minute. Kiri:Moto writes the Spindle macro (the `M3` line) only when Max Spindle is set, and caps speeds at it. 27 000 rpm is the router's reported top speed ([prior-art review, section 4](../../docs/research/prior-art-review.md#4-what-the-goliath-actually-is)) and the speed cap `goliath-prep` is designed to apply by default. Whether the router takes its speed from `S` or from a dial on the router is not known yet. |
| Header macro | `gcodePre` | `;; DECIMALS = 3`, `G21`, `G90`, `G17` | `;; DECIMALS = 3` sets how many decimals Kiri:Moto writes for numbers that are not whole: three, the layout GI recommends ([gi-format.md section 8.2](../../docs/prep/gi-format.md#82-numbers)). Whole numbers it writes with one decimal (`Z5.0`); `goliath-prep` accepts any number of decimals. Kiri:Moto reads that line and does not write it (a directive since Kiri:Moto 3.8, according to its documentation). Then millimetres, absolute coordinates and the XY plane. No comment and no annotation (section 2). |
| Footer macro | `gcodePost` | `M5`, `M30` | Kiri:Moto never writes `M5` itself, so the footer stops the spindle. `M30` stands alone on the last line ([gi-format.md section 8.8](../../docs/prep/gi-format.md#88-program-end-phys-17)). |
| Spindle macro | `gcodeSpindle` | `M3 S{speed}` | Spindle on, clockwise, at the operation's speed. Never give an operation a negative speed: Kiri:Moto then writes `M4`, which `goliath-prep` refuses (`D-UNSUPPORTED`). |
| Tool macro | `gcodeChange` | `M6 T{tool}` | Keep it. If a job uses a second tool, this line reaches `goliath-prep` after the first move and the file is refused (`D-SINGLETOOL`, [gi-format.md section 8.9](../../docs/prep/gi-format.md#89-one-tool)). Emptied, the second tool's paths would arrive with nothing to say they need another bit, and would be cut with the first one. |
| Dwell macro | `gcodeDwell` | `G4 P{time}` | Keep it, and set dwell times to zero in your operations. A dwell reaches `goliath-prep` as `G4`, which it refuses until bring-up rung R4 has shown how the spindle spins up, and afterwards drops with a disclosure (`G-GATE`, [gi-format.md section 8.5](../../docs/prep/gi-format.md#85-refused-words)). Emptied, a dwell would vanish without a trace. |
| File Extension | `gcodeFExt` | `"gcode"` | Keeps CAM output apart from the robot's own programs (`.NC` files), which `goliath-prep` only verifies. |
| Token Spacer | `gcodeSpace` | `true` | A space between words (`G1 X1 Y1`): easier to read. |
| Strip Comments | `gcodeStrip` | `true` | Required (section 2). |
| laser | `useLaser` | `false` | No laser operations. |
| rotary indexer | `useIndexed` | `false` | No rotary axis: `goliath-prep` refuses `A` and `B` words (`D-UNSUPPORTED`). |

## 4. The settings of each job

Kiri:Moto keeps these with each job (its process settings), not with the
machine. Their labels depend on the Kiri:Moto version, so they are described
here, not named:

- **Units:** millimetres. Kiri:Moto takes them from its global preferences,
  which default to millimetres.
- **Stock:** the panel. Width = panel width W, depth = panel height H, height =
  panel thickness T measured with callipers: the same three numbers as in the
  job file.
- **Origin:** X 0, Y 0 at the stock corner where tower 1 stands. The stock then
  lies at positive X and Y, so in Kiri:Moto's top view the origin is the
  stock's lower-left corner (the smallest X and Y). Z 0 at the top of the
  stock, so cuts go to negative Z. No offsets. Kiri:Moto's documentation places
  the Z zero among its limits settings and the XY origin, with optional
  offsets, among its origin settings.
- **First Z Max** (an output setting in Kiri:Moto's documentation): on. With
  it, Kiri:Moto's first move is in Z alone, to the height of the job's first
  point, and only then in X and Y
  ([gi-format.md section 14](../../docs/prep/gi-format.md#14-writing-a-post-informative), item 3).
  Without it, the first move goes in X and Y at a height the file never
  stated. Check in your export that the first move's Z is above the stock
  (positive).
- **One tool per file.** A job with two bits is two files and two jobs, each
  with its own job file. Kiri:Moto can export a zip archive with a separate
  file per operation for each tool.
- **Dwell times zero, spindle speeds positive** (section 3).
- **Arcs:** when Kiri:Moto writes arcs, it writes `G2`/`G3` with `I` and `J`,
  which `goliath-prep` accepts
  ([gi-format.md section 8.7](../../docs/prep/gi-format.md#87-arcs-phys-16)).

## 5. What a file from this profile looks like, and how to check yours

The first three lines are what the Header macro writes, the last two what the
Footer macro writes, and the `M3` line is the Spindle macro, which Kiri:Moto
writes before its first move. The moves are written by hand to show the
shape, in Kiri:Moto's number layout; they are not captured from Kiri:Moto.
They cut a 60 × 40 mm groove, 3 mm deep, in the middle of the sample panel of
the [generic guide](../../docs/user-guide/cam-generic.md#8-a-sample-file).

<!-- goliath-sample {"id": "kirimoto-shape", "expect": "ok", "from_profile": true} -->
```gcode
G21
G90
G17
M3 S18000
G0 Z5.0
G0 X1200.0 Y600.0
G1 Z-3.0 F300
G1 X1260.0 F1200
G1 Y640.0
G1 X1200.0
G1 Y600.0
G0 Z5.0
M5
M30
```

Open your own export in a text editor and check:

1. Line 1 is `G21`, and there is no `;` and no `(` anywhere in the file. A `;`
   comment means Strip Comments is off, or your Kiri:Moto writes comments this
   profile does not expect: `goliath-prep` would refuse the file (`D-MARKER`
   or `D-GI-VERSION`). Parentheses are refused too (`D-SYNTAX`).
2. The last two lines are `M5` and `M30`.
3. At most one `M6 T…` line, and only before the first move. A second one means
   the job uses a second tool: split it (section 4).
4. No `G4` line (a dwell) and no `M4` (a reversed spindle).
5. Then the checklist of the generic guide,
   [section 4](../../docs/user-guide/cam-generic.md#4-check-the-exported-file).

## 6. Setting it up in Kiri:Moto

Kiri:Moto's documentation describes a Machine Setup tab, opened from the menu
at the top right, with the settings of section 3: the workspace, Max Spindle,
Strip Comments, Token Spacer, File Extension and the Header, Footer, Tool,
Dwell and Spindle macros. It describes sharing machines as an exported
workspace (`.kmz`), and no way to import a machine file like
`goliath-device.json`. So:

1. In Kiri:Moto's CAM mode, create a machine of your own. Kiri:Moto's forum
   describes copying an existing machine, for example a generic grbl one, and
   changing its settings.
2. Type in the values of section 3, one by one. If your Kiri:Moto does not know
   the `;;` line, leave it out: it changes nothing but the number of decimals.
3. Export a small job and check the file as in section 5.

`goliath-device.json` holds the same values in the layout of Kiri:Moto's own
machine definitions, so that the repository's tests can check them and the
profile can be offered to the Kiri:Moto project later.

## 7. Limits

- No GI header, so no automatic cross-check of the CAM stock and tool against
  the job file (section 2), and the operations are inferred from Z.
- What section 2 says about Kiri:Moto was read in its code at one date. After
  updating Kiri:Moto, check an export again (section 5, step 1).
- `goliath-prep` is not released yet. Once it is, `goliath-prep check` is the
  final word on a file, not this page.
