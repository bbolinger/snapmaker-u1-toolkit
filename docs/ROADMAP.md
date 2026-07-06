# Roadmap — Safe AI Print Operator (U1 first)

This document tracks the project's direction across 9 phases from v1.6 ("Hermes-compatible slicer") to v2.0 ("Safe AI Print Operator with a portable safety model"). All 9 phases ship as a single v2.0.0 release.

The v1.x line is frozen on `main` at v1.6.0. The v2.0 work lives on the `v2.0-dev` branch until end-to-end acceptance, then merges to `main` with a v2.0.0 tag.

Status markers:
- ✅ **DONE** — shipped and validated end-to-end
- 📋 **QUEUED** — design clear, scope deferred; resume when a real need appears
- 🎯 **POSTURE** — ongoing principle, not a sprint

When you pick this up cold: read [`docs/DESIGN-CONTRACT.md`](DESIGN-CONTRACT.md) for the system contracts, then [`HERMES.md`](../HERMES.md) for the agent's procedural rules, then [`docs/events.md`](events.md) for the public event contract.

---

## Phase 1 — Repo identity + README reframe

**Status:** ✅ DONE

Positions the project as "Safe AI Print Operator — Snapmaker U1 first." README explains the safety model, the three layers (CLI / operator workflow / Hermes mode), and what the operator approves to a new reader in three minutes.

**Cross-links:** [`README.md`](../README.md)

---

## Phase 2 — Print Request Objects

**Status:** ✅ DONE

Every print job is a first-class entity with a stable `request_id` (`u1_YYYY_MMDD_xxxxxx`) and a durable `requests/<id>/request.json`. Content-hash recovery resumes in-flight requests across model swaps when the agent loses conversation context. Approval flows attach to the ID.

**Cross-links:** [`scripts/u1_request.py`](../scripts/u1_request.py), [`HERMES.md`](../HERMES.md) Rules 6 + 7

---

## Phase 3 — Audit log + start-safety gate

**Status:** ✅ DONE

Per-request append-only `audit.jsonl` (forensic evidence trail) + the `can_start()` precondition function as the single source of truth for whether it's safe to physically dispatch a print. Every Stage 2 path routes through it; refuses if the print plan has drifted (revision bump, re-slice, missing bed photo) since the operator reviewed the readiness card.

**Cross-links:** [`scripts/u1_audit.py`](../scripts/u1_audit.py), [`scripts/u1_safety.py`](../scripts/u1_safety.py), [`HERMES.md`](../HERMES.md) Rule 8

---

## Phase 4 — Capability modes

**Status:** 📋 QUEUED

Three deployment postures: `read_only` (inspection only), `upload_only` (slice + preview + upload, no start), `operator_start` (current behavior — start requires Stage 1 token). Build out when a second deployment posture appears.

---

## Phase 5 — Hermes skill operates on `request_id`

**Status:** ✅ DONE

Every operator-facing approval question includes the `request_id` verbatim, so the operator's "yes" routes to the specific named request rather than "whatever was most recent." Aligns the agent's behavior with the toolkit's already-built request_id primitives.

**Cross-links:** [`skills/3d-printer-slicing-automation/SKILL.md`](../skills/3d-printer-slicing-automation/SKILL.md), [`HERMES.md`](../HERMES.md), [`README.md`](../README.md#what-an-approval-looks-like)

---

## Phase 6 — Public event contract

**Status:** ✅ DONE

[`docs/events.md`](events.md) is the public contract for both event streams (workflow `events.jsonl` and forensic `audit.jsonl`). A new frontend (Telegram bot, web UI, MCP server) can wrap the workflow using this doc alone, with no source-reading required.

**Cross-links:** [`docs/events.md`](events.md)

---

## Phase 7 — Sandbox / demo mode

**Status:** 📋 QUEUED

Run the workflow without a U1 for CI and demos. The toolkit's existing `--no-live-material` flag + dry-run upload already cover the meaningful workflow steps (analysis, slice, upload). Stage 1/2 require real hardware by design — they verify physical state, so a sandbox would have to fake the moat to be useful, which would mislead evaluators. Build out a non-misleading version when a contributor without a U1 actually needs it.

---

## Phase 8 — First/last-layer photos + quiet monitoring

**Status:** ✅ DONE

Cron-driven: `u1_last_layer_watch.py` captures milestone photos (first 5 layers + last layer), and `u1_print_watchdog.py` runs a 20-minute health poll. Both follow the "print nothing unless operator-worthy" contract — quiet during normal print, one alert per distinct issue, no spam.

**Cross-links:** [`scripts/u1_last_layer_watch.py`](../scripts/u1_last_layer_watch.py), [`scripts/u1_print_watchdog.py`](../scripts/u1_print_watchdog.py)

---

## Phase 9 — Multi-printer scope avoidance

**Status:** 🎯 POSTURE

Resist scope creep. Make the U1 implementation excellent before chasing Bambu / Prusa / OctoPrint / Klipper / etc. Design internals so a second printer could be added later, but don't pre-build the abstraction — it will be wrong without a real second printer to design against.

The U1 implementation is the proving ground for the safety model + event contract. When a second printer eventually appears, refactor along the seams the U1 implementation has proven, not along guessed seams.

---

## Phase 10 — Single-STL system-width parity (NEXT)

**Status:** 🔜 NEXT WORK (2026-07-03)

The kit / multi-STL flow (`u1_kit_workflow.py`) was brought to full parity across
all interaction modes — button form, text fallback, and direct CLI all share
`_action_start` and use the "Slice & review" verb + the short-token
`--confirm-start` bed-clear confirm (Gemma-proof). The **single-STL** flow
(`u1_slice_workflow.py`) is a separate, older implementation that did NOT get any
of it and is the remaining "path parity" gap for tiny local models:

1. **Verbiage.** "Upload + start gate" → "Slice & review" framing (the `Upload?`
   prompt options + any pre-commit "Start" wording). Collapse the double-yes the
   same way: the bed-clear yes/no is the single start decision.
2. **Short-token confirm (safety-critical).** Single-STL currently makes the
   agent run a Stage-1 command, then *extract the approval token from the output
   and hand-rebuild the Stage-2 command* (`--bed-clear start --approval-token
   <token>`). This is the worst-case mangle pattern — a 26B model butchers it.
   Give single-STL a `--confirm-start <token>` path (its own, or refactor it to
   share the kit's `_action_start`). Keep the nonce/approval-token as the auth.
3. **Buttons (was Phase-4 / Increment 4).** Single-STL has no form mode yet.
   A shared decision-collection module gives it the same button UX as the kit.

Also fold in the small consistency cleanup: the kit **manual-bed-check override**
path still emits the long yes-command instead of a short token (rare degraded /
camera-failed path) — tokenize it for uniformity.

Rationale for "system width": a super-tiny model that can't drive buttons must
still work through the text fallback, and a human at a terminal must work through
the CLI — all three modes need the same verbiage + the same Gemma-proof confirm.
The kit flow already delivers this; single-STL must too before the demo can claim
path parity (kit AND single-STL both work on the model users actually run).

**UX order + wording (from live kit/Gemma run 2026-07-04) — apply to kit AND single-STL buttons:**
- ORDER: surface [plate preview + bed photo + review.md] BEFORE the decision (currently the question shows above the photo); do not let the verbose skill-ack land mid-flow.
- WORDING: hide the request-id from operator-facing text (leaks in the prompt, cancel hint, and cancelled message); one decision not a double question; plain "Reply CANCEL" (no `cancel <id>` targeting — single printer).
- Strip the Hermes doc-cache prefix (doc_<hash>_) from the printer filename too (kit got this in 82a9681; single-STL names off archive.stem the same way).
- KEEP: the parts thumbnail grid, "Submitted - slicing in the background", and surfacing preview+bed photo+review.

Reference: Ollama/gemma4 tool-call bug + the fixes are documented in the README
"Local model & serving requirements" section and TROUBLESHOOTING.md.

---

## v2.3 — Operator conveniences (planned 2026-07-06)

Three features, ordered cheapest-to-richest. All three reuse proven machinery;
none touches the safety boundary's shape.

### 1. Reprint — recall recent prints, restart through the gate

**Status:** 📋 QUEUED (first up)

List recent prints from the `u1_print_history` ledger (filename, tool,
material, when, duration) as a single-select form. Picking one skips slicing
entirely: straight to Stage-1 bed photo → operator yes → Stage-2 start on the
gcode already in printer storage.

Why it's cheap: the ledger already records everything needed, and the start
gate already (a) takes a printer-storage filename, (b) re-verifies material
against what's physically loaded, and (c) validates the file exists on the
printer before opening the grace window. If the file was deleted from the
printer, re-upload from the request dir when it still exists; otherwise offer
a fresh slice.

### 2. Quantity — print N copies

**Status:** 📋 QUEUED

Orca 2.4.0 CLI natively supports `--repetitions count` (whole plate) and
repeated positional STL paths (per-part control). Form gets a quantity
selector — single-model (kit-of-one) runs first; per-part ×N for kits only if
a real need appears. The instance-keyed 3D render already draws duplicate
copies correctly, and multi-plate overflow is already handled by the extent
guard + plate split.

### 3. Advanced settings screen (infill, walls, brim, fuzzy skin)

**Status:** 📋 QUEUED

An optional "Advanced" button on the form's Review screen jumps to an extra
group and returns to Review (the edit-return mechanism the re-edit fix added).
Skipping it = today's behavior, so the default path costs nothing — and since
the form schema rides on disk (the model only relays a `form_id`), extra
fields cost a small local model zero tokens.

Fields (button presets only — the renderer is single/multi-select, and preset
lists beat free-typed numbers for reliability):

- Infill density: 10 / 15 / 20 / 30 / 40 / 50 %
- Infill pattern: grid / gyroid / honeycomb / triangles
- Wall loops: 2 / 3 / 4
- Brim: off / auto
- Fuzzy skin: off / on

Backend: generalize `apply_supports_override` into
`apply_profile_overrides(dict)` — same flatten-profile → patch keys →
self-contained temp JSON pattern (Orca has no CLI override flags; profile
patching is the only reliable path). Keys: `sparse_infill_density`,
`sparse_infill_pattern`, `wall_loops`, `brim_type`, `fuzzy_skin`. Every
override must appear in `review.md` so what the operator approved is what
prints. Text mode: optional prefixed tokens in the one-liner; staged mode
skips advanced entirely.

Scope fence: layer height and supports stay where they are (profile choice +
the existing supports field). Seam, speeds, and temperatures stay out —
that's profile territory, and every added option is another decision on the
operator's screen.

---

## v2.4 — 3MF ingest (planned 2026-07-06)

**Status:** 📋 QUEUED

Accept a multi-object `.3mf` as a kit. Lean path: normalize to the proven STL
pipeline by having Orca itself explode the file (`--export-stls <dir>`), then
feed the extracted parts through the existing ingest → form → arrange → slice
flow. No new geometry parsing. The packer divergence that bit the old 3D
preview doesn't apply here — extraction only; our own arrange step re-packs
everything. Embedded profiles and paint/multi-material data are ignored:
geometry only. `--allow-newer-file` covers newer 3mf versions. Zip-of-STLs
stays the primary kit shape.

---

## Model bench — 12B-class tool callers (standing)

**Status:** 📋 QUEUED (independent of releases)

Candidates to test against the skill, in order: Qwen3 14B (native tool
template, ~9 GB), Llama-3-Groq-8B-Tool-Use (top small-class tool-calling
benchmark; verify its context window fits the skill payload), Mistral Nemo
12B, Granite4 small. Skip thinking-variant models (empty content in tool
turns). Protocol per model: create a temp-0.2 variant, then the live
gauntlet — does `form()` fire, does `next_command` relay verbatim, does it
freelance on a bare zip. Three runs each; score = forms fired / verbatim
relays / freelance incidents.

---

## How to pick up this work cold

1. Read [`docs/DESIGN-CONTRACT.md`](DESIGN-CONTRACT.md) for the immutable system contracts.
2. Read [`HERMES.md`](../HERMES.md) for the agent's procedural rules (Rules 1–8).
3. Read [`docs/events.md`](events.md) for the public event contract.
4. Check `git log --oneline main..v2.0-dev` for in-flight work that hasn't merged yet.
