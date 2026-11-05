# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""What the local interface shows, as plain data (unit U02; the project design, sections 8.3-8.5).

The page renders what these functions return and nothing else: it never reads
the session snapshot itself, so the rules below are tested here, in Python,
and hold for every screen. Every function is pure: its result depends only on
its arguments (no clock, no files, no network), and is plain JSON data (dicts,
lists, strings, numbers, booleans and None) or a frozen value with
``to_json()``.

**The persistent parts** (on every screen; :func:`session_view`, sent to the
page after every snapshot):

* the **top bar** (:func:`top_bar`): left the firmware and where it was read
  (``Robot: GFW 01.02.00 (from INFO;34)``) or ``FIRMWARE UNKNOWN``; centre the
  fixed red chip ``NO SOFTWARE STOP: PHYSICAL STOP ONLY`` on every firmware
  until rung R14 has verified a software stop (red-team PHYS-13); right the
  machine state, with a red ``MAY BE MOVING`` whenever the robot may be moving.
  Each part also says what to show when the view stops updating: no software
  stop, and "the robot may be moving" (an unknown state is never read as a
  safe one);
* the **banner model** (:func:`banner_model`, :func:`banners`): the phase
  banner, the banners derived from the snapshot (practice, no software stop,
  firmware unknown), the session's own banners, and a banner for every
  disclosure in effect (invariant I6 of design 10.5: a fallback cannot exist
  without being shown). Grouped where the design names one banner for many
  disclosures (UNMEASURED MACHINE PROFILE, RELAXED SAFETY, RESTART VERIFIED BY
  ATTESTATION ONLY; info notes under NOTICES); every other red or amber
  disclosure has a banner of its own. A banner is never less severe than a
  disclosure it covers. Every banner has text and an icon (design 8.7: colour
  is never the only signal);
* the **disclosure chip** (:func:`disclosure_chip`): "3 fallbacks / 7
  unverified defaults in effect", listing each with its consequence; it never
  dismisses itself.

**Values carry provenance** (design 8.4): :func:`param_tag` and
:func:`fact_tag` give the tag a value is shown with (``[spec]``,
``[robot 2026-11-05]``, ``[owner]``, ``[default, unverified]``,
``[INFERRED]``, ...); :class:`Shown` is a value with its tag and where it came
from. An unknown value is shown as unknown (:func:`shown_unknown`), never as a
default.

**The screens of this unit** (design 8.3): :func:`machine_view`,
:func:`ladder_view`, :func:`records_view` and :func:`settings_view`. A screen
is ``{"screen", "title", "sections"}``; a section is a ``notice``, ``fields``
(label and :class:`Shown` value), ``table``, ``list`` or ``group`` (of
sections). Data a screen was not given (no gate set before the robot is
identified, no ledger, no record folder) is shown as not available, with what
that means; nothing is filled in.

**The session's banners.** The phase banner and the derived banners are the
presenters' to add (goliath_run.types); a session banner that uses one of
their ids would put a second, possibly contradicting, state on the screen, so
it is refused with :class:`PresenterError`, never shown. The one session
banner with fixed content is :data:`AIR_BANNER`, which the session adds while
an air package is in use: any other text under its id is refused (red-team
PHYS-09 reworded it, and the old wording understated the hazard).
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from goliath_link.client import FirmwareCapabilities, format_version, parse_firmware_version
from goliath_link.transport import DEFAULT_AP_ADDRESS
from goliath_prep.core.disclosure import SEVERITY_RANK, Disclosure
from goliath_prep.core.params import SOURCES, Param
from goliath_prep.defaults import SPEC_PARAMS
from goliath_prep.machine import MEASURED_FIELDS, STOP_FIELDS, MachineProfile

from goliath_run.capabilities import GATE_CATALOGUE, FeatureGate, GateSet
from goliath_run.facts import FACT_BASES, STATIC_FAMILY, Fact, FactStore, thaw
from goliath_run.ledger import LedgerState
from goliath_run.record import OUTCOMES
from goliath_run.types import (
    BANNER_ICONS,
    BANNER_SEVERITIES,
    PHASE_INFO,
    Banner,
    SessionSnapshot,
    SnapshotSource,
)

__all__ = [
    "AIR_BANNER",
    "AIR_TEXT",
    "APP_NAME",
    "FIRMWARE_UNKNOWN_TEXT",
    "LADDER",
    "MAY_BE_MOVING",
    "NO_SOFTWARE_STOP_CHIP",
    "NO_SOFTWARE_STOP_HEADING",
    "PRACTICE_TEXT",
    "SCREENS",
    "SEVERITY_LABELS",
    "SOURCE_TAGS",
    "STALE_STATE_TEXT",
    "BannerModel",
    "ChipItem",
    "ChipModel",
    "LinkStatus",
    "PresenterError",
    "RecordSummary",
    "Rung",
    "Shown",
    "banner_model",
    "banners",
    "default_screens",
    "disclosure_chip",
    "fact_tag",
    "ladder_view",
    "machine_view",
    "param_tag",
    "physical_stop_text",
    "records_view",
    "session_view",
    "settings_view",
    "shown_fact",
    "shown_param",
    "shown_unknown",
    "top_bar",
]


class PresenterError(ValueError):
    """Input the presenters refuse to show, because showing it would misstate the robot's state.

    For example a session banner that claims a phase (``PHASE:RUNNING``) or
    another presenter-owned id, or the AIR banner with other words. The page
    then reports that it stopped updating (a stale screen is worse than none).
    """


# ---------------------------------------------------------------------------
# Fixed texts (the project design, section 8.4)
# ---------------------------------------------------------------------------

APP_NAME = "goliath-run"
NO_SOFTWARE_STOP_CHIP = "NO SOFTWARE STOP: PHYSICAL STOP ONLY"
"""The fixed red chip in the centre of the top bar, on every firmware until rung R14 (red-team PHYS-13)."""
NO_SOFTWARE_STOP_HEADING = "NO SOFTWARE STOP ON THIS FIRMWARE"
FIRMWARE_UNKNOWN_TEXT = "FIRMWARE UNKNOWN: read-only"
PRACTICE_TEXT = "PRACTICE: SIMULATOR, NOT A ROBOT"
MAY_BE_MOVING = "MAY BE MOVING"
STALE_STATE_TEXT = "UNKNOWN: this view is not updating, so the robot MAY BE MOVING"
"""The machine state to show when the view stops updating: an unknown state is read as the cautious one."""
AIR_TEXT = ("AIR: the program keeps Z >= +5 mm and has no M10; the robot's own Z calibration may still lower and "
            "spin the bit")
"""The AIR banner, worded by red-team PHYS-09: the robot's calibration after the upload is not our program."""
AIR_BANNER = Banner(id="AIR", severity="blue", text=AIR_TEXT, icon="air")
"""The banner the session adds while an air package (or a rung's air job) is in use; its blue stripe (design 8.4)."""
SOFTWARE_STOP_VERIFIED = ("SOFTWARE STOP VERIFIED AT RUNG R14 (ABORT WORK on the stop panel); the physical stop "
                          "works too")

SCREENS: tuple[str, ...] = ("machine", "ladder", "records", "settings")
"""The screens of this unit, as served at ``/api/screen/<name>`` (design 8.3: screens 1, 2, 8 and 9)."""

SEVERITY_LABELS: Mapping[str, str] = MappingProxyType({
    "info": "information",
    "amber": "warning",
    "red": "danger",
    "purple": "practice",
    "blue": "air job",
})
"""The word for each banner severity, written next to the colour (design 8.7)."""

if set(SEVERITY_LABELS) != set(BANNER_SEVERITIES):  # pragma: no cover - a guard on this module's tables
    raise RuntimeError("SEVERITY_LABELS must name every banner severity")

_BANNER_ORDER: Mapping[str, int] = MappingProxyType({"purple": 0, "red": 1, "amber": 2, "blue": 3, "info": 4})


def _louder(a: str, b: str) -> str:
    """The more severe of two disclosure severities (info, amber, red)."""
    return a if SEVERITY_RANK[a] >= SEVERITY_RANK[b] else b


# ---------------------------------------------------------------------------
# Values with their provenance
# ---------------------------------------------------------------------------

SOURCE_TAGS: Mapping[str, str] = MappingProxyType({
    "SPEC": "[spec]",
    "ROBOT_MEASURED": "[robot, measured]",
    "OWNER_PROFILE": "[owner]",
    "OWNER_JOB": "[owner, job]",
    "FACT": "[robot]",
    "DEFAULT_UNMEASURED": "[default, unverified]",
    "DERIVED": "[derived]",
    "INFERRED": "[INFERRED]",
    "POLICY": "[policy]",
    "GATE": "[gate default]",
})
"""The provenance tag of a :class:`~goliath_prep.core.params.Param` by its source (design 8.4). A DERIVED value
computed from any value that carries a disclosure is tagged ``[derived, unverified]`` (:func:`param_tag`)."""

if set(SOURCE_TAGS) != set(SOURCES):  # pragma: no cover - a guard on this module's tables
    raise RuntimeError("SOURCE_TAGS must name every Param source exactly once")

_FACT_TAGS: Mapping[str, str] = MappingProxyType({
    "observed": "robot {date}",
    "robot-measured": "robot {date}, measured",
    "owner": "owner",
    "spec-constant": "spec",
    "firmware-static": "static analysis of " + STATIC_FAMILY,
    "vendor-gate": "vendor gate, unverified",
    "default": "default, unverified",
})

if set(_FACT_TAGS) != set(FACT_BASES):  # pragma: no cover - a guard on this module's tables
    raise RuntimeError("the fact tags must name every fact basis exactly once")

_UNKNOWN_TAG = "[unknown]"


@dataclass(frozen=True)
class Shown:
    """A value as the page shows it: its ``text``, its provenance ``tag``, where it came from (``ref``).

    ``known`` is False for an unknown value, whose text says so; the page
    never shows an unknown as a number.
    """

    text: str
    tag: str
    ref: str
    known: bool

    def __post_init__(self) -> None:
        for name in ("text", "tag", "ref"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():
                raise ValueError("Shown.%s must be a non-empty str, got %r" % (name, value))
        if not (self.tag.startswith("[") and self.tag.endswith("]")):
            raise ValueError("Shown.tag %r is not a provenance tag in brackets" % self.tag)
        if type(self.known) is not bool:
            raise TypeError("Shown.known must be a bool")
        if not self.known and self.tag != _UNKNOWN_TAG:
            raise ValueError("an unknown value is tagged %s, not %s" % (_UNKNOWN_TAG, self.tag))

    def to_json(self) -> dict[str, Any]:
        return {"text": self.text, "tag": self.tag, "ref": self.ref, "known": self.known}

    def label(self) -> str:
        return "%s %s" % (self.text, self.tag)


def _number(value: int | float) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise PresenterError("a value to show is not a finite number: %r" % value)
    if value == 0:
        return "0"
    text = "%.6f" % value
    return text.rstrip("0").rstrip(".")


def _value_text(value: object) -> str:
    """A fact's or a reading's value as text: a string as it is, numbers plainly, structures as compact JSON."""
    if isinstance(value, str):
        return value
    if isinstance(value, (bool, int, float)):
        return _number(value)
    return json.dumps(thaw(value), sort_keys=True, ensure_ascii=False, separators=(", ", ": "))


_UNIT_WORDS: Mapping[str, str] = MappingProxyType({"byte": "bytes", "percent": "%"})


def _param_text(param: Param[Any]) -> str:
    if param.unit == "enum":
        return str(param.value)
    unit = _UNIT_WORDS.get(param.unit, param.unit)
    if isinstance(param.value, tuple):
        return "(%s) %s" % (", ".join(_number(v) for v in param.value), unit)
    return "%s %s" % (_number(param.value), unit)


def param_tag(param: Param[Any]) -> str:
    """The provenance tag of a Param (:data:`SOURCE_TAGS`); a derived value of doubtful inputs says so."""
    if not isinstance(param, Param):
        raise TypeError("param_tag takes a Param, got %s" % type(param).__name__)
    if param.source == "DERIVED" and param.all_disclosures():
        return "[derived, unverified]"
    return SOURCE_TAGS[param.source]


def fact_tag(fact: Fact, *, stale: bool = False, unverified: bool = False) -> str:
    """The provenance tag of a fact: ``[robot <date>]`` when observed, ``[robot <date>, measured]`` when taped, ...

    A stale fact (shown, never used) and a fact "unverified for this version" say so in their tag.
    """
    if not isinstance(fact, Fact):
        raise TypeError("fact_tag takes a Fact, got %s" % type(fact).__name__)
    base = _FACT_TAGS[fact.basis].format(date=fact.recorded_at[:10])
    if stale:
        base += ", STALE"
    if unverified:
        base += ", unverified for this firmware"
    return "[%s]" % base


def shown_param(param: Param[Any]) -> Shown:
    """A Param as shown: value and unit, its tag, and its reference."""
    return Shown(text=_param_text(param), tag=param_tag(param), ref=param.ref, known=True)


def shown_fact(fact: Fact, *, stale: str | None = None, unverified: str | None = None) -> Shown:
    """A fact as shown; ``stale`` and ``unverified`` are the store's reasons when it is one of those."""
    ref = "fact %s, rung %s" % (fact.id, fact.rung or "none")
    if fact.unknown_id:
        ref += ", %s" % fact.unknown_id
    if stale:
        ref += "; STALE: %s" % stale
    if unverified:
        ref += "; %s" % unverified
    return Shown(text=_value_text(fact.value), tag=fact_tag(fact, stale=bool(stale), unverified=bool(unverified)),
                 ref=ref, known=True)


def shown_unknown(ref: str, text: str = "unknown") -> Shown:
    """An unknown value: its text says so (``unknown`` unless given), ``ref`` says what would settle it."""
    return Shown(text=text, tag=_UNKNOWN_TAG, ref=ref, known=False)


def _as_shown(value: object, what: str) -> Shown | None:
    if value is None:
        return None
    if isinstance(value, Shown):
        return value if value.known else None
    if isinstance(value, Param):
        return shown_param(value)
    if isinstance(value, Fact):
        return shown_fact(value)
    raise TypeError("%s must be a Param, a Fact, a Shown or None, got %s" % (what, type(value).__name__))


# ---------------------------------------------------------------------------
# The physical stop
# ---------------------------------------------------------------------------

StopDescription = Mapping[str, Any]
"""The physical stop's description: field guide 04's ``stop.*`` ids to a Param, a Fact, a Shown or None
(for example :attr:`goliath_prep.machine.MachineProfile.stop`, or the store's ``stop.*`` facts)."""

_STOP_KINDS: Mapping[str, str] = MappingProxyType({
    "estop_button_on_robot": "the emergency-stop button on the robot",
    "power_switch_on_robot": "the power switch on the robot",
    "mains_plug": "pulling the robot's mains plug",
    "switched_socket_or_strip": "the switch of the robot's socket or power strip",
    "separate_mains_estop": "the separate mains emergency stop",
    "other": "the physical stop recorded as 'other'",
})
_STOP_UNRECORDED = ("use the robot's physical stop; what and where it is has not been recorded yet (field guide 04 "
                    "records it at rung R0b)")


def physical_stop_text(stop: StopDescription | None) -> str:
    """How to stop the robot, from its recorded description, with provenance; said plainly when it is unknown."""
    if stop is not None and not isinstance(stop, Mapping):
        raise TypeError("the physical stop description must be a mapping of stop.* fields, got %s"
                        % type(stop).__name__)
    fields = {} if stop is None else {name: _as_shown(stop.get(name), name) for name in STOP_FIELDS}
    kind = fields.get("stop.kind")
    location = fields.get("stop.location")
    if kind is not None and kind.text == "unknown":
        kind = None
    if location is not None and location.text.strip().lower() == "unknown":
        location = None
    where = "" if location is None else " (%s) %s" % (location.text, location.tag)
    if kind is None:
        if location is None:
            return _STOP_UNRECORDED
        return "use the physical stop at: %s %s; what kind of stop it is has not been recorded" % (
            location.text, location.tag)
    if kind.text == "none_found":
        return ("NO PHYSICAL STOP WAS FOUND on this robot %s: find one, or a way to cut its power, before it moves"
                % kind.tag)
    words = _STOP_KINDS.get(kind.text)
    if words is None:
        return "use the physical stop recorded as %r %s%s" % (kind.text, kind.tag, where)
    if location is not None and location.tag == kind.tag:
        return "use %s (%s) %s" % (words, location.text, kind.tag)
    return "use %s %s%s" % (words, kind.tag, where)


# ---------------------------------------------------------------------------
# Firmware
# ---------------------------------------------------------------------------


def _version(snapshot: SessionSnapshot) -> str | None:
    """The firmware version the snapshot names (``01.02.00``), or None when unknown or without a version number."""
    if snapshot.firmware is None:
        return None
    parsed = parse_firmware_version(snapshot.firmware)
    return None if parsed is None else format_version(parsed)


def _firmware_part(snapshot: SessionSnapshot) -> dict[str, Any]:
    version = _version(snapshot)
    if snapshot.firmware is None:
        return {"text": "FIRMWARE UNKNOWN", "known": False, "version": None, "source": None, "severity": "amber"}
    if version is None:
        return {"text": "FIRMWARE UNKNOWN (the robot reported %r, from %s)" % (snapshot.firmware,
                                                                              snapshot.firmware_source),
                "known": False, "version": None, "source": snapshot.firmware_source, "severity": "amber"}
    return {"text": "Robot: %s (from %s)" % (snapshot.firmware, snapshot.firmware_source), "known": True,
            "version": version, "source": snapshot.firmware_source, "severity": "info"}


def _firmware_unknown_banner(snapshot: SessionSnapshot) -> Banner | None:
    if _version(snapshot) is not None:
        return None
    if snapshot.firmware is None:
        text = FIRMWARE_UNKNOWN_TEXT + ". No job until the firmware version is read (rung R1)."
    else:
        text = (FIRMWARE_UNKNOWN_TEXT + ". The robot reported %r (from %s), which has no version number; no job "
                "until a version is read (rung R1)." % (snapshot.firmware, snapshot.firmware_source))
    return Banner(id="FIRMWARE_UNKNOWN", severity="amber", text=text, icon="caution")


# ---------------------------------------------------------------------------
# Top bar
# ---------------------------------------------------------------------------


def top_bar(snapshot: SessionSnapshot) -> dict[str, Any]:
    """The persistent top bar: firmware and its source, the stop chip, and the machine state (design 8.4)."""
    _check_snapshot(snapshot)
    info = PHASE_INFO[snapshot.phase]
    moving = snapshot.may_be_moving
    state = {
        "label": info.label,
        "phase": snapshot.phase.name,
        "may_be_moving": moving,
        "text": info.label + (" - " + MAY_BE_MOVING if moving else ""),
        "severity": "red" if moving else info.banner.severity,
        "icon": "motion" if moving else info.banner.icon,
        "if_stale": STALE_STATE_TEXT,
    }
    if snapshot.stop_offer == "verified":
        stop = {"text": SOFTWARE_STOP_VERIFIED, "severity": "amber", "icon": "stop", "verified": True,
                "if_stale": NO_SOFTWARE_STOP_CHIP}
    else:
        stop = {"text": NO_SOFTWARE_STOP_CHIP, "severity": "red", "icon": "stop", "verified": False,
                "if_stale": NO_SOFTWARE_STOP_CHIP}
    return {"firmware": _firmware_part(snapshot), "stop": stop, "state": state, "practice": snapshot.practice}


# ---------------------------------------------------------------------------
# Banner model
# ---------------------------------------------------------------------------

_RELAXATIONS = ("R-SUPPORT", "R-TABS", "R-FIXED", "R-KERF", "R-DETECT")
"""The relaxations of docs/prep/safety-model.md section 7.5. A disclosure is a relaxation when its id is one of
these names or starts with one followed by ``:``."""


def _is_relaxation(d: Disclosure) -> bool:
    return any(d.id == name or d.id.startswith(name + ":") for name in _RELAXATIONS)


@dataclass(frozen=True)
class _Group:
    banner_id: str
    heading: str
    icon: str
    floor: str
    style: Literal["names", "texts", "count"]
    match: Callable[[Disclosure], bool]


_GROUPS: tuple[_Group, ...] = (
    _Group("RESTART_ATTESTATION_ONLY", "RESTART VERIFIED BY ATTESTATION ONLY", "power", "amber", "texts",
           lambda d: d.id == "RESTART:attestation-only"),
    _Group("UNMEASURED_MACHINE_PROFILE", "UNMEASURED MACHINE PROFILE", "caution", "amber", "names",
           lambda d: d.id.startswith("DEFAULT_UNMEASURED:")),
    _Group("RELAXED_SAFETY", "RELAXED SAFETY", "caution", "amber", "texts", _is_relaxation),
)
_NOTICES = _Group("NOTICES", "NOTICES", "info", "info", "count", lambda d: d.severity == "info")

_DERIVED_IDS = frozenset({"PRACTICE", "NO_SOFTWARE_STOP", "FIRMWARE_UNKNOWN", _NOTICES.banner_id}
                         | {g.banner_id for g in _GROUPS})
_RESERVED_PREFIXES = ("PHASE:", "DISCLOSURE:")


@dataclass(frozen=True)
class BannerModel:
    """The banners to show, in order, and which disclosures each one covers (by banner id)."""

    banners: tuple[Banner, ...]
    covers: Mapping[str, tuple[Disclosure, ...]]

    def __post_init__(self) -> None:
        seen = [b.id for b in self.banners]
        if len(set(seen)) != len(seen):
            raise PresenterError("two banners share an id: %s" % sorted(i for i in seen if seen.count(i) > 1))
        stray = set(self.covers) - set(seen)
        if stray:
            raise PresenterError("covers names banners that are not shown: %s" % sorted(stray))
        object.__setattr__(self, "covers", MappingProxyType(dict(self.covers)))


def _check_snapshot(snapshot: object) -> None:
    if not isinstance(snapshot, SessionSnapshot):
        raise TypeError("the presenters take a goliath_run.types.SessionSnapshot, got %s" % type(snapshot).__name__)


def _check_session_banners(snapshot: SessionSnapshot) -> None:
    for b in snapshot.banners:
        if b.id == AIR_BANNER.id:
            if b != AIR_BANNER:
                raise PresenterError(
                    "the session sent the AIR banner as %r (%s, icon %s); it must be presenters.AIR_BANNER, worded "
                    "%r (red-team PHYS-09)" % (b.text, b.severity, b.icon, AIR_TEXT))
        elif b.id in _DERIVED_IDS or b.id.startswith(_RESERVED_PREFIXES):
            raise PresenterError(
                "the session sent banner %s, whose id is the presenters' (phase %s): the phase banner and the "
                "banners derived from the snapshot are added by the presenters, so a session banner cannot claim "
                "a state" % (b.id, snapshot.phase.name))


def _strip_heading(text: str, heading: str) -> str:
    if text.lower().startswith(heading.lower()):
        rest = text[len(heading):].lstrip(" :.-")
        if rest.strip():
            return rest
    return text


def _group_text(group: _Group, members: list[Disclosure]) -> str:
    distinct = list(dict.fromkeys((d.id, d.source, d.text) for d in members))
    if group.style == "names":
        names = list(dict.fromkeys(i.split(":", 1)[1] if ":" in i else i for i, _, _ in distinct))
        n = len(distinct)
        what = "1 value is an unmeasured default" if n == 1 else "%d values are unmeasured defaults" % n
        return "%s: %s: %s. Each is listed with its consequence in the disclosures." % (
            group.heading, what, ", ".join(names))
    if group.style == "texts":
        return "%s: %s" % (group.heading, "; ".join(_strip_heading(t, group.heading) for _, _, t in distinct))
    ids = list(dict.fromkeys(i for i, _, _ in distinct))
    n = len(distinct)
    return "%s: %s in effect (%s); each is listed in the disclosures." % (
        group.heading, "1 note" if n == 1 else "%d notes" % n, ", ".join(ids))


def _disclosure_banner_id(d: Disclosure) -> str:
    digest = hashlib.sha256("\0".join((d.id, d.source, d.text)).encode("utf-8", "surrogatepass")).hexdigest()
    return "DISCLOSURE:%s/%s" % (d.id, digest[:8])


def banner_model(snapshot: SessionSnapshot, *, stop: StopDescription | None = None) -> BannerModel:
    """Every banner for ``snapshot``, in the order to show them, and the disclosures each covers (invariant I6).

    ``stop`` describes the physical stop (see :func:`physical_stop_text`); without it the NO SOFTWARE STOP
    banner says that the stop is not recorded. Raises :class:`PresenterError` for a session banner that uses a
    presenter-owned id or the AIR id with other content.
    """
    _check_snapshot(snapshot)
    _check_session_banners(snapshot)
    out: list[Banner] = []
    if snapshot.practice:
        out.append(Banner(id="PRACTICE", severity="purple", text=PRACTICE_TEXT, icon="practice"))
    out.append(PHASE_INFO[snapshot.phase].banner)
    if snapshot.stop_offer != "verified":
        text = "%s. To stop: %s." % (NO_SOFTWARE_STOP_HEADING, physical_stop_text(stop))
        version = _version(snapshot)
        if version is not None and FirmwareCapabilities.for_version(parse_firmware_version(version)).has_abort_work:
            text += (" This firmware has ABORT WORK, but no software stop is offered before rung R14 verifies it on "
                     "this robot.")
        out.append(Banner(id="NO_SOFTWARE_STOP", severity="red", text=text, icon="stop"))
    covers: dict[str, list[Disclosure]] = {}
    fw_banner = _firmware_unknown_banner(snapshot)
    fw_members: list[Disclosure] = []
    groups: dict[str, list[Disclosure]] = {}
    singles: dict[tuple[str, str, str], list[Disclosure]] = {}
    for d in snapshot.disclosures:
        if fw_banner is not None and d.id == "FIRMWARE_UNKNOWN":
            fw_members.append(d)
            continue
        group = next((g for g in (*_GROUPS, _NOTICES) if g.match(d)), None)
        if group is not None:
            groups.setdefault(group.banner_id, []).append(d)
        else:
            singles.setdefault((d.id, d.source, d.text), []).append(d)
    if fw_banner is not None:
        severity = "amber"
        for d in fw_members:
            severity = _louder(severity, d.severity)
        out.append(Banner(id=fw_banner.id, severity=severity, text=fw_banner.text, icon=fw_banner.icon))
        if fw_members:
            covers[fw_banner.id] = fw_members
    out.extend(snapshot.banners)
    for group in (*_GROUPS, _NOTICES):
        members = groups.get(group.banner_id)
        if not members:
            continue
        severity = group.floor
        for d in members:
            severity = _louder(severity, d.severity)
        out.append(Banner(id=group.banner_id, severity=severity, text=_group_text(group, members), icon=group.icon))
        covers[group.banner_id] = members
    for (_, _, text), members in singles.items():
        severity = "info"
        for d in members:
            severity = _louder(severity, d.severity)
        banner_id = _disclosure_banner_id(members[0])
        out.append(Banner(id=banner_id, severity=severity, text=text, icon="caution"))
        covers[banner_id] = members
    ordered = sorted(enumerate(out), key=lambda item: (_BANNER_ORDER[item[1].severity], item[0]))
    return BannerModel(banners=tuple(b for _, b in ordered), covers={k: tuple(v) for k, v in covers.items()})


def banners(snapshot: SessionSnapshot, *, stop: StopDescription | None = None) -> list[Banner]:
    """The banners of :func:`banner_model`, in order (design 8.4)."""
    return list(banner_model(snapshot, stop=stop).banners)


# ---------------------------------------------------------------------------
# Disclosure chip
# ---------------------------------------------------------------------------

_UNVERIFIED_DEFAULT_PREFIXES = ("DEFAULT_UNMEASURED:", "DEFAULT:", "INFERRED:")
"""Disclosure ids of values that stand in for a measurement or a source: the chip's "unverified defaults"."""

ChipKind = Literal["fallback", "unverified default"]


@dataclass(frozen=True)
class ChipItem:
    """One disclosure as the chip lists it: what is in effect and its consequence (``text``), and its source."""

    id: str
    severity: str
    kind: ChipKind
    text: str
    source: str
    sticky: bool

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "severity": self.severity, "kind": self.kind, "text": self.text,
                "source": self.source, "sticky": self.sticky}


@dataclass(frozen=True)
class ChipModel:
    """The disclosure chip (design 8.4): a count of what is in effect, each item listed; never dismissed by itself."""

    label: str
    severity: str
    fallbacks: int
    unverified_defaults: int
    items: tuple[ChipItem, ...]
    dismissible: bool = False

    def __post_init__(self) -> None:
        if self.dismissible is not False:
            raise ValueError("the disclosure chip never dismisses itself (design 8.4): dismissible must be False")
        kinds = [i.kind for i in self.items]
        if (kinds.count("fallback"), kinds.count("unverified default")) != (self.fallbacks,
                                                                           self.unverified_defaults):
            raise ValueError("the chip's counts must match its items")
        object.__setattr__(self, "items", tuple(self.items))

    def to_json(self) -> dict[str, Any]:
        return {"label": self.label, "severity": self.severity, "fallbacks": self.fallbacks,
                "unverified_defaults": self.unverified_defaults, "items": [i.to_json() for i in self.items],
                "dismissible": self.dismissible}


def _plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def disclosure_chip(snapshot: SessionSnapshot) -> ChipModel:
    """The chip for ``snapshot``: every disclosure in effect, in the snapshot's order (design 8.4, 8.5)."""
    _check_snapshot(snapshot)
    items: list[ChipItem] = []
    severity = "info"
    for d in snapshot.disclosures:
        kind: ChipKind = "unverified default" if d.id.startswith(_UNVERIFIED_DEFAULT_PREFIXES) else "fallback"
        items.append(ChipItem(id=d.id, severity=d.severity, kind=kind, text=d.text, source=d.source,
                              sticky=d.sticky))
        severity = _louder(severity, d.severity)
    fallbacks = sum(1 for i in items if i.kind == "fallback")
    defaults = len(items) - fallbacks
    if not items:
        label = "No fallbacks or unverified defaults in effect"
    else:
        label = "%s / %s in effect" % (_plural(fallbacks, "fallback"), _plural(defaults, "unverified default"))
    return ChipModel(label=label, severity=severity, fallbacks=fallbacks, unverified_defaults=defaults,
                     items=tuple(items))


# ---------------------------------------------------------------------------
# What the page gets after every snapshot
# ---------------------------------------------------------------------------


def _banner_json(b: Banner, covered: Sequence[Disclosure]) -> dict[str, Any]:
    return {**b.to_json(), "icon_label": BANNER_ICONS[b.icon], "severity_label": SEVERITY_LABELS[b.severity],
            "covers": [d.id for d in covered]}


def session_view(snapshot: SessionSnapshot, *, stop: StopDescription | None = None) -> dict[str, Any]:
    """The persistent parts of every screen for ``snapshot``: window title, top bar, banners and chip (JSON)."""
    model = banner_model(snapshot, stop=stop)
    bar = top_bar(snapshot)
    title = [PRACTICE_TEXT] if snapshot.practice else []
    title += [bar["state"]["text"], APP_NAME]
    return {
        "seq": snapshot.seq,
        "phase": snapshot.phase.name,
        "title": " - ".join(title),
        "top_bar": bar,
        "banners": [_banner_json(b, model.covers.get(b.id, ())) for b in model.banners],
        "chip": disclosure_chip(snapshot).to_json(),
    }


# ---------------------------------------------------------------------------
# Screen building blocks
# ---------------------------------------------------------------------------


def _notice(section_id: str, text: str, severity: str = "info", title: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": section_id, "kind": "notice", "severity": severity, "text": text}
    if title is not None:
        out["title"] = title
    return out


def _fields(section_id: str, title: str, rows: Iterable[tuple[str, Shown]]) -> dict[str, Any]:
    return {"id": section_id, "kind": "fields", "title": title,
            "rows": [{"label": label, "value": value.to_json()} for label, value in rows]}


def _table(section_id: str, title: str, columns: Sequence[str],
           rows: Iterable[tuple[Sequence[str], str]]) -> dict[str, Any]:
    body = []
    for cells, severity in rows:
        if len(cells) != len(columns):  # pragma: no cover - a guard on this module's own tables
            raise PresenterError("table %s: a row has %d cells for %d columns" % (section_id, len(cells),
                                                                                 len(columns)))
        body.append({"cells": [str(c) for c in cells], "severity": severity})
    return {"id": section_id, "kind": "table", "title": title, "columns": list(columns), "rows": body}


def _items(section_id: str, title: str, items: Iterable[str], severity: str = "info") -> dict[str, Any]:
    return {"id": section_id, "kind": "list", "title": title, "severity": severity, "items": list(items)}


def _group(section_id: str, title: str, sections: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {"id": section_id, "kind": "group", "title": title, "sections": list(sections)}


def _screen(name: str, title: str, sections: Sequence[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"screen": name, "title": title, **extra, "sections": list(sections)}


_GATE_STATE: Mapping[bool | None, str] = MappingProxyType({True: "YES", False: "NO",
                                                           None: "UNCERTAIN (treated as NO)"})


def _gate_severity(gate: FeatureGate) -> str:
    if gate.basis == "fact+override":
        return "red"
    return "info" if gate.allowed is True else "amber"


# ---------------------------------------------------------------------------
# Machine screen (design 8.3, screen 1)
# ---------------------------------------------------------------------------

_LINK_NAMES = ("S1", "S2", "S3")


@dataclass(frozen=True)
class LinkStatus:
    """One of the three connections (S1-S3, spec 01): connected (True, False or None for unknown) and the age of
    the last bytes received from it in seconds (None: unknown)."""

    name: str
    connected: bool | None
    last_receive_age_s: float | None

    def __post_init__(self) -> None:
        if self.name not in _LINK_NAMES:
            raise ValueError("LinkStatus.name must be one of %s, got %r" % (", ".join(_LINK_NAMES), self.name))
        if self.connected is not None and type(self.connected) is not bool:
            raise TypeError("LinkStatus.connected must be True, False or None")
        age = self.last_receive_age_s
        if age is not None and (isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age)
                                or age < 0):
            raise ValueError("LinkStatus.last_receive_age_s must be a finite number of seconds >= 0 or None, got %r"
                             % (age,))


def _link_rows(links: Sequence[LinkStatus] | None) -> list[tuple[list[str], str]]:
    by_name: dict[str, LinkStatus] = {}
    for link in links or ():
        if not isinstance(link, LinkStatus):
            raise TypeError("links must hold LinkStatus values, got %s" % type(link).__name__)
        if link.name in by_name:
            raise PresenterError("connection %s is reported twice" % link.name)
        by_name[link.name] = link
    rows = []
    for name in _LINK_NAMES:
        link = by_name.get(name)
        if link is None or link.connected is None:
            state, severity = "unknown", "amber"
        elif link.connected:
            state, severity = "connected", "info"
        else:
            state, severity = "down", "red"
        age = "unknown" if link is None or link.last_receive_age_s is None else "%.1f s" % link.last_receive_age_s
        rows.append(([name, state, age], severity))
    return rows


def _gate_firmware(gates: GateSet) -> str | None:
    firmware = gates.snapshot().json["firmware"]
    return firmware if isinstance(firmware, str) else None


def machine_view(snapshot: SessionSnapshot, *, gates: GateSet | None = None, ledger: LedgerState | None = None,
                 links: Sequence[LinkStatus] | None = None, stop: StopDescription | None = None) -> dict[str, Any]:
    """The Machine screen: connections, identity, fingerprint, gate table, ledger and the physical stop.

    ``gates`` is the session's gate set (None before the robot is identified: every gate is then closed),
    ``ledger`` the robot's ledger, ``links`` the three connections, ``stop`` the physical stop's description.
    Whatever is not given is shown as not available, with what that means.
    """
    _check_snapshot(snapshot)
    sections: list[dict[str, Any]] = []
    version = _version(snapshot)
    if gates is not None:
        if not isinstance(gates, GateSet):
            raise TypeError("gates must be a GateSet, got %s" % type(gates).__name__)
        gate_fw = _gate_firmware(gates)
        if gate_fw != version:
            sections.append(_notice(
                "inconsistent-firmware",
                "INCONSISTENT: the gates below were computed for firmware %s, but the session reports %s. Nothing "
                "in this table applies until they agree." % (gate_fw or "unknown", version or "unknown"), "red"))
        if snapshot.stop_offer == "verified" and gates.allowed("software_stop") is not True:
            sections.append(_notice(
                "inconsistent-stop",
                "INCONSISTENT: the session offers a software stop, but the gate software_stop is not open. Use the "
                "physical stop.", "red"))
    fw = _firmware_part(snapshot)
    if fw["known"]:
        firmware = Shown(text=snapshot.firmware or "", tag="[robot]", ref="reported by the robot in %s"
                         % snapshot.firmware_source, known=True)
    else:
        firmware = shown_unknown("rung R1 reads the firmware version (the read-only probe)", text=fw["text"])
    if gates is None:
        branch = shown_unknown("computed with the gates once the robot is identified")
        fingerprint = shown_unknown("computed with the gates once the robot is identified")
    else:
        status = gates.fingerprint
        branch = (Shown(text=gates.branch, tag="[robot]", ref="the R1 branch (B1-B5) from the firmware and the "
                        "fingerprint", known=True) if gates.branch is not None
                  else shown_unknown("the firmware version is unknown"))
        fingerprint = Shown(text="%s: %s" % (status.state, status.reason), tag="[robot]",
                            ref="fingerprint family %s" % (status.family or "none"), known=True)
    sections.append(_fields("identity", "Identity", [("Firmware", firmware), ("Branch", branch),
                                                      ("Fingerprint", fingerprint)]))
    links_section = _table("connections", "Connections", ["Connection", "State", "Last receive"], _link_rows(links))
    if links is None:
        links_section["note"] = "Not reported in this view: the connection states are unknown."
    sections.append(links_section)
    if gates is None:
        sections.append(_notice(
            "gates", "No gate set: the robot has not been identified in this session, so every gate is closed "
                     "(an unknown is never read as open). The gates are computed after IDENTIFY.", "amber",
            title="Gates"))
    else:
        rows = []
        for name, gate in gates.gates.items():
            rows.append(([name, _GATE_STATE[gate.allowed], gate.basis, gate.reason,
                          "; ".join(gate.evidence) or "none", gate.unlock_rung or "-",
                          "yes, with a typed reason" if gate.overridable else "no"], _gate_severity(gate)))
        sections.append(_table("gates", "Gates", ["Gate", "Allowed", "Basis", "Reason", "Evidence",
                                                  "Unlocked by rung", "Overridable"], rows))
    sections.append(_ledger_section(ledger))
    stop_rows = [("Software stop", Shown(
        text=SOFTWARE_STOP_VERIFIED if snapshot.stop_offer == "verified" else NO_SOFTWARE_STOP_CHIP,
        tag="[robot]" if snapshot.stop_offer == "verified" else "[policy]",
        ref="no software-stop control on any firmware before rung R14 (red-team PHYS-13)", known=True)),
        ("How to stop", Shown(text=physical_stop_text(stop), tag="[policy]",
                              ref="the physical stop's description (field guide 04)", known=True))]
    for name in STOP_FIELDS:
        value = None if stop is None else _as_shown(stop.get(name), name)
        stop_rows.append((name, value if value is not None else shown_unknown("field guide 04 records it at rung "
                                                                                "R0b")))
    sections.append(_fields("stop", "Stopping the robot", stop_rows))
    return _screen("machine", "Machine", sections, seq=snapshot.seq)


def _ledger_section(ledger: LedgerState | None) -> dict[str, Any]:
    if ledger is None:
        return _notice("ledger", "Ledger: not available in this view (unknown). Whether a power cycle or an upload "
                                 "hazard is pending is not shown here; the command line shows the ledger.", "amber",
                       title="Ledger")
    if not isinstance(ledger, LedgerState):
        raise TypeError("ledger must be a LedgerState, got %s" % type(ledger).__name__)
    items = list(ledger.attention())
    if ledger.last_clearing is not None:
        c = ledger.last_clearing
        items.append("last power cycle accepted at %s (%s, route %s)" % (c.at, c.rule, c.route))
    if not ledger.attention():
        text = "Nothing pending in this robot's ledger: no upload hazard, no power cycle required"
        if ledger.updated_at:
            text += " (updated %s)" % ledger.updated_at
        if ledger.last_clearing is not None:
            text += "; %s" % items[-1]
        return _notice("ledger", text + ".", "info", title="Ledger")
    severity = "red" if ledger.may_be_in_upload_mode else "amber"
    return _items("ledger", "Ledger", items, severity)


# ---------------------------------------------------------------------------
# Ladder screen (design 8.3, screen 2; the ladder of design section 11)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rung:
    """One rung of the bring-up ladder: its id, title, goal and go criteria (the project design, section 11)."""

    id: str
    title: str
    goal: str
    go: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.go:
            raise ValueError("rung %s needs its go criteria" % self.id)


_LATER = "the detailed go criteria are written with the rung's procedure"

LADDER: tuple[Rung, ...] = (
    Rung("R1", "Read-only probe",
         "Read the firmware version and the robot's fingerprint without moving anything (field guide 02).",
         ("the probe exits 0, or 3 with every warning understood",
          "the firmware version is parsed",
          "routing nominal: INFO, ERR and WARN on S2, direct replies on S1",
          "the command terminator is none")),
    Rung("R0b", "Tape-measure the robot",
         "With the robot powered off: wheel layout and contact radii, body outline, where the towers' wires attach, "
         "and what the physical stop is (field guide 04).",
         ("support distances within 15 mm of 402, 211 and 211 mm",
          "support angles within 5 degrees of 0 and +-76 degrees about the body axis, spindle at the front")),
    Rung("R0a", "Desk rehearsal (no robot)",
         "Everything green on the desk: the tests including the ladder rehearsal, goliath-run doctor with the "
         "network off, and the rung programs certified for the declared scrap panel.",
         ("all checks green", "the rung programs certify for the declared panel")),
    Rung("R1b", "Read-only follow-ups",
         "The probe again with both towers on, the slot-release sweep, and one session idle for at least 60 s.",
         ("consistent results",)),
    Rung("R2", "Pairing and panel detection, no upload; the physical stop drilled on motion",
         "Pair with the towers and let the robot detect a scrap panel with no cuts; stop it once with the physical "
         "stop on its own motion.",
         ("the drill stopped the drive within the time set beforehand, and the link dropped",
          "every expected INFO in the specified order on S2",
          "exactly one decoding of the panel corners fits the taped corners within 10 mm, and it is identity",
          "INFO;27 within 10 mm of the taped tower distance",
          "INFO;0 after SET PANEL SIZE and no INFO;42",
          "INFO;40 equal to the idle baseline, or the difference reviewed and recorded",
          "no ERR")),
    Rung("R3", "Air-motion job (spindle off, Z >= +5 mm, inside the safe zone)",
         "The first upload: a fixed air job that checks direction, heading, rotation and heights.",
         ("the job starts by itself and INFO;23 arrives",
          "no end-of-upload signal before the last block",
          "the first motion goes toward the panel centre",
          "reported positions within 5 mm of the commanded points",
          "the physical heading matches theta = (C + 90) mod 360, the body behind the tool",
          "the tool moves at most 5 mm during each rotate-in-place line",
          "bit heights within 1.5 mm, and the first lines raise the bit",
          "the calibration touches off on the panel surface",
          "no ERR and no unexpected motion; an end-of-upload signal identified")),
    Rung("R3b", "Physical-stop drill in a job",
         "The R3 job again, stopped about 10 s into the work with the physical stop; then two more power cycles.",
         ("motion stops within the time set beforehand",
          "the robot comes back reachable",
          "this program shows LINK LOST, then POWER CYCLE REQUIRED, without being restarted",
          "an INFO;41 is shown and not answered",
          "after the second power cycle the robot no longer reports a pending resume")),
    Rung("R3c", "Clearing a pending resume (only if R3b needs it)",
         "Chosen by a design review: only when INFO;41 persists after R3b's power cycles.",
         ("after a power cycle the robot no longer reports INFO;41",)),
    Rung("R4", "Spindle air job",
         "The R3 job with the spindle on (M10 and S), every Z at +5 mm or above.",
         ("the spindle starts at M10 and stops at M05",
          "motion after M10 waits for spin-up, or the delay is judged acceptable at the entry feed",
          "no ERR; INFO;23")),
    Rung("R5", "First real cut",
         "A closed 100 x 100 mm square, 1.0 mm deep, at the panel centre, on a panel with no other cuts.",
         ("INFO;23 and no ERR",
          "groove depth 1.0 mm within 0.3 mm",
          "sides 100 mm within 0.5 mm; start and end meet",
          "the position trace matches the program")),
    Rung("R7", "Through cut with tabs",
         "A small part with an inner hole, cut inside-out and through, with tabs; support underneath confirmed.",
         ("the part and its tabs behave as planned", _LATER)),
    Rung("R8k", "A wheel crossing a narrow kerf, measured",
         "A through slot cut with no wheel crossing it, then an air job in which one side wheel crosses it at right "
         "angles at low feed.",
         ("no ERR", "deviation within 1 mm", "no visible drop")),
    Rung("R10", "Larger programs",
         "An air job of about 200 KB of short moves.",
         ("the whole program runs", _LATER)),
    Rung("R6", "Arcs",
         "The R3 air job with G02/G03 (partial and split full circles), then a shallow circle cut.",
         ("the arcs run as programmed", _LATER)),
    Rung("R8", "Pockets",
         "A 150 x 150 mm pocket, 3 mm deep, in two depth passes.",
         ("the pocket is cut as programmed", _LATER)),
    Rung("R9", "Near the edges, per edge",
         "Air jobs approaching each edge and the tower zones at the checker's limit with a 10 mm margin; the "
         "tower-baseline edge only if included explicitly.",
         ("each edge approached at the 10 mm margin without contact", _LATER)),
    Rung("R11", "M11",
         "An air job with M05, M11 and M10 in the middle of the program.",
         ("what happens is recorded and understood", _LATER)),
    Rung("R12", "Second job without a power cycle (optional, owner opt-in)",
         "After an air job, a second air job without a power cycle; needed on every firmware.",
         ("the second job runs as the first did", _LATER)),
    Rung("R13", "Jog",
         "ENTER JOG, one GOTO TARGET in the safe zone, JOG STOP GOLIATH, EXIT JOG.",
         ("the robot goes to the target and stops", _LATER)),
    Rung("R14", "Software stop (firmware newer than 01.02.00 only)",
         "ABORT WORK during an air job, under a rung exemption.",
         ("the stop is confirmed only by INFO;18 or INFO;23, or by the position frames stopping", _LATER)),
    Rung("R15", "Series on one panel",
         "Two jobs on one panel with a power cycle between; the detection path and the calibration location "
         "observed.",
         ("both jobs run and the re-detected panel matches", _LATER)),
)
"""The rungs in the order the ladder runs them (design 11.2, then 11.3: by what blocks real work)."""

_RUNG_IDS = [r.id for r in LADDER]
if len(set(_RUNG_IDS)) != len(_RUNG_IDS):  # pragma: no cover - a guard on this module's tables
    raise RuntimeError("a rung is listed twice in LADDER")
_UNLISTED = sorted({s.unlock_rung for s in GATE_CATALOGUE.values() if s.unlock_rung} - set(_RUNG_IDS))
if _UNLISTED:  # pragma: no cover - a guard on this module's tables
    raise RuntimeError("gates are unlocked by rungs that are not on the ladder: %s" % _UNLISTED)


def _rung_status(rung: Rung, facts: FactStore | None, gates: GateSet | None) -> tuple[str, str]:
    if facts is None:
        return ("UNKNOWN: no fact store in this view (no robot identified in this session); nothing is claimed for "
                "this rung", "amber")
    opens = [n for n, s in GATE_CATALOGUE.items() if s.unlock_rung == rung.id]
    stale = [f for f in facts.stale() if f.rung == rung.id]
    current = [f for f in facts.current().values() if f.rung == rung.id]
    unverified = [f for f in facts.unverified() if f.rung == rung.id]
    if stale:
        return ("REPEAT: %s recorded at this rung %s stale (%s); repeat the rung" % (
            _plural(len(stale), "fact"), "is" if len(stale) == 1 else "are", facts.why_stale(stale[0].id)), "amber")
    if opens and gates is not None:
        opened = [n for n in opens if gates.gate(n).allowed is True and gates.gate(n).basis.startswith("fact")]
        if len(opened) == len(opens):
            return ("GATES OPEN: the %s this rung unlocks %s open on this robot's facts" % (
                _plural(len(opens), "gate"), "is" if len(opens) == 1 else "are"), "info")
        if opened:
            return ("PARTIAL: %d of the %s this rung unlocks %s open" % (
                len(opened), _plural(len(opens), "gate"), "is" if len(opened) == 1 else "are"), "amber")
    if current or unverified:
        if not opens:
            tail = "it unlocks no gate"
        elif gates is None:
            tail = "its gates are unknown here (no gate set)"
        else:
            tail = "its gates are closed"
        return ("FACTS RECORDED: %s from this rung; %s" % (_plural(len(current) + len(unverified), "fact"), tail),
                "info")
    return "NOT RUN: no fact recorded at this rung", "info"


def ladder_view(facts: FactStore | None, gates: GateSet | None, *,
                ledger: LedgerState | None = None) -> dict[str, Any]:
    """The Ladder screen: every rung with its status, go criteria, the gates it unlocks and the facts it recorded.

    ``facts`` and ``gates`` are the session's fact store and gate set (None before the robot is identified:
    every status is then UNKNOWN). A rung's status says what the facts show (NOT RUN, FACTS RECORDED,
    PARTIAL, GATES OPEN, REPEAT when its facts are stale); it never claims a go that no fact records.
    """
    if facts is not None and not isinstance(facts, FactStore):
        raise TypeError("facts must be a FactStore, got %s" % type(facts).__name__)
    if gates is not None and not isinstance(gates, GateSet):
        raise TypeError("gates must be a GateSet, got %s" % type(gates).__name__)
    if ledger is not None and not isinstance(ledger, LedgerState):
        raise TypeError("ledger must be a LedgerState, got %s" % type(ledger).__name__)
    sections: list[dict[str, Any]] = [_notice(
        "order", "Rungs in the order they run. Any no-go stops the ladder: power the robot off and take the record "
                 "to a design review. Facts are written only on go, and only for the criteria that passed.")]
    stale_reasons: dict[str, str] = {}
    unverified_reasons: dict[str, str] = {}
    if facts is not None:
        stale_reasons = {f.id: facts.why_stale(f.id) for f in facts.stale()}
        unverified_reasons = {f.id: facts.why_unverified(f.id) for f in facts.unverified()}
    for rung in LADDER:
        status, severity = _rung_status(rung, facts, gates)
        parts: list[dict[str, Any]] = [_notice("rung-%s-status" % rung.id, status, severity),
                                       _notice("rung-%s-goal" % rung.id, rung.goal),
                                       _items("rung-%s-go" % rung.id, "Go criteria", rung.go)]
        opens = [n for n, s in GATE_CATALOGUE.items() if s.unlock_rung == rung.id]
        if opens:
            rows = []
            for name in opens:
                if gates is None:
                    rows.append(([name, "unknown", "no gate set"], "amber"))
                else:
                    gate = gates.gate(name)
                    rows.append(([name, _GATE_STATE[gate.allowed], gate.basis], _gate_severity(gate)))
            parts.append(_table("rung-%s-gates" % rung.id, "Gates this rung unlocks", ["Gate", "Allowed", "Basis"],
                                rows))
        if facts is not None:
            rows = []
            recorded = [*facts.current().values(), *facts.unverified(), *facts.stale()]
            for fact in sorted((f for f in recorded if f.rung == rung.id), key=lambda f: f.id):
                stale = stale_reasons.get(fact.id)
                unverified = unverified_reasons.get(fact.id)
                shown = shown_fact(fact, stale=stale, unverified=unverified)
                state = "STALE" if stale else "unverified for this firmware" if unverified else "in force"
                rows.append(([fact.id, shown.text, shown.tag, state], "amber" if stale or unverified else "info"))
            if rows:
                parts.append(_table("rung-%s-facts" % rung.id, "Facts this rung recorded",
                                    ["Fact", "Value", "Provenance", "State"], rows))
            else:
                parts.append(_notice("rung-%s-facts" % rung.id, "No fact recorded at this rung yet."))
        if ledger is not None and rung.id in ledger.rung_results:
            parts.append(_notice("rung-%s-result" % rung.id, "Result recorded in the ledger: %s" % _value_text(
                ledger.rung_results[rung.id])))
        sections.append(_group("rung-%s" % rung.id, "%s: %s" % (rung.id, rung.title), parts))
    return _screen("ladder", "Ladder", sections)


# ---------------------------------------------------------------------------
# Records screen (design 8.3, screen 8; the session record of design 7.9)
# ---------------------------------------------------------------------------

_SUMMARY_KEYS = frozenset({"schema_version", "job_id", "outcome", "reasons", "created_at", "finished_at", "policy",
                           "wire", "disclosures"})


@dataclass(frozen=True)
class RecordSummary:
    """One session record as the Records screen lists it (read from its ``summary.json`` by the caller).

    ``outcome`` None with no ``problem``: the session has no summary (it did not finish its record). ``problem``:
    why the summary could not be read; the row says so.
    """

    folder: str
    job_id: str | None
    created_at: str | None
    finished_at: str | None
    outcome: str | None
    reasons: tuple[str, ...]
    disclosures: tuple[Disclosure, ...]
    problem: str | None = None

    @classmethod
    def from_summary(cls, folder: str, data: Mapping[str, Any] | None) -> RecordSummary:
        """The record of ``folder`` from its parsed ``summary.json`` (None: there is none). Never raises for bad data:
        a summary this version cannot read becomes a row that says so."""
        if type(folder) is not str or not folder.strip():
            raise ValueError("a record needs its folder name")
        if data is None:
            return cls(folder, None, None, None, None, (), ())
        try:
            if not isinstance(data, Mapping):
                raise TypeError("summary.json is not a JSON object")
            if data.get("schema_version") != 1:
                raise ValueError("summary.json schema_version %r is not 1" % (data.get("schema_version"),))
            keys = set(data)
            if keys != _SUMMARY_KEYS:
                raise ValueError("summary.json keys: missing %s, unexpected %s" % (
                    sorted(_SUMMARY_KEYS - keys), sorted(map(str, keys - _SUMMARY_KEYS))))
            outcome = data["outcome"]
            if outcome not in OUTCOMES:
                raise ValueError("outcome %r is not one of %s" % (outcome, ", ".join(OUTCOMES)))
            reasons = data["reasons"]
            if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
                raise TypeError("reasons must be a list of strings")
            for key in ("job_id", "created_at", "finished_at"):
                if not isinstance(data[key], str):
                    raise TypeError("%s must be a string" % key)
            if not isinstance(data["disclosures"], list):
                raise TypeError("disclosures must be a list")
            disclosures = tuple(Disclosure.from_json(d) for d in data["disclosures"])
        except (KeyError, TypeError, ValueError) as exc:
            return cls(folder, None, None, None, None, (), (), problem="%s: %s" % (type(exc).__name__, exc))
        return cls(folder, data["job_id"], data["created_at"], data["finished_at"], outcome, tuple(reasons),
                   disclosures)


def records_view(records: Sequence[RecordSummary] | None) -> dict[str, Any]:
    """The Records screen: past sessions with their outcome, reasons and disclosures, and how to export one."""
    export = _notice("export", "Diagnostic export for a bug report, from the command line: goliath-run diag export "
                               "<session folder>. The robot's serial number and Wi-Fi names are redacted by default.")
    if records is None:
        return _screen("records", "Records", [_notice(
            "sessions", "No record folder is attached to this view: past sessions cannot be listed here.", "amber",
            title="Sessions"), export])
    rows = []
    for r in records:
        if not isinstance(r, RecordSummary):
            raise TypeError("records must hold RecordSummary values, got %s" % type(r).__name__)
        if r.problem is not None:
            outcome, severity = "UNREADABLE: %s" % r.problem, "red"
        elif r.outcome is None:
            outcome = ("NOT FINISHED: no summary. The session ended without closing its record; check the ledger "
                       "before the next job")
            severity = "amber"
        else:
            outcome, severity = r.outcome, "info" if r.outcome == "COMPLETED" else "amber"
        rows.append(([r.folder, r.created_at or "unknown", outcome, "; ".join(r.reasons) or "-",
                      str(len(r.disclosures)), r.finished_at or "unknown"], severity))
    if not rows:
        sessions = _notice("sessions", "No sessions recorded yet.", title="Sessions")
    else:
        sessions = _table("sessions", "Sessions", ["Session folder", "Started", "Outcome", "Reasons", "Disclosures",
                                                   "Finished"], rows)
    return _screen("records", "Records", [sessions, export])


# ---------------------------------------------------------------------------
# Settings screen (design 8.3, screen 9)
# ---------------------------------------------------------------------------


def _param_severity(param: Param[Any]) -> str:
    worst = "info"
    for d in param.all_disclosures():
        worst = _louder(worst, d.severity)
    if param.source == "DEFAULT_UNMEASURED":
        worst = _louder(worst, "amber")
    return worst


def settings_view(params: Mapping[str, Param[Any]] | None = None, *, profile: MachineProfile | None = None,
                  network: Mapping[str, Shown] | None = None) -> dict[str, Any]:
    """The Settings screen: every safety parameter with its provenance, the machine profile, tools and network.

    ``params`` defaults to the catalogue's values (goliath_prep.defaults.SPEC_PARAMS) with the profile's values
    over them. ``network`` is what the session knows about the robot's network; without it the robot's own
    access-point address is shown with its provenance and the session's address as unknown.
    """
    if profile is not None and not isinstance(profile, MachineProfile):
        raise TypeError("profile must be a goliath_prep MachineProfile, got %s" % type(profile).__name__)
    if params is None:
        params = {**SPEC_PARAMS, **(dict(profile.params) if profile is not None else {})}
    rows = []
    for name in sorted(params):
        param = params[name]
        if not isinstance(param, Param):
            raise TypeError("parameter %s must be a Param, got %s" % (name, type(param).__name__))
        shown = shown_param(param)
        notes = "; ".join(d.text for d in param.all_disclosures()) or "-"
        rows.append(([name, shown.text, shown.tag, shown.ref, notes], _param_severity(param)))
    sections = [_table("parameters", "Safety parameters", ["Parameter", "Value", "Provenance", "Source", "Notes"],
                       rows)]
    if profile is None:
        sections.append(_notice(
            "profile", "No machine profile loaded: every machine value above is the catalogue's, and the unmeasured "
                       "ones are defaults (UNMEASURED MACHINE PROFILE). Field guide 04 measures them at rung R0b.",
            "amber", title="Machine profile"))
    else:
        tag = "[robot, measured]" if profile.origin == "exported" else "[owner]"
        prows = []
        for name in MEASURED_FIELDS:
            value = profile.readings[name]
            if value is None:
                prows.append(([name, "unknown", _UNKNOWN_TAG], "amber"))
            else:
                prows.append(([name, _value_text(value), tag], "info"))
        table = _table("profile", "Machine profile (%s, %s)" % (profile.origin, "machine.toml"),
                       ["Field", "Value", "Provenance"], prows)
        sections.append(table)
    sections.append(_notice(
        "tools", "No tool table: each job declares its tool in job.toml ([tool]), and that job's checks use it.",
        title="Tools"))
    if network is None:
        net_rows = [
            ("Robot address on its own Wi-Fi", Shown(text=DEFAULT_AP_ADDRESS, tag="[spec]",
                                                     ref="docs/protocol/04-connection-and-provisioning.md",
                                                     known=True)),
            ("Address this session uses", shown_unknown("not reported in this view")),
        ]
    else:
        net_rows = []
        for label, value in network.items():
            if not isinstance(value, Shown):
                raise TypeError("network values must be Shown, got %s for %r" % (type(value).__name__, label))
            net_rows.append((label, value))
    sections.append(_fields("network", "Network", net_rows))
    return _screen("settings", "Settings", sections)


# ---------------------------------------------------------------------------
# Screens served without a driver
# ---------------------------------------------------------------------------


def default_screens(source: SnapshotSource) -> dict[str, Callable[[], dict[str, Any]]]:
    """The screens of this unit built from ``source`` alone (no fact store, gates, ledger, records or profile).

    What only the driver knows is then shown as not available. The driver passes its own providers to
    :func:`goliath_run.ui.serve` once it has those.
    """
    return {
        "machine": lambda: machine_view(source.snapshot()),
        "ladder": lambda: ladder_view(None, None),
        "records": lambda: records_view(None),
        "settings": lambda: settings_view(),
    }
