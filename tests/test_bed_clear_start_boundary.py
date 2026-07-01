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
    """Redirect u1_request storage to tmp_path so tests don't touch real state."""
    root = tmp_path / "requests"
    root.mkdir()
    monkeypatch.setattr(u1_request, "_requests_root", lambda: root, raising=False)
    monkeypatch.setattr(u1_request, "request_dir",
                        lambda rid: root / rid, raising=False)
    (tmp_path / "bed_snapshot.jpg").write_bytes(b"stub")
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
    assert "yes/no" in result["prompt"]
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


def test_action_start_second_call_success_mints_stage2_nonce(sandbox_requests):
    """Happy path: pending matches + hash matches + phase matches → emits
    Stage 2 command that includes --stage2-approval-nonce; pending is
    consumed; safety.stage2_approval_nonce is persisted."""
    rid = "u1_test_happy"
    _seed_request(sandbox_requests, rid)
    kw._action_start(None, rid, False, bed_clear_confirmed=False)
    result = kw._action_start(None, rid, False, bed_clear_confirmed=True)
    assert result["phase"] == "awaiting_print_start"
    cmd = result["command"]
    assert "--bed-clear start" in cmd
    assert "--stage2-approval-nonce" in cmd
    # pending is consumed
    state = u1_request.read_request(rid)
    assert state["safety"].get("pending_bed_clear_start") is None
    # nonce persisted for Stage 2 gate to consume
    assert state["safety"].get("stage2_approval_nonce")
    binds = state["safety"].get("stage2_approval_binds")
    assert binds["request_revision"] == 1
    assert binds["gcode_hash"] == "sha256:abc123"
    assert binds["prompt_key"] == "bed_clear_start"


def test_bed_clear_start_prompt_key_is_bed_clear_start(sandbox_requests, capsys):
    """The need_input event must carry key=bed_clear_start (Hermy's spec)."""
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
    assert "next_command_on_yes" in need
    assert "--bed-clear-confirmed" in need["next_command_on_yes"]
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
    result = kw._action_start_manual_bed_check(
        None, rid, "test:unit", False,
        operator_text="start manual-bed-check",
        verification_method="snapmaker_app",
        bed_clear_confirmed=True)
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
    "smoke:med1", "test:integration", "dev:brent",
    "dry:sanity", "mock:printer", "ci:workflow",
    "SMOKE:capitalized", "Fixture:auto",
])
def test_stage2_gate_refuses_test_prefixed_operator(
        sandbox_requests, monkeypatch, capsys, test_op):
    """Fence 1: any --operator starting with smoke:/test:/dev:/dry:/mock:/
    ci:/fixture: is refused BEFORE any Moonraker call. Test operators
    can never send print traffic to a real printer regardless of nonce
    validity, approval token, or preflight state."""
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
    gate.run_gate("test_plate1.gcode", "start", host="127.0.0.1", port=7125,
                  approval_token="test_token",
                  stage2_approval_nonce="anything",
                  request_id=rid,
                  operator=test_op,
                  out_dir=u1_request.request_dir(rid))
    out = capsys.readouterr().out
    payload = json.loads(out)
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
    """Fence 1 must NOT over-block: production operator strings like
    'telegram:brent', 'discord:ops', 'human:brent', bare 'brent', or an
    unknown-shaped operator must proceed past the fence."""
    import u1_print_start_gate as gate
    called = {"query_state": False}
    monkeypatch.setattr(gate, "query_state",
                        lambda h, p: called.__setitem__("query_state", True) or {})
    monkeypatch.setattr(gate, "preflight", lambda *a, **kw: ["some blocker"])
    monkeypatch.setattr(gate, "_read_approval_token", lambda d: None)
    for op in ("telegram:brent", "discord:ops", "human:brent", "brent", "unknown:x"):
        rid = f"u1_test_gate_fence1_pass_{op.replace(':','_')}"
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
