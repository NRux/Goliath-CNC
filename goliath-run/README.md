<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# goliath-run

The job runner for the **Goliath CNC**: it will drive the robot through
goliath-link to set up, upload, monitor and record a job prepared by
goliath-prep, with a command line and a local web interface.

> **Status: package skeleton.** Nothing is implemented yet. It cannot drive a
> robot.

**Licence: GPL-3.0-or-later** (see [`LICENSE`](LICENSE), the same text as the
repository root's). It builds on [`goliath-link`](../goliath-link/), which is
Apache-2.0, and on [`goliath-prep`](../goliath-prep/). See
[`../LICENSING.md`](../LICENSING.md).

## Install from this checkout

goliath-run needs **Python 3.11 or newer** (the probe's Python 3.9 environment
from field guide 02 cannot run it; make a new one).

goliath-link, goliath-prep and goliath-run are **not on PyPI**. Each one names
the others as ordinary requirements, and pip can satisfy those only from
packages installed from this checkout. From the repository root, in a new
virtual environment, while online (nothing can be installed later on the
robot's own network):

```
python -m pip install --upgrade pip
python -m pip install -e ./goliath-link -e ./goliath-prep -e ./goliath-run
```

Installing in one pip command works because pip uses the folders given on the
command line for the names the packages ask for. One at a time works too, in
this order: goliath-link, then goliath-prep, then goliath-run. On Windows the
same commands work in PowerShell or `cmd`.

If pip reports **"No matching distribution found for goliath-link"** (or
goliath-prep), that package was not installed from this checkout first. Install
it as above. Do **not** fix that error by installing a package with that name
from PyPI: this project has not published any, so a package with that name
there would not be ours.

The root project offers the same set as extras, once the packages above are
installed: `python -m pip install -e ".[run]"`.

## Tests

```
python -m pip install -e "./goliath-link[dev]" -e "./goliath-prep[dev]" -e "./goliath-run[dev]"
python -m pytest goliath-prep/tests goliath-run/tests
```

The two suites run in one pytest session, so test file names must be unique
across `goliath-prep/tests`, `goliath-run/tests` and `tests/`: prefix them
`test_run_` here. Architecture tests (one upload call site, no raw transport
send outside the driver, no never-send command literal) read the code with the
repository's AST helpers (`tests/arch/astscan.py`) through the `astscan`
fixture in `tests/conftest.py`.
