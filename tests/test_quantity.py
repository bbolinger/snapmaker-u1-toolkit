"""Quantity (v2.3): print N copies of a single-part job.

Quantity rides the existing kit machinery — the commit path duplicates the
lone part path N times, so the arranger packs N instances, the instance-keyed
previews draw every copy, and the plate-overflow split absorbs a count that
doesn't fit one bed. Offered ONLY for single-part jobs; per-part quantities on
multi-part kits are out of scope. A bare integer keeps its existing meaning
(profile index / loud kit ambiguity) — quantity is always an explicit form.
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import u1_audit
import u1_form
import u1_kit_workflow as kw
import u1_request
from u1_orient import write_binary_stl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapters" / "telegram"))
import u1_form_telegram as tg  # noqa: E402


def _spec(n_parts=1, offer=True):
    parts = [{"id": f"{i:02d}_p{i}", "label": f"p{i} (50x50mm)"}
             for i in range(1, n_parts + 1)]
    spec = {
        "parts": parts,
        "tools": ["T0", "T1"], "materials": ["PLA", "PETG"],
        "profiles": [{"idx": 1, "label": "0.20 Std"},
                     {"idx": 2, "label": "0.16 Opt"},
                     {"idx": 3, "label": "0.24 Draft"}],
        "supports": ["supports", "no-supports"],
        "actions": ["start", "upload-only"],
    }
    if offer:
        spec["offer_quantity"] = True
    return spec


# ---------- spec assembly (workflow) ----------

def _kit(n):
    return {"part_count": n, "parts": [
        {"part_id": f"{i:02d}_p{i}", "filename": f"p{i}.stl",
         "footprint_mm": [50.0, 50.0], "path": f"/tmp/p{i}.stl"}
        for i in range(1, n + 1)]}


def test_workflow_offers_quantity_only_for_single_part():
    profs = [{"idx": 1, "value": "0_20_standard", "label": "0.20 Standard"}]
    # refresh=False: this exercises offer_quantity logic, not the live printer.
    single = kw._build_form_spec(_kit(1), "0.4", persisted_profiles=profs, refresh=False)
    assert single.get("offer_quantity") is True
    multi = kw._build_form_spec(_kit(3), "0.4", persisted_profiles=profs, refresh=False)
    assert "offer_quantity" not in multi


def test_build_form_spec_refreshes_toolmap_before_render(monkeypatch):
    """The render path must pull the printer's LIVE filament before reading the
    tool map, so a spool swapped between jobs isn't shown stale on the head
    screen (bug 2026-07-14). The persist / re-validate paths pass refresh=False
    and must NOT query the printer."""
    import u1_toolmap
    profs = [{"idx": 1, "value": "0_20_standard", "label": "0.20 Standard"}]
    calls = {"n": 0}

    def _count(*a, **k):
        calls["n"] += 1
        return True

    monkeypatch.setattr(u1_toolmap, "refresh_toolmap", _count)
    monkeypatch.setattr(u1_toolmap, "load_head_options", lambda *a, **k: [])
    kw._build_form_spec(_kit(1), "0.4", persisted_profiles=profs)  # refresh=True default
    assert calls["n"] == 1, "render path must refresh before reading the tool map"
    kw._build_form_spec(_kit(1), "0.4", persisted_profiles=profs, refresh=False)
    assert calls["n"] == 1, "refresh=False must not query the printer"


def test_build_form_spec_marks_recommended_profile(monkeypatch):
    """A1: the form passes the operator's last-used preset to the picker and
    carries the recommended flag through, so the schema pre-selects it."""
    import u1_form
    seen = {}

    def fake_list_profiles(nozzle=None, history_print_settings_id=None):
        seen["history_id"] = history_print_settings_id
        return [
            {"value": "a", "label": "0.20 Standard @Snapmaker U1 (0.4 nozzle)"},
            {"value": "b", "label": "0.16 Optimal @Snapmaker U1 (0.4 nozzle)",
             "recommended": True},
        ]

    monkeypatch.setattr(kw, "list_profiles", fake_list_profiles)
    monkeypatch.setattr(kw, "last_used_print_settings_id", lambda **k: "0.16 Optimal")
    spec = kw._build_form_spec(_kit(1), "0.4", refresh=False)
    assert seen["history_id"] == "0.16 Optimal", "must tell the picker the last-used preset"
    rec = [p for p in spec["profiles"] if p.get("recommended")]
    assert len(rec) == 1 and rec[0]["idx"] == 2, "recommended profile carried into the spec"
    schema = u1_form.build_form_schema(spec)
    pf = next(f for f in schema["fields"] if f["id"] == "profile")
    assert pf["default"] == 2, "schema pre-selects the recommended profile end-to-end"


# ---------- schema ----------

def test_schema_quantity_is_a_stepper_on_the_setup_group():
    schema = u1_form.build_form_schema(_spec())
    q = next(f for f in schema["fields"] if f["id"] == "quantity")
    assert q["label"] == "Copies" and q["group"] == "setup"
    assert q["default"] == "1" and q["required"] is False
    # rendered as a +/- stepper (1..50), no longer a capped 1-9 option grid
    assert q.get("stepper") == {"steps": (5, 1), "unit": "", "min": 1, "max": 50}
    assert not q.get("advanced")   # a plain top-level count, not a profile override
    # not offered -> absent entirely
    schema2 = u1_form.build_form_schema(_spec(offer=False))
    assert "quantity" not in [f["id"] for f in schema2["fields"]]


def test_text_fallback_form_lists_quantity_only_when_offered():
    assert "QUANTITY" in u1_form.build_form(_spec())
    assert "QUANTITY" not in u1_form.build_form(_spec(offer=False))


# ---------- text answers ----------

@pytest.mark.parametrize("token", ["x3", "3x", "qty 3", "quantity 3", "3 copies"])
def test_text_quantity_tokens(token):
    r = u1_form.parse_answers(f"T0 | PLA | profile 1 | {token}", _spec())
    assert r["ok"], r["errors"]
    assert r["values"]["quantity"] == 3


def test_text_quantity_defaults_to_one():
    r = u1_form.parse_answers("T0 | PLA | profile 1", _spec())
    assert r["ok"], r["errors"]
    assert r["values"]["quantity"] == 1


def test_bare_int_still_means_what_it_means_today():
    # kit-of-1 spec: a bare number stays the loud ambiguity error — it must
    # never silently become a quantity.
    r = u1_form.parse_answers("3 | T0 | PLA", _spec())
    assert not r["ok"]
    assert any("ambiguous" in e for e in r["errors"])
    assert r["values"].get("quantity", 1) == 1
    # partless spec: bare number stays the profile index, and an explicit
    # x-form sets quantity alongside it.
    spec0 = _spec()
    spec0["parts"] = []
    r2 = u1_form.parse_answers("3 | x2 | T0 | PLA", spec0)
    assert r2["ok"], r2["errors"]
    assert r2["values"]["profile"]["idx"] == 3
    assert r2["values"]["quantity"] == 2


def test_numeric_list_still_selects_parts_on_kits():
    # multi-part kits never offer quantity; list grammar is untouched
    r = u1_form.parse_answers("1,3 | T0 | PLA | profile 1", _spec(n_parts=3, offer=False))
    assert r["ok"], r["errors"]
    assert r["values"]["parts"] == [1, 3]


def test_text_conflicting_quantities_fail_loudly():
    r = u1_form.parse_answers("T0 | PLA | profile 1 | x3 | qty 2", _spec())
    assert not r["ok"]
    assert any("quantity" in e and "twice" in e for e in r["errors"])


def test_text_repeated_same_quantity_is_harmless():
    r = u1_form.parse_answers("T0 | PLA | profile 1 | x3 | qty 3", _spec())
    assert r["ok"], r["errors"]
    assert r["values"]["quantity"] == 3


def test_text_in_range_count_now_accepted():
    # 5 was NOT in the old 1/2/3/4/6/9 grid; the stepper's 1..50 range accepts it
    r = u1_form.parse_answers("T0 | PLA | profile 1 | x5", _spec())
    assert r["ok"], r["errors"]
    assert r["values"]["quantity"] == 5


def test_text_out_of_range_count_rejected():
    r = u1_form.parse_answers("T0 | PLA | profile 1 | x99", _spec())
    assert not r["ok"]
    assert any("out of range" in e for e in r["errors"])


def test_text_quantity_ignored_when_not_offered():
    r = u1_form.parse_answers("T0 | PLA | profile 1 | x3", _spec(offer=False))
    assert not r["ok"]  # 'x3' is unrecognized when quantity isn't offered
    assert any("unrecognized" in e for e in r["errors"])
    assert "quantity" not in r["values"]


# ---------- JSON answers ----------

def test_json_quantity_accepted_as_string_or_int():
    for raw in ("3", 3):
        r = u1_form.parse_answers_json(
            {"tool": "T0", "material": "PLA", "profile": 1, "quantity": raw},
            _spec())
        assert r["ok"], r["errors"]
        assert r["values"]["quantity"] == 3


def test_json_quantity_defaults_to_one():
    r = u1_form.parse_answers_json(
        {"tool": "T0", "material": "PLA", "profile": 1}, _spec())
    assert r["ok"] and r["values"]["quantity"] == 1


def test_json_count_past_nine_accepted():
    r = u1_form.parse_answers_json(
        {"tool": "T0", "material": "PLA", "profile": 1, "quantity": "12"}, _spec())
    assert r["ok"], r["errors"]
    assert r["values"]["quantity"] == 12


def test_json_out_of_range_count_fails_loudly():
    r = u1_form.parse_answers_json(
        {"tool": "T0", "material": "PLA", "profile": 1, "quantity": "99"}, _spec())
    assert not r["ok"]
    assert any("out of range" in e for e in r["errors"])


def test_json_quantity_ignored_when_not_offered():
    r = u1_form.parse_answers_json(
        {"tool": "T0", "material": "PLA", "profile": 1, "quantity": "3"},
        _spec(offer=False))
    assert r["ok"], r["errors"]
    assert "quantity" not in r["values"]


def test_json_and_text_agree_on_quantity():
    spec = _spec()
    a = u1_form.parse_answers("T0 | PLA | profile 1 | x3", spec)["values"]
    b = u1_form.parse_answers_json(
        {"tool": "T0", "material": "PLA", "profile": 1, "quantity": "3"},
        spec)["values"]
    assert a["quantity"] == b["quantity"] == 3


def test_echo_parse_shows_quantity_only_when_plural():
    spec = _spec()
    v3 = u1_form.parse_answers("T0 | PLA | profile 1 | x3", spec)["values"]
    assert "quantity=3" in u1_form.echo_parse(v3, spec)
    v1 = u1_form.parse_answers("T0 | PLA | profile 1", spec)["values"]
    assert "quantity" not in u1_form.echo_parse(v1, spec)


# ---------- renderer ----------

def test_renderer_quantity_is_a_stepper_that_dials_past_nine():
    schema = u1_form.build_form_schema(_spec())
    form = tg.new_form(schema)
    # quantity renders on the SAME screen as supports (no new step)
    screen_ids = [[f["id"] for f in sc] for sc in tg._screens(form)]
    setup = next(sc for sc in screen_ids if "supports" in sc)
    assert "quantity" in setup
    fi = tg._field_index(form, "quantity")
    form["current"] = "quantity"
    kb = tg.render_screen(form)["keyboard"]
    # a header showing the current count (tap = reset) + one step row -5/-/+/+5
    assert any(b.get("callback_data") == f"T:{fi}:k" and "Copies: 1" in b["text"]
               for row in kb for b in row)
    step_cbs = [b["callback_data"] for row in kb for b in row
                if b.get("callback_data", "").startswith(f"T:{fi}:")
                and b["callback_data"] != f"T:{fi}:k"]
    assert step_cbs == [f"T:{fi}:-5", f"T:{fi}:-1", f"T:{fi}:1", f"T:{fi}:5"]
    # dial PAST the old 9 cap: +5 +5 +1 -> 12 (the whole point)
    tg.apply_callback(form, f"T:{fi}:5")
    tg.apply_callback(form, f"T:{fi}:5")
    tg.apply_callback(form, f"T:{fi}:1")
    assert form["steps"]["quantity"] == 12
    assert tg.answer_json(form)["quantity"] == "12"
    # review echoes the dialed count
    form["current"] = tg.REVIEW_FIELD
    assert "12" in tg.render_screen(form)["text"]


def test_renderer_quantity_clamps_and_resets():
    schema = u1_form.build_form_schema(_spec())
    form = tg.new_form(schema)
    fi = tg._field_index(form, "quantity")
    # can't dial below the floor of 1
    for _ in range(5):
        tg.apply_callback(form, f"T:{fi}:-5")
    assert form["steps"]["quantity"] == 1
    # clamps at the 50 ceiling
    for _ in range(20):
        tg.apply_callback(form, f"T:{fi}:5")
    assert form["steps"]["quantity"] == 50
    assert tg.answer_json(form)["quantity"] == "50"
    # tap the header -> reset to default (omitted from answers -> workflow uses 1)
    tg.apply_callback(form, f"T:{fi}:k")
    assert "quantity" not in tg.answer_json(form)


# ---------- commit path (workflow) ----------

_GCODE = "T0\nG1 X10 Y10 F3000\nG1 X50 Y50 E1.5\n"


def _cube(path, s):
    v = np.array([[0, 0, 0], [s, 0, 0], [s, s, 0], [0, s, 0],
                  [0, 0, s], [s, 0, s], [s, s, s], [0, s, s]], dtype=np.float32)
    faces = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    write_binary_stl(path, np.array([[v[a], v[b], v[c]] for a, b, c in faces],
                                    dtype=np.float32))
    return path


def _kit_zip(tmp_path, n=1):
    zp = tmp_path / "kit.zip"
    with zipfile.ZipFile(zp, "w") as z:
        for i in range(n):
            z.write(_cube(tmp_path / f"p{i}.stl", 20 + i), f"p{i}.stl")
    return zp


def _args(model, **kw_):
    base = dict(model=str(model), json_events=True, form_answers=None,
                form_answers_json=None, form_answers_from=None,
                interaction_mode=None, request_id=None, fresh=False,
                operator="test:unit", nozzle="0.4", out_dir=None,
                live_upload=False, on_collision=None, action=None)
    base.update(kw_)
    return SimpleNamespace(**base)


@pytest.fixture
def hermetic_commit(monkeypatch):
    """Stub the slicer/printer boundary; capture the paths handed to arrange."""
    monkeypatch.setattr(kw, "list_profiles", lambda nozzle=None, history_print_settings_id=None: [
        {"value": "0_20_standard", "label": "0.20 Standard @Snapmaker U1 (0.4 nozzle)"}])
    captured = {"calls": []}

    def fake_arrange(paths, out_dir, **kwargs):
        captured["calls"].append([str(p) for p in paths])
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        g = out_dir / "plate_1.gcode"
        g.write_text(_GCODE)
        return {"plate_count": 1, "plates": [{
            "plate_idx": 1, "gcode_path": str(g),
            "gcode_hash": "sha256:plate1", "metadata": {}}], "cmd": ["orca"]}

    monkeypatch.setattr(kw.u1_arrange, "arrange_slice", fake_arrange)
    monkeypatch.setattr(kw, "profile_path", lambda slug: Path("/tmp/process.json"))
    monkeypatch.setattr(kw, "apply_supports_override",
                        lambda p, en, od: Path("/tmp/process_ovr.json"))
    monkeypatch.setattr(kw, "_real_upload", lambda g, on_collision=None, material=None: {
        "uploaded_filename": Path(g).name, "moonraker_upload_ok": True, "returncode": 0})
    return captured


def test_commit_duplicates_single_part_path(tmp_path, hermetic_commit):
    res = kw.run_kit_workflow(_args(
        _kit_zip(tmp_path, 1),
        form_answers="T0 | PLA | profile 1 | x3 | upload-only"))
    assert res["phase"] == "complete", res
    paths = hermetic_commit["calls"][-1]
    assert len(paths) == 3
    assert len(set(paths)) == 1  # three instances of the SAME part


def test_commit_persists_and_audits_quantity(tmp_path, hermetic_commit):
    res = kw.run_kit_workflow(_args(
        _kit_zip(tmp_path, 1),
        form_answers="T0 | PLA | profile 1 | x3 | upload-only"))
    rid = res["request_id"]
    assert u1_request.read_request(rid)["quantity"] == 3
    sliced = next(r for r in u1_audit.read(rid) if r["event"] == "kit_sliced")
    assert sliced["details"]["quantity"] == 3
    assert sliced["details"]["parts"] == 1  # distinct parts, not instances


def test_commit_json_answers_quantity(tmp_path, hermetic_commit):
    res = kw.run_kit_workflow(_args(
        _kit_zip(tmp_path, 1),
        form_answers_json=json.dumps({"tool": "T0", "material": "PLA",
                                      "profile": 1, "quantity": "4",
                                      "action": "upload-only"})))
    assert res["phase"] == "complete", res
    assert len(hermetic_commit["calls"][-1]) == 4


def test_default_run_is_unchanged(tmp_path, hermetic_commit):
    """No quantity in the answers -> single path to arrange, no quantity key
    persisted or audited — byte-identical to the pre-quantity flow."""
    res = kw.run_kit_workflow(_args(
        _kit_zip(tmp_path, 1),
        form_answers="T0 | PLA | profile 1 | upload-only"))
    assert res["phase"] == "complete", res
    assert len(hermetic_commit["calls"][-1]) == 1
    rid = res["request_id"]
    assert "quantity" not in u1_request.read_request(rid)
    sliced = next(r for r in u1_audit.read(rid) if r["event"] == "kit_sliced")
    assert "quantity" not in sliced["details"]


def test_multipart_kit_never_duplicates(tmp_path, hermetic_commit):
    res = kw.run_kit_workflow(_args(
        _kit_zip(tmp_path, 2),
        form_answers_json=json.dumps({"parts": "all", "tool": "T0",
                                      "material": "PLA", "profile": 1,
                                      "quantity": "3",  # ignored: not offered
                                      "action": "upload-only"})))
    assert res["phase"] == "complete", res
    paths = hermetic_commit["calls"][-1]
    assert len(paths) == 2
    assert len(set(paths)) == 2
    assert "quantity" not in u1_request.read_request(res["request_id"])
