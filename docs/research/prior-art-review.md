# Goliath CNC replacement software: prior-art review and recommendation

*Research date: 2026-11-03. Status: draft for discussion. Nothing here has been tested on a real machine yet.*

## 0. Update: the owner's answers (2026-11-03)

| Question | Answer | Consequence |
|---|---|---|
| Working machine? | **Yes** | Hardware probing is possible. |
| Slingshot? | **No copy.** No public download link found. The official one sat behind a button on goliathcnc.com, and **that site is now offline** (see below). | Phase 1 as written (capture Slingshot's traffic) is blocked until a copy turns up. The work splits into two parallel tracks; see below. |

**goliathcnc.com is offline. This is not a network-policy problem.**
- `goliathcnc.com`, `www.` and `forum.` return no DNS answer, while other sites
  resolve normally.
- The public registry record (RDAP, Verisign, checked 2026-11-03) shows the domain
  is still registered, until 2027-09-28. Its nameservers are now
  `DNS10/11.PARKPAGE.FOUNDATIONAPI.COM`, which are parking servers.
- The record's "last changed" date is 2026-09-29, one day after the renewal
  anniversary. That may just be the auto-renewal, so the exact day the site went
  dark is unknown.
- Someone is still paying for the domain. That is one more reason to contact the
  liquidator.
- The Wayback Machine could not be queried from the research environment (its
  connections are cut, and the fetch tool refuses it). Archive searches have to be
  done from an ordinary browser.
| Audience | Own machine first, then a **public release** | Robustness and documentation matter. Copyleft fits the goal (see below). |
| Platform | Windows or Linux | — |
| Willing to open the machine / buy tools | Yes | Board photos, and possibly a debug probe later. |
| Licence | Any | **Switched to GPL-3.0-or-later.** That lets us reuse grbl, FluidNC, grblHAL, UGS, CAMotics, inkcut and PathCAM code, and MIT code (cncjs, Kiri:Moto, Deepnest) can still be included. It also means that no one can take a fork closed again, which is exactly what stranded Goliath owners this time. Code under GPL-2.0-only (possibly bCNC; LinuxCNC not checked) and Buildbotics' CERN-OHL-S must stay as separate programs, not be copied in. |

**Revised Phase 0/1: two tracks in parallel.**
- **Track A, find Slingshot:** the official site, the Internet Archive, the box
  contents, support@springa.tech, SCM Group, dealers, other owners.
- **Track B, first contact without Slingshot:**
  - **USB.** The robot has a USB Type-B "firmware update" port, per the Würth
    listing **[S]**, and owners were told to update "com port drivers" **[S]**. It
    is therefore probably a USB-serial port. Listen to it passively.
  - **Wi-Fi.** Join the robot's own access point, take a passive capture, and run a
    connect-only port scan.
  - **Photos** of the boards.

Both tracks are written up step by step in
[`docs/field-guide/01-first-contact.md`](../field-guide/01-first-contact.md), using
the read-only capture tool `tools/serial_listen.py`.

New facts found while looking for Slingshot (all **[S]**):
- The last public Slingshot release named is a v2.0.0.3 beta (announced on X,
  August 2023).
- Firmware updates originally went over USB, and later over Wi-Fi.
- A teardown reports FTDI-based USB updates and an ARM Cortex-M4 in the towers.

## 1. Bottom line

1. **Springa really is winding down.** Business registries list the company as
   "Springa S.r.l. In Liquidazione". About 1,000 machines were reportedly built.
2. **Most of what we need may already exist inside the machine.** Evidence suggests
   the robot's firmware *already interprets G-code* that the Slingshot app uploads over
   Wi-Fi. It also has commands for position reporting, stop/abort and Z calibration. The
   tower sensors run their own firmware and handle positioning. If so, "control" means a
   new **sender that speaks the existing Wi-Fi protocol**. It does not mean a new motion
   controller. That is a much smaller job, and it keeps the part Springa spent years and
   EU grant money solving: closed-loop positioning from the wire towers.
3. **None of the 13 repositories you listed solves a Goliath-specific problem.** They
   solve generic problems for gantry machines: CAM, simulation, G-code senders. Some of
   that is reusable. Four of the 13 are irrelevant, including the one named
   "Goliath CNC Projekt".
4. **Don't build CAD/CAM.** That *is* the wheel. Use existing CAM tools (Fusion 360,
   FreeCAD CAM, Kiri:Moto, Vectric, Estlcam). Write a Goliath **post-processor** for them,
   plus a **Goliath safety checker**. Its job is to answer one question: will this job
   make the robot drive its wheels into a pocket or off the panel? Springa gave exactly
   this risk as its reason for refusing outside G-code.
5. **First step: preservation and observation, not code.** Archive Slingshot installers,
   firmware files and the forum before the domain lapses. Then capture the Slingshot ↔
   robot Wi-Fi traffic with a working machine.

The rest of this document gives the evidence, the verdict on each repository, a phased
plan with decision gates, and the open questions only you can answer.

---

## 2. How to read the citations

Not every source could be read the same way. Each claim is tagged with how it was
obtained:

| Tag | Meaning |
|-----|---------|
| **[R]** | Page fetched and read directly. Quotes are verbatim. |
| **[S]** | **Search-engine excerpt only.** The page itself (goliathcnc.com, forum.goliathcnc.com) is **offline** (see §0), so the wording may be the index's paraphrase. Verify against archived copies before relying on them. |
| **[C]** | Inspected in a local clone of the repository (code, README, LICENSE). |
| **[M]** | Repository metadata from the GitHub search API (stars, license, last push), retrieved 2026-11-03. |

---

## 3. Premise check: is the company really gone?

Mostly yes, with nuances that matter for strategy.

- aziende.it lists the company as "Springa S.r.l. In Liquidazione". Its registry
  status still reads *attiva*: "Springa S.r.l. In Liquidazione risulta attualmente
  un'impresa attiva." **[R]** In Italian registry terms that means "not yet struck
  off". It does not mean "operating".
- reportaziende.it: "ATTENZIONE: IMPRESA IN LIQUIDAZIONE" **[R]**. Neither page gives the
  date liquidation started, or whether it is voluntary or court-ordered.
- Latest financials on aziende.it (2023): revenue €367,529 (down 39.8%), loss €126,979 **[R]**.
  reportaziende.it's auto-generated text claims 2024–2025 revenue collapsed to a few
  thousand euros. That text is internally inconsistent, so treat it as unverified.
- Install base: "Produzione: 1.000 pezzi prodotti sino ad oggi (aggiornamento a giugno 2024)"
  ("Production: 1,000 units to date, as of June 2024") on the crowdfunding page
  (company claim) **[R]**.
- As late as August 2024, SCM Group signed to distribute it worldwide "under the name
  'scm maker'" **[R]**.

**Why this matters:**
- *In liquidation* means a liquidator now controls Springa's assets. Those include the
  Slingshot and firmware source code and the positioning patent. An asset that is
  nearly worthless to a buyer can sometimes be bought cheaply, or released.
- **Asking the liquidator** (reachable through the company's PEC address / Registro
  Imprese) costs one email and could save months. SCM Group, which invested €300,000
  and distributed the machine, is a second party worth asking.
- This is not a reason to wait. It is a cheap option to run in parallel.

---

## 4. What the Goliath actually is

Facts that constrain any software design:

| Topic | Evidence | Confidence |
|---|---|---|
| **Positioning: two wire "tower" sensors** | "Thin measuring wires connect the CNC robot via magnetic connections with sensors that serve to delimit the working area, on the one hand, and monitor Goliath's position, on the other." (Red Dot, 2021) **[R]** · "the Goliath has two 'base sensors' that are tethered to the top of the machine and mounted to the edge of the workpiece" (Hackaday, 2017) **[R]** · forum: "the spring loded wire" **[S]** | High |
| Method / rate | "This machine uses two external position sensors to triangulate its position." (Santini thesis, 2019) **[R]** · "update the position of Goliath relative to the work area 100 times each second" (Develop3D, 2017) **[R]** · "patent-pending sensor system" (CORDIS, 2019) **[R]** | High |
| Wire length, radio, work area | "Length of sensor's wire 4 meters", "2.4GHz RF communication", "Maximum work area 3 x 2.5 m", "Repeatability 0.1mm" (goliathcnc.com/tech-specs) **[S]** | Medium (could not open the page) |
| **Robot controller** | "it is run by a microcontroller with wireless radio transmission" (Red Dot) **[R]** · a forum teardown thread names an **STM32F302-family** Cortex-M4 and notes that the firmware is closed source **[S]** | MCU family: low–medium (one poster) |
| **Robot firmware accepts uploaded G-code** | Firmware 1.2.3 (2022-09-07): "Added a communication message that confirms the successful G-Code Upload, or not." **[S]** · Changelogs mention G01/G02/G03 handling **[S]** | Medium–high |
| **Wi-Fi command interface exists** | Firmware 1.2.2 added Wi-Fi commands to set/read Z backlash and Z offset; a "Send Pos" command reports the robot's position; "stop/abort" returns it to "Panel Detection Complete" **[S]** | Medium |
| Connection modes | Slingshot can connect "directly" (to Goliath's own access point) or "using a shared 2.4g WiFi connection" **[S]** | Medium |
| **Towers have their own firmware** | Staff: "The Goliath firmware is meant to update only the robot, not the sensor. As you already experienced, using it to update the sensors will brick their board." **[S]** | Medium–high |
| **Slingshot refuses outside G-code** | Staff: "at the moment it is not possible to generate the G-Code externally and then import it; there could be problems when processing the data by sending incorrect information to the machine" **[S]** · forum user: "there is certainly custom positioning g-code being sent to the Goliath that's not a standard 'rapid' move" **[S]** | Medium–high |
| Slingshot platform | Windows only. A macOS port was planned but never confirmed. Mac users ran it under Parallels **[S]**. Internet needed to fetch updates **[S]**. Whether it needs a **login, licence check or cloud server** at runtime: **unknown**. | — |
| Control architecture | "The trajectory controller is divided into three parts: the kinematic model, the reference creator and the PID regulator." (Santini, *Implementation of a trajectory control for an omnidirectional wheeled CNC machine: Goliath*, PoliMi 2019) **[R]**. The full PDF is marked "non accessibile" (not publicly accessible). | High (abstract only) |
| Wheels | "nine wheels configured as three sets in a triangular pattern" (Hackaday) **[R]** · "two rollers are always in contact with the panel" (Develop3D) **[R]**. Holonomic: three omni-wheel modules. | High |
| Toolpath ordering is machine-specific | "Goliath carves out pockets or drills large holes after it has cut all the profiles"; pockets start "from one corner on the top and then moves backwards ... to avoid falling into what it has carved" (Core77 FAQ quote, 2017) **[R]** | High that rules exist; the exact rules are unknown |
| Spindle / Z | "A 900w Spindle that maxes out at 27,000 RPM" (Develop3D) **[R]**; "A re-skinned DeWalt 611 router" (Hackaday) **[R]**; tech specs: Z range 100 mm, max cutting depth 35 mm, collet up to 8 mm (1/4") **[S]** | Medium. A Würth retailer listing says 710 W and 58 mm Z **[S]**, so versions may differ. |
| Weight | 22 lb (Core77, 2017) **[R]** · 15 kg (Red Dot, 2021) **[R]** · 25 kg for "Goliath PRO" (CORDIS, 2019) **[R]** | Conflicting. Probably different generations. |

### 4.1 Geometry notes for whoever touches positioning

These are *my* inferences from the facts above. They are not sourced claims.

- Two wire lengths `r1`, `r2` from towers a known distance `d` apart fix a point by
  two-circle intersection: `x = (r1² − r2² + d²) / 2d`, `y = ±√(r1² − x²)`. The `±` is
  resolved by which side of the tower baseline the panel is on. "Panel detection" and
  the forum's "index distance ... calibration value" **[S]** are presumably how `d` and
  the frame get established.
- **Accuracy degrades near the line between the towers.** There `∂y/∂r` blows up, a
  dilution-of-precision effect. Tower placement matters, and any new software should
  warn about bad placement.
- **Two lengths give 2 degrees of freedom. The robot has 3 (x, y, heading).** Heading
  needs something else: an IMU, wheel odometry, or wires attached coaxially with the
  spindle so heading doesn't move the tool point. Hackaday commenter
  mikeselectricstuff raised exactly this: "I can't see how it avoids errors due to
  rotation of the body between the sensor attatch point and the cutter axis" **[R]**.
  **How the real machine does this is unknown.** Inspect it physically before writing
  any firmware.
- 0.1 mm over a 4 m wire is about 25 ppm. That is demanding for a spring-loaded
  string encoder (stretch, sag, temperature). The spec says *repeatability*, not
  *accuracy*, and the forum has a "Problems with accuracy" thread **[S]**. Expect any
  replacement to be judged against that bar.

---

## 5. Where the hard problems are

| Layer | Generic or Goliath-specific? | Already solved by… |
|---|---|---|
| CAD (drawing parts) | Generic | Any CAD tool. Don't build. |
| CAM (toolpaths: profiles, pockets, tabs, depth passes) | Generic | Fusion 360, FreeCAD CAM, Kiri:Moto, Vectric, Estlcam, Carbide Create |
| **Goliath dialect + job rules** (ordering, wheel footprint vs cut regions, work area from tower geometry) | **Goliath-specific** | Only Slingshot (closed, unsupported) |
| **Robot link** (upload, jog, panel detection, start/stop, status) | **Goliath-specific** | Only Slingshot |
| **Closed-loop pose control** (tower sensing → estimator → PID → omni-wheel kinematics) | **Goliath-specific** | Existing Goliath firmware (closed, but on the machine) |
| G-code sender UI | Generic, *if* the machine behaves like a normal controller | cncjs, UGS, gSender, bCNC |
| Simulation / preview | Generic | CAMotics, cncjs visualizer |

The repositories you listed cover the generic rows. The three Goliath-specific rows
are where the actual project lives. Our best leverage is to **keep the existing
firmware for the third row** and build the first two ourselves.

---

## 6. Verdict on each repository you listed

Metadata as of 2026-11-03 **[M]**. Code and licences checked in shallow clones **[C]**.

| # | Repo | What it actually is | Licence | Activity | Verdict |
|---|---|---|---|---|---|
| 1 | [shapeoko/ShapeOko](https://github.com/shapeoko/ShapeOko) | Autodesk Inventor CAD, drawings and build guide for a 2013 gantry machine. No software. | None stated | Last push 2013 | **Skip.** Hardware for a different machine class. |
| 2 | [gnea/grbl](https://github.com/gnea/grbl) | G-code interpreter + stepper motion planner for 8-bit AVR (Arduino Uno). `GRBL_VERSION "1.1h"`, build `20190830` **[C]**. | GPL-3 | Effectively final; successors are grblHAL / FluidNC | **Borrow the protocol only.** Grbl is open-loop (it counts steps) and Cartesian-only. It cannot close a loop on tower measurements or drive omni wheels. Its serial protocol is the de facto standard, so any firmware we ever write should speak it. Then existing senders work for free. |
| 3 | [buildbotics/bbctrl-firmware](https://github.com/buildbotics/bbctrl-firmware) | Complete controller: Vue web UI, Python/Tornado app on a Raspberry Pi, CAMotics path planner, ATxmega real-time step generation **[C]** | CERN-OHL-S v2 (strongly reciprocal) **[C]** | Active (Sep 2026) | **Borrow the architecture if we ever replace the controller (Tier 3).** A Linux single-board computer does planning and UI, and a microcontroller does real-time work. That is the right shape. Its step generation assumes steppers with fixed kinematics, so the real-time half doesn't transfer. |
| 4 | [inkcut/inkcut](https://github.com/inkcut/inkcut) | 2D plotter / vinyl-cutter app (Python/Qt) | GPL-3 | Active (Oct 2026) | **Skip.** No tool-radius compensation, pocketing or depth passes. It is the wrong tool for a router. |
| 5 | [LinuxCNC/linuxcnc](https://github.com/LinuxCNC/linuxcnc) | Full real-time machine controller, `2.10.0~pre2` **[C]**. Pluggable kinematics (trivkins, corexykins, genhexkins, scarakins…) and HAL with PID/encoder feedback **[C]**. | GPL-2 | Very active | **Tier-3 option only, probably overkill.** It *could* run a Goliath: planner → HAL PID on tower-measured X/Y → a custom omni-mixing component. But it needs a real-time Linux computer plus I/O hardware on or tethered to the robot, and the learning curve is steep. Its kinematics modules assume joint positions are a function of pose. Wheel rotation isn't (odometry depends on the path taken), so the work would live in HAL, not in a kinematics module. |
| 6 | [filipecaixeta/cncwebsim](https://github.com/filipecaixeta/cncwebsim) | Small browser G-code simulator (mill, lathe, 3D printer) | MIT | Last push Jul 2024 | **Optional.** Usable for a quick web preview. The cncjs visualizer or CAMotics are stronger. |
| 7 | [Barmaley13/CNC](https://github.com/Barmaley13/CNC) | SolidWorks files for a DIY gantry, plus furniture designs | None | Last push 2017 | **Skip.** |
| 8 | [CauldronDevelopmentLLC/CAMotics](https://github.com/CauldronDevelopmentLLC/CAMotics) | 3-axis G-code simulator, plus a G-code parser/planner library (`src/gcode`, `planner.cpp`) that Buildbotics embeds **[C]** | GPL-2+ | Active (Mar 2026) | **Use** to simulate material removal and verify jobs before cutting. Its planner is a candidate for Tier 3. |
| 9 | [xenovacivus/PathCAM](https://github.com/xenovacivus/PathCAM) | C#/Mono 2.5D toolpaths from STL/OBJ | GPL-3 | Last code 2023 | **Skip.** Kiri:Moto and FreeCAD do far more. |
| 10 | [boschresearch/CNC_Machining](https://github.com/boschresearch/CNC_Machining) | Vibration **dataset** for ML process monitoring on industrial machining centres | BSD-3 | **Archived** | **Skip.** Not software for controlling anything. |
| 11 | [Jack000/Deepnest](https://github.com/Jack000/Deepnest) | Sheet nesting (Electron + C) | MIT (`main/LICENSE.txt`) **[C]** | Unmaintained; use the [deepnest-next](https://github.com/deepnest-next/deepnest) fork (active Oct 2026) | **Later.** Useful for sheet goods. For a robot that rides on the sheet, nesting must also leave wheel paths supported, which no nester knows about. |
| 12 | [cncjs/cncjs](https://github.com/cncjs/cncjs) | Node.js + web UI sender. Controllers: Grbl, Marlin, Smoothie, TinyG **[C]**. | MIT | Active (v1.11.5, Oct 2026) | **Strong candidate for the UI.** Caveat: controllers are hard-coded classes on a serial transport (`SerialConnection.js`) **[C]**, not plugins. A Goliath driver means a fork or an upstream PR, plus a network transport. |
| 13 | [tilllit/MP-Programmierung](https://github.com/tilllit/MP-Programmierung) | Berlin student project (BHT, winter semester 2021/22) for a **"Goliath-CNC like"** clone. README: "It is used on a Raspberry Pi Zero 2 to control a Goliath-CNC like CNC machine." **[C]** The manual lists a 7.3 kg, 140 W machine with 3 mm bits **[C]**. | **"All Rights Reserved"**, so not legally reusable **[C]** | 2022 | **Skip.** Not the real Goliath. Open-loop steppers on Pi software PWM, only G0/G1, no acceleration, no position feedback. Note `time = time * 1.28  # Adjustment Factor` in the code **[C]**: a fudge factor to make open-loop distances come out right. That is a small illustration of why Goliath needs closed-loop sensing. |

**Licence warning** *(resolved: the repo is now GPL-3.0-or-later, see §0)*: this repository was MIT-licensed. Copying code from grbl, LinuxCNC,
CAMotics, inkcut, PathCAM, FluidNC, UGS or Buildbotics makes the combined work subject
to GPL / CERN-OHL-S terms, so keeping an MIT label would be misleading. Either choose
GPL-3 now, or keep GPL components as separate programs that talk over a process or
network boundary. cncjs, Kiri:Moto, cncwebsim and Deepnest are MIT and can be combined freely.

---

## 7. Relevant prior art not on your list

| Project | Why it matters |
|---|---|
| **[Kiri:Moto](https://github.com/GridSpace/grid-apps)** (MIT, active Oct 2026) **[M]** | Browser-based CAM with CNC support. A candidate "design" front end with a Goliath machine profile, so nothing needs installing on Windows/Mac/tablet. |
| **[FreeCAD CAM](https://github.com/FreeCAD/FreeCAD)** (LGPL-2.1) **[M]** | Full CAM with Python post-processors. Easy to add a Goliath post. |
| Fusion 360 / Vectric / Estlcam / Carbide Create | What owners already use. A forum user offered to write a Fusion post for Goliath "if given the command specs" **[S]**. Demand exists. |
| **[FluidNC](https://github.com/bdring/FluidNC)** (GPL) **[C]** | ESP32 grbl-compatible firmware with kinematics plugins. It includes **`WallPlotter`**, two cords from two fixed anchors **[C]**, which is the *same math* as Goliath's two wire towers. |
| **Maslow 4** (FluidNC fork) | The closest open-source relative. It is a closed-loop machine on encoded belts and includes routines that calibrate anchor positions. Per its forum the fork "has diverged pretty badly" from upstream **[S]**. Reusable ideas for tower calibration. |
| **[grblHAL](https://github.com/grblHAL/core)** (active Nov 2026) **[M]** | 32-bit grbl successor with many MCU drivers. Relevant only if we write new robot firmware. |
| [UGS](https://github.com/winder/Universal-G-Code-Sender) (GPL-3), [gSender](https://github.com/Sienci-Labs/gsender), [bCNC](https://github.com/vlachoudis/bCNC) (GPL-2) **[M]** | Alternative senders. They become usable if a grbl-protocol bridge to Goliath is ever practical (see §9, risk 4). |
| [Clipper2](https://github.com/AngusJohnson/Clipper2) (Boost licence) **[M]** · [OpenCAMLib](https://github.com/aewallin/opencamlib) (LGPL-2.1) **[M]** | Polygon offsetting and boolean operations. Exactly what the "wheel footprint vs already-cut region" checker needs. |
| Rivers, Moyer & Durand, ["Position-Correcting Tools for 2D Digital Fabrication"](https://groups.csail.mit.edu/graphics/positioncorrectingtools/index.htm) (SIGGRAPH 2012), which became Shaper Origin | Academic prior art for a tool that corrects its own position while cutting. It uses camera localization instead of wires. |
| Santini (2019) thesis | Describes **Goliath's actual trajectory controller**. The PDF is restricted. Ask Politecnico di Milano or the supervisor (Prof. Hermes Giberti) for a copy. |

GitHub search for "goliath cnc" finds only the student project and this repository **[M]**.
As far as I can tell, **nobody has published an open replacement**.

---

## 8. Recommended strategy: tiers with decision gates

### Phase 0: preserve and ask (now, no code)
- **Archive privately**, before the domain lapses: every Slingshot installer version,
  robot and tower firmware files, the manual, the support guides ("How to update the
  Goliath Firmware", "How to update the Tower Firmware" **[S]**), and the forum
  threads (release notes, the teardown thread, connection troubleshooting). Do **not**
  redistribute Springa's binaries publicly. They are still copyrighted.
- Email the liquidator about releasing or selling the source, and ask SCM Group too.
- Request the Santini thesis.

### Phase 1: observe the working system (Tier 1 feasibility)
Needs one working Goliath + Slingshot on Windows.
- Packet-capture the Wi-Fi link (Wireshark) for each action: connect, panel detection,
  jog, job upload, start, pause, stop/abort, "Send Pos", Z backlash read. **Avoid
  firmware updates.** A tower was bricked that way **[S]**.
- Identify Slingshot's tech stack. A .NET app decompiles readably (ILSpy). Unity,
  Electron and native code are progressively harder.
- Save the G-code Slingshot generates and list the non-standard commands
  ("custom positioning" moves).
- **Deliverable:** `docs/protocol.md` in this repo.
- **Exit criterion:** we replay a captured upload and the robot runs it.
- **Decision gate:** plaintext or simple framing → Phase 2. Encrypted, signed, or
  dependent on a dead server → evaluate Tier 2.

### Phase 2: the open sender (`goliath-link`)
- Python (or TypeScript) library + CLI: connect, panel detection, jog, upload,
  run/stop, status.
- **Fail loudly.** A lost tower link, a rejected upload or an unknown status must stop
  the job and say why. Never retry silently.
- Then a UI: a cncjs fork with a Goliath controller and network transport, or a small
  web UI.

### Phase 3: external CAM support (`goliath-prep` + post-processors)
- Post-processors for FreeCAD and Fusion 360 (and maybe Kiri:Moto) that emit Slingshot's dialect.
- A safety checker that **refuses** jobs which:
  - leave the work area derived from tower placement;
  - route a wheel footprint over an already-cut pocket or through-cut;
  - violate the ordering rules we observe Slingshot applying.
- Simulate with CAMotics before cutting.

### Tier 2 / Tier 3: only if Phase 1 fails or the hardware dies
- **Tier 2:** custom firmware on the existing board. Requires SWD access and checking
  the STM32 readout-protection level. Also requires understanding the tower RF link.
- **Tier 3:** a new controller, built like Buildbotics:
  - a single-board computer for planning (CAMotics planner or LinuxCNC) and the UI;
  - a microcontroller for the real-time loop: wire-tower decoding, a pose estimator
    (wires + IMU + wheel odometry), PID control as in Santini, and omni-wheel inverse
    kinematics;
  - wire geometry and calibration math borrowed from FluidNC `WallPlotter` / Maslow 4;
  - the grbl protocol spoken upward, so standard senders work.
- This **re-does a funded R&D programme**. Expect many months and serious safety work.
  An autonomous 900 W router that loses track of where it is can drive off the panel
  with the bit spinning.

---

## 9. Assumptions I'm challenging

1. **"The company is gone, so the software is gone."** Not necessarily. Slingshot and the
   firmware probably still run locally today. The real risks are different: losing the
   installers and firmware files, any hidden server dependency (unknown), Windows
   compatibility drift, and no bug fixes. If Slingshot still works, the minimum
   viable project is *preservation + an open sender*, not a rewrite.
2. **"We need new software to *design* the cuts."** Design and CAM are the most
   expensive and least differentiated things you could build. Rebuilding them is the
   reinvented wheel. The Goliath-specific value is the link and the safety rules.
3. **"The listed repos are the wheel."** Mostly they're wheels for a different vehicle.
   They assume a fixed frame and open-loop steppers. Goliath's hardest problem,
   closed-loop localization on the workpiece, appears in none of them. The most
   valuable existing wheel is the **firmware already inside your machine**.
4. **"Goliath behaves like a normal G-code controller."** Probably not. The evidence
   suggests the job is *uploaded and then executed*, after a panel-detection step, with
   proprietary positioning moves. Every standard sender (cncjs, UGS, gSender) assumes
   *line-by-line streaming* with real-time feed-hold. A grbl-compatible bridge may be
   impossible, or only partly possible (jog/status but not streaming). Find out in Phase 1.
5. **"Springa refused outside G-code for commercial reasons."** Partly, perhaps. But their
   stated reason, bad data reaching the machine, is a real physical risk for a robot
   that rides on the workpiece. Treat it as a requirement (the safety checker), not just
   an excuse.
6. **"Reverse engineering is off-limits"** or **"it's free-for-all now that the company's in
   liquidation."** Neither is safe to assume. Interoperability reverse engineering has
   legal room in the EU and US, within limits, but the copyright and patent still belong
   to someone (now the liquidator). *Not legal advice.* Get a quick opinion before
   publishing protocol details or anything derived from decompiled code.

---

## 10. Questions only you can answer

*(Answered 2026-11-03; see §0.)*

1. **Do you have a working Goliath right now?** Does Slingshot still launch and connect?
   Which Slingshot and firmware versions?
2. **Is this for your machine(s) or for the ~1,000-unit owner community?** That changes
   licence, robustness and UI priorities.
3. Do you have a Windows PC to run Slingshot and Wireshark? Are you willing to open the
   machine? Do you have, or would you buy, an ST-Link/SWD probe?
4. **What do you cut?** 2D profiles and pockets from SVG/DXF, or 3D carving? That decides
   which CAM to target first.
5. **Licence:** keep MIT (so no GPL code gets merged in) or switch to GPL-3?

---

## 11. What I could not verify

- **goliathcnc.com and forum.goliathcnc.com are offline.** They give no DNS
  answer, and the domain now points at parking nameservers (see §0). An earlier
  draft of this document guessed this was the environment's network policy. That
  was wrong: the environment has full network access. Everything tagged **[S]**
  comes from search-engine excerpts of pages that no longer exist. Archived copies
  may still exist on the Wayback Machine, which must be searched from a normal
  browser.
- **Kickstarter** returned HTTP 403. **CTE Magazine** returned 404, and **Woodworking
  Network** returned 403.
- The **Santini thesis** PDF is restricted; only its abstract was read.
- The **STM32F302** identification comes from one forum poster.
- No source states how **heading** is measured.

---

## 12. Sources

**Company and status**
- aziende.it, *Springa S.r.l. In Liquidazione*: https://www.aziende.it/springa-s-r-l **[R]**
- reportaziende.it: https://www.reportaziende.it/springa_srl_mi **[R]**
- CrowdFundMe, Springa campaign: https://www.crowdfundme.it/projects/springa/ **[R]**
- SCM Group, distribution agreement (7 Aug 2024): https://www.scmgroup.com/en_US/scmwood/news-events/news/springa-goliath.n213641.html **[R]**
- CORDIS, project 868599 "GOLIATH PRO": https://cordis.europa.eu/project/id/868599 **[R]**

**Technical**
- Santini, D. (2019), *Implementation of a trajectory control for an omnidirectional wheeled CNC machine: Goliath*, Politecnico di Milano: https://www.politesi.polimi.it/handle/10589/146611 **[R, abstract]**
- Red Dot, *Goliath by Springa*: https://red-dot.org/project/goliath-by-springa-51992 **[R]**
- Hackaday, *CNC Robot Makes a Move* (2017): https://hackaday.com/2017/10/12/cnc-robot-makes-a-move **[R]**
- Develop3D (2017): https://develop3d.com/develop3d-blog/amazing-automation-from-goliath-redesigns-the-workshop-cnc/ **[R]**
- Core77 (2017): https://www.core77.com/posts/69479/Holy-CRAP-Company-Invents-CNC-Mill-Thats-Essentially-an-Autonomous-Router **[R]**
- Goliath tech specs: https://www.goliathcnc.com/tech-specs **[S]**
- Goliath software page: https://goliathcnc.com/product/software/ **[S]**
- Würth LAC listing: https://wurthlac.com/product/3453/Goliath-CNC-Portable-Router **[S]**

**Goliath forum (all [S])**
- Firmware release notes 1.2.3: https://forum.goliathcnc.com/t/goliath-firmware-release-notes-1-2-3/360
- FR – Allow G-Code Upload: https://forum.goliathcnc.com/t/fr-allow-g-code-upload/613
- FR – Post processor for Vectric/Fusion: https://forum.goliathcnc.com/t/fr-post-processor-for-vectric-aspire-and-or-fusion-360/443
- Tower bricked during firmware update: https://forum.goliathcnc.com/t/tower-bricked-during-firmware-update/363
- Goliath Tear-Down Project: https://forum.goliathcnc.com/t/goliath-tear-down-project/1064
- Track position error: https://forum.goliathcnc.com/t/track-position-error/475
- Problems with accuracy: https://forum.goliathcnc.com/t/problems-with-accuracy/664
- Slingshot not seeing Goliath: https://forum.goliathcnc.com/t/slingshot-not-seeing-goliath/117
- Slingshot for macOS: https://forum.goliathcnc.com/t/when-will-slingshot-be-available-for-macos/56
- Firmware update guides: https://goliathcnc.com/support/guide/howtoupdatethefirmware/ · https://goliathcnc.com/support/guide/how-to-update-the-tower-firmware/
- Slingshot download button: https://goliathcnc.com/welcome **[S]**
- Slingshot v2.0.0.3 beta announcement: https://x.com/goliathcnc **[S]**
- Tower firmware / USB update history: https://goliathcnc.com/community/news/tower-sensors-and-firmware-update/ **[S]**

**Prior art**
- Rivers, Moyer & Durand (2012): https://groups.csail.mit.edu/graphics/positioncorrectingtools/index.htm
- Maslow forum threads on the FluidNC fork (the "diverged" quote comes from one of these; the search excerpt didn't say which): https://forums.maslowcnc.com/t/arduino-api-questions/23346 · https://forums.maslowcnc.com/t/maslow-4-the-next-generation-of-maslow/18773 **[S]**
- Repository links are in §6 and §7.
