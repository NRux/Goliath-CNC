# goliath-link

An open driver library for the **Goliath CNC** robot — a clean-room
implementation of its Wi-Fi control protocol, built from the specification in
[`../docs/protocol/`](../docs/protocol/), not from the vendor's software.

**Licence: Apache-2.0** (see [`LICENSE`](LICENSE)). Chosen so that *any* tool —
open or closed — can speak to a Goliath, to keep as many of these abandoned
machines working as possible. The controller/CAM applications in this repository
are GPL-3.0-or-later; this driver is deliberately permissive.

> **Status: early, and untested against hardware.** The protocol is well
> documented and the pure parts (message building, parsing, framing) are
> unit-tested, but nothing here has driven a real robot yet. Air-cut first (bit
> raised, wheels blocked, scrap panel) until the sequence — especially Z and the
> upload — is proven on your machine.

## What it does

- Builds every control command (`goliath_link.protocol.Commands`).
- Parses the robot's text responses (`INFO`/`ERR`/`WARN`) and decodes the three
  binary telemetry frames (position / tower / global status), including the
  bit-reversed tower battery field.
- Frames the mixed text+binary TCP stream robustly (`StreamFramer`), reassembling
  messages split across reads — an improvement over the vendor's timing-based
  framing.
- Opens the robot's three TCP connections the way the vendor app does (Nagle
  disabled, TCP keepalive on), runs one reader per connection, and offers a
  high-level client with serialised, paced sends, firmware-version gates and
  the G-code upload sequence. Its `stop_work()` sends `ABORT WORK` only on
  firmware newer than 01.02.00 and reports a stop only when the robot confirms
  it; GFW 01.02.00 has no remote stop, so there it refuses and points to the
  physical stop.

## Tools

- **`python -m goliath_link.probe`**: a read-only first-contact probe for a
  real robot. Close Slingshot and any phone app first: the robot sends its
  replies by connection slot, so the probe opens three connections (S1, S2, S3)
  the way the vendor app does and records every byte on each, tagged.
  - By default it sends only six commands whose handlers were verified
    read-only in firmware GFW 01.02.00: `INFO`, `GET GLOBAL SYS STATUS`,
    `PRINT SYS REPORT`, `GET SPINDLE CAGE STATUS`, `READ INDEX CALIBRATION`,
    `READ SPINDLE CALIB`. To test framing it also sends `INFO` followed by
    `\n` and by `\r\n`.
  - `--extended` adds `GET Z BACKLASH` and `SEND TCP POS`, but only on
    firmware newer than 01.02.00 (01.02.00 does not have them). They are not
    verified read-only. `GET Z PLATE OFFSET` is never sent.
  - A check in code refuses anything else at run time.
  - The report says which connection each reply arrived on, flags slot-routing
    problems (another client connected, slots not yet released), and lists
    late or unattributable messages separately.
  - Exit code: 0 only for a complete, consistent result; 3 = finished with
    warnings; 1 = failed; 130 = interrupted. Every reason is listed.

  See [`../docs/field-guide/02-first-probe.md`](../docs/field-guide/02-first-probe.md).
- **`python -m goliath_link.sim`**: a fake robot on TCP, for developing and
  testing without a Goliath. `--profile gfw-01.02.00` (the default) follows the
  disassembled GFW 01.02.00 firmware: six connection slots, replies routed by
  slot and padded to 512 bytes, the 48-byte status blob, its 94-command table.
  `--profile vendor-app-newer` models what the vendor app expects from newer
  firmware (a guess: no image survives); `--profile legacy-spec` is the old
  single-connection model. Hazard options reproduce real-world trouble, e.g.
  `--occupied-slots 1` (another client holds a slot) or
  `--slot-release-delay`. It is **not** the real firmware; replies whose exact
  text or timing is not pinned down are marked `GUESS` in `sim.py`.

## Install / test

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

## Example

```python
from goliath_link import GoliathTransport, GoliathClient

with GoliathTransport("10.0.0.1") as t:     # robot AP, or its DHCP address
    c = GoliathClient(t)
    c.learn_firmware_version()
    c.set_rf_channels(101, 114)             # pair the towers
    # the robot runs panel detection itself; watch c.subscribe() for INFO/frames
    c.set_panel_size(1200, 800)
    c.upload_gcode(open("job.nc").read())   # cleans, uploads, waits for INFO;8
```

`GoliathTransport` opens the robot's three connections (S1/S2/S3) and sends
only on S1. Once a `GoliathClient` is attached, its event pump consumes
`t.records`/`t.incoming`; read events through `c.subscribe()` or
`c.history()` instead. On GFW 01.02.00, which may never send `INFO;8`,
`upload_gcode` raises `UploadUnconfirmedError` unless you pass
`confirm="info8-or-info0"`; the error explains why.

See [`../docs/protocol/`](../docs/protocol/) for the full interface and the
session sequence this client follows.
