"""Tests for the leaked chat-template token sanitizer.

Strings marked "real" are taken verbatim from the live transcript store
(state.db) where gemma4 over Ollama leaked the token into the operator's reply.
"""
import sys
from pathlib import Path

import pytest

_PLUGIN_SRC = Path(__file__).resolve().parent.parent / "plugin" / "src"
sys.path.insert(0, str(_PLUGIN_SRC))

from snapmaker_u1.hooks.channel_sanitizer import sanitize_channel_leak  # noqa: E402
from snapmaker_u1.hooks import attachment_injector as ai  # noqa: E402


# --- the sanitizer in isolation -------------------------------------------

def test_bare_leak_only_becomes_empty():
    # real: assistant content stored as exactly "thought <channel|>" (len 18)
    assert sanitize_channel_leak("thought <channel|>") == ""


def test_leak_prefix_before_real_text_keeps_the_text():
    # real shape: "thought <channel|>On it - running the Snapmaker U1 workflow..."
    assert sanitize_channel_leak(
        "thought <channel|>On it, running the workflow.") == "On it, running the workflow."


def test_bare_channel_prefix_stripped():
    # real: "<channel|>The toolkit was unable to process this model..."
    assert sanitize_channel_leak(
        "<channel|>The toolkit was unable to process this model.") == \
        "The toolkit was unable to process this model."


def test_leak_prefix_before_a_path_keeps_the_path():
    # real: "<channel|>/opt/data/snapmaker_u1/requests/u1_2026_0709_4bcf1f/plate_1_preview.png"
    p = "/opt/data/snapmaker_u1/requests/u1_2026_0709_4bcf1f/plate_1_preview.png"
    assert sanitize_channel_leak("thought <channel|>" + p) == p


def test_full_pipe_delimited_tokens_removed():
    assert sanitize_channel_leak("Do it <|tool_call|> now.") == "Do it now."


def test_quote_garbage_fragment_removed():
    assert sanitize_channel_leak('call it <|"|> here') == "call it here"


def test_clean_text_is_returned_unchanged():
    msg = "The bed is clear. Reply YES to start now, or NO to keep the gcode."
    assert sanitize_channel_leak(msg) == msg


def test_leading_word_thought_in_prose_is_untouched():
    # "Thought" as a real word (no channel token after it) must survive.
    msg = "Thought about the orientation, laying it flat is best."
    assert sanitize_channel_leak(msg) == msg


def test_unknown_pipe_token_is_not_stripped():
    # Conservative: only the known control-token names are removed.
    msg = "Use the <|widget|> carefully."
    assert sanitize_channel_leak(msg) == msg


def test_empty_and_none_safe():
    assert sanitize_channel_leak("") == ""
    assert sanitize_channel_leak(None) is None


def test_idempotent():
    once = sanitize_channel_leak("thought <channel|>All set.")
    assert sanitize_channel_leak(once) == once == "All set."


# --- wired into the outbound hook -----------------------------------------

def test_transform_strips_leak_when_no_card(monkeypatch):
    # No marker this turn: the hook must still clean the leaked token.
    monkeypatch.setattr(ai, "_load_and_consume_marker", lambda: None)
    out = ai.transform(response_text="thought <channel|>Bed is clear, ready to print.")
    assert out == "Bed is clear, ready to print."


def test_transform_clean_reply_no_card_is_noop(monkeypatch):
    monkeypatch.setattr(ai, "_load_and_consume_marker", lambda: None)
    assert ai.transform(response_text="Bed is clear, ready to print.") is None


def test_transform_wholly_leaked_reply_left_unchanged(monkeypatch):
    # Sanitizes to "" -> returning "" would blank the message; hook returns None.
    monkeypatch.setattr(ai, "_load_and_consume_marker", lambda: None)
    assert ai.transform(response_text="thought <channel|>") is None
