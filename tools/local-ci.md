<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Running the CI jobs on your own machine

`tools/local_ci.py` runs the jobs of [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)
on your computer, started by hand. It runs the same jobs, Python versions,
environment variables, install and test commands, and time limits.
`tests/test_local_ci.py` fails as soon as the script's copy and `ci.yml` differ.

It is **not** a self-hosted GitHub runner:
- Nothing is registered with GitHub.
- No pull request from anyone else ever runs on your machine.
- It only tests the checkout you start it in.

That is deliberate. This repository is public, and GitHub's security guide
says self-hosted runners "should almost never be used for public
repositories on GitHub, because any user can open pull requests against the
repository and compromise the environment"
(*Security hardening for GitHub Actions*, section *Hardening for self-hosted
runners*).

## What you need

- **Python 3.9 or newer** to start the script (any version; each job gets
  its own).
- **git** on `PATH`.
- **uv**, which provides each Python version the jobs use. You need it for
  `--where host`, the default. Install it once:
  - Windows: `winget install astral-sh.uv`
  - anywhere: `python -m pip install uv`
- **Docker** (Docker Desktop on Windows) for `--where docker` only.
- **The GitHub CLI `gh`, logged in**, but only if you set up the busy check
  (below).

## Running it

From the repository root (Windows PowerShell shown; in Git Bash or on Linux
use `python3` or `python` as you normally do):

```powershell
py tools\local_ci.py --list                 # what would run, command by command; runs nothing
py tools\local_ci.py                        # every job, on this computer
py tools\local_ci.py --job apps             # one job, both of its Python versions
py tools\local_ci.py --job test --python 3.13
py tools\local_ci.py --where docker         # every job, in throwaway Linux containers
py tools\local_ci.py --dry-run              # the banner and the commands; runs nothing
```

Every job runs even if an earlier one failed, as on GitHub (`fail-fast:
false`). Add `--fail-fast` to stop at the first failure.

Within a job, the steps follow GitHub's rule too, in both `--where` modes.
After a step fails, a step that `ci.yml` marks `if: ${{ !cancelled() }}`
still runs, so each test suite always reports and a failure in one never
hides another. Any other step after a failure does not run. The job is FAILED
if any step failed, and the summary names every failed step, including one
that could not start (a missing `working-directory`, a program not on the
job's `PATH`, a program the system refuses to start). Reaching the job's time
limit still stops it, as on GitHub. So does Ctrl+C: with `--where host` the
summary then still names the steps that had failed; with `--where docker` the
container is stopped at once, so only the log names them. One difference: when
the script cannot set up the job's Python (`uv venv`, its stand-in for
`actions/setup-python`), no step runs, because there is no Python to run them
with; the job does not pass, and the summary says why. `tests/test_local_ci.py`
refuses any other step condition in `ci.yml`.

At the start, a banner says:
- what will run, and where;
- which commit is tested;
- whether the busy check is on;
- which of your environment variables are not passed on;
- where the logs go.

Every command runs with `CI=true`, as on GitHub. The property-testing library
Hypothesis reads it and then uses the same fixed examples as CI. Commands also
run without your personal git settings (`GIT_CONFIG_GLOBAL` points at an empty
file), as a GitHub runner has none.

The script runs each command directly, without a shell. A command in
`ci.yml` therefore may not use shell syntax: no variables, `>` or `|`, `&&` or
`;`, globs, comments, `NAME=value` prefixes, shell builtins such as `cd` or
`echo`, or GitHub's `$GITHUB_ENV`-style files. `tests/test_local_ci.py`
refuses such a command in `ci.yml`, and the script refuses to run one.

When CI never runs a job on your kind of system (the `spdx` job runs only on
Linux in CI), the banner says its result here has no CI counterpart.

At the end, a summary lists each job and Python version as PASSED, FAILED,
TIMED OUT, INTERRUPTED, NOT RUN or ERROR, with its log file.

| Exit code | Meaning |
|---|---|
| 0 | every selected job passed |
| 1 | a job failed, hit CI's time limit, or hit an error; this wins over 2 and 3 when the run also stopped |
| 2 | the run could not start, or stopped before any job failed: a missing tool, a bad option, a `--cpus` value Docker's engine cannot give, or the busy check could not ask GitHub |
| 3 | stopped because the configured other CI is running (see `--wait`) |
| 130 | you pressed Ctrl+C |

### `--where host` (default): this computer

- Each job and Python version gets a **fresh virtual environment**, made with
  `uv venv`, in the script's own folder (below). The CI commands then run in it
  exactly as written in `ci.yml`.
- It tests the **working tree as it is**, uncommitted changes included. The
  banner says how many there are.
- On Windows this stands in for CI's `windows-latest` jobs, and on Linux for
  the Ubuntu ones.

### `--where docker`: throwaway Linux containers

- Each job and Python version runs in its own container
  (`docker run --rm`). The container gets:
  - your checkout mounted **read-only**;
  - 2 CPUs (`--cpus N` changes it);
  - a network of its own, `goliath-local-ci-net`, created once, not Docker's
    default bridge, which other projects' containers may share;
  - no raw network sockets (`--cap-drop NET_RAW`) and no new privileges;
  - **no** access to the Docker engine (no `docker.sock` mount, no
    `--privileged`).
- Inside, it clones your checkout and checks out **the commit you are on**.
  It then runs that commit's own copy of this script, so the jobs and their
  commands are the commit's too. It tests what you committed, with Linux line
  endings, like a CI checkout. **Uncommitted changes are not tested**,
  including changes to this script or to `ci.yml`; the banner and the
  summary say so when you have some.
- The first run builds a small image, `goliath-local-ci:<hash>`, from
  [`tools/local-ci/Dockerfile`](local-ci/Dockerfile): Debian with git and uv.
- Python versions and pip downloads are kept in the Docker volume
  `goliath-local-ci-cache`, so later runs download almost nothing.
- It needs an ordinary clone. A `git worktree` checkout has its history
  outside the folder Docker can see, and the script refuses it with that
  reason.
- `tools/local_ci.py` and `.github/workflows/ci.yml` must be committed: the
  container runs the commit's own job list, so the script refuses to start
  while either has uncommitted changes.
- `--cpus` cannot exceed the CPUs Docker's engine has, which on Docker Desktop
  can be fewer than the computer's (Settings, Resources). The script checks
  this before starting. If Docker cannot start a container at all, the job is
  reported as ERROR, not as a failed test.
- Behind a proxy that re-signs HTTPS (some company networks), set
  `GOLIATH_LOCAL_CI_CA_BUNDLE` to a file with the extra CA certificates. The
  containers then trust it. Building the image needs no such setting.

## The busy check: another project's CI on the same machine

If this computer also runs another project's CI, and that CI suffers when the
machine is busy, the script can check before every job, and before building
its image, whether that project has a run of a given workflow in progress. It
asks GitHub with `gh run list -R OWNER/REPO --workflow NAME --status
in_progress`. gh matches the workflow by its name (in any case) or its file
name, and fails when the repository has no such workflow, so a misspelt name
stops the run rather than matching nothing.

Set it up once. The setting is saved in your user profile, never in this
repository:

```powershell
py tools\local_ci.py --configure-busy-check OWNER/REPO "Workflow name"
```

It first asks GitHub whether that workflow exists, and saves nothing if it
does not. It writes `busy-check.ini` to `%APPDATA%\goliath-local-ci\` on Windows,
`~/.config/goliath-local-ci/` on Linux, or
`~/Library/Application Support/goliath-local-ci/` on macOS.
- To use another file, set `GOLIATH_LOCAL_CI_CONFIG`.
- To set it for one shell only, set `GOLIATH_CI_BUSY_REPO` and
  `GOLIATH_CI_BUSY_WORKFLOW` instead; they win over the file.

When a run of that workflow is in progress:

| You give | It does |
|---|---|
| nothing | stops with exit code 3 and the run's URL; nothing more starts |
| `--wait` | waits, asking GitHub every minute and reporting every 5 minutes, then goes on |
| `--ignore-busy` | does not check; the banner says the check was skipped |

If the check cannot ask GitHub (`gh` missing, not logged in, no access to that
repository, no such workflow), the script stops with gh's message rather than
running unchecked. With no busy check configured, the banner says
`not configured` on every run.

The settings file must be outside the checkout; the script refuses one inside
it, because it names the other project. If `GOLIATH_LOCAL_CI_CONFIG` names a
file that does not exist, the script stops rather than running unchecked.

The other repository's name is also kept out of this one:
- `tests/test_local_ci.py` checks that no file in the checkout contains the
  name of the repository the busy check watches, in its contents (UTF-8 or
  UTF-16, Windows PowerShell 5.1's default), its path, or a symbolic link's
  target. It also checks everything a push would upload: every commit not
  yet pushed (since the branch's upstream, or `origin/main`), its message and
  author, and every file version in it, read as stored, so a UTF-16 file added
  in one commit and deleted in the next is still found.
- If one is found, remove it from history before pushing (amend or rebase).
  A new commit that deletes it leaves it in the old one.
- To add more words, such as the other project's own name, set
  `GOLIATH_PRIVATE_WORDS` (comma-separated, at least 4 characters each). A
  repository name shorter than that is not searched for, and the banner says
  so.
- The words come from your settings when the check runs and are never stored
  here. With none configured, the check is skipped and says why.
- The vendor files in `old_software/` are not opened.
- In `--where docker` the containers cannot see your settings. The script
  itself checks the commit, and the unpushed commits' messages and changes,
  for the words before the jobs, and reports the result as its own line in
  the summary.

## Resource use

- **CPU**, with `--priority low` (the default; `--priority normal` turns it
  off):
  - `--where host`: everything the script starts runs at low priority (nice
    +10 on Linux and macOS, *below normal* on Windows), so other work on the
    machine comes first.
  - `--where docker`: the containers are started by Docker, not by the
    script, so they get a lower CPU weight instead: `--cpu-shares 256`, a
    quarter of Docker's default 1024, so other containers on the same engine
    win when the CPU is busy. Each container is also capped at `--cpus`
    (default 2). Building the image is not lowered.
- **Disk and network:**
  - The first run downloads the Python versions and the packages.
  - Docker mode also downloads two base images, a few hundred MB.
  - Later runs reuse all of it.
- **One run at a time:** a second run started while one is going stops at
  once with a message. The lock is a file the operating system releases even
  if the first run crashes.
- **Time limits:** the same as CI (15, 20 and 5 minutes per job). A job that
  hits its limit is killed with everything it started and reported as TIMED
  OUT.

## Where things are kept, and how to remove them

- Virtual environments, logs and the lock live outside the repository:
  - Windows: `%LOCALAPPDATA%\goliath-local-ci\`
  - Linux: `~/.cache/goliath-local-ci/`
  - macOS: `~/Library/Caches/goliath-local-ci/`
  - Set `GOLIATH_LOCAL_CI_HOME` to use another folder outside the checkout.
- Each run's logs are in `logs/<date-time>/`, one file per job and Python
  version. They are never deleted automatically; delete old ones when you
  like.
- To remove everything docker mode made, delete only its own images, volume
  and network, by name:

  ```powershell
  docker image ls goliath-local-ci          # the images it built
  docker rmi goliath-local-ci:<tag>
  docker volume rm goliath-local-ci-cache
  docker network rm goliath-local-ci-net
  ```

  Never run Docker's prune commands for this. They remove images, volumes and
  build caches of **every** project on the engine, not just this one.

## How a local run differs from GitHub's

| | GitHub | Local |
|---|---|---|
| Operating system | Ubuntu, and Windows Server for `windows-latest` | your computer (`--where host`), or Debian 13 in a container (`--where docker`) |
| Python builds | `actions/setup-python` | uv's builds (python-build-standalone), or a matching Python already on your machine |
| Checkout | a fresh shallow clone (full history in the `apps` job); for a pull request, GitHub tests **your branch merged into its base**, not your branch alone | `host`: your working tree with your full history; `docker`: a fresh full clone of your commit. So the goliath-link freeze-commit check runs in every job here; on GitHub it skips in the shallow `test` job. To test what a pull request's CI will test, merge the base branch (`git merge origin/main`) first. |
| A failing command in a multi-line step | `bash` stops; PowerShell, the Windows default in the `test` job, goes on to the next line and only the last command's exit code counts | always stops at the first failing command |
| The `spdx` job | Ubuntu only | wherever you run it |
| `CI` | `true` | `true` (set by the script) |
| Environment | a clean runner | `host`: yours, minus the variables that change how Python or pytest behave (`PYTHON*`, `PYTEST_*`, `VIRTUAL_ENV`, `CONDA_PREFIX`) and minus credentials (names containing `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`, `API_KEY`, `ACCESS_KEY`, `PRIVATE_KEY` or `AUTH`, uv's and pip's included, such as an upload token); the banner names every variable it drops. pip's and uv's own download settings (`PIP_*`, `UV_*`, package-index logins included, and your pip and uv config files) still apply, because downloads may need them. `docker`: none of yours |
| Secrets, tokens | none | none passed on deliberately; see the row above |

A green local run is good evidence, not a guarantee. The pull request's CI on
GitHub is still the check that counts.
