# Snapmaker U1 filament detection API notes — 2026-06

## Source-code/docs scrub

Cloned/read locally under `<research-dir>/snapmaker-u1/SnapmakerU1-Extended-Firmware`.

Relevant upstream docs/files:

- `docs/rfid_support.md`
- `docs/design/filament_detect.md`
- `overlays/firmware-extended/13-patch-rfid/test/filament_detect.py`
- `overlays/firmware-extended/13-patch-rfid/test/filament_detect_test.py`

## Read-only endpoints/objects

The U1 exposes filament state through standard Moonraker object queries:

```text
/printer/objects/query?filament_detect
/printer/objects/query?print_task_config
```

Useful additional physical presence/sensor objects:

```text
filament_motion_sensor e0_filament
filament_motion_sensor e1_filament
filament_motion_sensor e2_filament
filament_motion_sensor e3_filament
filament_entangle_detect e0_filament
filament_entangle_detect e1_filament
filament_entangle_detect e2_filament
filament_entangle_detect e3_filament
filament_feed left
filament_feed right
```

Object names with spaces must be URL-encoded in Moonraker query strings.

## Channel mapping observed/used

```text
extruder  -> channel 0 / e0
extruder1 -> channel 1 / e1
extruder2 -> channel 2 / e2
extruder3 -> channel 3 / e3
```

Do not infer active print head from channel arrays alone. Use `toolhead.extruder` for the active tool, then map that object name to the channel index above.

## `filament_detect`

`filament_detect.info[channel]` exposes RFID/tag data. Important fields:

- `VENDOR`
- `MANUFACTURER`
- `MAIN_TYPE` — material, e.g. `PLA`, `PETG`
- `SUB_TYPE`
- `RGB_1` / `ARGB_COLOR`
- `HOTEND_MIN_TEMP`, `HOTEND_MAX_TEMP`
- `BED_TEMP`
- `FIRST_LAYER_TEMP`, `OTHER_LAYER_TEMP`
- `OFFICIAL`
- `CARD_UID` — tag presence/UID; may be `0` when absent/unknown

The extended firmware docs say tag data is read when filament is loaded and clears when removed. `CARD_UID` indicates physical tag presence independent of decoded material data.

Manual G-code commands documented upstream:

```text
FILAMENT_DT_UPDATE CHANNEL=<n>
FILAMENT_DT_CLEAR CHANNEL=<n>
FILAMENT_DT_QUERY CHANNEL=<n>
```

Hermes should not issue these automatically unless explicitly authorized; read-only object queries are enough for gating.

## `print_task_config`

`print_task_config` mirrors the UI-visible filament table and is the better normal source for what Snapmaker currently thinks is loaded/configured:

- `filament_vendor[channel]`
- `filament_type[channel]`
- `filament_sub_type[channel]`
- `filament_color_rgba[channel]`
- `filament_official[channel]`
- `filament_exist[channel]`
- `filament_edit[channel]`
- `filament_soft[channel]`
- `extruders_used[channel]`
- `extruder_map_table`
- `auto_replenish_filament`
- `filament_entangle_detect`

## Live U1 observation

A live read-only probe showed:

```text
channel 0 / extruder:  Snapmaker PETG, loaded true
channel 1 / extruder1: Generic PETG, loaded true, active print head
channel 2 / extruder2: Snapmaker PLA SnapSpeed, loaded true, official RFID true
channel 3 / extruder3: Polymaker PLA Polylite, loaded true
```

`filament_detect` only had official decoded RFID data for channel 2 in that probe, while `print_task_config` had material rows for all four channels. Therefore:

1. Use `print_task_config` for UI/current loaded material gating.
2. Include `filament_detect` as tag/RFID evidence when present.
3. Include motion/feed sensors as physical-presence checks.
4. Fail closed if requested material mismatches the intended tool or any presence sensor says not loaded.

## Implemented in Hermes

`<scripts-dir>/u1_toolmap.py` now queries `filament_detect`, `print_task_config`, `filament_motion_sensor eN_filament`, `filament_entangle_detect eN_filament`, and `filament_feed left/right`.

Material gate behavior now uses printer-reported `print_task_config.filament_type[channel]` first, then local declared map only as fallback. Live tests:

- `--requested-material PETG --intended-tool extruder1` passes because printer reports channel 1 as PETG and loaded.
- `--requested-material PLA --intended-tool extruder1` blocks with material mismatch because channel 1 is PETG.
