# 04 — Connection and provisioning

*Source: `GoliathWPF` ConnectionManager.cs, WifiManager.cs, CommManager.cs,
Setup019FU–022FU, IndexDistanceSetup010.cs, Slingshot 1.9.3.0. Verified: 34/34
facts confirmed.*

## Two connection modes

The app stores a `ConnectionMode` and branches on it:

- **AP mode** (`ConnectionMode != "Provisioning"`): the PC joins the robot's own
  Wi-Fi hotspot and talks to the fixed IP **`10.0.0.1`**.
  - SSID: **`Goliath_<6 hex>`** (the 6 hex digits come from the robot's MAC; e.g.
    `Goliath_a318c7`). The app lists candidate networks by prefix match
    `Goliath_` / `GOLIATH`.
  - WPA key: the app hard-codes a **single fixed WPA key shared by all units**
    (same value baked into every Slingshot build). The literal value is withheld
    from this public doc and kept in the private archive; it is also recoverable
    from any Slingshot installer. It doubles as the AP password if the printed
    manual is lost. *(A replacement that re-hosts the AP should let the owner set
    their own key rather than reuse this shared one.)*
- **Provisioning mode** (`ConnectionMode == "Provisioning"`): the robot has joined
  your shared Wi-Fi; the PC finds the robot's DHCP address and connects there (port
  80 as always).

The setup wizard offers three choices that reduce to these two: reuse the saved
provisioning network, run a **new** provisioning handoff, or just stay on the
robot's AP.

## Finding the robot on a shared network (Provisioning mode)

To connect in provisioning mode the app must discover the robot's DHCP IP:

1. **ARP-cache shortcut first:** check whether the last-known IP
   (`goliathLastIPAddress`) still maps to the last-known MAC
   (`goliathLastMACAddress`) via the system ARP table. If yes, use it.
2. Otherwise **sweep the local /24**: `Parallel.ForEach` over the subnet
   (parallelism = CPU count), pinging each host to populate the ARP cache, and
   match each entry's MAC against `goliathLastMACAddress`. Stop on first match.
   - MAC comparison is case-insensitive; the app reads the ARP table with a
     lowercase-hex regex (`([a-f0-9]{2}-?){6}`) and uppercases results.
3. The found address becomes `goliathCurrentIPAddress`, used for the TCP sockets.

A replacement can do the same, or simply let the user type the IP, or use mDNS if
the robot advertises it (not observed — the vendor used ARP scanning).

## Handing the robot a network (the provisioning handoff)

Run while connected to the robot's AP:

1. PC → `GET SSIDS`. (Firmware ≥ 01.00.14; older firmware used `RESET NET KEY`
   polling.)
2. Robot streams its scan: `INFO;31;START`, then one `INFO;31;<ssid>` per network,
   then `INFO;31;END`. (The app waits ~500 ms after `END` before showing the
   de-duplicated list.)
3. User picks an SSID, enters the password and security type.
4. PC → **`SSID=<hexSSID>&PWD=<hexPWD>&AUTH_TYPE=<n>`**
   - `hexSSID`/`hexPWD`: each character encoded as two uppercase hex digits.
   - `AUTH_TYPE`: **1** = open, **2** = WPA/WPA2-PSK, **3** = WEP.
   - For a secured network the password must be non-empty (the app guards this).
5. The robot joins that network; the PC leaves the AP, rejoins the same network,
   and re-finds the robot by MAC (the discovery above). `ConnectionMode` is saved
   as `Provisioning`.

## Tower pairing and geometry

The robot locates itself from two wire "towers" (anchors). Provisioning them:

- **RF channels:** PC → `SET RF CHANNELS;<T1ch>;<T2ch>`. The channel pair comes
  from `Slingshot.ini [COMUNICAZIONE]` keys `CONFIG_0`…`CONFIG_5`, each a
  `T1|T2` pair (e.g. `101|114`), selected by `CONFIG_SELECTED`. The chosen pair is
  saved to `T1_CHANNEL`/`T2_CHANNEL` and **re-sent at the start of each panel
  detection**. Defaults from the shipped INI:

  | CONFIG_n | T1 | T2 |
  |---|---|---|
  | 0 | 101 | 114 |
  | 1 | 103 | 116 |
  | 2 | 105 | 118 |
  | 3 | 107 | 120 |
  | 4 | 109 | 122 |
  | 5 | 111 | 124 |

- **Second tower axis:** PC → `SET T2 POSITION;<1|0>` (1 = T2 is along the X axis
  relative to T1, 0 = along Y).
- **Inter-tower distance:** the app does **not** call `GET TOWERS DISTANCE`; it
  reads the distance the robot pushes as `INFO;27;<distance>` during panel
  detection. `DISCARD TWRS DISTANCE` throws the measurement away and resets the
  panel-detection FSM to re-measure.

## Encoder index-distance calibration

The towers' wire encoders need an "index distance" calibration value:
- Read: `READ INDEX CALIBRATION` → reply `cfg read index :<value>`.
- Write: `SET INDEX CALIBRATION VALUE;<n>` — the app only sends when
  `27000 < n < 30100` (`MIN_INDEX_VALUE`/`MAX_INDEX_VALUE` in
  `Slingshot.ini [INDEX_DISTANCE]`). This is the value owners historically had to
  re-enter after a tower reset.

## Caveats
- The hard-coded AP WPA key and SSID prefix are from the 1.9.3.0 app; a different
  firmware generation could differ. Verify against your machine's label.
- `TOWERS POSITION` (explicit tower coordinates) exists but the app doesn't use
  it — tower geometry is established by the robot's own panel-detection sweep plus
  `SET T2 POSITION`.
