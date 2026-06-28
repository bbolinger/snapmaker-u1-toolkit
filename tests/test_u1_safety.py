"""Acceptance tests for u1_safety.can_start — v2.0 Phase 3b (the moat)."""
from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

import u1_audit
import u1_print_start_gate as g
import u1_request
import u1_safety


# ============================================================================
# can_start() unit tests — every reject branch + the happy path
# ============================================================================

def _seed_request_with_readiness_card(*, gcode_hash='sha256:abc',
                                       bed_photo_captured=True,
                                       bed_check_required=True):
    """Common scaffold: a request with a readiness_card_emitted audit row
    bound to the request's current revision + gcode_hash. By default this
    setup passes can_start(). Tests then mutate from here to exercise each
    reject branch.

    The seed populates every plan-affecting field so subsequent test
    mutations (changing tool, profile, etc.) bump the revision — required
    after the 2026-06-28 fix that excludes initial-set from bumping."""
    rid = u1_request.generate_request_id()
    u1_request.write_request(
        rid,
        gcode_hash=gcode_hash,
        # Seed with baseline plan-affecting fields so subsequent CHANGES
        # to any of these bump the revision (initial-set no longer does).
        orient='asauthored',
        tool='T1',
        material='PETG',
        profile='0.20_strength',
        supports='no_supports',
        nozzle='0.4',
        safety={
            'bed_clear_check_required': bed_check_required,
            'bed_clear_photo_captured': bed_photo_captured,
        },
    )
    req = u1_request.read_request(rid)
    u1_audit.append(rid, 'readiness_card_emitted', operator='cli:test',
                    request_revision=req['request_revision'],
                    gcode_hash=req['gcode_hash'])
    return rid


def test_can_start_happy_path():
    rid = _seed_request_with_readiness_card()
    req = u1_request.read_request(rid)
    allowed, reason = u1_safety.can_start(req)
    assert allowed is True
    assert reason == 'ok'


def test_can_start_rejects_none_request():
    allowed, reason = u1_safety.can_start(None)
    assert allowed is False
    assert 'no request' in reason.lower()


def test_can_start_rejects_request_without_id():
    allowed, reason = u1_safety.can_start({'foo': 'bar'})
    assert allowed is False


def test_can_start_rejects_when_no_readiness_card_emitted():
    """Operator never had a chance to review → can_start refuses.
    Defends against 'agent ran Stage 2 without going through Stage 1'
    (the readiness card emit is part of the Stage-1-prep boundary)."""
    rid = u1_request.generate_request_id()
    u1_request.write_request(rid, gcode_hash='sha256:abc',
                             safety={'bed_clear_check_required': False,
                                     'bed_clear_photo_captured': False})
    req = u1_request.read_request(rid)
    allowed, reason = u1_safety.can_start(req)
    assert allowed is False
    assert 'readiness_card' in reason or 'review' in reason.lower()


def test_can_start_rejects_when_revision_bumped_since_review():
    """The marquee safety property: operator reviewed revision N, plan
    changed to revision N+1, can_start refuses with 'plan changed'."""
    rid = _seed_request_with_readiness_card()
    # Plan change — bump revision via a plan-affecting field
    u1_request.write_request(rid, tool='T2')  # tool change is plan-affecting
    req = u1_request.read_request(rid)
    assert req['request_revision'] == 2  # confirmed bumped
    allowed, reason = u1_safety.can_start(req)
    assert allowed is False
    assert 'plan changed' in reason.lower()


def test_can_start_rejects_when_gcode_hash_changed_since_review():
    """The second marquee property: a re-slice produced a new gcode_hash
    after the operator reviewed → can_start refuses with 'gcode regenerated'."""
    rid = _seed_request_with_readiness_card(gcode_hash='sha256:original')
    # Simulate a re-slice replacing the gcode (without bumping revision —
    # if gcode_hash changes, revision SHOULD bump too via the helper, but
    # let's test the gcode_hash check directly).
    req = u1_request.read_request(rid)
    # Edit request.json directly to set new gcode_hash without bumping revision
    # (simulates a process that doesn't go through write_request).
    p = u1_request.request_dir(rid) / 'request.json'
    raw = json.loads(p.read_text())
    raw['gcode_hash'] = 'sha256:replaced'
    p.write_text(json.dumps(raw))
    req = u1_request.read_request(rid)
    allowed, reason = u1_safety.can_start(req)
    assert allowed is False
    assert 'gcode' in reason.lower()


def test_can_start_rejects_when_bed_photo_missing():
    """bed_clear_check_required=True but bed_clear_photo_captured=False → refuse."""
    rid = _seed_request_with_readiness_card(bed_photo_captured=False,
                                             bed_check_required=True)
    req = u1_request.read_request(rid)
    allowed, reason = u1_safety.can_start(req)
    assert allowed is False
    assert 'bed-clear' in reason.lower() or 'photo' in reason.lower()


def test_can_start_accepts_when_bed_check_not_required():
    """If bed_clear_check_required=False, missing photo is NOT a reject reason."""
    rid = _seed_request_with_readiness_card(bed_photo_captured=False,
                                             bed_check_required=False)
    req = u1_request.read_request(rid)
    allowed, reason = u1_safety.can_start(req)
    assert allowed is True


def test_can_start_accepts_audit_records_injection():
    """The audit_records kwarg short-circuits the disk read; useful for tests."""
    fake_audit = [{
        'seq': 1, 'ts': '2026-06-27T10:00:00+00:00',
        'event': 'readiness_card_emitted', 'operator': 'cli:test',
        'details': {'request_revision': 5, 'gcode_hash': 'sha256:injected'},
    }]
    req = {
        'request_id': 'u1_2026_0627_aaaaaa',
        'request_revision': 5,
        'gcode_hash': 'sha256:injected',
        'safety': {'bed_clear_check_required': False, 'bed_clear_photo_captured': False},
    }
    allowed, reason = u1_safety.can_start(req, audit_records=fake_audit)
    assert allowed is True


def test_can_start_uses_most_recent_readiness_card():
    """If two readiness cards were emitted (e.g. plan changed then operator
    re-reviewed), can_start uses the most recent one as the reference."""
    rid = _seed_request_with_readiness_card(gcode_hash='sha256:original')
    # First card was for revision 1 / sha256:original.
    # Now plan changes: tool swap bumps to revision 2.
    u1_request.write_request(rid, tool='T2')
    # Then a SECOND readiness card is emitted carrying revision 2.
    req = u1_request.read_request(rid)
    u1_audit.append(rid, 'readiness_card_emitted', operator='cli:test',
                    request_revision=req['request_revision'],
                    gcode_hash=req['gcode_hash'])
    # The most recent card matches current state → can_start passes.
    req = u1_request.read_request(rid)
    allowed, reason = u1_safety.can_start(req)
    assert allowed is True


# ============================================================================
# Integration tests — Stage 2 dispatch routes through can_start()
# ============================================================================

def _fake_capture_passing(out_dir, host, port, wait=5.0):
    out_dir.mkdir(parents=True, exist_ok=True)
    path = (out_dir / 'bed.jpg').resolve()
    path.write_bytes(b'\xff\xd8\xff\xe0FAKEJPEG')
    return {
        'ok': True, 'path': str(path), 'fresh': True, 'is_mock': False,
        'error': None,
        'timestamp_utc': datetime.now(timezone.utc).isoformat(),
        'brightness_mean': 200.0, 'brightness_ok': True,
        'bytes': path.stat().st_size,
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _fake_idle_status(*args, **kwargs):
    return {
        'print_stats': {'state': 'standby'},
        'virtual_sdcard': {'is_active': False},
        'pause_resume': {'is_paused': False},
        'toolhead': {'extruder': 'extruder1'},
    }


def test_stage2_refuses_when_no_request_id_passed(monkeypatch, tmp_path):
    """Acceptance #1: Stage 2 dispatch with NO --request-id refuses. The
    moat cannot operate without a request to verify against."""
    monkeypatch.setattr(g, 'query_state', _fake_idle_status)
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture_passing)
    # Stage 1 to get a valid token (without request_id — Stage 1 allows it).
    res1 = g.run_gate('x.gcode', bed_clear='cancel', host='h', port=1,
                      intended_tool='extruder1', out_dir=tmp_path)
    token = res1['approval_token']
    # Now Stage 2 without request_id → must refuse.
    called = {'start': False}
    res2 = g.run_gate('x.gcode', bed_clear='start', host='h', port=1,
                     intended_tool='extruder1', out_dir=tmp_path,
                     approval_token=token,
                     start_func=lambda *a: called.__setitem__('start', True))
    assert res2['started'] is False
    assert 'request-id' in res2['reason']
    assert not called['start']


def test_stage2_refuses_when_revision_drift_detected(monkeypatch, tmp_path):
    """Acceptance #2: operator reviewed revision N, plan bumped to N+1
    before Stage 2 fired → can_start refuses → printer_start never called."""
    monkeypatch.setattr(g, 'query_state', _fake_idle_status)
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture_passing)
    rid = _seed_request_with_readiness_card()
    res1 = g.run_gate('x.gcode', bed_clear='cancel', host='h', port=1,
                      intended_tool='extruder1', out_dir=tmp_path,
                      request_id=rid)
    token = res1['approval_token']
    # Plan change between Stage 1 and Stage 2 — bump revision.
    u1_request.write_request(rid, tool='T2')
    called = {'start': False}
    res2 = g.run_gate('x.gcode', bed_clear='start', host='h', port=1,
                     intended_tool='extruder1', out_dir=tmp_path,
                     approval_token=token,
                     request_id=rid,
                     start_func=lambda *a: called.__setitem__('start', True))
    assert res2['started'] is False
    assert 'plan changed' in res2['reason'].lower()
    assert not called['start']
    # And the audit log captured the safety check failure.
    rows = list(u1_audit.read(rid))
    assert any(r['event'] == 'start_safety_check_failed' for r in rows)


def test_stage2_refuses_when_gcode_hash_drift_detected(monkeypatch, tmp_path):
    """Acceptance #3: re-slice replaces gcode → can_start refuses with
    gcode_hash mismatch reason."""
    monkeypatch.setattr(g, 'query_state', _fake_idle_status)
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture_passing)
    rid = _seed_request_with_readiness_card(gcode_hash='sha256:original')
    res1 = g.run_gate('x.gcode', bed_clear='cancel', host='h', port=1,
                      intended_tool='extruder1', out_dir=tmp_path,
                      request_id=rid)
    token = res1['approval_token']
    # Re-slice: edit gcode_hash directly (without going through write_request,
    # so revision doesn't bump — isolates the gcode_hash check).
    p = u1_request.request_dir(rid) / 'request.json'
    raw = json.loads(p.read_text())
    raw['gcode_hash'] = 'sha256:replaced'
    p.write_text(json.dumps(raw))
    called = {'start': False}
    res2 = g.run_gate('x.gcode', bed_clear='start', host='h', port=1,
                     intended_tool='extruder1', out_dir=tmp_path,
                     approval_token=token,
                     request_id=rid,
                     start_func=lambda *a: called.__setitem__('start', True))
    assert res2['started'] is False
    assert 'gcode' in res2['reason'].lower()
    assert not called['start']


def test_stage2_happy_path_audits_safety_passed_and_print_started(monkeypatch, tmp_path):
    """Acceptance #4: when can_start passes, the gate proceeds to start
    AND records start_safety_check_passed + print_started audit rows."""
    monkeypatch.setattr(g, 'query_state', _fake_idle_status)
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture_passing)
    rid = _seed_request_with_readiness_card()
    res1 = g.run_gate('x.gcode', bed_clear='cancel', host='h', port=1,
                      intended_tool='extruder1', out_dir=tmp_path,
                      request_id=rid, operator='telegram:brent')
    token = res1['approval_token']
    res2 = g.run_gate('x.gcode', bed_clear='start', host='h', port=1,
                     intended_tool='extruder1', out_dir=tmp_path,
                     approval_token=token,
                     request_id=rid, operator='telegram:brent',
                     start_func=lambda *a: {'result': 'ok'})
    assert res2['started'] is True
    rows = list(u1_audit.read(rid))
    events = [r['event'] for r in rows]
    assert 'start_safety_check_passed' in events
    assert 'print_started' in events
    # The print_started row has the operator stamped.
    started_row = next(r for r in rows if r['event'] == 'print_started')
    assert started_row['operator'] == 'telegram:brent'


def test_stage2_records_approval_on_successful_start(monkeypatch, tmp_path):
    """Acceptance #5: after a successful Stage 2, request.json's
    approvals.start block is populated — bound to the revision + gcode_hash
    that was actually started. Useful for forensic inspection."""
    monkeypatch.setattr(g, 'query_state', _fake_idle_status)
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture_passing)
    rid = _seed_request_with_readiness_card()
    res1 = g.run_gate('x.gcode', bed_clear='cancel', host='h', port=1,
                      intended_tool='extruder1', out_dir=tmp_path,
                      request_id=rid, operator='telegram:brent')
    token = res1['approval_token']
    g.run_gate('x.gcode', bed_clear='start', host='h', port=1,
              intended_tool='extruder1', out_dir=tmp_path,
              approval_token=token,
              request_id=rid, operator='telegram:brent',
              start_func=lambda *a: {'result': 'ok'})
    req = u1_request.read_request(rid)
    start_approval = req['approvals']['start']
    assert start_approval['approved'] is True
    assert start_approval['approved_by'] == 'telegram:brent'
    assert start_approval['approved_revision'] == req['request_revision']
    assert start_approval['approved_gcode_hash'] == req['gcode_hash']


def test_stage1_marks_bed_clear_photo_captured(monkeypatch, tmp_path):
    """Stage 1's successful real bed photo capture should stamp
    safety.bed_clear_photo_captured=True on request.json. This is what
    lets can_start() satisfy its bed-clear precondition on Stage 2."""
    monkeypatch.setattr(g, 'query_state', _fake_idle_status)
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture_passing)
    rid = u1_request.generate_request_id()
    # Seed without bed photo captured — Stage 1 should flip the bit.
    u1_request.write_request(rid, gcode_hash='sha256:abc',
                             safety={'bed_clear_check_required': True,
                                     'bed_clear_photo_captured': False})
    g.run_gate('x.gcode', bed_clear='cancel', host='h', port=1,
              intended_tool='extruder1', out_dir=tmp_path,
              request_id=rid)
    after = u1_request.read_request(rid)
    assert after['safety']['bed_clear_photo_captured'] is True


# ============================================================================
# H4 cold-review fix: can_start() is fail-CLOSED on missing binding fields
# ============================================================================

def test_can_start_refuses_when_audit_row_lacks_request_revision():
    """H4: a readiness_card_emitted audit row without request_revision in
    details must NOT pass can_start. The earlier version would skip the
    check ('reviewed_revision is not None') and let the start through."""
    fake_audit = [{
        'seq': 1, 'ts': '2026-06-27T10:00:00+00:00',
        'event': 'readiness_card_emitted', 'operator': 'cli:test',
        'details': {'gcode_hash': 'sha256:xxx'},  # request_revision MISSING
    }]
    req = {
        'request_id': 'u1_2026_0627_aaaaaa',
        'request_revision': 5,
        'gcode_hash': 'sha256:xxx',
        'safety': {'bed_clear_check_required': False, 'bed_clear_photo_captured': False},
    }
    allowed, reason = u1_safety.can_start(req, audit_records=fake_audit)
    assert allowed is False
    assert 'revision' in reason.lower()


def test_can_start_refuses_when_audit_row_lacks_gcode_hash():
    """H4: a readiness_card_emitted audit row without gcode_hash in details
    must NOT pass can_start. Earlier version skipped the check."""
    fake_audit = [{
        'seq': 1, 'ts': '2026-06-27T10:00:00+00:00',
        'event': 'readiness_card_emitted', 'operator': 'cli:test',
        'details': {'request_revision': 5},  # gcode_hash MISSING
    }]
    req = {
        'request_id': 'u1_2026_0627_aaaaaa',
        'request_revision': 5,
        'gcode_hash': 'sha256:current',
        'safety': {'bed_clear_check_required': False, 'bed_clear_photo_captured': False},
    }
    allowed, reason = u1_safety.can_start(req, audit_records=fake_audit)
    assert allowed is False
    assert 'gcode' in reason.lower()


def test_can_start_refuses_when_request_lacks_gcode_hash_but_audit_has_it():
    """H4: opposite direction — audit row has gcode_hash, but request.json
    no longer does. The earlier version's `current_gcode_hash and ...`
    guard let this through. Now refused."""
    fake_audit = [{
        'seq': 1, 'ts': '2026-06-27T10:00:00+00:00',
        'event': 'readiness_card_emitted', 'operator': 'cli:test',
        'details': {'request_revision': 5, 'gcode_hash': 'sha256:reviewed'},
    }]
    req = {
        'request_id': 'u1_2026_0627_aaaaaa',
        'request_revision': 5,
        # gcode_hash absent — earlier code skipped the check
        'safety': {'bed_clear_check_required': False, 'bed_clear_photo_captured': False},
    }
    allowed, reason = u1_safety.can_start(req, audit_records=fake_audit)
    assert allowed is False
    assert 'gcode' in reason.lower()


def test_can_start_accepts_when_both_sides_have_no_gcode_hash():
    """Edge case: both sides lacking gcode_hash is symmetric — None == None.
    No mismatch, so the gcode check doesn't refuse. Revision still has to
    match (it does, both 5). This documents the fail-closed behavior on
    BOTH sides explicitly: it's not a 'reject whenever anything is None',
    it's 'reject on inequality, including None vs value'."""
    fake_audit = [{
        'seq': 1, 'ts': '2026-06-27T10:00:00+00:00',
        'event': 'readiness_card_emitted', 'operator': 'cli:test',
        'details': {'request_revision': 5},  # no gcode_hash
    }]
    req = {
        'request_id': 'u1_2026_0627_aaaaaa',
        'request_revision': 5,
        # also no gcode_hash
        'safety': {'bed_clear_check_required': False, 'bed_clear_photo_captured': False},
    }
    allowed, reason = u1_safety.can_start(req, audit_records=fake_audit)
    assert allowed is True


# ============================================================================
# M5 cold-review fix: Stage 2 sanity-capture failure is audited
# ============================================================================

def _fake_capture_mock(out_dir, host, port, wait=5.0):
    out_dir.mkdir(parents=True, exist_ok=True)
    path = (out_dir / 'bed_snapshot__MOCK.png').resolve()
    path.write_bytes(b'\xff\xd8\xff\xe0FAKEMOCK')
    return {
        'ok': False, 'path': str(path), 'fresh': False, 'is_mock': True,
        'error': 'simulated mock',
        'timestamp_utc': datetime.now(timezone.utc).isoformat(),
        'brightness_mean': None, 'brightness_ok': None,
        'brightness_check': None,
        'bytes': path.stat().st_size,
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def test_stage2_sanity_failure_is_audited(monkeypatch, tmp_path):
    """M5: when Stage 2 sanity capture fails, an audit row must record
    stage2_sanity_capture_failed so the forensic timeline isn't a black
    hole between start_safety_check_passed and 'nothing happened'."""
    monkeypatch.setattr(g, 'query_state', _fake_idle_status)
    # Stage 1: passing real capture
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture_passing)
    rid = _seed_request_with_readiness_card()
    res1 = g.run_gate('x.gcode', bed_clear='cancel', host='h', port=1,
                      intended_tool='extruder1', out_dir=tmp_path,
                      request_id=rid)
    token = res1['approval_token']
    # Swap to mock so Stage 2 sanity fails
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture_mock)
    called = {'start': False}
    res2 = g.run_gate('x.gcode', bed_clear='start', host='h', port=1,
                     intended_tool='extruder1', out_dir=tmp_path,
                     approval_token=token,
                     request_id=rid,
                     start_func=lambda *a: called.__setitem__('start', True))
    assert res2['started'] is False
    assert called['start'] is False
    rows = list(u1_audit.read(rid))
    events = [r['event'] for r in rows]
    assert 'start_safety_check_passed' in events  # moat let it through
    assert 'stage2_sanity_capture_failed' in events  # but sanity refused
    # And we DID NOT audit print_started
    assert 'print_started' not in events
