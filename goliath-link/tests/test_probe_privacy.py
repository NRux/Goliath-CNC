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
