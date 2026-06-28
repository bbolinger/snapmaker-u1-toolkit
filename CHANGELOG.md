# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
