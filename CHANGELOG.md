# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased — v2.2 line]

### Added

- **Pre-print review document** ([`scripts/u1_review_doc.py`](scripts/u1_review_doc.py)).
  Every readiness card now ships with a `review.md` flight plan the operator
  can actually read before saying yes: what will print (per-plate files,
  estimates, parts), the ~12 settings that decide success, and the operator's
  own decisions/overrides. Settings come from the config block inside the
  sliced gcode — what the printer will execute, never what the workflow
  intended — and the doc header carries request revision + gcode hash, so
  `can_start()`'s drift check guarantees the reviewed plan is the printed
  plan. Informational by design: generation failures audit
  `review_doc_failed` and never block the flow. Emitted as a `review_doc`
  event by both kit paths and the single-STL workflow; readiness cards carry
  `review_doc_path`.
- **Preset-deviation markers + full-config sweep.** Every setting present in
  both the gcode and the chosen preset (process + filament, inheritance
  flattened) is compared: curated rows flag inline (`240` ⚠ *preset: 230*),
  everything else that deviates renders in an "Other deviations" table —
  ironing, retraction, any of the ~300 keys. Deviations-only output is
  self-curating; ids/provenance metadata are excluded as noise. Baseline is
  the preset the operator PICKED (integrity, not orthodoxy — a custom
  profile is compared against itself, not against Snapmaker stock).
- **Material-envelope sanity check.** Nozzle temps in the gcode are checked
  against the range the material's own filament profile declares
  (`nozzle_temperature_range_low/high`) — the layer that catches a custom
  profile that matches itself perfectly but runs 275°C on a 220–260 material.
  In-range prints a quiet confirmation; no declared range, no invented norm.
- The doc also now states that the chosen material is re-verified against
  what is physically loaded at start time (the gate check that already
  existed, made visible).

---

## [2.1.0] — 2026-07-02

**Multi-part kit support, the pre-start grace period with model-free
Telegram cancel, and a hardened safety boundary** — the rc1 feature set
(see rc1/rc2 entries below) shipped after two external review rounds and
full live verification on real hardware.

### Added since rc2

- **Cancel-chain verification guide** ([`docs/verify-cancel-hook.md`](docs/verify-cancel-hook.md)):
  hook install checks, hook.log entry meanings, gate-side audit rows, and a
  zero-risk drill (seeded pending window, no printer needed) covering every
  match mode.
- **`HERMES_BIN` documented** (README + `.env.example`): the gate spawns the
  notify script in a stripped subprocess env where `hermes` is usually not
  on PATH — without it the DM is never sent (audited
  `pre_start_grace_notify_failed`; the wait still runs fail-open).

### Fixed since rc2

- **`CANCEL!!!` fires.** The exact-match rule refused trailing punctuation —
  ignoring exactly the panicking operator the button exists for. Punctuation
  is stripped before matching; extra WORDS still never match ("cancel that
  plan" stays safe). Found in live drilling, fixed with tests, re-verified
  on hardware the same hour.
- Kit `bed_clear_start` skill guidance: don't re-run the camera when
  `bed_snapshot_path` is null (runtime fix ported back to the workspace).

### Validated — live on hardware, 2026-07-02

- Full cancel drill, 5/5 on the real Hermes gateway: bare `CANCEL`
  (case-insensitive), scoped `cancel <code>`, wrong code cancels nothing,
  prose ignored, panic punctuation (`Cancel!?!`) fires. Proves the
  model-free path end to end: gateway hook → marker file → gate poll, with
  the LLM agent never in the loop.
- Real-print checklist: bare + scoped cancel during a live grace window
  (audit rows, no HTTP to the printer), last-seconds cancel caught,
  `recovery.stage1_command` restart (fresh photo + fresh yes, no re-slice),
  receipt-removed run advertises the SSH fallback, kit `--pending-nonce`
  enforcement, and the `smoke:` operator fence refusing Stage 2 end to end.
- 614 unit + integration tests green in CI (Python 3.11 + 3.12).

### Upgrading from v2.0.x

`git pull` + re-run `bash tools/install_hermes_cancel_hook.sh` (new hook +
receipt) and set `HERMES_BIN` + `U1_GRACE_NOTIFY_CMD` in your env (see
`.env.example`). `request.json` is additive — no migration.

---

## [2.1.0-rc2] — 2026-07-02

Review-driven hardening of rc1. An external deep review (4 parallel passes:
kit workflow, safety gate + grace-cancel, form/arrange/kit scripts, adapters)
found bugs that contradicted rc1's own safety claims; all release blockers
are fixed here. No schema changes.

### Fixed — safety

- **Test-operator fence is now sticky.** Only 2 of 15 emitted `next_command`s
  carried `--operator`, so a `smoke:*` run silently resolved to the production
  env operator by confirm time and Stage 2 fired a real print — the exact
  incident class Fence 1 was built to close. `_build_next_command` now stamps
  the explicit CLI operator into every emitted command (env-resolved identity
  stays env-resolved, replay-safe). The kit redirect in `u1_slice_workflow.py`
  carries `--operator`/`--nozzle` too, and a zip that fails kit inspection
  emits `kit_detection_failed` instead of silently slicing only the first model.
- **`cancel <code>` is implemented, not just documented.** The hook matched
  only bare keywords: `cancel abc123` (the documented form) cancelled nothing,
  and bare `cancel` touched every window. Now: bare keyword = cancel all;
  `cancel <code>` = cancel only the matching request; unknown code = cancel
  nothing (logged). The gate re-checks the marker after the final grace tick
  and immediately before the start call (a last-second CANCEL was silently
  lost). The notify DM only promises reply-to-cancel when the installer's
  receipt shows the hook actually loaded — otherwise it gives the SSH
  fallback. Pending-state JSON is written via `json` (a filename with a quote
  made that request uncancellable). Installer verifies `u1_grace_cancel`
  specifically, not any `hook(s) loaded` line.
- **Form parser fails loud instead of silently mis-parsing.** Duplicate
  fields with different values are conflicts (was silent last-wins, which
  also made parsing order-dependent). Unoffered material-family tokens
  (`PETG` with only PLA offered) error as a material problem instead of
  substring-matching a profile name. A bare integer on a multi-part kit is an
  ambiguity error (staged mode reads `3` as part 3; form mode read it as
  profile 3). Part ranges bounds-check before expansion (`parts 1-30000000`
  built a 275MB error string). Part/profile labels are sanitized before
  rendering (zip entry names could inject fake form lines).
- **`supports=overhangs` removed from every offer.** `enable_support` is
  binary in the profile patch — the option was accepted, echoed on the
  readiness card, and silently ignored (printed without supports). It now
  errors as not-offered until a real overhangs-only override exists.
- **Orca nonzero-rc tolerance narrowed to the verified overflow rc (154).**
  Any other rc raises even when a plate file exists — a truncated plate that
  happened to sit within bed extent was hashed and uploaded as a good slice.
  A plate with zero extrusion moves reports "bad or truncated slice output"
  instead of the misleading "extent overflow".
- **Unset operator identity (`unknown:gate`) still passes Fence 1** — now an
  explicit, tested decision with a loud `gate_operator_unknown` audit row.

### Fixed — usability (refusals now hand back a path forward)

- **Grace-cancel refusal carries `recovery.stage1_command`** — slice and
  upload are still valid, so restarting costs one fresh photo + one fresh
  yes, not a workflow re-run.
- **`adjust` → re-confirm no longer bricks on a filename collision**:
  re-uploading this request's own deterministic plate name defaults to
  overwrite (collisions with anything else still ask).
- **Post-confirm actions resume from persisted state.** An `--action` command
  missing turn flags backfills parts/orient/tool/material/profile/supports
  from the confirm state (audited) instead of falling back into the staged
  Q&A and re-slicing. `refresh-bed-photo` is now actually offered on the
  printer-busy card and its emitted command carries the full answer set.
- **Legacy `--form-answers` path honesty**: upload rc is checked (a dead
  Moonraker no longer yields "all plates on the printer"), dry-run completion
  says DRY RUN, and `--action start` adopts the Stage-1 sidecar token instead
  of re-emitting Stage 1 forever.

### Fixed — adapters (still EXPERIMENTAL; `form_schema` not yet emitted)

- `install.py` verify step crashed with a `%`-format `TypeError` on every
  non-dry-run install; the run.py anchor is checked before any files are
  copied (no more partial installs); `--uninstall` only restores the backup
  when the patch marker is present (no more downgrading an upgraded Hermes).
- Telegram form tool: slots matched on `(chat_id, message_id)` (message ids
  are per-chat; cross-chat collisions could mutate the wrong form); a submit
  after gateway timeout says the form expired instead of "✅ Submitted";
  schema-derived text is HTML-escaped (a part named `bracket<v2>.stl` killed
  the send).
- The byte-identical duplicate renderer under `adapters/hermes/tools/` is
  gone — the installer sources `adapters/telegram/u1_form_telegram.py`.

### Added

- **GitHub Actions CI** (`.github/workflows/tests.yml`) — pytest on push/PR,
  Python 3.11 + 3.12. rc1 was tagged with 4 failing tests (bed-size constant
  fixed 220→270 without updating its test; arrange fixtures predated the
  bed-overflow guard) — CI makes that class impossible to miss again.
- `docs/events.md` now documents the grace-period audit events,
  `kit_upload_failed`, `kit_detection_failed`, and the experimental status of
  the form protocol. HOOK.yaml and the README cancel section describe the
  implemented behavior.

### Fixed — review round 2

- **Kit ingest could silently destroy a part.** The `__N` dedup rename never
  checked whether the deduped name already existed as a distinct archive
  entry — a kit holding `a/part.stl`, a genuine `part__1.stl`, and
  `b/part.stl` overwrote the real `part__1.stl` and returned a duplicate
  path (one part lost, one printed twice, no error). Dedup now checks every
  name used so far.
- **Ingest limits + clean rejection.** Caps on entry count (100), per-part
  size (200MB), and total size (600MB) refuse a pathological kit zip before
  it OOMs the workflow; a garbage/unparseable STL (or any ingest failure)
  emits a `kit_rejected` event instead of a raw traceback. Windows-style
  backslash entry names are sanitized (latent zip-slip on non-POSIX hosts).
- **`start manual-bed-check` is refused when the camera worked.** The
  Layer-3 override exists for the degraded-camera case; when a real photo +
  token exist, the handler now refuses (`manual_bed_check_refused`, audited)
  and points at the normal `start` path — nothing can route around the photo.
- **The copy-verbatim yes-command contract is mechanically enforced.** The
  `bed_clear_start` pending object's nonce (previously minted but never
  checked) now rides the emitted `next_command_on_yes` as `--pending-nonce`
  and is validated on the confirm call — a hand-assembled
  `--bed-clear-confirmed` invocation is refused with a fresh-prompt
  instruction.
- `_normalize_filename` strips the `./x.gcode` form Moonraker rejects.
- ~530 lines of explicitly-dead plate-preview renderers deleted from
  `u1_kit_workflow.py` (recoverable via git history) — review attention
  belongs on the safety-critical code.
- Grace-notify timing reviewed and deliberately kept: the up-to-20s notify
  latency delays the window's close in the OPERATOR'S favor (their countdown
  starts when the DM lands, which is when the poll loop starts).

### Validated

- Full suite green at this commit. Live re-validation of the grace-cancel
  path on real hardware is the remaining gate before v2.1.0 final.

---

## [2.1.0-rc1] — 2026-06-30

**Multi-part / multi-plate kit support.** Send a zip of STLs (the common
Printables shape) and the toolkit arranges them onto the bed, slices every
plate Orca needs, uploads them all, and runs the safety gate on plate 1. The
operator answers one consolidated form; nothing else changes about the safety
model.

*(Scope note, added at rc2: this entry was written at the kit-workflow
milestone. rc1's tag also contained the pre-start grace period + Telegram
cancel hook, the Fence 1 test-operator refusal, and the experimental
form-protocol adapters tree — those are documented in the rc2 entry above.)*

### Added

- **Kit ingest** ([`scripts/u1_kit.py`](scripts/u1_kit.py)). Extracts every STL
  from a zip, measures each part's footprint, flags any too big for the bed, and
  builds a `kit.parts[]` record. A single STL is just a kit of one.
- **Arrange + multi-plate slice** ([`scripts/u1_arrange.py`](scripts/u1_arrange.py)).
  One Orca call with `--arrange 1` lays all selected parts out; Orca auto-splits
  overflow into `plate_1…plate_N.gcode`. No bin-packer or 3MF writer — Orca owns
  layout (spike-verified on the real binary). `--allow-rotations` always on;
  `--orient 1` when the operator picks auto-orient.
- **Script-parsed decision form** ([`scripts/u1_form.py`](scripts/u1_form.py)).
  After analysis the workflow emits one consolidated form (parts, orient, tool,
  material, profile, supports, action). The operator answers in a single line,
  any order; **the script parses + validates it — the model never interprets**.
  The readiness card echoes the parse back for the operator to confirm before
  the photo gate.
- **Kit orchestrator** ([`scripts/u1_kit_workflow.py`](scripts/u1_kit_workflow.py)).
  Drives ingest → form → arrange → upload-all → readiness → gate plate 1, reusing
  the existing upload + Stage 1/2 gate. Plates 2…N are uploaded for the operator
  to start from the Snapmaker app; the watchdog still photographs every plate
  regardless of who started it.
- **Auto-routing.** `u1_slice_workflow.py` detects a multi-STL zip and emits a
  `kit_detected` event with the kit command — one entrypoint for the agent, the
  script decides single-vs-kit.
- **Skill docs** — `references/multipart-kits.md` (agent guide), HERMES.md Rule 9,
  SKILL.md routing.
- **Shared `build_stage1_command()`** in `u1_print_start_gate.py` so the single
  and kit workflows can't drift on the Stage-1 command.

### Changed

- `request.json` gains **optional additive `kit` + `plates` fields** — NO
  `schema_version` bump and NO migrator. Nothing branches on the version, and a
  pre-v2.1 single-STL request keeps working untouched (absence of `plates` = one
  plate = the top-level gcode). Leaner than the planned schema-v2 + migrator.
- Top-level `gcode_hash` / approval / token stay bound to **plate 1**, so
  `can_start()` and the Stage 1/2 moat validate it exactly as in v2.0 — the
  safety core is unchanged.

### Safety

- Kit toolhead mapping mirrors the single workflow exactly (T0 → `extruder`,
  T1 → `extruder1`, …) — a self-review caught and fixed a wrong-toolhead bug
  before it shipped.

### Validated

- Live on the real Orca binary: 3-part kit → 1 arranged plate; 9 parts → 3
  plates (overflow auto-split); full end-to-end (form → parse → arrange →
  upload → readiness gating plate 1); content-hash recovery by re-send.
- 470 unit + integration tests passed at the kit-workflow milestone (the
  grace-cancel + Fence 1 commits landed after this count — see rc2 for the
  honest accounting). The v2.0 single-STL flow and
  its tests are untouched.

---

## [2.0.2] — 2026-06-30

Two safety-gate hotfixes. Both are the **same bug class**: v2.0 single-flow assumptions baked into Stage-2 preconditions refuse legitimate multi-tool / forward-compatible workflows. No schema changes; safe to apply by `git pull` alone.

### Fixed

- **Stage-2 tool-change blocker rejected multi-tool prints.** `u1_print_start_gate.preflight()` compared the printer's *idle* active extruder to the gcode's target tool and refused to dispatch if they differed — but the U1 is a gcode-driven 4-tool changer and every multi-tool slice begins with a `T<N>` activation in the preamble. The check was unreachable for any non-default tool. Fix: read the gcode preamble (first ~50 lines) and accept the print if the gcode itself issues the expected `T<N>` macro. Single-tool prints on the default extruder still pass via the original path.
- **`can_start()` refused every readiness event it didn't already know by name.** `_RESUMED_OR_EMITTED` hard-coded only the single-STL workflow's two event names. Any new workflow that emits a workflow-specific readiness event would be refused with "no readiness_card emitted yet" even after the operator reviewed the plan. Added forward-compat slot for the v2.1 kit workflow's `kit_readiness_card_emitted`; documented the rule that new readiness-emitting workflows must register here.

---

## [2.0.1] — 2026-06-28

Doc-only patch from an external review of the v2.0.0 release notes. No runtime or schema changes; no test additions; safe to apply by `git pull` alone.

### Fixed

- **Approval-token TTL drift.** README.md and `docs/DESIGN-CONTRACT.md` still said "5-min TTL" — v2.0.0 bumped it to 30 min and the CHANGELOG reflected that, but two reader-facing docs lagged. Now both say 30-min consistently.
- **`start_gate_stage1_command` description.** `docs/events.md` claimed the emitted command includes `--operator`. v2.0.0 stopped baking `--operator` so the gate resolves operator identity from `U1_OPERATOR` at execution time (replay-safe). Doc now describes the actual emitted shape.
- **README roadmap honesty.** Capability modes + Sandbox mode were listed as v2.0 "headline pieces" without indicating they're deferred. Now labeled inline `(Deferred — see CHANGELOG)` so the restraint reads as intentional, not as unfinished work.
- **`u1_audit.py` PIPE_BUF comment.** Module docstring and append() comments leaned on PIPE_BUF for atomicity. PIPE_BUF is a pipe/FIFO concept; on regular files the `fcntl.flock` is what gives the guarantee. Comments now describe the flock-based scheme accurately.

---

## [2.0.0] — 2026-06-28

The **Safe AI Print Operator** release. Reframes the project around an explicit safety boundary between the AI agent (which can recommend, explain, and prepare a print) and the toolkit (which owns the final safety checks and refuses printer-affecting actions without operator approval tied to a specific request ID).

### Added

- **Print Request Objects.** Every print job becomes a first-class entity with a stable, human-readable `request_id` (`u1_YYYY_MMDD_xxxxxx`) and a durable `requests/<id>/request.json`. Content-hash recovery resumes in-flight requests across model swaps when the agent loses conversation context — the operator can re-send the same STL via Telegram and the workflow finds the in-flight request.
- **Per-request audit log** ([`scripts/u1_audit.py`](scripts/u1_audit.py)). Append-only `requests/<id>/audit.jsonl` capturing every meaningful state transition. Multi-process safe via `O_APPEND + fcntl.flock`. CLI: `python3 scripts/u1_audit.py show <request_id>`.
- **`can_start()` safety gate** ([`scripts/u1_safety.py`](scripts/u1_safety.py)). Single precondition function every Stage 2 dispatch routes through. Verifies the print plan hasn't drifted (revision + gcode_hash) since the operator reviewed the readiness card. Refuses if anything plan-affecting changed between review and start.
- **Per-request token + photo storage.** Stage 1's approval token and bed photo now live in `requests/<id>/` instead of a global path. Prevents cross-request token leakage.
- **30-min approval-token TTL** (was 5 min). Humane for the real-world cadence of operator review.
- **CLI flags** on `u1_slice_workflow.py`: `--request-id`, `--operator`, `--fresh`. On `u1_print_start_gate.py`: `--request-id`, `--operator`.
- **One-shot migrator** ([`scripts/migrate_v0_to_v1.py`](scripts/migrate_v0_to_v1.py)) for pre-Phase-3 `request.json` files. Idempotent; never fabricates approval state.
- **HERMES.md Rules 6 / 7 / 8** — Resume on same STL path; every approval question includes `request_id`; Stage 2 dispatch uses the workflow's emitted command (never assemble from chat memory).
- **Public event contract** ([`docs/events.md`](docs/events.md)) documenting both event streams (workflow `events.jsonl` and forensic `audit.jsonl`). Any frontend can wrap the workflow using this doc alone.
- **Skill `references/` pattern.** Scenario-specific procedures live in `skills/3d-printer-slicing-automation/references/<topic>.md`, loaded on demand via Hermes' Level-2 progressive disclosure. Keeps SKILL.md minimal so small models (gemma4-26b-64k and below) have context budget to follow the workflow.

### Changed

- **Operator-facing approval questions now include the `request_id` verbatim** ("Bed clear and you want to start request `u1_2026_...`? (yes/no)"). Operator's "yes" routes to the specific named request rather than "whatever was most recent."
- **HERMES.md slimmed** for small-model compatibility — Rules 6/7/8 condensed; scenario detail moved to `skills/.../references/` files.
- **Workflow output location** — when invoked without `--out-dir`, slicing artifacts land in `requests/<id>/` (was `artifacts/slice_workflow/<stem>/<timestamp>/`). `--out-dir` is preserved as a legacy escape hatch.
- **`request.json` schema** — adds `schema_version: 1`, `request_revision`, `approvals.{upload,start}`, `safety.{bed_clear_check_required, bed_clear_photo_captured}`, top-level `operator` field.

### Fixed

- Operator stamping via `U1_OPERATOR` env var now works regardless of subprocess cwd (dotenv loader checks `/opt/data/.env` in addition to cwd-walk).
- `request_revision` no longer bumps on initial-set of a plan-affecting field (was incrementing on every operator answer during the decision walkthrough).
- Phase-aware-skip resume's audit row now carries `gcode_hash` (was missing, causing `can_start()` to incorrectly refuse with phantom mismatch).
- `start_gate_stage1_command` no longer bakes `--operator` into the emitted command (gate resolves from env at execution time — replay-safe across operator config changes).
- Harness's `detect_prompt_kind` correctly matches the new approval phrasing and doesn't false-positive on orient render PNGs.

### Deferred

- **Capability modes** (`read_only` / `upload_only` / `operator_start`). Currently only `operator_start` is in use. Build out when a second deployment posture appears.
- **Sandbox / demo mode.** Existing `--no-live-material` flag + dry-run upload already cover the meaningful workflow steps without a U1; Stage 1/2 require real hardware by design. Build out when a contributor without a U1 actually needs it.

### Validated

- Live cross-model end-to-end on `gpt-5.5` and `gemma4-26b-64k` via Telegram → Hermes → workflow → gate → Moonraker.
- 413 unit + integration tests pass.

### Upgrading

See [README.md → Upgrading from v1.x](README.md#upgrading-from-v1x).

---

## [1.6.0] — earlier

### Added

- Pre-slice Orca mesh-topology analysis. The workflow drafts a fast slice on the source mesh and surfaces Orca's verdict (`floating cantilever` / `clean` / overhang layer fraction) at the orient prompt so the operator picks based on Orca's actual call, not a face-angle approximation.
- `HERMES.md` stable-tier procedural rules (Rules 1-5).

For earlier releases, see `git log`.
