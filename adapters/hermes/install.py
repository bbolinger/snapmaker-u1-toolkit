#!/usr/bin/env python3
"""
Install the u1 form flow into a Hermes deployment.

What this does (idempotent, re-run safe):

  1. Finds the live Hermes ``tools/`` and ``gateway/`` packages by asking
     the venv interpreter where it imports them from (``site-packages`` for
     a PyPI install; the source checkout for an editable install, which
     moves on every upgrade), falling back to a ``site-packages`` scan, and
     copies in:
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

  3. Deploys the **snapmaker_u1 hook plugin** (auto-skill load, the
     next-action directive, and the image / review-doc attachment injector)
     as a folder plugin to ``<HERMES_HOME>/plugins/snapmaker_u1/``:
     ``plugin/plugin.yaml`` plus the ``plugin/src/snapmaker_u1/`` package.
     Hermes loads it straight from that folder, so nothing has to be
     installed into a Python environment. Hermes 0.21.6+ runs the gateway
     from a package-manager environment that ships without pip and is
     rebuilt whenever a plugin is installed or removed; a pip-installed copy
     would be dropped on every rebuild.

  4. Uninstalls the pip copy of that hook plugin earlier releases installed
     (``snapmaker-u1-toolkit-plugin``) from the venv and from any
     package-manager environment under ``<HERMES_HOME>/installs/``, with pip
     or, where an environment has no pip, uv.

  5. Enables both plugins (``hermes plugins enable u1-form`` and
     ``snapmaker_u1`` — user plugins are opt-in by design; prints the manual
     command if the CLI call fails).

  6. Edits ``gateway/run.py`` ONCE to publish a per-turn form callback into
     ``tools.form_gateway`` keyed by ``agent.session_id`` — the same value
     Hermes' registry dispatch passes to tool handlers, which is the only
     bridge a registered tool has back to the gateway (generic dispatch
     carries no callback kwarg and no agent reference). Anchor-based,
     marker-guarded, backed up; re-runs replace the marked block when its
     body changed (upgrades) and no-op when identical.

  7. Verifies the REAL invariant in the venv: with a bare-composite config
     (``platform_toolsets.telegram: [hermes-telegram]``) both ``clarify``
     AND ``form`` must resolve from ``_get_platform_tools`` — i.e. form is
     offered and clarify was not harmed. Then loads the deployed hook-plugin
     folder the way Hermes does and checks all three hooks register.

Run it as root or as the user Hermes runs as. As root, Hermes commands,
probes and package removals run as the owner of ``HERMES_HOME`` (or of the
environment being cleaned), and every file it copies takes the owner of the
folder it lands in, so a root run leaves nothing the gateway cannot write.

``--uninstall`` restores the run.py backup (skipped when a Hermes upgrade
already replaced run.py), removes both plugin dirs and the copied tool files
(including files from pre-plugin layouts), uninstalls any pip copy of the hook
plugin, and disables both plugins.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
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

# The snapmaker_u1 hook plugin: a folder plugin since 3.2.0. Releases before
# that pip-installed it as an entry point from this distribution.
HOOK_PLUGIN_KEY = "snapmaker_u1"
HOOK_PLUGIN_DIST = "snapmaker-u1-toolkit-plugin"
HOOK_PLUGIN_HOOKS = ("pre_gateway_dispatch", "transform_tool_result",
                     "transform_llm_output")
# Inside the deployed folder: the files this installer put there, so a later
# run removes only those (never a user's notes or backups).
HOOK_MANIFEST = ".u1-installed-files"


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


def _live_package_dirs(venv_python: Path | None) -> tuple[Path, Path] | None:
    """Where the venv's ``tools`` and ``gateway`` packages really live.

    Hermes from PyPI keeps them under site-packages. A source checkout or
    editable install (the only layout since the PyPI releases stopped) keeps
    them in the checkout, and that directory changes on every upgrade. A copy
    left in an old tree is never imported: the ``u1_kit`` tool silently drops
    out of the model's tool list and ``form`` answers "no gateway callback
    wired". Asking the interpreter is the one answer that is right for every
    layout. Runs from a neutral working directory so a ``tools/`` folder in
    the caller's cwd (HERMES_HOME has one) cannot pose as the package.
    Returns None when the interpreter cannot answer, leaving the
    site-packages scan as the fallback.
    """
    if venv_python is None or not venv_python.exists():
        return None
    src = (
        "import os, tools, gateway\n"
        "print(os.path.dirname(os.path.abspath(tools.__file__)))\n"
        "print(os.path.dirname(os.path.abspath(gateway.__file__)))\n"
    )
    try:
        proc = _run_probe([str(venv_python), "-c", src], timeout=60,
                          owner=_owner_for(_hermes_home()))
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if len(lines) != 2:
        return None
    tools_dir, gateway_dir = Path(lines[0]), Path(lines[1])
    if not tools_dir.is_dir() or not (gateway_dir / "run.py").exists():
        return None
    return tools_dir, gateway_dir


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


def _stat_owner(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_uid, st.st_gid


def _owner_for(path: Path) -> tuple[int, int] | None:
    """When the installer runs as root: the (uid, gid) that owns ``path`` (or
    its nearest existing parent), if that is not root. Hermes commands,
    probes and package removals run as that owner, so a root run never
    leaves root-owned files where the gateway's user has to write. Hermes
    0.21.6 crash-loops the gateway on root-owned package-manager state, and
    root-owned bytecode in a source tree breaks its later rebuilds. None
    when not root, on Windows, or when the path is root's anyway."""
    if os.name != "posix" or os.geteuid() != 0:
        return None
    probe = Path(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        owner = _stat_owner(probe)
    except OSError:
        return None
    return None if owner[0] == 0 else owner


def _as_owner(owner: tuple[int, int] | None) -> dict:
    """``subprocess.run`` keyword arguments that run a command as ``owner``."""
    if owner is None:
        return {}
    uid, gid = owner
    env = dict(os.environ)
    try:
        import pwd
        env["HOME"] = pwd.getpwuid(uid).pw_dir
    except (ImportError, KeyError):
        env["HOME"] = str(_hermes_home())
    return {"user": uid, "group": gid, "extra_groups": [], "env": env}


def _adopt_parent_owner(path: Path) -> None:
    """As root, give a file or folder this installer created the owner of the
    folder it sits in, so a root run never leaves root-owned files in a tree
    another user owns."""
    if os.name != "posix" or os.geteuid() != 0:
        return
    try:
        want = _stat_owner(path.parent)
        st = path.lstat()
    except OSError:
        return
    if (st.st_uid, st.st_gid) != want:
        os.chown(path, want[0], want[1], follow_symlinks=False)


def _run_probe(cmd: list[str], *, timeout: int,
               owner: tuple[int, int] | None = None):
    """Run an interpreter probe from a fresh private directory, as ``owner``
    when given (see _owner_for).

    ``python -c`` puts the working directory first on ``sys.path``. In a
    shared directory such as /tmp, anyone who can write there could plant a
    module (``tools/``, ``csv.py``) that the probe imports, possibly running
    as root. A new 0700 directory per call leaves nothing to import.
    """
    with tempfile.TemporaryDirectory(prefix="u1-install-") as cwd:
        if owner is not None:
            os.chown(cwd, owner[0], owner[1])
        return subprocess.run(cmd, text=True, capture_output=True,
                              timeout=timeout, cwd=cwd, **_as_owner(owner))


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


def _copy_if_changed(src: Path, dst: Path) -> bool:
    if _sha256(src) == _sha256(dst):
        return False
    missing = []
    parent = dst.parent
    while not parent.exists() and parent.parent != parent:
        missing.append(parent)
        parent = parent.parent
    dst.parent.mkdir(parents=True, exist_ok=True)
    for folder in reversed(missing):  # top-down: each takes its parent's owner
        _adopt_parent_owner(folder)
    shutil.copy2(src, dst)
    _adopt_parent_owner(dst)
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


def _enable_plugin(venv: Path, *, dry_run: bool, key: str = PLUGIN_KEY) -> str:
    """Enable a plugin via the Hermes CLI (user plugins are opt-in)."""
    hermes_bin = _venv_hermes_bin(venv)
    cmd = [str(hermes_bin), "plugins", "enable", key]
    if dry_run:
        return f"would run: {' '.join(cmd)}"
    if not hermes_bin.exists():
        return (f"SKIPPED — {hermes_bin} not found; run manually: "
                f"hermes plugins enable {key}")
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=60,
                              **_as_owner(_owner_for(_hermes_home())))
    except Exception as exc:
        return (f"FAILED ({exc}); run manually: "
                f"hermes plugins enable {key}")
    if proc.returncode != 0:
        return (f"FAILED (rc={proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}\n"
                f"  run manually: hermes plugins enable {key}")
    return proc.stdout.strip() or "enabled"


def _disable_plugin(venv: Path, *, dry_run: bool, key: str = PLUGIN_KEY) -> str:
    hermes_bin = _venv_hermes_bin(venv)
    cmd = [str(hermes_bin), "plugins", "disable", key]
    if dry_run:
        return f"would run: {' '.join(cmd)}"
    if not hermes_bin.exists():
        return f"SKIPPED — {hermes_bin} not found"
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=60,
                              **_as_owner(_owner_for(_hermes_home())))
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
    proc = _run_probe([str(venv_python), "-c", src], timeout=60,
                      owner=_owner_for(_hermes_home()))
    if proc.returncode != 0:
        return f"FAIL\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    return proc.stdout.strip()


def _hook_plugin_dir_dest() -> Path:
    return _hermes_home() / "plugins" / HOOK_PLUGIN_KEY


def _hook_plugin_files(plugin_pkg: Path) -> dict[str, Path] | None:
    """Relative path -> source file for the hook plugin's folder deploy:
    ``plugin.yaml`` plus everything in ``src/snapmaker_u1/`` (the package
    becomes the plugin folder), minus bytecode and build metadata. None when
    the checkout has no hook plugin package (a partial checkout)."""
    src_pkg = plugin_pkg / "src" / HOOK_PLUGIN_KEY
    manifest = plugin_pkg / "plugin.yaml"
    if not src_pkg.is_dir() or not manifest.is_file():
        return None
    files = {"plugin.yaml": manifest}
    for path in sorted(src_pkg.rglob("*")):
        rel = path.relative_to(src_pkg)
        if not path.is_file() or path.suffix in (".pyc", ".pyo"):
            continue
        if any(part == "__pycache__" or part.endswith(".egg-info")
               for part in rel.parts):
            continue
        files[rel.as_posix()] = path
    return files


def _make_readable(dst: Path, files: dict[str, Path]) -> None:
    """The gateway may run as a different user than the installer: make the
    deployed files and their folders readable to it whatever the umask or
    the checkout's own modes were."""
    for rel in files:
        path = dst / rel
        mode = path.stat().st_mode
        if mode & 0o444 != 0o444:
            path.chmod(mode | 0o444)
        parent = path.parent
        while True:
            pmode = parent.stat().st_mode
            if pmode & 0o555 != 0o555:
                parent.chmod(pmode | 0o555)
            if parent == dst:
                break
            parent = parent.parent


def _deploy_hook_plugin(files: dict[str, Path], dst: Path, *,
                        dry_run: bool) -> list[str]:
    """Mirror ``files`` into ``dst``: copy what changed and remove files an
    earlier run of this installer deployed (recorded in HOOK_MANIFEST) that
    this release no longer ships, so a module removed upstream cannot keep
    loading. Files the installer never put there are left alone."""
    lines = []
    for rel, src in files.items():
        if not src.exists():
            raise SystemExit(f"source missing: {src}")
        tgt = dst / rel
        if dry_run:
            changed = _sha256(src) != _sha256(tgt)
            lines.append(f"{'would copy' if changed else 'unchanged '} {tgt}")
        else:
            changed = _copy_if_changed(src, tgt)
            lines.append(f"{'copied   ' if changed else 'unchanged '} {tgt}")
    manifest = dst / HOOK_MANIFEST
    previous = []
    if manifest.is_file():
        previous = [ln.strip() for ln in
                    manifest.read_text(encoding="utf-8").splitlines() if ln.strip()]
    root = dst.resolve()
    for rel in previous:
        if rel in files:
            continue
        tgt = dst / rel
        try:
            tgt.resolve().relative_to(root)  # never follow a path out of the folder
        except (ValueError, OSError):
            continue
        if not tgt.is_file():
            continue
        if dry_run:
            lines.append(f"would remove {tgt} (no longer shipped)")
        else:
            tgt.unlink()
            lines.append(f"removed    {tgt} (no longer shipped)")
    if not dry_run:
        manifest.write_text("".join(f"{rel}\n" for rel in sorted(files)),
                            encoding="utf-8")
        _adopt_parent_owner(manifest)
        _make_readable(dst, files)
    return lines


def _managed_env_pythons(notes: list[str] | None = None) -> list[Path]:
    """Interpreters of the package-manager environments Hermes 0.21.6+ runs
    the gateway from (``installs/<key>/facts.json``). Hermes keeps that
    state in the root home even when HERMES_HOME names a profile
    (``<root>/profiles/<name>``), so both are searched. Unreadable state is
    reported in ``notes`` instead of being skipped silently."""
    home = _hermes_home()
    roots = [home]
    if home.parent.name == "profiles":
        roots.append(home.parent.parent)
    found = []
    for root in roots:
        for facts in sorted((root / "installs").glob("*/facts.json")):
            try:
                data = json.loads(facts.read_text(encoding="utf-8-sig"))
                env = data["packages"]["venv"]["environment"]
            except OSError as exc:
                if notes is not None:
                    notes.append(f"could not read {facts} ({exc.strerror or exc}); "
                                 f"run the installer as its owner to check that environment")
                continue
            except (ValueError, KeyError, TypeError):
                continue
            if not isinstance(env, str):
                continue
            py = _venv_python(Path(env))
            if py is not None and py.exists() and py not in found:
                found.append(py)
    return found


def _uv_executable() -> str | None:
    """uv on PATH, else the copy Hermes's package manager keeps under
    ``<HERMES_HOME>/tools/`` (its environments ship without pip)."""
    found = shutil.which("uv")
    if found:
        return found
    for name in ("uv", "uv.exe"):
        for cand in sorted((_hermes_home() / "tools").glob(f"uv-*/{name}"),
                           reverse=True):
            if cand.is_file():
                return str(cand)
    return None


def _env_dir(python: Path) -> Path:
    """The environment root of a venv interpreter (bin/ or Scripts/ parent)."""
    return Path(python).parent.parent


def _has_dist(python: Path, dist: str) -> bool | None:
    """True / False: ``dist`` is / is not installed for ``python``. None: the
    probe itself failed (launch error, timeout, crash), so the caller must not
    treat that environment as clean."""
    src = (
        "import importlib.metadata as m, sys\n"
        "try:\n"
        "    m.distribution(sys.argv[1])\n"
        "except m.PackageNotFoundError:\n"
        "    print('U1DIST:no')\n"
        "else:\n"
        "    print('U1DIST:yes')\n"
    )
    try:
        proc = _run_probe([str(python), "-c", src, dist], timeout=60,
                          owner=_owner_for(_env_dir(python)))
    except (OSError, subprocess.SubprocessError):
        return None
    words = (proc.stdout or "").split()
    if proc.returncode == 0 and "U1DIST:yes" in words:
        return True
    if proc.returncode == 0 and "U1DIST:no" in words:
        return False
    return None


def _remove_legacy_hook_dist(pythons: list[Path], *, dry_run: bool) -> list[str]:
    """Uninstall the pip copy of the hook plugin that releases before 3.2.0
    installed. Hermes 0.21.6+ prefers the folder copy when both exist, but a
    leftover loads again as soon as the folder is removed, and older Hermes
    may load both and register every hook twice."""
    lines = []
    seen = set()
    for py in pythons:
        key = str(py.absolute())
        if key in seen:
            continue
        seen.add(key)
        pip_cmd = [str(py), "-m", "pip", "uninstall", "-y", HOOK_PLUGIN_DIST]
        uv = _uv_executable()
        uv_cmd = [uv or "uv", "pip", "uninstall", "--python", str(py), HOOK_PLUGIN_DIST]
        manual = (f"run `{' '.join(pip_cmd)}` or, where the environment has no "
                  f"pip, `{' '.join(uv_cmd)}`")
        state = _has_dist(py, HOOK_PLUGIN_DIST)
        if state is None:
            lines.append(f"COULD NOT CHECK {py} for {HOOK_PLUGIN_DIST} (the probe "
                         f"failed); if a copy is installed there, {manual}")
            continue
        if not state:
            lines.append(f"not installed  {py}")
            continue
        if dry_run:
            lines.append(f"would uninstall {HOOK_PLUGIN_DIST} from {py}")
            continue
        attempts = [("pip", pip_cmd)] + ([("uv", uv_cmd)] if uv else [])
        done = False
        for label, cmd in attempts:
            try:
                proc = subprocess.run(cmd, text=True, capture_output=True,
                                      timeout=300,
                                      **_as_owner(_owner_for(_env_dir(py))))
            except (OSError, subprocess.SubprocessError):
                continue
            if proc.returncode == 0:
                lines.append(f"uninstalled ({label}) {HOOK_PLUGIN_DIST} from {py}")
                done = True
                break
        if not done:
            lines.append(f"COULD NOT uninstall {HOOK_PLUGIN_DIST} from {py}; {manual}")
    return lines


def _verify_hook_plugin(venv_python: Path, plugin_dir: Path) -> str:
    """Load the deployed folder the way Hermes loads a directory plugin (a
    package rooted at the folder, so the hooks' relative imports resolve) and
    confirm register() wires all three hooks, in particular
    transform_llm_output, without which image and review-doc delivery
    silently does nothing. Then ask Hermes's own plugin manager: it must load
    snapmaker_u1 from this folder, enabled and without a load error. Returns
    a line starting with ``OK:`` or ``FAIL:``."""
    src = (
        "import importlib.util, logging, os, sys\n"
        "logging.disable(logging.CRITICAL)\n"
        "d, need, key = sys.argv[1], set(sys.argv[2].split(',')), sys.argv[3]\n"
        "spec = importlib.util.spec_from_file_location(\n"
        "    'u1_verify_hook_plugin', os.path.join(d, '__init__.py'),\n"
        "    submodule_search_locations=[d])\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name] = m\n"
        "spec.loader.exec_module(m)\n"
        "class C:\n"
        "    def __init__(s): s.h = []\n"
        "    def register_hook(s, n, cb): s.h.append(n)\n"
        "    def register_skill(s, *a, **k): pass\n"
        "c = C(); m.register(c)\n"
        "miss = need - set(c.h)\n"
        "if miss:\n"
        "    print('FAIL: hooks missing from the folder: ' + ','.join(sorted(miss)))\n"
        "    sys.exit(1)\n"
        "msg = 'hooks=' + ','.join(c.h)\n"
        "try:\n"
        "    from hermes_cli.plugins import discover_plugins, get_plugin_manager\n"
        "except Exception as exc:\n"
        "    print('OK: ' + msg + '; plugin-manager check skipped (' + type(exc).__name__ + ')')\n"
        "    sys.exit(0)\n"
        "try:\n"
        "    discover_plugins()\n"
        "except Exception as exc:\n"
        "    print('FAIL: ' + msg + '; Hermes plugin discovery raised ' + type(exc).__name__ + ': ' + str(exc))\n"
        "    sys.exit(1)\n"
        "plugins = getattr(get_plugin_manager(), '_plugins', None)\n"
        "if not isinstance(plugins, dict):\n"
        "    print('OK: ' + msg + '; plugin-manager check skipped (unknown Hermes layout)')\n"
        "    sys.exit(0)\n"
        "lp = plugins.get(key)\n"
        "enable = ' (run: hermes plugins enable ' + key + ')'\n"
        "if lp is None:\n"
        "    print('FAIL: ' + msg + '; Hermes does not see ' + key + enable)\n"
        "    sys.exit(1)\n"
        "err = getattr(lp, 'error', None)\n"
        "if err:\n"
        "    print('FAIL: ' + msg + '; Hermes failed to load ' + key + ': ' + str(err))\n"
        "    sys.exit(1)\n"
        "if not getattr(lp, 'enabled', True):\n"
        "    print('FAIL: ' + msg + '; Hermes found ' + key + ' but it is not enabled' + enable)\n"
        "    sys.exit(1)\n"
        "path = getattr(getattr(lp, 'manifest', None), 'path', None)\n"
        "if not path or os.path.realpath(str(path)) != os.path.realpath(d):\n"
        "    print('FAIL: ' + msg + '; Hermes loads ' + key + ' from ' + str(path)\n"
        "          + ', not from ' + d + ' (an old pip copy may still be installed)')\n"
        "    sys.exit(1)\n"
        "print('OK: ' + msg + '; Hermes loads it from ' + d + ' (enabled)')\n"
    )
    try:
        proc = _run_probe(
            [str(venv_python), "-c", src, str(plugin_dir),
             ",".join(HOOK_PLUGIN_HOOKS), HOOK_PLUGIN_KEY],
            timeout=120, owner=_owner_for(_hermes_home()))
    except Exception as exc:  # noqa: BLE001
        return f"FAIL: verify could not launch: {exc}"
    out = (proc.stdout or "").strip()
    if out.startswith(("OK:", "FAIL:")):
        return out
    return f"FAIL: verify crashed (rc={proc.returncode}): {(proc.stderr or '').strip()[-400:]}"


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
    venv_python = _venv_python(venv)
    live = _live_package_dirs(venv_python)
    if live is not None:
        tools_dir, gateway_dir = live
        layout = "live packages, as imported by the venv interpreter"
    else:
        sp = _site_packages(venv)
        tools_dir = sp / "tools"
        gateway_dir = sp / "gateway"
        layout = f"site-packages scan under {sp}"
    run_py = gateway_dir / "run.py"
    if not tools_dir.is_dir() or not run_py.exists():
        raise SystemExit(f"unexpected layout ({layout}): {tools_dir} or {run_py} missing")
    if venv_python is None:
        raise SystemExit(
            f"no python under {venv} (tried bin/python* and Scripts/python.exe)")

    here = Path(__file__).resolve().parent
    plugin_src = here / "plugin"
    plugin_dst = _plugin_dir_dest()
    # The snapmaker_u1 hook plugin (auto-skill loader + next-action guard +
    # the v2.4 attachment injector) lives at <repo>/plugin, two levels up from
    # adapters/hermes/, and deploys as a folder plugin.
    hook_plugin_src = here.parent.parent / "plugin"
    hook_plugin_dst = _hook_plugin_dir_dest()
    hook_files = _hook_plugin_files(hook_plugin_src)
    # u1_form_telegram.py (the L1 pure renderer) is single-sourced from the
    # sibling adapters/telegram/ directory — the hermes tree carries no copy.
    renderer_src = here.parent / "telegram" / "u1_form_telegram.py"
    tools_src = {"form_gateway.py": here / "tools" / "form_gateway.py",
                 "u1_kit_tool.py": here / "tools" / "u1_kit_tool.py"}

    print(f"venv:           {venv}")
    print(f"tools dir:      {tools_dir}")
    print(f"gateway dir:    {gateway_dir}")
    print(f"layout:         {layout}")
    print(f"python:         {venv_python}")
    print(f"plugin dest:    {plugin_dst}")
    print(f"hook plugin:    {hook_plugin_dst}")
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
        # Disable while the folders still exist: Hermes's `plugins disable`
        # only knows plugins it can discover.
        for key in (PLUGIN_KEY, HOOK_PLUGIN_KEY):
            print(f"plugin disable {key}: "
                  f"{_disable_plugin(venv, dry_run=a.dry_run, key=key)}")
        for pdir in (plugin_dst, hook_plugin_dst):
            if pdir.is_dir():
                if a.dry_run:
                    print(f"would remove {pdir}")
                else:
                    shutil.rmtree(pdir)
                    print(f"removed       {pdir}")
        # A pip copy of the hook plugin from a release before 3.2.0, if any.
        notes: list[str] = []
        pythons = [venv_python] + _managed_env_pythons(notes)
        for line in notes + _remove_legacy_hook_dist(pythons, dry_run=a.dry_run):
            print(f"hook plugin pip copy: {line}")
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
    # Pre-flight (read-only): note whether the run.py FALLBACK anchor is
    # present. It no longer gates the install. The u1-form plugin publishes
    # the form callback itself from its pre_gateway_dispatch hook (the primary,
    # upgrade-durable path), so a missing anchor only means the belt-and-
    # suspenders run.py patch is skipped, not a broken form.
    run_txt = run_py.read_text()
    _run_anchor_present = (RUN_PY_MARKER in run_txt or RUN_PY_ANCHOR in run_txt)
    if not _run_anchor_present:
        print(f"note: run.py anchor {RUN_PY_ANCHOR!r} not found; skipping the")
        print("      optional run.py fallback patch. The u1-form plugin publishes")
        print("      the form callback itself, so the form still works.")
        print()

    print("[1/7] copy tools/ (form_gateway) + remove pre-plugin layout files")
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
    print(f"[2/7] deploy the u1-form plugin -> {plugin_dst}")
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
    print(f"[3/7] deploy the snapmaker_u1 hook plugin (folder plugin) -> {hook_plugin_dst}")
    if hook_files is None:
        print(f"  SKIPPED: no hook plugin package under {hook_plugin_src} "
              f"(partial checkout?)")
    else:
        for line in _deploy_hook_plugin(hook_files, hook_plugin_dst,
                                        dry_run=a.dry_run):
            print(f"  {line}")

    print()
    print("[4/7] remove the pip copy of the hook plugin (releases before 3.2.0)")
    notes: list[str] = []
    pythons = [venv_python] + _managed_env_pythons(notes)
    for line in notes + _remove_legacy_hook_dist(pythons, dry_run=a.dry_run):
        print(f"  {line}")

    print()
    print("[5/7] enable the plugins (user plugins are opt-in)")
    keys = (PLUGIN_KEY,) if hook_files is None else (PLUGIN_KEY, HOOK_PLUGIN_KEY)
    for key in keys:
        print(f"  {key}: {_enable_plugin(venv, dry_run=a.dry_run, key=key)}")

    print()
    print("[6/7] patch gateway/run.py (OPTIONAL fallback; anchor-based, marker-guarded)")
    if not _run_anchor_present:
        print("  skipped: no anchor in this Hermes build; the plugin publish covers it")
    else:
        status = _patch_run_py(run_py, dry_run=a.dry_run)
        print(f"  {status}: {run_py}")
        if status == "anchor-not-found":
            # Anchor vanished between pre-flight and here (a race). Non-fatal:
            # the plugin's pre_gateway_dispatch publish carries the form.
            print("  note: anchor vanished since pre-flight; skipping the fallback")
            print("        patch. The u1-form plugin publish covers the form.")
        elif status == "malformed-block":
            # A half-present prior block (begin marker, no end). _patch_run_py
            # refused to write it. Non-fatal now that the plugin carries the
            # form; flag it so the operator can tidy run.py.
            print("  WARNING: found the u1 begin marker without its end marker in")
            print("           run.py; left it untouched. Inspect run.py and restore")
            print("           the .u1-bak backup or remove the block manually. The")
            print("           plugin publish still carries the form regardless.")

    if a.dry_run:
        print()
        print("dry-run complete — no files modified.")
        return 0

    print()
    print("[7/7] verify: toolset resolution + hook-plugin registration")
    toolset_result = _verify(venv_python, tools_dir)
    print(" ", toolset_result)
    failed = toolset_result.startswith("FAIL")
    if hook_files is None:
        print("  hook plugin: SKIPPED (not deployed)")
    else:
        hook_result = _verify_hook_plugin(venv_python, hook_plugin_dst)
        print(" ", hook_result)
        failed = failed or not hook_result.startswith("OK:")
    if failed:
        print()
        print("VERIFY FAILED: see [7/7] above. The plugins will not work as")
        print("installed until that is fixed; re-run this installer afterwards.")
        return 1

    print()
    print("Done. Restart the Hermes gateway so the plugins load (and any run.py")
    print("fallback patch takes effect). Each inbound Telegram message installs")
    print("send_form on the live adapter class AND publishes the form callback")
    print("from the plugin, so the form keeps working across Hermes upgrades even")
    print("if the run.py fallback is absent (watch for the")
    print("'u1-form: TelegramAdapter.send_form installed' log line).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
