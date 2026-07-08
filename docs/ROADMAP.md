# Roadmap — Safe AI Print Operator (U1 first)

This tracks the project's direction. The shipped history is summarized; the
active plan is v2.4.

**Current release:** v2.3.0 on `main`. Per-release detail lives in the
[CHANGELOG](../CHANGELOG.md); the system contracts live in
[`docs/DESIGN-CONTRACT.md`](DESIGN-CONTRACT.md).

Status markers:
- ✅ **SHIPPED** — released and validated end-to-end
- 🔜 **NEXT** — active or next-up
- 📋 **QUEUED** — design clear, scope deferred; resume when a real need appears
- 🎯 **POSTURE** — ongoing principle, not a sprint

When you pick this up cold: read [`docs/DESIGN-CONTRACT.md`](DESIGN-CONTRACT.md)
for the system contracts, then [`HERMES.md`](../HERMES.md) for the agent's
stable-tier rules, then [`docs/events.md`](events.md) for the public event
contract.

---

## Shipped so far (v1.6 → v2.3)

**Status:** ✅ SHIPPED

The spine that every future release builds on:

- **Safe AI Print Operator framing** — CLI / operator-workflow / Hermes layers,
  a portable safety model, and a three-minute README for a new reader.
- **Print Request Objects** — every job is a first-class `request_id`
  (`u1_YYYY_MMDD_xxxxxx`) with a durable `request.json`; content-hash recovery
  resumes an in-flight request across model swaps.
- **Audit log + start-safety gate** — append-only `audit.jsonl` per request and
  a single `can_start()` precondition that refuses if the plan drifted since the
  operator reviewed it (revision bump, re-slice, missing bed photo).
- **One unified flow** — a lone STL is a kit of one; button form, text
  fallback, and CLI all share `_action_start` and the same Gemma-proof confirm.
- **First/last-layer photos + quiet monitoring** — cron-driven, no agent turn,
  one alert per distinct issue, watches every active print not just ours.
- **Model-free print-start boundary** — the agent is handed no start command;
  the operator's YES is redeemed by a gateway hook bound to their identity and
  private chat; the agent can only ever help cancel. Forced by a live incident,
  hardened across four adversarial review rounds.
- **v2.3 operator conveniences** — reprint (recall a recent job, no re-slice),
  quantity (1–9 copies), and an optional advanced-settings screen (infill,
  walls, brim, fuzzy skin, tree-vs-grid supports), each verified into the
  sliced gcode.

The public event contract ([`docs/events.md`](events.md)) lets a new frontend
wrap the workflow without reading source.

---

## v2.4 — the plan

Four buckets. A recommended order is at the end.

### Safety

#### Caller-bound start redemption (the broker)

**Status:** 🔜 NEXT — the headline

Today the gateway hook that redeems the operator's YES and the workflow CLI run
as the same Unix user, so redemption is operator-bound but not *caller*-bound: a
deliberately hostile agent with terminal access could invoke it directly. It is
contained — every start is audited, gated, and cancellable — but not prevented.
This is the recurring finding across three adversarial audits.

The fix moves redemption behind a boundary the agent worker cannot reach: the
gateway (or a small broker) runs under a **separate user or host**, owns the
confirmation secret, and exposes redemption only over a peer-credentialed
channel. A request id alone stops being authorization. Honest limit worth
stating up front: while the agent shares the gateway *process*, you cannot fully
defend a process from itself — the real boundary needs the separate user/host,
which is a deployment change, not just repo code. Turns "contained" into
"prevented" for the same-user case; document what remains.

#### Fail-open decisions (from the round-3 audit)

**Status:** 📋 QUEUED (operator decision, then implement)

Three start-path behaviors currently fail *safe*; flipping any to fail *closed*
is a UX-vs-strictness tradeoff to decide as a batch alongside the broker work,
since all three live in the same path:

1. Printer-side gcode existence check fails open on Moonraker 500 / timeout.
2. The confirm hook consumes the YES on process spawn, not on redemption
   success (a child crash strands a valid YES; fails safe).
3. Grace-window notification failure does not block the physical start (the
   operator already said YES, but loses the advertised cancel window).

### Reliability

#### Native-endpoint shim for the Ollama `/v1` tool-call leak

**Status:** 🔜 NEXT — highest daily payoff

The `<channel|>` / template-token residue and garble epochs all trace to
Ollama's `/v1` compatibility endpoint mangling gemma4 tool calls (upstream
ollama/ollama#15798); the native `/api/chat` parser is clean. A tiny proxy that
accepts Hermes' `/v1` requests and forwards them to `/api/chat` removes the
broken parser from the path entirely — no model change, no Hermes downgrade. De-
risks every future live test.

#### 12B-class tool-caller bench

**Status:** 📋 QUEUED (independent of releases)

Find a model that relays `form()` and `next_command` verbatim without
freelancing. Candidates in order: Qwen3 14B (native tool template, ~9 GB),
Llama-3-Groq-8B-Tool-Use (verify context fits the skill payload), Mistral Nemo
12B, Granite4 small. Skip thinking-variant models (empty content in tool turns).
Per model: temp-0.2 variant, then a three-run live gauntlet scored on forms
fired / verbatim relays / freelance incidents.

### Features

#### 3MF ingest

**Status:** 📋 QUEUED

Accept a multi-object `.3mf` as a kit. Lean path: let Orca explode the file
(`--export-stls <dir>`), then feed the extracted parts through the existing
ingest → form → arrange → slice flow. No new geometry parsing; our own arrange
step re-packs, so the old packer divergence does not apply. Geometry only —
embedded profiles and paint/multi-material data are ignored. Zip-of-STLs stays
the primary kit shape.

### Debt / cleanup

#### Consolidate the go/stop control surface

**Status:** 📋 QUEUED (pairs with the broker)

"Operator says go/stop" now spans a confirm marker, a cancel marker, two gateway
hooks, a button callback, typed cancel, a `--grace-cancel` arg, and an expiry
watchdog. Each piece is incident-justified and correct, but the surface is wide.
Fold the two marker dirs and two hooks into one confirmation-window module with a
single per-request state file carrying both go and stop affordances. Best done
alongside the broker, since both touch the redemption path.

#### De-hardcode the runtime script paths

**Status:** 📋 QUEUED (community-first)

The gateway hooks, the expiry watchdog, the start gate, and many emitted
`next_command` strings carry literal `/opt/data/scripts/...` paths. `/opt/data`
is the documented Hermes default, so a standard install works — but the *deploy*
target is already configurable (`U1_DEPLOY_SCRIPTS`) while the *runtime* paths
are not, so a maker who deploys elsewhere has working files the hooks point
past. Derive the runtime script directory from config once (same source the
deploy targets use) so the whole thing runs wherever a community member puts it,
not just this layout.

#### Small stragglers

**Status:** 📋 QUEUED

- Tokenize the kit manual-bed-check override yes-command (the rare degraded /
  camera-failed path still emits the long command instead of a short token).
- Image-order / skill-ack polish on the staged flow.
- Courtesy amend to the awesome-hermes-agent listing (staged → unified/form).

### Recommended order

1. **Native-endpoint shim** — cheapest, highest daily-annoyance payoff, makes
   every subsequent live test reliable instead of a dice roll.
2. **Decide the three fail-open calls** — quick operator judgment; settles the
   audit's open items.
3. **Broker + control-surface consolidation together** — the safety headline,
   done once as a clean refactor of the redemption path.
4. **3MF ingest** — the user-facing feature; an easy win whenever a lighter
   task is wanted.
5. **Model bench + stragglers** as fill-in.

---

## Standing postures

### Community-first, single-operator by design

**Status:** 🎯 POSTURE

This is a shared repo for makers, not a personal tool. The supported deployment
is one operator running their own instance for their own U1 — "single operator"
is the per-user unit, not a specific person. That posture has consequences the
codebase must honor:

- **No deployment-specific hardcodes.** Printer host/port, data dir, operator
  identity, and script paths come from config with sane defaults — never a value
  that only fits one person's box. When a feature is tightened to a setup (e.g.
  the operator binding), it is a documented config default, never a literal.
- **The supported shape is stated, not implied.** Model-free start is bound to
  one operator in one private chat by design; a shared team channel or multiple
  operators on one instance is out of scope, and the docs say so up front rather
  than letting a maker discover it.
- **Ship nothing personal.** No real ids, hosts, or paths in tracked code —
  examples use obvious placeholders.

A shared multi-operator / team deployment (several people driving one instance,
with auth and shared-channel binding) is a different product with a different
safety model; it is deliberately not on this roadmap.

### Multi-printer scope avoidance

**Status:** 🎯 POSTURE

Make the U1 implementation excellent before chasing Bambu / Prusa / OctoPrint /
etc. Design internals so a second printer *could* be added later, but do not
pre-build the abstraction — it will be wrong without a real second printer to
design against. Refactor along the seams the U1 has proven, not guessed seams.

### Capability modes

**Status:** 📋 QUEUED

Three deployment postures: `read_only`, `upload_only`, `operator_start` (current
behavior). Build out when a second posture actually appears.

### Sandbox / demo mode

**Status:** 📋 QUEUED

Run the workflow without a U1 for CI and demos. The existing `--no-live-material`
+ dry-run upload already cover analysis / slice / upload. Stage 1/2 verify
physical state by design, so a sandbox would have to fake the moat to be useful —
which would mislead evaluators. Build a non-misleading version when a contributor
without a U1 actually needs it.

---

## How to pick up this work cold

1. Read [`docs/DESIGN-CONTRACT.md`](DESIGN-CONTRACT.md) for the immutable system contracts.
2. Read [`HERMES.md`](../HERMES.md) for the agent's stable-tier rules.
3. Read [`docs/events.md`](events.md) for the public event contract.
4. Check the [CHANGELOG](../CHANGELOG.md) for what the current release actually shipped.
