"""One-shot migrator tests — pre-Phase-3 request.json → v1 schema (3a)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import u1_request
import migrate_v0_to_v1


def _seed_pre_v1_request(tmp_path, rid: str, **fields) -> Path:
    """Place a pre-Phase-3 request.json on disk WITHOUT going through
    write_request (which would auto-inject the v1 defaults we want to
    test the migrator against)."""
    d = u1_request.ensure_request_dir(rid)
    p = d / 'request.json'
    base = {
        'request_id': rid,
        'created_at': 1700000000.0,
        'updated_at': 1700000000.0,
        'model_file': 'm.stl',
        'phase': 'awaiting_start_approval',  # pre-v1 used phase
    }
    base.update(fields)
    p.write_text(json.dumps(base, indent=2))
    return p


def test_migrate_adds_v1_schema_fields(tmp_path):
    rid = u1_request.generate_request_id()
    _seed_pre_v1_request(tmp_path, rid)
    rc = migrate_v0_to_v1.main([])
    assert rc == 0
    after = json.loads((u1_request.request_dir(rid) / 'request.json').read_text())
    assert after['schema_version'] == 1
    assert after['request_revision'] == 1
    # Approvals BOTH default to false (the spec is very explicit about not
    # fabricating prior approval state).
    assert after['approvals']['upload']['approved'] is False
    assert after['approvals']['start']['approved'] is False
    assert after['approvals']['start']['approved_revision'] is None
    assert after['approvals']['start']['approved_gcode_hash'] is None
    # Safety block defaults to the conservative shape (bed-clear required).
    assert after['safety']['bed_clear_check_required'] is True
    assert after['safety']['bed_clear_photo_captured'] is False
    # capability_mode is NOT added by Phase 3 migrator — that's Phase 4.
    assert 'capability_mode' not in after['safety']
    # Phase 2 fields PRESERVED — migrator is additive.
    assert after['phase'] == 'awaiting_start_approval'
    assert after['model_file'] == 'm.stl'


def test_migrate_preserves_existing_phase2_fields(tmp_path):
    """A Phase 2 request with model_hash, gcode_hash, orient, tool, etc.
    should keep all those fields intact through migration."""
    rid = u1_request.generate_request_id()
    _seed_pre_v1_request(
        tmp_path, rid,
        model_hash='sha256:abc',
        gcode_hash='sha256:def',
        orient='asauthored',
        tool='T1',
        material='PETG',
        profile='0.20strength',
        supports='no_supports',
        printer_storage_filename='m_plate_1.gcode',
        start_gate_stage1_command='python3 /opt/data/scripts/u1_print_start_gate.py m_plate_1.gcode',
    )
    migrate_v0_to_v1.main([])
    after = json.loads((u1_request.request_dir(rid) / 'request.json').read_text())
    for fld in ('model_hash', 'gcode_hash', 'orient', 'tool', 'material',
                'profile', 'supports', 'printer_storage_filename',
                'start_gate_stage1_command'):
        assert fld in after, f'migrator dropped {fld}'


def test_migrate_idempotent(tmp_path):
    """Running the migrator twice in a row should be a no-op the second time."""
    rid = u1_request.generate_request_id()
    _seed_pre_v1_request(tmp_path, rid)
    migrate_v0_to_v1.main([])
    first_pass = json.loads((u1_request.request_dir(rid) / 'request.json').read_text())
    # Second pass should produce identical JSON (no spurious bumps).
    migrate_v0_to_v1.main([])
    second_pass = json.loads((u1_request.request_dir(rid) / 'request.json').read_text())
    assert first_pass == second_pass


def test_migrate_skips_already_v1_request(tmp_path):
    """A request already at v1 (e.g. created post-Phase-3) should be a noop."""
    rid = u1_request.generate_request_id()
    u1_request.write_request(rid, model_file='m.stl', tool='T1')  # auto-stamps v1
    before = json.loads((u1_request.request_dir(rid) / 'request.json').read_text())
    migrate_v0_to_v1.main([])
    after = json.loads((u1_request.request_dir(rid) / 'request.json').read_text())
    assert before == after


def test_migrate_dry_run_changes_nothing(tmp_path):
    rid = u1_request.generate_request_id()
    _seed_pre_v1_request(tmp_path, rid)
    before = (u1_request.request_dir(rid) / 'request.json').read_text()
    rc = migrate_v0_to_v1.main(['--dry-run'])
    assert rc == 0
    after = (u1_request.request_dir(rid) / 'request.json').read_text()
    assert before == after


def test_migrate_skips_corrupt_request_json(tmp_path, capsys):
    rid = u1_request.generate_request_id()
    d = u1_request.ensure_request_dir(rid)
    (d / 'request.json').write_text('{not valid json')
    rc = migrate_v0_to_v1.main([])
    assert rc == 0
    # File untouched (still corrupt)
    assert (d / 'request.json').read_text() == '{not valid json'
    # Reported as bad
    out = capsys.readouterr().out
    assert 'bad' in out


def test_migrate_skips_non_request_id_dirs(tmp_path):
    """Stray subdirs in requests/ (e.g. an operator's bookkeeping folder)
    should be ignored, not crashed on."""
    from u1_request import _requests_root
    root = _requests_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / 'not_a_request_id').mkdir()
    rc = migrate_v0_to_v1.main([])
    assert rc == 0  # no crash


def test_migrate_handles_empty_requests_dir(tmp_path):
    """No requests directory at all — exits 0 with informative message."""
    rc = migrate_v0_to_v1.main([])
    assert rc == 0
