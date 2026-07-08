"""Regression tests for the bed_clear_start two-turn safety boundary.

Every test asserts that a specific bypass path is architecturally
impossible, not just discouraged:
  - `stage2_command` never appears in a card emit (any adapter reading
    it would bypass the yes/no).
  - The Stage 2 approval nonce is single-use and required.
  - Manual bed-check overrides go through the same two-turn shape as
    the normal path (one exception path defeats the whole guard).
"""
from __future__ import annotations

import json
import pytest
from pathlib import Path
from types import SimpleNamespace

import u1_kit_workflow as kw
import u1_request
import u1_print_start_gate as _gate_mod


@pytest.fixture(autouse=True)
def _gate_file_exists_by_default(monkeypatch):
    """Q1 (2026-07-08): the gate FAILS CLOSED when it can't confirm the gcode
    exists on the printer. Gate tests not about existence assume it's present
    so they isolate their own logic; existence tests override in-body."""
    monkeypatch.setattr(_gate_mod, "gcode_exists_on_printer",
                        lambda *a, **k: True)



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


# ─── Helpers ────────────────────────────────────────────────────────────────

def _seed_request(tmp_path: Path, request_id: str, **overrides):
    """Create a request.json that _action_start can read."""
    state = {
        "phase": "awaiting_confirm",
        "printer_storage_filename": "test_plate1.gcode",
        "tool": "T1",
        "material": "PETG",
        "request_revision": 1,
        "plates": [{
            "plate_idx": 1,
            "gcode_path": str(tmp_path / "test_plate1.gcode"),
            "gcode_hash": "sha256:abc123",
            "printer_storage_filename": "test_plate1.gcode",
        }],
        "safety": {
            "approval_token": "test_token_xyz",
            "snapshot_path": str(tmp_path / "bed_snapshot.jpg"),
        },
    }
    state.update(overrides)
    u1_request.write_request(request_id, **state)
    return state


@pytest.fixture
def sandbox_requests(tmp_path, monkeypatch):
    """Redirect u1_request storage to tmp_path so tests don't touch real state.

    Also disables the pre-start grace period by default (0s) so unrelated
    tests don't pay a 2-minute wait each. Tests that specifically exercise
    the grace-period behavior override grace_seconds on the run_gate call."""
    root = tmp_path / "requests"
    root.mkdir()
    monkeypatch.setattr(u1_request, "_requests_root", lambda: root, raising=False)
    monkeypatch.setattr(u1_request, "request_dir",
                        lambda rid: root / rid, raising=False)
    (tmp_path / "bed_snapshot.jpg").write_bytes(b"stub")
    monkeypatch.setenv("U1_GRACE_PERIOD_SECONDS", "0")
    yield tmp_path


# ─── P0.1: No stage2_command leak in confirm event ──────────────────────────

def test_confirm_card_has_no_stage2_command_leak():
    """`stage2_command` must not appear in kit_readiness_card or the `start`
    option payload — any adapter grabbing it would bypass the
    bed_clear_start yes/no."""
    import re
    src = Path(kw.__file__).read_text()
    # Find every occurrence of "stage2_command" as a dict key emit
    leaks = re.findall(r'["\']stage2_command["\']\s*:', src)
    assert not leaks, (
        f"stage2_command leaks in {len(leaks)} places — any adapter "
        "could grab this to bypass the bed_clear_start prompt")


def test_confirm_card_source_has_no_raw_approval_token_key():
    """kit_readiness_card must not surface approval_token at the top level
    of the event — token stays in persisted safety.approval_token only."""
    src = Path(kw.__file__).read_text()
    # This regex specifically finds top-level approval_token in readiness dict
    # (loose check — presence of the key in operator-facing events is red flag)
    idx = src.find("kit_readiness_card")
    assert idx >= 0, "kit_readiness_card emission not found"
    readiness_block = src[idx:idx + 4000]
    assert '"approval_token"' not in readiness_block, (
        "approval_token surfaced in kit_readiness_card event — "
        "should live only in persisted safety.approval_token")


# ─── P0.2 + P0.3: bed_clear_start two-turn ──────────────────────────────────

def test_action_start_first_call_emits_bed_clear_start_need_input(
        sandbox_requests, capsys):
    """First `--action start` must emit need_input(key: bed_clear_start),
    NOT a Stage 2 next_action_required."""
    _seed_request(sandbox_requests, "u1_test_first_call")
    result = kw._action_start(events_file=None,
                              request_id="u1_test_first_call",
                              json_events=False, bed_clear_confirmed=False)
    assert result["phase"] == "awaiting_bed_clear_start"
    assert "prompt" in result
    # v2.2 one-decision wording: YES starts, NO keeps it uploaded; no request-id.
    assert "YES to start" in result["prompt"] and "NO to keep" in result["prompt"]
    assert "u1_test_first_call" not in result["prompt"]
    # Verify pending object persisted
    state = u1_request.read_request("u1_test_first_call")
    pending = state.get("safety", {}).get("pending_bed_clear_start")
    assert pending is not None
    assert pending["prompt_key"] == "bed_clear_start"
    assert pending["gcode_hash"] == "sha256:abc123"
    assert pending["request_revision"] == 1
    assert isinstance(pending["nonce"], str) and len(pending["nonce"]) >= 16


def test_action_start_second_call_refuses_without_pending(sandbox_requests):
    """`--bed-clear-confirmed` without pending object → refuse."""
    _seed_request(sandbox_requests, "u1_test_no_pending",
                  phase="awaiting_confirm")  # never went through first turn
    result = kw._action_start(events_file=None,
                              request_id="u1_test_no_pending",
                              json_events=False, bed_clear_confirmed=True)
    assert result["phase"] == "bed_clear_approval_rejected"
    assert any("no pending" in r for r in result["reasons"])


def test_action_start_second_call_refuses_mismatched_gcode_hash(
        sandbox_requests):
    """If gcode_hash drifts between first and second call → refuse."""
    rid = "u1_test_hash_drift"
    _seed_request(sandbox_requests, rid)
    # Emit first call to get pending
    kw._action_start(None, rid, False, bed_clear_confirmed=False)
    # Simulate a re-slice: gcode_hash changed
    state = u1_request.read_request(rid)
    state["plates"][0]["gcode_hash"] = "sha256:DIFFERENT"
    u1_request.write_request(rid, plates=state["plates"])
    # Second call — must refuse
    result = kw._action_start(None, rid, False, bed_clear_confirmed=True)
    assert result["phase"] == "bed_clear_approval_rejected"
    assert any("gcode_hash mismatch" in r for r in result["reasons"])


def test_action_start_second_call_success_mints_stage2_nonce(sandbox_requests, _fake_stage2_gate):
    """Happy path: pending matches + hash matches + phase matches → emits
    Stage 2 command that includes --stage2-approval-nonce; pending is
    consumed; safety.stage2_approval_nonce is persisted."""
    rid = "u1_test_happy"
    _seed_request(sandbox_requests, rid)
    kw._action_start(None, rid, False, bed_clear_confirmed=False)
    # The verbatim next_command_on_yes carries the pending nonce.
    pn = (u1_request.read_request(rid)["safety"]
          ["pending_bed_clear_start"]["nonce"])
    result = kw._action_start(None, rid, False, bed_clear_confirmed=True,
                              pending_nonce=pn, )
    # The workflow RUNS the gate itself now — the model never relays it.
    assert result["phase"] == "print_started"
    assert result["started"] is True
    gate_argv = _fake_stage2_gate["argv"]
    assert "--bed-clear" in gate_argv and "start" in gate_argv
    assert any(a.startswith("--stage2-approval-nonce=") for a in gate_argv)
    assert any(a.startswith("--approval-token=") for a in gate_argv)
    # pending is consumed
    state = u1_request.read_request(rid)
    assert state["safety"].get("pending_bed_clear_start") is None
    # nonce persisted for Stage 2 gate to consume
    assert state["safety"].get("stage2_approval_nonce")
    binds = state["safety"].get("stage2_approval_binds")
    assert binds["request_revision"] == 1
    assert binds["gcode_hash"] == "sha256:abc123"
    assert binds["prompt_key"] == "bed_clear_start"


def test_action_start_grace_in_progress_when_gate_detaches(sandbox_requests, monkeypatch):
    """When the gate is still running (grace window) after the bounded pre-grace
    wait, _invoke_stage2_gate returns None and _action_start reports
    grace_in_progress — the gate runs detached; the tool call must NOT block the
    full ~120s (it times out at 60s and kills the gate — live 2026-07-04)."""
    monkeypatch.setattr(kw, "_invoke_stage2_gate", lambda gate_py, argv, out_dir: None)
    rid = "u1_test_grace"
    _seed_request(sandbox_requests, rid)
    kw._action_start(None, rid, False, bed_clear_confirmed=False)
    pn = u1_request.read_request(rid)["safety"]["pending_bed_clear_start"]["nonce"]
    result = kw._action_start(None, rid, False, bed_clear_confirmed=True, pending_nonce=pn)
    assert result["phase"] == "grace_in_progress"
    assert result["started"] is None
    # nonce was still minted (the gate got it)
    assert u1_request.read_request(rid)["safety"].get("stage2_approval_nonce")


def test_bed_clear_start_prompt_key_is_bed_clear_start(sandbox_requests, capsys):
    """The need_input event must carry key=bed_clear_start."""
    import io
    rid = "u1_test_key_naming"
    _seed_request(sandbox_requests, rid)
    events = []
    def fake_emit(events_file, event, json_events=False):
        events.append(event)
    orig_emit = kw._emit
    kw._emit = fake_emit
    try:
        kw._action_start(None, rid, False, bed_clear_confirmed=False)
    finally:
        kw._emit = orig_emit
    need = next((e for e in events if e.get("stage") == "need_input"), None)
    assert need is not None
    assert need["need"] == "bed_clear_start"
    assert need["key"] == "bed_clear_start"
    assert need["approval_prompt_key"] == "bed_clear_start"
    assert need["requires_fresh_operator_bed_clear"] is True
    # Model-free YES (2026-07-07): the event must NOT hand the model any
    # start command — the confirm marker armed on disk is the only trigger.
    assert "next_command_on_yes" not in need
    assert need["next_command_on_no"] is None


# ─── #5: Manual bed-check override two-turn ─────────────────────────────────

def test_manual_bed_check_first_call_emits_bed_clear_start(sandbox_requests):
    """Manual override first call must also emit bed_clear_start need_input,
    not Stage 2 (uniformity across all Stage 2 paths)."""
    rid = "u1_test_manual_first"
    _seed_request(sandbox_requests, rid)
    result = kw._action_start_manual_bed_check(
        None, rid, "test:unit", False,
        operator_text="start manual-bed-check",
        verification_method="snapmaker_app",
        bed_clear_confirmed=False)
    assert result["phase"] == "awaiting_bed_clear_start"
    state = u1_request.read_request(rid)
    pending = state["safety"]["pending_bed_clear_start"]
    assert pending["manual_override"] is True
    assert pending["verification_method"] == "snapmaker_app"
    assert pending["operator_text"] == "start manual-bed-check"


def test_manual_bed_check_second_call_captures_both_timestamps(
        sandbox_requests):
    """Second call must produce Stage 2 command AND write an audit row
    with BOTH override_attempted_at and override_confirmed_at."""
    rid = "u1_test_manual_confirm"
    _seed_request(sandbox_requests, rid)
    kw._action_start_manual_bed_check(
        None, rid, "test:unit", False,
        operator_text="start manual-bed-check",
        verification_method="snapmaker_app",
        bed_clear_confirmed=False)
    pn = (u1_request.read_request(rid)["safety"]
          ["pending_bed_clear_start"]["nonce"])
    result = kw._action_start_manual_bed_check(
        None, rid, "test:unit", False,
        operator_text="start manual-bed-check",
        verification_method="snapmaker_app",
        bed_clear_confirmed=True, pending_nonce=pn)
    assert result["phase"] == "awaiting_print_start"
    assert "--stage2-approval-nonce" in result["command"]
    state = u1_request.read_request(rid)
    safety = state["safety"]
    assert safety.get("override_attempted_at")
    assert safety.get("override_confirmed_at")
    assert safety.get("override_attempted_at") != safety.get("override_confirmed_at")


# ─── #4: Stage 2 gate nonce validation ──────────────────────────────────────

def test_stage2_gate_refuses_without_nonce_when_request_has_one(
        sandbox_requests, monkeypatch):
    """u1_print_start_gate.py must refuse Stage 2 if request state has
    an expected nonce but caller didn't pass --stage2-approval-nonce."""
    import u1_print_start_gate as gate
    rid = "u1_test_gate_no_nonce"
    _seed_request(sandbox_requests, rid,
                  safety={
                      "approval_token": "test_token",
                      "stage2_approval_nonce": "expected_nonce_xyz",
                      "stage2_approval_binds": {
                          "request_revision": 1,
                          "gcode_hash": "sha256:abc123",
                          "prompt_key": "bed_clear_start",
                      },
                  })
    # Stub the underlying token/preflight to isolate the nonce check
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight",
                        lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: {})
    # No nonce passed
    res = gate.run_gate("test_plate1.gcode", "start", host="127.0.0.1", port=7125,
                        approval_token="test_token",
                        stage2_approval_nonce=None,
                        request_id=rid,
                        out_dir=u1_request.request_dir(rid))
    assert res["ok"] is False
    assert "nonce" in res["reason"].lower()


def test_stage2_gate_refuses_mismatched_nonce(sandbox_requests, monkeypatch):
    """Wrong nonce → refuse."""
    import u1_print_start_gate as gate
    rid = "u1_test_gate_wrong_nonce"
    _seed_request(sandbox_requests, rid,
                  safety={
                      "approval_token": "test_token",
                      "stage2_approval_nonce": "expected_nonce_xyz",
                      "stage2_approval_binds": {
                          "request_revision": 1,
                          "gcode_hash": "sha256:abc123",
                          "prompt_key": "bed_clear_start",
                      },
                  })
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: {})
    res = gate.run_gate("test_plate1.gcode", "start", host="127.0.0.1", port=7125,
                        approval_token="test_token",
                        stage2_approval_nonce="WRONG_nonce",
                        request_id=rid,
                        out_dir=u1_request.request_dir(rid))
    assert res["ok"] is False
    assert "nonce" in res["reason"].lower()


@pytest.mark.parametrize("test_op", [
    "smoke:med1", "test:integration",
    "dry:sanity", "mock:printer",
    "SMOKE:capitalized", "Fixture:auto",
])
def test_stage2_gate_refuses_test_prefixed_operator(
        sandbox_requests, monkeypatch, test_op):
    """Fence 1: any --operator starting with smoke:/test:/dry:/mock:/
    fixture: is refused BEFORE any Moonraker call. Test operators can
    never send print traffic to a real printer regardless of nonce
    validity, approval token, or preflight state. Refusal is delivered
    as a RETURN value (not a print) so main() is the only JSON writer."""
    import u1_print_start_gate as gate
    called = {"query_state": False, "start_func": False, "preflight": False}
    monkeypatch.setattr(gate, "query_state",
                        lambda h, p: called.__setitem__("query_state", True) or {})
    monkeypatch.setattr(gate, "preflight",
                        lambda *a, **kw: called.__setitem__("preflight", True) or [])
    monkeypatch.setattr(gate, "start_print",
                        lambda *a, **kw: called.__setitem__("start_func", True) or {"ok": True})
    rid = f"u1_test_gate_fence1_{test_op.replace(':', '_')}"
    _seed_request(sandbox_requests, rid)
    payload = gate.run_gate("test_plate1.gcode", "start", host="127.0.0.1", port=7125,
                            approval_token="test_token",
                            stage2_approval_nonce="anything",
                            request_id=rid,
                            operator=test_op,
                            out_dir=u1_request.request_dir(rid))
    assert payload is not None, "run_gate must return the refusal dict, not None"
    assert payload["ok"] is False
    assert payload["started"] is False
    assert payload["stage"] == "gate_refused_test_operator"
    # No Moonraker or start-func call reached under a test operator.
    assert not called["query_state"], (
        f"gate reached query_state under operator {test_op!r}")
    assert not called["start_func"], (
        f"gate reached start_func under operator {test_op!r}")
    assert not called["preflight"], (
        f"gate reached preflight under operator {test_op!r}")


def test_stage2_gate_allows_real_operator_prefixes(sandbox_requests, monkeypatch):
    """Fence 1 must NOT over-block: production operator strings must proceed
    past the fence. Includes:
      * platform-adapter identities: `telegram:brent`, `discord:ops`
      * generic identities: `human:brent`, bare `brent`, `unknown:x`
      * developer / CI identities: `dev:my-fork`, `ci:release-pipeline` —
        deliberately left off the fence list to avoid burning fork
        developers or CI-orchestrated real prints."""
    import u1_print_start_gate as gate
    called = {"query_state": False}
    monkeypatch.setattr(gate, "query_state",
                        lambda h, p: called.__setitem__("query_state", True) or {})
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: ["some blocker"])
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: None)
    for op in ("telegram:brent", "discord:ops", "human:brent", "brent",
               "unknown:x", "dev:my-fork", "ci:release-pipeline"):
        rid = f"u1_test_gate_fence1_pass_{op.replace(':','_').replace('-','_')}"
        _seed_request(sandbox_requests, rid)
        gate.run_gate("test_plate1.gcode", "start", host="127.0.0.1", port=7125,
                      approval_token="test_token",
                      request_id=rid, operator=op,
                      out_dir=u1_request.request_dir(rid))
    assert called["query_state"], (
        "gate short-circuited a production operator — Fence 1 over-blocks")


def test_stage2_gate_refuses_hash_binding_mismatch(sandbox_requests, monkeypatch):
    """Nonce matches but gcode_hash binding drifted → refuse."""
    import u1_print_start_gate as gate
    rid = "u1_test_gate_hash_drift"
    _seed_request(sandbox_requests, rid,
                  safety={
                      "approval_token": "test_token",
                      "stage2_approval_nonce": "the_nonce",
                      "stage2_approval_binds": {
                          "request_revision": 1,
                          "gcode_hash": "sha256:STALE_HASH",  # doesn't match plates[0]
                          "prompt_key": "bed_clear_start",
                      },
                  })
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: {})
    res = gate.run_gate("test_plate1.gcode", "start", host="127.0.0.1", port=7125,
                        approval_token="test_token",
                        stage2_approval_nonce="the_nonce",
                        request_id=rid,
                        out_dir=u1_request.request_dir(rid))
    assert res["ok"] is False
    assert "hash" in res["reason"].lower()


# ─── Kit request must require staged nonce ─────────────────────────────────

def test_stage2_gate_refuses_kit_request_without_persisted_nonce(
        sandbox_requests, monkeypatch):
    """Kit-path close: a kit request that
    reached Stage 2 via the legacy --form-answers one-liner never mints a
    Stage 2 nonce. Absent nonce state on a kit request means the two-turn
    boundary was bypassed. Gate refuses regardless of approval-token
    validity. Single-STL requests keep the legacy token-only path."""
    import u1_print_start_gate as gate
    rid = "u1_test_kit_no_nonce"
    # Seed a KIT request (has `kit` and `plates` fields) but with NO
    # stage2_approval_nonce in safety.
    _seed_request(sandbox_requests, rid,
                  kit={"parts": [{"part_id": "p1"}], "part_count": 1,
                       "selected": ["p1"]},
                  safety={"approval_token": "test_token"})
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: {})
    # No stage2_approval_nonce provided AND no nonce in safety
    res = gate.run_gate("test_plate1.gcode", "start", host="127.0.0.1", port=7125,
                        approval_token="test_token",
                        request_id=rid,
                        out_dir=u1_request.request_dir(rid))
    assert res["ok"] is False
    assert res["started"] is False
    assert "staged bed_clear_start" in res["reason"] or "nonce" in res["reason"].lower()


def test_stage2_gate_allows_single_stl_request_without_persisted_nonce(
        sandbox_requests, monkeypatch):
    """Single-STL requests (no `kit` / `plates` field) MUST keep the
    legacy token-only path — closing the kit hole must not break the
    single-STL workflow's backward compat."""
    import u1_print_start_gate as gate
    rid = "u1_test_single_stl_legacy"
    _seed_request(sandbox_requests, rid,
                  kit=None, plates=None,
                  safety={"approval_token": "test_token"})
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    reached_query = {"hit": False}
    monkeypatch.setattr(gate, "query_state",
                        lambda h, p: (reached_query.__setitem__("hit", True) or {}))
    monkeypatch.setattr(gate, "_read_approval_token",
                        lambda d: {"token": "test_token"})
    monkeypatch.setattr(gate, "capture_real_bed_photo",
                        lambda *a, **kw: {"ok": True, "path": None,
                                          "brightness_check": "deferred"})
    monkeypatch.setattr(gate, "start_print",
                        lambda *a, **kw: {"result": "ok"})
    # Missing plate1.gcode etc — expect the gate to progress to preflight
    # (proving the kit-check did not falsely block a single-STL request)
    gate.run_gate("test_plate1.gcode", "start", host="127.0.0.1", port=7125,
                  approval_token="test_token",
                  request_id=rid,
                  out_dir=u1_request.request_dir(rid))
    assert reached_query["hit"], (
        "single-STL request without a nonce must NOT be blocked by the "
        "kit-request check — legacy backward compat must hold")


# ─── Fence refusal must return dict, not print ─────────────────────────────

def test_fence_refusal_returns_dict_no_bare_none(sandbox_requests, monkeypatch):
    """Fence 1 refusal must RETURN the JSON payload, not print + return None.
    If it returned None, main() would json.dumps(None) → 'null' as a
    second output line, breaking any downstream JSON parser."""
    import u1_print_start_gate as gate
    monkeypatch.setattr(gate, "query_state", lambda h, p: {"__should_not_reach": True})
    res = gate.run_gate("test_plate1.gcode", "start", host="127.0.0.1", port=7125,
                        approval_token="test_token",
                        request_id="u1_test_fence_shape",
                        operator="smoke:shape",
                        out_dir=sandbox_requests / "no_such_request")
    assert res is not None, (
        "run_gate must return the refusal dict; None causes main() to "
        "print `null` as a second output line")
    assert res["ok"] is False
    assert res["started"] is False
    assert res["stage"] == "gate_refused_test_operator"


# ─── Unset operator passes the fence — a DECISION, loudly audited ──────────

def test_unset_operator_passes_fence_with_loud_audit(sandbox_requests, monkeypatch):
    """`unknown:gate` (no --operator, no U1_OPERATOR) deliberately PASSES
    Fence 1 — refusing every bare CLI run would tax legitimate local use.
    The trade-off: a smoke test that forgot to set an operator is not
    fenced. That gap is a decision, not an accident, and it must leave a
    gate_operator_unknown audit row."""
    import u1_print_start_gate as gate
    monkeypatch.delenv("U1_OPERATOR", raising=False)
    audits = []
    monkeypatch.setattr(gate, "_audit_gate",
                        lambda rid, event, op=None, **kw: audits.append(event))
    reached_query = {"hit": False}

    def _query(h, p):
        reached_query["hit"] = True
        raise RuntimeError("stop after fence — this test only pins the fence")

    monkeypatch.setattr(gate, "query_state", _query)
    try:
        gate.run_gate("test_plate1.gcode", "start", host="127.0.0.1", port=7125,
                      approval_token="tok", request_id="u1_test_unknown_op",
                      out_dir=sandbox_requests / "no_such_request")
    except RuntimeError:
        pass
    assert reached_query["hit"], "unknown:gate must NOT be refused by Fence 1"
    assert "gate_operator_unknown" in audits
    assert "gate_refused_test_operator" not in audits


# ─── Manual bed-check wires through the gate ───────────────────────────────

def test_stage2_gate_skips_sanity_capture_when_manual_verification_fresh(
        sandbox_requests, monkeypatch):
    """Manual-bed-check wire-through: when
    safety.manual_verification is True AND a Stage 2 nonce was minted
    (proving the operator's fresh yes actually happened) AND
    verification_method + operator_text are recorded, the Stage 2
    mandatory sanity photo capture is SKIPPED with a loud audit row.
    The human's yes is the load-bearing safety gate; the sanity capture
    was scaffolding for the camera path. This is what actually rescues
    manual override when the camera is the reason for the override."""
    import u1_print_start_gate as gate
    rid = "u1_test_manual_wired"
    _seed_request(sandbox_requests, rid,
                  kit={"parts": [{"part_id": "p1"}], "part_count": 1},
                  safety={
                      "approval_token": "test_token",
                      "stage2_approval_nonce": "the_manual_nonce",
                      "stage2_approval_binds": {
                          "request_revision": 1,
                          "gcode_hash": "sha256:abc123",
                          "prompt_key": "bed_clear_start",
                      },
                      "manual_verification": True,
                      "verification_method": "snapmaker_app",
                      "operator_text": "start manual-bed-check",
                      "override_confirmed_at": "2026-07-01T18:00:00+00:00",
                  })
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token",
                        lambda d: {"token": "test_token"})
    import u1_safety
    monkeypatch.setattr(u1_safety, "can_start", lambda req: (True, "ok"))
    # Camera call would fail (this is the whole point of manual override).
    # If the wire-through works, this lambda should NEVER be called.
    camera_called = {"hit": False}
    def raise_if_hit(*a, **kw):
        camera_called["hit"] = True
        return {"ok": False, "is_mock": True, "error": "camera unreachable"}
    monkeypatch.setattr(gate, "capture_real_bed_photo", raise_if_hit)
    started = {"hit": False}
    def fake_start(*a, **kw):
        started["hit"] = True
        return {"result": "ok"}
    res = gate.run_gate("test_plate1.gcode", "start", host="127.0.0.1", port=7125,
                        approval_token="test_token",
                        stage2_approval_nonce="the_manual_nonce",
                        request_id=rid,
                        operator="telegram:brent",
                        start_func=fake_start,
                        out_dir=u1_request.request_dir(rid))
    assert not camera_called["hit"], (
        "manual-verification path must skip the Stage 2 sanity capture; "
        "camera was called anyway → override defeated exactly when the "
        "operator needs it")
    assert started["hit"], (
        "manual-verification path with fresh nonce + all binds must "
        "actually reach start_print — the whole point of the override")


def test_stage2_gate_still_captures_sanity_when_no_manual_verification(
        sandbox_requests, monkeypatch):
    """Normal camera path (no manual_verification): sanity capture is
    still mandatory. The wire-through must not accidentally short-
    circuit the normal path."""
    import u1_print_start_gate as gate
    rid = "u1_test_normal_camera"
    _seed_request(sandbox_requests, rid,
                  kit={"parts": [{"part_id": "p1"}], "part_count": 1},
                  safety={
                      "approval_token": "test_token",
                      "stage2_approval_nonce": "the_normal_nonce",
                      "stage2_approval_binds": {
                          "request_revision": 1,
                          "gcode_hash": "sha256:abc123",
                          "prompt_key": "bed_clear_start",
                      },
                      # NO manual_verification flag
                  })
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token",
                        lambda d: {"token": "test_token"})
    import u1_safety
    monkeypatch.setattr(u1_safety, "can_start", lambda req: (True, "ok"))
    camera_called = {"hit": False}
    def real_capture(*a, **kw):
        camera_called["hit"] = True
        return {"ok": True, "path": None, "brightness_check": "deferred"}
    monkeypatch.setattr(gate, "capture_real_bed_photo", real_capture)
    gate.run_gate("test_plate1.gcode", "start", host="127.0.0.1", port=7125,
                  approval_token="test_token",
                  stage2_approval_nonce="the_normal_nonce",
                  request_id=rid,
                  operator="telegram:brent",
                  start_func=lambda *a, **kw: {"result": "ok"},
                  out_dir=u1_request.request_dir(rid))
    assert camera_called["hit"], (
        "normal camera path must still call capture_real_bed_photo; "
        "manual-verification wire-through must not over-reach")


# ─── Refresh-bed-photo cannot create a start path ─────────────────────────

def test_refresh_bed_photo_does_not_directly_emit_stage2(sandbox_requests, monkeypatch):
    """The refresh-bed-photo action must go through the SAME two-turn
    bed_clear_start yes/no; it cannot mint a Stage 2 nonce on its own.
    Otherwise the refresh path would be an alternate entry to Stage 2
    that bypasses the operator's yes."""
    import io
    rid = "u1_test_refresh_no_direct_start"
    # A request that has already reached the confirm point and has a
    # persisted start_gate_stage1_command (so we don't fail on missing
    # setup).
    _seed_request(sandbox_requests, rid,
                  phase="awaiting_print_start",
                  start_gate_stage1_command="python3 u1_print_start_gate.py x.gcode --intended-tool extruder --requested-material PLA --request-id " + rid)
    events = []
    def fake_emit(events_file, event, json_events=False):
        events.append(event)
    orig_emit = kw._emit
    kw._emit = fake_emit
    # Stub the bed capture so the refresh action doesn't need a live printer.
    monkeypatch.setattr(kw, "_capture_bed_and_issue_token",
                        lambda out_dir: {
                            "ok": True,
                            "snapshot_path": str(sandbox_requests / "bed_snapshot.jpg"),
                            "token": "refresh_token",
                            "approval_ttl_seconds": 1800,
                            "approval_expires_at": "2026-07-01T18:30:00+00:00",
                            "captured_at_utc": "2026-07-01T18:00:00+00:00",
                            "reason": None,
                        })
    args = SimpleNamespace(json_events=False, request_id=rid, operator="test:unit")
    try:
        kw._action_refresh_bed_photo(
            args, None, rid,
            Path("/tmp/no_archive.zip"),
            {"parts": [{"part_id": "p1"}], "part_count": 1},
            "test:unit", "0.4", "all", "T0", "PLA",
            True, True, False, "refresh-bed-photo")
    finally:
        kw._emit = orig_emit
    # The refresh must NOT emit a next_action_required carrying a
    # Stage 2 command directly. It must emit the render + a fresh
    # bed_clear_start need_input (same as _action_start).
    for e in events:
        if e.get("stage") == "next_action_required":
            cmd = e.get("command", "")
            assert "--stage2-approval-nonce" not in cmd, (
                "refresh-bed-photo emitted a Stage 2 command directly; it "
                "must route through the bed_clear_start yes/no first")
    # And the request state must NOT have gained a stage2_approval_nonce
    # from refresh alone (only from _action_start's fresh yes).
    state = u1_request.read_request(rid)
    assert not (state.get("safety") or {}).get("stage2_approval_nonce"), (
        "refresh alone must not persist a Stage 2 nonce")


# ─── Remaining coverage gaps ────────────────────────────────────────────────

def test_stage2_gate_refuses_kit_request_wrong_revision(sandbox_requests, monkeypatch):
    """Nonce matches but request_revision drifted (plan changed since the
    operator approved) → refuse. Guards silent plan-swap between
    approval and Stage 2."""
    import u1_print_start_gate as gate
    rid = "u1_test_kit_wrong_revision"
    _seed_request(sandbox_requests, rid,
                  kit={"parts": [{"part_id": "p1"}], "part_count": 1},
                  request_revision=7,  # current
                  safety={
                      "approval_token": "test_token",
                      "stage2_approval_nonce": "the_nonce",
                      "stage2_approval_binds": {
                          "request_revision": 3,  # bound at approval time
                          "gcode_hash": "sha256:abc123",
                          "prompt_key": "bed_clear_start",
                      },
                  })
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: {})
    res = gate.run_gate("test_plate1.gcode", "start", host="127.0.0.1", port=7125,
                        approval_token="test_token",
                        stage2_approval_nonce="the_nonce",
                        request_id=rid,
                        out_dir=u1_request.request_dir(rid))
    assert res["ok"] is False
    assert res["started"] is False
    assert "revision" in res["reason"].lower()


def test_stage2_gate_refuses_kit_replay_of_consumed_nonce(sandbox_requests, monkeypatch):
    """Consumed-nonce replay guard on kit path: after a successful Stage
    2 the workflow pops the nonce from safety. A subsequent invocation
    with the SAME nonce value now finds `expected_nonce is None` on a
    kit request, which is refused. Belt for defense in
    depth — the attacker had a valid consumed nonce; the gate still
    refuses."""
    import u1_print_start_gate as gate
    rid = "u1_test_kit_replay"
    # State AFTER a successful consumption: kit fields still present,
    # nonce popped, binds popped.
    _seed_request(sandbox_requests, rid,
                  kit={"parts": [{"part_id": "p1"}], "part_count": 1},
                  plates=[{"plate_idx": 1, "gcode_hash": "sha256:x"}],
                  safety={"approval_token": "test_token"})  # no nonce
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: {})
    started = {"hit": False}
    def bad_start(*a, **kw):
        started["hit"] = True
        return {"result": "ok"}
    res = gate.run_gate("test_plate1.gcode", "start", host="127.0.0.1", port=7125,
                        approval_token="test_token",
                        stage2_approval_nonce="the_old_consumed_nonce",
                        request_id=rid,
                        start_func=bad_start,
                        out_dir=u1_request.request_dir(rid))
    assert res["ok"] is False
    assert res["started"] is False
    assert not started["hit"], "replay of consumed kit nonce must not reach start"


def test_stage2_gate_falls_back_to_camera_when_manual_verification_missing_operator_text(
        sandbox_requests, monkeypatch):
    """Half-populated manual-verification state must NOT trigger the
    sanity-capture skip. safety.manual_verification=True but no
    operator_text → fall through to the normal camera path. If the
    camera is down (which is when manual override actually matters),
    the operator gets a clear refusal instead of an accidental start."""
    import u1_print_start_gate as gate
    rid = "u1_test_manual_no_operator_text"
    _seed_request(sandbox_requests, rid,
                  kit={"parts": [{"part_id": "p1"}], "part_count": 1},
                  safety={
                      "approval_token": "test_token",
                      "stage2_approval_nonce": "n",
                      "stage2_approval_binds": {
                          "request_revision": 1,
                          "gcode_hash": "sha256:abc123",
                          "prompt_key": "bed_clear_start",
                      },
                      "manual_verification": True,
                      # operator_text MISSING
                      "verification_method": "snapmaker_app",
                  })
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: {})
    import u1_safety
    monkeypatch.setattr(u1_safety, "can_start", lambda req: (True, "ok"))
    camera_called = {"hit": False}
    def real_capture(*a, **kw):
        camera_called["hit"] = True
        return {"ok": True, "path": None, "brightness_check": "deferred"}
    monkeypatch.setattr(gate, "capture_real_bed_photo", real_capture)
    gate.run_gate("test_plate1.gcode", "start", host="127.0.0.1", port=7125,
                  approval_token="test_token",
                  stage2_approval_nonce="n",
                  request_id=rid,
                  operator="telegram:brent",
                  start_func=lambda *a, **kw: {"result": "ok"},
                  out_dir=u1_request.request_dir(rid))
    assert camera_called["hit"], (
        "half-populated manual verification (no operator_text) must fall "
        "back to camera sanity capture")


def test_stage2_gate_falls_back_to_camera_when_manual_verification_missing_method(
        sandbox_requests, monkeypatch):
    """Same as above for missing verification_method."""
    import u1_print_start_gate as gate
    rid = "u1_test_manual_no_method"
    _seed_request(sandbox_requests, rid,
                  kit={"parts": [{"part_id": "p1"}], "part_count": 1},
                  safety={
                      "approval_token": "test_token",
                      "stage2_approval_nonce": "n",
                      "stage2_approval_binds": {
                          "request_revision": 1,
                          "gcode_hash": "sha256:abc123",
                          "prompt_key": "bed_clear_start",
                      },
                      "manual_verification": True,
                      "operator_text": "start manual-bed-check",
                      # verification_method MISSING
                  })
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: {})
    import u1_safety
    monkeypatch.setattr(u1_safety, "can_start", lambda req: (True, "ok"))
    camera_called = {"hit": False}
    def real_capture(*a, **kw):
        camera_called["hit"] = True
        return {"ok": True, "path": None, "brightness_check": "deferred"}
    monkeypatch.setattr(gate, "capture_real_bed_photo", real_capture)
    gate.run_gate("test_plate1.gcode", "start", host="127.0.0.1", port=7125,
                  approval_token="test_token",
                  stage2_approval_nonce="n",
                  request_id=rid,
                  operator="telegram:brent",
                  start_func=lambda *a, **kw: {"result": "ok"},
                  out_dir=u1_request.request_dir(rid))
    assert camera_called["hit"], (
        "half-populated manual verification (no verification_method) "
        "must fall back to camera sanity capture")


def test_readiness_card_gates_only_plate_1_not_plates_2_N():
    """Kit safety story: the toolkit is only allowed to start plate 1
    (the gated plate). Plates 2..N must not appear in any
    startable-command field — the operator has to start them from the
    Snapmaker app because we can't know the future bed state after
    plate 1 finishes.

    Guards against a future refactor that would emit per-plate Stage
    1/2 commands and accidentally offer them as start options."""
    import re
    src = Path(kw.__file__).read_text()
    # The readiness card must expose exactly ONE `gated_plate` field
    # and exactly ONE `start_gate_stage1_command` field per emit — both
    # referring to plate 1.
    #
    # There must be NO per-plate iteration that ends up in a
    # start-command emission for plates other than plate_idx=1.
    #
    # Sanity checks (compile-time-ish — grep the source shape):
    #   * `gated_plate` is only ever set from plate1's filename
    #   * no `for pl in plates` loop that builds a start command
    for anti in (
        "start_gate_stage2_command",
        "start_gate_stage1_command_per_plate",
        "per_plate_start_command",
    ):
        assert anti not in src, (
            f"{anti} present in source — plates 2..N might be exposed as "
            "toolkit-startable")
    # No emission of start_gate_stage1_command inside a per-plate iteration
    # (defense against a future refactor that would loop start commands
    # for every plate instead of just plate 1).
    for anti_loop in (
        "for pl in arr['plates']:\n        # start",
        "for plate in plates:\n        # start_gate",
        "for _pl in plates:\n        # start_gate",
    ):
        assert anti_loop not in src, (
            f"per-plate start-command emission detected: {anti_loop!r}")
    # Positive check: readiness always emits gated_plate keyed off
    # plates_state[0] / arr['plates'][0] (plate 1), never plate N.
    # Confirm this by re-reading the workflow after running one legacy
    # kit commit through it and ensuring readiness_card_event only
    # contains plate1 in gated_plate + start_gate_stage1_command.


# ─── Pre-start cancel window ─────────────────────────────────────────────

def test_grace_period_default_120s_from_env(monkeypatch):
    """No CLI override + no env → default 120s."""
    import u1_print_start_gate as gate
    monkeypatch.delenv("U1_GRACE_PERIOD_SECONDS", raising=False)
    assert gate._resolve_grace_seconds(None) == 120


def test_grace_period_env_var_override(sandbox_requests, monkeypatch):
    """Env var respected when CLI not set (sandbox fixture sets it to 0,
    but this test overrides to a real value)."""
    import u1_print_start_gate as gate
    monkeypatch.setenv("U1_GRACE_PERIOD_SECONDS", "45")
    assert gate._resolve_grace_seconds(None) == 45


def test_grace_period_env_zero_disables(sandbox_requests, monkeypatch):
    """`U1_GRACE_PERIOD_SECONDS=0` opts out (power users at the printer)."""
    import u1_print_start_gate as gate
    monkeypatch.setenv("U1_GRACE_PERIOD_SECONDS", "0")
    assert gate._resolve_grace_seconds(None) == 0


def test_grace_period_cli_overrides_env(sandbox_requests, monkeypatch):
    """CLI arg wins over env."""
    import u1_print_start_gate as gate
    monkeypatch.setenv("U1_GRACE_PERIOD_SECONDS", "999")
    assert gate._resolve_grace_seconds(30) == 30


def test_grace_period_bad_env_falls_back_to_default(monkeypatch):
    """Junk env value can't accidentally disable safety."""
    import u1_print_start_gate as gate
    monkeypatch.setenv("U1_GRACE_PERIOD_SECONDS", "not-an-int")
    assert gate._resolve_grace_seconds(None) == 120


def test_grace_period_negative_clamped_to_zero():
    """Negative values clamp to 0 (disable), never to something dangerous."""
    import u1_print_start_gate as gate
    assert gate._resolve_grace_seconds(-30) == 0


def test_grace_period_cancel_marker_prevents_start_func(sandbox_requests, monkeypatch):
    """The load-bearing test: if the cancel marker appears within the
    grace window, start_func is NEVER called. HTTP never reaches the
    printer. This is the safety net."""
    import u1_print_start_gate as gate
    rid = "u1_test_grace_cancel"
    _seed_request(sandbox_requests, rid,
                  kit={"parts": [{"part_id": "p1"}], "part_count": 1},
                  safety={
                      "approval_token": "test_token",
                      "stage2_approval_nonce": "n",
                      "stage2_approval_binds": {
                          "request_revision": 1,
                          "gcode_hash": "sha256:abc123",
                          "prompt_key": "bed_clear_start",
                      },
                  })
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: {"token": "test_token"})
    import u1_safety
    monkeypatch.setattr(u1_safety, "can_start", lambda req: (True, "ok"))
    monkeypatch.setattr(gate, "capture_real_bed_photo",
                        lambda *a, **kw: {"ok": True, "brightness_check": "deferred"})
    start_called = {"hit": False}
    # sleep_fn: after 2 sim-seconds the "operator" touches the cancel marker
    ticks = {"n": 0}
    out_dir = u1_request.request_dir(rid)
    cancel_marker = out_dir / "pre_start_cancel.marker"
    def fake_sleep(sec):
        ticks["n"] += 1
        if ticks["n"] == 2:
            cancel_marker.parent.mkdir(parents=True, exist_ok=True)
            cancel_marker.write_text("cancel")
    res = gate.run_gate(
        "test_plate1.gcode", "start", host="127.0.0.1", port=7125,
        approval_token="test_token", stage2_approval_nonce="n",
        request_id=rid, operator="telegram:brent",
        start_func=lambda *a, **kw: (start_called.__setitem__("hit", True) or {"ok": True}),
        grace_seconds=10, grace_sleep_fn=fake_sleep,
        out_dir=out_dir)
    assert not start_called["hit"], (
        "cancel marker MUST prevent start_func — this is the safety "
        "safety net the whole thing was built for")
    assert res["ok"] is False
    assert res["started"] is False
    assert "cancelled during the pre-start grace" in res["reason"]


def test_grace_cancel_during_final_tick_still_prevents_start(sandbox_requests, monkeypatch):
    """TOCTOU race pin: the DM counts the window down, so an operator
    racing the deadline lands their CANCEL during the LAST sleep tick.
    The final re-check after the loop must still catch it — no HTTP."""
    import u1_print_start_gate as gate
    rid = "u1_test_grace_lasttick"
    _seed_request(sandbox_requests, rid,
                  kit={"parts": [{"part_id": "p1"}], "part_count": 1},
                  safety={
                      "approval_token": "test_token",
                      "stage2_approval_nonce": "n",
                      "stage2_approval_binds": {
                          "request_revision": 1,
                          "gcode_hash": "sha256:abc123",
                          "prompt_key": "bed_clear_start",
                      },
                  })
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: {"token": "test_token"})
    import u1_safety
    monkeypatch.setattr(u1_safety, "can_start", lambda req: (True, "ok"))
    monkeypatch.setattr(gate, "capture_real_bed_photo",
                        lambda *a, **kw: {"ok": True, "brightness_check": "deferred"})
    start_called = {"hit": False}
    ticks = {"n": 0}
    out_dir = u1_request.request_dir(rid)
    cancel_marker = out_dir / "pre_start_cancel.marker"

    def fake_sleep(sec):
        ticks["n"] += 1
        if ticks["n"] == 3:  # grace_seconds=3 → this is the LAST tick
            cancel_marker.parent.mkdir(parents=True, exist_ok=True)
            cancel_marker.write_text("cancel")

    res = gate.run_gate(
        "test_plate1.gcode", "start", host="127.0.0.1", port=7125,
        approval_token="test_token", stage2_approval_nonce="n",
        request_id=rid, operator="telegram:brent",
        start_func=lambda *a, **kw: (start_called.__setitem__("hit", True) or {"ok": True}),
        grace_seconds=3, grace_sleep_fn=fake_sleep,
        out_dir=out_dir)
    assert not start_called["hit"], (
        "a CANCEL during the final sleep tick was silently lost — the "
        "operator was promised the full window")
    assert res["ok"] is False and res["started"] is False


def test_grace_cancel_refusal_offers_stage1_recovery(sandbox_requests, monkeypatch):
    """Usability moat-crossing: a grace-cancel must not dead-end. The slice
    and upload are still valid — the refusal payload hands the operator the
    fresh-photo Stage 1 command so restarting costs one photo + one yes,
    not a whole re-run."""
    import u1_print_start_gate as gate
    rid = "u1_test_grace_recovery"
    _seed_request(sandbox_requests, rid,
                  kit={"parts": [{"part_id": "p1"}], "part_count": 1},
                  safety={
                      "approval_token": "test_token",
                      "stage2_approval_nonce": "n",
                      "stage2_approval_binds": {
                          "request_revision": 1,
                          "gcode_hash": "sha256:abc123",
                          "prompt_key": "bed_clear_start",
                      },
                  })
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: {"token": "test_token"})
    import u1_safety
    monkeypatch.setattr(u1_safety, "can_start", lambda req: (True, "ok"))
    monkeypatch.setattr(gate, "capture_real_bed_photo",
                        lambda *a, **kw: {"ok": True, "brightness_check": "deferred"})
    out_dir = u1_request.request_dir(rid)
    cancel_marker = out_dir / "pre_start_cancel.marker"

    def fake_sleep(sec):
        cancel_marker.parent.mkdir(parents=True, exist_ok=True)
        cancel_marker.write_text("cancel")

    res = gate.run_gate(
        "test_plate1.gcode", "start", host="127.0.0.1", port=7125,
        intended_tool="T1", requested_material="PLA",
        approval_token="test_token", stage2_approval_nonce="n",
        request_id=rid, operator="telegram:brent",
        start_func=lambda *a, **kw: {"ok": True},
        grace_seconds=5, grace_sleep_fn=fake_sleep,
        out_dir=out_dir)
    assert res["ok"] is False
    rec = res.get("recovery")
    assert rec, "cancel refusal must carry a recovery block"
    assert "stage1_command" in rec and rid in rec["stage1_command"]
    assert "--intended-tool T1" in rec["stage1_command"]


def test_grace_period_expires_and_start_proceeds(sandbox_requests, monkeypatch):
    """No cancel within window → after grace_seconds, start_func fires."""
    import u1_print_start_gate as gate
    rid = "u1_test_grace_proceed"
    _seed_request(sandbox_requests, rid,
                  kit={"parts": [{"part_id": "p1"}], "part_count": 1},
                  safety={
                      "approval_token": "test_token",
                      "stage2_approval_nonce": "n",
                      "stage2_approval_binds": {
                          "request_revision": 1,
                          "gcode_hash": "sha256:abc123",
                          "prompt_key": "bed_clear_start",
                      },
                  })
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: {"token": "test_token"})
    import u1_safety
    monkeypatch.setattr(u1_safety, "can_start", lambda req: (True, "ok"))
    monkeypatch.setattr(gate, "capture_real_bed_photo",
                        lambda *a, **kw: {"ok": True, "brightness_check": "deferred"})
    start_called = {"hit": False}
    res = gate.run_gate(
        "test_plate1.gcode", "start", host="127.0.0.1", port=7125,
        approval_token="test_token", stage2_approval_nonce="n",
        request_id=rid, operator="telegram:brent",
        start_func=lambda *a, **kw: (start_called.__setitem__("hit", True) or {"result": "ok"}),
        grace_seconds=3, grace_sleep_fn=lambda s: None,  # no-op sleep = instant
        out_dir=u1_request.request_dir(rid))
    assert start_called["hit"], "no cancel → must proceed to start_func after grace"
    assert res["ok"] is True
    assert res["started"] is True


def test_grace_period_zero_skips_entirely(sandbox_requests, monkeypatch):
    """grace_seconds=0 → no wait, no marker check, no audit rows for
    the grace period. Opt-out for power users."""
    import u1_print_start_gate as gate
    rid = "u1_test_grace_zero"
    _seed_request(sandbox_requests, rid,
                  kit={"parts": [{"part_id": "p1"}], "part_count": 1},
                  safety={
                      "approval_token": "test_token",
                      "stage2_approval_nonce": "n",
                      "stage2_approval_binds": {
                          "request_revision": 1,
                          "gcode_hash": "sha256:abc123",
                          "prompt_key": "bed_clear_start",
                      },
                  })
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: {"token": "test_token"})
    import u1_safety
    monkeypatch.setattr(u1_safety, "can_start", lambda req: (True, "ok"))
    monkeypatch.setattr(gate, "capture_real_bed_photo",
                        lambda *a, **kw: {"ok": True, "brightness_check": "deferred"})
    slept = {"n": 0}
    def bad_sleep(s):
        slept["n"] += 1
    gate.run_gate(
        "test_plate1.gcode", "start", host="127.0.0.1", port=7125,
        approval_token="test_token", stage2_approval_nonce="n",
        request_id=rid, operator="telegram:brent",
        start_func=lambda *a, **kw: {"result": "ok"},
        grace_seconds=0, grace_sleep_fn=bad_sleep,
        out_dir=u1_request.request_dir(rid))
    assert slept["n"] == 0, "grace_seconds=0 must skip the wait entirely"


def test_grace_period_stale_marker_from_prior_run_is_cleared(sandbox_requests, monkeypatch):
    """A leftover cancel marker from a PRIOR request must not
    immediately cancel THIS run. Fresh window per invocation."""
    import u1_print_start_gate as gate
    rid = "u1_test_grace_stale"
    _seed_request(sandbox_requests, rid,
                  kit={"parts": [{"part_id": "p1"}], "part_count": 1},
                  safety={
                      "approval_token": "test_token",
                      "stage2_approval_nonce": "n",
                      "stage2_approval_binds": {
                          "request_revision": 1,
                          "gcode_hash": "sha256:abc123",
                          "prompt_key": "bed_clear_start",
                      },
                  })
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: {"token": "test_token"})
    import u1_safety
    monkeypatch.setattr(u1_safety, "can_start", lambda req: (True, "ok"))
    monkeypatch.setattr(gate, "capture_real_bed_photo",
                        lambda *a, **kw: {"ok": True, "brightness_check": "deferred"})
    # Pre-seed a stale marker
    out_dir = u1_request.request_dir(rid)
    out_dir.mkdir(parents=True, exist_ok=True)
    stale = out_dir / "pre_start_cancel.marker"
    stale.write_text("leftover")
    start_called = {"hit": False}
    gate.run_gate(
        "test_plate1.gcode", "start", host="127.0.0.1", port=7125,
        approval_token="test_token", stage2_approval_nonce="n",
        request_id=rid, operator="telegram:brent",
        start_func=lambda *a, **kw: (start_called.__setitem__("hit", True) or {"result": "ok"}),
        grace_seconds=2, grace_sleep_fn=lambda s: None,
        out_dir=out_dir)
    assert start_called["hit"], (
        "stale cancel marker from prior run was not cleared — safety "
        "net would falsely cancel every subsequent print")


# ─── Grace-period notify command ──────────────────────────────────────────

def test_grace_notify_cmd_resolution_from_env(sandbox_requests, monkeypatch):
    """CLI arg wins over env var."""
    import u1_print_start_gate as gate
    monkeypatch.setenv("U1_GRACE_NOTIFY_CMD", "env-cmd")
    assert gate._resolve_grace_notify_cmd("cli-cmd") == "cli-cmd"
    assert gate._resolve_grace_notify_cmd(None) == "env-cmd"


def test_grace_notify_cmd_none_when_unset(monkeypatch):
    """No CLI + no env → None (no notification)."""
    import u1_print_start_gate as gate
    monkeypatch.delenv("U1_GRACE_NOTIFY_CMD", raising=False)
    assert gate._resolve_grace_notify_cmd(None) is None


def test_grace_notify_cmd_fired_when_window_opens(sandbox_requests, monkeypatch):
    """Notify command MUST run when the grace window opens, with U1_* env
    vars exported. Notification fires BEFORE the wait loop starts so the
    operator has the full window to react."""
    import u1_print_start_gate as gate
    rid = "u1_test_notify_fires"
    _seed_request(sandbox_requests, rid,
                  kit={"parts": [{"part_id": "p1"}], "part_count": 1},
                  safety={
                      "approval_token": "test_token",
                      "stage2_approval_nonce": "n",
                      "stage2_approval_binds": {
                          "request_revision": 1,
                          "gcode_hash": "sha256:abc123",
                          "prompt_key": "bed_clear_start",
                      },
                  })
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: {"token": "test_token"})
    import u1_safety
    monkeypatch.setattr(u1_safety, "can_start", lambda req: (True, "ok"))
    monkeypatch.setattr(gate, "capture_real_bed_photo",
                        lambda *a, **kw: {"ok": True, "brightness_check": "deferred"})
    notify_calls = []
    def fake_notify(cmd, *, request_id, filename, grace_seconds,
                    cancel_marker, operator):
        notify_calls.append({
            "cmd": cmd, "request_id": request_id, "filename": filename,
            "grace_seconds": grace_seconds, "cancel_marker": str(cancel_marker),
            "operator": operator,
        })
        return {"ok": True, "exit_code": 0, "stderr_tail": ""}
    gate.run_gate(
        "test_plate1.gcode", "start", host="127.0.0.1", port=7125,
        approval_token="test_token", stage2_approval_nonce="n",
        request_id=rid, operator="telegram:brent",
        start_func=lambda *a, **kw: {"result": "ok"},
        grace_seconds=3, grace_sleep_fn=lambda s: None,
        grace_notify_cmd="my-notify-cmd",
        grace_notify_fn=fake_notify,
        out_dir=u1_request.request_dir(rid))
    assert len(notify_calls) == 1, "notify must fire exactly once per window"
    call = notify_calls[0]
    assert call["cmd"] == "my-notify-cmd"
    assert call["request_id"] == rid
    assert call["filename"] == "test_plate1.gcode"
    assert call["grace_seconds"] == 3
    assert call["operator"] == "telegram:brent"
    assert "pre_start_cancel.marker" in call["cancel_marker"]


def test_grace_notify_failure_does_not_block_start(sandbox_requests, monkeypatch):
    """Q2: a configured countdown/CANCEL that cannot be delivered ABORTS the
    start (operator decision 2026-07-08) — a print the operator can neither
    see nor cancel must not fire. Escape hatch tested separately."""
    import u1_print_start_gate as gate
    rid = "u1_test_notify_fail"
    _seed_request(sandbox_requests, rid,
                  kit={"parts": [{"part_id": "p1"}], "part_count": 1},
                  safety={
                      "approval_token": "test_token",
                      "stage2_approval_nonce": "n",
                      "stage2_approval_binds": {
                          "request_revision": 1,
                          "gcode_hash": "sha256:abc123",
                          "prompt_key": "bed_clear_start",
                      },
                  })
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: {"token": "test_token"})
    import u1_safety
    monkeypatch.setattr(u1_safety, "can_start", lambda req: (True, "ok"))
    monkeypatch.setattr(gate, "capture_real_bed_photo",
                        lambda *a, **kw: {"ok": True, "brightness_check": "deferred"})
    def fake_notify_fail(cmd, **kw):
        return {"ok": False, "exit_code": 1, "stderr_tail": "network down"}
    start_hit = {"n": 0}
    res = gate.run_gate(
        "test_plate1.gcode", "start", host="127.0.0.1", port=7125,
        approval_token="test_token", stage2_approval_nonce="n",
        request_id=rid, operator="telegram:brent",
        start_func=lambda *a, **kw: (start_hit.__setitem__("n", start_hit["n"]+1)
                                     or {"ok": True}),
        grace_seconds=2, grace_sleep_fn=lambda s: None,
        grace_notify_cmd="broken-cmd",
        grace_notify_fn=fake_notify_fail,
        out_dir=u1_request.request_dir(rid))
    # Q2 (operator decision 2026-07-08): a configured countdown/CANCEL message
    # that CANNOT be delivered aborts the start — never fire a print the
    # operator can't see or cancel.
    assert start_hit["n"] == 0, "undeliverable countdown must block the start"
    assert res["started"] is False
    assert res["stage"] == "gate_refused_notify_undeliverable"


def test_grace_notify_failure_can_be_overridden_to_proceed(sandbox_requests, monkeypatch):
    """The escape hatch: U1_GRACE_NOTIFY_OPTIONAL=1 restores the old fail-open
    behavior for anyone who wants it (e.g. a headless/monitored setup)."""
    import u1_print_start_gate as gate
    monkeypatch.setenv("U1_GRACE_NOTIFY_OPTIONAL", "1")
    rid = "u1_test_notify_optional"
    _seed_request(sandbox_requests, rid,
                  kit={"parts": [{"part_id": "p1"}], "part_count": 1},
                  safety={"approval_token": "test_token",
                          "stage2_approval_nonce": "n",
                          "stage2_approval_binds": {
                              "request_revision": 1,
                              "gcode_hash": "sha256:abc123",
                              "prompt_key": "bed_clear_start"}})
    monkeypatch.setattr(gate, "_approval_token_valid", lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: {"token": "test_token"})
    import u1_safety
    monkeypatch.setattr(u1_safety, "can_start", lambda req: (True, "ok"))
    monkeypatch.setattr(gate, "capture_real_bed_photo",
                        lambda *a, **kw: {"ok": True, "brightness_check": "deferred"})
    start_hit = {"n": 0}
    gate.run_gate(
        "test_plate1.gcode", "start", host="127.0.0.1", port=7125,
        approval_token="test_token", stage2_approval_nonce="n",
        request_id=rid, operator="telegram:brent",
        start_func=lambda *a, **kw: start_hit.__setitem__("n", start_hit["n"]+1) or {"ok": True},
        grace_seconds=2, grace_sleep_fn=lambda s: None,
        grace_notify_cmd="broken-cmd",
        grace_notify_fn=lambda cmd, **kw: {"ok": False, "exit_code": 1, "stderr_tail": "down"},
        out_dir=u1_request.request_dir(rid))
    assert start_hit["n"] == 1  # opt-out honored: start proceeds


def test_grace_no_notify_cmd_still_works(sandbox_requests, monkeypatch):
    """No notify command configured → grace period still runs cleanly.
    Nobody gets notified, but the SSH-touch cancel path still works."""
    import u1_print_start_gate as gate
    rid = "u1_test_no_notify"
    _seed_request(sandbox_requests, rid,
                  kit={"parts": [{"part_id": "p1"}], "part_count": 1},
                  safety={
                      "approval_token": "test_token",
                      "stage2_approval_nonce": "n",
                      "stage2_approval_binds": {
                          "request_revision": 1,
                          "gcode_hash": "sha256:abc123",
                          "prompt_key": "bed_clear_start",
                      },
                  })
    monkeypatch.setattr(gate, "_approval_token_valid",
                        lambda stored, tok: (True, "ok"))
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(gate, "query_state", lambda h, p: {})
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: {"token": "test_token"})
    import u1_safety
    monkeypatch.setattr(u1_safety, "can_start", lambda req: (True, "ok"))
    monkeypatch.setattr(gate, "capture_real_bed_photo",
                        lambda *a, **kw: {"ok": True, "brightness_check": "deferred"})
    monkeypatch.delenv("U1_GRACE_NOTIFY_CMD", raising=False)
    notify_hit = {"n": 0}
    def bad_notify(*a, **kw):
        notify_hit["n"] += 1
        return {"ok": True, "exit_code": 0, "stderr_tail": ""}
    gate.run_gate(
        "test_plate1.gcode", "start", host="127.0.0.1", port=7125,
        approval_token="test_token", stage2_approval_nonce="n",
        request_id=rid, operator="telegram:brent",
        start_func=lambda *a, **kw: {"ok": True},
        grace_seconds=2, grace_sleep_fn=lambda s: None,
        grace_notify_fn=bad_notify,
        out_dir=u1_request.request_dir(rid))
    assert notify_hit["n"] == 0, "no notify_cmd → notify_fn must not fire"


# ─── Pending-nonce binding: only the VERBATIM yes-command can confirm ───────

def test_action_start_confirm_without_pending_nonce_refused(sandbox_requests):
    """A hand-assembled `--action start --bed-clear-confirmed` (no
    --pending-nonce) must be refused — the confirm has to be the verbatim
    next_command_on_yes emitted at the yes/no prompt."""
    rid = "u1_test_nonce_hand"
    _seed_request(sandbox_requests, rid)
    first = kw._action_start(None, rid, False, bed_clear_confirmed=False)
    assert first["phase"] == "awaiting_bed_clear_start"
    result = kw._action_start(None, rid, False, bed_clear_confirmed=True)
    assert result["phase"] == "bed_clear_approval_rejected"
    assert any("nonce" in r for r in result["reasons"])


def test_action_start_arms_model_free_confirm_marker(sandbox_requests, capsys,
                                                     tmp_path, monkeypatch):
    """Model-free YES (incident 2026-07-07: the model fired the emitted
    confirm command itself). The bed-clear event carries NO start command;
    the workflow arms an OPAQUE marker (no command, no token — the hook
    builds its own --confirm-start-for argv) while the single-use token is
    persisted server-side, and only the gateway hook redeems it."""
    import json as _json
    import u1_form
    monkeypatch.setattr(kw, "_PENDING_CONFIRM_DIR", tmp_path / "pending_confirm")
    monkeypatch.setattr(kw.u1_config, "get_operator_binding",
                        lambda: ("telegram", "555000111"))
    rid = "u1_test_nonce_cmd"
    _seed_request(sandbox_requests, rid)
    kw._action_start(None, rid, True, yes_command="python3 kit.py --action start --bed-clear-confirmed",
                     bed_clear_confirmed=False)
    out = capsys.readouterr().out
    evs = [_json.loads(l) for l in out.splitlines() if l.strip().startswith("{")]
    need = next(e for e in evs if e.get("stage") == "need_input")
    assert "next_command_on_yes" not in need
    assert "--confirm-start" not in out           # token never shown to the model
    marker_text = (tmp_path / "pending_confirm" / f"{rid}.json").read_text()
    marker = _json.loads(marker_text)
    assert "confirm_cmd" not in marker            # marker carries NO argv
    assert "log_path" not in marker
    assert marker["platform"] == "telegram"
    assert marker["operator_user_id"] == "555000111"
    # the single-use token lives server-side, resolvable to this request —
    # and never appears in the marker file the hook (or /tmp) can see
    tok = (u1_request.read_request(rid)["safety"]
           ["pending_bed_clear_start"]["confirm_token"])
    assert u1_form.resolve_confirm_token(tok, consume=False) == rid
    assert tok not in marker_text
    assert u1_request.read_request(rid)["safety"]["pending_bed_clear_start"]["nonce"]


def test_manual_bed_check_refused_when_camera_verification_available(sandbox_requests):
    """The Layer-3 manual override exists for the degraded-camera case. When
    a real photo + token already exist, the override is refused so nothing
    can route around the photo."""
    rid = "u1_test_manual_guard"
    _seed_request(sandbox_requests, rid,
                  safety={"approval_token": "tok123",
                          "bed_clear_photo_captured": True})
    result = kw._action_start_manual_bed_check(
        None, rid, "test:unit", False,
        operator_text="start manual-bed-check",
        verification_method="snapmaker_app",
        bed_clear_confirmed=False)
    assert result["phase"] == "manual_bed_check_refused"

