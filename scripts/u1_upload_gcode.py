#!/usr/bin/env python3
"""Upload-only Snapmaker U1 G-code staging helper.

Safe behavior:
- validates basic G-code metadata
- checks printer is ready/idle/cool-ish
- checks requested material against U1 tool map helper
- uploads via Moonraker /server/files/upload with print=false
- verifies printer did not start
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from u1_config import get_u1_host, get_u1_port, get_data_dir

# Toolmap helper sits next to this script — derive at call time, no hardcoded path.
def _default_toolmap_path() -> str:
    return str(Path(__file__).resolve().parent / "u1_toolmap.py")


def http_json(url: str, timeout: float = 12.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def base_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


_META_BOUNDARY = {" ", "\t", "="}


def _strip_quotes(value: str) -> str:
    """Strip a single matching pair of surrounding double-quotes.

    Orca quotes values containing spaces (e.g. `filament_settings_id = "..."`);
    without this, the captured value would include the literal `"` characters.
    """
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def parse_gcode_metadata(path: Path) -> dict[str, Any]:
    # Read bounded head/tail so huge files don't become memory bombs.
    size = path.stat().st_size
    with path.open("rb") as f:
        head = f.read(512_000)
        if size > 512_000:
            f.seek(max(0, size - 512_000))
            tail = f.read(512_000)
        else:
            tail = b""
    text = (head + b"\n" + tail).decode("utf-8", "replace")
    wanted = [
        "printer_settings_id", "print_settings_id", "filament_type",
        "filament_settings_id", "curr_bed_type", "layer_height",
        "nozzle_diameter", "first_layer_bed_temperature", "first_layer_temperature",
        "bed_temperature", "nozzle_temperature", "filament used [g]",
        "total filament used [g]", "estimated printing time (normal mode)",
        "estimated printing time",
    ]
    meta: dict[str, str] = {}
    # Match requires a word boundary (space/tab/`=`) after the key — otherwise
    # `nozzle_temperature` greedily swallows the value of
    # `nozzle_temperature_range_low` (last write wins). Break on first match
    # so longer-overlapping keys assigned to the same slot don't double-fire.
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith(";"):
            continue
        body = line[1:].strip()
        body_lower = body.lower()
        for key in wanted:
            klen = len(key)
            if len(body) <= klen:
                continue
            if body_lower[:klen] != key.lower():
                continue
            if body[klen] not in _META_BOUNDARY:
                continue
            if "=" in body:
                meta[key] = _strip_quotes(body.split("=", 1)[1].strip())
            else:
                meta[key] = body
            break
    startup: list[tuple[int, str]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if i > 300:
                break
            if re.match(r"\s*(T\d+|M10[49]|M1[49]0)\b", line):
                startup.append((i, line.strip()))
    tool_match = None
    for _, cmd in startup:
        m = re.match(r"T(\d+)\b", cmd)
        if m:
            tool_match = int(m.group(1))
            break
    intended_tool = "extruder" if tool_match in (None, 0) else f"extruder{tool_match}"
    return {"path": str(path), "size": size, "metadata": meta, "startup_commands": startup[:40], "intended_tool": intended_tool}


def query_state(host: str, port: int) -> dict[str, Any]:
    # Include print_task_config/filament_detect so readiness/upload artifacts use
    # printer-reported material/color as primary truth, not just local labels.
    query = "print_stats&virtual_sdcard&webhooks&pause_resume&heater_bed&toolhead&extruder&extruder1&extruder2&extruder3&print_task_config&filament_detect"
    return http_json(f"{base_url(host, port)}/printer/objects/query?{query}")["result"]["status"]


def printer_filament_table(status: dict[str, Any]) -> list[dict[str, Any]]:
    ptc = status.get("print_task_config", {})
    fd_info = status.get("filament_detect", {}).get("info", [])

    def list_get(values: Any, idx: int, default: Any = None) -> Any:
        return values[idx] if isinstance(values, list) and idx < len(values) else default

    rows = []
    for ch, obj in enumerate(["extruder", "extruder1", "extruder2", "extruder3"]):
        rfid = fd_info[ch] if isinstance(fd_info, list) and ch < len(fd_info) and isinstance(fd_info[ch], dict) else {}
        rows.append({
            "printhead": ch + 1,
            "tool": f"T{ch}",
            "object": obj,
            "vendor": list_get(ptc.get("filament_vendor"), ch, "unknown"),
            "material": list_get(ptc.get("filament_type"), ch, "unknown"),
            "subtype": list_get(ptc.get("filament_sub_type"), ch, ""),
            "color_rgba": list_get(ptc.get("filament_color_rgba"), ch, "unknown"),
            "exists": list_get(ptc.get("filament_exist"), ch),
            "official": list_get(ptc.get("filament_official"), ch),
            "rfid_material": rfid.get("MAIN_TYPE"),
            "rfid_subtype": rfid.get("SUB_TYPE"),
            "rfid_card_uid": rfid.get("CARD_UID"),
        })
    return rows


def ensure_idle_ready(status: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    ps = status.get("print_stats", {})
    vsd = status.get("virtual_sdcard", {})
    wh = status.get("webhooks", {})
    pause = status.get("pause_resume", {})
    if wh.get("state") and wh.get("state") != "ready":
        blockers.append(f"webhooks state is {wh.get('state')}: {wh.get('state_message', '')}".strip())
    if pause.get("is_paused"):
        blockers.append("printer is paused")
    if vsd.get("is_active"):
        blockers.append(f"virtual_sdcard is active on {ps.get('filename')}")
    if ps.get("state") not in {None, "standby", "complete", "ready"}:
        blockers.append(f"print_stats state is {ps.get('state')}")
    for heater in ["heater_bed", "extruder", "extruder1", "extruder2", "extruder3"]:
        obj = status.get(heater, {})
        if float(obj.get("target") or 0) > 0:
            blockers.append(f"{heater} target is non-zero: {obj.get('target')}")
    return blockers


def run_tool_gate(host: str, port: int, material: str, intended_tool: str) -> tuple[bool, str]:
    cmd = [sys.executable, _default_toolmap_path(),
           "--host", host, "--port", str(port),
           "--requested-material", material, "--intended-tool", intended_tool]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=45)
    return proc.returncode == 0, proc.stdout


def inject_thumbnail(stl_path: Path, gcode_path: Path,
                     sizes: str = "48x48,300x300", timeout: float = 60.0) -> tuple[bool, str]:
    """Run tools/gcode_inject_thumbnail.py against (stl, gcode) in-place.

    Returns (ok, detail). Fails closed: any non-zero exit / missing injector /
    missing deps surfaces as ok=False so the caller can block the upload —
    we never silently upload a thumbnail-less file when the operator asked
    for one.
    """
    injector = Path(__file__).resolve().parent.parent / "tools" / "gcode_inject_thumbnail.py"
    if not injector.exists():
        return False, f"thumbnail injector not found at {injector}"
    if not stl_path.exists():
        return False, f"STL not found: {stl_path}"
    cmd = [sys.executable, str(injector),
           "--stl", str(stl_path), "--gcode", str(gcode_path),
           "--sizes", sizes, "--in-place"]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"thumbnail injector timed out after {timeout}s"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "no output").strip()
        return False, f"thumbnail injector exit {proc.returncode}: {detail}"
    return True, (proc.stdout or "").strip()


def multipart_upload(url: str, fields: dict[str, str], file_field: str, file_path: Path, timeout: float = 120.0) -> dict[str, Any]:
    boundary = f"----HermesU1Boundary{int(time.time()*1000)}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode())
        chunks.append(b"\r\n")
    ctype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'.encode())
    chunks.append(f"Content-Type: {ctype}\r\n\r\n".encode())
    chunks.append(file_path.read_bytes())
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("gcode", type=Path)
    # Defaults are None — lazy host/port resolution happens after parse_args
    # so a missing config only fails when actually run, not at import time.
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--material", default="PETG")
    ap.add_argument("--expected-printer", default="Snapmaker U1")
    ap.add_argument("--path", default="")
    ap.add_argument("--stl", type=Path, default=None,
                    help="If set, inject Snapmaker-app preview thumbnails from this "
                         "STL into the G-code (in-place) before upload.")
    ap.add_argument("--thumbnail-sizes", default="48x48,300x300",
                    help="Comma-separated WxH list passed through to the injector "
                         "(default matches the U1 machine profile).")
    args = ap.parse_args()

    # Resolve host/port lazily (env or config file) — no import-time call.
    host = args.host or get_u1_host()
    port = args.port if args.port is not None else get_u1_port()

    gcode = args.gcode
    if not gcode.exists() or not gcode.is_file():
        raise SystemExit(f"G-code file not found: {gcode}")
    if gcode.suffix.lower() not in {".gcode", ".gco", ".gc"}:
        raise SystemExit(f"Refusing non-G-code file: {gcode}")

    thumbnail_result: dict[str, Any] | None = None
    if args.stl is not None:
        ok, detail = inject_thumbnail(args.stl, gcode, sizes=args.thumbnail_sizes)
        thumbnail_result = {"ok": ok, "stl": str(args.stl), "sizes": args.thumbnail_sizes, "detail": detail}
        if not ok:
            # Fail-closed: operator asked for thumbnails, don't silently upload without.
            raise SystemExit(f"Thumbnail injection failed (refusing upload): {detail}")

    parsed = parse_gcode_metadata(gcode)
    meta = parsed["metadata"]
    blockers: list[str] = []
    printer_id = meta.get("printer_settings_id", "")
    filament_type = meta.get("filament_type", "")
    if args.expected_printer.lower() not in printer_id.lower():
        blockers.append(f"printer_settings_id mismatch: {printer_id!r}")
    if args.material.upper() not in filament_type.upper().split(";"):
        blockers.append(f"filament_type does not include {args.material}: {filament_type!r}")
    bed = meta.get("first_layer_bed_temperature") or meta.get("bed_temperature")
    if bed and not re.search(r"\b80\b", bed):
        blockers.append(f"unexpected PETG bed temp: {bed}")

    state_before = query_state(host, port)
    blockers.extend(ensure_idle_ready(state_before))

    tool_ok, tool_out = run_tool_gate(host, port, args.material, parsed["intended_tool"])
    if not tool_ok:
        blockers.append(f"tool/material gate failed for {parsed['intended_tool']} / {args.material}")

    if blockers:
        print("UPLOAD BLOCKED")
        for b in blockers:
            print(f"- {b}")
        print("\n--- tool gate ---")
        print(tool_out)
        return 2

    upload_url = f"{base_url(host, port)}/server/files/upload"
    response = multipart_upload(upload_url, {"root": "gcodes", "path": args.path, "print": "false"}, "file", gcode)

    state_after = query_state(host, port)
    after_blockers = []
    if state_after.get("virtual_sdcard", {}).get("is_active"):
        after_blockers.append("printer became active after upload")
    if state_after.get("print_stats", {}).get("state") not in {None, "standby", "complete", "ready"}:
        after_blockers.append(f"post-upload print state is {state_after.get('print_stats', {}).get('state')}")
    if response.get("result", {}).get("print_started") is True or response.get("result", {}).get("print_queued") is True:
        after_blockers.append(f"Moonraker indicated print start/queue: {response}")

    remote_name = gcode.name if not args.path else f"{args.path.rstrip('/')}/{gcode.name}"
    metadata = http_json(f"{base_url(host,port)}/server/files/metadata?filename={urllib.parse.quote(remote_name)}").get("result", {})

    result = {
        "ok": not after_blockers,
        "uploaded": remote_name,
        "upload_response": response.get("result", response),
        "parsed": parsed,
        "thumbnail_injection": thumbnail_result,
        "printer_before_filaments": printer_filament_table(state_before),
        "printer_after": {
            "state": state_after.get("print_stats", {}).get("state"),
            "active": state_after.get("virtual_sdcard", {}).get("is_active"),
            "filename": state_after.get("print_stats", {}).get("filename"),
        },
        "printer_after_filaments": printer_filament_table(state_after),
        "remote_metadata": metadata,
        "post_upload_blockers": after_blockers,
    }
    out = get_data_dir() / "latest_upload_result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")

    if after_blockers:
        print("UPLOAD WARNING")
        for b in after_blockers:
            print(f"- {b}")
        print(json.dumps(result, indent=2)[:4000])
        return 3

    print("U1 upload-only staging complete:")
    print(f"- File: {remote_name}")
    print(f"- print_started: {result['upload_response'].get('print_started')}")
    print(f"- print_queued: {result['upload_response'].get('print_queued')}")
    print(f"- Printer after: {result['printer_after']['state']} active={result['printer_after']['active']} last={result['printer_after']['filename']}")
    print(f"- Remote metadata: slicer={metadata.get('slicer')} {metadata.get('slicer_version')} estimate={metadata.get('estimated_time')}s filament={metadata.get('filament_weight_total')}g height={metadata.get('object_height')}mm")
    print("- Printer-reported filaments:")
    for row in result["printer_after_filaments"]:
        print(f"    printhead #{row['printhead']} / {row['tool']} / {row['object']}: {row['vendor']} {row['material']} {row['subtype']} color={row['color_rgba']} loaded={row['exists']} official={row['official']}")
    print(f"- Artifact: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
