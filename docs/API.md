# Carvera Control HTTP API

Base URL: `http://127.0.0.1:17916` (change with `--port` or `CARVERA_PORT`). JSON in, JSON out.
This port listens on localhost only. It refuses requests whose `Host` is not 127.0.0.1 / localhost
(DNS rebinding) and POST/PATCH/DELETE requests with a cross-site `Origin`. Scripts and agents (no
`Origin` header) are unaffected. Phones and tablets use a second port: see
[Phone and tablet access](#phone-and-tablet-access).

- **Source labels.** Send `X-Source: ui | mcp | <name>` to label your actions in the activity log. The web
  UI sends `ui`, `carvera-api` and the MCP server send `mcp`, and anything else defaults to `api`.
- **Jobs.** Machine operations (jog, move, home, tool change, probe, zero, raw G-code, upload, run) run
  as **jobs**, one at a time. A POST returns the job at once. Add `?wait=1` (and optionally
  `&timeout=<s>`) to block until it finishes. A finished job reports `state: done|failed`, a `result`
  and the machine messages it produced. Failures come back as HTTP 422 with
  `{"state":"failed","error":"..."}`. If another operation is running you get 409.
- **Immediate actions** (hold, resume, stop) and **switches** (vacuum, spindle) are not jobs.

## Status and connection

| Endpoint | Body / query | What it does |
|---|---|---|
| `GET /api/status` | | Connection, parsed status (state, machine/work position, feed, spindle rpm, tool, TLO, probe battery, program progress), offsets (G54–G59, TLO, PRB), modal state, `vacuum`, `spindle`, the raw `diag` reply, the current job, sequence numbers for activity and messages, `remote` (`{on, port, devices}`) and `client` (`local`, or `remote` on the phone port). |
| `POST /api/connect` | `{host?, force?}` | Connects. With no host (and none configured), it uses the Carvera found on UDP 3333. Refuses if the discovery broadcast says another controller is connected, unless `force`. |
| `POST /api/disconnect` | | Closes the connection, handing the machine back to CarveraController. |
| `GET /api/discover` | `?timeout=3` | Lists Carveras broadcasting on UDP 3333: `{name, ip, port, busy}`. |
| `GET /api/offsets` · `POST /api/offsets/refresh` | | Reads `$#` (work offsets, TLO, last probe). |

## Motion and machine operations (jobs)

| Endpoint | Body | Notes |
|---|---|---|
| `POST /api/jog` | `{axis, distance, feed?}` | Relative feed move on one axis. Refused if the end point is outside machine travel (X −371…0, Y −250…0, Z −135…0). |
| `POST /api/move` | `{x?, y?, z?, system: "work"\|"machine", feed?, z_feed?, rapid?}` | Absolute move. Z goes first when rising and last when lowering. |
| `POST /api/home` | | `$H`; ends at −1, −1, −1. |
| `POST /api/tool` | `{tool, remeasure?}` | ATC tool change: 0 = probe, 1–6 = rack, −1 = empty. Refused while the spindle turns. When the probe is the tool being put away, it uses the **deep stow** (below). Result: new tool, TLO, setter Z. |
| `POST /api/probe/z` | `{max_distance, lift, set_zero, fast_feed, slow_feed, retract, allow_non_probe?}` | Probe straight down with the wireless probe: fast G38.3, back off, slow re-touch, lift. With `set_zero`, work Z0 is set on the surface (G10 L20, repeated once if the firmware's TLO reset leaves the readout off). Refused below 3.5 V probe battery. |
| `POST /api/zero` | `{axes:["x","y"]}` or `{values:{"z":5}}` | Sets work coordinates at the current position (G10 L20 P0), verified. |
| `POST /api/gcode` | `{lines:[...], allow_spindle?}` | Raw G-code, one line at a time, waiting for each `ok`. Refuses `M6` (use `/api/tool`) and `M3/M4` unless `allow_spindle`. Restores G90 after G91. Re-reads offsets after G10, G92, G38, M495, M491 or M493 and returns them as `offsets`. |
| `POST /api/unlock` | | `$X`, clears an alarm. |

## Immediate actions

| Endpoint | What it does |
|---|---|
| `POST /api/hold` | Feed hold (`!`). |
| `POST /api/resume` | Resume (`~`). |
| `POST /api/stop` | Feed hold, then abort (Ctrl-X). The machine goes to Alarm; Unlock before moving. A halt switches off the spindle, the vacuum and the probe charger. Software stop, not an E-stop. |

## Switches (not jobs)

| Endpoint | Body | Notes |
|---|---|---|
| `POST /api/vacuum` · `GET /api/vacuum` | `{on, power?}` | Onboard vacuum: `M801 S<1-100>` / `M802`, then read back from `diagnose` (`confirmed`). Allowed while a program runs; the firmware switches at once and the cut does not pause. Default power from `vacuum_power` (80). 400 in Alarm, 409 while a long exchange holds the line. |
| `POST /api/spindle` · `GET /api/spindle` | `{on, rpm?}` | Spindle: `M3 S<rpm>` / `M5`, confirmed from `diagnose`. Range 1000–20000, default `spindle_rpm` (10000). Start refused with the probe or an empty spindle. Both directions refused in Alarm and while a program plays (the program owns the spindle). `GET` returns `{on, target, rpm, can_start, why_not, range, default_rpm}`. |

**Manual moves with the spindle on.** The firmware reports state `Run` whenever the spindle is on, even with
nothing moving. Carvera Control treats "Run with the spindle on, no program playing, no tool change" as
ready: `jog`, `move`, `goto`, `zero` and `gcode` jobs run while it turns. `home`, `tool`, `probe/z`,
`program/upload` and `program/run` refuse with "the spindle is running; stop it before …".

## Phone and tablet access

A second listener, `0.0.0.0:<remote_port>` (17917), serves the same UI to paired phones and tablets
while phone access is on. Guide: [PHONE.md](PHONE.md). Code: `carvera_control/remote.py`.

On 127.0.0.1 (the phone port refuses these with 403):

| Endpoint | Body | Notes |
|---|---|---|
| `GET /api/remote` | | `{on, port, urls, code, tries_left, devices:[{id, name, paired, last_seen, ip}], error}`. `urls` lists the Bonjour `.local` name first, then the LAN IP. |
| `POST /api/remote` | `{on}` | Starts or stops the listener and saves `"remote"` in `data/config.json`. A new pairing code on every start. 409 with `error` if the port can't be opened. |
| `POST /api/remote/forget` | `{id?}` | Forgets one paired device, or all of them without `id`. Their cookies stop working at once. |

On the phone port:

| Endpoint | Body | Notes |
|---|---|---|
| `POST /api/pair` | `{code, name}` | Pairs a device. Success sets the `cc_device` cookie (HttpOnly, SameSite=Strict, one year) and changes the code; `data/devices.json` stores only the cookie's SHA-256. A wrong code returns 401; 5 wrong codes change the code. |

- Static files (the UI) load without pairing, so an unpaired device sees the pairing screen. Every
  other `/api/` call needs the cookie (401 `{"pair": true}` without it).
- Allowed for a paired device (`REMOTE_GET` / `REMOTE_POST` in `remote.py`): status, points (list,
  save, edit, delete, go to), activity, messages, job status, vacuum, spindle, connect, disconnect,
  hold, resume, stop, unlock, jog, move, home, tool, probe/z, zero. Everything else answers 403: raw
  G-code, programs, discovery, offsets refresh and `/api/remote`.
- Actions from the phone port are logged with source `remote` (the `X-Source` header is ignored).
- POSTs with a cross-site `Origin` are refused here too.

## Programs

| Endpoint | Body | Notes |
|---|---|---|
| `GET /api/program/analyze` | `?path=<abs path>` | Extents (arcs sampled), Z range, tools, spindle speeds, whether the file clears leveling (`M370`), size. |
| `POST /api/program/upload` | `{path, name}` | XMODEM-8K upload to `/sd/gcodes/<name>` (no spaces in the name), then the machine's `md5sum` must match the local file. Status polling pauses during the transfer. |
| `POST /api/program/run` | `{path, name, margin_xy?, margin_z?}` | Refuses unless the stored file's md5 matches the local file. Then `play`: the firmware runs the file from its SD card (tool changes, spindle, vacuum), so a Wi-Fi drop does not stop the cut. The job reports `progress` and ends on "Done printing file". **Kill switch:** with the spindle on and no tool change running, a tool below work Z3 outside the program's XY extents + `margin_xy` (3 mm), or below its lowest Z − `margin_z` (0.5 mm), triggers feed hold + abort. |
| `GET /api/job/<id>` | | A job's state, result, error, messages and progress. |

## Points, activity, messages

| Endpoint | Body | Notes |
|---|---|---|
| `GET /api/points` · `POST /api/points` | `{name, axes:["x","y","z"], note?}` | Saves the current position per axis under a name: machine and work coordinates, tool, TLO, G54, time. Saving X at one edge and Y at another under one name builds an indexing point. |
| `PATCH /api/points/<name>` · `DELETE /api/points/<name>` | `{note}` | Edit the note, or delete. |
| `POST /api/points/<name>/goto` | `{mode: "direct"\|"lift", include_z?, feed?}` | Go to the saved machine X/Y, at the current height or after lifting to safe Z; optionally then Z. |
| `GET /api/activity` | `?limit=&since=` | The activity log (`time, source, action, detail`). |
| `GET /api/messages` | `?since=` | Lines the machine printed (status and `diagnose` replies excluded). |

## Deep probe stow

The firmware releases the wireless probe at the slot height, which often leaves it sitting off its
charging coil. When the probe is the tool being put away, Carvera Control runs the firmware's own drop
sequence itself, with one extra slow plunge before releasing:

```
M120 · G53 G0 Z-3 · G53 G0 X<slot> Y<slot> · M492.2 (halts if the slot is filled)
G53 G1 Z<slot+15> F1000 · G53 G1 Z<slot> F200 · G53 G1 Z<slot - extra> F100 · M400 · M490.2 (release)
G53 G0 Z-50 · M493.2 T-1 · M492.1 (halts if the probe is not in the slot)
G53 G0 Z-3 · G53 G0 X<start> Y<start> · M400 · M121 · M841 (charger on)
```

- `probe_stow_extra_mm` (default 2, max 3; 0 = the firmware's drop). `probe_slot` overrides the slot
  defaults (`PROBE_SLOT_DEFAULT` in `machine.py`, from the reference machine).
- Before any motion it reads `M499.2` (the firmware's slot table) and refuses if the slot differs.
- `M497` (the ATC status flag) is left out on purpose: a flag stuck after a halt would make the run
  kill switch treat the machine as mid tool change.
- If the new tool is not −1, the firmware's M6 then picks and measures it from an empty spindle.
- A tool change inside a program is the firmware's and drops the probe its own way, so stow the probe
  through Carvera Control before starting a program.

## Configuration (`data/config.json`)

Created on first run with these defaults; edit and restart the server.

| Key | Default | Meaning |
|---|---|---|
| `host` | `""` | Machine IP. Empty = use the Carvera found on UDP 3333. `--machine <ip>` overrides. |
| `port` | `2222` | Machine TCP port. |
| `safe_z_machine` | `-3.0` | Safe machine Z for `lift` moves. |
| `feeds` | `{"xy":1000,"z":500}` | Default feeds. |
| `vacuum_power` | `80` | Default vacuum power %. |
| `spindle_rpm` | `10000` | Default spindle speed for the UI. |
| `probe_stow_extra_mm` | `2.0` | Extra plunge when stowing the probe (0 = off). |
| `probe_slot` | (absent) | Overrides the probe slot `{x, y, z, approach, fast_feed, slow_feed, lift_z, clearance_z}`. |
| `remote` | `false` | Phone and tablet access on at start. The Phone dialog saves it. |
| `remote_port` | `17917` | Port of the phone and tablet listener. |

Command-line options: `python3 -m carvera_control [--port N] [--machine IP] [--machine-port N] [--data DIR]
[--connect] [--sim] [--remote]` (`--remote` turns phone and tablet access on for one run).

## MCP tools

`mcp/carvera_mcp.py` (stdio, needs the `mcp` package) exposes 24 tools that call this API with
`X-Source: mcp`: `machine_status`, `get_activity`, `get_messages`, `list_points`, `save_point`,
`edit_point`, `delete_point`, `goto_point`, `jog`, `move_to`, `home`, `tool_change`, `probe_z`,
`set_work_zero`, `send_gcode`, `vacuum`, `feed_hold`, `resume`, `stop`, `unlock`, `connect`,
`disconnect`, `discover` and `open_ui`. It starts the server if it is not running (`CARVERA_PORT` selects
the port). There is deliberately no spindle tool: agents start the spindle only when the user asks for
a cut.
