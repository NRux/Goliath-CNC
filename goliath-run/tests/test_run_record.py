# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The session record, the black box (unit R02; the project design, section 7.9; red-team C-18).

Every byte per connection through the transport's recorder hook, decoded
events, state transitions and operator answers, under a size and retention
policy; and the free-disk check made before arming.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import shutil
import stat
import time
import typing
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from goliath_link.framing import Discarded
from goliath_link.protocol import PositionFrame, TextMessage, parse_text_message
from goliath_link.sim import SimulatedGoliath
from goliath_link.transport import GoliathTransport, LinkEvent, TransportClosed
from goliath_prep.core.disclosure import Disclosure
from goliath_run import record as R
from goliath_run.record import (
    OUTCOMES,
    RECORDER_RATE_BYTES_PER_HOUR,
    RECORDER_RESERVE_BYTES,
    DiskCheck,
    JobRecord,
    RecordClosedError,
    RecorderPolicy,
    free_disk_ok,
    prune_sessions,
)
from goliath_run.types import Phase

MiB = 1024 * 1024
POLICY = RecorderPolicy(max_session_mb=16, keep_sessions=3, source="test policy")


class Clock:
    """A wall clock the test moves by hand (UTC)."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _record(root: Path, job_id: str = "job-0001", **kwargs: Any) -> JobRecord:
    kwargs.setdefault("policy", POLICY)
    return JobRecord.create(root, job_id, **kwargs)


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------------------
# The recorder hook
# ---------------------------------------------------------------------------


def test_recorder_signature_matches_transport(tmp_path: Path) -> None:
    # The hook goliath-link calls: recorder(conn: int, direction: str, data: bytes, t: float) -> None.
    hints = typing.get_type_hints(GoliathTransport.__init__)
    expected = hints["recorder"]
    (callable_type,) = [a for a in typing.get_args(expected) if a is not type(None)]
    params, ret = typing.get_args(callable_type)
    assert params == [int, str, bytes, float] and ret is type(None)

    rec = _record(tmp_path)
    sig = inspect.signature(rec.recorder)
    assert list(sig.parameters) == ["conn", "direction", "data", "t"]
    mine = typing.get_type_hints(JobRecord.recorder)
    assert [mine[n] for n in sig.parameters] == params
    assert mine["return"] is type(None)
    rec.finish("ABORTED_BEFORE_UPLOAD", ["test"])


def test_recorder_captures_a_real_session_with_the_simulator(tmp_path: Path) -> None:
    # Real bytes through a real GoliathTransport into the record.
    rec = _record(tmp_path)
    with SimulatedGoliath(profile="gfw-01.02.00") as sim:
        transport = GoliathTransport(sim.host, sim.port, recorder=rec.recorder, slot_release_wait=0.0,
                                     connect_gaps=(0.0, 0.0), settle=0.2)
        transport.connect()
        try:
            transport.send("INFO")  # read-only on 01.02.00: formats INFO;34 from constants
            deadline = time.monotonic() + 10
            seen: list[LinkEvent] = []
            while time.monotonic() < deadline:
                try:
                    item = transport.records.get(timeout=0.2)
                except Exception:  # queue.Empty: keep polling until the deadline
                    continue
                seen.append(item)
                rec.event(item)
                if isinstance(item.event, TextMessage) and item.event.code == 34:
                    break
        finally:
            transport.close()
    rec.finish("ABORTED_BEFORE_UPLOAD", ["test session"])

    s1 = (rec.dir / "wire" / "S1.bin").read_bytes()
    s2 = (rec.dir / "wire" / "S2.bin").read_bytes()
    assert s1.startswith(b"INFO")  # what was sent, NUL-padded to 512 bytes by the client layer or not
    assert b"INFO;34;" in s2
    index = _lines(rec.dir / "wire" / "index.jsonl")
    sent = [e for e in index if e.get("dir") == ">"]
    assert sent and sent[0]["conn"] == 0 and sent[0]["off"] == 0
    # The index locates every chunk in its connection's file.
    for entry in (e for e in index if "conn" in e):
        blob = (rec.dir / "wire" / ("S%d.bin" % (entry["conn"] + 1))).read_bytes()
        assert entry["off"] + entry["len"] <= len(blob)
        assert isinstance(entry["t"], float) and entry["wall"].endswith("Z")
    events = _lines(rec.dir / "events.jsonl")
    assert any(e["type"] == "TextMessage" and e["fields"]["code"] == 34 for e in events)
    # The identity line's serial is noted as private (redacted on export).
    private = json.loads((rec.dir / "private.json").read_text(encoding="utf-8"))
    assert private["serials"] == ["000000000001"]


def test_recorder_validates_and_routes_by_connection(tmp_path: Path) -> None:
    rec = _record(tmp_path)
    rec.recorder(0, ">", b"A" * 512, 1.0)
    rec.recorder(1, "<", b"INFO;0\n", 1.5)
    rec.recorder(2, "<", b"{pos}", 2.0)
    rec.recorder(0, "<", b"reply", 2.5)
    assert (rec.dir / "wire" / "S1.bin").read_bytes() == b"A" * 512 + b"reply"
    assert (rec.dir / "wire" / "S2.bin").read_bytes() == b"INFO;0\n"
    assert (rec.dir / "wire" / "S3.bin").read_bytes() == b"{pos}"
    index = _lines(rec.dir / "wire" / "index.jsonl")
    assert [(e["conn"], e["dir"], e["off"], e["len"]) for e in index] == [
        (0, ">", 0, 512), (1, "<", 0, 7), (2, "<", 0, 5), (0, "<", 512, 5)]
    for bad in [(3, "<", b"x", 1.0), (0, "?", b"x", 1.0), (0, "<", "text", 1.0), (0, "<", b"x", float("nan")),
                (True, "<", b"x", 1.0)]:
        with pytest.raises((TypeError, ValueError)):
            rec.recorder(*bad)  # type: ignore[arg-type]


def test_recorder_cap_stops_raw_bytes_with_a_red_disclosure(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    seen: list[Disclosure] = []
    policy = RecorderPolicy(max_session_mb=1, keep_sessions=3, source="test policy")
    rec = _record(tmp_path, policy=policy, on_disclosure=seen.append)
    chunk = b"\x00" * 512
    with caplog.at_level(logging.ERROR, logger="goliath_run.record"):
        for i in range(2050):  # 2050 * 512 bytes > 1 MiB
            rec.recorder(i % 3, "<", chunk, float(i))
    total = sum((rec.dir / "wire" / ("S%d.bin" % n)).stat().st_size for n in (1, 2, 3))
    assert total <= MiB
    assert rec.cap_reached
    (d,) = seen  # raised once, not per chunk
    assert d.severity == "red" and d.sticky and "not recorded" in d.text
    assert d in rec.disclosures
    assert any("cap" in r.getMessage() for r in caplog.records)
    # Decoded events and state transitions are still recorded after the cap.
    rec.state(Phase.ARMED, Phase.UPLOADING, "K5 typed")
    summary = json.loads(rec.finish("COMPLETED", []).read_text(encoding="utf-8"))
    assert summary["wire"]["cap_reached"] is True
    assert summary["wire"]["bytes_dropped"] == 2050 * 512 - total
    assert any(x["severity"] == "red" for x in summary["disclosures"])


def test_state_and_operator_answers_are_fsynced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _record(tmp_path)
    synced: list[str] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            synced.append("file")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    rec.operator("K5", "Uploading starts this job immediately...", "START ab12")
    assert synced, "an operator answer is on disk before operator() returns"
    synced.clear()
    rec.state(Phase.ARMED, Phase.UPLOADING, "operator typed K5")
    assert synced
    monkeypatch.undo()
    (op,) = _lines(rec.dir / "operator.jsonl")
    assert op["kind"] == "K5" and op["answer"] == "START ab12"
    assert op["shown"].startswith("Uploading starts")
    (tr,) = _lines(rec.dir / "states.jsonl")
    assert (tr["old"], tr["new"], tr["cause"]) == ("ARMED", "UPLOADING", "operator typed K5")


def test_state_accepts_none_as_the_first_old_phase_and_refuses_strings(tmp_path: Path) -> None:
    rec = _record(tmp_path)
    rec.state(None, Phase.IDLE, "session start")
    with pytest.raises(TypeError):
        rec.state("IDLE", Phase.NET_CHECK, "x")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        rec.state(Phase.IDLE, Phase.NET_CHECK, "  ")


def test_events_are_serialised_faithfully(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    rec = _record(tmp_path)
    rec.event(LinkEvent(1, 10.5, parse_text_message("INFO;31;Goliath_TEST00")))
    rec.event(LinkEvent(2, 11.0, PositionFrame(x=1.5, y=2.0, z=0.0, angle=90.0, gcode_line=7)))
    rec.event(LinkEvent(0, 11.5, Discarded(raw=b"\x01\xff", reason="resync")))
    rec.event(LinkEvent(None, 12.0, TransportClosed(error=ConnectionError("EOF"), conn=1)))
    Odd = namedtuple("Odd", "a")
    with caplog.at_level(logging.WARNING, logger="goliath_run.record"):
        rec.event(LinkEvent(0, 13.0, Odd(a=object())))
    events = _lines(rec.dir / "events.jsonl")
    assert [e["type"] for e in events] == ["TextMessage", "PositionFrame", "Discarded", "TransportClosed", "Odd"]
    assert events[0]["conn"] == 1 and events[0]["t"] == 10.5
    assert events[0]["fields"]["params"] == ["Goliath_TEST00"]
    assert events[1]["fields"]["gcode_line"] == 7
    assert events[2]["fields"]["raw"] == {"hex": "01ff"}
    assert events[3]["fields"]["error"] == {"type": "ConnectionError", "message": "EOF"}
    assert events[4]["unserialisable"] is True and "repr" in events[4]
    assert any("Odd" in r.getMessage() for r in caplog.records)
    private = json.loads((rec.dir / "private.json").read_text(encoding="utf-8"))
    assert private["ssids"] == ["Goliath_TEST00"]


def test_position_rows(tmp_path: Path) -> None:
    rec = _record(tmp_path)
    rec.position(t=1.0, x=10.0, y=20.0, z=-1.0, angle=90.0, line=3)
    rec.position(t=2.0, x=11.0, y=21.0, z=-1.0, angle=91.0, line=4, cut=1, deviation=0.5, wheel_slack=12.0)
    rows = (rec.dir / "positions.csv").read_text(encoding="utf-8").splitlines()
    assert rows[0] == "t,x,y,z,angle,line,cut,deviation,wheel_slack"
    assert rows[1] == "1.0,10.0,20.0,-1.0,90.0,3,,,"
    assert rows[2].startswith("2.0,11.0,21.0,-1.0,91.0,4,1,0.5,12.0")
    with pytest.raises(ValueError):
        rec.position(t=float("inf"), x=0.0, y=0.0, z=0.0, angle=0.0, line=0)


def test_finish_validates_outcome_and_closes(tmp_path: Path) -> None:
    rec = _record(tmp_path)
    assert set(OUTCOMES) == {"COMPLETED", "COMPLETED_UNCONFIRMED_UPLOAD", "ENDED_LINK_LOST", "FAULTED_ROBOT_ERR",
                             "STOPPED_PHYSICALLY", "COMPLETED_END_UNCONFIRMED", "REFUSED_BEFORE_UPLOAD",
                             "ABORTED_BEFORE_UPLOAD"}
    with pytest.raises(ValueError, match="outcome"):
        rec.finish("FINE", [])
    with pytest.raises(ValueError):
        rec.finish("COMPLETED", ["ok", ""])
    path = rec.finish("STOPPED_PHYSICALLY", ["operator attested K6"],
                      disclosures=[Disclosure("N-TEST", "amber", "a test disclosure", "test")])
    summary = json.loads(path.read_text(encoding="utf-8"))
    assert summary["outcome"] == "STOPPED_PHYSICALLY"
    assert summary["reasons"] == ["operator attested K6"]
    assert summary["job_id"] == "job-0001"
    assert summary["policy"] == {"max_session_mb": 16, "keep_sessions": 3, "source": "test policy"}
    assert {d["id"] for d in summary["disclosures"]} >= {"N-TEST"}
    for late in (lambda: rec.recorder(0, "<", b"x", 1.0), lambda: rec.operator("K6", "x", "y"),
                 lambda: rec.finish("COMPLETED", [])):
        with pytest.raises(RecordClosedError):
            late()


def test_create_refuses_bad_ids_and_existing_records(tmp_path: Path) -> None:
    clock = Clock()
    for bad in ["", "../x", "a/b", "a b", "x" * 80, "CON"]:
        with pytest.raises(ValueError):
            _record(tmp_path, bad, clock=clock)
    first = _record(tmp_path, "job-0001", clock=clock)
    assert first.dir.parent == tmp_path
    assert first.dir.name == "20261104T120000.000000Z-job-0001"
    with pytest.raises(FileExistsError):
        _record(tmp_path, "job-0001", clock=clock)
    meta = json.loads((first.dir / "record.json").read_text(encoding="utf-8"))
    assert meta["job_id"] == "job-0001" and meta["created_at"] == "2026-11-04T12:00:00.000000Z"
    with pytest.raises(TypeError):
        JobRecord.create(tmp_path, "job-0002")  # type: ignore[call-arg]  # the policy is never implicit


def test_write_file_is_append_only_and_stays_inside(tmp_path: Path) -> None:
    rec = _record(tmp_path)
    rec.write_file("upload.nc", b"G21\nG90\n")
    rec.write_file("sources/job.toml", b"[panel]\n")
    assert (rec.dir / "upload.nc").read_bytes() == b"G21\nG90\n"
    with pytest.raises(FileExistsError):
        rec.write_file("upload.nc", b"other")
    for bad in ["../escape", "/abs", "wire/S1.bin", "summary.json", "private.json", "a/../../b", ""]:
        with pytest.raises(ValueError):
            rec.write_file(bad, b"x")


# ---------------------------------------------------------------------------
# Size and retention policy; free disk
# ---------------------------------------------------------------------------


def test_recorder_policy_needs_values_with_provenance() -> None:
    with pytest.raises(TypeError):
        RecorderPolicy(16, 3)  # type: ignore[call-arg]
    for bad in [dict(max_session_mb=0), dict(keep_sessions=0), dict(max_session_mb=1.5), dict(source=" "),
                dict(keep_sessions=True)]:
        args: dict[str, Any] = dict(max_session_mb=16, keep_sessions=3, source="s")
        args.update(bad)
        with pytest.raises((TypeError, ValueError)):
            RecorderPolicy(**args)
    default = RecorderPolicy.project_default()
    assert default.max_session_mb >= 1 and default.keep_sessions >= 1
    assert "INFERRED" in default.source
    d = default.disclosure()
    assert d.id == "DEFAULT:recorder_policy" and str(default.max_session_mb) in d.text


def _finished(root: Path, clock: Clock, job_id: str, *, finish: bool = True) -> JobRecord:
    rec = _record(root, job_id, clock=clock)
    if finish:
        rec.finish("COMPLETED", [])
    clock.advance(60)
    return rec


def test_retention_policy(tmp_path: Path) -> None:
    clock = Clock()
    policy = RecorderPolicy(max_session_mb=16, keep_sessions=2, source="test policy")
    old_exported = _finished(tmp_path, clock, "job-a")
    old_unexported = _finished(tmp_path, clock, "job-b")
    old_unfinished = _finished(tmp_path, clock, "job-c", finish=False)
    new1 = _finished(tmp_path, clock, "job-d")
    new2 = _finished(tmp_path, clock, "job-e")
    (tmp_path / "not-a-session").mkdir()
    # An export marks a session as safe to prune.
    R.mark_exported(old_exported.dir, "diag.zip", "00" * 32)

    report = prune_sessions(tmp_path, policy, confirmation=None)
    assert report.removed == (old_exported.dir,)
    assert not old_exported.dir.exists()
    skipped = dict(report.skipped)
    assert "not exported" in skipped[old_unexported.dir]
    assert "not finished" in skipped[old_unfinished.dir]
    assert "not a session record" in skipped[tmp_path / "not-a-session"]
    assert set(report.kept) == {new1.dir, new2.dir}
    for d in (old_unexported.dir, old_unfinished.dir, new1.dir, new2.dir):
        assert d.exists()

    # With the operator's confirmation, older sessions go too; the newest are always kept.
    confirmed = prune_sessions(tmp_path, policy, confirmation="operator typed PRUNE")
    assert set(confirmed.removed) == {old_unexported.dir, old_unfinished.dir}
    assert set(confirmed.kept) == {new1.dir, new2.dir}
    assert new1.dir.exists() and new2.dir.exists()
    with pytest.raises(ValueError):
        prune_sessions(tmp_path, policy, confirmation="   ")


def test_prune_reports_a_failed_removal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    policy = RecorderPolicy(max_session_mb=16, keep_sessions=1, source="test policy")
    old = _finished(tmp_path, clock, "job-a")
    _finished(tmp_path, clock, "job-b")

    def failing_rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "in use (injected)")

    monkeypatch.setattr(shutil, "rmtree", failing_rmtree)
    report = prune_sessions(tmp_path, policy, confirmation="yes")
    assert report.removed == ()
    assert [p for p, _ in report.failed] == [old.dir]
    assert "in use" in report.failed[0][1]


def test_free_disk_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert RECORDER_RATE_BYTES_PER_HOUR == 200 * MiB
    assert RECORDER_RESERVE_BYTES == 1024 * MiB
    Usage = namedtuple("Usage", "total used free")
    free = {"value": 0}
    asked: list[Path] = []

    def disk_usage(path: Any) -> Any:
        asked.append(Path(path))
        return Usage(10**13, 0, free["value"])

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)
    two_hours = 2 * 3600.0
    needed = 2 * 200 * MiB + 1024 * MiB
    free["value"] = needed
    ok = free_disk_ok(tmp_path, two_hours)
    assert isinstance(ok, DiskCheck)
    assert ok.ok and ok.needed_bytes == needed and ok.free_bytes == needed
    free["value"] = needed - 1
    short = free_disk_ok(tmp_path, two_hours)
    assert not short.ok and "MiB" in short.message and str(tmp_path) in short.message
    # A folder that does not exist yet: the nearest existing parent is measured, and said so.
    missing = tmp_path / "data" / "sessions"
    check = free_disk_ok(missing, 0.0)
    assert asked[-1] == tmp_path and check.path_checked == tmp_path
    assert check.needed_bytes == 1024 * MiB
    for bad in (float("nan"), -1.0, float("inf"), "1h"):
        with pytest.raises((TypeError, ValueError)):
            free_disk_ok(tmp_path, bad)  # type: ignore[arg-type]


def test_free_disk_check_on_the_real_disk(tmp_path: Path) -> None:
    check = free_disk_ok(tmp_path, 60.0)
    assert check.free_bytes == shutil.disk_usage(tmp_path).free or check.free_bytes > 0
    assert check.needed_bytes == int(RECORDER_RATE_BYTES_PER_HOUR * 60 / 3600) + RECORDER_RESERVE_BYTES


def test_periodic_sync_while_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = {"t": 100.0}
    rec = _record(tmp_path, monotonic=lambda: ticks["t"])
    synced: list[int] = []
    real_fsync: Callable[[int], None] = os.fsync

    def fsync(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    rec.event(LinkEvent(1, 1.0, parse_text_message("INFO;10")))
    assert synced == []  # less than a second since the last sync
    ticks["t"] += 1.5
    rec.event(LinkEvent(1, 2.0, parse_text_message("INFO;11")))
    assert synced, "events are fsync'd at least every second"
