# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The shared prep interface types (unit F05; red-team C-06).

Frozen value types with validation and a JSON round trip, and nothing else:
Pose, RotationModel, FootprintSet, EmitProfile, the GateView protocol and the
all-closed desk view, MeasuredGeometry, Envelope, SessionBinding, CheckContext
and RungExemption.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import pickle
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, get_args

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from goliath_prep.core import types as types_module
from goliath_prep.core.canon import canonical_json, canonical_sha256
from goliath_prep.core.disclosure import Disclosure
from goliath_prep.core.params import SOURCES, Param
from goliath_prep.core.types import (
    CONTEXT_SCALAR_PARAMS,
    EMIT_FIRST_LINES,
    EMIT_ROTATION_STYLES,
    EMIT_Z_LINE_STYLES,
    EXEMPT_RULES,
    EXEMPTION_RUNGS,
    MEASURED_SUPPORT_PARAMS,
    ROTATION_MODELS,
    VENDOR_SUPPORT_PARAMS,
    CheckContext,
    ClosedGateView,
    EmitProfile,
    Envelope,
    ExemptLine,
    Fixation,
    FootprintSet,
    FrozenMapping,
    GateView,
    MeasuredGeometry,
    Obstacle,
    Pose,
    PriorCut,
    RotationModel,
    RungExemption,
    RungExemptionOriginError,
    SessionBinding,
)

REPO = Path(__file__).resolve().parents[3]
SAFETY_MODEL = REPO / "docs" / "prep" / "safety-model.md"
SCANNED_SOURCES = (REPO / "goliath-prep" / "src" / "goliath_prep", REPO / "goliath-run" / "src" / "goliath_run")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")

SPEC_FOOTPRINT = ((402.0, 0.0), (211.0, 76.0), (211.0, -76.0))
SHA_A = "a" * 64
SHA_B = "0123456789abcdef" * 4


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def unmeasured(name: str) -> Disclosure:
    return Disclosure(
        id="DEFAULT_UNMEASURED:%s" % name,
        severity="amber",
        text="%s is not measured: conservative default in use" % name,
        source="docs/prep/params.json %s" % name,
    )


def spec(name: str, value: float, unit: str) -> Param[Any]:
    return Param(value, unit, "SPEC", "docs/prep/params.json %s" % name)


def r_w() -> Param[Any]:
    return Param(40.0, "mm", "DEFAULT_UNMEASURED", "docs/prep/params.json r_w", disclosures=(unmeasured("r_w"),))


def m_w() -> Param[Any]:
    return Param(15.0, "mm", "POLICY", "docs/prep/params.json m_w")


def gate_param(name: str, value: str, source: str = "GATE") -> Param[Any]:
    return Param(value, "enum", source, "gate %s" % name)  # type: ignore[arg-type]


def context_params(
    footprint: FootprintSet,
    *,
    rho: float = 55.0,
    r_rot: float = 404.0,
    rotation_model: str = "unverified",
    heading_map_verified: bool = False,
    gate_source: str = "GATE",
) -> dict[str, Param[Any]]:
    """Every Param a CheckContext with these fields must carry, with honest sources."""
    params: dict[str, Param[Any]] = {}
    for k, (r, delta) in enumerate(footprint.vendor):
        r_name, delta_name = VENDOR_SUPPORT_PARAMS[k]
        params[r_name] = spec(r_name, r, "mm")
        params[delta_name] = spec(delta_name, delta, "deg")
    if footprint.measured is not None:
        for k, position in enumerate(footprint.measured):
            if position is None:
                continue
            r_name, delta_name = MEASURED_SUPPORT_PARAMS[k]
            params[r_name] = Param(position[0], "mm", "ROBOT_MEASURED", "machine.toml footprint.q%d_mm" % (k + 1))
            params[delta_name] = Param(position[1], "deg", "ROBOT_MEASURED", "machine.toml footprint.q%d_deg" % (k + 1))
    params["rho"] = Param.derived(rho, "mm", "rho = r_w + m_w (safety-model 4.4)", (r_w(), m_w()))
    supports = tuple(params[name] for pair in VENDOR_SUPPORT_PARAMS for name in pair)
    params["r_rot"] = Param.derived(r_rot, "mm", "R_rot (safety-model 4.6)", supports)
    model_source = gate_source if rotation_model != "unverified" else "GATE"
    params["rotation_model"] = gate_param("rotation_model", rotation_model, model_source)
    heading_source = gate_source if heading_map_verified else "GATE"
    params["heading_map_verified"] = gate_param(
        "heading_map_verified", "verified" if heading_map_verified else "unverified", heading_source
    )
    return params


@dataclasses.dataclass(frozen=True)
class FakeGateView:
    """A GateView over a fixed table (test double; goliath-run's real view arrives with unit R01)."""

    allowed_table: tuple[tuple[str, bool | None], ...] = ()
    value_table: tuple[tuple[str, str], ...] = ()

    def allowed(self, name: str) -> bool | None:
        return dict(self.allowed_table).get(name)

    def value(self, name: str) -> object:
        return dict(self.value_table).get(name)

    def snapshot_sha256(self) -> str:
        return canonical_sha256({"allowed": dict(self.allowed_table), "values": dict(self.value_table)})


OPEN_GATES = FakeGateView(
    allowed_table=(("heading_map_verified", True), ("rotation_model", True)),
    value_table=(("rotation_model", "in-place-shortest"),),
)


def make_pose(**kw: Any) -> Pose:
    fields: dict[str, Any] = {"x": 412.5, "y": 300.0, "c": 90, "source": "detection.park_pose (K3)"}
    fields.update(kw)
    return Pose(**fields)


def make_measured(**kw: Any) -> MeasuredGeometry:
    fields: dict[str, Any] = {
        "raw_info25": ("INFO;25;0.0000;0.0000", "INFO;25;1220.0000;0.0000", "INFO;25;1220.0000;2440.0000",
                       "INFO;25;0.0000;2440.0000"),
        "raw_info27": "INFO;27;2440.0000",
        "hypothesis": "xy/identity",
        "polygon_wkt": "POLYGON ((0 0, 1220 0, 1220 2440, 0 2440, 0 0))",
        "set_panel_size": (1220, 2440),
        "tower2": "Y",
        "tower_distance": 2440.0,
        "park_pose": make_pose(),
    }
    fields.update(kw)
    return MeasuredGeometry.build(**fields)


def make_footprint(measured: Any = None) -> FootprintSet:
    return FootprintSet(SPEC_FOOTPRINT, measured, "vendor" if measured is None else "union")


def make_context(**kw: Any) -> CheckContext:
    footprint = kw.pop("footprint", make_footprint())
    fields: dict[str, Any] = {
        "footprint": footprint,
        "rho": 55.0,
        "r_rot": 404.0,
        "gates": ClosedGateView(),
        "rotation_model": "unverified",
        "heading_map_verified": False,
        "start_pose": None,
        "measured": None,
        "obstacles": (),
        "fixations": (),
        "prior_removal": (),
    }
    fields.update(kw)
    if "params" not in fields:
        fields["params"] = context_params(
            footprint,
            rho=fields["rho"],
            r_rot=fields["r_rot"],
            rotation_model=fields["rotation_model"],
            heading_map_verified=fields["heading_map_verified"],
            gate_source="FACT",
        )
    return CheckContext(**fields)


def full_context() -> CheckContext:
    """A context with every optional part present and the permissive gate facts recorded."""
    footprint = make_footprint(((405.5, 0.5), None, (209.0, -75.5)))
    return make_context(
        footprint=footprint,
        r_rot=406.0,
        gates=OPEN_GATES,
        rotation_model="in-place-shortest",
        heading_map_verified=True,
        start_pose=make_pose(),
        measured=make_measured(),
        obstacles=(Obstacle(((100.0, 100.0), (150.0, 100.0), (150.0, 140.0)), 12.0, "clamp", "job.toml obstacles[0]"),),
        fixations=(Fixation((600.0, 800.0), 4.5, "job.toml fixations[0]"),),
        prior_removal=(PriorCut(((10.0, 10.0), (20.0, 10.0), (20.0, 20.0), (10.0, 20.0)), 3.0,
                                "job.toml prior_removal[0]"),),
    )


# A stand-in for goliath_prep.rungs (unit P18, not written yet): a namespace whose
# module name is the one module allowed to build a RungExemption. It is not put in
# sys.modules, so it cannot be mistaken for the real module.
_RUNGS_STAND_IN: dict[str, Any] = {"__name__": "goliath_prep.rungs"}
exec(  # noqa: S102 - a fixed literal, compiled under a known name
    compile(
        "import copy, dataclasses\n"
        "from goliath_prep.core.types import ExemptLine, RungExemption\n"
        "def build(rung, payload_sha256, lines):\n"
        "    return RungExemption(rung, payload_sha256, lines)\n"
        "def replace(exemption, **changes):\n"
        "    return dataclasses.replace(exemption, **changes)\n",
        "<stand-in for goliath_prep.rungs>",
        "exec",
    ),
    _RUNGS_STAND_IN,
)


def make_exemption(**kw: Any) -> RungExemption:
    fields: dict[str, Any] = {
        "rung": "R3",
        "payload_sha256": SHA_A,
        "lines": (ExemptLine(14, "D-CSTEP:step-180", "the deliberate single wrap step (350 -> 10)"),
                  ExemptLine(2, "D-CSTEP:xy-and-c", "the combined rotate-and-translate line")),
    }
    fields.update(kw)
    return _RUNGS_STAND_IN["build"](**fields)


def safety_model_gate_names() -> list[str]:
    """The gate names of the gate table in docs/prep/safety-model.md section 1.4."""
    text = SAFETY_MODEL.read_text(encoding="utf-8")
    start = text.index("**Gates.**")
    end = text.index("**Rungs.**", start)
    names = re.findall(r"^\| `([a-z0-9_]+)` \|", text[start:end], flags=re.MULTILINE)
    assert len(names) >= 20 and "heading_map_verified" in names and "rotation_model" in names, names
    return names


# ---------------------------------------------------------------------------
# tests_first: frozen and hashable
# ---------------------------------------------------------------------------


def every_instance() -> list[Any]:
    return [
        make_pose(),
        make_pose(c=None),
        make_footprint(),
        make_footprint(((405.5, 0.5), None, None)),
        EmitProfile("bare", "split", "combined"),
        ClosedGateView(),
        make_measured(),
        Envelope({"x_max": 1210.0, "y_max": 2430.0}, ("the measured panel must contain the desk panel",)),
        SessionBinding("session-0001", "TEST-SERIAL-0001", "01.02.00", SHA_A, SHA_B, SHA_A, SHA_B,
                       "2026-11-04T10:15:00Z"),
        Obstacle(((0.0, 0.0), (10.0, 0.0), (10.0, 10.0)), 5.0, "screw head", "job.toml obstacles[0]"),
        Fixation((5.0, 5.0), 3.0, "job.toml fixations[0]"),
        PriorCut(((0.0, 0.0), (10.0, 0.0), (10.0, 10.0)), 2.0, "job.toml prior_removal[0]"),
        ExemptLine(3, "D-CSTEP:step-180", "wrap"),
        make_exemption(),
        make_context(),
        full_context(),
    ]


def test_types_frozen_and_hashable() -> None:
    instances = every_instance()
    for obj in instances:
        assert dataclasses.is_dataclass(obj), obj
        hash(obj)
        assert obj == obj
        twin = copy.copy(obj)
        assert twin == obj and hash(twin) == hash(obj)
        first = dataclasses.fields(obj)[0].name if dataclasses.fields(obj) else None
        if first is not None:
            with pytest.raises(dataclasses.FrozenInstanceError):
                setattr(obj, first, None)
    # Every dataclass the module defines is frozen, and each of them is covered above.
    defined = {
        obj
        for obj in vars(types_module).values()
        if isinstance(obj, type) and dataclasses.is_dataclass(obj) and obj.__module__ == types_module.__name__
    }
    assert defined, "no dataclass found in goliath_prep.core.types"
    for cls in defined:
        assert cls.__dataclass_params__.frozen, cls  # type: ignore[attr-defined]
    assert defined == {type(obj) for obj in instances}


def test_containers_are_copied_into_immutable_ones() -> None:
    raw = ["INFO;25;0.0000;0.0000", "INFO;25;10.0000;0.0000", "INFO;25;10.0000;10.0000"]
    measured = make_measured(raw_info25=raw)
    raw.append("INFO;25;99.0000;99.0000")
    assert len(measured.raw_info25) == 3 and isinstance(measured.raw_info25, tuple)

    limits = {"x_max": 10.0}
    envelope = Envelope(limits, ["m"])
    limits["x_max"] = 9999.0
    assert envelope.limits["x_max"] == 10.0 and envelope.messages == ("m",)
    with pytest.raises(TypeError):
        envelope.limits["x_max"] = 1.0  # type: ignore[index]

    params = context_params(make_footprint())
    context = make_context(params=params)
    params["rho"] = Param(1.0, "mm", "POLICY", "x")
    assert context.params["rho"].value == 55.0
    assert isinstance(context.params, FrozenMapping)
    with pytest.raises(TypeError):
        context.params["rho"] = Param(1.0, "mm", "POLICY", "x")  # type: ignore[index]
    with pytest.raises(AttributeError):
        context.params.clear()  # type: ignore[attr-defined]

    vendor = [[402, 0], [211, 76], [211, -76]]
    footprint = FootprintSet(vendor, None, "vendor")  # type: ignore[arg-type]
    vendor[0][0] = 1
    assert footprint.vendor == SPEC_FOOTPRINT
    assert all(isinstance(v, float) for pair in footprint.vendor for v in pair)


def test_frozen_mapping_is_a_hashable_read_only_mapping() -> None:
    a = FrozenMapping({"b": 2.0, "a": 1.0})
    b = FrozenMapping([("a", 1.0), ("b", 2.0)])
    assert a == b == {"a": 1.0, "b": 2.0} and hash(a) == hash(b)
    assert list(a) == ["a", "b"] and len(a) == 2 and isinstance(a, Mapping)
    assert a != FrozenMapping({"a": 1.0})
    with pytest.raises(TypeError):
        a["c"] = 3.0  # type: ignore[index]
    with pytest.raises(TypeError):
        FrozenMapping([("a", 1.0), ("a", 2.0)])  # a key given twice is an error, not a silent overwrite
    assert "FrozenMapping" in repr(a)
    # Copies and pickles rebuild it (a context holds one), and stay read-only.
    assert copy.deepcopy(a) == a and pickle.loads(pickle.dumps(a)) == a
    context = full_context()
    assert copy.deepcopy(context) == context
    desk = make_context()
    assert pickle.loads(pickle.dumps(desk)) == desk
    with pytest.raises(AttributeError):
        a._data = {}  # type: ignore[misc]


# ---------------------------------------------------------------------------
# tests_first: the all-closed desk view
# ---------------------------------------------------------------------------


def test_closed_gate_view_denies_everything() -> None:
    view = ClosedGateView()
    assert isinstance(view, GateView)
    for name in safety_model_gate_names() + ["jobs_allowed", "software_stop", "max_program_bytes", "no_such_gate"]:
        assert view.allowed(name) is False, name
        assert view.value(name) is None, name
    for bad in (None, 7, b"rotation_model"):
        with pytest.raises(TypeError):
            view.allowed(bad)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            view.value(bad)  # type: ignore[arg-type]
    for bad in ("", "  "):
        with pytest.raises(ValueError):
            view.allowed(bad)
    # One fixed, documented snapshot hash, which no gate snapshot of a robot shares.
    sha = view.snapshot_sha256()
    assert HEX64.match(sha) and sha == ClosedGateView().snapshot_sha256()
    assert sha == canonical_sha256(ClosedGateView.SNAPSHOT)
    assert ClosedGateView.SNAPSHOT["gates"] == "all closed" and ClosedGateView.SNAPSHOT["facts"] == "none"
    # The fallback is disclosed: the desk view carries the disclosure the CLI raises.
    disclosure = ClosedGateView.DISCLOSURE
    assert isinstance(disclosure, Disclosure)
    assert disclosure.id == "DESK_NO_ROBOT_FACTS" and disclosure.severity == "amber" and disclosure.sticky
    assert "DESK, NO ROBOT FACTS" in disclosure.text
    # A context cannot claim, under the desk view, what only a robot fact can open.
    with pytest.raises(ValueError, match="heading_map_verified"):
        make_context(heading_map_verified=True)
    for model in ROTATION_MODELS:
        if model == "unverified":
            continue
        with pytest.raises(ValueError, match="rotation_model"):
            make_context(rotation_model=model)
    assert make_context().gates == ClosedGateView()


@given(st.text(min_size=1, max_size=40).filter(lambda s: s.strip() != ""))
def test_closed_gate_view_denies_any_name(name: str) -> None:
    view = ClosedGateView()
    assert view.allowed(name) is False
    assert view.value(name) is None


def test_open_gates_are_needed_for_permissive_context_values() -> None:
    context = full_context()
    assert context.heading_map_verified and context.rotation_model == "in-place-shortest"
    # The gate view must say yes ("is True"), with the same model; None or a falsy
    # stand-in is never yes.
    for allowed in (None, False, 1, "yes"):
        view = FakeGateView(allowed_table=(("heading_map_verified", allowed),))  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="heading_map_verified"):
            make_context(gates=view, heading_map_verified=True)
    mismatched = FakeGateView(
        allowed_table=(("rotation_model", True),), value_table=(("rotation_model", "about-centroid"),)
    )
    with pytest.raises(ValueError, match="rotation_model"):
        make_context(gates=mismatched, rotation_model="in-place-shortest")
    # A permissive value needs a FACT source; a closed one may come from the gate or owner policy.
    footprint = make_footprint()
    gate_sourced = context_params(footprint, rotation_model="in-place-shortest", gate_source="GATE")
    with pytest.raises(ValueError, match="FACT"):
        make_context(gates=OPEN_GATES, rotation_model="in-place-shortest", params=gate_sourced)
    policy = context_params(footprint)
    policy["rotation_model"] = gate_param("rotation_model", "unverified", "POLICY")
    assert make_context(gates=OPEN_GATES, params=policy).rotation_model == "unverified"
    with pytest.raises(TypeError, match="GateView"):
        make_context(gates=object())
    with pytest.raises(TypeError, match="heading_map_verified"):
        make_context(heading_map_verified=0)


# ---------------------------------------------------------------------------
# tests_first: CheckContext JSON round trip
# ---------------------------------------------------------------------------

_GRID = st.integers(-3_000_000, 3_000_000)
_POSITIVE = st.integers(1, 3_000_000)
_TEXT = st.text(min_size=1, max_size=12).filter(lambda s: s.strip() != "")


def _mm(n: int) -> float:
    return n / 1000


@st.composite
def poses(draw: st.DrawFn) -> Pose:
    c = draw(st.one_of(st.none(), st.sampled_from(range(0, 360, 10))))
    return Pose(_mm(draw(_GRID)), _mm(draw(_GRID)), c, draw(_TEXT))


@st.composite
def rectangles(draw: st.DrawFn) -> tuple[tuple[float, float], ...]:
    x, y, w, h = draw(_GRID), draw(_GRID), draw(_POSITIVE), draw(_POSITIVE)
    return ((_mm(x), _mm(y)), (_mm(x + w), _mm(y)), (_mm(x + w), _mm(y + h)), (_mm(x), _mm(y + h)))


@st.composite
def footprints(draw: st.DrawFn) -> FootprintSet:
    support = st.tuples(_POSITIVE.map(_mm), st.integers(-180_000, 180_000).map(_mm))
    vendor = draw(st.one_of(st.just(SPEC_FOOTPRINT), st.tuples(support, support, support)))
    measured = draw(st.one_of(st.none(), st.tuples(*(st.one_of(st.none(), support) for _ in range(3)))))
    if measured is not None and all(m is None for m in measured):
        measured = None
    return FootprintSet(vendor, measured, "vendor" if measured is None else "union")


@st.composite
def measured_geometries(draw: st.DrawFn) -> MeasuredGeometry:
    lines = draw(st.lists(st.from_regex(r"INFO;25;[0-9.;-]{0,30}", fullmatch=True), min_size=1, max_size=6))
    distance = draw(st.one_of(st.none(), _POSITIVE.map(_mm)))
    raw27 = None if distance is None else "INFO;27;%.4f" % distance
    return MeasuredGeometry.build(
        raw_info25=tuple(lines),
        raw_info27=raw27,
        hypothesis=draw(st.sampled_from(["xy/identity", "xy/swap", "polar/rot180"])),
        polygon_wkt="POLYGON ((0 0, %d 0, %d %d, 0 %d, 0 0))" % (4, 4, 5, 5),
        set_panel_size=(draw(st.integers(1, 3500)), draw(st.integers(1, 3500))),
        tower2=draw(st.sampled_from(["X", "Y"])),
        tower_distance=distance,
        park_pose=draw(st.one_of(st.none(), poses())),
    )


@st.composite
def contexts(draw: st.DrawFn) -> CheckContext:
    footprint = draw(footprints())
    rho = _mm(draw(_POSITIVE))
    largest = max(r for position in footprint.vendor + (footprint.measured or ()) if position for r in position[:1])
    r_rot = _mm(int(round(largest * 1000)) + draw(st.integers(0, 500_000)))
    open_gates = draw(st.booleans())
    model: RotationModel = draw(st.sampled_from(ROTATION_MODELS)) if open_gates else "unverified"
    heading = draw(st.booleans()) if open_gates else False
    gates: Any = ClosedGateView()
    if open_gates:
        gates = FakeGateView(
            allowed_table=(("heading_map_verified", heading), ("rotation_model", model != "unverified")),
            value_table=(("rotation_model", model),),
        )
    obstacle = st.builds(Obstacle, rectangles(), _POSITIVE.map(_mm), _TEXT, _TEXT)
    fixation = st.builds(Fixation, st.tuples(_GRID.map(_mm), _GRID.map(_mm)), _POSITIVE.map(_mm), _TEXT)
    prior = st.builds(PriorCut, rectangles(), _POSITIVE.map(_mm), _TEXT)
    params = context_params(footprint, rho=rho, r_rot=r_rot, rotation_model=model, heading_map_verified=heading,
                            gate_source="FACT")
    extra = draw(st.dictionaries(st.from_regex(r"[a-z][a-z_]{0,10}", fullmatch=True), _POSITIVE, max_size=3))
    for name, n in extra.items():
        params.setdefault(name, Param(_mm(n), "mm", "POLICY", "docs/prep/params.json %s" % name))
    return CheckContext(
        params=params,
        footprint=footprint,
        rho=rho,
        r_rot=r_rot,
        gates=gates,
        rotation_model=model,
        heading_map_verified=heading,
        start_pose=draw(st.one_of(st.none(), poses())),
        measured=draw(st.one_of(st.none(), measured_geometries())),
        obstacles=tuple(draw(st.lists(obstacle, max_size=2))),
        fixations=tuple(draw(st.lists(fixation, max_size=2))),
        prior_removal=tuple(draw(st.lists(prior, max_size=2))),
    )


def _roundtrip(context: CheckContext) -> CheckContext:
    data = canonical_json(context)
    back = CheckContext.from_json(json.loads(data.decode("utf-8")), gates=context.gates)
    assert back == context
    assert canonical_json(back) == data
    return back


@settings(max_examples=60, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(contexts())
def test_check_context_json_roundtrip(context: CheckContext) -> None:
    back = _roundtrip(context)
    assert hash(back) == hash(context)


def test_check_context_json_roundtrip_examples() -> None:
    for context in (make_context(), full_context()):
        _roundtrip(context)
    data = json.loads(canonical_json(full_context()).decode("utf-8"))
    assert data["gates_sha256"] == OPEN_GATES.snapshot_sha256()
    assert data["measured"]["raw_info25"][1] == "INFO;25;1220.0000;0.0000"
    # Read back only against the gate snapshot it was built under.
    with pytest.raises(ValueError, match="gate snapshot"):
        CheckContext.from_json(data, gates=ClosedGateView())
    # Exactly the documented keys: an unknown key is refused, never ignored.
    for mutate in (lambda d: d.update(extra=1), lambda d: d.pop("fixations")):
        broken = json.loads(canonical_json(full_context()).decode("utf-8"))
        mutate(broken)
        with pytest.raises(ValueError, match="keys"):
            CheckContext.from_json(broken, gates=OPEN_GATES)


def _json_cases() -> list[tuple[Any, Any]]:
    return [
        (Pose, make_pose()),
        (FootprintSet, make_footprint(((405.5, 0.5), None, (209.0, -75.5)))),
        (EmitProfile, EmitProfile("g00", "combined", "translate-then-rotate")),
        (MeasuredGeometry, make_measured()),
        (Envelope, Envelope({"x_max": 1210.0}, ("why",))),
        (SessionBinding, SessionBinding("s", "TEST-SERIAL-0001", "01.02.00", SHA_A, SHA_A, SHA_A, SHA_A,
                                        "2026-11-04T10:15:00+00:00")),
        (Obstacle, Obstacle(((0.0, 0.0), (10.0, 0.0), (10.0, 10.0)), 5.0, "clamp", "job.toml obstacles[0]")),
        (Fixation, Fixation((5.0, 5.0), 3.0, "job.toml fixations[0]")),
        (PriorCut, PriorCut(((0.0, 0.0), (10.0, 0.0), (10.0, 10.0)), 2.0, "job.toml prior_removal[0]")),
        (ExemptLine, ExemptLine(3, "D-CSTEP:step-180", "wrap")),
    ]


@pytest.mark.parametrize("cls, obj", _json_cases(), ids=lambda v: v.__name__ if isinstance(v, type) else None)
def test_value_types_json_roundtrip_and_refuse_wrong_keys(cls: Any, obj: Any) -> None:
    data = json.loads(canonical_json(obj).decode("utf-8"))
    back = cls.from_json(data)
    assert back == obj and canonical_json(back) == canonical_json(obj)
    with pytest.raises(ValueError, match="keys"):
        cls.from_json({**data, "unexpected": 1})
    with pytest.raises(ValueError, match="keys"):
        cls.from_json({k: v for k, v in list(data.items())[1:]})
    with pytest.raises(ValueError, match="keys"):
        cls.from_json([data])


def test_off_grid_floats_are_refused_when_written() -> None:
    # Canonical JSON writes floats to 0.001; a value the rounding would change is
    # refused rather than shown as another value than the one in use.
    with pytest.raises(ValueError, match="0.001"):
        make_pose(x=1.0005).to_json()
    with pytest.raises(ValueError, match="0.001"):
        Envelope({"x_max": 0.0004}, ()).to_json()
    with pytest.raises(ValueError, match="0.001"):
        make_context(rho=55.0001).to_json()
    # MeasuredGeometry hashes its content when built, so the error comes then: the
    # robot's INFO;27 has four decimals, and the decoder must quantize explicitly.
    with pytest.raises(ValueError, match="0.001"):
        make_measured(tower_distance=2440.1234)


# ---------------------------------------------------------------------------
# tests_first: every context parameter has provenance
# ---------------------------------------------------------------------------

# How each CheckContext field carries its provenance. A new field must be added
# here, with its provenance, or this test fails.
FIELD_PROVENANCE = {
    "params": "each value is a Param (source and ref)",
    "footprint": "VENDOR_SUPPORT_PARAMS and MEASURED_SUPPORT_PARAMS in params",
    "rho": "params['rho']",
    "r_rot": "params['r_rot']",
    "rotation_model": "params['rotation_model'], checked against the gate view",
    "heading_map_verified": "params['heading_map_verified'], checked against the gate view",
    "gates": "snapshot_sha256(), written into the JSON",
    "start_pose": "Pose.source",
    "measured": "the raw INFO;25/INFO;27 lines and the content SHA-256",
    "obstacles": "Obstacle.ref",
    "fixations": "Fixation.ref",
    "prior_removal": "PriorCut.ref",
}


def test_every_context_param_has_provenance() -> None:
    assert {f.name for f in dataclasses.fields(CheckContext)} == set(FIELD_PROVENANCE)
    assert set(CONTEXT_SCALAR_PARAMS) == {"rho", "r_rot", "rotation_model", "heading_map_verified"}

    context = full_context()
    for name, param in context.params.items():
        assert isinstance(param, Param) and param.source in SOURCES and param.ref.strip(), name
    # Scalars equal the Param of the same name.
    assert context.params["rho"].value == context.rho
    assert context.params["r_rot"].value == context.r_rot
    assert context.params["rotation_model"].value == context.rotation_model
    assert context.params["heading_map_verified"].value == "verified"
    # The footprint's numbers are Params too: the spec ones from SPEC, the measured
    # ones from a measurement.
    for k, (r, delta) in enumerate(context.footprint.vendor):
        r_name, delta_name = VENDOR_SUPPORT_PARAMS[k]
        assert (context.params[r_name].value, context.params[delta_name].value) == (r, delta)
        assert context.params[r_name].source == context.params[delta_name].source == "SPEC"
    assert context.footprint.measured is not None
    for k, position in enumerate(context.footprint.measured):
        r_name, delta_name = MEASURED_SUPPORT_PARAMS[k]
        if position is None:
            assert r_name not in context.params and delta_name not in context.params
            continue
        assert (context.params[r_name].value, context.params[delta_name].value) == position
        assert context.params[r_name].source == "ROBOT_MEASURED"
    # rho derived from an unmeasured r_w still carries the amber disclosure.
    assert any(d.id == "DEFAULT_UNMEASURED:r_w" for d in context.params["rho"].all_disclosures())
    # The self-describing fields.
    assert context.start_pose is not None and context.start_pose.source
    assert context.measured is not None and HEX64.match(context.measured.sha256)
    assert all(item.ref for item in context.obstacles + context.fixations + context.prior_removal)


@pytest.mark.parametrize(
    "name",
    ["rho", "r_rot", "rotation_model", "heading_map_verified", "support_r1", "support_delta3", "support_r1_measured",
     "support_delta3_measured"],
)
def test_context_without_a_params_provenance_is_refused(name: str) -> None:
    context = full_context()
    params = dict(context.params)
    del params[name]
    with pytest.raises(ValueError, match=name):
        dataclasses.replace(context, params=params)


def test_context_params_must_agree_with_the_fields() -> None:
    footprint = make_footprint()
    with pytest.raises(ValueError, match="rho"):
        make_context(rho=50.0, params=context_params(footprint, rho=55.0))
    with pytest.raises(ValueError, match="r_rot"):
        make_context(r_rot=410.0, params=context_params(footprint, r_rot=404.0))
    # A vendor support value that is not the spec's provenance is refused.
    params = context_params(footprint)
    params["support_r1"] = Param(402.0, "mm", "OWNER_PROFILE", "machine.toml", disclosures=(unmeasured("x"),))
    with pytest.raises(ValueError, match="support_r1"):
        make_context(params=params)
    # A vendor support whose value differs from the footprint's.
    params = context_params(footprint)
    params["support_delta2"] = spec("support_delta2", 75.0, "deg")
    with pytest.raises(ValueError, match="support_delta2"):
        make_context(params=params)
    # Wrong unit.
    params = context_params(footprint)
    params["support_r2"] = spec("support_r2", 211.0, "um")
    with pytest.raises(ValueError, match="support_r2"):
        make_context(params=params)
    # A measured value that the footprint does not use would be silently ignored: refused.
    params = context_params(footprint)
    params["support_r2_measured"] = Param(210.0, "mm", "ROBOT_MEASURED", "machine.toml footprint.q2_mm")
    with pytest.raises(ValueError, match="support_r2_measured"):
        make_context(params=params)
    # A measured position cannot come from the spec or an unmeasured default.
    measured_fp = make_footprint(((405.5, 0.5), None, None))
    params = context_params(measured_fp, r_rot=406.0)
    params["support_r1_measured"] = spec("support_r1_measured", 405.5, "mm")
    with pytest.raises(ValueError, match="support_r1_measured"):
        make_context(footprint=measured_fp, r_rot=406.0, params=params)
    # Param values must be Params under string names.
    with pytest.raises(TypeError):
        make_context(params={**context_params(footprint), "x": 1.0})
    with pytest.raises(TypeError):
        make_context(params={**context_params(footprint), 3: Param(1.0, "mm", "POLICY", "x")})


def test_context_refuses_a_rotation_radius_below_a_support() -> None:
    # R_rot holds every support during a turn (safety-model 4.6): a smaller one
    # would check rotations with a disc that misses a wheel.
    with pytest.raises(ValueError, match="r_rot"):
        make_context(r_rot=401.0)
    measured = make_footprint(((405.5, 0.5), None, None))
    with pytest.raises(ValueError, match="r_rot"):
        make_context(footprint=measured, r_rot=404.0)
    assert make_context(footprint=measured, r_rot=405.5).r_rot == 405.5
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            make_context(rho=bad, params=context_params(make_footprint()))


# ---------------------------------------------------------------------------
# Value types: validation
# ---------------------------------------------------------------------------


def test_pose_validation() -> None:
    pose = Pose(1, 2, 350, "frames")
    assert (pose.x, pose.y, pose.c) == (1.0, 2.0, 350) and isinstance(pose.x, float)
    assert Pose(0.0, 0.0, None, "placeholder for the R0a rehearsal").c is None
    for c in (5, 360, -10, 355, 10.0, True):
        with pytest.raises((ValueError, TypeError), match="c"):
            Pose(0.0, 0.0, c, "x")  # type: ignore[arg-type]
    for bad in (float("nan"), float("inf"), True, "1", None):
        with pytest.raises((ValueError, TypeError)):
            Pose(bad, 0.0, None, "x")  # type: ignore[arg-type]
    for source in ("", "  ", None, 3):
        with pytest.raises((ValueError, TypeError), match="source"):
            Pose(0.0, 0.0, None, source)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Pose(0.0, 0.0, None)  # type: ignore[call-arg]


def test_footprint_set_validation() -> None:
    assert make_footprint().active == "vendor"
    union = make_footprint(((405.5, 0.5), None, (209.0, -75.5)))
    assert union.measured == ((405.5, 0.5), None, (209.0, -75.5))
    # Measured positions exist only with the union model; the union only with a measurement.
    with pytest.raises(ValueError, match="union"):
        FootprintSet(SPEC_FOOTPRINT, ((405.5, 0.5), None, None), "vendor")
    with pytest.raises(ValueError, match="union"):
        FootprintSet(SPEC_FOOTPRINT, None, "union")
    with pytest.raises(ValueError, match="measured"):
        FootprintSet(SPEC_FOOTPRINT, (None, None, None), "union")
    # Three supports, aligned: q1 rear, q2 right, q3 left.
    with pytest.raises(ValueError, match="three"):
        FootprintSet(SPEC_FOOTPRINT[:2], None, "vendor")
    with pytest.raises(ValueError, match="three"):
        FootprintSet(SPEC_FOOTPRINT, ((405.5, 0.5),), "union")
    for bad in (((0.0, 0.0),) + SPEC_FOOTPRINT[1:], ((-1.0, 0.0),) + SPEC_FOOTPRINT[1:],
                ((402.0, 181.0),) + SPEC_FOOTPRINT[1:], ((402.0,),) + SPEC_FOOTPRINT[1:]):
        with pytest.raises(ValueError):
            FootprintSet(bad, None, "vendor")
    with pytest.raises(ValueError, match="active"):
        FootprintSet(SPEC_FOOTPRINT, None, "measured")  # type: ignore[arg-type]


def test_emit_profile_has_no_defaults_and_validates() -> None:
    assert all(f.default is dataclasses.MISSING for f in dataclasses.fields(EmitProfile))
    with pytest.raises(TypeError):
        EmitProfile()  # type: ignore[call-arg]
    for z in EMIT_Z_LINE_STYLES:
        for rotation in EMIT_ROTATION_STYLES:
            for first in EMIT_FIRST_LINES:
                EmitProfile(z, rotation, first)
    with pytest.raises(ValueError, match="z_line_style"):
        EmitProfile("G00", "split", "combined")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="rotation_style"):
        EmitProfile("bare", "in-place", "combined")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="first_line"):
        EmitProfile("bare", "split", "rotate-then-translate")  # type: ignore[arg-type]


def test_literal_tuples_match_their_types() -> None:
    assert ROTATION_MODELS == get_args(RotationModel) == (
        "unverified", "in-place-shortest", "in-place-numeric", "during-translation", "about-centroid")
    assert EMIT_Z_LINE_STYLES == ("bare", "g00")
    assert EMIT_ROTATION_STYLES == ("split", "combined")
    assert EMIT_FIRST_LINES == ("combined", "translate-then-rotate")
    assert EXEMPTION_RUNGS == ("R3", "R3b", "R4")
    assert tuple(EXEMPT_RULES) == ("D-CSTEP:step-180", "D-CSTEP:xy-and-c")
    assert VENDOR_SUPPORT_PARAMS == (("support_r1", "support_delta1"), ("support_r2", "support_delta2"),
                                     ("support_r3", "support_delta3"))
    assert MEASURED_SUPPORT_PARAMS == tuple((r + "_measured", d + "_measured") for r, d in VENDOR_SUPPORT_PARAMS)
    # The vendor support names are the parameter catalogue's.
    catalogue = {e["name"] for e in json.loads((REPO / "docs" / "prep" / "params.json").read_text(encoding="utf-8"))}
    assert {name for pair in VENDOR_SUPPORT_PARAMS for name in pair} | {"rho", "r_rot"} <= catalogue


def test_measured_geometry_keeps_raw_lines_verbatim() -> None:
    raw = ("INFO;25;0.0000;0.0000", "INFO;25;1220.0000;0.0000", "INFO;25;1220.0000;2440.0000",
           "INFO;25;0.0000;2440.0000")
    measured = make_measured(raw_info25=raw)
    assert measured.raw_info25 == raw and measured.raw_info27 == "INFO;27;2440.0000"
    data = json.loads(canonical_json(measured).decode("utf-8"))
    assert data["raw_info25"] == list(raw) and data["raw_info27"] == "INFO;27;2440.0000"
    # The vendor's three-value form and a malformed tail are kept as they came: decoding is not this type's job.
    assert make_measured(raw_info25=("INFO;25;1;2;3", "INFO;25;x")).raw_info25 == ("INFO;25;1;2;3", "INFO;25;x")
    # Only INFO;25 lines in raw_info25, only an INFO;27 line in raw_info27, one line each.
    for bad in (("INFO;250;1;2",), ("INFO;27;2440.0000",), ("info;25;1;2",), ("INFO;25;1;2\n",),
                ("INFO;25;1;2\rINFO;25;3;4",), ()):
        with pytest.raises(ValueError):
            make_measured(raw_info25=bad)
    with pytest.raises(TypeError):
        make_measured(raw_info25="INFO;25;1;2")
    for bad27 in ("INFO;25;1;2", "INFO;27;1\n", ""):
        with pytest.raises(ValueError):
            make_measured(raw_info27=bad27)
    # A tower distance needs the line it was read from; no INFO;27 means no distance.
    with pytest.raises(ValueError, match="raw_info27"):
        make_measured(raw_info27=None)
    assert make_measured(raw_info27=None, tower_distance=None).tower_distance is None
    assert make_measured(tower_distance=None).raw_info27 == "INFO;27;2440.0000"


def test_measured_geometry_hash_binds_its_content() -> None:
    measured = make_measured()
    assert HEX64.match(measured.sha256)
    assert measured.sha256 == canonical_sha256(measured.content_json())
    assert "sha256" not in measured.content_json()
    # Any change of content changes the hash, so a tampered record is refused.
    data = json.loads(canonical_json(measured).decode("utf-8"))
    data["raw_info25"][0] = "INFO;25;0.0000;0.0001"
    with pytest.raises(ValueError, match="sha256"):
        MeasuredGeometry.from_json(data)
    with pytest.raises(ValueError, match="sha256"):
        dataclasses.replace(measured, set_panel_size=(1219, 2440))
    assert make_measured(set_panel_size=(1219, 2440)).sha256 != measured.sha256
    assert make_measured(park_pose=None).sha256 != measured.sha256


def test_measured_geometry_validation() -> None:
    for size in ((0, 10), (10,), (10, 10, 10), (10.0, 10), (True, 10), (-1, 5)):
        with pytest.raises((ValueError, TypeError), match="set_panel_size"):
            make_measured(set_panel_size=size)
    with pytest.raises(ValueError, match="tower2"):
        make_measured(tower2="Z")
    for wkt in ("", "POINT (1 1)", "POLYGON EMPTY", "polygon ((0 0, 1 0, 1 1, 0 0))"):
        with pytest.raises(ValueError, match="polygon_wkt"):
            make_measured(polygon_wkt=wkt)
    with pytest.raises(ValueError, match="hypothesis"):
        make_measured(hypothesis=" ")
    for distance in (0.0, -5.0, float("nan")):
        with pytest.raises(ValueError, match="tower_distance"):
            make_measured(tower_distance=distance)
    with pytest.raises(TypeError, match="park_pose"):
        make_measured(park_pose=(412.5, 300.0))
    with pytest.raises(ValueError, match="sha256"):
        MeasuredGeometry(("INFO;25;1;2",), None, "xy/identity", "POLYGON ((0 0, 1 0, 1 1, 0 0))", (1, 1), "X", None,
                         None, SHA_A)


def test_envelope_validation() -> None:
    envelope = Envelope({"x_max": 1210, "y_max": 2430.5}, ("contain the desk panel",))
    assert envelope.limits == {"x_max": 1210.0, "y_max": 2430.5}
    assert all(isinstance(v, float) for v in envelope.limits.values())
    # No limit at all would read as "anything carries over": refused.
    with pytest.raises(ValueError, match="limits"):
        Envelope({}, ())
    for bad in ({"": 1.0}, {"x": float("nan")}, {"x": True}, {"x": "1"}):
        with pytest.raises((ValueError, TypeError)):
            Envelope(bad, ())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="messages"):
        Envelope({"x": 1.0}, ("",))
    with pytest.raises(TypeError, match="messages"):
        Envelope({"x": 1.0}, "a message")  # type: ignore[arg-type]


def test_session_binding_validation() -> None:
    good = dict(session_id="session-0001", serial="TEST-SERIAL-0001", firmware="01.02.00", panel_sha256=SHA_A,
                facts_sha256=SHA_A, profile_sha256=SHA_A, material_sha256=SHA_A,
                detection_done_at="2026-11-04T10:15:00Z")
    SessionBinding(**good)
    for key in ("panel_sha256", "facts_sha256", "profile_sha256", "material_sha256"):
        for bad in ("A" * 64, "a" * 63, "g" * 64, "", None):
            with pytest.raises((ValueError, TypeError), match=key):
                SessionBinding(**{**good, key: bad})
    for when in ("2026-11-04T10:15:00", "2026-11-04T11:15:00+01:00", "yesterday", ""):
        with pytest.raises(ValueError, match="detection_done_at"):
            SessionBinding(**{**good, "detection_done_at": when})
    for firmware in ("", "unknown", "1.2", None):
        with pytest.raises((ValueError, TypeError), match="firmware"):
            SessionBinding(**{**good, "firmware": firmware})
    for key in ("session_id", "serial"):
        with pytest.raises(ValueError, match=key):
            SessionBinding(**{**good, key: "  "})


def test_obstacles_fixations_and_prior_cuts_validation() -> None:
    square = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))
    assert Obstacle([list(p) for p in square], 5, "clamp", "job.toml obstacles[0]").polygon == square  # type: ignore[arg-type]
    for polygon in (square[:2], square + (square[0],), ((0.0, 0.0), (1.0, 1.0), (2.0, 2.0)),
                    ((0.0, 0.0), (0.0, 0.0), (1.0, 0.0), (1.0, 1.0)), ((0.0, 0.0), (1.0,), (1.0, 1.0))):
        with pytest.raises(ValueError, match="polygon"):
            Obstacle(polygon, 5.0, "clamp", "job.toml obstacles[0]")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="polygon"):
            PriorCut(polygon, 2.0, "job.toml prior_removal[0]")  # type: ignore[arg-type]
    for height in (0.0, -1.0, float("inf")):
        with pytest.raises(ValueError, match="height_mm"):
            Obstacle(square, height, "clamp", "job.toml obstacles[0]")
        with pytest.raises(ValueError, match="height_mm"):
            Fixation((1.0, 1.0), height, "job.toml fixations[0]")
        with pytest.raises(ValueError, match="depth_mm"):
            PriorCut(square, height, "job.toml prior_removal[0]")
    for text_field in ("kind", "ref"):
        with pytest.raises(ValueError, match=text_field):
            Obstacle(square, 5.0, **{"kind": "clamp", "ref": "r", text_field: ""})
    with pytest.raises(ValueError, match="point"):
        Fixation((1.0,), 1.0, "job.toml fixations[0]")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="obstacles"):
        make_context(obstacles=(Fixation((1.0, 1.0), 1.0, "r"),))
    with pytest.raises(TypeError, match="prior_removal"):
        make_context(prior_removal=[square])


# ---------------------------------------------------------------------------
# RungExemption
# ---------------------------------------------------------------------------


def test_rung_exemption_validation() -> None:
    exemption = make_exemption()
    # Lines are kept in line order, whatever order they were given in.
    assert [line.line for line in exemption.lines] == [2, 14]
    data = exemption.to_json()
    assert data == canonical_json_roundtrip(exemption)
    assert data["payload_sha256"] == SHA_A and data["lines"][0]["rule"] == "D-CSTEP:xy-and-c"
    # Rung programs with exemptions are R3, R3b and R4 (safety-model section 14).
    for rung in ("R5", "R2", "r3", ""):
        with pytest.raises(ValueError, match="rung"):
            make_exemption(rung=rung)
    for sha in ("A" * 64, "a" * 63, "", None):
        with pytest.raises((ValueError, TypeError), match="payload_sha256"):
            make_exemption(payload_sha256=sha)
    with pytest.raises(ValueError, match="lines"):
        make_exemption(lines=())
    with pytest.raises(TypeError, match="lines"):
        make_exemption(lines=(("2", "D-CSTEP:step-180", "x"),))
    with pytest.raises(ValueError, match="twice"):
        make_exemption(lines=(ExemptLine(2, "D-CSTEP:step-180", "a"), ExemptLine(2, "D-CSTEP:step-180", "b")))
    # One line may be exempt from both rules.
    assert len(make_exemption(lines=(ExemptLine(2, "D-CSTEP:step-180", "a"),
                                     ExemptLine(2, "D-CSTEP:xy-and-c", "b"))).lines) == 2


def canonical_json_roundtrip(obj: Any) -> Any:
    return json.loads(canonical_json(obj).decode("utf-8"))


def test_exempt_line_lifts_only_the_two_rung_rules_of_d_cstep() -> None:
    assert set(EXEMPT_RULES) == {"D-CSTEP:step-180", "D-CSTEP:xy-and-c"}
    assert all(text.strip() for text in EXEMPT_RULES.values())
    # Never another check, and never the D-CSTEP rules a rung does not need: C off
    # the grid, C on a feed line, the cumulative rotation bound.
    for rule in ("W-SAFEZONE", "D-CSTEP", "D-CSTEP:off-grid", "D-CSTEP:c-on-feed", "D-CSTEP:cumulative", "D-ZSAFE",
                 ""):
        with pytest.raises(ValueError, match="rule"):
            ExemptLine(3, rule, "x")  # type: ignore[arg-type]
    for line in (0, -1, 1.0, True, "3"):
        with pytest.raises((ValueError, TypeError), match="line"):
            ExemptLine(line, "D-CSTEP:step-180", "x")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="reason"):
        ExemptLine(3, "D-CSTEP:step-180", " ")


def _constructions_outside_rungs(astscan: Any, root: Path) -> list[Any]:
    return [
        loc
        for loc in astscan.find_references(str(root), "RungExemption")
        if loc.kind in ("call", "store")
        and not (loc.module == "goliath_prep.rungs" or loc.module.startswith("goliath_prep.rungs."))
    ]


def test_rung_exemption_constructed_only_in_rungs_module(astscan: Any, tmp_path: Path) -> None:
    """Architecture: only goliath_prep.rungs (unit P18) builds a RungExemption.

    Statically, no module of goliath_prep or goliath_run outside goliath_prep.rungs
    calls RungExemption or rebinds the name; at run time the constructor refuses
    any other calling module, including through dataclasses.replace and copy.replace,
    and an exemption can neither be pickled nor read back from JSON.
    """
    # Positive control: the query finds a construction, under an alias too.
    fake = tmp_path / "fakepkg"
    fake.mkdir()
    (fake / "__init__.py").write_text("", encoding="utf-8")
    (fake / "mod.py").write_text(
        "from goliath_prep.core.types import RungExemption as RE\n"
        "from goliath_prep.core import types\n"
        "a = RE('R3', 'a' * 64, ())\n"
        "b = types.RungExemption('R3', 'a' * 64, ())\n",
        encoding="utf-8",
    )
    found = _constructions_outside_rungs(astscan, fake)
    assert [(loc.module, loc.line, loc.kind) for loc in found] == [
        ("fakepkg.mod", 3, "call"),
        ("fakepkg.mod", 4, "call"),
    ]
    # The real trees: the query sees the type (its own module refers to it) and no construction.
    references = astscan.find_references(str(SCANNED_SOURCES[0]), "RungExemption")
    assert any(loc.module == "goliath_prep.core.types" for loc in references)
    for root in SCANNED_SOURCES:
        assert _constructions_outside_rungs(astscan, root) == [], root

    # Run time: refused from any module but goliath_prep.rungs.
    lines = (ExemptLine(3, "D-CSTEP:step-180", "wrap"),)
    with pytest.raises(RungExemptionOriginError, match="goliath_prep.rungs"):
        RungExemption("R3", SHA_A, lines)
    exemption = make_exemption()
    assert exemption.rung == "R3"
    with pytest.raises(RungExemptionOriginError):
        dataclasses.replace(exemption, payload_sha256=SHA_B)
    if hasattr(copy, "replace"):  # Python 3.13+
        with pytest.raises(RungExemptionOriginError):
            copy.replace(exemption, payload_sha256=SHA_B)  # type: ignore[attr-defined]
    assert _RUNGS_STAND_IN["replace"](exemption, payload_sha256=SHA_B).payload_sha256 == SHA_B
    # Copies are the same object; pickling (a second way to make one) is refused.
    assert copy.copy(exemption) is exemption and copy.deepcopy(exemption) is exemption
    with pytest.raises(TypeError, match="pickle"):
        pickle.dumps(exemption)
    # It is written into the certificate, never read back: the runner re-certifies.
    assert not hasattr(RungExemption, "from_json")
