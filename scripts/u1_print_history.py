#!/usr/bin/env python3
"""Maintain a quiet Snapmaker U1 print history ledger.

Cron/no_agent contract: print nothing during normal operation. This script only
writes local history artifacts; it never moves/heats/starts/stops the printer.

Artifacts (under the resolved data dir — see u1_config.get_data_dir):
- <data-dir>/print_history.json        canonical upserted records (atomic rewrite)
- <data-dir>/print_history.jsonl       append-only lifecycle/events (POSIX O_APPEND)
- <data-dir>/print_history_state.json  watcher cursor state
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import os
import tempfile
from u1_config import get_u1_host, get_u1_port, get_data_dir

# Module attributes are None until first use; tests can monkeypatch.setattr
# them directly to point at tmp paths. _resolve_* helpers below honor either
# the patched value (when set) or fall back to live get_data_dir() lookup.
HISTORY_JSON: Path | None = None
HISTORY_JSONL: Path | None = None
STATE_PATH: Path | None = None
LATEST_UPLOAD: Path | None = None
LAYER_STATE: Path | None = None


def _base_url() -> str:
    return f"http://{get_u1_host()}:{get_u1_port()}"


def _history_json() -> Path:
    return HISTORY_JSON if HISTORY_JSON is not None else get_data_dir() / "print_history.json"


def _history_jsonl() -> Path:
    return HISTORY_JSONL if HISTORY_JSONL is not None else get_data_dir() / "print_history.jsonl"


def _state_path() -> Path:
    return STATE_PATH if STATE_PATH is not None else get_data_dir() / "print_history_state.json"


def _latest_upload() -> Path:
    return LATEST_UPLOAD if LATEST_UPLOAD is not None else get_data_dir() / "latest_upload_result.json"


def _layer_state() -> Path:
    return LAYER_STATE if LAYER_STATE is not None else get_data_dir() / "last_layer" / "last_layer_watch_state.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def estimated_started_at(ps: dict[str, Any]) -> str:
    """Best-effort actual start timestamp from Klipper duration counters."""
    seconds = ps.get("total_duration") or ps.get("print_duration")
    if isinstance(seconds, (int, float)) and seconds > 0:
        return (datetime.now(timezone.utc) - timedelta(seconds=float(seconds))).isoformat()
    return now_iso()


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    """Atomic write: tmpfile in same dir + os.replace, so concurrent cron
    runs can't ever produce a half-written or truncated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    # NamedTemporaryFile in the same dir guarantees os.replace is atomic
    # (cross-fs renames aren't atomic; same-dir always is on POSIX + NTFS).
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup; don't mask the original error.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_event(event: dict[str, Any]) -> None:
    jsonl = _history_jsonl()
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    # POSIX O_APPEND is atomic for writes < PIPE_BUF (~4KB); a JSONL line is
    # well under that, so concurrent cron-driven appends don't interleave.
    with jsonl.open("a") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")


def http_json(path: str, timeout: float = 8.0) -> dict[str, Any]:
    with urllib.request.urlopen(f"{_base_url()}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def query_status() -> dict[str, Any]:
    q = "print_stats&display_status&virtual_sdcard&pause_resume&heater_bed&toolhead&extruder&extruder1&extruder2&extruder3&webhooks"
    return http_json(f"/printer/objects/query?{q}")["result"]["status"]


def query_metadata(filename: str) -> dict[str, Any]:
    if not filename:
        return {}
    try:
        return http_json(f"/server/files/metadata?filename={urllib.parse.quote(filename)}").get("result", {})
    except Exception:
        return {}


def layer_info(ps: dict[str, Any]) -> tuple[Any, Any]:
    info = ps.get("info") or {}
    return info.get("current_layer") or ps.get("current_layer"), info.get("total_layer") or ps.get("total_layer")


def progress_value(st: dict[str, Any]) -> float | None:
    p = (st.get("display_status") or {}).get("progress")
    if p is None:
        p = (st.get("virtual_sdcard") or {}).get("progress")
    return float(p) if isinstance(p, (int, float)) else None


def active_tool_record(st: dict[str, Any], upload: dict[str, Any]) -> dict[str, Any]:
    ext_name = (st.get("toolhead") or {}).get("extruder") or (upload.get("parsed") or {}).get("intended_tool") or "extruder"
    tool_lookup = {"extruder": "T0", "extruder1": "T1", "extruder2": "T2", "extruder3": "T3"}
    printhead_lookup = {"extruder": 1, "extruder1": 2, "extruder2": 3, "extruder3": 4}
    filament = None
    for f in upload.get("printer_before_filaments") or upload.get("printer_after_filaments") or []:
        if f.get("object") == ext_name:
            filament = f
            break
    return {
        "object": ext_name,
        "tool": tool_lookup.get(ext_name),
        "printhead": printhead_lookup.get(ext_name),
        "filament": filament,
    }


def find_record(records: list[dict[str, Any]], job_key: str) -> dict[str, Any] | None:
    for rec in records:
        if rec.get("job_key") == job_key:
            return rec
    return None


def enrich_from_layer_state(rec: dict[str, Any], job_key: str) -> None:
    layer_state = read_json(_layer_state(), {})
    if layer_state.get("first_layer_fired_job_key") == job_key:
        rec["first_layer_photo"] = layer_state.get("first_layer_image")
        rec["first_layer_photo_at"] = layer_state.get("first_layer_fired_at")
        rec["first_layer_photo_layer"] = layer_state.get("first_layer_fired_layer")
    # Backward compatibility with the old last-layer keys from before rename.
    last_key = layer_state.get("last_layer_fired_job_key") or layer_state.get("fired_job_key")
    if last_key == job_key:
        rec["last_layer_photo"] = layer_state.get("last_layer_image") or layer_state.get("image")
        rec["last_layer_photo_at"] = layer_state.get("last_layer_fired_at") or layer_state.get("fired_at")
        rec["last_layer_photo_layer"] = layer_state.get("last_layer_fired_layer") or layer_state.get("fired_layer")


def build_or_update_record(rec: dict[str, Any], st: dict[str, Any], metadata: dict[str, Any], upload: dict[str, Any], job_key: str, first_seen: str) -> dict[str, Any]:
    ps = st.get("print_stats", {})
    bed = st.get("heater_bed", {})
    current_layer, total_layer = layer_info(ps)
    progress = progress_value(st)
    parsed = upload.get("parsed") or {}
    parsed_meta = parsed.get("metadata") or {}
    tool = active_tool_record(st, upload)
    rec.setdefault("job_key", job_key)
    rec.setdefault("started_at", first_seen)
    rec.setdefault("events", [])
    rec.update({
        "filename": ps.get("filename") or rec.get("filename"),
        "state": ps.get("state"),
        "last_seen_at": now_iso(),
        "progress": progress,
        "current_layer": current_layer,
        "total_layer": total_layer,
        "bed_temp": bed.get("temperature"),
        "bed_target": bed.get("target"),
        "active_tool": tool,
        "slicer": metadata.get("slicer") or "OrcaSlicer" if metadata.get("slicer_version") else metadata.get("slicer"),
        "slicer_version": metadata.get("slicer_version"),
        "estimated_time_s": metadata.get("estimated_time"),
        "filament_g": metadata.get("filament_weight_total") or (metadata.get("filament_weight") or [None])[0] if isinstance(metadata.get("filament_weight"), list) else metadata.get("filament_weight"),
        "filament_type": metadata.get("filament_type") or parsed_meta.get("filament_type"),
        "filament_name": metadata.get("filament_name") or parsed_meta.get("filament_settings_id"),
        "object_height_mm": metadata.get("object_height"),
        "layer_height_mm": metadata.get("layer_height") or parsed_meta.get("layer_height"),
        "first_layer_bed_temp": metadata.get("first_layer_bed_temp") or parsed_meta.get("first_layer_bed_temperature"),
        "first_layer_nozzle_temp": metadata.get("first_layer_extr_temp") or parsed_meta.get("first_layer_temperature"),
        "print_settings_id": parsed_meta.get("print_settings_id"),
        "printer_settings_id": parsed_meta.get("printer_settings_id"),
        "source_gcode_path": parsed.get("path"),
    })
    enrich_from_layer_state(rec, job_key)
    return rec


def main() -> int:
    get_data_dir().mkdir(parents=True, exist_ok=True)
    state = read_json(_state_path(), {})
    records_data = read_json(_history_json(), {"records": []})
    records: list[dict[str, Any]] = records_data.setdefault("records", [])

    try:
        st = query_status()
    except Exception as exc:
        state.update({"last_checked_at": now_iso(), "last_error": str(exc), "last_error_at": now_iso()})
        write_json(_state_path(), state)
        return 0

    ps = st.get("print_stats", {})
    vsd = st.get("virtual_sdcard", {})
    filename = ps.get("filename") or ""
    current_layer, total_layer = layer_info(ps)
    job_key = f"{filename}|{total_layer or ''}"
    active = bool(vsd.get("is_active")) and ps.get("state") == "printing" and bool(filename)
    upload = read_json(_latest_upload(), {})
    metadata = query_metadata(filename) if filename else {}

    previous_active_key = state.get("active_job_key")

    if active:
        first_seen = state.get("active_started_at") if previous_active_key == job_key else estimated_started_at(ps)
        if previous_active_key != job_key:
            append_event({"event": "print_started", "at": first_seen, "job_key": job_key, "filename": filename, "total_layer": total_layer})
            state["active_started_at"] = first_seen
        rec = find_record(records, job_key) or {}
        if rec not in records:
            records.append(rec)
        build_or_update_record(rec, st, metadata, upload if upload.get("uploaded") == filename else {}, job_key, first_seen)
        state.update({"active_job_key": job_key, "last_checked_at": now_iso(), "last_filename": filename})
    else:
        if previous_active_key:
            rec = find_record(records, previous_active_key)
            event = {"event": "print_finished", "at": now_iso(), "job_key": previous_active_key, "final_state": ps.get("state"), "filename": filename}
            append_event(event)
            if rec:
                rec.update({"completed_at": event["at"], "result": ps.get("state") or "unknown", "state": ps.get("state"), "last_seen_at": event["at"]})
                enrich_from_layer_state(rec, previous_active_key)
        state.update({"active_job_key": None, "active_started_at": None, "last_checked_at": now_iso(), "last_filename": filename, "last_state": ps.get("state")})

    records_data["updated_at"] = now_iso()
    write_json(_history_json(), records_data)
    write_json(_state_path(), state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
