# Snapmaker U1 tool/material map script — 2026-06

## What was built

`<scripts-dir>/u1_toolmap.py` is a read-only U1 multi-tool probe and material-gate helper.

It queries:

- `toolhead.extruder` for the actual active tool object.
- `extruder`, `extruder1`, `extruder2`, `extruder3` for all nozzle/tool states.
- `print_stats`, `virtual_sdcard`, `display_status`, `pause_resume`, and `heater_bed` for context.

It writes:

- `<data-dir>/snapmaker_u1/latest_toolmap.json`
- `<data-dir>/snapmaker_u1/latest_toolmap.txt`
- `<data-dir>/snapmaker_u1/u1_tool_material_map.json` if missing, or when explicitly updated.

## Commands

Read-only probe:

```bash
python3 <scripts-dir>/u1_toolmap.py
```

Check that a requested material matches the active tool's declared material:

```bash
python3 <scripts-dir>/u1_toolmap.py --requested-material PETG
```

Check a specific intended tool:

```bash
python3 <scripts-dir>/u1_toolmap.py --requested-material PETG --intended-tool extruder1
```

Declare/update a confirmed tool material mapping, then run the probe:

```bash
python3 <scripts-dir>/u1_toolmap.py --set-tool extruder1 --set-material PETG --set-color 'black or unknown' --confirmed-by the operator
```

## Safety behavior

- The script performs no movement, heating, G-code, upload, start/resume/cancel, or printer writes.
- Material-gated control is blocked if the requested material cannot be verified against the declared tool map.
- `unknown` material intentionally fails closed.
- The current U1 quirk is handled: the plain `extruder` object may be parked/cold while the actual active print head is `extruder1`/`extruder2`/`extruder3`; always use `toolhead.extruder` first.

## Observed verification

A live read-only run during an active print reported:

```text
Active toolhead: extruder1
extruder: 38/0°C PARKED
extruder1: 240/240°C ACTIVATE
extruder2: 38/0°C PARKED
extruder3: 38/0°C PARKED
```

The material gate correctly blocked PETG control while `extruder1` material remained `unknown` in `<data-dir>/snapmaker_u1/u1_tool_material_map.json`.

## Next integration step

The upload-only script should call or import equivalent logic before accepting a requested material/tool:

- Resolve intended tool.
- Check declared material is known and matches requested material.
- Refuse upload/start paths if mismatch or unknown.
- Keep start approval separately gated; this script only supplies mapping evidence.
