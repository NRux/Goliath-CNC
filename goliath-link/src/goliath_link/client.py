# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""High-level Goliath client: connect, send named commands, follow sequences.

This sits on top of :class:`~goliath_link.transport.GoliathTransport` and
:mod:`~goliath_link.protocol`. It is intentionally a thin, explicit layer — the
robot drives its own state machine (see ``docs/protocol/03-session-sequence.md``),
so this client mostly *sends configuration/actions* and *waits for* the INFO the
robot reports. It does not try to hide that model.

The one multi-step helper is :meth:`upload_gcode`, which reproduces the vendor's
upload sequence exactly (optional firmware-gated ``GCODE CONTINUE;0``, then
``GCODE UPLOAD;<cleaned-size>``, then the cleaned bytes in blocks, then wait for
``INFO;8``).

Timing constants default to the vendor's values; they are starting points to
confirm on hardware.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from . import protocol
from .gcode import clean_gcode
from .protocol import Commands, Error, Info, TextMessage
from .transport import GoliathTransport, TransportClosed

#: Default G-code upload block size (bytes). The vendor reads this from
#: ``Slingshot.ini`` (``GCODE_PACK_SIZE``); 1024 is the shipped value.
DEFAULT_BLOCK_SIZE = 1024
#: Default pause between upload blocks (seconds). Vendor used ~50 ms plus the
#: configurable GCODE_PACK_INTERVAL; 0.075 s is a safe starting point.
DEFAULT_BLOCK_INTERVAL = 0.075


class GoliathError(RuntimeError):
    """The robot reported an ``ERR;<n>`` relevant to the current operation."""

    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(message or f"robot error ERR;{code}")
        self.code = code


class GoliathTimeout(TimeoutError):
    """Expected a response (e.g. a specific INFO) that did not arrive in time."""


def parse_firmware_version(text: str) -> Optional[tuple[int, int, int]]:
    """Extract an ``xx.yy.zz`` firmware version from a string like ``GFW 01.02.00``."""
    import re

    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


class GoliathClient:
    def __init__(self, transport: GoliathTransport) -> None:
        self.t = transport
        self.firmware_version: Optional[tuple[int, int, int]] = None

    # -- plumbing -----------------------------------------------------------

    def send(self, command: str) -> None:
        self.t.send(command)

    def drain(self) -> list[object]:
        """Return every event currently queued (non-blocking)."""
        import queue

        out: list[object] = []
        while True:
            try:
                out.append(self.t.incoming.get_nowait())
            except queue.Empty:
                return out

    def wait_for(
        self,
        predicate: Callable[[object], bool],
        *,
        timeout: float = 30.0,
        raise_on_error: bool = True,
    ) -> object:
        """Block until a queued event satisfies ``predicate``; return it.

        Raises :class:`GoliathTimeout` on timeout, :class:`GoliathError` if an
        ``ERR`` arrives first (when ``raise_on_error``), and
        :class:`ConnectionError` if the transport closes.
        """
        import queue

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GoliathTimeout("timed out waiting for expected message")
            try:
                event = self.t.incoming.get(timeout=remaining)
            except queue.Empty:
                raise GoliathTimeout("timed out waiting for expected message")
            if isinstance(event, TransportClosed):
                raise ConnectionError(
                    f"transport closed while waiting: {event.error!r}"
                )
            if predicate(event):
                return event
            if (
                raise_on_error
                and isinstance(event, TextMessage)
                and event.kind == "ERR"
                and event.code is not None
            ):
                raise GoliathError(event.code)

    def wait_for_info(self, code: int, *, timeout: float = 30.0) -> TextMessage:
        """Wait for a specific ``INFO;<code>`` text message."""
        result = self.wait_for(
            lambda e: isinstance(e, TextMessage)
            and e.kind == "INFO"
            and e.code == int(code),
            timeout=timeout,
        )
        assert isinstance(result, TextMessage)
        return result

    # -- convenience commands ----------------------------------------------

    def request_app_info(self) -> None:
        self.send(Commands.info())

    def get_global_status(self) -> None:
        self.send(Commands.get_global_sys_status())

    def set_rf_channels(self, t1: int, t2: int) -> None:
        self.send(Commands.set_rf_channels(t1, t2))

    def set_panel_size(self, x_max: int, y_max: int) -> None:
        self.send(Commands.set_panel_size(x_max, y_max))

    def abort_work(self) -> None:
        self.send(Commands.abort_work())

    def enter_jog(self) -> None:
        self.send(Commands.enter_jog())

    def goto(self, x: float, y: float, z: float, angle: float) -> None:
        self.send(Commands.goto_target(x, y, z, angle))

    def learn_firmware_version(self, *, timeout: float = 10.0) -> Optional[tuple[int, int, int]]:
        """Ask for app info and record the firmware version from ``INFO;34``."""
        self.request_app_info()
        msg = self.wait_for_info(int(Info.APP_INFO), timeout=timeout)
        if msg.params:
            self.firmware_version = parse_firmware_version(msg.params[0])
        return self.firmware_version

    # -- the upload sequence ------------------------------------------------

    def upload_gcode(
        self,
        gcode: str | bytes,
        *,
        firmware_version: Optional[tuple[int, int, int]] = None,
        block_size: int = DEFAULT_BLOCK_SIZE,
        block_interval: float = DEFAULT_BLOCK_INTERVAL,
        done_timeout: float = 120.0,
    ) -> None:
        """Clean and upload a G-code program, then wait for ``INFO;8``.

        ``gcode`` may be raw text (it will be cleaned) or already-cleaned bytes.
        ``firmware_version`` gates the ``GCODE CONTINUE;0`` pre-step (sent for
        firmware >= 01.02.00); if None, the client's learned version is used, and
        if that is unknown the pre-step is sent (safe: it just tells the robot to
        discard any previously loaded program).
        """
        payload = gcode if isinstance(gcode, (bytes, bytearray)) else clean_gcode(gcode)
        payload = bytes(payload)

        fw = firmware_version or self.firmware_version
        if fw is None or fw >= (1, 2, 0):
            self.send(Commands.gcode_continue(False))  # GCODE CONTINUE;0
            time.sleep(0.25)

        self.send(Commands.gcode_upload(len(payload)))
        time.sleep(0.25)

        for i in range(0, len(payload), block_size):
            self.t.send_bytes(payload[i : i + block_size])
            if block_interval:
                time.sleep(block_interval)

        # Success = INFO;8; failure = ERR;0 (raised by wait_for).
        self.wait_for_info(int(Info.GCODE_UPLOAD_DONE), timeout=done_timeout)


def is_safety_error(event: object) -> bool:
    """True if ``event`` is an ``ERR`` the controller should treat as critical."""
    return (
        isinstance(event, TextMessage)
        and event.kind == "ERR"
        and event.code is not None
        and event.code in {int(e) for e in protocol.SAFETY_ERRORS}
    )
