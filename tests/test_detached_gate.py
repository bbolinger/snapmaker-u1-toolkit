"""v2.2.1 #2: the detached Stage-2 gate resolves its outcome via the child's
EXPLICIT state marker, not by inferring 'still alive after 25s' == 'in grace'.
These exercise the REAL _invoke_stage2_gate (no mock) with tiny fake gate
scripts, so they must live in their own file away from the boundary suite's
autouse mock of _invoke_stage2_gate."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import u1_kit_workflow as kw  # noqa: E402


def _fake_gate(tmp_path, body):
    p = tmp_path / "fake_gate.py"
    p.write_text(body)
    return str(p)


def test_fast_exit_returns_result(tmp_path):
    """Child exits within the pre-grace wait (fast refusal): return its result."""
    gate = _fake_gate(tmp_path, "import sys\nsys.exit(2)\n")
    res = kw._invoke_stage2_gate(gate, [], tmp_path)
    assert res is not None
    assert getattr(res, "returncode", None) == 2
    assert not getattr(res, "stalled", False)


def test_grace_marker_returns_none(tmp_path):
    """Child writes a grace_started marker: the window genuinely opened -> None.
    v2.2.2 #4: the marker is run-scoped; the child reads its id from U1_GATE_RUN_ID
    (injected by the parent) and the parent polls that exact path."""
    gate = _fake_gate(tmp_path,
        "import json,sys,time,pathlib,os\n"
        "d=pathlib.Path(sys.argv[1])\n"
        "rid=os.environ.get('U1_GATE_RUN_ID','')\n"
        "(d/f'stage2_gate_state_{rid}.json').write_text(json.dumps({'state':'grace_started'}))\n"
        "time.sleep(2)\n")
    res = kw._invoke_stage2_gate(gate, [str(tmp_path)], tmp_path)
    assert res is None


def test_stall_returns_stalled(tmp_path, monkeypatch):
    """Child alive past the wait with NO grace marker (stalled): must NOT be
    reported as a healthy grace. Returns a stalled sentinel."""
    monkeypatch.setattr(kw, "_GATE_PREGRACE_WAIT", 1.0)
    gate = _fake_gate(tmp_path, "import time\ntime.sleep(3)\n")
    res = kw._invoke_stage2_gate(gate, [], tmp_path)
    assert getattr(res, "stalled", False) is True
    assert getattr(res, "returncode", "x") is None


def test_clears_stale_marker(tmp_path):
    """A stale grace_started marker from a PRIOR invocation must not be mistaken
    for this run's: the parent clears it before launch."""
    (tmp_path / "stage2_gate_state.json").write_text('{"state": "grace_started"}')
    gate = _fake_gate(tmp_path, "import sys\nsys.exit(5)\n")  # exits, writes no new marker
    res = kw._invoke_stage2_gate(gate, [], tmp_path)
    assert res is not None and getattr(res, "returncode", None) == 5
