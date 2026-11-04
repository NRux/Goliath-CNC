# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""goliath-run: the job runner for the Goliath CNC.

It will drive the robot through goliath-link: set-up, upload, monitoring and
recording of a job prepared by goliath-prep, with a command line and a local
web interface (build design sections 7 and 8).

**Status: package skeleton.** Nothing is implemented yet; the parts arrive in
later work units. It cannot drive a robot.

Licence: GPL-3.0-or-later. It builds on goliath-link (Apache-2.0) and
goliath-prep.
"""
# Keep this file parseable by old Pythons, and keep this check above every
# import that needs Python >= 3.11: run from a source checkout on an older
# interpreter, the package then stops with this message instead of a confusing
# syntax error further in.
import sys

if sys.version_info < (3, 11):
    raise ImportError(
        "goliath-run requires Python >= 3.11; this is Python %d.%d. "
        "Create a new virtual environment with Python 3.11 or newer." % sys.version_info[:2]
    )

__version__ = "0.0.1"

__all__ = ["__version__"]
