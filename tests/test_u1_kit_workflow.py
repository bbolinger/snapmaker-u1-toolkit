"""Tests for scripts/u1_kit_workflow.py — kit orchestrator (Option 2 seam).

External deps (Orca arrange-slice, Moonraker upload, profile resolution) are
mocked so the orchestration logic is tested hermetically. A live end-to-end run
against the real binary is done separately in hermes-agent-stack.
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import u1_kit_workflow as kw
from u1_orient import write_binary_stl


def _cube(path: Path, s: float) -> Path:
    v = np.array([[0, 0, 0], [s, 0, 0], [s, s, 0], [0, s, 0],
                  [0, 0, s], [s, 0, s], [s, s, s], [0, s, s]], dtype=np.float32)
    faces = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    write_binary_stl(path, np.array([[v[a], v[b], v[c]] for a, b, c in faces], dtype=np.float32))
    return path


def _kit_zip(tmp_path: Path, n=3) -> Path:
    zp = tmp_path / "kit.zip"
    with zipfile.ZipFile(zp, "w") as z:
        for i in range(n):
            stl = _cube(tmp_path / f"part{i}.stl", 20 + i)
            z.write(stl, f"part{i}.stl")
    return zp


def _args(model, **kw_):
    base = dict(model=str(model), json_events=True, form_answers=None, form_answers_json=None, request_id=None,
                fresh=False, operator="test:unit", nozzle="0.4", out_dir=None,
                live_upload=False, on_collision=None)
    base.update(kw_)
    return SimpleNamespace(**base)


@pytest.fixture
def fake_profiles(monkeypatch):
    monkeypatch.setattr(kw, "list_profiles", lambda nozzle=None: [
        {"value": "0_20_standard", "label": "0.20 Standard @Snapmaker U1 (0.4 nozzle)"},
        {"value": "0_16_optimal", "label": "0.16 Optimal @Snapmaker U1 (0.4 nozzle)"},
    ])


@pytest.fixture
def fake_slice_upload(monkeypatch):
    """Mock arrange-slice (writes plate files) + upload + profile resolution."""
    def fake_arrange(paths, out_dir, *, tool, material, profile, nozzle,
                     auto_orient, allow_rotations, process_path_override=None):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        plates = []
        for i in (1, 2):  # pretend overflow -> 2 plates
            g = out_dir / f"plate_{i}.gcode"
            g.write_text(f"; plate {i}\nT0\n")
            plates.append({"plate_idx": i, "gcode_path": str(g),
                           "gcode_hash": f"sha256:plate{i}", "metadata": {}})
        return {"plate_count": 2, "plates": plates, "cmd": ["orca"]}

    monkeypatch.setattr(kw.u1_arrange, "arrange_slice", fake_arrange)
    monkeypatch.setattr(kw, "profile_path", lambda slug: Path("/tmp/process.json"))
    monkeypatch.setattr(kw, "apply_supports_override", lambda p, en, od: Path("/tmp/process_ovr.json"))
    uploads = {"calls": []}

    def fake_upload(gcode, on_collision=None):
        uploads["calls"].append(Path(gcode).name)
        return {"uploaded_filename": Path(gcode).name, "moonraker_upload_ok": True}

    monkeypatch.setattr(kw, "_real_upload", fake_upload)
    return uploads


# --------------------------------------------------------------------------- #
# ANALYSIS + DECISION
# --------------------------------------------------------------------------- #

def test_no_form_answers_emits_form(tmp_path, fake_profiles):
    res = kw.run_kit_workflow(_args(_kit_zip(tmp_path, 3)))
    assert res["phase"] == "awaiting_form"
    rid = res["request_id"]
    # kit persisted
    req = __import__("u1_request").read_request(rid)
    assert req["kit"]["part_count"] == 3


def test_bad_form_answers_rejected(tmp_path, fake_profiles):
    res = kw.run_kit_workflow(_args(_kit_zip(tmp_path, 3), form_answers="gronk | flim"))
    assert res["phase"] == "form_rejected"
    assert res["errors"]


def test_missing_required_field_rejected(tmp_path, fake_profiles):
    # no tool/material/profile
    res = kw.run_kit_workflow(_args(_kit_zip(tmp_path, 2), form_answers="auto | no-supports"))
    assert res["phase"] == "form_rejected"


# --------------------------------------------------------------------------- #
# COMMIT
# --------------------------------------------------------------------------- #

def test_commit_happy_path_gates_plate_1(tmp_path, fake_profiles, fake_slice_upload):
    res = kw.run_kit_workflow(_args(
        _kit_zip(tmp_path, 3),
        form_answers="parts 1,3 | T0 | PLA | profile 1 | no-supports | start",
    ))
    assert res["phase"] == "awaiting_start_approval"
    assert res["plate_count"] == 2
    rid = res["request_id"]
    u1_request = __import__("u1_request")
    req = u1_request.read_request(rid)
    # plates recorded
    assert len(req["plates"]) == 2
    # top-level gcode_hash bound to plate 1 (the gated plate)
    assert req["gcode_hash"] == "sha256:plate1"
    assert req["printer_storage_filename"].endswith("_plate1.gcode")
    # selection persisted
    assert req["kit"]["selected"] == ["01_part0", "03_part2"]
    # stage-1 command targets plate 1 + the request
    assert "_plate1.gcode" in res["start_gate_stage1_command"]
    assert rid in res["start_gate_stage1_command"]


def test_commit_uploads_all_plates_with_distinct_names(tmp_path, fake_profiles, fake_slice_upload):
    kw.run_kit_workflow(_args(
        _kit_zip(tmp_path, 2), live_upload=True,
        form_answers="all | T1 | PETG | profile 2 | supports | start",
    ))
    names = fake_slice_upload["calls"]
    assert len(names) == 2
    assert names[0].endswith("_plate1.gcode") and names[1].endswith("_plate2.gcode")
    assert names[0] != names[1]


def test_extruder_mapping_matches_single_workflow(tmp_path, fake_profiles, fake_slice_upload):
    # SAFETY: T0 -> 'extruder' (NOT extruder1); T1 -> 'extruder1'. Must match
    # u1_slice_workflow's mapping or the gate's tool-match check heats wrong head.
    r0 = kw.run_kit_workflow(_args(
        _kit_zip(tmp_path, 1), fresh=True,
        form_answers="all | T0 | PLA | profile 1 | no-supports | start"))
    assert "--intended-tool extruder " in r0["start_gate_stage1_command"]

    r1 = kw.run_kit_workflow(_args(
        _kit_zip(tmp_path, 1), fresh=True,
        form_answers="all | T1 | PLA | profile 1 | no-supports | start"))
    assert "--intended-tool extruder1 " in r1["start_gate_stage1_command"]


def test_upload_only_action_completes_without_gate(tmp_path, fake_profiles, fake_slice_upload):
    res = kw.run_kit_workflow(_args(
        _kit_zip(tmp_path, 2),
        form_answers="all | T0 | PLA | profile 1 | no-supports | upload-only",
    ))
    assert res["phase"] == "complete"
    rid = res["request_id"]
    req = __import__("u1_request").read_request(rid)
    assert req["phase"] == "complete"
    # plates still uploaded + recorded
    assert len(req["plates"]) == 2


def test_analysis_persists_model_hash_for_recovery(tmp_path, fake_profiles):
    # Review fix: model_hash is the recovery key. Without it, re-sending the
    # same zip (no --request-id) can't resume.
    res = kw.run_kit_workflow(_args(_kit_zip(tmp_path, 2)))
    req = __import__("u1_request").read_request(res["request_id"])
    assert req.get("model_hash"), "model_hash must be persisted for content-hash recovery"


def test_recovery_by_resend_finds_request(tmp_path, fake_profiles, fake_slice_upload):
    # Review fix: re-sending the SAME zip with NO --request-id must resume the
    # request created at form-emit (via find_recent_request_for_model).
    zp = _kit_zip(tmp_path, 2)
    r1 = kw.run_kit_workflow(_args(zp))  # no request-id -> creates request, writes model_hash
    # Second call, same zip, NO request-id, with answers -> must recover r1's id
    r2 = kw.run_kit_workflow(_args(zp, form_answers="all | T0 | PLA | profile 1 | no-supports | start"))
    assert r2["request_id"] == r1["request_id"], "recovery-by-resend must find the prior request"


def test_profile_index_stable_via_persisted_list(tmp_path, fake_profiles, fake_slice_upload, monkeypatch):
    # Review fix: form-emit persists the profile list; the answer call resolves
    # `profile N` against the PERSISTED list even if list_profiles reorders.
    zp = _kit_zip(tmp_path, 1)
    r1 = kw.run_kit_workflow(_args(zp))  # persists form_profiles = [standard, optimal]
    rid = r1["request_id"]
    # Now flip list_profiles order to simulate a history-driven re-sort.
    monkeypatch.setattr(kw, "list_profiles", lambda nozzle=None: [
        {"value": "0_16_optimal", "label": "0.16 Optimal @Snapmaker U1 (0.4 nozzle)"},
        {"value": "0_20_standard", "label": "0.20 Standard @Snapmaker U1 (0.4 nozzle)"},
    ])
    # profile 1 must STILL mean the originally-listed first profile (standard),
    # not the reordered one (optimal), because we replay the persisted list.
    captured = {}
    orig = kw.u1_arrange.arrange_slice
    def spy(paths, out_dir, *, profile, **k):
        captured["profile"] = profile
        return orig(paths, out_dir, profile=profile, **k)
    monkeypatch.setattr(kw.u1_arrange, "arrange_slice", spy)
    kw.run_kit_workflow(_args(zp, request_id=rid,
                              form_answers="all | T0 | PLA | profile 1 | no-supports | start"))
    assert captured["profile"] == "0_20_standard", "profile index must resolve against the persisted list"


def test_slice_failure_emits_clean_event_not_stacktrace(tmp_path, fake_profiles, monkeypatch):
    monkeypatch.setattr(kw, "profile_path", lambda slug: Path("/tmp/p.json"))
    monkeypatch.setattr(kw, "apply_supports_override", lambda p, en, od: Path("/tmp/p.json"))
    def boom(*a, **k):
        raise RuntimeError("Orca arrange-slice failed rc=206: object too large")
    monkeypatch.setattr(kw.u1_arrange, "arrange_slice", boom)
    res = kw.run_kit_workflow(_args(_kit_zip(tmp_path, 2),
                                    form_answers="all | T0 | PLA | profile 1 | no-supports | start"))
    assert res["phase"] == "slice_failed"
    assert "206" in res["error"]


def test_resume_by_request_id_after_form(tmp_path, fake_profiles, fake_slice_upload):
    # First call: emit form (no answers). Second call: same request-id + answers.
    r1 = kw.run_kit_workflow(_args(_kit_zip(tmp_path, 2)))
    rid = r1["request_id"]
    r2 = kw.run_kit_workflow(_args(
        _kit_zip(tmp_path, 2), request_id=rid,
        form_answers="all | T0 | PLA | profile 1 | no-supports | start",
    ))
    assert r2["request_id"] == rid
    assert r2["phase"] == "awaiting_start_approval"


# --------------------------------------------------------------------------- #
# form-protocol: schema emission + --form-answers-json intake
# --------------------------------------------------------------------------- #

def test_form_event_includes_schema(tmp_path, fake_profiles, capsys):
    res = kw.run_kit_workflow(_args(_kit_zip(tmp_path, 3)))
    assert res["phase"] == "awaiting_form"
    # the emitted need_input event carries both text + declarative schema
    out = capsys.readouterr().out
    import json as _j
    events = [_j.loads(l) for l in out.splitlines() if l.strip().startswith("{")]
    form_ev = next(e for e in events if e.get("key") == "kit_form")
    assert "form" in form_ev                      # text fallback retained
    assert form_ev["form_schema"]["version"] == 1
    ids = {f["id"] for f in form_ev["form_schema"]["fields"]}
    assert {"parts", "tool", "material", "profile", "supports", "action"} <= ids
    assert form_ev["form_schema"]["submit"]["json"]  # json submit template present


def test_commit_via_form_answers_json(tmp_path, fake_profiles, fake_slice_upload):
    res = kw.run_kit_workflow(_args(
        _kit_zip(tmp_path, 3),
        form_answers_json='{"parts": ["01_part0", "03_part2"], "tool": "T0", '
                          '"material": "PLA", "profile": 1, "supports": "no-supports", "action": "start"}',
    ))
    assert res["phase"] == "awaiting_start_approval"
    rid = res["request_id"]
    req = __import__("u1_request").read_request(rid)
    assert req["kit"]["selected"] == ["01_part0", "03_part2"]
    assert req["gcode_hash"] == "sha256:plate1"


def test_form_answers_json_invalid_rejected(tmp_path, fake_profiles, fake_slice_upload):
    res = kw.run_kit_workflow(_args(_kit_zip(tmp_path, 2), form_answers_json="{not valid json"))
    assert res["phase"] == "form_rejected"
    assert any("invalid --form-answers-json" in e for e in res["errors"])


def test_form_event_emits_form_url_when_sidecar_configured(tmp_path, fake_profiles, capsys, monkeypatch):
    # Sidecar deep link (Level 3 escape hatch) appears only when configured.
    monkeypatch.setenv("U1_SIDECAR_BOT_USERNAME", "u1_sidecar_test_bot")
    res = kw.run_kit_workflow(_args(_kit_zip(tmp_path, 2)))
    assert res["phase"] == "awaiting_form"
    import json as _j
    out = capsys.readouterr().out
    events = [_j.loads(l) for l in out.splitlines() if l.strip().startswith("{")]
    form_ev = next(e for e in events if e.get("key") == "kit_form")
    assert "form_url" in form_ev
    assert form_ev["form_url"].startswith("https://t.me/u1_sidecar_test_bot?start=")
    assert form_ev["form_url"].endswith(res["request_id"])


def test_form_event_omits_form_url_when_no_sidecar(tmp_path, fake_profiles, capsys, monkeypatch):
    monkeypatch.delenv("U1_SIDECAR_BOT_USERNAME", raising=False)
    res = kw.run_kit_workflow(_args(_kit_zip(tmp_path, 2)))
    import json as _j
    out = capsys.readouterr().out
    events = [_j.loads(l) for l in out.splitlines() if l.strip().startswith("{")]
    form_ev = next(e for e in events if e.get("key") == "kit_form")
    assert "form_url" not in form_ev   # no leak when not configured
