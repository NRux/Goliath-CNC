# SPDX-License-Identifier: GPL-3.0-or-later
"""Checks of the Kiri:Moto device profile and the generic CAM guide (unit PP3).

Two deliverables are checked here:

* ``posts/kirimoto/``: a Kiri:Moto machine (device) profile, stated in
  ``goliath-device.json``, and its README. Kiri:Moto has header and footer
  macros, not post-processor scripting, so the profile is all it can offer.
* ``docs/user-guide/cam-generic.md``: the guide for any CAM program that
  writes grbl-style millimetre G-code (Vectric, Estlcam, Carbide Create, ...).

Both documents show sample files. ``test_guide_sample_outputs_pass_goliath_prep_check``
runs them through goliath-prep: through its G-code reader (unit P02) as soon as
that is in the checkout and importable, and through ``goliath-prep check``
(unit P17) once that exists. Until then it skips, saying which part is
missing. The other tests need no part of goliath-prep and always run: they
check the profile's macros, that every setting the guide names can be checked
against ``docs/prep/gi-format.md``, and the samples with a small checker.

The small checker below is not a G-code reader. It holds the samples to a
stricter, smaller vocabulary than GI, so that a sample shown to the owner as
"a good file" is a good file before goliath-prep can say so itself.

Python 3.9 compatible on purpose: the root suite runs on 3.9 in CI.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

import pytest

REPO = Path(__file__).resolve().parent.parent
GUIDE = REPO / "docs" / "user-guide" / "cam-generic.md"
KIRI_DIR = REPO / "posts" / "kirimoto"
KIRI_PROFILE = KIRI_DIR / "goliath-device.json"
KIRI_README = KIRI_DIR / "README.md"
GI_DOC = REPO / "docs" / "prep" / "gi-format.md"
CHECKS_JSON = REPO / "docs" / "prep" / "checks.json"

# goliath-prep's parts this file runs the samples through, by work unit.
PREP_SRC = REPO / "goliath-prep" / "src" / "goliath_prep"
READER_DIR = PREP_SRC / "gcode"  # unit P02: read_gcode
JOBSPEC_FILE = PREP_SRC / "jobspec.py"  # unit P01: JobSpec.load(job.toml)
CLI_FILE = PREP_SRC / "cli.py"  # unit P17: goliath-prep check JOB.toml INPUT
CLI_EXIT_READY = 0
CLI_EXIT_REFUSED = 4

SPDX_GPL = "<!-- SPDX-License-Identifier: GPL-3.0-or-later -->"
SPDX_APACHE = "<!-- SPDX-License-Identifier: Apache-2.0 -->"

# The design's default caps (feed 2000 mm/min, spindle 27 000 rpm): a sample
# shown as good must not need more.
FEED_CAP = Decimal("2000")
RPM_CAP = Decimal("27000")
ARC_RADIUS_TOLERANCE = Decimal("0.005")  # gi-format.md 8.7
ARC_AMBIGUOUS = Decimal("0.01")  # gi-format.md 8.7 (PHYS-16)

# The keys the profile sets, each documented in the README's settings table.
KIRI_KEYS = frozenset(
    {
        "mode",
        "deviceName",
        "bedWidth",
        "bedDepth",
        "maxHeight",
        "originCenter",
        "spindleMax",
        "gcodePre",
        "gcodePost",
        "gcodeSpindle",
        "gcodeChange",
        "gcodeDwell",
        "gcodeFExt",
        "gcodeSpace",
        "gcodeStrip",
        "useLaser",
        "useIndexed",
    }
)
# Kiri:Moto header directives ("';; NAME ...'" lines in the Header macro) are
# consumed by Kiri:Moto, not written. SCALE and AXISMAP would rescale or rename
# axes without a trace in the file, and COMPACT-OUTPUT changes the layout, so
# the profile may use only this one.
ALLOWED_DIRECTIVES = (";; DECIMALS = 3",)
# Values the macros are expanded with in these tests.
KIRI_CONSTS = {"speed": "18000", "spindle": "18000", "rpm": "18000", "tool": "1", "tool_name": "6mm flat", "time": "1"}

URL_PATTERNS = (
    ("URL with a scheme", re.compile(r"\b[a-z][a-z0-9+.-]*://", re.IGNORECASE)),
    ("www host name", re.compile(r"\bwww\.", re.IGNORECASE)),
    ("mailto link", re.compile(r"\bmailto:", re.IGNORECASE)),
    (
        "bare domain name",
        re.compile(
            r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|tech|it|eu|dev|app|co|uk|de|info|edu|gov|space)\b",
            re.IGNORECASE,
        ),
    ),
)
# What a menu path looks like ("File > Save", "Machine -> Setup", "Toolpaths → Save").
MENU_PATH = re.compile(r"[A-Za-z)]\s*(?:>|->|→|»)\s*[A-Z]")

FINDING_ID = re.compile(r"\b[DGN]-[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*\b")
SAMPLE_MARKER = re.compile(r"<!--\s*goliath-sample\s+(\{.*?\})\s*-->\s*\n```gcode\n(.*?)\n```", re.DOTALL)
JOB_MARKER = re.compile(r"<!--\s*goliath-sample-job\s+(\{.*?\})\s*-->", re.DOTALL)
JOB_FILE_MARKER = re.compile(r"<!--\s*goliath-sample-job-file\s*-->\s*\n```toml\n(.*?)\n```", re.DOTALL)
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def read(path: Path) -> str:
    assert path.is_file(), "%s is missing" % path.relative_to(REPO).as_posix()
    return path.read_text(encoding="utf-8")


def profile() -> Dict[str, Any]:
    data = json.loads(read(KIRI_PROFILE))
    assert isinstance(data, dict), "goliath-device.json must hold one JSON object (a Kiri:Moto device)"
    return data


def without_code_blocks(markdown: str) -> str:
    return re.sub(r"^[ \t]*```.*?^[ \t]*```[ \t]*$", "", markdown, flags=re.MULTILINE | re.DOTALL)


class Sample(NamedTuple):
    doc: Path
    meta: Dict[str, Any]
    lines: List[str]
    end: int  # offset in the document where the sample's code block ends


def samples(doc: Path) -> List[Sample]:
    text = read(doc)
    found = []
    for match in SAMPLE_MARKER.finditer(text):
        meta = json.loads(match.group(1))
        assert set(meta) <= {"id", "expect", "from_profile"}, "%s: unknown sample keys %s" % (doc.name, meta)
        assert meta.get("expect") == "ok", "%s: samples shown in the guides are good files: %s" % (doc.name, meta)
        found.append(Sample(doc, meta, match.group(2).split("\n"), match.end()))
    return found


class Job(NamedTuple):
    W: Decimal
    H: Decimal
    T: Decimal
    tower2: str
    d: Decimal
    flute: Decimal
    raw: Dict[str, Any]


def sample_job() -> Job:
    """The job every sample is written for, stated once, in the guide."""
    found = JOB_MARKER.findall(read(GUIDE))
    assert len(found) == 1, "the guide must state the samples' job exactly once (goliath-sample-job marker)"
    raw = json.loads(found[0], parse_float=Decimal, parse_int=Decimal)
    assert set(raw) == {"panel", "placement", "z_zero", "tool"}, raw
    assert raw["placement"] == {"dx": 0, "dy": 0} and raw["z_zero"] == "top", raw
    panel, tool = raw["panel"], raw["tool"]
    return Job(panel["W"], panel["H"], panel["T"], panel["tower2"], tool["d"], tool["flute"], raw)


def kiri_emit(lines: Sequence[str], consts: Optional[Dict[str, str]] = None) -> List[str]:
    """What Kiri:Moto writes for a macro, with Strip Comments on.

    Facts from Kiri:Moto's CAM exporter (its open-source code, read for this
    unit): ``;; DECIMALS`` and the other header directives are consumed; every
    other macro line has its ``{name}`` placeholders replaced, is cut at its
    first ``;`` and trimmed, and is dropped if nothing is left.
    """
    values = dict(KIRI_CONSTS if consts is None else consts)
    out = []
    for raw in lines:
        if raw.startswith(";; "):
            continue
        line = PLACEHOLDER.sub(lambda m: values[m.group(1)], raw)
        if ";" in line:
            line = line[: line.index(";")].strip()
            if not line:
                continue
        out.append(line)
    return out


# --------------------------------------------------------------------------
# Markdown structure: headings, anchors, tables, links
# --------------------------------------------------------------------------


def github_slug(heading: str) -> str:
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(markdown: str) -> Set[str]:
    seen: Dict[str, int] = {}
    result = set()
    for match in re.finditer(r"^#{1,6}[ \t]+(.+?)[ \t]*$", without_code_blocks(markdown), re.MULTILINE):
        slug = github_slug(match.group(1))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(slug if count == 0 else "%s-%d" % (slug, count))
    return result


def gi_sections() -> Dict[str, str]:
    """gi-format.md: anchor -> the text of that section (up to the next heading of any level)."""
    text = without_code_blocks(read(GI_DOC))
    heads = list(re.finditer(r"^#{1,6}[ \t]+(.+?)[ \t]*$", text, re.MULTILINE))
    sections = {}
    for i, match in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        sections[github_slug(match.group(1))] = text[match.end() : end]
    return sections


def section(markdown: str, heading_regex: str) -> str:
    match = re.search(r"^(#{1,6})[ \t]+" + heading_regex + r".*$", markdown, re.MULTILINE)
    assert match, "no heading matching %r" % heading_regex
    level = len(match.group(1))
    rest = markdown[match.end() :]
    end = re.search(r"^#{1,%d}[ \t]" % level, rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


def table_rows(markdown: str) -> List[List[str]]:
    rows = []
    for line in markdown.splitlines():
        line = line.strip()
        if not line.startswith("|") or re.match(r"^\|[\s:|-]+\|$", line):
            continue
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows[1:]  # without the header row


def links(markdown: str) -> List[str]:
    return LINK.findall(without_code_blocks(markdown))


def backticked(cell: str) -> List[str]:
    return re.findall(r"`([^`]+)`", cell)


# --------------------------------------------------------------------------
# The sample checker (not a reader: a small, stricter vocabulary than GI)
# --------------------------------------------------------------------------

WORD = re.compile(r"([A-Z])([+-]?\d+(?:\.\d+)?)")
CODE_LINE = re.compile(r"^(?:[ \t]*[A-Z][+-]?\d+(?:\.\d+)?)*[ \t]*$")
SAMPLE_LETTERS = frozenset("GMXYZIJFST")
SAMPLE_G = frozenset({0, 1, 2, 3, 17, 21, 90, 94})
SAMPLE_M = frozenset({3, 5, 6, 30})


def sample_problems(lines: Sequence[str], job: Job) -> List[str]:
    """Why these lines are not an exemplary GI-body program for this job (empty: they are).

    Exemplary means: only the words above, G21 and G90 before the first move,
    the first move raises Z, plunges are feeds, every cut stays inside the
    panel by the tool radius and above its bottom, arcs follow gi-format.md 8.7
    with I/J centres, feeds and speeds within the default caps, one tool at
    most and before the first move, the spindle stopped and M30 last.
    """
    problems: List[str] = []
    radius = job.d / 2
    pos: Dict[str, Optional[Decimal]] = {"X": None, "Y": None, "Z": None}
    mode: Optional[int] = None
    units = absolute = spindle = moved = ended = False
    tool_words = 0
    for number, line in enumerate(lines, 1):
        where = "line %d %r" % (number, line)
        if ended:
            problems.append("%s: after M30" % where)
            continue
        if any(ch in line for ch in ";(%") or not CODE_LINE.match(line):
            problems.append("%s: not a line of words (comments, parentheses and %% lines are not used)" % where)
            continue
        words = WORD.findall(line)
        letters = [w[0] for w in words]
        values: Dict[str, Decimal] = {}
        gs: List[int] = []
        ms: List[int] = []
        for letter, num in words:
            if letter not in SAMPLE_LETTERS:
                problems.append("%s: letter %s is not used in samples" % (where, letter))
            elif letter in "GM":
                if not re.match(r"^\d+$", num):
                    problems.append("%s: %s%s is not a whole number" % (where, letter, num))
                    continue
                (gs if letter == "G" else ms).append(int(num))
            elif letters.count(letter) > 1:
                problems.append("%s: %s twice" % (where, letter))
            else:
                values[letter] = Decimal(num)
        problems.extend("%s: G%d is not used in samples" % (where, g) for g in gs if g not in SAMPLE_G)
        problems.extend("%s: M%d is not used in samples" % (where, m) for m in ms if m not in SAMPLE_M)
        if len(ms) > 1:
            problems.append("%s: more than one M word" % where)
        units = units or 21 in gs
        absolute = absolute or 90 in gs
        if "F" in values and not Decimal(0) < values["F"] <= FEED_CAP:
            problems.append("%s: feed outside (0, %s]" % (where, FEED_CAP))
        if "T" in values or 6 in ms:
            tool_words += 1
            if moved or tool_words > 1:
                problems.append("%s: a tool word after the first move, or a second one" % where)
        if 3 in ms:
            if not Decimal(0) < values.get("S", Decimal(0)) <= RPM_CAP:
                problems.append("%s: M3 needs S in (0, %s]" % (where, RPM_CAP))
            spindle = True
        if 5 in ms:
            spindle = False
        if 30 in ms:
            if len(words) != 1:
                problems.append("%s: M30 shares its line" % where)
            ended = True
            continue
        motions = [g for g in gs if g in (0, 1, 2, 3)]
        if len(motions) > 1:
            problems.append("%s: two motion codes" % where)
        if motions:
            mode = motions[0]
        axes = {k: values[k] for k in "XYZ" if k in values}
        has_ij = "I" in values or "J" in values
        if not axes and not has_ij:
            continue
        if mode is None:
            problems.append("%s: a move before any motion code" % where)
            continue
        if not (units and absolute):
            problems.append("%s: a move before G21 and G90" % where)
        if not moved and ("X" in axes or "Y" in axes or mode in (2, 3)):
            problems.append("%s: the first move must raise Z before any X or Y move" % where)
        start = dict(pos)
        end = {k: axes.get(k, pos[k]) for k in "XYZ"}
        if end["Z"] is None:
            problems.append("%s: Z unknown" % where)
            continue
        cutting = end["Z"] < 0
        if cutting and mode == 0:
            problems.append("%s: a rapid below the stock top" % where)
        if cutting and not spindle:
            problems.append("%s: cutting with the spindle off" % where)
        if end["Z"] < -job.T:
            problems.append("%s: below the panel bottom (T = %s)" % (where, job.T))
        if end["Z"] > 40:
            problems.append("%s: Z above +40 mm" % where)
        points: List[Tuple[Decimal, Decimal, Decimal]] = []  # (x, y, reach around the point)
        if mode in (2, 3):
            if not has_ij or start["X"] is None or start["Y"] is None:
                problems.append("%s: an arc needs I or J and a known start" % where)
                continue
            cx = start["X"] + values.get("I", Decimal(0))
            cy = start["Y"] + values.get("J", Decimal(0))
            r_start = (values.get("I", Decimal(0)) ** 2 + values.get("J", Decimal(0)) ** 2).sqrt()
            r_end = ((end["X"] - cx) ** 2 + (end["Y"] - cy) ** 2).sqrt()
            gap = ((end["X"] - start["X"]) ** 2 + (end["Y"] - start["Y"]) ** 2).sqrt()
            if r_start == 0 or abs(r_start - r_end) > ARC_RADIUS_TOLERANCE or Decimal(0) < gap < ARC_AMBIGUOUS:
                problems.append("%s: arc breaks gi-format.md 8.7" % where)
            points.append((cx, cy, r_start))
        else:
            if has_ij:
                problems.append("%s: I or J on a linear move" % where)
            if end["X"] is not None and end["Y"] is not None:
                points.append((end["X"], end["Y"], Decimal(0)))
        inset = radius if cutting else Decimal(0)
        for x, y, reach in points:
            if not (inset + reach <= x <= job.W - inset - reach and inset + reach <= y <= job.H - inset - reach):
                problems.append("%s: outside the panel [0, %s] x [0, %s] (tool radius %s)" % (where, job.W, job.H, inset))
        pos = end
        moved = True
    if not ended:
        problems.append("no M30 at the end")
    if spindle:
        problems.append("the spindle is still on at the end")
    return problems


# --------------------------------------------------------------------------
# The Kiri:Moto profile
# --------------------------------------------------------------------------


def test_kiri_profile_sets_exactly_the_documented_keys() -> None:
    data = profile()
    assert set(data) == KIRI_KEYS, "keys differ from the documented set: extra %s, missing %s" % (
        sorted(set(data) - KIRI_KEYS),
        sorted(KIRI_KEYS - set(data)),
    )
    assert data["mode"] == "CAM"
    assert data["useLaser"] is False and data["useIndexed"] is False  # no laser, no A or B axis
    assert data["originCenter"] is False  # origin at a corner, not the bed centre


def test_kiri_profile_writes_plain_millimetre_absolute_gcode_first() -> None:
    pre = profile()["gcodePre"]
    assert kiri_emit(pre) == ["G21", "G90", "G17"], kiri_emit(pre)
    # Nothing in the Header macro may be an annotation: Kiri:Moto cannot put
    # the GI version line on line 1 (see the README), so any ;GOLIATH line
    # would make the file a GI file that is refused at line 1 (D-GI-VERSION).
    for macro in ("gcodePre", "gcodePost", "gcodeSpindle", "gcodeChange", "gcodeDwell"):
        for line in profile()[macro]:
            assert "goliath" not in line.lower(), "%s: %r" % (macro, line)


def test_kiri_profile_strips_comments_and_uses_only_safe_directives() -> None:
    data = profile()
    # With comments kept, Kiri:Moto writes its own comment block (with
    # "; Stock X:..." and "; starting ... op" lines) that goliath-prep refuses
    # as in-stream markers (D-MARKER). Strip Comments must stay on.
    assert data["gcodeStrip"] is True
    for macro in ("gcodePre", "gcodePost", "gcodeSpindle", "gcodeChange", "gcodeDwell"):
        for line in data[macro]:
            if line.startswith(";;"):
                assert macro == "gcodePre" and line in ALLOWED_DIRECTIVES, "%s: directive %r" % (macro, line)
            else:
                assert ";" not in line and "(" not in line, "%s: macro lines carry no comments: %r" % (macro, line)


def test_kiri_profile_keeps_a_second_tool_and_a_dwell_visible() -> None:
    data = profile()
    change = kiri_emit(data["gcodeChange"], dict(KIRI_CONSTS, tool="2"))
    # An empty Tool macro would let a second tool's paths be cut with the first
    # tool without a trace; with a T word goliath-prep refuses it (D-SINGLETOOL).
    assert change and any(re.search(r"\bT2\b", line) for line in change), change
    assert all("{tool}" in line for line in data["gcodeChange"] if line.startswith(("M6", "T")))
    dwell = kiri_emit(data["gcodeDwell"])
    # An empty Dwell macro would drop a dwell without a trace; G4 reaches
    # goliath-prep, which refuses it until rung R4 (G-GATE).
    assert dwell and all(line.startswith("G4") for line in dwell), dwell


def test_kiri_profile_spindle_macro_and_max_spindle() -> None:
    data = profile()
    assert kiri_emit(data["gcodeSpindle"]) == ["M3 S18000"]
    # Kiri:Moto writes the Spindle macro only when Max Spindle is set.
    assert data["spindleMax"] == int(RPM_CAP)


def test_kiri_profile_footer_stops_the_spindle_then_ends() -> None:
    assert kiri_emit(profile()["gcodePost"]) == ["M5", "M30"]


def test_kiri_profile_workspace_and_file_settings() -> None:
    data = profile()
    assert data["bedWidth"] == 3500 and data["bedDepth"] == 3500  # spec README: max work area
    assert data["maxHeight"] == 58  # spec README: Z travel
    assert data["gcodeFExt"] == "gcode" and data["gcodeSpace"] is True
    for text in (read(REPO / "docs" / "protocol" / "README.md"),):
        assert "3500 × 3500 mm" in text and "58 mm" in text


def test_kiri_readme_documents_every_profile_value() -> None:
    rows = table_rows(section(read(KIRI_README), r"\d+\.\s+The settings"))
    data = profile()
    for key, value in data.items():
        matching = [row for row in rows if "`%s`" % key in " ".join(row)]
        assert len(matching) == 1, "README: exactly one settings row must name `%s`" % key
        cells = " ".join(matching[0])
        if isinstance(value, list):
            for line in value:
                assert "`%s`" % line in cells, "README row for %s must show `%s`" % (key, line)
        else:
            shown = json.dumps(value) if isinstance(value, (bool, str)) else str(value)
            assert "`%s`" % shown in cells, "README row for %s must show `%s`" % (key, shown)


def test_kiri_readme_explains_why_the_file_has_no_gi_header() -> None:
    text = read(KIRI_README)
    why = section(text, r"\d+\.\s+Why")
    for needed in ("Strip Comments", "D-GI-VERSION", "D-MARKER", "N-INFERRED-OPS", "job file"):
        assert needed in why, "the README's 'Why' section must mention %r" % needed


def test_kiri_readme_sample_is_what_the_profile_writes() -> None:
    found = [s for s in samples(KIRI_README) if s.meta.get("from_profile")]
    assert len(found) == 1, "the README shows one sample built from the profile"
    lines = found[0].lines
    data = profile()
    pre, post = kiri_emit(data["gcodePre"]), kiri_emit(data["gcodePost"])
    assert lines[: len(pre)] == pre, "the sample must start with what the Header macro writes"
    assert lines[-len(post) :] == post, "the sample must end with what the Footer macro writes"
    assert kiri_emit(data["gcodeSpindle"])[0] in lines


# --------------------------------------------------------------------------
# The guide
# --------------------------------------------------------------------------


def test_documents_carry_their_folder_licence() -> None:
    assert read(GUIDE).splitlines()[0] == SPDX_GPL  # docs/user-guide/: root licence (LICENSING.md)
    assert read(KIRI_README).splitlines()[0] == SPDX_APACHE  # posts/: Apache-2.0, pending Q13


@pytest.mark.parametrize("doc", [GUIDE, KIRI_README], ids=lambda p: p.name)
def test_no_external_urls_and_no_menu_paths(doc: Path) -> None:
    """Offline help: no internet links. No menu paths: we could not check any CAM program's menus."""
    text = read(doc)
    for name, pattern in URL_PATTERNS:
        hit = pattern.search(text)
        assert not hit, "%s has a %s: %r" % (doc.name, name, hit.group(0))
    for line in without_code_blocks(text).splitlines():
        hit = MENU_PATH.search(re.sub(r"`[^`]*`|<!--.*?-->", "", line))
        assert not hit, "%s reads like a menu path: %r" % (doc.name, line)


@pytest.mark.parametrize("doc", [GUIDE, KIRI_README], ids=lambda p: p.name)
def test_relative_links_and_anchors_resolve(doc: Path) -> None:
    targets = links(read(doc))
    assert targets, "%s links nowhere" % doc.name
    for target in targets:
        path_part, _, anchor = target.partition("#")
        dest = (doc.parent / path_part).resolve() if path_part else doc
        assert dest.exists(), "%s: link %s points to a missing file" % (doc.name, target)
        assert REPO in dest.parents or dest == REPO, "%s: link %s leaves the repository" % (doc.name, target)
        if anchor:
            assert dest.suffix == ".md", "%s: anchor on a non-Markdown file: %s" % (doc.name, target)
            assert anchor in anchors(read(dest)), "%s: no heading #%s in %s" % (doc.name, anchor, dest.name)


def test_guide_and_readme_point_at_each_other() -> None:
    assert any(t.split("#")[0].endswith("posts/kirimoto/README.md") for t in links(read(GUIDE)))
    assert any(t.split("#")[0].endswith("docs/user-guide/cam-generic.md") for t in links(read(KIRI_README)))


def test_guide_settings_table_is_checkable_against_gi_format() -> None:
    """Every setting row cites gi-format.md, and what it says is refused is refused there."""
    rows = table_rows(section(read(GUIDE), r"\d+\.\s+The settings"))
    assert len(rows) >= 15, "the settings table looks truncated (%d rows)" % len(rows)
    sections = gi_sections()
    defined = set(FINDING_ID.findall(section(read(GI_DOC), r"12\.\s+Findings")))
    for row in rows:
        cited = [t.split("#", 1)[1] for t in LINK.findall(" ".join(row)) if "gi-format.md#" in t]
        assert cited, "settings row cites no section of gi-format.md: %s" % row
        text = " ".join(sections[a] for a in cited if a in sections)
        assert len(text) > 0 and all(a in sections for a in cited), "row cites a missing section: %s" % cited
        otherwise = row[2]
        for finding in FINDING_ID.findall(otherwise):
            assert finding in defined, "%s is not a reader finding of gi-format.md section 12" % finding
            assert finding in text, "%s is not named in the cited sections %s" % (finding, cited)
        for code in backticked(otherwise):
            if re.match(r"^[GM]\d+(?:\.\d+)?$|^[A-Z]$", code):
                assert "`%s`" % code in text, "`%s` is not named in the cited sections %s" % (code, cited)


@pytest.mark.parametrize("doc", [GUIDE, KIRI_README], ids=lambda p: p.name)
def test_every_finding_named_is_defined(doc: Path) -> None:
    gi_text = read(GI_DOC)
    checks = read(CHECKS_JSON) if CHECKS_JSON.is_file() else None
    reader_ids = set(FINDING_ID.findall(section(gi_text, r"12\.\s+Findings")))
    named = set(FINDING_ID.findall(read(doc)))
    assert named, "%s names no finding" % doc.name
    for finding in sorted(named):
        assert finding in gi_text, "%s names %s, which gi-format.md does not define" % (doc.name, finding)
        if checks is not None and finding in reader_ids:
            assert '"%s"' % finding in checks, "%s is missing from docs/prep/checks.json" % finding


def test_guide_troubleshooting_covers_every_refusal_of_the_settings_table() -> None:
    guide = read(GUIDE)
    in_settings = set(FINDING_ID.findall(section(guide, r"\d+\.\s+The settings")))
    troubleshooting = section(guide, r"\d+\.\s+When goliath-prep refuses")
    listed = {row[0].strip("`") for row in table_rows(troubleshooting)}
    assert in_settings <= listed, "troubleshooting table lacks %s" % sorted(in_settings - listed)


def test_guide_states_the_sample_job_in_words() -> None:
    job = sample_job()
    guide = read(GUIDE)
    tail = guide[JOB_MARKER.search(guide).end() :][:1200]
    for fragment in (
        "%s × %s mm" % (job.W, job.H),
        "%s mm thick" % job.T,
        "tower 2 on the %s axis" % job.tower2,
        "%s mm diameter" % job.d,
        "%s mm flute" % job.flute,
    ):
        assert fragment in tail, "the guide's words after the job marker must say %r" % fragment


def test_sample_checker_rejects_what_it_should() -> None:
    job = Job(Decimal(600), Decimal(400), Decimal(18), "Y", Decimal(6), Decimal(22), {})
    good = ["G21", "G90", "G0 Z5.000", "G0 X50.000 Y50.000", "M3 S18000", "G1 Z-2.000 F300", "G1 X60.000 F1000",
            "G0 Z5.000", "M5", "M30"]
    assert sample_problems(good, job) == []
    bad = {
        "inch": ["G20" if line == "G21" else line for line in good],
        "comment": good[:2] + ["(hello)"] + good[2:],
        "xy first": ["G21", "G90", "G0 X50.000 Y50.000 Z5.000"] + good[4:],
        "r arc": good[:7] + ["G2 X70.000 Y50.000 R5.000"] + good[7:],
        "mismatched arc": good[:7] + ["G2 X70.000 Y50.000 I1.000 J0.000"] + good[7:],
        "near full circle": good[:7] + ["G2 X60.005 Y50.000 I5.000 J0.000"] + good[7:],
        "outside": good[:6] + ["G1 X599.000 F1000"] + good[7:],
        "too deep": good[:5] + ["G1 Z-18.500 F300"] + good[6:],
        "rapid plunge": good[:5] + ["G0 Z-2.000"] + good[6:],
        "spindle off": good[:4] + good[5:],
        "second tool": good[:7] + ["M6 T2"] + good[7:],
        "fast": good[:6] + ["G1 X60.000 F2500"] + good[7:],
        "no end": good[:-1],
        "after end": good + ["G0 Z10.000"],
        "drill cycle": good[:5] + ["G81 X50.000 Y50.000 Z-2.000"] + good[5:],
    }
    for name, lines in bad.items():
        assert sample_problems(lines, job), "the sample checker missed: %s" % name


def test_guide_samples_pass_the_sample_checks() -> None:
    job = sample_job()
    shown = samples(GUIDE) + samples(KIRI_README)
    assert len(samples(GUIDE)) >= 1 and len(samples(KIRI_README)) >= 1
    for sample in shown:
        problems = sample_problems(sample.lines, job)
        assert not problems, "%s sample %s:\n  %s" % (sample.doc.name, sample.meta["id"], "\n  ".join(problems))


def test_guide_full_circle_sample_is_explained() -> None:
    """The guide's sample has a full circle (end equals start); the text says what goliath-prep makes of it."""
    lines = samples(GUIDE)[0].lines
    assert any(re.match(r"^G[23] ", line) for line in lines)
    assert "full circle" in read(GUIDE)


# --------------------------------------------------------------------------
# The samples through goliath-prep (units P02, P01 and P17)
# --------------------------------------------------------------------------


def kiri_variants(base: Sample) -> List[Tuple[str, List[str], str, int]]:
    """(name, lines, expected refusing finding, line) built from the README sample with the profile's macros."""
    data = profile()
    lines = list(base.lines)
    post = kiri_emit(data["gcodePost"])
    spindle_at = lines.index(kiri_emit(data["gcodeSpindle"])[0])
    second_tool = lines[: -len(post)] + kiri_emit(data["gcodeChange"], dict(KIRI_CONSTS, tool="2")) + post
    dwell = lines[: spindle_at + 1] + kiri_emit(data["gcodeDwell"]) + lines[spindle_at + 1 :]
    return [
        ("second tool after the first move", second_tool, "D-SINGLETOOL", len(lines) - len(post) + 1),
        ("dwell while drop_dwells is closed", dwell, "G-GATE", spindle_at + 2),
    ]


def _goliath_prep_or_skip(stage: Path, unit: str) -> None:
    rel = stage.relative_to(REPO).as_posix()
    if not stage.exists():
        pytest.skip("%s (unit %s) is not in this checkout yet; the checks above that need no reader ran" % (rel, unit))
    if sys.version_info < (3, 11):
        pytest.skip("goliath-prep needs Python >= 3.11; CI's apps job (Python 3.11 and 3.14) runs this check")
    if importlib.util.find_spec("goliath_prep") is None:
        pytest.skip("goliath-prep is not installed in this environment; CI's apps job installs it and runs this check")
    import goliath_prep

    installed = Path(goliath_prep.__file__).resolve()
    assert PREP_SRC.resolve() in installed.parents, "goliath_prep imports from %s, not this checkout" % installed


def _value(x: Any) -> Any:
    """A bound job value: goliath-prep may wrap it in a Param (value, unit, source, ref)."""
    return getattr(x, "value", x)


def _job_file(tmp_path: Path) -> Path:
    found = JOB_FILE_MARKER.findall(read(GUIDE))
    if len(found) != 1:
        pytest.fail(
            "goliath-prep can read the samples now, but the guide shows no job file for them. Add the samples' "
            "job file, in goliath-prep's job file layout (unit P01), as a ```toml block after a "
            "<!-- goliath-sample-job-file --> marker in docs/user-guide/cam-generic.md, so the owner sees a "
            "complete job file and this test can read the samples with it."
        )
    path = tmp_path / "job.toml"
    path.write_text(found[0] + "\n", encoding="utf-8")
    return path


def _load_job_spec(path: Path) -> Any:
    jobspec = importlib.import_module("goliath_prep.jobspec")
    spec = jobspec.JobSpec.load(path)
    job = sample_job()
    shown = {
        "W": _value(spec.panel.W),
        "H": _value(spec.panel.H),
        "T": _value(spec.panel.T),
        "d": _value(spec.tool.d),
        "flute": _value(spec.tool.flute),
    }
    for key, value in shown.items():
        assert Decimal(str(value)) == getattr(job, key), "the guide's job file says %s = %s, its words %s" % (
            key,
            value,
            getattr(job, key),
        )
    return spec


def _reader() -> Callable[..., Any]:
    module = importlib.import_module("goliath_prep.gcode")
    read_gcode = getattr(module, "read_gcode", None)
    if read_gcode is None:
        pytest.fail("goliath_prep.gcode exists but does not export read_gcode (unit P02's interface)")
    return read_gcode


def _refusing(findings: Sequence[Any]) -> List[Any]:
    return [f for f in findings if f.severity in ("refuse", "internal-error")]


def _line_of(finding: Any) -> Optional[int]:
    witness = finding.witness
    return witness.src.line if witness is not None and witness.src is not None else None


def _describe(findings: Sequence[Any]) -> str:
    return "; ".join("%s %s line %s: %s" % (f.check_id, f.severity, _line_of(f), f.message) for f in findings)


def _all_cases() -> List[Tuple[str, List[str], Optional[str], Optional[int]]]:
    cases: List[Tuple[str, List[str], Optional[str], Optional[int]]] = []
    for sample in samples(GUIDE) + samples(KIRI_README):
        cases.append(("%s sample %s" % (sample.doc.name, sample.meta["id"]), sample.lines, None, None))
    base = [s for s in samples(KIRI_README) if s.meta.get("from_profile")][0]
    cases.extend(("Kiri:Moto profile, %s" % n, lines, f, line) for n, lines, f, line in kiri_variants(base))
    return cases


def _goliath_prep_command() -> List[str]:
    folder = str(Path(sys.executable).parent)
    script = shutil.which("goliath-prep", path=os.pathsep.join([folder, os.environ.get("PATH", "")]))
    if script is None:
        pytest.fail("goliath_prep/cli.py exists, but no goliath-prep command is installed next to %s" % sys.executable)
    return [script]


def test_guide_sample_outputs_pass_goliath_prep_check(tmp_path: Path) -> None:
    """Every sample file the guides show is accepted by goliath-prep; the profile's refusals are refused.

    Stage 1 (unit P02 present): the reader, with the samples' job file from
    the guide. Stage 2 (unit P17 present): ``goliath-prep check JOB INPUT``,
    exit status 0 for a sample, 4 and the finding id for a refused variant.
    Before either stage can run, the samples and their refused variants must
    exist and pass the small checker.
    """
    cases = _all_cases()
    shown = [case for case in cases if case[2] is None]
    assert len(shown) >= 2 and len(cases) > len(shown), "the guides show no samples, or the profile no variants"
    job = sample_job()
    for name, lines, _expected, _line in shown:
        assert not sample_problems(lines, job), "%s fails the small checker" % name
    _goliath_prep_or_skip(READER_DIR, "P02")
    read_gcode = _reader()
    job_path = _job_file(tmp_path)
    spec = _load_job_spec(job_path)
    failures = []
    for name, lines, expected, line in cases:
        result = read_gcode("\n".join(lines) + "\n", flavour="auto", job=spec)
        refusing = _refusing(result.findings)
        if expected is None:
            if refusing or result.gi_header is not None:
                failures.append("%s: expected a plain file read without refusal; got %s" % (name, _describe(refusing)))
        elif not any(f.check_id == expected and f.severity == "refuse" and _line_of(f) == line for f in refusing):
            failures.append("%s: expected %s on line %d; got %s" % (name, expected, line, _describe(result.findings)))
    assert not failures, "\n".join(failures)

    if not CLI_FILE.is_file():
        return  # stage 2 waits for unit P17; stage 1 above ran in full
    command = _goliath_prep_command()
    for index, (name, lines, expected, _line) in enumerate(cases):
        source = tmp_path / ("case-%d.gcode" % index)
        source.write_text("\n".join(lines) + "\n", encoding="utf-8")
        run = subprocess.run(command + ["check", str(job_path), str(source)], capture_output=True, text=True, timeout=300)
        output = run.stdout + run.stderr
        if expected is None and run.returncode != CLI_EXIT_READY:
            failures.append("%s: goliath-prep check exited %d:\n%s" % (name, run.returncode, output))
        if expected is not None and (run.returncode != CLI_EXIT_REFUSED or expected not in output):
            failures.append("%s: expected exit 4 naming %s; exited %d:\n%s" % (name, expected, run.returncode, output))
    assert not failures, "\n".join(failures)
