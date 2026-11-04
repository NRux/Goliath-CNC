# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Tri-state feature gates from firmware capabilities, facts and owner policy (unit R01; design 3.1, 3.2, 4.15.1)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from goliath_link.client import FirmwareCapabilities
from goliath_prep.core.canon import canonical_sha256
from goliath_prep.core.types import ClosedGateView, GateView
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from run_facts_support import AT, OPERATOR, SIM_SERIAL, TEST_SERIAL, observed, r1_store, report_path

from goliath_run.capabilities import (
    GATE_CATALOGUE,
    FeatureGate,
    GateClosedError,
    GateOverride,
    GateOverrideRefused,
    GateSet,
    OwnerPolicy,
    UnknownGateError,
    compute_gates,
    load_gate_snapshot,
)
from goliath_run.facts import FactStore, read_probe_report
from goliath_run.types import Override

REPO = Path(__file__).resolve().parents[2]
V0102 = FirmwareCapabilities.for_version((1, 2, 0))
V0103 = FirmwareCapabilities.for_version((1, 2, 3))
NO_POLICY = OwnerPolicy.none()

DESIGN_GATES = {
    "jobs_allowed", "software_stop", "keepalive_during_work", "upload_confirm_mode",
    "second_job_without_power_cycle", "panel_vertices_usable", "heading_map_verified", "rotation_model",
    "rotate_in_place_ok", "z_semantics_verified", "spindle_verified", "drop_dwells", "first_cut_done",
    "arcs_emit", "through_cuts_allowed", "tabs_hold", "pockets_allowed", "kerf_crossing", "near_edge_allowed",
    "max_program_bytes", "m11_emit", "jog_allowed", "series_allowed", "sanity_baseline",
    "stop_verified_on_motion", "start_pose_known", "frames_end_upload", "post_job_motion_known",
    "wire_attachment_measured", "link_keepalive_ok",
}


def _gates(store: FactStore, caps: FirmwareCapabilities = V0102, policy: OwnerPolicy = NO_POLICY,
           overrides: tuple = ()) -> GateSet:
    return compute_gates(caps, store, policy, overrides)


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


def test_catalogue_is_the_design_table() -> None:
    assert set(GATE_CATALOGUE) == DESIGN_GATES
    for name, spec in GATE_CATALOGUE.items():
        assert spec.closed_reason.strip(), name
        assert spec.unlock_rung is None or re.fullmatch(r"R\d{1,2}[a-z]?", spec.unlock_rung), name
    # Only the sanity-baseline check takes a typed override (design 3.1, 3.2).
    assert {n for n, s in GATE_CATALOGUE.items() if s.overridable} == {"sanity_baseline"}


def test_catalogue_covers_the_safety_model_gates() -> None:
    text = (REPO / "docs" / "prep" / "safety-model.md").read_text(encoding="utf-8")
    table = text.split("**Gates.**", 1)[1].split("**Rungs.**", 1)[0]
    named = set(re.findall(r"^\| `([a-z_0-9]+)` \|", table, flags=re.M))
    assert named and named <= set(GATE_CATALOGUE)
    for name in named:
        rung = re.search(r"^\| `%s` \|.*\| (R\w+) \|$" % name, table, flags=re.M)
        assert rung is not None and GATE_CATALOGUE[name].unlock_rung == rung.group(1), name


# ---------------------------------------------------------------------------
# No unknown is read as "yes"
# ---------------------------------------------------------------------------


def test_none_never_allowed(tmp_path: Path) -> None:
    gate = FeatureGate(name="upload_confirm_mode", allowed=None, basis="vendor-gate",
                       reason="the vendor app did not wait for INFO;8 below 01.02.03", evidence=(),
                       overridable=False, unlock_rung="R3")
    assert gate.is_open is False
    with pytest.raises(ValueError):
        FeatureGate(name="x", allowed=None, basis="fact", reason="r", evidence=(), overridable=False,
                    unlock_rung=None, value="a value on a gate that is not open")
    with pytest.raises(TypeError):
        FeatureGate(name="x", allowed=1, basis="fact", reason="r", evidence=(), overridable=False,  # type: ignore
                    unlock_rung=None)

    # Firmware unknown: jobs_allowed is uncertain, and uncertain refuses.
    store = FactStore.open(tmp_path, SIM_SERIAL, None)
    gates = compute_gates(FirmwareCapabilities.for_version(None), store, NO_POLICY)
    assert gates.allowed("jobs_allowed") is None
    with pytest.raises(GateClosedError, match="jobs_allowed") as info:
        gates.require("jobs_allowed")
    assert "R1" in str(info.value)
    assert any("UNCERTAIN" in line and line.startswith("jobs_allowed") for line in gates.explain())
    view = gates.as_prep_view()
    assert view.allowed("jobs_allowed") is None
    # Every gate that is not exactly True refuses, whatever its reason.
    for name in GATE_CATALOGUE:
        if gates.allowed(name) is not True:
            with pytest.raises(GateClosedError):
                gates.require(name)
            assert gates.value(name) is None
    with pytest.raises(UnknownGateError):
        gates.require("no_such_gate")
    assert gates.allowed("no_such_gate") is False
    assert view.allowed("no_such_gate") is False


_UNKNOWNISH = st.sampled_from(["unknown", "", "no", "maybe", False, 0, -1, [], {}, "unverified", {"value": "unknown"}])


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(values=st.dictionaries(st.sampled_from(sorted({f for s in GATE_CATALOGUE.values() for f in s.facts})),
                              _UNKNOWNISH, min_size=1, max_size=12),
       basis=st.sampled_from(["observed", "robot-measured", "owner", "default", "vendor-gate"]))
def test_unknown_values_never_open_a_gate(tmp_path_factory: pytest.TempPathFactory, values: dict,
                                          basis: str) -> None:
    root = tmp_path_factory.mktemp("store")
    store = FactStore.open(root, SIM_SERIAL, "01.02.00")
    firmware = None if basis == "robot-measured" else "01.02.00"
    store.record_many([observed(fid, v, basis=basis, firmware=firmware, rung="R3") for fid, v in values.items()])
    gates = _gates(store)
    for name, spec in GATE_CATALOGUE.items():
        # frames.stream_outside_work = False is the one meaningful False, and it opens
        # frames_end_upload only together with R1's frames.at_idle, which no value here gives.
        if spec.facts:
            assert gates.allowed(name) is not True, (name, values)


def test_owner_and_default_facts_never_open_a_gate(tmp_path: Path) -> None:
    store = FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")
    store.record(observed("panel.vertex_frame", "identity", basis="owner"))
    store.record(observed("rotation.model", "in-place-shortest", basis="default", rung="R3"))
    store.record(observed("spindle.m10_m05", True, rung=None))  # no rung: not through a rung's go criteria
    gates = _gates(store)
    for name in ("panel_vertices_usable", "rotation_model", "spindle_verified"):
        assert gates.allowed(name) is False, name


def test_fact_gates_open_on_observed_rung_facts(tmp_path: Path) -> None:
    store = r1_store(tmp_path, "gfw-01.02.00")  # frames_end_upload needs R1's frames.at_idle too
    store.record(observed("panel.vertex_frame", "identity", rung="R2"))
    store.record(observed("rotation.model", "in-place-shortest", rung="R3"))
    store.record(observed("upload.max_verified_bytes", 200000, rung="R10"))
    store.record(observed("frames.stream_outside_work", False, rung="R2"))
    gates = _gates(store)
    assert gates.allowed("panel_vertices_usable") is True
    assert gates.value("rotation_model") == "in-place-shortest"
    assert gates.value("max_program_bytes") == 200000
    assert gates.allowed("frames_end_upload") is True
    assert gates.gate("panel_vertices_usable").basis == "fact"
    assert any("panel.vertex_frame" in e for e in gates.gate("panel_vertices_usable").evidence)

    other = FactStore.open(tmp_path / "b", SIM_SERIAL, "01.02.00")
    other.record(observed("panel.vertex_frame", "swap", rung="R2"))
    other.record(observed("frames.stream_outside_work", True, rung="R2"))
    gates = _gates(other)
    assert gates.allowed("panel_vertices_usable") is False
    assert "design review" in gates.gate("panel_vertices_usable").reason
    assert gates.allowed("frames_end_upload") is False


def test_tape_measurements_open_only_what_r0b_settles(tmp_path: Path) -> None:
    store = FactStore.open(tmp_path, TEST_SERIAL, "01.02.00")
    assert _gates(store).allowed("wire_attachment_measured") is False
    store.record_measurement("wire.attachment", "offset", None, None, OPERATOR, AT, rung="R0b", unknown_id="U-49")
    store.record_measurement("wire.same_point", "yes", None, None, OPERATOR, AT, rung="R0b")
    gates = _gates(store)
    assert gates.allowed("wire_attachment_measured") is False  # an offset needs its two distances
    assert "wire.back_mm" in gates.gate("wire_attachment_measured").reason
    store.record_measurement("wire.back_mm", 35.5, "mm", 2, OPERATOR, AT, rung="R0b")
    store.record_measurement("wire.right_mm", -4, "deg", 2, OPERATOR, AT, rung="R0b")  # a typo in the unit
    assert _gates(store).allowed("wire_attachment_measured") is False
    store.record_measurement("wire.right_mm", -4, "mm", 2, OPERATOR, AT, rung="R0b")
    gates = _gates(store)
    assert gates.value("wire_attachment_measured") == {"kind": "offset", "back_mm": 35.5, "right_mm": -4}
    store.record_measurement("wire.same_point", "unknown", None, None, OPERATOR, AT, rung="R0b")
    assert _gates(store).allowed("wire_attachment_measured") is False

    # The heading map needs the R3 observation and the R0b body axis, both.
    store.record(observed("heading.map_verified", True, serial=TEST_SERIAL, rung="R3"))
    assert _gates(store).allowed("heading_map_verified") is False
    store.record_measurement("footprint.axis_matches_body", "yes", None, None, OPERATOR, AT, rung="R0b")
    assert _gates(store).allowed("heading_map_verified") is True
    # A tape measurement cannot stand in for an observation, nor the other way round.
    store.record(observed("footprint.axis_matches_body", "yes", serial=TEST_SERIAL, rung="R3"))
    assert _gates(store).allowed("heading_map_verified") is False


def test_snapshot_facts_carry_values_and_gates_carry_their_provenance(tmp_path: Path) -> None:
    store = r1_store(tmp_path, "gfw-01.02.00")
    store.record_measurement("wire.attachment", "on_spindle_axis", None, None, OPERATOR, AT, rung="R0b")
    store.record_measurement("wire.same_point", "yes", None, None, OPERATOR, AT, rung="R0b")
    gates = _gates(store)
    assert gates.allowed("wire_attachment_measured") is True
    snap = gates.snapshot().json
    # The facts part carries values and their basis, rung and time, not the evidence lines.
    assert set(snap["facts"]["wire.attachment"]) == {"value", "basis", "rung", "unknown_id", "firmware",
                                                     "recorded_at"}
    assert OPERATOR not in json.dumps(snap["facts"])
    # An open gate carries its evidence, which for a typed measurement names who measured
    # it and when (design 3.1, C-13): provenance, not hidden.
    assert any(OPERATOR in e and "K9" in e for e in snap["gates"]["wire_attachment_measured"]["evidence"])
    override = GateOverride(gate="sanity_baseline", reason="checked", operator=OPERATOR, at=AT)
    # An override is shown with who made it, on every screen and in the snapshot (design 3.1).
    assert _gates(store, overrides=(override,)).snapshot().json["overrides"][0]["operator"] == OPERATOR


def test_frames_seen_at_idle_keep_frames_end_upload_closed(tmp_path: Path) -> None:
    # R1 saw a position frame while the robot was idle (U-52, FW-08). A later R2 fact that
    # frames do not stream outside work contradicts it; uncertain evidence may restrict, never
    # permit (design 3.1), so frames still never end an upload.
    store = r1_store(tmp_path, "gfw-01.02.00-idle-frame")
    store.record(observed("frames.stream_outside_work", False, rung="R2"))
    gate = _gates(store).gate("frames_end_upload")
    assert gate.allowed is False
    assert "frames.at_idle" in gate.reason and "S3" in gate.reason
    # The gate is unlocked by R1 and R2 (design 3.2): without R1's frames.at_idle it stays closed.
    bare = FactStore.open(tmp_path / "bare", SIM_SERIAL, "01.02.00")
    bare.record(observed("frames.stream_outside_work", False, rung="R2"))
    assert _gates(bare).allowed("frames_end_upload") is False
    assert "frames.at_idle" in _gates(bare).gate("frames_end_upload").reason
    # R1 saw no frame at idle and R2 found none outside work: frames may end an upload.
    clean = r1_store(tmp_path / "clean", "gfw-01.02.00")
    clean.record(observed("frames.stream_outside_work", False, rung="R2"))
    gate = _gates(clean).gate("frames_end_upload")
    assert gate.allowed is True
    assert any("frames.at_idle" in e for e in gate.evidence)


def test_measured_park_pose_opens_start_pose_known(tmp_path: Path) -> None:
    # Design 3.1 and K9: the R2 park pose is measured by hand (tool position by tape, body
    # heading by protractor: 4.10.5) and typed in, so it is robot-measured, one value per entry.
    store = FactStore.open(tmp_path, TEST_SERIAL, "01.02.00")
    assert _gates(store).allowed("start_pose_known") is False
    for fid, value, unit, tol in (("detection.park_pose.x_mm", 640, "mm", 5),
                                  ("detection.park_pose.y_mm", 410.5, "mm", 8)):
        store.record_measurement(fid, value, unit, tol, OPERATOR, AT, rung="R2", unknown_id="U-44")
    gates = _gates(store)
    assert gates.allowed("start_pose_known") is False  # no heading yet
    assert "detection.park_pose.heading_deg" in gates.gate("start_pose_known").reason
    store.record_measurement("detection.park_pose.heading_deg", 92, "deg", 3, OPERATOR, AT, rung="R2",
                             unknown_id="U-44")
    gates = _gates(store)
    assert gates.allowed("start_pose_known") is True
    assert gates.value("start_pose_known") == {"x_mm": 640, "y_mm": 410.5, "heading_deg": 92,
                                               "tolerance_mm": 8, "tolerance_deg": 3}
    assert load_gate_snapshot(gates.snapshot().canonical_bytes()).value("start_pose_known") == \
        gates.value("start_pose_known")
    # A unit that does not fit, or an answer that is not a number, closes it again.
    store.record_measurement("detection.park_pose.heading_deg", 92, "mm", 3, OPERATOR, AT, rung="R2")
    assert _gates(store).allowed("start_pose_known") is False
    store.record_measurement("detection.park_pose.heading_deg", "unknown", None, None, OPERATOR, AT, rung="R2")
    assert _gates(store).allowed("start_pose_known") is False
    # An observation of the pose (not typed in by the operator) does not stand in for it.
    other = FactStore.open(tmp_path / "observed", SIM_SERIAL, "01.02.00")
    other.record(observed("detection.park_pose", {"x": 640, "y": 410.5, "c": 0}, rung="R2"))
    assert _gates(other).allowed("start_pose_known") is False


def test_r1_terminator_must_be_none_for_jobs(tmp_path: Path) -> None:
    # Design 11.2 R1 go criteria: terminator none, on every branch. On B3 no image catches a
    # baseline recorded with another framing, so jobs_allowed checks it itself.
    facts = [f if f.id != "link.terminator" else observed(f.id, "lf", firmware="01.02.03", rung="R1",
                                                           recorded_at=f.recorded_at)
             for f in read_probe_report(report_path("vendor-app-newer-extended")).facts]
    store = FactStore.open(tmp_path, SIM_SERIAL, "01.02.03")
    store.record_many(facts)
    assert store.fingerprint_status().state == "baseline-recorded"
    gates = _gates(store, caps=V0103)
    assert gates.allowed("jobs_allowed") is False
    assert "terminator" in gates.gate("jobs_allowed").reason
    # No terminator in force at all: uncertain, so refused too.
    without = FactStore.open(tmp_path / "without", SIM_SERIAL, "01.02.00")
    without.record_many([f for f in read_probe_report(report_path("gfw-01.02.00")).facts
                         if f.id != "link.terminator"])
    gate = _gates(without).gate("jobs_allowed")
    assert gate.allowed is None and "link.terminator" in gate.reason


def test_frames_at_idle_never_opens_frames_end_upload(tmp_path: Path) -> None:
    store = r1_store(tmp_path, "gfw-01.02.00")  # frames.at_idle: none seen
    gates = _gates(store)
    assert gates.allowed("frames_end_upload") is False
    assert "R2" in gates.gate("frames_end_upload").reason or gates.gate("frames_end_upload").unlock_rung == "R2"


# ---------------------------------------------------------------------------
# jobs_allowed and the R1 branches
# ---------------------------------------------------------------------------


def test_b1_opens_jobs_allowed_and_the_baseline(tmp_path: Path) -> None:
    gates = _gates(r1_store(tmp_path, "gfw-01.02.00"))
    assert gates.branch == "B1"
    assert gates.allowed("jobs_allowed") is True
    gates.require("jobs_allowed")
    assert gates.allowed("sanity_baseline") is True
    for name in ("panel_vertices_usable", "heading_map_verified", "spindle_verified", "software_stop",
                 "keepalive_during_work", "second_job_without_power_cycle", "link_keepalive_ok"):
        assert gates.allowed(name) is False, name
    assert gates.allowed("upload_confirm_mode") is None  # the vendor gate is uncertain below 01.02.03


def test_b2_refuses_jobs(tmp_path: Path) -> None:
    gates = _gates(r1_store(tmp_path, "gfw-01.02.00-unknown-command-error"))
    assert gates.branch == "B2"
    assert gates.allowed("jobs_allowed") is False
    assert "design review" in gates.gate("jobs_allowed").reason
    assert any(d.id == "FINGERPRINT_DIFFERS" and d.severity == "red" for d in gates.disclosures)


def test_unknown_or_older_firmware_refuses_jobs(tmp_path: Path) -> None:
    # Unknown (U-01): read-only, an amber banner, gate basis "unknown".
    unknown = compute_gates(FirmwareCapabilities.for_version(None), FactStore.open(tmp_path, SIM_SERIAL, None),
                            NO_POLICY)
    assert unknown.allowed("jobs_allowed") is None
    assert unknown.gate("jobs_allowed").basis == "unknown"
    assert any(d.id == "FIRMWARE_UNKNOWN" and d.severity == "amber" and "read-only" in d.text
               for d in unknown.disclosures)

    # Older than 01.02.00 (B4): refused even though the probe facts are complete and
    # look like the 01.02.00 image.
    older_store = r1_store(tmp_path / "old", "gfw-01.01.09")
    older = compute_gates(FirmwareCapabilities.for_version((1, 1, 9)), older_store, NO_POLICY)
    assert older.branch == "B4"
    assert older.allowed("jobs_allowed") is False
    assert "older than 01.02.00" in older.gate("jobs_allowed").reason

    # Known and supported, but R1 has not run on this robot and firmware.
    fresh = _gates(FactStore.open(tmp_path / "fresh", SIM_SERIAL, "01.02.00"))
    assert fresh.allowed("jobs_allowed") is False
    assert "R1" in fresh.gate("jobs_allowed").reason
    assert fresh.allowed("sanity_baseline") is False

    # Facts of the previous firmware do not carry over to a newer one.
    r1_store(tmp_path / "flash", "gfw-01.02.00")
    reflashed = _gates(FactStore.open(tmp_path / "flash", SIM_SERIAL, "01.02.03"), caps=V0103)
    assert reflashed.allowed("jobs_allowed") is False
    stale = [d for d in reflashed.disclosures if d.id == "FACTS_STALE"]
    assert stale and stale[0].severity == "red"
    assert re.search(r"firmware changed from 01\.02\.00 to 01\.02\.03: \d+ facts stale; repeat rungs R1",
                     stale[0].text), stale[0].text


def test_jobs_allowed_needs_the_fingerprint_from_rung_observations(tmp_path: Path) -> None:
    # A fingerprint part seen outside a rung (say, a read during a session) cannot complete
    # the fingerprint: uncertain, so jobs stay refused. It can still contradict it.
    store = FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")
    store.record_many([f for f in read_probe_report(report_path("gfw-01.02.00")).facts
                       if f.id != "fingerprint.cage_status"])
    store.record(observed("fingerprint.cage_status", "CAGE STATUS: CLOSED", rung=None))
    gates = _gates(store)
    assert gates.allowed("jobs_allowed") is None
    assert gates.branch == "unverified"
    assert "fingerprint.cage_status" in gates.gate("jobs_allowed").reason
    assert any(d.id == "FINGERPRINT_UNVERIFIED" for d in gates.disclosures)
    store.record(observed("fingerprint.index_reply", "cfg read index: <n>", rung=None))
    gates = _gates(store)
    assert gates.allowed("jobs_allowed") is False
    assert gates.branch == "B2"


def test_caps_must_describe_the_store_firmware(tmp_path: Path) -> None:
    store = FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")
    with pytest.raises(ValueError, match="01.02.03"):
        compute_gates(V0103, store, NO_POLICY)
    with pytest.raises(TypeError):
        compute_gates("01.02.00", store, NO_POLICY)  # type: ignore[arg-type]


def test_b3_baseline_vendor_gates_and_unverified_rules(tmp_path: Path) -> None:
    gates = _gates(r1_store(tmp_path, "vendor-app-newer-extended"), caps=V0103)
    assert gates.branch == "B3"
    assert gates.allowed("jobs_allowed") is True
    ids = {d.id for d in gates.disclosures}
    assert "B3_NEW_BASELINE" in ids
    assert any(i.startswith("UNVERIFIED_FOR_VERSION:") for i in ids)
    # Permissions that rest only on the vendor's gates are open on B3, and disclosed while in use.
    assert gates.allowed("keepalive_during_work") is True
    assert gates.gate("keepalive_during_work").basis == "vendor-gate"
    assert gates.value("upload_confirm_mode") == "info8"
    assert "VENDOR_GATE:keepalive_during_work" in ids and "VENDOR_GATE:upload_confirm_mode" in ids
    # GCODE CONTINUE;0 before the header also rests only on the vendor's gate (design 11.5).
    assert "VENDOR_GATE:gcode_continue" in ids
    assert "VENDOR_GATE:gcode_continue" not in {d.id for d in _gates(r1_store(tmp_path / "b1", "gfw-01.02.00"))
                                                .disclosures}
    # One stop rule on every firmware: no software stop before R14 (design 7.6, 11.5).
    assert gates.allowed("software_stop") is False
    assert gates.gate("software_stop").unlock_rung == "R14"


def test_replay_with_info8_discloses_missing_evidence(tmp_path: Path) -> None:
    store = r1_store(tmp_path, "gfw-01.02.00")
    store.record(observed("replay.fresh_boot", {"codes": [34, 8], "connections": ["S2"]}, rung="R1"))
    gates = _gates(store)
    assert any(d.id == "REPLAY_INFO8_UNAVAILABLE" for d in gates.disclosures)


# ---------------------------------------------------------------------------
# Owner policy and typed overrides
# ---------------------------------------------------------------------------


def test_override_only_on_overridable_gate_with_reason_recorded(tmp_path: Path) -> None:
    override = GateOverride(gate="sanity_baseline", reason="INFO;40 index 7 differs after a tower swap; checked",
                            operator=OPERATOR, at=AT)
    # Without the R1 baselines the gate is closed for lack of its facts ("BLOCK until R1
    # has run", design 3.2): a typed reason cannot stand in for a missing baseline.
    empty = FactStore.open(tmp_path / "empty", SIM_SERIAL, "01.02.00")
    assert _gates(empty).allowed("sanity_baseline") is False
    with pytest.raises(GateOverrideRefused, match="R1"):
        _gates(empty, overrides=(override,))

    # With the baselines the gate is open; the override covers a difference its check
    # finds (U-08: "any difference blocks, typed-reason override allowed"), and is
    # recorded with who, when and why.
    store = r1_store(tmp_path, "gfw-01.02.00")
    plain = _gates(store)
    gates = _gates(store, overrides=(override,))
    gate = gates.gate("sanity_baseline")
    assert gate.allowed is True
    assert gate.basis == "fact+override"
    assert gate.value == plain.gate("sanity_baseline").value  # the baselines themselves are unchanged
    assert OPERATOR in gate.reason and "tower swap" in gate.reason and AT in gate.reason
    assert any("K7" in e and OPERATOR in e and AT in e for e in gate.evidence)
    assert gates.override_for("sanity_baseline") == override
    assert plain.override_for("sanity_baseline") is None
    assert gates.override_for("jobs_allowed") is None
    shown = [d for d in gates.disclosures if d.id == "OVERRIDE:sanity_baseline"]
    assert shown and shown[0].severity == "red" and OPERATOR in shown[0].text and "tower swap" in shown[0].text
    assert any("OVERRIDDEN" in line and OPERATOR in line for line in gates.explain())
    snap = gates.snapshot().json
    assert snap["overrides"] == [override.to_json()]
    assert gates.snapshot().sha256 != plain.snapshot().sha256
    assert load_gate_snapshot(gates.snapshot().canonical_bytes()).allowed("sanity_baseline") is True

    # The intent from the interface carries gate and reason; who and when are added by the runner.
    intent = Override(gate="sanity_baseline", reason="checked by hand")
    made = GateOverride.from_intent(intent, operator=OPERATOR, at=AT)
    assert (made.gate, made.reason, made.operator, made.at) == ("sanity_baseline", "checked by hand", OPERATOR, AT)

    for name in ("jobs_allowed", "panel_vertices_usable", "software_stop", "spindle_verified"):
        with pytest.raises(GateOverrideRefused, match=name):
            _gates(store, overrides=(GateOverride(gate=name, reason="I am sure", operator=OPERATOR, at=AT),))
    with pytest.raises(UnknownGateError):
        _gates(store, overrides=(GateOverride(gate="no_such_gate", reason="x", operator=OPERATOR, at=AT),))
    with pytest.raises(ValueError, match="two overrides"):
        _gates(store, overrides=(override, made))
    with pytest.raises(ValueError, match="reason"):
        GateOverride(gate="sanity_baseline", reason="  ", operator=OPERATOR, at=AT)
    with pytest.raises(ValueError, match="operator"):
        GateOverride(gate="sanity_baseline", reason="checked", operator="", at=AT)
    with pytest.raises(ValueError, match="UTC"):
        GateOverride(gate="sanity_baseline", reason="checked", operator=OPERATOR, at="2026-11-04T10:00:00")
    with pytest.raises(TypeError):
        _gates(store, overrides=(intent,))  # who and when are missing: not an override record


def test_owner_policy_only_tightens(tmp_path: Path) -> None:
    store = r1_store(tmp_path, "gfw-01.02.00")
    store.record(observed("panel.vertex_frame", "identity", rung="R2"))
    policy = OwnerPolicy(closed={"panel_vertices_usable": "no uploads until the new towers are in"},
                         consents={}, source="owner settings (test)")
    gates = _gates(store, policy=policy)
    assert gates.allowed("panel_vertices_usable") is False
    assert gates.gate("panel_vertices_usable").basis == "policy"
    assert "new towers" in gates.gate("panel_vertices_usable").reason
    # Policy beats an override: it can only tighten, and the override is refused, not dropped quietly.
    closed = OwnerPolicy(closed={"sanity_baseline": "owner wants the baseline"}, consents={}, source="test")
    o = GateOverride(gate="sanity_baseline", reason="checked", operator=OPERATOR, at=AT)
    assert _gates(store, policy=closed).allowed("sanity_baseline") is False
    with pytest.raises(GateOverrideRefused, match="policy"):
        _gates(store, policy=closed, overrides=(o,))
    # A consent never opens a gate on its own: drop_dwells also needs its R4 fact.
    consent = OwnerPolicy(closed={}, consents={"drop_dwells": "owner agrees"}, source="test")
    assert _gates(store, policy=consent).allowed("drop_dwells") is False
    store.record(observed("spindle.spinup_wait", True, rung="R4"))
    assert _gates(store).allowed("drop_dwells") is False  # fact without consent
    assert _gates(store, policy=consent).allowed("drop_dwells") is True
    with pytest.raises(ValueError, match="consent"):
        OwnerPolicy(closed={}, consents={"jobs_allowed": "yes"}, source="test")
    with pytest.raises(UnknownGateError):
        OwnerPolicy(closed={"no_such_gate": "x"}, consents={}, source="test")
    with pytest.raises(ValueError, match="reason"):
        OwnerPolicy(closed={"arcs_emit": ""}, consents={}, source="test")


# ---------------------------------------------------------------------------
# Snapshot for goliath-prep (red-team C-05)
# ---------------------------------------------------------------------------


def test_gate_snapshot_roundtrip_into_prep_gateview(tmp_path: Path) -> None:
    store = r1_store(tmp_path, "gfw-01.02.00")
    store.record(observed("panel.vertex_frame", "identity", rung="R2"))
    store.record(observed("rotation.model", "in-place-shortest", rung="R3"))
    store.record(observed("upload.max_verified_bytes", 65536, rung="R10"))
    gates = _gates(store)
    snap = gates.snapshot()
    assert snap.sha256 == canonical_sha256(snap.json)
    assert snap.json["schema"] == "goliath-run/gate-snapshot@1"
    assert snap.json["serial"] == SIM_SERIAL and snap.json["firmware"] == "01.02.00"
    assert snap.json["facts"]["panel.vertex_frame"]["value"] == "identity"

    path = tmp_path / "gates.json"
    snap.write(path)
    assert path.read_bytes() == snap.canonical_bytes()
    view = load_gate_snapshot(path)
    live = gates.as_prep_view()
    for v in (view, live):
        assert isinstance(v, GateView)
        assert v.snapshot_sha256() == snap.sha256
    for name in GATE_CATALOGUE:
        assert view.allowed(name) is live.allowed(name), name
        assert view.value(name) == live.value(name), name
    assert view.value("rotation_model") == "in-place-shortest"
    assert view.value("max_program_bytes") == 65536
    assert view.allowed("upload_confirm_mode") is None  # tri-state survives the file
    assert view.snapshot_sha256() != ClosedGateView().snapshot_sha256()
    # The same gates give the same bytes; a changed fact gives another snapshot.
    assert _gates(store).snapshot() == snap
    store.record(observed("arcs.verified", True, rung="R6"))
    assert _gates(store).snapshot().sha256 != snap.sha256


def test_snapshot_loader_refuses_tampering(tmp_path: Path) -> None:
    snap = _gates(r1_store(tmp_path, "gfw-01.02.00")).snapshot()
    # An edited snapshot is another snapshot: its hash no longer matches what a certificate bound.
    data = json.loads(snap.canonical_bytes())
    data["gates"]["arcs_emit"].update(allowed=True, basis="fact")
    assert load_gate_snapshot(data).snapshot_sha256() != snap.sha256
    with pytest.raises(ValueError, match="sha256"):
        load_gate_snapshot(data, expect_sha256=snap.sha256)
    for bad in ("yes", 1, "null"):
        broken = json.loads(snap.canonical_bytes())
        broken["gates"]["arcs_emit"]["allowed"] = bad
        with pytest.raises((TypeError, ValueError), match="arcs_emit"):
            load_gate_snapshot(broken)
    missing = json.loads(snap.canonical_bytes())
    del missing["gates"]["jobs_allowed"]
    with pytest.raises(ValueError, match="jobs_allowed"):
        load_gate_snapshot(missing)
    extra = json.loads(snap.canonical_bytes())
    extra["gates"]["arcs_emit"]["comment"] = "added by hand"
    with pytest.raises(ValueError, match="comment"):
        load_gate_snapshot(extra)
    wrong = json.loads(snap.canonical_bytes())
    wrong["schema"] = "goliath-run/gate-snapshot@2"
    with pytest.raises(ValueError, match="schema"):
        load_gate_snapshot(wrong)
    # The parts a reader shows are checked too, not only the gates.
    for key, bad, match in (
        ("serial", "../elsewhere", "serial"),
        ("serial", "", "serial"),
        ("firmware", "1.2.0", "firmware"),
        ("branch", "B9", "branch"),
        ("disclosures", [{"id": "X", "severity": "green", "text": "t", "source": "s", "sticky": True}], "severity"),
        ("disclosures", {}, "disclosures"),
        ("overrides", [{"gate": "jobs_allowed", "reason": "r", "operator": OPERATOR, "at": AT}], "jobs_allowed"),
        ("overrides", [{"gate": "sanity_baseline", "reason": "r", "operator": OPERATOR}], "override"),
        ("stale", [1], "stale"),
        ("facts", [], "facts"),
    ):
        broken = json.loads(snap.canonical_bytes())
        broken[key] = bad
        with pytest.raises(ValueError, match=match):
            load_gate_snapshot(broken)
    # An open gate needs a basis that can open it, and every override its gate.
    for basis in ("policy", "no-fact", "unknown", "firmware"):
        broken = json.loads(snap.canonical_bytes())
        broken["gates"]["arcs_emit"].update(allowed=True, basis=basis)
        with pytest.raises(ValueError, match="arcs_emit"):
            load_gate_snapshot(broken)
    broken = json.loads(snap.canonical_bytes())
    broken["overrides"] = [{"gate": "sanity_baseline", "reason": "r", "operator": OPERATOR, "at": AT}]
    with pytest.raises(ValueError, match="sanity_baseline"):
        load_gate_snapshot(broken)  # its gate does not say fact+override
    broken = json.loads(snap.canonical_bytes())
    broken["gates"]["sanity_baseline"]["basis"] = "fact+override"
    with pytest.raises(ValueError, match="sanity_baseline"):
        load_gate_snapshot(broken)  # fact+override with no override recorded
    with pytest.raises(ValueError, match="sha256"):
        load_gate_snapshot(snap.canonical_bytes(), expect_sha256="0" * 64)
    assert load_gate_snapshot(snap.canonical_bytes(), expect_sha256=snap.sha256).snapshot_sha256() == snap.sha256


def test_snapshot_binds_serial_and_firmware(tmp_path: Path) -> None:
    a = _gates(r1_store(tmp_path / "a", "gfw-01.02.00")).snapshot()
    imported = read_probe_report(report_path("gfw-01.02.00"))
    other = FactStore.open(tmp_path / "b", TEST_SERIAL, "01.02.00")
    assert other.serial == TEST_SERIAL and imported.serial == SIM_SERIAL
    b = _gates(other).snapshot()
    assert a.sha256 != b.sha256


def test_explain_names_every_gate(tmp_path: Path) -> None:
    gates = _gates(r1_store(tmp_path, "gfw-01.02.00"))
    lines = gates.explain()
    for name in GATE_CATALOGUE:
        assert any(line.startswith(name + ":") for line in lines), name
    closed = [line for line in lines if line.startswith("arcs_emit:")][0]
    assert "NO" in closed and "R6" in closed
