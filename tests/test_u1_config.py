"""Test the env > json > default resolution order in u1_config."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import u1_config


def test_env_var_wins_over_file(fake_u1_env, monkeypatch):
    monkeypatch.setenv("SNAPMAKER_U1_HOST", "10.0.0.5")
    assert u1_config.get_u1_host() == "10.0.0.5"


def test_file_wins_over_default(fake_u1_env):
    # No env var, file has 192.0.2.1 from the fixture
    assert u1_config.get_u1_host() == "192.0.2.1"


def test_default_when_nothing_set(monkeypatch, tmp_path):
    # Empty config file, no env
    cfg = tmp_path / "empty.json"
    cfg.write_text("{}")
    monkeypatch.setenv("SNAPMAKER_U1_CONFIG", str(cfg))
    monkeypatch.delenv("SNAPMAKER_U1_HOST", raising=False)
    assert u1_config.get_u1_host(default="snapmaker-u1.local") == "snapmaker-u1.local"


def test_raises_when_truly_unconfigured(monkeypatch, tmp_path):
    cfg = tmp_path / "empty.json"
    cfg.write_text("{}")
    monkeypatch.setenv("SNAPMAKER_U1_CONFIG", str(cfg))
    monkeypatch.delenv("SNAPMAKER_U1_HOST", raising=False)
    with pytest.raises(RuntimeError, match="not configured"):
        u1_config.get_u1_host()


def test_port_env_wins(fake_u1_env, monkeypatch):
    monkeypatch.setenv("SNAPMAKER_U1_PORT", "9999")
    assert u1_config.get_u1_port() == 9999


def test_port_file_wins_over_default(fake_u1_env):
    assert u1_config.get_u1_port() == 7125  # from fixture file


def test_port_default(monkeypatch, tmp_path):
    cfg = tmp_path / "empty.json"
    cfg.write_text("{}")
    monkeypatch.setenv("SNAPMAKER_U1_CONFIG", str(cfg))
    monkeypatch.delenv("SNAPMAKER_U1_PORT", raising=False)
    assert u1_config.get_u1_port() == 7125  # FALLBACK_PORT


def test_base_url_composes_host_and_port(fake_u1_env):
    assert u1_config.get_u1_base_url() == "http://192.0.2.1:7125"


def test_corrupt_config_file_falls_back(monkeypatch, tmp_path):
    """A malformed JSON file should NOT crash — fall through to env/default."""
    cfg = tmp_path / "corrupt.json"
    cfg.write_text("{not valid json")
    monkeypatch.setenv("SNAPMAKER_U1_CONFIG", str(cfg))
    monkeypatch.setenv("SNAPMAKER_U1_HOST", "10.0.0.1")
    assert u1_config.get_u1_host() == "10.0.0.1"


# ---------- data-dir 3-tier resolution (env > /opt/data → home/xdg) ----------

def test_data_dir_env_var_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("SNAPMAKER_U1_DATA_DIR", str(tmp_path / "custom"))
    assert u1_config.get_data_dir() == tmp_path / "custom"


def test_data_dir_falls_back_to_home_when_opt_data_absent(monkeypatch, tmp_path):
    """Fresh community-install path — no /opt/data, no env override.

    Hermes finding F3: on Windows, `Path.home()` ignores HOME, so the
    fallback must check HOME explicitly first to keep test + Git Bash
    behavior consistent with Linux/macOS."""
    monkeypatch.delenv("SNAPMAKER_U1_DATA_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    # /opt/data path won't exist in pytest tmp; the helper checks .exists() so this is safe.
    expected = tmp_path / ".local" / "share" / "snapmaker-u1"
    assert u1_config.get_data_dir() == expected


def test_data_dir_honors_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.delenv("SNAPMAKER_U1_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert u1_config.get_data_dir() == tmp_path / "xdg" / "snapmaker-u1"


# ---------- L1 regression: importing must not require working config ----------

def test_scripts_import_without_any_config(monkeypatch):
    """Module-import time must NOT call get_u1_host(). Strip env, force a path
    where /opt/data isn't present, then re-import every script and ensure none
    raise RuntimeError about missing config."""
    import importlib
    monkeypatch.delenv("SNAPMAKER_U1_HOST", raising=False)
    monkeypatch.delenv("SNAPMAKER_U1_PORT", raising=False)
    monkeypatch.delenv("SNAPMAKER_U1_CONFIG", raising=False)
    monkeypatch.delenv("SNAPMAKER_U1_DATA_DIR", raising=False)
    for mod_name in ("u1_toolmap", "u1_upload_gcode", "u1_print_history",
                     "u1_camera", "u1_preflight", "u1_print_watchdog",
                     "u1_last_layer_watch", "snapmaker_u1_status",
                     "snapmaker_u1_snapshot"):
        import sys
        sys.modules.pop(mod_name, None)
        # Should NOT raise — config is only required at first use.
        importlib.import_module(mod_name)


def test_argparse_scripts_help_works_without_config(monkeypatch, tmp_path):
    """Every script with --help must succeed even with no config — argparse
    defaults must be lazy (None), not eager calls to get_u1_host()."""
    import subprocess, sys
    monkeypatch.delenv("SNAPMAKER_U1_HOST", raising=False)
    monkeypatch.delenv("SNAPMAKER_U1_PORT", raising=False)
    monkeypatch.delenv("SNAPMAKER_U1_CONFIG", raising=False)
    monkeypatch.delenv("SNAPMAKER_U1_DATA_DIR", raising=False)
    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    for script_name in ("u1_toolmap.py", "u1_upload_gcode.py", "u1_camera.py",
                        "u1_preflight.py", "snapmaker_u1_status.py",
                        "snapmaker_u1_snapshot.py"):
        proc = subprocess.run(
            [sys.executable, str(scripts_dir / script_name), "--help"],
            capture_output=True, text=True, timeout=10,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(scripts_dir)},
        )
        assert proc.returncode == 0, (
            f"{script_name} --help exited {proc.returncode}: "
            f"stderr={proc.stderr[:500]}"
        )
        assert "usage:" in proc.stdout.lower(), \
            f"{script_name} --help produced no usage line: {proc.stdout[:300]}"


# ---------- .env auto-loader ----------

def _reset_dotenv_flag():
    """The loader is one-shot per process — flip the flag off for fresh tests."""
    u1_config._DOTENV_LOADED = False


def test_dotenv_loads_when_cwd_has_env_file(monkeypatch, tmp_path):
    """Standalone-Python use: user runs `python script.py` from a dir with .env."""
    _reset_dotenv_flag()
    (tmp_path / ".env").write_text("SNAPMAKER_U1_HOST=10.99.99.99\nSNAPMAKER_U1_PORT=9999\n")
    monkeypatch.delenv("SNAPMAKER_U1_HOST", raising=False)
    monkeypatch.delenv("SNAPMAKER_U1_PORT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert u1_config.get_u1_host() == "10.99.99.99"
    assert u1_config.get_u1_port() == 9999


def test_dotenv_walks_up_from_cwd(monkeypatch, tmp_path):
    """Running from a subdirectory still finds the project-root .env."""
    _reset_dotenv_flag()
    (tmp_path / ".env").write_text("SNAPMAKER_U1_HOST=192.0.2.99\n")
    sub = tmp_path / "deeper" / "and" / "deeper"
    sub.mkdir(parents=True)
    monkeypatch.delenv("SNAPMAKER_U1_HOST", raising=False)
    monkeypatch.chdir(sub)
    assert u1_config.get_u1_host() == "192.0.2.99"


def test_dotenv_does_not_overwrite_explicit_env(monkeypatch, tmp_path):
    """Explicit env always wins — .env is a fallback, not an override."""
    _reset_dotenv_flag()
    (tmp_path / ".env").write_text("SNAPMAKER_U1_HOST=10.0.0.99\n")
    monkeypatch.setenv("SNAPMAKER_U1_HOST", "172.16.0.1")
    monkeypatch.chdir(tmp_path)
    assert u1_config.get_u1_host() == "172.16.0.1"


def test_dotenv_handles_quoted_and_commented_lines(monkeypatch, tmp_path):
    """Tolerate `KEY="quoted"`, `KEY='single'`, `# comment` lines."""
    _reset_dotenv_flag()
    (tmp_path / ".env").write_text(
        '# a comment\n'
        'SNAPMAKER_U1_HOST="10.55.55.55"\n'
        "BLANK_LINE_BELOW=\n"
        "\n"
        "MALFORMED_NO_EQUALS\n"
    )
    monkeypatch.delenv("SNAPMAKER_U1_HOST", raising=False)
    monkeypatch.chdir(tmp_path)
    assert u1_config.get_u1_host() == "10.55.55.55"


def test_dotenv_missing_file_is_silent_noop(monkeypatch, tmp_path):
    """No .env anywhere → loader runs cleanly, env unchanged."""
    _reset_dotenv_flag()
    monkeypatch.delenv("SNAPMAKER_U1_HOST", raising=False)
    monkeypatch.chdir(tmp_path)  # tmp_path has no .env
    with pytest.raises(RuntimeError, match="not configured"):
        u1_config.get_u1_host()


def test_cron_scripts_run_main_without_crash(monkeypatch, tmp_path):
    """Cron-style scripts (no --help) must NOT crash with NameError or other
    leftover-symbol bugs when main() runs. Use a bogus host so they fail-soft
    on the network call but exercise the full main() code path. Catches the
    'ROOT used but never imported' class of refactor mistake."""
    import subprocess, sys
    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(scripts_dir),
        "SNAPMAKER_U1_HOST": "192.0.2.1",  # TEST-NET-1, unroutable
        "SNAPMAKER_U1_DATA_DIR": str(tmp_path / "data"),
    }
    for script_name in ("u1_print_history.py", "u1_last_layer_watch.py",
                        "u1_print_watchdog.py"):
        proc = subprocess.run(
            [sys.executable, str(scripts_dir / script_name)],
            capture_output=True, text=True, timeout=30, env=env,
        )
        # Exit code MAY be non-zero (network unreachable) but must NOT be
        # a Python traceback. NameError specifically would be the regression.
        combined = proc.stdout + proc.stderr
        assert "NameError" not in combined, \
            f"{script_name}: NameError leaked from main(): {combined[:500]}"
        assert "Traceback" not in combined or "RuntimeError" in combined, \
            f"{script_name}: unexpected traceback: {combined[:500]}"
