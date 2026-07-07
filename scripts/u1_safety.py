#!/usr/bin/env python3
"""Safety preconditions — v2.0 Phase 3b.

Single source of truth for "is it safe to physically start this print."
Every Stage 2 dispatch path (currently just ``u1_print_start_gate.py``,
but any future printer-affecting helper) MUST route through ``can_start()``
before commanding the printer.

The check is intentionally narrow: it answers "has anything drifted
since the operator reviewed the readiness card?" The existing Stage 2
checks — token TTL, preflight, sanity capture — remain in place; this
adds drift detection on top. Defense in depth: four small focused
layers, all must pass.

Public surface:
  can_start(request) -> (bool, reason)
"""
from __future__ import annotations

from typing import Any


_RESUMED_OR_EMITTED = (
    'readiness_card_emitted',
    'readiness_card_replayed_from_resume',
    # Forward-compat for workflows that emit a workflow-specific readiness
    # event name (e.g. the v2.1 multi-part kit workflow emits
    # `kit_readiness_card_emitted` because it carries plate metadata + the
    # operator-selected gated_plate). Same bug class as the v2.0 active-tool
    # blocker that assumed single-tool semantics — any new readiness-emitting
    # workflow must be added here or can_start() will refuse Stage 2 with
    # "no readiness_card emitted yet".
    'kit_readiness_card_emitted',
    # v2.3 reprint: the review moment is the reprint turn itself — the
    # original plate previews + review doc are re-surfaced alongside a fresh
    # bed photo before the operator's yes. Same revision+hash binding rules.
    'reprint_readiness_card_emitted',
)


def can_start(
    request: dict[str, Any] | None,
    *,
    audit_records: list[dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for whether this request may physically start.

    Reads the request's audit log to find the most recent
    ``readiness_card_emitted`` (or ``readiness_card_replayed_from_resume``)
    row — that's the moment the operator was asked to review the print
    plan. Compares the revision + gcode_hash that was reviewed against
    the request's CURRENT state. If they drifted, the prior review is
    no longer valid for the current plan.

    Also checks the safety block: if ``bed_clear_check_required`` is set,
    ``bed_clear_photo_captured`` must be True.

    *audit_records* may be supplied to short-circuit the disk read (used
    in tests to inject a synthetic audit log without writing files).
    """
    if not request or not isinstance(request, dict):
        return False, 'no request state on disk'

    rid = request.get('request_id')
    if not rid:
        return False, 'request has no request_id'

    if audit_records is None:
        # Local import: u1_audit imports u1_request which imports u1_config —
        # keep this dependency lazy so importing u1_safety in isolation works.
        from u1_audit import read as audit_read
        audit_records = list(audit_read(rid))

    # Find the readiness card — that's the moment the operator was asked.
    readiness_rows = [r for r in audit_records
                      if r.get('event') in _RESUMED_OR_EMITTED]
    if not readiness_rows:
        return False, ('no readiness_card emitted yet — operator never had '
                       'a chance to review the print plan')
    readiness = readiness_rows[-1]
    details = readiness.get('details') or {}
    reviewed_revision = details.get('request_revision')
    reviewed_gcode_hash = details.get('gcode_hash')

    # H4 fix (cold review 2026-06-27): refuse strictly. Earlier versions
    # skipped the check when either side was None — which let an unguarded
    # print through if the audit row was tampered with or if the workflow
    # ever wrote an incomplete row. The moat must be fail-CLOSED: missing
    # binding info is itself a mismatch.
    current_revision = request.get('request_revision')
    if reviewed_revision != current_revision:
        return False, (f'plan changed since operator reviewed '
                       f'(revision {reviewed_revision!r} → {current_revision!r})')

    current_gcode_hash = request.get('gcode_hash')
    if reviewed_gcode_hash != current_gcode_hash:
        return False, ('gcode regenerated since operator reviewed '
                       f'(gcode_hash {reviewed_gcode_hash!r} → {current_gcode_hash!r})')

    safety = request.get('safety') or {}
    if isinstance(safety, dict) and safety.get('bed_clear_check_required'):
        if not safety.get('bed_clear_photo_captured'):
            return False, 'bed-clear photo required but not captured'

    return True, 'ok'
