# SPDX-License-Identifier: GPL-3.0-or-later
import json

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


def test_main_reports_unopenable_port(tmp_path, capsys):
    rc = sl.main(["--port", "/dev/does-not-exist-goliath", "--bauds", "115200",
                  "--seconds", "0.1", "--out", str(tmp_path)])
    assert rc == 1
    assert "could not open" in capsys.readouterr().err
