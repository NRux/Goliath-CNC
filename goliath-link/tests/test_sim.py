# SPDX-License-Identifier: Apache-2.0
"""Simulator tests over raw localhost sockets (no goliath_link transport/client).

They pin the GFW 01.02.00 wire behaviour the simulator must reproduce
(docs/protocol/09-firmware-01.02.00.md and the firmware brief):

* six client slots, first free slot on accept, freed on disconnect;
* output routed BY SLOT (0: printf text + 4-byte tower frames, 1: every
  INFO/ERR/WARN, 2: position frames), dropped when the slot is empty;
* every slot message is one 512-byte NUL-padded block, tower frames are 4 bytes;
* direct replies (48-byte GLOBAL STATUS blob, CAGE STATUS, xmax=...) go to the
  requesting connection, exact length, unterminated;
* one received buffer = one command (strtok on ';', upper-cased, exact match
  against the 01.02.00 table); unknown names follow a configurable policy.

Every socket here connects to 127.0.0.1 only.
"""
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
from goliath_link.sim import (
    GFW_01_02_00_TABLE,
    OTHER_CLIENT,
    PROFILES,
    VENDOR_NEWER_EXTRA_COMMANDS,
    LegacyProfileWarning,
    SimulatedGoliath,
)

HOST = "127.0.0.1"
BLOCK = 512
HEAD = b"GLOBAL STATUS HEAD\x00"
TALE = b"GLOBAL STATUS TALE\x00"
IDENTITY_RE = re.compile(rb"INFO;34;GFW (\d\d\.\d\d\.\d\d) *;\d+;\d+;[0-9A-F]{12}\n")


# --------------------------------------------------------------------------- #
# helpers
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
            raise AssertionError(
                f"timed out: wanted {n} bytes, got {len(buf)}: {bytes(buf)[:80]!r}"
            )
        r, _, _ = select.select([sock], [], [], remaining)
        if not r:
            continue
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise AssertionError(f"peer closed after {len(buf)} of {n} bytes: {bytes(buf)[:80]!r}")
        buf.extend(chunk)
    return bytes(buf)


def recv_block_text(sock: socket.socket) -> bytes:
    """Read one 512-byte slot block, check its padding, return the message."""
    block = recv_exact(sock, BLOCK)
    msg = block.split(b"\x00", 1)[0]
    assert block == pad(msg), f"block is not message + NUL padding: {block[:80]!r}"
    return msg


def assert_silent(sock: socket.socket, wait: float = 0.2) -> None:
    r, _, _ = select.select([sock], [], [], wait)
    if r:
        try:
            data = sock.recv(4096)
        except ConnectionError as exc:  # reset is "not silent" either
            raise AssertionError(f"expected silence, got {exc!r}") from exc
        raise AssertionError(f"expected no data, got {data[:80]!r}")


def peer_closed(sock: socket.socket, timeout: float = 3.0) -> bool:
    r, _, _ = select.select([sock], [], [], timeout)
    if not r:
        return False
    try:
        return sock.recv(4096) == b""
    except ConnectionError:
        return True


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


def vendor_connect(sim: SimulatedGoliath, opened: list):
    """Open S1, S2, S3 in order (each accepted before the next), drain the replay."""
    s1 = connect(sim, opened)
    s2 = connect(sim, opened)
    s3 = connect(sim, opened)
    if sim.connect_replay and sim.slot_holders()[1] == 1:
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


# --------------------------------------------------------------------------- #
# profiles, table, construction
# --------------------------------------------------------------------------- #

def test_profiles_registry_and_command_table():
    assert set(PROFILES) == {"gfw-01.02.00", "vendor-app-newer", "legacy-spec"}
    assert len(GFW_01_02_00_TABLE) == 94
    assert GFW_01_02_00_TABLE[6] == "INFO"
    assert GFW_01_02_00_TABLE[23] == "GCODE UPLOAD"
    assert GFW_01_02_00_TABLE[86] == "GET GLOBAL SYS STATUS"
    assert GFW_01_02_00_TABLE[2] == "SYS RESET"
    gfw = PROFILES["gfw-01.02.00"]
    assert gfw.firmware == (1, 2, 0)
    assert gfw.dispatched == frozenset(GFW_01_02_00_TABLE[4:88])
    assert gfw.not_dispatched == frozenset(GFW_01_02_00_TABLE[:4] + GFW_01_02_00_TABLE[88:])
    for absent in ("ABORT WORK", "GCODE CONTINUE", "SEND TCP POS", "GET Z BACKLASH",
                   "SET Z BACKLASH", "GET Z PLATE OFFSET", "SET Z PLATE OFFSET",
                   "SET SPINDLE DISTANCE", "DISCARD TWRS DISTANCE", "CMD SYS RESET"):
        assert absent in VENDOR_NEWER_EXTRA_COMMANDS
        assert absent not in gfw.dispatched
    newer = PROFILES["vendor-app-newer"]
    assert newer.dispatched == gfw.dispatched | VENDOR_NEWER_EXTRA_COMMANDS
    assert newer.firmware > (1, 2, 0)
    assert "vendor app" in newer.basis.lower()


def test_command_table_matches_the_extracted_firmware_table():
    path = Path(__file__).resolve().parents[2] / "docs" / "protocol" / "09-firmware-01.02.00.md"
    if not path.exists():
        pytest.skip("spec 09 not present in this checkout")
    text = path.read_text(encoding="utf-8")
    for index, name in enumerate(GFW_01_02_00_TABLE):
        if index in range(4, 88):
            assert re.search(rf"\| {index} \| {re.escape(name)} \|", text), (index, name)


def test_bad_options_fail_clearly():
    with pytest.raises(ValueError, match="profile"):
        SimulatedGoliath(profile="gfw-9.9.9")
    with pytest.raises(ValueError, match="unknown_command"):
        SimulatedGoliath(profile="gfw-01.02.00", unknown_command="maybe")
    with pytest.raises(ValueError, match="occupied_slots"):
        SimulatedGoliath(profile="gfw-01.02.00", occupied_slots=(6,))
    with pytest.raises(ValueError, match="status_bytes"):
        SimulatedGoliath(profile="gfw-01.02.00", status_bytes=b"\x00" * 9)


def test_no_profile_falls_back_to_legacy_visibly():
    with pytest.warns(LegacyProfileWarning, match="gfw-01.02.00"):
        s = SimulatedGoliath()
    assert s.profile.name == "legacy-spec"
    assert any(e.kind == "config" and "legacy" in e.note for e in s.events)


# --------------------------------------------------------------------------- #
# slot table
# --------------------------------------------------------------------------- #

def test_slots_follow_accept_order(make_sim, socks):
    sim = make_sim()
    vendor_connect(sim, socks)
    assert sim.slot_holders() == (0, 1, 2, None, None, None)
    assert [(e.conn, e.slot) for e in sim.events_of("accept")] == [(0, 0), (1, 1), (2, 2)]


def test_first_free_slot_is_reused_after_disconnect(make_sim, socks):
    sim = make_sim()
    s1, s2, s3 = vendor_connect(sim, socks)
    s2.close()
    sim.wait_for_event("slot-freed", slot=1)
    connect(sim, socks)
    assert sim.slot_holders()[:4] == (0, 3, 2, None)


def test_slow_slot_release_puts_a_quick_reconnect_in_another_slot(make_sim, socks):
    sim = make_sim(slot_release_delay=0.6)
    s1, s2, s3 = vendor_connect(sim, socks)
    s2.close()
    sim.wait_for_event("peer-closed", conn=1)
    connect(sim, socks)  # too soon: slot 1 is not released yet
    assert sim.slot_holders()[:4] == (0, 1, 2, 3)
    send_cmd(sim, s1, "INFO")  # INFO;34 goes to slot 1, whose peer is gone
    dropped = sim.events_of("dropped")
    assert dropped and dropped[-1].slot == 1 and dropped[-1].data.startswith(b"INFO;34")
    assert "not yet released" in dropped[-1].note
    sim.wait_for_event("slot-freed", slot=1, timeout=3.0)
    assert sim.slot_holders()[:4] == (0, None, 2, 3)


def test_seventh_connection_is_accepted_then_closed(make_sim, socks):
    sim = make_sim()
    for _ in range(6):
        connect(sim, socks)
    seventh = socket.create_connection((HOST, sim.port), timeout=3.0)
    socks.append(seventh)
    sim.wait_for_event("reject-full", conn=6)
    assert peer_closed(seventh)
    assert sim.slot_holders() == (0, 1, 2, 3, 4, 5)


# --------------------------------------------------------------------------- #
# routing, padding, dropping
# --------------------------------------------------------------------------- #

def test_identity_reply_goes_to_slot1_padded_to_512(make_sim, socks):
    sim = make_sim(connect_replay=False)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "INFO")
    m = IDENTITY_RE.fullmatch(recv_block_text(s2))
    assert m and m.group(1) == b"01.02.00"
    assert_silent(s1)
    assert_silent(s3)


def test_single_connection_never_sees_info_and_the_drop_is_logged(make_sim, socks):
    sim = make_sim()
    s1 = connect(sim, socks)
    send_cmd(sim, s1, "INFO")
    assert_silent(s1)
    dropped = sim.events_of("dropped")
    assert [(e.slot, e.data[:8]) for e in dropped] == [(1, b"INFO;34;")]
    assert "empty" in dropped[0].note


def test_commands_are_accepted_from_any_connection(make_sim, socks):
    sim = make_sim(connect_replay=False)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s3, "INFO")
    assert IDENTITY_RE.fullmatch(recv_block_text(s2))
    assert_silent(s3)


def test_received_log_carries_connection_and_slot(make_sim, socks):
    sim = make_sim(connect_replay=False)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "INFO")
    send_cmd(sim, s3, "PRINT SYS REPORT")
    assert sim.received == ["INFO", "PRINT SYS REPORT"]
    assert [(r.conn, r.slot) for r in sim.received] == [(0, 0), (2, 2)]
    assert "conn=2" in repr(sim.received[1])


def test_sys_report_is_info40_on_slot1(make_sim, socks):
    sim = make_sim(connect_replay=False)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "PRINT SYS REPORT")
    assert re.fullmatch(rb"INFO;40;(\d+;){15}END\n", recv_block_text(s2))
    assert_silent(s2)  # no INFO;0 from this handler
    assert_silent(s1)


# --------------------------------------------------------------------------- #
# direct replies and unterminated text
# --------------------------------------------------------------------------- #

def test_global_status_is_a_48_byte_blob_to_the_requester(make_sim, socks):
    status = bytes([3, 1, 1, 0, 0, 0, 0, 0, 0, 1])
    sim = make_sim(connect_replay=False, status_bytes=status)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "GET GLOBAL SYS STATUS")
    assert recv_exact(s1, 48) == HEAD + status + TALE
    assert_silent(s1)  # exactly 48 bytes: no padding, no terminator
    assert recv_block_text(s2) == b"INFO;0\n"
    # The direct reply follows the requester, not slot 0.
    send_cmd(sim, s3, "GET GLOBAL SYS STATUS")
    assert recv_exact(s3, 48) == HEAD + status + TALE
    assert_silent(s1)


def test_default_status_bytes_are_ten_and_include_nul(make_sim, socks):
    sim = make_sim(connect_replay=False)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "GET GLOBAL SYS STATUS")
    blob = recv_exact(s1, 48)
    assert blob[:19] == HEAD and blob[29:] == TALE
    assert b"\x00" in blob[19:29]


def test_calibration_reads_are_unterminated_printf_on_slot0(make_sim, socks):
    sim = make_sim(connect_replay=False, index_calibration=28500, spindle_distance=12.34)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "READ INDEX CALIBRATION")
    assert recv_exact(s1, BLOCK) == pad(b"cfg read index : 28500")
    assert recv_block_text(s2) == b"INFO;0\n"
    send_cmd(sim, s3, "READ SPINDLE CALIB")  # printf follows slot 0, not the requester
    assert recv_exact(s1, BLOCK) == pad(b"spindle distance: 12.34")
    assert recv_block_text(s2) == b"INFO;0\n"
    assert_silent(s3)


@pytest.mark.parametrize(
    "cage,cage_reply,expected",
    [
        ("CLOSED", "direct", b"CAGE STATUS: CLOSED"),  # 19 bytes: '\n' cut by a 20-byte buffer
        ("OPEN", "direct", b"CAGE STATUS: OPEN\n"),
        ("CLOSED", "slot0", pad(b"CAGE STATUS: CLOSED\n")),
    ],
)
def test_cage_status_reply_shapes(make_sim, socks, cage, cage_reply, expected):
    sim = make_sim(connect_replay=False, cage=cage, cage_reply=cage_reply)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "GET SPINDLE CAGE STATUS")
    assert recv_exact(s1, len(expected)) == expected
    assert_silent(s1)
    assert recv_block_text(s2) == b"INFO;0\n"


def test_panel_size_round_trip_and_unterminated_direct_reply(make_sim, socks):
    sim = make_sim(connect_replay=False)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "SET PANEL SIZE;1200;800")
    assert recv_block_text(s2) == b"INFO;0\n"
    send_cmd(sim, s1, "GET PANEL SIZE")
    expected = b"xmax=1200.0000 \n\rymax=800.0000"
    assert recv_exact(s1, len(expected)) == expected
    assert_silent(s1)
    assert recv_block_text(s2) == b"INFO;0\n"


def test_tower_frames_are_4_bytes_unpadded_on_slot0(make_sim, socks):
    sim = make_sim(connect_replay=False, tower_bytes=(0x00, 0x0A))
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "SET RF CHANNELS;101;114")
    assert recv_exact(s1, 8) == b"<\xaa\x00>" + b"<\xbb\x0a>"
    assert_silent(s1)
    assert [recv_block_text(s2) for _ in range(3)] == [b"INFO;0\n", b"INFO;3\n", b"INFO;4\n"]


# --------------------------------------------------------------------------- #
# command parsing and the unknown-command policy
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "raw",
    [
        b"GCODE CONTINUE;0",           # vendor-app command absent from 01.02.00
        b"ABORT WORK",
        b"SEND TCP POS",
        b"GET Z BACKLASH",
        b"CMD SYS RESET",
        b"INFO\n",                     # a terminator breaks the exact match
        b"INFO\r\n",
        b" INFO",
        b"INFOGET GLOBAL SYS STATUS",  # two commands coalesced in one buffer
    ],
)
def test_unknown_commands_are_silent_by_default(make_sim, socks, raw):
    sim = make_sim(connect_replay=False)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, raw)
    for s in (s1, s2, s3):
        assert_silent(s, 0.1)
    ev = sim.events_of("unknown-command")
    assert [(e.conn, e.data) for e in ev] == [(0, raw)]
    assert "silent" in ev[0].note
    assert sim.slot_holders()[:3] == (0, 1, 2)  # nothing was dropped or reset


def test_unknown_command_error_policy_sends_err1_on_slot1(make_sim, socks):
    sim = make_sim(connect_replay=False, unknown_command="error")
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "ABORT WORK")
    assert recv_block_text(s2) == b"ERR;1\n"
    assert_silent(s1)


@pytest.mark.parametrize("name", ["SYS RESET", "SET PID", "OK", "INVALID COMMAND", "ERROR"])
def test_table_entries_without_a_handler_do_nothing(make_sim, socks, name):
    sim = make_sim(connect_replay=False, unknown_command="error")
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, name)
    for s in (s1, s2, s3):
        assert_silent(s, 0.1)
    assert [e.data for e in sim.events_of("not-dispatched")] == [name.encode()]
    assert not sim.events_of("unknown-command")


@pytest.mark.parametrize("raw", [b"info", b"Info;anything", b";INFO", b"INFO\x00junk"])
def test_name_is_strtok_token_upper_cased(make_sim, socks, raw):
    sim = make_sim(connect_replay=False)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, raw)
    assert IDENTITY_RE.fullmatch(recv_block_text(s2))


def test_arguments_are_not_upper_cased(make_sim, socks):
    sim = make_sim(connect_replay=False)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "set panel size;1200.5;800")
    assert recv_block_text(s2) == b"INFO;0\n"
    send_cmd(sim, s1, "GET PANEL SIZE")
    expected = b"xmax=1200.5000 \n\rymax=800.0000"
    assert recv_exact(s1, len(expected)) == expected


def test_coalescing_window_merges_back_to_back_writes_into_one_unknown_command(make_sim, socks):
    sim = make_sim(connect_replay=False, coalesce_window=0.15)
    s1, s2, s3 = vendor_connect(sim, socks)
    before = len(sim.events_of("done"))
    s1.sendall(b"INFO")
    s1.sendall(b"PRINT SYS REPORT")
    assert sim.wait_for(lambda: len(sim.events_of("done")) > before)
    assert [e.data for e in sim.events_of("unknown-command")] == [b"INFOPRINT SYS REPORT"]
    for s in (s1, s2, s3):
        assert_silent(s, 0.1)


def test_unmodelled_table_commands_are_flagged(make_sim, socks):
    sim = make_sim(connect_replay=False)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "GET RTC DATE")  # in the table, handler sends INFO;0, not simulated
    assert recv_block_text(s2) == b"INFO;0\n"
    send_cmd(sim, s1, "SANITY CHECK")  # empty handler on 01.02.00: nothing
    assert_silent(s2)
    assert [e.data for e in sim.events_of("unmodelled")] == [b"GET RTC DATE", b"SANITY CHECK"]


def test_line_framing_option_needs_a_newline(make_sim, socks):
    sim = make_sim(connect_replay=False, framing="line")
    s1, s2, s3 = vendor_connect(sim, socks)
    s1.sendall(b"INFO")
    assert_silent(s2, 0.3)
    send_cmd(sim, s1, b"\r\n")
    assert IDENTITY_RE.fullmatch(recv_block_text(s2))


# --------------------------------------------------------------------------- #
# connect-time replay, polling, other outputs
# --------------------------------------------------------------------------- #

def test_connect_replay_pushes_identity_to_the_second_connection(make_sim, socks):
    sim = make_sim()
    s1 = connect(sim, socks)
    s2 = connect(sim, socks)
    assert IDENTITY_RE.fullmatch(recv_block_text(s2))
    assert_silent(s1)


def test_connect_replay_can_be_disabled(make_sim, socks):
    sim = make_sim(connect_replay=False)
    connect(sim, socks)
    s2 = connect(sim, socks)
    assert_silent(s2, 0.3)


def test_poll_cage_prints_newline_terminated_status_on_slot0(make_sim, socks):
    sim = make_sim(connect_replay=False, cage_poll_interval=0.05)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "POLL CAGE")
    assert recv_block_text(s2) == b"INFO;0\n"
    assert recv_block_text(s1) == b"CAGE STATUS: CLOSED\n"
    send_cmd(sim, s1, "STOP POLL CAGE")
    assert recv_block_text(s2) == b"INFO;0\n"
    time.sleep(0.15)
    while select.select([s1], [], [], 0)[0]:  # discard blocks already in flight
        recv_exact(s1, BLOCK)
    assert_silent(s1, 0.2)


def test_get_ssids_streams_info31_on_slot1(make_sim, socks):
    sim = make_sim(connect_replay=False, ssids=("NetA", "NetB"))
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "GET SSIDS")
    got = [recv_block_text(s2) for _ in range(4)]
    assert got == [b"INFO;31;START\n", b"INFO;31;NetA\n", b"INFO;31;NetB\n", b"INFO;31;END\n"]


# --------------------------------------------------------------------------- #
# G-code upload and the work cycle
# --------------------------------------------------------------------------- #

PROGRAM = b"G00 X0 Y0\r\nG01 X10 Y5 Z-1 C315\r\nG01 X20 Y5\r\n\r\n"


def _position_frames(sock: socket.socket, count: int):
    frames = []
    for _ in range(count):
        block = recv_exact(sock, BLOCK)
        assert block[0:1] == b"{" and block[21:22] == b"}", block[:24]
        assert block[22:] == b"\x00" * (BLOCK - 22)
        frames.append(struct.unpack("<ffffi", block[1:21]))
    return frames


def test_upload_turns_the_parser_off_then_info0_and_work_cycle(make_sim, socks):
    sim = make_sim(connect_replay=False)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, b"GCODE UPLOAD;%d" % len(PROGRAM))
    assert_silent(s2)  # the 01.02.00 header handler sends nothing (INFO;7 not established)
    send_cmd(sim, s1, PROGRAM[:20])
    send_cmd(sim, s1, PROGRAM[20:])
    sim.wait_for_event("work-end", timeout=5.0)
    assert sim.program == PROGRAM
    assert [recv_block_text(s2) for _ in range(6)] == [
        b"INFO;0\n", b"INFO;15\n", b"INFO;17\n", b"INFO;18\n", b"INFO;16\n", b"INFO;23\n",
    ]
    n = len([e for e in sim.events_of("sent") if e.slot == 2])
    frames = _position_frames(s3, n)
    assert [f[4] for f in frames] == list(range(1, n + 1))
    assert round(frames[-1][0]) == 20 and round(frames[1][3]) == 315
    assert sim.received == ["GCODE UPLOAD;%d" % len(PROGRAM)]  # payload is not a command


def test_upload_takes_data_from_any_connection_and_swallows_commands(make_sim, socks):
    sim = make_sim(connect_replay=False, auto_start_work=False)
    s1, s2, s3 = vendor_connect(sim, socks)
    keepalive = b"GET GLOBAL SYS STATUS"
    total = len(PROGRAM) + len(keepalive)
    send_cmd(sim, s1, b"GCODE UPLOAD;%d" % total)
    send_cmd(sim, s1, PROGRAM[:20])
    send_cmd(sim, s3, keepalive)  # e.g. a keep-alive from another thread: becomes G-code
    send_cmd(sim, s2, PROGRAM[20:])
    sim.wait_for_event("upload-complete")
    assert sim.program == PROGRAM[:20] + keepalive + PROGRAM[20:]
    assert recv_block_text(s2) == b"INFO;0\n"
    assert_silent(s3)  # no GLOBAL STATUS blob: the parser was off
    assert [(e.conn, len(e.data)) for e in sim.events_of("upload-data")] == [
        (0, 20), (2, len(keepalive)), (1, len(PROGRAM) - 20),
    ]


def test_upload_header_bytes_after_the_size_are_not_program_data(make_sim, socks):
    sim = make_sim(connect_replay=False, auto_start_work=False)
    s1, s2, s3 = vendor_connect(sim, socks)
    # One buffer = one command: the handler reads the size and nothing else.
    send_cmd(sim, s1, b"GCODE UPLOAD;10\r\n0123456789")
    assert [e.data for e in sim.events_of("upload-header-extra-ignored")] == [b"\r\n0123456789"]
    assert not sim.events_of("upload-complete")
    send_cmd(sim, s1, b"ABCDEFGHIJ")
    sim.wait_for_event("upload-complete")
    assert sim.program == b"ABCDEFGHIJ"


def test_abort_work_is_unknown_on_0102_and_the_job_keeps_running(make_sim, socks):
    sim = make_sim(connect_replay=False, work_line_delay=0.03)
    s1, s2, s3 = vendor_connect(sim, socks)
    program = b"".join(b"G01 X%d Y0\r\n" % i for i in range(15)) + b"\r\n"
    send_cmd(sim, s1, b"GCODE UPLOAD;%d" % len(program))
    send_cmd(sim, s1, program)
    sim.wait_for_event("work-start")
    send_cmd(sim, s1, "ABORT WORK")
    assert [e.data for e in sim.events_of("unknown-command")] == [b"ABORT WORK"]
    sim.wait_for_event("work-end", timeout=5.0)
    assert "completed" in sim.events_of("work-end")[0].note
    assert sim.wait_for(
        lambda: any(e.kind == "sent" and e.data == b"INFO;23\n" for e in sim.events)
    )


def test_position_frames_to_an_empty_slot2_are_dropped(make_sim, socks):
    sim = make_sim(connect_replay=False)
    s1 = connect(sim, socks)
    connect(sim, socks)  # S2 only: no third connection, so slot 2 is empty
    send_cmd(sim, s1, b"GCODE UPLOAD;%d" % len(PROGRAM))
    send_cmd(sim, s1, PROGRAM)
    sim.wait_for_event("work-end", timeout=5.0)
    dropped = [e for e in sim.events_of("dropped") if e.slot == 2]
    assert len(dropped) == 4 and all(e.data[:1] == b"{" for e in dropped)


# --------------------------------------------------------------------------- #
# vendor-app-newer profile
# --------------------------------------------------------------------------- #

def test_vendor_newer_identity_status_and_extra_commands(make_sim, socks):
    sim = make_sim(profile="vendor-app-newer", connect_replay=False, tower_firmware=(1, 10, 3))
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "INFO")
    m = IDENTITY_RE.fullmatch(recv_block_text(s2))
    assert m and m.group(1) == b"01.02.03"
    send_cmd(sim, s1, "GET GLOBAL SYS STATUS")
    frame = recv_exact(s1, 19)
    assert frame[:1] == b"G" and frame[18:] == b"S"
    assert (frame[14], frame[13], frame[12]) == (1, 10, 3)
    assert recv_block_text(s2) == b"INFO;0\n"
    send_cmd(sim, s1, "SEND TCP POS")
    assert len(_position_frames(s3, 1)) == 1
    send_cmd(sim, s1, "GET Z BACKLASH")
    assert recv_block_text(s1).startswith(b"Z const offset: ")
    send_cmd(sim, s1, "GCODE CONTINUE;0")
    assert not sim.events_of("unknown-command")


def test_vendor_newer_upload_acks_and_abort(make_sim, socks):
    sim = make_sim(profile="vendor-app-newer", connect_replay=False, work_line_delay=0.03)
    s1, s2, s3 = vendor_connect(sim, socks)
    program = b"".join(b"G01 X%d Y0\r\n" % i for i in range(30)) + b"\r\n"
    send_cmd(sim, s1, b"GCODE UPLOAD;%d" % len(program))
    assert recv_block_text(s2) == b"INFO;7\n"
    send_cmd(sim, s1, program)
    assert [recv_block_text(s2) for _ in range(5)] == [
        b"INFO;0\n", b"INFO;8\n", b"INFO;9\n", b"INFO;15\n", b"INFO;17\n",
    ]
    send_cmd(sim, s1, "ABORT WORK")
    sim.wait_for_event("work-end", timeout=5.0)
    assert "aborted" in sim.events_of("work-end")[0].note
    texts = [recv_block_text(s2) for _ in range(3)]
    assert texts == [b"INFO;0\n", b"INFO;18\n", b"INFO;16\n"]
    assert_silent(s2, 0.2)  # no INFO;23 after an abort


def test_vendor_newer_full_position_frame_option(make_sim, socks):
    sim = make_sim(profile="vendor-app-newer", connect_replay=False,
                   position_frame="full", temperature=125)
    s1, s2, s3 = vendor_connect(sim, socks)
    send_cmd(sim, s1, "SEND TCP POS")
    block = recv_exact(s3, BLOCK)
    assert block[0] == 0x7B and block[57] == 0x7D
    assert block[21] == 0x7D  # temperature LSB 125 == '}' : the FRAME-6 ambiguity
    assert block[58:] == b"\x00" * (BLOCK - 58)


# --------------------------------------------------------------------------- #
# real-world hazards
# --------------------------------------------------------------------------- #

def test_pre_occupied_slots_send_replies_to_the_other_client(make_sim, socks):
    sim = make_sim(occupied_slots=(0, 1))
    s1, s2, s3 = vendor_connect(sim, socks)
    assert sim.slot_holders() == (OTHER_CLIENT, OTHER_CLIENT, 0, 1, 2, None)
    send_cmd(sim, s1, "INFO")
    send_cmd(sim, s1, "READ INDEX CALIBRATION")
    for s in (s1, s2, s3):
        assert_silent(s, 0.1)
    other = sim.events_of("to-other-client")
    assert [(e.slot, e.data[:7]) for e in other] == [
        (1, b"INFO;34"), (0, b"cfg rea"), (1, b"INFO;0\n"),
    ]
    # A direct reply still reaches the requester.
    send_cmd(sim, s1, "GET GLOBAL SYS STATUS")
    assert recv_exact(s1, 48)[:19] == HEAD


def test_releasing_an_occupied_slot_lets_the_next_connection_take_it(make_sim, socks):
    sim = make_sim(occupied_slots=(1,), connect_replay=False)
    connect(sim, socks)
    sim.release_slot(1)
    connect(sim, socks)
    assert sim.slot_holders()[:2] == (0, 1)


def test_robot_drops_a_connection_on_accept(make_sim, socks):
    sim = make_sim(drop_on_accept={1}, connect_replay=False)
    s1 = connect(sim, socks)
    s2 = connect(sim, socks)
    assert peer_closed(s2)
    sim.wait_for_event("robot-dropped", conn=1)
    assert sim.slot_holders()[:2] == (0, None)
    send_cmd(sim, s1, "INFO")
    assert [e.slot for e in sim.events_of("dropped")] == [1]


def test_robot_drops_a_connection_mid_session(make_sim, socks):
    sim = make_sim(connect_replay=False)
    s1, s2, s3 = vendor_connect(sim, socks)
    sim.drop(1)
    assert peer_closed(s2)
    assert sim.slot_holders()[:3] == (0, None, 2)
    send_cmd(sim, s1, "INFO")
    assert sim.events_of("dropped")[-1].slot == 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def test_cli_help_lists_profiles(capsys):
    with pytest.raises(SystemExit) as ei:
        simmod.main(["--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    for name in PROFILES:
        assert name in out


def test_cli_rejects_an_unknown_profile(capsys):
    with pytest.raises(SystemExit) as ei:
        simmod.main(["--profile", "gfw-9.9.9"])
    assert ei.value.code == 2


def test_cli_serves_the_gfw_profile_over_tcp(socks):
    src = str(Path(simmod.__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "goliath_link.sim", "--profile", "gfw-01.02.00",
         "--port", "0", "--duration", "20", "--no-connect-replay"],
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
        assert any("gfw-01.02.00" in ln for ln in lines)
        conns = []
        for _ in range(3):
            c = socket.create_connection((HOST, port), timeout=3.0)
            socks.append(c)
            conns.append(c)
            time.sleep(0.2)
        conns[0].sendall(b"INFO")
        assert IDENTITY_RE.fullmatch(recv_block_text(conns[1]))
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        proc.stdout.close()
