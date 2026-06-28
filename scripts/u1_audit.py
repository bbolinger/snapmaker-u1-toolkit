#!/usr/bin/env python3
"""Per-request audit log — v2.0 Phase 3a.

Every print request gets a `requests/<request_id>/audit.jsonl` file —
one JSON object per line, append-only at the application level. The audit
log is the forensic evidence trail: what happened to the request, in what
order, performed by whom.

This is distinct from `request.json` (the operational current state) and
distinct from `events.jsonl` (the workflow's chatty stage stream that
goes to the agent). `audit.jsonl` is permanent and trimmable only via
intentional retention policy — never rewritten in the normal path.

Concurrency: `append()` uses `O_APPEND + fcntl.flock` so two processes
hitting the same request_id interleave cleanly. The whole count→write
sequence runs under the exclusive lock, so `seq` is monotonic and no
two writers can interleave bytes mid-line.

Public surface:
  append(request_id, event, *, operator=None, **details) -> dict
  read(request_id, *, since=None, until=None) -> Iterator[dict]
  fold(request_id) -> dict
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# Lazy import so this module imports cleanly even when u1_config can't
# resolve (early-test contexts) or when called from a one-shot script.
def _data_dir() -> Path:
    from u1_config import get_data_dir
    return get_data_dir()


def _requests_root() -> Path:
    return _data_dir() / 'requests'


def _audit_path(request_id: str) -> Path:
    from u1_request import request_dir  # local import to avoid cycle at module load
    return request_dir(request_id) / 'audit.jsonl'


# ============================================================================
# Append (the hot path)
# ============================================================================

def append(request_id: str, event: str, *, operator: str | None = None, **details: Any) -> dict[str, Any]:
    """Append one event line to ``requests/<request_id>/audit.jsonl``.

    Always stamps `seq`, `ts`, `request_id`, `event`, and (if provided)
    `operator`. Extra kwargs land under `details`. Returns the written
    line as a dict so the caller can mirror it (e.g. into the workflow's
    own events.jsonl) without re-serializing.

    Atomic line write via `O_APPEND + fcntl.flock` — the lock spans
    the count→write so seq stays monotonic and bytes from concurrent
    writers don't interleave.
    """
    from u1_request import ensure_request_dir  # local import to avoid cycle
    ensure_request_dir(request_id)
    p = _audit_path(request_id)

    # Build the record. ts is UTC ISO-8601 (consistent with request_id's
    # date stamp). The seq lookup is wrapped in flock so two appenders
    # in flight see a consistent count.
    record: dict[str, Any] = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'request_id': request_id,
        'event': event,
    }
    if operator is not None:
        record['operator'] = operator
    if details:
        record['details'] = details

    # Open append-mode + flock the file before counting + appending. The
    # whole sequence (count → write) is under the lock so two writers can't
    # both write seq=N.
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # Re-count under lock so seq is monotonic across concurrent writers.
        # next_seq() reads its own way; here we count directly on the open fd.
        try:
            with open(p, 'rb') as count_f:
                seq = sum(1 for _ in count_f) + 1
        except OSError:
            seq = 1
        record['seq'] = seq
        # Reorder keys so the line reads naturally: seq, ts, request_id, event, …
        ordered = {k: record[k] for k in ('seq', 'ts', 'request_id', 'event') if k in record}
        for k in ('operator', 'details'):
            if k in record:
                ordered[k] = record[k]
        line = json.dumps(ordered, separators=(',', ':'), default=str) + '\n'
        # Write happens under the exclusive flock; concurrent writers
        # serialize cleanly and seq stays monotonic.
        os.write(fd, line.encode('utf-8'))
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
    return ordered


# ============================================================================
# Read + fold (forensic reads, summary reconstruction)
# ============================================================================

def read(
    request_id: str,
    *,
    since: float | None = None,
    until: float | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield audit events in chronological order.

    *since* / *until* filter by UNIX timestamp (float seconds). They compare
    against the parsed `ts` field; events with malformed `ts` are kept (we
    can't filter what we can't parse — visible to the reader).
    """
    p = _audit_path(request_id)
    if not p.exists():
        return
    try:
        with p.open('r', encoding='utf-8') as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # one bad line shouldn't stop the rest
                if since is not None or until is not None:
                    ts_str = record.get('ts')
                    if isinstance(ts_str, str):
                        try:
                            ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00')).timestamp()
                        except ValueError:
                            ts = None
                    else:
                        ts = None
                    if ts is not None:
                        if since is not None and ts < since:
                            continue
                        if until is not None and ts > until:
                            continue
                yield record
    except OSError:
        return


def fold(request_id: str) -> dict[str, Any]:
    """Reduce all audit events into a state snapshot.

    Useful for forensic summary CLI and for the future case where
    request.json is corrupted and we want to reconstruct it. The fold
    is intentionally lossy: it returns the *latest* value seen for each
    field, not the full history. Use ``read()`` for the full timeline.
    """
    state: dict[str, Any] = {'request_id': request_id, 'event_count': 0}
    last_operator: str | None = None
    seen_events: list[str] = []
    for record in read(request_id):
        state['event_count'] += 1
        ev = record.get('event')
        if ev:
            seen_events.append(ev)
        if record.get('operator'):
            last_operator = record['operator']
        details = record.get('details') or {}
        # Selected field promotions — extend as Phase 3a/3b emit shapes settle.
        for promote in ('approved_revision', 'approved_gcode_hash', 'request_revision',
                        'gcode_hash', 'model_hash', 'printer_storage_filename',
                        'uploaded_filename', 'status'):
            if promote in details:
                state[promote] = details[promote]
    state['seen_events'] = seen_events
    if last_operator is not None:
        state['last_operator'] = last_operator
    return state


# ============================================================================
# CLI surface — `show` only for v2.0.0
# ============================================================================

def _cli_show(request_id: str) -> int:
    try:
        from u1_request import is_request_id
    except ImportError:
        is_request_id = None
    if is_request_id is not None and not is_request_id(request_id):
        print(f'invalid request_id: {request_id!r}', file=sys.stderr)
        return 2
    p = _audit_path(request_id)
    if not p.exists():
        print(f'no audit log for {request_id} at {p}', file=sys.stderr)
        return 1
    for record in read(request_id):
        # Compact one-line-per-event view; details inlined.
        seq = record.get('seq', '?')
        ts = record.get('ts', '?')
        event = record.get('event', '?')
        operator = record.get('operator', '-')
        details = record.get('details')
        if details:
            details_str = ' ' + json.dumps(details, separators=(',', ':'), default=str)
        else:
            details_str = ''
        print(f'#{seq:>4} {ts} [{operator}] {event}{details_str}')
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description='Inspect per-request audit logs.')
    sub = ap.add_subparsers(dest='cmd', required=False)
    sp_show = sub.add_parser('show', help='Print one request\'s audit timeline')
    sp_show.add_argument('request_id')
    args = ap.parse_args(argv)
    if args.cmd == 'show':
        return _cli_show(args.request_id)
    ap.print_help()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
