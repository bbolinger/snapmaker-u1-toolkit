"""Strip leaked chat-template control tokens from the model's visible reply.

Why this exists
---------------
The local models this toolkit is documented to run (gemma4 over Ollama) are
imported with a bare ``{{ .Prompt }}`` Ollama template - every gemma4 variant
ships the same passthrough template. A reasoning-formatted model with no
template to structure or parse its channels leaks the raw delimiters
(``<|channel|>``, ``<channel|>``, ``<|tool_call|>`` ...) straight into the
assistant's content, most often as a ``thought <channel|>`` prefix on the
operator's message. Measured on the real transcript store: the leak lands on
roughly 75% of tool-call turns over Ollama's ``/v1`` compatibility endpoint and
25-40% over native ``/api/chat`` - neither is clean, and it happens on any turn
(errors, form-open, plain replies), not only on the image card.

It is a serving/import defect, not a toolkit bug and not a fundamental limit of
the model: a correctly-templated import would not leak. But the toolkit should
not trust model output either way, so this removes the known control tokens from
the VISIBLE text before the operator ever sees them. It is deliberately
model-agnostic (any client's reply passes through) and endpoint-agnostic.

Scope / non-goals
-----------------
This cleans the operator's readable message only. It never touches the safety
gate (which validates real gcode, never model text), a structured tool call, or
a file path. A turn whose ENTIRE content is a leak (no real text) is left to the
caller - there is nothing to show, and blanking a reply is not this function's
call. The ``call:terminal{...}`` shape (a tool call the serializer failed to
parse into a real call) is a functional failure, not cosmetic, so it is
intentionally NOT stripped here - hiding it would hide a dropped action.
"""
from __future__ import annotations

import re

# Known harmony / channel control-token names. Matched both correctly-delimited
# (``<|channel|>``) and in the leaked half-delimited form (``<channel|>``) the
# serializer emits. Restricted to a fixed allowlist of token names so an ordinary
# ``<|something|>`` in prose is never touched - conservative on purpose.
_LEAK_TOKEN_RE = re.compile(
    r"<\|?(?:channel|tool_call|message|start|end|final|analysis|commentary|"
    r"constrain|assistant|user|system|return|think|thinking)\|>"
)
# The ``<|"|>`` fragment seen jammed into leaked ``call:terminal{command:<|"|>...``
_QUOTE_GARBAGE_RE = re.compile(r'<\|"\|>')
# A leading reasoning label ("thought") that directly precedes a channel token
# is part of the leak, not prose. Only stripped when a channel/analysis/think
# token follows it, so a real sentence starting with "Thought ..." is untouched.
_LEADING_THOUGHT_RE = re.compile(
    r"^\s*thought\s+(?=<\|?(?:channel|message|analysis|think|thinking)\|>)",
    re.IGNORECASE,
)


def sanitize_channel_leak(text: str) -> str:
    """Return ``text`` with leaked chat-template control tokens removed.

    Fast-paths clean text: if the reply carries no ``|>`` control-token marker it
    is returned unchanged (the overwhelmingly common case, so a normal message is
    never rewritten). Otherwise the leading reasoning label, every known control
    token, and the ``<|"|>`` fragment are removed, and the whitespace the removals
    leave behind is tidied. Returns an empty string when the content was nothing
    but a leak - the caller decides what an empty result means.
    """
    if not text or "|>" not in text:
        return text
    cleaned = _LEADING_THOUGHT_RE.sub("", text, count=1)
    cleaned = _LEAK_TOKEN_RE.sub("", cleaned)
    cleaned = _QUOTE_GARBAGE_RE.sub("", cleaned)
    # Tidy the gaps the removals leave: runs of spaces, trailing spaces on a
    # line, and 3+ blank lines collapsed to a single paragraph break.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()
