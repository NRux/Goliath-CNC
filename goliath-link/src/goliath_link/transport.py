# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""TCP transport for the Goliath control link.

A :class:`GoliathTransport` owns a socket to the robot (``10.0.0.1:80`` on the
robot's own Wi-Fi AP, or its DHCP address on a shared network), runs a background
reader that turns incoming bytes into messages via :class:`~goliath_link.framing.StreamFramer`,
and exposes:

* :meth:`send` — write a command string (ASCII), optionally paced;
* an inbound :class:`queue.Queue` of parsed events, and/or a listener callback.

The socket is injectable (``opener``) so the whole thing can be driven by a fake
in tests without a network. There is **no** automatic reconnect here by design
(the spec notes the vendor layer had none); build reconnect policy on top.

Fail loudly: a closed/errored socket makes the reader put a
:class:`TransportClosed` marker on the queue and stop, rather than silently
hanging.
"""

from __future__ import annotations

import queue
import socket
import threading
import warnings
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from .framing import StreamFramer

DEFAULT_PORT = 80
DEFAULT_AP_ADDRESS = "10.0.0.1"


class SocketLike(Protocol):
    def sendall(self, data: bytes) -> None: ...
    def recv(self, bufsize: int) -> bytes: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class TransportClosed:
    """Placed on the inbound queue when the reader stops. ``error`` is None on a clean close."""

    error: Optional[BaseException] = None


def _default_opener(host: str, port: int, timeout: float) -> SocketLike:
    sock = socket.create_connection((host, port), timeout=timeout)
    # The robot's Wi-Fi chip (WINC1500) hands each TCP segment to the STM32
    # command parser as one unit, so two small writes coalesced into one
    # segment by Nagle's algorithm would arrive as one garbled command.
    # Disable Nagle so each send goes out promptly as its own segment.
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    # The connect timeout must not apply to the long-lived blocking reader.
    sock.settimeout(None)
    return sock


class GoliathTransport:
    def __init__(
        self,
        host: str = DEFAULT_AP_ADDRESS,
        port: int = DEFAULT_PORT,
        *,
        timeout: float = 10.0,
        opener: Callable[[str, int, float], SocketLike] = _default_opener,
        listener: Optional[Callable[[object], None]] = None,
        encoding: str = "ascii",
    ) -> None:
        self.host = host
        self.port = port
        self._timeout = timeout
        self._opener = opener
        self._listener = listener
        self._encoding = encoding
        self._sock: Optional[SocketLike] = None
        self._framer = StreamFramer()
        self._reader: Optional[threading.Thread] = None
        self._closed = threading.Event()
        self.incoming: "queue.Queue[object]" = queue.Queue()
        self._send_lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    def connect(self) -> None:
        if self._sock is not None:
            raise RuntimeError("already connected")
        self._sock = self._opener(self.host, self.port, self._timeout)
        self._closed.clear()
        self._reader = threading.Thread(
            target=self._read_loop, name="goliath-reader", daemon=True
        )
        self._reader.start()

    def close(self) -> None:
        self._closed.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            # close() alone does NOT wake a thread blocked in recv() on Linux;
            # shutdown() does, so the reader exits promptly instead of leaking.
            shutdown = getattr(sock, "shutdown", None)
            if shutdown is not None:
                try:
                    shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            try:
                sock.close()
            except OSError:
                pass
        reader, self._reader = self._reader, None
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2.0)
            if reader.is_alive():
                warnings.warn(
                    "goliath-link reader thread did not stop within 2 s of close()",
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
        return self._sock is not None and not self._closed.is_set()

    # -- send ---------------------------------------------------------------

    def send(self, command: str) -> None:
        """Send a command string. Raises if not connected."""
        self.send_bytes(command.encode(self._encoding))

    def send_bytes(self, payload: bytes) -> None:
        """Send raw bytes (e.g. a G-code upload block). Raises if not connected."""
        sock = self._sock
        if sock is None or self._closed.is_set():
            raise RuntimeError("transport is not connected")
        with self._send_lock:
            sock.sendall(payload)

    # -- receive ------------------------------------------------------------

    def _read_loop(self) -> None:
        sock = self._sock
        assert sock is not None
        error: Optional[BaseException] = None
        try:
            while not self._closed.is_set():
                chunk = sock.recv(8192)
                if not chunk:
                    break  # clean EOF
                for event in self._framer.feed(chunk):
                    self.incoming.put(event)
                    if self._listener is not None:
                        try:
                            self._listener(event)
                        except Exception:
                            # A listener bug must not kill the reader; surface
                            # nothing silently though — re-raise on close path.
                            pass
        except OSError as exc:
            if not self._closed.is_set():
                error = exc
        finally:
            self._closed.set()
            self.incoming.put(TransportClosed(error=error))
