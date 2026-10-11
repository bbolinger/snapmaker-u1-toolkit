"""Regression tests against the REAL hermes-agent package.

These exist because the form-tool visibility bug class is invisible to
hermetic tests: whether a registered tool is ever OFFERED to a platform
agent is decided by ``hermes_cli.tools_config._get_platform_tools`` against
live plugin/config state. Three shipped attempts in a row were wrong about
that machinery (wrong module, wrong return shape, an approach that evicted
``clarify`` on bare-composite configs) — each one green on 600+ hermetic
tests. The invariant has to be proven against the real code.

Gated on ``U1_HERMES_AGENT_SRC`` — a path whose directory is importable as
the hermes-agent package tree (an unzipped wheel, a checkout, or a venv's
``site-packages``). Unset → the whole module skips (CI has no Hermes).

To run locally:
    pip download hermes-agent --no-deps -d /tmp/hp
    unzip -q /tmp/hp/hermes_agent-*.whl -d /tmp/hermes-src
    U1_HERMES_AGENT_SRC=/tmp/hermes-src python3 -m pytest tests/test_hermes_real_package.py -v

Each scenario runs in a SUBPROCESS: Hermes' plugin manager is a process
singleton and ``discover_plugins`` caches, so in-process scenarios would
contaminate each other.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERMES_SRC = os.environ.get("U1_HERMES_AGENT_SRC", "").strip()

pytestmark = pytest.mark.skipif(
    not HERMES_SRC or not Path(HERMES_SRC).is_dir(),
    reason="U1_HERMES_AGENT_SRC not set (real hermes-agent tree required)",
)

_REPO = Path(__file__).resolve().parent.parent
_PLUGIN_SRC = _REPO / "adapters" / "hermes" / "plugin"
_RENDERER_SRC = _REPO / "adapters" / "telegram" / "u1_form_telegram.py"

# Brent's production shape: a bare composite, no explicit toolset list —
# the config class where subset-inference runs and where joining an
# existing toolset evicts it.
_BARE_COMPOSITE_CFG = {"platform_toolsets": {"telegram": ["hermes-telegram"]}}


def _run_probe(hermes_home: Path, body: str) -> dict:
    """Run probe code in a subprocess against the real package; return JSON."""
    src = (
        "import sys, json\n"
        f"sys.path.insert(0, {HERMES_SRC!r})\n"
        + body
        + "\nprint('U1PROBE:' + json.dumps(out))\n"
    )
    env = dict(os.environ)
    env["HERMES_HOME"] = str(hermes_home)
    proc = subprocess.run([sys.executable, "-c", src],
                          text=True, capture_output=True, timeout=120, env=env)
    assert proc.returncode == 0, f"probe failed:\n{proc.stdout}\n{proc.stderr}"
    line = next(l for l in proc.stdout.splitlines() if l.startswith("U1PROBE:"))
    return json.loads(line[len("U1PROBE:"):])


_TOOLSET_PROBE = f"""
from hermes_cli.plugins import discover_plugins
discover_plugins()
from hermes_cli.tools_config import _get_platform_tools
cfg = {_BARE_COMPOSITE_CFG!r}
ts = _get_platform_tools(cfg, 'telegram')
out = {{'toolsets': sorted(ts)}}
"""


def _deploy_plugin(hermes_home: Path) -> None:
    pdir = hermes_home / "plugins" / "u1-form"
    pdir.mkdir(parents=True)
    for name in ("plugin.yaml", "__init__.py", "telegram_patch.py"):
        shutil.copy(_PLUGIN_SRC / name, pdir / name)
    shutil.copy(_RENDERER_SRC, pdir / "u1_form_telegram.py")
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - u1-form\n")


def test_baseline_bare_composite_resolves_clarify_not_form(tmp_path):
    """Sanity + bug reproduction: without the plugin, clarify resolves and
    form does not — the state the runtime box was stuck in."""
    home = tmp_path / "home-baseline"
    home.mkdir()
    out = _run_probe(home, _TOOLSET_PROBE)
    assert "clarify" in out["toolsets"]
    assert "form" not in out["toolsets"]


def test_plugin_adds_form_without_evicting_anything(tmp_path):
    """THE invariant: with the u1-form plugin enabled, the resolved toolset
    set is a strict superset of baseline plus 'form'. This is the test that
    would have caught the clarify-eviction regression (toolset=clarify made
    resolve_toolset('clarify') ⊄ hermes-telegram → clarify itself dropped)."""
    baseline_home = tmp_path / "home-baseline"
    baseline_home.mkdir()
    baseline = set(_run_probe(baseline_home, _TOOLSET_PROBE)["toolsets"])

    plugin_home = tmp_path / "home-plugin"
    _deploy_plugin(plugin_home)
    with_plugin = set(_run_probe(plugin_home, _TOOLSET_PROBE)["toolsets"])

    assert "form" in with_plugin
    assert "clarify" in with_plugin
    missing = baseline - with_plugin
    assert not missing, f"plugin evicted toolsets: {sorted(missing)}"
    # u1_kit joins form when the tree carries tools/u1_kit_tool.py, which
    # install.py copies in; a bare Hermes tree yields form alone.
    added = with_plugin - baseline
    assert "form" in added and added <= {"form", "u1_kit"}, sorted(added)


def test_form_schema_reaches_get_tool_definitions(tmp_path):
    """End of the delivery chain: the model-facing tool list contains both
    form and clarify when both toolsets are enabled."""
    home = tmp_path / "home-defs"
    _deploy_plugin(home)
    out = _run_probe(home, """
from hermes_cli.plugins import discover_plugins
discover_plugins()
import inspect
from model_tools import get_tool_definitions
sets = ['clarify', 'form']
defs = get_tool_definitions(enabled_toolsets=sets, quiet_mode=True)
out = {'tools': sorted(d['function']['name'] for d in defs), 'full': None}
if 'skip_tool_search_assembly' in inspect.signature(get_tool_definitions).parameters:
    full = get_tool_definitions(enabled_toolsets=sets, quiet_mode=True,
                                skip_tool_search_assembly=True)
    out['full'] = sorted(d['function']['name'] for d in full)
""")
    assert "clarify" in out["tools"]
    if "form" not in out["tools"]:
        # Hermes 0.21.6+ offers plugin tools through tool_search instead of
        # listing them directly; the model reaches form from there.
        assert "tool_search" in out["tools"], out
        assert out["full"] and "form" in out["full"], out


def test_plugin_loads_clean_under_real_plugin_manager(tmp_path):
    """The plugin must load with no error and register exactly the expected
    surface (tool: form; hook: pre_gateway_dispatch)."""
    home = tmp_path / "home-load"
    _deploy_plugin(home)
    out = _run_probe(home, """
from hermes_cli.plugins import discover_plugins, get_plugin_manager
discover_plugins()
lp = get_plugin_manager()._plugins.get('u1-form')
out = {
    'found': lp is not None,
    'enabled': bool(lp and lp.enabled),
    'error': getattr(lp, 'error', None),
    'tools': list(getattr(lp, 'tools_registered', [])),
    'hooks': list(getattr(lp, 'hooks_registered', [])),
}
""")
    assert out["found"] and out["enabled"], out
    assert out["error"] is None, out
    # u1_kit registers too when the tree carries tools/u1_kit_tool.py.
    assert "form" in out["tools"] and set(out["tools"]) <= {"form", "u1_kit"}, out
    assert out["hooks"] == ["pre_gateway_dispatch"]


def _load_installer():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "u1_hermes_install_real_hook", _REPO / "adapters" / "hermes" / "install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _deploy_hook_plugin(hermes_home: Path) -> Path:
    """Deploy the snapmaker_u1 hook plugin with install.py's own helpers
    (3.2.0+: a folder plugin, no pip) and enable it."""
    inst = _load_installer()
    hdir = hermes_home / "plugins" / "snapmaker_u1"
    inst._deploy_hook_plugin(inst._hook_plugin_files(_REPO / "plugin"), hdir,
                             dry_run=False)
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - snapmaker_u1\n")
    return hdir


def test_hook_plugin_folder_loads_under_real_plugin_manager(tmp_path):
    """Hermes's own discovery must load the folder deploy as a user plugin,
    enabled, with no error, registering all three hooks. Before 3.2.0 this
    plugin was a pip entry point; Hermes 0.21.6+ runs from a package-manager
    environment that drops undeclared pip installs on every rebuild."""
    home = tmp_path / "home-hook"
    hdir = _deploy_hook_plugin(home)
    out = _run_probe(home, """
from hermes_cli.plugins import discover_plugins, get_plugin_manager
discover_plugins()
lp = get_plugin_manager()._plugins.get('snapmaker_u1')
out = {
    'found': lp is not None,
    'enabled': bool(lp and lp.enabled),
    'error': getattr(lp, 'error', None),
    'hooks': sorted(getattr(lp, 'hooks_registered', [])),
    'path': str(getattr(getattr(lp, 'manifest', None), 'path', '')),
}
""")
    assert out["found"] and out["enabled"], out
    assert out["error"] is None, out
    assert out["hooks"] == ["pre_gateway_dispatch", "transform_llm_output",
                            "transform_tool_result"]
    assert Path(out["path"]).resolve() == hdir.resolve()


def test_installer_hook_verify_against_real_plugin_manager(tmp_path, monkeypatch):
    """install.py's [7/7] hook check, run for real: the folder registers all
    three hooks and Hermes reports loading it from the deployed folder."""
    home = tmp_path / "home-verify"
    hdir = _deploy_hook_plugin(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("PYTHONPATH", HERMES_SRC)
    result = _load_installer()._verify_hook_plugin(Path(sys.executable), hdir)
    assert result.startswith("OK: hooks="), result
    assert f"Hermes loads it from {hdir}" in result and "(enabled)" in result, result


def test_followup_hooks_exist_in_real_hermes(tmp_path):
    """The bed-clear question is posted from a wrapper around the adapter's
    on_processing_complete, in the reply's thread, flagged notify. If Hermes
    renames any of those, this fails instead of the question silently falling
    back into the card."""
    home = tmp_path / "home-followup"
    home.mkdir()
    out = _run_probe(home, """
import inspect
from gateway.platforms import base
meta = base._mark_notify_metadata(None)
out = {
    'complete_is_async': inspect.iscoroutinefunction(
        base.BasePlatformAdapter.on_processing_complete),
    'send_params': list(inspect.signature(base.BasePlatformAdapter.send).parameters),
    'thread_helper': callable(getattr(base, '_thread_metadata_for_event', None)),
    'notify': meta.get('notify'),
}
""")
    assert out["complete_is_async"], out
    assert out["send_params"][:3] == ["self", "chat_id", "content"], out
    assert "metadata" in out["send_params"], out
    assert out["thread_helper"] and out["notify"] is True, out


def test_run_py_patcher_against_real_gateway_run_py(tmp_path):
    """The anchor exists in the real run.py, the patched file compiles, and
    strip-and-reinsert round-trips (idempotent re-runs, in-place upgrades)."""
    real_run_py = Path(HERMES_SRC) / "gateway" / "run.py"
    if not real_run_py.exists():
        pytest.skip("gateway/run.py not present in U1_HERMES_AGENT_SRC tree")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "u1_hermes_install_real", _REPO / "adapters" / "hermes" / "install.py")
    inst = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inst)

    work = tmp_path / "run.py"
    shutil.copy(real_run_py, work)
    pristine = work.read_text()
    if inst.RUN_PY_ANCHOR not in pristine:
        pytest.skip("no clarify-callback anchor in this Hermes build (0.21+): "
                    "install.py skips the optional run.py fallback and the "
                    "u1-form plugin publishes the form callback itself")

    assert inst._patch_run_py(work, dry_run=False) == "inserted"
    compile(work.read_text(), str(work), "exec")
    assert inst._patch_run_py(work, dry_run=False) == "already-applied"

    stripped, block = inst._strip_marked_block(work.read_text())
    assert stripped == pristine
    assert block and inst.RUN_PY_MARKER in block
