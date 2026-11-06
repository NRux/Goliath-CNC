# SPDX-License-Identifier: Apache-2.0
"""End-to-end tests of the read-only probe against the firmware-faithful simulator.

Every socket here goes to 127.0.0.1 (the opener below asserts it). The
simulator profiles are models: ``gfw-01.02.00`` follows the disassembly of the
only surviving firmware image (three-connection slot routing, 512-byte
NUL-padded sends, the 48-byte status blob); ``vendor-app-newer`` is what the
vendor app expects of newer firmware (guesses). Passing here shows the probe
copes with those models and reports honestly when they misbehave; it proves
nothing about the real robot.

Fault injection goes through the probe's ``opener`` seam (:class:`FaultOpener`):
refused connects, failing sends, Ctrl+C, bytes pushed at connect or at close,
delayed or swallowed receives. The probe's open order is fixed: each session
opens S1, S2, S3, so with ``terminator="none"`` opens 1-3 are the terminator
test and opens 4-6 the sweep.
"""
import io
import json
import socket
import sys
import threading
import time

import pytest

from goliath_link import probe
from goliath_link.probe import EXTENDED_COMMANDS, READ_ONLY_COMMANDS, SWEEP_ORDER, main, run_probe
from goliath_link.sim import SimulatedGoliath
from goliath_link.transport import _default_opener

HOST = "127.0.0.1"
FAST = dict(listen=0.2, reply_timeout=0.8, pace=0.3, connect_timeout=2.0,
            slot_release_wait=0.3, connect_gaps=(0.05, 0.05), settle=0.2)
TERMINATORS = ("", "\n", "\r\n")
ALLOWED_PAYLOADS = {
    (c + t).encode("ascii") for c in READ_ONLY_COMMANDS | EXTENDED_COMMANDS for t in TERMINATORS
}
DEFAULT_PAYLOADS = {(c + t).encode("ascii") for c in READ_ONLY_COMMANDS for t in TERMINATORS}
CONN_TAGS = {"S1", "S2", "S3", None}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


class _Sock:
    """Delegating socket wrapper used by :class:`FaultOpener`."""

    def __init__(self, inner, n: int, fo: "FaultOpener") -> None:
        self._inner = inner
        self.n = n
        self._fo = fo
        self._first = True
        self._shut = False

    def __getattr__(self, name):  # setsockopt, fileno, ...
        return getattr(self._inner, name)

    def sendall(self, data) -> None:
        data = bytes(data)
        self._fo.payloads.append(data)
        exc = self._fo.send_fault.pop(data, None)
        if exc is not None:
            raise exc
        self._inner.sendall(data)

    def _late(self) -> bytes:
        return self._fo.at_shutdown.pop(self.n, b"") if self._shut else b""

    def recv(self, bufsize: int) -> bytes:
        if self._first:
            self._first = False
            injected = self._fo.first_recv.get(self.n)
            if injected:
                return injected
        while True:
            try:
                data = self._inner.recv(bufsize)
            except OSError:
                late = self._late()
                if late:
                    return late
                raise
            if not data:
                return self._late() or data
            if self.n in self._fo.blackhole:
                continue  # the robot is silent on this connection
            break
        delay = self._fo.recv_delay.get(self.n)
        if delay:
            time.sleep(delay)
        for old, new in self._fo.replace:
            data = data.replace(old, new)
        return data

    def shutdown(self, how: int) -> None:
        self._shut = True
        self._inner.shutdown(how)

    def close(self) -> None:
        self._inner.close()


class FaultOpener:
    """An opener for run_probe: real localhost sockets plus injected faults.

    ``n`` below is the 1-based open count over the whole probe run.
    """

    def __init__(self, *, refuse_from=None, send_fault=None, first_recv=None, at_shutdown=None,
                 recv_delay=None, blackhole=(), replace=()) -> None:
        self.opens = 0
        self.payloads = []  # every payload handed to sendall, on any connection
        self.refuse_from = refuse_from
        self.send_fault = dict(send_fault or {})  # payload -> exception (raised once)
        self.first_recv = dict(first_recv or {})  # n -> bytes returned by the first recv
        self.at_shutdown = dict(at_shutdown or {})  # n -> bytes returned once after shutdown
        self.recv_delay = dict(recv_delay or {})  # n -> seconds each received chunk is held
        self.blackhole = set(blackhole)  # n whose received data is swallowed
        self.replace = list(replace)  # (old, new) applied to every received chunk

    def __call__(self, host: str, port: int, timeout: float):
        assert host == HOST, "tests must only ever connect to 127.0.0.1"
        self.opens += 1
        if self.refuse_from is not None and self.opens >= self.refuse_from:
            raise ConnectionRefusedError(111, "Connection refused (test)")
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


def run(sim, tmp_path, stamp, **kw):
    opts = dict(FAST)
    opts.update(kw)
    return run_probe(HOST, sim.port, str(tmp_path), stamp=stamp, **opts)


def errors_text(report) -> str:
    return "\n".join(report["errors"])


def by_command(report):
    return {e["command"]: e for e in report["sweep"]}


def reply(entry, part):
    found = [r for r in entry["replies"] if r["part"] == part]
    assert found, f"{entry['command']}: no {part!r} reply in {entry['replies']}"
    return found[0]


def closed_port() -> int:
    s = socket.socket()
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def assert_only_allowlisted(sim, fo=None, allowed=DEFAULT_PAYLOADS):
    names = {c.split(";")[0].rstrip("\r\n") for c in sim.received}
    assert names <= READ_ONLY_COMMANDS | EXTENDED_COMMANDS, names
    assert "GET Z PLATE OFFSET" not in names
    assert all(c.slot == 0 for c in sim.received if c.conn is not None), sim.received
    if fo is not None:
        assert fo.payloads, "nothing was sent?"
        assert set(fo.payloads) <= allowed, sorted(set(fo.payloads) - allowed)


# --------------------------------------------------------------------------- #
# The firmware-faithful profile: the expected success
# --------------------------------------------------------------------------- #


def test_gfw_01_02_00_full_run_succeeds_with_terminator_none(make_sim, tmp_path):
    sim = make_sim()
    fo = FaultOpener()
    report = run(sim, tmp_path, "gfw", opener=fo)

    assert report["errors"] == [], report["errors"]
    assert report["warnings"] == [], report["warnings"]
    assert report["result"] == "ok" and report["exit_code"] == 0
    assert report["completed"] is True and report["interrupted"] is False
    assert report["chosen_terminator"] == "none"
    assert report["firmware_version"] == "01.02.00"

    tests = {t["mode"]: t for t in report["terminator_tests"]}
    assert {m: t["responded"] for m, t in tests.items()} == {"none": True, "lf": False, "crlf": False}
    assert tests["none"]["identity_conn"] == "S2"

    # The sweep: exactly the six, in order, each answered on the expected connection.
    assert [e["command"] for e in report["sweep"]] == list(SWEEP_ORDER)
    for e in report["sweep"]:
        assert e["answered"] and e["complete"], e
        assert e["replies"] and all(r["conn_ok"] for r in e["replies"]), e
    s = by_command(report)
    ident = reply(s["INFO"], "identity")
    assert ident["conn"] == "S2" and ident["raw"].startswith("INFO;34;GFW 01.02.00")
    blob = reply(s["GET GLOBAL SYS STATUS"], "status")
    assert blob["conn"] == "S1" and blob["type"] == "global_status_blob"
    assert len(blob["fields"]) == 10
    assert reply(s["PRINT SYS REPORT"], "report")["raw"].startswith("INFO;40;")
    cage = reply(s["GET SPINDLE CAGE STATUS"], "cage")
    assert cage["conn"] == "S1" and cage["raw"] == "CAGE STATUS: CLOSED"
    assert reply(s["READ INDEX CALIBRATION"], "value")["raw"] == "cfg read index : 28500"
    assert reply(s["READ SPINDLE CALIB"], "value")["raw"] == "spindle distance: 12.34"
    for cmd in ("GET GLOBAL SYS STATUS", "GET SPINDLE CAGE STATUS", "READ INDEX CALIBRATION",
                "READ SPINDLE CALIB"):
        assert reply(s[cmd], "ack")["raw"] == "INFO;0" and reply(s[cmd], "ack")["conn"] == "S2"
    assert report["late"] == [] and report["unattributed"] == []

    # GFW 01.02.00's blob carries no tower firmware: say so, do not invent it.
    assert report["tower_firmware"] is None
    assert "blob" in report["tower_firmware_note"]
    assert report["global_status"]["fields"] == blob["fields"]
    assert report["extended"]["requested"] is False and report["extended"]["sent"] is False

    # Every session used three connections, and every event is tagged.
    sessions = report["sessions"]
    assert [x["kind"] for x in sessions] == ["terminator-test"] * 3 + ["sweep"]
    for x in sessions:
        assert x["connected"] and x["opened"] == 3
        assert [c["conn"] for c in x["connections"]] == ["S1", "S2", "S3"]
        for ev in x["events"]:
            assert ev["conn"] in CONN_TAGS and "t" in ev and ev["phase"] and ev["session"] == x["name"]
        # the connect-time identity push (to slot 1 = S2) is recorded, not drained away
        pushes = [ev for ev in x["events"] if ev.get("code") == 34 and ev["phase"] in ("connect", "passive")]
        assert pushes and all(ev["conn"] == "S2" for ev in pushes), x["name"]
    assert sum(1 for ev in report["passive"] if ev.get("code") == 34) >= 4
    phases = {ev["phase"] for x in sessions for ev in x["events"]}
    known = {"connect", "passive", "trailing", "at close"} | {f"after {c}" for c in SWEEP_ORDER}
    assert phases <= known, phases
    for x in sessions:  # a local close ends every session, recorded as such
        closed = [ev for ev in x["events"] if ev["type"] == "closed"]
        assert len(closed) == 1 and closed[0]["phase"] == "at close" and closed[0]["error"] is None

    # Safety: only allowlisted bytes ever left the probe, all on S1.
    assert_only_allowlisted(sim, fo)

    # Files: UTF-8, JSON matches, capture tags every byte with its connection.
    saved = json.loads((tmp_path / "goliath-probe-gfw.json").read_text(encoding="utf-8"))
    assert saved["exit_code"] == 0 and saved["chosen_terminator"] == "none"
    capture = (tmp_path / "goliath-probe-gfw.capture.txt").read_text(encoding="utf-8")
    rows = [ln.split() for ln in capture.splitlines() if ln and not ln.startswith("#")]
    sent = [r for r in rows if r[1] == ">"]
    received = [r for r in rows if r[1] == "<"]
    assert sent and {r[3] for r in sent} == {"S1"}
    assert {r[3] for r in received} >= {"S1", "S2"}
    info34 = " ".join(f"{b:02x}" for b in b"INFO;34")
    assert any(r[3] == "S2" and info34 in " ".join(r) for r in received)


def test_vendor_app_newer_default_sweep_reads_tower_firmware(make_sim, tmp_path):
    sim = make_sim(profile="vendor-app-newer")
    report = run(sim, tmp_path, "vn", terminator="none")
    assert report["exit_code"] == 0, (report["errors"], report["warnings"])
    assert report["firmware_version"] == "01.02.03"
    assert report["tower_firmware"] == {"t1": "1.2.0", "t2": "1.2.0"}
    assert [e["command"] for e in report["sweep"]] == list(SWEEP_ORDER)
    assert report["extended"]["requested"] is False and report["extended"]["sent"] is False
    assert not ({"GET Z BACKLASH", "SEND TCP POS", "GET Z PLATE OFFSET"} & set(sim.received))


def test_extended_commands_on_newer_firmware(make_sim, tmp_path):
    sim = make_sim(profile="vendor-app-newer")
    fo = FaultOpener()
    report = run(sim, tmp_path, "ext", terminator="none", extended=True, opener=fo)
    assert report["exit_code"] == 0, (report["errors"], report["warnings"])
    assert [e["command"] for e in report["sweep"]] == list(SWEEP_ORDER) + ["GET Z BACKLASH",
                                                                            "SEND TCP POS"]
    s = by_command(report)
    assert reply(s["GET Z BACKLASH"], "value")["raw"].startswith("Z const offset")
    pos = reply(s["SEND TCP POS"], "position")
    assert pos["type"] == "position" and pos["conn"] == "S3"
    assert report["extended"]["requested"] is True and report["extended"]["sent"] is True
    assert_only_allowlisted(sim, fo, allowed=ALLOWED_PAYLOADS)


def test_extended_commands_are_not_sent_to_01_02_00(make_sim, tmp_path):
    sim = make_sim()
    fo = FaultOpener()
    report = run(sim, tmp_path, "ext00", terminator="none", extended=True, opener=fo)
    assert report["extended"]["requested"] is True and report["extended"]["sent"] is False
    assert "01.02.00" in report["extended"]["reason"]
    assert [e["command"] for e in report["sweep"]] == list(SWEEP_ORDER)
    assert_only_allowlisted(sim, fo)  # not even with --extended
    assert report["errors"] == []
    assert any("--extended" in w for w in report["warnings"])
    assert report["exit_code"] == 3  # partial: what was asked for did not all run


def test_line_framed_robot_picks_lf(make_sim, tmp_path):
    sim = make_sim(framing="line")
    report = run(sim, tmp_path, "line")
    modes = {t["mode"]: t["responded"] for t in report["terminator_tests"]}
    assert modes == {"none": False, "lf": True, "crlf": True}
    assert report["chosen_terminator"] == "lf"
    assert report["exit_code"] == 0, (report["errors"], report["warnings"])
    assert all(e["answered"] for e in report["sweep"])


def test_unknown_command_error_policy_is_shown_not_mistaken_for_an_answer(make_sim, tmp_path):
    sim = make_sim(unknown_command="error")
    report = run(sim, tmp_path, "errpol")
    tests = {t["mode"]: t for t in report["terminator_tests"]}
    assert tests["none"]["responded"]
    for mode in ("lf", "crlf"):
        assert not tests[mode]["responded"]
        got = [(r["conn"], r.get("raw")) for r in tests[mode]["replies"]]
        assert ("S2", "ERR;1") in got, got
    assert report["chosen_terminator"] == "none"
    assert report["exit_code"] == 0, (report["errors"], report["warnings"])


# --------------------------------------------------------------------------- #
# Slot routing: another client, slow slot release
# --------------------------------------------------------------------------- #


def test_slot_1_held_by_another_client_is_diagnosed(make_sim, tmp_path):
    sim = make_sim(occupied_slots=(1,))
    report = run(sim, tmp_path, "occ1", terminator="none")
    assert sim.events_of("to-other-client"), "scenario: the reply went to the other client"
    assert report["chosen_terminator"] is None
    assert report["sweep"] == []
    assert report["exit_code"] == 1
    errs = errors_text(report)
    assert "another client" in errs and "S2" in errs and "power-cycle" in errs


def test_slot_0_held_by_another_client_shifts_replies_and_is_diagnosed(make_sim, tmp_path):
    sim = make_sim(occupied_slots=(0,))
    report = run(sim, tmp_path, "occ0", terminator="none")
    assert report["exit_code"] == 1
    assert report["routing"], "the slot violation must be reported"
    errs = errors_text(report)
    assert "INFO;34" in errs and "S1" in errs and "S2" in errs
    assert "another client" in errs


def test_slow_slot_release_is_diagnosed(make_sim, tmp_path):
    sim = make_sim(slot_release_delay=3.0)
    report = run(sim, tmp_path, "slow", terminator="none", reply_timeout=0.5, slot_release_wait=0.1)
    assert report["terminator_tests"][0]["responded"]
    s = by_command(report)
    assert not s["INFO"]["answered"]  # INFO;34 went to the old, closed slot 1
    assert s["GET GLOBAL SYS STATUS"]["answered"]  # the direct reply still reached S1
    assert report["exit_code"] == 1
    errs = errors_text(report)
    assert "S2" in errs and "--slot-release-wait" in errs


def test_enough_slot_release_wait_avoids_the_slow_release(make_sim, tmp_path):
    sim = make_sim(slot_release_delay=0.4)
    report = run(sim, tmp_path, "slowok", terminator="none", slot_release_wait=1.5)
    assert report["exit_code"] == 0, (report["errors"], report["warnings"])


# --------------------------------------------------------------------------- #
# Honest failure reporting (SAFE-3, PROBE-2/3/6)
# --------------------------------------------------------------------------- #


def test_unreachable_robot_is_reported_as_could_not_connect(tmp_path):
    report = run_probe(HOST, closed_port(), str(tmp_path), stamp="unreach", **FAST)
    assert report["chosen_terminator"] is None
    assert any("could not connect" in e and "Wi-Fi" in e for e in report["errors"])
    assert report["exit_code"] == 1
    assert (tmp_path / "goliath-probe-unreach.json").exists()


def test_robot_drops_connection_mid_sweep(make_sim, tmp_path):
    sim = make_sim()

    def dropper():
        if sim.wait_for(lambda: "GET GLOBAL SYS STATUS" in sim.received, timeout=20):
            sim.drop(1)  # the robot closes the sweep's S2

    th = threading.Thread(target=dropper, daemon=True)
    th.start()
    report = run(sim, tmp_path, "drop", terminator="none")
    th.join(5)
    errs = errors_text(report)
    assert "could not connect" not in errs and "Wi-Fi" not in errs
    assert "closed" in errs and "S2" in errs and "sweep" in errs
    sent = [e["command"] for e in report["sweep"]]
    assert sent[:2] == ["INFO", "GET GLOBAL SYS STATUS"]
    assert "READ SPINDLE CALIB" not in sent
    assert any("not sent" in e and "READ SPINDLE CALIB" in e for e in report["errors"])
    assert report["completed"] is False and report["exit_code"] == 1
    saved = json.loads((tmp_path / "goliath-probe-drop.json").read_text(encoding="utf-8"))
    assert saved["errors"] == report["errors"]


def test_robot_closes_first_connection_on_accept(make_sim, tmp_path):
    sim = make_sim(drop_on_accept={0})
    report = run(sim, tmp_path, "acc", terminator="none")
    errs = errors_text(report)
    assert "could not connect" not in errs and "Wi-Fi" not in errs
    assert "S1" in errs and "closed" in errs and "another client" in errs
    assert report["exit_code"] == 1


def test_connect_refused_after_earlier_success_is_not_blamed_on_wifi(make_sim, tmp_path):
    sim = make_sim()
    fo = FaultOpener(refuse_from=4)  # the sweep's first connection
    report = run(sim, tmp_path, "refuse", terminator="none", opener=fo)
    assert report["terminator_tests"][0]["responded"]
    errs = errors_text(report)
    assert "sweep" in errs and "earlier" in errs
    assert "could not connect" not in errs and "Wi-Fi" not in errs
    assert report["exit_code"] == 1


def test_send_failure_is_reported_as_a_send_failure(make_sim, tmp_path):
    sim = make_sim()
    fo = FaultOpener(send_fault={b"GET GLOBAL SYS STATUS": BrokenPipeError(32, "Broken pipe")})
    report = run(sim, tmp_path, "sendfail", terminator="none", opener=fo)
    errs = errors_text(report)
    assert "could not connect" not in errs and "Wi-Fi" not in errs
    assert "sending GET GLOBAL SYS STATUS" in errs and "BrokenPipeError" in errs
    assert report["exit_code"] == 1


def test_unexpected_exception_in_one_phase_is_recorded_and_the_probe_goes_on(make_sim, tmp_path):
    sim = make_sim()
    fo = FaultOpener(send_fault={b"INFO\n": RuntimeError("boom")})
    report = run(sim, tmp_path, "boom", opener=fo)
    errs = errors_text(report)
    assert "boom" in errs and "'lf'" in errs
    assert [t["mode"] for t in report["terminator_tests"]] == ["none", "lf", "crlf"]
    assert report["chosen_terminator"] == "none"
    assert [e["command"] for e in report["sweep"]] == list(SWEEP_ORDER)
    assert report["exit_code"] == 1


def test_ctrl_c_is_recorded_and_the_report_written(make_sim, tmp_path):
    sim = make_sim()
    fo = FaultOpener(send_fault={b"PRINT SYS REPORT": KeyboardInterrupt()})
    try:
        report = run(sim, tmp_path, "ctrlc", terminator="none", opener=fo)
    except KeyboardInterrupt:  # pragma: no cover - the failure being tested for
        pytest.fail("KeyboardInterrupt escaped run_probe without a report")
    assert report["interrupted"] is True and report["completed"] is False
    assert report["exit_code"] == 130
    assert any("interrupted" in e.lower() and "PRINT SYS REPORT" in e for e in report["errors"])
    saved = json.loads((tmp_path / "goliath-probe-ctrlc.json").read_text(encoding="utf-8"))
    assert saved["interrupted"] is True and saved["errors"] == report["errors"]


def test_silent_sweep_is_a_failure(make_sim, tmp_path):
    sim = make_sim()
    fo = FaultOpener(blackhole={4, 5, 6})  # the robot answers nothing in the sweep
    report = run(sim, tmp_path, "silent", terminator="none", opener=fo, reply_timeout=0.4)
    assert report["chosen_terminator"] == "none" and report["firmware_version"] == "01.02.00"
    assert report["sweep"] and not any(e["answered"] for e in report["sweep"])
    assert report["exit_code"] == 1
    errs = errors_text(report)
    assert "no reply to any command" in errs
    assert "INFO" in errs


def test_unparseable_firmware_version_stops_the_probe(make_sim, tmp_path, monkeypatch):
    # The firmware gate: an identity line with no readable version means the firmware
    # is unknown, so nothing more is sent (here: nothing at all, the push came first).
    sim = make_sim()
    monkeypatch.setattr(sim, "_identity_line", lambda: b"INFO;34;GFW unknown;1;2;000000000001\n")
    fo = FaultOpener()
    report = run(sim, tmp_path, "nofw", terminator="none", opener=fo)
    assert fo.payloads == [] and list(sim.received) == []
    assert report["chosen_terminator"] is None and report["sweep"] == []
    assert report["firmware_version"] is None
    assert report["result"] == "stopped" and report["exit_code"] == 4
    gate = report["firmware_gate"]
    assert gate["stopped"] and gate["verdict"] == "unknown" and "GFW unknown" in gate["reason"]


# --------------------------------------------------------------------------- #
# Nothing received is left out (PROBE-4/5)
# --------------------------------------------------------------------------- #


def test_connect_time_push_is_recorded_and_flagged(make_sim, tmp_path):
    sim = make_sim()
    block = b"ERR;17\n" + bytes(512 - 7)  # GOLIATH_IN_HALT, padded like every slot message
    fo = FaultOpener(first_recv={5: block})  # pushed on the sweep's S2 at connect
    report = run(sim, tmp_path, "push", terminator="none", opener=fo)
    sweep = next(x for x in report["sessions"] if x["kind"] == "sweep")
    hits = [ev for ev in sweep["events"] if ev.get("raw") == "ERR;17"]
    assert hits and hits[0]["conn"] == "S2" and hits[0]["phase"] in ("connect", "passive")
    assert any(ev.get("raw") == "ERR;17" for ev in report["passive"])
    assert any("ERR;17" in w for w in report["warnings"])
    assert report["exit_code"] == 3


def test_events_still_queued_at_close_are_in_the_report(make_sim, tmp_path):
    sim = make_sim()
    fo = FaultOpener(at_shutdown={5: b"WARN;2"})  # unterminated: only the close-time flush emits it
    report = run(sim, tmp_path, "atclose", terminator="none", opener=fo)
    sweep = next(x for x in report["sessions"] if x["kind"] == "sweep")
    hits = [ev for ev in sweep["events"] if ev.get("raw") == "WARN;2"]
    assert hits and hits[0]["conn"] == "S2" and hits[0]["phase"] == "at close"
    assert any("WARN;2" in w for w in report["warnings"])
    assert report["exit_code"] == 3


def test_late_reply_is_reported_separately_not_misattributed(make_sim, tmp_path):
    sim = make_sim()
    fo = FaultOpener(recv_delay={5: 1.0})  # the sweep's S2 delivers everything 1 s late
    report = run(sim, tmp_path, "late", terminator="none", opener=fo, listen=1.5,
                 reply_timeout=0.4)
    s = by_command(report)
    assert not s["INFO"]["answered"]
    late = [ev for ev in report["late"] if str(ev.get("raw", "")).startswith("INFO;34")]
    assert late and late[0]["possibly_for"] == "INFO", report["late"]
    for e in report["sweep"]:
        assert not any(str(r.get("raw", "")).startswith("INFO;34") for r in e["replies"]), e
    assert report["exit_code"] == 1
    # Messages after the last command's window are labelled as such, not as its replies.
    last = report["sweep"][-1]
    window_end = last["sent_t"] + last["window_s"]
    sweep = next(x for x in report["sessions"] if x["kind"] == "sweep")
    after = [ev for ev in sweep["events"]
             if ev["t"] > window_end + 0.01 and ev["phase"] != "at close"]
    assert after, "scenario: delayed S2 messages arrive after the last window"
    assert {ev["phase"] for ev in after} == {"trailing"}, [(ev["t"], ev["phase"]) for ev in after]


# --------------------------------------------------------------------------- #
# Console output (PROBE-7) and the CLI
# --------------------------------------------------------------------------- #


def _strict_stdout(monkeypatch, encoding):
    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding=encoding, errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    return buf, stream


@pytest.mark.parametrize("encoding", ["cp1252", "ascii"])
@pytest.mark.parametrize("profile", ["legacy-spec", "gfw-01.02.00"])
def test_summary_survives_non_ascii_robot_bytes(make_sim, tmp_path, monkeypatch, profile, encoding):
    sim = make_sim(profile=profile)
    fo = FaultOpener(replace=[(b"GFW", b"G\xc9W")])
    report = run(sim, tmp_path, f"enc-{profile}-{encoding}", terminator="none", opener=fo)
    buf, stream = _strict_stdout(monkeypatch, encoding)
    probe._print_summary(report)
    stream.flush()
    text = buf.getvalue().decode(encoding)
    assert "Report:" in text
    assert "G\\ufffdW" in text  # shown escaped, never dropped and never a crash


@pytest.mark.parametrize("encoding", ["cp1252", "ascii"])
def test_summary_survives_localized_os_error_text(tmp_path, monkeypatch, encoding):
    def opener(host, port, timeout):
        assert host == HOST
        raise ConnectionRefusedError(111, "Подключение → refused")

    report = run_probe(HOST, closed_port(), str(tmp_path), stamp=f"oserr-{encoding}", opener=opener,
                       terminator="none", **dict(FAST))
    buf, stream = _strict_stdout(monkeypatch, encoding)
    probe._print_summary(report)
    stream.flush()
    text = buf.getvalue().decode(encoding)
    assert "could not connect" in text and "\\u2192" in text


def test_main_runs_against_the_simulator(make_sim, tmp_path, capsys):
    sim = make_sim()
    rc = main(["--host", HOST, "--port", str(sim.port), "--out", str(tmp_path),
               "--terminator", "none", "--listen", "0.2", "--reply-timeout", "1",
               "--pace", "0.3", "--slot-release-wait", "0.3"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Chosen terminator: none" in out and "01.02.00" in out
    [path] = [p for p in tmp_path.glob("goliath-probe-*.json") if ".shareable." not in p.name]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["exit_code"] == 0


# --------------------------------------------------------------------------- #
# Second review: unsolicited errors, late replies, late pushes (PH-1/2/3)
# --------------------------------------------------------------------------- #


def _unknown_command_answer(sim, monkeypatch, message: bytes):
    """The simulator answers every unknown command with ``message`` on slot 1.

    GFW 01.02.00 sends nothing for an unknown command, so on that firmware
    such a line is indistinguishable from an unsolicited fault that happened
    to arrive in the window: that is what this models.
    """
    monkeypatch.setattr(sim, "_unknown", lambda conn, text: sim._slot_message(1, message))


@pytest.mark.parametrize("message,needle", [(b"ERR;13\n", "MOTOR_OCD"),
                                            (b"ERR;17\n", "GOLIATH_IN_HALT"),
                                            (b"WARN;2\n", "T1_UNDER_10")])
def test_fault_in_an_unanswered_terminator_window_is_a_warning(make_sim, tmp_path, monkeypatch,
                                                                message, needle):
    sim = make_sim()
    _unknown_command_answer(sim, monkeypatch, message)
    report = run(sim, tmp_path, "fault-" + needle)
    tests = {t["mode"]: t for t in report["terminator_tests"]}
    raw = message.decode().strip()
    assert ("S2", raw) in [(r["conn"], r.get("raw")) for r in tests["lf"]["replies"]]
    assert report["chosen_terminator"] == "none"
    hits = [w for w in report["warnings"] if raw in w]
    assert hits, report["warnings"]
    assert any(needle in w and "unsolicited" in w for w in hits), hits
    assert report["exit_code"] == 3, (report["errors"], report["warnings"])


def test_err_1_in_an_unanswered_terminator_window_is_kept_visible_as_a_note(make_sim, tmp_path):
    sim = make_sim(unknown_command="error")
    report = run(sim, tmp_path, "err1note")
    assert report["exit_code"] == 0, (report["errors"], report["warnings"])
    notes = [n for n in report["notes"] if "ERR;1" in n]
    assert notes, report["notes"]
    assert any("'lf'" in n for n in notes) and any("'crlf'" in n for n in notes), notes


def test_identity_reply_just_after_the_reply_timeout_is_flagged_as_timing(make_sim, tmp_path,
                                                                         monkeypatch):
    sim = make_sim()
    timers = []

    def slow_info(conn, args, text):  # INFO answered 0.3 s after the probe's reply window
        t = threading.Timer(1.1, lambda: sim._slot_message(1, sim._identity_line()))
        t.daemon = True
        timers.append(t)
        t.start()
        return None

    monkeypatch.setitem(sim._handlers, "INFO", slow_info)
    try:
        report = run(sim, tmp_path, "lateinfo", terminator="none", listen=1.0, reply_timeout=0.8)
    finally:
        for t in timers:
            t.cancel()
    test = report["terminator_tests"][0]
    assert test["responded"] is False
    late = [w for w in report["warnings"] if "after the 0.8 s reply window" in w]
    assert late and "--reply-timeout" in late[0], report["warnings"]
    assert report["chosen_terminator"] is None and report["exit_code"] == 1
    assert "--reply-timeout" in errors_text(report)
    sess = report["sessions"][0]
    assert any(ev.get("code") == 34 and ev["phase"] == "trailing" for ev in sess["events"])


def _push_after_the_send(sim, monkeypatch):
    """The connect-time INFO;34 push arrives only after the probe's first send.

    It is triggered by the first bytes the simulator receives after the
    accept (parsed as a command or not), so the ordering does not depend on
    timing.
    """
    orig = sim._connect_replay

    def late_replay():
        n = len(sim.events_of("done"))

        def fire():
            if sim.wait_for(lambda: len(sim.events_of("done")) > n, timeout=15):
                time.sleep(0.05)
                orig()

        threading.Thread(target=fire, daemon=True).start()

    monkeypatch.setattr(sim, "_connect_replay", late_replay)


def test_late_connect_push_in_every_session_does_not_make_lf_and_crlf_answered(
        make_sim, tmp_path, monkeypatch, capsys):
    sim = make_sim()
    _push_after_the_send(sim, monkeypatch)
    report = run(sim, tmp_path, "latepush")
    tests = {t["mode"]: t for t in report["terminator_tests"]}
    # 'none' got its reply and then the late push: two identity lines.
    assert tests["none"]["responded"] and not tests["none"].get("uncertain")
    for mode in ("lf", "crlf"):
        assert tests[mode].get("uncertain"), tests[mode]
        assert any(f"'{mode}'" in w and "push" in w for w in report["warnings"]), \
            report["warnings"]
    assert report["chosen_terminator"] == "none"
    assert report["exit_code"] == 3, (report["errors"], report["warnings"])
    probe._print_summary(report)
    out = capsys.readouterr().out
    rows = {ln.split(":", 1)[0].strip(): ln for ln in out.splitlines() if "Terminator" in ln}
    assert "ANSWERED (identity" in rows["Terminator none"]
    for mode in ("lf", "crlf"):
        assert "UNCERTAIN" in rows[f"Terminator {mode}"], rows


def test_late_push_prefers_the_variant_with_a_certain_answer(make_sim, tmp_path, monkeypatch):
    # A robot that only accepts LF-terminated commands and pushes late:
    # 'none' gets just the push, 'lf' gets its reply and the push.
    sim = make_sim(framing="line")
    _push_after_the_send(sim, monkeypatch)
    report = run(sim, tmp_path, "latepush-line")
    tests = {t["mode"]: t for t in report["terminator_tests"]}
    assert tests["none"].get("uncertain"), tests["none"]
    assert tests["lf"]["responded"] and not tests["lf"].get("uncertain")
    assert report["chosen_terminator"] == "lf"


def test_robot_without_connect_push_is_not_flagged(make_sim, tmp_path):
    sim = make_sim(connect_replay=False)
    report = run(sim, tmp_path, "nopush")
    tests = {t["mode"]: t for t in report["terminator_tests"]}
    assert tests["none"]["responded"] and not tests["none"].get("uncertain")
    assert report["exit_code"] == 0, (report["errors"], report["warnings"])
