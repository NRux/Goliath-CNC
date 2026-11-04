# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The facts store: provenance, binding to serial, firmware and fingerprint (unit R01; design 3.1, 11.5)."""
from __future__ import annotations

import json
import os
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from run_facts_support import AT, OPERATOR, SIM_SERIAL, TEST_SERIAL, observed, r1_store, report_path

from goliath_run.facts import (
    FINGERPRINT_FACT_IDS,
    FINGERPRINTS,
    STATIC_01_02_00_RULES,
    Fact,
    FactBindingError,
    FactStore,
    FactStoreConflictError,
    FactStoreError,
    read_probe_report,
)

# ---------------------------------------------------------------------------
# Fact
# ---------------------------------------------------------------------------


def test_fact_is_frozen_hashable_and_round_trips() -> None:
    fact = observed("panel.vertex_frame", "identity", unknown_id="U-13")
    nested = observed("detection.park_pose", {"x": 100.5, "y": 20.25, "c": 90, "tags": ["a", "b"]})
    for f in (fact, nested):
        assert Fact.from_json(f.to_json()) == f
        assert hash(f) == hash(Fact.from_json(json.loads(json.dumps(f.to_json()))))
        assert pickle.loads(pickle.dumps(f)) == f
    with pytest.raises(AttributeError):
        fact.value = "swap"  # type: ignore[misc]
    with pytest.raises(TypeError):
        nested.value["x"] = 1  # type: ignore[index]


@pytest.mark.parametrize(
    "changes, error",
    [
        ({"id": "Panel Frame"}, ValueError),
        ({"id": "nodots"}, ValueError),
        ({"basis": "guessed"}, ValueError),
        ({"value": None}, ValueError),  # an unknown is not a fact
        ({"value": float("nan")}, ValueError),
        ({"value": 0.0001}, ValueError),  # off the canonical 0.001 grid: a record would show another value
        ({"value": b"bytes"}, TypeError),
        ({"value": {1: "x"}}, TypeError),
        ({"unknown_id": "U1"}, ValueError),
        ({"rung": "rung 3"}, ValueError),
        ({"firmware": "1.2.0"}, ValueError),
        ({"serial": "../escape"}, ValueError),
        ({"recorded_at": "2026-11-04T10:00:00"}, ValueError),  # naive: not UTC
        ({"recorded_at": "2026-11-04T10:00:00+02:00"}, ValueError),
        ({"recorded_at": "today"}, ValueError),
        ({"evidence": ("",)}, ValueError),
        ({"evidence": "one string"}, TypeError),
        ({"note": None}, TypeError),
    ],
)
def test_fact_validation(changes: dict, error: type) -> None:
    fields = observed("panel.vertex_frame", "identity").to_json()
    fields.update(changes)
    with pytest.raises(error):
        Fact(**fields)


def test_recorded_at_is_normalised_to_utc_z() -> None:
    f = observed("panel.vertex_frame", "identity", recorded_at="2026-11-04T10:00:00+00:00")
    assert f.recorded_at == "2026-11-04T10:00:00Z"


# ---------------------------------------------------------------------------
# Store basics
# ---------------------------------------------------------------------------


def test_store_persists_and_binds_to_serial(tmp_path: Path) -> None:
    store = FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")
    fact = observed("panel.vertex_frame", "identity")
    store.record(fact)
    assert store.get("panel.vertex_frame") == fact
    again = FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")
    assert again.get("panel.vertex_frame") == fact
    assert again.path == tmp_path / "robots" / SIM_SERIAL / "facts.json"
    # Another robot has its own store: nothing carries over.
    other = FactStore.open(tmp_path, TEST_SERIAL, "01.02.00")
    assert other.get("panel.vertex_frame") is None
    assert other.current() == {}


def test_record_refuses_a_fact_of_another_robot_or_firmware(tmp_path: Path) -> None:
    store = FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")
    with pytest.raises(FactBindingError, match="serial"):
        store.record(observed("panel.vertex_frame", "identity", serial=TEST_SERIAL))
    with pytest.raises(FactBindingError, match="firmware"):
        store.record(observed("panel.vertex_frame", "identity", firmware="01.02.03"))
    unknown = FactStore.open(tmp_path, SIM_SERIAL, None)
    with pytest.raises(FactBindingError, match="unknown"):
        unknown.record(observed("panel.vertex_frame", "identity"))
    assert store.get("panel.vertex_frame") is None


@pytest.mark.parametrize("serial", ["", "../x", "a/b", "a\\b", "CON", "nul", ".hidden", "x" * 65, "TEST SERIAL"])
def test_serial_must_be_a_safe_folder_name(tmp_path: Path, serial: str) -> None:
    with pytest.raises(ValueError):
        FactStore.open(tmp_path, serial, "01.02.00")


def test_unreadable_or_newer_store_fails_closed(tmp_path: Path) -> None:
    store = FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")
    store.record(observed("panel.vertex_frame", "identity"))
    path = store.path
    path.write_text("{truncated", encoding="utf-8")
    with pytest.raises(FactStoreError, match=str(path.name)):
        FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")
    path.write_text(json.dumps({"schema": "goliath-run/facts@2", "serial": SIM_SERIAL}), encoding="utf-8")
    with pytest.raises(FactStoreError, match="facts@2"):
        FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")
    path.write_text(json.dumps({"schema": "goliath-run/facts@1", "serial": TEST_SERIAL, "records": [],
                                "identities": []}), encoding="utf-8")
    with pytest.raises(FactStoreError, match="serial"):
        FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")


def test_concurrent_writer_is_detected(tmp_path: Path) -> None:
    first = FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")
    second = FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")
    first.record(observed("panel.vertex_frame", "identity"))
    with pytest.raises(FactStoreConflictError):
        second.record(observed("rotation.model", "in-place-shortest", rung="R3"))
    # Nothing was lost: the first writer's fact is there, the refused one is not.
    fresh = FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")
    assert fresh.get("panel.vertex_frame") is not None
    assert fresh.get("rotation.model") is None


def test_failed_write_keeps_the_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")
    store.record(observed("panel.vertex_frame", "identity"))
    before = store.path.read_bytes()

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        store.record(observed("rotation.model", "in-place-shortest", rung="R3"))
    monkeypatch.undo()
    assert store.path.read_bytes() == before
    assert [p.name for p in store.path.parent.iterdir()] == ["facts.json"]  # no temporary file left
    assert store.get("rotation.model") is None


def test_record_many_is_all_or_nothing(tmp_path: Path) -> None:
    store = FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")
    good = observed("panel.vertex_frame", "identity")
    bad = observed("rotation.model", "in-place-shortest", firmware="01.02.03")
    with pytest.raises(FactBindingError):
        store.record_many([good, bad])
    assert store.get("panel.vertex_frame") is None
    assert json.loads(store.path.read_text(encoding="utf-8"))["records"] == []  # only the firmware seen
    with pytest.raises(ValueError, match="twice"):
        store.record_many([good, observed("panel.vertex_frame", "swap")])
    with pytest.raises(ValueError, match="nothing"):
        store.record_many([])


def test_a_newer_observation_supersedes(tmp_path: Path) -> None:
    store = FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")
    store.record(observed("panel.vertex_frame", "swap"))
    store.record(observed("panel.vertex_frame", "identity"))
    assert store.get("panel.vertex_frame").value == "identity"
    assert store.stale() == []


# ---------------------------------------------------------------------------
# Staleness: firmware, serial, fingerprint (design 3.1, U-02)
# ---------------------------------------------------------------------------


def test_firmware_change_marks_observed_facts_stale(tmp_path: Path) -> None:
    store = r1_store(tmp_path, "gfw-01.02.00")
    store.record(observed("panel.vertex_frame", "identity"))
    store.record_measurement("footprint.q1_mm", 401.5, "mm", 2.0, OPERATOR, AT, rung="R0b", unknown_id="U-22")
    r1_ids = {f.id for f in read_probe_report(report_path("gfw-01.02.00")).facts}
    assert store.stale() == []

    newer = FactStore.open(tmp_path, SIM_SERIAL, "01.02.03")
    stale = {f.id for f in newer.stale()}
    assert stale == r1_ids | {"panel.vertex_frame"}
    for fid in stale:
        assert newer.get(fid) is None  # shown, not used
        assert "firmware changed from 01.02.00 to 01.02.03" in newer.why_stale(fid)
    # A tape measurement of the switched-off robot does not depend on its firmware.
    assert newer.get("footprint.q1_mm") is not None

    # Back on 01.02.00 the old observations stay stale: the robot was reflashed in between.
    back = FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")
    assert {f.id for f in back.stale()} == stale
    assert back.get("firmware.version") is None
    assert [fw for fw, _ in back.firmware_history()] == ["01.02.00", "01.02.03", "01.02.00"]


def test_store_opened_with_unknown_firmware_uses_no_observed_fact(tmp_path: Path) -> None:
    r1_store(tmp_path, "gfw-01.02.00")
    unknown = FactStore.open(tmp_path, SIM_SERIAL, None)
    assert unknown.get("firmware.version") is None
    assert {f.id for f in unknown.stale()} >= {"firmware.version", "rung.r1.go"}
    assert "firmware unknown" in unknown.why_stale("firmware.version")


def test_fingerprint_contradiction_marks_stale(tmp_path: Path) -> None:
    store = r1_store(tmp_path, "gfw-01.02.00")
    store.record(observed("panel.vertex_frame", "identity"))
    assert store.fingerprint_status().state == "matches"

    # A later read-only probe of the same robot and firmware answers unknown commands with ERR;1.
    later = read_probe_report(report_path("gfw-01.02.00-unknown-command-error"))
    store.record_many(later.facts)
    status = store.fingerprint_status()
    assert status.state == "differs"
    assert [c.fact_id for c in status.contradictions] == ["fingerprint.unknown_command"]
    assert status.contradictions[0].observed == "reply:ERR;1"

    stale = {f.id for f in store.stale()}
    assert "panel.vertex_frame" in stale
    assert "fingerprint behaviour contradicted" in store.why_stale("panel.vertex_frame")
    # The run that showed the difference is evidence of this build, not stale.
    assert store.get("fingerprint.unknown_command").value == "reply:ERR;1"
    assert store.get("firmware.version") is not None
    # It persists: reopening does not forget the contradiction.
    assert FactStore.open(tmp_path, SIM_SERIAL, "01.02.00").fingerprint_status().state == "differs"


def test_fingerprint_unverified_until_every_part_is_observed(tmp_path: Path) -> None:
    store = FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")
    store.record(observed("fingerprint.status_reply", "blob48", rung="R1"))
    status = store.fingerprint_status()
    assert status.state == "unverified"
    assert set(status.missing) == set(FINGERPRINT_FACT_IDS) - {"fingerprint.status_reply"}


def test_fingerprints_are_per_version_family() -> None:
    assert set(FINGERPRINTS) == {"01.02.00"}
    fp = FINGERPRINTS["01.02.00"]
    assert set(fp.expected) == set(FINGERPRINT_FACT_IDS)
    assert fp.accepts("fingerprint.cage_status", "CAGE STATUS: CLOSED")
    assert fp.accepts("fingerprint.cage_status", "CAGE STATUS: OPEN\n")
    assert not fp.accepts("fingerprint.cage_status", "CAGE STATUS: CLOSED\n")
    assert not fp.accepts("fingerprint.status_reply", "gs19")
    assert not fp.accepts("link.terminator", "lf")
    assert "09-firmware-01.02.00.md" in fp.source


def test_b3_records_new_baseline_and_marks_static_facts_unverified(tmp_path: Path) -> None:
    # A fact derived from static analysis of 01.02.00, recorded while the robot ran 01.02.00.
    store = FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")
    static = observed("upload.end_signal_static", "INFO;0", basis="firmware-static", rung=None,
                      note="spec 09 G-code upload mode")
    store.record(static)
    assert store.unverified() == []
    assert store.get("upload.end_signal_static") == static  # shown as single-source; it opens nothing

    # The robot now reports 01.02.03 (branch B3).
    b3 = r1_store(tmp_path, "vendor-app-newer-extended")
    assert b3.firmware == "01.02.03"
    status = b3.fingerprint_status()
    assert status.state == "baseline-recorded"
    assert status.family == "baseline:01.02.03"
    assert status.baseline["fingerprint.status_reply"] == "gs19"
    assert status.baseline["link.terminator"] == "none"
    assert [f.id for f in b3.unverified()] == ["upload.end_signal_static"]
    assert "unverified for this version" in b3.why_unverified("upload.end_signal_static")
    assert b3.get("upload.end_signal_static") is None
    assert len(STATIC_01_02_00_RULES) >= 5

    # A later observation that differs from the B3 baseline is a contradiction.
    later = read_probe_report(report_path("vendor-app-newer-extended"))
    changed = [f if f.id != "fingerprint.status_reply" else observed(
        f.id, "blob48", firmware="01.02.03", rung="R1") for f in later.facts]
    b3.record_many(changed)
    assert b3.fingerprint_status().state == "differs"


def test_b3_cage_state_change_is_not_a_contradiction(tmp_path: Path) -> None:
    b3 = r1_store(tmp_path, "vendor-app-newer-extended")
    b3.record(observed("fingerprint.cage_status", "CAGE STATUS: OPEN\n", firmware="01.02.03", rung="R1"))
    assert b3.fingerprint_status().state == "baseline-recorded"
    b3.record(observed("fingerprint.cage_status", "CAGE STATUS: CLOSED\n", firmware="01.02.03", rung="R1"))
    assert b3.fingerprint_status().state == "differs"


# ---------------------------------------------------------------------------
# Typed measurement entry (red-team C-13)
# ---------------------------------------------------------------------------


def test_measurement_entry_provenance(tmp_path: Path) -> None:
    store = FactStore.open(tmp_path, TEST_SERIAL, "01.02.00")
    at = datetime(2026, 11, 4, 10, 30, tzinfo=timezone.utc)
    fact = store.record_measurement("wheel.q1.contact_radius_mm", 38, "mm", 1, OPERATOR, at, rung="R0b",
                                    unknown_id="U-22")
    assert fact.basis == "robot-measured"
    assert fact.rung == "R0b"
    assert fact.unknown_id == "U-22"
    assert fact.serial == TEST_SERIAL
    assert fact.firmware is None  # a tape measurement does not depend on the firmware
    assert fact.recorded_at == "2026-11-04T10:30:00Z"
    assert dict(fact.value) == {"value": 38, "unit": "mm", "tolerance": 1, "operator": OPERATOR}
    assert any(OPERATOR in e and "K9" in e for e in fact.evidence)
    assert store.get("wheel.q1.contact_radius_mm") == fact

    choice = store.record_measurement("wire.attachment", "on_spindle_axis", None, None, OPERATOR, AT, rung="R0b")
    assert dict(choice.value) == {"value": "on_spindle_axis", "unit": None, "tolerance": None, "operator": OPERATOR}

    # Nothing is defaulted: no unit, no tolerance, no operator, no time zone, no rung.
    with pytest.raises(ValueError, match="unit"):
        store.record_measurement("footprint.q1_mm", 402, None, 2, OPERATOR, AT, rung="R0b")
    with pytest.raises(ValueError, match="tolerance"):
        store.record_measurement("footprint.q1_mm", 402, "mm", None, OPERATOR, AT, rung="R0b")
    with pytest.raises(ValueError, match="operator"):
        store.record_measurement("footprint.q1_mm", 402, "mm", 2, " ", AT, rung="R0b")
    with pytest.raises(ValueError, match="UTC"):
        store.record_measurement("footprint.q1_mm", 402, "mm", 2, OPERATOR, datetime(2026, 11, 4, 10), rung="R0b")
    with pytest.raises(ValueError, match="UTC"):
        store.record_measurement("footprint.q1_mm", 402, "mm", 2, OPERATOR,
                                 datetime(2026, 11, 4, 10, tzinfo=timezone(timedelta(hours=1))), rung="R0b")
    with pytest.raises(TypeError, match="rung"):
        store.record_measurement("footprint.q1_mm", 402, "mm", 2, OPERATOR, AT)  # type: ignore[call-arg]
    assert store.get("footprint.q1_mm") is None


def test_measurement_survives_reopen_and_binds_to_serial(tmp_path: Path) -> None:
    store = FactStore.open(tmp_path, TEST_SERIAL, "01.02.00")
    fact = store.record_measurement("footprint.q2_mm", 210.5, "mm", 2.0, OPERATOR, AT, rung="R0b")
    assert FactStore.open(tmp_path, TEST_SERIAL, "01.02.03").get("footprint.q2_mm") == fact
    path = store.path
    data = json.loads(path.read_text(encoding="utf-8"))
    data["records"][-1]["fact"]["serial"] = SIM_SERIAL  # copied from another robot's store
    path.write_text(json.dumps(data), encoding="utf-8")
    moved = FactStore.open(tmp_path, TEST_SERIAL, "01.02.00")
    assert moved.get("footprint.q2_mm") is None
    assert "another robot" in moved.why_stale("footprint.q2_mm")


def test_facts_sha256_changes_with_current_facts(tmp_path: Path) -> None:
    store = FactStore.open(tmp_path, SIM_SERIAL, "01.02.00")
    empty = store.sha256()
    store.record(observed("panel.vertex_frame", "identity"))
    assert store.sha256() != empty
    assert FactStore.open(tmp_path, SIM_SERIAL, "01.02.00").sha256() == store.sha256()
