# Snapmaker U1 toolkit — procedural rules (stable tier)

These rules sit in Hermes' **stable+context tier** — the prompt-assembly
layer that survives compression on long multi-turn conversations,
regardless of model size or conversation length. Skill text loads into
the *volatile* tier and gets summarized once context pressure exceeds the
threshold; stable-tier rules don't. This is the architecture pattern
Hermes (and Claude, and other agent runtimes) ship specifically for
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
into each `next_command`. There are no exceptions.

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

## Rule 4 — Stage 1 only after `readiness_card`

You may not issue a `u1_print_start_gate.py` tool call until a
`readiness_card` event has been received in the current turn's captured
output AND a `next_action_required` event has named the Stage 1 command.

If you have no `readiness_card` in this turn, re-run the workflow with
`--request-id <request_id>` (preferred, v2.0+) or `--out-dir <prior_out_dir>`
(legacy) to recover its events from disk. Never compose a Stage-1
invocation from chat memory — the printer storage filename, intended
tool, and material flags must come from the workflow's emitted
`start_gate_stage1_command`, not from anywhere else.

## Rule 5 — Bed-clear is a SEPARATE turn from Stage 1's photo capture

Stage 1 (`u1_print_start_gate.py`) captures a real bed photo and writes
an approval token. Surface the photo path bare in your reply, then ask
the operator: "Bed clear and you want to start? (yes/no)".

That is a NEW question, with NEW operator approval, separate from the
collision answer two turns earlier. Do not collapse them.

## When `--out-dir` is in play, recovery does NOT apply

`--out-dir` is a legacy escape hatch (tests, direct CLI use that pins a
specific output path). When the workflow is invoked with `--out-dir`,
request.json still gets written there for audit, but
`find_recent_request_for_model` only scans `<data_dir>/requests/` — so
recovery via content hash DOES NOT find requests stored under a custom
`--out-dir`. If you want resume-on-context-loss, omit `--out-dir` and
let the workflow use `requests/<request_id>/` by default.

## Rule 6 — Do NOT re-extract a file you already extracted

If a `request_resumed` or `request_created` event has fired in this
conversation for the operator's STL/3MF, the file is already extracted to
disk and the workflow knows about it. When you've lost context (no
recent workflow events in your captured output) and need to continue:

1. **Do NOT re-extract the zip / re-download / re-process the source file.**
2. **Do NOT start a new slicing run from scratch.**
3. Tool-call `u1_slice_workflow.py <stl-path> --json-events` — the workflow
   will find the in-flight request by content hash and emit a
   `request_resumed` event with the answers already collected. Continue
   from there.
4. If you have the `request_id` from earlier in the conversation, prefer
   `--request-id <id>` — it's faster and unambiguous.

Re-extracting wastes turns and creates phantom state. The workflow's
on-disk request object IS the source of truth across context resets.

## Rule 7 — Operator approval is keyed to a specific `request_id`

When the workflow emits `request_created` or `request_resumed`, the agent
remembers the `request_id` and uses it in every approval ask. Examples:

- "Approve start for request `u1_2026_0626_abc123`?"
- "Cancel request `u1_2026_0626_abc123`?"

Operator replies like "yes" / "approve" route to the SPECIFIC named
request, not to "whatever was most recent." This prevents stale
approvals from carrying over if the conversation was interrupted.
