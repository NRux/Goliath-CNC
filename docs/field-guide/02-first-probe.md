# Field guide 02: the first probe (read-only, about 15 minutes)

**Goal:** confirm that our protocol specification matches your real robot. The
probe connects to the robot over its Wi-Fi, asks it six read-only questions,
and records every byte both ways.

**What it settles:** the robot's firmware version; whether it routes its replies
to three connections the way our analysis of firmware GFW 01.02.00 says;
whether commands need a terminator (none, `\n` or `\r\n`); how it answers each
of the six questions; what it does with a command it does not know; and what it
sends unprompted.

> The probe has only been run against our simulator, never a real Goliath.
> Treat this as the first time anything other than the vendor app has spoken to
> your machine.

## What the probe sends, and why we believe it is read-only

By default it sends exactly these six commands, and nothing else:

`INFO`, `GET GLOBAL SYS STATUS`, `PRINT SYS REPORT`, `GET SPINDLE CAGE STATUS`,
`READ INDEX CALIBRATION`, `READ SPINDLE CALIB`.

- **Basis:** we disassembled the handler of each of these six in **GFW
  01.02.00**, the only robot firmware image that survives. None of them calls a
  motor, spindle, flash-erase or state-setting routine. One nuance:
  `READ INDEX CALIBRATION` reads through the firmware's normal configuration
  reader, which rewrites its stored copy only if its two internal copies
  disagree. The vendor app triggers the same read routinely, from its main
  toolbar.
- To find the terminator, it also sends `INFO` followed by `\n` and by `\r\n`.
  GFW 01.02.00 compares each received command exactly against its command
  table, so these match no command and are ignored.
- Opening and closing connections does not move the robot or change its work
  state on GFW 01.02.00.
- **If your robot runs newer firmware** (the probe tells you), these six are
  expected to behave the same, but that is **not verified**: no newer firmware
  image survives.
- `--extended` adds `GET Z BACKLASH` and `SEND TCP POS`, and **only** if the
  robot reports firmware newer than 01.02.00 (01.02.00 does not have them). The
  only evidence that they are read-only is that the vendor app used them as
  reads. Leave `--extended` off for the first run.
- `GET Z PLATE OFFSET` is never sent. The vendor app sent it only inside its
  tool-change dialog, and we don't know what it does.

Every send goes through one check in the code that refuses anything else. Even
so, **remove the bit and put the robot on blocks** (step 2).

## 1. Make sure nothing else is connected to the robot

This matters more than anything else in this guide.

The robot sends its replies **by connection slot**, not to whoever asked. If
another program is connected to it, that program holds the slots and receives
the replies, and the probe sees nothing.

- **Close Slingshot** (the vendor app) on every computer that has it, including
  ones in other rooms. Check the system tray too.
- **Close any phone or tablet app** that talks to the robot.
- If anything was connected to the robot since it was last switched on,
  **power-cycle the robot** (off, wait 10 seconds, on) after closing it.

## 2. Make the machine safe (2 minutes)

- **Remove the bit.**
- **Put the robot on blocks** so the wheels are off the table and spin freely.
- The towers can stay off; they aren't needed for this.

## 3. Prepare the laptop (5 minutes, once)

You need Python 3.9 or newer and a copy of this repository (`git clone`, or
**Code → Download ZIP** on GitHub).

Linux / macOS:

```bash
cd Goliath-CNC/goliath-link
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

Windows (PowerShell):

```powershell
cd Goliath-CNC\goliath-link
py -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e .
```

Do this while the laptop still has internet: the install downloads its build
tools. Don't skip the pip upgrade: the pip bundled with older Python 3.9
releases (20.x, e.g. Debian 11's) refuses this install with
`editable mode currently requires a setup.py based build`.

## 4. Join the robot's Wi-Fi (1–3 minutes)

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

## 5. Run the probe (about 1 minute)

Linux / macOS:

```bash
.venv/bin/python -m goliath_link.probe
```

Windows:

```powershell
.venv\Scripts\python -m goliath_link.probe
```

It runs four short sessions (three terminator tests, then the sweep of six
questions), waits two seconds between them, and prints a summary like this:

```
 Goliath probe summary: OK (exit code 0)
 Firmware:          01.02.00  (reply to INFO in the sweep, on S2)
 Tower firmware:    not available: the 48-byte GLOBAL STATUS blob (GFW 01.02.00) carries no tower firmware
 Terminator none :  ANSWERED (identity on S2)
 Terminator lf   :  no answer
 Terminator crlf :  no answer
 Chosen terminator: none
 Sweep (terminator none):
   INFO                     -> S2 INFO;34;GFW 01.02.00   ;...
   GET GLOBAL SYS STATUS    -> S2 INFO;0; S1 status blob [..]
   GET SPINDLE CAGE STATUS  -> S2 INFO;0; S1 CAGE STATUS: CLOSED [no terminator]
   READ INDEX CALIBRATION   -> S1 cfg read index : ...; S2 INFO;0
   ...
```

Useful options (the defaults are fine for the first run):

| Option | Default | What it does |
|---|---|---|
| `--slot-release-wait S` | 2 | seconds between sessions, so the robot can free its connection slots |
| `--pace S` | 2 | minimum seconds between two questions (at least 0.3) |
| `--reply-timeout S` | 5 | how long to wait for each expected reply |
| `--listen S` | 3 | how long to listen after connecting, before asking anything |
| `--terminator none` | auto | skip the terminator test and use one variant |
| `--extended` | off | see "What the probe sends" above |

## 6. What S1, S2 and S3 mean

Each session opens **three connections**, in order, the way the vendor app does.
The probe sends only on the first. On firmware 01.02.00, if nothing else is
connected, the robot answers like this:

| Tag | Connection | What arrives there |
|---|---|---|
| **S1** | 1st | answers to the questions that reply directly (`GLOBAL STATUS` blob, `CAGE STATUS: …`) and free-form text (`cfg read index : …`, `spindle distance: …`); tower frames |
| **S2** | 2nd | every `INFO;…`, `ERR;…` and `WARN;…` line, including the identity line `INFO;34;GFW xx.yy.zz…`, the sanity report `INFO;40` and the `INFO;0` acknowledgements. The robot may also push an `INFO;34` here as soon as you connect |
| **S3** | 3rd | position frames (none expected while the robot is idle) |

The capture file tags every byte with its connection and session, and the JSON
tags every decoded message the same way. If something arrives on the wrong
connection, the probe says so (next step).

## 7. Read the result

**Exit code:** `0` = everything worked; `3` = it finished, but with warnings
(read them); `1` = something failed (read the errors); `130` = you pressed
Ctrl+C (the files are still written, with what was collected so far); `2` = an
invalid option, such as a `--port` outside 1-65535 (nothing was sent). The summary lists every reason.

| You see | It means |
|---|---|
| `OK`, a firmware version, terminator `none`, a reply under every question | Matches our firmware analysis. |
| `could not connect to 10.0.0.1:80 … Is this computer joined to the robot's Goliath_xxxxxx Wi-Fi` | The very first connection failed: the laptop isn't on the robot's network, a firewall blocked it, or the robot isn't in hotspot mode yet. Wait, then retry. |
| `the robot accepted S… and then closed it during the three-connection handshake` | The robot has no free connection slot. Something else is connected (step 1), or the previous session's connections weren't released yet: retry with `--slot-release-wait 5`. |
| `could not open S1 … although … connected earlier` | The robot stopped accepting connections partway through (rebooted, or the wireless link dropped). Not a Wi-Fi join problem. |
| `no terminator variant got an INFO;34 identity reply on any connection` | Most likely another client holds the second slot and got the reply: do step 1 again and power-cycle. If nothing else was connected, the robot frames commands differently from all three variants: still a valuable result, the capture shows exactly what came back. |
| `SLOT ROUTING: … arrived on S1 … instead of S2` | The probe's connections didn't land in the slots it expects: another client is connected, or the robot was slow to free the previous slots. Do step 1, then retry with `--slot-release-wait 5`. |
| `SLOT ROUTING: sweep: nothing at all arrived on S2 …` | Same causes. The direct replies still reached S1, which is why some questions answered. |
| `Terminator lf: no answer; got S2 ERR;1` and a NOTE about it | Probably the robot's answer to a command it did not recognise (firmware 01.02.00 sends nothing in that case, so your firmware may differ). Note it. |
| `Terminator lf: no answer; got S2 ERR;13` (any code other than `ERR;1`, or a `WARN;…`) and a WARNING | **Not** an answer to the test: the robot reported a fault or warning of its own (the warning names it, e.g. `MOTOR_OCD`, `GOLIATH_IN_HALT`, a low tower battery). Check the machine before anything else. |
| `Terminator lf: UNCERTAIN (identity on S2, but it may be the robot's late connect-time push …)` | The robot pushes its identity line when you connect; here it arrived after the probe had already asked, so it cannot be told apart from an answer. Retry with `--listen 10`. |
| `an identity line arrived after the … s reply window` | The robot answered (or pushed) more slowly than the probe waited. Retry with `--reply-timeout 10` (and `--listen 10` if the warning says so). |
| `the robot closed connection S… during the sweep … while waiting for the reply to X` and `not sent: …` | The robot dropped the link after a particular question. Note which one. |
| A question shows `(no reply in … s)` | That question got no answer. If a matching message is listed as `LATE`, it came after the next question was sent: retry with `--pace 4 --reply-timeout 10`. |
| `also in window: …` / `Unattributed` | Messages that arrived during a question but don't look like its answer. They are kept and listed, but not credited to the question. |
| `--extended was given but … not sent` | Your robot runs 01.02.00 (or its version is unknown), which doesn't have those commands. Nothing to do. |

## 8. Optional: one more run (2 minutes)

If you have time, run the probe a second time **with both towers switched on**
(still no other changes). On firmware 01.02.00 the reply to
`GET GLOBAL SYS STATUS` is a 48-byte block holding 10 status bytes whose
meaning nobody has confirmed. Comparing the two runs shows which bytes react to
the towers. (On newer firmware the reply may instead be the 19-byte frame that
carries the towers' firmware versions; the summary then shows them.)

## 9. Send the results back

The probe writes two files into `probe-results/`:
- `goliath-probe-<time>.json`: the decoded report;
- `goliath-probe-<time>.capture.txt`: every byte sent and received, timestamped,
  tagged with its connection.

Both are UTF-8 text. They contain no Wi-Fi password, but **they do include your
robot's serial number** (the robot reports it). Share them privately, here in
this conversation, not in a public GitHub issue. The folder is git-ignored so
they can't be committed by accident.
