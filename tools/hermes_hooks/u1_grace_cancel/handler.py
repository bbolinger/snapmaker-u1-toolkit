"""Hermes gateway hook: reply `cancel` (or `stop` / `abort`) in the U1
grace-period Telegram DM and the pending print aborts before any HTTP
call reaches the printer.

Match: message text is EXACTLY one of {cancel, stop, abort, /cancel,
/stop, /abort}, whitespace-trimmed, case-insensitive. Substrings do
NOT match — "cancel that idea" is safe.

Contract with u1_print_start_gate.py + u1_grace_notify_hermes.sh:
  * When the gate opens a grace window, the notify script writes a
    per-request file at /tmp/u1_pending_cancel/<request_id>.json:
      {
        "request_id":    "u1_2026_0701_abc123",
        "cancel_marker": "/opt/data/.../pre_start_cancel.marker",
        "filename":      "plate1.gcode",
        "grace_seconds": 120,
        "expires_at":    "2026-07-01T20:35:00+00:00"
      }
    Multiple concurrent grace windows each write their own file so
    they can't race each other.
  * The gate polls the cancel_marker every second. When we touch it,
    the gate wakes up, returns the refusal payload, and NEVER HTTPs
    the printer.
  * When the gate exits (cancel OR expire), it deletes its own file
    in the pending dir.

Multiple pending windows + bare cancel: touch ALL current markers.
Rationale — in Brent's setup this is the same U1 handling one job at a
time; if somehow two windows are open at once, an operator's "cancel"
is intended as "stop what's about to happen," not "guess which of
several pending things to cancel." Preserves the trust-the-human
design. Expired entries are ignored.

No AI, no LLM call, no interpretation. Pure pattern match + file
touch. Fires from Hermes' gateway process directly.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import json

PENDING_DIR = Path("/tmp/u1_pending_cancel")
LOG_FILE = Path(__file__).parent / "hook.log"

CANCEL_KEYWORDS = {
    "cancel", "stop", "abort",
    "/cancel", "/stop", "/abort",
}


def _log(entry: dict) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as fh:
            fh.write(json.dumps(
                {"ts": datetime.now(timezone.utc).isoformat(), **entry}) + "\n")
    except Exception:
        pass


def _extract_text(context: dict) -> str:
    """Message context shape varies slightly across gateway platforms.
    Try the common keys. Return the raw stripped text or empty string."""
    for k in ("message", "text", "raw_message"):
        val = context.get(k)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            for kk in ("text", "message", "content"):
                sub = val.get(kk)
                if isinstance(sub, str) and sub.strip():
                    return sub.strip()
    return ""


def _is_cancel_message(text: str) -> bool:
    """Exact-match on cancel keywords, whitespace-trimmed, case-
    insensitive. Substrings do NOT match — "cancel that plan" is
    intentionally safe."""
    return text.strip().lower() in CANCEL_KEYWORDS


def _load_pending_windows() -> list[dict]:
    """Read every json file in PENDING_DIR, drop expired or malformed
    entries. Returns a list of active pending-window state dicts."""
    if not PENDING_DIR.exists():
        return []
    now = datetime.now(timezone.utc)
    out = []
    for p in PENDING_DIR.iterdir():
        if not p.is_file() or p.suffix != ".json":
            continue
        try:
            state = json.loads(p.read_text())
            marker = state.get("cancel_marker")
            expires_at = state.get("expires_at")
            if not marker:
                continue
            if expires_at:
                try:
                    exp = datetime.fromisoformat(
                        expires_at.replace("Z", "+00:00"))
                    if now > exp:
                        continue  # expired; ignore (SIGKILL belt)
                except ValueError:
                    pass  # unparseable expiry — treat as valid
            state["_source_file"] = str(p)
            out.append(state)
        except Exception:
            continue
    return out


async def handle(event_type: str, context: dict) -> None:
    text = _extract_text(context)
    if not text:
        return
    if not _is_cancel_message(text):
        return
    pending = _load_pending_windows()
    if not pending:
        _log({"event": "cancel_ignored_no_pending_window",
              "text": text[:60],
              "platform": context.get("platform"),
              "user_id": context.get("user_id")})
        return
    # Touch every active pending marker. In Brent's single-U1 setup
    # there's normally only one; if multiple somehow exist, cancel-
    # means-cancel-all is the least surprising behavior.
    for state in pending:
        marker_path = Path(state["cancel_marker"])
        request_id = state.get("request_id", "unknown")
        try:
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text("cancel via telegram hook")
            _log({"event": "cancel_marker_touched",
                  "request_id": request_id,
                  "marker": str(marker_path),
                  "platform": context.get("platform"),
                  "user_id": context.get("user_id"),
                  "message_text": text[:60]})
        except Exception as exc:
            _log({"event": "cancel_touch_failed",
                  "request_id": request_id,
                  "marker": str(marker_path),
                  "error": f"{type(exc).__name__}: {exc}"})
