# 06 — Firmware: images and flashing

*Source: `GoliathWPF` Firmware/FirmwareUpdate.cs, Helpers/FirmwareUpdateManager.cs,
GoliathStatus.cs; the decompiled `GoliathFirmwareTool` (v1.1.2.0); and
`app/Firmware/Latest.hex`. Verified: 31/32 facts confirmed (1 corrected).*

> **Flashing firmware can brick a device and there is no factory recovery server
> left.** Read all of this before attempting anything. Owners previously bricked
> **towers** by flashing the wrong image. Do not flash unless you have a recovery
> plan and ideally a known-good backup.

## The firmware image we have

- `app/Firmware/Latest.hex` — **Intel HEX**, **identical across versions 1.09.05,
  1.9.1.0, 1.9.3.0** (same MD5). This is the **robot (main) firmware** only.
- Version string in the image: **`GFW 01.02.00`** ("Goliath FirmWare").
- Loads to STM32 flash **`0x08008000`–`0x0802D8A4`** (~150 KB). The first 32 KB of
  flash (`0x08000000`–`0x08008000`) is **not** in the image — it is a resident
  bootloader/loader that stays put. Flashing writes only the application region.
- The image contains the control-protocol command strings (e.g. `JOG STOP
  GOLIATH`), confirming it implements the robot side of this protocol.
- SSID format string `Goliath_%02x%02x%02x` (confirms the `Goliath_<hex>` SSID),
  and references to a `goliathconfig.com` config domain and an on-device FW
  backup/rollback mechanism.

## Two ways the vendor flashed firmware

### Path 1 — over Wi-Fi, inside Slingshot (same TCP link)
Uses the normal control socket (TCP :80, `S1`). It targets the **resident loader**,
not the STM32 system/DFU bootloader:
1. PC → `LOAD;<lineCount>;` (number of Intel-HEX records to expect).
2. Robot → a line containing **`waiting for a hex file`**.
3. PC streams each Intel-HEX record as **raw ASCII, one record per write, no
   trailing newline**.
4. Robot acks each record with **`FW LB OK`**; PC sends the next.
5. Completion = a reply containing **`Number` / `stored` / `records`**; any
   **`ERROR`** aborts.
6. A 500 ms timer re-establishes the TCP connection if it drops mid-flash. After
   completion the app re-reads `INFO;34` to confirm the new version.

### Path 2 — over USB serial (the standalone `GoliathFirmwareTool.exe`)
Slingshot can launch the separate tool (and then exits). The tool uses a COM port
(the robot's USB-B port; driver is **FTDI** — it links users to `ftdichip.com`):

- **Serial settings** (defaults): robot = **57600 baud, 8-N-1**, DTR on; tower =
  **19200 baud, 8-N-1**.
- Handshake:
  1. open port; wait for boot banner (`calibration done` for the robot, `end` for a
     tower);
  2. PC → byte **`0x03`** (enter command mode); wait for `entered`;
  3. PC → **`load\r\n`**; wait for a reply containing `hex`;
  4. stream each Intel-HEX line **character by character** (a few ms between chars,
     a few more between lines), each line ended with `\n`;
  5. completion detected on reboot (`calibration done` / `end`).
- The tool flashes **both** the robot (`Goliath_Main.hex`) and the **towers**
  (`Goliath_Tower.hex`), picking baud and image by device type. **This is why
  cross-flashing bricks a tower** — wrong baud, wrong image.

## How Slingshot decides a robot needs updating

- Current version: parsed from `INFO;34;<prj_name>;…`, where `prj_name` holds the
  `GFW xx.yy.zz` string (`GoliathStatus.ReadVersion`). Tower firmware versions come
  from the global-status frame (id 1002, fields `t1_fw`/`t2_fw`).
- Latest version: a JSON manifest and the `.hex` were fetched from URLs stored in
  config. Update offered when latest > current.

## The servers are gone (recovery implications)

The firmware/manifest/tool were served from an **S3 bucket**, not the website:
- `https://goliathcnc.s3.eu-west-3.amazonaws.com/Firmware/Goliath_Main.hex`
- `https://goliathcnc.s3.eu-west-3.amazonaws.com/Firmware/Goliath_Tower.hex`
- `.../FWTool/GoliathFirmwareTool.exe`, `.../FWTool/goliathfirmwaretool.json`,
  `.../Firmware/LatestFirmwareVersion.json`

As of 2026-11, that **bucket no longer exists** (`NoSuchBucket`). Consequences:
- The **main robot firmware** is preserved (`Latest.hex`, GFW 01.02.00, bundled in
  the installers). Keep copies.
- The **tower firmware (`Goliath_Tower.hex`) is not recoverable** from the vendor —
  it was never bundled in an installer, only downloaded. If any owner downloaded a
  tower update, that file is valuable; otherwise tower firmware cannot currently be
  reflashed from a known-good image.

## Recommendation for a replacement

- Treat firmware flashing as **last resort**. The machine we have runs GFW
  01.02.00 fine; there is no reason to reflash the robot unless recovering a brick.
- If you build a flasher, prefer the **serial path** (Path 2) — it is fully
  specified above and doesn't depend on the robot's Wi-Fi/app state. Read back the
  version afterward to confirm.
- **Never** flash a tower with the robot image or vice-versa. Keep the two images
  and their baud rates strictly separate.

## Caveats
- The exact robot-side acceptance of the Wi-Fi `LOAD;` path (per-record vs block,
  the precise completion phrase) is read from the host; confirm with a capture
  before relying on it.
- `GoliathFirmwareTool` was analysed from a managed build extracted from the
  single-file bundle; the shipped build could differ in minor details.
- STM32 part number, the loader's own protocol, and the backup/rollback mechanism
  live in the firmware image itself and would need a Cortex-M disassembly (Ghidra,
  ARM Cortex-M) to pin down — out of scope here.
