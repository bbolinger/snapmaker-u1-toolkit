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

import html
from typing import Any

# Tunables (callers may monkey-patch for tests / different UX).
PAGE_SIZE = 8                # buttons per screen when a field has > PAGE_SIZE options
REVIEW_FIELD = "__review__"


# --------------------------------------------------------------------------- #
# Pure core — no SDK
# --------------------------------------------------------------------------- #

def _esc(s: Any) -> str:
    """HTML-escape a schema-derived string before interpolating it into
    message text (which is sent with ParseMode.HTML). A part filename like
    ``bracket<v2>.stl`` in a label would otherwise make Telegram reject the
    whole send ("can't parse entities") — or inject markup into the card.

    NOTE: only message TEXT is parsed as HTML. InlineKeyboardButton text is
    plain — do not escape button labels (double-escaping shows raw entities).
    """
    return html.escape(str(s), quote=False)


def _opt_id(opt: Any) -> Any:
    return opt["id"] if isinstance(opt, dict) else opt


def _opt_label(opt: Any) -> str:
    return str(opt["label"] if isinstance(opt, dict) else opt)


def _is_screen_field(f: dict[str, Any]) -> bool:
    """A field that gets its own screen. ``submit_choice`` fields (e.g. Action)
    are rendered as verbs on the review card, never a standalone screen."""
    return not f.get("submit_choice")


def _first_screen_field(fields: list[dict[str, Any]]) -> str:
    return next((f["id"] for f in fields if _is_screen_field(f)), REVIEW_FIELD)


def new_form(schema: dict[str, Any]) -> dict[str, Any]:
    """Initial form state: cursor at the first screen field, empty selections."""
    fields = schema["fields"]
    return {
        "schema": schema,
        "current": _first_screen_field(fields),
        "selections": {f["id"]: (set() if f["type"] == "multi_select" else None) for f in fields},
        "pages": {f["id"]: 0 for f in fields},
    }


def _field(form: dict[str, Any], fid: str) -> dict[str, Any]:
    return next(f for f in form["schema"]["fields"] if f["id"] == fid)


def _field_index(form: dict[str, Any], fid: str) -> int:
    return next(i for i, f in enumerate(form["schema"]["fields"]) if f["id"] == fid)


def _screens(form: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """Ordered list of screens. A screen is a maximal run of CONSECUTIVE
    screen-fields sharing a non-empty ``group`` (e.g. head + orient + supports
    render together), or a single ungrouped field. submit_choice fields have no
    screen."""
    fields = [f for f in form["schema"]["fields"] if _is_screen_field(f)]
    screens: list[list[dict[str, Any]]] = []
    i = 0
    while i < len(fields):
        grp = fields[i].get("group")
        if grp:
            j = i
            while j + 1 < len(fields) and fields[j + 1].get("group") == grp:
                j += 1
            screens.append(fields[i:j + 1])
            i = j + 1
        else:
            screens.append([fields[i]])
            i += 1
    return screens


def _screen_of(form: dict[str, Any], fid: str) -> list[dict[str, Any]]:
    """The screen (list of fields) that renders together with ``fid``."""
    for screen in _screens(form):
        if any(f["id"] == fid for f in screen):
            return screen
    return [_field(form, fid)]


def _next_field(form: dict[str, Any]) -> str:
    """Advance the cursor to the first field of the NEXT screen, or
    REVIEW_FIELD past the end."""
    if form["current"] == REVIEW_FIELD:
        return REVIEW_FIELD
    screens = _screens(form)
    for si, screen in enumerate(screens):
        if any(f["id"] == form["current"] for f in screen):
            return screens[si + 1][0]["id"] if si + 1 < len(screens) else REVIEW_FIELD
    return REVIEW_FIELD


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
    screen = _screen_of(form, form["current"])
    if len(screen) > 1:
        return _render_group(form, screen)
    return _render_field(form, screen[0])


def _step_suffix(form: dict[str, Any], fid: str) -> str:
    """"Step N of M" over SCREENS (a group counts as one screen)."""
    screens = _screens(form)
    pos = next((i for i, sc in enumerate(screens) if any(f["id"] == fid for f in sc)), 0)
    return f"\n<i>Step {pos + 1} of {len(screens)}</i>"


def _field_control_rows(form: dict[str, Any], field: dict[str, Any]) -> list[list[dict[str, str]]]:
    """Option rows (+ pagination, + multi Select-all/Clear) for ONE field.
    Shared by single-field and grouped screens. Callback field indices are
    ABSOLUTE into schema['fields'] so apply_callback resolves them directly."""
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
            mark = "● " if sel == oi else "○ "
            cb = f"s:{fi}:{oi}"
        rows.append([{"text": f"{mark}{_opt_label(opt)}", "callback_data": cb}])
    if paginated and total_pages > 1:
        nav: list[dict[str, str]] = []
        if page > 0:
            nav.append({"text": "‹ Prev", "callback_data": f"p:{fi}:{page - 1}"})
        nav.append({"text": f"{page + 1}/{total_pages}", "callback_data": f"p:{fi}:{page}"})
        if page + 1 < total_pages:
            nav.append({"text": "Next ›", "callback_data": f"p:{fi}:{page + 1}"})
        rows.append(nav)
    if is_multi:
        multi_row = [
            {"text": "Select all", "callback_data": f"a:{fi}"},
            {"text": "Clear", "callback_data": f"z:{fi}"},
        ]
        # On its OWN screen a multi keeps its Next; in a group the Next is
        # shared (added by _render_group).
        if not field.get("group"):
            multi_row.append({"text": "Next ➜", "callback_data": f"n:{fi}"})
        rows.append(multi_row)
    return rows


def _multi_hint(field: dict[str, Any], sel) -> str:
    n = len(sel) if sel else 0
    if n == 0 and field.get("default") == "all":
        return f"  (none picked → all {len(field['options'])})"
    return f"  ({n} of {len(field['options'])} selected)"


def _render_field(form: dict[str, Any], field: dict[str, Any]) -> dict[str, Any]:
    rows = _field_control_rows(form, field)
    rows.append([{"text": "✖ Cancel", "callback_data": "X"}])
    label = _esc(field.get("label", field["id"]))
    hint = tip = ""
    if field["type"] == "multi_select":
        hint = _multi_hint(field, form["selections"][field["id"]])
        tip = "\n<i>Tap options to toggle ✔, then Next ➜</i>"
    text = f"<b>{label}</b>{hint}{tip}" + _step_suffix(form, field["id"])
    return {"text": text, "keyboard": rows}


def _render_group(form: dict[str, Any], screen: list[dict[str, Any]]) -> dict[str, Any]:
    """Render several fields on ONE screen (e.g. head + orient + supports),
    each as a labelled block, with a single shared Next ➜."""
    first = screen[0]
    title = _esc(first.get("group_label") or "Setup")
    lines = [f"<b>{title}</b>" + _step_suffix(form, first["id"])]
    rows: list[list[dict[str, str]]] = []
    for field in screen:
        sub = _esc(field.get("label", field["id"]))
        if field["type"] == "multi_select":
            sub += _multi_hint(field, form["selections"][field["id"]])
        lines.append(f"\n<b>{sub}</b>")
        rows.extend(_field_control_rows(form, field))
    rows.append([{"text": "Next ➜", "callback_data": f"n:{_field_index(form, first['id'])}"}])
    rows.append([{"text": "✖ Cancel", "callback_data": "X"}])
    return {"text": "\n".join(lines), "keyboard": rows}


# Submit-verb button styling per action option id.
_SUBMIT_VERBS = {
    "upload-only": "\u2b06 Upload only",
    "start": "\u25b6 Upload + Start",
}


def _render_review(form: dict[str, Any]) -> dict[str, Any]:
    lines = ["<b>Review</b>", ""]
    rows: list[list[dict[str, str]]] = []
    for fi, field in enumerate(form["schema"]["fields"]):
        if not _is_screen_field(field):
            continue  # submit_choice (Action) is the verb row below, not a line
        echo = _selection_label_for(form, field)
        # Message text is ParseMode.HTML \u2192 escape schema-derived strings.
        # Button text is NOT parsed as HTML \u2192 leave the Edit label raw.
        lines.append(f"\u2022 <b>{_esc(field.get('label', field['id']))}</b>: {_esc(echo)}")
        rows.append([{"text": f"\u270e Edit {field.get('label', field['id'])}",
                      "callback_data": f"e:{fi}"}])
    # Action becomes the submit verbs: each option submits with that action.
    submit_field = next((f for f in form["schema"]["fields"] if f.get("submit_choice")), None)
    if submit_field:
        sfi = _field_index(form, submit_field["id"])
        verb_row = [
            {"text": _SUBMIT_VERBS.get(_opt_id(opt), f"Submit ({_opt_id(opt)})"),
             "callback_data": f"S:{sfi}:{oi}"}
            for oi, opt in enumerate(submit_field["options"])
        ]
        rows.append(verb_row)
        rows.append([{"text": "\u2716 Cancel", "callback_data": "X"}])
    else:
        rows.append([
            {"text": "\u2705 Submit", "callback_data": "S"},
            {"text": "\u2716 Cancel", "callback_data": "X"},
        ])
    lines.append("")
    lines.append("<i>Submit runs the same safety pipeline as the typed form (slicer warnings \u2192 readiness card \u2192 Stage-1 photo gate). The buttons only collect \u2014 they never bypass.</i>")
    return {"text": "\n".join(lines), "keyboard": rows}


# ---- Callback handling (the state machine) -------------------------------- #

def apply_callback(form: dict[str, Any], data: str) -> dict[str, Any]:
    """Apply a tap. Returns an event dict:

    ``{"kind": "rerender"}``                 → caller edits the message in place
    ``{"kind": "submit", "answer": {...}}``  → caller invokes --form-answers-json
    ``{"kind": "cancel"}``                   → caller acknowledges + cleans up
    ``{"kind": "rerender", "warning": ...}`` → stale/malformed callback (e.g. an
                                              old button after a deploy/schema
                                              change); the form continues.
    """
    try:
        return _apply_callback_inner(form, data)
    except (ValueError, IndexError, KeyError, TypeError) as exc:
        # Stale callback_data after a redeploy, an out-of-range field/option
        # index, or any other malformed input must not crash the bot — surface
        # it as a clean rerender so the operator can keep going from the
        # current screen rather than seeing an exception traceback.
        return {"kind": "rerender",
                "warning": f"Stale or invalid button ({_esc(exc)}). Re-open the form via the deep link if this persists."}


def _apply_callback_inner(form: dict[str, Any], data: str) -> dict[str, Any]:
    parts = data.split(":")
    kind = parts[0]
    fields = form["schema"]["fields"]

    if kind == "X":
        return {"kind": "cancel"}

    if kind == "S":
        # A submit-verb (S:<action_field>:<option>) also SETS the action before
        # submitting; a bare "S" (legacy / no submit_choice field) just submits.
        submit_field = next((f for f in fields if f.get("submit_choice")), None)
        if len(parts) == 3:
            afi, aoi = int(parts[1]), int(parts[2])
            afield = fields[afi]
            if afield.get("submit_choice") and 0 <= aoi < len(afield["options"]):
                form["selections"][afield["id"]] = aoi
        elif submit_field is not None:
            # Bare "S" on a submit-verb schema (a stale button from a pre-verb
            # render, or an injected callback). Do NOT submit with a silently
            # defaulted action — that would pick "start" (the print path) on
            # ambiguity. Re-show the review so the operator taps an explicit
            # verb; the verbs are the only sanctioned submit path here.
            form["current"] = REVIEW_FIELD
            return {"kind": "rerender",
                    "warning": "Choose “Upload only” or “Upload + Start”."}
        # Validate required SCREEN fields are answered before submit.
        missing = []
        for f in fields:
            if f.get("required") and _is_screen_field(f):
                val = form["selections"][f["id"]]
                if (f["type"] == "multi_select" and not val) or (f["type"] != "multi_select" and val is None):
                    missing.append(f.get("label", f["id"]))
        if missing:
            # Jump back to the first required-but-unset field.
            for f in fields:
                if f.get("label", f["id"]) == missing[0]:
                    form["current"] = f["id"]
                    break
            return {"kind": "rerender",
                    "warning": f"Please answer: {_esc(', '.join(missing))}"}
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
        if oi < 0 or oi >= len(field["options"]):
            return {"kind": "rerender",
                    "warning": f"Stale button (option {oi} out of range for {_esc(repr(fid))})."}
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
        if oi < 0 or oi >= len(field["options"]):
            return {"kind": "rerender",
                    "warning": f"Stale button (option {oi} out of range for {_esc(repr(fid))})."}
        form["selections"][fid] = oi
        # A single-select on its own screen advances on tap; one inside a
        # group is a radio — it only marks, the shared Next advances.
        if not field.get("group"):
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
