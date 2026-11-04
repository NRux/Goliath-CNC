#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run this repository's CI jobs on your own machine, started by hand.

    python tools/local_ci.py                   every job, on this computer
    python tools/local_ci.py --where docker    every job, in throwaway Linux containers
    python tools/local_ci.py --job apps --python 3.11
    python tools/local_ci.py --list            what would run, command by command

It runs a copy of .github/workflows/ci.yml: the same jobs, Python versions,
environment variables, install and test commands, and time limits.
tests/test_local_ci.py fails as soon as the copy and ci.yml differ. Nothing is
registered with GitHub and no code from anyone else's pull request ever runs:
it tests the checkout you start it in.

tools/local-ci.md explains the options, the optional check for another
project's CI on the same machine, and how a local run differs from GitHub's.

Standard library only, Python >= 3.9. Needs git and uv on this computer
(uv provides each Python version); --where docker needs Docker instead of uv.

Exit codes: 0 every job passed; 1 a job failed, timed out or hit an error;
2 the run could not start or stopped before any job failed (a missing tool,
a bad option, the busy check could not ask GitHub); 3 stopped because the
configured other CI was running (see --wait); 130 interrupted (Ctrl+C).
"""
from __future__ import annotations

import argparse
import codecs
import configparser
import datetime
import hashlib
import io
import json
import locale
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------
# The jobs: a copy of .github/workflows/ci.yml (kept in step by
# tests/test_local_ci.py). Commands are the ci.yml text, character for
# character; "python" at the start of a command is the job's Python.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    name: str
    commands: Tuple[str, ...]
    cwd: str = "."  # ci.yml working-directory, relative to the repository root


@dataclass(frozen=True)
class Job:
    name: str  # the job id in ci.yml
    pythons: Tuple[str, ...]
    timeout_minutes: int
    env: Tuple[Tuple[str, str], ...]
    steps: Tuple[Step, ...]
    ci_runners: str  # where GitHub runs it, for the banner
    # ci.yml's actions/checkout inputs. Not used locally (a local run always has
    # the full history); kept so the sync test notices when CI's checkout changes.
    checkout: Tuple[Tuple[str, str], ...] = (("persist-credentials", "false"),)
    # The (runner, Python) pairs CI runs, compared by the sync test.
    ci_matrix: Tuple[Tuple[str, str], ...] = ()

    @property
    def ci_os(self) -> Tuple[str, ...]:
        return tuple(dict.fromkeys(runner for runner, _ in self.ci_matrix))


JOBS: Tuple[Job, ...] = (
    Job(
        name="test",
        pythons=("3.9", "3.13"),
        timeout_minutes=15,
        env=(),
        steps=(
            Step("Install", (
                "python -m pip install --upgrade pip",
                'python -m pip install -e ".[dev]" -e "./goliath-link[dev]"',
            )),
            Step("Root tests (tools/)", ("python -m pytest -q",)),
            Step("goliath-link tests", ("python -m pytest -q",), cwd="goliath-link"),
        ),
        ci_runners="ubuntu and windows-latest",
        ci_matrix=(("ubuntu-24.04", "3.9"), ("ubuntu-latest", "3.13"), ("windows-latest", "3.9"),
                   ("windows-latest", "3.13")),
    ),
    Job(
        name="apps",
        pythons=("3.11", "3.14"),
        timeout_minutes=20,
        env=(("GOLIATH_REQUIRE_GIT", "1"),),
        steps=(
            Step("Install (siblings editable, then the root extras against them)", (
                "python -m pip install --upgrade pip",
                'python -m pip install -e "./goliath-link[dev]" -e "./goliath-prep[dev]" -e "./goliath-run[dev]"',
                'python -m pip install -e ".[dev,prep,run]"',
            )),
            Step("goliath-prep and goliath-run tests", (
                "python -m pytest -q -rs -p no:cacheprovider goliath-prep/tests goliath-run/tests",
            )),
            Step("Repository-level tests (tools/, tests/arch/)", (
                "python -m pytest -q -rs -p no:cacheprovider",
            )),
        ),
        ci_runners="ubuntu-latest and windows-latest",
        checkout=(("persist-credentials", "false"), ("fetch-depth", "0")),
        ci_matrix=(("ubuntu-latest", "3.11"), ("ubuntu-latest", "3.14"), ("windows-latest", "3.11"),
                   ("windows-latest", "3.14")),
    ),
    Job(
        name="spdx",
        pythons=("3.13",),
        timeout_minutes=5,
        env=(),
        steps=(Step("Check SPDX headers", ("python tests/arch/spdx_check.py",)),),
        ci_runners="ubuntu-latest only",
        ci_matrix=(("ubuntu-latest", "3.13"),),
    ),
)

# ci.yml jobs that are not copied here, with the reason.
NOT_MIRRORED = {"local-ci": "it tests this script itself"}

EXIT_OK, EXIT_FAILED, EXIT_SETUP, EXIT_BUSY, EXIT_INTERRUPTED = 0, 1, 2, 3, 130
EXIT_TIMEOUT_INNER = 124  # used only between the docker container and this script

# Environment variables that change how Python or pytest behave. GitHub's
# runners do not set them, so a local run does not pass them on. pip's and
# uv's own settings (PIP_*, UV_*: package indexes, proxies, certificates,
# caches) are passed on, because downloads may not work without them; the
# banner lists them.
DROPPED_PREFIXES = ("PYTHON", "PYTEST_")
DROPPED_NAMES = ("VIRTUAL_ENV", "CONDA_PREFIX", "__PYVENV_LAUNCHER__")
DOWNLOAD_PREFIXES = ("PIP_", "UV_")
# Credentials are not passed on either: GitHub gives a job's commands none
# unless the workflow hands them over, and ci.yml hands over none.
CREDENTIAL_WORDS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "API_KEY", "ACCESS_KEY", "PRIVATE_KEY",
                    "AUTH")
# Package-index settings may carry the index's login, and downloads need them;
# they are passed on and named in the banner. Any other credential is dropped,
# uv's and pip's included (an upload token, say, is never needed here).
DOWNLOAD_AUTH_PREFIXES = ("UV_INDEX_", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX", "PIP_INDEX", "PIP_EXTRA_INDEX",
                          "PIP_CLIENT_CERT")

DOCKER_PREFIX = "goliath-local-ci"
DOCKER_VOLUME = DOCKER_PREFIX + "-cache"
# The job containers get a network of their own instead of Docker's default
# bridge, which other projects' containers on the same engine may share.
DOCKER_NETWORK = DOCKER_PREFIX + "-net"
DOCKERFILE = REPO / "tools" / "local-ci" / "Dockerfile"
DOCKER_GRACE_SECONDS = 300  # clone and container start, on top of the job's own limit
DOCKER_LOW_CPU_SHARES = 256  # --priority low: a quarter of Docker's default CPU weight (1024)
DOCKER_CANNOT_START = (125, 126, 127)  # docker run's own exit codes: the container never ran the job
# GitHub sets CI=true for every step. Hypothesis reads it and switches to its CI
# profile (derandomized examples, no example database, no deadline), so a local
# run needs it too to explore the same examples.
CI_ENV = (("CI", "true"),)
PRIVATE_WORDS_ENV = "GOLIATH_PRIVATE_WORDS"

BUSY_POLL_SECONDS = 60
BUSY_REPORT_EVERY_SECONDS = 300
_REPO_SLUG = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")


class SetupError(Exception):
    """The run cannot start or continue; the message says why and what to do."""


class BusyCheckError(SetupError):
    """The check for the other project's CI could not run."""


class OtherCiBusy(Exception):
    def __init__(self, runs: List[Dict[str, object]]) -> None:
        super().__init__("%d run(s) in progress" % len(runs))
        self.runs = runs


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def say(text: str = "") -> None:
    print(text, flush=True)


def banner(lines: Sequence[str]) -> None:
    width = max([len(line) for line in lines] + [20])
    say("=" * width)
    for line in lines:
        say(line)
    say("=" * width)


# --------------------------------------------------------------------------
# Where things are kept
# --------------------------------------------------------------------------


def _env_path(environ: Mapping[str, str], name: str) -> Optional[Path]:
    value = environ.get(name)
    return Path(os.path.abspath(value)) if value else None


def default_home(environ: Mapping[str, str] = os.environ) -> Path:
    """Folder for this script's virtual environments, logs and lock file."""
    explicit = _env_path(environ, "GOLIATH_LOCAL_CI_HOME")
    if explicit:
        return explicit
    if os.name == "nt":
        base = _env_path(environ, "LOCALAPPDATA")
        if not base:
            raise SetupError("LOCALAPPDATA is not set; set GOLIATH_LOCAL_CI_HOME to a folder for the "
                             "virtual environments and logs")
        return base / "goliath-local-ci"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "goliath-local-ci"
    return (_env_path(environ, "XDG_CACHE_HOME") or Path.home() / ".cache") / "goliath-local-ci"


def default_config_file(environ: Mapping[str, str] = os.environ) -> Path:
    explicit = _env_path(environ, "GOLIATH_LOCAL_CI_CONFIG")
    if explicit:
        return explicit
    if os.name == "nt":
        base = _env_path(environ, "APPDATA")
        if not base:
            raise SetupError("APPDATA is not set; set GOLIATH_LOCAL_CI_CONFIG to the busy-check file's path")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = _env_path(environ, "XDG_CONFIG_HOME") or Path.home() / ".config"
    return base / "goliath-local-ci" / "busy-check.ini"


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def check_config_file(config_file: Path, repo: Path) -> None:
    if _is_inside(config_file, repo):
        raise SetupError("GOLIATH_LOCAL_CI_CONFIG (%s) is inside the repository; the busy check's settings name "
                         "another project and must stay out of it. Choose a file outside the checkout." % config_file)


def check_home(home: Path, repo: Path) -> None:
    if _is_inside(home, repo):
        raise SetupError("GOLIATH_LOCAL_CI_HOME (%s) is inside the repository; the virtual environments "
                         "would end up in the checkout under test. Choose a folder outside it." % home)


# --------------------------------------------------------------------------
# The check for another project's CI on this machine
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BusyConfig:
    repo: str
    workflow: str
    source: str


def _valid_busy(repo: str, workflow: str, source: str) -> BusyConfig:
    repo, workflow = repo.strip(), workflow.strip()
    if not _REPO_SLUG.match(repo):
        raise SetupError("%s: %r is not a GitHub repository in the form OWNER/NAME" % (source, repo))
    if not workflow:
        raise SetupError("%s: the workflow name is empty" % source)
    return BusyConfig(repo, workflow, source)


def load_busy_config(environ: Mapping[str, str], config_file: Path) -> Optional[BusyConfig]:
    """The other project's CI to wait for, or None when none is configured."""
    repo = environ.get("GOLIATH_CI_BUSY_REPO", "")
    workflow = environ.get("GOLIATH_CI_BUSY_WORKFLOW", "")
    if repo or workflow:
        source = "environment variables GOLIATH_CI_BUSY_REPO and GOLIATH_CI_BUSY_WORKFLOW"
        if not (repo and workflow):
            raise SetupError("set both GOLIATH_CI_BUSY_REPO and GOLIATH_CI_BUSY_WORKFLOW, or neither")
        return _valid_busy(repo, workflow, source)
    if not config_file.exists():
        if environ.get("GOLIATH_LOCAL_CI_CONFIG"):
            raise SetupError("GOLIATH_LOCAL_CI_CONFIG names %s, which does not exist; fix the path or unset it"
                             % config_file)
        return None
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with config_file.open(encoding="utf-8-sig") as fh:
            parser.read_file(fh)
        if not parser.has_section("busy-check"):
            raise SetupError("%s has no [busy-check] section" % config_file)
        section = parser["busy-check"]
        values = {key: section.get(key, "").strip() for key in ("repo", "workflow")}
    except UnicodeError:
        raise SetupError("%s is not UTF-8 text; save it as UTF-8 (--configure-busy-check writes it that way)"
                         % config_file) from None
    except (OSError, configparser.Error, ValueError) as exc:
        raise SetupError("cannot read %s: %s" % (config_file, exc)) from None
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise SetupError("%s: [busy-check] is missing %s" % (config_file, " and ".join(missing)))
    return _valid_busy(values["repo"], values["workflow"], str(config_file))


def write_busy_config(config_file: Path, repo: str, workflow: str, run: Runner = subprocess.run) -> BusyConfig:
    """Save the busy check after asking GitHub that the workflow exists (nothing is saved otherwise)."""
    config = _valid_busy(repo, workflow, str(config_file))
    try:
        busy_runs(config, run, status="", limit=1)
    except BusyCheckError as exc:
        raise SetupError("not saved: %s" % exc) from None
    config_file.parent.mkdir(parents=True, exist_ok=True)
    text = ("# Written by tools/local_ci.py --configure-busy-check. Before each job, the\n"
            "# script asks GitHub (gh run list) whether a run of this workflow is in progress.\n"
            "[busy-check]\nrepo = %s\nworkflow = %s\n" % (config.repo, config.workflow))
    config_file.write_text(text, encoding="utf-8")
    return config


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def busy_runs(config: BusyConfig, run: Runner = subprocess.run, status: str = "in_progress",
              limit: int = 100) -> List[Dict[str, object]]:
    """In-progress runs of the configured workflow. Raises BusyCheckError when gh cannot tell.

    gh resolves --workflow by name (ignoring case) or by file name, and fails
    when the repository has no such workflow, so a misspelt name stops the run
    instead of silently matching nothing.
    """
    cmd = ["gh", "run", "list", "-R", config.repo, "--workflow", config.workflow]
    if status:
        cmd += ["--status", status]
    cmd += ["--limit", str(limit), "--json", "databaseId,workflowName,displayTitle,url,startedAt,status"]
    try:
        result = run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except FileNotFoundError:
        raise BusyCheckError("the GitHub CLI (gh) is not installed or not on PATH, so the check for %s "
                             "cannot run" % config.repo) from None
    except subprocess.TimeoutExpired:
        raise BusyCheckError("gh run list -R %s did not answer within 60 s" % config.repo) from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:500]
        raise BusyCheckError("gh run list -R %s failed (exit %d): %s" % (config.repo, result.returncode, detail))
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise BusyCheckError("gh run list -R %s returned something that is not JSON: %r"
                             % (config.repo, result.stdout[:200])) from None
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise BusyCheckError("gh run list -R %s returned %s, not a list of runs" % (config.repo, type(data).__name__))
    return data


@dataclass
class BusyGuard:
    """Before anything heavy: is the other project's CI running? (None = not configured.)"""

    config: Optional[BusyConfig]
    mode: str  # "stop", "wait" or "ignore"
    run: Runner = subprocess.run
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def describe(self) -> str:
        if self.mode == "ignore":
            return "SKIPPED (--ignore-busy): other CI on this machine is not checked"
        if self.config is None:
            return ("not configured: other CI on this machine is not checked "
                    "(see --configure-busy-check in tools/local-ci.md)")
        how = "waits" if self.mode == "wait" else "stops"
        return "before each job, %s while a '%s' run of %s is in progress (from %s)" % (
            how, self.config.workflow, self.config.repo, self.config.source)

    def check(self, what: str) -> None:
        if self.mode == "ignore" or self.config is None:
            return
        runs = busy_runs(self.config, self.run)
        if not runs:
            return
        if self.mode == "stop":
            raise OtherCiBusy(runs)
        started = self.clock()
        last_report = started
        say("busy check: %s is running a '%s' run (%s); waiting before %s. Ctrl+C stops."
            % (self.config.repo, self.config.workflow, _run_urls(runs), what))
        while runs:
            self.sleep(BUSY_POLL_SECONDS)
            runs = busy_runs(self.config, self.run)
            now = self.clock()
            if runs and now - last_report >= BUSY_REPORT_EVERY_SECONDS:
                say("busy check: still waiting (%d min): %s" % ((now - started) // 60, _run_urls(runs)))
                last_report = now
        say("busy check: %s is clear after %d min; starting %s"
            % (self.config.repo, (self.clock() - started) // 60, what))


def _run_urls(runs: List[Dict[str, object]]) -> str:
    return ", ".join(str(r.get("url") or r.get("databaseId") or "?") for r in runs)


# --------------------------------------------------------------------------
# Private names: words from this machine's settings that must not be in the repository
# --------------------------------------------------------------------------

PRIVATE_SCAN_EXCLUDED = ("old_software/",)  # third-party vendor files (LICENSING.md); never opened


def private_words(environ: Mapping[str, str], config_file: Path) -> Tuple[List[str], List[str]]:
    """(words, notes): GOLIATH_PRIVATE_WORDS plus the busy check's repository name, and what was left out.

    The words come from this machine's settings at run time and are never
    stored in the repository. A word shorter than 4 characters would match
    ordinary text: one given in GOLIATH_PRIVATE_WORDS is refused, and a short
    repository name is left out with a note (give a longer word instead).
    """
    given = [w.strip() for w in environ.get(PRIVATE_WORDS_ENV, "").split(",") if w.strip()]
    short = [w for w in given if len(w) < 4]
    if short:
        raise SetupError("%s has %d word(s) shorter than 4 characters, which would match ordinary text; use "
                         "longer ones" % (PRIVATE_WORDS_ENV, len(short)))
    words, notes = list(given), []
    config = load_busy_config(environ, config_file)
    if config is not None:
        name = config.repo.split("/", 1)[1]  # its owner may be this repository's owner too
        if len(name) < 4:
            notes.append("the busy check's repository name is shorter than 4 characters, so it is not searched "
                         "for; put a longer word for that project in %s" % PRIVATE_WORDS_ENV)
        else:
            words.append(name)
    return sorted({w.lower() for w in words}), notes


def _contains(data: bytes, word: str) -> bool:
    lowered = data.lower()  # ASCII letters only; enough for names, in UTF-8 and UTF-16 alike
    return any(word.encode(encoding) in lowered for encoding in ("utf-8", "utf-16-le", "utf-16-be"))


def _git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run([find_tool("git"), "-C", str(repo), *args], capture_output=True)
    if result.returncode != 0:
        raise SetupError("git %s failed: %s" % (" ".join(args[:3]), result.stderr.decode("utf-8", "replace").strip()))
    return result.stdout


def private_name_hits(repo: Path, words: Sequence[str], commit: Optional[str] = None) -> List[str]:
    """Files whose path or content contains one of the words: in the working tree, or in a commit.

    Returns "path (word N)" entries; the words themselves are not repeated.
    Contents in UTF-8 or UTF-16 are searched; a symbolic link's target counts as
    its content. The commit is read blob by blob (git ls-tree and cat-file), so
    no export-ignore attribute can hide a file. old_software/ is not opened.
    """
    hits: List[str] = []

    def check(name: str, data: bytes) -> None:
        if name.startswith(PRIVATE_SCAN_EXCLUDED):
            return
        for n, word in enumerate(words):
            if word in name.lower():
                hits.append("%s (word %d, in the path)" % (name, n + 1))
            elif _contains(data, word):
                hits.append("%s (word %d)" % (name, n + 1))

    if commit is None:
        listed = _git_bytes(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        for name in listed.decode("utf-8", "replace").split("\0"):
            path = repo / name
            if not name or name.startswith(PRIVATE_SCAN_EXCLUDED):
                continue
            if path.is_symlink():
                check(name, os.readlink(str(path)).encode("utf-8", "replace"))
            elif path.is_file():
                check(name, path.read_bytes())
        return hits
    entries = []
    for record in _git_bytes(repo, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if record:
            meta, _, name = record.partition(b"\t")
            mode, kind, sha = meta.split()
            if kind == b"blob":
                entries.append((name.decode("utf-8", "replace"), sha.decode()))
    entries = [(name, sha) for name, sha in entries if not name.startswith(PRIVATE_SCAN_EXCLUDED)]
    for (name, _), (_, _, data) in zip(entries, _cat_file(repo, [sha for _, sha in entries])):
        check(name, data)
    return hits


def _cat_file(repo: Path, shas: Sequence[str]) -> List[Tuple[str, str, bytes]]:
    """(sha, type, content) of each object, read with git cat-file --batch."""
    if not shas:
        return []
    batch = subprocess.run([find_tool("git"), "-C", str(repo), "cat-file", "--batch"],
                           input=b"".join(sha.encode() + b"\n" for sha in shas), capture_output=True)
    if batch.returncode != 0:
        raise SetupError("git cat-file failed: %s" % batch.stderr.decode("utf-8", "replace").strip())
    out, pos, objects = batch.stdout, 0, []
    for sha in shas:
        header_end = out.index(b"\n", pos)
        header = out[pos:header_end].split()
        if len(header) != 3 or header[0].decode() != sha:
            raise SetupError("git cat-file gave an unexpected answer for %s" % sha)
        size = int(header[2])
        objects.append((sha, header[1].decode(), out[header_end + 1:header_end + 1 + size]))
        pos = header_end + 1 + size + 1
    return objects


def unpublished_range(repo: Path) -> Optional[str]:
    """base..HEAD for the commits not yet on the remote (the branch's upstream, else origin/main), or None."""
    for base in ("@{upstream}", "origin/main"):
        probe = subprocess.run([find_tool("git"), "-C", str(repo), "rev-parse", "--verify", "--quiet", base],
                               capture_output=True)
        if probe.returncode == 0:
            return "%s..HEAD" % base
    return None


def private_history_hits(repo: Path, words: Sequence[str], revisions: str) -> List[str]:
    """Objects a push of the range would upload that contain one of the words.

    Every commit (message, author), every path and every file version in the
    range is read as stored, with git rev-list --objects and cat-file, so a
    file git treats as binary (UTF-16 text, for one) is read too, and a file
    added in one commit and deleted in the next is still found.
    """
    listed = _git_bytes(repo, "rev-list", "--objects", revisions).decode("utf-8", "replace").splitlines()
    objects = [(line.split(" ", 1) + [""])[:2] for line in listed if line]  # "sha" or "sha path"
    hits: List[str] = []
    for (sha, path), (_, kind, data) in zip(objects, _cat_file(repo, [sha for sha, _ in objects])):
        if path.startswith(PRIVATE_SCAN_EXCLUDED):
            continue
        where = "commit %s" % sha[:12] if kind == "commit" else "%s %s" % (path or "(root)", sha[:12])
        for n, word in enumerate(words):
            if word in path.lower():
                hits.append("%s (word %d, in the path)" % (where, n + 1))
            elif kind in ("commit", "blob") and _contains(data, word):
                hits.append("%s (word %d)" % (where, n + 1))
    return hits


# --------------------------------------------------------------------------
# One run at a time
# --------------------------------------------------------------------------


class RunLock:
    """An exclusive lock on HOME/lock, released by the OS even if this process dies."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Optional[IO[bytes]] = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise SetupError("another local CI run is in progress (it holds %s); wait for it to finish"
                             % self.path) from None
        self._fh = fh
        return self

    def __exit__(self, *exc: object) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


# --------------------------------------------------------------------------
# Running a command: output to the screen and the log, a deadline, a clean kill
# --------------------------------------------------------------------------


def lower_own_priority() -> str:
    """Make this process and everything it starts yield the CPU to other work."""
    if os.name == "nt":
        return "below normal (Windows priority class of every command)"
    os.nice(10)
    return "nice +10 (this script and everything it starts)"


def _popen_kwargs(low_priority: bool) -> Dict[str, object]:
    if os.name == "nt":
        flags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0) if low_priority else 0
        return {"creationflags": flags}
    return {"start_new_session": True}  # its own process group, so a timeout kills the whole tree


def kill_tree(proc: "subprocess.Popen[bytes]") -> Optional[str]:
    """Kill the process and its children. Returns a problem description, or None."""
    if proc.poll() is not None:
        return None
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                                capture_output=True, text=True, errors="replace")
        if result.returncode != 0 and proc.poll() is None:
            proc.kill()
            return "taskkill failed (%s); killed only the top process, its children may still run" % (
                result.stderr.strip() or result.returncode)
        return None
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return None


_screen_lock = threading.Lock()
_screen_problems: List[str] = []


def _screen(text: str) -> None:
    """Show text. A screen that cannot take it never stops the run or the log (problems are reported later)."""
    try:
        with _screen_lock:
            sys.stdout.write(text)
            sys.stdout.flush()
    except Exception as exc:  # recorded, then reported by the caller: never silent
        if len(_screen_problems) < 3:
            _screen_problems.append(repr(exc))


def _pump(source: IO[bytes], log: IO[bytes], encoding: str) -> None:
    """Copy a command's output: raw bytes to the log first, then decoded text to the screen.

    Decoding with an incremental decoder means the screen only ever gets whole
    characters. (Writing raw bytes to a Windows console, a chunk ending inside
    a UTF-8 sequence makes Python 3.12.8+ flush forever.)
    """
    # Newlines are translated, so a child's \r\n is not doubled by this script's own stdout on Windows.
    decoder = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder(encoding)(errors="replace"), translate=True)
    read = getattr(source, "read1", None) or source.read
    while True:
        chunk = read(65536)
        if not chunk:
            tail = decoder.decode(b"", final=True)
            if tail:
                _screen(tail)
            return
        log.write(chunk)
        log.flush()
        text = decoder.decode(chunk)
        if text:
            _screen(text)


def output_encoding() -> str:
    """The encoding the jobs' Python commands write to a pipe, used to show their output.

    On Windows that is the ANSI code page whatever mode this script runs in (the
    jobs get no PYTHONUTF8 or PYTHONIOENCODING, as on GitHub); elsewhere the
    locale's. uv, git and docker write UTF-8 and are decoded as such.
    """
    if os.name == "nt":
        import ctypes

        return "cp%d" % ctypes.windll.kernel32.GetACP()  # type: ignore[attr-defined]
    return locale.getpreferredencoding(False) or "utf-8"


def run_logged(argv: Sequence[str], cwd: Path, env: Mapping[str, str], log: IO[bytes],
               deadline: float, low_priority: bool,
               on_interrupt: Optional[Callable[[], None]] = None,
               encoding: Optional[str] = None) -> Tuple[str, Optional[int]]:
    """Run argv with its output on the screen and in the log.

    Returns ("ok", 0), ("failed", exit code) or ("timeout", None). Ctrl+C kills
    the command (and calls on_interrupt) before KeyboardInterrupt goes on up.
    """
    if time.monotonic() >= deadline:
        return "timeout", None
    try:
        proc = subprocess.Popen(list(argv), cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                **_popen_kwargs(low_priority))  # type: ignore[call-overload]
    except FileNotFoundError as exc:
        raise SetupError("cannot start %r in %s: %s" % (argv[0], cwd, exc)) from None
    assert proc.stdout is not None
    pump = threading.Thread(target=_pump, args=(proc.stdout, log, encoding or output_encoding()), daemon=True)
    pump.start()
    try:
        while True:
            try:
                code = proc.wait(timeout=min(1.0, max(0.05, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    problem = kill_tree(proc)
                    proc.wait()
                    pump.join(timeout=10)
                    if problem:
                        _log_line(log, "local-ci: " + problem)
                    return "timeout", None
    except KeyboardInterrupt:
        problem = kill_tree(proc)
        if on_interrupt is not None:
            on_interrupt()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            problem = (problem or "") + " the command did not stop within 15 s"
        if problem:
            _log_line(log, "local-ci: " + problem.strip())
        raise
    finally:
        pump.join(timeout=10)
        if pump.is_alive():
            _log_line(log, "local-ci: the command's output was still being copied after 10 s; the log may be "
                           "incomplete")
        if _screen_problems:
            problems = ", ".join(_screen_problems)
            del _screen_problems[:]
            _log_line(log, "local-ci: part of the output could not be shown on the screen (%s); the log has all "
                           "of it" % problems)
    return ("ok", 0) if code == 0 else ("failed", code)


def _log_line(log: IO[bytes], text: str) -> None:
    log.write((text + "\n").encode("utf-8", "replace"))
    log.flush()
    _screen(text + "\n")


# --------------------------------------------------------------------------
# Host mode: one job with one Python version, in a fresh virtual environment
# --------------------------------------------------------------------------


def clean_environ(base: Mapping[str, str]) -> Tuple[Dict[str, str], List[str]]:
    """base without the variables GitHub's runners do not set; also returns the dropped names."""
    kept: Dict[str, str] = {}
    dropped: List[str] = []
    for name, value in base.items():
        upper = name.upper()
        credential = any(word in upper for word in CREDENTIAL_WORDS) and not upper.startswith(DOWNLOAD_AUTH_PREFIXES)
        if upper.startswith(DROPPED_PREFIXES) or upper in DROPPED_NAMES or credential:
            dropped.append(name)
        else:
            kept[name] = value
    return kept, sorted(dropped)


def download_settings(base: Mapping[str, str]) -> List[str]:
    """Names of the pip and uv settings that are passed on (for the banner)."""
    clean, _ = clean_environ(base)
    return sorted(name for name in clean if name.upper().startswith(DOWNLOAD_PREFIXES))


def job_environ(base: Mapping[str, str], venv: Path, job: Job) -> Dict[str, str]:
    env, _ = clean_environ(base)
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
    env[path_key] = str(scripts) + os.pathsep + env.get(path_key, "")
    env["VIRTUAL_ENV"] = str(venv)
    # A GitHub runner has no personal git settings (signing, hooks, aliases); nor do the jobs here.
    env["GIT_CONFIG_GLOBAL"] = str(empty_git_config(venv.parent.parent))
    env.update(dict(CI_ENV))
    env.update(dict(job.env))
    return env


def empty_git_config(home: Path) -> Path:
    path = home / "empty-gitconfig"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Empty on purpose: the jobs run without your personal git settings, as on GitHub.\n",
                        encoding="utf-8")
    return path


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def remove_venv(venv: Path, venvs_root: Path) -> None:
    """Delete a virtual environment this script made earlier, and nothing else."""
    if not venv.exists():
        return
    if venv.resolve().parent != venvs_root.resolve():
        raise SetupError("refusing to delete %s: it is not directly inside %s" % (venv, venvs_root))
    if not (venv / "pyvenv.cfg").is_file():
        raise SetupError("refusing to delete %s: it has no pyvenv.cfg, so this script did not make it. "
                         "Move or delete it yourself." % venv)
    for attempt in range(3):
        try:
            # pyvenv.cfg goes last, so a deletion that stops halfway leaves the marker
            # and the next run can still recognise the folder as its own.
            for child in venv.iterdir():
                if child.name != "pyvenv.cfg":
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
            (venv / "pyvenv.cfg").unlink()
            venv.rmdir()
            return
        except OSError as exc:
            if attempt == 2:
                raise SetupError("cannot delete the previous virtual environment %s: %s. A program from an earlier "
                                 "run may still be using it; close it and run again." % (venv, exc)) from None
            time.sleep(1.0)


def resolve_command(argv: List[str], path: str) -> Optional[List[str]]:
    """argv with its program found on the job's PATH (the virtual environment first), as on GitHub.

    Without this, Windows would look the program up on this script's own PATH.
    None when it is not there. An absolute program (the job's Python) is kept.
    """
    if os.path.isabs(argv[0]):
        return argv
    found = shutil.which(argv[0], path=path)
    return None if found is None else [found] + argv[1:]


# The script runs each command without a shell, so a command may not rely on one:
# no variables, redirections, pipes, command lists, globs, subshells or GitHub's
# step files. tests/test_local_ci.py applies the same rule to ci.yml.
SHELL_SYNTAX = re.compile(r"[$`><|&;*?~{}()]|GITHUB_(?:ENV|PATH|OUTPUT|STEP_SUMMARY)|(?:^|\s)#"
                          r"|^\s*[A-Za-z_][A-Za-z0-9_]*=")
SHELL_BUILTINS = frozenset({"cd", "pushd", "popd", "export", "unset", "set", "source", ".", "alias", "eval", "exec",
                            "time", "ulimit", "umask", "if", "for", "while", "until", "case", "function", "echo",
                            "test", "[", "true", "false", "shopt", "trap", "read", "Set-Location", "Write-Output"})


def shell_syntax(command: str) -> Optional[str]:
    """The shell syntax in a command that a shell-less run cannot reproduce, or None.

    Includes a first word that is a shell builtin or keyword (cd, export, echo,
    ...), a leading VAR=value assignment, and a '#' comment.
    """
    found = SHELL_SYNTAX.search(command)
    if found:
        return found.group(0).strip()
    first = command.split(None, 1)[0] if command.split() else ""
    return first if first in SHELL_BUILTINS else None


def command_argv(command: str, python: Path) -> List[str]:
    syntax = shell_syntax(command)
    if syntax is not None:
        raise SetupError("the command %r uses shell syntax (%r) that this script cannot run without a shell"
                         % (command, syntax))
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise SetupError("cannot split the command %r: %s" % (command, exc)) from None
    if argv and argv[0] == "python":
        argv[0] = str(python)
    return argv


@dataclass
class Result:
    job: str
    python: str
    where: str
    status: str  # passed, failed, timed out, interrupted, not run, error
    seconds: float = 0.0
    detail: str = ""
    log: Optional[Path] = None


def find_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise SetupError("%s is not installed or not on PATH. %s" % (name, INSTALL_HINTS[name]))
    return path


INSTALL_HINTS = {
    "uv": "Install it once: 'winget install astral-sh.uv' (Windows), or 'python -m pip install uv'. "
          "It provides each Python version the CI jobs use.",
    "git": "Install Git (https://git-scm.com); the repository checks use it.",
    "docker": "Install and start Docker Desktop, or use --where host.",
}


def run_job_host(repo: Path, job: Job, python: str, home: Path, log: IO[bytes], low_priority: bool,
                 base_env: Mapping[str, str]) -> Tuple[str, str]:
    """Returns (status, detail): status is passed, failed or timed out."""
    deadline = time.monotonic() + job.timeout_minutes * 60
    venvs = home / "venvs"
    venvs.mkdir(parents=True, exist_ok=True)
    venv = venvs / ("%s-py%s" % (job.name, python))
    remove_venv(venv, venvs)
    uv = find_tool("uv")
    clean, _ = clean_environ(base_env)
    _log_line(log, "::: %s / Python %s: set up Python (uv venv)" % (job.name, python))
    status, code = run_logged([uv, "venv", "--no-project", "--seed", "--python", python, str(venv)],
                              home, clean, log, deadline, low_priority, encoding="utf-8")
    if status != "ok":
        return _verdict(status, "setting up Python %s with uv (exit %s)" % (python, code))
    interpreter = venv_python(venv)
    probe = subprocess.run([str(interpreter), "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                           capture_output=True, text=True, env=clean)
    actual = probe.stdout.strip()
    if probe.returncode != 0 or not (actual == python or actual.startswith(python + ".")):
        return "failed", "uv gave Python %r for %s (%s)" % (actual, python, probe.stderr.strip()[:200])
    _log_line(log, "local-ci: Python %s at %s" % (actual, interpreter))
    env = job_environ(base_env, venv, job)
    path = next((value for key, value in env.items() if key.upper() == "PATH"), "")
    for step in job.steps:
        _log_line(log, "::: %s / Python %s: %s" % (job.name, python, step.name))
        cwd = repo / step.cwd
        if not cwd.is_dir():
            return "failed", "step '%s': working-directory %s does not exist" % (step.name, step.cwd)
        for command in step.commands:
            _log_line(log, "$ %s%s" % ("(in %s) " % step.cwd if step.cwd != "." else "", command))
            argv = resolve_command(command_argv(command, interpreter), path)
            if argv is None:
                return "failed", "step '%s': %r is not on the job's PATH" % (step.name, command.split()[0])
            status, code = run_logged(argv, cwd, env, log, deadline, low_priority)
            if status != "ok":
                return _verdict(status, "step '%s': %s (exit %s)" % (step.name, command, code))
    return "passed", ""


def _verdict(status: str, detail: str) -> Tuple[str, str]:
    if status == "timeout":
        return "timed out", "CI's limit for this job was reached during " + detail.split(" (exit")[0]
    return "failed", detail


# --------------------------------------------------------------------------
# Docker mode: the same job inside a throwaway Linux container
# --------------------------------------------------------------------------


def image_tag(dockerfile: Path = DOCKERFILE) -> str:
    digest = hashlib.sha256(dockerfile.read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:12]
    return "%s:%s" % (DOCKER_PREFIX, digest)


CA_IN_CONTAINER = "/etc/goliath-local-ci/ca-bundle.crt"
IN_CONTAINER_MARKER = Path("/.dockerenv")


def _mount_source(path: Path, what: str) -> str:
    source = str(path.resolve())
    if "," in source:
        raise SetupError("docker mode cannot mount the %s %s: the path contains a comma" % (what, source))
    return source


def ca_bundle(environ: Mapping[str, str] = os.environ) -> Optional[Path]:
    """GOLIATH_LOCAL_CI_CA_BUNDLE: extra CA certificates for a proxy that re-signs HTTPS (docker mode)."""
    value = environ.get("GOLIATH_LOCAL_CI_CA_BUNDLE")
    if not value:
        return None
    path = Path(value)
    if not path.is_file():
        raise SetupError("GOLIATH_LOCAL_CI_CA_BUNDLE=%s is not a file" % value)
    return path


# What the container runs: clone the read-only checkout, check out the commit
# under test, then run THAT commit's copy of this script, so the jobs and their
# commands come from the commit, not from the working tree. safe.directory goes
# into the throwaway container's global git config, which git's local clone
# (upload-pack) also reads; a -c option would not reach it.
DOCKER_BOOTSTRAP = (
    'set -eu; '
    'git config --global --add safe.directory "*"; '
    'git clone --quiet --no-checkout --no-hardlinks /src /work/repo '
    '|| { echo "local-ci: git clone of the mounted checkout failed" >&2; exit 2; }; '
    'cd /work/repo; '
    'git checkout --quiet --detach "$1" '
    '|| { echo "local-ci: git checkout $1 failed" >&2; exit 2; }; '
    'if [ ! -f tools/local_ci.py ]; then echo "local-ci: commit $1 has no tools/local_ci.py" >&2; exit 2; fi; '
    'echo "local-ci: testing commit $1 in a fresh clone"; '
    'exec python3 tools/local_ci.py --inside-docker --commit "$1" --job "$2" --python "$3"'
)


def docker_run_argv(docker: str, repo: Path, image: str, container: str, cpus: str, commit: str,
                    job: str, python: str, ca: Optional[Path] = None, cpu_shares: Optional[int] = None) -> List[str]:
    argv = [
        docker, "run", "--rm", "--name", container,
        "--cpus", cpus, "--network", DOCKER_NETWORK,
        "--cap-drop", "NET_RAW", "--security-opt", "no-new-privileges",
        "--mount", "type=bind,source=%s,target=/src,readonly" % _mount_source(repo, "repository"),
        "--mount", "type=volume,source=%s,target=/cache" % DOCKER_VOLUME,
        "-e", "UV_CACHE_DIR=/cache/uv", "-e", "UV_PYTHON_INSTALL_DIR=/cache/python",
        "-e", "PIP_CACHE_DIR=/cache/pip", "-e", "GOLIATH_LOCAL_CI_HOME=/tmp/goliath-local-ci",
    ]
    for name, value in CI_ENV:
        argv += ["-e", "%s=%s" % (name, value)]
    if cpu_shares is not None:
        argv += ["--cpu-shares", str(cpu_shares)]
    if ca is not None:
        argv += ["--mount", "type=bind,source=%s,target=%s,readonly" % (_mount_source(ca, "CA bundle"),
                                                                        CA_IN_CONTAINER)]
        for name in ("SSL_CERT_FILE", "PIP_CERT", "REQUESTS_CA_BUNDLE"):
            argv += ["-e", "%s=%s" % (name, CA_IN_CONTAINER)]
    return argv + [image, "sh", "-c", DOCKER_BOOTSTRAP, "sh", commit, job, python]


JOB_DEFINITIONS = ("tools/local_ci.py", ".github/workflows/ci.yml")


def check_docker_checkout(repo: Path) -> None:
    if (repo / ".git").is_file():
        raise SetupError("--where docker needs an ordinary clone: %s is a git worktree (its .git is a file "
                         "pointing elsewhere), so the container cannot see its history. Use --where host, "
                         "or run from the main clone." % repo)
    if not (repo / ".git").is_dir():
        raise SetupError("--where docker needs a git clone; %s has no .git folder" % repo)
    changed = subprocess.run([find_tool("git"), "-C", str(repo), "status", "--porcelain", "--"] + list(JOB_DEFINITIONS),
                             capture_output=True, text=True, errors="replace")
    if changed.returncode != 0 or changed.stdout.strip():
        raise SetupError("--where docker tests the commit with the commit's own job list, but %s differ from it; "
                         "commit or stash those changes first (or use --where host)" % " and ".join(JOB_DEFINITIONS))


def ensure_docker(cpus: str) -> str:
    """The docker command, after checking its engine answers and has at least --cpus CPUs."""
    docker = find_tool("docker")
    result = subprocess.run([docker, "info", "--format", "{{.NCPU}}"],
                            capture_output=True, text=True, errors="replace")
    if result.returncode != 0:
        raise SetupError("Docker is installed but its engine does not answer (is Docker Desktop running?): %s"
                         % (result.stderr.strip()[:300] or result.returncode))
    try:
        engine_cpus = int(result.stdout.strip())
    except ValueError:
        raise SetupError("docker info reported %r CPUs, not a number" % result.stdout.strip()) from None
    if float(cpus) > engine_cpus:
        raise SetupError("--cpus %s is more than the %d CPU(s) Docker's engine has (Docker Desktop: Settings, "
                         "Resources); use --cpus %d or fewer" % (cpus, engine_cpus, engine_cpus))
    return docker


def ensure_network(docker: str) -> None:
    """Create this project's own bridge network for the job containers, once."""
    if subprocess.run([docker, "network", "inspect", DOCKER_NETWORK], capture_output=True).returncode == 0:
        return
    made = subprocess.run([docker, "network", "create", "--driver", "bridge", DOCKER_NETWORK],
                          capture_output=True, text=True, errors="replace")
    if made.returncode != 0:
        raise SetupError("docker network create %s failed: %s" % (DOCKER_NETWORK, made.stderr.strip()))


def ensure_image(docker: str, guard: BusyGuard, log_dir: Path, low_priority: bool) -> str:
    tag = image_tag()
    if subprocess.run([docker, "image", "inspect", tag], capture_output=True).returncode == 0:
        return tag
    guard.check("building the image %s" % tag)
    say("::: building %s from %s (once; later runs reuse it)" % (tag, DOCKERFILE.relative_to(REPO)))
    with (log_dir / "docker-build.log").open("wb") as log:
        status, code = run_logged([docker, "build", "-t", tag, "-f", str(DOCKERFILE), str(DOCKERFILE.parent)],
                                  REPO, os.environ, log, time.monotonic() + 1800, low_priority, encoding="utf-8")
    if status != "ok":
        raise SetupError("building %s failed (%s, exit %s); see %s" % (tag, status, code, log_dir / "docker-build.log"))
    return tag


def run_job_docker(docker: str, image: str, repo: Path, commit: str, job: Job, python: str, cpus: str,
                   log: IO[bytes], low_priority: bool) -> Tuple[str, str]:
    container = "%s-%s-py%s-%d" % (DOCKER_PREFIX, job.name, python.replace(".", ""), os.getpid())
    argv = docker_run_argv(docker, repo, image, container, cpus, commit, job.name, python, ca_bundle(),
                           DOCKER_LOW_CPU_SHARES if low_priority else None)

    def stop_container() -> None:
        result = subprocess.run([docker, "kill", container], capture_output=True, text=True, errors="replace")
        if result.returncode != 0 and "No such container" not in result.stderr:
            _log_line(log, "local-ci: docker kill %s failed: %s" % (container, result.stderr.strip()))

    deadline = time.monotonic() + job.timeout_minutes * 60 + DOCKER_GRACE_SECONDS
    status, code = run_logged(argv, repo, os.environ, log, deadline, low_priority, on_interrupt=stop_container,
                              encoding="utf-8")
    if status == "timeout":
        stop_container()
        return "timed out", "the container did not finish within CI's limit plus %d s" % DOCKER_GRACE_SECONDS
    if code == EXIT_TIMEOUT_INNER:
        return "timed out", "CI's limit for this job was reached (see the log)"
    if code == EXIT_SETUP:
        return "error", "the container could not set up the run (see the log)"
    if code in DOCKER_CANNOT_START:
        return "error", "docker could not start the container (exit %s; see the log)" % code
    if status != "ok":
        return "failed", "exit %s (see the log)" % code
    return "passed", ""


def inside_docker_main(args: argparse.Namespace) -> int:
    """The half of --where docker that runs in the container, from the clone of the commit under test."""
    if not IN_CONTAINER_MARKER.exists():
        say("local-ci: --inside-docker is only for the container that --where docker starts")
        return EXIT_SETUP
    try:
        job = _job(args.job[0])
        python = args.python[0]
        if python not in job.pythons:
            say("local-ci: this commit's %s job runs Python %s, not %s: the working tree's job list differs from "
                "the commit's (--where docker tests the commit)" % (job.name, ", ".join(job.pythons), python))
            return EXIT_SETUP
        head = git_out(REPO, "rev-parse", "HEAD")
        if head != args.commit:
            say("local-ci: the clone is at %s, not at the commit to test %s" % (head, args.commit))
            return EXIT_SETUP
        status, detail = run_job_host(REPO, job, python, default_home(), _stdout_null(), False, os.environ)
    except SetupError as exc:
        say("local-ci: %s" % exc)
        return EXIT_SETUP
    except KeyboardInterrupt:
        say("local-ci: interrupted")
        return EXIT_INTERRUPTED
    if detail:
        say("local-ci: %s: %s" % (status, detail))
    return {"passed": EXIT_OK, "timed out": EXIT_TIMEOUT_INNER}.get(status, EXIT_FAILED)


class _NullLog:
    def write(self, data: bytes) -> int:
        return len(data)

    def flush(self) -> None:
        pass


def _stdout_null() -> IO[bytes]:
    # Inside the container the output goes to stdout only; the outer script keeps the log.
    return _NullLog()  # type: ignore[return-value]


# --------------------------------------------------------------------------
# Git facts about the checkout
# --------------------------------------------------------------------------


def git_out(repo: Path, *args: str) -> str:
    result = subprocess.run([find_tool("git"), "-C", str(repo), *args], capture_output=True, text=True,
                            errors="replace")
    if result.returncode != 0:
        raise SetupError("git %s failed in %s: %s" % (" ".join(args), repo, result.stderr.strip()))
    return result.stdout.strip()


def checkout_facts(repo: Path) -> Tuple[str, int]:
    """(HEAD commit, number of changed or untracked paths)."""
    commit = git_out(repo, "rev-parse", "HEAD")
    status = git_out(repo, "status", "--porcelain")
    return commit, len([line for line in status.splitlines() if line.strip()])


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def _job(name: str) -> Job:
    for job in JOBS:
        if job.name == name:
            return job
    raise SetupError("no job %r; the jobs are %s" % (name, ", ".join(j.name for j in JOBS)))


def plan(job_names: Sequence[str], pythons: Sequence[str]) -> List[Tuple[Job, str]]:
    job_names, pythons = list(dict.fromkeys(job_names)), list(dict.fromkeys(pythons))
    jobs = [_job(name) for name in job_names] if job_names else list(JOBS)
    available = sorted({p for job in jobs for p in job.pythons}, key=lambda v: tuple(int(x) for x in v.split(".")))
    unknown = [p for p in pythons if p not in available]
    if unknown:
        raise SetupError("no selected job runs Python %s in CI; they run %s"
                         % (", ".join(unknown), ", ".join(available)))
    for job in jobs if job_names and pythons else []:
        if not any(p in pythons for p in job.pythons):
            raise SetupError("--job %s runs Python %s in CI, none of the --python versions given"
                             % (job.name, ", ".join(job.pythons)))
    return [(job, p) for job in jobs for p in job.pythons if not pythons or p in pythons]


def without_ci_counterpart(selected: Sequence[Tuple[Job, str]], where: str) -> List[str]:
    """Job/version pairs that CI never runs on the kind of system this run uses."""
    family = "ubuntu" if where == "docker" or sys.platform.startswith("linux") else \
        "windows" if os.name == "nt" else "macos"
    return ["%s/%s" % (job.name, python) for job, python in selected
            if not any(runner.startswith(family) and version == python for runner, version in job.ci_matrix)]


def mirrored_runners(where: str) -> str:
    if where == "docker":
        return "the Linux jobs (here: a Debian container; on GitHub: Ubuntu)"
    if os.name == "nt":
        return "the windows-latest jobs"
    if sys.platform == "darwin":
        return "no CI job (CI has no macOS runner)"
    return "the Linux jobs (here: this computer; on GitHub: Ubuntu)"


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python tools/local_ci.py",
        description="Run this repository's CI jobs (.github/workflows/ci.yml) on this machine.",
        epilog="Details: tools/local-ci.md")
    parser.add_argument("--where", choices=("host", "docker"), default="host",
                        help="host: this computer, with uv (default); docker: throwaway Linux containers")
    parser.add_argument("--job", action="append", default=[], metavar="NAME",
                        help="run only this job (repeatable): %s" % ", ".join(j.name for j in JOBS))
    parser.add_argument("--python", action="append", default=[], metavar="X.Y",
                        help="run only this Python version (repeatable)")
    parser.add_argument("--fail-fast", action="store_true", help="stop after the first job that fails")
    busy = parser.add_mutually_exclusive_group()
    busy.add_argument("--wait", action="store_true",
                      help="when the configured other CI is running, wait for it instead of stopping")
    busy.add_argument("--ignore-busy", action="store_true", help="do not check the configured other CI")
    parser.add_argument("--cpus", default="2", help="--where docker: CPUs each container may use (default 2)")
    parser.add_argument("--priority", choices=("low", "normal"), default="low",
                        help="CPU priority of everything this script runs (default low)")
    parser.add_argument("--list", action="store_true", help="print the jobs and their commands, run nothing")
    parser.add_argument("--dry-run", action="store_true", help="print what would run, run nothing")
    parser.add_argument("--configure-busy-check", nargs=2, metavar=("OWNER/REPO", "WORKFLOW"),
                        help="save the other project's CI to check before each job, then exit")
    parser.add_argument("--inside-docker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--commit", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _validate_cpus(text: str) -> str:
    try:
        value = float(text)
    except ValueError:
        raise SetupError("--cpus %r is not a number" % text) from None
    limit = os.cpu_count() or 1
    if not 0 < value <= limit:
        raise SetupError("--cpus must be more than 0 and at most %d (this machine's CPUs)" % limit)
    return text


def print_list(selected: List[Tuple[Job, str]]) -> None:
    for job in dict.fromkeys(job for job, _ in selected):
        pythons = [p for j, p in selected if j is job]
        say("%s  (Python %s; CI runs it on %s; time limit %d min)"
            % (job.name, ", ".join(pythons), job.ci_runners, job.timeout_minutes))
        for name, value in job.env:
            say("    env %s=%s" % (name, value))
        for step in job.steps:
            say("    %s%s" % (step.name, "  [in %s]" % step.cwd if step.cwd != "." else ""))
            for command in step.commands:
                say("      $ %s" % command)


def summary(results: List[Result]) -> None:
    say()
    say("Summary")
    for r in results:
        line = "  %-5s Python %-5s %-12s %6.0f s" % (r.job, r.python, r.status.upper(), r.seconds)
        if r.detail:
            line += "  " + r.detail
        say(line)
        if r.log is not None:
            say("        log: %s" % r.log)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:  # a character the screen cannot show is escaped, never an error
            reconfigure(errors="backslashreplace")
        except (ValueError, OSError) as exc:
            say("local-ci: note: could not set the screen's error handling (%s)" % exc)
    try:
        if args.inside_docker:
            return inside_docker_main(args)
        return _main(args)
    except SetupError as exc:
        say("local-ci: %s" % exc)
        return EXIT_SETUP
    except KeyboardInterrupt:
        say("local-ci: interrupted")
        return EXIT_INTERRUPTED


def _priority_line(where: str, cpus: str) -> str:
    if where == "docker":
        return ("containers: CPU weight %d (Docker's default is 1024) and at most %s CPU(s); the docker "
                "client: %s; the image build is not lowered" % (DOCKER_LOW_CPU_SHARES, cpus, lower_own_priority()))
    return lower_own_priority()


def _new_log_dir(home: Path) -> Path:
    """A new folder for this run's logs: named by the time, made unique by the OS (never a shared one)."""
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(tempfile.mkdtemp(prefix=stamp + "-", dir=str(logs)))


def _main(args: argparse.Namespace) -> int:
    config_file = default_config_file()
    check_config_file(config_file, REPO)
    if args.configure_busy_check:
        path = config_file
        config = write_busy_config(path, *args.configure_busy_check)
        say("Saved %s: before each job, check whether a '%s' run of %s is in progress (GitHub knows this "
            "workflow)." % (path, config.workflow, config.repo))
        return EXIT_OK
    selected = plan(args.job, args.python)
    if args.list:
        print_list(selected)
        return EXIT_OK
    cpus = _validate_cpus(args.cpus)
    mode = "ignore" if args.ignore_busy else "wait" if args.wait else "stop"
    guard = BusyGuard(load_busy_config(os.environ, config_file), mode)
    words, word_notes = private_words(os.environ, config_file)
    home = default_home()
    check_home(home, REPO)
    commit, changed = checkout_facts(REPO)
    _, dropped = clean_environ(os.environ)
    lines = [
        "Local CI: %s" % ", ".join("%s/%s" % (j.name, p) for j, p in selected),
        "Where:    %s, which stands in for %s" % (args.where, mirrored_runners(args.where)),
        "Checkout: %s at commit %s" % (REPO, commit[:12]),
    ]
    if args.where == "docker":
        lines.append("          tests the commit only, with the commit's own copy of this script: %s"
                     % ("%d uncommitted change(s) are NOT tested" % changed if changed else "no uncommitted changes"))
    else:
        lines.append("          tests the working tree as it is (%d uncommitted change(s))" % changed)
    lines.append("Busy check: " + guard.describe())
    if args.where == "docker":
        lines.append("Private names: %s" % ("%d word(s) from this machine's settings; this script checks the commit "
                                            "for them before the jobs" % len(words) if words else
                                            "none configured, so not checked (see tools/local-ci.md)"))
    lines += ["Note:     " + note for note in word_notes]
    for pair in without_ci_counterpart(selected, args.where):
        lines.append("Note:     CI never runs %s on this kind of system (CI runs it on %s), so its result here has no "
                     "CI counterpart" % (pair, ", ".join(_job(pair.split("/")[0]).ci_os)))
    lines.append("CI=true:  set for every command, as GitHub does (Hypothesis then uses its CI settings)")
    passed_on = download_settings(os.environ)
    if args.where == "host":
        if dropped:
            lines.append("Not passed on (GitHub's runners do not set them; credentials never): " + ", ".join(dropped))
        if passed_on:
            lines.append("Passed on (pip and uv download settings): " + ", ".join(passed_on))
    else:
        lines.append("Environment: the containers get none of yours, only the cache and CA settings below")
        lines.append("Limits:   %s CPU(s) per container; cache volume %s; network %s (not the default bridge); no "
                     "NET_RAW, no new privileges" % (cpus, DOCKER_VOLUME, DOCKER_NETWORK))
        extra_ca = ca_bundle()
        if extra_ca is not None:
            lines.append("Extra CA: %s (GOLIATH_LOCAL_CI_CA_BUNDLE), trusted inside the containers" % extra_ca)
    if args.dry_run:
        banner(lines)
        print_list(selected)
        say("(dry run: nothing was run)")
        return EXIT_OK
    low = args.priority == "low"
    lines.append("Priority: " + (_priority_line(args.where, cpus) if low else "normal (--priority normal)"))
    find_tool("git")
    docker = ""
    if args.where == "docker":
        check_docker_checkout(REPO)
        docker = ensure_docker(cpus)
    else:
        find_tool("uv")

    results: List[Result] = []
    running: List[Result] = []  # the job being run right now, if any
    with RunLock(home / "lock"):
        log_dir = _new_log_dir(home)
        lines.append("Logs:     %s" % log_dir)
        banner(lines)
        try:
            if args.where == "docker" and words:
                revisions = unpublished_range(REPO)
                hits = private_name_hits(REPO, words, commit)
                hits += private_history_hits(REPO, words, revisions) if revisions else []
                checked = "commit %s%s" % (commit[:12], " and the messages and changes of %s" % revisions
                                           if revisions else " (no upstream or origin/main: commit messages not "
                                           "checked)")
                results.append(Result("names", "-", "docker", "failed" if hits else "passed",
                                      detail=("a private name from this machine's settings is in: %s. Remove it from "
                                              "history (amend or rebase before pushing): a new commit leaves it in "
                                              "the old one" % ", ".join(hits)) if hits else
                                      "%s: none of the configured words" % checked))
            image = ensure_image(docker, guard, log_dir, low) if args.where == "docker" else ""
            if args.where == "docker":
                ensure_network(docker)
            for index, (job, python) in enumerate(selected):
                guard.check("%s / Python %s" % (job.name, python))
                log_path = log_dir / ("%s-py%s.log" % (job.name, python))
                result = Result(job.name, python, args.where, "error", log=log_path)
                results.append(result)
                running[:] = [result]
                started = time.monotonic()
                say()
                say("::: %s / Python %s (%s) :::" % (job.name, python, args.where))
                with log_path.open("wb") as log:
                    try:
                        if args.where == "docker":
                            status, detail = run_job_docker(docker, image, REPO, commit, job, python, cpus, log, low)
                        else:
                            status, detail = run_job_host(REPO, job, python, home, log, low, os.environ)
                    except (SetupError, KeyboardInterrupt, OtherCiBusy):
                        raise
                    except Exception as exc:  # a bug here: record it, show it, go on with the next job
                        _log_line(log, "local-ci: internal error, please report it:\n" + traceback.format_exc())
                        status, detail = "error", "internal error: %r (traceback in the log)" % (exc,)
                    finally:
                        result.seconds = time.monotonic() - started
                result.status, result.detail = status, detail
                running[:] = []
                if status != "passed" and args.fail_fast:
                    for job2, python2 in selected[index + 1:]:
                        results.append(Result(job2.name, python2, args.where, "not run", detail="--fail-fast"))
                    break
        except OtherCiBusy as busy:
            _mark_not_run(results, selected, args.where, "the configured other CI is running")
            summary(results)
            say("local-ci: stopped: %s is running a '%s' run (%s). Run again later, or add --wait."
                % (guard.config.repo if guard.config else "?", guard.config.workflow if guard.config else "?",
                   _run_urls(busy.runs)))
            return EXIT_FAILED if _any_failed(results) else EXIT_BUSY
        except KeyboardInterrupt:
            for result in running:
                result.status = "interrupted"
            _mark_not_run(results, selected, args.where, "interrupted")
            summary(results)
            say("local-ci: interrupted")
            return EXIT_INTERRUPTED
        except SetupError as exc:
            for result in running:
                result.detail = "could not run: %s" % exc
            _mark_not_run(results, selected, args.where, "the run stopped")
            summary(results)
            say("local-ci: %s" % exc)
            return EXIT_FAILED if _any_failed(results) else EXIT_SETUP
    summary(results)
    not_passed = [r for r in results if r.status != "passed"]
    if args.where == "docker" and changed:
        say("Note: %d uncommitted change(s) were not tested (--where docker tests commit %s)." % (changed, commit[:12]))
    say("local-ci: %s" % ("all %d passed" % len(results) if not not_passed else "%d of %d did not pass"
                         % (len(not_passed), len(results))))
    return EXIT_OK if not not_passed else EXIT_FAILED


def _any_failed(results: List[Result]) -> bool:
    """Some job ran and did not pass. The job a setup error stopped, and interrupted or not-run ones, do not count."""
    return any(r.status in ("failed", "timed out", "error") and not r.detail.startswith("could not run")
               for r in results)


def _mark_not_run(results: List[Result], selected: List[Tuple[Job, str]], where: str, why: str) -> None:
    done = {(r.job, r.python) for r in results}
    for job, python in selected:
        if (job.name, python) not in done:
            results.append(Result(job.name, python, where, "not run", detail=why))


if __name__ == "__main__":
    sys.exit(main())
