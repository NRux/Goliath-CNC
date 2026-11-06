# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Importing the read-only probe's JSON report as rung R1 facts (unit R01; design 11.2 R1, 3.1).

Every report read here was written by goliath-link's probe run against its
simulator: the committed ones under fixtures/probe_reports/, and others produced
live by the probe of this checkout (``test_probe_report_import_live_matches_committed``
and the tests after it). Tampered copies are only used to show that the importer
refuses what it cannot vouch for.

Two probes wrote the committed reports: the probe as frozen for rung R1, and the
hotfixed probe, which stops on firmware older than 01.02.00 or an unknown identity
(its firmware gate, exit code 4) and writes shareable copies with the serial number
and Wi-Fi names masked (fixtures/probe_reports/make_probe_reports.py says which).
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path

import pytest
from run_facts_support import OPERATOR, PROBE_REPORTS, REPORT_NAME, SIM_SERIAL, load_generator, report_path

from goliath_run.facts import (
    ABSENT_UNTIL_R1B,
    R1_FACT_IDS,
    Fact,
    ProbeReportError,
    import_probe_report,
    read_probe_report,
)

R1_IDS = {
    "firmware.version",
    "link.terminator",
    "fingerprint.unknown_command",
    "fingerprint.status_reply",
    "fingerprint.cage_status",
    "fingerprint.index_reply",
    "replay.fresh_boot",
    "status_blob.baseline.idle",
    "sanity.baseline.idle",
    "index.baseline",
    "frames.at_idle",
    "rung.r1.go",
}


def _by_id(facts: tuple[Fact, ...] | list[Fact]) -> dict[str, Fact]:
    ids = [f.id for f in facts]
    assert len(ids) == len(set(ids)), "one fact per id from one report: %s" % ids
    return {f.id: f for f in facts}


def _report(scenario: str) -> dict:
    return json.loads(report_path(scenario).read_text(encoding="utf-8"))


def _copy(scenario: str, tmp_path: Path, edit=None) -> Path:
    """A copy of a committed report (and its capture), optionally edited, to show a refusal."""
    src = report_path(scenario)
    dst = tmp_path / scenario
    dst.mkdir(parents=True)
    for f in src.parent.iterdir():
        shutil.copy(f, dst / f.name)
    if edit is not None:
        data = json.loads((dst / REPORT_NAME).read_text(encoding="utf-8"))
        edit(data)
        (dst / REPORT_NAME).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return dst / REPORT_NAME


def test_r1_fact_ids_are_the_designed_ones() -> None:
    # Design 11.2 R1: firmware.version, link.terminator, fingerprint.*, replay.fresh_boot,
    # status_blob.baseline.idle, sanity.baseline.idle, index.baseline, frames.at_idle;
    # plus the record that R1 passed, which jobs_allowed needs (design 3.2).
    assert set(R1_FACT_IDS) == R1_IDS


def test_probe_report_import() -> None:
    """Branch B1: the 01.02.00 simulator; every R1 fact, with its provenance."""
    path = report_path("gfw-01.02.00")
    report = _report("gfw-01.02.00")
    imported = read_probe_report(path)
    assert imported.serial == SIM_SERIAL
    assert imported.firmware == "01.02.00"
    assert imported.branch == "B1"
    facts = _by_id(imported.facts)
    assert set(facts) == R1_IDS

    report_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    capture = path.parent / Path(report["capture_file"]).name
    capture_sha = hashlib.sha256(capture.read_bytes()).hexdigest()
    for fact in facts.values():
        assert fact.basis == "observed"
        assert fact.rung == "R1"
        assert fact.serial == SIM_SERIAL
        assert fact.firmware == "01.02.00"
        assert fact.recorded_at == "2026-11-04T09:00:00Z"  # the probe run's start, not the import time
        assert any(report_sha in e and REPORT_NAME in e for e in fact.evidence), fact.evidence
        assert any(capture_sha in e for e in fact.evidence), fact.evidence
        assert all("/" not in e.split(" ", 1)[0] for e in fact.evidence), "file names, not this computer's paths"

    assert facts["firmware.version"].value == "01.02.00"
    assert facts["firmware.version"].unknown_id == "U-01"
    assert facts["link.terminator"].value == "none"
    assert facts["link.terminator"].unknown_id == "U-03"
    assert facts["fingerprint.unknown_command"].value == "silent"
    assert facts["fingerprint.status_reply"].value == "blob48"
    assert facts["fingerprint.cage_status"].value == "CAGE STATUS: CLOSED"  # snprintf(20) cut the newline
    assert facts["fingerprint.index_reply"].value == "cfg read index : <n>"
    for fid in ("fingerprint.unknown_command", "fingerprint.status_reply", "fingerprint.cage_status",
                "fingerprint.index_reply"):
        assert facts[fid].unknown_id == "U-02"
    assert facts["index.baseline"].value == 28500
    assert facts["index.baseline"].unknown_id == "U-09"

    blob = report["global_status"]
    status = facts["status_blob.baseline.idle"]
    assert status.unknown_id == "U-07"
    assert status.value["kind"] == "blob48"
    assert status.value["raw"] == blob["raw"]
    assert list(status.value["fields"]) == blob["fields"]

    sanity = facts["sanity.baseline.idle"]
    assert sanity.unknown_id == "U-08"
    assert list(sanity.value["values"]) == ["1"] * 15
    assert sanity.value["raw"] == "INFO;40;1;1;1;1;1;1;1;1;1;1;1;1;1;1;1;END"

    replay = facts["replay.fresh_boot"]
    assert replay.unknown_id == "U-06"
    assert list(replay.value["codes"]) == [34]
    assert list(replay.value["connections"]) == ["S2"]

    frames = facts["frames.at_idle"]
    assert frames.unknown_id == "U-52"
    assert frames.value["unsolicited"] == 0
    assert frames.value["solicited"] == 0

    assert facts["rung.r1.go"].value is True
    assert "exit code 0" in facts["rung.r1.go"].note

    assert imported.facts == tuple(import_probe_report(path))


def test_probe_report_import_b2_unknown_command_answered() -> None:
    imported = read_probe_report(report_path("gfw-01.02.00-unknown-command-error"))
    facts = _by_id(imported.facts)
    assert facts["fingerprint.unknown_command"].value == "reply:ERR;1"
    assert imported.branch == "B2"
    assert any("fingerprint.unknown_command" in line for line in imported.notes)


def test_probe_notes_are_carried_into_the_import(caplog: pytest.LogCaptureFixture) -> None:
    # What the probe noted (here: ERR;1 after the lf and crlf variants) is never dropped.
    path = report_path("gfw-01.02.00-unknown-command-error")
    probe_notes = _report("gfw-01.02.00-unknown-command-error")["notes"]
    assert probe_notes
    imported = read_probe_report(path)
    for note in probe_notes:
        assert any(note in line and line.startswith("the probe noted:") for line in imported.notes), note
    with caplog.at_level(logging.WARNING, logger="goliath_run.facts"):
        import_probe_report(path)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert all(note in logged for note in probe_notes)


def test_unattributed_messages_are_disclosed(tmp_path: Path) -> None:
    # A line the probe could not credit to any command adds no warning in the probe, so the
    # import says so (the line itself stays in the report).
    report = _report("gfw-01.02.00")
    stray = dict(report["passive"][0], unattributed="arrived after the last command's reply window")
    path = _copy("gfw-01.02.00", tmp_path, edit=lambda d: d.__setitem__("unattributed", [stray]))
    imported = read_probe_report(path)
    assert any("1 message(s)" in line and "unattributed" in line for line in imported.notes), imported.notes


@pytest.mark.parametrize("field, value", [("uncertain", "a connect-time push arrived late"),
                                          ("error", "connect failed: timed out")])
def test_uncertain_terminator_verdicts_are_no_fingerprint_evidence(tmp_path: Path, field: str, value: str) -> None:
    # Uncertain evidence may restrict, never permit (design 3.1): an unanswered variant whose
    # verdict the probe marked uncertain, or whose session failed, does not show "silent".
    def mark(d: dict) -> None:
        for test in d["terminator_tests"]:
            if test["mode"] != d["chosen_terminator"]:
                test[field] = value

    imported = read_probe_report(_copy("gfw-01.02.00", tmp_path, edit=mark))
    assert "fingerprint.unknown_command" not in _by_id(imported.facts)
    assert "fingerprint.unknown_command" in imported.absent
    assert imported.branch == "unverified"
    assert any("fingerprint.unknown_command not recorded" in line for line in imported.notes)


def test_probe_report_import_b3_newer_firmware() -> None:
    imported = read_probe_report(report_path("vendor-app-newer-extended"))
    facts = _by_id(imported.facts)
    assert imported.firmware == "01.02.03"
    assert imported.branch == "B3"
    assert facts["fingerprint.status_reply"].value == "gs19"
    status = facts["status_blob.baseline.idle"]
    assert status.value["kind"] == "gs19"
    assert "fields" not in status.value  # the 19-byte frame carries no status fields
    # The frame that answered SEND TCP POS was asked for: it is not a frame streamed at idle.
    assert facts["frames.at_idle"].value["solicited"] == 1
    assert facts["frames.at_idle"].value["unsolicited"] == 0


def test_probe_report_import_b4_older_firmware() -> None:
    # A report the frozen probe wrote on old firmware (it still swept, as on the owner's
    # robot at rung R1): branch B4, which closes every gate (test_run_capabilities).
    imported = read_probe_report(report_path("frozen-probe-gfw-01.01.09"))
    assert imported.firmware == "01.01.09"
    assert imported.branch == "B4"
    assert _by_id(imported.facts)["firmware.version"].value == "01.01.09"


def _assert_stopped_refusal(path: Path, version: str) -> None:
    for acknowledged in (None, OPERATOR):
        with pytest.raises(ProbeReportError) as info:
            read_probe_report(path, warnings_acknowledged_by=acknowledged)
        text = str(info.value)
        assert "firmware gate stopped the run" in text and "branch B4" in text, text
        assert version in text and "01.02.00" in text and "nothing is imported" in text, text
    with pytest.raises(ProbeReportError, match="firmware gate"):
        import_probe_report(path)


def test_report_the_probe_stopped_on_old_firmware_is_refused() -> None:
    # The hotfixed probe on firmware older than 01.02.00: it sent nothing (exit code 4),
    # so there is nothing to record, and no gate may open on it.
    report = _report("gfw-01.01.09")
    assert report["result"] == "stopped" and report["exit_code"] == 4
    assert report["firmware_gate"]["stopped"] is True and report["firmware_gate"]["verdict"] == "older"
    assert report["sweep"] == [] and report["errors"] == []
    _assert_stopped_refusal(report_path("gfw-01.01.09"), "01.01.09")


def test_report_the_live_probe_stopped_on_old_firmware_is_refused(tmp_path: Path) -> None:
    generator = load_generator()
    live = generator.run_scenario("gfw-01.01.09", tmp_path / "live")
    _assert_stopped_refusal(live, "01.01.09")
    gate = json.loads(live.read_text(encoding="utf-8"))["firmware_gate"]
    assert gate == _report("gfw-01.01.09")["firmware_gate"]


def test_shareable_copy_is_refused() -> None:
    path = report_path("gfw-01.01.09").with_name("goliath-probe-20261104T090000Z.shareable.json")
    assert "shareable_copy" in json.loads(path.read_text(encoding="utf-8"))
    with pytest.raises(ProbeReportError, match="shareable copy") as info:
        read_probe_report(path, warnings_acknowledged_by=OPERATOR)
    assert REPORT_NAME in str(info.value)  # it names the raw report to import instead


def test_import_slot_release_keepalive_and_idle_frames(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # Frames at idle (FW-08, U-52): the simulator pushed one position frame on S3
    # while the sweep listened, before any command.
    imported = read_probe_report(report_path("gfw-01.02.00-idle-frame"))
    frames = _by_id(imported.facts)["frames.at_idle"]
    assert frames.value["unsolicited"] == 1
    assert list(frames.value["connections"]) == ["S3"]
    assert "not evidence" in frames.note  # restrict-only: it never opens frames_end_upload

    # The slot-release sweep and the idle-keepalive session (FW-10, rung R1b) are
    # sections the frozen probe does not write: no fact, and the import says so.
    assert set(ABSENT_UNTIL_R1B) == {"slot.release_wait_s", "link.keepalive_ok"}
    for fid in ABSENT_UNTIL_R1B:
        assert fid not in {f.id for f in imported.facts}
        assert fid in imported.absent
        assert "R1b" in imported.absent[fid]
    with caplog.at_level(logging.WARNING, logger="goliath_run.facts"):
        import_probe_report(report_path("gfw-01.02.00"))
    logged = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "slot.release_wait_s" in logged and "link.keepalive_ok" in logged

    # A report that carries either section in a form this version does not know
    # is refused as a whole, never guessed at.
    for section in ("slot_release", "keepalive"):
        copy = _copy("gfw-01.02.00", tmp_path / section, edit=lambda d, s=section: d.__setitem__(s, {}))
        with pytest.raises(ProbeReportError, match=section):
            read_probe_report(copy)


def test_warnings_need_acknowledgement() -> None:
    path = report_path("gfw-01.02.00-extended-warning")
    assert _report("gfw-01.02.00-extended-warning")["exit_code"] == 3
    with pytest.raises(ProbeReportError, match="warning"):
        read_probe_report(path)
    with pytest.raises(ValueError):
        read_probe_report(path, warnings_acknowledged_by="   ")
    imported = read_probe_report(path, warnings_acknowledged_by=OPERATOR)
    go = _by_id(imported.facts)["rung.r1.go"]
    assert OPERATOR in go.note and "--extended was given" in go.note
    assert "exit code 3" in go.note


def test_routing_anomaly_is_refused() -> None:
    # Branch B5: another client held slot 0. Nothing is imported.
    with pytest.raises(ProbeReportError) as info:
        read_probe_report(report_path("gfw-01.02.00-slot0-occupied"))
    text = str(info.value)
    assert "B5" in text and "SLOT ROUTING" in text


def test_terminator_other_than_none_is_a_no_go_on_every_branch() -> None:
    # Design 11.2 R1: the go criteria include "terminator none". The simulator's newer-firmware
    # model reading commands line by line answers only INFO\n: the probe chooses lf and exits 0.
    # That run is a no-go on every branch, B3 included (where no fingerprint image would catch
    # it): nothing is imported. Its "none" variant went unanswered because the robot waited for
    # a line end, so it says nothing about unknown commands either.
    report = _report("vendor-app-newer-line-framing")
    assert report["chosen_terminator"] == "lf"
    assert report["exit_code"] == 0 and report["firmware_version"] == "01.02.03"
    with pytest.raises(ProbeReportError, match="terminator") as info:
        read_probe_report(report_path("vendor-app-newer-line-framing"))
    text = str(info.value)
    assert "'lf'" in text and "none" in text and "design review" in text
    with pytest.raises(ProbeReportError, match="terminator"):
        import_probe_report(report_path("vendor-app-newer-line-framing"))


def test_bytes_after_an_unknown_command_are_no_silence(tmp_path: Path) -> None:
    # Uncertain evidence may restrict, never permit (design 3.1): bytes the probe could not
    # frame after a variant the robot does not know (the probe discards them and warns) are a
    # reply of some kind, not silence, even when a named person has read the warning.
    def garble(d: dict) -> None:
        for session in d["sessions"]:
            if session["name"] == "terminator test 'lf'":
                session["events"].insert(-1, {
                    "t": 3.5, "session": session["name"], "conn": "S1", "phase": "after INFO",
                    "type": "discarded", "reason": "no message starts with these bytes", "length": 3,
                    "raw": "ff fe 01"})
        d["warnings"].append("terminator test 'lf': 3 byte(s) on S1 could not be framed and were discarded "
                             "(no message starts with these bytes); they are in the capture file")
        d["result"], d["exit_code"] = "partial", 3

    imported = read_probe_report(_copy("gfw-01.02.00", tmp_path, edit=garble), warnings_acknowledged_by=OPERATOR)
    value = _by_id(imported.facts)["fingerprint.unknown_command"].value
    assert value != "silent"
    assert value.startswith("reply:") and "discarded" in value
    assert imported.branch == "B2"


@pytest.mark.parametrize(
    "edit, match",
    [
        (lambda d: d.__setitem__("tool", "something else"), "goliath_link.probe"),
        (lambda d: d.__setitem__("report_format", 3), "report_format"),
        (lambda d: d.__setitem__("interrupted", True), "interrupted"),
        (lambda d: d.__setitem__("completed", False), "complete"),
        (lambda d: d.__setitem__("firmware_version", None), "firmware version"),
        (lambda d: d.__setitem__("started_utc", "yesterday"), "started_utc"),
        (lambda d: d.__setitem__("chosen_terminator", None), "terminator"),
        (lambda d: d.__setitem__("shareable_copy", {}), "shareable copy"),
        (lambda d: d.__setitem__("firmware_gate", {"stopped": True, "verdict": "unknown", "reason": "x"}),
         "firmware gate"),
        (lambda d: d.__setitem__("firmware_gate", {}), "firmware gate"),
        (lambda d: d.__setitem__("exit_code", 4), "firmware gate"),
        (lambda d: d.__setitem__("result", "stopped"), "firmware gate"),
    ],
)
def test_report_that_is_not_a_go_is_refused(tmp_path: Path, edit, match: str) -> None:
    with pytest.raises(ProbeReportError, match=match):
        read_probe_report(_copy("gfw-01.02.00", tmp_path, edit=edit))


def test_two_serials_in_one_report_are_refused(tmp_path: Path) -> None:
    def second_robot(d: dict) -> None:
        event = d["sweep"][0]["replies"][0]
        event["params"][3] = "000000000002"

    with pytest.raises(ProbeReportError, match="serial"):
        read_probe_report(_copy("gfw-01.02.00", tmp_path, edit=second_robot))


def test_two_firmware_versions_in_one_report_are_refused(tmp_path: Path) -> None:
    # The probe only warns when identity lines disagree; facts cannot be bound to either version.
    def reflashed(d: dict) -> None:
        d["passive"][0]["params"][0] = "GFW 01.02.03   "

    with pytest.raises(ProbeReportError, match="01.02.03"):
        read_probe_report(_copy("gfw-01.02.00", tmp_path / "a", edit=reflashed), warnings_acknowledged_by=OPERATOR)

    def mismatch(d: dict) -> None:
        d["firmware_version"] = "01.02.03"

    with pytest.raises(ProbeReportError, match="identity"):
        read_probe_report(_copy("gfw-01.02.00", tmp_path / "b", edit=mismatch))


def test_unreadable_reports_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ProbeReportError, match="cannot read"):
        read_probe_report(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProbeReportError, match="JSON"):
        read_probe_report(bad)
    listed = tmp_path / "list.json"
    listed.write_text("[]", encoding="utf-8")
    with pytest.raises(ProbeReportError, match="object"):
        read_probe_report(listed)


def test_missing_capture_is_disclosed_not_hidden(tmp_path: Path) -> None:
    path = _copy("gfw-01.02.00", tmp_path)
    capture = path.parent / "goliath-probe-20261104T090000Z.capture.txt"
    capture.unlink()
    imported = read_probe_report(path)
    assert any("capture" in line and "not found" in line for line in imported.notes)
    for fact in imported.facts:
        assert any("capture" in e and "not found" in e for e in fact.evidence)


def test_capture_named_with_a_windows_path(tmp_path: Path) -> None:
    # The probe may have run on Windows: the capture is still found next to the report.
    name = "goliath-probe-20261104T090000Z.capture.txt"
    path = _copy("gfw-01.02.00", tmp_path, edit=lambda d: d.__setitem__("capture_file", "C:\\probe\\" + name))
    capture_sha = hashlib.sha256((path.parent / name).read_bytes()).hexdigest()
    imported = read_probe_report(path)
    assert all(any(capture_sha in e for e in f.evidence) for f in imported.facts)
    assert not imported.notes


def _essence(facts: tuple[Fact, ...]) -> dict[str, tuple]:
    return {f.id: (f.value, f.basis, f.unknown_id, f.rung, f.serial, f.firmware) for f in facts}


def test_probe_report_import_live_matches_committed(tmp_path: Path) -> None:
    """The probe and the simulator of this checkout still produce what the committed report holds."""
    generator = load_generator()
    path = generator.run_scenario("gfw-01.02.00", tmp_path / "live")
    live = read_probe_report(path)
    committed = read_probe_report(report_path("gfw-01.02.00"))

    assert _essence(live.facts) == _essence(committed.facts)
    assert live.branch == committed.branch == "B1"
    # The firmware gate was open; the shareable copy next to the report is not importable.
    assert json.loads(path.read_text(encoding="utf-8"))["firmware_gate"]["verdict"] == "ok"
    with pytest.raises(ProbeReportError, match="shareable copy"):
        read_probe_report(path.with_name(path.name[: -len(".json")] + ".shareable.json"))


@pytest.mark.parametrize("scenario, acknowledged, branch", [
    ("vendor-app-newer-extended", None, "B3"),      # --extended on newer firmware
    ("gfw-01.02.00-extended-warning", OPERATOR, "B1"),  # --extended refused on 01.02.00, a warning
])
def test_hotfixed_probe_imports_as_the_frozen_one_did(tmp_path: Path, scenario: str, acknowledged, branch) -> None:
    """On 01.02.00 and newer the hotfixed probe sends what the frozen one did: the same facts."""
    generator = load_generator()
    live = read_probe_report(generator.run_scenario(scenario, tmp_path / "live"),
                             warnings_acknowledged_by=acknowledged)
    committed = read_probe_report(report_path(scenario), warnings_acknowledged_by=acknowledged)
    assert _essence(live.facts) == _essence(committed.facts)
    assert live.branch == committed.branch == branch


def test_every_committed_scenario_has_a_report() -> None:
    generator = load_generator()
    for name in generator.SCENARIOS:
        assert (PROBE_REPORTS / name / REPORT_NAME).is_file(), name
