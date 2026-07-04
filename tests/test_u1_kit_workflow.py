"""Tests for scripts/u1_kit_workflow.py — kit orchestrator (Option 2 seam).

External deps (Orca arrange-slice, Moonraker upload, profile resolution) are
mocked so the orchestration logic is tested hermetically. A live end-to-end run
against the real binary is done separately in hermes-agent-stack.
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


def _sha256_of(path: Path) -> str:
    """Compute sha256:<hex> for a file, matching u1_kit_workflow's format."""
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()

import u1_kit_workflow as kw


@pytest.fixture(autouse=True)
def _fake_stage2_gate(monkeypatch):
    """_action_start now RUNS the Stage-2 gate as a subprocess (the model no
    longer relays the token+nonce command). Mock it so unit tests never contact
    Moonraker or block for the grace window. Returns a dict tests can inspect."""
    calls = {}

    def _fake(gate_py, argv, out_dir):
        calls["argv"] = list(argv)
        calls["cmd"] = " ".join(argv)
        out = json.dumps({"stage": "start_attempt", "ok": True,
                          "started": True, "blockers": []})
        return SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")

    monkeypatch.setattr(kw, "_invoke_stage2_gate", _fake)
    return calls
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
    # form action=start now routes into the two-turn bed-clear gate (like the
    # staged path) instead of handing out a raw Stage-1 command.
    assert res["phase"] == "awaiting_bed_clear_start"
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
    # stage-1 command (persisted for the fallback) targets plate 1 + the request
    assert "_plate1.gcode" in req["start_gate_stage1_command"]
    assert rid in req["start_gate_stage1_command"]


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
    _ur = __import__("u1_request")
    r0 = kw.run_kit_workflow(_args(
        _kit_zip(tmp_path, 1), fresh=True,
        form_answers="all | T0 | PLA | profile 1 | no-supports | start"))
    assert "--intended-tool extruder " in _ur.read_request(r0["request_id"])["start_gate_stage1_command"]

    r1 = kw.run_kit_workflow(_args(
        _kit_zip(tmp_path, 1), fresh=True,
        form_answers="all | T1 | PLA | profile 1 | no-supports | start"))
    assert "--intended-tool extruder1 " in _ur.read_request(r1["request_id"])["start_gate_stage1_command"]


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
    assert r2["phase"] == "awaiting_bed_clear_start"


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
    assert res["phase"] == "awaiting_bed_clear_start"
    rid = res["request_id"]
    req = __import__("u1_request").read_request(rid)
    assert req["kit"]["selected"] == ["01_part0", "03_part2"]
    plate1_path = Path(req["plates"][0]["gcode_path"])
    assert req["gcode_hash"] == _sha256_of(plate1_path)


def test_form_answers_json_invalid_rejected(tmp_path, fake_profiles, fake_slice_upload):
    res = kw.run_kit_workflow(_args(_kit_zip(tmp_path, 2), form_answers_json="{not valid json"))
    assert res["phase"] == "form_rejected"
    assert any("invalid --form-answers-json" in e for e in res["errors"])



# --------------------------------------------------------------------------- #
# Fence stickiness: explicit CLI operator must ride every emitted command
# --------------------------------------------------------------------------- #

def _stdout_events(capsys):
    evs = []
    for line in capsys.readouterr().out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                evs.append(json.loads(line))
            except Exception:
                pass
    return evs


def _emitted_commands(evs):
    cmds = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("next_command", "command", "yes_command") and isinstance(v, str):
                    cmds.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for item in o:
                walk(item)

    walk(evs)
    return cmds


def test_explicit_cli_operator_sticky_in_every_emitted_command(tmp_path, fake_profiles, capsys):
    # 2026-07-01 incident class: a smoke:* operator dropped out of one
    # next_command, the agent copied it verbatim, and the chain resolved to
    # the production env operator — Fence 1 passed and a real print fired.
    # Every kit-workflow command emitted under an explicit operator must
    # carry it.
    kw.run_kit_workflow(_args(_kit_zip(tmp_path, 3), operator="smoke:sticky"))
    cmds = [c for c in _emitted_commands(_stdout_events(capsys))
            if "u1_kit_workflow.py" in c]
    assert cmds, "expected at least one emitted kit-workflow command"
    for c in cmds:
        assert "--operator smoke:sticky" in c, c


def test_env_resolved_operator_is_not_baked_into_commands(tmp_path, fake_profiles, capsys, monkeypatch):
    # Replay-safety (v2.0.0 decision): identity that came from U1_OPERATOR
    # env resolves at execution time; it is NOT frozen into commands.
    monkeypatch.setenv("U1_OPERATOR", "telegram:someone")
    kw.run_kit_workflow(_args(_kit_zip(tmp_path, 3), operator=None))
    cmds = [c for c in _emitted_commands(_stdout_events(capsys))
            if "u1_kit_workflow.py" in c]
    assert cmds
    for c in cmds:
        assert "--operator" not in c, c


# --------------------------------------------------------------------------- #
# v2.1.0-rc2 state-machine fixes: collision, backfill, upload honesty, sidecar
# --------------------------------------------------------------------------- #

@pytest.fixture
def recording_upload(monkeypatch):
    """Like fake_slice_upload's uploader but records on_collision per call
    and lets tests script per-call results."""
    calls = {"uploads": [], "result": None}

    def fake_upload(gcode, on_collision=None, material=None):
        calls["uploads"].append({"name": Path(gcode).name,
                                 "on_collision": on_collision})
        if calls["result"]:
            return dict(calls["result"], uploaded_filename=Path(gcode).name)
        return {"uploaded_filename": Path(gcode).name,
                "moonraker_upload_ok": True, "returncode": 0}

    monkeypatch.setattr(kw, "_real_upload", fake_upload)
    return calls


def _fake_arrange(monkeypatch, n_plates=1):
    def fake_arrange(paths, out_dir, *, tool, material, profile, nozzle,
                     auto_orient, allow_rotations, process_path_override=None):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        plates = []
        for i in range(1, n_plates + 1):
            g = out_dir / f"plate_{i}.gcode"
            g.write_text(f"; plate {i}\nT0\nG1 X10 Y10 E1\n")
            plates.append({"plate_idx": i, "gcode_path": str(g),
                           "gcode_hash": f"sha256:plate{i}", "metadata": {}})
        return {"plate_count": n_plates, "plates": plates, "cmd": ["orca"]}

    monkeypatch.setattr(kw.u1_arrange, "arrange_slice", fake_arrange)
    monkeypatch.setattr(kw, "profile_path", lambda slug: Path("/tmp/process.json"))
    monkeypatch.setattr(kw, "apply_supports_override",
                        lambda p, en, od: Path("/tmp/process_ovr.json"))


def test_reupload_of_own_plate_defaults_to_overwrite(tmp_path, fake_profiles,
                                                     recording_upload, monkeypatch):
    # adjust -> re-confirm re-slices to the SAME deterministic plate name.
    # First upload: no collision default. Second (same request): overwrite —
    # rc=5 previously dead-ended the advertised adjust option.
    _fake_arrange(monkeypatch)
    zp = _kit_zip(tmp_path, 2)
    ans = "all | T0 | PLA | profile 1 | no-supports | start"
    r1 = kw.run_kit_workflow(_args(zp, form_answers=ans, live_upload=True))
    rid = r1["request_id"]
    assert recording_upload["uploads"][0]["on_collision"] is None
    kw.run_kit_workflow(_args(zp, request_id=rid, form_answers=ans,
                              live_upload=True))
    assert recording_upload["uploads"][1]["on_collision"] == "overwrite"


def test_legacy_upload_failure_does_not_claim_uploaded(tmp_path, fake_profiles,
                                                       recording_upload, monkeypatch):
    # A dead Moonraker (rc=4) previously still emitted kit_uploaded and
    # phase=complete "all plates on the printer".
    _fake_arrange(monkeypatch)
    recording_upload["result"] = {"moonraker_upload_ok": False, "returncode": 4}
    res = kw.run_kit_workflow(_args(
        _kit_zip(tmp_path, 2), live_upload=True,
        form_answers="all | T0 | PLA | profile 1 | no-supports | upload-only"))
    assert res["phase"] == "upload_failed"
    assert res["failures"]


def test_post_confirm_action_backfills_from_persisted_state(tmp_path, fake_profiles,
                                                            recording_upload, monkeypatch):
    # A post-confirm --action with missing turn flags must resume from
    # persisted state, NOT fall back into the staged Q&A (which re-slices).
    _fake_arrange(monkeypatch)
    import u1_request
    zp = _kit_zip(tmp_path, 2)
    ans = "all | T0 | PLA | profile 1 | no-supports | start"
    r1 = kw.run_kit_workflow(_args(zp, form_answers=ans, live_upload=True))
    rid = r1["request_id"]
    # persisted confirm state exists; now invoke with ONLY the action flag
    res = kw.run_kit_workflow(_args(zp, request_id=rid, action="start"))
    assert res["phase"] not in ("awaiting_parts", "awaiting_orient",
                                "awaiting_tool", "awaiting_preset",
                                "awaiting_supports"), res
    # no second slice happened: staged Q&A fallback would have re-sliced
    st = u1_request.read_request(rid) or {}
    assert st.get("phase") != "kit_analysis"


def test_action_start_adopts_stage1_sidecar_token(tmp_path, fake_profiles, monkeypatch):
    # Legacy loop-closer: confirm never persisted a token (bed capture
    # failed), operator ran the emitted Stage 1 command which wrote the
    # sidecar. --action start must adopt it and proceed to the yes/no
    # prompt instead of re-emitting Stage 1 forever.
    import json as _json
    import u1_request
    rid = "u1_2026_0701_a1b2c3"
    u1_request.write_request(
        rid, phase="awaiting_start_approval",
        printer_storage_filename="kit_plate1.gcode",
        tool="T0", material="PLA",
        plates=[{"plate_idx": 1, "gcode_hash": "sha256:x",
                 "printer_storage_filename": "kit_plate1.gcode"}],
        start_gate_stage1_command="python3 gate.py kit_plate1.gcode",
        safety={"approval_token": None})
    rd = u1_request.request_dir(rid)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "bed_snapshot.approval_token.json").write_text(
        _json.dumps({"token": "sidecartoken123", "timestamp_utc": "2026-07-01T00:00:00Z"}))
    res = kw._action_start(None, rid, False, yes_command="echo yes",
                           operator="test:unit")
    assert res["phase"] == "awaiting_bed_clear_start", res
    st = u1_request.read_request(rid)
    assert st["safety"]["approval_token"] == "sidecartoken123"


def test_garbage_stl_in_zip_emits_kit_rejected_not_traceback(tmp_path, fake_profiles, capsys):
    # A .stl entry full of garbage used to escape as a raw ValueError
    # traceback from build_kit. It must be a clean kit_rejected event.
    import zipfile as _zf
    zp = tmp_path / "bad.zip"
    with _zf.ZipFile(zp, "w") as z:
        z.writestr("a.stl", "this is not an stl at all")
        z.writestr("b.stl", "neither is this")
    res = kw.run_kit_workflow(_args(zp))
    assert res["phase"] == "kit_rejected"
    stages = [e.get("stage") for e in _stdout_events(capsys)]
    assert "kit_rejected" in stages


def test_over_limit_zip_emits_kit_rejected(tmp_path, fake_profiles, capsys, monkeypatch):
    import u1_kit as _uk
    monkeypatch.setattr(_uk, "MAX_KIT_PARTS", 2)
    res = kw.run_kit_workflow(_args(_kit_zip(tmp_path, 3)))
    assert res["phase"] == "kit_rejected"
    evs = _stdout_events(capsys)
    rej = next(e for e in evs if e.get("stage") == "kit_rejected")
    assert "limit is 2" in rej["error"]


@pytest.fixture(autouse=True)
def _fake_bed_capture(monkeypatch):
    """Form-mode `action=start` now captures a bed photo + token and routes into
    the two-turn bed-clear gate (like the staged path), instead of handing out a
    raw Stage-1 command. Mock the capture so tests are fast + deterministic and
    don't touch a real camera. Tests wanting capture-failure override this."""
    def _fake(out_dir):
        snap = Path(out_dir) / "bed_snapshot.jpg"
        try:
            snap.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
        except Exception:
            pass
        return {"ok": True, "snapshot_path": str(snap), "token": "faketoken123",
                "approval_ttl_seconds": 2700, "approval_expires_at": None,
                "captured_at_utc": None, "reason": None}
    monkeypatch.setattr(kw, "_capture_bed_and_issue_token", _fake)


def test_form_start_reaches_bed_clear_gate_and_mints_nonce(tmp_path, fake_profiles, fake_slice_upload):
    """Regression (operator 2026-07-03, every run): form action=start used to
    hand out a raw Stage-1 command with NO nonce path, so the kit gate refused
    every start. It must now route into the two-turn bed-clear gate — the first
    call persists a pending_bed_clear_start nonce, and the yes-turn mints the
    Stage-2 approval nonce (the value the gate consumes)."""
    ur = __import__("u1_request")
    # Reuse the SAME zip for both calls, exactly like production: the real
    # yes-command carries the same archive path, so model_hash is unchanged and
    # request_revision does not bump (a fresh zip per call bumps model_hash ->
    # revision -> the pending binding mismatches; that was a test artifact).
    zip_path = _kit_zip(tmp_path, 3)
    res = kw.run_kit_workflow(_args(
        zip_path,
        form_answers="parts 1,3 | T0 | PLA | profile 1 | no-supports | start"))
    rid = res["request_id"]
    assert res["phase"] == "awaiting_bed_clear_start"
    pending = (ur.read_request(rid).get("safety") or {}).get("pending_bed_clear_start")
    assert pending and pending.get("nonce")
    assert pending.get("prompt_key") == "bed_clear_start"

    # operator says yes -> the Stage-2 nonce gets minted (the missing path)
    kw.run_kit_workflow(_args(
        zip_path, request_id=rid,
        action="start", bed_clear_confirmed=True, pending_nonce=pending["nonce"]))
    safety2 = ur.read_request(rid).get("safety") or {}
    assert safety2.get("stage2_approval_nonce"), "Stage-2 nonce was never minted"


def test_confirm_start_token_redeems_and_mints_nonce(tmp_path, fake_profiles, fake_slice_upload):
    """The bed-clear 'yes' is now a short `--confirm-start <token>` (a 26B model
    mangled the old ~200-char command). Redeeming the token resolves the request
    + single-use nonce from state and mints the Stage-2 approval nonce."""
    ur = __import__("u1_request")
    zip_path = _kit_zip(tmp_path, 3)
    res = kw.run_kit_workflow(_args(
        zip_path, form_answers="parts 1,3 | T0 | PLA | profile 1 | no-supports | start"))
    rid = res["request_id"]
    assert res["phase"] == "awaiting_bed_clear_start"
    tok = ur.read_request(rid)["safety"]["pending_bed_clear_start"]["confirm_token"]
    assert tok and tok.startswith("c")

    # the operator says yes -> the model relays ONLY the short token
    kw.run_kit_workflow(_args(zip_path, confirm_start=tok))
    assert ur.read_request(rid)["safety"].get("stage2_approval_nonce"), \
        "Stage-2 nonce not minted via --confirm-start"

    # single-use: the token is consumed and cannot be replayed
    import u1_form
    assert u1_form.resolve_confirm_token(tok, consume=False) is None


def test_confirm_start_invalid_token_refused(tmp_path, fake_profiles, fake_slice_upload):
    """An unknown / already-used / traversal token is refused with a structured
    event — never a crash, never a start."""
    res = kw.run_kit_workflow(_args(_kit_zip(tmp_path, 2), confirm_start="cdeadbeef00"))
    assert res["phase"] == "bed_clear_confirm_token_invalid"
    res2 = kw.run_kit_workflow(_args(_kit_zip(tmp_path, 2), confirm_start="../../etc/passwd"))
    assert res2["phase"] == "bed_clear_confirm_token_invalid"


def test_printer_filename_strips_doc_cache_prefix(tmp_path, fake_profiles, fake_slice_upload):
    """The Hermes doc-cache prefix (doc_<hash>_) must NOT lead the printer
    filename — otherwise every file shows as 'doc_55da…' and the operator can
    only tell them apart by thumbnail (operator 2026-07-04). The gcode hash is
    content-based, so the rename doesn't affect tracking."""
    zp = _kit_zip(tmp_path, 2)
    doc_zip = zp.with_name("doc_55da642fda9e_angles_teaching_kit.zip")
    zp.rename(doc_zip)
    res = kw.run_kit_workflow(_args(
        doc_zip, form_answers="all | T0 | PLA | profile 1 | no-supports | start"))
    req = __import__("u1_request").read_request(res["request_id"])
    fn = req["printer_storage_filename"]
    assert not fn.startswith("doc_"), fn
    assert fn.startswith("angles_teaching_kit_plate1"), fn
    # unit check on the helper
    assert kw._strip_doc_prefix("doc_98e0403a8bc4_bracket") == "bracket"
    assert kw._strip_doc_prefix("my_bracket") == "my_bracket"


def test_form_start_camera_fail_offers_upload_only_no_crash(
        tmp_path, fake_profiles, fake_slice_upload, monkeypatch):
    """Camera-unreachable at the bed-clear step must gracefully offer upload-only,
    NOT crash. Regression: _commit_kit_legacy's camera-fail branch referenced bare
    no_live_upload / no_live_material where only the _-prefixed locals exist →
    NameError (found via kit-of-1 unification 2026-07-03; would bite any operator
    whose camera is unreachable, single OR multi)."""
    monkeypatch.setattr(
        kw, "_capture_bed_and_issue_token",
        lambda out_dir: {"ok": False, "reason": "camera unreachable",
                         "snapshot_path": None, "token": None,
                         "approval_ttl_seconds": None, "approval_expires_at": None,
                         "captured_at_utc": None})
    res = kw.run_kit_workflow(_args(
        _kit_zip(tmp_path, 2),
        form_answers="all | T0 | PLA | profile 1 | no-supports | start"))
    assert res["phase"] == "awaiting_confirm", res
    assert res.get("bed_capture_failed") is True
