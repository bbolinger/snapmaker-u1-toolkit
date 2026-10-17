"""The slice-time floor under a process profile that carries no speed tuning.

Every profiles/from-printer JSON written before extractor version 2 had no
speed, acceleration or jerk keys, so Orca sliced it at its generic defaults
(60 mm/s walls, 500 mm/s^2, 120 mm/s travel) instead of the U1's stock
tuning. A 4.5 h plate took 8.8 h (live 2026-10-17). apply_supports_override
is the single materialization point every slice goes through, so the
backfill lives there: speed-less profile -> stock U1 values for its layer
height; profile with speeds -> untouched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import u1_slice_workflow as sw  # noqa: E402


def _vendor(tmp_path: Path) -> Path:
    """A minimal Snapmaker vendor tree: leaf per layer height -> U1 common
    -> generic common, like the real AppImage ships."""
    proc = tmp_path / "vendor" / "process"
    proc.mkdir(parents=True)
    (proc / "fdm_process_common.json").write_text(json.dumps({
        "name": "fdm_process_common", "outer_wall_speed": "70",
        "travel_speed": "120", "default_acceleration": "1100",
        "default_jerk": "0", "wall_loops": "2",
    }))
    (proc / "fdm_process_U1_common.json").write_text(json.dumps({
        "name": "fdm_process_U1_common", "inherits": "fdm_process_common",
        "inner_wall_speed": "150", "sparse_infill_speed": "250",
        "travel_speed": "500", "outer_wall_acceleration": "5000",
    }))
    (proc / "fdm_process_U1_0.16.json").write_text(json.dumps({
        "name": "fdm_process_U1_0.16", "inherits": "fdm_process_U1_common",
        "outer_wall_speed": "200", "inner_wall_speed": "300",
        "sparse_infill_speed": "330",
    }))
    return proc


def test_speedless_profile_gets_the_stock_layer_for_its_height(tmp_path):
    proc = _vendor(tmp_path)
    data = {"layer_height": "0.16", "wall_loops": "4", "sparse_infill_density": "15%"}
    filled = sw.backfill_speed_settings(data, vendor_dirs=[proc])
    assert set(filled) >= {"outer_wall_speed", "inner_wall_speed", "sparse_infill_speed",
                           "travel_speed", "default_acceleration",
                           "outer_wall_acceleration", "default_jerk"}
    # leaf wins over U1 common wins over generic common
    assert data["outer_wall_speed"] == "200"
    assert data["inner_wall_speed"] == "300"
    assert data["sparse_infill_speed"] == "330"
    assert data["travel_speed"] == "500"
    assert data["outer_wall_acceleration"] == "5000"
    assert data["default_acceleration"] == "1100"
    # only speed keys come across: the stock layer's wall count must not
    assert data["wall_loops"] == "4"
    assert data[sw.SPEED_BACKFILL_KEY] == {"from": "fdm_process_U1_0.16", "keys": filled}
    assert any("backfilled from Snapmaker stock fdm_process_U1_0.16" in n
               for n in data["_u1_workflow_notes"])


def test_unknown_layer_height_falls_back_to_u1_common(tmp_path):
    proc = _vendor(tmp_path)
    data = {"layer_height": "0.3"}
    filled = sw.backfill_speed_settings(data, vendor_dirs=[proc])
    assert filled
    assert data[sw.SPEED_BACKFILL_KEY]["from"] == "fdm_process_U1_common"
    assert data["inner_wall_speed"] == "150"
    assert data["outer_wall_speed"] == "70"       # from the generic parent


def test_profile_with_speeds_is_left_alone(tmp_path):
    proc = _vendor(tmp_path)
    data = {"layer_height": "0.16", "outer_wall_speed": "60"}
    assert sw.backfill_speed_settings(data, vendor_dirs=[proc]) == []
    assert data == {"layer_height": "0.16", "outer_wall_speed": "60"}
    data = {"layer_height": "0.16", "default_acceleration": "4000"}
    assert sw.backfill_speed_settings(data, vendor_dirs=[proc]) == []
    assert "inner_wall_speed" not in data


def test_no_vendor_tree_means_no_change(tmp_path):
    data = {"layer_height": "0.16"}
    assert sw.backfill_speed_settings(data, vendor_dirs=[tmp_path / "nowhere"]) == []
    assert data == {"layer_height": "0.16"}


def test_apply_supports_override_backfills_a_speedless_preset(tmp_path, monkeypatch):
    """The real entry point: a from-printer style preset goes in, the temp
    profile Orca slices carries the stock speeds and says why."""
    proc = _vendor(tmp_path)
    monkeypatch.setattr(sw, "_vendor_profile_dirs", lambda orca_bin=None: [proc])
    preset = tmp_path / "old_from_printer_process.json"
    preset.write_text(json.dumps({
        "type": "process", "name": "0.16 High Quality @Snapmaker U1 (0.4 nozzle)",
        "inherits": "", "layer_height": "0.16", "wall_loops": "4",
    }))
    temp = sw.apply_supports_override(preset, False, tmp_path / "out")
    data = json.loads(temp.read_text())
    assert data["enable_support"] == "0"
    assert data["outer_wall_speed"] == "200"
    assert data["default_acceleration"] == "1100"
    assert data["travel_speed"] == "500"
    note = sw.speed_backfill_note(temp)
    assert note and "fdm_process_U1_0.16" in note and "200 mm/s" in note


def test_apply_supports_override_keeps_a_tuned_preset(tmp_path, monkeypatch):
    proc = _vendor(tmp_path)
    monkeypatch.setattr(sw, "_vendor_profile_dirs", lambda orca_bin=None: [proc])
    preset = tmp_path / "tuned_process.json"
    preset.write_text(json.dumps({
        "type": "process", "name": "tuned", "inherits": "",
        "layer_height": "0.16", "outer_wall_speed": "60",
        "default_acceleration": "4000",
    }))
    temp = sw.apply_supports_override(preset, True, tmp_path / "out")
    data = json.loads(temp.read_text())
    assert data["outer_wall_speed"] == "60"
    assert data["default_acceleration"] == "4000"
    assert sw.SPEED_BACKFILL_KEY not in data
    assert sw.speed_backfill_note(temp) is None


def test_speed_backfill_note_is_quiet_on_garbage():
    assert sw.speed_backfill_note(None) is None
    assert sw.speed_backfill_note("/nonexistent/path.json") is None
