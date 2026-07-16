"""Temperature controls in the form (Track C): schema, parse validation, and the
material-dynamic renderer (current temp resolved from the loaded material, only
that material's in-range options shown, temp reset when the head changes)."""
from __future__ import annotations

import sys
from pathlib import Path

import u1_form

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapters" / "telegram"))
import u1_form_telegram as tg  # noqa: E402


def _spec():
    return {
        "heads": [{"tool": "T0", "material": "PLA", "color": "red"},
                  {"tool": "T1", "material": "PETG", "color": "black"}],
        "tool_materials": {"T0": "PLA", "T1": "PETG"},
        "profiles": [{"idx": 1, "label": "0.20 Standard"}],
        "supports": ["supports", "no-supports"], "actions": ["start", "upload-only"],
        "offer_advanced": True,
        "temp_current_by_material": {"PLA": {"nozzle_temp": 220, "bed_temp": 55},
                                     "PETG": {"nozzle_temp": 245, "bed_temp": 70}},
    }


# ---------- schema ----------

def test_schema_offers_temperature_fields_and_ranges():
    sch = u1_form.build_form_schema(_spec())
    temp = [f for f in sch["fields"] if f.get("category") == "temperature"]
    assert [f["id"] for f in temp] == ["nozzle_temp", "bed_temp"]
    assert all(f.get("material_dynamic") and f.get("advanced") for f in temp)
    assert {"key": "temperature", "label": "\U0001f525 Temperature"} in sch["advanced_categories"]
    assert sch["temp_range_by_material"]["PLA"]["nozzle_temp"] == [190, 240]
    assert sch["temp_range_by_material"]["PLA"]["bed_temp"] == [0, 70]


# ---------- parse ----------

def test_parse_in_range_temp_becomes_filament_override():
    res = u1_form.parse_answers_json(
        {"tool": "T0", "profile": 1, "nozzle_temp": "230", "bed_temp": "0"}, _spec())
    assert res["ok"], res["errors"]
    assert res["values"]["filament_overrides"] == {"nozzle_temperature": 230, "hot_plate_temp": 0}


def test_parse_out_of_range_temp_fails_loudly():
    res = u1_form.parse_answers_json(
        {"tool": "T0", "profile": 1, "nozzle_temp": "280"}, _spec())  # PLA max 240
    assert not res["ok"]
    assert any("out of range for PLA" in e for e in res["errors"])


def test_parse_keep_default_is_no_override():
    res = u1_form.parse_answers_json(
        {"tool": "T0", "profile": 1, "nozzle_temp": "default"}, _spec())
    assert res["ok"] and "filament_overrides" not in res["values"]


# ---------- renderer (material-dynamic) ----------

def _pick_head(form, tool_id):
    tfi = tg._field_index(form, "tool")
    oi = next(i for i, o in enumerate(tg._field(form, "tool")["options"])
              if tg._opt_id(o) == tool_id)
    tg.apply_callback(form, f"s:{tfi}:{oi}")


def test_temp_page_resolves_current_and_hides_out_of_range_for_material():
    form = tg.new_form(u1_form.build_form_schema(_spec()))
    _pick_head(form, "T0")  # PLA
    form["current"] = "nozzle_temp"
    kb = tg.render_screen(form)["keyboard"]
    assert "keep profile (220°C)" in kb[0][0]["text"]           # PLA current
    vals = [b["text"] for r in kb for b in r if b["callback_data"].startswith("s:")]
    assert any("190°C" in t for t in vals) and any("240°C" in t for t in vals)
    assert not any("250°C" in t or "270°C" in t for t in vals)  # above PLA range, hidden
    # switch to PETG -> range + current follow
    _pick_head(form, "T1")
    form["current"] = "nozzle_temp"
    kb2 = tg.render_screen(form)["keyboard"]
    assert "keep profile (245°C)" in kb2[0][0]["text"]
    vals2 = [b["text"] for r in kb2 for b in r if b["callback_data"].startswith("s:")]
    assert any("270°C" in t for t in vals2)                     # in PETG range now
    assert not any("190°C" in t for t in vals2)                 # below PETG range, hidden


def test_bed_control_offers_off_for_cold_plate():
    form = tg.new_form(u1_form.build_form_schema(_spec()))
    _pick_head(form, "T0")
    form["current"] = "bed_temp"
    kb = tg.render_screen(form)["keyboard"]
    assert any("off" in b["text"] for r in kb for b in r)       # bed-off / cold plate


def test_changing_head_resets_a_stale_temp_pick():
    form = tg.new_form(u1_form.build_form_schema(_spec()))
    _pick_head(form, "T0")  # PLA
    # pick PLA nozzle 190 (below PETG's 230 min)
    nfi = tg._field_index(form, "nozzle_temp")
    oi = next(i for i, o in enumerate(tg._field(form, "nozzle_temp")["options"])
              if tg._opt_id(o) == "190")
    tg.apply_callback(form, f"s:{nfi}:{oi}")
    assert tg._opt_id(tg._field(form, "nozzle_temp")["options"][form["selections"]["nozzle_temp"]]) == "190"
    # switch head to PETG -> the stale 190 pick resets to default
    _pick_head(form, "T1")
    assert tg._opt_id(tg._field(form, "nozzle_temp")["options"][form["selections"]["nozzle_temp"]]) == "default"
