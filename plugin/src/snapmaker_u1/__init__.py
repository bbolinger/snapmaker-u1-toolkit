"""snapmaker_u1 — Hermes plugin: auto-trigger the U1 slicing skill on 3D-model attachments.

Why this exists: a small local model can't be trusted to remember to load the
right skill when a print job arrives. This plugin makes that mechanical.

Architecture (hermes-agent v0.17):

  1. Plugin registers a `pre_gateway_dispatch` hook.
  2. On each inbound MessageEvent, the hook checks the raw attachment filename
     and the visible message text for a 3D-model extension (.stl / .3mf / .zip).
  3. When matched, the hook sets `event.auto_skill = "3d-printer-slicing-automation"`
     (or, if the bundled skill is registered, `snapmaker_u1:3d-printer-slicing-automation`).
  4. The gateway's existing auto-skill plumbing injects the full SKILL.md body
     as the first user message of the session — the same path topic-bound
     skills use. The model sees the entire skill from turn 1, no `skill_view`
     round-trip needed.

Skill discovery:
  Plugin-bundled SKILL.md first (the namespaced form); falls back to the
  deployed copy under ~/.hermes/skills (where deploy_to_runtime.sh puts it).
  This keeps the plugin pip-installable while matching the standard deploy.

Distribution: `pip install -e ./plugin/` from the repo root.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Skill we trigger
SKILL_NAME = "3d-printer-slicing-automation"

# Candidate paths to the SKILL.md, in priority order.
# - First entry: bundled inside the plugin package (for distribution)
# - Fallback: the standard deployed location (deploy_to_runtime.sh)
_PLUGIN_SKILL_DIR = Path(__file__).resolve().parent / "skills" / SKILL_NAME
_DEPLOYED_SKILL_DIR = Path("/opt/data/skills/hardware-automation") / SKILL_NAME


def _resolve_skill_path() -> Path | None:
    """Return the first SKILL.md path that exists, or None if neither does."""
    for d in (_PLUGIN_SKILL_DIR, _DEPLOYED_SKILL_DIR):
        candidate = d / "SKILL.md"
        if candidate.is_file():
            return candidate
    return None


def register(ctx: Any) -> None:
    """Plugin entry point. Called by Hermes' plugin manager on startup."""
    from .hooks.attachment_router import make_handler

    skill_path = _resolve_skill_path()
    skill_identifier = SKILL_NAME  # default: flat name (deployed in ~/.hermes/skills/)

    if skill_path and skill_path.parent == _PLUGIN_SKILL_DIR:
        # Bundled version found — register it via plugin namespacing.
        try:
            ctx.register_skill(
                SKILL_NAME,
                skill_path,
                description=(
                    "Snapmaker U1 staged slicing workflow (orient → render → slice → "
                    "upload → camera-gated start). Plugin-bundled."
                ),
            )
            # Namespaced identifier — gateway's _load_skill_payload → skill_view
            # resolves `<plugin>:<skill>` per tools/skills_tool.py docstring.
            skill_identifier = "snapmaker_u1:" + SKILL_NAME
            logger.info(
                "snapmaker_u1 plugin: registered bundled skill %r at %s",
                skill_identifier, skill_path,
            )
        except Exception as exc:
            logger.warning(
                "snapmaker_u1 plugin: register_skill failed (%s); falling back to "
                "deployed copy via flat name '%s'", exc, SKILL_NAME,
            )
            skill_identifier = SKILL_NAME

    elif skill_path:
        logger.info(
            "snapmaker_u1 plugin: using deployed SKILL.md at %s (skill auto-load "
            "will resolve flat name '%s' via the index)", skill_path, SKILL_NAME,
        )
    else:
        logger.warning(
            "snapmaker_u1 plugin: SKILL.md not found in plugin bundle (%s) OR at "
            "deployed path (%s). Hook will still set auto_skill='%s' but Hermes "
            "may log 'Auto-skill not found' at session start.",
            _PLUGIN_SKILL_DIR, _DEPLOYED_SKILL_DIR, SKILL_NAME,
        )

    # Register the attachment-detection hook with the resolved skill identifier.
    handler = make_handler(skill_identifier)
    ctx.register_hook("pre_gateway_dispatch", handler)
    logger.info(
        "snapmaker_u1 plugin: pre_gateway_dispatch hook registered (skill='%s')",
        skill_identifier,
    )

    # Register the transform_tool_result hook that prevents anti-pattern #7
    # (Gemma printing next_action_required.command as text instead of
    # tool-calling it). Observed live 2026-06-29 in session
    # 20260629_220708_5e63b52f; hook prepends a strongly directive prefix to
    # the terminal tool output when it contains a next_action_required event.
    from .hooks.next_action_directive import transform as next_action_transform
    ctx.register_hook("transform_tool_result", next_action_transform)
    logger.info(
        "snapmaker_u1 plugin: transform_tool_result hook registered "
        "(next_action_required directive)",
    )
