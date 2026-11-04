# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The object behind the canonical-JSON golden bytes (unit F04).

Used by test_prep_canon.py in its own process and in fresh interpreters with a
different environment, so it is a module of its own, loaded from its path.
Run as a script, it writes the object's canonical JSON to standard output as
raw bytes.

It covers every case where an encoder could depend on the platform or on the
interpreter's state: dictionary insertion order and hash seed (key order),
float formatting (rounding boundaries, negative zero), line endings, text
encoding (non-ASCII, astral characters, control characters, U+2028), integers
beyond 64 bits, and the core value types' own JSON forms.
"""
from __future__ import annotations

import sys
from typing import Any


def golden_object() -> dict[str, Any]:
    from goliath_prep.core.disclosure import Disclosure
    from goliath_prep.core.findings import Finding, SourceRef, Witness
    from goliath_prep.core.params import Param

    unmeasured = Disclosure(
        id="DEFAULT_UNMEASURED:r_w",
        severity="amber",
        text="wheel contact radius r_w = 40 mm is not measured (field guide 04)",
        source="docs/prep/params.json r_w",
    )
    r_w = Param(40.0, "mm", "DEFAULT_UNMEASURED", "docs/prep/params.json r_w", disclosures=(unmeasured,))
    m_w = Param(15.0, "mm", "POLICY", "docs/prep/params.json m_w")
    rho = Param.derived(55.0, "mm", "rho = r_w + m_w (docs/prep/params.json rho)", inputs=(r_w, m_w))
    finding = Finding.of(
        "W-SURF",
        "rear wheel enters the kerf of cut 7",
        witness=Witness(
            piece=5522,
            cut=8,
            src=SourceRef("job.gi", 1234, "G1 X10 Y20 Z-18"),
            wheel=1,
            point=(1234.5675, -0.0004),
            hazard="kerf of cut 7, line 1234, depth 18.0 mm, removed at piece 5521",
            slack_mm=-0.0625,
            raw_mm=54.9375,
            margins={"m_w": 15.0, "e_j": 0.0005},
            heading_c=30,
        ),
        geometry_wkt="POINT (1234.5675 -0.0004)",
    )
    # Insertion orders are deliberately not sorted.
    return {
        "zeta": [True, False, None, 0, -7, 12345678901234567890],
        "floats": [0.0625, -0.0625, 0.1875, 1.0005, 0.0005, -0.0, -0.0004, 2.675, 0.1 + 0.2],
        "text": "Ø 6 mm \"flat\" end mill\\\n\r\t  \U0001f527",
        "keys": {"b": 1, "a": 2, "B": 3, "é": 4, "\U0001f600": 5, "￿": 6, "": 7},
        "empty": {"map": {}, "list": []},
        "params": {"rho": rho},
        "finding": finding,
    }


if __name__ == "__main__":
    import os

    # What a Windows interpreter would report; canonical_json must not use it.
    os.linesep = "\r\n"
    from goliath_prep.core.canon import canonical_json

    sys.stdout.buffer.write(canonical_json(golden_object()))
    sys.stdout.buffer.flush()
