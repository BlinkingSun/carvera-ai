# Makera Carvera C1 kit

Everything machine-specific for the Makera Carvera C1 (3-axis, firmware 1.0.3).

| Folder | What it is |
|---|---|
| [`spoilboard/`](spoilboard/README.md) | A replacement spoilboard: A3 drawing, the hole program, the hole map and a copy-paste agent prompt. |
| [`3d-model/`](3d-model/README.md) | `fetch_makera_models.sh` downloads Makera's own C1 models (STEP and Fusion f3d/f3z) and bed drawings from their repository. They are not stored here. |

## Machine facts

- **Travel:** X −371…0, Y −250…0, Z −135…0 (machine coordinates). Home ends at −1, −1, −1. +X right,
  +Y away from the operator, +Z up. Max feed XY 3000, Z 2000 mm/min.
- **Network:** TCP 2222; discovery broadcast on UDP 3333.
- **Tools:** T0 wireless probe, T1–T6 in the rack, T−1 empty. The ATC measures each new tool on the
  setter (about 60–70 s per change).
- **Anchor 1** (the L-shaped block at the front-left of the bed) is the usual work origin: G54 X/Y at
  its inner corner. Its position is calibrated per machine; `config-get sd coordinate.anchor1_x` and
  `..._y` report it. In work coordinates its arms cover X −15…85 × Y −15…0 and X −15…0 × Y −15…85.
- **Stock bed:** an 18 mm MDF plate with 59 Ø5.5 holes, 54 of them on the open bed (the other 5 are
  anchor screws). The holes are on a 45 mm clamp grid (columns X 25, 65, 110, 155, 200, 245, 290, 327;
  rows Y 15, 60, 105, 150, 195), with a front row at Y −4.5, a left column at X −4.5 and ten others.
- **Probe slot** on the reference machine: X −3.403 Y −25.143, release height Z −112.5 (`M499.2`
  reports yours).

More: [docs/FIRMWARE-NOTES.md](../../docs/FIRMWARE-NOTES.md).
