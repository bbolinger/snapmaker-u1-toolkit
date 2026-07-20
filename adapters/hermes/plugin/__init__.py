"""u1-form — Hermes plugin: structured multi-field forms for the U1 workflow.

Why a plugin (and not a tool dropped into ``tools/``): platform agents get
a per-toolset allowlist resolved by ``hermes_cli.tools_config
._get_platform_tools``. On a bare-composite config
(``platform_toolsets.telegram: [hermes-telegram]``) built-in toolsets are
enabled by SUBSET-inference against the composite — a runtime-registered
toolset is never a subset, and joining an existing toolset (e.g. clarify)
evicts that toolset entirely. Plugin-provided toolsets take a separate,
first-party path: auto-enabled per platform unless the operator disables
them via ``hermes tools``. So ``form`` registered from a plugin is offered
everywhere clarify is, with zero effect on any built-in toolset.

Delivery pieces:
  * ``register(ctx)``     — Hermes plugin entry point: registers the
    ``form`` tool (own ``form`` toolset) and a ``pre_gateway_dispatch``
    hook that patches ``send_form`` onto the LIVE Telegram adapter class.
  * ``telegram_patch.py`` — the class-level patch (inline-keyboard
    renderer, callback router, answers-file writer).
  * The per-turn form callback published into ``tools.form_gateway``.
    A registered tool cannot reach it any other way: generic dispatch
    hands handlers only (task_id, session_id, user_task), with no callback
    kwarg and no agent reference, so an ``agent.form_callback`` attribute
    alone is unreachable. Two publishers, primary + fallback:
      1. PRIMARY (upgrade-durable): ``_pre_gateway_dispatch`` builds the
         callback from its own context (live adapter + inbound chat_id +
         gateway loop) and publishes it every inbound message. The plugin
         lives on the persistent volume, so this survives a Hermes package
         upgrade that replaces ``gateway/run.py``.
      2. FALLBACK (older Hermes): the ``run.py`` anchor patch applied by
         ``install.py`` publishes the same callback from the gateway's
         per-turn locals. Kept for Hermes builds whose dispatch context is
         thinner than the primary path needs. Last-writer-wins per turn;
         both build an equivalent callback, so they coexist safely.
    Publishing only renders the operator FORM; it is not the print-start
    boundary (that stays in ``u1_print_start_gate.py``, untouched), so
    where the callback is wired has no bearing on model-free confirm.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# Tool core
# =============================================================================

def form_tool(
    form_schema: Dict[str, Any],
    callback: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> str:
    """Present ``form_schema`` to the user; block until they submit.

    Args:
        form_schema: the platform-neutral schema (form-protocol §3 — fields,
                     options, defaults, text_fallback, submit templates).
        callback:    gateway-provided ``(schema) -> answer_dict``. Handles
                     the platform UI + blocks on the user's submit.

    Returns:
        JSON string with the user's answer dict (form-protocol §4 — stable
        option ids for multi_select; option ids for single_select). When the
        schema carries ``submit: {mode: "file", ...}`` the answer dict is a
        write receipt, not answer content — the gateway wrote the answers to
        disk and the model only relays the opaque form id.
    """
    if not isinstance(form_schema, dict):
        return json.dumps({"error": "form_schema must be an object"}, ensure_ascii=False)
    fields = form_schema.get("fields")
    if not isinstance(fields, list) or not fields:
        return json.dumps({"error": "form_schema.fields must be a non-empty list"},
                          ensure_ascii=False)
    if callback is None:
        return json.dumps(
            {"error": "form tool is not available in this execution context "
                      "(no gateway callback wired)."}, ensure_ascii=False)

    try:
        answer = callback(form_schema)
    except Exception as exc:
        return json.dumps({"error": f"form callback failed: {exc}"}, ensure_ascii=False)

    if not isinstance(answer, dict):
        # Defensive: coerce so downstream JSON parse never silently breaks.
        answer = {"_raw": str(answer) if answer is not None else None}

    if answer.get("_cancelled"):
        return json.dumps({"cancelled": True, "user_answer": None}, ensure_ascii=False)

    return json.dumps({
        "schema_version": form_schema.get("version"),
        "fields": [f.get("id") for f in fields],
        "user_answer": answer,
    }, ensure_ascii=False)


import os
import re

_FORM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def _load_persisted_schema(form_id: str) -> Optional[Dict[str, Any]]:
    """Load the schema the workflow persisted for ``form_id``.

    The agent passes ONLY the flat form_id — a 26B local model (gemma4)
    could not reproduce the nested schema in a tool call (template-token
    soup, finish=stop; Ollama issues #15539/#15798/#15943), so the schema
    never rides through the model. Filename-safety: form_id must match the
    same strict pattern the workflow uses, which also blocks traversal."""
    if not _FORM_ID_RE.match(str(form_id or "")):
        return None
    base = os.environ.get("U1_FORM_SCHEMAS_DIR", "").strip() \
        or "/opt/data/snapmaker_u1/form_schemas"
    path = os.path.join(base, f"{form_id}.json")
    try:
        with open(path, "r") as f:
            schema = json.load(f)
        return schema if isinstance(schema, dict) else None
    except Exception:
        return None


def _form_handler(args: Dict[str, Any], **kwargs: Any) -> str:
    """Registry-dispatched handler: resolve the gateway callback by session.

    ``kwargs["session_id"]`` is ``agent.session_id`` (what registry.dispatch
    passes); the run.py patch published the matching callback into
    ``tools.form_gateway`` under that key when it wired the agent's turn.
    """
    callback = None
    try:
        from tools import form_gateway  # type: ignore
        callback = form_gateway.get_form_callback(kwargs.get("session_id") or "")
    except ImportError:
        logger.warning("u1-form: tools.form_gateway not installed — was "
                       "install.py run against this Hermes?")
    schema = args.get("form_schema")
    if not (isinstance(schema, dict) and schema.get("fields")):
        form_id = str(args.get("form_id") or "").strip()
        schema = _load_persisted_schema(form_id)
        if schema is None:
            return json.dumps(
                {"error": f"no pending form found for form_id {form_id!r}. "
                          "Use the form_id from the most recent kit_form "
                          "event; if it expired, re-run the kit workflow "
                          "command to get a fresh form."}, ensure_ascii=False)
    return form_tool(form_schema=schema, callback=callback)


# =============================================================================
# Function-calling tool schema
# =============================================================================

FORM_SCHEMA = {
    "name": "form",
    "description": (
        "Present a STRUCTURED MULTI-FIELD FORM to the user (native button "
        "UI) and block until they submit.\n\n"
        "When a `kit_form` event is emitted, call this tool with ONLY the "
        "event's `form_id` string — the form definition is already stored; "
        "do not reconstruct or restate it. Example: form(form_id=\"f1a2b3c4d5\").\n\n"
        "You get back the user's answers (or a write-receipt when answers "
        "are file-redeemed).\n\n"
        "Do NOT use for single yes/no (use the terminal tool's approval) or "
        "single-pick clarification (use `clarify`)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "form_id": {
                "type": "string",
                "description": (
                    "The `form_id` from the kit_form event. Pass it "
                    "EXACTLY as given. This is the only field you need."
                ),
            },
            "form_schema": {
                "type": "object",
                "description": (
                    "LEGACY / advanced: a full platform-neutral schema "
                    "object. Only pass this when there is no form_id."
                ),
            },
        },
        "required": ["form_id"],
    },
}


# =============================================================================
# Hook: patch the LIVE Telegram adapter class
# =============================================================================
#
# The adapter file can be imported under two module names (the plugin loader
# uses hermes_plugins.platforms__telegram.adapter; a plain import resolves
# plugins.platforms.telegram.adapter as a namespace package) — two SEPARATE
# class objects from the same source. Patching by import can land on the
# copy the gateway never instantiates. So we take type() of the live adapter
# instances from gateway.adapters inside pre_gateway_dispatch: that is, by
# construction, the class the gateway dispatches through — and the hook
# fires before agent dispatch on every inbound message, so send_form exists
# before any form callback can run.

def _acquire_loop():
    """The gateway event loop, captured while we are ON it.

    ``pre_gateway_dispatch`` runs synchronously inside the gateway's async
    dispatch coroutine, so ``get_running_loop()`` returns the loop the
    adapter's ``send_form`` coroutine must be scheduled on. Returns None when
    no loop runs in this thread (e.g. unit tests, or a future Hermes that
    dispatches hooks off-loop) so the caller skips the plugin publish and
    lets the run.py fallback carry the turn."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _make_form_callback(adapter, chat_id, session_key, loop):
    """Build the per-turn form callback the u1_kit / form tool invokes.

    Same contract as the gateway run.py patch's ``_form_callback_sync``, but
    sourced from the plugin's own dispatch context so it survives a Hermes
    upgrade that replaces run.py: render ``form_schema`` via the patched
    adapter's ``send_form``, block on the form_gateway primitive, return the
    answer dict. Fail-soft: every failure path returns an ``{"_error": ...}``
    dict (the driving tool surfaces it) and never raises."""
    def _callback(form_schema):
        import uuid
        from tools import form_gateway as _fmod
        if not hasattr(adapter, "send_form"):
            return {"_error": "active adapter has no send_form (plugin not loaded?)"}
        form_id = uuid.uuid4().hex[:10]
        _fmod.register(form_id, session_key or "", form_schema)
        try:
            adapter.pause_typing_for_chat(chat_id)
        except Exception:
            pass
        try:
            fut = asyncio.run_coroutine_threadsafe(
                adapter.send_form(
                    chat_id=chat_id, form_schema=form_schema,
                    form_id=form_id, session_key=session_key or "",
                    metadata=None,
                ),
                loop,
            )
        except Exception as exc:
            _fmod.clear_session(session_key or "")
            return {"_error": f"form prompt could not be scheduled: {exc}"}
        try:
            send_result = fut.result(timeout=15)
            if not getattr(send_result, "success", False):
                _fmod.clear_session(session_key or "")
                return {"_error": "form prompt send failed"}
        except Exception as exc:
            logger.warning("u1-form: form send failed: %s", exc)
            _fmod.clear_session(session_key or "")
            return {"_error": f"form send exception: {exc}"}
        response = _fmod.wait_for_response(
            form_id, timeout=float(_fmod.get_form_timeout()))
        if response is None:
            return {"_timeout": True}
        return response
    return _callback


def _publish_form_callback(adapter, event, session_store) -> bool:
    """Publish a per-turn form callback into tools.form_gateway from the
    plugin's own dispatch context (the upgrade-durable primary path).

    Best-effort and fail-soft: any missing ingredient (no addressable chat,
    not on the gateway loop, form_gateway absent) just returns False and
    leaves the run.py fallback, where present, to carry the turn. Returns
    True when a callback was published."""
    source = getattr(event, "source", None)
    chat_id = getattr(source, "chat_id", None)
    if source is None or chat_id is None:
        return False  # internal / None event; no chat to address
    loop = _acquire_loop()
    if loop is None:
        return False  # not on the gateway loop; run.py fallback carries it
    try:
        from tools import form_gateway
    except ImportError:
        return False
    session_key = ""
    try:
        gen = getattr(session_store, "_generate_session_key", None)
        if gen is not None:
            session_key = gen(source) or ""
    except Exception:
        # A private-API drift just means we key on "" -> form_gateway's
        # __default__ (latest registration) resolves it for a single
        # operator, exactly as the run.py path already relies on.
        session_key = ""
    form_gateway.set_form_callback(
        session_key, _make_form_callback(adapter, chat_id, session_key, loop))
    return True


def _pre_gateway_dispatch(**kwargs: Any) -> None:
    try:
        from . import telegram_patch
        gateway = kwargs.get("gateway")
        adapters = getattr(gateway, "adapters", None) or {}
        for platform_key, adapter in dict(adapters).items():
            name = str(getattr(platform_key, "value", platform_key)).lower()
            if "telegram" in name:
                if telegram_patch.ensure_patched(type(adapter)):
                    # Register the callback handlers (form + grace-cancel button)
                    # on the LIVE PTB app NOW, every inbound message — not lazily
                    # on the first send_form. The countdown CANCEL button
                    # (u1c:<rid>) rides the same pattern-scoped handler; a reprint
                    # never sends a form, so before this the button had no handler
                    # and taps were silently dropped (live 2026-07-09: a reprint
                    # countdown CANCEL flashed, the grace expired, and the print
                    # started anyway). _ensure_cb_handler is idempotent per-app
                    # and re-registers on reconnect, so calling it per message is
                    # cheap and guarantees the abort button is live before any
                    # countdown.
                    _reg = getattr(adapter, "_u1_ensure_cb_handler", None)
                    if _reg is not None:
                        try:
                            _reg()
                        except Exception:
                            logger.warning("u1-form: proactive callback-handler "
                                           "registration failed", exc_info=True)
                    # PRIMARY form-callback publish (upgrade-durable). Builds
                    # the callback from this dispatch context and publishes it
                    # into form_gateway so the form works even when a Hermes
                    # upgrade has wiped the run.py fallback patch. Fail-soft:
                    # a False return (no chat / off-loop) just leaves the
                    # run.py fallback to carry the turn where it is present.
                    try:
                        _publish_form_callback(
                            adapter, kwargs.get("event"),
                            kwargs.get("session_store"))
                    except Exception:
                        logger.warning("u1-form: form-callback publish failed",
                                       exc_info=True)
    except Exception:
        logger.warning("u1-form: pre_gateway_dispatch patch attempt failed",
                       exc_info=True)
    return None  # never influence message dispatch


# =============================================================================
# Plugin entry point
# =============================================================================

def register(ctx) -> None:
    ctx.register_tool(
        name="form",
        toolset="form",
        schema=FORM_SCHEMA,
        handler=_form_handler,
        description="Structured multi-field form (multi-field sibling of clarify)",
        emoji="📝",
    )
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)

    # u1_kit: the deterministic kit entry point. The model calls this ONCE for a
    # kit zip; the handler runs the workflow, renders the form ITSELF via
    # form_gateway.invoke_form, collects the answer, re-invokes, and returns the
    # readiness card -- the model never emits a mid-flow `form` tool call, so it
    # cannot garble it. Registered on its own toolset (like `form`) so it is
    # actually offered to the model (a bare tools/ drop registers but is never
    # offered -- see this module's header). Lazy-imported so a non-gateway
    # context (e.g. install.py's plugin verification) can still load the plugin.
    try:
        from tools.u1_kit_tool import u1_kit_tool as _kit_run, U1_KIT_SCHEMA

        def _u1_kit_handler(args: Dict[str, Any], **_kw: Any) -> str:
            return _kit_run(model_path=args.get("model_path", ""),
                            request_id=args.get("request_id") or None)

        ctx.register_tool(
            name="u1_kit",
            toolset="u1_kit",
            schema=U1_KIT_SCHEMA,
            handler=_u1_kit_handler,
            description="Slice a multi-part 3D print kit (zip of STLs); renders "
                        "its own operator form deterministically",
            emoji="🖨️",
        )
        logger.info("snapmaker_u1 u1-form plugin: u1_kit tool registered "
                    "(deterministic model-free form)")
    except Exception:
        logger.warning("snapmaker_u1 u1-form plugin: u1_kit tool NOT registered "
                       "(kits fall back to the model-driven form path)",
                       exc_info=True)
