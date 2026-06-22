#!/usr/bin/env python3
"""Snapmaker U1 read-only preflight summary.

Combines Moonraker state + camera freshness capture into one operator packet.
No movement, heating, G-code, start/resume/cancel, or write operations.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from u1_config import get_u1_host, get_u1_port, get_data_dir

# Camera helper sits next to this script — no hardcoded /opt/data path.
def _default_camera_helper() -> str:
    return str(Path(__file__).resolve().parent / "u1_camera.py")


def http_json(url: str, timeout: float = 8.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def iso(ts: float | int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def moonraker_base(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def query_printer(host: str, port: int) -> dict[str, Any]:
    base = moonraker_base(host, port)
    # U1 exposes multiple tool objects. The plain "extruder" object can be parked/cold
    # while the active print head is extruder1/extruder2/extruder3; always query all.
    query = "print_stats&heater_bed&extruder&extruder1&extruder2&extruder3&toolhead&display_status&virtual_sdcard&pause_resume&webhooks"
    server_info = http_json(f"{base}/server/info")["result"]
    printer_info = http_json(f"{base}/printer/info")["result"]
    objects = http_json(f"{base}/printer/objects/query?{query}")["result"]["status"]
    return {"server_info": server_info, "printer_info": printer_info, "objects": objects}


def run_camera_check(host: str, port: int, output_dir: Path, timeout: float) -> dict[str, Any]:
    out_img = output_dir / "latest_monitor.jpg"
    cmd = [sys.executable, _default_camera_helper(),
           "--host", host, "--port", str(port),
           "check", "--timeout", str(timeout), "--output", str(out_img)]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout + 15)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip() or proc.stdout.strip(), "command": cmd}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"camera helper returned invalid JSON: {exc}", "stdout": proc.stdout[-1000:], "stderr": proc.stderr[-1000:]}


def rounded(value: Any, ndigits: int = 1) -> Any:
    if isinstance(value, (int, float)):
        return round(float(value), ndigits)
    return value


def summarize(data: dict[str, Any], camera: dict[str, Any]) -> dict[str, Any]:
    obj = data["objects"]
    ps = obj.get("print_stats", {})
    bed = obj.get("heater_bed", {})
    toolhead = obj.get("toolhead", {})
    active_extruder_name = toolhead.get("extruder") or "extruder"
    ext = obj.get(active_extruder_name) or obj.get("extruder", {})
    display = obj.get("display_status", {})
    vsd = obj.get("virtual_sdcard", {})
    pause = obj.get("pause_resume", {})
    webhooks = obj.get("webhooks", {})

    print_state = ps.get("state") or webhooks.get("state") or "unknown"
    printer_state = data.get("printer_info", {}).get("state", "unknown")
    progress = display.get("progress")
    if progress is None:
        progress = vsd.get("progress")

    blockers: list[str] = []
    warnings: list[str] = []
    safe_next: list[str] = []

    if data.get("server_info", {}).get("klippy_state") != "ready" or printer_state != "ready":
        blockers.append(f"printer not ready: {printer_state}")
    if webhooks.get("state") and webhooks.get("state") != "ready":
        blockers.append(f"webhooks state: {webhooks.get('state')} — {webhooks.get('state_message', '')}".strip())
    if pause.get("is_paused"):
        blockers.append("print is paused")
    if print_state not in {"standby", "complete", "ready", "unknown"} and vsd.get("is_active"):
        warnings.append(f"active print/job state: {print_state}")
    if float(bed.get("target") or 0) > 0 or float(ext.get("target") or 0) > 0:
        warnings.append("heater target is non-zero")
    if camera.get("ok") is not True or camera.get("fresh") is not True:
        blockers.append("fresh camera capture failed")

    # The visual classifier is intentionally outside this script; fail closed until vision/human result is attached.
    visual = {
        "classification": "unknown",
        "reason": "fresh camera capture available, but visual bed clearance requires vision/human classification; fail closed until attached",
    }
    blockers.append("bed visual clearance is unknown")

    if blockers:
        safe_next.append("Do not start/resume automatically; resolve blockers or get explicit operator confirmation.")
    else:
        safe_next.append("Read-only preflight found no state blockers, but physical actions still require explicit approval.")
    safe_next.append("If the operator confirms bed clear, this can become a pre-start readiness hint, not an auto-start approval.")

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "printer": {
            "host": data.get("printer_info", {}).get("hostname"),
            "ip": get_u1_host(),
            "state": printer_state,
            "state_message": data.get("printer_info", {}).get("state_message"),
            "klippy_state": data.get("server_info", {}).get("klippy_state"),
            "moonraker_version": data.get("server_info", {}).get("moonraker_version"),
            "software_version": data.get("printer_info", {}).get("software_version"),
        },
        "job": {
            "state": print_state,
            "filename": ps.get("filename"),
            "progress_percent": rounded(progress * 100, 1) if isinstance(progress, (int, float)) else None,
            "is_active": vsd.get("is_active"),
            "is_paused": pause.get("is_paused"),
            "message": ps.get("message") or display.get("message"),
            "layer": (ps.get("info") or {}).get("current_layer"),
            "total_layer": (ps.get("info") or {}).get("total_layer"),
        },
        "temps": {
            "bed_c": rounded(bed.get("temperature")),
            "bed_target_c": rounded(bed.get("target")),
            "active_extruder": active_extruder_name,
            "extruder_c": rounded(ext.get("temperature")),
            "extruder_target_c": rounded(ext.get("target")),
            "extruder_state": ext.get("state"),
            "can_extrude": ext.get("can_extrude"),
        },
        "motion": {
            "homed_axes": toolhead.get("homed_axes"),
            "position": [rounded(v, 2) for v in toolhead.get("position", [])],
        },
        "camera": {
            "fresh": camera.get("fresh"),
            "ok": camera.get("ok"),
            "image": camera.get("image"),
            "monitor_modified_utc": (camera.get("monitor") or {}).get("modified_iso_utc") or iso((camera.get("monitor") or {}).get("modified")),
        },
        "visual_bed": visual,
        "blockers": blockers,
        "warnings": warnings,
        "safe_next_actions": safe_next,
        "raw_camera_packet": str(output_dir_path() / "latest_bed_check_capture.json"),
    }


def output_dir_path() -> Path:
    return get_data_dir()


def render_text(summary: dict[str, Any]) -> str:
    p = summary["printer"]
    j = summary["job"]
    t = summary["temps"]
    c = summary["camera"]
    v = summary["visual_bed"]
    lines = [
        "U1 preflight:",
        f"- Printer: {p.get('state')} / Klippy {p.get('klippy_state')} ({p.get('host') or p.get('ip')})",
        f"- Job: {j.get('state')} — {j.get('filename') or 'none'} — {j.get('progress_percent')}%",
        f"- Paused/active: paused={j.get('is_paused')} active={j.get('is_active')}",
        f"- Temps: bed {t.get('bed_c')}/{t.get('bed_target_c')}°C, {t.get('active_extruder') or 'nozzle'} {t.get('extruder_c')}/{t.get('extruder_target_c')}°C ({t.get('extruder_state')})",
        f"- Motion: homed_axes={summary['motion'].get('homed_axes')!r} pos={summary['motion'].get('position')}",
        f"- Camera: fresh={c.get('fresh')} image={c.get('image')}",
        f"- Bed visual: {v.get('classification')} — {v.get('reason')}",
    ]
    if summary.get("blockers"):
        lines.append("- Blockers: " + "; ".join(summary["blockers"]))
    if summary.get("warnings"):
        lines.append("- Warnings: " + "; ".join(summary["warnings"]))
    lines.append("- Next: " + " ".join(summary.get("safe_next_actions", [])))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Snapmaker U1 preflight")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--json", action="store_true", help="print JSON only")
    args = parser.parse_args()

    host = args.host or get_u1_host()
    port = args.port if args.port is not None else get_u1_port()
    outdir = get_data_dir()
    outdir.mkdir(parents=True, exist_ok=True)

    state = query_printer(host, port)
    camera = run_camera_check(host, port, outdir, args.timeout)
    (outdir / "latest_bed_check_capture.json").write_text(json.dumps(camera, indent=2, sort_keys=True))
    summary = summarize(state, camera)
    (outdir / "latest_preflight.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    (outdir / "latest_preflight.txt").write_text(render_text(summary) + "\n")

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(render_text(summary))
        print(f"\nArtifacts: {outdir / 'latest_preflight.json'} | {outdir / 'latest_preflight.txt'}")
    return 0 if camera.get("ok") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"u1_preflight failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
