# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Material and structure over time (the project design, section 4.7; docs/prep/safety-model.md section 7).

* :mod:`~goliath_prep.material.removal` - the removal index (unit P06): what
  each in-material piece removes, the time-exact W-SURF query "how far is this
  wheel from the material removed before piece j", the prior removal declared
  in job.toml that seeds Rem(0), the piece length Delta and the time invariant
  that makes the query exact, and the relaxation R-KERF (only with the R8k
  fact).
"""
from __future__ import annotations

from goliath_prep.material.removal import (
    KERF_PARAMS,
    SEARCH_MM,
    TOUCH_TOL_MM,
    Clearance,
    InvariantError,
    KerfRelaxation,
    PriorRegion,
    PriorRemoval,
    RemovalIndex,
    assert_time_invariant,
    delta_for,
    derive_delta,
    r_tool_param,
)

__all__ = [
    "KERF_PARAMS",
    "SEARCH_MM",
    "TOUCH_TOL_MM",
    "Clearance",
    "InvariantError",
    "KerfRelaxation",
    "PriorRegion",
    "PriorRemoval",
    "RemovalIndex",
    "assert_time_invariant",
    "delta_for",
    "derive_delta",
    "r_tool_param",
]
