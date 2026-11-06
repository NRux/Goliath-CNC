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
  - **Firmware gate.** Those commands were checked on firmware 01.02.00 only,
    and the first robot probed runs 01.00.13. So the probe reads the version
    from every identity line the robot sends (`INFO;34`, `GFW NN.NN.NN`, the
    dated 01.00.13 form included). Before one arrives it sends only `INFO`.
    Once the robot reports firmware older than 01.02.00, or an identity that
    cannot be read or that contradicts another, it sends **nothing more** in the
    whole run: no further terminator test, no sweep, no `PRINT SYS REPORT`, no
    `--extended` (and not a single byte if the identity arrives on connect). An
    `INFO;28` line alone does not open the gate. The gate is asked after the
    link's send hold-back and again just before each write, and while an
    `INFO` line is still arriving undecoded the probe waits for it (if it
    stays undecoded, the firmware is unknown). It reports why, where and every
    command it did not send, and prints a `PROBE STOPPED` line. On 01.02.00 and
    newer it runs as before.
  - A check in code, at the one place that sends, refuses anything else at run
    time, the firmware gate included.
  - The report says which connection each reply arrived on, flags slot-routing
    problems (another client connected, slots not yet released), and lists
    late or unattributable messages separately.
  - **Privacy.** The robot reports its serial number, and firmware 01.00.13
    lists the names of nearby Wi-Fi networks on every connect: together they
    can locate it. The raw report and capture keep them (keep those private).
    Next to them the probe writes `goliath-probe-<time>.shareable.json` and
    `.shareable.capture.txt`, with the serial replaced by `X`s and each network
    name by `SSID01`, `SSID02`, ... of the same length (hex and ASCII alike, so
    every message keeps its length), and its terminal summary never shows
    either. Both are found by position (the identity fields, whatever their
    layout; each name up to the end of its block, line breaks included), so
    what the probe sent and what it states about the firmware are never
    rewritten. The summary says which files are safe to share.
  - Exit code: 0 only for a complete, consistent result; 3 = finished with
    warnings; 4 = stopped by the firmware gate (not a success); 1 = failed;
    130 = interrupted. Every reason is listed.

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
`c.history()` instead. One client per transport: `c.close()` before you
attach another (a second live client raises `RecordsInUseError`). To
reconnect, `t.close()` then `t.connect()`: `connect()` waits
`slot_release_wait` (2 s) after the old link went down so the robot can free
its slots. A send from a transport `listener` waits for the send lock at most
`listener_send_wait` (1 s), then raises `SendBlockedError` (an upload holds
the lock until the robot leaves upload mode). On GFW 01.02.00,
which may never send `INFO;8`, `upload_gcode` raises `UploadUnconfirmedError`
unless you pass `confirm="info8-or-info0"`; the error explains why.

See [`../docs/protocol/`](../docs/protocol/) for the full interface and the
session sequence this client follows.
