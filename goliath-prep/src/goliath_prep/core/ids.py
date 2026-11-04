# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
#
# GENERATED FILE, DO NOT EDIT. Generated from docs/prep/checks.json (the check
# catalogue) and docs/prep/gi-format.md section 12 (the GI reader's findings) by
# goliath-prep/tests/core/prep_ids_codegen.py. After changing either document, run
# from the repository root:
#     python goliath-prep/tests/core/prep_ids_codegen.py
# goliath-prep/tests/core/test_prep_ids.py fails while this file is stale.
"""Check ids of the Goliath safety model, as code constants (unit F04).

``docs/prep/checks.json`` is the normative check catalogue (safety-model
section 11.6); this module is generated from it, so code names a check by a
constant (``ids.W_SURF``) instead of a string that could be mistyped. Every id
in the catalogue is here and nothing else is (``test_prep_doc_ids.py``).

* one constant per check id, named after the id with ``-`` replaced by ``_``;
* :data:`ALL`: every id, in catalogue order;
* :data:`GI_READER_IDS`: the ids the GI reader reports (gi-format section 12);
* :data:`SEVERITY`, :data:`APPLIES_TO`, :data:`SLACK_UNIT`,
  :data:`WITNESS_FIELDS`, :data:`DEFINED_IN`: the catalogue's columns, by id
  (``SLACK_UNIT`` is None for a check without a slack);
* :data:`CATALOGUE_SHA256`: SHA-256 of the catalogue's canonical JSON
  (:func:`goliath_prep.core.canon.canonical_json`), naming the catalogue
  version this code implements.
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

CATALOGUE_SHA256: Final = "058e0e916a81b0cf26ffe333f2914f4e3e273f62666dde5baf8e06c317e5cf9a"

# W: wheel
W_AREA: Final = "W-AREA"
W_TOWER: Final = "W-TOWER"
W_SURF: Final = "W-SURF"
W_SAFEZONE: Final = "W-SAFEZONE"
W_ROT: Final = "W-ROT"
W_RAPID: Final = "W-RAPID"
W_FIRST: Final = "W-FIRST"
W_PARK: Final = "W-PARK"
W_OBST: Final = "W-OBST"
W_VENDORFLOOR: Final = "W-VENDORFLOOR"

# B: body
B_TOWER: Final = "B-TOWER"
B_OBST: Final = "B-OBST"

# M: material and structure
M_ATTACH: Final = "M-ATTACH"
M_TOWERSEP: Final = "M-TOWERSEP"
M_INVARIANT: Final = "M-INVARIANT"

# T: tool
T_AREA: Final = "T-AREA"
T_TOWER: Final = "T-TOWER"
T_DEPTH: Final = "T-DEPTH"
T_ATTACH: Final = "T-ATTACH"
T_TOOL: Final = "T-TOOL"
T_OBST: Final = "T-OBST"
T_PASS: Final = "T-PASS"

# P: panel geometry
P_RANGE: Final = "P-RANGE"
P_REACH: Final = "P-REACH"
P_GEOM: Final = "P-GEOM"

# D: dialect and program
D_GI_VERSION: Final = "D-GI-VERSION"
D_GI_SYNTAX: Final = "D-GI-SYNTAX"
D_GI_HEADER: Final = "D-GI-HEADER"
D_GI_OP: Final = "D-GI-OP"
D_GI_CONFLICT: Final = "D-GI-CONFLICT"
D_SYNTAX: Final = "D-SYNTAX"
D_UNSUPPORTED: Final = "D-UNSUPPORTED"
D_UNITS: Final = "D-UNITS"
D_RANGE: Final = "D-RANGE"
D_ARC: Final = "D-ARC"
D_AFTER_END: Final = "D-AFTER-END"
D_MARKER: Final = "D-MARKER"
D_SINGLETOOL: Final = "D-SINGLETOOL"
D_ZSAFE: Final = "D-ZSAFE"
D_SPINDLE: Final = "D-SPINDLE"
D_CSTEP: Final = "D-CSTEP"
D_FEED: Final = "D-FEED"
D_WORDS: Final = "D-WORDS"
D_ROUNDTRIP: Final = "D-ROUNDTRIP"
D_SIZE: Final = "D-SIZE"

# F: firmware
F_GATE: Final = "F-GATE"

# G: feature gate
G_GATE: Final = "G-GATE"

# S: series
S_SERIES: Final = "S-SERIES"

# N: notices
N_GI_UNKNOWN_KEY: Final = "N-GI-UNKNOWN-KEY"
N_INFERRED_OPS: Final = "N-INFERRED-OPS"
N_OPS_MISMATCH: Final = "N-OPS-MISMATCH"
N_DUST: Final = "N-DUST"
N_VENDORC: Final = "N-VENDORC"
N_BASELINE: Final = "N-BASELINE"

# Every check id, in catalogue order.
ALL: Final[tuple[str, ...]] = (
    W_AREA,
    W_TOWER,
    W_SURF,
    W_SAFEZONE,
    W_ROT,
    W_RAPID,
    W_FIRST,
    W_PARK,
    W_OBST,
    W_VENDORFLOOR,
    B_TOWER,
    B_OBST,
    M_ATTACH,
    M_TOWERSEP,
    M_INVARIANT,
    T_AREA,
    T_TOWER,
    T_DEPTH,
    T_ATTACH,
    T_TOOL,
    T_OBST,
    T_PASS,
    P_RANGE,
    P_REACH,
    P_GEOM,
    D_GI_VERSION,
    D_GI_SYNTAX,
    D_GI_HEADER,
    D_GI_OP,
    D_GI_CONFLICT,
    D_SYNTAX,
    D_UNSUPPORTED,
    D_UNITS,
    D_RANGE,
    D_ARC,
    D_AFTER_END,
    D_MARKER,
    D_SINGLETOOL,
    D_ZSAFE,
    D_SPINDLE,
    D_CSTEP,
    D_FEED,
    D_WORDS,
    D_ROUNDTRIP,
    D_SIZE,
    F_GATE,
    G_GATE,
    S_SERIES,
    N_GI_UNKNOWN_KEY,
    N_INFERRED_OPS,
    N_OPS_MISMATCH,
    N_DUST,
    N_VENDORC,
    N_BASELINE,
)

# The ids the GI reader reports (gi-format section 12), in that table's order.
GI_READER_IDS: Final[tuple[str, ...]] = (
    D_GI_VERSION,
    D_GI_SYNTAX,
    D_GI_HEADER,
    D_GI_OP,
    D_GI_CONFLICT,
    N_GI_UNKNOWN_KEY,
    D_SYNTAX,
    D_UNSUPPORTED,
    D_UNITS,
    D_RANGE,
    D_ARC,
    D_AFTER_END,
    D_MARKER,
    D_SINGLETOOL,
    G_GATE,
)

# Severity of each check: refuse, warn, disclose or internal-error (safety-model 11.1).
SEVERITY: Final[Mapping[str, str]] = MappingProxyType(
    {
        W_AREA: "refuse",
        W_TOWER: "refuse",
        W_SURF: "refuse",
        W_SAFEZONE: "refuse",
        W_ROT: "refuse",
        W_RAPID: "refuse",
        W_FIRST: "refuse",
        W_PARK: "refuse",
        W_OBST: "refuse",
        W_VENDORFLOOR: "internal-error",
        B_TOWER: "refuse",
        B_OBST: "refuse",
        M_ATTACH: "refuse",
        M_TOWERSEP: "refuse",
        M_INVARIANT: "internal-error",
        T_AREA: "refuse",
        T_TOWER: "refuse",
        T_DEPTH: "refuse",
        T_ATTACH: "refuse",
        T_TOOL: "refuse",
        T_OBST: "refuse",
        T_PASS: "warn",
        P_RANGE: "refuse",
        P_REACH: "refuse",
        P_GEOM: "refuse",
        D_GI_VERSION: "refuse",
        D_GI_SYNTAX: "refuse",
        D_GI_HEADER: "refuse",
        D_GI_OP: "refuse",
        D_GI_CONFLICT: "refuse",
        D_SYNTAX: "refuse",
        D_UNSUPPORTED: "refuse",
        D_UNITS: "refuse",
        D_RANGE: "refuse",
        D_ARC: "refuse",
        D_AFTER_END: "refuse",
        D_MARKER: "refuse",
        D_SINGLETOOL: "refuse",
        D_ZSAFE: "refuse",
        D_SPINDLE: "refuse",
        D_CSTEP: "refuse",
        D_FEED: "refuse",
        D_WORDS: "refuse",
        D_ROUNDTRIP: "refuse",
        D_SIZE: "refuse",
        F_GATE: "refuse",
        G_GATE: "refuse",
        S_SERIES: "refuse",
        N_GI_UNKNOWN_KEY: "warn",
        N_INFERRED_OPS: "disclose",
        N_OPS_MISMATCH: "warn",
        N_DUST: "disclose",
        N_VENDORC: "disclose",
        N_BASELINE: "disclose",
    }
)

# Scope of each check (safety-model 11.1).
APPLIES_TO: Final[Mapping[str, str]] = MappingProxyType(
    {
        W_AREA: "piece",
        W_TOWER: "piece",
        W_SURF: "piece",
        W_SAFEZONE: "tool-point",
        W_ROT: "link",
        W_RAPID: "link",
        W_FIRST: "link",
        W_PARK: "program-end",
        W_OBST: "piece",
        W_VENDORFLOOR: "cut",
        B_TOWER: "piece",
        B_OBST: "piece",
        M_ATTACH: "piece",
        M_TOWERSEP: "program",
        M_INVARIANT: "startup",
        T_AREA: "in-material-piece",
        T_TOWER: "piece",
        T_DEPTH: "piece",
        T_ATTACH: "in-material-piece",
        T_TOOL: "tool",
        T_OBST: "piece",
        T_PASS: "in-material-piece",
        P_RANGE: "panel",
        P_REACH: "piece",
        P_GEOM: "job-time",
        D_GI_VERSION: "source-line",
        D_GI_SYNTAX: "source-line",
        D_GI_HEADER: "source-line",
        D_GI_OP: "source-line",
        D_GI_CONFLICT: "source-line",
        D_SYNTAX: "source-line",
        D_UNSUPPORTED: "source-line",
        D_UNITS: "source-line",
        D_RANGE: "source-line",
        D_ARC: "source-line",
        D_AFTER_END: "source-line",
        D_MARKER: "source-line",
        D_SINGLETOOL: "source-line",
        D_ZSAFE: "reparsed-bytes",
        D_SPINDLE: "reparsed-bytes",
        D_CSTEP: "reparsed-bytes",
        D_FEED: "reparsed-bytes",
        D_WORDS: "reparsed-bytes",
        D_ROUNDTRIP: "reparsed-bytes",
        D_SIZE: "program",
        F_GATE: "job-time",
        G_GATE: "program",
        S_SERIES: "series",
        N_GI_UNKNOWN_KEY: "source-line",
        N_INFERRED_OPS: "program",
        N_OPS_MISMATCH: "program",
        N_DUST: "program",
        N_VENDORC: "cut",
        N_BASELINE: "tool-point",
    }
)

# Unit of each check's slack; None for a check without one (safety-model 11.2).
SLACK_UNIT: Final[Mapping[str, str | None]] = MappingProxyType(
    {
        W_AREA: "mm",
        W_TOWER: "mm",
        W_SURF: "mm",
        W_SAFEZONE: "mm",
        W_ROT: "mm",
        W_RAPID: "mm",
        W_FIRST: "mm",
        W_PARK: "mm",
        W_OBST: "mm",
        W_VENDORFLOOR: None,
        B_TOWER: "mm",
        B_OBST: "mm",
        M_ATTACH: "mm",
        M_TOWERSEP: None,
        M_INVARIANT: "mm",
        T_AREA: "mm",
        T_TOWER: "mm",
        T_DEPTH: "mm",
        T_ATTACH: "mm",
        T_TOOL: None,
        T_OBST: "mm",
        T_PASS: "mm",
        P_RANGE: "mm",
        P_REACH: "mm",
        P_GEOM: None,
        D_GI_VERSION: None,
        D_GI_SYNTAX: None,
        D_GI_HEADER: None,
        D_GI_OP: None,
        D_GI_CONFLICT: None,
        D_SYNTAX: None,
        D_UNSUPPORTED: None,
        D_UNITS: None,
        D_RANGE: None,
        D_ARC: None,
        D_AFTER_END: None,
        D_MARKER: None,
        D_SINGLETOOL: None,
        D_ZSAFE: None,
        D_SPINDLE: None,
        D_CSTEP: None,
        D_FEED: None,
        D_WORDS: None,
        D_ROUNDTRIP: None,
        D_SIZE: "byte",
        F_GATE: None,
        G_GATE: None,
        S_SERIES: None,
        N_GI_UNKNOWN_KEY: None,
        N_INFERRED_OPS: None,
        N_OPS_MISMATCH: None,
        N_DUST: None,
        N_VENDORC: None,
        N_BASELINE: "mm",
    }
)

# The witness fields each check fills (safety-model 11.3).
WITNESS_FIELDS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        W_AREA: ("piece", "cut", "src", "wheel", "point", "hazard", "slack_mm", "raw_mm", "margins", "heading_c"),
        W_TOWER: (
            "piece",
            "cut",
            "src",
            "wheel",
            "point",
            "hazard",
            "slack_mm",
            "raw_mm",
            "margins",
            "heading_c",
            "tower2_placement",
        ),
        W_SURF: ("piece", "cut", "src", "wheel", "point", "hazard", "slack_mm", "raw_mm", "margins", "heading_c"),
        W_SAFEZONE: (
            "piece",
            "cut",
            "src",
            "point",
            "hazard",
            "slack_mm",
            "raw_mm",
            "margins",
            "rule",
            "gate",
            "unlock_rung",
        ),
        W_ROT: (
            "piece",
            "cut",
            "src",
            "wheel",
            "point",
            "hazard",
            "slack_mm",
            "raw_mm",
            "margins",
            "heading_c",
            "rule",
        ),
        W_RAPID: (
            "piece",
            "cut",
            "src",
            "wheel",
            "point",
            "hazard",
            "slack_mm",
            "raw_mm",
            "margins",
            "heading_c",
            "rule",
        ),
        W_FIRST: (
            "piece",
            "cut",
            "src",
            "wheel",
            "point",
            "hazard",
            "slack_mm",
            "raw_mm",
            "margins",
            "heading_c",
            "rule",
            "fact",
        ),
        W_PARK: ("piece", "cut", "src", "wheel", "point", "hazard", "slack_mm", "raw_mm", "margins", "fact"),
        W_OBST: ("piece", "cut", "src", "wheel", "point", "hazard", "slack_mm", "raw_mm", "margins", "heading_c"),
        W_VENDORFLOOR: ("cut", "src", "heading_c", "hazard"),
        B_TOWER: (
            "piece",
            "cut",
            "src",
            "point",
            "hazard",
            "slack_mm",
            "raw_mm",
            "margins",
            "heading_c",
            "tower2_placement",
        ),
        B_OBST: ("piece", "cut", "src", "point", "hazard", "slack_mm", "raw_mm", "margins", "heading_c"),
        M_ATTACH: (
            "piece",
            "cut",
            "src",
            "wheel",
            "point",
            "hazard",
            "slack_mm",
            "raw_mm",
            "margins",
            "heading_c",
            "island",
        ),
        M_TOWERSEP: ("piece", "cut", "src", "island", "hazard", "tower2_placement"),
        M_INVARIANT: ("value", "limit", "hazard", "slack_mm"),
        T_AREA: ("piece", "cut", "src", "point", "hazard", "slack_mm", "raw_mm", "margins"),
        T_TOWER: ("piece", "cut", "src", "point", "hazard", "slack_mm", "raw_mm", "margins", "tower2_placement"),
        T_DEPTH: ("piece", "cut", "src", "point", "value", "limit", "hazard", "slack_mm"),
        T_ATTACH: ("piece", "cut", "src", "point", "island", "hazard", "slack_mm"),
        T_TOOL: ("src", "value", "limit", "hazard"),
        T_OBST: ("piece", "cut", "src", "point", "value", "limit", "hazard", "slack_mm"),
        T_PASS: ("piece", "cut", "src", "point", "value", "limit", "hazard", "slack_mm"),
        P_RANGE: ("value", "limit", "hazard", "slack_mm"),
        P_REACH: ("piece", "cut", "src", "point", "hazard", "slack_mm", "tower2_placement", "fact"),
        P_GEOM: ("value", "limit", "fact", "hazard"),
        D_GI_VERSION: ("src", "column", "hazard"),
        D_GI_SYNTAX: ("src", "column", "hazard"),
        D_GI_HEADER: ("src", "column", "hazard"),
        D_GI_OP: ("src", "column", "hazard"),
        D_GI_CONFLICT: ("src", "sources", "hazard"),
        D_SYNTAX: ("src", "column", "hazard"),
        D_UNSUPPORTED: ("src", "column", "hazard"),
        D_UNITS: ("src", "column", "hazard"),
        D_RANGE: ("src", "column", "hazard"),
        D_ARC: ("src", "column", "hazard"),
        D_AFTER_END: ("src", "column", "hazard"),
        D_MARKER: ("src", "column", "hazard"),
        D_SINGLETOOL: ("src", "value", "hazard"),
        D_ZSAFE: ("src", "value", "limit", "hazard"),
        D_SPINDLE: ("src", "hazard"),
        D_CSTEP: ("src", "value", "limit", "heading_c", "hazard"),
        D_FEED: ("src", "value", "limit", "hazard"),
        D_WORDS: ("src", "column", "value", "hazard"),
        D_ROUNDTRIP: ("src", "value", "limit", "hazard"),
        D_SIZE: ("value", "limit", "gate", "unlock_rung", "hazard"),
        F_GATE: ("value", "limit", "gate", "fact", "hazard"),
        G_GATE: ("src", "gate", "unlock_rung", "hazard"),
        S_SERIES: ("value", "limit", "gate", "unlock_rung", "fact", "hazard"),
        N_GI_UNKNOWN_KEY: ("src", "hazard"),
        N_INFERRED_OPS: ("hazard",),
        N_OPS_MISMATCH: ("cut", "src", "value", "hazard"),
        N_DUST: ("value", "limit", "hazard"),
        N_VENDORC: ("cut", "src", "heading_c", "rule", "hazard"),
        N_BASELINE: ("piece", "cut", "src", "point", "hazard", "slack_mm"),
    }
)

# The section of docs/prep/ that defines each check.
DEFINED_IN: Final[Mapping[str, str]] = MappingProxyType(
    {
        W_AREA: "safety-model.md#62-wheel-work-area-and-per-edge-margins",
        W_TOWER: "safety-model.md#51-keep-out-zones-at-both-tower-2-placements",
        W_SURF: "safety-model.md#72-wheels-over-removed-material",
        W_SAFEZONE: "safety-model.md#81-conditions",
        W_ROT: "safety-model.md#93-rotation-sweeps",
        W_RAPID: "safety-model.md#91-the-link-shape",
        W_FIRST: "safety-model.md#95-the-first-line-w-first",
        W_PARK: "safety-model.md#96-after-the-last-line-w-park",
        W_OBST: "safety-model.md#65-obstacles-and-fixations",
        W_VENDORFLOOR: "safety-model.md#10-heading-selection",
        B_TOWER: "safety-model.md#52-tower-bases",
        B_OBST: "safety-model.md#65-obstacles-and-fixations",
        M_ATTACH: "safety-model.md#74-structure-attached-and-detached-material",
        M_TOWERSEP: "safety-model.md#74-structure-attached-and-detached-material",
        M_INVARIANT: "safety-model.md#73-why-pieces-suffice-the-exactness-invariant",
        T_AREA: "safety-model.md#63-tool-work-area",
        T_TOWER: "safety-model.md#51-keep-out-zones-at-both-tower-2-placements",
        T_DEPTH: "safety-model.md#32-z",
        T_ATTACH: "safety-model.md#74-structure-attached-and-detached-material",
        T_TOOL: "safety-model.md#116-the-check-catalogue",
        T_OBST: "safety-model.md#65-obstacles-and-fixations",
        T_PASS: "safety-model.md#71-removal",
        P_RANGE: "safety-model.md#64-range-and-wire-reach",
        P_REACH: "safety-model.md#64-range-and-wire-reach",
        P_GEOM: "safety-model.md#12-job-time-geometry-and-firmware",
        D_GI_VERSION: "gi-format.md#12-findings",
        D_GI_SYNTAX: "gi-format.md#12-findings",
        D_GI_HEADER: "gi-format.md#12-findings",
        D_GI_OP: "gi-format.md#12-findings",
        D_GI_CONFLICT: "gi-format.md#12-findings",
        D_SYNTAX: "gi-format.md#12-findings",
        D_UNSUPPORTED: "gi-format.md#12-findings",
        D_UNITS: "gi-format.md#12-findings",
        D_RANGE: "gi-format.md#12-findings",
        D_ARC: "gi-format.md#12-findings",
        D_AFTER_END: "gi-format.md#12-findings",
        D_MARKER: "gi-format.md#12-findings",
        D_SINGLETOOL: "safety-model.md#116-the-check-catalogue",
        D_ZSAFE: "safety-model.md#115-checks-on-the-shipped-bytes",
        D_SPINDLE: "safety-model.md#115-checks-on-the-shipped-bytes",
        D_CSTEP: "safety-model.md#92-the-180-degree-step-rule-and-the-cumulative-rotation-bound",
        D_FEED: "safety-model.md#115-checks-on-the-shipped-bytes",
        D_WORDS: "safety-model.md#115-checks-on-the-shipped-bytes",
        D_ROUNDTRIP: "safety-model.md#115-checks-on-the-shipped-bytes",
        D_SIZE: "safety-model.md#115-checks-on-the-shipped-bytes",
        F_GATE: "safety-model.md#12-job-time-geometry-and-firmware",
        G_GATE: "safety-model.md#14-unknowns-gates-and-rungs-this-model-depends-on",
        S_SERIES: "safety-model.md#13-series-jobs",
        N_GI_UNKNOWN_KEY: "gi-format.md#12-findings",
        N_INFERRED_OPS: "safety-model.md#34-operations-and-through-cuts",
        N_OPS_MISMATCH: "safety-model.md#34-operations-and-through-cuts",
        N_DUST: "safety-model.md#116-the-check-catalogue",
        N_VENDORC: "safety-model.md#10-heading-selection",
        N_BASELINE: "safety-model.md#62-wheel-work-area-and-per-edge-margins",
    }
)
