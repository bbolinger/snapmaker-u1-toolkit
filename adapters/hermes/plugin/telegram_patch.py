"""Class-level Telegram form patch — applied to the LIVE adapter class.

``ensure_patched(cls)`` receives the adapter class from ``type()`` of a
running gateway adapter instance (see ``__init__._pre_gateway_dispatch``).
It never resolves the class by module name: the same adapter source file
can be loaded under two module names (``hermes_plugins.platforms__telegram
.adapter`` via the plugin loader vs ``plugins.platforms.telegram.adapter``
as a namespace-package import), producing two distinct class objects — an
import-side patch can land on the copy the gateway never instantiates.

What the patch adds (no Hermes source edits):
  * ``send_form``                — renders a form_schema as inline-keyboard
    screens via the vendored L1 renderer (``u1_form_telegram``).
  * ``_u1_handle_form_callback`` — applies taps to the form state machine,
    edits the message in place, and on Submit resolves the agent thread
    blocked in ``tools.form_gateway``.
  * a wrapped ``_handle_callback_query`` — routes callbacks that belong to
    a form we own (matched by chat_id + message_id, not just prefix) to the
    form handler; everything else falls through to Hermes' original
    (clarify ``cl:``, exec-approval ``ea:``, model picker ``mp:``…).

File handoff: when the schema carries ``submit: {mode: "file", form_id}``,
the GATEWAY writes ``<U1_FORM_ANSWERS_DIR>/<form_id>.json`` on Submit and
resolves the agent thread with a receipt only — answer content never rides
through the model in either direction.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _load_renderer():
    """Import the vendored L1 renderer (copied into this plugin dir by
    install.py). Relative import when loaded as a package (the Hermes
    plugin loader), path-based fallback otherwise (tests, manual runs)."""
    try:
        from . import u1_form_telegram  # type: ignore
        return u1_form_telegram
    except ImportError:
        import sys
        from pathlib import Path
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import u1_form_telegram  # type: ignore
        return u1_form_telegram


def _make_send_result(**kwargs):
    """Build Hermes' SendResult if importable, else a duck-typed stand-in.

    ``gateway/platforms/base.py`` exists in 0.17 and 0.18; if it ever moves
    the send path degrades to an attribute-compatible namespace instead of
    raising inside the gateway's event loop.
    """
    try:
        from gateway.platforms.base import SendResult  # type: ignore
    except ImportError:
        from types import SimpleNamespace
        return SimpleNamespace(success=kwargs.get("success", False),
                               message_id=kwargs.get("message_id"),
                               error=kwargs.get("error"))
    return SendResult(**kwargs)


def write_answers_file(form_id, answers):
    """v2.2 file handoff: persist the collected answers where the workflow's
    ``--form-answers-from`` can redeem them. The GATEWAY writes this — the
    model never carries answer content. Mirrors ``u1_form.write_answers_file``
    (kept self-contained on purpose: the plugin must not import the toolkit).
    """
    import json as _json
    import os
    import re as _re
    if not _re.match(r"^[A-Za-z0-9_-]{6,64}$", str(form_id or "")):
        raise ValueError(f"invalid form_id: {form_id!r}")
    base = os.environ.get("U1_FORM_ANSWERS_DIR", "").strip() \
        or "/opt/data/snapmaker_u1/form_answers"
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, f"{form_id}.json")
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        _json.dump(answers, fh, indent=2)
    os.replace(tmp, path)
    return path


def ensure_patched(adapter_cls) -> bool:
    """Idempotently install form support on ``adapter_cls``.

    Returns True when the class carries the patch on exit (already patched
    or patched now); False when the patch could not apply (missing hook
    point / renderer) — logged loudly, adapter left untouched.
    """
    if adapter_cls is None:
        return False
    if getattr(adapter_cls, "_u1_form_patched", False):
        return True

    _orig_cb = getattr(adapter_cls, "_handle_callback_query", None)
    if _orig_cb is None:
        logger.warning("u1-form: %s has no _handle_callback_query — the "
                       "callback hook point moved in this hermes-agent "
                       "build; form tool will only work via text fallback.",
                       adapter_cls.__name__)
        return False
    try:
        _tg = _load_renderer()
    except Exception as exc:
        logger.warning("u1-form: L1 renderer import failed (%s); form tool "
                       "will only work via text fallback.", exc)
        return False

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
            logger.warning("u1-form: send failed: %s", exc)
            return _make_send_result(success=False, error=str(exc))
        _form_state(self)[form_id] = {
            "form": form, "schema": form_schema,
            "session_key": session_key, "msg_id": msg.message_id,
            "chat_id": int(chat_id),
        }
        return _make_send_result(success=True, message_id=str(msg.message_id))

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
                    submit_spec = (slot.get("schema") or {}).get("submit") or {}
                    payload = ev["answer"]
                    if submit_spec.get("mode") == "file":
                        # v2.2 handoff: answers go to DISK for the workflow
                        # to redeem via --form-answers-from; the agent
                        # thread gets a receipt only — no answer content
                        # rides back through the model.
                        target_id = submit_spec.get("form_id") or form_id
                        path = write_answers_file(target_id, ev["answer"])
                        payload = {"_answers_file_written": True,
                                   "form_id": target_id, "path": path}
                    # False ⇒ the gateway entry is gone (agent's wait timed
                    # out and popped it) — nothing will run, so don't claim
                    # success. Drop the stale slot either way.
                    resolved = bool(_fmod.resolve_gateway_form(form_id, payload))
                    st.pop(form_id, None)
            except Exception as exc:
                logger.warning("u1-form: resolve failed: %s", exc)
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
            logger.warning("u1-form: rerender edit failed: %s", exc)

    # Wrap the existing callback dispatcher: form callbacks routed first,
    # everything else falls through to the original handler.
    _FORM_PREFIXES = {"t", "s", "a", "z", "n", "p", "e", "S", "X"}

    def _looks_like_form_cb(data: str) -> bool:
        if not data:
            return False
        head = data.split(":", 1)[0]
        return head in _FORM_PREFIXES and (":" in data or data in ("S", "X"))

    async def _wrapped_cb(self, update, ctx):
        data = (update.callback_query.data if update and update.callback_query else "") or ""
        # Only treat as form callback when (chat_id, message_id) matches a form
        # slot we own — keeps us from snatching callbacks Hermes' OWN handlers
        # use (their prefixes are all multi-char + colon: cl:, ea:, mp:, gt:…),
        # and keeps concurrent forms in different chats apart (Telegram
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

    adapter_cls.send_form = _send_form
    adapter_cls._u1_handle_form_callback = _u1_handle_form_callback
    adapter_cls._handle_callback_query = _wrapped_cb
    adapter_cls._u1_form_patched = True
    logger.info("u1-form: %s.send_form installed on the live adapter class "
                "(no source edits).", adapter_cls.__name__)
    return True
