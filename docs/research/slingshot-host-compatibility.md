<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Slingshot host compatibility with firmware 01.00.13

Research date: 2026-11-06. Baseline: latest main at investigation start,
d42c5c21e15674d6f14827030059f95e61c4e4ca.

Static analysis only. The application, its assemblies, and the robot were not executed or contacted. ILSpy 9.1.0.7988 output was inspected locally. This note describes interface facts and host control flow in original prose; it is not recovered vendor source. Unless another version or assembly is named, source references below identify ILSpy output relative to `.local-research/decompiled/1.9.3.0/Slingshot/`. References beginning `GoliathCommLibTCP/` instead start at the version directory. Lines are decompiler-output anchors, not original source lines.

Primary target is Slingshot 1.9.3.0, `app/Slingshot.exe` SHA-256 `bc7b8b2c2037bd1f5167436fb5734b91384217a2c1413b333bdf6e77a3dfdcb5`. Its TCP library SHA-256 is `8a7b4bed23267c8d259ddae01d34d61c63887a9eea62fd28ee6c4126ab8f0f20`. Findings are host behavior or source-derived predictions. They do not establish what GFW 01.00.13 implements.

## Evidence and present project state

The [reverse-engineering report](slingshot-reverse-engineering.md) records tool setup, reproduction, retained local evidence and the investigation's wider scope. The [artifact manifest](slingshot-artifact-manifest.json) identifies the installer and extracted bytes. Raw vendor source and full CIL output remain local and are not included in this report.

REA evidence `ev_d36ba90333f2e1740ea42b1574d3b85ebfadb3400e005a4cce04239fe7dd4897`, retained as `1.9.3.0-firmware-cage-members.json`, independently identifies the same Slingshot hash above, module MVID `d707a06b-712c-496c-af6b-33fe50733522`, and 141 methods across these selected types:

- `GoliathWPF.Helpers.GoliathStatus`
- `GoliathWPF.SendingGCode`
- `GoliathWPF.SetupStep080`

The selected metadata/CIL coverage is complete with no reported coverage issues. This means those selected declarations and file-backed bodies were decoded; it does not establish runtime behavior or complete application coverage. Build-local method tokens corroborating the central H1/H2 traces are:

| Type | Method | Token |
|---|---|---|
| `GoliathWPF.Helpers.GoliathStatus` | `ReadVersion` | `0x060005cf` |
| Same | `CheckCageStatusEnabled` | `0x0600062b` |
| Same | `StartCageStatusPolling` | `0x0600062c` |
| Same | `StopCageStatusPolling` | `0x0600062d` |
| `GoliathWPF.SendingGCode` | `PollCageTimer_Tick` | `0x060000ad` |

Tokens must be interpreted with that artifact hash and MVID. The broader H3–H6 traces use the named ILSpy output; the selected REA result does not cover every class mentioned below.

Main at `d42c5c21` already contains the B4 probe stop from PR #25. The previous compatibility review's C1 recommendation is therefore **implemented**, not outstanding: once old, unknown or contradictory firmware is detected, the probe refuses further commands. This report preserves that stop and does not authorize or implement a hardware test, library policy change or firmware update.

## H1: The build-date banner changes the vendor's parsed firmware version

`GoliathWPF.Helpers/GoliathStatus.cs:871`, `ReadVersion`, takes the entire suffix after `GFW` and removes everything except decimal digits and dots. It does not isolate the first version token. `AddMessage(INFO)` at line 650 passes the first INFO parameter unchanged (lines 683–701). In `GoliathCommLibTCP/GoliathCommLib.Helpers/INFO.cs:53`, `Params` is just a semicolon split; `GoliathCommLibTCP/GoliathCommLib/SocketManager.cs:1003`, `ResponseObject(string)`, retains the full message.

For the observed first parameter `GFW 01.00.13 +b` followed by two control bytes and `Jul  2 2021`, these transformations produce **1.0.1322021**. This was independently reproduced with PowerShell's standard Regex and Version types, without loading the vendor assembly. The input used actual control bytes 0x01 and 0x02, not their printed escape characters. The outer tag check is case-sensitive even though the subsequent index search is case-insensitive: the captured uppercase `GFW` is accepted, while a wholly lowercase tag would take the fallback path. That derived version is above 1.0.14, but below 1.2.0. Thus the nominal cage/SSID threshold in the existing protocol document is not a reliable prediction for this exact banner: the newest Slingshot can choose the newer one-shot cage command and explicit SSID query even though the true version is 01.00.13. This is a static prediction conditional on the complete identity reaching that method; no running Slingshot observation was made.

The same whole-suffix filtering occurs in 1.7.12.0.0 and 1.8.0.0 `GoliathStatus.cs:703/722`, 1.09.05 at `:870/889`, and 1.9.1.0 at `:832/851`. Preserve a strict first-version-token parser in the replacement. Do not reproduce this vendor behavior.

## H2: Nominal cage support and polling differ from safety proof

In 1.9.3.0, `GoliathStatus.CheckCageStatusEnabled` (`:1101`) enables checks on parsed versions >=01.00.10 unless a debug bypass is enabled. `GoliathWPF/SendingGCode.cs:314` and `GoliathWPF/SetupStep080.cs:137`, `CheckFWversion`, select one-shot `GET SPINDLE CAGE STATUS` at >=01.00.14. Older parsed versions use `POLL CAGE` and `STOP POLL CAGE` (wire mappings in TCP `SocketManager.cs:353`). H1 means the captured banner can select the newer branch.

`StartCageStatusPolling` (`GoliathStatus.cs:1117`) sets a singleton Boolean before calling send, ignores the Boolean send result, and returns success unless an exception is thrown. `StopCageStatusPolling` (`:1136`) clears that Boolean, similarly ignores send success, and waits 1000 ms. These success returns establish neither delivery nor an acknowledgement. `CommManager.Disconnect` (`GoliathWPF.Helpers/CommManager.cs:198`) only closes the three socket objects; it does not invoke the cage-stop helper. No disconnect-based proof that robot polling stops follows from this host code.

The initial cage state is UNDEFINED (`GoliathStatus.cs:97`). `SendingGCode.Window_ContentRendered` (`:149`) starts a 500 ms timer after requesting status/polling. `PollCageTimer_Tick` (`:185`) blocks only the explicit OPEN state; any other state, including UNDEFINED or a retained CLOSED state, can proceed to `StartWorker` (with the stop helper first in the legacy branch). `SetupStep080.Setup_Next_Click` (`:68`) likewise only enters its protective path when the current state is OPEN. The replacement must require a fresh, explicit CLOSED observation before any later operation that relies on the cage. A timer expiring, ignored send result, or unknown status cannot satisfy that condition.

## H3: Connection establishment itself is passive at the transport layer

`CommManager.Connect` (`CommManager.cs:128`) opens S1, waits 250 ms, opens S2, waits 250 ms, opens S3, then waits 500 ms. Each enables listening. `Send` (`:233`) writes through S1. `Connected` (`:49`) reports S1 alone, although the end-of-connect check tests all three. TCP `SocketManager.Connect` (`:472`) delegates to `Connection` (`:507`), which opens TCP and begins reads without an application login, identity request, or keep-alive write in that method.

This is not proof that the complete UI is passive. `Setup022FU.Setup_Next_Click` (`GoliathWPF/Setup022FU.cs:76`) sends global status without a firmware gate. `Editor.PanelDetectionCompleted` (`GoliathWPF/Editor.cs:1159`) sends panel dimensions, requests position, then system report and global status, with delays and no old-firmware gate. `Setup021FU.BtnRefresh_Click` (around `GoliathWPF/Setup021FU.cs:304–347`) uses explicit SSID query for newer parsed versions and reconnection plus `RESET NET KEY` for older ones. Neither this provisioning flow nor panel detection belongs in a passive observer.

## H4: Upload success means transport completion on older parsed versions

`SendingGCode.StartWorker` (`:219`) skips `GCODE CONTINUE;0` below 01.02.00; otherwise it sends that pre-command and waits 250 ms after successful send. It then sends `GCODE;<byte-count>`, waits 250 ms and starts the worker. `Worker_DoWork` (`:255`) initially marks `sent=true`, sends blocks and clears that flag on a false send result. `Manager_DataReceived` (`:111`) can clear it on an upload error and closes on INFO;8.

The shipped `Slingshot.ini:50–51` sets 1024-byte blocks and 75 ms pack interval. The worker also waits 50 ms between block calls. `Worker_RunWorkerCompleted` (`:297`) sleeps the configured 1000 ms (`Slingshot.ini:72`) and closes below 01.02.03 without requiring INFO;8. `Editor.BtnSendToGoliath_Click` (`Editor.cs:666`) subsequently advances when `SendingGCode.Sent` is true. Therefore local writes plus a timer must remain an **unconfirmed upload**, not robot acceptance, in the replacement.

## H5: Abort and keep-alive are narrower than a generic session feature

`GoliathWPF.Templates/ucProductionDashboard.cs:176`, constructor, enables Abort only when parsed version >1.02.00; otherwise it hides/disables it. `BtnAbort_Click` (`:587`) sends ABORT WORK after user confirmation and sets an optimistic local flag without checking send success. For both actual 01.00.13 and the malformed derived 1.0.1322021, this UI gate remains closed. It supplies no stop command verified for this robot.

`Editor.BtnSendToGoliath_Click` (`:666–677`) requests restart rather than a second upload when the local uploaded flag is already set and parsed version <=01.02.00. `Production.WorkEnded` (`GoliathWPF/Production.cs:328`) starts the 60-second global-status timer only **after work completion**, for >01.02.00 or production-debug mode; otherwise it asks for restart. `KeepAliveDispatcherTimer_Tick` (`:718`) sends global status. Existing documentation should not imply this timer begins on connection or runs universally during a job.

## H6: Wi-Fi updater is not a compatibility/recovery validator

`GoliathWPF.Firmware/FirmwareUpdate.cs:701`, `ReadFileData`, accepts any readable nonempty line list. In that method there is no Intel HEX checksum/address parsing, hardware/tower compatibility validation, signature validation, or extraction of the image's true version. `BtnFWLoad_Click` (`:413`) copies the selected file to `Firmware/Latest.hex`; displayed version is maintained separately from online metadata. `ShowProcedureStep` (around `:265–312`) warns on equal/older selected metadata, but that is not proof that the bytes match it.

`BtnWriteStart_Click` (`:545`) sends LOAD with the line count. `Manager_DataReceived` (`:612`) feeds lines on the bootloader's readiness/line acknowledgements; completion UI is driven by broad textual matches and later identity. No backup/readback/rollback sequence was found in this inspected Wi-Fi updater class. This bounded absence does not describe the separate native firmware tool or prove the robot lacks readback. It leaves recovery and old-image preservation unresolved, so the host updater supplies no reason to flash this 01.00.13 machine now.

## Consequences for the next safe test

Retain the latest main probe's B4 stop. Implement the proposed distinct receive-only TCP observer without constructing Slingshot-style status/provisioning/job controllers. Treat identity, tower events and cage state as observations with provenance and freshness; do not import host assumptions as robot capabilities. Keep cage polling, network reset, job upload, motion, software abort, and firmware flashing outside the next diagnostic. H1–H6 support software changes and research priorities; none demonstrates safe handler effects on firmware 01.00.13.
