# SPDX-License-Identifier: GPL-3.0-or-later
"""Checks for the bring-up field guides (unit D00).

Field guide 03 installs the applications; field guide 04 is rung R0b, the
tape-measure session with the robot switched off. Its fill-in fields are listed,
machine-readably, in ``docs/field-guide/04-measure-your-robot.fields.json``; the
runner's measurement entry (unit R07) is meant to use the same ids.

These tests run in the root suite, so they must stay Python 3.9 compatible and
use only the standard library and pytest. TOML is read with a small parser for
the plain ``key = "value"`` lines used here, because ``tomllib`` needs 3.11.
"""

from __future__ import annotations

import importlib
import json
import math
import re
import sys
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pytest

REPO = Path(__file__).resolve().parents[1]
GUIDES = REPO / "docs" / "field-guide"
INSTALL_GUIDE = GUIDES / "03-install-the-applications.md"
MEASURE_GUIDE = GUIDES / "04-measure-your-robot.md"
FIELDS_JSON = GUIDES / "04-measure-your-robot.fields.json"
SPEC_08 = REPO / "docs" / "protocol" / "08-head-angle-and-cut-order.md"

# Unit R07 owns these files (work units: goliath-run/src/goliath_run/{cli,rungs}.py).
R07_RUNGS = REPO / "goliath-run" / "src" / "goliath_run" / "rungs.py"
R07_CLI = REPO / "goliath-run" / "src" / "goliath_run" / "cli.py"

# The three distributions the install guide installs, by folder. goliath-link
# exists; goliath-prep and goliath-run are planned (design 9.2: distribution
# names; 9.3: Python >= 3.11). Their pyproject files, once unit F01 has created
# them, take precedence over these planned values.
PLANNED_NAMES = {"goliath-prep": "goliath-prep", "goliath-run": "goliath-run"}
PLANNED_REQUIRES_PYTHON = ">=3.11"
APP_FOLDERS = ("goliath-link", "goliath-prep", "goliath-run")
APP_VENV = "goliath-run/.venv"

SPDX_MARKDOWN = "<!-- SPDX-License-Identifier: GPL-3.0-or-later -->"
SPDX_JSON = "SPDX-License-Identifier: GPL-3.0-or-later"

# Guides that may keep internet links, each with the reason. Every other field
# guide (and every file that will be added) is read offline, at the robot.
URL_EXEMPT_GUIDES = {
    "01-first-contact.md": (
        "the pre-specification exploration guide: its steps need the internet "
        "(web archive searches, tool downloads) and are not done at the robot"
    ),
}

# Things that count as an external URL in an offline guide.
URL_PATTERNS = (
    ("URL with a scheme", re.compile(r"\b[a-z][a-z0-9+.-]*://", re.IGNORECASE)),
    ("www host name", re.compile(r"\bwww\.", re.IGNORECASE)),
    ("mailto link", re.compile(r"\bmailto:", re.IGNORECASE)),
    (
        "bare domain name",
        re.compile(
            r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\."
            r"(?:com|org|net|io|tech|it|eu|dev|app|co|uk|de|info|edu|gov)\b",
            re.IGNORECASE,
        ),
    ),
    ("protocol-relative link", re.compile(r"\]\(//")),
)

FIELD_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9][a-z0-9_]*)+$")
UNIT_SUFFIXES = {"_mm": "mm", "_deg": "deg", "_s": "s"}
NUMBER_UNITS = {"mm", "deg", "s", "count"}
KINDS = {"number", "choice", "text"}
REF_RE = re.compile(r"^(?:U-\d{2}|Q\d{1,2})$")
REQUIRED_KEYS = ("id", "kind", "unit", "meaning", "how_measured", "feeds", "refs", "required", "measurement_entry")

# The design's R0b go criterion (design 11.2 R0b): distances within +-15 mm of
# 402/211/211, angles within +-5 deg of 0/+-76 deg.
GO_DISTANCE_TOLERANCE_MM = 15.0
GO_ANGLE_TOLERANCE_DEG = 5.0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _read(path: Path) -> str:
    assert path.is_file(), "%s is missing" % path.relative_to(REPO).as_posix()
    return path.read_text(encoding="utf-8")


def _load_fields() -> dict:
    return json.loads(_read(FIELDS_JSON))


def _field_list() -> List[dict]:
    data = _load_fields()
    fields = data.get("fields")
    assert isinstance(fields, list) and fields, "fields.json must hold a non-empty list under 'fields'"
    return fields


def _field_ids() -> List[str]:
    return [f["id"] for f in _field_list()]


def _code_blocks(markdown: str) -> List[Tuple[str, str]]:
    """Return (language, body) for every fenced code block, including fences indented inside lists."""
    blocks = []
    pattern = r"^[ \t]*```([A-Za-z0-9_-]*)[ \t]*\n(.*?)^[ \t]*```[ \t]*$"
    for match in re.finditer(pattern, markdown, re.MULTILINE | re.DOTALL):
        blocks.append((match.group(1).lower(), match.group(2)))
    return blocks


def _toml_table(text: str, table: str) -> Optional[Dict[str, str]]:
    """Return the plain ``key = "value"`` pairs of one TOML table, or None if it is absent.

    Only what the pyproject files here use: one table header per line, string
    values on one line, optional quotes around keys. Anything else in the table
    is ignored, which the callers do not rely on.
    """
    inside = False
    found = False
    pairs: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        header = re.match(r"^\[([^\[\]]+)\]", line)
        if header:
            inside = header.group(1).strip() == table
            found = found or inside
            continue
        if not inside:
            continue
        match = re.match(r'^"?([A-Za-z0-9_.-]+)"?\s*=\s*"([^"]*)"', line)
        if match:
            pairs[match.group(1)] = match.group(2)
    return pairs if found else None


def _distribution_name(folder: str) -> str:
    pyproject = REPO / folder / "pyproject.toml"
    if pyproject.is_file():
        project = _toml_table(pyproject.read_text(encoding="utf-8"), "project")
        assert project and "name" in project, "%s has no [project] name" % pyproject.relative_to(REPO)
        return project["name"]
    assert folder in PLANNED_NAMES, "%s has no pyproject.toml and is not a planned distribution" % folder
    return PLANNED_NAMES[folder]


def _pip_install_lines(markdown: str) -> List[Tuple[str, str]]:
    found = []
    for language, body in _code_blocks(markdown):
        for line in body.splitlines():
            if re.search(r"\bpip\s+install\b", line):
                found.append((language, line.strip()))
    return found


def _install_args(line: str) -> List[str]:
    tokens = line.split()
    index = next(i for i, tok in enumerate(tokens) if tok == "install")
    return tokens[index + 1:]


def _folder_of(path_token: str) -> str:
    normalised = path_token.replace("\\", "/")
    while normalised.startswith("./"):
        normalised = normalised[2:]
    return normalised.rstrip("/")


def _section(markdown: str, title_fragment: str) -> str:
    """Return the text of the level-2 section whose heading contains title_fragment."""
    lines = markdown.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and title_fragment.lower() in line.lower():
            start = i
            break
    assert start is not None, "no '## ... %s' section" % title_fragment
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end])


def _table_rows(section: str) -> List[List[str]]:
    rows = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if cells and re.fullmatch(r"`[^`]+`", cells[0]):
            rows.append(cells)
    return rows


def _fill_in_rows() -> Dict[str, List[str]]:
    rows: Dict[str, List[str]] = {}
    duplicates = []
    for cells in _table_rows(_section(_read(MEASURE_GUIDE), "Fill-in table")):
        field_id = cells[0].strip("`")
        if field_id in rows:
            duplicates.append(field_id)
        rows[field_id] = cells
    assert not duplicates, "fill-in table lists these ids twice: %s" % sorted(duplicates)
    return rows


def _spec08_constants() -> Tuple[float, float, float]:
    """Rear distance, side distance and side angle from spec 08 §Constants."""
    text = _read(SPEC_08)
    rear = re.search(r"^\|\s*Rear support distance\s*\|\s*([0-9.]+)\s*mm\s*\|", text, re.MULTILINE)
    side = re.search(r"^\|\s*Side support distance\s*\|\s*([0-9.]+)\s*mm\s*\|", text, re.MULTILINE)
    angle = re.search(r"^\|\s*Side support angle\s*\|\s*±\s*([0-9.]+)\s*°", text, re.MULTILINE)
    assert rear and side and angle, "spec 08 §Constants no longer lists the support distances and angle"
    return float(rear.group(1)), float(side.group(1)), float(angle.group(1))


def _vendor_points() -> Dict[str, Tuple[float, float]]:
    """Support points of spec 08 as (back, right) in mm in the guide's body frame.

    Spec 08: q_k = p - r_k u(theta + delta_k), u(theta) points from the body to
    the tool, delta = 0, +76, -76 deg. Seen from above with the panel's X axis to
    the right, +76 deg puts q2 on the robot's right: back = r cos(delta),
    right = r sin(delta). (Whether the robot uses that orientation is U-20.)
    """
    rear, side, angle = _spec08_constants()
    points = {}
    for name, distance, delta in (("q1", rear, 0.0), ("q2", side, angle), ("q3", side, -angle)):
        rad = math.radians(delta)
        points[name] = (distance * math.cos(rad), distance * math.sin(rad))
    return points


def _r07_lookup_r0b(module: types.ModuleType) -> object:
    """Find the R0b RungDefinition in R07's rungs module.

    Accepted: a ``RUNGS`` mapping (id -> definition) or sequence, or a
    module-level object with ``id == "R0b"`` and a ``measurements`` attribute.
    Anything else is an error that names what was expected.
    """
    candidates: List[object] = []
    registry = getattr(module, "RUNGS", None)
    if isinstance(registry, Mapping):
        if "R0b" in registry:
            candidates.append(registry["R0b"])
    elif isinstance(registry, (list, tuple)):
        candidates.extend(r for r in registry if getattr(r, "id", None) == "R0b")
    for value in vars(module).values():
        if isinstance(value, type):
            continue
        if getattr(value, "id", None) == "R0b" and hasattr(value, "measurements"):
            candidates.append(value)
    unique: List[object] = []
    for candidate in candidates:
        if not any(candidate is seen for seen in unique):
            unique.append(candidate)
    if not unique:
        raise LookupError(
            "%s defines no R0b RungDefinition with measurements: expose it as RUNGS['R0b'] "
            "(or a module-level definition with id 'R0b')" % module.__name__
        )
    if len(unique) > 1:
        raise LookupError("%s defines %d different R0b definitions" % (module.__name__, len(unique)))
    return unique[0]


def r07_r0b_measurement_ids(module: types.ModuleType) -> Set[str]:
    """Return the ids of the MeasurementSpecs of R07's R0b rung."""
    rung = _r07_lookup_r0b(module)
    measurements = getattr(rung, "measurements", None)
    if measurements is None:
        raise LookupError("the R0b definition has no 'measurements'")
    ids: List[str] = []
    for spec in measurements:
        spec_id = getattr(spec, "id", None)
        if spec_id is None:
            spec_id = getattr(spec, "fact_id", None)
        if not isinstance(spec_id, str):
            raise LookupError("an R0b MeasurementSpec has no string 'id' or 'fact_id': %r" % (spec,))
        ids.append(spec_id)
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise LookupError("R0b measurement ids repeat: %s" % duplicates)
    return set(ids)


def describe_id_mismatch(guide_ids: Set[str], r07_ids: Set[str]) -> str:
    """Empty when the sets agree; otherwise both differences, by name."""
    parts = []
    only_guide = sorted(guide_ids - r07_ids)
    only_r07 = sorted(r07_ids - guide_ids)
    if only_guide:
        parts.append("in field guide 04 but not in R07's R0b measurement entry: %s" % ", ".join(only_guide))
    if only_r07:
        parts.append("in R07's R0b measurement entry but not in field guide 04: %s" % ", ".join(only_r07))
    return "; ".join(parts)


def _offline_guides() -> List[Path]:
    paths = sorted(GUIDES.glob("*.md")) + sorted(GUIDES.glob("*.json"))
    return [p for p in paths if p.name not in URL_EXEMPT_GUIDES]


# --------------------------------------------------------------------------
# both guides
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["03-install-the-applications.md", "04-measure-your-robot.md",
                                  "04-measure-your-robot.fields.json"])
def test_guides_have_no_external_urls(name: str) -> None:
    path = GUIDES / name
    text = _read(path)
    hits = []
    for number, line in enumerate(text.splitlines(), start=1):
        for label, pattern in URL_PATTERNS:
            match = pattern.search(line)
            if match:
                hits.append("line %d: %s %r" % (number, label, match.group(0)))
    assert not hits, "%s must work offline at the robot, without links:\n%s" % (name, "\n".join(hits))


def test_every_offline_field_guide_has_no_external_urls() -> None:
    """The same rule for every field guide except the listed exemptions, so new guides inherit it."""
    offline = _offline_guides()
    assert INSTALL_GUIDE in offline and MEASURE_GUIDE in offline and FIELDS_JSON in offline
    hits = []
    for path in offline:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for label, pattern in URL_PATTERNS:
                match = pattern.search(line)
                if match:
                    hits.append("%s line %d: %s %r" % (path.name, number, label, match.group(0)))
    assert not hits, "field guides are read offline at the robot:\n" + "\n".join(hits)


def test_url_exemptions_name_existing_guides() -> None:
    for name, reason in URL_EXEMPT_GUIDES.items():
        assert (GUIDES / name).is_file(), "stale URL exemption for %s" % name
        assert reason.strip(), "URL exemption for %s needs a reason" % name


def test_url_patterns_catch_planted_links() -> None:
    planted = [
        "see https://example.invalid/page",
        "get it from www.example.invalid",
        "download it from python.org",
        "write to mailto:someone",
        "[link](//example.invalid/x)",
    ]
    for line in planted:
        assert any(p.search(line) for _, p in URL_PATTERNS), line
    harmless = ["see [field guide 02](02-first-probe.md)", "machine.toml", "goliath_link.probe", "e.g. 3.11"]
    for line in harmless:
        assert not any(p.search(line) for _, p in URL_PATTERNS), line


@pytest.mark.parametrize("path", [INSTALL_GUIDE, MEASURE_GUIDE], ids=lambda p: p.name)
def test_guides_carry_spdx_header(path: Path) -> None:
    first_line = _read(path).splitlines()[0]
    assert first_line == SPDX_MARKDOWN


def test_fields_json_carries_spdx_identifier() -> None:
    assert _load_fields().get("spdx") == SPDX_JSON


@pytest.mark.parametrize("path", [INSTALL_GUIDE, MEASURE_GUIDE, FIELDS_JSON], ids=lambda p: p.name)
def test_guides_never_mention_the_wifi_password(path: Path) -> None:
    text = _read(path).lower()
    for word in ("password", "passphrase", "wpa key", "wi-fi key", "wifi key", "network key"):
        assert word not in text, "%s mentions %r: the Wi-Fi key is never written into the repository" % (
            path.name, word)


@pytest.mark.parametrize("path", [INSTALL_GUIDE, MEASURE_GUIDE], ids=lambda p: p.name)
def test_guide_relative_links_resolve(path: Path) -> None:
    text = _read(path)
    missing = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        target = target.split("#", 1)[0]
        if not target:
            continue
        if not (path.parent / target).resolve().exists():
            missing.append(target)
    assert not missing, "%s links to missing files: %s" % (path.name, missing)


def test_readme_lists_both_guides() -> None:
    readme = _read(REPO / "README.md")
    for path in (INSTALL_GUIDE, MEASURE_GUIDE):
        rel = path.relative_to(REPO).as_posix()
        assert "](%s)" % rel in readme, "README.md does not link %s" % rel


# --------------------------------------------------------------------------
# field guide 03: install
# --------------------------------------------------------------------------


def test_install_commands_match_package_names() -> None:
    lines = _pip_install_lines(_read(INSTALL_GUIDE))
    assert lines, "field guide 03 has no pip install command"
    expected_folders = set(APP_FOLDERS)
    app_install_languages = set()
    for language, line in lines:
        args = _install_args(line)
        editable: List[str] = []
        others: List[str] = []
        i = 0
        while i < len(args):
            token = args[i]
            if token in ("-e", "--editable"):
                assert i + 1 < len(args), "'-e' without a path in: %s" % line
                editable.append(_folder_of(args[i + 1]))
                i += 2
                continue
            if token.startswith("-"):
                assert token in ("--upgrade", "-U"), "unexpected pip option %r in: %s" % (token, line)
            else:
                others.append(token)
            i += 1
        if editable:
            app_install_languages.add(language)
            assert not others, "packages by name next to the local folders in: %s" % line
            assert sorted(editable) == sorted(expected_folders), (
                "the three packages must be installed together from their folders: %s" % line)
            for folder in editable:
                assert (REPO / folder).is_dir() or folder in PLANNED_NAMES, "no folder %s" % folder
                assert _distribution_name(folder) == folder, (
                    "folder %s holds distribution %r" % (folder, _distribution_name(folder)))
        else:
            # Before the release nothing may be installed by name from the
            # internet: only pip itself is upgraded.
            assert others == ["pip"], "only 'pip install --upgrade pip' may name a package: %s" % line
    assert app_install_languages >= {"bash", "powershell"}, (
        "expected the three-package install for Linux (bash) and for Windows (powershell), found %s"
        % sorted(app_install_languages))


def test_install_guide_has_linux_and_windows_commands() -> None:
    blocks = _code_blocks(_read(INSTALL_GUIDE))
    for language in ("bash", "powershell"):
        bodies = "\n".join(body for lang, body in blocks if lang == language)
        assert bodies, "no %s block" % language
        assert re.search(r"-m venv\b", bodies), "%s: no venv creation" % language
        assert re.search(r"pip install --upgrade pip", bodies), "%s: no pip upgrade" % language
        assert re.search(r"pip install -e \S+ -e \S+ -e \S+", bodies), "%s: no three-package install" % language
        assert re.search(r"goliath-run doctor\b", bodies), "%s: no doctor run" % language


def test_install_guide_uses_its_own_venv() -> None:
    """The applications get a new venv; the probe's goliath-link/.venv is never used."""
    venv_paths = []
    for _language, body in _code_blocks(_read(INSTALL_GUIDE)):
        venv_paths.extend(re.findall(r"[A-Za-z0-9_./\\-]*\.venv[A-Za-z0-9_./\\-]*", body))
    assert venv_paths, "field guide 03 creates no venv"
    for raw in venv_paths:
        path = raw.replace("\\", "/")
        assert path.startswith(APP_VENV), "code in field guide 03 uses %r, not %s" % (raw, APP_VENV)


def test_install_guide_doctor_runs_the_installed_console_script() -> None:
    for language, body in _code_blocks(_read(INSTALL_GUIDE)):
        for line in body.splitlines():
            if "doctor" not in line:
                continue
            command = line.split()[0].replace("\\", "/")
            if language == "bash":
                assert command == APP_VENV + "/bin/goliath-run", line
            elif language == "powershell":
                assert command == APP_VENV + "/Scripts/goliath-run", line


def test_install_guide_python_minimum_matches_packages() -> None:
    text = _read(INSTALL_GUIDE)
    assert "Python 3.11 or newer" in text
    for folder in ("goliath-prep", "goliath-run"):
        pyproject = REPO / folder / "pyproject.toml"
        if pyproject.is_file():
            project = _toml_table(pyproject.read_text(encoding="utf-8"), "project") or {}
            requires = project.get("requires-python", "")
        else:
            requires = PLANNED_REQUIRES_PYTHON
        assert re.match(r"^>=\s*3\.11(?:\D|$)", requires), (
            "%s requires-python is %r but field guide 03 says Python 3.11 or newer" % (folder, requires))


def _runner_has_doctor() -> bool:
    """True once R07's CLI source defines a 'doctor' command (a skeleton cli.py does not count)."""
    if not R07_CLI.is_file():
        return False
    return re.search(r"\bdoctor\b", R07_CLI.read_text(encoding="utf-8")) is not None


DOCTOR_MISSING_RE = re.compile(r"`goliath-run doctor`(?:\s|>)+does not exist yet")


def install_banner_problems(text: str, doctor_exists: bool, console_scripts: Optional[Dict[str, str]]) -> List[str]:
    """What is wrong with field guide 03's release banner, given the state of the runner.

    Before R07 defines 'doctor', the top of the guide must say that the
    applications are not released and that doctor does not exist yet. Once
    doctor exists, the goliath-run console script must exist and the guide must
    no longer claim that doctor does not exist (a stale banner is a false
    statement to the owner, not a harmless leftover).
    """
    problems = []
    head = "\n".join(text.splitlines()[:30])
    if not doctor_exists:
        if not re.search(r"not released yet", head, re.IGNORECASE):
            problems.append("goliath-run has no 'doctor' command yet, so the top of field guide 03 must say "
                            "that the applications are not released yet")
        if "feat/prep-and-run" not in head:
            problems.append("the banner must name the branch feat/prep-and-run")
        if "02-first-probe.md" not in head:
            problems.append("the banner must point to field guide 02, the only installable tool")
        if not DOCTOR_MISSING_RE.search(head):
            problems.append("the banner must say that goliath-run doctor does not exist yet")
    else:
        if not console_scripts or "goliath-run" not in console_scripts:
            problems.append("the runner CLI has a doctor command, so goliath-run/pyproject.toml must define the "
                            "goliath-run console script that field guide 03 runs")
        if DOCTOR_MISSING_RE.search(text):
            problems.append("the runner CLI now defines doctor, but field guide 03 still says that "
                            "goliath-run doctor does not exist yet: update the banner")
    return problems


def test_install_guide_says_not_released_until_doctor_exists() -> None:
    text = _read(INSTALL_GUIDE)
    doctor_exists = _runner_has_doctor()
    scripts = None
    if doctor_exists:
        scripts = _toml_table(_read(REPO / "goliath-run" / "pyproject.toml"), "project.scripts")
    problems = install_banner_problems(text, doctor_exists, scripts)
    assert not problems, "\n".join(problems)


def test_install_banner_check_flags_a_stale_doctor_sentence() -> None:
    """The banner check must fail once doctor exists if the guide still says it does not."""
    current = _read(INSTALL_GUIDE)
    assert install_banner_problems(current, False, None) == []
    scripts = {"goliath-run": "goliath_run.cli:main"}
    stale = install_banner_problems(current, True, scripts)
    assert any("still says" in p for p in stale), stale
    updated = DOCTOR_MISSING_RE.sub("`goliath-run doctor` checks the install offline", current)
    assert install_banner_problems(updated, True, scripts) == []
    assert any("console script" in p for p in install_banner_problems(updated, True, {}))
    assert install_banner_problems("# no banner\n", False, None), "a guide without the banner must be flagged"


def _redirect_targets(line: str) -> List[str]:
    """Files a shell line writes to with '>' (bash) or '*>' (PowerShell); '2>&1' is not a file."""
    return [m.group(1) for m in re.finditer(r"(?:^|\s)(?:\d|\*)?>>?\s*(?!&)(\S+)", line)]


def test_redirect_target_reader() -> None:
    assert _redirect_targets("a doctor > ~/x.txt 2>&1") == ["~/x.txt"]
    assert _redirect_targets("a doctor *> $HOME\\x.txt") == ["$HOME\\x.txt"]
    assert _redirect_targets("a doctor > doctor.txt 2>&1") == ["doctor.txt"]
    assert _redirect_targets("pip install -e ./goliath-run") == []


def test_install_guide_saves_output_outside_the_repository() -> None:
    """The doctor output may name the robot (its profile is per serial): it must not land in the checkout.

    The commands run from the repository folder, where a plain 'doctor.txt' is
    not ignored by git (.gitignore ignores probe-results/ for the same reason).
    """
    targets = []
    for _language, body in _code_blocks(_read(INSTALL_GUIDE)):
        for line in body.splitlines():
            targets.extend(_redirect_targets(line))
    assert targets, "field guide 03 no longer saves the doctor output to a file"
    inside = [t for t in targets if not (t.startswith("~/") or t.startswith("$HOME"))]
    assert not inside, "field guide 03 writes %s inside the repository folder; use the home folder" % inside


def test_toml_table_reader() -> None:
    text = '[project]\nname = "goliath-run"\nrequires-python = ">=3.11"\n\n[project.scripts]\n' \
           '"goliath-run" = "goliath_run.cli:main"\n[tool.x]\nname = "other"\n'
    assert _toml_table(text, "project") == {"name": "goliath-run", "requires-python": ">=3.11"}
    assert _toml_table(text, "project.scripts") == {"goliath-run": "goliath_run.cli:main"}
    assert _toml_table(text, "absent") is None


def test_code_block_reader_handles_indented_fences() -> None:
    text = "a\n```bash\nls\n```\n1. step\n\n   ```powershell\n   dir x\n   ```\n"
    assert _code_blocks(text) == [("bash", "ls\n"), ("powershell", "   dir x\n")]


# --------------------------------------------------------------------------
# field guide 04: R0b fields
# --------------------------------------------------------------------------


def test_r0b_field_ids_unique() -> None:
    ids = _field_ids()
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, "field ids repeat: %s" % duplicates


def test_r0b_fields_well_formed() -> None:
    problems = []
    for field in _field_list():
        missing = [key for key in REQUIRED_KEYS if key not in field]
        if missing:
            problems.append("%r misses %s" % (field.get("id"), missing))
            continue
        field_id = field["id"]
        if not FIELD_ID_RE.match(field_id):
            problems.append("%r is not a dotted lower-case id" % field_id)
        kind, unit = field["kind"], field["unit"]
        if kind not in KINDS:
            problems.append("%s: unknown kind %r" % (field_id, kind))
        for key in ("meaning", "how_measured", "feeds"):
            if not isinstance(field[key], str) or not field[key].strip():
                problems.append("%s: empty %s" % (field_id, key))
        if not isinstance(field["required"], bool) or not isinstance(field["measurement_entry"], bool):
            problems.append("%s: 'required' and 'measurement_entry' must be true or false" % field_id)
        if not isinstance(field["refs"], list) or not all(isinstance(r, str) and REF_RE.match(r)
                                                           for r in field["refs"]):
            problems.append("%s: refs must be a list of U-nn / Qn ids" % field_id)
        suffix_unit = next((u for s, u in UNIT_SUFFIXES.items() if field_id.endswith(s)), None)
        if kind == "number":
            if unit not in NUMBER_UNITS:
                problems.append("%s: number with unit %r" % (field_id, unit))
            if unit in UNIT_SUFFIXES.values() and suffix_unit != unit:
                problems.append("%s: unit %s but the id does not end in _%s" % (field_id, unit, unit))
            if unit == "count" and suffix_unit is not None:
                problems.append("%s: a count must not carry a unit suffix" % field_id)
        else:
            if unit is not None:
                problems.append("%s: a %s field has no unit (null)" % (field_id, kind))
            if suffix_unit is not None:
                problems.append("%s: only numbers end in a unit suffix" % field_id)
        if kind == "choice":
            choices = field.get("choices")
            if not (isinstance(choices, list) and len(choices) >= 2 and len(set(choices)) == len(choices)
                    and all(isinstance(c, str) and re.fullmatch(r"[a-z][a-z0-9_]*", c) for c in choices)):
                problems.append("%s: choices must be distinct lower-case words" % field_id)
            elif "unknown" not in choices:
                problems.append("%s: 'unknown' must always be a possible answer" % field_id)
        elif "choices" in field:
            problems.append("%s: only choice fields have choices" % field_id)
        if "default" in field:
            problems.append("%s: fields have no default value; the owner writes a value or 'unknown'" % field_id)
    assert not problems, "\n".join(problems)


def test_r0b_fields_name_the_lead_examples_and_design_facts() -> None:
    ids = set(_field_ids())
    for required in ("footprint.q1_mm", "footprint.q1_deg", "footprint.q2_mm", "footprint.q2_deg",
                     "footprint.q3_mm", "footprint.q3_deg", "wheel.q1.contact_extent_mm",
                     "wheel.q2.contact_extent_mm", "wheel.q3.contact_extent_mm", "wheel.q1.contact_radius_mm",
                     "wire.attachment", "wire.swivel", "stop.kind", "stop.cuts", "router.own_switch",
                     "tower.base_radius_mm", "body.max_radius_mm"):
        assert required in ids, required


def test_r0b_how_measured_cites_existing_steps() -> None:
    guide = _read(MEASURE_GUIDE)
    steps = set(re.findall(r"^## (\d+)\. ", guide, re.MULTILINE))
    assert steps, "field guide 04 has no numbered steps"
    for field in _field_list():
        cited = re.findall(r"\bsteps? (\d+)", field["how_measured"])
        assert cited, "%s: how_measured names no step of the guide" % field["id"]
        for number in cited:
            assert number in steps, "%s cites step %s, which field guide 04 does not have" % (field["id"], number)


def test_r0b_fill_in_table_matches_fields_json() -> None:
    rows = _fill_in_rows()
    json_ids = _field_ids()
    only_json = sorted(set(json_ids) - set(rows))
    only_table = sorted(set(rows) - set(json_ids))
    assert not only_json and not only_table, (
        "fill-in table and fields.json differ: only in json %s; only in table %s" % (only_json, only_table))
    for field in _field_list():
        cells = rows[field["id"]]
        assert len(cells) >= 4, "%s: table row too short" % field["id"]
        answer_cell = cells[2]
        if field["kind"] == "choice":
            listed = [c.strip() for c in answer_cell.replace("`", "").split("/")]
            assert listed == field["choices"], "%s: table offers %s, fields.json %s" % (
                field["id"], listed, field["choices"])
        elif field["kind"] == "number":
            assert answer_cell == field["unit"], "%s: table unit %r, fields.json %r" % (
                field["id"], answer_cell, field["unit"])
        else:
            assert answer_cell == "text", "%s: table should say 'text', says %r" % (field["id"], answer_cell)


def test_r0b_guide_mentions_only_known_field_ids() -> None:
    ids = set(_field_ids())
    prefixes = {i.split(".", 1)[0] for i in ids}
    unknown = set()
    for token in re.findall(r"`([a-z][a-z0-9_]*(?:\.[a-z0-9][a-z0-9_]*)+)`", _read(MEASURE_GUIDE)):
        if token.split(".", 1)[0] in prefixes and token not in ids:
            unknown.add(token)
    assert not unknown, "field guide 04 mentions ids that fields.json does not define: %s" % sorted(unknown)


def test_r0b_expected_values_follow_spec08() -> None:
    fields = {f["id"]: f for f in _field_list()}
    points = _vendor_points()
    expected: Dict[str, float] = {}
    for name, (back, right) in points.items():
        expected["footprint.%s_back_mm" % name] = back
        expected["footprint.%s_right_mm" % name] = right
        expected["footprint.%s_mm" % name] = math.hypot(back, right)
        expected["footprint.%s_deg" % name] = math.degrees(math.atan2(right, back))
    for a, b in (("q1", "q2"), ("q2", "q3"), ("q3", "q1")):
        expected["footprint.%s_%s_mm" % (a, b)] = math.dist(points[a], points[b])
    for field_id, value in expected.items():
        assert field_id in fields, field_id
        stated = fields[field_id].get("expected")
        assert isinstance(stated, dict), "%s has no expected value" % field_id
        assert abs(stated["value"] - value) <= 0.06, "%s: fields.json says %s, spec 08 gives %.2f" % (
            field_id, stated["value"], value)
        assert "spec 08" in stated["source"], field_id


def test_r0b_go_tolerances_on_distances_and_angles() -> None:
    fields = {f["id"]: f for f in _field_list()}
    for name in ("q1", "q2", "q3"):
        assert fields["footprint.%s_mm" % name]["expected"]["tolerance"] == GO_DISTANCE_TOLERANCE_MM
        assert fields["footprint.%s_deg" % name]["expected"]["tolerance"] == GO_ANGLE_TOLERANCE_DEG


def test_r0b_go_table_matches_expected_values_and_tolerances() -> None:
    fields = {f["id"]: f for f in _field_list()}
    rows = _table_rows(_section(_read(MEASURE_GUIDE), "go / no-go"))
    checked = set()
    for cells in rows:
        field_id = cells[0].strip("`")
        stated = fields[field_id]["expected"]
        bounds = [float(x) for x in re.findall(r"[-+]?\d+(?:\.\d+)?", cells[2].replace("−", "-"))]
        assert len(bounds) == 2, "%s: cannot read the range %r" % (field_id, cells[2])
        assert bounds == [stated["value"] - stated["tolerance"], stated["value"] + stated["tolerance"]], (
            "%s: the go range %r is not %s ± %s" % (field_id, cells[2], stated["value"], stated["tolerance"]))
        checked.add(field_id)
    with_tolerance = {i for i, f in fields.items() if "tolerance" in f.get("expected", {})}
    assert checked == with_tolerance, "the go table must list exactly the fields with a go tolerance"


def test_r0b_table_shows_the_expected_values() -> None:
    rows = _fill_in_rows()
    for field in _field_list():
        stated = field.get("expected")
        if not stated:
            continue
        cell = rows[field["id"]][-1].replace("−", "-")
        rounded = int(round(stated["value"]))
        pattern = r"(?<![0-9-])" + re.escape(str(rounded)) + r"(?![0-9])"
        assert re.search(pattern, cell), (
            "%s: the table's vendor-model cell %r does not show %d" % (field["id"], cell, rounded))
        if "tolerance" in stated:
            assert "± %d" % int(stated["tolerance"]) in cell, "%s: tolerance missing in %r" % (field["id"], cell)


def test_r0b_guide_explains_why_the_wheel_contact_matters() -> None:
    text = _read(MEASURE_GUIDE)
    head = "\n".join(text.splitlines()[:45])
    for needed in ("40 mm", "15 mm", "200 mm"):
        assert needed in head, "the opening of field guide 04 should state %r (design 4.5, 4.20)" % needed


def test_r0b_says_the_applications_are_not_released_yet() -> None:
    """Guide 04 describes the checker's use of the numbers; it must not read as if the checker exists."""
    head = "\n".join(_read(MEASURE_GUIDE).splitlines()[:40])
    assert re.search(r"not released yet", head, re.IGNORECASE), (
        "the opening of field guide 04 must say that goliath-prep and goliath-run are not released yet")
    assert "03-install-the-applications.md" in head


def test_r0b_go_check_compares_unrounded_values() -> None:
    """Rounding before the go check can turn a no-go into a go.

    Design 11.2 R0b: angles within +-5 deg of 76. A computed 81.4 deg is a
    no-go; rounded to the nearest degree it reads 81 and passes "+71 to +81".
    """
    text = _read(MEASURE_GUIDE)
    assert not re.search(r"nearest\s+(?:whole\s+)?degree", text, re.IGNORECASE), (
        "field guide 04 must not round angles to whole degrees before the go / no-go check")
    assert re.search(r"angles\s+to\s+one\s+decimal\s+place", text, re.IGNORECASE)
    go = _section(text, "go / no-go")
    assert re.search(r"without\s+rounding", go, re.IGNORECASE), (
        "the go / no-go step must say that values are compared as written, without rounding")
    # The worked example in the go step must itself be a no-go under the JSON tolerances.
    fields = {f["id"]: f for f in _field_list()}
    example = re.search(r"([0-9]+\.[0-9])\s*°\s+is\s+outside", go)
    assert example, "the go step should show an angle just outside its range as an example"
    stated = fields["footprint.q2_deg"]["expected"]
    assert abs(float(example.group(1)) - stated["value"]) > stated["tolerance"]
    assert round(float(example.group(1))) <= stated["value"] + stated["tolerance"], (
        "the example should be one that rounding would wrongly let pass")


def _bullet(section: str, label: str) -> str:
    """The text of the list item that starts with '- **<label>:**', joined across its wrapped lines."""
    lines = section.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("- **%s:**" % label):
            item = [line]
            for follow in lines[i + 1:]:
                if not follow.startswith("  "):
                    break
                item.append(follow.strip())
            return " ".join(item)
    raise AssertionError("no '- **%s:**' item" % label)


def test_r0b_go_requires_the_body_axis_check() -> None:
    """The angles are about the drawn axis; if it is not the robot's centre line, the go cannot be claimed.

    fields.json says 'no' or 'unknown' in footprint.axis_matches_body goes to
    design review; the guide's go / no-go step must say the same, or an
    unconfirmed axis would be read as 'yes'.
    """
    go = _section(_read(MEASURE_GUIDE), "go / no-go")
    go_item = _bullet(go, "Go")
    nogo_item = _bullet(go, "No-go")
    assert "`footprint.axis_matches_body` is `yes`" in go_item, go_item
    assert "`footprint.axis_matches_body`" in nogo_item and "`no`" in nogo_item and "`unknown`" in nogo_item, (
        nogo_item)
    feeds = {f["id"]: f for f in _field_list()}["footprint.axis_matches_body"]["feeds"]
    assert "no-go" in feeds and "step 13" in feeds, feeds


@pytest.mark.parametrize("rule", ["by the spindle", "unplug", "bit holder", "Remove the bit"])
def test_r0b_guide_states_the_safety_rules(rule: str) -> None:
    safety = _section(_read(MEASURE_GUIDE), "Safety")
    assert rule.lower() in safety.lower(), "the safety section of field guide 04 does not say %r" % rule


# --------------------------------------------------------------------------
# R07 handshake (activated by unit R07)
# --------------------------------------------------------------------------


def test_r0b_field_ids_match_r07_measurement_ids() -> None:
    if not R07_RUNGS.is_file():
        pytest.skip(
            "activated by unit R07: %s does not exist yet, so there is no R0b measurement entry "
            "to compare field guide 04's ids with" % R07_RUNGS.relative_to(REPO).as_posix())
    if sys.version_info < (3, 11):
        pytest.skip("R07 exists, but goliath_run needs Python >= 3.11 and this is %d.%d; "
                    "the check runs in the >= 3.11 runs" % sys.version_info[:2])
    try:
        module = importlib.import_module("goliath_run.rungs")
    except ImportError as exc:
        pytest.fail(
            "R07 exists (%s) but goliath_run.rungs cannot be imported here (%r). Install goliath-link, "
            "goliath-prep and goliath-run into the environment that runs the root tests, or move this "
            "check into goliath-run's own tests." % (R07_RUNGS.relative_to(REPO).as_posix(), exc))
    guide_ids = {f["id"] for f in _field_list() if f["measurement_entry"]}
    try:
        r07_ids = r07_r0b_measurement_ids(module)
    except LookupError as exc:
        pytest.fail(str(exc))
    mismatch = describe_id_mismatch(guide_ids, r07_ids)
    assert not mismatch, mismatch


def _fake_module(**attrs: object) -> types.ModuleType:
    module = types.ModuleType("fake_rungs")
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _measurement(**attrs: object) -> types.SimpleNamespace:
    return types.SimpleNamespace(**attrs)


def test_r07_lookup_reads_a_rungs_mapping() -> None:
    rung = types.SimpleNamespace(id="R0b", measurements=[_measurement(id="a.b_mm"), _measurement(id="wire.attachment")])
    other = types.SimpleNamespace(id="R1b", measurements=[_measurement(id="x.y")])
    module = _fake_module(RUNGS={"R0b": rung, "R1b": other})
    assert r07_r0b_measurement_ids(module) == {"a.b_mm", "wire.attachment"}


def test_r07_lookup_finds_a_module_level_definition_and_fact_ids() -> None:
    rung = types.SimpleNamespace(id="R0b", measurements=(_measurement(fact_id="stop.kind"),))
    module = _fake_module(R0B=rung, RUNGS=[rung])
    assert r07_r0b_measurement_ids(module) == {"stop.kind"}


@pytest.mark.parametrize(
    "module",
    [
        _fake_module(),
        _fake_module(RUNGS={"R1b": types.SimpleNamespace(id="R1b", measurements=[])}),
        _fake_module(A=types.SimpleNamespace(id="R0b", measurements=[]),
                     B=types.SimpleNamespace(id="R0b", measurements=[])),
        _fake_module(RUNGS={"R0b": types.SimpleNamespace(id="R0b", measurements=[_measurement(id="a.b"),
                                                                                  _measurement(id="a.b")])}),
        _fake_module(RUNGS={"R0b": types.SimpleNamespace(id="R0b", measurements=[_measurement(name="a.b")])}),
        _fake_module(RUNGS={"R0b": types.SimpleNamespace(id="R0b")}),
    ],
    ids=["empty", "no-r0b", "two-r0b", "duplicate-ids", "no-id", "no-measurements"],
)
def test_r07_lookup_refuses_what_it_cannot_read(module: types.ModuleType) -> None:
    with pytest.raises(LookupError):
        r07_r0b_measurement_ids(module)


def test_describe_id_mismatch_names_both_sides() -> None:
    assert describe_id_mismatch({"a.b", "c.d"}, {"a.b", "c.d"}) == ""
    message = describe_id_mismatch({"a.b", "c.d"}, {"a.b", "e.f"})
    assert "not in R07" in message and "c.d" in message
    assert "not in field guide 04" in message and "e.f" in message
