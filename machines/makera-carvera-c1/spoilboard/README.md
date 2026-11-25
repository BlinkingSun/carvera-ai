# Spoilboard for the Makera Carvera C1

A replacement spoilboard laid on top of the original Makera bed, with every bolt hole of Makera's stock
bed pattern bored through it so clamps and screws still reach the bed, plus two dowel holes for
locating fixtures. First made and verified on the reference machine (1/4" PVC, 11-minute cut).

![Hole layout](Spoilboard%20Holes%20Carvera%20-%20layout.png)

## The spoilboard

- **Size:** 335 × 225 mm, 1/4" (6.35 mm) PVC, laid on the original bed and pushed into the anchor 1
  corner.
- **Origin:** 0,0 is its front-left corner, top surface Z0. Because it butts into the anchor 1 corner,
  0,0 is also G54 X0 Y0 when G54 X/Y is set to anchor 1. +X right, +Y away from you.
- **48 holes,** lined up with the holes in the original bed:
  - 46 bolt holes, Ø5.2 (a 5.1 hole also fits the bolts);
  - 2 dowel holes, **Ø4.1 for 4 mm dowels** (4.0 was very tight): **dowel A X82.5 Y126.0**,
    **dowel B X171.0 Y37.5**;
  - all 7.1 mm deep from the top, i.e. 0.75 mm into the old bed.
- **Left out on purpose:**
  - the 6 bed holes in line with the anchor's legs (row Y −4.5 and column X −4.5), which fall off the
    sheet;
  - the 5 anchor screw holes under the anchor block.

## Dowel rule

**The tool head must never travel below Z0 (the spoilboard top) at the dowel locations**: within R5.0
of X82.5 Y126.0 or X171.0 Y37.5, measured to the tool's edge. This applies to every program, probe,
grid and jog. The only exception is `Spoilboard Holes Carvera.cnc` itself, which cuts those holes. Also:
nothing travels over the anchor 1 block at any height. Both rules are in `site-rules.txt`.

## Files

| File | What it is |
|---|---|
| `Spoilboard drawing.pdf` | A3 drawing: size, thickness, dowel locations and diameter, section through a dowel, the dowel rule. |
| `Spoilboard Holes Carvera.cnc` | The program: 48 holes, about 11 minutes, onboard vacuum on. |
| `Spoilboard Holes Carvera - layout.png` | Hole map seen from the front, with the tool route, the anchor keep-out, the dowel no-go zones and the Z0 probe point. |

The generators are `tools/spoilboard_holes.py` (program) and `tools/spoilboard_drawing.py` (drawing,
needs matplotlib).

## What the program assumes

- **G54 X/Y = the anchor 1 inner corner.** On your machine,
  `config-get sd coordinate.anchor1_x` / `..._y` report it. The reference machine's value is in the
  program header.
- **Z0 = top of the new sheet.** Probe it at X177.5 Y82.5: open sheet, clear of holes, dowels and the
  anchor.
- **T3 = 1/8" (3.175 mm) flat end mill.** Helical bore, climb cut, S20000. Bolt holes at F600; the small
  dowel bores slow to F350 to keep the same load at the wall.
- **Height:** the program lifts to machine Z −3 before any travel and goes down only over the first
  hole. No move passes over the anchor (the tool edge stays at least 12.4 mm from it).

## Step by step

1. Lay the new sheet on the bed, pushed into the anchor 1 corner. Keep hold-downs off the hole spots and
   below 5 mm.
2. Start Carvera Control and connect (or `/carvera-ai` in your agent).
3. Load the probe (T0), move high to X177.5 Y82.5, probe down and set Z0 on the sheet top.
4. Put the probe away through Carvera Control. Its deep stow plunges 2 mm deeper than the firmware, so
   the probe sits on its charger. Check that it is charging.
5. Run `Spoilboard Holes Carvera.cnc`.
6. Check: a 4 mm dowel in each dowel hole, and the bolt holes over the old holes.

## Prompt for an agent (copy and paste)

```text
/carvera-ai Make a new spoilboard on the Carvera. A fresh 1/4" sheet, 335 x 225, is on the bed on top of
the old one, pushed into the anchor 1 corner. Read machines/makera-carvera-c1/spoilboard/README.md in the
carvera-ai repository first and use the files in that folder. Rules:
- Keep G54 X/Y at anchor 1. Check the machine's position and tool before every motion step.
- Nothing may travel over the anchor 1 block at any height, and no probing there.
- Dowel rule: the tool never goes below Z0 within R5 of the dowels at X82.5 Y126 and X171 Y37.5,
  except in the spoilboard program itself.
- Load the probe, move high to X177.5 Y82.5, probe Z0 on the sheet top, then put the probe away with
  Carvera Control's deep stow and check that it charges.
- Show me the preflight and wait for my go, then run "Spoilboard Holes Carvera.cnc"
  (46 x D5.2 bolt holes, 2 x D4.1 dowel holes, 7.1 mm deep) with the vacuum on.
- If the sheet thickness or the hole sizes change, regenerate the file first with
  tools/spoilboard_holes.py and verify it before uploading.
```

## Changing sizes

```sh
python3 tools/spoilboard_holes.py "Spoilboard Holes Carvera.cnc"    # the spec above (the defaults)
python3 tools/spoilboard_holes.py out.cnc --depth 13.5              # 1/2" sheet
python3 tools/spoilboard_holes.py out.cnc --dia 5.1                 # bolt holes at 5.1
python3 tools/spoilboard_holes.py out.cnc --dowel-dia 4.15          # looser dowel holes
python3 tools/spoilboard_holes.py out.cnc --comp 4.1:0.02           # dowel holes measured 0.02 small
python3 tools/spoilboard_drawing.py out.cnc "Spoilboard drawing.pdf"
```

`--comp D:C` adds C mm to every hole whose nominal size is D (C = wanted − measured). The generator
refuses to write a file with nested comments or with a hole too close to the anchor.

## Where the hole positions come from

Makera's `CarveraC1_BedSimplified.dxf` (in inches; 59 holes of Ø5.5) from
[MakeraInc/CarveraProfiles](https://github.com/MakeraInc/CarveraProfiles). That drawing has the anchor at
its back-right corner, so it is rotated 180° into machine orientation. The "Carvera WasteBoard v1" part
in Makera's detailed C1 model has the same holes. Makera's CAD has no dowel holes; the two dowel
positions are this spoilboard's design choice.

## Hole table (from the 0,0 corner, mm)

| X | Y | Ø | |
|---:|---:|---:|---|
| 65 | 15 | 5.2 |  |
| 110 | 15 | 5.2 |  |
| 155 | 15 | 5.2 |  |
| 200 | 15 | 5.2 |  |
| 245 | 15 | 5.2 |  |
| 290 | 15 | 5.2 |  |
| 327 | 15 | 5.2 |  |
| 25 | 37.5 | 5.2 |  |
| 105 | 37.5 | 5.2 |  |
| 160 | 37.5 | 5.2 |  |
| 171 | 37.5 | 4.1 | **dowel B** |
| 308.5 | 37.5 | 5.2 |  |
| 120 | 42 | 5.2 |  |
| 25 | 60 | 5.2 |  |
| 65 | 60 | 5.2 |  |
| 110 | 60 | 5.2 |  |
| 155 | 60 | 5.2 |  |
| 200 | 60 | 5.2 |  |
| 245 | 60 | 5.2 |  |
| 290 | 60 | 5.2 |  |
| 327 | 60 | 5.2 |  |
| 87 | 75 | 5.2 |  |
| 25 | 105 | 5.2 |  |
| 65 | 105 | 5.2 |  |
| 110 | 105 | 5.2 |  |
| 155 | 105 | 5.2 |  |
| 200 | 105 | 5.2 |  |
| 245 | 105 | 5.2 |  |
| 290 | 105 | 5.2 |  |
| 327 | 105 | 5.2 |  |
| 82.5 | 115 | 5.2 |  |
| 82.5 | 126 | 4.1 | **dowel A** |
| 25 | 150 | 5.2 |  |
| 65 | 150 | 5.2 |  |
| 110 | 150 | 5.2 |  |
| 155 | 150 | 5.2 |  |
| 200 | 150 | 5.2 |  |
| 245 | 150 | 5.2 |  |
| 290 | 150 | 5.2 |  |
| 327 | 150 | 5.2 |  |
| 65 | 185 | 5.2 |  |
| 25 | 195 | 5.2 |  |
| 110 | 195 | 5.2 |  |
| 155 | 195 | 5.2 |  |
| 200 | 195 | 5.2 |  |
| 245 | 195 | 5.2 |  |
| 290 | 195 | 5.2 |  |
| 327 | 195 | 5.2 |  |
