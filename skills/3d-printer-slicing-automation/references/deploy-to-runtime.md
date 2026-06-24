# Deploy Pipeline — Snapmaker U1 Toolkit

## Overview

The workspace repo at `/opt/data/workspaces/snapmaker-u1-toolkit` (branch `v1.4.0-dev`) deploys to runtime paths via an idempotent deploy script that copies new files, creates dated backups, and prints a Diff summary comparing workspace vs runtime post-deploy.

## Target Directories

| Variable | Destination | Purpose |
|---|---|---|
| `SCRIPT_DST` | `/opt/data/scripts/` | Python scripts the workflow calls (e.g., `u1_slice_workflow.py`) |
| `TOOLS_DST` | `/opt/data/tools/` | Standalone tools (e.g., `gcode_inject_thumbnail.py`, `u1_orient.py`) |
| `SKILL_DST` | Container path under `/opt/data/skills/hardware-automation/3d-printer-slicing-automation/` | Hermes skill manifest (SKILL.md) and references — **must be the container path, not host-side bind mount** |

## Deploy process (deploy_to_runtime.sh)

1. Copies files from workspace to RUNTIME paths
2. Creates dated backups of overwritten files before replacing them
3. Prints Diff summary showing what changed between workspace and runtime post-deploy

### Operational lesson: Path boundaries matter in Docker-based setups

The container path for `SKILL_DST` was originally assumed to be the host-side bind mount, which produced `Permission denied`. The correct path is inside the Hermes container filesystem. This same boundary applies when any script writes to directories visible through `/opt/data/` inside the agent: it's the containerized filesystem, not whatever bind mount happened outside.

## Runtime paths quick-reference

| Path | Role |
|---|---|
| `/opt/data/scripts/u1_slice_workflow.py` | Canonical slice workflow entry point |
| `/opt/data/tools/u1_orient.py` | Orientation engine (Kabsch rotation) |
| `/opt/data/tools/gcode_inject_thumbnail.py` | Thumbnail renderer + G-code header splicer |
| `/opt/data/scripts/u1_upload_gcode.py` | Upload script with `--stl` thumbnail injection |
| `/opt/data/scripts/u1_preflight.py` | Printer status, temps, toolhead, camera checks |
| `/opt/data/scripts/u1_toolmap.py` | Active-tool/material/filament map |
| `/opt/data/scripts/u1_print_watchdog.py` | Quiet issue monitoring (5-min cadence) |
| `/opt/data/scripts/u1_last_layer_watch.py` | Layer milestone photos |
| `/opt/data/scripts/u1_print_history.py` | Print history ledger |
| `/opt/data/snapmaker_u1/README.md` | Living project record; update after meaningful changes, failures, or safety lessons |
