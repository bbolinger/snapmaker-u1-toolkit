"""Telegram sidecar bot — Level-3 escape hatch.

Runs a SEPARATE Telegram bot (its own token) that renders the kit form as
inline-keyboard screens (via the L1 pure renderer in ``u1_form_telegram.py``)
and submits the answer back to ``u1_kit_workflow.py`` via subprocess. Use this
when your main agent runtime (Hermes / OpenClaw / a custom bot) cannot be
modified to consume ``form_schema`` directly.

**This is the escape hatch, not the primary path.** The clean answer is L2:
the host agent's Telegram adapter renders the form natively (see the Hermes
upstream PR draft / FORM-PROTOCOL §5). Two bots in the same chat is real UX
tax; document it for the operator.

How the operator gets here:
  1. Hermes/Gemma posts a tappable Telegram deep link in your chat:
       https://t.me/<sidecar_bot_username>?start=<request_id>
  2. Operator taps; Telegram opens a DM with the sidecar bot and sends
     ``/start <request_id>`` automatically.
  3. Sidecar loads the request, rebuilds the form schema, walks the operator
     through the step-by-step screens (parts → orient → tool → … → review).
  4. On Submit → sidecar runs ``u1_kit_workflow.py --request-id <id>
     --form-answers-json '<json>'`` as a subprocess. The same downstream
     safety pipeline as the typed path runs.
  5. Sidecar reports the outcome and tells the operator to return to their
     main Hermes chat for the Stage-1 bed-photo gate.

Setup (one-time):
  1. Create a second bot in @BotFather: ``/newbot`` → pick a name → get token.
     Save the token (env var ``U1_SIDECAR_BOT_TOKEN``).
  2. Note the bot's username (env var ``U1_SIDECAR_BOT_USERNAME`` —
     ``u1_kit_workflow`` uses this to build the deep link in the form event).
  3. ``pip install -r adapters/telegram/requirements.txt``
  4. Run: ``U1_SIDECAR_BOT_TOKEN=… python3 adapters/telegram/sidecar.py``
  5. Restrict who can use it via ``U1_SIDECAR_ALLOWLIST`` (CSV of Telegram
     user ids). Without an allowlist, the bot refuses everyone — fail closed.

Requires: ``python-telegram-bot>=21`` (lazy import; pure renderer is dep-free).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# u1_form_telegram is the pure L1 renderer, dep-free. We import it here, not
# the python-telegram-bot SDK (lazy in main()).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import u1_form_telegram as tg


# Toolkit scripts dir — sidecar shells out to u1_kit_workflow.py here.
# Default mirrors the runtime layout; override with U1_SCRIPTS_DIR if needed.
SCRIPTS_DIR = Path(os.environ.get("U1_SCRIPTS_DIR", "/opt/data/scripts"))
KIT_WORKFLOW = SCRIPTS_DIR / "u1_kit_workflow.py"

# Data dir — where requests/<id>/request.json lives. Mirrors the workflow's
# default (u1_config.get_data_dir behavior) but kept explicit here so the
# sidecar is a single-file standalone.
DATA_DIR = Path(os.environ.get("SNAPMAKER_U1_DATA_DIR",
                                os.environ.get("U1_DATA_DIR", "/opt/data/snapmaker_u1")))


logger = logging.getLogger("u1_sidecar")


# --------------------------------------------------------------------------- #
# Schema reconstruction from the persisted request
# --------------------------------------------------------------------------- #

DEFAULT_TOOLS = ["T0", "T1", "T2", "T3"]
DEFAULT_MATERIALS = ["PLA", "PETG", "ABS", "TPU", "ASA", "PLA-CF", "PETG-CF"]
DEFAULT_SUPPORTS = ["supports", "no-supports", "overhangs"]
DEFAULT_ACTIONS = ["start", "upload-only"]


def load_request(request_id: str) -> dict[str, Any] | None:
    """Read ``requests/<id>/request.json`` from the data dir, or None."""
    p = DATA_DIR / "requests" / request_id / "request.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("load_request(%s) failed: %s", request_id, exc)
        return None


def rebuild_schema(req: dict[str, Any]) -> dict[str, Any] | None:
    """Reconstruct the form_schema from a persisted request.

    The kit workflow persists ``kit.parts[]`` + ``form_profiles`` (the
    index-stable profile list captured at form-emit). Tools / materials /
    supports / actions are static defaults that match the workflow's emission.
    """
    kit = req.get("kit") or {}
    parts_raw = kit.get("parts") or []
    profiles = req.get("form_profiles") or []
    if not profiles:
        return None  # form was never emitted for this request

    spec = {
        "parts": [{"id": p["part_id"], "label":
                   f"{p['filename']} ({p['footprint_mm'][0]:.0f}x{p['footprint_mm'][1]:.0f}mm)"}
                  for p in parts_raw],
        "tools": DEFAULT_TOOLS,
        "materials": DEFAULT_MATERIALS,
        "profiles": [{"idx": int(p["idx"]), "label": p["label"]} for p in profiles],
        "supports": DEFAULT_SUPPORTS,
        "actions": DEFAULT_ACTIONS,
    }
    # Re-use the toolkit's own schema builder for parity with the workflow.
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import u1_form  # type: ignore
        return u1_form.build_form_schema(spec)
    except ImportError:
        # Standalone fallback: don't ship u1_form-equivalent here; the sidecar
        # requires the toolkit on PYTHONPATH/SCRIPTS_DIR. Log + bail.
        logger.error("u1_form not importable from %s; set U1_SCRIPTS_DIR", SCRIPTS_DIR)
        return None


# --------------------------------------------------------------------------- #
# Submit — shell out to the kit workflow, same downstream safety as typed path
# --------------------------------------------------------------------------- #

def submit_answer(request_id: str, answer: dict[str, Any], archive: str) -> dict[str, Any]:
    """Invoke ``u1_kit_workflow.py --request-id … --form-answers-json '<json>'``.

    Returns a small result dict for the operator: {ok, phase, message, plate_count}.
    """
    cmd = [
        sys.executable, str(KIT_WORKFLOW), archive,
        "--json-events", "--request-id", request_id,
        "--form-answers-json", json.dumps(answer),
    ]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=900)
    except subprocess.TimeoutExpired:
        return {"ok": False, "phase": "timeout", "message": "Slice timed out (>15m)."}
    if proc.returncode != 0:
        return {"ok": False, "phase": "error",
                "message": f"Workflow rc={proc.returncode}\n{proc.stderr[-2000:]}"}
    # Parse the LAST JSON line of stdout (the workflow's return dict).
    last: dict[str, Any] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            last = json.loads(line)
        except json.JSONDecodeError:
            continue
    if not last:
        return {"ok": False, "phase": "no-output", "message": proc.stdout[-2000:]}
    return {
        "ok": True,
        "phase": last.get("phase"),
        "plate_count": last.get("plate_count"),
        "gated_plate": last.get("gated_plate"),
        "message": ("Submitted. Return to your main Hermes chat — the Stage-1 "
                    "bed-photo approval happens there."),
    }


# --------------------------------------------------------------------------- #
# Telegram runtime (lazy SDK import) — wires the L1 pure renderer to a real bot
# --------------------------------------------------------------------------- #

def _is_allowed(user_id: int) -> bool:
    """Allowlist check — fail-closed. Set U1_SIDECAR_ALLOWLIST=csv to allow."""
    allow = os.environ.get("U1_SIDECAR_ALLOWLIST", "").strip()
    if not allow:
        return False
    try:
        return user_id in {int(x.strip()) for x in allow.split(",") if x.strip()}
    except ValueError:
        return False


def _rows_to_markup(rows, InlineKeyboardButton, InlineKeyboardMarkup):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(b["text"], callback_data=b["callback_data"]) for b in row]
        for row in rows
    ])


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - live bot
    """Entry point. Requires ``U1_SIDECAR_BOT_TOKEN``."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("U1_SIDECAR_BOT_TOKEN")
    if not token:
        print("ERROR: set U1_SIDECAR_BOT_TOKEN (get a bot token from @BotFather).",
              file=sys.stderr)
        return 2
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
        from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                                  ContextTypes)
    except ImportError as exc:
        print(f"ERROR: python-telegram-bot not installed: {exc}\n"
              "  pip install -r adapters/telegram/requirements.txt", file=sys.stderr)
        return 2

    # Per-user form state: {user_id: {"form": ..., "request_id": ..., "archive": ..., "msg_id": ...}}
    state: dict[int, dict[str, Any]] = {}

    async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not _is_allowed(user.id):
            await update.message.reply_text(
                "Not allowed. The toolkit operator must add your Telegram user id to "
                "U1_SIDECAR_ALLOWLIST. Your id: " + str(user.id if user else "?"))
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text(
                "Usage: open via the deep link your Hermes chat posted, e.g.\n"
                "https://t.me/<this_bot>?start=<request_id>")
            return
        request_id = args[0].strip()
        req = load_request(request_id)
        if not req:
            await update.message.reply_text(
                f"No request {request_id!r} found on disk. Has the form been emitted yet?")
            return
        schema = rebuild_schema(req)
        if not schema:
            await update.message.reply_text(
                f"Request {request_id} has no persisted form_profiles — was it never "
                "in awaiting_form?")
            return
        archive = req.get("model_path")
        if not archive:
            await update.message.reply_text(f"Request {request_id} has no model_path.")
            return
        form = tg.new_form(schema)
        screen = tg.render_screen(form)
        msg = await update.message.reply_text(
            screen["text"], parse_mode="HTML",
            reply_markup=_rows_to_markup(screen["keyboard"],
                                          InlineKeyboardButton, InlineKeyboardMarkup))
        state[user.id] = {"form": form, "request_id": request_id,
                          "archive": archive, "msg_id": msg.message_id}

    async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        user = q.from_user
        if user is None or user.id not in state:
            await q.edit_message_text("Form session not found. Re-open via the deep link.")
            return
        if not _is_allowed(user.id):
            await q.edit_message_text("Not allowed.")
            return
        s = state[user.id]
        ev = tg.apply_callback(s["form"], q.data)
        kind = ev["kind"]
        if kind == "cancel":
            await q.edit_message_text("Form cancelled. Re-open via the deep link to retry.")
            state.pop(user.id, None)
            return
        if kind == "submit":
            await q.edit_message_text("Submitting — slicing in the background, please wait…")
            res = submit_answer(s["request_id"], ev["answer"], s["archive"])
            tail = (f"\nPlate count: {res['plate_count']}" if res.get("plate_count") else "")
            await q.edit_message_text(
                ("✅ " if res["ok"] else "❌ ") + res.get("message", "") + tail,
                parse_mode="HTML")
            state.pop(user.id, None)
            return
        # rerender
        screen = tg.render_screen(s["form"])
        warning = ev.get("warning")
        text = screen["text"] + (f"\n\n⚠ {warning}" if warning else "")
        await q.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=_rows_to_markup(screen["keyboard"],
                                          InlineKeyboardButton, InlineKeyboardMarkup))

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_callback))
    logger.info("U1 sidecar bot starting (scripts=%s data=%s)", SCRIPTS_DIR, DATA_DIR)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
