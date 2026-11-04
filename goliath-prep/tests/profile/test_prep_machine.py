# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""machine.toml: the machine profile of one robot (unit P01; red-team C-13, PHYS-05, PHYS-19).

The profile's measured values use field guide 04's field ids as their TOML key
paths. A profile exported from the facts store (its content hash verifies)
loads as ROBOT_MEASURED; a hand-written or hand-edited one as OWNER_PROFILE,
disclosed. Anything not measured keeps the catalogue's cautious default with
the amber "UNMEASURED MACHINE PROFILE" disclosure. All values below are test
data: the serial is an obviously fake one.
"""
from __future__ import annotations

import json
import tomllib
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from goliath_prep import defaults, machine
from goliath_prep.core.params import Param
from goliath_prep.core.types import CheckContext, ClosedGateView, FootprintSet
from goliath_prep.machine import MachineProfile

REPO = Path(__file__).resolve().parents[3]
FIELDS_JSON = REPO / "docs" / "field-guide" / "04-measure-your-robot.fields.json"
SERIAL = "TEST-SERIAL-0001"
FACTS = "cd" * 32

MINIMAL = """\
profile_version = 1
serial = "TEST-SERIAL-0001"
"""

FULL = """\
profile_version = 1
serial = "TEST-SERIAL-0001"

[footprint]
q1_mm = 405.0
q1_deg = 0.5
q2_mm = 212.0
q2_deg = 75.5
q3_mm = 210.0
q3_deg = -76.5

[wheel.q1]
contact_radius_mm = 36.0

[wheel.q2]
contact_radius_mm = 38.0

[wheel.q3]
contact_radius_mm = 37.0

[body]
ahead_of_tool_mm = 90.0
behind_tool_mm = 470.0
left_of_axis_mm = 260.0
right_of_axis_mm = 255.0
max_radius_mm = 500.0

[tower]
base_radius_mm = 110.0
base_length_mm = 200.0
base_width_mm = 180.0

[wire]
attachment = "offset"
same_point = "yes"
back_mm = 150.0
right_mm = -5.0

[stop]
kind = "estop_button_on_robot"
location = "red button on the back of the robot"
latching = "yes"
cuts = "controller_power"
cuts_basis = "label"

[rf]
t1_channel = 101
t2_channel = 114

[policy]
m_edge = 60.0
m_w = 20.0
reach_max = 3200.0
battery_ack_below = 60.0
battery_ack_job_min = 10.0
"""

# The fingerprint of FULL as hand-written, pinned: the same bytes on every system.
FULL_FINGERPRINT = "ee4bc753efa8dcd3caf258da24382da0f8e4fbd8ef716c6dcd8190fbd60529d6"


def write(tmp_path: Path, text: str, name: str = "machine.toml", newline: str = "\n") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return path


def load(tmp_path: Path, text: str) -> MachineProfile:
    return MachineProfile.load(write(tmp_path, text))


def stamped(text: str, facts: str = FACTS) -> str:
    """``text`` with the export stamp goliath-run writes: its content hash and the facts it came from."""
    digest = machine.export_content_sha256(tomllib.loads(text, parse_float=Decimal))
    return text + '\n[export]\nby = "goliath-run 0.0.1"\nfacts_sha256 = "%s"\ncontent_sha256 = "%s"\n' % (
        facts, digest)


def with_section(text: str, section: str, body: str) -> str:
    """``text`` with ``[section]`` replaced by ``body`` (the section's lines up to the next header)."""
    head, rest = text.split("[%s]\n" % section, 1)
    tail = rest.split("\n[", 1)
    return head + "[%s]\n" % section + body + ("\n[" + tail[1] if len(tail) == 2 else "")


# ---------------------------------------------------------------------------
# A profile with nothing measured
# ---------------------------------------------------------------------------


def test_minimal_profile_keeps_every_cautious_default(tmp_path: Path) -> None:
    profile = load(tmp_path, MINIMAL)
    assert profile.serial == SERIAL and profile.origin == "hand-written"
    p = profile.params
    assert p["r_w"] is defaults.SPEC_PARAMS["r_w"]
    assert p["tower_base_radius"] is defaults.SPEC_PARAMS["tower_base_radius"]
    assert p["body_hull_margin"] is defaults.SPEC_PARAMS["body_hull_margin"]
    assert p["wire_attachment"] is defaults.SPEC_PARAMS["wire_attachment"]
    assert p["reach_max"] is defaults.SPEC_PARAMS["reach_max"]
    assert p["body_hull"].value == "default" and p["body_hull"].source == "DEFAULT_UNMEASURED"
    rho = p["rho"]
    assert (rho.value, rho.source) == (55.0, "DERIVED")
    assert {i.ref for i in rho.inputs} == {"docs/prep/params.json r_w", "docs/prep/params.json m_w"}
    assert any(d.id == "DEFAULT_UNMEASURED:r_w" and d.severity == "amber" for d in rho.all_disclosures())
    assert profile.footprint_models() == defaults.vendor_footprint()
    assert profile.wire_attachment is None and profile.body_outline is None and profile.rf_pair is None
    assert profile.unmeasured() == sorted(machine.MEASURED_FIELDS)
    assert all(v is None for v in profile.stop.values())
    ids = {d.id for d in profile.disclosures()}
    assert {"DEFAULT_UNMEASURED:r_w", "DEFAULT_UNMEASURED:wire_attachment", "DEFAULT_UNMEASURED:tower_base_radius",
            "DEFAULT_UNMEASURED:body_hull_margin"} <= ids


@pytest.mark.parametrize(
    ("text", "key"),
    [("serial = \"TEST-SERIAL-0001\"\n", "profile_version"), ("profile_version = 1\n", "serial")],
)
def test_missing_profile_identity_refuses(tmp_path: Path, text: str, key: str) -> None:
    with pytest.raises(defaults.MissingValueError) as info:
        load(tmp_path, text)
    assert info.value.key == key and "machine.toml" in str(info.value)


def test_profile_version_and_serial_are_checked(tmp_path: Path) -> None:
    with pytest.raises(defaults.InputFileError, match="profile_version"):
        load(tmp_path, MINIMAL.replace("profile_version = 1", "profile_version = 2"))
    with pytest.raises(defaults.InputFileError, match="serial"):
        load(tmp_path, MINIMAL.replace('"TEST-SERIAL-0001"', '"  "'))
    with pytest.raises(defaults.InputFileError, match="serial"):
        load(tmp_path, MINIMAL.replace('"TEST-SERIAL-0001"', "1234"))


# ---------------------------------------------------------------------------
# Hand-written, exported and edited profiles
# ---------------------------------------------------------------------------


def test_hand_written_profile_loads_as_owner_profile(tmp_path: Path) -> None:
    profile = load(tmp_path, FULL)
    assert profile.origin == "hand-written" and profile.facts_sha256 is None
    r_w = profile.params["r_w"]
    assert r_w.value == 38.0 and r_w.source == "DERIVED"
    assert [i.value for i in r_w.inputs] == [36.0, 38.0, 37.0]
    assert all(i.source == "OWNER_PROFILE" for i in r_w.inputs)
    assert [i.ref for i in r_w.inputs] == ["machine.toml wheel.q%d.contact_radius_mm" % k for k in (1, 2, 3)]
    owner = [d for d in r_w.all_disclosures() if d.id == "OWNER_PROFILE:machine.toml"]
    assert owner and owner[0].severity == "amber" and "by hand" in owner[0].text
    assert profile.params["rho"].value == 58.0  # 38 + the owner's m_w of 20
    assert profile.params["support_r1_measured"].source == "OWNER_PROFILE"
    assert profile.unmeasured() == []
    for name in ("rf_pair", "stop.kind", "stop.location"):
        assert profile.runner_values[name].source == "OWNER_PROFILE", name


def test_exported_profile_loads_as_robot_measured(tmp_path: Path) -> None:
    profile = load(tmp_path, stamped(FULL))
    assert profile.origin == "exported" and profile.facts_sha256 == FACTS
    assert profile.exported_by == "goliath-run 0.0.1"
    r_w = profile.params["r_w"]
    assert r_w.source == "DERIVED" and all(i.source == "ROBOT_MEASURED" for i in r_w.inputs)
    assert all(FACTS[:12] in i.ref for i in r_w.inputs)
    for name in ("support_r1_measured", "support_delta2_measured", "tower_base_radius", "wire_attachment",
                 "wire_attachment_back", "wire_attachment_right", "body_hull"):
        assert profile.params[name].source == "ROBOT_MEASURED", name
    for name in ("rf_pair", "stop.kind", "stop.cuts"):
        assert profile.runner_values[name].source == "ROBOT_MEASURED", name
    assert "OWNER_PROFILE:machine.toml" not in {d.id for d in profile.disclosures()}
    # owner policy stays the owner's, also in an exported profile
    assert profile.params["m_edge"].source == "OWNER_PROFILE"


@pytest.mark.parametrize("section", ["rf", "stop"])
def test_hand_written_runner_values_are_disclosed(tmp_path: Path, section: str) -> None:
    # A hand-written profile that holds only the RF pair or only the stop's description still says so:
    # those values reach the pairing screen and the arming checklist, so they carry the profile's provenance.
    body = FULL.split("[%s]\n" % section, 1)[1].split("\n[", 1)[0]
    profile = load(tmp_path, MINIMAL + "\n[%s]\n%s" % (section, body))
    assert profile.origin == "hand-written"
    held = [p for p in profile.runner_values.values()]
    assert held and all(p.source == "OWNER_PROFILE" for p in held)
    assert all(p.ref.startswith("machine.toml %s" % section) for p in held)
    (owner,) = [d for d in profile.disclosures() if d.id == "OWNER_PROFILE:machine.toml"]
    assert owner.severity == "amber"
    if section == "rf":
        assert profile.rf_pair == (101, 114)
        assert profile.runner_values["rf_pair"].value == "101|114"
        assert profile.runner_values["rf_pair"].ref == "machine.toml rf.t1_channel, rf.t2_channel"
    else:
        assert profile.stop["stop.kind"].value == "estop_button_on_robot"
        assert profile.rf_pair is None and "rf_pair" not in profile.runner_values
    # nothing in the file: nothing to disclose about its origin, and no runner value is invented
    bare = load(tmp_path, MINIMAL)
    assert dict(bare.runner_values) == {}
    assert "OWNER_PROFILE:machine.toml" not in {d.id for d in bare.disclosures()}


def test_edited_export_loads_as_owner_profile(tmp_path: Path) -> None:
    text = stamped(FULL).replace("contact_radius_mm = 38.0", "contact_radius_mm = 30.0")
    profile = load(tmp_path, text)
    assert profile.origin == "edited"
    assert profile.params["r_w"].value == 37.0
    assert all(i.source == "OWNER_PROFILE" for i in profile.params["r_w"].inputs)
    (owner,) = [d for d in profile.disclosures() if d.id == "OWNER_PROFILE:machine.toml"]
    assert owner.severity == "amber" and "changed after" in owner.text


def test_owner_policy_is_outside_the_export_stamp(tmp_path: Path) -> None:
    # tightening a margin is the owner's right; it does not turn the measurements into hand entries
    profile = load(tmp_path, stamped(FULL).replace("m_edge = 60.0", "m_edge = 70.0"))
    assert profile.origin == "exported" and profile.params["m_edge"].value == 70.0
    assert profile.params["support_r1_measured"].source == "ROBOT_MEASURED"
    # but a changed serial, RF pair or measurement voids it
    for old, new in ((SERIAL, "TEST-SERIAL-0002"), ("t2_channel = 114", "t2_channel = 116\n"),
                     ("q1_deg = 0.5", "q1_deg = 0.6")):
        text = stamped(FULL).replace(old, new)
        if old.startswith("t2_channel"):
            text = text.replace("t1_channel = 101", "t1_channel = 103")
        assert load(tmp_path, text).origin == "edited", old


def test_export_stamp_is_checked(tmp_path: Path) -> None:
    good = stamped(FULL)
    with pytest.raises(defaults.MissingValueError) as info:
        load(tmp_path, good.replace('by = "goliath-run 0.0.1"\n', ""))
    assert info.value.key == "export.by"
    with pytest.raises(defaults.InputFileError, match="SHA-256"):
        load(tmp_path, good.replace(FACTS, "xyz"))
    with pytest.raises(defaults.UnknownKeyError):
        load(tmp_path, good + 'signature = "x"\n')


def test_export_hash_ignores_layout_but_not_values() -> None:
    doc = tomllib.loads(FULL, parse_float=Decimal)
    same = tomllib.loads(FULL.replace("contact_radius_mm = 36.0", "contact_radius_mm = 36.000"), parse_float=Decimal)
    assert machine.export_content_sha256(doc) == machine.export_content_sha256(same)
    other = tomllib.loads(FULL.replace("contact_radius_mm = 36.0", "contact_radius_mm = 36.5"), parse_float=Decimal)
    assert machine.export_content_sha256(doc) != machine.export_content_sha256(other)
    plain = tomllib.loads(FULL)  # floats as float give the same hash as floats as Decimal
    assert machine.export_content_sha256(plain) == machine.export_content_sha256(doc)
    stamped_doc = tomllib.loads(stamped(FULL), parse_float=Decimal)
    assert machine.export_content_sha256(stamped_doc) == machine.export_content_sha256(doc)


# ---------------------------------------------------------------------------
# Wheel contact radius: the maximum of the three modules, never the mean
# ---------------------------------------------------------------------------


def radii(text: str, q1: str, q2: str, q3: str) -> str:
    for k, value in ((1, q1), (2, q2), (3, q3)):
        text = with_section(text, "wheel.q%d" % k, "contact_radius_mm = %s\n" % value)
    return text


def test_r_w_is_the_maximum_never_the_mean(tmp_path: Path) -> None:
    profile = load(tmp_path, radii(FULL, "30.0", "35.0", "45.0"))
    assert profile.params["r_w"].value == 45.0
    assert profile.params["r_w"].value != pytest.approx((30 + 35 + 45) / 3)
    assert profile.params["rho"].value == 65.0


def test_r_w_with_an_unknown_module_stays_unmeasured(tmp_path: Path) -> None:
    larger = load(tmp_path, radii(FULL, "30.0", '"unknown"', "45.0")).params["r_w"]
    assert (larger.value, larger.source) == (45.0, "DEFAULT_UNMEASURED")
    assert larger.disclosures[0].severity == "amber" and "wheel.q2.contact_radius_mm" in larger.disclosures[0].text
    smaller = load(tmp_path, radii(FULL, "30.0", '"unknown"', "35.0"))
    assert (smaller.params["r_w"].value, smaller.params["r_w"].source) == (40.0, "DEFAULT_UNMEASURED")
    assert smaller.unmeasured() == ["wheel.q2.contact_radius_mm"]
    with pytest.raises(defaults.InputFileError, match="greater than 0"):
        load(tmp_path, radii(FULL, "0.0", "35.0", "35.0"))
    with pytest.raises(defaults.InputFileError, match="unknown"):
        load(tmp_path, radii(FULL, '"n/a"', "35.0", "35.0"))


RADIUS = st.one_of(st.none(), st.integers(min_value=1, max_value=200_000).map(lambda t: Decimal(t).scaleb(-3)))


@given(st.tuples(RADIUS, RADIUS, RADIUS))
def test_r_w_property(radius: tuple[Decimal | None, Decimal | None, Decimal | None]) -> None:
    # Any three readings (None = "unknown"): r_w is their maximum when all are known, else the larger of the
    # 40 mm default and the known ones; it is never below a known reading, and rho = r_w + m_w exactly.
    document: dict = {"profile_version": 1, "serial": SERIAL, "wheel": {}}
    for k, value in enumerate(radius, start=1):
        document["wheel"]["q%d" % k] = {"contact_radius_mm": "unknown" if value is None else value}
    profile = MachineProfile.from_mapping(document)
    r_w, rho = profile.params["r_w"], profile.params["rho"]
    known = [float(v) for v in radius if v is not None]
    if len(known) == 3:
        assert r_w.value == max(known) and r_w.source == "DERIVED"
        assert [i.value for i in r_w.inputs] == known
    else:
        assert r_w.value == max([40.0] + known) and r_w.source == "DEFAULT_UNMEASURED"
        assert any(d.severity == "amber" for d in r_w.all_disclosures())
    assert all(r_w.value >= v for v in known)
    assert Decimal(repr(rho.value)) == Decimal(repr(r_w.value)) + Decimal(15)
    rho.to_json()  # exact on the canonical grid


# ---------------------------------------------------------------------------
# Footprint
# ---------------------------------------------------------------------------


def test_footprint_union_once_measured(tmp_path: Path) -> None:
    fp = load(tmp_path, FULL).footprint_models()
    assert isinstance(fp, FootprintSet)
    assert fp.vendor == defaults.vendor_footprint().vendor
    assert fp.measured == ((405.0, 0.5), (212.0, 75.5), (210.0, -76.5)) and fp.active == "union"
    partial = load(tmp_path, FULL.replace("q2_deg = 75.5", 'q2_deg = "unknown"'))
    assert partial.footprint_models().measured == ((405.0, 0.5), None, (210.0, -76.5))
    assert "support_r2_measured" not in partial.params and "support_delta2_measured" not in partial.params
    assert partial.unmeasured() == ["footprint.q2_deg"]
    # the distance that was measured is not lost silently: a note says why it adds no position
    (note,) = [d for d in partial.notes if "footprint.q2_mm" in d.text]
    assert "footprint.q2_deg" in note.text and "212 mm" in note.text and note in partial.disclosures()
    with pytest.raises(defaults.InputFileError, match="180"):
        load(tmp_path, FULL.replace("q2_deg = 75.5", "q2_deg = 190.0"))
    with pytest.raises(defaults.InputFileError, match="greater than 0"):
        load(tmp_path, FULL.replace("q1_mm = 405.0", "q1_mm = 0.0"))


def test_profile_params_build_a_check_context(tmp_path: Path) -> None:
    for text in (MINIMAL, FULL, stamped(FULL)):
        profile = load(tmp_path, text)
        fp = profile.footprint_models()
        farthest = max(r for r, _ in fp.vendor + tuple(p for p in (fp.measured or ()) if p is not None))
        r_rot = Param.derived(float(farthest + 5), "mm", "test stand-in for R_rot",
                              (defaults.SPEC_PARAMS["support_r1"],))
        params = dict(defaults.SPEC_PARAMS)
        params.update(profile.params)
        params["r_rot"] = r_rot
        params["rotation_model"] = Param("unverified", "enum", "GATE", "test gate rotation_model")
        params["heading_map_verified"] = Param("unverified", "enum", "GATE", "test gate heading_map_verified")
        context = CheckContext(
            params=params, footprint=fp, rho=profile.params["rho"].value, r_rot=r_rot.value,
            gates=ClosedGateView(), rotation_model="unverified", heading_map_verified=False, start_pose=None,
            measured=None, obstacles=(), fixations=(), prior_removal=(),
        )
        assert context.footprint == fp


# ---------------------------------------------------------------------------
# Wire attachment (red-team PHYS-05; U-49)
# ---------------------------------------------------------------------------


def wire(text: str, attachment: str, same_point: str, back: str, right: str) -> str:
    return with_section(text, "wire", 'attachment = %s\nsame_point = %s\nback_mm = %s\nright_mm = %s\n' % (
        attachment, same_point, back, right))


def test_unmeasured_wire_attachment_means_body_hull(tmp_path: Path) -> None:
    unmeasured = load(tmp_path, MINIMAL)
    assert unmeasured.wire_attachment is None
    param = unmeasured.params["wire_attachment"]
    assert (param.value, param.source) == ("body-hull", "DEFAULT_UNMEASURED")
    assert param.disclosures[0].severity == "amber"
    assert {"wire.attachment", "wire.same_point", "wire.back_mm", "wire.right_mm"} <= set(unmeasured.unmeasured())
    # exported from typed measurements, so only the unknown keeps the body hull
    for args in (('"unknown"', '"yes"', "150.0", "0.0"), ('"not_found"', '"yes"', "150.0", "0.0"),
                 ('"offset"', '"unknown"', "150.0", "0.0"), ('"offset"', '"yes"', '"unknown"', "0.0"),
                 ('"offset"', '"yes"', "150.0", '"unknown"'), ('"on_spindle_axis"', '"yes"', '"unknown"', "0.0")):
        profile = load(tmp_path, stamped(wire(FULL, *args)))
        assert profile.origin == "exported", args
        assert profile.wire_attachment is None, args
        assert profile.params["wire_attachment"].value == "body-hull", args
        assert profile.params["wire_attachment"].source == "DEFAULT_UNMEASURED", args
        assert "wire_attachment_back" not in profile.params, args
    two_points = load(tmp_path, stamped(wire(FULL, '"offset"', '"no"', "150.0", "0.0")))
    assert two_points.wire_attachment is None
    assert any("design review" in d.text for d in two_points.params["wire_attachment"].disclosures)


def test_measured_wire_attachment_is_a_body_frame_offset(tmp_path: Path) -> None:
    offset = load(tmp_path, stamped(FULL))
    assert offset.wire_attachment == (150.0, -5.0)  # (back, right) from T in the body frame
    assert offset.params["wire_attachment"].value == "measured-point"
    assert offset.params["wire_attachment"].source == "ROBOT_MEASURED"
    assert offset.params["wire_attachment_back"].value == 150.0
    assert offset.params["wire_attachment_right"].value == -5.0
    spindle = load(tmp_path, stamped(wire(FULL, '"on_spindle_axis"', '"yes"', "1.5", "-1.0")))
    assert spindle.wire_attachment == (0.0, 0.0)
    # a contradiction or a word outside the field guide's choices is refused whoever wrote the file
    for text in (wire(FULL, '"on_spindle_axis"', '"yes"', "12.0", "0.0"),
                 stamped(wire(FULL, '"on_spindle_axis"', '"yes"', "12.0", "0.0"))):
        with pytest.raises(defaults.InputFileError, match="2 mm"):
            load(tmp_path, text)
    with pytest.raises(defaults.InputFileError, match="attachment"):
        load(tmp_path, wire(FULL, '"magnetic"', '"yes"', "12.0", "0.0"))


@pytest.mark.parametrize("edit", ["hand-written", "edited"])
def test_hand_entered_wire_attachment_counts_the_body_hull(tmp_path: Path, edit: str) -> None:
    # P-REACH uses a measured attachment point only once rung R0b has recorded it as a typed measurement
    # (gate wire_attachment_measured): a point written or changed by hand keeps the whole body hull, visibly.
    text = FULL if edit == "hand-written" else stamped(FULL).replace("back_mm = 150.0", "back_mm = 120.0")
    profile = load(tmp_path, text)
    assert profile.origin == edit
    assert profile.wire_attachment is None
    param = profile.params["wire_attachment"]
    assert (param.value, param.source) == ("body-hull", "DEFAULT_UNMEASURED")
    assert "wire_attachment_back" not in profile.params and "wire_attachment_right" not in profile.params
    (note,) = [d for d in param.disclosures if d.id == "OWNER_PROFILE:wire.attachment"]
    assert note.severity == "amber"
    assert "body hull" in note.text and "R0b" in note.text
    assert ("%s mm" % ("150" if edit == "hand-written" else "120")) in note.text
    # the hand entry is kept as read, not lost
    assert profile.readings["wire.back_mm"] == (150.0 if edit == "hand-written" else 120.0)
    assert "wire.back_mm" not in profile.unmeasured()
    assert note in profile.disclosures()


# ---------------------------------------------------------------------------
# Body hull and tower base
# ---------------------------------------------------------------------------


def test_body_hull_from_extents_outline_or_default(tmp_path: Path) -> None:
    box = load(tmp_path, FULL)
    assert box.body_outline == ((-90.0, -260.0), (-90.0, 255.0), (470.0, 255.0), (470.0, -260.0))
    assert box.params["body_hull"].value == "measured-extents" and "body_hull_margin" not in box.params
    assert box.params["body_max_radius"].value == 500.0
    outline = load(tmp_path, with_outline(FULL, "[[-90, 0], [-40, 255], [470, 200], [470, -260], [-40, -150]]"))
    assert outline.body_outline == ((-90.0, 0.0), (-40.0, 255.0), (470.0, 200.0), (470.0, -260.0), (-40.0, -150.0))
    assert outline.params["body_hull"].value == "measured-outline"
    one_unknown = load(tmp_path, FULL.replace("left_of_axis_mm = 260.0", 'left_of_axis_mm = "unknown"'))
    assert one_unknown.body_outline is None and one_unknown.params["body_hull"].value == "default"
    assert one_unknown.params["body_hull_margin"] is defaults.SPEC_PARAMS["body_hull_margin"]
    (note,) = [d for d in one_unknown.params["body_hull"].disclosures if d.id == "PARTIAL:body"]
    assert "body.left_of_axis_mm" in note.text and "body.behind_tool_mm = 470 mm" in note.text
    with pytest.raises(defaults.InputFileError, match="outline_points"):
        load(tmp_path, FULL.replace("max_radius_mm = 500.0", "outline_points = [[0, 0], [1, 1]]"))
    # field guide 04's "unknown" reads as no outline, as for every other field; any other text is refused
    no_outline = load(tmp_path, with_outline(FULL, '"unknown"'))
    assert no_outline.body_outline == box.body_outline and no_outline.params["body_hull"].value == "measured-extents"
    with pytest.raises(defaults.InputFileError, match="outline_points"):
        load(tmp_path, with_outline(FULL, '"-60,0; -40,150; 460,200"'))


def with_outline(text: str, points: str) -> str:
    return text.replace("max_radius_mm = 500.0", "max_radius_mm = 500.0\noutline_points = %s" % points)


@pytest.mark.parametrize(
    ("points", "field", "outline_reach", "extent"),
    [
        # the rear-most dot is missing from the outline: the hull would be 10 mm short at the back
        ("[[-90, 0], [-40, 255], [460, 200], [460, -260], [-40, -150]]", "body.behind_tool_mm", "460", "470"),
        ("[[-80, 0], [-40, 255], [470, 200], [470, -260], [-40, -150]]", "body.ahead_of_tool_mm", "80", "90"),
        ("[[-90, 0], [-40, 250], [470, 200], [470, -260], [-40, -150]]", "body.right_of_axis_mm", "250", "255"),
        # an outline beyond a measured extent contradicts it as well: both describe the same outline dots
        ("[[-90, 0], [-40, 255], [470, 200], [470, -270], [-40, -150]]", "body.left_of_axis_mm", "270", "260"),
    ],
)
def test_body_outline_must_reach_the_measured_extents(tmp_path: Path, points: str, field: str, outline_reach: str,
                                                      extent: str) -> None:
    with pytest.raises(defaults.InputFileError) as info:
        load(tmp_path, with_outline(FULL, points))
    message = str(info.value)
    assert info.value.key == "body.outline_points"
    assert field in message and ("%s mm" % outline_reach) in message and ("%s mm" % extent) in message
    # with that extent unknown, the outline alone describes the body and is used
    key = field.split(".", 1)[1]
    unknown = FULL.replace("%s = %s.0" % (key, extent), '%s = "unknown"' % key)
    assert unknown != FULL
    profile = load(tmp_path, with_outline(unknown, points))
    assert profile.params["body_hull"].value == "measured-outline"
    assert profile.body_outline is not None and len(profile.body_outline) == 5


def test_tower_base_radius_rules(tmp_path: Path) -> None:
    measured = load(tmp_path, FULL).params["tower_base_radius"]
    assert (measured.value, measured.source) == (110.0, "OWNER_PROFILE")
    with pytest.raises(defaults.InputFileError, match="design review"):
        load(tmp_path, FULL.replace("base_length_mm = 200.0", "base_length_mm = 300.0"))
    partial = load(tmp_path, FULL.replace("base_width_mm = 180.0", 'base_width_mm = "unknown"'))
    assert (partial.params["tower_base_radius"].value, partial.params["tower_base_radius"].source) == (
        120.0, "DEFAULT_UNMEASURED")
    larger = load(tmp_path, FULL.replace("base_radius_mm = 110.0", "base_radius_mm = 150.0").replace(
        "base_width_mm = 180.0", 'base_width_mm = "unknown"'))
    assert (larger.params["tower_base_radius"].value, larger.params["tower_base_radius"].source) == (
        150.0, "DEFAULT_UNMEASURED")


# ---------------------------------------------------------------------------
# Owner policy, RF pair, physical stop
# ---------------------------------------------------------------------------


def test_policy_only_tightens(tmp_path: Path) -> None:
    profile = load(tmp_path, FULL)
    assert profile.params["m_edge"].value == 60.0 and profile.params["m_edge"].source == "OWNER_PROFILE"
    assert profile.params["reach_max"].value == 3200.0
    assert profile.params["battery_ack_below"].value == 60.0
    assert profile.params["battery_ack_job_min"].value == 10.0
    for line, new in (("m_edge = 60.0", "m_edge = 40.0"), ("reach_max = 3200.0", "reach_max = 3600.0"),
                      ("battery_ack_below = 60.0", "battery_ack_below = 40.0"),
                      ("battery_ack_job_min = 10.0", "battery_ack_job_min = 20.0"), ("m_w = 20.0", "m_w = 10.0")):
        with pytest.raises(defaults.InputFileError, match="tighten"):
            load(tmp_path, FULL.replace(line, new))
    assert set(machine.POLICY_KEYS) <= set(defaults.OWNER_TIGHTENS)


def test_rf_pair_is_a_vendor_pair(tmp_path: Path) -> None:
    assert load(tmp_path, FULL).rf_pair == (101, 114)
    assert machine.RF_PAIRS == ((101, 114), (103, 116), (105, 118), (107, 120), (109, 122), (111, 124))
    with pytest.raises(defaults.InputFileError, match="CONFIG"):
        load(tmp_path, FULL.replace("t2_channel = 114", "t2_channel = 116"))
    with pytest.raises(defaults.MissingValueError):
        load(tmp_path, FULL.replace("t2_channel = 114\n", ""))


def test_stop_description(tmp_path: Path) -> None:
    profile = load(tmp_path, stamped(FULL))
    assert set(profile.stop) == set(machine.STOP_FIELDS)
    assert profile.stop["stop.kind"].value == "estop_button_on_robot"
    assert profile.stop["stop.location"].value == "red button on the back of the robot"
    kind = profile.stop["stop.kind"]
    assert (kind.unit, kind.source) == ("enum", "ROBOT_MEASURED") and kind.ref.startswith("machine.toml stop.kind")
    partial = load(tmp_path, FULL.replace('latching = "yes"', 'latching = "unknown"'))
    assert partial.stop["stop.latching"] is None and partial.unmeasured() == ["stop.latching"]
    assert "stop.latching" not in partial.runner_values
    with pytest.raises(defaults.InputFileError, match="stop.kind"):
        load(tmp_path, FULL.replace('kind = "estop_button_on_robot"', 'kind = "big red thing"'))


# ---------------------------------------------------------------------------
# Field guide 04 is the vocabulary
# ---------------------------------------------------------------------------


def test_measured_fields_are_field_guide_ids() -> None:
    fields = {f["id"]: f for f in json.loads(FIELDS_JSON.read_text(encoding="utf-8"))["fields"]}
    for field_id in machine.MEASURED_FIELDS:
        assert field_id in fields, field_id
        assert fields[field_id]["measurement_entry"] is True, field_id
    for field_id, choices in machine.FIELD_CHOICES.items():
        assert tuple(fields[field_id]["choices"]) == choices, field_id
    for field_id in machine.MEASURED_FIELDS:
        kind = fields[field_id]["kind"]
        assert kind in ("number", "choice", "text"), field_id
        assert (kind == "choice") == (field_id in machine.FIELD_CHOICES), field_id
    assert "body.outline_points" in fields and "body.outline_points" not in machine.MEASURED_FIELDS
    # the parameter catalogue's measured_by names only fields the profile reads
    catalogue = json.loads((REPO / "docs" / "prep" / "params.json").read_text(encoding="utf-8"))
    read = set(machine.MEASURED_FIELDS) | {"body.outline_points"}
    for entry in catalogue:
        if entry["name"] in ("r_w", "body_hull_margin", "tower_base_radius", "wire_attachment"):
            assert set(entry["measured_by"]) <= read, entry["name"]
        for field_id in entry.get("measured_by", ()):
            if field_id.startswith("footprint."):
                assert field_id in read, field_id


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_stable_across_os(tmp_path: Path) -> None:
    lf = load(tmp_path, FULL)
    assert len(lf.fingerprint()) == 64
    assert lf.fingerprint() == FULL_FINGERPRINT
    crlf = MachineProfile.load(write(tmp_path / "windows", FULL, "Robot A.toml", newline="\r\n"))
    bom = tmp_path / "bom.toml"
    bom.write_bytes(b"\xef\xbb\xbf" + FULL.encode("utf-8"))
    reordered = FULL.replace("[rf]\nt1_channel = 101\nt2_channel = 114\n\n", "").replace(
        "[footprint]\n", "[rf]\nt2_channel = 114\nt1_channel = 101\n\n[footprint]\n")
    assert reordered != FULL and "t1_channel = 101" in reordered
    integers = FULL.replace("contact_radius_mm = 36.0", "contact_radius_mm = 36").replace("q1_mm = 405.0",
                                                                                           "q1_mm = 405")
    for other in (crlf, MachineProfile.load(bom), load(tmp_path, reordered), load(tmp_path, integers)):
        assert other.fingerprint() == lf.fingerprint()
    assert load(tmp_path, FULL.replace("q1_mm = 405.0", "q1_mm = 405.5")).fingerprint() != lf.fingerprint()
    assert load(tmp_path, FULL.replace(SERIAL, "TEST-SERIAL-0002")).fingerprint() != lf.fingerprint()
    assert load(tmp_path, stamped(FULL)).fingerprint() != lf.fingerprint()  # exported: other provenance


def test_profile_json_is_canonical_and_complete(tmp_path: Path) -> None:
    from goliath_prep.core.canon import canonical_json

    profile = load(tmp_path, FULL)
    data = profile.to_json()
    canonical_json(data)
    assert data["serial"] == SERIAL and data["origin"] == "hand-written"
    assert data["readings"]["wheel.q2.contact_radius_mm"] == 38.0
    assert data["rf_pair"] == [101, 114]
    assert set(data["params"]) == set(profile.params)
    assert set(data["runner_values"]) == set(profile.runner_values) == {"rf_pair"} | set(machine.STOP_FIELDS)
    assert data["runner_values"]["rf_pair"]["value"] == "101|114"


def test_profile_invariants_hold_for_direct_construction(tmp_path: Path) -> None:
    import dataclasses

    exported = load(tmp_path, stamped(FULL))
    hand = load(tmp_path, FULL)
    # a measured wire point exists only in an exported profile, and only with its measured-point Param
    with pytest.raises(ValueError, match="exported"):
        dataclasses.replace(hand, wire_attachment=(150.0, -5.0))
    with pytest.raises(ValueError, match="measured-point"):
        dataclasses.replace(exported, wire_attachment=None)
    with pytest.raises(ValueError, match="runner_values"):
        dataclasses.replace(hand, runner_values={"colour": hand.runner_values["rf_pair"]})
    with pytest.raises(ValueError, match="rf_pair"):
        dataclasses.replace(hand, rf_pair=None)
    with pytest.raises(TypeError, match="notes"):
        dataclasses.replace(hand, notes=["a note"])


# ---------------------------------------------------------------------------
# Unknown keys
# ---------------------------------------------------------------------------

MACHINE_UNKNOWN_KEYS = [
    ('colour = "red"\n' + MINIMAL, "colour"),
    (MINIMAL + "\n[wheel.q1]\ncontact_radius = 38.0\n", "wheel.q1.contact_radius"),
    (MINIMAL + "\n[wheel.q4]\ncontact_radius_mm = 38.0\n", "wheel.q4"),
    (MINIMAL + "\n[wheel]\nradius = 38.0\n", "wheel.radius"),
    (MINIMAL + '\n[wire]\nswivel = "swivel"\n', "wire.swivel"),
    (MINIMAL + "\n[body]\nwidth_mm = 500.0\n", "body.width_mm"),
    (MINIMAL + "\n[footprint]\nq1_back_mm = 402.0\n", "footprint.q1_back_mm"),
    (MINIMAL + "\n[policy]\ngeom_tol = 20.0\n", "policy.geom_tol"),
    (MINIMAL + '\n[stop]\ncolour = "red"\n', "stop.colour"),
    (MINIMAL + "\n[rf]\nt1_channel = 101\nt2_channel = 114\nconfig = 0\n", "rf.config"),
    (MINIMAL + '\n[session]\noperator = "someone"\n', "session"),
]


@pytest.mark.parametrize(("text", "key"), MACHINE_UNKNOWN_KEYS)
def test_unknown_machine_keys_refused(tmp_path: Path, text: str, key: str) -> None:
    with pytest.raises(defaults.UnknownKeyError) as info:
        load(tmp_path, text)
    assert info.value.key == key
    assert key in str(info.value)
