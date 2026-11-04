# 07 — Version compatibility

*Source: diff of the decompiled `GoliathCommLib` / `GoliathCommLibTCP` / `PPGoliath`
across Slingshot 1.7.12.0.0, 1.8.0.0, 1.09.05, 1.9.1.0, 1.9.3.0. Verified: 24/25
facts (1 count corrected: +6 errors at 1.09.05, not +7).*

## The short version

- The wire protocol is **TCP from the start** — it never migrated from serial. The
  "old vs new" comms library is just a **raw-socket → `TcpClient` rewrite**; both
  talk TCP :80.
- The protocol **grew substantially at 1.09.05** and then **froze**. If your
  machine runs firmware from the 1.09.05 era or later, the command/response set in
  this spec applies.
- The **G-code dialect is byte-identical across all five versions.** It has not
  changed since 1.7.12.
- Target **1.9.3.0** (newest) for a replacement; it is a strict superset in
  practice. The only wobble is the spindle/aspiration commands (below).

## Timeline

| Version | Comms library | Protocol change | Post-processor |
|---|---|---|---|
| 1.7.12.0.0 | old `GoliathCommLib` (raw socket, ASCII) only | baseline; smallest command set | baseline dialect |
| 1.8.0.0 | same | **identical** to 1.7.12 | identical |
| 1.09.05 | **new `GoliathCommLibTCP`** (`TcpClient`, UTF-8) introduced; old lib renamed `…Old` | **big jump**: +8 commands (GCODE CONTINUE, ABORT WORK, SEND POS, PRINT SYS REPORT, the 4 Z-offset cmds), +4 INFO (40–43), +6 ERROR (16–21); TCP lib adds GET GLOBAL SYS STATUS, ENABLE/DISABLE SPINDLE, ENABLE/DISABLE ASPIRATION, and the global-status frame (id 1002) | transient internal changes (later reverted) |
| 1.9.1.0 | both present; app uses the TCP lib | TCP lib **removes** the 4 spindle/aspiration commands (leaving GET GLOBAL SYS STATUS); everything else frozen | reverted to the 1.8.0 post-processor |
| 1.9.3.0 | both present; app uses the TCP lib | spindle/aspiration commands **re-added** (same syntax as 1.09.05); otherwise equals the frozen 1.09.05 set + global-status frame | unchanged since 1.7.12 |

## What a replacement should target

- **Transport & framing:** the `TcpClient`-style TCP :80 behaviour (this spec).
  Works for any machine on firmware ≥ ~1.09.05.
- **Commands/responses:** the 1.09.05-frozen set **plus** `GET GLOBAL SYS STATUS`
  and the `SMART_INFO_GLOBAL_STATUS` (id 1002) frame. That is the common
  denominator from 1.09.05 onward.
- **Spindle/aspiration (`ENABLE/DISABLE SPINDLE`, `ENABLE/DISABLE ASPIRATION`):**
  present on 1.09.05 and 1.9.3.0, **absent on 1.9.1.0**. Treat them as optional —
  probe or make them non-essential. (The robot may still obey them; the 1.9.1.0
  *app* just didn't expose them.)
- **G-code:** emit the single dialect in [05-gcode-dialect.md](05-gcode-dialect.md);
  it is valid for every version.
- **Firmware-gated behaviour** to honour (firmware version from `INFO;34`):
  - `GCODE CONTINUE;0` before upload: firmware ≥ **01.02.00**.
  - `ABORT WORK`: the app only sends it on firmware **> 01.02.00**.
  - In-job tool-change Z-calibration dialog: firmware ≥ **01.02.02**.

## Caveats
- The "frozen since 1.09.05" claim is from diffing the five installers we have;
  there may be intermediate releases (e.g. 1.09.xx) we did not analyse.
- Older machines still running pre-1.09.05 firmware would use the **raw-socket**
  library's smaller command set. We have the old library decompiled if that case
  ever arises, but this spec documents the modern (TCP-lib) protocol.
