#!/usr/bin/env python3
"""
Install the u1 form flow into a Hermes deployment.

What this does (idempotent, re-run safe):

  1. Detects the Hermes ``site-packages`` (auto-finds ``gateway/`` and
     ``tools/`` under the given venv) and copies in:
       - form_gateway.py   (blocking primitive; mirrors clarify_gateway;
                            plus the session-keyed form-callback registry;
                            from ``adapters/hermes/tools/``)

  2. Deploys the **u1-form plugin** to ``<HERMES_HOME>/plugins/u1-form/``
     (``HERMES_HOME`` env, default ``~/.hermes``): plugin.yaml, __init__.py
     (registers the ``form`` tool + the pre_gateway_dispatch hook that
     patches the live Telegram adapter class), telegram_patch.py, and the
     vendored L1 renderer ``u1_form_telegram.py`` (single-sourced from the
     sibling ``adapters/telegram/`` directory at install time).

     The plugin is what makes the tool VISIBLE to platform agents:
     plugin-provided toolsets are auto-enabled per platform by
     ``hermes_cli.tools_config._get_platform_tools`` — the first-party
     path. (A tool dropped into ``tools/`` registers but is never offered:
     built-in toolsets resolve by subset-inference against the platform
     composite, which a runtime-registered toolset can never satisfy — and
     joining an existing toolset evicts it.)

  3. Enables the plugin (``hermes plugins enable u1-form`` — user plugins
     are opt-in by design; prints the manual command if the CLI call
     fails).

  4. Edits ``gateway/run.py`` ONCE to publish a per-turn form callback into
     ``tools.form_gateway`` keyed by ``agent.session_id`` — the same value
     Hermes' registry dispatch passes to tool handlers, which is the only
     bridge a registered tool has back to the gateway (generic dispatch
     carries no callback kwarg and no agent reference). Anchor-based,
     marker-guarded, backed up; re-runs replace the marked block when its
     body changed (upgrades) and no-op when identical.

  5. Verifies the REAL invariant in the venv: with a bare-composite config
     (``platform_toolsets.telegram: [hermes-telegram]``) both ``clarify``
     AND ``form`` must resolve from ``_get_platform_tools`` — i.e. form is
     offered and clarify was not harmed.

``--uninstall`` restores the run.py backup (skipped when a Hermes upgrade
already replaced run.py), removes the plugin dir and copied tool files
(including files from pre-plugin layouts), and disables the plugin.
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# gateway/run.py edit — the one source change, anchor-based and marked
# ---------------------------------------------------------------------------
RUN_PY_ANCHOR = "agent.clarify_callback = _clarify_callback_sync"
RUN_PY_MARKER = "# === u1 form patch ==="
RUN_PY_END_MARKER = "# === end u1 form patch ==="

RUN_PY_INSERT = '''
            # === u1 form patch ===
            # Mirrors _clarify_callback_sync above: agent calls the `form`
            # tool -> we render via the active adapter's send_form (the
            # live adapter class is patched by the u1-form plugin's
            # pre_gateway_dispatch hook) -> block on the form_gateway
            # primitive -> return the answer dict. Published into
            # form_gateway keyed by agent.session_id because registry
            # dispatch passes handlers session_id (and no callback kwarg) —
            # that lookup is how the plugin's form handler finds us.
            def _form_callback_sync(form_schema):
                import uuid as _uuid
                try:
                    from tools import form_gateway as _fmod
                except ImportError:
                    return {"_error": "form_gateway not installed"}
                if not _status_adapter or not hasattr(_status_adapter, "send_form"):
                    return {"_error": "active adapter has no send_form (plugin not loaded?)"}
                form_id = _uuid.uuid4().hex[:10]
                _fmod.register(form_id, session_key or "", form_schema)
                try:
                    _status_adapter.pause_typing_for_chat(_status_chat_id)
                except Exception:
                    pass
                fut = safe_schedule_threadsafe(
                    _status_adapter.send_form(
                        chat_id=_status_chat_id, form_schema=form_schema,
                        form_id=form_id, session_key=session_key or "",
                        metadata=_status_thread_metadata,
                    ),
                    _loop_for_step, logger=logger,
                    log_message="Form send failed to schedule",
                )
                if fut is None:
                    _fmod.clear_session(session_key or "")
                    return {"_error": "form prompt could not be scheduled"}
                try:
                    send_result = fut.result(timeout=15)
                    if not getattr(send_result, "success", False):
                        _fmod.clear_session(session_key or "")
                        return {"_error": "form prompt send failed"}
                except Exception as exc:
                    logger.warning("Form send failed: %s", exc)
                    _fmod.clear_session(session_key or "")
                    return {"_error": f"form send exception: {exc}"}
                response = _fmod.wait_for_response(
                    form_id, timeout=float(_fmod.get_form_timeout()))
                if response is None:
                    return {"_timeout": True}
                return response
            try:
                from tools import form_gateway as _fmod_pub
                _fmod_pub.set_form_callback(
                    getattr(agent, "session_id", "") or "", _form_callback_sync)
            except Exception:
                logger.warning("u1 form: could not publish form callback",
                               exc_info=True)
            agent.form_callback = _form_callback_sync
            # === end u1 form patch ===
'''

PLUGIN_KEY = "u1-form"
PLUGIN_FILES = ("plugin.yaml", "__init__.py", "telegram_patch.py")
# Files earlier v2.2 layouts copied into site-packages tools/ that the
# plugin architecture replaces. Removed on install AND uninstall so an
# upgraded deployment never double-registers the form tool.
LEGACY_TOOLS_FILES = ("form_tool.py", "u1_form_telegram.py")


def _site_packages(venv: Path) -> Path:
    """Find the site-packages under a venv — POSIX (lib/pythonX.Y/) or
    Windows (Lib/) layout (native Windows Hermes Desktop, install report
    2026-07-10)."""
    for sub in (venv / "lib").glob("python*"):
        sp = sub / "site-packages"
        if sp.is_dir():
            return sp
    win_sp = venv / "Lib" / "site-packages"
    if win_sp.is_dir():
        return win_sp
    raise SystemExit(
        f"no site-packages under {venv} (tried lib/pythonX.Y/ and Lib/)")


def _venv_python(venv: Path) -> Path | None:
    """The venv's interpreter — bin/python* (POSIX) or Scripts/python.exe
    (Windows)."""
    cand = next((venv / "bin").glob("python*"), None) if (venv / "bin").is_dir() else None
    if cand is not None:
        return cand
    win = venv / "Scripts" / "python.exe"
    return win if win.exists() else None


def _venv_hermes_bin(venv: Path) -> Path:
    """The venv's hermes entry point — bin/hermes or Scripts/hermes.exe."""
    posix = venv / "bin" / "hermes"
    if posix.exists():
        return posix
    win = venv / "Scripts" / "hermes.exe"
    return win if win.exists() else posix  # posix path keeps old error text


def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME", "").strip()
    return Path(env) if env else Path.home() / ".hermes"


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


def _copy_if_changed(src: Path, dst: Path) -> bool:
    if _sha256(src) == _sha256(dst):
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _strip_marked_block(txt: str):
    """Remove the marked u1 block from run.py text.

    Returns (stripped_text, removed_block_or_None). A begin marker without
    its end marker returns the text untouched with block None — the caller
    treats that as a malformed state and refuses to edit blind.
    """
    start = txt.find(RUN_PY_MARKER)
    if start == -1:
        return txt, None
    line_start = txt.rfind("\n", 0, start) + 1
    end_idx = txt.find(RUN_PY_END_MARKER, start)
    if end_idx == -1:
        return txt, None
    end_line = txt.find("\n", end_idx)
    end_line = len(txt) if end_line == -1 else end_line + 1
    return txt[:line_start] + txt[end_line:], txt[line_start:end_line]


def _render_block(anchor_indent: str) -> str:
    """RUN_PY_INSERT re-indented to the anchor line's actual indentation.

    The template is written at 12 spaces (0.17/0.18 layout); if a Hermes
    release re-nests the clarify wiring, the block follows it instead of
    breaking the file with a mismatched indent.
    """
    template_indent = "            "  # 12 spaces
    lines = []
    for line in RUN_PY_INSERT.strip("\n").splitlines():
        if line.startswith(template_indent):
            line = anchor_indent + line[len(template_indent):]
        lines.append(line)
    return "\n".join(lines) + "\n"


def _patch_run_py(run_py: Path, *, dry_run: bool) -> str:
    """Insert (or refresh) the form_callback wiring in gateway/run.py.

    The block is inserted as whole lines immediately above the anchor line,
    so strip-and-reinsert round-trips exactly: an unchanged body re-run is
    a no-op, a changed body (upgrade) replaces the old block in place.

    Returns: 'inserted' | 'updated' | 'already-applied' |
             'anchor-not-found' | 'malformed-block'.
    """
    txt = run_py.read_text()
    has_marker = RUN_PY_MARKER in txt
    stripped, old_block = _strip_marked_block(txt)
    if has_marker and old_block is None:
        return "malformed-block"  # begin marker without end — never edit blind
    idx = stripped.find(RUN_PY_ANCHOR)
    if idx == -1:
        return "anchor-not-found"
    line_start = stripped.rfind("\n", 0, idx) + 1
    anchor_indent = stripped[line_start:idx]
    if anchor_indent.strip():
        # The anchor text appears mid-line (not as its own statement) —
        # some future layout we don't understand. Don't edit blind.
        return "anchor-not-found"
    block = _render_block(anchor_indent)
    new = stripped[:line_start] + block + stripped[line_start:]
    if new == txt:
        return "already-applied"
    verb = "updated" if old_block else "inserted"
    if dry_run:
        return f"{verb} (dry-run)"
    backup = run_py.with_suffix(run_py.suffix + ".u1-bak")
    # The backup captures the CLEAN (marker-free) state of the CURRENT
    # Hermes version: on first install that's the file as found; on an
    # in-place body upgrade it's the file minus our old block. Either way
    # --uninstall restores a working, unpatched run.py for THIS Hermes.
    backup.write_text(stripped)
    shutil.copymode(run_py, backup)
    run_py.write_text(new)
    return verb


def _plugin_dir_dest() -> Path:
    return _hermes_home() / "plugins" / PLUGIN_KEY


def _enable_plugin(venv: Path, *, dry_run: bool) -> str:
    """Enable the plugin via the Hermes CLI (user plugins are opt-in)."""
    hermes_bin = _venv_hermes_bin(venv)
    cmd = [str(hermes_bin), "plugins", "enable", PLUGIN_KEY]
    if dry_run:
        return f"would run: {' '.join(cmd)}"
    if not hermes_bin.exists():
        return (f"SKIPPED — {hermes_bin} not found; run manually: "
                f"hermes plugins enable {PLUGIN_KEY}")
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=60)
    except Exception as exc:
        return (f"FAILED ({exc}); run manually: "
                f"hermes plugins enable {PLUGIN_KEY}")
    if proc.returncode != 0:
        return (f"FAILED (rc={proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}\n"
                f"  run manually: hermes plugins enable {PLUGIN_KEY}")
    return proc.stdout.strip() or "enabled"


def _disable_plugin(venv: Path, *, dry_run: bool) -> str:
    hermes_bin = _venv_hermes_bin(venv)
    cmd = [str(hermes_bin), "plugins", "disable", PLUGIN_KEY]
    if dry_run:
        return f"would run: {' '.join(cmd)}"
    if not hermes_bin.exists():
        return f"SKIPPED — {hermes_bin} not found"
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=60)
    except Exception as exc:
        return f"FAILED ({exc})"
    if proc.returncode != 0:
        return f"FAILED (rc={proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
    return proc.stdout.strip() or "disabled"


def _verify(venv_python: Path, tools_dir: Path) -> str:
    """Verify the REAL invariant, in the venv, against a bare-composite
    platform config: clarify must still resolve, and form must now resolve.
    This is exactly the check that caught the clarify-eviction regression.
    """
    # NB: this string is Python source for a subprocess. Avoid %-formatting
    # pitfalls (see the earlier %-in-% crash) — plain concatenation only.
    src = (
        "import sys; sys.path.insert(0, {tools_dir!r})\n"
        "from tools import form_gateway\n"
        "from hermes_cli.plugins import discover_plugins\n"
        "discover_plugins()\n"
        "from hermes_cli.tools_config import _get_platform_tools\n"
        "cfg = {{'platform_toolsets': {{'telegram': ['hermes-telegram']}}}}\n"
        "ts = _get_platform_tools(cfg, 'telegram')\n"
        "assert 'clarify' in ts, 'REGRESSION: clarify missing from ' + repr(sorted(ts))\n"
        "assert 'form' in ts, ('form toolset did not resolve — is the u1-form '\n"
        "                      'plugin enabled? (hermes plugins enable u1-form)')\n"
        "print('OK: clarify held, form resolves; toolsets=' + repr(sorted(ts)))\n"
    ).format(tools_dir=str(tools_dir))
    proc = subprocess.run([str(venv_python), "-c", src],
                          text=True, capture_output=True, timeout=60)
    if proc.returncode != 0:
        return f"FAIL\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    return proc.stdout.strip()


def _install_hook_plugin(venv_python: Path, plugin_pkg: Path, *,
                         dry_run: bool) -> str:
    """pip-install the ``snapmaker_u1`` entry-point plugin into the Hermes venv.

    This is a DIFFERENT plugin from the u1-form one deployed above: it carries
    the pip ``hermes_agent.plugins`` entry point that registers the auto-skill
    loader, the next-action directive, and the v2.4 transform_llm_output image /
    review-doc attach hook. Because it is an entry-point plugin, pip-installing
    it is all Hermes needs to discover and load it (no ``plugins enable`` step).
    Editable (-e) so a later ``git pull`` in the clone takes effect without a
    reinstall."""
    if not (plugin_pkg / "pyproject.toml").is_file():
        return f"SKIPPED: no plugin package at {plugin_pkg}"
    cmd = [str(venv_python), "-m", "pip", "install", "-e", str(plugin_pkg)]
    if dry_run:
        return "would run: " + " ".join(cmd)
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=300)
    except Exception as exc:  # noqa: BLE001 - surface any launch failure
        return f"FAILED to launch pip: {exc}"
    if proc.returncode != 0:
        return f"FAILED (rc={proc.returncode}): {proc.stderr.strip()[:400]}"
    return "installed (editable)"


def _verify_hook_plugin(venv_python: Path) -> str:
    """Confirm the snapmaker_u1 plugin loads in the venv and registers all three
    hooks -- in particular transform_llm_output, without which v2.4 image and
    review-doc delivery silently does nothing."""
    src = (
        "import logging; logging.disable(logging.CRITICAL)\n"
        "class C:\n"
        "    def __init__(s): s.h=[]\n"
        "    def register_hook(s,n,cb): s.h.append(n)\n"
        "    def register_skill(s,*a,**k): pass\n"
        "import importlib\n"
        "m=importlib.import_module('snapmaker_u1')\n"
        "c=C(); m.register(c)\n"
        "need={'pre_gateway_dispatch','transform_tool_result','transform_llm_output'}\n"
        "miss=need-set(c.h)\n"
        "print('OK: hooks='+','.join(c.h)) if not miss "
        "else print('MISSING hooks: '+','.join(sorted(miss)))\n"
    )
    try:
        proc = subprocess.run([str(venv_python), "-c", src], text=True,
                              capture_output=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return f"verify FAILED to launch: {exc}"
    return (proc.stdout or proc.stderr or
            f"no output (rc={proc.returncode})").strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Install the u1 form flow into Hermes.")
    ap.add_argument("--venv", type=Path, default=Path("/opt/hermes/.venv"),
                    help="Hermes virtualenv root (default: /opt/hermes/.venv)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would change without modifying anything.")
    ap.add_argument("--uninstall", action="store_true",
                    help="Remove the patch (restore gateway/run.py backup; "
                         "delete plugin dir + copied tool files; disable plugin).")
    a = ap.parse_args(argv)

    venv = a.venv.resolve()
    if not venv.is_dir():
        raise SystemExit(f"venv not found: {venv}")
    sp = _site_packages(venv)
    tools_dir = sp / "tools"
    gateway_dir = sp / "gateway"
    run_py = gateway_dir / "run.py"
    if not tools_dir.is_dir() or not run_py.exists():
        raise SystemExit(f"unexpected layout under {sp}: tools/ or gateway/run.py missing")
    venv_python = _venv_python(venv)
    if venv_python is None:
        raise SystemExit(
            f"no python under {venv} (tried bin/python* and Scripts/python.exe)")

    here = Path(__file__).resolve().parent
    plugin_src = here / "plugin"
    plugin_dst = _plugin_dir_dest()
    # The pip entry-point plugin (auto-skill loader + next-action guard + the
    # v2.4 attachment injector) lives at <repo>/plugin, two levels up from
    # adapters/hermes/.
    hook_plugin_src = here.parent.parent / "plugin"
    # u1_form_telegram.py (the L1 pure renderer) is single-sourced from the
    # sibling adapters/telegram/ directory — the hermes tree carries no copy.
    renderer_src = here.parent / "telegram" / "u1_form_telegram.py"
    tools_src = {"form_gateway.py": here / "tools" / "form_gateway.py",
                 "u1_kit_tool.py": here / "tools" / "u1_kit_tool.py"}

    print(f"venv:           {venv}")
    print(f"site-packages:  {sp}")
    print(f"python:         {venv_python}")
    print(f"plugin dest:    {plugin_dst}")
    print(f"action:         {'uninstall' if a.uninstall else 'install'}{' (dry-run)' if a.dry_run else ''}")
    print()

    if a.uninstall:
        for name in ("form_gateway.py", "u1_kit_tool.py") + LEGACY_TOOLS_FILES:
            tgt = tools_dir / name
            if tgt.exists():
                if a.dry_run:
                    print(f"would remove {tgt}")
                else:
                    tgt.unlink()
                    print(f"removed       {tgt}")
        if plugin_dst.is_dir():
            if a.dry_run:
                print(f"would remove {plugin_dst}")
            else:
                shutil.rmtree(plugin_dst)
                print(f"removed       {plugin_dst}")
        print(f"plugin disable: {_disable_plugin(venv, dry_run=a.dry_run)}")
        # Remove the pip entry-point plugin too (best-effort).
        _uni = [str(venv_python), "-m", "pip", "uninstall", "-y",
                "snapmaker-u1-toolkit-plugin"]
        if a.dry_run:
            print("would run: " + " ".join(_uni))
        else:
            try:
                _p = subprocess.run(_uni, text=True, capture_output=True, timeout=120)
                print(f"hook plugin pip uninstall: rc={_p.returncode}")
            except Exception as _exc:  # noqa: BLE001
                print(f"hook plugin pip uninstall failed to launch: {_exc}")
        bak = run_py.with_suffix(run_py.suffix + ".u1-bak")
        # Only restore the backup when OUR patch is actually present in the
        # current run.py. If Hermes was upgraded since install, pip replaced
        # run.py (marker gone) — restoring the old backup would DOWNGRADE
        # run.py to the pre-upgrade version. Leave it alone in that case.
        if RUN_PY_MARKER not in run_py.read_text():
            print(f"note: u1 marker not present in {run_py}; leaving run.py alone")
            print("      (Hermes was likely upgraded since install — the patch is already gone).")
            if bak.exists():
                print(f"note: stale backup left at {bak}; delete it manually if unwanted")
        elif bak.exists():
            if a.dry_run:
                print(f"would restore {run_py} from {bak}")
            else:
                shutil.copy2(bak, run_py)
                bak.unlink()
                print(f"restored      {run_py} from {bak} (backup removed)")
        else:
            print(f"note: marker present but no backup at {bak}; remove the "
                  f"marked block from run.py manually")
        return 0

    # Install.
    # Pre-flight (read-only): verify the run.py anchor BEFORE copying any
    # files. Hermes auto-imports tools/*, so copying first and then failing
    # the anchor check would leave a partial install — the form tool would
    # register while run.py stays unwired.
    run_txt = run_py.read_text()
    if RUN_PY_MARKER not in run_txt and RUN_PY_ANCHOR not in run_txt:
        print(f"ERROR: anchor {RUN_PY_ANCHOR!r} not found in gateway/run.py.")
        print("       Hermes may have changed the clarify wiring layout. Aborting")
        print("       before copying anything — no files were modified.")
        return 2

    print("[1/6] copy tools/ (form_gateway) + remove pre-plugin layout files")
    for name, src in tools_src.items():
        if not src.exists():
            raise SystemExit(f"source missing: {src}")
        tgt = tools_dir / name
        if a.dry_run:
            changed = _sha256(src) != _sha256(tgt)
            print(f"  {'would copy' if changed else 'unchanged '} {tgt}")
        else:
            changed = _copy_if_changed(src, tgt)
            print(f"  {'copied   ' if changed else 'unchanged '} {tgt}")
    for name in LEGACY_TOOLS_FILES:
        tgt = tools_dir / name
        if tgt.exists():
            if a.dry_run:
                print(f"  would remove {tgt} (superseded by the plugin)")
            else:
                tgt.unlink()
                print(f"  removed   {tgt} (superseded by the plugin)")

    print()
    print(f"[2/6] deploy plugin -> {plugin_dst}")
    plugin_files = {name: plugin_src / name for name in PLUGIN_FILES}
    plugin_files["u1_form_telegram.py"] = renderer_src
    for name, src in plugin_files.items():
        if not src.exists():
            raise SystemExit(f"source missing: {src}")
        tgt = plugin_dst / name
        if a.dry_run:
            changed = _sha256(src) != _sha256(tgt)
            print(f"  {'would copy' if changed else 'unchanged '} {tgt}")
        else:
            changed = _copy_if_changed(src, tgt)
            print(f"  {'copied   ' if changed else 'unchanged '} {tgt}")

    print()
    print("[3/6] enable plugin (user plugins are opt-in)")
    print(f"  {_enable_plugin(venv, dry_run=a.dry_run)}")

    print()
    print("[4/6] install the snapmaker_u1 hook plugin (pip entry point)")
    print(f"  {_install_hook_plugin(venv_python, hook_plugin_src, dry_run=a.dry_run)}")

    print()
    print("[5/6] patch gateway/run.py (anchor-based, marker-guarded)")
    status = _patch_run_py(run_py, dry_run=a.dry_run)
    print(f"  {status}: {run_py}")
    if status == "anchor-not-found":
        # Unreachable in practice (pre-flight above checks the same strings),
        # kept as a belt-and-braces guard against races.
        print()
        print(f"  ERROR: anchor {RUN_PY_ANCHOR!r} not found in gateway/run.py.")
        print("         Hermes may have changed the clarify wiring layout. Aborting.")
        return 2
    if status == "malformed-block":
        print()
        print("  ERROR: found the u1 begin marker without its end marker in run.py.")
        print("         Refusing to edit a half-present block — inspect run.py, then")
        print("         restore the .u1-bak backup or remove the block manually.")
        return 2

    if a.dry_run:
        print()
        print("dry-run complete — no files modified.")
        return 0

    print()
    print("[6/6] verify: toolset resolution + hook-plugin registration")
    print(" ", _verify(venv_python, tools_dir))
    print(" ", _verify_hook_plugin(venv_python))

    print()
    print("Done. Restart the Hermes gateway so both plugins load and the patched")
    print("gateway/run.py takes effect. First inbound Telegram message installs")
    print("send_form on the live adapter class (watch for the")
    print("'u1-form: TelegramAdapter.send_form installed' log line).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
