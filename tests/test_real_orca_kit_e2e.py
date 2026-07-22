"""End-to-end: drive u1_kit_workflow through a REAL Orca slice.

The kit-of-one unification made u1_slice_workflow a dispatcher, so the two old
slice_workflow integration tests now only assert the dispatch. This restores a
full-chain check at the CURRENT entry point: run_kit_workflow analyzes a single
STL, slices it with real OrcaSlicer, and produces a gated plate, and the
resulting gcode carries the machine + material the upload gate keys on. Only the
Moonraker upload is mocked (no printer contact).

Runs where a real Orca binary exists (inside the Hermes runtime); skips in the
dev container (Alpine/musl can't exec the glibc binary); same gate as
test_real_orca_u1_metadata_e2e. LOCAL test (not on the public branch): it needs
the runtime's Orca + fetched stock profiles.
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from u1_orient import DEFAULT_ORCA, write_binary_stl
from u1_upload_gcode import parse_gcode_metadata
import u1_kit_workflow as kw
import u1_request

from test_advanced_overrides_e2e import _cube_tris

pytestmark = pytest.mark.skipif(
    not DEFAULT_ORCA.exists(),
    reason="real Orca binary not present in this environment",
)


def _single_stl_zip(tmp_path: Path) -> Path:
    stl = tmp_path / "cube.stl"
    write_binary_stl(stl, _cube_tris(15.0), name="cube")
    zp = tmp_path / "one.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.write(stl, "cube.stl")
    return zp


def _args(model, out_dir, **over):
    base = dict(model=str(model), json_events=True, form_answers=None,
                form_answers_json=None, request_id=None, fresh=True,
                operator="test:unit", nozzle="0.4", out_dir=str(out_dir),
                live_upload=False, on_collision=None, no_live_material=True)
    base.update(over)
    return SimpleNamespace(**base)


def test_kit_workflow_real_orca_slice_produces_gated_gcode(tmp_path, monkeypatch):
    try:
        kw.profile_path("0_20_standard_snapmaker_u1_0_4_nozzle")
    except Exception as exc:
        pytest.skip(f"runtime profiles not fetched here ({exc})")

    # Mock ONLY the Moonraker upload; no printer contact. Real slice runs.
    monkeypatch.setattr(kw, "_real_upload", lambda gcode, on_collision=None,
                        material=None: {"uploaded_filename": Path(gcode).name,
                                        "moonraker_upload_ok": True,
                                        "returncode": 0})

    zp = _single_stl_zip(tmp_path)
    res = kw.run_kit_workflow(_args(
        zp, tmp_path / "out",
        form_answers="all | T0 | PETG | 0.20 Standard | no-supports | upload-only",
    ))

    rid = res.get("request_id")
    assert rid, f"no request_id in result: {res}"
    req = u1_request.read_request(rid)
    plates = req.get("plates") or []
    assert plates, f"no plates recorded; phase={res.get('phase')}, res={res}"

    plate = Path(plates[0]["gcode_path"])
    assert plate.exists() and plate.stat().st_size > 0, "real gcode not produced"

    # The gate's own parser: if these pass, the upload gate's printer/material
    # blockers pass for this file.
    meta = parse_gcode_metadata(plate)["metadata"]
    assert "snapmaker u1" in meta.get("printer_settings_id", "").lower(), meta
    assert "PETG" in meta.get("filament_type", "").upper(), meta
