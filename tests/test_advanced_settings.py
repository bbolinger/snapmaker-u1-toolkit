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

def test_renderer_advanced_reached_via_two_level_tweak_menu():
    schema = u1_form.build_form_schema(_spec())
    form = tg.new_form(schema)
    flow_ids = [f["id"] for sc in tg._screens(form) for f in sc]
    assert "infill" not in flow_ids and "fuzzy" not in flow_ids
    # Review shows ONE Print-tweaks button that opens the category MENU (not a
    # flat field jump).
    form["current"] = tg.REVIEW_FIELD
    kb = tg.render_screen(form)["keyboard"]
    tweak_btn = next(b for row in kb for b in row if "Advanced options" in b["text"])
    assert tweak_btn["callback_data"] == "g:m"
    tg.apply_callback(form, tweak_btn["callback_data"])
    assert form["current"] == tg.ADV_MENU
    menu = tg.render_screen(form)
    cats = [b["callback_data"] for row in menu["keyboard"] for b in row
            if b["callback_data"].startswith("g:c:")]
    assert "g:c:strength" in cats and "g:c:finish" in cats
    # open Strength & shells -> lands on the first strength control; the page
    # shows ONLY that category's fields, not the whole advanced list
    tg.apply_callback(form, "g:c:strength")
    assert form["current"] == "infill"
    page = tg.render_screen(form)
    assert "Strength" in page["text"]
    assert any("Pattern" in b["text"] for row in page["keyboard"] for b in row)
    assert not any("Fuzzy" in b["text"] for row in page["keyboard"] for b in row)
    # pick gyroid (a radio: marks, stays on the category page)
    fi = tg._field_index(form, "infill_pattern")
    gy = next(i for i, o in enumerate(tg._field(form, "infill_pattern")["options"])
              if tg._opt_id(o) == "gyroid")
    tg.apply_callback(form, f"s:{fi}:{gy}")
    assert form["current"] == "infill"  # still on the strength page
    # Back to the menu -> the strength row now shows its changed count
    tg.apply_callback(form, "g:m")
    assert form["current"] == tg.ADV_MENU
    menu2 = tg.render_screen(form)
    assert any(b["callback_data"] == "g:c:strength" and "1 changed" in b["text"]
               for row in menu2["keyboard"] for b in row)
    # Done -> Review; the change shows in the summary line + button count
    tg.apply_callback(form, "g:d")
    assert form["current"] == tg.REVIEW_FIELD
    review = tg.render_screen(form)
    assert "Pattern: gyroid" in review["text"]
    assert any("(1 changed)" in b["text"] for row in review["keyboard"] for b in row)


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
    # Review: exactly ONE advanced-related button (opens the tweak menu), and
    # NO per-advanced-field Edit rows.
    form["current"] = tg.REVIEW_FIELD
    review = tg.render_screen(form)
    adv_ids = {f["id"] for f in tg._advanced_fields(form)}
    edit_targets = []
    menu_buttons = 0
    for row in review["keyboard"]:
        for b in row:
            cd = b.get("callback_data", "")
            if cd == "g:m":
                menu_buttons += 1
            elif cd.startswith("e:"):
                fi = int(cd.split(":")[1])
                edit_targets.append(form["schema"]["fields"][fi]["id"])
    assert menu_buttons == 1  # the single advanced-options jump
    assert not any(t in adv_ids for t in edit_targets)  # no advanced Edit rows
    # no raw advanced control (e.g. an "Infill" option button) leaks onto Review
    assert not any("Infill" in b["text"] for row in review["keyboard"] for b in row)


def test_tweak_menu_conditional_supports_and_reset():
    """The Supports category only appears when the setup Supports toggle is ON
    (Orca ignores support_type otherwise), and Reset-all returns every advanced
    control to profile default."""
    schema = u1_form.build_form_schema(_spec())
    form = tg.new_form(schema)
    # supports defaults OFF -> the Supports category has no applicable field
    assert "supports" not in [k for k, _l, _f in tg._adv_categories(form)]
    # turn supports ON via its setup field -> the category becomes available
    sfi = tg._field_index(form, "supports")
    on = next(i for i, o in enumerate(tg._field(form, "supports")["options"])
              if tg._opt_id(o) == "supports")
    tg.apply_callback(form, f"s:{sfi}:{on}")
    assert "supports" in [k for k, _l, _f in tg._adv_categories(form)]
    # set a tweak, confirm it counts, then Reset-all clears it
    wfi = tg._field_index(form, "walls")
    three = next(i for i, o in enumerate(tg._field(form, "walls")["options"])
                 if tg._opt_id(o) == "3")
    tg.apply_callback(form, f"s:{wfi}:{three}")
    assert tg._adv_changed_count(form, tg._advanced_fields(form)) >= 1
    tg.apply_callback(form, "g:r")
    assert tg._adv_changed_count(form, tg._advanced_fields(form)) == 0
    assert tg.answer_json(form)["walls"] == "default"


def test_every_renderer_callback_is_routable_by_the_gateway():
    """Guard the live gap that shipped 2026-07-15: the tweak-menu buttons used
    callbacks (am/cat:...) the gateway's FORM_CB_PATTERN didn't route, so a tap
    only highlighted and nothing opened. Every callback the renderer can emit
    MUST match that pattern (or a button is dead on Telegram). Unit tests that
    call apply_callback directly can't catch this, so assert it structurally."""
    import re
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                           / "adapters" / "hermes" / "plugin"))
    import telegram_patch as tp  # noqa: E402
    form_re = re.compile(tp.FORM_CB_PATTERN)

    spec = _spec()
    # >8 profiles so the profile screen paginates (exercises the page controls)
    spec["profiles"] = [{"idx": i, "label": f"P{i}"} for i in range(1, 13)]
    spec["advanced_resolved"] = {"1": {"walls": "2"}}
    schema = u1_form.build_form_schema(spec)
    form = tg.new_form(schema)
    form["selections"]["profile"] = 0  # surfaces the single-select Continue button

    seen: set[str] = set()

    def _collect(screen):
        for row in screen["keyboard"]:
            for b in row:
                seen.add(b["callback_data"])

    # every field screen (groups + single + multi + the paginated profile pages)
    for f in form["schema"]["fields"]:
        if tg._is_screen_field(f):
            form["current"] = f["id"]
            _collect(tg.render_screen(form))
    form["current"] = "profile"
    for pg in range(3):
        form["pages"]["profile"] = pg
        _collect(tg.render_screen(form))
    # review + advanced-options menu + every category page
    form["current"] = tg.REVIEW_FIELD
    _collect(tg.render_screen(form))
    form["current"] = tg.ADV_MENU
    _collect(tg.render_screen(form))
    for key, _l, _f in tg._adv_categories(form):
        tg.apply_callback(form, f"g:c:{key}")
        _collect(tg.render_screen(form))

    unrouted = sorted(cd for cd in seen if not form_re.match(cd))
    assert not unrouted, f"gateway FORM_CB_PATTERN would not route: {unrouted}"


# ---------- resolved profile values ("keep profile (X)") ----------

def test_resolve_advanced_from_profile_maps_scalars():
    flat = {
        "sparse_infill_density": "15%", "sparse_infill_pattern": "grid",
        "wall_loops": "2", "top_shell_layers": "4", "bottom_shell_layers": "3",
        "only_one_wall_top": "0", "brim_type": "no_brim", "raft_layers": "0",
        "fuzzy_skin": "none", "support_type": "tree(auto)",
    }
    r = u1_form.resolve_advanced_from_profile(flat)
    assert r == {"infill": "15%", "infill_pattern": "grid", "walls": "2",
                 "top_shell": "4", "bottom_shell": "3", "one_wall_top": "off",
                 "brim": "off", "raft": "off", "fuzzy": "off",
                 "support_style": "tree"}
    # non-default raw values surface too; an absent key is simply omitted
    assert u1_form.resolve_advanced_from_profile(
        {"raft_layers": "3", "only_one_wall_top": "1", "fuzzy_skin": "external"}
    ) == {"raft": "3 layers", "one_wall_top": "on", "fuzzy": "on"}
    assert u1_form.resolve_advanced_from_profile({}) == {}


def test_schema_carries_advanced_resolved_keyed_by_str_idx():
    spec = _spec()
    spec["profiles"] = [{"idx": 1, "label": "A"}, {"idx": 2, "label": "B"}]
    spec["advanced_resolved"] = {1: {"walls": "2"}, 2: {"walls": "4"}}
    schema = u1_form.build_form_schema(spec)
    assert schema["advanced_resolved"] == {"1": {"walls": "2"}, "2": {"walls": "4"}}
    # not offered -> key absent
    assert "advanced_resolved" not in u1_form.build_form_schema(_spec(offer=False))


def test_tweak_menu_keep_profile_value_follows_selected_profile():
    spec = _spec()
    spec["profiles"] = [{"idx": 1, "label": "0.20 Std"}, {"idx": 2, "label": "0.28 Draft"}]
    spec["advanced_resolved"] = {"1": {"walls": "2", "infill": "15%"},
                                 "2": {"walls": "4", "infill": "25%"}}
    schema = u1_form.build_form_schema(spec)
    form = tg.new_form(schema)
    pfi = tg._field_index(form, "profile")

    def _pick_profile(idx_str):
        oi = next(i for i, o in enumerate(tg._field(form, "profile")["options"])
                  if str(tg._opt_id(o)) == idx_str)
        tg.apply_callback(form, f"s:{pfi}:{oi}")

    _pick_profile("1")
    form["current"] = "walls"  # a Strength & shells control
    txts = [b["text"] for row in tg.render_screen(form)["keyboard"] for b in row]
    assert any("keep profile (2)" in t for t in txts)
    assert any("keep profile (15%)" in t for t in txts)
    # switch profile -> the keep-profile values track the new selection
    _pick_profile("2")
    form["current"] = "walls"
    txts2 = [b["text"] for row in tg.render_screen(form)["keyboard"] for b in row]
    assert any("keep profile (4)" in t for t in txts2)
    assert not any("keep profile (2)" in t for t in txts2)


def test_build_form_spec_resolves_advanced_on_the_persisted_emit_path(monkeypatch):
    """The form-EMIT call passes persisted profiles (index stability), so the
    resolved "keep profile (X)" values must be computed on that path too, not
    only the fresh build. Live 2026-07-15: the emitted form showed "profile
    default" everywhere because resolution lived only in the fresh branch."""
    import u1_kit_workflow as kw
    fake = [{"value": "p_a", "path": "/x/a.json", "label": "A", "recommended": True},
            {"value": "p_b", "path": "/x/b.json", "label": "B", "recommended": False}]
    monkeypatch.setattr(kw, "list_profiles", lambda **k: fake)
    monkeypatch.setattr(sw, "_flatten_process_profile",
                        lambda p, **k: {"wall_loops": "3", "sparse_infill_density": "20%"})
    kit = {"parts": [{"part_id": "p1", "filename": "a.stl", "footprint_mm": (10, 10)}]}
    persisted = [{"idx": 1, "value": "p_a", "label": "A", "recommended": True},
                 {"idx": 2, "value": "p_b", "label": "B", "recommended": False}]
    spec = kw._build_form_spec(kit, "0.4", persisted_profiles=persisted,
                              refresh=False, scrub=False)
    assert spec["advanced_resolved"] == {
        "1": {"walls": "3", "infill": "20%"},
        "2": {"walls": "3", "infill": "20%"}}
    # and it survives into the schema
    schema = u1_form.build_form_schema(spec)
    assert schema["advanced_resolved"]["1"]["walls"] == "3"


def test_tweak_menu_keep_profile_label_absent_without_resolved():
    # No advanced_resolved in the schema -> the generic "profile default" stands.
    schema = u1_form.build_form_schema(_spec())
    form = tg.new_form(schema)
    form["current"] = "walls"
    txts = [b["text"] for row in tg.render_screen(form)["keyboard"] for b in row]
    assert any("Walls: profile default" in t for t in txts)
    assert not any("keep profile" in t for t in txts)



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
