# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The geometry kernel of goliath-prep (unit P04; the project design, sections 4.6, 4.8 and 4.10).

* :mod:`~goliath_prep.geom.footprint` - heading theta = (C + 90) mod 360, the
  supports of spec 08's footprint and of a measured one, the union model, and
  the rotation radius R_rot (404 mm with the spec values);
* :mod:`~goliath_prep.geom.sweeps` - discs, capsules, annuli, rotation arc
  sweeps, the convex envelope of a line that turns and moves together, the
  rotation disc and any-rotation envelope, wire reach and the body hull;
* :mod:`~goliath_prep.geom.zones` - tower keep-out zones at both tower-2
  placements, tower bases, anchor points, and spec 08's exact triangle test for
  the vendor-floor module;
* :mod:`~goliath_prep.geom.workarea` - the panel at the desk and at job time,
  per-edge margins (the tower-baseline edge keeps its own) and support strips.

Millimetres and degrees; the panel frame has tower 1 at the origin, angles
counter-clockwise from +X. Points are ``(x, y)`` tuples, regions shapely
polygons. Every polygon errs on the safe side by at most
:data:`~goliath_prep.geom.sweeps.APPROX_TOL_MM`: the robot's shapes and the
keep-outs hold the exact set, the work areas and the reach region lie inside
it. The kernel computes geometry only: it reads no file, decides no check and
defaults no safety value (margins, rho and the footprint are always passed in).
"""
from __future__ import annotations

from goliath_prep.geom.footprint import (
    C_GRID,
    Footprint,
    FootprintRadii,
    SupportPosition,
    Vec2,
    body_point,
    c_word,
    centroid_offsets,
    footprint_models,
    footprint_radii,
    rotation_radius,
    support_offset,
    support_offsets,
    support_positions,
    supports,
    theta,
    unit,
)
from goliath_prep.geom.sweeps import (
    APPROX_TOL_MM,
    annulus,
    any_rotation_envelope,
    arc_hull_points,
    arc_sweep,
    body_hull,
    body_hull_local,
    body_radius,
    capsule,
    convex_envelope,
    disc,
    grown_hull,
    reach_region,
    rotation_disc,
    support_arc_hull,
    wheel_capsules,
    wheel_discs,
)
from goliath_prep.geom.workarea import (
    SIDES,
    baseline_side,
    desk_panel,
    edge_margins,
    job_panel,
    support_strips,
    work_area_tool,
    work_area_wheel,
)
from goliath_prep.geom.zones import (
    TOWER_ZONE_LEG_TOOL_MM,
    TOWER_ZONE_LEG_WHEEL_MM,
    anchor_points,
    tool_zones,
    tower2_placements,
    tower_bases,
    tower_points,
    vendor_triangle_contains,
    wheel_zones,
    zone_triangles,
)

__all__ = [
    "APPROX_TOL_MM",
    "C_GRID",
    "SIDES",
    "TOWER_ZONE_LEG_TOOL_MM",
    "TOWER_ZONE_LEG_WHEEL_MM",
    "Footprint",
    "FootprintRadii",
    "SupportPosition",
    "Vec2",
    "anchor_points",
    "annulus",
    "any_rotation_envelope",
    "arc_hull_points",
    "arc_sweep",
    "baseline_side",
    "body_hull",
    "body_hull_local",
    "body_point",
    "body_radius",
    "c_word",
    "capsule",
    "centroid_offsets",
    "convex_envelope",
    "desk_panel",
    "disc",
    "edge_margins",
    "footprint_models",
    "footprint_radii",
    "grown_hull",
    "job_panel",
    "reach_region",
    "rotation_disc",
    "rotation_radius",
    "support_arc_hull",
    "support_offset",
    "support_offsets",
    "support_positions",
    "support_strips",
    "supports",
    "theta",
    "tool_zones",
    "tower2_placements",
    "tower_bases",
    "tower_points",
    "unit",
    "vendor_triangle_contains",
    "wheel_capsules",
    "wheel_discs",
    "wheel_zones",
    "work_area_tool",
    "work_area_wheel",
    "zone_triangles",
]
