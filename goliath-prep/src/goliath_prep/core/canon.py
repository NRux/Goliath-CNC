# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Canonical JSON: identical inputs give identical bytes on Linux and Windows (unit F04).

Certificates, reports and profile fingerprints (SHA-256 of the canonical bytes)
are compared byte for byte, on the laptop that prepared a job and on the one
that runs it (the project design, sections 4.5 and 4.14). The form is:

* **Encoding.** UTF-8 without a byte-order mark; LF line endings, never CR; a
  final LF. The function returns bytes, so no text-mode file layer can add a
  CR on Windows: write them with ``Path.write_bytes``.
* **Layout.** Two-space indentation, one member or element per line,
  ``": "`` between a key and its value, ``","`` at line ends; an empty object
  is ``{}`` and an empty array ``[]``.
* **Keys.** Object keys must be strings. They are sorted by Unicode code point
  (Python's ``str`` order). Note that RFC 8785 sorts by UTF-16 code units
  instead; the two differ only for keys with characters above U+FFFF.
* **Strings.** Escaped as JSON requires (``\\"``, ``\\\\``, the short forms
  ``\\b \\f \\n \\r \\t`` and ``\\u00XX`` for other control characters);
  every other character is written as itself in UTF-8. No Unicode
  normalisation. A lone surrogate cannot be UTF-8 and is refused.
* **Integers.** Decimal digits, any size (``numbers.Integral``, so numpy
  integers too). ``True``/``False``/``None`` are ``true``/``false``/``null``.
* **Floats: rounded to 0.001 in their own unit** (0.001 mm for lengths) and
  always written with exactly three decimals: ``18.0`` is ``18.000``, so a
  float reads back as a float. The rounding mode is **round half to even
  applied to the exact binary value of the double** (``Decimal(x)`` is exact):
  ``1.0005`` is ``1.000499999...`` in binary and gives ``1.000``; ``0.0625``
  lies exactly halfway and gives ``0.062``. Only odd multiples of 1/16 lie
  exactly halfway. Zero is written ``0.000``, never ``-0.000``, also when a
  small negative value rounds to zero. NaN and infinities are refused, and so
  is a magnitude of 10^12 or more (rounded): below 2^40 the 0.001 grid survives
  a read-back, so the canonical form is a fixed point of reading and writing.

Why round: maths libraries on different systems may differ in the last bit of
a computed value; rounding to 1 um makes a computed slack print the same on
Linux and Windows. It cannot help when the two results straddle a rounding
boundary (a half-thousandth), which is rare but possible; values that are
inputs (job files, constants, measurements) are exact.

A value that needs a finer resolution than 0.001 of its unit must be held in a
smaller unit. :class:`goliath_prep.core.params.Param` refuses to write a value
the rounding would change, so a report never shows a parameter other than the
one used.

Accepted types: None, bool, ``numbers.Integral``, float (and its subclasses,
such as ``numpy.float64``), str, ``Mapping``, list, tuple, and any object with a
``to_json()`` method (its result is encoded). Refused with ``TypeError``: an
Enum (convert it explicitly), sets (no order), bytes, named tuples (their names
would be lost), and anything else. Every error names the path of the value
(``$.findings[3].witness.slack_mm``).
"""
from __future__ import annotations

import decimal
import enum
import hashlib
import json
import math
import numbers
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Literal

__all__ = ["FLOAT_LIMIT", "QUANTUM", "Rounding", "canonical_json", "canonical_sha256", "quantize"]

QUANTUM = Decimal("0.001")
"""The resolution of every float in canonical JSON, in the value's own unit."""

FLOAT_LIMIT = 1e12
"""Floats whose magnitude (rounded) reaches this are refused."""

Rounding = Literal["half-even", "ceiling", "floor"]

_LIMIT = Decimal(10) ** 12
_MODES = {"half-even": decimal.ROUND_HALF_EVEN, "ceiling": decimal.ROUND_CEILING, "floor": decimal.ROUND_FLOOR}
# A private context, so the caller's thread-local decimal context cannot change the result.
_CONTEXT = decimal.Context(prec=60, rounding=decimal.ROUND_HALF_EVEN, traps=[decimal.InvalidOperation])
_INDENT = "  "
_PLAIN_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def quantize(value: float, mode: Rounding = "half-even") -> float:
    """``value`` on the canonical 0.001 grid, as a float.

    ``"half-even"`` is the canonical rounding (what :func:`canonical_json`
    writes). ``"ceiling"`` and ``"floor"`` round towards +infinity or
    -infinity, for a caller that must round a computed limit in its
    conservative direction before using it. The result is never -0.0.
    """
    if mode not in _MODES:
        raise ValueError("unknown rounding %r; use one of %s" % (mode, ", ".join(_MODES)))
    if isinstance(value, bool) or not isinstance(value, (float, numbers.Integral)):
        raise TypeError("quantize needs a float or an int, got %s" % type(value).__name__)
    return float(_quantized(float(value), mode, "value"))


def canonical_json(obj: object) -> bytes:
    """The canonical JSON bytes of ``obj`` (see the module documentation)."""
    out: list[str] = []
    _encode(obj, "$", 0, out, set())
    out.append("\n")
    return "".join(out).encode("utf-8")


def canonical_sha256(obj: object) -> str:
    """Hex SHA-256 of :func:`canonical_json` of ``obj``: the fingerprint certificates bind."""
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def _quantized(value: float, mode: str, path: str) -> Decimal:
    if math.isnan(value):
        raise ValueError("%s: NaN is not allowed in canonical JSON" % path)
    if math.isinf(value):
        raise ValueError("%s: %s is not allowed in canonical JSON" % (path, "inf" if value > 0 else "-inf"))
    if abs(value) >= FLOAT_LIMIT:
        raise ValueError("%s: %r is too large for the canonical 0.001 grid (limit %g)" % (path, value, FLOAT_LIMIT))
    q = Decimal(value).quantize(QUANTUM, rounding=_MODES[mode], context=_CONTEXT)
    if abs(q) >= _LIMIT:
        raise ValueError("%s: %r is too large for the canonical 0.001 grid (limit %g)" % (path, value, FLOAT_LIMIT))
    if q.is_zero():
        return Decimal("0.000")  # one zero: never -0.000
    return q


def _string(text: str, path: str) -> str:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "%s: the string contains a lone surrogate (%s), which UTF-8 cannot carry" % (path, exc)
        ) from None
    return json.dumps(text, ensure_ascii=False)


def _key_path(path: str, key: str) -> str:
    return "%s.%s" % (path, key) if _PLAIN_KEY.match(key) else "%s[%s]" % (path, json.dumps(key))


def _encode(value: object, path: str, depth: int, out: list[str], active: set[int]) -> None:
    if value is None:
        out.append("null")
    elif isinstance(value, bool):
        out.append("true" if value else "false")
    elif isinstance(value, enum.Enum):
        raise TypeError("%s: Enum %r: convert it explicitly (for example to its .value) before encoding"
                        % (path, value))
    elif isinstance(value, str):
        out.append(_string(str.__str__(value), path))
    elif isinstance(value, float):
        out.append(format(_quantized(float(value), "half-even", path), "f"))
    elif isinstance(value, numbers.Integral):
        out.append(str(int(value)))
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError("%s: bytes have no canonical JSON form; encode them explicitly (hex, base64)" % path)
    elif callable(getattr(value, "to_json", None)):
        result = value.to_json()  # type: ignore[attr-defined]
        if result is value:
            raise TypeError("%s: %s.to_json() returned the object itself" % (path, type(value).__name__))
        _encode(result, path, depth, out, active)
    elif isinstance(value, Mapping):
        _container(value, path, depth, out, active, mapping=True)
    elif isinstance(value, (list, tuple)):
        if isinstance(value, tuple) and hasattr(value, "_fields"):
            raise TypeError("%s: a named tuple (%s) would lose its field names; give it a to_json() method"
                            % (path, type(value).__name__))
        _container(value, path, depth, out, active, mapping=False)
    elif isinstance(value, (set, frozenset)):
        raise TypeError("%s: set has no order; sort it into a list first" % path)
    else:
        raise TypeError("%s: unsupported type %s; give it a to_json() method or convert it explicitly"
                        % (path, type(value).__name__))


def _container(value: object, path: str, depth: int, out: list[str], active: set[int], *, mapping: bool) -> None:
    marker = id(value)
    if marker in active:
        raise ValueError("%s: circular reference" % path)
    if mapping:
        assert isinstance(value, Mapping)
        items: list[tuple[str, object, str]] = []
        for key in value:
            if not isinstance(key, str) or isinstance(key, enum.Enum):
                raise TypeError("%s: object keys must be strings, got %s %r" % (path, type(key).__name__, key))
            plain = str.__str__(key)
            items.append((plain, value[key], _key_path(path, plain)))
        items.sort(key=lambda item: item[0])
        opening, closing = "{", "}"
    else:
        assert isinstance(value, (list, tuple))
        items = [("", element, "%s[%d]" % (path, index)) for index, element in enumerate(value)]
        opening, closing = "[", "]"
    if not items:
        out.append(opening + closing)
        return
    active.add(marker)
    inner = _INDENT * (depth + 1)
    out.append(opening + "\n")
    for index, (key, element, child) in enumerate(items):
        out.append(inner)
        if mapping:
            out.append(_string(key, child) + ": ")
        _encode(element, child, depth + 1, out, active)
        out.append(",\n" if index < len(items) - 1 else "\n")
    out.append(_INDENT * depth + closing)
    active.discard(marker)
