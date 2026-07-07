# Snapmaker U1 toolkit — procedural rules (stable tier)

These rules sit in Hermes' **stable+context tier** — the prompt-assembly
layer that survives compression on long multi-turn conversations,
regardless of model size or conversation length. Skill text loads into
the *volatile* tier and gets summarized once context pressure exceeds the
threshold; stable-tier rules don't. This is the architecture pattern
Hermes (and other agent runtimes) ship specifically for
procedural rules that must remain unconditionally present — not a
small-LLM workaround. Large agents need the same scaffolding for the
same reasons; the toolkit author has been caught violating his own
stable-tier rules too.

The rich SKILL.md procedural runbook still applies. These rules are the
backbone that cannot get summarized away.

## Rule 1 — `need_input` events ALWAYS route via `next_command`

Every `need_input` event the workflow emits has options, and EVERY option
carries a `next_command` field with the literal bash invocation. When the
operator answers a `need_input` prompt:

1. Find the option whose `value` (or `label`) matches the operator's answer.
2. Tool-call that option's `next_command` field VERBATIM.

This applies to: `orient`, `tool`, `preset`, `supports`, `upload`, AND
`filename_collision`. The workflow tracks state via CLI flags it pre-wrote
into each `next_command`. ONE exception by design: the bed-clear start
question carries no command at all — see Rule 8.

## Rule 2 — `next_action_required` events ALWAYS trigger a tool call

When a `next_action_required` event appears in the workflow's output, your
VERY NEXT action is a tool call to its `command` field VERBATIM. No
operator question, no narrative preamble, no confirmation prompt — the
workflow has decided this is the next step. Tool-call, then narrate.

## Rule 3 — Operator's collision answer is NOT bed-clear approval

Receiving "overwrite", "rename", or "cancel" from the operator on a
`filename_collision` prompt is **ONLY** the answer to the filename
collision question. It is NOT:

- A green light to start Stage 1
- Approval that the bed is clear
- Confirmation to dispatch the print

After a collision answer, you tool-call the matching option's
`next_command` and wait for the workflow's next emitted events
(`slice_reused`, `readiness_card`, then `next_action_required`).

## Rule 4 — You never invoke the print-start gate. Ever.

`u1_print_start_gate.py` is not yours to run, compose, or recover. The
workflow launches it internally after the operator's confirmed yes. If any
turn seems to require a gate invocation from you, that turn is wrong —
surface what the workflow said and wait. If you have lost this turn's
events, re-run the workflow with `--request-id <request_id>` to recover
them from disk; never reconstruct commands from chat memory.

## Rule 5 — Bed-clear is its own decision, made on a fresh photo

The workflow captures a real bed photo at decision time. Surface the photo
path bare (with the plate preview and review doc when emitted), then ask
the operator the exact yes/no question from the event. That is a NEW
question with NEW operator approval — never collapsed into any earlier
answer, never assumed from context.

## Rule 6 — On resume, re-run the workflow on the SAME STL path; never re-extract

If you've lost context mid-flow, re-run `u1_slice_workflow.py <stl-path> --json-events` (add `--request-id <id>` if you have it). Workflow finds the in-flight request by content hash and emits `request_resumed`. Never re-extract the zip or start a new slice from scratch.

## Rule 7 — Every approval question includes the `request_id`

Workflow events carry `request_id`. Include it verbatim in every approval ask: "Bed clear and start request `u1_2026_...`? (yes/no)". Operator's "yes" then routes to that specific request, not to a guess.

## Rule 8 — The start transition is model-free. You hold no start command.

At `bed_clear_start`, surface the artifacts and the exact question, then
wait. On the operator's YES you issue NO tool call — the gateway redeems
the YES directly and the printer countdown message arrives on its own. If
no countdown appears, say the start was not initiated; do not retry or
compose anything. On any error or refusal from a relayed command: surface
its message verbatim and stop. The one command you may run unprompted is
`u1_kit_workflow.py --grace-cancel` when the operator's CANCEL goes
unacknowledged — it can only ever stop a print. Never call Moonraker
directly.

## Rule 9 — Multi-part kits: follow `kit_detected`, relay the form verbatim

A zip with several STLs is a kit. `u1_slice_workflow.py` emits `kit_detected` — tool-call its `command` (runs `u1_kit_workflow.py`). The kit form arrives as one `need_input` (`key: kit_form`): show its `form`, then relay the operator's reply VERBATIM into `--form-answers '<line>'` (the script parses it; you don't). Only **plate 1** is camera-gated; plates 2..N are uploaded for the operator to start from the Snapmaker app. Detail: `references/multipart-kits.md`.
