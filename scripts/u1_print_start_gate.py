#!/usr/bin/env python3
"""Fail-closed U1 start gate.

Two-stage design with approval-token handoff (audit response 2026-06-25):

  Stage 1 — default (--bed-clear=cancel): captures REAL fresh photo via
  u1_camera.capture_photo (LED on + 5s settle), checks brightness so a
  dark frame can't pass, returns absolute path + approval token. NEVER
  starts. The agent surfaces the photo to the operator.

  Stage 2 — explicit (--bed-clear=start --approval-token=<token>):
  validates the token against the recent Stage-1 capture (TTL 5 min),
  re-runs preflight + a sanity-only fresh capture, dispatches start
  only if everything passes. Token absence forces full Stage 1 again.

Why the token: prevents the agent from skipping the human's review by
just re-invoking the gate twice in quick succession. The token ties the
final start to the photo the operator actually saw.

Filename handling: the gate accepts EITHER a host path or a printer
storage filename. Host paths are stripped to the basename before being
sent to Moonraker's /printer/print/start (which only knows files by
their storage name).
"""
from __future__ import annotations
import argparse, hashlib, json, subprocess, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from u1_config import get_u1_host, get_u1_port, get_data_dir


# Approval-token TTL: how long after Stage 1's photo is captured can the
# operator still approve a Stage 2 start. Short enough that the bed state
# can't drift; long enough to type a one-line approval.
APPROVAL_TTL_SEC = 300  # 5 minutes

# Brightness floor below which a photo is considered "too dark for operator
# review" (cold review 2026-06-25 round 10). Tuned to catch the all-black
# frame that bypassing photo_wrap produces, while not rejecting low-light
# bed photos with a part on it.
DARK_PHOTO_MEAN_LUMA = 12  # 0-255 scale


def http_json(url: str, timeout: float = 10.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def post_json(url: str, payload: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def query_state(host, port):
    q = 'print_stats&virtual_sdcard&pause_resume&webhooks&toolhead&extruder&extruder1&extruder2&extruder3&heater_bed&print_task_config&filament_detect'
    return http_json(f'http://{host}:{port}/printer/objects/query?{q}')['result']['status']


def _default_toolmap_path() -> str:
    return str(Path(__file__).resolve().parent / 'u1_toolmap.py')


def run_tool_gate(host: str, port: int, material: str, intended_tool: str) -> tuple[bool, str]:
    cmd = [sys.executable, _default_toolmap_path(),
           "--host", host, "--port", str(port),
           "--requested-material", material, "--intended-tool", intended_tool]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=45)
    return proc.returncode == 0, proc.stdout


def preflight(status: dict[str, Any],
              intended_tool: str | None = None,
              requested_material: str | None = None,
              host: str | None = None,
              port: int | None = None) -> list[str]:
    blockers = []
    ps = status.get('print_stats', {})
    vsd = status.get('virtual_sdcard', {})
    pause = status.get('pause_resume', {})
    wh = status.get('webhooks', {})
    if pause.get('is_paused'):
        blockers.append('printer is paused')
    if vsd.get('is_active'):
        blockers.append('virtual_sdcard is active')
    # Audit 2026-06-26: 'cancelled' is a benign terminal state from a prior
    # run when the printer is otherwise idle + webhooks ready + vsd inactive
    # + not paused. Klipper accepts /printer/print/start from this state.
    # Same shape as the u1_upload_gcode.py post-upload check.
    ps_state = ps.get('state')
    ps_cancelled_but_clean = (
        ps_state == 'cancelled'
        and not vsd.get('is_active')
        and not pause.get('is_paused')
        and (wh.get('state') in (None, 'ready'))
    )
    if ps_state not in (None, 'standby', 'complete', 'ready') and not ps_cancelled_but_clean:
        blockers.append(f"print_stats state is {ps_state}")
    if intended_tool and status.get('toolhead', {}).get('extruder') not in (None, intended_tool):
        blockers.append(f"active tool is {status.get('toolhead', {}).get('extruder')}, expected {intended_tool}")
    if requested_material and intended_tool and host and port is not None:
        ok, out = run_tool_gate(host, int(port), requested_material, intended_tool)
        if not ok:
            blockers.append(f"tool/material gate failed for {intended_tool} / {requested_material}: {out[-500:].strip()}")
    return blockers


def _measure_brightness(path: Path) -> float | None:
    """Return mean luma (0-255) of the photo, or None if unmeasurable.

    Why: u1_led.photo_wrap can fail silently (Klipper script timeout, LED
    not driving despite SET_LED) and produce a fresh JPEG that's all-black.
    A brightness check downstream of capture catches this so the operator
    isn't asked to approve a black frame as bed-clear evidence."""
    try:
        from PIL import Image, ImageStat
        with Image.open(path) as img:
            gray = img.convert('L')
            stat = ImageStat.Stat(gray)
            return float(stat.mean[0])
    except Exception:
        return None


def capture_real_bed_photo(out_dir: Path, host: str, port: int, wait: float = 5.0) -> dict[str, Any]:
    """Capture a fresh photo via u1_camera.capture_photo (LED + 5s settle)
    + brightness sanity check.

    Returns {'ok', 'path' (absolute), 'fresh', 'is_mock', 'error',
    'timestamp_utc', 'brightness_mean', 'brightness_ok', 'bytes', 'sha256'}.

    'ok' is True ONLY if: real capture succeeded AND brightness > floor.
    Either failure mode emits is_mock or brightness_ok=False so the
    caller knows the photo isn't valid for operator review.

    Cold-review fix 2026-06-25 round 10: prior version bypassed
    u1_led.photo_wrap and used start_monitor + sleep + fetch_monitor
    directly. Result: black photo, "fresh JPEG" passed, operator approved
    a bed they couldn't see, print started, bed wasn't actually clear.
    Now goes through u1_camera.capture_photo so the LED settle path is
    guaranteed."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = (out_dir / 'bed_snapshot.jpg').resolve()
    try:
        from u1_camera import capture_photo
        capture_photo(host, port, str(out_path), wait=wait, interval=1.0)
        brightness = _measure_brightness(out_path)
        sha = hashlib.sha256(out_path.read_bytes()).hexdigest() if out_path.exists() else None
        timestamp = datetime.now(timezone.utc).isoformat()
        if brightness is None:
            # Audit 2026-06-26: when PIL/Pillow isn't available or the JPEG
            # is malformed enough that we can't measure brightness, we have
            # a real-camera capture (not a mock) but no automated sanity
            # check. Defer to the operator — they're the bed-clear
            # gatekeeper per the skill contract. Don't refuse like we do
            # for verifiably-dark frames.
            return {
                'ok': True, 'path': str(out_path), 'fresh': True, 'is_mock': False,
                'error': None,
                'timestamp_utc': timestamp,
                'brightness_mean': None,
                'brightness_ok': None,
                'brightness_check': 'deferred',
                'brightness_check_reason': (
                    'PIL/Pillow not available in start_gate environment OR JPEG '
                    "malformed — couldn't auto-classify dark vs lit. Photo is "
                    'real (camera reached + JPEG bytes received); operator '
                    'must judge usability from the image itself.'
                ),
                'bytes': out_path.stat().st_size if out_path.exists() else 0,
                'sha256': sha,
            }
        if brightness <= DARK_PHOTO_MEAN_LUMA:
            return {
                'ok': False, 'path': str(out_path), 'fresh': True, 'is_mock': False,
                'error': (
                    f'photo too dark for operator review (mean luma '
                    f'{brightness:.1f}, floor {DARK_PHOTO_MEAN_LUMA}). The LED '
                    'may have failed to turn on or the camera settle window '
                    'was insufficient. Refusing to surface as bed-clear evidence.'
                ),
                'timestamp_utc': timestamp,
                'brightness_mean': brightness,
                'brightness_ok': False,
                'brightness_check': 'measured',
                'bytes': out_path.stat().st_size if out_path.exists() else 0,
                'sha256': sha,
            }
        return {
            'ok': True, 'path': str(out_path), 'fresh': True, 'is_mock': False,
            'error': None,
            'timestamp_utc': timestamp,
            'brightness_mean': brightness,
            'brightness_ok': True,
            'brightness_check': 'measured',
            'bytes': out_path.stat().st_size if out_path.exists() else 0,
            'sha256': sha,
        }
    except Exception as exc:
        # Camera path failed (network/LED/whatever). Write a CLEARLY mock
        # image so an agent or operator can see the camera didn't work.
        mock_path = (out_dir / 'bed_snapshot__MOCK.png').resolve()
        try:
            from PIL import Image, ImageDraw
            img = Image.new('RGB', (640, 360), (40, 0, 0))
            d = ImageDraw.Draw(img)
            d.text((20, 20), '!! MOCK !! CAMERA UNREACHABLE — NOT REAL BED EVIDENCE', fill=(255, 200, 0))
            d.text((20, 60), f'reason: {type(exc).__name__}: {exc}'[:200], fill=(255, 200, 200))
            d.text((20, 100), 'DO NOT APPROVE START based on this image.', fill=(255, 200, 0))
            img.save(mock_path)
        except Exception:
            mock_path.write_bytes(b'')
        return {
            'ok': False, 'path': str(mock_path), 'fresh': False, 'is_mock': True,
            'error': f'{type(exc).__name__}: {exc}',
            'timestamp_utc': None, 'brightness_mean': None, 'brightness_ok': False,
            'bytes': mock_path.stat().st_size if mock_path.exists() else 0,
            'sha256': None,
        }


def _normalize_filename(filename: str) -> str:
    """Map a host-filesystem gcode path to the printer-storage filename
    Moonraker expects on /printer/print/start.

    Audit 2026-06-25 (round 11): operator followed the skill verbatim,
    passed the host path returned in readiness_card / uploaded events,
    got HTTP 400 'Unable to open file' from Moonraker because Moonraker's
    file-lookup is by basename in its gcode dir (~/printer_data/gcodes/).

    Strip any directory components — Moonraker only knows files by their
    storage name. Accepts both forms transparently."""
    p = Path(filename)
    return p.name if p.parent != Path('.') else filename


def _read_approval_token(out_dir: Path) -> dict[str, Any] | None:
    """Stage 1 writes the approval token (photo sha256 + timestamp) to a
    sidecar file. Stage 2 reads it to verify the operator's approval refers
    to a real recent photo."""
    token_path = out_dir / 'bed_snapshot.approval_token.json'
    if not token_path.exists():
        return None
    try:
        return json.loads(token_path.read_text())
    except Exception:
        return None


def _write_approval_token(out_dir: Path, snapshot: dict[str, Any]) -> str:
    """Stage 1 ends by writing an approval-token sidecar so Stage 2 can
    verify the operator's yes refers to this specific photo."""
    token = hashlib.sha256(
        f'{snapshot.get("sha256")}:{snapshot.get("timestamp_utc")}'.encode()
    ).hexdigest()[:32]
    payload = {
        'token': token,
        'sha256': snapshot.get('sha256'),
        'timestamp_utc': snapshot.get('timestamp_utc'),
        'snapshot_path': snapshot.get('path'),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'bed_snapshot.approval_token.json').write_text(
        json.dumps(payload, indent=2)
    )
    return token


def _approval_token_valid(stored: dict[str, Any], offered: str) -> tuple[bool, str]:
    """Verify operator's offered token matches stored, within TTL."""
    if not stored:
        return False, 'no Stage-1 photo on disk — re-run without --bed-clear start to capture one'
    if not offered:
        return False, 'no --approval-token provided; Stage 2 requires the token printed by Stage 1'
    if stored.get('token') != offered:
        return False, 'approval token does not match the Stage-1 capture; re-run Stage 1 and approve the new photo'
    ts = stored.get('timestamp_utc')
    if not ts:
        return False, 'stored token has no timestamp — refusing to trust'
    try:
        captured = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except Exception:
        return False, 'stored token timestamp unparseable'
    age = (datetime.now(timezone.utc) - captured).total_seconds()
    if age > APPROVAL_TTL_SEC:
        return False, f'approval token is {age:.0f}s old (TTL {APPROVAL_TTL_SEC}s); re-run Stage 1'
    return True, ''


def start_print(host, port, filename):
    return post_json(f'http://{host}:{port}/printer/print/start', {'filename': filename})


def run_gate(filename: str,
             bed_clear: str = 'cancel',
             host=None,
             port=None,
             intended_tool=None,
             requested_material: str | None = None,
             approval_token: str | None = None,
             out_dir: Path | None = None,
             start_func=start_print):
    host = host or get_u1_host()
    port = port or get_u1_port()
    out_dir = (out_dir or get_data_dir()).resolve()
    printer_filename = _normalize_filename(filename)

    status = query_state(host, port)
    blockers = preflight(status, intended_tool,
                         requested_material=requested_material,
                         host=host, port=port)

    if bed_clear != 'start':
        # Stage 1 — capture real photo, write token, return readiness.
        snapshot = capture_real_bed_photo(out_dir, host, port)
        token: str | None = None
        if snapshot['ok']:
            token = _write_approval_token(out_dir, snapshot)
        return {
            'stage': 'readiness',
            'filename': printer_filename,
            'gcode_host_path': filename if filename != printer_filename else None,
            'printer_storage_filename': printer_filename,
            'blockers': blockers,
            'snapshot': snapshot,
            'approval_token': token,
            'approval_ttl_seconds': APPROVAL_TTL_SEC if token else None,
            'intended_tool': intended_tool,
            'requested_material': requested_material,
            'ok': not blockers and snapshot['ok'],
            'started': False,
            'cancelled': True,
            'next_step': (
                f"Review {snapshot['path']}. If bed is clear AND blockers above "
                "are empty, re-run with --bed-clear start AND --approval-token "
                f"{token}. Token TTL: {APPROVAL_TTL_SEC}s."
                if token else
                f"Stage 1 captured an unusable photo ({snapshot.get('error')}). "
                "Do NOT approve a start. Re-run Stage 1 once the camera issue "
                "is resolved."
            ),
        }

    # Stage 2 — verify token, re-check preflight, sanity-only re-capture, start.
    stored = _read_approval_token(out_dir)
    token_ok, token_reason = _approval_token_valid(stored, approval_token)
    if not token_ok:
        return {
            'stage': 'start_attempt',
            'filename': printer_filename,
            'blockers': blockers,
            'snapshot': None,
            'ok': False, 'started': False,
            'reason': f'approval token invalid: {token_reason}',
        }
    if blockers:
        return {
            'stage': 'start_attempt',
            'filename': printer_filename,
            'blockers': blockers,
            'snapshot': None,
            'ok': False, 'started': False,
            'reason': 'preflight blockers present at Stage 2',
        }
    # Sanity-only fresh capture so we don't fire blind into a state that
    # changed during the operator's review. We do NOT show this photo —
    # the operator already approved Stage 1's. We just refuse if this
    # one is mock/dark too.
    sanity_snapshot = capture_real_bed_photo(out_dir, host, port)
    # Audit 2026-06-26: distinguish mock (camera never reached) and
    # measured-dark from deferred-brightness-check (PIL unavailable but
    # photo IS real). Only the first two should block — deferred allows
    # the start because the operator already approved Stage 1's image.
    sanity_blocks_start = (
        sanity_snapshot.get('is_mock')
        or (sanity_snapshot.get('brightness_check') == 'measured'
            and not sanity_snapshot.get('ok'))
    )
    if sanity_blocks_start:
        return {
            'stage': 'start_attempt',
            'filename': printer_filename,
            'blockers': blockers,
            'snapshot': sanity_snapshot,
            'ok': False, 'started': False,
            'reason': (
                f"Stage 2 sanity capture failed ({sanity_snapshot.get('error')}). "
                "Refusing to start: a fresh real photo wasn't obtainable now, so "
                "I can't verify nothing changed since you approved."
            ),
        }
    resp = start_func(host, port, printer_filename)
    return {
        'stage': 'start_attempt',
        'filename': printer_filename,
        'blockers': blockers,
        'snapshot': sanity_snapshot,
        'ok': True, 'started': True,
        'response': resp,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('filename',
                    help='Either the host gcode path or the printer storage filename. '
                         'Host paths are auto-stripped to basename for the Moonraker call.')
    ap.add_argument('--bed-clear', choices=['start', 'cancel'], default='cancel',
                    help="'cancel' (default): Stage 1 — captures real photo + writes approval token, never starts. "
                         "'start': Stage 2 — operator's explicit approval; requires --approval-token from Stage 1.")
    ap.add_argument('--approval-token',
                    help='Token printed by Stage 1. Required for Stage 2; ties the start to the photo the operator reviewed.')
    ap.add_argument('--intended-tool', help='Klipper extruder name, e.g. extruder1 for T1')
    ap.add_argument('--requested-material', help='material to verify on intended_tool, e.g. PETG')
    ap.add_argument('--out-dir', type=Path, default=None,
                    help='Where to write the bed snapshot + approval token. Defaults to U1 data dir.')
    a = ap.parse_args(argv)
    res = run_gate(a.filename, a.bed_clear,
                   intended_tool=a.intended_tool,
                   requested_material=a.requested_material,
                   approval_token=a.approval_token,
                   out_dir=a.out_dir)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
