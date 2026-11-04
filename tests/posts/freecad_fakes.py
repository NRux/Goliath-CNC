# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Stand-ins for the FreeCAD objects the Goliath FreeCAD post reads.

FreeCAD cannot run in every test job, so the post's tests (and the committed
samples in posts/freecad/samples/) use these small classes in its place. They
carry only the attributes the post reads, with FreeCAD's names and units as
FreeCAD 1.1.4's public CAM sources define them:

* a path command has ``Name`` (``"G1"``, ``"M3"``, or a comment such as
  ``"(Begin Drilling)"``) and ``Parameters`` (letter -> float). Lengths are
  millimetres; **feed rates are millimetres per second**, FreeCAD's internal
  speed unit, which posts convert to millimetres per minute;
* an operation has ``Path``, ``ToolController``, ``Active``, ``FinalDepth``
  (a quantity in mm), ``CoolantMode`` and a ``Proxy`` whose class lives in a
  module such as ``Path.Op.Profile``; it reaches its job through ``InList``
  (operation -> the "Operations" group -> the job);
* a tool controller has ``ToolNumber``, ``Tool`` (the tool bit: ``Diameter``,
  ``CuttingEdgeHeight``, ``ShapeType``, ``Label``) and a ``Path`` that FreeCAD
  builds as a comment, ``M6 T<n>`` and ``M3 S<rpm>`` (``M4`` for reverse, no
  spindle word when the spindle is off or its speed is 0);
* the job has ``Stock`` (a shape with ``BoundBox`` and ``Volume``),
  ``Operations``, ``Fixtures`` and ``PostProcessorArgs``; the fixture object
  FreeCAD puts in the post list is named "Fixture" and holds ``G54``.

These are test doubles, not FreeCAD: a sample written through them says so in
its SOURCE line (``cam="FreeCAD (stand-in objects, not a FreeCAD run)"``).

Regenerate the committed samples after a deliberate change to the post:

    python tests/posts/freecad_fakes.py --write-samples

Python 3.9 compatible: the repository-level suite runs on 3.9.
"""
from __future__ import annotations

import argparse
import importlib
import sys
import types
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[2]
POST_DIR = REPO / "posts" / "freecad"
SAMPLES_DIR = POST_DIR / "samples"

STAND_IN_VERSION = ["(stand-in objects, not a FreeCAD run)"]


def per_second(mm_per_min: float) -> float:
    """A feed rate as FreeCAD stores it (mm/s) for a value set in mm/min."""
    return mm_per_min / 60.0


# --------------------------------------------------------------------------
# Objects
# --------------------------------------------------------------------------


class FakeCommand:
    def __init__(self, name: str, params: Optional[Dict[str, float]] = None) -> None:
        self.Name = name
        self.Parameters = dict(params or {})

    def __repr__(self) -> str:
        return "FakeCommand(%r, %r)" % (self.Name, self.Parameters)


def cmd(text: str) -> FakeCommand:
    """``cmd("G1 X10 Y2.5 F13.3")`` -> FakeCommand("G1", {"X": 10.0, ...}); ``cmd("(note)")`` -> a comment."""
    if text.startswith("("):
        return FakeCommand(text)
    words = text.split()
    params = {}
    for word in words[1:]:
        params[word[0]] = float(word[1:])
    return FakeCommand(words[0], params)


def cmds(*texts: str) -> List[FakeCommand]:
    return [cmd(t) for t in texts]


class FakePath:
    def __init__(self, commands: Iterable[Any]) -> None:
        self.Commands = list(commands)


class FakeQuantity:
    """A FreeCAD quantity: ``Value`` is in FreeCAD's internal unit (mm for lengths)."""

    def __init__(self, value: float) -> None:
        self.Value = value


class FakeBoundBox:
    def __init__(self, xmin: float, ymin: float, zmin: float, xmax: float, ymax: float, zmax: float) -> None:
        self.XMin, self.YMin, self.ZMin = xmin, ymin, zmin
        self.XMax, self.YMax, self.ZMax = xmax, ymax, zmax


class FakeShape:
    def __init__(self, bound_box: FakeBoundBox, volume: Optional[float] = None) -> None:
        self.BoundBox = bound_box
        if volume is None:
            box = bound_box
            volume = (box.XMax - box.XMin) * (box.YMax - box.YMin) * (box.ZMax - box.ZMin)
        self.Volume = volume


class FakeObject:
    """A document object: Name (unique, like "Profile001"), Label (the user's name), InList."""

    def __init__(self, name: str, label: Optional[str] = None) -> None:
        self.Name = name
        self.Label = label if label is not None else name
        self.InList: List[Any] = []


class FakeStock(FakeObject):
    def __init__(self, shape: FakeShape, name: str = "Stock") -> None:
        super().__init__(name)
        self.Shape = shape


class FakeJob(FakeObject):
    def __init__(self, stock: FakeStock, name: str = "Job", fixtures: Sequence[str] = ("G54",)) -> None:
        super().__init__(name)
        self.Stock = stock
        self.Fixtures = list(fixtures)
        self.PostProcessorArgs = ""
        self.Operations = FakeObject("Operations")
        self.Operations.InList.append(self)
        self.Operations.Group = []


class FakeToolBit(FakeObject):
    def __init__(
        self,
        label: str = "6 mm Endmill",
        diameter: float = 6.0,
        flute: Optional[float] = 22.0,
        shape_type: Optional[str] = "Endmill",
        name: str = "ToolBit",
    ) -> None:
        super().__init__(name, label)
        self.Diameter = FakeQuantity(diameter)
        if flute is not None:
            self.CuttingEdgeHeight = FakeQuantity(flute)
        if shape_type is not None:
            self.ShapeType = shape_type


class FakeToolController(FakeObject):
    def __init__(
        self,
        bit: Optional[FakeToolBit] = None,
        number: int = 1,
        rpm: float = 18000.0,
        direction: str = "Forward",
        name: str = "ToolController",
        label: str = "TC: 6 mm Endmill",
    ) -> None:
        super().__init__(name, label)
        self.ToolNumber = number
        self.Tool = bit if bit is not None else FakeToolBit()
        self.SpindleSpeed = rpm
        self.SpindleDir = direction
        commands = [FakeCommand("(%s)" % label), FakeCommand("M6", {"T": number})]
        if direction != "None" and rpm != 0:
            commands.append(FakeCommand("M3" if direction == "Forward" else "M4", {"S": rpm}))
        self.Path = FakePath(commands)


def proxy_of(module: str, cls: str) -> Any:
    """An instance of a class named ``cls`` that claims to live in ``module``, like a FreeCAD proxy."""
    klass = type(cls, (), {})
    klass.__module__ = module
    return klass()


PROFILE = ("Path.Op.Profile", "ObjectProfile")
POCKET = ("Path.Op.Pocket", "ObjectPocket")
DRILLING = ("Path.Op.Drilling", "ObjectDrilling")
ADAPTIVE = ("Path.Op.Adaptive", "PathAdaptive")
ENGRAVE = ("Path.Op.Engrave", "ObjectEngrave")
SLOT = ("Path.Op.Slot", "ObjectSlot")
DRESSUP = ("Path.Dressup.Tags", "ObjectDressup")


class FakeOp(FakeObject):
    def __init__(
        self,
        name: str,
        commands: Iterable[Any],
        tc: Optional[FakeToolController],
        proxy: Tuple[str, str] = PROFILE,
        label: Optional[str] = None,
        final_depth: Optional[float] = 0.0,
        active: bool = True,
        coolant: str = "None",
    ) -> None:
        super().__init__(name, label)
        self.Path = FakePath(commands)
        self.ToolController = tc
        self.Active = active
        self.CoolantMode = coolant
        if final_depth is not None:
            self.FinalDepth = FakeQuantity(final_depth)
        self.Proxy = proxy_of(*proxy)


class FakeDressup(FakeObject):
    """A dressup: no ToolController, Active or FinalDepth of its own; they come from ``Base``."""

    def __init__(self, name: str, base: FakeOp, commands: Iterable[Any], label: Optional[str] = None) -> None:
        super().__init__(name, label)
        self.Base = base
        self.Path = FakePath(commands)
        self.Proxy = proxy_of(*DRESSUP)
        base.InList.append(self)


class FakeFixture:
    """The object FreeCAD's post list builder makes for a fixture (its InList is shared and unreliable)."""

    Name = "Fixture"
    Label = "Fixture"

    def __init__(self, code: str = "G54") -> None:
        self.Path = FakePath([FakeCommand(code)])
        self.InList: List[Any] = []


class FakePlacement:
    def __init__(self, identity: bool) -> None:
        self._identity = identity

    def isIdentity(self) -> bool:  # noqa: N802 - FreeCAD's method name
        return self._identity


def add_op(job: FakeJob, op: Any) -> Any:
    job.Operations.Group.append(op)
    op.InList.append(job.Operations)
    return op


def box_job(
    xmin: float = 0.0,
    ymin: float = 0.0,
    zmin: float = 0.0,
    xmax: float = 2440.0,
    ymax: float = 1220.0,
    zmax: float = 18.0,
    fixtures: Sequence[str] = ("G54",),
) -> FakeJob:
    return FakeJob(FakeStock(FakeShape(FakeBoundBox(xmin, ymin, zmin, xmax, ymax, zmax))), fixtures=fixtures)


def post_list(job: FakeJob, *items: Any) -> List[Any]:
    """The list FreeCAD hands the post: the fixture, then tool controllers and operations in order."""
    return [FakeFixture()] + list(items)


# --------------------------------------------------------------------------
# A FreeCAD module stand-in
# --------------------------------------------------------------------------


class FakeConsole:
    def __init__(self) -> None:
        self.errors: List[str] = []
        self.messages: List[str] = []

    def PrintError(self, text: str) -> None:  # noqa: N802 - FreeCAD's method name
        self.errors.append(text)

    def PrintMessage(self, text: str) -> None:  # noqa: N802 - FreeCAD's method name
        self.messages.append(text)


def fake_freecad_module(version: Sequence[str] = tuple(STAND_IN_VERSION)) -> types.ModuleType:
    module = types.ModuleType("FreeCAD")
    module.Console = FakeConsole()  # type: ignore[attr-defined]
    module.Version = lambda: list(version)  # type: ignore[attr-defined]
    return module


def import_post() -> Tuple[types.ModuleType, types.ModuleType]:
    """(goliath_post, gi_format) imported from posts/freecad of this checkout."""
    if str(POST_DIR) not in sys.path:
        sys.path.insert(0, str(POST_DIR))
    gi_format = importlib.import_module("gi_format")
    goliath_post = importlib.import_module("goliath_post")
    for module in (gi_format, goliath_post):
        if Path(module.__file__).resolve().parent != POST_DIR.resolve():
            raise RuntimeError("imported %s from %s, not from %s" % (module.__name__, module.__file__, POST_DIR))
    return goliath_post, gi_format


# --------------------------------------------------------------------------
# Samples: realistic FreeCAD 1.1.4 command sequences
# --------------------------------------------------------------------------

F_PLUNGE = per_second(200.0)
F_CUT = per_second(800.0)
F_RAPID = per_second(3000.0)  # FreeCAD writes rapid rates on G0 lines; GI has no rapid speed


def sample_profile_and_pocket() -> Tuple[List[Any], str]:
    """A pocket (not through) and a through profile with arcs, on a 2440 x 1220 x 18 mm panel."""
    job = box_job()
    tc = FakeToolController(FakeToolBit("6 mm 2F upcut", 6.0, 22.0, "Endmill"), number=1, rpm=18000.0)
    pocket = add_op(
        job,
        FakeOp(
            "Pocket_Shape",
            cmds(
                "(Pocket_Shape)",
                "G0 Z23.0 F%r" % F_RAPID,
                "G0 X600.0 Y400.0 F%r" % F_RAPID,
                "G0 Z21.0 F%r" % F_RAPID,
                "G1 Z14.0 F%r" % F_PLUNGE,
                "G1 X700.0 Y400.0 F%r" % F_CUT,
                "G1 X700.0 Y450.0 F%r" % F_CUT,
                "G1 X600.0 Y450.0 F%r" % F_CUT,
                "G1 X600.0 Y400.0 F%r" % F_CUT,
                "G0 Z23.0 F%r" % F_RAPID,
            ),
            tc,
            proxy=("Path.Op.PocketShape", "ObjectPocket"),
            label="Hinge pocket",
            final_depth=14.0,
        ),
    )
    profile = add_op(
        job,
        FakeOp(
            "Profile",
            cmds(
                "(Profile)",
                "(Compensated Tool Path. Diameter: 6.0)",
                "G0 Z23.0 F%r" % F_RAPID,
                "G0 X797.0 Y300.0 F%r" % F_RAPID,
                "G0 Z21.0 F%r" % F_RAPID,
                "G1 Z-0.2 F%r" % F_PLUNGE,
                "G1 X797.0 Y600.0 F%r" % F_CUT,
                "G2 X800.0 Y603.0 Z-0.2 I3.0 J0.0 K0.0 F%r" % F_CUT,
                "G1 X1100.0 Y603.0 F%r" % F_CUT,
                "G2 X1103.0 Y600.0 Z-0.2 I0.0 J-3.0 K0.0 F%r" % F_CUT,
                "G1 X1103.0 Y300.0 F%r" % F_CUT,
                "G2 X1100.0 Y297.0 Z-0.2 I-3.0 J0.0 K0.0 F%r" % F_CUT,
                "G1 X800.0 Y297.0 F%r" % F_CUT,
                "G2 X797.0 Y300.0 Z-0.2 I0.0 J3.0 K0.0 F%r" % F_CUT,
                "G0 Z23.0 F%r" % F_RAPID,
            ),
            tc,
            label="Part A outline",
            final_depth=-0.2,
        ),
    )
    return post_list(job, tc, pocket, profile), "--tower2=Y"


def sample_drilling_cycles() -> Tuple[List[Any], str]:
    """FreeCAD Drilling with G98/G81 and G99/G83 pecks, on stock spanning Z 1.05 to 19.05 mm."""
    job = box_job(zmin=1.05, zmax=19.05, xmax=1220.0, ymax=610.0)
    # FreeCAD's Drill bit shape has no cutting edge height, which GI needs: a router drills with an endmill.
    tc = FakeToolController(
        FakeToolBit("5 mm endmill", 5.0, 25.0, "Endmill"), number=3, rpm=12000.0, label="TC: 5 mm endmill"
    )
    vert = per_second(150.0)
    drill = add_op(
        job,
        FakeOp(
            "Drilling",
            cmds(
                "(Drilling)",
                "(Begin Drilling)",
                "G90",
                "G0 Z25.05 F%r" % F_RAPID,
                "G98",
                "G0 X100.0 Y100.0 F%r" % F_RAPID,
                "G0 Z23.05 F%r" % F_RAPID,
                "G81 X100.0 Y100.0 Z0.55 R21.05 F%r" % vert,
                "G0 X200.0 Y100.0 F%r" % F_RAPID,
                "G81 X200.0 Y100.0 Z0.55 R21.05 F%r" % vert,
                "G80",
                "G0 Z25.05 F%r" % F_RAPID,
            ),
            tc,
            proxy=DRILLING,
            label="Through holes",
            final_depth=1.05,
        ),
    )
    peck = add_op(
        job,
        FakeOp(
            "Drilling001",
            cmds(
                "(Drilling001)",
                "(Begin Drilling)",
                "G90",
                "G0 Z25.05 F%r" % F_RAPID,
                "G99",
                "G0 X300.0 Y200.0 F%r" % F_RAPID,
                "G0 Z23.05 F%r" % F_RAPID,
                "G83 X300.0 Y200.0 Z9.05 R21.05 Q4.0 F%r" % vert,
                "G0 X350.0 Y200.0 F%r" % F_RAPID,
                "G83 X350.0 Y200.0 Z9.05 R21.05 Q4.0 F%r" % vert,
                "G80",
                "G0 Z25.05 F%r" % F_RAPID,
            ),
            tc,
            proxy=DRILLING,
            label="Blind holes 10 mm",
            final_depth=9.05,
            coolant="Flood",
        ),
    )
    return post_list(job, tc, drill, peck), "--tower2=X"


def sample_adaptive_helix_two_speeds() -> Tuple[List[Any], str]:
    """An adaptive op entering on a helix, then an engrave op whose controller runs the same tool slower."""
    job = box_job(xmax=800.0, ymax=600.0, zmax=12.0)
    bit = FakeToolBit("3 mm ball", 3.0, 12.0, "Ballend")
    fast = FakeToolController(bit, number=2, rpm=20000.0, name="ToolController", label="TC: 3 mm ball fast")
    # FreeCAD keeps labels unique, so the second controller's copy of the bit is "3 mm ball001".
    slow = FakeToolController(
        FakeToolBit("3 mm ball001", 3.0, 12.0, "Ballend", name="ToolBit001"),
        number=2,
        rpm=16000.0,
        name="ToolController001",
        label="TC: 3 mm ball slow",
    )
    adaptive = add_op(
        job,
        FakeOp(
            "Adaptive",
            cmds(
                "(Adaptive)",
                "G0 Z17.0 F%r" % F_RAPID,
                "G0 X205.0 Y200.0 F%r" % F_RAPID,
                "G0 Z13.0 F%r" % F_RAPID,
                "G1 Z12.0 F%r" % F_PLUNGE,
                "G3 X195.0 Y200.0 Z11.5 I-5.0 J0.0 K0.0 F%r" % F_PLUNGE,
                "G3 X205.0 Y200.0 Z11.0 I5.0 J0.0 K0.0 F%r" % F_PLUNGE,
                "G3 X205.0 Y200.0 Z11.0 I-5.0 J0.0 K0.0 F%r" % F_CUT,
                "G1 X230.0 Y200.0 F%r" % F_CUT,
                "G0 Z17.0 F%r" % F_RAPID,
            ),
            fast,
            proxy=ADAPTIVE,
            label="Adaptive clearing",
            final_depth=11.0,
        ),
    )
    engrave = add_op(
        job,
        FakeOp(
            "Engrave",
            cmds(
                "(Engrave)",
                "G0 Z17.0 F%r" % F_RAPID,
                "G0 X50.0 Y50.0 F%r" % F_RAPID,
                "G1 Z11.5 F%r" % F_PLUNGE,
                "G1 X150.0 Y50.0 F%r" % per_second(600.0),
                "G1 X150.0 Y80.0",
                "G0 Z17.0 F%r" % F_RAPID,
            ),
            slow,
            proxy=ENGRAVE,
            label="Text",
            final_depth=11.5,
        ),
    )
    return post_list(job, fast, adaptive, slow, engrave), "--tower2=Y"


SAMPLES: Dict[str, Callable[[], Tuple[List[Any], str]]] = {
    "profile-and-pocket.gi": sample_profile_and_pocket,
    "drilling-cycles.gi": sample_drilling_cycles,
    "adaptive-helix-two-speeds.gi": sample_adaptive_helix_two_speeds,
}


def render_sample(name: str) -> str:
    """The text the post writes for sample ``name``, through goliath_post.export with a stand-in FreeCAD module."""
    goliath_post, _ = import_post()
    objects, args = SAMPLES[name]()
    saved = sys.modules.get("FreeCAD")
    sys.modules["FreeCAD"] = fake_freecad_module()
    try:
        return goliath_post.export(objects, "-", args)
    finally:
        if saved is None:
            del sys.modules["FreeCAD"]
        else:
            sys.modules["FreeCAD"] = saved


def write_samples() -> List[Path]:
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for name in sorted(SAMPLES):
        path = SAMPLES_DIR / name
        path.write_bytes(render_sample(name).encode("utf-8"))
        written.append(path)
    return written


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Stand-in FreeCAD objects for the Goliath FreeCAD post's tests.")
    parser.add_argument("--write-samples", action="store_true", help="rewrite posts/freecad/samples/*.gi")
    args = parser.parse_args(argv)
    if not args.write_samples:
        parser.print_help()
        return 2
    for path in write_samples():
        print("wrote %s" % path.relative_to(REPO).as_posix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
