# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the read-only probe's safety gate and CLI validation.

Nothing here talks to a robot. The only sockets are to 127.0.0.1, on a port
with nothing listening (so even a regression that skipped validation cannot
reach anything).
"""
import ast
import inspect
import socket

import pytest

from goliath_link import probe
from goliath_link.probe import (
    EXTENDED_COMMANDS,
    READ_ONLY_COMMANDS,
    SWEEP_ORDER,
    UnsafeCommandError,
    _checked_send,
    _FirmwareGate,
    main,
    run_probe,
)
from goliath_link.protocol import parse_text_message

#: The six commands whose GFW 01.02.00 handlers were verified read-only
#: (firmware facts brief, section 4).
VERIFIED_SIX = (
    "INFO",
    "GET GLOBAL SYS STATUS",
    "PRINT SYS REPORT",
    "GET SPINDLE CAGE STATUS",
    "READ INDEX CALIBRATION",
    "READ SPINDLE CALIB",
)


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Recording:
    """Stands in for the transport: records what reaches send()."""

    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(data)


class Boom:
    def send(self, _):  # must never be reached
        raise AssertionError("unsafe command reached the transport")


def gate_for(fw):
    """A firmware gate that has seen one identity line stating ``fw`` (None: no line yet)."""
    gate = _FirmwareGate()
    if fw is not None:
        gate.observe(parse_text_message("INFO;34;GFW {:02d}.{:02d}.{:02d}   ;1;2;000000000001".format(*fw)))
    return gate


#: The firmware these unit tests assume, where the firmware gate is not what they test.
GFW_01_02_00 = (1, 2, 0)


# --------------------------------------------------------------------------- #
# SAFE-1: the allowlist
# --------------------------------------------------------------------------- #


def test_default_allowlist_is_exactly_the_six_verified_commands():
    assert READ_ONLY_COMMANDS == frozenset(VERIFIED_SIX)
    assert tuple(SWEEP_ORDER) == VERIFIED_SIX


def test_extended_commands_are_only_backlash_and_tcp_pos():
    assert EXTENDED_COMMANDS == frozenset({"GET Z BACKLASH", "SEND TCP POS"})
    assert not (EXTENDED_COMMANDS & READ_ONLY_COMMANDS)


def test_z_plate_offset_is_in_no_allowlist():
    assert "GET Z PLATE OFFSET" not in READ_ONLY_COMMANDS
    assert "GET Z PLATE OFFSET" not in EXTENDED_COMMANDS
    with pytest.raises(UnsafeCommandError):
        _checked_send(Boom(), "GET Z PLATE OFFSET", "", gate=gate_for((9, 9, 9)),
                      extended_firmware=(9, 9, 9))


@pytest.mark.parametrize(
    "unsafe",
    ["GOTO TARGET;0;0;0;0", "ENABLE SPINDLE", "GCODE UPLOAD;10", "CMD SYS RESET",
     "SET Z BACKLASH;0", "ENTER JOG", "GET SSIDS", "INFO;34", "SYS RESET", "LOAD",
     "MOVE Z", "GET Z PLATE OFFSET", "info", "INFO ", "INFO\n",
     # extended commands without the explicit firmware permission
     "GET Z BACKLASH", "SEND TCP POS"],
)
def test_checked_send_refuses_unsafe(unsafe):
    with pytest.raises(UnsafeCommandError):
        _checked_send(Boom(), unsafe, "", gate=gate_for(GFW_01_02_00))


@pytest.mark.parametrize("cmd", VERIFIED_SIX)
@pytest.mark.parametrize("term", ["", "\n", "\r\n"])
def test_checked_send_passes_the_six_with_each_terminator(cmd, term):
    rec = Recording()
    _checked_send(rec, cmd, term, gate=gate_for(GFW_01_02_00))
    assert rec.sent == [cmd + term]


@pytest.mark.parametrize("term", [";", "\r", "\n\n", " ", "\x00", "\nGOTO TARGET"])
def test_checked_send_refuses_bad_terminators(term):
    with pytest.raises(UnsafeCommandError):
        _checked_send(Boom(), "INFO", term, gate=gate_for(GFW_01_02_00))


@pytest.mark.parametrize("fw", [None, (1, 2, 0), (1, 1, 99), (0, 9, 0)])
@pytest.mark.parametrize("cmd", sorted(EXTENDED_COMMANDS))
def test_extended_commands_refused_unless_firmware_is_newer_than_01_02_00(cmd, fw):
    # GFW 01.02.00 does not have them; unknown firmware must not get them either.
    with pytest.raises(UnsafeCommandError):
        _checked_send(Boom(), cmd, "", gate=gate_for(fw), extended_firmware=fw)


@pytest.mark.parametrize("fw", [(1, 2, 1), (1, 2, 3), (2, 0, 0)])
@pytest.mark.parametrize("cmd", sorted(EXTENDED_COMMANDS))
def test_extended_commands_pass_on_newer_firmware(cmd, fw):
    rec = Recording()
    _checked_send(rec, cmd, "", gate=gate_for(fw), extended_firmware=fw)
    assert rec.sent == [cmd]


def test_checked_send_is_the_only_path_to_the_socket():
    """No other function in probe.py may call send/send_bytes/sendall."""
    tree = ast.parse(inspect.getsource(probe))
    offenders = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in ("send", "send_bytes", "sendall", "sendto",
                                                            "sendmsg"):
                if self.stack[-1:] != ["_checked_send"]:
                    offenders.append((node.lineno, ".".join(self.stack), f.attr))
            self.generic_visit(node)

    Visitor().visit(tree)
    assert offenders == [], f"socket writes outside _checked_send: {offenders}"


# --------------------------------------------------------------------------- #
# SAFE-2: argument validation (argparse errors, nothing is connected)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "flag,value",
    [("--pace", "0"), ("--pace", "-1"), ("--pace", "0.29"), ("--pace", "nan"), ("--pace", "inf"),
     ("--listen", "0"), ("--listen", "-2"), ("--listen", "nan"), ("--listen", "inf"),
     ("--reply-timeout", "0"), ("--reply-timeout", "-0.5"), ("--reply-timeout", "nan"),
     ("--slot-release-wait", "-1"), ("--slot-release-wait", "nan")],
)
def test_cli_rejects_bad_timing_values(flag, value, tmp_path, capsys):
    argv = ["--host", "127.0.0.1", "--port", str(closed_port()), "--out", str(tmp_path),
            flag, value]
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2  # argparse usage error, before anything is sent
    err = capsys.readouterr().err
    assert flag in err
    assert not list(tmp_path.iterdir()), "nothing may run (or be written) on a usage error"


def test_checked_send_needs_a_firmware_gate():
    with pytest.raises(TypeError):
        _checked_send(Boom(), "INFO", "")  # the gate is a required argument
    with pytest.raises(UnsafeCommandError, match="gate"):
        _checked_send(Boom(), "INFO", "", gate=None)


def test_cli_accepts_minimum_pace(tmp_path, capsys):
    # 0.3 s is the documented minimum and must be accepted (the run itself
    # fails: nothing listens on the port).
    argv = ["--host", "127.0.0.1", "--port", str(closed_port()), "--out", str(tmp_path),
            "--pace", "0.3", "--listen", "0.1", "--reply-timeout", "0.1",
            "--slot-release-wait", "0", "--terminator", "none"]
    assert main(argv) == 1


@pytest.mark.parametrize(
    "kw",
    [dict(pace=0), dict(pace=0.1), dict(pace=float("nan")), dict(listen=0), dict(listen=-1),
     dict(reply_timeout=0), dict(reply_timeout=float("inf")), dict(slot_release_wait=-0.1)],
)
def test_run_probe_rejects_bad_timing_values(kw, tmp_path):
    with pytest.raises(ValueError):
        run_probe("127.0.0.1", closed_port(), str(tmp_path), **kw)
    assert not list(tmp_path.iterdir())


# --------------------------------------------------------------------------- #
# PH-4: --port is range-checked (glibc wraps 70000 to 4464 silently)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["70000", "65536", "0", "-1", "80.5", "eighty"])
def test_cli_rejects_out_of_range_port(value, tmp_path, capsys):
    argv = ["--host", "127.0.0.1", "--port", value, "--out", str(tmp_path)]
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2  # usage error, before anything is sent
    err = capsys.readouterr().err
    assert "--port" in err
    assert not list(tmp_path.iterdir()), "nothing may run (or be written) on a usage error"


@pytest.mark.parametrize("port", [0, -1, 65536, 70000, True, 80.0, "80", None])
def test_run_probe_rejects_invalid_port(port, tmp_path):
    with pytest.raises(ValueError, match="port"):
        run_probe("127.0.0.1", port, str(tmp_path), terminator="none")
    assert not list(tmp_path.iterdir())


def test_cli_accepts_port_bounds():
    ap = probe._build_parser()
    assert ap.parse_args(["--port", "1"]).port == 1
    assert ap.parse_args(["--port", "65535"]).port == 65535
