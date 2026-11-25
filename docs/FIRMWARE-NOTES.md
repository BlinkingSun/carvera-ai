# Carvera C1 firmware 1.0.3: protocol notes

Facts found on a real Carvera C1 (firmware 1.0.3, Smoothieware-based) and in Makera's firmware source.
The simulator in `sim/carvera_sim.py` reproduces these behaviours.

## Connection

- TCP port **2222** over Wi-Fi, one client at a time. Discovery broadcasts on **UDP 3333**:
  `name,ip,port,busy` (`busy` = 1 while another controller is connected).
- Realtime bytes: `?` status, `!` feed hold, `~` resume, `0x18` (Ctrl-X) abort.
- Every G-code/M-code line gets one `ok`. **Shell commands** (`version`, `diagnose`, `config-get`, `ls`,
  `md5sum`, `play`, `rm`) answer with text and **no `ok`**.
- `$H`, `G38.x` and `M400` hold their `ok` until the motion ends. `M6` streams many internal lines and
  `ok`s and ends with `Done ATC`.

## Status (`?`)

`<State|MPos:x,y,z,a,b|WPos:...|F:...|S:rpm,target,...|T:tool,tlo|W:probe_v|L:...|O:lev_max|C:...|A:atc|P:lines,percent,secs>`

- `S:` actual rpm and target. `T:` tool number (T0 = probe, T−1 = empty) and TLO. `W:` wireless probe
  battery. `O:` the leveling grid's max delta (only while a grid is held). `A:` the ATC state (present
  while a tool change runs). `P:` playback progress.
- There is **no vacuum field** in the status. Use `diagnose`.

## `diagnose`

`{S:on,target|L:..|V:on,power|F:fan on,power|G:light|T:toolsensor|R:air|C:probecharger|E:endstops|P:probe pins|A:atc pins|I:estop}`

- A shell command (no `ok`) that also answers in Alarm. Carvera Control polls it about once a second
  for the vacuum, spindle switch and probe charger.
- A `diagnose` reply that arrives after a command's `ok` reflects the state after that command, because
  the firmware processes lines in order.

## Switches

- **Onboard vacuum:** `M801 S<1-100>` (on at that power) and `M802` (off). It switches at once, even
  while a program plays. `M331`/`M332` set "vacuum follows spindle" mode.
- **External-control output:** `M851 S<pct>` / `M852`. This is what Fusion's `Carvera.cps` posts, so a
  posted program never turns the onboard vacuum on by itself.
- **Probe charger:** `M841` (on) / `M842` (off). The firmware stops charging by itself when the probe is
  full (`WP voltage: [4.11V], end charging`).
- **A halt (stop, abort, alarm) switches off the spindle, the vacuum and the probe charger.** Send
  `M841` after recovering, or the probe stops charging.

## Tool length and work offsets

- Setting work Z (`G10 L20 P0 Z...`, or the Z probe with set-zero) computes the offset with the **old**
  TLO, then resets TLO to 0, and the current tool becomes the length reference. A single `G10 L20` can
  therefore leave the readout off by the old TLO; Carvera Control repeats it once and verifies.
- Later tools are measured on the setter at `M6`: TLO = their setter Z − the reference tool's setter Z.
  For example, T3 vs the probe: −89.725 − (−74.32) = −15.405. Work Z0 holds across tool changes.
- `G38.3` under `G91` is relative. The `[PRB:...]` reply has 3 decimals; `$#` reports the same point to
  4 decimals.
- `M495` (auto-leveling with Z probe) re-sets G54 Z at its first point. Re-read `$#` afterwards.
- `config-get sd coordinate.anchor1_x` (and `_y`) report the anchor 1 position, which is calibrated per
  machine. On the reference machine, G54 X/Y set to anchor 1 equals these values.

## ATC and the probe slot

- `M499.2` prints the slot table (`probe -- mx my mz` for the tool setter, then `tool0` to `tool6`), to
  0.1 mm.
- The firmware's drop script for a tool (from `ATCHandler::fill_drop_scripts`, echoed to the console
  during M6):
  `M497.1 · G53 G0 Z<clearance> · G53 G0 X Y (slot) · M492.2 · G53 G1 Z<slot+15> F1000 ·
  G53 G1 Z<slot> F200 · M490.2 (release) · G53 G0 Z-50 · M493.2 T-1 · M492.1`.
  M6 wraps it with push/pop of the modal state, returns to clearance Z and then to the X/Y it started
  from, and prints `Done ATC`.
- `M490.1` clamps, `M490.2` releases. `M492.1` / `M492.2` move ±0.5 mm in Y with the laser detector and
  halt if the slot under the spindle is empty or filled. `M493.2 T<n>` sets the tool number. `M497.x`
  sets the ATC status shown in `A:`. `M120`/`M121` push/pop the feed, modes and work coordinate system.
- Releasing the probe at the slot height often leaves it off the charging coil. Carvera Control's deep
  stow adds a slow 2 mm plunge before `M490.2` (see `docs/API.md`).
- `G28` on the Carvera means "go to the clearance position" (Z up, then the back-right corner).

## Programs on the SD card

- Upload: `upload /sd/gcodes/<name>`, then **XMODEM-8K**: 'C' handshake, MD5 in block 0, ACK/NAK,
  CAN = 0x16, EOT. Status polling must pause during the transfer (the firmware reads raw bytes). Then
  `md5sum /sd/gcodes/<name>` must match.
- `play /sd/gcodes/<name>` runs the file. Played lines get no `ok`; the status shows `|P:...|`; the end is
  `Done printing file`. `abort` stops it. A tool change inside a played file prints only `Done ATC`.
- `ls -s /sd/gcodes` lists files with sizes and timestamps (names with spaces show `\x01` separators).
- The firmware ends a `( … )` comment at the first `)`. Never nest brackets in comments.

## Machine

- Travel: X −371…0, Y −250…0, Z −135…0. Home ends at −1, −1, −1. +X right, +Y away from the operator, +Z
  up. Max feed XY 3000, Z 2000 mm/min.
- Tools: T0 = wireless probe, T1–T6 rack, T−1 empty, T8888 laser.
