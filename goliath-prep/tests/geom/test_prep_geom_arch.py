# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Architecture of the geometry kernel (unit P04): the same numbers on every platform and Python.

* Trigonometry comes from goliath_prep.heading_vendor.correctly_rounded_cos_sin
  (through goliath_prep.geom.footprint.unit): the C library's cos and sin
  differ in the last bit between platforms.
* No builtin ``sum`` or ``math.fsum`` of floats (``sum`` is compensated since
  Python 3.12, so 3.11 and 3.14 would differ), and no ``math.hypot`` (its
  algorithm changed between Python versions): plain left-to-right sums and
  ``math.sqrt``, which IEEE 754 rounds correctly.
* The kernel is a library of goliath-prep: it never imports goliath_run.
"""
from __future__ import annotations

from pathlib import Path
from types import ModuleType

import goliath_prep.geom as geom

GEOM_DIR = str(Path(geom.__file__).resolve().parent)


def test_no_platform_trigonometry(astscan: ModuleType) -> None:
    assert astscan.find_calls(GEOM_DIR, "correctly_rounded_cos_sin")  # positive control
    for name in ("math.cos", "math.sin", "math.tan", "math.atan2", "math.acos", "math.asin", "math.radians",
                 "math.degrees", "numpy.cos", "numpy.sin"):
        assert astscan.find_calls(GEOM_DIR, name) == [], name


def test_no_version_dependent_float_reductions(astscan: ModuleType) -> None:
    assert astscan.find_calls(GEOM_DIR, "math.sqrt")  # positive control
    for name in ("sum", "fsum", "math.hypot", "math.dist", "numpy.sum"):
        assert astscan.find_calls(GEOM_DIR, name) == [], name


def test_kernel_never_imports_the_runner(astscan: ModuleType) -> None:
    assert astscan.find_references(GEOM_DIR, "shapely")  # positive control
    assert astscan.find_references(GEOM_DIR, "goliath_run") == []
