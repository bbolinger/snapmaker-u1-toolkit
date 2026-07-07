"""Tests for the v2.2 form-answers file handoff + form-mode emission.

The point of the handoff: answer content never rides through the model.
The gateway writes a file keyed by form_id; the workflow redeems it with
--form-answers-from. Redemption is nonce-like — bound to the form this
request emitted, single-use, replay redeems nothing.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import u1_form
import u1_kit_workflow as kw
import u1_request
from u1_orient import write_binary_stl

_GCODE = "T0\nG1 X10 Y10 F3000\nG1 X50 Y50 E1.5\n"


# --------------------------------------------------------------------------- #
# u1_form file helpers
# --------------------------------------------------------------------------- #

def test_write_then_consume_is_single_use():
    fid = u1_form.new_form_id()
    p = u1_form.write_answers_file(fid, {"tool": "T0"})
    assert p.is_file()
    obj = u1_form.read_and_consume_answers(fid)
    assert obj == {"tool": "T0"}
    assert not p.is_file(), "consumed file must be renamed away"
    with pytest.raises(FileNotFoundError):
        u1_form.read_and_consume_answers(fid)


def test_bad_form_id_rejected_before_touching_disk():
    for bad in ("../escape", "a/b", "x", "", "id with spaces"):
        with pytest.raises((ValueError, FileNotFoundError)):
            u1_form.write_answers_file(bad, {})


def test_answers_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv(u1_form.FORM_ANSWERS_DIR_ENV, str(tmp_path / "handoff"))
    fid = u1_form.new_form_id()
    p = u1_form.write_answers_file(fid, {"k": 1})
    assert p.parent == tmp_path / "handoff"


# --------------------------------------------------------------------------- #
# form-mode emission + redemption through the workflow
# --------------------------------------------------------------------------- #

def _cube(path, s):
    v = np.array([[0, 0, 0], [s, 0, 0], [s, s, 0], [0, s, 0],
                  [0, 0, s], [s, 0, s], [s, s, s], [0, s, s]], dtype=np.float32)
    faces = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    write_binary_stl(path, np.array([[v[a], v[b], v[c]] for a, b, c in faces],
                                    dtype=np.float32))
    return path


def _kit_zip(tmp_path, n=2):
    zp = tmp_path / "kit.zip"
    with zipfile.ZipFile(zp, "w") as z:
        for i in range(n):
            z.write(_cube(tmp_path / f"p{i}.stl", 20 + i), f"p{i}.stl")
    return zp


def _args(model, **kw_):
    base = dict(model=str(model), json_events=True, form_answers=None,
                form_answers_json=None, form_answers_from=None,
                interaction_mode=None, request_id=None, fresh=False,
                operator="test:unit", nozzle="0.4", out_dir=None,
                live_upload=False, on_collision=None, action=None)
    base.update(kw_)
    return SimpleNamespace(**base)


@pytest.fixture
def hermetic_commit(monkeypatch):
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


def _events(capsys):
    return [json.loads(l) for l in capsys.readouterr().out.splitlines()
            if l.strip().startswith("{")]


def test_form_mode_emits_schema_with_bound_form_id(tmp_path, hermetic_commit, capsys):
    zp = _kit_zip(tmp_path)
    res = kw.run_kit_workflow(_args(zp, interaction_mode="form"))
    assert res["phase"] == "awaiting_form"
    fid = res["form_id"]
    events = _events(capsys)
    form_ev = next(e for e in events if e.get("key") == "kit_form")
    assert form_ev["form_id"] == fid
    # The schema no longer rides the event (weak models echoed the nested
    # JSON as text instead of tool-calling) — it is PERSISTED, keyed by
    # form_id, and the plugin loads it from disk.
    assert "form_schema" not in form_ev
    assert f'form(form_id="{fid}")' in form_ev["instruction"]
    schema = json.loads((u1_form.schemas_dir() / f"{fid}.json").read_text())
    assert schema["version"] == 1 and schema["fields"]
    assert schema["submit"] == {"mode": "file", "form_id": fid}
    # v2.2: the redeem relays NO form_id (gemma4 mangled the random hex, live
    # 2026-07-03) — --redeem-pending-form makes the workflow read it off the
    # request. So the form_id must NOT appear in the redeem command at all.
    assert "--redeem-pending-form" in form_ev["next_command"]
    assert fid not in form_ev["next_command"]
    # answer content can't be in the command — only the opaque id
    st = u1_request.read_request(res["request_id"])
    assert st["phase"] == "awaiting_form" and st["form_id"] == fid


def test_form_mode_reinvocation_reuses_form_id(tmp_path, hermetic_commit, capsys):
    zp = _kit_zip(tmp_path)
    r1 = kw.run_kit_workflow(_args(zp, interaction_mode="form"))
    r2 = kw.run_kit_workflow(_args(zp, request_id=r1["request_id"],
                                   interaction_mode="form"))
    assert r2["form_id"] == r1["form_id"], "re-prompt must not orphan the pending form"


def test_redeem_answers_file_commits_without_model_carrying_answers(
        tmp_path, hermetic_commit, capsys):
    zp = _kit_zip(tmp_path)
    r1 = kw.run_kit_workflow(_args(zp, interaction_mode="form"))
    fid, rid = r1["form_id"], r1["request_id"]
    # the GATEWAY writes this — the model never sees the dict
    u1_form.write_answers_file(fid, {
        "parts": "all", "tool": "T0", "material": "PLA",
        "profile": 1, "supports": "no-supports", "action": "upload-only"})
    res = kw.run_kit_workflow(_args(zp, request_id=rid,
                                    form_answers_from=fid, live_upload=True))
    assert res["phase"] == "complete", res
    events = _events(capsys)
    stages = [e.get("stage") for e in events]
    assert "form_accepted" in stages
    # single-use: the file is consumed
    with pytest.raises(FileNotFoundError):
        u1_form.read_and_consume_answers(fid)


def test_redeem_pending_form_derives_form_id_from_request(
        tmp_path, hermetic_commit, capsys):
    """--redeem-pending-form commits WITHOUT the model relaying the form_id (it
    reads form_id off the request). gemma4 mangled the random-hex id in the
    verbatim redeem command (live 2026-07-03: f7b273e3536 → f7b273e3504 → 'form
    id mismatch'); deriving it removes the manglable token."""
    zp = _kit_zip(tmp_path)
    r1 = kw.run_kit_workflow(_args(zp, interaction_mode="form"))
    fid, rid = r1["form_id"], r1["request_id"]
    u1_form.write_answers_file(fid, {
        "parts": "all", "tool": "T0", "material": "PLA",
        "profile": 1, "supports": "no-supports", "action": "upload-only"})
    # NOTE: no form_answers_from — only the flag. The workflow finds fid itself.
    res = kw.run_kit_workflow(_args(zp, request_id=rid,
                                    redeem_pending_form=True, live_upload=True))
    assert res["phase"] == "complete", res
    with pytest.raises(FileNotFoundError):
        u1_form.read_and_consume_answers(fid)


def test_replayed_redemption_is_refused(tmp_path, hermetic_commit, capsys):
    zp = _kit_zip(tmp_path)
    r1 = kw.run_kit_workflow(_args(zp, interaction_mode="form"))
    fid, rid = r1["form_id"], r1["request_id"]
    u1_form.write_answers_file(fid, {
        "tool": "T0", "material": "PLA", "profile": 1, "action": "upload-only"})
    kw.run_kit_workflow(_args(zp, request_id=rid, form_answers_from=fid,
                              live_upload=True))
    res2 = kw.run_kit_workflow(_args(zp, request_id=rid, form_answers_from=fid,
                                     live_upload=True))
    assert res2["phase"] == "form_rejected"
    assert any("redeem" in e for e in res2["errors"])


def test_duplicate_redeem_reemits_bed_clear_not_form(tmp_path, hermetic_commit, capsys):
    """v2.2.2: a SECOND --redeem-pending-form on a request that already sliced +
    uploaded (a pending_bed_clear_start survives in safety) must re-surface the
    SAME bed-clear prompt with the SAME confirm token, NOT render a fresh form. A
    small model relaying the redeem twice stranded the operator in a form loop
    (live 2026-07-06): the phase had been reset to awaiting_form but the pending
    object survived, so the guard keys off that."""
    zp = _kit_zip(tmp_path)
    r1 = kw.run_kit_workflow(_args(zp, interaction_mode="form"))
    rid = r1["request_id"]
    tok = u1_form.new_confirm_token()
    safety = dict((u1_request.read_request(rid) or {}).get("safety") or {})
    safety["pending_bed_clear_start"] = {
        "nonce": "n0", "confirm_token": tok, "prompt_key": "bed_clear_start"}
    safety["snapshot_path"] = str(tmp_path / "bed.jpg")
    u1_request.write_request(rid, safety=safety)
    u1_form.persist_confirm_token(tok, rid)
    capsys.readouterr()  # clear prior output
    res = kw.run_kit_workflow(_args(zp, request_id=rid,
                                    redeem_pending_form=True, live_upload=True))
    assert res["phase"] == "awaiting_bed_clear_start", res
    out = capsys.readouterr().out
    assert "bed_clear_start" in out
    assert tok not in out   # model-free YES: the token re-arms on disk, not in the event
    assert '"kit_form"' not in out                   # did NOT re-render a fresh form


def test_fresh_redeem_ignores_stale_pending(tmp_path, hermetic_commit, capsys):
    """v2.2.2: a FIRST redeem (its answers file present) must PROCEED and slice
    the fresh answers even if a STALE pending_bed_clear_start from a prior run
    lingers — request ids are content-derived, so re-uploading the same kit
    reuses the request. The idempotency guard must fire ONLY when the answers are
    gone (a genuine duplicate), never re-surfacing the old plate over fresh
    answers."""
    zp = _kit_zip(tmp_path)
    r1 = kw.run_kit_workflow(_args(zp, interaction_mode="form"))
    fid, rid = r1["form_id"], r1["request_id"]
    # A stale pending from a "previous" run lingering in safety.
    safety = dict((u1_request.read_request(rid) or {}).get("safety") or {})
    safety["pending_bed_clear_start"] = {
        "nonce": "stale", "confirm_token": u1_form.new_confirm_token()}
    u1_request.write_request(rid, safety=safety)
    # Fresh answers for THIS form are present.
    u1_form.write_answers_file(fid, {
        "parts": "all", "tool": "T0", "material": "PLA",
        "profile": 1, "supports": "no-supports", "action": "upload-only"})
    res = kw.run_kit_workflow(_args(zp, request_id=rid,
                                    redeem_pending_form=True, live_upload=True))
    assert res["phase"] == "complete", res  # proceeded; did NOT re-surface old plate


def test_mismatched_form_id_refused_and_file_preserved(tmp_path, hermetic_commit, capsys):
    zp = _kit_zip(tmp_path)
    r1 = kw.run_kit_workflow(_args(zp, interaction_mode="form"))
    rid = r1["request_id"]
    other = u1_form.new_form_id()
    u1_form.write_answers_file(other, {"tool": "T0", "material": "PLA", "profile": 1})
    res = kw.run_kit_workflow(_args(zp, request_id=rid, form_answers_from=other))
    assert res["phase"] == "form_rejected"
    assert any("mismatch" in e for e in res["errors"])
    # refused BEFORE consuming — the file survives for the right redeemer
    assert u1_form.read_and_consume_answers(other)


def test_text_mode_untouched_by_default(tmp_path, hermetic_commit, capsys, monkeypatch):
    monkeypatch.delenv("U1_INTERACTION_MODE", raising=False)
    zp = _kit_zip(tmp_path)
    res = kw.run_kit_workflow(_args(zp))
    assert res["phase"] == "awaiting_parts"  # staged flow remains the default
