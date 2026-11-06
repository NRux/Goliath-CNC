# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The importer never prints or stores the robot's serial number or nearby Wi-Fi names.

The probe's own messages (its errors, warnings and verdicts) may quote what the
robot sent: a slot-routing error quotes the first misrouted message, which on a
robot with an occupied slot is the identity line, serial number included; an
unreadable identity is quoted in full. The probe masks those in its summary and
its shareable copies. The importer reads the raw report, so whatever it shows in
a refusal, stores in a fact's note or logs from those messages goes through the
same masking (the probe's own redaction), or is withheld, visibly, if that fails.

Every report here was written by goliath-link's probe against its simulator
(fixtures/probe_reports/); edited copies only add a message that quotes the
identity line, the way the probe's own errors do. The serial is the
simulator's placeholder, never a real one.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest
from run_facts_support import OPERATOR, REPORT_NAME, SIM_SERIAL, report_path

from goliath_run.facts import ProbeReportError, read_probe_report

MASKED = "X" * len(SIM_SERIAL)
# The identity line as the simulator sends it (the probe quotes it this way when it
# cannot parse it, and in a slot-routing error).
IDENTITY = "INFO;34;GFW 01.02.00   ;1;2;" + SIM_SERIAL


def _copy(scenario: str, tmp_path: Path, edit) -> Path:
    """A copy of a committed report and its capture, with ``edit`` applied to the report."""
    src = report_path(scenario)
    dst = tmp_path / scenario
    dst.mkdir(parents=True)
    for f in src.parent.iterdir():
        if f.is_file():
            shutil.copy(f, dst / f.name)
    data = json.loads((dst / REPORT_NAME).read_text(encoding="utf-8"))
    edit(data)
    (dst / REPORT_NAME).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return dst / REPORT_NAME


def _refusal(path: Path, **kwargs) -> str:
    with pytest.raises(ProbeReportError) as info:
        read_probe_report(path, **kwargs)
    return str(info.value)


def test_the_fixture_quotes_the_serial_so_these_tests_can_fail() -> None:
    # Guard against a vacuous suite: the B5 report's own errors quote the identity line.
    data = json.loads(report_path("gfw-01.02.00-slot0-occupied").read_text(encoding="utf-8"))
    assert any(SIM_SERIAL in str(e) for e in data["errors"])


def test_slot_routing_refusal_masks_the_serial() -> None:
    # Branch B5: the refusal still says what went wrong, with the serial masked.
    text = _refusal(report_path("gfw-01.02.00-slot0-occupied"))
    assert "B5" in text and "SLOT ROUTING" in text and "instead of S2" in text
    assert SIM_SERIAL not in text
    assert MASKED in text


def test_error_refusal_masks_a_quoted_identity_line(tmp_path: Path) -> None:
    def edit(r: dict) -> None:
        r["errors"] = ["firmware version could not be parsed: the identity line %r gives no version" % IDENTITY]
        r["exit_code"] = 1

    text = _refusal(_copy("gfw-01.02.00", tmp_path, edit))
    assert "the probe reported errors" in text and "could not be parsed" in text
    assert SIM_SERIAL not in text
    assert MASKED in text


def test_warning_refusal_masks_a_quoted_identity_line(tmp_path: Path) -> None:
    def edit(r: dict) -> None:
        r["warnings"] = list(r["warnings"]) + ["an identity line arrived after the reply window: %r" % IDENTITY]

    text = _refusal(_copy("gfw-01.02.00-extended-warning", tmp_path, edit))
    assert "warning(s)" in text and "arrived after the reply window" in text
    assert SIM_SERIAL not in text
    assert MASKED in text


def test_acknowledged_warnings_are_masked_in_the_stored_notes(tmp_path: Path) -> None:
    # The go fact's note quotes the warnings; notes are stored with the facts, logged and
    # shown by the runner. The serial stays bound to every fact in its own field.
    def edit(r: dict) -> None:
        r["warnings"] = list(r["warnings"]) + ["an identity line arrived after the reply window: %r" % IDENTITY]

    imported = read_probe_report(_copy("gfw-01.02.00-extended-warning", tmp_path, edit),
                                 warnings_acknowledged_by=OPERATOR)
    go = next(f for f in imported.facts if f.id == "rung.r1.go")
    assert "arrived after the reply window" in go.note and MASKED in go.note
    texts = [f.note for f in imported.facts] + list(imported.notes) + list(imported.absent.values())
    assert not [t for t in texts if SIM_SERIAL in t]
    assert all(f.serial == SIM_SERIAL for f in imported.facts)


def test_masking_failure_withholds_the_messages(monkeypatch: pytest.MonkeyPatch,
                                                caplog: pytest.LogCaptureFixture) -> None:
    # If the masking cannot be done, the probe's messages are withheld, never shown unmasked,
    # and the refusal and the log say so (owner rule: visible, never silent).
    import goliath_link.probe as probe

    class Broken:
        def collect_received(self, data: bytes, conn: object = None) -> None:
            raise RuntimeError("broken redactor")

        def collect_report(self, report: dict) -> None:
            raise RuntimeError("broken redactor")

    monkeypatch.setattr(probe, "_Redactor", Broken)
    with caplog.at_level(logging.WARNING, logger="goliath_run.facts"):
        text = _refusal(report_path("gfw-01.02.00-slot0-occupied"))
    assert "B5" in text
    assert SIM_SERIAL not in text
    assert "withheld" in text and "RuntimeError" in text
    assert any("withheld" in rec.getMessage() for rec in caplog.records)
    assert not [rec for rec in caplog.records if SIM_SERIAL in rec.getMessage()]


def test_the_probe_redaction_this_relies_on_is_still_there() -> None:
    # The importer uses the frozen probe's own (private) redaction, so that both mask the
    # same way. If the probe's B4 rework renames or reshapes it, this fails here, loudly,
    # rather than the importer printing serial numbers again.
    from goliath_link.probe import _received_streams, _Redactor

    redactor = _Redactor()
    for method in ("collect_received", "collect_report", "text"):
        assert callable(getattr(redactor, method, None)), method
    assert _received_streams(Path("no such capture file")) == {}
