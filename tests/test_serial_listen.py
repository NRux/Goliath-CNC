# SPDX-License-Identifier: GPL-3.0-or-later
import json

import pytest

import serial

import serial_listen as sl


def test_analyze_silence():
    assert sl.analyze(b"")["verdict"] == "silence"


def test_analyze_text():
    stats = sl.analyze(b"Goliath FW 1.2.3 boot\r\n")
    assert stats["verdict"].startswith("text")
    assert stats["printable_ratio"] == 1.0


def test_analyze_binary():
    stats = sl.analyze(bytes(range(256)))
    assert stats["verdict"] == "binary data, or wrong baud rate"


def test_passive_port_has_no_write():
    port = serial.serial_for_url("loop://", timeout=0.05)
    passive = sl.PassivePort(port)
    assert not hasattr(passive, "write")
    passive.close()


def test_capture_reads_everything_that_arrives():
    port = serial.serial_for_url("loop://", timeout=0.05)
    port.write(b"BOOT\r\n\x02\x10\x03")  # the test plays the role of the robot
    seen = []
    data = sl.capture(sl.PassivePort(port), 0.3, on_chunk=seen.append)
    port.close()
    assert data == b"BOOT\r\n\x02\x10\x03"
    assert b"".join(seen) == data


def test_escape_for_terminal_marks_non_printables():
    assert sl.escape_for_terminal(b"ok\r\n\x02") == "ok\\x0d\n\\x02"


def test_main_records_silence_and_writes_files(tmp_path, capsys):
    rc = sl.main(["--port", "loop://", "--seconds", "0.2", "--out", str(tmp_path), "--quiet"])
    assert rc == 0
    bins = list(tmp_path.glob("*.bin"))
    summaries = list(tmp_path.glob("*-summary.json"))
    assert len(bins) == 1 and bins[0].read_bytes() == b""
    summary = json.loads(summaries[0].read_text())
    assert summary["results"][0]["verdict"] == "silence"
    assert "nothing was received" in capsys.readouterr().out


def test_main_over_tcp_records_banner_and_sends_nothing(tmp_path):
    """End-to-end: a fake robot sends a banner; the listener must send zero bytes back."""
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    received_from_client = bytearray()

    def fake_robot():
        conn, _ = srv.accept()
        conn.sendall(b"GOLIATH READY\n")
        conn.settimeout(0.6)
        try:
            while chunk := conn.recv(1024):
                received_from_client.extend(chunk)
        except socket.timeout:
            pass
        conn.close()

    t = threading.Thread(target=fake_robot)
    t.start()
    rc = sl.main(["--port", f"socket://127.0.0.1:{port}", "--seconds", "0.4",
                  "--out", str(tmp_path), "--quiet"])
    t.join()
    srv.close()

    assert rc == 0
    (capture_file,) = tmp_path.glob("*.bin")
    assert capture_file.read_bytes() == b"GOLIATH READY\n"
    assert bytes(received_from_client) == b""


def _prebuffered_connection(monkeypatch, payload: bytes):
    """Make socket.create_connection return a socket whose peer has ALREADY sent
    `payload`, so the bytes are waiting before the listener's first read."""
    import socket

    ours, robot = socket.socketpair()
    if payload:
        robot.sendall(payload)
    calls = []

    def fake_create_connection(address, *args, **kwargs):
        calls.append(tuple(address))
        return ours

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    return robot, calls


def test_socket_url_keeps_bytes_sent_before_first_read(monkeypatch):
    # Regression: pyserial 3.5's socket:// handler calls reset_input_buffer()
    # inside open(), which recv()s and discards whatever already arrived. A
    # device that greets on connect lost its banner and the capture reported
    # "silence" (caught on Windows CI, where that race is lost reliably).
    robot, calls = _prebuffered_connection(monkeypatch, b"GOLIATH READY\n")
    port = sl.open_passive("socket://10.0.0.1:80", 0)
    try:
        data = sl.capture(port, 0.3)
    finally:
        port.close()
        robot.close()
    assert calls == [("10.0.0.1", 80)]
    assert data == b"GOLIATH READY\n"


def test_socket_url_reports_device_closing_with_byte_count(monkeypatch):
    robot, _ = _prebuffered_connection(monkeypatch, b"BYE\n")
    robot.close()  # device hangs up right after its message
    port = sl.open_passive("socket://10.0.0.1:80", 0)
    buf = bytearray()
    try:
        with pytest.raises(serial.SerialException, match="closed by the device after 4 bytes"):
            sl.capture(port, 1.0, buf=buf)
    finally:
        port.close()
    assert bytes(buf) == b"BYE\n"  # bytes before the hang-up are kept


def test_socket_reader_cannot_transmit(monkeypatch):
    robot, _ = _prebuffered_connection(monkeypatch, b"")
    port = sl.open_passive("socket://10.0.0.1:80", 0)
    try:
        assert not hasattr(port, "write")
        assert not any(hasattr(port._port, name) for name in ("write", "send", "sendall"))
    finally:
        port.close()
        robot.close()


@pytest.mark.parametrize("url", ["socket://192.0.2.1", "socket://192.0.2.1:80?logging=debug",
                                 "socket://192.0.2.1:80/extra", "socket://:80",
                                 "socket://192.0.2.1:99999"])
def test_socket_url_rejects_what_it_cannot_honour(url, monkeypatch):
    import socket

    def must_not_connect(*args, **kwargs):
        raise AssertionError(f"tried to connect for a URL it should reject: {url}")

    monkeypatch.setattr(socket, "create_connection", must_not_connect)
    with pytest.raises(ValueError):
        sl.open_passive(url, 0)


def test_main_reports_unopenable_port(tmp_path, capsys):
    rc = sl.main(["--port", "/dev/does-not-exist-goliath", "--bauds", "115200",
                  "--seconds", "0.1", "--out", str(tmp_path)])
    assert rc == 1
    assert "could not open" in capsys.readouterr().err
