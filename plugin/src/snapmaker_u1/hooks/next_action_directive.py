"""transform_tool_result hook — harden Gemma against anti-pattern #7.

The kit workflow emits a `next_action_required` event when it needs the agent
to run the Stage 1 photo-gate command. Observed failure (session
20260629_220708_5e63b52f, 2026-06-29): gemma4-26b-64k printed the command
verbatim as TEXT in its reply (anti-pattern #7 in SKILL.md) AND fabricated a
typo in the request_id (`u1_2026_0603_…` vs the actual `u1_2026_0630_…`).
Stage 1 never ran.

This hook intercepts the `terminal` tool result and, when the result contains
a `next_action_required` event with a `command` field, prepends a strongly
directive prefix to the tool output the agent reads. The directive tells the
agent in unmissable language that the command must be tool-called via
terminal (NOT printed as text), and that the request_id must be copied
verbatim (NOT regenerated).

VALID_MIDDLEWARE includes TOOL_EXECUTION_MIDDLEWARE, but `transform_tool_result`
is the documented hook for adjusting tool results between agent rounds
(verified at hermes_cli/plugins.py:128). First non-None return wins.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Match the JSON-events line emitted by the kit/single-STL workflows.
# Lines are individual JSON objects, one per line, in the tool output's "output"
# field. We look for `"stage": "next_action_required"` with a `"command":` value.
_NEXT_ACTION_RE = re.compile(
    r'"stage"\s*:\s*"next_action_required".*?"command"\s*:\s*"([^"]+)"',
    re.DOTALL,
)
_REQUEST_ID_RE = re.compile(r"u1_\d{4}_\d{4}_[a-f0-9]+")


def transform(tool_name: str = "", args: Any = None, result: Any = None,
              **kwargs: Any) -> Any:
    """Wrap workflow output containing next_action_required with directive prefix."""
    if tool_name != "terminal":
        return None  # only target terminal tool output
    if not result:
        return None
    # The terminal tool result is a JSON-stringified dict or a dict.
    # We look at the "output" field which contains the workflow stdout.
    try:
        if isinstance(result, str):
            result_dict = json.loads(result)
        elif isinstance(result, dict):
            result_dict = result
        else:
            return None
    except (ValueError, TypeError):
        return None
    output = result_dict.get("output", "") or ""
    if not output or "next_action_required" not in output:
        return None
    m = _NEXT_ACTION_RE.search(output)
    if not m:
        return None
    command = m.group(1)
    # Extract the canonical request_id from the command for the directive
    rid_match = _REQUEST_ID_RE.search(command)
    canonical_rid = rid_match.group(0) if rid_match else None

    directive_parts = [
        "[CRITICAL — DO NOT IGNORE THIS DIRECTIVE]",
        "The workflow has emitted a `next_action_required` event below.",
        "Your IMMEDIATE next tool call MUST be the `terminal` tool with the",
        "EXACT `command` shown — verbatim, no edits, no paraphrase.",
        "",
        f"  command: {command}",
        "",
    ]
    if canonical_rid:
        directive_parts.append(
            f"The `request_id` in that command is `{canonical_rid}`. "
            "Copy it CHARACTER-FOR-CHARACTER. Do NOT regenerate, do NOT alter "
            "the date digits, do NOT make a new ID."
        )
        directive_parts.append("")
    directive_parts.extend([
        "Anti-patterns this directive blocks (see SKILL.md):",
        "  #7 — Printing the command as text instead of tool-calling it. NEVER do this.",
        "  #4 — State-from-chat-memory: do NOT compose the command from memory; use the verbatim string above.",
        "  #6 — Verification fabrication: do NOT fabricate or 'fix' the request_id.",
        "",
        "After the terminal tool call returns, follow Step 4 of the skill",
        "(surface the snapshot.path, ask for the operator's yes/no).",
        "",
        "===== ORIGINAL WORKFLOW OUTPUT FOLLOWS =====",
        output,
    ])
    new_output = "\n".join(directive_parts)
    result_dict["output"] = new_output
    logger.info(
        "snapmaker_u1 next_action_directive: wrapped %d-char terminal output "
        "with anti-pattern-#7 directive (canonical_rid=%s)",
        len(output), canonical_rid,
    )
    # Return must match the original result shape. terminal results are
    # serialized JSON strings in registry dispatch; pass back a string if we
    # got a string, dict if we got a dict.
    if isinstance(result, str):
        return json.dumps(result_dict, ensure_ascii=False)
    return result_dict
