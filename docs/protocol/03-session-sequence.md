# 03 — Session sequence

*Source: `GoliathWPF` Setup/SetupStep/Production/PanelDetection/SendingGCode,
Slingshot 1.9.3.0. Verified: 49/49 session facts confirmed.*

## The governing principle

**The robot runs its own state machine and advances itself.** The PC does **not**
drive the phase machine. `SET SETUP PHASE` and `SET WORK PHASE` exist in the
protocol but the app **never sends them**. Instead the PC:

1. sends **configuration** (RF channels, panel size, calibration values) and
   **actions** (upload G-code, abort, jog), and
2. **observes** phase changes through `INFO;<n>` messages and the binary status
   frames.

So a replacement is mostly a reactive state-follower: send the right thing when
the robot reports the right phase, and wait for the completion `INFO`.

The setup phases the firmware moves through (`eSETUP`): `PAIRING(0)` →
`PANEL_DETECTION(1)` → `GCODE_UPLOAD(2)` → `DRILL_CALIBRATION(3)` →
`DRILL_ZERO_CHECK(5)` → `COMPLETE(6)`. Work phases (`eWORK`): `WORK_CYCLE(0)`,
`WORK_HALTED(1)`, `WORK_PAUSED(2)`, `COMPLETE(3)`.

## Full job flow

Each step lists what the PC **sends** and what it **waits for**. Where "send"
is "(none)", the firmware acts autonomously and the PC only listens.

| # | Phase | PC sends | PC waits for |
|---|---|---|---|
| A | provisioning *(optional)* | `GET SSIDS`; then `SSID=<hex>&PWD=<hex>&AUTH_TYPE=<n>` | `INFO;31` `START`/`<ssid>`/`END`; then rejoin network & re-find robot |
| B | PAIRING(0) | `SET RF CHANNELS;<c1>;<c2>` | towers online (`INFO;1001` / global status); pairing done = `INFO;4` |
| C | enter setup | `GET GLOBAL SYS STATUS` | global status frame (id 1002); then encoder calibrated = `INFO;24` |
| D | PANEL_DETECTION(1) — perimeter | (none) | robot streams `INFO;25;x;y;z` vertices as it traces the panel edge |
| D2 | PANEL_DETECTION(1) — towers | (none) | `INFO;27;<distance>` (inter-tower distance) |
| D3 | PANEL_DETECTION(1) — T2 axis | `SET T2 POSITION;<1\|0>` | panel detection done = `INFO;6` |
| E | PLAY / cage check | `GET SPINDLE CAGE STATUS` | reply containing `CAGE STATUS` + `CLOSED` (must be CLOSED to proceed) |
| F | setup COMPLETE(6) | `SET PANEL SIZE;<x>;<y>`, then `SEND TCP POS`, `PRINT SYS REPORT`, `GET GLOBAL SYS STATUS` (≈250–500 ms apart) | `INFO;40` sanity report; status frame |
| G1 | GCODE_UPLOAD(2) — reset | `GCODE CONTINUE;0` *(only if firmware ≥ 01.02.00)*, wait 250 ms | (no reply) |
| G2 | GCODE_UPLOAD(2) — header | `GCODE UPLOAD;<filesize_bytes>`, wait 250 ms | `INFO;7` (entered upload) |
| G3 | GCODE_UPLOAD(2) — payload | raw cleaned G-code in **1024-byte blocks**, ~50 ms + `GCODE_PACK_INTERVAL` between blocks | **`INFO;8`** = received OK (failure = `ERR;0`) |
| H | WORK_CYCLE(0) | (none — robot starts automatically) | `INFO;1000` position frames stream; `INFO;23` = work complete |

Notes on the flow:
- **Upload size** is the byte length of the **cleaned** G-code (comments stripped,
  CRLF line endings — see [05-gcode-dialect.md](05-gcode-dialect.md)), not the raw
  `.NC`. The size you announce in `GCODE UPLOAD;<n>` must match the bytes you then
  stream.
- The firmware-gated `GCODE CONTINUE;0` pre-step (G1) tells the robot to **discard
  any previously loaded program and wait for a new one**. Skipping it on firmware
  ≥ 01.02.00 risks the robot trying to resume an old job.
- After a robot reset mid-job the firmware sends `INFO;41` ("resume previous or
  start new"). The vendor app does **not** auto-answer; it waits for the user. A
  replacement should surface this choice (`GCODE CONTINUE;1` to resume, or re-upload).
- On firmware > 01.02.00 the app sends `GET GLOBAL SYS STATUS` about every **60 s**
  as a keep-alive while idle/working.

## Maintenance / sub-flows (reached outside the linear wizard)

| Action | Sequence | Notes |
|---|---|---|
| Jog | `ENTER JOG` → `GOTO TARGET;x;y;0;angle` → `JOG STOP GOLIATH` → `EXIT JOG`; `ENABLE/DISABLE MANUAL JOG` for push-by-hand | acks `INFO;35` (mode), `INFO;32` (target reached) |
| In-job tool-change Z calibration (triggered by a `;U` G-code marker) | `GET Z PLATE OFFSET` → `SET Z PLATE OFFSET;<v>` | wait `INFO;43`; gated to firmware ≥ 01.02.02 for the dialog |
| Center-bit / drill calibration | `READ SPINDLE CALIB` → `SET SPINDLE DISTANCE;<d>` → `SET INDEX CALIBRATION VALUE;<angle>` | reply `spindle distance: <v>` |
| Z backlash | `GET Z BACKLASH` → `SET Z BACKLASH;<v>` (reset `;0`) | reply `Z const offset: <v>` |
| Index distance | `READ INDEX CALIBRATION` → `SET INDEX CALIBRATION VALUE;<n>` | reply `cfg read index :<v>`; only send if `27000 < n < 30100` |
| Spindle / aspiration / free Z | `ENABLE/DISABLE SPINDLE`, `ENABLE/DISABLE ASPIRATION`, `FREE Z AXIS` | no completion INFO |
| Abort a job | `ABORT WORK` | app gates to firmware > 01.02.00 |

## G-code in-stream markers

The uploaded G-code can carry two **comment markers** the robot/app act on during
work (they survive as behaviour even though literal comments are stripped before
upload — the robot recognises the operation boundaries):
- `;U|…` — **tool change**: pause, prompt the user to change the bit, then run the
  tool-change Z-calibration sub-flow.
- `;S…` — **empty the dust bin** notification.

## Caveats
- All timings (250 ms waits, 50 ms inter-block, 60 s keep-alive) are the vendor
  app's values; they are starting points, to be confirmed against a real machine.
- The cage check reply and several calibration replies are free-form text lines;
  match by substring (`CLOSED`, `spindle distance:`, etc.).
- The "PLAY"/confirm ordering between cage check and panel-size commit is as the
  app does it; a replacement should keep the cage check **before** any spindle
  enable.
