# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""The probe's slot-release sweep and idle-keepalive session (rung R1b).

``python -m goliath_link.probe --slot-release-sweep`` opens sessions of
connect -> ``INFO`` -> close with shrinking gaps and checks that each still
gets its ``INFO;34`` on S2, then holds one session idle to see whether an
idle link stays up. These tests run it against the firmware-faithful
simulator, whose ``slot_release_delay`` is how long a closed connection keeps
its slot. Every socket goes to 127.0.0.1 (the opener asserts it).

Passing here shows the sweep reads the simulator's slot model correctly and
reports honestly when it misbehaves; it proves nothing about the real robot.
"""
import ast
import inspect
import json
import socket
import threading
import time

import pytest

from goliath_link import probe
from goliath_link.probe import (
    DEFAULT_KEEPALIVE_IDLE,
    DEFAULT_SWEEP_GAPS,
    READ_ONLY_COMMANDS,
    SLOT_RELEASE_COMMAND,
    UnsafeCommandError,
    main,
    run_probe,
    run_slot_release_sweep,
)
from goliath_link.sim import SimulatedGoliath
from goliath_link.transport import Keepalive, _default_opener

HOST = "127.0.0.1"
#: Short timings for the simulator (the real defaults are seconds long).
FAST = dict(listen=0.1, reply_timeout=0.6, connect_timeout=2.0, connect_gaps=(0.05, 0.05),
            settle=0.15, keepalive_idle=0.3)
INFO_BYTES = len(b"INFO")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


class _Sock:
    def __init__(self, inner, n: int, opener: "RecordingOpener") -> None:
        self._inner = inner
        self.n = n
        self._opener = opener

    def __getattr__(self, name):  # setsockopt, shutdown, fileno, ...
        return getattr(self._inner, name)

    def sendall(self, data) -> None:
        self._opener.payloads.append((self.n, bytes(data)))
        self._inner.sendall(data)

    # Any other way of writing to the socket is recorded too, so that a
    # transport change cannot slip bytes past the count.
    def send(self, data, *args) -> int:
        self._opener.payloads.append((self.n, bytes(data)))
        return self._inner.send(data, *args)

    def sendto(self, data, *args) -> int:
        self._opener.payloads.append((self.n, bytes(data)))
        return self._inner.sendto(data, *args)

    def sendmsg(self, buffers, *args) -> int:
        self._opener.payloads.append((self.n, b"".join(bytes(b) for b in buffers)))
        return self._inner.sendmsg(buffers, *args)

    def recv(self, bufsize: int) -> bytes:
        while True:
            data = self._inner.recv(bufsize)
            if data and self.n in self._opener.blackhole:
                continue  # the robot's output on this connection never arrives
            return data

    def close(self) -> None:
        self._inner.close()


class RecordingOpener:
    """Real localhost sockets that record every payload, by 1-based open number."""

    def __init__(self, *, blackhole=(), interrupt_at=None) -> None:
        self.opens = 0
        self.payloads = []  # (open number, bytes) for every sendall, on any connection
        self.blackhole = set(blackhole)
        self.interrupt_at = interrupt_at

    def __call__(self, host: str, port: int, timeout: float):
        assert host == HOST, "tests must only ever connect to 127.0.0.1"
        self.opens += 1
        if self.interrupt_at is not None and self.opens >= self.interrupt_at:
            raise KeyboardInterrupt()
        return _Sock(_default_opener(host, port, timeout), self.opens, self)


@pytest.fixture
def make_sim():
    sims = []

    def _make(**kw):
        kw.setdefault("profile", "gfw-01.02.00")
        s = SimulatedGoliath(HOST, 0, **kw)
        s.start()
        sims.append(s)
        return s

    yield _make
    for s in sims:
        s.stop()


def sweep(sim, tmp_path, stamp, **kw):
    opts = dict(FAST)
    opts.update(kw)
    return run_slot_release_sweep(HOST, sim.port, str(tmp_path), stamp=stamp, **opts)


def closed_port() -> int:
    s = socket.socket()
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def errors_text(report) -> str:
    return "\n".join(report["errors"])


def trials(report):
    return report["slot_release"]["trials"]


def verdicts(report):
    return [(t["gap_s"], t["verdict"]) for t in trials(report)]


def assert_sent_only_info(report, sim, opener) -> int:
    """Every byte the probe sent, on every connection of every session, is one INFO on S1.

    Returns the number of INFO commands sent.
    """
    payloads = [data for _, data in opener.payloads]
    assert payloads and set(payloads) == {b"INFO"}, payloads
    assert set(sim.received) == {"INFO"}, sim.received
    assert len(sim.received) == len(payloads)
    sessions_that_sent = 0
    for s in report["sessions"]:
        sent = {c["conn"]: c["bytes_sent"] for c in s["connections"]}
        if not s["connected"]:
            assert all(v == 0 for v in sent.values()), s
            continue
        assert sent == {"S1": INFO_BYTES, "S2": 0, "S3": 0}, (s["name"], sent)
        sessions_that_sent += 1
    assert sessions_that_sent == len(payloads)
    # Each payload went out on the first connection of its session (open numbers
    # 1, 4, 7, ...): the probe never writes to S2 or S3.
    assert all((n - 1) % 3 == 0 for n, _ in opener.payloads), opener.payloads
    totals = report["bytes_sent"]
    assert totals == {"S1": INFO_BYTES * len(payloads), "S2": 0, "S3": 0}, totals
    assert [c["command"] for c in report["commands_sent"]] == ["INFO"] * len(payloads)
    # The simulated robot's own count, independent of the probe's counters and
    # of the opener: every buffer it read from any connection, with the
    # connection's accept index (0-based, the opener's open number - 1).
    assert sim.wait_for(lambda: len(sim.events_of("done")) >= len(payloads), timeout=5)
    got = [(ev.conn, ev.data) for ev in sim.events_of("done")]
    assert got == [(n - 1, data) for n, data in opener.payloads], (got, opener.payloads)
    per_conn = {}
    for conn, data in got:
        per_conn[conn] = per_conn.get(conn, 0) + len(data)
    assert set(per_conn.values()) == {INFO_BYTES}, per_conn  # one INFO per connection that got any
    return len(payloads)


# --------------------------------------------------------------------------- #
# tests first (unit L07)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "delay,gaps,threshold,failing",
    [
        # Gaps sit well clear of the simulated delay, so the outcome does not
        # depend on scheduling jitter.
        (0.3, (0.9, 0.6, 0.1, 0.0), 0.6, (0.1, 0.0)),
        (1.0, (2.0, 1.5, 0.5), 1.5, (0.5,)),
    ],
    ids=["delay-0.3s", "delay-1.0s"],
)
def test_sweep_finds_threshold_with_sim_delay_0p3_and_1p0(make_sim, tmp_path, delay, gaps,
                                                          threshold, failing):
    sim = make_sim(slot_release_delay=delay)
    opener = RecordingOpener()
    report = sweep(sim, tmp_path, f"thr-{delay}", gaps=gaps, recovery_wait=delay + 0.6,
                   opener=opener)
    sr = report["slot_release"]

    assert report["errors"] == [], report["errors"]
    assert sr["format"] == 1 and sr["command"] == "INFO"
    assert sr["gaps_planned_s"] == list(gaps)
    assert [g for g, _ in verdicts(report)] == list(gaps)
    for t in trials(report):
        if t["gap_s"] in failing:
            # INFO;34 went to slot 1, still held by the previous session's
            # closed connection: the probe saw it nowhere.
            assert t["verdict"] == "silent", t
            assert t["routed"] is False
        else:
            assert t["verdict"] == "routed", t
            assert t["routed"] is True
            assert [i["conn"] for i in t["identity_lines"] if i["after_send"]] == ["S2"]
        assert t["actual_gap_s"] >= t["gap_s"] - 0.01, t
        assert t["reference_session"], t
    assert sr["threshold_s"] == threshold
    assert sr["largest_failing_gap_s"] == max(failing)
    assert sr["monotonic"] is True and sr["complete"] is True and sr["stopped"] is None
    assert sr["fact"]["id"] == "slot.release_wait_s" and sr["fact"]["value"] == threshold

    # Before each trial, the session it is measured from had INFO;34 land on S2:
    # the opening reference, the previous routed trial, or a fresh reference
    # after a failed gap.
    refs = {r["session"]: r for r in sr["references"]}
    by_name = {t["session"]: t for t in trials(report)}
    for t in trials(report):
        prev = refs.get(t["reference_session"]) or by_name.get(t["reference_session"])
        assert prev is not None and prev["verdict"] == "routed", (t, prev)
    assert len(sr["references"]) == 1 + len([g for g in failing if g != gaps[-1]])

    # Exactly one INFO per session, nothing else, and only on S1.
    n = assert_sent_only_info(report, sim, opener)
    assert n == len(report["sessions"])
    # The keepalive session ran too (short idle here: no fact, and a warning says why).
    assert report["keepalive"]["ran"] is True
    assert report["keepalive"]["fact"]["value"] is None
    assert report["exit_code"] == 3, (report["errors"], report["warnings"])

    saved = json.loads((tmp_path / f"goliath-probe-slot-release-thr-{delay}.json")
                       .read_text(encoding="utf-8"))
    assert saved["slot_release"] == sr and saved["mode"] == "slot-release-sweep"


def test_sweep_sends_only_INFO(make_sim, tmp_path):
    sim = make_sim(slot_release_delay=0.3)
    opener = RecordingOpener()
    report = sweep(sim, tmp_path, "only-info", gaps=(0.9, 0.1), recovery_wait=0.9,
                   opener=opener)
    assert verdicts(report) == [(0.9, "routed"), (0.1, "silent")]
    # reference, gap 0.9, gap 0.1, then the keepalive session after the recovery wait
    kinds = [s["kind"] for s in report["sessions"]]
    assert kinds == ["slot-release-reference", "slot-release-trial", "slot-release-trial",
                     "keepalive"]
    assert assert_sent_only_info(report, sim, opener) == 4
    assert report["sends_only"] == ["INFO"]
    assert report["settings"]["terminator"] == "none"
    # Every byte left on the 1st connection; the capture shows nothing else was sent.
    capture = (tmp_path / "goliath-probe-slot-release-only-info.capture.txt").read_text(
        encoding="utf-8")
    rows = [ln.split() for ln in capture.splitlines() if ln and not ln.startswith("#")]
    sent = [r for r in rows if r[1] == ">"]
    assert len(sent) == 4 and {r[3] for r in sent} == {"S1"}
    assert {" ".join(r[4:8]) for r in sent} == {"49 4e 46 4f"}  # 'INFO'


def test_slot_release_code_sends_only_the_one_command():
    """Pins lane rule (e): the sweep sends INFO, through _checked_send, nothing else."""
    assert SLOT_RELEASE_COMMAND == "INFO" and SLOT_RELEASE_COMMAND in READ_ONLY_COMMANDS
    tree = ast.parse(inspect.getsource(probe))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "_SlotReleaseRun")
    calls = [n for n in ast.walk(cls) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr in ("send_checked", "_checked_send")]
    assert calls, "the sweep must send through send_checked"
    for call in calls:
        command = call.args[1] if len(call.args) > 1 else None
        assert isinstance(command, ast.Name) and command.id == "SLOT_RELEASE_COMMAND", \
            ast.dump(call)
    # send_checked itself goes through _checked_send (the only socket write;
    # test_probe.py pins that for the whole module).
    send_checked = inspect.getsource(probe._Reporting.send_checked)
    assert "_checked_send(" in send_checked


def test_sweep_send_guard_refuses_any_other_command():
    for cmd in ("GET GLOBAL SYS STATUS", "PRINT SYS REPORT", "GCODE UPLOAD;10", "INFO;34"):
        with pytest.raises(UnsafeCommandError):
            probe._slot_release_guard(cmd)
    probe._slot_release_guard("INFO")  # no error


@pytest.mark.parametrize("scenario", ["stays-up", "robot-drops-S2"])
def test_keepalive_idle_section_reports_link_state(make_sim, tmp_path, scenario):
    sim = make_sim()
    opener = RecordingOpener()
    dropper = None
    if scenario == "robot-drops-S2":
        def drop_during_idle():
            # The keepalive session is the last one (its INFO is the 3rd). Its
            # idle starts about 0.5 s after that INFO; 1 s after it, the robot
            # closes the session's 2nd connection (slot 1).
            if sim.wait_for(lambda: len(sim.received) >= 3, timeout=20):
                time.sleep(1.0)
                sim.drop(1)

        dropper = threading.Thread(target=drop_during_idle, daemon=True)
        dropper.start()
    report = sweep(sim, tmp_path, f"ka-{scenario}", gaps=(0.3,), recovery_wait=0.3,
                   keepalive_idle=2.0, opener=opener)
    if dropper is not None:
        dropper.join(5)
    ka = report["keepalive"]

    assert ka["format"] == 1 and ka["ran"] is True and ka["skipped_reason"] is None
    assert ka["idle_requested_s"] == 2.0
    assert ka["idle_default_s"] == DEFAULT_KEEPALIVE_IDLE == 60.0
    # None only where this OS keeps its own keepalive probe count (teardown unknown).
    assert (ka["idle_required_for_fact_s"] is None) == (ka["keepalive_teardown_s"] is None)
    assert ka["idle_required_for_fact_s"] is None or ka["idle_required_for_fact_s"] >= 60.0
    assert ka["routing_verdict"] == "routed"
    assert len(ka["socket_options"]) == 3 and all("SO_KEEPALIVE on" in o for o in ka["socket_options"])
    session = next(s for s in report["sessions"] if s["kind"] == "keepalive")
    assert session["name"] == ka["session"]
    # INFO exactly once in the keepalive session, then nothing.
    sent = {c["conn"]: c["bytes_sent"] for c in session["connections"]}
    assert sent == {"S1": INFO_BYTES, "S2": 0, "S3": 0}
    assert ka["commands_sent"] == ["INFO"]
    assert assert_sent_only_info(report, sim, opener) == 3

    if scenario == "stays-up":
        assert ka["link_stayed_up"] is True
        assert ka["idle_observed_s"] >= 2.0
        assert ka["went_down_after_s"] is None and ka["went_down_conn"] is None
        assert ka["bytes_received_during_idle"] == {"S1": 0, "S2": 0, "S3": 0}
        assert sorted(ka["silent_connections"]) == ["S1", "S2", "S3"]
        # 2 s of idle cannot show anything about keepalive: no fact, and a
        # warning that says so (never a 'yes' read from too short a test).
        assert ka["fact"] == {"id": "link.keepalive_ok", "value": None, "why": ka["fact"]["why"]}
        assert "60" in ka["fact"]["why"]
        assert any("link.keepalive_ok" in w and "60" in w for w in report["warnings"])
        assert report["exit_code"] == 3
    else:
        assert ka["link_stayed_up"] is False
        assert ka["went_down_conn"] == "S2"
        assert 0 < ka["went_down_after_s"] < 2.0
        assert "closed" in ka["went_down_cause"]
        assert ka["fact"]["value"] is False
        assert any("link.keepalive_ok" in w and "S2" in w for w in report["warnings"])
        assert report["errors"] == [], report["errors"]
        assert report["exit_code"] == 3
    idle_events = [ev for ev in session["events"] if ev["phase"] == "idle"]
    if scenario == "robot-drops-S2":
        assert any(ev["type"] == "closed" for ev in idle_events), session["events"]
    saved = json.loads((tmp_path / f"goliath-probe-slot-release-ka-{scenario}.json")
                       .read_text(encoding="utf-8"))
    assert saved["keepalive"] == ka


# --------------------------------------------------------------------------- #
# The keepalive fact: never a 'yes' that the observation cannot carry
# --------------------------------------------------------------------------- #


def test_keepalive_fact_is_set_only_from_a_long_enough_silent_idle(make_sim, tmp_path, monkeypatch):
    # The design's 60 s floor is lowered for the test only; the 2 x teardown
    # rule stays: keepalive probes after 1 s, every 1 s, 1 probe -> 2 s.
    monkeypatch.setattr(probe, "KEEPALIVE_MIN_IDLE", 0.0)
    sim = make_sim()
    report = sweep(sim, tmp_path, "ka-true", gaps=(0.3,), recovery_wait=0.3, keepalive_idle=4.3,
                   link_keepalive=Keepalive(idle=1.0, interval=1.0, count=1))
    ka = report["keepalive"]
    assert ka["link_stayed_up"] is True and ka["idle_observed_s"] >= 4.3
    if ka["keepalive_teardown_s"] is None:
        # This OS did not let the transport set the probe count: the teardown
        # time is unknown, so the observation cannot set the fact.
        assert ka["fact"]["value"] is None and "probe count" in ka["fact"]["why"]
    else:
        assert ka["keepalive_teardown_s"] == 2.0 and ka["idle_required_for_fact_s"] == 4.0
        assert ka["fact"]["value"] is True, ka["fact"]
        assert report["exit_code"] == 0, (report["errors"], report["warnings"])


def test_keepalive_fact_rules():
    fact = probe._keepalive_fact
    ok = dict(ran=True, stayed_up=True, idle_s=61.0, teardown_s=25.0,
              silent_conns=["S2", "S3"], down_text=None, min_idle=60.0)
    value, why = fact(**ok)
    assert value is True and "S2, S3" in why
    # too short an idle
    value, why = fact(**dict(ok, idle_s=59.9))
    assert value is None and "60" in why
    # 2 x teardown beats the floor
    value, why = fact(**dict(ok, idle_s=61.0, teardown_s=40.0))
    assert value is None and "80" in why
    # unknown probe count on this computer
    value, why = fact(**dict(ok, teardown_s=None))
    assert value is None and "probe count" in why
    # no connection was silent: keepalive probes never had to be answered
    value, why = fact(**dict(ok, silent_conns=[]))
    assert value is None and "every connection received" in why
    # the link went down: an observed 'no', whatever the cause
    value, why = fact(**dict(ok, stayed_up=False, down_text="the robot closed connection S2"))
    assert value is False and "S2" in why
    # the session never got to the idle
    value, why = fact(**dict(ok, ran=False, stayed_up=None))
    assert value is None


@pytest.mark.parametrize(
    "options,teardown",
    [
        (["TCP_NODELAY on; SO_KEEPALIVE on (idle 10 s, interval 5 s, count 3)"] * 3, 25.0),
        (["TCP_NODELAY on; SO_KEEPALIVE on (idle 10 s, interval 5 s, count 3)",
          "TCP_NODELAY on; SO_KEEPALIVE on (idle 10 s, interval 5 s, OS default probe count)",
          "TCP_NODELAY on; SO_KEEPALIVE on (idle 10 s, interval 5 s, count 3)"], None),
        (["TCP_NODELAY on; SO_KEEPALIVE on (timing not set (OSError()), OS default)"] * 3, None),
        (["not configured: socket object has no setsockopt()"] * 3, None),
        ([], None),
    ],
)
def test_keepalive_teardown_needs_every_connection_timed(options, teardown):
    assert probe._keepalive_teardown(options) == teardown


@pytest.mark.parametrize(
    "error,needle",
    [
        (TimeoutError(110, "Connection timed out"), "timed out"),
        (ConnectionResetError(104, "Connection reset by peer"), "reset"),
        (ConnectionError("connection 1 (firmware slot 1) was closed by the robot (EOF)"),
         "closed by the robot"),
        (OSError(113, "No route to host"), "OSError"),
    ],
)
def test_link_loss_cause_is_named(error, needle):
    assert needle in probe._link_loss_cause(error)


# --------------------------------------------------------------------------- #
# Stopping instead of sending into a robot that does not answer as expected
# --------------------------------------------------------------------------- #


def test_reference_without_any_identity_stops_the_sweep(make_sim, tmp_path):
    # Another client holds slot 1: INFO;34 goes to it, the probe sees nothing.
    sim = make_sim(occupied_slots=(1,))
    opener = RecordingOpener()
    report = sweep(sim, tmp_path, "occ1", gaps=(0.6, 0.1), opener=opener)
    sr = report["slot_release"]
    assert sr["trials"] == [] and sr["threshold_s"] is None and sr["fact"]["value"] is None
    assert sr["references"][0]["verdict"] == "silent"
    assert sr["stopped"] and sr["complete"] is False
    assert report["keepalive"]["ran"] is False and report["keepalive"]["skipped_reason"]
    assert report["keepalive"]["fact"]["value"] is None
    assert sim.events_of("to-other-client")
    # One INFO, then nothing more: the robot might be in upload mode.
    assert assert_sent_only_info(report, sim, opener) == 1
    errs = errors_text(report).lower()
    assert "another client" in errs and "power-cycle" in errs and "upload" in errs
    assert report["exit_code"] == 1 and report["completed"] is False


def test_reference_with_identity_on_the_wrong_connection_stops_the_sweep(make_sim, tmp_path):
    sim = make_sim(occupied_slots=(0,))  # S1 lands in slot 1 and gets INFO;34
    opener = RecordingOpener()
    report = sweep(sim, tmp_path, "occ0", gaps=(0.6, 0.1), opener=opener)
    ref = report["slot_release"]["references"][0]
    assert ref["verdict"] == "misrouted"
    assert {i["conn"] for i in ref["identity_lines"]} == {"S1"}
    assert report["slot_release"]["trials"] == []
    assert assert_sent_only_info(report, sim, opener) == 1
    assert "SLOT ROUTING" in errors_text(report)
    assert report["exit_code"] == 1


def test_robot_that_pushes_identity_but_does_not_answer_INFO_stops_the_sweep(make_sim, tmp_path,
                                                                           monkeypatch):
    sim = make_sim()
    monkeypatch.setitem(sim._handlers, "INFO", lambda conn, args, text: None)
    opener = RecordingOpener()
    report = sweep(sim, tmp_path, "noanswer", gaps=(0.6, 0.1), opener=opener)
    ref = report["slot_release"]["references"][0]
    assert ref["verdict"] == "unanswered"
    assert [i["after_send"] for i in ref["identity_lines"]] == [False]  # the connect push only
    assert report["slot_release"]["trials"] == []
    assert assert_sent_only_info(report, sim, opener) == 1
    assert "did not answer INFO" in errors_text(report)
    assert report["exit_code"] == 1


def test_largest_gap_failing_gives_no_threshold(make_sim, tmp_path):
    sim = make_sim(slot_release_delay=1.0)
    report = sweep(sim, tmp_path, "nothr", gaps=(0.5, 0.2), recovery_wait=1.6)
    sr = report["slot_release"]
    assert verdicts(report) == [(0.5, "silent"), (0.2, "silent")]
    assert sr["threshold_s"] is None and sr["fact"]["value"] is None
    assert sr["largest_failing_gap_s"] == 0.5
    assert "--gaps" in errors_text(report) and "0.5" in errors_text(report)
    assert report["keepalive"]["ran"] is True
    assert report["exit_code"] == 1


def test_inconsistent_sweep_keeps_the_conservative_threshold_and_sets_no_fact(make_sim, tmp_path):
    # Opens: reference 1-3, gap 0.6 trial 4-6, gap 0.3 trial 7-9 (its S2,
    # open 8, hears nothing), recovery reference 10-12, gap 0 trial 13-15.
    sim = make_sim()
    opener = RecordingOpener(blackhole={8})
    report = sweep(sim, tmp_path, "nonmono", gaps=(0.6, 0.3, 0.0), recovery_wait=0.3,
                   opener=opener)
    sr = report["slot_release"]
    assert verdicts(report) == [(0.6, "routed"), (0.3, "silent"), (0.0, "routed")]
    assert sr["monotonic"] is False
    assert sr["threshold_s"] == 0.6  # the smallest gap above every failing one
    assert sr["fact"]["value"] is None and "consistent" in sr["fact"]["why"]
    assert any("0.3" in w and "inconsistent" in w for w in report["warnings"])
    assert report["exit_code"] == 3


@pytest.mark.parametrize("held", [1, 0], ids=["slot-1-held-silent", "slot-0-held-misrouted"])
def test_keepalive_session_that_contradicts_the_sweep_withdraws_the_slot_fact(make_sim, tmp_path, held):
    # The sweep completes and finds 0.3 s; then another client takes a slot the
    # sweep's sessions used, so the keepalive session, opened after a longer
    # wait than any gap, does not get its INFO;34 on S2. The same run then holds
    # evidence against its own threshold: slot.release_wait_s must not be set.
    sim = make_sim()

    def occupy():
        # After the gap trial's INFO, once its close has freed the slot.
        if sim.wait_for(lambda: len(sim.received) >= 2 and sim.slot_holders()[held] is None,
                        timeout=20):
            sim.occupy_slot(held)

    th = threading.Thread(target=occupy, daemon=True)
    th.start()
    opener = RecordingOpener()
    report = sweep(sim, tmp_path, f"contra-{held}", gaps=(0.3,), recovery_wait=0.3, opener=opener)
    th.join(5)
    sr, ka = report["slot_release"], report["keepalive"]
    assert verdicts(report) == [(0.3, "routed")]
    assert sr["complete"] is True and sr["threshold_s"] == 0.3
    assert ka["routing_verdict"] == ("silent" if held == 1 else "misrouted")
    assert sr["fact"]["value"] is None, sr["fact"]
    assert "keepalive session" in sr["fact"]["why"]
    assert sr["contradicted_by"] and "keepalive session" in sr["contradicted_by"]
    assert any("slot.release_wait_s is not set" in w for w in report["warnings"])  # visible
    assert report["exit_code"] == 1
    assert assert_sent_only_info(report, sim, opener) == 3


def test_ctrl_c_during_the_keepalive_idle_keeps_what_was_observed(make_sim, tmp_path, monkeypatch):
    sim = make_sim()
    real = probe._IdleSession.pump_for

    def pump_for(self, seconds):
        if self.idle_start is not None:  # the idle: Ctrl+C after 0.2 s of it
            real(self, 0.2)
            raise KeyboardInterrupt()
        return real(self, seconds)

    monkeypatch.setattr(probe._IdleSession, "pump_for", pump_for)
    opener = RecordingOpener()
    report = sweep(sim, tmp_path, "ctrlc-idle", gaps=(0.3,), recovery_wait=0.3, keepalive_idle=5.0,
                   opener=opener)
    ka = report["keepalive"]
    assert report["interrupted"] is True and report["exit_code"] == 130
    assert ka["ran"] is True
    # Nothing is concluded from an idle cut short, and the report says why
    # (not that the session never ran).
    assert ka["link_stayed_up"] is None and ka["fact"]["value"] is None
    assert "did not run" not in ka["fact"]["why"] and "interrupt" in ka["fact"]["why"].lower()
    # What was observed up to then is kept.
    assert len(ka["socket_options"]) == 3
    assert ka["routing_verdict"] == "routed" and ka["commands_sent"] == ["INFO"]
    assert ka["idle_observed_s"] is not None and 0.1 <= ka["idle_observed_s"] < 5.0
    assert assert_sent_only_info(report, sim, opener) == 3
    saved = json.loads((tmp_path / "goliath-probe-slot-release-ctrlc-idle.json").read_text(encoding="utf-8"))
    assert saved["keepalive"] == ka


def test_silent_reference_names_the_terminator_as_a_possible_cause(make_sim, tmp_path):
    # A robot that pushes nothing at connect and ignores 'INFO\n' (GFW
    # 01.02.00 compares the whole buffer): the error must not send the
    # operator power-cycling for another client only.
    sim = make_sim(connect_replay=False)
    opener = RecordingOpener()
    report = sweep(sim, tmp_path, "term-lf", gaps=(0.6, 0.1), terminator="lf", opener=opener)
    ref = report["slot_release"]["references"][0]
    assert ref["verdict"] == "silent"
    errs = errors_text(report)
    assert "--terminator" in errs and "'lf'" in errs
    assert [d for _, d in opener.payloads] == [b"INFO\n"]
    assert report["exit_code"] == 1


# --------------------------------------------------------------------------- #
# Honest failure reporting
# --------------------------------------------------------------------------- #


def test_unreachable_robot(tmp_path):
    opener = RecordingOpener()
    report = run_slot_release_sweep(HOST, closed_port(), str(tmp_path), stamp="unreach",
                                    opener=opener, **FAST)
    assert any("could not connect" in e and "Wi-Fi" in e for e in report["errors"])
    assert opener.payloads == []
    assert report["keepalive"]["ran"] is False
    assert report["exit_code"] == 1
    assert (tmp_path / "goliath-probe-slot-release-unreach.json").exists()


def test_ctrl_c_is_recorded_and_the_report_written(make_sim, tmp_path):
    sim = make_sim()
    opener = RecordingOpener(interrupt_at=4)  # the first trial's S1
    report = sweep(sim, tmp_path, "ctrlc", gaps=(0.3, 0.0), opener=opener)
    assert report["interrupted"] is True and report["completed"] is False
    assert report["exit_code"] == 130
    assert any("interrupted" in e.lower() for e in report["errors"])
    assert report["keepalive"]["ran"] is False
    saved = json.loads((tmp_path / "goliath-probe-slot-release-ctrlc.json").read_text(encoding="utf-8"))
    assert saved["interrupted"] is True and saved["errors"] == report["errors"]
    assert [d for _, d in opener.payloads] == [b"INFO"]


def test_default_terminator_is_none_and_disclosed(make_sim, tmp_path):
    sim = make_sim()
    report = sweep(sim, tmp_path, "term", gaps=(0.3,), recovery_wait=0.3)
    s = report["settings"]
    assert s["terminator"] == "none"
    assert "default" in s["terminator_source"] and "--terminator" in s["terminator_source"]
    assert any("terminator" in n and "none" in n for n in report["notes"])
    assert s["gaps_s"] == [0.3] and s["recovery_wait_s"] == 0.3
    assert s["keepalive_idle_s"] == 0.3 and s["keepalive_idle_default_s"] == 60.0


def test_normal_probe_report_carries_no_r1b_sections(tmp_path):
    # goliath-run refuses an R1 report that carries these sections; the
    # normal probe must not grow them.
    report = run_probe(HOST, closed_port(), str(tmp_path), stamp="plain", terminator="none",
                       listen=0.1, reply_timeout=0.1, pace=0.3, slot_release_wait=0)
    assert "slot_release" not in report and "keepalive" not in report and "mode" not in report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_runs_the_sweep_against_the_simulator(make_sim, tmp_path, capsys):
    # The CLI keeps the vendor's connect timing (0.25 s between connections),
    # so S2 of the 0.1 s trial connects about 0.35 s after the previous close.
    # A 0.8 s slot hold keeps it well before the old slots are free (else S2
    # takes a freed slot 0 and S3 gets slot 1: MISROUTED, not SILENT) and the
    # 1.2 s gap well after: about 0.4 s of scheduling slack on each side.
    sim = make_sim(slot_release_delay=0.8)
    rc = main(["--host", HOST, "--port", str(sim.port), "--out", str(tmp_path),
               "--slot-release-sweep", "--gaps", "1.2,0.1", "--keepalive-idle", "0.5",
               "--listen", "0.1", "--reply-timeout", "0.6"])
    out = capsys.readouterr().out
    assert rc == 3, out  # the 0.5 s keepalive idle cannot set link.keepalive_ok
    assert "slot-release sweep" in out.lower(), out
    assert "Gap 1.2 s" in out and "ROUTED" in out and "SILENT" in out, out
    assert "Threshold" in out and "link.keepalive_ok" in out, out
    saved = json.loads(next(tmp_path.glob("goliath-probe-slot-release-*.json")).read_text(encoding="utf-8"))
    assert saved["slot_release"]["threshold_s"] == 1.2
    assert saved["settings"]["keepalive_idle_s"] == 0.5
    assert saved["settings"]["keepalive_idle_default_s"] == 60.0
    assert set(sim.received) == {"INFO"}


def test_cli_help_states_the_defaults(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "--slot-release-sweep" in out and "--gaps" in out and "--keepalive-idle" in out
    assert "default 5,2,1,0.5,0.25,0" in out
    assert "default 60" in out


def test_default_gaps_are_the_designed_ones():
    assert DEFAULT_SWEEP_GAPS == (5.0, 2.0, 1.0, 0.5, 0.25, 0.0)
    assert DEFAULT_KEEPALIVE_IDLE == 60.0 and probe.KEEPALIVE_MIN_IDLE == 60.0


@pytest.mark.parametrize(
    "argv",
    [
        ["--gaps", "5,2"],  # needs --slot-release-sweep
        ["--keepalive-idle", "60"],
        ["--slot-release-sweep", "--gaps", "1,2"],  # not decreasing
        ["--slot-release-sweep", "--gaps", "2,2"],
        ["--slot-release-sweep", "--gaps", ""],
        ["--slot-release-sweep", "--gaps", "5,nan"],
        ["--slot-release-sweep", "--gaps", "5,-1"],
        ["--slot-release-sweep", "--gaps", "5,x"],
        ["--slot-release-sweep", "--keepalive-idle", "0"],
        ["--slot-release-sweep", "--keepalive-idle", "inf"],
        ["--slot-release-sweep", "--extended"],
        ["--slot-release-sweep", "--pace", "2"],
        ["--slot-release-sweep", "--slot-release-wait", "2"],
    ],
)
def test_cli_usage_errors_exit_2_before_anything_runs(argv, tmp_path, capsys):
    full = ["--host", HOST, "--port", str(closed_port()), "--out", str(tmp_path)] + argv
    with pytest.raises(SystemExit) as exc:
        main(full)
    assert exc.value.code == 2
    assert capsys.readouterr().err
    assert not list(tmp_path.iterdir()), "nothing may run (or be written) on a usage error"


@pytest.mark.parametrize(
    "kw",
    [dict(gaps=()), dict(gaps=(1.0, 2.0)), dict(gaps=(1.0, 1.0)), dict(gaps=(float("nan"),)),
     dict(gaps=(-1.0,)), dict(keepalive_idle=0), dict(keepalive_idle=float("inf")),
     dict(recovery_wait=-1), dict(listen=0), dict(reply_timeout=0), dict(terminator="cr"),
     dict(port=70000)],
)
def test_run_slot_release_sweep_rejects_bad_arguments(kw, tmp_path):
    opts = dict(FAST)
    port = kw.pop("port", closed_port())
    opts.update(kw)
    with pytest.raises(ValueError):
        run_slot_release_sweep(HOST, port, str(tmp_path), **opts)
    assert not list(tmp_path.iterdir())
