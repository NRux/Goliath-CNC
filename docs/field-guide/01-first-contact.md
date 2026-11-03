# Field guide 01: first contact with a Goliath (no Slingshot needed)

**Goal:** find out what interfaces the robot exposes and what it says on them,
*without changing anything on the machine.* Everything here is read-only.

**Time:** about 1–2 hours. **You need:**
- a Windows or Linux laptop with Wi-Fi;
- a USB A-to-B cable (printer-style), since the robot has a USB Type-B port that
  was used for firmware updates;
- Python 3.9+, [Wireshark](https://www.wireshark.org/) and [nmap](https://nmap.org/);
- a phone camera.

Background on why these steps matter: [`docs/research/prior-art-review.md`](../research/prior-art-review.md).

---

## Ground rules: read these first

1. **Read only.** None of these steps sends commands to the robot. The capture tool,
   `tools/serial_listen.py`, has no code path that transmits. The `nmap` command
   below only opens and closes TCP connections.
2. **Make the machine safe before plugging anything in.**
   - Remove the bit.
   - Put the robot on blocks so the wheels are off the table and can spin freely.
   - Keep hands clear of the spindle.
   - Opening a serial port can reboot some boards, and an unknown device can do
     unexpected things.
3. **No firmware updates. Not from Slingshot, not from anything else.**
   - Springa's update servers are probably gone.
   - An interrupted update has no backup.
   - Owners bricked tower sensors this way before.
4. **Never run "unprotect", "erase", "mass erase", "write option bytes" or "readout
   unprotect" on any chip.**
   - The robot reportedly uses an STM32 microcontroller.
   - On STM32 chips, removing read protection **erases the whole firmware**, and
     there is no copy to restore it from.
5. **Write everything down**, including "nothing happened". Silence is a result.

---

## Step 0: try to get Slingshot (do this in parallel)

Slingshot is Goliath's Windows app. If you can get it, it lets you use the machine
today, and it is the fastest way to learn the protocol (see Field guide 02, to
come). Try these in your own browser:

1. **The official site is gone.** As of 2026-11-03, goliathcnc.com and its forum
   no longer resolve, and the domain is parked. Go straight to the archive.
2. **Internet Archive, from your own browser:**
   - Open https://web.archive.org/web/2025*/goliathcnc.com/welcome, pick the latest
     snapshot in the calendar, and click its "DOWNLOAD SLINGSHOT" button. If that
     snapshot is empty, try `2024*` or `2023*` instead of `2025*`.
   - Then open https://web.archive.org/web/*/goliathcnc.com* and type `exe`, `msi`,
     `zip` or `slingshot` into the filter box. That lists every file the archive
     saved from the site.
   - Or open this plain-text list of every saved installer-like file:
     https://web.archive.org/cdx/search/cdx?url=goliathcnc.com&matchType=domain&collapse=urlkey&filter=original:.*(exe|msi|zip).*
   - The installer is only there if the archive's crawler happened to save it. If
     the button points to *another* website (a download server, cloud storage),
     write down that address. It can be searched the same way.
   - While you're there, save the forum's archived threads, starting with:
     https://web.archive.org/web/*/forum.goliathcnc.com*
3. **The box:** check for a USB stick or card, and for the printed manual. The
   manual has the robot's Wi-Fi password, which you'll need in Step 3.
4. **Ask people who may still have it:**
   - support@springa.tech. The company is in liquidation, but the mailbox may
     still be read.
   - SCM Group, which distributed the robot as "scm maker" from 2024.
   - The dealer you bought from.
   - Other owners: forum.goliathcnc.com (if it is still up), @GoliathCNC
     followers on X, and commenters on Goliath YouTube videos.
5. **If an installer comes from anywhere except the official site:**
   - Check Windows' **Properties → Digital Signatures** tab for a valid publisher
     signature.
   - Upload the file to [VirusTotal](https://www.virustotal.com/).
   - Ideally, run it in a virtual machine first.
6. **If you get Slingshot:**
   - Keep the installer file. It is our reference.
   - Note its version number.
   - **Decline any firmware update it offers.**
   - Then stop and wait for Field guide 02, which covers capturing Slingshot's
     traffic before anything else changes.

---

## Step 1: inventory (10 minutes)

Photograph:
- the labels on the robot and on **both** towers (model, serial number, hardware
  revision, firmware version if shown);
- every connector and port;
- the manual page with the Wi-Fi details. Don't post the password publicly.

---

## Step 2: the USB port (passive)

1. Install the capture tool's one dependency:

   ```
   python -m pip install pyserial
   ```

2. Power on the robot, then connect the USB cable to the laptop.
3. List serial ports:

   ```
   python tools/serial_listen.py --list
   ```

   On Windows, also check **Device Manager → Ports (COM & LPT)**, then the device's
   **Properties → Details → Hardware Ids**. On Linux, run `lsusb` and `dmesg | tail`.

   **Record the VID:PID.** It identifies the USB chip:

   | VID:PID | Chip | What it suggests |
   |---|---|---|
   | `0403:6001`, `0403:6015` | FTDI FT232R / FT-X | USB-to-serial bridge. A teardown reported FTDI-based updates on the towers. |
   | `10c4:ea60` | Silicon Labs CP210x | USB-to-serial bridge |
   | `1a86:7523` | WCH CH340 | USB-to-serial bridge |
   | `0483:5740` | STMicro virtual COM port | The microcontroller's own USB |
   | `0483:df11` | STM32 DFU bootloader | The chip is in update mode. Unplug it and do nothing. |

   If no port appears on Windows, it may need the FTDI driver, which owners were
   told to install. Get it only from https://ftdichip.com, and only if the VID is `0403`.

4. Start listening. The tool cycles through 8 common baud rates, 20 seconds each
   (about 3 minutes total):

   ```
   python tools/serial_listen.py --port COM5          # Windows: use your COM number
   python tools/serial_listen.py --port /dev/ttyUSB0  # Linux
   ```

   **During each window, power-cycle the robot**: off, wait 3 seconds, on. Boot
   messages, if any, only appear at power-on. Readable text scrolling past is the
   best possible outcome.

5. Results go into `captures/`: one `.bin` file per baud rate, plus a
   `*-summary.json` file.

---

## Step 3: Wi-Fi (passive first)

1. With the robot powered, join **the robot's own Wi-Fi network** from the laptop,
   using the name and password from the manual.
   - If you don't see such a network, the robot may have been set to join someone
     else's Wi-Fi.
   - **Don't do any resets yet.** Just write down which networks you see.
2. Note the addresses:
   - Windows: `ipconfig`.
   - Linux: `ip addr` and `ip route`.
   - The "default gateway" is probably the robot.
3. Start a **Wireshark** capture on the Wi-Fi interface.
   - Leave it running 5 minutes with the robot idle, then power-cycle the robot
     once.
   - Save it as `captures/goliath-idle.pcapng`.
   - Look for UDP packets the robot broadcasts. They are probably how Slingshot
     finds it.
4. **Port scan.** This sends connection handshakes only, never data:

   ```
   nmap -sT -Pn -p- --max-retries 1 -oN captures/nmap-tcp.txt <robot-ip>
   ```

   Do **not** add `-sV`, `-A` or `-sC`. Those push probe data into open ports, and
   that data could reach the motion controller.
5. For each open port, connect and **listen only**:

   ```
   python tools/serial_listen.py --port socket://<robot-ip>:<port> --seconds 30
   ```

6. If port 80 is open, it is almost certainly a web page, such as the Wi-Fi
   module's settings.
   - Open `http://<robot-ip>/` in a browser and take screenshots.
   - **Don't change any settings.**

---

## Step 4 (optional): look inside

Only do this if you're comfortable working inside electrical equipment.

1. **Unplug the robot from mains power first.** The router runs on mains voltage.
2. Photograph **both sides** of every circuit board, with the chip markings sharp and
   readable. Include:
   - the Wi-Fi module;
   - the USB chip;
   - any small group of 4–6 unpopulated pins. These could be a debug header
     (typically GND, 3V3, SWDIO, SWCLK, NRST).
3. Do the same for one tower if it opens easily.
4. **Don't connect anything to the boards yet.**

---

## What to send back

Share these in this conversation, or on a private branch:
- `captures/` (the `.bin` files and summary JSON);
- `nmap-tcp.txt`;
- the `.pcapng` file;
- photos;
- version numbers.

`captures/` is git-ignored on purpose, so nothing gets published by accident.
Blur Wi-Fi passwords in screenshots.

## What happens next

| You found… | Next step |
|---|---|
| Slingshot | Field guide 02: capture the Slingshot ↔ robot traffic |
| Readable text on USB, or a TCP port that talks | Start mapping the protocol directly from those captures |
| A web page on port 80 | Identify the Wi-Fi module. It may be a standard serial-to-Wi-Fi bridge, which tells us the transport. |
| Silence everywhere | Step 4 photos → identify the chips → plan a **read-only** firmware backup. Only possible if read protection is off; never "unprotect". |
