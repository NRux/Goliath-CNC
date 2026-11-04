# 05 — G-code dialect

*Source: `PPGoliath` (PPGoliath.cs, GoliathParameters.cs), `GoliathWPF`
GCodeCleaner.cs / SendingGCode.cs, Slingshot 1.9.3.0. Verified: 26/27 facts
confirmed (1 citation corrected, claim true). The dialect is **byte-identical
across all five analysed versions**.*

## What the robot actually receives

Slingshot's post-processor (`PPGoliath`) writes a `.NC` ISO file, then
**`GCodeCleaner.GetGCodeForGoliath`** transforms it before upload:
- every character from the first `;` to end-of-line is **removed** (all comments
  stripped),
- lines are re-joined with **CRLF**, and the file is made to end with CRLF.

That cleaned text — ASCII, no comments, CRLF — is what streams to the robot. So a
replacement does **not** need the `.NC` comment conventions; it needs to emit the
**motion/spindle lines** below. (The operation-boundary markers `;U` and `;S` are
the exception: they drive tool-change / empty-bin behaviour and are recognised by
the robot/app — see [03-session-sequence.md](03-session-sequence.md).)

## Number formatting

- **Units: millimetres, implicit.** No `G20`/`G21` is ever emitted.
- **Decimal separator is `.`** and there is no thousands separator (the whole app
  forces `InvariantCulture`).
- **Coordinates X/Y/Z and arc I/J: 3 decimals.** Feed `F`, spindle `S`, and the
  `C` angle: **0 decimals** (integers).
- Each present word is preceded by a space: `G01 X10.000 Y20.000 Z-5.000 C315 F1500 S18000`.

## Codes emitted

| Code | Role | Notes |
|---|---|---|
| `G00` | rapid | XY (+`C`) on one line; **Z on a separate following line**, clamped to the safety height |
| `G01` | linear feed | cuts, lead-ins, ramps, lead-outs, hole entry |
| `G02` / `G03` | arc CW / CCW | incremental centre `I<cx-sx> J<cy-sy>`, **no `R`, no `K`** |
| `M10` | **spindle ON** | *not* M03 |
| `M05` | **spindle OFF / stop** | emitted before every rapid |
| `M06` | tool change | resets modal position memory |
| `M11` | dust/chip discharge | wrapped by M05…M10 (stop, discharge, restart spindle) |
| `F` | feed rate | integer, mm/min |
| `S` | spindle speed | integer, rpm |
| `C` | **tool-head rotation** | integer degrees, value = `(toolAngle − 90 + 360) mod 360` |
| `X`/`Y`/`Z` | position | absolute mm, 3 dp |
| `I`/`J` | arc centre | incremental from arc start |

**Not emitted** by the post-processor (would only appear if a user put them in a
custom header/footer): `G20`/`G21`, `G90`/`G91`, `M00`/`M30`, `M03`/`M04`, and no
`%` program sentinel. Motion is effectively **absolute (G90)** without the word
being written. Optional block numbering (`N100`, step +5) exists but is **forced
off** by default.

## Semantics a replacement must match

- **Spindle convention is M10/M05**, not the usual M03/M05. Emit M10 to start.
- **The `C` word is mandatory for correct cuts.** The robot physically rotates its
  tool head; `C` carries that orientation (offset −90°, wrapped 0–359). This comes
  from the CAM step's per-cut "best angle" selection (panel-edge + tower-cable
  collision avoidance); a replacement CAM must compute a head angle per move, not
  just XY.
- **Work Z-zero is the top of the stock; cuts are negative Z.** The post-processor
  shifts every point's Z down by the material thickness (`DimZ`). A rapid Z is
  clamped to the safety plane `min(pointZ, Z_safety)` — rapids never plunge below
  the safety height. Default `Z_safety` = 10 mm (override via
  `Slingshot.ini [UTENSILI] Z_SAFETY_DISTANCE`).
- **Rapids are split:** an `M05` line, then a `G00 X… Y… C…` line, then a separate
  `Z…` line at the safety height.
- **Entry vs cutting feed:** the first cutting move right after the spindle turns
  on uses the tool's **entry/plunge** feed; later moves use the **work** feed. (And
  after a rapid, the next cut re-emits `F` and `S` even if unchanged.)
- **Arcs** are real `G02`/`G03` with incremental centre offsets; direction from a
  3-point clockwise test. Arcs may be pre-linearised upstream into `G01` segments
  on some working faces (chordal tolerance 0.25 mm) — both forms are valid input.
- **Modal suppression:** a word is written only when its value changes from the
  previous line. A parser must carry modal state (last X/Y/Z/C/F/S and the active
  G/M) across lines.

## Upload mechanics (recap)

See [03-session-sequence.md](03-session-sequence.md) step G. In short: optional
`GCODE CONTINUE;0` (firmware ≥ 01.02.00), then `GCODE UPLOAD;<cleaned-byte-count>`,
then the cleaned ASCII bytes in **1024-byte blocks** (`GCODE_PACK_SIZE` from the
INI; the block size is read from config, **not** hard-coded), with a short delay
between blocks; success = `INFO;8`, failure = `ERR;0`.

## Toolpath ordering and "don't fall into cuts"

The marketing claim that Goliath "avoids falling into pockets/already-cut areas"
maps, in the code, to two **offline** mechanisms in `WorkLogicLibrary` (not the
firmware and not a runtime check):
- **Ordering:** `SortLavs`/`SortDIST` — nearest-neighbour sequencing by
  tool/face/priority.
- **Angle selection:** `FindBestAngle` + `CheckIfWheelIsNearTowers` +
  `PointInTriangle` — choose each cut's `C` head angle so the robot's footprint
  avoids the panel edges and the two tower cables.

There is **no** runtime "don't drive over an already-cut region" check. A
replacement CAM that reproduces sensible ordering and a safe head angle, and keeps
rapids at safety height, matches the vendor's behaviour.

## Caveats
- The post-processor also reads tool geometry/speeds from a `ppGoliath.ute` tool
  table and parameters from `ppGoliath.par` (header/footer lines, overridable
  codes). Those files weren't in the installer payload, so header/footer content
  is unknown — but since the robot ignores comments and the dialect defaults are
  as above, a replacement can emit a bare program without vendor header/footer.
- Confirm on hardware that the robot tolerates G-code **without** any header/footer
  and without `M30` (the vendor stream likewise has no `M30` unless a user added
  it).
