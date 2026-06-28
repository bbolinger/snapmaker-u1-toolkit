# Handling `can_start()` refusals

Stage 2 of `u1_print_start_gate.py` routes through `can_start()` before any printer-affecting action (v2.0 Phase 3b). When `can_start()` refuses, the gate returns:

```json
{"started": false, "reason": "<verbatim refusal reason>", ...}
```

## What to do

1. **Surface the `reason` field verbatim** to the operator. Do not paraphrase, do not improvise, do not "explain in your own words." The reason is already short and operator-readable.
2. **Re-run the workflow with the same `--request-id`** to recover the on-disk state and re-emit the readiness card with the updated plan.
3. **Re-ask the operator** to approve on current information.

```bash
python3 /opt/data/scripts/u1_slice_workflow.py <stl-path> --json-events --request-id u1_2026_...
```

## The reject branches

`can_start()` refuses in these cases (the `reason` field will indicate which):

| `reason` mentions | Cause |
|---|---|
| "request-id" | Stage 2 was invoked without `--request-id`. Re-run with the ID baked into `start_gate_stage1_command`. |
| "plan changed since operator reviewed" | A plan-affecting field (orient, tool, material, profile, supports, gcode_hash, nozzle) changed between the operator's review and Stage 2. Re-run the workflow with `--request-id` so the operator re-approves on the new plan. |
| "gcode regenerated since operator reviewed" | A re-slice produced a different gcode_hash. Same fix: re-run with `--request-id`. |
| "bed-clear photo required but not captured" | Stage 1 didn't produce a usable real photo. Re-run Stage 1 once the camera issue is resolved. |
| "no readiness_card emitted yet" | The agent invoked Stage 2 before the workflow emitted a `readiness_card` event. Never happens in normal flow; if it does, re-run the full workflow. |

## What NOT to do

- **Don't** route around the gate (call Moonraker directly, invent a magic phrase, etc.).
- **Don't** assemble a Stage-2 command from chat memory. The workflow's `start_gate_stage1_command` field already has the right `--request-id`. Add `--bed-clear start --approval-token <token>` and run.
- **Don't** retry the same Stage-2 invocation hoping it succeeds the second time. If `can_start()` refused, the refusal reason is durable — change the underlying state (re-run workflow → re-approve) before retrying.
