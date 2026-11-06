# SPDX-License-Identifier: Apache-2.0
"""A scriptable fake robot on 127.0.0.1, for the probe tests that need exact timing.

The simulator (goliath_link.sim) answers like GFW 01.02.00. These tests need
a robot that does odd things at chosen moments instead: text on S1 that keeps
the transport's send hold-back busy while an identity line arrives on S2, an
identity line with no terminator, a connection reset during the handshake.
Like the firmware, it gives each accepted connection the first free of six
slots. Scripts are two callbacks, each run on a thread of its own:

* ``on_accept(robot, slot, session)``: a connection took ``slot``; ``session``
  counts the connections that took slot 0 so far (1 for the probe's first
  session, 4 for the sweep after three terminator tests);
* ``on_receive(robot, slot, data, session)``: ``data`` arrived on ``slot``.

Everything received is kept with its ``time.monotonic()`` (the probe runs in
the same process, so the times compare). The serial number and network
names the tests use are synthetic (old_firmware_robot.py).
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Callable, List, Optional, Tuple

HOST = "127.0.0.1"
#: The 01.02.00 identity line of the simulator (serial: the simulator's placeholder).
IDENTITY_01_02_00 = b"INFO;34;GFW 01.02.00   ;1;2;000000000001\n"


def block(message: bytes) -> bytes:
    """``message`` NUL-padded to the robot's 512-byte block."""
    return message + b"\x00" * (512 - len(message)) if len(message) < 512 else message


class ScriptedRobot:
    def __init__(self, on_accept: Optional[Callable] = None,
                 on_receive: Optional[Callable] = None) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((HOST, 0))
        self._server.listen(16)
        self.port = self._server.getsockname()[1]
        self._lock = threading.Lock()
        self._slots: List[Optional[socket.socket]] = [None] * 6
        self.session = 0
        self.accepts = 0
        #: (time, slot, session, bytes) for every chunk received.
        self.received: List[Tuple[float, int, int, bytes]] = []
        #: (time, slot, bytes) for every message sent, the time taken just before the write.
        self.sent: List[Tuple[float, int, bytes]] = []
        self.marks: dict = {}
        self.errors: List[BaseException] = []
        self._on_accept = on_accept
        self._on_receive = on_receive
        self._stopped = False
        threading.Thread(target=self._accept_loop, daemon=True).start()

    # -- the server ----------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stopped:
            try:
                sock, _ = self._server.accept()
            except OSError:
                return
            with self._lock:
                try:
                    slot = self._slots.index(None)
                except ValueError:
                    sock.close()
                    continue
                self._slots[slot] = sock
                self.accepts += 1
                if slot == 0:
                    self.session += 1
                session = self.session
            threading.Thread(target=self._serve, args=(sock, slot, session), daemon=True).start()
            if self._on_accept is not None:
                self._spawn(self._on_accept, slot, session)

    def _spawn(self, fn: Callable, *args: object) -> None:
        def run() -> None:
            try:
                fn(self, *args)
            except BaseException as exc:  # noqa: BLE001 - reported by the test
                self.errors.append(exc)

        threading.Thread(target=run, daemon=True).start()

    def _serve(self, sock: socket.socket, slot: int, session: int) -> None:
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                with self._lock:
                    self.received.append((time.monotonic(), slot, session, data))
                if self._on_receive is not None:
                    self._spawn(self._on_receive, slot, data, session)
        except OSError:
            pass
        finally:
            with self._lock:
                if self._slots[slot] is sock:
                    self._slots[slot] = None
            try:
                sock.close()
            except OSError:
                pass

    # -- what a script does ----------------------------------------------------

    def send(self, slot: int, message: bytes, *, padded: bool = True) -> Optional[float]:
        """Send ``message`` (in a 512-byte block unless ``padded`` is False); its time, or None."""
        with self._lock:
            sock = self._slots[slot]
        if sock is None:
            return None
        wire = block(message) if padded else message
        t = time.monotonic()
        try:
            sock.sendall(wire)
        except OSError:
            return None
        with self._lock:
            self.sent.append((t, slot, message))
        return t

    def reset(self, slot: int) -> None:
        """Drop the connection in ``slot`` (shut down, then closed with a TCP reset)."""
        with self._lock:
            sock = self._slots[slot]
        if sock is None:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            # Wakes the reader thread, which then closes the socket (a reset: linger 0).
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    @staticmethod
    def sleep_until(t: float) -> None:
        delay = t - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def commands(self) -> List[bytes]:
        """Every chunk received on slot 0, in order."""
        with self._lock:
            return [d for _t, slot, _s, d in self.received if slot == 0]

    def received_after(self, t: float) -> List[Tuple[float, int, bytes]]:
        with self._lock:
            return [(rt - t, slot, d) for rt, slot, _s, d in self.received if rt > t]

    def stop(self) -> None:
        self._stopped = True
        try:
            self._server.close()
        except OSError:
            pass
        with self._lock:
            socks = [s for s in self._slots if s is not None]
        for sock in socks:
            try:
                sock.close()
            except OSError:
                pass
