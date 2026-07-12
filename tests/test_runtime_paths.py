"""Runtime-script path resolution: self-locating in scripts/, env chain
at the gateway boundary.

scripts/ consumers resolve siblings from their own file location
(u1_runtime_paths), so emitted next_command strings and spawned helpers
are correct wherever the scripts are deployed. Gateway-side files (the
confirm hook, the u1_kit tool) can't self-locate and use the chain
U1_RUNTIME_SCRIPTS_DIR > $HERMES_HOME/scripts (probed) > /opt/data/scripts.
The two copies of that chain are asserted here so they can't drift.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

import u1_runtime_paths  # scripts/ on path via conftest  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_confirm_hook():
    return _load("u1_rt_confirm_hook",
                 _ROOT / "tools" / "hermes_hooks" / "u1_confirm_start" / "handler.py")


def _load_kit_tool():
    """u1_kit_tool imports Hermes gateway modules at top level (and
    registers itself with tools.registry at the bottom); stub them all."""
    gw = types.ModuleType("gateway")
    sc = types.ModuleType("gateway.session_context")
    sc.get_session_env = lambda *a, **k: {}
    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = []  # mark as package so tools.registry resolves
    fg = types.ModuleType("tools.form_gateway")
    reg_mod = types.ModuleType("tools.registry")

    class _Reg:
        def register(self, **kw):
            pass

    reg_mod.registry = _Reg()
    tools_pkg.form_gateway = fg
    tools_pkg.registry = reg_mod
    stubs = {"gateway": gw, "gateway.session_context": sc,
             "tools": tools_pkg, "tools.form_gateway": fg,
             "tools.registry": reg_mod}
    saved = {k: sys.modules.get(k) for k in stubs}
    sys.modules.update(stubs)
    try:
        return _load("u1_rt_kit_tool",
                     _ROOT / "adapters" / "hermes" / "tools" / "u1_kit_tool.py")
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_script_path_self_locates(monkeypatch):
    monkeypatch.delenv("U1_RUNTIME_SCRIPTS_DIR", raising=False)
    p = Path(u1_runtime_paths.script_path("u1_notify.py"))
    assert p.parent == _ROOT / "scripts"
    assert p.name == "u1_notify.py"


def test_script_path_env_override(monkeypatch):
    monkeypatch.setenv("U1_RUNTIME_SCRIPTS_DIR", "/deployed/elsewhere")
    assert u1_runtime_paths.script_path("u1_kit_workflow.py") == str(
        Path("/deployed/elsewhere") / "u1_kit_workflow.py")


def test_gate_stage1_command_uses_sibling_gate(monkeypatch):
    monkeypatch.delenv("U1_RUNTIME_SCRIPTS_DIR", raising=False)
    import u1_print_start_gate as g
    cmd = g.build_stage1_command(
        printer_filename="p.gcode", intended_tool="extruder",
        material="PETG", request_id="u1_2026_0101_aaaaaa")
    # Emitted commands serialize paths with forward slashes on every
    # platform (Git Bash eats unquoted backslashes) — compare the shell
    # form, not str(Path) (caught on the 2026-07-12 Windows run).
    assert (_ROOT / "scripts" / "u1_print_start_gate.py").as_posix() in cmd
    assert "/opt/data/scripts" not in cmd or str(_ROOT).startswith("/opt/data")


def test_gateway_chain_env_override(monkeypatch):
    monkeypatch.setenv("U1_RUNTIME_SCRIPTS_DIR", "/x/scripts")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    hook = _load_confirm_hook()
    assert hook.WORKFLOW_PY == str(Path("/x/scripts") / "u1_kit_workflow.py")
    tool = _load_kit_tool()
    assert tool.DEFAULT_WORKFLOW_SCRIPT == str(
        Path("/x/scripts") / "u1_kit_workflow.py")


def test_gateway_chain_hermes_home_probed(monkeypatch, tmp_path):
    monkeypatch.delenv("U1_RUNTIME_SCRIPTS_DIR", raising=False)
    monkeypatch.delenv("U1_KIT_WORKFLOW", raising=False)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "u1_kit_workflow.py").write_text("# probe target\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    hook = _load_confirm_hook()
    assert hook.WORKFLOW_PY == str(scripts / "u1_kit_workflow.py")
    tool = _load_kit_tool()
    assert tool.DEFAULT_WORKFLOW_SCRIPT == str(scripts / "u1_kit_workflow.py")


def test_gateway_chain_unrelated_hermes_home_falls_back(monkeypatch, tmp_path):
    # HERMES_HOME set but no deployed scripts there -> the probe must NOT
    # hijack; fall back to the Linux deploy default. Compare as Path: on
    # native Windows the same fallback stringifies with backslashes
    # (caught by the 2026-07-10 Windows validation run).
    monkeypatch.delenv("U1_RUNTIME_SCRIPTS_DIR", raising=False)
    monkeypatch.delenv("U1_KIT_WORKFLOW", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    hook = _load_confirm_hook()
    assert Path(hook.WORKFLOW_PY) == Path("/opt/data/scripts/u1_kit_workflow.py")
    tool = _load_kit_tool()
    assert Path(tool.DEFAULT_WORKFLOW_SCRIPT) == Path(
        "/opt/data/scripts/u1_kit_workflow.py")


def test_gate_exports_notify_py_to_notify_script(monkeypatch, tmp_path):
    import u1_print_start_gate as g
    monkeypatch.delenv("U1_NOTIFY_PY", raising=False)
    capture = tmp_path / "env_dump"
    res = g._run_grace_notify(
        f"env > {capture}", request_id="u1_2026_0101_aaaaaa",
        filename="f.gcode", grace_seconds=5,
        cancel_marker=tmp_path / "m", operator="op")
    assert res["ok"], res
    dumped = capture.read_text()
    assert f"U1_NOTIFY_PY={_ROOT / 'scripts' / 'u1_notify.py'}" in dumped
