"""Test print_history ledger writers — append-only contract + JSON safety."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import u1_print_history as ph


def test_read_json_returns_default_when_missing(tmp_path):
    out = ph.read_json(tmp_path / "nope.json", default={"x": 1})
    assert out == {"x": 1}


def test_read_json_returns_default_when_corrupt(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid")
    out = ph.read_json(bad, default={"safe": True})
    assert out == {"safe": True}


def test_write_json_roundtrips(tmp_path):
    target = tmp_path / "sub" / "nested.json"
    ph.write_json(target, {"foo": "bar", "n": 42})
    assert target.exists()
    assert json.loads(target.read_text()) == {"foo": "bar", "n": 42}


def test_write_json_is_atomic_no_tmp_leak_on_success(tmp_path):
    """The atomic-rename path must clean up the tmpfile so no .tmp files leak."""
    target = tmp_path / "atomic.json"
    ph.write_json(target, {"ok": True})
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == [], f"unexpected tmpfile leak: {leftover}"


def test_write_json_overwrites_existing_without_truncation_window(tmp_path):
    """Existing file → fresh contents must replace it cleanly. The atomic
    rename guarantees a concurrent reader never sees a half-written file."""
    target = tmp_path / "overwrite.json"
    ph.write_json(target, {"version": 1, "data": "first"})
    ph.write_json(target, {"version": 2, "data": "second"})
    assert json.loads(target.read_text()) == {"version": 2, "data": "second"}


def test_write_json_cleans_up_tmp_on_io_error(tmp_path, monkeypatch):
    """If os.replace fails, the tmpfile must be removed — no garbage leak."""
    import os
    target = tmp_path / "fails.json"
    real_replace = os.replace

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated"):
        ph.write_json(target, {"x": 1})
    monkeypatch.setattr(os, "replace", real_replace)
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == [], f"tmpfile leaked after failed replace: {leftover}"


def test_append_event_creates_jsonl_lines(tmp_path, monkeypatch):
    """append_event must produce one JSON object per line (.jsonl format)."""
    monkeypatch.setattr(ph, "HISTORY_JSONL", tmp_path / "events.jsonl")
    ph.append_event({"event": "started", "id": 1})
    ph.append_event({"event": "completed", "id": 1})
    lines = (tmp_path / "events.jsonl").read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "started"
    assert json.loads(lines[1])["event"] == "completed"


def test_find_record_locates_by_job_key():
    records = [
        {"job_key": "a", "title": "first"},
        {"job_key": "b", "title": "second"},
    ]
    rec = ph.find_record(records, "b")
    assert rec is not None and rec["title"] == "second"


def test_find_record_returns_none_when_missing():
    assert ph.find_record([{"job_key": "a"}], "b") is None


def test_progress_value_handles_missing_field():
    assert ph.progress_value({}) is None


def test_progress_value_extracts_display_status_progress():
    state = {"display_status": {"progress": 0.42}}
    assert ph.progress_value(state) == 0.42


def test_progress_value_handles_garbage_gracefully():
    """A non-numeric progress field should not crash the script — return None."""
    state = {"display_status": {"progress": "not-a-number"}}
    assert ph.progress_value(state) is None


def test_layer_info_extracts_current_and_total():
    ps = {"info": {"current_layer": 100, "total_layer": 200}}
    cur, total = ph.layer_info(ps)
    assert cur == 100 and total == 200


def test_layer_info_safe_when_missing():
    cur, total = ph.layer_info({})
    assert cur is None and total is None
