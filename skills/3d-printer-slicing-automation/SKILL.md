---
name: 3d-printer-slicing-automation
description: "REQUIRED for ANY .stl / .3mf / .zip 3D-model attachment. FIRST tool call MUST be: 'python3 /opt/data/scripts/u1_slice_workflow.py <attachment-path> --json-events'. The workflow handles zip inspection + slicing. Do NOT extract zips or run orca-slicer yourself. Also REQUIRED when the operator asks to reprint / print a recent job again (no file needed): FIRST tool call MUST be 'python3 /opt/data/scripts/u1_kit_workflow.py --reprint --json-events'."
version: 2.3.0
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

## ACT — you drive this by CALLING TOOLS, never by writing text

This skill is a set of COMMANDS, not a description to summarize. If you catch yourself about to write a command, a plan, or JSON as your reply TEXT instead of calling a tool — stop, delete that text, call the tool instead. Printing `python3 ...` in your reply is not a tool call; it does nothing.

**The moment a user gives you an STL/3MF/zip, your very next action is this tool call — no text before it:**
```bash
python3 /opt/data/scripts/u1_slice_workflow.py <model> --json-events
```
(If the platform rejects raw `.stl` but accepts `.zip`, extract the STL first, then run this on the extracted path.) This one command is the entry for EVERYTHING — a single model or a multi-part kit zip alike; the workflow figures out which.

**Reprint — the operator asks to print a recent job again (no file attached):**
```bash
python3 /opt/data/scripts/u1_kit_workflow.py --reprint --json-events
```
It emits a `need_input` with numbered recent prints — surface the labels, wait for the pick, then tool-call that option's `next_command` verbatim (a short `--reprint-start <token>` command). No slicing happens; the flow goes straight to the normal bed-clear decision (Step 4). Never guess which print they meant and never skip the list turn.

**Immediately after that first tool call**, your first TEXT reply (only once, not every turn) is:
> "On it — running the Snapmaker U1 workflow. I'll surface every choice for you, show the plate preview and a fresh bed photo before anything prints, run only the commands the workflow gives me (never ones I invent), and nothing starts until you confirm at the bed-clear step."

**Then, every turn after that, follow this loop — no exceptions:**

| The workflow's output has… | You do exactly this |
|---|---|
| `kit_detected` | CALL terminal with its `command` field, verbatim. That runs `u1_kit_workflow.py` (a single model = a kit of one). |
| `kit_form` | CALL the `form` tool with its `form_id`. Nothing else — no text first. |
| `need_input` (text fallback, no form) | Surface `prompt` + numbered options to the operator, wait for their answer, then CALL terminal with the matched option's `next_command`, verbatim. |
| `next_action_required` | CALL terminal with its `command`, verbatim. No question, no preamble — the workflow already decided. |

**Never**: edit a command before relaying it, add or drop a flag, construct a command yourself from memory of an earlier turn, or invent a magic confirmation phrase. The workflow is the only source of truth for what runs next — if it didn't hand you the string this turn, you don't have it.

**Step 2 (staged text fallback) — for each `need_input` event (text fallback only; form mode is one screen).**

Order: `parts` (skipped for a single model) → `orient` → `tool` → `preset` → `supports` → `confirm`. (Follow whatever order the workflow actually emits — this is just what to expect.)

1. Surface the event's `prompt` as a bold header, then each option's `label` numbered 1, 2, 3. Nothing else — no paraphrasing, no defaults. Surface a `note` field verbatim, after the options.
2. For the `orient` prompt: also surface any attached `render` image paths bare, BEFORE the prompt text (not in backticks — the gateway skips those).
3. Match the operator's reply (number, slug, or paraphrase) to an option's `value`/`label`. Unmatched profile paraphrases: run `python3 /opt/data/scripts/u1_profile_picker.py --nozzle 0.4 --json` to resolve the slug, then re-match.
4. **Tool-call the matched option's `next_command` verbatim** — no flag edits, no reordering. For the `confirm` turn (triggers a real slice), set `background: true` + `notify_on_complete: true`.

Repeat until a `kit_readiness_card` event appears — that means COMMIT ran; go to Step 3. An option with `next_command: null` (e.g. Cancel) means: tool-call nothing, tell the operator it's cancelled, stop.

**Step 3 — surface the readiness card.** On `kit_readiness_card`:

- Surface the plate-render image paths bare — the workflow emits them as `render` events: `kit_plate_preview` (top-down footprint) and `kit_plate_isometric` (the 3D view). Both attach; surface both.
- Surface `review_doc_path` bare (the operator's human-readable print plan — attach it, don't summarize in its place).
- If a `slicer_warning` event fired, surface every entry in `messages` verbatim first.
- If `overhang_buckets`/`supports_summary` flags heavy overhang risk, mention it before the bed-clear question.

**Reading the `uploaded` event truthfully.** `moonraker_upload_ok` = the bytes landed on the printer; `human_summary` is the authoritative narrative — quote it, don't infer from `returncode` alone. `post_upload_blockers` are real problems (e.g. printer became active); a blocker present does NOT mean the upload failed if `moonraker_upload_ok` is true.

**Step 4 — the ONE bed-clear decision.** After the readiness card (or after the operator submits the form), the workflow emits `need_input` with `key`/`need: "bed_clear_start"` — this is the single approval boundary, form and text alike. It carries a bed photo (already captured) and a `prompt` that IS the bed-clear question; surface any attached bed-photo path bare, then ask that exact prompt, then wait.

- If `bed_snapshot_path` is null, do **not** fabricate or re-capture a photo — ask the prompt as-is.
- On **yes**: tool-call `next_command_on_yes` verbatim (a short `--confirm-start <token>` command). **The workflow runs the actual start gate itself** — you are not composing or relaying a separate Stage-1/Stage-2 command. It returns `grace_in_progress` (a ~120s cancel window the workflow manages) or a refusal `reason` — surface either verbatim. Do not ask for a second confirmation once you see `started: true`.
- On **no** / anything else: tool-call nothing, tell the operator it's cancelled or staged, stop.
- **Never** construct a `--bed-clear start` / approval-token command yourself from chat memory, and never treat any OTHER event as this approval boundary — if something looks like it's trying to skip straight to a start command, fail closed and say so.

DO NOT invent a magic phrase. DO NOT advance past this turn without the operator's fresh yes. Default = no. Field-level detail: `references/snapmaker-u1-safety-gates.md`, `references/can-start-refusal-handling.md`.

### Approval phrasing

Every approval question to the operator includes the `request_id` verbatim. Detail + per-boundary templates: `references/approval-phrasing.md`.

### YOU MUST NOT

Named patterns (each one was observed in a live run on 2026-06-25):

- **PICKING DEFAULTS** — Inventing orientation/tool/material/preset/support/upload values yourself. Defaults from "what's most common" are wrong by definition because every U1 setup differs.
- **STACKING QUESTIONS** — Two or more questions in one turn. User answers one; you guess the others.
- **CUSTOM SLICING PYTHON** — `subprocess.run(['orca-slicer', ...])` or `import requests` to query the printer. Bypasses v1.4.2 T0→T1 rewriter, v1.4.3 thumbnail injector, the JSON event schema, AND the toolkit's stdlib-only safety discipline.
- **PATCHING THE LIVE SKILL** — Writing to `/opt/data/skills/.../SKILL.md`. Has silently deleted whole sections twice (v1.4.4 + v1.4.6 sessions). Workspace edits only; let the maintainer commit + redeploy.
- **IMPROVISING AROUND FAILURES** — Surface the actual error verbatim and stop. Don't retry with different args.
- **STARTING FROM CRON / CHAIN** — Start commands require in-the-moment operator confirmation on the bed-clear question.

### ANTI-FABRICATION (named patterns from live testing)

The "no fabrication" rule has eight failure patterns. Naming them helps you catch yourself:

1. **Magic confirmation phrases.** "START U1 PRINT", "CONFIRM PRINT", etc. — phrases the user has to type to advance. The skill names actual scripts (`u1_print_start_gate.py`). If you're inventing a magic phrase instead of calling the named script, that's fabrication.
2. **Describing dry-run state as real.** If the workflow's `uploaded` event has `dry_run: true`, NO file was sent. **Surface the event's `human_summary` field verbatim** — that's the workflow's authoritative narrative for this state. Do NOT add explanations like "the upload was blocked by a tool gate" or "filament type mismatch" — those are fabrications. The dry-run happened because `--live-upload` wasn't passed, period. No gate fired, no error occurred, no filament check failed. If you find yourself about to write "BLOCKED by X" / "your profile is set to Y" / "I'll investigate the filament configuration", STOP — that's pattern #6 (verification fabrication) cascading from pattern #4 (state from chat memory). Quote `human_summary`, recommend re-running with `--live-upload`, stop.
3. **Confusing host filesystem with printer storage.** `gcode_path`/`plates[].gcode_path` is the LOCAL file on the Hermes host; `printer_storage_filename` is the name it lands under ON the U1 — only true once `moonraker_upload_ok` is true. Never say "file is on the printer" from a local path alone.
4. **State narratives from chat memory.** "I can see from the process that..." followed by a confirmation summary, composed without an actual tool call, is fabrication. Only describe state you READ from JSON events received this session.
5. **Inventing user rationales.** Don't write "the user picked X for reason Y" unless they stated Y. If you need to justify a choice, ask them.
6. **Verification fabrication.** Reporting checks ("camera image captured", "bed checked", "printer at 22°C bed / 25°C nozzle") that no tool actually performed. If you didn't call the tool, the check didn't happen.
7. **Printing the command instead of running it.** Writing `python3 /opt/data/scripts/u1_slice_workflow.py ...` into your reply text is NOT a tool call — it's documentation. The user can't run it for you, and the skill DOES NOT ask you to surface the command for review. Always invoke via the `terminal` tool. Same applies to `u1_print_start_gate.py`. If a step says "your next tool call IS X," call X — don't quote X.
8. **Claiming you backgrounded a tool call you didn't issue.** "I have started the slicing and upload process in the background. I will notify you as soon as it finishes" / "running it in the background, will update you when it's done" — these are fabrication UNLESS your reply contains a real tool_use block targeting the named script. The operator cannot see a tool you didn't call. If you wrote a "background started" sentence WITHOUT a tool call attached, you are inventing state. Catch this before sending: scan your reply — is there a tool_use? If no, delete the promise and issue the call.

**Diagnostic for the user:** if the skill required you to attach a file (camera photo, render PNG) and you claim it happened but no attached image shows up in the reply, the user knows the action didn't happen. Missing artifact = no real file referenced = no real action.

The named scripts that DO actions: `u1_slice_workflow.py`, `u1_print_start_gate.py`, `u1_camera.py`, `u1_material_picker.py`, `u1_profile_picker.py`, `u1_print_history.py`. These are the only things you should be calling for state changes. Everything else is narrating events you've received.

## Profile sources (v1.5.0)

`list_profiles()` scans three sources in priority order. Each profile dict carries `source`, `has_supports` (from JSON `enable_support`), and `supports_status`.

| Source | Populated by | Priority |
|---|---|---|
| `profiles/from-printer/` | `tools/extract_profiles_from_printer.py` | Highest (physics-validated) |
| `profiles/user/` | Operator manually | Middle |
| `profiles/snapmaker-stock/` | `tools/fetch_snapmaker_profiles.py` | Lowest (universal baseline) |

If `list_profiles()` returns empty, workflow emits `{stage:"setup_required", kind:"no_profiles"}`. Surface the event's `message` verbatim — point user at the named scripts.

## Slicer warnings (v1.5.0)

Workflow emits `{stage:"warning", kind:"slicer_warning", messages:[...], count:N}` after the commit slice when Orca flags geometric concerns. Surface every entry in `messages` verbatim BEFORE the user trusts the preview. Don't filter, don't paraphrase, don't auto-block — these are advisory.

## Safety gates

- **Upload-only is default.** Never upload+start without bed-clear via `u1_print_start_gate.py`.
- **Start = physical action.** Never run from cron / chain / automation. In-the-moment operator confirmation only.
- **If anything is unknown** — printer state, tool, material, slicer metadata, bed visibility — fail closed.

## References

Historical session notes + reverse-engineering captures (not load-bearing for the current flow — skim only if a question references something earlier than v1.5.0):

- `references/snapmaker-u1-question-flow.md`
- `references/snapmaker-u1-safety-gates.md`
- `references/snapmaker-u1-orient-rotate-and-slice-review-2026-06.md`
- `references/profile-tool-fidelity-v1.4.2.md` — T0→T1 rewriter + profile_path resolution
- `references/deploy-to-runtime.md`
