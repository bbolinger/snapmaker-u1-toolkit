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


@pytest.fixture(autouse=True)
def _estimate(monkeypatch):
    """Slicer estimate served by the fake Moonraker metadata. None by default
    (no network); tests set est["seconds"] to model a sliced file."""
    est = {"seconds": None, "fetches": 0}

    def _fetch(filename):
        est["fetches"] += 1
        return est["seconds"]

    monkeypatch.setattr(w, "fetch_estimated_time", _fetch)
    return est


def _status(print_state, current_layer, total_layer, filename="test.gcode",
            is_active=True, is_paused=False, progress=0.5, print_duration=None):
    return {
        "print_stats": {"filename": filename, "state": print_state,
                        "print_duration": print_duration,
                        "info": {"current_layer": current_layer, "total_layer": total_layer}},
        "virtual_sdcard": {"is_active": is_active, "progress": progress},
        "display_status": {"progress": progress},
        "pause_resume": {"is_paused": is_paused},
    }


def _run(monkeypatch, status):
    monkeypatch.setattr(w, "query_status", lambda: status)
    return w.main()


def test_live_in_window_catch_still_fires_normally(monkeypatch, _fake_camera, _estimate):
    """Regression: the in-progress last-layer catch while still 'printing'
    must keep working when the estimate says the print is nearly done."""
    _estimate["seconds"] = 3000
    _run(monkeypatch, _status("printing", 44, 48, print_duration=2800))  # 200s left
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


def test_fallback_does_not_double_fire_after_live_catch(monkeypatch, _fake_camera, _estimate):
    """If last_layer already fired live (in-window), the printing->complete
    transition must NOT capture a second photo for the same job."""
    _estimate["seconds"] = 3000
    _run(monkeypatch, _status("printing", 44, 48, print_duration=2800))
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


def test_slow_layers_wait_for_time_not_layer_count(monkeypatch, _fake_camera, _estimate):
    """Live bug 2026-09-27: 20-layer pumpkin plates with ~10-minute layers
    were announced as done at layer 14 of 20, 65-101 minutes early. Six
    layers from the end must not fire while the estimate says an hour is left."""
    _estimate["seconds"] = 12960  # 3h36m
    _run(monkeypatch, _status("printing", 14, 20, filename="pumpkin.gcode", print_duration=7000))
    _run(monkeypatch, _status("printing", 19, 20, filename="pumpkin.gcode", print_duration=12000))
    assert _fake_camera == []

    _run(monkeypatch, _status("printing", 20, 20, filename="pumpkin.gcode", print_duration=12700))
    assert [c[1] for c in _fake_camera] == ["last_layer"]
    assert w.load_state()["last_layer_fired_layer"] == 20


def test_estimate_fetched_once_per_job(monkeypatch, _estimate):
    _estimate["seconds"] = 12960
    for layer, elapsed in ((15, 8000), (16, 9000), (17, 10000)):
        _run(monkeypatch, _status("printing", layer, 20, filename="pumpkin.gcode", print_duration=elapsed))
    assert _estimate["fetches"] == 1


def test_short_estimate_cannot_fire_early_on_tall_print(monkeypatch, _fake_camera, _estimate):
    """A print running far slower than sliced must still not be announced
    more than LAST_LAYER_WINDOW layers from the end."""
    _estimate["seconds"] = 3600
    _run(monkeypatch, _status("printing", 880, 900, print_duration=3500))  # 20 layers left
    assert _fake_camera == []
    _run(monkeypatch, _status("printing", 895, 900, print_duration=4000))
    assert len(_fake_camera) == 1


def test_no_estimate_falls_back_to_final_layers(monkeypatch, _fake_camera):
    _run(monkeypatch, _status("printing", 44, 48, print_duration=2800))  # 4 layers left
    assert _fake_camera == []
    _run(monkeypatch, _status("printing", 47, 48, print_duration=3000))
    assert len(_fake_camera) == 1


@pytest.mark.parametrize("final_state", ["cancelled", "error"])
def test_fallback_skips_prints_that_did_not_complete(monkeypatch, _fake_camera, final_state):
    """Live bug 2026-09-22: a print cancelled early (layer 2 of 495) was announced
    as finished by the post-complete fallback."""
    _run(monkeypatch, _status("printing", 10, 495))
    _run(monkeypatch, _status(final_state, 10, 495, is_active=False))
    assert _fake_camera == []
    assert "last_layer_fired_job_key" not in w.load_state()
