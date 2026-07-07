"""Model-free outcome notifications (built after the 2026-07-07 drills).

Two live failures drove this: a typed CANCEL lost to a mid-turn interrupt
(the print started while the agent narrated a cancellation it never saw),
and a hook-run confirm whose preflight refusal went to a log file nobody
reads (the agent narrated a start that never happened). Every start-path
outcome now reaches the operator from the machinery: countdown DM with an
inline cancel button, cancelled DM from the gate, refusal DM from the
hook-run confirm wrapper.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import u1_notify
import u1_kit_workflow as kw


# ---------- notifier ----------

def test_send_operator_builds_button_payload(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["payload"] = json.loads(req.data.decode())
        return io.BytesIO(json.dumps({"ok": True}).encode())

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setattr(u1_notify, "_chat_id", lambda: "8131922235")
    monkeypatch.setattr(u1_notify.urllib.request, "urlopen", fake_urlopen)
    ok = u1_notify.send_operator("countdown", cancel_button_request_id="u1_2026_0707_abc123")
    assert ok
    assert "bottok123/sendMessage" in captured["url"]
    kb = captured["payload"]["reply_markup"]["inline_keyboard"]
    assert kb[0][0]["callback_data"] == "u1c:u1_2026_0707_abc123"
    assert captured["payload"]["chat_id"] == "8131922235"


def test_send_operator_falls_back_to_hermes_send(monkeypatch):
    calls = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setattr(u1_notify, "_chat_id", lambda: "8131922235")
    monkeypatch.setattr(u1_notify.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(u1_notify.subprocess, "run",
                        lambda cmd, timeout=0: calls.append(cmd) or SimpleNamespace(returncode=0))
    assert u1_notify.send_operator("hello")
    import os
    assert calls and os.path.basename(calls[0][0]) == "hermes" and calls[0][1] == "send"


def test_send_operator_no_binding_uses_fallback(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setattr(u1_notify, "_chat_id", lambda: None)
    monkeypatch.setattr(u1_notify.subprocess, "run",
                        lambda cmd, timeout=0: SimpleNamespace(returncode=0))
    assert u1_notify.send_operator("hello")


# ---------- hook-run confirm outcome wrapper ----------

def _run_main_confirm_for(monkeypatch, result_phase, extra=None):
    sent = []
    fake = SimpleNamespace(send_operator=lambda text, **k: sent.append(text) or True)
    monkeypatch.setitem(sys.modules, "u1_notify", fake)
    res = {"phase": result_phase}
    res.update(extra or {})
    monkeypatch.setattr(kw, "run_kit_workflow", lambda a: res)
    kw.main(["--confirm-start-for", "u1_2026_0707_abc123", "--json-events"])
    return sent


def test_hook_confirm_refusal_sends_dm(monkeypatch):
    sent = _run_main_confirm_for(
        monkeypatch, "bed_clear_approval_rejected",
        {"reasons": ["printer-side file changed since upload"]})
    assert len(sent) == 1
    assert "NOT started" in sent[0]
    assert "printer-side file changed" in sent[0]


def test_hook_confirm_success_sends_nothing(monkeypatch):
    sent = _run_main_confirm_for(monkeypatch, "grace_in_progress")
    assert sent == []


def test_model_relay_confirm_never_dms(monkeypatch):
    """The legacy --confirm-start (token) path surfaces its own events in
    the chat — no out-of-band DM, no double-reporting."""
    sent = []
    fake = SimpleNamespace(send_operator=lambda text, **k: sent.append(text) or True)
    monkeypatch.setitem(sys.modules, "u1_notify", fake)
    monkeypatch.setattr(kw, "run_kit_workflow",
                        lambda a: {"phase": "bed_clear_confirm_token_invalid"})
    kw.main(["--confirm-start", "sometoken", "--json-events"])
    assert sent == []
