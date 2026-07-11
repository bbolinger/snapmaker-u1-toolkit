"""The interpreter bootstrap must survive a poisoned PYTHONPATH.

Real failure (Windows Hermes Desktop, install report 2026-07-10): Hermes
injects its own 3.11 venv into PYTHONPATH; python3=3.13 then imports
Pillow's pure-Python shell from the wrong tree and dies in the compiled
_imaging extension. The interpreter itself was fine — only the inherited
env was broken. The bootstrap now retries ITSELF with PYTHONPATH cleared
before hunting other interpreters, and probes candidates sanitized too
(the same poison would break them identically).

These tests run the actual script under a deliberately poisoned
PYTHONPATH (a fake numpy that raises on import).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture
def poison_dir(tmp_path):
    """A site-dir whose numpy raises on import — shadowing the real one."""
    d = tmp_path / "poison"
    (d / "numpy").mkdir(parents=True)
    (d / "numpy" / "__init__.py").write_text(
        "raise ImportError('poisoned numpy from a foreign PYTHONPATH')\n")
    return d


def _run_workflow(extra_env: dict, timeout=60) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("U1_BOOTSTRAP_REEXEC", None)
    env.pop("U1_KEEP_PYTHONPATH", None)
    env.pop("U1_TOOLKIT_PYTHON", None)
    env.setdefault("SNAPMAKER_U1_HOST", "192.0.2.1")
    env.setdefault("SNAPMAKER_U1_PORT", "7125")
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(_SCRIPTS / "u1_slice_workflow.py"), "--help"],
        env=env, capture_output=True, text=True, timeout=timeout)


def test_poisoned_pythonpath_self_heals(poison_dir):
    # Requires the dev interpreter to genuinely have numpy+PIL (it does —
    # the suite imports them). Poison makes the fast path fail; the
    # sanitized self-retry must recover and the workflow must complete.
    r = _run_workflow({"PYTHONPATH": str(poison_dir)})
    assert r.returncode == 0, r.stderr
    assert "retrying with it cleared" in r.stderr
    assert "usage" in r.stdout.lower()


def test_keep_pythonpath_escape_hatch_respected(poison_dir):
    # With the escape hatch set, the bootstrap must NOT clear the poison —
    # and with no candidate interpreter available it fails with the
    # actionable error instead of silently overriding the operator.
    r = _run_workflow({"PYTHONPATH": str(poison_dir),
                       "U1_KEEP_PYTHONPATH": "1"})
    assert r.returncode == 2, (r.returncode, r.stderr[-500:])
    assert "retrying with it cleared" not in r.stderr
    assert "numpy" in r.stderr


def test_reexec_loop_guard_prevents_infinite_retry(poison_dir):
    # If a re-exec'd child STILL can't import (env poison wasn't the
    # cause), it must not self-retry forever — it falls through to the
    # candidate hunt and then the clear error.
    r = _run_workflow({"PYTHONPATH": str(poison_dir),
                       "U1_BOOTSTRAP_REEXEC": "1"})
    assert r.returncode == 2
    assert "retrying with it cleared" not in r.stderr
    assert "PYTHONPATH is set" in r.stderr, "diagnosis note must name the poison"
