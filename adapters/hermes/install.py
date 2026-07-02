#!/usr/bin/env python3
"""Install the u1 form-flow patch into a Hermes virtualenv.

Idempotent — safe to re-run after Hermes upgrades.

What this does:
  1. Detects the Hermes ``site-packages`` (auto-finds ``gateway/`` and ``tools/``
     under ``/opt/hermes/.venv`` by default; override with ``--venv``).
  2. Copies three files into Hermes' ``tools/``:
       - form_gateway.py   (blocking primitive; mirrors clarify_gateway;
                            from ``adapters/hermes/tools/``)
       - form_tool.py      (LLM-facing form tool + class-level monkey-patch
                            of TelegramPlatform — adds send_form + callback
                            routing without editing telegram.py; from
                            ``adapters/hermes/tools/``)
       - u1_form_telegram.py (the L1 pure renderer the patch's send_form
                            calls; single-sourced from the sibling
                            ``adapters/telegram/`` directory — no copy is
                            kept in the hermes tree)
     Copies are content-hashed; unchanged files are skipped.
  3. Edits ``gateway/run.py`` ONCE to wire ``agent.form_callback`` — this is
     the single source edit we can't sidestep, because callbacks are wired
     there alongside ``agent.clarify_callback`` and the agent class doesn't
     expose a hook for late additions. Anchor-based, idempotent via marker
     comment; aborts cleanly with a clear message if the anchor moved in a
     future Hermes release.
  4. Verifies the patched Hermes can import the new tools.

What this does NOT do:
  * Modify telegram.py — that's the class-level monkey-patch inside form_tool.py.
  * Modify the agent package — agent.form_callback is set at runtime in
    gateway/run.py; the agent class only needs to allow attribute assignment
    (it does).
  * Touch Hermes' bot token, config, or session state.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# gateway/run.py edit — the one source change, anchor-based and marked
# ---------------------------------------------------------------------------
RUN_PY_ANCHOR = "agent.clarify_callback = _clarify_callback_sync"
RUN_PY_MARKER = "# === u1 form patch ==="

RUN_PY_INSERT = '''
            # === u1 form patch ===
            # Mirrors _clarify_callback_sync above: agent calls the `form`
            # tool -> we render via the active adapter's send_form (Telegram
            # adapter is monkey-patched by tools/form_tool.py at startup) ->
            # block on the form_gateway primitive -> return the answer dict.
            def _form_callback_sync(form_schema):
                import uuid as _uuid
                try:
                    from tools import form_gateway as _fmod
                except ImportError:
                    return {"_error": "form_gateway not installed"}
                if not _status_adapter or not hasattr(_status_adapter, "send_form"):
                    return {"_error": "active adapter has no send_form (patch not loaded?)"}
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
            agent.form_callback = _form_callback_sync
            # === end u1 form patch ===
'''


def _site_packages(venv: Path) -> Path:
    """Find the lib/pythonX.Y/site-packages under a venv."""
    for sub in (venv / "lib").glob("python*"):
        sp = sub / "site-packages"
        if sp.is_dir():
            return sp
    raise SystemExit(f"no site-packages under {venv / 'lib'}")


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


def _copy_if_changed(src: Path, dst: Path) -> bool:
    if _sha256(src) == _sha256(dst):
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _patch_run_py(run_py: Path, *, dry_run: bool) -> str:
    """Insert the form_callback wiring into gateway/run.py exactly once.

    Returns: 'inserted' | 'already-applied' | 'anchor-not-found'.
    """
    txt = run_py.read_text()
    if RUN_PY_MARKER in txt:
        return "already-applied"
    if RUN_PY_ANCHOR not in txt:
        return "anchor-not-found"
    # Anchor preserved; inject immediately before it.
    new = txt.replace(RUN_PY_ANCHOR,
                      RUN_PY_INSERT.lstrip("\n") + "\n            " + RUN_PY_ANCHOR,
                      1)
    if dry_run:
        return "inserted (dry-run)"
    backup = run_py.with_suffix(run_py.suffix + ".u1-bak")
    # ALWAYS overwrite the backup so it captures the CURRENT pre-patch state.
    # Under Hermes upgrade, pip-install replaces run.py, our marker disappears,
    # and we re-run install — the backup MUST reflect the new Hermes version's
    # clean run.py (not the original install's), so --uninstall restores the
    # right thing. The marker-check above short-circuits when already applied,
    # so this only fires when we're about to actually edit.
    shutil.copy2(run_py, backup)
    run_py.write_text(new)
    return "inserted"


def _verify(venv_python: Path, tools_dir: Path) -> str:
    """Import each patched/new module; surface errors with file context."""
    # NB: this string is Python source for a subprocess. Avoid %-formatting
    # inside it — a previous version mixed an outer `%` with an inner
    # `'%s.%s' % (...)` and crashed with "not enough arguments".
    src = ("import sys; sys.path.insert(0, {tools_dir!r}); "
           "from tools import form_gateway, form_tool; "
           "import u1_form_telegram; "
           "print('OK: ' + form_gateway.__name__ + '.' + form_tool.__name__ + ' loaded')"
           ).format(tools_dir=str(tools_dir))
    proc = subprocess.run([str(venv_python), "-c", src],
                          text=True, capture_output=True, timeout=30)
    if proc.returncode != 0:
        return f"FAIL\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    return proc.stdout.strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Install u1 form-flow patch into Hermes.")
    ap.add_argument("--venv", type=Path, default=Path("/opt/hermes/.venv"),
                    help="Hermes virtualenv root (default: /opt/hermes/.venv)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would change without modifying anything.")
    ap.add_argument("--uninstall", action="store_true",
                    help="Remove the patch (restore gateway/run.py backup; delete new tool files).")
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
    venv_python = next((venv / "bin").glob("python*"), None)
    if venv_python is None:
        raise SystemExit(f"no python in {venv / 'bin'}")

    here = Path(__file__).resolve().parent
    src_tools = here / "tools"
    # u1_form_telegram.py (the L1 pure renderer) is single-sourced from the
    # sibling adapters/telegram/ directory — the hermes tree carries no copy.
    src_files = {
        "form_gateway.py": src_tools / "form_gateway.py",
        "form_tool.py": src_tools / "form_tool.py",
        "u1_form_telegram.py": here.parent / "telegram" / "u1_form_telegram.py",
    }

    print(f"venv:           {venv}")
    print(f"site-packages:  {sp}")
    print(f"python:         {venv_python}")
    print(f"action:         {'uninstall' if a.uninstall else 'install'}{' (dry-run)' if a.dry_run else ''}")
    print()

    if a.uninstall:
        for name in ("form_gateway.py", "form_tool.py", "u1_form_telegram.py"):
            tgt = tools_dir / name
            if tgt.exists():
                if a.dry_run:
                    print(f"would remove {tgt}")
                else:
                    tgt.unlink()
                    print(f"removed       {tgt}")
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

    print("[1/3] copy tools/")
    for name, src in src_files.items():
        if not src.exists():
            raise SystemExit(f"source missing: {src}")
        tgt = tools_dir / name
        if a.dry_run:
            changed = _sha256(src) != _sha256(tgt)
            print(f"  {'would copy' if changed else 'unchanged '} {tgt}")
        else:
            changed = _copy_if_changed(src, tgt)
            print(f"  {'copied   ' if changed else 'unchanged '} {tgt}")

    print()
    print("[2/3] patch gateway/run.py (anchor-based, idempotent)")
    status = _patch_run_py(run_py, dry_run=a.dry_run)
    print(f"  {status}: {run_py}")
    if status == "anchor-not-found":
        # Unreachable in practice (pre-flight above checks the same strings),
        # kept as a belt-and-braces guard against races.
        print()
        print(f"  ERROR: anchor {RUN_PY_ANCHOR!r} not found in gateway/run.py.")
        print("         Hermes may have changed the clarify wiring layout. Aborting.")
        return 2

    if a.dry_run:
        print()
        print("dry-run complete — no files modified.")
        return 0

    print()
    print("[3/3] verify imports")
    print(" ", _verify(venv_python, tools_dir))

    print()
    print("Done. Restart Hermes (e.g. `docker restart hermes-agent-stack`) so the")
    print("agent picks up the new `form` tool and the patched gateway/run.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
