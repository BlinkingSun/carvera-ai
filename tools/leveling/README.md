# Offline auto-leveling tools

| Script | What it does |
|---|---|
| `parse_grid.py REPLY.json X0 Y0 DATE OUT.json` | Turns the machine's `M375.1` grid printout (saved from `carvera-api POST /api/gcode '{"lines":["M375.1"]}'`) into a grid JSON, with the first probe point at X0 Y0. |
| `level_gcode.py IN OUT GRID.json` | Writes a leveled copy of a program: bilinear inside the grid, linear extrapolation outside it, lines split into ≤ 3 mm pieces, arcs into ≤ 3 mm helical sub-arcs, `M370` at the top. |
| `verify_level.py IN OUT GRID.json` | Checks the leveled copy against the original, move by move. |

How the Carvera's built-in leveling behaves, why offline leveling helps, and a full worked example are
in [docs/LEVELING.md](../../docs/LEVELING.md).
