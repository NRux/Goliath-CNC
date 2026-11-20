<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# goliath-prep

Job preparation for the **Goliath CNC**: it will read the G-code a CAM
post-processor writes, check it against the safety model and write the
program the robot runs (the converter, planner and safety checker).

> **Status: foundations implemented; full pipeline pending.** Parsing and
> normalization, machine/job configuration, geometry and heading analysis,
> material-removal queries and test oracles exist. The complete safety-check,
> planning and export pipeline is not ready for machining jobs.

See the [contributor guide](../CONTRIBUTING.md) and [roadmap](../ROADMAP.md)
for development setup and useful work that does not require a robot.

**Licence: GPL-3.0-or-later** (see [`LICENSE`](LICENSE), the same text as the
repository root's). It builds on [`goliath-link`](../goliath-link/), which is
Apache-2.0. See [`../LICENSING.md`](../LICENSING.md).

## Install from this checkout

goliath-prep needs **Python 3.11 or newer** (the probe's Python 3.9
environment from field guide 02 cannot run it; make a new one).

goliath-link, goliath-prep and goliath-run are **not on PyPI**. Each one names
the others as ordinary requirements, and pip can satisfy those only from
packages installed from this checkout. From the repository root, in a new
virtual environment, while online:

```
python -m pip install --upgrade pip
python -m pip install -e ./goliath-link -e ./goliath-prep
```

Installing in one pip command works because pip uses the folders given on the
command line for the names the packages ask for. One at a time works too, in
this order: goliath-link, then goliath-prep (then goliath-run). On Windows the
same commands work in PowerShell or `cmd` (use `.\goliath-link` if you prefer).

If pip reports **"No matching distribution found for goliath-link"**, goliath-link
was not installed from this checkout first. Install it as above. Do **not** fix
that error by installing a package called `goliath-link` (or `goliath-prep`,
`goliath-run`) from PyPI: this project has not published any, so a package
with that name there would not be ours.

The root project offers the same set as extras, once the packages above are
installed: `python -m pip install -e ".[prep]"` (or `".[run]"`).

## Tests

```
python -m pip install -e "./goliath-link[dev]" -e "./goliath-prep[dev]" -e "./goliath-run[dev]"
python -m pytest goliath-prep/tests goliath-run/tests
```

The two suites run in one pytest session, so test file names must be unique
across `goliath-prep/tests`, `goliath-run/tests` and `tests/`: prefix them
`test_prep_` here. Architecture tests read the code with the repository's AST
helpers (`tests/arch/astscan.py`) through the `astscan` fixture in
`tests/conftest.py`.
