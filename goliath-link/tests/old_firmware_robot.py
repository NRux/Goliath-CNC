# SPDX-License-Identifier: Apache-2.0
"""A fake robot on firmware older than 01.02.00, for the probe's firmware-gate and privacy tests.

Modelled on what rung R1 recorded from a real robot on GFW 01.00.13
(probe-results/README.md and the scrubbed capture there): when a connection
takes slot 1 (S2), the robot pushes an ``INFO;31`` Wi-Fi scan (``START``, one
line per nearby network, ``END``) with the ``INFO;34`` identity line among
it, each message in its own 512-byte NUL-padded block. The identity line has
the dated form ``INFO;34;GFW 01.00.13 +b \\x01\\x02Jul  2 2021;2;1;<24 hex>``.

Everything else (the reply to ``INFO``, the command table) is the 01.02.00
simulator's: the tests here are about what the probe sends and prints, not
about how that firmware answers. The serial number and the network names are
synthetic; none belongs to a robot or a real network.
"""
from __future__ import annotations

from typing import List, Sequence

from goliath_link.sim import SimulatedGoliath
from goliath_link.transport import _default_opener

HOST = "127.0.0.1"

#: A synthetic 24-hex serial number in the 01.00.13 identity format.
SERIAL = "0123456789ABCDEF01234567"
#: The dated identity line of GFW 01.00.13, with the synthetic serial.
IDENTITY_01_00_13 = b"INFO;34;GFW 01.00.13 +b \x01\x02Jul  2 2021;2;1;" + SERIAL.encode("ascii") + b"\n"
#: Synthetic network names: plain, short, empty (a hidden network), with a ';', and
#: with non-ASCII (UTF-8) bytes, which the probe decodes as U+FFFD.
SSIDS: Sequence[bytes] = (b"TestNet-Alpha", b"Cafe_5G", b"", b"abc", b"Test;Guest", b"Test\xc3\xa9-Net")
#: The names as the probe's decoder shows them (ASCII, every other byte U+FFFD).
SSID_TEXTS: Sequence[str] = tuple(s.decode("ascii", "replace") for s in SSIDS if s)


class OldFirmwareRobot(SimulatedGoliath):
    """The 01.02.00 simulator with a 01.00.13-style identity and connect-time Wi-Fi scan."""

    def __init__(self, *, identity: bytes = IDENTITY_01_00_13, ssids: Sequence[bytes] = SSIDS,
                 push_on_connect: bool = True, **kw: object) -> None:
        kw.setdefault("profile", "gfw-01.02.00")
        kw.setdefault("connect_replay", push_on_connect)
        super().__init__(HOST, 0, **kw)  # type: ignore[arg-type]
        self.identity = identity
        self.scan = tuple(ssids)

    def _identity_line(self) -> bytes:
        return self.identity

    def _connect_replay(self) -> None:
        with self._cmd_lock:
            self._slot_message(1, b"INFO;31;START\n")
            self._slot_message(1, self._identity_line())
            for ssid in self.scan:
                self._slot_message(1, b"INFO;31;" + ssid + b"\n")
            self._slot_message(1, b"INFO;31;END\n")


class _Sock:
    def __init__(self, inner, opener: "RecordingOpener") -> None:
        self._inner = inner
        self._opener = opener

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def sendall(self, data) -> None:
        self._opener.payloads.append(bytes(data))
        self._inner.sendall(data)


class RecordingOpener:
    """An opener for ``run_probe`` that records every payload the probe writes to a socket."""

    def __init__(self) -> None:
        self.opens = 0
        self.payloads: List[bytes] = []

    def __call__(self, host: str, port: int, timeout: float):
        assert host == HOST, "tests must only ever connect to 127.0.0.1"
        self.opens += 1
        return _Sock(_default_opener(host, port, timeout), self)
