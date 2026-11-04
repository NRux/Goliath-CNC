# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""TCP link to the Goliath robot: three connections, one tagged event stream.

Why three connections
---------------------
The robot (``10.0.0.1:80`` on its own Wi-Fi AP, or its DHCP address on a
shared network) routes its output **by connection slot**, not to whoever
asked. This is from a disassembly of firmware GFW 01.02.00 and contradicts
``docs/protocol/01-transport-and-framing.md``, which called the extra sockets
vestigial. Each accepted TCP connection takes the first free of 6 slots, and:

======  ================  ====================================================
index   firmware slot     what the robot sends there
======  ================  ====================================================
0       0 (1st accepted)  free-form ``tcp_printf`` replies (``CAGE STATUS:``,
                          ``cfg read index :``, ``spindle distance:``), tower
                          frames, and replies addressed to the requester (the
                          48-byte status blob, ``count:``, ...)
1       1 (2nd accepted)  every ``INFO;``/``ERR;``/``WARN;`` line, including
                          the ``INFO;34`` identity reply, ``INFO;0`` command
                          acks and the safety errors
2       2 (3rd accepted)  position frames
======  ================  ====================================================

A message for an empty slot is silently dropped by the robot. So a
single-socket client never sees INFO/ERR/WARN or positions. Like the vendor
app, :meth:`GoliathTransport.connect` opens connection 0, waits 0.25 s, opens
1, waits 0.25 s, opens 2, waits 0.5 s, and fails unless all three are still up.
Commands are sent **only on connection 0**.

**The slot mapping is an assumption** the transport cannot check: if another
client (an old Slingshot install, a phone) already holds slots, or the robot
has not yet freed the slots of a connection closed moments ago, our
connections land in other slots and replies go elsewhere. Every record is
tagged with the connection it arrived on, so a capture shows where things
really arrived.

Events
------
Each connection has its own :class:`~goliath_link.framing.StreamFramer` and
reader thread. Everything they produce goes into **one** queue,
:attr:`GoliathTransport.records`, as :class:`LinkEvent` records
``(conn, t, event)``: the connection index, the ``time.monotonic()`` time the
bytes completing the event were received, and the event (``TextMessage``,
``PositionFrame``, ``TowerFrame``, ``GlobalStatusBlob``, ``GlobalStatusFrame``,
``Discarded``, :class:`ListenerFailed` or :class:`TransportClosed`).
:attr:`GoliathTransport.incoming` is a queue-like view of the same records
that returns bare events, for consumers that do not need the tags. Both
consume the same queue: use one or the other.

Each :meth:`~GoliathTransport.connect` starts a fresh session: a new queue,
new framers, new threads. Nothing from a previous link (records, half-received
frames, its ``TransportClosed``) can leak into the new one; the old queue
stays readable through any reference to it. ``connect()`` refuses to start
while a thread of the previous link is still running.

Failure handling (fail loudly, never silently)
----------------------------------------------
* ``connect()`` raises :class:`ConnectionError` naming the connection if any
  connection cannot be opened, or drops before the settle time ends. All
  connections are closed first.
* Losing **any** connection takes the whole link down: every socket is shut
  down and closed, :attr:`~GoliathTransport.connected` turns False, sends raise
  :class:`NotConnectedError` (a ``ConnectionError`` and a ``RuntimeError``)
  saying which connection failed and why, and exactly one
  :class:`TransportClosed` ``(error, conn)`` is queued as the session's last
  record. ``error`` is None only after a local :meth:`~GoliathTransport.close`;
  an EOF from the robot is a :class:`ConnectionError`.
* A reader converts **any** exception (socket error, framer bug, recorder
  failure) into that link-down path; nothing is reported as a clean close.
* Before the ``TransportClosed`` record, each framer is flushed, so bytes the
  robot sent last come out as messages or ``Discarded`` events.
* Listener exceptions: see :class:`ListenerFailed` and
  :meth:`~GoliathTransport.check`.

Liveness: TCP keepalive (``SO_KEEPALIVE``, tuned to :class:`Keepalive` where
the OS allows it) is enabled on every connection, so a robot that vanishes
without FIN/RST takes the link down after about ``idle + interval * count``
seconds of silence (25 s by default). There is no application-level ping. Per
connection, :attr:`~GoliathTransport.last_receive` and
:meth:`~GoliathTransport.connection_info` expose when bytes last arrived.

Public API
----------
``GoliathTransport(host="10.0.0.1", port=80, *, timeout=10.0, opener,
listener=None, encoding="ascii", connections=3, connect_gaps=(0.25, 0.25),
settle=0.5, recorder=None, keepalive=Keepalive(), quiet_interval=0.3,
stall_timeout=5.0, poll_interval=0.05, join_timeout=2.0,
position_lengths=(22, 58))`` (``quiet_interval``, ``stall_timeout`` and
``position_lengths`` configure each connection's framer; see
:mod:`goliath_link.framing`).

* ``connect()`` / ``close()`` / context manager; ``connected``.
* ``send(command: str)`` / ``send_bytes(payload: bytes)`` -- on connection 0,
  under :attr:`send_lock` (an ``RLock``: hold it to make a multi-part send
  atomic with respect to other senders).
* ``records`` (``queue.Queue`` of :class:`LinkEvent`), ``incoming`` (bare
  events from the same queue).
* ``listener(record: LinkEvent)`` -- called on a transport thread for every
  record, in queue order, one at a time, just before the record is queued.
  Keep it fast; it may call ``send()`` or ``close()``.
* ``recorder(conn: int, direction: str, data: bytes, t: float)`` -- called
  for every chunk received (``'<'``) and every payload about to be sent
  (``'>'``), serialised, for session captures. If it raises, the link goes
  down with that error (a capture that silently misses data is worse).
* ``check()`` -- raises :class:`ListenerError` for listener failures not yet
  reported, then :class:`ConnectionError` if the link went down with an error.
* ``last_receive``, ``connection_info()``, ``closed_error``, ``closed_conn``,
  ``threads``, ``connection_count``.
* ``opener(host, port, timeout) -> socket`` is injectable (tests use fakes);
  :func:`configure_socket` is applied to whatever it returns that has
  ``setsockopt``.

There is **no** automatic reconnect (the vendor layer had none); build
reconnect policy on top of ``close()`` + ``connect()``.
"""

from __future__ import annotations

import logging
import queue
import socket
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from typing import Callable, NamedTuple, Optional, Protocol, Sequence

from .framing import DEFAULT_QUIET_INTERVAL, DEFAULT_STALL_TIMEOUT, StreamFramer
from .protocol import POS_LEN_FULL, POS_LEN_SHORT

log = logging.getLogger(__name__)

DEFAULT_PORT = 80
DEFAULT_AP_ADDRESS = "10.0.0.1"
#: Connections opened by default, in this order (= firmware slots 0, 1, 2).
DEFAULT_CONNECTIONS = 3
#: Pauses between opening consecutive connections (vendor: 250 ms each).
DEFAULT_CONNECT_GAPS = (0.25, 0.25)
#: Pause after the last connection before declaring the link up (vendor: 500 ms).
DEFAULT_SETTLE = 0.5
#: How often the quiet-interval flush runs.
DEFAULT_POLL_INTERVAL = 0.05
#: How long close()/teardown waits for each thread.
DEFAULT_JOIN_TIMEOUT = 2.0
RECV_SIZE = 8192

#: What GFW 01.02.00 sends to each slot (firmware facts brief, section 1).
SLOT_ROLES = (
    "printf replies, tower frames, replies to the requester (status blob)",
    "INFO/ERR/WARN lines (identity INFO;34, INFO;0 acks, safety errors)",
    "position frames",
)


class SocketLike(Protocol):
    def sendall(self, data: bytes) -> None: ...
    def recv(self, bufsize: int) -> bytes: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class Keepalive:
    """TCP keepalive timing: probe after ``idle`` s of silence, every
    ``interval`` s, and give up after ``count`` unanswered probes."""

    idle: float = 10.0
    interval: float = 5.0
    count: int = 3

    def __post_init__(self) -> None:
        if not (self.idle > 0 and self.interval > 0 and self.count >= 1):
            raise ValueError("keepalive idle/interval must be > 0 and count >= 1")


DEFAULT_KEEPALIVE = Keepalive()


@dataclass(frozen=True)
class TransportClosed:
    """The link went down. Always the last record of a session.

    ``error`` is None only for a local :meth:`GoliathTransport.close`;
    otherwise it is the exception that brought the link down (an EOF from the
    robot is a :class:`ConnectionError`). ``conn`` is the index of the
    connection that failed, or None for a local close.
    """

    error: Optional[BaseException] = None
    conn: Optional[int] = None


class LinkEvent(NamedTuple):
    """One record of the event queue: ``(conn, t, event)``."""

    conn: Optional[int]  #: connection index the bytes arrived on (None: link-level)
    t: float  #: time.monotonic() when the bytes completing ``event`` arrived
    event: object


@dataclass(frozen=True)
class ListenerFailed:
    """The listener raised ``error`` while handling ``record``.

    Queued immediately before ``record`` itself. The error is also logged and
    raised by :meth:`GoliathTransport.check`. The listener keeps being called
    for later records.
    """

    error: BaseException
    record: LinkEvent


class NotConnectedError(ConnectionError, RuntimeError):
    """A send was attempted while the link is not up. The message says why."""


class ListenerError(RuntimeError):
    """Raised by :meth:`GoliathTransport.check` when the listener has raised."""

    def __init__(self, message: str, errors: Sequence[BaseException]) -> None:
        super().__init__(message)
        self.errors = list(errors)


@dataclass(frozen=True)
class ConnectionInfo:
    index: int
    connected: bool
    last_receive: Optional[float]
    bytes_received: int
    bytes_sent: int
    socket_options: str


# --------------------------------------------------------------------------- #
# Socket setup
# --------------------------------------------------------------------------- #


def _tune_keepalive(sock, ka: Keepalive) -> str:
    idle = max(1, int(round(ka.idle)))
    intvl = max(1, int(round(ka.interval)))
    idle_opt = getattr(socket, "TCP_KEEPIDLE", None)
    if idle_opt is None and sys.platform == "darwin":
        idle_opt = getattr(socket, "TCP_KEEPALIVE", None)
    intvl_opt = getattr(socket, "TCP_KEEPINTVL", None)
    ioctl = getattr(sock, "ioctl", None)
    sio = getattr(socket, "SIO_KEEPALIVE_VALS", None)
    if idle_opt is not None and intvl_opt is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, idle_opt, idle)
            sock.setsockopt(socket.IPPROTO_TCP, intvl_opt, intvl)
            cnt_opt = getattr(socket, "TCP_KEEPCNT", None)
            if cnt_opt is not None:
                sock.setsockopt(socket.IPPROTO_TCP, cnt_opt, int(ka.count))
                return f"idle {idle} s, interval {intvl} s, count {ka.count}"
            return f"idle {idle} s, interval {intvl} s, OS default probe count"
        except OSError:
            if ioctl is None or sio is None:
                raise
            # Windows builds that define the constants but reject them: the
            # ioctl below sets the same timing.
    if ioctl is not None and sio is not None:
        ioctl(sio, (1, idle * 1000, intvl * 1000))
        return f"idle {idle} s, interval {intvl} s, OS default probe count (Windows ioctl)"
    raise OSError("this platform exposes no way to set keepalive timing")


def configure_socket(sock, keepalive: Optional[Keepalive] = DEFAULT_KEEPALIVE) -> str:
    """Set TCP_NODELAY and keepalive on ``sock``; return a description.

    TCP_NODELAY: the robot's WINC1500 hands each received TCP segment to the
    command parser as one command, so two commands coalesced by Nagle's
    algorithm would arrive as one garbled command.

    A failure to set TCP_NODELAY or SO_KEEPALIVE raises. A failure to tune the
    keepalive timing is logged as a warning and described in the result (the
    OS default, often 2 hours, then applies).
    """
    setsockopt = getattr(sock, "setsockopt", None)
    if setsockopt is None:
        return "not configured: socket object has no setsockopt()"
    setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    if keepalive is None:
        setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 0)
        return "TCP_NODELAY on; SO_KEEPALIVE off (disabled by caller)"
    setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    try:
        timing = _tune_keepalive(sock, keepalive)
    except OSError as exc:
        log.warning(
            "TCP keepalive is on but its timing could not be set (%r); the OS "
            "default applies, so a robot that vanishes silently may go unnoticed "
            "for a long time",
            exc,
        )
        timing = f"timing not set ({exc!r}), OS default"
    return f"TCP_NODELAY on; SO_KEEPALIVE on ({timing})"


def _default_opener(host: str, port: int, timeout: float) -> SocketLike:
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        configure_socket(sock, DEFAULT_KEEPALIVE)
        # The connect timeout must not apply to the long-lived blocking reader.
        sock.settimeout(None)
    except BaseException:
        sock.close()
        raise
    return sock


def _ordinal(n: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")


def _shutdown_close(sock, index: int) -> None:
    """Shut down and close a socket during teardown.

    ``close()`` alone does not wake a thread blocked in ``recv()`` on Linux;
    ``shutdown()`` does. Errors here are expected on an already-broken socket:
    the error that brought the link down is recorded elsewhere, so these are
    logged at debug level only.
    """
    shutdown = getattr(sock, "shutdown", None)
    if shutdown is not None:
        try:
            shutdown(socket.SHUT_RDWR)
        except OSError as exc:
            log.debug("connection %d: shutdown during teardown: %r", index, exc)
    try:
        sock.close()
    except OSError as exc:
        log.debug("connection %d: close during teardown: %r", index, exc)


# --------------------------------------------------------------------------- #
# One link session
# --------------------------------------------------------------------------- #


class _Conn:
    def __init__(self, index: int, sock, framer: StreamFramer, options: str) -> None:
        self.index = index
        self.sock = sock
        self.framer = framer
        self.options = options
        self.lock = threading.Lock()  # serialises framer.feed/poll/flush + dispatch
        self.thread: Optional[threading.Thread] = None
        self.last_rx: Optional[float] = None
        self.bytes_rx = 0
        self.bytes_tx = 0


class _Session:
    """Everything belonging to one connect(): sockets, framers, threads, queue."""

    def __init__(self, owner: "GoliathTransport") -> None:
        self.owner = owner
        self.records: "queue.Queue[LinkEvent]" = queue.Queue()
        self.conns: list[_Conn] = []
        self.state_lock = threading.Lock()
        self.dispatch_lock = threading.RLock()
        self.down = threading.Event()
        self.finished = threading.Event()
        self.ready = False
        self.closed_error: Optional[BaseException] = None
        self.closed_conn: Optional[int] = None
        self.down_at: Optional[float] = None
        self.closed_emitted = False
        self.supervisor = threading.Thread(
            target=self._supervise, name="goliath-link-supervisor", daemon=True
        )

    # -- bookkeeping --------------------------------------------------------

    @property
    def threads(self) -> list[threading.Thread]:
        with self.state_lock:
            ths = [c.thread for c in self.conns if c.thread is not None]
        if self.supervisor.ident is not None:
            ths.append(self.supervisor)
        return ths

    def snapshot(self) -> list[_Conn]:
        with self.state_lock:
            return list(self.conns)

    def add(self, index: int, sock, options: str) -> _Conn:
        owner = self.owner
        framer = StreamFramer(
            quiet_interval=owner._quiet_interval,
            stall_timeout=owner._stall_timeout,
            position_lengths=owner._position_lengths,
        )
        conn = _Conn(index, sock, framer, options)
        with self.state_lock:
            if self.down.is_set():
                _shutdown_close(sock, index)
                raise ConnectionError(
                    f"link went down while connection {index} was being opened: "
                    f"{self.closed_error!r}"
                )
            self.conns.append(conn)
            conn.thread = threading.Thread(
                target=self._read, args=(conn,), name=f"goliath-link-reader-{index}", daemon=True
            )
            conn.thread.start()
        return conn

    def link_down(self, conn: Optional[int], error: Optional[BaseException]) -> bool:
        """Take the whole link down (idempotent). Returns True for the first call."""
        with self.state_lock:
            if self.down.is_set():
                return False
            self.closed_conn = conn
            self.closed_error = error
            self.down_at = time.monotonic()
            self.down.set()
            conns = list(self.conns)
        if error is not None:
            log.error("goliath link down: connection %s failed: %r", conn, error)
        for c in conns:
            _shutdown_close(c.sock, c.index)
        return True

    def wait_finished(self, timeout: float) -> bool:
        if self.supervisor.ident is None:
            return True
        if self.supervisor is threading.current_thread():
            return False
        self.supervisor.join(timeout)
        return self.finished.is_set()

    def not_connected_reason(self) -> str:
        if not self.down.is_set():
            return "transport is not connected: connect() is still in progress"
        if self.closed_conn is None and self.closed_error is None:
            return "transport is not connected: it was closed locally"
        if self.closed_conn is None:
            return f"transport is not connected: the link went down: {self.closed_error!r}"
        return (
            f"transport is not connected: the link went down when connection "
            f"{self.closed_conn} failed: {self.closed_error!r}"
        )

    # -- dispatch -----------------------------------------------------------

    def dispatch(self, conn: Optional[int], t: float, events: Sequence[object]) -> None:
        listener = self.owner._listener
        with self.dispatch_lock:
            for event in events:
                rec = LinkEvent(conn, t, event)
                if listener is not None:
                    try:
                        listener(rec)
                    except Exception as exc:
                        self.owner._listener_failed(exc, rec)
                        self.records.put(LinkEvent(conn, t, ListenerFailed(exc, rec)))
                if self.closed_emitted:
                    log.warning(
                        "goliath link: record from connection %s arrived after "
                        "TransportClosed (a reader outlived teardown): %r",
                        conn,
                        event,
                    )
                self.records.put(rec)

    # -- threads ------------------------------------------------------------

    def _read(self, c: _Conn) -> None:
        try:
            while True:
                data = c.sock.recv(RECV_SIZE)
                now = time.monotonic()
                if not data:
                    if not self.down.is_set():
                        self.link_down(
                            c.index,
                            ConnectionError(
                                f"connection {c.index} (firmware slot {c.index}) was "
                                "closed by the robot (EOF)"
                            ),
                        )
                    return
                c.last_rx = now
                c.bytes_rx += len(data)
                self.owner._record(c.index, "<", data, now)
                with c.lock:
                    events = c.framer.feed(data, now)
                    if events:
                        self.dispatch(c.index, now, events)
        except BaseException as exc:  # noqa: BLE001 - every failure must reach the queue
            if self.down.is_set() and isinstance(exc, OSError):
                # Expected: teardown shut this socket down under the reader.
                log.debug("connection %d reader stopped during teardown: %r", c.index, exc)
                return
            self._fail(c.index, exc)

    def _fail(self, conn: Optional[int], exc: BaseException) -> None:
        if not self.link_down(conn, exc):
            # The link was already down for another reason; this error cannot
            # be the TransportClosed cause, so make sure it is still seen.
            log.error(
                "goliath link: connection %s failed after the link was already down",
                conn,
                exc_info=exc,
            )

    def _supervise(self) -> None:
        poll_interval = self.owner._poll_interval
        current: Optional[int] = None
        try:
            while not self.down.wait(poll_interval):
                now = time.monotonic()
                for c in self.snapshot():
                    current = c.index
                    with c.lock:
                        events = c.framer.poll(now)
                        if events:
                            self.dispatch(c.index, now, events)
                current = None
        except BaseException as exc:  # noqa: BLE001 - a framer bug must take the link down
            self._fail(current, exc)
        finally:
            self._teardown()

    def _teardown(self) -> None:
        join_timeout = self.owner._join_timeout
        conns = self.snapshot()
        deadline = time.monotonic() + join_timeout
        stuck = []
        for c in conns:
            th = c.thread
            if th is not None and th is not threading.current_thread():
                th.join(max(0.0, deadline - time.monotonic()))
                if th.is_alive():
                    stuck.append(c.index)
        now = time.monotonic()
        for c in conns:
            if not c.lock.acquire(timeout=join_timeout):
                log.warning(
                    "goliath link: connection %d is busy (listener blocked?); its "
                    "%d buffered byte(s) were not flushed before TransportClosed",
                    c.index,
                    len(c.framer.pending),
                )
                continue
            try:
                events = c.framer.flush()
                if events:
                    self.dispatch(c.index, now, events)
            finally:
                c.lock.release()
        if stuck:
            log.warning(
                "goliath link: reader thread(s) for connection(s) %s did not stop "
                "within %.1f s of the link going down",
                stuck,
                join_timeout,
            )
        closed = TransportClosed(error=self.closed_error, conn=self.closed_conn)
        t = self.down_at if self.down_at is not None else now
        self.dispatch(self.closed_conn, t, [closed])
        self.closed_emitted = True
        self.finished.set()


# --------------------------------------------------------------------------- #
# Public transport
# --------------------------------------------------------------------------- #


class _EventView:
    """Queue-like view of :attr:`GoliathTransport.records` yielding bare events."""

    def __init__(self, transport: "GoliathTransport") -> None:
        self._t = transport

    def get(self, block: bool = True, timeout: Optional[float] = None) -> object:
        return self._t.records.get(block, timeout).event

    def get_nowait(self) -> object:
        return self.get(False)

    def empty(self) -> bool:
        return self._t.records.empty()

    def qsize(self) -> int:
        return self._t.records.qsize()


class GoliathTransport:
    def __init__(
        self,
        host: str = DEFAULT_AP_ADDRESS,
        port: int = DEFAULT_PORT,
        *,
        timeout: float = 10.0,
        opener: Callable[[str, int, float], SocketLike] = _default_opener,
        listener: Optional[Callable[[LinkEvent], None]] = None,
        encoding: str = "ascii",
        connections: int = DEFAULT_CONNECTIONS,
        connect_gaps: Sequence[float] = DEFAULT_CONNECT_GAPS,
        settle: float = DEFAULT_SETTLE,
        recorder: Optional[Callable[[int, str, bytes, float], None]] = None,
        keepalive: Optional[Keepalive] = DEFAULT_KEEPALIVE,
        quiet_interval: float = DEFAULT_QUIET_INTERVAL,
        stall_timeout: float = DEFAULT_STALL_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        join_timeout: float = DEFAULT_JOIN_TIMEOUT,
        position_lengths: Sequence[int] = (POS_LEN_SHORT, POS_LEN_FULL),
    ) -> None:
        if not 1 <= int(connections) <= 3:
            raise ValueError("connections must be 1, 2 or 3 (firmware slots 0..2)")
        gaps = tuple(float(g) for g in connect_gaps)
        if len(gaps) < int(connections) - 1 or any(g < 0 for g in gaps):
            raise ValueError(
                f"connect_gaps needs {int(connections) - 1} non-negative value(s)"
            )
        if settle < 0 or poll_interval <= 0 or join_timeout <= 0 or timeout <= 0:
            raise ValueError("settle must be >= 0; poll_interval, join_timeout, timeout > 0")
        # Validate framer options now rather than at connect().
        StreamFramer(
            quiet_interval=quiet_interval,
            stall_timeout=stall_timeout,
            position_lengths=position_lengths,
        )
        self.host = host
        self.port = port
        self._timeout = timeout
        self._opener = opener
        self._listener = listener
        self._encoding = encoding
        self._connections = int(connections)
        self.connect_gaps = gaps[: self._connections - 1]
        self.settle = float(settle)
        self._recorder = recorder
        self._keepalive = keepalive
        self._quiet_interval = float(quiet_interval)
        self._stall_timeout = float(stall_timeout)
        self._poll_interval = float(poll_interval)
        self._join_timeout = float(join_timeout)
        self._position_lengths = tuple(position_lengths)
        self._session: Optional[_Session] = None
        self._idle_records: "queue.Queue[LinkEvent]" = queue.Queue()
        self._send_lock = threading.RLock()
        self._record_lock = threading.Lock()
        self._listener_lock = threading.Lock()
        self._unreported_listener_errors: list[BaseException] = []
        #: Bare events from :attr:`records` (same queue; use one or the other).
        self.incoming = _EventView(self)

    # -- lifecycle ----------------------------------------------------------

    def connect(self) -> None:
        """Open the connections in order; raise ConnectionError if any fails."""
        old = self._session
        if old is not None:
            if not old.down.is_set():
                raise RuntimeError("already connected")
            old.wait_finished(self._join_timeout)
            alive = [th.name for th in old.threads if th.is_alive()]
            if alive:
                raise RuntimeError(
                    f"cannot reconnect: thread(s) of the previous link are still running: "
                    f"{alive}. They are blocked in a socket call that ignored close(); "
                    "retry once they have exited."
                )
        session = _Session(self)
        self._session = session
        session.supervisor.start()
        n = self._connections
        try:
            for i in range(n):
                if i > 0:
                    time.sleep(self.connect_gaps[i - 1])
                if session.down.is_set():
                    raise ConnectionError(
                        f"connection {session.closed_conn} was lost while opening "
                        f"connection {i}: {session.closed_error!r}"
                    ) from session.closed_error
                try:
                    sock = self._opener(self.host, self.port, self._timeout)
                except Exception as exc:
                    session.link_down(i, exc)
                    raise ConnectionError(
                        f"could not open connection {i} (the {_ordinal(i + 1)} of {n}; "
                        f"expected firmware slot {i}) to {self.host}:{self.port}: {exc}"
                    ) from exc
                try:
                    options = configure_socket(sock, self._keepalive)
                except Exception as exc:
                    _shutdown_close(sock, i)
                    session.link_down(i, exc)
                    raise ConnectionError(
                        f"could not configure connection {i} (TCP_NODELAY/keepalive): {exc}"
                    ) from exc
                session.add(i, sock, options)
            time.sleep(self.settle)
            with session.state_lock:
                if not session.down.is_set():
                    session.ready = True
            if not session.ready:
                raise ConnectionError(
                    f"connection {session.closed_conn} was lost while the link was "
                    f"settling after connect: {session.closed_error!r}"
                ) from session.closed_error
        except BaseException as exc:
            session.link_down(None, exc)
            session.wait_finished(self._join_timeout)
            raise
        log.info(
            "goliath link up to %s:%d with %d connection(s); assuming firmware slots: %s",
            self.host,
            self.port,
            n,
            "; ".join(f"{i}: {SLOT_ROLES[i]}" for i in range(n)),
        )
        unconfigured = [c.index for c in session.snapshot() if c.options.startswith("not configured")]
        if unconfigured:
            log.warning(
                "connection(s) %s: the opener returned an object without setsockopt(), so "
                "the transport could not set TCP_NODELAY/keepalive itself; they are only "
                "on if the opener set them (the default opener does)",
                unconfigured,
            )
        if n < 3:
            missing = ", ".join(f"slot {i} ({SLOT_ROLES[i]})" for i in range(n, 3))
            log.warning(
                "only %d connection(s) open: on GFW 01.02.00 the robot sends to %s, "
                "and those messages will NOT be received",
                n,
                missing,
            )

    def close(self) -> None:
        """Close every connection; queue TransportClosed(error=None) if still up."""
        session = self._session
        if session is None:
            return
        session.link_down(None, None)
        if threading.current_thread() in session.threads:
            return  # called from a listener or reader: never join ourselves
        session.wait_finished(self._join_timeout + 1.0)
        alive = [th.name for th in session.threads if th.is_alive()]
        if alive:
            warnings.warn(
                f"goliath-link thread(s) {alive} did not stop within "
                f"{self._join_timeout:g} s of close()",
                RuntimeWarning,
                stacklevel=2,
            )

    def __enter__(self) -> "GoliathTransport":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        s = self._session
        return s is not None and s.ready and not s.down.is_set()

    # -- state --------------------------------------------------------------

    @property
    def records(self) -> "queue.Queue[LinkEvent]":
        """The current session's queue of :class:`LinkEvent` records."""
        s = self._session
        return s.records if s is not None else self._idle_records

    @property
    def send_lock(self) -> "threading.RLock":
        return self._send_lock

    @property
    def connection_count(self) -> int:
        return self._connections

    @property
    def closed_error(self) -> Optional[BaseException]:
        s = self._session
        return s.closed_error if s is not None else None

    @property
    def closed_conn(self) -> Optional[int]:
        s = self._session
        return s.closed_conn if s is not None else None

    @property
    def threads(self) -> list[threading.Thread]:
        """Reader and supervisor threads of the current (or last) session."""
        s = self._session
        return s.threads if s is not None else []

    @property
    def _reader(self) -> Optional[threading.Thread]:
        """Connection 0's reader thread (kept for older tests)."""
        s = self._session
        if s is None:
            return None
        conns = s.snapshot()
        return conns[0].thread if conns else None

    @property
    def last_receive(self) -> tuple[Optional[float], ...]:
        """Per connection, ``time.monotonic()`` of the last received byte (or None)."""
        s = self._session
        found = {c.index: c.last_rx for c in s.snapshot()} if s is not None else {}
        return tuple(found.get(i) for i in range(self._connections))

    def connection_info(self) -> list[ConnectionInfo]:
        s = self._session
        if s is None:
            return []
        up = self.connected
        return [
            ConnectionInfo(
                index=c.index,
                connected=up,
                last_receive=c.last_rx,
                bytes_received=c.bytes_rx,
                bytes_sent=c.bytes_tx,
                socket_options=c.options,
            )
            for c in s.snapshot()
        ]

    def check(self) -> None:
        """Raise if something went wrong behind the caller's back.

        :class:`ListenerError` (chained to the first exception) if the listener
        raised since the last ``check()``; otherwise :class:`ConnectionError`
        (chained to the cause) if the link went down with an error.
        """
        with self._listener_lock:
            errors = self._unreported_listener_errors
            self._unreported_listener_errors = []
        if errors:
            raise ListenerError(
                f"the listener raised {len(errors)} time(s); first: {errors[0]!r}", errors
            ) from errors[0]
        s = self._session
        if s is not None and s.down.is_set() and s.closed_error is not None:
            raise ConnectionError(s.not_connected_reason()) from s.closed_error

    # -- send ---------------------------------------------------------------

    def send(self, command: str) -> None:
        """Send a command string on connection 0. Raises if not connected."""
        self.send_bytes(command.encode(self._encoding))

    def send_bytes(self, payload: bytes) -> None:
        """Send raw bytes on connection 0, atomically. Raises if not connected.

        A failure while sending takes the link down (the robot may have
        received part of the payload) and is re-raised.
        """
        data = bytes(payload)
        with self._send_lock:
            s = self._session
            if s is None:
                raise NotConnectedError(
                    "transport is not connected: connect() has not been called"
                )
            if not s.ready or s.down.is_set():
                raise NotConnectedError(s.not_connected_reason())
            c = s.conns[0]
            try:
                self._record(0, ">", data, time.monotonic())
                c.sock.sendall(data)
            except BaseException as exc:
                s.link_down(0, exc)
                raise
            c.bytes_tx += len(data)

    # -- internals ----------------------------------------------------------

    def _record(self, conn: int, direction: str, data: bytes, t: float) -> None:
        recorder = self._recorder
        if recorder is not None:
            with self._record_lock:
                recorder(conn, direction, data, t)

    def _listener_failed(self, exc: BaseException, rec: LinkEvent) -> None:
        with self._listener_lock:
            self._unreported_listener_errors.append(exc)
        log.error(
            "goliath link: the listener raised while handling %r from connection %s",
            rec.event,
            rec.conn,
            exc_info=exc,
        )
