# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Server-Sent Events for the local interface, with ``Last-Event-ID`` replay (unit U01; design 8.2).

A tab follows the session through ``GET /events`` (an ``EventSource``). A
reloaded or reopened tab restores its state from the server, and closing a tab
never affects a job. The stream carries these events (one JSON object each):

=================  ======  ===============================================================
event              ``id``  data
=================  ======  ===============================================================
``snapshot``       seq     :meth:`SessionSnapshot.to_json() <goliath_run.types.SessionSnapshot.to_json>`
``view``           -       what the page shows for the newest snapshot sent: ``present(snapshot)``, by default
                           :func:`goliath_run.ui.presenters.session_view` (top bar, banners, disclosure chip);
                           sent after the snapshot events of each batch, so the page never renders a state
                           of its own reading (unit U02); a stream resumed at the current snapshot sends
                           its view again, since the tab may have missed it
``lease``          -       ``{"epoch": n, "held": bool}``: who controls changed (sent first, then on every change)
``heartbeat``      -       ``{}``, every :data:`HEARTBEAT_S` seconds without other events, so a page can
                           tell a quiet session from a dead connection
``end``            -       ``{"reason": ...}``: the session's snapshots ended; the stream closes
``server-error``   -       ``{"error": ...}``: the source failed or broke its contract, or the stream itself
                           failed; the stream closes (and the log has the details)
=================  ======  ===============================================================

Only ``snapshot`` events carry an ``id``, so the browser's ``Last-Event-ID``
is always the ``seq`` of the last snapshot it received. On reconnect the
stream resumes after it (:meth:`SnapshotSource.events
<goliath_run.types.SnapshotSource.events>`); a source that no longer holds the
next snapshot starts with the oldest it holds, and the gap is visible in
``seq``. Every snapshot is complete, so a new tab (no ``Last-Event-ID``) starts
from the current one. A ``Last-Event-ID`` this server cannot have sent (not a
number, or ahead of the newest snapshot, as after the session restarted) gets
the current snapshot too, with a warning in the log.

A stream never goes back: a snapshot whose ``seq`` is not greater than the
last one sent ends the stream with ``server-error`` (a stale phase on screen
would be worse than no stream). A ``present`` that raises (a snapshot the
presenters refuse to show) ends the stream the same way. The stream reads the source only as fast as
the browser takes events (back-pressure), so nothing is dropped here.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from goliath_run.types import SessionSnapshot, SnapshotSource

__all__ = [
    "BUFFERED_SNAPSHOTS",
    "CLIENT_CHECK_S",
    "HEARTBEAT_S",
    "MAX_STREAMS",
    "RETRY_MS",
    "EventStream",
    "LastEventIdError",
    "encode_event",
    "encode_retry",
    "heartbeat_event",
    "parse_last_event_id",
    "snapshot_event",
    "view_event",
]

LOG = logging.getLogger("goliath_run.ui")

RETRY_MS = 2000
"""How long a browser waits before reconnecting a dropped stream (sent as ``retry``)."""

HEARTBEAT_S = 15.0
"""Seconds without another event before a ``heartbeat`` event."""

MAX_STREAMS = 16
"""Open event streams per server; more are refused with 503 (each holds a thread)."""

CLIENT_CHECK_S = 0.5
"""How often a stream checks whether the browser closed it, so a closed tab frees its slot soon."""

BUFFERED_SNAPSHOTS = 64
"""Snapshots read ahead of a slow browser before the stream stops reading the source."""

_EVENT_NAME = re.compile(r"[a-z][a-z-]*\Z")
_LAST_EVENT_ID = re.compile(r"[0-9]{1,20}\Z")


class LastEventIdError(ValueError):
    """A ``Last-Event-ID`` this server cannot have sent."""


def parse_last_event_id(values: Sequence[str]) -> int | None:
    """The seq in the ``Last-Event-ID`` headers ``values``; None when there is none.

    This server sends ids as plain decimal numbers, so anything else (a sign,
    spaces, another script's digits, two headers) raises
    :class:`LastEventIdError`.
    """
    if not values:
        return None
    if len(values) != 1:
        raise LastEventIdError("%d Last-Event-ID headers %r" % (len(values), list(values)))
    value = values[0]
    if not _LAST_EVENT_ID.match(value):
        raise LastEventIdError("Last-Event-ID %r is not a snapshot seq" % (value[:60],))
    return int(value)


def _json(data: Any) -> str:
    # ASCII only: every line break and non-ASCII character inside a string is escaped, so the JSON is one line.
    return json.dumps(data, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def encode_event(event: str, data: Any, *, event_id: int | None = None) -> bytes:
    """One event block: optional ``id``, ``event``, one ``data`` line of JSON, a blank line."""
    if type(event) is not str or not _EVENT_NAME.match(event):
        raise ValueError("event name %r: lower-case letters and '-' only" % (event,))
    lines = []
    if event_id is not None:
        if type(event_id) is not int:
            raise TypeError("event id must be an int, got %s" % type(event_id).__name__)
        if event_id < 0:
            raise ValueError("event id must not be negative, got %d" % event_id)
        lines.append("id: %d" % event_id)
    lines.append("event: " + event)
    lines.append("data: " + _json(data))
    return ("\n".join(lines) + "\n\n").encode("ascii")


def encode_retry(ms: int) -> bytes:
    """The ``retry`` field alone (the browser's reconnection delay)."""
    return b"retry: %d\n\n" % ms


def snapshot_event(snapshot: SessionSnapshot) -> bytes:
    """A ``snapshot`` event; its id is the snapshot's ``seq``."""
    return encode_event("snapshot", snapshot.to_json(), event_id=snapshot.seq)


def heartbeat_event() -> bytes:
    return encode_event("heartbeat", {})


def view_event(view: Mapping[str, Any]) -> bytes:
    """A ``view`` event: what the page shows for the snapshot sent just before it (no ``id``)."""
    if not isinstance(view, Mapping):
        raise TypeError("a view must be a JSON object (a mapping), got %s" % type(view).__name__)
    return encode_event("view", dict(view))


class _ClientGone(Exception):
    """Writing to the browser failed: it closed the stream."""


@dataclass
class _Batch:
    items: list[Any]
    ended: bool
    error: BaseException | None
    closed: bool


class _Feed:
    """Reads ``source.events(after_seq)`` in its own thread, so the stream can wait with a timeout.

    The protocol's iterator blocks until the next snapshot, so the thread
    that writes to the browser cannot read it directly and still send
    heartbeats, notice a closed tab or close promptly. At most
    ``maxlen`` items wait here; beyond that the reader waits (back-pressure).
    When the feed is closed while the iterator blocks, the thread stays until
    the iterator yields or ends, then closes it and exits (it is a daemon).
    """

    def __init__(self, source: SnapshotSource, after_seq: int, *, logger: logging.Logger,
                 maxlen: int = BUFFERED_SNAPSHOTS) -> None:
        self._source = source
        self._after = after_seq
        self._log = logger
        self._maxlen = maxlen
        self._cond = threading.Condition()
        self._items: deque[Any] = deque()
        self._ended = False
        self._error: BaseException | None = None
        self._closed = False
        self._poked = False
        self._thread = threading.Thread(target=self._pump, name="goliath-ui-feed", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _pump(self) -> None:
        iterator: Iterator[Any] | None = None
        try:
            iterator = iter(self._source.events(self._after))
            for item in iterator:
                with self._cond:
                    while len(self._items) >= self._maxlen and not self._closed:
                        self._cond.wait()
                    if self._closed:
                        return
                    self._items.append(item)
                    self._cond.notify_all()
            with self._cond:
                self._ended = True
                self._cond.notify_all()
        except Exception as exc:  # handed to the stream, which logs it and tells the browser
            with self._cond:
                self._error = exc
                self._cond.notify_all()
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    self._log.exception("closing the session's snapshot iterator failed")

    def wait(self, timeout: float) -> _Batch:
        with self._cond:
            if not (self._items or self._ended or self._error is not None or self._closed or self._poked):
                self._cond.wait(timeout)
            items = list(self._items)
            self._items.clear()
            self._poked = False
            self._cond.notify_all()  # room again for a waiting reader
            return _Batch(items, self._ended, self._error, self._closed)

    def poke(self) -> None:
        with self._cond:
            self._poked = True
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


class EventStream:
    """One browser's event stream.

    ``write`` sends bytes to the browser (an ``OSError`` from it means the
    browser went away). ``last_event_ids`` are the request's ``Last-Event-ID``
    headers. ``lease_state`` returns the control lease's state as JSON.
    ``client_closed``, when given, says whether the browser has closed the
    connection; it is checked every :data:`CLIENT_CHECK_S`. ``present``, when
    given, turns a snapshot into what the page shows; its result follows the
    snapshot events as a ``view`` event (the newest snapshot of each batch).

    The constructor reads the current snapshot (an exception from the source
    propagates, so the server can refuse the request before it starts the
    stream). :meth:`run` then streams until one of: ``"closed"`` (:meth:`close`,
    when the server shuts down), ``"ended"`` (the session ended),
    ``"failed"`` (the source failed or broke its contract, or anything else
    went wrong while streaming: the page gets ``server-error``) or
    ``"client-gone"``. A heartbeat follows :data:`HEARTBEAT_S` (or
    ``heartbeat_s``) seconds without any other event.
    """

    def __init__(self, source: SnapshotSource, write: Callable[[bytes], None], *,
                 last_event_ids: Sequence[str], lease_state: Callable[[], Mapping[str, Any]],
                 heartbeat_s: float = HEARTBEAT_S, client_closed: Callable[[], bool] | None = None,
                 logger: logging.Logger | None = None,
                 present: Callable[[SessionSnapshot], Mapping[str, Any]] | None = None) -> None:
        if isinstance(heartbeat_s, bool) or not isinstance(heartbeat_s, (int, float)) \
                or not math.isfinite(heartbeat_s) or heartbeat_s <= 0:
            raise ValueError("heartbeat_s must be a positive number of seconds, got %r" % (heartbeat_s,))
        self._write = write
        self._lease_state = lease_state
        if present is not None and not callable(present):
            raise TypeError("present must be callable (a snapshot to the view the page shows), got %s"
                            % type(present).__name__)
        self._heartbeat_s = float(heartbeat_s)
        self._client_closed = client_closed
        self._present = present
        self._log = logger or LOG
        try:
            last = parse_last_event_id(last_event_ids)
        except LastEventIdError as exc:
            self._log.warning("event stream: %s; sending the current snapshot instead (it is complete)", exc)
            last = None
        current = source.snapshot()
        if not isinstance(current, SessionSnapshot):
            raise TypeError("the snapshot source returned %s, not a SessionSnapshot" % type(current).__name__)
        self._initial: SessionSnapshot | None
        if last is None:
            self._initial, after = current, current.seq
        elif last > current.seq:
            self._log.warning("event stream: Last-Event-ID %d is ahead of the newest snapshot (%d), as after the "
                              "session restarted; sending the current snapshot instead", last, current.seq)
            self._initial, after = current, current.seq
        else:
            self._initial, after = None, last
        # A view event carries no id, so a tab resuming at the current snapshot may have missed its view (the
        # connection dropped between the two): it is sent again. Resuming further back, the replayed batch ends
        # with the view of its newest snapshot anyway.
        self._resume_view = current if self._initial is None and last == current.seq else None
        self._after = after
        self._sent_at = time.monotonic()  # when the last event went out (heartbeats follow a quiet interval)
        self._feed = _Feed(source, after, logger=self._log)

    def poke(self) -> None:
        """Something besides the snapshots changed (the lease): look again."""
        self._feed.poke()

    def close(self) -> None:
        """End the stream soon (from another thread); :meth:`run` returns ``"closed"``."""
        self._feed.close()

    def _send_view(self, snapshot: SessionSnapshot) -> None:
        if self._present is not None:
            self._send(view_event(self._present(snapshot)))

    def _send(self, data: bytes) -> None:
        try:
            self._write(data)
        except OSError as exc:
            raise _ClientGone(exc) from exc
        self._sent_at = time.monotonic()

    def _fail(self, message: str, exc: BaseException | None = None) -> str:
        if exc is not None:
            self._log.error("event stream: %s: %s: %s", message, type(exc).__name__, exc, exc_info=exc)
            text = "%s: %s: %s" % (message, type(exc).__name__, exc)
        else:
            self._log.error("event stream: %s", message)
            text = message
        self._send(encode_event("server-error", {"error": text + ". See the goliath-run log."}))
        return "failed"

    def run(self) -> str:
        """Stream until the end; returns why it ended (see the class documentation)."""
        try:
            try:
                return self._run()
            except _ClientGone:
                raise
            except Exception as exc:  # anything else: the page is told, the log has the traceback
                return self._fail("the event stream failed", exc)
        except _ClientGone as gone:
            self._log.info("event stream: the browser closed it (%s)", gone.__cause__)
            return "client-gone"
        finally:
            self._feed.close()

    def _run(self) -> str:
        self._send(encode_retry(RETRY_MS))
        lease = dict(self._lease_state())
        self._send(encode_event("lease", lease))
        last_sent = self._after
        if self._initial is not None:
            self._send(snapshot_event(self._initial))
            last_sent = self._initial.seq
            self._send_view(self._initial)
        elif self._resume_view is not None:
            self._send_view(self._resume_view)
        self._feed.start()
        while True:
            wait = max(0.0, self._sent_at + self._heartbeat_s - time.monotonic())
            if self._client_closed is not None:
                wait = min(wait, CLIENT_CHECK_S)
            batch = self._feed.wait(wait)
            if batch.closed:
                return "closed"
            newest: SessionSnapshot | None = None
            for item in batch.items:
                if not isinstance(item, SessionSnapshot):
                    return self._fail("the session's snapshot source yielded %s, not a SessionSnapshot; this "
                                      "stream ends (reload to start from the current snapshot)"
                                      % type(item).__name__)
                if item.seq <= last_sent:
                    return self._fail("the session's snapshot source went back from seq %d to seq %d; this stream "
                                      "ends rather than show an older state (reload to start from the current "
                                      "snapshot)" % (last_sent, item.seq))
                self._send(snapshot_event(item))
                last_sent = item.seq
                newest = item
            if newest is not None:
                self._send_view(newest)
            now_lease = dict(self._lease_state())
            if now_lease != lease:
                lease = now_lease
                self._send(encode_event("lease", lease))
            if batch.error is not None:
                return self._fail("the session's snapshot source failed", batch.error)
            if batch.ended:
                self._send(encode_event("end", {"reason": "the session ended; its snapshots stop here"}))
                return "ended"
            if self._client_closed is not None and self._client_closed():
                self._log.info("event stream: the browser closed it")
                return "client-gone"
            if time.monotonic() - self._sent_at >= self._heartbeat_s:
                self._send(heartbeat_event())
