# SPDX-License-Identifier: GPL-3.0-or-later
"""goliath-link/ stays exactly as the owner's probe was built until rung R1 has run.

The owner runs the read-only probe (rung R1) from goliath-link at the freeze
commit recorded in goliath_link_freeze.json. Until R1 has run and its results
are in, nothing may change goliath-link/ (build design, section 9.3 and unit
F01). The build design asked for a git tag; no tag is created, so the commit
and the tree id of goliath-link/ at that commit are recorded in the JSON file
instead.

The main check compares tree ids, which needs no history: it works in a
shallow CI checkout. Checking that the recorded tree id belongs to the
recorded commit needs that commit; where it is missing (a shallow clone) that
one check is skipped with the reason, and CI runs it with full history.

Python 3.9 compatible: the repository-level suite runs on 3.9.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List

import pytest

from repo_git import Git, git_unavailable, run_git

FREEZE_FILE = Path(__file__).resolve().parent / "goliath_link_freeze.json"
_SHA1 = re.compile(r"^[0-9a-f]{40}$")


def _freeze() -> Dict[str, str]:
    with FREEZE_FILE.open(encoding="utf-8") as fh:
        return json.load(fh)


def _has_commit(git: Git, commit: str) -> bool:
    return git.run("cat-file", "-e", "%s^{commit}" % commit).returncode == 0


def freeze_problems(git: Git, record: Dict[str, str]) -> List[str]:
    """Every way the checkout's `record["path"]` differs from the frozen tree; empty if none."""
    path = record["path"]
    problems = []
    head_tree = git.out("rev-parse", "HEAD:%s" % path)
    if head_tree != record["tree"]:
        detail = ""
        if _has_commit(git, record["commit"]):
            detail = "\n" + git.out("diff", "--stat", record["commit"], "HEAD", "--", path)
        problems.append(
            "%s/ at HEAD (tree %s) differs from the freeze commit %s (tree %s)%s"
            % (path, head_tree, record["commit"][:12], record["tree"], detail)
        )
    # Uncommitted edits and new files count too: the frozen library is what is on disk.
    status = git.out("status", "--porcelain", "--untracked-files=all", "--", path)
    if status:
        problems.append("uncommitted changes under %s/:\n%s" % (path, status))
    return problems


def test_freeze_record_is_well_formed() -> None:
    record = _freeze()
    assert record["path"] == "goliath-link"
    assert _SHA1.match(record["commit"]), record["commit"]
    assert record["commit"].startswith("a647bbd")  # the head the owner's probe is built from
    assert _SHA1.match(record["tree"]), record["tree"]
    for key in ("reason", "how_to_lift"):
        assert record[key].strip(), key
    assert "R1" in record["reason"] and "R1" in record["how_to_lift"]


def test_goliath_link_untouched_until_r1_tag(git: Git) -> None:
    problems = freeze_problems(git, _freeze())
    assert not problems, "goliath-link is frozen until rung R1 has run (see %s):\n%s" % (
        FREEZE_FILE.name,
        "\n".join(problems),
    )


def test_freeze_record_matches_freeze_commit(git: Git) -> None:
    record = _freeze()
    if not _has_commit(git, record["commit"]):
        shallow = git.run("rev-parse", "--is-shallow-repository").stdout.strip()
        pytest.skip(
            "freeze commit %s is not in this clone (shallow clone: %s), so the recorded tree id cannot be "
            "checked against it here; the tree comparison above does not need it" % (record["commit"][:12], shallow)
        )
    assert git.out("rev-parse", "%s:%s" % (record["commit"], record["path"])) == record["tree"]


def test_freeze_check_sees_committed_uncommitted_and_new_files(tmp_path: Path, git: Git) -> None:
    del git  # only needed to skip (or fail) cleanly where git is missing
    repo = tmp_path / "repo"
    lib = repo / "lib"
    lib.mkdir(parents=True)
    (lib / "a.py").write_text("A = 1\n", encoding="utf-8")
    (repo / "other.txt").write_text("x\n", encoding="utf-8")
    identity = ["-c", "user.name=freeze-test", "-c", "user.email=freeze-test@example.invalid"]
    identity += ["-c", "commit.gpgsign=false"]
    for args in (["init", "-q"], ["add", "-A"], identity + ["commit", "-q", "-m", "base"]):
        result = run_git(repo, *args)
        if result.returncode != 0:
            git_unavailable("cannot create a scratch git repository: %s" % result.stderr.strip())
    scratch = Git(repo)
    record = {"path": "lib", "commit": scratch.out("rev-parse", "HEAD"), "tree": scratch.out("rev-parse", "HEAD:lib")}
    assert freeze_problems(scratch, record) == []

    (repo / "other.txt").write_text("changed outside the frozen folder\n", encoding="utf-8")
    assert freeze_problems(scratch, record) == []

    (lib / "new.py").write_text("NEW = 1\n", encoding="utf-8")
    [untracked] = freeze_problems(scratch, record)
    assert "uncommitted" in untracked and "new.py" in untracked

    (lib / "new.py").unlink()
    (lib / "a.py").write_text("A = 2\n", encoding="utf-8")
    [edited] = freeze_problems(scratch, record)
    assert "uncommitted" in edited and "a.py" in edited

    assert run_git(repo, *identity, "commit", "-q", "-am", "edit").returncode == 0
    [committed] = freeze_problems(scratch, record)
    assert "differs from the freeze commit" in committed and "a.py" in committed
