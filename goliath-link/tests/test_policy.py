# SPDX-License-Identifier: Apache-2.0
"""The command safety layer's tables and policies (no network).

The denylist (``NEVER_SEND``), the vendor-sent set (``VENDOR_SENT``) and the
strict set (``STRICT_COMMANDS``) are derived from
``docs/protocol/09-firmware-01.02.00.md``, section "The command table"
(subsections 1, 2a, 2b and 3). These tests cross-check the code's table against
the spec text and against the simulator's 94-entry table, so the three cannot
drift apart silently, and check that every spelling of a denylisted command is
refused.
"""
import re
from pathlib import Path

import pytest

from goliath_link import policy as pol
from goliath_link.probe import READ_ONLY_COMMANDS
from goliath_link.protocol import Commands
from goliath_link.sim import GFW_01_02_00_TABLE, VENDOR_NEWER_EXTRA_COMMANDS

ROOT = Path(__file__).resolve().parents[2]
SPEC09 = ROOT / "docs" / "protocol" / "09-firmware-01.02.00.md"

# Table indexes per section of spec 09 "The command table", written out here
# independently of the code (a third source next to the spec and policy.py).
IDX_SECTION_1 = frozenset({4, 5, 11, 23, 29, 30, 35, 45, 46, 48, 49, 50, 51, 52, 58, 59, 60,
                           65, 66, 67, 70, 76, 79, 82, 83, 86})
IDX_SECTION_2A = frozenset({6, 31, 32, 33, 34, 36, 37, 38, 39, 40})
IDX_SECTION_3 = frozenset({0, 1, 2, 3, 88, 89, 90, 91, 92, 93})
IDX_SECTION_2B = frozenset(range(94)) - IDX_SECTION_1 - IDX_SECTION_2A - IDX_SECTION_3
IDX_INFO = 6
IDX_LOAD = 11


# --------------------------------------------------------------------------- #
# Reading spec 09
# --------------------------------------------------------------------------- #


def spec09_text() -> str:
    if not SPEC09.exists():
        if (ROOT / "docs" / "protocol").is_dir():
            pytest.fail(f"{SPEC09} is missing from this repository checkout: the denylist "
                        "must be cross-checked against it")
        pytest.skip("docs/protocol/ is not in this checkout (goliath-link tested outside the "
                    "Goliath-CNC repository): cannot cross-check against spec 09")
    return SPEC09.read_text(encoding="utf-8")


def spec_section(text: str, heading: str) -> str:
    """The body under the heading line starting with ``heading``, up to the next heading."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(heading)]
    assert len(starts) == 1, f"spec 09: expected one heading {heading!r}, found {len(starts)}"
    body = []
    for line in lines[starts[0] + 1:]:
        if line.startswith("#"):
            break
        body.append(line)
    return "\n".join(body)


def table_rows(section: str, name_column: int) -> dict:
    """``{index: name}`` from the markdown table rows ``| <index> | ... |``."""
    rows = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) <= name_column or not cells[0].isdigit():
            continue
        name = cells[name_column]
        if name == "*(empty)*":
            name = ""
        assert int(cells[0]) not in rows, line
        rows[int(cells[0])] = name
    return rows


def stated_count(section: str) -> int:
    m = re.search(r"\b(\d+) commands\b", section)
    assert m, "spec 09 section states no command count"
    return int(m.group(1))


@pytest.fixture(scope="module")
def spec() -> dict:
    text = spec09_text()
    s1 = spec_section(text, "### 1. Commands the vendor app sends")
    s2a = spec_section(text, "#### 2a. In the vendor app's command table, never sent")
    s2b = spec_section(text, "#### 2b. Unknown to the vendor app")
    s3 = spec_section(text, "### 3. Not dispatched")
    off = spec_section(text, "### Vendor-app commands that are not in the table")
    allow = spec_section(text, "## Read-only allowlist")
    m = re.match(r"\s*((?:`[^`]+`[,\s]*)+)\.", off)
    assert m, "spec 09: the list of vendor-app commands absent from the table was not found"
    return {
        "1": table_rows(s1, 1), "1_count": stated_count(s1),
        "2a": table_rows(s2a, 1),
        "2b": table_rows(s2b, 1), "2b_count": stated_count(s2b),
        "3": table_rows(s3, 2),
        "off_table": frozenset(re.findall(r"`([^`]+)`", m.group(1))),
        "read_only": frozenset(re.findall(r"^\| `([^`]+)` \|", allow, re.M)),
    }


# --------------------------------------------------------------------------- #
# The tables
# --------------------------------------------------------------------------- #


def test_denylist_derived_from_in_repo_spec09(spec):
    # The code's per-section tables are exactly the spec's (index and name).
    assert dict(pol.SPEC09_VENDOR_SENT) == spec["1"]
    assert dict(pol.SPEC09_VENDOR_TABLE_NEVER_SENT) == spec["2a"]
    assert dict(pol.SPEC09_UNKNOWN_TO_VENDOR) == spec["2b"]
    assert dict(pol.SPEC09_NOT_DISPATCHED) == spec["3"]
    # ... and the counts the spec states in words agree with its own rows.
    assert len(spec["1"]) == spec["1_count"] == 26
    assert len(spec["2b"]) == spec["2b_count"] == 48
    assert len(spec["2a"]) == 10 and len(spec["3"]) == 10
    # The derived sets, from the spec text alone.
    s2a = frozenset(spec["2a"].values())
    expected_never = ((s2a - {"INFO"}) | frozenset(spec["2b"].values()) | {"LOAD"}
                      | frozenset(spec["3"].values()))
    assert pol.NEVER_SEND == expected_never
    assert pol.VENDOR_SENT == frozenset(spec["1"].values())
    assert pol.STRICT_COMMANDS == (frozenset(spec["1"].values()) | {"INFO"}) - {"LOAD"}
    assert pol.VENDOR_SENT_NOT_ON_GFW_01_02_00 == spec["off_table"]
    assert len(spec["off_table"]) == 10


def test_read_only_allowlist_is_strict_and_never_denylisted(spec):
    assert spec["read_only"] == READ_ONLY_COMMANDS  # the probe's allowlist is the spec's
    assert READ_ONLY_COMMANDS <= pol.STRICT_COMMANDS
    assert not (READ_ONLY_COMMANDS & pol.NEVER_SEND)


def test_sections_partition_the_gfw_01_02_00_table():
    sections = (pol.SPEC09_VENDOR_SENT, pol.SPEC09_VENDOR_TABLE_NEVER_SENT,
                pol.SPEC09_UNKNOWN_TO_VENDOR, pol.SPEC09_NOT_DISPATCHED)
    indexes = [i for s in sections for i in s]
    assert sorted(indexes) == list(range(94))  # disjoint, and all 94 records
    for s in sections:
        for index, name in s.items():
            assert GFW_01_02_00_TABLE[index] == name, (index, name)
    assert set(pol.SPEC09_VENDOR_SENT) == IDX_SECTION_1
    assert set(pol.SPEC09_VENDOR_TABLE_NEVER_SENT) == IDX_SECTION_2A
    assert set(pol.SPEC09_UNKNOWN_TO_VENDOR) == IDX_SECTION_2B
    assert set(pol.SPEC09_NOT_DISPATCHED) == IDX_SECTION_3
    assert pol.VENDOR_SENT_NOT_ON_GFW_01_02_00 == VENDOR_NEWER_EXTRA_COMMANDS


def test_tables_are_immutable():
    for s in (pol.NEVER_SEND, pol.VENDOR_SENT, pol.STRICT_COMMANDS,
              pol.VENDOR_SENT_NOT_ON_GFW_01_02_00):
        assert isinstance(s, frozenset)
    with pytest.raises(TypeError):
        pol.SPEC09_UNKNOWN_TO_VENDOR[14] = "MOVE Y"  # type: ignore[index]
    assert set(pol.NEVER_SEND_REASONS) == pol.NEVER_SEND


def test_denylist_blocks_2a_2b_and_LOAD():
    """Table-driven from the simulator's GFW 01.02.00 table."""
    wrong = []
    for index, name in enumerate(GFW_01_02_00_TABLE):
        if index == IDX_INFO:
            expect_refused = False  # the one 2a exception: read-only
        elif index in IDX_SECTION_2A or index in IDX_SECTION_2B or index == IDX_LOAD:
            expect_refused = True
        elif index in IDX_SECTION_3:
            expect_refused = True  # pinned: see the module docstring of goliath_link.policy
        else:
            assert index in IDX_SECTION_1
            expect_refused = False
        for command in (name, name + ";1", name + ";1;2"):
            d = pol.denylist_decision(command)
            if d.allowed is expect_refused:
                wrong.append((index, command, d))
            if expect_refused:
                assert "spec 09" in d.reason, d
    assert not wrong, wrong


def spellings(name: str) -> list:
    """Every way of writing ``name`` that some robot could read as that command."""
    spaced = name.replace(" ", "  ")
    tabbed = name.replace(" ", "\t")
    out = [
        name, name.lower(), name.title(), name.swapcase(),
        "".join(ch.lower() if i % 2 else ch for i, ch in enumerate(name)),
        " " + name, name + " ", "  " + name + "  ", "\t" + name, name + "\t",
        name + "\n", name + "\r\n", name + "\r", name + "\x00", name + "\x00garbage",
        "\r\n" + name, name + ";", name + ";1;2", name.lower() + ";1",
        ";" + name, ";;" + name + ";5", " ; " + name,
        spaced, tabbed, name.replace(" ", "\u00a0"), name.replace(" ", "\u3000"),
        "INFO\n" + name, "INFO\r\n" + name, "INFO\x00" + name, "INFO\r" + name,
        "INFO\x0b" + name, "INFO\x0c" + name, "GET GLOBAL SYS STATUS\n" + name.lower() + "\n",
        name + "\nINFO",
    ]
    return out


@pytest.mark.parametrize("name", sorted(pol.NEVER_SEND - {""}))
def test_every_spelling_of_a_denylisted_command_is_refused(name):
    passed = [s for s in spellings(name) if pol.denylist_decision(s).allowed]
    assert not passed, passed


@pytest.mark.parametrize("command", ["", " ", "\n", "\r\n", "\x00", ";", " ; ", "\t\n"])
def test_an_empty_command_is_refused(command):
    d = pol.denylist_decision(command)
    assert d.allowed is False and "empty" in d.reason


@pytest.mark.parametrize("command", sorted(
    (pol.VENDOR_SENT - {"LOAD"}) | {"INFO", "INFO;1"} | pol.VENDOR_SENT_NOT_ON_GFW_01_02_00
    | {"FOO", "SET PANEL SIZE;1200;800", "GCODE UPLOAD;123", "load wifix", "MOVE ZZ",
       "SSID=4D79&PWD=5365637265&AUTH_TYPE=2", "LOADING", "INFO;MOVE Z"}))
def test_other_commands_pass_the_denylist(command):
    d = pol.denylist_decision(command)
    assert d.allowed is True, d


def test_normalized_names():
    assert pol.normalized_names("move z;10") == ("MOVE Z",)
    assert pol.normalized_names("  Move \t Z ;10\r\n") == ("MOVE Z",)
    assert pol.normalized_names(";;MOVE Z;1") == ("MOVE Z",)  # strtok skips leading ';'
    assert pol.normalized_names("INFO\nMOVE Z") == ("INFO", "MOVE Z")
    assert pol.normalized_names("INFO\x00MOVE Z") == ("INFO", "MOVE Z")
    assert pol.normalized_names("") == ("",)
    assert pol.normalized_names("\r\n") == ("",)
    assert pol.command_name("info;1") == "INFO"
    assert pol.command_name(" info") == " INFO"  # what GFW 01.02.00 compares


# --------------------------------------------------------------------------- #
# The strict set
# --------------------------------------------------------------------------- #


def test_strict_set_is_spec09_section_1_plus_info():
    assert len(pol.STRICT_COMMANDS) == 26
    assert "INFO" in pol.STRICT_COMMANDS and "LOAD" not in pol.STRICT_COMMANDS
    assert not (pol.STRICT_COMMANDS & pol.NEVER_SEND)
    for name in pol.STRICT_COMMANDS:
        assert pol.strict_decision(name).allowed is True, name
        assert pol.strict_decision(name.lower() + ";1").allowed is True, name


@pytest.mark.parametrize("command", [
    "FOO", "MOVE Z", "SET VERTEX;1;2", "SYS RESET", "LOAD;12;",
    # Vendor-sent, but only to firmware newer than 01.02.00 (outside spec 09 section 1):
    "ABORT WORK", "GCODE CONTINUE;0", "SEND TCP POS", "GET Z BACKLASH", "CMD SYS RESET",
    "GET Z PLATE OFFSET",
    # Wi-Fi provisioning is not a table command:
    "SSID=4D79&PWD=5365637265&AUTH_TYPE=2",
    # Not spelled the way the firmware compares names:
    " INFO", "INFO ", "INFO\n", "INFO\r\n", "INFO\x00", ";INFO", "GET  GLOBAL SYS STATUS",
    "INF\u00d8", "INFO\u00a0",
    "",
])
def test_strict_refuses_unknown_names(command):
    d = pol.strict_decision(command)
    assert d.allowed is False, d
    assert "strict" in d.reason


def test_strict_explains_newer_firmware_commands():
    d = pol.strict_decision("ABORT WORK")
    assert "newer than 01.02.00" in d.reason and "section 1" in d.reason


# --------------------------------------------------------------------------- #
# PolicyDecision, PhasePolicy, bind
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("allowed", [None, 1, 0, "yes", "False"])
def test_policy_decision_allowed_must_be_a_bool(allowed):
    with pytest.raises(TypeError):
        pol.PolicyDecision(allowed, "reason")  # an unknown is never read as yes


@pytest.mark.parametrize("reason", ["", "   ", None, 3])
def test_policy_decision_needs_a_reason(reason):
    with pytest.raises((TypeError, ValueError)):
        pol.PolicyDecision(True, reason)


def test_bind_matches_the_command_builders():
    assert pol.bind("SET PANEL SIZE", 1200, 800).wire == Commands.set_panel_size(1200, 800)
    assert pol.bind("SET T2 POSITION", 1).wire == Commands.set_t2_position(True)
    assert pol.bind("SET T2 POSITION", 0).wire == Commands.set_t2_position(False)
    assert pol.bind("SET RF CHANNELS", 101, 114).wire == Commands.set_rf_channels(101, 114)
    assert pol.bind("GCODE UPLOAD", 4711).wire == Commands.gcode_upload(4711)
    assert pol.bind("PRINT SYS REPORT").wire == "PRINT SYS REPORT"
    assert pol.AllowedCommand("PRINT SYS REPORT").wire is None  # any arguments


@pytest.mark.parametrize("args, exc", [
    ((True,), TypeError),  # a bool is not an integer argument
    ((1.5,), TypeError),  # floats: bind the exact wire text
    ((None,), TypeError),
    (("1;2",), ValueError),
    (("1\n",), ValueError),
    (("",), ValueError),
])
def test_bind_refuses_ambiguous_arguments(args, exc):
    with pytest.raises(exc):
        pol.bind("SET PANEL SIZE", *args)


@pytest.mark.parametrize("name", ["set panel size", "SET  PANEL SIZE", " INFO", "INFO;1",
                                  "INFO\n", "", "SET\u00a0VERTEX"])
def test_allowed_command_names_must_be_canonical(name):
    with pytest.raises(ValueError):
        pol.AllowedCommand(name)


@pytest.mark.parametrize("name", ["MOVE Z", "LOAD", "SET VERTEX", "GCODE START", "SYS RESET"])
def test_a_policy_cannot_allow_a_denylisted_command(name):
    with pytest.raises(ValueError, match="denylist"):
        pol.PhasePolicy("TEST", [name])
    with pytest.raises(ValueError, match="denylist"):
        pol.PhasePolicy("TEST", [pol.bind(name, 1)])


def test_phase_policy_binds_arguments():
    p = pol.PhasePolicy("COMMIT", [pol.bind("SET PANEL SIZE", 1200, 800), "PRINT SYS REPORT",
                                   "GET GLOBAL SYS STATUS"], basis="test")
    assert isinstance(p, pol.CommandPolicy)
    ok = p.check("SET PANEL SIZE;1200;800")
    assert ok.allowed is True and "COMMIT" in ok.reason and "bound" in ok.reason
    for bad in ("SET PANEL SIZE;1201;800", "SET PANEL SIZE;1200;800;0", "SET PANEL SIZE;800;1200",
                "SET PANEL SIZE", "SET PANEL SIZE;1200.0;800", "set panel size;1200;800",
                "SET PANEL SIZE;1200;800\n", " SET PANEL SIZE;1200;800",
                "SET PANEL SIZE;;1200;800"):
        d = p.check(bad)
        assert d.allowed is False, bad
        assert "SET PANEL SIZE;1200;800" in d.reason and "COMMIT" in d.reason
    # Unbound entries allow any arguments and any case of the name.
    assert p.check("PRINT SYS REPORT").allowed is True
    assert p.check("print sys report;1").allowed is True
    # Anything not listed is refused, with the phase and what it allows.
    d = p.check("SET RF CHANNELS;101;114")
    assert d.allowed is False and "COMMIT" in d.reason and "PRINT SYS REPORT" in d.reason
    # One command per send.
    assert p.check("PRINT SYS REPORT\nGET GLOBAL SYS STATUS").allowed is False


def test_phase_policy_with_two_bound_values():
    p = pol.PhasePolicy("UPLOADING", [pol.bind("GCODE UPLOAD", 10), pol.bind("GCODE UPLOAD", 20)])
    assert p.check("GCODE UPLOAD;10").allowed and p.check("GCODE UPLOAD;20").allowed
    assert not p.check("GCODE UPLOAD;30").allowed


def test_an_empty_phase_policy_sends_nothing():
    p = pol.PhasePolicy("RUNNING", [])
    for command in ("INFO", "GET GLOBAL SYS STATUS", "ABORT WORK"):
        d = p.check(command)
        assert d.allowed is False and "no commands" in d.reason


def test_phase_policy_validates_its_input():
    with pytest.raises(ValueError):
        pol.PhasePolicy("", ["INFO"])
    with pytest.raises(TypeError):
        pol.PhasePolicy("X", "INFO")  # a str is not a list of commands
    with pytest.raises(TypeError):
        pol.PhasePolicy("X", [42])
    with pytest.raises(TypeError):
        pol.PhasePolicy("X", ["INFO"], basis=7)
    # An unbound entry next to a bound one would silently void the binding.
    with pytest.raises(ValueError, match="binding"):
        pol.PhasePolicy("COMMIT", ["SET PANEL SIZE", pol.bind("SET PANEL SIZE", 1200, 800)])
    pol.PhasePolicy("X", ["INFO", "INFO"])  # a repeated unbound name is harmless


# --------------------------------------------------------------------------- #
# Refusal errors
# --------------------------------------------------------------------------- #


def test_refusal_error_carries_command_decision_and_layer():
    d = pol.PolicyDecision(False, "phase X does not allow it")
    e = pol.CommandRefusedError("SET PANEL SIZE;1;2", d, layer="policy")
    assert e.command == "SET PANEL SIZE;1;2" and e.decision is d and e.layer == "policy"
    assert "SET PANEL SIZE;1;2" in str(e) and "phase X" in str(e)
    with pytest.raises(ValueError):
        pol.CommandRefusedError("X", d, layer="whatever")
    with pytest.raises(ValueError):
        pol.CommandRefusedError("X", pol.PolicyDecision(True, "ok"))


def test_refusal_message_redacts_a_wifi_key():
    secret_hex = "5365637265744B6579"  # a fake key, hex-encoded like the vendor app
    cmd = f"SSID=476F6C696174685F544553543030&PWD={secret_hex}&AUTH_TYPE=2"
    d = pol.strict_decision(cmd)
    assert secret_hex not in d.reason
    e = pol.CommandRefusedError(cmd, d, layer="strict")
    assert secret_hex not in str(e) and "PWD=<redacted>" in str(e)
    assert pol.redact(cmd).endswith("PWD=<redacted>&AUTH_TYPE=2")
