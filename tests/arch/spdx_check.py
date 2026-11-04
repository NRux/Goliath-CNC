#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Check that every source file carries the SPDX licence header its folder requires.

    python tests/arch/spdx_check.py [--root PATH]

Exit codes: 0 every header present and right; 1 problems found (each one
listed); 2 the check could not run (git missing, or not a git checkout).

Which files: every file git tracks or would add (untracked files that are not
ignored count, so a new file is checked before it is committed). Source code
(.py, .cps, .js, .css, .html, shell scripts, ...) must carry an
``SPDX-License-Identifier`` in its first HEADER_LINES lines. Prose (.md) may
carry one in an HTML comment (``<!-- SPDX-License-Identifier: ... -->``); when
it does, it must be right. Data and configuration (.json, .toml, .yml, licence
texts) are not checked.

Which licence: LICENSING.md, which follows build design section 9.2:
goliath-link/, docs/protocol/, docs/prep/ and posts/ are Apache-2.0 (posts/
pending the owner's decision, question Q13); everything else, goliath-prep/
and goliath-run/ included, is under the root GPL-3.0-or-later.

The vendor folder old_software/ is excluded by git itself and never opened.

Python 3.9 compatible, standard library only: it runs in every CI job.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, NamedTuple, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[2]

ROOT_LICENCE = "GPL-3.0-or-later"
# Folders whose licence differs from the root's. A prefix ends with "/" so it
# only matches at a folder boundary.
FOLDER_LICENCES = (
    ("goliath-link/", "Apache-2.0"),
    ("docs/protocol/", "Apache-2.0"),
    ("docs/prep/", "Apache-2.0"),
    ("posts/", "Apache-2.0"),  # recommended; the owner decides (question Q13)
)
VENDOR_DIRS = ("old_software/",)

REQUIRED_SUFFIXES = frozenset(
    {".py", ".pyi", ".cps", ".js", ".mjs", ".cjs", ".ts", ".css", ".html", ".htm", ".sh", ".ps1", ".bat", ".cmd"}
)
PROSE_SUFFIXES = frozenset({".md"})
HEADER_LINES = 10

_TAG = "SPDX-License-Identifier:"
_CODE_HEADER = re.compile(r"SPDX-License-Identifier:\s*(.*?)\s*(?:-->|\*/)?\s*$")
_PROSE_HEADER = re.compile(r"^\s*<!--\s*SPDX-License-Identifier:\s*(.*?)\s*-->\s*$")


class SpdxCheckError(Exception):
    """The check could not run (as opposed to: it ran and found problems)."""


class Report(NamedTuple):
    checked: List[str]
    problems: List[Tuple[str, str]]


def expected_licence(rel: str) -> str:
    for prefix, licence in FOLDER_LICENCES:
        if rel.startswith(prefix):
            return licence
    return ROOT_LICENCE


def _suffix(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""


def needs_check(rel: str) -> bool:
    """Whether this file type is checked at all (required for code, optional for prose)."""
    return _suffix(rel) in REQUIRED_SUFFIXES or _suffix(rel) in PROSE_SUFFIXES


def _identifiers(rel: str, text: str) -> List[str]:
    prose = _suffix(rel) in PROSE_SUFFIXES
    found = []
    for line in text.splitlines()[:HEADER_LINES]:
        if _TAG not in line:
            continue
        match = (_PROSE_HEADER.match(line) if prose else _CODE_HEADER.search(line))
        if match:
            found.append(match.group(1))
    return found


def check_text(rel: str, text: str) -> Optional[str]:
    """The problem with this file's header, or None. `rel` is relative to the repository root, with '/'."""
    if text.startswith("﻿"):
        text = text[1:]
    expected = expected_licence(rel)
    found = _identifiers(rel, text)
    if not found:
        if _suffix(rel) in PROSE_SUFFIXES:
            return None  # optional for prose: the folder's licence applies (LICENSING.md)
        return "missing SPDX-License-Identifier header in the first %d lines (this folder needs %s)" % (
            HEADER_LINES,
            expected,
        )
    if len(found) > 1:
        return "more than one SPDX-License-Identifier in the header: %s" % ", ".join(found)
    if found[0] != expected:
        return "SPDX header says %s; this folder needs %s" % (found[0], expected)
    return None


def _is_vendor(rel: str) -> bool:
    return any(rel == d.rstrip("/") or rel.startswith(d) for d in VENDOR_DIRS)


def check_files(root: Path, rels: Iterable[str]) -> Report:
    checked: List[str] = []
    problems: List[Tuple[str, str]] = []
    for rel in sorted(set(rels)):
        if _is_vendor(rel) or not needs_check(rel):
            continue  # the vendor folder is never opened
        path = root / rel
        if not path.is_file():
            continue  # tracked but deleted in the working tree: nothing to ship
        checked.append(rel)
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            problems.append((rel, "not UTF-8, so its header cannot be checked (%s)" % exc))
            continue
        problem = check_text(rel, text)
        if problem:
            problems.append((rel, problem))
    return Report(checked, problems)


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=str(root), capture_output=True)
    except FileNotFoundError as exc:
        raise SpdxCheckError("git is not installed; it lists the files to check") from exc
    if result.returncode != 0:
        raise SpdxCheckError(
            "git %s failed in %s: %s" % (" ".join(args), root, result.stderr.decode("utf-8", "replace").strip())
        )
    return result.stdout.decode("utf-8")


def list_files(root: Path) -> List[str]:
    """Tracked and not-ignored untracked files under `root`, which must be the top of a git checkout."""
    root = Path(root)
    if not root.is_dir():
        raise SpdxCheckError("%s is not a folder" % root)
    top = Path(_git(root, "rev-parse", "--show-toplevel").strip())
    if os.path.normcase(str(top.resolve())) != os.path.normcase(str(root.resolve())):
        raise SpdxCheckError("%s is not the top of its git checkout (that is %s)" % (root, top))
    exclude = [":(exclude)%s" % d.rstrip("/") for d in VENDOR_DIRS]
    out = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", ".", *exclude)
    return sorted({rel for rel in out.split("\0") if rel and not _is_vendor(rel)})


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Check SPDX licence headers (see the module docstring).")
    parser.add_argument("--root", type=Path, default=REPO, help="repository root (default: this checkout)")
    args = parser.parse_args(argv)
    try:
        files = list_files(args.root)
    except SpdxCheckError as exc:
        print("spdx_check: cannot run: %s" % exc, file=sys.stderr)
        return 2
    report = check_files(args.root, files)
    for rel, problem in report.problems:
        print("%s: %s" % (rel, problem))
    print(
        "spdx_check: %d files checked, %d with problems" % (len(report.checked), len(report.problems)),
    )
    return 1 if report.problems else 0


if __name__ == "__main__":
    sys.exit(main())
