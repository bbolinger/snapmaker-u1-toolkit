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
import os
import subprocess
import sys
import time
from pathlib import Path

_NOTIFY_PY = "/opt/data/scripts/u1_notify.py"


def run(request_id: str, filename: str, marker_path: str, ttl: int,
        *, sleep=time.sleep) -> str:
    """Returns one of: 'notified' | 'silent' (marker gone) | 'error'.
    `sleep` is injectable so a test can drive it without waiting."""
    sleep(ttl)
    marker = Path(marker_path)
    if not marker.exists():
        return "silent"
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
        subprocess.run(["python3", _NOTIFY_PY, msg], timeout=30)
    except Exception:
        return "error"
    return "notified"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("request_id")
    ap.add_argument("filename")
    ap.add_argument("marker_path")
    ap.add_argument("ttl", type=int)
    a = ap.parse_args(argv)
    run(a.request_id, a.filename, a.marker_path, a.ttl)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
