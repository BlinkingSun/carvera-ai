---
name: carvera-ai
description: Carvera AI — drive a Makera Carvera CNC (C1, firmware 1.0.3) from plain-text conversation through Carvera Control, a local server with a jog/indexing web UI, an HTTP API and an MCP server. It starts the server, connects to the machine over Wi-Fi, opens the UI and loads the machine state, saved indexing points, recent UI activity, site rules, the API and the firmware's quirks into context, so the agent can plan, do the math, probe, level, upload and run programs with the user. Use when the user types /carvera-ai, or asks to open, connect to, control, jog, probe, index, level or run a job on the Carvera, the CNC, the jog UI or "Carvera Control". Text after /carvera-ai is the first task. Always obey the site rules printed by carvera-open (reference setup: the tool never goes below the spoilboard top within R5 of the dowels at X82.5 Y126 and X171 Y37.5, and nothing travels over anchor 1).
---

# Carvera AI

The user positions the machine in the **web UI** (jogging, touching off edges, saving indexing
points). You read what they did, do the planning and the math, and act on the machine when asked.
One server (`127.0.0.1:17916`) owns the machine's single TCP connection. The UI, the `carvera` MCP
server and `carvera-api` all go through it, and every action is logged with its source (`ui`, `mcp`,
`api`).

Project root: `$CARVERA_CONTROL_HOME`, or the `carvera-ai` repository this skill lives in. The full API
is in `docs/API.md`, the firmware notes in `docs/FIRMWARE-NOTES.md`, the safety rules in `docs/SAFETY.md`.

## 1. Open (always first)

```sh
carvera-open                # start the server if needed, connect, open the UI, print the machine context
carvera-open --no-ui        # same without opening the browser (e.g. the UI is already open)
carvera-open --no-connect   # don't touch the machine connection
```

Read everything it prints and keep it in context: connection, state, tool and TLO, machine and work
position, G54, last probe, **SITE RULES**, saved points (with notes) and recent activity. Then give the
user a short summary: machine state, tool, where it is, which points exist and what they last did. After
that, do any task that followed `/carvera-ai`.

| Exit | Meaning | What to do |
|---|---|---|
| 0 | connected (or `--no-connect`) | go |
| 2 | server would not start | read `data/server.log` in the project and report |
| 3 | another controller holds the machine | usually CarveraController. Ask the user to close it (or press its disconnect), then rerun. Never use force without asking |
| 4 | machine unreachable | it is off, asleep or has a new IP. `carvera-api GET /api/discover` listens for its broadcast |

## 2. Act on the machine

**Use the MCP tools if this session has them** (`carvera` server: `machine_status`, `get_activity`,
`list_points`, `save_point`, `goto_point`, `jog`, `move_to`, `home`, `tool_change`, `probe_z`,
`set_work_zero`, `send_gcode`, `vacuum`, `stop`, `unlock`, ...). Otherwise use `carvera-api` (same API,
logged as source `mcp`). POSTs wait for the job to finish:

```sh
carvera-api GET  /api/status
carvera-api GET  /api/activity            # what the user just did in the UI (source "ui")
carvera-api GET  /api/points
carvera-api POST /api/jog   '{"axis":"x","distance":0.2,"feed":300}'
carvera-api POST /api/move  '{"x":181.2,"system":"work","feed":300}'       # or "system":"machine"
carvera-api POST /api/points '{"name":"start","axes":["y"],"note":"edge"}'  # capture the current position
carvera-api POST "/api/points/start/goto" '{"mode":"direct"}'               # or "lift"; "include_z":true
carvera-api POST /api/tool  '{"tool":0}'          # 0 probe, 1-6, -1 empty (putting the probe away = deep stow)
carvera-api POST /api/probe/z '{"max_distance":30,"lift":5,"set_zero":true}'
carvera-api POST /api/zero  '{"values":{"z":5}}'  # the current position reads Z5
carvera-api POST /api/gcode '{"lines":["G91 G1 X-1 F300"]}'   # raw; G90 restored; offsets re-read after G10/G92/M495
carvera-api POST /api/home
carvera-api POST /api/stop                        # feed hold + abort; then: carvera-api POST /api/unlock
carvera-api POST /api/vacuum '{"on":true,"power":80}'    # onboard vacuum; works mid-program; '{"on":false}' = off
carvera-api POST /api/spindle '{"on":true,"rpm":12000}'  # spindle M3/M5; ONLY when the user asks
carvera-api POST /api/disconnect                  # hand the machine back to CarveraController
```

Failures come back as `{"state":"failed","error":...}` (HTTP 422). A 409 means another operation is
running: wait, or ask the user.

## 3. Safety (non-negotiable)

- **Site rules first.** `carvera-open` prints `site-rules.txt`. Every program, probe point, leveling
  grid point and jog must obey them. The reference setup's rules (section 7) are:
  - **Dowel rule:** the tool head must NEVER travel below Z0, the spoilboard top, within R5 of the
    dowels (A X82.5 Y126.0, B X171.0 Y37.5, measured from the spoilboard's front-left corner = G54 0,0).
    Check every program before upload by replaying its moves (lines and arcs) against both zones, with
    the spoilboard top expressed in that program's work frame. The only exception is the spoilboard
    program, which cuts those holes.
  - **Anchor 1:** nothing travels over the anchor block at any height, and no probing there (work
    X −15…85 × Y −15…0 and X −15…0 × Y −15…85). Keep the tool edge 5 mm clear. The firmware's M6 returns
    to the XY it started from, so never start a tool change while parked over the anchor.
- Move the machine only when the user asked for **that** motion. Say what you will send before you
  send it. A standing request ("jog me there") covers that one move, not the session.
- Check the machine's position and tool (`machine_status`) before **every** motion step, including
  tool changes. The user may have jogged in the UI since you last looked.
- Never start the spindle unless the user asked for a cut (`/api/gcode` refuses `M3/M4` unless
  `allow_spindle`; `/api/spindle` refuses with the probe or an empty spindle). Spin up at a safe
  height and let it reach speed (about 15 s; S20000 reads about 16–17k rpm on the reference spindle).
- Near the work, use feed moves and go down in small steps. The `lift` goto mode rapids up to safe
  machine Z (−3) first.
- Starting a program IS a cut: show the preflight (file, ops, tools, Z0/XY origin, time, kill-switch
  envelope, vacuum) and get an explicit "go" first.
- Never delete anything (files on the computer, the machine's SD card or anywhere else) without asking.
- STOP in the UI or API is a software stop. The machine's E-stop is the real one.

## 4. Machine facts (Carvera C1, firmware 1.0.3)

- TCP port 2222; discovery broadcasts on UDP 3333 (`name,ip,port,busy`). With no `host` configured,
  Carvera Control connects to the Carvera it hears.
- Machine travel: X −371…0, Y −250…0, Z −135…0. Home ends at −1, −1, −1. **+X right, +Y away from the
  operator, +Z up.** Max feed XY 3000, Z 2000 mm/min.
- Tools: T0 = wireless probe (battery in status `W:`), T1–T6 in the rack, T−1 = empty. The ATC measures
  every new tool on the setter (about 60–70 s per change).
- **TLO model:** setting work Z (probe_z with set_zero, or a Z zero) resets TLO to 0, and the current
  tool becomes the length reference. Later tools get TLO = their setter Z − the reference tool's setter
  Z, so the work Z0 holds across tool changes. Never compute G54 = PRB − TLO by hand.
- `$H`, `G38.x` and `M400` hold their `ok` until motion ends. `version`, `diagnose`, `config-get`,
  `ls`, `md5sum` are shell commands that answer without an `ok`. `M6` streams many `ok`s and ends with
  `Done ATC`.
- Fusion's `Carvera.cps` post: `M851 S100` / `M852` switch the EXTERNAL-control output, NOT the onboard
  vacuum (`M801 S<pct>` / `M802`, or `/api/vacuum`). A posted program never turns the onboard vacuum on:
  add `M801 S80` after the tool change, or switch it with `/api/vacuum`. `G28` = go to the clearance
  position. `T<n>M6` = tool change.
- Probe battery: judge charge off the coil, not by the docked `W:` reading (it reads high while
  charging). Don't run a leveling grid below about 3.5 V: a low probe can miss a trigger and drive into
  the work. A stop or alarm switches the probe charger off; send `M841` afterwards. More in
  `docs/FIRMWARE-NOTES.md`.

## 5. Indexing math (touch-offs with the cutting tool)

When the user touches off with the **cutting tool**, a saved point is the **tool-centre** position at
an edge, not the edge. To map a CAM program onto the part:
1. Find the program's tool-centre coordinate for the same contact (e.g. a contour's Y-run at
   X = −1.587 for a 1/8" tool means the wall is at X 0; the end of a corner fillet sits at
   Y = fillet radius + tool radius).
2. shift = saved work coordinate − program tool-centre coordinate, per axis. Apply it to every X/Y word
   (I/J are incremental and stay as they are). For Z, check whether the program's Z0 is the probed
   surface (read the post header: "Z Origin … Stock Top").
3. Verify by mapping: the program's contact points must land exactly on the saved machine X/Y. Then do
   a ghost pass at the highest Z (in air), then the real run. Shift X/Y for step-overs; lower only the
   final pass for a floor clean-up.

Saved points keep machine X/Y (valid while homing holds) and work Z (valid while G54 Z is unchanged).

## 6. Running programs

The firmware plays the file from its SD card (tool changes, spindle and vacuum included), so a Wi-Fi
drop does not stop the cut. Carvera Control uploads, verifies, starts and watches it:

```sh
carvera-api GET  "/api/program/analyze?path=<url-encoded path>"     # extents, Z range, tools
carvera-api POST /api/program/upload '{"path":"/abs/path/job.cnc","name":"job.cnc"}'   # no spaces in name
curl -s -X POST -H 'X-Source: mcp' 127.0.0.1:17916/api/program/run \
     -d '{"path":"/abs/path/job.cnc","name":"job.cnc"}'        # no ?wait: returns the job id at once
carvera-api GET  /api/job/<id>                                    # progress: lines, percent, state
```

- Upload is XMODEM-8K, then the machine's `md5sum` must match. Run refuses unless the stored file's md5
  matches the local file.
- Kill switch: with the spindle on and no tool change running, a tool below work Z3 outside the
  program's XY extents + 3 mm, or below its lowest Z − 0.5, triggers feed hold + abort.
- Leveling: CarveraController's auto-leveling only covers the toolpath bounding box and the firmware
  applies NO compensation outside the probed grid. For full control, probe a grid with `M495`, read it
  with `M375.1` and level the program offline with `tools/leveling/` (see `docs/LEVELING.md`).
- **Probe stow:** putting the probe away through `/api/tool` plunges 2 mm below the firmware's drop
  (`probe_stow_extra_mm`) so it seats on its charger. A `T<n>M6` inside a program still drops it the
  firmware's way, so after a leveling grid, stow it with `/api/tool {"tool":-1}` BEFORE starting the
  run, and check that it charges.
- Watching a run: switch the vacuum on only while the spindle runs (never after the program's own
  `M802` at the end).

## 7. Site rules and spoilboard (reference setup; edit `site-rules.txt` for yours)

- **Spoilboard:** 335 × 225 × 6.35 mm PVC laid on the original Makera bed, pushed into the anchor 1
  corner. Its front-left corner is 0,0 = G54 X0 Y0 (the anchor 1 inner corner). Its top surface is Z0.
- **Top surface height:** on the reference machine, machine Z −103.12 with the probe as the length
  reference. When G54 Z is probed on top of stock, the spoilboard top in work Z is
  (spoilboard machine Z) − G54 Z. With a CAM Z0 at the stock top, it is −(stock thickness).
- **Holes:** 46 × Ø5.2 bolt holes on the Makera C1 bed pattern, plus **dowel A X82.5 Y126.0 and dowel B
  X171.0 Y37.5, Ø4.1** (4 mm dowels). All 7.1 deep. Files: `machines/makera-carvera-c1/spoilboard/`
  (drawing PDF, program, layout PNG, README with a copy-paste prompt). Generators:
  `tools/spoilboard_holes.py`, `tools/spoilboard_drawing.py`.
- **Dowel rule** and **anchor 1 rule**: see section 3. A through-cut that crosses a dowel zone must be
  refused or re-planned before upload.

## 8. From a text conversation to a finished part

With an Autodesk Fusion MCP server connected as well, the whole chain can run from conversation:
model the part in Fusion, create the CAM setup and toolpaths, post with `Carvera.cps`, fix the vacuum
codes, check the program against the site rules, index the stock (probe Z, touch-offs, saved points),
level if needed, upload, preflight, and run it on the machine with the user's go. The steps and the
pitfalls (Fusion Personal limits, the vacuum codes, the probe stow) are in `docs/FUSION-WORKFLOW.md`.
