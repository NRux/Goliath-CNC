# SPDX-License-Identifier: Apache-2.0
"""GoliathTransport tests: three-connection link, slot routing, failure handling.

Real-socket tests talk only to a server bound to 127.0.0.1 in this process.
``SlotServer`` models what the GFW 01.02.00 firmware is known to do with its
client table (see the transport module docstring): each accepted connection
takes the first free of 6 slots, and output is sent by slot index, not to the
requester.
"""
import logging
import queue
import socket
import sys
import threading
import time
import warnings

import pytest

from goliath_link.framing import Discarded
from goliath_link.protocol import GlobalStatusBlob, PositionFrame, TextMessage, TowerFrame
from goliath_link.transport import (
    GoliathTransport,
    Keepalive,
    LinkEvent,
    ListenerError,
    ListenerFailed,
    NotConnectedError,
    TransportClosed,
    _default_opener,
)

FAST = dict(connect_gaps=(0.02, 0.02), settle=0.05, timeout=2.0)
HEAD = b"GLOBAL STATUS HEAD\x00"
TALE = b"GLOBAL STATUS TALE\x00"


def pad512(b: bytes) -> bytes:
    return b + b"\x00" * (512 - len(b))


class SlotServer:
    """A localhost stand-in for the robot's 6-slot client table."""

    def __init__(self, on_receive=None):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.srv.settimeout(0.1)
        self.port = self.srv.getsockname()[1]
        self.slots = [None] * 6
        self.accept_times = []
        self.received = {i: bytearray() for i in range(6)}
        self.eof = {i: threading.Event() for i in range(6)}
        self.on_receive = on_receive
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._accept, daemon=True)]
        self._threads[0].start()

    def _accept(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except (socket.timeout, OSError):
                continue
            with self._lock:
                slot = self.slots.index(None)
                self.slots[slot] = conn
                self.eof[slot].clear()
                self.accept_times.append((slot, time.monotonic()))
            th = threading.Thread(target=self._serve, args=(slot, conn), daemon=True)
            th.start()
            self._threads.append(th)

    def _serve(self, slot, conn):
        try:
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    data = b""
                if not data:
                    break
                with self._lock:
                    self.received[slot].extend(data)
                if self.on_receive is not None:
                    self.on_receive(self, slot, data)
        finally:
            with self._lock:
                if self.slots[slot] is conn:
                    self.slots[slot] = None
            self.eof[slot].set()
            conn.close()

    def wait_slots(self, n, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.accept_times) >= n:
                    return
            time.sleep(0.01)
        raise AssertionError(f"server saw {len(self.accept_times)} connections, wanted {n}")

    def send(self, slot, data):
        with self._lock:
            conn = self.slots[slot]
        if conn is None:  # the firmware silently drops sends to an empty slot
            return
        conn.sendall(data)

    def drop(self, slot):
        with self._lock:
            conn = self.slots[slot]
        conn.shutdown(socket.SHUT_RDWR)

    def stop(self):
        self._stop.set()
        self.srv.close()
        with self._lock:
            conns = [c for c in self.slots if c is not None]
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for th in self._threads:
            th.join(timeout=2.0)


def routing_robot(server, slot, data):
    """Reply the way GFW 01.02.00 routes output (firmware facts brief, section 1)."""
    cmd = data.rstrip(b"\x00")
    if cmd == b"INFO":
        server.send(1, pad512(b"INFO;34;GFW 01.02.00   ;1;2;00010002000A\n"))
        server.send(1, pad512(b"INFO;0\n"))
    elif cmd == b"GET GLOBAL SYS STATUS":
        server.send(slot, HEAD + bytes([1, 0, 1, 2, 1, 0, 0, 1, 0, 1]) + TALE)  # to requester
        server.send(1, pad512(b"INFO;0\n"))
    elif cmd == b"GET SPINDLE CAGE STATUS":
        server.send(0, pad512(b"CAGE STATUS: CLOSED\n"))
        server.send(1, pad512(b"INFO;0\n"))


@pytest.fixture
def robot():
    s = SlotServer(on_receive=routing_robot)
    yield s
    s.stop()


def drain_until(t, predicate, timeout=3.0):
    """Collect records until one satisfies ``predicate``; return all collected."""
    out = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"timed out; got {out}")
        rec = t.records.get(timeout=remaining)
        out.append(rec)
        if predicate(rec):
            return out


def is_closed(rec):
    return isinstance(rec.event, TransportClosed)


# --------------------------------------------------------------------------- #
# PROTO-1: three connections, send on S1 only, slot-routed replies
# --------------------------------------------------------------------------- #


def test_opens_three_connections_in_order_with_gaps(robot):
    opened = []

    def opener(h, p, timeout):
        s = _default_opener(h, p, timeout)
        opened.append(s)
        return s

    t = GoliathTransport("127.0.0.1", robot.port, opener=opener,
                         connect_gaps=(0.15, 0.15), settle=0.05)
    t0 = time.monotonic()
    t.connect()
    try:
        assert time.monotonic() - t0 >= 0.3
        robot.wait_slots(3)
        assert [s for s, _ in robot.accept_times] == [0, 1, 2]
        times = [at for _, at in robot.accept_times]
        assert times[1] - times[0] >= 0.1 and times[2] - times[1] >= 0.1
        assert t.connected and t.connection_count == 3
        for s in opened:
            assert s.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) != 0
    finally:
        t.close()


def test_default_connect_timing_matches_vendor():
    t = GoliathTransport()
    assert t.connection_count == 3
    assert t.connect_gaps == (0.25, 0.25)
    assert t.settle == 0.5


def test_commands_go_only_on_connection_zero(robot):
    with GoliathTransport("127.0.0.1", robot.port, **FAST) as t:
        t.send("INFO")
        drain_until(t, lambda r: isinstance(r.event, TextMessage) and r.event.code == 34)
        t.send("GET SPINDLE CAGE STATUS")
        drain_until(t, lambda r: isinstance(r.event, TextMessage) and r.event.kind == "TEXT")
    assert bytes(robot.received[0]) == b"INFO" + b"GET SPINDLE CAGE STATUS"
    assert bytes(robot.received[1]) == b""
    assert bytes(robot.received[2]) == b""


def test_slot_routed_replies_arrive_tagged_with_their_connection(robot):
    with GoliathTransport("127.0.0.1", robot.port, **FAST) as t:
        before = time.monotonic()
        t.send("INFO")
        recs = drain_until(t, lambda r: isinstance(r.event, TextMessage) and r.event.code == 34)
        info = recs[-1]
        assert isinstance(info, LinkEvent)
        assert info.conn == 1
        assert before <= info.t <= time.monotonic()
        assert info.event.params[0].startswith("GFW 01.02.00")

        t.send("GET GLOBAL SYS STATUS")
        recs = drain_until(t, lambda r: isinstance(r.event, GlobalStatusBlob))
        assert recs[-1].conn == 0
        assert recs[-1].event.fields == (1, 0, 1, 2, 1, 0, 0, 1, 0, 1)

        robot.send(2, pad512(PositionFrame(1.0, 2.0, 3.0, 45.0, 9).encode()))
        recs = drain_until(t, lambda r: isinstance(r.event, PositionFrame))
        assert recs[-1].conn == 2 and recs[-1].event.gcode_line == 9

        robot.send(0, bytes([0x3C, 0xBB, 0x60, 0x3E]))
        recs = drain_until(t, lambda r: isinstance(r.event, TowerFrame))
        assert recs[-1].conn == 0 and recs[-1].event.tower == 2


def test_incoming_view_yields_bare_events_from_the_same_queue(robot):
    with GoliathTransport("127.0.0.1", robot.port, **FAST) as t:
        t.send("INFO")
        deadline = time.monotonic() + 3
        seen = []
        while not any(isinstance(e, TextMessage) and e.code == 34 for e in seen):
            seen.append(t.incoming.get(timeout=max(0.01, deadline - time.monotonic())))
        assert all(not isinstance(e, LinkEvent) for e in seen)
        with pytest.raises(queue.Empty):
            while True:  # drain INFO;0 etc., then empty
                t.incoming.get_nowait()


def test_each_connection_has_its_own_framer(robot):
    # A partial frame on S3 must not swallow text arriving on S2.
    with GoliathTransport("127.0.0.1", robot.port, **FAST) as t:
        robot.wait_slots(3)
        robot.send(2, b"{\x01\x02")  # first 3 bytes of a position frame
        time.sleep(0.05)
        robot.send(1, pad512(b"ERR;2\n"))
        recs = drain_until(t, lambda r: isinstance(r.event, TextMessage))
        assert recs[-1].conn == 1 and recs[-1].event.code == 2


# --------------------------------------------------------------------------- #
# Connect failures are loud and name the connection
# --------------------------------------------------------------------------- #


def test_connect_failure_names_the_connection_and_closes_the_others(robot):
    calls = []

    def opener(h, p, timeout):
        calls.append(1)
        if len(calls) == 2:
            raise ConnectionRefusedError(111, "refused")
        return _default_opener(h, p, timeout)

    t = GoliathTransport("127.0.0.1", robot.port, opener=opener, **dict(FAST, connect_gaps=(0, 0)))
    with pytest.raises(ConnectionError) as ei:
        t.connect()
    assert "connection 1" in str(ei.value) and "of 3" in str(ei.value)
    assert isinstance(ei.value.__cause__, ConnectionRefusedError)
    assert not t.connected
    assert robot.eof[0].wait(2.0), "connection 0 was left open after the failed connect"
    with pytest.raises(NotConnectedError):
        t.send("INFO")


def test_connection_lost_during_settle_fails_connect(robot):
    t = GoliathTransport("127.0.0.1", robot.port, connect_gaps=(0.02, 0.02), settle=0.5)
    th = threading.Thread(target=lambda: (robot.wait_slots(3), robot.drop(0)), daemon=True)
    th.start()
    with pytest.raises(ConnectionError) as ei:
        t.connect()
    th.join()
    assert "connection 0" in str(ei.value)
    assert not t.connected


# --------------------------------------------------------------------------- #
# Losing ANY connection takes the whole link down
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("slot", [0, 1, 2])
def test_losing_any_connection_is_link_down(robot, slot):
    t = GoliathTransport("127.0.0.1", robot.port, **FAST)
    t.connect()
    try:
        robot.wait_slots(3)
        robot.drop(slot)
        recs = drain_until(t, is_closed)
        closed = recs[-1].event
        assert closed.conn == slot and recs[-1].conn == slot
        assert isinstance(closed.error, ConnectionError)
        assert f"connection {slot}" in str(closed.error)
        assert not t.connected
        with pytest.raises(NotConnectedError) as ei:
            t.send("INFO")
        assert f"connection {slot}" in str(ei.value)
        assert isinstance(ei.value, RuntimeError) and isinstance(ei.value, ConnectionError)
        for other in range(3):
            assert robot.eof[other].wait(2.0), f"connection {other} left open after link loss"
        # Exactly one TransportClosed, and it is the last record.
        time.sleep(0.1)
        assert t.records.empty()
    finally:
        t.close()


def test_local_close_emits_one_clean_transport_closed_last(robot):
    t = GoliathTransport("127.0.0.1", robot.port, **FAST)
    t.connect()
    t.close()
    t.close()  # idempotent
    recs = []
    while not t.records.empty():
        recs.append(t.records.get_nowait())
    assert [r for r in recs if is_closed(r)] == [recs[-1]]
    assert recs[-1].event == TransportClosed(error=None, conn=None)
    assert not t.connected


def test_close_wakes_all_readers_promptly(robot):
    t = GoliathTransport("127.0.0.1", robot.port, **FAST)
    t.connect()
    threads = t.threads
    assert len(threads) >= 3
    t0 = time.monotonic()
    t.close()
    assert time.monotonic() - t0 < 1.0
    assert not any(th.is_alive() for th in threads)


# --------------------------------------------------------------------------- #
# MED-1: any reader exception becomes TransportClosed(error) and closes sockets
# --------------------------------------------------------------------------- #


class ScriptedSock:
    def __init__(self):
        self.q = queue.Queue()
        self.sent = []
        self.closed = False
        self.shut = False

    def recv(self, n):
        item = self.q.get()
        if isinstance(item, BaseException):
            raise item
        return item

    def sendall(self, data):
        self.sent.append(bytes(data))

    def shutdown(self, how):
        self.shut = True
        self.q.put(b"")

    def close(self):
        self.closed = True
        self.q.put(b"")


def fake_link(n=3, **kw):
    socks = [ScriptedSock() for _ in range(n)]
    it = iter(socks)
    t = GoliathTransport(opener=lambda h, p, to: next(it), connections=n,
                         connect_gaps=(0,) * (n - 1), settle=0, **kw)
    return t, socks


def test_med1_non_oserror_in_reader_is_reported_and_closes_everything():
    t, socks = fake_link()
    t.connect()
    socks[1].q.put(pad512(b"INFO;1\n"))
    socks[1].q.put(ValueError("parser exploded"))
    recs = drain_until(t, is_closed)
    assert any(isinstance(r.event, TextMessage) and r.event.code == 1 for r in recs)
    closed = recs[-1].event
    assert closed.conn == 1 and isinstance(closed.error, ValueError)
    assert all(s.closed for s in socks), "sockets must be closed so the robot sees FIN"
    assert not t.connected
    with pytest.raises(NotConnectedError):
        t.send("INFO")
    t.close()


def test_med1_reader_exception_is_visible_to_check():
    t, socks = fake_link()
    t.connect()
    socks[2].q.put(ValueError("boom"))
    drain_until(t, is_closed)
    with pytest.raises(ConnectionError) as ei:
        t.check()
    assert isinstance(ei.value.__cause__, ValueError)
    t.close()


def test_send_failure_takes_the_link_down():
    t, socks = fake_link()
    t.connect()

    def broken(_):
        raise BrokenPipeError(32, "broken pipe")

    socks[0].sendall = broken
    with pytest.raises(BrokenPipeError):
        t.send("INFO")
    recs = drain_until(t, is_closed)
    assert recs[-1].event.conn == 0 and isinstance(recs[-1].event.error, BrokenPipeError)
    with pytest.raises(NotConnectedError):
        t.send("INFO")
    t.close()


# --------------------------------------------------------------------------- #
# MED-2: listener exceptions are surfaced
# --------------------------------------------------------------------------- #


def test_med2_listener_exception_is_logged_queued_and_raised_by_check(caplog):
    calls = []

    def listener(rec):
        calls.append(rec)
        if isinstance(rec.event, TextMessage) and rec.event.kind == "ERR":
            rec.event.params[0]  # bug: bare "ERR;2" has no params -> IndexError

    t, socks = fake_link(listener=listener)
    t.connect()
    with caplog.at_level(logging.ERROR, logger="goliath_link.transport"):
        socks[1].q.put(pad512(b"ERR;2\n"))
        socks[1].q.put(pad512(b"INFO;23\n"))
        recs = drain_until(t, lambda r: isinstance(r.event, TextMessage) and r.event.code == 23)
    kinds = [type(r.event).__name__ for r in recs]
    assert kinds == ["ListenerFailed", "TextMessage", "TextMessage"]
    failed = recs[0].event
    assert isinstance(failed.error, IndexError)
    assert failed.record.event.code == 2 and recs[0].conn == 1
    assert any("listener" in r.getMessage().lower() for r in caplog.records)
    # The listener keeps being called for later events.
    assert any(isinstance(r.event, TextMessage) and r.event.code == 23 for r in calls)
    with pytest.raises(ListenerError) as ei:
        t.check()
    assert isinstance(ei.value.__cause__, IndexError)
    t.check()  # reported once; nothing new since
    t.close()


# --------------------------------------------------------------------------- #
# MED-6: reconnect starts from a clean slate
# --------------------------------------------------------------------------- #


def test_med6_reconnect_does_not_leak_the_old_link(robot):
    t = GoliathTransport("127.0.0.1", robot.port, **FAST)
    t.connect()
    robot.wait_slots(3)
    robot.send(1, b"INFO;3")      # partial text on the old S2
    robot.send(2, b"{\x00\x01")   # partial frame on the old S3
    time.sleep(0.05)
    old_records = t.records
    t.close()
    # Let the server free slots 0-2 first: reconnecting before the robot has
    # released them would land in other slots (a real-firmware hazard too).
    for slot in range(3):
        assert robot.eof[slot].wait(2.0)
    t.connect()
    try:
        assert t.records is not old_records
        assert t.records.empty(), "old link's records leaked into the new session"
        t.send("INFO")
        recs = drain_until(t, lambda r: isinstance(r.event, TextMessage) and r.event.code == 34)
        assert not any(is_closed(r) for r in recs)
        assert recs[-1].event.raw.startswith("INFO;34;GFW 01.02.00")
        assert recs[-1].conn == 1
    finally:
        t.close()
    # The old session's records are still readable from the old queue.
    old = []
    while not old_records.empty():
        old.append(old_records.get_nowait())
    assert is_closed(old[-1])


class StuckSock(ScriptedSock):
    """A socket whose recv() ignores shutdown/close until released."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def recv(self, n):
        self.release.wait()
        return b""

    def shutdown(self, how):
        self.shut = True

    def close(self):
        self.closed = True


def test_med6_reconnect_refused_while_an_old_reader_is_alive():
    socks = [StuckSock() for _ in range(3)] + [ScriptedSock() for _ in range(3)]
    it = iter(socks)
    t = GoliathTransport(opener=lambda h, p, to: next(it), connect_gaps=(0, 0), settle=0,
                         join_timeout=0.2)
    t.connect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        t.close()
    assert any("did not stop" in str(w.message) for w in caught)
    with pytest.raises(RuntimeError) as ei:
        t.connect()
    assert "still running" in str(ei.value)
    for s in socks[:3]:
        s.release.set()
    deadline = time.monotonic() + 2
    while any(th.is_alive() for th in t.threads) and time.monotonic() < deadline:
        time.sleep(0.01)
    t.connect()  # now allowed
    assert t.connected
    t.close()


# --------------------------------------------------------------------------- #
# MED-7: liveness
# --------------------------------------------------------------------------- #


def test_med7_keepalive_enabled_on_every_connection(robot):
    opened = []

    def opener(h, p, timeout):
        s = _default_opener(h, p, timeout)
        opened.append(s)
        return s

    ka = Keepalive(idle=7.0, interval=3.0, count=4)
    with GoliathTransport("127.0.0.1", robot.port, opener=opener, keepalive=ka, **FAST) as t:
        assert len(opened) == 3
        for s in opened:
            assert s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
            if sys.platform.startswith("linux"):
                assert s.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 7
                assert s.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 3
                assert s.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 4
        for info in t.connection_info():
            assert "SO_KEEPALIVE" in info.socket_options


def test_med7_last_receive_timestamps_per_connection(robot):
    with GoliathTransport("127.0.0.1", robot.port, **FAST) as t:
        assert t.last_receive == (None, None, None)
        before = time.monotonic()
        robot.send(1, pad512(b"INFO;0\n"))
        drain_until(t, lambda r: isinstance(r.event, TextMessage))
        lr = t.last_receive
        assert lr[0] is None and lr[2] is None
        assert lr[1] is not None and before <= lr[1] <= time.monotonic()
        info = t.connection_info()[1]
        assert info.index == 1 and info.bytes_received == 512 and info.last_receive == lr[1]


# --------------------------------------------------------------------------- #
# Recording hook, quiet flush, atomic sends, options
# --------------------------------------------------------------------------- #


def test_recorder_sees_every_byte_with_connection_and_direction(robot):
    log = []
    with GoliathTransport("127.0.0.1", robot.port, recorder=lambda *a: log.append(a), **FAST) as t:
        t.send("INFO")
        drain_until(t, lambda r: isinstance(r.event, TextMessage) and r.event.code == 0)
    sent = [(c, d, b) for c, d, b, _ in log if d == ">"]
    assert sent == [(0, ">", b"INFO")]
    received_on_1 = b"".join(b for c, d, b, _ in log if d == "<" and c == 1)
    assert received_on_1 == pad512(b"INFO;34;GFW 01.02.00   ;1;2;00010002000A\n") + pad512(b"INFO;0\n")
    assert all(isinstance(ts, float) for *_, ts in log)


def test_recorder_failure_is_not_swallowed():
    def recorder(conn, direction, data, ts):
        if direction == "<":
            raise OSError(28, "No space left on device")

    t, socks = fake_link(recorder=recorder)
    t.connect()
    socks[1].q.put(b"INFO;0\n")
    recs = drain_until(t, is_closed)
    assert isinstance(recs[-1].event.error, OSError) and recs[-1].event.conn == 1
    t.close()


def test_unterminated_reply_is_flushed_after_quiet_interval(robot):
    with GoliathTransport("127.0.0.1", robot.port, quiet_interval=0.2, **FAST) as t:
        robot.wait_slots(3)
        robot.send(0, b"count: 5")  # exact-length dispatcher reply, no terminator
        t0 = time.monotonic()
        recs = drain_until(t, lambda r: isinstance(r.event, TextMessage))
        assert time.monotonic() - t0 >= 0.15
        msg = recs[-1].event
        assert (msg.raw, msg.terminated, recs[-1].conn) == ("count: 5", False, 0)


def test_info_line_split_by_a_late_segment_is_not_cut_short(robot):
    # Wi-Fi retransmission can hold the rest of a line back for longer than
    # the quiet interval; INFO lines always end in '\n', so the framer waits.
    with GoliathTransport("127.0.0.1", robot.port, quiet_interval=0.1, **FAST) as t:
        robot.wait_slots(3)
        robot.send(1, b"INFO;3")
        time.sleep(0.4)
        robot.send(1, pad512(b"4;GFW 01.02.00   ;1;2;0001\n")[:-6])
        recs = drain_until(t, lambda r: isinstance(r.event, TextMessage))
        assert [(r.event.code, r.event.terminated) for r in recs] == [(34, True)]


def test_discarded_bytes_are_delivered_as_events():
    t, socks = fake_link()
    t.connect()
    socks[0].q.put(bytes([0x3C, 0x34, 0x12, 0x3E]) + pad512(b"CAGE STATUS: OPEN\n"))
    recs = drain_until(t, lambda r: isinstance(r.event, TextMessage))
    assert isinstance(recs[0].event, Discarded) and recs[0].conn == 0
    t.close()


def test_concurrent_sends_are_never_interleaved(robot):
    payloads = [bytes([65 + i]) * 3000 for i in range(4)]
    with GoliathTransport("127.0.0.1", robot.port, **FAST) as t:
        def worker(p):
            for _ in range(20):
                t.send_bytes(p)

        ths = [threading.Thread(target=worker, args=(p,)) for p in payloads]
        for th in ths:
            th.start()
        for th in ths:
            th.join()
        deadline = time.monotonic() + 3
        while len(robot.received[0]) < 4 * 20 * 3000 and time.monotonic() < deadline:
            time.sleep(0.01)
    data = bytes(robot.received[0])
    assert len(data) == 4 * 20 * 3000
    for i in range(0, len(data), 3000):
        assert len(set(data[i : i + 3000])) == 1, f"payload interleaved at {i}"


def test_send_lock_makes_multi_part_sends_atomic():
    t, socks = fake_link()
    t.connect()
    with t.send_lock:
        t.send_bytes(b"part1")
        t.send_bytes(b"part2")
    assert socks[0].sent == [b"part1", b"part2"]
    assert socks[1].sent == [] and socks[2].sent == []
    t.close()


def test_single_connection_mode_warns_that_slots_1_and_2_are_missed(caplog):
    t, socks = fake_link(n=1)
    with caplog.at_level(logging.WARNING, logger="goliath_link.transport"):
        t.connect()
    assert any("slot 1" in r.getMessage() for r in caplog.records)
    t.close()


@pytest.mark.parametrize("kw", [dict(connections=0), dict(connections=4),
                                dict(connections=3, connect_gaps=(0.1,)),
                                dict(settle=-1), dict(quiet_interval=0),
                                dict(quiet_interval=1.0, stall_timeout=0.5),
                                dict(position_lengths=(23,))])
def test_invalid_options_are_rejected(kw):
    with pytest.raises(ValueError):
        GoliathTransport(**kw)


def test_connect_twice_is_an_error():
    t, socks = fake_link()
    t.connect()
    with pytest.raises(RuntimeError):
        t.connect()
    t.close()
