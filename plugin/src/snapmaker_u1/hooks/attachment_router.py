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
            _raw = getattr(event, "raw_message", None)
            _has_doc = bool(_raw) and hasattr(_raw, "document") and _raw.document is not None
            _doc_name = getattr(_raw.document, "file_name", None) if _has_doc else None
            logger.debug(
                "snapmaker_u1 attachment_router: text_len=%d has_doc=%s doc_name=%r "
                "current_auto_skill=%r",
                len(_text), _has_doc, _doc_name, getattr(event, "auto_skill", None),
            )
        except Exception as _exc:
            logger.warning("snapmaker_u1 attachment_router: event inspect failed: %s", _exc)
            _text = ""
            _has_doc = False
            _doc_name = None
        # Don't overwrite an already-set auto_skill.
        if getattr(event, "auto_skill", None):
            return None
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
            return None
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
        return None  # let dispatch continue normally
    return handler
