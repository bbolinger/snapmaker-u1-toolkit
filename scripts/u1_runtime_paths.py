"""Where the deployed u1_*.py scripts live — self-locating.

The workflow emits next_command strings and spawns sibling scripts (gate,
notify, kit re-entry). Those paths were hardcoded /opt/data/scripts/...,
which is only true for the Linux runtime deploy. But every consumer in
scripts/ IS one of those siblings: the deployed copy of this module sits
in the deployed scripts dir, the repo copy sits in repo scripts/. So the
directory of THIS FILE is the runtime scripts dir — no env chain, no
existence probing, correct on any platform and in dev checkouts.

U1_RUNTIME_SCRIPTS_DIR overrides for the exotic case (emitting commands
for a runtime that is not the one executing).

Gateway-side components that reference these scripts from OUTSIDE the
scripts dir (the u1_confirm_start hook, the u1_kit tool) cannot
self-locate and use an env chain instead:
U1_RUNTIME_SCRIPTS_DIR > $HERMES_HOME/scripts (probed) > /opt/data/scripts.
tests/test_runtime_paths.py covers both shapes.
"""
from __future__ import annotations

import os
from pathlib import Path


def scripts_dir() -> Path:
    explicit = os.environ.get("U1_RUNTIME_SCRIPTS_DIR", "").strip()
    if explicit:
        return Path(explicit)
    return Path(__file__).resolve().parent


def script_path(name: str) -> str:
    """Absolute path of a sibling runtime script, as a string ready for
    argv or an emitted next_command."""
    return str(scripts_dir() / name)


def python_cmd() -> str:
    """Interpreter name for EMITTED command strings (next_command, stage-1
    commands) that the agent's terminal or an operator will run on this
    same machine. POSIX keeps the conventional python3; stock Windows has
    no python3 - only python (and a WindowsApps python3 STUB that spawns
    and dies silently, worse than not existing). Code that spawns
    subprocesses directly should use sys.executable instead."""
    return "python" if os.name == "nt" else "python3"
