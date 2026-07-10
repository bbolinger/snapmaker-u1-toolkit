"""transform_llm_output hook — attach U1 images structurally, not by model echo.

The problem this closes
-----------------------
Hermes core (gateway/platforms/base.py) delivers images by scanning the agent's
FINAL reply text for bare local file paths and turning any that exist on disk
into native Telegram photos (extract_local_files / extract_images strip the path
from the visible text and send it as media). The U1 readiness / bed-clear /
reprint card therefore depended on gemma4 echoing the workflow's image paths
verbatim. Observed live 2026-07-09 (reprint): the model rebuilt the paths from
a mangled request_id (`u1_2026...` became `u2026...`), so the paths pointed at
nothing, nothing attached, and the operator saw the bed photo and previews as
raw text. Losing the bed photo means losing the visual bed-clear check.

The fix (same model-free philosophy as the YES boundary)
--------------------------------------------------------
The workflow OWNS the real image paths in the request dir. When it emits an
image-bearing card it drops a one-shot marker keyed by ``HERMES_SESSION_KEY``
(the workflow subprocess inherits it; the gateway process where this hook runs
sets it at run.py's dispatch, so both sides read the identical value). This hook
reads that marker on the outbound turn and:

  1. strips any U1 image path the model echoed (correct OR mangled) so a dead
     path can never leak into the visible reply, then
  2. appends the AUTHORITATIVE paths (that actually exist on disk) as bare
     lines, so core attaches the true images regardless of what the model typed.

The marker is consumed one-shot. The safety gate is untouched — it validates
real gcode, never an image.

Contract (agent/turn_finalizer.py): the hook is called with
``response_text``, ``session_id``, ``model``, ``platform``; returning a non-empty
string replaces the reply, first hook to return one wins. Any failure here MUST
degrade to leaving the reply unchanged (return None) — an attachment is never
worth breaking the operator's message.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Marker location + schema. Mirrors the workflow's _arm_pending_attach in
# scripts/u1_kit_workflow.py (kept in lockstep by these two comments, the same
# way the pending_confirm marker is shared with the u1_confirm_start hook).
#   dir     : $U1_PENDING_ATTACH_DIR (default /tmp/u1_pending_attach)
#   file    : sha256(HERMES_SESSION_KEY)[:16].json
#   content : {"request_id": str, "images": [abs path, ...],
#              "operator": str|None, "created_at": float}
_PENDING_ATTACH_DIR = Path(
    os.environ.get("U1_PENDING_ATTACH_DIR", "/tmp/u1_pending_attach"))
# Images are only relevant to the immediate reply; a marker older than this is
# stale (a crash or a non-card turn) and is discarded rather than re-attached.
_PENDING_ATTACH_TTL_S = 120.0

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")

# A whitespace-delimited token "looks like a U1 image path" when it ends in an
# image extension AND carries a U1 signature. This strips both the correct paths
# and the mangled ones (e.g. a corrupted request id) so neither survives as text.
# Signatures are kept specific to U1 artifacts so an unrelated image path the
# model might mention (e.g. /photos/sunset_preview.png) is left alone. The real
# U1 images are plate_<n>_preview.png / plate_<n>_iso.png / bed_snapshot.jpg /
# parts_thumbnails.png, all of which carry "plate_", "bed_snapshot" or
# "parts_thumbnail", and every one lives under a "snapmaker_u1"/"/requests/"
# path, so a generic "_preview"/"_iso" substring is deliberately NOT a signature.
_U1_SIGNATURES = (
    "snapmaker_u1", "/requests/", "plate_", "bed_snapshot", "parts_thumbnail",
)


def _marker_path_for_session() -> Path | None:
    key = os.environ.get("HERMES_SESSION_KEY", "")
    # Empty key still hashes deterministically; on a single-operator U1 install
    # that simply means one shared slot, which is correct (one operator).
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return _PENDING_ATTACH_DIR / f"{digest}.json"


def _load_and_consume_marker() -> dict[str, Any] | None:
    """Return the marker for this session and delete it (one-shot). None if
    absent or stale. Never raises."""
    path = _marker_path_for_session()
    if path is None:
        return None
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    # Consume immediately so a half-processed / stale marker can't be re-used
    # on a later turn.
    try:
        path.unlink()
    except OSError:
        pass
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    created = data.get("created_at")
    if isinstance(created, (int, float)) and (time.time() - created) > _PENDING_ATTACH_TTL_S:
        logger.info("snapmaker_u1 attachment_injector: discarding stale marker "
                    "(age %.0fs > %.0fs)", time.time() - created, _PENDING_ATTACH_TTL_S)
        return None
    return data


def _is_u1_artifact_path(token: str) -> bool:
    low = token.lower()
    # Any token that references a U1 request path is ours to remove, even when
    # the model splits or corrupts it. Gemma has been seen to jam a space and a
    # '$' into the path ("requests/ $u1_..._8dfe85/bed_snapshot.jpg", live
    # 2026-07-09), which whitespace-splits into a bare "/snapmaker_u1/requests/"
    # prefix plus a "$..._8dfe85/bed_snapshot.jpg" tail. Catch BOTH: any token
    # holding the request-path fragment, plus any image-extension token with a
    # U1 filename signature. Neither should survive into the visible reply.
    if "/snapmaker_u1/requests" in low or "snapmaker_u1/requests" in low:
        return True
    if not low.endswith(_IMAGE_EXTS):
        return False
    return any(sig in low for sig in _U1_SIGNATURES)


def _strip_echoed_u1_images(text: str) -> str:
    """Remove any U1 image path the model wrote, correct or mangled. Operates on
    whitespace tokens so a path on its own line is removed cleanly; collapses the
    blank lines that leaves behind."""
    if not text:
        return text
    out_lines: list[str] = []
    for line in text.splitlines():
        toks = line.split()
        removed = any(_is_u1_artifact_path(t) for t in toks)
        kept = [t for t in toks if not _is_u1_artifact_path(t)]
        if removed:
            # When we pulled a U1 path off a line, drop an orphaned "MEDIA:"
            # directive keyword it was riding on (the model wraps the review doc
            # as "MEDIA: <path>"; without this the bare "MEDIA:" would leak).
            kept = [t for t in kept if t.upper() != "MEDIA:"]
        # A line that was ONLY a path (or several) collapses to empty; drop it.
        # A line with prose plus a trailing path keeps the prose.
        if not toks:
            out_lines.append(line)
        elif kept:
            out_lines.append(" ".join(kept))
        # else: line was purely path(s)/MEDIA: -> omit entirely
    cleaned = "\n".join(out_lines)
    # Collapse 3+ newlines left by removed blocks down to a paragraph break.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def transform(response_text: str = "", session_id: str = "", model: str = "",
              platform: str = "", **kwargs: Any) -> Any:
    """Inject authoritative U1 attachment paths so core delivers the real files.

    The marker carries images (plate preview / isometric / bed snapshot) and
    documents (the review doc). Both are injected as BARE paths: core's
    extract_local_files sends image extensions as native photos and everything
    else (the .md review doc) through send_document. A .md can only ride the
    bare-path route -- the MEDIA: directive has an extension allowlist that
    excludes it -- which is why the model echoing "MEDIA: ...review.md" never
    delivered the doc.

    Returns a replacement string, or None to leave the reply unchanged.
    """
    try:
        marker = _load_and_consume_marker()
        if not marker:
            return None  # no U1 card this turn -> no-op

        # Authoritative attachments = images + documents, in that order, keeping
        # only what exists on disk right now.
        paths: list[str] = []
        for p in ((marker.get("images", []) or [])
                  + (marker.get("documents", []) or [])):
            try:
                if p and Path(p).is_file() and p not in paths:
                    paths.append(str(p))
            except OSError:
                continue

        cleaned = _strip_echoed_u1_images(response_text or "")

        if not paths:
            # Marker existed but nothing survives on disk. Return the cleaned
            # text only if we actually removed a dead/mangled path AND something
            # is left to send. A bare "" would be treated by turn_finalizer as
            # "leave unchanged" (falsy), so never return that.
            if cleaned and cleaned != (response_text or "").strip():
                logger.info("snapmaker_u1 attachment_injector: no live files; "
                            "stripped echoed path(s) from reply "
                            "(request_id=%s)", marker.get("request_id"))
                return cleaned
            return None

        # Append the real paths as bare lines. Core's extract_local_files will
        # deliver each (photo for images, document for the review .md) and remove
        # the line from the visible text, so the operator sees the card text plus
        # the attachments, never a path.
        body = cleaned.rstrip()
        new_text = (body + "\n\n" if body else "") + "\n".join(paths)
        logger.info(
            "snapmaker_u1 attachment_injector: injected %d authoritative "
            "file path(s) (request_id=%s), replacing model-echoed paths",
            len(paths), marker.get("request_id"),
        )
        return new_text
    except Exception as exc:  # never break the operator's reply over an attachment
        logger.warning("snapmaker_u1 attachment_injector: failed, leaving reply "
                       "unchanged: %s", exc)
        return None
