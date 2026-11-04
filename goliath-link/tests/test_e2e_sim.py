# SPDX-License-Identifier: Apache-2.0
"""End-to-end tests over real localhost TCP against the spec-based simulator.

These exercise the real default socket opener, the reader thread, framing,
pacing and the probe — everything except the actual robot. Passing them shows a
client follows the *documented* protocol; it says nothing about the firmware.
"""
import time

import pytest

from goliath_link import (
    GlobalStatusFrame,
    GoliathClient,
    GoliathTransport,
    PositionFrame,
    TextMessage,
)
from goliath_link.protocol import Commands
from goliath_link.sim import SimulatedGoliath


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


# The probe's end-to-end tests live in test_probe_e2e.py (run against the
# firmware-faithful simulator profiles).


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
