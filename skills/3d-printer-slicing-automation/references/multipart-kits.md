# Multi-part kits (v2.1.0)

A Printables "kit" is a zip of several STLs meant to print together. When
`u1_slice_workflow.py` sees a zip with more than one STL it emits
`kit_detected` with a `command` — tool-call that command verbatim. It runs
`u1_kit_workflow.py`, which drives the whole kit. **Two operator triggers, same
as a single print:**

## Trigger 1 — the form
The kit workflow emits a `need_input` event with `key: "kit_form"` and a `form`
field (a numbered text block: parts, orient, tool, material, profile, supports,
action).

- **Show the operator the `form` text.**
- **Relay their reply VERBATIM** into `--form-answers '<their line>'` on the
  command in `next_command`. Do NOT interpret, reorder, or normalize it — the
  script parses it. One quoted line.
- **Form mode (v2.2, buttons):** when the `kit_form` event carries
  `form_schema` + `form_id`, pass the schema to the form tool. When the
  tool result says the answers file was written, tool-call the event's
  `next_command` (it carries `--form-answers-from=<form_id>`) VERBATIM.
  Never read, restate, or reconstruct the answers — you never had them.
- **Form timeout/failure fallback:** if the form tool returns `_timeout`,
  `cancelled`, or an error, fall back to the STAGED text flow — re-show the
  numbered `form` text and collect ONE `--form-answers` line, exactly as in
  text mode. NEVER dump every field as separate free-text questions in one
  message; the staged/form structure exists so the operator is not asked
  for word-vomit.
- The operator answers all fields at once in any order, e.g.:
  `parts 1,3 | auto | T0 | PLA | profile 2 | no-supports | start`
  - `parts`: `all`, or `1,3,5`, or a range `1-4`
  - `orient`: `as-authored` (default) or `auto` (auto-rotate; reorients each part)
  - `tool`: `T0`..`T3`; `material`: from the offered list; `profile N` by number
  - `supports`: `supports` | `no-supports`
  - `action`: `start` (gate plate 1) | `upload-only`

If the answer doesn't validate, the workflow emits `form_rejected` with
`errors` + the form again — show the errors and ask once more. Never guess.

After a valid answer the workflow slices all selected parts onto plate(s),
uploads them, and emits `kit_readiness_card` (preceded by a `review_doc`
event — attach its `path` so the operator can read the flight plan before
answering). The card's `parsed_echo`
("I read: …") is what the operator confirms — surface it.

## Trigger 2 — the photo gate (plate 1 only)
The kit workflow gates **only plate 1** through a **two-turn bed-clear
confirmation + nonce-bound Stage 2**:

1. Operator picks `start` in the readiness card options → workflow emits
   `need_input` with `key: "bed_clear_start"` + a fresh bed photo.
2. Surface the photo + prompt VERBATIM. Wait for the operator's `yes` or `no`.
3. On `yes`: tool-call the `next_command_on_yes` (which re-invokes the
   workflow with `--bed-clear-confirmed`). The workflow validates the pending
   confirmation, mints a single-use `stage2_approval_nonce`, and emits the
   nonce-bound Stage 2 command.
4. Run the emitted Stage 2 command. The gate consumes the nonce and starts
   the print.

Notes on the `start_gate_stage1_command` field also present in the
readiness card: it's the Stage 1 camera-refresh command. **It does not
authorize Stage 2.** For kit requests the gate refuses any Stage 2
invocation that arrives without a nonce, so the legacy
"run Stage 1 → get token → run Stage 2 with token" one-liner path is
architecturally closed for kits. Kit Stage 2 always requires the
staged bed_clear_start confirmation.

The legacy `--form-answers` / `--form-answers-json` one-liner modes can
still commit + slice + upload + emit the readiness card in a single
CLI call. **They do NOT authorize kit Stage 2 on their own.** Kit
print start still requires the staged bed_clear_start yes/no and
the nonce-bound Stage 2 command. Attempting Stage 2 for a kit request
without a persisted nonce returns
`gate refuses: Kit request requires the staged bed_clear_start
confirmation before Stage 2`.

## Plates 2..N
If the kit needed multiple plates, they are **already uploaded** to the printer.
After plate 1 finishes, tell the operator to **start plates 2..N from the
Snapmaker app** (the touchscreen/app print menu). The toolkit does not gate
those — but the watchdog still sends first-/last-layer photos for every plate
regardless of how it was started.

## Notes
- `oversized_part_ids` in `kit_ingested` / a `kit_slice_failed` event means a
  part can't fit the bed even rotated — tell the operator to deselect it
  (`parts …` without that number) and re-answer the form.
- Recovery: re-sending the same zip resumes the same request; or pass
  `--request-id <id>` explicitly (the workflow's `next_command` already does).
