"""Gateway-side form primitive — blocking event-based queue, mirrors
``tools.clarify_gateway``.

The ``form`` tool needs to present a multi-field schema as native UI on the
host platform (e.g. Telegram inline keyboards) and block the agent thread
until the user submits an answer. This module is the thread-safe primitive
the gateway adapter and the agent thread coordinate through.

Two paths from the platform adapter:
  1. **Native UI** (button-driven): adapter renders the schema as inline
     keyboards, handles taps in-place, and on Submit calls
     ``resolve_gateway_form(form_id, answer_dict)`` to unblock the agent.
  2. **Text fallback**: the schema also carries a ``text_fallback`` field so
     an adapter without native UI can present a typed one-line path to the
     same answer.

Module-level state (same shape as ``clarify_gateway``) so platform adapters
call ``resolve_gateway_form`` without holding a back-reference to the
``GatewayRunner`` instance.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Default timeout for waiting on the operator's form submission. Long because
# the operator may need time to think + edit fields before submitting.
DEFAULT_FORM_TIMEOUT_SEC = 600


# =========================================================================
# Module-level state
# =========================================================================

@dataclass
class _FormEntry:
    """One pending form request inside a gateway session."""
    form_id: str
    session_key: str
    schema: Dict[str, Any]
    event: threading.Event = field(default_factory=threading.Event)
    response: Optional[Dict[str, Any]] = None


_lock = threading.RLock()
_entries: Dict[str, _FormEntry] = {}
_session_index: Dict[str, List[str]] = {}


# =========================================================================
# Public API — agent-thread side
# =========================================================================

def register(form_id: str, session_key: str, schema: Dict[str, Any]) -> _FormEntry:
    """Register a pending form request and return the entry.

    The caller (gateway form_callback) will then trigger the adapter's
    ``send_form`` and block on ``wait_for_response(form_id, timeout)``.
    """
    entry = _FormEntry(form_id=form_id, session_key=session_key, schema=schema)
    with _lock:
        _entries[form_id] = entry
        _session_index.setdefault(session_key, []).append(form_id)
    return entry


def wait_for_response(form_id: str, timeout: float) -> Optional[Dict[str, Any]]:
    """Block on the entry's event until resolved or timeout fires.

    Polls in 1-second slices so the agent's inactivity heartbeat keeps firing
    (mirrors clarify_gateway). Returns the resolved answer dict, or ``None``
    on timeout.
    """
    with _lock:
        entry = _entries.get(form_id)
    if entry is None:
        return None

    try:
        from tools.environments.base import touch_activity_if_due
    except Exception:  # pragma: no cover - optional
        touch_activity_if_due = None

    deadline = time.monotonic() + max(timeout, 0.0)
    activity_state = {"last_touch": time.monotonic(), "start": time.monotonic()}
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if entry.event.wait(timeout=min(1.0, remaining)):
            break
        if touch_activity_if_due is not None:
            touch_activity_if_due(activity_state, "waiting for user form submission")

    with _lock:
        _entries.pop(form_id, None)
        ids = _session_index.get(entry.session_key)
        if ids and form_id in ids:
            ids.remove(form_id)
            if not ids:
                _session_index.pop(entry.session_key, None)

    return entry.response


# =========================================================================
# Public API — gateway / adapter side
# =========================================================================

def resolve_gateway_form(form_id: str, answer: Dict[str, Any]) -> bool:
    """Unblock the agent thread waiting on ``form_id`` with the answer dict.

    Returns True if an entry was found and resolved, False otherwise
    (already resolved, expired, or never existed).
    """
    with _lock:
        entry = _entries.get(form_id)
        if entry is None:
            return False
    if not isinstance(answer, dict):
        # Defensive: callers should always pass a dict (the --form-answers-json
        # shape). Stringly types here would silently break downstream parsing.
        logger.warning("resolve_gateway_form(%s): non-dict answer %r — coercing",
                       form_id, type(answer))
        answer = {"_raw": str(answer)}
    entry.response = dict(answer)
    entry.event.set()
    return True


def cancel_gateway_form(form_id: str) -> bool:
    """Resolve the form with an empty dict — agent treats as 'user cancelled'."""
    return resolve_gateway_form(form_id, {"_cancelled": True})


def clear_session(session_key: str) -> int:
    """Drop every pending form for a session (e.g. on session boundary).

    Returns the count of cleared entries. Unblocks any waiting threads by
    setting their events with ``response=None`` so they return ``None`` from
    ``wait_for_response``.
    """
    cleared = 0
    with _lock:
        ids = list(_session_index.get(session_key) or [])
        for fid in ids:
            entry = _entries.pop(fid, None)
            if entry is None:
                continue
            cleared += 1
            entry.event.set()  # response stays None
        _session_index.pop(session_key, None)
    return cleared


def get_form_timeout() -> float:
    """Form-wait timeout, in seconds. Mirrors clarify's config-driven knob;
    forms can take longer because they have more fields to fill in."""
    import os
    try:
        return float(os.environ.get("U1_FORM_TIMEOUT_SEC", DEFAULT_FORM_TIMEOUT_SEC))
    except (TypeError, ValueError):
        return float(DEFAULT_FORM_TIMEOUT_SEC)


# =========================================================================
# Form-callback registry — bridges the generic tool dispatch to the gateway
# =========================================================================
#
# Hermes' registry dispatch hands tool handlers only (task_id, session_id,
# user_task) — there is no callback kwarg and no agent reference (only
# hardcoded tools like clarify get agent.clarify_callback in the executor).
# So the gateway's run.py patch PUBLISHES its per-turn form callback here,
# keyed by agent.session_id — the exact value dispatch later passes to the
# handler — and the u1-form plugin's handler looks it up by that key.
# "__default__" always tracks the most recent registration so a session_id
# mismatch degrades to the latest gateway turn instead of a dead tool
# (right answer for a single-operator gateway; keyed lookup keeps
# concurrent sessions honest).

_CB_MAX_ENTRIES = 32  # bound per-session slots; __default__ never evicted

_cb_lock = threading.Lock()
_form_callbacks: Dict[str, Any] = {}
_cb_order: List[str] = []


def set_form_callback(session_id: str, callback: Any) -> None:
    """Publish the gateway's form callback for a session (and as default)."""
    key = str(session_id or "").strip() or "__default__"
    with _cb_lock:
        if key not in _form_callbacks and key != "__default__":
            _cb_order.append(key)
            while len(_cb_order) > _CB_MAX_ENTRIES:
                _form_callbacks.pop(_cb_order.pop(0), None)
        _form_callbacks[key] = callback
        _form_callbacks["__default__"] = callback


def get_form_callback(session_id: str = "") -> Optional[Any]:
    """Resolve the form callback for a session, else the latest default."""
    key = str(session_id or "").strip()
    with _cb_lock:
        if key and key in _form_callbacks:
            return _form_callbacks[key]
        return _form_callbacks.get("__default__")


# =========================================================================
# Deterministic-tool API (used by the u1_kit tool)
# =========================================================================
#
# A tool that drives the form itself (instead of the model picking the
# `form` tool and relaying the kit_form event) needs just two things: to
# confirm the gateway's per-turn form callback is wired for this session,
# and to invoke it (render + block + return the answer). These wrap the
# callback registry above so the tool never has to know the register/send/
# wait plumbing -- that lives in the gateway's published callback
# (_form_callback_sync, from the install.py run.py patch).

def get_notify(session_id: str = "") -> Optional[Any]:
    """The active form callback for a session, or None if none is wired.

    An intent-revealing alias of ``get_form_callback`` for callers that just
    want to check "is the form path installed for this session?" before using
    it."""
    return get_form_callback(session_id)


def invoke_form(session_id: str, form_schema: Dict[str, Any]) -> Dict[str, Any]:
    """Render ``form_schema`` to the operator and block until they submit,
    returning the answer dict.

    This is the deterministic entry point the u1_kit tool uses so the model
    never has to emit a ``form`` tool call. It resolves the session's
    published callback (the run.py patch's ``_form_callback_sync``) and calls
    it; that callback registers the form, sends it through the platform
    adapter, and waits on ``wait_for_response``. Returns ``{"_error": ...}``
    when no callback is wired for the session, and never raises."""
    cb = get_form_callback(session_id)
    if cb is None:
        return {"_error": "no form callback registered for this session "
                          "(run adapters/hermes/install.py and restart Hermes)"}
    try:
        answer = cb(form_schema)
    except Exception as exc:  # a form failure must not crash the driving tool
        return {"_error": f"form callback raised: {exc}"}
    if not isinstance(answer, dict):
        return {"_error": f"form callback returned {type(answer).__name__}, "
                          "expected an answer dict"}
    return answer
