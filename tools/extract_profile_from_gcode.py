#!/usr/bin/env python3
"""Extract Snapmaker Orca process + filament profiles from a successful G-code file.

Use this when you have a G-code file that printed well on your U1 and you want to
capture its settings as reusable profile JSONs — so future slicing for the same
filament-in-the-same-slot reuses physics-validated values.

Reads the trailing `; key = value` metadata block OrcaSlicer/PrusaSlicer write to
G-code, and emits two JSON files in the shape Snapmaker Orca expects:

  - process JSON   (layer_height, walls, infill, bed type, supports, etc.)
  - filament JSON  (filament_type, vendor, nozzle temp, plate temps, diameter)

Pure stdlib — no Hermes/toolkit dependency. Run anywhere Python 3.9+ runs.

Example:
    python3 tools/extract_profile_from_gcode.py \\
        my_print.gcode \\
        --process-out profiles/my_extruder1_petg_process.json \\
        --filament-out profiles/my_extruder1_sunlu_black_petg_filament.json \\
        --process-name "My 0.20 PETG Extruder1" \\
        --filament-name "My Generic PETG SUNLU Black Extruder1" \\
        --vendor SUNLU --brand-label "SUNLU Black PETG"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


# ---------- G-code metadata parsing ----------

# Keys we'll pull from the G-code metadata block. Mirrors the "wanted" list in
# u1_upload_gcode.parse_gcode_metadata, plus the slicer keys we actually use to
# reconstruct profiles. Keep this list broad — missing keys are silently skipped.
GCODE_KEYS = [
    # process
    "printer_settings_id", "print_settings_id", "nozzle_diameter",
    "layer_height", "first_layer_height",
    "wall_loops", "wall_generator",
    "sparse_infill_density", "sparse_infill_pattern",
    "top_shell_layers", "bottom_shell_layers",
    "top_surface_pattern", "bottom_surface_pattern",
    "enable_support", "support_type", "support_threshold_angle",
    "support_filament", "support_interface_filament",
    "brim_type", "brim_width", "raft_layers",
    "curr_bed_type", "bed_temperature", "first_layer_bed_temperature",
    "skirt_loops",
    # filament
    "filament_type", "filament_vendor", "filament_settings_id",
    "filament_diameter", "filament_flow_ratio", "filament_density",
    "filament_cost", "filament_id",
    "nozzle_temperature", "nozzle_temperature_initial_layer",
    "first_layer_temperature",
    "hot_plate_temp", "hot_plate_temp_initial_layer",
    "textured_plate_temp", "textured_plate_temp_initial_layer",
    "cool_plate_temp", "cool_plate_temp_initial_layer",
    "eng_plate_temp", "eng_plate_temp_initial_layer",
    "fan_max_speed", "fan_min_speed",
    "pressure_advance", "enable_pressure_advance",
    "filament_retraction_length", "filament_retraction_speed",
    "filament_max_volumetric_speed",
    # informational
    "estimated printing time (normal mode)", "estimated printing time",
    "filament used [g]", "total filament used [g]",
]


_BOUNDARY = {" ", "\t", "="}


def _strip_quotes(value: str) -> str:
    """Strip a single matching pair of leading/trailing double-quotes.

    Orca quotes metadata values that contain spaces, e.g.:
        ; filament_settings_id = "My PETG @U1 Textured"
    Without this, the captured value would include the literal quote chars
    and produce an invalid profile name.
    """
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def parse_gcode_metadata(path: Path) -> dict[str, str]:
    """Pull `; key = value` slicer metadata from G-code head/tail.

    Slicers usually write the metadata block at the bottom; reading both ends
    of the file (512KB each) catches both layouts without loading the whole
    file into memory for huge prints.

    Key matching requires a word boundary (`=`, space, or tab) after the key
    name — otherwise `nozzle_temperature` would greedily match
    `nozzle_temperature_range_low` and overwrite the real value.
    """
    size = path.stat().st_size
    with path.open("rb") as f:
        head = f.read(512_000)
        if size > 512_000:
            f.seek(max(0, size - 512_000))
            tail = f.read(512_000)
        else:
            tail = b""
    text = (head + b"\n" + tail).decode("utf-8", "replace")
    meta: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith(";"):
            continue
        body = line[1:].strip()
        body_lower = body.lower()
        for key in GCODE_KEYS:
            klen = len(key)
            if len(body) <= klen:
                continue
            if body_lower[:klen] != key.lower():
                continue
            if body[klen] not in _BOUNDARY:
                continue
            if "=" in body:
                meta[key] = _strip_quotes(body.split("=", 1)[1].strip())
            else:
                meta[key] = body
            break
    return meta


# ---------- profile building ----------

# Process-profile keys are scalars (strings). Filament-profile keys are lists
# (single-item, since Snapmaker Orca uses list-shape for filament settings).
PROCESS_FIELDS_FROM_META = [
    "layer_height", "first_layer_height",
    "wall_loops", "wall_generator",
    "sparse_infill_density", "sparse_infill_pattern",
    "top_shell_layers", "bottom_shell_layers",
    "top_surface_pattern", "bottom_surface_pattern",
    "enable_support", "support_type", "support_threshold_angle",
    "brim_type", "brim_width", "raft_layers", "skirt_loops",
    "curr_bed_type",
]

FILAMENT_FIELDS_FROM_META = [
    "filament_type", "filament_vendor",
    "filament_diameter", "filament_flow_ratio", "filament_density",
    "filament_cost",
    "nozzle_temperature", "nozzle_temperature_initial_layer",
    "hot_plate_temp", "hot_plate_temp_initial_layer",
    "textured_plate_temp", "textured_plate_temp_initial_layer",
    "cool_plate_temp", "cool_plate_temp_initial_layer",
    "eng_plate_temp", "eng_plate_temp_initial_layer",
    "fan_max_speed", "fan_min_speed",
    "pressure_advance", "enable_pressure_advance",
    "filament_retraction_length", "filament_retraction_speed",
    "filament_max_volumetric_speed",
]


def build_process_profile(
    meta: dict[str, str],
    name: str,
    compatible_printer: str,
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a flat process profile (no inheritance) suitable for headless slicing."""
    profile: dict[str, Any] = {
        "type": "process",
        "name": name,
        "from": "user",
        "inherits": "",
        "instantiation": "true",
        "setting_id": "GP003",
        "compatible_printers": [compatible_printer],
        "print_settings_id": name,
    }
    for key in PROCESS_FIELDS_FROM_META:
        if key in meta:
            profile[key] = meta[key]
    # nozzle_diameter belongs to the printer config, but slicers also write
    # it into the per-process metadata. Multi-tool G-code emits it as a CSV
    # (e.g. "0.4,0.4,0.4,0.4") — we want the single per-extruder value here.
    if "nozzle_diameter" in meta:
        nd = meta["nozzle_diameter"].split(",")[0].strip()
        profile["nozzle_diameter"] = nd
    # first_layer_bed_temperature isn't a process field in Orca's schema —
    # it lives on the filament. Don't leak it into process.
    if overrides:
        profile.update(overrides)
    return profile


def build_filament_profile(
    meta: dict[str, str],
    name: str,
    compatible_printer: str,
    vendor: str | None = None,
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a filament profile in the list-valued shape Snapmaker Orca uses."""
    profile: dict[str, Any] = {
        "type": "filament",
        "name": name,
        "from": "user",
        "inherits": "",
        "instantiation": "true",
        "compatible_printers": [compatible_printer],
        "filament_settings_id": [name],
        "filament_diameter": ["1.75"],
    }
    # Snapmaker Orca filament profiles carry their own bed_type (the active
    # plate they were tuned for). Mirror curr_bed_type from the process meta.
    if "curr_bed_type" in meta:
        profile["bed_type"] = [meta["curr_bed_type"]]
    for key in FILAMENT_FIELDS_FROM_META:
        if key in meta:
            profile[key] = [meta[key]]
    # first_layer_temperature in G-code is the slicer's nozzle_temperature_initial_layer.
    # If the explicit initial-layer key wasn't present, fall back to first_layer_temperature.
    if "nozzle_temperature_initial_layer" not in profile and "first_layer_temperature" in meta:
        profile["nozzle_temperature_initial_layer"] = [meta["first_layer_temperature"]]
    # Likewise first_layer_bed_temperature → *_temp_initial_layer for the active bed type.
    bed = (meta.get("curr_bed_type") or "").lower()
    flbt = meta.get("first_layer_bed_temperature")
    if flbt:
        if "textured" in bed and "textured_plate_temp_initial_layer" not in profile:
            profile["textured_plate_temp_initial_layer"] = [flbt]
        elif "hot" in bed and "hot_plate_temp_initial_layer" not in profile:
            profile["hot_plate_temp_initial_layer"] = [flbt]
        elif "cool" in bed and "cool_plate_temp_initial_layer" not in profile:
            profile["cool_plate_temp_initial_layer"] = [flbt]
    # Override vendor explicitly if caller passed one (G-code often says "Generic").
    if vendor:
        profile["filament_vendor"] = [vendor]
    if overrides:
        for k, v in overrides.items():
            profile[k] = v if isinstance(v, list) else [v]
    return profile


# ---------- CLI ----------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Extract process + filament profiles from a successful G-code print.",
    )
    ap.add_argument("gcode", type=Path, help="Path to a successful G-code file.")
    ap.add_argument("--process-out", type=Path, default=None,
                    help="Write process profile JSON here (default: stdout).")
    ap.add_argument("--filament-out", type=Path, default=None,
                    help="Write filament profile JSON here (default: stdout).")
    ap.add_argument("--process-name", default=None,
                    help="Name for the process profile. Default: derived from G-code metadata.")
    ap.add_argument("--filament-name", default=None,
                    help="Name for the filament profile. Default: derived from G-code metadata.")
    ap.add_argument("--compatible-printer", default="Snapmaker U1 (0.4 nozzle)",
                    help="Profile compatible_printers value. Default: Snapmaker U1 (0.4 nozzle).")
    ap.add_argument("--vendor", default=None,
                    help="Override filament_vendor (G-code often says 'Generic'; set 'SUNLU', 'eSun', etc.).")
    ap.add_argument("--brand-label", default=None,
                    help="Optional human label appended to filament name (e.g. 'SUNLU Black').")
    ap.add_argument("--metadata-only", action="store_true",
                    help="Dump raw parsed G-code metadata and exit (debug aid).")
    args = ap.parse_args(argv)

    if not args.gcode.exists() or not args.gcode.is_file():
        print(f"G-code file not found: {args.gcode}", file=sys.stderr)
        return 2
    if args.gcode.suffix.lower() not in {".gcode", ".gco", ".gc"}:
        print(f"Refusing non-G-code file (suffix={args.gcode.suffix!r}): {args.gcode}",
              file=sys.stderr)
        return 2

    meta = parse_gcode_metadata(args.gcode)
    if not meta:
        print(f"No `; key = value` metadata found in {args.gcode}. "
              f"Is this an OrcaSlicer/PrusaSlicer-emitted G-code?", file=sys.stderr)
        return 3

    if args.metadata_only:
        print(json.dumps(meta, indent=2))
        return 0

    # Derive names if not provided. Prefer the slicer's own *_settings_id so
    # the extracted profile is recognizably tied to the source print.
    process_name = args.process_name or meta.get("print_settings_id") or "Extracted Process"
    filament_name = args.filament_name or meta.get("filament_settings_id") or "Extracted Filament"
    if args.brand_label and args.brand_label not in filament_name:
        filament_name = f"{filament_name} ({args.brand_label})"

    process = build_process_profile(meta, process_name, args.compatible_printer)
    filament = build_filament_profile(meta, filament_name, args.compatible_printer, vendor=args.vendor)

    if args.process_out:
        args.process_out.parent.mkdir(parents=True, exist_ok=True)
        args.process_out.write_text(json.dumps(process, indent=4), encoding="utf-8")
        print(f"Wrote process profile  → {args.process_out}")
    else:
        print("# Process profile")
        print(json.dumps(process, indent=4))

    if args.filament_out:
        args.filament_out.parent.mkdir(parents=True, exist_ok=True)
        args.filament_out.write_text(json.dumps(filament, indent=4), encoding="utf-8")
        print(f"Wrote filament profile → {args.filament_out}")
    else:
        print("# Filament profile")
        print(json.dumps(filament, indent=4))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
