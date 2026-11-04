# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Finding, Witness and SourceRef: validated against the check catalogue, and JSON round trip (unit F04)."""
from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from goliath_prep.core import ids
from goliath_prep.core.canon import canonical_json
from goliath_prep.core.findings import WITNESS_FIELDS, Finding, SourceRef, Witness

REPO = Path(__file__).resolve().parents[3]
CHECKS = json.loads((REPO / "docs" / "prep" / "checks.json").read_text(encoding="utf-8"))

SRC = SourceRef("cabinet-side.gi", 812, "G1 X1220.5 Y300 Z-18.2 F1500")


def surf_witness(**overrides: object) -> Witness:
    fields: dict[str, object] = dict(
        piece=5522,
        cut=8,
        src=SRC,
        wheel=1,
        point=(1234.567, 300.25),
        hazard="kerf of cut 7, line 1234, depth 18.0 mm, removed at piece 5521",
        slack_mm=-12.5,
        raw_mm=42.5,
        margins={"m_w": 15.0, "e_j": 0.0},
        heading_c=30,
    )
    fields.update(overrides)
    return Witness(**fields)  # type: ignore[arg-type]


def roundtrip(finding: Finding) -> Finding:
    data = canonical_json(finding)
    back = Finding.from_json(json.loads(data))
    assert canonical_json(back) == data
    return back


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def test_finding_roundtrip_json() -> None:
    findings = [
        Finding("W-SURF", "refuse", "rear wheel enters the kerf of cut 7 at line 812", surf_witness(),
                "LINESTRING (0 0, 10 10)"),
        Finding.of(
            "D-GI-CONFLICT",
            "STOCK t disagrees with job.toml panel.T",
            witness=Witness(
                src=SourceRef("job.gi", 4, ";GOLIATH STOCK x=2440 y=1220 t=18"),
                sources={"GI STOCK t": 18.0, "job.toml panel.T": 17.6},
                hazard="stock thickness declared twice with different values",
            ),
        ),
        Finding.of(
            "D-SYNTAX",
            "comma used as decimal separator",
            witness=Witness(src=SourceRef("job.gi", 12, "G1 X1,5"), column=5, hazard="X1,5"),
        ),
        Finding.of(
            "D-SIZE",
            "program is larger than the verified upload size",
            witness=Witness(
                value=40000, limit=32768, gate="max_program_bytes", unlock_rung="R10", hazard="40000 bytes"
            ),
        ),
        Finding.of(
            "W-TOWER",
            "left wheel meets the tower-2 zone",
            witness=surf_witness(tower2_placement="measured", heading_c=350),
        ),
        Finding.of("N-INFERRED-OPS", "operations inferred from Z", witness=None),
        Finding.of("N-DUST", "about 1.4 dm3 removed", witness=Witness(value=1.4, limit=1.15, hazard="dust bin")),
        Finding("W-SURF", "internal-error", "checker assertion failed during W-SURF",
                Witness(hazard="AssertionError: piece index went backwards"), None),
    ]
    for finding in findings:
        assert roundtrip(finding) == finding
    as_json = json.loads(canonical_json(findings[0]))
    assert set(as_json) == {"check_id", "severity", "message", "witness", "geometry_wkt"}
    assert as_json["witness"]["src"] == {"file": "cabinet-side.gi", "line": 812, "text": SRC.text}
    assert as_json["witness"]["margins"] == {"e_j": 0.0, "m_w": 15.0}
    assert json.loads(canonical_json(findings[5]))["witness"] is None


def test_witness_json_has_only_the_fields_it_fills() -> None:
    witness = Witness(src=SRC, column=3, hazard="h")
    assert set(witness.to_json()) == {"src", "column", "hazard"}
    assert witness.fields_set() == frozenset({"src", "column", "hazard"})


def test_values_off_the_grid_are_rounded_and_stay_stable() -> None:
    finding = Finding.of("W-SURF", "m", witness=surf_witness(slack_mm=-0.0625, point=(1.0005, -0.0004)))
    data = json.loads(canonical_json(finding))["witness"]
    # The slack rounds towards -infinity (never shown larger than it is); the
    # other numbers to the nearest 0.001, ties to even.
    assert data["slack_mm"] == -0.063 and data["point"] == [1.0, 0.0]
    roundtrip(finding)


@pytest.mark.parametrize("slack", [-0.0004, -0.0005, -1e-9, -5e-324, -0.0625, -12.5, -0.001])
def test_a_violated_slack_is_never_shown_as_held(slack: float) -> None:
    # safety-model 11.2: the slack is negative when violated. Rounded to the
    # nearest 0.001, a slack of -0.0004 would read 0.000 (held) in the report
    # of a refusal; it must still read negative, and never less violated than
    # it is by more than the rounding step.
    finding = Finding.of("W-SURF", "rear wheel over the kerf", witness=surf_witness(slack_mm=slack))
    shown = json.loads(canonical_json(finding))["witness"]["slack_mm"]
    assert shown < 0
    assert shown <= slack and slack - shown < 0.001 + 1e-12
    assert roundtrip(finding).witness.slack_mm == shown  # type: ignore[union-attr]


@pytest.mark.parametrize("slack", [0.0, 0.0004, 0.0629, 12.5, 0.062, 18.2])
def test_a_slack_that_holds_is_never_overstated(slack: float) -> None:
    finding = Finding.of("T-PASS", "step-down", witness=Witness(slack_mm=slack, hazard="pass depth"))
    shown = json.loads(canonical_json(finding))["witness"]["slack_mm"]
    assert 0 <= shown <= slack and slack - shown < 0.001
    if round(slack, 3) == slack:
        assert shown == slack  # on the grid: shown as it is


def test_from_json_is_strict() -> None:
    good = json.loads(canonical_json(Finding.of("W-SURF", "m", witness=surf_witness())))
    with pytest.raises(ValueError, match="keys"):
        Finding.from_json({**good, "extra": 1})
    with pytest.raises(ValueError, match="keys"):
        Finding.from_json({k: v for k, v in good.items() if k != "geometry_wkt"})
    with pytest.raises(ValueError, match="unknown witness field"):
        Witness.from_json({**good["witness"], "pieces": 3})
    with pytest.raises(ValueError, match="hazard"):
        Witness.from_json({k: v for k, v in good["witness"].items() if k != "hazard"})
    with pytest.raises(ValueError, match="keys"):
        SourceRef.from_json({"file": "a", "line": 1})


GRID = st.integers(min_value=-10**7, max_value=10**7).map(lambda n: n / 1000)
ANY = st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False)


@settings(max_examples=300, deadline=None)
@given(
    piece=st.one_of(st.none(), st.integers(min_value=0, max_value=10**9)),
    cut=st.one_of(st.none(), st.integers(min_value=0, max_value=10**6)),
    wheel=st.one_of(st.none(), st.sampled_from([1, 2, 3])),
    point=st.one_of(st.none(), st.tuples(ANY, ANY)),
    slack=st.one_of(st.none(), ANY),
    raw=st.one_of(st.none(), GRID),
    heading=st.one_of(st.none(), st.integers(min_value=0, max_value=35).map(lambda n: n * 10)),
    text=st.text(st.characters(exclude_categories=("Cs",)), min_size=1, max_size=20).filter(str.strip),
    exact=st.booleans(),
)
def test_finding_roundtrip_property(
    piece: int | None,
    cut: int | None,
    wheel: int | None,
    point: tuple[float, float] | None,
    slack: float | None,
    raw: float | None,
    heading: int | None,
    text: str,
    exact: bool,
) -> None:
    if exact and point is not None:
        point = (round(point[0], 3), round(point[1], 3))
    if exact and slack is not None:
        slack = round(slack, 3)
    witness = Witness(piece=piece, cut=cut, wheel=wheel, point=point, slack_mm=slack, raw_mm=raw,
                      heading_c=heading, src=SourceRef("f.gi", 1, text), hazard=text)
    finding = Finding.of("W-AREA", text, witness=witness, geometry_wkt=None)
    back = roundtrip(finding)
    if exact:
        assert back == finding


# ---------------------------------------------------------------------------
# The catalogue is the contract
# ---------------------------------------------------------------------------


def test_witness_fields_match_catalogue() -> None:
    # Every field a check lists is a Witness field, and Witness has no field no
    # check lists (safety-model section 11.3).
    listed = {field for entry in CHECKS for field in entry["witness_fields"]}
    assert set(WITNESS_FIELDS) == listed
    assert set(WITNESS_FIELDS) == {f.name for f in dataclasses.fields(Witness)}
    # The design's seven are among them.
    assert {"piece", "cut", "src", "wheel", "point", "hazard", "slack_mm"} <= set(WITNESS_FIELDS)


def test_finding_check_id_must_be_in_catalogue() -> None:
    for bad in ("W-NOPE", "w-area", "R-KERF", "", None):
        with pytest.raises((ValueError, TypeError), match="check"):
            Finding(bad, "refuse", "m", Witness(hazard="h"), None)  # type: ignore[arg-type]


def test_finding_severity_follows_catalogue() -> None:
    w = surf_witness()
    assert Finding.of("W-SURF", "m", witness=w).severity == "refuse"
    assert Finding.of("T-PASS", "m", witness=None).severity == "warn"
    assert Finding.of("N-DUST", "m", witness=None).severity == "disclose"
    assert Finding.of("M-INVARIANT", "m", witness=Witness(hazard="h")).severity == "internal-error"
    # A refusal can never be filed as a warning or a notice ...
    for weaker in ("warn", "disclose"):
        with pytest.raises(ValueError, match="severity"):
            Finding("W-SURF", weaker, "m", w, None)  # type: ignore[arg-type]
    # ... nor a warning as a refusal (the catalogue decides), ...
    with pytest.raises(ValueError, match="severity"):
        Finding("T-PASS", "refuse", "m", Witness(hazard="h"), None)
    # ... but any check can fail as an internal error of the checker.
    assert Finding("T-PASS", "internal-error", "m", Witness(hazard="h"), None).severity == "internal-error"
    with pytest.raises(ValueError, match="severity"):
        Finding("W-SURF", "fatal", "m", w, None)  # type: ignore[arg-type]


def test_refusal_needs_a_witness() -> None:
    with pytest.raises(ValueError, match="witness"):
        Finding.of("W-SURF", "m", witness=None)
    with pytest.raises(ValueError, match="witness"):
        Finding("W-SURF", "internal-error", "m", None, None)
    with pytest.raises(TypeError, match="Witness"):
        Finding.of("W-SURF", "m", witness={"hazard": "h"})  # type: ignore[arg-type]


def test_witness_carries_only_the_checks_fields() -> None:
    with pytest.raises(ValueError, match="wheel"):
        Finding.of("D-SYNTAX", "m", witness=Witness(src=SRC, column=1, wheel=2, hazard="h"))
    with pytest.raises(ValueError, match="slack_mm"):
        Finding.of("D-SIZE", "m", witness=Witness(slack_mm=-3.0, hazard="h"))  # its slack is in bytes
    Finding.of("W-SURF", "m", witness=surf_witness(wheel=None))  # a listed field may be unknown


def test_message_and_wkt() -> None:
    for message in ("", "  ", None):
        with pytest.raises((ValueError, TypeError), match="message"):
            Finding("W-SURF", "refuse", message, surf_witness(), None)  # type: ignore[arg-type]
    for wkt in ("", 3):
        with pytest.raises((ValueError, TypeError), match="geometry_wkt"):
            Finding("W-SURF", "refuse", "m", surf_witness(), wkt)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "fields, error, message",
    [
        ({"hazard": ""}, ValueError, "hazard"),
        ({"hazard": None}, TypeError, "hazard"),
        ({"hazard": "h", "wheel": 4}, ValueError, "wheel"),
        ({"hazard": "h", "wheel": 0}, ValueError, "wheel"),
        ({"hazard": "h", "piece": -1}, ValueError, "piece"),
        ({"hazard": "h", "piece": True}, TypeError, "piece"),
        ({"hazard": "h", "cut": 1.0}, TypeError, "cut"),
        ({"hazard": "h", "column": 0}, ValueError, "column"),
        ({"hazard": "h", "point": (1.0,)}, ValueError, "point"),
        ({"hazard": "h", "point": (1.0, math.nan)}, ValueError, "point"),
        ({"hazard": "h", "slack_mm": math.inf}, ValueError, "slack_mm"),
        ({"hazard": "h", "raw_mm": "3"}, TypeError, "raw_mm"),
        ({"hazard": "h", "margins": {"": 1.0}}, ValueError, "margins"),
        ({"hazard": "h", "margins": {"m_w": math.nan}}, ValueError, "margins"),
        ({"hazard": "h", "heading_c": math.nan}, ValueError, "heading_c"),
        ({"hazard": "h", "tower2_placement": "left"}, ValueError, "tower2_placement"),
        ({"hazard": "h", "rule": ""}, ValueError, "rule"),
        ({"hazard": "h", "value": [1, 2]}, TypeError, "value"),
        ({"hazard": "h", "limit": math.nan}, ValueError, "limit"),
        ({"hazard": "h", "sources": {"a": [1]}}, TypeError, "sources"),
        ({"hazard": "h", "island": -2}, ValueError, "island"),
        ({"hazard": "h", "fact": ""}, ValueError, "fact"),
        ({"hazard": "h", "src": "job.gi:12"}, TypeError, "src"),
    ],
)
def test_witness_validation(fields: dict, error: type, message: str) -> None:
    with pytest.raises(error, match=message):
        Witness(**fields)


def test_witness_is_frozen_hashable_and_normalised() -> None:
    w = Witness(point=[1, 2], margins={"b": 1, "a": 2}, slack_mm=3, hazard="h")
    assert w.point == (1.0, 2.0) and isinstance(w.point[0], float)
    assert w.margins == (("a", 2.0), ("b", 1.0))
    assert isinstance(w.slack_mm, float)
    hash(w)
    with pytest.raises(dataclasses.FrozenInstanceError):
        w.hazard = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        Witness(1, 2, hazard="h")  # type: ignore[misc]  # keyword-only: piece and cut cannot be swapped


def test_source_ref_validation() -> None:
    assert SourceRef("a.gi", 1, "").line == 1
    with pytest.raises(ValueError, match="line"):
        SourceRef("a.gi", 0, "x")
    with pytest.raises(TypeError, match="line"):
        SourceRef("a.gi", True, "x")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="file"):
        SourceRef("", 1, "x")
    with pytest.raises(TypeError, match="text"):
        SourceRef("a.gi", 1, None)  # type: ignore[arg-type]


def test_every_catalogue_check_can_report_a_finding() -> None:
    # One finding per check id, with the catalogue's severity and only its
    # listed hazard field, so no id is unusable.
    for check_id in ids.ALL:
        witness = Witness(hazard="h") if "hazard" in ids.WITNESS_FIELDS[check_id] else None
        finding = Finding.of(check_id, "m", witness=witness)
        assert finding.severity == ids.SEVERITY[check_id]
        assert roundtrip(finding) == finding
