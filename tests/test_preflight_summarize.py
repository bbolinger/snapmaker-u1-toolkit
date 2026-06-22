"""F5 regression: u1_preflight.summarize() must accept the resolved host
as an argument and NOT call get_u1_host() internally — otherwise
`--host 192.168.1.123` is silently overridden by env/config when the
summary is generated. (Hermes finding, 2026-06-22.)"""
from __future__ import annotations

import os

import pytest

import u1_preflight


def _minimal_state():
    return {
        "server_info": {"klippy_state": "ready", "moonraker_version": "0.9"},
        "printer_info": {"state": "ready", "hostname": "u1-test"},
        "objects": {
            "print_stats": {"state": "standby", "filename": "", "info": {}},
            "heater_bed": {"temperature": 22.0, "target": 0},
            "extruder": {"temperature": 22, "target": 0, "state": "ready", "can_extrude": False},
            "extruder1": {"temperature": 22, "target": 0, "state": "ready", "can_extrude": False},
            "extruder2": {"temperature": 22, "target": 0, "state": "ready", "can_extrude": False},
            "extruder3": {"temperature": 22, "target": 0, "state": "ready", "can_extrude": False},
            "toolhead": {"extruder": "extruder", "homed_axes": "xyz", "position": [0, 0, 0]},
            "display_status": {"progress": 0.0, "message": None},
            "virtual_sdcard": {"is_active": False, "progress": 0.0},
            "pause_resume": {"is_paused": False},
            "webhooks": {"state": "ready"},
        },
    }


def _minimal_camera():
    return {"ok": True, "fresh": True, "image": "/tmp/x.jpg", "monitor": {"modified": 0}, "bed_check": {}}


def test_summarize_uses_passed_host_not_env(monkeypatch):
    """The --host CLI value (passed to summarize) wins over env/config.
    If summarize() internally called get_u1_host(), the env would be the
    only source of truth and --host would silently lose."""
    monkeypatch.setenv("SNAPMAKER_U1_HOST", "10.0.0.255")  # env says this
    out = u1_preflight.summarize(_minimal_state(), _minimal_camera(), host="192.168.99.99")
    assert out["printer"]["ip"] == "192.168.99.99", \
        f"summarize ignored host arg, used env instead: {out['printer']}"


def test_summarize_falls_back_to_address_from_printer_info():
    """If the printer reports its own address in printer_info.address,
    prefer that over the passed host (it's more authoritative)."""
    state = _minimal_state()
    state["printer_info"]["address"] = "192.168.7.7"
    out = u1_preflight.summarize(state, _minimal_camera(), host="192.168.99.99")
    assert out["printer"]["ip"] == "192.168.7.7"


def test_summarize_works_without_host_when_address_present():
    """If printer reports its own address, summarize() should NOT need a host
    arg at all — exercising the no-config-required code path."""
    state = _minimal_state()
    state["printer_info"]["address"] = "192.168.7.7"
    out = u1_preflight.summarize(state, _minimal_camera())
    assert out["printer"]["ip"] == "192.168.7.7"
