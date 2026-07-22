"""pre_gateway_dispatch hook — set event.auto_skill when a 3D-model attachment arrives.

Verified behavior (hermes-agent v0.17):
  * MessageEvent is @dataclass WITHOUT frozen=True → mutable.
  * The gateway calls `invoke_hook("pre_gateway_dispatch", event=event, ...)`
    BEFORE auth + agent dispatch.
  * A hook can mutate the event in place; the gateway's later auto-skill loader
    reads `getattr(event, "auto_skill", None)` and injects the skill body as
    the first user message of the session (the same plumbing topic-bound
    skills use).

What we detect (in order of confidence):
  1. The Telegram-shape attachment template Hermes injects when a document is
     received: ``[The user sent a document: 'name.zip'. It is saved at: <path>]``
  2. Generic file-extension matches anywhere in the visible message text
     (fallback for other platforms / pasted paths). Word-boundary keeps us
     from matching ``stl_pipeline.py`` or similar false positives.

Conservative scope:
  * We only mutate `auto_skill` when it's currently unset — if a topic binding
    or another upstream hook already chose a skill, we don't overwrite.
  * We return None so dispatch continues normally (we're not blocking/rewriting
    the message itself — just setting an attribute the loader reads later).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Matches the canonical Telegram attachment text Hermes injects + any bare
# .stl/.3mf/.zip reference in the message body. Word-boundary `\b` keeps
# us from matching "stl_pipeline.py" or similar false positives.
_ATTACHMENT_RE = re.compile(r"\.(stl|3mf|zip)\b", re.IGNORECASE)

# Reprint requests carry NO attachment, so the attachment match can't fire —
# but the skill (v2.3) owns the reprint flow. Trigger the same auto-load on
# reprint phrasing. Word-boundaried and narrow on purpose: "reprint" or
# "print (it/that/this) again".
_REPRINT_RE = re.compile(r"\b(reprint|print\s+(it|that|this)?\s*again)\b",
                         re.IGNORECASE)

# Per-turn steering for MID-SESSION kit uploads. auto_skill alone stopped
# covering this: the gateway's loader injects the skill body only when
# `_is_new_session` is true ("ongoing conversations already have the skill
# in history"), which is false the moment a kit arrives on any turn after
# the first; the model then has no idea u1_kit exists and hand-unzips the
# attachment (live 2026-07-21, post-upgrade). channel_prompt rides the
# event into the TURN'S ephemeral prompt on every dispatch, new session or
# not, so the one instruction that matters always lands. The full skill
# still auto-loads on new sessions via auto_skill, exactly as before.
_KIT_DIRECTIVE_TAG = "[U1 print kit attachment detected"
_KIT_DIRECTIVE = (
    "[U1 print kit attachment detected in this message. Immediately call the "
    "u1_kit tool with model_path set to the attachment's saved file path "
    "exactly as it appears in the message. Do not unzip, list, slice, or "
    "inspect the file with any other tool; u1_kit is the only correct entry "
    "point and it drives the whole flow itself.]"
)


def _document_file_name(raw: Any) -> str | None:
    """The attachment's filename from the raw platform message, if any.

    Handles both shapes seen across Hermes versions: an object graph
    (python-telegram-bot's Message.document.file_name) and plain dicts
    (adapters that pass the Bot API payload through unwrapped)."""
    if raw is None:
        return None
    doc = getattr(raw, "document", None)
    if doc is None and isinstance(raw, dict):
        doc = raw.get("document")
    if doc is None:
        return None
    name = getattr(doc, "file_name", None)
    if name is None and isinstance(doc, dict):
        name = doc.get("file_name")
    return name if isinstance(name, str) and name else None


def _arm_kit_directive(event: Any) -> bool:
    """Append the u1_kit directive to the event's channel_prompt.

    Idempotent (tag check) and fail-soft: a Hermes whose events have no
    channel_prompt simply ignores the attribute and we degrade to the
    auto_skill-only behavior this hook always had."""
    try:
        current = getattr(event, "channel_prompt", None) or ""
        if _KIT_DIRECTIVE_TAG in current:
            return True
        event.channel_prompt = (current + "\n\n" + _KIT_DIRECTIVE).strip()
        return True
    except Exception as exc:
        logger.warning(
            "snapmaker_u1 attachment_router: failed to arm kit directive: %s",
            exc,
        )
        return False


def make_handler(skill_identifier: str) -> Callable[..., Any]:
    """Build the pre_gateway_dispatch handler with the resolved skill identifier baked in.

    Args:
        skill_identifier: either `"<flat-name>"` (loaded from ~/.hermes/skills/)
                          or `"<plugin>:<skill>"` (plugin-namespaced).
                          The gateway's _load_skill_payload + skill_view chain
                          resolves both forms.
    """

    def handler(event: Any = None, gateway: Any = None, session_store: Any = None,
                **kwargs: Any):
        if event is None:
            return None
        try:
            _text = getattr(event, "text", "") or ""
            _doc_name = _document_file_name(getattr(event, "raw_message", None))
            logger.debug(
                "snapmaker_u1 attachment_router: text_len=%d doc_name=%r "
                "current_auto_skill=%r",
                len(_text), _doc_name, getattr(event, "auto_skill", None),
            )
        except Exception as _exc:
            logger.warning("snapmaker_u1 attachment_router: event inspect failed: %s", _exc)
            _text = ""
            _doc_name = None
        # Try BOTH the visible text (post-template) and the raw document
        # filename (pre-template): the hook fires before Hermes injects the
        # attachment template, so the filename match is the one that lands on
        # Telegram; the text regex covers other platforms and pasted paths.
        matched_by = None
        if _text and _ATTACHMENT_RE.search(_text):
            matched_by = "text-regex"
        elif _doc_name and _ATTACHMENT_RE.search(_doc_name):
            matched_by = "doc-filename"
        elif _text and _REPRINT_RE.search(_text):
            matched_by = "reprint-phrase"
        if not matched_by:
            if _doc_name:
                # A document arrived and NOTHING matched. For this operator's
                # bot that is the anomaly worth shouting about: it is exactly
                # how a silent extension gap or an upstream raw-shape change
                # would present (live 2026-07-21: the miss produced no log at
                # all and took a session of archaeology to find).
                logger.warning(
                    "snapmaker_u1 attachment_router: document %r arrived but "
                    "matched no kit pattern; u1_kit was NOT recommended",
                    _doc_name,
                )
            return None
        # Full skill for new sessions (the gateway's loader only injects on
        # a session's first message). Never overwrite a topic binding's pick.
        if not getattr(event, "auto_skill", None):
            try:
                event.auto_skill = skill_identifier
                logger.info(
                    "snapmaker_u1 attachment_router: set event.auto_skill=%r (via %s)",
                    skill_identifier, matched_by,
                )
            except Exception as exc:
                logger.warning(
                    "snapmaker_u1 attachment_router: failed to set auto_skill: %s", exc,
                )
        # Per-turn directive for attachment matches, REGARDLESS of auto_skill
        # state: mid-session the loader drops auto_skill (not a new session),
        # and a topic binding's auto_skill is dropped the same way, so the
        # channel_prompt directive is what actually reaches the model turn.
        if matched_by in ("text-regex", "doc-filename"):
            if _arm_kit_directive(event):
                logger.info(
                    "snapmaker_u1 attachment_router: kit directive armed (via %s)",
                    matched_by,
                )
        return None  # let dispatch continue normally
    return handler
