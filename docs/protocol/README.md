# Goliath CNC — protocol & interface specification

This folder specifies how the Goliath CNC robot is controlled, in enough detail to
build replacement software. It was reconstructed from the vendor's own (now
discontinued) Windows application, **Slingshot**, versions 1.7.12.0.0 through
1.9.3.0.

**Status:** reconstructed and cross-checked, **not yet tested against a real
machine.** Every claim here is traceable to decompiled host code or, since
2026-11-04, to static disassembly of the robot firmware GFW 01.02.00 (see
[09-firmware-01.02.00.md](09-firmware-01.02.00.md)); see *Provenance and
confidence* below. Treat it as a high-confidence draft to validate on hardware,
not as gospel.

## How this was produced

Springa (the maker) has been wound up and its website, forum, firmware server, and
download service are all offline. The owner of a Goliath machine retained the
Slingshot installers. Those were unpacked and the .NET assemblies decompiled to
C# (the vendor shipped debug symbols, so original names survived). The decompiled
source for seven subsystems was then analysed and **independently verified**: for
each subsystem one pass extracted the facts with code citations, and a second,
adversarial pass re-opened the cited code to confirm or refute each one.

- **225** individual facts confirmed against the code
- **1** refuted, **3** partially unsupported — all corrected and reflected here
- The correction log is in each document's *Caveats* section.

The raw decompiled source is **not** published in this repository, and nothing in
these documents is copied from it. The vendor's installers are not published here
either; they were removed on 2026-11-06, and they remain the vendor's copyright,
not covered by this repository's licences (see [LICENSING.md](../../LICENSING.md)).
These documents describe the *interface* — commands, message formats, sequences —
in our own words, which is what a replacement needs.

## The one-paragraph summary

The PC is a **TCP client**; the robot is a **TCP server at `10.0.0.1:80`** on its
own Wi-Fi access point (or at a DHCP address when joined to your network). The PC
opens **three connections** in order and sends on the first only; the robot
routes its output by connection (identity, acknowledgements and errors to the
second, position frames to the third). Commands are **ASCII text with no
terminator** (`COMMAND;arg;arg`, one command per TCP write, paced); replies are
text and **binary frames**, most of them sent as 512-byte NUL-padded blocks. The robot runs its own
state machine and **advances itself**; the PC mostly *configures* and *observes*,
then uploads a G-code file. The G-code is a fairly ordinary **absolute-mm
dialect** (G00/G01/G02/G03, mm, 3-decimal coordinates) with two unusual traits: a
**`C` word** carrying the robot's body heading (yaw about the tool), and **`M10`/`M05`** for spindle on/off
instead of M03/M05. Firmware is flashed either over this same Wi-Fi link or over
**USB serial** (FTDI, 57600 8-N-1 for the robot).

## What a replacement must implement (minimum)

1. Join the robot's Wi-Fi AP (or find it on your LAN) and open **three** TCP
   connections to port 80, in order. →
   [04-connection-and-provisioning.md](04-connection-and-provisioning.md),
   [03-session-sequence.md](03-session-sequence.md#connecting)
2. Speak the line protocol and decode the binary telemetry frames. →
   [01-transport-and-framing.md](01-transport-and-framing.md),
   [02-message-reference.md](02-message-reference.md)
3. Follow the job sequence: pair towers → let the robot run panel detection →
   send panel size → upload G-code → watch it run. →
   [03-session-sequence.md](03-session-sequence.md)
4. Generate G-code in the Goliath dialect (or post-process from a CAM tool). →
   [05-gcode-dialect.md](05-gcode-dialect.md)

Firmware flashing and version compatibility are separate concerns:
[06-firmware.md](06-firmware.md), [07-version-compatibility.md](07-version-compatibility.md), [08-head-angle-and-cut-order.md](08-head-angle-and-cut-order.md).
What the robot firmware actually implements (its command table, and where each
reply goes) is in [09-firmware-01.02.00.md](09-firmware-01.02.00.md); read it
before sending anything to a real machine.

## Documents

| File | Covers |
|---|---|
| [01-transport-and-framing.md](01-transport-and-framing.md) | TCP transport, the three connections and slot routing, wire shapes (NUL padding, unterminated replies), the binary frames (byte layouts) |
| [02-message-reference.md](02-message-reference.md) | Every command, and every INFO/ERROR/WARNING response, with wire codes |
| [03-session-sequence.md](03-session-sequence.md) | The ordered command/response flow for a full job, plus maintenance actions |
| [04-connection-and-provisioning.md](04-connection-and-provisioning.md) | Wi-Fi AP vs shared-network, discovery, tower RF pairing, calibration |
| [05-gcode-dialect.md](05-gcode-dialect.md) | The G-code the robot accepts, number formats, the C axis, upload mechanics |
| [06-firmware.md](06-firmware.md) | The two firmware-flash paths (Wi-Fi and serial), the image, dead servers |
| [07-version-compatibility.md](07-version-compatibility.md) | What changed across versions; the common denominator to target |
| [08-head-angle-and-cut-order.md](08-head-angle-and-cut-order.md) | How the host picks each cut's `C` heading (robot footprint vs panel and tower zones) and orders cuts |
| [09-firmware-01.02.00.md](09-firmware-01.02.00.md) | Firmware GFW 01.02.00 from static disassembly: the 94-entry command table (which commands exist, which never to send), output routing, wire encoding, with addresses |

## Key reference constants (machine 1.9.3.0 / firmware GFW 01.02.00)

| Thing | Value | Source |
|---|---|---|
| Robot TCP endpoint (AP mode) | `10.0.0.1:80` | `Slingshot.exe.config`, `SocketManager.cs` |
| Robot Wi-Fi SSID | `Goliath_<6 hex>` (e.g. `Goliath_a318c7`, from MAC) | `Slingshot.ini`, firmware strings |
| Robot AP WPA key | hard-coded in the app (value withheld — see 04) | `connection-provisioning` analysis |
| G-code upload packet size | `1024` bytes (`GCODE_PACK_SIZE`, from INI) | `Slingshot.ini`, `SendingGCode.cs` |
| Max work area | 3500 × 3500 mm, diagonal 3500 | `Slingshot.ini [GENERALE]` |
| Z travel | 58 mm (`MAX_DIM_Z`) | `Slingshot.ini` |
| Max bit diameter / length | 8 mm / 58 mm | `Slingshot.ini [UTENSILI]` |
| Tower encoder index-distance range | 27000–30100 | `Slingshot.ini [INDEX_DISTANCE]` |
| Firmware image | Intel HEX, app at `0x08008000`, "GFW 01.02.00" | `app/Firmware/Latest.hex` |
| Connections | 3, opened in order ~250 ms apart; send on the first only | `CommManager.cs`; firmware slot routing (09) |
| Robot → PC block size | 512 bytes, NUL-padded (slot messages); tower frames 4 bytes; direct replies exact length | firmware (09) |
| Firmware command table | 94 records, 84 dispatched; no `ABORT WORK` / `GCODE CONTINUE` | firmware (09) |

## Provenance and confidence

- Everything here derives from Slingshot **1.9.3.0** unless a document says
  otherwise. Firmware-side statements derive from static disassembly of GFW
  01.02.00 ([09](09-firmware-01.02.00.md)); where they contradicted the
  host-side reading, the documents were corrected and the correction is marked
  ("Earlier revisions said…"). The G-code dialect is byte-identical across all five analysed
  versions; the control protocol froze at 1.09.05 (see
  [07-version-compatibility.md](07-version-compatibility.md)).
- A fact stated plainly was confirmed in the code by two independent passes. A
  fact hedged with "appears/likely" was not fully pinned down and needs hardware
  confirmation. **Nothing here has run against a real robot yet** — the first job
  anyone drives with replacement software should be air-cut (bit raised, wheels
  blocked, panel scrap) until the sequence and especially Z handling are proven.
