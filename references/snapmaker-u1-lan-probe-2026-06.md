# Snapmaker U1 LAN/Moonraker probe notes

Session-specific facts from the first safe probe of the operator's Snapmaker U1.

## Read-only discovery result

- U1 LAN IP used in probe: `192.168.86.34`.
- Open ports observed from Hermes container:
  - `80` — Fluidd/web UI.
  - `7125` — Moonraker API.
- Moonraker read-only endpoints worked without auth from the trusted LAN/container:
  - `/server/info`
  - `/printer/info`
  - `/printer/objects/query?...`
  - `/server/files/list?root=gcodes`
  - `/server/files/list?root=camera`
  - `/server/files/roots`
- Reported stack:
  - `moonraker_version: 1.4.1`
  - `api_version_string: 1.4.0`
  - `klippy_state: ready`
  - printer hostname: `lava`
  - printer software: `1.4.1.6_20260608141446`

## File roots

- `gcodes` root is read/write at `/userdata/gcodes`.
- `camera` root is read-only at `/oem/printer_data/camera`.
- `config` and `logs` roots were read-only.

## Existing uploaded-file behavior

The printer exposes useful G-code metadata via Moonraker metadata. Example metadata fields returned for SnapmakerOrca-sliced files:

- slicer / slicer_version
- estimated_time
- layer_count / object_height
- nozzle_diameter
- layer_height / first_layer_height
- first_layer_extr_temp / first_layer_bed_temp
- filament_type / filament_name
- filament_weight_total
- thumbnails

This makes pre-start readiness cards feasible for already-uploaded files.

## Camera caveat

`camera/monitor.jpg` existed but was stale during the first probe: the file mtime was many hours older than the current check. Do not use it as bed-clear evidence unless its timestamp is fresh enough. If stale, require explicit user confirmation that the bed is clear.

## First helper script

A read-only helper was created at:

```text
<scripts-dir>/snapmaker_u1_status.py
```

It queries only GET/read endpoints and can optionally download `camera/monitor.jpg`. It must not be extended with movement/heating/upload/start behavior unless those paths are separately approval-gated.

## Safe next test pattern

1. Read status and metadata.
2. If testing writes, do upload-only with a known-good small G-code file under a harmless test filename.
3. Verify the uploaded file appears in `gcodes`.
4. Do not start until the explicit start phrase is received and bed/material/tool gates pass.

## Start-gate phrase example

Use exact approval wording for physical starts, e.g.:

```text
START U1 LAST PRINT — bed clear, filament correct
```

Before start, re-check printer state and file metadata immediately. If the last-used file contains multi-material/tool metadata such as `PETG;PLA;PLA;PETG`, require the operator to confirm loaded filament/tool mapping is still valid.
