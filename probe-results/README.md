# First hardware probe: 2026-11-05

This directory records the first read-only probe of the owner's Goliath CNC robot. The two original probe files are committed unchanged, at the owner's request. **They contain the robot's serial number and nearby Wi-Fi network names and are public in this repository.** Do not copy their contents into issues or other reports without considering that exposure.

## Evidence

| File | SHA-256 |
| --- | --- |
| `goliath-probe-20261105T193222Z.json` | `1957430a0b893eb95d725d92cfae9c4f4fe2a652fae251a0bb29b1dbfa581cb4` |
| `goliath-probe-20261105T193222Z.capture.txt` | `ea3773dbfccb350961e2343f6771ebd85230c22b8ff39158c86f5b0f6391737e` |

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
| Unprompted output | The robot emitted identity and `INFO;31` network-scan messages on S2. The latter include nearby Wi-Fi names. |

The capture confirms that the two silent command windows had no incoming bytes; they were not merely undecoded replies. The direct replies on S1 and `INFO` messages on S2 support the expected connection-slot roles for the commands that did answer. A successful subsystem initialization report does not establish that the towers were paired or that motion is safe.

## Disposition

This is **branch B4: firmware older than 01.02.00**. The project's preserved firmware image and read-only handler analysis cover 01.02.00, not this robot's 01.00.13. These observations are evidence for a design review, not a compatible-command fingerprint or permission for motion, job upload, or firmware update. Keep the hardware recovery and release holds in place. Do not run `--extended` or infer that the 01.02.00 command set applies to this robot.

The probe report is partial, and the R1 importer requires a named person's acknowledgement of every warning before it will accept a warning-bearing report. This record does not claim that acknowledgement or lift the `goliath-link/` freeze check. A firmware-specific review should determine the supported read-only commands and response forms before any further robot interaction. Powered-off measurements from field guide 04 remain independent of this protocol result.
