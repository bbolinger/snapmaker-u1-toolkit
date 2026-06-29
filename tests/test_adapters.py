"""Tests for the reference form-protocol adapters (pure cores, no SDK needed)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADAPTERS = Path(__file__).resolve().parent.parent / "adapters"
sys.path.insert(0, str(_ADAPTERS / "telegram"))
sys.path.insert(0, str(_ADAPTERS / "discord"))

import u1_form  # the core, to build a real schema
import u1_form_telegram as tg
import u1_form_discord as dc


def _schema(n_parts=3):
    spec = {
        "parts": [{"id": f"{i:02d}_p{i}", "label": f"p{i}"} for i in range(1, n_parts + 1)],
        "tools": ["T0", "T1"],
        "materials": ["PLA", "PETG"],
        "profiles": [{"idx": 1, "label": "Std"}, {"idx": 2, "label": "Opt"}],
        "supports": ["supports", "no-supports", "overhangs"],
        "actions": ["start", "upload-only"],
    }
    return u1_form.build_form_schema(spec)


# ---------------- Telegram pure core ----------------

def test_tg_multi_toggle_and_answer():
    schema = _schema(3)
    state = tg.new_state(schema)
    parts_idx = next(i for i, f in enumerate(schema["fields"]) if f["id"] == "parts")
    # toggle parts option 0 and 2
    tg.apply_callback(schema, state, f"t:{parts_idx}:0")
    tg.apply_callback(schema, state, f"t:{parts_idx}:2")
    # keyboard shows checkmarks on the toggled rows
    kb = tg.field_keyboard(schema["fields"][parts_idx], parts_idx, state)
    assert kb[0][0]["text"].startswith("✔")
    assert not kb[1][0]["text"].startswith("✔")
    assert kb[-1][0]["callback_data"] == f"d:{parts_idx}"
    # single selects
    tool_idx = next(i for i, f in enumerate(schema["fields"]) if f["id"] == "tool")
    tg.apply_callback(schema, state, f"s:{tool_idx}:0")  # T0
    mat_idx = next(i for i, f in enumerate(schema["fields"]) if f["id"] == "material")
    tg.apply_callback(schema, state, f"s:{mat_idx}:0")  # PLA
    prof_idx = next(i for i, f in enumerate(schema["fields"]) if f["id"] == "profile")
    tg.apply_callback(schema, state, f"s:{prof_idx}:1")  # idx 2
    ans = tg.answer_json(schema, state)
    assert ans["parts"] == ["01_p1", "03_p3"]
    assert ans["tool"] == "T0" and ans["material"] == "PLA" and ans["profile"] == 2


def test_tg_all_parts_collapses_to_all():
    schema = _schema(2)
    state = tg.new_state(schema)
    pi = next(i for i, f in enumerate(schema["fields"]) if f["id"] == "parts")
    tg.apply_callback(schema, state, f"t:{pi}:0")
    tg.apply_callback(schema, state, f"t:{pi}:1")
    assert tg.answer_json(schema, state)["parts"] == "all"


def test_tg_answer_feeds_parse_answers_json():
    # The adapter's output must validate through the core JSON parser.
    spec = {
        "parts": [{"id": "01_p1", "label": "p1"}, {"id": "02_p2", "label": "p2"}],
        "tools": ["T0", "T1"], "materials": ["PLA"],
        "profiles": [{"idx": 1, "label": "Std"}],
        "supports": ["supports", "no-supports", "overhangs"], "actions": ["start", "upload-only"],
    }
    schema = u1_form.build_form_schema(spec)
    state = tg.new_state(schema)
    pi = next(i for i, f in enumerate(schema["fields"]) if f["id"] == "parts")
    ti = next(i for i, f in enumerate(schema["fields"]) if f["id"] == "tool")
    mi = next(i for i, f in enumerate(schema["fields"]) if f["id"] == "material")
    fi = next(i for i, f in enumerate(schema["fields"]) if f["id"] == "profile")
    tg.apply_callback(schema, state, f"t:{pi}:0")
    tg.apply_callback(schema, state, f"s:{ti}:0")
    tg.apply_callback(schema, state, f"s:{mi}:0")
    tg.apply_callback(schema, state, f"s:{fi}:0")
    r = u1_form.parse_answers_json(tg.answer_json(schema, state), spec)
    assert r["ok"], r["errors"]
    assert r["values"]["parts"] == [1] and r["values"]["tool"] == "T0"


# ---------------- Discord pure core ----------------

def test_dc_components_multi_and_single():
    schema = _schema(3)
    rows = dc.build_components(schema)
    menus = {r["components"][0]["custom_id"]: r["components"][0] for r in rows}
    parts = menus["u1form:parts"]
    assert parts["type"] == 3 and parts["max_values"] == 3  # native multi-select
    assert {o["value"] for o in parts["options"]} == {"01_p1", "02_p2", "03_p3"}
    tool = menus["u1form:tool"]
    assert tool["max_values"] == 1 and tool["min_values"] == 1  # required single


def test_dc_answer_coerces_profile_to_int_and_collapses_all():
    schema = _schema(2)
    ans = dc.answer_json(schema, {
        "parts": ["01_p1", "02_p2"], "tool": ["T0"], "material": ["PLA"], "profile": ["2"],
    })
    assert ans["parts"] == "all"
    assert ans["tool"] == "T0"
    assert ans["profile"] == 2  # coerced back to int id


def test_dc_answer_feeds_parse_answers_json():
    spec = {
        "parts": [{"id": "01_p1", "label": "p1"}, {"id": "02_p2", "label": "p2"}],
        "tools": ["T0"], "materials": ["PLA"], "profiles": [{"idx": 1, "label": "Std"}],
        "supports": ["supports", "no-supports", "overhangs"], "actions": ["start", "upload-only"],
    }
    schema = u1_form.build_form_schema(spec)
    ans = dc.answer_json(schema, {"parts": ["02_p2"], "tool": ["T0"], "material": ["PLA"], "profile": ["1"]})
    r = u1_form.parse_answers_json(ans, spec)
    assert r["ok"], r["errors"]
    assert r["values"]["parts"] == [2]
