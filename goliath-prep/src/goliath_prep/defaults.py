# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The shipped defaults of the parameter catalogue, with provenance, and strict reading of the owner's files (unit P01).

**SPEC_PARAMS.** Every parameter of the catalogue (docs/prep/params.json,
described in docs/prep/parameters.md) that has a value of its own, as a
:class:`~goliath_prep.core.params.Param` with the catalogue's provenance class
as its source and ``docs/prep/params.json <name>`` as its reference. The
package cannot read docs/ once installed, so the values live here; a test
keeps them equal to params.json. The rules:

* numbers are floats, except byte counts (ints); an enum is its string;
* ``roundtrip_tol`` (0.0005 mm) is finer than the canonical 0.001 grid, so it
  is held as 0.5 ``um`` (goliath_prep.core.params);
* a fallback carries its disclosure: ``DEFAULT_UNMEASURED`` values the amber
  "UNMEASURED MACHINE PROFILE" disclosure, ``INFERRED`` values an info
  disclosure naming the unknown they stand in for;
* not in SPEC_PARAMS (:data:`NOT_IN_SPEC_PARAMS` says why for each): the
  ``DERIVED`` values, which are computed from the Params actually in use (the
  machine profile computes ``rho``; the geometry kernel and the context builder
  the footprint-derived ones), the job inputs (``OWNER_JOB``, read by
  goliath_prep.jobspec), and ``t_hold``, which is half the job's stock
  thickness. A derived value with default inputs is never offered on its own,
  so it cannot be combined with a measured input it was not computed from.

**Owner values only tighten** (parameters.md section 2.3; safety-model
section 2). :data:`OWNER_TIGHTENS` lists the catalogue values an owner may set
in machine.toml or job.toml and the direction that is tighter;
:func:`owner_value` refuses a looser one. A value is compared with the
catalogue default it replaces (for a gated margin, the value before its rung).

**Reading the owner's files.** :func:`load_toml` and :class:`StrictTable` read
machine.toml and job.toml strictly: UTF-8 (one leading byte-order mark is
allowed), TOML 1.0 by the standard library's ``tomllib``; every key of every
table must be one the reader knows, otherwise :class:`UnknownKeyError` (so a
declared value that nothing implements is refused, never ignored); a missing
required value raises :class:`MissingValueError`; numbers must be finite,
written with at most three decimals (canonical JSON keeps 0.001, so a report
shows exactly the value that was read) and below 10^12 in magnitude.
"""
from __future__ import annotations

import math
import numbers
import re
import tomllib
from collections.abc import Iterable, Mapping
from decimal import Context, Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from goliath_prep.core.disclosure import Disclosure
from goliath_prep.core.params import Param, Source
from goliath_prep.core.types import FootprintSet

__all__ = [
    "C_GRID",
    "NOT_IN_SPEC_PARAMS",
    "OWNER_TIGHTENS",
    "SET_T2_POSITION",
    "SPEC_PARAMS",
    "InputFileError",
    "MissingValueError",
    "StrictTable",
    "UnknownKeyError",
    "catalogue_ref",
    "exact_number",
    "load_toml",
    "owner_value",
    "vendor_footprint",
]

CATALOGUE = "docs/prep/params.json"


def catalogue_ref(name: str) -> str:
    """The reference of a catalogue default: ``docs/prep/params.json <name>``."""
    return "%s %s" % (CATALOGUE, name)


# ---------------------------------------------------------------------------
# The catalogue's own values
# ---------------------------------------------------------------------------

# (name, default, unit), grouped by provenance class. Transcribed from
# docs/prep/params.json; test_prep_defaults.py compares every entry.
_SPEC = (
    ("support_r1", 402, "mm"),
    ("support_r2", 211, "mm"),
    ("support_r3", 211, "mm"),
    ("support_delta1", 0, "deg"),
    ("support_delta2", 76, "deg"),
    ("support_delta3", -76, "deg"),
    ("heading_offset", 90, "deg"),
    ("c_grid_step", 10, "deg"),
    ("tower_zone_leg_wheel", 150, "mm"),
    ("tower_zone_leg_tool", 155, "mm"),
    ("panel_max_side", 3500, "mm"),
    ("z_safety", 10, "mm"),
    ("z_travel", 58, "mm"),
    ("tool_max_diameter", 8, "mm"),
    ("tool_max_length", 58, "mm"),
    ("upload_block", 1024, "byte"),
    ("tank_volume", 1.15, "dm3"),
)
_POLICY = (
    ("m_w", 15, "mm"),
    ("m_edge_tool", 2, "mm"),
    ("m_tower", 25, "mm"),
    ("geom_tol", 10, "mm"),
    ("tol_panel", 25, "mm"),
    ("near_square_extra", 10, "mm"),
    ("delta_tool", 0.25, "mm"),
    ("overcut_max", 0.5, "mm"),
    ("slack_min", 0, "mm"),
    ("attach_tol", 0.1, "mm"),
    ("obstacle_clearance", 3, "mm"),
    ("roundtrip_tol", 0.5, "um"),  # 0.0005 mm in the catalogue: finer than the 0.001 grid
    ("park_pose_tol", 20, "mm"),
    ("park_pose_tol_deg", 5, "deg"),
    ("r3_park_heading_sweep", 10, "deg"),
    ("eps_surf", 0.02, "mm"),
    ("through_tol", 0.05, "mm"),
    ("piece_length_min", 5, "mm"),
    ("check_chord_tol", 0.05, "mm"),
    ("emit_arc_tol", 0.02, "mm"),
    ("helix_tol", 0.02, "mm"),
    ("arc_radius_tol", 0.005, "mm"),
    ("arc_closure_tol", 0.01, "mm"),
    ("coordinate_limit", 10000, "mm"),
    ("c_step_max", 180, "deg"),
    ("battery_ack_below", 50, "percent"),
    ("battery_ack_job_min", 15, "min"),
    ("series_redetect_tol", 3, "mm"),
)
_GATE = (
    ("m_edge", 50, "mm"),
    ("m_edge_baseline", 50, "mm"),
    ("z_min_unverified", 5, "mm"),
    ("kerf_width_max", 0, "mm"),
    ("kerf_ride_max", 0, "mm"),
    ("recess_ignore_max", 0, "mm"),
    ("max_program_bytes", 32768, "byte"),
)
# (name, default, unit, what the disclosure says)
_DEFAULT_UNMEASURED = (
    ("r_w", 40, "mm",
     "the wheel contact radius r_w is the 40 mm default, a generous guess nobody has measured on this robot "
     "(U-22); field guide 04 measures each wheel module at rung R0b"),
    ("body_hull_margin", 60, "mm",
     "the body hull is the default one, the wheel discs grown by 60 mm (U-22); field guide 04 measures the body "
     "outline at rung R0b"),
    ("tower_base_radius", 120, "mm",
     "each tower base is taken as a disc of the 120 mm default radius; field guide 04 measures it at rung R0b"),
    ("wire_attachment", "body-hull", "enum",
     "where the towers' wires attach to the robot is not measured (U-49), so P-REACH counts the whole body hull; "
     "field guide 04 inspects the attachment at rung R0b"),
)
_INFERRED = (
    ("reach_max", 3500, "mm",
     "reach_max = 3500 mm, the wires' usable reach, is inferred from the specification's 'diagonal 3500' "
     "(U-40, U-49)"),
    ("baseline_band", 300, "mm",
     "baseline_band = 300 mm, where positioning near the tower baseline is expected to be least accurate, is "
     "inferred (U-50)"),
    ("z_upper_limit", 40, "mm", "the Z upper limit of +40 mm is inferred from the 58 mm Z travel"),
    ("feed_cap", 2000, "mm/min", "the feed cap of 2000 mm/min is inferred: no source gives a safe feed (U-37)"),
    ("rpm_cap", 27000, "rpm", "the spindle cap of 27000 rpm is inferred from a press source (U-37)"),
    ("cumulative_rotation_max", 350, "deg",
     "the net rotation bound of 350 degrees is inferred from the vendor's C range while the wire attachment is "
     "not known (U-49)"),
    ("series_band", 450, "mm", "the series detection band of 450 mm is inferred (U-12)"),
)

NOT_IN_SPEC_PARAMS: Mapping[str, str] = MappingProxyType(
    {
        "r_max": "DERIVED from the active footprint model (geometry kernel)",
        "r_min": "DERIVED from the active footprint model (geometry kernel)",
        "support_centroid_distance": "DERIVED from the active footprint model (geometry kernel)",
        "support_centroid_radius": "DERIVED from the active footprint model (geometry kernel)",
        "r_rot": "DERIVED from the active footprint model (geometry kernel)",
        "rho": "DERIVED: MachineProfile computes it from the r_w and m_w in use",
        "r_body": "DERIVED from R_rot, rho and the body hull in use",
        "safe_zone_inset_desk": "DERIVED from R_rot, rho, m_edge and geom_tol in use",
        "piece_length_max": "DERIVED from the active footprint, rho and the job's tool",
        "t_hold": "INFERRED as half the job's stock thickness T: it has no value without the job",
        "panel_width": "job input: JobSpec panel.W",
        "panel_height": "job input: JobSpec panel.H",
        "stock_thickness": "job input: JobSpec panel.T",
        "tower2_axis": "job input: JobSpec panel.tower2",
        "tool_diameter": "job input: JobSpec tool.d",
        "tool_flute_length": "job input: JobSpec tool.flute",
        "tool_stickout": "job input: JobSpec tool.stickout",
        "tool_max_stepdown": "job input: JobSpec tool.max_stepdown",
        "support_strip_width": "job input: JobSpec supports, per side",
        "placement_dx": "job input: JobSpec placement.dx",
        "placement_dy": "job input: JobSpec placement.dy",
    }
)
"""Catalogue parameters that are not in :data:`SPEC_PARAMS`, each with the reason."""


def _number_value(value: object, unit: str) -> object:
    if unit == "enum":
        return value
    if unit == "byte":
        return int(value)  # type: ignore[call-overload]
    return float(value)  # type: ignore[arg-type]


def _build() -> dict[str, Param[Any]]:
    out: dict[str, Param[Any]] = {}

    def add(name: str, default: object, unit: str, source: Source, disclosures: tuple[Disclosure, ...] = ()) -> None:
        if name in out:  # pragma: no cover - a guard on this module's own tables
            raise RuntimeError("catalogue parameter %s listed twice" % name)
        out[name] = Param(_number_value(default, unit), unit, source, catalogue_ref(name), disclosures=disclosures)

    for group, source in ((_SPEC, "SPEC"), (_POLICY, "POLICY"), (_GATE, "GATE")):
        for name, default, unit in group:
            add(name, default, unit, source)  # type: ignore[arg-type]
    for name, default, unit, what in _DEFAULT_UNMEASURED:
        add(name, default, unit, "DEFAULT_UNMEASURED", (Disclosure(
            id="DEFAULT_UNMEASURED:%s" % name, severity="amber", text="UNMEASURED MACHINE PROFILE: " + what,
            source=catalogue_ref(name)),))
    for name, default, unit, what in _INFERRED:
        add(name, default, unit, "INFERRED", (Disclosure(
            id="INFERRED:%s" % name, severity="info", text=what, source=catalogue_ref(name)),))
    return dict(sorted(out.items()))


SPEC_PARAMS: Mapping[str, Param[Any]] = MappingProxyType(_build())
"""The catalogue's shipped values by name (read-only); see the module documentation."""

C_GRID: tuple[int, ...] = tuple(range(0, 360, int(SPEC_PARAMS["c_grid_step"].value)))
"""The C words a program may carry: 0, 10, ..., 350 (spec 08 §Emitting the C word)."""

SET_T2_POSITION: Mapping[str, int] = MappingProxyType({"X": 1, "Y": 0})
"""The tower-2 axis as the number of ``SET T2 POSITION;<n>``: 1 = tower 2 along X (corner (W, 0)),
0 = along Y (corner (0, H)) (spec 02 §Tower pairing & geometry; spec 04 §Tower pairing and geometry)."""


def vendor_footprint() -> FootprintSet:
    """The spec footprint (safety-model 4.2) from :data:`SPEC_PARAMS`, nothing measured."""
    p = SPEC_PARAMS
    return FootprintSet(
        vendor=tuple((p["support_r%d" % k].value, p["support_delta%d" % k].value) for k in (1, 2, 3)),
        measured=None,
        active="vendor",
    )


# ---------------------------------------------------------------------------
# Errors and strict reading
# ---------------------------------------------------------------------------


class InputFileError(ValueError):
    """A machine.toml or job.toml (or the mapping read from one) that cannot be used as written.

    ``label`` is the file's role ("job.toml"), ``path`` where it was read from
    (None for a mapping), ``key`` the dotted key path of the value (None for a
    problem with the whole file), ``problem`` the explanation alone.
    """

    def __init__(self, problem: str, *, label: str | None = None, path: str | None = None,
                 key: str | None = None) -> None:
        self.problem = problem
        self.label = label
        self.path = path
        self.key = key
        where = label or ""
        if path:
            where = "%s (%s)" % (where, path) if where else path
        super().__init__(": ".join(part for part in (where, key, problem) if part))


class MissingValueError(InputFileError):
    """A required value is not in the file. Nothing is defaulted in its place."""


class UnknownKeyError(InputFileError):
    """A key the reader does not know: refused, so a declared value can never be ignored."""


_GRID = Decimal("0.001")
_LIMIT = Decimal(10) ** 12
_CONTEXT = Context(prec=60)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def exact_number(value: object) -> Decimal:
    """The number as written, exactly: refuses what canonical JSON could not show as read.

    Accepts an int, a :class:`~decimal.Decimal` (TOML floats read with
    ``parse_float=Decimal``) or a float (from JSON: its shortest repr, which
    is the decimal that was written). Raises ``TypeError`` for anything else
    (a bool included) and ``ValueError`` for a value that is not finite, has
    more than three decimals, or reaches 10^12.
    """
    if isinstance(value, bool) or not isinstance(value, (numbers.Integral, float, Decimal)):
        raise TypeError("must be a number, got %r" % (value,))
    if isinstance(value, float):
        exact = Decimal(repr(value))
    elif isinstance(value, Decimal):
        exact = value
    else:
        exact = Decimal(int(value))
    if not exact.is_finite():
        raise ValueError("must be a finite number, got %s" % value)
    if abs(exact) >= _LIMIT:
        raise ValueError("%s is too large (the limit is 10^12)" % value)
    try:
        on_grid = exact == exact.quantize(_GRID, context=_CONTEXT)
    except InvalidOperation:  # pragma: no cover - the limit above keeps quantize in range
        raise ValueError("%s cannot be read as a number" % value) from None
    if not on_grid:
        raise ValueError("%s has more than three decimals: write values to 0.001 at most" % value)
    return exact


def _float(exact: Decimal) -> float:
    return float(exact) + 0.0  # + 0.0: never -0.0


def load_toml(path: str | Path, *, label: str) -> dict[str, Any]:
    """The TOML document at ``path``: strict UTF-8, one leading byte-order mark allowed, floats as Decimal."""
    where = str(path)
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise InputFileError("cannot read the file: %s" % (exc.strerror or exc), label=label, path=where) from exc
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputFileError("the file is not UTF-8 text (byte %d); save it as UTF-8" % exc.start, label=label,
                             path=where) from exc
    try:
        return tomllib.loads(text, parse_float=Decimal)
    except tomllib.TOMLDecodeError as exc:
        raise InputFileError("the file is not valid TOML: %s" % exc, label=label, path=where) from exc


_ABSENT = object()


class StrictTable:
    """One table of a TOML document, read key by key against the keys this reader knows.

    Built with the ``keys`` the table may hold: any other key raises
    :class:`UnknownKeyError` at once. Every getter takes a key, records that
    it was read, and returns None when an optional key is absent (or, where
    ``unknown_ok``, holds the word ``unknown``, field guide 04's word for a
    value nobody could measure). :meth:`done` checks that every known key was
    asked for, so a reader cannot accept a key it then ignores.
    """

    def __init__(self, data: object, keys: Iterable[str], *, label: str, path: str | None = None,
                 prefix: str = "") -> None:
        self.label = label
        self.path = path
        self.prefix = prefix
        self._keys = tuple(keys)
        self._asked: set[str] = set()
        if not isinstance(data, Mapping):
            raise InputFileError("must be a table, got %r" % (data,), label=label, path=path, key=prefix or None)
        self._data = data
        unknown = sorted(str(k) for k in data if k not in self._keys)
        if unknown:
            raise UnknownKeyError(
                "unknown key: nothing reads it, so it is refused rather than ignored%s. The keys known here are: %s"
                % ("" if len(unknown) == 1 else " (also unknown: %s)" % ", ".join(unknown[1:]),
                   ", ".join(self._keys) or "none"),
                label=label, path=path, key=self.key(unknown[0]))

    def key(self, name: str) -> str:
        """The dotted key path of ``name`` in this table."""
        return "%s.%s" % (self.prefix, name) if self.prefix else name

    def error(self, name: str | None, problem: str, kind: type[InputFileError] = InputFileError) -> InputFileError:
        """An error about ``name`` (or about the whole table, for None), naming the file and the key path."""
        key = self.key(name) if name is not None else (self.prefix or None)
        return kind(problem, label=self.label, path=self.path, key=key)

    def has(self, name: str) -> bool:
        return name in self._data

    def _get(self, name: str, required: bool, why: str) -> object:
        if name not in self._keys:  # pragma: no cover - a reader asking for a key it did not declare
            raise RuntimeError("%s: the reader asks for %r, which it did not declare" % (self.prefix or "top", name))
        self._asked.add(name)
        if name not in self._data:
            if required:
                raise self.error(name, "missing. %s" % why if why else "missing", MissingValueError)
            return _ABSENT
        return self._data[name]

    def number(self, name: str, *, required: bool = False, why: str = "", above: float | None = None,
               at_least: float | None = None, at_most: float | None = None,
               unknown_ok: bool = False) -> float | None:
        """A number as a float (see :func:`exact_number`), within the given bounds."""
        value = self._get(name, required, why)
        if value is _ABSENT:
            return None
        if unknown_ok and isinstance(value, str):
            if value == "unknown":
                return None
            raise self.error(name, 'must be a number or the word "unknown", got %r' % (value,))
        return self._checked(name, value, above, at_least, at_most)

    def _checked(self, name: str, value: object, above: float | None, at_least: float | None,
                 at_most: float | None) -> float:
        try:
            exact = exact_number(value)
        except (TypeError, ValueError) as exc:
            raise self.error(name, str(exc)) from exc
        number = _float(exact)
        if above is not None and not number > above:
            raise self.error(name, "must be greater than %s, got %s" % (_show(above), _show(number)))
        if at_least is not None and number < at_least:
            raise self.error(name, "must be at least %s, got %s" % (_show(at_least), _show(number)))
        if at_most is not None and number > at_most:
            raise self.error(name, "must be at most %s, got %s" % (_show(at_most), _show(number)))
        return number

    def integer(self, name: str, *, required: bool = False, why: str = "") -> int | None:
        value = self._get(name, required, why)
        if value is _ABSENT:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise self.error(name, "must be a whole number, got %r" % (value,))
        return int(value)

    def text(self, name: str, *, required: bool = False, why: str = "", unknown_ok: bool = False) -> str | None:
        value = self._get(name, required, why)
        if value is _ABSENT:
            return None
        if not isinstance(value, str):
            raise self.error(name, "must be text in quotes, got %r" % (value,))
        if not value.strip():
            raise self.error(name, "must not be empty")
        if unknown_ok and value == "unknown":
            return None
        return value

    def choice(self, name: str, choices: Iterable[str], *, required: bool = False, why: str = "",
               unknown_ok: bool = False) -> str | None:
        options = tuple(choices)
        value = self._get(name, required, why)
        if value is _ABSENT:
            return None
        if not isinstance(value, str) or value not in options:
            raise self.error(name, "must be one of %s, got %r" % (", ".join('"%s"' % c for c in options), value))
        if unknown_ok and value == "unknown":
            return None
        return value

    def sha256(self, name: str, *, required: bool = False, why: str = "") -> str | None:
        value = self._get(name, required, why)
        if value is _ABSENT:
            return None
        if not isinstance(value, str) or not _SHA256.match(value):
            raise self.error(name, "must be a SHA-256 as 64 lowercase hex digits, got %r" % (value,))
        return value

    def table(self, name: str, keys: Iterable[str], *, required: bool = False, why: str = "") -> StrictTable | None:
        value = self._get(name, required, why)
        if value is _ABSENT:
            return None
        if not isinstance(value, Mapping):
            raise self.error(name, "must be a table ([%s]), got %r" % (self.key(name), value))
        return StrictTable(value, keys, label=self.label, path=self.path, prefix=self.key(name))

    def tables(self, name: str, keys: Iterable[str]) -> list[StrictTable]:
        """An array of tables (``[[name]]``), each read against ``keys``; absent is an empty list."""
        value = self._get(name, False, "")
        if value is _ABSENT:
            return []
        if not isinstance(value, list) or not all(isinstance(v, Mapping) for v in value):
            raise self.error(name, "must be a list of tables, each written as [[%s]], got %r"
                             % (self.key(name), value))
        names = tuple(keys)
        return [StrictTable(v, names, label=self.label, path=self.path, prefix="%s[%d]" % (self.key(name), i))
                for i, v in enumerate(value)]

    def point(self, name: str, *, required: bool = False, why: str = "") -> tuple[float, float] | None:
        value = self._get(name, required, why)
        if value is _ABSENT:
            return None
        return self._pair(name, value, "must be a point [x, y]")

    def points(self, name: str, *, required: bool = False, why: str = "", minimum: int = 3,
               unknown_ok: bool = False) -> tuple[tuple[float, float], ...] | None:
        value = self._get(name, required, why)
        if value is _ABSENT or (unknown_ok and value == "unknown"):
            return None
        if not isinstance(value, list):
            raise self.error(name, "must be a list of points [[x, y], ...], got %r" % (value,))
        out = tuple(self._pair(name, v, "point %d must be [x, y]" % i) for i, v in enumerate(value))
        if len(out) < minimum:
            raise self.error(name, "needs at least %d vertices, got %d" % (minimum, len(out)))
        return out

    def _pair(self, name: str, value: object, what: str) -> tuple[float, float]:
        if not isinstance(value, list) or len(value) != 2:
            raise self.error(name, "%s, got %r" % (what, value))
        return (self._checked(name, value[0], None, None, None), self._checked(name, value[1], None, None, None))

    def done(self) -> None:
        """Check that every key this table may hold was asked for (a reader bug otherwise)."""
        missed = [k for k in self._keys if k not in self._asked]
        if missed:  # pragma: no cover - a reader that accepts keys it never reads
            raise RuntimeError("%s: the reader accepts but never reads %s" % (self.prefix or "top", missed))


def _show(value: float) -> str:
    return format(Decimal(repr(value)).normalize(), "f") if math.isfinite(value) else repr(value)


# ---------------------------------------------------------------------------
# Owner values
# ---------------------------------------------------------------------------

OWNER_TIGHTENS: Mapping[str, Literal["higher", "lower"]] = MappingProxyType(
    {
        "m_w": "higher",
        "m_edge": "higher",
        "m_edge_baseline": "higher",
        "m_edge_tool": "higher",
        "m_tower": "higher",
        "reach_max": "lower",
        "battery_ack_below": "higher",
        "battery_ack_job_min": "lower",
        "feed_cap": "lower",
        "rpm_cap": "lower",
        "obstacle_clearance": "higher",
        "overcut_max": "lower",
        "delta_tool": "higher",
    }
)
"""The catalogue values an owner may set, and which direction is tighter. Nothing else is settable."""

# Bounds a "lower is tighter" value keeps (a "higher" one is bounded below by its default).
_ABOVE_ZERO = frozenset({"reach_max", "feed_cap", "rpm_cap"})
_AT_MOST = {"battery_ack_below": 100.0}


def owner_value(name: str, value: float, *, source: Literal["OWNER_PROFILE", "OWNER_JOB"], ref: str,
                label: str | None = None, path: str | None = None, key: str | None = None) -> Param[float]:
    """The owner's ``value`` for the catalogue parameter ``name``, refused if looser than the default.

    The value is read as the files are (:func:`exact_number`: a finite
    number with at most three decimals), so a report shows the value in use.
    ``KeyError`` for a name owners cannot set (not in :data:`OWNER_TIGHTENS`).
    The Param carries an info disclosure saying the owner's value is in effect.
    ``label``, ``path`` and ``key`` locate the value for the error message.
    """
    direction = OWNER_TIGHTENS[name]
    default = SPEC_PARAMS[name]

    def refuse(problem: str) -> InputFileError:
        return InputFileError(problem, label=label, path=path, key=key or ref)

    try:
        number = _float(exact_number(value))  # the files' rules: finite, at most three decimals
    except (TypeError, ValueError) as exc:
        raise refuse(str(exc)) from exc
    if name in _ABOVE_ZERO and not number > 0:
        raise refuse("must be greater than 0, got %s" % _show(number))
    if number < 0:
        raise refuse("must not be negative, got %s" % _show(number))
    limit = _AT_MOST.get(name)
    if limit is not None and number > limit:
        raise refuse("must be at most %s, got %s" % (_show(limit), _show(number)))
    looser = number < default.value if direction == "higher" else number > default.value
    if looser:
        raise refuse(
            "%s %s is looser than the catalogue's %s %s (%s): owner policy may only tighten, so give %s %s or %s"
            % (_show(number), default.unit, _show(default.value), default.unit, catalogue_ref(name),
               _show(default.value), default.unit, "more" if direction == "higher" else "less")
        )
    note = Disclosure(
        id="OWNER:%s" % name,
        severity="info",
        text="owner policy: %s = %s %s in place of the catalogue's %s %s (owner policy only tightens)"
        % (name, _show(number), default.unit, _show(default.value), default.unit),
        source=ref,
    )
    return Param(number, default.unit, source, ref, disclosures=(note,))
