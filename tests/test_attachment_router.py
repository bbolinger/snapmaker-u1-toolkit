"""attachment_router: a 3D-model attachment must reach the model as a u1_kit
instruction on EVERY turn, not just a session's first message.

The gateway's auto-skill loader injects the skill body only on new sessions,
so a kit uploaded mid-conversation used to leave the model with no idea
u1_kit existed (live 2026-07-21: it hand-unzipped the attachment and hung on
the interactive prompt). The router now arms a per-turn channel_prompt
directive alongside auto_skill, detects document filenames in both the
object and dict raw-message shapes, and logs a warning when a document
arrives that matches nothing.
"""
from __future__ import annotations

from types import SimpleNamespace

from snapmaker_u1.hooks import attachment_router as ar

SKILL = "3d-printer-slicing-automation"


def _handler():
    return ar.make_handler(SKILL)


def _event(text="", doc_name=None, raw_shape="object", auto_skill=None,
           channel_prompt=None):
    raw = None
    if doc_name is not None:
        if raw_shape == "object":
            raw = SimpleNamespace(document=SimpleNamespace(file_name=doc_name))
        else:
            raw = {"document": {"file_name": doc_name}}
    return SimpleNamespace(text=text, raw_message=raw, auto_skill=auto_skill,
                           channel_prompt=channel_prompt)


# ---------- detection ----------

def test_zip_document_sets_skill_and_arms_directive():
    ev = _event(doc_name="angles-teaching-aid-model_files.zip")
    _handler()(event=ev)
    assert ev.auto_skill == SKILL
    assert ar._KIT_DIRECTIVE_TAG in (ev.channel_prompt or "")
    assert "u1_kit" in ev.channel_prompt


def test_dict_shaped_raw_message_is_detected():
    """Upstream adapters have passed both an object graph and the bare Bot
    API dict; a raw-shape change must not silently disable kit detection."""
    ev = _event(doc_name="kit.stl", raw_shape="dict")
    _handler()(event=ev)
    assert ev.auto_skill == SKILL
    assert ar._KIT_DIRECTIVE_TAG in ev.channel_prompt


def test_text_path_mention_is_detected():
    ev = _event(text="please print /opt/data/cache/documents/doc_ab12_kit.3mf")
    _handler()(event=ev)
    assert ev.auto_skill == SKILL
    assert ar._KIT_DIRECTIVE_TAG in ev.channel_prompt


def test_non_model_document_matches_nothing_and_warns(caplog):
    import logging
    ev = _event(doc_name="invoice.pdf")
    with caplog.at_level(logging.WARNING):
        _handler()(event=ev)
    assert ev.auto_skill is None
    assert ev.channel_prompt is None
    assert any("matched no kit pattern" in r.message for r in caplog.records)


def test_plain_chat_mutates_nothing():
    ev = _event(text="how was the last print?")
    _handler()(event=ev)
    assert ev.auto_skill is None
    assert ev.channel_prompt is None


def test_stl_pipeline_style_name_is_not_a_false_positive():
    ev = _event(text="the stl_pipeline.py script broke again")
    _handler()(event=ev)
    assert ev.auto_skill is None
    assert ev.channel_prompt is None


# ---------- mid-session and binding interplay ----------

def test_directive_arms_even_when_auto_skill_already_bound():
    """A topic binding's auto_skill is dropped by the loader mid-session the
    same way ours is, so the directive must arm regardless. The bound skill
    itself is never overwritten."""
    ev = _event(doc_name="kit.zip", auto_skill="some-topic-skill")
    _handler()(event=ev)
    assert ev.auto_skill == "some-topic-skill"
    assert ar._KIT_DIRECTIVE_TAG in ev.channel_prompt


def test_directive_appends_to_existing_channel_prompt():
    ev = _event(doc_name="kit.zip", channel_prompt="You are the print bot.")
    _handler()(event=ev)
    assert ev.channel_prompt.startswith("You are the print bot.")
    assert ar._KIT_DIRECTIVE_TAG in ev.channel_prompt


def test_directive_is_idempotent():
    ev = _event(doc_name="kit.zip")
    _handler()(event=ev)
    once = ev.channel_prompt
    _handler()(event=ev)
    assert ev.channel_prompt == once


# ---------- reprint keeps its existing behavior ----------

def test_reprint_phrase_loads_skill_without_directive():
    ev = _event(text="reprint the last one")
    _handler()(event=ev)
    assert ev.auto_skill == SKILL
    assert ev.channel_prompt is None  # no attachment, no directive


# ---------- resilience ----------

def test_none_event_is_a_noop():
    assert _handler()(event=None) is None


def test_broken_event_never_raises():
    class Hostile:
        @property
        def text(self):
            raise RuntimeError("boom")

        raw_message = None

    assert _handler()(event=Hostile()) is None
