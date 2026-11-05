# SPDX-License-Identifier: Apache-2.0
"""GoliathClient's command safety layer, end to end against the simulator.

Before the firmware gate, every text command the client sends passes, in this
order: the hard denylist (always on), the strict set (``strict_commands=True``)
and the caller's per-phase :class:`~goliath_link.policy.CommandPolicy`. A
refusal raises :class:`~goliath_link.policy.CommandRefusedError` and puts
nothing on the wire: the simulator's ``received`` log (every buffer it parsed)
stays empty. Real localhost TCP, three connections, as in test_client_e2e.py.
"""
import logging
import threading
import time

import pytest

from goliath_link import client as cl
from goliath_link import policy as pol
from goliath_link.gcode import clean_gcode
from goliath_link.protocol import Commands
from goliath_link.sim import SimulatedGoliath
from goliath_link.transport import GoliathTransport

LINK = dict(connect_gaps=(0.05, 0.05), settle=0.1, timeout=2.0)
JOB = "G00 X0 Y0 ;start\nG01 X10 Y5 Z-1 C315 F1500\nG01 X20 Y5\nM05\n"
UP = dict(block_interval=0, continue_wait=0.05, header_wait=0.1, done_timeout=5.0,
          info8_grace=0.5)
READS = ["GET GLOBAL SYS STATUS", "PRINT SYS REPORT", "GET SPINDLE CAGE STATUS",
         "READ INDEX CALIBRATION", "READ SPINDLE CALIB"]


@pytest.fixture
def start_sim():
    sims = []

    def start(profile="gfw-01.02.00", **kw):
        kw.setdefault("work_line_delay", 0.002)
        s = SimulatedGoliath(profile=profile, **kw)
        s.start()
        sims.append(s)
        return s

    yield start
    for s in sims:
        s.stop()
        assert s.errors == [], s.errors


@pytest.fixture
def link():
    opened = []

    def open_link(sim, **client_kw):
        t = GoliathTransport("127.0.0.1", sim.port, **LINK)
        t.connect()
        kw = dict(command_interval=0.05)
        kw.update(client_kw)
        c = cl.GoliathClient(t, **kw)
        opened.append((t, c))
        return t, c

    yield open_link
    for t, c in opened:
        t.close()
        c.close()


class AllowAll:
    """A (bad) policy that allows everything, and records what it was asked."""

    def __init__(self):
        self.asked = []

    def check(self, command):
        self.asked.append(command)
        return pol.PolicyDecision(True, "allow-all test policy")


def settle(sim, wait=0.15):
    """Give anything already on the wire time to reach the simulator's log."""
    time.sleep(wait)
    return list(sim.received)


# --------------------------------------------------------------------------- #
# The denylist: always on, before everything else
# --------------------------------------------------------------------------- #


def test_denylisted_commands_never_reach_the_wire(start_sim, link):
    sim = start_sim()
    allow_all = AllowAll()
    t, c = link(sim, command_policy=allow_all)
    c.firmware_version = (1, 2, 3)  # newer: the firmware gate would let anything through
    calls = (
        lambda cmd: c.send(cmd),
        lambda cmd: c.send(cmd, allow_unsupported=True),
        lambda cmd: c.request(cmd, lambda e: True, timeout=0.5),
    )
    names = sorted(pol.NEVER_SEND - {""})
    commands = [spelled for name in names
                for spelled in (name, name.lower() + ";1", " " + name + "\r\n", "INFO\n" + name)]
    commands += ["", " ", "\r\n", ";"]  # the empty name (spec 09 section 3, record 88)
    for command in commands:
        for call in calls:
            with pytest.raises(pol.CommandRefusedError) as ei:
                call(command)
            assert ei.value.layer == "denylist", command
            assert ei.value.decision.allowed is False
    assert allow_all.asked == []  # the denylist answers first: the policy is never asked
    assert settle(sim) == []
    assert len(commands) == 4 * len(names) + 4 and len(names) == len(pol.NEVER_SEND) - 1
    c.send("INFO")  # the link itself works
    assert sim.wait_for(lambda: list(sim.received) == ["INFO"])


def test_denylist_cannot_be_switched_off(start_sim, link):
    sim = start_sim()
    t, c = link(sim, strict_commands=False, command_policy=None)
    with pytest.raises(pol.CommandRefusedError, match="spec 09"):
        c.send("EFLASH ERASE")
    with pytest.raises(pol.CommandRefusedError):
        c.send("LOAD;100;")
    assert settle(sim) == []


def test_denylist_also_checks_the_terminator(start_sim, link):
    sim = start_sim()
    t, c = link(sim, terminator="\nMOVE Z")  # a nonsense terminator must not smuggle a command
    with pytest.raises(pol.CommandRefusedError) as ei:
        c.send("INFO")
    assert ei.value.layer == "denylist"
    assert settle(sim) == []


def test_refusals_are_logged(start_sim, link, caplog):
    sim = start_sim()
    t, c = link(sim, command_policy=pol.PhasePolicy("PREFLIGHT", READS))
    caplog.set_level(logging.WARNING, logger="goliath_link.client")
    with pytest.raises(pol.CommandRefusedError):
        c.send("ZERO SET")
    with pytest.raises(pol.CommandRefusedError):
        c.send("SET PANEL SIZE;1;2")
    text = caplog.text
    assert "ZERO SET" in text and "denylist" in text
    assert "SET PANEL SIZE;1;2" in text and "PREFLIGHT" in text


# --------------------------------------------------------------------------- #
# The strict set
# --------------------------------------------------------------------------- #


def test_strict_refuses_unknown_names(start_sim, link):
    sim = start_sim("vendor-app-newer")
    probe = AllowAll()
    t, c = link(sim, strict_commands=True, command_policy=probe)
    c.firmware_version = (1, 2, 3)  # newer: the firmware gate would allow ABORT WORK etc.
    assert c.strict_commands is True
    for command in ("FOO", "ABORT WORK", "GCODE CONTINUE;0", "GET Z BACKLASH", "SEND TCP POS",
                    "CMD SYS RESET", "SSID=4D79&PWD=5365637265&AUTH_TYPE=2", " INFO", "INFO\n",
                    "GET  GLOBAL SYS STATUS"):
        with pytest.raises(pol.CommandRefusedError) as ei:
            c.send(command)
        assert ei.value.layer == "strict", command
    with pytest.raises(pol.CommandRefusedError) as ei:
        c.send("MOVE Z")
    assert ei.value.layer == "denylist"  # the denylist answers before the strict set
    assert probe.asked == []  # the strict set answers before the policy
    assert settle(sim) == []
    c.send("info")  # spec 09 section 1 plus INFO pass (names are case-insensitive)
    c.send("GET GLOBAL SYS STATUS")
    assert sim.wait_for(lambda: list(sim.received) == ["info", "GET GLOBAL SYS STATUS"])
    assert probe.asked == ["info", "GET GLOBAL SYS STATUS"]


def test_strict_refuses_before_the_firmware_gate(start_sim, link):
    sim = start_sim()
    t, c = link(sim, strict_commands=True)
    c.firmware_version = (1, 2, 0)  # the gate would also refuse ABORT WORK here
    with pytest.raises(pol.CommandRefusedError) as ei:
        c.send("ABORT WORK", allow_unsupported=True)
    assert ei.value.layer == "strict"
    assert settle(sim) == []


def test_strict_client_cannot_upload_to_newer_firmware(start_sim, link):
    """Pinned: GCODE CONTINUE (sent before every upload on firmware newer than
    01.02.00) is outside spec 09 section 1, so a strict client refuses the
    whole upload before any byte goes out."""
    sim = start_sim("vendor-app-newer")
    t, c = link(sim, strict_commands=True)
    c.firmware_version = (1, 2, 3)
    with pytest.raises(pol.CommandRefusedError, match="GCODE CONTINUE") as ei:
        c.upload_gcode(JOB, **UP)
    assert ei.value.layer == "strict"
    assert settle(sim) == [] and sim.program is None
    assert c.upload_hazard is None


def test_strict_client_uploads_to_gfw_01_02_00(start_sim, link):
    sim = start_sim()
    t, c = link(sim, strict_commands=True)
    c.learn_firmware_version(timeout=2.0)
    report = c.upload_gcode(JOB, confirm="info8-or-info0", **UP)
    assert report.confirmed
    assert sim.program == clean_gcode(JOB)


def test_strict_client_refuses_a_software_stop(start_sim, link):
    sim = start_sim("vendor-app-newer")
    t, c = link(sim, strict_commands=True)
    c.firmware_version = (1, 2, 3)
    with pytest.raises(pol.CommandRefusedError) as ei:
        c.stop_work(timeout=1.0)
    assert ei.value.layer == "strict"
    assert settle(sim) == []


@pytest.mark.parametrize("bad", [1, "yes", None])
def test_strict_commands_must_be_a_bool(start_sim, link, bad):
    sim = start_sim()
    with pytest.raises(TypeError):
        link(sim, strict_commands=bad)


# --------------------------------------------------------------------------- #
# The per-phase CommandPolicy
# --------------------------------------------------------------------------- #


def test_policy_refusal_puts_nothing_on_the_wire(start_sim, link):
    sim = start_sim()
    t, c = link(sim, command_policy=pol.PhasePolicy("PREFLIGHT", READS))
    for call in (lambda: c.set_panel_size(1200, 800), lambda: c.set_rf_channels(101, 114),
                 lambda: c.send("INFO"), lambda: c.learn_firmware_version(timeout=1.0),
                 lambda: c.enter_jog(), lambda: c.goto(1, 2, 0, 90),
                 lambda: c.send("GET Z BACKLASH", allow_unsupported=True)):
        with pytest.raises(pol.CommandRefusedError) as ei:
            call()
        assert ei.value.layer == "policy"
        assert "PREFLIGHT" in str(ei.value)
    with pytest.raises(pol.CommandRefusedError):
        c.upload_gcode(JOB, confirm="info8-or-info0", **UP)
    assert settle(sim) == []  # nothing reached the simulator
    assert sim.program is None and c.upload_hazard is None
    assert c.firmware_version is None  # INFO never went out


def test_policy_refuses_before_the_firmware_gate(start_sim, link):
    sim = start_sim()
    t, c = link(sim, command_policy=pol.PhasePolicy("PREFLIGHT", READS))
    c.firmware_version = (1, 2, 0)  # the gate would refuse GET Z BACKLASH too
    with pytest.raises(pol.CommandRefusedError):
        c.send("GET Z BACKLASH")


def test_policy_binds_arguments(start_sim, link):
    sim = start_sim()
    t, c = link(sim)
    c.set_command_policy(pol.PhasePolicy("COMMIT", [pol.bind("SET PANEL SIZE", 1200, 800),
                                                   "PRINT SYS REPORT"]))
    for x, y in ((1201, 800), (800, 1200), (1200, 799)):
        with pytest.raises(pol.CommandRefusedError, match="SET PANEL SIZE;1200;800"):
            c.set_panel_size(x, y)
    with pytest.raises(pol.CommandRefusedError):
        c.send("SET PANEL SIZE;1200;800;0")
    c.set_panel_size(1200, 800)
    c.set_command_policy(pol.PhasePolicy("PANEL_DETECTION", [pol.bind("SET T2 POSITION", 1)]))
    with pytest.raises(pol.CommandRefusedError, match="SET T2 POSITION;1"):
        c.send(Commands.set_t2_position(False))
    c.send(Commands.set_t2_position(True))
    assert sim.wait_for(lambda: len(sim.received) >= 2)
    assert settle(sim) == ["SET PANEL SIZE;1200;800", "SET T2 POSITION;1"]


def test_upload_is_checked_before_anything_is_sent(start_sim, link):
    sim = start_sim()
    t, c = link(sim)
    c.learn_firmware_version(timeout=2.0)
    size = len(clean_gcode(JOB))
    c.set_command_policy(pol.PhasePolicy("UPLOADING", [pol.bind("GCODE UPLOAD", size + 1)]))
    with pytest.raises(pol.CommandRefusedError, match="GCODE UPLOAD;%d" % (size + 1)):
        c.upload_gcode(JOB, confirm="info8-or-info0", **UP)
    assert settle(sim) == ["INFO"] and sim.program is None
    assert c.upload_hazard is None
    # The refused upload did not count as an upload (no restart demanded).
    c.set_command_policy(pol.PhasePolicy("UPLOADING", [pol.bind("GCODE UPLOAD", size)]))
    report = c.upload_gcode(JOB, confirm="info8-or-info0", **UP)
    assert report.confirmed and sim.program == clean_gcode(JOB)


def test_upload_to_newer_firmware_checks_every_command_first(start_sim, link):
    sim = start_sim("vendor-app-newer")
    t, c = link(sim)
    c.firmware_version = (1, 2, 3)
    size = len(clean_gcode(JOB))
    # GCODE UPLOAD is allowed, GCODE CONTINUE;0 is not: nothing at all is sent
    # (not even the GCODE CONTINUE;0 that would come first).
    c.set_command_policy(pol.PhasePolicy("UPLOADING", [pol.bind("GCODE UPLOAD", size)]))
    with pytest.raises(pol.CommandRefusedError, match="GCODE CONTINUE"):
        c.upload_gcode(JOB, **UP)
    assert settle(sim) == []
    c.set_command_policy(pol.PhasePolicy("UPLOADING", [pol.bind("GCODE CONTINUE", 0),
                                                      pol.bind("GCODE UPLOAD", size)]))
    assert c.upload_gcode(JOB, **UP).confirmed_by == "INFO;8"
    assert sim.received[-2:] == ["GCODE CONTINUE;0", "GCODE UPLOAD;%d" % size]


def test_stop_work_is_checked_against_the_policy(start_sim, link):
    sim = start_sim("vendor-app-newer")
    t, c = link(sim, command_policy=pol.PhasePolicy("RUNNING", []))
    c.firmware_version = (1, 2, 3)
    with pytest.raises(pol.CommandRefusedError, match="RUNNING"):
        c.stop_work(timeout=1.0)
    assert settle(sim) == []
    c.set_command_policy(pol.PhasePolicy("RUNNING", ["ABORT WORK"]))
    with pytest.raises(cl.StopRejectedError):  # idle robot: ERR;1, not a claimed stop
        c.stop_work(timeout=2.0)
    assert sim.received == ["ABORT WORK"]


def test_policy_swap_during_upload_refused(start_sim, link):
    sim = start_sim()
    t, c = link(sim)
    c.learn_firmware_version(timeout=2.0)
    program = "".join(f"G01 X{i}.000 Y{i}.000 F1500\n" for i in range(60))
    size = len(clean_gcode(program))
    uploading = pol.PhasePolicy("UPLOADING", [pol.bind("GCODE UPLOAD", size)])
    c.set_command_policy(uploading)
    result = {}

    def upload():
        try:
            result["report"] = c.upload_gcode(program, block_size=64, block_interval=0.05,
                                              confirm="info8-or-info0",
                                              **{k: v for k, v in UP.items()
                                                 if k != "block_interval"})
        except BaseException as exc:  # noqa: BLE001 - checked below
            result["error"] = exc

    th = threading.Thread(target=upload)
    th.start()
    try:
        assert sim.wait_for_event("upload-start", timeout=3.0)
        assert c.upload_in_progress
        for replacement in (pol.PhasePolicy("RUNNING", []), None, uploading):
            with pytest.raises(pol.PolicySwapRefusedError, match="upload"):
                c.set_command_policy(replacement)
            assert c.command_policy is uploading
    finally:
        th.join(15.0)
    assert "error" not in result, result.get("error")
    assert result["report"].confirmed
    assert not c.upload_in_progress
    running = pol.PhasePolicy("RUNNING", [])
    c.set_command_policy(running)  # allowed again once the upload has returned
    assert c.command_policy is running


def test_failed_upload_releases_the_swap_guard(start_sim, link):
    sim = start_sim()
    t, c = link(sim, command_policy=pol.PhasePolicy("PREFLIGHT", READS))
    with pytest.raises(pol.CommandRefusedError):
        c.upload_gcode(JOB, **UP)
    assert not c.upload_in_progress
    c.set_command_policy(None)
    assert c.command_policy is None


def test_a_swap_while_a_send_waits_is_rechecked(start_sim, link):
    """A command checked under one policy and still waiting for the send lock
    is checked again under the policy in force when it is about to go out."""
    sim = start_sim()
    checked = threading.Event()

    class Recording(pol.PhasePolicy):
        def check(self, command):
            d = super().check(command)
            checked.set()
            return d

    t, c = link(sim, command_policy=Recording("PREFLIGHT", READS))
    result = {}

    def send():
        try:
            c.send("GET GLOBAL SYS STATUS")
            result["sent"] = True
        except BaseException as exc:  # noqa: BLE001 - checked below
            result["error"] = exc

    t.send_lock.acquire()
    try:
        th = threading.Thread(target=send)
        th.start()
        assert checked.wait(3.0)  # the pre-check passed under PREFLIGHT
        time.sleep(0.05)
        c.set_command_policy(pol.PhasePolicy("RUNNING", []))  # does not need the send lock
    finally:
        t.send_lock.release()
    th.join(5.0)
    assert isinstance(result.get("error"), pol.CommandRefusedError), result
    assert "RUNNING" in str(result["error"])
    assert settle(sim) == []


class Broken:
    def __init__(self, answer):
        self.answer = answer

    def check(self, command):
        return self.answer


@pytest.mark.parametrize("answer", [True, None, "yes", (True, "ok")])
def test_a_policy_must_answer_with_a_decision(start_sim, link, answer):
    sim = start_sim()
    t, c = link(sim, command_policy=Broken(answer))
    with pytest.raises(pol.CommandRefusedError, match="PolicyDecision") as ei:
        c.send("INFO")
    assert ei.value.layer == "policy"
    assert settle(sim) == []


def test_a_policy_exception_propagates_and_nothing_is_sent(start_sim, link):
    class Exploding:
        def check(self, command):
            raise RuntimeError("policy bug")

    sim = start_sim()
    t, c = link(sim, command_policy=Exploding())
    with pytest.raises(RuntimeError, match="policy bug"):
        c.send("INFO")
    assert settle(sim) == []


@pytest.mark.parametrize("bad", ["INFO", 42, object(), pol.PolicyDecision(True, "x")])
def test_set_command_policy_needs_a_policy(start_sim, link, bad):
    sim = start_sim()
    t, c = link(sim)
    with pytest.raises(TypeError):
        c.set_command_policy(bad)
    with pytest.raises(TypeError):
        cl.GoliathClient(t, command_policy=bad)
    assert c.command_policy is None


def test_no_policy_keeps_the_previous_behaviour(start_sim, link):
    sim = start_sim()
    t, c = link(sim)
    assert c.command_policy is None and c.strict_commands is False
    c.learn_firmware_version(timeout=2.0)
    c.set_rf_channels(101, 114)
    c.set_panel_size(1200, 800)
    with pytest.raises(cl.CommandNotSupportedError):  # the firmware gate still applies
        c.send("GET Z BACKLASH")
    assert sim.wait_for(lambda: len(sim.received) >= 3)
    assert settle(sim) == ["INFO", "SET RF CHANNELS;101;114", "SET PANEL SIZE;1200;800"]
