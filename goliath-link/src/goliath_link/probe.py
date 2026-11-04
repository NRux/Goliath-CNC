# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""Read-only first-contact probe for a real Goliath robot.

    python -m goliath_link.probe                 # robot's own Wi-Fi AP (10.0.0.1:80)
    python -m goliath_link.probe --host 192.168.1.50

What it does, in order:

1. **Passive listen** for a few seconds after connecting, to record anything the
   robot sends unprompted (status frames, INFO pushes).
2. **Terminator test.** On a fresh connection per variant, sends the query
   ``INFO`` with no terminator, then ``\\n``, then ``\\r\\n``, and records which
   ones get an answer. This settles how the robot frames inbound commands.
3. **Read-only sweep.** With the first terminator that worked, sends each query
   in :data:`READ_ONLY_COMMANDS`, paced well apart, recording every reply.
4. Writes a **raw byte capture** (every byte sent and received, timestamped, in
   hex and ASCII) and a **JSON report**, and prints a summary.

**Safety.** The probe can only send the queries listed in
:data:`READ_ONLY_COMMANDS`; every send goes through :func:`_checked_send`, which
refuses anything else. It never sends motion, jog, spindle, aspiration,
calibration-write, G-code, reset, firmware or Wi-Fi-provisioning commands. It
deliberately omits ``GET SSIDS`` because a Wi-Fi scan could disturb the very
link the probe is using.

Even so: run it with the bit removed and the robot on blocks. The probe is
read-only by construction, but this is the first time anything other than the
vendor app has spoken to the machine.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

from .client import parse_firmware_version
from .protocol import GlobalStatusFrame, PositionFrame, TextMessage, TowerFrame
from .transport import GoliathTransport, TransportClosed, _default_opener

#: The ONLY commands the probe may send. All are queries; none changes state.
READ_ONLY_COMMANDS: frozenset[str] = frozenset(
    {
        "INFO",
        "GET GLOBAL SYS STATUS",
        "PRINT SYS REPORT",
        "GET SPINDLE CAGE STATUS",
        "GET Z BACKLASH",
        "GET Z PLATE OFFSET",
        "READ INDEX CALIBRATION",
        "READ SPINDLE CALIB",
        "SEND TCP POS",
    }
)

#: Order used for the sweep (INFO first, the rest roughly cheapest-first).
SWEEP_ORDER: tuple[str, ...] = (
    "INFO",
    "GET GLOBAL SYS STATUS",
    "PRINT SYS REPORT",
    "GET SPINDLE CAGE STATUS",
    "READ INDEX CALIBRATION",
    "READ SPINDLE CALIB",
    "GET Z BACKLASH",
    "GET Z PLATE OFFSET",
    "SEND TCP POS",
)

TERMINATORS: tuple[tuple[str, str], ...] = (("none", ""), ("lf", "\n"), ("crlf", "\r\n"))

assert set(SWEEP_ORDER) == READ_ONLY_COMMANDS


class UnsafeCommandError(RuntimeError):
    """Raised if anything tries to send a non-allowlisted command through the probe."""


def _checked_send(transport: GoliathTransport, command: str, terminator: str) -> None:
    if command not in READ_ONLY_COMMANDS:
        raise UnsafeCommandError(f"probe refuses to send non-read-only command: {command!r}")
    if terminator not in ("", "\n", "\r\n"):
        raise UnsafeCommandError(f"unexpected terminator {terminator!r}")
    transport.send(command + terminator)


# --------------------------------------------------------------------------- #
# Raw capture
# --------------------------------------------------------------------------- #


class _CaptureLog:
    """Thread-safe, timestamped log of every byte sent/received."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._t0 = time.monotonic()
        self._lock = threading.Lock()
        self._fh = open(path, "w", encoding="utf-8")
        self._fh.write("# goliath_link.probe raw capture. '>' = sent to robot, '<' = received.\n")
        self._fh.write("# columns: seconds_since_start  direction  connection  hex  |  ascii\n")

    def note(self, text: str) -> None:
        with self._lock:
            self._fh.write(f"# {time.monotonic() - self._t0:9.3f}  {text}\n")
            self._fh.flush()

    def record(self, direction: str, conn: str, data: bytes) -> None:
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
        with self._lock:
            self._fh.write(
                f"{time.monotonic() - self._t0:9.3f}  {direction}  {conn}  {data.hex(' ')}  |  {ascii_}\n"
            )
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            self._fh.close()


class _CapturingSocket:
    def __init__(self, inner, log: _CaptureLog, conn: str) -> None:
        self._inner = inner
        self._log = log
        self._conn = conn

    def sendall(self, data: bytes) -> None:
        self._log.record(">", self._conn, bytes(data))
        self._inner.sendall(data)

    def recv(self, bufsize: int) -> bytes:
        data = self._inner.recv(bufsize)
        if data:
            self._log.record("<", self._conn, data)
        return data

    def shutdown(self, how: int) -> None:
        self._inner.shutdown(how)

    def close(self) -> None:
        self._inner.close()


# --------------------------------------------------------------------------- #
# Event summaries (JSON-friendly)
# --------------------------------------------------------------------------- #


def summarize(event: object) -> dict:
    if isinstance(event, TextMessage):
        return {"type": "text", "kind": event.kind, "code": event.code,
                "params": event.params, "raw": event.raw}
    if isinstance(event, GlobalStatusFrame):
        return {"type": "global_status", "t1_firmware": list(event.t1_firmware),
                "t2_firmware": list(event.t2_firmware),
                "flags_raw": event.flags_raw.hex(" "), "raw": event.raw.hex(" ")}
    if isinstance(event, PositionFrame):
        return {"type": "position", **asdict(event)}
    if isinstance(event, TowerFrame):
        return {"type": "tower", **asdict(event)}
    if isinstance(event, TransportClosed):
        return {"type": "closed", "error": repr(event.error) if event.error else None}
    return {"type": "unknown", "repr": repr(event)}


def _collect(transport: GoliathTransport, seconds: float, *, until=None, grace: float = 0.5) -> list[object]:
    """Gather every event that arrives within ``seconds``.

    If ``until(event)`` becomes true, keep listening only ``grace`` seconds more
    (to catch trailing messages) instead of the full window.
    """
    out: list[object] = []
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return out
        try:
            ev = transport.incoming.get(timeout=remaining)
        except queue.Empty:
            return out
        out.append(ev)
        if isinstance(ev, TransportClosed):
            return out
        if until is not None and until(ev):
            deadline = min(deadline, time.monotonic() + grace)


def _is_identity_reply(ev: object) -> bool:
    """A reply that identifies the robot: INFO;34 (app info) or INFO;28 (sys info)."""
    return isinstance(ev, TextMessage) and ev.kind == "INFO" and ev.code in (28, 34)


# --------------------------------------------------------------------------- #
# The probe
# --------------------------------------------------------------------------- #


def run_probe(
    host: str,
    port: int,
    out_dir: str,
    *,
    terminator: str = "auto",
    listen: float = 3.0,
    reply_timeout: float = 5.0,
    pace: float = 2.0,
    connect_timeout: float = 10.0,
    opener=_default_opener,
    stamp: Optional[str] = None,
) -> dict:
    """Run the probe and return the report dict (also written to ``out_dir``)."""
    os.makedirs(out_dir, exist_ok=True)
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    capture_path = os.path.join(out_dir, f"goliath-probe-{stamp}.capture.txt")
    report_path = os.path.join(out_dir, f"goliath-probe-{stamp}.json")
    log = _CaptureLog(capture_path)
    conn_counter = [0]

    def make_transport() -> GoliathTransport:
        conn_counter[0] += 1
        name = f"c{conn_counter[0]}"

        def capturing_opener(h: str, p: int, t: float):
            return _CapturingSocket(opener(h, p, t), log, name)

        return GoliathTransport(host, port, timeout=connect_timeout, opener=capturing_opener)

    report: dict = {
        "tool": "goliath_link.probe",
        "started_utc": stamp,
        "host": host,
        "port": port,
        "read_only_commands": sorted(READ_ONLY_COMMANDS),
        "passive": [],
        "terminator_tests": [],
        "chosen_terminator": None,
        "sweep": [],
        "firmware_version": None,
        "tower_firmware": None,
        "errors": [],
        "capture_file": capture_path,
        "report_file": report_path,
    }

    modes = TERMINATORS if terminator == "auto" else tuple(
        m for m in TERMINATORS if m[0] == terminator
    )
    if not modes:
        raise ValueError("terminator must be auto, none, lf or crlf")

    try:
        # 1+2. Passive listen, then the terminator test, each on a fresh connection.
        for i, (name, term) in enumerate(modes):
            log.note(f"terminator test '{name}': connecting")
            t = make_transport()
            try:
                t.connect()
            except OSError as exc:
                log.note(f"connect failed: {exc!r}")
                if i == 0:
                    raise  # reported once, with guidance, by the outer handler
                report["errors"].append(f"connect failed ({name}): {exc!r}")
                continue
            try:
                passive = _collect(t, listen)
                if i == 0:
                    report["passive"] = [summarize(e) for e in passive]
                log.note(f"terminator test '{name}': sending INFO")
                _checked_send(t, "INFO", term)
                replies = _collect(t, reply_timeout, until=_is_identity_reply)
                responded = any(_is_identity_reply(e) for e in replies)
                report["terminator_tests"].append({
                    "mode": name,
                    "responded": responded,
                    "replies": [summarize(e) for e in replies],
                })
                if responded and report["chosen_terminator"] is None:
                    report["chosen_terminator"] = name
                    for e in replies:
                        if _is_identity_reply(e) and e.params and report["firmware_version"] is None:
                            v = parse_firmware_version(" ".join(e.params))
                            if v:
                                report["firmware_version"] = "{0:02d}.{1:02d}.{2:02d}".format(*v)
            finally:
                t.close()
                time.sleep(0.5)  # let the robot release the socket before the next connect

        # 3. Read-only sweep using the first terminator that worked.
        chosen = report["chosen_terminator"]
        if chosen is None:
            report["errors"].append(
                "no terminator variant produced an INFO;28/INFO;34 reply; "
                "sweep skipped — inspect the capture file"
            )
        else:
            term = dict(TERMINATORS)[chosen]
            log.note(f"sweep with terminator '{chosen}': connecting")
            t = make_transport()
            t.connect()
            try:
                _collect(t, 1.0)  # drain connect-time pushes
                for cmd in SWEEP_ORDER:
                    log.note(f"sweep: {cmd}")
                    _checked_send(t, cmd, term)
                    replies = _collect(t, max(pace, reply_timeout if cmd == "INFO" else pace))
                    report["sweep"].append({"command": cmd, "replies": [summarize(e) for e in replies]})
                    for e in replies:
                        if isinstance(e, GlobalStatusFrame) and report["tower_firmware"] is None:
                            report["tower_firmware"] = {
                                "t1": "{0}.{1}.{2}".format(*e.t1_firmware),
                                "t2": "{0}.{1}.{2}".format(*e.t2_firmware),
                            }
                        if isinstance(e, TransportClosed):
                            report["errors"].append(f"connection closed during sweep at {cmd!r}")
                    if any(isinstance(e, TransportClosed) for e in replies):
                        break
                tail = _collect(t, listen)
                if tail:
                    report["sweep"].append({"command": "(trailing)", "replies": [summarize(e) for e in tail]})
            finally:
                t.close()
    except OSError as exc:
        report["errors"].append(
            f"could not connect to {host}:{port}: {exc!r}. Is this computer joined to the "
            "robot's Goliath_xxxxxx Wi-Fi (or on the same network as it)?"
        )
    finally:
        log.close()
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    return report


def _print_summary(r: dict) -> None:
    print()
    print("=" * 66)
    print(" Goliath probe summary")
    print("=" * 66)
    print(f" Target:            {r['host']}:{r['port']}")
    print(f" Firmware:          {r['firmware_version'] or 'unknown'}")
    print(f" Tower firmware:    {r['tower_firmware'] or 'unknown'}")
    print(f" Unprompted msgs:   {len(r['passive'])}")
    for t in r["terminator_tests"]:
        print(f" Terminator {t['mode']:<5}:  {'ANSWERED' if t['responded'] else 'no answer'}"
              f"  ({len(t['replies'])} message(s))")
    print(f" Chosen terminator: {r['chosen_terminator'] or 'NONE WORKED'}")
    for s in r["sweep"]:
        kinds = ", ".join(
            (x.get("raw") or x["type"]) if x["type"] == "text" else x["type"]
            for x in s["replies"]
        ) or "(no reply)"
        print(f"   {s['command']:<24} -> {kinds[:70]}")
    if r["errors"]:
        print(" ERRORS:")
        for e in r["errors"]:
            print(f"   - {e}")
    print(f" Report:  {r['report_file']}")
    print(f" Capture: {r['capture_file']}")
    print("=" * 66)
    print(" Please send both files back. They contain no Wi-Fi passwords, but they DO")
    print(" include your robot's serial number: share them privately, not in a public issue.")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="READ-ONLY first-contact probe for a Goliath robot. "
                    "Sends no motion, spindle, calibration-write, G-code or Wi-Fi commands."
    )
    ap.add_argument("--host", default="10.0.0.1", help="robot address (default: its own AP, 10.0.0.1)")
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--out", default="probe-results", help="directory for the report and capture")
    ap.add_argument("--terminator", choices=["auto", "none", "lf", "crlf"], default="auto")
    ap.add_argument("--listen", type=float, default=3.0, help="passive listen seconds")
    ap.add_argument("--reply-timeout", type=float, default=5.0)
    ap.add_argument("--pace", type=float, default=2.0,
                    help="seconds to wait for replies after each sweep query")
    args = ap.parse_args(argv)

    print("Goliath READ-ONLY probe. Sends only:", ", ".join(SWEEP_ORDER))
    print("Bit removed and robot on blocks? Ctrl+C now if not.\n")
    report = run_probe(
        args.host, args.port, args.out,
        terminator=args.terminator, listen=args.listen,
        reply_timeout=args.reply_timeout, pace=args.pace,
    )
    _print_summary(report)
    return 0 if report["chosen_terminator"] and not report["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
