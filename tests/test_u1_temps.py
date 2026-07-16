"""Temperature envelopes for the bed/nozzle overrides (Track C).

Every material's bounds are sourced (Snapmaker on-box presets + published
guides + U1 hardware spec). These tests pin the sourced numbers and the two
load-bearing safety properties: nothing exceeds the U1 hardware caps, and the
bed has NO minimum (a cool/cold plate or bed-off must never be blocked).
"""
from __future__ import annotations

import pytest

import u1_temps as t


def test_nozzle_ranges_match_sourced_table():
    assert t.nozzle_range("PLA") == (190, 240)
    assert t.nozzle_range("PETG") == (230, 270)
    assert t.nozzle_range("ABS") == (230, 280)
    assert t.nozzle_range("ASA") == (240, 280)
    assert t.nozzle_range("TPU") == (200, 250)
    assert t.nozzle_range("PLA-CF") == (200, 250)
    assert t.nozzle_range("PETG-CF") == (235, 275)


def test_bed_ranges_have_no_minimum_and_sourced_max():
    # cold plate / bed-off: the MIN is always 0, for every material
    for m in ("PLA", "PETG", "ABS", "ASA", "TPU", "PLA-CF", "PETG-CF"):
        assert t.bed_range(m)[0] == 0, f"{m} bed must allow off (min 0)"
    assert t.bed_range("PLA")[1] == 70
    assert t.bed_range("PETG")[1] == 90
    assert t.bed_range("ABS")[1] == 110
    assert t.bed_range("ASA")[1] == 110
    assert t.bed_range("TPU")[1] == 60


def test_nothing_exceeds_u1_hardware_caps():
    for m in ("PLA", "PETG", "ABS", "ASA", "TPU", "PLA-CF", "PETG-CF", "MYSTERY"):
        assert t.nozzle_range(m)[1] <= t.HOTEND_MAX_C == 300
        assert t.bed_range(m)[1] <= t.BED_MAX_C == 110
        assert all(v <= 300 for v in t.offered_nozzle(m))
        assert all(v <= 110 for v in t.offered_bed(m))


def test_unknown_material_gets_safe_fallback():
    lo, hi = t.nozzle_range("SOME-EXOTIC-PA-CF")
    assert lo == 170 and hi == 300           # melt floor .. hotend cap
    assert t.bed_range("SOME-EXOTIC-PA-CF") == (0, 110)


def test_clamp_pins_to_the_envelope():
    assert t.clamp_nozzle("PLA", 999) == 240   # above PLA max
    assert t.clamp_nozzle("PLA", 100) == 190   # below PLA min
    assert t.clamp_nozzle("PLA", 210) == 210   # in range
    assert t.clamp_bed("PLA", 999) == 70       # above PLA bed max
    assert t.clamp_bed("PETG", 0) == 0         # bed off always ok
    assert t.clamp_nozzle("ABS", 350) == 280   # never past a material max
    # even a wild ABS request can't beat the 300 hotend cap via fallback path
    assert t.clamp_nozzle("MYSTERY", 500) == 300


def test_in_range_predicates():
    assert t.nozzle_in_range("PETG", 250)
    assert not t.nozzle_in_range("PETG", 300)
    assert t.bed_in_range("TPU", 0)            # off
    assert t.bed_in_range("TPU", 60)
    assert not t.bed_in_range("TPU", 80)       # above TPU bed max


def test_offered_values_are_in_range_and_include_off_for_bed():
    for m in ("PLA", "PETG", "ABS", "ASA", "TPU", "PLA-CF", "PETG-CF"):
        nlo, nhi = t.nozzle_range(m)
        noz = t.offered_nozzle(m)
        assert noz and all(nlo <= v <= nhi for v in noz)
        assert nhi in noz                       # top of range reachable
        bed = t.offered_bed(m)
        assert 0 in bed                         # bed-off offered (cold plate)
        _blo, bhi = t.bed_range(m)
        assert all(0 <= v <= bhi for v in bed)
        assert bhi in bed
