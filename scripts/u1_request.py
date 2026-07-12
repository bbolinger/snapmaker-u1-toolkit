#!/usr/bin/env python3
"""Print Request Object — v2.0 Phase 2.

A print request is a first-class entity with a stable, human-readable
`request_id` (e.g. ``u1_2026_0626_abc123``). The workflow writes one
request per print job to ``<data_dir>/requests/<request_id>/``, including
all artifacts (model, gcode, preview, bed photo, events, audit). Approval
flows attach to the ID rather than to vague "yes/no" answers — which is
what makes the approval auditable AND what makes the workflow resumable
when the agent loses conversation context mid-flow.

This module is the stdlib-only helper layer. Public surface:

  generate_request_id(stl: Path, prefix='u1') -> str
  request_dir(request_id: str) -> Path
  write_request(request_id, **fields) -> Path
  read_request(request_id) -> dict | None
  find_recent_request_for_model(stl: Path) -> str | None
  compute_model_hash(stl: Path) -> str

The compute_model_hash + find_recent_request_for_model pair is the
context-loss recovery path: when the agent re-runs the workflow with
just an STL path (no --request-id), the workflow looks up an existing
request for that STL's *content* hash. Same path with different content
correctly fails the lookup and starts a new request.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Lazy import so this module imports cleanly even when u1_config can't
# resolve (e.g. during early-test contexts without a configured data dir).
def _data_dir() -> Path:
    from u1_config import get_data_dir
    return get_data_dir()


def _requests_root() -> Path:
    return _data_dir() / 'requests'


# ============================================================================
# Request ID generation
# ============================================================================

# Format: u1_YYYY_MMDD_<6hex>
# u1     = printer model (matches the toolkit's printer scope — frozen at
#          this version; if/when the toolkit supports more printers, a
#          parallel u1_request_<model>.py module is the right pattern,
#          NOT a polymorphic prefix here)
# YYYY   = UTC year, MMDD = UTC month/day — sortable globally across TZs
# 6 hex  = 24 bits of entropy from secrets.token_hex(3) — enough to avoid
#          collision across the ~16M requests/day theoretical ceiling
_REQUEST_ID_PREFIX = 'u1'
_REQUEST_ID_RE = re.compile(rf'^{re.escape(_REQUEST_ID_PREFIX)}_(\d{{4}})_(\d{{4}})_([0-9a-f]{{6}})$')


def generate_request_id() -> str:
    """Return a fresh ``u1_YYYY_MMDD_xxxxxx`` request id.

    The id is **UTC-sortable** by creation date (matters when operators
    span time zones), contains the printer model namespace, and carries
    24 bits of entropy to avoid collisions across parallel sessions.
    """
    today = datetime.now(timezone.utc).strftime('%Y_%m%d')
    suffix = secrets.token_hex(3)
    return f'{_REQUEST_ID_PREFIX}_{today}_{suffix}'


def is_request_id(s: str | None) -> bool:
    """True iff *s* matches the ``u1_YYYY_MMDD_xxxxxx`` shape (lowercase hex)."""
    return bool(s and _REQUEST_ID_RE.match(s))


# ============================================================================
# Per-request directory layout
# ============================================================================

def request_dir(request_id: str) -> Path:
    """Path to ``<data_dir>/requests/<request_id>/`` (does NOT create)."""
    if not is_request_id(request_id):
        raise ValueError(f'invalid request_id: {request_id!r}')
    return _requests_root() / request_id


from contextlib import contextmanager as _contextmanager

from u1_lockfile import exclusive_lock as _exclusive_lock


@_contextmanager
def request_lock(request_id: str):
    """Exclusive per-request lock (u1_lockfile sidecar lock on
    ``<request_dir>/.request.lock``) held across a full read-modify-write.

    Cold-audit finding 2026-07-07: ``write_request`` was atomic against a
    TORN read (os.replace) but not against a LOST UPDATE — process B reads
    the request, process A consumes the single-use Stage 2 nonce under its
    own narrower lock and writes, then B writes its stale full document back
    and RESURRECTS the consumed nonce. Serializing every request read-
    modify-write under one lock closes that race for the nonce and for the
    whole lost-update family (revision bumps, safety-block edits, plate
    state). u1_lockfile backs this with fcntl on POSIX and msvcrt on
    Windows — never a no-op. A lock failure raises rather than silently
    proceeding unlocked — for a safety-state file a missing mutex must
    fail loud, not fail open."""
    ensure_request_dir(request_id)
    with _exclusive_lock(request_dir(request_id) / ".request.lock"):
        yield


def ensure_request_dir(request_id: str) -> Path:
    """Path to the request dir, creating it if missing.

    Self-heal: walk every ancestor we may have just created (from the
    request_id dir up to but NOT past ``_data_dir()``) and align uid/gid
    with the data_dir's. This catches the case where a workflow runs as
    root but the operator's daemon runs as uid 10000 — without the
    walk-up, a freshly created ``requests/`` parent stays root-owned
    and the operator's next write fails with PermissionError.

    Bounded to ``_data_dir()`` so we never chown ancestors outside the
    toolkit's scope. Best-effort; chown failures (CAP_CHOWN not held,
    cross-filesystem, etc.) are swallowed."""
    d = request_dir(request_id)
    d.mkdir(parents=True, exist_ok=True)
    try:
        data_root = _data_dir().resolve()
        st = data_root.stat()
        target_uid, target_gid = st.st_uid, st.st_gid
        cur = d.resolve()
        # Walk from the new dir UP to (but not including) data_root.
        # The `in cur.parents` guard prevents accidental escape if cur
        # somehow points outside the data_root subtree.
        while cur != data_root and data_root in cur.parents:
            cst = cur.stat()
            if cst.st_uid != target_uid or cst.st_gid != target_gid:
                # POSIX-only: Windows os has no chown at all (attribute
                # access raises AttributeError, which the except below
                # would NOT catch — 2026-07-10 Windows validation).
                if hasattr(os, "chown"):
                    os.chown(cur, target_uid, target_gid)
            cur = cur.parent
    except (OSError, PermissionError):
        pass  # best-effort; not authoritative
    return d


# ============================================================================
# request.json read / write
# ============================================================================
# Schema fields the workflow populates over time (none required at write):
#   request_id, created_at, updated_at, model_file, model_path, model_hash,
#   oriented_model_hash, gcode_hash, tool, material, profile, supports,
#   orient, upload_decision, estimated_time, estimated_filament_g,
#   preview_image, bed_photo, phase, answered, next_prompt, status
#
# v3a additions (auto-injected by write_request, no caller change required):
#   schema_version=1, request_revision, approvals.{upload,start},
#   safety.{bed_clear_check_required, bed_clear_photo_captured}, operator

SCHEMA_VERSION = 1

# Plan-affecting fields: a change to ANY of these bumps request_revision.
# Photo/upload/status changes do NOT bump (they don't change WHAT prints).
_PLAN_AFFECTING_FIELDS: tuple[str, ...] = (
    'model_hash', 'orient', 'profile', 'material', 'tool', 'supports',
    'gcode_hash', 'nozzle',
)

_EMPTY_APPROVAL: dict[str, Any] = {
    'approved': False,
    'approved_by': None,
    'approved_at': None,
    'approved_revision': None,
    'approved_gcode_hash': None,
}


def _bump_revision_if_changed(prior: dict[str, Any], new_fields: dict[str, Any]) -> int:
    """Return the request_revision integer to write.

    Rules:
      - First write of a request (no prior ``request_revision``) → 1.
        Initial-setting fields are NOT a "change," they're the baseline.
      - Subsequent write that SETS a plan-affecting field for the first
        time (field absent from prior) → no bump. Setting something for
        the first time is also a baseline, not a change.
      - Subsequent write that MODIFIES a plan-affecting field already
        in prior → +1.
      - Subsequent write that doesn't touch plan-affecting fields → unchanged.
      - Photo/upload/status fields are NOT plan-affecting (don't change WHAT prints).
      - Never bump backward.

    Live harness test 2026-06-28 showed this function bumped on every
    operator answer because the comparison ``new_fields[field] != prior.get(field)``
    saw ``value != None`` for first-time sets. The ``field in prior`` guard
    distinguishes initial set from real plan change.
    """
    if 'request_revision' not in prior:
        # First write — establish baseline at 1. New fields are "initial set,"
        # not "change."
        return 1
    current = prior['request_revision']
    if not isinstance(current, int) or current < 1:
        current = 1
    for field in _PLAN_AFFECTING_FIELDS:
        if field not in new_fields:
            continue
        if field not in prior:
            # Initial set of this field, not a change. Don't bump.
            continue
        if new_fields[field] != prior[field]:
            return current + 1
    return current


def _ensure_schema_defaults(d: dict[str, Any]) -> dict[str, Any]:
    """Fill in v1 schema defaults on a request dict in-place. Idempotent.

    Called from write_request so every persisted request carries the v1
    shape regardless of caller. Safe to call repeatedly: existing values
    are preserved; only missing keys get defaults.
    """
    d.setdefault('schema_version', SCHEMA_VERSION)
    d.setdefault('request_revision', 1)
    approvals = d.get('approvals')
    if not isinstance(approvals, dict):
        approvals = {}
        d['approvals'] = approvals
    for kind in ('upload', 'start'):
        block = approvals.get(kind)
        if not isinstance(block, dict):
            approvals[kind] = dict(_EMPTY_APPROVAL)
        else:
            for k, v in _EMPTY_APPROVAL.items():
                block.setdefault(k, v)
    safety = d.get('safety')
    if not isinstance(safety, dict):
        safety = {}
        d['safety'] = safety
    safety.setdefault('bed_clear_check_required', True)
    safety.setdefault('bed_clear_photo_captured', False)
    return d


def _request_json_path(request_id: str) -> Path:
    return request_dir(request_id) / 'request.json'


def read_request(request_id: str) -> dict[str, Any] | None:
    """Return the parsed request.json contents, or None if missing/invalid.

    Returns raw on-disk contents — does NOT apply v1 schema defaults on
    read. Callers that need the full v1 shape should call write_request()
    afterward (which idempotently fills defaults) or call
    _ensure_schema_defaults() on the returned dict. The one-shot migrator
    script (`scripts/migrate_v0_to_v1.py`) does an in-place rewrite for
    bulk fix-up.
    """
    p = _request_json_path(request_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def write_request(request_id: str, **fields: Any) -> Path:
    """Merge *fields* into the on-disk request.json (creating it if missing).

    Always stamps ``updated_at``, ``schema_version``, ``request_revision``,
    and the ``approvals`` + ``safety`` blocks (idempotent — pre-existing
    values are preserved). Bumps ``request_revision`` automatically when
    *fields* contains a plan-affecting change. Atomic write via mkstemp +
    os.replace so concurrent invocations never see half-written JSON.
    """
    ensure_request_dir(request_id)
    p = _request_json_path(request_id)
    # Hold the per-request lock across read-modify-write so a stale writer
    # cannot clobber a concurrent update (e.g. resurrect a just-consumed
    # Stage 2 nonce) — cold-audit finding 2026-07-07.
    with request_lock(request_id):
        prior = read_request(request_id) or {}
        now = time.time()
        if 'created_at' not in prior:
            prior['created_at'] = now
        # Compute new revision against PRIOR state BEFORE merging new fields.
        new_revision = _bump_revision_if_changed(prior, fields)
        prior.update(fields)
        prior['request_id'] = request_id
        prior['updated_at'] = now
        prior['request_revision'] = new_revision
        _ensure_schema_defaults(prior)
        # Atomic write: write to sibling temp file, then os.replace. The tmp
        # name is uuid-unique (threads share a pid) and the replace retries
        # briefly on PermissionError: on Windows an unlocked read_request
        # holding request.json open denies the swap for a moment, and inside
        # _consume_stage2_nonce that transient would surface as a spurious
        # fail-closed refusal.
        import time as _time
        import uuid as _uuid
        tmp = p.with_suffix(p.suffix + f'.tmp.{_uuid.uuid4().hex}')
        try:
            tmp.write_text(json.dumps(prior, indent=2, default=str))
            for attempt in range(20):
                try:
                    os.replace(tmp, p)
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    _time.sleep(0.01)
        finally:
            if tmp.exists():
                try: tmp.unlink()
                except OSError: pass
    return p


def record_approval(
    request_id: str,
    *,
    kind: str,
    operator: str,
    gcode_hash: str | None = None,
) -> dict[str, Any]:
    """Record an explicit operator approval against the current request state.

    Binds the approval to the current ``request_revision`` and (if provided)
    ``gcode_hash`` so that a later plan mutation can be detected by
    ``can_start()`` — the approval becomes invalid the moment its bound
    revision or gcode_hash no longer matches what's on disk.

    *kind* is ``'upload'`` or ``'start'``. *operator* is the identity string
    (e.g. ``'telegram:brent'`` or ``'cli:local'``). When *gcode_hash* is
    None, the current ``request['gcode_hash']`` is used.

    Returns the written approval block.
    """
    if kind not in ('upload', 'start'):
        raise ValueError(f'invalid approval kind: {kind!r} (expected upload|start)')
    req = read_request(request_id) or {}
    # NB: write_request below applies schema defaults atomically — no need
    # to call _ensure_schema_defaults here (removed as L13 in 2026-06-27
    # cold-review cleanup; the call was dead).
    revision = req.get('request_revision', 1)
    bound_gcode_hash = gcode_hash if gcode_hash is not None else req.get('gcode_hash')
    approval = {
        'approved': True,
        'approved_by': operator,
        'approved_at': time.time(),
        'approved_revision': revision,
        'approved_gcode_hash': bound_gcode_hash,
    }
    approvals = dict(req.get('approvals') or {})
    approvals[kind] = approval
    write_request(request_id, approvals=approvals)
    return approval


# ============================================================================
# Model fingerprinting + recovery lookup
# ============================================================================

# Cache for compute_model_hash. Key is (resolved_path, size, mtime_ns) so
# the cache invalidates the moment the file changes — and we never serve a
# stale hash for a re-extracted STL with the same path. Process-local;
# fresh workflow invocations rebuild it (which is fine — the hash itself
# is just I/O over a few MB).
_HASH_CACHE: dict[tuple[str, int, int], str] = {}


def compute_model_hash(stl: Path, chunk_size: int = 1 << 20) -> str:
    """SHA-256 of the STL bytes. The recovery-lookup primary key.

    Same path with different content (e.g. operator sent two zips with
    a same-named STL inside) → different hash → workflow correctly
    starts a NEW request instead of resuming the prior one.

    Result is cached by (resolved_path, st_size, st_mtime_ns) so the same
    STL doesn't get re-hashed across multiple calls within a workflow
    invocation. The cache invalidates automatically when the file content
    changes (size or mtime_ns differs).
    """
    try:
        st = stl.stat()
        key = (str(stl.resolve()), st.st_size, st.st_mtime_ns)
    except OSError:
        key = None
    if key is not None:
        cached = _HASH_CACHE.get(key)
        if cached is not None:
            return cached
    h = hashlib.sha256()
    with stl.open('rb') as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            h.update(block)
    result = f'sha256:{h.hexdigest()}'
    if key is not None:
        _HASH_CACHE[key] = result
    return result


def find_recent_request_for_model(
    stl: Path,
    *,
    ttl_seconds: int = 1800,  # 30 min
) -> str | None:
    """Find a recent request whose model_hash matches *stl*'s current content.

    Used when the agent has lost context (e.g. Hermes context compression
    summarized away the prior workflow events) and re-runs the workflow
    with just an STL path. The workflow can recover the in-flight request
    instead of starting over from orient.

    Returns the request_id, or None if no match. Matches on **content
    hash**, not path — so same-filename-different-content correctly
    misses (Case B from the v1.7 design discussion).

    Note: O(n) over all requests in <data_dir>/requests/. Acceptable up to
    a few thousand entries; pair with a periodic cleanup job (deferred to
    a future phase) for long-lived deployments.
    """
    if not stl.exists():
        return None
    target_hash = compute_model_hash(stl)
    root = _requests_root()
    if not root.exists():
        return None
    now = time.time()
    candidates: list[tuple[float, str]] = []
    # TTL is checked against the request.json `updated_at` field — that's
    # the workflow's own write timestamp. Filesystem mtime can drift
    # independently (touch, backup tools, etc.) and would give a misleading
    # "this request is still active" reading when it isn't.
    try:
        entries = [e for e in root.iterdir() if e.is_dir() and is_request_id(e.name)]
    except OSError:
        return None
    for entry in entries:
        req = read_request(entry.name)
        if not req:
            continue
        updated_at = req.get('updated_at')
        if not isinstance(updated_at, (int, float)):
            continue
        if now - updated_at > ttl_seconds:
            continue
        if req.get('model_hash') == target_hash:
            candidates.append((updated_at, entry.name))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


# ============================================================================
# Argparse helpers for the workflow's --request-id / --fresh flags
# ============================================================================

class RequestNotFoundError(LookupError):
    """Raised when --request-id <id> names a request that doesn't exist on disk.

    This is a hard error rather than a silent fallthrough: if the operator
    explicitly asks to resume a specific request, proceeding with a phantom
    state would corrupt the workflow's view of what's already been answered.
    """


def resolve_request_id(
    cli_request_id: str | None,
    cli_fresh: bool,
    stl: Path,
) -> tuple[str, bool]:
    """Resolve the workflow's effective request_id at invocation time.

    Returns (request_id, was_resumed):
      - was_resumed=True   if we loaded existing on-disk state
      - was_resumed=False  if we generated a fresh request_id

    Priority:
      1. --fresh wipes any prior state for this STL and starts a new request
      2. Explicit --request-id <id> wins. Validated to exist on disk; raises
         RequestNotFoundError if not (fail loud, don't proceed with phantom state)
      3. Otherwise, look up by model content hash (recovery path)
      4. If nothing found, generate a new request_id
    """
    if cli_fresh:
        return generate_request_id(), False
    if cli_request_id:
        if not is_request_id(cli_request_id):
            raise ValueError(f'invalid request_id: {cli_request_id!r}')
        if read_request(cli_request_id) is None:
            raise RequestNotFoundError(
                f'request_id {cli_request_id!r} has no on-disk state at '
                f'{_request_json_path(cli_request_id)}; pass --fresh to start '
                'a new request or check `u1_request.py list` for valid ids'
            )
        return cli_request_id, True
    found = find_recent_request_for_model(stl)
    if found:
        return found, True
    return generate_request_id(), False


# ============================================================================
# CLI surface for ad-hoc inspection
# ============================================================================

def _cli_list(limit: int = 20) -> int:
    """List recent requests, newest first.

    Sort key prefers request.json's ``updated_at`` so listing order matches
    ``find_recent_request_for_model``'s recency logic (filesystem mtime can
    drift independently — backup tools, touch, etc.). Falls back to mtime
    when the JSON is missing or unreadable.
    """
    root = _requests_root()
    if not root.exists():
        print('(no requests yet)')
        return 0
    rows = []
    for entry in root.iterdir():
        if not (entry.is_dir() and is_request_id(entry.name)):
            continue
        req = read_request(entry.name) or {}
        sort_key = req.get('updated_at')
        if not isinstance(sort_key, (int, float)):
            try:
                sort_key = entry.stat().st_mtime
            except OSError:
                continue
        rows.append((
            sort_key,
            entry.name,
            req.get('status', '?'),
            req.get('phase', '?'),
            req.get('model_file', '?'),
        ))
    rows.sort(reverse=True)
    rows = rows[:limit]
    for sort_key, rid, status, phase, model in rows:
        when = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(sort_key))
        print(f'{rid}  {when}  {phase:>14s}  {status:>24s}  {model}')
    return 0


def _cli_show(request_id: str) -> int:
    try:
        req = read_request(request_id)
    except ValueError as exc:
        print(f'invalid request_id: {exc}', file=sys.stderr)
        return 2
    if req is None:
        print(f'no such request: {request_id}', file=sys.stderr)
        return 1
    print(json.dumps(req, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description='Inspect print-request state on disk.')
    sub = ap.add_subparsers(dest='cmd', required=False)
    sp_list = sub.add_parser('list', help='List recent requests')
    sp_list.add_argument('--limit', type=int, default=20)
    sp_show = sub.add_parser('show', help='Show one request as JSON')
    sp_show.add_argument('request_id')
    args = ap.parse_args(argv)
    if args.cmd == 'list':
        return _cli_list(limit=args.limit)
    if args.cmd == 'show':
        return _cli_show(args.request_id)
    ap.print_help()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
