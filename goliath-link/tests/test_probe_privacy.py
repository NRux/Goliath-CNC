# SPDX-License-Identifier: Apache-2.0
"""The probe never prints the robot's serial number or nearby Wi-Fi names, and writes shareable copies.

The owner of the first robot probed pasted the probe's terminal summary into a
chat and published the raw files; both carried the serial and the names of
nearby networks (GFW 01.00.13 lists them on every connect), which can locate
the robot. Here a fake robot pushes a synthetic serial and synthetic network
names (old_firmware_robot.py): none may reach stdout or stderr or the
shareable files, the shareable files must keep every message's length and the
512-byte framing, and the raw files must still hold everything.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from old_firmware_robot import HOST, SERIAL, SSID_TEXTS, SSIDS, OldFirmwareRobot

import goliath_link
from goliath_link import probe
from goliath_link.probe import _Redactor, main, run_probe, ssid_placeholder
from goliath_link.sim import SimulatedGoliath

FAST_CLI = ["--listen", "0.5", "--reply-timeout", "0.8", "--pace", "0.3", "--slot-release-wait", "0.3"]
DATA_LINE = re.compile(r"^\s*([\d.]+)  ([<>])  (\S+)\s+(S\d)  ([0-9a-f ]*?)  \|  (.*)$")
SIM_SERIAL = "000000000001"


def secrets_text():
    """Every form in which the synthetic serial or a network name could be printed or written."""
    forms = {SERIAL}
    for name in SSID_TEXTS:
        forms |= {name, name.encode("unicode_escape").decode("ascii"), json.dumps(name)[1:-1]}
    forms |= {s.decode("utf-8") for s in SSIDS if s}  # what the name really was
    return forms


def assert_no_secrets(text, where):
    for secret in secrets_text():
        assert secret not in text, f"{secret!r} in {where}"


def files(out_dir: Path, stamp: str):
    base = out_dir / f"goliath-probe-{stamp}"
    return {k: Path(f"{base}{suffix}") for k, suffix in (
        ("report", ".json"), ("capture", ".capture.txt"),
        ("shareable_report", ".shareable.json"), ("shareable_capture", ".shareable.capture.txt"))}


def data_lines(path: Path):
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = DATA_LINE.match(line)
        if m:
            out.append((m.group(1), m.group(2), m.group(3), m.group(4),
                        bytes.fromhex(m.group(5)), m.group(6)))
    return out


def assert_same_framing(raw: Path, shared: Path):
    """Line for line the same time, direction, session, connection and byte count; NULs and newlines kept."""
    a, b = data_lines(raw), data_lines(shared)
    assert a and len(a) == len(b)
    for (t1, d1, s1, c1, x1, _), (t2, d2, s2, c2, x2, asc2) in zip(a, b):
        assert (t1, d1, s1, c1, len(x1)) == (t2, d2, s2, c2, len(x2))
        assert [i for i, v in enumerate(x1) if v in (0, 10, 13)] == [i for i, v in enumerate(x2) if v in (0, 10, 13)]
        assert asc2 == "".join(chr(v) if 32 <= v < 127 else "." for v in x2)
    raw_notes = [ln for ln in raw.read_text(encoding="utf-8").splitlines() if ln.startswith("#")]
    shared_notes = [ln for ln in shared.read_text(encoding="utf-8").splitlines() if ln.startswith("#")]
    assert len(shared_notes) == len(raw_notes) + 3  # the three-line SHAREABLE COPY header


@pytest.fixture
def old_robot():
    robot = OldFirmwareRobot()
    robot.start()
    yield robot
    robot.stop()


# --------------------------------------------------------------------------- #
# Placeholders
# --------------------------------------------------------------------------- #


def test_placeholders_follow_the_scrubbed_probe_results():
    assert ssid_placeholder(1, "TestNet-Alpha") == "SSID01_______"
    assert ssid_placeholder(2, "Cafe_5G") == "SSID02_"
    assert ssid_placeholder(12, "TestNet") == "SSID12_"
    assert ssid_placeholder(3, "Cafe01") == "SSID03"
    assert ssid_placeholder(1, "") == ""  # a hidden network stays empty


@pytest.mark.parametrize("number, name", [(3, "abc"), (1, "a"), (1, "1"), (2, "ab"), (1, "D01"), (1, "SSID01"),
                                          (7, "SSID07____"), (1, "###"), (1, "01")])
def test_placeholder_has_the_length_and_never_the_name(number, name):
    shown = ssid_placeholder(number, name)
    assert len(shown) == len(name) and name not in shown


def test_placeholder_avoids_other_names():
    assert ssid_placeholder(1, "abc", avoid=["D01"]) not in ("D01", "abc")
    assert ssid_placeholder(3, "abc") == "D03"  # the documented example


def test_redactor_masks_text_bytes_and_split_parameters():
    red = _Redactor()
    red.collect_report({"sessions": [{"events": [
        {"type": "text", "kind": "INFO", "code": 31, "raw": "INFO;31;Test;Guest", "params": ["Test", "Guest"]},
        {"type": "text", "kind": "INFO", "code": 34, "raw": "INFO;34;GFW 01.00.13 +b;2;1;" + SERIAL,
         "params": ["GFW 01.00.13 +b", "2", "1", SERIAL]},
    ]}]})
    red.collect_received(b"INFO;31;Test\xc3\xa9-Net\n\x00\x00")
    copy = red.report_copy({"warnings": ["the robot sent 'INFO;31;Test;Guest' on S2", "x " + SERIAL],
                            "e": {"type": "text", "kind": "INFO", "raw": "INFO;31;Test;Guest",
                                  "params": ["Test", "Guest"], "conn": "S2"},
                            "d": {"type": "discarded", "raw": (b"Test\xc3\xa9-Net").hex(" ")}})
    assert copy["warnings"] == ["the robot sent 'INFO;31;SSID01____' on S2", "x " + "X" * 24]
    assert copy["e"] == {"type": "text", "kind": "INFO", "raw": "INFO;31;SSID01____",
                         "params": ["SSID01____"], "conn": "S2"}
    assert bytes.fromhex(copy["d"]["raw"]) == b"SSID02____"
    assert red.data(b"..INFO;31;Test;Guest\n") == b"..INFO;31;SSID01____\n"
    assert red.text("Test��-Net") == "SSID02____"
    assert red.counts == (1, 2)


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #


def test_stopped_run_prints_and_shares_no_serial_or_network_name(old_robot, tmp_path, capsys):
    rc = main(["--host", HOST, "--port", str(old_robot.port), "--out", str(tmp_path), *FAST_CLI])
    captured = capsys.readouterr()
    assert rc == 4
    assert_no_secrets(captured.out, "stdout")
    assert_no_secrets(captured.err, "stderr")
    [report_path] = [p for p in tmp_path.glob("goliath-probe-*.json") if ".shareable." not in p.name]
    stamp = report_path.name[len("goliath-probe-"):-len(".json")]
    f = files(tmp_path, stamp)
    assert all(p.is_file() for p in f.values()), sorted(os.listdir(tmp_path))

    # The summary names the shareable files, and says what the raw ones hold.
    flat = " ".join(captured.out.split())
    assert "Safe to share" in flat
    assert str(f["shareable_report"]) in captured.out and str(f["shareable_capture"]) in captured.out
    assert "serial number" in flat and "Wi-Fi networks" in flat and "private" in flat
    assert "SSID01_" in captured.out

    # The shareable copies hold none of them, and keep every message's length.
    for key in ("shareable_report", "shareable_capture"):
        assert_no_secrets(f[key].read_text(encoding="utf-8"), f[key].name)
    shared_bytes = b"".join(x for *_, x, _a in data_lines(f["shareable_capture"]))
    for name in SSIDS:
        if name:
            assert name not in shared_bytes
    assert SERIAL.encode() not in shared_bytes
    assert_same_framing(f["capture"], f["shareable_capture"])
    s2 = b"".join(x for (_t, d, _s, conn, x, _a) in data_lines(f["shareable_capture"]) if d == "<" and conn == "S2")
    assert s2 and len(s2) % 512 == 0  # the robot's 512-byte blocks, all still whole
    blocks = [s2[i:i + 512] for i in range(0, len(s2), 512)]
    assert all(b.rstrip(b"\x00").endswith(b"\n") and b"\n" not in b.rstrip(b"\x00")[:-1] for b in blocks)

    raw = json.loads(f["report"].read_text(encoding="utf-8"))
    shared = json.loads(f["shareable_report"].read_text(encoding="utf-8"))
    assert "shareable_copy" in shared and "shareable_copy" not in raw
    assert shared["firmware_gate"]["stopped"] is True and shared["exit_code"] == 4
    raw_events = [e for s in raw["sessions"] for e in s["events"]]
    shared_events = [e for s in shared["sessions"] for e in s["events"]]
    assert [len(e.get("raw", "")) for e in raw_events] == [len(e.get("raw", "")) for e in shared_events]
    assert [e["raw"] for e in shared_events if str(e.get("raw")).startswith("INFO;31;")] == [
        "INFO;31;START", "INFO;31;SSID01_______", "INFO;31;SSID02_", "INFO;31;", "INFO;31;D03",
        "INFO;31;SSID04____", "INFO;31;SSID05____", "INFO;31;END"]

    # The raw files keep everything: they are the evidence.
    raw_text = f["report"].read_text(encoding="utf-8")
    assert SERIAL in raw_text
    for name in SSID_TEXTS:
        assert json.dumps(name)[1:-1] in raw_text
    raw_bytes = b"".join(x for *_, x, _a in data_lines(f["capture"]))
    assert SERIAL.encode() in raw_bytes
    for name in SSIDS:
        assert b"INFO;31;" + name + b"\n" in raw_bytes


def test_cli_process_prints_no_serial_or_network_name(old_robot, tmp_path):
    """Everything the process writes, warnings and tracebacks included, as an owner would paste it."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(goliath_link.__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONWARNINGS"] = "always"
    res = subprocess.run(
        [sys.executable, "-m", "goliath_link.probe", "--host", HOST, "--port", str(old_robot.port),
         "--out", str(tmp_path), "--extended", *FAST_CLI],
        env=env, timeout=120, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="backslashreplace",
    )
    assert res.returncode == 4, res.stdout + res.stderr
    assert_no_secrets(res.stdout, "stdout")
    assert_no_secrets(res.stderr, "stderr")
    assert "PROBE STOPPED" in res.stdout


def test_01_02_00_run_masks_the_serial_and_shares_the_same_framing(tmp_path, capsys):
    sim = SimulatedGoliath(HOST, 0, profile="gfw-01.02.00")
    sim.start()
    try:
        report, redactor = probe._run_probe(
            HOST, sim.port, str(tmp_path), terminator="none", listen=0.3, reply_timeout=0.8, pace=0.3,
            connect_timeout=2.0, slot_release_wait=0.3, extended=False, connect_gaps=(0.05, 0.05),
            settle=0.2, opener=probe._default_opener, stamp="gfw")
    finally:
        sim.stop()
    assert report["exit_code"] == 0, report["errors"] + report["warnings"]
    probe._print_summary(report, redactor)
    out = capsys.readouterr().out
    assert SIM_SERIAL not in out and "X" * len(SIM_SERIAL) in out
    f = files(tmp_path, "gfw")
    assert SIM_SERIAL not in f["shareable_report"].read_text(encoding="utf-8")
    assert SIM_SERIAL in f["report"].read_text(encoding="utf-8")
    assert_same_framing(f["capture"], f["shareable_capture"])
    shared = json.loads(f["shareable_report"].read_text(encoding="utf-8"))
    raw = json.loads(f["report"].read_text(encoding="utf-8"))
    # Apart from the serial, the shareable report is the report.
    assert shared.pop("shareable_copy") and shared.pop("privacy") != raw.pop("privacy")
    assert json.loads(json.dumps(shared).replace("X" * len(SIM_SERIAL), SIM_SERIAL)) == raw


def test_summary_without_a_redactor_masks_from_the_report(old_robot, tmp_path, capsys):
    # Callers of run_probe() that print the summary themselves get the same masking.
    report = run_probe(HOST, old_robot.port, str(tmp_path), stamp="lib", listen=0.5, reply_timeout=0.8,
                       pace=0.3, slot_release_wait=0.3, connect_gaps=(0.05, 0.05), settle=0.2)
    probe._print_summary(report)
    captured = capsys.readouterr()
    assert_no_secrets(captured.out + captured.err, "summary")
    assert "SSID01_" in captured.out


def test_a_redaction_failure_never_prints_robot_text(old_robot, tmp_path, capsys, monkeypatch):
    def broken(self, node):
        raise RuntimeError("would quote " + SERIAL)

    monkeypatch.setattr(probe._Redactor, "report_copy", broken)
    rc = main(["--host", HOST, "--port", str(old_robot.port), "--out", str(tmp_path), *FAST_CLI])
    captured = capsys.readouterr()
    assert rc == 4
    assert_no_secrets(captured.out + captured.err, "output")
    assert "do not share" in " ".join(captured.out.split()).lower()
    assert not list(tmp_path.glob("*.shareable.*"))
    [report_path] = list(tmp_path.glob("goliath-probe-*.json"))
    raw = json.loads(report_path.read_text(encoding="utf-8"))
    assert raw["shareable_report_file"] is None and "RuntimeError" in raw["shareable_error"]


# --------------------------------------------------------------------------- #
# Review of the hotfix: other identity layouts, odd network names, the probe's own text
# --------------------------------------------------------------------------- #


def _cli_run(robot, tmp_path, capsys, *extra):
    rc = main(["--host", HOST, "--port", str(robot.port), "--out", str(tmp_path), *FAST_CLI, *extra])
    captured = capsys.readouterr()
    [report_path] = [p for p in tmp_path.glob("goliath-probe-*.json") if ".shareable." not in p.name]
    stamp = report_path.name[len("goliath-probe-"):-len(".json")]
    return rc, captured, files(tmp_path, stamp)


def _shared_texts(f):
    capture = f["shareable_capture"].read_text(encoding="utf-8")
    report = f["shareable_report"].read_text(encoding="utf-8")
    shared_bytes = b"".join(x for *_, x, _a in data_lines(f["shareable_capture"]))
    return capture, report, shared_bytes


@pytest.mark.parametrize("identity", [
    b"INFO;34;GFW 01.00.05;" + SERIAL.encode() + b"\n",            # prj;serial
    b"INFO;34;GFW 01.00.05;2;" + SERIAL.encode() + b"\n",          # prj;HW;serial
    b"INFO;34;GFW 1.0.5;" + SERIAL.encode() + b"\n",               # and a version the probe cannot read
    b"INFO;34;GFW 01.00.05 " + SERIAL.encode() + b";2;1;7\n",      # in the project name
    b"INFO;34;GFW 01.00.05;" + SERIAL.encode() + b";2;1\n",        # first of four fields
    b"INFO;28; Project name:[GFW 01.00.05] Serial:[" + SERIAL.encode() + b"]\n",
])
def test_serial_is_masked_whatever_the_identity_layout(tmp_path, capsys, identity):
    robot = OldFirmwareRobot(identity=identity, ssids=(b"TestNet-Alpha",))
    robot.start()
    try:
        rc, captured, f = _cli_run(robot, tmp_path, capsys)
    finally:
        robot.stop()
    assert rc == 4
    terminal = captured.out + captured.err
    capture, report, shared_bytes = _shared_texts(f)
    for where, text in (("terminal", terminal), ("shareable capture", capture), ("shareable report", report)):
        assert SERIAL not in text, where
        assert SERIAL.encode().hex(" ") not in text, where
        assert "TestNet-Alpha" not in text, where
    assert SERIAL.encode() not in shared_bytes
    assert_same_framing(f["capture"], f["shareable_capture"])
    assert SERIAL in f["report"].read_text(encoding="utf-8")  # the raw files keep it
    raw = json.loads(f["report"].read_text(encoding="utf-8"))
    assert SERIAL not in raw["firmware_gate"]["reason"]


@pytest.mark.parametrize("line, masked", [
    ("INFO;34;GFW 01.00.13 +b \x01\x02Jul  2 2021;2;1;" + SERIAL,
     "INFO;34;GFW 01.00.13 +b \x01\x02Jul  2 2021;2;1;" + "X" * 24),  # rung R1's layout: HW;SW kept
    ("INFO;34;GFW 01.02.00   ;1;2;000000000001", "INFO;34;GFW 01.02.00   ;1;2;XXXXXXXXXXXX"),
    ("INFO;34;GFW 01.00.05;" + SERIAL, "INFO;34;GFW 01.00.05;" + "X" * 24),
    ("INFO;34;GFW 01.00.05;2;" + SERIAL, "INFO;34;GFW 01.00.05;X;" + "X" * 24),
    ("INFO;34;GFW 01.00.05;2;1", "INFO;34;GFW 01.00.05;X;X"),  # no serial field: every field after the name
    ("INFO;34;GFW 01.02.00;HW 1.0;SW 01.02.00;SN SIM0001",
     "INFO;34;GFW 01.02.00;XXXXXX;XXXXXXXXXXX;XXXXXXXXXX"),
    ("INFO;34;GFW 01.00.05 " + SERIAL + ";2;1;7", "INFO;34;GFW 01.00.05 " + "X" * 24 + ";2;1;X"),
    ("INFO;34; GFW 01.00.05 ; 2 ; 1 ; ab12 ", "INFO;34; GFW 01.00.05 ; 2 ; 1 ; XXXX "),
    ("INFO;28; Project name:[GFW 01.00.05] Serial:[" + SERIAL + "]",
     "INFO;28; Project name:[GFW 01.00.05] Serial:[" + "X" * 24 + "]"),
])
def test_identity_lines_are_masked_by_position(line, masked):
    red = _Redactor()
    event = {"type": "text", "kind": "INFO", "code": int(line[5:7]), "raw": line, "params": line.split(";")[2:]}
    red.collect_report({"sessions": [{"events": [event]}]})
    copy = red.report_copy({"e": event, "w": ["quoted: %r" % line], "f": line})
    assert copy["e"]["raw"] == masked and copy["f"] == masked
    assert copy["e"]["params"] == masked.split(";")[2:]
    assert copy["w"] == ["quoted: %r" % masked]
    assert red.data(line.encode("latin-1") + b"\n\x00\x00") == masked.encode("latin-1") + b"\n\x00\x00"


def test_wifi_name_with_a_newline_is_masked_whole(tmp_path, capsys):
    # 802.11 allows any octet in a name; the robot prints it raw, so a '\n' in it splits
    # it into two messages for the decoder. The whole name is masked, in its block.
    ssids = (b"TestNet\nAlpha-Home", b"Cafe_5G", b"x\r\ny", b"ab\ncd")
    robot = OldFirmwareRobot(ssids=ssids)
    robot.start()
    try:
        rc, captured, f = _cli_run(robot, tmp_path, capsys)
    finally:
        robot.stop()
    assert rc == 1  # the pieces after a line break are free-form text on S2: slot-routing errors
    terminal = captured.out + captured.err
    assert "PROBE STOPPED" in terminal
    capture, report, shared_bytes = _shared_texts(f)
    for part in ("TestNet", "Alpha-Home", "Cafe_5G"):
        for where, text in (("terminal", terminal), ("shareable capture", capture),
                            ("shareable report", report)):
            assert part not in text, (part, where)
        assert part.encode() not in shared_bytes
    for name in ssids:
        assert name not in shared_bytes
        assert b"INFO;31;" + name + b"\n" in b"".join(x for *_, x, _a in data_lines(f["capture"]))
    assert_same_framing(f["capture"], f["shareable_capture"])
    shared = json.loads(f["shareable_report"].read_text(encoding="utf-8"))
    texts = [e["raw"] for s in shared["sessions"] for e in s["events"] if e.get("type") == "text"]
    assert not any(t in ("Alpha-Home", "y", "cd") for t in texts), texts


def test_short_names_never_rewrite_the_firmware_facts(tmp_path, capsys):
    # Names made of a digit or two occur inside every version number. They are masked
    # where they stand, in the scan; what the probe states about the firmware stays.
    robot = OldFirmwareRobot(ssids=(b"0", b"1", b"2b", b"01", b"13", b"Cafe_5G"))
    robot.start()
    try:
        rc, captured, f = _cli_run(robot, tmp_path, capsys)
    finally:
        robot.stop()
    assert rc == 4
    out = " ".join(captured.out.split())
    assert "Firmware: 01.00.13" in out
    assert "the robot identified itself as firmware 01.00.13, older than 01.02.00" in out
    assert "checked on 01.02.00 only" in out
    _capture, report, shared_bytes = _shared_texts(f)
    assert b"INFO;34;GFW 01.00.13 +b \x01\x02Jul  2 2021;2;1;" + b"X" * 24 + b"\n" in shared_bytes
    shared = json.loads(report)
    assert shared["firmware_version"] == "01.00.13"
    gate = shared["firmware_gate"]
    assert (gate["version"], gate["minimum"], gate["versions"]) == ("01.00.13", "01.02.00", ["01.00.13"])
    assert "01.00.13" in gate["reason"] and "01.02.00" in gate["rule"]
    scan = [e["raw"] for s in shared["sessions"] for e in s["events"] if str(e.get("raw")).startswith("INFO;31;")]
    assert "INFO;31;0" not in scan and "INFO;31;13" not in scan and "INFO;31;2b" not in scan
    assert "INFO;31;Cafe_5G" not in scan


def test_shareable_capture_keeps_what_the_probe_sent(tmp_path, capsys):
    # Network names that are also the probe's command words: the robot's scan lines are
    # masked, the commands the probe sent and its own notes are not.
    robot = OldFirmwareRobot(identity=b"INFO;34;GFW 01.02.00   ;1;2;000000000001\n",
                             ssids=(b"INFO", b"SYS STATUS", b"Cafe_5G"))
    robot.start()
    try:
        rc, captured, f = _cli_run(robot, tmp_path, capsys)
    finally:
        robot.stop()
    assert rc in (0, 3), captured.out
    raw_rows, shared_rows = data_lines(f["capture"]), data_lines(f["shareable_capture"])
    sent = [r for r in raw_rows if r[1] == ">"]
    assert sent and [r for r in shared_rows if r[1] == ">"] == sent
    assert b"GET GLOBAL SYS STATUS" in [r[4] for r in sent]
    raw_notes = [ln for ln in f["capture"].read_text(encoding="utf-8").splitlines() if "] sending " in ln]
    shared_notes = [ln for ln in f["shareable_capture"].read_text(encoding="utf-8").splitlines()
                    if "] sending " in ln]
    assert raw_notes and shared_notes == raw_notes
    received = b"".join(r[4] for r in shared_rows if r[1] == "<")
    assert b"INFO;31;INFO\n" not in received and b"INFO;31;SYS STATUS\n" not in received
    assert b"INFO;31;Cafe_5G" not in received


def test_log_records_never_print_robot_text(old_robot, tmp_path):
    """A transport log record quotes the event it is about; the probe keeps it off stderr."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(goliath_link.__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    # A fault injected into the firmware gate: it raises for every scan line, so the
    # transport logs "the listener raised while handling TextMessage(... raw='INFO;31;...')".
    code = (
        "import sys\n"
        "from goliath_link import probe\n"
        "observe = probe._FirmwareGate.observe\n"
        "def broken(self, event):\n"
        "    if getattr(event, 'raw', '').startswith('INFO;31;'):\n"
        "        raise RuntimeError('injected')\n"
        "    observe(self, event)\n"
        "probe._FirmwareGate.observe = broken\n"
        "sys.exit(probe.main(sys.argv[1:]))\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", code, "--host", HOST, "--port", str(old_robot.port), "--out", str(tmp_path),
         *FAST_CLI],
        env=env, timeout=120, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="backslashreplace",
    )
    assert res.returncode in (1, 4), res.stdout + res.stderr
    assert_no_secrets(res.stdout, "stdout")
    assert_no_secrets(res.stderr, "stderr")
    assert "capture" in res.stderr  # it says where the record is
    [capture] = [p for p in tmp_path.glob("*.capture.txt") if ".shareable." not in p.name]
    assert "TestNet-Alpha" in capture.read_text(encoding="utf-8")  # the raw capture keeps it
    [shared] = list(tmp_path.glob("*.shareable.capture.txt"))
    assert_no_secrets(shared.read_text(encoding="utf-8"), "shareable capture")


def test_everything_in_a_scan_lines_block_is_masked(tmp_path, capsys):
    # A short piece after a line break ("Foo\nBar"), quoted by a slot-routing error, and
    # bytes after a NUL in the same 512-byte block ("Nul\x00Byte"; firmware printing the
    # name as a C string would stop at the NUL, a fake robot need not).
    robot = OldFirmwareRobot(ssids=(b"Foo\nBar", b"Nul\x00Byte", b"Cafe_5G"))
    robot.start()
    try:
        rc, captured, f = _cli_run(robot, tmp_path, capsys)
    finally:
        robot.stop()
    assert rc == 1, captured.out  # free-form text on S2: slot-routing errors
    terminal = captured.out + captured.err
    capture, report, shared_bytes = _shared_texts(f)
    for piece in ("Bar", "Byte", "Cafe_5G"):
        for where, text in (("terminal", terminal), ("shareable capture", capture),
                            ("shareable report", report)):
            assert piece not in text, (piece, where)
        assert piece.encode() not in shared_bytes
    assert_same_framing(f["capture"], f["shareable_capture"])
    raw = f["report"].read_text(encoding="utf-8")
    assert "'S2 Bar'" in raw  # the raw report keeps the routing error as it was
