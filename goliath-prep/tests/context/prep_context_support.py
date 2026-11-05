# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Test data for the context builder's tests (unit P22): jobs, machine profiles, gate views, measured panels.

All values are test data. The serial is an obviously fake one, and no gate
view here comes from a robot. :class:`Gates` imitates goliath-run's gate
views (goliath_run.capabilities: ``SnapshotGateView`` and the live view):
``allowed`` answers True, False or None per name, and ``value`` what an open
gate carries, in goliath-run's forms (the rotation model as text, the tested
edges as a tuple of ``x_min`` ... names, the park pose as ``{x_mm, y_mm,
heading_deg, tolerance_mm, tolerance_deg}``, the wire attachment as ``{kind,
back_mm, right_mm}``, the kerf values as ``{w_b, L_ride, h_r}``). Unlike
goliath-run's views it can also answer a value for a closed gate, so the tests
can show that such a value is never used.
"""
from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from goliath_prep import machine
from goliath_prep.core.types import MeasuredGeometry, Pose
from goliath_prep.jobspec import JobSpec
from goliath_prep.machine import MachineProfile

SERIAL = "TEST-SERIAL-0001"
FACTS = "ab" * 32

JOB: Mapping[str, Any] = {
    "panel": {"W": 2440, "H": 1220, "T": Decimal("17.6"), "tower2": "Y"},
    "tool": {"d": 6, "flute": 22},
}

MINIMAL_PROFILE = """\
profile_version = 1
serial = "TEST-SERIAL-0001"
"""

MEASURED_PROFILE = """\
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
"""


def job(**panel: Any) -> JobSpec:
    """The test job (a 2440 x 1220 mm sheet, tower 2 on Y), with ``panel`` values replaced."""
    document = {key: dict(value) for key, value in JOB.items()}
    document["panel"].update(panel)
    return JobSpec.from_mapping(document)


def job_from(document: Mapping[str, Any]) -> JobSpec:
    return JobSpec.from_mapping(document)


def stamped(text: str, facts: str = FACTS) -> str:
    """``text`` with the export stamp goliath-run writes."""
    digest = machine.export_content_sha256(tomllib.loads(text, parse_float=Decimal))
    return text + '\n[export]\nby = "goliath-run 0.0.1"\nfacts_sha256 = "%s"\ncontent_sha256 = "%s"\n' % (
        facts, digest)


def profile(text: str = MINIMAL_PROFILE, *, policy: str = "") -> MachineProfile:
    """A machine profile from TOML text; ``policy`` lines go into a [policy] table (outside the stamp)."""
    if policy:
        text = text + "\n[policy]\n" + policy
    return MachineProfile.from_mapping(tomllib.loads(text, parse_float=Decimal))


def exported(text: str = MEASURED_PROFILE, *, policy: str = "") -> MachineProfile:
    return profile(stamped(text), policy=policy)


def measured(*, tower2: str = "Y", info27: float | None = 1221.5, park: Pose | None = None,
             polygon: str = "POLYGON ((0 0, 2440 0, 2440 1220, 0 1220, 0 0))",
             size: tuple[int, int] = (2440, 1220)) -> MeasuredGeometry:
    return MeasuredGeometry.build(
        raw_info25=("INFO;25;0.0000;0.0000", "INFO;25;2440.0000;0.0000", "INFO;25;2440.0000;1220.0000",
                    "INFO;25;0.0000;1220.0000"),
        raw_info27=None if info27 is None else "INFO;27;%.4f" % info27,
        hypothesis="xy/identity",
        polygon_wkt=polygon,
        set_panel_size=size,
        tower2=tower2,  # type: ignore[arg-type]
        tower_distance=info27,
        park_pose=park,
    )


@dataclass(frozen=True)
class Gates:
    """A GateView over fixed tables (see the module documentation). A name not in ``allowed`` is closed."""

    allowed_map: tuple[tuple[str, object], ...] = ()
    value_map: tuple[tuple[str, object], ...] = ()

    def allowed(self, name: str) -> bool | None:
        return dict(self.allowed_map).get(name, False)  # type: ignore[return-value]

    def value(self, name: str) -> object:
        return dict(self.value_map).get(name)

    def snapshot_sha256(self) -> str:
        return hashlib.sha256(repr((self.allowed_map, self.value_map)).encode("utf-8")).hexdigest()


def gates(**spec: Any) -> Gates:
    """``gates(heading_map_verified=True, rotation_model=(True, "in-place-shortest"), rotate_in_place_ok=None)``.

    A bare value is the gate's ``allowed``; a pair is ``(allowed, value)``.
    """
    allowed: list[tuple[str, object]] = []
    values: list[tuple[str, object]] = []
    for name in sorted(spec):
        entry = spec[name]
        if isinstance(entry, tuple):
            allowed.append((name, entry[0]))
            values.append((name, entry[1]))
        else:
            allowed.append((name, entry))
    return Gates(tuple(allowed), tuple(values))


PARK = {"x_mm": 1220.0, "y_mm": 610.0, "heading_deg": 90.0, "tolerance_mm": 10.0, "tolerance_deg": 2.0}
"""A park pose in goliath-run's form: at the panel centre, the body heading 90 deg (C 0)."""

R3_FACTS: Mapping[str, Any] = {
    "heading_map_verified": True,
    "rotation_model": (True, "in-place-shortest"),
    "rotate_in_place_ok": True,
    "start_pose_known": (True, PARK),
}
"""The gates rung R3 opens on a robot that turns in place, with the R2 park pose."""
