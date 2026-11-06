# SPDX-License-Identifier: GPL-3.0-or-later
"""goliath-link/ stays exactly as recorded in goliath_link_freeze.json until the record is lifted.

The owner ran the read-only probe (rung R1) from goliath-link as frozen for it
(build design, section 9.3 and unit F01). R1 found firmware GFW 01.00.13
(branch B4), and the record now holds the probe hotfix that followed: the probe
stops on firmware below 01.02.00 or an unknown identity and redacts the shared
output. Nothing else may change goliath-link/ until the B4 design changes for
units L01 to L09 are reviewed (the record's how_to_lift). The build design
asked for a git tag; no tag is created, so the commit and the tree id of
goliath-link/ at that commit are recorded in the JSON file instead.

The main check compares tree ids, which needs no history: it works in a
shallow CI checkout. Checking that the recorded tree id belongs to the
recorded commit needs that commit. Only a shallow clone may lack it: there
that one check is skipped with the reason (or fails where GOLIATH_REQUIRE_GIT
is 1), and the CI jobs for the applications run it with full history. In a
full clone a missing commit fails: the record would name a commit that is not
in the history.

Python 3.9 compatible: the repository-level suite runs on 3.9.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

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


def freeze_commit_problem(git: Git, record: Dict[str, str]) -> Tuple[str, str]:
    """Whether the recorded tree id is `record["path"]` at the recorded commit.

    Returns ("ok", ""), ("wrong", why) or ("unverifiable", why). Only a shallow
    clone can lack the commit legitimately; in a full clone a missing commit
    means the record names a commit that is not in the history (rewritten or
    squashed), which is wrong, not unverifiable.
    """
    commit, path = record["commit"], record["path"]
    if not _has_commit(git, commit):
        shallow = git.out("rev-parse", "--is-shallow-repository")
        if shallow == "true":
            return (
                "unverifiable",
                "freeze commit %s is not in this shallow clone, so the recorded tree id cannot be checked "
                "against it here (the tree comparison with HEAD does not need it)" % commit[:12],
            )
        return (
            "wrong",
            "freeze commit %s is not in this repository's history, and this is not a shallow clone "
            "(is-shallow-repository: %s): the record names a commit that does not exist here; "
            "update %s" % (commit[:12], shallow, FREEZE_FILE.name),
        )
    tree = git.out("rev-parse", "%s:%s" % (commit, path))
    if tree != record["tree"]:
        return (
            "wrong",
            "%s/ at the freeze commit %s is tree %s, but the record says %s"
            % (path, commit[:12], tree, record["tree"]),
        )
    return ("ok", "")


def test_freeze_record_is_well_formed() -> None:
    record = _freeze()
    assert record["path"] == "goliath-link"
    assert _SHA1.match(record["commit"]), record["commit"]
    # The probe hotfix after rung R1 (which ran from a647bbd), with the fixes from its
    # review (the hotfix itself was 528bf13): what owners run now.
    assert record["commit"].startswith("81060c1")
    assert _SHA1.match(record["tree"]), record["tree"]
    for key in ("reason", "how_to_lift"):
        assert record[key].strip(), key
    assert "R1" in record["reason"] and "R1" in record["how_to_lift"]


def test_goliath_link_untouched_since_the_freeze_commit(git: Git) -> None:
    problems = freeze_problems(git, _freeze())
    assert not problems, "goliath-link is frozen at the probe hotfix until the B4 review (see %s):\n%s" % (
        FREEZE_FILE.name,
        "\n".join(problems),
    )


def test_freeze_record_matches_freeze_commit(git: Git) -> None:
    outcome, message = freeze_commit_problem(git, _freeze())
    if outcome == "unverifiable":
        git_unavailable(message)  # a skip with its reason; a failure where GOLIATH_REQUIRE_GIT=1
    assert outcome == "ok", message


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


def _scratch_repo(root: Path, commits: int) -> List[str]:
    """A scratch repository with `commits` commits touching lib/a.py; their ids, oldest first."""
    identity = ["-c", "user.name=freeze-test", "-c", "user.email=freeze-test@example.invalid"]
    identity += ["-c", "commit.gpgsign=false"]
    (root / "lib").mkdir(parents=True)
    ids = []
    result = run_git(root, "init", "-q")
    if result.returncode != 0:
        git_unavailable("cannot create a scratch git repository: %s" % result.stderr.strip())
    for number in range(commits):
        (root / "lib" / "a.py").write_text("A = %d\n" % number, encoding="utf-8")
        for args in (["add", "-A"], identity + ["commit", "-q", "-m", "c%d" % number]):
            result = run_git(root, *args)
            if result.returncode != 0:
                git_unavailable("cannot create a scratch git repository: %s" % result.stderr.strip())
        ids.append(Git(root).out("rev-parse", "HEAD"))
    return ids


def test_freeze_commit_verified_in_a_full_clone(tmp_path: Path, git: Git) -> None:
    del git
    repo = tmp_path / "full"
    first, _second = _scratch_repo(repo, 2)
    scratch = Git(repo)
    good = {"path": "lib", "commit": first, "tree": scratch.out("rev-parse", "%s:lib" % first)}
    assert freeze_commit_problem(scratch, good) == ("ok", "")
    wrong_tree = dict(good, tree=scratch.out("rev-parse", "HEAD:lib"))
    outcome, message = freeze_commit_problem(scratch, wrong_tree)
    assert outcome == "wrong" and first[:12] in message
    # A full clone that lacks the recorded commit cannot be explained by a
    # shallow checkout: the record names a commit that is not in the history
    # (rewritten or squashed). That is an error, never a skip.
    missing = dict(good, commit="0" * 40)
    outcome, message = freeze_commit_problem(scratch, missing)
    assert outcome == "wrong" and "not a shallow clone" in message


def test_freeze_commit_unverifiable_only_in_a_shallow_clone(tmp_path: Path, git: Git) -> None:
    del git
    origin = tmp_path / "origin"
    first, _second = _scratch_repo(origin, 2)
    shallow = tmp_path / "shallow"
    result = run_git(tmp_path, "clone", "-q", "--depth", "1", origin.resolve().as_uri(), str(shallow))
    if result.returncode != 0:
        git_unavailable("cannot make a shallow scratch clone: %s" % result.stderr.strip())
    clone = Git(shallow)
    assert clone.out("rev-parse", "--is-shallow-repository") == "true"
    record = {"path": "lib", "commit": first, "tree": Git(origin).out("rev-parse", "%s:lib" % first)}
    outcome, message = freeze_commit_problem(clone, record)
    assert outcome == "unverifiable" and "shallow" in message
