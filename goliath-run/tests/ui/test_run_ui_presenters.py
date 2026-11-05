# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The presenters: what every screen shows, tested directly (unit U02; the project design, sections 8.3-8.5).

The presenters are pure functions from the session snapshot (and, for the
screens, the facts, gates, ledger and profile) to plain data that the page
renders. These tests read that data: the banner model (with invariant I6:
every disclosure in effect is in the banner model), the disclosure chip, the
top bar, the provenance tags and the four screens of this unit (Machine,
Ladder, Records, Settings).
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import tomllib
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from goliath_link.client import FirmwareCapabilities
from goliath_prep.core.disclosure import Disclosure
from goliath_prep.core.params import SOURCES, Param
from goliath_prep.defaults import SPEC_PARAMS
from goliath_prep.machine import MachineProfile
from goliath_run.capabilities import GATE_CATALOGUE, OwnerPolicy, compute_gates
from goliath_run.facts import FACT_BASES, Fact, FactStore
from goliath_run.ledger import Hazard, LedgerState, PendingPowerCycle, restart_verdict
from goliath_run.types import BANNER_ICONS, PHASE_INFO, Banner, Phase, RestartEvidence, SessionSnapshot
from goliath_run.ui import presenters as P

SUPPORT = Path(__file__).resolve().parents[1] / "run_facts_support.py"


def _support() -> ModuleType:
    """goliath-run/tests/run_facts_support.py (the R01 test helpers), loaded from its path."""
    loaded = sys.modules.get("run_facts_support")
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location("run_facts_support", SUPPORT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_facts_support"] = module
    spec.loader.exec_module(module)
    return module


support = _support()

V0102 = FirmwareCapabilities.for_version((1, 2, 0))
NO_POLICY = OwnerPolicy.none()
SEVERITY_RANK = {"info": 0, "blue": 0, "amber": 1, "red": 2, "purple": 2}

FULL_STOP = """\
profile_version = 1
serial = "TEST-SERIAL-0001"

[stop]
kind = "estop_button_on_robot"
location = "red button on the back of the robot"
latching = "yes"
cuts = "controller_power"
cuts_basis = "label"
"""


def snap(phase: Phase = Phase.IDLE, **changes: Any) -> SessionSnapshot:
    """A valid snapshot of ``phase``; ``changes`` replace fields (firmware gets the source INFO;34)."""
    fields: dict[str, Any] = {
        "seq": 1, "phase": phase, "may_be_moving": PHASE_INFO[phase].may_be_moving, "banners": (),
        "disclosures": (), "firmware": None, "firmware_source": None, "stop_offer": "none", "practice": False,
        "progress": None,
    }
    fields.update(changes)
    if fields["firmware"] is not None and fields["firmware_source"] is None:
        fields["firmware_source"] = "INFO;34"
    return SessionSnapshot(**fields)


def disclosure(id: str, severity: str = "amber", text: str | None = None, source: str = "a test") -> Disclosure:
    return Disclosure(id=id, severity=severity, text=text or "%s is in effect" % id, source=source)  # type: ignore


def ids(banners: list[Banner]) -> list[str]:
    return [b.id for b in banners]


def by_id(banners: list[Banner], banner_id: str) -> Banner:
    found = [b for b in banners if b.id == banner_id]
    assert len(found) == 1, (banner_id, ids(banners))
    return found[0]


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_TEXT = st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc")), min_size=1, max_size=40).filter(
    lambda s: s.strip() != "")
_DISCLOSURE_IDS = st.one_of(
    st.sampled_from([
        "DEFAULT_UNMEASURED:r_w", "DEFAULT_UNMEASURED:body_hull_margin", "INFERRED:reach_max", "DEFAULT:job.z_zero",
        "RESTART:attestation-only", "RESTART:info8-unavailable", "FIRMWARE_UNKNOWN", "OVERRIDE:sanity_baseline",
        "VENDOR_GATE:keepalive_during_work", "FACTS_STALE", "R-TABS", "R-KERF:w_b", "R-SUPPORT", "N-DUST",
        "PARTIAL:wheel", "PHASE:RUNNING", "DISCLOSURE:x", "NO_SOFTWARE_STOP", "AIR",
    ]),
    st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._:/;=+-]{0,24}", fullmatch=True),
)
_DISCLOSURES = st.lists(
    st.builds(Disclosure, id=_DISCLOSURE_IDS, severity=st.sampled_from(["info", "amber", "red"]), text=_TEXT,
              source=_TEXT, sticky=st.booleans()),
    max_size=12,
)
_FIRMWARE = st.sampled_from([None, "GFW 01.02.00", "01.02.00", "GFW 01.01.09", "GFW 01.02.03", "garbage"])
_SESSION_BANNERS = st.lists(
    st.builds(Banner, id=st.sampled_from(["ROUTING_ANOMALY", "LINK:lost-at", "SLEEP_INHIBITION_FAILED", "NOTE-1"]),
              severity=st.sampled_from(["info", "amber", "red", "blue"]), text=_TEXT,
              icon=st.sampled_from(sorted(BANNER_ICONS))),
    max_size=3, unique_by=lambda b: b.id,
)


_PHASES = st.sampled_from(list(Phase))


@st.composite
def snapshots(draw: Any, phase: Any = _PHASES) -> SessionSnapshot:
    p = draw(phase)
    firmware = draw(_FIRMWARE)
    stop_offer = draw(st.sampled_from(["none", "verified"])) if firmware == "GFW 01.02.03" else "none"
    session = draw(_SESSION_BANNERS)
    if draw(st.booleans()):
        session = session + [P.AIR_BANNER]
    return snap(p, seq=draw(st.integers(0, 10 ** 6)),
                may_be_moving=PHASE_INFO[p].may_be_moving or draw(st.booleans()),
                banners=tuple(session), disclosures=tuple(draw(_DISCLOSURES)), firmware=firmware,
                stop_offer=stop_offer, practice=draw(st.booleans()))


# ---------------------------------------------------------------------------
# The tests the unit was specified with
# ---------------------------------------------------------------------------


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(snapshots())
def test_every_active_disclosure_in_banner_model(s: SessionSnapshot) -> None:
    # Invariant I6 (design 10.5): every disclosure raised is in the banner model, so a fallback cannot exist
    # without being shown (design 8.5). A banner that covers a disclosure is at least as loud as it.
    model = P.banner_model(s)
    shown = {b.id: b for b in model.banners}
    assert set(model.covers) <= set(shown)
    for d in s.disclosures:
        covering = [bid for bid, covered in model.covers.items() if d in covered]
        assert covering, "disclosure %s is in no banner" % d.id
        for bid in covering:
            assert SEVERITY_RANK[shown[bid].severity] >= SEVERITY_RANK[d.severity], (bid, d)
    # Nothing is covered that is not in effect.
    assert {d for covered in model.covers.values() for d in covered} == set(s.disclosures)
    # The chip lists each one with its text and source, in the snapshot's order, and never dismisses itself.
    chip = P.disclosure_chip(s)
    assert [(i.id, i.text, i.source) for i in chip.items] == [(d.id, d.text, d.source) for d in s.disclosures]
    assert chip.dismissible is False
    # The page gets the same model: each banner says which disclosures it covers.
    view = P.session_view(s)
    covered_ids = sorted(i for b in view["banners"] for i in b["covers"])
    assert covered_ids == sorted(d.id for d in s.disclosures)


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(snapshots())
def test_banners_have_text_and_icon(s: SessionSnapshot) -> None:
    # Design 8.4 and 8.7: colour is never the only signal; every banner has text and an icon.
    out = P.banners(s)
    assert out, "the top of every screen has at least the phase banner"
    assert len(set(ids(out))) == len(out)
    for b in out:
        assert isinstance(b, Banner)
        assert b.text.strip()
        assert b.icon in BANNER_ICONS
    for b in P.session_view(s)["banners"]:
        assert b["text"].strip() and b["icon"] in BANNER_ICONS
        assert b["icon_label"] == BANNER_ICONS[b["icon"]]
        assert b["severity_label"].strip()


@pytest.mark.parametrize("phase", list(Phase))
def test_no_software_stop_banner_on_01_02_00(phase: Phase) -> None:
    # Red-team PHYS-13: no software-stop control on any firmware before rung R14, and the screen says so.
    s = snap(phase, firmware="GFW 01.02.00")
    banner = by_id(P.banners(s), "NO_SOFTWARE_STOP")
    assert banner.severity == "red" and banner.icon == "stop"
    assert banner.text.startswith("NO SOFTWARE STOP ON THIS FIRMWARE. To stop: ")
    bar = P.top_bar(s)
    assert bar["stop"]["text"] == "NO SOFTWARE STOP: PHYSICAL STOP ONLY"
    assert bar["stop"]["severity"] == "red" and bar["stop"]["verified"] is False
    assert bar["stop"]["if_stale"] == "NO SOFTWARE STOP: PHYSICAL STOP ONLY"
    # 01.02.00 cannot even carry a software stop: the snapshot type refuses it.
    with pytest.raises(ValueError, match="ABORT WORK"):
        snap(phase, firmware="GFW 01.02.00", stop_offer="verified")


@pytest.mark.parametrize("firmware", [None, "GFW 01.01.09", "GFW 01.02.03", "garbage"])
def test_no_software_stop_banner_on_every_firmware_until_r14(firmware: str | None) -> None:
    s = snap(Phase.RUNNING, firmware=firmware)
    assert "NO_SOFTWARE_STOP" in ids(P.banners(s))
    assert P.top_bar(s)["stop"]["text"] == "NO SOFTWARE STOP: PHYSICAL STOP ONLY"
    if firmware == "GFW 01.02.03":
        # A firmware that has ABORT WORK still offers no software stop before R14 verifies it, and says why.
        assert "rung R14" in by_id(P.banners(s), "NO_SOFTWARE_STOP").text


def test_verified_software_stop_drops_the_banner_but_not_the_physical_stop() -> None:
    s = snap(Phase.RUNNING, firmware="GFW 01.02.03", stop_offer="verified")
    assert "NO_SOFTWARE_STOP" not in ids(P.banners(s))
    bar = P.top_bar(s)
    assert bar["stop"]["verified"] is True
    assert "R14" in bar["stop"]["text"] and "physical stop" in bar["stop"]["text"]
    # A view that stops updating never keeps offering a software stop.
    assert bar["stop"]["if_stale"] == "NO SOFTWARE STOP: PHYSICAL STOP ONLY"


def test_no_software_stop_banner_names_the_physical_stop_with_its_provenance() -> None:
    profile = MachineProfile.from_mapping(tomllib.loads(FULL_STOP))
    s = snap(Phase.ARMED, firmware="GFW 01.02.00")
    text = by_id(P.banners(s, stop=profile.stop), "NO_SOFTWARE_STOP").text
    assert "the emergency-stop button on the robot" in text
    assert "red button on the back of the robot" in text
    assert "[owner]" in text  # a hand-written machine.toml is the owner's entry, not a measurement
    # Unknown: said so, never a guessed stop.
    unknown = by_id(P.banners(s), "NO_SOFTWARE_STOP").text
    assert "not been recorded" in unknown and "R0b" in unknown
    none_found = P.physical_stop_text({"stop.kind": support.observed("stop.kind", "none_found", basis="robot-measured",
                                                                      rung="R0b")})
    assert none_found.startswith("NO PHYSICAL STOP WAS FOUND")


@pytest.mark.parametrize("phase", list(Phase))
def test_firmware_unknown_banner(phase: Phase) -> None:
    s = snap(phase)
    banner = by_id(P.banners(s), "FIRMWARE_UNKNOWN")
    assert banner.severity == "amber" and banner.text.startswith("FIRMWARE UNKNOWN: read-only")
    bar = P.top_bar(s)
    assert bar["firmware"]["text"] == "FIRMWARE UNKNOWN" and bar["firmware"]["known"] is False
    # A version the robot reported is shown with its source, and no unknown banner.
    known = snap(phase, firmware="GFW 01.02.00", firmware_source="INFO;34")
    assert "FIRMWARE_UNKNOWN" not in ids(P.banners(known))
    assert P.top_bar(known)["firmware"] == {"text": "Robot: GFW 01.02.00 (from INFO;34)", "known": True,
                                            "version": "01.02.00", "source": "INFO;34", "severity": "info"}


def test_firmware_without_a_version_number_is_unknown() -> None:
    s = snap(Phase.IDENTIFY, firmware="garbage", firmware_source="INFO;34")
    banner = by_id(P.banners(s), "FIRMWARE_UNKNOWN")
    assert "'garbage'" in banner.text and "INFO;34" in banner.text
    bar = P.top_bar(s)
    assert bar["firmware"]["known"] is False and bar["firmware"]["version"] is None
    assert bar["firmware"]["text"].startswith("FIRMWARE UNKNOWN")


def test_firmware_unknown_disclosure_of_the_gate_set_is_covered(tmp_path: Path) -> None:
    gates = compute_gates(FirmwareCapabilities.for_version(None), FactStore.open(tmp_path, support.SIM_SERIAL, None),
                          NO_POLICY)
    s = snap(Phase.LEDGER_CHECK, disclosures=gates.disclosures)
    model = P.banner_model(s)
    fw = [d for d in s.disclosures if d.id == "FIRMWARE_UNKNOWN"]
    assert fw and model.covers["FIRMWARE_UNKNOWN"] == tuple(fw)
    # With a version known, the same disclosure would contradict it: shown on its own, never merged away.
    contradicting = snap(Phase.LEDGER_CHECK, firmware="GFW 01.02.00", disclosures=tuple(fw))
    own = [b for b in P.banners(contradicting) if b.id.startswith("DISCLOSURE:FIRMWARE_UNKNOWN")]
    assert len(own) == 1 and own[0].text == fw[0].text


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(snapshots(phase=st.just(Phase.MONITORING_UNVERIFIED)))
def test_monitoring_unverified_banner_never_reads_running(s: SessionSnapshot) -> None:
    # Red-team FW-03: after a reconnect while a job may run, the screen never says RUNNING until a position
    # frame on S3 has shown the routing.
    bar = P.top_bar(s)
    assert bar["state"]["label"] == "MONITORING UNVERIFIED"
    assert bar["state"]["may_be_moving"] is True and "MAY BE MOVING" in bar["state"]["text"]
    assert not re.search(r"\bRUNNING\b", bar["state"]["text"])
    out = P.banners(s)
    phase = by_id(out, "PHASE:MONITORING_UNVERIFIED")
    assert phase.text == "MONITORING UNVERIFIED: safety errors may not reach this laptop"
    assert phase.severity == "red"
    assert not [b for b in out if b.id.startswith("PHASE:") and b.id != "PHASE:MONITORING_UNVERIFIED"]
    derived = [b for b in out if b.id in ("NO_SOFTWARE_STOP", "FIRMWARE_UNKNOWN", "PRACTICE") or
               b.id.startswith("PHASE:")]
    for b in derived:
        assert not re.search(r"\bRUNNING\b", b.text), b
    assert not re.search(r"\bRUNNING\b", P.session_view(s)["title"])


def test_session_banner_cannot_claim_a_phase() -> None:
    # The phase banner is the presenters' to add (goliath_run.types): a session banner that claims
    # PHASE:RUNNING in MONITORING_UNVERIFIED is refused, never shown as if it were the state.
    claim = Banner(id="PHASE:RUNNING", severity="red", text="RUNNING: the robot is working.", icon="motion")
    s = snap(Phase.MONITORING_UNVERIFIED, banners=(claim,))
    with pytest.raises(P.PresenterError, match="PHASE:RUNNING"):
        P.banners(s)
    for reserved in ("NO_SOFTWARE_STOP", "FIRMWARE_UNKNOWN", "PRACTICE", "DISCLOSURE:x", "NOTICES",
                     "UNMEASURED_MACHINE_PROFILE", "RELAXED_SAFETY", "RESTART_ATTESTATION_ONLY"):
        fake = Banner(id=reserved, severity="info", text="all is well", icon="info")
        with pytest.raises(P.PresenterError, match=re.escape(reserved)):
            P.banners(snap(Phase.IDLE, banners=(fake,)))


def test_air_banner_wording() -> None:
    # Red-team PHYS-09: the AIR banner describes the robot's own calibration too, not only our program.
    assert P.AIR_TEXT == ("AIR: the program keeps Z >= +5 mm and has no M10; the robot's own Z calibration may "
                          "still lower and spin the bit")
    assert P.AIR_BANNER == Banner(id="AIR", severity="blue", text=P.AIR_TEXT, icon="air")
    s = snap(Phase.RUNNING, firmware="GFW 01.02.00", banners=(P.AIR_BANNER,))
    assert by_id(P.banners(s), "AIR") == P.AIR_BANNER
    # The old wording, or any other, under the AIR id is refused: it would understate the hazard.
    old = Banner(id="AIR", severity="blue", text="AIR: the program keeps Z >= +5 mm and has no M10", icon="air")
    with pytest.raises(P.PresenterError, match="AIR"):
        P.banners(snap(Phase.RUNNING, banners=(old,)))
    with pytest.raises(P.PresenterError, match="AIR"):
        P.banners(snap(Phase.RUNNING, banners=(replace(P.AIR_BANNER, severity="info"),)))


# ---------------------------------------------------------------------------
# The rest of the red-team banner model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phase", [Phase.UPLOAD_HAZARD, Phase.SILENT_AFTER_SEND])
def test_upload_mode_banner_also_for_silent_after_send(phase: Phase) -> None:
    out = P.banners(snap(phase, firmware="GFW 01.02.00"))
    banner = by_id(out, "PHASE:%s" % phase.name)
    assert banner.text.startswith("ROBOT MAY BE IN UPLOAD MODE: power-cycle before anything else (stay connected, "
                                  "then switch the controller off)")
    assert banner.severity == "red" and banner.icon == "upload"
    assert out[0] == banner  # the state comes first


def test_setup_link_lost_banner() -> None:
    s = snap(Phase.SETUP_LINK_LOST, firmware="GFW 01.02.00")
    banner = by_id(P.banners(s), "PHASE:SETUP_LINK_LOST")
    assert banner.text.startswith("SETUP LINK LOST: the robot may be moving (panel detection); do not approach")
    assert P.top_bar(s)["state"]["text"] == "SETUP LINK LOST - MAY BE MOVING"


def test_restart_verified_by_attestation_only_banner() -> None:
    verdict = restart_verdict("after-job", "after-complete", RestartEvidence((), False, "after-complete"),
                              attestation="POWERCYCLED", baselines=None)
    assert verdict.cleared and verdict.route == "attestation-only"
    s = snap(Phase.IDLE, disclosures=verdict.disclosures)
    model = P.banner_model(s)
    banner = by_id(list(model.banners), "RESTART_ATTESTATION_ONLY")
    assert banner.text.startswith("RESTART VERIFIED BY ATTESTATION ONLY: ")
    assert "no replay or link drop was observed" in banner.text
    assert banner.severity == "amber" and banner.icon == "power"
    assert model.covers["RESTART_ATTESTATION_ONLY"] == verdict.disclosures


def test_unmeasured_machine_profile_is_one_banner_naming_each_value() -> None:
    unmeasured = tuple(d for p in SPEC_PARAMS.values() if p.source == "DEFAULT_UNMEASURED" for d in p.disclosures)
    assert len(unmeasured) >= 4
    s = snap(Phase.READY, firmware="GFW 01.02.00", disclosures=unmeasured)
    banner = by_id(P.banners(s), "UNMEASURED_MACHINE_PROFILE")
    assert banner.severity == "amber" and banner.text.startswith("UNMEASURED MACHINE PROFILE: ")
    for d in unmeasured:
        assert d.id.split(":", 1)[1] in banner.text
    assert "r_w" in banner.text


def test_relaxations_are_one_red_or_amber_banner_with_their_text() -> None:
    relax = (disclosure("R-TABS", "info", "R-TABS: t_hold lowered to 1.5 mm by the owner after rung R7"),
             disclosure("R-KERF:w_b", "amber", "R-KERF: wheels may cross kerfs up to 6 mm"))
    banner = by_id(P.banners(snap(Phase.READY, disclosures=relax)), "RELAXED_SAFETY")
    assert banner.severity == "amber"  # a relaxation is never shown as a mere note
    assert banner.text.startswith("RELAXED SAFETY: ")
    assert "t_hold lowered" in banner.text and "kerfs up to 6 mm" in banner.text


def test_info_disclosures_are_one_notices_banner_and_listed_in_the_chip() -> None:
    inferred = tuple(d for p in SPEC_PARAMS.values() if p.source == "INFERRED" for d in p.disclosures)
    s = snap(Phase.READY, disclosures=inferred)
    model = P.banner_model(s)
    banner = by_id(list(model.banners), "NOTICES")
    assert banner.severity == "info" and str(len(inferred)) in banner.text
    assert model.covers["NOTICES"] == inferred
    chip = P.disclosure_chip(s)
    assert chip.unverified_defaults == len(inferred) and chip.fallbacks == 0
    assert chip.label == "0 fallbacks / %d unverified defaults in effect" % len(inferred)


def test_red_and_amber_disclosures_get_their_own_banner() -> None:
    override = disclosure("OVERRIDE:sanity_baseline", "red", "sanity_baseline: a difference is OVERRIDDEN by X")
    vendor = disclosure("VENDOR_GATE:keepalive_during_work", "amber")
    same_id = disclosure("VENDOR_GATE:keepalive_during_work", "amber", "another text from another source",
                         source="elsewhere")
    s = snap(Phase.READY, disclosures=(override, vendor, same_id))
    model = P.banner_model(s)
    own = [b for b in model.banners if b.id.startswith("DISCLOSURE:")]
    assert len(own) == 3
    assert {b.text for b in own} == {override.text, vendor.text, same_id.text}
    assert [b.severity for b in own] == ["red", "amber", "amber"]


def test_practice_banner_first_and_in_the_title() -> None:
    s = snap(Phase.RUNNING, firmware="GFW 01.02.00", practice=True)
    out = P.banners(s)
    assert out[0] == Banner(id="PRACTICE", severity="purple", text="PRACTICE: SIMULATOR, NOT A ROBOT",
                            icon="practice")
    view = P.session_view(s)
    assert view["title"].startswith("PRACTICE: SIMULATOR, NOT A ROBOT")
    assert view["top_bar"]["practice"] is True


def test_banner_order_is_state_then_severity() -> None:
    s = snap(Phase.RUNNING, firmware="GFW 01.02.00", banners=(P.AIR_BANNER,),
             disclosures=(disclosure("INFERRED:reach_max", "info"), disclosure("FACTS_STALE", "red")))
    order = ids(P.banners(s))
    assert order[0] == "PHASE:RUNNING" and order[1] == "NO_SOFTWARE_STOP"
    severities = [b.severity for b in P.banners(s)]
    rank = {"purple": 0, "red": 1, "amber": 2, "blue": 3, "info": 4}
    assert severities == sorted(severities, key=rank.__getitem__)


def test_top_bar_state_and_may_be_moving() -> None:
    idle = P.top_bar(snap(Phase.IDLE))
    assert idle["state"] == {"label": "IDLE", "phase": "IDLE", "may_be_moving": False, "text": "IDLE",
                             "severity": "info", "icon": "info", "if_stale": P.STALE_STATE_TEXT}
    # BLOCKED keeps the flag of the phase it came from: shown, in red.
    blocked = P.top_bar(snap(Phase.BLOCKED, may_be_moving=True))
    assert blocked["state"]["text"] == "BLOCKED - MAY BE MOVING" and blocked["state"]["severity"] == "red"
    assert "MAY BE MOVING" in P.STALE_STATE_TEXT


def test_chip_counts_fallbacks_and_unverified_defaults() -> None:
    d = (disclosure("DEFAULT_UNMEASURED:r_w"), disclosure("INFERRED:reach_max", "info"),
         disclosure("DEFAULT:job.z_zero", "info"), disclosure("OVERRIDE:sanity_baseline", "red"),
         disclosure("RESTART:attestation-only"))
    chip = P.disclosure_chip(snap(Phase.IDLE, disclosures=d))
    assert (chip.fallbacks, chip.unverified_defaults) == (2, 3)
    assert chip.label == "2 fallbacks / 3 unverified defaults in effect"
    assert chip.severity == "red"
    assert [i.kind for i in chip.items] == ["unverified default"] * 3 + ["fallback"] * 2
    empty = P.disclosure_chip(snap(Phase.IDLE))
    assert empty.items == () and empty.label == "No fallbacks or unverified defaults in effect"
    with pytest.raises(ValueError, match="dismiss"):
        replace(chip, dismissible=True)
    assert json.loads(json.dumps(chip.to_json()))["dismissible"] is False


# ---------------------------------------------------------------------------
# Provenance tags
# ---------------------------------------------------------------------------


def test_every_param_source_has_a_tag() -> None:
    expected = {"SPEC": "[spec]", "DEFAULT_UNMEASURED": "[default, unverified]", "INFERRED": "[INFERRED]",
                "OWNER_PROFILE": "[owner]"}
    for source in SOURCES:
        tag = P.SOURCE_TAGS[source]
        assert re.fullmatch(r"\[[^\[\]]+\]", tag), (source, tag)
        if source in expected:
            assert tag == expected[source]
    for name, param in SPEC_PARAMS.items():
        assert P.param_tag(param) == P.SOURCE_TAGS[param.source], name


def test_derived_values_carry_the_doubt_of_their_inputs() -> None:
    r_w = SPEC_PARAMS["r_w"]
    m_w = SPEC_PARAMS["m_w"]
    rho = Param.derived(55.0, "mm", "rho = r_w + m_w", (r_w, m_w))
    assert P.param_tag(rho) == "[derived, unverified]"
    exact = Param.derived(613.0, "mm", "r1 + r2", (SPEC_PARAMS["support_r1"], SPEC_PARAMS["support_r2"]))
    assert P.param_tag(exact) == "[derived]"


def test_every_fact_basis_has_a_tag() -> None:
    for basis in FACT_BASES:
        fact = support.observed("footprint.q1_mm", 402.0, basis=basis, recorded_at="2026-11-04T10:00:00Z")
        tag = P.fact_tag(fact)
        assert re.fullmatch(r"\[[^\[\]]+\]", tag), (basis, tag)
    assert P.fact_tag(support.observed("x.y", 1, basis="observed")) == "[robot 2026-11-04]"
    assert P.fact_tag(support.observed("x.y", 1, basis="robot-measured")) == "[robot 2026-11-04, measured]"
    assert P.fact_tag(support.observed("x.y", 1, basis="default")) == "[default, unverified]"
    stale = P.shown_fact(support.observed("x.y", 1), stale="firmware changed")
    assert "STALE" in stale.tag and stale.known is True


def test_unknown_is_shown_as_unknown() -> None:
    u = P.shown_unknown("rung R0b measures it")
    assert u.text == "unknown" and u.tag == "[unknown]" and u.known is False
    assert P.shown_param(SPEC_PARAMS["r_w"]).text == "40 mm"
    assert P.shown_param(SPEC_PARAMS["wire_attachment"]).text == "body-hull"


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


def _texts(view: object) -> str:
    return json.dumps(view, ensure_ascii=False, allow_nan=False)


def _section(view: dict, section_id: str) -> dict:
    found = [s for s in view["sections"] if s["id"] == section_id]
    assert len(found) == 1, (section_id, [s["id"] for s in view["sections"]])
    return found[0]


def test_machine_view_without_session_data_shows_unknowns() -> None:
    view = P.machine_view(snap(Phase.IDLE))
    assert view["screen"] == "machine" and view["seq"] == 1
    json.loads(_texts(view))
    identity = _section(view, "identity")
    assert identity["rows"][0]["value"]["text"] == "FIRMWARE UNKNOWN"
    gates = _section(view, "gates")
    assert gates["kind"] == "notice" and "closed" in gates["text"]
    for sid in ("connections", "ledger"):
        assert "unknown" in _texts(_section(view, sid)).lower()
    stop = _section(view, "stop")
    assert "NO SOFTWARE STOP" in _texts(stop)
    links = _section(view, "connections")
    assert [r["cells"][0] for r in links["rows"]] == ["S1", "S2", "S3"]
    assert all(r["cells"][1] == "unknown" for r in links["rows"])


def test_machine_view_gate_table_from_a_real_gate_set(tmp_path: Path) -> None:
    store = support.r1_store(tmp_path, "gfw-01.02.00")
    gates = compute_gates(V0102, store, NO_POLICY)
    s = snap(Phase.READY, firmware="GFW 01.02.00", disclosures=gates.disclosures)
    view = P.machine_view(s, gates=gates)
    table = _section(view, "gates")
    assert table["kind"] == "table"
    rows = {r["cells"][0]: r for r in table["rows"]}
    assert set(rows) == set(GATE_CATALOGUE)
    assert rows["jobs_allowed"]["cells"][1] == "YES"
    assert rows["software_stop"]["cells"][1] == "NO" and rows["software_stop"]["cells"][5] == "R14"
    assert rows["upload_confirm_mode"]["cells"][1] == "UNCERTAIN (treated as NO)"
    assert rows["software_stop"]["severity"] != "info"
    identity = _texts(_section(view, "identity"))
    assert "B1" in identity and "matches" in identity


def test_machine_view_flags_a_firmware_that_disagrees_with_the_gates(tmp_path: Path) -> None:
    gates = compute_gates(V0102, support.r1_store(tmp_path, "gfw-01.02.00"), NO_POLICY)
    view = P.machine_view(snap(Phase.READY, firmware="GFW 01.02.03"), gates=gates)
    problems = [s for s in view["sections"] if s["kind"] == "notice" and s.get("severity") == "red"]
    assert problems and "01.02.00" in problems[0]["text"] and "01.02.03" in problems[0]["text"]


def test_machine_view_ledger_and_links() -> None:
    ledger = replace(LedgerState.empty("10.0.0.1", "TEST-SERIAL-0001"),
                     hazards=(Hazard("silent-after-send", "no reply to INFO", "2026-11-04T10:00:00.000000Z"),),
                     pending_power_cycle=(PendingPowerCycle("job finished", "2026-11-04T10:00:00.000000Z"),))
    links = (P.LinkStatus("S1", True, 0.4), P.LinkStatus("S2", True, 1.25), P.LinkStatus("S3", False, None))
    view = P.machine_view(snap(Phase.POWER_CYCLE_REQUIRED, firmware="GFW 01.02.00"), ledger=ledger, links=links)
    led = _section(view, "ledger")
    text = _texts(led)
    assert "upload hazard" in text and "power cycle required" in text
    rows = _section(view, "connections")["rows"]
    assert rows[0]["cells"][1:] == ["connected", "0.4 s"]
    assert rows[2]["cells"][1:] == ["down", "unknown"] and rows[2]["severity"] == "red"
    clean = P.machine_view(snap(Phase.IDLE), ledger=LedgerState.empty("10.0.0.1"))
    assert "nothing pending" in _texts(_section(clean, "ledger")).lower()


def test_ladder_view_lists_every_rung_r0_to_r15() -> None:
    view = P.ladder_view(None, None)
    rung_ids = [r.id for r in P.LADDER]
    for expected in ("R0a", "R0b", "R1", "R1b", "R2", "R3", "R3b", "R3c", "R4", "R5", "R6", "R7", "R8k", "R8",
                     "R9", "R10", "R11", "R12", "R13", "R14", "R15"):
        assert expected in rung_ids
    assert len(set(rung_ids)) == len(rung_ids)
    # Every gate's unlocking rung is on the ladder, and the rung lists the gate.
    for name, spec in GATE_CATALOGUE.items():
        if spec.unlock_rung is not None:
            assert spec.unlock_rung in rung_ids, name
    groups = {g["id"]: g for g in view["sections"] if g["kind"] == "group"}
    assert set(groups) == {"rung-%s" % r for r in rung_ids}
    for rung in P.LADDER:
        assert rung.go, rung.id
        g = groups["rung-%s" % rung.id]
        status = g["sections"][0]
        assert status["kind"] == "notice"
        # Without a fact store, nothing is claimed: no rung reads as done.
        assert status["text"].startswith("UNKNOWN"), status
        opened = [n for n, s in GATE_CATALOGUE.items() if s.unlock_rung == rung.id]
        if opened:
            assert set(opened) <= set(_texts(g).split('"')), (rung.id, opened)


def test_ladder_view_with_r1_facts(tmp_path: Path) -> None:
    store = support.r1_store(tmp_path, "gfw-01.02.00")
    gates = compute_gates(V0102, store, NO_POLICY)
    view = P.ladder_view(store, gates)
    groups = {g["id"]: g for g in view["sections"] if g["kind"] == "group"}
    r1 = groups["rung-R1"]
    status = r1["sections"][0]
    assert status["text"].startswith("GATES OPEN"), status
    facts = [s for s in r1["sections"] if s["id"] == "rung-R1-facts"][0]
    assert any(row["cells"][0] == "firmware.version" for row in facts["rows"])
    r2 = groups["rung-R2"]["sections"][0]
    assert r2["text"].startswith("NOT RUN"), r2
    for g in groups.values():
        assert not re.search(r"\b(DONE|GO|PASSED)\b", g["sections"][0]["text"]), g["sections"][0]


def test_ladder_view_flags_stale_facts(tmp_path: Path) -> None:
    store = support.r1_store(tmp_path, "gfw-01.02.00")
    later = FactStore.open(tmp_path, store.serial, "01.02.03")
    assert later.stale()
    gates = compute_gates(FirmwareCapabilities.for_version((1, 2, 3)), later, NO_POLICY)
    view = P.ladder_view(later, gates)
    r1 = [g for g in view["sections"] if g["id"] == "rung-R1"][0]
    assert r1["sections"][0]["text"].startswith("REPEAT"), r1["sections"][0]
    assert r1["sections"][0]["severity"] == "amber"


def test_records_view() -> None:
    assert "no record folder" in _texts(P.records_view(None)).lower()
    assert "no sessions" in _texts(P.records_view(())).lower()
    summary = {
        "schema_version": 1, "job_id": "job-1", "outcome": "COMPLETED", "reasons": ["INFO;23"],
        "created_at": "2026-11-04T10:00:00Z", "finished_at": "2026-11-04T10:30:00Z",
        "policy": {"max_session_mb": 4096, "keep_sessions": 20, "source": "test"},
        "wire": {"bytes_recorded": 0, "bytes_dropped": 0, "per_connection": {}, "cap_reached": False},
        "disclosures": [disclosure("RECORDER:policy", "info").to_json()],
    }
    done = P.RecordSummary.from_summary("20261104T100000Z-job-1", summary)
    unfinished = P.RecordSummary.from_summary("20261104T110000Z-job-2", None)
    broken = P.RecordSummary.from_summary("20261104T120000Z-job-3", {"schema_version": 9})
    view = P.records_view((done, unfinished, broken))
    table = _section(view, "sessions")
    rows = table["rows"]
    assert rows[0]["cells"][2] == "COMPLETED"
    assert rows[1]["cells"][2].startswith("NOT FINISHED") and rows[1]["severity"] == "amber"
    assert rows[2]["cells"][2].startswith("UNREADABLE") and rows[2]["severity"] == "red"
    assert "diag export" in _texts(view)


def test_settings_view_every_parameter_with_its_provenance() -> None:
    view = P.settings_view()
    table = _section(view, "parameters")
    rows = {r["cells"][0]: r for r in table["rows"]}
    assert set(rows) == set(SPEC_PARAMS)
    for name, param in SPEC_PARAMS.items():
        assert rows[name]["cells"][2] == P.param_tag(param), name
    assert rows["r_w"]["cells"][1] == "40 mm" and rows["r_w"]["severity"] == "amber"
    profile = _section(view, "profile")
    assert profile["kind"] == "notice" and profile["severity"] == "amber"
    assert "unknown" in _texts(_section(view, "network")).lower() or "10.0.0.1" in _texts(_section(view, "network"))


def test_settings_view_with_a_profile() -> None:
    profile = MachineProfile.from_mapping(tomllib.loads(FULL_STOP))
    view = P.settings_view(profile=profile)
    readings = _section(view, "profile")
    assert readings["kind"] == "table"
    rows = {r["cells"][0]: r for r in readings["rows"]}
    assert rows["stop.kind"]["cells"][1] == "estop_button_on_robot"
    assert rows["footprint.q1_mm"]["cells"][1] == "unknown" and rows["footprint.q1_mm"]["cells"][2] == "[unknown]"
    params = {r["cells"][0]: r for r in _section(view, "parameters")["rows"]}
    assert params["rho"]["cells"][2] == "[derived, unverified]"


def test_every_view_is_plain_json(tmp_path: Path) -> None:
    store = support.r1_store(tmp_path, "gfw-01.02.00")
    gates = compute_gates(V0102, store, NO_POLICY)
    s = snap(Phase.READY, firmware="GFW 01.02.00", disclosures=gates.disclosures)
    for view in (P.session_view(s), P.machine_view(s, gates=gates), P.ladder_view(store, gates),
                 P.records_view(()), P.settings_view()):
        assert json.loads(_texts(view)) == view


def test_default_screens_cover_the_unit_screens() -> None:
    class Source:
        def snapshot(self) -> SessionSnapshot:
            return snap(Phase.IDLE)

        def events(self, after_seq: int) -> Any:
            return iter(())

    screens = P.default_screens(Source())
    assert tuple(screens) == P.SCREENS == ("machine", "ladder", "records", "settings")
    for name, build in screens.items():
        view = build()
        assert view["screen"] == name
        json.loads(_texts(view))


def test_presenters_module_is_pure() -> None:
    # Pure functions of their inputs: no clock, no files, no network, no threads.
    import ast

    path = Path(P.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {(node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not imported & {"time", "datetime", "socket", "threading", "os", "subprocess", "random", "io"}, imported
    for name in ("open", "print", "input"):
        calls = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == name]
        assert calls == [], (name, calls)


def test_fact_values_and_unknown_ids_shown_verbatim() -> None:
    fact = Fact(id="detection.park_pose.x_mm", value=612.5, basis="robot-measured", unknown_id="U-44", rung="R2",
                evidence=("tape",), serial="TEST-SERIAL-0001", firmware=None, recorded_at="2026-11-05T09:00:00Z")
    shown = P.shown_fact(fact)
    assert shown.text == "612.5" and shown.tag == "[robot 2026-11-05, measured]"
    assert "R2" in shown.ref and "detection.park_pose.x_mm" in shown.ref
