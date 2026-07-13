"""Pending-marker directory resolution — gateway-side copy.

KEEP IN SYNC with scripts/u1_pending.py (the canonical copy, whose
docstring explains the rule). This package runs inside the Hermes gateway
where the toolkit's scripts/ dir is not importable, so the ~10-line rule
is duplicated rather than imported; tests/test_pending_paths.py asserts
every copy resolves identically.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def pending_dir(kind: str) -> Path:
    explicit = os.environ.get(f"U1_PENDING_{kind.upper()}_DIR", "").strip()
    if explicit:
        return Path(explicit)
    root = os.environ.get("U1_PENDING_STATE_DIR", "").strip()
    if root:
        return Path(root) / kind
    return Path(tempfile.gettempdir()) / "u1_pending" / kind
