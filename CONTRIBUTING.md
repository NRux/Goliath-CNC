<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Contributing to Goliath CNC

You do not need a robot to contribute. Documentation, synthetic fixtures,
geometry checks, protocol analysis and the local interface all need help.
Start with the [roadmap](ROADMAP.md) and [documentation index](docs/README.md).

## Pick a bounded change

- Reproduce a software bug with a small synthetic input and a focused test.
- Improve an installation instruction after trying it on your operating system.
- Add a documented GI-format example with its expected parser result.
- Improve UI wording or accessibility using local fixtures, without a robot.
- For a new parser, safety rule, hardware procedure or architectural change,
  describe the intended behavior and evidence in an issue before a large patch.

Check [open issues](https://github.com/NRux/Goliath-CNC/issues) and
[pull requests](https://github.com/NRux/Goliath-CNC/pulls) before duplicating work.
If no issue covers a small correction, a focused pull request is welcome.
Discuss technical disagreements respectfully and avoid personal attacks or
sharing another person's private information.

## Set up a development environment

Fork the repository on GitHub and clone your fork. Add the original repository
as `upstream` so your branch starts from current work:

```sh
git remote add upstream https://github.com/NRux/Goliath-CNC.git
git fetch upstream
git switch -c your-change upstream/main
```

Use the [README's virtual-environment commands](README.md#first-local-success).
Below, `python` means that environment's interpreter: `.venv/bin/python` on
Linux or `.\.venv\Scripts\python.exe` on Windows. Activation is optional.
Install the sibling packages from this checkout before the root extras; do not
substitute similarly named packages from a package registry.

The applications require Python 3.11+. The root tools and frozen driver still
support Python 3.9 and have separate CI jobs. Keep changes compatible with the
component you are editing. Current matrices and installation commands are in
[ci.yml](.github/workflows/ci.yml); avoid changing supported versions as part of
an unrelated patch.

## Find the right component

| Work | Source and reference |
|---|---|
| Input parsing and preparation | [goliath-prep/src](goliath-prep/src/goliath_prep/), [GI format](docs/prep/gi-format.md), [safety model](docs/prep/safety-model.md) |
| Runtime facts, gates and UI | [goliath-run/src](goliath-run/src/goliath_run/), [package guide](goliath-run/README.md) |
| Wire protocol | [protocol documentation](docs/protocol/README.md), [driver freeze](tests/arch/goliath_link_freeze.json) |
| CAM integrations | [posts](posts/), each integration's README and licence |
| Research and reproducibility | [research report](docs/research/slingshot-reverse-engineering.md), [inventory tool](tools/slingshot_inventory.py) |
| Repository checks and local CI | [tests](tests/), [local CI guide](tools/local-ci.md) |

Application tests share a pytest session. Use `test_prep_*.py` in
`goliath-prep/tests` and `test_run_*.py` in `goliath-run/tests`; avoid duplicate
module names across those suites and root `tests/`.

## Validate your change

Run focused tests while editing. Before requesting review, run the suites your
change affects and the SPDX check. These commands work from the repository root:

```sh
python -m pytest -q
python -m pytest -q goliath-link/tests
python -m pytest -q goliath-prep/tests goliath-run/tests
python tests/arch/spdx_check.py
```

The tests use synthetic inputs, replayed evidence and local test servers;
they do not require a real robot. Network-dependent package installation is a
separate setup step. For all CI jobs, including alternate Python versions and
optional containers, see [tools/local-ci.md](tools/local-ci.md).

Explain what you actually ran and any tests you could not run. A passing test
or simulator does not establish real hardware safety. For a bug fix, use a
regression that demonstrates the incorrect behavior and the intended result.
For prose-only changes, check links and run the repository documentation checks.

## Hardware and evidence boundaries

The observed `01.00.13` robot remains in **B4**, the older-firmware hold. The
current [freeze record](tests/arch/goliath_link_freeze.json) covers all of
`goliath-link/`, including its README, tests and packaging. Do not edit it or
weaken its architecture test merely to make CI pass. A driver change needs the
B4 design review and a deliberate freeze update in the same reviewed change.
The older held [driver PR #17](https://github.com/NRux/Goliath-CNC/pull/17) does
not become ready simply because the first probe has run.

Use published interface facts and original implementation work. Do not add
vendor installers, decompiled source, firmware images or extracted assets.
Keep raw analysis in ignored `.local-research/` storage or outside the checkout.
If contributing research, identify artifact hashes, versions, methods and
uncertainties, and write the explanation in your own words. Mark each conclusion
as a hardware observation, static finding, inference or untested assumption.

Raw captures can include serial numbers and nearby Wi-Fi names in **both text
and hexadecimal data**. Share only an inspected, redacted copy; keep the original
private. Do not attach Wi-Fi passwords. The checked-in probe evidence is a
deliberately redacted historical example, not a template for accepting arbitrary
raw uploads. For a security-sensitive issue, use [SECURITY.md](SECURITY.md).

## Submit a pull request

1. Keep one coherent purpose per PR, with a title describing the resulting change.
2. Explain the problem, before/after behavior, evidence and validation. Link the
   relevant issue or specification. State hardware impact explicitly.
3. Include documentation or fixture updates when behavior changes. Use the
   [PR template](.github/pull_request_template.md) to make review straightforward.
4. Push your branch to your fork and open a PR against `main`. Drafts are useful
   for early design feedback; mark the PR ready when your reviewable work is done.
5. Address review comments and keep all applicable CI jobs passing. The default
   branch requires a PR and resolved review conversations; do not force-push it
   or bypass its protection. CI does not clear the hardware hold.

The maintainer is [NRux](https://github.com/NRux). There is no promised review
turnaround. If you use AI tools, review their output and describe any material
validation limits; generated claims and tests need the same evidence as other work.

## Licensing your contribution

Use the existing licence for the target folder from [LICENSING.md](LICENSING.md).
Add the appropriate SPDX header to source files, and preserve third-party
notices when reusing material whose licence permits it. Prose can use the same
header in an HTML comment. By submitting a contribution, you are offering it
under that component's licence; submit only material you have permission to
contribute. No additional CLA or sign-off process is introduced by this guide.
