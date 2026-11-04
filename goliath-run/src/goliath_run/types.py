# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The runner's shared interface types (unit R00; the project design, sections 7.1-7.5 and 8.4).

The runner is a pure core, an imperative shell that owns the robot link, and
views (the command line and the local web interface). The views read immutable
:class:`SessionSnapshot` values and submit :class:`OperatorIntent` values; the
core and the shell exchange phases and restart evidence. These types have one
owner (this module, red-team C-06), so the parts built in parallel cannot
diverge. Types and validation only: no I/O, no clock, no threads.

* :class:`Phase` - the session's state (design 7.3), including the states the
  red-team round added: ``MONITORING_UNVERIFIED`` (FW-03), ``SETUP_LINK_LOST``
  (FW-10) and ``SILENT_AFTER_SEND`` (PHYS-02).
* :data:`PHASE_INFO` - per phase: the top-bar label, the phase banner and
  whether the robot may be moving, each flag with its stated basis.
* :class:`Banner` - one banner line: text plus icon, never colour alone (8.4).
* :class:`SessionSnapshot` - what every view renders. ``stop_offer`` is
  ``'none'`` or ``'verified'`` only: an unverified software stop cannot even be
  represented (red-team PHYS-13).
* :data:`OperatorIntent` - what an operator can ask for, including
  :class:`IStoppedTheRobot` and :class:`JobFinishedNoInfo23` (FW-05) and
  :class:`EnterMeasurement` (C-13). Typed answers are kept verbatim; the core
  compares them with what it expects and records exactly what was typed.
* :class:`SnapshotSource` and :class:`IntentSink` - what the UI server serves.
* :class:`RestartEvidence` - what the passive reconnect after a power cycle
  showed (design 7.7), for the ledger and the restart rules.

"May be moving" errs on the cautious side. A phase whose flag is set forces
``may_be_moving`` in every snapshot of it; any snapshot may report it where the
phase does not require it. ``BLOCKED`` and ``FAULTED`` keep the flag of the
phase they were entered from (:func:`may_be_moving_on_entry`).

JSON: every type has ``to_json()`` (plain JSON values; a phase is its name) and
a strict ``from_json()`` that refuses missing and unexpected keys; intents
carry their class name under ``"type"`` (:func:`intent_from_json`). Floats are
kept exactly (the snapshot is not a certificate, so it is not rounded the way
canonical JSON rounds); NaN and infinities are refused. ``copy``, ``deepcopy``
and ``pickle`` rebuild every value through its constructor, so they cannot
make a value the constructor refuses either.
"""
from __future__ import annotations

import dataclasses
import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePath
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypeAlias, get_args, runtime_checkable

from goliath_link.client import FirmwareCapabilities, parse_firmware_version
from goliath_prep.core.disclosure import Disclosure

__all__ = [
    "BANNER_ICONS",
    "BANNER_SEVERITIES",
    "BASELINE_STATES",
    "CONFIRMATION_INTENTS",
    "CONFIRM_KINDS",
    "INTENT_TYPES",
    "MEASUREMENT_UNITS",
    "PHASE_INFO",
    "STOP_OFFERS",
    "Arm",
    "AttestPowerCycle",
    "Banner",
    "BannerSeverity",
    "BaselineState",
    "Cancel",
    "Confirm",
    "Connect",
    "EnterMeasurement",
    "FrozenMap",
    "IStoppedTheRobot",
    "IntentSink",
    "JobFinishedNoInfo23",
    "OperatorIntent",
    "Override",
    "Phase",
    "PhaseInfo",
    "PickBundle",
    "RestartEvidence",
    "SessionSnapshot",
    "SnapshotSource",
    "StopOffer",
    "intent_from_json",
    "may_be_moving_on_entry",
]

# The id grammar of goliath_prep.core.disclosure.Disclosure, so a banner can carry a disclosure's id.
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/;=+-]*\Z")
# Gate names of the gate catalogue (design 3.2): jobs_allowed, sanity_baseline, ...
_GATE = re.compile(r"[a-z][a-z0-9_]*\Z")
# Measurement ids of field guide 04 and later rungs: footprint.q1_mm, wheel.q1.contact_radius_mm, ...
_FACT_ID = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+\Z")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _text(owner: str, name: str, value: object, *, required: bool) -> str:
    """``value`` as a plain str that UTF-8 can carry; ``required``: it must say something."""
    if type(value) is not str:
        raise TypeError("%s.%s must be a str, got %s" % (owner, name, type(value).__name__))
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("%s.%s contains a lone surrogate, which UTF-8 cannot carry (%s)"
                         % (owner, name, exc)) from None
    if required and not value.strip():
        raise ValueError("%s.%s must not be empty" % (owner, name))
    return value


def _optional_text(owner: str, name: str, value: object) -> str | None:
    return None if value is None else _text(owner, name, value, required=True)


def _flag(owner: str, name: str, value: object) -> bool:
    if type(value) is not bool:
        raise TypeError("%s.%s must be a bool, got %s" % (owner, name, type(value).__name__))
    return value


def _choice(owner: str, name: str, value: object, allowed: tuple[str, ...]) -> str:
    if type(value) is not str:
        raise TypeError("%s.%s must be one of %s (a str), got %s %r"
                        % (owner, name, ", ".join(allowed), type(value).__name__, value))
    if value not in allowed:
        raise ValueError("%s.%s %r is not one of %s" % (owner, name, value, ", ".join(allowed)))
    return value


def _sequence(owner: str, name: str, value: object) -> tuple[Any, ...]:
    if not isinstance(value, (tuple, list)):
        raise TypeError("%s.%s must be a tuple (or a list), got %s" % (owner, name, type(value).__name__))
    return tuple(value)


def _finite(owner: str, name: str, value: object) -> int | float:
    if type(value) not in (int, float):
        raise TypeError("%s.%s must be an int or a float, got %s" % (owner, name, type(value).__name__))
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("%s.%s must be finite, got %r" % (owner, name, value))
    return value  # type: ignore[return-value]


def _rebuild(cls: type, fields: Mapping[str, Any]) -> object:
    """Unpickling and copying: build the value through its constructor, which validates it."""
    if not (isinstance(cls, type) and issubclass(cls, _Validated) and dataclasses.is_dataclass(cls)):
        raise TypeError("%r is not a goliath_run.types value" % (cls,))
    return cls(**fields)


class _Validated:
    """Base of the validated dataclasses: ``copy``, ``deepcopy`` and ``pickle`` go through the constructor.

    Without this, a frozen slotted dataclass is rebuilt by setting its slots
    one by one, skipping ``__post_init__``: a pickle could then carry a third
    ``stop_offer`` value or a phase that reads "not moving".
    """

    __slots__ = ()

    def __reduce__(self) -> tuple[Any, tuple[type, dict[str, Any]]]:
        fields = {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}  # type: ignore[arg-type]
        return (_rebuild, (type(self), fields))


def _keys(what: str, data: object, expected: set[str]) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise TypeError("%s: expected a JSON object, got %s" % (what, type(data).__name__))
    missing = sorted(expected - set(data))
    extra = sorted(str(k) for k in set(data) - expected)
    if missing:
        raise ValueError("%s: missing key(s) %s" % (what, ", ".join(missing)))
    if extra:
        raise ValueError("%s: unexpected key(s) %s" % (what, ", ".join(extra)))
    return data


# ---------------------------------------------------------------------------
# Frozen JSON objects
# ---------------------------------------------------------------------------


def _freeze(value: object, path: str) -> object:
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):  # type: ignore[arg-type]
            raise ValueError("%s: %r is not a JSON number" % (path, value))
        return value
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("%s: the string contains a lone surrogate, which UTF-8 cannot carry (%s)"
                             % (path, exc)) from None
        return value
    if isinstance(value, FrozenMap):
        return value
    if isinstance(value, Mapping):
        return FrozenMap(value, _path=path)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, "%s[%d]" % (path, i)) for i, item in enumerate(value))
    raise TypeError("%s: %s is not a JSON value (use None, bool, int, float, str, a list or a mapping with "
                    "str keys; convert anything else explicitly)" % (path, type(value).__name__))


def _thaw(value: object) -> object:
    if isinstance(value, FrozenMap):
        return value.to_json()
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


class FrozenMap(Mapping[str, Any]):
    """A read-only, hashable JSON object: str keys; nested objects frozen too, arrays as tuples.

    Built from any mapping; the values are checked to be JSON values (finite
    numbers, no Enums, bytes or sets) and copied, so the caller's dict cannot
    change it. :meth:`to_json` gives plain dicts and lists back. Equal to any
    mapping with the same items.
    """

    __slots__ = ("_data", "_hash")

    def __init__(self, data: Mapping[str, Any], *, _path: str = "$") -> None:
        if not isinstance(data, Mapping):
            raise TypeError("%s: a JSON object must be a mapping, got %s" % (_path, type(data).__name__))
        frozen: dict[str, object] = {}
        for key, item in data.items():
            if type(key) is not str:
                raise TypeError("%s: JSON object keys must be strings, got %s key %r"
                                % (_path, type(key).__name__, key))
            _freeze(key, "%s key %r" % (_path, key))
            frozen[key] = _freeze(item, "%s.%s" % (_path, key))
        object.__setattr__(self, "_data", frozen)
        object.__setattr__(self, "_hash", None)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("FrozenMap is read-only")

    def __reduce__(self) -> tuple[type[FrozenMap], tuple[dict[str, object]]]:
        # copy, deepcopy and pickle rebuild it through __init__ (the default would set the slots one by one).
        return (FrozenMap, (dict(self._data),))

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __hash__(self) -> int:
        if self._hash is None:
            object.__setattr__(self, "_hash", hash(frozenset(self._data.items())))
        return self._hash  # type: ignore[return-value]

    def __repr__(self) -> str:
        return "FrozenMap(%r)" % (self._data,)

    def to_json(self) -> dict[str, Any]:
        """The object as plain dicts and lists."""
        return {key: _thaw(item) for key, item in self._data.items()}


# ---------------------------------------------------------------------------
# Banners
# ---------------------------------------------------------------------------

BannerSeverity = Literal["info", "amber", "red", "purple", "blue"]
"""``purple`` is practice mode (design 7.11), ``blue`` an air job's stripe (8.4)."""
BANNER_SEVERITIES: tuple[str, ...] = get_args(BannerSeverity)

BANNER_ICONS: Mapping[str, str] = MappingProxyType({
    "info": "information",
    "caution": "caution: read before going on",
    "stop": "a block or a refusal; also 'no software stop'",
    "motion": "the robot may be moving",
    "link": "the connection to the robot: connecting, lost or unverified",
    "upload": "the robot may be in upload mode",
    "power": "a power cycle of the robot's controller is needed",
    "practice": "practice mode: a simulator, not a robot",
    "air": "an air job: the program is not meant to cut",
    "fault": "an internal error in this program",
})
"""The icon names a banner may use, with what each means. The interface draws one per name (design 8.4, 8.7)."""


@dataclass(frozen=True, slots=True)
class Banner(_Validated):
    """One banner line (design 8.4): an id, a severity, the text and an icon; colour is never the only signal.

    ``id`` uses the disclosure id grammar (no spaces), so a banner raised for
    a disclosure can carry its id. ``icon`` is a name of :data:`BANNER_ICONS`.
    """

    id: str
    severity: BannerSeverity
    text: str
    icon: str

    def __post_init__(self) -> None:
        _text("Banner", "id", self.id, required=True)
        if not _ID.match(self.id):
            raise ValueError("Banner id %r: use letters, digits and . _ : / ; = + - without spaces" % self.id)
        _choice("Banner", "severity", self.severity, BANNER_SEVERITIES)
        _text("Banner", "text", self.text, required=True)
        if type(self.icon) is not str or self.icon not in BANNER_ICONS:
            raise ValueError("Banner icon %r is not one of %s" % (self.icon, ", ".join(BANNER_ICONS)))

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "severity": self.severity, "text": self.text, "icon": self.icon}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Banner:
        d = _keys("Banner", data, {"id", "severity", "text", "icon"})
        return cls(id=d["id"], severity=d["severity"], text=d["text"], icon=d["icon"])


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


class Phase(Enum):
    """The runner's session states (the project design, section 7.3). Each value is the member's name.

    The normal flow is IDLE, NET_CHECK, CONNECTING, IDENTIFY, LEDGER_CHECK,
    PREFLIGHT, READY, the set-up (PAIRING, ENCODER, PANEL_DETECTION, CERTIFY,
    CAGE, COMMIT), ARMED, UPLOADING, RUNNING, COMPLETE, POWER_CYCLE_REQUIRED.
    The others are where the session goes when something is uncertain or
    wrong. An alarm is an overlay on top of a phase, not a phase (design 7.5,
    7.8), and a refused job ends in BLOCKED with its reason.
    """

    IDLE = "IDLE"
    NET_CHECK = "NET_CHECK"
    CONNECTING = "CONNECTING"
    IDENTIFY = "IDENTIFY"
    LEDGER_CHECK = "LEDGER_CHECK"
    PREFLIGHT = "PREFLIGHT"
    READY = "READY"
    PAIRING = "PAIRING"
    ENCODER = "ENCODER"
    PANEL_DETECTION = "PANEL_DETECTION"
    CERTIFY = "CERTIFY"
    CAGE = "CAGE"
    COMMIT = "COMMIT"
    ARMED = "ARMED"
    UPLOADING = "UPLOADING"
    RUNNING = "RUNNING"
    RUNNING_UNCONFIRMED = "RUNNING_UNCONFIRMED"
    LINK_LOST = "LINK_LOST"
    MONITORING_UNVERIFIED = "MONITORING_UNVERIFIED"
    SETUP_LINK_LOST = "SETUP_LINK_LOST"
    COMPLETE = "COMPLETE"
    POWER_CYCLE_REQUIRED = "POWER_CYCLE_REQUIRED"
    UPLOAD_HAZARD = "UPLOAD_HAZARD"
    SILENT_AFTER_SEND = "SILENT_AFTER_SEND"
    MONITOR_ONLY = "MONITOR_ONLY"
    BLOCKED = "BLOCKED"
    FAULTED = "FAULTED"


@dataclass(frozen=True, slots=True)
class PhaseInfo(_Validated):
    """What every view shows for a phase, and whether the robot may be moving in it.

    * ``label`` - the machine state in the top bar (design 8.4), in capitals;
    * ``banner`` - the phase banner, id ``PHASE:<name>``;
    * ``may_be_moving`` - True: the robot may be moving whenever the session is
      in this phase, whatever came before, and every snapshot of the phase says
      so; False: this phase alone gives no reason to think so;
    * ``keeps_may_be_moving`` - True for BLOCKED and FAULTED, which keep the
      flag of the phase they were entered from;
    * ``basis`` - why the flag is what it is.
    """

    phase: Phase
    label: str
    banner: Banner
    may_be_moving: bool
    keeps_may_be_moving: bool
    basis: str

    def __post_init__(self) -> None:
        if not isinstance(self.phase, Phase):
            raise TypeError("PhaseInfo.phase must be a Phase, got %s" % type(self.phase).__name__)
        _text("PhaseInfo", "label", self.label, required=True)
        if not isinstance(self.banner, Banner):
            raise TypeError("PhaseInfo.banner must be a Banner, got %s" % type(self.banner).__name__)
        _flag("PhaseInfo", "may_be_moving", self.may_be_moving)
        _flag("PhaseInfo", "keeps_may_be_moving", self.keeps_may_be_moving)
        if self.may_be_moving and self.keeps_may_be_moving:
            raise ValueError("PhaseInfo %s: a phase that always may be moving has no flag to keep" % self.phase.name)
        _text("PhaseInfo", "basis", self.basis, required=True)


_UPLOAD_MODE = (
    "ROBOT MAY BE IN UPLOAD MODE: power-cycle before anything else (stay connected, then switch the controller off)"
)
_SINCE_PAIRING = (
    "from pairing on the robot may drive itself: K2 tells the operator that after pairing it traces the panel edge "
    "by itself, and a link loss from pairing on is SETUP_LINK_LOST (the project design, sections 7.4 and 7.5; "
    "red-team FW-10)"
)
_PARKED = (
    "entered only after INFO;6 reported panel detection done; the robot then waits for its program (spec 03, steps "
    "D3-G) and the operator confirms where it is parked (K3); a link loss here is still SETUP_LINK_LOST (the project "
    "design, sections 7.3-7.5)"
)
_UNTIL_POWER_CYCLE = "MAY BE MOVING from the upload until the attested power cycle (the project design, section 8.4)"


def _info(phase: Phase, label: str, severity: str, icon: str, text: str, *, moving: bool, basis: str,
          keeps: bool = False) -> PhaseInfo:
    banner = Banner(id="PHASE:" + phase.name, severity=severity, text=text, icon=icon)  # type: ignore[arg-type]
    return PhaseInfo(phase=phase, label=label, banner=banner, may_be_moving=moving, keeps_may_be_moving=keeps,
                     basis=basis)


_P = Phase
_PHASE_TABLE = (
    _info(_P.IDLE, "IDLE", "info", "info", "IDLE: not connected to a robot.", moving=False,
          basis="nothing is connected and this session has started nothing; a ledger that shows an upload in "
                "flight or a hazard opens the session in POWER_CYCLE_REQUIRED or UPLOAD_HAZARD instead (the "
                "project design, sections 3.3 and 7.3)"),
    _info(_P.NET_CHECK, "CHECKING NETWORK", "info", "link", "Checking that this laptop is on the robot's network.",
          moving=False, basis="a local address check before connecting; nothing has been sent (design 7.3)"),
    _info(_P.CONNECTING, "CONNECTING", "info", "link", "Connecting to the robot. Connecting sends nothing.",
          moving=False, basis="the three connections are opened and no byte is sent (design 7.3; spec 03, "
                              "section Connecting)"),
    _info(_P.IDENTIFY, "IDENTIFYING", "info", "info",
          "Identifying the robot: listening for its identity message (INFO;34).", moving=False,
          basis="K1 attests that no other client is connected and, with no ledger on this laptop, that the robot "
                "was switched on fresh; a replay that shows a loaded program, a reset or motion moves the session "
                "to MONITOR_ONLY, POWER_CYCLE_REQUIRED or a refusal (design 7.3, 7.4)"),
    _info(_P.LEDGER_CHECK, "CHECKING LEDGER", "info", "info", "Checking this robot's ledger and what it allows.",
          moving=False, basis="bookkeeping on this laptop; nothing is sent (design 7.3)"),
    _info(_P.PREFLIGHT, "PRE-FLIGHT", "info", "info", "Pre-flight: read-only checks of the robot.", moving=False,
          basis="only the read-only allowlist is sent, before pairing (design 7.2; spec 09, section Read-only "
                "allowlist)"),
    _info(_P.READY, "READY", "info", "info",
          "Ready: choose a prepared job. It is checked again for this robot before anything is sent.", moving=False,
          basis="before pairing nothing has been started on the robot (design 7.5, link-down row)"),
    _info(_P.PAIRING, "SETUP: PAIRING", "amber", "motion",
          "SETUP: pairing with the towers. From now on the robot may drive itself: keep clear of it.", moving=True,
          basis=_SINCE_PAIRING),
    _info(_P.ENCODER, "SETUP: ENCODER", "amber", "motion",
          "SETUP: encoder calibration. The robot may move by itself: keep clear of it.", moving=True,
          basis=_SINCE_PAIRING + "; whether encoder calibration moves the robot is not established (spec 03, "
                                 "step C)"),
    _info(_P.PANEL_DETECTION, "SETUP: PANEL DETECTION", "amber", "motion",
          "SETUP: panel detection. The robot traces the panel edge by itself: keep clear of it.", moving=True,
          basis="spec 03, step D: the robot traces the panel edge by itself; " + _SINCE_PAIRING),
    _info(_P.CERTIFY, "SETUP: CHECKING GEOMETRY", "info", "info",
          "SETUP: checking the measured panel against the job.", moving=False, basis=_PARKED),
    _info(_P.CAGE, "SETUP: CAGE", "info", "info", "SETUP: spindle cage check. The cage must read CLOSED.",
          moving=False, basis=_PARKED + "; the operator fits and tightens the bit here (K4)"),
    _info(_P.COMMIT, "SETUP: COMMIT", "info", "info", "SETUP: sending the certified panel size to the robot.",
          moving=False, basis=_PARKED),
    _info(_P.ARMED, "ARMED", "amber", "caution", "ARMED: uploading starts this job immediately.", moving=False,
          basis="nothing is uploaded yet and the arming countdown sends nothing (design 7.3; K5 in 7.4)"),
    _info(_P.UPLOADING, "UPLOADING", "red", "motion",
          "UPLOADING: the robot starts the job by itself as soon as it has the program.", moving=True,
          basis="spec 03, step H: the robot starts by itself after the upload; " + _UNTIL_POWER_CYCLE),
    _info(_P.RUNNING, "RUNNING", "red", "motion",
          "RUNNING: the robot is working. Keep clear; use the physical stop if anything looks wrong.", moving=True,
          basis=_UNTIL_POWER_CYCLE),
    _info(_P.RUNNING_UNCONFIRMED, "MAY BE RUNNING", "amber", "motion",
          "Upload not confirmed: the robot may be running. Nothing is sent to it.", moving=True,
          basis="the end of the upload was not confirmed, but the robot signalled the end of the upload or work "
                "(design 7.3 and 7.5; red-team FW-04); " + _UNTIL_POWER_CYCLE),
    _info(_P.LINK_LOST, "LINK LOST", "red", "link", "LINK LOST: THE JOB MAY STILL BE RUNNING", moving=True,
          basis="disconnecting does not stop or change any work (spec 03, section Connecting; design 8.4)"),
    _info(_P.MONITORING_UNVERIFIED, "MONITORING UNVERIFIED", "red", "link",
          "MONITORING UNVERIFIED: safety errors may not reach this laptop", moving=True,
          basis="reconnected while a job may run; until a position frame on S3 shows the routing, nothing is "
                "confirmed (design 7.3 and 8.4; red-team FW-03)"),
    _info(_P.SETUP_LINK_LOST, "SETUP LINK LOST", "red", "link",
          "SETUP LINK LOST: the robot may be moving (panel detection); do not approach. Power-cycle it before "
          "reconnecting.", moving=True,
          basis="the robot may trace the panel edge while the link is down (design 7.3 and 8.4; red-team FW-10)"),
    _info(_P.COMPLETE, "COMPLETE", "amber", "power",
          "COMPLETE: the robot reported the job finished (INFO;23). Power-cycle it before the next job.",
          moving=True,
          basis=_UNTIL_POWER_CYCLE + "; what the robot does after INFO;23 is not established (gate "
                                     "post_job_motion_known, design 3.2)"),
    _info(_P.POWER_CYCLE_REQUIRED, "POWER CYCLE REQUIRED", "amber", "power",
          "POWER CYCLE REQUIRED: switch the robot's controller off, wait 10 s, switch it on again; nothing else "
          "until then.", moving=True,
          basis="reached after a job, after 'I stopped the robot', on reboot evidence, and at start-up with an "
                "upload in flight in a ledger (design 7.3, 7.7); " + _UNTIL_POWER_CYCLE),
    _info(_P.UPLOAD_HAZARD, "UPLOAD HAZARD", "red", "upload",
          _UPLOAD_MODE + ". Anything sent now would become part of a program, and the robot may start it.",
          moving=True,
          basis="a cut-short upload leaves the robot in upload mode, and once the announced byte count is reached "
                "it starts a truncated program (spec 03, notes on the job flow; design 7.7 B)"),
    _info(_P.SILENT_AFTER_SEND, "NO REPLY FROM ROBOT", "red", "upload",
          _UPLOAD_MODE + ". The robot did not answer the first command: it may be in upload mode, or another "
                         "client holds its connections.", moving=True,
          basis="no reply to the first command sent after connecting (the one INFO, or the first pre-flight read "
                "when INFO;34 was replayed and no INFO was sent): a robot in upload mode stores every received byte "
                "as program data, and another client may hold the connections or be running a job (design 3.3, "
                "7.3, 7.5, 7.7 B; red-team PHYS-02)"),
    _info(_P.MONITOR_ONLY, "MONITOR ONLY", "amber", "motion",
          "MONITOR ONLY: a program is loaded on the robot or running. This session will not upload.", moving=True,
          basis="the connect-time replay showed a loaded or running program (design 7.3, IDENTIFY and "
                "MONITOR_ONLY)"),
    _info(_P.BLOCKED, "BLOCKED", "amber", "stop",
          "BLOCKED: this session cannot go on. The reason and the remedy are shown with it.", moving=False,
          keeps=True,
          basis="keeps the flag of the phase it was entered from: blocked during panel detection, the robot may "
                "still be tracing the panel (design 7.3)"),
    _info(_P.FAULTED, "FAULTED", "red", "fault",
          "FAULTED: internal error in this program. It did not stop the robot: if a job had started, it may "
          "still be running.", moving=False, keeps=True,
          basis="FAULTED keeps 'may be running' when it happens after the upload header (design 7.3)"),
)
del _P

PHASE_INFO: Mapping[Phase, PhaseInfo] = MappingProxyType({info.phase: info for info in _PHASE_TABLE})
"""Label, banner and "may be moving" for every :class:`Phase` (read-only)."""

if len(PHASE_INFO) != len(_PHASE_TABLE) or set(PHASE_INFO) != set(Phase):
    raise RuntimeError("PHASE_INFO must describe every Phase exactly once")


def may_be_moving_on_entry(phase: Phase, *, previous: bool) -> bool:
    """The least ``may_be_moving`` a snapshot of ``phase`` may report, entered from one that reported ``previous``.

    True when the phase always may be moving; ``previous`` for BLOCKED and
    FAULTED, which keep it; False otherwise. A snapshot may still report True
    where this is False: that is the cautious side.
    """
    if not isinstance(phase, Phase):
        raise TypeError("phase must be a Phase, got %s" % type(phase).__name__)
    _flag("may_be_moving_on_entry", "previous", previous)
    info = PHASE_INFO[phase]
    return info.may_be_moving or (info.keeps_may_be_moving and previous)


# ---------------------------------------------------------------------------
# Session snapshot
# ---------------------------------------------------------------------------

StopOffer = Literal["none", "verified"]
"""``none``: no software-stop control is shown (every firmware until rung R14 verifies one: red-team PHYS-13).
``verified``: the stop panel offers ``ABORT WORK`` because R14 verified it. There is no third value: an
unverified software stop is never offered."""
STOP_OFFERS: tuple[str, ...] = get_args(StopOffer)


@dataclass(frozen=True, slots=True)
class SessionSnapshot(_Validated):
    """Everything a view shows about the session at one moment (the project design, sections 7.1 and 8.4).

    Immutable.

    * ``seq`` - grows with every snapshot of a session; the interface's event
      stream replays from it;
    * ``phase`` and ``may_be_moving`` - the latter is True in every phase whose
      :data:`PHASE_INFO` flag is set (refused otherwise), and may be True in
      any phase;
    * ``banners`` - banners raised by the session (ids unique); the phase
      banner of :data:`PHASE_INFO` and banners derived from the other fields
      are the presenters' to add;
    * ``disclosures`` - every disclosure in effect (design 8.5), in the order
      to show them;
    * ``firmware`` and ``firmware_source`` - the version as the robot reported
      it and where it was read (for example ``INFO;34``); both or neither:
      a version is never shown without its source;
    * ``stop_offer`` - see :data:`StopOffer`; ``verified`` is refused unless
      the firmware is one that has ``ABORT WORK`` (newer than 01.02.00: spec
      03, section Stopping a job). That rung R14 verified it is the producer's
      duty;
    * ``practice`` - practice mode: a simulator, not a robot (design 7.11);
    * ``progress`` - the run's progress as a JSON object (frozen), or None.
    """

    seq: int
    phase: Phase
    may_be_moving: bool
    banners: tuple[Banner, ...]
    disclosures: tuple[Disclosure, ...]
    firmware: str | None
    firmware_source: str | None
    stop_offer: StopOffer
    practice: bool
    progress: Mapping[str, Any] | None

    def __post_init__(self) -> None:
        owner = "SessionSnapshot"
        if type(self.seq) is not int:
            raise TypeError("%s.seq must be an int, got %s" % (owner, type(self.seq).__name__))
        if self.seq < 0:
            raise ValueError("%s.seq must not be negative, got %d" % (owner, self.seq))
        if not isinstance(self.phase, Phase):
            raise TypeError("%s.phase must be a Phase, got %s" % (owner, type(self.phase).__name__))
        _flag(owner, "may_be_moving", self.may_be_moving)
        if PHASE_INFO[self.phase].may_be_moving and not self.may_be_moving:
            raise ValueError("%s: the robot may be moving in phase %s; a snapshot of it cannot say otherwise"
                             % (owner, self.phase.name))
        banners = _sequence(owner, "banners", self.banners)
        seen: set[str] = set()
        for b in banners:
            if not isinstance(b, Banner):
                raise TypeError("%s.banners must hold Banner values, got %s" % (owner, type(b).__name__))
            if b.id in seen:
                raise ValueError("%s.banners: banner id %r appears twice" % (owner, b.id))
            seen.add(b.id)
        object.__setattr__(self, "banners", banners)
        disclosures = _sequence(owner, "disclosures", self.disclosures)
        for d in disclosures:
            if not isinstance(d, Disclosure):
                raise TypeError("%s.disclosures must hold Disclosure values, got %s" % (owner, type(d).__name__))
        object.__setattr__(self, "disclosures", disclosures)
        _optional_text(owner, "firmware", self.firmware)
        _optional_text(owner, "firmware_source", self.firmware_source)
        if (self.firmware is None) != (self.firmware_source is None):
            raise ValueError("%s: firmware %r and its source %r go together: a version is never shown without "
                             "where it was read, and a source without a version names nothing"
                             % (owner, self.firmware, self.firmware_source))
        _choice(owner, "stop_offer", self.stop_offer, STOP_OFFERS)
        if self.stop_offer == "verified":
            version = None if self.firmware is None else parse_firmware_version(self.firmware)
            if not FirmwareCapabilities.for_version(version).has_abort_work:
                raise ValueError("%s.stop_offer 'verified' needs firmware that has ABORT WORK (newer than "
                                 "01.02.00); the firmware is %s"
                                 % (owner, "unknown" if self.firmware is None else repr(self.firmware)))
        _flag(owner, "practice", self.practice)
        if self.progress is not None:
            if not isinstance(self.progress, Mapping):
                raise TypeError("%s.progress must be a JSON object (a mapping) or None, got %s"
                                % (owner, type(self.progress).__name__))
            object.__setattr__(self, "progress", FrozenMap(self.progress, _path="progress"))

    def to_json(self) -> dict[str, Any]:
        """Plain JSON values; the phase as its name."""
        return {
            "seq": self.seq,
            "phase": self.phase.value,
            "may_be_moving": self.may_be_moving,
            "banners": [b.to_json() for b in self.banners],
            "disclosures": [d.to_json() for d in self.disclosures],
            "firmware": self.firmware,
            "firmware_source": self.firmware_source,
            "stop_offer": self.stop_offer,
            "practice": self.practice,
            "progress": None if self.progress is None else _thaw(self.progress),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SessionSnapshot:
        """The snapshot :meth:`to_json` wrote; missing or unexpected keys are refused."""
        d = _keys("SessionSnapshot", data, {f.name for f in dataclasses.fields(cls)})
        phase = d["phase"]
        if type(phase) is not str or phase not in Phase.__members__:
            raise ValueError("SessionSnapshot.phase %r is not the name of a Phase" % (phase,))
        return cls(
            seq=d["seq"],
            phase=Phase[phase],
            may_be_moving=d["may_be_moving"],
            banners=tuple(Banner.from_json(b) for b in _sequence("SessionSnapshot", "banners", d["banners"])),
            disclosures=tuple(Disclosure.from_json(x)
                              for x in _sequence("SessionSnapshot", "disclosures", d["disclosures"])),
            firmware=d["firmware"],
            firmware_source=d["firmware_source"],
            stop_offer=d["stop_offer"],
            practice=d["practice"],
            progress=d["progress"],
        )


# ---------------------------------------------------------------------------
# Operator intents
# ---------------------------------------------------------------------------

CONFIRM_KINDS: tuple[str, ...] = ("K1", "K2", "K3", "K4", "K7", "K8")
"""The confirmations of design 7.4 that :class:`Confirm` carries. K5, K6 and K9 have intents of their own."""

MEASUREMENT_UNITS: Mapping[str, str] = MappingProxyType({
    "mm": "millimetres",
    "deg": "degrees",
    "s": "seconds",
    "count": "a whole number",
})
"""The units of a typed measurement (field guide 04's units)."""


class _Intent(_Validated):
    __slots__ = ()

    def to_json(self) -> dict[str, Any]:
        """``{"type": <class name>, <field>: <value>, ...}``; read back with :func:`intent_from_json`."""
        data: dict[str, Any] = {"type": type(self).__name__}
        for field in dataclasses.fields(self):  # type: ignore[arg-type]
            value = getattr(self, field.name)
            data[field.name] = str(value) if isinstance(value, PurePath) else value
        return data


@dataclass(frozen=True, slots=True)
class Connect(_Intent):
    """Connect to the robot (after K1)."""


@dataclass(frozen=True, slots=True)
class Confirm(_Intent):
    """A confirmation of design 7.4: ``kind`` (one of :data:`CONFIRM_KINDS`), the text ``shown`` and the ``answer``.

    ``shown`` is the text the operator saw, verbatim; ``answer`` is what they
    ticked or typed, verbatim (the core checks it, for example K3's
    ``GEOMETRY OK``, and records both with a timestamp). For K7 the answer is
    the operator's typed reason, which must say something, as an
    :class:`Override`'s reason must.
    """

    kind: str
    shown: str
    answer: str

    def __post_init__(self) -> None:
        if type(self.kind) is not str:
            raise TypeError("Confirm.kind must be a str, got %s" % type(self.kind).__name__)
        if self.kind not in CONFIRM_KINDS:
            raise ValueError("Confirm kind %r is not one of %s (K5 is Arm, K6 AttestPowerCycle, K9 EnterMeasurement)"
                             % (self.kind, ", ".join(CONFIRM_KINDS)))
        _text("Confirm", "shown", self.shown, required=True)
        _text("Confirm", "answer", self.answer, required=False)
        if self.kind == "K7" and not self.answer.strip():
            raise ValueError("Confirm K7: the answer is the operator's typed reason (design 7.4); a blank answer "
                             "gives no reason")


@dataclass(frozen=True, slots=True)
class PickBundle(_Intent):
    """Choose a prepared job bundle. The runner re-certifies it from its inputs (design 4.15.1): a path is no proof."""

    path: Path

    def __post_init__(self) -> None:
        if isinstance(self.path, PurePath):
            text = str(self.path)
        else:
            text = _text("PickBundle", "path", self.path, required=True)
        if not text.strip() or "\x00" in text:
            raise ValueError("PickBundle.path %r is not a path" % (text,))
        object.__setattr__(self, "path", Path(text))


@dataclass(frozen=True, slots=True)
class Arm(_Intent):
    """K5: the typed arming phrase (``START <first 4 hex of the payload SHA-256>``), verbatim; checked server-side."""

    phrase: str

    def __post_init__(self) -> None:
        _text("Arm", "phrase", self.phrase, required=False)


@dataclass(frozen=True, slots=True)
class Cancel(_Intent):
    """Cancel what is pending (a prompt, the arming countdown). It never stops a robot."""


@dataclass(frozen=True, slots=True)
class AttestPowerCycle(_Intent):
    """K6: the typed attestation (``POWERCYCLED``), verbatim. Clearing a hazard needs evidence too (design 7.7)."""

    text: str

    def __post_init__(self) -> None:
        _text("AttestPowerCycle", "text", self.text, required=False)


@dataclass(frozen=True, slots=True)
class IStoppedTheRobot(_Intent):
    """"I stopped / power-cycled the robot": outcome STOPPED_PHYSICALLY, then POWER_CYCLE_REQUIRED (red-team FW-05).

    Available whenever the robot may be moving. ``text`` is the operator's
    words, verbatim.
    """

    text: str

    def __post_init__(self) -> None:
        _text("IStoppedTheRobot", "text", self.text, required=False)


@dataclass(frozen=True, slots=True)
class JobFinishedNoInfo23(_Intent):
    """"The job visibly finished; no INFO;23": COMPLETED_END_UNCONFIRMED, then POWER_CYCLE_REQUIRED (red-team FW-05).

    ``text`` is the operator's words, verbatim.
    """

    text: str

    def __post_init__(self) -> None:
        _text("JobFinishedNoInfo23", "text", self.text, required=False)


@dataclass(frozen=True, slots=True)
class Override(_Intent):
    """K7: a typed override of an overridable gate (design 3.1, 3.2), with the operator's reason (required)."""

    gate: str
    reason: str

    def __post_init__(self) -> None:
        _text("Override", "gate", self.gate, required=False)
        if not _GATE.match(self.gate):
            raise ValueError("Override gate %r is not a gate name (lower case, digits and _)" % self.gate)
        _text("Override", "reason", self.reason, required=True)


@dataclass(frozen=True, slots=True)
class EnterMeasurement(_Intent):
    """K9: one hand-measured value, recorded as a ROBOT_MEASURED fact (design 3.1, 7.4; red-team C-13).

    ``fact_id`` is the measurement's stable id (field guide 04 for rung R0b).
    A number needs its ``unit`` (one of :data:`MEASUREMENT_UNITS`; a ``count``
    is a whole number, never negative) and its ``tolerance`` (in the same unit, >= 0): neither
    is ever defaulted. An answer that is not a number (a choice, text, or the
    word ``unknown``) has neither. ``operator`` is the name of the person who
    measured. The time is recorded when the intent is received.
    """

    fact_id: str
    value: float | int | str
    unit: str | None
    tolerance: float | int | None
    operator: str

    def __post_init__(self) -> None:
        owner = "EnterMeasurement"
        _text(owner, "fact_id", self.fact_id, required=False)
        if not _FACT_ID.match(self.fact_id):
            raise ValueError("%s fact id %r: use dotted lower-case ids such as footprint.q1_mm" % (owner, self.fact_id))
        number = type(self.value) is not str
        if number:
            _finite(owner, "value", self.value)
        elif not _text(owner, "value", self.value, required=False).strip():
            raise ValueError("%s %s: an empty answer is not an answer; enter the value or 'unknown'"
                             % (owner, self.fact_id))
        if self.unit is not None and (type(self.unit) is not str or self.unit not in MEASUREMENT_UNITS):
            raise ValueError("%s %s: unit %r is not one of %s"
                             % (owner, self.fact_id, self.unit, ", ".join(MEASUREMENT_UNITS)))
        if self.tolerance is not None and _finite(owner, "tolerance", self.tolerance) < 0:
            raise ValueError("%s %s: tolerance %r is negative" % (owner, self.fact_id, self.tolerance))
        if number:
            if self.unit is None:
                raise ValueError("%s %s: a number needs its unit (one of %s)"
                                 % (owner, self.fact_id, ", ".join(MEASUREMENT_UNITS)))
            if self.tolerance is None:
                raise ValueError("%s %s: a number needs its tolerance (in %s); it is never defaulted"
                                 % (owner, self.fact_id, self.unit))
            if self.unit == "count" and (type(self.value) is not int or self.value < 0):
                raise ValueError("%s %s: a count is a whole number (0, 1, 2, ...), got %r"
                                 % (owner, self.fact_id, self.value))
        else:
            if self.unit is not None:
                raise ValueError("%s %s: an answer that is not a number has no unit, got unit %r"
                                 % (owner, self.fact_id, self.unit))
            if self.tolerance is not None:
                raise ValueError("%s %s: an answer that is not a number has no tolerance, got %r"
                                 % (owner, self.fact_id, self.tolerance))
        _text(owner, "operator", self.operator, required=True)


OperatorIntent: TypeAlias = (
    Connect | Confirm | PickBundle | Arm | Cancel | AttestPowerCycle | IStoppedTheRobot | JobFinishedNoInfo23
    | Override | EnterMeasurement
)
"""What an operator can ask the runner for, from the command line or the interface. No software stop (PHYS-13)."""

INTENT_TYPES: tuple[type, ...] = get_args(OperatorIntent)
_INTENTS_BY_NAME: Mapping[str, type] = MappingProxyType({cls.__name__: cls for cls in INTENT_TYPES})

CONFIRMATION_INTENTS: Mapping[str, tuple[type, ...]] = MappingProxyType({
    "K1": (Confirm,),
    "K2": (Confirm,),
    "K3": (Confirm,),
    "K4": (Confirm,),
    "K5": (Arm,),
    "K6": (AttestPowerCycle,),
    "K7": (Confirm, Override),
    "K8": (Confirm,),
    "K9": (EnterMeasurement,),
})
"""Which intent carries each confirmation of design 7.4. K5, K6 and K9 have one carrier each, so the arming
phrase, the power-cycle attestation and a measurement cannot come in through :class:`Confirm`. K7 has two:
:class:`Override` for an overridable gate and :class:`Confirm` for an acknowledged fallback; both need a typed
reason, and the core must not take a ``Confirm`` as a gate override."""

if tuple(k for k, carriers in CONFIRMATION_INTENTS.items() if Confirm in carriers) != CONFIRM_KINDS:
    raise RuntimeError("CONFIRM_KINDS and CONFIRMATION_INTENTS disagree")


def intent_from_json(data: Mapping[str, Any]) -> OperatorIntent:
    """The intent an ``intent.to_json()`` object describes; an unknown type, missing or unexpected keys are refused."""
    if not isinstance(data, Mapping):
        raise TypeError("an intent must be a JSON object, got %s" % type(data).__name__)
    name = data.get("type")
    cls = _INTENTS_BY_NAME.get(name) if type(name) is str else None
    if cls is None:
        raise ValueError("unknown intent type %r; expected one of %s" % (name, ", ".join(_INTENTS_BY_NAME)))
    names = [f.name for f in dataclasses.fields(cls)]
    d = _keys("intent %s" % name, data, {"type", *names})
    return cls(**{n: d[n] for n in names})  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# What the UI server serves
# ---------------------------------------------------------------------------


@runtime_checkable
class SnapshotSource(Protocol):
    """Where views read the session from (the driver, or a test double).

    ``snapshot()`` returns the current snapshot. ``events(after_seq)`` yields
    the snapshots with ``seq > after_seq`` in increasing ``seq`` order and then
    each new one as it is made, until the session ends. A source that no longer
    holds the snapshot right after ``after_seq`` starts with the oldest it
    holds: the gap shows in ``seq``, and since every snapshot is complete, the
    latest one is all a view needs.
    """

    def snapshot(self) -> SessionSnapshot: ...

    def events(self, after_seq: int) -> Iterator[SessionSnapshot]: ...


@runtime_checkable
class IntentSink(Protocol):
    """Where views submit operator intents. ``submit`` queues the intent for the session and returns.

    It never talks to the robot in the caller's thread. An intent the session
    refuses is reported in a later snapshot; it is never dropped silently.
    """

    def submit(self, intent: OperatorIntent) -> None: ...


# ---------------------------------------------------------------------------
# Restart evidence
# ---------------------------------------------------------------------------

BaselineState = Literal["fresh", "after-complete", "after-interrupt"]
"""The restart baseline the evidence is compared with (design 7.7 A): the robot's state before the power cycle,
a fresh boot (rung R1), after a completed job (R3) or after an interrupted one (the aftermath of R3b)."""
BASELINE_STATES: tuple[str, ...] = get_args(BaselineState)


@dataclass(frozen=True, slots=True)
class RestartEvidence(_Validated):
    """What the passive reconnect after a power cycle showed (design 7.7; red-team FW-02, FW-06, PHYS-02).

    * ``replay_codes`` - the ``INFO`` codes of the connect-time replay, in the
      order they arrived; empty when none arrived (a missing replay never
      blocks: spec 03, section Connecting, step 3);
    * ``link_drop_observed`` - the runner, connected passively, saw the link go
      down while the operator switched the controller off (7.7 B, route 1);
    * ``baseline_state`` - which baseline the codes are compared with.

    Gathered without sending anything. The rules that read it are the core's.
    """

    replay_codes: tuple[int, ...]
    link_drop_observed: bool
    baseline_state: BaselineState

    def __post_init__(self) -> None:
        owner = "RestartEvidence"
        codes = _sequence(owner, "replay_codes", self.replay_codes)
        for code in codes:
            if type(code) is not int:
                raise TypeError("%s.replay_codes must hold ints, got %s %r" % (owner, type(code).__name__, code))
            if code < 0:
                raise ValueError("%s.replay_codes: an INFO code is not negative, got %d" % (owner, code))
        object.__setattr__(self, "replay_codes", codes)
        _flag(owner, "link_drop_observed", self.link_drop_observed)
        _choice(owner, "baseline_state", self.baseline_state, BASELINE_STATES)

    def to_json(self) -> dict[str, Any]:
        return {"replay_codes": list(self.replay_codes), "link_drop_observed": self.link_drop_observed,
                "baseline_state": self.baseline_state}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RestartEvidence:
        d = _keys("RestartEvidence", data, {"replay_codes", "link_drop_observed", "baseline_state"})
        return cls(replay_codes=_sequence("RestartEvidence", "replay_codes", d["replay_codes"]),
                   link_drop_observed=d["link_drop_observed"], baseline_state=d["baseline_state"])

