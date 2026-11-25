# Carvera C1 3D models

Makera publishes the C1's CAD in [MakeraInc/CarveraProfiles](https://github.com/MakeraInc/CarveraProfiles)
(`Machine_Design_Files/`). That repository has no license, so the files are not copied into carvera-ai.
Download them from the source with:

```sh
sh fetch_makera_models.sh
```

| File | Format | What it is |
|---|---|---|
| `CarveraC1_MachineModel DETAILED.step` | STEP | The detailed C1 assembly (283 parts): frame, gantry, spindle, ATC rack, probe, MDF bed, bed base, anchors, and a 3.2 mm "Carvera WasteBoard v1" with the stock hole pattern. Origin = the anchor 1 inner corner, +X right, +Y back. |
| `CarveraC1_MachineModel DETAILED.f3z` | Fusion archive | The same detailed model for Fusion. |
| `CarveraC1_3 Axis_MachineModel v9.step` / `.f3d` | STEP / Fusion | The lighter 3-axis model (the basis of Makera's Fusion machine simulation). |
| `CarveraC1_MDF-Bed.STEP` | STEP | The 18 mm MDF bed with its 59 Ø5.5 holes. |
| `CarveraC1_BedSimplified.dxf` | DXF, inches | The bed outline, the anchor and the hole circles. The spoilboard's hole positions come from this file, rotated 180° (it has the anchor at its back-right corner). |

The downloaded files are git-ignored.
