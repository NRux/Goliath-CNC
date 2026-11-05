# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The presenters reach the page (unit U02; the project design, sections 8.1-8.4).

The page renders what the presenters say, never its own reading of the
snapshot: after every snapshot the event stream carries a ``view`` event (the
top bar, the banner model and the disclosure chip of that snapshot), and the
screens of this unit are served as presenter views at ``/api/screen/<name>``.
These tests run a real server on a loopback socket.
"""
from __future__ import annotations

import logging
from typing import Any

import pytest
from run_ui_harness import Client, FakeSource, Harness, RecordingSink

from goliath_run.types import Phase, SessionSnapshot
from goliath_run.ui import presenters as P
from goliath_run.ui import serve
from goliath_run.ui import server as U


def _view_after(reader: Any, seq: int) -> dict:
    """The next ``view`` event; it must follow the snapshot ``seq``."""
    snapshot = reader.next(skip=("heartbeat", "lease"))
    assert snapshot.event == "snapshot" and snapshot.id == str(seq), snapshot
    view = reader.next(skip=("heartbeat", "lease"))
    assert view.event == "view" and view.id is None, view
    return view.json()


def test_view_event_follows_each_snapshot(ui: Harness) -> None:
    reader = ui.browser().events()
    try:
        view = _view_after(reader, 0)
        assert view == P.session_view(ui.source.snapshot())
        assert view["top_bar"]["stop"]["text"] == "NO SOFTWARE STOP: PHYSICAL STOP ONLY"
        assert view["top_bar"]["firmware"]["text"] == "FIRMWARE UNKNOWN"
        ui.source.publish(phase=Phase.MONITORING_UNVERIFIED, may_be_moving=True, firmware="GFW 01.02.00",
                          firmware_source="INFO;34")
        view = _view_after(reader, 1)
        assert view["seq"] == 1
        assert view["top_bar"]["state"]["text"] == "MONITORING UNVERIFIED - MAY BE MOVING"
        assert [b["id"] for b in view["banners"]][:2] == ["PHASE:MONITORING_UNVERIFIED", "NO_SOFTWARE_STOP"]
    finally:
        reader.close()


def test_replayed_snapshots_end_with_the_view_of_the_latest(ui: Harness) -> None:
    for _ in range(4):
        ui.source.publish()
    reader = ui.browser().events(last_event_id="1")
    try:
        snapshots: list[str] = []
        while True:
            e = reader.next(skip=("heartbeat", "lease"))
            if e.event == "snapshot":
                snapshots.append(e.id or "")
                continue
            # A view always describes the snapshot sent just before it (the newest of its batch).
            assert e.event == "view" and snapshots, e
            assert str(e.json()["seq"]) == snapshots[-1]
            if e.json()["seq"] == 4:
                break
        # The three replayed snapshots arrive in order, and the last view is that of the newest.
        assert snapshots == ["2", "3", "4"]
    finally:
        reader.close()


def test_a_failing_presenter_stops_the_stream_visibly(source: FakeSource, sink: RecordingSink) -> None:
    def broken(snapshot: SessionSnapshot) -> dict:
        raise P.PresenterError("the session sent banner PHASE:RUNNING, which is the presenters' to add")

    with serve(source, sink, present=broken) as server:
        c = Client(server.port, server.url)
        c.sign_in()
        reader = c.events()
        try:
            e = reader.next(skip=("heartbeat", "lease", "snapshot"))
            assert e.event == "server-error" and "PHASE:RUNNING" in e.json()["error"]
        finally:
            reader.close()


def test_screens_serve_presenter_views(ui: Harness) -> None:
    c = ui.browser()
    for name in P.SCREENS:
        r = c.request("GET", "/api/screen/%s" % name)
        assert r.status == 200, (name, r.body)
        view = r.json()
        assert view["screen"] == name and view["sections"]
    machine = c.request("GET", "/api/screen/machine").json()
    assert machine == P.machine_view(ui.source.snapshot())
    ladder = c.request("GET", "/api/screen/ladder").json()
    assert ladder == P.ladder_view(None, None)


def test_unknown_screen_is_404_json(ui: Harness) -> None:
    c = ui.browser()
    for path in ("/api/screen/prepare", "/api/screen/", "/api/screen/Machine", "/api/screen/../snapshot",
                 "/api/screen/machine/x"):
        r = c.request("GET", path)
        assert r.status == 404, (path, r.status)
        assert "error" in r.json()


def test_screens_need_the_session_cookie_and_the_host(ui: Harness) -> None:
    c = ui.browser()
    assert c.request("GET", "/api/screen/machine", cookie=None).status == 401
    assert c.request("GET", "/api/screen/machine", host="evil.example:%d" % ui.port).status == 403
    assert c.request("GET", "/api/screen/machine", fetch_site="cross-site").status == 403
    assert c.request("POST", "/api/screen/machine", body=b"{}").status == 405


def test_custom_screen_providers_and_failures(source: FakeSource, sink: RecordingSink,
                                              caplog: pytest.LogCaptureFixture) -> None:
    def broken() -> dict:
        raise RuntimeError("fact store unreadable")

    screens = {"machine": lambda: {"screen": "machine", "title": "Machine", "sections": []}, "ladder": broken}
    with serve(source, sink, screens=screens) as server:
        c = Client(server.port, server.url)
        c.sign_in()
        assert c.request("GET", "/api/screen/machine").json()["title"] == "Machine"
        with caplog.at_level(logging.ERROR, logger="goliath_run.ui"):
            r = c.request("GET", "/api/screen/ladder")
        assert r.status == 500 and "fact store unreadable" in r.json()["error"]
        assert any("ladder" in rec.getMessage() for rec in caplog.records)
        # A screen the providers do not name is not served at all.
        assert c.request("GET", "/api/screen/records").status == 404


def test_screen_names_are_checked_when_serving() -> None:
    source, sink = FakeSource(), RecordingSink()
    try:
        with pytest.raises(ValueError, match="screen"):
            serve(source, sink, screens={"Bad Name": lambda: {}})
        with pytest.raises(TypeError, match="callable"):
            serve(source, sink, screens={"machine": "not callable"})  # type: ignore[dict-item]
        with pytest.raises(TypeError, match="present"):
            serve(source, sink, present="not callable")  # type: ignore[arg-type]
    finally:
        source.end()


def test_view_route_is_documented() -> None:
    assert "/api/screen/<name>" in (U.__doc__ or "")
