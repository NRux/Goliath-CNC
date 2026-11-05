# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""Simulator setup phases (spec 03 steps B-F) over raw localhost sockets.

``SimulatedGoliath(setup_model=True)`` models what the robot does between
connecting and the upload: pairing that succeeds only with the towers' RF
pair, the encoder report (``INFO;24`` or ``INFO;33``), panel detection
streaming ``INFO;25`` vertices from a configurable polygon under a
configurable encoding, axis map and noise, ``INFO;27``, the wait for
``SET T2 POSITION``, ``INFO;6``, a park pose, position frames outside work,
the connect-time replay of ``INFO;4/6/24`` and ``INFO;42`` for an upload that
comes before ``SET PANEL SIZE``.

Almost every one of these behaviours is a guess (the firmware's setup state
machine was not traced), so each is a knob, and the knob test below proves
that every knob changes what the robot visibly does. Passing these tests
shows the simulator behaves as configured; it proves nothing about the robot.

Every socket here connects to 127.0.0.1 only.
"""
import math
import os
import re
import select
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from goliath_link import sim as simmod
from goliath_link.sim import SETUP_KNOBS, VERTEX_FRAMES, SimulatedGoliath

HOST = "127.0.0.1"
BLOCK = 512
HEAD = b"GLOBAL STATUS HEAD\x00"
TALE = b"GLOBAL STATUS TALE\x00"
IDENTITY_RE = re.compile(rb"INFO;34;GFW (\d\d\.\d\d\.\d\d) *;\d+;\d+;[0-9A-F]{12}\n")
VERTEX_RE = re.compile(rb"INFO;25;(-?\d+\.\d{4});(-?\d+\.\d{4})\n")
NUM = r"-?\d+\.\d{4}"

# A clearly rectangular panel (W != H) so identity and swap differ.
RECT = ((0.0, 0.0), (1200.0, 0.0), (1200.0, 800.0), (0.0, 800.0))
TRIANGLE = ((0.0, 0.0), (900.0, 0.0), (0.0, 600.0))
PROGRAM = b"G00 X0 Y0\r\nG01 X10 Y5 Z-1 C315\r\nG01 X20 Y5\r\n\r\n"
FAST = dict(setup_model=True, detection_step_delay=0.0)


# --------------------------------------------------------------------------- #
# helpers (raw sockets, as in test_sim.py)
# --------------------------------------------------------------------------- #

def pad(msg: bytes) -> bytes:
    assert len(msg) < BLOCK
    return msg + b"\x00" * (BLOCK - len(msg))


def recv_exact(sock: socket.socket, n: int, timeout: float = 3.0) -> bytes:
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"timed out: wanted {n} bytes, got {len(buf)}: {bytes(buf)[:80]!r}")
        r, _, _ = select.select([sock], [], [], remaining)
        if not r:
            continue
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise AssertionError(f"peer closed after {len(buf)} of {n} bytes: {bytes(buf)[:80]!r}")
        buf.extend(chunk)
    return bytes(buf)


def recv_block_text(sock: socket.socket, timeout: float = 3.0) -> bytes:
    """Read one 512-byte slot block, check its padding, return the message."""
    block = recv_exact(sock, BLOCK, timeout)
    msg = block.split(b"\x00", 1)[0]
    assert block == pad(msg), f"block is not message + NUL padding: {block[:80]!r}"
    return msg


def texts(sock: socket.socket, n: int):
    return [recv_block_text(sock) for _ in range(n)]


def recv_until(sock: socket.socket, last_prefix: bytes, limit: int = 40):
    """Read slot blocks up to and including the first that starts with ``last_prefix``."""
    got = []
    for _ in range(limit):
        msg = recv_block_text(sock)
        got.append(msg)
        if msg.startswith(last_prefix):
            return got
    raise AssertionError(f"no {last_prefix!r} within {limit} blocks: {got}")


def assert_silent(sock: socket.socket, wait: float = 0.2) -> None:
    r, _, _ = select.select([sock], [], [], wait)
    if r:
        try:
            data = sock.recv(4096)
        except ConnectionError as exc:
            raise AssertionError(f"expected silence, got {exc!r}") from exc
        raise AssertionError(f"expected no data, got {data[:80]!r}")


def peer_closed(sock: socket.socket, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        r, _, _ = select.select([sock], [], [], remaining)
        if not r:
            return False
        try:
            if sock.recv(4096) == b"":
                return True
        except ConnectionError:
            return True


def position_frames(sock: socket.socket, count: int):
    """Read ``count`` padded short position frames: (x, y, z, angle, line) each."""
    out = []
    for _ in range(count):
        block = recv_exact(sock, BLOCK)
        assert block[0:1] == b"{" and block[21:22] == b"}", block[:24]
        assert block[22:] == b"\x00" * (BLOCK - 22)
        out.append(struct.unpack("<ffffi", block[1:21]))
    return out


@pytest.fixture
def make_sim():
    sims = []

    def _make(**kw):
        kw.setdefault("profile", "gfw-01.02.00")
        kw.setdefault("work_line_delay", 0.002)
        s = SimulatedGoliath(**kw)
        s.start()
        sims.append(s)
        return s

    yield _make
    for s in sims:
        s.stop()
        assert s.errors == [], s.errors


@pytest.fixture
def socks():
    opened = []
    yield opened
    for s in opened:
        try:
            s.close()
        except OSError:
            pass


def connect(sim: SimulatedGoliath, opened: list) -> socket.socket:
    before = sim.accept_count
    s = socket.create_connection((HOST, sim.port), timeout=3.0)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    opened.append(s)
    assert sim.wait_for(lambda: sim.accept_count > before, timeout=3.0), "sim never accepted"
    return s


def vendor_connect(sim: SimulatedGoliath, opened: list, *, drain_identity: bool = True):
    """Open S1, S2, S3 in order (each accepted before the next)."""
    s1 = connect(sim, opened)
    s2 = connect(sim, opened)
    s3 = connect(sim, opened)
    if drain_identity:
        assert IDENTITY_RE.fullmatch(recv_block_text(s2))
    return s1, s2, s3


def send_cmd(sim: SimulatedGoliath, sock: socket.socket, cmd) -> None:
    """Send one command segment and wait until the sim has fully processed it."""
    data = cmd.encode("ascii") if isinstance(cmd, str) else cmd
    before = len(sim.events_of("done"))
    sock.sendall(data)
    assert sim.wait_for(lambda: len(sim.events_of("done")) > before, timeout=3.0), (
        f"sim did not process {data!r}; events: {sim.events[-8:]}"
    )


def detection(sim: SimulatedGoliath) -> str:
    return sim.setup_state()["detection"]


def wait_detection(sim: SimulatedGoliath, *states: str, timeout: float = 3.0) -> bool:
    return sim.wait_for(lambda: detection(sim) in states, timeout=timeout)


def vertex_values(lines):
    return [tuple(float(v) for v in line.decode("ascii").rstrip("\n").split(";")[2:]) for line in lines]


def close_and_wait_free(sim: SimulatedGoliath, conns) -> None:
    for c in conns:
        c.close()
    assert sim.wait_for(lambda: all(h is None for h in sim.slot_holders()), timeout=3.0), (
        sim.slot_holders()
    )


def frame_map(name: str, x: float, y: float):
    (a, b), (c, d) = VERTEX_FRAMES[name]
    return a * x + b * y, c * x + d * y


# --------------------------------------------------------------------------- #
# The default flow, step by step (spec 03 steps B-H)
# --------------------------------------------------------------------------- #

def test_full_setup_flow_defaults_in_spec03_order(make_sim, socks):
    sim = make_sim(setup_model=True)
    s1, s2, s3 = vendor_connect(sim, socks)  # fresh robot: the replay is INFO;34 only
    assert_silent(s2, 0.2)
    blob = HEAD + bytes([0, 0, 0, 0, 0, 0, 0, 0, 0, 0]) + TALE  # fresh boot, nothing reported yet
    send_cmd(sim, s1, "GET GLOBAL SYS STATUS")
    assert recv_exact(s1, 48) == blob
    assert texts(s2, 1) == [b"INFO;0\n"]  # not paired: no encoder report yet
    assert_silent(s2, 0.1)

    # B. PAIRING: SET RF CHANNELS with the towers' pair
    send_cmd(sim, s1, "SET RF CHANNELS;101;114")
    assert recv_exact(s1, 8) == b"<\xaa\x38>" + b"<\xbb\x34>"  # both towers answer
    assert texts(s2, 3) == [b"INFO;0\n", b"INFO;3\n", b"INFO;4\n"]

    # C. enter setup: GET GLOBAL SYS STATUS, then the encoder report
    send_cmd(sim, s1, "GET GLOBAL SYS STATUS")
    blob = recv_exact(s1, 48)
    assert blob[19:22] == bytes([3, 1, 1])  # pairing done, both towers paired
    assert texts(s2, 2) == [b"INFO;0\n", b"INFO;24\n"]
    assert_silent(s2, 0.2)  # detection waits for the operator (the PC sends nothing)

    # D. PANEL_DETECTION: started at the robot; INFO;5, the vertices, INFO;27
    assert sim.start_panel_detection() is True
    got = recv_until(s2, b"INFO;27;")
    assert got[0] == b"INFO;5\n"
    assert got[1:5] == [b"INFO;25;0.0000;0.0000\n", b"INFO;25;1200.0000;0.0000\n",
                        b"INFO;25;1200.0000;800.0000\n", b"INFO;25;0.0000;800.0000\n"]
    assert got[5] == b"INFO;27;1200.0000\n" and len(got) == 6
    assert wait_detection(sim, "wait-t2")
    assert_silent(s2, 0.3)  # D3: no INFO;6 before SET T2 POSITION
    assert_silent(s3, 0.0)  # no position frames outside work by default

    # D3. SET T2 POSITION -> INFO;0, then panel detection done = INFO;6
    send_cmd(sim, s1, "SET T2 POSITION;1")
    assert texts(s2, 2) == [b"INFO;0\n", b"INFO;6\n"]
    assert wait_detection(sim, "done")

    # E. cage check
    send_cmd(sim, s1, "GET SPINDLE CAGE STATUS")
    assert recv_exact(s1, 19) == b"CAGE STATUS: CLOSED"
    assert texts(s2, 1) == [b"INFO;0\n"]

    # F. setup COMPLETE: panel size, sanity report, status
    send_cmd(sim, s1, "SET PANEL SIZE;1200;800")
    assert texts(s2, 1) == [b"INFO;0\n"]
    send_cmd(sim, s1, "PRINT SYS REPORT")
    assert re.fullmatch(rb"INFO;40;(\d+;){15}END\n", recv_block_text(s2))
    send_cmd(sim, s1, "GET GLOBAL SYS STATUS")
    blob = recv_exact(s1, 48)
    assert blob[19] == 3 and blob[22] == 3 and blob[23] == 1 and blob[28] == 1  # paired, detected, encoder
    assert texts(s2, 1) == [b"INFO;0\n"]  # INFO;24 is reported once, not again
    assert_silent(s2, 0.1)

    # G/H. upload, then the work cycle
    send_cmd(sim, s1, b"GCODE UPLOAD;%d" % len(PROGRAM))
    send_cmd(sim, s1, PROGRAM)
    sim.wait_for_event("work-end", timeout=5.0)
    assert texts(s2, 6) == [b"INFO;0\n", b"INFO;15\n", b"INFO;17\n", b"INFO;18\n", b"INFO;16\n",
                            b"INFO;23\n"]
    assert sim.program == PROGRAM
    assert not [e for e in sim.events if e.kind == "sent" and e.data.startswith(b"INFO;42")]

    state = sim.setup_state()
    assert state["pairing"] == "paired" and state["encoder"] == "calibrated"
    assert state["panel_detected"] is True and state["t2_position"] == 1
    assert state["panel_size"] == (1200.0, 800.0)
    assert state["vertices"] == [tuple(p) for p in RECT]
    # Every default the setup model used is disclosed in the simulator's log.
    config = " ".join(e.note for e in sim.events_of("config"))
    for knob in SETUP_KNOBS:
        assert knob in config, knob
    assert "GUESS" in config


def test_pairing_only_with_configured_pair(make_sim, socks):
    sim = make_sim(**FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    for wrong in ("SET RF CHANNELS;1;2", "SET RF CHANNELS;114;101"):  # wrong, then swapped
        send_cmd(sim, s1, wrong)
        assert texts(s2, 2) == [b"INFO;0\n", b"INFO;3\n"]
        assert_silent(s1, 0.2)  # no tower frames: no tower listens on these channels
        assert_silent(s2, 0.1)  # no INFO;4
        assert sim.setup_state()["pairing"] == "pairing"
    assert [e.note.split(":")[0] for e in sim.events_of("pairing-no-towers")] == [
        "SET RF CHANNELS;1;2", "SET RF CHANNELS;114;101"]
    send_cmd(sim, s1, "GET GLOBAL SYS STATUS")
    assert recv_exact(s1, 48)[19] == 1  # GUESS encoding: pairing in progress
    assert texts(s2, 1) == [b"INFO;0\n"]  # not paired: no encoder report
    assert sim.start_panel_detection() is False  # nothing to detect with
    assert "not paired" in sim.events_of("detection-not-started")[-1].note
    send_cmd(sim, s1, "SET RF CHANNELS;101;114")
    assert recv_exact(s1, 8) == b"<\xaa\x38><\xbb\x34>"
    assert texts(s2, 3) == [b"INFO;0\n", b"INFO;3\n", b"INFO;4\n"]
    assert sim.setup_state()["pairing"] == "paired"

    other = make_sim(rf_pair=(102, 115), **FAST)
    o1, o2, o3 = vendor_connect(other, socks)
    send_cmd(other, o1, "SET RF CHANNELS;101;114")
    assert texts(o2, 2) == [b"INFO;0\n", b"INFO;3\n"]
    assert_silent(o2, 0.2)
    send_cmd(other, o1, "SET RF CHANNELS;102;115")
    assert recv_exact(o1, 8) == b"<\xaa\x38><\xbb\x34>"
    assert texts(o2, 3) == [b"INFO;0\n", b"INFO;3\n", b"INFO;4\n"]


def _pair_and_calibrate(sim, s1, s2) -> None:
    send_cmd(sim, s1, "SET RF CHANNELS;%d;%d" % sim.rf_pair)
    recv_exact(s1, 8)
    assert texts(s2, 3)[-1] == b"INFO;4\n"
    send_cmd(sim, s1, "GET GLOBAL SYS STATUS")
    recv_exact(s1, 48 if sim.global_status == "blob48" else 19)  # 19: the G...S frame
    assert texts(s2, 2) == [b"INFO;0\n", b"INFO;24\n"]


def test_vertex_stream_on_slot1_padded_512(make_sim, socks):
    polygon = ((0.0, 0.0), (1234.5, 0.0), (1234.5, 610.25), (0.0, 610.25))
    sim = make_sim(panel_polygon=polygon, **FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    _pair_and_calibrate(sim, s1, s2)
    assert sim.start_panel_detection()
    assert recv_exact(s2, BLOCK) == pad(b"INFO;5\n")
    lines = []
    for _ in polygon:
        block = recv_exact(s2, BLOCK)  # exactly one 512-byte block per vertex
        msg = block.split(b"\x00", 1)[0]
        assert block == pad(msg)
        assert VERTEX_RE.fullmatch(msg), msg  # INFO;25;%.4f;%.4f\n: two values (spec 09)
        lines.append(msg)
    assert vertex_values(lines) == [tuple(p) for p in polygon]
    assert recv_block_text(s2) == b"INFO;27;1234.5000\n"
    assert_silent(s1, 0.1)  # nothing on slot 0 or slot 2: the stream is INFO on slot 1
    assert_silent(s3, 0.0)
    sent = [e for e in sim.events_of("sent") if e.data.startswith(b"INFO;25")]
    assert [e.slot for e in sent] == [1] * 4 and [e.conn for e in sent] == [1] * 4
    assert [e.data for e in sim.events_of("vertex")] == lines


def test_vertex_stream_to_an_empty_slot1_is_dropped(make_sim, socks):
    sim = make_sim(**FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    _pair_and_calibrate(sim, s1, s2)
    s2.close()
    sim.wait_for_event("slot-freed", slot=1)
    assert sim.start_panel_detection()
    assert wait_detection(sim, "wait-t2")
    dropped = [e for e in sim.events_of("dropped") if e.data.startswith(b"INFO;25")]
    assert len(dropped) == 4 and all("empty" in e.note for e in dropped)


def test_info6_only_after_t2_position(make_sim, socks):
    sim = make_sim(**FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    _pair_and_calibrate(sim, s1, s2)
    assert sim.start_panel_detection()
    assert recv_until(s2, b"INFO;27;")[-1] == b"INFO;27;1200.0000\n"
    assert wait_detection(sim, "wait-t2")
    assert_silent(s2, 0.5)
    assert sim.setup_state()["panel_detected"] is False
    send_cmd(sim, s1, "SET T2 POSITION;0")
    assert texts(s2, 2) == [b"INFO;0\n", b"INFO;6\n"]
    assert wait_detection(sim, "done")
    state = sim.setup_state()
    assert state["panel_detected"] is True and state["t2_position"] == 0

    # wait_t2_position=False: INFO;6 follows INFO;27 at once; a later
    # SET T2 POSITION is only acknowledged, and logged as out of order.
    eager = make_sim(wait_t2_position=False, **FAST)
    e1, e2, e3 = vendor_connect(eager, socks)
    _pair_and_calibrate(eager, e1, e2)
    assert eager.start_panel_detection()
    got = recv_until(e2, b"INFO;6")
    assert got[-2:] == [b"INFO;27;1200.0000\n", b"INFO;6\n"]
    send_cmd(eager, e1, "SET T2 POSITION;1")
    assert texts(e2, 1) == [b"INFO;0\n"]
    assert_silent(e2, 0.2)
    assert eager.events_of("setup-out-of-order")


def test_t2_before_the_wait_is_ignored_by_default_and_latched_when_configured(make_sim, socks):
    sim = make_sim(**FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    _pair_and_calibrate(sim, s1, s2)
    send_cmd(sim, s1, "SET T2 POSITION;1")  # an improvised early send
    assert texts(s2, 1) == [b"INFO;0\n"]
    assert "not waiting" in sim.events_of("setup-out-of-order")[-1].note
    assert sim.start_panel_detection()
    recv_until(s2, b"INFO;27;")
    assert wait_detection(sim, "wait-t2")
    assert_silent(s2, 0.3)  # still waits: the early send did not count

    latched = make_sim(t2_before_wait="accepted", **FAST)
    l1, l2, l3 = vendor_connect(latched, socks)
    _pair_and_calibrate(latched, l1, l2)
    send_cmd(latched, l1, "SET T2 POSITION;1")
    assert texts(l2, 1) == [b"INFO;0\n"]
    assert latched.start_panel_detection()
    assert recv_until(l2, b"INFO;6")[-2:] == [b"INFO;27;1200.0000\n", b"INFO;6\n"]


def test_info27_requires_t2_exercises_the_timeout_path(make_sim, socks):
    sim = make_sim(info27_requires_t2=True, **FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    _pair_and_calibrate(sim, s1, s2)
    assert sim.start_panel_detection()
    got = texts(s2, 5)
    assert got[0] == b"INFO;5\n" and all(m.startswith(b"INFO;25;") for m in got[1:])
    assert wait_detection(sim, "wait-t2")
    # A runner that waits for INFO;27 before sending SET T2 POSITION (spec 03
    # D2/D3) hears nothing: its INFO;27 timeout is the path under test.
    assert_silent(s2, 0.6)
    send_cmd(sim, s1, "SET T2 POSITION;1")
    assert texts(s2, 3) == [b"INFO;0\n", b"INFO;27;1200.0000\n", b"INFO;6\n"]
    with pytest.raises(ValueError, match="info27_requires_t2"):
        SimulatedGoliath(profile="gfw-01.02.00", setup_model=True, info27_requires_t2=True,
                         wait_t2_position=False)


def _upload(sim, s1, program=PROGRAM, *, size=None):
    send_cmd(sim, s1, b"GCODE UPLOAD;%d" % (len(program) if size is None else size))
    send_cmd(sim, s1, program)


def test_info42_if_upload_before_panel_size(make_sim, socks):
    # Default: INFO;42 as the header arrives. The 01.02.00 header handler sets
    # the upload flag unconditionally (spec 09), so the robot still stores the
    # program; it does not start the work cycle without a panel size.
    sim = make_sim(**FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, b"GCODE UPLOAD;%d" % len(PROGRAM))
    assert texts(s2, 1) == [b"INFO;42\n"]
    send_cmd(sim, s1, b"INFO")  # parser is off: this is program data, not a command
    send_cmd(sim, s1, PROGRAM[: len(PROGRAM) - 4])
    sim.wait_for_event("upload-complete")
    assert sim.program == b"INFO" + PROGRAM[: len(PROGRAM) - 4]
    assert texts(s2, 1) == [b"INFO;0\n"]  # left upload mode
    sim.wait_for_event("work-not-started")
    assert_silent(s2, 0.3)  # no INFO;15/17: no work cycle
    assert not sim.events_of("work-start")
    assert "SET PANEL SIZE" in sim.events_of("work-not-started")[0].note

    # With SET PANEL SIZE first: no INFO;42 and the work runs.
    ok = make_sim(**FAST)
    o1, o2, o3 = vendor_connect(ok, socks)
    send_cmd(ok, o1, "SET PANEL SIZE;1200;800")
    assert texts(o2, 1) == [b"INFO;0\n"]
    _upload(ok, o1)
    ok.wait_for_event("work-end", timeout=5.0)
    assert texts(o2, 6)[0] == b"INFO;0\n"
    assert not [e for e in ok.events_of("sent") if e.data == b"INFO;42\n"]

    # info42_at="upload-end": the header is silent; INFO;42 follows the end-of-upload INFO;0.
    late = make_sim(info42_at="upload-end", **FAST)
    l1, l2, l3 = vendor_connect(late, socks)
    send_cmd(late, l1, b"GCODE UPLOAD;%d" % len(PROGRAM))
    assert_silent(l2, 0.2)
    send_cmd(late, l1, PROGRAM)
    assert texts(l2, 2) == [b"INFO;0\n", b"INFO;42\n"]
    late.wait_for_event("work-not-started")
    assert not late.events_of("work-start")

    # require_panel_size_before_upload=False: no INFO;42, the work runs.
    lax = make_sim(require_panel_size_before_upload=False, **FAST)
    x1, x2, x3 = vendor_connect(lax, socks)
    _upload(lax, x1)
    lax.wait_for_event("work-end", timeout=5.0)
    assert texts(x2, 3) == [b"INFO;0\n", b"INFO;15\n", b"INFO;17\n"]


def test_park_pose_reported_in_frames_when_enabled(make_sim, socks):
    park = (600.0, 400.0, 10.0, 90.0)
    sim = make_sim(detection_frames=True, park_pose=park, **FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    _pair_and_calibrate(sim, s1, s2)
    assert sim.start_panel_detection()
    frames = position_frames(s3, len(RECT))  # one frame per traced vertex
    assert [(f[0], f[1]) for f in frames] == [tuple(p) for p in RECT]
    assert all(f[4] == 0 for f in frames)  # GUESS: line 0 outside work
    assert wait_detection(sim, "wait-t2")
    assert_silent(s3, 0.2)
    send_cmd(sim, s1, "SET T2 POSITION;1")
    assert recv_until(s2, b"INFO;6")[-2:] == [b"INFO;0\n", b"INFO;6\n"]
    (parked,) = position_frames(s3, 1)  # the park move, reported after INFO;6
    assert parked[:4] == park and parked[4] == 0
    assert sim.wait_for_event("parked").note.startswith("park pose (600.0, 400.0, 10.0, 90.0)")
    assert sim.pos == list(park)

    # Without detection frames the park pose is the robot's pose, but nothing reports it.
    quiet = make_sim(park_pose=park, **FAST)
    q1, q2, q3 = vendor_connect(quiet, socks)
    _pair_and_calibrate(quiet, q1, q2)
    assert quiet.start_panel_detection()
    assert wait_detection(quiet, "wait-t2")
    send_cmd(quiet, q1, "SET T2 POSITION;1")
    assert wait_detection(quiet, "done")
    quiet.wait_for_event("parked")
    assert_silent(q3, 0.3)
    assert quiet.pos == list(park)


def test_no_park_pose_leaves_the_robot_at_the_last_traced_vertex(make_sim, socks):
    sim = make_sim(detection_frames=True, **FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    _pair_and_calibrate(sim, s1, s2)
    assert sim.start_panel_detection()
    position_frames(s3, len(RECT))
    assert wait_detection(sim, "wait-t2")
    send_cmd(sim, s1, "SET T2 POSITION;1")
    assert wait_detection(sim, "done")
    note = sim.wait_for_event("parked").note
    assert "no park pose configured" in note and "GUESS" in note
    assert sim.pos[:2] == list(RECT[-1])
    assert_silent(s3, 0.2)  # no park move, so no extra frame


def test_frames_outside_work_knob(make_sim, socks):
    # True: the robot streams position frames whenever no job runs, even at idle.
    sim = make_sim(frames_outside_work=True, idle_frame_interval=0.05, **FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    frames = position_frames(s3, 3)
    assert all(f[4] == 0 for f in frames)
    assert sim.setup_state()["pairing"] == "idle"  # before any setup command

    # "after-detection": nothing at idle, frames once INFO;6 has been sent.
    after = make_sim(frames_outside_work="after-detection", idle_frame_interval=0.05,
                     park_pose=(10.0, 20.0, 10.0, 0.0), **FAST)
    a1, a2, a3 = vendor_connect(after, socks)
    assert_silent(a3, 0.3)
    _pair_and_calibrate(after, a1, a2)
    assert after.start_panel_detection()
    assert wait_detection(after, "wait-t2")
    assert_silent(a3, 0.2)  # during detection, detection_frames decides (off)
    send_cmd(after, a1, "SET T2 POSITION;1")
    assert wait_detection(after, "done")
    frames = position_frames(a3, 2)
    assert all(f[:2] == (10.0, 20.0) for f in frames)

    # Default (False): silent at idle and after detection.
    off = make_sim(idle_frame_interval=0.05, **FAST)
    f1, f2, f3 = vendor_connect(off, socks)
    _pair_and_calibrate(off, f1, f2)
    assert off.start_panel_detection()
    assert wait_detection(off, "wait-t2")
    send_cmd(off, f1, "SET T2 POSITION;1")
    assert wait_detection(off, "done")
    assert_silent(f3, 0.4)


def test_frames_outside_work_pause_while_the_job_runs(make_sim, socks):
    sim = make_sim(frames_outside_work=True, idle_frame_interval=0.02,
                   require_panel_size_before_upload=False, work_line_delay=0.05, **FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    time.sleep(0.15)  # idle: a few line-0 frames first
    program = b"".join(b"G01 X%d Y0\r\n" % i for i in range(10)) + b"\r\n"
    _upload(sim, s1, program)
    sim.wait_for_event("work-end", timeout=5.0)
    lines = [struct.unpack("<i", e.data[17:21])[0] for e in sim.events
             if e.kind == "sent" and e.slot == 2]
    first = next(i for i, n in enumerate(lines) if n > 0)
    last = max(i for i, n in enumerate(lines) if n > 0)
    # Idle frames (line 0) before and after the job, none in between: while
    # the work cycle runs, only its own frames (lines 1..n) are sent.
    assert 0 in lines[:first]
    assert lines[first:last + 1] == list(range(1, last - first + 2))


def test_replay_info4_info6_when_already_paired(make_sim, socks):
    sim = make_sim(**FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    _pair_and_calibrate(sim, s1, s2)
    # Paired and encoder reported, not detected: the replay says so.
    close_and_wait_free(sim, (s1, s2, s3))
    s1, s2, s3 = vendor_connect(sim, socks, drain_identity=False)
    got = texts(s2, 3)
    assert IDENTITY_RE.fullmatch(got[0])
    assert got[1:] == [b"INFO;4\n", b"INFO;24\n"]
    assert sim.start_panel_detection()
    recv_until(s2, b"INFO;27;")
    send_cmd(sim, s1, "SET T2 POSITION;1")
    assert texts(s2, 2) == [b"INFO;0\n", b"INFO;6\n"]
    close_and_wait_free(sim, (s1, s2, s3))
    s1, s2, s3 = vendor_connect(sim, socks, drain_identity=False)
    got = texts(s2, 4)
    assert IDENTITY_RE.fullmatch(got[0])
    assert got[1:] == [b"INFO;6\n", b"INFO;4\n", b"INFO;24\n"]  # spec 09 order: 8, 6, 4, then 24
    assert_silent(s2, 0.2)

    # replay_setup=False: the replay is conditional; only the identity arrives.
    bare = make_sim(replay_setup=False, **FAST)
    b1, b2, b3 = vendor_connect(bare, socks)
    _pair_and_calibrate(bare, b1, b2)
    close_and_wait_free(bare, (b1, b2, b3))
    b1, b2, b3 = vendor_connect(bare, socks)
    assert_silent(b2, 0.3)


def test_detection_interrupted_by_reboot(make_sim, socks):
    sim = make_sim(setup_model=True, detection_step_delay=0.3)
    s1, s2, s3 = vendor_connect(sim, socks)
    _pair_and_calibrate(sim, s1, s2)
    assert sim.start_panel_detection()
    assert texts(s2, 2) == [b"INFO;5\n", b"INFO;25;0.0000;0.0000\n"]
    sim.reboot()
    assert peer_closed(s1) and peer_closed(s2) and peer_closed(s3)
    time.sleep(0.8)  # longer than two more detection steps
    reboot_t = sim.events_of("reboot")[0].t
    late = [e for e in sim.events if e.t > reboot_t and e.kind in ("sent", "dropped")
            and e.data.startswith((b"INFO;25", b"INFO;27", b"INFO;6"))]
    assert late == []
    sim.wait_for_event("detection-interrupted")
    state = sim.setup_state()
    assert (state["pairing"], state["encoder"], state["detection"]) == ("idle", None, "idle")
    assert state["panel_detected"] is False and state["panel_size"] is None
    # After the power cycle the robot is fresh: the replay is the identity only.
    s1, s2, s3 = vendor_connect(sim, socks)
    assert_silent(s2, 0.3)
    send_cmd(sim, s1, "GET GLOBAL SYS STATUS")
    assert recv_exact(s1, 48)[19:29] == bytes(10)


def test_reboot_on_the_newer_profile_is_cmd_sys_reset(make_sim, socks):
    sim = make_sim(profile="vendor-app-newer", **FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    _pair_and_calibrate(sim, s1, s2)
    send_cmd(sim, s1, "CMD SYS RESET")
    assert peer_closed(s2)
    assert sim.events_of("reboot")[0].note == "CMD SYS RESET"
    assert sim.setup_state()["pairing"] == "idle"


def test_encoder_not_calibrated_sends_info33_and_detection_cannot_start(make_sim, socks):
    sim = make_sim(encoder_calibrated=False, **FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "SET RF CHANNELS;101;114")
    recv_exact(s1, 8)
    texts(s2, 3)
    send_cmd(sim, s1, "GET GLOBAL SYS STATUS")
    assert recv_exact(s1, 48)[28] == 0
    assert texts(s2, 2) == [b"INFO;0\n", b"INFO;33\n"]
    assert sim.start_panel_detection() is False
    assert "INFO;33" in sim.events_of("detection-not-started")[-1].note
    assert_silent(s2, 0.2)


def test_encoder_report_after_pairing_when_configured(make_sim, socks):
    sim = make_sim(encoder_trigger="pairing", **FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "SET RF CHANNELS;101;114")
    assert texts(s2, 4) == [b"INFO;0\n", b"INFO;3\n", b"INFO;4\n", b"INFO;24\n"]
    send_cmd(sim, s1, "GET GLOBAL SYS STATUS")
    recv_exact(s1, 48)
    assert texts(s2, 1) == [b"INFO;0\n"]
    assert_silent(s2, 0.1)


def test_detection_autostart_starts_without_the_operator(make_sim, socks):
    sim = make_sim(detection_autostart=0.1, **FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    _pair_and_calibrate(sim, s1, s2)
    assert recv_block_text(s2) == b"INFO;5\n"
    assert "autostart" in sim.events_of("detection-start")[0].note


def test_pairing_delay_holds_the_towers_and_info4(make_sim, socks):
    sim = make_sim(pairing_delay=1.0, **FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "SET RF CHANNELS;101;114")
    assert texts(s2, 2) == [b"INFO;0\n", b"INFO;3\n"]
    assert sim.setup_state()["pairing"] == "pairing"
    assert_silent(s1, 0.2)
    assert recv_exact(s1, 8) == b"<\xaa\x38><\xbb\x34>"
    assert texts(s2, 1) == [b"INFO;4\n"]


def test_reboot_during_pairing_delay_cancels_it(make_sim, socks):
    sim = make_sim(pairing_delay=1.0, **FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "SET RF CHANNELS;101;114")
    sim.reboot()
    time.sleep(1.3)  # past the pairing delay
    assert sim.setup_state()["pairing"] == "idle"
    assert not [e for e in sim.events if e.data == b"INFO;4\n"]


# --------------------------------------------------------------------------- #
# Vertex encodings and the eight axis maps
# --------------------------------------------------------------------------- #

def test_vertex_frames_are_the_eight_origin_fixing_axis_maps():
    assert list(VERTEX_FRAMES) == ["identity", "swap", "mirror-x", "mirror-y", "rot180",
                                   "swap-mirror-x", "swap-mirror-y", "swap-rot180"]
    mats = set()
    for (a, b), (c, d) in VERTEX_FRAMES.values():
        # A signed permutation: keeps the origin (tower 1) and every distance.
        assert sorted(abs(v) for v in (a, b, c, d)) == [0, 0, 1, 1]
        assert abs(a * d - b * c) == 1 and (a == 0) == (d == 0)
        mats.add((a, b, c, d))
    assert len(mats) == 8  # all distinct: the whole group of axis maps
    x, y = 3.0, 5.0
    assert frame_map("identity", x, y) == (3, 5)
    assert frame_map("swap", x, y) == (5, 3)
    assert frame_map("mirror-x", x, y) == (-3, 5)
    assert frame_map("mirror-y", x, y) == (3, -5)
    assert frame_map("rot180", x, y) == (-3, -5)
    # The swapped forms: the map's own output with its two values exchanged.
    assert frame_map("swap-mirror-x", x, y) == (5, -3)
    assert frame_map("swap-mirror-y", x, y) == (-5, 3)
    assert frame_map("swap-rot180", x, y) == (-5, -3)


def _detect_lines(make_sim, socks, **kw):
    sim = make_sim(**dict(FAST, **kw))
    s1, s2, s3 = vendor_connect(sim, socks)
    _pair_and_calibrate(sim, s1, s2)
    assert sim.start_panel_detection()
    got = recv_until(s2, b"INFO;27;")
    return sim, [m for m in got if m.startswith(b"INFO;25;")]


@pytest.mark.parametrize("frame", list(VERTEX_FRAMES))
def test_each_vertex_frame_on_the_wire(make_sim, socks, frame):
    sim, lines = _detect_lines(make_sim, socks, vertex_frame=frame)
    expected = [frame_map(frame, x, y) for x, y in RECT]
    assert vertex_values(lines) == [(float(a), float(b)) for a, b in expected]
    assert all(VERTEX_RE.fullmatch(m) for m in lines)


def test_polar_encoding_is_length_and_angle_about_tower1(make_sim, socks):
    sim, lines = _detect_lines(make_sim, socks, vertex_encoding="polar", vertex_frame="swap")
    for (x, y), (length, angle) in zip(RECT, vertex_values(lines)):
        fx, fy = frame_map("swap", x, y)
        assert length == pytest.approx(math.hypot(fx, fy), abs=1e-4)
        if length:
            assert angle == pytest.approx(math.degrees(math.atan2(fy, fx)), abs=1e-4)
        else:
            assert angle == 0.0  # the origin vertex: angle reported as 0
    _, rad = _detect_lines(make_sim, socks, vertex_encoding="polar", polar_angle_unit="rad")
    angles = [v[1] for v in vertex_values(rad)]
    assert angles[2] == pytest.approx(math.atan2(800, 1200), abs=1e-4)


def test_xyz_encoding_has_three_fields_and_z_from_the_polygon(make_sim, socks):
    polygon = ((0.0, 0.0, 1.5), (900.0, 0.0), (900.0, 600.0, -2.0))
    sim, lines = _detect_lines(make_sim, socks, vertex_encoding="xyz", panel_polygon=polygon)
    assert all(re.fullmatch(rb"INFO;25;%s;%s;%s\n" % ((NUM.encode(),) * 3), m) for m in lines)
    assert vertex_values(lines) == [(0.0, 0.0, 1.5), (900.0, 0.0, 0.0), (900.0, 600.0, -2.0)]


def test_vendor_newer_profile_defaults_to_three_field_vertices(make_sim, socks):
    sim, lines = _detect_lines(make_sim, socks, profile="vendor-app-newer")
    assert len(vertex_values(lines)[0]) == 3
    assert "vertex_encoding='xyz'" in " ".join(e.note for e in sim.events_of("config"))


def test_vertex_noise_is_bounded_and_seeded(make_sim, socks):
    _, a = _detect_lines(make_sim, socks, vertex_noise_mm=2.0)
    _, b = _detect_lines(make_sim, socks, vertex_noise_mm=2.0)
    _, c = _detect_lines(make_sim, socks, vertex_noise_mm=2.0, vertex_noise_seed=7)
    assert a == b and a != c  # deterministic for a seed
    for line_set in (a, c):
        values = vertex_values(line_set)
        assert values != [tuple(p) for p in RECT]
        for (x, y), (vx, vy) in zip(RECT, values):
            assert abs(vx - x) <= 2.0 + 1e-4 and abs(vy - y) <= 2.0 + 1e-4


# --------------------------------------------------------------------------- #
# Detection faults
# --------------------------------------------------------------------------- #

def _run_detection(make_sim, socks, faults, *, t2=True, **kw):
    sim = make_sim(detection_faults=faults, **dict(FAST, **kw))
    s1, s2, s3 = vendor_connect(sim, socks)
    _pair_and_calibrate(sim, s1, s2)
    assert sim.start_panel_detection()
    assert wait_detection(sim, "wait-t2", "stalled", "done")
    if t2 and detection(sim) == "wait-t2":
        send_cmd(sim, s1, "SET T2 POSITION;1")
        assert wait_detection(sim, "done")
    if detection(sim) == "done":
        sim.wait_for_event("parked")  # the last thing a completed detection logs
    sent = [e.data for e in sim.events if e.kind == "sent" and e.slot == 1]
    i = sent.index(b"INFO;24\n")
    return sim, (s1, s2, s3), [m for m in sent[i + 1:] if m != b"INFO;0\n"]


V = [b"INFO;25;0.0000;0.0000\n", b"INFO;25;1200.0000;0.0000\n", b"INFO;25;1200.0000;800.0000\n",
     b"INFO;25;0.0000;800.0000\n"]


@pytest.mark.parametrize("faults,expected,final", [
    ((), [b"INFO;5\n"] + V + [b"INFO;27;1200.0000\n", b"INFO;6\n"], "done"),
    (("no-info5",), V + [b"INFO;27;1200.0000\n", b"INFO;6\n"], "done"),
    (("skip-vertex:2",), [b"INFO;5\n", V[0], V[2], V[3], b"INFO;27;1200.0000\n", b"INFO;6\n"], "done"),
    (("duplicate-vertex:3",), [b"INFO;5\n"] + V[:3] + [V[2], V[3], b"INFO;27;1200.0000\n",
                                                       b"INFO;6\n"], "done"),
    (("stall-after:2",), [b"INFO;5\n", V[0], V[1]], "stalled"),
    (("no-info27",), [b"INFO;5\n"] + V + [b"INFO;6\n"], "done"),
    (("no-info6",), [b"INFO;5\n"] + V + [b"INFO;27;1200.0000\n"], "done"),
    (("info-after:1:41",), [b"INFO;5\n", V[0], b"INFO;41\n"] + V[1:]
     + [b"INFO;27;1200.0000\n", b"INFO;6\n"], "done"),
    (("err-after:3:3", "err-after:3:4"), [b"INFO;5\n"] + V[:3] + [b"ERR;3\n", b"ERR;4\n", V[3],
                                                                  b"INFO;27;1200.0000\n", b"INFO;6\n"],
     "done"),
])
def test_detection_faults(make_sim, socks, faults, expected, final):
    sim, _, got = _run_detection(make_sim, socks, faults)
    assert got == expected
    assert detection(sim) == final
    for f in faults:
        assert any(f in e.note for e in sim.events_of("fault")), f
    if "no-info6" in faults:
        # The robot finished (the message was lost): a reconnect replays INFO;6.
        assert sim.setup_state()["panel_detected"] is True


def test_link_drop_fault_closes_every_connection_and_detection_goes_on(make_sim, socks):
    sim = make_sim(detection_faults=("link-drop-after:2",), **FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    _pair_and_calibrate(sim, s1, s2)
    assert sim.start_panel_detection()
    assert texts(s2, 3) == [b"INFO;5\n", V[0], V[1]]
    assert peer_closed(s1) and peer_closed(s2) and peer_closed(s3)
    assert wait_detection(sim, "wait-t2")  # the robot keeps moving: it is not told
    dropped = [e.data for e in sim.events_of("dropped") if e.slot == 1]
    assert dropped == [V[2], V[3], b"INFO;27;1200.0000\n"]


@pytest.mark.parametrize("bad,match", [
    (("bogus",), "unknown detection fault"),
    (("skip-vertex",), "needs"),
    (("skip-vertex:0",), "between 1 and 4"),
    (("stall-after:5",), "between 1 and 4"),
    (("err-after:2",), "needs"),
    (("err-after:2:x",), "integer"),
    (("no-info5:3",), "takes no"),
])
def test_bad_detection_faults_fail_clearly(bad, match):
    with pytest.raises(ValueError, match=match):
        SimulatedGoliath(profile="gfw-01.02.00", setup_model=True, detection_faults=bad)


# --------------------------------------------------------------------------- #
# Every knob changes what the robot visibly does
# --------------------------------------------------------------------------- #

def _flow(sim, socks, *, pair=None, status=True, detect=True, early_t2=False, t2=True,
          panel_size=True, upload=True, reconnect=False, idle=0.0):
    """Drive a setup flow with bounded waits (never asserting on the robot's
    progress) and return what the robot sent: [(slot, message)] from its log."""
    s1, s2, s3 = vendor_connect(sim, socks, drain_identity=False)
    if idle:
        time.sleep(idle)
    if early_t2:
        send_cmd(sim, s1, "SET T2 POSITION;1")
    send_cmd(sim, s1, "SET RF CHANNELS;%d;%d" % (pair or (101, 114)))
    sim.wait_for(lambda: sim.setup_state()["pairing"] == "paired"
                 or sim.events_of("pairing-no-towers"), timeout=1.5)
    if status:
        send_cmd(sim, s1, "GET GLOBAL SYS STATUS")
    if detect:
        sim.wait_for(lambda: sim.setup_state()["encoder"] is not None, timeout=1.0)
        if sim.setup_state()["detection"] == "idle":
            sim.start_panel_detection()
        wait_detection(sim, "wait-t2", "done", "stalled", timeout=2.0)
        if t2:
            send_cmd(sim, s1, "SET T2 POSITION;1")
        wait_detection(sim, "done", "stalled", timeout=1.0 if t2 else 0.3)
        if detection(sim) == "done":
            sim.wait_for(lambda: sim.events_of("parked"), timeout=1.0)
    if panel_size:
        send_cmd(sim, s1, "SET PANEL SIZE;1200;800")
    if upload:
        _upload(sim, s1)
        sim.wait_for(lambda: sim.events_of("work-end") or sim.events_of("work-not-started"),
                     timeout=5.0)
    if reconnect:
        close_and_wait_free(sim, (s1, s2, s3))
        vendor_connect(sim, socks, drain_identity=False)
        time.sleep(0.2)
    return [(e.slot, e.data) for e in sim.events if e.kind == "sent" and e.note != "direct"]


def _infos(transcript):
    return [d for slot, d in transcript if slot == 1]


def _frames(transcript):
    return [struct.unpack("<ffffi", d[1:21]) for slot, d in transcript if slot == 2]


def _vertices(transcript):
    return vertex_values([d for d in _infos(transcript) if d.startswith(b"INFO;25;")])


def _times(sim, prefix):
    return [e.t for e in sim.events if e.kind == "sent" and e.data.startswith(prefix)]


def _k_rf_pair(make_sim, socks):
    base = _flow(make_sim(**FAST), socks, detect=False, upload=False)
    changed = _flow(make_sim(rf_pair=(102, 115), **FAST), socks, detect=False, upload=False)
    assert b"INFO;4\n" in _infos(base) and b"INFO;4\n" not in _infos(changed)


def _k_pairing_delay(make_sim, socks):
    a = make_sim(**FAST)
    _flow(a, socks, detect=False, upload=False)
    b = make_sim(pairing_delay=0.3, **FAST)
    _flow(b, socks, detect=False, upload=False)
    gap = lambda s: _times(s, b"INFO;4\n")[0] - _times(s, b"INFO;3\n")[0]  # noqa: E731
    assert gap(a) < 0.1 <= 0.3 <= gap(b) + 0.01


def _k_encoder_calibrated(make_sim, socks):
    base = _flow(make_sim(**FAST), socks, detect=False, upload=False)
    changed = _flow(make_sim(encoder_calibrated=False, **FAST), socks, detect=False, upload=False)
    assert b"INFO;24\n" in _infos(base) and b"INFO;33\n" in _infos(changed)
    assert b"INFO;24\n" not in _infos(changed)


def _k_encoder_trigger(make_sim, socks):
    base = _flow(make_sim(**FAST), socks, status=False, detect=False, upload=False)
    changed = _flow(make_sim(encoder_trigger="pairing", **FAST), socks, status=False,
                    detect=False, upload=False)
    assert b"INFO;24\n" not in _infos(base) and b"INFO;24\n" in _infos(changed)


def _k_detection_autostart(make_sim, socks):
    base = make_sim(**FAST)
    changed = make_sim(detection_autostart=0.0, **FAST)
    for sim in (base, changed):
        _flow(sim, socks, detect=False, upload=False, panel_size=False)
    assert changed.wait_for(lambda: detection(changed) == "wait-t2", timeout=2.0)
    assert detection(base) == "idle"
    assert not _times(base, b"INFO;5\n") and _times(changed, b"INFO;5\n")


def _k_panel_polygon(make_sim, socks):
    base = _flow(make_sim(**FAST), socks, upload=False)
    changed = _flow(make_sim(panel_polygon=TRIANGLE, **FAST), socks, upload=False)
    assert _vertices(base) == [tuple(p) for p in RECT]
    assert _vertices(changed) == [tuple(p) for p in TRIANGLE]


def _k_vertex_encoding(make_sim, socks):
    base = _flow(make_sim(**FAST), socks, upload=False)
    changed = _flow(make_sim(vertex_encoding="polar", **FAST), socks, upload=False)
    assert _vertices(changed)[1] == (1200.0, 0.0) and _vertices(changed)[3] == (800.0, 90.0)
    assert _vertices(base)[3] == (0.0, 800.0)


def _k_vertex_frame(make_sim, socks):
    base = _flow(make_sim(**FAST), socks, upload=False)
    changed = _flow(make_sim(vertex_frame="mirror-y", **FAST), socks, upload=False)
    assert [(x, -y if y else y) for x, y in _vertices(base)] == _vertices(changed)
    assert _vertices(base) != _vertices(changed)


def _k_vertex_noise_mm(make_sim, socks):
    base = _flow(make_sim(**FAST), socks, upload=False)
    changed = _flow(make_sim(vertex_noise_mm=3.0, **FAST), socks, upload=False)
    assert _vertices(base) == [tuple(p) for p in RECT] != _vertices(changed)


def _k_vertex_noise_seed(make_sim, socks):
    base = _flow(make_sim(vertex_noise_mm=3.0, **FAST), socks, upload=False)
    changed = _flow(make_sim(vertex_noise_mm=3.0, vertex_noise_seed=11, **FAST), socks,
                    upload=False)
    assert _vertices(base) != _vertices(changed)


def _k_polar_angle_unit(make_sim, socks):
    base = _flow(make_sim(vertex_encoding="polar", **FAST), socks, upload=False)
    changed = _flow(make_sim(vertex_encoding="polar", polar_angle_unit="rad", **FAST), socks,
                    upload=False)
    assert _vertices(base)[3][1] == 90.0
    assert _vertices(changed)[3][1] == pytest.approx(math.pi / 2, abs=1e-4)


def _k_tower_distance(make_sim, socks):
    base = _flow(make_sim(**FAST), socks, upload=False)
    changed = _flow(make_sim(tower_distance=1187.25, **FAST), socks, upload=False)
    assert b"INFO;27;1200.0000\n" in _infos(base)
    assert b"INFO;27;1187.2500\n" in _infos(changed)


def _k_wait_t2_position(make_sim, socks):
    base = _flow(make_sim(**FAST), socks, t2=False, upload=False)
    changed = _flow(make_sim(wait_t2_position=False, **FAST), socks, t2=False, upload=False)
    assert b"INFO;6\n" not in _infos(base) and b"INFO;6\n" in _infos(changed)


def _k_info27_requires_t2(make_sim, socks):
    base = _flow(make_sim(**FAST), socks, t2=False, upload=False)
    changed = _flow(make_sim(info27_requires_t2=True, **FAST), socks, t2=False, upload=False)
    assert any(d.startswith(b"INFO;27;") for d in _infos(base))
    assert not any(d.startswith(b"INFO;27;") for d in _infos(changed))


def _k_t2_before_wait(make_sim, socks):
    base = _flow(make_sim(**FAST), socks, early_t2=True, t2=False, upload=False)
    changed = _flow(make_sim(t2_before_wait="accepted", **FAST), socks, early_t2=True, t2=False,
                    upload=False)
    assert b"INFO;6\n" not in _infos(base) and b"INFO;6\n" in _infos(changed)


def _k_detection_step_delay(make_sim, socks):
    a = make_sim(**FAST)
    _flow(a, socks, upload=False)
    b = make_sim(setup_model=True, detection_step_delay=0.1)
    _flow(b, socks, upload=False)
    span = lambda s: _times(s, b"INFO;25;")[-1] - _times(s, b"INFO;25;")[0]  # noqa: E731
    assert span(a) < 0.1 and span(b) >= 0.3 - 0.01


def _k_detection_frames(make_sim, socks):
    base = _flow(make_sim(**FAST), socks, upload=False)
    changed = _flow(make_sim(detection_frames=True, **FAST), socks, upload=False)
    assert _frames(base) == []
    assert [f[:2] for f in _frames(changed)] == [tuple(p) for p in RECT]


def _k_frames_outside_work(make_sim, socks):
    base = _flow(make_sim(idle_frame_interval=0.05, **FAST), socks, idle=0.3, detect=False,
                 upload=False)
    changed = _flow(make_sim(frames_outside_work=True, idle_frame_interval=0.05, **FAST), socks,
                    idle=0.3, detect=False, upload=False)
    assert _frames(base) == [] and len(_frames(changed)) >= 2


def _k_idle_frame_interval(make_sim, socks):
    base = make_sim(frames_outside_work=True, **FAST)
    changed = make_sim(frames_outside_work=True, idle_frame_interval=0.05, **FAST)
    counts = []
    for sim in (base, changed):
        vendor_connect(sim, socks, drain_identity=False)
        time.sleep(0.45)
        counts.append(len([e for e in sim.events if e.kind == "sent" and e.slot == 2]))
    assert counts[0] <= 1 and counts[1] >= 4


def _k_park_pose(make_sim, socks):
    base = _flow(make_sim(detection_frames=True, **FAST), socks, upload=False)
    changed = _flow(make_sim(detection_frames=True, park_pose=(50.0, 60.0, 10.0, 45.0), **FAST),
                    socks, upload=False)
    assert len(_frames(base)) == 4 and _frames(changed)[-1][:4] == (50.0, 60.0, 10.0, 45.0)


def _k_detection_faults(make_sim, socks):
    base = _flow(make_sim(**FAST), socks, upload=False)
    changed = _flow(make_sim(detection_faults=("no-info5",), **FAST), socks, upload=False)
    assert b"INFO;5\n" in _infos(base) and b"INFO;5\n" not in _infos(changed)


def _k_require_panel_size_before_upload(make_sim, socks):
    base = _flow(make_sim(**FAST), socks, detect=False, panel_size=False)
    changed = _flow(make_sim(require_panel_size_before_upload=False, **FAST), socks, detect=False,
                    panel_size=False)
    assert b"INFO;42\n" in _infos(base) and b"INFO;23\n" not in _infos(base)
    assert b"INFO;42\n" not in _infos(changed) and b"INFO;23\n" in _infos(changed)


def _k_info42_at(make_sim, socks):
    base = _flow(make_sim(**FAST), socks, detect=False, panel_size=False)
    changed = _flow(make_sim(info42_at="upload-end", **FAST), socks, detect=False,
                    panel_size=False)
    tail = lambda t: _infos(t)[_infos(t).index(b"INFO;24\n") + 1:]  # noqa: E731
    assert tail(base) == [b"INFO;42\n", b"INFO;0\n"]
    assert tail(changed) == [b"INFO;0\n", b"INFO;42\n"]


def _k_replay_setup(make_sim, socks):
    base = _flow(make_sim(**FAST), socks, detect=False, upload=False, reconnect=True)
    changed = _flow(make_sim(replay_setup=False, **FAST), socks, detect=False, upload=False,
                    reconnect=True)
    assert _infos(base)[-2:] == [b"INFO;4\n", b"INFO;24\n"]
    assert IDENTITY_RE.fullmatch(_infos(changed)[-1])


KNOB_SCENARIOS = {
    "rf_pair": _k_rf_pair,
    "pairing_delay": _k_pairing_delay,
    "encoder_calibrated": _k_encoder_calibrated,
    "encoder_trigger": _k_encoder_trigger,
    "detection_autostart": _k_detection_autostart,
    "panel_polygon": _k_panel_polygon,
    "vertex_encoding": _k_vertex_encoding,
    "vertex_frame": _k_vertex_frame,
    "vertex_noise_mm": _k_vertex_noise_mm,
    "vertex_noise_seed": _k_vertex_noise_seed,
    "polar_angle_unit": _k_polar_angle_unit,
    "tower_distance": _k_tower_distance,
    "wait_t2_position": _k_wait_t2_position,
    "info27_requires_t2": _k_info27_requires_t2,
    "t2_before_wait": _k_t2_before_wait,
    "detection_step_delay": _k_detection_step_delay,
    "detection_frames": _k_detection_frames,
    "frames_outside_work": _k_frames_outside_work,
    "idle_frame_interval": _k_idle_frame_interval,
    "park_pose": _k_park_pose,
    "detection_faults": _k_detection_faults,
    "require_panel_size_before_upload": _k_require_panel_size_before_upload,
    "info42_at": _k_info42_at,
    "replay_setup": _k_replay_setup,
}


def test_every_setup_knob_has_an_observability_scenario():
    assert set(KNOB_SCENARIOS) == set(SETUP_KNOBS)


@pytest.mark.parametrize("knob", sorted(KNOB_SCENARIOS))
def test_each_knob_observable(make_sim, socks, knob):
    KNOB_SCENARIOS[knob](make_sim, socks)


# --------------------------------------------------------------------------- #
# Options: the setup model is opt-in, and nothing is silently ignored
# --------------------------------------------------------------------------- #

def test_setup_model_off_keeps_the_existing_behaviour_and_says_so(make_sim, socks):
    sim = make_sim()
    assert any("setup phases not modelled" in e.note for e in sim.events_of("config"))
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "SET RF CHANNELS;1;2")  # any pair pairs at once, as before
    assert texts(s2, 3) == [b"INFO;0\n", b"INFO;3\n", b"INFO;4\n"]
    _upload(sim, s1)  # no SET PANEL SIZE needed, as before
    sim.wait_for_event("work-end", timeout=5.0)
    with pytest.raises(RuntimeError, match="setup_model=True"):
        sim.start_panel_detection()
    with pytest.raises(RuntimeError, match="setup_model=True"):
        sim.setup_state()


@pytest.mark.parametrize("kw,match", [
    (dict(rf_pair=(1, 2)), "rf_pair.*setup_model=True"),
    (dict(vertex_frame="swap", park_pose=(0, 0, 0, 0)), "park_pose, vertex_frame.*setup_model=True"),
    (dict(setup_model=True, vertex_frame="diagonal"), "vertex_frame"),
    (dict(setup_model=True, vertex_encoding="lengths"), "vertex_encoding"),
    (dict(setup_model=True, panel_polygon=((0, 0), (1, 1))), "panel_polygon"),
    (dict(setup_model=True, panel_polygon=((0, 0), (1, 0), (0, float("nan")))), "panel_polygon"),
    (dict(setup_model=True, rf_pair=(101,)), "rf_pair"),
    (dict(setup_model=True, vertex_noise_mm=-1.0), "vertex_noise_mm"),
    (dict(setup_model=True, tower_distance=0.0), "tower_distance"),
    (dict(setup_model=True, park_pose=(1.0, 2.0)), "park_pose"),
    (dict(setup_model=True, frames_outside_work="sometimes"), "frames_outside_work"),
    (dict(setup_model=True, idle_frame_interval=0.0), "idle_frame_interval"),
    (dict(setup_model=True, info42_at="never"), "info42_at"),
    (dict(setup_model=True, encoder_trigger="button"), "encoder_trigger"),
    (dict(setup_model=True, t2_before_wait="maybe"), "t2_before_wait"),
    (dict(setup_model=True, polar_angle_unit="grad"), "polar_angle_unit"),
    (dict(setup_model=True, detection_autostart=-1.0), "detection_autostart"),
    (dict(setup_model=True, pairing_delay=-0.1), "pairing_delay"),
    (dict(setup_model=True, detection_step_delay=-0.1), "detection_step_delay"),
])
def test_bad_setup_options_fail_clearly(kw, match):
    with pytest.raises(ValueError, match=match):
        SimulatedGoliath(profile="gfw-01.02.00", **kw)


def test_legacy_profile_does_not_support_the_setup_model():
    with pytest.raises(ValueError, match="setup_model"):
        SimulatedGoliath(profile="legacy-spec", setup_model=True)


def test_start_panel_detection_twice_is_refused_while_running(make_sim, socks):
    sim = make_sim(**FAST)
    s1, s2, s3 = vendor_connect(sim, socks)
    _pair_and_calibrate(sim, s1, s2)
    assert sim.start_panel_detection()
    assert wait_detection(sim, "wait-t2")
    assert sim.start_panel_detection() is False
    assert "already" in sim.events_of("detection-not-started")[-1].note


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def test_cli_setup_flags_need_setup_model(capsys):
    with pytest.raises(SystemExit) as ei:
        simmod.main(["--port", "0", "--rf-pair", "1,2", "--duration", "0.1"])
    assert ei.value.code == 2
    assert "setup_model=True" in capsys.readouterr().err


def test_cli_help_lists_the_setup_flags(capsys):
    with pytest.raises(SystemExit):
        simmod.main(["--help"])
    out = capsys.readouterr().out
    for flag in ("--setup-model", "--rf-pair", "--pairing-delay", "--encoder-not-calibrated",
                 "--encoder-trigger", "--detection-autostart", "--panel-polygon",
                 "--vertex-encoding", "--vertex-frame", "--vertex-noise-mm", "--vertex-noise-seed",
                 "--polar-angle-unit", "--tower-distance", "--no-wait-t2-position",
                 "--info27-requires-t2", "--t2-before-wait", "--detection-step-delay",
                 "--detection-frames", "--frames-outside-work", "--idle-frame-interval",
                 "--park-pose", "--detection-fault", "--no-require-panel-size", "--info42-at",
                 "--no-replay-setup"):
        assert flag in out, flag


def test_cli_flags_map_to_the_knobs():
    args = simmod._build_parser().parse_args([
        "--setup-model", "--rf-pair", "102,115", "--pairing-delay", "0.5",
        "--encoder-not-calibrated", "--encoder-trigger", "pairing", "--detection-autostart", "2",
        "--panel-polygon", "0,0;900,0;900,600,1.5", "--vertex-encoding", "polar",
        "--vertex-frame", "swap-rot180", "--vertex-noise-mm", "1.5", "--vertex-noise-seed", "3",
        "--polar-angle-unit", "rad", "--tower-distance", "905", "--info27-requires-t2",
        "--t2-before-wait", "accepted", "--detection-step-delay", "0.2", "--detection-frames",
        "--frames-outside-work", "after-detection", "--idle-frame-interval", "0.25",
        "--park-pose", "1,2,3,4", "--detection-fault", "no-info5",
        "--detection-fault", "skip-vertex:2", "--no-require-panel-size",
        "--info42-at", "upload-end", "--no-replay-setup",
    ])
    kw = simmod._setup_kwargs(args)
    assert kw == dict(
        setup_model=True, rf_pair=(102, 115), pairing_delay=0.5, encoder_calibrated=False,
        encoder_trigger="pairing", detection_autostart=2.0,
        panel_polygon=((0.0, 0.0), (900.0, 0.0), (900.0, 600.0, 1.5)), vertex_encoding="polar",
        vertex_frame="swap-rot180", vertex_noise_mm=1.5, vertex_noise_seed=3,
        polar_angle_unit="rad", tower_distance=905.0, info27_requires_t2=True,
        t2_before_wait="accepted", detection_step_delay=0.2, detection_frames=True,
        frames_outside_work="after-detection", idle_frame_interval=0.25,
        park_pose=(1.0, 2.0, 3.0, 4.0), detection_faults=("no-info5", "skip-vertex:2"),
        require_panel_size_before_upload=False, info42_at="upload-end", replay_setup=False,
    )
    assert set(kw) - {"setup_model"} <= set(SETUP_KNOBS)
    assert simmod._setup_kwargs(simmod._build_parser().parse_args([])) == {}


def test_cli_serves_the_setup_model_over_tcp(socks):
    src = str(Path(simmod.__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "goliath_link.sim", "--port", "0", "--duration", "20",
         "--setup-model", "--detection-autostart", "0", "--detection-step-delay", "0",
         "--vertex-frame", "swap", "--quiet"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        text=True, encoding="ascii", errors="backslashreplace",
    )
    try:
        port = None
        lines = []
        for _ in range(20):
            line = proc.stdout.readline()
            lines.append(line)
            m = re.search(r"listening on 127\.0\.0\.1:(\d+)", line)
            if m:
                port = int(m.group(1))
                break
        assert port, f"no 'listening on' line in: {lines}"
        conns = []
        for _ in range(3):
            c = socket.create_connection((HOST, port), timeout=3.0)
            socks.append(c)
            conns.append(c)
            time.sleep(0.2)
        s1, s2, _ = conns
        assert IDENTITY_RE.fullmatch(recv_block_text(s2))
        s1.sendall(b"SET RF CHANNELS;101;114")
        assert recv_exact(s1, 8) == b"<\xaa\x38><\xbb\x34>"
        assert texts(s2, 3) == [b"INFO;0\n", b"INFO;3\n", b"INFO;4\n"]
        time.sleep(0.3)
        s1.sendall(b"GET GLOBAL SYS STATUS")
        recv_exact(s1, 48)
        got = recv_until(s2, b"INFO;27;")
        assert got[:3] == [b"INFO;0\n", b"INFO;24\n", b"INFO;5\n"]
        assert got[4] == b"INFO;25;0.0000;1200.0000\n"  # the swap frame
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        proc.stdout.close()
