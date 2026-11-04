# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Feature gates: firmware capabilities, robot facts and owner policy in one tri-state gate set (unit R01).

The project design, sections 3.1 and 3.2::

    FirmwareCapabilities.for_version(v)   (goliath_link, unchanged)  -+
    FactStore(serial, firmware, fingerprint)  (goliath_run.facts)     +-> compute_gates() -> GateSet
    OwnerPolicy (settings, job file) and typed overrides              -+   each gate: allowed, basis, reason, evidence

Rules this module enforces:

* **No unknown is read as "yes".** ``allowed`` is ``True``, ``False`` or
  ``None`` (uncertain); only ``True`` permits (:meth:`GateSet.require`), and an
  unknown gate name is never ``True``.
* **Facts open gates only when they are the robot's own.** A gate opens on a
  fact in force (not stale, not "unverified for this version") whose basis is
  ``observed`` - or ``robot-measured`` for what the tape measure of rung R0b
  settles - and that a rung recorded (its ``rung`` is set). Owner answers,
  defaults, spec constants, vendor gates and facts from 01.02.00 static analysis
  never open one. Each gate checks its fact's value strictly: a value it does
  not recognise (``unknown``, an empty text, a number where a flag belongs)
  leaves it closed.
* **Permissions that rest only on the vendor's gates** (on firmware newer than
  01.02.00, branch B3: the keep-alive during work and ``INFO;8`` as the upload
  confirmation) are open with basis ``vendor-gate`` and an amber disclosure
  while in use (design 11.5).
* **Owner policy can only tighten.** :class:`OwnerPolicy` closes gates and gives
  the owner's consent where a gate needs it in addition to its fact
  (``drop_dwells``, ``tabs_hold``); a consent never opens a gate alone. A typed
  override (:class:`GateOverride`: who, when, why) opens only a gate marked
  ``overridable`` (the sanity-baseline check), is shown as a red disclosure on
  every screen, and loses against the owner's policy.

The gate set is exported to goliath-prep as a snapshot (red-team C-05, design
4.15.1): :meth:`GateSet.snapshot` gives canonical JSON and its SHA-256, which a
provisional certificate binds; :func:`load_gate_snapshot` reads it back as a
:class:`goliath_prep.core.types.GateView`, the same view :meth:`GateSet.as_prep_view`
gives in-process. goliath-prep reads gates only through that protocol.

**Fact ids.** The design names some of the facts gates are computed from
(``panel.vertex_frame``, ``rotation.model``, ``upload.end_signal`` ...); the rest
are named here, in :data:`GATE_CATALOGUE`, and the rungs that record them use
these ids.
"""
from __future__ import annotations

import contextlib
import json as jsonlib
import math
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from goliath_link.client import FirmwareCapabilities, format_version
from goliath_prep.core.canon import canonical_json, canonical_sha256
from goliath_prep.core.disclosure import Disclosure
from goliath_prep.core.types import ROTATION_MODELS

import goliath_run
from goliath_run.facts import (
    FINGERPRINT_FACT_IDS,
    STATIC_01_02_00_RULES,
    STATIC_FAMILY,
    Fact,
    FactStore,
    FingerprintStatus,
    firmware_tuple,
    freeze_json,
    thaw,
    utc_text,
)
from goliath_run.types import FrozenMap, Override

__all__ = [
    "EDGE_NAMES",
    "GATE_BASIS_KINDS",
    "GATE_CATALOGUE",
    "SNAPSHOT_SCHEMA",
    "Z_SEMANTICS",
    "FeatureGate",
    "GateClosedError",
    "GateOverride",
    "GateOverrideRefused",
    "GateSet",
    "GateSnapshot",
    "GateSpec",
    "OwnerPolicy",
    "SnapshotGateView",
    "UnknownGateError",
    "compute_gates",
    "load_gate_snapshot",
]

SNAPSHOT_SCHEMA = "goliath-run/gate-snapshot@1"

GATE_BASIS_KINDS: tuple[str, ...] = (
    "fact", "fact+vendor-gate", "fact+owner", "vendor-gate", "no-fact", "unknown", "firmware", "policy", "override",
)
"""Why a gate is in its state: its fact(s); its fact and the vendor's gate; its fact and the owner's consent; the
vendor's gate alone; no usable fact yet; the firmware version is unknown; the firmware version itself; the owner's
policy; a typed override."""

Z_SEMANTICS = "stock-top-zero-cuts-negative"
"""The value of fact ``z.zero_and_sign`` that opens ``z_semantics_verified``: work Z zero at the top of the stock,
cuts at negative Z (spec 05 §Semantics a replacement must match). Any other observed value goes to design review."""

EDGE_NAMES: tuple[str, ...] = ("x_min", "x_max", "y_min", "y_max")
"""Panel sides in the panel frame, for fact ``edges.near_verified`` (rung R9, per edge; safety-model 6.2)."""

_GATE_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
_RUNG = re.compile(r"R\d{1,2}[a-z]?\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_END_SIGNAL = re.compile(r"INFO;\d+(?:\+INFO;\d+)*\Z")
_DISCLOSURE_SOURCE = "goliath-run gate computation (the project design, sections 3.1, 3.2 and 11.5)"


class UnknownGateError(KeyError):
    """A gate name that is not in the catalogue (a typo is never read as a closed or an open gate)."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else "unknown gate"


class GateClosedError(Exception):
    """:meth:`GateSet.require` on a gate that is not open; ``gate`` says why and which rung unlocks it."""

    def __init__(self, gate: FeatureGate) -> None:
        state = {False: "closed", None: "uncertain, so closed"}[gate.allowed]  # type: ignore[index]
        unlock = "; unlocked by rung %s" % gate.unlock_rung if gate.unlock_rung else ""
        super().__init__("gate %s is %s: %s%s" % (gate.name, state, gate.reason, unlock))
        self.gate = gate


class GateOverrideRefused(ValueError):
    """A typed override of a gate that is not marked overridable; nothing is opened."""


def _text(what: str, value: object, *, empty: bool = False) -> str:
    if type(value) is not str:
        raise TypeError("%s must be a str, got %s" % (what, type(value).__name__))
    if not empty and not value.strip():
        raise ValueError("%s must not be empty" % what)
    return value


def _gate_name(name: object) -> str:
    text = _text("gate name", name)
    if not _GATE_NAME.match(text):
        raise ValueError("gate name %r: lower case, digits and _" % (text,))
    return text


# ---------------------------------------------------------------------------
# Gate values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureGate:
    """One gate (design 3.1): ``allowed`` True, False or None (uncertain, treated as False), and why.

    ``value`` is what an open gate carries (a rotation model, a verified program
    size, a park pose); it is None whenever the gate is not open, so a closed
    gate's value from the parameter catalogue applies.
    """

    name: str
    allowed: bool | None
    basis: str
    reason: str
    evidence: tuple[str, ...]
    overridable: bool
    unlock_rung: str | None
    value: Any = None

    def __post_init__(self) -> None:
        _gate_name(self.name)
        if self.allowed is not None and type(self.allowed) is not bool:
            raise TypeError("gate %s: allowed must be True, False or None, got %r" % (self.name, self.allowed))
        if self.basis not in GATE_BASIS_KINDS:
            raise ValueError("gate %s: basis %r is not one of %s"
                             % (self.name, self.basis, ", ".join(GATE_BASIS_KINDS)))
        _text("gate %s reason" % self.name, self.reason)
        if not isinstance(self.evidence, (list, tuple)):
            raise TypeError("gate %s: evidence must be a tuple of strings" % self.name)
        object.__setattr__(self, "evidence", tuple(_text("gate %s evidence" % self.name, e) for e in self.evidence))
        if type(self.overridable) is not bool:
            raise TypeError("gate %s: overridable must be a bool" % self.name)
        if self.unlock_rung is not None and not _RUNG.match(_text("gate %s unlock_rung" % self.name,
                                                                  self.unlock_rung)):
            raise ValueError("gate %s: unlock_rung %r: write it as R1, R0b, R8k ..." % (self.name, self.unlock_rung))
        if self.value is not None:
            if self.allowed is not True:
                raise ValueError("gate %s is not open, so it carries no value (got %r)" % (self.name, self.value))
            object.__setattr__(self, "value", freeze_json(self.value, "gate %s value" % self.name))

    @property
    def is_open(self) -> bool:
        """Only True opens: None (uncertain) is closed."""
        return self.allowed is True

    def describe(self) -> str:
        state = {True: "YES", False: "NO", None: "UNCERTAIN (treated as NO)"}[self.allowed]
        line = "%s: %s -- %s [basis: %s]" % (self.name, state, self.reason, self.basis)
        if self.value is not None:
            line += " value %s" % jsonlib.dumps(thaw(self.value), sort_keys=True)
        if self.allowed is not True and self.unlock_rung:
            line += " (unlocked by rung %s)" % self.unlock_rung
        if self.overridable:
            line += " (overridable with a typed reason)"
        return line

    def to_json(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "basis": self.basis, "reason": self.reason, "evidence": list(self.evidence),
                "overridable": self.overridable, "unlock_rung": self.unlock_rung, "value": thaw(self.value)}

    @classmethod
    def from_json(cls, name: str, data: object) -> FeatureGate:
        keys = {"allowed", "basis", "reason", "evidence", "overridable", "unlock_rung", "value"}
        if not isinstance(data, Mapping):
            raise TypeError("gate %s: expected a JSON object" % name)
        if set(data) != keys:
            raise ValueError("gate %s: keys %s, expected %s" % (name, sorted(map(str, data)), sorted(keys)))
        evidence = data["evidence"]
        if not isinstance(evidence, list):
            raise TypeError("gate %s: evidence must be a JSON array" % name)
        return cls(name=name, allowed=data["allowed"], basis=data["basis"], reason=data["reason"],
                   evidence=tuple(evidence), overridable=data["overridable"], unlock_rung=data["unlock_rung"],
                   value=data["value"])


@dataclass(frozen=True)
class GateSpec:
    """One row of the gate catalogue (design 3.2): the facts it reads, why it starts closed, what unlocks it."""

    name: str
    facts: tuple[str, ...]
    closed_reason: str
    unlock_rung: str | None
    unknown_id: str | None = None
    overridable: bool = False
    consent: bool = False


def _spec(name: str, facts: tuple[str, ...], closed_reason: str, unlock_rung: str | None,
          unknown_id: str | None = None, *, overridable: bool = False, consent: bool = False) -> GateSpec:
    return GateSpec(name, facts, closed_reason, unlock_rung, unknown_id, overridable, consent)


GATE_CATALOGUE: Mapping[str, GateSpec] = MappingProxyType({s.name: s for s in (
    _spec("jobs_allowed", ("firmware.version", "rung.r1.go") + FINGERPRINT_FACT_IDS,
          "no job until rung R1 has read the firmware version and the fingerprint of this robot", "R1", "U-01"),
    _spec("software_stop", ("stop.abort_work_verified",),
          "no software stop on any firmware before rung R14: stop the robot with its physical stop", "R14", "U-48"),
    _spec("keepalive_during_work", (),
          "nothing is sent while a job runs on 01.02.00: any command restarts the state machine's tick timers",
          None, "U-38"),
    _spec("upload_confirm_mode", ("upload.end_signal",),
          "info8-or-info0, used to classify only: after the last block the robot may be running whatever arrives",
          "R3", "U-16"),
    _spec("second_job_without_power_cycle", ("restart.second_job_ok",),
          "one upload per power cycle, on every firmware, until rung R12", "R12", "U-26"),
    _spec("panel_vertices_usable", ("panel.vertex_frame",),
          "uploads refused until rung R2 records the frame of the panel vertices", "R2", "U-13"),
    _spec("heading_map_verified", ("heading.map_verified", "footprint.axis_matches_body"),
          "the any-heading safe zone applies to every tool point", "R3", "U-20"),
    _spec("rotation_model", ("rotation.model",),
          "rotation model unverified: each rotation checked as the full circle, both directions", "R3", "U-21"),
    _spec("rotate_in_place_ok", ("rotation.in_place",),
          "a G00 with unchanged XY is not known to rotate in place (the safe zone covers it)", "R3", "U-21"),
    _spec("z_semantics_verified", ("z.zero_and_sign",), "air jobs only: Z stays at +5 mm or above", "R3", "U-23"),
    _spec("spindle_verified", ("spindle.m10_m05",), "no M10 in any program", "R4", "U-24"),
    _spec("drop_dwells", ("spindle.spinup_wait",), "G4 refused", "R4", "U-24", consent=True),
    _spec("first_cut_done", ("cut.first_success",), "only rung jobs", "R5"),
    _spec("arcs_emit", ("arcs.verified",), "arcs written as G01 segments", "R6", "U-32"),
    _spec("through_cuts_allowed", ("cut.through_verified",), "no through cuts", "R7"),
    _spec("tabs_hold", ("tabs.hold_verified",), "no tab relaxation: t_hold = T/2", "R7", "U-42", consent=True),
    _spec("pockets_allowed", ("pocket.verified",), "no pockets", "R8"),
    _spec("kerf_crossing", ("kerf.crossing",),
          "every removal deeper than 0.02 mm is a wheel keep-out (no measured kerf values)", "R8k", "U-41"),
    _spec("near_edge_allowed", ("edges.near_verified",),
          "wheel edge margin 50 mm on every edge (bring-up margin, INFERRED)", "R9", "U-50"),
    _spec("max_program_bytes", ("upload.max_verified_bytes",), "programs up to 32 KiB (INFERRED)", "R10", "U-39"),
    _spec("m11_emit", ("m11.verified",), "M11 never emitted", "R11", "U-33"),
    _spec("jog_allowed", ("jog.verified",), "jog not offered", "R13", "U-29"),
    _spec("series_allowed", ("series.verified", "calibration.location", "detection.path"),
          "series and multi-tool jobs refused", "R15", "U-43"),
    _spec("sanity_baseline", ("sanity.baseline.idle", "status_blob.baseline.idle"),
          "no INFO;40 and status baseline: blocked until rung R1 has run", "R1", "U-08", overridable=True),
    _spec("stop_verified_on_motion", ("stop.physical_on_motion",),
          "the physical stop has not been drilled on the robot's own motion: R3 refused", "R2", "U-25"),
    _spec("start_pose_known", ("detection.park_pose",),
          "no recorded park pose: the first line cannot be checked (W-FIRST refuses)", "R2", "U-44"),
    _spec("frames_end_upload", ("frames.stream_outside_work",),
          "position frames alone never end an upload nor show a running job", "R2", "U-52"),
    _spec("post_job_motion_known", ("post_job.motion",),
          "motion after INFO;23 unknown: through cuts and pockets refused, other jobs carry a disclosure", "R3",
          "U-36"),
    _spec("wire_attachment_measured", ("wire.attachment", "wire.same_point", "wire.back_mm", "wire.right_mm"),
          "wire attachment unmeasured: P-REACH uses the whole body hull", "R0b", "U-49"),
    _spec("link_keepalive_ok", ("link.keepalive_ok",),
          "whether an idle link survives is unknown: keepalive timing disclosed on the setup screens", "R1b", "U-38"),
)})
"""The gates of design 3.2, with the facts each is computed from."""


# ---------------------------------------------------------------------------
# Owner policy and typed overrides
# ---------------------------------------------------------------------------


def _reasons(what: str, data: object, *, consent_only: bool) -> FrozenMap:
    if not isinstance(data, Mapping):
        raise TypeError("OwnerPolicy %s must map gate names to reasons" % what)
    out: dict[str, str] = {}
    for name, reason in data.items():
        if _gate_name(name) not in GATE_CATALOGUE:
            raise UnknownGateError("OwnerPolicy %s names gate %r, which is not in the gate catalogue" % (what, name))
        if consent_only and not GATE_CATALOGUE[name].consent:
            raise ValueError("OwnerPolicy consents: gate %s takes no owner consent (only %s do)"
                             % (name, ", ".join(n for n, s in GATE_CATALOGUE.items() if s.consent)))
        out[name] = _text("OwnerPolicy %s reason for %s" % (what, name), reason)
    return FrozenMap(out)


@dataclass(frozen=True)
class OwnerPolicy:
    """What the owner decided (settings, job file): gates to keep closed, and consents some gates need too.

    ``closed`` maps a gate to the owner's reason; ``consents`` maps a gate that
    needs the owner's agreement besides its fact (``drop_dwells``, ``tabs_hold``)
    to that agreement. ``source`` says where the policy comes from. A policy
    cannot open anything: it has no field that could.
    """

    closed: Mapping[str, str]
    consents: Mapping[str, str]
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "closed", _reasons("closed", self.closed, consent_only=False))
        object.__setattr__(self, "consents", _reasons("consents", self.consents, consent_only=True))
        _text("OwnerPolicy source", self.source)

    @classmethod
    def none(cls) -> OwnerPolicy:
        """No owner policy: nothing closed by the owner, no consent given (it is named in every snapshot)."""
        return cls(closed={}, consents={}, source="no owner policy given")

    def to_json(self) -> dict[str, Any]:
        return {"closed": dict(self.closed), "consents": dict(self.consents), "source": self.source}


@dataclass(frozen=True)
class GateOverride:
    """A typed override (confirmation K7) as recorded: the gate, why, who and when (design 3.1).

    The interface's :class:`goliath_run.types.Override` intent carries the gate
    and the reason; :meth:`from_intent` adds the operator and the time.
    """

    gate: str
    reason: str
    operator: str
    at: str

    def __post_init__(self) -> None:
        _gate_name(self.gate)
        _text("override reason", self.reason)
        _text("override operator", self.operator)
        object.__setattr__(self, "at", utc_text(self.at, "override time"))

    @classmethod
    def from_intent(cls, intent: Override, *, operator: str, at: object) -> GateOverride:
        if not isinstance(intent, Override):
            raise TypeError("from_intent takes an Override intent, got %s" % type(intent).__name__)
        return cls(gate=intent.gate, reason=intent.reason, operator=operator, at=at)  # type: ignore[arg-type]

    def to_json(self) -> dict[str, Any]:
        return {"gate": self.gate, "reason": self.reason, "operator": self.operator, "at": self.at}


# ---------------------------------------------------------------------------
# Computing the gates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Result:
    allowed: bool | None
    basis: str
    reason: str
    evidence: tuple[str, ...] = ()
    value: Any = None


@dataclass(frozen=True)
class _Ctx:
    caps: FirmwareCapabilities
    store: FactStore
    status: FingerprintStatus
    policy: OwnerPolicy

    @property
    def version(self) -> tuple[int, int, int] | None:
        return self.caps.version  # type: ignore[return-value]


def _evidence(fact: Fact) -> tuple[str, ...]:
    head = "fact %s = %s (%s, rung %s, %s)" % (fact.id, jsonlib.dumps(thaw(fact.value), sort_keys=True),
                                              fact.basis, fact.rung, fact.recorded_at)
    return (head,) + fact.evidence


def _usable(ctx: _Ctx, fact_id: str, bases: tuple[str, ...] = ("observed",)) -> tuple[Fact | None, str]:
    """The fact if it may open a gate, else None and why not."""
    fact = ctx.store.get(fact_id)
    if fact is None:
        return None, "no fact %s in force for this robot and firmware" % fact_id
    if fact.basis not in bases:
        return None, "fact %s has basis %s; only %s facts open this gate" % (fact_id, fact.basis, " or ".join(bases))
    if fact.rung is None:
        return None, "fact %s was not recorded through a rung's go criteria" % fact_id
    return fact, ""


def _closed(spec: GateSpec, why: str, basis: str = "no-fact", evidence: tuple[str, ...] = (),
            allowed: bool | None = False) -> _Result:
    return _Result(allowed, basis, "%s (%s)" % (spec.closed_reason, why), evidence)


def _flag_gate(fact_id: str) -> Callable[[_Ctx, GateSpec], _Result]:
    """A gate that opens on one observed fact whose value is exactly True."""

    def compute(ctx: _Ctx, spec: GateSpec) -> _Result:
        fact, why = _usable(ctx, fact_id)
        if fact is None:
            return _closed(spec, why)
        if fact.value is not True:
            return _closed(spec, "fact %s is %r, not true" % (fact_id, thaw(fact.value)), "fact", _evidence(fact))
        return _Result(True, "fact", "fact %s recorded at rung %s" % (fact_id, fact.rung), _evidence(fact))

    return compute


def _value_gate(fact_id: str, check: Callable[[object], object | None], expects: str,
                bases: tuple[str, ...] = ("observed",)) -> Callable[[_Ctx, GateSpec], _Result]:
    """A gate that opens on one fact whose value ``check`` accepts (it returns the gate's value, or None)."""

    def compute(ctx: _Ctx, spec: GateSpec) -> _Result:
        fact, why = _usable(ctx, fact_id, bases)
        if fact is None:
            return _closed(spec, why)
        value = check(fact.value)
        if value is None:
            return _closed(spec, "fact %s is %r; it opens only with %s" % (fact_id, thaw(fact.value), expects),
                           "fact", _evidence(fact))
        return _Result(True, "fact", "fact %s recorded at rung %s" % (fact_id, fact.rung), _evidence(fact), value)

    return compute


def _number(value: object, *, positive: bool = False) -> bool:
    if type(value) not in (int, float) or not math.isfinite(value):  # type: ignore[arg-type]
        return False
    return value > 0 if positive else value >= 0  # type: ignore[operator]


def _check_frame(value: object) -> object | None:
    return value if value == "identity" else None


def _check_rotation(value: object) -> object | None:
    return value if type(value) is str and value in ROTATION_MODELS and value != "unverified" else None


def _check_z(value: object) -> object | None:
    return value if value == Z_SEMANTICS else None


def _check_end_signal(value: object) -> object | None:
    return value if type(value) is str and _END_SIGNAL.match(value) else None


def _check_bytes(value: object) -> object | None:
    return value if type(value) is int and value > 0 else None


def _check_kerf(value: object) -> object | None:
    if isinstance(value, Mapping) and set(value) == {"w_b", "L_ride", "h_r"} and all(
            _number(value[k]) for k in value):
        return value
    return None


def _check_edges(value: object) -> object | None:
    if isinstance(value, tuple) and value and len(set(value)) == len(value) and all(v in EDGE_NAMES for v in value):
        return value
    return None


def _check_pose(value: object) -> object | None:
    if (isinstance(value, Mapping) and set(value) == {"x", "y", "c"} and _finite(value["x"]) and _finite(value["y"])
            and type(value["c"]) is int):
        return value
    return None


def _finite(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)  # type: ignore[arg-type]


def _check_post_job(value: object) -> object | None:
    if value == "none":
        return value
    if (isinstance(value, Mapping) and value.get("kind") == "park" and isinstance(value.get("path"), tuple)
            and value["path"]):
        return value
    return None


def _check_sanity(value: object) -> bool:
    return (isinstance(value, Mapping) and isinstance(value.get("values"), tuple) and len(value["values"]) > 0
            and type(value.get("raw")) is str and bool(value["raw"]))


def _check_blob(value: object) -> bool:
    return (isinstance(value, Mapping) and type(value.get("kind")) is str and value["kind"] in ("blob48", "gs19")
            and type(value.get("raw")) is str and bool(value["raw"]))


def _measured(fact: Fact) -> object:
    """The answer of a robot-measured fact (its value is {"value", "unit", "tolerance"})."""
    if isinstance(fact.value, Mapping) and set(fact.value) == {"value", "unit", "tolerance"}:
        return fact.value["value"]
    return None


def _jobs_allowed(ctx: _Ctx, spec: GateSpec) -> _Result:
    version = ctx.version
    if version is None:
        return _Result(None, "unknown", "FIRMWARE UNKNOWN (U-01): the version has not been read in this session; "
                                        "read-only, jobs refused")
    fw = format_version(version)
    if version < firmware_tuple(STATIC_FAMILY):
        return _Result(False, "firmware", "firmware %s is older than %s (branch B4): no model of its command set; "
                                          "probe only, design review" % (fw, STATIC_FAMILY))
    go, why = _usable(ctx, "rung.r1.go")
    seen, why_fw = _usable(ctx, "firmware.version")
    if go is None or go.value is not True or seen is None:
        return _Result(False, "no-fact", "rung R1 has not passed for firmware %s on this robot (%s): run the "
                                         "read-only probe and import its report" % (fw, why or why_fw or
                                                                                    "rung.r1.go is not true"))
    evidence = _evidence(go) + _evidence(seen)
    if seen.value != fw:
        return _Result(False, "fact", "fact firmware.version is %r but this session reads %s" % (seen.value, fw),
                       evidence)
    status = ctx.status
    if status.state == "differs":
        branch = "B2" if fw == STATIC_FAMILY else "B3, contradicting its baseline"
        return _Result(False, "fact", "branch %s: %s; jobs refused, design review" % (branch, status.reason),
                       evidence)
    if status.state == "unverified":
        return _Result(None, "fact", "the fingerprint is unverified (%s): run the probe again with all three "
                                     "terminator variants and the full sweep" % status.reason, evidence)
    if status.state == "matches":
        return _Result(True, "fact", "branch B1: firmware %s, the fingerprint matches the %s image, rung R1 passed"
                       % (fw, STATIC_FAMILY), evidence)
    if status.state == "baseline-recorded":
        return _Result(True, "fact", "branch B3: firmware %s; the fingerprint baseline recorded at R1 holds and "
                                     "nothing since contradicts it" % fw, evidence)
    return _Result(False, "fact", "no fingerprint model for firmware %s: %s" % (fw, status.reason), evidence)


def _software_stop(ctx: _Ctx, spec: GateSpec) -> _Result:
    decision = ctx.caps.supports_command("ABORT WORK")
    if not ctx.caps.has_abort_work:
        return _closed(spec, "vendor gate: %s" % decision.reason, "vendor-gate")
    fact, why = _usable(ctx, "stop.abort_work_verified")
    if fact is None or fact.value is not True:
        return _closed(spec, why or "fact stop.abort_work_verified is not true",
                       "no-fact" if fact is None else "fact", () if fact is None else _evidence(fact))
    return _Result(True, "fact+vendor-gate", "ABORT WORK verified at rung %s on this firmware" % fact.rung,
                   _evidence(fact) + ("vendor gate: %s" % decision.reason,))


def _keepalive(ctx: _Ctx, spec: GateSpec) -> _Result:
    version = ctx.version
    if version is not None and version > firmware_tuple(STATIC_FAMILY):
        return _Result(True, "vendor-gate", "firmware %s > 01.02.00: the vendor app sent a keep-alive every 60 s "
                                            "during work there (spec 07); unverified on this robot"
                       % format_version(version), ("FirmwareCapabilities.for_version(%s)" % format_version(version),))
    return _closed(spec, "firmware %s" % format_version(version), "vendor-gate")


def _upload_confirm(ctx: _Ctx, spec: GateSpec) -> _Result:
    fact, why = _usable(ctx, "upload.end_signal")
    if fact is not None:
        value = _check_end_signal(fact.value)
        if value is not None:
            return _Result(True, "fact", "fact upload.end_signal recorded at rung %s" % fact.rung, _evidence(fact),
                           value)
        why = "fact upload.end_signal is %r, not INFO;n[+INFO;n]" % (thaw(fact.value),)
    info8 = ctx.caps.confirms_upload_with_info8
    reason = next((d.reason for d in ctx.caps.decisions if d.feature.startswith("INFO;8")), "")
    if info8 is True:
        return _Result(True, "vendor-gate", "INFO;8 as the upload confirmation: %s; unverified on this robot"
                       % reason, ("FirmwareCapabilities: confirms_upload_with_info8",), "info8")
    return _closed(spec, "%s; vendor gate: %s" % (why, reason), "vendor-gate",
                   allowed=None if info8 is None else False)


def _consent_gate(fact_id: str) -> Callable[[_Ctx, GateSpec], _Result]:
    flag = _flag_gate(fact_id)

    def compute(ctx: _Ctx, spec: GateSpec) -> _Result:
        result = flag(ctx, spec)
        if result.allowed is not True:
            return result
        consent = ctx.policy.consents.get(spec.name)
        if consent is None:
            return _closed(spec, "fact %s is recorded, but the owner has not consented (%s)"
                           % (fact_id, ctx.policy.source), "fact", result.evidence)
        return _Result(True, "fact+owner", "%s; owner consent (%s): %s" % (result.reason, ctx.policy.source, consent),
                       result.evidence)

    return compute


def _heading_map(ctx: _Ctx, spec: GateSpec) -> _Result:
    fact, why = _usable(ctx, "heading.map_verified")
    axis, why_axis = _usable(ctx, "footprint.axis_matches_body", ("robot-measured",))
    if fact is None or axis is None:
        return _closed(spec, why or why_axis)
    evidence = _evidence(fact) + _evidence(axis)
    if fact.value is not True or _measured(axis) != "yes":
        return _closed(spec, "heading.map_verified is %r and footprint.axis_matches_body is %r"
                       % (thaw(fact.value), thaw(_measured(axis))), "fact", evidence)
    return _Result(True, "fact", "heading map verified at rung %s, body axis measured at rung %s"
                   % (fact.rung, axis.rung), evidence)


def _series(ctx: _Ctx, spec: GateSpec) -> _Result:
    found = [_usable(ctx, fid) for fid in spec.facts]
    missing = [why for fact, why in found if fact is None]
    if missing:
        return _closed(spec, "; ".join(missing))
    facts = [f for f, _ in found if f is not None]
    evidence = tuple(e for f in facts for e in _evidence(f))
    verified, location, path = facts
    if verified.value is not True or not (isinstance(location.value, Mapping) and location.value) or not (
            isinstance(path.value, Mapping) and path.value):
        return _closed(spec, "series.verified must be true and calibration.location and detection.path recorded",
                       "fact", evidence)
    return _Result(True, "fact", "series verified at rung %s" % verified.rung, evidence,
                   {"calibration_location": location.value, "detection_path": path.value})


def _sanity(ctx: _Ctx, spec: GateSpec) -> _Result:
    sanity, why = _usable(ctx, "sanity.baseline.idle")
    blob, why_blob = _usable(ctx, "status_blob.baseline.idle")
    if sanity is None or blob is None:
        return _closed(spec, why or why_blob)
    evidence = _evidence(sanity) + _evidence(blob)
    if not (_check_sanity(sanity.value) and _check_blob(blob.value)):
        return _closed(spec, "the recorded baselines are not in the form rung R1 writes", "fact", evidence)
    return _Result(True, "fact", "INFO;40 and status baselines recorded at rung %s" % sanity.rung, evidence,
                   {"sanity": sanity.value, "status_blob": blob.value})


def _wire(ctx: _Ctx, spec: GateSpec) -> _Result:
    attachment, why = _usable(ctx, "wire.attachment", ("robot-measured",))
    same, why_same = _usable(ctx, "wire.same_point", ("robot-measured",))
    if attachment is None or same is None:
        return _closed(spec, why or why_same)
    evidence = _evidence(attachment) + _evidence(same)
    kind = _measured(attachment)
    if _measured(same) != "yes" or kind not in ("on_spindle_axis", "offset"):
        return _closed(spec, "wire.attachment is %r and wire.same_point is %r: design review"
                       % (thaw(kind), thaw(_measured(same))), "fact", evidence)
    if kind == "on_spindle_axis":
        return _Result(True, "fact", "wires attach on the spindle axis (measured at rung %s)" % attachment.rung,
                       evidence, {"kind": "on_spindle_axis"})
    back, why_back = _usable(ctx, "wire.back_mm", ("robot-measured",))
    right, why_right = _usable(ctx, "wire.right_mm", ("robot-measured",))
    if back is None or right is None:
        return _closed(spec, why_back or why_right, "fact", evidence)
    evidence += _evidence(back) + _evidence(right)
    in_mm = all(isinstance(f.value, Mapping) and f.value.get("unit") == "mm" and _finite(_measured(f))
                for f in (back, right))
    if not in_mm:
        return _closed(spec, "the wire offset is not two distances measured in mm", "fact", evidence)
    return _Result(True, "fact", "wires attach at a measured offset from the tool (rung %s)" % attachment.rung,
                   evidence, {"kind": "offset", "back_mm": _measured(back), "right_mm": _measured(right)})


def _frames_end_upload(ctx: _Ctx, spec: GateSpec) -> _Result:
    fact, why = _usable(ctx, "frames.stream_outside_work")
    if fact is None:
        return _closed(spec, why)
    if fact.value is not False:
        return _closed(spec, "fact frames.stream_outside_work is %r: frames are no sign of work" % thaw(fact.value),
                       "fact", _evidence(fact))
    return _Result(True, "fact", "frames do not stream outside work (rung %s)" % fact.rung, _evidence(fact))


_COMPUTE: Mapping[str, Callable[[_Ctx, GateSpec], _Result]] = MappingProxyType({
    "jobs_allowed": _jobs_allowed,
    "software_stop": _software_stop,
    "keepalive_during_work": _keepalive,
    "upload_confirm_mode": _upload_confirm,
    "second_job_without_power_cycle": _flag_gate("restart.second_job_ok"),
    "panel_vertices_usable": _value_gate("panel.vertex_frame", _check_frame,
                                         "'identity' (any other frame goes to design review)"),
    "heading_map_verified": _heading_map,
    "rotation_model": _value_gate("rotation.model", _check_rotation, "one of %s" % ", ".join(
        m for m in ROTATION_MODELS if m != "unverified")),
    "rotate_in_place_ok": _flag_gate("rotation.in_place"),
    "z_semantics_verified": _value_gate("z.zero_and_sign", _check_z,
                                        "%r (any other observation goes to design review)" % Z_SEMANTICS),
    "spindle_verified": _flag_gate("spindle.m10_m05"),
    "drop_dwells": _consent_gate("spindle.spinup_wait"),
    "first_cut_done": _flag_gate("cut.first_success"),
    "arcs_emit": _flag_gate("arcs.verified"),
    "through_cuts_allowed": _flag_gate("cut.through_verified"),
    "tabs_hold": _consent_gate("tabs.hold_verified"),
    "pockets_allowed": _flag_gate("pocket.verified"),
    "kerf_crossing": _value_gate("kerf.crossing", _check_kerf, "measured w_b, L_ride and h_r in mm"),
    "near_edge_allowed": _value_gate("edges.near_verified", _check_edges, "a list of tested edges from %s"
                                     % ", ".join(EDGE_NAMES)),
    "max_program_bytes": _value_gate("upload.max_verified_bytes", _check_bytes, "a whole number of bytes"),
    "m11_emit": _flag_gate("m11.verified"),
    "jog_allowed": _flag_gate("jog.verified"),
    "series_allowed": _series,
    "sanity_baseline": _sanity,
    "stop_verified_on_motion": _flag_gate("stop.physical_on_motion"),
    "start_pose_known": _value_gate("detection.park_pose", _check_pose, "a pose {x, y, c}"),
    "frames_end_upload": _frames_end_upload,
    "post_job_motion_known": _value_gate("post_job.motion", _check_post_job,
                                         "'none' or a park path {kind: park, path: [...]}"),
    "wire_attachment_measured": _wire,
    "link_keepalive_ok": _flag_gate("link.keepalive_ok"),
})
if set(_COMPUTE) != set(GATE_CATALOGUE):  # pragma: no cover - a programming error caught at import
    raise RuntimeError("gate catalogue and gate computations disagree")


def _branch(version: tuple[int, int, int] | None, status: FingerprintStatus) -> str | None:
    if version is None:
        return None
    if version < firmware_tuple(STATIC_FAMILY):
        return "B4"
    if version > firmware_tuple(STATIC_FAMILY):
        return "B3"
    return {"matches": "B1", "differs": "B2"}.get(status.state, "unverified")


def compute_gates(caps: FirmwareCapabilities, facts: FactStore, policy: OwnerPolicy,
                  overrides: Iterable[GateOverride] = ()) -> GateSet:
    """The gate set for this robot and session: capabilities, facts and policy in, one tri-state gate each out.

    ``caps`` must describe the firmware the store was opened for (a mismatch is
    a programming error and raises). ``overrides`` are typed overrides as
    recorded (:class:`GateOverride`); one on a gate that is not overridable
    raises :class:`GateOverrideRefused`.
    """
    if not isinstance(caps, FirmwareCapabilities):
        raise TypeError("compute_gates needs goliath_link's FirmwareCapabilities, got %s" % type(caps).__name__)
    if not isinstance(facts, FactStore):
        raise TypeError("compute_gates needs a FactStore, got %s" % type(facts).__name__)
    if not isinstance(policy, OwnerPolicy):
        raise TypeError("compute_gates needs an OwnerPolicy (OwnerPolicy.none() for none), got %s"
                        % type(policy).__name__)
    typed: list[GateOverride] = []
    for o in overrides:
        if not isinstance(o, GateOverride):
            raise TypeError("overrides must be GateOverride records (who, when, why); got %s: build one with "
                            "GateOverride.from_intent" % type(o).__name__)
        if o.gate not in GATE_CATALOGUE:
            raise UnknownGateError("override of gate %r, which is not in the gate catalogue" % o.gate)
        if not GATE_CATALOGUE[o.gate].overridable:
            raise GateOverrideRefused("gate %s cannot be overridden: only %s take a typed override" % (
                o.gate, ", ".join(n for n, s in GATE_CATALOGUE.items() if s.overridable)))
        if any(t.gate == o.gate for t in typed):
            raise ValueError("two overrides for gate %s" % o.gate)
        typed.append(o)
    session_fw = None if caps.version is None else format_version(caps.version)
    if session_fw != facts.firmware:
        raise ValueError("the capabilities describe firmware %s but the fact store was opened for %s"
                         % (session_fw or "unknown", facts.firmware or "unknown"))

    ctx = _Ctx(caps, facts, facts.fingerprint_status(), policy)
    gates: dict[str, FeatureGate] = {}
    for name, spec in GATE_CATALOGUE.items():
        res = _COMPUTE[name](ctx, spec)
        gates[name] = FeatureGate(name=name, allowed=res.allowed, basis=res.basis, reason=res.reason,
                                  evidence=res.evidence, overridable=spec.overridable, unlock_rung=spec.unlock_rung,
                                  value=res.value if res.allowed is True else None)
    notes: list[str] = []
    applied: list[GateOverride] = []
    for o in typed:
        gate = gates[o.gate]
        if gate.allowed is True:
            notes.append("override of %s by %s at %s not applied: the gate is open from its facts"
                         % (o.gate, o.operator, o.at))
            continue
        gates[o.gate] = replace(gate, allowed=True, basis="override", value=None,
                                reason="overridden by %s at %s: %s (closed because: %s)"
                                % (o.operator, o.at, o.reason, gate.reason),
                                evidence=gate.evidence + ("typed override K7 by %s at %s" % (o.operator, o.at),))
        applied.append(o)
    for name, reason in policy.closed.items():
        gate = gates[name]
        if gate.basis == "override":
            applied = [o for o in applied if o.gate != name]
            notes.append("override of %s not applied: the owner's policy keeps it closed" % name)
        gates[name] = replace(gate, allowed=False, basis="policy", value=None,
                              reason="closed by the owner's policy (%s): %s" % (policy.source, reason))
    return GateSet(gates=gates, store=facts, caps=caps, status=ctx.status, policy=policy, overrides=tuple(typed),
                   applied=tuple(applied), notes=tuple(notes))


def _disclosures(gates: Mapping[str, FeatureGate], store: FactStore, caps: FirmwareCapabilities,
                 status: FingerprintStatus, applied: tuple[GateOverride, ...]) -> tuple[Disclosure, ...]:
    out: list[Disclosure] = []

    def add(id: str, severity: str, text: str) -> None:
        out.append(Disclosure(id=id, severity=severity, text=text, source=_DISCLOSURE_SOURCE))  # type: ignore[arg-type]

    version = caps.version
    if version is None:
        add("FIRMWARE_UNKNOWN", "amber", "FIRMWARE UNKNOWN: read-only. Jobs are refused until the firmware version "
                                         "is read (rung R1; U-01)")
    elif version < firmware_tuple(STATIC_FAMILY):
        add("FIRMWARE_OLDER", "red", "firmware %s is older than %s (branch B4): no model of its command set; probe "
                                     "only, design review" % (format_version(version), STATIC_FAMILY))
    else:
        fw = format_version(version)
        if status.state == "differs":
            add("FINGERPRINT_DIFFERS", "red",
                "FINGERPRINT DIFFERS: %s. Jobs are refused; design review" % status.reason)
        elif status.state == "unverified":
            add("FINGERPRINT_UNVERIFIED", "amber", "fingerprint unverified: %s" % status.reason)
        if version > firmware_tuple(STATIC_FAMILY):
            add("B3_NEW_BASELINE", "amber",
                "firmware %s is newer than %s (branch B3): its fingerprint is the baseline recorded at rung R1, not "
                "a firmware image; nothing derived from %s static analysis is a fact on it" % (fw, STATIC_FAMILY,
                                                                                            STATIC_FAMILY))
            for rule, text in STATIC_01_02_00_RULES.items():
                add("UNVERIFIED_FOR_VERSION:%s" % rule, "amber",
                    "%s: derived from static analysis of %s, unverified for %s" % (text, STATIC_FAMILY, fw))
    for fact in store.unverified():
        add("UNVERIFIED_FOR_VERSION:%s" % fact.id, "amber", "fact %s: %s" % (fact.id, store.why_unverified(fact.id)))
    stale = store.stale_summary()
    if stale:
        add("FACTS_STALE", "red", stale)
    for name, gate in gates.items():
        if gate.allowed is True and "vendor-gate" in gate.basis and not gate.basis.startswith("fact"):
            add("VENDOR_GATE:%s" % name, "amber", "%s rests only on the vendor app's gate: %s" % (name, gate.reason))
    for o in applied:
        add("OVERRIDE:%s" % o.gate, "red", "%s OVERRIDDEN by %s at %s: %s" % (o.gate, o.operator, o.at, o.reason))
    replay = store.get("replay.fresh_boot")
    if replay is not None and isinstance(replay.value, Mapping) and 8 in (replay.value.get("codes") or ()):
        add("REPLAY_INFO8_UNAVAILABLE", "amber",
            "the fresh-boot replay of this robot already includes INFO;8 (a program was left on it): INFO;8 is no "
            "evidence about a restart here (design 7.7)")
    return tuple(out)


# ---------------------------------------------------------------------------
# The gate set and its views
# ---------------------------------------------------------------------------


class GateSet:
    """The gates in force for one robot and session. Immutable; built by :func:`compute_gates`."""

    def __init__(self, *, gates: Mapping[str, FeatureGate], store: FactStore, caps: FirmwareCapabilities,
                 status: FingerprintStatus, policy: OwnerPolicy, overrides: tuple[GateOverride, ...],
                 applied: tuple[GateOverride, ...], notes: tuple[str, ...]) -> None:
        self._gates = MappingProxyType(dict(gates))
        self._serial = store.serial
        self._firmware = store.firmware
        self._status = status
        self._policy = policy
        self._overrides = overrides
        self._notes = notes
        self._branch = _branch(caps.version, status)  # type: ignore[arg-type]
        self._disclosures = _disclosures(self._gates, store, caps, status, applied)
        self._facts = {fid: f for fid, f in sorted(store.current().items())}
        self._facts_sha256 = store.sha256()
        self._stale = tuple(sorted(f.id for f in store.stale()))
        self._unverified = tuple(sorted(f.id for f in store.unverified()))
        self._snapshot: GateSnapshot | None = None

    @property
    def gates(self) -> Mapping[str, FeatureGate]:
        return self._gates

    @property
    def branch(self) -> str | None:
        """The R1 branch (design 11.2): B1, B2, B3, B4, ``unverified``, or None while the firmware is unknown."""
        return self._branch

    @property
    def disclosures(self) -> tuple[Disclosure, ...]:
        """What every screen must show while these gates are in force (red, amber)."""
        return self._disclosures

    @property
    def fingerprint(self) -> FingerprintStatus:
        return self._status

    def gate(self, name: str) -> FeatureGate:
        gate = self._gates.get(_gate_name(name))
        if gate is None:
            raise UnknownGateError("gate %r is not in the gate catalogue" % name)
        return gate

    def allowed(self, name: str) -> bool | None:
        """True, False or None; an unknown name is False (never True)."""
        gate = self._gates.get(_gate_name(name))
        return False if gate is None else gate.allowed

    def value(self, name: str) -> object:
        gate = self._gates.get(_gate_name(name))
        return None if gate is None or gate.allowed is not True else gate.value

    def require(self, name: str) -> None:
        """Return only if the gate is open (True); else raise :class:`GateClosedError` naming the reason and rung."""
        gate = self.gate(name)
        if gate.allowed is not True:
            raise GateClosedError(gate)

    def explain(self) -> list[str]:
        """One line per gate, then the notes (overrides not applied), for the machine screen and the records."""
        lines = ["firmware %s, branch %s, fingerprint %s: %s" % (
            self._firmware or "unknown", self._branch or "none", self._status.state, self._status.reason)]
        lines += [gate.describe() for gate in self._gates.values()]
        lines += ["note: %s" % n for n in self._notes]
        lines += ["disclosure [%s] %s" % (d.severity, d.text) for d in self._disclosures]
        return lines

    def as_prep_view(self) -> _LiveGateView:
        """These gates as goliath-prep's ``GateView`` (in-process; the same answers as the snapshot)."""
        return _LiveGateView(self)

    def snapshot(self) -> GateSnapshot:
        """The gates and facts in force as canonical JSON with its SHA-256 (``machine export-gates``)."""
        if self._snapshot is None:
            data = {
                "schema": SNAPSHOT_SCHEMA,
                "goliath_run": goliath_run.__version__,
                "serial": self._serial,
                "firmware": self._firmware,
                "branch": self._branch,
                "fingerprint": {"family": self._status.family, "state": self._status.state,
                                "reason": self._status.reason},
                "gates": {name: gate.to_json() for name, gate in self._gates.items()},
                "facts": {fid: {"value": thaw(f.value), "basis": f.basis, "rung": f.rung, "unknown_id": f.unknown_id,
                                "firmware": f.firmware, "recorded_at": f.recorded_at}
                          for fid, f in self._facts.items()},
                "facts_sha256": self._facts_sha256,
                "stale": list(self._stale),
                "unverified": list(self._unverified),
                "disclosures": [d.to_json() for d in self._disclosures],
                "overrides": [o.to_json() for o in self._overrides],
                "policy": self._policy.to_json(),
            }
            self._snapshot = GateSnapshot(json=data, sha256=canonical_sha256(data))
        return self._snapshot


class _LiveGateView:
    """:class:`goliath_prep.core.types.GateView` over a :class:`GateSet`."""

    def __init__(self, gates: GateSet) -> None:
        self._gates = gates

    def allowed(self, name: str) -> bool | None:
        return self._gates.allowed(name)

    def value(self, name: str) -> object:
        return self._gates.value(name)

    def snapshot_sha256(self) -> str:
        return self._gates.snapshot().sha256


@dataclass(frozen=True)
class GateSnapshot:
    """A gate snapshot: plain JSON data and the SHA-256 of its canonical form (which certificates bind)."""

    json: Mapping[str, Any] = field(hash=False)
    sha256: str = ""

    def __post_init__(self) -> None:
        data = jsonlib.loads(canonical_json(self.json))  # a private copy, as the canonical form reads back
        object.__setattr__(self, "json", data)
        if self.sha256 != canonical_sha256(data):
            raise ValueError("gate snapshot sha256 %r does not match its content" % self.sha256)

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.json)

    def write(self, path: str | os.PathLike[str]) -> Path:
        """Write the canonical bytes atomically (temporary file, fsync, rename); return the path."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".gates-", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(self.canonical_bytes())
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        return target


_SNAPSHOT_KEYS = frozenset({"schema", "goliath_run", "serial", "firmware", "branch", "fingerprint", "gates", "facts",
                            "facts_sha256", "stale", "unverified", "disclosures", "overrides", "policy"})


@dataclass(frozen=True)
class SnapshotGateView:
    """goliath-prep's ``GateView`` read from a gate snapshot file (``--gates``); see :func:`load_gate_snapshot`."""

    gates: Mapping[str, FeatureGate]
    serial: str
    firmware: str | None
    sha256: str

    def allowed(self, name: str) -> bool | None:
        gate = self.gates.get(_gate_name(name))
        return False if gate is None else gate.allowed

    def value(self, name: str) -> object:
        gate = self.gates.get(_gate_name(name))
        return None if gate is None or gate.allowed is not True else gate.value

    def snapshot_sha256(self) -> str:
        return self.sha256


def load_gate_snapshot(source: bytes | str | os.PathLike[str] | Mapping[str, Any], *,
                       expect_sha256: str | None = None) -> SnapshotGateView:
    """Read a gate snapshot (bytes, a file path or parsed JSON) as a ``GateView``.

    The schema, every catalogue gate and every key are checked; its SHA-256 is
    computed from the canonical form, and must equal ``expect_sha256`` when one
    is given (the hash a certificate bound). An edited snapshot is a different
    snapshot: its hash changes.
    """
    if isinstance(source, Mapping):
        data: object = source
    else:
        raw = source if isinstance(source, bytes) else Path(source).read_bytes()
        try:
            data = jsonlib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, jsonlib.JSONDecodeError) as exc:
            raise ValueError("the gate snapshot is not JSON: %s" % exc) from exc
    if not isinstance(data, Mapping):
        raise ValueError("a gate snapshot is a JSON object")
    if data.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError("gate snapshot schema %r; this version reads %s" % (data.get("schema"), SNAPSHOT_SCHEMA))
    if set(data) != _SNAPSHOT_KEYS:
        raise ValueError("gate snapshot keys: missing %s, unexpected %s"
                         % (sorted(_SNAPSHOT_KEYS - set(data)), sorted(set(map(str, data)) - _SNAPSHOT_KEYS)))
    raw_gates = data["gates"]
    if not isinstance(raw_gates, Mapping):
        raise ValueError("gate snapshot gates must be a JSON object")
    missing = sorted(set(GATE_CATALOGUE) - set(raw_gates))
    extra = sorted(set(map(str, raw_gates)) - set(GATE_CATALOGUE))
    if missing or extra:
        raise ValueError("gate snapshot gates: missing %s, not in the catalogue %s" % (missing, extra))
    gates: dict[str, FeatureGate] = {}
    for name in GATE_CATALOGUE:
        try:
            gates[name] = FeatureGate.from_json(name, raw_gates[name])
        except (TypeError, ValueError) as exc:
            raise ValueError("gate snapshot gate %s: %s" % (name, exc)) from exc
        if gates[name].overridable != GATE_CATALOGUE[name].overridable:
            raise ValueError("gate snapshot gate %s: overridable disagrees with the gate catalogue" % name)
    serial = data["serial"]
    if type(serial) is not str or not serial:
        raise ValueError("gate snapshot serial must be a non-empty string")
    firmware = data["firmware"]
    if firmware is not None and type(firmware) is not str:
        raise ValueError("gate snapshot firmware must be a string or null")
    sha = canonical_sha256(data)
    if expect_sha256 is not None:
        if type(expect_sha256) is not str or not _SHA256.match(expect_sha256):
            raise ValueError("expect_sha256 must be 64 lower-case hex digits")
        if sha != expect_sha256:
            raise ValueError("gate snapshot sha256 %s differs from the expected %s" % (sha, expect_sha256))
    return SnapshotGateView(gates=MappingProxyType(gates), serial=serial, firmware=firmware, sha256=sha)
