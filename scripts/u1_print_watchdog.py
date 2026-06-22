#!/usr/bin/env python3
"""Quiet Snapmaker U1 print watchdog.

Cron/no_agent contract: print nothing unless there is an operator-worthy issue.
No movement/heating/G-code/start/cancel commands.

Coverage:
- Any active U1 print, not just Hermes-started jobs.
- Quiet 20-minute health polling.
- Alerts once per distinct issue/job, with cooldown/state to avoid spam.
- Last-layer photos are handled by u1_last_layer_watch.py; keep that as the
  high-frequency near-completion watcher.
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import sys
from u1_config import get_u1_host, get_u1_port, get_data_dir


def _base_url() -> str:
    return f"http://{get_u1_host()}:{get_u1_port()}"


def _out_dir() -> Path:
    return get_data_dir() / "watchdog"


def _state_path() -> Path:
    return _out_dir() / "u1_print_watchdog_state.json"


def _camera_helper() -> str:
    return str(Path(__file__).resolve().parent / "u1_camera.py")

# Conservative thresholds to avoid false positives during warmup / normal slow phases.
NO_PROGRESS_SECONDS = 45 * 60
TEMP_LAG_SECONDS = 35 * 60
TEMP_LAG_C = 15.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def http_json(path: str, timeout: float = 8.0) -> dict[str, Any]:
    with urllib.request.urlopen(f"{_base_url()}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def query_status() -> dict[str, Any]:
    objects = [
        "print_stats",
        "display_status",
        "virtual_sdcard",
        "pause_resume",
        "heater_bed",
        "toolhead",
        "extruder",
        "extruder1",
        "extruder2",
        "extruder3",
        "webhooks",
        "filament_motion_sensor e0_filament",
        "filament_motion_sensor e1_filament",
        "filament_motion_sensor e2_filament",
        "filament_motion_sensor e3_filament",
        "filament_feed left",
        "filament_feed right",
    ]
    q = "&".join(urllib.parse.quote(o, safe="") for o in objects)
    return http_json(f"/printer/objects/query?{q}")["result"]["status"]


def load_state() -> dict[str, Any]:
    try:
        return json.loads(_state_path().read_text())
    except Exception:
        return {}


def save_state(state: dict[str, Any]) -> None:
    _out_dir().mkdir(parents=True, exist_ok=True)
    _state_path().write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def rounded(v: Any, ndigits: int = 1) -> str:
    try:
        return str(round(float(v), ndigits))
    except Exception:
        return "unknown"


def get_progress(st: dict[str, Any]) -> float | None:
    display = st.get("display_status", {})
    vsd = st.get("virtual_sdcard", {})
    p = display.get("progress")
    if p is None:
        p = vsd.get("progress")
    return float(p) if isinstance(p, (int, float)) else None


def layer_info(ps: dict[str, Any]) -> tuple[Any, Any]:
    info = ps.get("info") or {}
    return info.get("current_layer") or ps.get("current_layer"), info.get("total_layer") or ps.get("total_layer")


def active_extruder(st: dict[str, Any]) -> str:
    return (st.get("toolhead") or {}).get("extruder") or "extruder"


def active_feed_status(st: dict[str, Any]) -> dict[str, Any]:
    ext = active_extruder(st)
    idx = {"extruder": 0, "extruder1": 1, "extruder2": 2, "extruder3": 3}.get(ext)
    if idx is None:
        return {}
    motion = st.get(f"filament_motion_sensor e{idx}_filament", {}) or {}
    feed_group = "filament_feed left" if idx in (0, 1) else "filament_feed right"
    feed_key = f"extruder{idx}"
    feed = (st.get(feed_group, {}) or {}).get(feed_key, {}) or {}
    return {
        "extruder": ext,
        "index": idx,
        "filament_detected": motion.get("filament_detected") if motion else feed.get("filament_detected"),
        "motion_enabled": motion.get("enabled"),
        "channel_state": feed.get("channel_state"),
        "channel_action_state": feed.get("channel_action_state"),
        "channel_error": feed.get("channel_error"),
        "channel_error_state": feed.get("channel_error_state"),
        "module_exist": feed.get("module_exist"),
    }


def feed_ready_after_runout(st: dict[str, Any]) -> bool:
    feed = active_feed_status(st)
    return (
        feed.get("filament_detected") is True
        and feed.get("channel_error") == "ok"
        and feed.get("channel_state") == "load_finish"
        and feed.get("channel_action_state") == "load_finish"
    )


def capture_watchdog_photo(filename: str, reason: str) -> Path | None:
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in filename)[:80] or "u1_print"
    safe_reason = "".join(c if c.isalnum() or c in "._-" else "_" for c in reason)[:40] or "watchdog"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = _out_dir() / f"{stamp}_{safe_reason}_{safe_name}.jpg"
    cmd = [sys.executable, _camera_helper(), "watch", "--output", str(out), "--timeout", "25", "--wait", "2", "--poll", "2"]
    try:
        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35)
        if proc.returncode != 0:
            return None
        payload = json.loads(proc.stdout)
        photo = payload.get("result") or {}
        if payload.get("ok") and photo.get("jpeg_magic") and photo.get("changed"):
            return out
    except Exception:
        return None
    return None


def issue_message(issue: str, st: dict[str, Any], detail: str, image: Path | None = None) -> str:
    ps = st.get("print_stats", {})
    bed = st.get("heater_bed", {})
    ext_name = active_extruder(st)
    ext = st.get(ext_name, {}) or {}
    progress = get_progress(st)
    layer, total = layer_info(ps)
    pct = rounded(progress * 100, 1) + "%" if isinstance(progress, float) else "unknown"
    message = (
        f"U1 print watchdog issue: {issue}\n"
        f"- File: {ps.get('filename') or 'unknown'}\n"
        f"- State: {ps.get('state') or 'unknown'}\n"
        f"- Progress: {pct}\n"
        f"- Layer: {layer or 'unknown'} / {total or 'unknown'}\n"
        f"- Temps: bed {rounded(bed.get('temperature'))}/{rounded(bed.get('target'))}°C, "
        f"{ext_name} {rounded(ext.get('temperature'))}/{rounded(ext.get('target'))}°C\n"
        f"- Detail: {detail}"
    )
    if image:
        message += f"\nMEDIA:{image}"
    return message


def alert_once(state: dict[str, Any], key: str, message: str) -> bool:
    if state.get("last_alert_key") == key:
        return False
    state["last_alert_key"] = key
    state["last_alert_at"] = now_iso()
    state["last_alert_message"] = message
    print(message)
    return True


def main() -> int:
    _out_dir().mkdir(parents=True, exist_ok=True)
    state = load_state()
    ts = time.time()

    try:
        st = query_status()
    except Exception as exc:
        failures = int(state.get("consecutive_query_failures") or 0) + 1
        state.update({
            "last_checked_at": now_iso(),
            "consecutive_query_failures": failures,
            "last_query_error": str(exc),
            "last_query_error_at": now_iso(),
        })
        # Alert after repeated misses, once per outage series.
        if failures >= 3:
            alert_once(state, f"offline|{failures // 3}", f"U1 print watchdog issue: printer/status query failed {failures} times in a row\n- Detail: {exc}")
        save_state(state)
        return 0

    state["consecutive_query_failures"] = 0
    ps = st.get("print_stats", {})
    vsd = st.get("virtual_sdcard", {})
    pause = st.get("pause_resume", {})
    wh = st.get("webhooks", {})
    filename = ps.get("filename") or ""
    print_state = ps.get("state")
    progress = get_progress(st)
    layer, total = layer_info(ps)
    job_key = f"{filename}|{total or ''}"

    state.update({
        "last_checked_at": now_iso(),
        "filename": filename,
        "print_state": print_state,
        "progress": progress,
        "current_layer": layer,
        "total_layer": total,
    })

    has_job = bool(filename) and print_state not in {None, "standby", "complete"}

    # Immediate semantic issues. Check these before the normal active gate because
    # Snapmaker/Klipper reports paused/runout with virtual_sdcard.is_active=false.
    if pause.get("is_paused") or print_state == "paused":
        exc = ps.get("exception") or {}
        detail = exc.get("message") or "pause_resume reports paused"
        if "runout" in str(detail).lower() and feed_ready_after_runout(st):
            feed = active_feed_status(st)
            ready_detail = (
                f"{detail}; active feed reports load_finish/detected/ok "
                f"({feed.get('extruder')} e{feed.get('index')}, "
                f"channel={feed.get('channel_state')}, action={feed.get('channel_action_state')}, "
                f"detected={feed.get('filament_detected')}, error={feed.get('channel_error')}). "
                "Operator should verify purge/nozzle physically, then resume locally."
            )
            state.update({
                "pending_resume_check_job_key": job_key,
                "pending_resume_check_layer": layer,
                "pending_resume_check_total_layer": total,
                "pending_resume_check_filename": filename,
                "pending_resume_check_requested_at": now_iso(),
                "pending_resume_check_fired": False,
            })
            image = capture_watchdog_photo(filename, "runout_loaded_ready")
            alert_once(state, f"runout_loaded_ready|{job_key}|{detail}", issue_message("filament loaded; approve resume", st, ready_detail, image))
        else:
            alert_once(state, f"paused|{job_key}|{detail}", issue_message("paused", st, detail))
        save_state(state)
        return 0

    if print_state in {"error", "cancelled"} or wh.get("state") == "error":
        exc = ps.get("exception") or {}
        detail = exc.get("message") or wh.get("state_message") or f"print_stats={print_state}, webhooks={wh.get('state')}"
        alert_once(state, f"state|{job_key}|{print_state}|{wh.get('state')}", issue_message("printer error/cancelled", st, detail))
        save_state(state)
        return 0

    active = bool(vsd.get("is_active")) and print_state == "printing"
    if not active:
        # Reset per-print progress baseline when idle/complete; stay silent.
        state.update({
            "active_job_key": job_key if has_job else None,
            "baseline_progress": None,
            "baseline_layer": None,
            "baseline_at": None,
            "temp_lag_started_at": None,
        })
        save_state(state)
        return 0

    if state.get("active_job_key") != job_key:
        state.update({
            "active_job_key": job_key,
            "baseline_progress": progress,
            "baseline_layer": layer,
            "baseline_at": ts,
            "last_alert_key": None,
            "temp_lag_started_at": None,
        })

    # Progress stall detection: only after a long unchanged window.
    baseline_progress = state.get("baseline_progress")
    baseline_layer = state.get("baseline_layer")
    baseline_at = float(state.get("baseline_at") or ts)
    progressed = False
    if isinstance(progress, float) and isinstance(baseline_progress, (int, float)) and progress > float(baseline_progress) + 0.001:
        progressed = True
    if isinstance(layer, int) and isinstance(baseline_layer, int) and layer > baseline_layer:
        progressed = True

    if progressed:
        state.update({"baseline_progress": progress, "baseline_layer": layer, "baseline_at": ts})
    elif ts - baseline_at >= NO_PROGRESS_SECONDS:
        minutes = int((ts - baseline_at) // 60)
        alert_once(state, f"no_progress|{job_key}|{int(baseline_at // 600)}", issue_message("no progress", st, f"No progress/layer change for about {minutes} minutes"))

    # Temperature lag detection after warmup allowance.
    bed = st.get("heater_bed", {})
    ext_name = active_extruder(st)
    ext = st.get(ext_name, {}) or {}
    lagging = []
    for label, obj in [("bed", bed), (ext_name, ext)]:
        try:
            target = float(obj.get("target") or 0)
            temp = float(obj.get("temperature") or 0)
        except Exception:
            continue
        if target > 0 and temp < target - TEMP_LAG_C:
            lagging.append(f"{label} {rounded(temp)}/{rounded(target)}°C")

    if lagging:
        started = state.get("temp_lag_started_at")
        if not started:
            state["temp_lag_started_at"] = ts
        elif ts - float(started) >= TEMP_LAG_SECONDS:
            alert_once(state, f"temp_lag|{job_key}|{int(float(started) // 600)}", issue_message("temperature lag", st, ", ".join(lagging)))
    else:
        state["temp_lag_started_at"] = None

    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
