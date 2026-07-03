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

Reference: Ollama/gemma4 tool-call bug + the fixes are documented in the README
"Local model & serving requirements" section and TROUBLESHOOTING.md.

---

## How to pick up this work cold

1. Read [`docs/DESIGN-CONTRACT.md`](DESIGN-CONTRACT.md) for the immutable system contracts.
2. Read [`HERMES.md`](../HERMES.md) for the agent's procedural rules (Rules 1–8).
3. Read [`docs/events.md`](events.md) for the public event contract.
4. Check `git log --oneline main..v2.0-dev` for in-flight work that hasn't merged yet.
