# 03 — Session sequence

*Source: `GoliathWPF` Setup/SetupStep/Production/PanelDetection/SendingGCode,
Slingshot 1.9.3.0. Verified: 49/49 session facts confirmed. **Corrected 2026-11-04
against static disassembly of the robot firmware GFW 01.02.00**; see
[09-firmware-01.02.00.md](09-firmware-01.02.00.md) for addresses.*

## The governing principle

**The robot runs its own state machine and advances itself.** The PC does **not**
drive the phase machine. `SET SETUP PHASE` and `SET WORK PHASE` exist in the
protocol but the app **never sends them**. Instead the PC:

1. sends **configuration** (RF channels, panel size, calibration values) and
   **actions** (upload G-code, jog — and abort, on firmware that has it), and
2. **observes** phase changes through `INFO;<n>` messages and the binary status
   frames.

So a replacement is mostly a reactive state-follower: send the right thing when
the robot reports the right phase, and wait for the completion `INFO`.

The setup phases the firmware moves through (`eSETUP`): `PAIRING(0)` →
`PANEL_DETECTION(1)` → `GCODE_UPLOAD(2)` → `DRILL_CALIBRATION(3)` →
`DRILL_ZERO_CHECK(5)` → `COMPLETE(6)`. Work phases (`eWORK`): `WORK_CYCLE(0)`,
`WORK_HALTED(1)`, `WORK_PAUSED(2)`, `COMPLETE(3)`.

## Connecting

> **Correction.** Earlier revisions treated connecting as "open a TCP socket to
> port 80". The firmware routes its output by connection slot, so the connect
> sequence is part of the protocol. Details in
> [01-transport-and-framing.md](01-transport-and-framing.md#three-connections-and-slot-routing).

1. Open connection **S1**, wait ≈250 ms, open **S2**, wait ≈250 ms, open **S3**,
   wait ≈500 ms (the vendor's timings). If any of the three fails, close all and
   report it.
2. On a robot with no other client, S1/S2/S3 take firmware slots 0/1/2:
   - **S1** — the connection you send on. It receives free-form `printf` text,
     tower frames, and the direct replies to your commands (for example the
     `GLOBAL STATUS` blob).
   - **S2** — receives every `INFO`, `ERR` and `WARN`, including all
     acknowledgements and all safety errors.
   - **S3** — receives position frames.
3. **Connect-time replay.** When the connections are accepted, the firmware may
   push, depending on its state, `INFO;34` (identity, with the firmware version)
   and `INFO;4` / `INFO;6` / `INFO;8` (paired / panel detected / G-code received)
   and `INFO;24` (encoder calibrated). They arrive on S2. The exact conditions are
   not fully decoded, so a client must not *require* them.
   - The vendor app (1.9.3.0) never sends `INFO`; it reads the version from this
     pushed `INFO;34`. A replacement may send `INFO` (read-only on 01.02.00) to
     ask explicitly — the reply arrives on **S2**, not S1.
4. If another client (an old Slingshot, a phone) still holds slots, the robot's
   replies go to that client. If expected replies do not arrive on S2, say so —
   do not conclude that the robot is silent or that the command failed.
5. Send only on S1, one command at a time, ≈0.3 s apart, with `TCP_NODELAY`
   ([01](01-transport-and-framing.md#outbound-pc--robot)).

Disconnecting frees the slots and does not stop or change any work in progress.

## Full job flow

Each step lists what the PC **sends** and what it **waits for**. Where "send"
is "(none)", the firmware acts autonomously and the PC only listens. The last
column says what changes on GFW 01.02.00.

| # | Phase | PC sends | PC waits for | On GFW 01.02.00 |
|---|---|---|---|---|
| A | provisioning *(optional)* | `GET SSIDS`; then `SSID=<hex>&PWD=<hex>&AUTH_TYPE=<n>` | `INFO;31` `START`/`<ssid>`/`END`; then rejoin network & re-find robot | `INFO;31` on S2; the `SSID=…` string is not a table command (provisioning path unverified) |
| B | PAIRING(0) | `SET RF CHANNELS;<c1>;<c2>` | towers online (`INFO;1001` / global status); pairing done = `INFO;4` | tower frames on S1, `INFO` on S2 |
| C | enter setup | `GET GLOBAL SYS STATUS` | global status; then encoder calibrated = `INFO;24` | reply is the 48-byte `GLOBAL STATUS` blob on S1 plus `INFO;0` on S2 |
| D | PANEL_DETECTION(1) — perimeter | (none) | robot streams `INFO;25` vertices as it traces the panel edge | `INFO;25;x;y` (two values) |
| D2 | PANEL_DETECTION(1) — towers | (none) | `INFO;27;<distance>` (inter-tower distance) | |
| D3 | PANEL_DETECTION(1) — T2 axis | `SET T2 POSITION;<1\|0>` | panel detection done = `INFO;6` | |
| E | PLAY / cage check | `GET SPINDLE CAGE STATUS` | reply `CAGE STATUS: CLOSED` (must be CLOSED to proceed) | reply on S1 (requester), unpadded, no newline after `CLOSED` |
| F | setup COMPLETE(6) | `SET PANEL SIZE;<x>;<y>`, then `SEND TCP POS`, `PRINT SYS REPORT`, `GET GLOBAL SYS STATUS` (≈250–500 ms apart) | `INFO;40` sanity report; status | `SEND TCP POS` **does not exist** — skip it |
| G1 | GCODE_UPLOAD(2) — reset | `GCODE CONTINUE;0` *(vendor: only if firmware ≥ 01.02.00)*, wait 250 ms | (no reply) | **does not exist** — an unknown command, statically ignored |
| G2 | GCODE_UPLOAD(2) — header | `GCODE UPLOAD;<filesize_bytes>`, wait 250 ms | `INFO;7` (entered upload) | handler switches the parser to upload mode (below); sends no immediate reply itself |
| G3 | GCODE_UPLOAD(2) — payload | raw cleaned G-code in **1024-byte blocks**, ~50 ms + `GCODE_PACK_INTERVAL` between blocks | **`INFO;8`** = received OK (failure = `ERR;0`); the vendor waits for `INFO;8` only on firmware ≥ 01.02.03 | on completion the firmware sends `INFO;0`; whether it also sends `INFO;8` is not established |
| H | WORK_CYCLE(0) | (none — robot starts automatically) | position frames stream; `INFO;23` = work complete | position frames on S3 |

Notes on the flow:
- **Upload size** is the byte length of the **cleaned** G-code (comments stripped,
  CRLF line endings, ending in an empty line — see
  [05-gcode-dialect.md](05-gcode-dialect.md)), not the raw `.NC`. The size you
  announce in `GCODE UPLOAD;<n>` must match the bytes you then stream.
- **Upload mode (firmware, 01.02.00).** `GCODE UPLOAD;<n>` sets a flag that turns
  the command parser off: from then on **every** received buffer, from **any**
  connection, is stored as program data, until the firmware's end-of-upload check
  passes; then it clears the flag and sends `INFO;0`. Consequences:
  - Never send anything else — keep-alives included — from any thread or any
    client while an upload is in progress. It becomes part of the program.
  - If an upload is cut short, the robot stays in upload mode and treats the next
    commands as G-code. No other code path that clears the flag was found
    (statically; an access through a different pointer cannot be excluded). Plan
    on a robot restart after a failed upload.
- **`GCODE CONTINUE;0` (G1).** The vendor app sends it before each fresh upload on
  firmware ≥ 01.02.00, to make the robot discard any previously loaded program.
  > **Correction.** Earlier revisions said "skipping it on firmware ≥ 01.02.00
  > risks the robot trying to resume an old job". GFW 01.02.00 has no
  > `GCODE CONTINUE` command, so on 01.02.00 the vendor's pre-step was a no-op.
  > It may matter on later firmware.
- After a robot reset mid-job the firmware sends `INFO;41` ("resume previous or
  start new"). The vendor app does **not** auto-answer; it waits for the user. A
  replacement should surface this choice. The vendor's answer (`GCODE CONTINUE;1`
  to resume, or re-upload) needs `GCODE CONTINUE`, which 01.02.00 does not have.
- On firmware > 01.02.00 the app sends `GET GLOBAL SYS STATUS` about every **60 s**
  as a keep-alive while idle/working. Never during an upload (above).

## Stopping a job

> **Correction.** Earlier revisions listed "Abort a job: `ABORT WORK` (app gates to
> firmware > 01.02.00)" as an ordinary maintenance action. On GFW 01.02.00 there
> is **no software abort**.

| Firmware | What exists | What the vendor app did |
|---|---|---|
| **01.02.00** | No `ABORT WORK` in the command table. Sending it is an unknown command: statically no reply and no effect. Closing the connections does not stop the job either. | Disabled its Abort button, with the tooltip "Abort functionality is not available on current firmware version". |
| **> 01.02.00** | `ABORT WORK` presumably exists (we have no image to check). | Enabled the Abort button and sent `ABORT WORK`. What acknowledges it is not known. |

- **On 01.02.00 the only way to stop a running job is physical**, at the machine
  (cut its power). Plan every job on the assumption that the software cannot stop
  it.
- A replacement must check the firmware version before offering a software stop,
  must not send `ABORT WORK` to 01.02.00 and report success, and on later firmware
  must wait for a confirming message (for example `INFO;18`/`INFO;23` or position
  frames stopping) before telling the user the job stopped.
- GFW 01.02.00 does contain `GCODE STOP` (and `GCODE START`, `GCODE RESTART`),
  which acknowledge with `INFO;0`. The vendor app never sends them and what they
  do is unknown. They are **not** a substitute for an abort until tested on a
  machine set up for it.
- `JOG STOP GOLIATH` stops a jog move; the vendor app uses it only in jog mode.

## Second job on the same power cycle

On firmware ≤ 01.02.00 the vendor app refuses a second upload once a program has
been uploaded in the session ("Please restart Goliath before starting another
job.") and closes itself. This is **vendor behaviour**; whether the firmware
actually needs the restart is unverified. Note that GFW 01.02.00 also has no
remote reboot: `CMD SYS RESET` is not in its table (a bare `SYS RESET` entry
exists but has no handler), so "restart" means power-cycling the robot. A
replacement should follow the vendor here until a test shows otherwise.

## Maintenance / sub-flows (reached outside the linear wizard)

| Action | Sequence | Notes | On GFW 01.02.00 |
|---|---|---|---|
| Jog | `ENTER JOG` → `GOTO TARGET;x;y;0;angle` → `JOG STOP GOLIATH` → `EXIT JOG`; `ENABLE/DISABLE MANUAL JOG` for push-by-hand | acks `INFO;35` (mode), `INFO;32` (target reached) | all present |
| In-job tool-change Z calibration (triggered by a `;U` G-code marker) | `GET Z PLATE OFFSET` → `SET Z PLATE OFFSET;<v>` | wait `INFO;43`; gated to firmware ≥ 01.02.02 for the dialog | **both absent**; the vendor never ran this flow on 01.02.00 |
| Center-bit / drill calibration | `READ SPINDLE CALIB` → `SET SPINDLE DISTANCE;<d>` → `SET INDEX CALIBRATION VALUE;<angle>` | reply `spindle distance: <v>` (no newline) | `SET SPINDLE DISTANCE` **absent** |
| Z backlash | `GET Z BACKLASH` → `SET Z BACKLASH;<v>` (reset `;0`) | reply `Z const offset: <v>` | **both absent** |
| Index distance | `READ INDEX CALIBRATION` → `SET INDEX CALIBRATION VALUE;<n>` | reply `cfg read index : <v>` (no newline); only send if `27000 < n < 30100` | present |
| Spindle / aspiration / free Z | `ENABLE/DISABLE SPINDLE`, `ENABLE/DISABLE ASPIRATION`, `FREE Z AXIS` | `INFO;0` ack for spindle/aspiration on 01.02.00; no specific completion INFO | present |
| Abort a job | `ABORT WORK` | app gates to firmware > 01.02.00 | **absent** — see [Stopping a job](#stopping-a-job) |

## G-code in-stream markers

The uploaded G-code can carry two **comment markers** the robot/app act on during
work (they survive as behaviour even though literal comments are stripped before
upload — the robot recognises the operation boundaries):
- `;U|…` — **tool change**: pause, prompt the user to change the bit, then run the
  tool-change Z-calibration sub-flow (firmware ≥ 01.02.02 only, see above).
- `;S…` — **empty the dust bin** notification.

## Caveats
- All timings (250 ms waits, 50 ms inter-block, 60 s keep-alive) are the vendor
  app's values; they are starting points, to be confirmed against a real machine.
- The cage check reply and several calibration replies are free-form text; match
  by prefix (`CAGE STATUS:`, `spindle distance:`, `cfg read index :`) and expect no
  newline.
- The "PLAY"/confirm ordering between cage check and panel-size commit is as the
  app does it; a replacement should keep the cage check **before** any spindle
  enable.
- Every firmware statement is from static analysis of GFW 01.02.00. The robot's
  real firmware version is unknown until it is read; later firmware may behave
  differently.
