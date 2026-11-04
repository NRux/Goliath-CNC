# SPDX-License-Identifier: GPL-3.0-or-later
"""Checks of the GI format specification and its conformance examples (unit F03).

``docs/prep/gi-format.md`` defines GI (Goliath-intermediate G-code), the format
CAM post-processors write and ``goliath-prep`` reads. ``docs/prep/gi-examples/``
holds valid and invalid example files and ``manifest.json``, which states the
reader outcome each one must produce. The reader's own suite (unit P02)
executes the examples against the reader.

These repository-level tests need no reader. They check that the document, the
two JSON schemas, the manifest and the example files agree with each other, and
that the cases the design review asked for are present *and truthful*: a GI
header that disagrees with job.toml (red-team PHYS-12), ``ZSHIFT`` recorded but
never applied (PHYS-12), motion after ``M2``/``M30`` and the ``;U``/``;S``
markers (PHYS-17), and the arc end-equals-start rule (PHYS-16). "Truthful"
means the test recomputes the property from the example's text (the header
values against the manifest's job, the arc geometry, the marker comment) instead
of trusting the manifest's description.

The small parsers below exist only to check the examples. They are not a GI
reader: they read well-formed headers and well-formed G-code words and raise on
anything else.

Python 3.9 compatible on purpose: the root suite runs on 3.9 in CI.
"""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Set, Tuple

import pytest

REPO = Path(__file__).resolve().parent.parent
PREP = REPO / "docs" / "prep"
GI_DOC = PREP / "gi-format.md"
GI_SCHEMA = PREP / "gi.schema.json"
EXAMPLES = PREP / "gi-examples"
MANIFEST = EXAMPLES / "manifest.json"
MANIFEST_SCHEMA = EXAMPLES / "manifest.schema.json"
# Files in gi-examples/ that are not examples.
EXAMPLE_DIR_SUPPORT_FILES = frozenset({"manifest.json", "manifest.schema.json", ".gitattributes"})

SPDX_APACHE = "SPDX-License-Identifier: Apache-2.0"
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"

# The header values that must agree with job.toml. Red-team PHYS-12 names
# STOCK (x, y, t), TOWER2, TOOL d, origin and zzero; the GI spec adds the other
# tool values both files declare (flute, stick-out, rpm) as the safest reading.
REQUIRED_CONFLICTS = frozenset(
    {
        "stock.x",
        "stock.y",
        "stock.t",
        "stock.origin",
        "stock.zzero",
        "tower2.axis",
        "tool.d",
        "tool.flute",
        "tool.stickout",
        "tool.rpm",
    }
)

# Annotation keyword -> property of the header model in gi.schema.json.
KEYWORD_TO_MODEL = {
    "SOURCE": "source",
    "UNITS": "units",
    "STOCK": "stock",
    "TOWER2": "tower2",
    "TOOL": "tool",
    "ZSHIFT": "zshift",
    "OP": "op",
}
# Properties of the header model that are not GI keys: where a value came from
# (line, end_line) and the pinned "ZSHIFT is never applied" flag.
MODEL_ONLY_PROPERTIES = frozenset({"line", "end_line", "applied"})
# How a positional value (UNITS mm, ZSHIFT -18.000) appears in the model.
POSITIONAL = "value"

LENGTH_EQUAL = Decimal("0.0005")  # gi-format.md: values agree when they differ by less than this
ARC_AMBIGUOUS = Decimal("0.01")  # 0 < |end - start| < 0.01 mm is refused (PHYS-16)
ARC_RADIUS_TOLERANCE = Decimal("0.005")
SWEEP_TOLERANCE_DEG = 0.001
MOVE_TOLERANCE_MM = Decimal("0.0005")


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def read_all_lines(text: str) -> List[str]:
    """Split like .NET File.ReadAllLines (and goliath_link.gcode.read_all_lines).

    Terminators are CRLF, CR and LF only; a final terminator adds no empty line.
    Copied rather than imported: the root suite does not install goliath-link.
    """
    lines: List[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n" or ch == "\r":
            lines.append(text[start:i])
            if ch == "\r" and i + 1 < n and text[i + 1] == "\n":
                i += 1
            start = i + 1
        i += 1
    if start < n:
        lines.append(text[start:])
    return lines


def example_text(rel: str) -> str:
    """An example's text as a reader sees it: UTF-8, one leading BOM removed."""
    text = (EXAMPLES / rel).read_bytes().decode("utf-8")
    return text[1:] if text.startswith("\ufeff") else text


def example_lines(rel: str) -> List[str]:
    return read_all_lines(example_text(rel))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def manifest() -> List[Dict[str, Any]]:
    data = load_json(MANIFEST)
    assert isinstance(data, list), "manifest.json must be a JSON list (the interface F03 publishes)"
    return data


def entries(expect: Optional[str] = None, finding_id: Optional[str] = None) -> List[Dict[str, Any]]:
    out = []
    for entry in manifest():
        if expect is not None and entry.get("expect") != expect:
            continue
        if finding_id is not None and entry.get("finding_id") != finding_id:
            continue
        out.append(entry)
    return out


def split_comment(line: str) -> Tuple[str, Optional[str]]:
    """(code, comment text after the first ';' or None)."""
    at = line.find(";")
    return (line, None) if at < 0 else (line[:at], line[at + 1 :])


def doc_text() -> str:
    return GI_DOC.read_text(encoding="utf-8")


def doc_section(heading_regex: str) -> str:
    """The text from a heading to the next heading of the same or a higher level."""
    text = doc_text()
    match = re.search(r"^(#+) " + heading_regex[heading_regex.index(" ") + 1 :], text, re.MULTILINE)
    assert match, "gi-format.md has no heading matching %r" % heading_regex
    level = len(match.group(1))
    rest = text[match.end() :]
    following = re.search(r"^#{1,%d} " % level, rest, re.MULTILINE)
    return rest[: following.start()] if following else rest


def doc_findings() -> Dict[str, str]:
    """Finding id -> severity, from the findings table of gi-format.md."""
    rows = re.findall(r"^\| `([A-Z]-[A-Z0-9]+(?:-[A-Z0-9]+)*)` \| (refuse|warn) \|", doc_text(), re.MULTILINE)
    found: Dict[str, str] = {}
    for finding_id, severity in rows:
        assert finding_id not in found, "gi-format.md lists finding %s twice" % finding_id
        found[finding_id] = severity
    return found


# --------------------------------------------------------------------------
# A minimal JSON Schema (2020-12) checker for the keywords these schemas use.
# Every keyword a schema uses must be in SCHEMA_KEYWORDS (checked by
# schema_problems), so nothing in the schemas is silently not checked.
# --------------------------------------------------------------------------

ANNOTATION_KEYWORDS = frozenset({"$schema", "$id", "$comment", "title", "description", "examples"})
SCHEMA_KEYWORDS = ANNOTATION_KEYWORDS | frozenset(
    {
        "$defs",
        "$ref",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "enum",
        "const",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "multipleOf",
        "pattern",
        "minLength",
        "allOf",
        "if",
        "then",
        "else",
    }
)
JSON_TYPES = frozenset({"null", "boolean", "integer", "number", "string", "array", "object"})


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _type_ok(value: Any, json_type: str) -> bool:
    if json_type == "null":
        return value is None
    if json_type == "boolean":
        return isinstance(value, bool)
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "number":
        return _is_number(value)
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "array":
        return isinstance(value, list)
    if json_type == "object":
        return isinstance(value, dict)
    raise AssertionError("unknown JSON type %r" % json_type)


def json_equal(a: Any, b: Any) -> bool:
    """JSON equality: true is not 1, 1 equals 1.0."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_number(a) and _is_number(b):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(json_equal(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def resolve_ref(root: Dict[str, Any], ref: str) -> Dict[str, Any]:
    match = re.fullmatch(r"#/\$defs/([A-Za-z0-9_]+)", ref)
    assert match, "only local '#/$defs/<name>' references are used, got %r" % ref
    defs = root.get("$defs", {})
    assert match.group(1) in defs, "unresolved $ref %r" % ref
    return defs[match.group(1)]


def schema_errors(instance: Any, schema: Dict[str, Any], root: Dict[str, Any], where: str = "$") -> List[str]:
    errors: List[str] = []
    if "$ref" in schema:
        errors += schema_errors(instance, resolve_ref(root, schema["$ref"]), root, where)
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(instance, t) for t in types):
            return errors + ["%s: %r is not of type %s" % (where, instance, "/".join(types))]
    if "const" in schema and not json_equal(instance, schema["const"]):
        errors.append("%s: %r is not %r" % (where, instance, schema["const"]))
    if "enum" in schema and not any(json_equal(instance, e) for e in schema["enum"]):
        errors.append("%s: %r is not one of %r" % (where, instance, schema["enum"]))
    if _is_number(instance):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append("%s: %r < minimum %r" % (where, instance, schema["minimum"]))
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append("%s: %r > maximum %r" % (where, instance, schema["maximum"]))
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            errors.append("%s: %r <= exclusiveMinimum %r" % (where, instance, schema["exclusiveMinimum"]))
        if "multipleOf" in schema and Decimal(str(instance)) % Decimal(str(schema["multipleOf"])) != 0:
            errors.append("%s: %r is not a multiple of %r" % (where, instance, schema["multipleOf"]))
    if isinstance(instance, str):
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append("%s: %r does not match %r" % (where, instance, schema["pattern"]))
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append("%s: %r is shorter than %d" % (where, instance, schema["minLength"]))
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append("%s: fewer than %d items" % (where, schema["minItems"]))
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append("%s: more than %d items" % (where, schema["maxItems"]))
        if "items" in schema:
            for i, item in enumerate(instance):
                errors += schema_errors(item, schema["items"], root, "%s[%d]" % (where, i))
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append("%s: missing required %r" % (where, key))
        properties = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                errors += schema_errors(value, properties[key], root, "%s.%s" % (where, key))
            elif schema.get("additionalProperties") is False:
                errors.append("%s: unexpected property %r" % (where, key))
    for sub in schema.get("allOf", []):
        errors += schema_errors(instance, sub, root, where)
    if "if" in schema:
        if not schema_errors(instance, schema["if"], root, where):
            if "then" in schema:
                errors += schema_errors(instance, schema["then"], root, where)
        elif "else" in schema:
            errors += schema_errors(instance, schema["else"], root, where)
    return errors


def schema_problems(schema: Any, root: Dict[str, Any], where: str = "#") -> List[str]:
    """Structural problems of a schema: unknown keywords, bad refs, open objects."""
    if not isinstance(schema, dict):
        return ["%s: a schema must be a JSON object" % where]
    problems: List[str] = []
    unknown = sorted(set(schema) - SCHEMA_KEYWORDS)
    if unknown:
        problems.append("%s: keywords this checker does not implement (or typos): %s" % (where, unknown))
    if "$ref" in schema:
        try:
            resolve_ref(root, schema["$ref"])
        except AssertionError as exc:
            problems.append("%s: %s" % (where, exc))
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        bad = [t for t in types if t not in JSON_TYPES]
        if bad:
            problems.append("%s: unknown types %s" % (where, bad))
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            problems.append("%s: properties must be an object" % where)
        else:
            for name, sub in properties.items():
                problems += schema_problems(sub, root, "%s/properties/%s" % (where, name))
            if schema.get("type") == "object" and schema.get("additionalProperties") is not False:
                problems.append("%s: an object model must say additionalProperties: false" % where)
    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or len(set(required)) != len(required):
            problems.append("%s: required must be a list of distinct names" % where)
        elif properties is not None:
            missing = [r for r in required if r not in properties]
            if missing:
                problems.append("%s: required names that are not properties: %s" % (where, missing))
    if "enum" in schema:
        values = schema["enum"]
        if not isinstance(values, list) or not values:
            problems.append("%s: enum must be a non-empty list" % where)
        elif any(json_equal(a, b) for i, a in enumerate(values) for b in values[i + 1 :]):
            problems.append("%s: enum has duplicate values" % where)
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except re.error as exc:
            problems.append("%s: pattern does not compile: %s" % (where, exc))
    for key in ("minimum", "maximum", "exclusiveMinimum", "multipleOf"):
        if key in schema and not _is_number(schema[key]):
            problems.append("%s: %s must be a number" % (where, key))
    for key in ("items", "if", "then", "else"):
        if key in schema:
            problems += schema_problems(schema[key], root, "%s/%s" % (where, key))
    for i, sub in enumerate(schema.get("allOf", [])):
        problems += schema_problems(sub, root, "%s/allOf/%d" % (where, i))
    for name, sub in schema.get("$defs", {}).items():
        problems += schema_problems(sub, root, "%s/$defs/%s" % (where, name))
    return problems


def unreferenced_defs(root: Dict[str, Any]) -> Set[str]:
    text = json.dumps({k: v for k, v in root.items() if k != "$defs"}) + json.dumps(root.get("$defs", {}))
    return {name for name in root.get("$defs", {}) if ('"#/$defs/%s"' % name) not in text}


# --------------------------------------------------------------------------
# Header parser for well-formed examples (raises on anything else)
# --------------------------------------------------------------------------

VERSION_LINE = re.compile(r";GOLIATH-GI[ \t]+([1-9][0-9]*)[ \t]*")
ANNOTATION_LINE = re.compile(r";GOLIATH[ \t]+([A-Z][A-Z0-9]*)(.*)")
ITEM = re.compile(r'[ \t]+(?:([a-z][a-z0-9_]*)=)?("(?:[^"\\\x00-\x1f\x7f]|\\["\\])*"|[A-Za-z0-9_.+-]+)')
NUMBER = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?")
INTEGER = re.compile(r"[0-9]+")


class Item(NamedTuple):
    key: Optional[str]
    value: str
    quoted: bool


def parse_items(rest: str) -> List[Item]:
    items: List[Item] = []
    pos = 0
    while rest[pos:].strip(" \t"):
        match = ITEM.match(rest, pos)
        if not match:
            raise ValueError("malformed annotation items at %r" % rest[pos:])
        raw = match.group(2)
        quoted = raw.startswith('"')
        value = re.sub(r"\\([\"\\])", r"\1", raw[1:-1]) if quoted else raw
        items.append(Item(match.group(1), value, quoted))
        pos = match.end()
    return items


def is_annotation_like(comment: Optional[str]) -> bool:
    return comment is not None and comment.lstrip(" \t").upper().startswith("GOLIATH")


def _convert(value: Item, prop: Dict[str, Any], root: Dict[str, Any]) -> Any:
    prop = resolve_ref(root, prop["$ref"]) if "$ref" in prop else prop
    if "enum" in prop and any(isinstance(e, bool) for e in prop["enum"]):
        # through: auto | 1 | 0  ->  "auto" | true | false
        mapping = {"auto": "auto", "1": True, "0": False}
        if value.quoted or value.value not in mapping:
            raise ValueError("bad through value %r" % (value.value,))
        return mapping[value.value]
    json_type = prop.get("type")
    if json_type == "number":
        if value.quoted or not NUMBER.fullmatch(value.value):
            raise ValueError("not a GI number: %r" % (value.value,))
        return float(Decimal(value.value))
    if json_type == "integer":
        if value.quoted or not INTEGER.fullmatch(value.value):
            raise ValueError("not a GI integer: %r" % (value.value,))
        return int(value.value)
    if value.quoted and "enum" in prop:
        raise ValueError("an enumerated value is never quoted: %r" % (value.value,))
    return value.value


def parse_gi_header(text: str) -> Tuple[Dict[str, Any], List[int]]:
    """The header model (gi.schema.json) of a well-formed GI text, and the lines
    of unknown keywords or keys (which a reader warns about)."""
    schema = load_json(GI_SCHEMA)
    lines = read_all_lines(text[1:] if text.startswith("\ufeff") else text)
    if not lines or not VERSION_LINE.fullmatch(lines[0]):
        raise ValueError("line 1 is not a version line")
    model: Dict[str, Any] = {"version": int(VERSION_LINE.fullmatch(lines[0]).group(1)), "ops": []}
    unknown_lines: List[int] = []
    open_op: Optional[Dict[str, Any]] = None
    for number, line in enumerate(lines[1:], start=2):
        code, comment = split_comment(line)
        if not is_annotation_like(comment):
            continue
        match = ANNOTATION_LINE.fullmatch(line.rstrip(" \t"))
        if code or not match:
            raise ValueError("line %d: malformed annotation %r" % (number, line))
        keyword, items = match.group(1), parse_items(match.group(2))
        if keyword == "OPEND":
            if open_op is None or [(i.key, i.value) for i in items] != [("id", open_op.get("id"))]:
                raise ValueError("line %d: OPEND does not close the open OP" % number)
            open_op["end_line"] = number
            open_op = None
            continue
        if keyword not in KEYWORD_TO_MODEL:
            unknown_lines.append(number)
            continue
        definition = schema["$defs"][KEYWORD_TO_MODEL[keyword]]
        props = definition["properties"]
        entry: Dict[str, Any] = {"line": number}
        for item in items:
            name = POSITIONAL if item.key is None else item.key
            if name in MODEL_ONLY_PROPERTIES:
                raise ValueError("line %d: %r is not a GI key" % (number, name))
            if name not in props:
                unknown_lines.append(number)
                continue
            if name in entry:
                raise ValueError("line %d: duplicate %r" % (number, name))
            entry[name] = _convert(item, props[name], schema)
        if keyword == "ZSHIFT":
            entry["applied"] = False
        if keyword == "OP":
            if open_op is not None:
                raise ValueError("line %d: nested OP" % number)
            model["ops"].append(entry)
            open_op = entry
        else:
            if KEYWORD_TO_MODEL[keyword] in model:
                raise ValueError("line %d: duplicate %s" % (number, keyword))
            model[KEYWORD_TO_MODEL[keyword]] = entry
    if open_op is not None:
        raise ValueError("OP %r is never closed" % open_op["id"])
    return model, sorted(set(unknown_lines))


def header_or_none(rel: str) -> Optional[Dict[str, Any]]:
    """The header model if the example's header is well formed and schema-valid."""
    try:
        model, _ = parse_gi_header(example_text(rel))
    except ValueError:
        return None
    schema = load_json(GI_SCHEMA)
    return model if not schema_errors(model, schema, schema) else None


def header_job_differences(model: Dict[str, Any], job: Dict[str, Any]) -> Dict[str, int]:
    """Compared quantity -> GI line, for every header value that disagrees with
    the job (gi-format.md, section "The header against job.toml")."""

    def differ(a: Any, b: Any) -> bool:
        return abs(Decimal(str(a)) - Decimal(str(b))) >= LENGTH_EQUAL

    out: Dict[str, int] = {}
    stock, panel, tool, job_tool = model["stock"], job["panel"], model["tool"], job["tool"]
    for gi_key, job_key in (("x", "W"), ("y", "H"), ("t", "T")):
        if differ(stock[gi_key], panel[job_key]):
            out["stock." + gi_key] = stock["line"]
    placement_zero = not differ(job["placement"]["dx"], 0) and not differ(job["placement"]["dy"], 0)
    if (stock["origin"] == "t1-corner") != placement_zero:
        out["stock.origin"] = stock["line"]
    if stock["zzero"] != job["z_zero"]:
        out["stock.zzero"] = stock["line"]
    if model["tower2"]["axis"] != panel["tower2"]:
        out["tower2.axis"] = model["tower2"]["line"]
    for key in ("d", "flute", "stickout", "rpm"):
        if key in tool and key in job_tool and differ(tool[key], job_tool[key]):
            out["tool." + key] = tool["line"]
    if "kind" in tool and "kind" in job_tool and tool["kind"] != job_tool["kind"]:
        out["tool.kind"] = tool["line"]
    return out


# --------------------------------------------------------------------------
# G-code words and a modal position tracker for well-formed examples
# --------------------------------------------------------------------------

WORDS_LINE = re.compile(r"(?:[ \t]*[A-Z][+-]?[0-9]+(?:\.[0-9]+)?)*[ \t]*")
WORD = re.compile(r"([A-Z])([+-]?[0-9]+(?:\.[0-9]+)?)")
Point = Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]


def words(code: str) -> List[Tuple[str, Decimal]]:
    if not WORDS_LINE.fullmatch(code):
        raise ValueError("not a well-formed word line: %r" % code)
    return [(letter, Decimal(value)) for letter, value in WORD.findall(code)]


class Motion(NamedTuple):
    line: int
    mode: int  # 0, 1, 2 or 3
    start: Point
    end: Point
    centre: Optional[Tuple[Decimal, Decimal]]


def trace(lines: List[str]) -> Iterator[Motion]:
    """Motions of a well-formed program, with start and end (None = unknown)."""
    mode: Optional[int] = None
    pos: Point = (None, None, None)
    for number, line in enumerate(lines, start=1):
        code, _ = split_comment(line)
        ws = words(code)
        for letter, value in ws:
            if letter == "G" and value in (0, 1, 2, 3):
                mode = int(value)
        axes = {letter: value for letter, value in ws if letter in "XYZIJ"}
        if not axes:
            continue
        if mode is None:
            raise ValueError("line %d: motion with no motion mode" % number)
        end = (axes.get("X", pos[0]), axes.get("Y", pos[1]), axes.get("Z", pos[2]))
        centre = None
        if mode in (2, 3):
            if pos[0] is None or pos[1] is None:
                raise ValueError("line %d: arc from an unknown position" % number)
            centre = (pos[0] + axes.get("I", Decimal(0)), pos[1] + axes.get("J", Decimal(0)))
        yield Motion(number, mode, pos, end, centre)
        pos = end


def xy_distance(a: Point, b: Point) -> Decimal:
    dx, dy = a[0] - b[0], a[1] - b[1]
    return (dx * dx + dy * dy).sqrt()


def sweep_deg(m: Motion) -> float:
    """Swept angle of an arc in degrees (unsigned); end == start is a full circle."""
    if m.end[0] == m.start[0] and m.end[1] == m.start[1]:
        return 360.0
    cx, cy = float(m.centre[0]), float(m.centre[1])
    a0 = math.atan2(float(m.start[1]) - cy, float(m.start[0]) - cx)
    a1 = math.atan2(float(m.end[1]) - cy, float(m.end[0]) - cx)
    ccw = (a1 - a0) % (2 * math.pi)
    return math.degrees(ccw if m.mode == 3 else (2 * math.pi - ccw) % (2 * math.pi))


def radius_mismatch(m: Motion) -> Decimal:
    r0 = xy_distance(m.start, (m.centre[0], m.centre[1], None))
    r1 = xy_distance(m.end, (m.centre[0], m.centre[1], None))
    return abs(r0 - r1)


def end_of_program_line(lines: List[str]) -> Optional[Tuple[int, int]]:
    """(line, M code) of the first M2 or M30, or None."""
    for number, line in enumerate(lines, start=1):
        code, _ = split_comment(line)
        for letter, value in words(code):
            if letter == "M" and value in (2, 30):
                return number, int(value)
    return None


def is_marker(comment: Optional[str]) -> bool:
    return comment is not None and comment.lstrip(" \t")[:1] in ("U", "u", "S", "s")


# --------------------------------------------------------------------------
# The document and the schemas
# --------------------------------------------------------------------------


def test_doc_carries_spdx_header_and_cites_only_existing_repository_files() -> None:
    text = doc_text()
    assert text.startswith("<!-- %s -->" % SPDX_APACHE), "gi-format.md must start with its SPDX comment"
    broken = []
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
        if re.match(r"[a-z]+:", target):
            continue
        if not (GI_DOC.parent / target).resolve().exists():
            broken.append(target)
    assert not broken, "gi-format.md links files that do not exist: %s" % broken


@pytest.mark.parametrize("path", [GI_SCHEMA, MANIFEST_SCHEMA], ids=["gi.schema.json", "manifest.schema.json"])
def test_schema_well_formed(path: Path) -> None:
    schema = load_json(path)
    assert schema.get("$schema") == DRAFT_2020_12
    assert isinstance(schema.get("$id"), str) and schema["$id"].startswith("urn:goliath-cnc:")
    assert isinstance(schema.get("title"), str) and isinstance(schema.get("description"), str)
    assert str(schema.get("$comment", "")).startswith(SPDX_APACHE), "%s must carry its SPDX line" % path.name
    problems = schema_problems(schema, schema)
    assert not problems, "\n".join(problems)
    assert not unreferenced_defs(schema), "unused $defs in %s: %s" % (path.name, unreferenced_defs(schema))
    # "default" is not in SCHEMA_KEYWORDS, so schema_problems already refuses it:
    # every value is stated or absent, never defaulted.


def test_schema_checker_rejects_what_it_should() -> None:
    """The mini checker is used to validate the manifest and the headers; it must
    actually fail on bad input, or the other tests prove nothing."""
    schema = load_json(GI_SCHEMA)
    good, _ = parse_gi_header(example_text("valid/single-op-profile.gi"))
    assert schema_errors(good, schema, schema) == []
    for mutate in (
        lambda m: m["stock"].__setitem__("t", -1.0),
        lambda m: m["stock"].__setitem__("origin", "centre"),
        lambda m: m["tower2"].__setitem__("axis", "Z"),
        lambda m: m["tool"].pop("d"),
        lambda m: m["tool"].__setitem__("colour", "red"),
        lambda m: m["ops"][0].__setitem__("c", 45),
        lambda m: m["ops"][0].__setitem__("through", 1),
        lambda m: m["zshift"].__setitem__("applied", True),
        lambda m: m.__setitem__("version", 2),
    ):
        bad = json.loads(json.dumps(good))
        mutate(bad)
        assert schema_errors(bad, schema, schema), "the checker accepted %r" % bad
    manifest_schema = load_json(MANIFEST_SCHEMA)
    entry = dict(entries(expect="refused")[0])
    entry.pop("line")
    assert schema_errors([entry], manifest_schema, manifest_schema), "a refused entry without a line must fail"


def test_schema_keys_match_doc_key_tables() -> None:
    """Every key of every annotation table in gi-format.md is a property of the
    header model, and every model property (except provenance) is a documented key."""
    text = doc_text()
    schema = load_json(GI_SCHEMA)
    sections = re.split(r"^### ", text, flags=re.MULTILINE)
    documented: Dict[str, Set[str]] = {}
    for section in sections:
        heading = re.match(r"`([A-Z][A-Z0-9]*)`", section)
        if not heading:
            continue
        keys = set(re.findall(r"^\| `([a-z][a-z0-9_]*)` \|", section, re.MULTILINE))
        if re.search(r"^\| \*\(positional\)\* \|", section, re.MULTILINE):
            keys.add(POSITIONAL)
        documented[heading.group(1)] = keys
    assert set(documented) >= set(KEYWORD_TO_MODEL) | {"OPEND"}, "gi-format.md lacks a key table for %s" % (
        sorted(set(KEYWORD_TO_MODEL) | {"OPEND"} - set(documented))
    )
    assert documented["OPEND"] == {"id"}
    for keyword, model_name in KEYWORD_TO_MODEL.items():
        props = set(schema["$defs"][model_name]["properties"]) - MODEL_ONLY_PROPERTIES
        assert documented[keyword] == props, "%s: doc keys %s, schema keys %s" % (
            keyword,
            sorted(documented[keyword]),
            sorted(props),
        )


def test_doc_findings_cover_every_manifest_finding_and_red_team_rule() -> None:
    findings = doc_findings()
    for required in ("D-GI-VERSION", "D-GI-CONFLICT", "D-AFTER-END", "D-MARKER", "D-ARC", "N-GI-UNKNOWN-KEY"):
        assert required in findings, "gi-format.md's findings table lacks %s" % required
    assert findings["N-GI-UNKNOWN-KEY"] == "warn", "unknown keys are warned, not refused"
    used = {e["finding_id"] for e in manifest() if "finding_id" in e}
    assert used <= set(findings), "manifest uses undocumented findings: %s" % sorted(used - set(findings))
    # Each red-team rule is stated in the section that owns it, not just somewhere.
    for heading, phrases in (
        (r"### `ZSHIFT`", ("already applied by the post; informational", "never applies")),  # PHYS-12
        (r"## 10\. The header against job\.toml", ("naming both", "D-GI-CONFLICT")),  # PHYS-12
        (r"### 8\.7 Arcs", ("full circle", "0 < |end − start| < 0.01 mm", "D-ARC")),  # PHYS-16
        (r"### 8\.8 Program end", ("D-AFTER-END", "motion")),  # PHYS-17
        (r"### 3\.4 The in-stream markers", ("D-MARKER", "dialect verify mode")),  # PHYS-17
        (r"## 5\. The version line and versioning", ("unknown version is refused", "warns")),
    ):
        section = doc_section(heading)
        for phrase in phrases:
            assert phrase in section, "gi-format.md section %r must state %r" % (heading, phrase)


def test_doc_line_numbers_are_gi_lines_not_robot_frame_lines() -> None:
    """A GI file is never sent to the robot: goliath-prep emits its own program,
    and the robot's position frames number *that* program's lines. Section 3.2
    must not tell a reader that a GI line number is the robot's line number; it
    must point at the line map that leads from one to the other."""
    section = doc_section(r"### 3\.2 Lines")
    assert not re.search(r"same line in a finding, the report and the robot", section), (
        "section 3.2 claims a GI line number is the line in the robot's position frames"
    )
    assert "line map" in section, "section 3.2 must say how a robot line leads back to a GI line (the line map)"


def test_doc_forced_heading_is_never_pooled_or_ignored() -> None:
    """spec 08 (Which cuts are evaluated together) pools non-through pockets and
    grouped cuts and ignores their forced angles. GI's c= cites the vendor's
    forced-angle semantics, so it must also say that goliath-prep never ignores it,
    or a reader could drop a pocket's forced heading without a word."""
    section = doc_section(r"## 7\. Operations")
    for phrase in ("`through=0`", "never pooled", "never ignored"):
        assert phrase in section, "section 7 must state %r for the forced heading c" % phrase


def test_doc_defines_word_order_on_a_motion_line() -> None:
    """The model a reader builds needs the spindle state, speed and feed of each
    move. 'G1 Z-5 F200 M3' is legal GI, so section 8.3 must say whether M3, S and F
    act before or after the line's motion (RS274/NGC: before)."""
    section = doc_section(r"### 8\.3 One line")
    for phrase in ("RS274/NGC", "before the motion"):
        assert phrase in section, "section 8.3 must state %r" % phrase


def test_doc_agreement_rule_names_the_bound_value() -> None:
    """Section 10: when both sides declare a value and they agree (within 0.0005),
    the document must say which of the two is bound, so no reader picks one
    silently; and its worked numbers must match the examples on both sides of
    the limit."""
    section = doc_section(r"## 10\. The header against job\.toml")
    assert "the job.toml value is the one bound" in section
    for phrase in ("18.0004", "18.0005"):
        assert phrase in section, "section 10 must show %s (the examples on each side of the limit)" % phrase
    assert "anything else does not" not in section, "the 'three decimals' shorthand is not the rule"


# --------------------------------------------------------------------------
# The manifest
# --------------------------------------------------------------------------


def example_files_on_disk() -> List[str]:
    out = []
    for path in sorted(EXAMPLES.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(EXAMPLES).as_posix()
        if rel in EXAMPLE_DIR_SUPPORT_FILES:
            continue
        out.append(rel)
    return out


def test_examples_manifest_complete() -> None:
    schema = load_json(MANIFEST_SCHEMA)
    data = manifest()
    errors = schema_errors(data, schema, schema)
    assert not errors, "manifest.json does not match manifest.schema.json:\n" + "\n".join(errors)

    listed = [e["file"] for e in data]
    duplicates = sorted({f for f in listed if listed.count(f) > 1})
    assert not duplicates, "files listed more than once: %s" % duplicates
    on_disk = example_files_on_disk()
    stray = [f for f in on_disk if not re.fullmatch(r"(valid|invalid)/[^/]+\.gi", f)]
    assert not stray, "gi-examples/ holds files that are neither examples nor support files: %s" % stray
    assert sorted(listed) == on_disk, "manifest and disk differ: only listed %s, only on disk %s" % (
        sorted(set(listed) - set(on_disk)),
        sorted(set(on_disk) - set(listed)),
    )

    findings = doc_findings()
    for entry in data:
        rel = entry["file"]
        folder = rel.split("/", 1)[0]
        assert (folder == "valid") == (entry["expect"] == "ok"), "%s: valid/ means ok, invalid/ means refused" % rel
        if "finding_id" in entry:
            want = "refuse" if entry["expect"] == "refused" else "warn"
            assert findings[entry["finding_id"]] == want, "%s: %s is a %s finding, not %s" % (
                rel,
                entry["finding_id"],
                findings[entry["finding_id"]],
                want,
            )
            lines = example_lines(rel)
            assert 1 <= entry["line"] <= len(lines), "%s: line %d is outside the file" % (rel, entry["line"])
            assert lines[entry["line"] - 1].strip(" \t\ufeff"), "%s: line %d is blank" % (rel, entry["line"])
        for move in entry.get("assert_moves", []):
            lines = example_lines(rel)
            assert 1 <= move["line"] <= len(lines)
            assert split_comment(lines[move["line"] - 1])[0].strip(), "%s: line %d has no G-code" % (rel, move["line"])


def test_examples_cover_both_outcomes_and_every_refusal_family() -> None:
    assert len(entries(expect="ok")) >= 10 and len(entries(expect="refused")) >= 40
    findings = doc_findings()
    used = {e.get("finding_id") for e in manifest()}
    unexercised = sorted(f for f in findings if f not in used)
    assert not unexercised, "documented findings no example exercises: %s" % unexercised


def test_valid_example_headers_match_schema_and_agree_with_their_job() -> None:
    schema = load_json(GI_SCHEMA)
    for entry in entries(expect="ok"):
        model, unknown_lines = parse_gi_header(example_text(entry["file"]))
        errors = schema_errors(model, schema, schema)
        assert not errors, "%s: header model invalid:\n%s" % (entry["file"], "\n".join(errors))
        assert header_job_differences(model, entry["job"]) == {}, "%s disagrees with its job" % entry["file"]
        if entry.get("finding_id") == "N-GI-UNKNOWN-KEY":
            assert entry["line"] in unknown_lines, "%s: no unknown key on line %d" % (entry["file"], entry["line"])
        else:
            assert not unknown_lines, "%s: unknown keys on lines %s but no warning expected" % (
                entry["file"],
                unknown_lines,
            )


def test_valid_examples_are_well_formed_programs() -> None:
    """Valid examples contain no defect the rules refuse: arcs, end, markers, ASCII."""
    for entry in entries(expect="ok"):
        lines = example_lines(entry["file"])
        motions = list(trace(lines))
        for m in motions:
            if m.mode in (2, 3):
                distance = xy_distance(m.start, m.end)
                assert distance == 0 or distance >= ARC_AMBIGUOUS, "%s line %d" % (entry["file"], m.line)
                assert radius_mismatch(m) <= ARC_RADIUS_TOLERANCE, "%s line %d" % (entry["file"], m.line)
        end = end_of_program_line(lines)
        if end is not None:
            for line in lines[end[0] :]:
                code, comment = split_comment(line)
                assert not code.strip() and not is_annotation_like(comment), "%s: content after M%d" % (
                    entry["file"],
                    end[1],
                )
        for number, line in enumerate(lines, start=1):
            code, comment = split_comment(line)
            assert not is_marker(comment), "%s line %d: a marker" % (entry["file"], number)
            assert code.isascii(), "%s line %d: non-ASCII outside a comment" % (entry["file"], number)


def test_assert_moves_match_the_example_text() -> None:
    """Every asserted end point and sweep is what the file says (Z as written)."""
    checked = 0
    for entry in entries():
        if "assert_moves" not in entry:
            continue
        motions = {m.line: m for m in trace(example_lines(entry["file"]))}
        for move in entry["assert_moves"]:
            m = motions[move["line"]]
            for got, want in zip(m.end, move["end"]):
                assert got is not None and abs(got - Decimal(str(want))) < MOVE_TOLERANCE_MM, (entry["file"], move)
            if "sweep_deg" in move:
                assert m.mode in (2, 3)
                assert abs(sweep_deg(m) - move["sweep_deg"]) < SWEEP_TOLERANCE_DEG, (entry["file"], sweep_deg(m))
            checked += 1
    assert checked >= 5


# --------------------------------------------------------------------------
# The red-team cases are present and truthful
# --------------------------------------------------------------------------


def conflict_coverage() -> Dict[str, List[str]]:
    covered: Dict[str, List[str]] = {}
    for entry in entries(expect="refused", finding_id="D-GI-CONFLICT"):
        model = header_or_none(entry["file"])
        assert model is not None, "%s: a conflict example needs an otherwise valid header" % entry["file"]
        diffs = header_job_differences(model, entry["job"])
        assert len(diffs) == 1, "%s: expected exactly one disagreement, got %s" % (entry["file"], diffs)
        ((quantity, line),) = diffs.items()
        assert entry["line"] == line, "%s: the disagreement is on line %d, not %d" % (
            entry["file"],
            line,
            entry["line"],
        )
        covered.setdefault(quantity, []).append(entry["file"])
    return covered


def test_conflict_examples_differ_from_their_job_in_exactly_one_value() -> None:
    covered = conflict_coverage()
    missing = sorted(REQUIRED_CONFLICTS - set(covered))
    assert not missing, "no conflict example for: %s" % missing
    # Every other example agrees with its job wherever its header is readable,
    # so a non-conflict refusal is not also a hidden conflict.
    for entry in manifest():
        if entry.get("finding_id") == "D-GI-CONFLICT":
            continue
        model = header_or_none(entry["file"])
        if model is not None:
            assert header_job_differences(model, entry["job"]) == {}, "%s: hidden conflict" % entry["file"]


def test_rt27_combined_numbers_are_used() -> None:
    """RT27 of the red-team suite: GI TOWER2 axis=X and STOCK t=18 against a job
    with tower 2 along Y and T = 17.6."""
    found = {"stock.t": False, "tower2.axis": False}
    for entry in entries(expect="refused", finding_id="D-GI-CONFLICT"):
        model = header_or_none(entry["file"])
        diffs = header_job_differences(model, entry["job"])
        if "stock.t" in diffs and model["stock"]["t"] == 18.0 and entry["job"]["panel"]["T"] == 17.6:
            found["stock.t"] = True
        if "tower2.axis" in diffs and model["tower2"]["axis"] == "X" and entry["job"]["panel"]["tower2"] == "Y":
            found["tower2.axis"] = True
    assert all(found.values()), "RT27 numbers missing: %s" % found


def marker_cases() -> Dict[str, str]:
    """Marker letter (U or S, as written) -> example, for D-MARKER examples."""
    out: Dict[str, str] = {}
    for entry in entries(expect="refused", finding_id="D-MARKER"):
        _, comment = split_comment(example_lines(entry["file"])[entry["line"] - 1])
        assert is_marker(comment), "%s line %d is not a marker comment" % (entry["file"], entry["line"])
        out.setdefault(comment.lstrip(" \t")[0], entry["file"])
    return out


def end_cases() -> Dict[int, str]:
    """M code (2 or 30) -> example whose reported line is motion after it."""
    out: Dict[int, str] = {}
    for entry in entries(expect="refused", finding_id="D-AFTER-END"):
        lines = example_lines(entry["file"])
        end = end_of_program_line(lines)
        assert end is not None and end[0] <= entry["line"], "%s: no M2/M30 at or before line %d" % (
            entry["file"],
            entry["line"],
        )
        if end[0] < entry["line"] and any(m.line == entry["line"] for m in trace(lines)):
            out.setdefault(end[1], entry["file"])
    return out


def arc_cases() -> Dict[str, str]:
    """'full-circle', 'boundary' (|end-start| == 0.01, accepted) and 'ambiguous'."""
    out: Dict[str, str] = {}
    for entry in entries():
        lines = example_lines(entry["file"])
        try:
            motions = {m.line: m for m in trace(lines)}
        except ValueError:
            continue
        sweeps = {mv["line"]: mv.get("sweep_deg") for mv in entry.get("assert_moves", [])}
        for m in motions.values():
            if m.mode not in (2, 3):
                continue
            distance = xy_distance(m.start, m.end)
            if entry["expect"] == "ok" and distance == 0 and sweeps.get(m.line) == 360:
                out.setdefault("full-circle", entry["file"])
            if entry["expect"] == "ok" and distance == ARC_AMBIGUOUS and m.line in sweeps:
                out.setdefault("boundary", entry["file"])
            if (
                entry.get("finding_id") == "D-ARC"
                and entry["line"] == m.line
                and 0 < distance < ARC_AMBIGUOUS
                and radius_mismatch(m) <= ARC_RADIUS_TOLERANCE
            ):
                out.setdefault("ambiguous", entry["file"])
    return out


def test_examples_include_conflict_marker_m30_and_arc_cases() -> None:
    """The cases the design review requires (PHYS-12, PHYS-16, PHYS-17) and the
    versioning rules (unknown version refused, unknown key warned) are present."""
    assert REQUIRED_CONFLICTS <= set(conflict_coverage())
    assert set(marker_cases()) >= {"U", "S"}, "need a ;U and a ;S marker example, got %s" % marker_cases()
    assert set(end_cases()) >= {2, 30}, "need motion after M2 and after M30, got %s" % end_cases()
    assert set(arc_cases()) >= {"full-circle", "boundary", "ambiguous"}, arc_cases()

    unknown_version = [
        e
        for e in entries(expect="refused", finding_id="D-GI-VERSION")
        if VERSION_LINE.fullmatch(example_lines(e["file"])[0])
        and e["line"] == 1
        and VERSION_LINE.fullmatch(example_lines(e["file"])[0]).group(1) != "1"
    ]
    assert unknown_version, "need a well-formed version line with an unknown version"
    assert entries(expect="ok", finding_id="N-GI-UNKNOWN-KEY"), "need an unknown key that is warned, not refused"

    zshift = []
    for entry in entries(expect="ok"):
        model, _ = parse_gi_header(example_text(entry["file"]))
        if "zshift" not in model or model["zshift"]["value"] == 0 or "assert_moves" not in entry:
            continue
        lines = example_lines(entry["file"])
        for move in entry["assert_moves"]:
            written = dict(words(split_comment(lines[move["line"] - 1])[0])).get("Z")
            if written is not None and Decimal(str(move["end"][2])) == written:
                zshift.append(entry["file"])
    assert zshift, "need a ZSHIFT example asserting that Z is read as written (never shifted again)"


# --------------------------------------------------------------------------
# Bytes on disk
# --------------------------------------------------------------------------


def test_example_bytes_encoding_and_line_endings() -> None:
    """UTF-8 everywhere; non-ASCII outside comments only where a D-SYNTAX example
    says so; the line-ending examples keep their bytes (gi-examples/.gitattributes)."""
    for entry in manifest():
        rel = entry["file"]
        data = (EXAMPLES / rel).read_bytes()
        text = data.decode("utf-8")  # raises if not UTF-8
        assert not text.startswith("\ufeff") or rel == "valid/bom-utf8.gi", "%s: unexpected BOM" % rel
        if rel == "valid/line-endings-crlf.gi":
            assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
        elif rel == "valid/line-endings-cr.gi":
            assert b"\r" in data and b"\n" not in data
        else:
            assert b"\r" not in data, "%s: CR bytes (git converted line endings?)" % rel
        assert text.rstrip("\r\n").splitlines()[-1] == "; Licence: %s" % SPDX_APACHE, (
            "%s: the last line must be the SPDX comment" % rel
        )
        for number, line in enumerate(read_all_lines(text[1:] if text.startswith("\ufeff") else text), start=1):
            code, _ = split_comment(line)
            if not code.isascii():
                assert entry.get("finding_id") == "D-SYNTAX" and entry["line"] == number, (
                    "%s line %d: non-ASCII outside a comment" % (rel, number)
                )
    attributes = (EXAMPLES / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"^\*\.gi\s+-text\b", attributes, re.MULTILINE), "examples must be stored byte for byte"


# --------------------------------------------------------------------------
# Executed by the reader (unit P02)
# --------------------------------------------------------------------------

READER_PACKAGE = REPO / "goliath-prep" / "src" / "goliath_prep" / "gcode"


def test_gi_conformance_examples() -> None:
    """The reader executes every manifest entry (gi-format.md, "Conformance").

    The reader is unit P02 in goliath-prep, which runs in its own suite with its
    own dependencies; the root suite does not install it. Until it exists this
    test skips, saying so. Once it exists, this test fails unless the reader's
    suite has a test_gi_conformance_examples that loads this manifest, so the
    examples cannot silently go unexecuted.
    """
    if not READER_PACKAGE.is_dir():
        pytest.skip(
            "the GI reader goliath_prep.gcode (unit P02) does not exist yet; P02's own suite "
            "executes docs/prep/gi-examples/manifest.json against it"
        )
    runners = []
    tests_dir = REPO / "goliath-prep" / "tests"
    for path in sorted(tests_dir.rglob("*.py")) if tests_dir.is_dir() else []:
        text = path.read_text(encoding="utf-8")
        if "def test_gi_conformance_examples" in text and "gi-examples" in text and "manifest.json" in text:
            runners.append(path)
    assert runners, (
        "goliath_prep.gcode exists, but no goliath-prep test named test_gi_conformance_examples "
        "loads docs/prep/gi-examples/manifest.json"
    )
