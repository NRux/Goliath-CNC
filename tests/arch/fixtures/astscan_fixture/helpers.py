# SPDX-License-Identifier: GPL-3.0-or-later
"""A module-level function the other fixture modules call through relative imports."""


def run_thing():
    return None


def calls_its_neighbour():
    run_thing()  # MARK: helper exact
