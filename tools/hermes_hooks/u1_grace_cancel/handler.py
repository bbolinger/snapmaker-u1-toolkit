"""Hermes gateway hook: type CANCEL in the U1 grace-period Telegram DM and
the pending print aborts before any HTTP call reaches the printer.

Contract with u1_print_start_gate.py:
  * When the gate opens a grace window, the notify script writes the
    absolute path of the cancel_marker to /tmp/u1_pending_cancel_marker
    (single-instance — a second concurrent print would overwrite; for
    Brent's single-U1 setup this is fine and simpler than per-request
    routing). The file also carries the request_id so the log is
    unambiguous.
  * The gate polls the cancel_marker every second. When we touch it,
    the gate wakes up, returns the refusal payload, and NEVER HTTPs
    the printer.
  * When the window closes (either the operator cancelled or it
    expired) the gate deletes /tmp/u1_pending_cancel_marker so this
    handler stops treating an old CANCEL as an active one.

Match: message text (case-insensitive, whitespace-trimmed) equals one
of {cancel, stop, abort, /cancel, /stop, /abort}. Deliberately narrow
so casual conversation doesn't trigger it.

No AI, no LLM call, no interpretation. Pure pattern match + file
touch. Fires from Hermes' gateway process directly.
"""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
import json

PENDING_STATE_FILE = Path("/tmp/u1_pending_cancel_marker")
CANCEL_KEYWORDS = {
    "cancel", "stop", "abort",
    "/cancel", "/stop", "/abort",
}
# Log next to the hook itself. `__file__` resolves to the handler's
# location on disk (Hermes loads it from HOOKS_DIR/<hook_name>/handler.py)
# so we don't depend on HOME or the Hermes docs' path convention.
LOG_FILE = Path(__file__).parent / "hook.log"


def _log(entry: dict) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as fh:
            fh.write(json.dumps(
                {"ts": datetime.now().isoformat(), **entry}) + "\n")
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


async def handle(event_type: str, context: dict) -> None:
    text = _extract_text(context)
    if not text:
        return
    if text.lower() not in CANCEL_KEYWORDS:
        return
    # Message IS a cancel keyword. Is there a pending grace window?
    if not PENDING_STATE_FILE.exists():
        _log({"event": "cancel_ignored_no_pending_window",
              "text": text[:60],
              "platform": context.get("platform"),
              "user_id": context.get("user_id")})
        return
    try:
        state = json.loads(PENDING_STATE_FILE.read_text())
        marker_path = Path(state["cancel_marker"])
        request_id = state.get("request_id", "unknown")
    except Exception as exc:
        _log({"event": "cancel_state_unreadable",
              "error": f"{type(exc).__name__}: {exc}"})
        return
    # Touch the marker. The gate polls this file once per second and
    # will return the refusal payload as soon as it sees it.
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
