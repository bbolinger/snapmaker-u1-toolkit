#!/usr/bin/env python3
"""Read-only Snapmaker U1 / Moonraker status probe.

No motion, heat, upload, delete, or print-start operations. This script only
queries Moonraker HTTP GET endpoints and optionally downloads the current camera
snapshot from the read-only camera root.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from u1_config import get_u1_host, get_u1_port, get_data_dir


def _print_friendly_connect_error(host: str, port: int, exc: Exception) -> None:
    """First-run UX: replace the urllib traceback with one actionable line
    so a community user knows it's a connection problem, not a crash.
    (Hermes finding F7.)"""
    print(
        f"Could not connect to Snapmaker U1 at {host}:{port}: {exc}\n"
        "  • Check the printer is on and on your LAN.\n"
        "  • Edit .env (or set SNAPMAKER_U1_HOST), or pass --host <ip>.\n"
        "  • The default in .env.example (192.168.1.100) is a placeholder.",
        file=sys.stderr,
    )

UA = "Hermes Snapmaker U1 read-only status probe"


def get_json(base: str, path: str, timeout: int = 8) -> Any:
    req = urllib.request.Request(base + path, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return data.get("result", data)


def download(base: str, path: str, dest: Path, timeout: int = 10) -> Path:
    req = urllib.request.Request(base + path, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.read())
    return dest


def safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only Snapmaker U1 status")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--json", action="store_true", help="emit full JSON summary")
    ap.add_argument("--snapshot", action="store_true", help="download camera/monitor.jpg if present")
    ap.add_argument("--snapshot-dir", default=None,
                    help="snapshot output dir; defaults to <data-dir>/monitoring")
    args = ap.parse_args()

    host = args.host or get_u1_host()
    port = args.port if args.port is not None else get_u1_port()
    snapshot_dir = args.snapshot_dir or str(get_data_dir() / "monitoring")
    args.snapshot_dir = snapshot_dir  # so any downstream reads still work

    base = f"http://{host}:{port}"
    summary: dict[str, Any] = {
        "host": host,
        "port": port,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "read_only": True,
    }

    try:
        server = get_json(base, "/server/info")
        printer = get_json(base, "/printer/info")
        objects = get_json(base, "/printer/objects/query?print_stats&virtual_sdcard&pause_resume&webhooks&toolhead&extruder&extruder1&extruder2&extruder3&heater_bed")
        roots = get_json(base, "/server/files/roots")
        files = get_json(base, "/server/files/list?root=gcodes")
        camera_files = get_json(base, "/server/files/list?root=camera")
    except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout, OSError) as exc:
        _print_friendly_connect_error(host, port, exc)
        return 2

    status = objects.get("status", {})
    print_stats = status.get("print_stats", {})
    toolhead = status.get("toolhead", {})
    bed = status.get("heater_bed", {})
    virtual_sdcard = status.get("virtual_sdcard", {})
    pause_resume = status.get("pause_resume", {})
    webhooks = status.get("webhooks", {})

    extruders = {}
    for name in ("extruder", "extruder1", "extruder2", "extruder3"):
        obj = status.get(name)
        if obj:
            extruders[name] = {
                "temperature": safe_float(obj.get("temperature")),
                "target": safe_float(obj.get("target")),
                "state": obj.get("state"),
                "nozzle_diameter": obj.get("nozzle_diameter"),
            }

    recent_gcodes = sorted(
        files if isinstance(files, list) else [],
        key=lambda f: f.get("modified") or 0,
        reverse=True,
    )[:10]
    camera_list = camera_files if isinstance(camera_files, list) else []
    monitor = next((f for f in camera_list if f.get("path") == "monitor.jpg"), None)

    summary.update({
        "server": {
            "klippy_connected": server.get("klippy_connected"),
            "klippy_state": server.get("klippy_state"),
            "moonraker_version": server.get("moonraker_version"),
            "api_version_string": server.get("api_version_string"),
            "failed_components": server.get("failed_components"),
            "warnings": server.get("warnings"),
        },
        "printer": {
            "state": printer.get("state"),
            "state_message": printer.get("state_message"),
            "hostname": printer.get("hostname"),
            "software_version": printer.get("software_version"),
        },
        "print_stats": {
            "state": print_stats.get("state"),
            "filename": print_stats.get("filename"),
            "message": print_stats.get("message"),
            "current_layer": (print_stats.get("info") or {}).get("current_layer"),
            "total_layer": (print_stats.get("info") or {}).get("total_layer"),
            "is_active": virtual_sdcard.get("is_active"),
            "is_paused": pause_resume.get("is_paused"),
        },
        "toolhead": {
            "homed_axes": toolhead.get("homed_axes"),
            "active_extruder": toolhead.get("extruder"),
            "position": toolhead.get("position"),
            "axis_minimum": toolhead.get("axis_minimum"),
            "axis_maximum": toolhead.get("axis_maximum"),
        },
        "bed": {
            "temperature": safe_float(bed.get("temperature")),
            "target": safe_float(bed.get("target")),
        },
        "extruders": extruders,
        "roots": roots,
        "recent_gcodes": recent_gcodes,
        "camera_monitor": monitor,
        # `safe_to_upload` must mirror u1_upload_gcode.ensure_idle_ready() —
        # otherwise the read-only probe says 'safe' in states where the real
        # upload gate would block, which is misleading. (Hermes finding F4.)
        "safe_to_upload": (
            printer.get("state") == "ready"
            and webhooks.get("state") in {None, "ready"}
            and not pause_resume.get("is_paused")
            and not virtual_sdcard.get("is_active")
            and print_stats.get("state") in {None, "standby", "complete", "cancelled", "error", "ready"}
            and not any(
                float((status.get(name) or {}).get("target") or 0) > 0
                for name in ("heater_bed", "extruder", "extruder1", "extruder2", "extruder3")
            )
        ),
        "safe_to_start_requires_user_approval": True,
    })

    if args.snapshot and monitor:
        dest = Path(args.snapshot_dir) / f"monitor_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.jpg"
        try:
            # Moonraker file download endpoint for the read-only camera root.
            download(base, "/server/files/camera/" + urllib.parse.quote("monitor.jpg"), dest)
            summary["snapshot_path"] = str(dest)
        except Exception as exc:
            summary["snapshot_error"] = str(exc)

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"Snapmaker U1 @ {host}:{port}")
        print(f"- moonraker: {summary['server']['moonraker_version']} / klippy={summary['server']['klippy_state']}")
        print(f"- printer: {summary['printer']['state']} — {summary['printer']['state_message']}")
        print(f"- print: {summary['print_stats']['state']} — {summary['print_stats']['filename']}")
        print(f"- bed: {summary['bed']['temperature']}°C target {summary['bed']['target']}°C")
        for name, ex in extruders.items():
            print(f"- {name}: {ex['temperature']}°C target {ex['target']}°C state {ex['state']} nozzle {ex['nozzle_diameter']}")
        print(f"- homed axes: {summary['toolhead']['homed_axes']!r}")
        print(f"- gcodes rw root present: {any(r.get('name') == 'gcodes' and 'w' in r.get('permissions','') for r in roots)}")
        print(f"- camera monitor.jpg: {'yes' if monitor else 'no'}")
        print(f"- safe to upload: {summary['safe_to_upload']}")
        print("- safe to start: requires explicit the operator approval")
        if summary.get("snapshot_path"):
            print(f"- snapshot: {summary['snapshot_path']}")
        if summary.get("snapshot_error"):
            print(f"- snapshot error: {summary['snapshot_error']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
