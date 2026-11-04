# SPDX-License-Identifier: Apache-2.0
"""End-to-end transport tests over real localhost TCP against the simulator.

These exercise the real default socket opener and the reader threads —
everything except the actual robot. Passing them says nothing about the
firmware. The GoliathClient end-to-end tests are in ``test_client_e2e.py`` and
the probe's in ``test_probe_e2e.py``.
"""
import time

import pytest

from goliath_link import GoliathTransport
from goliath_link.sim import SimulatedGoliath


@pytest.fixture
def sim():
    s = SimulatedGoliath(profile="gfw-01.02.00", work_line_delay=0.002)
    s.start()
    yield s
    s.stop()


def test_close_wakes_blocked_reader_promptly(sim):
    # Regression: close() alone does not wake a thread blocked in recv() on
    # Linux, so close() used to wait out its 2 s join and leak the reader.
    t = GoliathTransport("127.0.0.1", sim.port, timeout=2.0)
    t.connect()
    reader = t._reader
    time.sleep(0.1)
    t0 = time.monotonic()
    t.close()
    assert time.monotonic() - t0 < 1.0
    assert reader is not None and not reader.is_alive()
