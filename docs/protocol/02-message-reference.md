# 02 — Message reference

*Source: `GoliathCommLibTCP/SocketManager.cs` static command/response tables,
Slingshot 1.9.3.0. Wire codes are taken verbatim from the registration arrays, not
from enum order. Verified against source. **Corrected 2026-11-04 against static
disassembly of the robot firmware GFW 01.02.00**; firmware addresses and the full
94-entry firmware command table are in [09-firmware-01.02.00.md](09-firmware-01.02.00.md).*

Conventions: `{0}`,`{1}`… are positional arguments joined with `;`. All text is
ASCII; integers are decimal. Columns:

- **Used** — whether the shipped 1.9.3.0 app ever actually sends it. Several
  commands exist in the vendor table but are never issued.
- **01.02.00** — whether the command exists in the command table of firmware
  **GFW 01.02.00** (the image bundled with Slingshot 1.09.05 through 1.9.3.0, and
  the only robot firmware that survives). **ABSENT** means the name is not in the
  table: the robot treats it as an unknown command and, statically, sends no reply
  and does nothing.

> **Correction.** Earlier revisions presented the vendor app's command table as
> the robot's command set. The firmware shows that **ten commands the vendor app
> knows do not exist on GFW 01.02.00**: `ABORT WORK`, `GCODE CONTINUE`,
> `SEND TCP POS`, `GET Z BACKLASH`, `SET Z BACKLASH`, `GET Z PLATE OFFSET`,
> `SET Z PLATE OFFSET`, `SET SPINDLE DISTANCE`, `DISCARD TWRS DISTANCE` and
> `CMD SYS RESET`. They presumably exist on later firmware (the app gates some of
> them on version, see [07](07-version-compatibility.md)), but no such image
> survives. Never assume they did anything on 01.02.00.

Where replies go (connection/slot) and what they look like on the wire is
specified in [01-transport-and-framing.md](01-transport-and-framing.md). In short:
every `INFO`/`ERR`/`WARN` goes to the **second** connection; free-form `printf`
text and tower frames to the **first**; position frames to the **third**; a few
replies go straight back to the connection that sent the command.

## Commands (PC → robot)

### Connection / system
| Wire string | Meaning | Used | 01.02.00 |
|---|---|---|---|
| `INFO` | Request identity → `INFO;34;<prj>;<HW>;<SW>;<SN>` (on the 2nd connection) | **no** — see note | present |
| `CMD SYS RESET` | Reboot the robot | yes | **ABSENT** (the table has a bare `SYS RESET` entry with no handler) |
| `GET GLOBAL SYS STATUS` | Request global status. On 01.02.00 → the 48-byte `GLOBAL STATUS` blob to the requester, plus `INFO;0`. Vendor also used it as a ~60 s keep-alive | yes | present |
| `PRINT SYS REPORT` | Sanity self-check report → `INFO;40;{b1…b15};END` (0=fail,1=ok) | yes | present |
| `ENABLE INFO LOG;{0}` | Enable/disable INFO stream during setup/work (1/0) | — | present |
| `GET SSIDS` | Robot scans Wi-Fi → streams `INFO;31;START` / `INFO;31;<ssid>` / `INFO;31;END` | yes | present |
| `{0}` | Raw pass-through of a custom string (eCMD `NONE`) | — | n/a |

> **Correction.** Earlier revisions marked `INFO` as used by the 1.9.3.0 app. It is
> not: the app relies on the `INFO;34` the robot pushes when the connections are
> opened ([03](03-session-sequence.md#connecting)). The firmware handler only
> formats `INFO;34` from constants, so it is safe to send.

### Wi-Fi provisioning (sent as raw strings, not in the eCMD table)
| Wire string | Meaning | 01.02.00 |
|---|---|---|
| `SSID=<hexSSID>&PWD=<hexPWD>&AUTH_TYPE=<1\|2\|3>` | Give the robot a network to join. SSID/PWD are per-character uppercase hex. AUTH_TYPE 1=Open, 2=WPA/WPA2-PSK, 3=WEP | not a table command; presumably handled by the provisioning-mode path (unverified) |
| `RESET NET KEY` | Legacy (firmware < 01.00.14) refresh of the SSID list | present |

### Tower pairing & geometry
| Wire string | Meaning | Used | 01.02.00 |
|---|---|---|---|
| `SET RF CHANNELS;{0};{1}` | Set the two towers' RF channels (T1,T2). Values from a `CONFIG_n` pair in `Slingshot.ini` | yes | present |
| `SET T2 POSITION;{0}` | Second tower axis: 1 = T2 along X, 0 = along Y | yes | present |
| `DISCARD TWRS DISTANCE` | Discard measured inter-tower distance; reset panel-detection FSM to the distance step | yes | **ABSENT** |
| `TOWERS POSITION;{0};{1};{2};{3}` | Give tower coords `x1;y1;x2;y2` so robot computes distance | no (defined only) | present |
| `GET TOWERS DISTANCE` | Read inter-tower distance (app instead reads pushed `INFO;27`) | no (defined only) | present |

### Panel & calibration
| Wire string | Meaning | Used | 01.02.00 |
|---|---|---|---|
| `SET PANEL SIZE;{0};{1}` | Working panel size `x_max;y_max` (mm) — the only way the app sends the panel | yes | present |
| `GET PANEL SIZE` | Read panel size. Vendor table: → `INFO;26;x_max;y_max`. **On 01.02.00:** → `xmax=<%.4f> \n\rymax=<%.4f>` to the requester (unterminated), plus `INFO;0` | no | present |
| `SET VERTEX;{0};{1}` | Send one panel-polygon vertex `length;angle` | no (robot emits vertices instead) | present |
| `GET VERTEXES` | Read panel vertex list | no | present |
| `READ INDEX CALIBRATION` | Read encoder index → `cfg read index : <n>` (first connection, **no newline**), plus `INFO;0` | yes | present |
| `SET INDEX CALIBRATION VALUE;{0}` | Write encoder index (app only sends when `27000 < n < 30100`) | yes | present |
| `READ SPINDLE CALIB` | Read spindle distance → `spindle distance: <%.2f>` (first connection, **no newline**), plus `INFO;0` | yes | present |
| `SET SPINDLE DISTANCE;{0}` | Write spindle/center-bit distance | yes | **ABSENT** (a differently named `SPINDLE CALIB VALUE` entry exists; whether it is the same function is unknown — do not substitute it) |
| `GET SPINDLE CAGE STATUS` | Read spindle-cover state → `CAGE STATUS: OPEN\n` or `CAGE STATUS: CLOSED` (no newline) to the requester, plus `INFO;0` | yes (firmware ≥ 01.00.14) | present |
| `POLL CAGE` / `STOP POLL CAGE` | Legacy continuous cage polling (firmware < 01.00.14). On 01.02.00 `POLL CAGE` starts a 500 ms timer that prints `CAGE STATUS: <OPEN\|CLOSED>\n` to the first connection | yes (legacy) | present |
| `GET Z BACKLASH` / `SET Z BACKLASH;{0}` | Read/write constant Z offset (backlash); reset = `SET Z BACKLASH;0`. Reply `Z const offset: <v>` | yes | **ABSENT** |
| `GET Z PLATE OFFSET` / `SET Z PLATE OFFSET;{0}` | Read/write Z plate (zero-plate) offset | yes (firmware ≥ 01.02.02 only) | **ABSENT** |

> **Correction.** Earlier revisions gave the index reply as `cfg read index :<value>`.
> The firmware format is `cfg read index : %d` (a space on both sides of the colon)
> and, like `spindle distance: %.2f`, it has no newline — it ends at the NUL
> padding. Earlier revisions also said the cage reply "contains `CAGE STATUS` +
> `CLOSED`/`OPEN`" on an unspecified connection; on 01.02.00 the `GET` reply goes
> to the requesting connection, unpadded, and the `CLOSED` form has no newline.

### Jog (manual movement)
| Wire string | Meaning | 01.02.00 |
|---|---|---|
| `ENTER JOG` / `EXIT JOG` | Enter / leave jog mode. Robot acks `INFO;35` (jog mode) | present |
| `ENABLE MANUAL JOG` / `DISABLE MANUAL JOG` | Zero motor torque so the robot can be pushed by hand / restore torque | present |
| `GOTO TARGET;{0};{1};{2};{3}` | Move to `x;y;Z;angle` (app sends Z=0). Done → `INFO;32` "Target reached" | present |
| `JOG STOP GOLIATH` | Stop a jog move in progress | present |
| `FREE Z AXIS` | Pull the Z axis up | present |

### Spindle / dust / work
| Wire string | Meaning | Used | 01.02.00 |
|---|---|---|---|
| `ENABLE SPINDLE` / `DISABLE SPINDLE` | Spindle power on/off | yes (present 1.09.05 & 1.9.3.0; absent 1.9.1.0 — see v-compat) | present |
| `ENABLE ASPIRATION` / `DISABLE ASPIRATION` | Dust extraction on/off | yes (same availability) | present |
| `GCODE UPLOAD;{0}` | Begin G-code upload, `{0}` = file size in bytes; then stream the file. On 01.02.00 this switches the command parser off until the upload completes ([03](03-session-sequence.md)) | yes | present |
| `GCODE CONTINUE;{0}` | 1 = resume previously loaded G-code, 0 = discard/wait for new. App sends only `;0`, before each fresh upload (firmware ≥ 01.02.00) | yes | **ABSENT** |
| `ABORT WORK` | Abort current job back to work-cycle-finish (app gates to firmware > 01.02.00) | yes (firmware > 01.02.00 only) | **ABSENT** |
| `SEND TCP POS` | Request one position frame (id 1000) | yes (only once a panel is detected) | **ABSENT** |
| `SET SETUP PHASE;{0}` | Jump the setup FSM to a phase id | no (firmware self-advances) | present |
| `SET WORK PHASE;{0}` | Jump the work FSM to a phase id | no | present |
| `ENABLE WORK MON;{0}` | Enable/disable periodic position streaming (1/0) | no (robot streams anyway) | present |

### Firmware (Wi-Fi flash path — see [06-firmware.md](06-firmware.md))
| Wire string | Meaning | 01.02.00 |
|---|---|---|
| `LOAD;<lineCount>;` | Enter hex-receive mode for an over-Wi-Fi flash; then stream Intel-HEX records. On 01.02.00 the reply `\n! Waiting for a hex file with %d lines \r\n` goes to the requester. **Never send this outside a deliberate, prepared flash.** | present |

### Commands in the firmware that the vendor app never uses

GFW 01.02.00 also contains about fifty commands the vendor app does not know at
all — among them direct motor and stepper control (`MOVE Z`, `RUN STEPPER`,
`START MOTORS`, …), spindle and dust switches (`SPINDLE ON`, `ASP ON`, …), job
control (`GCODE START`, `GCODE STOP`, `GCODE RESTART`), and flash/parameter writes
(`EFLASH ERASE`, `SET PARAM MEMORY`, `RESET PARAM MEMORY`). Their semantics are
unknown. **Never send them.** The full list, with hazards, is in
[09-firmware-01.02.00.md](09-firmware-01.02.00.md).

## The `INFO;0` acknowledgement

> **Correction.** Earlier revisions listed `INFO;0` ("OK") without saying when it
> is sent. On GFW 01.02.00 it is the robot's **generic acknowledgement**.

- Most command handlers send `INFO;0` when they run — on the **second**
  connection, like every `INFO`. Several send `ERR;1` (`CMD_NOT_EXECUTED`) instead
  when a precondition fails. The per-command list is in
  [09](09-firmware-01.02.00.md#the-command-table).
- `INFO;0` is sent by the individual handler, not by the dispatcher, so it is
  **not** universal. Statically, no `INFO;0` comes from: `INFO`, `PRINT SYS REPORT`,
  `GET SSIDS`, `GCODE UPLOAD` (the header), `FREE Z AXIS`, `JOG STOP GOLIATH`,
  `LOAD`, unknown commands, and the table entries whose handler is empty. (A
  subroutine the handler calls might still send something; that was not traced.)
- When a G-code upload finishes receiving, the firmware leaves upload mode and
  sends `INFO;0`.
- `INFO;0` carries no command name. With the pacing rule (one command at a time,
  ≈0.3 s apart) it can be attributed to the most recent command, but treat that as
  inference.

## Responses

### INFO (robot → PC): `INFO;<code>;<params…>`
Phase/notification messages. Codes are the **wire** values (note the gaps —
29,30,36–39 are unused). All go to the **second** connection.

| Code | Symbol | Text / params |
|---|---|---|
| 0 | OK | `OK` — the generic acknowledgement (above) |
| 1 / 2 | ENTER/EXIT_SETUP | entering / exiting setup |
| 3 / 4 | ENTER_PAIRING / PAIRING_DONE | pairing procedure start / done |
| 5 / 6 | ENTER / DONE PANEL_DETECTION | panel detection start / done |
| 7 / 8 / 9 | ENTER / DONE / EXIT GCODE_UPLOAD | **8 = "GCODE file received correctly"** (upload success). Whether 01.02.00 sends 8 at the end of an upload is not established (see [07](07-version-compatibility.md)) |
| 10 / 11 | ENTER/EXIT DRILL_CAL | drill calibration |
| 12 / 14 | ENTER/EXIT DRILL_ZERO_CHECK | drill zero check *(id 14's text wrongly duplicates id 12's — cosmetic)* |
| 13 | ZERO_VALUE_SET | "Z zero value set. Z Axis calibration complete." |
| 15 / 16 | ENTER/EXIT MODE_WORK | work mode |
| 17 / 18 | ENTER/EXIT WORK_CYCLE | work cycle |
| 19 / 20 | ENTER/EXIT WORK_PAUSED | work paused |
| 21 / 22 | ENTER/EXIT WORK_HALTED | work halted |
| 23 | WORK_COMPLETE | "work complete." (job done) |
| 24 | ENCODER_CALIBRATED | "encoder calibration done." |
| 25 | VERT | a detected panel vertex. Vendor table: `INFO;25;x;y;z`. **On 01.02.00** the format is `INFO;25;%.4f;%.4f` — two values |
| 26 | PANEL_SIZE | `INFO;26;x_max;y_max` |
| 27 | TWRS_DISTANCE | `INFO;27;distance` (01.02.00: `%.4f`) |
| 28 | SYS_INFO | `INFO;28; Project name:[…] Version:[…] Build date:[…] Build time:[…]` |
| 31 | AP | `INFO;31;<AP>` — streamed SSID scan (`START` / `<ssid>` / `END`) |
| 32 | JOG_TARGET_REACHED | "Target reached" |
| 33 | NEED_ENCODER_CALIBRATION | encoder needs calibration |
| 34 | APP_INFO | `INFO;34;<prj_name>;<HW rev>;<SW rev>;<SN>`. 01.02.00 format: `INFO;%d;%s;%d;%d;%04X%04X%04X` — `prj_name` is `GFW 01.02.00` padded with spaces, HW/SW are decimal, SN is 12 hex digits |
| 35 | JOG_MODE | robot is in jog phase |
| 40 | SANITY_CHECK | `INFO;40;{b1;…;b15};END` — reply to `PRINT SYS REPORT` |
| 41 | ASK_GCODE_RESTART | "Ask to resume previous GCODE or to start new" (after a reset) |
| 42 | PANEL_SIZE_NOT_RECEIVED | panel size missing |
| 43 | Z_CALIBRATION_FINISHED | Z calibration finished |
| 1000 | SMART_INFO_POS | binary position frame (see framing doc) — synthetic id, never on the wire |
| 1001 | SMART_INFO_TWR | binary tower-status frame — synthetic id |
| 1002 | SMART_INFO_GLOBAL_STATUS | binary global-status frame — synthetic id; 01.02.00 sends the 48-byte blob instead (below) |

(On 01.02.00, code 37 is used internally: asking `send_info` for code 37 re-sends
the most recent `INFO` code instead. It never appears as `INFO;37`.)

### ERROR (robot → PC): `ERR;<code>`
Sent to the **second** connection. 01.02.00 formats: `ERR;%d` and `ERR;%d;%d`.

| Code | Symbol | Meaning |
|---|---|---|
| 0 | GCODE_UPLOAD | error during G-code upload |
| 1 | CMD_NOT_EXECUTED | robot could not handle a PC command (on 01.02.00: a handler's precondition failed — not "unknown command", which gets no reply) |
| 2 | CAGE_OPENED | spindle cover opened (during jog or work) — **safety** |
| 3 / 4 | T1 / T2 DISCONNECTED | lost connectivity to tower 1 / 2 |
| 5 | REFERENCE_DIVERGENCE | positioning reference computation diverged |
| 6 / 7 | T1 / T2 WRONG_LENGTH | tower wire length out of range (<0 or > max) |
| 8 | SUM_TWRS_LESS_THAN_DISTANCE | T1+T2 lengths < their distance → positioning impossible |
| 9 | POS_OVER_THRESH | reference-vs-actual track error over threshold — **safety** |
| 10 | MOTOR_STALL | a motor stalled (logged, not shown to user) |
| 11 | CANNOT_LIFT_Z | spindle can't move freely (would hit a tower cable) — **safety** |
| 12 | DRILL_NOT_COMPLIANT | measured bit length off-spec after Z calibration |
| 13 | MOTOR_OCD | motor over-current |
| 14 | MOTOR_THERMAL_WARN | motor driver over temperature (≈100 °C) |
| 15 | MOTOR_NOT_CALIBRATED | motor not calibrated |
| 16 | POS_OUT_OF_PANEL | reference position outside the panel — **safety** |
| 17 | GOLIATH_IN_HALT | robot halted |
| 18 | STALL_NOT_DETECTED | expected stall not seen |
| 19 | MESSAGE_NOT_CONSTRUCTED | robot failed to build a message |
| 20 | Z_OFFSET_VALUE | Z offset error |
| 21 | TEMPERATURE_HIGH | inside temperature too high — Slingshot warns of imminent shutdown |

> The shipped app pops up a generic dialog for most errors; it has **no specific
> handler** for `POS_OUT_OF_PANEL` (16) or `GOLIATH_IN_HALT` (17). A good
> replacement should handle the safety errors (2, 9, 11, 16) deliberately — and
> must have the second connection open, or it will never see them.

### WARNING (robot → PC): `WARN;<code>`
Sent to the **second** connection. 01.02.00 format: `WARN;%d`.

| Code | Meaning |
|---|---|
| 0 | Tower 1 battery < 20 % |
| 1 | Tower 2 battery < 20 % |
| 2 | Tower 1 battery < 10 % |
| 3 | Tower 2 battery < 10 % |

### Global status

`GET GLOBAL SYS STATUS` on GFW 01.02.00 is answered with two things: `INFO;0` on
the second connection, and this **48-byte blob** sent directly to the connection
that asked (explicit length, no padding, no terminator):

| Offset | Length | Content |
|---|---|---|
| 0 | 19 | `GLOBAL STATUS HEAD` + one NUL byte |
| 19 | 10 | status bytes (below) |
| 29 | 19 | `GLOBAL STATUS TALE` + one NUL byte |

The ten status bytes are the low bytes of ten 32-bit fields of one firmware
status structure, written in this order:

| Blob offset | Field (byte offset in the status structure) | Name, by position in the vendor's `INFO;1002` template | Corroboration |
|---|---|---|---|
| 19 | +8 | `pairing` | value 3 makes the connect-time replay send `INFO;4` (pairing done) |
| 20 | +0 | `t1_paired` | position only |
| 21 | +4 | `t2_paired` | position only |
| 22 | +16 | `pd_status` | value 3 makes the replay send `INFO;6` (panel detection done) |
| 23 | +12 | `panel_detected` | position only |
| 24 | +20 | `gcode_uploaded` | non-zero makes the replay send `INFO;8` (G-code received) |
| 25 | +24 | `work_status` | position only |
| 26 | +28 | `conn_network` | position only |
| 27 | +32 | `resume_after_reset` | position only |
| 28 | +36 | `encoder_calibrated` | position only |

- **Naming is an assumption.** The firmware writes ten fields; the vendor's text
  template names ten status fields. Matching them by position is supported for
  three of the ten by the connect-time replay, which tests the same structure
  fields. The value encodings beyond those three are unknown. Capture the blob in
  different machine states to confirm.
- There are **no tower firmware versions** in this blob. The vendor app never
  decoded it (its detector expects the 19-byte `G…S` frame and hard-codes the
  status fields), so on 01.02.00 Slingshot never showed real values here either.
- On other firmware the reply may be the 19-byte `G…S` frame described in
  [01](01-transport-and-framing.md#frame-3--global-status-smart_info_global_status-id-1002).
  Decode both.

### Free-form replies (not `INFO;<n>`)

These are plain text, matched by prefix. On GFW 01.02.00:

| Reply to | Text | Connection | Terminator |
|---|---|---|---|
| `READ INDEX CALIBRATION` | `cfg read index : <int>` | first (slot 0), padded to 512 | **none** — ends at the NUL padding |
| `READ SPINDLE CALIB` | `spindle distance: <%.2f>` | first (slot 0), padded | **none** — NUL padding |
| `GET SPINDLE CAGE STATUS` | `CAGE STATUS: OPEN` / `CAGE STATUS: CLOSED` | requester, unpadded | `\n` after `OPEN`; **none** after `CLOSED` (cut off by a 20-byte buffer) |
| `POLL CAGE` (500 ms timer) | `CAGE STATUS: <OPEN\|CLOSED>` | first (slot 0), padded | `\n` |
| `GET PANEL SIZE` | `xmax=<%.4f> \n\rymax=<%.4f>` | requester, unpadded | **none** |
| `GET Z BACKLASH` | `Z const offset: <v>` (vendor parser) | — | command absent on 01.02.00 |

## Caveats
- `SET SETUP PHASE`, `SET WORK PHASE`, `SET VERTEX`, `GET VERTEXES`,
  `ENABLE WORK MON`, `TOWERS POSITION`, `GET TOWERS DISTANCE` are defined in the
  protocol but **never sent** by the 1.9.3.0 app. They exist on 01.02.00, but their
  behaviour is unvalidated by the vendor's own usage; the phase-jump commands in
  particular should never be sent.
- Every firmware statement here is from static analysis of GFW 01.02.00, not yet
  observed on a robot. Newer firmware existed; its command set is unknown beyond
  what the vendor app's version gates imply.
