# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""Turn one of the robot's raw TCP byte streams into discrete messages.

Use one :class:`StreamFramer` per TCP connection: the robot's connections are
independent byte streams (see :mod:`goliath_link.transport`), and a partial
message on one must never swallow bytes from another.

Wire facts this framer is built on
----------------------------------
These come from a disassembly of the robot firmware GFW 01.02.00 and win where
``docs/protocol/01-transport-and-framing.md`` disagrees:

* Text (``INFO;...``, ``ERR;...``, ``WARN;...`` and free-form ``tcp_printf``
  replies) and position frames are sent as **one message zero-padded to 512
  bytes**. TCP may split or coalesce those blocks anywhere.
* Tower frames (``<`` id status ``>``, 4 bytes) are sent unpadded. Their status
  byte may legitimately be 0x00, so NUL bytes are skipped only **between**
  messages, never inside a fixed-length frame.
* Every ``INFO``/``ERR``/``WARN`` format ends in ``\\n``. Some free-form replies
  have no terminator: ``cfg read index : %d`` and ``spindle distance: %.2f``
  are padded (so the first NUL ends them); ``count: %d``, ``Moving stepper!``
  and ``xmax=%.4f \\n\\rymax=%.4f`` are sent with their exact length and
  nothing after them.
* ``GET GLOBAL SYS STATUS`` is answered with a 48-byte blob:
  ``b"GLOBAL STATUS HEAD\\0"`` + 10 status bytes + ``b"GLOBAL STATUS TALE\\0"``.
  No 19-byte ``G...S`` frame builder exists in GFW 01.02.00; that frame is
  still decoded because the spec documents it for other firmware.
* Position frames are 22 bytes (``{`` + 20 bytes + ``}``) on GFW 01.02.00. The
  spec also describes a 58-byte form. A 58-byte frame whose temperature LSB
  (byte 21) is ``}`` looks like a 22-byte frame; see below.

Message grammar (applied at every message boundary)
---------------------------------------------------
1. NUL bytes are padding: skipped (counted in :attr:`StreamFramer.padding_bytes`).
2. ``{`` starts a position frame. If byte 21 is ``}`` the frame is 22 bytes,
   unless byte 22 cannot start a message (NUL or a non-text byte, which is
   what a 58-byte frame's temperature high byte looks like) and byte 57 is
   ``}``: then it is a 58-byte frame. The decision waits until those bytes
   are present (a padded frame always provides them at once) or the stream
   goes quiet. ``position_lengths`` can lock the framer to one length.
3. ``<`` starts a 4-byte tower frame. A frame whose id byte is not 0xAA/0xBB is
   reported as :class:`Discarded`, never raised.
4. ``GLOBAL STATUS HEAD\\0`` starts the 48-byte blob (the TALE is searched for,
   so a different field count is still delivered, with ``len(fields)`` showing
   it).
5. Otherwise a ``G`` followed by 17 bytes and ``S`` is a 19-byte ``G...S``
   frame, unless the bytes after the ``G`` read as a text line (printable
   ASCII up to a CR/LF, or 18 printable bytes). The decision waits for 19
   bytes, so a ``0x0A`` inside the frame cannot split it.
6. Anything else that can start text (printable ASCII, TAB, CR, LF) is text,
   which ends at the first ``\\n`` (kept out of ``raw``) or the first NUL.
   Text is also cut, with ``terminated=False``, right before an embedded
   tower frame (``<`` 0xAA/0xBB x ``>``) or ``GLOBAL STATUS HEAD\\0``, since an
   exact-length reply can be followed directly by one of those. Text with no
   end within :data:`MAX_TEXT_LEN` bytes is discarded.
7. Any other byte at a message boundary is discarded.

Every byte the framer drops (resync after a sentinel that did not close,
unknown tower id, overlong text, a blob without TALE, a partial message after
the stall timeout) is reported as a :class:`Discarded` event carrying the
bytes and a reason. Consecutive drops with the same reason are merged into one
event, which is emitted just before the next message (or at quiet time).

Time-driven resolution: quiet and stall
---------------------------------------
An exact-length reply has no terminator, so the framer cannot know it is
complete until the robot stops sending. :meth:`StreamFramer.poll` (the
transport calls it about every 50 ms) resolves what bytes alone cannot:

* After ``quiet_interval`` (default 0.3 s) without a byte: pending free-form
  text becomes a :class:`TextMessage` with ``terminated=False`` (cut before a
  trailing partial tower frame or blob head, which keep waiting), and a
  complete 22-byte position frame that was waiting for disambiguation is
  emitted. Partial binary frames and ``INFO``/``ERR``/``WARN`` fragments keep
  waiting: their end is certain to come (fixed length, or the ``\\n`` every
  such format has), so a gap means a TCP segment is late, for example a Wi-Fi
  retransmission. Emitting ``INFO;3`` from a late ``INFO;34`` would be wrong.
* After ``stall_timeout`` (default 5 s) without a byte: everything left is
  resolved as at the end of the stream: an ``INFO``/``ERR``/``WARN`` fragment
  becomes a ``TextMessage`` with ``terminated=False`` and partial frames
  become :class:`Discarded`, so nothing sits invisible in the buffer.

Decisions depend only on the bytes, never on where ``recv()`` split them: for
a given byte stream followed by quiet time, every split produces identical
events (tested, including random multi-splits and byte-at-a-time).

Known limits
------------
* An exact-length reply (no terminator) followed on the same connection by
  more text, with no quiet gap of ``quiet_interval`` between them, comes out
  glued to that text: no byte marks the boundary. On GFW 01.02.00 such
  replies share slot 0 only with command replies, so pacing commands at
  least ``quiet_interval`` apart avoids it.
* A ``{`` inside text is not treated as the start of a position frame
  (position frames have their own connection on GFW 01.02.00).
* ``Discarded`` events are emitted when the next message (or quiet time)
  arrives, because consecutive drops are merged first.

Public API
----------
* ``StreamFramer(*, quiet_interval=0.3, stall_timeout=5.0,
  position_lengths=(22, 58), max_text=4096)``
* ``StreamFramer.feed(data, now=None) -> list`` -- add received bytes; return
  every message now complete. ``now`` is a ``time.monotonic()`` value
  (defaults to the current time) recorded as the last-receive time.
* ``StreamFramer.poll(now=None) -> list`` -- the time-driven resolution above;
  ``[]`` while bytes are still arriving.
* ``StreamFramer.flush() -> list`` -- resolve everything buffered now (end of
  stream).
* ``StreamFramer.pending`` -- bytes buffered but not yet resolved;
  ``StreamFramer.last_receive``.
* ``StreamFramer.padding_bytes`` / ``StreamFramer.discarded_bytes`` -- counters.
* :class:`Discarded` -- ``raw: bytes``, ``reason: str``.

Events are :class:`~goliath_link.protocol.TextMessage`,
:class:`~goliath_link.protocol.PositionFrame`,
:class:`~goliath_link.protocol.TowerFrame`,
:class:`~goliath_link.protocol.GlobalStatusBlob`,
:class:`~goliath_link.protocol.GlobalStatusFrame` and :class:`Discarded`.
``feed``/``poll``/``flush`` never raise for any input bytes; an exception from
them is a framer bug and should be treated as fatal for the connection.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from .protocol import (
    GLOBAL_BLOB_HEAD,
    GLOBAL_BLOB_TALE,
    GLOBAL_END,
    GLOBAL_LEN,
    GLOBAL_START,
    POS_END,
    POS_LEN_FULL,
    POS_LEN_SHORT,
    POS_START,
    TOWER_IDS,
    TWR_END,
    TWR_LEN,
    TWR_START,
    GlobalStatusBlob,
    GlobalStatusFrame,
    PositionFrame,
    TowerFrame,
    parse_text_message,
)

#: Seconds without a received byte after which free-form text is complete.
DEFAULT_QUIET_INTERVAL = 0.3
#: Seconds without a received byte after which everything pending is resolved.
DEFAULT_STALL_TIMEOUT = 5.0
#: Longest text accepted before its terminator (the firmware's buffer is 512).
MAX_TEXT_LEN = 4096
#: Most status bytes searched for between the blob's HEAD and TALE.
MAX_BLOB_FIELDS = 64

Event = object  # TextMessage | PositionFrame | TowerFrame | GlobalStatus* | Discarded


@dataclass(frozen=True)
class Discarded:
    """Bytes the framer dropped while resynchronising, and why."""

    raw: bytes
    reason: str


class _Drop:
    def __init__(self, n: int, reason: str) -> None:
        self.n = n
        self.reason = reason


_NEED_MORE = object()  # not enough bytes yet to decide
_SKIP = object()  # consumed a blank text line; nothing to emit
_TEXT = object()  # a 'G' turned out to start text

# Resolution modes: bytes only / quiet_interval passed / stall or end of stream.
_NORMAL, _QUIET, _FINAL = 0, 1, 2

_HEAD_LEN = len(GLOBAL_BLOB_HEAD)
_TALE_LEN = len(GLOBAL_BLOB_TALE)
_LINE_KINDS = (b"INFO", b"ERR", b"WARN")  # firmware formats that always end in '\n'

_R_POS_UNCLOSED = "position frame '{' not closed by '}' at offset 21 or 57"
_R_POS_PARTIAL = "incomplete position frame (stream stalled or ended)"
_R_TWR_UNCLOSED = "tower frame '<' not closed by '>' at offset 3"
_R_TWR_PARTIAL = "incomplete tower frame (stream stalled or ended)"
_R_BLOB_NO_TALE = (
    f"GLOBAL STATUS HEAD without a TALE within {MAX_BLOB_FIELDS} status bytes"
)
_R_BLOB_PARTIAL = "incomplete GLOBAL STATUS blob (stream stalled or ended)"
_R_BAD_START = "byte that cannot start a message"

_NOT_NUL = re.compile(rb"[^\x00]")
#: Bytes where a text message may end or be cut: LF, NUL, '<' (tower frame),
#: 'G' (GLOBAL STATUS HEAD).
_TEXT_STOP = re.compile(rb"[\n\x00<G]")


def _is_text_start(b: int) -> bool:
    return b in (0x09, 0x0A, 0x0D) or 0x20 <= b <= 0x7E


def _is_printable(b: int) -> bool:
    return 0x20 <= b <= 0x7E


def _is_line_kind(text: bytes) -> bool:
    """True if ``text`` is (a prefix of) an INFO/ERR/WARN line."""
    t = text.lstrip(b"\r\n\t ")
    if not t:
        return False
    return any(t.startswith(k) or k.startswith(t) for k in _LINE_KINDS)


class StreamFramer:
    """Incremental framer for one connection. Feed bytes; get back messages.

    Not thread-safe: serialise calls to :meth:`feed`, :meth:`poll` and
    :meth:`flush` (the transport holds a per-connection lock).
    """

    def __init__(
        self,
        *,
        quiet_interval: float = DEFAULT_QUIET_INTERVAL,
        stall_timeout: float = DEFAULT_STALL_TIMEOUT,
        position_lengths: Iterable[int] = (POS_LEN_SHORT, POS_LEN_FULL),
        max_text: int = MAX_TEXT_LEN,
    ) -> None:
        if not quiet_interval > 0:
            raise ValueError("quiet_interval must be > 0 seconds")
        if not stall_timeout >= quiet_interval:
            raise ValueError("stall_timeout must be >= quiet_interval")
        lengths = tuple(sorted(set(int(n) for n in position_lengths)))
        if not lengths or any(n not in (POS_LEN_SHORT, POS_LEN_FULL) for n in lengths):
            raise ValueError(
                f"position_lengths must be a non-empty subset of ({POS_LEN_SHORT}, {POS_LEN_FULL})"
            )
        if max_text < 1:
            raise ValueError("max_text must be >= 1")
        self.quiet_interval = float(quiet_interval)
        self.stall_timeout = float(stall_timeout)
        self.position_lengths = lengths
        self.max_text = int(max_text)
        self._short = POS_LEN_SHORT in lengths
        self._full = POS_LEN_FULL in lengths
        self._buf = bytearray()
        self._last_rx: Optional[float] = None
        self._drop_buf = bytearray()
        self._drop_reason = ""
        #: NUL padding bytes skipped between messages.
        self.padding_bytes = 0
        #: Bytes reported in :class:`Discarded` events (including pending ones).
        self.discarded_bytes = 0

    # -- public -------------------------------------------------------------

    @property
    def pending(self) -> bytes:
        """Bytes received but not yet resolved into a message or a drop."""
        return bytes(self._buf)

    @property
    def last_receive(self) -> Optional[float]:
        """``now`` of the last :meth:`feed` that carried bytes, or None."""
        return self._last_rx

    def feed(self, data: bytes, now: Optional[float] = None) -> list[Event]:
        """Append ``data`` and return every message now complete."""
        if data:
            self._buf.extend(data)
            self._last_rx = time.monotonic() if now is None else now
        return self._parse(_NORMAL)

    def poll(self, now: Optional[float] = None) -> list[Event]:
        """Resolve what the quiet interval or the stall timeout allows."""
        if not self._buf and not self._drop_buf:
            return []
        now = time.monotonic() if now is None else now
        idle = float("inf") if self._last_rx is None else now - self._last_rx
        if idle >= self.stall_timeout:
            return self._parse(_FINAL)
        if idle >= self.quiet_interval:
            return self._parse(_QUIET)
        return []

    def flush(self) -> list[Event]:
        """Resolve everything buffered now, as at the end of the stream."""
        return self._parse(_FINAL)

    # -- parsing loop -------------------------------------------------------

    def _parse(self, mode: int) -> list[Event]:
        out: list[Event] = []
        buf = self._buf
        while True:
            m = _NOT_NUL.search(buf)
            n = m.start() if m is not None else len(buf)
            if n:
                del buf[:n]
                self.padding_bytes += n
                self._flush_drop(out)  # a Discarded run is contiguous bytes
            if not buf:
                break
            result = self._try_one(mode)
            if result is _NEED_MORE:
                if mode == _FINAL:
                    raise AssertionError("framer bug: undecided bytes in final mode")
                break
            if isinstance(result, _Drop):
                self._drop(result, out)
                continue
            if result is _SKIP:
                continue
            self._flush_drop(out)
            out.append(result)
        if mode != _NORMAL:
            self._flush_drop(out)
        return out

    def _drop(self, drop: _Drop, out: list[Event]) -> None:
        chunk = bytes(self._buf[: drop.n])
        del self._buf[: drop.n]
        self.discarded_bytes += len(chunk)
        if self._drop_buf and self._drop_reason == drop.reason:
            self._drop_buf.extend(chunk)
            return
        self._flush_drop(out)
        self._drop_buf = bytearray(chunk)
        self._drop_reason = drop.reason

    def _flush_drop(self, out: list[Event]) -> None:
        if self._drop_buf:
            out.append(Discarded(raw=bytes(self._drop_buf), reason=self._drop_reason))
            self._drop_buf = bytearray()
            self._drop_reason = ""

    def _take(self, n: int, decode) -> Event:
        frame = bytes(self._buf[:n])
        del self._buf[:n]
        return decode(frame)

    # -- one message --------------------------------------------------------

    def _try_one(self, mode: int):
        """Consume one message: return an event, a _Drop, _SKIP or _NEED_MORE.

        In final mode every return consumes at least one byte.
        """
        b0 = self._buf[0]
        if b0 == POS_START:
            return self._try_position(mode)
        if b0 == TWR_START:
            return self._try_tower(mode)
        if b0 == GLOBAL_START:
            result = self._try_g(mode)
            if result is not _TEXT:
                return result
            return self._try_text(mode)
        if not _is_text_start(b0):
            return _Drop(1, _R_BAD_START)
        return self._try_text(mode)

    def _try_position(self, mode: int):
        buf = self._buf
        size = len(buf)
        short, full = POS_LEN_SHORT, POS_LEN_FULL
        if self._short and size >= short and buf[short - 1] == POS_END:
            if not self._full:
                return self._take(short, PositionFrame.decode)
            if size > short and _is_text_start(buf[short]):
                # A message starts right after the short frame. A 58-byte
                # frame would need a temperature >= 0x2000 here: implausible.
                return self._take(short, PositionFrame.decode)
            if size >= full:
                return self._take(full if buf[full - 1] == POS_END else short, PositionFrame.decode)
            if mode != _NORMAL:
                return self._take(short, PositionFrame.decode)  # complete; nothing followed
            return _NEED_MORE
        need = full if self._full else short
        if size >= need:
            if self._full and buf[full - 1] == POS_END:
                return self._take(full, PositionFrame.decode)
            return _Drop(1, _R_POS_UNCLOSED)
        if mode == _FINAL:
            return _Drop(1, _R_POS_PARTIAL)
        return _NEED_MORE

    def _try_tower(self, mode: int):
        buf = self._buf
        if len(buf) < TWR_LEN:
            if mode != _FINAL:
                return _NEED_MORE
            # A known id byte makes it certainly a tower frame: drop it whole.
            whole = len(buf) >= 2 and buf[1] in TOWER_IDS
            return _Drop(len(buf) if whole else 1, _R_TWR_PARTIAL)
        if buf[TWR_LEN - 1] != TWR_END:
            return _Drop(1, _R_TWR_UNCLOSED)
        if buf[1] not in TOWER_IDS:
            return _Drop(TWR_LEN, f"tower frame with unknown id byte 0x{buf[1]:02X}")
        return self._take(TWR_LEN, TowerFrame.decode)

    def _try_g(self, mode: int):
        buf = self._buf
        size = len(buf)
        n = min(size, _HEAD_LEN)
        if buf[:n] == GLOBAL_BLOB_HEAD[:n]:
            if size < _HEAD_LEN:
                return _TEXT if mode == _FINAL else _NEED_MORE
            return self._try_blob(mode)
        return self._classify_g(mode)

    def _try_blob(self, mode: int):
        buf = self._buf
        limit = _HEAD_LEN + MAX_BLOB_FIELDS + _TALE_LEN
        idx = buf.find(GLOBAL_BLOB_TALE, _HEAD_LEN, limit)
        if idx >= 0:
            return self._take(idx + _TALE_LEN, GlobalStatusBlob.decode)
        if len(buf) >= limit:
            return _Drop(_HEAD_LEN, _R_BLOB_NO_TALE)
        if mode == _FINAL:
            return _Drop(len(buf), _R_BLOB_PARTIAL)
        return _NEED_MORE

    def _classify_g(self, mode: int):
        """Decide between a 19-byte ``G...S`` frame and text starting with 'G'."""
        buf = self._buf
        size = len(buf)
        first_np = None  # first non-printable byte after the 'G'
        for i in range(1, min(size, GLOBAL_LEN)):
            if not _is_printable(buf[i]):
                first_np = i
                break
        if first_np is not None and buf[first_np] in (0x0A, 0x0D):
            return _TEXT  # "G<printable...>\r\n": a text line
        if size < GLOBAL_LEN:
            if mode == _FINAL or (mode == _QUIET and first_np is None):
                return _TEXT  # quiet with only printable bytes: short free text
            return _NEED_MORE
        if first_np is None:
            return _TEXT  # 18 printable bytes after the 'G': text
        if buf[GLOBAL_LEN - 1] == GLOBAL_END:
            return self._take(GLOBAL_LEN, GlobalStatusFrame.decode)
        return _TEXT

    def _try_text(self, mode: int):
        buf = self._buf
        size = len(buf)
        limit = min(size, self.max_text + 1)
        pos = 0
        while True:
            m = _TEXT_STOP.search(buf, pos, limit)
            if m is None:
                break
            i = m.start()
            b = buf[i]
            pos = i + 1
            if b == 0x0A:
                return self._text(consume=i + 1, end=i + 1, terminated=True)
            if b == 0x00:
                return self._text(consume=i, end=i, terminated=True)
            if i == 0:
                continue  # a leading 'G' already classified as text
            if b == TWR_START:
                if i + 1 >= size:
                    partial = True
                elif buf[i + 1] in TOWER_IDS:
                    if i + 3 >= size:
                        partial = True
                    elif buf[i + 3] == TWR_END:
                        return self._text(consume=i, end=i, terminated=False)
                    else:
                        partial = False
                else:
                    partial = False
            else:  # 'G': an embedded GLOBAL STATUS HEAD?
                n = min(size - i, _HEAD_LEN)
                partial = False
                if buf[i : i + n] == GLOBAL_BLOB_HEAD[:n]:
                    if n == _HEAD_LEN:
                        return self._text(consume=i, end=i, terminated=False)
                    partial = True
            if partial:
                # Undecidable until more bytes arrive. Once quiet, a free-form
                # text ends here and the partial frame keeps waiting.
                if mode == _NORMAL:
                    return _NEED_MORE
                if mode == _QUIET:
                    if _is_line_kind(bytes(buf[:i])):
                        return _NEED_MORE
                    return self._text(consume=i, end=i, terminated=False)
                # final: treat the marker bytes as text and keep scanning
        if size > self.max_text:
            return _Drop(
                self.max_text, f"text longer than {self.max_text} bytes without a terminator"
            )
        if mode == _FINAL or (mode == _QUIET and not _is_line_kind(bytes(buf[:size]))):
            return self._text(consume=size, end=size, terminated=False)
        return _NEED_MORE

    def _text(self, *, consume: int, end: int, terminated: bool):
        raw = bytes(self._buf[:end])
        del self._buf[:consume]
        text = raw.decode("ascii", errors="replace")
        if not text.strip():
            return _SKIP  # blank line (e.g. the stray '\r' of the firmware's "\n\r")
        return parse_text_message(text, terminated=terminated)
