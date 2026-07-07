"""Model-free YES boundary (post-incident 2026-07-07).

The agent model fired the emitted confirm command itself — no operator YES
ever happened — and the operator's Cancel arrived as a mid-turn interrupt,
which bypasses gateway hooks. Two structural changes under test:

  1. bed_clear_start events carry NO start command; the workflow arms a
     marker file that only the u1_confirm_start gateway hook redeems from
     the operator's literal YES message. The model has nothing to fire.
  2. `--grace-cancel` is a model-relayable SAFE-direction fallback: it can
     only ever stop a pending start, never begin one.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import u1_kit_workflow as kw

_HOOK_PATH = (Path(__file__).resolve().parent.parent
              / "tools" / "hermes_hooks" / "u1_confirm_start" / "handler.py")
_spec = importlib.util.spec_from_file_location("u1_confirm_start_handler", _HOOK_PATH)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


@pytest.fixture()
def pending_dir(tmp_path, monkeypatch):
    d = tmp_path / "pending_confirm"
    monkeypatch.setattr(hook, "PENDING_DIR", d)
    monkeypatch.setattr(kw, "_PENDING_CONFIRM_DIR", d)
    monkeypatch.setattr(hook, "LOG_FILE", tmp_path / "hook.log")
    return d


def _marker(pending_dir, rid="u1_2026_0707_aaa111", expired=False, **over):
    entry = {
        "request_id": rid,
        "confirm_cmd": ["python3", "/opt/data/scripts/u1_kit_workflow.py",
                        "--confirm-start", "tok_" + rid[-6:], "--json-events"],
        "log_path": None,
        "filename": "plate1.gcode",
        "expires_at": (datetime.now(timezone.utc)
                       + timedelta(minutes=-5 if expired else 10)).isoformat(),
    }
    entry.update(over)
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / f"{rid}.json").write_text(json.dumps(entry))
    return entry


def _run(context):
    asyncio.run(hook.handle("agent:start", context))


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
    _run({"message": "Yes!", "platform": "telegram", "user_id": "1"})
    assert spawned == [entry["confirm_cmd"]]
    assert not (pending_dir / f"{entry['request_id']}.json").exists()
    # a second YES finds nothing — no double spawn
    _run({"message": "yes", "platform": "telegram", "user_id": "1"})
    assert len(spawned) == 1


def test_bare_yes_with_two_windows_refuses(pending_dir, monkeypatch):
    _marker(pending_dir, rid="u1_2026_0707_aaa111")
    _marker(pending_dir, rid="u1_2026_0707_bbb222")
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd))
    _run({"message": "yes", "platform": "telegram", "user_id": "1"})
    assert spawned == []                      # START NEVER GUESSES
    assert len(list(pending_dir.glob("*.json"))) == 2
    # code-scoped yes picks exactly one
    _run({"message": "yes bbb222", "platform": "telegram", "user_id": "1"})
    assert len(spawned) == 1 and "tok_bbb222" in spawned[0]
    assert (pending_dir / "u1_2026_0707_aaa111.json").exists()


def test_expired_marker_is_dead(pending_dir, monkeypatch):
    _marker(pending_dir, expired=True)
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd))
    _run({"message": "yes", "platform": "telegram", "user_id": "1"})
    assert spawned == []


def test_prose_yes_never_touches_markers(pending_dir, monkeypatch):
    _marker(pending_dir)
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen",
                        lambda cmd, **kw_: spawned.append(cmd))
    _run({"message": "yes let's do the switch one", "platform": "telegram",
          "user_id": "1"})
    assert spawned == [] and len(list(pending_dir.glob("*.json"))) == 1


# ---------- workflow side ----------

def test_arm_and_disarm_pending_confirm(pending_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(kw.u1_request, "request_dir",
                        lambda rid: tmp_path / "req" / rid)
    kw._arm_pending_confirm("u1_2026_0707_ccc333", "tokccc", "p.gcode", "brent")
    m = json.loads((pending_dir / "u1_2026_0707_ccc333.json").read_text())
    assert m["confirm_cmd"][m["confirm_cmd"].index("--confirm-start") + 1] == "tokccc"
    assert "--operator" in m["confirm_cmd"]
    assert m["expires_at"] > m["created_at"]
    kw._disarm_pending_confirm("u1_2026_0707_ccc333")
    assert not (pending_dir / "u1_2026_0707_ccc333.json").exists()
    kw._disarm_pending_confirm("u1_2026_0707_ccc333")  # idempotent


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
