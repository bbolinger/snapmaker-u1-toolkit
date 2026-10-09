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

read_mark = w.fetch_slicer_seconds_left  # the real reader; the autouse fake replaces it


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


@pytest.fixture(autouse=True)
def _mark(monkeypatch):
    """Slicer time-left mark (M73 R, seconds) at the printer's file position.
    None by default (no network); tests set mark["seconds"]."""
    mark = {"seconds": None, "positions": []}

    def _fetch(filename, file_position):
        mark["positions"].append(file_position)
        return mark["seconds"]

    monkeypatch.setattr(w, "fetch_slicer_seconds_left", _fetch)
    return mark


def _status(print_state, current_layer, total_layer, filename="test.gcode",
            is_active=True, is_paused=False, progress=0.5, print_duration=None,
            file_position=1000):
    return {
        "print_stats": {"filename": filename, "state": print_state,
                        "print_duration": print_duration,
                        "info": {"current_layer": current_layer, "total_layer": total_layer}},
        "virtual_sdcard": {"is_active": is_active, "progress": progress,
                           "file_position": file_position},
        "display_status": {"progress": progress},
        "pause_resume": {"is_paused": is_paused},
    }


def _run(monkeypatch, status):
    monkeypatch.setattr(w, "query_status", lambda: status)
    return w.main()


def test_live_in_window_catch_still_fires_normally(monkeypatch, _fake_camera, _estimate, _mark):
    """Regression: the in-progress last-layer catch while still 'printing'
    must keep working when the estimate says the print is nearly done."""
    _estimate["seconds"] = 3000
    _mark["seconds"] = 200
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


def test_fallback_does_not_double_fire_after_live_catch(monkeypatch, _fake_camera, _estimate, _mark):
    """If last_layer already fired live (in-window), the printing->complete
    transition must NOT capture a second photo for the same job."""
    _estimate["seconds"] = 3000
    _mark["seconds"] = 200
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


def test_slow_layers_wait_for_time_not_layer_count(monkeypatch, _fake_camera, _estimate, _mark):
    """Live bug 2026-09-27: 20-layer pumpkin plates with ~10-minute layers
    were announced as done at layer 14 of 20, 65-101 minutes early. Six
    layers from the end must not fire while the estimate says an hour is left."""
    _estimate["seconds"] = 12960  # 3h36m
    _mark["seconds"] = 5940
    _run(monkeypatch, _status("printing", 14, 20, filename="pumpkin.gcode", print_duration=7000))
    _mark["seconds"] = 960
    _run(monkeypatch, _status("printing", 19, 20, filename="pumpkin.gcode", print_duration=12000))
    assert _fake_camera == []

    _mark["seconds"] = 240
    _run(monkeypatch, _status("printing", 20, 20, filename="pumpkin.gcode", print_duration=12700))
    assert [c[1] for c in _fake_camera] == ["last_layer"]
    assert w.load_state()["last_layer_fired_layer"] == 20


def test_estimate_fetched_once_per_job(monkeypatch, _estimate):
    _estimate["seconds"] = 12960
    for layer, elapsed in ((15, 8000), (16, 9000), (17, 10000)):
        _run(monkeypatch, _status("printing", layer, 20, filename="pumpkin.gcode", print_duration=elapsed))
    assert _estimate["fetches"] == 1


def test_slow_print_waits_for_its_own_pace(monkeypatch, _fake_camera, _estimate, _mark):
    """A print running slower than sliced: estimate minus elapsed says 100 s
    left, but at this pace 700 s remain, so it must wait."""
    _estimate["seconds"] = 3600
    _mark["seconds"] = 600  # slicer: 3000 s done, 600 s left
    _run(monkeypatch, _status("printing", 880, 900, print_duration=3500))
    assert _fake_camera == []
    _mark["seconds"] = 180
    _run(monkeypatch, _status("printing", 899, 900, print_duration=4150))
    assert len(_fake_camera) == 1


def test_fast_print_fires_before_it_finishes(monkeypatch, _fake_camera, _estimate, _mark):
    """Live bug 2026-10-08: Pumpkin_Brace_Foot was sliced at 21365 s and
    finished in 20924 s. Estimate minus elapsed never got under 300 s, so the
    photo only came after the print. At its real pace 4 minutes are left."""
    _estimate["seconds"] = 21365
    _mark["seconds"] = 240  # slicer: 21125 s done
    _run(monkeypatch, _status("printing", 419, 421, filename="brace.gcode", print_duration=20684))
    assert [c[1] for c in _fake_camera] == ["last_layer"]


def test_tapered_top_fires_more_than_six_layers_out(monkeypatch, _fake_camera, _estimate, _mark):
    """The old six-layer ceiling shut the window on parts whose last layers
    take seconds: 21 layers out can already be the last 4 minutes."""
    _estimate["seconds"] = 5000
    _mark["seconds"] = 240
    _run(monkeypatch, _status("printing", 194, 215, print_duration=4760))
    assert len(_fake_camera) == 1


def test_pace_ignored_while_slicer_counts_much_time_left(monkeypatch, _fake_camera, _estimate, _mark):
    """An odd pace early on (here elapsed far below the slicer's time done)
    must not announce a print the slicer still has 40 minutes left on."""
    _estimate["seconds"] = 10000
    _mark["seconds"] = 2400
    _run(monkeypatch, _status("printing", 300, 421, print_duration=500))
    assert _fake_camera == []


def test_file_not_read_in_the_first_half(monkeypatch, _fake_camera, _estimate, _mark):
    _estimate["seconds"] = 10000
    _mark["seconds"] = 240
    _run(monkeypatch, _status("printing", 100, 421, progress=0.3, print_duration=9700))
    assert _mark["positions"] == [] and _fake_camera == []


def test_no_marks_in_file_falls_back_to_final_layers(monkeypatch, _fake_camera, _estimate):
    _estimate["seconds"] = 3000
    _run(monkeypatch, _status("printing", 44, 48, print_duration=2800))
    assert _fake_camera == []
    _run(monkeypatch, _status("printing", 47, 48, print_duration=2990))
    assert len(_fake_camera) == 1


def _gcode(n_lines=20000):
    body = []
    for i in range(n_lines):
        if i % 1000 == 0:
            body.append(f"M73 P{i // 1000} R{(n_lines - i) // 1000}")
        body.append(f"G1 X{i % 200} Y10 E0.01")
    return ("\n".join(body) + "\n").encode()


def _serve(monkeypatch, data, status=206):
    calls = []

    def _range(path, start, end, timeout=8.0):
        calls.append((path, start, end))
        return data[start:end + 1] if status == 206 else None

    monkeypatch.setattr(w, "http_range", _range)
    return calls


def test_reads_last_mark_before_position(monkeypatch):
    data = _gcode()
    pos = data.index(b"M73 P15 ") + 200  # just past the P15 mark
    calls = _serve(monkeypatch, data)
    assert read_mark("sub dir/part.gcode", pos) == 5 * 60
    assert calls[0][0] == "/server/files/gcodes/sub%20dir/part.gcode"
    assert calls[0][2] == pos - 1


def test_reads_further_back_when_mark_is_far(monkeypatch):
    data = b"M73 P0 R9\n" + b"G1 X1 Y1 E0.01\n" * 40000  # ~600 KB after the mark
    calls = _serve(monkeypatch, data)
    assert read_mark("a.gcode", len(data)) == 9 * 60
    assert len(calls) == 2


def test_partial_line_at_chunk_edge_is_not_misread(monkeypatch):
    data = b"M73 P1 R7\nG1 X1\nM73 P2 R12\nG1 X2\n"
    _serve(monkeypatch, data)
    cut = data.index(b"M73 P2 R12") + len(b"M73 P2 R1")  # mid-line: would read R1
    assert read_mark("a.gcode", cut) == 7 * 60


def test_no_partial_response_means_no_mark(monkeypatch):
    _serve(monkeypatch, _gcode(), status=200)
    assert read_mark("a.gcode", 5000) is None


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


@pytest.mark.parametrize("seconds,text", [(267, "about 4 minutes left"), (90, "about 2 minutes left"),
                                          (70, "about 1 minute left"), (20, "under a minute left")])
def test_time_left_text(seconds, text):
    assert w.time_left_text(seconds) == text


def test_last_layer_message_leads_with_time_left(monkeypatch, capsys, _estimate, _mark):
    """Live 2026-10-09: 'Layer 403 / 421' read as an hour early when it was
    4.5 minutes from the end, so the headline says how long is left."""
    _estimate["seconds"] = 21365
    _mark["seconds"] = 300  # slicer: 21065 s done
    _run(monkeypatch, _status("printing", 403, 421, filename="brace.gcode", print_duration=20340))
    first = capsys.readouterr().out.splitlines()[0]
    assert first == "U1 has about 5 minutes left — last-layer photo captured."


def test_final_layer_fallback_message_unchanged(monkeypatch, capsys):
    _run(monkeypatch, _status("printing", 47, 48, print_duration=3000))
    assert capsys.readouterr().out.splitlines()[0] == "U1 is basically done — last-layer photo captured."
