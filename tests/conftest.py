"""Shared pytest fixtures for the Snapmaker U1 toolkit test suite.

Most tests use the Moonraker mock fixture to avoid network calls — tests
must not require a real printer to run. CI-friendly.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

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
