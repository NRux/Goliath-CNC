<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Firmware GFW 01.00.13: compatibility review and next diagnostic

**Date:** 2026-11-05. **Status:** review complete; next diagnostic design proposed, implementation required. **Reviewer:** Codex. **Reviewed revision:** `fa2f4764805991df139a809f2140463a4c49bf7b`.

**Disposition updated 2026-11-20 against main `8335e4e`:** the original review
predates the probe hotfix. **C1 is resolved** by PR #25: the probe now stops
further commands after old, unknown or contradictory identity, including a
check at the send boundary. The importer also rejects stopped reports and
shareable copies. The historical observations and verification below describe
the earlier revision; they are not claims that those fixed defects remain.
The general-client policy, legacy response handling, and separate observer
still need work. The driver freeze and B4 hardware hold remain in effect.

The first hardware capture establishes a working TCP text exchange with firmware **01.00.13**, not compatibility with the analyzed **01.02.00** firmware. Keep this robot in **B4: probe only, design review**, with jobs and motion unavailable. Do not update firmware to make it fit the current driver assumptions.

The next proposed hardware experiment is a bounded **passive observation with no application command bytes sent**, initially with towers off and then, if practical, with them on. Implement and verify that observer before running it. The current `goliath_link.probe` CLI has no passive-only or identity-only option; `--terminator none` skips terminator selection but is still an active probe subject to the firmware gate.

## Evidence and limits

The published [JSON report](../../probe-results/goliath-probe-20261105T193222Z.json), [capture](../../probe-results/goliath-probe-20261105T193222Z.capture.txt), and [initial analysis](../../probe-results/README.md) describe the hardware evidence. Published copies have identifiers replaced by same-length placeholders; private originals contained the serial and nearby network names. This review does not repeat them.

| Command or behavior | Observation from this run | Compatibility conclusion |
| --- | --- | --- |
| Connect | Four sessions each opened three connections with the existing gaps and two-second release wait. | This sequence worked in this run. It does not establish the total slot count or minimum release interval for 01.00.13. |
| Connect-time output | `INFO;34` and network scan `INFO;31` messages arrived on S2 in every session. | A passive identity and scan path is available in this capture; do not send `RESET NET KEY` to obtain it. |
| `INFO` | The no-terminator test and sweep each received an identity on S2, in addition to connect-time identities. All six decoded identities name the same firmware. LF and CRLF tests received no identity after their sends. | Preserve exact `INFO` spelling and no terminator if a later reviewed test needs an identity request. Silence does not prove all unknown commands are harmless. |
| `GET GLOBAL SYS STATUS` | Zero received bytes during its five-second window. | No status reply was observed. Unsupported command, state dependence, or another firmware difference cannot be distinguished by silence. Do not retry automatically or use it as an application keepalive. |
| `PRINT SYS REPORT` | Two received chunks, 6,656 bytes in total: 13 padded text messages, consisting of a header, 11 subsystem initialization lines marked `OK`, and an `END` footer. Header about one second after send; footer about 1.5 seconds after send. All on S1. | A real response exists. The `INFO;40` matcher is inappropriate for this build. Initialization `OK` is not proof of operational safety, tower pairing, or cage state. This capture does not show whether the handler reads stored results or actively runs checks. |
| `GET SPINDLE CAGE STATUS` | Zero received bytes during its five-second window. | Consistent with the vendor gate requiring 01.00.14 or later. It does not show the cage is open or closed. |
| `READ INDEX CALIBRATION` | Value text on S1 and `INFO;0` on S2; 1,024 bytes total. | Response shape is observed; side effects are not established on 01.00.13. Even the 01.02.00 configuration getter can repair flash copies. Omit from the next diagnostic. |
| `READ SPINDLE CALIB` | Value text on S1 and `INFO;0` on S2; 1,024 bytes total. | Response shape is observed; calibration semantics and side effects on this firmware remain unverified. Omit from the next diagnostic. |
| Binary telemetry | No position, tower, or global-status frame was decoded. | S3 position routing, binary layouts, tower firmware, and tower power/pairing state remain unestablished. No frame is not proof of no physical motion. |

The JSON says the run completed with exit code 3, three missing-reply warnings, no probe errors, and no routing violations. That means the messages seen fit the checked slot rules; it does not validate slot roles for message types never received. The report does not explicitly record the tower power state or attest that the machine remained physically still.

The firmware string includes control bytes after the version. Preserve them in evidence, but do not interpret them as tower firmware or a hardware revision without a documented decoder. The current text parser already retains this identity correctly.

## Sources and their strength

- **Hardware observation:** the captured bytes above, from one robot and one run.
- **Vendor behavior documented by prior analysis:** [version compatibility](../protocol/07-version-compatibility.md) records `GET SPINDLE CAGE STATUS` only from 01.00.14, legacy `POLL CAGE` / `STOP POLL CAGE` below it, and periodic global-status requests only above 01.02.00. These are host-side decisions, not proofs of the older firmware's handlers.
- **Static firmware evidence:** [01.02.00 handler analysis](../protocol/09-firmware-01.02.00.md#read-only-allowlist-basis-for-the-probe) establishes the six-command allowlist on 01.02.00 only. No 01.00.13 image or handler analysis was available for this review.
- **Recovery constraint:** [firmware notes](../protocol/06-firmware.md) describe the preserved 01.02.00 main image and unresolved recovery/rollback details. An upgrade is a separate recovery decision, not a compatibility fix.

This review uses the published protocol analysis and clean-room driver code. It does not inspect vendor decompiled source or installers.

The separate November 6 [host analysis](slingshot-host-compatibility.md) adds
important later evidence: Slingshot's whole-banner version parsing can fold
build-date digits into the version, and its cage path can proceed on unknown
state. Nominal vendor gates above must not be treated as robot-handler proof.

## Findings and required changes

| ID | Finding | Required change and scope |
| --- | --- | --- |
| C1 | **Resolved by PR #25.** At the originally reviewed revision, the probe admitted the full sweep after discovering 01.00.13. | The current firmware gate validates identities throughout the run and checks admission again at the send boundary. Old, unknown or conflicting identities stop further sends; omitted commands have explicit reasons. Preserve this behavior. |
| C2 | **Before using the general client on this robot:** `FirmwareCapabilities.supports_command` returns `allowed=True, reason="not version-gated"` for status/cage reads, legacy polling, network reset, jog, and upload names on 01.00.13. The runner's B4 job gate is not this library-level admission check. | Default unknown/older firmware to a restricted diagnostic policy. Distinguish observed support, vendor expectation, static verification, and permitted sends. No arbitrary command, motion, upload, calibration write, provisioning, or loader entry may pass the B4 diagnostic path. A caller override must not widen that path. Retain the runner's independent B4 block. |
| C3 | **Do not substitute legacy commands automatically:** `POLL CAGE` starts a timer on 01.02.00; it changes state. The existence and cleanup behavior of its 01.00.13 handler have not been inspected or captured. `RESET NET KEY` is unnecessary for the next observation. | Omit polling, stop-polling, network reset, and all setters from the next diagnostic. A future cage-specific test needs a separate reviewed procedure: exact commands, both OPEN/CLOSED observations, timeout and disconnect behavior, cleanup, and physical stop provision. A command name that sounds like a read is not enough evidence. |
| C4 | **Before repeating the sanity request:** `probe.EXPECTED_REPLIES` accepts only `INFO;40` for `PRINT SYS REPORT`; `_Window` fills one event per reply part. The R1 importer also understands only `INFO;40` sanity values. | Add a version-scoped multiline result for 01.00.13: exact header, named subsystem results, and footer on S1 in the request transaction. Keep waiting through the footer; detect truncation, duplicate/missing fields, late messages, and interleaving. Preserve raw text and names. Do not turn the 11 textual results into the unrelated 01.02.00 positional array. First establish a basis for repeating this handler safely. |
| C5 | **Before integrating a reduced diagnostic report:** `read_probe_report` records `rung.r1.go=True` after its checks, while missing individual sweep parts become absent facts. A reduced diagnostic must not masquerade as a full R1 report. | Give the observer a distinct tool/schema that the current R1 importer rejects. If a future common schema carries profiles, require the importer to validate the profile and completed checks. Diagnostic completion must not record `rung.r1.go`, a full fingerprint, a safety baseline, or open a gate. Preserve the existing named-human warning acknowledgement requirement for R1. |
| C6 | **Before using the new observer on hardware:** current simulator profiles describe 01.02.00 or hypothetical other firmware, not this captured 01.00.13 behavior. | Verify zero command attempts with a fake transport, exact teardown, bounded duration, output integrity, and failure paths. Replay this capture as empirical text evidence. Any synthetic 01.00.13 simulator must label behaviors not observed here as unknown and must not inherit motor/status semantics from 01.02.00. |
| C7 | Documentation called 01.02.00 the smallest safe default, described the probe as never run on hardware, and claimed a real machine ran 01.02.00 fine. | Correct the unfrozen guides alongside this review. The frozen `goliath-link/README.md` also needs a status correction in its next reviewed code change. |

C1 is now fixed; C2 and C4 remain relevant before extending active diagnostics on this robot. They are not a reason to modify the frozen library just to build the passive observer described below. That observer can live in `tools/` and use the existing receive-only transport path. The general client's permissiveness is an admission-policy gap separate from the fixed probe gate; this review did not execute a motor or upload operation.

The [runner job gate](../../goliath-run/src/goliath_run/capabilities.py) already returns false for every firmware older than 01.02.00 before considering R1 facts. Preserve it. Individual feature gates are not a substitute for a session-wide B4 send policy at future hardware entry points.

## Proposed next hardware test

**Purpose:** capture connect-time identity and any unsolicited tower traffic under an explicit tower power sequence. This is a diagnostic observation, not a rung authorizing motion or a test of supported commands.

### Software that must exist first

Implement a small observer in `tools/`, outside the frozen `goliath-link/` tree:

1. Use `GoliathTransport` to open S1, S2, S3 with its existing connection gaps and read all records. Do not attach `GoliathClient`, run the current probe, or start an application keepalive.
2. Expose **no application send operation**. Do not fall back to `INFO`, polling, resets, RF pairing, or any other command if passive traffic is absent. Normal TCP handshakes and OS TCP keepalive packets are distinct from application commands.
3. Bound each connection attempt and cap collection at 60 seconds after the handshake, including any wait for an operator phase confirmation. Capture before/during connection and through teardown. Close every opened socket on timeout, error, interruption, and partial handshake failure; write partial evidence with an explicit incomplete outcome.
4. Require a consistent pushed identity on S2 for the expected robot and exact version 01.00.13 before proceeding to the towers-on observation. If identity is missing, changes, or is malformed, close and keep the capture. Preserve the observed control bytes in the project label; validate the version and identity fields without treating those known bytes as a different version. Unexpected ERR/WARN or loss of a connection ends the experiment. Escape robot text in terminal output.
5. Record operator-confirmed tower power phases and timestamp the boundaries. Treat any phase not confirmed as unknown. Record that the bit is removed and the robot is on blocks as operator attestations, not facts inferred from bytes.
6. Write a distinct diagnostic JSON and raw capture. Include tool revision, report schema, expected/observed identity, phase times, per-connection counts, termination reason, and an explicit **application TX byte count of zero**. Do not produce R1 facts or a compatible result. Keep new raw captures private by default.

The existing CLI does not implement this procedure. No new command line is authorized or supplied by this review.

### Offline acceptance before connecting

- A fake transport that rejects/counts every send must show **zero send attempts** through normal completion, no identity, unknown/changed identity, unexpected data, errors, interruption, and partial connection failure.
- All opened sockets close on every exit. JSON/capture output survives interruption and clearly distinguishes completed observation from compatibility acceptance.
- Receive parsing must preserve the existing capture at original boundaries and at small arbitrary splits, including the control bytes in the identity string and the padded multiline sanity output.
- The diagnostic schema must be rejected by the R1 importer. B4 jobs stay closed even if the diagnostic completes normally; no warning acknowledgement is invented.
- Review the observer's source and transmitted-byte assertions. The `goliath-link/` freeze tree and recovery/release safeguards stay enforced.

### Operator sequence after the observer passes review

Use the established physical precautions: remove the bit, support the robot so all wheels are free, close other clients, and power-cycle before joining its hotspot. Start with both towers off. Collect 15 seconds of passive traffic after connection and a valid pushed identity; then, while leaving the robot untouched, switch both towers on and explicitly mark that phase. Collect another 15 seconds, then close the session. If the towers cannot be switched on without disturbing the robot, defer that phase.

Keep clear and stop the experiment using the physical controls if unexpected behavior occurs. Any unexpected physical movement or spindle activity ends this procedure; the observer cannot provide a software stop. Power the robot off when finished.

**Interpretation:** new tower frames are evidence of unsolicited traffic in that phase, not proof of their field meanings or position accuracy. No frames means **not observed**; it does not distinguish unpaired towers, radio failure, firmware behavior, or timing. Do not follow silence with a pairing or polling command. This test cannot settle cage safety, upload completion, stop behavior, Z direction, or motion limits.

## Verification performed for this review

These are the original November 5 checks, before the hotfix. In particular,
the historical `_checked_send` result below no longer describes the current
probe. Current implementation and tests are the authority for fixed behavior.

All checks ran offline; no robot connection was opened and no robot FactStore was changed.

- Parsed 53 capture records: 44 received chunks and nine sends. The two status/cage windows had zero received chunks; the sanity window had two chunks containing 13 padded messages.
- Replayed all received data through `StreamFramer` at original boundaries and in chunks of 1, 7, and 511 bytes. Each replay reproduced all **63 text events** from the report with matching session, connection, and text, and no discarded events. This validates these text paths only; no binary telemetry was present to validate.
- Evaluated the actual 01.00.13 command classifier: global status, cage status, legacy polling, network reset, jog, and upload names were all admitted as not version-gated. `ABORT WORK` was refused. No send was performed on a socket.
- Exercised `_checked_send` against an in-memory sink: it accepted global status with no firmware argument, confirming C1.
- Evaluated runner gates against an empty in-memory 01.00.13 FactStore: branch B4, jobs false, software stop false, application keepalive false.
- Passed the original report to `read_probe_report` without a human warning acknowledgement: it refused as expected. No acknowledgement was supplied on anyone's behalf.
- Ran the existing documentation and freeze checks (`tests/test_repo_docs.py`, `tests/test_field_guides.py`, and `tests/arch/test_goliath_link_freeze.py`): **89 passed, 1 skipped**. Relative file links resolve in all five changed documents; both original evidence SHA-256 values and the protected driver tree remain unchanged.

These focused checks substantiate the findings; no new firmware implementation or automated regression suite is delivered by this review. The observer and the listed negative tests still need implementation.

## Alternatives and decision

| Option | Benefit | Limitation | Decision |
| --- | --- | --- | --- |
| Rerun the unchanged full probe with longer timeouts | Reuses existing tool. | Still sends commands not verified on this firmware; longer windows do not correct the known cage gate or sanity format. | Reject for the next test. |
| Adapt the full probe/client immediately | Could collect more command responses. | Requires firmware policy, transaction parsing, importer changes, evidence for handler safety, and a reviewed freeze change. | Separate follow-up after this review. |
| Add a bounded passive observer | Adds no command-handler exposure and can reveal unsolicited tower traffic. | Does not establish active-command support or motion safety. | Proposed next diagnostic after implementation and offline acceptance. |
| Flash the preserved 01.02.00 image | Could align some protocol behavior. | Changes the machine before compatibility/recovery is established; backup, bootloader, tower compatibility, and rollback are unresolved. | Reject as a compatibility workaround. |

Before any later motion test, separately establish the cage procedure, physical stop behavior, upload/start/abort semantics, Z semantics, tower pairing and telemetry, and measured geometry for this firmware. A diagnostic success or human acknowledgement of the current three warnings cannot open B4 job execution.
