# Snapmaker U1 printhead numbering, colors, and cautious start workflow — 2026-06

## Durable lesson

the operator's human-facing U1 printhead numbering does **not** match the raw object names in an obvious way. Keep both names in readiness cards:

```text
printhead #1 = T0 / extruder  / channel 0 / white PETG
printhead #2 = T1 / extruder1 / channel 1 / black or Generic PETG
printhead #3 = T2 / extruder2 / channel 2
printhead #4 = T3 / extruder3 / channel 3
```

The local map was updated at:

```text
<data-dir>/snapmaker_u1/u1_tool_material_map.json
```

## Printer-reported color/material source

The U1 exposes the printer's own material/color table through Moonraker object `print_task_config`. Prefer it over local labels when building readiness cards or validating a print:

```text
/printer/objects/query?print_task_config&filament_detect
```

Important arrays:

- `print_task_config.filament_vendor[channel]`
- `print_task_config.filament_type[channel]`
- `print_task_config.filament_sub_type[channel]`
- `print_task_config.filament_color_rgba[channel]`
- `print_task_config.filament_exist[channel]`

Observed mapping during this session:

```text
channel 0 / T0 / extruder:  vendor=Snapmaker  type=PETG  color=FFFFFFFF  exist=true
channel 1 / T1 / extruder1: vendor=Generic    type=PETG  color=000000FF  exist=true
channel 2 / T2 / extruder2: vendor=Snapmaker  type=PLA   color=E2DEDBFF  exist=true
channel 3 / T3 / extruder3: vendor=Polymaker  type=PLA   color=F78E0EFF  exist=true
```

Use `filament_detect.info[channel]` as stronger RFID/tag evidence when populated, but it may be `NONE`/empty for manually configured spools. Do not require official RFID if `print_task_config` and the operator's explicit confirmation agree.

## G-code tool validation pattern

Before start, inspect the staged G-code and remote metadata:

- first tool command must match requested human printhead (`T0` for printhead #1)
- `filament_used_mm[requested_channel] > 0`
- all other `filament_used_mm` values should be `0` for single-tool prints
- `filament_type[channel]` should match requested material
- `filament_colour[channel]` should match expected color when applicable
- remote metadata should match the local/staged file

For the globe-light print, validation showed:

```text
file: globe light_PETG_5h56m.gcode
first tool: T0
filament_used_mm: [18251.51, 0.0, 0.0, 0.0]
filament_colour: #FFFFFFFF;#000000FF;#E2DEDBFF;#080A0DFF
filament_type: PETG;PETG;PLA;PLA
```

## Start gate wording

When the operator gives explicit approval but includes a correction like “this is for printhead #1 white,” treat it as a required final validation input, not as casual context.

Safe final sequence:

1. Re-query `print_task_config` and printer state.
2. Re-read remote metadata for the exact staged filename.
3. Inspect local/staged G-code startup for first `Tn` command and single-tool usage.
4. Verify bed clear/clean was explicitly confirmed by the operator or sufficiently classified by policy.
5. Start only if all gates align.
6. Wait briefly and verify the active tool changed to the requested tool.

Post-start verification should explicitly report:

```text
active tool: T0 / extruder / printhead #1
T0 temp/target/state: 230/230 ACTIVATE
T1/T2/T3: parked, target 0
bed: target reached or heating toward target
job filename and layer/progress
```

## Pitfall

Immediately after starting, U1 may still report the previous `toolhead.extruder` for a few seconds while it parses startup G-code. Do not panic if `toolhead.extruder` still shows `extruder1` immediately after the start response. Re-check after a short delay; it should change to the G-code's `Tn` tool once the startup commands execute.
