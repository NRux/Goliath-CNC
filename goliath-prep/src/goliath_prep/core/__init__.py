# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Core value types shared by goliath-prep and goliath-run (unit F04).

* :mod:`goliath_prep.core.params` - :class:`~goliath_prep.core.params.Param`,
  a value with its unit, its source (provenance class) and a reference to where
  it came from;
* :mod:`goliath_prep.core.disclosure` - disclosures and the registry every
  fallback is shown from;
* :mod:`goliath_prep.core.findings` - findings of the safety checks, their
  witnesses and source references;
* :mod:`goliath_prep.core.canon` - canonical JSON: the same input gives the same
  bytes on Linux and Windows;
* :mod:`goliath_prep.core.ids` - the check ids of ``docs/prep/checks.json`` as
  constants (generated);
* :mod:`goliath_prep.core.types` - the interface types shared by the parts of
  goliath-prep (unit F05): Pose, FootprintSet, EmitProfile, GateView and the
  all-closed desk view, MeasuredGeometry, Envelope, SessionBinding,
  CheckContext and RungExemption.

Import from the submodules; this package re-exports nothing.
"""
