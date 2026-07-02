"""Discord reference adapter for the toolkit form-protocol.

Renders a `form_schema` (from a `kit_form` event) as Discord string select
menus, collects the operator's selections, and produces the `--form-answers-json`
payload the kit workflow expects. OPTIONAL, consumer-side reference code — the
core toolkit never imports it and carries no Discord dependency.

Discord has NATIVE multi-select (`min_values`/`max_values` on a string select),
so the `multi_select` field needs no toggle bookkeeping — one menu, pick several,
submit. The PURE core (component construction, answer assembly) has no SDK
dependency and is unit-tested; the live bot loop uses `discord.py` behind a
guarded import.
"""
from __future__ import annotations

from typing import Any

# Discord hard limit: 25 options per select menu.
MAX_OPTIONS = 25


def _opt_id(option: Any) -> Any:
    return option["id"] if isinstance(option, dict) else option


def _opt_label(option: Any) -> str:
    return str(option["label"] if isinstance(option, dict) else option)


def build_components(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Build Discord message components (one action row per field).

    Each field becomes a string select menu (component type 3). A multi_select
    sets max_values = number of options (native multi-pick); single_select sets
    max_values = 1. custom_id is the field id (so the interaction handler knows
    which field). Option `value`s are the STABLE option ids (as strings) so the
    interaction returns ids directly. Fields with > 25 options are truncated and
    flagged (Discord cap) — the consumer should fall back to text_fallback then.
    """
    rows: list[dict[str, Any]] = []
    for field in schema["fields"]:
        opts = field["options"]
        truncated = len(opts) > MAX_OPTIONS
        shown = opts[:MAX_OPTIONS]
        is_multi = field["type"] == "multi_select"
        menu = {
            "type": 3,  # string select
            "custom_id": f"u1form:{field['id']}",
            "placeholder": field.get("label", field["id"]) + (" (pick several)" if is_multi else ""),
            "min_values": 0 if (is_multi or not field.get("required")) else 1,
            "max_values": len(shown) if is_multi else 1,
            "options": [{"label": _opt_label(o)[:100], "value": str(_opt_id(o))} for o in shown],
        }
        if truncated:
            menu["_truncated"] = True  # consumer signal to prefer text_fallback
        rows.append({"type": 1, "components": [menu]})  # action row
    return rows


def answer_json(schema: dict[str, Any], selections: dict[str, list[str]]) -> dict[str, Any]:
    """Assemble the --form-answers-json payload from Discord interaction values.

    `selections` maps field id -> list of selected option `value`s (the strings
    Discord returns, which are our stable option ids). multi_select -> list of
    ids (or "all"); single_select -> the single id, coerced back to int for
    fields whose ids are ints (e.g. profile). Empty selections are omitted so
    the workflow applies defaults / flags required fields.
    """
    by_id = {f["id"]: f for f in schema["fields"]}
    out: dict[str, Any] = {}
    for fid, picked in selections.items():
        field = by_id.get(fid)
        if not field or not picked:
            continue
        opts = field["options"]
        # Coerce values back to the option id's native type (int ids -> int).
        def _coerce(v: str) -> Any:
            for o in opts:
                oid = _opt_id(o)
                if str(oid) == v:
                    return oid
            return v
        if field["type"] == "multi_select":
            ids = [_coerce(v) for v in picked]
            out[fid] = "all" if len(ids) == len(opts) else ids
        else:
            out[fid] = _coerce(picked[0])
    return out


def run_form_bot(*args, **kwargs):  # pragma: no cover - requires live SDK + bot
    """Thin reference runtime. Lazily imports discord.py so the pure core stays
    dependency-free.

    Integration sketch a gateway implements:
      1. On `kit_form`, send a message with components=build_components(schema).
         Keep `selections = {}`.
      2. In the select interaction handler: selections[custom_id.split(':')[1]]
         = interaction.data['values']. Re-render or wait for a Submit button.
      3. On submit: payload = answer_json(schema, selections) and run:
         u1_kit_workflow.py <archive> --request-id <id> --form-answers-json '<payload>'
    """
    try:
        import discord  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "discord.py not installed. `pip install -r adapters/discord/requirements.txt`"
        ) from exc
    raise NotImplementedError(
        "run_form_bot is a reference sketch; wire it to your gateway's bot loop "
        "using build_components / answer_json."
    )
