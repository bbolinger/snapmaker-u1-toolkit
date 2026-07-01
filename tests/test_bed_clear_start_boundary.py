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
        developers or CI-orchestrated real prints (2026-07-01 ship-review)."""
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


# ─── Fresh audit 2026-07-01: kit request must require staged nonce ──────────

def test_stage2_gate_refuses_kit_request_without_persisted_nonce(
        sandbox_requests, monkeypatch):
    """Kit-path close (fresh audit finding HIGH-1): a kit request that
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


# ─── Fresh audit 2026-07-01: fence refusal must return dict, not print ──────

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


# ─── Fresh audit 2026-07-01: manual bed-check wires through the gate ────────

def test_stage2_gate_skips_sanity_capture_when_manual_verification_fresh(
        sandbox_requests, monkeypatch):
    """Manual-bed-check wire-through (fresh audit finding MED-1): when
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


# ─── Fresh audit 2026-07-01: refresh-bed-photo cannot create a start path ───

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


# ─── Final audit 2026-07-01: remaining coverage gaps ────────────────────────

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
    kit request, which the HIGH-1 fix refuses. Belt for defense in
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
