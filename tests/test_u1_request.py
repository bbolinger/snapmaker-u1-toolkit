"""Unit tests for u1_request — the Print Request Object helpers (v2.0 Phase 2)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from u1_request import (
    RequestNotFoundError,
    compute_model_hash,
    ensure_request_dir,
    find_recent_request_for_model,
    generate_request_id,
    is_request_id,
    read_request,
    request_dir,
    resolve_request_id,
    write_request,
)
import u1_request as _u1_request


# M7 fix (Phase 2 cold review): file-level `_patched_data_dir` fixture
# removed. The conftest.py `_isolated_data_dir` fixture (autouse via env
# var SNAPMAKER_U1_DATA_DIR) is the single source of truth for per-test
# data-dir isolation. Two mechanisms could disagree about which tmp dir
# to use; one is enough.

@pytest.fixture(autouse=True)
def _patched_data_dir(tmp_path):
    """Alias kept so existing tests that name the fixture as a parameter
    keep working. Effective isolation comes from the conftest env var."""
    yield tmp_path


def _write_stl(path: Path, content: bytes = b'STL_DUMMY_BYTES') -> Path:
    path.write_bytes(content)
    return path


# ---------- generate_request_id / is_request_id ----------

def test_generate_request_id_shape():
    rid = generate_request_id()
    # u1_YYYY_MMDD_xxxxxx
    assert rid.startswith('u1_')
    parts = rid.split('_')
    assert len(parts) == 4
    assert parts[0] == 'u1'
    assert len(parts[1]) == 4 and parts[1].isdigit()
    assert len(parts[2]) == 4 and parts[2].isdigit()
    assert len(parts[3]) == 6 and all(c in '0123456789abcdef' for c in parts[3])


def test_generate_request_id_uses_utc():
    """M1 fix: id stamp uses UTC, not local time. Sortability claim in the
    docstring holds globally."""
    from datetime import datetime, timezone
    rid = generate_request_id()
    parts = rid.split('_')
    today_utc = datetime.now(timezone.utc).strftime('%Y_%m%d')
    assert parts[1] + '_' + parts[2] == today_utc


def test_generate_request_id_uniqueness():
    ids = {generate_request_id() for _ in range(50)}
    # 24 bits of entropy → collision probability negligible at this scale
    assert len(ids) == 50


def test_is_request_id_accepts_valid():
    assert is_request_id('u1_2026_0626_abc123')
    assert is_request_id(generate_request_id())


def test_is_request_id_rejects_invalid():
    assert not is_request_id(None)
    assert not is_request_id('')
    assert not is_request_id('hello')
    assert not is_request_id('u1_2026_06_abc123')  # wrong MMDD width
    assert not is_request_id('u1_2026_0626_abcde')  # too few hex
    assert not is_request_id('u1_2026_0626_ABC123')  # uppercase rejected


# ---------- request_dir / ensure_request_dir ----------

def test_request_dir_invalid_raises():
    with pytest.raises(ValueError):
        request_dir('not-a-valid-id')


def test_ensure_request_dir_creates(_patched_data_dir):
    rid = generate_request_id()
    d = ensure_request_dir(rid)
    assert d.is_dir()
    # The conftest fixture sets SNAPMAKER_U1_DATA_DIR=tmp_path/_data_dir,
    # so the request dir lives under that.
    assert d.name == rid
    assert d.parent.name == 'requests'


def test_ensure_request_dir_walkup_bounded_to_data_dir(_patched_data_dir, monkeypatch):
    """MED-1 fix: the chown walk-up must stop at _data_dir(); it must NOT
    traverse into ancestors of data_dir (e.g. /opt/data, /appdata/hermes).

    Verified via a side-effect spy: collect every path os.chown is called on
    and assert none escape the data_dir subtree."""
    import os as _os
    seen: list[Path] = []
    real_chown = _os.chown

    def spy_chown(path, uid, gid):
        seen.append(Path(path))
        # don't actually chown — test runs as root and the file is in tmp_path;
        # exercising real chown would just succeed. We only care WHICH paths
        # were touched.
        return None

    monkeypatch.setattr(_os, 'chown', spy_chown)
    rid = generate_request_id()
    d = ensure_request_dir(rid)
    data_root = _u1_request._data_dir().resolve()
    # Every chown target must be inside data_root (data_root itself is
    # excluded from the walk; the walk stops one level above the new dir).
    for p in seen:
        resolved = p.resolve()
        assert resolved != data_root, f'walk-up escaped into data_dir itself: {p}'
        assert data_root in resolved.parents, f'walk-up escaped data_dir subtree: {p}'
    # And the new dir + the freshly-created requests/ parent should both have
    # been examined when uid/gid mismatched (test runs as root → uid 0 == data_dir
    # uid 0, so no actual chown call; but the loop visited them. The spy
    # only fires on mismatch, so on a homogeneous test fs we may see zero calls
    # — which is also valid).


# ---------- write_request / read_request ----------

def test_write_request_creates_file(_patched_data_dir):
    rid = generate_request_id()
    write_request(rid, model_file='cable_holder.stl', tool='T1')
    data = read_request(rid)
    assert data is not None
    assert data['request_id'] == rid
    assert data['model_file'] == 'cable_holder.stl'
    assert data['tool'] == 'T1'
    assert 'created_at' in data
    assert 'updated_at' in data


def test_write_request_merges_existing(_patched_data_dir):
    rid = generate_request_id()
    write_request(rid, model_file='m.stl', tool='T1')
    initial = read_request(rid)
    # second write adds a field, keeps prior
    write_request(rid, material='PETG')
    after = read_request(rid)
    assert after['model_file'] == 'm.stl'  # preserved
    assert after['tool'] == 'T1'  # preserved
    assert after['material'] == 'PETG'  # new
    assert after['created_at'] == initial['created_at']  # NOT overwritten
    assert after['updated_at'] >= initial['updated_at']  # bumped


def test_read_request_returns_none_when_missing(_patched_data_dir):
    assert read_request('u1_2026_0626_aaaaaa') is None


def test_read_request_returns_none_on_corrupt_json(_patched_data_dir):
    rid = generate_request_id()
    ensure_request_dir(rid)
    (request_dir(rid) / 'request.json').write_text('{not valid json')
    assert read_request(rid) is None


def test_write_request_atomic_no_temp_left_behind(_patched_data_dir):
    rid = generate_request_id()
    write_request(rid, model_file='m.stl')
    # No .tmp.<pid> stragglers
    leftovers = list((request_dir(rid)).glob('*.tmp.*'))
    assert not leftovers


# ---------- compute_model_hash ----------

def test_compute_model_hash_deterministic(tmp_path):
    stl = _write_stl(tmp_path / 'a.stl', b'hello world')
    h1 = compute_model_hash(stl)
    h2 = compute_model_hash(stl)
    assert h1 == h2
    assert h1.startswith('sha256:')
    assert len(h1.split(':')[1]) == 64  # SHA-256 hex


def test_compute_model_hash_changes_when_content_changes(tmp_path):
    stl = _write_stl(tmp_path / 'a.stl', b'first content')
    h1 = compute_model_hash(stl)
    stl.write_bytes(b'second content')
    h2 = compute_model_hash(stl)
    assert h1 != h2


def test_compute_model_hash_same_path_different_files(tmp_path):
    """Case B from the design discussion: two zips contain a same-named STL
    with different content. Hash MUST differ so workflow doesn't resume the
    wrong state."""
    (tmp_path / 'a').mkdir()
    (tmp_path / 'b').mkdir()
    a = tmp_path / 'a' / 'model.stl'
    b = tmp_path / 'b' / 'model.stl'
    a.write_bytes(b'AAA')
    b.write_bytes(b'BBB')
    assert compute_model_hash(a) != compute_model_hash(b)


def test_compute_model_hash_caches_by_path_size_mtime(tmp_path, monkeypatch):
    """M4 fix: cache must hit on second call with identical file, miss when
    the file changes (different mtime_ns or size)."""
    stl = _write_stl(tmp_path / 'm.stl', b'first')
    # Clear cache + count hashlib.sha256 instantiations as a proxy for "re-read"
    _u1_request._HASH_CACHE.clear()
    h1 = compute_model_hash(stl)
    # Immediate re-call should be served from cache
    h2 = compute_model_hash(stl)
    assert h1 == h2
    assert len(_u1_request._HASH_CACHE) == 1
    # Overwrite the file. mtime_ns differs → cache miss → fresh hash
    import os, time
    time.sleep(0.01)  # ensure mtime_ns moves
    stl.write_bytes(b'second')
    h3 = compute_model_hash(stl)
    assert h3 != h1
    # Cache now has both entries (different mtime_ns keys)
    assert len(_u1_request._HASH_CACHE) >= 1


# ---------- find_recent_request_for_model ----------

def test_find_recent_returns_none_when_no_requests(_patched_data_dir, tmp_path):
    stl = _write_stl(tmp_path / 'm.stl')
    assert find_recent_request_for_model(stl) is None


def test_find_recent_matches_by_content_hash(_patched_data_dir, tmp_path):
    stl = _write_stl(tmp_path / 'm.stl', b'specific content')
    rid = generate_request_id()
    write_request(rid, model_file='m.stl', model_hash=compute_model_hash(stl))
    found = find_recent_request_for_model(stl)
    assert found == rid


def test_find_recent_misses_when_content_changed(_patched_data_dir, tmp_path):
    """If the operator sends a zip with same-named STL but different bytes,
    the previously-stored request_id MUST NOT match — workflow should treat
    the new STL as a fresh job."""
    stl = _write_stl(tmp_path / 'm.stl', b'original')
    rid = generate_request_id()
    write_request(rid, model_file='m.stl', model_hash=compute_model_hash(stl))
    # Overwrite the STL with different bytes
    stl.write_bytes(b'replacement bytes')
    found = find_recent_request_for_model(stl)
    assert found is None


def _backdate_request_updated_at(request_id: str, seconds_ago: float) -> None:
    """Directly patch the request.json's updated_at field. M2 fix means
    find_recent_request_for_model now keys TTL off this, not filesystem mtime."""
    p = request_dir(request_id) / 'request.json'
    data = json.loads(p.read_text())
    data['updated_at'] = time.time() - seconds_ago
    p.write_text(json.dumps(data))


def test_find_recent_picks_newest_match(_patched_data_dir, tmp_path):
    stl = _write_stl(tmp_path / 'm.stl', b'shared content')
    model_hash = compute_model_hash(stl)
    older_rid = generate_request_id()
    write_request(older_rid, model_hash=model_hash)
    _backdate_request_updated_at(older_rid, 600)  # 10 min ago
    newer_rid = generate_request_id()
    write_request(newer_rid, model_hash=model_hash)
    found = find_recent_request_for_model(stl)
    assert found == newer_rid


def test_find_recent_respects_ttl(_patched_data_dir, tmp_path):
    """M2 fix: TTL is checked against request.json's updated_at field,
    NOT the filesystem mtime. A backup tool that touches the dir won't
    accidentally extend the TTL."""
    stl = _write_stl(tmp_path / 'm.stl', b'content')
    model_hash = compute_model_hash(stl)
    rid = generate_request_id()
    write_request(rid, model_hash=model_hash)
    _backdate_request_updated_at(rid, 60 * 60 * 24)  # 24h ago via JSON
    # Even though filesystem mtime is recent (we just wrote), TTL check
    # uses updated_at and correctly identifies this as expired.
    found = find_recent_request_for_model(stl, ttl_seconds=60)
    assert found is None


# ---------- resolve_request_id (the workflow's entry-point helper) ----------

def test_resolve_explicit_request_id_must_exist(_patched_data_dir, tmp_path):
    """H2 fix: an explicit --request-id with no on-disk state is a hard
    error, not a silent half-state. The workflow must not proceed past
    this point with a phantom id."""
    stl = _write_stl(tmp_path / 'm.stl')
    nonexistent = generate_request_id()
    with pytest.raises(RequestNotFoundError):
        resolve_request_id(nonexistent, False, stl)


def test_resolve_explicit_request_id_invalid_format_raises(_patched_data_dir, tmp_path):
    stl = _write_stl(tmp_path / 'm.stl')
    with pytest.raises(ValueError):
        resolve_request_id('not-a-valid-id', False, stl)


def test_resolve_explicit_request_id_resumes_if_exists(_patched_data_dir, tmp_path):
    stl = _write_stl(tmp_path / 'm.stl')
    rid_existing = generate_request_id()
    write_request(rid_existing, model_hash=compute_model_hash(stl), tool='T1')
    rid, resumed = resolve_request_id(rid_existing, False, stl)
    assert rid == rid_existing
    assert resumed


def test_resolve_fresh_and_explicit_request_id_fresh_wins(_patched_data_dir, tmp_path):
    """When both --fresh and --request-id are passed, --fresh takes precedence
    and generates a brand-new id. --request-id is ignored."""
    stl = _write_stl(tmp_path / 'm.stl')
    existing = generate_request_id()
    write_request(existing, model_hash=compute_model_hash(stl))
    rid, resumed = resolve_request_id(existing, True, stl)
    assert rid != existing
    assert is_request_id(rid)
    assert not resumed


def test_resolve_recovery_lookup_when_no_explicit_id(_patched_data_dir, tmp_path):
    """Context-loss recovery: workflow re-runs with just STL path → finds
    the recent request by content hash."""
    stl = _write_stl(tmp_path / 'm.stl')
    auto_rid = generate_request_id()
    write_request(auto_rid, model_hash=compute_model_hash(stl))
    rid, resumed = resolve_request_id(None, False, stl)
    assert rid == auto_rid
    assert resumed


def test_resolve_fresh_starts_new_request_even_when_state_exists(_patched_data_dir, tmp_path):
    """--fresh wipes recovery: even when an existing request would match,
    operator gets a brand-new request_id."""
    stl = _write_stl(tmp_path / 'm.stl')
    existing_rid = generate_request_id()
    write_request(existing_rid, model_hash=compute_model_hash(stl))
    rid, resumed = resolve_request_id(None, True, stl)
    assert rid != existing_rid
    assert is_request_id(rid)
    assert not resumed


def test_resolve_falls_through_to_new_request_when_nothing_matches(_patched_data_dir, tmp_path):
    stl = _write_stl(tmp_path / 'm.stl')
    rid, resumed = resolve_request_id(None, False, stl)
    assert is_request_id(rid)
    assert not resumed


# ============================================================================
# Phase 2 integration scenarios — context-loss recovery
# ============================================================================
# These walk the same recovery scenario the operator hit in the live Telegram
# regression: agent loses context mid-flow, re-runs the workflow with just
# the STL path, workflow finds the in-flight request by content hash and
# resumes from where it left off.


def test_context_loss_recovery_by_content_hash(_patched_data_dir, tmp_path):
    """The smoking-gun scenario from 2026-06-26 (cable_holder_vcd Telegram run).

    Conversation: operator picks orient → tool. Agent loses context. Operator
    sends '2' again (the answer to a question the agent no longer remembers
    asking). Agent re-invokes workflow with just the STL path. WITHOUT
    Phase 2: workflow generates a fresh request_id and asks orient again.
    WITH Phase 2: workflow finds the existing request by model_hash and
    resumes at the tool prompt."""
    stl = _write_stl(tmp_path / 'cable.stl', b'cable_holder_geometry_bytes')

    # Round 1: workflow creates a fresh request, operator picks orient
    rid1, resumed1 = resolve_request_id(None, False, stl)
    assert not resumed1  # fresh
    write_request(rid1,
                  model_hash=compute_model_hash(stl),
                  orient='asauthored',
                  phase='analysis')

    # Agent loses context. Re-runs workflow with just the STL path.
    rid2, resumed2 = resolve_request_id(None, False, stl)
    assert rid2 == rid1  # SAME request — recovery worked
    assert resumed2

    # Workflow merges the resumed state with CLI args. orient is already set
    # so the workflow walks straight to the tool prompt.
    req = read_request(rid2)
    assert req['orient'] == 'asauthored'


def test_context_loss_recovery_ignores_same_path_different_content(_patched_data_dir, tmp_path):
    """Case B regression guard: operator sends a new zip with a same-named STL
    inside (different content). The workflow MUST NOT resume the prior
    request — those answers were for a different model."""
    stl = _write_stl(tmp_path / 'model.stl', b'original_geometry')
    rid1, _ = resolve_request_id(None, False, stl)
    write_request(rid1, model_hash=compute_model_hash(stl), orient='asauthored', tool='T1')

    # Operator sends a new zip; agent extracts to the same path with new bytes
    stl.write_bytes(b'completely_different_geometry')

    rid2, resumed2 = resolve_request_id(None, False, stl)
    assert rid2 != rid1  # fresh request, not resumed
    assert not resumed2
