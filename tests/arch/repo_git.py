# SPDX-License-Identifier: GPL-3.0-or-later
"""git access for the repository-level checks, with a visible skip-or-fail rule.

A check that needs git skips with its reason where git or the checkout is
missing (a source archive, a machine without git), and fails instead where the
environment variable GOLIATH_REQUIRE_GIT is 1 (the CI jobs for the
applications set it), so CI can never pass such a check by skipping it.

Python 3.9 compatible: the repository-level suite runs on 3.9.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest

REPO = Path(__file__).resolve().parents[2]
REQUIRE_GIT_ENV = "GOLIATH_REQUIRE_GIT"


def same_dir(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def run_git(cwd: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    """Run git; raises FileNotFoundError when git is not installed."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def git_unavailable(reason: str) -> NoReturn:
    """Skip with the reason, or fail when this run must have git."""
    if os.environ.get(REQUIRE_GIT_ENV) == "1":
        pytest.fail("%s, and %s=1 says this run must have git" % (reason, REQUIRE_GIT_ENV))
    pytest.skip(reason)


class Git:
    """git in this repository's checkout."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def run(self, *args: str) -> "subprocess.CompletedProcess[str]":
        return run_git(self.repo, *args)

    def out(self, *args: str) -> str:
        """stdout of a git command that must succeed; a failure fails the test."""
        result = self.run(*args)
        if result.returncode != 0:
            pytest.fail("git %s failed (exit %d): %s" % (" ".join(args), result.returncode, result.stderr.strip()))
        return result.stdout.strip()


def open_git(repo: Path = REPO) -> Git:
    """A Git for `repo`, or skip/fail (see git_unavailable) if it has no usable checkout."""
    try:
        probe = run_git(repo, "rev-parse", "--show-toplevel")
    except FileNotFoundError:
        git_unavailable("git is not installed, so this check cannot read the repository")
    if probe.returncode != 0:
        git_unavailable("%s is not a git checkout (%s)" % (repo, probe.stderr.strip()))
    top = Path(probe.stdout.strip())
    if not same_dir(top, repo):
        git_unavailable("the git checkout found (%s) is not this repository (%s)" % (top, repo))
    return Git(repo)
