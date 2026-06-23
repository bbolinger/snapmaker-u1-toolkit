#!/usr/bin/env python3
"""Snapmaker U1 cavity LED control (read + set + photo wrap context manager).

The U1's cavity LED is white-only. Per the live printer.cfg shipped by
Snapmaker, the LED is configured as:

    [led cavity_led]
    white_pin: PA10

There is no red_pin / green_pin / blue_pin. Klipper's `[led]` interface
always exposes four channels (R/G/B/W) in `color_data`, but on the U1
only the W channel is physically wired. Setting R/G/B values via SET_LED
is harmless but has no visible effect.

This module keeps the 4-channel API to match Klipper, while every helper
that "turns on" the LED uses WHITE=1 — which is the only channel that
will actually light up.

Reading the current state goes through `printer/objects/query`; setting
goes through `printer/gcode/script?script=SET_LED ...`.

CLI usage:
    u1_led.py status                 # print current (R, G, B, W) state
    u1_led.py on                     # full white (WHITE=1)
    u1_led.py off                    # all channels 0
    u1_led.py set --w 0.5            # half-brightness white
    u1_led.py is-on                  # exit 0 if any channel > 0, else 1

Library usage:
    from u1_led import photo_wrap
    with photo_wrap():
        capture()

The context manager queries current state, sets WHITE=1 if all channels
are zero, yields, then restores the prior state. If LED was already on,
nothing changes (no visible flicker during a normal photo).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from u1_config import get_u1_host, get_u1_port  # noqa: E402

LED_OBJECT = "led cavity_led"
LED_NAME = "cavity_led"

# After turn-on, give the camera's auto-exposure time to adapt to the bright
# cavity. The U1's monitor camera is slow to converge — 0.3s produced black
# (no adaptation), 3.0s produced blown-out (partial adaptation, frame still
# captured before stable). 5.0s puts capture at ~7s LED-on which gives the
# camera enough time to land at a properly-exposed image of a bright cavity
# with white prints. If this is still blown-out, the camera may have fixed
# exposure metering and we'll need to lower LED brightness (W < 1) instead.
PHOTO_SETTLE_SEC = 5.0

# After SET_LED, poll printer/objects/query until query reports any channel >
# 0, defending against the rare case where Klipper accepts the script but
# doesn't actually drive the pin (hardware glitch / config mismatch). In the
# normal case the first poll succeeds within a few hundred ms.
LED_ON_CONFIRM_TIMEOUT_SEC = 3.0
LED_ON_CONFIRM_POLL_SEC = 0.3

# /printer/gcode/script BLOCKS until Klipper processes the script. During an
# active print the gcode queue may be busy (extruder change, layer change,
# complex move) so SET_LED can stall for several seconds. 8s was too tight —
# saw silent timeouts producing dark photos. 15s gives margin without eating
# most of a 60s cron tick if something is wedged.
LED_GCODE_TIMEOUT_SEC = 15.0
LED_QUERY_TIMEOUT_SEC = 15.0


def _base() -> str:
    return f"http://{get_u1_host()}:{get_u1_port()}"


def _http_json(path: str, timeout: float = LED_GCODE_TIMEOUT_SEC) -> dict:
    with urllib.request.urlopen(f"{_base()}{path}", timeout=timeout) as r:
        return json.loads(r.read())


def query() -> tuple[float, float, float, float]:
    """Return current LED state as (R, G, B, W) tuple. Each channel 0.0–1.0.

    On the U1's cavity_led only W is physically wired; R/G/B will report
    whatever was last set via SET_LED but have no visible effect.
    """
    obj_quoted = urllib.parse.quote(LED_OBJECT)
    data = _http_json(f"/printer/objects/query?{obj_quoted}", timeout=LED_QUERY_TIMEOUT_SEC)
    status = (data.get("result") or {}).get("status") or {}
    led = status.get(LED_OBJECT) or {}
    segments = led.get("color_data") or []
    if not segments:
        return (0.0, 0.0, 0.0, 0.0)
    seg = segments[0]
    while len(seg) < 4:
        seg = list(seg) + [0.0]
    r, g, b, w = float(seg[0]), float(seg[1]), float(seg[2]), float(seg[3])
    return (r, g, b, w)


def is_on() -> bool:
    """LED considered 'on' if any channel is non-zero."""
    return any(c > 0.0 for c in query())


def set_state(r: float, g: float, b: float, w: float = 0.0) -> None:
    """Send SET_LED via Moonraker's gcode_script endpoint."""
    script = f"SET_LED LED={LED_NAME} RED={r:.3f} GREEN={g:.3f} BLUE={b:.3f} WHITE={w:.3f}"
    url = f"/printer/gcode/script?script={urllib.parse.quote(script)}"
    _http_json(url, timeout=LED_GCODE_TIMEOUT_SEC)


def on() -> None:
    """Full white via the W channel (the only physically wired channel on the U1)."""
    set_state(0.0, 0.0, 0.0, 1.0)


def off() -> None:
    set_state(0.0, 0.0, 0.0, 0.0)


@contextmanager
def photo_wrap(settle_sec: float = PHOTO_SETTLE_SEC):
    """Ensure the LED is on for a photo capture, restoring prior state on exit.

    If the LED was already on (any channel > 0), nothing changes — no flicker.
    If it was off, turn on (WHITE=1), settle briefly, yield, then restore off.

    Failures in the LED layer don't propagate — photo capture is more important
    than perfect LED state.
    """
    prior: tuple[float, float, float, float] | None
    try:
        prior = query()
    except Exception as exc:
        print(f"[u1_led] photo_wrap: query failed ({exc}); not touching LED", file=sys.stderr)
        prior = None

    turned_on = False
    if prior is not None and all(c == 0.0 for c in prior):
        try:
            on()
            turned_on = True
            # Confirm via query — defends against Klipper accepting SET_LED but
            # not actually driving the pin (rare hardware glitch / wiring issue
            # / config mismatch). In the common case the first poll succeeds.
            confirm_deadline = time.time() + LED_ON_CONFIRM_TIMEOUT_SEC
            confirmed = False
            while time.time() < confirm_deadline:
                try:
                    if any(c > 0.0 for c in query()):
                        confirmed = True
                        break
                except Exception:
                    pass
                time.sleep(LED_ON_CONFIRM_POLL_SEC)
            if not confirmed:
                print(
                    f"[u1_led] WARNING photo_wrap: SET_LED accepted but query never "
                    f"reported any channel > 0 within {LED_ON_CONFIRM_TIMEOUT_SEC}s; "
                    f"capturing anyway — image may be dark or LED may be physically broken.",
                    file=sys.stderr,
                )
            # Settle for the camera's auto-exposure to adapt to the now-bright cavity.
            if settle_sec > 0:
                time.sleep(settle_sec)
        except Exception as exc:
            # Loud warning — silent failure here produces dark photos with no
            # other visible signal. Most common cause: SET_LED queued behind a
            # long-running gcode and the HTTP call timed out
            # (LED_GCODE_TIMEOUT_SEC). If you see this repeatedly, the
            # printer's gcode queue is heavily backed up.
            print(
                f"[u1_led] WARNING photo_wrap: SET_LED turn-on FAILED ({exc!r}); "
                f"capturing anyway with LED still OFF — image will be dark. "
                f"Bump LED_GCODE_TIMEOUT_SEC or investigate printer gcode queue stall.",
                file=sys.stderr,
            )

    try:
        yield
    finally:
        if turned_on:
            try:
                set_state(*prior)
            except Exception as exc:
                print(f"[u1_led] photo_wrap: restore failed ({exc}); LED left on", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="Snapmaker U1 cavity LED helper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="Print current (R, G, B, W) state as JSON")
    sub.add_parser("on", help="Full white (WHITE=1)")
    sub.add_parser("off", help="All channels 0")
    sub.add_parser("is-on", help="Exit 0 if any channel > 0, else 1")
    p_set = sub.add_parser("set", help="Custom color")
    p_set.add_argument("--r", type=float, default=0.0)
    p_set.add_argument("--g", type=float, default=0.0)
    p_set.add_argument("--b", type=float, default=0.0)
    p_set.add_argument("--w", type=float, default=0.0)
    args = ap.parse_args()

    if args.cmd == "status":
        r, g, b, w = query()
        print(json.dumps({"r": r, "g": g, "b": b, "w": w, "on": any(c > 0 for c in (r, g, b, w))}, indent=2))
        return 0
    if args.cmd == "on":
        on()
        print(json.dumps({"ok": True, "action": "on"}))
        return 0
    if args.cmd == "off":
        off()
        print(json.dumps({"ok": True, "action": "off"}))
        return 0
    if args.cmd == "is-on":
        return 0 if is_on() else 1
    if args.cmd == "set":
        set_state(args.r, args.g, args.b, args.w)
        print(json.dumps({"ok": True, "action": "set", "r": args.r, "g": args.g, "b": args.b, "w": args.w}))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
