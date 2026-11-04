# SPDX-License-Identifier: GPL-3.0-or-later
"""Checks of the normative safety model and parameter catalogue (unit F02).

``docs/prep/safety-model.md`` is the normative text of the checks that
``goliath-prep`` runs before anything reaches the robot; ``docs/prep/checks.json``
lists every check id with its violation condition, severity, scope, slack and
witness fields; ``docs/prep/params.json`` lists every parameter a check uses with
its default, unit and provenance, and ``docs/prep/parameters.md`` is that table
for people. All four are Apache-2.0 (LICENSING.md), so another sender can
implement the same checks.

These tests need no goliath-prep. They check that the four files agree with
each other, with the GI format's findings (``docs/prep/gi-format.md`` section 12),
with field guide 04's measurement ids, and with the protocol specification they
cite, and that the numbers the files derive are recomputed here rather than
trusted. The matching test against goliath-prep's id constants is
``goliath-prep/tests/test_prep_doc_ids.py`` (it needs Python >= 3.11).

Python 3.9 compatible on purpose: the root suite runs on 3.9 in CI.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Set, Tuple

import pytest

REPO = Path(__file__).resolve().parent.parent
PREP = REPO / "docs" / "prep"
PROTOCOL = REPO / "docs" / "protocol"
SAFETY_MODEL = PREP / "safety-model.md"
PARAMETERS_MD = PREP / "parameters.md"
CHECKS_JSON = PREP / "checks.json"
PARAMS_JSON = PREP / "params.json"
GI_DOC = PREP / "gi-format.md"
FIELDS_JSON = REPO / "docs" / "field-guide" / "04-measure-your-robot.fields.json"
PAR = REPO / "docs" / "research" / "prior-art-review.md"

APACHE_PROSE_HEADER = "<!-- SPDX-License-Identifier: Apache-2.0 -->"

# --------------------------------------------------------------------------
# The vocabularies the documents define (safety-model.md section 11)
# --------------------------------------------------------------------------

FAMILIES = {
    "W": "wheel",
    "B": "body",
    "M": "material and structure",
    "T": "tool",
    "D": "dialect and program",
    "P": "panel geometry",
    "F": "firmware",
    "G": "feature gate",
    "S": "series",
    "N": "notices",
}
ID_RE = re.compile(r"^[WBMTDPFGSN]-[A-Z0-9]+(?:-[A-Z0-9]+)*$")
# An id-like token in prose: a family letter, a hyphen, an upper-case word.
PROSE_ID_RE = re.compile(r"`([WBMTDPFGSN]-[A-Z0-9]+(?:-[A-Z0-9]+)*)`")

SEVERITIES = {"refuse", "warn", "disclose", "internal-error"}
SCOPES = {
    "source-line",
    "piece",
    "in-material-piece",
    "link",
    "tool-point",
    "program-end",
    "cut",
    "tool",
    "program",
    "reparsed-bytes",
    "panel",
    "job-time",
    "series",
    "startup",
}
# Scopes whose checks are geometric: they must give a slack in mm and a witness
# with the piece, the closest point and the slack.
GEOMETRIC_SCOPES = {"piece", "in-material-piece", "link", "tool-point", "program-end"}
SLACK_UNITS = {"mm", "byte"}
WITNESS_FIELDS = {
    "piece",
    "cut",
    "src",
    "column",
    "wheel",
    "point",
    "hazard",
    "slack_mm",
    "raw_mm",
    "margins",
    "heading_c",
    "rule",
    "tower2_placement",
    "gate",
    "unlock_rung",
    "value",
    "limit",
    "sources",
    "island",
    "fact",
}
CHECK_KEYS = {"id", "family", "severity", "applies_to", "condition", "slack", "witness_fields", "defined_in"}

# The check catalogue: the project design's check table (section 4.9), the GI
# reader's findings (gi-format.md section 12) and the two internal-error
# assertions the design names without an id (heading selection's vendor-floor
# assertion, section 4.11; the piece-length invariant, section 4.7.3).
DESIGN_CHECK_IDS = {
    "W-AREA", "W-TOWER", "W-SURF", "M-ATTACH", "M-TOWERSEP", "B-TOWER", "W-SAFEZONE", "W-PARK",
    "W-ROT", "W-RAPID", "W-FIRST", "T-AREA", "T-TOWER", "T-DEPTH", "T-ATTACH", "T-TOOL",
    "W-OBST", "B-OBST", "T-OBST", "T-PASS", "D-ZSAFE", "D-SPINDLE", "D-CSTEP", "D-FEED",
    "D-WORDS", "D-ROUNDTRIP", "D-SIZE", "D-SINGLETOOL", "P-RANGE", "P-REACH", "P-GEOM",
    "F-GATE", "G-GATE", "S-SERIES", "N-DUST", "N-VENDORC", "N-INFERRED-OPS", "N-BASELINE",
}  # fmt: skip
READER_IDS = {
    "D-GI-VERSION", "D-GI-SYNTAX", "D-GI-HEADER", "D-GI-OP", "D-GI-CONFLICT", "N-GI-UNKNOWN-KEY",
    "D-SYNTAX", "D-UNSUPPORTED", "D-UNITS", "D-RANGE", "D-ARC", "D-AFTER-END", "D-MARKER",
    "D-SINGLETOOL", "G-GATE",
}  # fmt: skip
ASSERTION_IDS = {"W-VENDORFLOOR", "M-INVARIANT"}
# A warning the design describes without an id (section 4.4: operation blocks
# that disagree with the segmentation by Z, a through flag the geometry
# contradicts; gi-format.md section 7 calls it "a warning of a later stage").
IMPLIED_IDS = {"N-OPS-MISMATCH"}

PROVENANCES = {"SPEC", "DEFAULT_UNMEASURED", "INFERRED", "POLICY", "GATE", "DERIVED", "OWNER_JOB"}
PARAM_UNITS = {"mm", "deg", "mm/min", "rpm", "byte", "dm3", "percent", "min", "enum"}
PARAM_KEYS = {"name", "unit", "default", "provenance", "source_citation", "overridable", "unlock_rung"}
PARAM_OPTIONAL_KEYS = {
    "symbol", "formula", "measured_by", "measured_rule", "unknown", "note", "choices", "after_unlock", "required",
}  # fmt: skip
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
RUNG_RE = re.compile(r"^R\d+[a-z]?$")
UNKNOWN_RE = re.compile(r"^U-\d{2}$")

# Where a citation must point. Every spec citation resolves to a heading of a
# file in docs/protocol; the others to numbered sections of in-repository docs.
SPEC_CITE_RE = re.compile(r"\bspec (0[1-9]|README)\b(?: §([^;,)\]\n]+))?")
NUMBERED_CITES = (
    ("prior-art review", re.compile(r"\bprior-art review §(\d+(?:\.\d+)?)"), PAR),
    ("gi-format", re.compile(r"\bgi-format §(\d+(?:\.\d+)?)"), GI_DOC),
    ("safety-model", re.compile(r"\bsafety-model §(\d+(?:\.\d+)?)"), SAFETY_MODEL),
    ("parameters", re.compile(r"\bparameters §(\d+(?:\.\d+)?)"), PARAMETERS_MD),
)
# Citations of places a reader of the repository cannot open, or of code lines
# that move with every edit. The spec in docs/protocol is the only spec cited.
FORBIDDEN = (
    ("build-session scratchpad", re.compile(r"scratchpad", re.IGNORECASE)),
    ("scratchpad brief", re.compile(r"fw-facts-brief|\bbrief\s*§", re.IGNORECASE)),
    ("scratchpad design file", re.compile(r"\b(?:design|work-units)-final\b", re.IGNORECASE)),
    ("scratchpad spec worktree", re.compile(r"wt-docs", re.IGNORECASE)),
    ("temporary directory", re.compile(r"/tmp/|\btmp[/\\]claude-", re.IGNORECASE)),
    ("git worktree", re.compile(r"\.claude[/\\]worktrees", re.IGNORECASE)),
    ("code line citation", re.compile(r"\b[\w/.-]+\.py:\d+")),
    ("PAR short form", re.compile(r"\bPAR §")),
)
# The four files of this unit.
UNIT_FILES = (SAFETY_MODEL, PARAMETERS_MD, CHECKS_JSON, PARAMS_JSON)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _checks() -> List[Dict[str, Any]]:
    return _load(CHECKS_JSON)


def _params() -> List[Dict[str, Any]]:
    return _load(PARAMS_JSON)


def _params_by_name() -> Dict[str, Dict[str, Any]]:
    return {p["name"]: p for p in _params()}


def _fields() -> Dict[str, Dict[str, Any]]:
    return {f["id"]: f for f in _load(FIELDS_JSON)["fields"]}


class Heading(NamedTuple):
    level: int
    text: str  # Markdown stripped
    slug: str
    line_no: int


def strip_markdown(text: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return text.replace("`", "").replace("**", "").replace("*", "").strip()


def github_slug(text: str) -> str:
    """The anchor GitHub gives a heading (before duplicate numbering)."""
    text = strip_markdown(text).lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def headings(path: Path) -> List[Heading]:
    found: List[Heading] = []
    seen: Dict[str, int] = {}
    fence = False
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        match = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if not match:
            continue
        base = github_slug(match.group(2))
        slug = base if base not in seen else "%s-%d" % (base, seen[base])
        seen[base] = seen.get(base, 0) + 1
        found.append(Heading(len(match.group(1)), strip_markdown(match.group(2)), slug, line_no))
    return found


def section_text(path: Path, heading: Heading) -> str:
    """The text under a heading, up to the next heading of the same or a higher level."""
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for line_no, line in enumerate(lines, start=1):
        if line_no <= heading.line_no:
            continue
        match = re.match(r"^(#{1,6})\s", line)
        if match and len(match.group(1)) <= heading.level:
            # A heading inside a code fence does not end the section; the
            # sections cited here contain no fenced headings, so a plain
            # check is enough and is asserted.
            break
        out.append(line)
    return "\n".join(out)


def spec_file(number: str) -> Path:
    if number == "README":
        return PROTOCOL / "README.md"
    matches = sorted(PROTOCOL.glob("%s-*.md" % number))
    assert len(matches) == 1, "docs/protocol must hold one %s-*.md, found %s" % (number, matches)
    return matches[0]


def _short_title(title: str) -> str:
    """A heading's title before a parenthesis or a dash: 'Key reference constants'."""
    return re.split(r" \(| — ", title, maxsplit=1)[0].strip()


def resolve_spec_heading(number: str, cited: str) -> Optional[Heading]:
    """The heading of spec `number` that the text after '§' starts with, or None."""
    matches: List[Tuple[int, Heading]] = []
    for heading in headings(spec_file(number)):
        for title in {heading.text, _short_title(heading.text)}:
            if title and (cited == title or (cited.startswith(title) and not cited[len(title)].isalnum())):
                matches.append((len(title), heading))
    return max(matches, key=lambda m: m[0])[1] if matches else None


def numbered_heading(path: Path, number: str) -> Optional[Heading]:
    for heading in headings(path):
        match = re.match(r"^(\d+(?:\.\d+)*)\.?\s", heading.text)
        if match and match.group(1) == number:
            return heading
    return None


def strings_in(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from strings_in(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from strings_in(value)


def unit_texts() -> Iterator[Tuple[str, str]]:
    """(file name, text) for every text in the four files of this unit."""
    for path in (SAFETY_MODEL, PARAMETERS_MD):
        yield path.name, path.read_text(encoding="utf-8")
    for path in (CHECKS_JSON, PARAMS_JSON):
        for text in strings_in(_load(path)):
            yield path.name, text


def markdown_links(text: str) -> List[str]:
    return re.findall(r"\[[^\]]*\]\(([^)\s]+)\)", text)


def table_rows(text: str, first_header: str) -> List[List[str]]:
    """The body rows of the first Markdown table whose first header cell is `first_header`."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        cells = _cells(line)
        if cells and cells[0] == first_header:
            rows = []
            for row in lines[i + 2:]:
                if not row.lstrip().startswith("|"):
                    break
                rows.append(_cells(row))
            return rows
    raise AssertionError("no table with first header %r" % first_header)


def _cells(line: str) -> List[str]:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return []
    # Split on pipes that are not escaped.
    parts = re.split(r"(?<!\\)\|", stripped[1:-1])
    return [p.strip().replace("\\|", "|") for p in parts]


def _number(text: str) -> str:
    value = float(text)
    return str(int(value)) if value == int(value) else repr(value)


def _fmt(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


# --------------------------------------------------------------------------
# checks.json
# --------------------------------------------------------------------------


def test_checks_json_schema() -> None:
    checks = _checks()
    assert isinstance(checks, list) and checks, "checks.json must be a non-empty JSON list"
    seen: Set[str] = set()
    for entry in checks:
        assert isinstance(entry, dict), entry
        cid = entry.get("id")
        assert set(entry) == CHECK_KEYS, "%s: keys %s, expected %s" % (cid, sorted(entry), sorted(CHECK_KEYS))
        assert isinstance(cid, str) and ID_RE.match(cid), "malformed id %r" % cid
        assert cid not in seen, "duplicate id %s" % cid
        seen.add(cid)
        assert entry["family"] == cid.split("-", 1)[0] and entry["family"] in FAMILIES, cid
        assert entry["severity"] in SEVERITIES, (cid, entry["severity"])
        assert entry["applies_to"] in SCOPES, (cid, entry["applies_to"])
        condition = entry["condition"]
        assert isinstance(condition, str) and len(condition) >= 20, (cid, condition)
        assert condition == condition.strip() and "\n" not in condition, cid
        slack = entry["slack"]
        if slack is not None:
            assert isinstance(slack, dict) and set(slack) == {"unit", "definition"}, (cid, slack)
            assert slack["unit"] in SLACK_UNITS, (cid, slack)
            assert isinstance(slack["definition"], str) and len(slack["definition"]) >= 20, (cid, slack)
        witness = entry["witness_fields"]
        assert isinstance(witness, list) and witness, cid
        assert len(set(witness)) == len(witness), "%s: repeated witness field" % cid
        unknown = sorted(set(witness) - WITNESS_FIELDS)
        assert not unknown, "%s: witness fields not in the witness schema: %s" % (cid, unknown)
        assert "hazard" in witness, "%s: every witness names its hazard" % cid
        if entry["applies_to"] in GEOMETRIC_SCOPES:
            assert slack is not None and slack["unit"] == "mm", "%s is geometric: its slack is in mm" % cid
            missing = {"piece", "point", "slack_mm"} - set(witness)
            assert not missing, "%s is geometric: witness lacks %s" % (cid, sorted(missing))
        if entry["applies_to"] in {"source-line", "reparsed-bytes"}:
            assert "src" in witness, "%s is raised at a line: its witness names it" % cid
        if slack is not None and slack["unit"] == "mm":
            assert "slack_mm" in witness, cid
        assert isinstance(entry["defined_in"], str), cid


def test_check_ids_are_exactly_the_catalogue() -> None:
    ids = {c["id"] for c in _checks()}
    expected = DESIGN_CHECK_IDS | READER_IDS | ASSERTION_IDS | IMPLIED_IDS
    assert ids == expected, "missing: %s; not in the catalogue: %s" % (sorted(expected - ids), sorted(ids - expected))


def test_check_severities() -> None:
    by_id = {c["id"]: c for c in _checks()}
    # The design marks these as warnings or notices; every other check refuses.
    assert by_id["T-PASS"]["severity"] == "warn"
    assert by_id["N-GI-UNKNOWN-KEY"]["severity"] == "warn"
    assert by_id["N-OPS-MISMATCH"]["severity"] == "warn"
    for cid in ("N-DUST", "N-VENDORC", "N-INFERRED-OPS", "N-BASELINE"):
        assert by_id[cid]["severity"] == "disclose", cid
    for cid in ASSERTION_IDS:
        assert by_id[cid]["severity"] == "internal-error", cid
    lenient = {"T-PASS", "N-GI-UNKNOWN-KEY", "N-DUST", "N-VENDORC", "N-INFERRED-OPS", "N-BASELINE"}
    lenient |= ASSERTION_IDS | IMPLIED_IDS
    for cid, entry in by_id.items():
        if cid not in lenient:
            assert entry["severity"] == "refuse", "%s must refuse, says %s" % (cid, entry["severity"])
    # Notices never refuse; nothing outside the N family is a mere notice.
    for cid, entry in by_id.items():
        if entry["severity"] == "disclose":
            assert entry["family"] == "N", cid


def test_check_conditions_name_the_red_team_rules() -> None:
    by_id = {c["id"]: c["condition"] for c in _checks()}
    # Rotation steps use the full disc until the rotation model is a fact.
    assert "R_rot + ρ" in by_id["W-ROT"] and "full disc" in by_id["W-ROT"]
    # The cumulative-rotation bound and the rung exemption.
    assert "350" in by_id["D-CSTEP"] and "RungExemption" in by_id["D-CSTEP"]
    # A start pose is required; the any-rotation envelope until the facts exist.
    assert "no start pose" in by_id["W-FIRST"].lower() and "R_rot + ρ" in by_id["W-FIRST"]
    # Post-job motion: through cuts and pockets refused while the fact is missing.
    assert "post_job.motion" in by_id["W-PARK"] and "through cuts and pockets" in by_id["W-PARK"]
    # Wire reach at both tower-2 placements, body hull until R0b.
    assert "reach_max" in by_id["P-REACH"] and "both" in by_id["P-REACH"] and "R0b" in by_id["P-REACH"]
    # Both tower-2 placements for the wheel zones.
    assert "both tower-2 placements" in by_id["W-TOWER"]
    # The tool zone applies to rapids too.
    assert "rapids" in by_id["T-TOWER"]
    # Obstacles and fixations.
    for cid in ("W-OBST", "B-OBST", "T-OBST"):
        assert "obstacle" in by_id[cid] and "fixation" in by_id[cid], cid
    # The split link lines while rotate-in-place is not a fact.
    assert "rotate_in_place_ok" in by_id["G-GATE"]
    # The arc end-equals-start rule (gi-format 8.7).
    assert "full circle" in by_id["D-ARC"] or "8.7" in by_id["D-ARC"]


# --------------------------------------------------------------------------
# The GI reader's findings (gi-format.md section 12)
# --------------------------------------------------------------------------


def _gi_findings() -> Dict[str, Tuple[str, str]]:
    text = GI_DOC.read_text(encoding="utf-8")
    heading = numbered_heading(GI_DOC, "12")
    assert heading is not None and heading.text == "12. Findings", heading
    rows = table_rows(section_text(GI_DOC, heading), "Id")
    found = {}
    for cells in rows:
        cid = cells[0].strip("`")
        found[cid] = (cells[1], cells[2])
    assert text  # the document was read
    return found


def test_reader_findings_match_gi_format() -> None:
    gi = _gi_findings()
    assert set(gi) == READER_IDS, "gi-format.md section 12 changed: %s" % sorted(set(gi) ^ READER_IDS)
    by_id = {c["id"]: c for c in _checks()}
    for cid, (severity, raised_when) in sorted(gi.items()):
        assert cid in by_id, "%s (gi-format.md section 12) is missing from checks.json" % cid
        assert by_id[cid]["severity"] == severity, "%s: checks.json %s, gi-format %s" % (
            cid,
            by_id[cid]["severity"],
            severity,
        )
        assert raised_when in by_id[cid]["condition"], (
            "%s: the condition must contain gi-format.md's 'Raised when' text verbatim: %r" % (cid, raised_when)
        )


# --------------------------------------------------------------------------
# safety-model.md
# --------------------------------------------------------------------------


def _catalogue_rows() -> List[List[str]]:
    heading = next(h for h in headings(SAFETY_MODEL) if h.text.endswith("The check catalogue"))
    return table_rows(section_text(SAFETY_MODEL, heading), "Id")


def test_safety_model_check_table_matches_checks_json() -> None:
    rows = _catalogue_rows()
    table = {}
    for cells in rows:
        assert len(cells) == 4, cells
        table[cells[0].strip("`")] = cells
    checks = _checks()
    assert [c["id"] for c in checks] == list(table), "the catalogue table lists the ids in checks.json order"
    for entry in checks:
        cells = table[entry["id"]]
        assert cells[1] == entry["severity"], entry["id"]
        assert cells[2] == entry["applies_to"], entry["id"]
        assert cells[3] == entry["condition"], "%s: table and checks.json disagree" % entry["id"]


def test_safety_model_mentions_only_catalogued_ids() -> None:
    ids = {c["id"] for c in _checks()}
    for path in (SAFETY_MODEL, PARAMETERS_MD):
        mentioned = set(PROSE_ID_RE.findall(path.read_text(encoding="utf-8")))
        stray = sorted(mentioned - ids)
        assert not stray, "%s names check ids that checks.json does not define: %s" % (path.name, stray)
    for entry in _checks():
        for cid in PROSE_ID_RE.findall(entry["condition"]):
            assert cid in ids, "%s's condition names an unknown id %s" % (entry["id"], cid)


def test_every_check_is_described_in_the_safety_model() -> None:
    text = SAFETY_MODEL.read_text(encoding="utf-8")
    catalogue = section_text(SAFETY_MODEL, next(h for h in headings(SAFETY_MODEL) if h.text.endswith("catalogue")))
    outside = text.replace(catalogue, "")
    for entry in _checks():
        if entry["id"] in READER_IDS - {"D-SINGLETOOL", "G-GATE"}:
            continue  # defined by gi-format.md section 12
        assert "`%s`" % entry["id"] in outside, "%s is only in the catalogue table" % entry["id"]


def test_defined_in_resolves() -> None:
    for entry in _checks():
        target = entry["defined_in"]
        assert re.match(r"^[a-z0-9-]+\.md#[^\s#]+$", target), (entry["id"], target)
        name, anchor = target.split("#", 1)
        path = PREP / name
        assert path.is_file(), (entry["id"], target)
        assert anchor in {h.slug for h in headings(path)}, "%s: %s has no heading %s" % (entry["id"], name, anchor)
        if entry["id"] in READER_IDS - {"D-SINGLETOOL", "G-GATE"}:
            assert target == "gi-format.md#12-findings", entry["id"]


def test_unit_docs_carry_the_apache_header() -> None:
    for path in (SAFETY_MODEL, PARAMETERS_MD):
        first = path.read_text(encoding="utf-8").splitlines()[0]
        assert first == APACHE_PROSE_HEADER, "%s must start with %s" % (path.name, APACHE_PROSE_HEADER)


def _register(first_header: str) -> Dict[str, List[str]]:
    heading = numbered_heading(SAFETY_MODEL, "1.4")
    assert heading is not None, "safety-model.md has no section 1.4"
    return {cells[0].strip("`"): cells for cells in table_rows(section_text(SAFETY_MODEL, heading), first_header)}


def test_unknowns_gates_and_rungs_are_registered() -> None:
    unknowns = _register("Unknown")
    gates = _register("Gate")
    rungs = _register("Rung")
    assert all(UNKNOWN_RE.match(u) for u in unknowns), sorted(unknowns)
    assert all(RUNG_RE.match(r) for r in rungs), sorted(rungs)
    texts = [t for _, t in unit_texts()]
    used_unknowns: Set[str] = set()
    used_rungs: Set[str] = set()
    for text in texts:
        used_unknowns.update(re.findall(r"\bU-\d{2}\b", text))
        used_rungs.update(re.findall(r"\b(R\d+[a-z]?)\b", text))
    unregistered = sorted(used_unknowns - set(unknowns))
    assert not unregistered, "unknowns used but not registered: %s" % unregistered
    assert used_rungs <= set(rungs), "rungs used but not registered: %s" % sorted(used_rungs - set(rungs))
    # A gate a check condition names exists in the gate table (gate and
    # parameter names are snake_case with at least one underscore).
    param_names = {p["name"] for p in _params()}
    for entry in _checks():
        for token in re.findall(r"`([a-z][a-z0-9]*_[a-z0-9_]+)`", entry["condition"]):
            assert token in gates or token in param_names, "%s names `%s`, which is no gate" % (entry["id"], token)
    # Every gate row names the rung that unlocks it.
    for gate, cells in gates.items():
        assert any(re.search(r"\bR\d+[a-z]?\b", c) for c in cells[1:]), "gate %s names no rung" % gate
    for param in _params():
        for unknown in param.get("unknown", []):
            assert unknown in unknowns, (param["name"], unknown)
        if param["unlock_rung"] is not None:
            assert param["unlock_rung"] in rungs, (param["name"], param["unlock_rung"])


# --------------------------------------------------------------------------
# params.json
# --------------------------------------------------------------------------


def test_params_json_schema() -> None:
    params = _params()
    assert isinstance(params, list) and params, "params.json must be a non-empty JSON list"
    fields = _fields()
    seen: Set[str] = set()
    for p in params:
        assert isinstance(p, dict), p
        name = p.get("name")
        assert PARAM_KEYS <= set(p), "%s lacks %s" % (name, sorted(PARAM_KEYS - set(p)))
        extra = set(p) - PARAM_KEYS - PARAM_OPTIONAL_KEYS
        assert not extra, "%s: unknown keys %s" % (name, sorted(extra))
        assert isinstance(name, str) and NAME_RE.match(name), name
        assert name not in seen, "duplicate parameter %s" % name
        seen.add(name)
        assert p["unit"] in PARAM_UNITS, (name, p["unit"])
        assert p["provenance"] in PROVENANCES, (name, p["provenance"])
        assert isinstance(p["source_citation"], str) and len(p["source_citation"]) >= 10, name
        assert isinstance(p["overridable"], bool), name
        rung = p["unlock_rung"]
        assert rung is None or (isinstance(rung, str) and RUNG_RE.match(rung)), (name, rung)
        default = p["default"]
        if p["unit"] == "enum":
            choices = p.get("choices")
            assert isinstance(choices, list) and choices and all(isinstance(c, str) for c in choices), name
            assert default is None or default in choices, (name, default, choices)
        else:
            assert "choices" not in p, name
            assert default is None or (isinstance(default, (int, float)) and not isinstance(default, bool)), (
                name,
                default,
            )
        if default is None:
            # Only a job input, or a value computed from one, has no default.
            assert p["provenance"] == "OWNER_JOB" or "formula" in p, (
                "%s has no default: only OWNER_JOB inputs and formulas over them may" % name
            )
        if p["provenance"] == "OWNER_JOB":
            assert rung is None, name
            # A job input says whether a job without it is refused (required)
            # or runs with that check disclosed as not evaluated (optional).
            assert isinstance(p.get("required"), bool), "%s: say whether the job input is required" % name
            if p["required"]:
                assert default is None, "%s is required: it has no default" % name
        else:
            assert "required" not in p, name
        if p["provenance"] == "DERIVED":
            assert isinstance(p.get("formula"), str) and p["formula"], "%s is DERIVED: give its formula" % name
        if p["provenance"] == "GATE":
            assert rung is not None, "%s is GATE: name the rung that changes it" % name
        if "after_unlock" in p:
            assert rung is not None, name
        for key in ("symbol", "formula", "measured_rule", "note"):
            if key in p:
                assert isinstance(p[key], str) and p[key], (name, key)
        unknowns = p.get("unknown", [])
        assert isinstance(unknowns, list) and all(UNKNOWN_RE.match(u) for u in unknowns), (name, unknowns)
        measured_by = p.get("measured_by", [])
        assert isinstance(measured_by, list), name
        for field_id in measured_by:
            assert field_id in fields, "%s: field guide 04 has no field %s" % (name, field_id)
            field = fields[field_id]
            if p["unit"] != "enum" and field["kind"] == "number":
                assert field["unit"] == p["unit"], (name, field_id, field["unit"], p["unit"])
        if measured_by:
            assert "measured_rule" in p, "%s: say how the measured values become the parameter" % name
        else:
            assert "measured_rule" not in p, name


def test_no_parameter_is_overridable_in_version_1() -> None:
    # The project design (section 3.1) lets owner policy only tighten, except
    # typed overrides on items marked overridable, and names none of the
    # parameters. The safe reading: none is, until a reviewed change marks one.
    overridable = sorted(p["name"] for p in _params() if p["overridable"])
    assert overridable == [], overridable


def test_every_spec_param_has_spec_citation() -> None:
    checked = 0
    for p in _params():
        if p["provenance"] != "SPEC":
            continue
        cites = [(m.group(1), (m.group(2) or "").strip()) for m in SPEC_CITE_RE.finditer(p["source_citation"])]
        cites = [(n, s) for n, s in cites if s]
        assert cites, "%s is SPEC but cites no 'spec NN §Heading': %r" % (p["name"], p["source_citation"])
        sections = []
        for number, cited in cites:
            heading = resolve_spec_heading(number, cited)
            assert heading is not None, "%s cites spec %s §%s, which is not a heading there" % (
                p["name"],
                number,
                cited,
            )
            sections.append(section_text(spec_file(number), heading))
        # The cited text must contain the value (truthfulness, not just form).
        default = p["default"]
        if isinstance(default, (int, float)):
            needle = _number(str(abs(default)))
            pattern = re.compile(r"(?<![\d.])%s(?![\d])" % re.escape(needle))
            assert any(pattern.search(s) for s in sections), "%s = %s does not appear in the cited section(s) %s" % (
                p["name"],
                default,
                cites,
            )
        checked += 1
    assert checked >= 10, "expected the spec constants (supports, zones, Z, tool, panel, tank, block), got %d" % checked


def test_spec_constants_are_spec() -> None:
    by_name = _params_by_name()
    expected = {
        "support_r1": 402,
        "support_r2": 211,
        "support_r3": 211,
        "support_delta2": 76,
        "support_delta3": -76,
        "heading_offset": 90,
        "c_grid_step": 10,
        "tower_zone_leg_wheel": 150,
        "tower_zone_leg_tool": 155,
        "z_safety": 10,
        "z_travel": 58,
        "tool_max_diameter": 8,
        "tool_max_length": 58,
        "panel_max_side": 3500,
        "tank_volume": 1.15,
        "upload_block": 1024,
    }
    for name, value in expected.items():
        assert by_name[name]["provenance"] == "SPEC", name
        assert by_name[name]["default"] == value, (name, by_name[name]["default"])


def test_unmeasured_defaults_name_their_field_guide_04_fields() -> None:
    by_name = _params_by_name()
    fields = _fields()
    for p in _params():
        if p["provenance"] == "DEFAULT_UNMEASURED":
            assert p["measured_by"], "%s is unmeasured: name the field guide 04 fields that measure it" % p["name"]
            assert p["unlock_rung"] == "R0b", p["name"]
            assert "field guide 04" in p["source_citation"], p["name"]
            for field_id in p["measured_by"]:
                assert fields[field_id]["measurement_entry"] is True, (p["name"], field_id)
    assert {p["name"] for p in _params() if p["provenance"] == "DEFAULT_UNMEASURED"} == {
        "r_w",
        "body_hull_margin",
        "tower_base_radius",
        "wire_attachment",
    }
    # Field guide 04 measures the footprint too; the spec value stays in the union model.
    for name, field_id in (("support_r1", "footprint.q1_mm"), ("support_r2", "footprint.q2_mm"),
                           ("support_r3", "footprint.q3_mm"), ("support_delta1", "footprint.q1_deg"),
                           ("support_delta2", "footprint.q2_deg"), ("support_delta3", "footprint.q3_deg")):
        assert field_id in by_name[name].get("measured_by", []), (name, field_id)
        assert "union" in by_name[name]["measured_rule"], name


def test_wheel_contact_radius_is_the_maximum_of_the_three_modules() -> None:
    r_w = _params_by_name()["r_w"]
    assert r_w["default"] == 40 and r_w["unit"] == "mm"
    assert r_w["measured_by"] == [
        "wheel.q1.contact_radius_mm",
        "wheel.q2.contact_radius_mm",
        "wheel.q3.contact_radius_mm",
    ]
    rule = r_w["measured_rule"].lower()
    assert "maximum" in rule and "never the mean" in rule, rule
    assert "U-22" in r_w["unknown"]


def test_parameters_md_matches_params_json() -> None:
    heading = numbered_heading(PARAMETERS_MD, "3")
    assert heading is not None, "parameters.md has no section 3 (the table)"
    text = section_text(PARAMETERS_MD, heading)
    rows: Dict[str, List[str]] = {}
    for line in text.splitlines():
        cells = _cells(line)
        if cells and re.fullmatch(r"`[a-z][a-z0-9_]*`", cells[0]):
            name = cells[0].strip("`")
            assert name not in rows, "parameters.md lists %s twice" % name
            rows[name] = cells
    params = _params_by_name()
    assert set(rows) == set(params), "parameters.md and params.json list different parameters: %s" % sorted(
        set(rows) ^ set(params)
    )
    for name, p in params.items():
        cells = rows[name]
        assert len(cells) == 6, (name, cells)
        assert cells[2] == _fmt(p["default"]), "%s: default %r in the table, %r in params.json" % (
            name,
            cells[2],
            _fmt(p["default"]),
        )
        assert cells[3] == p["unit"], (name, cells[3])
        assert cells[4] == p["provenance"], (name, cells[4])
        assert cells[5] == (p["unlock_rung"] or "-"), (name, cells[5])


# --------------------------------------------------------------------------
# Derived values are recomputed, not trusted
# --------------------------------------------------------------------------


def _supports(by_name: Dict[str, Dict[str, Any]]) -> List[Tuple[float, float]]:
    """Support offsets from the tool at C = 0 (spec 08 §Footprint model)."""
    theta = math.radians(by_name["heading_offset"]["default"])  # C = 0
    out = []
    for k in (1, 2, 3):
        r = by_name["support_r%d" % k]["default"]
        delta = math.radians(by_name["support_delta%d" % k]["default"])
        out.append((-r * math.cos(theta + delta), -r * math.sin(theta + delta)))
    return out


def test_footprint_golden_values() -> None:
    q1, q2, q3 = _supports(_params_by_name())
    assert q1 == pytest.approx((0.0, -402.0), abs=1e-9)
    assert q2 == pytest.approx((204.73, -51.05), abs=0.01)
    assert q3 == pytest.approx((-204.73, -51.05), abs=0.01)


def test_derived_params_recompute() -> None:
    by_name = _params_by_name()
    supports = _supports(by_name)
    rho = by_name["r_w"]["default"] + by_name["m_w"]["default"]
    assert by_name["rho"]["default"] == rho == 55

    cx = sum(s[0] for s in supports) / 3
    cy = sum(s[1] for s in supports) / 3
    d_c = math.hypot(cx, cy)
    r_c = max(math.hypot(s[0] - cx, s[1] - cy) for s in supports)
    r_max = max(math.hypot(*s) for s in supports)
    r_min = min(math.hypot(*s) for s in supports)
    assert by_name["support_centroid_distance"]["default"] == pytest.approx(d_c, abs=0.005)
    assert by_name["support_centroid_radius"]["default"] == pytest.approx(r_c, abs=0.005)
    assert by_name["r_max"]["default"] == pytest.approx(r_max, abs=1e-9)
    assert by_name["r_min"]["default"] == pytest.approx(r_min, abs=1e-9)

    # R_rot contains every support during a turn about any pivot between the
    # tool and the support centroid: the distance s + |d_k - s c^| is convex in
    # s, so it peaks at an end. Check that claim numerically too.
    unit = (cx / d_c, cy / d_c)
    worst = 0.0
    for i in range(0, 1001):
        s = d_c * i / 1000
        pivot = (unit[0] * s, unit[1] * s)
        for sx, sy in supports:
            worst = max(worst, s + math.hypot(sx - pivot[0], sy - pivot[1]))
    assert worst == pytest.approx(max(r_max, d_c + r_c), abs=1e-6)
    r_rot = math.ceil(max(r_max, d_c + r_c))
    assert by_name["r_rot"]["default"] == r_rot == 404

    # The piece-length invariant (safety-model §7.3) with the largest tool the
    # spec allows (8 mm) and the check chord tolerance as the largest sagitta.
    r_tool = by_name["tool_max_diameter"]["default"] / 2
    delta_tool = by_name["delta_tool"]["default"]
    e_max = by_name["check_chord_tol"]["default"]
    piece = min(50, math.floor(r_min - rho - r_tool - delta_tool - 2 * e_max - 1))
    assert by_name["piece_length_max"]["default"] == piece == 50
    assert r_min - piece > rho + r_tool + delta_tool + 2 * e_max

    # The bring-up safe-zone inset at the desk (safety-model §8.3).
    inset = r_rot + rho + by_name["m_edge"]["default"] + by_name["geom_tol"]["default"]
    assert by_name["safe_zone_inset_desk"]["default"] == inset == 519


def test_bring_up_panel_numbers_in_the_text() -> None:
    text = SAFETY_MODEL.read_text(encoding="utf-8")
    inset = _params_by_name()["safe_zone_inset_desk"]["default"]
    for w, h in ((1220, 1600), (1220, 2440)):
        zone = "%d x %d mm" % (w - 2 * inset, h - 2 * inset)
        assert zone in text, "safety-model.md should state the %d x %d panel's zone as %s" % (w, h, zone)
    assert "%d mm" % (2 * inset) in text  # below this side length there is no zone


# --------------------------------------------------------------------------
# Citations: in-repository only, and each one resolves
# --------------------------------------------------------------------------


def test_cites_in_repo_spec_only() -> None:
    problems = []
    spec_cites = 0
    for name, text in unit_texts():
        for kind, pattern in FORBIDDEN:
            for match in pattern.finditer(text):
                problems.append("%s: %s: %r" % (name, kind, match.group(0)))
        for match in SPEC_CITE_RE.finditer(text):
            number, cited = match.group(1), (match.group(2) or "").strip()
            spec_file(number)  # the file exists
            if cited:
                spec_cites += 1
                if resolve_spec_heading(number, cited) is None:
                    problems.append("%s: 'spec %s §%s' is not a heading of %s" % (
                        name, number, cited, spec_file(number).name))
        for label, pattern, path in NUMBERED_CITES:
            for match in pattern.finditer(text):
                if numbered_heading(path, match.group(1)) is None:
                    problems.append("%s: '%s §%s' is not a section of %s" % (name, label, match.group(1), path.name))
    assert spec_cites >= 20, "expected many spec citations, found %d" % spec_cites
    assert not problems, "\n".join(problems)


def test_markdown_links_resolve() -> None:
    problems = []
    for path in (SAFETY_MODEL, PARAMETERS_MD):
        for target in markdown_links(path.read_text(encoding="utf-8")):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
                problems.append("%s: external link %s (the documents are read offline)" % (path.name, target))
                continue
            file_part, _, anchor = target.partition("#")
            dest = (path.parent / file_part).resolve() if file_part else path
            if not dest.exists():
                problems.append("%s: %s does not exist" % (path.name, target))
                continue
            if anchor:
                if dest.suffix != ".md":
                    problems.append("%s: anchor on a non-Markdown file: %s" % (path.name, target))
                elif anchor not in {h.slug for h in headings(dest)}:
                    problems.append("%s: %s has no heading #%s" % (path.name, dest.name, anchor))
            if file_part and "protocol" in file_part:
                if not str(dest).startswith(str(PROTOCOL.resolve())):
                    problems.append("%s: protocol link outside docs/protocol: %s" % (path.name, target))
    assert not problems, "\n".join(problems)


# --------------------------------------------------------------------------
# What one heading per cut can certify (red-team PHYS-15; the lead's correction)
# --------------------------------------------------------------------------


def _limit_section() -> str:
    heading = next(h for h in headings(SAFETY_MODEL) if "one heading per cut can certify" in h.text)
    return section_text(SAFETY_MODEL, heading)


def test_heading_limit_states_the_geometric_cause() -> None:
    text = " ".join(_limit_section().split())
    assert "computed from the footprint model, not measured on a robot" in text
    # The limit is a property of holding one heading round a closed outline,
    # not of the unmeasured wheel contact radius: it persists at rho = 15 mm.
    assert "not caused by the unmeasured wheel contact radius" in text
    assert "Measuring `r_w` will not lift this limit" in text
    assert "ρ = 15 mm" in text and "18.25 mm" in text
    assert "translated by a fixed offset" in text
    # The data: what certifies at no heading, what certifies at all, and in between.
    sizes = ("250 x 250", "300 x 200", "400 x 300", "700 x 500", "100 x 100", "200 x 150", "220 x 220", "240 x 240")
    for size in sizes:
        assert size in text, size
    assert "6 of 36" in text and "19 of 36" in text
    # The two paths to larger closed parts, neither of which loosens W-SURF.
    assert "P23" in text and "R8k" in text
    # The inference about the vendor is labelled as one.
    assert "INFERENCE" in text


def test_heading_limit_never_blames_the_default_radius() -> None:
    text = " ".join(_limit_section().split())
    # The design's first explanation compared the 51 mm side-support offset with
    # rho + r_tool + delta_tool = 58.25 mm. That comparison may be quoted only as
    # the explanation that was found incomplete.
    for sentence in re.split(r"(?<=[.;:])\s+", text):
        if "58.25" in sentence:
            assert "incomplete" in sentence or "not the cause" in sentence, sentence
        lowered = sentence.lower()
        if ("contact radius" in lowered or "r_w" in lowered) and re.search(r"\b(lift|raise|remove)s?\b", lowered):
            assert re.search(r"\b(not|never|no)\b", lowered), sentence


# --------------------------------------------------------------------------
# Rules that must hold whatever the wording (independent review of unit F02)
# --------------------------------------------------------------------------


def _code_block_line(section: str, starts_with: str) -> str:
    for line in section.splitlines():
        if line.strip().startswith(starts_with):
            return line
    raise AssertionError("no line starting with %r" % starts_with)


def test_holding_material_counts_prior_removal() -> None:
    # A panel that is not new may carry prior removal (job.toml, or the previous
    # job of a series), and Rem(0) holds it. Removal deep enough to break holding
    # material breaks it whether it was cut by this program or before: a part
    # freed by an earlier through cut is loose, and a wheel on it or a tool in it
    # must be refused (M-ATTACH, T-ATTACH), and towers separated before the job
    # must be refused (M-TOWERSEP at the first piece).
    heading = numbered_heading(SAFETY_MODEL, "7.4")
    assert heading is not None
    definition = _code_block_line(section_text(SAFETY_MODEL, heading), "Rem_hold(j)")
    assert "Rem(0)" in definition, "Rem_hold(j) must include the deep part of the prior removal Rem(0): %r" % definition
    for cid in ("M-ATTACH", "T-ATTACH", "M-TOWERSEP"):
        condition = next(c["condition"] for c in _checks() if c["id"] == cid)
        assert "prior removal" in condition, "%s must say that prior removal counts" % cid


def test_every_link_line_shape_has_a_check() -> None:
    # Links are rotation steps (W-ROT), translations at a fixed heading (W-RAPID),
    # the first line (W-FIRST), and lines that rotate and translate together in
    # the disclosed combined profile. The last kind must be reported by a check
    # whose condition says so (the project design's red-team case RT22 expects
    # W-RAPID), or a checker written from these documents has no id for it.
    by_id = {c["id"]: c["condition"] for c in _checks()}
    assert "combined" in by_id["W-RAPID"] and "safety-model §9.4" in by_id["W-RAPID"], by_id["W-RAPID"]
    heading = numbered_heading(SAFETY_MODEL, "9.4")
    assert heading is not None
    assert "`W-RAPID`" in section_text(SAFETY_MODEL, heading)


def _hull_reach(by_name: Dict[str, Dict[str, Any]], w: float, h: float, tower: Tuple[float, float]) -> float:
    """The largest distance from `tower` to the default body hull of any pose W-AREA accepts.

    W-AREA keeps every wheel disc of radius rho inside the panel shrunk by m_edge,
    so every support lies in [m_edge + rho, W - m_edge - rho] x [...]; the default
    hull is the convex hull of the wheel discs grown by m_body, so its farthest
    point from a tower is the farthest support plus rho + m_body.
    """
    rho = by_name["rho"]["default"]
    lo = by_name["m_edge"]["default"] + rho
    corners = [(x, y) for x in (lo, w - lo) for y in (lo, h - lo)]
    far = max(math.hypot(x - tower[0], y - tower[1]) for x, y in corners)
    return far + rho + by_name["body_hull_margin"]["default"]


def _reach_section() -> str:
    heading = numbered_heading(SAFETY_MODEL, "6.4")
    assert heading is not None
    return " ".join(section_text(SAFETY_MODEL, heading).split())


def test_wire_reach_examples_recompute() -> None:
    by_name = _params_by_name()
    reach = by_name["reach_max"]["default"]
    off = by_name["tol_panel"]["default"]  # how far INFO;27 may place tower 2 beyond its corner
    text = _reach_section()

    def worst(w: float, h: float) -> Tuple[float, float]:
        t1 = _hull_reach(by_name, w, h, (0.0, 0.0))
        t2 = max(_hull_reach(by_name, w, h, (w + off, 0.0)), _hull_reach(by_name, w, h, (0.0, h + off)))
        return t1, t2

    # The 3050 x 1530 mm panel: its far corner is 3412 mm from tower 1, but a hull
    # that W-AREA accepts stays well inside reach_max from either tower, so P-REACH
    # cannot be what refuses work there before R0b.
    t1, t2 = worst(3050, 1530)
    assert t1 < reach and t2 < reach
    assert "%d mm" % round(t1) in text and "%d mm" % math.ceil(t2) in text, (round(t1), math.ceil(t2))
    assert "%d mm" % round(math.hypot(3050, 1530)) in text
    # The 2440 x 1220 mm sheet is clear by a wide margin.
    t1, t2 = worst(2440, 1220)
    assert max(t1, t2) < reach
    assert "%d mm" % math.ceil(max(t1, t2)) in text
    # Where P-REACH does refuse: the far end of a 3500 x 1000 mm panel.
    t1, _ = worst(3500, 1000)
    assert t1 > reach and "3500 x 1000 mm" in text
    # The hull reaches at most m_body - m_edge beyond the panel's edges.
    beyond = by_name["body_hull_margin"]["default"] - by_name["m_edge"]["default"]
    assert "%d mm beyond" % beyond in text
    # The triangle-inequality figure (corner distance plus the tower offset) is not
    # a distance anything has; it must not be stated as one.
    assert "2753" not in text


def test_measured_rules_say_what_unknown_does() -> None:
    # Field guide 04 lets every field be answered 'unknown', and 'unknown keeps the
    # software's cautious default'. A rule that only says how known readings
    # combine leaves a partial measurement open to being read as complete.
    for p in _params():
        if not p.get("measured_by"):
            continue
        assert "unknown" in p["measured_rule"], "%s: say what an 'unknown' reading does" % p["name"]
    rule = _params_by_name()["r_w"]["measured_rule"]
    # One unknown module could have the largest contact radius: the default must
    # stay in force (and stay disclosed), and a known reading above it still counts.
    assert "DEFAULT_UNMEASURED" in rule and "larger of" in rule, rule


def test_invariant_slack_sign_matches_its_violation() -> None:
    # M-INVARIANT fires when Δ = min(50, floor(X − 1)) < 5 with
    # X = min_k r_k − ρ − r_tool − δ_tool − 2·e_max, i.e. when X < 6. Its slack must
    # be negative exactly then.
    entry = next(c for c in _checks() if c["id"] == "M-INVARIANT")
    definition = entry["slack"]["definition"]
    assert definition.startswith("min_k r_k − ρ − r_tool − δ_tool − 2·e_max − 6 mm"), definition
    for tenth in range(0, 200):
        x = tenth / 10.0
        violated = min(50, math.floor(x - 1)) < 5
        assert violated == (x - 6 < 0), x


def test_derived_body_radius_and_air_z_floor_are_catalogued() -> None:
    # The catalogue says a check never uses a number it does not list.
    by_name = _params_by_name()
    r_body = by_name["r_body"]
    assert r_body["provenance"] == "DERIVED" and r_body["unit"] == "mm"
    expected = by_name["r_rot"]["default"] + by_name["rho"]["default"] + by_name["body_hull_margin"]["default"]
    assert r_body["default"] == expected == 519
    z_floor = by_name["z_min_unverified"]
    assert z_floor["default"] == 5 and z_floor["provenance"] == "GATE" and z_floor["unlock_rung"] == "R3"
    assert "U-23" in z_floor["unknown"]


def test_feed_and_speed_caps_change_at_the_same_rung() -> None:
    # Both caps stand for U-37; the rung table says the owner sets them at R5.
    by_name = _params_by_name()
    assert by_name["feed_cap"]["unlock_rung"] == by_name["rpm_cap"]["unlock_rung"] == "R5"
    assert "U-37" in by_name["feed_cap"]["unknown"] and "U-37" in by_name["rpm_cap"]["unknown"]


def test_post_job_motion_counts_prior_removal() -> None:
    # While post_job.motion is missing the robot may drive anywhere after the job;
    # a through cut or pocket already in the panel is as much a hazard as one this
    # program cuts.
    condition = next(c["condition"] for c in _checks() if c["id"] == "W-PARK")
    assert "prior removal" in condition, condition


def test_rotation_radius_under_the_union_model() -> None:
    # After R0b the active model is the union of the vendor and the measured
    # footprints: six support positions and two candidate centroids. "The mean of
    # the d_k" over six positions is neither robot's centroid, so the rule must say
    # which pivots and supports R_rot covers: both centroids, every position.
    for name in ("r_rot", "r_body"):
        formula = _params_by_name()[name]["formula"]
        assert "union" in formula and "both" in formula, (name, formula)
    heading = numbered_heading(SAFETY_MODEL, "4.6")
    assert heading is not None
    text = " ".join(section_text(SAFETY_MODEL, heading).split())
    assert "union model" in text, "safety-model §4.6 must say how R_rot is computed under the union model"
