# SPDX-License-Identifier: Apache-2.0
"""GoliathClient tests over a scripted three-connection fake robot (no network).

``FakeRobot`` hands the transport three scripted sockets and routes its output
the way firmware GFW 01.02.00 does (firmware facts brief, section 1): every
``INFO``/``ERR``/``WARN`` line goes to connection 1, position frames to
connection 2, free-form text to connection 0, each in a NUL-padded 512-byte
block. Commands arrive on connection 0; a handler scripts the replies.

Each review finding fixed here has tests named after it (``test_safe1_*``,
``test_high1_*`` ...); they fail on the client as it was before the fix.
"""
import logging
import queue
import struct
import threading
import time

import pytest

from goliath_link import client as cl
from goliath_link.gcode import clean_gcode
from goliath_link.protocol import PositionFrame, TextMessage
from goliath_link.transport import GoliathTransport

OLD = (1, 2, 0)  # GFW 01.02.00, the only firmware image that survives
NEW = (1, 2, 3)  # newer firmware the vendor app gates on (no image survives)

PROGRAM = "".join(f"G01 X{i}.000 Y{i}.000 C90 F1500 ;line {i}\n" for i in range(40))
# Stage windows stay well above the ~16 ms tick of time.monotonic() on Windows:
# records are attributed to upload stages by when they arrived.
FAST = dict(block_interval=0, continue_wait=0.05, header_wait=0.05, done_timeout=2.0,
            info8_grace=0.3)


def pad(msg: bytes) -> bytes:
    return msg + bytes(512 - len(msg))


class FakeSock:
    def __init__(self, robot: "FakeRobot", index: int) -> None:
        self.robot = robot
        self.index = index
        self.q: "queue.Queue[object]" = queue.Queue()
        self.closed = False

    def recv(self, n: int) -> bytes:
        item = self.q.get()
        if isinstance(item, BaseException):
            raise item
        return item  # type: ignore[return-value]

    def sendall(self, data: bytes) -> None:
        self.robot.on_send(self.index, bytes(data))

    def shutdown(self, how: int) -> None:
        self.q.put(b"")

    def close(self) -> None:
        self.closed = True
        self.q.put(b"")


class FakeRobot:
    def __init__(self, handler=None) -> None:
        self.socks = [FakeSock(self, i) for i in range(3)]
        self._next = iter(self.socks)
        self.handler = handler
        self.lock = threading.Lock()
        self.wire: list = []  # (monotonic time, connection, bytes) written by the client

    def opener(self, host, port, timeout):
        return next(self._next)

    def on_send(self, index: int, data: bytes) -> None:
        with self.lock:
            self.wire.append((time.monotonic(), index, data))
        if self.handler is not None:
            self.handler(self, data)

    @property
    def commands(self) -> list:
        with self.lock:
            return [d for (_, i, d) in self.wire if i == 0]

    def line(self, text: str, conn=None) -> None:
        """Send one text message the way the firmware routes it."""
        if conn is None:
            conn = 1 if text.split(";")[0] in ("INFO", "ERR", "WARN") else 0
        self.socks[conn].q.put(pad(text.encode("ascii") + b"\n"))

    def position(self, line: int, x: float = 0.0, y: float = 0.0) -> None:
        frame = b"{" + struct.pack("<ffffi", x, y, 0.0, 90.0, line) + b"}"
        self.socks[2].q.put(pad(frame))

    def eof(self, conn: int = 1) -> None:
        self.socks[conn].q.put(b"")


class Firmware:
    """A scripted firmware: identity, upload mode, and canned replies.

    ``header``/``done``/``cont``/``abort`` are the lines sent when the upload
    header arrives, when the announced size has been received, on
    ``GCODE CONTINUE`` and on ``ABORT WORK``.
    """

    def __init__(self, version="01.02.00", header=(), done=("INFO;0",), cont=(),
                 abort=(), other=None) -> None:
        self.version = version
        self.header = tuple(header)
        self.done = tuple(done)
        self.cont = tuple(cont)
        self.abort = tuple(abort)
        self.other = other
        self.expect = None
        self.program = bytearray()
        self.programs: list = []
        self.uploading = threading.Event()

    def __call__(self, robot: FakeRobot, data: bytes) -> None:
        if self.expect is not None:
            self.program += data
            if len(self.program) >= self.expect:
                self.programs.append(bytes(self.program))
                self.expect = None
                self.uploading.clear()
                for r in self.done:
                    r(robot) if callable(r) else robot.line(r)
            return
        cmd = data.decode("ascii")
        name = cmd.split(";")[0]
        if cmd == "INFO":
            robot.line(f"INFO;34;GFW {self.version}   ;1;2;000000000001")
        elif name == "GCODE UPLOAD":
            self.expect = int(cmd.split(";")[1])
            self.program = bytearray()
            self.uploading.set()
            for r in self.header:
                robot.line(r)
        elif name == "GCODE CONTINUE":
            for r in self.cont:
                robot.line(r)
        elif cmd == "ABORT WORK":
            for r in self.abort:
                robot.line(r)
        elif self.other is not None:
            self.other(robot, cmd)


@pytest.fixture
def make():
    made = []

    def factory(handler=None, *, firmware=None, **client_kw):
        robot = FakeRobot(handler)
        t = GoliathTransport(opener=robot.opener, connect_gaps=(0, 0), settle=0,
                             join_timeout=1.0)
        t.connect()
        kw = dict(command_interval=0.01)
        kw.update(client_kw)
        c = cl.GoliathClient(t, **kw)
        c.firmware_version = firmware
        made.append((c, t))
        return robot, t, c

    yield factory
    for c, t in made:
        t.close()
        close = getattr(c, "close", None)
        if close is not None:
            close()


def payload_bytes(robot: FakeRobot) -> bytes:
    """Everything the client wrote after the GCODE UPLOAD header."""
    cmds = robot.commands
    i = next(i for i, d in enumerate(cmds) if d.startswith(b"GCODE UPLOAD;"))
    return b"".join(cmds[i + 1:])


# --------------------------------------------------------------------------- #
# Event bus (LOW-1): every waiter has its own subscription
# --------------------------------------------------------------------------- #


def _waiter(c, code, results, name, timeout=5.0):
    t0 = time.monotonic()
    try:
        c.wait_for_info(code, timeout=timeout)
        results[name] = ("ok", time.monotonic() - t0)
    except Exception as exc:  # noqa: BLE001 - recorded for the assertion
        results[name] = (type(exc).__name__, time.monotonic() - t0)


def test_low1_every_waiter_gets_transport_closed():
    robot = FakeRobot()
    t = GoliathTransport(opener=robot.opener, connect_gaps=(0, 0), settle=0)
    t.connect()
    c = cl.GoliathClient(t, command_interval=0.01)
    results: dict = {}
    threads = [threading.Thread(target=_waiter, args=(c, code, results, name))
               for name, code in (("gui", 23), ("safety", 41), ("third", 8))]
    for th in threads:
        th.start()
    time.sleep(0.2)
    robot.eof(1)  # the robot drops the link
    for th in threads:
        th.join(6.0)
    assert {k: v[0] for k, v in results.items()} == {
        "gui": "ConnectionError", "safety": "ConnectionError", "third": "ConnectionError"
    }
    assert all(v[1] < 2.0 for v in results.values()), results
    t.close()


def test_low1_wait_after_link_loss_fails_at_once_instead_of_timing_out(make):
    robot, t, c = make()
    robot.eof(1)
    with pytest.raises(ConnectionError):
        c.wait_for_info(8, timeout=3.0)
    t0 = time.monotonic()
    with pytest.raises(ConnectionError):
        c.wait_for_info(8, timeout=3.0)  # the closed state is sticky
    assert time.monotonic() - t0 < 1.0


def test_low1_two_waiters_both_receive_the_same_event(make):
    robot, t, c = make()
    results: dict = {}
    threads = [threading.Thread(target=_waiter, args=(c, 23, results, n, 3.0)) for n in "ab"]
    for th in threads:
        th.start()
    time.sleep(0.2)
    robot.line("INFO;23")
    for th in threads:
        th.join(4.0)
    assert results["a"][0] == "ok" and results["b"][0] == "ok", results


def test_low1_events_a_waiter_skips_are_still_seen_and_logged(make, caplog):
    robot, t, c = make()
    caplog.set_level(logging.WARNING, logger="goliath_link.client")
    with c.subscribe() as watcher:
        robot.line("WARN;2")
        robot.line("INFO;41")
        robot.line("INFO;8")
        c_msg = c.wait_for_info(8, timeout=2.0, since=0.0)
        assert c_msg.code == 8
        seen = [watcher.get(timeout=2.0).event for _ in range(3)]
    assert [(e.kind, e.code) for e in seen] == [("WARN", 2), ("INFO", 41), ("INFO", 8)]
    text = caplog.text
    assert "WARN;2" in text and "INFO;41" in text


def test_stale_event_does_not_satisfy_a_new_wait(make):
    robot, t, c = make()
    robot.line("INFO;8")
    time.sleep(0.2)  # it arrived before anyone waited
    with pytest.raises(cl.GoliathTimeout):
        c.wait_for_info(8, timeout=0.3)


def test_wait_since_a_send_sees_the_immediate_reply(make):
    def handler(robot, data):
        if data.startswith(b"SET RF CHANNELS"):
            robot.line("INFO;0")
            robot.line("INFO;4")

    robot, t, c = make(handler)
    t0 = c.set_rf_channels(101, 114)
    assert c.wait_for_info(4, timeout=2.0, since=t0).code == 4


def test_request_subscribes_before_sending(make):
    def handler(robot, data):
        if data == b"PRINT SYS REPORT":
            robot.line("INFO;40;1;1;1;1;1;1;1;1;1;1;1;1;1;1;1;END")

    robot, t, c = make(handler)
    ev = c.request("PRINT SYS REPORT", lambda e: isinstance(e, TextMessage) and e.code == 40,
                   timeout=2.0)
    assert ev.params[-1] == "END"


def test_wait_raises_goliath_error_with_code(make):
    robot, t, c = make()
    threading.Timer(0.1, robot.line, args=("ERR;9",)).start()
    with pytest.raises(cl.GoliathError) as ei:
        c.wait_for_info(8, timeout=2.0)
    assert ei.value.code == 9
    assert isinstance(ei.value, cl.GoliathSafetyError)  # 9 = POS_OVER_THRESH


def test_safety_error_raises_even_when_raise_on_error_is_false(make):
    robot, t, c = make()
    threading.Timer(0.1, robot.line, args=("ERR;2",)).start()
    with pytest.raises(cl.GoliathSafetyError):
        c.wait_for_info(8, timeout=2.0, raise_on_error=False)


def test_non_safety_error_is_skipped_when_raise_on_error_is_false(make):
    robot, t, c = make()

    def later():
        robot.line("ERR;10")
        robot.line("INFO;8")

    threading.Timer(0.1, later).start()
    assert c.wait_for_info(8, timeout=2.0, raise_on_error=False).code == 8


def test_safety_error_is_logged_with_nobody_waiting(make, caplog):
    robot, t, c = make()
    caplog.set_level(logging.ERROR, logger="goliath_link.client")
    robot.line("ERR;2")
    deadline = time.monotonic() + 2.0
    while "ERR;2" not in caplog.text and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "ERR;2" in caplog.text and "SAFETY" in caplog.text.upper()


def test_unexpected_connection_is_reported(make, caplog):
    robot, t, c = make()
    caplog.set_level(logging.WARNING, logger="goliath_link.client")
    robot.line("INFO;4", conn=0)  # GFW 01.02.00 sends INFO only to connection 1
    deadline = time.monotonic() + 2.0
    while "connection 0" not in caplog.text and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "connection 0" in caplog.text


def test_position_frames_are_delivered(make):
    robot, t, c = make()
    with c.subscribe() as sub:
        robot.position(12, x=5.0, y=6.0)
        ev = sub.wait_for(lambda e: isinstance(e, PositionFrame), timeout=2.0)
    assert ev.gcode_line == 12 and round(ev.angle) == 90


def test_drain_returns_events_since_the_previous_drain(make):
    robot, t, c = make()
    robot.line("INFO;1")
    robot.line("INFO;2")
    time.sleep(0.2)
    first = c.drain()
    assert [(e.kind, e.code) for e in first if isinstance(e, TextMessage)] == [
        ("INFO", 1), ("INFO", 2)
    ]
    robot.line("INFO;3")
    time.sleep(0.2)
    assert [e.code for e in c.drain() if isinstance(e, TextMessage)] == [3]


def test_close_stops_the_event_pump(make):
    robot, t, c = make()
    assert c.event_bus.running
    c.close()
    assert not c.event_bus.running
    with pytest.raises(RuntimeError):
        c.subscribe()


# --------------------------------------------------------------------------- #
# Send serialisation and pacing (HIGH-2)
# --------------------------------------------------------------------------- #


def test_high2_concurrent_send_is_not_spliced_into_the_upload_payload(make):
    fw = Firmware(version="01.02.03", done=("INFO;0", "INFO;8"))
    robot, t, c = make(fw, firmware=NEW)
    errors: list = []

    def keepalive():
        fw.uploading.wait(2.0)
        time.sleep(0.02)
        try:
            c.send("GET GLOBAL SYS STATUS")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    th = threading.Thread(target=keepalive)
    th.start()
    c.upload_gcode(PROGRAM, block_size=64, block_interval=0.01, done_timeout=3.0)
    th.join(5.0)
    assert errors == []
    assert fw.programs == [clean_gcode(PROGRAM)]
    assert robot.commands[-1] == b"GET GLOBAL SYS STATUS"


def test_high2_raw_transport_send_is_also_held_off_during_upload(make):
    fw = Firmware(version="01.02.03", done=("INFO;0", "INFO;8"))
    robot, t, c = make(fw, firmware=NEW)

    def keepalive():
        fw.uploading.wait(2.0)
        time.sleep(0.02)
        t.send("GET GLOBAL SYS STATUS")  # bypasses the client entirely

    th = threading.Thread(target=keepalive)
    th.start()
    c.upload_gcode(PROGRAM, block_size=64, block_interval=0.01, done_timeout=3.0)
    th.join(5.0)
    assert fw.programs == [clean_gcode(PROGRAM)]


def test_high2_concurrent_sends_keep_the_command_interval(make):
    robot, t, c = make(command_interval=0.2)
    barrier = threading.Barrier(3)

    def sender(cmd):
        barrier.wait()
        c.send(cmd)

    threads = [threading.Thread(target=sender, args=(cmd,))
               for cmd in ("INFO", "GET GLOBAL SYS STATUS", "PRINT SYS REPORT")]
    for th in threads:
        th.start()
    for th in threads:
        th.join(5.0)
    times = sorted(ts for (ts, i, d) in robot.wire if i == 0)
    assert len(times) == 3
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(g >= 0.19 for g in gaps), gaps


@pytest.mark.parametrize("bad", [0, -0.1, float("nan")])
def test_high2_command_interval_must_be_positive(make, bad):
    robot, t, c = make()
    with pytest.raises(ValueError):
        cl.GoliathClient(t, command_interval=bad)
    with pytest.raises(ValueError):
        c.command_interval = bad


# --------------------------------------------------------------------------- #
# Firmware capabilities
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "version, abort, cont, info8, restart, zplate, newer",
    [
        (None, False, False, None, True, False, False),
        ((1, 0, 14), False, False, None, True, False, False),
        ((1, 2, 0), False, False, None, True, False, False),
        ((1, 2, 1), True, True, None, False, False, True),
        ((1, 2, 2), True, True, None, False, True, True),
        ((1, 2, 3), True, True, True, False, True, True),
        ((1, 3, 0), True, True, True, False, True, True),
    ],
)
def test_capabilities_follow_the_vendor_gates(version, abort, cont, info8, restart, zplate, newer):
    caps = cl.FirmwareCapabilities.for_version(version)
    assert caps.version == version
    assert caps.has_abort_work is abort
    assert caps.has_gcode_continue is cont
    assert caps.confirms_upload_with_info8 is info8
    assert caps.requires_restart_between_jobs is restart
    assert caps.has_z_plate_offset is zplate
    assert caps.has_newer_commands is newer


def test_capability_decisions_are_explained():
    caps = cl.FirmwareCapabilities.for_version(None)
    lines = caps.explain()
    text = "\n".join(lines)
    for feature in ("ABORT WORK", "GCODE CONTINUE", "INFO;8", "restart"):
        assert feature in text
    assert "unknown" in text.lower()
    d = caps.supports_command("ABORT WORK")
    assert d.allowed is False and "physical" in d.reason


@pytest.mark.parametrize("cmd", ["ABORT WORK", "SEND TCP POS", "GET Z BACKLASH",
                                 "SET Z PLATE OFFSET;1.0", "GCODE CONTINUE;0", "CMD SYS RESET"])
@pytest.mark.parametrize("version", [None, OLD])
def test_send_refuses_commands_absent_from_the_firmware(make, cmd, version):
    robot, t, c = make(firmware=version)
    with pytest.raises(cl.CommandNotSupportedError):
        c.send(cmd)
    assert robot.commands == []


def test_send_allows_newer_commands_on_newer_firmware(make, caplog):
    robot, t, c = make(firmware=NEW)
    caplog.set_level(logging.INFO, logger="goliath_link.client")
    c.send("GET Z BACKLASH")
    assert robot.commands == [b"GET Z BACKLASH"]
    assert "GET Z BACKLASH" in caplog.text  # the gate decision is logged


# --------------------------------------------------------------------------- #
# Stop (SAFE-1)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("version", [OLD, None, (1, 0, 14)])
def test_safe1_stop_is_refused_where_abort_work_does_not_exist(make, version):
    robot, t, c = make(Firmware(abort=("INFO;0", "INFO;18")), firmware=version)
    with pytest.raises(cl.StopNotSupportedError) as ei:
        c.stop_work(timeout=0.5)
    assert "physical" in str(ei.value)
    with pytest.raises(cl.StopNotSupportedError):
        c.abort_work()  # the old name is gated the same way
    assert b"ABORT WORK" not in robot.commands


def test_safe1_stop_waits_for_the_work_cycle_to_end(make):
    robot, t, c = make(Firmware(version="01.02.03", abort=("INFO;0", "INFO;18", "INFO;16")),
                       firmware=NEW)
    report = c.stop_work(timeout=2.0)
    assert report.confirmed_by.code == 18
    assert report.acknowledged
    assert robot.commands == [b"ABORT WORK"]


def test_safe1_stop_confirmed_by_work_complete(make):
    robot, t, c = make(Firmware(version="01.02.03", abort=("INFO;23",)), firmware=NEW)
    assert c.stop_work(timeout=2.0).confirmed_by.code == 23


def test_safe1_unconfirmed_stop_raises(make):
    robot, t, c = make(Firmware(version="01.02.03", abort=("INFO;0",)), firmware=NEW)
    t0 = time.monotonic()
    with pytest.raises(cl.StopNotConfirmedError) as ei:
        c.stop_work(timeout=0.4)
    assert time.monotonic() - t0 < 2.0
    assert "physical" in str(ei.value) and "running" in str(ei.value)


def test_safe1_stop_rejected_by_the_robot(make):
    robot, t, c = make(Firmware(version="01.02.03", abort=("ERR;1",)), firmware=NEW)
    with pytest.raises(cl.StopRejectedError) as ei:
        c.stop_work(timeout=2.0)
    assert ei.value.code == 1


def test_safe1_an_old_exit_message_does_not_confirm_a_stop(make):
    robot, t, c = make(Firmware(version="01.02.03", abort=()), firmware=NEW)
    robot.line("INFO;18")  # from earlier, before the stop was asked for
    time.sleep(0.2)
    with pytest.raises(cl.StopNotConfirmedError):
        c.stop_work(timeout=0.4)


def test_safe1_stop_during_an_upload_fails_fast_instead_of_blocking(make):
    fw = Firmware(version="01.02.03", done=(), abort=("INFO;18",))
    robot, t, c = make(fw, firmware=NEW)
    errors: list = []

    def upload():
        try:
            c.upload_gcode(PROGRAM, **dict(FAST, done_timeout=2.0))
        except Exception as exc:  # noqa: BLE001 - expected: unconfirmed
            errors.append(exc)

    th = threading.Thread(target=upload)
    th.start()
    fw.uploading.wait(2.0)
    t0 = time.monotonic()
    with pytest.raises(cl.StopBlockedError, match="physical"):
        c.stop_work(timeout=0.3)
    assert time.monotonic() - t0 < 1.0
    th.join(5.0)
    assert b"ABORT WORK" not in robot.commands
    assert fw.programs == [clean_gcode(PROGRAM)]


def test_safe1_stop_refused_while_the_robot_may_be_in_upload_mode(make):
    fw = Firmware(version="01.02.03", done=(), abort=("INFO;18",))
    robot, t, c = make(fw, firmware=NEW)
    with pytest.raises(cl.UploadUnconfirmedError):
        c.upload_gcode(PROGRAM, **dict(FAST, done_timeout=0.3))
    with pytest.raises(cl.RobotStateUncertainError, match="physical"):
        c.stop_work(timeout=0.5)
    assert b"ABORT WORK" not in robot.commands


def test_safe1_link_loss_during_stop_is_not_a_stop(make):
    robot, t, c = make(Firmware(version="01.02.03"), firmware=NEW)
    threading.Timer(0.1, robot.eof, args=(1,)).start()
    with pytest.raises(ConnectionError, match="physical"):
        c.stop_work(timeout=3.0)


# --------------------------------------------------------------------------- #
# Upload: refusal of empty programs (LOW-3), cleaning (MED-5), bytes input
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("program", ["", ";only comments\n;U|tool 1|\n", "\n\n   \n",
                                     b"\r\n\r\n"])
def test_low3_empty_program_is_refused_and_nothing_is_sent(make, program):
    robot, t, c = make(Firmware(), firmware=OLD)
    with pytest.raises(ValueError) as ei:
        c.upload_gcode(program, block_interval=0, done_timeout=0.5)
    assert isinstance(ei.value, cl.EmptyProgramError)
    assert robot.commands == []


def test_med5_announced_size_includes_the_final_empty_line(make):
    fw = Firmware(done=("INFO;0", "INFO;8"))
    robot, t, c = make(fw, firmware=OLD)
    c.upload_gcode("G01 X1\nG01 X2\n", **FAST)
    assert b"GCODE UPLOAD;18" in robot.commands
    assert fw.programs == [b"G01 X1\r\nG01 X2\r\n\r\n"]


def test_uncleaned_bytes_are_refused(make):
    robot, t, c = make(Firmware(), firmware=OLD)
    with pytest.raises(ValueError):
        c.upload_gcode(b";U|tool|\nG01 X1\n", **FAST)
    assert robot.commands == []


@pytest.mark.parametrize("kw", [dict(block_size=0), dict(done_timeout=0), dict(block_interval=-1),
                                dict(confirm="maybe"), dict(header_ack="sometimes"),
                                dict(info8_grace=-1)])
def test_upload_options_are_validated(make, kw):
    robot, t, c = make(Firmware(), firmware=OLD)
    with pytest.raises(ValueError):
        c.upload_gcode("G01 X1\n", **dict(FAST, **kw))
    assert robot.commands == []


# --------------------------------------------------------------------------- #
# Upload: GCODE CONTINUE gate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("version", [OLD, None, (1, 0, 14)])
def test_continue_is_not_sent_where_the_firmware_lacks_it(make, version):
    fw = Firmware(done=("INFO;0", "INFO;8"))
    robot, t, c = make(fw, firmware=version)
    report = c.upload_gcode("G01 X1\n", **FAST)
    assert robot.commands[0].startswith(b"GCODE UPLOAD;")
    assert not report.continue_sent
    assert any("GCODE CONTINUE" in d for d in report.decisions)


def test_continue_is_sent_on_newer_firmware(make):
    fw = Firmware(version="01.02.03", done=("INFO;0", "INFO;8"))
    robot, t, c = make(fw, firmware=NEW)
    report = c.upload_gcode("G01 X1\n", **FAST)
    assert robot.commands[:2] == [b"GCODE CONTINUE;0", b"GCODE UPLOAD;10"]
    assert report.continue_sent


# --------------------------------------------------------------------------- #
# Upload: only ERR;0 is an upload failure (HIGH-1)
# --------------------------------------------------------------------------- #


def test_high1_stale_err_before_the_upload_does_not_fail_it(make):
    fw = Firmware(version="01.02.03", done=("INFO;0", "INFO;8"))
    robot, t, c = make(fw, firmware=NEW)
    robot.line("ERR;1")  # e.g. the reply to an earlier, unrelated command
    time.sleep(0.2)
    report = c.upload_gcode(PROGRAM, block_interval=0, done_timeout=2.0)
    assert report.confirmed_by == "INFO;8"


def test_high1_err_reply_to_continue_is_collected_not_a_failure(make):
    fw = Firmware(version="01.02.03", cont=("ERR;1",), done=("INFO;0", "INFO;8"))
    robot, t, c = make(fw, firmware=NEW)
    report = c.upload_gcode(PROGRAM, **dict(FAST, continue_wait=0.25))
    assert report.confirmed_by == "INFO;8"
    assert [(o.stage, o.record.event.code) for o in report.errors] == [("continue", 1)]


def test_high1_unrelated_err_and_warn_during_the_upload_are_collected(make):
    fw = Firmware(version="01.02.03", done=("ERR;10", "WARN;2", "INFO;0", "INFO;8"))
    robot, t, c = make(fw, firmware=NEW)
    report = c.upload_gcode(PROGRAM, **FAST)
    assert report.confirmed
    assert [o.record.event.code for o in report.errors] == [10]
    assert [o.record.event.code for o in report.warnings] == [2]


def test_high1_err0_is_an_upload_failure(make):
    fw = Firmware(version="01.02.03", done=("ERR;0",))
    robot, t, c = make(fw, firmware=NEW)
    with pytest.raises(cl.UploadFailedError) as ei:
        c.upload_gcode(PROGRAM, **FAST)
    assert ei.value.code == 0 and isinstance(ei.value, cl.GoliathError)
    assert ei.value.report is not None


def test_high1_safety_error_during_upload_always_raises(make):
    fw = Firmware(version="01.02.03", done=("ERR;2",))
    robot, t, c = make(fw, firmware=NEW)
    with pytest.raises(cl.GoliathSafetyError) as ei:
        c.upload_gcode(PROGRAM, **FAST)
    assert ei.value.code == 2


# --------------------------------------------------------------------------- #
# Upload: a stale INFO;8 never confirms a new upload (MED-3)
# --------------------------------------------------------------------------- #


def test_med3_late_info8_from_a_previous_upload_does_not_confirm_the_next(make):
    fw = Firmware(version="01.02.03", done=("ERR;0",))
    robot, t, c = make(fw, firmware=NEW)
    robot.line("INFO;8")  # the previous upload's confirmation, arriving late
    time.sleep(0.2)
    with pytest.raises(cl.UploadFailedError):
        c.upload_gcode(PROGRAM, block_interval=0, done_timeout=2.0)


def test_med3_info8_before_the_program_was_streamed_is_ignored(make):
    fw = Firmware(version="01.02.03", header=("INFO;8",), done=("ERR;0",))
    robot, t, c = make(fw, firmware=NEW)
    with pytest.raises(cl.UploadFailedError) as ei:
        c.upload_gcode(PROGRAM, **FAST)
    assert any("INFO;8" in note for note in ei.value.report.ignored)


# --------------------------------------------------------------------------- #
# Upload: firmware confirmation gates (MED-4)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("version", [OLD, None])
def test_med4_missing_info8_on_old_firmware_is_a_distinct_unconfirmed_error(make, version):
    fw = Firmware(done=("INFO;0",))
    robot, t, c = make(fw, firmware=version)
    t0 = time.monotonic()
    with pytest.raises(cl.UploadUnconfirmedError) as ei:
        c.upload_gcode(PROGRAM, **dict(FAST, done_timeout=5.0))
    assert time.monotonic() - t0 < 3.0  # does not sit out done_timeout after INFO;0
    msg = str(ei.value)
    assert "01.02.03" in msg and "may not" in msg
    assert ei.value.report.end_of_upload_ack is not None
    assert c.upload_hazard is None  # INFO;0: the robot left upload mode


def test_med4_info0_can_be_accepted_explicitly(make):
    fw = Firmware(done=("INFO;0",))
    robot, t, c = make(fw, firmware=OLD)
    report = c.upload_gcode(PROGRAM, **dict(FAST, confirm="info8-or-info0"))
    assert report.confirmed_by.startswith("INFO;0")
    assert report.confirmed


def test_med4_info8_on_old_firmware_confirms(make):
    fw = Firmware(done=("INFO;0", "INFO;8"))
    robot, t, c = make(fw, firmware=OLD)
    assert c.upload_gcode(PROGRAM, **FAST).confirmed_by == "INFO;8"


def test_med4_newer_firmware_missing_info8_is_unconfirmed(make):
    fw = Firmware(version="01.02.03", done=("INFO;0",))
    robot, t, c = make(fw, firmware=NEW)
    with pytest.raises(cl.UploadUnconfirmedError) as ei:
        c.upload_gcode(PROGRAM, **dict(FAST, done_timeout=0.6))
    assert "should" in str(ei.value)


def test_med4_no_end_of_upload_signal_blocks_further_sends(make):
    fw = Firmware(done=())
    robot, t, c = make(fw, firmware=OLD)
    with pytest.raises(cl.UploadUnconfirmedError):
        c.upload_gcode(PROGRAM, **dict(FAST, done_timeout=0.5))
    assert c.upload_hazard
    n = len(robot.commands)
    with pytest.raises(cl.RobotStateUncertainError) as ei:
        c.send("INFO")
    assert "program data" in str(ei.value)
    assert len(robot.commands) == n
    c.clear_upload_hazard()
    c.send("INFO")
    assert robot.commands[-1] == b"INFO"


def test_med4_work_start_is_reported_when_unconfirmed(make):
    fw = Firmware(done=("INFO;0", "INFO;15", "INFO;17", lambda robot: robot.position(1)))
    robot, t, c = make(fw, firmware=OLD)
    with pytest.raises(cl.UploadUnconfirmedError) as ei:
        c.upload_gcode(PROGRAM, **FAST)
    started = ei.value.report.work_started
    # INFO arrives on connection 1 and positions on connection 2 (separate
    # reader threads), so only the order within a connection is fixed.
    codes = [o.record.event.code for o in started if isinstance(o.record.event, TextMessage)]
    assert set(codes) <= {15, 17} and codes and codes == sorted(codes)
    assert any(isinstance(o.record.event, PositionFrame) for o in started)
    assert "running" in str(ei.value).lower()


def test_med4_second_upload_needs_a_restart_on_old_firmware(make):
    fw = Firmware(done=("INFO;0", "INFO;8"))
    robot, t, c = make(fw, firmware=OLD)
    c.upload_gcode(PROGRAM, **FAST)
    n = len(robot.commands)
    with pytest.raises(cl.RestartRequiredError):
        c.upload_gcode(PROGRAM, **FAST)
    assert len(robot.commands) == n
    c.upload_gcode(PROGRAM, allow_second_upload=True, **FAST)
    c.note_robot_restarted()
    c.upload_gcode(PROGRAM, **FAST)
    assert len(fw.programs) == 3


def test_second_upload_is_allowed_on_newer_firmware(make):
    fw = Firmware(version="01.02.03", done=("INFO;0", "INFO;8"))
    robot, t, c = make(fw, firmware=NEW)
    c.upload_gcode(PROGRAM, **FAST)
    c.upload_gcode(PROGRAM, **FAST)
    assert len(fw.programs) == 2


# --------------------------------------------------------------------------- #
# Upload: the header acknowledgement (LOW-2)
# --------------------------------------------------------------------------- #


def test_low2_rejected_header_stops_the_upload_before_any_block(make):
    fw = Firmware(version="01.02.03", header=("ERR;1",))
    robot, t, c = make(fw, firmware=NEW)
    with pytest.raises(cl.UploadFailedError) as ei:
        c.upload_gcode(PROGRAM, block_interval=0, done_timeout=1.0)
    assert ei.value.stage == "header" and ei.value.code == 1
    assert payload_bytes(robot) == b""


def test_low2_required_info7_missing_stops_before_streaming(make):
    fw = Firmware(version="01.02.03")
    robot, t, c = make(fw, firmware=NEW)
    with pytest.raises(cl.UploadHeaderNotAcknowledgedError):
        c.upload_gcode(PROGRAM, **dict(FAST, header_ack="info7", header_ack_timeout=0.3))
    assert payload_bytes(robot) == b""
    assert c.upload_hazard  # the robot may be waiting for the announced bytes


def test_low2_required_info7_present_then_streams(make):
    fw = Firmware(version="01.02.03", header=("INFO;7",), done=("INFO;0", "INFO;8"))
    robot, t, c = make(fw, firmware=NEW)
    report = c.upload_gcode(PROGRAM, **dict(FAST, header_ack="info7"))
    assert report.header_ack is not None and report.header_ack.code == 7
    assert fw.programs == [clean_gcode(PROGRAM)]


def test_low2_observe_mode_records_info7_when_it_comes(make):
    fw = Firmware(version="01.02.03", header=("INFO;7",), done=("INFO;0", "INFO;8"))
    robot, t, c = make(fw, firmware=NEW)
    report = c.upload_gcode(PROGRAM, **dict(FAST, header_wait=0.1))
    assert report.header_ack is not None


def test_link_loss_mid_upload_leaves_the_hazard_set(make):
    fw = Firmware(version="01.02.03", done=())
    robot, t, c = make(fw, firmware=NEW)

    def drop_when_uploading():
        fw.uploading.wait(2.0)
        robot.eof(1)

    threading.Thread(target=drop_when_uploading).start()
    with pytest.raises(ConnectionError):
        c.upload_gcode(PROGRAM, block_size=32, block_interval=0.02, done_timeout=2.0)
    assert c.upload_hazard


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


def test_learn_firmware_version(make):
    robot, t, c = make(Firmware(version="01.02.00"))
    assert c.learn_firmware_version(timeout=2.0) == (1, 2, 0)
    assert c.firmware_version == (1, 2, 0)
    assert c.capabilities.has_abort_work is False


def test_learn_firmware_version_timeout_explains_slot_routing(make):
    robot, t, c = make()
    with pytest.raises(cl.GoliathTimeout) as ei:
        c.learn_firmware_version(timeout=0.3)
    assert "connection 1" in str(ei.value)


def test_parse_firmware_version():
    assert cl.parse_firmware_version("GFW 01.02.00   ") == (1, 2, 0)
    assert cl.parse_firmware_version("nonsense") is None


def test_safety_error_classification():
    assert cl.is_safety_error(TextMessage(kind="ERR", raw="ERR;16", code=16))
    assert cl.is_safety_error(TextMessage(kind="ERR", raw="ERR;2", code=2))
    assert not cl.is_safety_error(TextMessage(kind="ERR", raw="ERR;1", code=1))
    assert not cl.is_safety_error(TextMessage(kind="INFO", raw="INFO;8", code=8))


def test_firmware_version_is_validated_and_normalised(make):
    robot, t, c = make()
    c.firmware_version = [1, 2, 3]
    assert c.firmware_version == (1, 2, 3) and c.capabilities.has_abort_work
    with pytest.raises(ValueError):
        c.firmware_version = (1, 2)
    c.firmware_version = None
    assert c.capabilities.has_abort_work is False
