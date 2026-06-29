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


def _schema(n_parts=3, n_profiles=2):
    spec = {
        "parts": [{"id": f"{i:02d}_p{i}", "label": f"p{i}"} for i in range(1, n_parts + 1)],
        "tools": ["T0", "T1", "T2", "T3"],
        "materials": ["PLA", "PETG"],
        "profiles": [{"idx": i + 1, "label": f"profile{i + 1}"} for i in range(n_profiles)],
        "supports": ["supports", "no-supports", "overhangs"],
        "actions": ["start", "upload-only"],
    }
    return u1_form.build_form_schema(spec)


# --------------------------------------------------------------------------- #
# Telegram pure core — step-by-step state machine
# --------------------------------------------------------------------------- #

def _ids_in_keyboard(keyboard):
    return [b["callback_data"] for row in keyboard for b in row]


def test_tg_new_form_starts_at_first_field():
    form = tg.new_form(_schema())
    assert form["current"] == form["schema"]["fields"][0]["id"]  # "parts" (multi)
    assert form["selections"]["parts"] == set()


def test_tg_render_field_screen_shows_field_and_step_hint():
    form = tg.new_form(_schema())
    screen = tg.render_screen(form)
    assert "Parts" in screen["text"]
    assert "Step 1 of" in screen["text"]
    # Cancel always reachable; Done present on multi screen
    cbs = _ids_in_keyboard(screen["keyboard"])
    assert "X" in cbs
    assert "n:0" in cbs    # Done for parts
    assert "a:0" in cbs    # All
    assert "z:0" in cbs    # None


def test_tg_multi_toggle_marks_in_place_and_done_advances():
    form = tg.new_form(_schema(n_parts=3))
    # toggle parts 0 and 2
    assert tg.apply_callback(form, "t:0:0")["kind"] == "rerender"
    assert tg.apply_callback(form, "t:0:2")["kind"] == "rerender"
    kb = tg.render_screen(form)["keyboard"]
    # ✔ on the toggled rows
    assert any(b["text"].startswith("✔") and b["callback_data"] == "t:0:0" for row in kb for b in row)
    assert not any(b["text"].startswith("✔") and b["callback_data"] == "t:0:1" for row in kb for b in row)
    # Done → advance to next field (orient)
    tg.apply_callback(form, "n:0")
    assert form["current"] == "orient"


def test_tg_single_select_advances_immediately():
    form = tg.new_form(_schema())
    # advance past parts
    tg.apply_callback(form, "n:0")
    assert form["current"] == "orient"
    # tap orient: single-select advances to tool
    ev = tg.apply_callback(form, "s:1:0")
    assert ev["kind"] == "rerender"
    assert form["current"] == "tool"


def test_tg_all_and_none_shortcuts():
    form = tg.new_form(_schema(n_parts=4))
    tg.apply_callback(form, "a:0")  # All
    assert form["selections"]["parts"] == {0, 1, 2, 3}
    tg.apply_callback(form, "z:0")  # None
    assert form["selections"]["parts"] == set()


def test_tg_paginates_long_fields():
    # Profile field with 16 options should paginate
    schema = _schema(n_parts=2, n_profiles=16)
    form = tg.new_form(schema)
    # Jump cursor to profile to inspect its screen
    form["current"] = "profile"
    s = tg.render_screen(form)
    cbs = _ids_in_keyboard(s["keyboard"])
    # Page 1/2 navigation present (no Prev on first page; Next present)
    assert any(c == f"p:4:1" for c in cbs)  # next page; profile is field index 4
    # Only PAGE_SIZE option buttons on this page
    opt_cbs = [c for c in cbs if c.startswith("s:4:")]
    assert len(opt_cbs) == tg.PAGE_SIZE
    # Navigate to page 1
    tg.apply_callback(form, "p:4:1")
    s2 = tg.render_screen(form)
    cbs2 = _ids_in_keyboard(s2["keyboard"])
    assert any(c == "p:4:0" for c in cbs2)  # Prev present
    opt_cbs2 = [c for c in cbs2 if c.startswith("s:4:")]
    assert len(opt_cbs2) == 16 - tg.PAGE_SIZE


def test_tg_review_card_after_last_field_lists_all_and_offers_edit():
    form = tg.new_form(_schema(n_parts=2))
    form["selections"]["parts"] = {0, 1}        # all
    form["selections"]["orient"] = 1            # auto
    form["selections"]["tool"] = 0              # T0
    form["selections"]["material"] = 0          # PLA
    form["selections"]["profile"] = 0           # profile1
    form["selections"]["supports"] = 1          # no-supports
    form["selections"]["action"] = 0            # start
    form["current"] = tg.REVIEW_FIELD
    s = tg.render_screen(form)
    assert "Review" in s["text"]
    assert "auto" in s["text"] and "T0" in s["text"]
    cbs = _ids_in_keyboard(s["keyboard"])
    assert "S" in cbs and "X" in cbs            # Submit + Cancel
    assert any(c.startswith("e:") for c in cbs)  # Edit buttons


def test_tg_submit_blocks_when_required_unset_and_jumps_back():
    form = tg.new_form(_schema())
    # advance straight to review without setting tool/material/profile
    form["current"] = tg.REVIEW_FIELD
    ev = tg.apply_callback(form, "S")
    assert ev["kind"] == "rerender"
    assert "warning" in ev and ("Tool" in ev["warning"] or "Material" in ev["warning"])
    # form cursor should land on a required-but-unset field, not stay at review
    assert form["current"] != tg.REVIEW_FIELD


def test_tg_submit_with_all_required_yields_answer_json():
    form = tg.new_form(_schema(n_parts=3))
    form["selections"]["parts"] = {0, 2}
    form["selections"]["orient"] = 1            # auto
    form["selections"]["tool"] = 0              # T0
    form["selections"]["material"] = 0          # PLA
    form["selections"]["profile"] = 1           # profile2
    form["selections"]["supports"] = 1          # no-supports
    form["selections"]["action"] = 0            # start
    form["current"] = tg.REVIEW_FIELD
    ev = tg.apply_callback(form, "S")
    assert ev["kind"] == "submit"
    a = ev["answer"]
    assert a["parts"] == ["01_p1", "03_p3"]
    assert a["orient"] == "auto" and a["tool"] == "T0" and a["material"] == "PLA"
    assert a["profile"] == 2 and a["supports"] == "no-supports" and a["action"] == "start"


def test_tg_edit_from_review_returns_to_that_field():
    form = tg.new_form(_schema())
    form["current"] = tg.REVIEW_FIELD
    tg.apply_callback(form, "e:2")  # tool is field 2
    assert form["current"] == "tool"


def test_tg_cancel_returns_cancel_event():
    form = tg.new_form(_schema())
    ev = tg.apply_callback(form, "X")
    assert ev["kind"] == "cancel"


def test_tg_answer_collapses_all_parts_to_all_keyword():
    form = tg.new_form(_schema(n_parts=2))
    form["selections"]["parts"] = {0, 1}
    assert tg.answer_json(form)["parts"] == "all"


def test_tg_full_walkthrough_feeds_parse_answers_json():
    """End-to-end: build form → tap through → answer_json → core JSON parser."""
    spec = {
        "parts": [{"id": "01_a", "label": "a"}, {"id": "02_b", "label": "b"}],
        "tools": ["T0", "T1"], "materials": ["PLA"],
        "profiles": [{"idx": 1, "label": "Std"}, {"idx": 2, "label": "Opt"}],
        "supports": ["supports", "no-supports", "overhangs"],
        "actions": ["start", "upload-only"],
    }
    schema = u1_form.build_form_schema(spec)
    form = tg.new_form(schema)
    # parts: tap option 0, Done
    tg.apply_callback(form, "t:0:0")
    tg.apply_callback(form, "n:0")
    # orient: pick auto (option 1)
    tg.apply_callback(form, "s:1:1")
    # tool: T0
    tg.apply_callback(form, "s:2:0")
    # material: PLA
    tg.apply_callback(form, "s:3:0")
    # profile: Opt
    tg.apply_callback(form, "s:4:1")
    # supports: no-supports
    tg.apply_callback(form, "s:5:1")
    # action: start
    tg.apply_callback(form, "s:6:0")
    assert form["current"] == tg.REVIEW_FIELD
    ev = tg.apply_callback(form, "S")
    assert ev["kind"] == "submit"
    r = u1_form.parse_answers_json(ev["answer"], spec)
    assert r["ok"], r["errors"]
    assert r["values"]["parts"] == [1]
    assert r["values"]["tool"] == "T0"
    assert r["values"]["profile"]["idx"] == 2


def test_tg_callback_data_under_telegram_64byte_cap():
    # All callback_data must stay under Telegram's 64-byte cap.
    form = tg.new_form(_schema(n_parts=9, n_profiles=16))
    for state_field in form["schema"]["fields"]:
        form["current"] = state_field["id"]
        s = tg.render_screen(form)
        for row in s["keyboard"]:
            for b in row:
                assert len(b["callback_data"].encode("utf-8")) <= 64, b


# --------------------------------------------------------------------------- #
# Discord pure core (unchanged — native multi-select means no screen flow)
# --------------------------------------------------------------------------- #

def test_dc_components_multi_and_single():
    schema = _schema(3)
    rows = dc.build_components(schema)
    menus = {r["components"][0]["custom_id"]: r["components"][0] for r in rows}
    parts = menus["u1form:parts"]
    assert parts["type"] == 3 and parts["max_values"] == 3
    assert {o["value"] for o in parts["options"]} == {"01_p1", "02_p2", "03_p3"}
    tool = menus["u1form:tool"]
    assert tool["max_values"] == 1 and tool["min_values"] == 1


def test_dc_answer_coerces_profile_to_int_and_collapses_all():
    schema = _schema(2)
    ans = dc.answer_json(schema, {
        "parts": ["01_p1", "02_p2"], "tool": ["T0"], "material": ["PLA"], "profile": ["2"],
    })
    assert ans["parts"] == "all"
    assert ans["tool"] == "T0"
    assert ans["profile"] == 2


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


# --------------------------------------------------------------------------- #
# Review fixes
# --------------------------------------------------------------------------- #

def test_tg_malformed_callback_returns_clean_warning_not_exception():
    # Review MED-1: stale callback after redeploy, or out-of-range index, must
    # not propagate an IndexError/ValueError — return a rerender with a warning.
    form = tg.new_form(_schema())
    # field index way out of range
    ev = tg.apply_callback(form, "s:99:0")
    assert ev["kind"] == "rerender" and "warning" in ev
    # option index out of range for an existing field
    ev = tg.apply_callback(form, "s:1:999")
    assert ev["kind"] == "rerender" and "warning" in ev
    # garbage shape
    ev = tg.apply_callback(form, "garbage_data")
    assert ev["kind"] == "rerender" and "warning" in ev
    # non-int field index
    ev = tg.apply_callback(form, "s:not_an_int:0")
    assert ev["kind"] == "rerender" and "warning" in ev


def test_tg_X_and_S_still_work_alongside_defensive_wrap():
    # Make sure the defensive wrap didn't break the happy paths.
    form = tg.new_form(_schema())
    assert tg.apply_callback(form, "X")["kind"] == "cancel"
    form2 = tg.new_form(_schema(n_parts=0))
    # set required fields then submit
    form2["selections"]["tool"] = 0
    form2["selections"]["material"] = 0
    form2["selections"]["profile"] = 0
    form2["current"] = tg.REVIEW_FIELD
    assert tg.apply_callback(form2, "S")["kind"] == "submit"
