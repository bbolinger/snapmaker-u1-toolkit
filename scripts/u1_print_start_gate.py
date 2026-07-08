#!/usr/bin/env python3
"""Fail-closed U1 start gate.

Two-stage design with approval-token handoff:

  Stage 1 — default (--bed-clear=cancel): captures REAL fresh photo via
  u1_camera.capture_photo (LED on + 5s settle), checks brightness so a
  dark frame can't pass, returns absolute path + approval token. NEVER
  starts. The agent surfaces the photo to the operator.

  Stage 2 — explicit (--bed-clear=start --approval-token=<token>):
  validates the token against the recent Stage-1 capture (TTL:
  APPROVAL_TTL_SEC, currently 30 minutes), re-runs preflight + a
  sanity-only fresh capture, dispatches start only if everything
  passes. Token absence forces full Stage 1 again.

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
# can't drift meaningfully (no human will walk in, mess with the bed,
# and walk out again in this window for a normal home/office deployment);
# long enough for an operator to set the phone down, deal with something,
# and come back to confirm. 30 min is long enough to set the phone
# down and deal with something, short enough that bed drift matters.
APPROVAL_TTL_SEC = 1800  # 30 minutes

# Brightness floor below which a photo is considered "too dark for operator
# review". Tuned to catch the all-black
# frame that bypassing photo_wrap produces, while not rejecting low-light
# bed photos with a part on it.
DARK_PHOTO_MEAN_LUMA = 12  # 0-255 scale

# Canonical deployed path of this gate script — used when building the Stage-1
# command string the workflow hands to the agent.
GATE_SCRIPT_PATH = "/opt/data/scripts/u1_print_start_gate.py"


def build_stage1_command(*, printer_filename: str, intended_tool: str,
                         material: str, request_id: str) -> str:
    """Build the Stage-1 gate command string (shared by the single-STL and kit
    workflows so the two never drift). Stage 1 captures a real bed photo +
    approval token; nothing starts until Stage 2 with that token + operator yes.
    """
    import shlex
    return (
        f"python3 {GATE_SCRIPT_PATH} {printer_filename} "
        f"--intended-tool {intended_tool} --requested-material {shlex.quote(str(material))} "
        f"--request-id {request_id}"
    )


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


# Maps Klipper extruder section names to the T-command the slicer emits
# in the gcode preamble. The U1's klipper config wraps T0/T1/T2/T3 as
# macros that fire ACTIVATE_EXTRUDER + the physical carousel pickup, so
# the gcode itself drives the tool change. Pre-activation is NOT required
# on the U1 — what matters is that the gcode contains the right T<N>
# activation in its preamble.
_EXTRUDER_TO_T_COMMAND = {
    'extruder':  'T0',
    'extruder1': 'T1',
    'extruder2': 'T2',
    'extruder3': 'T3',
}


def _gcode_has_tool_activation(gcode_path: Path | None, expected_t: str,
                               scan_lines: int = 3000) -> tuple[bool, str | None]:
    """Return (found, sample_line) — True if expected_t appears as a standalone
    command in the gcode's executable section.

    Looks for the bare T-command (e.g. ``T1`` at start of line) or the
    explicit M104/M109 with a matching T parameter, since OrcaSlicer emits
    both. Conservative: requires word-boundary match so ``T10`` doesn't
    falsely match ``T1``.

    Scans starting from ``; EXECUTABLE_BLOCK_START`` when present — OrcaSlicer
    puts a 900+ line config-comment header before any real gcode, so a naive
    top-of-file scan (500 lines) misses the T-activation and false-flags
    slicer-config-mismatch:
    T1 activation was at line 952 but the gate stopped scanning at 500).
    Falls back to first ``scan_lines`` if the marker is absent.
    """
    if gcode_path is None or not gcode_path.is_file():
        return False, None
    import re
    # Match T1 at start of line OR M104/M109 ... T1 ... as a standalone token
    pat = re.compile(rf'(?m)^(?:{re.escape(expected_t)}\b|M10[49]\b.*\b{re.escape(expected_t)}\b)')
    try:
        with gcode_path.open('r', encoding='utf-8', errors='ignore') as f:
            buf_lines: list[str] = []
            in_exec = False
            count = 0
            for line in f:
                if not in_exec:
                    buf_lines.append(line)
                    if line.strip().startswith('; EXECUTABLE_BLOCK_START'):
                        in_exec = True
                        buf_lines = [line]  # reset — scan from marker forward
                        count = 0
                    continue
                buf_lines.append(line)
                count += 1
                if count >= scan_lines:
                    break
            buf = ''.join(buf_lines)
            m = pat.search(buf)
            return (m is not None), (m.group(0) if m else None)
    except Exception:
        return False, None


def preflight(status: dict[str, Any],
              intended_tool: str | None = None,
              requested_material: str | None = None,
              host: str | None = None,
              port: int | None = None,
              gcode_path: Path | None = None,
              accept_material_mismatch: bool = False) -> list[str]:
    blockers = []
    ps = status.get('print_stats', {})
    vsd = status.get('virtual_sdcard', {})
    pause = status.get('pause_resume', {})
    wh = status.get('webhooks', {})
    if pause.get('is_paused'):
        blockers.append('printer is paused')
    if vsd.get('is_active'):
        blockers.append('virtual_sdcard is active')
    # 'cancelled' is a benign terminal state from a prior
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
    # Tool-activation check (replaces v2.0's idle-state check):
    # The original check refused if Klipper's `toolhead.extruder` (idle-state
    # last-activated extruder) didn't already match intended_tool. That logic
    # is correct for a single-extruder printer where the operator must
    # manually pick which extruder is active before printing — but it's
    # WRONG for the Snapmaker U1, which is a 4-tool changer where the gcode
    # itself drives tool selection via macros that wrap T0/T1/T2/T3
    # commands. The macros fire ACTIVATE_EXTRUDER + the physical carousel
    # pickup at print start. Pre-activation is never required on the U1.
    #
    # Correct check for the U1: verify the gcode's preamble contains the
    # expected T<N> activation command. Block only if missing — which would
    # indicate a slicer misconfiguration (intended_tool doesn't match what
    # the gcode actually targets).
    if intended_tool:
        expected_t = _EXTRUDER_TO_T_COMMAND.get(intended_tool)
        if expected_t is None:
            blockers.append(
                f"unknown intended_tool '{intended_tool}' — expected one of "
                f"{', '.join(sorted(_EXTRUDER_TO_T_COMMAND))}"
            )
        elif gcode_path is not None:
            found, sample = _gcode_has_tool_activation(gcode_path, expected_t)
            if not found:
                blockers.append(
                    f"gcode preamble does not activate {expected_t} "
                    f"(intended_tool={intended_tool}) — slicer config "
                    f"mismatch suspected. Re-slice with the correct tool."
                )
        # else: no gcode_path passed (legacy callers / CLI direct invocation)
        # — skip the check rather than fail spuriously. Operator-facing
        # workflow always passes gcode_path; bare CLI invocations don't.
    if requested_material and intended_tool and host and port is not None:
        ok, out = run_tool_gate(host, int(port), requested_material, intended_tool)
        if not ok:
            # Layer 3 override: the material
            # check is loud-by-default, but the operator can take explicit
            # responsibility via --accept-material-mismatch (audited in
            # main() after the override fires). When the override is set,
            # downgrade the blocker to an OVERRIDE_LINE so the caller can
            # log the warning without refusing the start. The audit row
            # captures expected_tool / expected_material / detected_tool /
            # detected_material in main() because preflight() doesn't have
            # request_id context.
            label = (f"tool/material gate failed for {intended_tool} / "
                     f"{requested_material}: {out[-500:].strip()}")
            if accept_material_mismatch:
                # Tag the line so callers (main) can route it to a warning
                # log + audit instead of refusing the start.
                blockers.append(f"[OVERRIDE:material_mismatch] {label}")
            else:
                blockers.append(label)
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

    Prior version bypassed
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
            # When PIL/Pillow isn't available or the JPEG
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

    Prior class of failure: operator followed the skill verbatim,
    passed the host path returned in readiness_card / uploaded events,
    got HTTP 400 'Unable to open file' from Moonraker because Moonraker's
    file-lookup is by basename in its gcode dir (~/printer_data/gcodes/).

    Strip any directory components — Moonraker only knows files by their
    storage name. Accepts both forms transparently. (`Path.name` alone
    handles the `./x.gcode` edge the old parent-compare missed: Path
    collapses the leading `./`, so its parent looked like a bare name.)"""
    name = Path(filename).name
    return name or filename


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


def _gate_state_path(out_dir) -> Path:
    # v2.2.2 #4: run-scoped marker so overlapping detached invocations for one
    # request never cross-talk. The parent passes a unique id via U1_GATE_RUN_ID
    # and polls this exact path. Falls back to the shared name if unset (direct
    # CLI use).
    import os
    run_id = os.environ.get('U1_GATE_RUN_ID', '')
    name = f'stage2_gate_state_{run_id}.json' if run_id else 'stage2_gate_state.json'
    return Path(out_dir) / name


def _write_gate_state(out_dir, state: str, **extra) -> None:
    """v2.2.1 #2: persist an explicit Stage-2 lifecycle marker (grace_started /
    started / refused) so the DETACHED parent can distinguish 'the grace window
    genuinely opened' from 'child still alive but stalled or heading to a late
    refusal'. Previously the parent inferred grace purely from the child still
    being alive after 25s, which reported a stall as a healthy grace window.
    Best-effort: a marker write never blocks the gate."""
    try:
        import os
        p = _gate_state_path(out_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + '.tmp')
        payload = {'state': state,
                   'gate_run_id': os.environ.get('U1_GATE_RUN_ID', ''), **extra}
        tmp.write_text(json.dumps(payload))
        tmp.replace(p)  # atomic
    except Exception:
        pass


def _consume_stage2_nonce(request_id: str, expected_nonce: str) -> bool:
    """v2.2.1 #3: atomically consume the single-use Stage-2 nonce under a
    per-request file lock. Re-reads the nonce INSIDE the lock and consumes it
    only if it is still present and still equals expected, so two concurrent
    gate processes (double-click / retry / duplicate delivery that got past the
    confirm-token claim, or a direct-gate race) cannot both validate and consume
    the same nonce. Returns True if THIS call consumed it, False if it was
    already consumed/changed (the caller must then refuse the start)."""
    import u1_request
    try:
        import fcntl
        req_dir = Path(u1_request.ensure_request_dir(request_id))
        with open(req_dir / '.stage2_nonce.lock', 'w') as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            fresh_safety = dict((u1_request.read_request(request_id) or {}).get('safety') or {})
            if fresh_safety.get('stage2_approval_nonce') != expected_nonce:
                return False  # already consumed / changed by a concurrent start
            for k in ('stage2_approval_nonce', 'stage2_approval_issued_at',
                      'stage2_approval_binds'):
                fresh_safety.pop(k, None)
            u1_request.write_request(request_id, safety=fresh_safety)
            return True
    except Exception as exc:
        # Fail CLOSED (v2.2.2): if we cannot prove the nonce was valid AND
        # durably consumed under the lock, REFUSE the start. Authorizing on a
        # lock/read/write error (or a missing fcntl) is exactly backwards for a
        # safety gate — the old best-effort fallback turned any such failure into
        # an authorization. The target runtime is Linux, where fcntl is always
        # present, so no permissive path is needed.
        try:
            _audit_gate(request_id, 'stage2_nonce_consume_failed', 'gate',
                        error=f"{type(exc).__name__}: {exc}"[:200])
        except Exception:
            pass
        return False


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


# Retry knobs for gcode_exists_on_printer (tests set these to run fast).
_GCODE_EXISTS_ATTEMPTS = 3
_GCODE_EXISTS_TIMEOUT = 8.0
_GCODE_EXISTS_BACKOFF = 1.0


def gcode_exists_on_printer(host, port, filename):
    """Does ``filename`` actually exist in the printer's gcodes storage?

    Returns True (confirmed present), False (confirmed ABSENT — Moonraker 404),
    or None (couldn't verify: network/other error → the caller fails OPEN so a
    flaky metadata query never blocks a real print).

    Why the gate needs this: the grace window fires an operator "print
    starting" notification BEFORE the actual start. A confused agent that
    invokes the start command with a placeholder/nonexistent filename (live
    2026-07-02: gpt-5.5 looping on `x.gcode` / `wall_mount.gcode` from the
    skill examples) would otherwise spam real print-start alarms for prints
    that can never happen. Confirming the file exists first turns those into a
    fast, silent refusal — no notification.
    """
    import urllib.error
    import time as _t
    sleep = _t.sleep
    url = (f'http://{host}:{port}/server/files/metadata'
           f'?filename={urllib.parse.quote(str(filename))}')
    # Operator decision 2026-07-08: retry a few times, then FAIL CLOSED. A
    # single transient blip (one 500 / one timeout) is absorbed by the
    # retries — not "flaky enough" to block a legitimate print. A printer
    # STILL unreachable after them is "broken", not "flaky", and this close
    # to a physical start, unverifiable means no. A confirmed-absent file
    # (404) is definitive and refuses immediately without retrying.
    attempts = max(1, int(_GCODE_EXISTS_ATTEMPTS))
    for _i in range(attempts):
        try:
            http_json(url, timeout=_GCODE_EXISTS_TIMEOUT)
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False          # definitive — no retry
        except Exception:
            pass                      # unreachable / timeout — retry
        if _i < attempts - 1:
            sleep(_GCODE_EXISTS_BACKOFF)
    return None                       # unverifiable after every attempt


def _resolve_operator_for_gate(cli_operator: str | None) -> str:
    """Phase 3b: resolve operator identity for audit + approval rows.

    Same priority as the workflow's `_resolve_operator`: --operator CLI
    flag > U1_OPERATOR env > 'unknown:gate' fallback. Kept distinct so
    audit rows clearly attribute "where did this row come from."

    Force dotenv load here too — the
    gate may run from any cwd, and u1_config's lazy loader hasn't fired
    yet at this point in the call chain.
    """
    import os
    if cli_operator:
        return str(cli_operator).strip()
    try:
        import u1_config
        u1_config._load_dotenv_if_present()
    except Exception:
        pass
    env = os.environ.get('U1_OPERATOR', '').strip()
    if env:
        return env
    return 'unknown:gate'


def _audit_gate(request_id: str | None, event: str, operator: str, **details) -> None:
    """Audit-emit wrapper that never raises out of the gate. Same defensive
    pattern as the workflow's _audit — observability never breaks the moat."""
    if not request_id:
        return
    try:
        import u1_audit
        u1_audit.append(request_id, event, operator=operator, **details)
    except Exception:
        pass


def _mark_bed_clear_photo_captured(request_id: str | None) -> None:
    """Phase 3b: when Stage 1 captures a usable real bed photo, stamp
    safety.bed_clear_photo_captured=True on request.json so can_start() can
    see that the bed-clear check has been satisfied. Best-effort."""
    if not request_id:
        return
    try:
        import u1_request
        req = u1_request.read_request(request_id) or {}
        safety = dict(req.get('safety') or {})
        safety['bed_clear_photo_captured'] = True
        u1_request.write_request(request_id, safety=safety)
    except Exception:
        pass


def _resolve_grace_seconds(cli_override: int | None) -> int:
    """Grace-period source order: CLI arg → env var → default 120.

    Values are ints (seconds). 0 disables the grace period entirely
    (opt-out for power users standing at the printer). Anything <0 is
    clamped to 0. Env var must parse as int; junk values fall back to
    the default so a bad env can't accidentally disable safety."""
    import os as _os
    if cli_override is not None:
        return max(0, int(cli_override))
    env_val = _os.environ.get('U1_GRACE_PERIOD_SECONDS', '').strip()
    if env_val:
        try:
            return max(0, int(env_val))
        except ValueError:
            pass
    return 120  # default ON — see feedback_dev_branch_until_tested


def _resolve_grace_notify_cmd(cli_override: str | None) -> str | None:
    """Notify-command source: CLI arg → env → None (no notification).

    When set, the gate runs this shell command as the grace window
    opens, with the following env vars exported so the command can
    templatize them:
      * U1_REQUEST_ID
      * U1_FILENAME
      * U1_GRACE_SECONDS
      * U1_CANCEL_MARKER
      * U1_OPERATOR
    Example — send to Telegram via Hermes (no LLM, no agent loop):
      U1_GRACE_NOTIFY_CMD='hermes send --to telegram "⚠️ Print starting in $U1_GRACE_SECONDS s: $U1_FILENAME. SSH: touch $U1_CANCEL_MARKER to abort. Request: $U1_REQUEST_ID"'
    """
    import os as _os
    if cli_override:
        return cli_override
    env_val = _os.environ.get('U1_GRACE_NOTIFY_CMD', '').strip()
    return env_val or None


def _run_grace_notify(notify_cmd: str, *, request_id: str | None,
                      filename: str, grace_seconds: int,
                      cancel_marker: Path, operator: str) -> dict[str, Any]:
    """Fire the operator notification synchronously with a short timeout.

    Runs the caller-provided shell command with U1_* env vars exported.
    Returns a dict {ok, exit_code, stderr_tail}. Notification failure
    is audited but does NOT block the wait — the grace window still
    runs and the operator can still SSH-touch the marker to cancel.
    The philosophy: we'd rather notify-fail-open-and-wait than block
    every print on notification infrastructure. Operator sets a
    monitor separately if they want strict fail-closed."""
    import os as _os
    import subprocess as _sp
    env = _os.environ.copy()
    env['U1_REQUEST_ID'] = request_id or ''
    env['U1_FILENAME'] = filename
    env['U1_GRACE_SECONDS'] = str(grace_seconds)
    env['U1_CANCEL_MARKER'] = str(cancel_marker)
    env['U1_OPERATOR'] = operator
    try:
        proc = _sp.run(notify_cmd, shell=True, env=env,
                       capture_output=True, text=True, timeout=20)
        result = {
            'ok': proc.returncode == 0,
            'exit_code': proc.returncode,
            'stderr_tail': (proc.stderr or '')[-500:],
        }
    except _sp.TimeoutExpired:
        result = {'ok': False, 'exit_code': -1,
                  'stderr_tail': 'notify command timed out (>20s)'}
    except Exception as exc:
        result = {'ok': False, 'exit_code': -1,
                  'stderr_tail': f'{type(exc).__name__}: {exc}'}
    _audit_gate(request_id,
                'pre_start_grace_notify_sent' if result['ok']
                else 'pre_start_grace_notify_failed',
                operator,
                exit_code=result['exit_code'],
                stderr_tail=result['stderr_tail'][:200])
    return result


GRACE_NOTIFY_CAP = 4


def _grace_notify_allowed(request_id: str | None, cap: int = GRACE_NOTIFY_CAP) -> bool:
    """Loop guard: cap how many "print starting" notifications one request may
    send. A normal flow notifies once per start; a couple of legitimate
    cancel-then-retry cycles stay well under the cap. A confused agent looping
    the start command on a REAL uploaded file (which Fence 2's existence check
    can't catch) trips it. The grace WAIT still runs when suppressed — only the
    DM is muted — so the cancel safety net is untouched. No request_id (nothing
    to track) → always allowed.
    """
    if not request_id:
        return True
    try:
        import u1_request
        req = u1_request.read_request(request_id) or {}
        safety = dict(req.get('safety') or {})
        count = int(safety.get('grace_notify_count') or 0) + 1
        safety['grace_notify_count'] = count
        u1_request.write_request(request_id, safety=safety)
        return count <= cap
    except Exception:
        return True  # never let bookkeeping block a legitimate notification


def _wait_pre_start_grace_period(cancel_marker: Path, grace_seconds: int,
                                 request_id: str | None,
                                 resolved_operator: str,
                                 filename: str = '',
                                 notify_cmd: str | None = None,
                                 sleep_fn=None,
                                 notify_fn=None) -> bool:
    """Wait up to grace_seconds for cancel_marker to appear on disk.

    Returns a status string: ``'proceed'`` (start), ``'cancelled'`` (the
    operator cancelled during the window), or ``'notify_failed'`` (a
    configured countdown/CANCEL message could not be delivered, so the start
    is aborted — Q2, operator decision 2026-07-08). Audit rows on both start
    and end. ``sleep_fn`` is injected for tests so they don't actually sleep.
    ``notify_fn`` is
    injected for tests so we don't shell out."""
    import time as _time
    _sleep = sleep_fn or _time.sleep
    import os
    _notify = notify_fn or _run_grace_notify
    # Fresh window: any leftover marker from a prior request must be
    # cleared before we start listening.
    if cancel_marker.exists():
        try:
            cancel_marker.unlink()
        except OSError:
            pass
    _audit_gate(request_id, 'pre_start_grace_period_started',
                resolved_operator,
                grace_seconds=grace_seconds,
                cancel_marker=str(cancel_marker))
    # Fire the operator notification BEFORE we start waiting so they
    # have the full grace window to react. Notification failure is
    # audited but does not block the wait (fail-open at notify layer;
    # the wait itself is the safety net).
    #
    # Timing note (reviewed, kept as-is): the notify command can block up
    # to 20s, so the window closes up to 20s later than the started audit
    # row suggests. That drift is in the OPERATOR'S favor — the DM lands
    # at the end of the send, and the countdown it advertises starts
    # then, which is exactly when the poll loop starts. Do not "fix" this
    # by subtracting notify latency; that would shorten the window below
    # what the DM promised.
    if notify_cmd:
        if _grace_notify_allowed(request_id):
            _nres = _notify(notify_cmd,
                            request_id=request_id,
                            filename=filename,
                            grace_seconds=grace_seconds,
                            cancel_marker=cancel_marker,
                            operator=resolved_operator)
            # Q2 (operator decision 2026-07-08): if a countdown/CANCEL DM was
            # configured but could NOT be delivered, do not start a print the
            # operator can neither see nor cancel — abort. Local-console runs
            # (no notify_cmd) never reach here. Escape hatch:
            # U1_GRACE_NOTIFY_OPTIONAL=1 restores the old fail-open behavior.
            if (isinstance(_nres, dict) and not _nres.get("ok")
                    and os.environ.get("U1_GRACE_NOTIFY_OPTIONAL", "") not in
                    ("1", "true", "yes", "on")):
                _audit_gate(request_id, 'pre_start_grace_notify_failed_abort',
                            resolved_operator,
                            stderr_tail=str(_nres.get("stderr_tail"))[:160])
                try:
                    (Path(f'/tmp/u1_pending_cancel/{request_id}.json')).unlink()
                except Exception:
                    pass
                return 'notify_failed'
        else:
            # Loop guard tripped: suppress the DM but STILL run the wait so the
            # cancel safety net is intact. Audited so a real loop is visible.
            _audit_gate(request_id, 'pre_start_grace_notify_suppressed_loop_guard',
                        resolved_operator, cap=GRACE_NOTIFY_CAP)
    # Contract with the Hermes gateway hook + notify script: the notify
    # script wrote /tmp/u1_pending_cancel/<request_id>.json so a
    # `cancel <code>` reply in Telegram routes to the right marker.
    # Clean up our OWN entry on ANY exit path (cancel OR expire) so a
    # stale entry doesn't cause the next unrelated print to abort on
    # some later `cancel <code>` — and so multiple concurrent grace
    # windows don't step on each other.
    def _clear_pending_state() -> None:
        try:
            pending = Path(f'/tmp/u1_pending_cancel/{request_id}.json')
            if pending.exists():
                pending.unlink()
        except OSError:
            pass

    def _cancelled() -> str:
        _audit_gate(request_id, 'pre_start_grace_cancelled',
                    resolved_operator,
                    cancel_marker=str(cancel_marker),
                    cancelled_after_wait_s=None)
        _clear_pending_state()
        # Model-free outcome DM: the operator hears "cancelled" from the
        # machinery, not from an agent narrating an outcome it never saw
        # (live 2026-07-07: the agent claimed a cancel that never happened).
        try:
            import u1_notify
            u1_notify.send_operator(
                f"\U0001f6d1 Cancelled — nothing was sent to the printer. "
                f"({filename})")
        except Exception:
            pass
        return 'cancelled'

    for _ in range(int(grace_seconds)):
        if cancel_marker.exists():
            return _cancelled()
        _sleep(1)
    # Final re-check AFTER the last sleep tick: the notification counts the
    # window down, so an operator racing the deadline lands their CANCEL
    # exactly here. A marker written during the final second must still
    # win — they were promised the full window.
    if cancel_marker.exists():
        return _cancelled()
    _audit_gate(request_id, 'pre_start_grace_period_expired',
                resolved_operator, grace_seconds=grace_seconds,
                proceeded_to_start=True)
    _clear_pending_state()
    return 'proceed'


def run_gate(filename: str,
             bed_clear: str = 'cancel',
             host=None,
             port=None,
             intended_tool=None,
             requested_material: str | None = None,
             approval_token: str | None = None,
             stage2_approval_nonce: str | None = None,
             out_dir: Path | None = None,
             start_func=start_print,
             request_id: str | None = None,
             operator: str | None = None,
             accept_material_mismatch: bool = False,
             operator_text: str | None = None,
             verification_method: str | None = None,
             grace_seconds: int | None = None,
             grace_sleep_fn=None,
             grace_notify_cmd: str | None = None,
             grace_notify_fn=None,
             gcode_exists_fn=None):
    host = host or get_u1_host()
    port = port or get_u1_port()
    # Per-request token + photo storage: if we have a
    # request_id, prefer its request_dir so the bed_snapshot.jpg + approval
    # token live inside the per-request folder. Prevents cross-request
    # token leakage: a class of bug where an agent dispatches Stage 2 for
    # a new request and the gate picked up a stale GLOBAL token from a
    # prior unrelated session (88 min old, refused). With per-request
    # storage, a new request without a Stage 1 capture has no token to
    # find, so the gate refuses with a clearer signal.
    if request_id and out_dir is None:
        try:
            import u1_request
            out_dir = u1_request.ensure_request_dir(request_id)
        except Exception:
            out_dir = get_data_dir()
    out_dir = (out_dir or get_data_dir()).resolve()
    printer_filename = _normalize_filename(filename)
    resolved_operator = _resolve_operator_for_gate(operator)

    # Fence 1 — test-operator refusal. If the resolved operator carries an
    # unambiguously test-flavored prefix, refuse Stage 2 BEFORE any
    # Moonraker call. Closes the "smoke test accidentally runs a real
    # print" failure class: tester runs the workflow
    # with --operator smoke:xxx, extracts the emitted Stage 2 command,
    # runs it, and the gate happily sends /printer/print/start to the
    # real printer.
    #
    # Prefix choice: only prefixes that are IMPLAUSIBLE as production
    # identity strings. `dev:` and `ci:` were considered but left out —
    # a fork developer running real prints from a dev environment, or a
    # legitimate CI/CD pipeline orchestrating real prints, would use
    # those. The list here is the "no ambiguity" tier: nobody names their
    # production operator `smoke:*` or `mock:*`.
    _TEST_OPERATOR_PREFIXES = ("smoke:", "test:", "dry:", "mock:", "fixture:")
    _op_lc = (resolved_operator or "").lower()
    if any(_op_lc.startswith(p) for p in _TEST_OPERATOR_PREFIXES):
        _audit_gate(request_id, 'gate_refused_test_operator',
                    resolved_operator, prefix_match=next(
                        p for p in _TEST_OPERATOR_PREFIXES
                        if _op_lc.startswith(p)))
        return {
            'stage': 'gate_refused_test_operator',
            'ok': False, 'started': False,
            'operator': resolved_operator,
            'reason': (
                f"gate refuses --operator={resolved_operator!r}: prefix is "
                "in the test-flavored refusal set "
                f"({', '.join(_TEST_OPERATOR_PREFIXES)}). Live printer "
                "traffic is not allowed from a test-flavored operator. "
                "If this is a real print, use a non-test operator value "
                "(bare name, `human:*`, `dev:*`, `ci:*`, or your platform "
                "adapter's identity all pass)."
            ),
        }

    if _op_lc == "unknown:gate":
        # Deliberate decision: an UNSET operator identity passes the fence --
        # refusing every bare CLI run would tax legitimate local use. But it
        # also means a smoke test that simply forgot to set an operator is
        # not fenced, so leave a loud audit trail for that exact case.
        _audit_gate(request_id, 'gate_operator_unknown', resolved_operator,
                    note=('operator identity was not set (no --operator, no '
                          'U1_OPERATOR env) -- proceeding, but set an operator '
                          'so audit rows carry a real identity'))

    status = query_state(host, port)
    # Construct the local gcode path for the new preamble-activation check.
    # The slice workflow writes plates to <out_dir>/slice/<printer_filename>.
    # If the file doesn't exist (legacy layout / direct CLI test),
    # _gcode_has_tool_activation returns (False, None) — but preflight only
    # blocks on missing activation when intended_tool is set AND gcode_path
    # is present. The "fail-open if no path" branch keeps CLI runs working.
    _gcode_path: Path | None = None
    try:
        _candidate = out_dir / 'slice' / printer_filename
        if _candidate.is_file():
            _gcode_path = _candidate
    except Exception:
        _gcode_path = None
    blockers = preflight(status, intended_tool,
                         requested_material=requested_material,
                         host=host, port=port,
                         gcode_path=_gcode_path,
                         accept_material_mismatch=accept_material_mismatch)
    # Layer 3 override: when --accept-material-mismatch is set, preflight tags
    # the material-mismatch line with [OVERRIDE:...]. The override is honored
    # ONLY when a real operator authorizing phrase is supplied (v2.2.1 #1). The
    # provenance is NEVER defaulted: without operator_text the override does not
    # apply and the material blocker stands (a rogue agent cannot mint
    # authorization it did not receive). main() additionally refuses the flag
    # unless invoked from an interactive terminal, so an agent-mediated /
    # subprocess start can never reach this branch with the override honored.
    # The hardware safety isn't bypassed even then — just the mismatch refusal.
    overrides_used: list[dict[str, Any]] = []
    # v2.2.2: enforce the interactive-terminal requirement HERE, where the
    # override is actually applied, not only in main(). A direct caller of
    # run_gate() (bypassing the CLI) therefore cannot honor the override without
    # an interactive TTY either. (A pty can still spoof isatty; that is treated
    # as UX friction, not authentication — a single-operator homelab tool does
    # not add a sudo/second-user ceremony for it.)
    _tty_ok = bool(sys.stdin is not None and sys.stdin.isatty())
    _override_ok = bool(accept_material_mismatch and operator_text and _tty_ok)
    if _override_ok:
        kept: list[str] = []
        for line in blockers:
            if line.startswith("[OVERRIDE:material_mismatch] "):
                overrides_used.append({
                    "kind": "material_mismatch",
                    "blocker_text": line[len("[OVERRIDE:material_mismatch] "):],
                })
            else:
                kept.append(line)
        blockers = kept
        if overrides_used and request_id:
            _audit_gate(request_id, "operator_override", resolved_operator,
                        override_kind="material_mismatch",
                        reason="loaded_material_does_not_match_requested",
                        verification_method=verification_method,
                        operator_text=operator_text,
                        expected_tool=intended_tool,
                        expected_material=requested_material,
                        blocker_text=overrides_used[0].get("blocker_text"))
    elif accept_material_mismatch and not _override_ok:
        # Override requested but not authorized (missing operator text, or not
        # from an interactive terminal): refuse. Un-tag the [OVERRIDE:...] marker
        # back to a clean hard blocker so the refusal reads normally, and audit
        # the rejected attempt for forensics.
        blockers = [
            (l[len("[OVERRIDE:material_mismatch] "):]
             if l.startswith("[OVERRIDE:material_mismatch] ") else l)
            for l in blockers
        ]
        if request_id:
            _audit_gate(request_id, "operator_override_rejected", resolved_operator,
                        override_kind="material_mismatch",
                        reason=("override_missing_operator_text" if not operator_text
                                else "override_requires_interactive_terminal"),
                        expected_tool=intended_tool,
                        expected_material=requested_material)

    if bed_clear != 'start':
        # Stage 1 — capture real photo, write token, return readiness.
        snapshot = capture_real_bed_photo(out_dir, host, port)
        token: str | None = None
        if snapshot['ok']:
            token = _write_approval_token(out_dir, snapshot)
            # Phase 3b: mark the safety check satisfied. can_start() reads
            # this when Stage 2 fires later.
            _mark_bed_clear_photo_captured(request_id)
            _audit_gate(request_id, 'stage1_photo_captured', resolved_operator,
                        snapshot_path=snapshot.get('path'),
                        approval_token=token)
        else:
            _audit_gate(request_id, 'stage1_photo_failed', resolved_operator,
                        error=snapshot.get('error'),
                        is_mock=snapshot.get('is_mock'))
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

    # Stage 2 — verify token, verify nonce, re-check preflight, sanity-only
    # re-capture, start.
    stored = _read_approval_token(out_dir)
    token_ok, token_reason = _approval_token_valid(stored, approval_token)
    if not token_ok:
        _audit_gate(request_id, 'stage2_token_invalid', resolved_operator,
                    reason=token_reason)
        return {
            'stage': 'start_attempt',
            'filename': printer_filename,
            'blockers': blockers,
            'snapshot': None,
            'ok': False, 'started': False,
            'reason': f'approval token invalid: {token_reason}',
        }

    # single-use stage2 approval nonce.
    # Kit workflow paths REQUIRE this nonce — the workflow only mints one
    # inside _action_start() AFTER the operator answers yes to the fresh
    # bed_clear_start prompt. This closes the direct-Stage-2 attack: an
    # adversarial agent reading approval_token from request.json cannot
    # fire Stage 2 without also going through the yes/no handler that
    # mints the nonce. The nonce also binds to gcode_hash + revision, so
    # a stale nonce from a prior slice can't gate a new one.
    #
    # Non-kit paths (single-STL workflow, direct CLI) don't mint nonces;
    # for backward compat we only enforce when request_id is present AND
    # the request state has any Stage 2 nonce persisted. Absent state +
    # absent flag = legacy path, allowed (with audit).
    # Initialized outside the if-request_id block so the
    # manual-verification check downstream can reference them safely
    # even when request_id is None (single-STL direct-CLI path).
    req_for_nonce: dict[str, Any] = {}
    safety_for_nonce: dict[str, Any] = {}
    expected_nonce = None
    binds: dict[str, Any] = {}
    if request_id:
        try:
            import u1_request
            req_for_nonce = u1_request.read_request(request_id) or {}
            safety_for_nonce = req_for_nonce.get('safety') or {}
            expected_nonce = safety_for_nonce.get('stage2_approval_nonce')
            binds = safety_for_nonce.get('stage2_approval_binds') or {}
        except Exception:
            expected_nonce = None
            binds = {}
            req_for_nonce = {}
            safety_for_nonce = {}
        # Kit-path nonce requirement (closes the legacy-token bypass a
        # closes the kit legacy-token bypass). Kit requests ALWAYS mint a
        # nonce via _action_start() after the operator's fresh yes at
        # bed_clear_start. An absent nonce on a kit request means the
        # legacy --form-answers one-liner bypassed the two-turn
        # boundary; refuse regardless of token validity. Single-STL
        # requests (no `kit` / `plates` field) keep the legacy
        # token-only path for backward compat.
        is_kit_request = bool(
            req_for_nonce.get('kit') or req_for_nonce.get('plates'))
        if is_kit_request and expected_nonce is None:
            _audit_gate(request_id, 'stage2_kit_missing_nonce',
                        resolved_operator,
                        reason='kit_request_without_staged_confirmation')
            return {
                'stage': 'start_attempt',
                'filename': printer_filename,
                'blockers': blockers,
                'snapshot': None,
                'ok': False, 'started': False,
                'reason': (
                    "Kit request requires the staged bed_clear_start "
                    "confirmation before Stage 2. No Stage 2 nonce is "
                    "persisted for this request, meaning the yes/no "
                    "prompt was never issued or never answered. Re-run "
                    "the kit workflow with --action start (without "
                    "--bed-clear-confirmed) to get the yes/no prompt, "
                    "answer yes, then run the Stage 2 command it emits."
                ),
            }
        if expected_nonce is not None:
            problems: list[str] = []
            if not stage2_approval_nonce:
                problems.append("Stage 2 nonce required but not provided "
                                "(--stage2-approval-nonce missing). Direct "
                                "Stage 2 invocation attempted?")
            elif stage2_approval_nonce != expected_nonce:
                problems.append("Stage 2 nonce mismatch — either stale or "
                                "forged. Refusing start.")
            # Bind checks — nonce is only valid for the exact plan it was
            # issued against.
            current_revision = req_for_nonce.get('request_revision')
            current_gcode_hash = req_for_nonce.get('gcode_hash')
            plates_l = req_for_nonce.get('plates') or []
            plate1_hash = plates_l[0].get('gcode_hash') if plates_l else None
            # Prefer plate1 hash if present (kit path), else top-level.
            effective_hash = plate1_hash or current_gcode_hash
            if binds.get('request_revision') is not None:
                if binds.get('request_revision') != current_revision:
                    problems.append(
                        f"nonce binds to revision {binds.get('request_revision')} "
                        f"but current is {current_revision}. Plan changed after "
                        "operator approval.")
            if binds.get('gcode_hash') is not None:
                if binds.get('gcode_hash') != effective_hash:
                    problems.append(
                        "nonce binds to a different gcode_hash — the slice "
                        "changed after the operator's yes.")
            if problems:
                _audit_gate(request_id, 'stage2_nonce_rejected',
                            resolved_operator, reasons=problems)
                return {
                    'stage': 'start_attempt',
                    'filename': printer_filename,
                    'blockers': blockers,
                    'snapshot': None,
                    'ok': False, 'started': False,
                    'reason': ("Stage 2 nonce rejected: "
                               + "; ".join(problems)),
                }
            # Consume the nonce (single-use, ATOMIC under a per-request lock).
            # Wipes binds too so a replay can't re-use them. If a concurrent
            # start already consumed it, refuse — exactly one start proceeds.
            if not _consume_stage2_nonce(request_id, expected_nonce):
                _audit_gate(request_id, 'stage2_nonce_double_consume_refused',
                            resolved_operator)
                return {
                    'stage': 'start_attempt',
                    'filename': printer_filename,
                    'blockers': blockers,
                    'snapshot': None,
                    'ok': False, 'started': False,
                    'reason': ("Stage 2 nonce was already consumed by a "
                               "concurrent start (double-click / retry). Exactly "
                               "one start proceeds; this one is refused."),
                }
            _audit_gate(request_id, 'stage2_nonce_verified',
                        resolved_operator,
                        nonce_prefix=stage2_approval_nonce[:8] + '...')
        elif stage2_approval_nonce:
            # Nonce was passed but request has no expected nonce — either
            # legacy path (fine) or replay of already-consumed nonce
            # (suspicious). Audit but allow so legacy tests / non-kit
            # paths still work.
            _audit_gate(request_id, 'stage2_nonce_unexpected',
                        resolved_operator,
                        note=('nonce passed but request state has no expected '
                              'nonce — may be replay of consumed nonce; '
                              'audited but allowed for legacy compat'))
    if blockers:
        _audit_gate(request_id, 'stage2_preflight_blocked', resolved_operator,
                    blockers=blockers)
        return {
            'stage': 'start_attempt',
            'filename': printer_filename,
            'blockers': blockers,
            'snapshot': None,
            'ok': False, 'started': False,
            'reason': 'preflight blockers present at Stage 2',
        }

    # Phase 3b: the moat. Before any printer-affecting action, verify the
    # request's plan hasn't drifted since the operator reviewed the
    # readiness card. can_start() reads the audit log + request.json.
    # If no request_id was passed, we cannot apply the moat — the gate
    # refuses rather than starting unguarded.
    if not request_id:
        _audit_gate(None, 'start_safety_check_failed', resolved_operator,
                    reason='no --request-id passed to Stage 2')
        return {
            'stage': 'start_attempt',
            'filename': printer_filename,
            'blockers': blockers,
            'snapshot': None,
            'ok': False, 'started': False,
            'reason': ('Stage 2 requires --request-id to verify plan stability. '
                       'Re-run with --request-id <id>.'),
        }
    try:
        import u1_request
        import u1_safety
        req = u1_request.read_request(request_id)
    except Exception as exc:
        _audit_gate(request_id, 'start_safety_check_failed', resolved_operator,
                    reason=f'request.json unreadable: {exc}')
        return {
            'stage': 'start_attempt',
            'filename': printer_filename,
            'blockers': blockers,
            'snapshot': None,
            'ok': False, 'started': False,
            'reason': f'safety check: request.json unreadable for {request_id}',
        }
    allowed, reason = u1_safety.can_start(req)
    if not allowed:
        _audit_gate(request_id, 'start_safety_check_failed', resolved_operator,
                    reason=reason,
                    current_revision=(req or {}).get('request_revision'),
                    current_gcode_hash=(req or {}).get('gcode_hash'))
        return {
            'stage': 'start_attempt',
            'filename': printer_filename,
            'blockers': blockers,
            'snapshot': None,
            'ok': False, 'started': False,
            'reason': f'safety check failed: {reason}',
        }
    _audit_gate(request_id, 'start_safety_check_passed', resolved_operator,
                request_revision=(req or {}).get('request_revision'),
                gcode_hash=(req or {}).get('gcode_hash'))
    # Manual-verification path (Layer 3 override): if
    # the operator's fresh yes at bed_clear_start was backed by a manual
    # verification method (physical look at the bed / Snapmaker app /
    # other camera), the mandatory Stage 2 sanity capture is REDUNDANT.
    # The real safety gate is the human yes; the sanity capture was
    # belt-and-suspenders scaffolding for the camera path.
    #
    # For that skip to be safe, ALL of these must hold:
    #   * safety.manual_verification is True
    #   * a fresh Stage 2 nonce was minted for this request (the fresh
    #     yes actually happened — an old override_confirmed_at from a
    #     prior slice must not carry forward)
    #   * revision + gcode_hash binds match (already checked above; the
    #     safety-check-passed audit implies they do)
    #   * operator_text + verification_method are recorded
    # Loud audit row so the forensic timeline distinguishes this from a
    # normal Stage 2 sanity pass.
    manual_ver = (safety_for_nonce or {}).get('manual_verification') is True
    manual_ver_ok = (
        manual_ver
        and expected_nonce is not None
        and (safety_for_nonce or {}).get('operator_text')
        and (safety_for_nonce or {}).get('verification_method')
    )
    if manual_ver_ok:
        _audit_gate(request_id,
                    'stage2_sanity_capture_skipped_manual_verification',
                    resolved_operator,
                    verification_method=safety_for_nonce.get(
                        'verification_method'),
                    operator_text=safety_for_nonce.get('operator_text'),
                    override_confirmed_at=safety_for_nonce.get(
                        'override_confirmed_at'),
                    request_revision=(req or {}).get('request_revision'),
                    gcode_hash=(req or {}).get('gcode_hash'),
                    stage2_nonce_prefix=(expected_nonce or '')[:8])
        sanity_snapshot = {
            'ok': True,
            'skipped': True,
            'reason': 'manual verification path — sanity capture skipped',
        }
        sanity_blocks_start = False
    else:
        # Sanity-only fresh capture so we don't fire blind into a state
        # that changed during the operator's review. We do NOT show this
        # photo — the operator already approved Stage 1's. We just
        # refuse if this one is mock/dark too.
        sanity_snapshot = capture_real_bed_photo(out_dir, host, port)
        # Distinguish mock (camera never reached) and
        # measured-dark from deferred-brightness-check (PIL unavailable
        # but photo IS real). Only the first two should block — deferred
        # allows the start because the operator already approved Stage
        # 1's image.
        sanity_blocks_start = (
            sanity_snapshot.get('is_mock')
            or (sanity_snapshot.get('brightness_check') == 'measured'
                and not sanity_snapshot.get('ok'))
        )
    if sanity_blocks_start:
        # Audit the sanity-capture refusal
        # so the forensic timeline reflects WHY the start was blocked
        # between start_safety_check_passed and 'nothing happened'.
        _audit_gate(request_id, 'stage2_sanity_capture_failed', resolved_operator,
                    error=sanity_snapshot.get('error'),
                    is_mock=sanity_snapshot.get('is_mock'),
                    brightness_check=sanity_snapshot.get('brightness_check'))
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
    # Human safety net: last-chance cancel window before we HTTP the
    # printer. Adapters (Hermes / Telegram) tail the audit log; when
    # they see pre_start_grace_period_started, they send an operator
    # notification with a cancel UI that touches cancel_marker_file
    # within the grace window. Default 120s (opt-out via
    # U1_GRACE_PERIOD_SECONDS=0 or --grace-seconds 0). Motivated by
    # Without this the operator learns about an
    # unauthorized start only when the first-layer camera cron fires.
    _resolved_grace = _resolve_grace_seconds(grace_seconds)
    _resolved_notify = _resolve_grace_notify_cmd(grace_notify_cmd)
    cancel_marker = out_dir / 'pre_start_cancel.marker'

    # Fence 2 — the file must actually exist on the printer BEFORE the grace
    # window fires its "print starting" notification. Closes the false-alarm
    # class where a confused agent loops the start command with a placeholder/
    # nonexistent filename and spams real print-start alerts for prints that
    # can never happen (live 2026-07-02: gpt-5.5 on x.gcode / wall_mount.gcode).
    # Fails OPEN: only a CONFIRMED-absent file (Moonraker 404) refuses; a flaky
    # metadata query (None) proceeds so a transient error never blocks a real
    # print — the start itself still fails safely if the file is truly gone.
    _gcode_exists = gcode_exists_fn or gcode_exists_on_printer
    _exists = _gcode_exists(host, port, printer_filename)
    if _exists is False:
        _audit_gate(request_id, 'gate_refused_file_missing',
                    resolved_operator, printer_storage_filename=printer_filename)
        return {
            'stage': 'gate_refused_file_missing',
            'filename': printer_filename,
            'ok': False, 'started': False,
            'reason': (
                f"gate refuses: {printer_filename!r} is not in the printer's "
                "gcode storage, so there is nothing to start. No notification "
                "was sent. If you meant a real plate, re-run the workflow so "
                "the sliced file is uploaded first."),
        }
    if _exists is None:
        # Fail CLOSED (operator decision 2026-07-08): the printer could not
        # confirm the file exists after several tries — unreachable or
        # erroring. This close to a physical start, do not fire blind.
        _audit_gate(request_id, 'gate_refused_file_unverifiable',
                    resolved_operator, printer_storage_filename=printer_filename)
        return {
            'stage': 'gate_refused_file_unverifiable',
            'filename': printer_filename,
            'ok': False, 'started': False,
            'reason': (
                f"gate refuses: could not confirm {printer_filename!r} is on "
                "the printer after several tries — the printer is unreachable "
                "or erroring. Nothing was started and no notification was "
                "sent. Check the printer is online, then re-run."),
        }

    def _grace_cancel_refusal() -> dict[str, Any]:
        # A grace-cancel consumed the Stage 2 nonce, but the slice, the
        # upload, and the plate on the printer are all still valid. The
        # cheap, legitimate path back is a fresh Stage 1 (new photo, new
        # yes) — NOT re-running the whole workflow. Hand that path to the
        # operator instead of a dead-end refusal.
        recovery: dict[str, Any] = {
            'instruction': (
                'Cancelled before any printer traffic. The uploaded plate '
                'is still valid — to print it after all, re-run Stage 1 '
                '(fresh bed photo + fresh yes); no re-slice or re-upload '
                'is needed.'),
        }
        if intended_tool and requested_material and request_id:
            recovery['stage1_command'] = build_stage1_command(
                printer_filename=printer_filename,
                intended_tool=str(intended_tool),
                material=str(requested_material),
                request_id=request_id)
        return {
            'stage': 'start_attempt',
            'filename': printer_filename,
            'blockers': blockers,
            'snapshot': sanity_snapshot,
            'ok': False, 'started': False,
            'reason': (
                'Operator cancelled during the pre-start grace '
                'period. No HTTP call was sent to the printer.'),
            'cancel_marker': str(cancel_marker),
            'recovery': recovery,
        }

    if _resolved_grace > 0:
        # v2.2.1 #2: mark grace as genuinely started BEFORE the blocking wait,
        # so the detached parent polling this marker knows the ~120s window
        # actually opened (vs. the child stalling in a pre-grace check).
        _write_gate_state(out_dir, 'grace_started', request_id=request_id,
                          grace_seconds=_resolved_grace)
        _grace_status = _wait_pre_start_grace_period(
                cancel_marker, _resolved_grace, request_id,
                resolved_operator,
                filename=printer_filename,
                notify_cmd=_resolved_notify,
                sleep_fn=grace_sleep_fn,
                notify_fn=grace_notify_fn)
        if _grace_status == 'notify_failed':
            return {
                'stage': 'gate_refused_notify_undeliverable',
                'filename': printer_filename,
                'ok': False, 'started': False,
                'reason': (
                    "gate refuses: the pre-start countdown/CANCEL message "
                    "could not be delivered, so the print was NOT started — "
                    "you would have had no way to see it or cancel it. Fix "
                    "the notifier (or check Telegram) and re-run. Set "
                    "U1_GRACE_NOTIFY_OPTIONAL=1 to start anyway on notify "
                    "failure."),
            }
        if _grace_status != 'proceed':
            return _grace_cancel_refusal()
        # Belt: one more marker check between the window closing and the
        # HTTP call — audit/log latency in the wait's exit path is real
        # time an operator's last-instant CANCEL could land in.
        if cancel_marker.exists():
            _audit_gate(request_id, 'pre_start_grace_cancelled',
                        resolved_operator,
                        cancel_marker=str(cancel_marker),
                        cancelled_after_wait_s='post-window')
            return _grace_cancel_refusal()
    resp = start_func(host, port, printer_filename)
    # Phase 3b: record the start approval as granted now that we've actually
    # commanded the printer. record_approval binds the approval to the
    # current revision + gcode_hash; can_start() on a future invocation
    # would see this and verify drift against it. Also audit the start.
    try:
        import u1_request
        u1_request.record_approval(request_id, kind='start',
                                   operator=resolved_operator,
                                   gcode_hash=(req or {}).get('gcode_hash'))
    except Exception:
        pass
    _audit_gate(request_id, 'print_started', resolved_operator,
                printer_storage_filename=printer_filename,
                request_revision=(req or {}).get('request_revision'),
                gcode_hash=(req or {}).get('gcode_hash'))
    _write_gate_state(out_dir, 'started', request_id=request_id)
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
    ap.add_argument('--stage2-approval-nonce', default=None,
                    help=('Single-use nonce minted by u1_kit_workflow._action_start() '
                          'AFTER the operator answers yes to the fresh bed_clear_start '
                          'prompt. Kit paths REQUIRE this — without it, Stage 2 '
                          'refuses even if the approval-token is valid. Consumed '
                          'on successful start (single-use). Closes the direct-'
                          'Stage-2 attack.'))
    ap.add_argument('--intended-tool', help='Klipper extruder name, e.g. extruder1 for T1')
    ap.add_argument('--requested-material', help='material to verify on intended_tool, e.g. PETG')
    ap.add_argument('--out-dir', type=Path, default=None,
                    help='Where to write the bed snapshot + approval token. Defaults to U1 data dir.')
    ap.add_argument('--request-id', type=str, default=None,
                    help='v2.0 Phase 3b: the Print Request Object ID this Stage is acting on. '
                         'Stage 2 REQUIRES this — without it, can_start() has no request to verify '
                         'and the gate refuses. Stage 1 uses it to stamp safety.bed_clear_photo_captured '
                         'on the matching request.json. SKILL.md fills it in from the readiness card.')
    ap.add_argument('--operator', type=str, default=None,
                    help='v2.0 Phase 3a: operator identity for audit + approval rows '
                         '(e.g. "telegram:brent"). Falls back to env U1_OPERATOR, '
                         'then "unknown:gate".')
    # Layer 3 override flags. The material-mismatch blocker is loud-by-default.
    # v2.2.1 #1: the override is now MECHANICALLY constrained, not merely
    # forensic. It is refused unless invoked from an interactive terminal (an
    # agent-mediated / workflow-subprocess start has no TTY) AND --operator-text
    # is supplied. Provenance is never defaulted, so an agent cannot mint an
    # override it did not receive. This is a deliberate CLI-only escape hatch
    # for an operator physically at the machine.
    ap.add_argument('--accept-material-mismatch', action='store_true',
                    help=('Layer 3 override (INTERACTIVE TERMINAL ONLY): operator '
                          'physically at the CLI accepts a material mismatch '
                          '(loaded filament does not match requested material). '
                          'REQUIRES --operator-text and a real TTY; refused for '
                          'agent-mediated / subprocess starts. Audited.'))
    ap.add_argument('--operator-text', default=None,
                    help=('Layer 3 override: verbatim operator phrase '
                          'authorizing the override. Audited.'))
    ap.add_argument('--verification-method', default=None,
                    choices=['manual', 'snapmaker_app', 'other_camera',
                             'unspecified_manual'],
                    help='Layer 3 override: how operator verified the override.')
    ap.add_argument('--grace-seconds', type=int, default=None,
                    help=('Human safety net (default 120s): after ALL '
                          'checks pass, the gate waits this many seconds '
                          'before HTTPing the printer. Adapter tails the '
                          'audit log for pre_start_grace_period_started, '
                          'sends the operator a cancel notification, and '
                          'touches <out_dir>/pre_start_cancel.marker on '
                          'cancel. Use 0 to disable (opt-out for power '
                          'users standing at the printer). Overrides env '
                          'U1_GRACE_PERIOD_SECONDS.'))
    ap.add_argument('--grace-notify-cmd', default=None,
                    help=('Shell command to fire when the grace window '
                          'opens, notifying the operator. Env vars '
                          'exported: U1_REQUEST_ID, U1_FILENAME, '
                          'U1_GRACE_SECONDS, U1_CANCEL_MARKER, '
                          'U1_OPERATOR. Notify failure is audited but '
                          'does not block the wait. Example (Hermes): '
                          "'hermes send --to telegram \"Print $U1_FILENAME "
                          "starting in ${U1_GRACE_SECONDS}s. touch "
                          "$U1_CANCEL_MARKER to abort.\"'. Overrides env "
                          'U1_GRACE_NOTIFY_CMD.'))
    a = ap.parse_args(argv)
    # v2.2.1 #1: the material-mismatch override must be a deliberate operator
    # action, never something an agent can forge. Refuse the flag unless it comes
    # from a real interactive terminal (an agent-mediated / workflow-subprocess
    # start has no TTY and cannot fake one) AND the operator's authorizing phrase
    # is actually supplied. Provenance is not defaulted.
    if a.accept_material_mismatch:
        if not (sys.stdin and sys.stdin.isatty()):
            print(json.dumps({
                "phase": "refused",
                "reason": ("material-mismatch override refused: it requires an "
                           "interactive terminal. Agent-mediated or workflow "
                           "starts cannot override a material mismatch. Load the "
                           "correct material, or run this gate directly from a "
                           "terminal.")}, indent=2))
            return 3
        if not a.operator_text:
            print(json.dumps({
                "phase": "refused",
                "reason": ("material-mismatch override refused: --operator-text "
                           "is required (the phrase authorizing this override). "
                           "It is never defaulted.")}, indent=2))
            return 3
    res = run_gate(a.filename, a.bed_clear,
                   intended_tool=a.intended_tool,
                   requested_material=a.requested_material,
                   approval_token=a.approval_token,
                   stage2_approval_nonce=a.stage2_approval_nonce,
                   out_dir=a.out_dir,
                   request_id=a.request_id,
                   operator=a.operator,
                   accept_material_mismatch=a.accept_material_mismatch,
                   operator_text=a.operator_text,
                   verification_method=a.verification_method,
                   grace_seconds=a.grace_seconds,
                   grace_notify_cmd=a.grace_notify_cmd)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
