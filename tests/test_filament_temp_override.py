"""Bed/nozzle temperature overrides (Track C).

The override patches the FILAMENT profile (not the process profile), clamps to
the material's sourced envelope, writes every sibling temp key as a single-
element list, and lands in the sliced gcode. Unit tests cover the patch/clamp/
cold-plate logic; the e2e proves Orca actually stamps the overridden temps.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

import u1_slice_workflow as wf
from u1_orient import DEFAULT_ORCA, write_binary_stl


def _write_filament(tmp_path: Path, **over) -> Path:
    base = {
        "nozzle_temperature": ["245"], "nozzle_temperature_initial_layer": ["250"],
        "hot_plate_temp": ["70"], "hot_plate_temp_initial_layer": ["70"],
        "textured_plate_temp": ["0"], "textured_plate_temp_initial_layer": ["0"],
        "eng_plate_temp": ["0"], "cool_plate_temp": ["0"],
        "filament_type": ["PETG"],
    }
    base.update(over)
    p = tmp_path / "fil.json"
    p.write_text(json.dumps(base))
    return p


# ---------- unit: patch / clamp / cold-plate / no-op ----------

def test_override_patches_both_layers_and_all_bed_plates(tmp_path):
    src = _write_filament(tmp_path)
    out = wf.apply_filament_overrides(
        src, {"nozzle_temperature": 235, "hot_plate_temp": 60}, tmp_path, material="PETG")
    d = json.loads(out.read_text())
    assert d["nozzle_temperature"] == ["235"]
    assert d["nozzle_temperature_initial_layer"] == ["235"]
    for k in ("hot_plate_temp", "hot_plate_temp_initial_layer", "textured_plate_temp",
              "textured_plate_temp_initial_layer", "eng_plate_temp", "cool_plate_temp"):
        assert d[k] == ["60"], k
    assert out != src


def test_first_layer_nozzle_overrides_only_the_initial_layer(tmp_path):
    src = _write_filament(tmp_path)   # profile nozzle 245, initial 250
    # main nozzle sets both siblings, then the separate first-layer value wins
    # on the initial-layer key
    out = wf.apply_filament_overrides(
        src, {"nozzle_temperature": 235, "nozzle_temperature_initial_layer": 245},
        tmp_path, material="PETG")
    d = json.loads(out.read_text())
    assert d["nozzle_temperature"] == ["235"]                  # main layer
    assert d["nozzle_temperature_initial_layer"] == ["245"]    # first layer, independent


def test_first_layer_nozzle_alone_leaves_main_at_profile(tmp_path):
    src = _write_filament(tmp_path)   # profile nozzle 245, initial 250
    out = wf.apply_filament_overrides(
        src, {"nozzle_temperature_initial_layer": 240}, tmp_path, material="PETG")
    d = json.loads(out.read_text())
    assert d["nozzle_temperature"] == ["245"]                  # untouched profile value
    assert d["nozzle_temperature_initial_layer"] == ["240"]    # only the first layer moved


def test_first_layer_nozzle_clamps_to_material_envelope(tmp_path):
    src = _write_filament(tmp_path)
    out = wf.apply_filament_overrides(
        src, {"nozzle_temperature_initial_layer": 999}, tmp_path, material="PETG")
    assert json.loads(out.read_text())["nozzle_temperature_initial_layer"] == ["270"]  # PETG max


def test_override_clamps_out_of_range_to_material_envelope(tmp_path):
    src = _write_filament(tmp_path)
    # PETG nozzle max is 270, bed max 90 (u1_temps)
    hot = wf.apply_filament_overrides(src, {"nozzle_temperature": 999, "hot_plate_temp": 999},
                                      tmp_path, material="PETG")
    d = json.loads(hot.read_text())
    assert d["nozzle_temperature"] == ["270"]
    assert d["hot_plate_temp"] == ["90"]
    # below-range nozzle clamps up to the material min
    low = wf.apply_filament_overrides(src, {"nozzle_temperature": 100}, tmp_path, material="PETG")
    assert json.loads(low.read_text())["nozzle_temperature"] == ["230"]


def test_bed_off_cold_plate_is_honored(tmp_path):
    src = _write_filament(tmp_path)
    out = wf.apply_filament_overrides(src, {"hot_plate_temp": 0}, tmp_path, material="PETG")
    d = json.loads(out.read_text())
    assert d["hot_plate_temp"] == ["0"]      # bed off / cool plate never clamped up
    assert d["cool_plate_temp"] == ["0"]


def test_noop_returns_original(tmp_path):
    src = _write_filament(tmp_path)
    assert wf.apply_filament_overrides(src, {}, tmp_path, material="PETG") == src
    assert wf.apply_filament_overrides(src, {"bogus": 5}, tmp_path, material="PETG") == src
    assert wf.apply_filament_overrides(
        src, {"nozzle_temperature": "default"}, tmp_path, material="PETG") == src


# ---------- e2e: the override survives the real slicer ----------

_PROFILE = "0_20_standard_snapmaker_u1_0_4_nozzle"


def _cube_tris(s: float) -> np.ndarray:
    v = np.array([[0, 0, 0], [s, 0, 0], [s, s, 0], [0, s, 0],
                  [0, 0, s], [s, 0, s], [s, s, s], [0, s, s]], dtype=np.float32)
    faces = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return np.array([[v[a], v[b], v[c]] for a, b, c in faces], dtype=np.float32)


def _gcode_config(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("; ") and " = " in line:
            key, _, value = line[2:].partition(" = ")
            out[key.strip()] = value.strip()
    return out


@pytest.mark.skipif(not DEFAULT_ORCA.exists(),
                    reason="extracted Orca binary not present in this environment")
def test_temp_override_lands_in_sliced_gcode(tmp_path):
    try:
        wf.profile_path(_PROFILE)
        wf.filament_path("PETG", nozzle="0.4")
    except RuntimeError as exc:
        pytest.skip(f"runtime profiles not fetched here ({exc})")
    out_dir = tmp_path / "slice"
    out_dir.mkdir()
    stl = tmp_path / "cube.stl"
    write_binary_stl(stl, _cube_tris(15.0), name="cube")
    res = wf.real_orca_slice(
        stl, out_dir / "cube.gcode", tool="T0", material="PETG", profile=_PROFILE,
        filament_overrides={"nozzle_temperature": 235, "hot_plate_temp": 60})
    cfg = _gcode_config(Path(res["gcode"]).read_text(errors="replace"))
    assert cfg, "no config block in sliced gcode"
    assert cfg.get("nozzle_temperature") == "235", cfg.get("nozzle_temperature")
    assert cfg.get("hot_plate_temp") == "60", cfg.get("hot_plate_temp")


@pytest.mark.skipif(not DEFAULT_ORCA.exists(),
                    reason="extracted Orca binary not present in this environment")
def test_temp_override_lands_via_kit_arrange_slice(tmp_path):
    """The KIT path slices through u1_arrange.arrange_slice, not real_orca_slice,
    so prove the override lands there too (this is the path the operator's kit
    actually takes)."""
    import u1_arrange
    try:
        wf.profile_path(_PROFILE)
        wf.filament_path("PETG", nozzle="0.4")
    except RuntimeError as exc:
        pytest.skip(f"runtime profiles not fetched here ({exc})")
    out_dir = tmp_path / "slice"
    out_dir.mkdir()
    stl = tmp_path / "cube.stl"
    write_binary_stl(stl, _cube_tris(15.0), name="cube")
    res = u1_arrange.arrange_slice(
        [stl], out_dir, tool="T0", material="PETG", profile=_PROFILE,
        filament_overrides={"nozzle_temperature": 235, "hot_plate_temp": 60})
    plate = Path(res["plates"][0]["gcode_path"])
    cfg = _gcode_config(plate.read_text(errors="replace"))
    assert cfg.get("nozzle_temperature") == "235", cfg.get("nozzle_temperature")
    assert cfg.get("hot_plate_temp") == "60", cfg.get("hot_plate_temp")
