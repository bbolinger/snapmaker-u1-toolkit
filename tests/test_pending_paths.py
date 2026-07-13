"""Every pending-marker consumer must resolve the SAME directory.

The confirm/cancel/attach markers cross process boundaries (workflow <->
gateway hooks <-> Telegram button <-> notify script). The resolution rule
lives in scripts/u1_pending.py but is necessarily duplicated in files that
deploy standalone where imports can't reach. A copy drifting means markers
written on one side are invisible on the other — for cancel, that is a
dead CANCEL button (the 2026-07-09 incident class). This test imports
every copy and asserts identical resolution across the whole env matrix,
so drift fails CI instead of dropping a cancel tap.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent

import u1_pending  # scripts/ is on the path via conftest  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The canonical resolver + every standalone copy, loaded fresh.
def _resolvers():
    plugin_pending = _load(
        "u1_test_plugin_pending",
        _ROOT / "plugin" / "src" / "snapmaker_u1" / "pending.py")
    confirm_hook = _load(
        "u1_test_confirm_hook",
        _ROOT / "tools" / "hermes_hooks" / "u1_confirm_start" / "handler.py")
    cancel_hook = _load(
        "u1_test_cancel_hook",
        _ROOT / "tools" / "hermes_hooks" / "u1_grace_cancel" / "handler.py")
    sys.path.insert(0, str(_ROOT / "adapters" / "hermes" / "plugin"))
    try:
        import telegram_patch
        importlib.reload(telegram_patch)
    finally:
        sys.path.pop(0)
    return {
        "scripts/u1_pending.py": u1_pending.pending_dir,
        "plugin snapmaker_u1.pending": plugin_pending.pending_dir,
        "confirm hook": confirm_hook._pending_dir,
        "cancel hook": cancel_hook._pending_dir,
        "telegram_patch": telegram_patch._u1_pending_dir,
    }


_ENV_CASES = [
    # (env overrides, description)
    ({}, "default tempdir root"),
    ({"U1_PENDING_STATE_DIR": "/somewhere/state"}, "STATE root"),
    ({"U1_PENDING_CANCEL_DIR": "/legacy/cancel"}, "legacy per-kind override"),
    ({"U1_PENDING_STATE_DIR": "/somewhere/state",
      "U1_PENDING_CANCEL_DIR": "/legacy/cancel"},
     "legacy wins over STATE root"),
]


@pytest.mark.parametrize("env,desc", _ENV_CASES, ids=[c[1] for c in _ENV_CASES])
@pytest.mark.parametrize("kind", ["confirm", "cancel", "attach", "log"])
def test_all_copies_resolve_identically(monkeypatch, env, desc, kind):
    for var in ("U1_PENDING_STATE_DIR", "U1_PENDING_CONFIRM_DIR",
                "U1_PENDING_CANCEL_DIR", "U1_PENDING_ATTACH_DIR",
                "U1_PENDING_LOG_DIR"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    resolved = {name: fn(kind) for name, fn in _resolvers().items()}
    unique = set(resolved.values())
    assert len(unique) == 1, f"resolvers disagree for kind={kind} ({desc}): {resolved}"


def test_resolution_precedence(monkeypatch):
    # Clear the conftest sandbox's per-kind overrides first.
    for var in ("U1_PENDING_CONFIRM_DIR", "U1_PENDING_CANCEL_DIR",
                "U1_PENDING_ATTACH_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("U1_PENDING_STATE_DIR", "/root_x")
    monkeypatch.setenv("U1_PENDING_CANCEL_DIR", "/explicit_y")
    assert u1_pending.pending_dir("cancel") == Path("/explicit_y")
    assert u1_pending.pending_dir("confirm") == Path("/root_x/confirm")
    monkeypatch.delenv("U1_PENDING_CANCEL_DIR")
    assert u1_pending.pending_dir("cancel") == Path("/root_x/cancel")


def test_gate_exports_cancel_dir_to_notify_script(monkeypatch, tmp_path):
    # The notify script writes the routing entry the gate later polls; the
    # gate must hand it the RESOLVED dir explicitly so the bash fallback
    # never has to agree by coincidence.
    import u1_print_start_gate as g
    monkeypatch.setenv("U1_PENDING_CANCEL_DIR", str(tmp_path / "pc"))
    capture = tmp_path / "env_dump"
    res = g._run_grace_notify(
        f"env > {capture}", request_id="u1_2026_0101_aaaaaa",
        filename="f.gcode", grace_seconds=5,
        cancel_marker=tmp_path / "m", operator="op")
    assert res["ok"], res
    dumped = capture.read_text()
    assert f"U1_PENDING_CANCEL_DIR={tmp_path / 'pc'}" in dumped


def test_notify_script_fallback_matches_python_rule(tmp_path):
    # Manual-run fallback in u1_grace_notify_hermes.sh: extract just the
    # resolution block and evaluate it under the same env cases as the
    # Python rule. Guards the bash copy against silent drift.
    script = (_ROOT / "tools" / "u1_grace_notify_hermes.sh").read_text()
    start = script.index('if [[ -n "${U1_PENDING_CANCEL_DIR:-}" ]]')
    end = script.index("fi", start) + 2
    block = script[start:end] + '\necho "$PENDING_DIR"\n'

    def bash_resolve(env: dict) -> str:
        out = subprocess.run(
            ["bash", "-c", block], env={"PATH": "/usr/bin:/bin", **env},
            capture_output=True, text=True, check=True)
        return out.stdout.strip()

    assert bash_resolve({"U1_PENDING_CANCEL_DIR": "/x"}) == "/x"
    assert bash_resolve({"U1_PENDING_STATE_DIR": "/s"}) == "/s/cancel"
    assert bash_resolve({"TMPDIR": "/t"}) == "/t/u1_pending/cancel"
    assert bash_resolve({}) == "/tmp/u1_pending/cancel"
