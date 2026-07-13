"""End-to-end: a real slice must stamp the U1 machine AND the requested material.

Why this exists (2026-07-10, real Windows desktop): upstream Orca portable
sliced a U1 plate headlessly just fine, but loading the upstream PETG LEAF
profile directly made Orca silently fall back to PLA defaults
(filament_type=PLA, first_layer_temperature=200, bed 45). G-code existing is
NOT validation. This test slices through the toolkit's own call path
(real_orca_slice, which flattens the filament inheritance chain) and asserts
the metadata the upload gate keys on — via the gate's own parser — so a
resource-root or flattening regression on ANY platform turns the material
silently-wrong failure into a red test instead of a wrong print.

Runs only where a real Orca binary lives; skips cleanly elsewhere — same
mechanism as test_advanced_overrides_e2e (ORCA_SLICER_BIN env override / the
extracted-binary default path).

To run on a Windows desktop against portable Orca (Git Bash):

    export ORCA_SLICER_BIN='C:/path/to/orca242/orca-slicer.exe'
    python -m pytest tests/test_real_orca_u1_metadata_e2e.py -v

Fetch profiles first (tools/fetch_snapmaker_profiles.py) or the test skips.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from u1_orient import DEFAULT_ORCA, write_binary_stl
from u1_upload_gcode import parse_gcode_metadata
import u1_slice_workflow as wf

from test_advanced_overrides_e2e import _cube_tris

pytestmark = pytest.mark.skipif(
    not DEFAULT_ORCA.exists(),
    reason="real Orca binary not present in this environment",
)

_PROFILE = "0_20_standard_snapmaker_u1_0_4_nozzle"


def test_u1_petg_slice_stamps_machine_and_material(tmp_path):
    try:
        wf.profile_path(_PROFILE)
        wf.filament_path("PETG", nozzle="0.4")
    except RuntimeError as exc:
        pytest.skip(f"runtime profiles not fetched here ({exc})")

    out_dir = tmp_path / "slice"
    out_dir.mkdir()
    stl = tmp_path / "cube.stl"
    write_binary_stl(stl, _cube_tris(15.0), name="cube")

    res = wf.real_orca_slice(
        stl, out_dir / "cube.gcode", tool="T0", material="PETG",
        profile=_PROFILE,
    )

    gcode_path = Path(res["gcode"])
    assert gcode_path.exists() and gcode_path.stat().st_size > 0

    # The gate's own parser — if this test passes, the upload gate's
    # printer/material blockers pass for the same file.
    meta = parse_gcode_metadata(gcode_path)["metadata"]

    printer_id = meta.get("printer_settings_id", "")
    assert "snapmaker u1" in printer_id.lower(), (
        f"machine profile did not land: printer_settings_id={printer_id!r}"
    )

    # Mirror u1_upload_gcode's material blocker exactly (multi-tool gcode
    # carries a ;-joined list).
    filament_type = meta.get("filament_type", "")
    assert "PETG" in filament_type.upper().split(";"), (
        f"material fell back — filament_type={filament_type!r} (the PLA-default "
        f"fallback stamps 'PLA' here; see _flatten_filament_profile docstring)"
    )

    # Temperature sanity: the PLA fallback stamps nozzle 200 / bed 45. Real
    # PETG profiles run hotter on both. Loose bounds so profile tweaks don't
    # break the test, tight enough that a PLA fallback always fails it.
    nozzle = float(meta.get("first_layer_temperature", "0").split(";")[0] or 0)
    bed = float(meta.get("first_layer_bed_temperature", "0").split(";")[0] or 0)
    assert nozzle >= 220, f"first_layer_temperature={nozzle} is PLA-range, not PETG"
    assert bed >= 60, f"first_layer_bed_temperature={bed} is PLA-range, not PETG"
