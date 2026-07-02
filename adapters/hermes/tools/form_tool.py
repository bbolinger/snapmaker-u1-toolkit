#!/usr/bin/env python3
"""
Form Tool — present a structured multi-field form to the user.

Companion to ``clarify`` (which is single-question / max-4-choice). Used when
the agent needs to collect SEVERAL decisions at once that don't fit clarify's
shape: a printer kit form with parts (multi-select), tool (single-select),
material, profile (16+ options), supports, action.

The actual UI lives in the platform layer:
  * Telegram adapter renders step-by-step inline keyboards (via the L1
    renderer at ``u1_form_telegram``), with a review card before submit.
  * Other adapters can fall back to ``schema["text_fallback"]`` and accept
    a typed line — the toolkit's own ``parse_answers`` parses it.

This module defines the LLM-facing schema + a thin dispatcher that delegates
to a platform-provided callback (injected by ``gateway/run.py`` as
``agent.form_callback``).
"""

import json
from typing import Any, Callable, Dict, Optional


def form_tool(
    form_schema: Dict[str, Any],
    callback: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> str:
    """Present ``form_schema`` to the user; block until they submit.

    Args:
        form_schema: the platform-neutral schema (form-protocol §3 — fields,
                     options, defaults, text_fallback, submit templates).
        callback:    platform-provided ``(schema) -> answer_dict`` injected
                     by the agent runner (gateway/run.py). The callback
                     handles the platform UI + blocks on the user's submit.

    Returns:
        JSON string with the user's answer dict (form-protocol §4 — stable
        option ids for multi_select; option ids for single_select).
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


def check_form_requirements() -> bool:
    """Form tool has no external requirements — always available."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

FORM_SCHEMA = {
    "name": "form",
    "description": (
        "Present a STRUCTURED MULTI-FIELD FORM to the user and block until "
        "they submit. Use when you need several decisions at once that "
        "don't fit `clarify` (which is single-question, max-4-choice).\n\n"
        "Pass a `form_schema` object (the platform-neutral spec the toolkit "
        "emits in its `kit_form` event): a list of typed fields "
        "(`single_select`, `multi_select`), each with an `id`, `label`, "
        "`options` (stable ids), optional `default`, optional `required`. "
        "Include `text_fallback` so platforms without rich UI degrade "
        "gracefully.\n\n"
        "The user sees native UI on platforms that support it (Telegram "
        "inline keyboards, Discord select menus) or a typed-line form on "
        "ones that don't. You get back the canonical answer dict — keyed by "
        "field id, values are stable option ids (or `'all'` for fully-"
        "selected multi).\n\n"
        "Use this tool when:\n"
        "- The flow has several related decisions the user should review "
        "together (kit slicing options, multi-step config).\n"
        "- A `kit_form` event has been emitted with a `form_schema` field — "
        "pass that schema directly.\n\n"
        "Do NOT use for single yes/no (use the terminal tool's approval) or "
        "single-pick clarification (use `clarify`)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "form_schema": {
                "type": "object",
                "description": (
                    "The platform-neutral form schema. Pass the schema "
                    "VERBATIM from a `kit_form` event's `form_schema` field. "
                    "Do not invent or rewrite it."
                ),
            },
        },
        "required": ["form_schema"],
    },
}


# --- Registry ---
from tools.registry import registry  # type: ignore

registry.register(
    name="form",
    toolset="form",
    schema=FORM_SCHEMA,
    handler=lambda args, **kw: form_tool(
        form_schema=args.get("form_schema") or {},
        callback=kw.get("callback")),
    check_fn=check_form_requirements,
    emoji="📝",
)


# =============================================================================
# Telegram class-level monkey-patch: install send_form + callback router
# =============================================================================
#
# Hermes auto-imports tools/* at agent init. When THIS module is imported we
# (a) register the LLM-facing `form` tool above, then (b) patch the
# TelegramPlatform CLASS so any instance the gateway creates already has
# `send_form` and a form-aware callback dispatcher. No edits to Hermes source.
#
# The dispatcher wraps the existing `_handle_callback_query`: if the
# callback_data prefix belongs to our L1 renderer (t/s/a/z/n/p/e/S/X), we
# route to `_u1_handle_form_callback`; otherwise we fall through to the
# original (clarify, exec-approval, model picker, slash confirm).
#
# Failure mode is loud: if the patch can't apply (Hermes class moved, import
# error), we log a warning and skip — `form` tool still works as text fallback.

import logging as _logging

_logger = _logging.getLogger(__name__)


def _install_telegram_form_patch() -> None:
    try:
        from gateway.platforms.telegram import TelegramPlatform  # type: ignore
    except ImportError as exc:
        _logger.warning("u1 form patch: TelegramPlatform import failed (%s); "
                        "form tool will only work via text fallback.", exc)
        return
    if getattr(TelegramPlatform, "_u1_form_patched", False):
        return  # idempotent — already patched (re-import safe)
    try:
        import sys
        # Make our vendored L1 renderer importable (lives next to this file).
        _here = __file__
        from pathlib import Path as _P
        _tools_dir = str(_P(_here).resolve().parent)
        if _tools_dir not in sys.path:
            sys.path.insert(0, _tools_dir)
        import u1_form_telegram as _tg  # type: ignore
    except Exception as exc:
        _logger.warning("u1 form patch: L1 renderer import failed (%s); "
                        "form tool will only work via text fallback.", exc)
        return

    # Per-instance state attached lazily. Keyed by form_id (uuid from gateway).
    def _form_state(self):
        if not hasattr(self, "_u1_form_state"):
            self._u1_form_state = {}
        return self._u1_form_state

    def _rows_to_markup(rows):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup  # type: ignore
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(b["text"], callback_data=b["callback_data"]) for b in row]
            for row in rows
        ])

    async def _send_form(self, chat_id, form_schema, form_id, session_key, metadata=None):
        """Render a form_schema as a sequence of inline-keyboard screens.

        Stores per-form state under self._u1_form_state[form_id]. Operator taps
        edit the message in place; on Submit we call resolve_gateway_form to
        unblock the agent thread waiting on this form_id.
        """
        from telegram.constants import ParseMode  # type: ignore
        form = _tg.new_form(form_schema)
        screen = _tg.render_screen(form)
        kwargs = {
            "chat_id": int(chat_id),
            "text": screen["text"],
            "parse_mode": ParseMode.HTML,
            "reply_markup": _rows_to_markup(screen["keyboard"]),
        }
        if hasattr(self, "_thread_kwargs_for_send"):
            try:
                kwargs.update(self._thread_kwargs_for_send(chat_id, None, metadata,
                                                          reply_to_message_id=None))
            except Exception:
                pass
        try:
            msg = await self._send_message_with_thread_fallback(**kwargs) \
                if hasattr(self, "_send_message_with_thread_fallback") \
                else await self._bot.send_message(**kwargs)
        except Exception as exc:
            _logger.warning("u1 form: send failed: %s", exc)
            from gateway.platforms.base import SendResult  # type: ignore
            return SendResult(success=False, error=str(exc))
        _form_state(self)[form_id] = {
            "form": form, "schema": form_schema,
            "session_key": session_key, "msg_id": msg.message_id,
            "chat_id": int(chat_id),
        }
        from gateway.platforms.base import SendResult  # type: ignore
        return SendResult(success=True, message_id=str(msg.message_id))

    async def _u1_handle_form_callback(self, update, ctx) -> None:
        q = update.callback_query
        data = q.data or ""
        msg = q.message
        if msg is None:  # old/inaccessible callback — Telegram may omit message
            await q.answer("Stale form")
            return
        # Find the form by (chat_id, message_id). Telegram message_ids are
        # per-chat counters, so message_id alone can collide across two
        # concurrent forms in different chats.
        st = _form_state(self)
        slot = next((s for s in st.values()
                     if s["chat_id"] == msg.chat_id and s["msg_id"] == msg.message_id),
                    None)
        if slot is None:
            await q.answer("Stale form")
            return
        await q.answer()
        ev = _tg.apply_callback(slot["form"], data)
        kind = ev["kind"]
        if kind == "cancel":
            try:
                from tools import form_gateway as _fmod  # type: ignore
                form_id = next((fid for fid, s in st.items() if s is slot), None)
                if form_id:
                    _fmod.cancel_gateway_form(form_id)
                    st.pop(form_id, None)
            except Exception:
                pass
            try:
                await q.edit_message_text("Form cancelled.")
            except Exception:
                pass
            return
        if kind == "submit":
            resolved = False
            try:
                from tools import form_gateway as _fmod  # type: ignore
                form_id = next((fid for fid, s in st.items() if s is slot), None)
                if form_id:
                    # False ⇒ the gateway entry is gone (agent's wait timed
                    # out and popped it) — nothing will run, so don't claim
                    # success. Drop the stale slot either way.
                    resolved = bool(_fmod.resolve_gateway_form(form_id, ev["answer"]))
                    st.pop(form_id, None)
            except Exception as exc:
                _logger.warning("u1 form: resolve failed: %s", exc)
            try:
                if resolved:
                    await q.edit_message_text("✅ Submitted — slicing in the background.")
                else:
                    await q.edit_message_text(
                        "⚠ This form expired — the agent stopped waiting for it. "
                        "Nothing was submitted; please re-send the request.")
            except Exception:
                pass
            return
        # rerender
        screen = _tg.render_screen(slot["form"])
        warning = ev.get("warning")
        text = screen["text"] + (f"\n\n⚠ {warning}" if warning else "")
        try:
            from telegram.constants import ParseMode  # type: ignore
            await q.edit_message_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=_rows_to_markup(screen["keyboard"]))
        except Exception as exc:
            _logger.warning("u1 form: rerender edit failed: %s", exc)

    # Wrap the existing callback dispatcher: form callbacks routed first,
    # everything else falls through to the original handler.
    _FORM_PREFIXES = {"t", "s", "a", "z", "n", "p", "e", "S", "X"}

    def _looks_like_form_cb(data: str) -> bool:
        if not data:
            return False
        head = data.split(":", 1)[0]
        return head in _FORM_PREFIXES and (":" in data or data in ("S", "X"))

    _orig_cb = TelegramPlatform._handle_callback_query

    async def _wrapped_cb(self, update, ctx):
        data = (update.callback_query.data if update and update.callback_query else "") or ""
        # Only treat as form callback when (chat_id, message_id) matches a form
        # slot we own — keeps us from snatching the four chars Hermes' OWN
        # handlers might also use (defensive; Hermes uses prefixes like "cl:" /
        # "ea:"), and keeps concurrent forms in different chats apart (Telegram
        # message_ids are only unique per chat). `message` can be None on old
        # callbacks — guard before dereferencing.
        st = _form_state(self)
        msg = update.callback_query.message if update and update.callback_query else None
        owns = msg is not None and any(
            s["chat_id"] == msg.chat_id and s["msg_id"] == msg.message_id
            for s in st.values()
        )
        if _looks_like_form_cb(data) and owns:
            return await _u1_handle_form_callback(self, update, ctx)
        return await _orig_cb(self, update, ctx)

    TelegramPlatform.send_form = _send_form
    TelegramPlatform._u1_handle_form_callback = _u1_handle_form_callback
    TelegramPlatform._handle_callback_query = _wrapped_cb
    TelegramPlatform._u1_form_patched = True
    _logger.info("u1 form patch: TelegramPlatform.send_form installed (no source edits).")


_install_telegram_form_patch()
