# SPDX-License-Identifier: Apache-2.0
"""GoliathClient end to end: real localhost TCP, three connections, simulator.

The simulator profiles (see :mod:`goliath_link.sim`):

* ``gfw-01.02.00`` -- what static disassembly of GFW 01.02.00 established:
  output routed by connection slot, NUL-padded 512-byte blocks, the 48-byte
  status blob, ``INFO;0`` (no ``INFO;8``) at the end of an upload, no
  ``ABORT WORK`` and no ``GCODE CONTINUE``.
* ``vendor-app-newer`` -- what the vendor app expects of newer firmware
  (identity 01.02.03, ``ABORT WORK``, ``GCODE CONTINUE``, ``INFO;7`` on the
  upload header, ``INFO;8`` at the end). No such firmware image survives, so
  this profile is a model of the app's expectations, not of a robot.

Passing these shows the client copes with those models over the real socket
stack. It proves nothing about the robot itself. Only 127.0.0.1 is used.
"""
import threading
import time

import pytest

from goliath_link import client as cl
from goliath_link.gcode import clean_gcode
from goliath_link.protocol import Commands, GlobalStatusBlob, PositionFrame, TextMessage
from goliath_link.sim import SimulatedGoliath
from goliath_link.transport import GoliathTransport

LINK = dict(connect_gaps=(0.05, 0.05), settle=0.1, timeout=2.0)
JOB = "G00 X0 Y0 ;start\nG01 X10 Y5 Z-1 C315 F1500\nG01 X20 Y5\nM05\n"
UP = dict(block_interval=0, continue_wait=0.05, header_wait=0.1, done_timeout=5.0,
          info8_grace=0.5)


@pytest.fixture
def start_sim():
    sims = []

    def start(profile, **kw):
        kw.setdefault("work_line_delay", 0.002)
        s = SimulatedGoliath(profile=profile, **kw)
        s.start()
        sims.append(s)
        return s

    yield start
    for s in sims:
        s.stop()
        assert s.errors == [], s.errors


@pytest.fixture
def link():
    opened = []

    def open_link(sim, **client_kw):
        t = GoliathTransport("127.0.0.1", sim.port, **LINK)
        t.connect()
        kw = dict(command_interval=0.05)
        kw.update(client_kw)
        c = cl.GoliathClient(t, **kw)
        opened.append((t, c))
        return t, c

    yield open_link
    for t, c in opened:
        t.close()
        c.close()


def is_info(code):
    return lambda e: isinstance(e, TextMessage) and e.kind == "INFO" and e.code == code


# --------------------------------------------------------------------------- #
# gfw-01.02.00
# --------------------------------------------------------------------------- #


def test_gfw_identity_reply_arrives_on_the_second_connection(start_sim, link):
    sim = start_sim("gfw-01.02.00")
    t, c = link(sim)
    t0 = time.monotonic()
    assert c.learn_firmware_version(timeout=2.0) == (1, 2, 0)
    replies = [r for r in c.history(since=t0) if is_info(34)(r.event)]
    assert replies and all(r.conn == 1 for r in replies)
    assert sim.received == ["INFO"]


def test_gfw_full_job_is_unconfirmed_by_default_and_says_why(start_sim, link):
    sim = start_sim("gfw-01.02.00")
    t, c = link(sim)
    c.learn_firmware_version(timeout=2.0)
    t0 = c.set_rf_channels(101, 114)
    c.wait_for_info(4, timeout=2.0, since=t0)  # pairing done
    c.set_panel_size(1200, 800)
    started = time.monotonic()
    with pytest.raises(cl.UploadUnconfirmedError) as ei:
        c.upload_gcode(JOB, **UP)
    report = ei.value.report
    assert report.end_of_upload_ack is not None  # the firmware's INFO;0
    assert "01.02.03" in str(ei.value)
    assert c.upload_hazard is None
    c.wait_for(is_info(23), timeout=5.0, since=started)
    assert sim.program == clean_gcode(JOB)
    assert "GCODE CONTINUE;0" not in sim.received  # absent from the 01.02.00 table
    assert sim.received[-1] == "GCODE UPLOAD;%d" % len(clean_gcode(JOB))
    positions = [r for r in c.history(since=started) if isinstance(r.event, PositionFrame)]
    assert positions and all(r.conn == 2 for r in positions)
    assert any(round(r.event.x) == 20 for r in positions)


def test_gfw_job_with_the_end_of_upload_ack_accepted(start_sim, link):
    sim = start_sim("gfw-01.02.00")
    t, c = link(sim)
    c.learn_firmware_version(timeout=2.0)
    report = c.upload_gcode(JOB, confirm="info8-or-info0", **UP)
    assert report.confirmed and report.confirmed_by.startswith("INFO;0")
    assert report.end_of_upload_ack.conn == 1
    c.wait_for(is_info(23), timeout=5.0, since=report.started_at)


def test_gfw_info8_confirms_when_the_firmware_sends_it(start_sim, link):
    sim = start_sim("gfw-01.02.00", upload_acks="info0+info8")
    t, c = link(sim)
    c.learn_firmware_version(timeout=2.0)
    assert c.upload_gcode(JOB, **UP).confirmed_by == "INFO;8"


def test_gfw_second_upload_is_refused_until_restart(start_sim, link):
    sim = start_sim("gfw-01.02.00", upload_acks="info0+info8")
    t, c = link(sim)
    c.learn_firmware_version(timeout=2.0)
    c.upload_gcode(JOB, **UP)
    with pytest.raises(cl.RestartRequiredError):
        c.upload_gcode(JOB, **UP)
    assert sum(1 for r in sim.received if r.startswith("GCODE UPLOAD")) == 1


@pytest.mark.parametrize("learn", [True, False])
def test_gfw_stop_is_refused_and_abort_work_never_sent(start_sim, link, learn):
    sim = start_sim("gfw-01.02.00")
    t, c = link(sim)
    if learn:
        c.learn_firmware_version(timeout=2.0)
    with pytest.raises(cl.StopNotSupportedError, match="physical"):
        c.stop_work(timeout=1.0)
    assert "ABORT WORK" not in sim.received


def test_gfw_keepalive_thread_never_lands_inside_the_program(start_sim, link):
    sim = start_sim("gfw-01.02.00")
    t, c = link(sim)
    c.learn_firmware_version(timeout=2.0)
    program = "".join(f"G01 X{i}.000 Y{i}.000 F1500\n" for i in range(200))
    stop = threading.Event()
    errors = []

    def keepalive():
        while not stop.is_set():
            try:
                c.get_global_status()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return
            time.sleep(0.02)

    th = threading.Thread(target=keepalive)
    th.start()
    try:
        c.upload_gcode(program, confirm="info8-or-info0",
                       **dict(UP, block_size=256, block_interval=0.02))
    finally:
        stop.set()
        th.join(5.0)
    assert errors == []
    assert sim.program == clean_gcode(program)
    assert b"GET GLOBAL" not in sim.program


def test_gfw_pacing_keeps_commands_separate(start_sim, link):
    sim = start_sim("gfw-01.02.00")
    t, c = link(sim, command_interval=0.1)
    with c.subscribe() as sub:
        for _ in range(3):
            c.send(Commands.get_global_sys_status())
        blobs = [sub.wait_for(lambda e: isinstance(e, GlobalStatusBlob), timeout=2.0)
                 for _ in range(3)]
    assert len(blobs) == 3
    assert sim.received == ["GET GLOBAL SYS STATUS"] * 3


def test_gfw_link_loss_reaches_every_waiter(start_sim, link):
    sim = start_sim("gfw-01.02.00")
    t, c = link(sim)
    results = {}

    def waiter(name, code):
        try:
            c.wait_for_info(code, timeout=5.0)
            results[name] = "ok"
        except ConnectionError:
            results[name] = "ConnectionError"
        except Exception as exc:  # noqa: BLE001
            results[name] = type(exc).__name__

    threads = [threading.Thread(target=waiter, args=(n, code))
               for n, code in (("a", 23), ("b", 41))]
    for th in threads:
        th.start()
    time.sleep(0.2)
    sim.drop(1)
    for th in threads:
        th.join(6.0)
    assert results == {"a": "ConnectionError", "b": "ConnectionError"}


def test_reader_survives_idle_longer_than_connect_timeout(start_sim):
    sim = start_sim("gfw-01.02.00")
    with GoliathTransport("127.0.0.1", sim.port, **dict(LINK, timeout=0.3)) as t:
        time.sleep(1.0)
        assert t.connected
        with cl.GoliathClient(t, command_interval=0.05) as c:
            assert c.learn_firmware_version(timeout=2.0) == (1, 2, 0)


# --------------------------------------------------------------------------- #
# vendor-app-newer
# --------------------------------------------------------------------------- #


def test_newer_full_job_is_confirmed_by_info8(start_sim, link):
    sim = start_sim("vendor-app-newer")
    t, c = link(sim)
    assert c.learn_firmware_version(timeout=2.0) == (1, 2, 3)
    report = c.upload_gcode(JOB, **UP)
    assert report.confirmed_by == "INFO;8"
    assert report.continue_sent
    assert report.header_ack is not None and report.header_ack.code == 7
    assert sim.received[-2:] == ["GCODE CONTINUE;0", "GCODE UPLOAD;%d" % len(clean_gcode(JOB))]
    c.wait_for(is_info(23), timeout=5.0, since=report.started_at)


def test_newer_stop_during_work_is_confirmed(start_sim, link):
    sim = start_sim("vendor-app-newer", work_line_delay=0.05)
    t, c = link(sim)
    c.learn_firmware_version(timeout=2.0)
    program = "".join(f"G01 X{i}.000 Y1.000 F1500\n" for i in range(200))
    report = c.upload_gcode(program, **UP)
    c.wait_for(lambda e: isinstance(e, PositionFrame), timeout=3.0, since=report.started_at)
    stop = c.stop_work(timeout=3.0)
    assert stop.confirmed_by.code in (18, 23)
    assert sim.wait_for_event("work-end", timeout=3.0).note == "aborted"
    assert sim.received[-1] == "ABORT WORK"


def test_newer_stop_when_idle_is_rejected_not_claimed(start_sim, link):
    sim = start_sim("vendor-app-newer")
    t, c = link(sim)
    c.learn_firmware_version(timeout=2.0)
    with pytest.raises(cl.StopRejectedError):
        c.stop_work(timeout=2.0)
