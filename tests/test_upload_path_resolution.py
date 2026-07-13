"""Tests for doc-hash upload-path recovery and the .zip single-flow guard.

The driving agent occasionally mangles the human-readable suffix of an uploaded
file name (a '+' turns into '_') so the typed path points at nothing. The
``doc_<hash>`` prefix stays stable, so ``resolve_upload_path`` recovers the real
file, and a .zip that can't be confirmed as a kit must never be handed to the
single-STL flow (which would slice only the first model or fail outright).
"""
from __future__ import annotations

import json
import zipfile

import u1_kit
from u1_slice_workflow import main


def _touch(p):
    p.write_bytes(b"stub")
    return p


def test_resolve_finds_real_file_by_doc_hash_when_suffix_mangled(tmp_path):
    real = _touch(tmp_path / "doc_abc123def456_real name.zip")
    asked = tmp_path / "doc_abc123def456_MANGLED_name.zip"
    assert u1_kit.resolve_upload_path(asked) == real


def test_resolve_returns_path_unchanged_when_it_exists(tmp_path):
    real = _touch(tmp_path / "doc_abc123def456_present.zip")
    assert u1_kit.resolve_upload_path(real) == real


def test_resolve_returns_path_unchanged_for_non_doc_basename(tmp_path):
    _touch(tmp_path / "doc_abc123def456_real.zip")
    asked = tmp_path / "just_a_model.zip"  # no doc_<hash> prefix
    assert u1_kit.resolve_upload_path(asked) == asked


def test_resolve_returns_path_unchanged_when_ambiguous(tmp_path):
    _touch(tmp_path / "doc_abc123def456_one.zip")
    _touch(tmp_path / "doc_abc123def456_two.zip")
    asked = tmp_path / "doc_abc123def456_MANGLED.zip"
    assert u1_kit.resolve_upload_path(asked) == asked


def _real_zip(tmp_path):
    zp = tmp_path / "kit.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("a.stl", b"solid a\nendsolid a\n")
    return zp


def test_existing_single_object_zip_routes_on_as_kit_of_one(
        tmp_path, capsys, monkeypatch):
    # A real, present .zip that is_multi_part_archive reports as not-multi is a
    # valid kit-of-one (e.g. a single .3mf, or one STL): it must route on to the
    # kit workflow, never be rejected. Guards against over-refusing.
    zp = _real_zip(tmp_path)
    monkeypatch.setattr(u1_kit, "is_multi_part_archive", lambda _p: False)
    main([str(zp), "--json-events"])
    events = [json.loads(l) for l in capsys.readouterr().out.splitlines()
              if l.strip().startswith("{")]
    stages = [e.get("stage") for e in events]
    assert "kit_detected" in stages, stages
    assert "kit_detection_failed" not in stages, stages


def test_missing_zip_is_refused_not_parsed_as_single_model(
        tmp_path, capsys, monkeypatch):
    # A .zip path that does not exist (the agent mangled the name past recovery)
    # must be surfaced clearly, never handed to the single-STL parser (which
    # would fail with a confusing "unsupported model file").
    missing = tmp_path / "doc_abc123def456_gone.zip"  # no such file on disk
    monkeypatch.setattr(u1_kit, "is_multi_part_archive", lambda _p: False)
    main([str(missing), "--json-events"])
    events = [json.loads(l) for l in capsys.readouterr().out.splitlines()
              if l.strip().startswith("{")]
    stages = [e.get("stage") for e in events]
    assert "kit_detection_failed" in stages, stages
    assert "kit_detected" not in stages, stages
