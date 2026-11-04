# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""Turn the robot's raw TCP byte stream into discrete messages.

The robot interleaves, on one socket, **newline-delimited text** (``INFO;...``,
``ERR;...``, free-form replies) with **binary telemetry frames** that are *not*
newline-terminated and are recognised by sentinel bytes and fixed lengths
(see ``docs/protocol/01-transport-and-framing.md``):

* position ``{`` ... ``}``  — 22 or 58 bytes
* tower    ``<`` ... ``>``  — 4 bytes
* global   ``G`` ... ``S``  — 19 bytes

The vendor app leaned on read-boundary timing to separate these, which is
fragile. This framer instead uses the documented sentinels and lengths, and
buffers across partial reads, so a message split across two TCP segments is
reassembled correctly. Bytes are consumed only when a complete message is
recognised.

Design note on the ``G``/``S`` frame: ``G`` also begins ordinary ASCII, so a
19-byte candidate is only accepted as a binary global-status frame when byte 18
is ``S`` (0x53); otherwise the leading ``G`` is treated as text. This matches the
observed data (the robot's text replies never start with ``{``/``<`` and its
``G...S`` frames are always exactly 19 bytes).
"""

from __future__ import annotations

from .protocol import (
    GLOBAL_END,
    GLOBAL_LEN,
    GLOBAL_START,
    POS_END,
    POS_LEN_FULL,
    POS_LEN_SHORT,
    POS_START,
    TWR_END,
    TWR_LEN,
    TWR_START,
    GlobalStatusFrame,
    PositionFrame,
    TextMessage,
    TowerFrame,
    parse_text_message,
)

Event = object  # TextMessage | PositionFrame | TowerFrame | GlobalStatusFrame

_NEED_MORE = object()  # sentinel: not enough bytes yet to decide


class StreamFramer:
    """Incremental framer. Feed bytes; get back whole messages.

    Not thread-safe; call :meth:`feed` from a single reader.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Event]:
        """Append ``data`` and return every complete message now available."""
        self._buf.extend(data)
        out: list[Event] = []
        while self._buf:
            event = self._try_one()
            if event is _NEED_MORE:
                break
            if event is not None:
                out.append(event)
        return out

    # -- internals ----------------------------------------------------------

    def _try_one(self) -> Event | object | None:
        """Consume and return one message, or _NEED_MORE, or None.

        Every ``None`` return must make progress (consume >=1 byte) so the feed
        loop cannot spin; ``_NEED_MORE`` means no progress is possible until more
        bytes arrive.
        """
        b0 = self._buf[0]

        if b0 == POS_START:
            return self._try_position()
        if b0 == TWR_START:
            return self._try_fixed_or_resync(TWR_LEN, TWR_END, TowerFrame.decode)
        if b0 == GLOBAL_START:
            # A complete, valid 19-byte 'G...S' frame wins.
            if len(self._buf) >= GLOBAL_LEN and self._buf[GLOBAL_LEN - 1] == GLOBAL_END:
                frame = bytes(self._buf[:GLOBAL_LEN])
                del self._buf[:GLOBAL_LEN]
                return GlobalStatusFrame.decode(frame)
            # Still ambiguous and could become either a frame or a text line:
            # wait only while we have neither 19 bytes nor a newline.
            if len(self._buf) < GLOBAL_LEN and b"\n" not in self._buf:
                return _NEED_MORE
            # Has a newline (so it's a text line), or 19+ bytes that don't close
            # as a frame: treat the leading 'G' as text.
            return self._try_text()
        return self._try_text()

    def _try_position(self) -> Event | object | None:
        # Short (22) and full (58) both start '{' end '}'. Disambiguate by the
        # byte at offset 21: for a 22-byte frame it is the closing '}'.
        if len(self._buf) >= POS_LEN_SHORT and self._buf[POS_LEN_SHORT - 1] == POS_END:
            frame = bytes(self._buf[:POS_LEN_SHORT])
            del self._buf[:POS_LEN_SHORT]
            return PositionFrame.decode(frame)
        if len(self._buf) >= POS_LEN_FULL:
            if self._buf[POS_LEN_FULL - 1] == POS_END:
                frame = bytes(self._buf[:POS_LEN_FULL])
                del self._buf[:POS_LEN_FULL]
                return PositionFrame.decode(frame)
            # Looked like a position frame but neither length closed properly;
            # drop the stray '{' and resync.
            del self._buf[:1]
            return None
        return _NEED_MORE

    def _try_fixed_or_resync(self, length: int, end_byte: int, decode) -> Event | object | None:
        if len(self._buf) < length:
            return _NEED_MORE
        if self._buf[length - 1] == end_byte:
            frame = bytes(self._buf[:length])
            del self._buf[:length]
            return decode(frame)
        # Looked like this frame type but did not close correctly; drop the
        # leading sentinel byte to resync (guarantees progress).
        del self._buf[:1]
        return None

    def _try_text(self) -> Event | object | None:
        nl = self._buf.find(b"\n")
        if nl < 0:
            # No complete line yet. Guard against unbounded growth from a
            # never-terminated binary blob we failed to recognise.
            if len(self._buf) > _MAX_LINE:
                # Drop one byte to resync rather than buffer forever.
                del self._buf[:1]
                return None
            return _NEED_MORE
        line = bytes(self._buf[: nl + 1])
        del self._buf[: nl + 1]
        text = line.decode("ascii", errors="replace").replace("\x00", "")
        if not text.strip():
            return None  # blank line, ignore
        return parse_text_message(text)


_MAX_LINE = 4096
