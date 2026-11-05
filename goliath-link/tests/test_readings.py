# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Noah Rappaport and the Goliath-CNC contributors.
"""Typed readings (``goliath_link.readings``): reply parsers and the expected-reply table.

Every format comes from docs/protocol: spec 02 (``02-message-reference.md``,
sections "Responses", "Global status", "Free-form replies", "The INFO;0
acknowledgement") and spec 09 (``09-firmware-01.02.00.md``, "Where output goes"
and "The command table"). Nothing here has been observed on a robot yet.

Every value in the test data is made up: the serial number ``0123456789AB`` is a
test pattern, not a robot's serial number.
"""
import ast
import queue
import random
import re
import select
import socket
import time
from pathlib import Path

import pytest

from goliath_link import GoliathTransport
from goliath_link import readings as R
from goliath_link import sim as simmod
from goliath_link.protocol import (
    GLOBAL_BLOB_FIELD_NAMES_UNVERIFIED,
    GlobalStatusBlob,
    GlobalStatusFrame,
    TextMessage,
    parse_text_message,
)
from goliath_link.sim import GFW_01_02_00_TABLE, PROFILES, VENDOR_NEWER_EXTRA_COMMANDS, SimulatedGoliath

DOCS = Path(__file__).resolve().parents[2] / "docs" / "protocol"
SPEC02 = DOCS / "02-message-reference.md"
SPEC09 = DOCS / "09-firmware-01.02.00.md"

FW = (1, 2, 0)
FAKE_SERIAL = "0123456789AB"  # a test pattern, not any robot's serial number
HEAD = b"GLOBAL STATUS HEAD\x00"
TALE = b"GLOBAL STATUS TALE\x00"


def _spec_text(path: Path) -> str:
    if not path.exists():
        pytest.skip(f"{path.name} is not in this checkout (goliath-link tested outside the repository)")
    return path.read_text(encoding="utf-8")


def _raises_naming_the_line(func, line):
    with pytest.raises(R.ReadingParseError) as exc:
        func(line)
    shown = line.strip(" \t\r\n\x00") if isinstance(line, str) else line
    if isinstance(shown, str):
        assert repr(shown) in str(exc.value), str(exc.value)
    return exc.value


# --------------------------------------------------------------------------- #
# INFO;25 (panel vertex)
# --------------------------------------------------------------------------- #

def test_info25_two_and_three_fields():
    two = R.parse_vertex("INFO;25;1200.0000;-35.5000\n")
    assert two == (1200.0, -35.5)
    assert isinstance(two, tuple)
    assert two.form == "info25-gfw-01.02.00"
    assert "01.02.00" in two.basis and "not yet observed on a robot" in two.basis
    assert two.form in R.FORMATS and R.FORMATS[two.form].field_count == 2

    three = R.parse_vertex(parse_text_message("INFO;25;1.5;2.25;0"))
    assert three == (1.5, 2.25, 0.0)
    assert three.form == "info25-vendor-table"
    assert "vendor table" in three.basis
    assert R.FORMATS[three.form].field_count == 3

    # A TextMessage from the framer works the same as the raw line.
    tm = TextMessage(kind="INFO", raw="INFO;25;10.0000;20.0000", code=25,
                     params=["10.0000", "20.0000"])
    assert R.parse_vertex(tm) == (10.0, 20.0)


@pytest.mark.parametrize("line", [
    "INFO;25",
    "INFO;25;1.0000",
    "INFO;25;1;2;3;4",
    "INFO;25;1.0000;",            # an empty third field is not a third value
    "INFO;25;1.0000;;2.0000",
    "INFO;25;1.0000;abc",
    "INFO;25;nan;1.0000",
    "INFO;25;-nan;1.0000",
    "INFO;25;inf;1.0000",
    "INFO;25;1e3;1.0000",
    "INFO;25; 1.0000;2.0000",
    "INFO;25;1,5;2.0000",
    "INFO;25;0x10;2.0000",
    "INFO;025;1.0000;2.0000",
    "INFO;27;1.0000;2.0000",
    "ERR;25;1.0000;2.0000",
    "CAGE STATUS: OPEN",
])
def test_info25_anything_else_is_an_error_naming_the_line(line):
    err = _raises_naming_the_line(R.parse_vertex, line)
    assert err.line == line.strip()
    assert err.what == "INFO;25"


def test_info25_values_are_reported_not_interpreted():
    # x;y versus length;angle is open (spec 03 table row D, spec 02 SET VERTEX):
    # the parser hands back the numbers in wire order and never names them.
    v = R.parse_vertex("INFO;25;350.0000;90.0000")
    assert not hasattr(v, "x") and not hasattr(v, "angle")
    assert "not interpreted" in R.FORMATS["info25-gfw-01.02.00"].note


def test_info25_round_trips_printf_output():
    rng = random.Random(25)
    for _ in range(500):
        a, b = rng.uniform(-5000, 5000), rng.uniform(-5000, 5000)
        v = R.parse_vertex("INFO;25;%.4f;%.4f\n" % (a, b))
        assert v == (float("%.4f" % a), float("%.4f" % b))
        assert v.form == "info25-gfw-01.02.00"


# --------------------------------------------------------------------------- #
# INFO;27 (inter-tower distance)
# --------------------------------------------------------------------------- #

def test_info27_float():
    d = R.parse_tower_distance("INFO;27;2450.1234\n")
    assert isinstance(d, float) and d == 2450.1234
    assert R.parse_tower_distance(parse_text_message("INFO;27;0.0000")) == 0.0
    # Range checks belong to the caller (goliath-prep compares it with the panel).
    assert R.parse_tower_distance("INFO;27;-1.5000") == -1.5
    for bad in ("INFO;27", "INFO;27;", "INFO;27;1.0;2.0", "INFO;27;abc", "INFO;27;-nan",
                "INFO;27;inf", "INFO;25;1.0", "INFO;27;1.0 x"):
        err = _raises_naming_the_line(R.parse_tower_distance, bad)
        assert err.what == "INFO;27"


# --------------------------------------------------------------------------- #
# INFO;34 (identity)
# --------------------------------------------------------------------------- #

def test_identity_format_01_02_00():
    line = "INFO;34;GFW 01.02.00   ;1;2;%s\n" % FAKE_SERIAL
    ident = R.parse_identity(line)
    assert ident.prj == "GFW 01.02.00"
    assert ident.hw == 1 and ident.sw == 2
    assert ident.serial == FAKE_SERIAL
    assert ident.version == (1, 2, 0)
    assert ident.form == "info34-gfw-01.02.00"
    assert ident.notes == ()
    assert "01.02.00" in ident.basis and "not yet observed on a robot" in ident.basis
    assert ident.raw == line.strip()
    # The framer's TextMessage gives the same reading.
    assert R.parse_identity(parse_text_message(line)) == ident


@pytest.mark.parametrize("line, version, note", [
    ("INFO;34;GFW 01.02.03;1;2;TEST-SERIAL-0001", (1, 2, 3), "serial"),
    ("INFO;34;GFW 01.02.00;1;2;0123456789ab", (1, 2, 0), "serial"),
    ("INFO;34;GFW 01.02.00;1;2;0123456789ABCD", (1, 2, 0), "serial"),
    ("INFO;34;GFW 1.2.3;1;2;0123456789AB", (1, 2, 3), "project name"),
    ("INFO;34;Goliath test project;1;2;0123456789AB", None, "version"),
])
def test_identity_other_shapes_are_read_and_said(line, version, note):
    ident = R.parse_identity(line)
    assert ident.form == "info34-vendor-table"
    assert "vendor table" in ident.basis
    assert ident.version == version
    assert ident.notes and any(note in n for n in ident.notes), ident.notes


@pytest.mark.parametrize("line", [
    "INFO;34;GFW 01.02.00;1;2",
    "INFO;34;GFW 01.02.00;1;2;0123456789AB;extra",
    "INFO;34;GFW 01.02.00;A;2;0123456789AB",
    "INFO;34;GFW 01.02.00;1;2.0;0123456789AB",
    "INFO;34;;1;2;0123456789AB",
    "INFO;34;GFW 01.02.00;1;2;",
    "INFO;28; Project name:[x] Version:[01.02.00]",
    "INFO;34",
])
def test_identity_malformed_is_an_error_naming_the_line(line):
    err = _raises_naming_the_line(R.parse_identity, line)
    assert err.what == "INFO;34"


# --------------------------------------------------------------------------- #
# INFO;40 (sanity report)
# --------------------------------------------------------------------------- #

def test_info40_fifteen_values_then_END():
    vals = (1,) * 13 + (0, 1)
    line = "INFO;40;" + ";".join(str(v) for v in vals) + ";END\n"
    assert R.parse_sanity(line) == vals
    assert R.SANITY_VALUE_COUNT == 15
    assert "not yet observed on a robot" in R.FORMATS["info40-gfw-01.02.00"].basis
    # Values are reported as sent, never coerced to ok/fail (meaning: U-08 in
    # the project design; the vendor table says 0 = fail, 1 = ok).
    odd = (2,) + (1,) * 14
    assert R.parse_sanity("INFO;40;" + ";".join(map(str, odd)) + ";END") == odd
    # The count is a parameter, for a firmware that reports another number.
    assert R.parse_sanity("INFO;40;1;0;END", count=2) == (1, 0)


@pytest.mark.parametrize("line", [
    "INFO;40;" + ";".join(["1"] * 15),                 # no END: cut short
    "INFO;40;" + ";".join(["1"] * 14) + ";END",        # 14 values
    "INFO;40;" + ";".join(["1"] * 16) + ";END",        # 16 values
    "INFO;40;" + ";".join(["1"] * 14) + ";x;END",
    "INFO;40;END;" + ";".join(["1"] * 15),
    "INFO;40;" + ";".join(["1"] * 15) + ";end",
    "INFO;40;" + ";".join(["1"] * 15) + ";END;",
    "INFO;0",
])
def test_info40_malformed_is_an_error_naming_the_line(line):
    err = _raises_naming_the_line(R.parse_sanity, line)
    assert err.what == "INFO;40"


# --------------------------------------------------------------------------- #
# Free-form replies: cage, index, spindle distance
# --------------------------------------------------------------------------- #

def test_cage_closed_without_newline():
    direct = b"CAGE STATUS: CLOSED"  # snprintf(reply, 20, "CAGE STATUS: %s\n"): the \n is cut off
    assert len(direct) == 19
    assert R.parse_cage(direct) == "CLOSED"
    assert R.parse_cage(direct.decode("ascii")) == "CLOSED"
    assert R.parse_cage(TextMessage(kind="TEXT", raw="CAGE STATUS: CLOSED", terminated=False)) == "CLOSED"
    assert R.parse_cage(b"CAGE STATUS: OPEN\n") == "OPEN"  # 18 bytes, with \n
    # POLL CAGE form: a 512-byte slot-0 block, with \n
    assert R.parse_cage(b"CAGE STATUS: CLOSED\n" + bytes(492)) == "CLOSED"
    assert R.parse_cage(b"CAGE STATUS: OPEN\n" + bytes(494)) == "OPEN"


@pytest.mark.parametrize("text", [
    "CAGE STATUS: CLOS",                       # truncated: never read as CLOSED
    "CAGE STATUS: CLOSEDCAGE STATUS: CLOSED",  # two replies glued together
    "CAGE STATUS: closed",
    "CAGE STATUS:CLOSED",
    "CAGE STATUS: ",
    "CAGE STATUS",
    "cage status: CLOSED",
    "CAGE STATUS: OPEN CLOSED",
    "CAGE STATUS: CLOSED\x00junk",
])
def test_cage_anything_else_is_an_error(text):
    err = _raises_naming_the_line(R.parse_cage, text)
    assert err.what == "cage status"


def test_cage_other_text_is_not_a_cage_reply():
    assert R.parse_cage("cfg read index : 28500") is None
    assert R.parse_cage("") is None
    assert R.parse_cage(parse_text_message("INFO;0")) is None
    with pytest.raises(TypeError):
        R.parse_cage(19)


def test_index_format_space_both_sides():
    assert R.parse_index("cfg read index : 28500") == 28500
    assert R.parse_index(b"cfg read index : 28500" + bytes(490)) == 28500  # padded, no \n
    assert R.parse_index(TextMessage(kind="TEXT", raw="cfg read index : 28500")) == 28500
    assert R.parse_index("cfg read index : -3") == -3
    assert R.parse_index("spindle distance: 1.00") is None
    assert R.parse_index(parse_text_message("INFO;0")) is None


@pytest.mark.parametrize("text", [
    "cfg read index :28500",   # the form of an earlier spec revision, not the firmware's
    "cfg read index: 28500",
    "cfg read index:28500",
    "cfg read index : 28500.0",
    "cfg read index : ",
    "cfg read index : 28 500",
    "CFG READ INDEX : 28500",
    "cfg read index : 28500\x0012",
])
def test_index_anything_else_is_an_error(text):
    err = _raises_naming_the_line(R.parse_index, text)
    assert err.what == "index calibration"


def test_spindle_distance_without_newline():
    d = R.parse_spindle_distance("spindle distance: 12.34")
    assert isinstance(d, float) and d == 12.34
    assert R.parse_spindle_distance(b"spindle distance: -0.50" + bytes(489)) == -0.5
    assert R.parse_spindle_distance("cfg read index : 1") is None
    for bad in ("spindle distance:12.34", "spindle distance: abc", "spindle distance: nan",
                "spindle distance: 12.34 mm", "spindle distance: ", "Spindle distance: 1.00"):
        err = _raises_naming_the_line(R.parse_spindle_distance, bad)
        assert err.what == "spindle distance"


# --------------------------------------------------------------------------- #
# The 48-byte GLOBAL STATUS blob
# --------------------------------------------------------------------------- #

def test_status_blob_48_bytes_with_nuls():
    status = bytes([3, 0, 0x0A, 3, 0, 1, 0, 0x00, 0x7B, 0])  # NULs, a \n and a '{' inside
    blob = HEAD + status + TALE
    assert len(blob) == 48
    sb = R.StatusBlob.from_bytes(blob)
    assert sb.raw10 == status
    assert sb.to_bytes() == blob
    assert sb.assumed_names == GLOBAL_BLOB_FIELD_NAMES_UNVERIFIED
    assert R.StatusBlob.from_bytes(bytearray(blob)) == sb
    assert R.StatusBlob.from_bytes(memoryview(blob)) == sb
    # The transport's event carries the same bytes.
    assert R.StatusBlob.from_bytes(GlobalStatusBlob.decode(blob).raw) == sb


@pytest.mark.parametrize("data", [
    HEAD + bytes(10) + TALE[:-1],                          # 47 bytes
    HEAD + bytes(11) + TALE,                               # 49 bytes
    b"GLOBAL STATUS HEAD " + bytes(10) + TALE,             # HEAD without its NUL
    HEAD + bytes(10) + b"GLOBAL STATUS TAIL\x00",
    GlobalStatusFrame.build((1, 2, 0), (1, 2, 0)).raw,     # the 19-byte G...S frame
    b"",
])
def test_status_blob_wrong_shape_is_an_error(data):
    with pytest.raises(R.ReadingParseError) as exc:
        R.StatusBlob.from_bytes(data)
    assert exc.value.what == "GLOBAL STATUS blob"
    assert data.hex(" ")[:40] in str(exc.value)


def test_blob_accessors_flag_assumed_names():
    status = bytes([3, 1, 1, 3, 1, 5, 2, 0, 7, 1])
    sb = R.StatusBlob.from_bytes(HEAD + status + TALE)
    expected = {  # name: (blob offset, status-structure offset) from spec 02 / spec 09
        "pairing": (19, 8), "pd_status": (22, 16), "gcode_uploaded": (24, 20),
        "work_status": (25, 24), "resume_after_reset": (27, 32),
    }
    for name, (blob_off, struct_off) in expected.items():
        f = getattr(sb, name)
        assert isinstance(f, R.AssumedField) and not isinstance(f, int)
        assert f.assumed is True
        assert f.name == name
        assert f.blob_offset == blob_off and f.struct_offset == struct_off
        assert f.value == status[blob_off - 19]
        assert "assumed" in f.basis.lower() and "low byte" in f.basis.lower()
        assert f.observed is False
    assert "INFO;4" in sb.pairing.corroboration
    assert "INFO;6" in sb.pd_status.corroboration
    assert "INFO;8" in sb.gcode_uploaded.corroboration
    assert sb.work_status.corroboration is None
    assert sb.resume_after_reset.corroboration is None
    # Every one of the ten bytes is reachable, and always flagged.
    assert tuple(sb.field(n).value for n in sb.assumed_names) == tuple(status)
    assert all(sb.field(n).assumed for n in sb.assumed_names)
    with pytest.raises(KeyError):
        sb.field("tower_firmware")


def test_blob_offsets_match_spec02_table():
    text = _spec_text(SPEC02)
    rows = re.findall(r"^\| (\d+) \| \+(\d+) \| `(\w+)` \|", text, re.M)
    assert len(rows) == 10
    sb = R.StatusBlob.from_bytes(HEAD + bytes(range(10)) + TALE)
    for blob_off, struct_off, name in rows:
        f = sb.field(name)
        assert (f.blob_offset, f.struct_offset) == (int(blob_off), int(struct_off))


# --------------------------------------------------------------------------- #
# The expected-reply table
# --------------------------------------------------------------------------- #

def test_expected_reply_matches_sim_table():
    gfw = PROFILES["gfw-01.02.00"]
    table = R.EXPECTED_REPLY
    assert set(GFW_01_02_00_TABLE) <= set(table)
    assert set(table) - set(GFW_01_02_00_TABLE) == set(VENDOR_NEWER_EXTRA_COMMANDS)
    for index, name in enumerate(GFW_01_02_00_TABLE):
        spec = table[name]
        assert spec.table_record == index, name
        if name in gfw.not_dispatched:
            assert spec.kind == "none", name
        else:
            # the simulator's list of handlers that send no INFO;0 (spec 09)
            assert spec.expects_info0 == (name not in simmod._NO_INFO0_HANDLERS), name
    for name in VENDOR_NEWER_EXTRA_COMMANDS:
        spec = table[name]
        assert spec.kind == "none" and spec.table_record is None
        assert "absent" in spec.basis


def _classify_handler_sends(handler: str, sends: str) -> tuple:
    """(kind, err1_possible) as spec 09's 'Handler sends' column states it."""
    if "(empty)" in handler:
        return "none", False
    err1 = "ERR;1" in sends
    info0 = "INFO;0" in sends
    if "INFO;34" in sends:
        return "info34", err1
    if "INFO;40" in sends:
        return "info40", err1
    if "INFO;31" in sends:
        return "stream", err1
    if "re-sends the last" in sends:
        return "info-last", err1
    if "printf" in sends:
        return ("info0+printf" if info0 else "printf"), err1
    if "direct reply" in sends or "blob" in sends:
        return ("info0+direct" if info0 else "direct"), err1
    if info0:
        return "info0", err1
    assert sends.strip().startswith("—"), sends
    return "none", err1


def test_expected_reply_matches_spec09_handler_sends():
    text = _spec_text(SPEC09)
    dispatched = re.findall(
        r"^\| (\d+) \| ([A-Z][A-Z0-9 ]*[A-Z0-9]) \| (`0x[0-9a-f]+`(?: \(empty\))?) \| ([^|]*) \|(.*)$",
        text, re.M)
    assert len(dispatched) == 84, len(dispatched)
    for record, name, handler, sends, _notes in dispatched:
        spec = R.EXPECTED_REPLY[name]
        assert spec.table_record == int(record), name
        kind, err1 = _classify_handler_sends(handler, sends)
        assert (spec.kind, spec.err1_possible) == (kind, err1), (name, sends)
        assert spec.handler == handler.split("`")[1], name
        if "†" in sends:
            assert "single pass" in spec.basis, name
        if "printf" in sends:
            assert "single pass" in spec.basis, name
    not_dispatched = re.findall(r"^\| (\d+) \| `[^`]*` \| ([^|]*) \|", text, re.M)
    assert len(not_dispatched) == 10
    for record, name in not_dispatched:
        name = "" if name.strip() == "*(empty)*" else name.strip()
        spec = R.EXPECTED_REPLY[name]
        assert spec.kind == "none" and spec.table_record == int(record), name


def test_no_info0_for_INFO_and_PRINT_SYS_REPORT():
    info = R.expected_reply("INFO", FW)
    assert info.kind == "info34" and not info.expects_info0
    assert info.conn == 1 and info.terminator == "\n" and info.ack_conn is None
    report = R.expected_reply("PRINT SYS REPORT", FW)
    assert report.kind == "info40" and not report.expects_info0
    assert report.conn == 1 and report.terminator == "\n"
    # Ordinary acknowledging commands do expect INFO;0, on the second connection.
    for name in ("GET GLOBAL SYS STATUS", "SET PANEL SIZE", "READ INDEX CALIBRATION"):
        spec = R.expected_reply(name, FW)
        assert spec.expects_info0 and spec.ack_conn == 1, name


def test_spec02_no_info0_list_is_honoured():
    text = _spec_text(SPEC02)
    para = re.search(r"Statically, no `INFO;0` comes from:(.*?)\(A\s+subroutine", text, re.S)
    assert para, "spec 02 paragraph on INFO;0 not found"
    names = re.findall(r"`([A-Z][A-Z ]*[A-Z])`", para.group(1))
    assert set(names) >= {"INFO", "PRINT SYS REPORT", "GET SSIDS", "GCODE UPLOAD",
                          "FREE Z AXIS", "JOG STOP GOLIATH", "LOAD"}
    for name in names:
        assert not R.expected_reply(name, FW).expects_info0, name
    # "unknown commands, and the table entries whose handler is empty"
    assert not R.expected_reply("NO SUCH COMMAND", FW).expects_info0
    for name, spec in R.EXPECTED_REPLY.items():
        if spec.handler is not None and spec.handler in ("0x0800cc02", "0x0800cebc"):
            assert spec.kind == "none", name


def test_reply_specs_are_consistent():
    conns = {"info0": 1, "info0+direct": 0, "info0+printf": 0, "info34": 1, "info40": 1,
             "info-last": 1, "direct": 0, "stream": 1, "none": None}
    assert set(conns) == set(R.REPLY_KINDS)
    for name, spec in R.EXPECTED_REPLY.items():
        assert spec.conn == conns[spec.kind], name
        assert spec.expects_info0 == spec.kind.startswith("info0"), name
        assert spec.ack_conn == (1 if spec.expects_info0 else None), name
        if spec.kind == "none":
            assert spec.timeout_s == 0.0 and spec.terminator is None and spec.reply is None, name
        else:
            assert spec.timeout_s == R.DEFAULT_REPLY_TIMEOUT_S > 0, name
            assert spec.reply, name
        assert "not yet observed on a robot" in spec.basis, name
        assert spec.observed is False, name
        assert "not a firmware fact" in spec.timeout_basis, name
    with pytest.raises(ValueError, match="kind"):
        R.ReplySpec(kind="maybe", conn=1, terminator="\n", timeout_s=1.0, basis="x")
    with pytest.raises(ValueError, match="conn"):
        R.ReplySpec(kind="info0", conn=None, terminator="\n", timeout_s=1.0, basis="x")
    with pytest.raises(ValueError, match="conn"):
        R.ReplySpec(kind="info0", conn=7, terminator="\n", timeout_s=1.0, basis="x")
    with pytest.raises(ValueError, match="conn"):
        R.ReplySpec(kind="none", conn=1, terminator=None, timeout_s=0.0, basis="x")
    with pytest.raises(ValueError, match="basis"):
        R.ReplySpec(kind="info0", conn=1, terminator="\n", timeout_s=1.0, basis="")
    with pytest.raises(ValueError, match="timeout"):
        R.ReplySpec(kind="info0", conn=1, terminator="\n", timeout_s=float("nan"), basis="x")


def test_expected_reply_reads_the_name_as_the_firmware_does():
    assert R.expected_reply("SET PANEL SIZE;1200;800", FW) is R.EXPECTED_REPLY["SET PANEL SIZE"]
    assert R.expected_reply("get global sys status", FW) is R.EXPECTED_REPLY["GET GLOBAL SYS STATUS"]
    # A terminator makes the name unknown (spec 01 Outbound): no reply on 01.02.00.
    for raw in ("INFO\n", "INFO\r\n", "NO SUCH COMMAND"):
        spec = R.expected_reply(raw, FW)
        assert spec.kind == "none" and spec.table_record is None, raw
        assert "unknown command" in spec.basis
        assert spec.err1_possible, "the reply to an unknown command is not observed yet"
    # 01.02.00 does not have ABORT WORK: an unknown command there, not an abort.
    abort = R.expected_reply("ABORT WORK", FW)
    assert abort.kind == "none" and abort.err1_possible and "absent" in abort.basis
    # The provisioning string is not a table command; its path was not traced.
    assert R.expected_reply("SSID=41&PWD=42&AUTH_TYPE=2", FW) is None


def test_newer_firmware_has_no_basis():
    for fw in ((1, 2, 1), (1, 2, 2), (1, 2, 3), (1, 3, 0), (2, 0, 0)):
        for command in ("INFO", "GET GLOBAL SYS STATUS", "PRINT SYS REPORT", "ABORT WORK",
                        "SET PANEL SIZE;1200;800", "NO SUCH COMMAND"):
            assert R.expected_reply(command, fw) is None, (command, fw)
    assert set(R.REPLY_TABLES) == {FW}


def test_older_and_unknown_firmware_have_no_basis():
    assert R.expected_reply("INFO", (1, 0, 14)) is None
    assert R.expected_reply("INFO", None) is None  # version not read yet: never assume 01.02.00


def test_expected_reply_table_is_a_parameter():
    # After R1 (or for another firmware) a table can be passed in; nothing else
    # changes. The default tables are read-only.
    newer = R.ReplyTable(firmware=(1, 2, 3), basis="test table", specs={
        "INFO": R.ReplySpec(kind="info34", conn=1, terminator="\n", timeout_s=2.0, basis="test")},
        unknown=None)
    tables = {(1, 2, 3): newer}
    assert R.expected_reply("INFO", (1, 2, 3), tables=tables).timeout_s == 2.0
    assert R.expected_reply("GET GLOBAL SYS STATUS", (1, 2, 3), tables=tables) is None  # no basis
    assert R.expected_reply("INFO", FW, tables=tables) is None
    with pytest.raises(TypeError):
        R.EXPECTED_REPLY["INFO"] = R.EXPECTED_REPLY["INFO"]  # type: ignore[index]
    with pytest.raises(TypeError):
        R.REPLY_TABLES[(9, 9, 9)] = newer  # type: ignore[index]


@pytest.mark.parametrize("fw", [(1, 2), (1, 2, 0, 0), "01.02.00", (True, 2, 0), (1, 2.0, 0), (-1, 2, 0)])
def test_expected_reply_rejects_a_malformed_firmware_version(fw):
    with pytest.raises((TypeError, ValueError), match="firmware"):
        R.expected_reply("INFO", fw)


def test_formats_carry_their_basis():
    assert R.FORMATS
    for key, fmt in R.FORMATS.items():
        assert fmt.key == key
        assert fmt.basis and "spec 0" in fmt.basis, key
        assert "not yet observed on a robot" in fmt.basis, key
        assert fmt.observed is False, key
        assert ("01.02.00" in key) or ("vendor-table" in key), key


def test_readings_is_standard_library_only():
    tree = ast.parse(Path(R.__file__).read_text(encoding="utf-8"))
    allowed = {"__future__", "math", "re", "dataclasses", "types", "typing"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in allowed, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.level > 0 or (node.module or "").split(".")[0] in allowed, node.module


# --------------------------------------------------------------------------- #
# Against the running simulator
# --------------------------------------------------------------------------- #

def _connect(sim, opened):
    before = sim.accept_count
    s = socket.create_connection(("127.0.0.1", sim.port), timeout=3.0)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    opened.append(s)
    assert sim.wait_for(lambda: sim.accept_count > before, timeout=3.0)
    return s


def _drain(socks):
    for s in socks:
        while select.select([s], [], [], 0)[0]:
            if not s.recv(65536):
                break


def test_expected_reply_matches_the_running_simulator():
    """Send every dispatched 01.02.00 command to the simulator; compare what it sent.

    The simulator is built from the same spec, so this checks consistency, not the
    robot. GCODE UPLOAD is left out (it turns the parser off); POLL CAGE's timer is
    slowed so its periodic output does not land in a later command's window.
    """
    sim = SimulatedGoliath(profile="gfw-01.02.00", cage_poll_interval=60.0,
                           auto_start_work=False)
    sim.start()
    opened = []
    try:
        socks = [_connect(sim, opened) for _ in range(3)]
        assert sim.slot_holders()[:3] == (0, 1, 2)
        checked = 0
        for name in GFW_01_02_00_TABLE:
            if not name or name == "GCODE UPLOAD":
                continue
            spec = R.expected_reply(name, FW)
            start = len(sim.events)
            done_before = len(sim.events_of("done"))
            socks[0].sendall(name.encode("ascii"))
            assert sim.wait_for(lambda n=done_before: len(sim.events_of("done")) > n, timeout=3.0), name
            events = sim.events[start:]
            _drain(socks)
            sent = [e for e in events if e.kind == "sent"]
            unmodelled = any(e.kind == "unmodelled" for e in events)
            on1 = [e.data for e in sent if e.conn == 1]
            if spec.expects_info0:
                assert b"INFO;0\n" in on1 or (spec.err1_possible and b"ERR;1\n" in on1), (name, on1)
            elif spec.kind != "info-last":
                assert b"INFO;0\n" not in on1, (name, on1)
            if spec.kind == "none":
                assert sent == [], (name, sent)
            elif spec.kind == "info34":
                assert any(d.startswith(b"INFO;34;") for d in on1), name
            elif spec.kind == "info40":
                assert any(d.startswith(b"INFO;40;") for d in on1), name
            elif spec.kind == "stream":
                assert on1[0] == b"INFO;31;START\n" and on1[-1] == b"INFO;31;END\n", name
            elif spec.kind in ("info0+direct", "direct"):
                direct = [e for e in sent if e.note == "direct"]
                if unmodelled:  # the simulator does not model this direct reply
                    assert direct == [], name
                else:
                    assert direct and all(e.conn == spec.conn for e in direct), (name, direct)
            elif spec.kind == "info0+printf":
                printf = [e for e in sent if e.slot == 0 and e.note == "slot"]
                if unmodelled:
                    assert printf == [], name
                else:
                    assert printf and all(e.conn == spec.conn for e in printf), (name, printf)
            checked += 1
        assert checked == 92  # 93 named records, less GCODE UPLOAD
        assert not sim.errors
    finally:
        for s in opened:
            s.close()
        sim.stop()


def _wait_records(t, predicate, timeout=3.0):
    got = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            rec = t.records.get(timeout=0.05)
        except queue.Empty:
            continue
        got.append(rec)
        if predicate(rec):
            return rec, got
    raise AssertionError(f"no matching record within {timeout}s; got {got!r}")


def test_parsers_read_the_simulator_replies_over_the_transport():
    sim = SimulatedGoliath(profile="gfw-01.02.00", index_calibration=28500,
                           spindle_distance=12.34, cage="CLOSED",
                           sanity=(1,) * 14 + (0,), auto_start_work=False)
    sim.start()
    t = GoliathTransport("127.0.0.1", sim.port, timeout=2.0, connect_gaps=(0.05, 0.05),
                         settle=0.1, slot_release_wait=0.0)
    try:
        t.connect()

        def is_text(rec, conn, test):
            ev = rec.event
            return rec.conn == conn and isinstance(ev, TextMessage) and test(ev)

        # connect-time replay: INFO;34 on the second connection
        rec, _ = _wait_records(t, lambda r: is_text(r, 1, lambda e: e.kind == "INFO" and e.code == 34))
        ident = R.parse_identity(rec.event)
        assert ident.version == FW and ident.form == "info34-gfw-01.02.00"

        def ask(command, predicate):
            done = len(sim.events_of("done"))
            t.send(command)
            assert sim.wait_for(lambda: len(sim.events_of("done")) > done, timeout=3.0)
            return _wait_records(t, predicate)[0].event

        blob = ask("GET GLOBAL SYS STATUS", lambda r: r.conn == 0 and isinstance(r.event, GlobalStatusBlob))
        sb = R.StatusBlob.from_bytes(blob.raw)
        assert sb.pairing.assumed and len(sb.raw10) == 10
        report = ask("PRINT SYS REPORT", lambda r: is_text(r, 1, lambda e: e.code == 40))
        assert R.parse_sanity(report) == (1,) * 14 + (0,)
        cage = ask("GET SPINDLE CAGE STATUS", lambda r: is_text(r, 0, lambda e: e.kind == "TEXT"))
        assert R.parse_cage(cage) == "CLOSED"
        index = ask("READ INDEX CALIBRATION",
                    lambda r: is_text(r, 0, lambda e: e.raw.startswith("cfg")))
        assert R.parse_index(index) == 28500
        spindle = ask("READ SPINDLE CALIB",
                      lambda r: is_text(r, 0, lambda e: e.raw.startswith("spindle")))
        assert R.parse_spindle_distance(spindle) == 12.34
        assert not sim.errors
    finally:
        t.close()
        sim.stop()


def test_newer_profile_identity_reads_but_has_no_reply_basis():
    sim = SimulatedGoliath(profile="vendor-app-newer", auto_start_work=False)
    sim.start()
    t = GoliathTransport("127.0.0.1", sim.port, timeout=2.0, connect_gaps=(0.05, 0.05),
                         settle=0.1, slot_release_wait=0.0)
    try:
        t.connect()
        rec, _ = _wait_records(t, lambda r: isinstance(r.event, TextMessage) and r.event.code == 34)
        ident = R.parse_identity(rec.event)
        assert ident.version == (1, 2, 3)
        assert R.expected_reply("GET GLOBAL SYS STATUS", ident.version) is None
    finally:
        t.close()
        sim.stop()
