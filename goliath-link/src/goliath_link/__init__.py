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
        report = c.upload_gcode(open("job.nc").read())
        # GFW 01.02.00 may never send INFO;8: there upload_gcode raises
        # UploadUnconfirmedError unless confirm="info8-or-info0" is passed.
        # Watch events with c.subscribe() / c.history(). Once a GoliathClient
        # is attached, its event pump consumes t.records (and so t.incoming):
        # do not read those queues yourself, or you take events from it. One
        # client per transport: close() it before attaching another.

Without a client, read ``t.records`` (each event tagged with the connection
it arrived on), ``t.incoming`` (bare events) or pass a ``listener``.

Nothing here has been tested against a real machine yet — air-cut first.
"""

from .client import (
    GoliathClient,
    GoliathError,
    GoliathTimeout,
    is_safety_error,
    parse_firmware_version,
)
from .framing import Discarded, StreamFramer
from .gcode import clean_gcode
from .policy import (
    NEVER_SEND,
    STRICT_COMMANDS,
    VENDOR_SENT,
    AllowedCommand,
    CommandPolicy,
    CommandRefusedError,
    PhasePolicy,
    PolicyDecision,
    PolicySwapRefusedError,
    bind,
)
from .protocol import (
    Commands,
    Error,
    GlobalStatusBlob,
    GlobalStatusFrame,
    Info,
    PositionFrame,
    TextMessage,
    TowerFrame,
    TowerStatus,
    Warning,
    parse_text_message,
)
from .transport import (
    GoliathTransport,
    Keepalive,
    LinkEvent,
    ListenerError,
    ListenerFailed,
    NotConnectedError,
    RecordsInUseError,
    SendBlockedError,
    TransportClosed,
)

__version__ = "0.1.0"

__all__ = [
    "GoliathTransport",
    "TransportClosed",
    "LinkEvent",
    "ListenerFailed",
    "ListenerError",
    "NotConnectedError",
    "SendBlockedError",
    "RecordsInUseError",
    "Keepalive",
    "Discarded",
    "GlobalStatusBlob",
    "GoliathClient",
    "GoliathError",
    "GoliathTimeout",
    "is_safety_error",
    "parse_firmware_version",
    "NEVER_SEND",
    "VENDOR_SENT",
    "STRICT_COMMANDS",
    "CommandPolicy",
    "PolicyDecision",
    "PhasePolicy",
    "AllowedCommand",
    "bind",
    "CommandRefusedError",
    "PolicySwapRefusedError",
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
