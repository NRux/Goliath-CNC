# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""machine.toml: the machine profile of one robot (unit P01; the project design, sections 3.1, 4.2 and 4.5).

The profile holds what was measured on one robot at rung R0b (field guide 04)
and the owner's policy for it. Its measured values use **field guide 04's
field ids as their TOML key paths** (docs/field-guide/04-measure-your-robot.fields.json),
so the field ``wheel.q1.contact_radius_mm`` is the key ``contact_radius_mm``
of the table ``[wheel.q1]``. Each of them holds a number (or a choice, or
text) or the word ``"unknown"``; an absent one reads as ``unknown``. A
partial measurement is never read as a complete one.

Format, version 1 (every table is optional; a key not listed is refused)::

    profile_version = 1                  # required
    serial = "TEST-SERIAL-0001"          # required: the robot's serial (private: stays on this computer)

    [footprint]                          # footprint.q1_mm ... footprint.q3_deg
    q1_mm = 402.0                        # distance from the collet mark T to the module's contact centre
    q1_deg = 0.0                         # angle from the backward body axis, positive to the right
    q2_mm = 211.0
    q2_deg = 76.0
    q3_mm = 211.0
    q3_deg = -76.0

    [wheel.q1]                           # and [wheel.q2], [wheel.q3]
    contact_radius_mm = 38.0

    [body]                               # extents from T along and across the body axis
    ahead_of_tool_mm = 90.0
    behind_tool_mm = 470.0
    left_of_axis_mm = 260.0
    right_of_axis_mm = 255.0
    max_radius_mm = 500.0
    outline_points = [[-60.0, 0.0], [-40.0, 150.0], ...]   # optional: [back, right] pairs

    [tower]
    base_radius_mm = 110.0
    base_length_mm = 200.0
    base_width_mm = 180.0

    [wire]
    attachment = "offset"                # on_spindle_axis | offset | not_found | unknown
    same_point = "yes"                   # yes | no | unknown
    back_mm = 150.0                      # the attachment W behind T (minus = ahead)
    right_mm = 0.0                       # W right of the body axis (minus = left)

    [stop]                               # the physical stop (field guide 04 step 11)
    kind = "estop_button_on_robot"
    location = "red button on the back of the robot"
    latching = "yes"
    cuts = "controller_power"
    cuts_basis = "label"

    [rf]                                 # the towers' RF channel pair (U-10): one of spec 04's CONFIG_0..5
    t1_channel = 101
    t2_channel = 114

    [policy]                             # owner policy: may only tighten the catalogue values
    m_edge = 60.0                        # keys: see POLICY_KEYS

    [export]                             # written by goliath-run when it exports the profile from facts
    by = "goliath-run 0.0.1"
    facts_sha256 = "<64 hex digits>"
    content_sha256 = "<64 hex digits>"   # export_content_sha256(): everything but [export] and [policy]

Field guide 04 writes ``body.outline_points`` as text (``-60,0; -40,150``);
here it is a TOML list of ``[back, right]`` pairs. The other field guide
fields (``footprint.q1_back_mm``, ``wheel.q1.diameter_mm``, ``wire.swivel``,
``session.operator`` ...) are not read by any check, so the profile refuses
them; they stay in the measurement record.

**Provenance** (the project design, section 3.1, red-team C-13). A profile
whose ``[export]`` stamp verifies (its ``content_sha256`` equals
:func:`export_content_sha256` of the file) was generated from typed
measurements: its measured values are ``ROBOT_MEASURED``. A profile without
the stamp, or edited after it was stamped, loads its measured values as
``OWNER_PROFILE``, with an amber disclosure; it opens no fact-based gate (the
gates come from the facts store, never from this file). Owner policy values
are ``OWNER_PROFILE`` in either case, so ``[policy]`` is outside the stamp:
the owner may tighten a margin without voiding the measurements. The stamp
tells an exported file from an edited one; it is not a signature, and it
needs none, because nothing in this file opens a gate.

**What each measurement becomes** (docs/prep/parameters.md section 2.2):

* ``r_w`` is the **maximum** of the three modules' contact radii, never the
  mean, a ``DERIVED`` Param whose inputs are the three readings. If any is
  unknown, ``r_w`` stays ``DEFAULT_UNMEASURED`` (amber) and is the larger of
  the 40 mm default and the known readings. ``rho = r_w + m_w``.
* A support whose distance or angle is unknown adds no measured position;
  any measured one makes the active footprint the union of the spec one and
  the measured one (:meth:`MachineProfile.footprint_models`).
* The body hull is the convex hull of the wheel discs and the measured
  outline (``outline_points``, else the box of the four extents). Without
  them the default hull (wheel discs grown by ``body_hull_margin``) stays.
* ``tower.base_radius_mm`` replaces the 120 mm default when the base's
  length and width are known too and the radius is at least half the larger
  of them; a smaller radius is refused (it goes to design review). With any
  of the three unknown the value stays ``DEFAULT_UNMEASURED``: the larger of
  120 mm, the known radius and half each known side.
* The wire attachment is a point in the body frame, ``(back_mm, right_mm)``
  from T, when ``wire.attachment`` is ``offset`` (or ``on_spindle_axis``:
  then T itself, and the measured offset must lie within 2 mm of T),
  ``wire.same_point`` is ``yes`` and both offsets are known. Otherwise
  :attr:`MachineProfile.wire_attachment` is None: the whole body hull counts
  (P-REACH). ``not_found`` and two attachment points also go to design review.

**Fingerprint.** :meth:`MachineProfile.fingerprint` is the SHA-256 of the
profile's canonical JSON (goliath_prep.core.canon): the same on Linux and
Windows, independent of the file's name, folder, line endings, key order and
of writing ``36`` or ``36.0``. Certificates bind it.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from goliath_prep.core.canon import canonical_sha256, quantize
from goliath_prep.core.disclosure import Disclosure
from goliath_prep.core.params import Param
from goliath_prep.core.types import MEASURED_SUPPORT_PARAMS, FootprintSet, FrozenMapping
from goliath_prep.defaults import SPEC_PARAMS, InputFileError, StrictTable, catalogue_ref, load_toml, owner_value

__all__ = [
    "BODY_FIELDS",
    "FIELD_CHOICES",
    "FOOTPRINT_FIELDS",
    "LABEL",
    "MEASURED_FIELDS",
    "ON_SPINDLE_AXIS_TOL_MM",
    "POLICY_KEYS",
    "PROFILE_VERSION",
    "RF_PAIRS",
    "STOP_FIELDS",
    "TOWER_FIELDS",
    "WHEEL_FIELDS",
    "WIRE_FIELDS",
    "MachineProfile",
    "Origin",
    "Vec2",
    "export_content_sha256",
]

Vec2 = tuple[float, float]
Origin = Literal["exported", "edited", "hand-written"]

LABEL = "machine.toml"
"""The file's role: the references of its values start with it, whatever the file is called."""
PROFILE_VERSION = 1

FOOTPRINT_FIELDS: tuple[str, ...] = tuple("footprint.q%d_%s" % (k, s) for k in (1, 2, 3) for s in ("mm", "deg"))
WHEEL_FIELDS: tuple[str, ...] = tuple("wheel.q%d.contact_radius_mm" % k for k in (1, 2, 3))
BODY_FIELDS: tuple[str, ...] = ("body.ahead_of_tool_mm", "body.behind_tool_mm", "body.left_of_axis_mm",
                                "body.right_of_axis_mm", "body.max_radius_mm")
TOWER_FIELDS: tuple[str, ...] = ("tower.base_radius_mm", "tower.base_length_mm", "tower.base_width_mm")
WIRE_FIELDS: tuple[str, ...] = ("wire.attachment", "wire.same_point", "wire.back_mm", "wire.right_mm")
STOP_FIELDS: tuple[str, ...] = ("stop.kind", "stop.location", "stop.latching", "stop.cuts", "stop.cuts_basis")
MEASURED_FIELDS: tuple[str, ...] = FOOTPRINT_FIELDS + WHEEL_FIELDS + BODY_FIELDS + TOWER_FIELDS + WIRE_FIELDS + \
    STOP_FIELDS
"""The field guide 04 fields the profile reads (``body.outline_points``, optional, aside)."""

FIELD_CHOICES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "wire.attachment": ("on_spindle_axis", "offset", "not_found", "unknown"),
        "wire.same_point": ("yes", "no", "unknown"),
        "stop.kind": ("estop_button_on_robot", "power_switch_on_robot", "mains_plug", "switched_socket_or_strip",
                      "separate_mains_estop", "other", "none_found", "unknown"),
        "stop.latching": ("yes", "no", "unknown"),
        "stop.cuts": ("controller_power", "motors_and_spindle_only", "spindle_only", "unknown"),
        "stop.cuts_basis": ("label", "manual", "experience", "unknown"),
    }
)
"""The choices of field guide 04's choice fields that the profile reads (a test compares them)."""

POLICY_KEYS: tuple[str, ...] = ("m_w", "m_edge", "m_edge_baseline", "m_edge_tool", "m_tower", "reach_max",
                                "battery_ack_below", "battery_ack_job_min")
"""The owner policy values machine.toml may set (each only tighter than the catalogue's)."""

RF_PAIRS: tuple[tuple[int, int], ...] = ((101, 114), (103, 116), (105, 118), (107, 120), (109, 122), (111, 124))
"""The vendor's tower channel pairs CONFIG_0..CONFIG_5 (spec 04 §Tower pairing and geometry)."""

ON_SPINDLE_AXIS_TOL_MM = 2.0
"""Field guide 04: the wires attach on the spindle axis when the mark W lies within 2 mm of T."""

_OWNER_DISCLOSURE_TEXT = {
    "hand-written": "machine.toml was written by hand (it carries no export stamp from goliath-run): its measured "
                    "values are the owner's entries (OWNER_PROFILE), not typed measurements of this robot, and "
                    "they open no fact-based gate",
    "edited": "machine.toml was changed after goliath-run exported it (its content no longer matches the export "
              "stamp): its measured values load as the owner's entries (OWNER_PROFILE), not typed measurements, "
              "and they open no fact-based gate",
}


_UNSTAMPED = ("export", "policy")


def export_content_sha256(document: Mapping[str, Any]) -> str:
    """The content hash of an export stamp: canonical SHA-256 of the document but ``[export]`` and ``[policy]``.

    ``document`` is the TOML document as read back (``tomllib``, floats as
    float or as Decimal). Numbers count by value: ``36``, ``36.0`` and
    ``36.000`` hash the same. goliath-run computes it on the document it
    writes, read back, and stores it in ``[export] content_sha256``. Owner
    policy is left out: its values are the owner's in any case.
    """
    content = {key: value for key, value in document.items() if key not in _UNSTAMPED}
    return canonical_sha256(_plain(content, "$"))


def _plain(value: object, where: str) -> object:
    if isinstance(value, bool) or isinstance(value, str):
        return value
    if isinstance(value, (int, float, Decimal)):
        exact = value if isinstance(value, Decimal) else Decimal(repr(value)) if isinstance(value, float) else value
        return float(exact) + 0.0
    if isinstance(value, Mapping):
        return {str(k): _plain(v, "%s.%s" % (where, k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v, "%s[%d]" % (where, i)) for i, v in enumerate(value)]
    raise TypeError("%s: a machine profile holds numbers, text, tables and lists, not %s"
                    % (where, type(value).__name__))


@dataclass(frozen=True)
class MachineProfile:
    """One robot's machine profile, as read from machine.toml (see the module documentation).

    ``readings`` maps each field of :data:`MEASURED_FIELDS` to its value, or
    None when it is unknown or absent. ``params`` holds the values the checks
    use, by catalogue name, each with its provenance: ``r_w``, ``m_w``,
    ``rho``, ``body_hull`` (``default``, ``measured-outline`` or
    ``measured-extents``), ``body_hull_margin`` (with the default hull),
    ``body_max_radius`` (when measured), ``tower_base_radius``,
    ``wire_attachment`` (``body-hull`` or ``measured-point``) with
    ``wire_attachment_back`` and ``wire_attachment_right`` for a point, the
    measured supports (``support_r1_measured`` ...), and the owner policy
    values of :data:`POLICY_KEYS` (the catalogue's unless the owner tightened
    them). The other catalogue values are in
    :data:`goliath_prep.defaults.SPEC_PARAMS`; a context builder layers these
    over them.
    """

    profile_version: int
    serial: str
    origin: Origin
    exported_by: str | None
    facts_sha256: str | None
    readings: Mapping[str, float | str | None]
    params: Mapping[str, Param[Any]] = field(repr=False)
    footprint: FootprintSet
    body_outline: tuple[Vec2, ...] | None
    wire_attachment: Vec2 | None
    rf_pair: tuple[int, int] | None

    def __post_init__(self) -> None:
        if self.origin not in ("exported", "edited", "hand-written"):
            raise ValueError("MachineProfile origin must be exported, edited or hand-written, got %r" % self.origin)
        if (self.origin == "hand-written") != (self.facts_sha256 is None):
            raise ValueError("MachineProfile facts_sha256 is given exactly when the profile carries an export stamp")
        if set(self.readings) != set(MEASURED_FIELDS):
            raise ValueError("MachineProfile readings must hold exactly the fields of MEASURED_FIELDS")
        object.__setattr__(self, "readings", FrozenMapping(self.readings))
        for name, param in self.params.items():
            if not isinstance(param, Param):
                raise TypeError("MachineProfile params[%r] must be a Param, got %r" % (name, param))
        object.__setattr__(self, "params", FrozenMapping(self.params))
        if not isinstance(self.footprint, FootprintSet):
            raise TypeError("MachineProfile footprint must be a FootprintSet, got %r" % (self.footprint,))

    # -- reading ------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> MachineProfile:
        """Read machine.toml at ``path``. Raises :class:`~goliath_prep.defaults.InputFileError` (and its kinds)."""
        return cls.from_mapping(load_toml(path, label=LABEL), path=str(path))

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any], *, path: str | None = None) -> MachineProfile:
        """Read a machine profile from the document ``tomllib`` returns (floats as Decimal or float)."""
        return _Reader(document, path).read()

    # -- views --------------------------------------------------------------

    def footprint_models(self) -> FootprintSet:
        """The spec footprint and the measured one (the union is active once any support is measured)."""
        return self.footprint

    def unmeasured(self) -> list[str]:
        """The field guide 04 fields this profile reads that are unknown or absent, sorted."""
        return sorted(name for name in MEASURED_FIELDS if self.readings[name] is None)

    @property
    def stop(self) -> Mapping[str, str | None]:
        """The physical stop's description: ``stop.*`` field id to value, None where unknown."""
        return MappingProxyType({name: self.readings[name] for name in STOP_FIELDS})  # type: ignore[misc]

    def disclosures(self) -> tuple[Disclosure, ...]:
        """Every disclosure the profile's values carry, each once, in a stable order."""
        seen: dict[Disclosure, None] = {}
        for name in sorted(self.params):
            for d in self.params[name].all_disclosures():
                seen.setdefault(d, None)
        return tuple(seen)

    def to_json(self) -> dict[str, Any]:
        """The profile as canonical-JSON data: what :meth:`fingerprint` hashes."""
        return {
            "profile_version": self.profile_version,
            "serial": self.serial,
            "origin": self.origin,
            "exported_by": self.exported_by,
            "facts_sha256": self.facts_sha256,
            "readings": dict(self.readings),
            "params": {name: param.to_json() for name, param in self.params.items()},
            "footprint": self.footprint.to_json(),
            "body_outline": None if self.body_outline is None else [list(p) for p in self.body_outline],
            "wire_attachment": None if self.wire_attachment is None else list(self.wire_attachment),
            "rf_pair": None if self.rf_pair is None else list(self.rf_pair),
        }

    def fingerprint(self) -> str:
        """SHA-256 (hex) of the profile's canonical JSON: the value certificates bind."""
        return canonical_sha256(self.to_json())


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------

_TOP_KEYS = ("profile_version", "serial", "footprint", "wheel", "body", "tower", "wire", "stop", "rf", "policy",
             "export")


def _sum(a: float, b: float) -> float:
    """``a + b`` exactly on the 0.001 grid both lie on (no float noise in a report)."""
    return float(Decimal(repr(a)) + Decimal(repr(b)))


class _Reader:
    def __init__(self, document: Mapping[str, Any], path: str | None) -> None:
        self.document = document
        self.path = path
        self.top = StrictTable(document, _TOP_KEYS, label=LABEL, path=path)
        self.readings: dict[str, float | str | None] = {}
        self.params: dict[str, Param[Any]] = {}

    # -- phase 1: every key and type, as written ----------------------------

    def read(self) -> MachineProfile:
        top = self.top
        version = top.integer("profile_version", required=True,
                              why="Write profile_version = %d as the first line" % PROFILE_VERSION)
        if version != PROFILE_VERSION:
            raise top.error("profile_version", "version %d is not known to this goliath-prep (it reads version %d)"
                            % (version, PROFILE_VERSION))
        serial = top.text("serial", required=True, why="Write the robot's serial, as goliath-run reads it")
        assert serial is not None
        self._footprint(top.table("footprint", tuple(f.split(".", 1)[1] for f in FOOTPRINT_FIELDS)))
        self._wheels(top.table("wheel", ("q1", "q2", "q3")))
        outline = self._body(top.table("body", tuple(f.split(".", 1)[1] for f in BODY_FIELDS) + ("outline_points",)))
        self._simple(top.table("tower", tuple(f.split(".", 1)[1] for f in TOWER_FIELDS)), TOWER_FIELDS, above=0.0)
        self._wire(top.table("wire", tuple(f.split(".", 1)[1] for f in WIRE_FIELDS)))
        self._stop(top.table("stop", tuple(f.split(".", 1)[1] for f in STOP_FIELDS)))
        rf_pair = self._rf(top.table("rf", ("t1_channel", "t2_channel")))
        policy = top.table("policy", POLICY_KEYS)
        policy_values = {name: policy.number(name) for name in POLICY_KEYS} if policy is not None else {}
        if policy is not None:
            policy.done()
        export = top.table("export", ("by", "facts_sha256", "content_sha256"))
        exported_by = facts = None
        origin: Origin = "hand-written"
        if export is not None:
            why = "goliath-run writes it with the export stamp; remove the whole [export] table from a hand-written " \
                  "profile"
            exported_by = export.text("by", required=True, why=why)
            facts = export.sha256("facts_sha256", required=True, why=why)
            content = export.sha256("content_sha256", required=True, why=why)
            export.done()
            origin = "exported" if content == export_content_sha256(self.document) else "edited"
        top.done()

        # -- phase 2: values with their provenance --------------------------
        self.origin = origin
        self.facts = facts
        self.owner_note = None if origin == "exported" else Disclosure(
            id="OWNER_PROFILE:machine.toml", severity="amber", text=_OWNER_DISCLOSURE_TEXT[origin], source=LABEL)
        for name in POLICY_KEYS:
            value = policy_values.get(name)
            self.params[name] = SPEC_PARAMS[name] if value is None else owner_value(
                name, value, source="OWNER_PROFILE", ref="%s policy.%s" % (LABEL, name), label=LABEL,
                path=self.path, key="policy.%s" % name)
        self._r_w_and_rho()
        footprint = self._footprint_models()
        body_outline = self._body_hull(outline)
        self._tower_base()
        wire_attachment = self._wire_attachment()
        return MachineProfile(
            profile_version=version, serial=serial, origin=origin, exported_by=exported_by, facts_sha256=facts,
            readings=self.readings, params=dict(sorted(self.params.items())), footprint=footprint,
            body_outline=body_outline, wire_attachment=wire_attachment, rf_pair=rf_pair,
        )

    def _simple(self, table: StrictTable | None, fields: tuple[str, ...], *, above: float | None = None,
                at_least: float | None = None, at_most: float | None = None) -> None:
        for name in fields:
            key = name.rsplit(".", 1)[1]
            self.readings[name] = None if table is None else table.number(
                key, above=above, at_least=at_least, at_most=at_most, unknown_ok=True)
        if table is not None:
            table.done()

    def _footprint(self, table: StrictTable | None) -> None:
        for name in FOOTPRINT_FIELDS:
            key = name.split(".", 1)[1]
            if table is None:
                self.readings[name] = None
            elif key.endswith("_mm"):
                self.readings[name] = table.number(key, above=0.0, unknown_ok=True)
            else:
                self.readings[name] = table.number(key, at_least=-180.0, at_most=180.0, unknown_ok=True)
        if table is not None:
            table.done()

    def _wheels(self, table: StrictTable | None) -> None:
        for k, name in enumerate(WHEEL_FIELDS, start=1):
            module = None if table is None else table.table("q%d" % k, ("contact_radius_mm",))
            self.readings[name] = None if module is None else module.number(
                "contact_radius_mm", above=0.0, unknown_ok=True)
            if module is not None:
                module.done()
        if table is not None:
            table.done()

    def _body(self, table: StrictTable | None) -> tuple[Vec2, ...] | None:
        for name in BODY_FIELDS:
            key = name.split(".", 1)[1]
            if table is None:
                self.readings[name] = None
            elif key == "max_radius_mm":
                self.readings[name] = table.number(key, above=0.0, unknown_ok=True)
            else:
                self.readings[name] = table.number(key, at_least=0.0, unknown_ok=True)
        if table is None:
            return None
        outline = table.points("outline_points", minimum=3)
        table.done()
        return outline

    def _wire(self, table: StrictTable | None) -> None:
        for name in WIRE_FIELDS:
            key = name.split(".", 1)[1]
            if table is None:
                self.readings[name] = None
            elif name in FIELD_CHOICES:
                self.readings[name] = table.choice(key, FIELD_CHOICES[name], unknown_ok=True)
            else:
                self.readings[name] = table.number(key, unknown_ok=True)
        if table is not None:
            table.done()

    def _stop(self, table: StrictTable | None) -> None:
        for name in STOP_FIELDS:
            key = name.split(".", 1)[1]
            if table is None:
                self.readings[name] = None
            elif name in FIELD_CHOICES:
                self.readings[name] = table.choice(key, FIELD_CHOICES[name], unknown_ok=True)
            else:
                self.readings[name] = table.text(key, unknown_ok=True)
        if table is not None:
            table.done()

    def _rf(self, table: StrictTable | None) -> tuple[int, int] | None:
        if table is None:
            return None
        why = "the RF pair needs both channels; remove [rf] while the pair is not known (U-10)"
        t1 = table.integer("t1_channel", required=True, why=why)
        t2 = table.integer("t2_channel", required=True, why=why)
        table.done()
        pair = (t1, t2)
        if pair not in RF_PAIRS:
            raise table.error(None, "(%s, %s) is not one of the vendor's channel pairs CONFIG_0..CONFIG_5 %s "
                                    "(spec 04 §Tower pairing and geometry)" % (t1, t2, list(RF_PAIRS)))
        return pair  # type: ignore[return-value]

    # -- phase 2 helpers ----------------------------------------------------

    def measured(self, value: float | str, unit: str, key: str) -> Param[Any]:
        """A measured value with this profile's provenance."""
        if self.origin == "exported":
            assert self.facts is not None
            return Param(value, unit, "ROBOT_MEASURED", "%s %s (exported from facts %s)" % (LABEL, key,
                                                                                         self.facts[:12]))
        assert self.owner_note is not None
        return Param(value, unit, "OWNER_PROFILE", "%s %s" % (LABEL, key), disclosures=(self.owner_note,))

    def unmeasured_default(self, name: str, value: float, why: str) -> Param[float]:
        """The catalogue default of ``name`` (DEFAULT_UNMEASURED), or a larger value the known readings force."""
        default = SPEC_PARAMS[name]
        if value == default.value and not why:
            return default
        return Param(value, default.unit, "DEFAULT_UNMEASURED", "%s (%s)" % (catalogue_ref(name), why),
                     disclosures=(Disclosure(id="DEFAULT_UNMEASURED:%s" % name, severity="amber",
                                             text="UNMEASURED MACHINE PROFILE: %s" % why,
                                             source="%s, %s" % (LABEL, catalogue_ref(name))),))

    def _r_w_and_rho(self) -> None:
        values = [self.readings[name] for name in WHEEL_FIELDS]
        if all(v is not None for v in values):
            inputs = tuple(self.measured(v, "mm", name) for name, v in zip(WHEEL_FIELDS, values))  # type: ignore
            r_w: Param[Any] = Param.derived(
                max(values), "mm",  # type: ignore[type-var]
                "r_w = the largest of %s (the maximum, never the mean)" % ", ".join(WHEEL_FIELDS), inputs)
        else:
            known = [(name, v) for name, v in zip(WHEEL_FIELDS, values) if v is not None]
            missing = [name for name, v in zip(WHEEL_FIELDS, values) if v is None]
            value = max([float(SPEC_PARAMS["r_w"].value)] + [v for _, v in known])  # type: ignore[misc]
            why = "" if not known else (
                "%s not measured, so r_w = %s mm, the larger of the 40 mm default and the known readings %s: the "
                "module nobody measured could be the widest"
                % (" and ".join(missing), _show(value), ", ".join("%s = %s mm" % (n, _show(v)) for n, v in known)))
            r_w = self.unmeasured_default("r_w", value, why)
        self.params["r_w"] = r_w
        m_w = self.params["m_w"]
        self.params["rho"] = Param.derived(_sum(r_w.value, m_w.value), "mm", "rho = r_w + m_w", (r_w, m_w))

    def _footprint_models(self) -> FootprintSet:
        vendor = tuple((SPEC_PARAMS["support_r%d" % k].value, SPEC_PARAMS["support_delta%d" % k].value)
                       for k in (1, 2, 3))
        measured: list[tuple[float, float] | None] = []
        for k, (r_name, delta_name) in enumerate(MEASURED_SUPPORT_PARAMS, start=1):
            r_key, delta_key = "footprint.q%d_mm" % k, "footprint.q%d_deg" % k
            r, delta = self.readings[r_key], self.readings[delta_key]
            if r is None or delta is None:
                measured.append(None)
                continue
            self.params[r_name] = self.measured(r, "mm", r_key)
            self.params[delta_name] = self.measured(delta, "deg", delta_key)
            measured.append((r, delta))  # type: ignore[arg-type]
        if all(m is None for m in measured):
            return FootprintSet(vendor=vendor, measured=None, active="vendor")
        return FootprintSet(vendor=vendor, measured=tuple(measured), active="union")

    def _body_hull(self, outline: tuple[Vec2, ...] | None) -> tuple[Vec2, ...] | None:
        r = self.readings
        if r["body.max_radius_mm"] is not None:
            self.params["body_max_radius"] = self.measured(r["body.max_radius_mm"], "mm", "body.max_radius_mm")
        if outline is not None:
            self.params["body_hull"] = self.measured("measured-outline", "enum", "body.outline_points")
            return outline
        ahead, behind, left, right = (r[name] for name in BODY_FIELDS[:4])
        if ahead is not None and behind is not None and left is not None and right is not None:
            self.params["body_hull"] = self.measured("measured-extents", "enum", ", ".join(BODY_FIELDS[:4]))
            return ((-ahead, -left), (-ahead, right), (behind, right), (behind, -left))  # type: ignore[operator]
        margin = SPEC_PARAMS["body_hull_margin"]
        self.params["body_hull_margin"] = margin
        self.params["body_hull"] = Param("default", "enum", "DEFAULT_UNMEASURED", catalogue_ref("body_hull_margin"),
                                         disclosures=margin.disclosures)
        return None

    def _tower_base(self) -> None:
        radius, length, width = (self.readings[name] for name in TOWER_FIELDS)
        if radius is not None and length is not None and width is not None:
            need = max(length, width) / 2  # type: ignore[operator]
            if radius < need:  # type: ignore[operator]
                raise InputFileError(
                    "tower.base_radius_mm = %s mm is less than half the base's larger side (%s mm): a disc of that "
                    "radius cannot hold the base. Re-measure (field guide 04, step 10) or ask for a design review"
                    % (_show(radius), _show(max(length, width))),  # type: ignore[arg-type,type-var]
                    label=LABEL, path=self.path, key="tower.base_radius_mm")
            self.params["tower_base_radius"] = self.measured(radius, "mm", "tower.base_radius_mm")
            return
        candidates = [(float(SPEC_PARAMS["tower_base_radius"].value), "the 120 mm default")]
        if radius is not None:
            candidates.append((radius, "tower.base_radius_mm = %s mm" % _show(radius)))  # type: ignore[arg-type]
        for name, side in (("tower.base_length_mm", length), ("tower.base_width_mm", width)):
            if side is not None:
                candidates.append((quantize(side / 2, "ceiling"), "half of %s = %s mm" % (name, _show(side))))
        value = max(v for v, _ in candidates)
        missing = [name for name in TOWER_FIELDS if self.readings[name] is None]
        why = "" if len(candidates) == 1 else (
            "%s not measured, so the tower base radius is %s mm, the largest of %s"
            % (" and ".join(missing), _show(value), ", ".join(text for _, text in candidates)))
        self.params["tower_base_radius"] = self.unmeasured_default("tower_base_radius", value, why)

    def _wire_attachment(self) -> Vec2 | None:
        r = self.readings
        attachment, same_point, back, right = (r[name] for name in WIRE_FIELDS)
        review = []
        if attachment == "not_found":
            review.append("wire.attachment = not_found: no attachment point was found")
        if same_point == "no":
            review.append("wire.same_point = no: the two towers' wires attach at different points")
        if review or attachment is None or same_point is None or back is None or right is None:
            default = SPEC_PARAMS["wire_attachment"]
            if not review:
                self.params["wire_attachment"] = default
            else:
                note = Disclosure(id="DESIGN_REVIEW:wire.attachment", severity="amber",
                                  text="%s. P-REACH counts the whole body hull until a design review decides how to "
                                       "model the wires" % "; ".join(review), source=LABEL + " wire")
                self.params["wire_attachment"] = Param(default.value, default.unit, default.source, default.ref,
                                                       disclosures=default.disclosures + (note,))
            return None
        if attachment == "on_spindle_axis":
            distance = math.hypot(back, right)  # type: ignore[arg-type]
            if distance > ON_SPINDLE_AXIS_TOL_MM:
                raise InputFileError(
                    "wire.attachment = on_spindle_axis, but wire.back_mm, wire.right_mm = %s, %s lie %.1f mm from T; "
                    "on the spindle axis means within %s mm (field guide 04). Correct the attachment or the offsets"
                    % (_show(back), _show(right), distance, _show(ON_SPINDLE_AXIS_TOL_MM)),  # type: ignore[arg-type]
                    label=LABEL, path=self.path, key="wire.attachment")
            point: Vec2 = (0.0, 0.0)
            where = "wire.attachment = on_spindle_axis (the tool point T)"
            back_ref = right_ref = where
        else:
            point = (back, right)  # type: ignore[assignment]
            back_ref, right_ref = "wire.back_mm", "wire.right_mm"
        self.params["wire_attachment"] = self.measured("measured-point", "enum", ", ".join(WIRE_FIELDS))
        self.params["wire_attachment_back"] = self.measured(point[0], "mm", back_ref)
        self.params["wire_attachment_right"] = self.measured(point[1], "mm", right_ref)
        return point


def _show(value: float) -> str:
    return format(Decimal(repr(float(value))).normalize(), "f")
