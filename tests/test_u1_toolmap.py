"""Test the material gate logic in u1_toolmap.summarize().

The material gate is the SAFETY-CRITICAL gate that prevents shipping
prints to the wrong extruder. If a regression bypasses it, real filament
and printer time get wasted.
"""
from __future__ import annotations

import json
import pytest

import u1_toolmap


def _fake_printer_raw(active_tool="extruder1", e1_material="PETG"):
    """Build a minimal Moonraker status response that summarize() can chew on."""
    ptc_idx = ["", "", "", ""]  # filament_type by channel
    ptc_idx[0] = e1_material  # extruder1 = channel 0 in EXTRUDER_CHANNEL? check
    # Actually channels are e0..e3 mapping to extruder/extruder1/extruder2/extruder3
    return {
        "status": {
            "print_stats": {"state": "standby", "filename": "", "info": {}},
            "toolhead": {"extruder": active_tool, "homed_axes": "xyz"},
            "extruder": {"temperature": 35.0, "target": 0.0, "can_extrude": True},
            "extruder1": {"temperature": 240.0, "target": 240.0, "can_extrude": True},
            "extruder2": {"temperature": 36.0, "target": 0.0, "can_extrude": True},
            "extruder3": {"temperature": 34.0, "target": 0.0, "can_extrude": True},
            "heater_bed": {"temperature": 80.0, "target": 80.0},
            "virtual_sdcard": {"file_position": 0, "file_size": 0, "is_active": False},
            "display_status": {"progress": 0.0, "message": None},
            "pause_resume": {"is_paused": False},
            "filament_detect": {"info": [{}, {}, {}, {}], "state": ["yes", "yes", "no", "no"]},
            "print_task_config": {"filament_type": [e1_material, "", "", ""]},
        }
    }


def _material_map(tool="extruder1", material="PETG", confirmed=True):
    return {
        "schema": "snapmaker-u1-tool-material-map/v1",
        "tools": {
            "extruder": {"material": "unknown", "confirmed_by": None},
            "extruder1": {"material": material, "confirmed_by": "op" if confirmed else None},
            "extruder2": {"material": "unknown", "confirmed_by": None},
            "extruder3": {"material": "unknown", "confirmed_by": None},
        },
    }


def test_material_match_passes_gate():
    """Requesting PETG against extruder1 declared PETG → no gate blocks it."""
    raw = _fake_printer_raw()
    mmap = _material_map(material="PETG")
    out = u1_toolmap.summarize(raw, mmap, requested_material="PETG", intended_tool="extruder1")
    gates = out.get("gates", [])
    blocking = [g for g in gates if "requested material" in g]
    assert not blocking, f"unexpected gate block: {blocking}"


def test_material_mismatch_blocks():
    """Requesting PLA against extruder1 declared PETG → gate must block."""
    raw = _fake_printer_raw(e1_material="PETG")
    mmap = _material_map(material="PETG")
    out = u1_toolmap.summarize(raw, mmap, requested_material="PLA", intended_tool="extruder1")
    gates = out.get("gates", [])
    blocking = [g for g in gates if "does not match" in g]
    assert blocking, f"PLA-vs-PETG mismatch should have been blocked. Gates: {gates}"


def test_unknown_material_blocks():
    """Requesting PETG against undeclared extruder3 → fail-closed."""
    raw = _fake_printer_raw()
    mmap = _material_map(material="PETG")  # only extruder1 is declared
    out = u1_toolmap.summarize(raw, mmap, requested_material="PETG", intended_tool="extruder3")
    gates = out.get("gates", [])
    blocking = [g for g in gates if "cannot be verified" in g or "unknown" in g]
    assert blocking, f"Unknown tool material should be fail-closed. Gates: {gates}"


def test_no_request_means_no_gate():
    """If caller doesn't ask for a material, no material gate fires (read-only mode)."""
    raw = _fake_printer_raw()
    mmap = _material_map(material="PETG")
    out = u1_toolmap.summarize(raw, mmap, requested_material=None, intended_tool=None)
    gates = out.get("gates", [])
    blocking = [g for g in gates if "requested material" in g]
    assert not blocking


def test_case_insensitive_material_match():
    """petg vs PETG should still match — operator typing convenience."""
    raw = _fake_printer_raw(e1_material="petg")
    mmap = _material_map(material="petg")
    out = u1_toolmap.summarize(raw, mmap, requested_material="PETG", intended_tool="extruder1")
    gates = out.get("gates", [])
    blocking = [g for g in gates if "requested material" in g]
    assert not blocking, f"Case-mismatch should not block: {blocking}"


def test_warns_on_unknown_active_toolhead():
    """If toolhead.extruder is some unexpected name, summarize warns (doesn't crash)."""
    raw = _fake_printer_raw(active_tool="extruder99")
    mmap = _material_map()
    out = u1_toolmap.summarize(raw, mmap)
    assert any("not one of expected" in w for w in out.get("warnings", []))


def test_load_material_map_with_missing_file(tmp_path):
    """load_material_map returns a sensible default when file is absent."""
    missing = tmp_path / "no-such.json"
    mmap = u1_toolmap.load_material_map(missing)
    assert "tools" in mmap
    assert all(t["material"] == "unknown" for t in mmap["tools"].values())


def test_load_material_map_with_corrupt_file_fails_closed(tmp_path, capsys):
    """Corrupt material map must NOT crash the toolmap script — return the
    unknown-everywhere default so every material gate denies (fail-closed)."""
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    mmap = u1_toolmap.load_material_map(bad)
    assert "tools" in mmap
    assert all(t["material"] == "unknown" for t in mmap["tools"].values()), \
        "fail-closed default must mark every tool as unknown material"
    # Operator-visible warning on stderr so the corruption isn't silent
    err = capsys.readouterr().err
    assert "unreadable" in err and str(bad) in err


def test_load_material_map_with_non_object_root_fails_closed(tmp_path, capsys):
    """JSON that parses but isn't an object (e.g. a list) also fails closed."""
    bad = tmp_path / "list.json"
    bad.write_text("[]")
    mmap = u1_toolmap.load_material_map(bad)
    assert all(t["material"] == "unknown" for t in mmap["tools"].values())
