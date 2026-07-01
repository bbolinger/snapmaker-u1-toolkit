"""Tests for scripts/u1_kit_workflow.py — kit orchestrator (Option 2 seam).

External deps (Orca arrange-slice, Moonraker upload, profile resolution) are
mocked so the orchestration logic is tested hermetically. A live end-to-end run
against the real binary is done separately in hermes-agent-stack.
"""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


def _sha256_of(path: Path) -> str:
    """Compute sha256:<hex> for a file, matching u1_kit_workflow's format."""
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()

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

    def fake_upload(gcode, on_collision=None, material=None):
        uploads["calls"].append(Path(gcode).name)
        return {"uploaded_filename": Path(gcode).name, "moonraker_upload_ok": True}

    monkeypatch.setattr(kw, "_real_upload", fake_upload)
    return uploads


# --------------------------------------------------------------------------- #
# ANALYSIS + DECISION
# --------------------------------------------------------------------------- #

def test_no_form_answers_emits_first_turn_prompt(tmp_path, fake_profiles):
    # Staged flow: no answers → Turn 1 emits the parts prompt (previously a
    # single form-emit).
    res = kw.run_kit_workflow(_args(_kit_zip(tmp_path, 3)))
    assert res["phase"] == "awaiting_parts"
    rid = res["request_id"]
    # kit persisted at ingest
    req = __import__("u1_request").read_request(rid)
    assert req["kit"]["part_count"] == 3
    # profile list also persisted at Turn 1 so a later --form-answers
    # one-liner resolves `profile N` against a stable list.
    assert req.get("form_profiles"), "Turn 1 must persist form_profiles for stable resolution"


def test_turn1_form_profiles_not_clobbered_by_reinvocation(tmp_path, monkeypatch):
    # First-write-wins guard: the profile list persisted at Turn 1 must
    # survive a second no-answer invocation even if list_profiles has
    # re-sorted between them. Without the guard, `profile 1` on a later
    # --form-answers call resolves against a list the operator never saw.
    monkeypatch.setattr(kw, "list_profiles", lambda nozzle=None: [
        {"value": "A", "label": "A-label"},
        {"value": "B", "label": "B-label"},
    ])
    zp = _kit_zip(tmp_path, 3)
    r1 = kw.run_kit_workflow(_args(zp))
    rid = r1["request_id"]
    u1_request = __import__("u1_request")
    persisted_1 = [p["value"] for p in u1_request.read_request(rid)["form_profiles"]]
    assert persisted_1 == ["A", "B"]
    # Flip the order — simulate history-driven re-sort
    monkeypatch.setattr(kw, "list_profiles", lambda nozzle=None: [
        {"value": "B", "label": "B-label"},
        {"value": "A", "label": "A-label"},
    ])
    kw.run_kit_workflow(_args(zp, request_id=rid))
    persisted_2 = [p["value"] for p in u1_request.read_request(rid)["form_profiles"]]
    assert persisted_2 == ["A", "B"], (
        "second Turn 1 invocation must NOT clobber the persisted profile list; "
        f"got {persisted_2}")


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
    # top-level gcode_hash bound to plate 1 (the gated plate). The
    # workflow re-hashes the renamed file, so match the file bytes.
    plate1_path = Path(req["plates"][0]["gcode_path"])
    assert req["gcode_hash"] == _sha256_of(plate1_path)
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

def test_first_turn_prompt_carries_operator_needs(tmp_path, fake_profiles, capsys):
    # Under the staged 6-turn flow, the single kit_form-with-schema event
    # is deferred (form-mode button UX not yet wired). Turn 1 emits a
    # `parts` need_input carrying the parts_thumbnail_grid + a listing +
    # `next_command` options — enough for the operator to pick which
    # STLs to include. This test guards those elements so a future
    # refactor doesn't quietly break the operator's Turn 1 UX.
    res = kw.run_kit_workflow(_args(_kit_zip(tmp_path, 3)))
    assert res["phase"] == "awaiting_parts"
    out = capsys.readouterr().out
    import json as _j
    events = [_j.loads(l) for l in out.splitlines() if l.strip().startswith("{")]
    parts_ev = next(e for e in events if e.get("key") == "parts")
    assert "prompt" in parts_ev
    assert "options" in parts_ev and parts_ev["options"]
    assert "next_command" in parts_ev["options"][0]
    # thumbnail grid render event fires before the need_input
    assert any(e.get("kind") == "parts_thumbnail_grid" for e in events)


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
    plate1_path = Path(req["plates"][0]["gcode_path"])
    assert req["gcode_hash"] == _sha256_of(plate1_path)


def test_form_answers_json_invalid_rejected(tmp_path, fake_profiles, fake_slice_upload):
    res = kw.run_kit_workflow(_args(_kit_zip(tmp_path, 2), form_answers_json="{not valid json"))
    assert res["phase"] == "form_rejected"
    assert any("invalid --form-answers-json" in e for e in res["errors"])

