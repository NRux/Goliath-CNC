# SPDX-License-Identifier: Apache-2.0
"""Client/transport tests driven by a fake socket (no network, no robot)."""
import queue
from typing import Callable, Optional

import pytest

from goliath_link import client as client_mod
from goliath_link.client import GoliathClient, GoliathError, is_safety_error
from goliath_link.protocol import TextMessage
from goliath_link.transport import GoliathTransport

_EOF = object()


class FakeSocket:
    """A socket-like object. ``on_send`` may script robot replies via push()."""

    def __init__(self, on_send: Optional[Callable[[bytes, "FakeSocket"], None]] = None):
        self._to_client: "queue.Queue[object]" = queue.Queue()
        self.sent: list[bytes] = []
        self._on_send = on_send

    # robot -> client
    def push(self, data: bytes) -> None:
        self._to_client.put(data)

    def recv(self, bufsize: int) -> bytes:
        item = self._to_client.get()
        if item is _EOF:
            return b""
        return item  # type: ignore[return-value]

    # client -> robot
    def sendall(self, data: bytes) -> None:
        self.sent.append(bytes(data))
        if self._on_send is not None:
            self._on_send(bytes(data), self)

    def close(self) -> None:
        self._to_client.put(_EOF)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_: None)


def _transport_with(fake: FakeSocket) -> GoliathTransport:
    return GoliathTransport(opener=lambda h, p, t: fake)


def test_upload_sequence_sends_continue_header_payload_and_waits_done():
    def on_send(data, sock):
        if b"GCODE UPLOAD" in data:
            sock.push(b"INFO;8\n")

    fake = FakeSocket(on_send)
    with _transport_with(fake) as t:
        c = GoliathClient(t)
        c.upload_gcode(
            "G01 X1\nG01 X2\n", firmware_version=(1, 2, 0), block_interval=0
        )

    # CONTINUE;0 first (firmware >= 1.2.0), then UPLOAD;<size>, then the payload.
    assert fake.sent[0] == b"GCODE CONTINUE;0"
    assert fake.sent[1] == b"GCODE UPLOAD;16"  # len of cleaned "G01 X1\r\nG01 X2\r\n"
    assert fake.sent[2] == b"G01 X1\r\nG01 X2\r\n"


def test_upload_skips_continue_on_old_firmware():
    def on_send(data, sock):
        if b"GCODE UPLOAD" in data:
            sock.push(b"INFO;8\n")

    fake = FakeSocket(on_send)
    with _transport_with(fake) as t:
        GoliathClient(t).upload_gcode(
            "G00 X0\n", firmware_version=(1, 0, 14), block_interval=0
        )
    assert not any(b"GCODE CONTINUE" in s for s in fake.sent)
    assert fake.sent[0].startswith(b"GCODE UPLOAD;")


def test_wait_for_info_raises_on_error_frame():
    fake = FakeSocket()
    with _transport_with(fake) as t:
        c = GoliathClient(t)
        fake.push(b"ERR;9\n")
        with pytest.raises(GoliathError) as ei:
            c.wait_for_info(8, timeout=2.0)
        assert ei.value.code == 9


def test_learn_firmware_version():
    def on_send(data, sock):
        if data == b"INFO":
            sock.push(b"INFO;34;GFW 01.02.00;HWrevA;SWrevB;SN123\n")

    fake = FakeSocket(on_send)
    with _transport_with(fake) as t:
        c = GoliathClient(t)
        assert c.learn_firmware_version(timeout=2.0) == (1, 2, 0)
        assert c.firmware_version == (1, 2, 0)


def test_transport_closed_surfaces_as_connection_error():
    fake = FakeSocket()
    t = _transport_with(fake)
    t.connect()
    try:
        fake.close()  # robot drops the link
        c = GoliathClient(t)
        with pytest.raises(ConnectionError):
            c.wait_for_info(8, timeout=2.0)
    finally:
        t.close()


def test_safety_error_classification():
    assert is_safety_error(TextMessage(kind="ERR", raw="ERR;16", code=16))  # out of panel
    assert is_safety_error(TextMessage(kind="ERR", raw="ERR;2", code=2))  # cage opened
    assert not is_safety_error(TextMessage(kind="ERR", raw="ERR;1", code=1))  # cmd not exec
    assert not is_safety_error(TextMessage(kind="INFO", raw="INFO;8", code=8))


def test_position_frames_arrive_on_incoming_queue():
    import struct

    buf = bytearray(22)
    buf[0] = 0x7B
    buf[21] = 0x7D
    struct.pack_into("<ffff", buf, 1, 5.0, 6.0, 0.0, 90.0)
    struct.pack_into("<i", buf, 17, 12)

    fake = FakeSocket()
    with _transport_with(fake) as t:
        fake.push(bytes(buf))
        from goliath_link.protocol import PositionFrame

        c = GoliathClient(t)
        ev = c.wait_for(lambda e: isinstance(e, PositionFrame), timeout=2.0)
        assert ev.gcode_line == 12 and round(ev.angle) == 90
