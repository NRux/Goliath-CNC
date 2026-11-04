<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Field guide 03: install the applications (Windows or Linux, about 20 minutes)

> **Not released yet.** `goliath-prep` (prepares and safety-checks a job on your
> computer) and `goliath-run` (runs it on the robot) are **not released yet**.
> They are being built on the repository branch `feat/prep-and-run`. Until they
> are released, the only thing you can install and use is the read-only probe of
> [field guide 02](02-first-probe.md).
>
> This guide describes the install procedure as it will be. Before the release,
> some of its steps fail or do nothing useful; for example, `goliath-run doctor`
> does not exist yet. Nothing in this guide connects to the robot.

**Goal:** install the applications on your laptop while it has internet, then
prove, with the internet switched off, that the laptop has everything they need.

**Why in that order:** at the robot, the laptop joins the robot's own Wi-Fi
network, which has no internet. Nothing can be downloaded or installed there. So
everything is installed beforehand, and `goliath-run doctor` checks it offline
before you go near the robot.

**You need:** your Windows or Linux laptop, with internet for steps 1 to 4;
Python 3.11 or newer (step 1); a copy of this repository that contains the
folders `goliath-link`, `goliath-prep` and `goliath-run` (step 2).

## What stays as it is

The probe of field guide 02 keeps its own environment in `goliath-link/.venv`,
made with Python 3.9 or newer. Leave it alone: don't delete it, don't upgrade it,
and don't install the applications into it. The applications get a second,
separate environment in `goliath-run/.venv`, because they need Python 3.11 or
newer.

## 1. Get Python 3.11 or newer (5 to 10 minutes, once)

**Linux.** In a terminal:

```bash
python3 --version
```

If it prints `Python 3.11` or higher (3.12, 3.13, 3.14), use `python3` in step 3.
If it prints 3.10 or lower, install a newer Python from your distribution's
packages (on Debian and Ubuntu releases that offer it, for example
`sudo apt install python3.12 python3.12-venv`) and use that command, for
example `python3.12`, instead of `python3` in step 3.

**Windows.** In PowerShell:

```powershell
py --list
```

It lists the Python versions installed. If none is 3.11 or newer, or `py` is not
found, install a current Python 3 with the official Windows installer from the
Python website, keeping its default options (they include the `py` launcher),
and run `py --list` again. In step 3, `py -3.12` stands for the version you
have: write `py -3.13` if that is the one listed, and so on.

## 2. Get the repository copy (5 minutes)

Use the copy of the repository from field guide 02, updated (`git pull`), or a
fresh **Code → Download ZIP** from GitHub. Until the release, the folders
`goliath-prep` and `goliath-run` exist only on the branch `feat/prep-and-run`.
A ZIP download may name the folder `Goliath-CNC-` followed by the branch name;
use that name below instead of `Goliath-CNC`.

Check that all three folders are there:

**Linux:**

```bash
cd Goliath-CNC
ls goliath-link goliath-prep goliath-run
```

**Windows (PowerShell):**

```powershell
cd Goliath-CNC
dir goliath-link, goliath-prep, goliath-run
```

If any of the three is missing, you have the wrong copy or branch: stop here.
Every command below is run from this `Goliath-CNC` folder.

## 3. Create the applications' environment (2 minutes, once)

**Linux** (use `python3.12` or similar instead of `python3` if step 1 told you
to):

```bash
python3 -m venv goliath-run/.venv
goliath-run/.venv/bin/python --version
```

**Windows (PowerShell):**

```powershell
py -3.12 -m venv goliath-run\.venv
goliath-run\.venv\Scripts\python --version
```

The second line must print Python 3.11 or newer. If it doesn't, delete the
folder `goliath-run/.venv` and repeat this step with the right Python.

On Linux, if creating the environment fails with `ensurepip is not available`,
install your distribution's venv package for that Python (on Debian and Ubuntu,
for example, `sudo apt install python3.12-venv`), delete `goliath-run/.venv`, and
repeat this step.

## 4. Install the three packages, online (5 to 10 minutes)

The laptop must have internet for this step: pip downloads the libraries the
applications use (for example shapely, for the geometry checks).

**Linux:**

```bash
goliath-run/.venv/bin/python -m pip install --upgrade pip
goliath-run/.venv/bin/python -m pip install -e ./goliath-link -e ./goliath-prep -e ./goliath-run
```

**Windows (PowerShell):**

```powershell
goliath-run\.venv\Scripts\python -m pip install --upgrade pip
goliath-run\.venv\Scripts\python -m pip install -e .\goliath-link -e .\goliath-prep -e .\goliath-run
```

- **Install the three together, in one command, from these folders**, exactly as
  written. `goliath-prep` and `goliath-run` depend on `goliath-link`. Installed
  together, pip uses the copies in this repository. Installed one at a time, pip
  would look for the others on the internet instead, where no released version
  exists, and where a package of the same name could be someone else's.
- `-e` ("editable") makes the environment use the files in the repository
  folders directly. After an update, you run this command again (see *After an
  update*).
- If pip ends with an error (a line starting with `ERROR`), don't go on: see
  *If something fails*.

## 5. Check offline with `goliath-run doctor` (2 minutes)

1. **Switch the laptop's internet off**: turn Wi-Fi off and unplug any network
   cable. Don't join the robot's network.
2. Run:

   **Linux:**

   ```bash
   goliath-run/.venv/bin/goliath-run doctor
   ```

   **Windows (PowerShell):**

   ```powershell
   goliath-run\.venv\Scripts\goliath-run doctor
   ```

3. `doctor` checks, without the network, that everything the applications need
   imports, that the files of the browser interface are present, and that the
   machine profile loads. What it prints is not settled yet; this guide will show
   it once `goliath-run` is released. Read every line: it must report no failure.
4. When `doctor` reports no failure, the laptop is ready, and you can switch the
   internet back on. **Don't connect `goliath-run` to the robot until the field
   guide of a later rung tells you to.** The first rung that uses it with the
   robot, the read-only follow-ups (R1b), comes after the probe of field guide 02
   and the measurements of [field guide 04](04-measure-your-robot.md), which
   need no laptop.

## If something fails

- **Don't join the robot's network, and don't start any rung.** Nothing can be
  installed there, so a problem found at the robot cannot be fixed at the robot.
- **Repeat the install:** switch the internet back on, run step 4 again exactly
  as written, then switch the internet off and run `doctor` again. This fixes an
  incomplete install, for example an interrupted download.
- **Python version:** if a message mentions the Python version, the environment
  was made with a Python older than 3.11. Delete the folder `goliath-run/.venv`
  and start again at step 3.
- **Machine profile:** if `doctor` reports a problem with the machine profile,
  don't edit the profile by hand to make it pass. A hand-edited profile counts as
  an owner profile, not as measured values. Send the output back instead.
- **Still failing:** save the whole output to a file and send it back.

  **Linux:**

  ```bash
  goliath-run/.venv/bin/goliath-run doctor > doctor.txt 2>&1
  ```

  **Windows (PowerShell):**

  ```powershell
  goliath-run\.venv\Scripts\goliath-run doctor *> doctor.txt
  ```

  Check the file for your robot's serial number before you send it, and share it
  privately, the same way as the probe results, not in a public GitHub issue.

## After an update

Whenever you update the repository (`git pull`, or a new ZIP), repeat step 4 with
the internet on and step 5 with it off. With a new ZIP in a new folder, start at
step 3 in that folder.

## Later: the public release

For the public release, ready-made packages are planned, among them a Windows
installer that brings its own Python. Until then, this guide is the way to
install the applications.
