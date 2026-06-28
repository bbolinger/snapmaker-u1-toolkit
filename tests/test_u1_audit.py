"""Unit tests for u1_audit — per-request audit log (v2.0 Phase 3a)."""
from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

import u1_audit
import u1_request


# ---------- append + read happy path ----------

def test_append_writes_one_jsonl_line(tmp_path):
    rid = u1_request.generate_request_id()
    u1_audit.append(rid, 'request_created', operator='cli:test',
                    model_file='m.stl', model_hash='sha256:aaa')
    p = u1_request.request_dir(rid) / 'audit.jsonl'
    assert p.exists()
    lines = p.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record['event'] == 'request_created'
    assert record['operator'] == 'cli:test'
    assert record['details']['model_file'] == 'm.stl'
    assert record['details']['model_hash'] == 'sha256:aaa'
    assert record['seq'] == 1
    assert 'ts' in record


def test_append_returns_the_written_record(tmp_path):
    rid = u1_request.generate_request_id()
    record = u1_audit.append(rid, 'slicing_completed', operator='cli:test',
                             gcode_hash='sha256:bbb', estimated_time='1h')
    assert record['event'] == 'slicing_completed'
    assert record['details']['gcode_hash'] == 'sha256:bbb'
    assert record['seq'] == 1


def test_append_monotonic_seq(tmp_path):
    rid = u1_request.generate_request_id()
    for i in range(5):
        u1_audit.append(rid, f'event_{i}', operator='cli:test')
    records = list(u1_audit.read(rid))
    assert len(records) == 5
    assert [r['seq'] for r in records] == [1, 2, 3, 4, 5]


def test_append_without_operator_omits_field(tmp_path):
    rid = u1_request.generate_request_id()
    u1_audit.append(rid, 'silent_event')
    records = list(u1_audit.read(rid))
    assert 'operator' not in records[0]
    assert records[0]['event'] == 'silent_event'


# ---------- read filtering ----------

def test_read_returns_empty_when_no_audit_file(tmp_path):
    rid = u1_request.generate_request_id()
    assert list(u1_audit.read(rid)) == []


def test_read_filters_by_since_until(tmp_path):
    rid = u1_request.generate_request_id()
    u1_audit.append(rid, 'first', operator='cli:test')
    time.sleep(0.05)
    cutoff = time.time()
    time.sleep(0.05)
    u1_audit.append(rid, 'second', operator='cli:test')
    earlier = [r['event'] for r in u1_audit.read(rid, until=cutoff)]
    later = [r['event'] for r in u1_audit.read(rid, since=cutoff)]
    assert earlier == ['first']
    assert later == ['second']


def test_read_skips_corrupt_lines(tmp_path):
    rid = u1_request.generate_request_id()
    u1_audit.append(rid, 'good_event', operator='cli:test')
    # Inject a bad line directly
    p = u1_request.request_dir(rid) / 'audit.jsonl'
    with p.open('a') as f:
        f.write('this is not json\n')
    u1_audit.append(rid, 'good_event_2', operator='cli:test')
    records = list(u1_audit.read(rid))
    # Bad line skipped, good ones survive
    assert [r['event'] for r in records] == ['good_event', 'good_event_2']


# ---------- fold ----------

def test_fold_summarizes_state(tmp_path):
    rid = u1_request.generate_request_id()
    u1_audit.append(rid, 'request_created', operator='cli:test', model_hash='sha256:aaa')
    u1_audit.append(rid, 'slicing_completed', operator='cli:test', gcode_hash='sha256:bbb')
    u1_audit.append(rid, 'upload_completed', operator='cli:test',
                    uploaded_filename='m_plate_1.gcode')
    state = u1_audit.fold(rid)
    assert state['event_count'] == 3
    assert state['model_hash'] == 'sha256:aaa'
    assert state['gcode_hash'] == 'sha256:bbb'
    assert state['uploaded_filename'] == 'm_plate_1.gcode'
    assert state['last_operator'] == 'cli:test'
    assert state['seen_events'] == ['request_created', 'slicing_completed', 'upload_completed']


# ---------- concurrency (the L9 deferred case from Phase 2) ----------

def _append_worker(rid: str, n_events: int, prefix: str, data_dir: str) -> None:
    """Worker process: append n_events under the given prefix.

    Crucially re-imports the modules so each subprocess gets a clean
    module-load + fresh _data_dir lookup."""
    os.environ['SNAPMAKER_U1_DATA_DIR'] = data_dir
    import importlib
    import u1_audit as _a
    importlib.reload(_a)
    for i in range(n_events):
        _a.append(rid, f'{prefix}_{i}', operator='cli:test')


def test_append_concurrent_processes_no_lost_writes(tmp_path):
    """The L9 case: two processes appending to the same audit.jsonl. With
    O_APPEND + flock, every write lands, sequence numbers are unique, and
    no line is interleaved (each line is fully one writer's record)."""
    rid = u1_request.generate_request_id()
    # Seed a baseline so the request dir exists before forking.
    u1_audit.append(rid, 'seed', operator='cli:test')
    data_dir = os.environ['SNAPMAKER_U1_DATA_DIR']
    n_per_worker = 25
    n_workers = 4
    procs = []
    for i in range(n_workers):
        p = multiprocessing.Process(
            target=_append_worker,
            args=(rid, n_per_worker, f'p{i}', data_dir),
        )
        procs.append(p)
        p.start()
    for p in procs:
        p.join(timeout=10)
        assert p.exitcode == 0, f'worker {p.pid} failed with exitcode={p.exitcode}'
    records = list(u1_audit.read(rid))
    expected_total = 1 + n_workers * n_per_worker  # seed + per-worker
    assert len(records) == expected_total, f'expected {expected_total}, got {len(records)}'
    # Every line is a valid JSON record (no torn writes).
    p = u1_request.request_dir(rid) / 'audit.jsonl'
    for line in p.read_text().splitlines():
        assert line.startswith('{') and line.endswith('}'), f'torn line: {line!r}'
    # Sequence numbers form a complete consecutive set [1..expected_total].
    seqs = sorted(r['seq'] for r in records)
    assert seqs == list(range(1, expected_total + 1))


# ---------- CLI show ----------

def test_cli_show_renders_chronologically(tmp_path, capsys):
    rid = u1_request.generate_request_id()
    u1_audit.append(rid, 'first', operator='cli:test')
    u1_audit.append(rid, 'second', operator='cli:test', gcode_hash='sha256:bbb')
    rc = u1_audit.main(['show', rid])
    assert rc == 0
    out = capsys.readouterr().out
    # Each event on its own line, with seq + ts + operator + event.
    assert 'first' in out
    assert 'second' in out
    assert 'sha256:bbb' in out
    # The seq numbers should appear in order in the output
    pos_first = out.find('first')
    pos_second = out.find('second')
    assert 0 <= pos_first < pos_second


def test_cli_show_rejects_invalid_request_id(capsys):
    rc = u1_audit.main(['show', 'not-a-valid-id'])
    assert rc == 2
    err = capsys.readouterr().err
    assert 'invalid request_id' in err


def test_cli_show_returns_nonzero_when_no_audit(tmp_path, capsys):
    rid = u1_request.generate_request_id()
    rc = u1_audit.main(['show', rid])
    assert rc == 1
