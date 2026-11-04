# 09 — Firmware GFW 01.02.00: command table and output behaviour

*Source: static disassembly of the robot application firmware **GFW 01.02.00**
(`app/Firmware/Latest.hex`, converted to the flat binary `firmware_app.bin`).
Compiled 2026-11-04 from an adversarial review in which independent reviewers and
verifiers disassembled the image, plus a further pass for this document. Nothing
here has been observed on a live robot yet.*

The other documents in this folder were reconstructed from the vendor's Windows
app, i.e. from what the **host** sends and expects. This document records what the
**robot firmware** does with it: which commands exist, how a command is parsed,
and where each kind of output is sent. Where this contradicts an earlier,
host-side statement, the firmware wins, and the other documents have been
corrected to point here.

## Method

- **Image.** `Latest.hex` is byte-identical in Slingshot 1.09.05, 1.9.1.0 and
  1.9.3.0 ([06](06-firmware.md)). Converted to a flat binary, it is 153,764 bytes
  loaded at **`0x08008000`** (STM32 flash, after a 32 KB resident loader that is
  not in the image). The version string is `GFW 01.02.00`.
- **Analysis.** Static disassembly of the ARM Cortex-M **Thumb-2** code with
  Capstone, following calls, literal pools and the command dispatch table by hand
  and with small scripts. No emulation; no hardware.
- **Addresses** are absolute, as the CPU sees them: code addresses are Thumb
  addresses without the Thumb bit, equal to file offset + `0x08008000`. Some
  review notes quote file offsets; add `0x08008000` to compare. `0x2000xxxx`
  addresses are RAM.
- **Confidence.** Unmarked statements were reproduced by at least two independent
  passes. Statements marked **†** come from a single pass (this document's own
  analysis) and should be re-checked before code depends on them.
- **Clean room.** This document describes interface behaviour in our own words,
  with addresses as evidence. It reproduces no firmware code.

## How a command is parsed

1. The WINC1500 socket callback (`0x080241b0`) receives an event for one socket.
   For a receive event with more than 0 bytes it calls the registered receive
   handler, then sends any pending **direct reply** back to that same socket
   (`0x08024362`). If the receive returned ≤ 0 bytes, it closes the socket and
   frees its slot (`0x080243ac`–`0x080243b4`).
2. The receive handler (`0x08023c74`) routes the buffer to the Wi-Fi
   firmware-load record handler (`0x0800eb74`) if that mode is active (RAM flag
   `0x20000440`); otherwise it runs the per-command state wrapper (`0x0800b8c0`)
   and calls the **command parser** `0x0800bf14`. It always returns 1 ("keep
   receiving"), so the parser's own result is discarded. There is no filter on
   which connection the buffer came from.
3. The parser (`0x0800bf14`):
   - clears the 1024-byte direct-reply buffer (`0x20001a09`);
   - copies the received bytes to `0x20000608` and NUL-terminates them;
   - if the **G-code upload flag** (`0x2000000c`) is set, hands the whole buffer
     to the upload path `0x0800cf56` instead of parsing it (see
     [G-code upload mode](#g-code-upload-mode)) †;
   - otherwise takes the text before the first `;` as the name
     (`0x0800bf58`–`0x0800bf66`), upper-cases it (`0x0800d038`), and compares it
     with exact `strcmp` (`0x080082c2`) against the 94 table records
     (loop `0x0800bf7e`–`0x0800bfaa`);
   - **no match** → `0x0800bfb2` branches to `0x0800cf8a`, which restores the
     state and returns: **no reply, no effect**;
   - **match at index 4–87** → TBH jump table at `0x0800bfc0` to the handler;
   - **match at index 0–3 or 88–93** → straight to the common tail: no effect.
4. The common tail (`0x0800cf2e`) queues the direct-reply buffer for the
   requesting socket through `set_response` (`0x080246dc`): with the explicit
   length a handler set (`[sp,#0x200]` flag), or else with its `strlen`.
   `set_response` copies at most 1024 bytes and **does nothing for length 0** †,
   so commands that write no direct reply send nothing to the requester. Then
   `0x0800cf8a` restores the per-command state (`0x0801b480`).

Consequences, all covered in [01](01-transport-and-framing.md): one receive buffer
is one command; a terminator (`\n`, `\r\n`) or a second command in the same buffer
makes the name unknown; command names are case-insensitive; unknown commands are
silently ignored (statically — confirm on hardware).

## Where output goes

### Client slots

- The slot table is 6 signed bytes at RAM `0x20004236`; `-1` means empty.
- `add_client` (`0x08023e88`), called only from the accept path (`0x08024238`),
  stores each accepted socket in the **first free slot**. If none is free, the
  new socket is closed (`0x080242ac`).
- `remove_client` (`0x08024104`) frees a slot when a receive returns ≤ 0 bytes,
  and when sending a direct reply fails (`0x0802436e`–`0x08024376`). A close-all
  routine is at `0x08023ec8`.
- Only these routines write the table.

### Senders

`send_slot(idx)` (`0x0800d0fc`) looks up `table[idx]` (`0x08024710`). If the slot
holds a socket it sends **512 bytes** from the shared buffer `0x2000534c`, which
every caller first zeroes and then formats with `snprintf`. If the slot is empty
the message is **dropped**; there is no fallback.

Every caller of `send_slot`, and the slot it passes:

| Message | Built / sent at | Slot |
|---|---|---|
| `INFO;34;%s;%d;%d;%04X%04X%04X\n` (identity) | builder `0x08009c84`, send `0x08009cb6` | 1 |
| `INFO;%d\n` (`send_info`, `0x08013dd0`) | `0x08013df6`; code 37 is special: re-sends the last code, `0x08013e0e` | 1 |
| `INFO;40;{15 × %d};END\n` (sanity report) | `0x08013e3c`, send `0x08013ec4` | 1 |
| `INFO;31;START\n` / `INFO;31;%s\n` / `INFO;31;END\n` (SSID scan) | `0x0801c714` / `0x0801c6a8` / `0x0801c6dc` | 1 |
| `INFO;25;%.4f;%.4f\n` (panel vertex) † | `0x080238e0` | 1 |
| `INFO;27;%.4f\n` (tower distance) † | `0x0800d326` | 1 |
| `ERR;%d\n` (`send_err`, `0x0800dcf4`) / `ERR;%d;%d\n` | `0x0800dd12` / `0x0800dd46` | 1 |
| `WARN;%d\n` | `0x08023922` | 1 |
| free-form `printf` (`tcp_printf`, `0x0800d0cc`) | `0x0800d0ec` | 0 |
| 22-byte position frame (builder `0x0801c550`) | `0x0801a678` | 2 |

Not through `send_slot`:

| Message | Where | Destination | Size |
|---|---|---|---|
| tower frame `<` id status `>` — id `0xAA` (T1, builder `0x0801ffec`) or `0xBB` (T2, builder `0x080200e6`); battery = `(byte & 0x3F) × 1.5625` (`0x0801ff30`–`0x0801ff44`) | slot-0 lookup `0x0801ff0e`, send `0x0802000e` | slot 0 | exactly 4 bytes |
| direct reply | `set_response` `0x080246dc`, sent at `0x08024362` | the socket the command came from | explicit length, ≤ 1024 |

The position frame is `{`, 20 bytes copied from the position record (X, Y, Z,
angle as float32; G-code line as int32), and `}`. The angle is converted from
radians to degrees before sending (constants near `0x0801c5dc`). No 58-byte
builder was found.

### Free-form text on slot 0

All are sent through `tcp_printf`, padded to 512 bytes. **Only the cage-status
line ends in `\n`**; every other one ends at the NUL padding.

| Text | Call site | Produced by |
|---|---|---|
| `cfg read index : %d` | `0x0800c434` | `READ INDEX CALIBRATION` |
| `spindle distance: %.2f` | `0x0800c1c4` | `READ SPINDLE CALIB` |
| `CAGE STATUS: %s\n` (`OPEN`/`CLOSED`) † | `0x0800b8ee` | callback `0x0800b8d8`, run every 500 ms by a timer that `POLL CAGE` starts (`0x0800cefc`) and that one other internal path also starts (`0x0800ba20`) |
| `ENC Test Started` | `0x0800c1a0` | `PRINT ENCODER CAN` |
| `Recived Parameters are INVALID` / `Successfull Store of Parameters` (sic) | `0x0800c154` / `0x0800c15c` | `SET PARAM MEMORY` |
| `RESTART GCODE` | `0x0800a99c` | internal |
| `\n\rindex position:%d \n\rstop position:%d \n\rcalibration value:%d` | `0x0800d6cc` | internal (encoder calibration) |
| `Shutting down system...` | `0x0801b672`, `0x0801b698` | internal |
| `Drill length: %.2f mm` | `0x0801fa18` | internal (drill calibration) |
| `\n\rt1 pkts : %.2f%%\n\rt2 pkts : %.2f%%` | `0x0801faa6` | internal (radio statistics) |

### Direct replies to the requester

Written into the reply buffer `0x20001a09` by the handler and sent by the common
tail, unpadded, with no terminator added.

| Command | Reply | Evidence |
|---|---|---|
| `GET GLOBAL SYS STATUS` | the 48-byte `GLOBAL STATUS` blob (below) | builder `0x0802697c` called at `0x0800c07e`; explicit-length flag set at `0x0800c084` |
| `GET SPINDLE CAGE STATUS` | `CAGE STATUS: OPEN\n` (18 bytes) or `CAGE STATUS: CLOSED` (19 bytes) † | `snprintf(reply, 20, "CAGE STATUS: %s\n", …)` at `0x0800c17e`–`0x0800c186`; with `CLOSED` the full text is 20 characters, so the `\n` is cut off |
| `GET PANEL SIZE` | `xmax=%.4f \n\rymax=%.4f` | `0x0800ccca`–`0x0800ccd6` |
| `GCODE READ` | `count: %d` | `0x0800c722`–`0x0800c72c` |
| `MOVE STEPPER` | `Moving stepper!` | `0x0800cb48`–`0x0800cb50` |
| `LOAD` | `\n! Waiting for a hex file with %d lines \r\n` | `0x0800c09e`–`0x0800c0a8` |

> **Note for implementers.** The review brief placed `CAGE STATUS: %s\n` on slot 0
> for the `GET SPINDLE CAGE STATUS` command. The `GET` handler formats its own
> copy of the string into the direct-reply buffer instead †. Only the polling
> timer uses slot 0. With the vendor's connection order the requester and slot 0
> are both the first connection, so the reply arrives on the same connection
> either way; what differs is the shape (unpadded, and no `\n` after `CLOSED`).

### The `GLOBAL STATUS` blob

Builder `0x0802697c` (only caller `0x0800c07e`):

- `snprintf(buf, 19, "GLOBAL STATUS HEAD")` (`0x08026984`–`0x0802698a`): 18
  characters and a NUL;
- ten byte stores from the status structure at RAM `0x20004e44`, fields at byte
  offsets +8, +0, +4, +16, +12, +20, +24, +28, +32, +36
  (`0x08026990`–`0x080269e4`);
- `snprintf(buf + 29, 19, "GLOBAL STATUS TALE")` (`0x080269e8`–`0x080269ec`);
- length = 48 (`0x080269f0`–`0x080269f6`).

The handler also sends `INFO;0` (`0x0800c088`). No builder for the 19-byte `G…S`
frame exists: there is no `MOVS #0x47` / `MOVS #0x53` pair anywhere in the image.
Field names and corroboration: [02](02-message-reference.md#global-status).

### Connect and disconnect

- On accept, besides `add_client`, the firmware may raise a **state replay**
  (`0x08024252`–`0x080242a4`): only for the accepted socket whose WINC1500
  descriptor is 2, and only in some machine states. Its handler (`0x0802471c`)
  does nothing in machine state 3; otherwise it sends `INFO;34`, then `INFO;8` if
  status field +20 is non-zero, `INFO;6` if field +16 is 3, and `INFO;4` if field
  +8 is 3. A second event leads to `INFO;24`. All of these go to slot 1. Which TCP
  connection gets descriptor 2 depends on the WINC1500's socket numbering (not
  verified).
- Neither accept nor close moves the robot or changes its work state; the close
  path only frees slots and clears the connected flag (`0x0802410c`,
  `0x0802413c`).

## G-code upload mode

† (single pass; the review attributed the same flag to `LOAD`.)

- The `GCODE UPLOAD` handler (`0x0800cd40`) sets the flag `0x2000000c` to 1, reads
  the size argument, prepares program storage (`0x0800f4a0`, `0x0800f530`) and
  clears a chunk counter (`0x2000000e`). It sends no reply of its own.
- While the flag is set, the parser (`0x0800bf4e`–`0x0800bf54`) passes **every
  received buffer, from any connection,** to `0x0800cf56`, which stores it
  (`0x0800f510`, `0x0800f3ac`, `0x0800f500`) and counts it. When both end checks
  (`0x0800efd0`, `0x0800f4dc`) pass, it clears the flag and sends `INFO;0`
  (`0x0800cf7e`–`0x0800cf86`).
- The only literal references to the flag are that read (`0x0800bf4e`), the set
  (`0x0800cd42`) and the clear (`0x0800cf80`). An access through another base
  pointer cannot be excluded, but no timeout or abort path was found: an upload
  cut short leaves the parser off.
- Whether `INFO;7` or `INFO;8` accompany an upload on this firmware is **not
  established**. `send_info(8)` is called directly only in the connect-time
  replay (`0x0802473a`). A queued-notification routine (`0x080149c4`) is called
  with 8 at `0x08022034`, but it maps its codes through a table at `0x0802c1dd`
  that was not decoded.

## The command table

94 records of 48 bytes at **`0x0802ae16`**. Each record is a 2-byte prefix (`> `,
except record 88, whose prefix is `\r\n`) followed by a NUL-terminated name; the
parser compares from offset 2. Records 4–87 are dispatched through the TBH table
at `0x0800bfc4` (entry *i*−4; target = `0x0800bfc4` + 2 × halfword). Records 0–3
and 88–93 are matched but not dispatched.

The **Handler sends** column lists the `INFO`/`ERR` calls made directly in the
handler body (all to slot 1) and any slot-0 `printf` †. A handler may also send
messages from routines it calls; those were not traced. "INFO;0, ERR;1" means
`INFO;0` on success and `ERR;1` when a precondition fails. "(empty)" handlers are
a single branch to the common tail (`0x0800cc02`, `0x0800cebc`): on this firmware
they do nothing and reply nothing.

### 1. Commands the vendor app sends

26 commands. These are the ones a replacement may need. They change machine state
unless noted.

| # | Name | Handler | Handler sends | Vendor use / notes |
|---|---|---|---|---|
| 4 | ENABLE SPINDLE | `0x0800c60e` | INFO;0 | spindle on — **hazard** |
| 5 | DISABLE SPINDLE | `0x0800c61c` | INFO;0 | spindle off |
| 11 | LOAD | `0x0800c08e` | — (direct reply "Waiting for a hex file…") | Wi-Fi firmware flash — **never outside a prepared flash** |
| 23 | GCODE UPLOAD | `0x0800cd40` | — | starts upload mode |
| 29 | GOTO TARGET | `0x0800c646` | INFO;0 | jog move — **motion** |
| 30 | EXIT JOG | `0x0800c6dc` | INFO;0, ERR;1 | |
| 35 | SET PANEL SIZE | `0x0800cc18` | INFO;0 | |
| 45 | SET RF CHANNELS | `0x0800c524` | INFO;0 | |
| 46 | SET T2 POSITION | `0x0800c4b8` | INFO;0 | |
| 48 | RESET NET KEY | `0x0800c49e` | INFO;0 | legacy (firmware < 01.00.14) |
| 49 | ENABLE MANUAL JOG | `0x0800c488` | INFO;0 | zero motor torque |
| 50 | ENTER JOG | `0x0800c47c` | INFO;0 | |
| 51 | ENABLE ASPIRATION | `0x0800c62a` | INFO;0 | dust extraction on |
| 52 | DISABLE ASPIRATION | `0x0800c638` | INFO;0 | |
| 58 | SET INDEX CALIBRATION VALUE | `0x0800c404` | INFO;0 | writes calibration |
| 59 | JOG STOP GOLIATH | `0x0800cd8a` | — | stop a jog move |
| 60 | DISABLE MANUAL JOG | `0x0800c2ca` | INFO;0 | restore motor torque |
| 65 | READ INDEX CALIBRATION | `0x0800c42a` | INFO;0, printf → slot 0 | **read-only** (allowlist) |
| 66 | POLL CAGE | `0x0800cef4` | INFO;0 | legacy; starts the 500 ms cage-status timer |
| 67 | STOP POLL CAGE | `0x0800cf12` | INFO;0 | legacy |
| 70 | PRINT SYS REPORT | `0x0800c25e` | — (`INFO;40` from `0x08013e3c`) | **read-only** (allowlist) |
| 76 | READ SPINDLE CALIB | `0x0800c1ac` | INFO;0, printf → slot 0 | **read-only** (allowlist) |
| 79 | FREE Z AXIS | `0x0800cbe6` | — | raises Z — **motion** |
| 82 | GET SSIDS | `0x0800c18c` | — (`INFO;31` stream) | Wi-Fi scan |
| 83 | GET SPINDLE CAGE STATUS | `0x0800c168` | INFO;0 + direct reply | **read-only** (allowlist); firmware ≥ 01.00.14 |
| 86 | GET GLOBAL SYS STATUS | `0x0800c07a` | INFO;0 + 48-byte blob | **read-only** (allowlist); also the vendor keep-alive |

### 2. Present, but never sent by the vendor app

**Never send these** (the one exception is `INFO`, on the read-only allowlist).
Their behaviour has not been validated by anyone; names suggest hazards but prove
nothing.

#### 2a. In the vendor app's command table, never sent

The vendor table gives each a one-line meaning ([02](02-message-reference.md)).

| # | Name | Handler | Handler sends | Notes |
|---|---|---|---|---|
| 6 | INFO | `0x0800c732` | — (`INFO;34` from `0x08009c84`) | **read-only** (allowlist): formats `INFO;34` from constants |
| 31 | SET SETUP PHASE | `0x0800c7d6` | INFO;0 | jumps the setup state machine — **do not send** |
| 32 | SET WORK PHASE | `0x0800c7fe` | INFO;0 | jumps the work state machine — **do not send** |
| 33 | SET VERTEX | `0x0800c944` | INFO;0 | panel geometry write |
| 34 | GET VERTEXES | `0x0800c9a2` | INFO;0 | read-like by name |
| 36 | GET PANEL SIZE | `0x0800cc90` | INFO;0 + direct reply `xmax=…` | read-like by name |
| 37 | ENABLE WORK MON | `0x0800cd6a` | INFO;0 | telemetry switch |
| 38 | ENABLE INFO LOG | `0x0800cd90` | INFO;0 | telemetry switch (no vendor send site established) |
| 39 | TOWERS POSITION | `0x0800cdb0` | INFO;0 | tower geometry write |
| 40 | GET TOWERS DISTANCE | `0x0800ceac` | INFO;0 | read-like by name |

#### 2b. Unknown to the vendor app (semantics unknown)

48 commands. The hazard column is a reading of the **name only**, except where a
handler is empty on this firmware.

| # | Name | Handler | Handler sends | Hazard (by name) |
|---|---|---|---|---|
| 7 | SET RTC TIME | `0x0800c740` | INFO;0, ERR;1 | clock write |
| 8 | SET RTC DATE | `0x0800c870` | INFO;0, ERR;1 | clock write |
| 9 | GET RTC DATE | `0x0800c91c` | INFO;0 | read-like |
| 10 | BOOT | `0x0800cc02` (empty) | — | **boot/flash** — empty here, unknown elsewhere |
| 12 | READ BACKUP | `0x0800cc02` (empty) | — | **firmware backup** — empty here |
| 13 | EFLASH ERASE | `0x0800c92e` | INFO;0, ERR;1 | **erases external flash** |
| 14 | MOVE Z | `0x0800cb7c` | INFO;0, ERR;1 | **motion** |
| 15 | RUN STEPPER | `0x0800c9d6` | INFO;0, ERR;1 | **motion** |
| 16 | STOP STEPPER | `0x0800ca4a` | INFO;0, ERR;1 | **motion** |
| 17 | GOTO POS STEPPER | `0x0800ca8c` | INFO;0, ERR;1 | **motion** |
| 18 | START MOTORS | `0x0800c56a` | INFO;0 | **motion** |
| 19 | MOVE STEPPER | `0x0800caf0` | ERR;1 + direct reply `Moving stepper!` | **motion** |
| 20 | SET STEP CLOCK MODE | `0x0800cce2` | INFO;0, ERR;1 | **motor driver configuration** |
| 21 | SPINDLE START | `0x0800cc02` (empty) | — | **spindle** — empty here |
| 22 | SPINDLE STOP | `0x0800cc02` (empty) | — | spindle — empty here |
| 24 | GCODE START | `0x0800cbec` | INFO;0, ERR;1 | **job control — may start cutting** |
| 25 | GCODE STOP | `0x0800cc04` | INFO;0 | **job control** |
| 26 | GCODE RESTART | `0x0800cc10` | INFO;0 | **job control — may start cutting** |
| 27 | GCODE READ | `0x0800c71c` | direct reply `count: %d` | read-like |
| 28 | ZERO SET | `0x0800cb66` | INFO;0 | **sets a zero/reference** |
| 41 | SET START POS | `0x0800cebc` (empty) | — | empty here |
| 42 | GET START POS | `0x0800cc02` (empty) | — | empty here |
| 43 | WORK SPEED MOD | `0x0800cebe` | INFO;0 | **changes work speed** |
| 44 | REQ UPD POS | `0x0800cc02` (empty) | — | empty here |
| 47 | BTN CLICK | `0x0800c4ac` | INFO;0 | **simulates a button press — may advance the state machine** |
| 53 | ENTER ENCODER CALIBRATION | `0x0800cc02` (empty) | — | empty here |
| 54 | EXIT ENCODER CALIBRATION | `0x0800cc02` (empty) | — | empty here |
| 55 | SCAN WIFI | `0x0800c440` | INFO;0 | radio |
| 56 | ENTER MANUAL ENC CALIBRATION | `0x0800c3ec` | INFO;0 | **calibration mode** |
| 57 | EXIT MANUAL ENC CALIBRATION | `0x0800c3f8` | INFO;0 | calibration mode |
| 61 | SPINDLE ON | `0x0800c296` | INFO;0 | **spindle on** |
| 62 | SPINDLE OFF | `0x0800c2a2` | INFO;0 | spindle |
| 63 | ASP ON | `0x0800c2ae` | INFO;0 | dust extraction |
| 64 | ASP OFF | `0x0800c2bc` | INFO;0 | dust extraction |
| 68 | ENTER TEST PHASE | `0x0800c264` | INFO;0 | **test mode** |
| 69 | EXIT TEST PHASE | `0x0800c27c` | INFO;0 | test mode |
| 71 | POLL ENCODER | `0x0800c240` | — | periodic output |
| 72 | STOP POLL ENCODER | `0x0800c256` | — | |
| 73 | TEST Z CAL | `0x0800c20e` | INFO;0 | **Z calibration test — likely motion** |
| 74 | TEST RF | `0x0800c22c` | INFO;0 | radio test |
| 75 | SPINDLE CALIB VALUE | `0x0800c1d0` | INFO;0, ERR;1 | **calibration write** |
| 77 | PRINT ENCODER CAN | `0x0800c192` | INFO;0, printf → slot 0 | prints `ENC Test Started` — **starts a test** |
| 78 | LAST STATUS | `0x0800cf28` | re-sends the last `INFO` code † | read-like |
| 80 | SANITY CHECK | `0x0800cc02` (empty) | — | empty here (`PRINT SYS REPORT` does not run it) |
| 81 | LOAD WIFI | `0x0800cc02` (empty) | — | **Wi-Fi firmware load** — empty here |
| 84 | GET PARAM MEMORY | `0x0800c0e4` | INFO;0 | read-like |
| 85 | SET PARAM MEMORY | `0x0800c118` | INFO;0, printf → slot 0 | **persistent parameter write** |
| 87 | RESET PARAM MEMORY | `0x0800c06c` | INFO;0 | **resets persistent parameters** |

### 3. Not dispatched: response and console strings

Matched by the parser like any name, but outside the jump table, so they reach the
common tail with no effect and no reply.

| # | Prefix | Text | Notes |
|---|---|---|---|
| 0 | `> ` | COMMAND MODE ENTERED | reads as a console message (the serial flasher waits for `entered`) |
| 1 | `> ` | EXIT COMMAND MODE | console |
| 2 | `> ` | SYS RESET | no handler on 01.02.00 (the vendor's `CMD SYS RESET` is not in the table at all) |
| 3 | `> ` | SET PID | no handler on 01.02.00 |
| 88 | `\r\n` | *(empty)* | |
| 89 | `> ` | OK | |
| 90 | `> ` | INVALID COMMAND | nothing dispatches to it; unknown commands get no reply |
| 91 | `> ` | INVALID VALUE | |
| 92 | `> ` | ERROR | |
| 93 | `> ` | FIRST BACKUP | |

Whether these strings are printed elsewhere (for example on the serial console)
was not traced.

### Vendor-app commands that are not in the table

`ABORT WORK`, `GCODE CONTINUE`, `SEND TCP POS`, `GET Z BACKLASH`, `SET Z BACKLASH`,
`GET Z PLATE OFFSET`, `SET Z PLATE OFFSET`, `SET SPINDLE DISTANCE`,
`DISCARD TWRS DISTANCE`, `CMD SYS RESET`. A raw byte search of the image finds no
`BACKLASH`, `PLATE`, `TCP POS`, `ABORT WORK` or `GCODE CONTINUE` at all. The Wi-Fi
provisioning string (`SSID=…&PWD=…&AUTH_TYPE=…`) is not a table command either; it
presumably goes through the provisioning-mode path (not traced).

## Read-only allowlist (basis for the probe)

Six commands were checked handler by handler and found to only read state and
reply:

| Command | What the handler does |
|---|---|
| `INFO` | formats `INFO;34` from constants (`0x0800c732` → `0x08009c84`) |
| `GET GLOBAL SYS STATUS` | builds the 48-byte blob from RAM (`0x0802697c` writes only to the reply buffer); `INFO;0` |
| `PRINT SYS REPORT` | copies 20 stored sanity bytes and prints `INFO;40` (`0x0800c25e` → `0x08013e3c`); does not run `SANITY CHECK` |
| `GET SPINDLE CAGE STATUS` | reads a GPIO input (GPIOB pin 8); `INFO;0` and the direct reply |
| `READ SPINDLE CALIB` | reads a RAM float; slot-0 text and `INFO;0` |
| `READ INDEX CALIBRATION` | reads configuration through the standard getter (`0x0800a6a4`); slot-0 text and `INFO;0` |

- None of them calls motor, spindle, flash-erase or state-setting routines.
- Nuance: the configuration getter rewrites both external-flash configuration
  copies, but only when the two disagree or are invalid. It is the firmware's
  normal configuration read (8 call sites), and the vendor app sends
  `READ INDEX CALIBRATION` routinely from its toolbar.
- Every command, reads included, runs inside a per-command state wrapper
  (`0x0800b8c0` → `0x0801b480(1)`, restored at `0x0800cf8a`). In setup and work this
  only stops and restarts the state-machine tick timers. The vendor's routine reads
  go through the same path.
- **Not** on the allowlist: `GET Z BACKLASH`, `SEND TCP POS` and
  `GET Z PLATE OFFSET`. They are absent here, and nothing shows that they are pure
  reads on newer firmware (the vendor sent `GET Z PLATE OFFSET` only inside the
  tool-change calibration dialog, on ≥ 01.02.02).
- "Read-only" is established for **GFW 01.02.00** only. On any other firmware
  version it rests on the vendor's naming and usage.

## Open questions (to settle on hardware)

- What the robot does on an unknown command (statically: nothing, no reply).
- How quickly a slot is freed after a disconnect, and which connection gets
  WINC1500 descriptor 2 (the connect-time replay).
- Whether `INFO;7` / `INFO;8` are sent during or after an upload on 01.02.00.
- What the ten `GLOBAL STATUS` bytes contain in each machine state, and whether the
  vendor's field names match their positions.
- The `CAGE STATUS: CLOSED` reply without `\n` †.
- Everything about firmware newer than 01.02.00.
