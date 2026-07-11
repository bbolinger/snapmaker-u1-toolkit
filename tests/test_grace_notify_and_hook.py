"""Direct-invocation tests for the grace-period notify shell script AND
the Hermes gateway cancel-hook handler. Unit-level, no Hermes runtime
needed — subprocess for bash, direct import for Python.

Guards the contract:
  * shell notify writes <pending-cancel dir>/<request_id>.json with
    the exact schema the handler expects
  * shell notify does NOT write the pending file when `hermes send`
    fails (send-first ordering)
  * handler touches marker on bare `cancel` / `stop` / `abort`
  * handler ignores substrings like "cancel that idea" (exact match only)
  * handler ignores expired pending entries (belt for a killed gate)
  * multi-request pending dir: cancel touches ALL active markers
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


HANDLER_PATH = (
    Path(__file__).parent.parent
    / "tools" / "hermes_hooks" / "u1_grace_cancel" / "handler.py"
)
NOTIFY_SCRIPT = (
    Path(__file__).parent.parent
    / "tools" / "u1_grace_notify_hermes.sh"
)


def _load_handler():
    spec = importlib.util.spec_from_file_location("u1_grace_cancel_handler",
                                                  HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def sandbox_pending_dir(tmp_path, monkeypatch):
    """Redirect the handler's PENDING_DIR to tmp_path so tests can't
    stomp on real /tmp state."""
    handler = _load_handler()
    pending = tmp_path / "u1_pending_cancel"
    pending.mkdir()
    monkeypatch.setattr(handler, "PENDING_DIR", pending)
    monkeypatch.setattr(handler, "LOG_FILE", tmp_path / "hook.log")
    return handler, pending, tmp_path


def _seed_pending(pending_dir: Path, request_id: str,
                  marker: Path, expires_at: str | None = None) -> Path:
    if expires_at is None:
        expires_at = (datetime.now(timezone.utc)
                      + timedelta(seconds=300)).isoformat()
    state = {
        "request_id": request_id,
        "cancel_marker": str(marker),
        "filename": "test.gcode",
        "grace_seconds": 120,
        "expires_at": expires_at,
    }
    path = pending_dir / f"{request_id}.json"
    path.write_text(json.dumps(state))
    return path


def _run(handler, text: str):
    """Async wrapper — the handler's handle() is async."""
    asyncio.run(handler.handle("agent:start",
                               {"platform": "telegram",
                                "user_id": "test-user",
                                "message": text}))


# ─── Handler tests ──────────────────────────────────────────────────────────

def test_handler_touches_marker_on_bare_cancel(sandbox_pending_dir):
    handler, pending, tmp = sandbox_pending_dir
    marker = tmp / "marker_A.txt"
    _seed_pending(pending, "u1_2026_0701_abc123", marker)
    _run(handler, "cancel")
    assert marker.exists()
    assert "cancel via telegram hook" in marker.read_text()


def test_handler_touches_marker_on_bare_stop(sandbox_pending_dir):
    handler, pending, tmp = sandbox_pending_dir
    marker = tmp / "marker_stop.txt"
    _seed_pending(pending, "u1_2026_0701_stop01", marker)
    _run(handler, "stop")
    assert marker.exists()


def test_handler_touches_marker_on_bare_abort(sandbox_pending_dir):
    handler, pending, tmp = sandbox_pending_dir
    marker = tmp / "marker_abort.txt"
    _seed_pending(pending, "u1_2026_0701_abrt01", marker)
    _run(handler, "abort")
    assert marker.exists()


def test_handler_case_insensitive(sandbox_pending_dir):
    handler, pending, tmp = sandbox_pending_dir
    marker = tmp / "marker_case.txt"
    _seed_pending(pending, "u1_2026_0701_case01", marker)
    _run(handler, "CANCEL")
    assert marker.exists()


def test_handler_accepts_slash_form(sandbox_pending_dir):
    handler, pending, tmp = sandbox_pending_dir
    marker = tmp / "marker_slash.txt"
    _seed_pending(pending, "u1_2026_0701_slsh01", marker)
    _run(handler, "/cancel")
    assert marker.exists()


def test_handler_ignores_substring_in_sentence(sandbox_pending_dir):
    """A message like 'cancel that plan' is NOT an exact match — should
    be safe from unintended cancels during a grace window."""
    handler, pending, tmp = sandbox_pending_dir
    marker = tmp / "marker_sentence.txt"
    _seed_pending(pending, "u1_2026_0701_sent01", marker)
    _run(handler, "let's cancel that idea")
    assert not marker.exists()
    _run(handler, "cancel the meeting")
    assert not marker.exists()


def test_handler_touches_all_markers_when_multiple_pending(sandbox_pending_dir):
    """Multiple concurrent grace windows each have their
    own pending file. A bare cancel is intended as 'stop what's about
    to happen' — touch every active marker."""
    handler, pending, tmp = sandbox_pending_dir
    marker_a = tmp / "marker_a.txt"
    marker_b = tmp / "marker_b.txt"
    _seed_pending(pending, "u1_2026_0701_aaaaa1", marker_a)
    _seed_pending(pending, "u1_2026_0701_bbbbb2", marker_b)
    _run(handler, "cancel")
    assert marker_a.exists()
    assert marker_b.exists()


def test_handler_ignores_expired_pending_entry(sandbox_pending_dir):
    """Belt for a killed gate that leaves a stale pending file.
    Handler must not touch an expired marker."""
    handler, pending, tmp = sandbox_pending_dir
    marker = tmp / "marker_expired.txt"
    expired_ts = (datetime.now(timezone.utc)
                  - timedelta(seconds=60)).isoformat()
    _seed_pending(pending, "u1_2026_0701_expir1", marker,
                  expires_at=expired_ts)
    _run(handler, "cancel")
    assert not marker.exists()


def test_handler_no_pending_dir_returns_cleanly(tmp_path, monkeypatch):
    """If /tmp/u1_pending_cancel/ doesn't exist yet (fresh install,
    no active window), handler must not crash."""
    handler = _load_handler()
    monkeypatch.setattr(handler, "PENDING_DIR", tmp_path / "does_not_exist")
    monkeypatch.setattr(handler, "LOG_FILE", tmp_path / "hook.log")
    _run(handler, "cancel")  # must not raise


def test_handler_extracts_text_from_nested_context(sandbox_pending_dir):
    """Some gateway platforms wrap the message in a dict. The extractor
    should reach into common sub-keys."""
    handler, pending, tmp = sandbox_pending_dir
    marker = tmp / "marker_nested.txt"
    _seed_pending(pending, "u1_2026_0701_nest01", marker)
    asyncio.run(handler.handle("agent:start",
                               {"platform": "discord",
                                "message": {"text": "cancel"}}))
    assert marker.exists()


def test_handler_ignores_empty_message(sandbox_pending_dir):
    """No text → no-op (attachment-only message, presence update, etc.)"""
    handler, pending, tmp = sandbox_pending_dir
    marker = tmp / "marker_empty.txt"
    _seed_pending(pending, "u1_2026_0701_empt01", marker)
    _run(handler, "")
    assert not marker.exists()


# ─── Notify shell script tests ──────────────────────────────────────────────

def _run_notify(env_overrides: dict[str, str], hermes_stub_exit: int = 0,
                tmpdir: Path | None = None) -> subprocess.CompletedProcess:
    """Run the notify script with `hermes` stubbed to a shell script
    that captures its args and returns the requested exit code."""
    env = os.environ.copy()
    env.update(env_overrides)
    if tmpdir is not None:
        stub = tmpdir / "hermes"
        stub.write_text(
            f"#!/bin/bash\n"
            f'echo "hermes called with: $@" > "{tmpdir}/hermes_calls.log"\n'
            f"exit {hermes_stub_exit}\n"
        )
        stub.chmod(0o755)
        env["PATH"] = f"{tmpdir}:{env.get('PATH', '')}"
        env["HERMES_BIN"] = str(stub)
    return subprocess.run(
        ["bash", str(NOTIFY_SCRIPT)],
        env=env, capture_output=True, text=True, timeout=10)


@pytest.fixture
def notify_env(tmp_path):
    """Common env for notify-script tests. U1_PENDING_CANCEL_DIR points
    into tmp_path — the same explicit-env contract the gate uses when it
    invokes the script — so tests never touch a real pending dir."""
    rid = f"u1_2026_0701_ntf{os.getpid() % 1000:03d}"
    repo = Path(__file__).resolve().parent.parent
    return {
        "U1_REQUEST_ID": rid,
        "U1_FILENAME": "test_plate1.gcode",
        "U1_GRACE_SECONDS": "120",
        "U1_CANCEL_MARKER": str(tmp_path / "the_marker"),
        "U1_OPERATOR": "test:script-verify",
        "U1_PENDING_CANCEL_DIR": str(tmp_path / "pending_cancel"),
        # Hermetic: no runtime .env (so no bot token reachable) and the
        # repo copy of the notifier — its Bot API path is skipped and it
        # falls through to the stubbed `hermes send`, same as before the
        # button existed.
        "HERMES_HOME": str(tmp_path),
        "TELEGRAM_BOT_TOKEN": "",
        "U1_NOTIFY_PY": str(repo / "scripts" / "u1_notify.py"),
    }, tmp_path


def test_notify_writes_pending_state_with_correct_schema(notify_env):
    env, tmp = notify_env
    r = _run_notify(env, hermes_stub_exit=0, tmpdir=tmp)
    assert r.returncode == 0, f"notify exited {r.returncode}: {r.stderr}"
    state_file = Path(env["U1_PENDING_CANCEL_DIR"]) / f"{env['U1_REQUEST_ID']}.json"
    assert state_file.exists(), "pending state file must be written on success"
    state = json.loads(state_file.read_text())
    assert state["request_id"] == env["U1_REQUEST_ID"]
    assert state["cancel_marker"] == env["U1_CANCEL_MARKER"]
    assert state["filename"] == env["U1_FILENAME"]
    assert state["grace_seconds"] == int(env["U1_GRACE_SECONDS"])
    # expires_at is ISO-parseable
    datetime.fromisoformat(state["expires_at"].replace("Z", "+00:00"))


def test_notify_calls_hermes_send_with_message(notify_env):
    env, tmp = notify_env
    r = _run_notify(env, hermes_stub_exit=0, tmpdir=tmp)
    assert r.returncode == 0
    calls_log = tmp / "hermes_calls.log"
    assert calls_log.exists(), "hermes stub was never invoked"
    log = calls_log.read_text()
    assert "send" in log
    assert "telegram" in log
    # The message must include the CANCEL instruction
    assert "cancel" in log.lower()


def test_notify_does_not_persist_pending_state_if_hermes_send_fails(notify_env):
    """Send-first ordering. If Telegram delivery fails, we
    must not leave a phantom pending window that a future unrelated
    cancel could hit."""
    env, tmp = notify_env
    r = _run_notify(env, hermes_stub_exit=1, tmpdir=tmp)
    assert r.returncode != 0, (
        "notify must exit non-zero when hermes send fails")
    state_file = Path(env["U1_PENDING_CANCEL_DIR"]) / f"{env['U1_REQUEST_ID']}.json"
    assert not state_file.exists(), (
        "pending state must NOT be written when hermes send fails")


def test_notify_writes_per_request_files_not_shared(notify_env, tmp_path):
    """Two concurrent notifies must NOT clobber each
    other. Each writes to <request_id>.json."""
    env_a, tmp_a = notify_env
    env_a["U1_REQUEST_ID"] = "u1_2026_0701_notfyA"
    env_a["U1_CANCEL_MARKER"] = str(tmp_path / "marker_a")
    env_b = dict(env_a)
    env_b["U1_REQUEST_ID"] = "u1_2026_0701_notfyB"
    env_b["U1_CANCEL_MARKER"] = str(tmp_path / "marker_b")
    r_a = _run_notify(env_a, hermes_stub_exit=0, tmpdir=tmp_a)
    r_b = _run_notify(env_b, hermes_stub_exit=0, tmpdir=tmp_a)
    assert r_a.returncode == 0 and r_b.returncode == 0
    pending = Path(env_a["U1_PENDING_CANCEL_DIR"])
    file_a = pending / "u1_2026_0701_notfyA.json"
    file_b = pending / "u1_2026_0701_notfyB.json"
    assert file_a.exists()
    assert file_b.exists()
    # Confirm they didn't clobber each other's marker paths
    state_a = json.loads(file_a.read_text())
    state_b = json.loads(file_b.read_text())
    assert state_a["cancel_marker"] == env_a["U1_CANCEL_MARKER"]
    assert state_b["cancel_marker"] == env_b["U1_CANCEL_MARKER"]


# ─── Code-scoped cancel (v2.1.0-rc2: `cancel <code>` is now implemented) ────

def test_handler_code_scoped_cancel_touches_only_matching_window(sandbox_pending_dir):
    handler, pending, tmp = sandbox_pending_dir
    m_a = tmp / "marker_A.txt"
    m_b = tmp / "marker_B.txt"
    _seed_pending(pending, "u1_2026_0701_abc123", m_a)
    _seed_pending(pending, "u1_2026_0701_zzz999", m_b)
    _run(handler, "cancel abc123")
    assert m_a.exists(), "code-matched window must be cancelled"
    assert not m_b.exists(), "non-matching window must be untouched"


def test_handler_code_with_no_match_cancels_nothing(sandbox_pending_dir):
    # A typo'd code must not fall back to cancel-all — that would let a
    # mistargeted cancel kill an unrelated print.
    handler, pending, tmp = sandbox_pending_dir
    marker = tmp / "marker_C.txt"
    _seed_pending(pending, "u1_2026_0701_abc123", marker)
    _run(handler, "cancel nope99")
    assert not marker.exists()


def test_handler_code_scoped_is_case_insensitive(sandbox_pending_dir):
    handler, pending, tmp = sandbox_pending_dir
    marker = tmp / "marker_D.txt"
    _seed_pending(pending, "u1_2026_0701_ABC123", marker)
    _run(handler, "CANCEL abc123")
    assert marker.exists()


def test_handler_prose_after_keyword_still_ignored(sandbox_pending_dir):
    handler, pending, tmp = sandbox_pending_dir
    marker = tmp / "marker_E.txt"
    _seed_pending(pending, "u1_2026_0701_abc123", marker)
    _run(handler, "cancel that plan for tomorrow")
    assert not marker.exists()


def test_handler_bare_cancel_still_cancels_all(sandbox_pending_dir):
    handler, pending, tmp = sandbox_pending_dir
    m_a = tmp / "marker_F.txt"
    m_b = tmp / "marker_G.txt"
    _seed_pending(pending, "u1_2026_0701_aaa111", m_a)
    _seed_pending(pending, "u1_2026_0701_bbb222", m_b)
    _run(handler, "cancel")
    assert m_a.exists() and m_b.exists()


# ─── Notify script: JSON safety + hook-receipt honesty ──────────────────────

def test_notify_state_file_valid_json_with_hostile_filename(notify_env):
    env, tmp = notify_env
    env = dict(env)
    env["U1_FILENAME"] = 'we"ird\nname\t|.gcode'
    r = _run_notify(env, hermes_stub_exit=0, tmpdir=tmp)
    assert r.returncode == 0, r.stderr
    state_file = Path(env["U1_PENDING_CANCEL_DIR"]) / f"{env['U1_REQUEST_ID']}.json"
    state = json.loads(state_file.read_text())  # must not raise
    assert state["filename"] == env["U1_FILENAME"]


def test_notify_message_advertises_ssh_fallback_without_hook_receipt(notify_env):
    env, tmp = notify_env
    env = dict(env)
    env["U1_CANCEL_HOOK_RECEIPT"] = str(tmp / "no_such_receipt")
    r = _run_notify(env, hermes_stub_exit=0, tmpdir=tmp)
    assert r.returncode == 0, r.stderr
    sent = (tmp / "hermes_calls.log").read_text()
    assert "hook not detected" in sent
    assert "or reply CANCEL" not in sent, (
        "without the hook, promising reply-CANCEL is a lie — the reply "
        "would silently do nothing")


def test_notify_message_advertises_reply_cancel_when_hook_installed(notify_env):
    env, tmp = notify_env
    env = dict(env)
    receipt = tmp / "receipt.json"
    receipt.write_text('{"hook": "u1_grace_cancel"}')
    env["U1_CANCEL_HOOK_RECEIPT"] = str(receipt)
    r = _run_notify(env, hermes_stub_exit=0, tmpdir=tmp)
    assert r.returncode == 0, r.stderr
    sent = (tmp / "hermes_calls.log").read_text()
    # v2.2: plain "Reply CANCEL" only — no per-id `cancel <code>` targeting
    # (one printer, one thing to cancel) and no request-id in the message.
    assert "or reply CANCEL" in sent
    assert "Tap 🛑 CANCEL below" in sent
    code = env["U1_REQUEST_ID"][-6:]
    assert f"cancel {code}" not in sent
    assert env["U1_REQUEST_ID"] not in sent


# ─── Urgency punctuation: "CANCEL!!!" must fire; extra words must not ───────

def test_handler_trailing_punctuation_still_cancels(sandbox_pending_dir):
    # Trailing punctuation is urgency, not ambiguity — the panicking
    # operator hammering "CANCEL!!!" is exactly who this hook exists for.
    handler, pending, tmp = sandbox_pending_dir
    marker = tmp / "marker_urgent.txt"
    _seed_pending(pending, "u1_2026_0701_urgnt1", marker)
    _run(handler, "CANCEL!!!")
    assert marker.exists()


def test_handler_scoped_cancel_with_trailing_punctuation(sandbox_pending_dir):
    handler, pending, tmp = sandbox_pending_dir
    marker = tmp / "marker_urgent2.txt"
    _seed_pending(pending, "u1_2026_0701_abc123", marker)
    _run(handler, "cancel abc123!")
    assert marker.exists()


def test_handler_extra_words_still_do_not_cancel(sandbox_pending_dir):
    # The punctuation tolerance must NOT loosen the word rule.
    handler, pending, tmp = sandbox_pending_dir
    marker = tmp / "marker_words.txt"
    _seed_pending(pending, "u1_2026_0701_abc123", marker)
    _run(handler, "cancel please!")
    _run(handler, "please cancel")
    _run(handler, "Cancel that plan!!!")
    assert not marker.exists()
