"""Tests for scripts/u1_form.py — script-parsed one-line decision form (B/C).

The parser is safety-relevant: it interprets the operator's intent that drives
slicing. Tests cover the happy path, order-independence, forgiving punctuation,
selection ranges, defaults, and adversarial/malformed input (which must fail
loudly, never silently mis-parse).
"""
from __future__ import annotations

import u1_form


def _spec(n_parts=3):
    parts = [{"id": f"{i:02d}_part{i}", "label": f"part{i} (50x50mm)"} for i in range(1, n_parts + 1)]
    return {
        "parts": parts if n_parts else [],
        "tools": ["T0", "T1", "T2", "T3"],
        "materials": ["PLA", "PETG", "ABS"],
        "profiles": [
            {"idx": 1, "label": "0.20 Standard @Snapmaker U1 (0.4 nozzle)"},
            {"idx": 2, "label": "0.16 Optimal @Snapmaker U1 (0.4 nozzle)"},
        ],
        "supports": ["supports", "no-supports", "overhangs"],
        "actions": ["start", "upload-only"],
    }


# --------------------------------------------------------------------------- #
# Happy path + order independence
# --------------------------------------------------------------------------- #

def test_full_answer_canonical_order():
    r = u1_form.parse_answers("parts 1,3 | auto | T0 | PLA | profile 2 | no-supports | start", _spec())
    assert r["ok"], r["errors"]
    v = r["values"]
    assert v["parts"] == [1, 3]
    assert v["orient"] == "auto"
    assert v["tool"] == "T0"
    assert v["material"] == "PLA"
    assert v["profile"]["idx"] == 2
    assert v["supports"] == "no-supports"
    assert v["action"] == "start"


def test_order_independent():
    r = u1_form.parse_answers("PLA | start | T1 | profile 1 | supports | all | as-authored", _spec())
    assert r["ok"], r["errors"]
    v = r["values"]
    assert v["tool"] == "T1" and v["material"] == "PLA"
    assert v["profile"]["idx"] == 1 and v["supports"] == "supports"
    assert v["parts"] == [1, 2, 3] and v["orient"] == "as-authored"
    assert v["action"] == "start"


def test_forgiving_separators_and_case():
    r = u1_form.parse_answers("t0 ; pla ; PROFILE 1 ; OVERHANGS", _spec(n_parts=0))
    assert r["ok"], r["errors"]
    assert r["values"]["tool"] == "T0"
    assert r["values"]["material"] == "PLA"
    assert r["values"]["supports"] == "overhangs"


# --------------------------------------------------------------------------- #
# Selection parsing
# --------------------------------------------------------------------------- #

def test_parts_range():
    r = u1_form.parse_answers("parts 1-3 | T0 | PLA | profile 1", _spec(n_parts=5))
    assert r["values"]["parts"] == [1, 2, 3]


def test_parts_bare_list_without_prefix():
    r = u1_form.parse_answers("1,3,5 | T0 | PLA | profile 1", _spec(n_parts=5))
    assert r["ok"], r["errors"]
    assert r["values"]["parts"] == [1, 3, 5]


def test_parts_all_keyword():
    r = u1_form.parse_answers("all | T0 | PLA | profile 1", _spec(n_parts=4))
    assert r["ok"], r["errors"]  # 'all' must be RECOGNIZED, not silently defaulted
    assert r["values"]["parts"] == [1, 2, 3, 4]
    assert "all" not in r["unrecognized"]


def test_parts_default_is_all_when_omitted():
    r = u1_form.parse_answers("T0 | PLA | profile 1", _spec(n_parts=3))
    assert r["ok"], r["errors"]
    assert r["values"]["parts"] == [1, 2, 3]


def test_parts_out_of_range_errors():
    r = u1_form.parse_answers("parts 1,9 | T0 | PLA | profile 1", _spec(n_parts=3))
    assert not r["ok"]
    assert any("out of range" in e for e in r["errors"])


def test_parts_given_on_single_part_job_errors():
    r = u1_form.parse_answers("parts 1,2 | T0 | PLA | profile 1", _spec(n_parts=0))
    assert not r["ok"]
    assert any("single-part" in e for e in r["errors"])


# --------------------------------------------------------------------------- #
# Disambiguation: bare int = profile, list = parts
# --------------------------------------------------------------------------- #

def test_bare_int_is_profile_not_part():
    r = u1_form.parse_answers("2 | T0 | PLA", _spec(n_parts=3))
    assert r["ok"], r["errors"]
    assert r["values"]["profile"]["idx"] == 2
    # parts defaulted to all, not [2]
    assert r["values"]["parts"] == [1, 2, 3]


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

def test_defaults_orient_supports_action():
    r = u1_form.parse_answers("T0 | PLA | profile 1", _spec(n_parts=0))
    assert r["ok"], r["errors"]
    v = r["values"]
    assert v["orient"] == "as-authored"
    assert v["supports"] == "no-supports"
    assert v["action"] == "start"


# --------------------------------------------------------------------------- #
# Required-field + validation failures
# --------------------------------------------------------------------------- #

def test_missing_required_fields_fail_loudly():
    r = u1_form.parse_answers("auto | no-supports", _spec(n_parts=0))
    assert not r["ok"]
    joined = " ".join(r["errors"])
    assert "tool" in joined and "material" in joined and "profile" in joined


def test_tool_not_offered_errors():
    spec = _spec(n_parts=0)
    spec["tools"] = ["T0", "T1"]
    r = u1_form.parse_answers("T3 | PLA | profile 1", spec)
    assert not r["ok"]
    assert any("tool T3 not offered" in e for e in r["errors"])


def test_profile_index_out_of_range_errors():
    r = u1_form.parse_answers("T0 | PLA | profile 9", _spec(n_parts=0))
    assert not r["ok"]
    assert any("profile index 9 out of range" in e for e in r["errors"])


def test_unrecognized_token_fails_loudly_not_silent():
    r = u1_form.parse_answers("T0 | PLA | profile 1 | flibbertigibbet", _spec(n_parts=0))
    assert not r["ok"]
    assert "flibbertigibbet" in " ".join(r["errors"])
    assert "flibbertigibbet" in r["unrecognized"]


def test_unknown_material_is_unrecognized():
    r = u1_form.parse_answers("T0 | UNOBTAINIUM | profile 1", _spec(n_parts=0))
    assert not r["ok"]
    assert any("UNOBTAINIUM" in u for u in r["unrecognized"])


# --------------------------------------------------------------------------- #
# Profile by name
# --------------------------------------------------------------------------- #

def test_profile_by_name_substring():
    r = u1_form.parse_answers("T0 | PLA | profile Optimal", _spec(n_parts=0))
    assert r["ok"], r["errors"]
    assert r["values"]["profile"]["idx"] == 2
    assert "Optimal" in r["values"]["profile"]["label"]


def test_profile_ambiguous_name_not_matched():
    # "Snapmaker" matches both -> ambiguous -> not resolved -> error
    r = u1_form.parse_answers("T0 | PLA | profile Snapmaker", _spec(n_parts=0))
    assert not r["ok"]


# --------------------------------------------------------------------------- #
# build_form / echo_parse
# --------------------------------------------------------------------------- #

def test_build_form_lists_parts_and_options():
    text = u1_form.build_form(_spec(n_parts=3))
    assert "PARTS (3)" in text
    assert "TOOL:" in text and "T0" in text
    assert "PROFILE" in text and "Optimal" in text
    assert "Example:" in text


def test_echo_parse_roundtrip_readable():
    r = u1_form.parse_answers("parts 1,3 | T0 | PLA | profile 2 | no-supports | start", _spec(n_parts=3))
    echo = u1_form.echo_parse(r["values"], _spec(n_parts=3))
    assert echo.startswith("I read:")
    assert "tool=T0" in echo and "material=PLA" in echo
    assert "supports=no-supports" in echo and "action=start" in echo


def test_echo_parse_all_parts_collapses():
    r = u1_form.parse_answers("all | T0 | PLA | profile 1", _spec(n_parts=4))
    echo = u1_form.echo_parse(r["values"], _spec(n_parts=4))
    assert "parts=all (4)" in echo


def test_echo_parse_resolves_profile_name_from_index():
    # Review L1: the verification surface must show the profile NAME, not "#2",
    # so the operator can actually verify the choice before the photo gate.
    r = u1_form.parse_answers("T0 | PLA | profile 2", _spec(n_parts=0))
    assert r["values"]["profile"] == {"idx": 2}  # parser stores index only
    echo = u1_form.echo_parse(r["values"], _spec(n_parts=0))
    assert "profile=0.16 Optimal @Snapmaker U1 (0.4 nozzle)" in echo
    assert "#2" not in echo


# --------------------------------------------------------------------------- #
# build_form_schema (form-protocol §3)
# --------------------------------------------------------------------------- #

def test_build_form_schema_shape():
    schema = u1_form.build_form_schema(_spec(n_parts=3))
    assert schema["version"] == u1_form.FORM_SCHEMA_VERSION
    assert schema["text_fallback"].startswith("Decide all at once")
    fields = {f["id"]: f for f in schema["fields"]}
    assert fields["parts"]["type"] == "multi_select"
    assert fields["parts"]["default"] == "all"
    assert {o["id"] for o in fields["parts"]["options"]} == {"01_part1", "02_part2", "03_part3"}
    assert fields["tool"]["type"] == "single_select" and fields["tool"]["required"] is True
    assert fields["profile"]["options"][1] == {"id": 2, "label": "0.16 Optimal @Snapmaker U1 (0.4 nozzle)"}


def test_build_form_schema_single_part_has_no_parts_field():
    schema = u1_form.build_form_schema(_spec(n_parts=0))
    assert "parts" not in {f["id"] for f in schema["fields"]}
    assert schema["text_fallback"]  # text fallback always present


# --------------------------------------------------------------------------- #
# parse_answers_json (form-protocol §4) — must match the text parser
# --------------------------------------------------------------------------- #

def test_json_full_answer_by_ids():
    r = u1_form.parse_answers_json(
        {"parts": ["01_part1", "03_part3"], "orient": "auto", "tool": "T0",
         "material": "PLA", "profile": 2, "supports": "no-supports", "action": "start"},
        _spec(n_parts=3))
    assert r["ok"], r["errors"]
    v = r["values"]
    assert v["parts"] == [1, 3]         # ids normalized to internal indices
    assert v["orient"] == "auto" and v["tool"] == "T0" and v["material"] == "PLA"
    assert v["profile"]["idx"] == 2 and v["supports"] == "no-supports" and v["action"] == "start"


def test_json_parts_all_keyword():
    r = u1_form.parse_answers_json({"parts": "all", "tool": "T0", "material": "PLA", "profile": 1}, _spec(n_parts=4))
    assert r["ok"], r["errors"]
    assert r["values"]["parts"] == [1, 2, 3, 4]


def test_json_unknown_part_id_errors():
    r = u1_form.parse_answers_json({"parts": ["99_ghost"], "tool": "T0", "material": "PLA", "profile": 1}, _spec(n_parts=3))
    assert not r["ok"]
    assert any("unknown part id" in e for e in r["errors"])


def test_json_indices_in_parts_are_rejected_not_silently_accepted():
    # JSON path is id-only; an integer index is NOT a valid id -> error (no drift).
    r = u1_form.parse_answers_json({"parts": [1, 3], "tool": "T0", "material": "PLA", "profile": 1}, _spec(n_parts=3))
    assert not r["ok"]
    assert any("unknown part id" in e for e in r["errors"])


def test_json_missing_required_fail_loudly():
    r = u1_form.parse_answers_json({"parts": "all", "orient": "auto"}, _spec(n_parts=3))
    assert not r["ok"]
    j = " ".join(r["errors"])
    assert "tool" in j and "material" in j and "profile" in j


def test_json_defaults_applied():
    r = u1_form.parse_answers_json({"tool": "T0", "material": "PLA", "profile": 1}, _spec(n_parts=0))
    assert r["ok"], r["errors"]
    v = r["values"]
    assert v["orient"] == "as-authored" and v["supports"] == "no-supports" and v["action"] == "start"


def test_json_bad_tool_errors():
    spec = _spec(n_parts=0); spec["tools"] = ["T0", "T1"]
    r = u1_form.parse_answers_json({"tool": "T3", "material": "PLA", "profile": 1}, spec)
    assert not r["ok"]
    assert any("tool T3 not offered" in e for e in r["errors"])


def test_json_and_text_agree_on_same_answer():
    # The two intakes must produce the same validated decision set.
    spec = _spec(n_parts=3)
    text = u1_form.parse_answers("parts 1,3 | auto | T0 | PLA | profile 2 | no-supports | start", spec)
    js = u1_form.parse_answers_json(
        {"parts": ["01_part1", "03_part3"], "orient": "auto", "tool": "T0",
         "material": "PLA", "profile": 2, "supports": "no-supports", "action": "start"}, spec)
    assert text["values"] == js["values"]
