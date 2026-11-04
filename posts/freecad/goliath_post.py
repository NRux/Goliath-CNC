# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 the Goliath-CNC contributors.
"""Goliath CNC post-processor for FreeCAD CAM: writes GI for goliath-prep.

A script post: FreeCAD calls ``export(objectslist, filename, argstring)``, the
function FreeCAD 1.1.4's own script posts define. It writes GI, the
Goliath-intermediate G-code of docs/prep/gi-format.md in the Goliath-CNC
repository, which goliath-prep reads, checks and turns into the program the
robot runs. The output is not for the robot directly.

Install: copy this file and ``gi_format.py`` (both, side by side) into
FreeCAD's macro folder or another folder FreeCAD searches for posts. In the CAM
job choose the post processor ``goliath`` and set its arguments to
``--tower2=Y`` or ``--tower2=X`` (where tower 2 stands, seen from tower 1). See
posts/freecad/README.md.

What it reads from FreeCAD, and only from FreeCAD (never job.toml: the GI header
and job.toml are compared by goliath-prep, which refuses any disagreement
naming both, red-team rule PHYS-12):

* the **stock** of the job: its bounding box gives STOCK x, y, t and ZSHIFT
  (Z = 0 at the stock top). The stock must be a box whose minimum corner is at
  X = 0, Y = 0 (the corner where tower 1 stands); otherwise the post refuses;
* the **tower-2 axis** from the job's post-processor arguments; there is no
  default;
* the **tool** of each operation's tool controller (number, diameter, cutting
  edge height as the flute length, shape, spindle speed from the controller's
  path). GI carries one tool;
* one ``OP`` block per active **operation**, its commands from the operation's
  path. Inactive operations are left out with a note in the file.

Every refusal raises GoliathPostError with the reason and the fix, and is also
printed to FreeCAD's report view. A refused export writes no file.

Python 3.9 or newer, standard library only.
"""
from __future__ import annotations

import importlib.util
import shlex
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

TOOLTIP = """Goliath CNC post-processor: writes GI (Goliath-intermediate G-code) for goliath-prep,
which checks the job and writes the program the Goliath runs. Millimetres, absolute,
origin at the stock's minimum corner (the tower-1 corner), Z = 0 at the stock top,
one tool per file."""

TOOLTIP_ARGS = """--tower2=Y   tower 2 stands on the Y axis seen from tower 1 (at X = 0, Y = panel height)
--tower2=X   tower 2 stands on the X axis seen from tower 1 (at X = panel width, Y = 0)
The argument is required; there is no default."""

# FreeCAD 1.1 reads UNITS from a script post to decide between metric and inch.
UNITS = "G21"

_GI_FORMAT_ID = "goliath-cnc posts/freecad gi_format"
_PRIVATE_NAME = "goliath_post_gi_format"


def _report_error(text: str) -> None:
    """Print to FreeCAD's report view when FreeCAD is loaded; the exception carries the message either way."""
    module = sys.modules.get("FreeCAD")
    console = getattr(module, "Console", None) if module is not None else None
    if console is not None:
        console.PrintError(text)


def _load_gi_format() -> ModuleType:
    """gi_format.py from this file's folder, whether or not that folder is on sys.path.

    FreeCAD 1.1 loads a script post from its file without adding the folder to
    sys.path, so a plain ``import gi_format`` may find nothing, or a different
    module of that name. Another module named gi_format is never executed: its
    location is checked before it is imported.
    """
    here = Path(__file__).resolve().parent
    ours = here / "gi_format.py"
    if not ours.is_file():
        raise ImportError(
            "goliath_post.py needs gi_format.py next to it, in %s: copy both files of the Goliath post into "
            "that folder" % here
        )
    loaded = sys.modules.get("gi_format")
    if loaded is None:
        spec = importlib.util.find_spec("gi_format")
        if spec is not None and spec.origin and Path(spec.origin).resolve() == ours:
            import gi_format as loaded  # noqa: F811 - the module next to this file, on sys.path
    if loaded is not None and Path(getattr(loaded, "__file__", "")).resolve() == ours:
        if getattr(loaded, "GI_FORMAT_MODULE_ID", None) == _GI_FORMAT_ID:
            return loaded
    private = sys.modules.get(_PRIVATE_NAME)
    if private is not None and Path(getattr(private, "__file__", "")).resolve() == ours:
        if getattr(private, "GI_FORMAT_MODULE_ID", None) == _GI_FORMAT_ID:
            return private
    spec = importlib.util.spec_from_file_location(_PRIVATE_NAME, ours)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load %s" % ours)
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: dataclasses look their module up in sys.modules.
    sys.modules[_PRIVATE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[_PRIVATE_NAME]
        raise
    if getattr(module, "GI_FORMAT_MODULE_ID", None) != _GI_FORMAT_ID:
        del sys.modules[_PRIVATE_NAME]  # never reused by a later load
        raise ImportError(
            "%s is not the Goliath post's gi_format module: copy gi_format.py from the same Goliath post "
            "release as goliath_post.py" % ours
        )
    return module


try:
    gi_format = _load_gi_format()
except Exception as _exc:
    # FreeCAD 1.1.4 skips a post whose loading raises ImportError while it searches its post
    # folders, and shows nothing; the report view is where the owner learns why.
    _report_error("Goliath post cannot load: %s\n" % _exc)
    raise
GoliathPostError = gi_format.GoliathPostError


def export(objectslist: Any, filename: Any, argstring: Any) -> str:
    """Write the GI for ``objectslist`` to ``filename`` ("-" or None: write no file) and return it.

    ``argstring`` is the job's post-processor arguments (``--tower2=X|Y``).
    Raises GoliathPostError, after printing it to FreeCAD's report view, when
    the job cannot be written as GI exactly.
    """
    try:
        text = _gi_text(objectslist, argstring)
    except GoliathPostError as exc:
        _report_error("Goliath post refused the job: %s\n" % exc)
        raise
    if filename is not None and filename != "-":
        with open(filename, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    return text


def parse_arguments(argstring: Any) -> str:
    """The tower-2 axis from ``--tower2=X|Y`` (or ``--tower2 X|Y``); anything else is refused."""
    text = "" if argstring is None else argstring
    if not isinstance(text, str):
        raise GoliathPostError("the post-processor arguments are %r, not text" % (argstring,))
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise GoliathPostError("the post-processor arguments %r cannot be split: %s" % (text, exc)) from exc
    axis: Optional[str] = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--tower2":
            if index + 1 >= len(tokens):
                raise GoliathPostError("--tower2 needs a value: --tower2=Y or --tower2=X")
            value = tokens[index + 1]
            index += 2
        elif token.startswith("--tower2="):
            value = token[len("--tower2="):]
            index += 1
        else:
            raise GoliathPostError(
                "unknown post-processor argument %r; the Goliath post takes only --tower2=X or --tower2=Y" % token
            )
        if axis is not None:
            raise GoliathPostError("--tower2 is given more than once in %r" % text)
        if value.upper() not in ("X", "Y"):
            raise GoliathPostError("--tower2 must be X or Y, not %r" % value)
        axis = value.upper()
    if axis is None:
        raise GoliathPostError(
            "the job's post-processor arguments must say where tower 2 stands: --tower2=Y (tower 2 at X = 0, "
            "Y = panel height) or --tower2=X (tower 2 at X = panel width, Y = 0). There is no default"
        )
    return axis


# --------------------------------------------------------------------------
# Reading the FreeCAD job
# --------------------------------------------------------------------------


def _gi_text(objectslist: Any, argstring: Any) -> str:
    tower2 = parse_arguments(argstring)
    if objectslist is None:
        raise GoliathPostError("FreeCAD passed no objects to the post")
    objects = list(objectslist)
    ops, fixtures, notes = _sort_objects(objects)
    if not ops:
        raise GoliathPostError("there is no active operation in what FreeCAD passed to the post")
    job = _job_of(ops)
    _check_fixtures(job, fixtures)
    stock = _stock_of(job)
    tools: Dict[int, Any] = {}
    op_data = []
    for op in ops:
        tc = _op_property(op, "ToolController")
        if tc is None:
            raise GoliathPostError("%s has no tool controller; give it one" % _describe(op))
        tool = tools.get(id(tc))
        if tool is None:
            tool = _tool_of(tc)
            tools[id(tc)] = tool
        kind, kind_note = _kind_of(op)
        op_data.append(
            gi_format.OpData(
                id=_text_attr(op, "Name"),
                kind=kind,
                label=_text_attr(op, "Label"),
                tool=tool.number,
                spindle_rpm=tool.rpm,
                commands=tuple(_path_commands(op)),
                final_depth=_final_depth(op),
                coolant=_coolant(op),
                notes=(kind_note,) if kind_note else (),
            )
        )
    cam = gi_format.freecad_cam_text(_freecad().Version())
    return gi_format.format_gi(op_data, stock, list(tools.values()), tower2=tower2, cam=cam, notes=notes)


def _freecad() -> Any:
    module = sys.modules.get("FreeCAD")
    if module is None:
        try:
            import FreeCAD as module  # type: ignore[import-not-found,no-redef]
        except ImportError as exc:
            raise GoliathPostError("FreeCAD is not loaded: this post runs inside FreeCAD or freecadcmd") from exc
    return module


def _describe(obj: Any) -> str:
    return "FreeCAD operation %r (%s)" % (getattr(obj, "Label", "?"), getattr(obj, "Name", "?"))


def _text_attr(obj: Any, name: str) -> str:
    value = getattr(obj, name, None)
    if not isinstance(value, str):
        raise GoliathPostError("FreeCAD object %r has %s %r, not text" % (obj, name, value))
    return value


def _single_base(obj: Any) -> Any:
    """A dressup's base operation, or None. (An operation's Base is a list of geometry links, not an operation.)"""
    base = getattr(obj, "Base", None)
    if base is None or isinstance(base, (list, tuple)) or not hasattr(base, "Path"):
        return None
    return base


def _op_property(obj: Any, name: str, default: Any = None) -> Any:
    """``obj.<name>``, or that of the operation a dressup is built on, as FreeCAD itself resolves it."""
    seen = set()
    while obj is not None and id(obj) not in seen:
        seen.add(id(obj))
        if hasattr(obj, name):
            return getattr(obj, name)
        obj = _single_base(obj)
    return default


def _is_tool_controller(obj: Any) -> bool:
    return hasattr(obj, "ToolNumber") and hasattr(obj, "Tool") and hasattr(obj, "Path")


def _is_fixture(obj: Any) -> bool:
    return getattr(obj, "Name", None) == "Fixture" and hasattr(obj, "Path") and not hasattr(obj, "ToolController")


def _is_operation(obj: Any) -> bool:
    """An object with a path that has a ToolController property, itself or through a dressup's base."""
    if not hasattr(obj, "Path"):
        return False
    seen = set()
    while obj is not None and id(obj) not in seen:
        seen.add(id(obj))
        if hasattr(obj, "ToolController"):
            return True
        obj = _single_base(obj)
    return False


def _sort_objects(objects: List[Any]) -> Tuple[List[Any], List[Any], List[str]]:
    """(active operations, fixture objects, notes). Tool controllers are read through the operations."""
    ops, fixtures, notes = [], [], []
    for obj in objects:
        if _is_tool_controller(obj):
            continue
        if _is_fixture(obj):
            fixtures.append(obj)
            continue
        if _is_operation(obj):
            if not _op_property(obj, "Active", True):
                notes.append("%s is inactive and was not written" % _describe(obj))
                continue
            ops.append(obj)
            continue
        raise GoliathPostError(
            "FreeCAD passed the object %r (%s) to the post; it is neither an operation, a tool controller nor "
            "a fixture, and this post does not know what to write for it"
            % (getattr(obj, "Label", "?"), getattr(obj, "Name", "?"))
        )
    return ops, fixtures, notes


def _looks_like_job(obj: Any) -> bool:
    return hasattr(obj, "Stock") and hasattr(obj, "Operations")


def _find_jobs(obj: Any) -> List[Any]:
    """The CAM jobs above ``obj``: operation -> the Operations group -> the job (through dressups too)."""
    found: List[Any] = []
    seen = {id(obj)}
    frontier = [obj]
    for _ in range(16):
        following = []
        for item in frontier:
            for parent in list(getattr(item, "InList", None) or []):
                if id(parent) in seen:
                    continue
                seen.add(id(parent))
                if _looks_like_job(parent):
                    found.append(parent)
                else:
                    following.append(parent)
        if not following:
            break
        frontier = following
    return found


def _job_of(ops: List[Any]) -> Any:
    jobs: List[Any] = []
    for op in ops:
        found = _find_jobs(op)
        if not found:
            raise GoliathPostError("%s is not inside a CAM job" % _describe(op))
        for job in found:
            if all(job is not known for known in jobs):
                jobs.append(job)
    if len(jobs) > 1:
        raise GoliathPostError(
            "the operations belong to more than one job (%s); post one job at a time"
            % ", ".join(repr(getattr(j, "Label", "?")) for j in jobs)
        )
    return jobs[0]


def _check_fixtures(job: Any, fixtures: List[Any]) -> None:
    """One fixture, G54: the Goliath has no work offsets, and a second fixture would repeat the program."""
    declared = getattr(job, "Fixtures", None)
    if declared is not None and [str(f) for f in declared] != ["G54"]:
        raise GoliathPostError(
            "job %r uses the fixtures %s; the Goliath has no work offsets. Set the job's fixtures to G54 only"
            % (getattr(job, "Label", "?"), ", ".join(str(f) for f in declared))
        )
    for fixture in fixtures:
        commands = list(getattr(getattr(fixture, "Path", None), "Commands", None) or [])
        codes = [gi_format.command_code(getattr(c, "Name", None)) for c in commands]
        if codes != ["G54"]:
            raise GoliathPostError(
                "the job's fixture writes %s; the Goliath has no work offsets. Set the job's fixtures to G54 only"
                % ", ".join(gi_format.command_text(c) for c in commands)
            )


def _float(value: Any, what: str) -> float:
    """A FreeCAD quantity (its Value, in FreeCAD's internal unit: mm for lengths) or a plain number, as float."""
    raw = getattr(value, "Value", value)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise GoliathPostError("%s is %r, not a number" % (what, value))
    return float(raw)


def _stock_of(job: Any) -> Any:
    label = getattr(job, "Label", "?")
    stock = getattr(job, "Stock", None)
    if stock is None:
        raise GoliathPostError("job %r has no stock; give it a box stock the size of the panel" % label)
    source = "stock %r of job %r" % (getattr(stock, "Label", "?"), label)
    shape = getattr(stock, "Shape", None)
    box = getattr(shape, "BoundBox", None)
    if box is None:
        raise GoliathPostError("%s has no shape; recompute the job" % source)
    keys = ("XMin", "YMin", "ZMin", "XMax", "YMax", "ZMax")
    bounds = [_float(getattr(box, k, None), "%s %s" % (source, k)) for k in keys]
    volume = _float(getattr(shape, "Volume", None), "%s volume" % source)
    box_volume = (bounds[3] - bounds[0]) * (bounds[4] - bounds[1]) * (bounds[5] - bounds[2])
    if abs(volume - box_volume) > 1e-6 * max(abs(box_volume), 1.0):
        raise GoliathPostError(
            "%s is not a box (its volume is %.3f mm3, its bounding box %.3f mm3). GI describes the panel as a "
            "box: give the job a box stock the size of the panel" % (source, volume, box_volume)
        )
    return gi_format.StockData(*bounds, source=source)


def _tool_of(tc: Any) -> Any:
    source = "tool controller %r" % getattr(tc, "Label", "?")
    number = getattr(tc, "ToolNumber", None)
    if isinstance(number, bool) or not isinstance(number, int):
        raise GoliathPostError("%s has tool number %r, not a whole number" % (source, number))
    bit = getattr(tc, "Tool", None)
    if bit is None:
        raise GoliathPostError("%s has no tool bit" % source)
    for prop in ("Diameter", "CuttingEdgeHeight"):
        if getattr(bit, prop, None) is None:
            raise GoliathPostError(
                "the tool bit %r of %s has no %s; GI needs the tool's diameter and its flute (cutting edge) "
                "length. Describe the bit with a shape that has both, such as Endmill (FreeCAD's Drill shape "
                "has no cutting edge height)" % (getattr(bit, "Label", "?"), source, prop)
            )
    diameter = _float(bit.Diameter, "%s diameter" % source)
    flute = _float(bit.CuttingEdgeHeight, "%s cutting edge height" % source)
    shape = getattr(bit, "ShapeType", None) or getattr(bit, "ShapeName", None)
    name = getattr(bit, "Label", None)
    return gi_format.ToolData(
        number=number,
        diameter=diameter,
        flute=flute,
        rpm=_spindle_rpm(tc, source),
        kind=gi_format.tool_kind_for(shape),
        name=name if isinstance(name, str) and name else None,
        source=source,
    )


def _spindle_rpm(tc: Any, source: str) -> float:
    """The speed the tool controller's own path starts the spindle at (what FreeCAD itself would post)."""
    commands = getattr(getattr(tc, "Path", None), "Commands", None)
    if commands is None:
        raise GoliathPostError("%s has no path; recompute the job" % source)
    starts = [c for c in commands if gi_format.command_code(getattr(c, "Name", None)) in ("M3", "M4")]
    if any(gi_format.command_code(c.Name) == "M4" for c in starts):
        raise GoliathPostError(
            "%s turns the spindle counter-clockwise (M4); the Goliath spindle turns one way. Set the spindle "
            "direction to Forward" % source
        )
    if not starts:
        raise GoliathPostError(
            "%s starts no spindle (its spindle speed is 0 or its direction is None); set a spindle speed and the "
            "direction Forward" % source
        )
    if len(starts) > 1:
        raise GoliathPostError("%s starts the spindle %d times; expected once" % (source, len(starts)))
    params = getattr(starts[0], "Parameters", None) or {}
    if "S" not in params:
        raise GoliathPostError("%s starts the spindle without a speed (M3 with no S)" % source)
    return _float(params["S"], "%s spindle speed" % source)


def _kind_of(op: Any) -> Tuple[str, Optional[str]]:
    """The GI kind from the operation's proxy class, following a dressup to its base; 'other' with a note."""
    obj = op
    seen = set()
    while obj is not None and id(obj) not in seen:
        seen.add(id(obj))
        proxy = getattr(obj, "Proxy", None)
        if proxy is not None:
            kind = gi_format.op_kind_for(type(proxy).__module__)
            if kind is not None:
                return kind, None
        obj = _single_base(obj)
    proxy = getattr(op, "Proxy", None)
    described = "%s.%s" % (type(proxy).__module__, type(proxy).__name__) if proxy is not None else "object"
    return "other", "%s is a %s, which has no GI kind of its own; written as kind=other" % (_describe(op), described)


def _final_depth(op: Any) -> Optional[float]:
    value = _op_property(op, "FinalDepth")
    return None if value is None else _float(value, "%s final depth" % _describe(op))


def _coolant(op: Any) -> str:
    mode = _op_property(op, "CoolantMode", "None")
    if not isinstance(mode, str):
        raise GoliathPostError("%s has coolant mode %r" % (_describe(op), mode))
    return mode


def _path_commands(op: Any) -> List[Any]:
    """The operation's path commands, with the operation's placement applied as FreeCAD's own posts do."""
    path = getattr(op, "Path", None)
    if path is None:
        raise GoliathPostError("%s has no path; recompute the job" % _describe(op))
    placement = getattr(op, "Placement", None)
    if placement is not None and not placement.isIdentity():
        try:
            from PathScripts import PathUtils  # type: ignore[import-not-found]
        except ImportError as exc:
            raise GoliathPostError(
                "%s has a placement; this post applies it with FreeCAD's PathUtils, which is not available "
                "here" % _describe(op)
            ) from exc
        path = PathUtils.getPathWithPlacement(op)
    commands = getattr(path, "Commands", None)
    if commands is None:
        raise GoliathPostError("%s has a path without commands; recompute the job" % _describe(op))
    return list(commands)
