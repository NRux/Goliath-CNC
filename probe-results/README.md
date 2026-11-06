# First hardware probe: 2026-11-05

This directory records the first read-only probe of the owner's Goliath CNC robot.

**Scrubbed, at the owner's request (2026-11-06).** The robot's serial number and the names of nearby Wi-Fi networks have been replaced in both files by placeholders of the same length:
- the 24-character serial in the `INFO;34` identity line is now `XXXXXXXXXXXXXXXXXXXXXXXX`;
- each Wi-Fi name in the `INFO;31` scan is now `SSID01`, `SSID02`, and so on, padded with `_` to the original length.

The same substitution is applied to the hex columns of the capture, so every message keeps its length and the robot's 512-byte block framing is unchanged. Everything else is byte for byte as recorded. Earlier commits of these two files (in this repository's history before the scrub) still hold the original values.

## Evidence

| File | SHA-256 |
| --- | --- |
| `goliath-probe-20261105T193222Z.json` (scrubbed) | `40abf92dd9ad1c9a962a3f5d8df307bf357ab8b1d519f1252a8ce43e069987ef` |
| `goliath-probe-20261105T193222Z.capture.txt` (scrubbed) | `c5cb264730004076a414007a67d87221d2401c7fa0620f0526363861205354fa` |

The probe started at 2026-11-05 19:32:22 UTC against `10.0.0.1:80`, with default timing and `--extended` off. All four three-connection sessions connected. The run completed with exit code 3 and three warnings; there were no probe errors or slot-routing errors.

## Observations

| Check | Observed result |
| --- | --- |
| Identity | `INFO` without a terminator returned `INFO;34` on S2. Firmware is **01.00.13**. The LF and CRLF variants did not get identity replies. |
| `GET GLOBAL SYS STATUS` | No received bytes during its five-second reply window. No global-status frame or tower firmware was established. |
| `PRINT SYS REPORT` | A complete 13-line sanity report arrived on S1, from header through `END`; all 11 listed subsystem initialization lines say `OK`. The probe expected an `INFO;40` on S2 and therefore marked this command unanswered. This warning reflects a response-format difference, not silence. |
| `GET SPINDLE CAGE STATUS` | No received bytes during its five-second reply window. The [version guide](../docs/protocol/07-version-compatibility.md) records that the vendor app uses this command only on firmware 01.00.14 and later. |
| `READ INDEX CALIBRATION` | A `cfg read index` value arrived on S1, followed by `INFO;0` on S2. |
| `READ SPINDLE CALIB` | A spindle-distance value arrived on S1, followed by `INFO;0` on S2. |
| Unprompted output | The robot emitted identity and `INFO;31` network-scan messages on S2, on every connect. The scan listed six nearby Wi-Fi networks; their names are replaced by placeholders here. |

The capture confirms that the two silent command windows had no incoming bytes; they were not merely undecoded replies. The direct replies on S1 and `INFO` messages on S2 support the expected connection-slot roles for the commands that did answer. A successful subsystem initialization report does not establish that the towers were paired or that motion is safe.

## Disposition

This is **branch B4: firmware older than 01.02.00**. The project's preserved firmware image and read-only handler analysis cover 01.02.00, not this robot's 01.00.13. These observations are evidence for a design review, not a compatible-command fingerprint or permission for motion, job upload, or firmware update. Keep the hardware recovery and release holds in place. Do not run `--extended` or infer that the 01.02.00 command set applies to this robot.

The probe report is partial, and the R1 importer requires a named person's acknowledgement of every warning before it will accept a warning-bearing report. This record does not claim that acknowledgement or lift the `goliath-link/` freeze check. A firmware-specific review should determine the supported read-only commands and response forms before any further robot interaction. Powered-off measurements from field guide 04 remain independent of this protocol result.
