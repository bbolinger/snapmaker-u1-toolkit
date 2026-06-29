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
from tools.registry import registry, tool_error  # type: ignore

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
