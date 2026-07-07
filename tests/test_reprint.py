"""Reprint (v2.3): list recent prints, restart through the standard gate.

No slicing happens on a reprint — the gcode is already in printer storage.
The safety boundary is untouched: fresh bed photo, single-use confirm token,
revision+hash-bound pending, live material re-check at the gate.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import u1_form
import u1_request
import u1_kit_workflow as kw


@pytest.fixture(autouse=True)
def _sandbox_confirm_markers(tmp_path, monkeypatch):
    monkeypatch.setattr(kw, "_PENDING_CONFIRM_DIR", tmp_path / "pending_confirm")


@pytest.fixture(autouse=True)
def _stable_printer_metadata(monkeypatch):
    """Reprint binds printer-side size+modified; hermetic default = stable."""
    monkeypatch.setattr(kw, "_printer_file_metadata",
                        lambda fname: {"size": 4242, "modified": 1751900000.0})


def _seed_uploaded_request(model="widget", fname=None, tool="T1",
                           material="PETG", ghash="sha256:abc123",
                           doc_hash="a1b2c3d4e5f6"):
    rid = u1_request.generate_request_id()
    fname = fname or f"{model}_plate1.gcode"
    d = Path(u1_request.ensure_request_dir(rid))
    (d / "review.md").write_text("# review\n")
    u1_request.write_request(
        rid,
        model_file=f"doc_{doc_hash}_{model}.zip",
        tool=tool, material=material, request_revision=1,
        printer_storage_filename=fname,
        out_dir=str(d),
        plates=[{"plate_idx": 1, "gcode_hash": ghash,
                 "gcode_path": str(d / "plate_1.gcode"),
                 "printer_storage_filename": fname, "uploaded": True}],
    )
    return rid, fname


def _fake_bed_ok(out_dir):
    p = Path(out_dir) / "bed_snapshot.jpg"
    p.write_bytes(b"jpg")
    return {"ok": True, "snapshot_path": str(p), "token": "tok123",
            "approval_ttl_seconds": 1800, "approval_expires_at": None,
            "captured_at_utc": "2026-07-06T00:00:00Z", "reason": None}


def test_candidates_dedupe_and_strip_prefix(monkeypatch):
    """Same model re-uploaded under a different doc hash collapses to the
    newest; labels drop the doc_<hash>_ cache prefix."""
    monkeypatch.setattr(kw, "_printer_gcode_filenames", lambda: None)
    _seed_uploaded_request(model="grip", fname="grip_old.gcode", doc_hash="aaaa11112222")
    _seed_uploaded_request(model="grip", fname="grip_new.gcode", doc_hash="bbbb33334444")
    cands = kw._reprint_candidates()
    grips = [c for c in cands if c["model"] == "grip"]
    assert len(grips) == 1
    assert grips[0]["model"] == "grip"  # prefix stripped
    assert not grips[0]["model"].startswith("doc_")


def test_reprint_list_mints_resolvable_tokens(monkeypatch, capsys):
    monkeypatch.setattr(kw, "_printer_gcode_filenames", lambda: None)
    rid, _ = _seed_uploaded_request()
    res = kw._action_reprint_list(None, True, "test-op")
    assert res["phase"] == "awaiting_reprint_pick"
    out = capsys.readouterr().out
    ev = next(json.loads(l) for l in out.splitlines()
              if '"reprint_pick"' in l and '"need_input"' in l)
    tok = ev["options"][0]["next_command"].split("--reprint-start ")[1].strip()
    assert u1_form.resolve_confirm_token(tok) == rid  # resolves to old request


def test_reprint_start_reaches_bed_clear(monkeypatch, capsys):
    """Happy path: seeds a fresh request (reprint_of), copies the plate hash,
    persists the Stage-1 token, and lands on the standard bed-clear prompt."""
    old_rid, fname = _seed_uploaded_request()
    monkeypatch.setattr(kw, "_printer_gcode_filenames", lambda: {fname})
    monkeypatch.setattr(kw, "_capture_bed_and_issue_token", _fake_bed_ok)
    tok = u1_form.new_confirm_token()
    u1_form.persist_confirm_token(tok, old_rid)
    res = kw._action_reprint_start(None, True, "test-op", tok)
    assert res["phase"] == "awaiting_bed_clear_start", res
    new_rid = res["request_id"]
    assert new_rid != old_rid
    st = u1_request.read_request(new_rid)
    assert st["reprint_of"] == old_rid
    assert st["printer_storage_filename"] == fname
    assert st["plates"][0]["gcode_hash"] == "sha256:abc123"
    assert st["safety"]["approval_token"] == "tok123"
    pending = st["safety"]["pending_bed_clear_start"]
    assert pending["gcode_hash"] == "sha256:abc123" and pending["confirm_token"]
    out = capsys.readouterr().out
    # Model-free YES: the token stays server-side, never emitted where the
    # model could fire it — and the armed marker is opaque (the hook builds
    # its own --confirm-start-for argv), so the token isn't in /tmp either.
    assert pending["confirm_token"] not in out
    marker = kw._PENDING_CONFIRM_DIR / f"{new_rid}.json"
    assert marker.exists()
    marker_text = marker.read_text()
    assert pending["confirm_token"] not in marker_text
    assert "confirm_cmd" not in marker_text


def test_reprint_refuses_when_file_gone(monkeypatch, capsys):
    old_rid, fname = _seed_uploaded_request()
    monkeypatch.setattr(kw, "_printer_gcode_filenames", lambda: {"other.gcode"})
    tok = u1_form.new_confirm_token(); u1_form.persist_confirm_token(tok, old_rid)
    res = kw._action_reprint_start(None, True, "test-op", tok)
    assert res["phase"] == "reprint_file_missing"
    assert "no longer in printer storage" in capsys.readouterr().out


def test_reprint_fails_closed_when_printer_unreachable(monkeypatch, capsys):
    old_rid, fname = _seed_uploaded_request()
    monkeypatch.setattr(kw, "_printer_gcode_filenames", lambda: None)
    tok = u1_form.new_confirm_token(); u1_form.persist_confirm_token(tok, old_rid)
    res = kw._action_reprint_start(None, True, "test-op", tok)
    assert res["phase"] == "reprint_file_missing"  # unknown = refuse


def test_reprint_fails_closed_on_bed_capture_failure(monkeypatch, capsys):
    old_rid, fname = _seed_uploaded_request()
    monkeypatch.setattr(kw, "_printer_gcode_filenames", lambda: {fname})
    monkeypatch.setattr(kw, "_capture_bed_and_issue_token",
                        lambda d: {"ok": False, "snapshot_path": None,
                                   "token": None, "reason": "camera dark"})
    tok = u1_form.new_confirm_token(); u1_form.persist_confirm_token(tok, old_rid)
    res = kw._action_reprint_start(None, True, "test-op", tok)
    assert res["phase"] == "reprint_bed_capture_failed"
    st = u1_request.read_request(res["request_id"]) or {}
    assert not (st.get("safety") or {}).get("pending_bed_clear_start")


def test_reprint_invalid_token_refused(capsys):
    res = kw._action_reprint_start(None, True, "test-op", "c00000000000")
    assert res["phase"] == "reprint_token_invalid"


def test_reprint_pick_token_is_single_use(monkeypatch):
    old_rid, fname = _seed_uploaded_request()
    monkeypatch.setattr(kw, "_printer_gcode_filenames", lambda: {fname})
    monkeypatch.setattr(kw, "_capture_bed_and_issue_token", _fake_bed_ok)
    tok = u1_form.new_confirm_token(); u1_form.persist_confirm_token(tok, old_rid)
    first = kw._action_reprint_start(None, True, "test-op", tok)
    assert first["phase"] == "awaiting_bed_clear_start"
    second = kw._action_reprint_start(None, True, "test-op", tok)  # replay
    assert second["phase"] == "reprint_token_invalid"
def test_reprint_confirm_start_skips_ingest(monkeypatch, capsys):
    """Live 2026-07-06: the operator's YES on a reprint died because the
    confirm-token fall-through tried to recover + re-ingest the ORIGINAL
    archive (long gone from the doc cache). A reprint confirm must route
    straight to the gate turn — no archive, no ingest."""
    from types import SimpleNamespace
    old_rid, fname = _seed_uploaded_request()
    monkeypatch.setattr(kw, "_printer_gcode_filenames", lambda: {fname})
    monkeypatch.setattr(kw, "_capture_bed_and_issue_token", _fake_bed_ok)
    tok = u1_form.new_confirm_token(); u1_form.persist_confirm_token(tok, old_rid)
    res = kw._action_reprint_start(None, True, "test-op", tok)
    new_rid = res["request_id"]
    confirm = (u1_request.read_request(new_rid)["safety"]
               ["pending_bed_clear_start"]["confirm_token"])

    # The confirmed turn would invoke the real gate subprocess — stub it and
    # capture that we actually REACHED it (the bug never got this far).
    reached = {}
    def _fake_gate(gate_py, argv, out_dir):
        reached["argv"] = argv
        return None  # None == grace window opened, gate detached
    monkeypatch.setattr(kw, "_invoke_stage2_gate", _fake_gate)

    args = SimpleNamespace(
        model=None, confirm_start=confirm, reprint=False, reprint_start=None,
        json_events=True, operator="test-op", events_file=None,
        request_id=None, action=None, bed_clear_confirmed=False,
        pending_nonce=None, nozzle="0.4",
    )
    out_res = kw.run_kit_workflow(args)
    out = capsys.readouterr().out
    assert reached, f"gate turn never reached; result={out_res} out={out[:400]}"
    assert fname in " ".join(reached["argv"])          # gating the right file
    assert "kit_ingested" not in out                    # NO re-ingest happened

def test_reprint_satisfies_can_start(monkeypatch):
    """Live-caught: the gate refused the reprint with 'no readiness_card
    emitted yet'. The reprint turn IS the review moment (original previews +
    review doc + fresh bed photo), so it must record the audited readiness
    row with the same revision+hash binding — and can_start() must pass."""
    import u1_safety
    old_rid, fname = _seed_uploaded_request()
    monkeypatch.setattr(kw, "_printer_gcode_filenames", lambda: {fname})
    monkeypatch.setattr(kw, "_capture_bed_and_issue_token", _fake_bed_ok)
    tok = u1_form.new_confirm_token(); u1_form.persist_confirm_token(tok, old_rid)
    res = kw._action_reprint_start(None, True, "test-op", tok)
    assert res["phase"] == "awaiting_bed_clear_start"
    req = u1_request.read_request(res["request_id"])
    allowed, reason = u1_safety.can_start(req)
    assert allowed, f"can_start refused a seeded reprint: {reason}"
    # And the drift check still bites: change the hash, must refuse.
    req2 = dict(req); req2["gcode_hash"] = "sha256:tampered"
    allowed2, reason2 = u1_safety.can_start(req2)
    assert not allowed2 and "gcode regenerated" in reason2

def test_reprint_of_reprint_resurfaces_original_artifacts(monkeypatch, capsys, tmp_path):
    """Live 2026-07-07: reprinting a reprint showed the operator a bed photo
    and nothing else — the seeded request carried no preview/iso/review
    pointers, so the review moment was visually empty while the prompt
    claimed everything was attached. The resolver must walk reprint_of back
    to whoever has the artifacts, emit them, and persist the pointers on the
    new seed (chains stay one hop deep)."""
    import json as _json
    old_rid, fname = _seed_uploaded_request()
    # give the ORIGINAL request real artifact files + pointers
    art_dir = tmp_path / "orig"; art_dir.mkdir()
    prev = art_dir / "plate_1_preview.png"; prev.write_bytes(b"png")
    iso = art_dir / "plate_1_iso.png"; iso.write_bytes(b"png")
    rev = art_dir / "review.md"; rev.write_text("# review")
    st = u1_request.read_request(old_rid)
    plates = st["plates"]; plates[0]["preview_path"] = str(prev); plates[0]["iso_path"] = str(iso)
    u1_request.write_request(old_rid, plates=plates, out_dir=str(art_dir))

    monkeypatch.setattr(kw, "_printer_gcode_filenames", lambda: {fname})
    monkeypatch.setattr(kw, "_capture_bed_and_issue_token", _fake_bed_ok)

    # hop 1: reprint the original
    tok = u1_form.new_confirm_token(); u1_form.persist_confirm_token(tok, old_rid)
    r1 = kw._action_reprint_start(None, True, "test-op", tok)
    mid_rid = r1["request_id"]
    capsys.readouterr()

    # hop 2: reprint the REPRINT (the live failure case)
    tok2 = u1_form.new_confirm_token(); u1_form.persist_confirm_token(tok2, mid_rid)
    r2 = kw._action_reprint_start(None, True, "test-op", tok2)
    out = capsys.readouterr().out
    assert str(prev) in out, "original preview not re-surfaced on 2nd-gen reprint"
    assert str(iso) in out
    assert str(rev) in out, "original review.md not re-surfaced on 2nd-gen reprint"
    # and the new seed carries the pointers forward
    st2 = u1_request.read_request(r2["request_id"])
    assert st2["plates"][0]["preview_path"] == str(prev)
    assert st2["review_path"] == str(rev)



def test_reprint_refuses_when_printer_metadata_unavailable(monkeypatch, capsys):
    old_rid, fname = _seed_uploaded_request()
    monkeypatch.setattr(kw, "_printer_gcode_filenames", lambda: {fname})
    monkeypatch.setattr(kw, "_capture_bed_and_issue_token", _fake_bed_ok)
    monkeypatch.setattr(kw, "_printer_file_metadata", lambda fname: None)
    tok = u1_form.new_confirm_token(); u1_form.persist_confirm_token(tok, old_rid)
    res = kw._action_reprint_start(None, True, "test-op", tok)
    assert res["phase"] == "reprint_refused"
    assert res["reason"] == "printer_metadata_unavailable"


def test_reprint_refuses_when_printer_file_changed(monkeypatch, capsys):
    """Same filename, different bytes (review finding): stored size+modified
    from upload time must match the CURRENT printer file or the reprint
    refuses — filename existence is not content identity."""
    old_rid, fname = _seed_uploaded_request()
    st = u1_request.read_request(old_rid)
    plates = st["plates"]
    plates[0]["printer_file_size"] = 1000
    plates[0]["printer_file_modified"] = 1751000000.0
    u1_request.write_request(old_rid, plates=plates)
    monkeypatch.setattr(kw, "_printer_gcode_filenames", lambda: {fname})
    monkeypatch.setattr(kw, "_capture_bed_and_issue_token", _fake_bed_ok)
    monkeypatch.setattr(kw, "_printer_file_metadata",
                        lambda fname: {"size": 9999, "modified": 1751999999.0})
    tok = u1_form.new_confirm_token(); u1_form.persist_confirm_token(tok, old_rid)
    res = kw._action_reprint_start(None, True, "test-op", tok)
    assert res["phase"] == "reprint_refused"
    assert res["reason"] == "printer_file_changed"


def test_reprint_legacy_record_binds_current_metadata(monkeypatch, capsys):
    """Records from before metadata binding have no stored size/modified:
    the seed binds the CURRENT values so the pick-to-start window is
    covered from now on."""
    old_rid, fname = _seed_uploaded_request()
    monkeypatch.setattr(kw, "_printer_gcode_filenames", lambda: {fname})
    monkeypatch.setattr(kw, "_capture_bed_and_issue_token", _fake_bed_ok)
    tok = u1_form.new_confirm_token(); u1_form.persist_confirm_token(tok, old_rid)
    res = kw._action_reprint_start(None, True, "test-op", tok)
    st = u1_request.read_request(res["request_id"])
    assert st["plates"][0]["printer_file_size"] == 4242
    assert st["plates"][0]["printer_file_modified"] == 1751900000.0


def test_confirm_turn_refuses_on_printer_file_drift(monkeypatch, capsys):
    """The last check before Stage 2: file drifts between the bed-clear
    prompt and the confirmed yes -> structured refusal, no gate launch."""
    from types import SimpleNamespace
    old_rid, fname = _seed_uploaded_request()
    monkeypatch.setattr(kw, "_printer_gcode_filenames", lambda: {fname})
    monkeypatch.setattr(kw, "_capture_bed_and_issue_token", _fake_bed_ok)
    tok = u1_form.new_confirm_token(); u1_form.persist_confirm_token(tok, old_rid)
    res = kw._action_reprint_start(None, True, "test-op", tok)
    new_rid = res["request_id"]
    confirm = (u1_request.read_request(new_rid)["safety"]
               ["pending_bed_clear_start"]["confirm_token"])
    reached = {}
    monkeypatch.setattr(kw, "_invoke_stage2_gate",
                        lambda *a, **k: reached.setdefault("gate", True))
    # drift AFTER the prompt: current metadata no longer matches the seed
    monkeypatch.setattr(kw, "_printer_file_metadata",
                        lambda fname: {"size": 7777, "modified": 1751911111.0})
    args = SimpleNamespace(
        model=None, confirm_start=confirm, confirm_start_for=None,
        reprint=False, reprint_start=None, grace_cancel=False,
        json_events=True, operator="test-op", events_file=None,
        request_id=None, action=None, bed_clear_confirmed=False,
        pending_nonce=None, nozzle="0.4",
    )
    out_res = kw.run_kit_workflow(args)
    out = capsys.readouterr().out
    assert "gate" not in reached, "gate must not launch on drifted bytes"
    assert out_res["phase"] == "bed_clear_approval_rejected"
    assert "changed since upload" in out
