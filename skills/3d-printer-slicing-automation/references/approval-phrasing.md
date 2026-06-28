# Operator approval phrasing — the `request_id` rule

Every operator-facing approval question must include the `request_id` verbatim. This makes the operator's "yes" unambiguous about WHICH request is being approved (matters when context resets, when a prior approval is still being asked about, or when multiple requests have been discussed in the same conversation).

The `request_id` is in every workflow event payload (`request_created`, `request_resumed`, `readiness_card`, `next_action_required`). Use the one most recently emitted in the current turn.

## Templates

| Boundary | Question to ask |
|---|---|
| After `readiness_card`, before Stage 1 | "Bed clear and you want to start request `u1_2026_0627_abc123`? (yes/no)" |
| After Stage 1's photo, before Stage 2 | "Review the attached photo. Bed clear and you want to start request `u1_2026_0627_abc123`? (yes/no)" |
| If the operator wants to abort | "Cancel request `u1_2026_0627_abc123`?" |

## What NOT to do

- **Don't** ask "should I start it?" / "approve?" / "yes/no?" without the request_id.
- **Don't** ask the operator to remember or type the request_id back — you have it in the event payload, surface it.
- **Don't** route the operator's "yes" to "whatever was most recent" without the request_id pin. If context was reset between the question and the answer, the most-recent request might not be the one the operator is approving.
