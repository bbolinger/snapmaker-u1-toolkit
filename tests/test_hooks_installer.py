"""Tests for tools/install_hermes_u1_hooks.sh (and the deprecated
install_hermes_cancel_hook.sh pointer that now runs it).

Every run shells out with HERMES_HOOKS_DIR pointed at a pytest tmp dir, and
HERMES_HOME / U1_CANCEL_HOOK_RECEIPT boxed into the same tmp tree so the
script can never touch the live /opt/data hooks, gateway log, or notify
receipt. No gateway is restarted anywhere here — the installer only prints
the restart step, and these tests assert exactly that.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "tools" / "install_hermes_u1_hooks.sh"
OLD_INSTALLER = REPO / "tools" / "install_hermes_cancel_hook.sh"
HOOK_SRC = REPO / "tools" / "hermes_hooks"
HOOKS = ("u1_grace_cancel", "u1_confirm_start")


def _run(tmp_path, *args, script=INSTALLER, hooks_dir=None, extra_env=None):
    """Run an installer script sandboxed into tmp_path. Returns the proc."""
    hooks_dir = tmp_path / "hooks" if hooks_dir is None else hooks_dir
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["HERMES_HOOKS_DIR"] = str(hooks_dir)
    env["HERMES_HOME"] = str(home)  # gateway.log lookup stays in the sandbox
    env["U1_CANCEL_HOOK_RECEIPT"] = str(home / ".u1_cancel_hook_receipt")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---- install ---------------------------------------------------------------

def test_install_places_both_hooks_and_receipts(tmp_path):
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for hook in HOOKS:
        dest = tmp_path / "hooks" / hook
        for fname in ("handler.py", "HOOK.yaml"):
            installed = dest / fname
            assert installed.is_file() and installed.stat().st_size > 0, (
                f"{hook}/{fname} missing after install")
            assert installed.read_bytes() == (HOOK_SRC / hook / fname).read_bytes()
        receipt = json.loads((dest / ".install_receipt.json").read_text())
        assert receipt["hook"] == hook
        assert receipt["installed_at"]
        assert receipt["source"] == str(HOOK_SRC / hook)
        assert receipt["toolkit_version"]
        if receipt["sha256"]["handler.py"] != "unavailable":
            assert receipt["sha256"]["handler.py"] == _sha256(dest / "handler.py")
            assert receipt["sha256"]["HOOK.yaml"] == _sha256(dest / "HOOK.yaml")


def test_install_prints_restart_step_instead_of_restarting(tmp_path):
    proc = _run(tmp_path)
    assert proc.returncode == 0
    assert "gateway restart" in proc.stdout
    assert "--verify" in proc.stdout
    # The next-step text also names what to grep for in the gateway log.
    assert "hook(s) loaded" in proc.stdout


def test_install_is_idempotent(tmp_path):
    first = _run(tmp_path)
    assert first.returncode == 0
    second = _run(tmp_path)
    assert second.returncode == 0, second.stdout + second.stderr
    for hook in HOOKS:
        receipt = json.loads(
            (tmp_path / "hooks" / hook / ".install_receipt.json").read_text())
        assert receipt["hook"] == hook
    verify = _run(tmp_path, "--verify")
    assert verify.returncode == 0, verify.stdout + verify.stderr


def test_install_fails_clearly_when_hooks_dir_unresolvable(tmp_path):
    """No HERMES_HOOKS_DIR + no Hermes python = exit 2 before any write."""
    hooks_dir = tmp_path / "hooks"
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    env.pop("HERMES_HOOKS_DIR", None)
    env["HERMES_PY"] = str(tmp_path / "no_such_python")
    env["HERMES_HOME"] = str(home)
    env["U1_CANCEL_HOOK_RECEIPT"] = str(home / ".u1_cancel_hook_receipt")
    proc = subprocess.run(
        ["bash", str(INSTALLER)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 2
    assert "HERMES_HOOKS_DIR" in proc.stderr
    assert not hooks_dir.exists()


def test_unknown_flag_is_usage_error(tmp_path):
    proc = _run(tmp_path, "--frobnicate")
    assert proc.returncode == 64
    assert "usage" in proc.stderr.lower()


# ---- verify ----------------------------------------------------------------

def test_verify_passes_after_install(tmp_path):
    assert _run(tmp_path).returncode == 0
    proc = _run(tmp_path, "--verify")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout
    # Sandbox has no gateway log — verify must say it checked files only.
    assert "file checks only" in proc.stdout


def test_verify_fails_on_fresh_dir_naming_both_hooks(tmp_path):
    (tmp_path / "hooks").mkdir()
    proc = _run(tmp_path, "--verify")
    assert proc.returncode != 0
    for hook in HOOKS:
        assert hook in proc.stderr, f"{hook} not named in verify failure"


def test_verify_fails_after_deleting_one_handler(tmp_path):
    assert _run(tmp_path).returncode == 0
    (tmp_path / "hooks" / "u1_confirm_start" / "handler.py").unlink()
    proc = _run(tmp_path, "--verify")
    assert proc.returncode != 0
    assert "u1_confirm_start" in proc.stderr
    assert "handler.py" in proc.stderr
    # The intact hook must not be blamed.
    assert "u1_grace_cancel" not in proc.stderr


def test_verify_fails_on_empty_hook_yaml(tmp_path):
    assert _run(tmp_path).returncode == 0
    (tmp_path / "hooks" / "u1_grace_cancel" / "HOOK.yaml").write_text("")
    proc = _run(tmp_path, "--verify")
    assert proc.returncode != 0
    assert "u1_grace_cancel" in proc.stderr
    assert "HOOK.yaml" in proc.stderr


def test_verify_fails_when_receipt_missing(tmp_path):
    assert _run(tmp_path).returncode == 0
    (tmp_path / "hooks" / "u1_confirm_start" / ".install_receipt.json").unlink()
    proc = _run(tmp_path, "--verify")
    assert proc.returncode != 0
    assert ".install_receipt.json" in proc.stderr
    assert "u1_confirm_start" in proc.stderr


# ---- deprecated pointer ----------------------------------------------------

def test_old_cancel_installer_points_to_and_runs_unified(tmp_path):
    proc = _run(tmp_path, script=OLD_INSTALLER)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "install_hermes_u1_hooks.sh" in proc.stdout
    # The pointer really ran the unified installer: both hooks landed.
    for hook in HOOKS:
        assert (tmp_path / "hooks" / hook / "handler.py").is_file()
        assert (tmp_path / "hooks" / hook / ".install_receipt.json").is_file()
