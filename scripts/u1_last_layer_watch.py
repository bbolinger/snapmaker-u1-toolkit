#!/usr/bin/env python3
"""Silent Snapmaker U1 layer-photo watcher.

Designed for Hermes cron no_agent mode: print nothing unless an active print reaches
an operator-useful layer milestone, then capture a fresh camera image and print a
Telegram-ready notification with MEDIA:<path>.

Milestones:
- first-layer/bed-adhesion check: first observed layer 2 through 5, once per job
- last-layer check: about five minutes of printing left at this print's real
  pace (final or next-to-final layer when there is no estimate), once per job

No movement/heating/G-code/start/cancel commands.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from u1_config import get_u1_host, get_u1_port, get_data_dir


def _base_url() -> str:
    return f"http://{get_u1_host()}:{get_u1_port()}"


def _out_dir() -> Path:
    return get_data_dir() / "last_layer"


def _state_path() -> Path:
    return _out_dir() / "last_layer_watch_state.json"


def _watchdog_state_path() -> Path:
    return get_data_dir() / "watchdog" / "u1_print_watchdog_state.json"


def _camera_helper() -> str:
    return str(Path(__file__).resolve().parent / "u1_camera.py")

# "Basically done" is measured in time, not layers. A fixed layer count was
# wrong in both directions: 6 layers is seconds on a 900-layer figure but over
# an hour on a 20-layer print with slow layers (live 2026-09-27: three
# pumpkin plates were announced as done 65-101 minutes before they finished).
#
# The time has to be this print's, not the slicer's. Estimate minus elapsed
# never reaches five minutes when a print runs faster than sliced (live
# 2026-10-08: a 5h56m estimate finished in 5h49m, so the photo came after the
# print). The watcher reads the slicer's own time-left mark (M73 R) at the
# printer's spot in the file and scales it by this print's pace so far. With
# no estimate or no marks it falls back to the final or next-to-final layer. A
# print that finishes between two polls is still caught by the post-complete
# fallback in main().

FIRST_LAYER_TARGET = 2
FIRST_LAYER_MAX = 5
LAST_LAYER_SECONDS_LEFT = 300
LAST_LAYER_NO_ESTIMATE_LAYERS = 1
# Pace is trusted only once the slicer itself counts this little time left, so
# an odd pace early in a print can never announce it as done.
LAST_LAYER_PACE_FROM_SECONDS = 1800
# Start reading the file for time-left marks once half of it has been printed.
LAST_LAYER_CHECK_FROM_PROGRESS = 0.5
# Slicer marks sit at most ~220 KB apart (5h56m print); read back this far.
SLICER_MARK_READ_BYTES = (256 * 1024, 1024 * 1024)

# Auto-off cavity LED after a print finishes. Default 300s grace so the
# operator can inspect the bed before the cavity goes dark. Override with
# U1_LED_OFF_DELAY_SEC=0 for immediate, or a high number to effectively disable.
LED_FINISHED_STATES = {"complete", "error", "cancelled"}
LED_OFF_DELAY_SEC = int(os.environ.get("U1_LED_OFF_DELAY_SEC", "300"))


def maybe_dim_led_after_finish(state: dict, print_state: str | None, job_key: str) -> None:
    """One-shot LED off after the print enters a finished state and the grace
    period elapses. State is mutated in place; caller is responsible for the
    save_state(state) flush. Failures don't propagate — the watcher's primary
    job is layer photos, not LED control."""
    if print_state in {"printing", "paused"}:
        state.pop("led_off_pending_at", None)
        state.pop("led_off_pending_key", None)
        return
    if print_state not in LED_FINISHED_STATES:
        return
    if state.get("led_off_fired_job_key") == job_key:
        return

    now = datetime.now(timezone.utc)
    pending_at_str = state.get("led_off_pending_at")
    pending_key = state.get("led_off_pending_key")
    if not pending_at_str or pending_key != job_key:
        state["led_off_pending_at"] = now.isoformat()
        state["led_off_pending_key"] = job_key
        return

    try:
        pending_at = datetime.fromisoformat(pending_at_str.replace("Z", "+00:00"))
    except Exception:
        pending_at = now
    if (now - pending_at) < timedelta(seconds=LED_OFF_DELAY_SEC):
        return

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import u1_led
        if u1_led.is_on():
            u1_led.off()
            state["led_off_fired_at"] = now.isoformat()
            state["led_off_fired_action"] = "off"
        else:
            state["led_off_fired_at"] = now.isoformat()
            state["led_off_fired_action"] = "noop_already_off"
        state["led_off_fired_job_key"] = job_key
        state.pop("led_off_pending_at", None)
        state.pop("led_off_pending_key", None)
    except Exception as exc:
        state["led_off_last_error"] = str(exc)
        state["led_off_last_error_at"] = now.isoformat()


def http_json(path: str, timeout: float = 8.0) -> dict[str, Any]:
    with urllib.request.urlopen(f"{_base_url()}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_range(path: str, start: int, end: int, timeout: float = 8.0) -> bytes | None:
    """Bytes start..end (inclusive) of a file Moonraker serves, or None when the
    server answers anything but a partial response (never pull a whole file)."""
    req = urllib.request.Request(f"{_base_url()}{path}", headers={"Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read() if r.status == 206 else None


def query_status() -> dict[str, Any]:
    q = "print_stats&display_status&virtual_sdcard&pause_resume&heater_bed&toolhead&extruder&extruder1&extruder2&extruder3"
    return http_json(f"/printer/objects/query?{q}")["result"]["status"]


def fetch_estimated_time(filename: str) -> float | None:
    """Slicer's whole-print estimate in seconds, from Moonraker file metadata."""
    try:
        meta = http_json(f"/server/files/metadata?filename={urllib.parse.quote(filename)}")
    except Exception:
        return None
    est = (meta.get("result") or {}).get("estimated_time")
    return float(est) if isinstance(est, (int, float)) and est > 0 else None


def fetch_slicer_seconds_left(filename: str, file_position: int) -> float | None:
    """The slicer's time-left mark (``M73 ... R<minutes>``) last passed before
    ``file_position``, read with ranged requests instead of fetching the file."""
    path = f"/server/files/gcodes/{urllib.parse.quote(filename, safe='/')}"
    for span in SLICER_MARK_READ_BYTES:
        start = max(0, file_position - span)
        try:
            chunk = http_range(path, start, file_position - 1)
        except Exception:
            return None
        if chunk is None:
            return None
        lines = chunk.split(b"\n")
        if start > 0:
            lines = lines[1:]  # cut mid-line
        if not chunk.endswith(b"\n"):
            lines = lines[:-1]
        for line in reversed(lines):
            if line.startswith(b"M73 "):
                m = re.search(rb"\bR(\d+)", line)
                if m:
                    return int(m.group(1)) * 60.0
        if start == 0:
            return None
    return None


def estimated_seconds_left(state: dict[str, Any], job_key: str, filename: str, ps: dict[str, Any],
                           vsd: dict[str, Any]) -> float | None:
    """Time left at this print's real pace, or None when it cannot be worked out.

    The slicer's time left at the printer's spot in the file, scaled by how this
    print has run against the slicer so far (elapsed / slicer time done). The
    whole-print estimate is cached per job so the metadata is fetched once."""
    elapsed = ps.get("print_duration")
    position = vsd.get("file_position")
    if not isinstance(elapsed, (int, float)) or not isinstance(position, int) or position <= 0:
        return None
    if state.get("estimated_time_job_key") != job_key:
        est = fetch_estimated_time(filename)
        if est is None:
            return None
        state["estimated_time_job_key"] = job_key
        state["estimated_time_s"] = est
    slicer_left = fetch_slicer_seconds_left(filename, position)
    if slicer_left is None:
        return None
    slicer_done = state["estimated_time_s"] - slicer_left
    if slicer_left > LAST_LAYER_PACE_FROM_SECONDS or slicer_done <= 0:
        return slicer_left
    return slicer_left * elapsed / slicer_done


def last_layer_due(remaining_layers: int, seconds_left: float | None) -> bool:
    if seconds_left is None:
        return remaining_layers <= LAST_LAYER_NO_ESTIMATE_LAYERS
    return seconds_left <= LAST_LAYER_SECONDS_LEFT


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def load_state() -> dict[str, Any]:
    return read_json(_state_path(), {})


def save_state(state: dict[str, Any]) -> None:
    _out_dir().mkdir(parents=True, exist_ok=True)
    _state_path().write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def rounded(v: Any, ndigits: int = 1) -> Any:
    return round(float(v), ndigits) if isinstance(v, (int, float)) else v


def capture_photo(filename: str, milestone: str, layer: int, total_layer: int) -> tuple[Path, dict[str, Any]]:
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in filename)[:80] or "u1_print"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = _out_dir() / f"{stamp}_{milestone}_layer_{layer}_of_{total_layer}_{safe_name}.jpg"
    cmd = [sys.executable, _camera_helper(), "watch", "--output", str(out), "--timeout", "25", "--wait", "2", "--poll", "2"]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"camera helper exited {proc.returncode}")
    payload = json.loads(proc.stdout)
    photo = payload.get("result") or payload.get("last_result") or {}
    if not payload.get("ok") or not photo.get("jpeg_magic") or not photo.get("changed"):
        raise RuntimeError(f"fresh camera capture failed or was stale: {payload}")
    return out, payload


def active_extruder_name(st: dict[str, Any]) -> str:
    return (st.get("toolhead") or {}).get("extruder") or "extruder"


def print_photo_message(milestone: str, filename: str, current_layer: int, total_layer: int, progress: Any, st: dict[str, Any], image: Path) -> None:
    ext_name = active_extruder_name(st)
    ext = st.get(ext_name, {}) or st.get("extruder", {})
    bed = st.get("heater_bed", {})
    pct = rounded(progress * 100, 1) if isinstance(progress, (int, float)) else "unknown"
    if milestone == "first_layer_check":
        headline = "U1 first-layer / bed-adhesion photo captured."
    elif milestone == "post_resume_check":
        headline = "U1 post-resume layer photo captured."
    else:
        headline = "U1 is basically done — last-layer photo captured."
    print(
        f"{headline}\n"
        f"- File: {filename}\n"
        f"- Layer: {current_layer} / {total_layer}\n"
        f"- Progress: {pct}%\n"
        f"- Temps: bed {rounded(bed.get('temperature'))}/{rounded(bed.get('target'))}°C, "
        f"{ext_name} {rounded(ext.get('temperature'))}/{rounded(ext.get('target'))}°C\n"
        f"MEDIA:{image}"
    )


def main() -> int:
    _out_dir().mkdir(parents=True, exist_ok=True)
    try:
        st = query_status()
    except Exception as exc:
        # Silent on transient reachability errors; the 20-minute watchdog handles repeated outage alerts.
        save_state({**load_state(), "last_error": str(exc), "last_error_at": datetime.now(timezone.utc).isoformat()})
        return 0

    ps = st.get("print_stats", {})
    info = ps.get("info") or {}
    vsd = st.get("virtual_sdcard", {})
    display = st.get("display_status", {})
    pause = st.get("pause_resume", {})
    filename = ps.get("filename") or ""
    current_layer = info.get("current_layer")
    total_layer = info.get("total_layer")
    print_state = ps.get("state")
    progress = display.get("progress")
    if progress is None:
        progress = vsd.get("progress")

    state = load_state()

    # Re-run detection: detect operator-restart of same gcode via two signals
    # (either is sufficient) so we don't miss the rerun on the edge where
    # current_layer is None on the first tick of the new print.
    #
    #   1. layer regression — current_layer drops while filename is unchanged
    #      (covers: rerun where Klipper already reports a layer on tick 1)
    #   2. state transition — prev print_state was finished/idle and current
    #      is "printing" with same filename (covers: rerun where Klipper
    #      hasn't reported a layer yet so current_layer is None)
    #
    # Without this, the job_key (filename|total_layer) is identical to the
    # previous run and every fired_*_job_key marker silently suppresses
    # re-firing milestone photos for the new session.
    prev_layer = state.get("current_layer")
    prev_total_layer = state.get("total_layer")
    prev_filename = state.get("filename") or ""
    prev_state_recorded = state.get("print_state")
    is_layer_regression = (
        isinstance(prev_layer, int)
        and isinstance(current_layer, int)
        and current_layer < prev_layer
    )
    is_state_restart = (
        prev_state_recorded in {"complete", "error", "cancelled", "standby"}
        and print_state == "printing"
    )
    if (
        (is_layer_regression or is_state_restart)
        and filename == prev_filename
        and filename != ""
    ):
        for k in (
            "first_layer_fired_job_key", "first_layer_fired_at",
            "first_layer_fired_layer", "first_layer_image",
            "first_layer_camera_changed",
            "last_layer_fired_job_key", "last_layer_fired_at",
            "last_layer_fired_layer", "last_layer_fired_total_layer",
            "last_layer_image", "last_layer_camera_changed",
            "post_resume_fired_job_key", "post_resume_fired_at",
            "post_resume_fired_layer", "post_resume_image",
            "post_resume_camera_changed",
            "led_off_fired_job_key", "led_off_fired_at",
            "led_off_fired_action",
            "fired_job_key", "fired_at", "fired_layer",
            "fired_total_layer", "image", "camera_changed",
        ):
            state.pop(k, None)
        state["rerun_detected_at"] = datetime.now(timezone.utc).isoformat()
        state["rerun_from_layer"] = prev_layer
        state["rerun_to_layer"] = current_layer
        state["rerun_trigger"] = (
            "layer_regression" if is_layer_regression else "state_restart"
        )
        state["rerun_prev_state"] = prev_state_recorded

    state.update({
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
        "filename": filename,
        "print_state": print_state,
        "current_layer": current_layer,
        "total_layer": total_layer,
        "progress": progress,
    })

    # LED auto-off runs every tick, independent of active-print gate, so a
    # transition complete → grace → off works even between photo milestones.
    led_key = f"{filename}|{total_layer}" if filename and total_layer else f"unknown|{print_state}"
    maybe_dim_led_after_finish(state, print_state, led_key)

    active = bool(vsd.get("is_active")) and print_state == "printing" and not pause.get("is_paused")
    if not active or not filename or not isinstance(current_layer, int) or not isinstance(total_layer, int) or total_layer <= 0:
        # Fallback: catch a job that finished so fast its last-layer window never
        # overlapped a poll tick while print_state was still "printing" (live
        # 2026-07-05: a 48-layer/43-min print went printing -> complete between
        # two 1-minute ticks, so the in-progress branch below never fired).
        # Detect the printing -> terminal transition for the SAME job and, if
        # last_layer never fired for it, capture one now using the last known
        # layer numbers (this tick's, if Moonraker still serves them; else the
        # previous tick's snapshot) instead of silently losing the milestone.
        prev_job_key = (f"{prev_filename}|{prev_total_layer}"
                        if prev_filename and prev_total_layer else None)
        if (
            prev_state_recorded == "printing"
            # Completed prints only: a cancelled or failed job did not reach
            # its last layer (live 2026-09-22: a print cancelled at layer 2 of
            # 495 was announced as finished).
            and print_state == "complete"
            and prev_job_key
            # SAME job only: the U1 persists print_stats.filename through the
            # terminal state (verified 2026-07-05), so a mismatched non-empty
            # filename means a DIFFERENT job appeared complete between ticks
            # (e.g. plate N finished + plate N+1 auto-started + finished) — do
            # not capture the current bed and mislabel it as the prior job.
            and (not filename or filename == prev_filename)
            and state.get("last_layer_fired_job_key") != prev_job_key
        ):
            fb_layer = current_layer if isinstance(current_layer, int) else prev_layer
            fb_total = total_layer if isinstance(total_layer, int) and total_layer > 0 else prev_total_layer
            if isinstance(fb_layer, int) and isinstance(fb_total, int) and fb_total > 0:
                try:
                    image, cam = capture_photo(prev_filename, "last_layer_post_complete", fb_layer, fb_total)
                except Exception as exc:
                    state.update({"last_capture_error": str(exc),
                                 "last_capture_error_at": datetime.now(timezone.utc).isoformat()})
                    save_state(state)
                    return 0
                fired_at = datetime.now(timezone.utc).isoformat()
                state.update({
                    "last_layer_fired_job_key": prev_job_key,
                    "last_layer_fired_at": fired_at,
                    "last_layer_fired_layer": fb_layer,
                    "last_layer_fired_total_layer": fb_total,
                    "last_layer_image": str(image),
                    "last_layer_camera_changed": bool((cam.get("result") or {}).get("changed")),
                    "last_layer_caught_post_complete": True,
                })
                save_state(state)
                print(
                    f"U1 finished before the live last-layer window landed a poll — "
                    f"capturing now.\n- File: {prev_filename}\n- Layer: {fb_layer} / {fb_total}\n"
                    f"- Final state: {print_state}"
                )
                return 0
        save_state(state)
        return 0

    job_key = f"{filename}|{total_layer}"
    milestones: list[str] = []
    watchdog_state = read_json(_watchdog_state_path(), {})

    pending_resume_job = watchdog_state.get("pending_resume_check_job_key")
    pending_resume_layer = watchdog_state.get("pending_resume_check_layer")
    if (
        pending_resume_job == job_key
        and watchdog_state.get("pending_resume_check_fired") is not True
        and isinstance(pending_resume_layer, int)
        and current_layer > pending_resume_layer
    ):
        milestones.append("post_resume_check")

    if state.get("first_layer_fired_job_key") != job_key and FIRST_LAYER_TARGET <= current_layer <= FIRST_LAYER_MAX:
        milestones.append("first_layer_check")

    remaining_layers = total_layer - current_layer
    file_progress = vsd.get("progress")
    near_end = (remaining_layers <= LAST_LAYER_NO_ESTIMATE_LAYERS
                or (isinstance(file_progress, (int, float)) and file_progress >= LAST_LAYER_CHECK_FROM_PROGRESS))
    if state.get("last_layer_fired_job_key") != job_key and near_end:
        seconds_left = estimated_seconds_left(state, job_key, filename, ps, vsd)
        if last_layer_due(remaining_layers, seconds_left):
            milestones.append("last_layer")

    if not milestones:
        save_state(state)
        return 0

    # If both somehow happen, first-layer is impossible on a sane print, but keep deterministic order.
    milestone = milestones[0]
    try:
        image, cam = capture_photo(filename, milestone, current_layer, total_layer)
    except Exception as exc:
        state.update({"last_capture_error": str(exc), "last_capture_error_at": datetime.now(timezone.utc).isoformat()})
        save_state(state)
        print(
            f"U1 reached {milestone.replace('_', ' ')}, but fresh camera capture failed.\n"
            f"- File: {filename}\n"
            f"- Layer: {current_layer} / {total_layer}\n"
            f"- Progress: {rounded(progress * 100, 1) if isinstance(progress, (int, float)) else 'unknown'}%\n"
            f"- Camera error: {exc}"
        )
        return 0

    fired_at = datetime.now(timezone.utc).isoformat()
    if milestone == "post_resume_check":
        state.update({
            "post_resume_fired_job_key": job_key,
            "post_resume_fired_at": fired_at,
            "post_resume_fired_layer": current_layer,
            "post_resume_image": str(image),
            "post_resume_camera_changed": bool((cam.get("result") or {}).get("changed")),
        })
        watchdog_state.update({
            "pending_resume_check_fired": True,
            "pending_resume_check_fired_at": fired_at,
            "pending_resume_check_fired_layer": current_layer,
            "pending_resume_check_image": str(image),
        })
        _watchdog_state_path().parent.mkdir(parents=True, exist_ok=True)
        _watchdog_state_path().write_text(json.dumps(watchdog_state, indent=2, sort_keys=True) + "\n")
    elif milestone == "first_layer_check":
        state.update({
            "first_layer_fired_job_key": job_key,
            "first_layer_fired_at": fired_at,
            "first_layer_fired_layer": current_layer,
            "first_layer_image": str(image),
            "first_layer_camera_changed": bool((cam.get("result") or {}).get("changed")),
        })
    else:
        state.update({
            "last_layer_fired_job_key": job_key,
            "last_layer_fired_at": fired_at,
            "last_layer_fired_layer": current_layer,
            "last_layer_fired_total_layer": total_layer,
            "last_layer_image": str(image),
            "last_layer_camera_changed": bool((cam.get("result") or {}).get("changed")),
        })
    save_state(state)
    print_photo_message(milestone, filename, current_layer, total_layer, progress, st, image)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
