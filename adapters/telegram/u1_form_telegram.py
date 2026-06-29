"""Telegram reference renderer for the toolkit form-protocol.

**Pure** state-machine + screen builder + callback handler for rendering a
``form_schema`` (from a ``kit_form`` event) as a sequence of Telegram inline
keyboards. SDK-free — any bot can vendor it. The runtime wrapper at the bottom
imports ``python-telegram-bot`` lazily and is intentionally tiny.

Design (per the form-protocol Level-1 ladder):

  Parts → Orient → Tool → Material → Profile (paginated) → Supports → Action
        → Review card  →  on Submit, the caller invokes
                          ``u1_kit_workflow.py … --form-answers-json '<json>'``

Why screens instead of one giant keyboard: Telegram keyboards get tall fast,
profile lists are 16+ entries, and a review card before commit is the operator's
audit surface. Buttons collect choices ONLY — every downstream safety gate
(readiness card, Stage-1 photo, can_start) runs untouched on the submitted JSON.

Callback data convention (well under Telegram's 64-byte cap):
  ``t:<f>:<o>``   toggle option <o> of multi field <f>
  ``s:<f>:<o>``   select single option (advances to next screen)
  ``a:<f>``       multi: select All
  ``z:<f>``       multi: clear (None)
  ``n:<f>``       multi: Done → advance
  ``p:<f>:<pg>``  paginate field <f> to page <pg> (0-based)
  ``e:<f>``       edit field <f> from review (jump back to its screen)
  ``S``           Submit (from review)
  ``X``           Cancel
"""
from __future__ import annotations

from typing import Any

# Tunables (callers may monkey-patch for tests / different UX).
PAGE_SIZE = 8                # buttons per screen when a field has > PAGE_SIZE options
INLINE_OPTIONS_FOR_SINGLE = 6  # single-select fields ≤ this many opts render unpaginated
REVIEW_FIELD = "__review__"


# --------------------------------------------------------------------------- #
# Pure core — no SDK
# --------------------------------------------------------------------------- #

def _opt_id(opt: Any) -> Any:
    return opt["id"] if isinstance(opt, dict) else opt


def _opt_label(opt: Any) -> str:
    return str(opt["label"] if isinstance(opt, dict) else opt)


def new_form(schema: dict[str, Any]) -> dict[str, Any]:
    """Initial form state: cursor at the first field, empty selections."""
    fields = schema["fields"]
    return {
        "schema": schema,
        "current": fields[0]["id"] if fields else REVIEW_FIELD,
        "selections": {f["id"]: (set() if f["type"] == "multi_select" else None) for f in fields},
        "pages": {f["id"]: 0 for f in fields},
    }


def _field(form: dict[str, Any], fid: str) -> dict[str, Any]:
    return next(f for f in form["schema"]["fields"] if f["id"] == fid)


def _field_index(form: dict[str, Any], fid: str) -> int:
    return next(i for i, f in enumerate(form["schema"]["fields"]) if f["id"] == fid)


def _next_field(form: dict[str, Any]) -> str:
    """Advance the cursor: next field, or REVIEW_FIELD when past the end."""
    fields = form["schema"]["fields"]
    if form["current"] == REVIEW_FIELD:
        return REVIEW_FIELD
    idx = _field_index(form, form["current"])
    return fields[idx + 1]["id"] if idx + 1 < len(fields) else REVIEW_FIELD


def _paginate(opts: list, page: int, size: int) -> tuple[list, int, int]:
    """Return (slice, page, total_pages)."""
    total = (len(opts) + size - 1) // size or 1
    page = max(0, min(page, total - 1))
    return opts[page * size:(page + 1) * size], page, total


def _selection_label_for(form: dict[str, Any], field: dict[str, Any]) -> str:
    """Human-readable echo of a field's current selection (for the review card)."""
    val = form["selections"].get(field["id"])
    opts = field["options"]
    if field["type"] == "multi_select":
        if not val:
            return "—" if field.get("default") != "all" else f"all ({len(opts)})"
        if len(val) == len(opts):
            return f"all ({len(opts)})"
        return ", ".join(_opt_label(opts[i]) for i in sorted(val))
    if val is None:
        d = field.get("default")
        return f"{d} (default)" if d else "—"
    return _opt_label(opts[val])


# ---- Screen builders (one per state) -------------------------------------- #

def render_screen(form: dict[str, Any]) -> dict[str, Any]:
    """Build the message text + keyboard rows for the current screen.

    Returns ``{"text": str, "keyboard": list[list[{"text": str, "callback_data": str}]]}``.
    """
    if form["current"] == REVIEW_FIELD:
        return _render_review(form)
    field = _field(form, form["current"])
    return _render_field(form, field)


def _render_field(form: dict[str, Any], field: dict[str, Any]) -> dict[str, Any]:
    fi = _field_index(form, field["id"])
    page = form["pages"][field["id"]]
    paginated = len(field["options"]) > PAGE_SIZE
    slice_, page, total_pages = _paginate(field["options"], page, PAGE_SIZE)
    rows: list[list[dict[str, str]]] = []
    is_multi = field["type"] == "multi_select"

    page_offset = page * PAGE_SIZE
    sel = form["selections"][field["id"]]
    for local_oi, opt in enumerate(slice_):
        oi = page_offset + local_oi
        if is_multi:
            mark = "✔ " if oi in sel else ""
            cb = f"t:{fi}:{oi}"
        else:
            mark = "● " if sel == oi else ""
            cb = f"s:{fi}:{oi}"
        rows.append([{"text": f"{mark}{_opt_label(opt)}", "callback_data": cb}])

    # Pagination row
    if paginated and total_pages > 1:
        nav: list[dict[str, str]] = []
        if page > 0:
            nav.append({"text": "‹ Prev", "callback_data": f"p:{fi}:{page - 1}"})
        nav.append({"text": f"{page + 1}/{total_pages}", "callback_data": f"p:{fi}:{page}"})
        if page + 1 < total_pages:
            nav.append({"text": "Next ›", "callback_data": f"p:{fi}:{page + 1}"})
        rows.append(nav)

    # Multi-select action row: All / None / Done
    if is_multi:
        rows.append([
            {"text": "All", "callback_data": f"a:{fi}"},
            {"text": "None", "callback_data": f"z:{fi}"},
            {"text": "✅ Done", "callback_data": f"n:{fi}"},
        ])

    # Cancel always present
    rows.append([{"text": "✖ Cancel", "callback_data": "X"}])

    label = field.get("label", field["id"])
    hint = ""
    if is_multi:
        n = len(sel) if sel else 0
        hint = f"  ({n} selected)"
    text = f"<b>{label}</b>{hint}\n<i>Step {fi + 1} of {len(form['schema']['fields'])}</i>"
    return {"text": text, "keyboard": rows}


def _render_review(form: dict[str, Any]) -> dict[str, Any]:
    lines = ["<b>Review</b>", ""]
    rows: list[list[dict[str, str]]] = []
    for fi, field in enumerate(form["schema"]["fields"]):
        echo = _selection_label_for(form, field)
        lines.append(f"• <b>{field.get('label', field['id'])}</b>: {echo}")
        rows.append([{"text": f"✎ Edit {field.get('label', field['id'])}",
                      "callback_data": f"e:{fi}"}])
    rows.append([
        {"text": "✅ Submit", "callback_data": "S"},
        {"text": "✖ Cancel", "callback_data": "X"},
    ])
    lines.append("")
    lines.append("<i>Submit runs the same safety pipeline as the typed form (slicer warnings → readiness card → Stage-1 photo gate). The buttons only collect — they never bypass.</i>")
    return {"text": "\n".join(lines), "keyboard": rows}


# ---- Callback handling (the state machine) -------------------------------- #

def apply_callback(form: dict[str, Any], data: str) -> dict[str, Any]:
    """Apply a tap. Returns an event dict:

    ``{"kind": "rerender"}``                 → caller edits the message in place
    ``{"kind": "submit", "answer": {...}}``  → caller invokes --form-answers-json
    ``{"kind": "cancel"}``                   → caller acknowledges + cleans up
    """
    parts = data.split(":")
    kind = parts[0]
    fields = form["schema"]["fields"]

    if kind == "X":
        return {"kind": "cancel"}

    if kind == "S":
        # Validate required fields are answered before letting submit through.
        missing = []
        for f in fields:
            if f.get("required"):
                val = form["selections"][f["id"]]
                if (f["type"] == "multi_select" and not val) or (f["type"] != "multi_select" and val is None):
                    missing.append(f.get("label", f["id"]))
        if missing:
            # Jump back to the first required-but-unset field.
            for f in fields:
                if f.get("label", f["id"]) == missing[0]:
                    form["current"] = f["id"]
                    break
            return {"kind": "rerender", "warning": f"Please answer: {', '.join(missing)}"}
        return {"kind": "submit", "answer": answer_json(form)}

    fi = int(parts[1])
    field = fields[fi]
    fid = field["id"]

    if kind == "e":
        form["current"] = fid
        return {"kind": "rerender"}

    if kind == "p":
        form["pages"][fid] = int(parts[2])
        return {"kind": "rerender"}

    if kind == "t":
        oi = int(parts[2])
        s = form["selections"][fid]
        s.discard(oi) if oi in s else s.add(oi)
        return {"kind": "rerender"}

    if kind == "a":
        form["selections"][fid] = set(range(len(field["options"])))
        return {"kind": "rerender"}

    if kind == "z":
        form["selections"][fid] = set()
        return {"kind": "rerender"}

    if kind == "n":
        # Done on a multi field → advance to next
        form["current"] = _next_field(form)
        return {"kind": "rerender"}

    if kind == "s":
        oi = int(parts[2])
        form["selections"][fid] = oi
        form["current"] = _next_field(form)
        return {"kind": "rerender"}

    raise ValueError(f"bad callback_data: {data!r}")


def answer_json(form: dict[str, Any]) -> dict[str, Any]:
    """Assemble the --form-answers-json payload from collected selections.

    multi_select → list of stable option ids (or ``"all"`` when every option is
    chosen). single_select → the chosen option id. Unset single fields are
    omitted so the toolkit applies its defaults / flags required ones.
    """
    out: dict[str, Any] = {}
    for field in form["schema"]["fields"]:
        fid = field["id"]
        val = form["selections"][fid]
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
# Live bot loop (optional — sketches, behind a guarded import)
# --------------------------------------------------------------------------- #

def run_form_bot(*args, **kwargs):  # pragma: no cover - requires live SDK + bot
    """Reference runtime sketch. Imports python-telegram-bot lazily so the pure
    core never requires it. Pattern any host bot can copy:

        form = new_form(schema)
        screen = render_screen(form)
        msg = await bot.send_message(chat_id, screen['text'], parse_mode='HTML',
                                     reply_markup=InlineKeyboardMarkup(rows_to_buttons(screen['keyboard'])))

        async def on_callback(query):
            ev = apply_callback(form, query.data)
            if ev['kind'] == 'cancel':
                await query.edit_message_text("Form cancelled.")
                return
            if ev['kind'] == 'submit':
                # Subprocess: u1_kit_workflow.py ... --form-answers-json '<json>'
                payload = json.dumps(ev['answer'])
                ...
                await query.edit_message_text("Submitted.")
                return
            screen = render_screen(form)
            await query.edit_message_text(screen['text'], parse_mode='HTML',
                                          reply_markup=...)
    """
    try:
        import telegram  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "python-telegram-bot not installed. `pip install -r adapters/telegram/requirements.txt`"
        ) from exc
    raise NotImplementedError(
        "run_form_bot is a reference sketch; wire it to your bot's update loop "
        "using new_form / render_screen / apply_callback / answer_json."
    )
