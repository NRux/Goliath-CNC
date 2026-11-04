# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The check ids in docs/prep/checks.json and in goliath-prep's code are the same set (unit F02).

docs/prep/checks.json is the normative list of check ids (docs/prep/safety-model.md
section 11). goliath-prep turns it into code constants in its id module, which
arrives with unit F04 (``goliath_prep/core/ids.py``, generated from checks.json).
Until that module exists, ``test_doc_ids_match_code`` skips and says why; as soon
as it exists the test runs, and every id in checks.json must be a constant in the
module and every id-shaped constant in the module must be in checks.json.

The module may expose its ids as string constants, as collections of them, as
the keys of a mapping, or as a string-valued Enum; all of them are collected.
A module named ``ids`` anywhere else in the package makes the test fail rather
than skip, so a renamed module cannot leave the check skipping for ever.
"""
from __future__ import annotations

import enum
import importlib
import json
import re
import types
from collections.abc import Mapping
from pathlib import Path

import pytest

import goliath_prep

REPO = Path(__file__).resolve().parents[2]
CHECKS_JSON = REPO / "docs" / "prep" / "checks.json"
# Where the id module lives (design unit F04: goliath_prep/core/ids.py); the
# short form goliath_prep.ids is accepted too. If both exist they must agree.
ID_MODULES = ("goliath_prep.core.ids", "goliath_prep.ids")
ID_RE = re.compile(r"^[WBMTDPFGSN]-[A-Z0-9]+(?:-[A-Z0-9]+)*$")


def _is_id(value: object) -> bool:
    return isinstance(value, str) and ID_RE.match(value) is not None


def ids_in_module(module: types.ModuleType) -> set[str]:
    """Every check id a module exposes publicly."""
    found: set[str] = set()
    for attr, value in vars(module).items():
        if attr.startswith("_") or isinstance(value, types.ModuleType):
            continue
        if _is_id(value):
            found.add(value)
        elif isinstance(value, type) and issubclass(value, enum.Enum):
            found.update(m.value for m in value if _is_id(m.value))
        elif isinstance(value, Mapping):
            if value and all(_is_id(k) for k in value):
                found.update(value)
        elif isinstance(value, (frozenset, set, tuple, list)):
            if value and all(_is_id(v) for v in value):
                found.update(value)
    return found


def _import_if_present(name: str) -> types.ModuleType | None:
    """The module, or None when it (or its parent package below goliath_prep) does not exist.

    Any other import error, such as a missing dependency inside the module,
    propagates: a broken id module must fail the test, not skip it.
    """
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing.startswith("goliath_prep.") and (missing == name or name.startswith(missing + ".")):
            return None
        raise


def _doc_ids() -> set[str]:
    entries = json.loads(CHECKS_JSON.read_text(encoding="utf-8"))
    ids = [e["id"] for e in entries]
    assert len(ids) == len(set(ids)), "checks.json repeats an id"
    return set(ids)


def test_id_collector_reads_every_form() -> None:
    module = types.ModuleType("fake_ids")

    class CheckId(str, enum.Enum):
        W_SURF = "W-SURF"

    module.W_AREA = "W-AREA"
    module.READER = frozenset({"D-GI-OP", "N-GI-UNKNOWN-KEY"})
    module.SEVERITY = {"W-TOWER": "refuse"}
    module.CheckId = CheckId
    module.NOT_AN_ID = "refuse"
    module.MIXED = ("W-PARK", "refuse")  # not an id collection
    module.R_KERF = "R-KERF"  # a relaxation, not a check family
    module._PRIVATE = "B-TOWER"
    module.json = json  # a module attribute is skipped
    assert ids_in_module(module) == {"W-AREA", "D-GI-OP", "N-GI-UNKNOWN-KEY", "W-TOWER", "W-SURF"}


def test_import_if_present_skips_only_missing_modules() -> None:
    assert _import_if_present("goliath_prep.no_such_module_f02") is None
    assert _import_if_present("goliath_prep.no_such_package_f02.ids") is None
    with pytest.raises(ModuleNotFoundError):
        _import_if_present("no_such_top_level_package_f02.ids")


def test_no_id_module_hides_elsewhere() -> None:
    package_dir = Path(goliath_prep.__file__).resolve().parent
    expected = {package_dir / "core" / "ids.py", package_dir / "ids.py"}
    stray = sorted(str(p.relative_to(package_dir)) for p in package_dir.rglob("ids.py") if p not in expected)
    assert not stray, "an id module outside %s would leave test_doc_ids_match_code skipping: %s" % (
        ", ".join(ID_MODULES),
        stray,
    )


def test_doc_ids_match_code() -> None:
    modules = {name: m for name in ID_MODULES if (m := _import_if_present(name)) is not None}
    if not modules:
        pytest.skip(
            "goliath-prep has no check-id module yet: goliath_prep.core.ids arrives with unit F04, "
            "generated from docs/prep/checks.json; this test runs as soon as it exists"
        )
    doc = _doc_ids()
    for name, module in modules.items():
        code = ids_in_module(module)
        assert code, "%s exposes no check id" % name
        missing = sorted(doc - code)
        extra = sorted(code - doc)
        assert not missing, "ids in docs/prep/checks.json but not in %s: %s" % (name, missing)
        assert not extra, "ids in %s but not in docs/prep/checks.json: %s" % (name, extra)
