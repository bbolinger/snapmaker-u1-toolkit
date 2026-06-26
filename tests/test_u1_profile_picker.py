"""Tests for scripts/u1_profile_picker.py.

Profile picker rewritten in v1.5.0 to scan multiple sources
(from-printer / user / snapmaker-stock) and annotate each profile
with `source` and `has_supports` (read from JSON's `enable_support`).
"""
from __future__ import annotations

import json
from pathlib import Path

from u1_profile_picker import list_profiles, profile_id, normalize_value, _read_supports_flag, _is_process_profile


def _write_process_json(path: Path, *, enable_support: str | None = None) -> None:
    """Helper — minimum-shape process profile fixture."""
    data: dict = {"type": "process", "name": path.stem}
    if enable_support is not None:
        data["enable_support"] = enable_support
    path.write_text(json.dumps(data))


# ---------- class_hint scoring ----------

def test_strength_hint_recommends_strength(tmp_path):
    _write_process_json(tmp_path / '0.20 Strength @Snapmaker U1 (0.4 nozzle).json')
    _write_process_json(tmp_path / '0.16 Optimal @Snapmaker U1 (0.4 nozzle).json')
    opts = list_profiles(tmp_path, class_hint='bracket holder')
    recommended = [o for o in opts if o.get('recommended')]
    assert len(recommended) == 1
    assert 'strength' in recommended[0]['label'].lower()


def test_cosmetic_hint_recommends_optimal(tmp_path):
    _write_process_json(tmp_path / '0.20 Strength @Snapmaker U1 (0.4 nozzle).json')
    _write_process_json(tmp_path / '0.16 Optimal @Snapmaker U1 (0.4 nozzle).json')
    opts = list_profiles(tmp_path, class_hint='cosmetic')
    recommended = [o for o in opts if o.get('recommended')]
    assert len(recommended) == 1
    assert 'optimal' in recommended[0]['label'].lower()


# ---------- profile_id slug normalization ----------

def test_profile_id_normalizes_snapmaker_stock_name():
    # `0.20 Strength @Snapmaker U1 (0.4 nozzle)` should become a shell-safe slug.
    result = profile_id(Path('0.20 Strength @Snapmaker U1 (0.4 nozzle).json'))
    assert ' ' not in result
    assert '@' not in result
    assert '(' not in result
    assert result.islower()


def test_profile_id_handles_extracted_gcode_stem():
    # Extracted profile names follow the gcode-stem convention.
    result = profile_id(Path('globe_light_PETG_5h56m_process.json'))
    assert result == 'globe_light_petg_5h56m_process'


# ---------- normalize_value ----------
# Used by both profile_id (file → value) AND profile_path (user input → value).
# These tests guarantee the two paths produce identical slugs.

def test_normalize_value_matches_profile_id_for_snapmaker_stock_name():
    stem = '0.20 Strength @Snapmaker U1 (0.4 nozzle)'
    assert normalize_value(stem) == profile_id(Path(f'{stem}.json'))


def test_normalize_value_idempotent_on_already_slug_form():
    # If the user passes the already-slugified value, it round-trips unchanged.
    slug = '0_20_strength_snapmaker_u1_0_4_nozzle'
    assert normalize_value(slug) == slug


def test_normalize_value_drops_special_chars_and_lowercases():
    assert normalize_value('Hello-World!') == 'hello_world'


# ---------- enable_support detection ----------

def test_has_supports_true_when_enable_support_is_1(tmp_path):
    _write_process_json(tmp_path / '0.20 Support W.json', enable_support='1')
    opts = list_profiles(tmp_path)
    assert opts and opts[0]['has_supports'] is True


def test_has_supports_false_when_enable_support_is_0(tmp_path):
    _write_process_json(tmp_path / '0.20 Standard.json', enable_support='0')
    opts = list_profiles(tmp_path)
    assert opts and opts[0]['has_supports'] is False


def test_has_supports_false_when_enable_support_missing(tmp_path):
    # Default: if the field is absent, fail-closed (don't claim supports).
    _write_process_json(tmp_path / '0.20 Plain.json')
    opts = list_profiles(tmp_path)
    assert opts and opts[0]['has_supports'] is False


# ---------- multi-source scanning ----------

def test_multi_source_scan_tags_each_profile_with_its_source(tmp_path):
    from_printer = tmp_path / 'from-printer'
    snapmaker_stock = tmp_path / 'snapmaker-stock'
    from_printer.mkdir()
    snapmaker_stock.mkdir()
    _write_process_json(from_printer / 'recent_print_process.json')
    _write_process_json(snapmaker_stock / '0.20 Standard.json')

    sources = (
        ('from-printer', from_printer),
        ('snapmaker-stock', snapmaker_stock),
    )
    opts = list_profiles(sources=sources)
    by_source = {o['source']: o for o in opts}
    assert set(by_source.keys()) == {'from-printer', 'snapmaker-stock'}


def test_multi_source_priority_first_source_wins_on_collision(tmp_path):
    # Same slug from two sources → first source (higher priority) keeps it.
    src_a = tmp_path / 'a'
    src_b = tmp_path / 'b'
    src_a.mkdir()
    src_b.mkdir()
    _write_process_json(src_a / '0.20 Strength.json')
    _write_process_json(src_b / '0.20 Strength.json')

    opts = list_profiles(sources=(('a', src_a), ('b', src_b)))
    assert len(opts) == 1
    assert opts[0]['source'] == 'a'


def test_filters_to_process_profiles_only_when_subdir_says_machine(tmp_path):
    # snapmaker-stock layout: machine/Snapmaker U1.json should be skipped
    # by the process-only filter.
    process_dir = tmp_path / 'process'
    machine_dir = tmp_path / 'machine'
    process_dir.mkdir()
    machine_dir.mkdir()
    _write_process_json(process_dir / '0.20 Standard.json')
    (machine_dir / 'Snapmaker U1.json').write_text(json.dumps({"type": "machine", "name": "U1"}))

    opts = list_profiles(tmp_path)
    assert len(opts) == 1
    assert opts[0]['label'] == '0.20 Standard'


# ---------- _read_supports_flag type coverage (#15) ----------
# Orca normally encodes enable_support as the string "1"/"0", but the helper
# accepts int and bool too defensively. These tests pin the type matrix.

def test_read_supports_flag_string_one(tmp_path):
    p = tmp_path / 'x.json'
    p.write_text(json.dumps({"enable_support": "1"}))
    assert _read_supports_flag(p) is True


def test_read_supports_flag_string_zero(tmp_path):
    p = tmp_path / 'x.json'
    p.write_text(json.dumps({"enable_support": "0"}))
    assert _read_supports_flag(p) is False


def test_read_supports_flag_string_with_whitespace(tmp_path):
    # Defensive: handle stray whitespace from upstream profile writers.
    p = tmp_path / 'x.json'
    p.write_text(json.dumps({"enable_support": "  1  "}))
    assert _read_supports_flag(p) is True


def test_read_supports_flag_int_one(tmp_path):
    p = tmp_path / 'x.json'
    p.write_text(json.dumps({"enable_support": 1}))
    assert _read_supports_flag(p) is True


def test_read_supports_flag_int_zero(tmp_path):
    p = tmp_path / 'x.json'
    p.write_text(json.dumps({"enable_support": 0}))
    assert _read_supports_flag(p) is False


def test_read_supports_flag_bool_true(tmp_path):
    p = tmp_path / 'x.json'
    p.write_text(json.dumps({"enable_support": True}))
    assert _read_supports_flag(p) is True


def test_read_supports_flag_missing_key(tmp_path):
    # Fail-closed: missing field → not enabled.
    p = tmp_path / 'x.json'
    p.write_text(json.dumps({"name": "no flag here"}))
    assert _read_supports_flag(p) is False


def test_read_supports_flag_malformed_json(tmp_path):
    # Don't crash on unreadable / corrupted profile JSON; treat as no supports.
    p = tmp_path / 'x.json'
    p.write_text('{not valid json')
    assert _read_supports_flag(p) is False


def test_read_supports_flag_missing_file(tmp_path):
    # File doesn't exist (race: deleted between list and read).
    assert _read_supports_flag(tmp_path / 'nonexistent.json') is False


# ---------- _is_process_profile JSON fallback (#15) ----------
# When the filename doesn't follow the _process / _filament / _machine
# convention, the helper reads the JSON `type` field.

def test_is_process_profile_via_subdir_name(tmp_path):
    pdir = tmp_path / 'process'
    pdir.mkdir()
    p = pdir / 'whatever.json'
    p.write_text('{}')  # subdir name alone is enough
    assert _is_process_profile(p) is True


def test_is_process_profile_falls_back_to_json_type_field(tmp_path):
    # No subdir hint, no name suffix → JSON inspection.
    p = tmp_path / 'random_name.json'
    p.write_text(json.dumps({"type": "process", "name": "x"}))
    assert _is_process_profile(p) is True


def test_is_process_profile_rejects_filament_type_in_json(tmp_path):
    p = tmp_path / 'random_name.json'
    p.write_text(json.dumps({"type": "filament", "name": "x"}))
    assert _is_process_profile(p) is False


def test_is_process_profile_rejects_filename_suffix(tmp_path):
    # *_filament.json and *_machine.json suffixes win immediately.
    p = tmp_path / 'something_filament.json'
    p.write_text(json.dumps({"type": "process"}))  # JSON type would lie but filename short-circuits
    assert _is_process_profile(p) is False


def test_is_process_profile_malformed_json_returns_false(tmp_path):
    p = tmp_path / 'mystery.json'
    p.write_text('{garbage')
    assert _is_process_profile(p) is False


# ---------- nozzle filter (v1.5.1 P3) ----------

def test_nozzle_filter_keeps_only_matching_nozzle_when_label_encodes_it(tmp_path):
    _write_process_json(tmp_path / '0.20 Strength @Snapmaker U1 (0.4 nozzle).json')
    _write_process_json(tmp_path / '0.06 High Quality @Snapmaker U1 (0.2 nozzle).json')
    _write_process_json(tmp_path / '0.30 Draft @Snapmaker U1 (0.6 nozzle).json')
    opts = list_profiles(tmp_path, nozzle='0.4')
    labels = [o['label'] for o in opts]
    assert all('(0.4 nozzle)' in l.lower() for l in labels)
    assert len(opts) == 1


def test_nozzle_filter_keeps_profiles_with_no_nozzle_token(tmp_path):
    # User-named profiles often don't encode nozzle. Don't drop them on filter.
    _write_process_json(tmp_path / 'my_custom_PETG_strong.json')
    _write_process_json(tmp_path / '0.06 High Quality @Snapmaker U1 (0.2 nozzle).json')
    opts = list_profiles(tmp_path, nozzle='0.4')
    labels = {o['label'] for o in opts}
    assert 'my_custom_PETG_strong' in labels
    assert '0.06 High Quality @Snapmaker U1 (0.2 nozzle)' not in labels


def test_nozzle_filter_matches_underscored_slug_form(tmp_path):
    # Some extracted profiles use 0_4_nozzle in the stem rather than (0.4 nozzle).
    _write_process_json(tmp_path / '0_20_strength_snapmaker_u1_0_4_nozzle.json')
    _write_process_json(tmp_path / '0_06_quality_snapmaker_u1_0_2_nozzle.json')
    opts = list_profiles(tmp_path, nozzle='0.4')
    assert len(opts) == 1
    assert '0_4_nozzle' in opts[0]['label']


def test_no_nozzle_arg_keeps_everything(tmp_path):
    _write_process_json(tmp_path / '0.20 Strength @Snapmaker U1 (0.4 nozzle).json')
    _write_process_json(tmp_path / '0.06 High Quality @Snapmaker U1 (0.2 nozzle).json')
    opts = list_profiles(tmp_path)
    assert len(opts) == 2
