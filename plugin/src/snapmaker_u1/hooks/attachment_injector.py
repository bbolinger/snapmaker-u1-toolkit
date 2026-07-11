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
import math
import os
import re
import time
import uuid
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
# Allow a little clock slack for a marker stamped microseconds in the "future".
_CLOCK_SKEW_S = 60.0

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")

# The marker file is writable by anything running as the same uid, so its paths
# are NOT trusted. Before handing a path to core (which will send it to the
# operator), it must resolve to a real, non-symlink file that is a KNOWN U1
# artifact by name, sitting in a request-id directory under the canonical
# requests root. Audit 2026-07-10: a forged marker injected "/etc/hosts" and the
# hook would have delivered it. We validate the destination, not the marker.
_REQUESTS_ROOT = Path(
    os.environ.get("SNAPMAKER_U1_DATA_DIR", "/opt/data/snapmaker_u1")) / "requests"
# Exact artifact basenames the workflow ever emits (plate N preview/isometric,
# the fresh bed snapshot, the parts grid, and the review doc). Nothing else.
_ALLOWED_ARTIFACT_RE = re.compile(
    r"^(?:plate_\d+_preview\.png|plate_\d+_iso\.png|bed_snapshot\.jpg|"
    r"parts_thumbnails\.png|review\.md)$")
# A request-id directory name (the immediate parent of an artifact). Reprints
# reuse the ORIGINAL request's artifacts, so the parent is not necessarily the
# marker's own request_id -- any valid request dir under the root is acceptable,
# which is why we match the shape rather than the exact id.
_REQUEST_ID_RE = re.compile(r"^u1_\d{4}_\d{4}_[a-z0-9]+$")

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
    if not key:
        # Refuse the empty-key shared slot: without a stable per-session key,
        # every session hashes to the same file and one request's marker could
        # be redeemed by an unrelated turn (audit #3). No key -> no marker.
        return None
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return _PENDING_ATTACH_DIR / f"{digest}.json"


def _load_and_consume_marker() -> dict[str, Any] | None:
    """Atomically claim the marker for this session and return it, or None.

    Single-use is enforced by an atomic rename to a unique name: only the caller
    whose ``os.rename`` wins may read it, so two concurrent finalizers cannot
    both consume the same marker (audit #4 -- read-then-unlink let both win).
    Never raises."""
    path = _marker_path_for_session()
    if path is None:
        return None
    claimed = path.with_name(path.name + f".claimed.{uuid.uuid4().hex[:8]}")
    try:
        os.rename(path, claimed)  # atomic on POSIX; loser gets FileNotFoundError
    except (FileNotFoundError, OSError):
        return None
    try:
        raw = claimed.read_text()
    except OSError:
        raw = ""
    finally:
        try:
            claimed.unlink()
        except OSError:
            pass
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    # created_at must be a finite number in a sane window. A missing or string
    # timestamp previously skipped the TTL check entirely and was accepted as
    # fresh (audit #6); bool is an int subclass, so exclude it explicitly.
    created = data.get("created_at")
    if (not isinstance(created, (int, float)) or isinstance(created, bool)
            or not math.isfinite(created)):
        logger.info("snapmaker_u1 attachment_injector: rejecting marker with "
                    "missing/malformed created_at")
        return None
    age = time.time() - created
    if age > _PENDING_ATTACH_TTL_S or age < -_CLOCK_SKEW_S:
        logger.info("snapmaker_u1 attachment_injector: discarding stale/future "
                    "marker (age %.0fs)", age)
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


def _is_safe_artifact(path_str: str) -> bool:
    """True only for a real, non-symlink file that is a known U1 artifact by name,
    inside a request-id directory under the canonical requests root.

    The marker is same-uid-writable and therefore untrusted; this is what stops a
    forged marker turning the hook into a "send any readable local file" primitive
    (audit #2). resolve(strict=True) follows every symlink, so a symlinked
    component pointing outside the root is caught by the final under-root check."""
    try:
        p = Path(path_str)
        if p.is_symlink():
            return False
        rp = p.resolve(strict=True)
        if not rp.is_file():
            return False
        if not _ALLOWED_ARTIFACT_RE.match(rp.name):
            return False
        if not _REQUEST_ID_RE.match(rp.parent.name):
            return False
        return _REQUESTS_ROOT.resolve() in rp.parents
    except (OSError, ValueError, RuntimeError):
        return False


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

        # Authoritative attachments = images + documents, in that order. Every
        # path is validated (real, non-symlink, known artifact name, under the
        # requests root) because the marker is untrusted (audit #2). Anything
        # else is dropped and logged, never delivered.
        paths: list[str] = []
        for p in ((marker.get("images", []) or [])
                  + (marker.get("documents", []) or [])):
            sp = str(p) if p else ""
            if not sp or sp in paths:
                continue
            if _is_safe_artifact(sp):
                paths.append(sp)
            else:
                logger.warning("snapmaker_u1 attachment_injector: refusing path "
                               "that is not a known U1 artifact under the "
                               "requests root: %r", sp)

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
