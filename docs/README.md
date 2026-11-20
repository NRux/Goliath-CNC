<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Documentation

New contributors: start with [CONTRIBUTING.md](../CONTRIBUTING.md) and the
[roadmap](../ROADMAP.md). The repository is under development; documentation
about a planned feature is not evidence that a runnable application exists.

## Protocol and preparation

- [Protocol index](protocol/README.md): transport, commands, sessions, firmware
  differences and the surviving firmware image.
- [GI interchange format](prep/gi-format.md) and [example corpus](prep/gi-examples/manifest.json).
- [Safety model](prep/safety-model.md), [check catalogue](prep/checks.json)
  and [parameters](prep/parameters.md).
- [Preparation package](../goliath-prep/README.md) and
  [runner package](../goliath-run/README.md): implemented foundations and tests.

## Hardware observations and research

- [First probe evidence](../probe-results/README.md): redacted November 5 capture,
  firmware `01.00.13`, and its limits. This was an active diagnostic probe.
- [Compatibility review](research/firmware-01.00.13-compatibility-review.md):
  historical findings, current disposition, and the proposed receive-only test.
- [Slingshot reverse engineering](research/slingshot-reverse-engineering.md):
  reproducible REA inventory and static-analysis coverage.
- [Host firmware pitfalls](research/slingshot-host-compatibility.md) and
  [project formats](research/slingshot-project-format.md).
- [Prior-art review](research/prior-art-review.md): earlier research and possible
  building blocks. Treat dated availability and planning claims as historical.

## Owner field guides

- [01: First contact](field-guide/01-first-contact.md): USB/Wi-Fi investigation.
- [02: Diagnostic probe](field-guide/02-first-probe.md): active queries, identity
  stop behavior and hardware precautions. It is not a passive observer.
- [03: Install the applications](field-guide/03-install-the-applications.md):
  source setup and offline checks, with planned interfaces marked.
- [04: Measure the robot](field-guide/04-measure-your-robot.md): powered-off geometry.

Read the current [hardware status](../README.md#hardware-status) before a field
guide. Historical instructions do not override the older-firmware hold.

## CAM integrations

- [FreeCAD post](../posts/freecad/README.md)
- [Fusion post](../posts/fusion/README.md)
- [Kiri:Moto configuration](../posts/kirimoto/README.md)

## Maintenance

- [Local CI](../tools/local-ci.md) and [GitHub CI](../.github/workflows/ci.yml)
- [Licensing boundaries](../LICENSING.md)
- [Security reporting](../SECURITY.md)
- [Default-branch rules](../.github/rulesets/README.md)
