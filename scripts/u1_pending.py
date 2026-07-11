"""Shared resolver for the pending-marker directories (confirm/cancel/attach).

The model-free control surfaces meet across process boundaries through
tiny marker files: the workflow arms a confirm/attach marker that gateway
hooks redeem, the notify script and Telegram button write cancel markers
the gate polls. Every side MUST resolve the same directory or markers are
silently dropped — for cancel that means a dead CANCEL button, the exact
2026-07-09 incident class.

Resolution order (per marker kind):
  1. U1_PENDING_<KIND>_DIR   — explicit per-kind override (tests, migration)
  2. U1_PENDING_STATE_DIR    — one root for all kinds, <root>/<kind>
  3. <tempdir>/u1_pending/<kind> — default; tempfile.gettempdir() so native
     Windows lands on %TEMP% (consistent across all native processes)
     instead of a literal /tmp that Git Bash and native Python map to
     different places.

KEEP IN SYNC — this rule is duplicated where imports can't reach:
  plugin/src/snapmaker_u1/pending.py          (gateway-side pip package)
  tools/hermes_hooks/u1_confirm_start/handler.py
  tools/hermes_hooks/u1_grace_cancel/handler.py
  adapters/hermes/plugin/telegram_patch.py
  tools/u1_grace_notify_hermes.sh             (bash fallback; the gate also
                                               passes the resolved dir via env)
tests/test_pending_paths.py asserts all copies resolve identically.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def pending_dir(kind: str) -> Path:
    """Directory holding pending ``<kind>`` markers. Not created here —
    writers mkdir on arm, readers treat a missing dir as no-markers."""
    explicit = os.environ.get(f"U1_PENDING_{kind.upper()}_DIR", "").strip()
    if explicit:
        return Path(explicit)
    root = os.environ.get("U1_PENDING_STATE_DIR", "").strip()
    if root:
        return Path(root) / kind
    return Path(tempfile.gettempdir()) / "u1_pending" / kind
