# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Every value the profile files and the defaults give has a source; unknown keys are refused (unit P01).

The owner's rule: a value works with real data, or falls back visibly, or
fails with a clear error; it never degrades silently. Here: every value
goliath_prep.defaults, MachineProfile and JobSpec hand out is a Param with a
source and a reference; every fallback (an unmeasured machine value, an absent
job value) carries a disclosure; and a key nobody implements is refused in
either file, so a declared obstacle or margin can never be ignored.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path

import pytest

from goliath_prep import defaults
from goliath_prep.core.disclosure import SEVERITY_RANK, DisclosureRegistry
from goliath_prep.core.params import DISCLOSED_SOURCES, SOURCES, Param
from goliath_prep.jobspec import JobSpec
from goliath_prep.machine import MachineProfile

MACHINE_MINIMAL = 'profile_version = 1\nserial = "TEST-SERIAL-0001"\n'
MACHINE_MEASURED = MACHINE_MINIMAL + """
[footprint]
q1_mm = 402.0
q1_deg = 0.0
q2_mm = 211.0
q2_deg = "unknown"

[wheel.q1]
contact_radius_mm = 36.0

[wheel.q2]
contact_radius_mm = 38.0

[wheel.q3]
contact_radius_mm = 37.0

[wire]
attachment = "offset"
same_point = "yes"
back_mm = 120.0
right_mm = 0.0

[policy]
m_tower = 30.0
"""
JOB_MINIMAL = '[panel]\nW = 2440\nH = 1220\nT = 18\ntower2 = "Y"\n\n[tool]\nd = 6\nflute = 22\n'
JOB_FULL = 'units = "mm"\nz_zero = "bottom"\n' + JOB_MINIMAL + """stickout = 40
rpm = 18000
kind = "ball"

[placement]
dx = 10.0
dy = 20.0

[supports]
left = 100.0
right = 0.0
bottom = 50.0
top = 0.0

[feeds]
feed_cap = 1800.0

[policy]
overcut_max = 0.2

[[obstacles]]
polygon = [[1, 1], [2, 1], [2, 2]]
height_mm = 3.0
kind = "clamp"
"""


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


def all_params(params: object) -> list[Param]:
    out: list[Param] = []
    stack = list(params.values()) if isinstance(params, Mapping) else list(params)  # type: ignore[arg-type]
    while stack:
        param = stack.pop()
        out.append(param)
        stack.extend(param.inputs)
    return out


def assert_sourced(param: Param, where: str) -> None:
    assert isinstance(param, Param), where
    assert param.source in SOURCES, where
    assert param.ref.strip(), where
    needed = DISCLOSED_SOURCES.get(param.source)
    if needed is not None:
        assert any(d.rank >= SEVERITY_RANK[needed] for d in param.disclosures), where
    param.to_json()  # exact in canonical JSON: a report shows the value that was used


def job_field_params(job: JobSpec) -> list[tuple[str, Param]]:
    """Every Param held in a JobSpec field, found by walking its dataclasses (not by its own params())."""
    found: list[tuple[str, Param]] = []

    def walk(obj: object, where: str) -> None:
        if isinstance(obj, Param):
            found.append((where, obj))
        elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            for f in dataclasses.fields(obj):
                walk(getattr(obj, f.name), "%s.%s" % (where, f.name))
        elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
            raise AssertionError("%s is a bare number %r without a source" % (where, obj))

    walk(job, "job")
    return found


def test_every_value_has_source(tmp_path: Path) -> None:
    for name, param in defaults.SPEC_PARAMS.items():
        assert_sourced(param, "SPEC_PARAMS[%s]" % name)
        assert param.ref == "docs/prep/params.json %s" % name

    for text in (MACHINE_MINIMAL, MACHINE_MEASURED):
        profile = MachineProfile.load(write(tmp_path, "machine.toml", text))
        for param in all_params(profile.params):
            assert_sourced(param, param.ref)
        # a value of the profile that is not measured is the catalogue's default, disclosed amber
        for name, param in profile.params.items():
            if param.source == "DEFAULT_UNMEASURED":
                assert any(d.severity == "amber" for d in param.disclosures), name
        # the profile's disclosures are exactly what its values carry, so showing them shows every fallback
        registry = DisclosureRegistry()
        for d in profile.disclosures():
            registry.raise_(d)
        registry.require_disclosed(profile.params.values())

    for text in (JOB_MINIMAL, JOB_FULL):
        job = JobSpec.load(write(tmp_path, "job.toml", text))
        held = job_field_params(job)
        assert held
        for where, param in held:
            assert_sourced(param, where)
        params = job.params()
        assert {id(p) for _, p in held} == {id(p) for p in params.values()}, "params() lists every job value once"
        for param in params.values():
            if "absent" in param.ref:
                assert param.disclosures, param.ref  # a value nobody wrote is a disclosed default
        registry = DisclosureRegistry()
        for d in job.disclosures():
            registry.raise_(d)
        registry.require_disclosed(params.values())


@pytest.mark.parametrize(
    ("name", "text", "key"),
    [
        ("machine.toml", MACHINE_MINIMAL + "wheel_radius = 38.0\n", "wheel_radius"),
        ("machine.toml", MACHINE_MEASURED + "\n[wheel.q3.extra]\nx = 1\n", "wheel.q3.extra"),
        ("machine.toml", MACHINE_MEASURED.replace("m_tower = 30.0", "m_tower = 30.0\nm_edge_tol = 1.0"),
         "policy.m_edge_tol"),
        ("job.toml", "obstacle = [[1, 1], [2, 1], [2, 2]]\n" + JOB_MINIMAL, "obstacle"),
        ("job.toml", JOB_FULL.replace('kind = "clamp"', 'kind = "clamp"\nheight = 3.0'), "obstacles[0].height"),
        ("job.toml", JOB_FULL.replace("feed_cap = 1800.0", "feed_cap = 1800.0\nplunge = 300.0"), "feeds.plunge"),
    ],
)
def test_unknown_keys_refused(tmp_path: Path, name: str, text: str, key: str) -> None:
    path = write(tmp_path, name, text)
    with pytest.raises(defaults.UnknownKeyError) as info:
        (MachineProfile if name == "machine.toml" else JobSpec).load(path)
    assert info.value.key == key
    message = str(info.value)
    assert key in message and name in message and str(path) in message
