# SPDX-License-Identifier: Apache-2.0
"""Cross-unit check: the probe CLI against the simulator CLI, as real processes.

This is the owner's dry run before the real robot:

    python -m goliath_link.sim --profile gfw-01.02.00
    python -m goliath_link.probe --host 127.0.0.1 --port <port>

Each side runs in its own interpreter (``sys.executable -m ...``), so both
``-m`` entry points, the CLI argument wiring, the probe's process exit code and
the simulator's hazard flags are exercised end to end. The simulator's event
log (its stdout) is parsed for every command it received, to check across the
process boundary that only allowlisted probe payloads reached the "robot".

Everything listens and connects on 127.0.0.1 only. The simulator is a model of
GFW 01.02.00, not the robot: passing here proves nothing about the hardware.
"""
import ast
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

import goliath_link
from goliath_link.probe import READ_ONLY_COMMANDS, SWEEP_ORDER

HOST = "127.0.0.1"
SRC = str(Path(goliath_link.__file__).resolve().parents[1])
#: Fast probe timings (all within the CLI's validated ranges).
FAST_PROBE = ["--listen", "0.2", "--reply-timeout", "1", "--pace", "0.3",
              "--slot-release-wait", "0.3"]
PROBE_PAYLOADS = {c + t for c in READ_ONLY_COMMANDS for t in ("", "\n", "\r\n")}
RECEIVED_RE = re.compile(r"^\[sim\] <- conn=\d+ slot=\S+ (.*)$")


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_module(module: str, args, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", module, *args], env=_env(), timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="backslashreplace",
    )


class SimProcess:
    """``python -m goliath_link.sim`` on a free localhost port, log to a file."""

    def __init__(self, log_path: Path, *sim_args: str) -> None:
        self.log_path = log_path
        self._log = open(log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "goliath_link.sim", "--host", HOST, "--port", "0",
             "--duration", "180", *sim_args],
            stdout=self._log, stderr=subprocess.STDOUT, env=_env(),
        )
        self.port = self._wait_for_port()

    def _wait_for_port(self) -> int:
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            m = re.search(r"listening on 127\.0\.0\.1:(\d+)", self.log())
            if m:
                return int(m.group(1))
            if self.proc.poll() is not None:
                break
            time.sleep(0.05)
        self.stop()
        raise AssertionError(f"simulator did not start; its output:\n{self.log()}")

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="backslashreplace")

    def received(self):
        """Every command buffer the simulator logged as received, decoded."""
        out = []
        for line in self.log().splitlines():
            m = RECEIVED_RE.match(line)
            if m:
                out.append(ast.literal_eval(m.group(1)))
        return out

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self._log.close()


@pytest.fixture
def sim_process(tmp_path):
    started = []

    def start(*sim_args: str) -> SimProcess:
        sp = SimProcess(tmp_path / f"sim-{len(started)}.log", *sim_args)
        started.append(sp)
        return sp

    yield start
    for sp in started:
        sp.stop()


def _probe(port: int, out_dir: Path) -> subprocess.CompletedProcess:
    out_dir.mkdir()
    return _run_module(
        "goliath_link.probe",
        ["--host", HOST, "--port", str(port), "--out", str(out_dir), *FAST_PROBE],
        timeout=150,
    )


def _report(out_dir: Path) -> dict:
    files = [p for p in out_dir.glob("goliath-probe-*.json") if not p.name.endswith(".shareable.json")]
    assert len(files) == 1, files
    # Next to it, the copy that may be shared (serial number and Wi-Fi names masked).
    assert files[0].with_name(files[0].name[: -len(".json")] + ".shareable.json").is_file()
    return json.loads(files[0].read_text(encoding="utf-8"))


@pytest.mark.parametrize("module,expect", [
    ("goliath_link.probe", ["READ-ONLY", "--extended", "--slot-release-wait"]),
    ("goliath_link.sim", ["--profile", "gfw-01.02.00", "--occupied-slots"]),
])
def test_cli_help_exits_zero(module, expect):
    res = _run_module(module, ["--help"], timeout=60)
    assert res.returncode == 0, res.stdout
    for text in expect:
        assert text in res.stdout


def test_probe_cli_against_gfw_sim_cli_succeeds(sim_process, tmp_path):
    sim = sim_process("--profile", "gfw-01.02.00")
    res = _probe(sim.port, tmp_path / "out")
    sim.stop()
    assert res.returncode == 0, res.stdout + "\n--- sim ---\n" + sim.log()
    assert "OK (exit code 0)" in res.stdout
    report = _report(tmp_path / "out")
    assert report["result"] == "ok" and report["exit_code"] == 0
    assert report["errors"] == [] and report["warnings"] == []
    assert report["firmware_version"] == "01.02.00"
    assert report["chosen_terminator"] == "none"
    assert [s["command"] for s in report["sweep"]] == list(SWEEP_ORDER)
    assert all(s["complete"] for s in report["sweep"]), report["sweep"]
    received = sim.received()
    assert received, "the simulator logged no commands"
    assert set(received) <= PROBE_PAYLOADS, sorted(set(received) - PROBE_PAYLOADS)


@pytest.mark.parametrize("slot,diagnosis", [
    ("1", ["another client", "S2", "power-cycle"]),
    ("0", ["SLOT ROUTING", "another client", "S1"]),
])
def test_probe_cli_against_occupied_slot_sim_fails_with_a_diagnosis(
        sim_process, tmp_path, slot, diagnosis):
    sim = sim_process("--profile", "gfw-01.02.00", "--occupied-slots", slot)
    res = _probe(sim.port, tmp_path / "out")
    sim.stop()
    assert "to-other-client" in sim.log(), "scenario: a reply must have gone to the other client"
    assert res.returncode == 1, res.stdout
    assert "FAILED (exit code 1)" in res.stdout
    for text in diagnosis:
        assert text in res.stdout, (text, res.stdout)
    report = _report(tmp_path / "out")
    assert report["result"] == "failed" and report["exit_code"] == 1
    errs = " ".join(report["errors"])
    for text in diagnosis:
        assert text in errs, (text, report["errors"])
    assert set(sim.received()) <= PROBE_PAYLOADS
