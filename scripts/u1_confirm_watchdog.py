#!/usr/bin/env python3
"""Expiry watchdog for one bed-clear confirmation window.

Spawned detached by u1_kit_workflow._spawn_confirm_expiry_watchdog with the
request id, filename, marker path, and TTL passed as ARGV — never
interpolated into a `python3 -c` string (cold-review finding 2026-07-07:
the old string-format build let a crafted filename produce runnable code).

Behavior: sleep out the TTL, then — only if the marker still exists —
remove it and DM the operator that the window expired unredeemed. A
redeemed or cancelled window has no marker left, so the watchdog exits
silently. Best-effort throughout: a watchdog failure changes nothing about
safety, only about feedback.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from u1_runtime_paths import script_path as _script_path

_NOTIFY_PY = _script_path("u1_notify.py")


def run(request_id: str, filename: str, marker_path: str, ttl: int,
        generation: str = "", *, sleep=time.sleep) -> str:
    """Returns 'notified' | 'silent' (marker gone or a NEWER window replaced
    it) | 'error'. `sleep` is injectable so a test can drive it without
    waiting.

    Generation guard (cold-audit finding 2026-07-07): the marker is
    request-scoped, so re-prompting the SAME request overwrites it and each
    arm starts its own watchdog. Without a generation token an old watchdog
    would wake and delete the NEW window. It now deletes only when the
    marker on disk still carries the generation this watchdog was armed
    with — a re-armed window has a different generation and is left alone."""
    sleep(ttl)
    marker = Path(marker_path)
    try:
        current_gen = json.loads(marker.read_text()).get("generation", "")
    except FileNotFoundError:
        return "silent"                       # redeemed / cancelled
    except Exception:
        current_gen = None                    # unreadable — do not touch
    if generation and current_gen != generation:
        return "silent"                       # a newer window owns this path
    try:
        marker.unlink()
    except FileNotFoundError:
        return "silent"  # redeemed/cancelled between the check and the unlink
    except Exception:
        return "error"
    msg = (f"⏳ The bed-clear window for {filename or 'the pending print'} "
           "expired with no YES. Nothing was printed. Re-run the flow when "
           "ready.")
    try:
        subprocess.run([sys.executable, _NOTIFY_PY, msg], timeout=30)
    except Exception:
        return "error"
    return "notified"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("request_id")
    ap.add_argument("filename")
    ap.add_argument("marker_path")
    ap.add_argument("ttl", type=int)
    ap.add_argument("generation", nargs="?", default="")
    a = ap.parse_args(argv)
    run(a.request_id, a.filename, a.marker_path, a.ttl, a.generation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
