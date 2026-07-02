"""Shared pytest fixtures for the Snapmaker U1 toolkit test suite.

Most tests use the Moonraker mock fixture to avoid network calls — tests
must not require a real printer to run. CI-friendly.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest


# v2.0 Phase 2: every test gets a fresh SNAPMAKER_U1_DATA_DIR pointing at
# a tmp dir, so request.json files from one test don't leak into another
# (and don't accidentally trigger find_recent_request_for_model hits in
# integration tests). u1_config.get_data_dir reads this env var first.
@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch, request):
    """Per-test data dir so requests/ + state files are sandboxed."""
    monkeypatch.setenv('SNAPMAKER_U1_DATA_DIR', str(tmp_path / '_data_dir'))
    # Deterministic interaction mode: u1_config._load_dotenv_if_present()
    # falls back to /opt/data/.env, so an operator running the suite on a
    # box with U1_INTERACTION_MODE=form live (e.g. during v2.2 form testing)
    # silently flips every staged-flow test into form mode (live 2026-07-02:
    # two "failures" that were really env leakage). Kill the fallback for
    # tests and scrub anything already loaded into this process; tests that
    # want form mode set U1_INTERACTION_MODE explicitly. Exemption: the
    # loader's own tests (test_u1_config.py) exercise the real walk with
    # their own tmp .env files.
    if request.node.fspath.basename != 'test_u1_config.py':
        import u1_config
        monkeypatch.setattr(u1_config, '_load_dotenv_if_present', lambda: None)
    monkeypatch.delenv('U1_INTERACTION_MODE', raising=False)

    # HARD SAFETY: the test suite must NEVER reach the operator's real Telegram.
    # tools/u1_grace_notify_hermes.sh defaults HERMES_BIN->`hermes` (the real
    # binary on PATH) and DEST->`telegram` (the real chat), so any test that
    # runs the real notify path DMs the operator. Live 2026-07-02: every full
    # suite run spammed Brent a "print starting" notification. Force a no-op
    # sender + a non-telegram destination for EVERY test, and shadow `hermes`
    # on PATH so a bare `hermes` call can't hit the real binary either. Any
    # invocation is logged so the offending test is identifiable. Tests that
    # assert on notify behaviour set their own stub, which overrides this.
    _stub_dir = tmp_path / "_hermes_stub"
    _stub_dir.mkdir(exist_ok=True)
    _log = os.environ.get("U1_TEST_HERMES_LOG", "/tmp/u1_test_hermes_calls.log")
    _stub = _stub_dir / "hermes"
    _stub.write_text(
        "#!/bin/bash\n"
        f'echo "SEND-ATTEMPT test=${{PYTEST_CURRENT_TEST:-?}} args=$*" >> "{_log}"\n'
        "exit 0\n")
    _stub.chmod(0o755)
    monkeypatch.setenv("HERMES_BIN", str(_stub))
    monkeypatch.setenv("U1_GRACE_NOTIFY_DEST", "test-noop-not-telegram")
    monkeypatch.setenv("PATH", str(_stub_dir) + os.pathsep + os.environ.get("PATH", ""))
    # Belt to the HERMES_BIN suspenders: the gate reads U1_GRACE_NOTIFY_CMD from
    # os.environ DIRECTLY (not via u1_config's dotenv loader), so a leaked value
    # (test_u1_config loads the real /opt/data/.env into os.environ) would make a
    # start-path test run the real notify command. Scrub it for every test; and
    # default the grace window to 0 so a start-path test that doesn't explicitly
    # set grace_seconds skips the window entirely — no notify attempt, and no
    # 120s real-time sleep. Tests that exercise grace/notify set these
    # explicitly (via grace_seconds= or monkeypatch), which overrides the below.
    monkeypatch.delenv("U1_GRACE_NOTIFY_CMD", raising=False)
    monkeypatch.setenv("U1_GRACE_PERIOD_SECONDS", "0")
    yield

# Real-Orca test harness (added 2026-06-26). When pytest runs from
# dev-container (Alpine/musl), the bundled Orca appimage can't execute
# directly. Tests marked with @pytest.mark.real_orca instead use the shim
# at tests/_orca_shim/orca-via-hermes.sh to invoke Orca via
# `docker exec hermes-agent-stack`. Path translation is automatic for any
# arg under /appdata/hermes/.
_HERE = Path(__file__).resolve().parent
_ORCA_SHIM = _HERE / "_orca_shim" / "orca-via-hermes.sh"
_HERMES_VISIBLE_TMP_ROOT = Path("/appdata/hermes/test-tmp")


def _hermes_orca_available() -> bool:
    """True iff (a) we can docker-exec into hermes-agent-stack, AND (b) the
    shim runs Orca's --help cleanly. Returns False with no exception so the
    skip-marker decorators can use it."""
    if not _ORCA_SHIM.exists() or not os.access(_ORCA_SHIM, os.X_OK):
        return False
    try:
        proc = subprocess.run(
            [str(_ORCA_SHIM), "--help"],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0 and "OrcaSlicer" in proc.stdout


_HAS_REAL_ORCA = _hermes_orca_available()


@pytest.fixture
def hermes_visible_tmp(request):
    """Yield a tmp dir under /appdata/hermes/test-tmp/ — visible from BOTH
    dev-container (this container, where pytest runs) and Hermes
    (where the Orca shim docker-execs to). Auto-cleaned after the test.

    Use this for any test that calls the real Orca shim — pytest's default
    tmp_path is under /tmp which Hermes can't see.

    Skips (not errors) when the Hermes stack isn't present: this fixture is
    a DEPENDENCY of real_orca, so pytest sets it up BEFORE real_orca's own
    skip check runs — on a host without the /appdata mount (e.g. GitHub
    Actions) the mkdir raised PermissionError and the job went red even
    though every runnable test passed."""
    if not _HAS_REAL_ORCA:
        pytest.skip("real Orca unreachable (no Hermes container or shim not working)")
    try:
        _HERMES_VISIBLE_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        pytest.skip(f"hermes-visible tmp unavailable ({exc}) — no /appdata mount")
    scratch = _HERMES_VISIBLE_TMP_ROOT / f"{request.node.name}-{uuid.uuid4().hex[:8]}"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@pytest.fixture
def real_orca(monkeypatch, hermes_visible_tmp):
    """Sets ORCA_SLICER_BIN to the docker-exec shim so the workflow's
    real_orca_slice() actually runs OrcaSlicer (via Hermes).

    Tests using this fixture should ALSO use `hermes_visible_tmp` for any
    paths handed to the workflow, since Hermes can't see dev-container's
    pytest tmp_path.

    Skips the test cleanly if Hermes/Orca isn't reachable from this env."""
    if not _HAS_REAL_ORCA:
        pytest.skip("real Orca unreachable (no Hermes container or shim not working)")
    monkeypatch.setenv("ORCA_SLICER_BIN", str(_ORCA_SHIM))
    # Force u1_orient and dependent modules to re-resolve DEFAULT_ORCA from
    # the env (it's set at import time otherwise).
    import importlib
    import u1_orient
    importlib.reload(u1_orient)
    import u1_slice_workflow
    importlib.reload(u1_slice_workflow)
    return {"shim": _ORCA_SHIM, "tmp": hermes_visible_tmp}

# Make scripts/ importable from tests
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# Some scripts call get_u1_host() at module-import time (see DESIGN NOTE in
# README — to be fixed by deferring config resolution to first-use). Until
# then, set a benign env var BEFORE any test-collection imports happen so
# the scripts import without RuntimeError.
os.environ.setdefault("SNAPMAKER_U1_HOST", "192.0.2.1")  # TEST-NET-1, never routable
os.environ.setdefault("SNAPMAKER_U1_PORT", "7125")


@pytest.fixture
def fake_u1_env(tmp_path, monkeypatch):
    """Point u1_config at a tmp data dir + override host so no real lookups happen.

    Also monkeypatches u1_config.CONFIG_PATH since that constant is computed
    at module-import time and ignores subsequent env changes.
    """
    cfg_path = tmp_path / "u1_config.json"
    cfg_path.write_text(json.dumps({"host": "192.0.2.1", "port": 7125}))
    monkeypatch.setenv("SNAPMAKER_U1_CONFIG", str(cfg_path))
    monkeypatch.delenv("SNAPMAKER_U1_HOST", raising=False)
    monkeypatch.delenv("SNAPMAKER_U1_PORT", raising=False)
    # Override the import-time constant too
    import u1_config
    monkeypatch.setattr(u1_config, "CONFIG_PATH", cfg_path)
    return {"host": "192.0.2.1", "port": 7125, "config_path": cfg_path, "tmp": tmp_path}


@pytest.fixture
def moonraker_responses():
    """Build a dict of {endpoint_path: json_response} the mock will serve.

    Override per-test by mutating the returned dict before triggering the
    code-under-test.
    """
    return {
        "/server/info": {"result": {"klippy_state": "ready"}},
        "/printer/info": {"result": {"hostname": "u1-test"}},
        "/printer/objects/query": {
            "result": {
                "status": {
                    "print_stats": {"state": "standby", "filename": "", "info": {}},
                    "toolhead": {"extruder": "extruder1", "homed_axes": "xyz"},
                    "extruder": {"temperature": 35.0, "target": 0.0},
                    "extruder1": {"temperature": 240.0, "target": 240.0},
                    "extruder2": {"temperature": 36.0, "target": 0.0},
                    "extruder3": {"temperature": 34.0, "target": 0.0},
                    "heater_bed": {"temperature": 80.0, "target": 80.0},
                    "virtual_sdcard": {"file_position": 0, "file_size": 0},
                    "display_status": {"progress": 0.0, "message": None},
                    "pause_resume": {"is_paused": False},
                }
            }
        },
    }


@pytest.fixture
def mock_http(monkeypatch, moonraker_responses):
    """Patch http_json across modules to serve from moonraker_responses.

    Matches by URL substring so callers can build full URLs naturally.
    """
    def _fake(url, timeout=8.0):
        for path, payload in moonraker_responses.items():
            if path in url:
                return payload
        raise AssertionError(f"unmocked URL: {url}")

    targets = ["u1_toolmap", "u1_upload_gcode", "u1_preflight",
               "u1_print_history", "snapmaker_u1_status"]
    for mod in targets:
        try:
            monkeypatch.setattr(f"{mod}.http_json", _fake, raising=False)
        except (ImportError, AttributeError):
            pass
    return _fake
