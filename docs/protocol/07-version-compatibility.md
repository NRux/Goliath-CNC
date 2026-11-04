# 07 — Version compatibility

*Source: diff of the decompiled `GoliathCommLib` / `GoliathCommLibTCP` / `PPGoliath`
across Slingshot 1.7.12.0.0, 1.8.0.0, 1.09.05, 1.9.1.0, 1.9.3.0. Verified: 24/25
facts (1 count corrected: +6 errors at 1.09.05, not +7). **Corrected 2026-11-04
against static disassembly of the robot firmware GFW 01.02.00**; see
[09-firmware-01.02.00.md](09-firmware-01.02.00.md).*

There are two version axes, and earlier revisions of this page only covered the
first:

1. the **app / protocol library** version (Slingshot 1.7.12 … 1.9.3.0), and
2. the **robot firmware** version (`GFW xx.yy.zz`, read from `INFO;34`).

The app's command table is *not* the robot's command set. What the robot actually
accepts depends on its firmware.

## The short version

- The wire protocol is **TCP from the start** — it never migrated from serial. The
  "old vs new" comms library is just a **raw-socket → `TcpClient` rewrite**; both
  talk TCP :80.
- The app-side protocol **grew substantially at 1.09.05** and then **froze**.
- The **G-code dialect is byte-identical across all five versions.** It has not
  changed since 1.7.12.
- Target **1.9.3.0** (newest) for the host side; it is a strict superset in
  practice. The only wobble is the spindle/aspiration commands (below).
- **But the robot firmware lags the app.** The only robot firmware that survives,
  **GFW 01.02.00** (bundled, byte-identical, in 1.09.05, 1.9.1.0 and 1.9.3.0),
  lacks ten commands of the 1.09.05 set (below). The robot's actual version is
  unknown until it is read.

> **Correction.** Earlier revisions said: "If your machine runs firmware from the
> 1.09.05 era or later, the command/response set in this spec applies." The
> firmware bundled *with* 1.09.05 does not implement `ABORT WORK`,
> `GCODE CONTINUE`, `SEND TCP POS`, the four Z-offset commands,
> `SET SPINDLE DISTANCE`, `DISCARD TWRS DISTANCE` or `CMD SYS RESET`.

## Timeline (app / protocol library)

| Version | Comms library | Protocol change | Post-processor |
|---|---|---|---|
| 1.7.12.0.0 | old `GoliathCommLib` (raw socket, ASCII) only | baseline; smallest command set | baseline dialect |
| 1.8.0.0 | same | **identical** to 1.7.12 | identical |
| 1.09.05 | **new `GoliathCommLibTCP`** (`TcpClient`, UTF-8) introduced; old lib renamed `…Old` | **big jump**: +8 commands (GCODE CONTINUE, ABORT WORK, SEND POS, PRINT SYS REPORT, the 4 Z-offset cmds), +4 INFO (40–43), +6 ERROR (16–21); TCP lib adds GET GLOBAL SYS STATUS, ENABLE/DISABLE SPINDLE, ENABLE/DISABLE ASPIRATION, and the global-status frame (id 1002) | transient internal changes (later reverted) |
| 1.9.1.0 | both present; app uses the TCP lib | TCP lib **removes** the 4 spindle/aspiration commands (leaving GET GLOBAL SYS STATUS); everything else frozen | reverted to the 1.8.0 post-processor |
| 1.9.3.0 | both present; app uses the TCP lib | spindle/aspiration commands **re-added** (same syntax as 1.09.05); otherwise equals the frozen 1.09.05 set + global-status frame | unchanged since 1.7.12 |

Of the 1.09.05 additions, GFW 01.02.00 has `PRINT SYS REPORT`,
`GET GLOBAL SYS STATUS` (answered with a 48-byte blob, not the 19-byte frame),
`ENABLE/DISABLE SPINDLE` and `ENABLE/DISABLE ASPIRATION`. It lacks `GCODE CONTINUE`,
`ABORT WORK`, `SEND TCP POS` and the four Z-offset commands.

## Firmware versions

What is known about robot firmware versions comes from two places: the one
surviving image (GFW 01.02.00) and the version checks in the vendor app. Newer
firmware existed — the app gates features on 01.02.02 and 01.02.03 — but those
images were only ever downloaded from the vendor's now-deleted server
([06](06-firmware.md)).

### Firmware gates the vendor app applies

The version comes from `INFO;34` (`GFW xx.yy.zz`). Comparisons are the app's own.

| Behaviour | Vendor gate | On GFW 01.02.00 (firmware evidence) |
|---|---|---|
| `ABORT WORK` (Abort button) | sent only on firmware **> 01.02.00**; otherwise the button is disabled with "Abort functionality is not available on current firmware version" | **absent** from the command table. No software abort exists; stopping a job is physical ([03](03-session-sequence.md#stopping-a-job)) |
| `GCODE CONTINUE;0` before each upload | firmware **≥ 01.02.00** | **absent** from the command table: an unknown command, statically ignored with no reply. On 01.02.00 the vendor's pre-step did nothing |
| In-job tool-change Z-plate dialog (`GET`/`SET Z PLATE OFFSET`, triggered by a `;U` marker) | firmware **≥ 01.02.02** | both commands **absent**; consistent with the gate |
| Upload confirmation | on firmware **≥ 01.02.03** the app waits for `INFO;8`; **below 01.02.03** it sleeps `WAIT_AFTER_SEND_GCODE` = **1000 ms** after the last block and closes the upload dialog without waiting | at the end of an upload the firmware sends `INFO;0`. A direct `INFO;8` is sent only in the connect-time replay; whether one is also sent at the end of an upload is not established. Do not treat a missing `INFO;8` as failure below 01.02.03 — report the upload as unconfirmed instead |
| Second job without restart | on firmware **≤ 01.02.00** the app refuses a second upload in the same session ("Please restart Goliath before starting another job.") and closes | vendor behaviour; whether the firmware needs the restart is **unverified**. 01.02.00 has no remote reboot (`CMD SYS RESET` absent), so "restart" means a power cycle |
| Cage check | firmware **≥ 01.00.14**: `GET SPINDLE CAGE STATUS`; older: `POLL CAGE` / `STOP POLL CAGE` | both present |
| SSID list refresh | firmware **< 01.00.14**: `RESET NET KEY` | present |
| Keep-alive | firmware **> 01.02.00**: `GET GLOBAL SYS STATUS` about every 60 s | present (answered with the 48-byte blob) |

> **Correction.** Earlier revisions listed only three gates (CONTINUE, ABORT,
> Z-plate) under "Firmware-gated behaviour to honour", and implied that sending
> `GCODE CONTINUE;0` on ≥ 01.02.00 was required. On 01.02.00 it does not exist.
> The upload-confirmation and second-job gates were missing.

## What a replacement should target

- **Transport & framing:** TCP :80 with **three connections** and the firmware's
  slot routing and wire shapes ([01](01-transport-and-framing.md)).
- **Read the firmware version first** (`INFO;34`, pushed on connect or requested
  with `INFO` — the reply arrives on the second connection). Until it is known,
  assume the smallest command set (GFW 01.02.00) and refuse commands that need a
  newer version with a clear error.
- **Commands/responses:** the intersection of the 1.09.05-frozen app set and the
  GFW 01.02.00 command table is what works on the firmware we can inspect. The
  commands outside it (`ABORT WORK`, `GCODE CONTINUE`, `SEND TCP POS`, the Z-offset
  commands, `SET SPINDLE DISTANCE`, `DISCARD TWRS DISTANCE`, `CMD SYS RESET`) are
  for newer firmware only, and their semantics there are unverified.
- **Global status:** decode both the 48-byte `GLOBAL STATUS` blob (01.02.00) and the
  19-byte `G…S` frame (vendor decoder; other firmware).
- **Spindle/aspiration (`ENABLE/DISABLE SPINDLE`, `ENABLE/DISABLE ASPIRATION`):**
  present on 1.09.05 and 1.9.3.0 apps, **absent from the 1.9.1.0 app**, and present
  in GFW 01.02.00.
- **G-code:** emit the single dialect in [05-gcode-dialect.md](05-gcode-dialect.md);
  it is valid for every version.
- **Firmware-gated behaviour to honour:** every row of the gate table above.

## Caveats
- The "frozen since 1.09.05" claim is from diffing the five installers we have;
  there may be intermediate releases (e.g. 1.09.xx) we did not analyse.
- Older machines still running pre-1.09.05 firmware would use the **raw-socket**
  library's smaller command set. We have the old library decompiled if that case
  ever arises, but this spec documents the modern (TCP-lib) protocol.
- Firmware facts are from static analysis of GFW 01.02.00 only. For any other
  firmware version, the table above records what the vendor app *assumed*, not
  what the firmware does.
