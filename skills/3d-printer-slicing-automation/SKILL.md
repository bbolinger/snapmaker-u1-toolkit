---
name: 3d-printer-slicing-automation
description: "REQUIRED for ANY .stl / .3mf / .zip 3D-model attachment. FIRST tool call MUST be: 'python3 /opt/data/scripts/u1_slice_workflow.py <attachment-path> --json-events'. The workflow handles zip inspection + slicing. Do NOT extract zips or run orca-slicer yourself."
version: 2.1.0
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

### Pre-flight acknowledgement (FIRST user-visible reply, after the first tool call)

Run the analysis tool call FIRST (Step 1 below), then include this acknowledgement at the top of your first text response. The five points must each appear explicitly. Generic "I'll help you slice this" sentences do NOT count.

Order: tool call → acknowledgement → analysis-event surface (orientation question). Don't speak before the tool call; don't skip the acknowledgement after it.

> "Acknowledging the Snapmaker U1 skill rules: I'll (1) run `u1_slice_workflow.py --json-events` FIRST before asking anything; (2) ask 6 staged questions one per user turn; (3) never invent procedures, magic phrases, file paths, or printer state — every action I claim must come from an actual tool call; (4) attach files by writing absolute paths bare in my reply text (Hermes auto-attaches); (5) call the named scripts (`u1_print_start_gate.py`, `u1_camera.py`) directly when the skill names them, never write my own status checks or magic confirmation phrases."

Do the acknowledgement once per conversation, not every turn. Skipping it is correlated 1:1 with sessions where the agent later fabricates state, invents protocols, and stacks questions (caught live 2026-06-25).

### How this works (v1.5.2 — `next_command` flow)

The workflow IS the source of truth for what command to run next. You never construct slicing commands from chat memory. Two event-types tell you what to run:

| Event | What you do |
|---|---|
| `need_input` with options | Surface the prompt + options to the operator. When they answer, find the matching option, tool-call its `next_command` field VERBATIM. |
| `next_action_required` | Tool-call its `command` field VERBATIM immediately. No operator question, no narrative preamble. The workflow has decided this is the next step. |

For both: copy the string, do not edit, do not paraphrase, do not add or remove flags. The workflow handles state.

The workflow walks the operator through five prompts one at a time: **orient → tool → preset → supports → upload**. Each turn = one tool call. The workflow tracks state via the CLI flags it pre-wrote into each next_command.

### YOU MUST

**Step 1 — Start.** When the user gives you an STL/3MF, your next tool call IS:
```bash
python3 /opt/data/scripts/u1_slice_workflow.py <model> --json-events
```
If the messaging platform rejects raw `.stl` but accepts `.zip` (single STL inside), extract the STL from the ZIP first, then run the workflow on the extracted model path. For zip archives with **multiple STLs (a kit)**, pass the ZIP DIRECTLY to `u1_slice_workflow.py` — the workflow emits `kit_detected` whose `command` field runs `u1_kit_workflow.py`. When you see `kit_detected`, tool-call that `command` verbatim via terminal. The kit workflow then drives a **3-turn staged Q&A**: parts → tool → confirm. Each turn emits a `need_input` event with options carrying baked-in `next_command` strings — same per-field pattern as Step 2 below (`references/multipart-kits.md`).

Optionally `--no-live-material` if Moonraker isn't reachable. Do not ask the user anything before this runs.

**Step 2 — For each `need_input` event the workflow emits:**

1. Surface the event's `prompt` field as a bold header line, THEN each option's `label` numbered 1, 2, 3 on their own lines. NOTHING ELSE. No paraphrasing, no commentary, no defaults. If the event has a `note` field, surface it verbatim AFTER the options.
2. **For the orient prompt**: ALSO surface both `render` event paths (`source_as_authored` + `auto_oriented` if present) bare in your reply text BEFORE the prompt, so the images attach. Paths must NOT be inside backticks or fenced code blocks (gateway skips those).
3. Wait for the operator's answer. They will reply with a number, a slug (`asauthored`, `T1`, `no_supports`), or a paraphrase (`the as-authored one`, `0.20mm fast`).
4. Find the option whose `value` or `label` matches their answer. For paraphrased preset names that don't match any option's `value`, run `python3 /opt/data/scripts/u1_profile_picker.py --nozzle 0.4 --json` to look up the right slug, then re-match.
5. **Tool-call the matched option's `next_command` field verbatim.** No flag additions. No flag re-orderings. No splitting. The workflow knows what to do — it wrote this command. For commands likely to take >60s (the commit step that triggers a real slice), set `background: true` + `notify_on_complete: true`.

That's the entire prompt loop. Repeat until you see a `readiness_card` event — that means the COMMIT phase ran and the agent should proceed to Step 3.

**Cancel option.** Some options have `next_command: null` (e.g. the Upload? prompt's "Cancel" option). When the operator picks one of those, do not tool-call anything. Tell the user it was cancelled and stop.

**Filename collision.** If the workflow emits a `need_input` event with `key: "filename_collision"`, surface its options and use the matching option's `next_command` like any other prompt. The workflow already populated the `--out-dir` AND `--on-collision` flags into the next_command, so you don't have to think about it — just copy and tool-call. A `slice_reused` event in the re-run means the cached slice was reused (no wasted re-slicing).

**Step 3 — Surface the preview + readiness card.** After the COMMIT slice finishes (you'll see a `summary` event and a `readiness_card` event), surface the post-slice review:

- From the `render` event with `kind:"preview"`, emit its `image` field bare in your reply (auto-attaches).
- From the `review_doc` event (v2.2), emit its `path` bare in your reply so the file attaches — it's the operator's human-readable review of exactly what will print. Informational: do not block on it, do not summarize it in place of attaching it.
- If there's a `slicer_warning` event, surface every entry in its `messages` array verbatim BEFORE the user trusts the preview.
- From the `summary` event, present `first_layer_width_mm` × `first_layer_depth_mm` for the footprint — NEVER render `first_layer_bbox` as "X by Y to X by Y" (that's the raw xmin/xmax/ymin/ymax tuple, not dimensions).

**Reading the `uploaded` event truthfully.** v1.5.1 audit (2026-06-26) split the upload result into granular fields. Read them, don't infer from `returncode` alone:

| Field | Meaning |
|---|---|
| `moonraker_upload_ok` | The Moonraker upload itself succeeded (file bytes accepted) |
| `remote_metadata_ok` | The printer can serve metadata for the uploaded file — independent confirmation that it lives in storage |
| `post_upload_validation_ok` | No blockers in post-upload state (warnings OK) |
| `post_upload_warnings` | Non-blocking state observations (e.g. terminal `cancelled` state that's otherwise idle) |
| `post_upload_blockers` | Real problems with post-upload state (e.g. printer became active) |
| `human_summary` | Quote verbatim. NEVER substitute "no file reached the U1" if `moonraker_upload_ok` AND `remote_metadata_ok` are both true |

Specifically: **`returncode=3` means "upload succeeded but post-upload state has warnings/blockers"**, NOT "file failed to upload". The file IS on the printer. Surface the blockers, but do NOT claim the upload failed.

After `uploaded`, the workflow emits a `readiness_card` event. Use it to compose your pre-start narrative:

- `orient_supports_tier` + `orient_overhang_area_pct` describe the CHOSEN orientation's overhang risk (not the abstract recommended one). Always mention this if the tier is `heavy` or `very heavy`.
- `warning_if_overhang_risky` is pre-filled when `supports=no_supports` AND tier is heavy/very heavy. If present, surface it verbatim before the start question.
- `next_step_if_starting` is the exact stage-1 start-gate command shape — use as your Step 9 next tool call.

**Step 4 — Stage 1 (only if operator picked "Upload + start gate").**

After the `readiness_card` event, the workflow emits a `next_action_required` event. Its `command` field is the Stage-1 invocation. **Tool-call it BEFORE writing any narrative.** This is the same rule as Step 2: workflow says run X, you run X.

What you MUST NOT do here (gemma4-26b failed this in harness run 6/7):
- Surface the SLICE preview path and ask "bed clear?" — that preview is the gcode visualization, NOT a bed photo. Asking "bed clear?" without a real bed photo is anti-fab pattern #4 (state from chat memory) + #6 (verification fabrication).
- Wait for the operator to "approve" before running Stage 1 — they already committed at the Upload? prompt. The Stage 1 photo capture is mechanical: LED on, camera grab, ~5 sec. The OPERATOR APPROVAL HAPPENS AFTER, when you surface the real bed photo from Stage 1's output.

So: see `next_action_required` → tool-call `command` → workflow returns with `snapshot.path` (the real bed photo) → surface that path bare → ask the operator "Bed clear and you want to start request `<request_id>`? (yes/no)" → wait for their answer. The `<request_id>` comes from the `readiness_card` event's `request_id` field (or the `request_created`/`request_resumed` event upstream). Including it makes the approval unambiguous and auditable — see "Approval phrasing" below.

If the operator picked "Upload only" instead, the workflow emits `{stage: "complete"}` — surface the upload status, tell the operator the workflow is done, stop.

**HARD RULE — no readiness_card, no Stage 1.** If you do NOT have a `readiness_card` event in the JSON output you captured this turn, you CANNOT advance to Stage 1. Do not "attempt" Stage 1 by guessing the printer storage filename, the tool index, the material, or the command shape. The readiness_card is not optional context — it carries the exact command (with the right basename, the right `--intended-tool`, the right `--requested-material`) that Stage 1 expects. If your output appears truncated, re-run the workflow command and redirect stdout to a file (`> /tmp/u1_events.jsonl`), then read the file — do NOT compose a Stage 1 invocation from chat memory. This is anti-fabrication pattern #4 (state from chat memory) and pattern #6 (verification fabrication). Refuse with "I don't have the readiness_card from the uploaded run — I cannot advance to Stage 1 without it. Re-running the workflow with output captured to a file." and STOP.

**Stage 1 — readiness + photo + token (mandatory).** Run the `start_gate_stage1_command` from the readiness card. It NEVER starts the print. It returns `blockers`, `snapshot` (with `path`), `approval_token` + `approval_ttl_seconds` (30 min), and `next_step`. Surface `snapshot.path` BARE in your reply text first (auto-attaches the photo), then ask the operator: *"Review the attached photo. Bed clear and you want to start request `<request_id>`? (yes/no)"*. Default = no. Field-level detail + the only two refusal conditions: `references/snapmaker-u1-safety-gates.md`.

**Stage 2 — actual start.** Only after a fresh "yes" AND a valid token. Run `next_step` (or equivalently the Stage-1 base + `--bed-clear start --approval-token <token>`). On any refusal, surface `reason` verbatim. Token TTL, expired-token recovery, can_start() routing, and post-success reporting: `references/snapmaker-u1-safety-gates.md` and `references/can-start-refusal-handling.md`.

DO NOT skip Stage 1. DO NOT invent a magic phrase. DO NOT pass `--bed-clear start` without the token AND the operator's explicit yes. Default = cancel.

**Kit start-boundary guard (2026-07-01 incident).** For multi-part kit flows, the confirm/print-plan choice of `start` is **not** bed-clear approval, even if the kit workflow has already captured a bed photo and issued a token. If a kit `start` action emits `need_input` with `key`/`need: "bed_clear_start"`, surface the bed photo path if present, then ask that prompt and wait for the operator's yes/no. If `bed_snapshot_path` is null, do **not** fabricate or re-run a camera capture; the prompt intentionally refers to the bed photo already surfaced with the print plan, so ask the prompt only. This event may not have normal `options`; when the operator answers yes, route via its `next_command_on_yes` verbatim, and when they answer no/cancel, stop without tool-calling. Only the follow-up yes for the same `request_id` may route to the next command that emits/runs Stage 2. If any kit confirm/refreshed-confirm event exposes a direct `stage2_command` or a `next_action_required` containing `--bed-clear start` before a dedicated `bed_clear_start` yes/no turn, fail closed and tell the operator the workflow is trying to collapse the separate bed-clear approval boundary. Do not run that Stage 2 command. After the Stage 2 `u1_print_start_gate.py ... --bed-clear start --approval-token ...` call returns `started: true`, report the started request and snapshot path; do **not** ask for another bed-clear confirmation because the dedicated `bed_clear_start` yes/no turn was already the final operator approval.

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
3. **Confusing host filesystem with printer storage.** The `uploaded` event distinguishes `host_path` (Hermes-host local disk, where the gcode physically is) from any printer-side reference. Never say "file is in printer storage" when only `host_path` is present in the event — that means the file is on the Hermes container's disk, not the U1.
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
