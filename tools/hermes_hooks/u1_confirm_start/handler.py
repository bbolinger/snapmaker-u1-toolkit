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
    <pending-confirm dir>/<request_id>.json (u1_pending resolver):
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
      python3 <scripts-dir>/u1_kit_workflow.py \
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
    <rid>.claimed.<claim_id>.json (an opaque per-spawn id) BEFORE spawning (of
    N concurrent YESes exactly one wins the rename; the rest find nothing).
    The spawned child is passed --confirm-claim-id and, at its commitment
    point (right before it consumes the single-use token), releases ONLY its
    own claim. The parent records the ACTUAL child pid into the claim content
    so the reaper can tell a live confirm from a crashed one. Spawn failure
    renames the claim back while unexpired, so the operator's next YES retries
    instead of the window dying silently. A child that dies BEFORE its
    commitment point leaves the claim for _reap_orphaned_claims to restore.
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
import uuid

def _pending_dir(kind: str) -> Path:
    """KEEP IN SYNC with scripts/u1_pending.py (canonical copy + rationale).
    This hook file deploys standalone into the gateway's hooks dir, so the
    ~10-line rule is duplicated; test_pending_paths.py asserts identity."""
    import tempfile
    explicit = os.environ.get(f"U1_PENDING_{kind.upper()}_DIR", "").strip()
    if explicit:
        return Path(explicit)
    root = os.environ.get("U1_PENDING_STATE_DIR", "").strip()
    if root:
        return Path(root) / kind
    return Path(tempfile.gettempdir()) / "u1_pending" / kind


PENDING_DIR = _pending_dir("confirm")
LOG_FILE = Path(__file__).parent / "hook.log"

def _scripts_dir() -> Path:
    """Runtime scripts dir, resolved from THIS hook's side of the boundary.
    KEEP IN SYNC with adapters/hermes/tools/u1_kit_tool.py (same chain);
    scripts/ consumers self-locate via u1_runtime_paths instead. Never
    resolved from marker content — the marker contributes ONLY a request id."""
    explicit = os.environ.get("U1_RUNTIME_SCRIPTS_DIR", "").strip()
    if explicit:
        return Path(explicit)
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        cand = Path(hermes_home) / "scripts"
        if (cand / "u1_kit_workflow.py").is_file():
            return cand
    return Path("/opt/data/scripts")


# The ONLY thing this hook ever executes. Marker content contributes one
# argv element — a request id that must match _REQUEST_ID_RE.
WORKFLOW_PY = str(_scripts_dir() / "u1_kit_workflow.py")
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
        _sp.Popen(["python3", str(_scripts_dir() / "u1_notify.py"), text],
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


# A spawn that has claimed but not yet recorded its child pid is given
# this long to finish before the reaper treats it as a failed spawn.
_CLAIM_SPAWN_GRACE_S = 30.0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists, owned by someone else — treat as alive
    except Exception:
        return True          # unknown — do not reap on uncertainty


def _reap_orphaned_claims() -> None:
    """Restore a claim whose child died BEFORE releasing it (Q3, 2026-07-08;
    pid source fixed in audit 2026-07-09).

    Liveness is the CHILD pid recorded in the claim CONTENT (`child_pid`) by
    _spawn_confirm — NOT a pid parsed from the filename (the name now carries an
    opaque claim_id). The old code parsed os.getpid() = the gateway pid, always
    alive, so it never reaped. A dead child_pid means the child crashed before
    its commitment point; restore `<rid>.json` if the window is still live, else
    drop. A live child_pid is a confirm in flight — left alone. A claim with no
    child_pid yet is a spawn still recording; it gets a short mtime grace before
    being treated as a failed-spawn orphan."""
    if not PENDING_DIR.exists():
        return
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    for p in list(PENDING_DIR.iterdir()):
        if not p.is_file() or ".claimed." not in p.name:
            continue
        try:
            st = json.loads(p.read_text())
        except Exception:
            st = {}
        rid = str(st.get("request_id") or "")
        child_pid = st.get("child_pid")
        if child_pid is None:
            # spawn hasn't recorded the child pid yet (or died before it could).
            try:
                age = now_ts - p.stat().st_mtime
            except Exception:
                age = 0.0
            if age < _CLAIM_SPAWN_GRACE_S:
                continue  # genuinely in flight — leave it
            # stale + no pid -> the recording never happened -> orphan.
        else:
            try:
                if _pid_alive(int(child_pid)):
                    continue  # child running -> in flight
            except Exception:
                continue  # unparseable pid -> do not reap on uncertainty
        exp = st.get("expires_at")
        live = True
        if exp:
            try:
                live = now <= datetime.fromisoformat(
                    str(exp).replace("Z", "+00:00"))
            except Exception:
                live = True
        if not rid or not _REQUEST_ID_RE.match(rid):
            _delete_marker(p, "confirm_claim_orphan_bad_dropped")
            continue
        target = PENDING_DIR / f"{rid}.json"
        if live and not target.exists():
            try:
                os.replace(p, target)
                _log({"event": "confirm_claim_orphan_restored",
                      "request_id": rid, "dead_child_pid": child_pid})
            except Exception as exc:
                _log({"event": "confirm_claim_orphan_restore_failed",
                      "request_id": rid,
                      "error": f"{type(exc).__name__}: {exc}"})
        else:
            _delete_marker(p, "confirm_claim_orphan_expired_dropped",
                           request_id=rid)


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
    _reap_orphaned_claims()
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

    Claim = atomic rename to <rid>.claimed.<claim_id>.json (opaque per-spawn
    id): of N concurrent YESes exactly one wins; the losers see
    FileNotFoundError and stand down. On spawn success the claim is RETAINED
    and annotated with the actual child pid; the child releases its own claim
    at its commitment point, and the reaper restores it if the child dies
    first. Spawn failure renames it back (while unexpired) so the next YES
    retries — the old unlink-then-spawn burned the window when Popen failed.
    Returns True when the spawn was issued."""
    rid = str(state.get("request_id") or "")
    src = state.get("_source_file")
    if not src or not _REQUEST_ID_RE.match(rid):
        return False
    src_path = Path(src)
    # Q3 fix (audit 2026-07-09): the claim filename carries an opaque claim_id,
    # NOT os.getpid(). The old name embedded the GATEWAY pid (this hook runs in
    # the gateway), so the reaper's liveness check always saw a live process
    # and never restored an orphaned claim. Liveness now lives in the claim
    # CONTENT as the actual spawned child pid (recorded below).
    claim_id = uuid.uuid4().hex[:12]
    claimed = src_path.with_name(f"{rid}.claimed.{claim_id}.json")
    try:
        os.rename(src_path, claimed)
    except FileNotFoundError:
        return False  # another YES beat us to it — single-fire held
    except Exception as exc:
        _log({"event": "confirm_marker_claim_failed", "request_id": rid,
              "error": f"{type(exc).__name__}: {exc}"})
        return False
    # The child releases ONLY this claim_id at its commitment point (audit #3),
    # so a concurrently re-armed generation's claim is never deleted.
    cmd = ["python3", WORKFLOW_PY, "--confirm-start-for", rid,
           "--confirm-claim-id", claim_id, "--json-events"]
    # Log path is DERIVED, never read from the marker.
    log_path = _pending_dir("log") / f"u1_confirm_start_{rid}.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass  # the open() below already tolerates a missing log
    out = None
    spawn_ok = False
    child = None
    try:
        try:
            out = open(log_path, "ab")
        except Exception:
            out = None  # losing the log is acceptable; losing the start isn't
        child = subprocess.Popen(
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
        # Q3 (child-ack, 2026-07-08): do NOT delete the claim here. The old
        # code consumed the operator's YES the instant Popen succeeded — a
        # child that then crashed during bootstrap/import/request-load
        # stranded a valid YES with nothing started. The claim now stays
        # until the CHILD releases it right before consuming the token; a
        # child that dies before that point leaves the claim for the reaper
        # to restore, so the operator's next YES retries. Single-fire still
        # holds: a concurrent YES skips `.claimed.` markers.
        #
        # Record the ACTUAL child pid + claim_id into the claim content so the
        # reaper checks the CHILD's liveness, not the gateway's.
        #
        # RACE (follow-up audit 2026-07-09): a fast child can reach its
        # commitment point and DELETE this claim before we get here. We must
        # NOT recreate a claim the child intentionally removed — doing so would
        # let the reaper later "restore" a dead confirmation window. So open the
        # EXISTING file read+write with NO create:
        #   * already gone   -> FileNotFoundError -> child committed, do nothing;
        #   * deleted mid-write -> our bytes land on the now-unlinked inode and
        #     the claim is never resurrected (no path points at it).
        try:
            with open(claimed, "r+") as _fh:
                try:
                    _st = json.loads(_fh.read() or "{}")
                except Exception:
                    _st = dict(state)
                _st["child_pid"] = child.pid if child is not None else None
                _st["claim_id"] = claim_id
                _st.setdefault("request_id", rid)
                _fh.seek(0)
                _fh.truncate()
                _fh.write(json.dumps(_st))
        except FileNotFoundError:
            _log({"event": "confirm_claim_released_before_pid_record",
                  "request_id": rid})
        except Exception as exc:
            _log({"event": "confirm_claim_pid_record_failed", "request_id": rid,
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
            # os.replace, not os.rename: POSIX rename overwrites an existing
            # target (a freshly re-armed marker) but Windows rename refuses
            # (WinError 183) - replace gives the same overwrite semantics on
            # both platforms.
            os.replace(claimed, src_path)
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
