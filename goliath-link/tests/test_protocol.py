# SPDX-License-Identifier: Apache-2.0
import struct

import pytest

from goliath_link import protocol as P
from goliath_link.protocol import (
    Commands,
    GlobalStatusFrame,
    Info,
    PositionFrame,
    TowerFrame,
    parse_text_message,
)


def test_command_strings_exact():
    assert Commands.get_global_sys_status() == "GET GLOBAL SYS STATUS"
    assert Commands.set_panel_size(1200, 800) == "SET PANEL SIZE;1200;800"
    assert Commands.set_rf_channels(101, 114) == "SET RF CHANNELS;101;114"
    assert Commands.gcode_upload(4096) == "GCODE UPLOAD;4096"
    assert Commands.gcode_continue(False) == "GCODE CONTINUE;0"
    assert Commands.gcode_continue(True) == "GCODE CONTINUE;1"
    assert Commands.set_t2_position(True) == "SET T2 POSITION;1"
    assert Commands.goto_target(10, 20, 0, 315) == "GOTO TARGET;10;20;0;315"
    assert Commands.set_index_calibration_value(28500) == "SET INDEX CALIBRATION VALUE;28500"


def test_set_panel_size_rounds_to_int():
    assert Commands.set_panel_size(1200.6, 799.4) == "SET PANEL SIZE;1201;799"


def test_provision_network_hex_encodes_uppercase():
    # 'A' -> 41, 'b' -> 62
    assert Commands.provision_network("Ab", "x", 2) == "SSID=4162&PWD=78&AUTH_TYPE=2"


def test_provision_network_rejects_bad_auth():
    with pytest.raises(ValueError):
        Commands.provision_network("net", "pw", 9)


def test_parse_info_with_params():
    m = parse_text_message("INFO;25;100.5;200.0;0\n")
    assert m.kind == "INFO" and m.code == 25
    assert m.params == ["100.5", "200.0", "0"]


def test_parse_err_and_warn():
    assert parse_text_message("ERR;9").kind == "ERR"
    assert parse_text_message("ERR;9").code == 9
    assert parse_text_message("WARN;2").code == 2


def test_parse_freeform_text():
    m = parse_text_message("cfg read index :28500")
    assert m.kind == "TEXT"
    assert "28500" in m.raw


def test_parse_strips_nul_and_whitespace():
    m = parse_text_message("\x00INFO;8\r\n")
    assert m.kind == "INFO" and m.code == 8


def test_info_wire_codes_match_spec():
    # Codes are the wire values, NOT enum ordinals (29,30,36-39 are gaps).
    assert int(Info.GCODE_UPLOAD_DONE) == 8
    assert int(Info.AP) == 31
    assert int(Info.APP_INFO) == 34
    assert int(Info.SANITY_CHECK) == 40
    assert int(Info.SMART_POS) == 1000
    assert int(Info.SMART_GLOBAL_STATUS) == 1002


def test_status_and_battery_bit_order():
    # status = high 2 bits, battery = low 6 bits * 1.5625
    assert P._status_and_battery(0xC0) == (3, 0.0)
    assert P._status_and_battery(0x3F) == (0, 63 * 1.5625)
    assert P._status_and_battery(0x60) == (1, 50.0)


def _position_full(x, y, z, angle, line, temp, t1, t2):
    buf = bytearray(58)
    buf[0] = 0x7B
    buf[57] = 0x7D
    struct.pack_into("<ffff", buf, 1, x, y, z, angle)
    struct.pack_into("<i", buf, 17, line)
    struct.pack_into("<i", buf, 21, temp)
    buf[25] = t1
    buf[26] = t2
    return bytes(buf)


def _position_short(x, y, z, angle, line):
    buf = bytearray(22)
    buf[0] = 0x7B
    buf[21] = 0x7D
    struct.pack_into("<ffff", buf, 1, x, y, z, angle)
    struct.pack_into("<i", buf, 17, line)
    return bytes(buf)


def test_position_frame_full():
    f = PositionFrame.decode(_position_full(10.0, 20.0, -5.0, 315.0, 42, 48, 0x60, 0xC0))
    assert (round(f.x), round(f.y), round(f.z), round(f.angle)) == (10, 20, -5, 315)
    assert f.gcode_line == 42
    assert f.temperature == 48
    assert f.t1_status == 1 and f.t1_battery == 50.0
    assert f.t2_status == 3 and f.t2_battery == 0.0


def test_position_frame_short_has_no_temp():
    f = PositionFrame.decode(_position_short(1.0, 2.0, 3.0, 0.0, 7))
    assert f.gcode_line == 7
    assert f.temperature is None and f.t1_status is None


def test_position_frame_bad_sentinel():
    buf = bytearray(_position_short(0, 0, 0, 0, 0))
    buf[-1] = 0x00
    with pytest.raises(ValueError):
        PositionFrame.decode(bytes(buf))


def test_tower_frame():
    f = TowerFrame.decode(bytes([0x3C, 0xBB, 0x60, 0x3E]))
    assert f.tower == 2 and f.status == 1 and f.battery == 50.0
    with pytest.raises(ValueError):
        TowerFrame.decode(bytes([0x3C, 0x11, 0x00, 0x3E]))  # unknown tower id


def test_global_status_frame_firmware_bytes():
    buf = bytearray(19)
    buf[0] = 0x47
    buf[18] = 0x53
    buf[12], buf[13], buf[14] = 0, 2, 1  # t1 read high->low: [14,13,12] = (1,2,0)
    buf[15], buf[16], buf[17] = 0, 2, 1  # t2 = (1,2,0)
    f = GlobalStatusFrame.decode(bytes(buf))
    assert f.t1_firmware == (1, 2, 0)
    assert f.t2_firmware == (1, 2, 0)


def test_position_frame_encode_roundtrip_full_and_short():
    full = PositionFrame(x=1.5, y=-2.25, z=-3.0, angle=315.0, gcode_line=9, temperature=41,
                         t1_status=1, t1_battery=50.0, t2_status=3, t2_battery=0.0)
    assert len(full.encode()) == 58
    assert PositionFrame.decode(full.encode()) == full
    short = PositionFrame(x=1.0, y=2.0, z=3.0, angle=0.0, gcode_line=4)
    assert len(short.encode()) == 22
    assert PositionFrame.decode(short.encode()) == short


def test_tower_frame_encode_roundtrip():
    f = TowerFrame(tower=1, status=2, battery=75.0)
    assert TowerFrame.decode(f.encode()) == f


def test_global_status_build_and_flags_raw():
    flags = bytes(range(1, 12))
    f = GlobalStatusFrame.build((1, 2, 0), (1, 1, 9), flags)
    assert f.t1_firmware == (1, 2, 0) and f.t2_firmware == (1, 1, 9)
    assert f.flags_raw == flags
    assert GlobalStatusFrame.decode(f.encode()) == f


# --------------------------------------------------------------------------- #
# GFW 01.02.00 wire shapes (firmware facts brief, FRAME-3/FRAME-4)
# --------------------------------------------------------------------------- #

_HEAD = b"GLOBAL STATUS HEAD\x00"
_TALE = b"GLOBAL STATUS TALE\x00"


def test_text_message_terminated_flag_defaults_true_and_can_be_cleared():
    assert parse_text_message("INFO;8\n").terminated is True
    m = parse_text_message("count: 5", terminated=False)
    assert m.kind == "TEXT" and m.raw == "count: 5" and m.terminated is False
    # Positional construction keeps working (terminated is the last field).
    assert P.TextMessage("ERR", "ERR;2", 2, []).terminated is True


def test_global_status_blob_decode_keeps_all_ten_bytes_in_order():
    fields = (1, 0, 1, 2, 1, 0, 0, 1, 0, 10)
    data = _HEAD + bytes(fields) + _TALE
    assert len(data) == P.GLOBAL_BLOB_LEN == 48
    b = P.GlobalStatusBlob.decode(data)
    assert b.fields == fields
    assert b.raw == data
    assert b.encode() == data
    assert P.GlobalStatusBlob.build(fields) == b


def test_global_status_blob_rejects_bad_sentinels():
    with pytest.raises(ValueError):
        P.GlobalStatusBlob.decode(b"GLOBAL STATUS HEAX\x00" + bytes(10) + _TALE)
    with pytest.raises(ValueError):
        P.GlobalStatusBlob.decode(_HEAD + bytes(10) + b"GLOBAL STATUS TAIL\x00")
    with pytest.raises(ValueError):
        P.GlobalStatusBlob.decode(_HEAD + bytes(10))


def test_global_status_blob_vendor_name_mapping_is_marked_unverified():
    names = P.GLOBAL_BLOB_FIELD_NAMES_UNVERIFIED
    assert len(names) == 10 and names[0] == "pairing" and names[-1] == "encoder_calibrated"
    b = P.GlobalStatusBlob.build(tuple(range(10)))
    mapping = b.fields_by_vendor_name_unverified()
    assert mapping == dict(zip(names, range(10)))
    assert "unverified" in P.GlobalStatusBlob.fields_by_vendor_name_unverified.__doc__.lower()
    # A blob with a different field count has no name mapping at all.
    odd = P.GlobalStatusBlob.decode(_HEAD + bytes(11) + _TALE)
    assert len(odd.fields) == 11
    assert odd.fields_by_vendor_name_unverified() is None


def test_global_status_blob_has_no_tower_firmware():
    b = P.GlobalStatusBlob.build(tuple(range(10)))
    assert not hasattr(b, "t1_firmware")
