# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Canonical JSON: the same input gives the same bytes on Linux and Windows (unit F04).

The CI matrix runs this file on ubuntu-latest and windows-latest (Python 3.11
and 3.14), so the golden bytes below are checked on both systems. Fresh
interpreters with another hash seed, locale and I/O encoding must agree too.
"""
from __future__ import annotations

import enum
import hashlib
import importlib.util
import json
import math
import os
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from goliath_prep.core.canon import FLOAT_LIMIT, canonical_json, canonical_sha256, quantize

GOLDEN = Path(__file__).resolve().parent / "prep_canon_golden.py"

GOLDEN_LINES = [
    "{",
    '  "empty": {',
    '    "list": [],',
    '    "map": {}',
    "  },",
    '  "finding": {',
    '    "check_id": "W-SURF",',
    '    "geometry_wkt": "POINT (1234.5675 -0.0004)",',
    '    "message": "rear wheel enters the kerf of cut 7",',
    '    "severity": "refuse",',
    '    "witness": {',
    '      "cut": 8,',
    '      "hazard": "kerf of cut 7, line 1234, depth 18.0 mm, removed at piece 5521",',
    '      "heading_c": 30,',
    '      "margins": {',
    '        "e_j": 0.001,',
    '        "m_w": 15.000',
    "      },",
    '      "piece": 5522,',
    '      "point": [',
    "        1234.568,",
    "        0.000",
    "      ],",
    '      "raw_mm": 54.938,',
    '      "slack_mm": -0.062,',
    '      "src": {',
    '        "file": "job.gi",',
    '        "line": 1234,',
    '        "text": "G1 X10 Y20 Z-18"',
    "      },",
    '      "wheel": 1',
    "    }",
    "  },",
    '  "floats": [',
    "    0.062,",
    "    -0.062,",
    "    0.188,",
    "    1.000,",
    "    0.001,",
    "    0.000,",
    "    0.000,",
    "    2.675,",
    "    0.300",
    "  ],",
    '  "keys": {',
    '    "": 7,',
    '    "B": 3,',
    '    "a": 2,',
    '    "b": 1,',
    '    "é": 4,',
    '    "￿": 6,',
    '    "\U0001f600": 5',
    "  },",
    '  "params": {',
    '    "rho": {',
    '      "disclosures": [],',
    '      "inputs": [',
    "        {",
    '          "disclosures": [',
    "            {",
    '              "id": "DEFAULT_UNMEASURED:r_w",',
    '              "severity": "amber",',
    '              "source": "docs/prep/params.json r_w",',
    '              "sticky": true,',
    '              "text": "wheel contact radius r_w = 40 mm is not measured (field guide 04)"',
    "            }",
    "          ],",
    '          "inputs": [],',
    '          "ref": "docs/prep/params.json r_w",',
    '          "source": "DEFAULT_UNMEASURED",',
    '          "unit": "mm",',
    '          "value": 40.000',
    "        },",
    "        {",
    '          "disclosures": [],',
    '          "inputs": [],',
    '          "ref": "docs/prep/params.json m_w",',
    '          "source": "POLICY",',
    '          "unit": "mm",',
    '          "value": 15.000',
    "        }",
    "      ],",
    '      "ref": "rho = r_w + m_w (docs/prep/params.json rho)",',
    '      "source": "DERIVED",',
    '      "unit": "mm",',
    '      "value": 55.000',
    "    }",
    "  },",
    '  "text": "Ø 6 mm \\"flat\\" end mill\\\\\\n\\r\\t  \U0001f527",',
    '  "zeta": [',
    "    true,",
    "    false,",
    "    null,",
    "    0,",
    "    -7,",
    "    12345678901234567890",
    "  ]",
    "}",
]
GOLDEN_BYTES = ("\n".join(GOLDEN_LINES) + "\n").encode("utf-8")


def load_golden() -> ModuleType:
    spec = importlib.util.spec_from_file_location("goliath_prep_tests_core_canon_golden", GOLDEN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _text(value: object) -> str:
    """The canonical JSON of one scalar, without the final newline."""
    return canonical_json(value).decode("utf-8")[:-1]


# ---------------------------------------------------------------------------
# Same bytes everywhere
# ---------------------------------------------------------------------------


def test_canonical_json_identical_on_linux_and_windows() -> None:
    golden = load_golden().golden_object()
    data = canonical_json(golden)
    # 1. The exact bytes, reviewed by hand; CI checks them on Linux and Windows.
    assert data == GOLDEN_BYTES
    assert canonical_sha256(golden) == hashlib.sha256(GOLDEN_BYTES).hexdigest()
    # 2. Nothing in them that a platform could change: LF only, UTF-8, final LF.
    assert b"\r" not in data
    assert data.endswith(b"}\n") and not data.endswith(b"\n\n")
    data.decode("utf-8", errors="strict")
    # 3. It is JSON, and reading it back gives the same canonical bytes.
    assert canonical_json(json.loads(data)) == data
    # 4. A different insertion order of every mapping gives the same bytes.
    assert canonical_json(_reversed_mappings(json.loads(data))) == data


def _reversed_mappings(value: object) -> object:
    if isinstance(value, dict):
        return {k: _reversed_mappings(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [_reversed_mappings(v) for v in value]
    return value


@pytest.mark.parametrize(
    "env",
    [
        {"PYTHONHASHSEED": "0", "PYTHONUTF8": "0", "LC_ALL": "C", "LANG": "C", "PYTHONIOENCODING": "latin-1"},
        {"PYTHONHASHSEED": "4242", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-16"},
    ],
    ids=["seed0-C-locale-latin1", "seed4242-utf8-mode"],
)
def test_fresh_interpreter_with_other_seed_locale_and_encoding_agrees(env: dict[str, str]) -> None:
    # The script also sets os.linesep to Windows' value before encoding.
    full_env = dict(os.environ)
    full_env.update(env)
    result = subprocess.run([sys.executable, str(GOLDEN)], capture_output=True, env=full_env, timeout=120)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert result.stdout == GOLDEN_BYTES


def test_layout() -> None:
    assert canonical_json({}) == b"{}\n"
    assert canonical_json([]) == b"[]\n"
    assert canonical_json({"b": [1, {}], "a": None}) == b'{\n  "a": null,\n  "b": [\n    1,\n    {}\n  ]\n}\n'
    assert canonical_json("x") == b'"x"\n'


def test_keys_sort_by_code_point() -> None:
    # Code point order, not UTF-16 order: U+FFFF sorts before U+1F600 here,
    # where RFC 8785 (UTF-16 code units) would put it after.
    keys = list(json.loads(canonical_json({"\U0001f600": 0, "￿": 0, "a": 0, "B": 0, "é": 0})))
    assert keys == ["B", "a", "é", "￿", "\U0001f600"]


# ---------------------------------------------------------------------------
# Rounding to 0.001
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, text",
    [
        # Exactly halfway in binary: ties go to the even last digit.
        (0.0625, "0.062"),
        (-0.0625, "-0.062"),
        (0.1875, "0.188"),
        (0.3125, "0.312"),
        (0.4375, "0.438"),
        (1.0625, "1.062"),
        (8.0625, "8.062"),
        (54.9375, "54.938"),
        # Written with a 5 in the fourth decimal but not halfway in binary:
        # the exact binary value decides, not the decimal spelling.
        (1.0005, "1.000"),  # 1.000499999999999944...
        (2.0005, "2.001"),  # 2.000500000000000166...
        (0.0005, "0.001"),  # 0.000500000000000000010...
        (-0.0005, "-0.001"),
        (1.0015, "1.002"),
        (1234.5675, "1234.568"),
        (2.675, "2.675"),
        # Zero has one spelling: no negative zero, also after rounding.
        (0.0, "0.000"),
        (-0.0, "0.000"),
        (-0.0004, "0.000"),
        (0.00025, "0.000"),
        (-1e-300, "0.000"),
        (5e-324, "0.000"),
        (-5e-324, "0.000"),
        # Integral floats keep their decimals, so they read back as floats.
        (18.0, "18.000"),
        (-3.0, "-3.000"),
        (0.1 + 0.2, "0.300"),
        (168.0297, "168.030"),
        (999999999999.9994, "999999999999.999"),
    ],
)
def test_rounding_boundaries(value: float, text: str) -> None:
    assert _text(value) == text
    assert quantize(value) == float(text)


def test_negative_zero_never_appears() -> None:
    for value in (-0.0, -0.0004, -1e-12, -0.0004999):
        assert b"-" not in canonical_json(value)
        assert math.copysign(1.0, quantize(value)) == 1.0


def test_quantize_directions() -> None:
    assert quantize(0.0625) == 0.062
    assert quantize(0.0621, "ceiling") == 0.063
    assert quantize(-0.0621, "ceiling") == -0.062
    assert quantize(0.0629, "floor") == 0.062
    assert quantize(-0.0621, "floor") == -0.063
    assert quantize(0.062, "ceiling") == 0.062  # already on the grid
    assert math.copysign(1.0, quantize(-0.0004, "ceiling")) == 1.0
    with pytest.raises(ValueError, match="rounding"):
        quantize(1.0, "half-up")  # type: ignore[arg-type]


def _reference_text(value: float) -> str:
    """Round half to even on the exact binary value, with fractions only (independent of decimal)."""
    thousandths = round(Fraction(value) * 1000)  # Fraction.__round__ ties to even
    sign = "-" if thousandths < 0 else ""
    whole, frac = divmod(abs(thousandths), 1000)
    return "%s%d.%03d" % (sign, whole, frac)


FINITE = st.floats(min_value=-FLOAT_LIMIT, max_value=FLOAT_LIMIT, allow_nan=False, allow_infinity=False,
                   exclude_min=True, exclude_max=True)


@settings(max_examples=2000, deadline=None)
@given(FINITE)
def test_rounding_matches_exact_fraction_reference(value: float) -> None:
    expected = _reference_text(value)
    if abs(Fraction(expected)) >= FLOAT_LIMIT:
        with pytest.raises(ValueError, match="too large"):
            canonical_json(value)
        return
    assert _text(value) == expected
    # Reading the text back and encoding again gives the same text.
    assert _text(json.loads(expected)) == expected


@settings(max_examples=2000, deadline=None)
@given(st.integers(min_value=-(10**12), max_value=10**12))
def test_exact_ties_go_to_even(j: int) -> None:
    # A double lies exactly halfway between two thousandths only when it is an
    # odd multiple of 1/16 (k/2000 with 125 | k): 0.0625, 0.1875, ... Random
    # doubles almost never hit one, so build them directly.
    value = (2 * j + 1) / 16
    assert Fraction(value) * 1000 % 1 == Fraction(1, 2)
    assert _text(value) == _reference_text(value)
    assert int(_text(value)[-1]) % 2 == 0


JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**30), max_value=10**30),
    FINITE,
    st.text(st.characters(exclude_categories=("Cs",)), max_size=12),
)
JSON_VALUES = st.recursive(
    JSON_SCALARS,
    lambda children: st.lists(children, max_size=4)
    | st.dictionaries(st.text(st.characters(exclude_categories=("Cs",)), max_size=6), children, max_size=4),
    max_leaves=25,
)


@settings(max_examples=500, deadline=None)
@given(JSON_VALUES)
def test_canonical_form_is_a_fixed_point(value: object) -> None:
    try:
        first = canonical_json(value)
    except ValueError as exc:
        assert "too large" in str(exc)
        return
    again = canonical_json(json.loads(first))
    assert again == first
    assert b"\r" not in first and first.endswith(b"\n")


# ---------------------------------------------------------------------------
# What it refuses, with a path to the value
# ---------------------------------------------------------------------------


class Colour(enum.Enum):
    RED = "red"


class Number(enum.IntEnum):
    ONE = 1


@pytest.mark.parametrize(
    "value, error, message",
    [
        ({"a": [1, math.nan]}, ValueError, r"\$\.a\[1\]: NaN"),
        ({"a": math.inf}, ValueError, r"\$\.a: inf"),
        ([-math.inf], ValueError, r"\$\[0\]: -inf"),
        ({"big": 1e12}, ValueError, "too large"),
        ({"big": -1e15}, ValueError, "too large"),
        ({"big": 999999999999.9995}, ValueError, "too large"),
        ({1: "x"}, TypeError, "keys must be strings"),
        ({"a": {None: 1}}, TypeError, r"\$\.a: object keys must be strings"),
        ({"s": {1, 2}}, TypeError, r"\$\.s: set"),
        ({"b": b"x"}, TypeError, "bytes"),
        ({"e": Colour.RED}, TypeError, "Enum"),
        ({"e": Number.ONE}, TypeError, "Enum"),
        ({"d": object()}, TypeError, "to_json"),
        ({"f": Fraction(1, 3)}, TypeError, "Fraction"),
        ({"t": "\ud800"}, ValueError, "surrogate"),
        ({"\udc00": 1}, ValueError, "surrogate"),
    ],
)
def test_refusals_name_the_value(value: object, error: type, message: str) -> None:
    with pytest.raises(error, match=message):
        canonical_json(value)


def test_circular_reference_is_refused() -> None:
    loop: list[object] = [1]
    loop.append(loop)
    with pytest.raises(ValueError, match=r"\$\[1\]: circular"):
        canonical_json(loop)


def test_named_tuple_is_refused_rather_than_losing_its_names() -> None:
    from collections import namedtuple

    point = namedtuple("point", "x y")(1.0, 2.0)
    with pytest.raises(TypeError, match="named tuple"):
        canonical_json(point)


def test_to_json_objects_and_numeric_subclasses() -> None:
    import numpy

    class Thing:
        def to_json(self) -> dict[str, object]:
            return {"b": numpy.float64(0.0625), "a": numpy.int64(3)}

    assert canonical_json(Thing()) == b'{\n  "a": 3,\n  "b": 0.062\n}\n'

    class Selfish:
        def to_json(self) -> object:
            return self

    with pytest.raises(TypeError, match="to_json"):
        canonical_json(Selfish())

    class Loop:
        def to_json(self) -> object:
            return {"again": self}

    with pytest.raises(ValueError, match=r"\$\.again: circular"):
        canonical_json(Loop())


def test_shared_subtrees_are_not_circular() -> None:
    shared = {"x": 1}
    assert canonical_json([shared, shared]) == b'[\n  {\n    "x": 1\n  },\n  {\n    "x": 1\n  }\n]\n'


def test_sha256_is_of_the_bytes() -> None:
    assert canonical_sha256({"a": 1.0}) == hashlib.sha256(b'{\n  "a": 1.000\n}\n').hexdigest()
