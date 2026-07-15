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


def test_idle_loaded_tool_not_blocked_by_motion_sensor():
    """U1 (verified 2026-07-15): a LOADED tool (filament_exist True) whose motion
    sensor reads False because it's idle/not feeding must NOT be gated. The
    motion sensor only fires while filament is actively moving; at pre-start
    every tool is idle, so gating on it refused every real print."""
    raw = _fake_printer_raw(e1_material="PETG")
    st = raw["status"]
    # extruder1 is channel 1 (EXTRUDER_CHANNEL): load ch1 + set its material.
    st["print_task_config"]["filament_exist"] = [False, True, False, False]
    st["print_task_config"]["filament_type"] = ["", "PETG", "", ""]
    st["filament_motion_sensor e1_filament"] = {"filament_detected": False, "enabled": True}
    out = u1_toolmap.summarize(raw, _material_map(material="PETG"),
                               requested_material="PETG", intended_tool="extruder1")
    assert not [g for g in out.get("gates", []) if "not loaded" in g], out.get("gates")


def test_empty_tool_still_blocked_by_filament_exist():
    """Safety intact: a genuinely empty tool (filament_exist False) MUST block."""
    raw = _fake_printer_raw(e1_material="PETG")
    raw["status"]["print_task_config"]["filament_exist"] = [False, False, False, False]
    out = u1_toolmap.summarize(raw, _material_map(material="PETG"),
                               requested_material="PETG", intended_tool="extruder1")
    assert [g for g in out.get("gates", []) if "not loaded" in g], out.get("gates")


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


# --------------------------------------------------------------------------- #
# load_head_options — merged head/material screen source (v2.2.1)
# --------------------------------------------------------------------------- #

def _write_toolmap(data_dir, tools):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "latest_toolmap.json").write_text(json.dumps({"tools": tools}))


def test_load_head_options_reads_printer_reported(tmp_path):
    _write_toolmap(tmp_path, {
        "extruder":  {"printer_reported": {"material": "PETG", "color_rgba": "FFFFFFFF", "exists": True, "vendor": "Generic"}},
        "extruder1": {"printer_reported": {"material": "PETG", "color_rgba": "000000FF", "exists": True}},
        "extruder2": {"printer_reported": {"material": "PLA",  "color_rgba": "F78E0EFF", "exists": True, "vendor": "Polymaker"}},
        "extruder3": {"printer_reported": {"material": "unknown", "exists": True}},
    })
    heads = u1_toolmap.load_head_options(data_dir=tmp_path)
    tools = [h["tool"] for h in heads]
    assert tools == ["T0", "T1", "T2"]         # unknown head T3 not offered
    assert heads[0]["material"] == "PETG" and heads[0]["color"] == "white"
    assert heads[1]["color"] == "black"
    assert heads[2]["material"] == "PLA" and heads[2]["color"] == "orange"


def test_load_head_options_skips_empty_heads(tmp_path):
    _write_toolmap(tmp_path, {
        "extruder":  {"printer_reported": {"material": "PLA", "color_rgba": "FFFFFFFF", "exists": True}},
        "extruder1": {"printer_reported": {"material": "PETG", "exists": False}},   # not loaded
        "extruder2": {"printer_reported": {"material": "", "exists": True}},        # unknown
    })
    heads = u1_toolmap.load_head_options(data_dir=tmp_path)
    assert [h["tool"] for h in heads] == ["T0"]


def test_load_head_options_missing_toolmap_returns_empty(tmp_path):
    # No file → [] so the form falls back to generic tool+material fields.
    assert u1_toolmap.load_head_options(data_dir=tmp_path) == []


# --------------------------------------------------------------------------- #
# refresh_toolmap — the form-build path pulls the printer's LIVE filament before
# reading the tool map, so a spool swapped BETWEEN jobs isn't served stale. Live
# bug 2026-07-14: the head screen showed the previous run's colour ("orange")
# after a swap the printer already reported, because the cache was only rewritten
# by the gate / upload CLI, never at form-build time. Could mislead an operator
# into printing the wrong material — safety-relevant, hence the regression
# coverage. load_head_options itself stays a pure cache read (tested above); the
# refresh is invoked by _build_form_spec (see test_quantity).
# --------------------------------------------------------------------------- #

def test_refresh_toolmap_writes_live_snapshot_and_is_guarded(tmp_path, monkeypatch):
    """refresh_toolmap queries the printer and rewrites the cache; on any failure
    it returns False without raising, so load_head_options can fall back."""
    monkeypatch.setattr(u1_toolmap, "query_u1", lambda *a, **k: _fake_printer_raw())
    monkeypatch.setattr(u1_toolmap, "get_u1_host", lambda: "x")
    monkeypatch.setattr(u1_toolmap, "get_u1_port", lambda: 1)
    monkeypatch.setattr(u1_toolmap, "_default_map_path", lambda: tmp_path / "map.json")
    assert u1_toolmap.refresh_toolmap(data_dir=tmp_path) is True
    written = json.loads((tmp_path / "latest_toolmap.json").read_text())
    assert "tools" in written
    # guarded: a query failure returns False, no exception escapes
    def _boom(*a, **k):
        raise OSError("unreachable")
    monkeypatch.setattr(u1_toolmap, "query_u1", _boom)
    assert u1_toolmap.refresh_toolmap(data_dir=tmp_path) is False


# ── Enforcement-layer tests (2026-07-05) ─────────────────────────────────────
# The tests above verify summarize() DETECTS gates. But the live bug was that
# main() returned exit 0 REGARDLESS of gates, and run_tool_gate() keys purely on
# `returncode == 0` — so a detected mismatch (sliced PETG, loaded PLA) sailed
# through as "safe" and the print started. These test the ENFORCEMENT layer:
# a blocking gate MUST produce a non-zero exit code.

def _run_main(monkeypatch, tmp_path, requested, intended, e1_material="PETG"):
    monkeypatch.setattr(u1_toolmap, "query_u1",
                        lambda *a, **k: _fake_printer_raw(e1_material=e1_material))
    monkeypatch.setattr(u1_toolmap, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(u1_toolmap, "_default_map_path", lambda: tmp_path / "map.json")
    (tmp_path / "map.json").write_text(json.dumps(_material_map(material="PETG")))
    argv = ["u1_toolmap.py", "--host", "x", "--port", "1",
            "--requested-material", requested, "--intended-tool", intended]
    monkeypatch.setattr("sys.argv", argv)
    return u1_toolmap.main()


def test_main_exits_nonzero_when_gate_blocks(monkeypatch, tmp_path):
    """The bug: main() returned 0 even with a blocking gate, so run_tool_gate
    (returncode-keyed) treated a real PLA-vs-PETG mismatch as PASSED."""
    rc = _run_main(monkeypatch, tmp_path, requested="PLA", intended="extruder1",
                   e1_material="PETG")
    assert rc != 0, "a blocking material gate MUST exit non-zero so run_tool_gate refuses"


def test_main_exits_zero_when_material_matches(monkeypatch, tmp_path):
    """Legit prints (requested == loaded) must still pass, or every start breaks."""
    rc = _run_main(monkeypatch, tmp_path, requested="PETG", intended="extruder1",
                   e1_material="PETG")
    assert rc == 0


def test_main_exits_zero_for_plain_probe_no_request(monkeypatch, tmp_path):
    """A read-only probe (no --requested-material) has no gate and must exit 0."""
    monkeypatch.setattr(u1_toolmap, "query_u1", lambda *a, **k: _fake_printer_raw())
    monkeypatch.setattr(u1_toolmap, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(u1_toolmap, "_default_map_path", lambda: tmp_path / "map.json")
    (tmp_path / "map.json").write_text(json.dumps(_material_map()))
    monkeypatch.setattr("sys.argv", ["u1_toolmap.py", "--host", "x", "--port", "1"])
    assert u1_toolmap.main() == 0
