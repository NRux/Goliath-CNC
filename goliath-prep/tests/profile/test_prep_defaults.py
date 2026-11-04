# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The shipped defaults of the parameter catalogue, with their provenance (unit P01).

docs/prep/params.json is the normative catalogue; goliath_prep.defaults holds the
same values in code (the package cannot read docs/ once installed), and these
tests keep the two equal. Spec 08's constants are checked against the text of
docs/protocol/08 itself, and the tower-2 mapping against specs 02 and 04 and
against goliath-link's own command builder.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from goliath_prep import defaults
from goliath_prep.core.disclosure import SEVERITY_RANK
from goliath_prep.core.params import DISCLOSED_SOURCES, SOURCES, Param
from goliath_prep.core.types import FootprintSet

REPO = Path(__file__).resolve().parents[3]
PARAMS_JSON = REPO / "docs" / "prep" / "params.json"
SPEC02 = REPO / "docs" / "protocol" / "02-message-reference.md"
SPEC04 = REPO / "docs" / "protocol" / "04-connection-and-provisioning.md"
SPEC08 = REPO / "docs" / "protocol" / "08-head-angle-and-cut-order.md"


def catalogue() -> dict[str, dict]:
    entries = json.loads(PARAMS_JSON.read_text(encoding="utf-8"))
    return {e["name"]: e for e in entries}


def spec08_constants() -> dict[str, str]:
    """The rows of spec 08's Constants table: constant name -> value text."""
    text = SPEC08.read_text(encoding="utf-8")
    section = text.split("## Constants", 1)[1].split("\n## ", 1)[0]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[0] not in ("Constant", "---"):
            rows[cells[0]] = cells[1]
    return rows


# ---------------------------------------------------------------------------
# Spec 08
# ---------------------------------------------------------------------------


def test_spec_constants_equal_spec08() -> None:
    p = defaults.SPEC_PARAMS
    # The values the unit definition names: 402, 211, +-76 deg, 150, 155, the C grid 0..350 step 10.
    assert (p["support_r1"].value, p["support_r2"].value, p["support_r3"].value) == (402, 211, 211)
    assert (p["support_delta1"].value, p["support_delta2"].value, p["support_delta3"].value) == (0, 76, -76)
    assert p["tower_zone_leg_wheel"].value == 150
    assert p["tower_zone_leg_tool"].value == 155
    assert p["c_grid_step"].value == 10
    assert p["heading_offset"].value == 90
    assert defaults.C_GRID == tuple(range(0, 360, 10))
    assert defaults.C_GRID[0] == 0 and defaults.C_GRID[-1] == 350 and len(defaults.C_GRID) == 36
    for name in ("support_r1", "support_r2", "support_r3", "support_delta1", "support_delta2", "support_delta3",
                 "tower_zone_leg_wheel", "tower_zone_leg_tool", "c_grid_step", "heading_offset"):
        assert p[name].source == "SPEC", name

    # ... and against the text of spec 08 itself.
    rows = spec08_constants()
    assert rows["Rear support distance"] == "402 mm"
    assert rows["Side support distance"] == "211 mm"
    assert rows["Side support angle"] == "±76° from the heading"
    assert rows["Wheel tower zone"] == "150 mm legs"
    assert rows["Toolpath tower zone"] == "155 mm legs"
    assert rows["Heading offset"] == "+90°"
    assert rows["`TANK_VOLUME`"] == "1.15 dm³"
    assert p["tank_volume"].value == 1.15 and p["tank_volume"].unit == "dm3"
    text = SPEC08.read_text(encoding="utf-8")
    assert "q1 = p − 402 · u(θ)" in text
    assert "q2 = p − 211 · u(θ + 76°)" in text
    assert "q3 = p − 211 · u(θ − 76°)" in text
    assert "every emitted value is a multiple of 10 in `[0, 350]`" in text
    assert "θ = (C + 90) mod 360" in text


def test_vendor_footprint_is_the_spec_footprint() -> None:
    fp = defaults.vendor_footprint()
    assert isinstance(fp, FootprintSet)
    assert fp.vendor == ((402.0, 0.0), (211.0, 76.0), (211.0, -76.0))
    assert fp.measured is None and fp.active == "vendor"


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


def test_spec_params_match_the_catalogue() -> None:
    cat = catalogue()
    covered = set(defaults.SPEC_PARAMS) | set(defaults.NOT_IN_SPEC_PARAMS)
    assert covered == set(cat), "every catalogue parameter is either in SPEC_PARAMS or excluded with a reason"
    assert not set(defaults.SPEC_PARAMS) & set(defaults.NOT_IN_SPEC_PARAMS)
    for name, param in defaults.SPEC_PARAMS.items():
        entry = cat[name]
        assert param.source == entry["provenance"], name
        assert param.ref == "docs/prep/params.json %s" % name
        if entry["unit"] == "enum":
            assert param.unit == "enum" and param.value == entry["default"]
            assert param.value in entry["choices"]
        elif param.unit == "um":
            # values finer than the canonical 0.001 grid are held in micrometres (core.params)
            assert entry["unit"] == "mm", name
            assert Decimal(repr(param.value)) / 1000 == Decimal(repr(entry["default"])), name
        else:
            assert param.unit == entry["unit"], name
            assert param.value == entry["default"], name
            assert type(param.value) is (int if param.unit == "byte" else float), name
        assert entry["provenance"] not in ("DERIVED", "OWNER_JOB"), name


def test_excluded_parameters_are_derived_job_inputs_or_need_the_job() -> None:
    cat = catalogue()
    for name, reason in defaults.NOT_IN_SPEC_PARAMS.items():
        assert reason.strip(), name
        entry = cat[name]
        assert entry["provenance"] in ("DERIVED", "OWNER_JOB") or entry["default"] is None, name
    # every derived and every job parameter is excluded: none has a value that holds without its inputs
    for name, entry in cat.items():
        if entry["provenance"] in ("DERIVED", "OWNER_JOB"):
            assert name in defaults.NOT_IN_SPEC_PARAMS, name


def test_fallback_defaults_carry_their_disclosure() -> None:
    for name, param in defaults.SPEC_PARAMS.items():
        needed = DISCLOSED_SOURCES.get(param.source)
        if needed is None:
            continue
        assert param.disclosures, name
        assert max(d.rank for d in param.disclosures) >= SEVERITY_RANK[needed], name
    r_w = defaults.SPEC_PARAMS["r_w"]
    assert r_w.source == "DEFAULT_UNMEASURED" and r_w.value == 40.0
    (d,) = r_w.disclosures
    assert d.severity == "amber" and "UNMEASURED MACHINE PROFILE" in d.text
    assert d.id == "DEFAULT_UNMEASURED:r_w"
    reach = defaults.SPEC_PARAMS["reach_max"]
    assert reach.source == "INFERRED" and reach.value == 3500.0
    assert any("U-40" in x.text for x in reach.disclosures)


def test_spec_params_are_read_only_params() -> None:
    assert all(isinstance(p, Param) and p.source in SOURCES for p in defaults.SPEC_PARAMS.values())
    with pytest.raises(TypeError):
        defaults.SPEC_PARAMS["r_w"] = defaults.SPEC_PARAMS["m_w"]  # type: ignore[index]
    for p in defaults.SPEC_PARAMS.values():
        p.to_json()  # every default is exact in canonical JSON


# ---------------------------------------------------------------------------
# Tower 2
# ---------------------------------------------------------------------------


def test_set_t2_position_mapping_matches_specs_02_and_04() -> None:
    assert dict(defaults.SET_T2_POSITION) == {"X": 1, "Y": 0}
    assert "| `SET T2 POSITION;{0}` | Second tower axis: 1 = T2 along X, 0 = along Y |" in SPEC02.read_text(
        encoding="utf-8")
    spec04 = re.sub(r"\s+", " ", SPEC04.read_text(encoding="utf-8"))
    assert "`SET T2 POSITION;<1|0>` (1 = T2 is along the X axis relative to T1, 0 = along Y)" in spec04
    from goliath_link.protocol import Commands

    for axis, number in defaults.SET_T2_POSITION.items():
        assert Commands.set_t2_position(axis == "X") == "SET T2 POSITION;%d" % number


# ---------------------------------------------------------------------------
# Owner values only tighten
# ---------------------------------------------------------------------------


def test_owner_value_only_tightens() -> None:
    m_edge = defaults.owner_value("m_edge", 60.0, source="OWNER_PROFILE", ref="machine.toml policy.m_edge")
    assert (m_edge.value, m_edge.unit, m_edge.source) == (60.0, "mm", "OWNER_PROFILE")
    assert m_edge.disclosures and "60" in m_edge.disclosures[0].text
    same = defaults.owner_value("m_edge", 50.0, source="OWNER_PROFILE", ref="machine.toml policy.m_edge")
    assert same.value == 50.0
    with pytest.raises(defaults.InputFileError, match="tighten"):
        defaults.owner_value("m_edge", 49.999, source="OWNER_PROFILE", ref="machine.toml policy.m_edge")
    reach = defaults.owner_value("reach_max", 3000.0, source="OWNER_PROFILE", ref="machine.toml policy.reach_max")
    assert reach.value == 3000.0
    with pytest.raises(defaults.InputFileError, match="tighten"):
        defaults.owner_value("reach_max", 3600.0, source="OWNER_PROFILE", ref="machine.toml policy.reach_max")
    with pytest.raises(defaults.InputFileError, match="greater than 0"):
        defaults.owner_value("reach_max", 0.0, source="OWNER_PROFILE", ref="machine.toml policy.reach_max")
    with pytest.raises(defaults.InputFileError, match="100"):
        defaults.owner_value("battery_ack_below", 101.0, source="OWNER_PROFILE", ref="machine.toml policy.x")
    feed = defaults.owner_value("feed_cap", 1500.0, source="OWNER_JOB", ref="job.toml feeds.feed_cap")
    assert feed.source == "OWNER_JOB" and feed.unit == "mm/min"
    with pytest.raises(KeyError):
        defaults.owner_value("geom_tol", 20.0, source="OWNER_PROFILE", ref="machine.toml policy.geom_tol")


def test_every_tightening_direction_names_a_catalogue_default() -> None:
    for name, direction in defaults.OWNER_TIGHTENS.items():
        assert direction in ("higher", "lower"), name
        assert name in defaults.SPEC_PARAMS, name
        assert defaults.SPEC_PARAMS[name].source in ("POLICY", "GATE", "INFERRED"), name


# ---------------------------------------------------------------------------
# Numbers are read exactly as written
# ---------------------------------------------------------------------------

THOUSANDTHS = st.integers(min_value=-(10**15) + 1, max_value=10**15 - 1)


@given(THOUSANDTHS)
def test_three_decimal_numbers_read_exactly(thousandths: int) -> None:
    written = Decimal(thousandths).scaleb(-3)
    assert defaults.exact_number(written) == written
    as_float = float(written)
    # the decimal as written survives the float a Param holds: GI agreement compares decimals as written
    assert Decimal(repr(as_float)) == written
    assert defaults.exact_number(as_float) == written


@given(THOUSANDTHS, st.integers(min_value=1, max_value=9))
def test_a_fourth_decimal_is_refused(thousandths: int, extra: int) -> None:
    written = Decimal(thousandths).scaleb(-3) + Decimal(extra).scaleb(-4)
    with pytest.raises(ValueError, match="0.001"):
        defaults.exact_number(written)


def test_exact_number_refuses_what_is_not_a_finite_number() -> None:
    for value in (True, "1", None, [1], Decimal("NaN"), Decimal("Infinity"), float("nan"), float("-inf")):
        with pytest.raises((TypeError, ValueError)):
            defaults.exact_number(value)
    with pytest.raises(ValueError, match="large"):
        defaults.exact_number(10**12)
    assert defaults.exact_number(-(10**12) + 1) == Decimal(-(10**12) + 1)
