#!/usr/bin/env python3
"""Read-only Snapmaker U1 multi-tool/material mapping probe.

No movement, heating, G-code, upload, start/resume/cancel, or printer writes.

Purpose:
- Query the U1's active tool from toolhead.extruder.
- Query all extruder objects so the plain parked `extruder` object is not mistaken
  for the actual hot/active print head.
- Optionally validate a declared material/tool map before future slice/upload/start flows.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from u1_config import get_u1_host, get_u1_port, get_data_dir


def _default_map_path() -> Path:
    """Lazy default — resolved on call, not import. Tracks env changes."""
    return get_data_dir() / "u1_tool_material_map.json"

EXTRUDER_NAMES = ["extruder", "extruder1", "extruder2", "extruder3"]
EXTRUDER_CHANNEL = {"extruder": 0, "extruder1": 1, "extruder2": 2, "extruder3": 3}
CHANNEL_EXTRUDER = {v: k for k, v in EXTRUDER_CHANNEL.items()}


def http_json(url: str, timeout: float = 8.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def rounded(value: Any, ndigits: int = 2) -> Any:
    if isinstance(value, (int, float)):
        return round(float(value), ndigits)
    return value


def _default_map() -> dict[str, Any]:
    """Empty/unknown map — every tool gates closed until the operator confirms."""
    return {
        "schema": "snapmaker-u1-tool-material-map/v1",
        "updated_at_utc": None,
        "notes": "Edit materials/tool labels as the operator confirms spool locations. Unknown blocks material-gated control.",
        "tools": {name: {"label": name, "material": "unknown", "color": "unknown", "confirmed_by": None} for name in EXTRUDER_NAMES},
    }


def load_material_map(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _default_map()
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError(f"material map root must be a JSON object, got {type(data).__name__}")
    except (json.JSONDecodeError, ValueError, OSError) as e:
        # Fail closed: return the unknown-everywhere default so every gate
        # check denies. Logging via stderr so the operator notices.
        print(f"WARNING: material map at {path} is unreadable ({e}); "
              f"using fail-closed default (all tools = unknown material)", file=sys.stderr)
        return _default_map()
    data.setdefault("tools", {})
    for name in EXTRUDER_NAMES:
        data["tools"].setdefault(name, {"label": name, "material": "unknown", "color": "unknown", "confirmed_by": None})
    return data


def query_u1(host: str, port: int, timeout: float) -> dict[str, Any]:
    base = f"http://{host}:{port}"
    objects = [
        "print_stats", "virtual_sdcard", "display_status", "pause_resume", "heater_bed", "toolhead",
        "filament_detect", "print_task_config", "filament_parameters",
        *EXTRUDER_NAMES,
        "filament_motion_sensor e0_filament", "filament_motion_sensor e1_filament",
        "filament_motion_sensor e2_filament", "filament_motion_sensor e3_filament",
        "filament_entangle_detect e0_filament", "filament_entangle_detect e1_filament",
        "filament_entangle_detect e2_filament", "filament_entangle_detect e3_filament",
        "filament_feed left", "filament_feed right",
    ]
    query = "&".join(urllib.parse.quote(o, safe="") for o in objects)
    server_info = http_json(f"{base}/server/info", timeout)["result"]
    printer_info = http_json(f"{base}/printer/info", timeout)["result"]
    status = http_json(f"{base}/printer/objects/query?{query}", timeout)["result"]["status"]
    return {"server_info": server_info, "printer_info": printer_info, "status": status}


def summarize(raw: dict[str, Any], material_map: dict[str, Any], requested_material: str | None = None, intended_tool: str | None = None) -> dict[str, Any]:
    status = raw["status"]
    toolhead = status.get("toolhead", {})
    active_name = toolhead.get("extruder") or "unknown"
    map_tools = material_map.get("tools", {})

    fd = status.get("filament_detect", {})
    fd_info = fd.get("info", []) if isinstance(fd.get("info"), list) else []
    fd_state = fd.get("state", []) if isinstance(fd.get("state"), list) else []
    ptc = status.get("print_task_config", {})

    def list_get(values: Any, idx: int, default: Any = None) -> Any:
        return values[idx] if isinstance(values, list) and idx < len(values) else default

    tools: dict[str, Any] = {}
    for name in EXTRUDER_NAMES:
        ch = EXTRUDER_CHANNEL[name]
        ext = status.get(name, {})
        declared = map_tools.get(name, {})
        rfid = fd_info[ch] if ch < len(fd_info) and isinstance(fd_info[ch], dict) else {}
        detected_motion = status.get(f"filament_motion_sensor e{ch}_filament", {})
        entangle = status.get(f"filament_entangle_detect e{ch}_filament", {})
        feed_side = "left" if ch in {0, 1} else "right"
        feed_key = f"extruder{ch}"
        feed = status.get(f"filament_feed {feed_side}", {}).get(feed_key, {})
        printer_material = list_get(ptc.get("filament_type"), ch, "unknown")
        printer_vendor = list_get(ptc.get("filament_vendor"), ch, "unknown")
        printer_subtype = list_get(ptc.get("filament_sub_type"), ch, "")
        printer_color = list_get(ptc.get("filament_color_rgba"), ch, "unknown")
        printer_official = list_get(ptc.get("filament_official"), ch)
        printer_exists = list_get(ptc.get("filament_exist"), ch)
        tools[name] = {
            "channel": ch,
            "declared": {
                "label": declared.get("label", name),
                "material": declared.get("material", "unknown"),
                "color": declared.get("color", "unknown"),
                "confirmed_by": declared.get("confirmed_by"),
            },
            "printer_reported": {
                "vendor": printer_vendor,
                "material": printer_material,
                "subtype": printer_subtype,
                "color_rgba": printer_color,
                "official": printer_official,
                "exists": printer_exists,
                "edit": list_get(ptc.get("filament_edit"), ch),
                "soft": list_get(ptc.get("filament_soft"), ch),
            },
            "rfid": {
                "vendor": rfid.get("VENDOR"),
                "manufacturer": rfid.get("MANUFACTURER"),
                "material": rfid.get("MAIN_TYPE"),
                "subtype": rfid.get("SUB_TYPE"),
                "official": rfid.get("OFFICIAL"),
                "card_uid": rfid.get("CARD_UID"),
                "hotend_min_c": rfid.get("HOTEND_MIN_TEMP"),
                "hotend_max_c": rfid.get("HOTEND_MAX_TEMP"),
                "bed_temp_c": rfid.get("BED_TEMP"),
                "first_layer_temp_c": rfid.get("FIRST_LAYER_TEMP"),
                "other_layer_temp_c": rfid.get("OTHER_LAYER_TEMP"),
                "weight_g": rfid.get("WEIGHT"),
                "diameter_raw": rfid.get("DIAMETER"),
                "state": list_get(fd_state, ch),
            },
            "filament_sensors": {
                "motion_detected": detected_motion.get("filament_detected"),
                "motion_enabled": detected_motion.get("enabled"),
                "feed_detected": feed.get("filament_detected"),
                "feed_channel_state": feed.get("channel_state"),
                "feed_channel_error": feed.get("channel_error"),
                "feed_action_state": feed.get("channel_action_state"),
                "entangle_detect_factor": entangle.get("detect_factor"),
            },
            "is_active_toolhead": name == active_name,
            "temperature_c": rounded(ext.get("temperature"), 1),
            "target_c": rounded(ext.get("target"), 1),
            "power": rounded(ext.get("power"), 3),
            "state": ext.get("state"),
            "can_extrude": ext.get("can_extrude"),
            "extruder_index": ext.get("extruder_index"),
            "nozzle_diameter": ext.get("nozzle_diameter"),
            "active_pin": ext.get("active_pin"),
            "park_pin": ext.get("park_pin"),
            "grab_valid_pin": ext.get("grab_valid_pin"),
            "switch_count": ext.get("switch_count"),
            "retry_count": ext.get("retry_count"),
            "error_count": ext.get("error_count"),
            "pressure_advance": ext.get("pressure_advance"),
            "offset": [rounded(v, 4) for v in ext.get("extruder_offset", [])] if isinstance(ext.get("extruder_offset"), list) else ext.get("extruder_offset"),
            "real_extruder_stats": ext.get("real_extruder_stats"),
        }

    ps = status.get("print_stats", {})
    vsd = status.get("virtual_sdcard", {})
    display = status.get("display_status", {})
    bed = status.get("heater_bed", {})
    progress = vsd.get("progress")
    if progress is None:
        progress = display.get("progress")

    gates: list[str] = []
    warnings: list[str] = []
    active_tool = tools.get(active_name, {})

    if active_name not in EXTRUDER_NAMES:
        warnings.append(f"active toolhead is not one of expected U1 objects: {active_name!r}")
    if active_tool and active_tool.get("state") != "ACTIVATE" and vsd.get("is_active"):
        warnings.append(f"active print but active tool {active_name} state is {active_tool.get('state')!r}")

    requested_norm = requested_material.strip().upper() if requested_material else None
    intended_tool_norm = intended_tool.strip() if intended_tool else None
    if requested_norm:
        check_tool = intended_tool_norm or active_name
        if check_tool not in tools:
            gates.append(f"requested material check failed: intended tool {check_tool!r} not found")
        else:
            tool = tools[check_tool]
            printer_material = (tool.get("printer_reported", {}).get("material") or "unknown").upper()
            declared_material = (tool.get("declared", {}).get("material") or "unknown").upper()
            detected_material = printer_material if printer_material not in {"", "UNKNOWN", "NONE"} else declared_material
            sensors = tool.get("filament_sensors", {})
            exists = tool.get("printer_reported", {}).get("exists")
            if exists is False or sensors.get("motion_detected") is False or sensors.get("feed_detected") is False:
                gates.append(f"requested material {requested_norm} blocked: {check_tool} filament presence sensor says not loaded")
            elif detected_material in {"", "UNKNOWN", "NONE"}:
                gates.append(f"requested material {requested_norm} cannot be verified: {check_tool} material is unknown")
            elif detected_material != requested_norm:
                gates.append(f"requested material {requested_norm} does not match {check_tool} detected material {detected_material}")

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "printer": {
            "host": raw.get("printer_info", {}).get("hostname"),
            "ip": raw.get("printer_info", {}).get("address") or get_u1_host(),
            "state": raw.get("printer_info", {}).get("state"),
            "klippy_state": raw.get("server_info", {}).get("klippy_state"),
            "moonraker_version": raw.get("server_info", {}).get("moonraker_version"),
        },
        "job": {
            "state": ps.get("state"),
            "filename": ps.get("filename"),
            "progress_percent": rounded(progress * 100, 1) if isinstance(progress, (int, float)) else None,
            "layer": (ps.get("info") or {}).get("current_layer"),
            "total_layer": (ps.get("info") or {}).get("total_layer"),
            "is_active": vsd.get("is_active"),
            "is_paused": status.get("pause_resume", {}).get("is_paused"),
        },
        "bed": {"temperature_c": rounded(bed.get("temperature"), 1), "target_c": rounded(bed.get("target"), 1)},
        "toolhead": {
            "active_extruder": active_name,
            "position": [rounded(v, 2) for v in toolhead.get("position", [])] if isinstance(toolhead.get("position"), list) else toolhead.get("position"),
            "homed_axes": toolhead.get("homed_axes"),
        },
        "tools": tools,
        "material_map_path": str(_default_map_path()),
        "gates": gates,
        "warnings": warnings,
        "safe_for_material_gated_control": not gates,
    }


def render(summary: dict[str, Any]) -> str:
    j = summary["job"]
    bed = summary["bed"]
    lines = [
        "U1 tool map:",
        f"- Printer: {summary['printer'].get('state')} / Klippy {summary['printer'].get('klippy_state')}",
        f"- Job: {j.get('state')} — {j.get('filename') or 'none'} — {j.get('progress_percent')}% layer {j.get('layer')}/{j.get('total_layer')}",
        f"- Bed: {bed.get('temperature_c')}/{bed.get('target_c')}°C",
        f"- Active toolhead: {summary['toolhead'].get('active_extruder')}",
        "- Tools:",
    ]
    for name, t in summary["tools"].items():
        mark = "*" if t.get("is_active_toolhead") else " "
        d = t.get("declared", {})
        pr = t.get("printer_reported", {})
        fs = t.get("filament_sensors", {})
        lines.append(
            f"  {mark} {name}/ch{t.get('channel')}: {t.get('temperature_c')}/{t.get('target_c')}°C {t.get('state')} "
            f"printer={pr.get('vendor')} {pr.get('material')} {pr.get('subtype')} color={pr.get('color_rgba')} "
            f"loaded={pr.get('exists')}/{fs.get('motion_detected')}/{fs.get('feed_detected')} "
            f"declared={d.get('material')} active_pin={t.get('active_pin')} park_pin={t.get('park_pin')} "
            f"err/retry={t.get('error_count')}/{t.get('retry_count')}"
        )
    if summary.get("gates"):
        lines.append("- Gates blocking material-gated control: " + "; ".join(summary["gates"]))
    if summary.get("warnings"):
        lines.append("- Warnings: " + "; ".join(summary["warnings"]))
    lines.append(f"- Material map: {summary.get('material_map_path')}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Snapmaker U1 multi-tool/material map probe")
    # Defaults are None so the host/port lookup only happens at CLI parse-time
    # via the env/config layer — module import no longer requires a working config.
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--map", dest="map_path", default=None, help="material map JSON path")
    parser.add_argument("--requested-material", help="optional gate check, e.g. PETG")
    parser.add_argument("--intended-tool", help="optional gate check tool, e.g. extruder1; defaults active tool")
    parser.add_argument("--set-tool", choices=EXTRUDER_NAMES, help="update declared material map for this tool, then still run read-only probe")
    parser.add_argument("--set-material", help="material to store with --set-tool, e.g. PETG, PLA, unknown")
    parser.add_argument("--set-color", default=None, help="optional color/spool note to store with --set-tool")
    parser.add_argument("--confirmed-by", default="the operator", help="who confirmed --set-tool mapping")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    # Resolve host/port/data-dir lazily so missing config only fails on run, not import.
    host = args.host or get_u1_host()
    port = args.port if args.port is not None else get_u1_port()
    outdir = get_data_dir()
    outdir.mkdir(parents=True, exist_ok=True)
    map_path = Path(args.map_path) if args.map_path else _default_map_path()
    material_map = load_material_map(map_path)
    if args.set_tool:
        if not args.set_material:
            raise SystemExit("--set-tool requires --set-material")
        tool_entry = material_map.setdefault("tools", {}).setdefault(args.set_tool, {"label": args.set_tool})
        tool_entry["material"] = args.set_material.strip().upper() if args.set_material else "unknown"
        if args.set_color is not None:
            tool_entry["color"] = args.set_color.strip() or "unknown"
        tool_entry["confirmed_by"] = args.confirmed_by
        tool_entry["confirmed_at_utc"] = datetime.now(timezone.utc).isoformat()
        material_map["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    if args.set_tool or not map_path.exists():
        map_path.write_text(json.dumps(material_map, indent=2, sort_keys=True) + "\n")

    raw = query_u1(host, port, args.timeout)
    summary = summarize(raw, material_map, args.requested_material, args.intended_tool)

    (outdir / "latest_toolmap.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    (outdir / "latest_toolmap.txt").write_text(render(summary) + "\n")

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(render(summary))
        print(f"\nArtifacts: {outdir / 'latest_toolmap.json'} | {outdir / 'latest_toolmap.txt'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"u1_toolmap failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
