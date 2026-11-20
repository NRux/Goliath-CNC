<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Goliath CNC

[![CI](https://github.com/NRux/Goliath-CNC/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/NRux/Goliath-CNC/actions/workflows/ci.yml)

Open replacement software for the **Goliath CNC mobile router**. We are
documenting its protocol and building independent job-preparation and runner
tools so owners can keep their machines useful without Slingshot.

**Early development: not released for cutting.** Software contributors can work
with fixtures, simulations and local tests without owning a robot. Linux and
Windows are covered by CI. There is no complete end-user application yet.

[Start contributing](CONTRIBUTING.md) · [Roadmap](ROADMAP.md) ·
[Documentation](docs/README.md) · [Report a bug](https://github.com/NRux/Goliath-CNC/issues/new/choose)

## What is here

| Component | Current state |
|---|---|
| [Protocol specification](docs/protocol/README.md) | Commands, framing, job format and firmware analysis; findings distinguish static evidence from hardware observations. |
| [goliath-link](goliath-link/) | Python transport/client, simulator and diagnostic probe. Changes are held for the older-firmware design review described below. |
| [goliath-prep](goliath-prep/README.md) | G-code parsing, configuration, geometry, heading and material-removal foundations. The complete check/plan/export pipeline is still being built. |
| [goliath-run](goliath-run/README.md) | Facts, capability gates, recording, export and local UI infrastructure. Robot-controller integration and an application CLI are still pending. |
| [CAM posts](docs/README.md#cam-integrations) | FreeCAD and Fusion posts and a Kiri:Moto device configuration. |
| [Slingshot research](docs/research/slingshot-reverse-engineering.md) | Five-version static analysis, reproducible artifact identities, firmware pitfalls and project formats. Recovered vendor source is not distributed. |

## First local success

Use **Python 3.11 or newer**; Python 3.13 is a convenient choice for all packages.
These commands install from the checkout and run documentation checks. They do
not contact a robot. The project packages are not published on PyPI.

```sh
git clone https://github.com/NRux/Goliath-CNC.git
cd Goliath-CNC
```

Linux/macOS shell (macOS is not currently in CI):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e "./goliath-link[dev]" -e "./goliath-prep[dev]" -e "./goliath-run[dev]"
.venv/bin/python -m pip install -e ".[dev,prep,run]"
.venv/bin/python -m pytest -q tests/test_repo_docs.py tests/test_field_guides.py
```

Windows PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e "./goliath-link[dev]" -e "./goliath-prep[dev]" -e "./goliath-run[dev]"
.\.venv\Scripts\python.exe -m pip install -e ".[dev,prep,run]"
.\.venv\Scripts\python.exe -m pytest -q tests/test_repo_docs.py tests/test_field_guides.py
```

Choose an installed Python 3.11+ version if you do not have 3.13. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the full test commands, code layout,
licences, and pull-request process. No firmware, vendor application or robot
is required for this setup.

The [application installation guide](docs/field-guide/03-install-the-applications.md)
has additional source-installation and offline-check details.

## Hardware status

The [first hardware capture](probe-results/README.md), recorded on 2026-11-05,
identified **firmware 01.00.13**. It sent diagnostic commands; it was not a
receive-only observation. The inspected firmware image is **01.02.00**, so its
command behavior cannot be assumed for that older robot.

This is the project's **B4 hold**: the older-firmware case needs design review,
and jobs/motion remain blocked. The current probe stops further commands after
detecting old, unknown or conflicting firmware. A separate receive-only TCP
observer is [proposed](docs/research/firmware-01.00.13-compatibility-review.md),
but is **not implemented**. Do not rerun the old probe as a passive test or
upgrade firmware to work around the compatibility gap.

Contributions to offline tools and documentation are welcome now. Changes to
`goliath-link/` require the reviewed B4 design and an explicit update to the
[freeze record](tests/arch/goliath_link_freeze.json); do not bypass that check.
Owners can start with the
[powered-off measurement guide](docs/field-guide/04-measure-your-robot.md).

## Licence and independence

Applications, tools and research notes are **GPL-3.0-or-later**. The driver,
protocol/preparation specifications and CAM posts are **Apache-2.0**. Follow
[LICENSING.md](LICENSING.md) for the exact folder boundaries; the licences do not
cover proprietary vendor software.

Copyright (C) 2026 Noah Rappaport and contributors. See [LICENSE](LICENSE).
"Goliath" and "Slingshot" are names used by Springa S.r.l. This project is
independent and not affiliated with Springa, SCM Group or their successors.
