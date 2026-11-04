# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""A spec-based Goliath robot simulator (a fake robot on TCP).

Run it and point any client — this library, the probe, a future GUI — at it::

    python -m goliath_link.sim --port 8080
    python -m goliath_link.probe --host 127.0.0.1 --port 8080

It implements the behaviour documented in ``docs/protocol/`` closely enough to
exercise a client end to end: the text replies, the three binary telemetry
frames, tower pairing, the G-code upload handshake, and a simulated work cycle
that streams position frames along the uploaded toolpath.

**It is not a faithful model of the firmware.** Where the spec does not pin a
reply's exact text, the simulator uses a plausible stand-in, marked
``# GUESS`` in the source. Never treat "it works against the simulator" as
evidence about real hardware — it only shows a client follows the documented
protocol.

Command framing (how the robot splits inbound bytes into commands) is not yet
confirmed on hardware, so the simulator supports both candidate behaviours:

* ``segment`` — each TCP read is one command (the vendor app sent no
  terminator and relied on this). Trailing CR/LF is stripped.
* ``line`` — commands end with ``\\n`` (CR optional); bytes without a newline
  are buffered indefinitely, i.e. the robot stays silent.
"""

from __future__ import annotations

import argparse
import re
import socket
import threading
import time
from typing import Optional

from .protocol import GlobalStatusFrame, PositionFrame, TowerFrame

_UPLOAD_RE = re.compile(rb"^GCODE UPLOAD;(\d+)")
_COORD_RE = re.compile(r"([XYZC])(-?\d+(?:\.\d+)?)")


class SimulatedGoliath:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        framing: str = "segment",
        firmware: tuple[int, int, int] = (1, 2, 0),
        tower_firmware: tuple[int, int, int] = (1, 2, 0),
        work_line_delay: float = 0.01,
        auto_start_work: bool = True,
        verbose: bool = False,
    ) -> None:
        if framing not in ("segment", "line"):
            raise ValueError("framing must be 'segment' or 'line'")
        self.host = host
        self.port = port
        self.framing = framing
        self.firmware = firmware
        self.tower_firmware = tower_firmware
        self.work_line_delay = work_line_delay
        self.auto_start_work = auto_start_work
        self.verbose = verbose
        #: Every command received, in order (for tests).
        self.received: list[str] = []
        self._lock = threading.Lock()
        self._server: Optional[socket.socket] = None
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._abort = False
        self.pos = [0.0, 0.0, 10.0, 0.0]  # x, y, z, angle

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> tuple[str, int]:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(4)
        srv.settimeout(0.2)
        self._server = srv
        self.port = srv.getsockname()[1]
        t = threading.Thread(target=self._accept_loop, name="sim-accept", daemon=True)
        t.start()
        self._threads.append(t)
        return self.host, self.port

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.close()
        for t in self._threads:
            t.join(timeout=2.0)

    def __enter__(self) -> "SimulatedGoliath":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- server internals ---------------------------------------------------

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (socket.timeout, OSError):
                continue
            conn.settimeout(0.2)
            t = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            t.start()
            self._threads.append(t)

    def _serve(self, conn: socket.socket) -> None:
        send_lock = threading.Lock()

        def send(data: bytes) -> None:
            with send_lock:
                try:
                    conn.sendall(data)
                except OSError:
                    pass

        buf = bytearray()
        upload_remaining = 0
        upload_data = bytearray()
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    return

                # Upload mode: bytes are payload until the announced size is met.
                if upload_remaining > 0:
                    take = chunk[:upload_remaining]
                    upload_data.extend(take)
                    upload_remaining -= len(take)
                    if upload_remaining == 0:
                        self._finish_upload(bytes(upload_data), send)
                    continue

                commands: list[bytes] = []
                if self.framing == "segment":
                    commands = [chunk]
                else:
                    buf.extend(chunk)
                    while b"\n" in buf:
                        line, _, rest = bytes(buf).partition(b"\n")
                        buf[:] = rest
                        commands.append(line)

                for raw in commands:
                    m = _UPLOAD_RE.match(raw)
                    if m:
                        # The header may share a segment with the start of the
                        # payload; anything after the header (+ optional CR/LF)
                        # counts as payload. GUESS: lenient on purpose.
                        size = int(m.group(1))
                        self._record(raw[: m.end()].decode("ascii", "replace"))
                        send(b"INFO;7\n")
                        rest = raw[m.end():]
                        if rest.startswith(b"\r\n"):
                            rest = rest[2:]
                        elif rest[:1] in (b"\n", b"\r"):
                            rest = rest[1:]
                        upload_data = bytearray(rest[:size])
                        upload_remaining = size - len(upload_data)
                        if upload_remaining == 0:
                            self._finish_upload(bytes(upload_data), send)
                        continue
                    cmd = raw.decode("ascii", "replace").strip("\r\n").strip()
                    if not cmd:
                        continue
                    self._record(cmd)
                    if cmd == "CMD SYS RESET":
                        return  # simulate a reboot: drop the connection
                    for reply in self._handle(cmd, send):
                        send(reply)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _record(self, cmd: str) -> None:
        with self._lock:
            self.received.append(cmd)
        if self.verbose:
            print(f"[sim] <- {cmd!r}")

    def _fw(self, v: tuple[int, int, int]) -> str:
        return "{0:02d}.{1:02d}.{2:02d}".format(*v)

    def _position_frame(self, line: int) -> bytes:
        x, y, z, a = self.pos
        return PositionFrame(
            x=x, y=y, z=z, angle=a, gcode_line=line, temperature=38,
            t1_status=0, t1_battery=87.5, t2_status=0, t2_battery=81.25,
        ).encode()

    def _handle(self, cmd: str, send) -> list[bytes]:
        """Return replies for one command (may also start background activity)."""
        parts = cmd.split(";")
        head = parts[0]

        if cmd == "INFO":
            return [
                "INFO;34;GFW {0};HW 1.0;SW {0};SN SIM0001\n".format(
                    self._fw(self.firmware)
                ).encode()
            ]
        if cmd == "GET GLOBAL SYS STATUS":
            return [
                GlobalStatusFrame.build(self.tower_firmware, self.tower_firmware).encode()
            ]
        if cmd == "PRINT SYS REPORT":
            return [b"INFO;40;" + b";".join([b"1"] * 15) + b";END\n"]  # GUESS: exact layout
        if cmd == "GET SPINDLE CAGE STATUS":
            return [b"CAGE STATUS: CLOSED\n"]  # GUESS: exact text; spec only says it contains both
        if cmd == "GET Z BACKLASH":
            return [b"Z const offset: 0.00\n"]
        if cmd == "READ INDEX CALIBRATION":
            return [b"cfg read index :28500\n"]
        if cmd == "READ SPINDLE CALIB":
            return [b"spindle distance: 0.00\n"]
        if cmd == "GET Z PLATE OFFSET":
            return [b"Z plate offset: 0.00\n"]  # GUESS: reply text unknown
        if cmd == "SEND TCP POS":
            return [self._position_frame(0)]  # GUESS: full vs short frame unknown
        if head == "SET RF CHANNELS" and len(parts) == 3:
            return [
                b"INFO;3\n",
                TowerFrame(tower=1, status=0, battery=87.5).encode(),
                TowerFrame(tower=2, status=0, battery=81.25).encode(),
                b"INFO;4\n",
            ]
        if head in ("SET T2 POSITION", "SET PANEL SIZE", "GCODE CONTINUE",
                    "ENABLE SPINDLE", "DISABLE SPINDLE", "ENABLE ASPIRATION",
                    "DISABLE ASPIRATION", "JOG STOP GOLIATH", "EXIT JOG",
                    "ENABLE MANUAL JOG", "DISABLE MANUAL JOG", "FREE Z AXIS",
                    "SET Z BACKLASH", "SET Z PLATE OFFSET", "SET INDEX CALIBRATION VALUE",
                    "SET SPINDLE DISTANCE", "DISCARD TWRS DISTANCE"):
            return []
        if cmd == "ENTER JOG":
            return [b"INFO;35\n"]
        if head == "GOTO TARGET" and len(parts) == 5:
            try:
                self.pos = [float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])]
            except ValueError:
                return [b"ERR;1\n"]
            return [b"INFO;32\n"]
        if cmd == "ABORT WORK":
            self._abort = True
            return [b"INFO;18\n"]
        return [b"ERR;1\n"]  # CMD_NOT_EXECUTED

    def _finish_upload(self, payload: bytes, send) -> None:
        send(b"INFO;8\n")
        send(b"INFO;9\n")
        if self.auto_start_work:
            self._abort = False
            t = threading.Thread(
                target=self._run_work, args=(payload, send), daemon=True
            )
            t.start()
            self._threads.append(t)

    def _run_work(self, payload: bytes, send) -> None:
        send(b"INFO;15\n")
        send(b"INFO;17\n")
        lines = payload.decode("ascii", "replace").splitlines()
        for n, line in enumerate(lines, start=1):
            if self._stop.is_set() or self._abort:
                return
            for axis, value in _COORD_RE.findall(line.upper()):
                idx = "XYZC".index(axis)
                self.pos[idx] = float(value)
            send(self._position_frame(n))
            if self.work_line_delay:
                time.sleep(self.work_line_delay)
        send(b"INFO;18\n")
        send(b"INFO;16\n")
        send(b"INFO;23\n")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Run a spec-based fake Goliath robot on TCP.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--framing", choices=["segment", "line"], default="segment")
    ap.add_argument("--line-delay", type=float, default=0.05,
                    help="seconds per G-code line in the simulated work cycle")
    args = ap.parse_args(argv)
    sim = SimulatedGoliath(args.host, args.port, framing=args.framing,
                           work_line_delay=args.line_delay, verbose=True)
    host, port = sim.start()
    print(f"Simulated Goliath listening on {host}:{port} (framing={args.framing}). Ctrl+C to stop.")
    print("This is NOT the real firmware; replies marked GUESS in sim.py are stand-ins.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
