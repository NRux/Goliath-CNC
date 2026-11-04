# Field guide 02: the first probe (read-only, about 15 minutes)

**Goal:** confirm that our protocol specification matches your real robot. The
probe connects to the robot over its Wi-Fi, asks it a handful of *read-only*
questions, and records every byte both ways. Nothing moves, the spindle never
turns, and nothing is written to the machine.

**What it settles:** how the robot separates incoming commands (with no
terminator, `\n`, or `\r\n`), whether it answers the way the spec says, its
firmware version and the towers' firmware versions, and what it sends unprompted.

> The probe itself is new and has only been run against our simulator, never a
> real Goliath. It is read-only by construction (it can only send the queries
> listed below, and refuses anything else), but treat this as the first time
> anything other than the vendor app has spoken to your machine.

## 1. Make the machine safe (2 minutes)

- **Remove the bit.**
- **Put the robot on blocks** so the wheels are off the table and spin freely.
- The towers can stay off; they aren't needed for this.

## 2. Prepare the laptop (5 minutes, once)

You need Python 3.9 or newer and a copy of this repository (`git clone`, or
**Code → Download ZIP** on GitHub).

Linux / macOS:

```bash
cd Goliath-CNC/goliath-link
python3 -m venv .venv
.venv/bin/pip install -e .
```

Windows (PowerShell):

```powershell
cd Goliath-CNC\goliath-link
py -m venv .venv
.venv\Scripts\pip install -e .
```

## 3. Join the robot's Wi-Fi (1–3 minutes)

1. Power on the robot.
2. **Wait.** If the robot was previously set up to join someone's home network,
   its firmware first retries that network, then gives up and starts its **own
   hotspot**. (The firmware literally logs *"max try to connect reached. Switch to
   AP Mode"*.) Allow a minute or two.
3. On the laptop, join the Wi-Fi network named **`Goliath_xxxxxx`** (six hex
   characters; the exact name is on your robot). The password is in the printed
   manual. It is a fixed key shared by all Goliath units; if you've lost the
   manual, ask and we'll provide it privately.
4. Your laptop should get an address like `10.0.0.x`. The robot is `10.0.0.1`.

If you don't see a `Goliath_` network after a few minutes, the robot may still be
on another network. Find its address in your router's device list and use
`--host <that address>` in the next step.

## 4. Run the probe (about 1 minute)

Linux / macOS:

```bash
.venv/bin/python -m goliath_link.probe
```

Windows:

```powershell
.venv\Scripts\python -m goliath_link.probe
```

It prints a summary like this when it finishes:

```
 Firmware:          01.02.00
 Terminator none :  ANSWERED
 ...
 Chosen terminator: none
   INFO                     -> INFO;34;GFW 01.02.00;...
   GET GLOBAL SYS STATUS    -> global_status
```

The only things it ever sends: `INFO`, `GET GLOBAL SYS STATUS`,
`PRINT SYS REPORT`, `GET SPINDLE CAGE STATUS`, `READ INDEX CALIBRATION`,
`READ SPINDLE CALIB`, `GET Z BACKLASH`, `GET Z PLATE OFFSET`, `SEND TCP POS`.

## 5. Read the result

| You see | It means |
|---|---|
| A firmware version and at least one `ANSWERED` | The transport works. Which terminators answered tells us the framing. |
| `could not connect` | The laptop isn't on the robot's network, a firewall blocked it, or the robot isn't in hotspot mode yet. Wait, then retry. |
| Connected but `NONE WORKED` | The robot frames commands differently from the spec. **This is still a valuable result**: the capture file shows exactly what came back. |
| `connection closed during sweep at …` | The robot dropped the link after a particular query. Note which one. |

## 6. Optional: one more run (2 minutes)

If you have time, run the probe a second time **with both towers switched on**
(still no other changes). The 19-byte status frame contains 11 bytes nobody has
decoded yet, not even the vendor app. Comparing the two runs shows which bytes
react to the towers.

## 7. Send the results back

The probe writes two files into `probe-results/`:
- `goliath-probe-<time>.json`: the decoded report;
- `goliath-probe-<time>.capture.txt`: every byte sent and received, timestamped.

They contain no Wi-Fi password, but **they do include your robot's serial
number** (the robot reports it). Share them privately, here in this conversation,
not in a public GitHub issue. The folder is git-ignored so they can't be
committed by accident.
