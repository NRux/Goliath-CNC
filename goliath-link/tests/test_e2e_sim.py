# SPDX-License-Identifier: Apache-2.0
"""End-to-end tests over real localhost TCP against the spec-based simulator.

These exercise the real default socket opener, the reader thread, framing,
pacing and the probe — everything except the actual robot. Passing them shows a
client follows the *documented* protocol; it says nothing about the firmware.
"""
import json
import socket
import time

import pytest

from goliath_link import (
    GlobalStatusFrame,
    GoliathClient,
    GoliathTransport,
    PositionFrame,
    TextMessage,
)
from goliath_link.probe import READ_ONLY_COMMANDS, UnsafeCommandError, _checked_send, run_probe
from goliath_link.protocol import Commands
from goliath_link.sim import SimulatedGoliath

FAST = dict(listen=0.2, reply_timeout=1.0, pace=0.25, connect_timeout=2.0)


@pytest.fixture
def sim():
    s = SimulatedGoliath(work_line_delay=0.002)
    s.start()
    yield s
    s.stop()


def test_reader_survives_idle_longer_than_connect_timeout(sim):
    # Regression: the connect timeout used to stay on the socket, so the
    # blocking reader died after `timeout` seconds of robot silence.
    with GoliathTransport("127.0.0.1", sim.port, timeout=0.3) as t:
        time.sleep(1.0)
        assert t.connected
        c = GoliathClient(t, command_interval=0.05)
        assert c.learn_firmware_version(timeout=2.0) == (1, 2, 0)


def test_full_job_over_tcp(sim):
    with GoliathTransport("127.0.0.1", sim.port, timeout=2.0) as t:
        c = GoliathClient(t, command_interval=0.05)
        c.learn_firmware_version(timeout=2.0)
        c.set_rf_channels(101, 114)
        c.wait_for_info(4, timeout=2.0)  # pairing done
        c.set_panel_size(1200, 800)
        c.upload_gcode(
            "G00 X0 Y0 ;start\nG01 X10 Y5 Z-1 C315 F1500\nG01 X20 Y5\nM05\n",
            block_interval=0,
        )
        positions = []
        done = c.wait_for(
            lambda e: (isinstance(e, PositionFrame) and positions.append(e) and False)
            or (isinstance(e, TextMessage) and e.code == 23),
            timeout=5.0,
        )
        assert done.code == 23
    assert positions, "expected position frames during the simulated work cycle"
    assert any(round(p.x) == 20 for p in positions)
    assert sim.received[-2:] == ["GCODE CONTINUE;0", "GCODE UPLOAD;56"]


def test_pacing_keeps_commands_separate_in_segment_framing(sim):
    with GoliathTransport("127.0.0.1", sim.port, timeout=2.0) as t:
        c = GoliathClient(t, command_interval=0.1)
        for _ in range(3):
            c.send(Commands.get_global_sys_status())
        for _ in range(3):
            c.wait_for(lambda e: isinstance(e, GlobalStatusFrame), timeout=2.0)
    assert sim.received == ["GET GLOBAL SYS STATUS"] * 3


def test_probe_against_segment_robot(sim, tmp_path):
    report = run_probe("127.0.0.1", sim.port, str(tmp_path), stamp="t1", **FAST)
    assert report["errors"] == []
    assert report["chosen_terminator"] == "none"
    assert report["firmware_version"] == "01.02.00"
    assert report["tower_firmware"] == {"t1": "1.2.0", "t2": "1.2.0"}
    assert [t["mode"] for t in report["terminator_tests"]] == ["none", "lf", "crlf"]
    assert len(report["sweep"]) >= len(READ_ONLY_COMMANDS)
    # Files written and parseable.
    saved = json.loads((tmp_path / "goliath-probe-t1.json").read_text())
    assert saved["chosen_terminator"] == "none"
    capture = (tmp_path / "goliath-probe-t1.capture.txt").read_text()
    assert ">" in capture and "<" in capture


def test_probe_detects_line_framed_robot(tmp_path):
    with SimulatedGoliath(framing="line") as line_sim:
        report = run_probe("127.0.0.1", line_sim.port, str(tmp_path), stamp="t2", **FAST)
    modes = {t["mode"]: t["responded"] for t in report["terminator_tests"]}
    assert modes == {"none": False, "lf": True, "crlf": True}
    assert report["chosen_terminator"] == "lf"


def test_probe_only_ever_sends_read_only_commands(sim, tmp_path):
    run_probe("127.0.0.1", sim.port, str(tmp_path), stamp="t3", **FAST)
    assert sim.received, "probe sent nothing?"
    assert set(sim.received) <= READ_ONLY_COMMANDS, sorted(set(sim.received) - READ_ONLY_COMMANDS)


@pytest.mark.parametrize(
    "unsafe",
    ["GOTO TARGET;0;0;0;0", "ENABLE SPINDLE", "GCODE UPLOAD;10", "CMD SYS RESET",
     "SET Z BACKLASH;0", "ENTER JOG", "GET SSIDS", "INFO;34"],
)
def test_checked_send_refuses_unsafe(unsafe):
    class Boom:
        def send(self, _):  # must never be reached
            raise AssertionError("unsafe command reached the transport")

    with pytest.raises(UnsafeCommandError):
        _checked_send(Boom(), unsafe, "")


def test_probe_reports_unreachable_robot_clearly(tmp_path):
    # Find a port with nothing listening.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    report = run_probe("127.0.0.1", port, str(tmp_path), stamp="t4", **FAST)
    assert report["chosen_terminator"] is None
    assert any("could not connect" in e for e in report["errors"])
    assert (tmp_path / "goliath-probe-t4.json").exists()


def test_close_wakes_blocked_reader_promptly(sim):
    # Regression: close() alone does not wake a thread blocked in recv() on
    # Linux, so close() used to wait out its 2 s join and leak the reader.
    t = GoliathTransport("127.0.0.1", sim.port, timeout=2.0)
    t.connect()
    reader = t._reader
    time.sleep(0.1)
    t0 = time.monotonic()
    t.close()
    assert time.monotonic() - t0 < 1.0
    assert reader is not None and not reader.is_alive()
