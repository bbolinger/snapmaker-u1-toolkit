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
def _suggested_rename(filename: str) -> str:
    """Build the timestamped rename target for a filename collision.

    Inserts <stem>_<UTC YYYYMMDD-HHMMSS><suffix>. Audit 2026-06-26: operator
    preferred default when a target already exists on U1 storage."""
    from datetime import datetime, timezone
    p = Path(filename)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{p.stem}_{stamp}{p.suffix}"


def _default_toolmap_path() -> str:
    return str(Path(__file__).resolve().parent / "u1_toolmap.py")


def http_json(url: str, timeout: float = 12.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_remote_metadata(host: str, port: int, filename: str) -> dict[str, Any]:
    """Post-upload metadata, scanned to completion when the printer allows.

    Moonraker parses a large file's metadata (thumbnails included)
    asynchronously after upload; the touchscreen and app can ask for the
    preview in that window, get nothing, and cache the miss for that
    filename (live 2026-07-22 on 50 MB plates: generic icons that never
    healed). POST /server/files/metascan forces the scan and BLOCKS until
    it finishes, so by the time the workflow announces the upload the
    thumbnails are queryable. Older Moonraker builds without the endpoint
    fall back to the plain metadata GET, which is the previous behavior.
    """
    try:
        req = urllib.request.Request(
            f"{base_url(host, port)}/server/files/metascan",
            data=json.dumps({"filename": filename}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=90.0) as r:
            meta = json.loads(r.read().decode("utf-8")).get("result", {})
        if meta:
            return meta
    except Exception:
        pass  # endpoint absent or scan failed; the GET below still answers
    try:
        return http_json(
            f"{base_url(host, port)}/server/files/metadata?filename="
            f"{urllib.parse.quote(filename)}").get("result", {})
    except Exception:
        return {}


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


def query_userdata_space(host: str, port: int, timeout: float = 5.0) -> dict[str, Any] | None:
    """Query the U1's user-data storage space via Snapmaker's custom Moonraker
    endpoint `/server/files/get_userdata_space` (POST). Returns the result dict
    with `free_space`, `total_space`, `units` (MiB) — or None on any error or
    if the endpoint isn't implemented on the firmware.

    Verified live 2026-06-24 against U1 firmware 1.4.1: returns
    `{state: 'success', free_space: N, total_space: N, units: 'MiB'}` and
    reflects real-time post-upload state (a 21 MiB upload immediately reduced
    free_space by ~21 MiB).

    Fail-soft: older firmware that doesn't implement this endpoint should not
    block uploads. Callers must handle the None case as "skip the check."
    """
    url = f"{base_url(host, port)}/server/files/get_userdata_space"
    try:
        req = urllib.request.Request(url, data=b"", method="POST",
                                     headers={"Content-Length": "0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        result = data.get("result", {})
        if result.get("state") != "success":
            return None
        return result
    except Exception:
        return None

def query_moonraker_metadata(host: str, port: int, filename: str,
                              settle_seconds: float = 0.0,
                              timeout: float = 5.0) -> dict[str, Any] | None:
    """Query Moonraker's rich G-code metadata for an already-uploaded file.

    Standard Moonraker endpoint `/server/files/metadata?filename=...` returns
    ~34 numeric/structured fields the hand parser doesn't (estimated_time as
    int seconds, filament_used_mm per extruder, filament_colour, slicer +
    slicer_version, object_height, line_width, uuid, thumbnail descriptors,
    gcode_start_byte/end_byte, etc.). Verified live 2026-06-24 against U1
    firmware 1.4.1 (~24ms for an already-scanned 21 MB gcode).

    `settle_seconds` defaults to 0 — Moonraker's metadata scan is fast in
    practice and complete by the time the multipart upload subprocess returns
    (verified live 2026-06-24). Set a small positive value only if you observe
    thin results on first-upload metadata queries.

    Returns the result dict or None on any error / empty result. Fail-soft.
    Hand-parser fields (`print_settings_id`, `printer_settings_id`,
    `curr_bed_type`) are NOT here — keep using `parse_gcode_metadata()`
    for the v1.4.2 fidelity-check safety rule.
    """
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    url = f"{base_url(host, port)}/server/files/metadata?filename={urllib.parse.quote(filename)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        result = data.get("result", {})
        # Empty / metadata not yet scanned — treat as None so caller doesn't
        # surface a misleading partial dict.
        if not result or "size" not in result:
            return None
        return result
    except Exception:
        return None

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
    # Audit 2026-06-26 (filename collision finding). Pre-upload metadata
    # query catches the case where the same printer-storage name already
    # exists; without this the operator gets a silent overwrite that may
    # clobber an in-progress queue.
    ap.add_argument("--on-collision", choices=["cancel", "overwrite", "rename"],
                    default=None,
                    help="What to do if the target filename already exists on the U1. "
                         "Not set + collision detected -> exit rc=5 with a collision packet "
                         "the caller can present to the operator. 'cancel' = exit. "
                         "'overwrite' = upload over the existing file. "
                         "'rename' = upload as <stem>_<timestamp><suffix> (preferred default).")
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
            # Fail-soft 2026-06-25 (gate audit). Thumbnail is a cosmetic preview
            # for the U1 touchscreen — not safety-critical. Warn + proceed so a
            # bad STL or PIL hiccup doesn't block a legitimate upload.
            print(f"WARNING: thumbnail injection failed ({detail}); uploading without preview", file=sys.stderr)

    parsed = parse_gcode_metadata(gcode)
    meta = parsed["metadata"]
    blockers: list[str] = []
    printer_id = meta.get("printer_settings_id", "")
    filament_type = meta.get("filament_type", "")
    if args.expected_printer.lower() not in printer_id.lower():
        blockers.append(f"printer_settings_id mismatch: {printer_id!r}")
    if args.material.upper() not in filament_type.upper().split(";"):
        blockers.append(f"filament_type does not include {args.material}: {filament_type!r}")
    # Bed temp check removed 2026-06-25. The upload gate was duplicating the
    # slicer's job — checking gcode metadata bed temps to catch the case where
    # Orca silently fell back to PLA defaults. With v1.5.1's filament inheritance
    # flattening (_flatten_filament_profile + _materialize_flat_filament in
    # u1_slice_workflow.py), Orca can't silently fall back anymore; filament_type
    # reflects the actual loaded profile, and bed temps come from that profile's
    # validated values. The filament_type blocker above is the meaningful gate.

    # Idle/print-state check removed 2026-06-25. Moonraker accepts file
    # uploads to storage regardless of print state — a busy printer can
    # still receive a file into its filesystem; you just can't START a
    # new print until it's idle. The idle/paused/heater-target checks
    # belong on the START gate (u1_print_start_gate.py already has them),
    # not the upload path. Blocking uploads on busy state forced the
    # operator to wait for a print to finish before queueing the next
    # file, which is the wrong UX.
    state_before = query_state(host, port)

    # Preflight: confirm the U1 has enough user-data space for this gcode. The
    # endpoint is Snapmaker's custom /server/files/get_userdata_space (POST,
    # ~0.01s, returns MiB). Fail-soft — older firmware without this endpoint
    # returns None and we skip the check rather than block uploads.
    SPACE_HEADROOM_MIB = 50  # keep some breathing room; don't fill to 0
    space = query_userdata_space(host, port)
    if space is not None:
        try:
            free_mib = int(space.get("free_space", 0))
            gcode_mib = int(gcode.stat().st_size / (1024 * 1024)) + 1  # round up
            needed_mib = gcode_mib + SPACE_HEADROOM_MIB
            if free_mib < needed_mib:
                blockers.append(
                    f"insufficient U1 storage: need ~{needed_mib} MiB "
                    f"(gcode {gcode_mib} MiB + {SPACE_HEADROOM_MIB} MiB headroom), "
                    f"have {free_mib} MiB free of {space.get('total_space')} MiB"
                )
        except (TypeError, ValueError):
            pass  # malformed response — treat like missing endpoint

    # Tool/material gate moved to u1_print_start_gate.py 2026-06-25 (gate
    # audit). Checking whether the printer currently has the right filament
    # loaded on the intended tool is a START-time concern, not an UPLOAD-time
    # concern. The operator should be able to queue an upload now and load
    # material later. The start gate calls u1_toolmap with the same args.

    if blockers:
        # Cold review F1 (2026-06-26): the dead `print(tool_out)` lines used
        # to print the tool/material gate output here, but the gate moved to
        # u1_print_start_gate.py (commit ffa425d) and `tool_out` is no longer
        # defined. Calling this path raised NameError on every pre-upload
        # blocker (printer_id mismatch, filament_type mismatch, storage). The
        # crash exited rc=1, which the workflow's _real_upload mistreats as
        # rc=3 ("upload succeeded with warnings") — totally wrong. Removed.
        print("UPLOAD BLOCKED")
        for b in blockers:
            print(f"- {b}")
        return 2

    # Filename collision detection (audit 2026-06-26). Query the target's
    # metadata; if Moonraker returns a non-empty result, the file exists
    # already. Resolve per --on-collision policy.
    target_storage_name = gcode.name if not args.path else f"{args.path.rstrip('/')}/{gcode.name}"
    try:
        existing_meta = http_json(
            f"{base_url(host,port)}/server/files/metadata?filename={urllib.parse.quote(target_storage_name)}"
        ).get("result", {})
    except Exception:
        existing_meta = {}
    filename_already_existed = bool(existing_meta) and existing_meta.get("filename")
    uploaded_filename = target_storage_name
    collision_policy: str | None = None
    if filename_already_existed:
        if args.on_collision is None:
            # Caller didn't pre-decide. Emit collision packet + exit rc=5.
            # The workflow surfaces this as a need_input prompt to the operator.
            packet = {
                "filename_already_existed": True,
                "target_filename": target_storage_name,
                "existing_size": existing_meta.get("size"),
                "existing_modified": existing_meta.get("modified"),
                "options": [
                    {"value": "rename", "label": "Upload with timestamped name (recommended)",
                     "preview": _suggested_rename(target_storage_name)},
                    {"value": "overwrite", "label": "Overwrite existing file"},
                    {"value": "cancel", "label": "Cancel"},
                ],
                "human_summary": (
                    f"A file named {target_storage_name} already exists on the U1. "
                    "Re-run with --on-collision=rename (recommended), overwrite, or cancel."
                ),
            }
            print("UPLOAD COLLISION")
            print(json.dumps(packet, indent=2))
            return 5
        if args.on_collision == "cancel":
            # Cold review F9: return rc=6 (not rc=5) so the workflow can
            # distinguish "user cancelled" from "needs operator answer."
            # Without this, the workflow's rc=5 handler kept re-emitting the
            # collision prompt forever.
            print("UPLOAD CANCELLED (filename collision, --on-collision=cancel)")
            return 6
        if args.on_collision == "rename":
            uploaded_filename = _suggested_rename(target_storage_name)
            collision_policy = "rename"
        elif args.on_collision == "overwrite":
            uploaded_filename = target_storage_name
            collision_policy = "overwrite"

    upload_url = f"{base_url(host, port)}/server/files/upload"
    # Moonraker's upload form field 'filename' is what determines the storage
    # name. When renaming, we need to send the rename-target, not the local
    # gcode's basename. The multipart_upload helper uses the file's local
    # name by default — when collision_policy=='rename', stage a renamed
    # copy in a temp dir so the Moonraker storage name comes out right.
    import tempfile, shutil
    upload_source = gcode
    _tmp_for_rename: tempfile.TemporaryDirectory | None = None
    if collision_policy == "rename":
        _tmp_for_rename = tempfile.TemporaryDirectory()
        upload_source = Path(_tmp_for_rename.name) / Path(uploaded_filename).name
        shutil.copyfile(gcode, upload_source)
    try:
        _t0 = time.monotonic()
        try:
            response = multipart_upload(upload_url, {"root": "gcodes", "path": args.path, "print": "false"}, "file", upload_source)
        except Exception as exc:
            # Transport failure IS the rc=4 contract - it must not escape as
            # a bare traceback and generic rc=1 (first Windows live upload,
            # 2026-07-12: a connect timeout left nothing diagnosable). Record
            # the same granular artifact the success path writes, with the
            # target URL (no credentials ride in it), exception class, and
            # elapsed time.
            diag = {
                "moonraker_upload_ok": False,
                "remote_metadata_ok": False,
                "ok": False,
                "transport_error": f"{type(exc).__name__}: {exc}"[:400],
                "upload_url": upload_url,
                "elapsed_s": round(time.monotonic() - _t0, 1),
                "uploaded_filename": uploaded_filename,
            }
            art = get_data_dir() / "latest_upload_result.json"
            art.parent.mkdir(parents=True, exist_ok=True)
            art.write_text(json.dumps(diag, indent=2), encoding="utf-8")
            print("UPLOAD FAILED (transport error before Moonraker answered)")
            print(json.dumps(diag, indent=2))
            return 4
    finally:
        if _tmp_for_rename is not None:
            _tmp_for_rename.cleanup()

    state_after = query_state(host, port)
    after_blockers: list[str] = []
    after_warnings: list[str] = []

    # Audit 2026-06-26: `cancelled` is a benign terminal state when the
    # printer is otherwise idle and ready. Treat as warning, not blocker.
    vsd = state_after.get("virtual_sdcard", {})
    pause = state_after.get("pause_resume", {})
    wh = state_after.get("webhooks", {})
    ps_state = state_after.get("print_stats", {}).get("state")
    ps_idle_terminal = (ps_state in {None, "standby", "complete", "ready"})
    ps_cancelled_but_clean = (
        ps_state == "cancelled"
        and not vsd.get("is_active")
        and not pause.get("is_paused")
        and (wh.get("state") in {None, "ready"})
    )

    if vsd.get("is_active"):
        after_blockers.append("printer became active after upload")
    if not ps_idle_terminal and not ps_cancelled_but_clean:
        after_blockers.append(f"post-upload print state is {ps_state}")
    elif ps_cancelled_but_clean:
        after_warnings.append("post-upload print_stats.state is 'cancelled' but printer is idle + webhooks ready — not blocking")
    if response.get("result", {}).get("print_started") is True or response.get("result", {}).get("print_queued") is True:
        after_blockers.append(f"Moonraker indicated print start/queue: {response}")

    remote_name = uploaded_filename
    metadata = fetch_remote_metadata(host, port, remote_name)
    remote_metadata_ok = bool(metadata)
    # Moonraker's /server/files/upload response can be EITHER wrapped (with
    # a top-level "result" key, Moonraker JSON-RPC convention) OR unwrapped
    # (the inner dict directly, which is what we actually observe live —
    # caught 2026-06-26 against the production U1). And the action is
    # 'create_file' for new files, 'modify_file' for overwrite. Accept all
    # four combinations rather than just the wrapped/create-file case.
    _payload = response.get("result", response) if isinstance(response, dict) else {}
    moonraker_upload_ok = bool(
        _payload.get("item")
        or _payload.get("action") in ("create_file", "modify_file")
    )

    result = {
        # Granular truth (audit 2026-06-26).
        "moonraker_upload_ok": moonraker_upload_ok,
        "remote_metadata_ok": remote_metadata_ok,
        "post_upload_validation_ok": not after_blockers,
        "ok": moonraker_upload_ok and remote_metadata_ok and not after_blockers,
        # File identity.
        "uploaded": remote_name,
        "uploaded_filename": uploaded_filename,
        "target_filename": target_storage_name,
        "filename_already_existed": bool(filename_already_existed),
        "collision_policy": collision_policy,
        # Existing fields, kept for backward compat with downstream consumers.
        "upload_response": response.get("result", response),
        "parsed": parsed,
        "thumbnail_injection": thumbnail_result,
        "printer_before_filaments": printer_filament_table(state_before),
        "printer_after": {
            "state": ps_state,
            "active": vsd.get("is_active"),
            "filename": state_after.get("print_stats", {}).get("filename"),
        },
        "printer_after_filaments": printer_filament_table(state_after),
        "remote_metadata": metadata,
        "post_upload_blockers": after_blockers,
        "post_upload_warnings": after_warnings,
    }
    out = get_data_dir() / "latest_upload_result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")

    # Audit 2026-06-26 return-code contract:
    #   0 — upload succeeded + post-upload validation clean (warnings allowed)
    #   3 — upload succeeded BUT post-upload blockers (file is on printer)
    #   4 — Moonraker upload itself failed; no printer-side file confirmed
    if not moonraker_upload_ok or not remote_metadata_ok:
        print("UPLOAD FAILED (Moonraker upload did not produce a remote file)")
        print(json.dumps(result, indent=2)[:4000])
        return 4

    if after_blockers:
        print("UPLOAD WARNING (file IS on printer; blockers below are post-upload state, not transport)")
        for b in after_blockers:
            print(f"- {b}")
        for w in after_warnings:
            print(f"warning: {w}")
        print(json.dumps(result, indent=2)[:4000])
        return 3

    print("U1 upload-only staging complete:")
    for w in after_warnings:
        print(f"- warning: {w}")
    print(f"- File: {remote_name}")
    if collision_policy:
        print(f"- Collision: {collision_policy} (original target was {target_storage_name})")
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
