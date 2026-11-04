# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The runner's shared interface types (unit R00; the project design, sections 7.1-7.5, 8.4).

Red-team C-06: phases, snapshots, operator intents, banners and restart
evidence have one owner, so the core, the driver and the UI cannot diverge.
"""
from __future__ import annotations

import ast
import copy
import dataclasses
import json
import pickle
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, get_args

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import goliath_run.types as T
from goliath_prep.core.disclosure import Disclosure
from goliath_run.types import (
    BANNER_ICONS,
    BANNER_SEVERITIES,
    BASELINE_STATES,
    CONFIRM_KINDS,
    CONFIRMATION_INTENTS,
    INTENT_TYPES,
    MEASUREMENT_UNITS,
    PHASE_INFO,
    STOP_OFFERS,
    Arm,
    AttestPowerCycle,
    Banner,
    Cancel,
    Confirm,
    Connect,
    EnterMeasurement,
    FrozenMap,
    IntentSink,
    IStoppedTheRobot,
    JobFinishedNoInfo23,
    OperatorIntent,
    Override,
    Phase,
    PhaseInfo,
    PickBundle,
    RestartEvidence,
    SessionSnapshot,
    SnapshotSource,
    StopOffer,
    intent_from_json,
    may_be_moving_on_entry,
)

REPO = Path(__file__).resolve().parents[2]
TYPES_PY = REPO / "goliath-run" / "src" / "goliath_run" / "types.py"
FIELDS_JSON = REPO / "docs" / "field-guide" / "04-measure-your-robot.fields.json"

# The phase list of the unit's public interface, in its order.
DESIGNED_PHASES = (
    "IDLE", "NET_CHECK", "CONNECTING", "IDENTIFY", "LEDGER_CHECK", "PREFLIGHT", "READY", "PAIRING",
    "ENCODER", "PANEL_DETECTION", "CERTIFY", "CAGE", "COMMIT", "ARMED", "UPLOADING", "RUNNING",
    "RUNNING_UNCONFIRMED", "LINK_LOST", "MONITORING_UNVERIFIED", "SETUP_LINK_LOST", "COMPLETE",
    "POWER_CYCLE_REQUIRED", "UPLOAD_HAZARD", "SILENT_AFTER_SEND", "MONITOR_ONLY", "BLOCKED", "FAULTED",
)

# Phases in which the robot may be moving whatever came before (see PHASE_INFO's basis texts):
# from pairing on the robot may drive itself (spec 03 steps B-D, red-team FW-10, K2), and from
# the upload until the attested power cycle (design 8.4), plus every state that has lost sight
# of the robot or may have left it in upload mode.
MOVING_PHASES = {
    "PAIRING", "ENCODER", "PANEL_DETECTION", "UPLOADING", "RUNNING", "RUNNING_UNCONFIRMED", "LINK_LOST",
    "MONITORING_UNVERIFIED", "SETUP_LINK_LOST", "COMPLETE", "POWER_CYCLE_REQUIRED", "UPLOAD_HAZARD",
    "SILENT_AFTER_SEND", "MONITOR_ONLY",
}
# BLOCKED and FAULTED keep the flag of the phase they were entered from ("FAULTED keeps 'may be
# running' if after the header", design 7.3).
INHERITING_PHASES = {"BLOCKED", "FAULTED"}

UPLOAD_MODE_TEXT = (
    "ROBOT MAY BE IN UPLOAD MODE: power-cycle before anything else (stay connected, then switch the controller off)"
)


def snap(**changes: Any) -> SessionSnapshot:
    fields: dict[str, Any] = dict(
        seq=0, phase=Phase.IDLE, may_be_moving=False, banners=(), disclosures=(), firmware=None,
        firmware_source=None, stop_offer="none", practice=False, progress=None,
    )
    fields.update(changes)
    return SessionSnapshot(**fields)


def banner(id_: str = "ROUTING-ANOMALY", severity: str = "red", text: str = "routing anomaly",
           icon: str = "link") -> Banner:
    return Banner(id=id_, severity=severity, text=text, icon=icon)  # type: ignore[arg-type]


def disclosure(id_: str = "DEFAULT_UNMEASURED:r_w") -> Disclosure:
    return Disclosure(id=id_, severity="amber", text="r_w = 40 mm, not measured", source="docs/prep/params.json r_w")


def through_json(data: object) -> Any:
    """What the UI server and the recorder do: plain JSON text, NaN refused."""
    return json.loads(json.dumps(data, allow_nan=False))


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

TEXT = st.text(st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=40).filter(lambda s: s.strip())
ANY_TEXT = st.text(st.characters(blacklist_categories=("Cs",)), max_size=40)
IDS = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._:/;=+-]{0,20}", fullmatch=True)
FINITE = st.floats(allow_nan=False, allow_infinity=False)
JSON_LEAVES = st.none() | st.booleans() | st.integers() | FINITE | ANY_TEXT
JSON_VALUES = st.recursive(
    JSON_LEAVES,
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(ANY_TEXT, inner, max_size=4),
    max_leaves=12,
)

banners_st = st.builds(
    Banner, id=IDS, severity=st.sampled_from(BANNER_SEVERITIES), text=TEXT, icon=st.sampled_from(sorted(BANNER_ICONS))
)
disclosures_st = st.builds(
    Disclosure, id=IDS, severity=st.sampled_from(("info", "amber", "red")), text=TEXT, source=TEXT,
    sticky=st.booleans(),
)


@st.composite
def snapshots(draw: st.DrawFn) -> SessionSnapshot:
    phase = draw(st.sampled_from(list(Phase)))
    moving = PHASE_INFO[phase].may_be_moving or draw(st.booleans())
    raw_banners = draw(st.lists(banners_st, max_size=4, unique_by=lambda b: b.id))
    firmware = draw(st.none() | st.sampled_from(("01.02.00", "01.02.03", "GFW 01.03.01", "garbled")) | TEXT)
    source = None if firmware is None else draw(TEXT)
    abort_capable = firmware in ("01.02.03", "GFW 01.03.01")
    stop_offer = draw(st.sampled_from(STOP_OFFERS)) if abort_capable else "none"
    progress = draw(st.none() | st.dictionaries(ANY_TEXT, JSON_VALUES, max_size=5))
    return SessionSnapshot(
        seq=draw(st.integers(min_value=0, max_value=2**40)), phase=phase, may_be_moving=moving,
        banners=tuple(raw_banners), disclosures=tuple(draw(st.lists(disclosures_st, max_size=3))),
        firmware=firmware, firmware_source=source, stop_offer=stop_offer, practice=draw(st.booleans()),
        progress=progress,
    )


NUMBER_UNITS = tuple(sorted(MEASUREMENT_UNITS))


@st.composite
def measurements(draw: st.DrawFn) -> EnterMeasurement:
    fact_id = draw(st.from_regex(r"[a-z][a-z0-9_]{0,8}(\.[a-z0-9_]{1,8}){1,3}", fullmatch=True))
    kind = draw(st.sampled_from(("number", "count", "text")))
    operator = draw(TEXT)
    if kind == "text":
        return EnterMeasurement(fact_id=fact_id, value=draw(TEXT), unit=None, tolerance=None, operator=operator)
    if kind == "count":
        return EnterMeasurement(fact_id=fact_id, value=draw(st.integers(min_value=0, max_value=1000)), unit="count",
                                tolerance=draw(st.integers(min_value=0, max_value=3)), operator=operator)
    unit = draw(st.sampled_from([u for u in NUMBER_UNITS if u != "count"]))
    return EnterMeasurement(fact_id=fact_id, value=draw(FINITE), unit=unit,
                            tolerance=draw(st.floats(min_value=0, max_value=1e6)), operator=operator)


intents_st = st.one_of(
    st.just(Connect()),
    st.just(Cancel()),
    st.builds(Confirm, kind=st.sampled_from(CONFIRM_KINDS), shown=TEXT, answer=ANY_TEXT),
    st.builds(PickBundle, path=TEXT.filter(lambda s: "\x00" not in s)),
    st.builds(Arm, phrase=ANY_TEXT),
    st.builds(AttestPowerCycle, text=ANY_TEXT),
    st.builds(IStoppedTheRobot, text=ANY_TEXT),
    st.builds(JobFinishedNoInfo23, text=ANY_TEXT),
    st.builds(Override, gate=st.from_regex(r"[a-z][a-z0-9_]{0,20}", fullmatch=True), reason=TEXT),
    measurements(),
)


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


def test_phase_names_and_order_as_designed() -> None:
    assert tuple(p.name for p in Phase) == DESIGNED_PHASES
    # The JSON form of a phase is its name.
    assert all(p.value == p.name for p in Phase)


def test_red_team_phases_present_and_overlays_are_not_phases() -> None:
    for name in ("MONITORING_UNVERIFIED", "SETUP_LINK_LOST", "SILENT_AFTER_SEND"):
        assert Phase[name].name == name
    # An alarm is an overlay on top of a phase (design 7.5, 7.8), and a refused job ends
    # BLOCKED with its reason (the unit's phase list has no DONE): pinned reading.
    for name in ("ALARM", "DONE", "STOPPED"):
        assert name not in Phase.__members__


def test_every_phase_has_banner_text_and_may_be_moving_flag() -> None:
    assert isinstance(PHASE_INFO, Mapping)
    assert set(PHASE_INFO) == set(Phase)
    for phase in Phase:
        info = PHASE_INFO[phase]
        assert isinstance(info, PhaseInfo)
        assert info.phase is phase
        assert isinstance(info.label, str) and info.label.strip() and info.label == info.label.upper()
        assert isinstance(info.banner, Banner)
        assert info.banner.text.strip()
        assert info.banner.icon in BANNER_ICONS
        assert type(info.may_be_moving) is bool
        assert type(info.keeps_may_be_moving) is bool
        assert info.basis.strip(), "%s: the flag needs a stated basis" % phase.name
    ids = [info.banner.id for info in PHASE_INFO.values()]
    assert len(ids) == len(set(ids))
    with pytest.raises(TypeError):
        PHASE_INFO[Phase.IDLE] = PHASE_INFO[Phase.IDLE]  # type: ignore[index]


def test_may_be_moving_flags_pinned() -> None:
    assert {p.name for p in Phase if PHASE_INFO[p].may_be_moving} == MOVING_PHASES
    assert {p.name for p in Phase if PHASE_INFO[p].keeps_may_be_moving} == INHERITING_PHASES
    # The red-team phases are all "may be moving".
    assert {"MONITORING_UNVERIFIED", "SETUP_LINK_LOST", "SILENT_AFTER_SEND"} <= MOVING_PHASES


def test_moving_phase_banners_are_amber_or_red() -> None:
    for phase in Phase:
        info = PHASE_INFO[phase]
        if info.may_be_moving:
            assert info.banner.severity in ("amber", "red"), phase
        assert info.banner.severity in ("info", "amber", "red"), phase  # purple and blue are not phase colours


def test_phase_banner_wording_from_the_design() -> None:
    text = {p.name: PHASE_INFO[p].banner.text for p in Phase}
    assert "LINK LOST: THE JOB MAY STILL BE RUNNING" in text["LINK_LOST"]
    assert "MONITORING UNVERIFIED: safety errors may not reach this laptop" in text["MONITORING_UNVERIFIED"]
    assert "SETUP LINK LOST: the robot may be moving (panel detection); do not approach" in text["SETUP_LINK_LOST"]
    assert UPLOAD_MODE_TEXT in text["UPLOAD_HAZARD"]
    assert UPLOAD_MODE_TEXT in text["SILENT_AFTER_SEND"]
    assert "upload not confirmed" in text["RUNNING_UNCONFIRMED"].lower()
    assert PHASE_INFO[Phase.RUNNING_UNCONFIRMED].banner.severity == "amber"
    for name in ("LINK_LOST", "MONITORING_UNVERIFIED", "SETUP_LINK_LOST", "UPLOAD_HAZARD", "SILENT_AFTER_SEND"):
        assert PHASE_INFO[Phase[name]].banner.severity == "red", name


def test_monitoring_unverified_never_reads_running() -> None:
    # Red-team FW-03: after a reconnect nothing may read as if the job were confirmed running.
    info = PHASE_INFO[Phase.MONITORING_UNVERIFIED]
    for words in (info.label, info.banner.text, info.banner.id):
        assert "running" not in words.lower()


def test_may_be_moving_on_entry() -> None:
    for phase in Phase:
        info = PHASE_INFO[phase]
        for previous in (False, True):
            got = may_be_moving_on_entry(phase, previous=previous)
            if info.may_be_moving:
                assert got is True
            elif info.keeps_may_be_moving:
                assert got is previous
            else:
                assert got is False
    with pytest.raises(TypeError):
        may_be_moving_on_entry(Phase.BLOCKED, previous=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        may_be_moving_on_entry("BLOCKED", previous=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def test_snapshot_refuses_not_moving_in_a_moving_phase() -> None:
    for name in sorted(MOVING_PHASES):
        with pytest.raises(ValueError, match="may be moving"):
            snap(phase=Phase[name], may_be_moving=False)
        assert snap(phase=Phase[name], may_be_moving=True).may_be_moving is True
    # Reporting "may be moving" is always allowed: it is the cautious side.
    for phase in Phase:
        assert snap(phase=phase, may_be_moving=True).may_be_moving is True
    with pytest.raises(TypeError):
        snap(may_be_moving=0)


def test_types_frozen() -> None:
    dataclass_types = [obj for obj in vars(T).values()
                       if isinstance(obj, type) and dataclasses.is_dataclass(obj) and obj.__module__ == T.__name__]
    assert {c.__name__ for c in dataclass_types} >= {
        "Banner", "SessionSnapshot", "PhaseInfo", "RestartEvidence", "Connect", "Confirm", "PickBundle", "Arm",
        "Cancel", "AttestPowerCycle", "IStoppedTheRobot", "JobFinishedNoInfo23", "Override", "EnterMeasurement",
    }
    for cls in dataclass_types:
        assert cls.__dataclass_params__.frozen, cls.__name__  # type: ignore[attr-defined]

    samples: list[object] = [
        banner(), PHASE_INFO[Phase.RUNNING], RestartEvidence((34,), False, "fresh"),
        snap(banners=(banner(),), disclosures=(disclosure(),), progress={"line": 3, "cuts": [1, {"a": 2.5}]}),
        Connect(), Cancel(), Confirm("K3", "declared vs measured", "GEOMETRY OK"), PickBundle("job.bundle"),
        Arm("START 1a2b"), AttestPowerCycle("POWERCYCLED"), IStoppedTheRobot("switched off at the wall"),
        JobFinishedNoInfo23("it parked"), Override("sanity_baseline", "index moved after service"),
        EnterMeasurement("footprint.q1_mm", 402.0, "mm", 2.0, "Test Operator"),
    ]
    for obj in samples:
        fields = dataclasses.fields(obj)  # type: ignore[arg-type]
        for field in fields:
            with pytest.raises(dataclasses.FrozenInstanceError):
                setattr(obj, field.name, getattr(obj, field.name))
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError, TypeError)):
            obj.extra = 1  # type: ignore[attr-defined]
        hash(obj)

    s = samples[3]
    assert isinstance(s, SessionSnapshot)
    assert isinstance(s.banners, tuple) and isinstance(s.disclosures, tuple)
    assert isinstance(s.progress, FrozenMap)
    with pytest.raises(TypeError):
        s.progress["line"] = 4  # type: ignore[index]
    assert isinstance(s.progress["cuts"], tuple)
    assert isinstance(s.progress["cuts"][1], FrozenMap)
    # Lists given to the constructor become tuples: the caller's list cannot change the snapshot.
    given_banners = [banner()]
    held = snap(banners=given_banners)
    given_banners.append(banner("OTHER"))
    assert len(held.banners) == 1


def test_types_copy_and_pickle() -> None:
    # Immutable values may be copied, deep-copied or pickled (a worker process, a test double) and stay equal.
    samples: list[object] = [
        snap(phase=Phase.RUNNING, may_be_moving=True, banners=(banner(),), disclosures=(disclosure(),),
             progress={"line": 3, "cuts": [1, {"a": 2.5}]}),
        FrozenMap({"a": [1, {"b": None}]}), PHASE_INFO[Phase.SETUP_LINK_LOST], RestartEvidence((34, 8), True, "fresh"),
        PickBundle("bundles/job"), EnterMeasurement("footprint.q1_mm", 402.0, "mm", 2.0, "Test Operator"), Connect(),
    ]
    for obj in samples:
        assert copy.copy(obj) == obj
        assert copy.deepcopy(obj) == obj
        assert pickle.loads(pickle.dumps(obj)) == obj
    again = pickle.loads(pickle.dumps(samples[0]))
    assert isinstance(again, SessionSnapshot) and isinstance(again.progress, FrozenMap)
    with pytest.raises(TypeError):
        again.progress["line"] = 4  # type: ignore[index]


@settings(deadline=None)
@given(snapshots())
def test_snapshot_json_roundtrip(snapshot: SessionSnapshot) -> None:
    data = snapshot.to_json()
    assert SessionSnapshot.from_json(through_json(data)) == snapshot
    assert SessionSnapshot.from_json(data) == snapshot
    # A frozen snapshot is still a valid argument for its own replace().
    assert dataclasses.replace(snapshot) == snapshot


def test_snapshot_json_shape() -> None:
    s = snap(seq=7, phase=Phase.RUNNING, may_be_moving=True, banners=(banner(),), disclosures=(disclosure(),),
             firmware="01.02.00", firmware_source="INFO;34", progress={"line": 12, "by_line_base": {"0": 12}})
    data = through_json(s.to_json())
    assert data == {
        "seq": 7, "phase": "RUNNING", "may_be_moving": True,
        "banners": [{"id": "ROUTING-ANOMALY", "severity": "red", "text": "routing anomaly", "icon": "link"}],
        "disclosures": [disclosure().to_json()], "firmware": "01.02.00", "firmware_source": "INFO;34",
        "stop_offer": "none", "practice": False, "progress": {"line": 12, "by_line_base": {"0": 12}},
    }


def test_snapshot_from_json_strict() -> None:
    good = snap().to_json()
    SessionSnapshot.from_json(good)
    missing = dict(good)
    del missing["practice"]
    with pytest.raises(ValueError, match="practice"):
        SessionSnapshot.from_json(missing)
    with pytest.raises(ValueError, match="unexpected"):
        SessionSnapshot.from_json(dict(good, extra=1))
    with pytest.raises(ValueError, match="phase"):
        SessionSnapshot.from_json(dict(good, phase="ALARM"))
    with pytest.raises(TypeError):
        SessionSnapshot.from_json(dict(good, seq="0"))
    with pytest.raises(TypeError):
        SessionSnapshot.from_json(dict(good, banners="none"))
    with pytest.raises(TypeError):
        SessionSnapshot.from_json([good])  # type: ignore[arg-type]


def test_snapshot_field_validation() -> None:
    with pytest.raises(ValueError):
        snap(seq=-1)
    with pytest.raises(TypeError):
        snap(seq=True)
    with pytest.raises(TypeError):
        snap(seq=1.0)
    with pytest.raises(TypeError):
        snap(phase="IDLE")
    with pytest.raises(TypeError):
        snap(practice=1)
    with pytest.raises(TypeError):
        snap(banners=(disclosure(),))
    with pytest.raises(TypeError):
        snap(disclosures=(banner(),))
    with pytest.raises(TypeError):
        snap(banners="ROUTING-ANOMALY")
    with pytest.raises(ValueError, match="twice"):
        snap(banners=(banner(), banner(text="another text")))


def test_firmware_and_its_source_go_together() -> None:
    # Every value carries its provenance: a firmware version without its source cannot be shown.
    assert snap(firmware="01.02.00", firmware_source="INFO;34 (connect-time replay)").firmware == "01.02.00"
    with pytest.raises(ValueError, match="source"):
        snap(firmware="01.02.00", firmware_source=None)
    with pytest.raises(ValueError, match="source"):
        snap(firmware=None, firmware_source="INFO;34")
    with pytest.raises(ValueError):
        snap(firmware="  ", firmware_source="INFO;34")
    with pytest.raises(ValueError):
        snap(firmware="01.02.00", firmware_source="")


def test_stop_offer_has_no_unverified_value() -> None:
    assert get_args(StopOffer) == ("none", "verified")
    assert STOP_OFFERS == ("none", "verified")
    abort_capable = dict(firmware="01.02.03", firmware_source="INFO;34")
    assert snap(stop_offer="none").stop_offer == "none"
    assert snap(stop_offer="verified", **abort_capable).stop_offer == "verified"
    for bad in ("unverified", "available", "offered", "pending", "None", "VERIFIED", "verified ", "", None, True, 1):
        with pytest.raises((TypeError, ValueError)):
            snap(stop_offer=bad, **abort_capable)
        with pytest.raises((TypeError, ValueError)):
            dataclasses.replace(snap(**abort_capable), stop_offer=bad)
        with pytest.raises((TypeError, ValueError)):
            SessionSnapshot.from_json(dict(snap(**abort_capable).to_json(), stop_offer=bad))

    class Sneaky(str):
        pass

    with pytest.raises(TypeError):
        snap(stop_offer=Sneaky("verified"), **abort_capable)


def test_verified_stop_needs_firmware_with_abort_work() -> None:
    # GFW 01.02.00 has no ABORT WORK (spec 03 section Stopping a job): no verified stop there, and
    # none on firmware that is unknown or unparseable. That R14 verified it is the producer's duty.
    for firmware in (None, "01.02.00", "GFW 01.02.00", "01.01.09", "garbled"):
        source = None if firmware is None else "INFO;34"
        with pytest.raises(ValueError, match="ABORT WORK"):
            snap(stop_offer="verified", firmware=firmware, firmware_source=source)
    assert snap(stop_offer="verified", firmware="GFW 01.03.01", firmware_source="INFO;34").stop_offer == "verified"


def test_progress_is_frozen_json() -> None:
    s = snap(progress={"line": 12, "deviation_mm": 1.25, "labels": ["a", "b"], "nested": {"k": None}})
    assert s.progress == {"line": 12, "deviation_mm": 1.25, "labels": ("a", "b"), "nested": {"k": None}}
    assert s.to_json()["progress"] == {"line": 12, "deviation_mm": 1.25, "labels": ["a", "b"], "nested": {"k": None}}
    assert type(s.to_json()["progress"]) is dict
    # JSON object keys are strings: an int key would come back as a string and break the round trip.
    with pytest.raises(TypeError, match="key"):
        snap(progress={0: 12})
    for bad in (float("nan"), float("inf"), {1, 2}, b"x", Phase.IDLE, object()):
        with pytest.raises((TypeError, ValueError)):
            snap(progress={"x": bad})
    with pytest.raises(TypeError):
        snap(progress=[1, 2])
    # The recorder writes UTF-8: a lone surrogate is refused here, not when the record is written.
    with pytest.raises(ValueError, match="surrogate"):
        snap(progress={"x": ["ok", "\ud800"]})
    with pytest.raises(ValueError, match="surrogate"):
        snap(progress={"\udc00": 1})
    fm = FrozenMap({"a": 1})
    assert fm == {"a": 1} and fm == FrozenMap({"a": 1}) and hash(fm) == hash(FrozenMap({"a": 1}))
    assert len(fm) == 1 and list(fm) == ["a"] and "a" in fm
    assert repr(fm).startswith("FrozenMap(")


# ---------------------------------------------------------------------------
# Banners
# ---------------------------------------------------------------------------


def test_banner_validation() -> None:
    assert BANNER_SEVERITIES == ("info", "amber", "red", "purple", "blue")
    assert get_args(T.BannerSeverity) == BANNER_SEVERITIES
    assert isinstance(BANNER_ICONS, Mapping) and all(v.strip() for v in BANNER_ICONS.values())
    b = banner()
    assert Banner.from_json(through_json(b.to_json())) == b
    for sev in BANNER_SEVERITIES:
        assert banner(severity=sev).severity == sev
    with pytest.raises(ValueError):
        banner(severity="green")
    with pytest.raises(ValueError, match="icon"):
        banner(icon="smiley")
    with pytest.raises(ValueError):
        banner(text="   ")
    with pytest.raises(ValueError):
        banner(id_="has space")
    with pytest.raises(TypeError):
        banner(text=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Banner.from_json({"id": "X", "severity": "red", "text": "t"})


# ---------------------------------------------------------------------------
# Operator intents
# ---------------------------------------------------------------------------


def test_operator_intent_union() -> None:
    assert INTENT_TYPES == get_args(OperatorIntent)
    assert [c.__name__ for c in INTENT_TYPES] == [
        "Connect", "Confirm", "PickBundle", "Arm", "Cancel", "AttestPowerCycle", "IStoppedTheRobot",
        "JobFinishedNoInfo23", "Override", "EnterMeasurement",
    ]
    assert isinstance(Connect(), OperatorIntent)
    # No software-stop intent before G04 and rung R14 (red-team PHYS-13).
    assert not any("stop" == c.__name__.lower() or "abort" in c.__name__.lower() for c in INTENT_TYPES)


@settings(deadline=None)
@given(intents_st)
def test_intent_json_roundtrip(intent: OperatorIntent) -> None:
    data = intent.to_json()
    assert data["type"] == type(intent).__name__
    assert intent_from_json(through_json(data)) == intent
    hash(intent)


def test_intent_from_json_strict() -> None:
    assert intent_from_json({"type": "Connect"}) == Connect()
    assert intent_from_json({"type": "Arm", "phrase": "START 1a2b"}) == Arm("START 1a2b")
    with pytest.raises(ValueError, match="unknown intent"):
        intent_from_json({"type": "Stop"})
    with pytest.raises(ValueError, match="unknown intent"):
        intent_from_json({"type": "__class__"})
    with pytest.raises(ValueError):
        intent_from_json({"phrase": "START 1a2b"})
    with pytest.raises(ValueError, match="phrase"):
        intent_from_json({"type": "Arm"})
    with pytest.raises(ValueError, match="unexpected"):
        intent_from_json({"type": "Arm", "phrase": "START 1a2b", "force": True})
    with pytest.raises(TypeError):
        intent_from_json({"type": "Arm", "phrase": 1234})
    with pytest.raises(TypeError):
        intent_from_json("Connect")  # type: ignore[arg-type]


def test_confirm_kinds_have_no_second_path() -> None:
    # One way per confirmation: K5 is Arm (server-checked phrase), K6 AttestPowerCycle,
    # K9 EnterMeasurement (design 7.4). Confirm cannot carry them.
    assert set(CONFIRMATION_INTENTS) == {"K%d" % n for n in range(1, 10)}
    assert CONFIRM_KINDS == tuple(k for k, types in CONFIRMATION_INTENTS.items() if Confirm in types)
    assert CONFIRM_KINDS == ("K1", "K2", "K3", "K4", "K7", "K8")
    assert CONFIRMATION_INTENTS["K5"] == (Arm,)
    assert CONFIRMATION_INTENTS["K6"] == (AttestPowerCycle,)
    assert CONFIRMATION_INTENTS["K9"] == (EnterMeasurement,)
    assert Override in CONFIRMATION_INTENTS["K7"]
    for kind in ("K5", "K6", "K9", "K0", "K10", "k1", "", "GEOMETRY OK"):
        with pytest.raises(ValueError, match="kind"):
            Confirm(kind, "shown", "answer")


def test_intent_text_fields() -> None:
    # Typed answers are kept verbatim (the core compares them and records exactly what was typed).
    assert Arm(" start 1a2b ").phrase == " start 1a2b "
    assert Arm("").phrase == ""
    assert Confirm("K1", "shown text", "").answer == ""
    assert AttestPowerCycle("").text == ""
    # What was shown, a reason and an operator's name must say something.
    with pytest.raises(ValueError, match="shown"):
        Confirm("K1", "  ", "ticked")
    with pytest.raises(ValueError, match="reason"):
        Override("sanity_baseline", " ")
    with pytest.raises(ValueError, match="operator"):
        EnterMeasurement("footprint.q1_mm", 402.0, "mm", 2.0, "")
    with pytest.raises(ValueError, match="gate"):
        Override("Sanity Baseline", "reason")
    for cls in (Arm, AttestPowerCycle, IStoppedTheRobot, JobFinishedNoInfo23):
        with pytest.raises(TypeError):
            cls(None)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="surrogate"):
            cls("\ud800")
    assert PickBundle("bundles/job").path == Path("bundles/job")
    assert PickBundle(Path("x")).to_json() == {"type": "PickBundle", "path": "x"}
    with pytest.raises(ValueError):
        PickBundle("")
    with pytest.raises(TypeError):
        PickBundle(3)  # type: ignore[arg-type]


def test_enter_measurement_rules() -> None:
    m = EnterMeasurement("footprint.q1_mm", 402.0, "mm", 2.0, "Test Operator")
    assert (m.fact_id, m.value, m.unit, m.tolerance, m.operator) == ("footprint.q1_mm", 402.0, "mm", 2.0,
                                                                     "Test Operator")
    assert EnterMeasurement("wheel.q1.wheels_in_module", 2, "count", 0, "T").value == 2
    assert EnterMeasurement("stop.kind", "mains_plug", None, None, "T").value == "mains_plug"
    assert EnterMeasurement("wire.back_mm", "unknown", None, None, "T").value == "unknown"
    # A number needs its unit and its tolerance: neither is ever defaulted.
    with pytest.raises(ValueError, match="unit"):
        EnterMeasurement("footprint.q1_mm", 402.0, None, 2.0, "T")
    with pytest.raises(ValueError, match="tolerance"):
        EnterMeasurement("footprint.q1_mm", 402.0, "mm", None, "T")
    # Text, a choice or 'unknown' has neither.
    with pytest.raises(ValueError, match="unit"):
        EnterMeasurement("stop.kind", "mains_plug", "mm", None, "T")
    with pytest.raises(ValueError, match="tolerance"):
        EnterMeasurement("stop.kind", "mains_plug", None, 1.0, "T")
    with pytest.raises(ValueError, match="answer"):
        EnterMeasurement("stop.kind", "  ", None, None, "T")
    with pytest.raises(ValueError, match="count"):
        EnterMeasurement("wheel.q1.wheels_in_module", 2.5, "count", 0, "T")
    for bad_tol in (-0.1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="tolerance"):
            EnterMeasurement("footprint.q1_mm", 402.0, "mm", bad_tol, "T")
    for bad_value in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            EnterMeasurement("footprint.q1_mm", bad_value, "mm", 1.0, "T")
    with pytest.raises(TypeError):
        EnterMeasurement("footprint.q1_mm", True, "mm", 1.0, "T")
    with pytest.raises(TypeError):
        EnterMeasurement("footprint.q1_mm", 402.0, "mm", True, "T")
    with pytest.raises(ValueError, match="unit"):
        EnterMeasurement("footprint.q1_mm", 402.0, "inch", 1.0, "T")
    for bad_id in ("footprint", "Footprint.q1", "footprint.", ".q1", "foot print.q1", ""):
        with pytest.raises(ValueError, match="fact id"):
            EnterMeasurement(bad_id, 1.0, "mm", 1.0, "T")


def test_measurement_vocabulary_matches_field_guide_04() -> None:
    # The typed entry (K9) uses the stable ids of field guide 04 (rung R0b).
    data = json.loads(FIELDS_JSON.read_text(encoding="utf-8"))
    assert set(data["units"]) <= set(MEASUREMENT_UNITS)
    entry_fields = [f for f in data["fields"] if f["measurement_entry"]]
    assert entry_fields, "positive control: field guide 04 marks fields for the typed entry"
    for field in entry_fields:
        if field["kind"] == "number":
            value: object = 1 if field["unit"] == "count" else 1.5
            m = EnterMeasurement(field["id"], value, field["unit"], 0 if field["unit"] == "count" else 0.5, "T")
        else:
            m = EnterMeasurement(field["id"], "unknown", None, None, "T")
        assert m.fact_id == field["id"]


# ---------------------------------------------------------------------------
# Restart evidence
# ---------------------------------------------------------------------------


def test_restart_evidence() -> None:
    assert BASELINE_STATES == ("fresh", "after-complete", "after-interrupt")
    assert get_args(T.BaselineState) == BASELINE_STATES
    ev = RestartEvidence(replay_codes=(34, 8), link_drop_observed=True, baseline_state="after-complete")
    assert RestartEvidence.from_json(through_json(ev.to_json())) == ev
    assert RestartEvidence([34], False, "fresh").replay_codes == (34,)
    assert RestartEvidence((), False, "fresh").replay_codes == ()
    with pytest.raises(ValueError):
        RestartEvidence((34,), False, "after-reset")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RestartEvidence((34,), 1, "fresh")  # type: ignore[arg-type]
    for bad in ((True,), ("34",), (34.0,)):
        with pytest.raises(TypeError):
            RestartEvidence(bad, False, "fresh")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RestartEvidence((-1,), False, "fresh")
    with pytest.raises(ValueError):
        RestartEvidence.from_json({"replay_codes": [34], "link_drop_observed": True})


# ---------------------------------------------------------------------------
# Protocols the UI server serves
# ---------------------------------------------------------------------------


class _Source:
    def __init__(self, items: list[SessionSnapshot]) -> None:
        self.items = items

    def snapshot(self) -> SessionSnapshot:
        return self.items[-1]

    def events(self, after_seq: int) -> Iterator[SessionSnapshot]:
        return iter([s for s in self.items if s.seq > after_seq])


class _Sink:
    def __init__(self) -> None:
        self.got: list[OperatorIntent] = []

    def submit(self, intent: OperatorIntent) -> None:
        self.got.append(intent)


def test_protocols() -> None:
    source = _Source([snap(seq=0), snap(seq=1)])
    assert isinstance(source, SnapshotSource)
    assert [s.seq for s in source.events(0)] == [1]
    sink = _Sink()
    assert isinstance(sink, IntentSink)
    sink.submit(Connect())
    assert sink.got == [Connect()]
    assert not isinstance(sink, SnapshotSource)
    assert not isinstance(object(), IntentSink)


# ---------------------------------------------------------------------------
# Architecture: types only, no I/O
# ---------------------------------------------------------------------------

ALLOWED_IMPORTS = {
    "__future__", "dataclasses", "enum", "math", "re", "collections.abc", "pathlib", "types", "typing",
    "goliath_prep.core.disclosure", "goliath_link.client",
}
IO_CALLS = ("open", "print", "input", "exec", "eval", "__import__", "read_text", "read_bytes", "write_text",
            "write_bytes", "exists", "is_file", "is_dir", "stat", "resolve", "mkdir", "iterdir", "glob", "touch",
            "unlink", "sleep", "time", "now", "connect", "send", "recv")


def _imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import in types.py"
            found.add(node.module or "")
    return found


def test_types_module_does_no_io(astscan: ModuleType) -> None:
    tree = ast.parse(TYPES_PY.read_text(encoding="utf-8"))
    assert _imports(tree) <= ALLOWED_IMPORTS, sorted(_imports(tree) - ALLOWED_IMPORTS)
    for name in IO_CALLS:
        assert astscan.find_calls(str(TYPES_PY), name) == [], name
    # Positive control: the query finds calls that are there.
    assert astscan.find_calls(str(TYPES_PY), "isinstance")


def test_phase_texts_are_plain_text() -> None:
    # Phase texts are shown verbatim in the CLI and the UI: plain printable text, no markup.
    for info in PHASE_INFO.values():
        for text in (info.label, info.banner.text):
            assert not re.search(r"[<>\x00-\x1f]", text), text
