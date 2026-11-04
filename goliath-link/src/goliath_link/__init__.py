# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""goliath-link — an open driver for the Goliath CNC robot.

Clean-room implementation of the Goliath control protocol, built from the
specification in ``docs/protocol/`` (not from the vendor's code). Apache-2.0.

Quick start::

    from goliath_link import GoliathTransport, GoliathClient

    with GoliathTransport("10.0.0.1") as t:   # robot AP; or its DHCP address
        c = GoliathClient(t)
        c.learn_firmware_version()
        c.set_rf_channels(101, 114)           # pair towers
        # ... let the robot run panel detection, then:
        c.set_panel_size(1200, 800)
        c.upload_gcode(open("job.nc").read())
        # watch position frames arrive via t.incoming / a listener

Nothing here has been tested against a real machine yet — air-cut first.
"""

from .client import (
    GoliathClient,
    GoliathError,
    GoliathTimeout,
    is_safety_error,
    parse_firmware_version,
)
from .framing import StreamFramer
from .gcode import clean_gcode
from .protocol import (
    Commands,
    Error,
    GlobalStatusFrame,
    Info,
    PositionFrame,
    TextMessage,
    TowerFrame,
    TowerStatus,
    Warning,
    parse_text_message,
)
from .transport import GoliathTransport, TransportClosed

__version__ = "0.1.0"

__all__ = [
    "GoliathTransport",
    "TransportClosed",
    "GoliathClient",
    "GoliathError",
    "GoliathTimeout",
    "is_safety_error",
    "parse_firmware_version",
    "StreamFramer",
    "clean_gcode",
    "Commands",
    "Info",
    "Error",
    "Warning",
    "TowerStatus",
    "TextMessage",
    "PositionFrame",
    "TowerFrame",
    "GlobalStatusFrame",
    "parse_text_message",
    "__version__",
]
