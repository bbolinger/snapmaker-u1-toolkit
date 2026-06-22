"""Verify the bundled Snapmaker U1 machine profile is a standalone (no
unresolved inheritance) and carries every field a slicer needs to anchor
the community process + filament profiles.

If this test ever fails after re-flattening from a new upstream Orca
release, regenerate the bundled file and re-check field coverage."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
MACHINE_PROFILE = REPO_ROOT / "profiles" / "machine" / "snapmaker_u1_0_4_nozzle.json"


def _profile() -> dict:
    return json.loads(MACHINE_PROFILE.read_text(encoding="utf-8"))


def test_bundled_machine_profile_exists():
    assert MACHINE_PROFILE.exists(), \
        f"machine profile missing at {MACHINE_PROFILE}"
    assert MACHINE_PROFILE.stat().st_size > 1000, \
        "machine profile suspiciously small — flatten likely produced an empty/broken file"


def test_machine_profile_is_valid_json():
    """Parse must not raise."""
    _profile()


def test_machine_profile_is_standalone_no_inherits():
    """Flattened standalone — must NOT carry an 'inherits' field, since
    Orca CLI can't resolve named parents reliably across platforms."""
    p = _profile()
    assert "inherits" not in p or p["inherits"] == "", \
        f"machine profile must be standalone; found inherits={p.get('inherits')!r}"


def test_machine_profile_identifies_as_snapmaker_u1():
    p = _profile()
    assert p.get("type") == "machine"
    assert p.get("printer_model") == "Snapmaker U1"
    assert p.get("printer_variant") == "0.4"


def test_machine_profile_has_required_slicing_fields():
    """Every field a slicer needs to actually emit valid G-code from the
    paired community process + filament profiles. If any are missing the
    headless slice will fail with a confusing validation error."""
    p = _profile()
    required = [
        "printer_model", "printer_variant", "nozzle_diameter",
        "gcode_flavor",                # 'klipper' for the U1
        "extruder_offset",             # multi-tool offsets
        "machine_max_acceleration_x", "machine_max_acceleration_y",
        "machine_max_acceleration_z", "machine_max_acceleration_e",
        "machine_max_speed_x", "machine_max_speed_y", "machine_max_speed_z",
        "printable_area",              # bed bounds
        "printable_height",            # Z max
    ]
    missing = [k for k in required if k not in p]
    assert not missing, f"machine profile missing required fields: {missing}"


def test_machine_profile_targets_klipper_gcode_flavor():
    """The U1 runs Klipper under the hood — Snapmaker just doesn't tell you
    that on the box. If this flips to anything else, slices won't run."""
    p = _profile()
    assert p["gcode_flavor"] == "klipper", \
        f"U1 uses Klipper firmware; got gcode_flavor={p['gcode_flavor']!r}"


def test_machine_profile_has_four_extruders():
    """U1 is a 4-tool toolchanger. All extruder-related arrays should have 4 entries."""
    p = _profile()
    eo = p.get("extruder_offset")
    assert isinstance(eo, list) and len(eo) == 4, \
        f"expected 4 extruder offsets, got {len(eo) if isinstance(eo, list) else eo!r}"


def test_machine_profile_marked_as_user_for_cli_loading():
    """OrcaSlicer's CLI distinguishes 'system' (vendor-shipped) from 'user'
    profiles. A user-shipped flattened copy must declare 'from'='user' so
    it loads cleanly via --load-settings without conflicting with the
    vendor copy in the operator's Orca install."""
    p = _profile()
    assert p.get("from") == "user"
    assert p.get("instantiation") == "true"


def test_notice_file_present():
    """Attribution NOTICE must ship alongside the profile so consumers know
    where the data came from + the AGPL-3.0 lineage."""
    notice = MACHINE_PROFILE.parent / "NOTICE"
    assert notice.exists()
    text = notice.read_text()
    assert "OrcaSlicer" in text
    assert "AGPL-3.0" in text
    assert "Snapmaker" in text
