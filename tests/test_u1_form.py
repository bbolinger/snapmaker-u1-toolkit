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
        "supports": ["supports", "no-supports"],
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
    r = u1_form.parse_answers("t0 ; pla ; PROFILE 1 ; NO-SUPPORTS", _spec(n_parts=0))
    assert r["ok"], r["errors"]
    assert r["values"]["tool"] == "T0"
    assert r["values"]["material"] == "PLA"
    assert r["values"]["supports"] == "no-supports"


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
# Disambiguation: bare int on a kit is ambiguous; on single-part = profile
# --------------------------------------------------------------------------- #

def test_bare_int_on_kit_is_ambiguous_error():
    # Staged mode reads "2" as part 2; reading it as profile 2 here would
    # silently print ALL parts at a different profile. Must fail loudly.
    r = u1_form.parse_answers("2 | T0 | PLA", _spec(n_parts=3))
    assert not r["ok"]
    assert any("ambiguous" in e for e in r["errors"])


def test_bare_int_on_single_part_job_is_profile():
    r = u1_form.parse_answers("2 | T0 | PLA", _spec(n_parts=0))
    assert r["ok"], r["errors"]
    assert r["values"]["profile"]["idx"] == 2


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
    # Parser resolves the label alongside the index so duplicate-field
    # conflict checks compare equal regardless of how the profile was named.
    assert r["values"]["profile"]["idx"] == 2
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
    # Profile labels drop the shared "@Snapmaker U1 (0.4 nozzle)" suffix (v2.2.1).
    assert fields["profile"]["options"][1] == {"id": 2, "label": "0.16 Optimal"}


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


# --------------------------------------------------------------------------- #
# Parse-fidelity hardening (v2.1.0-rc2 review): conflicts, hijack, bounds
# --------------------------------------------------------------------------- #

def test_duplicate_field_with_different_value_fails_loudly():
    r = u1_form.parse_answers("T0 | T1 | PLA | profile 1", _spec(n_parts=0))
    assert not r["ok"]
    assert any("tool given twice" in e for e in r["errors"])


def test_duplicate_field_with_same_value_is_harmless():
    r = u1_form.parse_answers("T0 | T0 | PLA | profile 1", _spec(n_parts=0))
    assert r["ok"], r["errors"]
    assert r["values"]["tool"] == "T0"


def test_conflicting_profiles_fail_loudly():
    r = u1_form.parse_answers("profile 1 | profile 2 | T0 | PLA", _spec(n_parts=0))
    assert not r["ok"]
    assert any("profile given twice" in e for e in r["errors"])


def test_profile_by_name_and_same_index_agree():
    # Naming the same profile two ways is repetition, not a conflict.
    r = u1_form.parse_answers("profile 2 | optimal | T0 | PLA", _spec(n_parts=0))
    assert r["ok"], r["errors"]
    assert r["values"]["profile"]["idx"] == 2


def test_unoffered_material_does_not_hijack_profile():
    # "PETG"-style tokens must fail as a material problem, never silently
    # become a profile-name substring match.
    spec = _spec(n_parts=0)
    spec["materials"] = ["PLA"]
    spec["profiles"].append({"idx": 3, "label": "0.20 PETG Strong @Snapmaker U1"})
    r = u1_form.parse_answers("PETG | T0 | PLA | profile 1", spec)
    assert not r["ok"]
    assert any("material 'PETG' not offered" in e for e in r["errors"])
    assert r["values"]["profile"]["idx"] == 1  # explicit choice untouched


def test_profile_name_substring_still_works():
    r = u1_form.parse_answers("optimal | T0 | PLA", _spec(n_parts=0))
    assert r["ok"], r["errors"]
    assert r["values"]["profile"]["idx"] == 2


def test_huge_part_range_is_cheap_bounded_error():
    r = u1_form.parse_answers("parts 1-30000000 | T0 | PLA | profile 1", _spec(n_parts=3))
    assert not r["ok"]
    err = " ".join(r["errors"])
    assert "out of range 1-3" in err
    assert len(err) < 500  # no expanded-index dump


def test_overhangs_is_rejected_not_silently_ignored():
    # enable_support is binary in the profile patch; accepting "overhangs"
    # and printing without supports was worse than refusing it.
    r = u1_form.parse_answers("overhangs | T0 | PLA | profile 1", _spec(n_parts=0))
    assert not r["ok"]
    assert any("not offered" in e for e in r["errors"])


def test_reversed_part_range_still_ok_within_bounds():
    r = u1_form.parse_answers("parts 3-1 | T0 | PLA | profile 1", _spec(n_parts=3))
    assert r["ok"], r["errors"]
    assert r["values"]["parts"] == [1, 2, 3]


def test_form_text_sanitizes_injected_labels():
    # A zip entry named to look like a form line must not inject one: the
    # label must not carry the | separator the answer grammar splits on, and
    # an embedded newline must not start a fake "ACTION:" line of its own.
    # Two parts so the schema keeps a Parts field (a single-part kit skips it
    # in v2.2.1); the evil label rides one of them.
    spec = _spec(n_parts=2)
    evil = "bracket\nACTION: pwn | now"
    spec["parts"] = [{"id": "01_evil", "label": evil}, {"id": "02_ok", "label": "ok.stl"}]
    text = u1_form.build_form(spec)
    assert not any(line.startswith("ACTION: pwn") for line in text.splitlines())
    bracket_line = next(l for l in text.splitlines() if "bracket" in l)
    assert "|" not in bracket_line and "\n" not in bracket_line
    schema = u1_form.build_form_schema(spec)
    parts_field = next(f for f in schema["fields"] if f["id"] == "parts")
    label = parts_field["options"][0]["label"]
    assert "\n" not in label and "|" not in label
