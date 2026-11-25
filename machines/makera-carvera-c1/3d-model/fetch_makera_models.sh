#!/bin/sh
# Download Makera's Carvera C1 3D models and bed drawings from their official repository
# (https://github.com/MakeraInc/CarveraProfiles, Machine_Design_Files/) into this folder.
# They are not redistributed in carvera-ai: that repository has no license, so they stay Makera's.
# usage: sh fetch_makera_models.sh            (about 100 MB)
set -e
cd "$(dirname "$0")"
BASE="https://raw.githubusercontent.com/MakeraInc/CarveraProfiles/main/Machine_Design_Files"
for f in \
  "CarveraC1_MachineModel%20DETAILED.step" \
  "CarveraC1_MachineModel%20DETAILED.f3z" \
  "CarveraC1_3%20Axis_MachineModel%20v9.step" \
  "CarveraC1_3%20Axis_MachineModel%20v9.f3d" \
  "CarveraC1_MDF-Bed.STEP" \
  "CarveraC1_BedSimplified.dxf"
do
  name=$(printf '%s' "$f" | sed 's/%20/ /g')
  if [ -s "$name" ]; then
    echo "have  $name"
  else
    echo "fetch $name"
    curl -fL --retry 3 -o "$name.part" "$BASE/$f" && mv "$name.part" "$name"
  fi
done
echo "done: models are in $(pwd)"
