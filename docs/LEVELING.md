# Leveling on the Carvera

## How the Carvera's own leveling works (firmware 1.0.3, CarveraController 0.9.14)

Found by reading CarveraController's bytecode, Makera's `CartGridStrategy.cpp` and testing on a machine.

- **CarveraController's "Auto Leveling"** sends
  `buffer M495 X<xmin>Y<ymin>[C<xmax>D<ymax>][O<dx>F<dy>]A<width>B<height>I<nx>J<ny>H<height>` and then
  `play`. The grid is **always the loaded file's toolpath bounding box**. The UI only sets the point
  counts and the probing height. With leveling, margin and Z probe all off, Run sends `M495 X Y P1`
  (go to the origin). Run never clears leveling; Clear sends `M370`.
- **The firmware** runs `M495` as a two-corner `G32R1` probe. Grid values are relative to the first
  probe point, which is also the auto-Z-probe point and **sets work Z0**.
- **Outside the grid rectangle the compensation is ZERO.** Makera changed Smoothieware's clamp, so any
  toolpath outside the probed area runs uncompensated. An inset grid leaves edge passes uncompensated.
- `M375.1` prints the grid (read-only). There is no command to set or offset a single point. After
  `G32R1`, `M374`/`M375` (save/load) refuse. The grid lives in `/sd/cartesian.grid` (binary).
- **Status line:** `O:<value>` = the grid's maximum delta (present only while a grid is held).
- `M495` works standalone through `POST /api/gcode`: its `ok` comes back after about 10 s while probing
  continues, so poll status until Idle. Carvera Control re-reads the offsets afterwards (the Z probe
  moved G54 Z).

## Offline leveling (full control)

Probe a grid that covers the whole toolpath, read it back, and write a leveled copy of the program
where every Z already follows the surface. Then run the copy with the controller's Auto Leveling and
Auto Z Probe **off** (the leveled file starts with `M370`, so a grid held in the firmware is not applied
twice).

```sh
# 1. load the probe, then probe a 5x5 grid over the toolpath (X12-273, Y11-214 here), probing height 5 mm
carvera-api POST /api/tool '{"tool":0}'
carvera-api POST /api/gcode '{"lines":["M495 X12Y11O0F0A261B203I5J5H5"]}'      # about 3 min; poll /api/status
# 2. read the grid back and turn it into JSON (origin = the first probe point)
carvera-api POST /api/gcode '{"lines":["M375.1"]}' > reply.json
python3 tools/leveling/parse_grid.py reply.json 12 11 2026-01-01 grid.json
# 3. level the program, then verify the result move by move
python3 tools/leveling/level_gcode.py job.cnc job_leveled.cnc grid.json
python3 tools/leveling/verify_level.py job.cnc job_leveled.cnc grid.json
# 4. put the probe away (deep stow) before running
carvera-api POST /api/tool '{"tool":-1}'
```

- **`parse_grid.py REPLY.json X0 Y0 DATE OUT.json`**: reads the `M375.1` reply (rows print back to front
  as `<dy>| v v v`, the last line lists the X offsets) and writes
  `{"origin":[x0,y0], "xs":[...], "ys":[...], "z":[[row at ys[0]], ...]}`.
- **`level_gcode.py IN OUT GRID.json`**:
  - interpolates bilinearly inside the grid and extrapolates linearly outside it, from the edge cells;
  - splits lines into ≤ 3 mm pieces, and G2/G3 arcs into ≤ 3 mm helical sub-arcs about the original
    centre, so arcs stay arcs;
  - passes a line with no Z through unchanged while Z is still unknown;
  - inserts a comment header and `M370`.
- **`verify_level.py IN OUT GRID.json`**: an independent check. Non-motion lines must be identical. Every
  original move must be matched in order, with end Z = original Z + surface, split lines on the original
  path, sub-arcs on the original centre and radius, and the same modal feed. It reports the Z range,
  XY extents and the surface at the corners.

## Probing a good grid

- **Probe battery.** A grid costs about 0.15 V. Start at 3.6 V or more, measured *off* the charger: the
  docked `W:` reading runs high while charging. On the reference machine a probe at 3.34 V missed a
  trigger and drove 5.9 mm past the surface until a Z motor alarm.
- **Stay off edges and openings.** A probe point on the edge of a hole or window reads high: the ball
  half over the opening trips on its curved side. Keep points at least the ball radius + 2 mm clear of
  any opening. Treat a single point more than 0.2 mm off its neighbours as suspect and check what is
  under it.
- **Stay off tape and clamps**, and obey the site rules: no grid point inside a dowel zone or over the
  anchor.
- **Check the shape.** A smooth tilt or dome is normal (a bowed sheet, the bed itself). Plot the grid
  before using it.
- After the grid, put the probe away with Carvera Control's deep stow and check that it charges.
