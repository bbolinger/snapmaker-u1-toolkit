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
        "request_id":       "u1_2026_0707_abc123",
        "filename":         "plate1.gcode",
        "platform":         "telegram",
        "operator_user_id": "8131922235",
        "created_at":       "<ISO timestamp>",
        "expires_at":       "<ISO timestamp>"
      }
  * The marker is OPAQUE: display + binding data only. It never carries a
    command, a token, or a path. This hook builds its own argv from the
    constants below — a marker is a claim that a window exists, not
    instructions to run (review finding: the old confirm_cmd field made
    anything that could write /tmp a command author, gated only by the
    word "yes"). The spawned command is exactly
      python3 /opt/data/scripts/u1_kit_workflow.py \
          --confirm-start-for <request_id> --json-events
    with request_id validated against ^u1_[a-z0-9_]+$ first. The workflow
    resolves the persisted single-use confirm token server-side and then
    runs the SAME redemption path as a relayed --confirm-start — nonce,
    revision, gcode hash, phase checks all unchanged. A request id with no
    valid pending confirmation redeems nothing.
  * YES is bound to the operator: the marker's platform/operator_user_id
    (written at arm time from config) must equal the message context's
    platform/user_id. Mismatch refuses. A marker WITHOUT binding fields
    also refuses — fail closed; the workflow warns at arm time when the
    binding config is missing, so this shows up before the YES, not at it.
  * Claim-then-spawn: the marker is atomically renamed to
    <rid>.claimed.<pid>.json BEFORE spawning (of N concurrent YESes exactly
    one wins the rename; the rest find nothing). Spawn success deletes the
    claimed file; spawn failure renames it back while unexpired, so the
    operator's next YES retries instead of the window dying silently.
  * Expiry fails closed: expires_at missing -> marker deleted; unparseable
    or more than 24h out -> quarantined to <name>.bad (kept for
    inspection); expired -> deleted. All logged.

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

# The ONLY thing this hook ever executes. Marker content contributes one
# argv element — a request id that must match _REQUEST_ID_RE.
WORKFLOW_PY = "/opt/data/scripts/u1_kit_workflow.py"
_REQUEST_ID_RE = re.compile(r"^u1_[a-z0-9_]+$")

# A marker claiming to be valid for more than a day is not a window
# anyone armed on purpose — the workflow TTL is 15 minutes.
_MAX_EXPIRY_AHEAD_S = 24 * 60 * 60

# `yes` / `yes abc123`. The code is the request_id tail the bed-clear DM
# shows — those tails are hex; requiring hex keeps natural replies like
# "yes please" from parsing as a code. (The cancel hook is looser on
# purpose: starts are the direction that never guesses.)
_YES_RE = re.compile(
    r"^(?:/)?yes(?:\s+([0-9a-f]{4,12}))?$",
    re.IGNORECASE,
)


def _notify_operator(text: str) -> None:
    """DM the BOUND operator (never the sender) via the toolkit notifier.
    Live 2026-07-07: a false-negative identity refusal left the legitimate
    operator staring at four minutes of silence. Any refusal now reports to
    the operator the window was armed for — a stranger triggering one gets
    nothing in their own chat, but the operator always learns their machine
    declined something and why. Best-effort; failures only log."""
    try:
        import subprocess as _sp
        _sp.Popen(["python3", "/opt/data/scripts/u1_notify.py", text],
                  stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                  stdin=_sp.DEVNULL, start_new_session=True)
    except Exception as exc:
        _log({"event": "refusal_notify_failed",
              "error": f"{type(exc).__name__}: {exc}"})


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


def _quarantine_marker(p: Path, event: str, **details) -> None:
    """Rename a bad marker to <name>.bad — out of the pending set but kept
    on disk, because a marker that LIES about its expiry (or its request
    id) is evidence, not litter."""
    try:
        os.replace(p, p.with_name(p.name + ".bad"))
        _log({"event": event, "marker": p.name, **details})
    except Exception as exc:
        _log({"event": f"{event}_rename_failed", "marker": p.name,
              "error": f"{type(exc).__name__}: {exc}", **details})


def _delete_marker(p: Path, event: str, **details) -> None:
    try:
        p.unlink()
        _log({"event": event, "marker": p.name, **details})
    except FileNotFoundError:
        pass
    except Exception as exc:
        _log({"event": f"{event}_unlink_failed", "marker": p.name,
              "error": f"{type(exc).__name__}: {exc}", **details})


def _load_pending_windows() -> list[dict]:
    """Read every marker in PENDING_DIR and enforce marker hygiene.
    Returns the list of live pending-confirm state dicts.

    Fail-closed rules (review findings — expiry used to be advisory):
      * unreadable / non-object JSON        -> quarantine to <name>.bad
      * request_id not ^u1_[a-z0-9_]+$      -> quarantine
      * expires_at missing                  -> delete
      * expires_at unparseable or tz-naive  -> quarantine
      * expires_at more than 24h out        -> quarantine
      * expired                             -> delete
    `.claimed.` files are in-flight spawns, not windows — skipped."""
    if not PENDING_DIR.exists():
        return []
    now = datetime.now(timezone.utc)
    out = []
    for p in sorted(PENDING_DIR.iterdir()):
        if not p.is_file() or p.suffix != ".json" or ".claimed." in p.name:
            continue
        try:
            state = json.loads(p.read_text())
            if not isinstance(state, dict):
                raise ValueError("marker is not a JSON object")
        except Exception as exc:
            _quarantine_marker(p, "confirm_marker_unreadable_quarantined",
                               error=f"{type(exc).__name__}: {exc}")
            continue
        rid = str(state.get("request_id") or "")
        if not _REQUEST_ID_RE.match(rid):
            _quarantine_marker(p, "confirm_marker_bad_request_id_quarantined",
                               request_id=rid[:60])
            continue
        raw_exp = state.get("expires_at")
        if not raw_exp:
            _delete_marker(p, "confirm_marker_missing_expiry_deleted",
                           request_id=rid)
            continue
        try:
            exp = datetime.fromisoformat(str(raw_exp).replace("Z", "+00:00"))
            if exp.tzinfo is None:
                raise ValueError("naive expiry timestamp")
        except Exception:
            _quarantine_marker(p, "confirm_marker_bad_expiry_quarantined",
                               request_id=rid, expires_at=str(raw_exp)[:60])
            continue
        if (exp - now).total_seconds() > _MAX_EXPIRY_AHEAD_S:
            _quarantine_marker(p, "confirm_marker_expiry_too_far_quarantined",
                               request_id=rid, expires_at=str(raw_exp)[:60])
            continue
        if now > exp:
            _delete_marker(p, "confirm_marker_expired_deleted",
                           request_id=rid)
            continue
        state["_source_file"] = str(p)
        state["_expires"] = exp
        out.append(state)
    return out


def _operator_binding_ok(state: dict, context: dict) -> bool:
    """The YES must come from the operator the window was armed for:
    context platform + user_id must equal the marker's binding fields.
    A marker WITHOUT binding fields (legacy shape, or binding config unset
    at arm time) refuses — fail closed; the workflow already warned at arm
    time, this log entry is the enforcement half. user_id is compared as a
    string on both sides — gateways deliver it as int or str depending on
    platform."""
    rid = state.get("request_id")
    want_platform = state.get("platform")
    want_user = state.get("operator_user_id")
    if not want_platform or not want_user:
        _log({"event": "confirm_refused_marker_missing_binding",
              "request_id": rid,
              "platform": context.get("platform"),
              "user_id": context.get("user_id")})
        _notify_operator(f"\u26a0\ufe0f A YES for {state.get('filename')} "
                         "was refused: this window has no operator binding "
                         "(set U1_OPERATOR_BINDING or TELEGRAM_ALLOWED_USERS "
                         "and re-run). Nothing was started.")
        return False
    got_platform = str(context.get("platform") or "").strip().lower()
    got_user = str(context.get("user_id") or "").strip()
    if (got_platform != str(want_platform).strip().lower()
            or got_user != str(want_user).strip()):
        _log({"event": "confirm_refused_operator_mismatch",
              "request_id": rid,
              "expected_platform": want_platform,
              "platform": context.get("platform"),
              "user_id": context.get("user_id")})
        _notify_operator(f"\u26a0\ufe0f A YES for {state.get('filename')} "
                         "was refused: it did not come from the bound "
                         "operator account. Nothing was started.")
        return False
    # Conversation binding (final release review): the YES must come from
    # the private DM the window was armed for, not merely from the right
    # human somewhere on the platform. Model-free start is a private-DM
    # feature by design — a group chat refuses outright, and a chat_id
    # mismatch (same operator, different conversation) refuses too. A
    # marker without the chat field (legacy shape) refuses, fail closed.
    # Gateways name the one-on-one chat differently: Telegram's Bot API
    # says "private", Hermes normalizes to "dm" (live 2026-07-07: the
    # operator's own DM was refused for not being called "private").
    _PRIVATE_CHAT_TYPES = {"private", "dm", "direct", "im"}
    chat_type = str(context.get("chat_type") or "").strip().lower()
    if chat_type and chat_type not in _PRIVATE_CHAT_TYPES:
        _log({"event": "confirm_refused_not_private_chat",
              "request_id": rid, "chat_type": chat_type,
              "user_id": context.get("user_id")})
        _notify_operator(f"\u26a0\ufe0f A YES for {state.get('filename')} "
                         "was refused: it came from a group or channel, and "
                         "print confirmation only works in your private DM. "
                         "Nothing was started.")
        return False
    want_chat = state.get("operator_chat_id")
    if not want_chat:
        _log({"event": "confirm_refused_marker_missing_chat_binding",
              "request_id": rid})
        _notify_operator(f"\u26a0\ufe0f A YES for {state.get('filename')} "
                         "was refused: stale confirmation window from an "
                         "older version. Re-run the flow for a fresh "
                         "bed-clear prompt. Nothing was started.")
        return False
    got_chat = str(context.get("chat_id") or "").strip()
    if got_chat != str(want_chat).strip():
        _log({"event": "confirm_refused_chat_mismatch",
              "request_id": rid,
              "chat_id": context.get("chat_id")})
        _notify_operator(f"\u26a0\ufe0f A YES for {state.get('filename')} "
                         "was refused: wrong conversation. Reply YES in the "
                         "private DM where the bed-clear prompt arrived. "
                         "Nothing was started.")
        return False
    return True


def _spawn_confirm(state: dict) -> bool:
    """Claim-then-spawn. The command is built HERE from constants — the
    marker's only contribution is a request id that already matched
    _REQUEST_ID_RE in the loader (checked again for direct callers).

    Claim = atomic rename to <rid>.claimed.<pid>.json: of N concurrent
    YESes exactly one wins; the losers see FileNotFoundError and stand
    down. Spawn success deletes the claimed file. Spawn failure renames it
    back (while unexpired) so the next YES retries — the old unlink-then-
    spawn burned the window when Popen failed. Returns True when the spawn
    was issued."""
    rid = str(state.get("request_id") or "")
    src = state.get("_source_file")
    if not src or not _REQUEST_ID_RE.match(rid):
        return False
    src_path = Path(src)
    claimed = src_path.with_name(f"{rid}.claimed.{os.getpid()}.json")
    try:
        os.rename(src_path, claimed)
    except FileNotFoundError:
        return False  # another YES beat us to it — single-fire held
    except Exception as exc:
        _log({"event": "confirm_marker_claim_failed", "request_id": rid,
              "error": f"{type(exc).__name__}: {exc}"})
        return False
    cmd = ["python3", WORKFLOW_PY, "--confirm-start-for", rid, "--json-events"]
    # Log path is DERIVED, never read from the marker.
    log_path = Path(f"/tmp/u1_confirm_start_{rid}.log")
    out = None
    spawn_ok = False
    try:
        try:
            out = open(log_path, "ab")
        except Exception:
            out = None  # losing the log is acceptable; losing the start isn't
        subprocess.Popen(
            cmd,
            stdout=out if out is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        spawn_ok = True
    except Exception as exc:
        _log({"event": "confirm_spawn_failed", "request_id": rid,
              "error": f"{type(exc).__name__}: {exc}"})
    finally:
        if out is not None:
            try:
                out.close()
            except Exception:
                pass
    if spawn_ok:
        try:
            claimed.unlink()
        except Exception as exc:
            _log({"event": "confirm_claimed_cleanup_failed", "request_id": rid,
                  "error": f"{type(exc).__name__}: {exc}"})
        return True
    # Spawn failed — put the marker back while the window is still live so
    # the operator's next YES can retry.
    exp = state.get("_expires")
    if exp is None:
        try:
            exp = datetime.fromisoformat(
                str(state.get("expires_at")).replace("Z", "+00:00"))
        except Exception:
            exp = None
    try:
        if exp is not None and datetime.now(timezone.utc) <= exp:
            os.rename(claimed, src_path)
            _log({"event": "confirm_spawn_failed_marker_restored",
                  "request_id": rid})
        else:
            claimed.unlink()
            _log({"event": "confirm_spawn_failed_marker_expired",
                  "request_id": rid})
    except Exception as exc:
        _log({"event": "confirm_spawn_failed_restore_failed",
              "request_id": rid,
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
    if not _operator_binding_ok(state, context):
        return
    ok = _spawn_confirm(state)
    _log({"event": "confirm_spawned" if ok else "confirm_not_spawned",
          "request_id": state.get("request_id"),
          "filename": state.get("filename"),
          "platform": context.get("platform"),
          "user_id": context.get("user_id"),
          "message_text": text[:60]})
