# Safety

Carvera AI moves a real CNC machine with a spinning cutter, on request from a person or an AI agent.
Treat every motion as something that can break a tool, a part, a fixture or a finger.

- **The real stop is the machine's E-stop.** STOP in the UI and `POST /api/stop` send feed hold + abort
  over Wi-Fi. They are fast, but they are software.
- Not affiliated with or endorsed by Makera. Use at your own risk (see the MIT license).

## What the software enforces

| Interlock | Where |
|---|---|
| One machine operation at a time; others get 409 | server |
| Motion only when the machine is Idle; operations fail fast if it enters Alarm | `machine.py` |
| Jogs and moves outside machine travel (X −371…0, Y −250…0, Z −135…0) are refused | `machine.py` |
| Raw G-code may not start the spindle (`M3/M4`) unless `allow_spindle`, and may not change tools | `/api/gcode` |
| Tool changes are refused while the spindle turns | `/api/tool` |
| The spindle switch refuses to start with the probe or an empty spindle, in Alarm, or while a program runs | `/api/spindle` |
| Probing is refused below 3.5 V probe battery, or if the probe already reads triggered | `/api/probe/z` |
| A program runs only if the machine's copy has the same MD5 as the local file | `/api/program/run` |
| **Kill switch** during a run: spindle on, no tool change, and the tool below work Z3 outside the program's XY extents + 3 mm, or below its lowest Z − 0.5 mm → feed hold + abort | `program.py` |
| The deep probe stow checks the firmware's slot table first, keeps the firmware's own slot sensor checks, and refuses an extra plunge over 3 mm | `machine.py` |

## Site rules (`site-rules.txt`)

Some hazards are specific to your setup: dowels sticking out of the spoilboard, clamps, an anchor block.
Write them in `site-rules.txt` at the repository root. `carvera-open` prints the file at the start of
every agent session, and the skill tells the agent to obey it. The reference setup's rules:

- **Dowel rule:** the tool must NEVER travel below Z0, the spoilboard top surface, within R5 of the
  dowels at X82.5 Y126.0 and X171.0 Y37.5 (measured from the spoilboard's front-left corner = G54 0,0).
  This covers every program, probe point, leveling grid point and jog. The only exception is the
  spoilboard program that cuts those holes.
- **Anchor 1:** nothing travels over the anchor block at any height, and no probing there (work
  X −15…85 × Y −15…0 and X −15…0 × Y −15…85). The firmware's M6 returns to the X/Y it started from, so
  never start a tool change while parked over the anchor.

Site rules are **not yet enforced in code**: the agent checks programs against them before upload by
replaying every move (lines and arcs) against the zones. `tools/spoilboard_holes.py` shows how to route
a program around a keep-out zone.

## Rules the skill gives the agent

- Move the machine only when the user asked for **that** motion, and say what will be sent first. A
  request covers that one move, not the session.
- Check the machine's position and tool before every motion step: the user may have jogged in the UI.
- Never start the spindle unless the user asked for a cut. Spin up at a safe height.
- Starting a program is a cut: show a preflight (file, operations, tools, origin, time, kill-switch
  envelope, vacuum) and wait for an explicit "go".
- Never force a connection over another controller, and never delete anything (files, the machine's
  SD card) without asking.
- After a stop or alarm, the probe charger is off: send `M841` once the machine is unlocked.
- Don't run a leveling grid with a low probe battery (below about 3.5 V off the charger): a low probe can
  miss a trigger and drive into the work.

## Generated G-code

- Comments must be flat: never put `(` or `)` inside a `( … )` comment. The firmware ends a comment at
  the first `)`, so a nested bracket turns the rest of the line into G-code. `tools/spoilboard_holes.py`
  refuses to write such a file.
- Lift to a safe machine height (`G53 G0 Z-3.`) before the first X/Y move, and go down only over the
  first cut: after a tool change, or when the tool is already loaded, the spindle can be anywhere.
