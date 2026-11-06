# SPDX-License-Identifier: Apache-2.0
"""The probe's firmware gate: nothing beyond the identity query to firmware older than 01.02.00 or unknown.

Rung R1 found a robot on GFW 01.00.13; every command of the probe was checked
on 01.02.00 only. The gate must hold at the send choke point
(``_checked_send``), whatever the procedure does, and the procedure must stop
early, open no further session and say so. Every socket here goes to
127.0.0.1; the old-firmware robot is the simulator with a 01.00.13-style
identity and connect-time Wi-Fi scan (old_firmware_robot.py).
"""
import ast
import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from old_firmware_robot import HOST, IDENTITY_01_00_13, OldFirmwareRobot, RecordingOpener

from goliath_link import probe
from goliath_link.probe import (
    EXIT_STOPPED,
    EXTENDED_COMMANDS,
    EXTENDED_ORDER,
    READ_ONLY_COMMANDS,
    SWEEP_ORDER,
    TERMINATORS,
    FirmwareGateClosed,
    UnsafeCommandError,
    _checked_send,
    _FirmwareGate,
    identity_version,
    main,
    run_probe,
)
from goliath_link.framing import StreamFramer
from goliath_link.protocol import parse_text_message
from goliath_link.sim import SimulatedGoliath

FAST = dict(listen=0.5, reply_timeout=0.8, pace=0.3, connect_timeout=2.0,
            slot_release_wait=0.3, connect_gaps=(0.05, 0.05), settle=0.2)
ALL_COMMANDS = tuple(SWEEP_ORDER) + tuple(EXTENDED_ORDER)
TERMS = tuple(t for _, t in TERMINATORS)


def ident(line: str):
    return parse_text_message(line)


def gate_after(*lines: str) -> _FirmwareGate:
    gate = _FirmwareGate()
    for line in lines:
        gate.observe(ident(line))
    return gate


class Recording:
    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(data)


V010013 = "INFO;34;GFW 01.00.13 +b \x01\x02Jul  2 2021;2;1;0123456789ABCDEF01234567"
V010200 = "INFO;34;GFW 01.02.00   ;1;2;000000000001"
V010203 = "INFO;34;GFW 01.02.03   ;1;2;000000000001"
UNREADABLE = "INFO;34;GFW unknown;1;2;000000000001"


# --------------------------------------------------------------------------- #
# The version in an identity line
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("line, version", [
    (V010200, (1, 2, 0)),
    (V010203, (1, 2, 3)),
    (V010013, (1, 0, 13)),  # the dated 01.00.13 form seen at rung R1: the rest is no version
    ("INFO;34;GFW 01.00.13 +b ��Jul  2 2021;2;1;0123", (1, 0, 13)),
    ("INFO;34;GFW 01.02.00;HW 1.0;SW 01.02.00;SN SIM0001", (1, 2, 0)),  # legacy-spec simulator
    ("INFO;34;  GFW 01.01.09   ;1;2;0001", (1, 1, 9)),
    ("INFO;28; Project name:[Goliath] Version:[01.02.00] Build date:[x]", (1, 2, 0)),
    ("INFO;28; Project name:[Goliath] Version:[GFW 01.00.13]", (1, 0, 13)),
])
def test_identity_version_reads_the_gfw_token(line, version):
    assert identity_version(ident(line)) == version


@pytest.mark.parametrize("line", [
    UNREADABLE,
    "INFO;34;GFW 1.2.0;1;2;0001",          # not NN.NN.NN
    "INFO;34;GFW 01.02.003;1;2;0001",      # a longer token is not 01.02.00
    "INFO;34;GFW 01.02.00b;1;2;0001",
    "INFO;34;XYZ 01.02.00;1;2;0001",       # no GFW
    "INFO;34;Build 2021.07.02 GFW 01.02.00;1;2;0001",  # not at the start
    "INFO;34;G�W 01.02.00   ;1;2;0001",
    "INFO;34;",
    "INFO;34",
    "INFO;28; Project name:[Goliath] Build date:[2021.07.02]",
    "INFO;31;GFW 01.02.00",                # no identity line at all
    "INFO;0",
])
def test_anything_else_is_unknown(line):
    assert identity_version(ident(line)) is None


# --------------------------------------------------------------------------- #
# The gate's verdicts
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("lines, verdict", [
    ((), "none"),
    ((V010200,), "ok"),
    ((V010200, V010200), "ok"),
    ((V010203,), "ok"),
    ((V010013,), "older"),
    (("INFO;34;GFW 01.01.99   ;1;2;1",), "older"),
    ((UNREADABLE,), "unknown"),
    ((V010200, UNREADABLE), "unknown"),
    ((V010013, UNREADABLE), "older"),
    ((V010200, V010203), "contradictory"),
    ((V010200, V010013), "contradictory"),
    (("INFO;0", "INFO;31;START", "ERR;1"), "none"),  # only identity lines count
])
def test_gate_verdicts(lines, verdict):
    gate = gate_after(*lines)
    assert gate.state()[0] == verdict
    assert gate.stopped() is (verdict in ("older", "unknown", "contradictory"))
    assert gate.sweep_open() is (verdict == "ok")


def test_stop_reasons_name_the_firmware_and_the_rule():
    assert "01.00.13" in gate_after(V010013).stop_reason() and "01.02.00" in gate_after(V010013).stop_reason()
    assert "GFW unknown" in gate_after(UNREADABLE).stop_reason()
    assert "01.02.00, 01.02.03" in gate_after(V010200, V010203).stop_reason()
    assert gate_after(V010200).stop_reason() is None and gate_after().stop_reason() is None


# --------------------------------------------------------------------------- #
# The choke point: every command, every terminator, every state
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("lines", [(V010013,), (UNREADABLE,), (V010200, V010203), (V010200, V010013),
                                   ("INFO;34;GFW 01.01.09   ;1;2;1", V010013)])
@pytest.mark.parametrize("term", TERMS)
@pytest.mark.parametrize("cmd", ALL_COMMANDS)
def test_choke_point_sends_nothing_once_the_gate_has_stopped(lines, term, cmd):
    rec = Recording()
    with pytest.raises(UnsafeCommandError):
        # Even with the extended permission the caller could pass.
        _checked_send(rec, cmd, term, gate=gate_after(*lines), extended_firmware=(1, 2, 3))
    assert rec.sent == []


@pytest.mark.parametrize("term", TERMS)
@pytest.mark.parametrize("cmd", ALL_COMMANDS)
def test_before_any_identity_only_info_passes(term, cmd):
    rec = Recording()
    if cmd == "INFO":
        _checked_send(rec, cmd, term, gate=gate_after())
        assert rec.sent == [cmd + term]
        return
    with pytest.raises(UnsafeCommandError):
        _checked_send(rec, cmd, term, gate=gate_after(), extended_firmware=(1, 2, 3))
    assert rec.sent == []


@pytest.mark.parametrize("cmd", sorted(READ_ONLY_COMMANDS))
def test_on_01_02_00_and_newer_the_six_pass(cmd):
    for line in (V010200, V010203):
        rec = Recording()
        _checked_send(rec, cmd, "", gate=gate_after(line))
        assert rec.sent == [cmd]


@pytest.mark.parametrize("cmd", sorted(EXTENDED_COMMANDS))
def test_extended_needs_the_robot_to_state_the_newer_version(cmd):
    # The caller's word is not enough: the identities received must say so too.
    with pytest.raises(FirmwareGateClosed):
        _checked_send(Recording(), cmd, "", gate=gate_after(V010200), extended_firmware=(1, 2, 3))
    with pytest.raises(FirmwareGateClosed):
        _checked_send(Recording(), cmd, "", gate=gate_after(), extended_firmware=(1, 2, 3))
    with pytest.raises(FirmwareGateClosed):
        _checked_send(Recording(), cmd, "", gate=gate_after("INFO;34;GFW 01.02.04   ;1;2;1"),
                      extended_firmware=(1, 2, 3))
    rec = Recording()
    _checked_send(rec, cmd, "", gate=gate_after(V010203), extended_firmware=(1, 2, 3))
    assert rec.sent == [cmd]


def _run_for_unit_test(tmp_path, extended=True):
    report = {"extended": {"requested": extended, "sent": False, "reason": None},
              "errors": [], "warnings": [], "notes": [], "terminator_tests": []}
    capture = probe._CaptureLog(str(tmp_path / "unit.capture.txt"), 0.0)
    run = probe._Run(HOST, 1, capture, report, terminator="auto", listen=0.1, reply_timeout=0.1,
                     pace=0.3, slot_release_wait=0.0, connect_timeout=1.0, extended=extended,
                     connect_gaps=(0.0, 0.0), settle=0.0, opener=None)
    return run, capture


@pytest.mark.parametrize("lines", [(V010013,), (UNREADABLE,), (V010200, V010203)])
def test_every_send_path_of_the_run_is_refused_once_stopped(tmp_path, lines):
    """_Run.send_checked is the only caller of the choke point (see the AST test below)."""
    run, capture = _run_for_unit_test(tmp_path)
    for line in lines:
        run.gate.observe(ident(line))
    transport = SimpleNamespace(connected=True, last_send_at=None, sent=[])
    transport.send = transport.sent.append
    sess = SimpleNamespace(name="s", label="s", run=run, transport=transport, sends=[])
    try:
        for cmd in ALL_COMMANDS:
            for term in TERMS:
                with pytest.raises(probe._GateStop):
                    run.send_checked(sess, cmd, term, extended_firmware=(1, 2, 3))
    finally:
        capture.close()
    assert transport.sent == [] and sess.sends == []


def test_send_paths_all_go_through_the_run_choke_point():
    tree = ast.parse(inspect.getsource(probe))
    callers = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name in ("_checked_send", "send_checked"):
                callers.setdefault(name, set()).add(self.stack[-1])
                if name == "_checked_send":
                    assert "gate" in {k.arg for k in node.keywords}, "the gate must be passed"
            self.generic_visit(node)

    Visitor().visit(tree)
    assert callers["_checked_send"] == {"send_checked"}
    assert callers["send_checked"] == {"terminator_test", "sweep"}


# --------------------------------------------------------------------------- #
# End to end: the probe against robots on old, unknown and contradictory firmware
# --------------------------------------------------------------------------- #


@pytest.fixture
def robots():
    started = []

    def start(cls=OldFirmwareRobot, **kw):
        robot = cls(**kw) if cls is OldFirmwareRobot else cls(HOST, 0, **kw)
        robot.start()
        started.append(robot)
        return robot

    yield start
    for robot in started:
        robot.stop()


def run(robot, tmp_path, stamp, **kw):
    opts = dict(FAST)
    opts.update(kw)
    return run_probe(HOST, robot.port, str(tmp_path), stamp=stamp, **opts)


def assert_stopped(report, verdict):
    gate = report["firmware_gate"]
    assert gate["stopped"] is True and gate["verdict"] == verdict, gate
    assert report["result"] == "stopped" and report["exit_code"] == EXIT_STOPPED == 4
    assert report["completed"] is False
    assert report["errors"] == [], report["errors"]
    assert gate["reason"] and gate["stopped_at"]
    saved = json.loads(open(report["report_file"], encoding="utf-8").read())
    assert saved["firmware_gate"] == gate and saved["exit_code"] == 4


@pytest.mark.parametrize("extended", [False, True])
@pytest.mark.parametrize("terminator", ["auto", "none", "crlf"])
def test_identity_pushed_at_connect_means_not_one_byte_is_sent(robots, tmp_path, terminator, extended):
    robot = robots()
    fo = RecordingOpener()
    report = run(robot, tmp_path, "push", opener=fo, terminator=terminator, extended=extended)
    assert fo.payloads == [] and list(robot.received) == []
    assert fo.opens == 3, "one session only: no further terminator variant, no sweep"
    assert_stopped(report, "older")
    gate = report["firmware_gate"]
    assert gate["version"] == "01.00.13" and report["firmware_version"] == "01.00.13"
    assert "01.00.13" in gate["reason"] and "01.02.00" in gate["reason"]
    assert "before the probe had sent anything" in gate["stopped_at"]
    planned = [m for m, _ in TERMINATORS] if terminator == "auto" else [terminator]
    for mode in planned:
        assert any(f"terminator test '{mode}'" in c for c in gate["not_sent"]), gate["not_sent"]
    for cmd in SWEEP_ORDER:
        assert f"{cmd!r} (sweep)" in gate["not_sent"]
    assert ("'GET Z BACKLASH' (--extended)" in gate["not_sent"]) is extended
    assert report["sweep"] == [] and report["chosen_terminator"] is None
    [test] = report["terminator_tests"]
    assert test["not_sent"] and not test["responded"]
    if extended:
        assert "firmware gate" in report["extended"]["reason"] and report["extended"]["sent"] is False


def test_identity_in_reply_stops_after_the_one_info(robots, tmp_path):
    # No push at connect: the first INFO is sent (no identity yet), its reply says
    # 01.00.13, and nothing else follows: no lf/crlf variant, no sweep.
    robot = robots(push_on_connect=False)
    fo = RecordingOpener()
    report = run(robot, tmp_path, "reply", opener=fo, extended=True)
    assert fo.payloads == [b"INFO"]
    assert [str(c) for c in robot.received] == ["INFO"]
    assert fo.opens == 3
    assert_stopped(report, "older")
    assert "after sending 'INFO'" in report["firmware_gate"]["stopped_at"]
    not_sent = report["firmware_gate"]["not_sent"]
    assert "'INFO\\n' (terminator test 'lf')" in not_sent and "'INFO' (sweep)" in not_sent
    assert not any("'none'" in c for c in not_sent)
    assert report["chosen_terminator"] == "none"  # the evidence stays: INFO was answered
    assert not any("the sweep checks it" in n for n in report["notes"]), report["notes"]


def test_simulator_firmware_01_01_09_stops_too(robots, tmp_path):
    # The plain 01.02.00 simulator told to report 01.01.09 (the importer's B4 fixture).
    robot = robots(cls=SimulatedGoliath, profile="gfw-01.02.00", firmware=(1, 1, 9))
    fo = RecordingOpener()
    report = run(robot, tmp_path, "0109", opener=fo)
    assert fo.payloads == [] and list(robot.received) == []
    assert_stopped(report, "older")
    assert report["firmware_version"] == "01.01.09"


def test_unreadable_identity_stops_the_probe(robots, tmp_path):
    robot = robots(identity=b"INFO;34;GFW unknown;1;2;000000000001\n", ssids=())
    fo = RecordingOpener()
    report = run(robot, tmp_path, "unknown", opener=fo)
    assert fo.payloads == [] and list(robot.received) == []
    assert_stopped(report, "unknown")
    assert report["firmware_version"] is None
    assert "GFW unknown" in report["firmware_gate"]["reason"]
    assert "GFW unknown" in report["firmware_identity_raw"]


def test_contradicting_identity_at_the_sweep_stops_it(robots, tmp_path):
    # 01.02.00 through the terminator tests, then the sweep's connect push says 01.02.03:
    # two robots, or a reflash. The sweep sends nothing.
    robot = robots(cls=SimulatedGoliath, profile="gfw-01.02.00")
    original = robot._identity_line

    def identity():
        return original().replace(b"01.02.00", b"01.02.03") if robot.accept_count > 9 else original()

    robot._identity_line = identity
    fo = RecordingOpener()
    report = run(robot, tmp_path, "contra", opener=fo, extended=True)
    assert sorted(fo.payloads) == sorted([b"INFO", b"INFO\n", b"INFO\r\n"]), fo.payloads
    assert fo.opens == 12, "three terminator tests and the sweep's connect"
    assert_stopped(report, "contradictory")
    assert report["firmware_gate"]["versions"] == ["01.02.00", "01.02.03"]
    assert report["firmware_version"] is None
    assert report["sweep"] == []
    for cmd in ALL_COMMANDS:
        assert any(c.startswith(repr(cmd)) for c in report["firmware_gate"]["not_sent"]), cmd
    assert report["firmware_gate"]["stopped_at"].startswith("sweep, ")


def test_choke_point_holds_when_the_procedure_does_not_stop(robots, tmp_path, monkeypatch):
    """Regression: with the procedure's own checks switched off, every send is still refused."""
    monkeypatch.setattr(probe._FirmwareGate, "stopped", lambda self: False)
    monkeypatch.setattr(probe._FirmwareGate, "sweep_open", lambda self: True)

    def choose(self):
        self.report["chosen_terminator"] = "none"

    monkeypatch.setattr(probe._Run, "choose_terminator", choose)
    robot = robots()
    fo = RecordingOpener()
    report = run(robot, tmp_path, "nogate", opener=fo, extended=True)
    assert fo.payloads == [] and list(robot.received) == []
    assert fo.opens == 12, "every terminator session and the sweep were opened and tried"
    tests = report["terminator_tests"]
    assert len(tests) == 3 and all("probe refuses 'INFO'" in t["not_sent"] for t in tests)
    assert report["firmware_gate"]["stopped"] is True and report["exit_code"] == 4


def test_01_02_00_runs_exactly_as_before(robots, tmp_path):
    robot = robots(cls=SimulatedGoliath, profile="gfw-01.02.00")
    fo = RecordingOpener()
    report = run(robot, tmp_path, "ok", opener=fo)
    assert report["result"] == "ok" and report["exit_code"] == 0, report["errors"] + report["warnings"]
    gate = report["firmware_gate"]
    assert gate["verdict"] == "ok" and gate["stopped"] is False and gate["not_sent"] == []
    assert "01.02.00" in gate["reason"]
    assert fo.payloads == [b"INFO", b"INFO\n", b"INFO\r\n"] + [c.encode() for c in SWEEP_ORDER]


def test_cli_prints_the_stop_and_where_to_read_on(robots, tmp_path, capsys):
    robot = robots()
    rc = main(["--host", HOST, "--port", str(robot.port), "--out", str(tmp_path),
               "--listen", "0.5", "--reply-timeout", "0.8", "--pace", "0.3",
               "--slot-release-wait", "0.3", "--extended"])
    out = capsys.readouterr().out
    assert rc == 4
    assert "STOPPED (exit code 4)" in out
    stop = out[out.index("PROBE STOPPED"):]
    flat = " ".join(stop.split())
    assert "not a success" in flat and "01.00.13" in flat and "older than 01.02.00" in flat
    assert "probe-results/README.md" in flat and "docs/field-guide/02-first-probe.md" in flat
    assert "Not sent:" in out and "'PRINT SYS REPORT' (sweep)" in out


def test_identity_bytes_used_here_are_the_01_00_13_form():
    # Guard for the fixture itself: the dated identity and its version.
    line = parse_text_message(IDENTITY_01_00_13.decode("ascii").rstrip("\n"))
    assert identity_version(line) == (1, 0, 13)


def test_the_gate_stops_on_what_the_rung_r1_robot_pushed_at_connect():
    """The scrubbed rung R1 capture (probe-results/): the first session's S2 bytes close the gate."""
    root = Path(__file__).resolve().parents[2] / "probe-results"
    captures = sorted(root.glob("goliath-probe-*.capture.txt")) if root.is_dir() else []
    if not captures:
        pytest.skip("no rung R1 capture in probe-results/ (a goliath-link checkout of its own)")
    row = re.compile(r"^\s*([\d.]+)  <  (\S+)\s+S2  ([0-9a-f ]+?)  \|")
    first_send = None
    pushed = b""
    for line in captures[0].read_text(encoding="utf-8").splitlines():
        if first_send is None and "  >  " in line:
            first_send = line
            break
        m = row.match(line)
        if m:
            pushed += bytes.fromhex(m.group(3))
    framer = StreamFramer()
    gate = _FirmwareGate()
    for event in framer.feed(pushed, now=0.0) + framer.flush():
        gate.observe(event)
    verdict, version, _versions, _unreadable = gate.state()
    assert (verdict, version) == ("older", (1, 0, 13))
    assert first_send is not None  # that probe, frozen, went on to send; this one would not
    with pytest.raises(FirmwareGateClosed):
        _checked_send(Recording(), "INFO", "", gate=gate)
