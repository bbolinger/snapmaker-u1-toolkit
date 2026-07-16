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
  * a PATTERN-SCOPED ``CallbackQueryHandler`` registered directly on the
    adapter's live PTB Application (negative group, raises
    ``ApplicationHandlerStop``) — taps on form buttons never reach Hermes'
    native dispatcher, everything else is untouched.

Why registration instead of swapping ``_handle_callback_query`` on the
class: the adapter runs ``add_handler(CallbackQueryHandler(
self._handle_callback_query))`` at ``connect()`` — that bound method
captured the ORIGINAL function, so a later class-attribute swap is
invisible to PTB. Live v2.2 finding (2026-07-02): with the swap approach
every button press routed to the native handler, toggles never rendered,
and the form sat untouched until its 600s timeout. The form callback
vocabulary is exclusively single-char prefixes (``t: s: p: a: z: n: e:
g: S X``) while Hermes' own callbacks are all multi-char + colon (``cl:``,
``ea:``, ``mp:``…), so the pattern can never shadow a native callback.

File handoff: when the schema carries ``submit: {mode: "file", form_id}``,
the GATEWAY writes ``<U1_FORM_ANSWERS_DIR>/<form_id>.json`` on Submit and
resolves the agent thread with a receipt only — answer content never rides
through the model in either direction.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# The renderer's complete callback vocabulary — and nothing else. Anchored so
# a native Hermes callback (multi-char prefix + colon) can never match.
# S:<f>:<o> is a submit-verb (sets the Action option, then submits).
# g:<sub> is the Advanced-options menu (g:m menu, g:d done, g:r reset,
# g:c:<category> open a category) — a SINGLE-char prefix, so it stays inside
# the form vocabulary and never shadows a native multi-char callback.
FORM_CB_PATTERN = r"^(?:[tsp]:\d+:\d+|S:\d+:\d+|[azne]:\d+|g:(?:[mdr]|c:[a-z_]+)|[SX])$"

# Grace-window cancel button on the countdown DM (u1c:<request_id>). Handled
# HERE — the gateway adapter layer — because a typed CANCEL that lands while
# the agent's turn is still streaming gets injected mid-turn and never
# reaches the dispatch hooks (lost live twice, 2026-07-07). Button callbacks
# ride PTB's handler queue instead, which no agent turn can swallow.
CANCEL_CB_PATTERN = r"^u1c:u1_[a-z0-9_]{4,40}$"


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


# TEMPORARY v2.4.1 upgrade shim - remove in v2.5 (see the cancel handler).
_U1_LEGACY_CANCEL_DIR = "/tmp/u1_pending_cancel"


def _u1_pending_dir(kind):
    """KEEP IN SYNC with scripts/u1_pending.py (canonical copy + rationale).
    This file deploys standalone as the u1-form plugin, so the ~10-line
    rule is duplicated; test_pending_paths.py asserts identity."""
    import os as _os
    import tempfile as _tempfile
    from pathlib import Path as _P
    explicit = _os.environ.get(f"U1_PENDING_{kind.upper()}_DIR", "").strip()
    if explicit:
        return _P(explicit)
    root = _os.environ.get("U1_PENDING_STATE_DIR", "").strip()
    if root:
        return _P(root) / kind
    return _P(_tempfile.gettempdir()) / "u1_pending" / kind


async def _u1_handle_cancel_callback(adapter, update, ctx):
    """Touch the gate's cancel marker for the tapped request. Model-free:
    pure file touch, same contract as the u1_grace_cancel message hook. The
    gate polls the marker once per second and refuses the start."""
    import json as _json
    from datetime import datetime, timezone
    from pathlib import Path as _P
    q = update.callback_query
    rid = (q.data or "").split(":", 1)[-1]
    pending = _u1_pending_dir("cancel") / f"{rid}.json"
    # TEMPORARY v2.4.1 upgrade shim - remove in v2.5: a pre-v2.4.1 notify
    # script writes the routing entry to the old literal location; the
    # button must not go dead across a partial upgrade.
    if not pending.exists():
        legacy = _P(_U1_LEGACY_CANCEL_DIR) / f"{rid}.json"
        if legacy.exists():
            pending = legacy
    touched = False
    try:
        st = _json.loads(pending.read_text())
        fresh = True
        exp = st.get("expires_at")
        if exp:
            try:
                fresh = (datetime.now(timezone.utc)
                         <= datetime.fromisoformat(exp.replace("Z", "+00:00")))
            except ValueError:
                pass
        marker = st.get("cancel_marker")
        if fresh and marker:
            _P(marker).parent.mkdir(parents=True, exist_ok=True)
            _P(marker).write_text("cancel via telegram button")
            touched = True
    except FileNotFoundError:
        pass
    except Exception:
        logger.exception("u1-form: cancel button failed for %s", rid)
    if touched:
        await q.answer("Cancelling — nothing will be sent to the printer.")
        try:
            await q.edit_message_text(
                (getattr(q.message, "text", "") or "")
                + "\n\n🛑 CANCELLED — nothing was sent to the printer.")
        except Exception:
            pass
        logger.info("u1-form: grace cancel button touched marker for %s", rid)
    else:
        await q.answer("No active grace window for this print (already "
                       "started, cancelled, or expired).", show_alert=True)


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

    def _ensure_cb_handler(self) -> None:
        """Register the form callback handler on the LIVE PTB Application.

        Idempotent per Application object: a reconnect builds a fresh
        ``self._app`` (losing our handler with the old one), so the flag
        lives on the app, not the adapter — the next send_form re-registers.
        Negative group + ``ApplicationHandlerStop`` keep matched taps away
        from Hermes' native dispatcher without touching its registration.
        """
        app = getattr(self, "_app", None)
        if app is None:
            logger.warning("u1-form: adapter has no _app — taps cannot be "
                           "routed; form will time out. (hook point moved?)")
            return
        # Idempotence flag lives on the ADAPTER (plain __dict__), keyed by
        # app identity — PTB's Application is __slots__-ed, so setting an
        # attribute on it raises (live 2026-07-02: form send exception
        # "'Application' object has no attribute ... no __dict__"). The
        # adapter already carries our per-instance form state, and a
        # reconnect swaps self._app to a fresh object, failing the identity
        # check and re-registering on the new app.
        if getattr(self, "_u1_form_cb_app", None) is app:
            return
        from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler  # type: ignore

        async def _entry(update, ctx, _self=self):
            await _u1_handle_form_callback(_self, update, ctx)
            raise ApplicationHandlerStop

        handler = CallbackQueryHandler(_entry, pattern=FORM_CB_PATTERN)
        app.add_handler(handler, group=-11)

        async def _cancel_entry(update, ctx, _self=self):
            await _u1_handle_cancel_callback(_self, update, ctx)
            raise ApplicationHandlerStop

        app.add_handler(CallbackQueryHandler(_cancel_entry,
                                             pattern=CANCEL_CB_PATTERN),
                        group=-11)
        self._u1_form_cb_app = app
        logger.info("u1-form: callback handlers registered on live PTB app "
                    "(group -11, pattern-scoped: form + grace-cancel button).")

    async def _send_form(self, chat_id, form_schema, form_id, session_key, metadata=None):
        """Render a form_schema as a sequence of inline-keyboard screens.

        Stores per-form state under self._u1_form_state[form_id]. Operator taps
        edit the message in place; on Submit we call resolve_gateway_form to
        unblock the agent thread waiting on this form_id.
        """
        from telegram.constants import ParseMode  # type: ignore
        _ensure_cb_handler(self)  # before send: no tap may beat the handler
        # Send the header image (STL thumbnail grid) FIRST so the operator sees
        # the pieces while picking — independent of the agent surfacing it.
        header_image = (form_schema or {}).get("header_image")
        if header_image:
            try:
                import os as _os
                if _os.path.isfile(header_image):
                    with open(header_image, "rb") as _img:
                        await self._bot.send_photo(chat_id=int(chat_id), photo=_img)
            except Exception as exc:
                logger.warning("u1-form: header image send failed: %s", exc)
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
            "last_text": screen["text"],
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

        def _find_slot():
            return next((s for s in st.values()
                         if s["chat_id"] == msg.chat_id
                         and s["msg_id"] == msg.message_id), None)

        slot = _find_slot()
        if slot is None:
            # First-render race: Telegram makes the message tappable the instant
            # it is delivered, but _send_form records the form slot just AFTER the
            # send call returns. A fast tap in that window would otherwise flash
            # "Stale form" with no check (live 2026-07-13: the operator's first
            # taps on a fresh form didn't register). Briefly retry so the tap
            # lands once the slot registers a fraction of a second later; a
            # genuinely stale form never appears and still falls through.
            import asyncio as _asyncio
            for _ in range(10):
                await _asyncio.sleep(0.1)
                slot = _find_slot()
                if slot is not None:
                    break
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
            markup = _rows_to_markup(screen["keyboard"])
            if text == slot.get("last_text"):
                # Only the SELECTION changed, not the screen text (a radio/
                # multi tap within the current screen). Edit JUST the keyboard
                # — a full edit_message_text re-sends all the HTML text too,
                # which Telegram throttles on rapid taps and makes the
                # selection dot visibly lag behind the tap. Keyboard-only
                # edits are a fraction of the payload and snap immediately.
                await q.edit_message_reply_markup(reply_markup=markup)
            else:
                await q.edit_message_text(text, parse_mode=ParseMode.HTML,
                                          reply_markup=markup)
                slot["last_text"] = text
        except Exception as exc:
            # "message is not modified" (identical tap) is benign; others log.
            if "not modified" not in str(exc).lower():
                logger.warning("u1-form: rerender edit failed: %s", exc)

    # NOTE deliberately NOT touching adapter_cls._handle_callback_query —
    # PTB holds the bound method it captured at connect(); replacing the
    # class attribute after that is a silent no-op (the v2.2 live bug).
    # Routing happens via _ensure_cb_handler's pattern-scoped registration.
    adapter_cls.send_form = _send_form
    adapter_cls._u1_handle_form_callback = _u1_handle_form_callback
    adapter_cls._u1_ensure_cb_handler = _ensure_cb_handler
    adapter_cls._u1_form_patched = True
    logger.info("u1-form: %s.send_form installed on the live adapter class "
                "(no source edits).", adapter_cls.__name__)
    return True
