"""Advanced settings screen (v2.3): optional per-run slicer overrides.

The Advanced screen is reached only from the form's Review button; the default
path never sees it. Overrides ride the same flatten-and-patch temp-profile
mechanism supports use, and every override shows in the review doc's DIFFERS
sweep before the operator's yes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import u1_form
import u1_slice_workflow as sw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapters" / "telegram"))
import u1_form_telegram as tg  # noqa: E402


def _spec(offer=True):
    return {
        "parts": [{"id": "p1", "label": "a"}, {"id": "p2", "label": "b"}],
        "tools": ["T0", "T1"], "materials": ["PLA", "PETG"],
        "profiles": [{"idx": 1, "label": "0.20 Std"}],
        "supports": ["supports", "no-supports"],
        "actions": ["start", "upload-only"],
        "offer_advanced": offer,
    }


# ---------- override engine ----------

def test_apply_profile_overrides_patches_flat_temp(tmp_path):
    src = tmp_path / "proc.json"
    src.write_text(json.dumps({"name": "p", "sparse_infill_density": "15%",
                               "wall_loops": "2"}))
    out = sw.apply_profile_overrides(
        src, {"sparse_infill_density": "30%", "sparse_infill_pattern": "gyroid",
              "not_a_real_key": "x"}, tmp_path)
    data = json.loads(out.read_text())
    assert data["sparse_infill_density"] == "30%"
    assert data["sparse_infill_pattern"] == "gyroid"
    assert "not_a_real_key" not in data          # unknown keys dropped
    assert data["wall_loops"] == "2"             # untouched values preserved
    assert out != src


def test_apply_profile_overrides_noop_returns_original(tmp_path):
    src = tmp_path / "proc.json"
    src.write_text(json.dumps({"name": "p"}))
    assert sw.apply_profile_overrides(src, {}, tmp_path) == src
    assert sw.apply_profile_overrides(src, {"bogus": "1"}, tmp_path) == src


def test_supports_override_forces_exclude_object_for_m486_previews(tmp_path):
    """A profile WITHOUT exclude_object (e.g. an extracted from-printer profile)
    must still slice with M486 object labels, because the toolkit's plate
    previews (footprint + 3D iso) are parsed from those markers. apply_supports_
    override runs before every slice, so it forces exclude_object=1 (live
    2026-07-15: a from-printer profile dropped the iso view)."""
    src = tmp_path / "proc.json"
    src.write_text(json.dumps({"name": "p"}))  # no exclude_object key
    out = sw.apply_supports_override(src, False, tmp_path)
    assert json.loads(out.read_text())["exclude_object"] == "1"


# ---------- schema ----------

def test_schema_offers_advanced_fields_flagged():
    schema = u1_form.build_form_schema(_spec())
    adv = [f for f in schema["fields"] if f.get("advanced")]
    assert [f["id"] for f in adv] == ["infill", "infill_pattern", "walls", "brim", "fuzzy",
                                      "top_shell", "bottom_shell", "one_wall_top", "raft",
                                      "support_style"]
    assert all(f["group"] == "advanced" and f["default"] == "default" for f in adv)
    # not offered -> absent entirely
    schema2 = u1_form.build_form_schema(_spec(offer=False))
    assert not [f for f in schema2["fields"] if f.get("advanced")]


# ---------- JSON answers ----------

def test_json_answers_map_to_orca_overrides():
    res = u1_form.parse_answers_json(
        {"parts": "all", "tool": "T0", "material": "PLA", "profile": 1,
         "infill": "30", "infill_pattern": "gyroid", "walls": "3",
         "brim": "auto", "fuzzy": "on"}, _spec())
    assert res["ok"], res["errors"]
    assert res["values"]["overrides"] == {
        "sparse_infill_density": "30%", "sparse_infill_pattern": "gyroid",
        "wall_loops": "3", "brim_type": "auto_brim", "fuzzy_skin": "external"}


def test_json_answers_default_means_no_override():
    res = u1_form.parse_answers_json(
        {"parts": "all", "tool": "T0", "material": "PLA", "profile": 1,
         "infill": "default", "brim": "default"}, _spec())
    assert res["ok"] and "overrides" not in res["values"]


def test_json_answers_unknown_advanced_option_fails_loudly():
    res = u1_form.parse_answers_json(
        {"tool": "T0", "material": "PLA", "profile": 1, "infill": "37"}, _spec())
    assert not res["ok"] and any("infill" in e for e in res["errors"])


# ---------- text answers ----------

def test_text_answers_advanced_tokens():
    res = u1_form.parse_answers(
        "parts all | T0 | PLA | profile 1 | infill 30% | gyroid | walls 3 | brim off | fuzzy",
        _spec())
    assert res["ok"], res["errors"]
    assert res["values"]["overrides"] == {
        "sparse_infill_density": "30%", "sparse_infill_pattern": "gyroid",
        "wall_loops": "3", "brim_type": "no_brim", "fuzzy_skin": "external"}


def test_text_answers_unoffered_infill_rejected():
    res = u1_form.parse_answers("T0 | PLA | profile 1 | infill 37", _spec())
    assert not res["ok"] and any("infill" in e for e in res["errors"])


def test_text_advanced_ignored_when_not_offered():
    res = u1_form.parse_answers("T0 | PLA | profile 1 | gyroid", _spec(offer=False))
    assert not res["ok"]  # 'gyroid' is unrecognized when advanced isn't offered
    assert any("unrecognized" in e for e in res["errors"])


# ---------- renderer ----------

def test_renderer_advanced_hidden_from_linear_flow_but_reachable():
    schema = u1_form.build_form_schema(_spec())
    form = tg.new_form(schema)
    flow_ids = [f["id"] for sc in tg._screens(form) for f in sc]
    assert "infill" not in flow_ids and "fuzzy" not in flow_ids
    # Review shows the Advanced button; tapping it jumps to the advanced group
    form["current"] = tg.REVIEW_FIELD
    kb = tg.render_screen(form)["keyboard"]
    adv_btn = next(b for row in kb for b in row if "Advanced" in b["text"])
    tg.apply_callback(form, adv_btn["callback_data"])
    assert form["current"] == "infill" and form.get("_edit_return")
    screen = tg.render_screen(form)
    assert "Advanced settings" in screen["text"]
    # pick gyroid (a grouped radio: marks, doesn't advance)
    fi = tg._field_index(form, "infill_pattern")
    gy = next(i for i, o in enumerate(tg._field(form, "infill_pattern")["options"])
              if tg._opt_id(o) == "gyroid")
    tg.apply_callback(form, f"s:{fi}:{gy}")
    assert form["current"] == "infill"  # still on the advanced screen
    # group Next returns to Review; summary line shows the change
    tg.apply_callback(form, f"n:{tg._field_index(form, 'infill')}")
    assert form["current"] == tg.REVIEW_FIELD
    review = tg.render_screen(form)
    assert "Pattern: gyroid" in review["text"]
    assert any("(1 set)" in b["text"] for row in review["keyboard"] for b in row)


def test_renderer_answer_json_carries_advanced_ids():
    schema = u1_form.build_form_schema(_spec())
    form = tg.new_form(schema)
    fi = tg._field_index(form, "walls")
    three = next(i for i, o in enumerate(tg._field(form, "walls")["options"])
                 if tg._opt_id(o) == "3")
    tg.apply_callback(form, f"s:{fi}:{three}")
    out = tg.answer_json(form)
    assert out["walls"] == "3"
    assert out["infill"] == "default"  # untouched advanced fields submit their default



def test_advanced_buttons_self_describing_and_review_not_duplicated():
    """Live UX feedback 2026-07-06: five bare "Profile default" buttons and
    naked numbers made the advanced screen unreadable ("you feel blind"), and
    Review listed an Edit row per advanced field on top of the one button.
    Every advanced option label must carry its field identity, and Review must
    expose advanced ONLY via the single Advanced button + summary line."""
    schema = u1_form.build_form_schema(_spec())
    form = tg.new_form(schema)
    # every advanced option label is unique across the whole group screen
    labels = [tg._opt_label(o) for f in tg._advanced_fields(form) for o in f["options"]]
    assert len(labels) == len(set(labels)), "duplicate/blind button labels"
    # and each label identifies its field (no bare values)
    for f in tg._advanced_fields(form):
        key = {"infill": "Infill", "infill_pattern": "Pattern", "walls": "Walls",
               "brim": "Brim", "fuzzy": "Fuzzy", "support_style": "Support",
               "top_shell": "Top", "bottom_shell": "Bottom",
               "one_wall_top": "One wall", "raft": "Raft"}[f["id"]]
        assert all(key in tg._opt_label(o) for o in f["options"]), f["id"]
    # Review: exactly ONE advanced-related button (the jump), no Edit rows
    form["current"] = tg.REVIEW_FIELD
    review = tg.render_screen(form)
    adv_ids = {f["id"] for f in tg._advanced_fields(form)}
    edit_targets = []
    for row in review["keyboard"]:
        for b in row:
            cd = b.get("callback_data", "")
            if cd.startswith("e:"):
                fi = int(cd.split(":")[1])
                edit_targets.append(form["schema"]["fields"][fi]["id"])
    assert sum(1 for t in edit_targets if t in adv_ids) == 1  # the one jump button
    assert not any(f"Infill" in b["text"] and "Advanced" not in b["text"]
                   for row in review["keyboard"] for b in row)



def test_support_style_tree_vs_grid():
    """v2.3: tree vs grid support style rides the advanced-override rail."""
    res = u1_form.parse_answers_json(
        {"tool": "T0", "material": "PLA", "profile": 1, "supports": "supports",
         "support_style": "tree"}, _spec())
    assert res["ok"], res["errors"]
    assert res["values"]["overrides"]["support_type"] == "tree(auto)"
    # text mode: "tree supports" sets BOTH supports on and the style
    res2 = u1_form.parse_answers("T0 | PLA | profile 1 | tree supports", _spec())
    assert res2["ok"], res2["errors"]
    assert res2["values"]["supports"] == "supports"
    assert res2["values"]["overrides"]["support_type"] == "tree(auto)"
    # bare "grid" is still the INFILL pattern, not support style
    res3 = u1_form.parse_answers("T0 | PLA | profile 1 | grid", _spec())
    assert res3["ok"]
    assert res3["values"]["overrides"] == {"sparse_infill_pattern": "grid"}
    # "grid supports" is the explicit support-style form
    res4 = u1_form.parse_answers("T0 | PLA | profile 1 | grid supports", _spec())
    assert res4["ok"], res4["errors"]
    assert res4["values"]["overrides"]["support_type"] == "normal(auto)"
    assert res4["values"]["supports"] == "supports"
