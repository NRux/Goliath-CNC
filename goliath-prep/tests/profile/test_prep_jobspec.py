# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""job.toml: the job file, read strictly (unit P01; red-team C-01, PHYS-06, PHYS-12).

Required values are refused when missing, unknown keys are refused, and every
value that is not written in the file is a Param that says so (a disclosed
default). The GI conformance manifest states each example's job as a JSON
object; JobSpec.from_mapping reads exactly that object, and the same values
written as job.toml give the same JobSpec.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from goliath_prep import defaults
from goliath_prep.core.types import Fixation, Obstacle, PriorCut
from goliath_prep.jobspec import JobSpec, Panel

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "docs" / "prep" / "gi-examples" / "manifest.json"
FIELD_GUIDE_04 = REPO / "docs" / "field-guide" / "04-measure-your-robot.fields.json"

MINIMAL = """\
[panel]
W = 2440
H = 1220
T = 18
tower2 = "Y"

[tool]
d = 6
flute = 22
"""

FULL = """\
units = "mm"
z_zero = "top"

[panel]
W = 2440.0
H = 1220.0
T = 17.6
tower2 = "X"

[placement]
dx = 0.0
dy = 0.0

[tool]
d = 6.0
flute = 22.0
stickout = 40.0
rpm = 18000
kind = "flat"
max_stepdown = 3.0

[supports]
right = 300.0

[feeds]
feed_cap = 1500.0
rpm_cap = 20000

[policy]
obstacle_clearance = 5.0
overcut_max = 0.3
delta_tool = 0.3

[[obstacles]]
polygon = [[100.0, 100.0], [180.0, 100.0], [180.0, 160.0], [100.0, 160.0]]
height_mm = 25.0
kind = "clamp"

[[obstacles]]
polygon = [[2000, 1000], [2010, 1000], [2010, 1010]]
height_mm = 3.5
kind = "screw head"

[[fixations]]
point = [1200.0, 600.0]
height_mm = 4.5

[[prior_removal]]
polygon = [[500.0, 500.0], [700.0, 500.0], [700.0, 650.0]]
depth_mm = 6.0

[series]
prior_material = "../job-1/material-after.json"
prior_sha256 = "%s"
""" % ("ab" * 32)


def write(tmp_path: Path, text: str, name: str = "job.toml", newline: str = "\n") -> Path:
    path = tmp_path / name
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return path


def load(tmp_path: Path, text: str) -> JobSpec:
    return JobSpec.load(write(tmp_path, text))


def without(text: str, line: str) -> str:
    assert line + "\n" in text, line
    return text.replace(line + "\n", "", 1)


# ---------------------------------------------------------------------------
# Required values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ("T = 18", "panel.T"),
        ("W = 2440", "panel.W"),
        ("H = 1220", "panel.H"),
        ('tower2 = "Y"', "panel.tower2"),
        ("d = 6", "tool.d"),
        ("flute = 22", "tool.flute"),
    ],
)
def test_missing_required_values_refuse(tmp_path: Path, line: str, key: str) -> None:
    with pytest.raises(defaults.MissingValueError) as info:
        load(tmp_path, without(MINIMAL, line))
    assert info.value.key == key
    assert key in str(info.value) and "job.toml" in str(info.value)


def test_missing_required_tables_refuse(tmp_path: Path) -> None:
    with pytest.raises(defaults.MissingValueError) as info:
        load(tmp_path, "")
    assert info.value.key == "panel"
    with pytest.raises(defaults.MissingValueError) as info:
        load(tmp_path, MINIMAL.split("[tool]")[0])
    assert info.value.key == "tool"


def test_missing_units_refuse_only_when_the_file_has_no_g21(tmp_path: Path) -> None:
    job = load(tmp_path, MINIMAL)
    assert job.units is None
    with pytest.raises(defaults.MissingValueError) as info:
        job.units_for(file_has_g21=False)
    assert info.value.key == "units" and "G21" in str(info.value)
    from_g21 = job.units_for(file_has_g21=True)
    assert from_g21.value == "mm" and from_g21.source == "OWNER_JOB" and "G21" in from_g21.ref
    with_units = load(tmp_path, 'units = "mm"\n' + MINIMAL)
    declared = with_units.units_for(file_has_g21=False)
    assert declared is with_units.units and declared.value == "mm" and declared.ref == "job.toml units"
    with pytest.raises(defaults.InputFileError, match="mm"):
        load(tmp_path, 'units = "inch"\n' + MINIMAL)


# ---------------------------------------------------------------------------
# Values and their sources
# ---------------------------------------------------------------------------


def test_full_job_values(tmp_path: Path) -> None:
    job = load(tmp_path, FULL)
    assert (job.panel.W.value, job.panel.H.value, job.panel.T.value, job.panel.tower2.value) == (
        2440.0, 1220.0, 17.6, "X")
    assert job.panel.T.ref == "job.toml panel.T" and job.panel.T.source == "OWNER_JOB"
    assert (job.tool.d.value, job.tool.flute.value, job.tool.stickout.value, job.tool.rpm.value) == (
        6.0, 22.0, 40.0, 18000.0)
    assert job.tool.kind.value == "flat" and job.tool.max_stepdown.value == 3.0
    assert (job.placement.dx.value, job.placement.dy.value) == (0.0, 0.0)
    assert job.placement.dx.ref == "job.toml placement.dx" and not job.placement.dx.disclosures
    assert job.z_zero.value == "top" and job.z_zero.ref == "job.toml z_zero"
    assert job.units.value == "mm"
    assert job.supports.right.value == 300.0 and job.supports.right.ref == "job.toml supports.right"
    assert job.supports.left.value == 0.0 and job.supports.left.disclosures  # not written: disclosed default
    assert job.feeds.feed_cap.value == 1500.0 and job.feeds.feed_cap.unit == "mm/min"
    assert job.feeds.rpm_cap.value == 20000.0
    assert job.policy.obstacle_clearance.value == 5.0
    assert job.policy.overcut_max.value == 0.3 and job.policy.delta_tool.value == 0.3
    assert job.series_prior is not None
    assert job.series_prior.material == "../job-1/material-after.json"
    assert job.series_prior.sha256 == "ab" * 32
    params = job.params()
    for name in ("panel_width", "panel_height", "stock_thickness", "tower2_axis", "tool_diameter",
                 "tool_flute_length", "tool_stickout", "tool_max_stepdown", "placement_dx", "placement_dy"):
        assert name in params, name
    assert params["stock_thickness"] is job.panel.T
    assert params["feed_cap"] is job.feeds.feed_cap and params["support_strip_width_right"] is job.supports.right


def test_absent_optional_values_are_disclosed_defaults(tmp_path: Path) -> None:
    job = load(tmp_path, MINIMAL)
    assert (job.placement.dx.value, job.placement.dy.value) == (0.0, 0.0)
    for param in (job.placement.dx, job.placement.dy, job.z_zero, job.supports.left, job.supports.right,
                  job.supports.bottom, job.supports.top):
        assert param.disclosures, param.ref
        assert "absent" in param.ref or "not written" in param.ref, param.ref
    assert job.z_zero.value == "top" and job.z_zero.source == "SPEC"
    assert job.tool.stickout is None and job.tool.rpm is None and job.tool.kind is None
    assert job.tool.max_stepdown is None
    assert job.feeds.feed_cap is None and job.feeds.rpm_cap is None
    assert job.policy.obstacle_clearance is None
    assert job.obstacles == () and job.fixations == () and job.prior_removal == ()
    assert job.series_prior is None
    assert "tool_stickout" not in job.params() and "feed_cap" not in job.params()
    assert {d.id for d in job.disclosures()} >= {"DEFAULT:job.placement", "DEFAULT:job.z_zero",
                                                    "DEFAULT:job.supports"}


def test_placement_needs_both_offsets(tmp_path: Path) -> None:
    with pytest.raises(defaults.MissingValueError) as info:
        load(tmp_path, MINIMAL + "\n[placement]\ndx = 100.0\n")
    assert info.value.key == "placement.dy"


def test_z_zero_bottom_and_bad_values(tmp_path: Path) -> None:
    job = load(tmp_path, 'z_zero = "bottom"\n' + MINIMAL)
    assert job.z_zero.value == "bottom" and job.z_zero.source == "OWNER_JOB"
    with pytest.raises(defaults.InputFileError, match="z_zero"):
        load(tmp_path, 'z_zero = "middle"\n' + MINIMAL)


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("T = 18", "T = 0", "greater than 0"),
        ("T = 18", "T = -18", "greater than 0"),
        ("T = 18", 'T = "18"', "number"),
        ("T = 18", "T = true", "number"),
        ("T = 18", "T = nan", "finite"),
        ("T = 18", "T = inf", "finite"),
        ("T = 18", "T = 18.0004", "0.001"),
        ("T = 18", "T = 1e13", "large"),
        ("T = 18", "T = 1979-05-27", "number"),
        ('tower2 = "Y"', 'tower2 = "Z"', "tower2"),
        ('tower2 = "Y"', 'tower2 = "y"', "tower2"),
        ("d = 6", "d = 0", "greater than 0"),
        ("flute = 22", "flute = -1", "greater than 0"),
    ],
)
def test_bad_values_refuse(tmp_path: Path, old: str, new: str, match: str) -> None:
    with pytest.raises(defaults.InputFileError, match=match):
        load(tmp_path, MINIMAL.replace(old, new, 1))


def test_tool_kind_and_optional_values_are_checked(tmp_path: Path) -> None:
    for extra, match in (('kind = "drill"', "kind"), ("rpm = 0", "greater than 0"), ("stickout = -5", "greater"),
                         ("max_stepdown = 0", "greater than 0")):
        with pytest.raises(defaults.InputFileError, match=match):
            load(tmp_path, MINIMAL + extra + "\n")


def test_job_policy_and_feeds_only_tighten(tmp_path: Path) -> None:
    for table, line in (("feeds", "feed_cap = 2500.0"), ("feeds", "rpm_cap = 30000"),
                        ("policy", "obstacle_clearance = 2.0"), ("policy", "overcut_max = 1.0"),
                        ("policy", "delta_tool = 0.1")):
        with pytest.raises(defaults.InputFileError, match="tighten"):
            load(tmp_path, MINIMAL + "\n[%s]\n%s\n" % (table, line))


def test_supports_are_not_negative(tmp_path: Path) -> None:
    with pytest.raises(defaults.InputFileError, match="supports.top"):
        load(tmp_path, MINIMAL + "\n[supports]\ntop = -1.0\n")


def test_series_prior_needs_both_keys_and_a_sha256(tmp_path: Path) -> None:
    with pytest.raises(defaults.MissingValueError) as info:
        load(tmp_path, MINIMAL + '\n[series]\nprior_material = "a.json"\n')
    assert info.value.key == "series.prior_sha256"
    with pytest.raises(defaults.InputFileError, match="SHA-256"):
        load(tmp_path, MINIMAL + '\n[series]\nprior_material = "a.json"\nprior_sha256 = "abc"\n')


# ---------------------------------------------------------------------------
# Unknown keys
# ---------------------------------------------------------------------------

JOB_UNKNOWN_KEYS = [
    ('colour = "red"\n' + MINIMAL, "colour"),
    (MINIMAL.replace("T = 18", "T = 18\nthickness = 18"), "panel.thickness"),
    (MINIMAL.replace("d = 6", "d = 6\ndiameter = 6"), "tool.diameter"),
    (MINIMAL + "\n[placement]\ndx = 0.0\ndy = 0.0\ndz = 1.0\n", "placement.dz"),
    (MINIMAL + "\n[supports]\nfront = 10.0\n", "supports.front"),
    (MINIMAL + "\n[feeds]\nfeed = 1000.0\n", "feeds.feed"),
    (MINIMAL + "\n[policy]\nm_edge = 60.0\n", "policy.m_edge"),
    (MINIMAL + "\n[[obstacles]]\npolygon = [[0, 0], [1, 0], [1, 1]]\nheight_mm = 3.0\nkind = \"clamp\"\n"
     "heigth_mm = 3.0\n", "obstacles[0].heigth_mm"),
    (MINIMAL + "\n[[fixations]]\npoint = [1.0, 1.0]\nheight_mm = 3.0\ndiameter = 4.0\n", "fixations[0].diameter"),
    (MINIMAL + "\n[[prior_removal]]\npolygon = [[0, 0], [1, 0], [1, 1]]\ndepth_mm = 3.0\nthrough = true\n",
     "prior_removal[0].through"),
    (MINIMAL + '\n[series]\nprior_material = "a.json"\nprior_sha256 = "%s"\nprior = "b"\n' % ("0" * 64),
     "series.prior"),
    (MINIMAL + "\n[relaxations]\nkerf = true\n", "relaxations"),
]


@pytest.mark.parametrize(("text", "key"), JOB_UNKNOWN_KEYS)
def test_unknown_job_keys_refused(tmp_path: Path, text: str, key: str) -> None:
    with pytest.raises(defaults.UnknownKeyError) as info:
        load(tmp_path, text)
    assert info.value.key == key
    assert key in str(info.value) and "unknown" in str(info.value)


def test_tables_must_be_tables(tmp_path: Path) -> None:
    with pytest.raises(defaults.InputFileError, match="obstacles"):
        load(tmp_path, MINIMAL + "\nobstacles = 3\n")
    with pytest.raises(defaults.InputFileError, match="panel"):
        load(tmp_path, 'panel = "big"\n' + MINIMAL.replace("[panel]", "[panelx]"))


# ---------------------------------------------------------------------------
# Obstacles, fixations, prior removal (red-team C-01, PHYS-06)
# ---------------------------------------------------------------------------


def test_obstacles_fixations_prior_removal_parsed(tmp_path: Path) -> None:
    job = load(tmp_path, FULL)
    assert len(job.obstacles) == 2 and all(isinstance(o, Obstacle) for o in job.obstacles)
    clamp, screw = job.obstacles
    assert clamp.polygon == ((100.0, 100.0), (180.0, 100.0), (180.0, 160.0), (100.0, 160.0))
    assert clamp.height_mm == 25.0 and clamp.kind == "clamp" and clamp.ref == "job.toml obstacles[0]"
    assert screw.polygon == ((2000.0, 1000.0), (2010.0, 1000.0), (2010.0, 1010.0))
    assert screw.height_mm == 3.5 and screw.kind == "screw head" and screw.ref == "job.toml obstacles[1]"
    (fix,) = job.fixations
    assert isinstance(fix, Fixation)
    assert fix.point == (1200.0, 600.0) and fix.height_mm == 4.5 and fix.ref == "job.toml fixations[0]"
    (prior,) = job.prior_removal
    assert isinstance(prior, PriorCut)
    assert prior.polygon == ((500.0, 500.0), (700.0, 500.0), (700.0, 650.0))
    assert prior.depth_mm == 6.0 and prior.ref == "job.toml prior_removal[0]"


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ("[[obstacles]]\npolygon = [[0, 0], [1, 0], [1, 1]]\nkind = \"clamp\"\n", "obstacles\\[0\\].height_mm"),
        ("[[obstacles]]\npolygon = [[0, 0], [1, 0], [1, 1]]\nheight_mm = 3.0\n", "obstacles\\[0\\].kind"),
        ("[[obstacles]]\nheight_mm = 3.0\nkind = \"clamp\"\n", "obstacles\\[0\\].polygon"),
        ("[[obstacles]]\npolygon = [[0, 0], [1, 0]]\nheight_mm = 3.0\nkind = \"clamp\"\n", "3 vertices"),
        ("[[obstacles]]\npolygon = [[0, 0], [1, 1], [1, 0], [0, 1]]\nheight_mm = 3.0\nkind = \"x\"\n", "simple"),
        ("[[obstacles]]\npolygon = [[0, 0], [1, 0], [1, 1]]\nheight_mm = 0.0\nkind = \"clamp\"\n", "height_mm"),
        ("[[obstacles]]\npolygon = [[0, 0], [1, 0, 3], [1, 1]]\nheight_mm = 1.0\nkind = \"clamp\"\n", "point"),
        ("[[fixations]]\npoint = [1.0]\nheight_mm = 3.0\n", "fixations\\[0\\].point"),
        ("[[fixations]]\npoint = [1.0, 2.0]\n", "fixations\\[0\\].height_mm"),
        ("[[prior_removal]]\npolygon = [[0, 0], [1, 0], [1, 1]]\n", "prior_removal\\[0\\].depth_mm"),
        ("[[prior_removal]]\npolygon = [[0, 0], [1, 0], [1, 1]]\ndepth_mm = -2.0\n", "depth_mm"),
        ("[[prior_removal]]\npolygon = [[0, 0], [1, 0], [1, 0.0001]]\ndepth_mm = 2.0\n", "0.001"),
    ],
)
def test_bad_obstacles_fixations_prior_removal_refuse(tmp_path: Path, entry: str, match: str) -> None:
    with pytest.raises(defaults.InputFileError, match=match):
        load(tmp_path, MINIMAL + "\n" + entry)


# ---------------------------------------------------------------------------
# Tower 2
# ---------------------------------------------------------------------------


def test_tower2_maps_to_set_t2_position(tmp_path: Path) -> None:
    from goliath_link.protocol import Commands

    along_x = load(tmp_path, MINIMAL.replace('tower2 = "Y"', 'tower2 = "X"'))
    along_y = load(tmp_path, MINIMAL)
    assert isinstance(along_x.panel, Panel)
    assert along_x.panel.set_t2_position == 1 and along_x.panel.along_x is True
    assert along_y.panel.set_t2_position == 0 and along_y.panel.along_x is False
    for job in (along_x, along_y):
        assert Commands.set_t2_position(job.panel.along_x) == "SET T2 POSITION;%d" % job.panel.set_t2_position
        assert job.panel.set_t2_position == defaults.SET_T2_POSITION[job.panel.tower2.value]


# ---------------------------------------------------------------------------
# The conformance manifest's job objects
# ---------------------------------------------------------------------------


def manifest_jobs() -> list[dict]:
    entries = json.loads(MANIFEST.read_text(encoding="utf-8"))
    unique: dict[str, dict] = {}
    for entry in entries:
        unique.setdefault(json.dumps(entry["job"], sort_keys=True), entry["job"])
    return list(unique.values())


def as_toml(job: dict) -> str:
    """The manifest's job object written as job.toml, key for key."""
    def number(v: object) -> str:
        return repr(v)

    lines = ['z_zero = "%s"' % job["z_zero"], "", "[panel]"]
    for key in ("W", "H", "T"):
        lines.append("%s = %s" % (key, number(job["panel"][key])))
    lines += ['tower2 = "%s"' % job["panel"]["tower2"], "", "[placement]"]
    lines += ["dx = %s" % number(job["placement"]["dx"]), "dy = %s" % number(job["placement"]["dy"]), "", "[tool]"]
    for key, value in job["tool"].items():
        lines.append('%s = "%s"' % (key, value) if isinstance(value, str) else "%s = %s" % (key, number(value)))
    return "\n".join(lines) + "\n"


def test_manifest_job_objects_load_and_equal_their_job_toml(tmp_path: Path) -> None:
    jobs = manifest_jobs()
    assert len(jobs) >= 4
    for job in jobs:
        spec = JobSpec.from_mapping(job)
        assert spec.panel.W.value == job["panel"]["W"] and spec.panel.T.value == job["panel"]["T"]
        assert spec.panel.tower2.value == job["panel"]["tower2"]
        assert (spec.placement.dx.value, spec.placement.dy.value) == (job["placement"]["dx"], job["placement"]["dy"])
        assert spec.z_zero.value == job["z_zero"] and spec.z_zero.source == "OWNER_JOB"
        assert spec.tool.d.value == job["tool"]["d"] and spec.tool.flute.value == job["tool"]["flute"]
        for key in ("stickout", "rpm", "kind"):
            value = getattr(spec.tool, key)
            assert (value is None) == (key not in job["tool"]), key
            if value is not None:
                assert value.value == job["tool"][key], key
        assert spec.units is None  # a GI file's UNITS line states them
        from_toml = JobSpec.load(write(tmp_path, as_toml(job)))
        assert from_toml.to_json() == spec.to_json()
        assert from_toml == spec


def test_manifest_job_schema_names_are_job_toml_names() -> None:
    schema = json.loads((MANIFEST.parent / "manifest.schema.json").read_text(encoding="utf-8"))
    job = schema["$defs"]["job"]
    assert set(job["properties"]) == {"panel", "placement", "z_zero", "tool"}
    assert set(job["properties"]["panel"]["properties"]) == {"W", "H", "T", "tower2"}
    assert set(job["properties"]["placement"]["properties"]) == {"dx", "dy"}
    assert set(job["properties"]["tool"]["properties"]) == {"d", "flute", "stickout", "rpm", "kind"}
    from goliath_prep import jobspec

    assert jobspec.PANEL_KEYS == ("W", "H", "T", "tower2")
    assert jobspec.PLACEMENT_KEYS == ("dx", "dy")
    assert set(job["properties"]["tool"]["properties"]) <= set(jobspec.TOOL_KEYS)
    assert tuple(job["properties"]["tool"]["properties"]["kind"]["enum"]) == jobspec.TOOL_KINDS
    assert tuple(job["properties"]["z_zero"]["enum"]) == jobspec.Z_ZEROS


def test_from_mapping_refuses_like_load() -> None:
    job = manifest_jobs()[0]
    broken = json.loads(json.dumps(job))
    del broken["panel"]["T"]
    with pytest.raises(defaults.MissingValueError):
        JobSpec.from_mapping(broken)
    broken = json.loads(json.dumps(job))
    broken["tool"]["diameter"] = 6.0
    with pytest.raises(defaults.UnknownKeyError):
        JobSpec.from_mapping(broken)
    broken = json.loads(json.dumps(job))
    broken["panel"]["T"] = 0.1 + 0.2  # 0.30000000000000004: not a value anyone wrote
    with pytest.raises(defaults.InputFileError, match="0.001"):
        JobSpec.from_mapping(broken)


def test_job_file_reading_is_byte_stable(tmp_path: Path) -> None:
    lf = JobSpec.load(write(tmp_path, FULL, "a.toml"))
    crlf = JobSpec.load(write(tmp_path, FULL, "b.toml", newline="\r\n"))
    bom = tmp_path / "c.toml"
    bom.write_bytes(b"\xef\xbb\xbf" + FULL.encode("utf-8"))
    assert crlf == lf and JobSpec.load(bom) == lf
    bad = tmp_path / "d.toml"
    bad.write_bytes(MINIMAL.encode("utf-8") + b"# \xff\n")
    with pytest.raises(defaults.InputFileError, match="UTF-8"):
        JobSpec.load(bad)
    with pytest.raises(defaults.InputFileError, match="TOML"):
        JobSpec.load(write(tmp_path, "[panel\n", "e.toml"))
    with pytest.raises(defaults.InputFileError, match="cannot read"):
        JobSpec.load(tmp_path / "missing.toml")


def test_job_to_json_is_canonical(tmp_path: Path) -> None:
    from goliath_prep.core.canon import canonical_json

    job = load(tmp_path, FULL)
    data = job.to_json()
    canonical_json(data)
    assert data["panel"]["T"]["value"] == 17.6
    assert data["obstacles"][0]["ref"] == "job.toml obstacles[0]"


def test_field_guide_is_not_a_job_input() -> None:
    # job.toml holds the job; nothing of the robot's measurements belongs in it (they live in machine.toml)
    fields = json.loads(FIELD_GUIDE_04.read_text(encoding="utf-8"))["fields"]
    from goliath_prep import jobspec

    tops = {f["id"].split(".")[0] for f in fields}
    assert not tops & set(jobspec.TOP_LEVEL_KEYS)
