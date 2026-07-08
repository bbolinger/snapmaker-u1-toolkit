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



def test_old_watchdog_leaves_re_armed_window_alone(tmp_path, monkeypatch):
    """Cold-audit finding: A armed -> redeemed -> same request re-armed as B
    at the same path. A's watchdog must NOT delete B's window."""
    import json as _json
    marker = tmp_path / "u1_x.json"
    marker.write_text(_json.dumps({"generation": "B-newer"}))
    sent = []
    monkeypatch.setattr(wd.subprocess, "run",
                        lambda cmd, timeout=0: sent.append(cmd))
    # watchdog A fires with A's generation; the marker now belongs to B
    outcome = wd.run("u1_x", "grip.gcode", str(marker), 0, "A-older",
                     sleep=lambda s: None)
    assert outcome == "silent"
    assert marker.exists()                     # B's window preserved
    assert sent == []                          # no false expiry DM


def test_matching_generation_still_expires(tmp_path, monkeypatch):
    import json as _json
    marker = tmp_path / "u1_x.json"
    marker.write_text(_json.dumps({"generation": "same"}))
    sent = []
    monkeypatch.setattr(wd.subprocess, "run",
                        lambda cmd, timeout=0: sent.append(cmd))
    outcome = wd.run("u1_x", "grip.gcode", str(marker), 0, "same",
                     sleep=lambda s: None)
    assert outcome == "notified" and not marker.exists() and sent


def test_empty_generation_is_backward_compatible(tmp_path, monkeypatch):
    """A marker without a generation (or a watchdog armed before this fix)
    still expires — the guard only bites when a generation is supplied AND
    differs."""
    marker = tmp_path / "u1_x.json"; marker.write_text("{}")
    monkeypatch.setattr(wd.subprocess, "run", lambda *a, **k: None)
    assert wd.run("u1_x", "g.gcode", str(marker), 0, "",
                  sleep=lambda s: None) == "notified"
