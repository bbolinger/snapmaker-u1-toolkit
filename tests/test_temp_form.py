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
    assert [f["id"] for f in temp] == ["nozzle_temp", "nozzle_temp_first", "bed_temp"]
    assert all(f.get("material_dynamic") and f.get("advanced") for f in temp)
    assert {"key": "temperature", "label": "\U0001f525 Temperature"} in sch["advanced_categories"]
    assert sch["temp_range_by_material"]["PLA"]["nozzle_temp"] == [190, 240]
    assert sch["temp_range_by_material"]["PLA"]["nozzle_temp_first"] == [190, 240]
    assert sch["temp_range_by_material"]["PLA"]["bed_temp"] == [0, 70]


# ---------- parse ----------

def test_parse_in_range_temp_becomes_filament_override():
    res = u1_form.parse_answers_json(
        {"tool": "T0", "profile": 1, "nozzle_temp": "230", "bed_temp": "0"}, _spec())
    assert res["ok"], res["errors"]
    assert res["values"]["filament_overrides"] == {"nozzle_temperature": 230, "hot_plate_temp": 0}


def test_parse_first_layer_nozzle_maps_to_initial_layer_key():
    res = u1_form.parse_answers_json(
        {"tool": "T0", "profile": 1, "nozzle_temp": "230", "nozzle_temp_first": "235"}, _spec())
    assert res["ok"], res["errors"]
    assert res["values"]["filament_overrides"] == {
        "nozzle_temperature": 230, "nozzle_temperature_initial_layer": 235}


def test_parse_first_layer_nozzle_out_of_range_fails_for_material():
    res = u1_form.parse_answers_json(
        {"tool": "T0", "profile": 1, "nozzle_temp_first": "280"}, _spec())  # PLA max 240
    assert not res["ok"]
    assert any("out of range for PLA" in e for e in res["errors"])


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


def test_temp_stepper_dials_exact_value_clamped_to_material():
    form = tg.new_form(u1_form.build_form_schema(_spec()))
    _pick_head(form, "T0")  # PLA: current 220, range 190-240
    form["current"] = "nozzle_temp"
    nfi = tg._field_index(form, "nozzle_temp")
    kb = tg.render_screen(form)["keyboard"]
    assert "keep profile (220" in kb[0][0]["text"]            # header shows current
    # the stepper is one row of exactly four steps: -5, -1, +1, +5
    assert [b["callback_data"] for b in kb[1]] == [
        f"T:{nfi}:-5", f"T:{nfi}:-1", f"T:{nfi}:1", f"T:{nfi}:5"]
    # dial +5 +5 +1 -> 231 (an EXACT value the step buttons never offered)
    tg.apply_callback(form, f"T:{nfi}:5")
    tg.apply_callback(form, f"T:{nfi}:5")
    tg.apply_callback(form, f"T:{nfi}:1")
    assert form["steps"]["nozzle_temp"] == 231
    assert "231°C" in tg.render_screen(form)["keyboard"][0][0]["text"]
    assert tg.answer_json(form)["nozzle_temp"] == "231"
    # can't dial past the PLA max even with many taps
    for _ in range(20):
        tg.apply_callback(form, f"T:{nfi}:5")
    assert form["steps"]["nozzle_temp"] == 240


def test_temp_stepper_keep_resets_to_no_override():
    form = tg.new_form(u1_form.build_form_schema(_spec()))
    _pick_head(form, "T0")
    nfi = tg._field_index(form, "nozzle_temp")
    tg.apply_callback(form, f"T:{nfi}:5")               # 220 -> 225
    assert tg.answer_json(form).get("nozzle_temp") == "225"
    tg.apply_callback(form, f"T:{nfi}:k")               # tap header -> keep
    assert "nozzle_temp" not in tg.answer_json(form)


def test_bed_stepper_can_reach_off_for_cold_plate():
    form = tg.new_form(u1_form.build_form_schema(_spec()))
    _pick_head(form, "T0")  # PLA bed 55, range 0-70
    bfi = tg._field_index(form, "bed_temp")
    for _ in range(20):
        tg.apply_callback(form, f"T:{bfi}:-5")
    assert form["steps"]["bed_temp"] == 0                # bed-off / cold plate reachable
    assert tg.answer_json(form)["bed_temp"] == "0"


def test_changing_head_clears_the_temp_stepper():
    form = tg.new_form(u1_form.build_form_schema(_spec()))
    _pick_head(form, "T0")  # PLA
    nfi = tg._field_index(form, "nozzle_temp")
    tg.apply_callback(form, f"T:{nfi}:5")
    assert form.get("steps", {}).get("nozzle_temp") is not None
    _pick_head(form, "T1")  # PETG -> temp stepper cleared (its range changed)
    assert not form.get("steps")
