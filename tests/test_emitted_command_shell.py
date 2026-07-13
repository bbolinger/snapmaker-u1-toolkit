"""Emitted workflow commands must EXECUTE in the agent's shell, not just
read well as strings.

Live 2026-07-11, first real Windows model hand-off: the slice workflow's
kit_detected.command embedded a backslash script path unquoted; Git Bash
ate the backslashes and Python got 'C:Usersbbolinger...' — dead before the
kit workflow began. Every string test in the suite was green while the one
thing the string exists for (being run) was broken. These tests parse the
emitted command and RUN it through bash — on Windows that's Git Bash, the
same shell the agent uses; on POSIX it's a plain bash and guards against
regressing the deployed runtime's commands.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

from u1_orient import write_binary_stl

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _cube_stl(path: Path, s: float = 12.0) -> None:
    v = np.array(
        [[0, 0, 0], [s, 0, 0], [s, s, 0], [0, s, 0],
         [0, 0, s], [s, 0, s], [s, s, s], [0, s, s]], dtype=np.float32)
    faces = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7),
             (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    tris = np.array([[v[a], v[b], v[c]] for a, b, c in faces], dtype=np.float32)
    write_binary_stl(path, tris, name="cube")


def _events(stdout: str) -> list[dict]:
    out = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def test_kit_handoff_command_runs_in_bash(tmp_path):
    # The filename deliberately contains spaces — the quoting stress case
    # from the live failure ("Hand tape cutter_25mm.stl").
    stl = tmp_path / "hand tape cutter test.stl"
    _cube_stl(stl)

    r1 = subprocess.run(
        [sys.executable, str(_SCRIPTS / "u1_slice_workflow.py"),
         str(stl), "--json-events"],
        capture_output=True, text=True, timeout=120, env=os.environ.copy())
    kit = next((e for e in _events(r1.stdout)
                if e.get("stage") == "kit_detected"), None)
    assert kit is not None, f"no kit_detected event; stderr: {r1.stderr[-500:]}"
    cmd = kit.get("command")
    assert cmd, "kit_detected must carry the hand-off command"

    # Run the emitted command EXACTLY as the agent's terminal would.
    r2 = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                        timeout=180, env=os.environ.copy())
    assert "can't open file" not in (r2.stderr or ""), (
        f"emitted command failed to reach the workflow:\n{cmd}\n{r2.stderr[-800:]}")
    stages = {e.get("stage") for e in _events(r2.stdout)}
    assert stages & {"kit_form", "need_input", "setup_required"}, (
        f"emitted command ran but produced no workflow stage; got {stages}; "
        f"stderr: {r2.stderr[-500:]}")


def test_emitted_next_command_runs_in_bash(tmp_path):
    # The kit workflow's own continuation commands (text mode) must survive
    # the shell round-trip too — they accumulate flags across turns.
    stl = tmp_path / "spaced part name.stl"
    _cube_stl(stl)
    env = os.environ.copy()
    env["U1_INTERACTION_MODE"] = "text"

    r1 = subprocess.run(
        [sys.executable, str(_SCRIPTS / "u1_kit_workflow.py"),
         str(stl), "--json-events"],
        capture_output=True, text=True, timeout=180, env=env)
    nexts = [e.get("next_command") for e in _events(r1.stdout)
             if e.get("next_command")]
    options = [o.get("next_command")
               for e in _events(r1.stdout)
               for o in (e.get("options") or [])
               if isinstance(o, dict) and o.get("next_command")]
    candidates = [c for c in (nexts + options) if c]
    if not candidates:
        import pytest
        pytest.skip("this flow emitted no next_command (form-only path)")
    cmd = candidates[0]
    r2 = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                        timeout=180, env=env)
    assert "can't open file" not in (r2.stderr or ""), (
        f"next_command failed the shell round-trip:\n{cmd}\n{r2.stderr[-800:]}")
    assert _events(r2.stdout), (
        f"next_command produced no events; stderr: {r2.stderr[-500:]}")
