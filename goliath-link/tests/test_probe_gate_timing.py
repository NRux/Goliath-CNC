# SPDX-License-Identifier: Apache-2.0
"""The firmware gate against identity lines that arrive at awkward moments.

Review of the probe hotfix: the gate was asked before the transport's send
hold-back (text pending on S1 holds a send back up to about 0.4 s), so an
identity decoded during the hold did not stop the send; and it saw only lines
the framer had released, so an identity line still waiting for its ``\\n``
was invisible to it for up to the framer's stall timeout (5 s). Each test
here has a robot (scripted_robot.py, 127.0.0.1 only) that sends an identity
line at such a moment and checks, with the robot's own receive times, that
nothing reached it once it had identified itself as 01.00.13.

The timings leave at least 0.15 s on each side of every window they aim at
(Windows timers tick at about 15.6 ms).
"""
import json
import time

import pytest
from old_firmware_robot import IDENTITY_01_00_13
from scripted_robot import HOST, IDENTITY_01_02_00, ScriptedRobot

from goliath_link import probe
from goliath_link.probe import EXIT_STOPPED, main, run_probe

SETTLE = 0.2
GAPS = (0.05, 0.05)


@pytest.fixture
def scripted():
    robots = []

    def start(on_accept=None, on_receive=None):
        robot = ScriptedRobot(on_accept, on_receive)
        robots.append(robot)
        return robot

    yield start
    for robot in robots:
        robot.stop()
        assert not robot.errors, robot.errors


def _probe(robot, tmp_path, stamp, **kw):
    opts = {"listen": 0.3, "reply_timeout": 1.0, "pace": 1.0, "connect_timeout": 2.0,
            "slot_release_wait": 0.2, "connect_gaps": GAPS, "settle": SETTLE}
    opts.update(kw)
    return run_probe(HOST, robot.port, str(tmp_path), stamp=stamp, **opts)


def _drip_s1(robot, start, until, every=0.1):
    """Keep S1's text pending: one byte every ``every`` seconds, so it never goes quiet."""
    t = start
    while t < until:
        t += every
        robot.sleep_until(t)
        robot.send(0, b"d", padded=False)


def test_identity_decoded_during_the_send_hold_back_stops_the_sweep(scripted, tmp_path):
    # 01.02.00 until the sweep's GET GLOBAL SYS STATUS, which gets no reply. Its window
    # ends 1.0 s after the send (pace = reply timeout = 1.0); the next send, PRINT SYS
    # REPORT, is then held back while S1 text is pending (from +0.6 s, a byte every
    # 0.1 s until +1.7 s, so the hold lasts its full 0.4 s), and during that hold, at
    # +1.2 s, the robot says it is 01.00.13: a contradiction. The gate must be asked
    # after the hold, so PRINT SYS REPORT is not sent.
    def on_accept(robot, slot, session):
        if slot == 1:
            time.sleep(0.02)
            robot.send(1, IDENTITY_01_02_00)

    def on_receive(robot, slot, data, session):
        if slot != 0:
            return
        if data.startswith(b"INFO"):
            robot.send(1, IDENTITY_01_02_00)
        elif data == b"GET GLOBAL SYS STATUS":
            t0 = time.monotonic()
            robot.sleep_until(t0 + 0.6)
            robot.send(0, b"abc", padded=False)  # exact-length S1 text, no terminator
            _drip_s1(robot, t0 + 0.6, t0 + 1.2)
            robot.marks["identity"] = robot.send(1, IDENTITY_01_00_13)
            _drip_s1(robot, t0 + 1.2, t0 + 1.7)

    robot = scripted(on_accept, on_receive)
    report = _probe(robot, tmp_path, "hold-sweep", terminator="none")
    assert "identity" in robot.marks, "the scenario did not reach GET GLOBAL SYS STATUS"
    assert robot.received_after(robot.marks["identity"]) == []
    assert robot.commands() == [b"INFO", b"INFO", b"GET GLOBAL SYS STATUS"]
    gate = report["firmware_gate"]
    assert gate["stopped"] is True and gate["verdict"] == "contradictory", gate
    assert "'PRINT SYS REPORT' (sweep)" in gate["not_sent"]
    assert [s["command"] for s in report["sweep"] if s["sent"]] == ["INFO", "GET GLOBAL SYS STATUS"]
    assert report["exit_code"] == EXIT_STOPPED, report["errors"]


def test_identity_decoded_during_the_hold_back_before_the_first_send_means_no_byte(scripted, tmp_path):
    # A robot on 01.00.13 only. Its connect push comes just after --listen ends, while
    # S1 holds exact-length text that keeps the first INFO held back: the probe must
    # send no byte at all, and the report must say so.
    listen = 1.0

    def on_accept(robot, slot, session):
        if slot != 2 or session != 1:
            return
        t0 = time.monotonic()  # the probe listens from about t0 + SETTLE, for `listen` s
        robot.sleep_until(t0 + 0.9)
        robot.send(0, b"Moving stepper!", padded=False)
        _drip_s1(robot, t0 + 0.9, t0 + 1.4)
        robot.marks["identity"] = robot.send(1, IDENTITY_01_00_13)
        _drip_s1(robot, t0 + 1.4, t0 + 2.0)

    robot = scripted(on_accept)
    report = _probe(robot, tmp_path, "hold-first", listen=listen, pace=0.3)
    assert "identity" in robot.marks
    assert robot.received == [], robot.received
    gate = report["firmware_gate"]
    assert gate["stopped"] is True and gate["verdict"] == "older", gate
    assert "before the probe had sent anything" in gate["stopped_at"]
    assert [t["mode"] for t in report["terminator_tests"]] == ["none"]
    assert report["terminator_tests"][0]["not_sent"]
    assert report["exit_code"] == EXIT_STOPPED, report["errors"]


def test_identity_line_still_waiting_for_its_newline_is_seen_before_any_send(scripted, tmp_path):
    # The connect push without '\n' and without padding: the framer holds it until its
    # stall timeout (5 s). The probe waits for it instead of sending INFO meanwhile.
    def on_accept(robot, slot, session):
        if slot == 1 and session == 1:
            time.sleep(0.02)
            robot.marks["identity"] = robot.send(1, IDENTITY_01_00_13.rstrip(b"\n"), padded=False)

    robot = scripted(on_accept)
    report = _probe(robot, tmp_path, "fragment", listen=0.5, pace=0.3)
    assert robot.received == [], robot.received
    gate = report["firmware_gate"]
    assert gate["stopped"] is True and gate["verdict"] == "older", gate
    assert report["firmware_version"] == "01.00.13"
    assert report["exit_code"] == EXIT_STOPPED, report["errors"]


def test_info_line_that_never_ends_stops_the_probe(scripted, tmp_path):
    # An INFO line that keeps growing (a byte every 0.5 s, so the framer's stall timeout
    # never comes): the probe cannot tell what it is, so after waiting for it the
    # firmware is unknown and nothing is sent.
    def on_accept(robot, slot, session):
        if slot == 1 and session == 1:
            time.sleep(0.02)
            t0 = time.monotonic()
            robot.send(1, b"INFO;34;GFW 01.0", padded=False)
            k = 0
            while robot.send(1, b"0", padded=False) is not None and k < 40:
                k += 1
                robot.sleep_until(t0 + 0.5 * k)

    robot = scripted(on_accept)
    report = _probe(robot, tmp_path, "endless", listen=0.3, pace=0.3)
    assert robot.received == [], robot.received
    gate = report["firmware_gate"]
    assert gate["stopped"] is True and gate["verdict"] == "unknown", gate
    assert "S2" in gate["reason"] and "INFO" in gate["reason"], gate["reason"]
    assert report["exit_code"] == EXIT_STOPPED, report["errors"]


def test_stop_line_shows_the_exit_code_the_process_returns(scripted, tmp_path, capsys, monkeypatch):
    # The 01.00.13 identity pushed on S1 (the wrong slot): a slot-routing error, so the
    # run fails (exit code 1) although the gate stopped it too.
    def on_accept(robot, slot, session):
        if slot == 0:
            time.sleep(0.02)
            robot.send(0, IDENTITY_01_00_13)

    robot = scripted(on_accept)
    monkeypatch.setattr(probe, "DEFAULT_CONNECT_GAPS", GAPS)
    rc = main(["--host", HOST, "--port", str(robot.port), "--out", str(tmp_path),
               "--listen", "0.5", "--reply-timeout", "0.5", "--slot-release-wait", "0.1"])
    out = " ".join(capsys.readouterr().out.split())
    assert rc == 1
    assert "FAILED (exit code 1)" in out and "PROBE STOPPED" in out
    assert "exit code 4" not in out
    assert "PROBE STOPPED (exit code 1, not a success; errors were found too)" in out
    assert robot.received == []


def test_identity_from_a_session_whose_connect_failed_is_reported(scripted, tmp_path, capsys):
    # The robot pushes its 01.00.13 identity on S2 and then resets S3 during the
    # handshake: no session completed its connect, but the firmware is known.
    def on_accept(robot, slot, session):
        if slot == 1:
            time.sleep(0.01)
            robot.send(1, IDENTITY_01_00_13)
        elif slot == 2:
            time.sleep(0.05)
            robot.reset(2)

    robot = scripted(on_accept)
    report, redactor = probe._run_probe(
        HOST, robot.port, str(tmp_path), terminator="auto", listen=0.3, reply_timeout=0.5, pace=0.3,
        connect_timeout=2.0, slot_release_wait=0.1, extended=False, connect_gaps=GAPS, settle=0.3,
        opener=probe._default_opener, stamp="drop")
    assert robot.commands() == []
    assert report["firmware_gate"]["verdict"] == "older"
    assert report["firmware_version"] == "01.00.13"
    assert report["exit_code"] == 1 and report["errors"]
    probe._print_summary(report, redactor)
    out = capsys.readouterr().out
    assert "Firmware:          01.00.13" in out, out
    with open(report["report_file"], encoding="utf-8") as fh:
        assert json.load(fh)["firmware_version"] == "01.00.13"
