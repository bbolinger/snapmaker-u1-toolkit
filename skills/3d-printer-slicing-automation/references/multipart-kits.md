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
- The operator answers all fields at once in any order, e.g.:
  `parts 1,3 | auto | T0 | PLA | profile 2 | no-supports | start`
  - `parts`: `all`, or `1,3,5`, or a range `1-4`
  - `orient`: `as-authored` (default) or `auto` (auto-rotate; reorients each part)
  - `tool`: `T0`..`T3`; `material`: from the offered list; `profile N` by number
  - `supports`: `supports` | `no-supports` | `overhangs`
  - `action`: `start` (gate plate 1) | `upload-only`

If the answer doesn't validate, the workflow emits `form_rejected` with
`errors` + the form again — show the errors and ask once more. Never guess.

After a valid answer the workflow slices all selected parts onto plate(s),
uploads them, and emits `kit_readiness_card`. The card's `parsed_echo`
("I read: …") is what the operator confirms — surface it.

## Trigger 2 — the photo gate (plate 1 only)
The kit workflow gates **only plate 1** through the normal Stage 1/2 camera
gate (`start_gate_stage1_command` in the readiness card). Run Stage 1, surface
the bed photo, get the operator's yes/no, then Stage 2 — exactly the single-STL
gate procedure. Nothing new here.

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
