<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Contribution roadmap

Updated 2026-11-20 against `main` at `8335e4e`. This is a guide to useful work,
not a release schedule. Check [open PRs](https://github.com/NRux/Goliath-CNC/pulls)
before starting. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and review.

## Useful work without hardware

| Area | A bounded contribution | Done when |
|---|---|---|
| Documentation | Try the development setup on Linux or Windows and fix an inaccurate step. | The commands are reproducible, links resolve, and supported-version claims match CI. |
| GI fixtures | Add a small authored example covering an existing parser rule or an edge case. | Input, expected outcome and rationale agree with the [GI specification](docs/prep/gi-format.md); the fixture manifest and relevant tests pass. |
| Geometry | Reproduce a numerical/geometry issue in the existing sweep, heading or material-removal code. | A small synthetic case demonstrates the issue and a focused regression checks the intended conservative result. |
| Local interface | Improve an inaccessible control, confusing diagnostic or missing empty state with fixture data. | The change works without a robot and preserves the UI's capability gates. |
| Project preservation | Propose a bounded XML/SVG project reader from the [format research](docs/research/slingshot-project-format.md). | Unsupported constructs and units are explicit; the first output is preservation data or previews, not machine-ready jobs. |
| Evidence quality | Cross-check a documented interface fact and record its source and limits. | Another person can reproduce the finding without obtaining proprietary source from this repository. |

## Integration work still needed

Preparation already includes parsing, normalization, machine/job configuration,
geometry, heading search and material-removal analysis. Runner foundations include
facts, gates, ledger/recording, diagnostic export and local HTTP/SSE presentation.
Remaining integration includes the complete preparation check/plan/export
pipeline, job bundles, application CLIs, and a robot session controller. Propose
interfaces and tests before large implementations; avoid assuming that a module
being present means its hardware-dependent behavior has been validated.

Existing FreeCAD, Fusion and Kiri:Moto integrations provide another way to
contribute. Their outputs still need the preparation and firmware gates; a CAM
post is not a safety checker.

## Hardware work is gated

The first active probe found firmware `01.00.13`, older than the inspected
`01.02.00` image. The probe now stops on old/unknown/conflicting identity. The
driver and hardware release remain held for **B4 design review**.

The next proposed diagnostic is a separate receive-only TCP observer outside
the frozen driver. It does **not exist yet**. Its
[acceptance criteria](docs/research/firmware-01.00.13-compatibility-review.md#offline-acceptance-before-connecting)
require zero application sends, bounded duration, complete teardown, explicit
operator phases, privacy-aware output and a distinct report rejected by the R1
importer. Software and review come before a hardware run.

Later work needs version-specific command admission, fresh cage-state evidence,
legacy sanity-response parsing, and verified upload/stop behavior. Firmware
recovery and rollback are separate research tasks; updating the robot is not a
shortcut through these requirements. The
[host compatibility analysis](docs/research/slingshot-host-compatibility.md)
explains why vendor behavior cannot simply be copied.

For owners who want to help now, the
[powered-off measurement guide](docs/field-guide/04-measure-your-robot.md)
collects useful geometry without software or motion. Do not post raw robot
captures or private identifiers in public issues.
