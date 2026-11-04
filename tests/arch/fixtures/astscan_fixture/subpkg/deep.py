# SPDX-License-Identifier: GPL-3.0-or-later
"""Two levels down: ``from .. import helpers`` and a module-level alias."""
from .. import helpers
from ..helpers import run_thing  # REF: helper import exact


def use():
    helpers.run_thing()  # MARK: helper exact
    run_thing()  # MARK: helper exact


def shadowed(run_thing):
    # A parameter shadows the import: still reported (conservative), still exact
    # because the name could be the import.
    run_thing()  # MARK: helper exact
