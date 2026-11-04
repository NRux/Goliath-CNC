# 01 — Transport and framing

*Source: `GoliathCommLibTCP` (SocketManager.cs) and `Slingshot.exe` (CommManager.cs,
GoliathStatus.cs), Slingshot 1.9.3.0. Verified: 26/26 transport facts confirmed.*

## Transport

- **TCP.** The PC is the **client**; the robot is the **server**.
- **Endpoint:** `10.0.0.1:80` when connected to the robot's own Wi-Fi access point;
  the robot's DHCP-assigned IP (port still **80**) when it has joined your network.
  Port 80 is fixed in code; only the address varies.
- The app opens **three** TCP sockets to the same address (internally `S1`/`S2`/`S3`).
  **All outbound commands go on `S1` only.** The other two only receive. A
  replacement can use a **single socket** — nothing requires three; the extra two
  appear to be vestigial. (Historically the robot used two position "references";
  the three-socket design was never exercised for sending.)
- Each client socket is **bound to the Wi-Fi adapter's local IPv4** with an
  ephemeral port before connecting. On a normal single-NIC PC this is irrelevant;
  it matters only if the host has several interfaces.
- **No socket timeout, keep-alive, or `TCP_NODELAY` is set.** There is **no
  automatic reconnect** inside the socket layer — the application re-initiates a
  connection when needed. Build your own reconnect/timeout policy.
- The receive buffer is sized once at connect to the OS default
  (`ReceiveBufferSize`, typically 8192). That is the max bytes per read, not a
  message boundary.

## Text message framing

**Outbound (PC → robot):**
- A command is a text string built as `string.Format(InvariantCulture, template,
  args)` — e.g. template `SET PANEL SIZE;{0};{1}` → `SET PANEL SIZE;1200;800`.
- Encoded as **UTF-8**. (The older raw-socket library used ASCII; see
  [07-version-compatibility.md](07-version-compatibility.md). Stick to ASCII bytes
  and the two are identical.)
- **No terminator is appended by the host.** The command string is written as-is.
  Observed command templates contain no trailing `\n`.
- **Why that works, and the catch: the robot's network side.** The firmware
  strings identify the Wi-Fi chip as an **Atmel/Microchip WINC1500** (`m2m_wifi_*`
  driver API, `nmi spi` logs), attached to the STM32 over **SPI**. The WINC1500
  runs the TCP/IP stack and hands the STM32 one receive buffer per TCP segment,
  which strongly suggests the command parser treats **each received segment as
  one command**. The catch: if two commands are written back-to-back, TCP (and
  Nagle's algorithm in particular) may coalesce them into one segment, which the
  robot would see as one garbled command. The vendor app avoided this by sleeping
  250–500 ms between commands. A replacement should **disable Nagle
  (`TCP_NODELAY`) and pace commands** (≈0.3 s). Still to confirm on hardware:
  whether a trailing `\n`/`\r\n` is tolerated (the `goliath_link.probe` tool
  tests all three variants).

**Inbound (robot → PC):**
- Decoded as **ASCII**; NUL (`\0`) bytes are stripped and discarded.
- Text responses begin with `INFO`, `ERR`, or `WARN`. These are delimited by
  **newline `\n`**: the receive buffer is cut at each `\n` (inclusive) and each
  piece dispatched. The app also trims a leading stray character and trailing `\n`
  before parsing.
- A text response is parsed by **splitting on `;`**. The **first two fields** form
  the lookup key: `INFO;<n>`, `ERR;<n>`, `WARN;<n>` (e.g. `INFO;8`). Fields from
  index 2 on are the parameters. See [02-message-reference.md](02-message-reference.md).
- **Important:** `<n>` is the message's **wire code**, which is *not* the same as
  the enum position in the decompiled source. Always use the wire codes in the
  message reference, not an inferred ordinal.

## Binary telemetry frames

During work (and status polls) the robot also pushes **three binary frames**,
detected by **sentinel bytes** rather than newlines. The socket reader recognises
them by their first byte and length and converts each into a synthetic
`INFO;1000/1001/1002` message. A replacement reading the raw stream must detect
these by sentinel + length, because they are **not newline-terminated** and will
otherwise corrupt text-line parsing.

Multi-byte integers/floats are **little-endian** except where noted. The two
"status+battery" bytes use a specific bit order (see note).

### Frame 1 — position (`SMART_INFO_POS`, id 1000)
- **Sentinels:** starts with `{` (0x7B), ends with `}` (0x7D).
- **Lengths:** **58 bytes** (full) or **22 bytes** (short, no temperature/battery).
- Layout (byte offsets from the `{`):

  | Offset | Type | Field |
  |---|---|---|
  | 1 | float32 LE | X (mm) |
  | 5 | float32 LE | Y (mm) |
  | 9 | float32 LE | Z (mm) |
  | 13 | float32 LE | angle (C, degrees) |
  | 17 | int32 LE | current G-code line number |
  | 21 | int32 LE | inside temperature *(58-byte frame only)* |
  | 25 | byte | Tower 1 status+battery *(58-byte only)* — see bit note |
  | 26 | byte | Tower 2 status+battery *(58-byte only)* |

- Text-equivalent form (also seen): `INFO;1000;x;y;z;angle;line;temp;batt1;batt2`.

### Frame 2 — tower status (`SMART_INFO_TWR`, id 1001)
- **Sentinels:** starts with `<` (0x3C), ends with `>` (0x3E). **4 bytes.**
- Byte 1 = tower id: **0xAA = T1**, **0xBB = T2**. Byte 2 = status+battery (bit note).
- Text-equivalent: `INFO;1001;<T1|T2>;status;battery`.

### Frame 3 — global status (`SMART_INFO_GLOBAL_STATUS`, id 1002)
- **Sentinels:** starts with `G` (0x47), ends with `S` (0x53). **19 bytes.**
- **Known fields:** the two **tower firmware versions** only — t1 from bytes
  `[14,13,12]`, t2 from bytes `[17,16,15]` (major, minor, patch).
- **Bytes 1–11 are undecoded — by anyone, including the vendor.** The protocol
  table names a text form with ten status fields
  (`INFO;1002;pairing;t1_paired;t2_paired;pd_status;panel_detected;gcode_uploaded;work_status;conn_network;resume_after_reset;encoder_calibrated;t1_fw;t2_fw`),
  but the vendor app's decoder reads **only** the tower firmware bytes and
  **hard-codes every other field to 0/false** when it builds that text. So the
  field *names* are known, their byte positions are not, and Slingshot itself
  never used them. To learn the layout, capture this frame in different machine
  states (towers off/on, before/after panel detection, during work) and compare
  bytes 1–11.

### Status + battery byte (bit order) — verified correction
For the tower status/battery byte, the app **reverses the bits** of the byte and
then reads it MSB-first. The net effect, expressed on the **original** byte:
- **status** = the byte's **two most-significant bits** (`byte >> 6`), values map to
  `OK / CHARGING / CHARGED / SHUTDOWN_RISK` (see `eTOWER_STATUS`).
- **battery %** = the **low six bits** (`byte & 0x3F`) **× 1.5625**.

> An earlier analysis pass got this backwards (status in the low bits). The
> verified reading above is status = high 2 bits, battery = low 6 bits × 1.5625.
> Confirm against a real tower's reported percentage.

## Caveats

- The "no terminator on send" and exact inbound command framing on the **robot
  side** are inferred from the host code; confirm with a packet capture on a live
  machine.
- One source literal mislabels the global-status format string as `INFO;1001;...`
  — it is actually id **1002**. Decode by the `G…S` sentinel/length, not the label.
- Sockets `S2`/`S3` carry no outbound traffic and can be ignored by a replacement.
