#!/usr/bin/env python3
"""One-shot migrator: pre-Phase-3 request.json → v1 schema.

Walks every directory under ``<data_dir>/requests/`` whose name matches the
request_id pattern, and rewrites each ``request.json`` in place to carry
the v1 schema fields: ``schema_version``, ``request_revision``, the
``approvals`` block (both upload + start empty), and the ``safety`` block
(bed_clear_check_required=True, bed_clear_photo_captured=False).

Run once after upgrading the toolkit from Phase 2 to Phase 3. Idempotent
— safe to re-run; already-migrated requests are no-ops.

Conservative defaults:
- request_revision = 1 (we can't know how many plan changes happened pre-v1)
- Approvals are EMPTY. **Pre-v1 implicit approvals do not become explicit
  approvals.** When in doubt, the operator re-approves.
- bed_clear_check_required defaults to True (the safe choice).

NEVER invent approval state during migration. Pre-v1 requests that were
mid-flow when the upgrade happened will require operator re-approval to
complete. That's by design — Phase 3 is the boundary at which approvals
became revision/hash-bound, so previous "yes I approve" answers aren't
auditable under the new model.

Usage:
    python3 scripts/migrate_v0_to_v1.py            # migrate using default data_dir
    python3 scripts/migrate_v0_to_v1.py --dry-run  # show what would change
    python3 scripts/migrate_v0_to_v1.py --data-dir /custom/path
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# scripts/ is on sys.path when this is invoked from the repo root.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from u1_request import (
    SCHEMA_VERSION,
    _ensure_schema_defaults,
    is_request_id,
)


def _data_dir(override: str | None) -> Path:
    if override:
        return Path(override)
    # Defer the import so an --data-dir override avoids the dotenv walk.
    from u1_config import get_data_dir
    return get_data_dir()


def migrate_one(request_dir: Path, *, dry_run: bool) -> str:
    """Migrate a single request directory. Returns a one-word status:
    'migrated', 'noop' (already v1), 'missing' (no request.json), or 'bad'
    (file unreadable/corrupt)."""
    p = request_dir / 'request.json'
    if not p.exists():
        return 'missing'
    try:
        original = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return 'bad'
    if not isinstance(original, dict):
        return 'bad'
    if original.get('schema_version') == SCHEMA_VERSION and 'request_revision' in original:
        # Already a clean v1 — no work to do (idempotent re-run case).
        return 'noop'
    # Build the migrated dict. _ensure_schema_defaults fills in everything
    # missing without overwriting prior values.
    migrated = dict(original)
    _ensure_schema_defaults(migrated)
    if dry_run:
        return 'migrated'
    # Atomic write.
    tmp = p.with_suffix(p.suffix + f'.tmp.{os.getpid()}')
    try:
        tmp.write_text(json.dumps(migrated, indent=2, default=str))
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return 'migrated'


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Migrate pre-Phase-3 request.json files to v1.')
    ap.add_argument('--data-dir', type=str, default=None,
                    help='Override the data dir. Default: u1_config.get_data_dir().')
    ap.add_argument('--dry-run', action='store_true',
                    help='Report what would change without writing.')
    args = ap.parse_args(argv)

    root = _data_dir(args.data_dir) / 'requests'
    if not root.exists():
        print(f'(no requests dir at {root} — nothing to migrate)')
        return 0

    counts = {'migrated': 0, 'noop': 0, 'missing': 0, 'bad': 0}
    for entry in sorted(root.iterdir()):
        if not (entry.is_dir() and is_request_id(entry.name)):
            continue
        status = migrate_one(entry, dry_run=args.dry_run)
        counts[status] += 1
        verb = 'WOULD migrate' if (args.dry_run and status == 'migrated') else status
        print(f'  {entry.name}: {verb}')

    print('---')
    for k in ('migrated', 'noop', 'missing', 'bad'):
        print(f'  {k}: {counts[k]}')
    if args.dry_run:
        print('(dry-run — no files modified)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
