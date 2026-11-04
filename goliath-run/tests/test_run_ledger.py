# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The machine ledger (unit R02; the project design, sections 3.3, 7.5 and 7.7; red-team FW-07, C-08, PHYS-02).

The ledger is the runner's memory across app restarts: an upload in flight, a
pending power cycle, upload hazards, the last known phase, rung results and the
restart baselines. These tests prove that it is written atomically and durably
(temporary file, fsync, rename, directory fsync on POSIX), that anything it
cannot read fails closed, that it is only ever read-modify-written under the
robot lock, and that a power cycle clears it only on the evidence design 7.7
asks for.

Test values are obviously fake (lead note g): serials ``TEST-SERIAL-000n``.
"""
from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from goliath_run import ledger as L
from goliath_run.ledger import (
    SCHEMA_VERSION,
    Ledger,
    LedgerBusyError,
    LedgerLockError,
    LedgerScan,
    LedgerState,
    LedgerStateError,
    LedgerUnreadableError,
    Verdict,
    atomic_write_bytes,
    quarantine_unreadable,
    restart_verdict,
    scan_ledgers,
)
from goliath_run.types import Phase, RestartEvidence

ADDRESS = "10.0.0.1"
SERIAL = "TEST-SERIAL-0001"
OTHER_SERIAL = "TEST-SERIAL-0002"
SHA = "ab" * 32
ATTEST = "POWERCYCLED"


class FakeLock:
    """Stands in for the RobotLock (L09): ``held()`` says whether this process holds it; every call is counted."""

    def __init__(self, held: bool = True) -> None:
        self.is_held = held
        self.calls = 0

    def held(self) -> bool:
        self.calls += 1
        return self.is_held


def _open(data_dir: Path, lock: FakeLock | None = None, address: str = ADDRESS) -> Ledger:
    return Ledger.open_for_address(data_dir, address, lock=lock or FakeLock())


def _bound(data_dir: Path, serial: str = SERIAL, lock: FakeLock | None = None) -> Ledger:
    led = _open(data_dir, lock)
    led.begin_session(4242, ADDRESS)
    led.bind_serial(serial)
    return led


def _evidence(codes: tuple[int, ...] = (), *, drop: bool = False, state: str = "fresh") -> RestartEvidence:
    return RestartEvidence(replay_codes=codes, link_drop_observed=drop, baseline_state=state)  # type: ignore[arg-type]


def _serial_path(data_dir: Path, serial: str = SERIAL) -> Path:
    return data_dir / "robots" / serial / "ledger.json"


# ---------------------------------------------------------------------------
# Atomic, durable writes
# ---------------------------------------------------------------------------

_KILL_CHILD = r"""
import os, sys, time
from pathlib import Path
from goliath_run import ledger as L

class Held:
    def held(self):
        return True

data_dir, marker, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
led = L.Ledger.open_for_address(data_dir, "10.0.0.1", lock=Held())
led.begin_session(os.getpid(), "10.0.0.1")
led.bind_serial("TEST-SERIAL-0001")
if mode == "mid-write":
    real_write = os.write
    def stuck_write(fd, data):
        n = real_write(fd, bytes(data)[: max(1, len(data) // 2)])
        marker.write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(120)
        return n
    os.write = stuck_write
elif mode == "before-rename":
    def stuck_replace(src, dst):
        marker.write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(120)
    os.replace = stuck_replace
else:
    raise SystemExit("unknown mode " + mode)
led.begin_upload("job-0001", "ab" * 32)
raise SystemExit("begin_upload returned although the write was stuck")
"""


@pytest.mark.parametrize("mode", ["mid-write", "before-rename"])
def test_atomic_write_survives_kill_during_write(tmp_path: Path, mode: str) -> None:
    # A real process is killed (SIGKILL on POSIX, TerminateProcess on Windows)
    # while it writes the ledger: half of the bytes written, or all written and
    # fsync'd but not yet renamed. The ledger must still hold the previous
    # complete state, and the scan must say a write was interrupted.
    data_dir = tmp_path / "data"
    marker = tmp_path / "stuck.marker"
    proc = subprocess.Popen([sys.executable, "-c", _KILL_CHILD, str(data_dir), str(marker), mode],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 60
    child_pid = None
    try:
        while child_pid is None:
            text = marker.read_text(encoding="utf-8") if marker.exists() else ""
            if text.strip():
                child_pid = int(text)
                break
            if proc.poll() is not None:
                out, err = proc.communicate()
                pytest.fail("the child ended before getting stuck (exit %s): %s %s"
                            % (proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")))
            if time.monotonic() > deadline:
                pytest.fail("the child never reached the stuck write")
            time.sleep(0.02)
        # Kill the interpreter itself (on Windows a virtual environment's python.exe may be a launcher
        # whose child is the real interpreter): SIGKILL on POSIX, TerminateProcess on Windows.
        os.kill(child_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=30)

    scan = scan_ledgers(data_dir)
    assert scan.unreadable == ()
    (state,) = [s for s in scan.ledgers if s.serial == SERIAL]
    assert state.upload_in_flight is None  # the write that would have set it never completed
    assert state.session_active is not None and state.session_active.pid == child_pid
    assert scan.interrupted_writes, "a temporary file of the killed write should be reported"
    assert all(p.parent == _serial_path(data_dir).parent for p in scan.interrupted_writes)
    json.loads(_serial_path(data_dir).read_text(encoding="utf-8"))  # complete JSON

    # The previous state is usable: a new session continues from it, and its
    # first successful write removes the stale temporary file.
    led = _bound(data_dir)
    led.begin_upload("job-0002", SHA)
    assert led.state.upload_in_flight is not None
    assert scan_ledgers(data_dir).interrupted_writes == ()


class _Trace:
    """Records fsync, replace and directory opens in order (monkeypatched over os)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[tuple[str, Any]] = []
        real_fsync, real_replace, real_open = os.fsync, os.replace, os.open

        def fsync(fd: int) -> None:
            kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
            self.events.append(("fsync", kind))
            real_fsync(fd)

        def replace(src: Any, dst: Any) -> None:
            self.events.append(("replace", Path(dst).name))
            real_replace(src, dst)

        def open_(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if Path(path).is_dir():
                self.events.append(("open-dir", Path(path)))
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(os, "open", open_)


def test_begin_upload_fsyncs_before_returning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    led = _bound(tmp_path)
    trace = _Trace(monkeypatch)
    led.begin_upload("job-0001", SHA)
    # By the time begin_upload returns (and the caller may send the header),
    # the new content was fsync'd and renamed into place.
    events = trace.events
    assert ("replace", "ledger.json") in events
    i_replace = events.index(("replace", "ledger.json"))
    assert ("fsync", "file") in events[:i_replace], "the file is fsync'd before the rename"
    if os.name == "posix":
        assert ("fsync", "dir") in events[i_replace:], "the directory is fsync'd after the rename"
    on_disk = json.loads(_serial_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["upload_in_flight"]["job_id"] == "job-0001"
    assert on_disk["upload_in_flight"]["sha256"] == SHA
    assert on_disk["uploads_since_attested_power_cycle"] == 1


def test_begin_upload_raises_when_fsync_fails_and_changes_nothing(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    led = _bound(tmp_path)
    before = _serial_path(tmp_path).read_bytes()

    def broken_fsync(fd: int) -> None:
        raise OSError(5, "Input/output error (injected)")

    monkeypatch.setattr(os, "fsync", broken_fsync)
    with pytest.raises(OSError, match="injected"):
        led.begin_upload("job-0001", SHA)
    monkeypatch.undo()
    # The caller never got a normal return, so the header was never sent; the
    # ledger on disk and in memory still says no upload, and no temp file is left.
    assert _serial_path(tmp_path).read_bytes() == before
    assert led.state.upload_in_flight is None
    assert scan_ledgers(tmp_path).interrupted_writes == ()


def test_fsync_parent_dir_on_posix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trace = _Trace(monkeypatch)
    target = tmp_path / "robots" / "x" / "ledger.json"
    atomic_write_bytes(target, b'{"a": 1}\n')
    assert target.read_bytes() == b'{"a": 1}\n'
    dir_fsyncs = [e for e in trace.events if e == ("fsync", "dir")]
    if os.name == "posix":
        # The rename is durable only once the directory is fsync'd; the folders
        # created on the way are fsync'd into their parents too.
        assert dir_fsyncs, trace.events
        i_replace = trace.events.index(("replace", "ledger.json"))
        assert ("fsync", "dir") in trace.events[i_replace:]
        opened = [p for kind, p in trace.events if kind == "open-dir"]
        assert target.parent in opened
    else:
        # Windows cannot open a directory for fsync: it is not attempted (and NTFS
        # journals the rename's metadata).
        assert dir_fsyncs == []
        assert [e for e in trace.events if e[0] == "open-dir"] == []


def test_directory_fsync_is_posix_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The non-POSIX branch, exercised on every platform.
    trace = _Trace(monkeypatch)
    monkeypatch.setattr(L, "_DIRECTORY_FSYNC", False)
    atomic_write_bytes(tmp_path / "f.json", b"{}\n")
    assert ("fsync", "dir") not in trace.events
    assert ("fsync", "file") in trace.events


def test_atomic_write_keeps_bytes_exact_on_every_os(tmp_path: Path) -> None:
    # No newline translation (Windows text mode would turn \n into \r\n).
    data = b'{\n  "k": "v"\n}\n' + bytes(range(256))
    target = tmp_path / "f.bin"
    atomic_write_bytes(target, data)
    assert target.read_bytes() == data
    atomic_write_bytes(target, b"second\n")
    assert target.read_bytes() == b"second\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f.bin"]


def test_file_held_open_by_another_program_is_a_clear_error(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    led = _bound(tmp_path)
    before = _serial_path(tmp_path).read_bytes()
    calls: list[Any] = []

    def busy_replace(src: Any, dst: Any) -> None:
        calls.append((src, dst))
        raise PermissionError(13, "Access is denied (injected)")

    monkeypatch.setattr(os, "replace", busy_replace)
    with pytest.raises(LedgerBusyError) as info:
        led.require_power_cycle("test reason")
    monkeypatch.undo()
    message = str(info.value)
    assert str(_serial_path(tmp_path)) in message
    assert "another program" in message and "nothing was changed" in message.lower()
    assert len(calls) == 1, "no silent retry loop"
    assert _serial_path(tmp_path).read_bytes() == before
    assert led.state.pending_power_cycle == ()
    assert scan_ledgers(tmp_path).interrupted_writes == ()


def test_real_open_handle_on_windows_is_a_clear_error(tmp_path: Path) -> None:
    # On Windows a file opened by another handle without delete sharing cannot be
    # replaced; on POSIX the rename succeeds. Either way nothing is lost silently.
    led = _bound(tmp_path)
    with open(_serial_path(tmp_path), "rb"):
        if os.name == "nt":
            with pytest.raises(LedgerBusyError):
                led.require_power_cycle("test reason")
            assert led.state.pending_power_cycle == ()
        else:
            led.require_power_cycle("test reason")
            assert len(led.state.pending_power_cycle) == 1


# ---------------------------------------------------------------------------
# Restart with an upload in flight
# ---------------------------------------------------------------------------


def test_restart_with_upload_in_flight_requires_power_cycle(tmp_path: Path) -> None:
    led = _bound(tmp_path)
    led.note_phase(Phase.UPLOADING, may_be_moving=True)
    led.begin_upload("job-0001", SHA)
    del led  # the app dies: nothing else is written

    scan = scan_ledgers(tmp_path)
    assert not scan.clean
    (pending,) = scan.pending
    assert pending.serial == SERIAL
    assert pending.upload_in_flight is not None and pending.upload_in_flight.job_id == "job-0001"
    # No end-of-upload signal was recorded: the robot may still be in upload mode,
    # so clearing it needs positive power-loss evidence (design 7.7 B).
    assert pending.requirement == "upload-mode"
    assert scan.requirement == "upload-mode"
    assert any("upload mode" in r for r in pending.attention())

    # The restarted app cannot upload again, before or after binding.
    led2 = _open(tmp_path)
    assert led2.requirement() == "upload-mode"
    led2.begin_session(4243, ADDRESS)
    led2.bind_serial(SERIAL)
    with pytest.raises(LedgerStateError, match="upload"):
        led2.begin_upload("job-0002", SHA)
    # The previous session did not close while the robot may have been moving.
    assert any("did not close" in p.reason or "may be moving" in p.reason
               for p in led2.state.pending_power_cycle)


def test_upload_that_ended_but_never_completed_needs_a_power_cycle(tmp_path: Path) -> None:
    led = _bound(tmp_path)
    led.begin_upload("job-0001", SHA)
    led.end_upload("INFO;8")  # the robot left upload mode; the job runs
    assert led.state.upload_in_flight is not None
    assert led.state.upload_in_flight.stream_end == "INFO;8"
    assert led.state.requirement == "after-job"  # POWER_CYCLE_REQUIRED, rule 7.7 A
    assert led.state.restart_baseline_state == "after-interrupt"
    (pending,) = scan_ledgers(tmp_path).pending
    assert pending.requirement == "after-job"


def test_end_upload_clears_only_with_info23(tmp_path: Path) -> None:
    led = _bound(tmp_path)
    led.begin_upload("job-0001", SHA)
    with pytest.raises(LedgerStateError, match="end-of-upload signal"):
        led.end_upload("INFO;23")  # INFO;23 alone: which end-of-upload signal?
    with pytest.raises(ValueError):
        led.end_upload("")
    led.end_upload("INFO;8+INFO;23")
    st_ = led.state
    assert st_.upload_in_flight is None
    assert st_.uploads_since_attested_power_cycle == 1
    # One upload per power cycle (vendor rule, until rung R12): a power cycle is pending.
    assert st_.requirement == "after-job"
    assert st_.restart_baseline_state == "after-complete"
    assert any("one upload per power cycle" in p.reason for p in st_.pending_power_cycle)
    with pytest.raises(LedgerStateError, match="power cycle"):
        led.begin_upload("job-0002", SHA)


def test_end_upload_in_two_steps(tmp_path: Path) -> None:
    led = _bound(tmp_path)
    led.begin_upload("job-0001", SHA)
    led.end_upload("INFO;0")
    led.end_upload("INFO;23")
    assert led.state.upload_in_flight is None
    with pytest.raises(LedgerStateError, match="no upload"):
        led.end_upload("INFO;8+INFO;23")


def test_begin_upload_refused_before_binding_and_with_hazard(tmp_path: Path) -> None:
    led = _open(tmp_path)
    led.begin_session(4242, ADDRESS)
    with pytest.raises(LedgerStateError, match="serial"):
        led.begin_upload("job-0001", SHA)
    led.bind_serial(SERIAL)
    led.mark_upload_hazard("upload cut short at block 3/5", "upload-cut-short")
    with pytest.raises(LedgerStateError, match="hazard"):
        led.begin_upload("job-0001", SHA)


def test_begin_upload_validates_its_arguments(tmp_path: Path) -> None:
    led = _bound(tmp_path)
    for job_id, sha in [("", SHA), ("../x", SHA), ("job", "AB" * 32), ("job", "ab" * 31), ("job", 123)]:
        with pytest.raises((TypeError, ValueError)):
            led.begin_upload(job_id, sha)  # type: ignore[arg-type]
    assert led.state.upload_in_flight is None


# ---------------------------------------------------------------------------
# Unreadable, partly written or newer ledgers fail closed
# ---------------------------------------------------------------------------


def _valid_ledger_json(tmp_path: Path) -> dict[str, Any]:
    _bound(tmp_path / "template")
    data: dict[str, Any] = json.loads(_serial_path(tmp_path / "template").read_text(encoding="utf-8"))
    return data


_BAD: dict[str, Any] = {
    "empty": b"",
    "partly-written": None,  # the first half of a valid file
    "not-json": b"this is not json\n",
    "not-utf8": b"\xff\xfe{}",
    "not-an-object": b"[1, 2, 3]\n",
    "newer-schema": {"schema_version": SCHEMA_VERSION + 1},
    "no-schema": {"schema_version": None},
    "bool-schema": {"schema_version": True},
    "unknown-key": {"surprise": 1},
    "missing-key": {"hazards": None},
    "bad-hazard-kind": {"hazards": [{"kind": "mystery", "reason": "x", "at": "2026-01-01T00:00:00.000000Z"}]},
    "bad-phase": {"last_known_phase": "FLYING"},
    "wrong-serial": {"serial": OTHER_SERIAL},
}


@pytest.mark.parametrize("case", sorted(_BAD))
def test_unreadable_or_newer_schema_ledger_fails_closed(tmp_path: Path, case: str) -> None:
    good = _valid_ledger_json(tmp_path)
    data_dir = tmp_path / "data"
    path = _serial_path(data_dir)
    path.parent.mkdir(parents=True)
    bad = _BAD[case]
    written: bytes
    if case == "partly-written":
        whole = json.dumps(good, indent=2).encode()
        written = whole[: len(whole) // 2]
    elif isinstance(bad, bytes):
        written = bad
    else:
        mutated = dict(good)
        for key, value in bad.items():
            if case == "missing-key":
                del mutated[key]
            else:
                mutated[key] = value
        written = json.dumps(mutated).encode("utf-8")
    path.write_bytes(written)

    scan = scan_ledgers(data_dir)
    assert scan.unreadable == (path,)
    assert scan.reasons[path]
    if case == "newer-schema":
        assert "newer" in scan.reasons[path]
    assert not scan.clean
    assert scan.requirement == "upload-mode"  # unknown content: the strictest clearing

    led = _open(data_dir)  # the address ledger itself is fine
    assert led.requirement() == "upload-mode"
    assert [p for p, _ in led.unreadable_elsewhere] == [path]
    led.begin_session(4242, ADDRESS)
    with pytest.raises(LedgerUnreadableError) as info:
        led.bind_serial(SERIAL)
    assert str(path) in str(info.value)
    assert info.value.path == path
    assert path.read_bytes() == written  # untouched


def test_unreadable_address_ledger_refuses_to_open(tmp_path: Path) -> None:
    led = _open(tmp_path)
    led.begin_session(4242, ADDRESS)
    unbound = led.path
    unbound.write_bytes(b"{")
    with pytest.raises(LedgerUnreadableError) as info:
        _open(tmp_path)
    assert info.value.path == unbound
    assert scan_ledgers(tmp_path).unreadable == (unbound,)


def test_unreadable_pointer_and_dangling_pointer_fail_closed(tmp_path: Path) -> None:
    _bound(tmp_path)
    pointer = tmp_path / "robots" / "pointer.json"
    assert json.loads(pointer.read_text(encoding="utf-8"))["serial"] == SERIAL
    pointer.write_bytes(b"{")
    assert scan_ledgers(tmp_path).unreadable == (pointer,)

    _bound(tmp_path / "b")
    (tmp_path / "b" / "robots" / SERIAL / "ledger.json").unlink()
    scan = scan_ledgers(tmp_path / "b")
    assert scan.unreadable == (tmp_path / "b" / "robots" / "pointer.json",)
    assert "missing" in scan.reasons[scan.unreadable[0]]


def test_quarantine_needs_positive_power_loss_evidence(tmp_path: Path) -> None:
    lock = FakeLock()
    path = _serial_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"schema_version": 99}')
    blocked = quarantine_unreadable(tmp_path, path, _evidence((34,)), attestation=ATTEST, lock=lock)
    assert not blocked.cleared
    assert blocked.remedy
    assert path.exists()

    cleared = quarantine_unreadable(tmp_path, path, _evidence(drop=True), attestation=ATTEST, lock=lock)
    assert cleared.cleared and cleared.route == "link-drop"
    assert not path.exists()
    scan = scan_ledgers(tmp_path)
    assert scan.clean
    assert len(scan.quarantined) == 1 and scan.quarantined[0].read_bytes() == b'{"schema_version": 99}'

    with pytest.raises(LedgerLockError):
        quarantine_unreadable(tmp_path, path, _evidence(drop=True), attestation=ATTEST, lock=FakeLock(False))


def test_quarantine_refuses_readable_or_foreign_files(tmp_path: Path) -> None:
    _bound(tmp_path)
    with pytest.raises(LedgerStateError, match="reads"):
        quarantine_unreadable(tmp_path, _serial_path(tmp_path), _evidence(drop=True), attestation=ATTEST,
                              lock=FakeLock())
    outside = tmp_path / "elsewhere.json"
    outside.write_bytes(b"{")
    with pytest.raises(ValueError, match="not a ledger"):
        quarantine_unreadable(tmp_path, outside, _evidence(drop=True), attestation=ATTEST, lock=FakeLock())


# ---------------------------------------------------------------------------
# Scan before connecting; open by address; bind after IDENTIFY
# ---------------------------------------------------------------------------


def test_scan_of_an_empty_data_dir(tmp_path: Path) -> None:
    scan = scan_ledgers(tmp_path / "never-created")
    assert isinstance(scan, LedgerScan)
    assert scan.clean and not scan.any_ledger
    assert scan.pending == () and scan.unreadable == () and scan.pointer is None
    assert scan.requirement == "none"


def test_scan_finds_in_flight_under_other_serial(tmp_path: Path) -> None:
    first = _bound(tmp_path, SERIAL)
    first.begin_upload("job-0001", SHA)
    first.end_upload("INFO;8")
    del first

    # A new session at the same address: the serial is not known before IDENTIFY,
    # so every ledger counts.
    scan = scan_ledgers(tmp_path)
    assert [s.serial for s in scan.pending] == [SERIAL]
    assert scan.any_ledger and scan.pointer is not None and scan.pointer.serial == SERIAL

    led = _open(tmp_path)
    assert led.serial is None
    assert [s.serial for s in led.pending_elsewhere] == [SERIAL]
    assert led.requirement() == "after-job"
    led.begin_session(4243, ADDRESS)

    # A power cycle with positive evidence, attested before the robot is identified.
    verdict = led.attest_power_cycle(_evidence(drop=True, state="after-interrupt"), attestation=ATTEST)
    assert verdict.cleared and verdict.positive_power_loss

    # IDENTIFY shows another robot: the first robot's ledger stays pending (it
    # was not shown to be power-cycled), this robot's is clean.
    led.bind_serial(OTHER_SERIAL)
    assert led.state.requirement == "none"
    scan = scan_ledgers(tmp_path)
    assert [s.serial for s in scan.pending] == [SERIAL]
    assert scan.pointer is not None and scan.pointer.serial == OTHER_SERIAL


def test_prebind_clearing_is_applied_to_the_identified_robot(tmp_path: Path) -> None:
    first = _bound(tmp_path, SERIAL)
    first.begin_upload("job-0001", SHA)
    first.end_upload("INFO;8")
    first.end_session("test")
    del first
    led = _open(tmp_path)
    led.begin_session(4243, ADDRESS)
    verdict = led.attest_power_cycle(_evidence(drop=True, state="after-interrupt"), attestation=ATTEST)
    assert verdict.cleared
    led.bind_serial(SERIAL)
    assert led.state.requirement == "none"
    assert led.state.upload_in_flight is None
    assert led.state.uploads_since_attested_power_cycle == 0
    assert led.state.last_clearing is not None
    assert scan_ledgers(tmp_path).clean


def test_prebind_weak_clearing_does_not_clear_an_upload_hazard(tmp_path: Path) -> None:
    # Pre-bind, the clearing is judged against every pending ledger; an
    # attestation without positive evidence cannot clear an upload hazard.
    first = _bound(tmp_path, SERIAL)
    first.mark_upload_hazard("upload cut short at block 3/5", "upload-cut-short")
    del first
    led = _open(tmp_path)
    led.begin_session(4243, ADDRESS)
    verdict = led.attest_power_cycle(_evidence((34,), state="fresh"), attestation=ATTEST)
    assert not verdict.cleared and verdict.rule == "upload-mode"
    led.bind_serial(SERIAL)
    assert led.state.requirement == "upload-mode"


def test_bind_merges_hazards_marked_before_identify(tmp_path: Path) -> None:
    led = _open(tmp_path)
    led.begin_session(4242, ADDRESS)
    unbound = led.path
    # The first command after connecting got no answer (PHYS-02): persisted before the serial is known.
    led.mark_upload_hazard("no reply to INFO after connecting", "silent-after-send")
    assert unbound.exists()
    scan = scan_ledgers(tmp_path)
    assert [(s.serial, s.address) for s in scan.pending] == [(None, ADDRESS)]
    led.bind_serial(SERIAL)
    assert not unbound.exists()
    assert [h.kind for h in led.state.hazards] == ["silent-after-send"]
    assert led.path == _serial_path(tmp_path)
    assert [s.serial for s in scan_ledgers(tmp_path).pending] == [SERIAL]


def test_bind_serial_validates_and_never_switches_robot(tmp_path: Path) -> None:
    led = _open(tmp_path)
    led.begin_session(4242, ADDRESS)
    for bad in ["", "../escape", "a/b", "a\\b", "CON", "nul.txt", "x" * 65, "trailing.", "_hidden",
                "pointer.json", " lead", "tab\t"]:
        with pytest.raises(ValueError):
            led.bind_serial(bad)
    with pytest.raises(TypeError):
        led.bind_serial(12)  # type: ignore[arg-type]
    led.bind_serial(SERIAL)
    led.bind_serial(SERIAL)  # the same robot again: no-op
    with pytest.raises(LedgerStateError, match="different robot"):
        led.bind_serial(OTHER_SERIAL)


def test_open_for_address_validates_the_address(tmp_path: Path) -> None:
    for bad in ["", " ", "10.0.0.1\n"]:
        with pytest.raises(ValueError):
            _open(tmp_path, address=bad)
    led = _open(tmp_path, address="fe80::1")  # not a safe folder name: hashed, still works
    led.begin_session(1, "fe80::1")
    assert led.path.exists() and ":" not in led.path.parent.name
    assert scan_ledgers(tmp_path).ledgers[0].address == "fe80::1"


def test_session_active_record_at_connecting(tmp_path: Path) -> None:
    led = _open(tmp_path)
    led.begin_session(4242, ADDRESS)
    on_disk = json.loads(led.path.read_text(encoding="utf-8"))
    assert on_disk["session_active"]["pid"] == 4242
    assert on_disk["session_active"]["address"] == ADDRESS
    assert on_disk["last_known_phase"] == "CONNECTING"
    with pytest.raises(ValueError, match="address"):
        led.begin_session(4242, "10.0.0.2")
    for pid in (0, -1, True, 1.5):
        with pytest.raises((TypeError, ValueError)):
            led.begin_session(pid, ADDRESS)  # type: ignore[arg-type]


def test_session_ending_while_robot_may_move_leaves_power_cycle_pending(tmp_path: Path) -> None:
    led = _bound(tmp_path)
    led.note_phase(Phase.PANEL_DETECTION, may_be_moving=True)
    led.end_session("operator closed the app")
    assert led.state.session_active is None
    assert led.state.requirement == "after-job"
    assert any("PANEL_DETECTION" in p.reason for p in led.state.pending_power_cycle)


def test_clean_session_end_leaves_nothing_pending(tmp_path: Path) -> None:
    led = _bound(tmp_path)
    led.note_phase(Phase.READY, may_be_moving=False)
    led.end_session("operator closed the app")
    assert scan_ledgers(tmp_path).clean


def test_unclosed_session_in_a_moving_phase_is_pending_on_the_next_scan(tmp_path: Path) -> None:
    led = _bound(tmp_path)
    led.note_phase(Phase.PAIRING, may_be_moving=True)
    del led  # the app dies
    (pending,) = scan_ledgers(tmp_path).pending
    assert pending.requirement == "after-job"
    assert any("PAIRING" in r for r in pending.attention())


def test_note_phase_never_records_not_moving_for_a_moving_phase(tmp_path: Path) -> None:
    led = _bound(tmp_path)
    with pytest.raises(ValueError, match="may be moving"):
        led.note_phase(Phase.RUNNING, may_be_moving=False)
    led.note_phase(Phase.BLOCKED, may_be_moving=True)  # BLOCKED keeps the flag it was entered with
    assert led.state.last_known_phase == "BLOCKED" and led.state.last_may_be_moving is True
    with pytest.raises(TypeError):
        led.note_phase("RUNNING", may_be_moving=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Read-modify-write under the injected lock
# ---------------------------------------------------------------------------


def test_read_modify_write_under_lock(tmp_path: Path) -> None:
    lock = FakeLock()
    led = _bound(tmp_path, lock=lock)
    calls = lock.calls
    led.require_power_cycle("reason one")
    assert lock.calls > calls, "every write asks the lock first"

    # Without the lock nothing is read, written or changed.
    before = _serial_path(tmp_path).read_bytes()
    lock.is_held = False
    for action in (lambda: led.require_power_cycle("reason two"),
                   lambda: led.mark_upload_hazard("x", "silent-after-send"),
                   lambda: led.note_phase(Phase.IDLE, may_be_moving=False),
                   lambda: led.end_session("x")):
        with pytest.raises(LedgerLockError):
            action()
    with pytest.raises(LedgerLockError):
        Ledger.open_for_address(tmp_path, ADDRESS, lock=lock)
    assert _serial_path(tmp_path).read_bytes() == before
    lock.is_held = True

    # The write re-reads the file first: a change made behind the session's back
    # (another tool, a hand edit) is refused, never silently overwritten.
    data = json.loads(before)
    data["hazards"] = []
    data["pending_power_cycle"] = []
    _serial_path(tmp_path).write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(LedgerStateError, match="changed on disk"):
        led.require_power_cycle("reason three")
    assert json.loads(_serial_path(tmp_path).read_text(encoding="utf-8"))["pending_power_cycle"] == []


def test_concurrent_writers_in_one_process_lose_nothing(tmp_path: Path) -> None:
    led = _bound(tmp_path)
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            led.require_power_cycle("reason %d" % i)
        except BaseException as exc:  # collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    reasons = sorted(p.reason for p in scan_ledgers(tmp_path).ledgers[0].pending_power_cycle)
    assert reasons == sorted("reason %d" % i for i in range(12))


# ---------------------------------------------------------------------------
# Clearing by power cycle (design 7.7)
# ---------------------------------------------------------------------------


def _baselines(**codes: tuple[int, ...]) -> dict[str, L.RestartBaseline]:
    return {state.replace("_", "-"): L.RestartBaseline(codes=c, rung="R1", at="2026-01-01T00:00:00.000000Z")
            for state, c in codes.items()}


def test_rule_a_attestation_only_is_cleared_with_sticky_disclosure() -> None:
    v = restart_verdict("after-job", "after-complete", _evidence(state="after-complete"), attestation=ATTEST,
                        baselines=_baselines(fresh=(34,), after_complete=(34,)))
    assert isinstance(v, Verdict)
    assert v.cleared and v.route == "attestation-only" and not v.positive_power_loss
    (d,) = v.disclosures
    assert d.sticky and "attestation only" in d.text


def test_rule_a_contradicting_replay_blocks_with_remedy() -> None:
    v = restart_verdict("after-job", "after-complete", _evidence((34, 8), state="after-complete"),
                        attestation=ATTEST, baselines=_baselines(fresh=(34,), after_complete=(34,)))
    assert not v.cleared and v.route is None
    assert any("INFO;8" in r for r in v.reasons)
    assert v.remedy and "power-cycle again" in v.remedy
    # INFO;41 likewise
    v41 = restart_verdict("after-job", "after-complete", _evidence((34, 41), state="after-complete"),
                          attestation=ATTEST, baselines=_baselines(fresh=(34,), after_complete=(34,)))
    assert not v41.cleared


def test_rule_a_code_in_the_matching_baseline_does_not_block() -> None:
    v = restart_verdict("after-job", "after-interrupt", _evidence((34, 8, 41), state="after-interrupt"),
                        attestation=ATTEST, baselines=_baselines(fresh=(34,), after_interrupt=(34, 8, 41)))
    assert v.cleared and v.route == "replay-consistent"
    # ...but the same codes against another state's baseline do.
    v2 = restart_verdict("after-job", "after-complete", _evidence((34, 8), state="after-complete"),
                         attestation=ATTEST, baselines=_baselines(fresh=(34,), after_interrupt=(34, 8)))
    assert not v2.cleared and any("after-complete" in r for r in v2.reasons)


def test_rule_a_info8_evidence_unavailable_when_fresh_baseline_has_it() -> None:
    v = restart_verdict("after-job", "after-complete", _evidence((34, 8), state="after-complete"),
                        attestation=ATTEST, baselines=_baselines(fresh=(34, 8), after_complete=(34,)))
    assert v.cleared
    assert any("INFO;8" in d.text and "unavailable" in d.text for d in v.disclosures)


def test_rule_a_without_baselines_any_info8_or_info41_blocks() -> None:
    v = restart_verdict("after-job", "after-complete", _evidence((34, 8), state="after-complete"),
                        attestation=ATTEST, baselines=None)
    assert not v.cleared
    v_ok = restart_verdict("after-job", "after-complete", _evidence((34,), state="after-complete"),
                           attestation=ATTEST, baselines=None)
    assert v_ok.cleared


def test_rule_a_evidence_against_the_wrong_baseline_state_is_refused() -> None:
    v = restart_verdict("after-job", "after-complete", _evidence((34,), state="fresh"), attestation=ATTEST,
                        baselines=_baselines(fresh=(34,)))
    assert not v.cleared and any("after-complete" in r and "fresh" in r for r in v.reasons)


def test_rule_b_needs_link_drop_or_fresh_boot_signature() -> None:
    base = _baselines(fresh=(34,))
    attested_only = restart_verdict("upload-mode", "after-interrupt", _evidence(state="after-interrupt"),
                                    attestation=ATTEST, baselines=base)
    assert not attested_only.cleared
    assert attested_only.remedy and "link" in attested_only.remedy
    drop = restart_verdict("upload-mode", "after-interrupt", _evidence(drop=True, state="after-interrupt"),
                           attestation=ATTEST, baselines=None)
    assert drop.cleared and drop.route == "link-drop" and drop.positive_power_loss
    fresh = restart_verdict("upload-mode", "after-interrupt", _evidence((34,), state="after-interrupt"),
                            attestation=ATTEST, baselines=base)
    assert fresh.cleared and fresh.route == "fresh-boot-replay"
    # Not the fresh-boot signature: INFO;8 or INFO;6 replayed, or no fresh baseline at all.
    for codes, baselines in [((34, 8), base), ((34, 6), base), ((34,), None), ((34,), _baselines(fresh=(34, 8)))]:
        v = restart_verdict("upload-mode", "after-interrupt", _evidence(codes, state="after-interrupt"),
                            attestation=ATTEST, baselines=baselines)
        assert not v.cleared, (codes, baselines)


def test_attestation_text_is_required() -> None:
    for text in ("", "   ", None):
        with pytest.raises((TypeError, ValueError)):
            restart_verdict("after-job", "fresh", _evidence(), attestation=text, baselines=None)  # type: ignore


def test_attest_power_cycle_on_a_bound_ledger(tmp_path: Path) -> None:
    led = _bound(tmp_path)
    led.record_restart_baseline("fresh", (34,), rung="R1")
    led.record_restart_baseline("after-complete", (34,), rung="R3")
    led.begin_upload("job-0001", SHA)
    led.end_upload("INFO;8+INFO;23")
    blocked = led.attest_power_cycle(_evidence((34, 8), state="after-complete"), attestation=ATTEST)
    assert not blocked.cleared
    assert led.state.requirement == "after-job"
    cleared = led.attest_power_cycle(_evidence((34,), state="after-complete"), attestation=ATTEST)
    assert cleared.cleared and cleared.route == "replay-consistent"
    state = led.state
    assert state.requirement == "none" and state.uploads_since_attested_power_cycle == 0
    assert state.last_clearing is not None and state.last_clearing.attestation == ATTEST
    assert state.last_clearing.evidence == _evidence((34,), state="after-complete")
    led.begin_upload("job-0002", SHA)  # one upload per power cycle: allowed again


def test_hazard_survives_everything_but_a_positive_clearing(tmp_path: Path) -> None:
    led = _bound(tmp_path)
    led.record_restart_baseline("fresh", (34,), rung="R1")
    led.mark_upload_hazard("no reply to INFO after connecting", "silent-after-send")
    for evidence in (_evidence(state="fresh"), _evidence((34, 8), state="fresh")):
        assert not led.attest_power_cycle(evidence, attestation=ATTEST).cleared
    assert [h.kind for h in led.state.hazards] == ["silent-after-send"]
    assert led.attest_power_cycle(_evidence((34,), state="fresh"), attestation=ATTEST).cleared
    assert led.state.hazards == ()


def test_mark_upload_hazard_and_require_power_cycle_validate(tmp_path: Path) -> None:
    led = _bound(tmp_path)
    with pytest.raises(ValueError):
        led.mark_upload_hazard("x", "something-else")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        led.mark_upload_hazard("  ", "upload-cut-short")
    with pytest.raises(ValueError):
        led.require_power_cycle("")
    assert led.state.requirement == "none"


def test_rung_results_and_baselines_are_recorded(tmp_path: Path) -> None:
    led = _bound(tmp_path)
    led.record_rung_result("R1", {"outcome": "go", "facts": ["upload.end_signal"]})
    led.record_restart_baseline("fresh", (34,), rung="R1")
    with pytest.raises(ValueError):
        led.record_rung_result("rung one", {"outcome": "go"})
    with pytest.raises(ValueError):
        led.record_restart_baseline("sometime", (34,), rung="R1")  # type: ignore[arg-type]
    state = scan_ledgers(tmp_path).ledgers[0]
    assert state.rung_results["R1"]["outcome"] == "go"
    assert state.restart_baselines["fresh"].codes == (34,)
    unbound = _open(tmp_path / "other")
    with pytest.raises(LedgerStateError, match="serial"):
        unbound.record_restart_baseline("fresh", (34,), rung="R1")


# ---------------------------------------------------------------------------
# JSON form
# ---------------------------------------------------------------------------


def test_ledger_json_roundtrip_and_strictness(tmp_path: Path) -> None:
    led = _bound(tmp_path)
    led.record_restart_baseline("fresh", (34,), rung="R1")
    led.begin_upload("job-0001", SHA)
    led.end_upload("INFO;8")
    led.mark_upload_hazard("upload cut short at block 3/5", "upload-cut-short")
    data = json.loads(_serial_path(tmp_path).read_text(encoding="utf-8"))
    assert data["schema_version"] == SCHEMA_VERSION
    state = LedgerState.from_json(data)
    assert state.to_json() == data
    assert state == led.state
    raw = _serial_path(tmp_path).read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    with pytest.raises(ValueError):
        LedgerState.from_json({**data, "extra": 1})


def _persistent(state: LedgerState) -> bool:
    return bool(state.upload_in_flight or state.hazards or state.pending_power_cycle
                or state.uploads_since_attested_power_cycle)


def test_live_session_phase_flag_follows_the_phase(tmp_path: Path) -> None:
    # While the session lives, the "may be moving" part follows the noted phase (design 7.3 allows
    # PANEL_DETECTION -> CERTIFY); if the app dies in a moving phase, the next scan sees it.
    led = _bound(tmp_path)
    led.note_phase(Phase.PANEL_DETECTION, may_be_moving=True)
    assert led.state.requirement == "after-job"
    led.note_phase(Phase.CERTIFY, may_be_moving=False)
    assert led.state.requirement == "none"


_OPS = st.lists(st.sampled_from(["hazard", "pending", "upload", "stream-end", "complete", "phase-moving",
                                 "phase-still", "attest-weak", "attest-strong", "end-session"]),
                max_size=12)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ops=_OPS)
def test_property_disk_equals_memory_and_nothing_is_lost(tmp_path_factory: pytest.TempPathFactory,
                                                         ops: list[str]) -> None:
    data_dir = tmp_path_factory.mktemp("prop")
    led = _bound(data_dir)
    led.record_restart_baseline("fresh", (34,), rung="R1")
    for op in ops:
        before = led.state
        try:
            if op == "hazard":
                led.mark_upload_hazard("cut short", "upload-cut-short")
            elif op == "pending":
                led.require_power_cycle("operator stopped the robot")
            elif op == "upload":
                led.begin_upload("job-0001", SHA)
            elif op == "stream-end":
                led.end_upload("INFO;8")
            elif op == "complete":
                led.end_upload("INFO;23")
            elif op == "phase-moving":
                led.note_phase(Phase.RUNNING, may_be_moving=True)
            elif op == "phase-still":
                led.note_phase(Phase.READY, may_be_moving=False)
            elif op == "attest-weak":
                led.attest_power_cycle(_evidence(state=before.restart_baseline_state), attestation=ATTEST)
            elif op == "attest-strong":
                led.attest_power_cycle(_evidence(drop=True, state=before.restart_baseline_state),
                                       attestation=ATTEST)
            elif op == "end-session":
                led.end_session("test")
                led.begin_session(4242, ADDRESS)
        except LedgerStateError:
            assert led.state == before  # a refused call changes nothing
        after = led.state
        # Disk and memory agree after every call.
        assert LedgerState.from_json(json.loads(_serial_path(data_dir).read_text(encoding="utf-8"))) == after
        # A hazard is never dropped except by a clearing with positive power-loss evidence.
        if before.hazards and not after.hazards:
            assert op == "attest-strong"
        # A persistent record (upload in flight, hazard, pending power cycle, upload count) never becomes
        # nothing except by an attested clearing. (Whether the live session's current phase may be moving
        # follows the phase: PANEL_DETECTION -> CERTIFY is moving -> still, design 7.3.)
        if _persistent(before) and not _persistent(after):
            assert op.startswith("attest")


def test_stale_temp_that_cannot_be_removed_does_not_fail_the_write(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch,
                                                                  caplog: pytest.LogCaptureFixture) -> None:
    led = _bound(tmp_path)
    stale = _serial_path(tmp_path).parent / ".ledger.json.999999.deadbeef.tmp"
    stale.write_bytes(b"half")
    real_unlink = Path.unlink

    def held_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == stale:
            raise PermissionError(13, "held by a dying process (injected)")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", held_unlink)
    with caplog.at_level("WARNING", logger="goliath_run.ledger"):
        led.require_power_cycle("reason")
    assert len(led.state.pending_power_cycle) == 1  # the write succeeded
    assert any("could not remove" in r.getMessage() for r in caplog.records)
    monkeypatch.undo()
    assert scan_ledgers(tmp_path).interrupted_writes == (stale,)


def test_bind_retried_after_a_busy_address_ledger_adds_nothing_twice(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    led = _open(tmp_path)
    led.begin_session(4242, ADDRESS)
    unbound = led.path
    led.mark_upload_hazard("no reply to INFO after connecting", "silent-after-send")
    real_unlink = Path.unlink

    def busy_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == unbound:
            raise PermissionError(13, "open in another program (injected)")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", busy_unlink)
    with pytest.raises(LedgerBusyError):
        led.bind_serial(SERIAL)
    monkeypatch.undo()
    # The binding happened (the robot's ledger and the pointer are written); binding again is a no-op.
    assert led.serial == SERIAL
    led.bind_serial(SERIAL)
    assert [h.kind for h in led.state.hazards] == ["silent-after-send"]
    # A new session at the address merges the leftover address ledger without duplicating its hazard.
    led2 = _open(tmp_path)
    led2.begin_session(4243, ADDRESS)
    led2.bind_serial(SERIAL)
    assert [h.kind for h in led2.state.hazards] == ["silent-after-send"]
    assert not unbound.exists()


def test_scan_attention_names_every_reason(tmp_path: Path) -> None:
    led = _bound(tmp_path)
    led.mark_upload_hazard("upload cut short at block 3/5", "upload-cut-short")
    (tmp_path / "robots" / "pointer.json").write_bytes(b"{")
    lines = scan_ledgers(tmp_path).attention()
    assert any("pointer.json" in line for line in lines)
    assert any(SERIAL in line and "block 3/5" in line for line in lines)


def test_default_data_dir_is_per_user() -> None:
    path = L.default_data_dir()
    assert isinstance(path, Path) and path.is_absolute()
    assert "goliath-run" in path.parts[-1] or "goliath-run" in str(path)

