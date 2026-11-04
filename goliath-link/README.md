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
- Opens the TCP link, runs a background reader, and offers a high-level client
  with the firmware-gated G-code upload sequence.

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
    # the robot runs panel detection itself; watch t.incoming for INFO/frames
    c.set_panel_size(1200, 800)
    c.upload_gcode(open("job.nc").read())   # cleans, uploads, waits for INFO;8
```

See [`../docs/protocol/`](../docs/protocol/) for the full interface and the
session sequence this client follows.
