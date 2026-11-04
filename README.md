# Goliath-CNC

Open replacement software for the **Goliath CNC**, the mobile robotic router made
by Springa S.r.l. (Milan), which is now in liquidation. The goal is to keep these
machines working without the discontinued Slingshot app.

**Status: research / first contact.** Nothing here drives a machine yet.

- [`docs/research/prior-art-review.md`](docs/research/prior-art-review.md): what is
  known about the machine, which existing open-source projects help and which
  don't, and the phased plan.
- [`docs/field-guide/01-first-contact.md`](docs/field-guide/01-first-contact.md):
  read-only steps for probing a Goliath's USB and Wi-Fi interfaces. Start here if
  you own one.
- [`docs/field-guide/02-first-probe.md`](docs/field-guide/02-first-probe.md):
  the read-only probe (`goliath-link`): firmware version and connection routing.
- [`docs/field-guide/03-install-the-applications.md`](docs/field-guide/03-install-the-applications.md):
  installing `goliath-prep` and `goliath-run` on Windows or Linux, and the offline
  check. They are not released yet.
- [`docs/field-guide/04-measure-your-robot.md`](docs/field-guide/04-measure-your-robot.md):
  tape-measuring the robot with it switched off (bring-up rung R0b): wheel
  contact points, body outline, wire attachment, the physical stop.
- `tools/serial_listen.py`: a passive capture tool for those steps. It never
  transmits.

## Development

```
python -m venv .venv
.venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip install -e ".[dev]"
.venv/bin/python -m pytest
```

## Licence

Copyright (C) 2026 Noah Rappaport and contributors.

This program is free software: you can redistribute it and/or modify it under the
terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. See [`LICENSE`](LICENSE).

"Goliath" and "Slingshot" are names used by Springa S.r.l. This project is
independent and not affiliated with Springa, SCM Group or their successors.
