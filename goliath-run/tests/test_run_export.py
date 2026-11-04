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
import logging
import zipfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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


def test_export_redacts_values_seen_only_in_decoded_events(tmp_path: Path,
                                                           caplog: pytest.LogCaptureFixture) -> None:
    # The raw bytes were not kept (the recorder cap), but the decoded events were.
    rec = JobRecord.create(tmp_path / "sessions", "job-0002", policy=POLICY)
    rec.event(LinkEvent(1, 1.0, parse_text_message("INFO;31;%s" % NEIGHBOUR_SSID)))
    rec.event(LinkEvent(1, 2.0, parse_text_message("INFO;34;GFW 01.02.00;1;2;%s" % HEX_SERIAL)))
    (rec.dir / "private.json").unlink()  # even without the record's own note
    rec.finish("ABORTED_BEFORE_UPLOAD", [])
    with open(rec.dir / "events.jsonl", "ab") as f:  # odd lines: not an object, and a torn last line
        f.write(b"[1, 2]\n\"text\"\n{\"type\": \"TextMessage\", \"fie")
    with caplog.at_level(logging.WARNING, logger="goliath_run.export"):
        members = _members(export_diag(rec.dir, tmp_path / "out"))
    blob = b"\n".join(members.values())
    for secret in (NEIGHBOUR_SSID, HEX_SERIAL):
        for form in _forms(secret):
            assert form not in blob
    # Lines that are not event records were covered by the byte patterns only: counted and said, never silent.
    manifest = json.loads(members["REDACTION.json"])
    assert manifest["event_lines_not_parsed"] == 3
    assert any("3 line(s)" in r.getMessage() and "events.jsonl" in r.getMessage() for r in caplog.records)


def test_export_masks_short_values_in_decoded_event_fields(tmp_path: Path) -> None:
    # A network name shorter than MIN_VALUE_LENGTH is not masked everywhere (it would destroy unrelated
    # data), but where the protocol carries it, in the wire bytes and in the decoded event, it is.
    short = "Ab3"
    assert len(short) < E.MIN_VALUE_LENGTH
    rec = JobRecord.create(tmp_path / "sessions", "job-0005", policy=POLICY)
    rec.recorder(1, "<", ("INFO;31;%s\n" % short).encode(), 1.0)
    rec.event(LinkEvent(1, 1.0, parse_text_message("INFO;31;%s" % short)))
    rec.finish("ABORTED_BEFORE_UPLOAD", [])
    members = _members(export_diag(rec.dir, tmp_path / "out"))
    root = rec.dir.name
    assert members[root + "/wire/S2.bin"] == b"INFO;31;***\n"
    (entry,) = [json.loads(line) for line in members[root + "/events.jsonl"].splitlines()]
    assert entry["fields"]["params"] == ["***"] and entry["fields"]["raw"] == "INFO;31;***"
    assert entry["type"] == "TextMessage" and entry["fields"]["code"] == 31  # the rest is kept
    assert json.loads(members["REDACTION.json"])["short_values_masked_in_protocol_fields_only"] == 1


@pytest.mark.parametrize("ssid", ['Cafe "Corner" Net', "Back\\slash-Net", 'Q"', "Semi;colon;Net"])
def test_export_masks_network_names_with_quotes_backslashes_and_separators(tmp_path: Path, ssid: str) -> None:
    rec = JobRecord.create(tmp_path / "sessions", "job-0006", policy=POLICY)
    rec.recorder(1, "<", ("INFO;31;START\nINFO;31;%s\nINFO;31;END\n" % ssid).encode().ljust(512, b"\x00"), 1.0)
    rec.event(LinkEvent(1, 1.0, parse_text_message("INFO;31;%s" % ssid)))
    rec.finish("ABORTED_BEFORE_UPLOAD", [])
    members = _members(export_diag(rec.dir, tmp_path / "out"))
    s2 = members[rec.dir.name + "/wire/S2.bin"]
    assert s2.startswith(b"INFO;31;START\nINFO;31;" + b"*" * len(ssid.encode()) + b"\nINFO;31;END\n")
    blob = b"\n".join(members.values())
    for form in _forms(ssid) + [json.dumps(ssid)[1:-1].encode()]:
        if len(form) >= 2:
            assert form not in blob, form


def test_export_masks_values_written_in_python_repr_form(tmp_path: Path) -> None:
    # Log lines carry messages in Python's repr form (the client logs '... failed on %r' with the event):
    # a network name holding both quote characters, or a control character, is escaped differently there
    # than as written or in JSON. Every part of it must be gone all the same, and the read-back must check it.
    quotes = 'Fake\'s "Zebra" Net'
    control = "Fake\x07Bell-Net"
    rec = JobRecord.create(tmp_path / "sessions", "job-0009", policy=POLICY)
    events = [LinkEvent(1, 1.0 + i, parse_text_message("INFO;31;%s" % s)) for i, s in enumerate((quotes, control))]
    rec.recorder(1, "<", "".join("INFO;31;%s\n" % s for s in (quotes, control)).encode().ljust(512, b"\x00"), 1.0)
    for ev in events:
        rec.event(ev)
    log_text = "".join("ERROR goliath client: event observer failed on %r\n" % (ev,) for ev in events)
    assert "Zebra" in log_text and "Bell-Net" in log_text and '\\"' not in log_text  # repr, not JSON
    rec.write_file("runner.log", log_text.encode("utf-8"))
    rec.write_file("runner.jsonl", (json.dumps({"msg": log_text}) + "\n").encode("utf-8"))  # repr inside JSON
    rec.finish("ABORTED_BEFORE_UPLOAD", [])
    members = _members(export_diag(rec.dir, tmp_path / "out"))
    for name, data in members.items():
        for part in (b"Zebra", b"Bell-Net"):
            assert part not in data, (name, part, data[:400])


def test_export_read_back_checks_the_repr_form(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    quotes = 'Fake\'s "Zebra" Net'
    rec = JobRecord.create(tmp_path / "sessions", "job-0010", policy=POLICY)
    ev = LinkEvent(1, 1.0, parse_text_message("INFO;31;%s" % quotes))
    rec.event(ev)
    # Only the repr form of the name, without the protocol prefix, so nothing but that form can be found.
    rec.write_file("runner.log", ("failed on params=%r\n" % (ev.event.params,)).encode("utf-8"))
    rec.finish("ABORTED_BEFORE_UPLOAD", [])
    real = E.redact_bytes

    def skip_runner_log(data: bytes, values: Any) -> tuple[bytes, int]:
        return (data, 0) if data.startswith(b"failed on ") else real(data, values)

    monkeypatch.setattr(E, "redact_bytes", skip_runner_log)
    with pytest.raises(ExportRedactionError, match="runner.log"):
        export_diag(rec.dir, tmp_path / "out")


# Half of the characters are the ones that end a field somewhere: quotes, backslashes, separators, spaces.
_SSID_CHARS = st.one_of(st.sampled_from('"\\;=& %{}[],:'),
                        st.characters(min_codepoint=32, max_codepoint=0x24F, blacklist_categories=("Cs", "Cc")))
_FAKE_SSID = st.builds(lambda tail: "Fake-" + tail, st.text(alphabet=_SSID_CHARS, max_size=20)
                       ).filter(lambda s: s.strip() == s)
_FAKE_SERIAL = st.text(alphabet="0123456789ABCDEF", min_size=8, max_size=8).map(lambda s: "TEST" + s)


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ssids=st.lists(_FAKE_SSID, min_size=1, max_size=4, unique=True), serial=_FAKE_SERIAL)
def test_property_generated_fake_values_never_leave_the_laptop(tmp_path_factory: pytest.TempPathFactory,
                                                               ssids: list[str], serial: str) -> None:
    # Generated values, obviously fake (lead note g): every form of every serial and network name is gone
    # from the archive, wherever the session recorded it.
    root = tmp_path_factory.mktemp("prop")
    rec = JobRecord.create(root / "sessions", "job-0007", policy=POLICY)
    scan = "INFO;31;START\n" + "".join("INFO;31;%s\n" % s for s in ssids) + "INFO;31;END\n"
    rec.recorder(1, "<", ("INFO;34;GFW 01.02.00   ;1;2;%s\n" % serial).encode().ljust(512, b"\x00"), 1.0)
    rec.recorder(1, "<", scan.encode("utf-8"), 2.0)
    for t, line in enumerate(["INFO;34;GFW 01.02.00   ;1;2;%s" % serial] + scan.splitlines()):
        rec.event(LinkEvent(1, 10.0 + t, parse_text_message(line)))
    rec.operator("K1", "Laptop on %s" % ssids[0], "tick")
    rec.write_file("runner.log", ("robot %s near %s\n" % (serial, " / ".join(ssids))).encode("utf-8"))
    rec.finish("ABORTED_BEFORE_UPLOAD", [])
    members = _members(export_diag(rec.dir, root / "out"))
    blob = b"\n".join(members.values()) + "\n".join(members).encode("utf-8")
    for secret in [serial] + ssids:
        for form in _forms(secret) + [json.dumps(secret)[1:-1].encode("utf-8")]:
            assert form not in blob, (secret, form)
    # Not even part of a value is left: the wire capture is exactly the original with each value starred...
    stars = "INFO;31;START\n" + "".join("INFO;31;%s\n" % ("*" * len(s.encode("utf-8"))) for s in ssids) + \
        "INFO;31;END\n"
    expected = ("INFO;34;GFW 01.02.00   ;1;2;%s\n" % ("*" * len(serial))).encode().ljust(512, b"\x00") + stars.encode()
    assert members[rec.dir.name + "/wire/S2.bin"] == expected
    # ...and every decoded identity and scan record has its private fields starred.
    for line in members[rec.dir.name + "/events.jsonl"].splitlines():
        fields = json.loads(line)["fields"]
        private = fields["params"][3:] if fields["code"] == 34 else fields["params"]
        if fields["code"] == 31 and fields["params"] in (["START"], ["END"]):
            continue
        assert private and all(set(p) <= {"*"} for p in private), fields


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


def test_export_read_back_catches_a_short_value_left_in_a_decoded_record(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    # A value shorter than MIN_VALUE_LENGTH is not searched for everywhere; the read-back still checks the
    # decoded scan records field by field.
    rec = JobRecord.create(tmp_path / "sessions", "job-0008", policy=POLICY)
    rec.event(LinkEvent(1, 1.0, parse_text_message("INFO;31;Ab3")))
    rec.finish("ABORTED_BEFORE_UPLOAD", [])
    monkeypatch.setattr(E, "_mask_events", lambda data, **kwargs: (data, 0, 0))
    with pytest.raises(ExportRedactionError, match="decoded record") as info:
        export_diag(rec.dir, tmp_path / "out")
    assert "Ab3" not in str(info.value)
    assert not (tmp_path / "out").exists() or list((tmp_path / "out").iterdir()) == []


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
    out_scan, n = E.redact_patterns(b"INFO;31;A\nINFO;31;START\n", serial=True, ssids=True, wire=False)
    assert out_scan == b"INFO;31;*\nINFO;31;START\n" and n == 1
    # In raw link bytes a network name runs to the end of its line, quotes and backslashes included; in
    # text files (JSON) a quote ends it.
    raw = b'INFO;31;Q"x\\y\n\x00\x00'
    assert E.redact_patterns(raw, serial=True, ssids=True, wire=True)[0] == b"INFO;31;*****\n\x00\x00"
    assert E.redact_patterns(b'{"raw": "INFO;31;Q\\"x"}', serial=True, ssids=True, wire=False)[0] == \
        b'{"raw": "INFO;31;*\\"x"}'
    with pytest.raises(TypeError):
        E.redact_patterns(raw, serial=True, ssids=True)  # type: ignore[call-arg]  # the context is never implicit
