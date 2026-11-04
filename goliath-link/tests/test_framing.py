# SPDX-License-Identifier: Apache-2.0
import struct

from goliath_link.framing import StreamFramer
from goliath_link.protocol import GlobalStatusFrame, PositionFrame, TextMessage, TowerFrame


def pos_short(line=1):
    buf = bytearray(22)
    buf[0] = 0x7B
    buf[21] = 0x7D
    struct.pack_into("<ffff", buf, 1, 1.0, 2.0, 3.0, 0.0)
    struct.pack_into("<i", buf, 17, line)
    return bytes(buf)


def pos_full(line=1):
    buf = bytearray(58)
    buf[0] = 0x7B
    buf[57] = 0x7D
    struct.pack_into("<ffff", buf, 1, 1.0, 2.0, 3.0, 0.0)
    struct.pack_into("<i", buf, 17, line)
    struct.pack_into("<i", buf, 21, 40)
    buf[25] = 0x60
    buf[26] = 0x60
    return bytes(buf)


def tower():
    return bytes([0x3C, 0xAA, 0x60, 0x3E])


def glob():
    buf = bytearray(19)
    buf[0] = 0x47
    buf[18] = 0x53
    return bytes(buf)


def test_single_text_line():
    out = StreamFramer().feed(b"INFO;8\n")
    assert len(out) == 1
    assert isinstance(out[0], TextMessage) and out[0].code == 8


def test_multiple_text_lines_in_one_feed():
    out = StreamFramer().feed(b"INFO;7\nINFO;8\n")
    assert [m.code for m in out] == [7, 8]


def test_text_split_across_feeds():
    f = StreamFramer()
    assert f.feed(b"INFO;2") == []  # no newline yet
    out = f.feed(b"7;5\n")
    assert len(out) == 1 and out[0].code == 27


def test_position_short_and_full():
    out = StreamFramer().feed(pos_short(5))
    assert isinstance(out[0], PositionFrame) and out[0].gcode_line == 5
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
    f = StreamFramer()
    stream = b"INFO;5\n" + pos_full(1) + b"INFO;6\n" + tower() + pos_short(2)
    out = f.feed(stream)
    kinds = [type(e).__name__ for e in out]
    assert kinds == [
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


def test_byte_at_a_time_feed_reassembles_everything():
    f = StreamFramer()
    stream = b"INFO;8\n" + pos_full(7) + tower()
    out = []
    for i in range(len(stream)):
        out.extend(f.feed(stream[i : i + 1]))
    assert [type(e).__name__ for e in out] == [
        "TextMessage",
        "PositionFrame",
        "TowerFrame",
    ]
