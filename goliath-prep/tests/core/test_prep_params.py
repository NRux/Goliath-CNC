# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Param: every value a check uses carries its source and a reference (unit F04, red-team PHYS-12)."""
from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
import math
from pathlib import Path
from types import ModuleType

import pytest

from goliath_prep.core.canon import canonical_json
from goliath_prep.core.disclosure import Disclosure, UndisclosedFallbackError
from goliath_prep.core.params import DISCLOSED_SOURCES, SOURCES, UNITS, Param

REPO = Path(__file__).resolve().parents[3]
PARAMS_JSON = REPO / "docs" / "prep" / "params.json"
DESIGN_SOURCES = {"SPEC", "ROBOT_MEASURED", "OWNER_PROFILE", "OWNER_JOB", "FACT", "DEFAULT_UNMEASURED", "DERIVED"}


def unmeasured(name: str = "r_w", severity: str = "amber") -> Disclosure:
    return Disclosure(
        id="DEFAULT_UNMEASURED:%s" % name,
        severity=severity,  # type: ignore[arg-type]
        text="%s is not measured: conservative default in use" % name,
        source="docs/prep/params.json %s" % name,
    )


def r_w() -> Param[float]:
    return Param(40.0, "mm", "DEFAULT_UNMEASURED", "docs/prep/params.json r_w", disclosures=(unmeasured(),))


def m_w() -> Param[float]:
    return Param(15.0, "mm", "POLICY", "docs/prep/params.json m_w")


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_param_requires_source_and_ref() -> None:
    with pytest.raises(TypeError):
        Param(18.0, "mm")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Param(18.0, "mm", "OWNER_JOB")  # type: ignore[call-arg]
    for ref in ("", "   ", None, 7):
        with pytest.raises((ValueError, TypeError), match="ref"):
            Param(18.0, "mm", "OWNER_JOB", ref)  # type: ignore[arg-type]
    for source in ("", "MEASURED", "spec", None, "DEFAULT"):
        with pytest.raises(ValueError, match="source"):
            Param(18.0, "mm", source, "job.toml panel.T")  # type: ignore[arg-type]
    good = Param(18.0, "mm", "OWNER_JOB", "job.toml panel.T")
    assert (good.value, good.unit, good.source, good.ref) == (18.0, "mm", "OWNER_JOB", "job.toml panel.T")


@pytest.mark.parametrize(
    "value, unit, error, message",
    [
        (None, "mm", ValueError, "no value"),
        (math.nan, "mm", ValueError, "finite"),
        (math.inf, "deg", ValueError, "finite"),
        (True, "mm", TypeError, "number"),
        ("18", "mm", TypeError, "number"),
        ((1.0, math.nan), "mm", ValueError, "finite"),
        ((), "mm", ValueError, "empty"),
        (3, "enum", TypeError, "string"),
        ("", "enum", ValueError, "empty"),
        (18.0, "", ValueError, "unit"),
        (18.0, "millimetre", ValueError, "unit"),
        (18.0, "MM", ValueError, "unit"),
        ({"a": 1}, "mm", TypeError, "number"),
    ],
)
def test_param_refuses_values_it_cannot_carry(value: object, unit: str, error: type, message: str) -> None:
    with pytest.raises(error, match=message):
        Param(value, unit, "OWNER_JOB", "job.toml x")


def test_lists_become_tuples_and_ints_stay_ints() -> None:
    p = Param([211.0, 76], "mm", "SPEC", "spec 08 Constants")
    assert p.value == (211.0, 76) and isinstance(p.value, tuple)
    assert isinstance(Param(2438, "mm", "OWNER_JOB", "job.toml panel.W").value, int)
    hash(p)  # frozen and hashable
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.value = (0.0,)  # type: ignore[misc]


def test_source_classes_are_the_parameter_catalogues() -> None:
    # docs/prep/parameters.md section 2: the catalogue's classes; section 2.1:
    # the sources that replace them at run time. The design's seven are all in.
    catalogue = {entry["provenance"] for entry in json.loads(PARAMS_JSON.read_text(encoding="utf-8"))}
    assert catalogue == {"SPEC", "DEFAULT_UNMEASURED", "INFERRED", "POLICY", "GATE", "DERIVED", "OWNER_JOB"}
    assert SOURCES == catalogue | {"ROBOT_MEASURED", "OWNER_PROFILE", "FACT", "OWNER_JOB"}
    assert DESIGN_SOURCES <= SOURCES
    assert dict(DISCLOSED_SOURCES) == {"DEFAULT_UNMEASURED": "amber", "INFERRED": "info", "OWNER_PROFILE": "info"}


def test_every_catalogue_default_can_be_a_param() -> None:
    # Every unit in params.json is a Param unit, and every shipped default can
    # be held with its class (DERIVED ones with an input, disclosed ones with a
    # disclosure).
    entries = json.loads(PARAMS_JSON.read_text(encoding="utf-8"))
    assert {e["unit"] for e in entries} <= UNITS
    anchor = Param(1.0, "mm", "SPEC", "anchor")
    for entry in entries:
        if entry["default"] is None:
            continue
        source = entry["provenance"]
        ref = "docs/prep/params.json %s" % entry["name"]
        if source == "DERIVED":
            p = Param.derived(entry["default"], entry["unit"], ref, inputs=(anchor,))
        else:
            severity = DISCLOSED_SOURCES.get(source)
            disclosures = () if severity is None else (unmeasured(entry["name"], severity),)
            p = Param(entry["default"], entry["unit"], source, ref, disclosures=disclosures)
        assert p.value == entry["default"]


# ---------------------------------------------------------------------------
# Fallbacks cannot exist without their disclosure
# ---------------------------------------------------------------------------


def test_unmeasured_default_cannot_exist_without_an_amber_disclosure() -> None:
    with pytest.raises(UndisclosedFallbackError, match="DEFAULT_UNMEASURED"):
        Param(40.0, "mm", "DEFAULT_UNMEASURED", "docs/prep/params.json r_w")
    with pytest.raises(UndisclosedFallbackError, match="amber"):
        Param(40.0, "mm", "DEFAULT_UNMEASURED", "docs/prep/params.json r_w", disclosures=(unmeasured("r_w", "info"),))
    assert r_w().disclosures == (unmeasured(),)
    # A red disclosure is at least amber.
    Param(40.0, "mm", "DEFAULT_UNMEASURED", "docs/prep/params.json r_w", disclosures=(unmeasured("r_w", "red"),))


@pytest.mark.parametrize("source", ["INFERRED", "OWNER_PROFILE"])
def test_inferred_and_hand_edited_values_need_a_disclosure(source: str) -> None:
    with pytest.raises(UndisclosedFallbackError, match=source):
        Param(3500.0, "mm", source, "docs/prep/params.json reach_max")
    note = Disclosure(id="%s:reach_max" % source, severity="info", text="reach_max is %s" % source, source="x")
    assert Param(3500.0, "mm", source, "docs/prep/params.json reach_max", disclosures=(note,)).source == source


def test_disclosures_must_be_disclosures() -> None:
    with pytest.raises(TypeError, match="Disclosure"):
        Param(40.0, "mm", "DEFAULT_UNMEASURED", "r", disclosures=("unmeasured r_w",))  # type: ignore[arg-type]


def test_replace_cannot_strip_a_disclosure() -> None:
    with pytest.raises(UndisclosedFallbackError):
        dataclasses.replace(r_w(), disclosures=())
    with pytest.raises(ValueError, match="source"):
        dataclasses.replace(r_w(), source="MEASURED")


def test_derived_value_names_its_inputs_and_inherits_their_disclosures() -> None:
    rho = Param.derived(55.0, "mm", "rho = r_w + m_w", inputs=(r_w(), m_w()))
    assert rho.source == "DERIVED"
    assert rho.inputs == (r_w(), m_w())
    assert rho.disclosures == ()
    assert rho.all_disclosures() == (unmeasured(),)
    # Two levels down, and each disclosure once.
    r_body = Param.derived(519.0, "mm", "r_body", inputs=(rho, r_w()))
    assert r_body.all_disclosures() == (unmeasured(),)
    with pytest.raises(ValueError, match="inputs"):
        Param(55.0, "mm", "DERIVED", "rho = r_w + m_w")
    with pytest.raises(ValueError, match="inputs"):
        Param.derived(55.0, "mm", "rho", inputs=())
    with pytest.raises(ValueError, match="inputs"):
        Param(55.0, "mm", "POLICY", "rho", inputs=(m_w(),))
    with pytest.raises(TypeError, match="Param"):
        Param.derived(55.0, "mm", "rho", inputs=(40.0,))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def test_param_json_roundtrip() -> None:
    rho = Param.derived(55.0, "mm", "rho = r_w + m_w", inputs=(r_w(), m_w()))
    point = Param((211.0, -76.0), "deg", "SPEC", "spec 08 Constants")
    axis = Param("Y", "enum", "OWNER_JOB", "job.toml panel.tower2")
    for p in (rho, point, axis, Param(2438, "mm", "OWNER_JOB", "job.toml panel.W")):
        data = canonical_json(p)
        back = Param.from_json(json.loads(data))
        assert back == p
        assert canonical_json(back) == data
    assert json.loads(canonical_json(rho))["inputs"][0]["disclosures"][0]["id"] == "DEFAULT_UNMEASURED:r_w"


def test_from_json_is_strict() -> None:
    data = json.loads(canonical_json(m_w()))
    with pytest.raises(ValueError, match="keys"):
        Param.from_json({**data, "default": 15.0})
    missing = dict(data)
    del missing["ref"]
    with pytest.raises(ValueError, match="keys"):
        Param.from_json(missing)
    with pytest.raises(UndisclosedFallbackError):
        Param.from_json({**json.loads(canonical_json(r_w())), "disclosures": []})


def test_to_json_refuses_a_value_the_canonical_form_would_change() -> None:
    # The canonical form writes numbers to 0.001. A Param's value must survive
    # that unchanged, so a report never shows a value other than the one used.
    with pytest.raises(ValueError, match="0.001"):
        Param(168.0297, "mm", "SPEC", "x").to_json()
    with pytest.raises(ValueError, match="0.001"):
        Param((1.0, 0.0005), "mm", "SPEC", "x").to_json()
    # docs/prep/params.json roundtrip_tol (0.0005 mm) is finer than the grid:
    # in micrometres it is exact.
    with pytest.raises(ValueError, match="smaller unit"):
        Param(0.0005, "mm", "POLICY", "docs/prep/params.json roundtrip_tol").to_json()
    assert Param(0.5, "um", "POLICY", "docs/prep/params.json roundtrip_tol").to_json()["value"] == 0.5
    # Float noise below a nanometre is not a change.
    assert Param(0.1 + 0.2, "mm", "SPEC", "x").to_json()["value"] == pytest.approx(0.3)
    assert canonical_json(Param(0.1 + 0.2, "mm", "SPEC", "x")) == canonical_json(Param(0.3, "mm", "SPEC", "x"))


# ---------------------------------------------------------------------------
# Architecture: nothing rewrites a frozen value behind its validation
# ---------------------------------------------------------------------------


def frozen_bypasses(source: str, filename: str = "<string>") -> list[str]:
    """Places that set or delete attributes of an object other than ``self`` behind its back.

    ``object.__setattr__``/``__delattr__`` on anything but ``self`` (a frozen
    dataclass may normalise its own fields in ``__post_init__``), any other use
    of them (an alias), and writes through ``x.__dict__`` or ``vars(x)``.
    """
    found: list[str] = []
    tree = ast.parse(source, filename)
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_object_setattr(node.func):
            called.add(id(node.func))
            first = node.args[0] if node.args else None
            if not (isinstance(first, ast.Name) and first.id == "self"):
                found.append("%s:%d object.%s on another object" % (filename, node.lineno, node.func.attr))
    for node in ast.walk(tree):
        if _is_object_setattr(node) and id(node) not in called:
            found.append("%s:%d object.%s used outside a call" % (filename, node.lineno, node.attr))
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        elif isinstance(node, ast.Delete):
            targets = list(node.targets)
        for target in targets:
            if isinstance(target, ast.Subscript) and _is_foreign_dict(target.value):
                found.append("%s:%d write through __dict__/vars() of another object" % (filename, node.lineno))
    return found


def _is_object_setattr(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr in ("__setattr__", "__delattr__")
        and isinstance(node.value, ast.Name)
        and node.value.id == "object"
    )


def _is_foreign_dict(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "__dict__":
        return not (isinstance(node.value, ast.Name) and node.value.id == "self")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "vars":
        return not (node.args and isinstance(node.args[0], ast.Name) and node.args[0].id == "self")
    return False


def test_bypass_scanner_finds_planted_bypasses() -> None:
    planted = (
        "object.__setattr__(param, 'disclosures', ())\n"
        "object.__delattr__(p, 'ref')\n"
        "s = object.__setattr__\n"
        "param.__dict__['source'] = 'SPEC'\n"
        "vars(param)['ref'] = 'x'\n"
        "del vars(param)['ref']\n"
        "class A:\n"
        "    def __post_init__(self):\n"
        "        object.__setattr__(self, 'value', 1)\n"
        "        self.__dict__['x'] = 1\n"
    )
    lines = [int(f.split(":")[1].split()[0]) for f in frozen_bypasses(planted)]
    assert sorted(lines) == [1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize("package", ["goliath_prep", "goliath_run"])
def test_no_code_rewrites_another_objects_frozen_fields(package: str, astscan: ModuleType) -> None:
    # Param, Disclosure, Finding and Witness validate in __post_init__; a value
    # rewritten through object.__setattr__ or __dict__ would skip that, so an
    # undisclosed fallback could be made. Python cannot forbid it; this test
    # finds it.
    if importlib.util.find_spec(package) is None:
        pytest.skip("%s is not installed in this environment" % package)
    problems: list[str] = []
    for path, _module in astscan.package_files(package):
        problems += frozen_bypasses(Path(path).read_text(encoding="utf-8"), str(path))
    assert not problems, problems
