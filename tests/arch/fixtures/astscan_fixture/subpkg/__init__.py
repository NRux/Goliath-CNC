# SPDX-License-Identifier: GPL-3.0-or-later
"""Sub-package: relative imports resolve against the package, not the file."""
from ..helpers import run_thing  # REF: helper import exact


def from_package_init():
    run_thing()  # MARK: helper exact
