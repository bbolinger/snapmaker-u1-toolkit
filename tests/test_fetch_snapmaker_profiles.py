"""Unit tests for tools/fetch_snapmaker_profiles.py.

Only covers the filter logic — keeps real network out of the test suite.
End-to-end download verification belongs in #18 (live test on real hardware
context, not alpine CI).
"""
from __future__ import annotations

import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import fetch_snapmaker_profiles as fetcher  # noqa: E402


# ---------- machine ----------

def test_is_u1_machine_accepts_u1():
    assert fetcher.is_u1_machine("Snapmaker U1 (0.4 nozzle).json")
    assert fetcher.is_u1_machine("Snapmaker U1.json")
    assert fetcher.is_u1_machine("Snapmaker U1 (0.2 nozzle).json")


def test_is_u1_machine_rejects_other_printers():
    assert not fetcher.is_u1_machine("Snapmaker A250 (0.4 nozzle).json")
    assert not fetcher.is_u1_machine("Snapmaker J1 (0.4 nozzle).json")
    assert not fetcher.is_u1_machine("Snapmaker Artisan.json")


def test_is_u1_machine_rejects_dev_detritus():
    assert not fetcher.is_u1_machine("Snapmaker U1 (0.4 nozzle) copy.json")
    assert not fetcher.is_u1_machine("Snapmaker U1 (0.4 nozzle)_old.json")


# ---------- process ----------

def test_is_u1_process_accepts_u1_variants():
    assert fetcher.is_u1_process("0.20 Strength @Snapmaker U1 (0.4 nozzle).json")
    assert fetcher.is_u1_process("0.20 Support W @Snapmaker U1 (0.4 nozzle).json")
    assert fetcher.is_u1_process("0.16 Optimal @Snapmaker U1 (0.4 nozzle).json")
    assert fetcher.is_u1_process("0.06 High Quality @Snapmaker U1 (0.2 nozzle).json")


def test_is_u1_process_rejects_other_printers():
    assert not fetcher.is_u1_process("0.20 Standard @Snapmaker (0.4 nozzle).json")
    assert not fetcher.is_u1_process("0.20 Standard @Snapmaker Artisan (0.4 nozzle).json")
    assert not fetcher.is_u1_process("0.20 Standard @Snapmaker J1 (0.4 nozzle).json")


def test_is_u1_process_rejects_copies_and_old():
    assert not fetcher.is_u1_process("0.20 Standard @Snapmaker U1 (0.4 nozzle)_old.json")
    assert not fetcher.is_u1_process("0.10 Standard @Snapmaker U1 (0.2 nozzle) copy.json")


# ---------- filament ----------

def test_is_u1_filament_accepts_u1_tuned():
    assert fetcher.is_u1_filament("Generic PETG @U1 0.2 nozzle.json")
    assert fetcher.is_u1_filament("Generic PLA @U1 0.6 nozzle.json")
    assert fetcher.is_u1_filament("Generic ABS @U1 0.8 nozzle.json")


def test_is_u1_filament_accepts_base_for_inheritance():
    # Orca filament profiles use `inherits` chains; without the @base files
    # the U1-specific filament profiles can't be resolved.
    assert fetcher.is_u1_filament("Generic PETG @base.json")
    assert fetcher.is_u1_filament("Generic PLA @base2.json")


def test_is_u1_filament_rejects_root_generic():
    # `Generic PETG.json` (no @ tag) is the OrcaSlicer-wide root — not
    # Snapmaker-specific. Skip.
    assert not fetcher.is_u1_filament("Generic PETG.json")
    assert not fetcher.is_u1_filament("Generic PLA.json")


def test_is_u1_filament_rejects_copies_and_old():
    assert not fetcher.is_u1_filament("Generic PETG @U1 0.2 nozzle copy.json")
    assert not fetcher.is_u1_filament("Generic PETG @U1 0.2 nozzle_old.json")
