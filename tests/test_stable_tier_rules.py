"""The stable-tier rules (HERMES.md) outrank the volatile skill text in
Hermes' prompt assembly. Audit 2026-07-07 caught them still teaching the
pre-v2.3 model-relayed start flow AFTER the start had been made model-free:
the strongest prompt layer was instructing the model to do exactly what the
boundary forbids. These tests fail the build if any legacy start-capability
phrase reappears in a model-facing instruction file."""
from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent

# Phrases that only existed in the model-relayed start architecture. Any
# one of them in a model-facing file means the model is being taught to
# compose or relay a start again.
_FORBIDDEN = (
    "--bed-clear start",
    "--approval-token",
    "next_command_on_yes",
    "start_gate_stage1_command",
    "--confirm-start ",
)

_MODEL_FACING = (
    "HERMES.md",
    "skills/3d-printer-slicing-automation/SKILL.md",
)


@pytest.mark.parametrize("relpath", _MODEL_FACING)
def test_no_legacy_start_phrases(relpath):
    text = (_ROOT / relpath).read_text()
    hits = [ph for ph in _FORBIDDEN if ph in text]
    assert not hits, (
        f"{relpath} teaches the model a start capability again: {hits}. "
        "The start transition is model-free (see tools/hermes_hooks/"
        "u1_confirm_start); rewrite the instruction instead of relaying "
        "commands.")


def test_hermes_md_states_the_model_free_rule():
    text = (_ROOT / "HERMES.md").read_text()
    assert "model-free" in text and "--grace-cancel" in text, (
        "HERMES.md must state the model-free start rule and the one "
        "permitted safe-direction fallback")
