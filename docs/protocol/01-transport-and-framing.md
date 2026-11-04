# 01 — Transport and framing

*Source: `GoliathCommLibTCP` (SocketManager.cs) and `Slingshot.exe` (CommManager.cs,
GoliathStatus.cs), Slingshot 1.9.3.0. Verified: 26/26 transport facts confirmed.
**Corrected 2026-11-04 against static disassembly of the robot firmware GFW 01.02.00**
(`firmware_app.bin`, the application image from `app/Firmware/Latest.hex`). Where the
firmware and the earlier host-side reading disagree, the firmware wins; each such
change is marked "Earlier revisions said…". Firmware addresses, the method and the
full command table are in [09-firmware-01.02.00.md](09-firmware-01.02.00.md).*

## Transport

- **TCP.** The PC is the **client**; the robot is the **server**.
- **Endpoint:** `10.0.0.1:80` when connected to the robot's own Wi-Fi access point;
  the robot's DHCP-assigned IP (port still **80**) when it has joined your network.
  Port 80 is fixed in code; only the address varies.
- **Three connections, opened in order, are required** (next section). Commands go
  on the first one only.
- Each client socket is **bound to the Wi-Fi adapter's local IPv4** with an
  ephemeral port before connecting. On a normal single-NIC PC this is irrelevant;
  it matters only if the host has several interfaces.
- **The vendor app sets no socket timeout, keep-alive, or `TCP_NODELAY`.** There is
  **no automatic reconnect** inside the socket layer — the application
  re-initiates a connection when needed. Build your own reconnect/timeout policy
  (and see the pacing rule below: a replacement *should* set `TCP_NODELAY`).
- The receive buffer is sized once at connect to the OS default
  (`ReceiveBufferSize`, typically 8192). That is the max bytes per read, not a
  message boundary.

## Three connections and slot routing

> **Correction.** Earlier revisions said the app's three sockets `S1`/`S2`/`S3`
> were "vestigial" and that a replacement could use a **single socket**. The
> firmware shows the opposite: the robot **routes its output by connection slot**,
> and a single-socket client never receives any `INFO`/`ERR`/`WARN` line or any
> position frame.

**What the firmware does (GFW 01.02.00):**

- It keeps a table of **6 client slots**. Each accepted TCP connection takes the
  **first free slot**, so on a fresh robot slot order = connection order. A slot is
  freed when a receive returns ≤ 0 bytes (the peer closed or reset) or when a
  direct reply to that connection fails to send. If all 6 slots are taken, a new
  connection is accepted and then closed at once.
- It **sends by slot number**, not to whoever asked. If the target slot is empty
  the message is **silently dropped** — there is no fallback to another connection.

| Slot | Normally | Receives |
|---|---|---|
| 0 | 1st connection (`S1`) | free-form `printf` text (e.g. `cfg read index : %d`, `spindle distance: %.2f`, `CAGE STATUS: %s` from cage polling); 4-byte tower frames `<id sb>` |
| 1 | 2nd connection (`S2`) | **every** `INFO;…`, `ERR;…` and `WARN;…` line — including `INFO;34` (identity), `INFO;40` (sanity report), `INFO;31` (SSID scan), and the `INFO;0` acknowledgement most commands produce |
| 2 | 3rd connection (`S3`) | position frames (22-byte `{…}` on this firmware) |
| — | the connection that sent the command | a few **direct replies** with an explicit length: the 48-byte `GLOBAL STATUS` blob, `CAGE STATUS: …` (reply to `GET SPINDLE CAGE STATUS`), `xmax=…`/`ymax=…` (reply to `GET PANEL SIZE`), and a few others ([09](09-firmware-01.02.00.md)) |

- The receive side accepts commands from **any** connection (there is no source
  filter), so which connection you *send* on does not matter to the parser. Where
  the reply goes is decided by the table above.
- Connecting or disconnecting does not move the robot or change its work state.
  On connect the firmware may push a short state replay (`INFO;34` and, depending
  on state, `INFO;4`/`INFO;6`/`INFO;8`/`INFO;24`) — like every `INFO`, to slot 1.
  See [03-session-sequence.md](03-session-sequence.md#connecting).

**What the vendor app does** (CommManager `Connect`): open `S1`, sleep 250 ms, open
`S2`, sleep 250 ms, open `S3`, sleep 500 ms; if any of the three is not connected,
disconnect all. It **sends only on `S1`**, and all three receive handlers feed one
event stream. That arrangement puts `S1`/`S2`/`S3` in slots 0/1/2.

**What a replacement must do:**

1. Open three connections in that order, with gaps, and fail loudly if any of them
   does not connect.
2. Send commands on the first connection only.
3. Read all three. Give each connection its **own** stream framer (they are
   independent byte streams), and merge their events into one timeline tagged with
   the connection they arrived on.
4. Treat the slot assignment as an **assumption to report**, not a certainty:
   - If another client (an old Slingshot, a phone app) already holds slots 0/1,
     the robot's replies go to *that* client.
   - How quickly the robot frees a slot after a disconnect is unknown. A connection
     opened too soon after the previous one closed may land in an unexpected slot.
   If replies arrive on an unexpected connection, or not at all, say so in the
   output; do not silently re-map.

## Text message framing

### Outbound (PC → robot)

- A command is a text string built as `string.Format(InvariantCulture, template,
  args)` — e.g. template `SET PANEL SIZE;{0};{1}` → `SET PANEL SIZE;1200;800`.
- Encoded as **UTF-8** by the vendor. (The older raw-socket library used ASCII; see
  [07-version-compatibility.md](07-version-compatibility.md). Stick to ASCII bytes
  and the two are identical.)
- **No terminator is appended by the host**, and the firmware requires none.
- **How the robot parses a command (firmware, confirmed).** The Wi-Fi chip is an
  Atmel/Microchip **WINC1500** on SPI; it runs the TCP/IP stack and hands the
  firmware one receive buffer at a time. For **each receive buffer** the firmware:
  1. copies it and NUL-terminates it;
  2. takes the text up to the first `;` as the command name (the rest are
     arguments);
  3. upper-cases the name (so command names are case-insensitive; arguments are
     not touched);
  4. compares it with **exact `strcmp`** against a fixed table of 94 names
     ([09](09-firmware-01.02.00.md)). There is no prefix or substring matching.

  > **Correction.** Earlier revisions said the per-segment parsing was "strongly
  > suggested" by the WINC1500 and left the terminator question open. The firmware
  > confirms one receive buffer = one command, and that a terminator breaks it:
  > `INFO\n` and `INFO\r\n` do not match `INFO`.

- **Consequences:**
  - **One receive buffer = one command.** If two commands arrive in one buffer
    (TCP coalescing, Nagle), the robot sees one unknown name such as
    `GET SPINDLE CAGE STATUSREAD INDEX CALIBRATION`. Set **`TCP_NODELAY`** and
    **pace** commands (the vendor slept 250–500 ms between commands; ≈0.3 s is a
    sensible minimum). Never write two commands back-to-back.
  - **An unknown name gets no reply.** Statically, the parser returns without
    sending anything, and the receive callback discards its result. The table also
    holds the strings `INVALID COMMAND` and `ERROR`, but nothing dispatches to
    them. This has not yet been observed on hardware, so a client must treat both
    "no reply" and "an error reply" as possible, and must never read silence as
    success.
  - **While a G-code upload is in progress the parser is switched off.** After
    `GCODE UPLOAD;<n>`, every received buffer, from any connection, is stored as
    program data instead of being parsed, until the upload completes. See
    [03-session-sequence.md](03-session-sequence.md).

### Inbound (robot → PC)

> **Correction.** Earlier revisions said inbound text is "delimited by newline
> `\n`" with NUL bytes "stripped and discarded". The firmware shows four distinct
> wire shapes, and two of them have no newline at all.

**The four wire shapes** (GFW 01.02.00):

1. **Padded slot messages — always 512 bytes.** Every message sent to a slot
   (every `INFO`/`ERR`/`WARN` line, every free-form `printf` reply, and every
   position frame) is written into a zeroed 512-byte buffer and the **whole 512
   bytes** are sent: the message, then NUL padding.
   - A text message therefore ends at the **first `\n` or the first NUL**,
     whichever comes first.
   - Most formats end in `\n`, but **some do not**: `cfg read index : %d` and
     `spindle distance: %.2f` (the replies to `READ INDEX CALIBRATION` and
     `READ SPINDLE CALIB`), and several console messages such as
     `Drill length: %.2f mm`. For these the NUL padding is the only terminator.
2. **Tower frames — exactly 4 bytes, not padded** (slot 0). The status byte inside
   may legitimately be `0x00`.
3. **Direct replies to the requester — explicit length, not padded, no
   terminator.** A few commands answer the connection that sent them with exactly
   the bytes of the reply (at most 1024; an empty reply is not sent at all):
   - the **48-byte `GLOBAL STATUS` blob** (reply to `GET GLOBAL SYS STATUS`);
   - `CAGE STATUS: OPEN\n` (18 bytes) or `CAGE STATUS: CLOSED` (19 bytes — the
     firmware formats this into a 20-byte buffer, so the trailing `\n` of `CLOSED`
     is cut off);
   - `xmax=%.4f \n\rymax=%.4f` (reply to `GET PANEL SIZE`), `count: %d`,
     `Moving stepper!`, `\n! Waiting for a hex file with %d lines \r\n`.

   The vendor app relied on read boundaries to separate these.
4. **TCP does not preserve any of this.** A 512-byte block may arrive split over
   several reads, or coalesced with the next block.

**Rules for a replacement's stream framer** (one framer per connection):

- Detect the fixed-length binary forms **first**, by sentinel and length (position
  frames, tower frames, the `GLOBAL STATUS` blob). They contain arbitrary bytes,
  including `0x00`, `0x0A` and sentinel characters.
- Skip runs of NUL bytes **between** messages only — never inside a fixed-length
  frame.
- End a text message at `\n` **or** at the first NUL.
- For an unpadded, unterminated direct reply, flush the pending text when the next
  recognised message start arrives or after a short idle timeout, and **mark it**
  as flushed-without-terminator rather than holding it indefinitely or gluing it to
  the next message.
- Never drop bytes silently: when the framer has to resynchronise, report what it
  discarded.

**Parsing a text line** (unchanged):

- Text responses begin with `INFO`, `ERR`, or `WARN`. The vendor app also trims a
  leading stray character and a trailing `\n` before parsing.
- Some free-form texts begin with `\n\r`, or contain `\n\r` in the middle
  (`xmax=… \n\rymax=…`, the encoder-calibration printout). A framer that ends
  text at `\n` will see these as several lines; match them by prefix accordingly
  and tolerate a leading `\r`.
- A text response is parsed by **splitting on `;`**. The **first two fields** form
  the lookup key: `INFO;<n>`, `ERR;<n>`, `WARN;<n>` (e.g. `INFO;8`). Fields from
  index 2 on are the parameters. See [02-message-reference.md](02-message-reference.md).
- **Important:** `<n>` is the message's **wire code**, which is *not* the same as
  the enum position in the decompiled source. Always use the wire codes in the
  message reference, not an inferred ordinal.

## Binary telemetry frames

The robot pushes **binary frames**, detected by **sentinel bytes and length**
rather than newlines. The vendor's socket reader recognises them by their first
byte and length and converts each into a synthetic `INFO;1000/1001/1002` message
(these ids never appear on the wire). A replacement reading the raw stream must
detect them by sentinel + length, because they are **not newline-terminated** and
will otherwise corrupt text-line parsing.

Multi-byte integers/floats are **little-endian** except where noted. The
"status+battery" bytes use a specific bit order (see note).

### Frame 1 — position (`SMART_INFO_POS`, id 1000)
- **Sentinels:** starts with `{` (0x7B), ends with `}` (0x7D).
- **Lengths:** **22 bytes** (short) or **58 bytes** (full, adds temperature and
  battery). **GFW 01.02.00 sends only the 22-byte form**, to slot 2 (the third
  connection), NUL-padded to 512 bytes like every slot message. The 58-byte form
  is decoded by the vendor app and may exist on other firmware.
- Layout (byte offsets from the `{`):

  | Offset | Type | Field |
  |---|---|---|
  | 1 | float32 LE | X (mm) |
  | 5 | float32 LE | Y (mm) |
  | 9 | float32 LE | Z (mm) |
  | 13 | float32 LE | angle (C, degrees — the firmware converts from radians before sending) |
  | 17 | int32 LE | current G-code line number |
  | 21 | int32 LE | inside temperature *(58-byte frame only)* |
  | 25 | byte | Tower 1 status+battery *(58-byte only)* — see bit note |
  | 26 | byte | Tower 2 status+battery *(58-byte only)* |

- **Ambiguity:** in a 58-byte frame, offset 21 is the low byte of the temperature.
  If that byte is `0x7D` (`}`), the frame's first 22 bytes look like a complete
  short frame. Disambiguate by what follows (NUL padding, or the next sentinel),
  or lock to the length the firmware is known to send.
- Text-equivalent form (also seen): `INFO;1000;x;y;z;angle;line;temp;batt1;batt2`.

### Frame 2 — tower status (`SMART_INFO_TWR`, id 1001)
- **Sentinels:** starts with `<` (0x3C), ends with `>` (0x3E). **Exactly 4 bytes**,
  sent unpadded to slot 0 (the first connection).
- Byte 1 = tower id: **0xAA = T1**, **0xBB = T2** (the only ids GFW 01.02.00
  builds). Byte 2 = status+battery (bit note); it can be `0x00`.
- A 4-byte `<`…`>` window with any other id byte is a mis-framing symptom; report
  it as malformed, never raise out of the reader.
- Text-equivalent: `INFO;1001;<T1|T2>;status;battery`.

### Frame 3 — global status (`SMART_INFO_GLOBAL_STATUS`, id 1002)

> **Correction.** Earlier revisions said `GET GLOBAL SYS STATUS` is answered by a
> 19-byte `G…S` frame. GFW 01.02.00 contains **no** builder for that frame. It
> answers with a **48-byte blob** sent directly to the requesting connection.

**On GFW 01.02.00 — the 48-byte `GLOBAL STATUS` blob:**

```
"GLOBAL STATUS HEAD\0"   19 bytes   (ASCII text + one NUL)
<10 status bytes>        10 bytes
"GLOBAL STATUS TALE\0"   19 bytes
                         -- 48 bytes, explicit length, no padding, no terminator
```

- Detect it by the 19-byte prefix, then wait for all 48 bytes and check the
  suffix. The status bytes may be `0x00`; do not strip NULs inside it.
- It does **not** contain the tower firmware versions, so on this firmware those
  are unavailable from this message.
- Meaning of the 10 bytes: see [02-message-reference.md](02-message-reference.md#global-status).
- The vendor app's frame detector does not recognise this blob (it looks for the
  19-byte form), which is consistent with the vendor hard-coding the status fields
  (below).

**The 19-byte `G…S` frame (decoded by the vendor app; not produced by 01.02.00):**
- **Sentinels:** starts with `G` (0x47), ends with `S` (0x53). **19 bytes.**
- **Known fields:** the two **tower firmware versions** only — t1 from bytes
  `[14,13,12]`, t2 from bytes `[17,16,15]` (major, minor, patch).
- **Bytes 1–11 are undecoded — by anyone, including the vendor.** The vendor app's
  decoder reads **only** the tower firmware bytes and **hard-codes every other
  field to 0/false** when it builds the text form
  (`INFO;1002;pairing;t1_paired;t2_paired;pd_status;panel_detected;gcode_uploaded;work_status;conn_network;resume_after_reset;encoder_calibrated;t1_fw;t2_fw`).
- Keep decoding this form for firmware other than 01.02.00, but do not expect it
  there. A `0x0A` inside this frame is data: wait for 19 bytes after a `G` before
  deciding it is text.

### Status + battery byte (bit order) — verified correction
For the tower status/battery byte, the app **reverses the bits** of the byte and
then reads it MSB-first. The net effect, expressed on the **original** byte:
- **status** = the byte's **two most-significant bits** (`byte >> 6`), values map to
  `OK / CHARGING / CHARGED / SHUTDOWN_RISK` (see `eTOWER_STATUS`).
- **battery %** = the **low six bits** (`byte & 0x3F`) **× 1.5625**. The firmware
  computes the battery value the same way.

> An earlier analysis pass got this backwards (status in the low bits). The
> verified reading above is status = high 2 bits, battery = low 6 bits × 1.5625.
> Confirm against a real tower's reported percentage.

## Caveats

- Everything firmware-side here comes from **static disassembly of GFW 01.02.00**.
  Nothing has yet been observed on a live robot. Later firmware (the vendor app
  gates features on 01.02.02 and 01.02.03) may differ, and no image of it survives.
- The robot's actual firmware version is unknown until it is read (`INFO;34`).
- One source literal mislabels the global-status format string as `INFO;1001;...`
  — it is actually id **1002**.
- *Earlier revisions said "Sockets `S2`/`S3` carry no outbound traffic and can be
  ignored by a replacement." The first half is true (the vendor sends only on
  `S1`); the second half is wrong: `S2` and `S3` carry all `INFO`/`ERR`/`WARN`
  lines and all position frames.*
