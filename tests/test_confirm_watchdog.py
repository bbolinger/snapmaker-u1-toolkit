"""Expiry watchdog unit tests — the production path the old string-format
version left entirely untested (cold-review finding 2026-07-07). Also
locks in that a hostile filename can NEVER become code: it is argv now, so
it is inert data."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import u1_confirm_watchdog as wd


def test_expired_window_notifies(tmp_path, monkeypatch):
    marker = tmp_path / "u1_x.json"
    marker.write_text("{}")
    sent = []
    monkeypatch.setattr(wd.subprocess, "run",
                        lambda cmd, timeout=0: sent.append(cmd))
    outcome = wd.run("u1_x", "grip.gcode", str(marker), 0, sleep=lambda s: None)
    assert outcome == "notified"
    assert not marker.exists()                    # window cleaned up
    assert sent and sent[0][:2] == ["python3", wd._NOTIFY_PY]
    assert "grip.gcode" in sent[0][2] and "expired" in sent[0][2]


def test_redeemed_window_is_silent(tmp_path, monkeypatch):
    """Marker already gone (redeemed or cancelled) → no DM."""
    sent = []
    monkeypatch.setattr(wd.subprocess, "run",
                        lambda cmd, timeout=0: sent.append(cmd))
    outcome = wd.run("u1_x", "grip.gcode",
                     str(tmp_path / "gone.json"), 0, sleep=lambda s: None)
    assert outcome == "silent" and sent == []


def test_hostile_filename_is_inert_data(tmp_path, monkeypatch):
    """The exact attack the old -c string enabled: a filename crafted to
    break out. As argv it is just a string in the DM body — never code."""
    marker = tmp_path / "u1_x.json"; marker.write_text("{}")
    sent = []
    monkeypatch.setattr(wd.subprocess, "run",
                        lambda cmd, timeout=0: sent.append(cmd))
    hostile = 'evil"); import os; os.system("touch /tmp/pwned"); ("'
    wd.run("u1_x", hostile, str(marker), 0, sleep=lambda s: None)
    assert not Path("/tmp/pwned").exists()        # nothing executed
    assert hostile in sent[0][2]                  # carried verbatim as text


def test_notify_failure_does_not_raise(tmp_path, monkeypatch):
    marker = tmp_path / "u1_x.json"; marker.write_text("{}")
    monkeypatch.setattr(wd.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert wd.run("u1_x", "g.gcode", str(marker), 0, sleep=lambda s: None) == "error"


def test_main_argv_smoke(tmp_path, monkeypatch):
    marker = tmp_path / "u1_x.json"; marker.write_text("{}")
    monkeypatch.setattr(wd.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(wd.time, "sleep", lambda s: None)
    assert wd.main(["u1_x", "g.gcode", str(marker), "0"]) == 0
    assert not marker.exists()
