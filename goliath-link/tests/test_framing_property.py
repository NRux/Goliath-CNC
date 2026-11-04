# SPDX-License-Identifier: Apache-2.0
"""Split-invariance property of StreamFramer.

TCP may split or coalesce the robot's sends anywhere. For a fixed byte stream,
the events the framer produces must not depend on where the recv() boundaries
fell. The stream below mixes every wire shape the GFW 01.02.00 firmware is
known to send (NUL-padded INFO/printf lines, unterminated padded replies,
4-byte tower frames, padded 22-byte position frames, the 48-byte status blob,
exact-length unterminated replies), plus the 58-byte and 19-byte G..S forms
documented for other firmware, and ends with an unterminated reply followed by
quiet time.
"""
import random

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


def pad512(b: bytes) -> bytes:
    return b + b"\x00" * (512 - len(b))


# A position whose float bytes contain 0x0A and a '<' 0x34 0x12 '>' run (y bytes
# 00 00 0A 3C, z bytes 34 12 3E 42): the old framer cut it at the 0x0A and then
# raised on "tower id 0x34" (FRAME-2/FRAME-5).
_POS_TRAP = PositionFrame(100.0, 0.0084228515625, 47.51777648925781, 0.0, 2)
_POS_PLAIN = PositionFrame(100.0, 200.0, 5.0, 315.0, 1)
_POS_FULL_7D = PositionFrame(1.0, 2.0, 3.0, 90.0, 7, temperature=125, t1_status=1,
                             t1_battery=50.0, t2_status=2, t2_battery=25.0)
_GS_NL = GlobalStatusFrame.build((1, 10, 0), (1, 0, 10))
_BLOB_FIELDS = (1, 0, 1, 10, 1, 0, 0, 1, 0, 1)

STREAM = b"".join([
    pad512(b"INFO;34;GFW 01.02.00   ;1;2;00010002000A\n"),
    bytes([0x3C, 0xAA, 0x60, 0x3E]),
    pad512(b"CAGE STATUS: CLOSED\n"),
    pad512(_POS_PLAIN.encode()),
    pad512(_POS_TRAP.encode()),
    HEAD + bytes(_BLOB_FIELDS) + TALE,
    bytes([0x3C, 0xBB, 0x0A, 0x3E]),
    pad512(b"cfg read index : 28500"),
    b"count: 5",                      # exact length, no terminator...
    bytes([0x3C, 0xAA, 0x00, 0x3E]),  # ...cut by the next tower frame
    _POS_FULL_7D.encode(),
    b"ERR;2\n",
    _GS_NL.encode(),
    bytes([0x3C, 0x34, 0x12, 0x3E]),  # unknown tower id -> Discarded
    pad512(b"WARN;2\n"),
    b"xmax=1200.0000 \n\rymax=800.0000",  # exact length, no final terminator
])

EXPECTED = [
    TextMessage(kind="INFO", raw="INFO;34;GFW 01.02.00   ;1;2;00010002000A", code=34,
                params=["GFW 01.02.00   ", "1", "2", "00010002000A"]),
    TowerFrame(tower=1, status=1, battery=50.0),
    TextMessage(kind="TEXT", raw="CAGE STATUS: CLOSED"),
    _POS_PLAIN,
    _POS_TRAP,
    GlobalStatusBlob(raw=HEAD + bytes(_BLOB_FIELDS) + TALE, fields=_BLOB_FIELDS),
    TowerFrame(tower=2, status=0, battery=10 * 1.5625),
    TextMessage(kind="TEXT", raw="cfg read index : 28500"),
    TextMessage(kind="TEXT", raw="count: 5", terminated=False),
    TowerFrame(tower=1, status=0, battery=0.0),
    _POS_FULL_7D,
    TextMessage(kind="ERR", raw="ERR;2", code=2),
    _GS_NL,
    Discarded(raw=bytes([0x3C, 0x34, 0x12, 0x3E]), reason="tower frame with unknown id byte 0x34"),
    TextMessage(kind="WARN", raw="WARN;2", code=2),
    TextMessage(kind="TEXT", raw="xmax=1200.0000"),
    TextMessage(kind="TEXT", raw="ymax=800.0000", terminated=False),
]


def run(chunks) -> list:
    f = StreamFramer()
    out = []
    t = 0.0
    for c in chunks:
        out.extend(f.feed(c, now=t))
        out.extend(f.poll(now=t))  # polling while data flows must not change anything
        t += 0.001
    out.extend(f.poll(now=t + DEFAULT_QUIET_INTERVAL + 0.05))
    assert f.pending == b""
    return out


def test_whole_stream_decodes_to_the_expected_events():
    assert _POS_TRAP.encode().count(b"\n") >= 1  # the trap really is a trap
    assert run([STREAM]) == EXPECTED


def test_every_single_split_point_gives_identical_events():
    for cut in range(1, len(STREAM)):
        got = run([STREAM[:cut], STREAM[cut:]])
        assert got == EXPECTED, f"split at {cut}"


def test_random_multi_splits_give_identical_events():
    rng = random.Random(20261104)
    for trial in range(300):
        cuts = sorted(rng.sample(range(1, len(STREAM)), rng.randrange(2, 40)))
        bounds = [0] + cuts + [len(STREAM)]
        chunks = [STREAM[a:b] for a, b in zip(bounds, bounds[1:])]
        assert run(chunks) == EXPECTED, f"trial {trial} cuts {cuts}"


def test_byte_at_a_time_gives_identical_events():
    assert run([STREAM[i : i + 1] for i in range(len(STREAM))]) == EXPECTED
