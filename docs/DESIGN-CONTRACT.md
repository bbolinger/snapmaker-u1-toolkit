# Design Contract — Snapmaker U1 Toolkit + Skill

**Read this FIRST every session before touching the skill, the workflow, or the start gate.** This is the single source of truth for what the system MUST do. The SKILL.md, scripts, and tests implement these rules — they don't override them. When in doubt, this file wins.

---

## Why this exists

Design intent for this toolkit had been drifting between AI-assisted development sessions, with the same anti-patterns being re-corrected repeatedly because the intent lived only in the operator's head + scattered audit comments. This file pins it down.

---

## The goal

A **Hermes agent driven by a quantized local LLM (target: `gemma-4-26b-64k`, 20 GB VRAM)**, talking to the operator over **Telegram**, can walk a `.stl` file all the way from "slice this" to a printing U1 — with the only human-required action being one yes/no on a bed-clear photo.

This is not a "show off Claude's intelligence" demo. The skill MUST be simple enough that a 26B local model executes it cleanly. If a step is too subtle for gemma-26b, it is too subtle for the skill — simplify the script, not the model.

---

## The three contracts

### Operator contract (only the human decides)

The operator is the final gate on three things — and ONLY these:
1. **Tool / material / preset / orient / supports** — answers to need_input prompts. Never inferred, never defaulted.
2. **Upload mode** — upload-only vs. upload+start.
3. **Bed-clear visual approval** — the Stage 1 photo. Pass/fail is a human verdict on a real bed image. Brightness checks, mock detection, etc., are inputs to the human's decision, NOT substitutes for it.

Everything else is the agent's or the script's job.

### Skill contract (what the SKILL.md MUST be)

- **A procedural runbook**, not a tutorial. Each MUST is a single rule a 26B model can follow in one read.
- **Tool-call lead-ins** for every external action: skill says "run this command," agent runs it, agent reads the JSON events, agent surfaces the named fields. No paraphrasing, no inferring.
- **Refusal conditions are explicit and exhaustive.** Every "do not advance" point names the missing field + the fixed refusal message.
- **No new rules without a named live failure.** If a MUST exists, the commit that added it should cite the specific run where the absence caused a fabrication. Otherwise it's clutter the 26B model will skip.
- **Never edited live.** Edit in workspace + `deploy_to_runtime.sh`. Live edits have silently deleted whole sections twice.

### Agent contract (what the LLM MUST do)

The agent is dumb-by-design — it does what the skill says, no more. Specifically:

1. **Capture every JSON event the workflow emits.** If output appears truncated, re-run with stdout redirected to a file. Do NOT compose downstream calls from chat memory.
2. **Surface events VERBATIM.** The `human_summary`, `slicer_warning.messages`, `blockers` fields are quoted, not summarized.
3. **Never invent state.** If a field isn't in this turn's captured events, the agent doesn't know it. Refuse with a fixed message + stop.
4. **Operator answers are slugs, not paraphrases.** When the operator types "the 0.2 fast one", look it up via `u1_profile_picker.py --json` — never guess.
5. **One question per turn.** Stacking questions = agent guesses for the rest.
6. **Photo first, verdict second.** Stage 1's `snapshot.path` goes in the reply text BEFORE any narrative — the operator needs the image.

---

## End-to-end flow (state machine)

```
                   ┌──────────────────┐
                   │   slice request  │ ← operator: "slice <stl>"
                   └────────┬─────────┘
                            ▼
                   ┌──────────────────┐
                   │     ANALYSIS     │ ← workflow: triage + render source + render auto
                   └────────┬─────────┘
                            ▼
                   ┌──────────────────┐
                   │     DECISION     │ ← workflow emits need_input × 4 (orient, tool, preset, supports)
                   └────────┬─────────┘                                  agent asks operator, one per turn
                            ▼
                   ┌──────────────────┐
                   │  UPLOAD MODE Q   │ ← agent asks: upload-only or upload+start?
                   └────────┬─────────┘
                            ▼
                   ┌──────────────────┐
                   │      COMMIT      │ ← workflow: slice + render preview + summary + upload
                   └────────┬─────────┘
                            │
                            │ collision? ──→ need_input "filename_collision" ──→ operator picks
                            │                                                          │
                            │                ┌─────────────────────────────────────────┘
                            │                ▼
                            │       re-invoke with --out-dir <prev> --on-collision <answer>
                            │       workflow emits slice_reused, jumps to upload
                            ▼
                   ┌──────────────────┐
                   │  READINESS CARD  │ ← workflow event — agent CANNOT advance to Stage 1 without it
                   └────────┬─────────┘
                            ▼
                   ┌──────────────────┐
                   │     STAGE 1      │ ← gate captures bed photo + writes approval token
                   │                  │   agent: photo path bare → operator visually approves
                   └────────┬─────────┘
                            ▼
                   ┌──────────────────┐
                   │     STAGE 2      │ ← gate validates token + sanity capture + dispatches /printer/print/start
                   └──────────────────┘
```

**Invariants:**
- ANALYSIS runs every workflow invocation (idempotent).
- DECISION events are independent — agent asks them across separate turns.
- COMMIT only runs with `--yes` (operator has answered all four).
- Collision resume short-circuits the slice via `slice_res.json` cache + `--out-dir` re-use.
- Stage 2 requires BOTH a valid token (5-min TTL) AND a fresh sanity capture.

---

## Acceptance criteria — "ships on gemma-26b via Telegram"

The skill is shippable when **all** of these pass in one continuous session, with gemma-4-26b-64k as the agent and Telegram as the channel:

1. **Walkthrough**: agent walks operator through all 4 need_input prompts WITHOUT stacking, WITHOUT picking defaults, WITHOUT paraphrasing options.
2. **Slice + preview**: agent surfaces the preview image bare + the slice summary's footprint dimensions correctly (mm × mm, not raw bbox).
3. **Collision (if hit)**: agent re-invokes with `--out-dir` + `--on-collision`; the next run emits `slice_reused` (NOT `slicing`).
4. **Readiness card present**: agent captures the `readiness_card` event from the COMMIT phase's stdout. If not captured, agent refuses to proceed.
5. **Stage 1 photo first**: agent emits `snapshot.path` as a bare path in the reply BEFORE any narrative or verdict.
6. **Stage 1 refusal logic**: agent refuses ONLY on `is_mock=true` OR `(brightness_check='measured' AND ok=false)`. Deferred brightness proceeds.
7. **Stage 2 dispatch**: with explicit operator yes + valid token, gate starts the print.
8. **Cancel before bed heat**: operator cancels via U1 touchscreen — NOT part of the skill, but is the test stop signal.

**Acceptance is binary**: any single failure = fail. The skill is not shipped until eight-of-eight pass on gemma-26b. Higher-IQ models passing doesn't count.

---

## Non-negotiables (the rules that exist because we've been burned)

- **Live-skill-edits forbidden.** Workspace + `deploy_to_runtime.sh` only. v1.4.4 + v1.4.6 lost whole sections this way.
- **No `--no-verify` git commits, no force-pushes to main.** Public repo discipline.
- **Untested release candidates → `vX.Y.Z-dev` branch.** Never directly to main on public repos until live-verified.
- **Photo is operator-gated.** No script may pass/fail the bed clearance check. The script supplies evidence; the human supplies the verdict.
- **No custom slicing python.** All slicing goes through `u1_slice_workflow.py`. `subprocess.run(['orca-slicer', ...])` bypasses the T0→T<chosen> rewriter, thumbnail injector, and event schema.
- **No state-from-chat-memory.** Every claim is grounded in a JSON event captured this turn. If you can't quote the event, you don't know.

---

## When to update this file

Update this file when:
- A new acceptance criterion is added (e.g., new model target, new channel)
- A non-negotiable changes (rare — these are learned-the-hard-way invariants)
- The state machine changes shape (e.g., a new safety stage is added)

Do NOT update this file for:
- Skill prompt tweaks (those live in SKILL.md)
- Workflow implementation changes (those live in scripts/)
- Test refactors

If you find yourself wanting to add a fourth contract or a fifth stage, you're solving a different problem than the one this toolkit was built for. Stop and ask the maintainer.

---

## Drift checks (run mentally at session start)

Before touching any code or skill rule, answer:

1. **What's the current shippable target?** (Today: gemma-4-26b-64k via Telegram. NOT Claude. NOT ChatGPT.)
2. **Which acceptance criterion is failing right now?** Name the number. If you can't, you're not ready to edit.
3. **Is the fix a script change, a skill rule, or both?** Default: prefer script. Skill rules cost the 26B model attention budget every turn.
4. **Have you read the existing SKILL.md MUST you're about to add/edit?** Re-state it back to yourself before changing it.

If any of these is "I don't know," stop and re-read this file.
