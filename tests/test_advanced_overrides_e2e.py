"""End-to-end: advanced overrides must survive the real slicer.

The override chain (form -> apply_profile_overrides -> temp process JSON)
has schema/parsing/patching tests, but none of them prove OrcaSlicer
actually ACCEPTS the patched values — a recognized-looking but invalid
value would be silently ignored at slice time and nobody would know. This
slices a small cube through the same call path the kit workflow uses
(apply_supports_override -> apply_profile_overrides -> real_orca_slice)
with every overridable key set to a non-default value, then asserts each
`; key = value` line lands in the gcode's embedded config block.

Runs only where the extracted Orca binary lives (the runtime box). Skips
cleanly everywhere else — same mechanism as the other real-slicer tests
(ORCA_SLICER_BIN env override / the extracted-binary default path).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from u1_orient import DEFAULT_ORCA, write_binary_stl
import u1_slice_workflow as wf

pytestmark = pytest.mark.skipif(
    not DEFAULT_ORCA.exists(),
    reason="extracted Orca binary not present in this environment",
)

# The stock preset the runtime picker ships; slug per u1_profile_picker.
_PROFILE = "0_20_standard_snapmaker_u1_0_4_nozzle"

# Every key the form's Advanced screen can override, each set to a value
# that differs from the 0.20 Standard defaults. Values are Orca's own
# formats, exactly as u1_form.ADVANCED_FIELDS maps them.
OVERRIDES = {
    "sparse_infill_density": "30%",
    "sparse_infill_pattern": "gyroid",
    "wall_loops": "3",
    "top_shell_layers": "5",
    "bottom_shell_layers": "4",
    "only_one_wall_top": "1",
    "brim_type": "auto_brim",
    "raft_layers": "3",
    "fuzzy_skin": "external",
    "support_type": "tree(auto)",
}


def test_overrides_cover_every_advertised_key():
    """A new ADVANCED_OVERRIDE_KEYS entry must be added here too, or the
    e2e run below silently stops covering it."""
    assert set(OVERRIDES) == set(wf.ADVANCED_OVERRIDE_KEYS)


def _cube_tris(s: float) -> np.ndarray:
    """12-triangle axis-aligned cube spanning [0, s]^3."""
    v = np.array(
        [[0, 0, 0], [s, 0, 0], [s, s, 0], [0, s, 0],
         [0, 0, s], [s, 0, s], [s, s, s], [0, s, s]],
        dtype=np.float32,
    )
    faces = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7),
             (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return np.array([[v[a], v[b], v[c]] for a, b, c in faces], dtype=np.float32)


def _gcode_config(gcode_text: str) -> dict[str, str]:
    """Parse Orca's embedded config block (`; key = value` comments)."""
    out: dict[str, str] = {}
    for line in gcode_text.splitlines():
        if line.startswith("; ") and " = " in line:
            key, _, value = line[2:].partition(" = ")
            out[key.strip()] = value.strip()
    return out


def test_every_override_lands_in_sliced_gcode_config_block(tmp_path):
    try:
        process = wf.profile_path(_PROFILE)
        wf.filament_path("PETG", nozzle="0.4")
    except RuntimeError as exc:
        pytest.skip(f"runtime profiles not fetched here ({exc})")

    out_dir = tmp_path / "slice"
    out_dir.mkdir()
    stl = tmp_path / "cube.stl"
    write_binary_stl(stl, _cube_tris(15.0), name="cube")

    # Same composition order as the kit workflow's commit path: the
    # Supports answer patches first, the Advanced overrides go on top.
    # Supports ON so support_type is actually consumed, not just carried.
    process = wf.apply_supports_override(process, True, out_dir)
    process = wf.apply_profile_overrides(process, dict(OVERRIDES), out_dir)

    res = wf.real_orca_slice(
        stl, out_dir / "cube.gcode", tool="T0", material="PETG",
        profile=_PROFILE, process_path_override=process,
    )

    gcode_path = Path(res["gcode"])
    config = _gcode_config(gcode_path.read_text(errors="replace"))
    assert config, f"no `; key = value` config block found in {gcode_path}"

    for key, expected in OVERRIDES.items():
        assert config.get(key) == expected, (
            f"{key}: override sent {expected!r} but the sliced gcode's config "
            f"block says {config.get(key)!r} — Orca dropped or rewrote it"
        )
    # The supports patch beneath the overrides must survive too, or
    # support_type above proves nothing.
    assert config.get("enable_support") == "1"
