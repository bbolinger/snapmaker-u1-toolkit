"""Hermes gateway hook: reply `cancel` (or `stop` / `abort`) in the U1
grace-period Telegram DM and the pending print aborts before any HTTP
call reaches the printer.

Match: message text is EXACTLY a cancel keyword ({cancel, stop, abort,
/cancel, /stop, /abort}, whitespace-trimmed, case-insensitive) — which
cancels EVERY active grace window — or a keyword followed by a request
code (`cancel abc123`, the last 6 chars of the request_id) — which
cancels ONLY the matching window. Anything else does NOT match —
"cancel that idea" is safe. A code that matches no active window
cancels nothing (logged), rather than guessing.

Contract with u1_print_start_gate.py + u1_grace_notify_hermes.sh:
  * When the gate opens a grace window, the notify script writes a
    per-request file at /tmp/u1_pending_cancel/<request_id>.json:
      {
        "request_id":    "u1_2026_0701_abc123",
        "cancel_marker": "/opt/data/.../pre_start_cancel.marker",
        "filename":      "plate1.gcode",
        "grace_seconds": 120,
        "expires_at":    "<ISO timestamp>"
      }
    Multiple concurrent grace windows each write their own file so
    they can't race each other.
  * The gate polls the cancel_marker every second. When we touch it,
    the gate wakes up, returns the refusal payload, and NEVER HTTPs
    the printer.
  * When the gate exits (cancel OR expire), it deletes its own file
    in the pending dir.

Multiple pending windows + bare cancel: touch ALL current markers.
Rationale — in a typical single-U1 setup this is one job at a
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
import re

PENDING_DIR = Path("/tmp/u1_pending_cancel")
LOG_FILE = Path(__file__).parent / "hook.log"

CANCEL_KEYWORDS = {
    "cancel", "stop", "abort",
    "/cancel", "/stop", "/abort",
}

# `cancel` / `cancel abc123`. The code is the request_id tail the notify
# DM shows; [a-z0-9_-]{4,12} keeps prose like "cancel that idea" unmatched.
_CANCEL_RE = re.compile(
    r"^(?:/)?(?:cancel|stop|abort)(?:\s+([a-z0-9_-]{4,12}))?$",
    re.IGNORECASE,
)


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


def _parse_cancel_message(text: str) -> tuple[bool, str | None]:
    """Returns (is_cancel, code). Bare keyword → (True, None): cancel all
    active windows. Keyword + code → (True, code): cancel only the window
    whose request_id ends with the code. Prose ("cancel that plan") does
    not match — intentionally safe.

    Trailing punctuation is tolerated ("CANCEL!!!" / "cancel." / "stop!?"):
    extra WORDS are ambiguity, but exclamation marks are urgency — the
    panicking operator hammering the keyboard is exactly who this exists
    for. Only word-content decides the match."""
    stripped = text.strip().lower().rstrip("!.?,;: ")
    if stripped in CANCEL_KEYWORDS:
        return True, None
    m = _CANCEL_RE.match(stripped)
    if m:
        return True, m.group(1)
    return False, None


def _is_cancel_message(text: str) -> bool:
    """Back-compat shim over _parse_cancel_message."""
    return _parse_cancel_message(text)[0]


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
    is_cancel, code = _parse_cancel_message(text)
    if not is_cancel:
        return
    pending = _load_pending_windows()
    if not pending:
        _log({"event": "cancel_ignored_no_pending_window",
              "text": text[:60],
              "platform": context.get("platform"),
              "user_id": context.get("user_id")})
        return
    if code is not None:
        # Code-scoped cancel: only the window whose request_id ends with
        # the code. A code that matches nothing cancels NOTHING — we log
        # instead of guessing, so a typo can't kill an unrelated print.
        scoped = [s for s in pending
                  if str(s.get("request_id", "")).lower().endswith(code)]
        if not scoped:
            _log({"event": "cancel_code_no_match", "code": code,
                  "active_request_ids": [s.get("request_id") for s in pending],
                  "text": text[:60],
                  "platform": context.get("platform"),
                  "user_id": context.get("user_id")})
            return
        pending = scoped
    # Bare keyword: touch every active pending marker. In a single-U1
    # setup there's normally only one; if multiple somehow exist, cancel-
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
