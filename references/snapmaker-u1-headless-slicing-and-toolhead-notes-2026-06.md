# Snapmaker U1 headless slicing and active-tool notes — 2026-06

Session-derived notes for future U1 automation work.

## Headless OrcaSlicer proof

A safe proof chain was completed:

```text
STL -> OrcaSlicer CLI -> U1 PETG G-code -> Moonraker upload with print=false -> verify printer stayed idle
```

Working local paths in the operator's Hermes container:

```text
<orcaslicer-install>/squashfs-root/bin/orca-slicer
<orcaslicer-install>/local-libs/usr/lib/x86_64-linux-gnu
<data-dir>/snapmaker_u1/profiles/hermes_generic_petg_u1_textured_pei.json
<data-dir>/snapmaker_u1/profiles/hermes_merged_016_optimal_u1_textured_pei.json
```

The AppImage was extracted because FUSE was not available in the container. Run with an `LD_LIBRARY_PATH` that includes the extracted Orca libs and the local dependency dir.

## Profile facts observed from known-good U1 PETG G-code

Known-good Snapmaker Orca output used these key values:

```text
printer_settings_id = Snapmaker U1 (0.4 nozzle)
print_settings_id = 0.16 Optimal @Snapmaker U1 (0.4 nozzle)
filament_type = PETG
layer_height = 0.16
curr_bed_type = Textured PEI Plate
nozzle_temperature = 255
first_layer_temperature = 255
first_layer_bed_temperature = 80
sparse_infill_density = 15%
```

Stock Orca/Snapmaker profile inheritance did not fully resolve these values when loaded naïvely via CLI; a merged local process profile plus self-contained PETG profile produced correct G-code metadata.

## Upload-only proof

Upload endpoint:

```text
POST /server/files/upload
form-data: root=gcodes, path=, print=false, file=<gcode>
```

Successful response included:

```json
{"print_started": false, "print_queued": false}
```

Always verify file presence under `/server/files/list?root=gcodes` and query printer state after upload.

## Active toolhead gotcha

On U1, the plain `extruder` object can report parked/cold while the printer is actively printing on another tool. Query all tool objects and use `toolhead.extruder` to select the active one:

```text
/printer/objects/query?print_stats&virtual_sdcard&toolhead&extruder&extruder1&extruder2&extruder3&heater_bed
```

Example correction from live print:

```text
toolhead.extruder = extruder1
extruder.temperature = 36 / 0 C, state PARKED
extruder1.temperature = 240 / 240 C, state ACTIVATE
```

Operator-facing status must report the active object, e.g. `extruder1 240/240 C (ACTIVATE)`, not generic `nozzle 36/0 C`.

## Safe default for future STL requests

For user-supplied STL:

1. Ask/confirm material, preset, supports, and orientation.
2. Slice using the U1 merged local profiles.
3. Validate G-code metadata for printer/profile/material/temps/layer height.
4. Upload with `print=false` only.
5. Report readiness and require explicit start approval.
## v1.4.0 orientation/render correction

Orca `--orient` only prints the optimum; toolkit applies the rotation. See `snapmaker-u1-orient-rotate-and-slice-review-2026-06.md`.

Orca `--orient` only prints the optimal orientation; it does not export a rotated STL. The toolkit applies Orca's row vector as the source-frame direction that becomes build-up `+Z`, writes `oriented.stl`, and uses that same file for both render and slice. If a render disagrees with the slice, the rotation step or first-layer parser did not run. See `references/snapmaker-u1-orient-rotate-and-slice-review-2026-06.md`.

