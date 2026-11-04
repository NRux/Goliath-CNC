# SPDX-License-Identifier: Apache-2.0
"""StreamFramer tests.

The wire shapes used here come from the GFW 01.02.00 firmware facts (see the
module docstring of ``goliath_link.framing``): text and position frames are
sent as one message zero-padded to 512 bytes, tower frames are 4 unpadded
bytes, ``GET GLOBAL SYS STATUS`` answers with a 48-byte HEAD/TALE blob, and some
free-form replies have no terminator at all.

Times are passed explicitly (``now=``) so the quiet-interval flush is tested
deterministically, without sleeping.
"""
import struct

import pytest

from goliath_link.framing import DEFAULT_QUIET_INTERVAL, Discarded, StreamFramer
from goliath_link.protocol import (
    GlobalStatusBlob,
    GlobalStatusFrame,
    PositionFrame,
    TextMessage,
    TowerFrame,
)

HEAD = b"GLOBAL STATUS HEAD\x00"
TALE = b"GLOBAL STATUS TALE\x00"
QUIET = DEFAULT_QUIET_INTERVAL + 0.01


def pad512(b: bytes) -> bytes:
    assert len(b) <= 512
    return b + b"\x00" * (512 - len(b))


def pos_short(line=1, x=1.0, y=2.0, z=3.0, angle=0.0):
    buf = bytearray(22)
    buf[0] = 0x7B
    buf[21] = 0x7D
    struct.pack_into("<ffff", buf, 1, x, y, z, angle)
    struct.pack_into("<i", buf, 17, line)
    return bytes(buf)


def pos_full(line=1, temp=40):
    buf = bytearray(58)
    buf[0] = 0x7B
    buf[57] = 0x7D
    struct.pack_into("<ffff", buf, 1, 1.0, 2.0, 3.0, 0.0)
    struct.pack_into("<i", buf, 17, line)
    struct.pack_into("<i", buf, 21, temp)
    buf[25] = 0x60
    buf[26] = 0x60
    return bytes(buf)


def tower(tid=0xAA, sb=0x60):
    return bytes([0x3C, tid, sb, 0x3E])


def glob():
    buf = bytearray(19)
    buf[0] = 0x47
    buf[18] = 0x53
    return bytes(buf)


def blob(fields=(1, 0, 1, 2, 1, 0, 0, 1, 0, 1)):
    return HEAD + bytes(fields) + TALE


def feed_all(data: bytes, *, chunks=None) -> list:
    """Feed ``data`` (optionally in the given chunks), then let it go quiet."""
    f = StreamFramer()
    out = []
    t = 100.0
    for piece in chunks if chunks is not None else [data]:
        out.extend(f.feed(piece, now=t))
        t += 0.001
    out.extend(f.poll(now=t + QUIET))
    return out


def kinds(events):
    return [type(e).__name__ for e in events]


# --------------------------------------------------------------------------- #
# Behaviour kept from the first framer
# --------------------------------------------------------------------------- #


def test_single_text_line():
    out = StreamFramer().feed(b"INFO;8\n")
    assert len(out) == 1
    assert isinstance(out[0], TextMessage) and out[0].code == 8
    assert out[0].terminated is True


def test_multiple_text_lines_in_one_feed():
    out = StreamFramer().feed(b"INFO;7\nINFO;8\n")
    assert [m.code for m in out] == [7, 8]


def test_text_split_across_feeds():
    f = StreamFramer()
    assert f.feed(b"INFO;2") == []  # no terminator yet, and not quiet yet
    out = f.feed(b"7;5\n")
    assert len(out) == 1 and out[0].code == 27


def test_position_short_and_full():
    # A bare 22-byte frame cannot be told from the start of a 58-byte frame
    # whose temperature byte is '}' until byte 22+ arrives (FRAME-6), so an
    # unpadded short frame is emitted once the stream goes quiet. On the wire
    # the firmware pads it to 512 bytes, which settles it at once.
    f = StreamFramer()
    assert f.feed(pos_short(5), now=1.0) == []
    out = f.poll(now=1.0 + QUIET)
    assert isinstance(out[0], PositionFrame) and out[0].gcode_line == 5
    out = StreamFramer().feed(pad512(pos_short(6)))
    assert isinstance(out[0], PositionFrame) and out[0].gcode_line == 6
    out = StreamFramer().feed(pos_full(9))
    assert isinstance(out[0], PositionFrame) and out[0].temperature == 40


def test_position_split_across_feeds():
    f = StreamFramer()
    frame = pos_full(3)
    assert f.feed(frame[:30]) == []
    out = f.feed(frame[30:])
    assert len(out) == 1 and isinstance(out[0], PositionFrame)


def test_tower_and_global_frames():
    out = StreamFramer().feed(tower())
    assert isinstance(out[0], TowerFrame) and out[0].tower == 1
    out = StreamFramer().feed(glob())
    assert isinstance(out[0], GlobalStatusFrame)


def test_interleaved_text_and_binary():
    stream = b"INFO;5\n" + pos_full(1) + b"INFO;6\n" + tower() + pos_short(2)
    out = feed_all(stream)
    assert kinds(out) == [
        "TextMessage",
        "PositionFrame",
        "TextMessage",
        "TowerFrame",
        "PositionFrame",
    ]
    assert out[0].code == 5 and out[2].code == 6


def test_global_g_is_treated_as_text_when_not_a_valid_frame():
    # A real text line starting with 'G' that is not a 19-byte G...S frame.
    out = StreamFramer().feed(b"GCODE done\n")
    assert isinstance(out[0], TextMessage) and out[0].kind == "TEXT"
    assert out[0].raw == "GCODE done"


def test_byte_at_a_time_feed_reassembles_everything():
    stream = b"INFO;8\n" + pos_full(7) + tower()
    out = feed_all(stream, chunks=[stream[i : i + 1] for i in range(len(stream))])
    assert kinds(out) == ["TextMessage", "PositionFrame", "TowerFrame"]


def test_crlf_and_firmware_newline_cr_variants():
    out = feed_all(b"INFO;8\r\n" + b"xmax=1200.0000 \n\rymax=800.0000")
    assert [(m.kind, m.raw, m.terminated) for m in out] == [
        ("INFO", "INFO;8", True),
        ("TEXT", "xmax=1200.0000", True),
        ("TEXT", "ymax=800.0000", False),  # exact-length reply: no terminator
    ]


# --------------------------------------------------------------------------- #
# FRAME-2: NUL padding between messages
# --------------------------------------------------------------------------- #


def test_frame2_padded_text_then_tower_then_padded_text():
    f = StreamFramer()
    out = f.feed(pad512(b"CAGE STATUS: CLOSED\n"))
    out += f.feed(tower())
    out += f.feed(pad512(b"CAGE STATUS: OPEN\n"))
    assert kinds(out) == ["TextMessage", "TowerFrame", "TextMessage"]
    assert out[0].raw == "CAGE STATUS: CLOSED"
    assert out[1].tower == 1
    assert out[2].raw == "CAGE STATUS: OPEN"


def test_frame2_padded_info_tower_err_in_one_feed():
    out = StreamFramer().feed(pad512(b"INFO;17\n") + tower() + pad512(b"ERR;2\n"))
    assert kinds(out) == ["TextMessage", "TowerFrame", "TextMessage"]
    assert (out[0].kind, out[0].code) == ("INFO", 17)
    assert (out[2].kind, out[2].code) == ("ERR", 2)


def test_frame2_padded_position_stream_every_frame_promptly():
    # 40 position frames, each zero-padded to 512 bytes, one recv per frame.
    # Some floats contain 0x0A; every frame must come out of its own chunk.
    f = StreamFramer()
    for i in range(40):
        frame = PositionFrame(10.0 + i, 0.00842 if i % 3 else 20.0, 3.0, 0.0, i).encode()
        out = f.feed(pad512(frame))
        assert [type(e).__name__ for e in out] == ["PositionFrame"], (i, out)
        assert out[0].gcode_line == i
    assert f.pending == b""


def test_frame2_tower_status_byte_zero_is_not_padding():
    # NULs are skipped only BETWEEN messages: a 0x00 status byte stays in the frame.
    out = StreamFramer().feed(pad512(b"INFO;1\n") + tower(0xBB, 0x00) + pad512(b"INFO;2\n"))
    assert kinds(out) == ["TextMessage", "TowerFrame", "TextMessage"]
    assert out[1].tower == 2 and out[1].status == 0 and out[1].battery == 0.0


# --------------------------------------------------------------------------- #
# FRAME-3: unterminated replies
# --------------------------------------------------------------------------- #


def test_frame3_padded_unterminated_reply_ends_at_first_nul():
    f = StreamFramer()
    out = f.feed(pad512(b"cfg read index : 28500"))
    assert len(out) == 1
    assert out[0].kind == "TEXT" and out[0].raw == "cfg read index : 28500"
    assert out[0].terminated is True  # the NUL padding ended it
    out = f.feed(pad512(b"CAGE STATUS: CLOSED\n"))
    assert [m.raw for m in out] == ["CAGE STATUS: CLOSED"]


def test_frame3_exact_length_reply_flushed_after_quiet_interval():
    f = StreamFramer(quiet_interval=0.3)
    assert f.feed(b"count: 5", now=10.0) == []
    assert f.poll(now=10.2) == []  # not quiet long enough yet
    out = f.poll(now=10.31)
    assert len(out) == 1
    assert out[0].raw == "count: 5" and out[0].terminated is False
    # The next message is not glued to it.
    out = f.feed(pad512(b"CAGE STATUS: CLOSED\n"), now=11.0)
    assert [m.raw for m in out] == ["CAGE STATUS: CLOSED"]


def test_frame3_quiet_interval_is_measured_from_the_last_byte():
    f = StreamFramer(quiet_interval=0.3)
    assert f.feed(b"Moving ", now=10.0) == []
    assert f.feed(b"stepper!", now=10.25) == []
    assert f.poll(now=10.4) == []
    out = f.poll(now=10.56)
    assert [(m.raw, m.terminated) for m in out] == [("Moving stepper!", False)]


def test_frame3_unterminated_reply_cut_by_following_tower_frame():
    out = feed_all(b"count: 5" + tower(0xBB, 0x41) + pad512(b"CAGE STATUS: OPEN\n"))
    assert kinds(out) == ["TextMessage", "TowerFrame", "TextMessage"]
    assert (out[0].raw, out[0].terminated) == ("count: 5", False)
    assert out[1].tower == 2
    assert (out[2].raw, out[2].terminated) == ("CAGE STATUS: OPEN", True)


def test_frame3_unterminated_reply_cut_by_following_status_blob():
    out = feed_all(b"Moving stepper!" + blob())
    assert kinds(out) == ["TextMessage", "GlobalStatusBlob"]
    assert (out[0].raw, out[0].terminated) == ("Moving stepper!", False)


def test_partial_frame_survives_a_quiet_gap_shorter_than_stall_timeout():
    # A TCP segment held back by Wi-Fi retransmission must not cost the frame:
    # a binary frame's length is known, so the framer keeps waiting.
    frame = pad512(PositionFrame(5.0, 6.0, 7.0, 0.0, 42).encode())
    f = StreamFramer(quiet_interval=0.3, stall_timeout=5.0)
    assert f.feed(frame[:10], now=0.0) == []
    assert f.poll(now=1.0) == []
    out = f.feed(frame[10:], now=1.5)
    assert kinds(out) == ["PositionFrame"] and out[0].gcode_line == 42


def test_partial_frame_is_discarded_after_stall_timeout():
    f = StreamFramer(quiet_interval=0.3, stall_timeout=5.0)
    f.feed(bytes([0x3C, 0xAA]), now=0.0)
    assert f.poll(now=4.9) == []
    out = f.poll(now=5.0)
    assert kinds(out) == ["Discarded"] and out[0].raw.startswith(b"<")
    assert f.pending == b""


def test_info_err_warn_fragment_waits_for_its_newline():
    # Every INFO/ERR/WARN format in the firmware ends in '\n', so a fragment is
    # an incomplete line (a delayed segment), not an exact-length reply: it
    # must not come out early as a wrong 'INFO;3'.
    f = StreamFramer(quiet_interval=0.3, stall_timeout=5.0)
    assert f.feed(b"INFO;3", now=0.0) == []
    assert f.poll(now=1.0) == []
    out = f.feed(b"4;GFW 01.02.00   ;1;2;0001\n", now=1.2)
    assert [(m.code, m.terminated) for m in out] == [(34, True)]


def test_info_fragment_is_flushed_unterminated_after_stall_timeout():
    f = StreamFramer(quiet_interval=0.3, stall_timeout=5.0)
    f.feed(b"ERR;1", now=0.0)
    assert f.poll(now=1.0) == []
    out = f.poll(now=5.5)
    assert [(m.kind, m.code, m.terminated) for m in out] == [("ERR", 1, False)]


def test_quiet_cuts_free_text_before_a_partial_tower_frame():
    f = StreamFramer(quiet_interval=0.3, stall_timeout=5.0)
    assert f.feed(b"count: 5<\xaa", now=0.0) == []
    out = f.poll(now=0.4)
    assert [(m.raw, m.terminated) for m in out] == [("count: 5", False)]
    assert f.pending == b"<\xaa"
    out = f.feed(b"\x60>", now=0.6)
    assert kinds(out) == ["TowerFrame"] and out[0].tower == 1


def test_stall_timeout_must_not_be_shorter_than_quiet_interval():
    with pytest.raises(ValueError):
        StreamFramer(quiet_interval=1.0, stall_timeout=0.5)


def test_flush_emits_pending_unterminated_text():
    f = StreamFramer()
    assert f.feed(b"spindle distance: 0.00") == []
    out = f.flush()
    assert [(m.raw, m.terminated) for m in out] == [("spindle distance: 0.00", False)]
    assert f.pending == b""


# --------------------------------------------------------------------------- #
# FRAME-4: 48-byte GLOBAL STATUS HEAD/TALE blob
# --------------------------------------------------------------------------- #


def test_frame4_status_blob_decoded_with_zero_fields_intact():
    fields = (1, 0, 1, 2, 1, 0, 0, 1, 0, 10)  # zeros and a 0x0A must survive
    f = StreamFramer()
    out = f.feed(blob(fields))
    assert kinds(out) == ["GlobalStatusBlob"]
    assert out[0].fields == fields
    assert out[0].raw == blob(fields)
    out = f.feed(tower())
    out += f.feed(pad512(b"CAGE STATUS: CLOSED\n"))
    assert kinds(out) == ["TowerFrame", "TextMessage"]
    assert out[1].raw == "CAGE STATUS: CLOSED"


def test_frame4_status_blob_split_inside_head_and_fields():
    data = blob()
    for cut in range(1, len(data)):
        f = StreamFramer()
        out = f.feed(data[:cut]) + f.feed(data[cut:])
        assert kinds(out) == ["GlobalStatusBlob"], cut


def test_frame4_head_without_tale_is_discarded_visibly():
    out = feed_all(HEAD + bytes(10) + b"GLOBAL STATUS XXXX\x00" + bytes(80) + b"INFO;1\n")
    assert any(isinstance(e, Discarded) and e.raw.startswith(HEAD) for e in out), out
    assert isinstance(out[-1], TextMessage) and out[-1].code == 1


# --------------------------------------------------------------------------- #
# FRAME-5: unknown tower id must not raise out of feed()
# --------------------------------------------------------------------------- #


def test_frame5_unknown_tower_id_is_a_discarded_event_not_an_exception():
    out = StreamFramer().feed(b"INFO;17\n" + bytes([0x3C, 0x34, 0x12, 0x3E]) + b"ERR;2\n")
    assert kinds(out) == ["TextMessage", "Discarded", "TextMessage"]
    assert out[0].code == 17
    assert out[1].raw == bytes([0x3C, 0x34, 0x12, 0x3E])
    assert out[1].reason == "tower frame with unknown id byte 0x34"
    assert (out[2].kind, out[2].code) == ("ERR", 2)


# --------------------------------------------------------------------------- #
# FRAME-6: 22- vs 58-byte position frames
# --------------------------------------------------------------------------- #


def test_frame6_full_frame_with_byte21_close_brace_is_not_cut_short():
    full = PositionFrame(1.0, 2.0, 3.0, 0.0, 7, temperature=125, t1_status=0,
                         t1_battery=50.0, t2_status=0, t2_battery=50.0).encode()
    assert full[21] == 0x7D
    out = feed_all(full + b"ERR;2\n")
    assert kinds(out) == ["PositionFrame", "TextMessage"]
    assert out[0].temperature == 125 and out[0].t1_battery == 50.0
    assert (out[1].kind, out[1].code) == ("ERR", 2)


def test_frame6_padded_short_frame_is_short():
    out = StreamFramer().feed(pad512(pos_short(3)))
    assert kinds(out) == ["PositionFrame"] and out[0].temperature is None


def test_frame6_short_frames_back_to_back_and_followed_by_text():
    # Unpadded short frames where byte 57 happens to be '}' (inside the third
    # frame) must still frame as 22-byte frames.
    third = bytearray(pos_short(3))
    third[13] = 0x7D  # angle LSB -> stream offset 44 + 13 = 57
    stream = pos_short(1) + pos_short(2) + bytes(third) + b"INFO;23\n"
    assert stream[21] == 0x7D and stream[57] == 0x7D
    out = feed_all(stream)
    assert kinds(out) == ["PositionFrame"] * 3 + ["TextMessage"]
    assert [p.gcode_line for p in out[:3]] == [1, 2, 3]


def test_frame6_locked_lengths():
    f = StreamFramer(position_lengths=(22,))
    out = f.feed(pos_short(4))  # no wait: 58-byte frames are ruled out
    assert kinds(out) == ["PositionFrame"]
    with pytest.raises(ValueError):
        StreamFramer(position_lengths=(23,))


# --------------------------------------------------------------------------- #
# FRAME-7: 19-byte G..S frame containing 0x0A, split across recv()
# --------------------------------------------------------------------------- #


def test_frame7_g_s_frame_with_newline_is_split_invariant():
    g = GlobalStatusFrame.build((1, 10, 0), (1, 0, 10)).encode()
    assert b"\n" in g
    for cut in range(1, len(g)):
        f = StreamFramer()
        out = f.feed(g[:cut]) + f.feed(g[cut:])
        assert kinds(out) == ["GlobalStatusFrame"], cut
        assert out[0].t1_firmware == (1, 10, 0) and out[0].t2_firmware == (1, 0, 10)


# --------------------------------------------------------------------------- #
# FRAME-8: every resync/discard is visible
# --------------------------------------------------------------------------- #


def test_frame8_stray_open_brace_is_reported():
    out = feed_all(b"{" + b"INFO;1\n" + bytes(60))
    assert isinstance(out[0], Discarded) and out[0].raw == b"{"
    assert "position" in out[0].reason
    assert any(isinstance(e, TextMessage) and e.code == 1 for e in out)


def test_frame8_unclosed_tower_sentinel_is_reported():
    out = feed_all(b"<ab" + b"cd\n")
    assert isinstance(out[0], Discarded) and out[0].raw == b"<"
    assert isinstance(out[1], TextMessage) and out[1].raw == "abcd"


def test_frame8_overlong_text_is_reported_not_dropped_byte_by_byte():
    junk = b"x" * 5000
    f = StreamFramer()
    out = f.feed(junk + b"\nINFO;1\n")
    discarded = [e for e in out if isinstance(e, Discarded)]
    assert discarded and sum(len(d.raw) for d in discarded) >= 4096
    assert isinstance(out[-1], TextMessage) and out[-1].code == 1


def test_frame8_consecutive_drops_are_coalesced():
    out = feed_all(b"<<<" + b"INFO;1\n")
    drops = [e for e in out if isinstance(e, Discarded)]
    assert len(drops) == 1 and drops[0].raw == b"<<<"
    assert isinstance(out[-1], TextMessage) and out[-1].code == 1


def test_non_text_byte_at_message_start_is_discarded():
    out = feed_all(b"\xff\x01" + b"INFO;1\n")
    assert kinds(out) == ["Discarded", "TextMessage"]
    assert out[0].raw == b"\xff\x01" and out[1].code == 1


def test_discarded_counter_and_padding_counter():
    f = StreamFramer()
    f.feed(pad512(b"INFO;1\n") + bytes([0x3C, 0x01, 0x02, 0x3E]))
    assert f.padding_bytes == 512 - len(b"INFO;1\n")
    assert f.discarded_bytes == 4


def test_feed_never_raises_on_random_garbage():
    import random

    rng = random.Random(1234)
    alphabet = [0x00, 0x0A, 0x0D, 0x3C, 0x3E, 0x7B, 0x7D, 0x47, 0x53, 0xAA, 0xBB, 0x41, 0x3B]
    for _ in range(300):
        data = bytes(rng.choice(alphabet) if rng.random() < 0.7 else rng.randrange(256)
                     for _ in range(rng.randrange(1, 400)))
        f = StreamFramer()
        pos = 0
        while pos < len(data):
            step = rng.randrange(1, 64)
            f.feed(data[pos : pos + step], now=float(pos))
            pos += step
        f.flush()
        assert f.pending == b""


def test_quiet_interval_must_be_positive():
    with pytest.raises(ValueError):
        StreamFramer(quiet_interval=0)
