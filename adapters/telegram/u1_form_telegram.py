"""Telegram reference adapter for the toolkit form-protocol.

Renders a `form_schema` (from a `kit_form` event) as Telegram inline keyboards,
collects the operator's taps, and produces the `--form-answers-json` payload the
kit workflow expects. This is OPTIONAL, consumer-side reference code — the core
toolkit never imports it and carries no Telegram dependency. A gateway can use
this, copy it, or ignore it and fall back to the schema's `text_fallback`.

The PURE core (keyboard layout, tap state, answer assembly) has no SDK
dependency and is unit-tested. The live bot loop at the bottom uses
`python-telegram-bot` behind a guarded import.

Pattern (the established Telegram multi-select idiom): single-select fields
resolve on tap; the `multi_select` field (parts) shows toggle buttons (✔) that
edit the keyboard in place, plus a Done button. callback_data is index-based
(`<kind>:<field_idx>:<opt_idx>`) to stay well under Telegram's 64-byte cap; the
held schema maps indices back to stable option ids when assembling the answer.
"""
from __future__ import annotations

from typing import Any


# --------------------------------------------------------------------------- #
# Pure core (no SDK)
# --------------------------------------------------------------------------- #

def _opt_id(option: Any) -> Any:
    return option["id"] if isinstance(option, dict) else option


def _opt_label(option: Any) -> str:
    return str(option["label"] if isinstance(option, dict) else option)


def new_state(schema: dict[str, Any]) -> dict[str, Any]:
    """Per-message collection state: field_id -> set(opt_idx) | opt_idx | None."""
    state: dict[str, Any] = {}
    for f in schema["fields"]:
        state[f["id"]] = set() if f["type"] == "multi_select" else None
    return state


def field_keyboard(field: dict[str, Any], field_idx: int, state: dict[str, Any]) -> list[list[dict[str, str]]]:
    """Inline-keyboard rows for one field. Returns rows of {text, callback_data}."""
    rows: list[list[dict[str, str]]] = []
    is_multi = field["type"] == "multi_select"
    sel = state.get(field["id"])
    for oi, opt in enumerate(field["options"]):
        chosen = (oi in sel) if is_multi else (sel == oi)
        mark = "✔ " if chosen else ""
        kind = "t" if is_multi else "s"
        rows.append([{"text": f"{mark}{_opt_label(opt)}", "callback_data": f"{kind}:{field_idx}:{oi}"}])
    if is_multi:
        rows.append([{"text": "✅ Done", "callback_data": f"d:{field_idx}"}])
    return rows


def apply_callback(schema: dict[str, Any], state: dict[str, Any], data: str) -> tuple[str, str]:
    """Apply a callback_data string to the state. Returns (action, field_id).

    action is 'toggled' (multi tap), 'selected' (single tap), or 'done'
    (multi-field confirm). Raises ValueError on malformed data.
    """
    parts = data.split(":")
    kind = parts[0]
    field_idx = int(parts[1])
    field = schema["fields"][field_idx]
    fid = field["id"]
    if kind == "t":
        oi = int(parts[2])
        s = state[fid]
        s.discard(oi) if oi in s else s.add(oi)
        return "toggled", fid
    if kind == "s":
        state[fid] = int(parts[2])
        return "selected", fid
    if kind == "d":
        return "done", fid
    raise ValueError(f"bad callback_data: {data!r}")


def answer_json(schema: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Assemble the --form-answers-json payload from collected tap state.

    multi_select -> list of stable option ids (or "all" when every option is
    selected); single_select -> the chosen option id. Unset single fields are
    omitted so the workflow applies its defaults / flags required ones.
    """
    out: dict[str, Any] = {}
    for fi, field in enumerate(schema["fields"]):
        fid = field["id"]
        val = state.get(fid)
        opts = field["options"]
        if field["type"] == "multi_select":
            if not val:
                continue
            if len(val) == len(opts):
                out[fid] = "all"
            else:
                out[fid] = [_opt_id(opts[i]) for i in sorted(val)]
        else:
            if val is None:
                continue
            out[fid] = _opt_id(opts[val])
    return out


# --------------------------------------------------------------------------- #
# Live bot loop (optional; needs python-telegram-bot)
# --------------------------------------------------------------------------- #

def run_form_bot(*args, **kwargs):  # pragma: no cover - requires live SDK + bot
    """Thin reference runtime. Imports the SDK lazily so importing this module
    (for the pure core) never requires python-telegram-bot.

    Sketch of the integration a gateway implements:
      1. On a `kit_form` event, send a message per field with
         InlineKeyboardMarkup(field_keyboard(...)). Keep `state = new_state(schema)`.
      2. In the CallbackQueryHandler: action, fid = apply_callback(schema, state, data).
         On 'toggled' -> edit_message_reply_markup(field_keyboard(...)) in place.
         On 'selected' -> mark the single field done, advance.
         On 'done' (last field) -> payload = answer_json(schema, state) and run:
            u1_kit_workflow.py <archive> --request-id <id> --form-answers-json '<payload>'
    """
    try:
        import telegram  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "python-telegram-bot not installed. `pip install -r adapters/telegram/requirements.txt`"
        ) from exc
    raise NotImplementedError(
        "run_form_bot is a reference sketch; wire it to your gateway's bot loop "
        "using the pure helpers (field_keyboard / apply_callback / answer_json)."
    )
