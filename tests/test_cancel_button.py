"""The 🛑 CANCEL button on the grace countdown DM.

Adapter-level callback (same plumbing as the form buttons) because a typed
CANCEL that lands mid-turn is injected into the agent conversation and never
reaches the dispatch hooks — lost live twice on 2026-07-07, one print
started against the operator's explicit cancel. A button callback rides
PTB's handler queue, which no agent turn can swallow.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_PATCH = (Path(__file__).resolve().parent.parent
          / "adapters" / "hermes" / "plugin" / "telegram_patch.py")
_spec = importlib.util.spec_from_file_location("u1_telegram_patch", _PATCH)
tp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tp)


class _Query:
    def __init__(self, data, text="countdown msg"):
        self.data = data
        self.message = SimpleNamespace(text=text)
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text):
        self.edits.append(text)


def _run(query, pending_dir, monkeypatch, legacy_dir=None):
    monkeypatch.setenv("U1_PENDING_CANCEL_DIR", str(pending_dir))
    # Sandbox the v2.4.1 legacy-dir shim away from the real /tmp; a test
    # passes legacy_dir explicitly to exercise the fallback.
    monkeypatch.setattr(tp, "_U1_LEGACY_CANCEL_DIR",
                        str(legacy_dir if legacy_dir is not None
                            else pending_dir / "no_legacy"))
    update = SimpleNamespace(callback_query=query)
    asyncio.run(tp._u1_handle_cancel_callback(None, update, None))


def _window(pending_dir, rid, marker, expired=False):
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / f"{rid}.json").write_text(json.dumps({
        "request_id": rid,
        "cancel_marker": str(marker),
        "filename": "plate1.gcode",
        "expires_at": (datetime.now(timezone.utc)
                       + timedelta(minutes=-5 if expired else 5)).isoformat(),
    }))


def test_pattern_matches_only_cancel_callbacks():
    pat = re.compile(tp.CANCEL_CB_PATTERN)
    assert pat.match("u1c:u1_2026_0707_abc123")
    assert not pat.match("u1c:evil/../path")
    assert not pat.match("s:1:2")          # form callback
    assert not pat.match("u1c:")
    assert not pat.match("cl:something")   # native hermes callback
    # and the form pattern can never eat a cancel callback
    assert not re.compile(tp.FORM_CB_PATTERN).match("u1c:u1_2026_0707_abc123")


def test_tap_touches_marker_and_confirms(tmp_path, monkeypatch):
    marker = tmp_path / "req" / "pre_start_cancel.marker"
    _window(tmp_path / "pending", "u1_2026_0707_abc123", marker)
    q = _Query("u1c:u1_2026_0707_abc123")
    _run(q, tmp_path / "pending", monkeypatch)
    assert marker.exists()
    assert marker.read_text() == "cancel via telegram button"
    assert q.answers and "Cancelling" in q.answers[0][0]
    assert q.edits and "CANCELLED" in q.edits[0]


def test_tap_with_no_window_alerts_and_touches_nothing(tmp_path, monkeypatch):
    q = _Query("u1c:u1_2026_0707_abc123")
    _run(q, tmp_path / "pending", monkeypatch)
    assert q.answers == [("No active grace window for this print (already "
                          "started, cancelled, or expired).", True)]
    assert q.edits == []


def test_tap_on_expired_window_refuses(tmp_path, monkeypatch):
    marker = tmp_path / "req" / "pre_start_cancel.marker"
    _window(tmp_path / "pending", "u1_2026_0707_abc123", marker, expired=True)
    q = _Query("u1c:u1_2026_0707_abc123")
    _run(q, tmp_path / "pending", monkeypatch)
    assert not marker.exists()
    assert q.answers[0][1] is True  # alert


def test_double_tap_second_is_calm(tmp_path, monkeypatch):
    marker = tmp_path / "req" / "pre_start_cancel.marker"
    _window(tmp_path / "pending", "u1_2026_0707_abc123", marker)
    q1 = _Query("u1c:u1_2026_0707_abc123")
    _run(q1, tmp_path / "pending", monkeypatch)
    # gate consumed the window (removes pending json) after the touch
    (tmp_path / "pending" / "u1_2026_0707_abc123.json").unlink()
    q2 = _Query("u1c:u1_2026_0707_abc123")
    _run(q2, tmp_path / "pending", monkeypatch)
    assert q2.answers[0][1] is True  # alert, no crash, no double effect


def test_tap_falls_back_to_legacy_dir(tmp_path, monkeypatch):
    """v2.4.1 upgrade shim: a routing entry written by a pre-v2.4.1 notify
    script (old literal location) must still be cancellable by the button."""
    marker = tmp_path / "marker.txt"
    legacy = tmp_path / "legacy_pending"
    _window(legacy, "u1_2026_0707_leg111", marker)
    q = _Query("u1c:u1_2026_0707_leg111")
    _run(q, tmp_path / "new_pending", monkeypatch, legacy_dir=legacy)
    assert marker.exists(), "legacy-dir window must still cancel"
