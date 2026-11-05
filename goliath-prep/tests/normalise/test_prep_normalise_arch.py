# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Architecture of the normaliser and the time pieces (unit P03): the same numbers on every platform.

The pieces are the time coordinate of every check, and the normaliser writes
new points (helix segments, arc parts) into the program the robot runs, so
they follow the geometry kernel's rules (goliath-prep/tests/geom/
test_prep_geom_arch.py): cos and sin come from the correctly rounded
``goliath_prep.geom.footprint.unit``; no ``atan2``, ``acos``, ``asin``,
``hypot``, builtin ``sum`` or ``fsum`` (whose last bits differ between
platforms or Python versions); lengths with ``math.sqrt``, which IEEE 754
rounds correctly. Neither module imports goliath_run.
"""
from __future__ import annotations

from pathlib import Path
from types import ModuleType

import goliath_prep.ir as ir
import goliath_prep.normalise as normalise
import goliath_prep.pieces as pieces

FILES = [str(Path(module.__file__).resolve()) for module in (ir, normalise, pieces)]

BANNED = ("math.cos", "math.sin", "math.tan", "math.atan2", "math.atan", "math.acos", "math.asin", "math.radians",
          "math.degrees", "math.hypot", "math.dist", "math.fsum", "sum", "numpy.cos", "numpy.sin", "numpy.arctan2",
          "numpy.hypot", "numpy.sum")


def test_no_platform_dependent_float_maths(astscan: ModuleType) -> None:
    assert astscan.find_calls(FILES[2], "goliath_prep.geom.footprint.unit")  # positive control
    assert astscan.find_calls(FILES[2], "math.sqrt")  # positive control
    for path in FILES:
        for name in BANNED:
            assert astscan.find_calls(path, name) == [], (path, name)


def test_never_imports_the_runner(astscan: ModuleType) -> None:
    for path in FILES:
        assert astscan.find_references(path, "goliath_run") == [], path
