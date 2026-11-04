# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Server-Sent Events with Last-Event-ID replay (unit U01; the project design, section 8.2).

A reloaded or reopened tab restores its state from the server; closing a tab
never affects a job. Every snapshot is complete (goliath_run.types), so a tab
that missed some needs only the latest one; a gap is visible in ``seq``, a
stale or broken stream is never silent: the end of a session, a failing
source and a source that breaks its contract each send an event the page can
show, and a log line.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from run_ui_harness import Client, CollectingWriter, FakeSource, Harness, make_snapshot, parse_stream

from goliath_run.types import Banner, SessionSnapshot
from goliath_run.ui import serve
from goliath_run.ui import sse as E

LOGGER = "goliath_run.ui"


def _lease_free() -> dict[str, Any]:
    return {"epoch": 0, "held": False}


def _stream(source: FakeSource, writer: CollectingWriter, ids: list[str] | None = None, *,
            lease: Any = _lease_free, heartbeat_s: float = 30.0) -> E.EventStream:
    return E.EventStream(source, writer, last_event_ids=ids or [], lease_state=lease, heartbeat_s=heartbeat_s)


def _run(stream: E.EventStream) -> tuple[threading.Thread, list[str]]:
    result: list[str] = []
    t = threading.Thread(target=lambda: result.append(stream.run()), daemon=True)
    t.start()
    return t, result


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def test_encode_event_layout() -> None:
    assert E.encode_event("snapshot", {"a": "x\ny\r "}, event_id=5) == (
        b'id: 5\nevent: snapshot\ndata: {"a":"x\\ny\\r\\u2028"}\n\n'
    )
    assert E.encode_event("lease", {"epoch": 1, "held": True}) == b'event: lease\ndata: {"epoch":1,"held":true}\n\n'
    assert E.encode_retry(2000) == b"retry: 2000\n\n"
    for bad in ("", "Snapshot", "snap shot", "a\nb", "message:x"):
        with pytest.raises(ValueError):
            E.encode_event(bad, {})
    with pytest.raises(ValueError):
        E.encode_event("x", {"v": float("nan")})
    with pytest.raises((TypeError, ValueError)):
        E.encode_event("x", {}, event_id=-1)


_text = st.text(st.characters(codec="utf-8"), min_size=1, max_size=40)


@settings(max_examples=200, deadline=None)
@given(text=_text, seq=st.integers(min_value=0, max_value=2**40))
def test_snapshot_event_round_trips_any_text(text: str, seq: int) -> None:
    # Banner text comes from robots, files and operators: whatever it holds (newlines, CR, NUL, U+2028),
    # the event is one data line and the page reads back exactly the snapshot.
    if not text.strip():
        text = "x" + text
    snap = make_snapshot(seq, banners=(Banner(id="B", severity="amber", text=text, icon="caution"),))
    block = E.snapshot_event(snap)
    assert block.count(b"\n\n") == 1 and block.endswith(b"\n\n")
    assert block.count(b"\ndata: ") == 1 and b"\r" not in block
    (event,), _, _ = parse_stream(block)
    assert event.event == "snapshot" and event.id == str(seq)
    assert SessionSnapshot.from_json(json.loads(event.data)) == snap


@pytest.mark.parametrize("values,expected", [([], None), (["0"], 0), (["7"], 7), (["123456789012"], 123456789012)])
def test_parse_last_event_id(values: list[str], expected: int | None) -> None:
    assert E.parse_last_event_id(values) == expected


@pytest.mark.parametrize("values", [["abc"], ["-1"], ["1.5"], [" 3"], ["3 "], [""], ["٣"], ["+3"], ["0x10"],
                                    ["1", "2"], ["9" * 40]])
def test_parse_last_event_id_refuses_what_this_server_never_sent(values: list[str]) -> None:
    with pytest.raises(E.LastEventIdError):
        E.parse_last_event_id(values)


# ---------------------------------------------------------------------------
# The stream engine, with a collecting writer
# ---------------------------------------------------------------------------


def test_fresh_stream_sends_retry_lease_and_current_snapshot_then_live() -> None:
    source = FakeSource()
    source.publish()
    writer = CollectingWriter()
    stream = _stream(source, writer)
    t, result = _run(stream)
    try:
        assert [e.id for e in writer.wait_for(1)] == ["1"]
        events, retry, _ = parse_stream(writer.data)
        assert retry == E.RETRY_MS
        assert [e.event for e in events] == ["lease", "snapshot"]
        assert events[0].json() == {"epoch": 0, "held": False} and events[0].id is None
        source.publish()
        source.publish()
        assert [e.id for e in writer.wait_for(3)] == ["1", "2", "3"]
    finally:
        stream.close()
        t.join(5)
        source.end()
    assert result == ["closed"]


def test_replay_after_last_event_id() -> None:
    source = FakeSource()
    for _ in range(5):
        source.publish()
    writer = CollectingWriter()
    stream = _stream(source, writer, ["2"])
    t, _ = _run(stream)
    try:
        assert [e.id for e in writer.wait_for(3)] == ["3", "4", "5"]
        assert source.events_calls == [2]
        snaps = [SessionSnapshot.from_json(e.json()) for e in writer.events(kinds=("snapshot",))]
        assert [s.seq for s in snaps] == [3, 4, 5]
    finally:
        stream.close()
        t.join(5)
        source.end()


def test_replay_starts_with_the_oldest_kept_snapshot_and_shows_the_gap() -> None:
    source = FakeSource()
    for _ in range(6):
        source.publish()
    source.forget_through(3)
    writer = CollectingWriter()
    stream = _stream(source, writer, ["1"])
    t, _ = _run(stream)
    try:
        assert [e.id for e in writer.wait_for(3)] == ["4", "5", "6"]
    finally:
        stream.close()
        t.join(5)
        source.end()


@pytest.mark.parametrize("header,why", [(["999"], "ahead"), (["abc"], "not a number"), (["1", "2"], "two")])
def test_unusable_last_event_id_sends_the_current_snapshot_and_logs(
        header: list[str], why: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    source = FakeSource()
    for _ in range(3):
        source.publish()
    writer = CollectingWriter()
    stream = _stream(source, writer, header)
    t, _ = _run(stream)
    try:
        assert [e.id for e in writer.wait_for(1)] == ["3"]
    finally:
        stream.close()
        t.join(5)
        source.end()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Last-Event-ID" in m and "current snapshot" in m for m in warnings), warnings


def test_last_event_id_equal_to_current_replays_nothing() -> None:
    source = FakeSource()
    source.publish()
    writer = CollectingWriter()
    stream = _stream(source, writer, ["1"])
    t, _ = _run(stream)
    try:
        writer.wait_for(1, kinds=("lease",))
        time.sleep(0.1)
        assert writer.events(kinds=("snapshot",)) == []
        source.publish()
        assert [e.id for e in writer.wait_for(1)] == ["2"]
    finally:
        stream.close()
        t.join(5)
        source.end()


def test_end_of_session_is_an_event() -> None:
    source = FakeSource()
    writer = CollectingWriter()
    stream = _stream(source, writer)
    t, result = _run(stream)
    writer.wait_for(1)
    source.end()
    t.join(5)
    assert result == ["ended"]
    (end,) = writer.events(kinds=("end",))
    assert "ended" in end.json()["reason"]


def test_failing_source_is_an_event_and_a_log_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR, logger=LOGGER)
    source = FakeSource()
    source.fail_events = RuntimeError("driver crashed")
    writer = CollectingWriter()
    stream = _stream(source, writer)
    t, result = _run(stream)
    t.join(5)
    assert result == ["failed"]
    (err,) = writer.events(kinds=("server-error",))
    assert "driver crashed" in err.json()["error"]
    assert any("driver crashed" in (r.exc_text or "") + r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("bad", ["older", "same", "not a snapshot"])
def test_source_breaking_its_contract_ends_the_stream_visibly(bad: str, caplog: pytest.LogCaptureFixture) -> None:
    # A stream never shows an older state after a newer one: that would put a stale phase on screen.
    caplog.set_level(logging.ERROR, logger=LOGGER)
    source = FakeSource()
    for _ in range(3):
        source.publish()
    writer = CollectingWriter()
    stream = _stream(source, writer)
    t, result = _run(stream)
    writer.wait_for(1)
    source.inject({"older": make_snapshot(1), "same": make_snapshot(3), "not a snapshot": {"seq": 9}}[bad])
    t.join(5)
    source.end()
    assert result == ["failed"]
    assert [e.id for e in writer.events(kinds=("snapshot",))] == ["3"]
    (err,) = writer.events(kinds=("server-error",))
    assert err.json()["error"]
    assert caplog.records


def test_browser_going_away_ends_the_stream() -> None:
    source = FakeSource()
    writer = CollectingWriter(fail_after=3)
    stream = _stream(source, writer)
    t, result = _run(stream)
    for _ in range(3):
        source.publish()
    t.join(5)
    source.end()
    assert result == ["client-gone"]


def test_closed_browser_is_noticed_without_a_write() -> None:
    source = FakeSource()
    writer = CollectingWriter()
    gone = threading.Event()
    stream = E.EventStream(source, writer, last_event_ids=[], lease_state=_lease_free, heartbeat_s=30.0,
                           client_closed=gone.is_set)
    t, result = _run(stream)
    writer.wait_for(1)
    gone.set()
    t.join(5)
    source.end()
    assert result == ["client-gone"]


def test_close_is_prompt_while_the_source_blocks() -> None:
    source = FakeSource()
    writer = CollectingWriter()
    stream = _stream(source, writer)
    t, result = _run(stream)
    writer.wait_for(1)
    started = time.monotonic()
    stream.close()
    t.join(5)
    assert result == ["closed"] and time.monotonic() - started < 2
    source.end()


def test_heartbeat_when_nothing_happens() -> None:
    source = FakeSource()
    writer = CollectingWriter()
    stream = _stream(source, writer, heartbeat_s=0.1)
    t, _ = _run(stream)
    try:
        assert len(writer.wait_for(3, kinds=("heartbeat",), timeout=5)) >= 3
    finally:
        stream.close()
        t.join(5)
        source.end()


def test_heartbeat_only_after_a_quiet_interval() -> None:
    # A heartbeat says "nothing else for HEARTBEAT_S"; while snapshots flow, none is sent. Checked on the
    # write times: every heartbeat follows at least heartbeat_s of silence (load only lengthens the gaps).
    heartbeat_s = 0.5
    source = FakeSource()
    writer = CollectingWriter()
    stream = _stream(source, writer, heartbeat_s=heartbeat_s)
    t, _ = _run(stream)
    try:
        writer.wait_for(1)
        for _ in range(30):  # 1.5 s of snapshots, 0.05 s apart: three heartbeat intervals
            time.sleep(0.05)
            source.publish()
        writer.wait_for(31)
        writer.wait_for(1, kinds=("heartbeat",), timeout=5)  # and once it is quiet, a heartbeat comes
    finally:
        stream.close()
        t.join(5)
        source.end()
    chunks = list(writer.chunks)
    beats = [i for i, (_, chunk) in enumerate(chunks) if b"event: heartbeat\n" in chunk]
    assert beats
    for i in beats:
        gap = chunks[i][0] - chunks[i - 1][0]
        assert gap >= heartbeat_s - 0.05, "heartbeat %.3f s after the previous event" % gap


def test_unexpected_error_ends_the_stream_visibly(caplog: pytest.LogCaptureFixture) -> None:
    # Whatever goes wrong inside the stream, the page is told (server-error) and the log has the traceback;
    # the stream never just stops.
    caplog.set_level(logging.ERROR, logger=LOGGER)
    source = FakeSource()
    writer = CollectingWriter()
    calls: list[int] = []

    def lease() -> dict[str, Any]:
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("lease state unreadable")
        return _lease_free()

    stream = _stream(source, writer, lease=lease)
    t, result = _run(stream)
    writer.wait_for(1)
    source.publish()
    t.join(5)
    source.end()
    assert result == ["failed"]
    (err,) = writer.events(kinds=("server-error",))
    assert "lease state unreadable" in err.json()["error"]
    assert any("lease state unreadable" in (r.exc_text or "") + r.getMessage() for r in caplog.records)


def test_lease_change_is_sent_when_poked() -> None:
    source = FakeSource()
    writer = CollectingWriter()
    lease = {"epoch": 0, "held": False}
    stream = _stream(source, writer, lease=lambda: dict(lease))
    t, _ = _run(stream)
    try:
        writer.wait_for(1, kinds=("lease",))
        lease.update(epoch=1, held=True)
        stream.poke()
        events = writer.wait_for(2, kinds=("lease",))
        assert events[-1].json() == {"epoch": 1, "held": True}
    finally:
        stream.close()
        t.join(5)
        source.end()


def test_slow_browser_loses_nothing() -> None:
    # Back-pressure: the stream reads the source only as fast as the browser takes events, so a source that
    # keeps its history delivers every snapshot, in order.
    source = FakeSource()
    writer = CollectingWriter(delay_s=0.0005)
    stream = _stream(source, writer)
    t, _ = _run(stream)
    try:
        for _ in range(300):
            source.publish()
        ids = [int(e.id) for e in writer.wait_for(301, timeout=30)]
        assert ids == list(range(301))
    finally:
        stream.close()
        t.join(5)
        source.end()


# ---------------------------------------------------------------------------
# Over real sockets
# ---------------------------------------------------------------------------


def test_sse_last_event_id_replay(ui: Harness) -> None:
    for _ in range(5):
        ui.source.publish()
    c = ui.browser()
    reader = c.events(last_event_id="2")
    try:
        assert reader.status == 200
        assert reader.header("Content-Type") == ["text/event-stream"]
        assert reader.header("Cache-Control") == ["no-store"]
        replayed = reader.snapshots(3)
        assert [e.id for e in replayed] == ["3", "4", "5"]
        assert [SessionSnapshot.from_json(e.json()).seq for e in replayed] == [3, 4, 5]
        assert reader.retry == E.RETRY_MS
        ui.source.publish()
        assert [e.id for e in reader.snapshots(1)] == ["6"]
    finally:
        reader.close()
    # A reopened tab (no Last-Event-ID) starts from the current snapshot, which is complete.
    reader = c.events()
    try:
        (first,) = reader.snapshots(1)
        assert first.id == "6" and first.json() == ui.source.snapshot().to_json()
        ui.source.publish()
        assert [e.id for e in reader.snapshots(1)] == ["7"]
    finally:
        reader.close()


def test_closing_a_tab_affects_nothing(ui: Harness) -> None:
    c = ui.browser()
    reader = c.events()
    reader.snapshots(1)
    reader.close()
    ui.source.publish()
    assert c.request("GET", "/api/snapshot").json()["seq"] == 1
    assert ui.sink.intents == []


def test_end_of_session_reaches_the_tab(ui: Harness) -> None:
    c = ui.browser()
    reader = c.events()
    try:
        reader.snapshots(1)
        ui.source.end()
        e = reader.next(skip=("heartbeat", "lease"))
        assert e.event == "end"
        assert reader.at_eof()
    finally:
        reader.close()


def test_failing_source_reaches_the_tab(ui: Harness) -> None:
    ui.source.fail_events = RuntimeError("driver crashed")
    reader = ui.browser().events()
    try:
        e = reader.next(skip=("heartbeat", "lease", "snapshot"))
        assert e.event == "server-error" and "driver crashed" in e.json()["error"]
    finally:
        reader.close()


def test_failing_snapshot_read_refuses_the_stream(ui: Harness) -> None:
    ui.source.fail_snapshot = RuntimeError("driver gone")
    reader = ui.browser().events()
    try:
        assert reader.status == 500
        assert b"driver gone" in reader.body()
    finally:
        reader.close()


def test_heartbeats_over_the_wire(source: FakeSource, sink: Any) -> None:
    with serve(source, sink, heartbeat_s=0.1) as server:
        c = Client(server.port, server.url)
        c.sign_in()
        reader = c.events()
        try:
            e = reader.next(timeout=5, skip=("lease", "snapshot"))
            assert e.event == "heartbeat"
        finally:
            reader.close()


def test_event_stream_limit(ui: Harness) -> None:
    c = ui.browser()
    readers = []
    try:
        for _ in range(E.MAX_STREAMS):
            r = c.events()
            readers.append(r)
            assert r.status == 200
            r.snapshots(1)
        extra = c.events()
        readers.append(extra)
        assert extra.status == 503
        assert b"open tabs" in extra.body()
        # A closed tab frees its slot soon, without waiting for the next heartbeat to fail.
        readers.pop().close()
        readers.pop().close()
        deadline = time.monotonic() + 5
        while True:
            again = c.events()
            readers.append(again)
            if again.status == 200 or time.monotonic() > deadline:
                break
            time.sleep(0.1)
        assert again.status == 200
    finally:
        for r in readers:
            r.close()
