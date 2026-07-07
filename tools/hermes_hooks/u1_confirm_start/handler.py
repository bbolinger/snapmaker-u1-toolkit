"""Hermes gateway hook: the operator's YES at the bed-clear prompt starts
the print — the model never holds a start command.

Why this exists (incident 2026-07-07): the agent model fired the emitted
`--confirm-start <token>` command itself ten seconds after showing the
previews — no operator YES ever happened. Guidance text cannot bound an
agentic model, so the start capability moves where guidance isn't needed:
the workflow stops emitting any confirm command to the model and instead
writes a per-request marker file; THIS hook redeems the operator's actual
YES message by spawning the confirm command directly from the gateway
process. The model can no longer start a print — it has nothing to fire.

Match: message text is EXACTLY `yes` (whitespace-trimmed, case-insensitive,
trailing punctuation tolerated: "YES!!" counts) — or `yes <code>` where
code is the last chars of the request_id. Anything else does NOT match;
"yes but wait" is a conversation, not a confirmation.

Contract with u1_kit_workflow.py:
  * When the workflow reaches the bed-clear prompt it writes
    /tmp/u1_pending_confirm/<request_id>.json:
      {
        "request_id":  "u1_2026_0707_abc123",
        "confirm_cmd": ["python3", ".../u1_kit_workflow.py",
                         "--confirm-start", "<token>", "--json-events"],
        "log_path":    ".../requests/<rid>/confirm_via_hook.log",
        "filename":    "plate1.gcode",
        "expires_at":  "<ISO timestamp>"
      }
  * The confirm command redeems the same single-use token + nonce chain
    as before — every downstream safety check (revision, gcode hash,
    material, grace window, cancel hook) is unchanged.
  * The workflow deletes the marker when the token is redeemed; this hook
    deletes it BEFORE spawning (single-fire: a double YES can't double-
    spawn) and ignores expired entries.

START NEVER GUESSES: with multiple active windows, a bare `yes` refuses
and logs — the operator must say `yes <code>`. (The cancel hook does the
opposite — bare cancel kills everything — because the safe direction is
asymmetric by design.)

No AI, no LLM call, no interpretation. Pure pattern match + detached
spawn. Fires from Hermes' gateway process directly.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import subprocess

PENDING_DIR = Path(os.environ.get("U1_PENDING_CONFIRM_DIR",
                                  "/tmp/u1_pending_confirm"))
LOG_FILE = Path(__file__).parent / "hook.log"

# `yes` / `yes abc123`. The code is the request_id tail the bed-clear DM
# shows — those tails are hex; requiring hex keeps natural replies like
# "yes please" from parsing as a code. (The cancel hook is looser on
# purpose: starts are the direction that never guesses.)
_YES_RE = re.compile(
    r"^(?:/)?yes(?:\s+([0-9a-f]{4,12}))?$",
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


def _parse_yes_message(text: str) -> tuple[bool, str | None]:
    """Returns (is_yes, code). Bare `yes` → (True, None); `yes abc123` →
    (True, code). Extra words do not match — a confirmation must be a
    confirmation and nothing else. Trailing punctuation is tolerated
    ("YES!" / "yes.") — enthusiasm isn't ambiguity."""
    stripped = text.strip().lower().rstrip("!.?,;: ")
    m = _YES_RE.match(stripped)
    if m:
        return True, m.group(1)
    return False, None


def _load_pending_windows() -> list[dict]:
    """Read every json file in PENDING_DIR, drop expired or malformed
    entries. Returns a list of active pending-confirm state dicts."""
    if not PENDING_DIR.exists():
        return []
    now = datetime.now(timezone.utc)
    out = []
    for p in sorted(PENDING_DIR.iterdir()):
        if not p.is_file() or p.suffix != ".json":
            continue
        try:
            state = json.loads(p.read_text())
            cmd = state.get("confirm_cmd")
            if not (isinstance(cmd, list) and cmd):
                continue
            expires_at = state.get("expires_at")
            if expires_at:
                try:
                    exp = datetime.fromisoformat(
                        expires_at.replace("Z", "+00:00"))
                    if now > exp:
                        continue  # expired; the token TTL is the real gate
                except ValueError:
                    pass
            state["_source_file"] = str(p)
            out.append(state)
        except Exception:
            continue
    return out


def _spawn_confirm(state: dict) -> bool:
    """Single-fire: remove the marker FIRST (a second YES finds nothing),
    then spawn the confirm command detached with output captured to the
    request's own log. Returns True when the spawn was issued."""
    src = state.get("_source_file")
    try:
        if src:
            Path(src).unlink()
    except FileNotFoundError:
        return False  # another YES beat us to it — single-fire held
    except Exception as exc:
        _log({"event": "confirm_marker_unlink_failed",
              "request_id": state.get("request_id"),
              "error": f"{type(exc).__name__}: {exc}"})
        return False
    log_path = state.get("log_path")
    try:
        if log_path:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            out = open(log_path, "ab")
        else:
            out = subprocess.DEVNULL
        subprocess.Popen(
            state["confirm_cmd"],
            stdout=out, stderr=subprocess.STDOUT if log_path else out,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return True
    except Exception as exc:
        _log({"event": "confirm_spawn_failed",
              "request_id": state.get("request_id"),
              "error": f"{type(exc).__name__}: {exc}"})
        return False


async def handle(event_type: str, context: dict) -> None:
    text = _extract_text(context)
    if not text:
        return
    is_yes, code = _parse_yes_message(text)
    if not is_yes:
        return
    pending = _load_pending_windows()
    if not pending:
        return  # nothing armed; a stray "yes" in conversation is not ours
    if code is not None:
        pending = [s for s in pending
                   if str(s.get("request_id", "")).lower().endswith(code)]
        if not pending:
            _log({"event": "yes_code_no_match", "code": code,
                  "text": text[:60],
                  "platform": context.get("platform"),
                  "user_id": context.get("user_id")})
            return
    if len(pending) > 1:
        # START NEVER GUESSES. Two armed windows + a bare yes = refuse.
        _log({"event": "yes_ambiguous_multiple_windows",
              "request_ids": [s.get("request_id") for s in pending],
              "text": text[:60],
              "platform": context.get("platform"),
              "user_id": context.get("user_id")})
        return
    state = pending[0]
    ok = _spawn_confirm(state)
    _log({"event": "confirm_spawned" if ok else "confirm_not_spawned",
          "request_id": state.get("request_id"),
          "filename": state.get("filename"),
          "platform": context.get("platform"),
          "user_id": context.get("user_id"),
          "message_text": text[:60]})
