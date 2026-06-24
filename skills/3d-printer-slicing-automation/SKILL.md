---
name: 3d-printer-slicing-automation
description: "Snapmaker U1 staged slicing workflow: orient, render, slice, upload-only, and camera-gated starts."
version: 1.4.1
author: Brent Bolinger / snapmaker-u1-toolkit
license: MIT
metadata:
  hermes:
    tags: [snapmaker-u1, 3d-printing, slicing, safety, hardware-automation]
prerequisites:
  - python3.11+
  - OrcaSlicer 2.4.0+
  - numpy
  - PIL
  - Moonraker LAN access
---

# Snapmaker U1 slicing automation

## RULES FOR AGENTS — read first, no exceptions

When a user attaches a `.stl` or `.3mf` and asks to slice / prepare a print:

### YOU MUST

1. **Use `/opt/data/scripts/u1_slice_workflow.py` as the only slicing path.** Do not write your own slicing python. Do not call `orca-slicer` directly. Do not bypass the workflow because "you already know the defaults."
2. **Ask the user EVERY one of these questions, in order, ONE PER USER TURN,** before invoking the workflow:
   - *Orientation?* — Auto-orient (recommended) / As-authored / I have notes
   - *Filament/tool?* — show loaded slots from `u1_material_picker.py --json` output
   - *Preset?* — show options from `u1_profile_picker.py --json` output, recommend the one matching the object class (bracket/utility → 0.20 Strength; cosmetic → 0.16 Optimal)
   - *Supports?* — Auto-orient handled it (recommended) / Add supports / Show me overhangs
   - *Upload?* — Upload only (print=false) (recommended) / Upload + start / Cancel
3. **Wait for the user's reply between each question.** Do not stack questions. Do not assume.
4. **Only after collecting every answer**, invoke the workflow in headless mode with the collected values:
   ```bash
   python3 /opt/data/scripts/u1_slice_workflow.py <model.stl> \
     --tool <T0|T1|T2|T3> \
     --material <PETG|PLA|...> \
     --orient <auto|asauthored> \
     --profile <020_strength|016_optimal|...> \
     --upload-only --yes
   ```
5. **Show the user the preview render** (the `preview.png` from the workflow's output dir) before continuing.
6. **If the user chose "Upload + start"**, run the camera-gated start gate via `u1_print_start_gate.py` AFTER the workflow's upload completes. The bed-clear question is its own user turn. The default answer is **Cancel**.

### YOU MUST NOT

- Pick the orientation, tool, material, preset, support, or upload values yourself.
- Guess what the user "probably wants."
- Re-implement the staged workflow with custom Python or shell heredocs.
- Run the workflow's `--json-events` mode in a Telegram-driven session — that mode requires bidirectional stdin, which Telegram-driven agents cannot provide. The collect-answers-then-run-headless pattern above is the right one.
- Patch this skill manifest mid-session to paper over environment-specific paths or errors. If the workflow fails, surface the actual error to the user and stop. Don't improvise.
- Start a print from automation, cron, or any chained call. Start commands require explicit in-the-moment operator approval on the bed-clear question.

## Critical orientation lesson

Orca `--orient` only prints the optimal rotation; the toolkit applies it. Render and slice both consume the same `oriented.stl`. If a render disagrees with a slice, the rotation step was not run. The earlier "TOP / BED FOOTPRINT projections are misleading" lesson was incorrect — that was a symptom, not the bug. The bug was rendering an unrotated STL while slicing an auto-oriented one.

The current `u1_orient.py` applies Orca's chosen rotation by interpreting its output vector as the source-frame direction that becomes the new build-up (+Z) axis. Do not change that semantic without re-running the EGO trimmer regression test in `tests/test_u1_orient.py` and `tests/test_u1_slice_workflow.py`.

## 10-step canonical flow (mirrors the user MUST-asks above)

1. Receive `.stl`/`.3mf` — silent triage: dimensions, triangles, volume
2. **Ask orientation** — default Auto-orient
3. **Ask filament/tool** — only show loaded slots from `filament_detect` + toolmap
4. **Ask preset** — recommend based on object class
5. **Show oriented render** — iso + side-on-printer + top-down, generated from the `oriented.stl` the slicer will use
6. **Ask supports** — default "auto-orient handled it"
7. Slice — workflow handles this, using the same `oriented.stl` rendered in step 5
8. **Show preview render** — first-layer footprint + time/weight/material
9. **Ask upload** — default "Upload only (print=false)"
10. **If upload+start**: capture fresh LED-on bed photo, show it, ask "Bed clear?" — default Cancel

## Safety gates

- Upload-only is the default. Never run upload+start without bed-clear confirmation.
- Never start from arbitrary STL/3MF without a fresh camera snapshot + in-the-moment operator approval.
- If printer state, tool/material, slicer metadata, or bed visibility is unknown, fail closed.
- Start/resume/cancel are physical side effects; never run them from cron or chained automation.

## Agent integration: WHY collect-then-headless instead of `--json-events`

`u1_slice_workflow.py --json-events` emits questions on stdout and reads answers from stdin. That requires a bidirectional pipe held open across the user's reply. Telegram-driven agents like Hermes cannot keep a subprocess pipe alive across user turns — each user message starts a fresh tool-call context.

The realistic pattern: agent collects answers across turns (one question per user message), then invokes the workflow in `--yes` headless mode with all flags set. The workflow's `--json-events` mode is reserved for terminal-UI or CI/CD adapters where bidirectional pipes work.

The `recommended:true` field in `u1_material_picker.py --json` and `u1_profile_picker.py --json` output is a UI hint for highlighting the default option. The agent should still ask the user.

## References

- `references/snapmaker-u1-question-flow.md`
- `references/snapmaker-u1-safety-gates.md`
- `references/snapmaker-u1-orient-rotate-and-slice-review-2026-06.md`
