"""Model-free YES boundary (post-incident 2026-07-07).

The agent model fired the emitted confirm command itself — no operator YES
ever happened — and the operator's Cancel arrived as a mid-turn interrupt,
which bypasses gateway hooks. Structural changes under test:

  1. bed_clear_start events carry NO start command; the workflow arms a
     marker file that only the u1_confirm_start gateway hook redeems from
     the operator's literal YES message. The model has nothing to fire.
  2. `--grace-cancel` is a model-relayable SAFE-direction fallback: it can
     only ever stop a pending start, never begin one.

Hardening pass (review findings):

  3. The marker is OPAQUE — no confirm_cmd, no token, no log_path. The
     hook builds its own argv (`--confirm-start-for <rid>`) from constants
     and the workflow resolves the persisted single-use token server-side,
     so a same-UID writer to /tmp can no longer hand the gateway a command.
  4. YES is bound to the operator: marker platform/operator_user_id must
     match the message context; markers without binding refuse.
  5. Claim-then-spawn: atomic rename claims the marker before Popen, and a
     failed spawn restores it instead of burning the window.
  6. Expiry fails closed: missing -> deleted, malformed or >24h out ->
     quarantined to .bad, expired -> deleted.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import u1_form
import u1_request
import u1_kit_workflow as kw

_HOOK_PATH = (Path(__file__).resolve().parent.parent
              / "tools" / "hermes_hooks" / "u1_confirm_start" / "handler.py")
_spec = importlib.util.spec_from_file_location("u1_confirm_start_handler", _HOOK_PATH)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

# The operator identity used throughout: what the workflow binds at arm
# time and what the gateway context must present at YES time.
_OP_PLATFORM = "telegram"
_OP_USER = "555000111"

# Sentinel: `_marker(..., field=_OMIT)` removes the field from the marker.
_OMIT = object()


@pytest.fixture(autouse=True)
def _no_real_watchdogs(monkeypatch):
    """Arm calls spawn a detached expiry watchdog; tests must not."""
    monkeypatch.setattr(kw, "_spawn_confirm_expiry_watchdog",
                        lambda rid, fn, gen="": None)


@pytest.fixture(autouse=True)
def _no_real_refusal_dms(monkeypatch):
    """Hook refusals DM the bound operator; tests capture instead."""
    sent = []
    monkeypatch.setattr(hook, "_notify_operator",
                        lambda text: sent.append(text))
    hook._test_refusal_dms = sent
    yield
    hook._test_refusal_dms = None


@pytest.fixture(autouse=True)
def _stable_printer_metadata(monkeypatch):
    """Reprint-style seeds bind printer-side size+modified; hermetic
    default keeps them stable so identity checks pass unless a test
    drifts them on purpose."""
    monkeypatch.setattr(kw, "_printer_file_metadata",
                        lambda fname: {"size": 4242, "modified": 1751900000.0})


@pytest.fixture()
def pending_dir(tmp_path, monkeypatch):
    d = tmp_path / "pending_confirm"
    monkeypatch.setattr(hook, "PENDING_DIR", d)
    monkeypatch.setattr(kw, "_PENDING_CONFIRM_DIR", d)
    monkeypatch.setattr(hook, "LOG_FILE", tmp_path / "hook.log")
    monkeypatch.setattr(kw.u1_config, "get_operator_binding",
                        lambda: (_OP_PLATFORM, _OP_USER))
    return d


def _marker(pending_dir, rid="u1_2026_0707_aaa111", expired=False, **over):
    entry = {
        "request_id": rid,
        "filename": "plate1.gcode",
        "platform": _OP_PLATFORM,
        "operator_user_id": _OP_USER,
        "operator_chat_id": _OP_USER,   # private-DM assumption: chat == user
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc)
                       + timedelta(minutes=-5 if expired else 10)).isoformat(),
    }
    entry.update(over)
    entry = {k: v for k, v in entry.items() if v is not _OMIT}
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / f"{rid}.json").write_text(json.dumps(entry))
    return entry


def _ctx(text="yes", platform=_OP_PLATFORM, user_id=_OP_USER,
         chat_id=_OP_USER, chat_type="private"):
    return {"message": text, "platform": platform, "user_id": user_id,
            "chat_id": chat_id, "chat_type": chat_type}


def _run(context):
    asyncio.run(hook.handle("agent:start", context))


def _expected_cmd(rid):
    """The one and only argv shape the hook may spawn — built by the hook
    from its own constants (hook.WORKFLOW_PY resolves per deployment),
    never from marker content."""
    return ["python3", hook.WORKFLOW_PY,
            "--confirm-start-for", rid, "--json-events"]


def _strip_claim_id(cmd):
    """Drop the --confirm-claim-id <uuid> pair so an argv can be compared to
    the fixed _expected_cmd shape (the id is a random per-spawn uuid)."""
    out, i = [], 0
    while i < len(cmd):
        if cmd[i] == "--confirm-claim-id":
            i += 2
            continue
        out.append(cmd[i])
        i += 1
    return out


def _hook_log(tmp_path):
    p = tmp_path / "hook.log"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


# ---------- YES parsing: a confirmation must be ONLY a confirmation ----------

@pytest.mark.parametrize("text,want", [
    ("yes", (True, None)),
    ("YES", (True, None)),
    ("Yes!!", (True, None)),
    ("yes.", (True, None)),
    ("/yes", (True, None)),
    ("yes aaa111", (True, "aaa111")),
    ("YES bbb222", (True, "bbb222")),
    ("yes please", (False, None)),
    ("yes but wait", (False, None)),
    ("yesterday", (False, None)),
    ("y", (False, None)),          # too short to be an unambiguous start
    ("start", (False, None)),      # not a confirm keyword by design
    ("no", (False, None)),
])
def test_yes_parse_matrix(text, want):
    assert hook._parse_yes_message(text) == want


# ---------- redemption ----------

def test_single_window_yes_spawns_and_single_fires(pending_dir, monkeypatch):
    entry = _marker(pending_dir)
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd) or SimpleNamespace(pid=1))
    _run(_ctx("Yes!"))
    assert [_strip_claim_id(c) for c in spawned] == [_expected_cmd(entry["request_id"])]
    assert not (pending_dir / f"{entry['request_id']}.json").exists()
    # Q3: the claim is RETAINED for the child (not deleted on spawn); its pid
    # is this live test process, so the reaper leaves it and a second YES
    # still finds no armed window (claimed files are never windows).
    assert len(list(pending_dir.glob(f"{entry['request_id']}.claimed.*.json"))) == 1
    _run(_ctx())
    assert len(spawned) == 1


def test_bare_yes_with_two_windows_refuses(pending_dir, monkeypatch):
    _marker(pending_dir, rid="u1_2026_0707_aaa111")
    _marker(pending_dir, rid="u1_2026_0707_bbb222")
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd) or SimpleNamespace(pid=1))
    _run(_ctx())
    assert spawned == []                      # START NEVER GUESSES
    assert len(list(pending_dir.glob("*.json"))) == 2
    # code-scoped yes picks exactly one
    _run(_ctx("yes bbb222"))
    assert [_strip_claim_id(c) for c in spawned] == [_expected_cmd("u1_2026_0707_bbb222")]
    assert (pending_dir / "u1_2026_0707_aaa111.json").exists()


def test_prose_yes_never_touches_markers(pending_dir, monkeypatch):
    _marker(pending_dir)
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd))
    _run(_ctx("yes let's do the switch one"))
    assert spawned == [] and len(list(pending_dir.glob("*.json"))) == 1


# ---------- marker opacity: argv is NEVER read from the marker ----------

def test_hostile_confirm_cmd_field_is_ignored(pending_dir, monkeypatch):
    """A marker smuggling the old confirm_cmd/log_path fields gets the
    hook-built argv anyway — marker content contributes ONLY a validated
    request id."""
    entry = _marker(pending_dir,
                    confirm_cmd=["/bin/sh", "-c", "echo owned > /tmp/owned"],
                    log_path="/tmp/somewhere/else.log")
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd) or SimpleNamespace(pid=1))
    _run(_ctx())
    assert [_strip_claim_id(c) for c in spawned] == [_expected_cmd(entry["request_id"])]


@pytest.mark.parametrize("bad_rid", [
    "u1_x; rm -rf /",
    "../../etc",
    "u1_UPPER",
    "u1_x rm",
    "",
])
def test_hostile_request_id_refused_and_quarantined(pending_dir, monkeypatch,
                                                    tmp_path, bad_rid):
    """The request id is the marker's only argv contribution, so it gets
    the strict shape check; anything else is quarantined, not spawned."""
    _marker(pending_dir, request_id=bad_rid)  # file name stays sane
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd))
    _run(_ctx())
    assert spawned == []
    assert list(pending_dir.glob("*.json")) == []
    assert len(list(pending_dir.glob("*.json.bad"))) == 1
    assert any(e["event"] == "confirm_marker_bad_request_id_quarantined"
               for e in _hook_log(tmp_path))


def test_unreadable_marker_quarantined(pending_dir, monkeypatch, tmp_path):
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / "u1_2026_0707_aaa111.json").write_text("{not json")
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd))
    _run(_ctx())
    assert spawned == []
    assert list(pending_dir.glob("*.json")) == []
    assert len(list(pending_dir.glob("*.json.bad"))) == 1


# ---------- operator binding: the YES must come from the operator ----------

def test_yes_from_bound_operator_spawns(pending_dir, monkeypatch):
    entry = _marker(pending_dir)
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd) or SimpleNamespace(pid=1))
    _run(_ctx(platform=_OP_PLATFORM, user_id=_OP_USER))
    assert [_strip_claim_id(c) for c in spawned] == [_expected_cmd(entry["request_id"])]


def test_yes_from_wrong_user_refuses(pending_dir, monkeypatch, tmp_path):
    entry = _marker(pending_dir)
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd))
    _run(_ctx(user_id="999888777"))
    assert spawned == []
    # refusal happens BEFORE the claim — the window stays armed for the
    # real operator
    assert (pending_dir / f"{entry['request_id']}.json").exists()
    assert any(e["event"] == "confirm_refused_operator_mismatch"
               for e in _hook_log(tmp_path))


def test_yes_from_wrong_platform_refuses(pending_dir, monkeypatch, tmp_path):
    entry = _marker(pending_dir)
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd))
    _run(_ctx(platform="discord", user_id=_OP_USER))
    assert spawned == []
    assert (pending_dir / f"{entry['request_id']}.json").exists()
    assert any(e["event"] == "confirm_refused_operator_mismatch"
               for e in _hook_log(tmp_path))


def test_marker_without_binding_refuses(pending_dir, monkeypatch, tmp_path):
    """Legacy marker shape (or binding config unset at arm time): no
    platform/operator_user_id -> the hook cannot verify WHO said yes, so
    nobody can. Fail closed, loudly."""
    entry = _marker(pending_dir, platform=_OMIT, operator_user_id=_OMIT)
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd))
    _run(_ctx())
    assert spawned == []
    assert (pending_dir / f"{entry['request_id']}.json").exists()
    assert any(e["event"] == "confirm_refused_marker_missing_binding"
               for e in _hook_log(tmp_path))


def test_int_user_id_normalizes_against_marker_string(pending_dir, monkeypatch):
    """Gateways deliver user_id as int or str depending on platform; the
    comparison is string-vs-string on both sides."""
    entry = _marker(pending_dir)
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd) or SimpleNamespace(pid=1))
    _run(_ctx(user_id=int(_OP_USER)))
    assert [_strip_claim_id(c) for c in spawned] == [_expected_cmd(entry["request_id"])]


# ---------- claim-then-spawn ----------

def test_spawn_failure_restores_marker(pending_dir, monkeypatch, tmp_path):
    """A failed Popen must not burn the window: the claimed file goes back
    to its armed name so the operator's next YES retries."""
    entry = _marker(pending_dir)
    def _boom(cmd, **kw_):
        raise OSError("fork failed")
    monkeypatch.setattr(hook.subprocess, "Popen", _boom)
    _run(_ctx())
    assert (pending_dir / f"{entry['request_id']}.json").exists()
    assert list(pending_dir.glob("*claimed*")) == []
    events = [e["event"] for e in _hook_log(tmp_path)]
    assert "confirm_spawn_failed" in events
    assert "confirm_spawn_failed_marker_restored" in events
    # and the restored marker still works on the next YES
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd) or SimpleNamespace(pid=1))
    _run(_ctx())
    assert [_strip_claim_id(c) for c in spawned] == [_expected_cmd(entry["request_id"])]
    # Q3: successful retry retains the claim for the child
    assert len(list(pending_dir.glob("*.claimed.*.json"))) == 1


def test_second_yes_mid_claim_finds_nothing(pending_dir, monkeypatch):
    """The rename IS the claim: by the time Popen runs, a concurrent second
    YES sees zero armed windows and cannot double-spawn."""
    _marker(pending_dir)
    calls = []
    def _popen(cmd, **kw_):
        calls.append(cmd)
        if len(calls) == 1:
            # marker already claimed (renamed) at this point — this is
            # exactly what a second YES arriving mid-spawn would load
            assert hook._load_pending_windows() == []
        return SimpleNamespace(pid=1)
    monkeypatch.setattr(hook.subprocess, "Popen", _popen)
    _run(_ctx())
    assert len(calls) == 1


def test_stale_claimed_file_is_not_a_window(pending_dir, monkeypatch):
    """A .claimed. file (in-flight or crashed spawn) never counts as an
    armed window."""
    pending_dir.mkdir(parents=True, exist_ok=True)
    import os as _os
    entry = _marker(pending_dir)
    src = pending_dir / f"{entry['request_id']}.json"
    # a LIVE pid = confirm genuinely in flight -> never a window (the reaper
    # only restores claims whose pid is dead).
    src.rename(pending_dir / f"{entry['request_id']}.claimed.{_os.getpid()}.json")
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd))
    _run(_ctx())
    assert spawned == []


# ---------- expiry fails closed ----------

def test_expired_marker_is_deleted(pending_dir, monkeypatch, tmp_path):
    entry = _marker(pending_dir, expired=True)
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd))
    _run(_ctx())
    assert spawned == []
    assert not (pending_dir / f"{entry['request_id']}.json").exists()
    assert any(e["event"] == "confirm_marker_expired_deleted"
               for e in _hook_log(tmp_path))


def test_marker_missing_expiry_is_deleted(pending_dir, monkeypatch, tmp_path):
    entry = _marker(pending_dir, expires_at=_OMIT)
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd))
    _run(_ctx())
    assert spawned == []
    assert list(pending_dir.iterdir()) == []   # deleted, not quarantined
    assert any(e["event"] == "confirm_marker_missing_expiry_deleted"
               for e in _hook_log(tmp_path))


@pytest.mark.parametrize("bad_expiry", ["soonish", "2026-13-45T99:99:99",
                                        "2026-07-07T12:00:00"])  # tz-naive
def test_malformed_expiry_is_quarantined(pending_dir, monkeypatch, tmp_path,
                                         bad_expiry):
    entry = _marker(pending_dir, expires_at=bad_expiry)
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd))
    _run(_ctx())
    assert spawned == []
    assert not (pending_dir / f"{entry['request_id']}.json").exists()
    assert (pending_dir / f"{entry['request_id']}.json.bad").exists()
    assert any(e["event"] == "confirm_marker_bad_expiry_quarantined"
               for e in _hook_log(tmp_path))


def test_far_future_expiry_is_quarantined(pending_dir, monkeypatch, tmp_path):
    """The workflow arms 15-minute windows; a marker claiming to be live
    for days was not written by the workflow."""
    entry = _marker(pending_dir, expires_at=(
        datetime.now(timezone.utc) + timedelta(hours=25)).isoformat())
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd))
    _run(_ctx())
    assert spawned == []
    assert (pending_dir / f"{entry['request_id']}.json.bad").exists()
    assert any(e["event"] == "confirm_marker_expiry_too_far_quarantined"
               for e in _hook_log(tmp_path))


# ---------- workflow side: arming ----------

def test_arm_writes_opaque_marker_and_disarm_removes(pending_dir):
    kw._arm_pending_confirm("u1_2026_0707_ccc333", "p.gcode", "telegram:brent")
    m = json.loads((pending_dir / "u1_2026_0707_ccc333.json").read_text())
    assert set(m) == {"request_id", "filename", "generation", "platform", "operator_chat_id",
                      "operator_user_id", "created_at", "expires_at"}
    assert m["platform"] == _OP_PLATFORM
    assert m["operator_user_id"] == _OP_USER
    assert m["expires_at"] > m["created_at"]
    kw._disarm_pending_confirm("u1_2026_0707_ccc333")
    assert not (pending_dir / "u1_2026_0707_ccc333.json").exists()
    kw._disarm_pending_confirm("u1_2026_0707_ccc333")  # idempotent


def test_arm_without_binding_config_warns_but_still_arms(pending_dir,
                                                         monkeypatch, capsys):
    """Missing binding config is diagnosed at ARM time (where the operator
    can fix it), not just silently enforced at YES time. The marker is
    still written — without binding fields, so the hook fails closed."""
    monkeypatch.setattr(kw.u1_config, "get_operator_binding", lambda: None)
    audits = []
    monkeypatch.setattr(kw, "_audit",
                        lambda rid, ev, op, **d: audits.append((rid, ev)))
    kw._arm_pending_confirm("u1_2026_0707_ddd444", "p.gcode", "telegram:brent",
                            None, True)
    m = json.loads((pending_dir / "u1_2026_0707_ddd444.json").read_text())
    assert "platform" not in m and "operator_user_id" not in m
    out = capsys.readouterr().out
    ev = next(json.loads(l) for l in out.splitlines()
              if '"confirm_binding_unconfigured"' in l)
    assert ev["request_id"] == "u1_2026_0707_ddd444"
    assert ("u1_2026_0707_ddd444", "confirm_binding_unconfigured") in audits


# ---------- workflow side: --confirm-start-for redemption ----------

def _seed_uploaded_request(model="widget", tool="T1", material="PETG"):
    """Same hermetic seed shape as test_reprint.py: an uploaded request the
    reprint turn can take to the bed-clear boundary."""
    rid = u1_request.generate_request_id()
    fname = f"{model}_plate1.gcode"
    d = Path(u1_request.ensure_request_dir(rid))
    (d / "review.md").write_text("# review\n")
    u1_request.write_request(
        rid,
        model_file=f"doc_a1b2c3d4e5f6_{model}.zip",
        tool=tool, material=material, request_revision=1,
        printer_storage_filename=fname,
        out_dir=str(d),
        plates=[{"plate_idx": 1, "gcode_hash": "sha256:abc123",
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


def _confirm_args(**over):
    base = dict(model=None, confirm_start=None, confirm_start_for=None,
                reprint=False, reprint_start=None, json_events=True,
                operator="test-op", events_file=None, request_id=None,
                action=None, bed_clear_confirmed=False, pending_nonce=None,
                nozzle="0.4")
    base.update(over)
    return SimpleNamespace(**base)


def test_confirm_start_for_reaches_same_redemption(pending_dir, monkeypatch,
                                                   capsys):
    """--confirm-start-for resolves the request's PERSISTED token and then
    delegates to the exact --confirm-start path: same single-use claim,
    same gate turn."""
    old_rid, fname = _seed_uploaded_request()
    monkeypatch.setattr(kw, "_printer_gcode_filenames", lambda: {fname})
    monkeypatch.setattr(kw, "_capture_bed_and_issue_token", _fake_bed_ok)
    tok = u1_form.new_confirm_token()
    u1_form.persist_confirm_token(tok, old_rid)
    res = kw._action_reprint_start(None, True, "test-op", tok)
    rid = res["request_id"]
    persisted = (u1_request.read_request(rid)["safety"]
                 ["pending_bed_clear_start"]["confirm_token"])

    resolved = {}
    _orig_resolve = u1_form.resolve_confirm_token
    def _spy(token, *a, **k):
        resolved["token"] = token
        return _orig_resolve(token, *a, **k)
    monkeypatch.setattr(kw.u1_form, "resolve_confirm_token", _spy)
    reached = {}
    def _fake_gate(gate_py, argv, out_dir):
        reached["argv"] = argv
        return None  # None == grace window opened, gate detached
    monkeypatch.setattr(kw, "_invoke_stage2_gate", _fake_gate)

    out_res = kw.run_kit_workflow(_confirm_args(confirm_start_for=rid))
    out = capsys.readouterr().out
    assert reached, f"gate turn never reached; result={out_res} out={out[:400]}"
    assert fname in " ".join(reached["argv"])
    # the redemption consumed the SAME persisted token --confirm-start would
    assert resolved["token"] == persisted
    # marker disarmed by the confirm turn
    assert not (pending_dir / f"{rid}.json").exists()


def test_confirm_start_for_is_single_use(pending_dir, monkeypatch, capsys):
    """A second --confirm-start-for finds the token already consumed —
    replaying the hook's spawn cannot double-start."""
    old_rid, fname = _seed_uploaded_request()
    monkeypatch.setattr(kw, "_printer_gcode_filenames", lambda: {fname})
    monkeypatch.setattr(kw, "_capture_bed_and_issue_token", _fake_bed_ok)
    tok = u1_form.new_confirm_token()
    u1_form.persist_confirm_token(tok, old_rid)
    rid = kw._action_reprint_start(None, True, "test-op", tok)["request_id"]
    monkeypatch.setattr(kw, "_invoke_stage2_gate",
                        lambda gate_py, argv, out_dir: None)
    kw.run_kit_workflow(_confirm_args(confirm_start_for=rid))
    capsys.readouterr()
    res2 = kw.run_kit_workflow(_confirm_args(confirm_start_for=rid))
    # depending on how far the first confirm advanced the request, the
    # replay dies on the consumed token or on the absent pending — both
    # are structured refusals with no start
    assert res2["phase"] in ("bed_clear_confirm_token_invalid",
                             "bed_clear_confirm_no_pending")


def test_confirm_start_for_refuses_when_no_pending(capsys):
    res = kw.run_kit_workflow(
        _confirm_args(confirm_start_for="u1_2026_0707_eee999"))
    assert res["phase"] == "bed_clear_confirm_no_pending"
    out = capsys.readouterr().out
    ev = next(json.loads(l) for l in out.splitlines()
              if '"bed_clear_confirm_no_pending"' in l)
    assert ev["request_id"] == "u1_2026_0707_eee999"
    assert "nothing was started" in ev["reason"]


@pytest.mark.parametrize("bad_rid", ["../../etc", "u1_x; rm -rf /", "u1_X"])
def test_confirm_start_for_refuses_hostile_request_id(capsys, monkeypatch,
                                                      bad_rid):
    reads = []
    monkeypatch.setattr(kw.u1_request, "read_request",
                        lambda rid: reads.append(rid) or None)
    res = kw.run_kit_workflow(_confirm_args(confirm_start_for=bad_rid))
    assert res["phase"] == "bed_clear_confirm_bad_request_id"
    assert reads == []   # refused before touching request storage


# ---------- config: operator binding resolution ----------

def test_operator_binding_resolution_order(monkeypatch):
    import u1_config
    for var in ("U1_OPERATOR_BINDING", "TELEGRAM_ALLOWED_USERS",
                "TELEGRAM_HOME_CHANNEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(u1_config, "_load_file", lambda: {})
    assert u1_config.get_operator_binding() is None
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "42")
    assert u1_config.get_operator_binding() == ("telegram", "42")
    # several allowed users can't name THE operator — falls to home channel
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "50, 51")
    assert u1_config.get_operator_binding() == ("telegram", "42")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "77")
    assert u1_config.get_operator_binding() == ("telegram", "77")
    monkeypatch.setattr(u1_config, "_load_file",
                        lambda: {"operator_binding": "telegram:88"})
    assert u1_config.get_operator_binding() == ("telegram", "88")
    monkeypatch.setenv("U1_OPERATOR_BINDING", "Telegram:99")
    assert u1_config.get_operator_binding() == ("telegram", "99")


def test_operator_binding_malformed_explicit_fails_closed(monkeypatch):
    """An explicit override that doesn't parse must surface as missing —
    not silently fall through to a different identity."""
    import u1_config
    monkeypatch.setenv("U1_OPERATOR_BINDING", "just-a-name")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "42")
    monkeypatch.setattr(u1_config, "_load_file", lambda: {})
    assert u1_config.get_operator_binding() is None


# ---------- grace cancel: the model-relayable SAFE direction ----------

def test_grace_cancel_touches_all_active_markers(tmp_path, monkeypatch, capsys):
    cdir = tmp_path / "pending_cancel"
    cdir.mkdir()
    monkeypatch.setenv("U1_PENDING_CANCEL_DIR", str(cdir))
    audits = []
    monkeypatch.setattr(kw, "_audit",
                        lambda rid, ev, op, **d: audits.append((rid, ev)))
    m1 = tmp_path / "req1.marker"; m2 = tmp_path / "req2.marker"
    exp = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    (cdir / "a.json").write_text(json.dumps(
        {"request_id": "u1_a", "cancel_marker": str(m1), "expires_at": exp}))
    (cdir / "b.json").write_text(json.dumps(
        {"request_id": "u1_b", "cancel_marker": str(m2), "expires_at": exp}))
    (cdir / "old.json").write_text(json.dumps(
        {"request_id": "u1_old", "cancel_marker": str(tmp_path / "old.marker"),
         "expires_at": (datetime.now(timezone.utc)
                        - timedelta(minutes=1)).isoformat()}))
    res = kw._action_grace_cancel(True, "brent")
    assert sorted(res["cancelled"]) == ["u1_a", "u1_b"]
    assert m1.exists() and m2.exists()
    assert not (tmp_path / "old.marker").exists()
    assert ("u1_a", "grace_cancel_via_workflow") in audits


def test_grace_cancel_with_nothing_pending_is_calm(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("U1_PENDING_CANCEL_DIR", str(tmp_path / "nope"))
    res = kw._action_grace_cancel(True, "brent")
    assert res["cancelled"] == []
    assert "No active grace window" in capsys.readouterr().out



# ---------- conversation binding (final release review) ----------

def test_yes_from_wrong_chat_refuses(pending_dir, monkeypatch):
    """Same operator, different conversation: refuses. The YES belongs to
    the private DM the window was armed for."""
    _marker(pending_dir)
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd))
    asyncio.run(hook.handle("agent:start", _ctx(chat_id="999999")))
    assert spawned == []
    assert len(list(pending_dir.glob("*.json"))) == 1


def test_yes_from_group_chat_refuses(pending_dir, monkeypatch):
    """Model-free start is a private-DM feature: any non-private chat_type
    refuses outright, even from the right operator in the right chat id."""
    _marker(pending_dir)
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd))
    asyncio.run(hook.handle("agent:start", _ctx(chat_type="group")))
    assert spawned == []


def test_marker_without_chat_binding_refuses(pending_dir, monkeypatch):
    """Legacy marker shape (no operator_chat_id) fails closed."""
    entry = _marker(pending_dir)
    import json as _json
    raw = _json.loads((pending_dir / f"{entry['request_id']}.json").read_text())
    raw.pop("operator_chat_id")
    (pending_dir / f"{entry['request_id']}.json").write_text(_json.dumps(raw))
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd))
    asyncio.run(hook.handle("agent:start", _ctx()))
    assert spawned == []


@pytest.mark.parametrize("ctype", ["private", "dm", "direct", "im"])
def test_private_dm_chat_type_synonyms_succeed(pending_dir, monkeypatch, ctype):
    """Hermes says "dm" where Telegram says "private" (live 2026-07-07:
    the operator's own DM was refused). Every one-on-one synonym passes;
    anything else still refuses."""
    _marker(pending_dir)
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd) or SimpleNamespace(pid=1))
    asyncio.run(hook.handle("agent:start", _ctx(chat_type=ctype)))
    assert len(spawned) == 1


def test_empty_chat_type_with_right_chat_id_succeeds(pending_dir, monkeypatch):
    """Some gateway paths omit chat_type; the chat_id equality still
    gates. Absence of evidence about the chat KIND is tolerated only when
    the conversation identity itself matches."""
    _marker(pending_dir)
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd) or SimpleNamespace(pid=1))
    asyncio.run(hook.handle("agent:start", _ctx(chat_type="")))
    assert len(spawned) == 1


def test_arm_marker_carries_chat_binding(pending_dir, tmp_path, monkeypatch):
    import json as _json
    monkeypatch.setattr(kw.u1_request, "request_dir",
                        lambda rid: tmp_path / "req" / rid)
    monkeypatch.setattr(kw.u1_config, "get_operator_binding",
                        lambda: ("telegram", "8131922235"))
    kw._arm_pending_confirm("u1_2026_0707_chat01", "p.gcode", "brent")
    m = _json.loads((pending_dir / "u1_2026_0707_chat01.json").read_text())
    assert m["operator_chat_id"] == "8131922235"



# ---------- silence is not an outcome (operator feedback 2026-07-07) ----------

def test_wrong_user_refusal_notifies_bound_operator(pending_dir, monkeypatch):
    _marker(pending_dir)
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: pytest.fail("must not spawn"))
    asyncio.run(hook.handle("agent:start", _ctx(user_id="42")))
    assert hook._test_refusal_dms and "refused" in hook._test_refusal_dms[0]
    assert "Nothing was started" in hook._test_refusal_dms[0]


def test_wrong_chat_refusal_notifies_with_guidance(pending_dir, monkeypatch):
    _marker(pending_dir)
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: pytest.fail("must not spawn"))
    asyncio.run(hook.handle("agent:start", _ctx(chat_id="999999")))
    assert any("wrong conversation" in t for t in hook._test_refusal_dms)


def test_group_chat_refusal_notifies(pending_dir, monkeypatch):
    _marker(pending_dir)
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: pytest.fail("must not spawn"))
    asyncio.run(hook.handle("agent:start", _ctx(chat_type="group")))
    assert any("private DM" in t for t in hook._test_refusal_dms)


def test_no_yes_no_dm(pending_dir, monkeypatch):
    """A non-YES message near an armed window stays silent — the refusal
    DMs exist for refused YESes, not for ambient chatter."""
    _marker(pending_dir)
    asyncio.run(hook.handle("agent:start", _ctx(text="how long will it take?")))
    assert hook._test_refusal_dms == []


def test_arm_spawns_expiry_watchdog(pending_dir, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(kw, "_spawn_confirm_expiry_watchdog",
                        lambda rid, fn, gen="": calls.append((rid, fn)))
    monkeypatch.setattr(kw.u1_request, "request_dir",
                        lambda rid: tmp_path / "req" / rid)
    monkeypatch.setattr(kw.u1_config, "get_operator_binding",
                        lambda: ("telegram", "8131922235"))
    kw._arm_pending_confirm("u1_2026_0707_wd0001", "p.gcode", "brent")
    assert calls == [("u1_2026_0707_wd0001", "p.gcode")]



# ---------- Q3: child-ack + orphan claim reaper (2026-07-08) ----------

def _dead_pid():
    import subprocess as _sp
    d = _sp.Popen(["true"]); d.wait()
    return d.pid           # reliably dead now


def test_spawn_leaves_claim_for_child(pending_dir, monkeypatch):
    """Q3: the hook no longer deletes the claim on spawn — the child owns it.
    Previously the YES was consumed the instant Popen returned."""
    entry = _marker(pending_dir)
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd) or SimpleNamespace(pid=1))
    asyncio.run(hook.handle("agent:start", _ctx()))
    assert spawned, "should have spawned"
    rid = entry["request_id"]
    assert not (pending_dir / f"{rid}.json").exists()          # renamed to claim
    claims = list(pending_dir.glob(f"{rid}.claimed.*.json"))
    assert len(claims) == 1, "claim must be RETAINED, not deleted on spawn"


def _claim(pending_dir, rid, cid, *, child_pid=None, minutes=5, body=None):
    """Write a claim file the reaper's way: opaque claim_id filename, liveness
    in the CONTENT as child_pid (audit 2026-07-09)."""
    pending_dir.mkdir(parents=True, exist_ok=True)
    st = {"request_id": rid,
          "expires_at": (datetime.now(timezone.utc)
                         + timedelta(minutes=minutes)).isoformat()}
    if child_pid is not None:
        st["child_pid"] = child_pid
    if body:
        st.update(body)
    f = pending_dir / f"{rid}.claimed.{cid}.json"
    f.write_text(json.dumps(st))
    return f


def test_spawn_records_actual_child_pid_not_gateway(pending_dir, monkeypatch):
    """AUDIT #2 handoff assertion: the pid the reaper checks must be the CHILD's
    Popen pid, NOT os.getpid() (the gateway, always alive). This is the exact
    assertion the original tests lacked, which let the broken handoff pass."""
    import os as _os
    entry = _marker(pending_dir)
    CHILD_PID = 991234
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: SimpleNamespace(pid=CHILD_PID))
    asyncio.run(hook.handle("agent:start", _ctx()))
    rid = entry["request_id"]
    claims = list(pending_dir.glob(f"{rid}.claimed.*.json"))
    assert len(claims) == 1
    st = json.loads(claims[0].read_text())
    assert st.get("child_pid") == CHILD_PID, "must record the Popen child pid"
    assert st.get("child_pid") != _os.getpid(), "must NOT be the gateway pid"
    # filename is an opaque claim_id, not a pid
    cid = claims[0].name.rsplit(".claimed.", 1)[1].split(".")[0]
    assert cid != str(_os.getpid())


def test_fast_child_commit_is_not_recreated_by_parent(pending_dir, monkeypatch):
    """Follow-up audit race: a fast child can reach its commitment point and
    DELETE its claim before the parent records the child pid. The parent must
    NOT recreate the claim — recreating it would let the reaper later 'restore'
    a dead confirmation window. Popen here simulates the child committing
    (deleting the exact claim) before it returns."""
    entry = _marker(pending_dir)
    rid = entry["request_id"]

    def _popen_child_commits_immediately(cmd, **kw_):
        for c in pending_dir.glob(f"{rid}.claimed.*.json"):
            c.unlink()                      # child released its claim
        return SimpleNamespace(pid=555000)

    monkeypatch.setattr(hook.subprocess, "Popen", _popen_child_commits_immediately)
    asyncio.run(hook.handle("agent:start", _ctx()))
    assert not list(pending_dir.glob(f"{rid}.claimed.*.json")), \
        "parent must not recreate a claim the child already committed"
    assert not (pending_dir / f"{rid}.json").exists(), \
        "a committed confirmation must not be re-armed"


def test_reaper_restores_orphaned_claim(pending_dir):
    """Dead CHILD pid + live window = child crashed before consuming -> restore."""
    rid = "u1_2026_0708_cafe01"
    claim = _claim(pending_dir, rid, "abc123", child_pid=_dead_pid())
    hook._reap_orphaned_claims()
    assert not claim.exists()
    assert (pending_dir / f"{rid}.json").exists(), "orphan must be restored"


def test_reaper_leaves_live_claim_alone(pending_dir, monkeypatch):
    """A claim whose recorded CHILD pid is alive = confirm in flight -> untouched."""
    rid = "u1_2026_0708_cafe02"
    monkeypatch.setattr(hook, "_pid_alive", lambda pid: True)
    claim = _claim(pending_dir, rid, "def456", child_pid=4242)
    hook._reap_orphaned_claims()
    assert claim.exists()
    assert not (pending_dir / f"{rid}.json").exists()


def test_reaper_drops_expired_orphan_without_restoring(pending_dir):
    """Dead child pid but window expired -> drop, do NOT restore a dead window."""
    rid = "u1_2026_0708_cafe03"
    claim = _claim(pending_dir, rid, "ghi789", child_pid=_dead_pid(), minutes=-5)
    hook._reap_orphaned_claims()
    assert not claim.exists()
    assert not (pending_dir / f"{rid}.json").exists()


def test_reaper_skips_fresh_claim_before_pid_recorded(pending_dir):
    """A just-created claim with no child_pid yet = spawn in flight -> left
    alone during the mtime grace (do not reap a confirm that is bootstrapping)."""
    rid = "u1_2026_0709_inflight"
    claim = _claim(pending_dir, rid, "fresh1")   # no child_pid
    hook._reap_orphaned_claims()
    assert claim.exists(), "fresh no-pid claim is in flight, not an orphan"
    assert not (pending_dir / f"{rid}.json").exists()


def test_release_scoped_to_own_claim_id(pending_dir):
    """AUDIT #3: a child releases ONLY its own claim_id — a concurrently
    re-armed generation's claim survives instead of being deleted."""
    rid = "u1_2026_0709_gen001"
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / f"{rid}.claimed.AAA.json").write_text("{}")
    (pending_dir / f"{rid}.claimed.BBB.json").write_text("{}")
    kw._release_confirm_claim(rid, "AAA")
    assert not (pending_dir / f"{rid}.claimed.AAA.json").exists(), "own claim gone"
    assert (pending_dir / f"{rid}.claimed.BBB.json").exists(), "other generation kept"


def test_release_confirm_claim_legacy_glob_without_id(pending_dir):
    """No claim_id (direct/legacy call) falls back to the request-wide glob."""
    rid = "u1_2026_0708_cafe04"
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / f"{rid}.claimed.111.json").write_text("{}")
    kw._release_confirm_claim(rid)
    assert not list(pending_dir.glob(f"{rid}.claimed.*.json"))
