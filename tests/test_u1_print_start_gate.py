import hashlib
import json
from pathlib import Path

import pytest

import u1_print_start_gate as g
import u1_audit
import u1_request


def _seed_can_start_passing_request(printer_filename: str = 'x.gcode') -> str:
    """Phase 3b helper: seed a request that can_start() will accept.

    Creates a fresh request_id, writes a request.json with the bed-clear
    photo already captured + gcode_hash set, and injects a
    readiness_card_emitted audit row carrying the matching revision +
    gcode_hash. After this, can_start(request) returns (True, 'ok')
    and Stage 2 is allowed to dispatch.

    Returns the request_id."""
    rid = u1_request.generate_request_id()
    u1_request.write_request(
        rid,
        printer_storage_filename=printer_filename,
        gcode_hash='sha256:stage_gate_test',
        safety={'bed_clear_check_required': True, 'bed_clear_photo_captured': True},
    )
    req = u1_request.read_request(rid)
    u1_audit.append(rid, 'readiness_card_emitted', operator='cli:test',
                    request_revision=req['request_revision'],
                    gcode_hash=req['gcode_hash'])
    return rid


def idle():
    return {
        'print_stats': {'state': 'standby'},
        'virtual_sdcard': {'is_active': False},
        'pause_resume': {'is_paused': False},
        'toolhead': {'extruder': 'extruder1'},
    }


def _fake_capture(success: bool = True, brightness: float = 200.0, is_mock: bool = False):
    """Stand-in for capture_real_bed_photo. Writes a tiny JPEG so file ops work."""
    from datetime import datetime, timezone

    def _cap(out_dir, host, port, wait: float = 5.0):
        out_dir.mkdir(parents=True, exist_ok=True)
        path = (out_dir / ('bed_snapshot__MOCK.png' if is_mock else 'bed_snapshot.jpg')).resolve()
        path.write_bytes(b'\xff\xd8\xff\xe0FAKEJPEG_' + str(brightness).encode())
        sha = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        return {
            'ok': success and brightness > g.DARK_PHOTO_MEAN_LUMA,
            'path': str(path), 'fresh': success, 'is_mock': is_mock,
            'error': None if success and brightness > g.DARK_PHOTO_MEAN_LUMA else 'simulated',
            # Current time so the approval-token TTL window is fresh during tests.
            'timestamp_utc': datetime.now(timezone.utc).isoformat(),
            'brightness_mean': brightness,
            'brightness_ok': brightness > g.DARK_PHOTO_MEAN_LUMA,
            'bytes': path.stat().st_size,
            'sha256': sha,
        }
    return _cap


def test_preflight_failure_returns_blockers(monkeypatch, tmp_path):
    monkeypatch.setattr(g, 'query_state',
                        lambda h, p: {'print_stats': {'state': 'printing'},
                                       'virtual_sdcard': {'is_active': True},
                                       'pause_resume': {'is_paused': False}})
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture(success=True))
    res = g.run_gate('x.gcode', host='h', port=1, out_dir=tmp_path)
    assert not res['started']
    assert res['blockers']
    assert res['stage'] == 'readiness'


def test_stage1_returns_approval_token_when_photo_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(g, 'query_state', lambda h, p: idle())
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture(success=True))
    res = g.run_gate('x.gcode', bed_clear='cancel', host='h', port=1,
                     intended_tool='extruder1', out_dir=tmp_path)
    assert res['stage'] == 'readiness'
    assert res['approval_token'] and isinstance(res['approval_token'], str)
    assert res['approval_ttl_seconds'] == g.APPROVAL_TTL_SEC
    # Token sidecar written to disk
    assert (tmp_path / 'bed_snapshot.approval_token.json').exists()


def test_stage1_dark_photo_returns_no_token(monkeypatch, tmp_path):
    # Audit round-10 bug: black photo passed as fresh JPEG. Now: brightness
    # below floor → snapshot.ok=False, no approval token issued.
    monkeypatch.setattr(g, 'query_state', lambda h, p: idle())
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture(success=True, brightness=2.0))
    res = g.run_gate('x.gcode', bed_clear='cancel', host='h', port=1,
                     intended_tool='extruder1', out_dir=tmp_path)
    assert res['snapshot']['brightness_ok'] is False
    assert res['snapshot']['ok'] is False
    assert res['approval_token'] is None
    assert 'unusable photo' in res['next_step'] or 'too dark' in res['snapshot']['error']


def test_stage2_refuses_without_token(monkeypatch, tmp_path):
    monkeypatch.setattr(g, 'query_state', lambda h, p: idle())
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture(success=True))
    called = {'start': False}
    res = g.run_gate('x.gcode', bed_clear='start', host='h', port=1,
                     intended_tool='extruder1', out_dir=tmp_path,
                     start_func=lambda *a: called.__setitem__('start', True))
    assert not res['started']
    assert 'approval token' in res['reason']
    assert not called['start']


def test_stage2_refuses_with_wrong_token(monkeypatch, tmp_path):
    monkeypatch.setattr(g, 'query_state', lambda h, p: idle())
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture(success=True))
    # Stage 1 first, capture the legit token
    res1 = g.run_gate('x.gcode', bed_clear='cancel', host='h', port=1,
                      intended_tool='extruder1', out_dir=tmp_path)
    legit_token = res1['approval_token']
    # Now Stage 2 with a fake token
    called = {'start': False}
    res2 = g.run_gate('x.gcode', bed_clear='start', host='h', port=1,
                     intended_tool='extruder1', out_dir=tmp_path,
                     approval_token='deadbeef' * 4,
                     start_func=lambda *a: called.__setitem__('start', True))
    assert not res2['started']
    assert 'does not match' in res2['reason']
    assert not called['start']
    assert legit_token != 'deadbeef' * 4


def test_stage2_accepts_valid_token_and_starts(monkeypatch, tmp_path):
    monkeypatch.setattr(g, 'query_state', lambda h, p: idle())
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture(success=True))
    rid = _seed_can_start_passing_request()
    res1 = g.run_gate('x.gcode', bed_clear='cancel', host='h', port=1,
                      intended_tool='extruder1', out_dir=tmp_path,
                      request_id=rid)
    token = res1['approval_token']
    res2 = g.run_gate('x.gcode', bed_clear='start', host='h', port=1,
                     intended_tool='extruder1', out_dir=tmp_path,
                     approval_token=token,
                     request_id=rid,
                     start_func=lambda *a: {'result': 'ok'})
    assert res2['started'] is True


def test_stage2_refuses_when_sanity_capture_is_mock(monkeypatch, tmp_path):
    # Stage 1 succeeded with a real photo + token. Stage 2 sanity capture
    # fails (camera unreachable). Refuse: we can't verify nothing changed.
    monkeypatch.setattr(g, 'query_state', lambda h, p: idle())
    # Stage 1: real capture so token is issued
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture(success=True))
    rid = _seed_can_start_passing_request()
    res1 = g.run_gate('x.gcode', bed_clear='cancel', host='h', port=1,
                      intended_tool='extruder1', out_dir=tmp_path,
                      request_id=rid)
    token = res1['approval_token']
    assert token, 'Stage 1 setup should have produced a token'
    # Now swap capture to mock so Stage 2 sanity capture fails
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture(is_mock=True, success=False))
    called = {'start': False}
    res2 = g.run_gate('x.gcode', bed_clear='start', host='h', port=1,
                     intended_tool='extruder1', out_dir=tmp_path,
                     approval_token=token,
                     request_id=rid,
                     start_func=lambda *a: called.__setitem__('start', True))
    assert not res2['started']
    assert 'sanity capture failed' in res2['reason']
    assert not called['start']


def test_filename_normalization_strips_host_path(monkeypatch, tmp_path):
    # Audit round-11 bug: host path → HTTP 400. Normalize to basename.
    monkeypatch.setattr(g, 'query_state', lambda h, p: idle())
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture(success=True))
    res = g.run_gate('/opt/data/artifacts/x/y/wall_mount.gcode', host='h', port=1,
                     intended_tool='extruder1', out_dir=tmp_path)
    assert res['filename'] == 'wall_mount.gcode'
    assert res['printer_storage_filename'] == 'wall_mount.gcode'
    assert res['gcode_host_path'] == '/opt/data/artifacts/x/y/wall_mount.gcode'


def test_filename_normalization_passthrough_for_bare_name(monkeypatch, tmp_path):
    monkeypatch.setattr(g, 'query_state', lambda h, p: idle())
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture(success=True))
    res = g.run_gate('wall_mount.gcode', host='h', port=1,
                     intended_tool='extruder1', out_dir=tmp_path)
    assert res['filename'] == 'wall_mount.gcode'
    assert res['printer_storage_filename'] == 'wall_mount.gcode'
    assert res['gcode_host_path'] is None


def test_stage1_writes_token_to_per_request_dir(monkeypatch, tmp_path):
    """Live bug regression 2026-06-28: token storage was global, which let
    request B's Stage 2 attempt pick up request A's leftover token from a
    prior session (88-min-old refusal). When --request-id is passed,
    Stage 1 must write the token + photo inside the per-request directory."""
    monkeypatch.setattr(g, 'query_state', lambda h, p: idle())
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture(success=True))
    rid = u1_request.generate_request_id()
    u1_request.write_request(rid, gcode_hash='sha256:per_req',
                             safety={'bed_clear_check_required': True,
                                     'bed_clear_photo_captured': False})
    # Note: NOT passing out_dir — the gate should derive it from request_id
    res = g.run_gate('x.gcode', bed_clear='cancel', host='h', port=1,
                     intended_tool='extruder1', request_id=rid)
    assert res['approval_token'], 'Stage 1 should have issued a token'
    request_dir = u1_request.request_dir(rid)
    assert (request_dir / 'bed_snapshot.approval_token.json').exists(), \
        f'token should be in per-request dir {request_dir}, not global'
    assert (request_dir / 'bed_snapshot.jpg').exists(), \
        'photo should be in per-request dir too'


def test_stage2_cross_request_token_leakage_prevented(monkeypatch, tmp_path):
    """The marquee fix: request A captures Stage 1 → token written to
    request A's dir. Request B's Stage 2 attempt without its own Stage 1
    capture must NOT find request A's token (no global lookup)."""
    monkeypatch.setattr(g, 'query_state', lambda h, p: idle())
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture(success=True))
    # Request A: Stage 1 captures a token in its dir
    rid_a = _seed_can_start_passing_request('a.gcode')
    res_a = g.run_gate('a.gcode', bed_clear='cancel', host='h', port=1,
                       intended_tool='extruder1', request_id=rid_a)
    token_a = res_a['approval_token']
    assert token_a
    # Request B: brand-new request, never ran Stage 1. Try Stage 2 with
    # request A's leaked token.
    rid_b = _seed_can_start_passing_request('b.gcode')
    called = {'start': False}
    res_b = g.run_gate('b.gcode', bed_clear='start', host='h', port=1,
                       intended_tool='extruder1', request_id=rid_b,
                       approval_token=token_a,
                       start_func=lambda *a: called.__setitem__('start', True))
    assert res_b['started'] is False, \
        f"cross-request token leak — request B accepted request A's token: {res_b}"
    assert called['start'] is False
    assert 'token' in res_b['reason'].lower(), \
        f'expected token-related refusal, got: {res_b["reason"]!r}'


def test_stage2_filename_basename_sent_to_start_func(monkeypatch, tmp_path):
    # Verifies the basename is what gets sent to /printer/print/start.
    monkeypatch.setattr(g, 'query_state', lambda h, p: idle())
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture(success=True))
    rid = _seed_can_start_passing_request('wall_mount.gcode')
    res1 = g.run_gate('/opt/data/artifacts/x/wall_mount.gcode', bed_clear='cancel',
                      host='h', port=1, intended_tool='extruder1', out_dir=tmp_path,
                      request_id=rid)
    token = res1['approval_token']
    captured_filename = {'f': None}
    def fake_start(host, port, filename):
        captured_filename['f'] = filename
        return {'result': 'ok'}
    res2 = g.run_gate('/opt/data/artifacts/x/wall_mount.gcode', bed_clear='start',
                     host='h', port=1, intended_tool='extruder1', out_dir=tmp_path,
                     approval_token=token, start_func=fake_start,
                     request_id=rid)
    assert res2['started'] is True
    assert captured_filename['f'] == 'wall_mount.gcode'  # basename, not host path


def test_expired_token_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(g, 'query_state', lambda h, p: idle())
    monkeypatch.setattr(g, 'capture_real_bed_photo', _fake_capture(success=True))
    res1 = g.run_gate('x.gcode', bed_clear='cancel', host='h', port=1,
                      intended_tool='extruder1', out_dir=tmp_path)
    token = res1['approval_token']
    # Tamper the stored token's timestamp to expire it
    token_path = tmp_path / 'bed_snapshot.approval_token.json'
    stored = json.loads(token_path.read_text())
    stored['timestamp_utc'] = '2020-01-01T00:00:00+00:00'  # ancient
    token_path.write_text(json.dumps(stored))
    res2 = g.run_gate('x.gcode', bed_clear='start', host='h', port=1,
                     intended_tool='extruder1', out_dir=tmp_path,
                     approval_token=token, start_func=lambda *a: {'result': 'ok'})
    assert not res2['started']
    assert 'TTL' in res2['reason'] or 'old' in res2['reason']


def test_brightness_measurement_with_real_jpeg(tmp_path):
    # Verify _measure_brightness produces sensible values for known images.
    # Skipped if PIL not available.
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("PIL not available")
    dark = tmp_path / 'dark.jpg'
    Image.new('RGB', (32, 32), (5, 5, 5)).save(dark, 'JPEG')
    bright = tmp_path / 'bright.jpg'
    Image.new('RGB', (32, 32), (200, 200, 200)).save(bright, 'JPEG')
    dark_luma = g._measure_brightness(dark)
    bright_luma = g._measure_brightness(bright)
    assert dark_luma is not None and dark_luma < g.DARK_PHOTO_MEAN_LUMA
    assert bright_luma is not None and bright_luma > g.DARK_PHOTO_MEAN_LUMA
