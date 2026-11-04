#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Passive listener for first contact with a Goliath CNC (or anything else).

It records whatever a serial port or TCP port sends, and it never transmits a
single byte. That guarantee is enforced in code: captures go through
`PassivePort`, which has no write method.

Typical use (see docs/field-guide/01-first-contact.md):

    python tools/serial_listen.py --list
    python tools/serial_listen.py --port COM5            # Windows
    python tools/serial_listen.py --port /dev/ttyUSB0    # Linux
    python tools/serial_listen.py --port socket://192.168.4.1:2000

For local serial ports it cycles through common baud rates and saves the raw
bytes of each window to its own file. Power-cycle the robot during each
window to catch boot messages. Network URLs (socket://, loop://) are captured
once, because baud rate means nothing there. socket:// is read with a plain
TCP socket rather than pyserial's handler, which throws away bytes that arrive
before the first read (such as a banner sent on connect).

Caveat: on Linux, and with some Windows drivers, opening a serial port briefly
asserts DTR/RTS before this program can clear them. If the board wires those
lines to RESET or BOOT0 (common on FTDI-based flashing circuits), the robot may
reboot when the port opens. That is harmless when the machine is idle, the bit
is removed, and the wheels are off the ground. Do it that way.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import pathlib
import re
import socket
import sys
import time
import urllib.parse
from typing import Callable, Optional

import serial
from serial.tools import list_ports

TOOL_VERSION = "0.1.0"

# Most common first; 115200 is the usual STM32/ESP default.
DEFAULT_BAUDS = [115200, 9600, 57600, 38400, 19200, 230400, 460800, 921600]

# URL schemes where a baud-rate sweep is meaningless.
_NO_BAUD_SCHEMES = ("socket://", "loop://")


class PassivePort:
    """Read-only view of a port. Deliberately has no `write`."""

    def __init__(self, port):
        self._port = port

    def read(self, size: int = 1) -> bytes:
        return self._port.read(size)

    def close(self) -> None:
        self._port.close()


class _TcpReader:
    """Read-only TCP stream for socket://host:port, opened WITHOUT pyserial.

    pyserial 3.5's socket:// handler calls reset_input_buffer() inside open(),
    which recv()s and discards whatever the device has already sent. A device
    that greets on connect would lose its banner, and the capture would read as
    "silence". Windows CI lost that race every time. This reader keeps every
    byte. Like PassivePort, it has no method that transmits.
    """

    def __init__(self, host: str, port: int, timeout: float, connect_timeout: float = 10.0):
        self._sock = socket.create_connection((host, port), timeout=connect_timeout)
        self._sock.settimeout(timeout)
        self._received = 0

    def read(self, size: int = 1) -> bytes:
        try:
            chunk = self._sock.recv(size)
        except socket.timeout:
            return b""
        except OSError as exc:
            raise serial.SerialException(
                f"socket error after {self._received} bytes: {exc}") from exc
        if not chunk:
            raise serial.SerialException(
                f"connection closed by the device after {self._received} bytes")
        self._received += len(chunk)
        return chunk

    def close(self) -> None:
        self._sock.close()


def _parse_socket_url(url: str) -> tuple[str, int]:
    """socket://host:port -> (host, port). Rejects anything it would ignore."""
    parts = urllib.parse.urlsplit(url)
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError(f"socket:// options/paths are not supported here: {url!r}")
    port = parts.port  # raises ValueError if out of range
    if not parts.hostname or port is None:
        raise ValueError(f"expected socket://host:port, got {url!r}")
    return parts.hostname, port


def open_passive(url: str, baud: int, timeout: float = 0.2) -> PassivePort:
    """Open `url` (device name or pyserial URL) without transmitting anything."""
    if url.startswith("socket://"):
        host, port_number = _parse_socket_url(url)
        return PassivePort(_TcpReader(host, port_number, timeout))
    port = serial.serial_for_url(url, do_not_open=True)
    port.baudrate = baud
    port.timeout = timeout
    # Ask for DTR/RTS deasserted before opening (see module docstring caveat).
    port.dtr = False
    port.rts = False
    port.open()
    return PassivePort(port)


def capture(
    port: PassivePort,
    seconds: float,
    on_chunk: Optional[Callable[[bytes], None]] = None,
    buf: Optional[bytearray] = None,
) -> bytes:
    """Read everything that arrives on `port` for `seconds` seconds.

    Pass your own `buf` to keep the partial capture if reading raises
    (device unplugged, Ctrl+C).
    """
    deadline = time.monotonic() + seconds
    if buf is None:
        buf = bytearray()
    while time.monotonic() < deadline:
        chunk = port.read(4096)
        if chunk:
            buf += chunk
            if on_chunk is not None:
                on_chunk(chunk)
    return bytes(buf)


def _is_printable(b: int) -> bool:
    return 32 <= b < 127 or b in (9, 10, 13)


def analyze(data: bytes) -> dict:
    """Cheap statistics to guess whether a capture is text, binary, or nothing."""
    if not data:
        return {"bytes": 0, "printable_ratio": None, "distinct_bytes": 0, "verdict": "silence"}
    printable = sum(1 for b in data if _is_printable(b))
    ratio = printable / len(data)
    if ratio >= 0.9:
        verdict = "text (baud rate is probably right)"
    elif ratio >= 0.6:
        verdict = "mixed text/binary"
    else:
        # A wrong baud rate and a genuine binary protocol look alike here.
        verdict = "binary data, or wrong baud rate"
    return {
        "bytes": len(data),
        "printable_ratio": round(ratio, 3),
        "distinct_bytes": len(set(data)),
        "verdict": verdict,
    }


def escape_for_terminal(chunk: bytes) -> str:
    """Show printable ASCII as-is and everything else as \\xNN."""
    out = []
    for b in chunk:
        if b == 10:
            out.append("\n")
        elif _is_printable(b) and b != 13:
            out.append(chr(b))
        else:
            out.append(f"\\x{b:02x}")
    return "".join(out)


def _safe_name(url: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", url).strip("_") or "port"


def list_serial_ports() -> list[dict]:
    ports = []
    for p in list_ports.comports():
        ports.append(
            {
                "device": p.device,
                "description": p.description,
                "manufacturer": p.manufacturer,
                "vid": f"{p.vid:04x}" if p.vid is not None else None,
                "pid": f"{p.pid:04x}" if p.pid is not None else None,
                "serial_number": p.serial_number,
                "hwid": p.hwid,
            }
        )
    return ports


def _print_ports(ports: list[dict]) -> None:
    if not ports:
        print("No serial ports found. Is the robot plugged in and powered? "
              "On Windows, check Device Manager > Ports (COM & LPT).")
        return
    for p in ports:
        vidpid = f"{p['vid']}:{p['pid']}" if p["vid"] else "????:????"
        print(f"{p['device']:<16} {vidpid}  {p['description']}  "
              f"(manufacturer={p['manufacturer']}, serial={p['serial_number']})")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Passively record what a serial or TCP port sends. Never transmits.")
    ap.add_argument("--list", action="store_true", help="list serial ports with USB VID:PID and exit")
    ap.add_argument("--port", help="device (COM5, /dev/ttyUSB0) or pyserial URL (socket://host:port)")
    ap.add_argument("--bauds", default=",".join(str(b) for b in DEFAULT_BAUDS),
                    help="comma-separated baud rates to try (serial devices only)")
    ap.add_argument("--seconds", type=float, default=20.0, help="capture time per baud rate")
    ap.add_argument("--out", default="captures", help="output directory (default: ./captures)")
    ap.add_argument("--quiet", action="store_true", help="don't echo received bytes live")
    args = ap.parse_args(argv)

    if args.list:
        _print_ports(list_serial_ports())
        return 0
    if not args.port:
        ap.error("--port is required (or use --list)")

    if args.port.startswith(_NO_BAUD_SCHEMES):
        bauds = [0]  # placeholder: a single pass
    else:
        try:
            bauds = [int(b) for b in args.bauds.split(",") if b.strip()]
        except ValueError:
            ap.error(f"--bauds must be comma-separated integers, got {args.bauds!r}")

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{_safe_name(args.port)}-{stamp}"

    echo = None if args.quiet else (lambda c: print(escape_for_terminal(c), end="", flush=True))
    results = []
    interrupted = False

    for baud in bauds:
        label = f"{baud} baud" if baud else "network stream"
        print(f"\n=== {label}: listening {args.seconds:g} s on {args.port} "
              f"(power-cycle the robot now to catch boot messages) ===", flush=True)
        try:
            port = open_passive(args.port, baud or 115200)
        except (serial.SerialException, ValueError, OSError) as exc:
            print(f"\nERROR: could not open {args.port!r}: {exc}", file=sys.stderr)
            print("Available serial ports:", file=sys.stderr)
            _print_ports(list_serial_ports())
            return 1
        buf = bytearray()
        read_error = None
        try:
            capture(port, args.seconds, on_chunk=echo, buf=buf)
        except KeyboardInterrupt:
            interrupted = True
        except serial.SerialException as exc:
            # A device that vanishes mid-capture (unplugged, USB chip reset by a
            # robot reboot) is itself worth knowing. Report it loudly, keep the
            # bytes received before the error, and move on to the next baud.
            read_error = str(exc)
            print(f"\nERROR while reading at {label}: {exc}", file=sys.stderr)
        finally:
            port.close()

        data = bytes(buf)
        fname = out_dir / (f"{base}-{baud}.bin" if baud else f"{base}.bin")
        fname.write_bytes(data)
        stats = analyze(data)
        stats.update({"baud": baud or None, "file": str(fname)})
        if read_error:
            stats["error"] = read_error
        results.append(stats)
        print(f"\n--- {label}: {stats['bytes']} bytes, {stats['verdict']} -> {fname}"
              + (f"  [read error: {read_error}]" if read_error else ""))
        if interrupted:
            print("Interrupted by user; stopping the sweep (captured data was saved).")
            break

    summary = {
        "tool": "serial_listen.py",
        "tool_version": TOOL_VERSION,
        "port": args.port,
        "started": stamp,
        "seconds_per_window": args.seconds,
        "interrupted": interrupted,
        "results": results,
        "serial_ports_seen": list_serial_ports(),
    }
    summary_path = out_dir / f"{base}-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))

    print("\n==================== summary ====================")
    for r in results:
        ratio = "-" if r["printable_ratio"] is None else f"{r['printable_ratio']:.2f}"
        err = f"  [read error: {r['error']}]" if "error" in r else ""
        print(f"{str(r['baud'] or '-'):>8}  {r['bytes']:>8} bytes  printable={ratio:>5}  {r['verdict']}{err}")
    print(f"Summary written to {summary_path}")
    if results and all(r["bytes"] == 0 for r in results):
        print("\nNOTE: nothing was received in any window. That is a real result, not a "
              "tool failure: the port may only be used for firmware flashing, or the "
              "device may only answer when spoken to. Report it as 'silence'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
