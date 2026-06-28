# Event Contract

This document is the public contract for the JSON event streams the toolkit emits. Anyone building a frontend (Telegram bot, web UI, Discord, MCP server, …) should be able to wrap the workflow using this document alone — no source-reading required.

There are **two streams**, intentionally separate, each with its own vocabulary and purpose.

| Stream | File | Vocabulary | Purpose |
|---|---|---|---|
| **Workflow events** | `<out_dir>/events.jsonl` (per-request) + stdout when `--json-events` is set | `stage: "<name>"` | Chatty agent-facing stream. Says "what part of the workflow is happening right now." Tail-able. |
| **Audit log** | `requests/<request_id>/audit.jsonl` | `event: "<name>"` | Forensic record. Says "what discrete happened-thing got recorded." Append-only at the application level (`O_APPEND` + `flock`). |

The two streams overlap but are NOT redundant. Some workflow events have audit twins (e.g. `stage: "uploaded"` ↔ `event: "upload_completed"`). Some workflow events don't (e.g. `triage`, `render`, `history_hint` — observational only). Some audit events have no workflow twin (e.g. all the Stage 1/2 gate events — they fire in `u1_print_start_gate.py`, not the workflow). The twin-pair table at the bottom of this doc lists which is which.

The two vocabularies exist because the streams answer different questions:

- **`stage:`** answers "where are we in the workflow?" — present tense, often blocks awaiting input.
- **`event:`** answers "what was recorded?" — past tense, immutable, forensic.

Don't conflate them. A consumer that wants to render a live progress UI subscribes to `events.jsonl`. A consumer that wants to reconstruct what happened to a finished request reads `audit.jsonl`.

---

## Lifecycle cheat-sheet

A normal `--yes` upload+start run from a fresh STL emits events in this order. (Items in brackets are conditional / depend on operator answers + collisions.)

```text
WORKFLOW STREAM                            AUDIT STREAM
==========================================================================
request_created                            request_created
   triage
   [render × 1-2]
   [orient_analysis]
   [history_hint]
   [need_input × 0-5]                      (no audit twins for need_input)
   [awaiting_input — only without --yes]
COMMIT (only when --yes / --upload-only):
   [supports_override]
   [slice_reused — collision short-circuit]
   slicing                                 slicing_completed
   [warning]
   summary
   uploaded                                upload_completed
   readiness_card                          readiness_card_emitted
   next_action_required (Stage 1 cmd)
                                           — agent then runs u1_print_start_gate.py —
                                           stage1_photo_captured / stage1_photo_failed
                                           — operator approves photo, agent runs Stage 2 —
                                           [stage2_token_invalid]
                                           [stage2_preflight_blocked]
                                           start_safety_check_passed | start_safety_check_failed
                                           [stage2_sanity_capture_failed]
                                           print_started  ← terminal success
```

**Resume path** (operator re-uploads same STL after agent lost context):

```text
request_resumed                            request_resumed
   [phase-aware skip — Phase 2 design]
   readiness_card_resumed                  readiness_card_replayed_from_resume
   next_action_required (Stage 1 cmd)
                                           ... same Stage 1 + Stage 2 events as above ...
```

**Upload-only path** (operator chose "Upload only" at the Upload? prompt):

```text
   ... (analysis + slice + uploaded same as above) ...
   readiness_card                          upload_only_complete
   complete                                ← terminal
```

---

## Workflow events — `stage:` vocabulary

Each entry: **event name**, payload fields, when it fires. Required fields marked plainly; optional fields are in italic. Field types: paths are absolute strings; numerics are JSON numbers; everything else is a string unless noted.

### Lifecycle

#### `stage: "request_created"`
First emitted on a fresh STL (no on-disk recovery match found).
- `request_id` — `u1_YYYY_MMDD_xxxxxx`
- `out_dir` — absolute path to `requests/<request_id>/`
- `note` — human-readable summary
- **Audit twin:** `event: "request_created"` (carries `model_file`, `model_hash`).

#### `stage: "request_resumed"`
Fires when content-hash recovery matched an in-flight request on disk (Phase 2 design — same STL bytes → same request_id).
- `request_id`, `out_dir`, `note`
- `resumed_from` — the prior `phase` value (e.g. `"awaiting_start_approval"`, `"sliced"`)
- **Audit twin:** `event: "request_resumed"` (also carries `request_revision`).

#### `stage: "readiness_card_resumed"`
Fires when phase-aware skip short-circuits past the slice/upload prompts because the prior run already reached `awaiting_start_approval`. Payload is a copy of the prior `readiness_card` event with `stage` renamed and `resumed_from_phase` added.
- All fields from the prior `readiness_card` (see below)
- `resumed_from_phase: "awaiting_start_approval"`
- **Audit twin:** `event: "readiness_card_replayed_from_resume"`.

### Analysis

These are observational — they describe what the workflow learned about the model. No audit twins; the analysis itself doesn't change print state.

#### `stage: "triage"`
First emitted after the source STL is parsed.
- `dims_mm` — `[x, y, z]` floats
- `tris` — integer triangle count
- `bbox_volume_cm3` — float

#### `stage: "render"`
Preview images. Multiple may fire per run (source view, auto-oriented view, slicer preview).
- `image` — absolute path to PNG
- `kind` — `"source_as_authored" | "auto_oriented" | "orient_analysis_v16" | "preview"`
- `overhang_area_pct` — float (when computed)
- `supports_tier` — `"clean" | "light" | "moderate" | "heavy" | "very heavy"`
- *`recommended_orient`, `recommendation_reason`, `note`, `error`* — when applicable (v1.6 pre-slice Orca analysis)

#### `stage: "orient_analysis"`
Comparison of the two orientations. Fires when both got rendered.
- `source_dims_mm`, `auto_dims_mm` — `[x, y, z]` floats
- `auto_down_vec` — `[x, y, z]` floats (Orca's down-vector recommendation)
- `source_overhang_area_pct`, `auto_overhang_area_pct` — floats
- `source_supports_tier`, `auto_supports_tier` — tier strings
- `recommended_orient` — `"asauthored" | "auto"`
- `recommendation_reason` — human-readable
- *`note`* — extra context
- *`error`* — if auto-orient itself failed and the workflow fell back to as-authored

#### `stage: "history_hint"`
Surfaces prior-print history for this tool/nozzle to inform the preset recommendation. Always emitted (may carry an empty `per_tool`).
- `last_used_print_settings_id` — string or `null`
- `installed` — boolean
- `tool_filtered` — boolean
- `per_tool` — object keyed by tool name
- *`message`* — context when no history exists

### Decision

#### `stage: "need_input"`
The workflow has reached a decision point and is exiting. Each event surfaces one question. Every option carries a fully-formed `next_command` the agent should tool-call verbatim when the operator picks it.
- `key` — `"orient" | "tool" | "preset" | "supports" | "upload" | "filename_collision"`
- `prompt` — short string surfaced to the operator
- `options` — list of `{label, value, next_command, recommended?, …}`
- *`note`* — context paragraph
- *`truncated`, `total_available`* — for preset (when the list was filtered)
- *`out_dir`, `resume_hint`* — for `filename_collision`

After every `need_input`, an `awaiting_input` event fires before the workflow exits, so consumers tailing the stream know the workflow process has terminated and the next turn requires a tool-call.

#### `stage: "awaiting_input"`
Sentinel that the workflow process has exited awaiting the operator's answer.
- `need` — the same key as the most recent `need_input` (e.g. `"orient"`) — or
- `note` — human-readable reason (e.g. `"no slice performed — re-invoke with --yes plus collected answers"`)

### Commit

(Only fires when `--yes` or `--upload-only` is present.)

#### `stage: "supports_override"`
Fires once when the supports answer materialized a temp profile with the `enable_support` flag overridden.
- `enable_support` — `"1" | "0"`
- `process_path` — absolute path to the temp profile JSON
- `reason` — human-readable

#### `stage: "slice_reused"`
Cache hit: a prior slice was reused because the gcode for this filename + profile combo already existed.
- `gcode` — absolute path to the reused gcode
- `note` — human-readable

#### `stage: "slicing"`
Bare event marking the start of a real Orca slice. No fields.
- **Audit twin:** `event: "slicing_completed"` (carries `gcode_hash`, `estimated_time`, `estimated_filament_g`).

#### `stage: "warning"`
Slicer emitted geometric warnings (overhang, cantilever) in its output.
- `kind` — `"slicer_warning"`
- `messages` — list of message strings
- `count` — integer
- `note` — human-readable

#### `stage: "summary"`
Slice complete; metrics + preview ready for the operator to see.
- `time` — string (e.g. `"3h 12m"`)
- `weight_g` — string (e.g. `"86.5"`)
- `warnings` — list (post-warning, post-render)
- `first_layer_bbox` — `[xmin, ymin, xmax, ymax]`
- `first_layer_width_mm`, `first_layer_depth_mm` — floats
- `summary_file` — absolute path

#### `stage: "uploaded"`
Upload to Moonraker completed (or detected a collision / transport error). The payload spreads `_real_upload`'s result, which carries many fields depending on outcome.
- `print_started` — boolean
- `dry_run` — boolean
- `returncode` — integer
- `host_path` — absolute path on host
- `output` — string (combined stdout/stderr from the upload helper)
- `moonraker_upload_ok` — boolean
- `remote_metadata_ok` — boolean
- `post_upload_validation_ok` — boolean
- `uploaded_filename` — string (printer-storage basename — may include `_YYYYMMDDHHMMSS_<hex>` collision suffix)
- `target_filename` — the unsuffixed basename
- `filename_already_existed` — boolean
- `collision_policy` — `"rename" | "overwrite" | "cancel"`
- `post_upload_blockers`, `post_upload_warnings` — lists
- `human_summary` — operator-facing narrative — surface verbatim
- *`moonraker_metadata`* — full Moonraker metadata response (only when `returncode == 0`)
- *`filename_collision`* — set when a collision was detected; pairs with `cancelled` shape
- *`cancelled`, `cancelled_reason`* — when the operator's `--on-collision cancel` answer applied
- **Audit twin:** `event: "upload_completed"` (carries `uploaded_filename`, `moonraker_upload_ok`, `dry_run`).

#### `stage: "cancelled"`
Workflow cancelled by operator (`--cancel`, or `Cancel` chosen at Upload?/filename_collision).
- *`reason`* — context string when applicable

### Readiness + dispatch

#### `stage: "readiness_card"`
Consolidated final-decision summary the agent surfaces to the operator before Stage 1. Carries everything the agent + operator need to make the start decision.
- `orient`, `tool`, `material`, `profile` — strings
- `orient_supports_tier` — tier string
- `orient_overhang_area_pct` — float
- `supports_override` — `"supports" | "no_supports" | "overhangs"`
- `first_layer_width_mm`, `first_layer_depth_mm` — floats
- `gcode_host_path` — absolute host path
- `printer_storage_filename` — basename on the printer
- `uploaded` — same shape as the `uploaded` event payload
- `start_gate_stage1_command` — shell-ready string (includes `--request-id` + `--operator`)
- `next_step_if_starting` — human-readable
- *`warning_if_overhang_risky`* — set when chosen orient + no_supports is risky
- **Audit twin:** `event: "readiness_card_emitted"` (carries `request_revision`, `gcode_hash`, `printer_storage_filename`).

#### `stage: "next_action_required"`
Imperative signal: tool-call this command verbatim, no operator question, no narrative preamble.
- `reason` — human-readable
- `command` — shell-ready string

Fires after `readiness_card` to push the Stage 1 dispatch, and after `need_input` in some recovery paths.

#### `stage: "complete"`
Terminal event for the upload-only path.
- `reason` — human-readable (e.g. `"Operator chose 'Upload only' at Upload?. File is on the printer; no Stage 1 photo is needed."`)
- **Audit twin:** `event: "upload_only_complete"`.

#### `stage: "setup_required"`
Operator-environment problem detected; workflow halted with remediation guidance.
- `kind` — `"no_profiles" | "profile_not_in_picker"`
- `message` — human-readable
- *`missing_sources`* (for `no_profiles`) — list of expected source paths
- *`requested`, `resolved_slug`, `nearby_slugs`* (for `profile_not_in_picker`)

---

## Audit events — `event:` vocabulary

Audit rows have a common shape:

```json
{"seq": 1, "ts": "2026-06-27T10:42:00+00:00", "request_id": "u1_...", "event": "<name>", "operator": "telegram:brent", "details": {...}}
```

- `seq` — monotonic integer, scoped to one request
- `ts` — UTC ISO 8601
- `request_id` — same shape as the workflow event
- `event` — the name (see below)
- `operator` — identity string. CLI flag `--operator` wins; falls back to env `U1_OPERATOR`; final fallback is `unknown:cli` (workflow) or `unknown:gate` (gate)
- `details` — event-specific keyword fields

Only the fields under `details` are listed below.

### Lifecycle audit twins

| Event | Fires when | `details` fields |
|---|---|---|
| `event: "request_created"` | New request created (workflow) | `model_file`, `model_hash` |
| `event: "request_resumed"` | Content-hash recovery matched | `resumed_from`, `request_revision` |
| `event: "readiness_card_replayed_from_resume"` | Phase-aware skip fired | `printer_storage_filename`, `request_revision` |

### Commit audit twins

| Event | Fires when | `details` fields |
|---|---|---|
| `event: "slicing_completed"` | After real Orca slice produced gcode | `gcode_hash`, `estimated_time`, `estimated_filament_g` |
| `event: "upload_completed"` | After upload to Moonraker | `uploaded_filename`, `moonraker_upload_ok`, `dry_run` |
| `event: "readiness_card_emitted"` | Readiness card built (upload+start path) | `printer_storage_filename`, `gcode_hash`, `request_revision` |
| `event: "upload_only_complete"` | Readiness card built (upload-only path) | `printer_storage_filename`, `gcode_hash`, `request_revision` |

The `gcode_hash` + `request_revision` on `readiness_card_emitted` is what `can_start()` (Phase 3b) consumes to verify the plan hasn't drifted between the operator's review and Stage 2 dispatch.

### Start gate (Stage 1)

These fire from `scripts/u1_print_start_gate.py` and have no workflow twin — they exist only in the audit stream.

| Event | Fires when | `details` fields |
|---|---|---|
| `event: "stage1_photo_captured"` | Real bed photo captured + approval token written | `snapshot_path`, `approval_token` |
| `event: "stage1_photo_failed"` | Camera unreachable or photo verifiably dark | `error`, `is_mock` |

### Start gate (Stage 2)

| Event | Fires when | `details` fields |
|---|---|---|
| `event: "stage2_token_invalid"` | Operator-supplied approval token invalid/expired | `reason` |
| `event: "stage2_preflight_blocked"` | Preflight re-check failed at Stage 2 | `blockers` (list) |
| `event: "stage2_sanity_capture_failed"` | Sanity-only fresh photo unusable | `error`, `is_mock`, `brightness_check` |
| `event: "start_safety_check_passed"` | `can_start()` returned ok | `request_revision`, `gcode_hash` |
| `event: "start_safety_check_failed"` | `can_start()` refused | `reason`, `current_revision`, `current_gcode_hash` |
| `event: "print_started"` | Moonraker `/printer/print/start` accepted | `printer_storage_filename`, `request_revision`, `gcode_hash` |

Stage 2 emits exactly ONE of: `print_started` (success), or a `*_failed` / `*_invalid` / `*_blocked` row (refusal). Stage 1 emits exactly ONE of `stage1_photo_captured` (success) or `stage1_photo_failed` (camera problem).

---

## Twin-pair table (quick cross-reference)

| Workflow stream `stage:` | Audit stream `event:` |
|---|---|
| `request_created` | `request_created` |
| `request_resumed` | `request_resumed` |
| `readiness_card_resumed` | `readiness_card_replayed_from_resume` |
| `slicing` | `slicing_completed` |
| `uploaded` | `upload_completed` |
| `readiness_card` (upload+start path) | `readiness_card_emitted` |
| `complete` (upload-only path) | `upload_only_complete` |
| `triage`, `render`, `orient_analysis`, `history_hint` | — (observational only) |
| `need_input`, `awaiting_input` | — (decision-flow only) |
| `supports_override`, `slice_reused`, `warning`, `summary`, `cancelled`, `setup_required`, `next_action_required` | — (workflow-internal signals) |
| — | `stage1_photo_captured`, `stage1_photo_failed`, `stage2_token_invalid`, `stage2_preflight_blocked`, `stage2_sanity_capture_failed`, `start_safety_check_passed`, `start_safety_check_failed`, `print_started` (all gate-only) |

---

## How to consume each stream

### Workflow events (`events.jsonl` + stdout when `--json-events` is set)

- **For a live agent (Hermes-shaped):** spawn the workflow as a subprocess with `--json-events`, parse each newline-delimited record on stdout, react to the most recent event before the process exits. The `awaiting_input` event marks "process exited waiting for the next turn." See [`HERMES.md`](../HERMES.md) for the full agent contract.
- **For a tail-style consumer:** open `<out_dir>/events.jsonl` and read sequentially. The workflow appends one line per emit. The file is the same content as stdout when `--json-events` is set.
- **For a polling consumer:** stat `events.jsonl` and re-read from the last byte you read. The workflow writes the file via the same `emit()` calls that produce stdout, so eventually-consistent tail-following works.

### Audit log (`requests/<request_id>/audit.jsonl`)

- **For a forensic consumer:** read the file top-to-bottom. Each line is one event. Events are append-only at the application level (`O_APPEND` + `fcntl.flock`), so concurrent writers don't interleave bytes.
- **For state reconstruction:** call `u1_audit.fold(request_id)` (Python). It returns a summary dict with the latest value of selected fields. Useful when `request.json` is corrupted and you need to reconstruct the request's state.
- **For querying:** for v2.0.0 the only CLI is `python3 scripts/u1_audit.py show <request_id>` (chronological pretty-print). Programmatic queries iterate `u1_audit.read(request_id, since=, until=)`.

---

## Versioning

The event contract is **additive**: new events can appear; existing events' field set can grow with optional fields. Consumers should ignore unknown stages and unknown fields.

Two changes would be **breaking** and require a major-version bump:
1. Renaming an existing event (e.g. `stage: "uploaded"` → `stage: "upload_complete"`).
2. Removing a previously-required field from an existing event.

Neither is planned for v2.0.0. If either is ever needed, the version field on `request.json` (currently `schema_version: 1`) will bump in lockstep so consumers can branch on schema version.

---

## Cross-references

- [`HERMES.md`](../HERMES.md) — the agent's procedural rules (Rules 1–8) for how to react to these events.
- [`skills/3d-printer-slicing-automation/SKILL.md`](../skills/3d-printer-slicing-automation/SKILL.md) — the bundled Hermes skill's operator-facing contract.
- [`docs/DESIGN-CONTRACT.md`](DESIGN-CONTRACT.md) — the immutable system contracts (operator / skill / agent).
- [`docs/ROADMAP.md`](ROADMAP.md) — the 9-phase v2.0 plan.
