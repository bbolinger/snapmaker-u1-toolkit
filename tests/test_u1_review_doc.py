"""Tests for scripts/u1_review_doc.py — the pre-print flight plan (v2.2).

The doc is a trust artifact with two hard properties to pin:
(1) settings come from the gcode's own config block (ground truth), and
(2) generation is informational — the workflow tests separately prove a
doc failure never blocks the print flow.
"""
from __future__ import annotations

import json
from pathlib import Path

import u1_review_doc


_GCODE = """;FLAVOR:Klipper
; estimated printing time (normal mode) = 1h 14m 9s
; total filament used [g] = 18.42
G28
T1
G1 X10 Y10 E1.5
; CONFIG_BLOCK_START
; layer_height = 0.2
; wall_loops = 3
; sparse_infill_density = 15%
; sparse_infill_pattern = gyroid
; nozzle_temperature = 220,220
; textured_plate_temp = 65,65
; enable_support = 1
; brim_type = no_brim
; layer_height = 999
; CONFIG_BLOCK_END
"""


def _gcode(tmp_path: Path) -> Path:
    p = tmp_path / "plate_1.gcode"
    p.write_text(_GCODE)
    return p


# --------------------------------------------------------------------------- #
# parse_gcode_config
# --------------------------------------------------------------------------- #

def test_parse_config_block_prefers_markers_and_first_value_wins(tmp_path):
    cfg = u1_review_doc.parse_gcode_config(_gcode(tmp_path))
    assert cfg["layer_height"] == "0.2"  # first occurrence, not the 999 dup
    assert cfg["sparse_infill_pattern"] == "gyroid"
    assert cfg["enable_support"] == "1"
    # header comment lines OUTSIDE the block are not part of the config
    assert "estimated printing time (normal mode)" not in cfg


def test_parse_config_falls_back_without_markers(tmp_path):
    p = tmp_path / "old.gcode"
    p.write_text("; layer_height = 0.28\nG28\n; wall_loops = 2\n")
    cfg = u1_review_doc.parse_gcode_config(p)
    assert cfg["layer_height"] == "0.28"
    assert cfg["wall_loops"] == "2"


# --------------------------------------------------------------------------- #
# generate
# --------------------------------------------------------------------------- #

def _plates(tmp_path):
    return [{
        "plate_idx": 1,
        "gcode_path": str(_gcode(tmp_path)),
        "printer_storage_filename": "kit_plate1.gcode",
        "gcode_hash": "sha256:" + "ab" * 32,
        "metadata": {"estimated printing time (normal mode)": "1h 14m 9s",
                     "total filament used [g]": "18.42"},
        "partition_parts": ["bracket.stl", "cap.stl"],
    }]


def test_generate_writes_bound_reviewable_doc(tmp_path):
    doc = u1_review_doc.generate(
        "u1_2026_0702_abc123", tmp_path / "out", _plates(tmp_path),
        state={"request_revision": 3},
        decisions={"tool": "T1", "material": "PETG",
                   "profile": "0_20_strength", "supports": "supports"},
        overrides=["supports forced ON by your answer"],
        operator="test:unit",
    )
    text = Path(doc).read_text()
    # moat binding in the header
    assert "u1_2026_0702_abc123" in text
    assert "revision **3**" in text
    assert "abab" in text  # gcode hash surfaced
    # ground-truth settings from the gcode config block
    assert "gyroid" in text and "0.2" in text
    # operator decisions echoed
    assert "T1" in text and "PETG" in text
    assert "supports forced ON" in text
    # estimates surfaced
    assert "1h 14m 9s" in text and "18.42 g" in text
    # parts listed for the plate
    assert "bracket.stl" in text
    # doc never leaks tokens/nonces vocabulary
    assert "approval_token" not in text and "nonce" not in text


def test_generate_survives_missing_gcode(tmp_path):
    plates = _plates(tmp_path)
    plates[0]["gcode_path"] = str(tmp_path / "nope.gcode")
    doc = u1_review_doc.generate("u1_2026_0702_abc123", tmp_path / "out",
                                 plates, state={}, decisions={"tool": "T0"})
    text = Path(doc).read_text()
    assert "settings table" in text  # graceful placeholder, not a crash
    assert "T0" in text


def test_generate_audits_doc_hash(tmp_path, monkeypatch):
    rows = []
    import u1_audit
    monkeypatch.setattr(u1_audit, "append",
                        lambda rid, event, **kw: rows.append((rid, event, kw)))
    u1_review_doc.generate("u1_2026_0702_abc123", tmp_path / "out",
                           _plates(tmp_path), state={"request_revision": 2})
    assert rows and rows[0][1] == "review_doc_generated"
    kw = rows[0][2]
    assert kw["doc_sha256"].startswith("sha256:")
    assert kw["request_revision"] == 2


def test_deviation_from_preset_is_marked(tmp_path):
    # The tweaked-and-forgotten value is the classic trust-killer: when the
    # gcode differs from the chosen preset, the table says so inline with
    # the preset's own number.
    doc = u1_review_doc.generate(
        "u1_2026_0702_abc123", tmp_path / "out", _plates(tmp_path),
        state={}, decisions={"tool": "T1"},
        reference={"nozzle_temperature": "240",     # gcode says 220,220
                   "layer_height": "0.2",           # matches → no marker
                   "sparse_infill_pattern": "gyroid"})
    text = Path(doc).read_text()
    assert "⚠" in text and "preset: `240`" in text
    # matching values carry no marker
    assert "| Layer height (mm) | `0.2` |" in text
    assert "1 setting(s) differ from the chosen preset" in text


def test_no_deviations_says_so_explicitly(tmp_path):
    doc = u1_review_doc.generate(
        "u1_2026_0702_abc123", tmp_path / "out", _plates(tmp_path),
        state={},
        reference={"layer_height": "0.2", "sparse_infill_pattern": "gyroid",
                   "nozzle_temperature": "220", "enable_support": "1"})
    text = Path(doc).read_text()
    assert "No deviations detected" in text.replace("no \ndeviations", "") or \
           "no \ndeviations detected" in text.lower() or \
           "deviations detected" in text.lower()
    assert "⚠" not in text


def test_norm_collapses_per_filament_lists():
    assert u1_review_doc._norm(["240", "240"]) == "240"
    assert u1_review_doc._norm("240,240") == "240"
    assert u1_review_doc._norm(["240", "230"]) == "240,230"
    assert u1_review_doc._norm(" 0.2 ") == "0.2"


def test_material_double_check_note_present(tmp_path):
    doc = u1_review_doc.generate("u1_2026_0702_abc123", tmp_path / "out",
                                 _plates(tmp_path), state={})
    assert "PHYSICALLY loaded" in Path(doc).read_text()


def test_multi_plate_doc_says_only_plate1_is_gated(tmp_path):
    plates = _plates(tmp_path)
    plates.append({"plate_idx": 2, "gcode_path": plates[0]["gcode_path"],
                   "printer_storage_filename": "kit_plate2.gcode",
                   "gcode_hash": "sha256:" + "cd" * 32, "metadata": {}})
    doc = u1_review_doc.generate("u1_2026_0702_abc123", tmp_path / "out",
                                 plates, state={})
    text = Path(doc).read_text()
    assert "Only **plate 1**" in text
    assert "kit_plate2.gcode" in text


# --------------------------------------------------------------------------- #
# workflow integration (legacy path harness — cheap and hermetic)
# --------------------------------------------------------------------------- #

def test_kit_commit_emits_review_doc_and_never_blocks(tmp_path, capsys, monkeypatch):
    import zipfile
    import numpy as np
    from types import SimpleNamespace
    import u1_kit_workflow as kw
    from u1_orient import write_binary_stl

    def _cube(path, s):
        v = np.array([[0, 0, 0], [s, 0, 0], [s, s, 0], [0, s, 0],
                      [0, 0, s], [s, 0, s], [s, s, s], [0, s, s]], dtype=np.float32)
        faces = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
                 (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
        write_binary_stl(path, np.array([[v[a], v[b], v[c]] for a, b, c in faces],
                                        dtype=np.float32))
        return path

    zp = tmp_path / "kit.zip"
    with zipfile.ZipFile(zp, "w") as z:
        for i in range(2):
            z.write(_cube(tmp_path / f"p{i}.stl", 20 + i), f"p{i}.stl")

    monkeypatch.setattr(kw, "list_profiles", lambda nozzle=None: [
        {"value": "0_20_standard", "label": "0.20 Standard @Snapmaker U1 (0.4 nozzle)"}])

    def fake_arrange(paths, out_dir, **kwargs):
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

    args = SimpleNamespace(model=str(zp), json_events=True,
                           form_answers="all | T0 | PLA | profile 1 | no-supports | upload-only",
                           form_answers_json=None, request_id=None, fresh=False,
                           operator="test:unit", nozzle="0.4", out_dir=None,
                           live_upload=True, on_collision=None)
    res = kw.run_kit_workflow(args)
    assert res["phase"] == "complete"
    events = [json.loads(l) for l in capsys.readouterr().out.splitlines()
              if l.strip().startswith("{")]
    rd = [e for e in events if e.get("stage") == "review_doc"]
    assert rd, "review_doc event must be emitted with the readiness card"
    doc = Path(rd[0]["path"])
    assert doc.is_file()
    assert "gyroid" in doc.read_text()
    # readiness card carries the path too
    card = next(e for e in events if e.get("stage") == "kit_readiness_card")
    assert card.get("review_doc_path") == str(doc)


def test_kit_commit_survives_review_doc_failure(tmp_path, capsys, monkeypatch):
    # The doc is informational: if generation explodes, the flow completes
    # anyway and the failure is audited, not raised.
    import u1_review_doc as rd_mod
    monkeypatch.setattr(rd_mod, "generate",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    # re-run the happy-path setup with the broken generator
    try:
        test_kit_commit_emits_review_doc_and_never_blocks(tmp_path, capsys, monkeypatch)
        raised = False
    except AssertionError as exc:
        # the ONLY acceptable failure is the review_doc assertion — the
        # workflow itself must have completed (phase == complete asserted
        # before the review_doc check)
        raised = "review_doc event must be emitted" in str(exc)
    assert raised, "workflow must complete cleanly with the doc generator broken"
