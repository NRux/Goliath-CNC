# SPDX-License-Identifier: GPL-3.0-or-later
"""Repository-level checks on the documentation and the licensing table.

These tests guard three things:

* the corrected protocol specification (docs/protocol 01-09 and its README) is
  in the repository, so every component cites in-repository text;
* LICENSING.md names a licence for every top-level component, including the
  ones that are planned but not created yet, and that licence agrees with the
  packaging metadata;
* no document cites a build-session scratchpad, a temporary directory or a
  worktree path, which a reader of the published repository cannot open.

Python 3.9 compatible on purpose: the root suite runs on 3.9 in CI.

Clean room: nothing here opens, lists or reads anything inside the vendor
folder (``old_software/``). Only its name, as an entry of the repository root,
is looked at. git is asked only about folders that .gitignore matches, never
about the vendor folder.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import warnings
from pathlib import Path
from typing import Dict, Iterator, List, NamedTuple, Optional, Set, Tuple

import pytest

REPO = Path(__file__).resolve().parent.parent
LICENSING = REPO / "LICENSING.md"
PROTOCOL_DIR = REPO / "docs" / "protocol"

# The vendor folder. Never descended into, never opened, never listed.
VENDOR_DIRS = frozenset({"old_software"})

# Folders the build design plans but that may not exist yet. LICENSING.md fixes
# their licence in advance, so the first file in them gets the right header.
PLANNED_COMPONENTS = frozenset(
    {
        "goliath-prep/",
        "goliath-run/",
        "posts/",
        "docs/prep/",
        "docs/user-guide/",
        "packaging/",
    }
)

# The licence each component must carry (build design, section 9.2).
EXPECTED_LICENCE = {
    "goliath-link/": "Apache-2.0",
    "docs/protocol/": "Apache-2.0",
    "docs/prep/": "Apache-2.0",
    # Recommended; the owner decides (owner question Q13). The row must say so.
    "posts/": "Apache-2.0",
    "goliath-prep/": "GPL-3.0-or-later",
    "goliath-run/": "GPL-3.0-or-later",
    # Under the root licence unless the owner decides otherwise (Q13).
    "docs/field-guide/": "GPL-3.0-or-later",
    "docs/user-guide/": "GPL-3.0-or-later",
    "docs/research/": "GPL-3.0-or-later",
    "tools/": "GPL-3.0-or-later",
    "tests/": "GPL-3.0-or-later",
    "packaging/": "GPL-3.0-or-later",
}
ROOT_LICENCE = "GPL-3.0-or-later"
ROOT_ROW_MARKER = "repository root"

# Rows whose licence is still an open owner decision (question Q13).
PENDING_OWNER_DECISION = ("posts/", "docs/field-guide/", "docs/user-guide/")

_SPDX_RE = re.compile(
    r"(?<![\w.-])("
    r"Apache-2\.0"
    r"|(?:A|L)?GPL-[23]\.0(?:-or-later|-only)?"
    r"|MPL-2\.0|MIT|BSD-[0-9]-Clause|CC-BY(?:-SA)?-4\.0|CC0-1\.0"
    r")(?![\w.-])"
)

# What a citation of a non-repository location looks like.
FORBIDDEN_CITATIONS = (
    ("build-session scratchpad", re.compile(r"scratchpad[/\\]", re.IGNORECASE)),
    ("scratchpad brief", re.compile(r"fw-facts-brief", re.IGNORECASE)),
    # The build design's short form for that brief (design section 0.2).
    ("scratchpad brief", re.compile(r"\bbrief\s*§", re.IGNORECASE)),
    ("scratchpad design document", re.compile(r"\b(?:design|work-units)-final\b", re.IGNORECASE)),
    ("scratchpad spec worktree", re.compile(r"wt-docs[/\\]", re.IGNORECASE)),
    ("temporary directory", re.compile(r"/tmp/")),
    ("temporary directory", re.compile(r"\btmp[/\\]claude-", re.IGNORECASE)),
    ("Windows temporary directory", re.compile(r"AppData[/\\]Local[/\\]Temp", re.IGNORECASE)),
    ("git worktree", re.compile(r"\.claude[/\\]worktrees", re.IGNORECASE)),
)

# Files under docs/ that are not text. Everything else must decode as UTF-8.
BINARY_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".bmp", ".tif", ".tiff", ".zip", ".gz"}
)

# Generated or tool-installed directories inside a component: the READMEs in
# them belong to third-party packages, not to this repository.
GENERATED_DIR_NAMES = frozenset({"__pycache__", "node_modules", "build", "dist", "venv"})

# Hidden top-level folders whose READMEs are repository documentation.
HIDDEN_DOC_DIRS = (".github",)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class Row(NamedTuple):
    line_no: int
    text: str
    paths: Tuple[str, ...]
    licences: Tuple[str, ...]


def _pruned_in_component(name: str) -> bool:
    """Folders not searched for READMEs inside a component (or at the root)."""
    return (
        name.startswith(".")
        or name in GENERATED_DIR_NAMES
        or name in VENDOR_DIRS
        or name.endswith(".egg-info")
    )


def _pruned_in_docs(name: str) -> bool:
    """Folders not scanned inside docs/: only hidden ones and bytecode caches."""
    return name.startswith(".") or name == "__pycache__"


def _split_cells(line: str) -> List[str]:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        raise ValueError("not a table row: %r" % line)
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def parse_component_table(text: str) -> List[Row]:
    """Parse the component table of LICENSING.md.

    The table is the first Markdown table whose header has a "Path" and a
    "Licence" column. Each row's paths are the code spans of its Path cell;
    its licences are the SPDX identifiers in its Licence cell.
    """
    lines = text.splitlines()
    header_at = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("|"):
            names = [c.lower() for c in _split_cells(line)]
            if "path" in names and "licence" in names:
                header_at = i
                break
    if header_at is None:
        raise ValueError("LICENSING.md has no table with a 'Path' and a 'Licence' column")
    names = [c.lower() for c in _split_cells(lines[header_at])]
    path_col, licence_col = names.index("path"), names.index("licence")
    separator = lines[header_at + 1] if header_at + 1 < len(lines) else ""
    if not re.fullmatch(r"\s*\|(\s*:?-{3,}:?\s*\|)+\s*", separator):
        raise ValueError("table header on line %d has no separator row" % (header_at + 1))

    rows = []
    for offset, line in enumerate(lines[header_at + 2:], start=header_at + 3):
        if not line.lstrip().startswith("|"):
            break
        cells = _split_cells(line)
        if len(cells) != len(names):
            raise ValueError("line %d has %d cells, the header has %d" % (offset, len(cells), len(names)))
        paths = tuple(re.findall(r"`([^`]+)`", cells[path_col]))
        licences = tuple(dict.fromkeys(_SPDX_RE.findall(cells[licence_col])))
        rows.append(Row(offset, line, paths, licences))
    if not rows:
        raise ValueError("the component table in LICENSING.md has no rows")
    return rows


def _licensing_rows() -> List[Row]:
    return parse_component_table(LICENSING.read_text(encoding="utf-8"))


def _row_for(rows: List[Row], path: str) -> Optional[Row]:
    found = [r for r in rows if path in r.paths]
    if len(found) > 1:
        pytest.fail(
            "LICENSING.md lists %s on more than one row (lines %s)" % (path, [r.line_no for r in found])
        )
    return found[0] if found else None


def _root_row(rows: List[Row]) -> Row:
    found = [r for r in rows if ROOT_ROW_MARKER in r.text.lower()]
    if len(found) != 1:
        pytest.fail(
            "LICENSING.md needs exactly one row covering the %r (found %d)" % (ROOT_ROW_MARKER, len(found))
        )
    return found[0]


def _the_licence(row: Row) -> str:
    if len(row.licences) != 1:
        pytest.fail(
            "LICENSING.md line %d must name exactly one SPDX licence in its Licence cell, found %s: %s"
            % (row.line_no, list(row.licences) or "none", row.text)
        )
    return row.licences[0]


def gitignore_patterns(repo: Path) -> List[str]:
    """Top-level-applicable patterns from the repository's .gitignore.

    Negations are refused rather than ignored: honouring them needs a full
    gitignore engine, and skipping them would hide a directory from the check.
    """
    path = repo / ".gitignore"
    if not path.is_file():
        return []
    patterns = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            raise ValueError(
                ".gitignore negation %r is not supported by tests/test_repo_docs.py; "
                "extend gitignore_patterns() before using one" % line
            )
        body = line.lstrip("/").rstrip("/")
        if "/" in body:
            continue  # anchored below the top level
        patterns.append(body)
    return patterns


def _git_for(repo: Path) -> Optional[str]:
    """The git executable, when repo is the top of a git work tree; else None."""
    git = shutil.which("git")
    if git is None:
        return None
    proc = subprocess.run([git, "-C", str(repo), "rev-parse", "--show-toplevel"], capture_output=True)
    if proc.returncode != 0:
        return None
    top = Path(os.fsdecode(proc.stdout).strip())
    return git if top.resolve() == repo.resolve() else None


def git_tracks_files_in(repo: Path, rel: str) -> Optional[bool]:
    """Whether git tracks any file under rel/ (True or False).

    None when that cannot be known: repo is not the top of a git work tree, or
    git is not installed or fails. Never called for the vendor folder.
    """
    if rel.split("/", 1)[0] in VENDOR_DIRS:
        raise AssertionError("git must not be asked about the vendor folder: %s" % rel)
    git = _git_for(repo)
    if git is None:
        return None
    proc = subprocess.run(
        [git, "--literal-pathspecs", "-C", str(repo), "ls-files", "-z", "--", rel.rstrip("/") + "/"],
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    return bool(proc.stdout.strip(b"\0"))


def _ignored_and_unpublished(repo: Path, rel: str, name: str, ignored: List[str]) -> bool:
    """True when a folder matches .gitignore and nothing in it is published.

    A folder git tracks files in is published even if .gitignore matches it
    (files added with 'git add -f', as for a capture shared deliberately), so
    it stays a component. When git cannot tell, the folder is skipped on the
    strength of .gitignore alone, with a warning that says so.
    """
    if not any(fnmatch.fnmatch(name, p) for p in ignored):
        return False
    tracked = git_tracks_files_in(repo, rel)
    if tracked is None:
        warnings.warn(
            "%s matches .gitignore and is skipped, but git could not confirm that nothing in it is "
            "tracked (not a git checkout, or git is unavailable); a tracked folder needs a LICENSING.md row"
            % rel,
            UserWarning,
            stacklevel=3,
        )
        return True
    return not tracked


def top_level_components(repo: Path) -> List[str]:
    """Every component folder of the repository, as 'name/' or 'docs/name/'.

    Hidden entries are not components; nor are entries the .gitignore excludes,
    unless git tracks files in them (see _ignored_and_unpublished). docs/ is a
    container: each of its sub-folders is a component of its own. The vendor
    folder is a component whenever it is present; it is reported by name only,
    and nothing inside it is read or passed to git.
    """
    ignored = gitignore_patterns(repo)
    found = []
    for entry in sorted(os.listdir(repo)):
        if entry.startswith(".") or not (repo / entry).is_dir():
            continue
        if entry in VENDOR_DIRS:
            found.append("%s/" % entry)
            continue
        if _ignored_and_unpublished(repo, entry + "/", entry, ignored):
            continue
        if entry == "docs":
            for sub in sorted(os.listdir(repo / "docs")):
                if sub.startswith(".") or not (repo / "docs" / sub).is_dir():
                    continue
                if _ignored_and_unpublished(repo, "docs/%s/" % sub, sub, ignored):
                    continue
                found.append("docs/%s/" % sub)
        else:
            found.append("%s/" % entry)
    return found


def documents_to_scan(repo: Path) -> Iterator[Path]:
    """Every file under docs/; the READMEs of every component and of .github/;
    the root README.md and LICENSING.md. Never anything in the vendor folder."""
    for name in ("README.md", "LICENSING.md"):
        if (repo / name).is_file():
            yield repo / name
    docs = repo / "docs"
    if docs.is_dir():
        for dirpath, dirnames, filenames in os.walk(docs):
            dirnames[:] = sorted(d for d in dirnames if not _pruned_in_docs(d))
            for filename in sorted(filenames):
                yield Path(dirpath) / filename
    readme_roots = [e for e in sorted(os.listdir(repo)) if e != "docs" and not _pruned_in_component(e)]
    readme_roots.extend(HIDDEN_DOC_DIRS)
    for entry in readme_roots:
        if not (repo / entry).is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(repo / entry):
            dirnames[:] = sorted(d for d in dirnames if not _pruned_in_component(d))
            for filename in sorted(filenames):
                if filename.upper().startswith("README"):
                    yield Path(dirpath) / filename


class Citation(NamedTuple):
    path: str
    line_no: int
    kind: str
    line: str


def find_forbidden_citations(repo: Path) -> List[Citation]:
    """Every line of a scanned document that cites a non-repository location.

    A non-binary file that is not UTF-8 raises: it cannot be checked, and
    skipping it would let a citation through unseen.
    """
    hits = []
    for path in documents_to_scan(repo):
        rel = path.relative_to(repo).as_posix()
        if rel.split("/", 1)[0] in VENDOR_DIRS:  # defence in depth: never read vendor files
            raise AssertionError("scanner reached the vendor folder: %s" % rel)
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "%s is not UTF-8 text and has no binary suffix, so it cannot be checked "
                "for scratchpad citations (%s)" % (rel, exc)
            ) from exc
        for line_no, line in enumerate(text.splitlines(), start=1):
            for kind, pattern in FORBIDDEN_CITATIONS:
                if pattern.search(line):
                    hits.append(Citation(rel, line_no, kind, line.strip()))
    return hits


def _relative_links(md_text: str) -> List[str]:
    targets = re.findall(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", md_text)
    return [t for t in targets if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", t) and not t.startswith("#")]


def _pyproject_licence(pyproject: Path) -> str:
    text = pyproject.read_text(encoding="utf-8")
    match = re.search(r'^license\s*=\s*"([^"]+)"', text, re.MULTILINE) or re.search(
        r'^license\s*=\s*\{\s*text\s*=\s*"([^"]+)"', text, re.MULTILINE
    )
    if match is None:
        pytest.fail("%s declares no SPDX licence string (license = \"...\")" % pyproject.relative_to(REPO))
    return match.group(1)


# --------------------------------------------------------------------------
# The corrected protocol specification is in the repository
# --------------------------------------------------------------------------


@pytest.mark.parametrize("number", ["%02d" % n for n in range(1, 10)])
def test_spec_files_present_01_to_09(number: str) -> None:
    matches = sorted(PROTOCOL_DIR.glob("%s-*.md" % number))
    assert len(matches) == 1, "docs/protocol must hold exactly one %s-*.md, found %s" % (
        number,
        [m.name for m in matches],
    )
    text = matches[0].read_text(encoding="utf-8")
    first_line = text.splitlines()[0] if text else ""
    assert first_line.startswith("# %s " % number), (
        "%s must open with its '# %s ...' title, found %r" % (matches[0].name, number, first_line)
    )
    readme = (PROTOCOL_DIR / "README.md").read_text(encoding="utf-8")
    assert "(%s)" % matches[0].name in readme, (
        "docs/protocol/README.md does not link %s from its index" % matches[0].name
    )


def test_protocol_relative_links_resolve() -> None:
    broken = []
    for doc in sorted(PROTOCOL_DIR.glob("*.md")):
        for target in _relative_links(doc.read_text(encoding="utf-8")):
            file_part = target.split("#", 1)[0]
            if file_part and not (doc.parent / file_part).exists():
                broken.append("%s -> %s" % (doc.name, target))
    assert not broken, "broken relative links in docs/protocol: %s" % broken


# --------------------------------------------------------------------------
# LICENSING.md
# --------------------------------------------------------------------------


def test_licensing_table_lists_every_top_level_component() -> None:
    rows = _licensing_rows()
    listed: Dict[str, Row] = {}
    for row in rows:
        for path in row.paths:
            if path in listed:
                pytest.fail("LICENSING.md lists %s on lines %d and %d" % (path, listed[path].line_no, row.line_no))
            listed[path] = row

    present = top_level_components(REPO)
    missing_present = [c for c in present if c not in listed]
    assert not missing_present, (
        "LICENSING.md has no row for these top-level components: %s (add a row; a generated folder "
        "belongs in .gitignore, but a folder git tracks files in is published whatever .gitignore says)"
        % missing_present
    )

    missing_planned = sorted(c for c in EXPECTED_LICENCE if c not in listed)
    assert not missing_planned, "LICENSING.md has no row for these planned components: %s" % missing_planned

    stale = sorted(p for p in listed if p not in present and p not in PLANNED_COMPONENTS)
    assert not stale, (
        "LICENSING.md lists paths that neither exist nor are planned: %s "
        "(remove the row, or add the folder to PLANNED_COMPONENTS in this test)" % stale
    )

    wrong = []
    for component, expected in sorted(EXPECTED_LICENCE.items()):
        actual = _the_licence(listed[component])
        if actual != expected:
            wrong.append("%s: table says %s, the design says %s" % (component, actual, expected))
    assert not wrong, "LICENSING.md licences disagree with the design: %s" % wrong

    root = _root_row(rows)
    assert _the_licence(root) == ROOT_LICENCE, "the repository root must be %s" % ROOT_LICENCE

    # Every other row names exactly one licence, or says plainly that it is not ours.
    for row in rows:
        if any(p in VENDOR_DIRS or p.rstrip("/") in VENDOR_DIRS for p in row.paths):
            continue
        _the_licence(row)


@pytest.mark.parametrize("component", PENDING_OWNER_DECISION)
def test_licensing_marks_owner_decisions_as_pending(component: str) -> None:
    row = _row_for(_licensing_rows(), component)
    assert row is not None, "LICENSING.md has no row for %s" % component
    assert "Q13" in row.text, "the %s row must name the open owner question Q13: %s" % (component, row.text)
    if component == "posts/":
        assert "pending" in row.text.lower(), "the posts/ licence is not decided yet; its row must say pending"


@pytest.mark.parametrize("vendor", sorted(VENDOR_DIRS))
def test_vendor_folder_is_listed_as_not_covered(vendor: str) -> None:
    row = _row_for(_licensing_rows(), vendor + "/")
    if not (REPO / vendor).is_dir():
        # Moved out of the repository: the row must go with it.
        assert row is None, "%s/ is no longer in the repository; remove its LICENSING.md row" % vendor
        return
    assert row is not None, "LICENSING.md must say that %s/ is not under this repository's licences" % vendor
    lowered = row.text.lower()
    assert row.licences == (), (
        "%s/ holds third-party files; its row must not name one of our licences: %s" % (vendor, row.text)
    )
    assert "not covered" in lowered and "proprietary" in lowered, (
        "the %s/ row must say the files are proprietary and not covered by this repository's licences" % vendor
    )


@pytest.mark.parametrize("vendor", sorted(VENDOR_DIRS))
def test_licensing_describes_vendor_installers_truthfully(vendor: str) -> None:
    text = " ".join(LICENSING.read_text(encoding="utf-8").replace("*", "").split())
    if (REPO / vendor).is_dir():
        assert not re.search(r"installers\s+are\s+not\s+published", text, re.IGNORECASE), (
            "LICENSING.md claims the vendor installers are not published, but %s/ is in the repository" % vendor
        )
        assert "`%s/`" % vendor in text, "LICENSING.md's clean-room note must name %s/" % vendor
    else:
        assert "`%s/`" % vendor not in text, (
            "%s/ has left the repository; update LICENSING.md's clean-room note and table" % vendor
        )


def test_licence_metadata_agrees_with_table() -> None:
    rows = _licensing_rows()
    checked = []
    root = REPO / "pyproject.toml"
    if root.is_file():
        assert _pyproject_licence(root) == _the_licence(_root_row(rows)), "root pyproject.toml licence"
        checked.append("pyproject.toml")
    for component in top_level_components(REPO):
        pyproject = REPO / component / "pyproject.toml"
        if component.rstrip("/") in VENDOR_DIRS or not pyproject.is_file():
            continue
        row = _row_for(rows, component)
        assert row is not None, "LICENSING.md has no row for %s" % component
        assert _pyproject_licence(pyproject) == _the_licence(row), "%spyproject.toml licence" % component
        checked.append(component + "pyproject.toml")
    assert "pyproject.toml" in checked and "goliath-link/pyproject.toml" in checked, checked


def test_licence_texts_present() -> None:
    gpl = (REPO / "LICENSE").read_text(encoding="utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in gpl and "Version 3" in gpl
    apache = (REPO / "goliath-link" / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in apache and "Version 2.0" in apache


def test_licensing_relative_links_resolve() -> None:
    broken = [
        target
        for target in _relative_links(LICENSING.read_text(encoding="utf-8"))
        if target.split("#", 1)[0] and not (REPO / target.split("#", 1)[0]).exists()
    ]
    assert not broken, (
        "LICENSING.md links paths that do not exist (planned folders go in backticks, not links): %s" % broken
    )


# --------------------------------------------------------------------------
# No document cites a scratchpad, temporary or worktree path
# --------------------------------------------------------------------------


def test_no_doc_cites_scratchpad_paths() -> None:
    hits = find_forbidden_citations(REPO)
    assert not hits, "documents cite locations outside the repository:\n" + "\n".join(
        "  %s:%d (%s): %s" % h for h in hits
    )


def test_scan_covers_docs_and_package_readmes() -> None:
    scanned = {p.relative_to(REPO).as_posix() for p in documents_to_scan(REPO)}
    expected = {
        "README.md",
        "LICENSING.md",
        "goliath-link/README.md",
        "docs/protocol/README.md",
        ".github/rulesets/README.md",
    }
    expected.update("docs/protocol/" + p.name for p in PROTOCOL_DIR.glob("0*.md"))
    expected.update(p.relative_to(REPO).as_posix() for p in (REPO / "docs" / "field-guide").glob("*.md"))
    assert expected <= scanned, sorted(expected - scanned)
    assert not any(p.split("/", 1)[0] in VENDOR_DIRS for p in scanned)


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")


def test_scanner_finds_planted_citations_and_never_enters_vendor_folder(tmp_path: Path) -> None:
    _write(tmp_path / "README.md", "Plain text.\n")
    _write(tmp_path / "docs" / "prep" / "a.md", "ok\nsee scratchpad/fw-facts-brief.md section 3\n")
    _write(tmp_path / "docs" / "prep" / "checks.json", '{"source": "/tmp/claude-0/x/spec.md"}\n')
    _write(tmp_path / "docs" / "img.png", b"\x89PNG\r\n\x1a\n\xff\xfe/tmp/")
    _write(tmp_path / "goliath-prep" / "README.md", "built in .claude/worktrees/abc\n")
    _write(tmp_path / "posts" / "freecad" / "README.md", "C:\\Users\\x\\AppData\\Local\\Temp\\post\n")
    _write(tmp_path / "goliath-prep" / "src" / "notes.md", "/tmp/ in a non-README file is not scanned\n")
    _write(tmp_path / "goliath-prep" / ".venv" / "README.md", "/tmp/ inside a hidden folder\n")
    _write(tmp_path / "goliath-prep" / "build" / "lib" / "README.md", "/tmp/ in generated build output\n")
    _write(tmp_path / "old_software" / "README.md", "/tmp/ inside the vendor folder\n")
    _write(tmp_path / ".github" / "rulesets" / "README.md", "copied from scratchpad/notes.md\n")
    _write(tmp_path / ".github" / "workflows" / "ci.yml", "run: ls /tmp/ (workflows are not documents)\n")
    _write(tmp_path / "docs" / "build" / "x.md", "a docs folder named build is still scanned: /tmp/x\n")

    hits = find_forbidden_citations(tmp_path)

    by_file: Dict[str, Set[str]] = {}
    for hit in hits:
        by_file.setdefault(hit.path, set()).add(hit.kind)
    assert by_file == {
        ".github/rulesets/README.md": {"build-session scratchpad"},
        "docs/build/x.md": {"temporary directory"},
        "docs/prep/a.md": {"build-session scratchpad", "scratchpad brief"},
        "docs/prep/checks.json": {"temporary directory"},
        "goliath-prep/README.md": {"git worktree"},
        "posts/freecad/README.md": {"Windows temporary directory"},
    }
    assert [h.line_no for h in hits if h.path == "docs/prep/a.md"] == [2, 2]


def test_scanner_finds_short_form_scratchpad_citations(tmp_path: Path) -> None:
    # The build design cites its facts brief as "brief §n" (design 0.2) and lives
    # in scratchpad files of its own; none of them is in the repository, so a
    # document must not cite them even without the scratchpad/ prefix.
    _write(
        tmp_path / "docs" / "prep" / "safety-model.md",
        "Reply to an unknown command: none or ERR;1 (brief §3).\n"
        "Frames are NUL-padded to 512 bytes (brief § 2).\n"
        "See design-final.md section 4.8.\n"
        "Unit list: work-units-final.json.\n"
        "In brief, section 3 of spec 09 lists the commands.\n",
    )
    hits = find_forbidden_citations(tmp_path)
    assert [(h.line_no, h.kind) for h in hits] == [
        (1, "scratchpad brief"),
        (2, "scratchpad brief"),
        (3, "scratchpad design document"),
        (4, "scratchpad design document"),
    ]


def test_scanner_refuses_undecodable_text(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "notes.txt", b"caf\xe9 /tmp/x\n")
    with pytest.raises(ValueError, match="not UTF-8"):
        find_forbidden_citations(tmp_path)


def test_table_parser_rejects_ambiguous_licence_cell() -> None:
    text = (
        "| Component | Path | Licence | Why |\n"
        "|---|---|---|---|\n"
        "| Posts | `posts/` | Apache-2.0 or GPL-3.0-or-later | undecided |\n"
        "| Root | repository root | **GPL-3.0-or-later** | default |\n"
    )
    rows = parse_component_table(text)
    assert rows[0].paths == ("posts/",) and rows[0].licences == ("Apache-2.0", "GPL-3.0-or-later")
    assert rows[1].paths == () and rows[1].licences == ("GPL-3.0-or-later",)
    with pytest.raises(pytest.fail.Exception, match="exactly one SPDX licence"):
        _the_licence(rows[0])


def test_gitignore_negation_is_refused(tmp_path: Path) -> None:
    _write(tmp_path / ".gitignore", "build/\n!build/keep\n")
    with pytest.raises(ValueError, match="negation"):
        gitignore_patterns(tmp_path)


def test_top_level_components_skip_ignored_and_hidden(tmp_path: Path) -> None:
    # tmp_path is not a git checkout: git cannot say whether the ignored folders
    # hold tracked files, so skipping them is a fallback and must say so.
    _write(tmp_path / ".gitignore", "# comment\n.venv/\n*.egg-info/\ncaptures/\ndocs/generated/x\n")
    for d in ("goliath-x", "captures", "foo.egg-info", ".github", "docs/protocol", "docs/prep", "old_software"):
        (tmp_path / d).mkdir(parents=True)
    _write(tmp_path / "pyproject.toml", "")
    with pytest.warns(UserWarning) as record:
        found = top_level_components(tmp_path)
    assert found == ["docs/prep/", "docs/protocol/", "goliath-x/", "old_software/"]
    messages = sorted(str(w.message) for w in record)
    assert len(messages) == 2, messages
    assert messages[0].startswith("captures/ matches .gitignore") and "git could not confirm" in messages[0]
    assert messages[1].startswith("foo.egg-info/ matches .gitignore") and "git could not confirm" in messages[1]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo)] + list(args), check=True, capture_output=True)


def test_ignored_folder_with_tracked_files_is_a_component(tmp_path: Path) -> None:
    # The .gitignore keeps captures/ and probe-results/ out "by accident", not
    # by rule: a capture shared deliberately is force-added and then published.
    # A published folder needs a LICENSING.md row like any other.
    if shutil.which("git") is None:
        pytest.skip("git is not installed, so a git checkout cannot be built for this test")
    _git(tmp_path, "init", "-q")
    _write(tmp_path / ".gitignore", "captures/\nprobe-results/\n*.egg-info/\n")
    _write(tmp_path / "captures" / "shared-capture.txt", "shared deliberately\n")
    _write(tmp_path / "probe-results" / "local.json", "{}\n")
    (tmp_path / "goliath_cnc.egg-info").mkdir()
    _git(tmp_path, "add", "-f", ".gitignore", "captures/shared-capture.txt")
    assert top_level_components(tmp_path) == ["captures/"]


def test_vendor_folder_is_a_component_even_when_ignored(tmp_path: Path) -> None:
    # Its presence alone needs the "not covered" row; git is never asked about it.
    _write(tmp_path / ".gitignore", "old_software/\n")
    (tmp_path / "old_software").mkdir()
    assert top_level_components(tmp_path) == ["old_software/"]
