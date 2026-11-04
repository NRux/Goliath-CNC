# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/local_ci.py: its copy of the CI jobs, its safety rules and its plumbing.

The first test is the important one: the script runs a copy of
.github/workflows/ci.yml, and this fails as soon as the two differ, naming the
difference. ci.yml is read with a small reader for the shapes it uses (no
YAML library: this suite runs on Python 3.9 with only pytest installed); any
key, value shape or action it does not know fails the reader by name instead
of being skipped.

Python 3.9 compatible, standard library and pytest only.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

import local_ci

REPO = Path(__file__).resolve().parents[1]
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
SCRIPT = REPO / "tools" / "local_ci.py"
DOC = REPO / "tools" / "local-ci.md"
DOCKERFILE = REPO / "tools" / "local-ci" / "Dockerfile"


# --------------------------------------------------------------------------
# A reader for the subset of YAML that ci.yml uses
# --------------------------------------------------------------------------


class CiJob:
    def __init__(self, name: str) -> None:
        self.name = name
        self.pythons: List[str] = []
        self.timeout: Optional[int] = None
        self.env: Dict[str, str] = {}
        self.steps: List[Tuple[str, Tuple[str, ...], str]] = []  # run steps: (name, commands, working-directory)
        self.actions: List[Tuple[str, Dict[str, str]]] = []  # uses steps: (action, with)
        self.os: List[str] = []  # matrix os values, include os values, or a literal runs-on
        self.matrix: List[Tuple[str, str]] = []  # (runner, Python) pairs CI runs

    def pairs(self) -> List[Tuple[str, str]]:
        """include entries, else the os x python cross product, else runs-on x the one version."""
        if self.matrix:
            return sorted(set(self.matrix))
        return sorted({(runner, python) for runner in self.os for python in self.pythons})


# Everything the reader understands. Any other key, value shape or action
# could change what CI runs in a way tools/local_ci.py cannot copy, so it
# fails the reader by name instead of being skipped.
TOP_KEYS = {"name", "on", "permissions", "concurrency", "jobs"}
JOB_KEYS = {"name", "runs-on", "timeout-minutes", "strategy", "defaults", "env", "steps"}
JOB_INLINE_KEYS = {"name", "runs-on", "timeout-minutes"}
STRATEGY_KEYS = {"fail-fast", "matrix"}
MATRIX_KEYS = {"include", "os", "python"}
STEP_KEYS = {"uses", "with", "name", "run", "working-directory"}
ACTIONS = {
    "actions/checkout@v4": {"persist-credentials", "fetch-depth"},
    "actions/setup-python@v5": {"python-version"},
}


class UnsupportedCiYaml(AssertionError):
    pass


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _scalar(value: str) -> str:
    """A YAML scalar as written on one line: quotes removed, or a trailing ' # comment' dropped."""
    value = value.strip()
    if value[:1] in "\"'":
        return _unquote(value)
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def _flow_list(value: str) -> List[str]:
    match = re.fullmatch(r"\[(.*)\]", value.strip())
    if not match:
        raise ValueError("not a [flow, list]")
    return [_scalar(v) for v in match.group(1).split(",") if v.strip()]


def _version(value: str) -> str:
    """A Python version, which must be quoted: YAML reads an unquoted 3.10 as the number 3.1."""
    value = value.strip()
    if value[:1] not in "\"'":
        raise ValueError("unquoted Python version %s (YAML reads 3.10 as 3.1); quote it" % value)
    return _unquote(value)


def _flow_mapping(value: str) -> Dict[str, str]:
    match = re.fullmatch(r"-\s*\{(.*)\}", value.strip())
    if not match:
        raise ValueError("not a - { flow: mapping }")
    mapping = {}
    for part in match.group(1).split(","):
        key, sep, val = part.partition(":")
        if not sep:
            raise ValueError("%r has no ':'" % part)
        mapping[key.strip()] = _version(val) if key.strip() == "python" else _scalar(val)
    return mapping


def read_ci_jobs(text: str) -> Dict[str, CiJob]:
    """The jobs of a workflow file in ci.yml's style: names, Python versions, limits, env and steps."""
    lines = text.splitlines()
    i = 0

    def fail(why: str) -> UnsupportedCiYaml:
        where = lines[i].strip() if i < len(lines) else "end of file"
        return UnsupportedCiYaml("ci.yml line %d (%r): %s. Copy the change into tools/local_ci.py and teach "
                                 "tests/test_local_ci.py's reader the new shape." % (i + 1, where, why))

    for n, line in enumerate(lines):
        if line.strip() and not line.startswith((" ", "#")):
            key = line.split(":", 1)[0].strip()
            i = n
            if key not in TOP_KEYS:
                raise fail("top-level key %r is not understood (a workflow-level env or defaults applies to "
                           "every job)" % key)
    try:
        start = lines.index("jobs:")
    except ValueError:
        i = 0
        raise fail("no top-level 'jobs:' line") from None
    jobs: Dict[str, CiJob] = {}
    current: Optional[CiJob] = None
    section = ""
    step: Optional[Dict[str, object]] = None
    step_key = ""

    def finish_step() -> None:
        if current is None or step is None:
            return
        if ("run" in step) == ("uses" in step):
            raise fail("a step needs exactly one of 'run' and 'uses'")
        if "run" in step:
            if "with" in step:
                raise fail("'with' on a run step")
            done = {action for action, _ in current.actions}
            if not {"actions/checkout@v4", "actions/setup-python@v5"} <= done:
                raise fail("a run step before actions/checkout and actions/setup-python")
            for command in step["run"]:  # type: ignore[union-attr]
                syntax = local_ci.shell_syntax(command)
                if syntax is not None:
                    raise fail("shell syntax %r in a run command is not copied (the script runs no shell)" % syntax)
            current.steps.append((str(step.get("name", "")), tuple(step["run"]),  # type: ignore[arg-type]
                                  str(step.get("working-directory", "."))))
        else:
            action = str(step["uses"])
            given = dict(step.get("with", {}))  # type: ignore[call-overload]
            unknown = set(given) - ACTIONS[action]
            if unknown:
                raise fail("input(s) %s of %s are not understood" % (sorted(unknown), action))
            current.actions.append((action, given))

    i = start + 1
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        indent = _indent(line)
        if indent == 0:
            break
        key, colon, value = stripped.partition(":")
        key, value = key.strip(), value.strip()
        if "${{" in stripped and not ((section == "steps" and indent == 10 and key == "python-version")
                                      or (indent == 4 and key in ("name", "runs-on"))):
            raise fail("a ${{ }} expression here is not copied (only setup-python's python-version may use one)")
        if indent == 2:
            if not colon or value:
                raise fail("a job id line must be 'id:'")
            finish_step()
            step = None
            current = jobs.setdefault(key, CiJob(key))
            section = ""
            i += 1
            continue
        if current is None:
            raise fail("outside a job")
        if indent == 4:
            if key not in JOB_KEYS:
                raise fail("job key %r is not understood" % key)
            if value and key not in JOB_INLINE_KEYS:
                raise fail("an inline value for %r is not understood" % key)
            if key in JOB_INLINE_KEYS and not value:
                raise fail("%r needs a value on its line" % key)
            section = key
            if key == "timeout-minutes":
                current.timeout = int(_scalar(value))
            if key == "runs-on" and "${{" not in value:
                current.os.append(_scalar(value))
            i += 1
            continue
        if section == "env":
            if indent != 6 or not colon or not value:
                raise fail("env must be 'NAME: value' lines")
            current.env[key] = _scalar(value)
        elif section == "defaults":
            if not ((indent == 6 and stripped == "run:") or (indent == 8 and stripped == "shell: bash")):
                raise fail("only 'defaults: run: shell: bash' is understood")
        elif section == "strategy":
            try:
                if indent == 6:
                    if key not in STRATEGY_KEYS or (key == "matrix") == bool(value):
                        raise ValueError("strategy key %r is not understood" % key)
                elif indent == 8:
                    if key not in MATRIX_KEYS:
                        raise ValueError("matrix key %r is not understood" % key)
                    if key == "include":
                        if value:
                            raise ValueError("include must be a list of '- { ... }' lines")
                    elif key == "python":
                        match = re.fullmatch(r"\[(.*)\]", value)
                        if not match:
                            raise ValueError("not a [flow, list]")
                        current.pythons.extend(_version(v) for v in match.group(1).split(",") if v.strip())
                    else:
                        current.os.extend(_flow_list(value))
                elif indent == 10:
                    entry = _flow_mapping(stripped)
                    if "python" not in entry:
                        raise ValueError("an include entry without a python version")
                    current.pythons.append(entry["python"])
                    if "os" not in entry:
                        raise ValueError("an include entry without an os")
                    current.os.append(entry["os"])
                    current.matrix.append((entry["os"], entry["python"]))
                else:
                    raise ValueError("unexpected nesting under strategy")
            except ValueError as exc:
                raise fail(str(exc)) from None
        elif section == "steps":
            if indent == 6 and stripped.startswith("- "):
                finish_step()
                step = {}
                stripped = stripped[2:]
                indent = 8
                key, colon, value = stripped.partition(":")
                key, value = key.strip(), value.strip()
            if step is None:
                raise fail("step content before a step")
            if indent == 10 and step_key == "with":
                if not colon or not value:
                    raise fail("with must be 'input: value' lines")
                if key == "python-version" and not value.startswith("${{"):
                    try:
                        value = '"%s"' % _version(value)
                    except ValueError as exc:
                        raise fail(str(exc)) from None
                step.setdefault("with", {})[key] = _scalar(value)  # type: ignore[index]
                if key == "python-version" and not current.pythons:
                    current.pythons.append(_scalar(value))
                i += 1
                continue
            if indent != 8:
                raise fail("unexpected nesting in a step (only 'with:' inputs may be nested)")
            if key not in STEP_KEYS:
                raise fail("step key %r is not understood" % key)
            step_key = key
            if key == "uses":
                if _scalar(value) not in ACTIONS:
                    raise fail("action %r is not understood" % value)
                step["uses"] = _scalar(value)
            elif key == "with":
                if value:
                    raise fail("an inline value for 'with' is not understood")
                step["with"] = {}
            elif key == "run":
                if value == "|":
                    block: List[str] = []
                    j = i + 1
                    while j < len(lines) and (not lines[j].strip() or _indent(lines[j]) > indent):
                        text_line = lines[j].strip()
                        if text_line.startswith("#"):
                            i = j
                            raise fail("comment lines inside a run block are not copied")
                        if text_line.endswith("\\"):
                            i = j
                            raise fail("'\\' line continuations are not copied")
                        if re.search(r"\s#", text_line):
                            i = j
                            raise fail("a '#' comment after a command is not copied (the script runs no shell)")
                        if text_line:
                            block.append(text_line)
                        j += 1
                    step["run"] = block
                    i = j
                    continue
                if not value or value[:1] in ">|":
                    raise fail("only 'run: |' blocks and one-line runs are understood")
                if value[:1] in "\"'" and ("\\" in value or "''" in value):
                    raise fail("escapes in a quoted run are not copied")
                if value[:1] not in "\"'" and (": " in value or value[:1] in "-?:,[]{}#&*!%@`"):
                    raise fail("this unquoted run is not a plain YAML scalar GitHub can read; quote it")
                step["run"] = [_scalar(value)]
            else:
                step[key] = _scalar(value)
        else:
            raise fail("unexpected line")
        i += 1
    finish_step()
    return jobs


SAMPLE = """\
name: CI
on:
  pull_request:
jobs:
  one:
    runs-on: x
    timeout-minutes: 7
    strategy:
      fail-fast: false
      matrix:
        include:
          - { os: a, python: "3.9" }
          - { python: "3.13", os: b }
    env:
      A: "1"
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - uses: actions/setup-python@v5
        with:
          python-version: "3.9"
      - name: Install
        run: |
          python -m pip install a
          python -m pip install "b[c]"
      - name: Sub
        working-directory: sub
        run: python -m pytest -q  # a comment
  two:
    runs-on: y
    timeout-minutes: 3
    strategy:
      matrix:
        python: ["3.11", "3.14"]
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python }}
      - name: Only
        run: "python x.py"
"""


def test_ci_reader_reads_the_shapes_ci_yml_uses() -> None:
    jobs = read_ci_jobs(SAMPLE)
    assert sorted(jobs) == ["one", "two"]
    one, two = jobs["one"], jobs["two"]
    assert (one.pythons, one.timeout, one.env) == (["3.9", "3.13"], 7, {"A": "1"})
    assert one.steps == [
        ("Install", ("python -m pip install a", 'python -m pip install "b[c]"'), "."),
        ("Sub", ("python -m pytest -q",), "sub"),
    ]
    assert one.actions == [("actions/checkout@v4", {"persist-credentials": "false"}),
                           ("actions/setup-python@v5", {"python-version": "3.9"})]
    assert (one.os, two.os) == (["x", "a", "b"], ["y"])
    assert (one.pairs(), two.pairs()) == ([("a", "3.9"), ("b", "3.13")], [("y", "3.11"), ("y", "3.14")])
    assert (two.pythons, two.timeout, two.steps) == (["3.11", "3.14"], 3, [("Only", ("python x.py",), ".")])


@pytest.mark.parametrize("old, new, why", [
    ("      - name: Only\n", "      - name: Only\n        shell: pwsh\n", "step key 'shell'"),
    ("      - name: Only\n", "      - name: Only\n        env:\n          A: b\n", "step key 'env'"),
    ("      - name: Only\n", "      - name: Only\n        if: always()\n", "step key 'if'"),
    ("    timeout-minutes: 3\n", "    timeout-minutes: 3\n    needs: one\n", "job key 'needs'"),
    ("    timeout-minutes: 3\n", "    timeout-minutes: 3\n    services:\n      db:\n        image: x\n",
     "job key 'services'"),
    ("    timeout-minutes: 3\n", "    timeout-minutes: 3\n    env: { A: b }\n", "inline value for 'env'"),
    ('        python: ["3.11", "3.14"]\n', '        python: ["3.11", "3.14"]\n        exclude:\n          - x: 1\n',
     "matrix key 'exclude'"),
    ('          - { os: a, python: "3.9" }\n', '          - { os: a }\n', "without a python version"),
    ('          - { os: a, python: "3.9" }\n', '          - os: a\n            python: "3.9"\n', "flow: mapping"),
    ("        run: \"python x.py\"\n", "        run: >\n          python x.py\n", "only 'run: |'"),
    ("        run: \"python x.py\"\n", "        run: python x.py\n          --more\n", "unexpected nesting"),
    ("          python -m pip install a\n", "          # set up\n          python -m pip install a\n",
     "comment lines"),
    ("          python -m pip install a\n", "          python -m pip install \\\n            a\n",
     "continuations"),
    ("    timeout-minutes: 3\n", "    timeout-minutes: 3\n    defaults:\n      run:\n        working-directory: x\n",
     "only 'defaults: run: shell: bash'"),
    ("      - uses: actions/setup-python@v5\n", "      - uses: actions/cache@v4\n", "action 'actions/cache@v4'"),
    ("          persist-credentials: false\n", "          persist-credentials: false\n          submodules: true\n",
     "submodules"),
    ("on:\n", "env:\n  A: b\non:\n", "top-level key 'env'"),
    ('        python: ["3.11", "3.14"]\n', '        python: [3.11, "3.14"]\n', "unquoted Python version"),
    ('          - { os: a, python: "3.9" }\n', '          - { os: a, python: 3.10 }\n', "unquoted Python version"),
    ('      A: "1"\n', '      A: ${{ github.sha }}\n', "expression here is not copied"),
    ("          python -m pip install a\n", "          python -m pip install a  # why\n", "'#' comment after"),
    ('        run: "python x.py"\n', '        run: "python \\"x.py\\""\n', "escapes in a quoted run"),
    ("          python -m pip install a\n", "          python -m pip install a && echo ok\n", "shell syntax '&'"),
    ("          python -m pip install a\n", "          echo ok >> $GITHUB_ENV\n", "shell syntax"),
    ("          python -m pip install a\n", "          echo ${{ github.sha }}\n", "shell syntax '$'"),
    ("          python-version: ${{ matrix.python }}\n", "          python-version: 3.10\n", "unquoted Python version"),
    ('        run: "python x.py"\n', "        run: python -c print: 1\n", "not a plain YAML scalar"),
    ("      - uses: actions/checkout@v4\n",
     "      - name: Early\n        run: python -V\n      - uses: actions/checkout@v4\n",
     "before actions/checkout and actions/setup-python"),
    ("on:\n", "defaults:\n  run:\n    shell: bash\non:\n", "top-level key 'defaults'"),
])
def test_ci_reader_refuses_shapes_it_cannot_copy(old: str, new: str, why: str) -> None:
    assert old in SAMPLE, old
    with pytest.raises(UnsupportedCiYaml, match=re.escape(why)):
        read_ci_jobs(SAMPLE.replace(old, new, 1))  # the first occurrence, in job 'one'
    read_ci_jobs(SAMPLE)  # control: the unedited sample reads


def test_local_jobs_are_an_exact_copy_of_ci_yml() -> None:
    ci = read_ci_jobs(CI_YML.read_text(encoding="utf-8"))
    local = {job.name: job for job in local_ci.JOBS}
    assert set(ci) == set(local) | set(local_ci.NOT_MIRRORED), (
        "ci.yml jobs %s, tools/local_ci.py copies %s and leaves out %s: copy the new job into JOBS, or add it "
        "to NOT_MIRRORED with the reason" % (sorted(ci), sorted(local), sorted(local_ci.NOT_MIRRORED)))
    for name, job in local.items():
        theirs = ci[name]
        # A matrix lists a version once per operating system; the local copy runs each version once.
        assert sorted(job.pythons) == sorted(set(theirs.pythons)), (name, job.pythons, theirs.pythons)
        assert len(set(job.pythons)) == len(job.pythons), (name, job.pythons)
        assert job.timeout_minutes == theirs.timeout, (name, job.timeout_minutes, theirs.timeout)
        assert dict(job.env) == theirs.env, (name, job.env, theirs.env)
        ours = [(s.name, s.commands, s.cwd) for s in job.steps]
        assert ours == theirs.steps, "job %s: tools/local_ci.py has\n%s\nci.yml has\n%s" % (
            name, json.dumps(ours, indent=1), json.dumps(theirs.steps, indent=1))
        assert sorted(set(job.ci_matrix)) == theirs.pairs(), (name, job.ci_matrix, theirs.pairs())
        actions = dict(theirs.actions)
        assert actions.get("actions/checkout@v4") == dict(job.checkout), (name, theirs.actions, job.checkout)
        # setup-python must install the version the matrix (or the job's one version) names.
        wanted = "${{ matrix.python }}" if len(set(theirs.pythons)) > 1 else theirs.pythons[0]
        assert actions.get("actions/setup-python@v5") == {"python-version": wanted}, (name, theirs.actions)


def test_every_ci_job_has_python_versions_and_steps() -> None:
    # Guards the reader: a ci.yml shape it does not understand must not read as "no steps".
    for name, job in read_ci_jobs(CI_YML.read_text(encoding="utf-8")).items():
        assert job.pythons, "could not read the Python versions of ci.yml job %s" % name
        assert job.steps, "could not read the run steps of ci.yml job %s" % name
        assert job.timeout, "ci.yml job %s has no timeout-minutes" % name


def test_the_ci_self_test_runs_the_script() -> None:
    ci = read_ci_jobs(CI_YML.read_text(encoding="utf-8"))
    commands = [c for _, cmds, _ in ci["local-ci"].steps for c in cmds]
    assert any(c.startswith("python tools/local_ci.py") and "--job" in c for c in commands), commands


# --------------------------------------------------------------------------
# Safety rules for a machine that also runs another project's CI
# --------------------------------------------------------------------------


def _shipped_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in (SCRIPT, DOC, DOCKERFILE, CI_YML))


def test_never_prunes() -> None:
    # Pruning images, volumes or the system would also wipe other projects' caches on a shared engine.
    for line in _shipped_text().splitlines():
        if "prune" in line.lower():
            assert re.search(r"\b(never|not|no)\b", line, re.IGNORECASE), (
                "local CI may mention pruning only to forbid it: %r" % line)


def test_repository_never_names_this_machines_private_projects() -> None:
    words, notes = local_ci.private_words(os.environ, local_ci.default_config_file())
    for note in notes:
        print("note:", note)
    if not words:
        if local_ci.IN_CONTAINER_MARKER.exists():
            pytest.skip("inside --where docker's container no settings are visible; the outer run checks the "
                        "commit for this machine's private names before the jobs")
        pytest.skip("no private names configured on this machine (the busy check's repository, or "
                    "GOLIATH_PRIVATE_WORDS), so there is nothing to look for")
    hits = local_ci.private_name_hits(REPO, words)
    revisions = local_ci.unpublished_range(REPO)
    if revisions:
        hits += local_ci.private_history_hits(REPO, words, revisions)
    assert hits == [], ("a private name from this machine's settings is in the repository: %s. Remove it from history "
                        "(amend or rebase before pushing); a new commit leaves it in the old one" % hits)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", "-c", "user.name=t",
                    "-c", "user.email=t@example.invalid", *args], check=True, capture_output=True)


def _git_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    return path


def test_private_name_scan_reads_contents_paths_and_links_in_the_tree_and_in_a_commit(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "r")
    (repo / "plain.txt").write_text("hello Hiddenword here\n", encoding="utf-8")
    (repo / "ps51.txt").write_text("Hiddenword\n", encoding="utf-16")  # Windows PowerShell 5.1's default
    (repo / "notes-hiddenword.md").write_text("nothing\n", encoding="utf-8")  # the word in a path
    (repo / "hidden.txt").write_text("Hiddenword\n", encoding="utf-8")
    (repo / ".gitattributes").write_text("hidden.txt export-ignore\n", encoding="utf-8")  # git archive skips it
    (repo / "clean.txt").write_text("nothing\n", encoding="utf-8")
    (repo / "old_software").mkdir()
    (repo / "old_software" / "vendor.bin").write_bytes(b"hiddenword")
    links = os.name != "nt"
    if links:
        os.symlink("../hiddenword-target", str(repo / "link"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "x")
    words = ["hiddenword"]
    expected = ["hidden.txt (word 1)", "notes-hiddenword.md (word 1, in the path)", "plain.txt (word 1)",
                "ps51.txt (word 1)"] + (["link (word 1)"] if links else [])
    assert sorted(local_ci.private_name_hits(repo, words)) == sorted(expected)
    assert sorted(local_ci.private_name_hits(repo, words, "HEAD")) == sorted(expected)
    assert local_ci.private_name_hits(repo, ["absentword"]) == []


def test_private_name_scan_reads_unpublished_commit_messages(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "r")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "branch", "published")
    (repo / "a.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "mention Hiddenword in the message only")
    assert local_ci.private_name_hits(repo, ["hiddenword"]) == []  # the tree is clean
    hits = local_ci.private_history_hits(repo, ["hiddenword"], "published..HEAD")
    assert len(hits) == 1 and hits[0].startswith("commit ")


def test_private_words_come_from_settings_and_refuse_short_ones(tmp_path: Path) -> None:
    config = tmp_path / "busy-check.ini"
    config.write_text("[busy-check]\nrepo = owner/Elsewhere\nworkflow = Tests\n", encoding="utf-8")
    assert local_ci.private_words({"GOLIATH_PRIVATE_WORDS": "Alpha, beta2"}, config) == (
        ["alpha", "beta2", "elsewhere"], [])
    assert local_ci.private_words({}, tmp_path / "none.ini") == ([], [])
    with pytest.raises(local_ci.SetupError, match="shorter than 4"):
        local_ci.private_words({"GOLIATH_PRIVATE_WORDS": "abc"}, tmp_path / "none.ini")
    # A short repository name only leaves itself out, with a note; the busy check keeps working.
    config.write_text("[busy-check]\nrepo = owner/abc\nworkflow = Tests\n", encoding="utf-8")
    words, notes = local_ci.private_words({}, config)
    assert words == [] and "shorter than 4" in notes[0]


def test_an_explicit_settings_path_that_does_not_exist_is_an_error(tmp_path: Path) -> None:
    missing = tmp_path / "typo.ini"
    with pytest.raises(local_ci.SetupError, match="does not exist"):
        local_ci.load_busy_config({"GOLIATH_LOCAL_CI_CONFIG": str(missing)}, missing)
    assert local_ci.load_busy_config({}, missing) is None  # the default location: nothing configured


def test_docker_names_carry_this_projects_prefix() -> None:
    assert local_ci.DOCKER_PREFIX == "goliath-local-ci"
    assert local_ci.DOCKER_VOLUME.startswith(local_ci.DOCKER_PREFIX)
    assert local_ci.image_tag().startswith(local_ci.DOCKER_PREFIX + ":")


def test_docker_run_is_throwaway_limited_and_has_no_engine_access(tmp_path: Path) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text("x", encoding="utf-8")
    argv = local_ci.docker_run_argv("docker", REPO, "goliath-local-ci:abc", "goliath-local-ci-apps-py311-1",
                                    "2", "deadbeef", "apps", "3.11", ca, 256)
    joined = " ".join(argv)
    assert argv[:3] == ["docker", "run", "--rm"]
    assert argv[argv.index("--cpus") + 1] == "2"
    assert argv[argv.index("--cpu-shares") + 1] == "256"
    assert argv[argv.index("--network") + 1] == "goliath-local-ci-net"  # its own, not the shared default bridge
    assert argv[argv.index("--cap-drop") + 1] == "NET_RAW"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert "docker.sock" not in joined and "--privileged" not in joined
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
    assert mounts[0] == "type=bind,source=%s,target=/src,readonly" % REPO.resolve()
    assert mounts[1] == "type=volume,source=goliath-local-ci-cache,target=/cache"
    assert mounts[2].endswith("target=%s,readonly" % local_ci.CA_IN_CONTAINER)
    assert all("readonly" in m for m in mounts if m.startswith("type=bind"))
    assert argv[-8:] == ["goliath-local-ci:abc", "sh", "-c", local_ci.DOCKER_BOOTSTRAP, "sh", "deadbeef", "apps",
                         "3.11"]
    assert "--cpu-shares" not in local_ci.docker_run_argv("docker", REPO, "i", "c", "2", "s", "spdx", "3.13")
    assert argv[argv.index("CI=true") - 1] == "-e"  # as GitHub sets it


def test_docker_runs_the_commits_own_script_from_the_clone() -> None:
    # The jobs must come from the commit under test, not from the working tree's copy of this script.
    boot = local_ci.DOCKER_BOOTSTRAP
    clone, checkout, run = boot.index("git clone"), boot.index("git checkout"), boot.index("exec python3")
    assert clone < checkout < run
    assert "exec python3 tools/local_ci.py --inside-docker" in boot and "/src/tools" not in boot
    # upload-pack, which a local clone runs, ignores `git -c` options; the global config reaches it.
    assert "git config --global --add safe.directory" in boot and boot.index("safe.directory") < clone


def test_docker_mount_refuses_a_path_with_a_comma(tmp_path: Path) -> None:
    odd = tmp_path / "a,b"
    odd.mkdir()
    with pytest.raises(local_ci.SetupError, match="comma"):
        local_ci.docker_run_argv("docker", odd, "img", "c", "2", "sha", "spdx", "3.13")


def test_ca_bundle_must_be_a_file(tmp_path: Path) -> None:
    assert local_ci.ca_bundle({}) is None
    with pytest.raises(local_ci.SetupError, match="not a file"):
        local_ci.ca_bundle({"GOLIATH_LOCAL_CI_CA_BUNDLE": str(tmp_path / "missing.crt")})


def test_dockerfile_pins_its_images_by_digest() -> None:
    froms = [line for line in DOCKERFILE.read_text(encoding="utf-8").splitlines() if line.startswith("FROM ")]
    assert froms and all(re.search(r"@sha256:[0-9a-f]{64}\b", line) for line in froms), froms


def test_image_tag_ignores_line_endings(tmp_path: Path) -> None:
    lf, crlf = tmp_path / "lf", tmp_path / "crlf"
    lf.write_bytes(b"FROM x\nRUN y\n")
    crlf.write_bytes(b"FROM x\r\nRUN y\r\n")
    assert local_ci.image_tag(lf) == local_ci.image_tag(crlf)


# --------------------------------------------------------------------------
# The check for another project's CI
# --------------------------------------------------------------------------


class FakeGh:
    def __init__(self, answers: List[object]) -> None:
        self.answers = list(answers)
        self.calls: List[List[str]] = []

    def __call__(self, cmd: List[str], **kwargs: object) -> "subprocess.CompletedProcess[str]":
        self.calls.append(cmd)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, subprocess.CompletedProcess):
            return answer
        return subprocess.CompletedProcess(cmd, 0, json.dumps(answer), "")


CONFIG = local_ci.BusyConfig("owner/other", "Tests", "test")
TESTS_RUN = {"databaseId": 7, "workflowName": "Tests", "url": "https://example.invalid/runs/7"}
LINT_RUN = {"databaseId": 8, "workflowName": "Lint", "url": "https://example.invalid/runs/8"}


def test_busy_runs_lets_gh_resolve_the_workflow() -> None:
    # gh resolves --workflow by name (any case) or file name and fails on an unknown one,
    # so a misspelt name stops the run instead of matching nothing.
    gh = FakeGh([[TESTS_RUN]])
    assert local_ci.busy_runs(CONFIG, gh) == [TESTS_RUN]
    cmd = gh.calls[0]
    assert cmd[:5] == ["gh", "run", "list", "-R", "owner/other"]
    assert cmd[cmd.index("--workflow") + 1] == "Tests"
    assert cmd[cmd.index("--status") + 1] == "in_progress"


def test_busy_check_fails_closed_on_an_unknown_workflow() -> None:
    unknown = subprocess.CompletedProcess([], 1, "", "could not find any workflows named Tets")
    with pytest.raises(local_ci.BusyCheckError, match="could not find any workflows named Tets"):
        local_ci.busy_runs(local_ci.BusyConfig("owner/other", "Tets", "test"), FakeGh([unknown]))


def test_configure_verifies_the_workflow_before_saving(tmp_path: Path) -> None:
    file = tmp_path / "busy-check.ini"
    unknown = subprocess.CompletedProcess([], 1, "", "could not find any workflows named Tets")
    with pytest.raises(local_ci.SetupError, match="not saved"):
        local_ci.write_busy_config(file, "owner/other", "Tets", FakeGh([unknown]))
    assert not file.exists()
    gh = FakeGh([[]])
    local_ci.write_busy_config(file, "owner/other", "Tests", gh)
    assert "--status" not in gh.calls[0] and file.exists()


@pytest.mark.parametrize("answer, message", [
    (FileNotFoundError(), "not installed"),
    (subprocess.TimeoutExpired("gh", 60), "did not answer"),
    (subprocess.CompletedProcess([], 1, "", "HTTP 404: Not Found"), "HTTP 404"),
    (subprocess.CompletedProcess([], 0, "<html>", ""), "not JSON"),
    (subprocess.CompletedProcess([], 0, "{}", ""), "not a list"),
    (subprocess.CompletedProcess([], 0, "[1, 2]", ""), "not a list of runs"),
])
def test_busy_check_failures_stop_the_run_with_the_reason(answer: object, message: str) -> None:
    with pytest.raises(local_ci.BusyCheckError, match=message):
        local_ci.busy_runs(CONFIG, FakeGh([answer]))


def test_guard_stops_when_the_other_ci_runs() -> None:
    guard = local_ci.BusyGuard(CONFIG, "stop", run=FakeGh([[TESTS_RUN]]))
    with pytest.raises(local_ci.OtherCiBusy) as info:
        guard.check("x")
    assert info.value.runs == [TESTS_RUN]


def test_guard_waits_until_clear(capsys: pytest.CaptureFixture) -> None:
    sleeps: List[float] = []
    clock = iter([0.0, 60.0, 400.0, 460.0, 460.0])
    guard = local_ci.BusyGuard(CONFIG, "wait", run=FakeGh([[TESTS_RUN], [TESTS_RUN], [TESTS_RUN], []]),
                               sleep=sleeps.append, clock=lambda: next(clock))
    guard.check("the next job")
    out = capsys.readouterr().out
    assert sleeps == [local_ci.BUSY_POLL_SECONDS] * 3
    assert "waiting before the next job" in out and "still waiting" in out and "is clear" in out


def test_guard_ignore_and_unconfigured_never_call_gh() -> None:
    gh = FakeGh([])
    local_ci.BusyGuard(CONFIG, "ignore", run=gh).check("x")
    local_ci.BusyGuard(None, "stop", run=gh).check("x")
    assert gh.calls == []
    assert "SKIPPED" in local_ci.BusyGuard(CONFIG, "ignore").describe()
    assert "not configured" in local_ci.BusyGuard(None, "stop").describe()


def test_busy_config_from_environment_and_file(tmp_path: Path) -> None:
    file = tmp_path / "busy-check.ini"
    assert local_ci.load_busy_config({}, file) is None
    env = {"GOLIATH_CI_BUSY_REPO": "owner/other", "GOLIATH_CI_BUSY_WORKFLOW": "Tests"}
    assert local_ci.load_busy_config(env, file).repo == "owner/other"
    with pytest.raises(local_ci.SetupError, match="both"):
        local_ci.load_busy_config({"GOLIATH_CI_BUSY_REPO": "owner/other"}, file)
    with pytest.raises(local_ci.SetupError, match="OWNER/NAME"):
        local_ci.load_busy_config({"GOLIATH_CI_BUSY_REPO": "not a repo", "GOLIATH_CI_BUSY_WORKFLOW": "T"}, file)
    written = local_ci.write_busy_config(file, "owner/other", "Tests", FakeGh([[]]))
    assert local_ci.load_busy_config({}, file) == local_ci.BusyConfig("owner/other", "Tests", str(file))
    assert written.repo == "owner/other"
    file.write_text("[busy-check]\nrepo = owner/other\n", encoding="utf-8")
    with pytest.raises(local_ci.SetupError, match="missing workflow"):
        local_ci.load_busy_config({}, file)
    file.write_text("[other]\n", encoding="utf-8")
    with pytest.raises(local_ci.SetupError, match="no \\[busy-check\\]"):
        local_ci.load_busy_config({}, file)


# --------------------------------------------------------------------------
# Plumbing: selection, environment, venv removal, lock, running commands
# --------------------------------------------------------------------------


def test_plan_selects_jobs_and_versions() -> None:
    every = local_ci.plan([], [])
    assert [(j.name, p) for j, p in every] == [("test", "3.9"), ("test", "3.13"), ("apps", "3.11"),
                                               ("apps", "3.14"), ("spdx", "3.13")]
    assert [(j.name, p) for j, p in local_ci.plan([], ["3.13"])] == [("test", "3.13"), ("spdx", "3.13")]
    assert [(j.name, p) for j, p in local_ci.plan(["spdx", "spdx"], ["3.13", "3.13"])] == [("spdx", "3.13")]
    with pytest.raises(local_ci.SetupError, match="no job 'nope'"):
        local_ci.plan(["nope"], [])
    with pytest.raises(local_ci.SetupError, match="no selected job runs Python 3.11"):
        local_ci.plan(["spdx"], ["3.11"])


def test_environment_drops_what_changes_python_or_pytest_and_keeps_download_settings(tmp_path: Path) -> None:
    base = {"PATH": "/usr/bin", "PYTHONPATH": "/x", "PYTHONHOME": "/y", "PYTEST_ADDOPTS": "-x",
            "VIRTUAL_ENV": "/old", "PIP_INDEX_URL": "https://mirror.invalid", "UV_CACHE_DIR": "/c", "HOME": "/h",
            "GH_TOKEN": "t", "AWS_SECRET_ACCESS_KEY": "s", "DB_PASSWORD": "p", "UV_INDEX_X_PASSWORD": "i"}
    clean, dropped = local_ci.clean_environ(base)
    assert dropped == ["AWS_SECRET_ACCESS_KEY", "DB_PASSWORD", "GH_TOKEN", "PYTEST_ADDOPTS", "PYTHONHOME",
                       "PYTHONPATH", "VIRTUAL_ENV"]
    assert clean["UV_INDEX_X_PASSWORD"] == "i"  # a download setting, named in the banner
    assert clean["PIP_INDEX_URL"] == "https://mirror.invalid" and clean["UV_CACHE_DIR"] == "/c"
    assert local_ci.download_settings(base) == ["PIP_INDEX_URL", "UV_CACHE_DIR", "UV_INDEX_X_PASSWORD"]
    job = local_ci._job("apps")
    env = local_ci.job_environ(base, tmp_path / "venv", job)
    assert env["GOLIATH_REQUIRE_GIT"] == "1"
    assert env["CI"] == "true"  # GitHub sets it; Hypothesis then uses its CI settings
    assert env["VIRTUAL_ENV"] == str(tmp_path / "venv")
    assert env["PATH"].split(os.pathsep)[0].startswith(str(tmp_path / "venv"))
    assert "PYTHONPATH" not in env


def test_command_argv_uses_the_jobs_python() -> None:
    py = Path("/v/bin/python")
    assert local_ci.command_argv('python -m pip install -e ".[dev]" -e "./goliath-link[dev]"', py) == [
        str(py), "-m", "pip", "install", "-e", ".[dev]", "-e", "./goliath-link[dev]"]


def test_remove_venv_deletes_only_what_this_script_made(tmp_path: Path) -> None:
    root = tmp_path / "venvs"
    ours = root / "spdx-py3.13"
    ours.mkdir(parents=True)
    (ours / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    local_ci.remove_venv(ours, root)
    assert not ours.exists()
    foreign = root / "something"
    foreign.mkdir()
    with pytest.raises(local_ci.SetupError, match="no pyvenv.cfg"):
        local_ci.remove_venv(foreign, root)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "pyvenv.cfg").write_text("", encoding="utf-8")
    with pytest.raises(local_ci.SetupError, match="not directly inside"):
        local_ci.remove_venv(outside, root)
    assert foreign.exists() and outside.exists()


def test_home_inside_the_repository_is_refused() -> None:
    with pytest.raises(local_ci.SetupError, match="inside the repository"):
        local_ci.check_home(REPO / "venvs", REPO)
    local_ci.check_home(REPO.parent / "goliath-local-ci-elsewhere", REPO)


def test_only_one_run_at_a_time(tmp_path: Path) -> None:
    with local_ci.RunLock(tmp_path / "lock"):
        with pytest.raises(local_ci.SetupError, match="another local CI run"):
            with local_ci.RunLock(tmp_path / "lock"):
                pass
    with local_ci.RunLock(tmp_path / "lock"):
        pass  # released


def _python_cmd(code: str) -> List[str]:
    return [sys.executable, "-c", code]


def test_run_logged_sends_output_to_screen_and_log(capfd: pytest.CaptureFixture, tmp_path: Path) -> None:
    log = io.BytesIO()
    status = local_ci.run_logged(_python_cmd("import sys; sys.stdout.write('out-line\\n'); "
                                             "sys.stderr.write('err-line\\n'); sys.exit(3)"),
                                 tmp_path, os.environ, log, time.monotonic() + 60, False)
    assert status == ("failed", 3)
    assert b"out-line" in log.getvalue() and b"err-line" in log.getvalue()
    assert "out-line" in capfd.readouterr().out


def test_run_logged_enforces_the_deadline(tmp_path: Path) -> None:
    started = time.monotonic()
    status = local_ci.run_logged(_python_cmd("import time; time.sleep(60)"), tmp_path, os.environ, io.BytesIO(),
                                 time.monotonic() + 1.5, False)
    assert status == ("timeout", None)
    assert time.monotonic() - started < 20


@pytest.mark.skipif(os.name == "nt", reason="process-group check uses POSIX signals; Windows uses taskkill /T")
def test_timeout_kills_the_children_too(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    code = ("import subprocess, sys, time; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "open(%r, 'w').write(str(p.pid)); time.sleep(60)" % str(pid_file))
    assert local_ci.run_logged(_python_cmd(code), tmp_path, os.environ, io.BytesIO(),
                               time.monotonic() + 3, False)[0] == "timeout"
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _alive(child):
            return
        time.sleep(0.1)
    pytest.fail("the grandchild %d survived the timeout" % child)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path("/proc/%d/stat" % pid)
    if stat.exists():  # a killed process nobody has reaped yet (a zombie) counts as dead
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return False
    return True


@pytest.fixture
def no_user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the tests away from the busy-check settings of whoever runs them."""
    monkeypatch.delenv("GOLIATH_LOCAL_CI_CONFIG", raising=False)
    monkeypatch.setattr(local_ci, "default_config_file",
                        lambda environ=os.environ: tmp_path / "no-config" / "busy-check.ini")
    monkeypatch.delenv("GOLIATH_PRIVATE_WORDS", raising=False)
    monkeypatch.delenv("GOLIATH_CI_BUSY_REPO", raising=False)
    monkeypatch.delenv("GOLIATH_CI_BUSY_WORKFLOW", raising=False)
    monkeypatch.setenv("GOLIATH_LOCAL_CI_HOME", str(tmp_path / "home"))


@pytest.mark.usefixtures("no_user_config")
def test_main_list_and_dry_run_run_nothing(capsys: pytest.CaptureFixture) -> None:
    assert local_ci.main(["--list"]) == 0
    listed = capsys.readouterr().out
    assert "python tests/arch/spdx_check.py" in listed and "GOLIATH_REQUIRE_GIT=1" in listed
    assert local_ci.main(["--dry-run", "--job", "spdx", "--ignore-busy"]) == 0
    out = capsys.readouterr().out
    assert "dry run: nothing was run" in out and "SKIPPED (--ignore-busy)" in out


@pytest.mark.usefixtures("no_user_config")
def test_main_reports_bad_options_with_exit_2(capsys: pytest.CaptureFixture) -> None:
    assert local_ci.main(["--job", "nope"]) == local_ci.EXIT_SETUP
    assert "no job 'nope'" in capsys.readouterr().out
    assert local_ci.main(["--dry-run", "--cpus", "0"]) == local_ci.EXIT_SETUP


def test_configure_busy_check_writes_the_users_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                       capsys: pytest.CaptureFixture) -> None:
    config = tmp_path / "cfg" / "busy-check.ini"
    monkeypatch.setenv("GOLIATH_LOCAL_CI_CONFIG", str(config))
    monkeypatch.setattr(local_ci, "busy_runs", lambda *a, **k: [])
    assert local_ci.main(["--configure-busy-check", "owner/other", "Tests"]) == 0
    assert local_ci.load_busy_config({}, config) == local_ci.BusyConfig("owner/other", "Tests", str(config))
    assert str(config) in capsys.readouterr().out


def test_inside_docker_refuses_to_run_outside_a_container(capsys: pytest.CaptureFixture) -> None:
    if Path("/.dockerenv").exists():
        pytest.skip("this test run is itself inside a container")
    assert local_ci.main(["--inside-docker", "--commit", "x", "--job", "spdx", "--python", "3.13"]) == \
        local_ci.EXIT_SETUP
    assert "only for the container" in capsys.readouterr().out


def test_relative_paths_in_settings_become_absolute() -> None:
    assert local_ci.default_home({"GOLIATH_LOCAL_CI_HOME": "relative/dir"}).is_absolute()
    assert local_ci.default_config_file({"GOLIATH_LOCAL_CI_CONFIG": "x.ini"}).is_absolute()


def test_a_venv_that_cannot_be_deleted_is_a_clear_setup_error(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "venvs"
    venv = root / "spdx-py3.13"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("", encoding="utf-8")

    def locked(path: object) -> None:
        raise PermissionError(13, "in use", str(path))

    monkeypatch.setattr(local_ci.shutil, "rmtree", locked)
    monkeypatch.setattr(local_ci.time, "sleep", lambda seconds: None)
    with pytest.raises(local_ci.SetupError, match="cannot delete the previous virtual environment"):
        local_ci.remove_venv(venv, root)


def test_ctrl_c_before_any_job_exits_130(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    def interrupted(args: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(local_ci, "_main", interrupted)
    assert local_ci.main([]) == local_ci.EXIT_INTERRUPTED
    assert "interrupted" in capsys.readouterr().out


def test_log_folders_are_unique(tmp_path: Path) -> None:
    assert local_ci._new_log_dir(tmp_path) != local_ci._new_log_dir(tmp_path)


class FakeJobs:
    """Stands in for run_job_host: a scripted outcome per call, and a record of the busy checks."""

    def __init__(self, outcomes: List[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: List[Tuple[str, str]] = []

    def __call__(self, repo: Path, job: object, python: str, *rest: object) -> Tuple[str, str]:
        self.calls.append((job.name, python))  # type: ignore[attr-defined]
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch, no_user_config: None) -> pytest.MonkeyPatch:
    monkeypatch.setattr(local_ci, "find_tool", lambda name: name)
    return monkeypatch


def test_a_failure_before_a_setup_error_still_exits_1(fake_run: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture) -> None:
    fake_run.setattr(local_ci, "run_job_host", FakeJobs([("failed", "step x"), local_ci.SetupError("boom")]))
    code = local_ci.main(["--job", "test", "--python", "3.13", "--job", "spdx", "--priority", "normal"])
    out = capsys.readouterr().out
    assert code == local_ci.EXIT_FAILED, out
    assert "FAILED" in out and "could not run: boom" in out


def test_an_internal_error_is_reported_and_the_next_job_still_runs(fake_run: pytest.MonkeyPatch,
                                                                   capsys: pytest.CaptureFixture) -> None:
    jobs = FakeJobs([RuntimeError("bug"), ("passed", "")])
    fake_run.setattr(local_ci, "run_job_host", jobs)
    code = local_ci.main(["--job", "test", "--python", "3.13", "--job", "spdx", "--priority", "normal"])
    out = capsys.readouterr().out
    assert code == local_ci.EXIT_FAILED, out
    assert jobs.calls == [("test", "3.13"), ("spdx", "3.13")]
    assert "ERROR" in out and "internal error: RuntimeError('bug')" in out


def test_the_busy_check_runs_before_every_job_including_the_first(fake_run: pytest.MonkeyPatch) -> None:
    checks: List[str] = []
    fake_run.setenv("GOLIATH_CI_BUSY_REPO", "owner/other")
    fake_run.setenv("GOLIATH_CI_BUSY_WORKFLOW", "Tests")
    fake_run.setattr(local_ci, "busy_runs", lambda config, run, *a, **k: checks.append(config.repo) or [])
    fake_run.setattr(local_ci, "run_job_host", FakeJobs([("passed", ""), ("passed", "")]))
    assert local_ci.main(["--job", "test", "--python", "3.13", "--job", "spdx", "--priority", "normal"]) == 0
    assert checks == ["owner/other", "owner/other"]


def test_a_busy_other_ci_stops_before_the_first_job(fake_run: pytest.MonkeyPatch,
                                                    capsys: pytest.CaptureFixture) -> None:
    fake_run.setenv("GOLIATH_CI_BUSY_REPO", "owner/other")
    fake_run.setenv("GOLIATH_CI_BUSY_WORKFLOW", "Tests")
    fake_run.setattr(local_ci, "busy_runs", lambda *a, **k: [TESTS_RUN])
    jobs = FakeJobs([])
    fake_run.setattr(local_ci, "run_job_host", jobs)
    assert local_ci.main(["--job", "spdx", "--priority", "normal"]) == local_ci.EXIT_BUSY
    assert jobs.calls == [] and "NOT RUN" in capsys.readouterr().out


class Chunks:
    """A pipe that hands over the given chunks one read at a time."""

    def __init__(self, chunks: List[bytes]) -> None:
        self.chunks = list(chunks)

    def read1(self, size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""


class Screen(io.StringIO):
    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail

    def write(self, text: str) -> int:
        if self.fail:
            raise UnicodeEncodeError("cp1252", text, 0, 1, "cannot show")
        return super().write(text)


def test_output_reaches_the_screen_as_whole_characters_and_the_log_as_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    # A chunk can end inside a UTF-8 character; writing such raw bytes to a Windows console
    # makes Python 3.12.8+ flush forever. The screen must only ever get whole characters.
    screen = Screen()
    monkeypatch.setattr(local_ci.sys, "stdout", screen)
    log = io.BytesIO()
    local_ci._pump(Chunks([b"caf\xc3", b"\xa9 ok\n"]), log, "utf-8")
    assert screen.getvalue() == "caf\u00e9 ok\n"
    assert log.getvalue() == b"caf\xc3\xa9 ok\n"


def test_a_screen_that_cannot_show_the_output_never_stops_the_run(monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    monkeypatch.setattr(local_ci.sys, "stdout", Screen(fail=True))
    log = io.BytesIO()
    status = local_ci.run_logged([sys.executable, "-c", "print('x' * 10)"], tmp_path, os.environ, log,
                                 time.monotonic() + 60, False)
    assert status == ("ok", 0)
    assert b"xxxxxxxxxx" in log.getvalue()
    assert b"could not be shown on the screen" in log.getvalue()


def test_busy_check_settings_tolerate_percent_signs_and_a_bom_but_not_utf16(tmp_path: Path) -> None:
    file = tmp_path / "busy-check.ini"
    file.write_bytes(b"\xef\xbb\xbf[busy-check]\nrepo = owner/other\nworkflow = Tests 100%\n")
    assert local_ci.load_busy_config({}, file).workflow == "Tests 100%"
    file.write_text("[busy-check]\nrepo = owner/other\nworkflow = Tests\n", encoding="utf-16")
    with pytest.raises(local_ci.SetupError, match="not UTF-8"):
        local_ci.load_busy_config({}, file)


def test_busy_check_settings_must_stay_outside_the_repository() -> None:
    with pytest.raises(local_ci.SetupError, match="inside the repository"):
        local_ci.check_config_file(REPO / "busy-check.ini", REPO)
    local_ci.check_config_file(REPO.parent / "elsewhere" / "busy-check.ini", REPO)


def test_a_named_job_left_without_versions_is_an_error() -> None:
    with pytest.raises(local_ci.SetupError, match="--job spdx runs Python 3.13"):
        local_ci.plan(["spdx", "apps"], ["3.11"])
    assert [(j.name, p) for j, p in local_ci.plan([], ["3.11"])] == [("apps", "3.11")]


def test_commands_are_found_on_the_jobs_path(tmp_path: Path) -> None:
    tool = tmp_path / ("tool.exe" if os.name == "nt" else "tool")
    tool.write_text("", encoding="utf-8")
    tool.chmod(0o755)
    found = local_ci.resolve_command(["tool", "--version"], str(tmp_path))
    assert found is not None and Path(found[0]).resolve() == tool.resolve() and found[1:] == ["--version"]
    assert local_ci.resolve_command(["tool"], str(tmp_path / "empty")) is None
    absolute = [str(Path(sys.executable).resolve()), "-V"]
    assert local_ci.resolve_command(absolute, "") == absolute


@pytest.mark.parametrize("code", [125, 126, 127])
def test_docker_that_cannot_start_the_container_is_an_error_not_a_failed_test(
        monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    monkeypatch.setattr(local_ci, "run_logged", lambda *a, **k: ("failed", code))
    monkeypatch.setattr(local_ci, "ca_bundle", lambda: None)
    status, detail = local_ci.run_job_docker("docker", "img", REPO, "sha", local_ci._job("spdx"), "3.13", "2",
                                             io.BytesIO(), False)
    assert status == "error" and "could not start the container" in detail


def test_ctrl_c_while_waiting_for_the_next_job_leaves_the_finished_job_alone(
        fake_run: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    fake_run.setenv("GOLIATH_CI_BUSY_REPO", "owner/other")
    fake_run.setenv("GOLIATH_CI_BUSY_WORKFLOW", "Tests")
    answers: List[object] = [[], KeyboardInterrupt()]

    def busy(*a: object, **k: object) -> List[object]:
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer  # type: ignore[return-value]

    fake_run.setattr(local_ci, "busy_runs", busy)
    fake_run.setattr(local_ci, "run_job_host", FakeJobs([("failed", "step x")]))
    code = local_ci.main(["--job", "test", "--python", "3.13", "--job", "spdx", "--priority", "normal"])
    out = capsys.readouterr().out
    assert code == local_ci.EXIT_INTERRUPTED
    assert re.search(r"test\s+Python 3.13\s+FAILED", out) and re.search(r"spdx\s+Python 3.13\s+NOT RUN", out), out


def test_inside_docker_refuses_a_version_the_commits_job_does_not_run(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    marker = tmp_path / "dockerenv"
    marker.write_text("", encoding="utf-8")
    monkeypatch.setattr(local_ci, "IN_CONTAINER_MARKER", marker)
    assert local_ci.main(["--inside-docker", "--commit", "x", "--job", "spdx", "--python", "3.11"]) == \
        local_ci.EXIT_SETUP
    assert "job list differs from the commit's" in capsys.readouterr().out


def test_docker_mode_checks_the_commit_for_private_names(fake_run: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture) -> None:
    fake_run.setenv("GOLIATH_PRIVATE_WORDS", "hiddenword")
    fake_run.setattr(local_ci, "check_docker_checkout", lambda repo: None)
    fake_run.setattr(local_ci, "ensure_docker", lambda cpus: "docker")
    fake_run.setattr(local_ci, "ensure_image", lambda *a: "img")
    fake_run.setattr(local_ci, "run_job_docker", lambda *a: ("passed", ""))
    seen: List[object] = []
    fake_run.setattr(local_ci, "private_name_hits",
                     lambda repo, words, commit=None: seen.append((words, commit)) or ["notes.md (word 1)"])
    fake_run.setattr(local_ci, "unpublished_range", lambda repo: "origin/main..HEAD")
    fake_run.setattr(local_ci, "private_history_hits", lambda repo, words, revisions: ["commit abc (word 1)"])
    fake_run.setattr(local_ci, "ensure_network", lambda docker: None)
    code = local_ci.main(["--where", "docker", "--job", "spdx", "--priority", "normal"])
    out = capsys.readouterr().out
    assert code == local_ci.EXIT_FAILED, out
    assert seen and seen[0][0] == ["hiddenword"] and seen[0][1]  # the commit, not the working tree
    assert re.search(r"names\s+Python -\s+FAILED", out) and "notes.md (word 1)" in out
    assert "commit abc (word 1)" in out and "amend or rebase" in out


def test_a_half_deleted_venv_keeps_its_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "venvs"
    venv = root / "spdx-py3.13"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Lib").mkdir()
    (venv / "pyvenv.cfg").write_text("", encoding="utf-8")
    real = local_ci.shutil.rmtree

    def stops_at_lib(path: object) -> None:
        if Path(str(path)).name == "Lib":
            raise PermissionError(13, "in use", str(path))
        real(path)  # type: ignore[arg-type]

    monkeypatch.setattr(local_ci.shutil, "rmtree", stops_at_lib)
    monkeypatch.setattr(local_ci.time, "sleep", lambda seconds: None)
    with pytest.raises(local_ci.SetupError):
        local_ci.remove_venv(venv, root)
    assert (venv / "pyvenv.cfg").is_file()  # so the next run still knows the folder is its own
    monkeypatch.setattr(local_ci.shutil, "rmtree", real)
    local_ci.remove_venv(venv, root)
    assert not venv.exists()


@pytest.mark.parametrize("command", [
    "python -m pytest > out.txt", "python -c x | tee y", "python a.py && python b.py", "python a.py; python b.py",
    "echo $HOME", "echo ok >> $GITHUB_ENV", "python -m pytest tests/*.py", "python -c `x`", "cd ~", "(python a)",
    "cd goliath-link", "FOO=1 python -m pytest", "python -m pytest  # all", "echo hi", "export A",
])
def test_commands_needing_a_shell_are_refused(command: str) -> None:
    assert local_ci.shell_syntax(command) is not None
    with pytest.raises(local_ci.SetupError, match="shell syntax"):
        local_ci.command_argv(command, Path("/v/python"))


def test_every_ci_command_runs_without_a_shell() -> None:
    for job in local_ci.JOBS:
        for step in job.steps:
            for command in step.commands:
                assert local_ci.shell_syntax(command) is None, (job.name, command)


def test_jobs_run_without_the_users_git_settings(tmp_path: Path) -> None:
    env = local_ci.job_environ({"PATH": "/usr/bin"}, tmp_path / "venvs" / "x", local_ci._job("spdx"))
    config = Path(env["GIT_CONFIG_GLOBAL"])
    assert config.is_file() and "[" not in config.read_text(encoding="utf-8")


def test_upload_credentials_are_dropped_and_index_logins_kept() -> None:
    base = {"UV_PUBLISH_TOKEN": "t", "UV_PUBLISH_PASSWORD": "p", "DOCKER_AUTH_CONFIG": "{}", "HATCH_INDEX_AUTH": "a",
            "UV_INDEX_PRIVATE_PASSWORD": "i", "PIP_INDEX_URL": "https://u:p@x.invalid", "UV_CACHE_DIR": "/c"}
    clean, dropped = local_ci.clean_environ(base)
    assert dropped == ["DOCKER_AUTH_CONFIG", "HATCH_INDEX_AUTH", "UV_PUBLISH_PASSWORD", "UV_PUBLISH_TOKEN"]
    assert local_ci.download_settings(base) == ["PIP_INDEX_URL", "UV_CACHE_DIR", "UV_INDEX_PRIVATE_PASSWORD"]


class FakeDocker:
    """Records docker commands; answers 'image inspect' with a failure so a build would be needed."""

    def __init__(self) -> None:
        self.commands: List[List[str]] = []

    def __call__(self, cmd: List[str], **kwargs: object) -> "subprocess.CompletedProcess[str]":
        self.commands.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 1 if cmd[1:3] == ["image", "inspect"] else 0, "", "")


def test_the_busy_check_runs_before_an_image_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    docker = FakeDocker()
    monkeypatch.setattr(local_ci.subprocess, "run", docker)
    built: List[object] = []
    monkeypatch.setattr(local_ci, "run_logged", lambda *a, **k: built.append(a) or ("ok", 0))
    guard = local_ci.BusyGuard(CONFIG, "stop", run=FakeGh([[TESTS_RUN]]))
    with pytest.raises(local_ci.OtherCiBusy):
        local_ci.ensure_image("docker", guard, tmp_path, False)
    assert built == []  # nothing was built


@pytest.mark.parametrize("outcome", ["timeout", "interrupt"])
def test_a_container_is_killed_on_timeout_and_on_ctrl_c(monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    docker = FakeDocker()
    monkeypatch.setattr(local_ci.subprocess, "run", docker)
    monkeypatch.setattr(local_ci, "ca_bundle", lambda: None)

    def run_logged(argv: List[str], *a: object, on_interrupt: object = None, **k: object) -> Tuple[str, object]:
        if outcome == "interrupt":
            on_interrupt()  # type: ignore[operator]
            raise KeyboardInterrupt
        return "timeout", None

    monkeypatch.setattr(local_ci, "run_logged", run_logged)
    try:
        local_ci.run_job_docker("docker", "img", REPO, "sha", local_ci._job("spdx"), "3.13", "2", io.BytesIO(), False)
    except KeyboardInterrupt:
        assert outcome == "interrupt"
    kills = [c for c in docker.commands if c[1:2] == ["kill"]]
    assert kills and kills[0][2].startswith("goliath-local-ci-spdx-py313-")


def test_docker_mode_refuses_uncommitted_changes_to_the_job_list(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "r")
    (repo / "tools").mkdir()
    (repo / "tools" / "local_ci.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "x")
    local_ci.check_docker_checkout(repo)
    (repo / "tools" / "local_ci.py").write_text("x = 2\n", encoding="utf-8")
    with pytest.raises(local_ci.SetupError, match="commit or stash"):
        local_ci.check_docker_checkout(repo)


def test_jobs_ci_never_runs_on_this_kind_of_system_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_ci.os, "name", "nt")
    monkeypatch.setattr(local_ci.sys, "platform", "win32")
    assert local_ci.without_ci_counterpart(local_ci.plan([], []), "host") == ["spdx/3.13"]
    assert local_ci.without_ci_counterpart(local_ci.plan([], []), "docker") == []


def test_unpublished_history_is_read_as_stored_including_utf16_and_deleted_files(tmp_path: Path) -> None:
    # git log -p shows a UTF-16 file only as "Binary files differ"; a push uploads it all the same.
    repo = _git_repo(tmp_path / "r")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "branch", "published")
    (repo / "notes.txt").write_text("Hiddenword\n", encoding="utf-16")  # Windows PowerShell 5.1's default
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add notes")
    _git(repo, "rm", "-q", "notes.txt")
    _git(repo, "commit", "-qm", "remove notes")
    assert local_ci.private_name_hits(repo, ["hiddenword"]) == []  # the tree is clean now
    hits = local_ci.private_history_hits(repo, ["hiddenword"], "published..HEAD")
    assert len(hits) == 1 and hits[0].startswith("notes.txt "), hits
    assert local_ci.private_history_hits(repo, ["absentword"], "published..HEAD") == []


def test_screen_newlines_are_not_doubled(monkeypatch: pytest.MonkeyPatch) -> None:
    screen = Screen()
    monkeypatch.setattr(local_ci.sys, "stdout", screen)
    log = io.BytesIO()
    local_ci._pump(Chunks([b"a\r\n", b"b\r", b"\nc\n"]), log, "utf-8")
    assert screen.getvalue() == "a\nb\nc\n"
    assert log.getvalue() == b"a\r\nb\r\nc\n"  # the log keeps the bytes as they came
