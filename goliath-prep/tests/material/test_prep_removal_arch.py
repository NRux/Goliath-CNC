# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Architecture of the material package (unit P06): the same numbers on every platform and Python.

As for the geometry kernel (unit P04): trigonometry only through the correctly
rounded ``goliath_prep.geom.footprint.unit``, no builtin ``sum``, ``math.fsum``,
``math.hypot``, ``math.dist`` or ``numpy.sum`` (their results differ between
Python versions or platforms), and no import of the runner.
"""
from __future__ import annotations

from pathlib import Path
from types import ModuleType

import goliath_prep.material as material

MATERIAL_DIR = str(Path(material.__file__).resolve().parent)


def test_no_platform_trigonometry(astscan: ModuleType) -> None:
    assert astscan.find_calls(MATERIAL_DIR, "unit")  # positive control: the correctly rounded unit vector
    for name in ("math.cos", "math.sin", "math.tan", "math.atan2", "math.acos", "math.asin", "math.radians",
                 "math.degrees", "numpy.cos", "numpy.sin"):
        assert astscan.find_calls(MATERIAL_DIR, name) == [], name


def test_no_version_dependent_float_reductions(astscan: ModuleType) -> None:
    assert astscan.find_calls(MATERIAL_DIR, "math.sqrt")  # positive control
    for name in ("sum", "fsum", "math.fsum", "math.hypot", "math.dist", "numpy.sum"):
        assert astscan.find_calls(MATERIAL_DIR, name) == [], name


def test_material_never_imports_the_runner(astscan: ModuleType) -> None:
    assert astscan.find_references(MATERIAL_DIR, "shapely")  # positive control
    assert astscan.find_references(MATERIAL_DIR, "goliath_run") == []
