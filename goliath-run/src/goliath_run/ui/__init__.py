# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The local web interface of goliath-run (the project design, section 8).

:func:`serve` starts the server on ``127.0.0.1`` (unit U01: :mod:`.server`,
:mod:`.security`, :mod:`.sse`); the browser is a disposable view of a
:class:`~goliath_run.types.SnapshotSource` that submits intents to an
:class:`~goliath_run.types.IntentSink`. The files the browser loads are in
``static/``, read from the package, with no external URL. What the page shows
(top bar, banners, disclosure chip and the screens) is built by
:mod:`.presenters` (unit U02), pure functions tested directly; the page only
draws their output.
"""
from goliath_run.ui.server import UIServer, serve

__all__ = ["UIServer", "serve"]
