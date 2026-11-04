# 02 — Message reference

*Source: `GoliathCommLibTCP/SocketManager.cs` static command/response tables,
Slingshot 1.9.3.0. Wire codes are taken verbatim from the registration arrays, not
from enum order. Verified against source.*

Conventions: `{0}`,`{1}`… are positional arguments joined with `;`. All text is
ASCII; integers are decimal; where the robot replies, the reply is listed. "Used
by 1.9.3.0 app" marks whether the shipped app ever actually sends it — several
commands exist in the table but are never issued (the firmware does that work
itself). A replacement can still send them if the firmware supports them, but
"no" means untested by the vendor app.

## Commands (PC → robot)

### Connection / system
| Wire string | Meaning | Used |
|---|---|---|
| `INFO` | Request firmware/app info → reply `INFO;34;<prj>;<HW>;<SW>;<SN>` | yes |
| `CMD SYS RESET` | Reboot the robot | yes |
| `GET GLOBAL SYS STATUS` | Request global status → binary `G…S` frame (id 1002); also used as ~60 s keep-alive | yes |
| `PRINT SYS REPORT` | Sanity self-check → `INFO;40;{b1…b15};END` (0=fail,1=ok) | yes |
| `ENABLE INFO LOG;{0}` | Enable/disable INFO stream during setup/work (1/0) | — |
| `GET SSIDS` | Robot scans Wi-Fi → streams `INFO;31;START` / `INFO;31;<ssid>` / `INFO;31;END` | yes |
| `{0}` | Raw pass-through of a custom string (eCMD `NONE`) | — |

### Wi-Fi provisioning (sent as raw strings, not in the eCMD table)
| Wire string | Meaning |
|---|---|
| `SSID=<hexSSID>&PWD=<hexPWD>&AUTH_TYPE=<1\|2\|3>` | Give the robot a network to join. SSID/PWD are per-character uppercase hex. AUTH_TYPE 1=Open, 2=WPA/WPA2-PSK, 3=WEP |
| `RESET NET KEY` | Legacy (firmware < 01.00.14) refresh of the SSID list |

### Tower pairing & geometry
| Wire string | Meaning | Used |
|---|---|---|
| `SET RF CHANNELS;{0};{1}` | Set the two towers' RF channels (T1,T2). Values from a `CONFIG_n` pair in `Slingshot.ini` | yes |
| `SET T2 POSITION;{0}` | Second tower axis: 1 = T2 along X, 0 = along Y | yes |
| `DISCARD TWRS DISTANCE` | Discard measured inter-tower distance; reset panel-detection FSM to the distance step | yes |
| `TOWERS POSITION;{0};{1};{2};{3}` | Give tower coords `x1;y1;x2;y2` so robot computes distance | no (defined only) |
| `GET TOWERS DISTANCE` | Read inter-tower distance (app instead reads pushed `INFO;27`) | no (defined only) |

### Panel & calibration
| Wire string | Meaning | Used |
|---|---|---|
| `SET PANEL SIZE;{0};{1}` | Working panel size `x_max;y_max` (mm) — the only way the app sends the panel | yes |
| `GET PANEL SIZE` | Read panel size → `INFO;26;x_max;y_max` | no |
| `SET VERTEX;{0};{1}` | Send one panel-polygon vertex `length;angle` | no (robot emits vertices instead) |
| `GET VERTEXES` | Read panel vertex list | no |
| `READ INDEX CALIBRATION` | Read encoder index → reply `cfg read index :<value>` | yes |
| `SET INDEX CALIBRATION VALUE;{0}` | Write encoder index (app only sends when `27000 < n < 30100`) | yes |
| `READ SPINDLE CALIB` | Read spindle distance → reply `spindle distance: <v>` | yes |
| `SET SPINDLE DISTANCE;{0}` | Write spindle/center-bit distance | yes |
| `GET SPINDLE CAGE STATUS` | Read spindle-cover state → reply contains `CAGE STATUS` + `CLOSED`/`OPEN` | yes |
| `POLL CAGE` / `STOP POLL CAGE` | Legacy continuous cage polling (firmware < 01.00.14) | yes (legacy) |
| `GET Z BACKLASH` / `SET Z BACKLASH;{0}` | Read/write constant Z offset (backlash); reset = `SET Z BACKLASH;0`. Reply `Z const offset: <v>` | yes |
| `GET Z PLATE OFFSET` / `SET Z PLATE OFFSET;{0}` | Read/write Z plate (zero-plate) offset | yes |

### Jog (manual movement)
| Wire string | Meaning |
|---|---|
| `ENTER JOG` / `EXIT JOG` | Enter / leave jog mode. Robot acks `INFO;35` (jog mode) |
| `ENABLE MANUAL JOG` / `DISABLE MANUAL JOG` | Zero motor torque so the robot can be pushed by hand / restore torque |
| `GOTO TARGET;{0};{1};{2};{3}` | Move to `x;y;Z;angle` (app sends Z=0). Done → `INFO;32` "Target reached" |
| `JOG STOP GOLIATH` | Stop a jog move in progress |
| `FREE Z AXIS` | Pull the Z axis up |

### Spindle / dust / work
| Wire string | Meaning | Used |
|---|---|---|
| `ENABLE SPINDLE` / `DISABLE SPINDLE` | Spindle power on/off | yes (present 1.09.05 & 1.9.3.0; absent 1.9.1.0 — see v-compat) |
| `ENABLE ASPIRATION` / `DISABLE ASPIRATION` | Dust extraction on/off | yes (same availability) |
| `GCODE UPLOAD;{0}` | Begin G-code upload, `{0}` = file size in bytes; then stream the file | yes |
| `GCODE CONTINUE;{0}` | 1 = resume previously loaded G-code, 0 = discard/wait for new. App sends only `;0`, before each fresh upload (firmware ≥ 01.02.00) | yes |
| `ABORT WORK` | Abort current job back to work-cycle-finish (app gates to firmware > 01.02.00) | yes |
| `SEND TCP POS` | Request one position frame (id 1000) | yes |
| `SET SETUP PHASE;{0}` | Jump the setup FSM to a phase id | no (firmware self-advances) |
| `SET WORK PHASE;{0}` | Jump the work FSM to a phase id | no |
| `ENABLE WORK MON;{0}` | Enable/disable periodic position streaming (1/0) | no (robot streams anyway) |

### Firmware (Wi-Fi flash path — see [06-firmware.md](06-firmware.md))
| Wire string | Meaning |
|---|---|
| `LOAD;<lineCount>;` | Enter hex-receive mode for an over-Wi-Fi flash; then stream Intel-HEX records |

## Responses

### INFO (robot → PC): `INFO;<code>;<params…>`
Phase/notification messages. Codes are the **wire** values (note the gaps —
29,30,36–39 are unused).

| Code | Symbol | Text / params |
|---|---|---|
| 0 | OK | `OK` |
| 1 / 2 | ENTER/EXIT_SETUP | entering / exiting setup |
| 3 / 4 | ENTER_PAIRING / PAIRING_DONE | pairing procedure start / done |
| 5 / 6 | ENTER / DONE PANEL_DETECTION | panel detection start / done |
| 7 / 8 / 9 | ENTER / DONE / EXIT GCODE_UPLOAD | **8 = "GCODE file received correctly"** (upload success) |
| 10 / 11 | ENTER/EXIT DRILL_CAL | drill calibration |
| 12 / 14 | ENTER/EXIT DRILL_ZERO_CHECK | drill zero check *(id 14's text wrongly duplicates id 12's — cosmetic)* |
| 13 | ZERO_VALUE_SET | "Z zero value set. Z Axis calibration complete." |
| 15 / 16 | ENTER/EXIT MODE_WORK | work mode |
| 17 / 18 | ENTER/EXIT WORK_CYCLE | work cycle |
| 19 / 20 | ENTER/EXIT WORK_PAUSED | work paused |
| 21 / 22 | ENTER/EXIT WORK_HALTED | work halted |
| 23 | WORK_COMPLETE | "work complete." (job done) |
| 24 | ENCODER_CALIBRATED | "encoder calibration done." |
| 25 | VERT | `INFO;25;x;y;z` — a detected panel vertex |
| 26 | PANEL_SIZE | `INFO;26;x_max;y_max` |
| 27 | TWRS_DISTANCE | `INFO;27;distance` |
| 28 | SYS_INFO | `INFO;28; Project name:[…] Version:[…] Build date:[…] Build time:[…]` |
| 31 | AP | `INFO;31;<AP>` — streamed SSID scan (`START` / `<ssid>` / `END`) |
| 32 | JOG_TARGET_REACHED | "Target reached" |
| 33 | NEED_ENCODER_CALIBRATION | encoder needs calibration |
| 34 | APP_INFO | `INFO;34;<prj_name>;<HW rev>;<SW rev>;<SN>` — `prj_name` carries `GFW 01.02.00` |
| 35 | JOG_MODE | robot is in jog phase |
| 40 | SANITY_CHECK | `INFO;40;{b1;…;b15};END` — reply to `PRINT SYS REPORT` |
| 41 | ASK_GCODE_RESTART | "Ask to resume previous GCODE or to start new" (after a reset) |
| 42 | PANEL_SIZE_NOT_RECEIVED | panel size missing |
| 43 | Z_CALIBRATION_FINISHED | Z calibration finished |
| 1000 | SMART_INFO_POS | binary position frame (see framing doc) |
| 1001 | SMART_INFO_TWR | binary tower-status frame |
| 1002 | SMART_INFO_GLOBAL_STATUS | binary global-status frame |

### ERROR (robot → PC): `ERR;<code>`
| Code | Symbol | Meaning |
|---|---|---|
| 0 | GCODE_UPLOAD | error during G-code upload |
| 1 | CMD_NOT_EXECUTED | robot could not handle a PC command |
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
> replacement should handle the safety errors (2, 9, 11, 16) deliberately.

### WARNING (robot → PC): `WARN;<code>`
| Code | Meaning |
|---|---|
| 0 | Tower 1 battery < 20 % |
| 1 | Tower 2 battery < 20 % |
| 2 | Tower 1 battery < 10 % |
| 3 | Tower 2 battery < 10 % |

## Caveats
- `SET SETUP PHASE`, `SET WORK PHASE`, `SET VERTEX`, `GET VERTEXES`,
  `ENABLE WORK MON`, `TOWERS POSITION`, `GET TOWERS DISTANCE` are defined in the
  protocol but **never sent** by the 1.9.3.0 app. They may work on the firmware,
  but are unvalidated by the vendor's own usage.
- Several replies (`READ INDEX CALIBRATION`, `READ SPINDLE CALIB`, `GET Z BACKLASH`,
  cage status) are **free-form text lines**, not `INFO;<n>` — parse them as
  substrings, as the app does.
