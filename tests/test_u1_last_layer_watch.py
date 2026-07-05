"""Tests for u1_last_layer_watch.py — first/last-layer photo milestones.

No coverage existed for this module before (live in production since
2026-06-21, watching every real print). Added alongside the fallback-capture
fix for a live-observed miss (2026-07-05): a fast-finishing print (48 layers,
43 min) transitioned printing -> complete between two 1-minute cron ticks
without ever landing a poll inside the LAST_LAYER_WINDOW, so the last-layer
photo silently never fired.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import u1_last_layer_watch as w  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "get_data_dir", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _fake_camera(monkeypatch):
    calls = []

    def _fake_capture(filename, milestone, layer, total_layer):
        calls.append((filename, milestone, layer, total_layer))
        out = w._out_dir() / f"fake_{milestone}_{layer}_{total_layer}.jpg"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\xff\xd8\xff\xe0fake")
        return out, {"ok": True, "result": {"changed": True, "jpeg_magic": True}}

    monkeypatch.setattr(w, "capture_photo", _fake_capture)
    return calls


def _status(print_state, current_layer, total_layer, filename="test.gcode",
            is_active=True, is_paused=False, progress=0.5):
    return {
        "print_stats": {"filename": filename, "state": print_state,
                        "info": {"current_layer": current_layer, "total_layer": total_layer}},
        "virtual_sdcard": {"is_active": is_active, "progress": progress},
        "display_status": {"progress": progress},
        "pause_resume": {"is_paused": is_paused},
    }


def _run(monkeypatch, status):
    monkeypatch.setattr(w, "query_status", lambda: status)
    return w.main()


def test_live_in_window_catch_still_fires_normally(monkeypatch, _fake_camera):
    """Regression: the original in-progress last-layer catch (remaining <=
    LAST_LAYER_WINDOW while still 'printing') must keep working unchanged."""
    _run(monkeypatch, _status("printing", 44, 48))  # remaining=4, within window
    state = w.load_state()
    assert state["last_layer_fired_job_key"] == "test.gcode|48"
    assert state["last_layer_fired_layer"] == 44
    assert "last_layer_caught_post_complete" not in state


def test_fast_finish_missed_window_caught_by_fallback(monkeypatch, _fake_camera):
    """The live bug: printing far outside the window, then straight to
    complete next tick, with last_layer never having fired. Fallback must
    catch it using the last known layer numbers."""
    _run(monkeypatch, _status("printing", 35, 48))  # remaining=13, NOT in window
    state = w.load_state()
    assert "last_layer_fired_job_key" not in state

    _run(monkeypatch, _status("complete", 48, 48, is_active=False))
    state = w.load_state()
    assert state["last_layer_fired_job_key"] == "test.gcode|48"
    assert state["last_layer_caught_post_complete"] is True
    assert len(_fake_camera) == 1
    assert _fake_camera[0][1] == "last_layer_post_complete"


def test_fallback_does_not_double_fire_after_live_catch(monkeypatch, _fake_camera):
    """If last_layer already fired live (in-window), the printing->complete
    transition must NOT capture a second photo for the same job."""
    _run(monkeypatch, _status("printing", 44, 48))
    _run(monkeypatch, _status("complete", 48, 48, is_active=False))
    assert len(_fake_camera) == 1  # only the live catch, no fallback duplicate


def test_fallback_skipped_when_moonraker_drops_layer_info_on_complete(monkeypatch, _fake_camera):
    """Some Moonraker states stop reporting current_layer once terminal — the
    fallback must still use the PREVIOUS tick's last known layer numbers."""
    _run(monkeypatch, _status("printing", 35, 48))
    complete_status = _status("complete", None, None, is_active=False)
    _run(monkeypatch, complete_status)
    state = w.load_state()
    assert state["last_layer_fired_job_key"] == "test.gcode|48"
    assert state["last_layer_fired_layer"] == 35  # fell back to prior tick's layer
    assert state["last_layer_fired_total_layer"] == 48


def test_fallback_does_not_fire_across_different_jobs(monkeypatch, _fake_camera):
    """A brand-new job appearing already 'complete' (never observed as
    'printing' by this watcher) must not spuriously fire for a stale prior
    job_key that never matched."""
    _run(monkeypatch, _status("printing", 10, 100, filename="other.gcode"))
    _run(monkeypatch, _status("complete", 100, 100, filename="other.gcode", is_active=False))
    assert len(_fake_camera) == 1
    state = w.load_state()
    assert state["last_layer_fired_job_key"] == "other.gcode|100"


def test_fallback_does_not_fire_for_different_job_appearing_complete(monkeypatch, _fake_camera):
    """Review finding (2026-07-05): the fallback must fire only for the
    SAME job. If job A was printing and next tick a DIFFERENT job B shows
    complete (A finished + B auto-started+finished between two 1-min ticks —
    realistic for multi-plate kits), capturing A's last-layer would photograph
    B's bed and mislabel it. Guard on filename must reject the cross-job case."""
    _run(monkeypatch, _status("printing", 35, 48, filename="plateA.gcode"))  # A never hit window
    _run(monkeypatch, _status("complete", 20, 60, filename="plateB.gcode", is_active=False))  # different job
    assert len(_fake_camera) == 0  # must NOT fire for A against B's bed
    state = w.load_state()
    assert state.get("last_layer_fired_job_key") != "plateA.gcode|48"
