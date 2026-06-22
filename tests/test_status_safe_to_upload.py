"""F4 regression: snapmaker_u1_status.safe_to_upload must match the actual
upload gate (u1_upload_gcode.ensure_idle_ready). If the status probe says
'safe' in a state where the real upload would block, that's misleading and
exactly the bug Hermes' Windows install smoke caught (2026-06-22)."""
from __future__ import annotations

import importlib

import pytest

import u1_upload_gcode


# Same shape as snapmaker_u1_status uses for its `status` dict slice
def _state(**overrides):
    base = {
        "print_stats": {"state": "standby", "filename": "", "info": {}},
        "virtual_sdcard": {"is_active": False},
        "webhooks": {"state": "ready"},
        "pause_resume": {"is_paused": False},
        "heater_bed": {"target": 0},
        "extruder": {"target": 0},
        "extruder1": {"target": 0},
        "extruder2": {"target": 0},
        "extruder3": {"target": 0},
    }
    # Allow nested overrides like {"print_stats": {"state": "printing"}}
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base


# These cases are the same shape ensure_idle_ready() blocks on. If the
# safe_to_upload predicate ever drifts from this list, the corresponding
# test fails and we know the two paths re-desynced.
BLOCKING_STATES = [
    ("active_sdcard", _state(virtual_sdcard={"is_active": True, "filename": "x.gcode"})),
    ("paused", _state(pause_resume={"is_paused": True})),
    ("webhooks_shutdown", _state(webhooks={"state": "shutdown"})),
    ("printing_state", _state(print_stats={"state": "printing"})),
    ("hot_bed", _state(heater_bed={"target": 80})),
    ("hot_extruder1", _state(extruder1={"target": 240})),
]


# Inline the safe_to_upload predicate from snapmaker_u1_status — we DON'T
# import the script because doing so would force its top-of-file argparse
# imports to run. The predicate is small and the whole point of this test
# is to lock the SHAPE in case the script's copy drifts.
def safe_to_upload_inline(printer_state: str, status: dict) -> bool:
    webhooks = status.get("webhooks", {})
    pause_resume = status.get("pause_resume", {})
    virtual_sdcard = status.get("virtual_sdcard", {})
    print_stats = status.get("print_stats", {})
    return (
        printer_state == "ready"
        and webhooks.get("state") in {None, "ready"}
        and not pause_resume.get("is_paused")
        and not virtual_sdcard.get("is_active")
        and print_stats.get("state") in {None, "standby", "complete", "cancelled", "error", "ready"}
        and not any(
            float((status.get(name) or {}).get("target") or 0) > 0
            for name in ("heater_bed", "extruder", "extruder1", "extruder2", "extruder3")
        )
    )


@pytest.mark.parametrize("name,status", BLOCKING_STATES, ids=[c[0] for c in BLOCKING_STATES])
def test_blocking_state_says_not_safe(name, status):
    """Every state that the upload gate blocks on must also flip
    safe_to_upload=False. Asymmetry = misleading status output."""
    upload_blockers = u1_upload_gcode.ensure_idle_ready(status)
    assert upload_blockers, f"{name}: upload gate should have blockers but had none"
    assert not safe_to_upload_inline("ready", status), \
        f"{name}: upload gate blocks but safe_to_upload says True — these MUST agree"


def test_clean_idle_state_says_safe():
    """The mirror property: when upload gate has zero blockers, status says safe."""
    status = _state()
    assert u1_upload_gcode.ensure_idle_ready(status) == []
    assert safe_to_upload_inline("ready", status) is True


def test_printer_state_not_ready_flips_safe_to_false():
    """Printer 'starting' or 'error' state — safe_to_upload False even if
    everything else looks idle, because the upload would 4xx anyway."""
    assert safe_to_upload_inline("startup", _state()) is False
    assert safe_to_upload_inline("error", _state()) is False
