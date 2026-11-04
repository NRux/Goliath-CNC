# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The diagnostic export (unit R02; the project design, section 7.9).

Owners attach these archives to public bug reports, so the robot's serial
number and every Wi-Fi network name (SSID) are redacted by default, and a
provisioning password never leaves the laptop. The test data is obviously fake
(lead note g): serial ``TEST-SERIAL-0001``, SSIDs ``Goliath_TEST00`` and
``Example-Neighbour-Net``.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from goliath_link.protocol import parse_text_message
from goliath_link.transport import LinkEvent
from goliath_run import export as E
from goliath_run.export import ExportRedactionError, export_diag, redact_bytes
from goliath_run.record import JobRecord, RecorderPolicy

SERIAL = "TEST-SERIAL-0001"
HEX_SERIAL = "0A0B0C0D0E0F"  # the 01.02.00 form: 12 hex digits (fake)
ROBOT_SSID = "Goliath_TEST00"
NEIGHBOUR_SSID = "Example-Neighbour-Net"
PASSWORD = "not-a-real-password"
POLICY = RecorderPolicy(max_session_mb=16, keep_sessions=3, source="test policy")


def _hex(text: str) -> str:
    return text.encode("utf-8").hex().upper()


def _session(tmp_path: Path) -> JobRecord:
    rec = JobRecord.create(tmp_path / "sessions", "job-0001", policy=POLICY)
    identity = ("INFO;34;GFW 01.02.00   ;1;2;%s\n" % SERIAL).encode()
    identity_hex = ("INFO;34;GFW 01.02.00   ;1;2;%s\n" % HEX_SERIAL).encode()
    scan = ("INFO;31;START\nINFO;31;%s\nINFO;31;%s\nINFO;31;END\n" % (ROBOT_SSID, NEIGHBOUR_SSID)).encode()
    provisioning = ("SSID=%s&PWD=%s&AUTH_TYPE=2" % (_hex(NEIGHBOUR_SSID), _hex(PASSWORD))).encode()
    rec.recorder(1, "<", identity.ljust(512, b"\x00"), 1.0)
    rec.recorder(1, "<", identity_hex.ljust(512, b"\x00"), 1.1)
    rec.recorder(1, "<", scan, 2.0)
    rec.recorder(0, ">", provisioning, 3.0)
    for t, line in enumerate(identity.decode().splitlines() + scan.decode().splitlines()):
        rec.event(LinkEvent(1, 10.0 + t, parse_text_message(line)))
    rec.operator("K1", "Laptop on %s" % ROBOT_SSID, "tick")
    rec.write_file("certificate.json", json.dumps({"serial": SERIAL, "firmware": "01.02.00"}).encode())
    rec.write_file("runner.log", ("connected to robot %s (lowercase %s) on %s\n"
                                  % (SERIAL, HEX_SERIAL.lower(), ROBOT_SSID)).encode())
    rec.note_private("serial", SERIAL)
    rec.finish("ABORTED_BEFORE_UPLOAD", ["test session on %s" % ROBOT_SSID])
    return rec


def _members(zip_path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(zip_path) as z:
        return {name: z.read(name) for name in z.namelist()}


def _forms(value: str) -> list[bytes]:
    raw = value.encode("utf-8")
    return [raw, raw.lower(), raw.upper(), raw.hex().encode(), raw.hex().upper().encode()]


def test_export_redacts_serial_and_ssids(tmp_path: Path) -> None:
    rec = _session(tmp_path)
    out = export_diag(rec.dir, tmp_path / "out")
    assert out.parent == tmp_path / "out" and out.suffix == ".zip"
    members = _members(out)
    blob = b"\n".join(members.values()) + "\n".join(members).encode()
    for secret in (SERIAL, HEX_SERIAL, ROBOT_SSID, NEIGHBOUR_SSID, PASSWORD):
        for form in _forms(secret):
            assert form not in blob, "%r leaked in the export" % secret
    root = rec.dir.name
    # The private file never leaves the laptop; the session's other files do.
    assert root + "/private.json" not in members
    assert {root + "/wire/S1.bin", root + "/wire/S2.bin", root + "/events.jsonl", root + "/summary.json",
            root + "/certificate.json", root + "/runner.log"} <= set(members)
    # Masking keeps lengths, so the wire index still locates every chunk.
    for n in (1, 2):
        name = "%s/wire/S%d.bin" % (root, n)
        assert len(members[name]) == len((rec.dir / "wire" / ("S%d.bin" % n)).read_bytes())
    assert members[root + "/wire/index.jsonl"] == (rec.dir / "wire" / "index.jsonl").read_bytes()
    # What is kept is still useful: the firmware version, the scan structure.
    s2 = members[root + "/wire/S2.bin"]
    assert b"INFO;34;GFW 01.02.00   ;1;2;" in s2 and b"INFO;31;START" in s2 and b"INFO;31;END" in s2
    assert b"AUTH_TYPE=2" in members[root + "/wire/S1.bin"]
    # The manifest says what was done, without the values.
    manifest = json.loads(members["REDACTION.json"])
    assert manifest["serial_redacted"] is True and manifest["ssids_redacted"] is True
    assert manifest["passwords_redacted"] is True
    assert manifest["values"]["serial"] >= 2 and manifest["values"]["ssid"] >= 2
    assert "private.json" in " ".join(manifest["excluded"])
    # The session is marked exported (for the retention policy), without the output path.
    marks = (rec.dir / "exports.jsonl").read_text(encoding="utf-8")
    assert out.name in marks and str(out.parent) not in marks


def test_export_without_redaction_keeps_values_but_never_the_password(tmp_path: Path) -> None:
    rec = _session(tmp_path)
    out = export_diag(rec.dir, tmp_path / "plain.zip", redact_serial=False, redact_ssids=False)
    assert out == tmp_path / "plain.zip"
    members = _members(out)
    blob = b"\n".join(members.values())
    assert SERIAL.encode() in blob and ROBOT_SSID.encode() in blob
    for form in _forms(PASSWORD):
        assert form not in blob
    manifest = json.loads(members["REDACTION.json"])
    assert manifest["serial_redacted"] is False and manifest["ssids_redacted"] is False
    assert manifest["passwords_redacted"] is True


def test_export_redacts_values_seen_only_in_decoded_events(tmp_path: Path) -> None:
    # The raw bytes were not kept (the recorder cap), but the decoded events were.
    rec = JobRecord.create(tmp_path / "sessions", "job-0002", policy=POLICY)
    rec.event(LinkEvent(1, 1.0, parse_text_message("INFO;31;%s" % NEIGHBOUR_SSID)))
    rec.event(LinkEvent(1, 2.0, parse_text_message("INFO;34;GFW 01.02.00;1;2;%s" % HEX_SERIAL)))
    (rec.dir / "private.json").unlink()  # even without the record's own note
    rec.finish("ABORTED_BEFORE_UPLOAD", [])
    blob = b"\n".join(_members(export_diag(rec.dir, tmp_path / "out")).values())
    for secret in (NEIGHBOUR_SSID, HEX_SERIAL):
        for form in _forms(secret):
            assert form not in blob


def test_extra_known_values_are_redacted(tmp_path: Path) -> None:
    rec = JobRecord.create(tmp_path / "sessions", "job-0003", policy=POLICY)
    rec.write_file("facts.json", json.dumps({"robot": "TEST-SERIAL-0009", "net": "Example-Home-Net"}).encode())
    rec.finish("ABORTED_BEFORE_UPLOAD", [])
    out = export_diag(rec.dir, tmp_path / "out", extra_serials=["TEST-SERIAL-0009"],
                      extra_ssids=["Example-Home-Net"])
    blob = b"\n".join(_members(out).values())
    assert b"TEST-SERIAL-0009" not in blob and b"Example-Home-Net" not in blob


def test_home_directory_is_masked_in_text_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_home = tmp_path / "home" / "example-user"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    rec = JobRecord.create(tmp_path / "sessions", "job-0004", policy=POLICY)
    rec.write_file("runner.log", ("bundle %s\n" % (fake_home / "jobs" / "a.zip")).encode())
    rec.finish("ABORTED_BEFORE_UPLOAD", [])
    blob = b"\n".join(_members(export_diag(rec.dir, tmp_path / "out")).values())
    assert b"example-user" not in blob and b"<HOME>" in blob


def test_export_verifies_and_refuses_a_leak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _session(tmp_path)

    def no_masking(data: bytes, *args: Any, **kwargs: Any) -> tuple[bytes, int]:
        return data, 0

    monkeypatch.setattr(E, "redact_bytes", no_masking)
    with pytest.raises(ExportRedactionError) as info:
        export_diag(rec.dir, tmp_path / "out")
    # The error names the file but never the value it found.
    assert SERIAL not in str(info.value) and ROBOT_SSID not in str(info.value)
    assert not (tmp_path / "out").exists() or list((tmp_path / "out").iterdir()) == []
    assert not (rec.dir / "exports.jsonl").exists()


def test_export_refuses_to_overwrite_and_checks_its_input(tmp_path: Path) -> None:
    rec = _session(tmp_path)
    out = export_diag(rec.dir, tmp_path / "a.zip")
    with pytest.raises(FileExistsError):
        export_diag(rec.dir, out)
    with pytest.raises(ValueError, match="session record"):
        export_diag(tmp_path, tmp_path / "b.zip")
    with pytest.raises(ValueError, match=".zip"):
        export_diag(rec.dir, tmp_path / "c.tar")


def test_redact_bytes_keeps_length_and_masks_every_form() -> None:
    data = ("id %s / %s / %s / hex %s\n" % (SERIAL, SERIAL.lower(), SERIAL.upper(), _hex(SERIAL))).encode()
    out, count = redact_bytes(data, {SERIAL})
    assert len(out) == len(data) and count == 4
    for form in _forms(SERIAL):
        assert form not in out
    assert out.startswith(b"id ") and b" / hex " in out
    # Short values are not masked everywhere (they would destroy unrelated data)...
    out_short, n = redact_bytes(b"A cat sat", {"A"})
    assert out_short == b"A cat sat" and n == 0
    # ...but they are masked where the protocol puts a network name.
    out_scan, n = E.redact_patterns(b"INFO;31;A\nINFO;31;START\n", serial=True, ssids=True)
    assert out_scan == b"INFO;31;*\nINFO;31;START\n" and n == 1
