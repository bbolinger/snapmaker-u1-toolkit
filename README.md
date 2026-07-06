# Snapmaker U1 Toolkit

### Safe AI Print Operator — Snapmaker U1 first

![Hermes Agent + Snapmaker U1 — safety-staged print automation](docs/images/hero-hermes-snapmaker.png)

[![tests](https://github.com/bbolinger/snapmaker-u1-toolkit/actions/workflows/tests.yml/badge.svg)](https://github.com/bbolinger/snapmaker-u1-toolkit/actions/workflows/tests.yml)

> Inspired by safety-staged agent workflows, this project applies the pattern specifically to the [Snapmaker U1](https://snapmaker.com/snapmaker-u1) — local slicing, visual previews, camera-gated checks, and explicit operator approval. Useful from the command line on its own; an AI agent like Hermes is the optional remote-control layer on top.

This is how AI should touch physical machines: **plan, explain, preview, ask, verify, then act only within a narrow approved boundary.**

**Single STLs and multi-part kits run one flow:** send a model or a zip of
STLs, answer one button form, and the toolkit slices, previews the plate from two
angles, and gates the print start behind a fresh bed photo and your explicit yes,
with a pre-start grace period and a model-free Telegram cancel as the final
backstop. Every safety claim is live-verified on real hardware. See the
[CHANGELOG](CHANGELOG.md).

---

## See it in action

A single local model takes a zip of eight STLs all the way to a printing plate in
about 160 seconds (with local LLM), entirely over Telegram and entirely on local hardware. It
measures each part, offers one button form for parts, tool, orientation, and
supports, slices, shows a top-down footprint next to a 3D plate view, captures a
fresh photo of the bed, and then waits. Nothing starts until a human replies
"yes." Then the U1 begins the print.

https://github.com/user-attachments/assets/f3acea60-4ebe-4059-8158-92abd207f4ec

---

## What This Is

A toolkit + workflow that turns "I have an STL, slice it for my U1" — or "I have a zip of twelve STLs" — into an auditable, operator-gated print job. **A single STL and a multi-part kit run the same flow** (a lone model is just a kit of one). On a tool-capable model the plan is collected in **one native button form**; on a small local model it falls back to a typed one-liner — the *safety boundary is identical either way*:

1. **Triage** the model (dimensions, triangle count, mesh validity)
2. **Orient** it — show both as-authored and Orca's auto-orient, with the real mesh-topology verdict (`floating cantilever` / `clean` / overhang layer fraction) so the operator picks based on Orca's actual call, not a face-angle approximation
3. **Tool / filament / preset / supports** — surface live U1 state (what's actually loaded), recommend, never assume
4. **Slice** through OrcaSlicer with the chosen profile, T0→T<chosen> rewriting, Snapmaker thumbnail injection, real Orca warnings surfaced
5. **Preview — two corroborating views**: a top-down footprint traced from the *sliced gcode* (the real toolpath) and an isometric 3D render of the *actual arranged, oriented parts*. Built from different sources, so if they agree you're seeing the truth; plus footprint dimensions + real Orca warnings
6. **Upload** to the U1's Moonraker storage with `print=false` (file lands; printer does NOT start)
7. **Bed-clear photo** captured fresh by the U1's onboard camera, surfaced to the operator
8. **One explicit bed-clear decision** — yes/no on the *fresh* bed photo (yes starts; no keeps the gcode staged, doesn't print)
9. **Start** — only after approval, only via a token handed off from the photo step (30-min TTL)
10. **Monitor** — first-layer photo (the earliest real tell a print is about to fail), last-layer check, completion — see [Always-on print monitoring](#always-on-print-monitoring--no-agent-required)

Steps 1–6 are useful as CLI utilities even if you never touch an AI agent. Steps 7–10 are where the "operator workflow" wrapping makes the difference between "AI presses print" and "AI safely shows you the print so you can press it."

### Multi-part kits + the unified flow
Send a **zip of STLs** — the common Printables shape — and the kit workflow
takes it from there:

1. **Ingest** every part (footprints measured, oversized parts flagged, hostile
   archives refused with a clean error instead of a crash)
2. **One consolidated decision form** — parts, orientation, tool, material,
   profile, supports. On a tool-capable model it renders as **native buttons**
   (one submit); on a small model, a typed one-liner in any order. Either way
   **a script parses and validates the answer; the model never interprets it**,
   and the answer content never rides through the model — only an opaque form
   id it can't corrupt. Conflicting or ambiguous input fails loudly, never a
   silent guess. For a single model, Orca's real orientation verdict rides in
   the form so you pick the pose Orca actually prefers.
3. **Arrange + slice** onto as many plates as the bed needs, with a
   gcode-extent guard that refuses any plate whose extrusion would leave the
   bed (built from a real incident, not a hypothetical)
4. **Upload all plates**, then run the same Stage 1/2 camera-gated start
   boundary on plate 1. Plates 2..N start from the Snapmaker app after it —
   the watchdog photographs every plate either way.

The operator's confirm rides on a single-use nonce baked into the emitted
command, so even the agent relaying your answers can't hand-assemble its way
past the boundary. A single STL is just a kit of one — same entrypoint,
auto-detected.

## Always-on print monitoring — no agent required

The start decision needs an LLM + your explicit approval. **Watching the print once it's running does not** — that part is three quiet cron jobs, no agent turn, no LLM in the loop at all.

**Why first-layer specifically matters:** it's the earliest real tell a print is about to fail — bed adhesion problems, warping, a shifted part, wrong Z-offset all show up in the first few layers, long before you'd otherwise notice. A last-layer photo confirms it finished; a first-layer photo is the one that could actually save you hours of wasted filament and time if you catch it early.

| Job | Cadence | What it does |
|---|---|---|
| `u1_last_layer_watch.py` | every 1 min | Snaps a photo at **first-layer** (layers 2–5, the bed-adhesion check), at **last-layer** (final ~6 layers), and after a **pause/resume** (an extra confidence check). Delivers each straight to Telegram. Auto-dims the cavity LED a few minutes after the job completes/errors/cancels. |
| `u1_print_watchdog.py` | every 5 min | Silent health poll across *any* active U1 print, not just Hermes-started ones. Alerts once per distinct issue, with cooldown so it never spams. |
| `u1_print_history.py` | every 5 min | Appends to a durable print ledger. |

All three run in Hermes' `no_agent` cron mode — a plain script invocation with no persona and no model call, so there's nothing for a weak or a strong model to get wrong, and nothing that can fabricate a milestone that didn't happen (the photo either exists or the job says nothing). They watch *every* active print, whether it was started through this toolkit, the Snapmaker app, or anything else touching the same Moonraker.

## What This Is Not

- **It is not an autonomous printer driver.** No agent in this stack can start a print without an operator yes/no on a real bed photo captured in-the-moment.
- **It is not a generic slicer wrapper.** Specific profile resolution, T0→T<n> rewriting, Snapmaker thumbnail injection, and Moonraker storage discipline are baked in for the U1.
- **It is not a multi-printer abstraction yet.** The safety model and event contract are portable in principle. The implementation is U1-specific by design until the U1 experience is solid.
- **It is not a Hermes-only project.** Hermes is the convenient remote-control layer. Every workflow step has a CLI form and JSON event stream — wrap it with whatever you want.

## Safety Model

Hermes — and any other AI agent layered on top — can recommend, explain, and prepare a print, but the U1 toolkit owns the final safety checks and will not perform printer-affecting actions without an explicit operator approval tied to a specific request ID.

The default lifecycle:

```text
read state → slice → preview → upload-only → operator approval → start → monitor
```

Actions that always require explicit operator confirmation:

- Starting a print
- Resuming or canceling a print
- Heating nozzle or bed
- Moving axes
- Clearing alarms
- Changing tool state
- Anything that affects the physical printer

The workflow fails closed. If a check is unsure, it stops and asks rather than guessing. Bed-clear verdicts come from the operator looking at a real photo, not from the toolkit deciding the bed is "probably fine." Slicer profile mismatches abort BEFORE the slice. Upload that hits a filename collision asks before overwriting.

None of this is aspirational: 703 tests run in CI on every change, and the
cancel chain is **live-verified on real hardware**, including a reproducible,
no-printer-needed drill anyone can run:
[docs/verify-cancel-hook.md](docs/verify-cancel-hook.md).

## The Three Layers

The toolkit ships as three layers that build on each other.

### 1. CLI mode — useful without Hermes

Scriptable, deterministic, single-purpose tools that a U1 owner can use directly:

- Slice + preview a model
- Inspect printer state, profiles, print history
- Generate orientation renders
- Upload a job with `print=false`
- Review G-code metadata before printing

These are designed for shell scripts, cron jobs, manual workflows. No AI required.

### 2. Operator workflow — the staged experience

A multi-step state machine that walks an operator through the print decision. Emits structured JSON events at every step, so any frontend (Telegram bot, web UI, custom integration) can wrap it without re-implementing the logic.

This is the core product. It's what makes the toolkit feel like a responsible assistant instead of a generic API wrapper.

### 3. Hermes mode — the remote-control layer

A bundled Hermes skill (`3d-printer-slicing-automation`) that lets a Telegram-bridged Hermes agent drive the operator workflow on the user's behalf. The agent:

- Surfaces the staged questions to the user verbatim
- Tool-calls the named scripts (never invents its own slicing path)
- Surfaces every preview and bed photo for visual approval
- Never decides bed-clear status on its own — the operator does, looking at a real photo

The skill is designed to work on small local models (`gemma4-26b-64k` and below) via [Ollama](https://ollama.com/). See [Using with Hermes](#using-with-hermes--install-the-bundled-skill) for the full setup.

## What the Operator Approves

Every operator decision is concrete and tied to a specific artifact:

| Decision | What the operator sees |
|---|---|
| Orientation | Source render + auto-oriented render + Orca's mesh-topology verdict |
| Tool / filament | Live U1 toolhead state ("T0: Generic white PETG (loaded)") |
| Preset | Recommended profile based on model class + your print history |
| Supports | Overhang verdict from a fast draft slice — Orca's real call, not face-angle |
| Upload | Three options: upload-only / upload+start gate / cancel |
| Filename collision | Three options: timestamped rename / overwrite / cancel |
| Kit decisions (v2.1.0) | One consolidated form (parts / orient / tool / material / profile / supports / action) — script-parsed, echoed back verbatim for confirmation before anything slices |
| Pre-print review (v2.2) | A `review.md` flight plan generated from the sliced gcode's own config block: what will print, the ~12 settings that matter, your decisions and overrides — bound to the plan's revision + hash so what you read is what prints |
| **Bed clear** | A **real, fresh photo** of the bed from the U1's onboard camera. The operator types yes/no. Default is no. |

If anything is unknown — printer state, tool, material, slicer metadata, bed visibility — the workflow stops and asks. No silent assumptions.

## What an approval looks like

Operator-facing approvals are tied to a specific `request_id` — the toolkit-generated identifier for the print job (shape `u1_YYYY_MMDD_xxxxxx`). The agent is required (per [`HERMES.md`](HERMES.md) Rule 7 and the bundled [skill contract](skills/3d-printer-slicing-automation/SKILL.md)) to include the `request_id` in every approval question. This makes the operator's "yes" unambiguous — it routes to that specific request, not "whatever was most recent."

A Telegram exchange after a `Stand.stl` was sent:

> **Bot:** Slicing complete for request `u1_2026_0627_1b977b`. Estimated time 1h 14m, 18g PETG, T1, 0.20mm Strength. Uploaded as `Stand.gcode`. Stage 1 captures the bed photo next.
>
> *(Stage 1 runs, photo arrives in chat)*
>
> **Bot:** Bed photo: `/opt/data/snapmaker_u1/bed_snapshot.jpg`
> Bed clear and you want to start request `u1_2026_0627_1b977b`? (yes/no)
>
> **You:** yes
>
> **Bot:** Started. Request `u1_2026_0627_1b977b` is now printing.

If a re-slice or plan change happened between the photo and the "yes," `can_start()` would refuse and the bot would re-ask with the new revision instead of starting on stale state. That's the safety property [`HERMES.md`](HERMES.md) Rule 8 ("Approvals are revision+hash bound") encodes.

## Quick Start

If you have a U1 reachable on the LAN and want to try a slice:

```bash
git clone https://github.com/bbolinger/snapmaker-u1-toolkit.git
cd snapmaker-u1-toolkit
python3 -m pip install -r requirements.txt

# Fetch Snapmaker's stock U1 profiles (~217 files)
python3 tools/fetch_snapmaker_profiles.py

# Slice a model — workflow asks for orient / tool / preset / supports / upload
python3 scripts/u1_slice_workflow.py path/to/your_model.stl --json-events --no-live-material
```

For the full install (interpreter selection, Hermes skill install, U1 connection setup), see [Setup](#setup) below.

For the design rationale, architecture, and acceptance criteria, see [`docs/DESIGN-CONTRACT.md`](docs/DESIGN-CONTRACT.md). For the public event contract (every event the workflow + audit log emit, with payload shapes), see [`docs/events.md`](docs/events.md).

## Setup

### Requirements

- Python 3.9 or newer
- `numpy` + `Pillow` — installed via `requirements.txt`
- [OrcaSlicer 2.4.0+](https://github.com/OrcaSlicer/OrcaSlicer) CLI binary (extracted AppImage path is fine; full install steps in [Headless slicing setup](#headless-slicing-setup-no-gui--scripted))
- Network reachability from your host to your U1's Moonraker port (default `7125`)

### Local model & serving requirements (form mode / button UX)

The button-based **form mode** (the rich Telegram UX in the demo) asks the local
model to emit one tool call. Small local models are inconsistent at tool-calling,
so form mode has hard serving requirements — verified end-to-end on
`gemma4-26b-64k` via [Ollama](https://ollama.com/) on an NVIDIA Quadro P6000:

- **Ollama 0.31.1 or newer.** Ollama 0.30.x has a gemma4 tool-call parser bug
  ([#15539](https://github.com/ollama/ollama/issues/15539),
  [#15798](https://github.com/ollama/ollama/issues/15798),
  [#15943](https://github.com/ollama/ollama/issues/15943)): the model's tool
  call leaks into the message *content* as raw template tokens (`<|channel|>`,
  `<|"|>`), the parser misses it, `finish_reason` is `stop`, and the agent
  stalls with no buttons.

- **Run the model at low temperature (~0.2) for tool turns.** Gemma's default
  Modelfile ships `temperature 1`, which is unreliable for tool calls (~2 of 3
  succeed in testing — one run in three strands the operator). A temp-0.2 variant
  is 3 of 3. Create one (it shares the same weights blob — no extra disk):

  ```bash
  printf 'FROM gemma4-26b-64k:latest\nPARAMETER temperature 0.2\nPARAMETER num_ctx 65536\n' \
    | ollama create gemma4-26b-64k-tool -f -
  ```

  then point your agent's model at `gemma4-26b-64k-tool`.

- **The toolkit already does its part.** The `kit_form` event carries only a short
  `form_id`; the full form definition is persisted to disk and loaded by the form
  plugin, so the model never has to reproduce a large nested schema in its tool
  call (what small models fail at). Nothing to configure — just don't downgrade
  the bundled `u1-form` plugin.

All three are needed together: on 0.30.8 the model failed even with the flat
`form_id` call; on 0.31.1 the flat call works, but only at low temperature is it
reliable. If tool calls still fail, fall back to **text mode**
(`--interaction-mode text`) — the staged one-question-per-turn flow uses only
simple `terminal` calls that even small models handle reliably.

> **Hardware note (Pascal / older GPUs).** Ollama 0.31's `cuda_v13` runtime
> dropped Pascal (compute capability 6.1); it falls back to the bundled
> `cuda_v12`, so a P6000 / GTX-10-series still works today — but a future Ollama
> that drops `cuda_v12` would break it. Benign `driverInitFileInfo ... result=11`
> lines at startup are that fallback, not a failure.

### Install

```bash
git clone https://github.com/bbolinger/snapmaker-u1-toolkit.git
cd snapmaker-u1-toolkit
python3 -m pip install -r requirements.txt
```

### Verify

```bash
python3 scripts/u1_slice_workflow.py --help
```

If you see argparse usage text, your environment is ready. If you see `ERROR: u1_slice_workflow.py needs numpy + PIL`, the workflow tells you exactly which interpreters it tried and how to fix — see the next section.

### Choosing a Python interpreter

The workflow needs `numpy` and `Pillow` on the Python that runs it. It auto-detects a working interpreter in this priority order — the first one that can `import numpy, PIL` wins:

1. `$U1_TOOLKIT_PYTHON` (your override; set to any path you like)
2. `/opt/hermes/.venv/bin/python` (Hermes-bundled venv — common on agent hosts)
3. `<repo>/venv/bin/python` (project-local venv — the recommended fresh install)
4. `<repo>/.venv/bin/python` (uv-/poetry-style hidden venv)
5. `/opt/homebrew/bin/python3` (macOS Homebrew on Apple Silicon — the M-series default)
6. `/usr/local/bin/python3` (macOS Homebrew on Intel — legacy install path)

If none has the deps, the workflow exits with a clear error listing each path it tried and concrete fix steps. You'll know exactly what to do next.

### Recommended: isolated project venv

Cleanest install for a fresh host, no clutter in your system Python:

```bash
cd snapmaker-u1-toolkit
python3 -m venv venv
venv/bin/pip install -r requirements.txt
export U1_TOOLKIT_PYTHON=$PWD/venv/bin/python
```

Add the `export U1_TOOLKIT_PYTHON=...` line to your shell rc (`~/.bashrc`, `~/.zshrc`) to make it permanent. The workflow respects this on every invocation.

### For Hermes users

Hermes typically ships with `numpy` + `Pillow` already in its bundled venv. Verify:

```bash
/opt/hermes/.venv/bin/python -c 'import numpy, PIL; print("ok")'
```

If that prints `ok`, the workflow's auto-detection finds it automatically — you don't need to set `U1_TOOLKIT_PYTHON`. The bundled Hermes skill is installable in one command:

```bash
hermes skills install bbolinger/snapmaker-u1-toolkit/skills/3d-printer-slicing-automation
```

See [Using with Hermes](#using-with-hermes--install-the-bundled-skill) below for what the skill does and how Hermes drives the workflow.

### Configure your U1

Connection details (host, port, data dir) live in [Configuration](#configuration) below. The toolkit honors env vars, then a JSON config file, then sane defaults — set whichever fits your host best.

### Deploy to runtime (Hermes users)

If you're driving the toolkit through Hermes, the bundled skill expects the workflow scripts to live at the runtime paths Hermes calls into. Deploy them:

```bash
bash deploy_to_runtime.sh
```

After copying files, the deploy script invokes the deployed workflow's `--help` to confirm the Python at the runtime location can actually start the workflow. On success: `✓ workflow starts cleanly` + `✓ Deploy complete`. On env failure (no Python with `numpy`+`Pillow` reachable): files are deployed but the script exits non-zero with the workflow's own diagnostic output — fix what it tells you, re-run.

Override target paths via env vars if your layout differs from the Hermes default:

```bash
U1_DEPLOY_SCRIPTS=/my/runtime/scripts \
U1_DEPLOY_TOOLS=/my/runtime/tools \
U1_DEPLOY_SKILL=/my/runtime/skill/.../3d-printer-slicing-automation \
U1_DEPLOY_PROFILES=/my/runtime/profiles \
bash deploy_to_runtime.sh
```

## End-to-end slice workflow
![Workflow preview render — auto-oriented mounting plate flat on bed, U-cradle upright; first-layer footprint parsed from real Orca G-code](docs/images/workflow-preview-corrected-orientation.jpg)

**Before your first slice**, populate the profile picker (one-time setup — see [Profile sources (v1.5.0)](#profile-sources-v150) below):

```bash
python3 tools/fetch_snapmaker_profiles.py            # Snapmaker U1 stock baseline
python3 tools/extract_profiles_from_printer.py       # extract YOUR successful prints
```

Without either, the workflow exits with a `setup_required` event and points you back at these scripts. Hit something the docs didn't cover? Check [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

Then the canonical STL/3MF → U1 path:

```bash
python3 scripts/u1_slice_workflow.py model.3mf
```

Agent/Telegram wrappers should use the event stream instead of re-implementing the workflow:

```bash
python3 scripts/u1_slice_workflow.py model.3mf --json-events
```

The workflow owns the full 10-step flow: triage, orientation choice, loaded filament/tool choice, preset choice, oriented render, support choice, slice, preview render, upload-only default, and optional camera-gated start. Render and slice both consume the same `oriented.stl`; Orca `--orient` only reports the best rotation, and this toolkit applies it.

Safe headless proof run (pass `--profile` slugs your picker actually has — list with `python3 scripts/u1_profile_picker.py`):

```bash
python3 scripts/u1_slice_workflow.py model.3mf \
  --tool T1 --material PETG --orient auto \
  --profile 0_20_strength_snapmaker_u1_0_4_nozzle \
  --supports auto --upload-only --yes
```

## Using with Hermes — install the bundled skill

After release, Hermes users can install the workflow guidance directly from this repo:

```bash
hermes skills install bbolinger/snapmaker-u1-toolkit/skills/3d-printer-slicing-automation
```

![End-to-end example with Hermes — model preview, Telegram operator conversation, AI-derived slice settings, and the actual printed part in hand](docs/images/end-to-end-example.jpg)

That skill tells Hermes to call `scripts/u1_slice_workflow.py`, ask the 10 questions instead of guessing, default to upload-only, and fail closed at the bed-clear start gate.

### Gotcha for skill writers: Hermes attaches files via bare paths in text, not a tool parameter

If you fork this skill or write your own, Hermes' platform gateways (Telegram, Discord, Signal, etc.) deliver media to the user by scanning the agent's reply text for **bare absolute file paths** ending in known media extensions and auto-attaching whatever exists on disk. There is **no** `files=[...]` tool parameter the agent needs to call. See `gateway/platforms/base.py:extract_local_files()` in Hermes 0.15.2 for the canonical implementation.

What this means for your skill prompt:

- ✅ Tell the agent: *"emit the absolute path bare in your reply text"*
- ❌ Do NOT tell the agent: *"attach the file via the reply tool's files parameter"*
- ❌ Paths inside backticks or fenced code blocks are skipped — the agent must emit them as bare text

This caught me out during the first v1.5.0 live test — the agent kept claiming it would "attach" renders but the gateway saw nothing to extract. See `TROUBLESHOOTING.md` for the full diagnosis if you hit the same.

## Optional: notify me when OrcaSlicer has an update

The toolkit ships a small checker that compares your installed `orca-slicer` version against the upstream latest release. **It does nothing unless you wire it into your scheduler.** Cloning the repo does not subscribe you to anything.

To enable, add one line to cron (Linux/macOS):

```
0 7 * * * /usr/bin/python3 /path/to/snapmaker-u1-toolkit/tools/check_for_updates.py
```

Behavior:
- **Silent when you're current.** No stdout → no cron email.
- **Single-line stdout when an update is available** — cron mails it via your usual cron-email setup. Example: `OrcaSlicer 2.4.1 available (you have 2.4.0). Patch (bug fixes, likely safe). Release notes: https://github.com/OrcaSlicer/OrcaSlicer/releases/tag/v2.4.1`
- **Refuses to query GitHub more than once per 24h** regardless of how often you invoke it (cache at `~/.cache/snapmaker-u1-toolkit/update-check.json`). `--force` overrides for one-off "tell me now" runs.
- **Returns silently when GitHub is unreachable or the binary isn't present.** Never breaks your cron with stray stderr.

Compatibility note: Snapmaker upstreamed the U1 vendor profile into OrcaSlicer 2.4.0, and `tools/fetch_snapmaker_profiles.py` pulls fresh stock profiles from that upstream — patch/minor upgrades should keep slicing U1 prints. Major-version bumps may change CLI flags or profile schema — re-run the EGO trimmer regression after upgrading. The notifier's risk label ("patch / minor / major") flags this in the alert text.

If your `orca-slicer` binary lives anywhere other than `/opt/data/tools/orcaslicer/squashfs-root/bin/orca-slicer` (Hermes-container default), pass the path explicitly OR set the `ORCA_SLICER_BIN` environment variable in your crontab, otherwise the script silently can't probe your installed version and you'll never see notifications.

CLI:

```
python3 tools/check_for_updates.py                                    # daily-cached check
python3 tools/check_for_updates.py --force                            # bypass cache, hit GitHub now
python3 tools/check_for_updates.py --orca-bin /path/to/orca-slicer    # one-off
ORCA_SLICER_BIN=/path/to/orca-slicer python3 tools/check_for_updates.py  # persistent env
```

## Maintainer helper: promote a tag to a GitHub Release

`git push origin vX.Y.Z` creates a Tag on GitHub but NOT a Release object — the Releases page won't see it until a Release is explicitly created. `tools/create_release_from_tag.py` closes that gap by reading the tag's commit message and publishing it as the Release notes.

```
# After tagging + pushing a new version:
export GITHUB_TOKEN=<your PAT with `repo` scope>

python3 tools/create_release_from_tag.py                # promote the latest tag
python3 tools/create_release_from_tag.py v1.4.5         # promote a specific tag
python3 tools/create_release_from_tag.py --all-missing  # backfill every tag without a Release
python3 tools/create_release_from_tag.py v1.4.5 --update  # replace existing Release's notes
```

Idempotent: a tag that already has a Release is skipped unless `--update` is passed. Repo slug is auto-detected from `git remote get-url origin`. Token sources (first match wins): `--token`, `GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_PAT`.

## What's in here

| Script | What it does |
|---|---|
| `u1_config.py` | Centralized host/port resolution (env > JSON > default) |
| `u1_camera.py` | Camera capture via Snapmaker-specific websocket `camera.start_monitor`; auto-on/restore the cavity LED for each capture via `u1_led.photo_wrap` |
| `u1_led.py` | Cavity LED helper — CLI (`status / on / off / set --r/g/b/w / is-on`) and `photo_wrap()` context manager. The U1's cavity LED is white-only (`white_pin: PA10` in printer.cfg); the 4-channel API matches Klipper's interface, only WHITE has visible effect |
| `u1_toolmap.py` | Multi-tool material gate — declared vs detected material check |
| `u1_preflight.py` | Combined Moonraker state + camera freshness packet for "is it safe to start?" |
| `u1_upload_gcode.py` | Upload-only (`print_started=false`) with gates: idle state + tool/material match |
| `u1_slice_workflow.py` | Canonical v1.4.0 end-to-end STL/3MF workflow: orient → render → slice → preview → upload-only/start gate |
| `u1_last_layer_watch.py` | Watch active print for first-layer (2–5) and "last ~6 layers" milestones, snap photos; also auto-dims the cavity LED 5 minutes after `complete`/`error`/`cancelled` (`U1_LED_OFF_DELAY_SEC` overrides) |
| `u1_print_watchdog.py` | Quiet 20-min health watcher with cooldown to avoid notification spam |
| `u1_print_history.py` | Append-only JSONL print ledger + canonical upserted JSON |
| `snapmaker_u1_status.py` | Read-only status probe |
| `snapmaker_u1_snapshot.py` | Websocket camera trigger helper |
| `tools/extract_profile_from_gcode.py` | One-shot extractor — turn a successful G-code into Snapmaker Orca process + filament JSONs |
| `tools/extract_profiles_from_printer.py` | Auto-pull recent G-codes off your U1 over Moonraker, run the extractor against each — one command, gets your real print history into `profiles/from-printer/` |
| `tools/fetch_snapmaker_profiles.py` | Fetch Snapmaker's official U1 stock profiles (machine + process + filament) from the upstream `Snapmaker/OrcaSlicer` GitHub repo into `profiles/snapmaker-stock/` (v1.5.0) |
| `tools/gcode_inject_thumbnail.py` | Add Snapmaker-app preview thumbnails to headless-sliced G-code (PIL renderer + base64 splice) |
| `tools/render_stl_orientation.py` | Pre-print orientation review — 4-view PNG (isometric, front, side, top) with overhang faces highlighted in orange |

## Safety model — concrete details

The high-level model is in [Safety Model](#safety-model) above. This is the per-action breakdown.

```
read → slice → upload (print=false) → operator-approved start → quiet monitor
```

**Allowed automatically**:
- Read printer state through Moonraker/Klipper endpoints
- Read toolhead/extruder/material/feed sensor state
- Read G-code metadata
- Upload/stage G-code with `print=false`
- Trigger fresh camera snapshots
- Send operator alerts for milestones or issues
- Record local print history

**Requires explicit operator approval**:
- Start a print
- Resume a paused print
- Cancel/stop a print
- Any movement/heating command

### Test-operator fence
`u1_print_start_gate.py` refuses Stage 2 (real printer start) BEFORE any Moonraker
call if the `--operator` argument starts with an unambiguously test-flavored
prefix: `smoke:`, `test:`, `dry:`, `mock:`, or `fixture:` (case-insensitive).

The gate returns a `gate_refused_test_operator` payload and audits the refusal.
No HTTP call reaches the printer, no photo is taken, no preflight runs. This
closes the "smoke-test accidentally runs a real print" failure that motivated
the fence.

`u1_kit_workflow.py` also prints a highly visible TEST MODE banner to stderr
at the top of every invocation under a test-flavored operator, so the tester
sees the gate will refuse before any command runs.

If you hit the fence for a legitimate print — e.g. you set `U1_OPERATOR=test:homelab`
in your environment because that's your naming convention — change the operator
string to something without a test prefix (`homelab`, `human:homelab`, etc.).
The fence is deliberately narrow: identity strings starting with `dev:`, `ci:`,
`telegram:`, `discord:`, `human:`, or bare names all proceed normally.

Two sharp edges, decided deliberately (rc2):

- An explicit `--operator` now rides **every** command the kit workflow
  emits, so a `smoke:*` identity can't silently drop back to your
  production `U1_OPERATOR` mid-chain. Env-resolved identity is never baked
  into commands (replay-safe).
- An **unset** operator (`unknown:gate`) passes the fence — refusing every
  bare CLI run would tax legitimate local use — but leaves a loud
  `gate_operator_unknown` audit row. If your smoke tests might run with no
  operator set, set one (`smoke:whatever`) so the fence can catch them.

### Pre-start grace period + Telegram cancel button
After every safety check passes and before the gate HTTPs the printer's
`/printer/print/start`, there's a **grace window** (default 120s, configurable
via `U1_GRACE_PERIOD_SECONDS` env var or `--grace-seconds N`). During the
window the gate:

1. Fires an operator notification via `$U1_GRACE_NOTIFY_CMD` (a shell
   command you configure). Env vars `U1_REQUEST_ID`, `U1_FILENAME`,
   `U1_GRACE_SECONDS`, `U1_CANCEL_MARKER`, `U1_OPERATOR` are exported so
   your command can templatize them.
2. Polls a per-request marker file at `<out_dir>/pre_start_cancel.marker`
   once per second.
3. If the marker appears → refuses the print, no HTTP call, audit
   `pre_start_grace_cancelled`.
4. If it doesn't appear before the window expires → proceeds, audit
   `pre_start_grace_period_expired`.

**Hermes users get a one-tap cancel button:** ship a Telegram notification
via `hermes send` and a Gateway hook that touches the marker when you reply
`cancel <code>` in the DM. Zero LLM, zero agent-loop — the hook runs directly
in the Hermes gateway process.

Install:

```bash
# One-time — installs the hook into Hermes' actual HOOKS_DIR + restarts gateway
bash tools/install_hermes_cancel_hook.sh

# Per environment — point the gate at the notify script
export U1_GRACE_NOTIFY_CMD=/absolute/path/to/tools/u1_grace_notify_hermes.sh

# REQUIRED for Hermes users: the gate spawns the notify script in a
# stripped subprocess env where `hermes` is usually NOT on PATH. Without
# this, the notify fails (audited as pre_start_grace_notify_failed — the
# wait still runs fail-open, but no Telegram warning is sent).
export HERMES_BIN=/opt/hermes/.venv/bin/hermes
```

The notify script sends a Telegram DM. Reply `CANCEL` (or `STOP` or
`ABORT`, case-insensitive) within the window and the print aborts before
any HTTP call — a bare keyword cancels **every** active grace window;
`cancel <code>` (the code is the last 6 chars of the request id, shown in
the DM) cancels **only** that request, and a code that matches nothing
cancels nothing. Trailing punctuation is fine — `CANCEL!!!` fires (urgency
isn't ambiguity) — but extra words never match: "cancel that plan" is safe
from unintended cancels. Multi-request setups (two concurrent grace windows)
each write their own pending-state file so they don't race each other.

Honesty guard: the DM only promises reply-to-cancel when the installer's
receipt file shows the hook actually loaded — otherwise it gives the SSH
`touch <marker>` fallback instead of a reply that would silently do
nothing.

Cancelled by mistake, or the bed was actually fine? The refusal payload
carries a `recovery.stage1_command`: the slice and upload are still
valid, so restarting costs one fresh bed photo + one fresh yes — not a
workflow re-run.

Verify the whole chain (including a zero-risk drill that needs no
printer): [docs/verify-cancel-hook.md](docs/verify-cancel-hook.md).

Opt-out for power users at the printer: `U1_GRACE_PERIOD_SECONDS=0`
disables the window. `U1_GRACE_NOTIFY_CMD` unset disables the
notification but the wait still runs (in that case an SSH `touch
<marker>` cancels).

## Quick start — per-platform commands

The 30-second flavor is in [Quick Start](#quick-start) above. This section has the per-platform install + first-status-probe commands.

### Linux / macOS

```bash
git clone https://github.com/bbolinger/snapmaker-u1-toolkit.git
cd snapmaker-u1-toolkit
cp .env.example .env
# edit .env — set SNAPMAKER_U1_HOST to your U1's LAN IP

# .env is auto-loaded the first time any script reads config — no
# 'source .env' needed. Explicit env vars still win if set in the shell.

# read-only status probe (no risk):
python3 scripts/snapmaker_u1_status.py

# combined preflight packet:
python3 scripts/u1_preflight.py

# upload a G-code file (does NOT start the print):
# Material expectation is asserted at upload time; the intended tool is
# auto-detected from the G-code's T0/T1/T2/T3 startup command.
python3 scripts/u1_upload_gcode.py /path/to/file.gcode --material PETG

# same upload, but inject a Snapmaker-app preview thumbnail from the source STL first:
python3 scripts/u1_upload_gcode.py /path/to/file.gcode --material PETG \
    --stl /path/to/model.stl   # fail-closed: if injection fails, upload is refused
```

### Windows (PowerShell)

```powershell
git clone https://github.com/bbolinger/snapmaker-u1-toolkit.git
cd snapmaker-u1-toolkit
Copy-Item .env.example .env
# edit .env — set SNAPMAKER_U1_HOST to your U1's LAN IP

# Same .env auto-load applies. If you'd rather set env vars explicitly:
#   $env:SNAPMAKER_U1_HOST = "192.168.1.100"

# read-only status probe (no risk):
python scripts\snapmaker_u1_status.py

# preflight + upload flows mirror the Linux examples above
python scripts\u1_upload_gcode.py C:\path\to\file.gcode --material PETG
```

On Windows the data dir defaults to `C:\Users\<you>\.local\share\snapmaker-u1`
(no `/opt/data` auto-detection). Override with `$env:SNAPMAKER_U1_DATA_DIR` if
you'd rather keep state under `%APPDATA%` or another path.

## Configuration

`u1_config.py` resolves two things — the **connection** to the printer, and
the **data dir** where runtime state lives (configs, photos, ledgers).

### Connection (host/port)
1. **Environment variables**: `SNAPMAKER_U1_HOST`, `SNAPMAKER_U1_PORT`
2. **JSON file**: location from `SNAPMAKER_U1_CONFIG` env, default `<data-dir>/u1_config.json` (contains `{"host": "...", "port": 7125}`)
3. **Hardcoded default**: port 7125 only — host is required

### Data dir (where runtime artifacts live)
1. **`SNAPMAKER_U1_DATA_DIR`** env var (explicit override)
2. **`/opt/data/snapmaker_u1`** if it exists (auto-detects Hermes-style installs — for the agent setup these scripts came from)
3. **`~/.local/share/snapmaker-u1`** (community default, follows XDG Base Dir)

All host/port/data-dir lookups happen on first call — `import u1_toolmap` (or
any other script) never touches disk for config. The lookup only fails when
you actually run a command without any configuration.

See `.env.example` for a starting template.

### Cavity LED auto-control
The U1's `cavity_led` is white-only — Snapmaker's shipped `printer.cfg`
defines it as `[led cavity_led] / white_pin: PA10`, no R/G/B. Klipper's
`[led]` interface exposes all four channels regardless, but only the W
channel is physically wired. The toolkit drives the LED in two places
so the operator doesn't have to think about it:

- **Every camera capture** (`u1_camera.py photo`, and therefore every
  milestone photo from `u1_last_layer_watch.py`) is wrapped in a
  `u1_led.photo_wrap()` context manager:
  - LED already on → no change, no flicker.
  - LED off → turn on white (W=1), settle ~300 ms for the camera's
    auto-exposure, capture, then restore the LED to off.
- **5 minutes after a print finishes** (`print_state` enters `complete`,
  `error`, or `cancelled`) the LED is turned off, once per print. If you
  manually turn it back on, it stays on — the watcher dedups by
  `job_key = filename|total_layer` and won't re-fire for the same print.

**Tuning / disabling:**

- `U1_LED_OFF_DELAY_SEC=N` env var — grace window before auto-off. Default
  `300`. Set `0` for immediate. Set a large value (e.g. `86400`) to
  effectively disable the auto-off without removing the wiring.
- The wiring is **fail-soft**: if `cavity_led` isn't configured on your
  printer (or the LED endpoint errors), the LED helper logs to stderr and
  the photo/watcher keeps doing its primary job.
- Manual control via the CLI: `u1_led.py status / on / off / set --r --g --b --w`.

**Why:** photos taken at first/last-layer/post-resume milestones need
the LED on to be useful, but leaving the cavity bright forever after a
finished print is wasteful and surprising. The 5-minute grace gives you
time to inspect the bed before it goes dark.

## Reference docs

Real reverse-engineering notes from getting these scripts working — the kind of stuff Snapmaker doesn't document publicly:

| Doc | Topic |
|---|---|
| `references/snapmaker-u1-lan-probe-2026-06.md` | Open ports, working endpoints, API key handling |
| `references/snapmaker-u1-camera-websocket.md` | Snapmaker-specific `camera.start_monitor` websocket method |
| `references/snapmaker-u1-headless-slicing-and-toolhead-notes-2026-06.md` | OrcaSlicer CLI for headless slicing, tool naming gotchas |
| `references/snapmaker-u1-filament-detection-api-2026-06.md` | Filament presence/material detection objects |
| `references/snapmaker-u1-printhead-numbering-and-start-workflow-2026-06.md` | T0..T3 mapping to `extruder`..`extruder3` |
| `references/snapmaker-u1-last-layer-photo-watch-2026-06-21.md` | Last-layer event detection for milestone photos |
| `references/snapmaker-u1-toolmap-script-2026-06.md` | Material-gate design rationale |
| `references/snapmaker-u1-orca-moonraker.md` | OrcaSlicer + Moonraker integration |
| `references/snapmaker-u1-research.md` | First-pass research summary |

## Profile sources
**The toolkit no longer ships default profiles.** A fresh install has an empty picker. Profiles come from one of three sources you populate yourself, scanned in priority order:

| Source dir | Populated by | Purpose | Priority |
|---|---|---|---|
| `profiles/from-printer/` | `python3 tools/extract_profiles_from_printer.py` | Profiles extracted from your printer's recent G-code history. Physics-validated — every setting produced a successful print. | Highest |
| `profiles/user/` | The operator, manually | Hand-tuned overrides + custom variants you want to keep stable across stock refreshes | Middle |
| `profiles/snapmaker-stock/` | `python3 tools/fetch_snapmaker_profiles.py` | Snapmaker's official U1 profiles, pulled fresh from the Snapmaker/OrcaSlicer upstream repo (~217 files: every nozzle size + layer height + Snapmaker-tuned filament) | Lowest (universal baseline) |

All three are listed in `.gitignore` — they're per-user, not redistributed.

### First-run setup

```bash
# Pull Snapmaker's official U1 baseline (~217 files, one-time):
python3 tools/fetch_snapmaker_profiles.py

# Extract whatever you've actually printed successfully so far:
python3 tools/extract_profiles_from_printer.py
```

Both are idempotent — re-run anytime to pick up Snapmaker upstream updates or fresh prints from your printer's history. Snapmaker stock gives you the universal U1 baseline; extracted profiles reflect what you've validated on your hardware.

Without either, the workflow fails closed at analysis time with a clear `setup_required` event pointing you back here. Hermes agents surface that error verbatim.

### Why ship empty
Earlier versions shipped 13 personal community profiles in `profiles/` as defaults. They were tuned for one bed surface (Textured PEI), one bed temp, specific filament brands (SUNLU PETG, HF White PETG), specific tool assignments. Running them silently on another U1 with different filaments or a different bed surface could ruin prints — and the toolkit had no way to warn the user that the profile underneath didn't match their setup.

v1.5.0 moves those personal templates to `examples/profiles/` and points the picker at three honest sources: Snapmaker upstream (universal baseline), your printer's history (physics-validated on your hardware), and your own hand-tuned profiles. The agent's *Preset?* prompt now annotates each option with `source`, `has_supports` (read from the JSON's `enable_support` field), and `supports_status` (does picking "Add supports" auto-promote to a `_supports` sibling, already encode supports, or fail with a `no_supports_variant` warning).

### Supports auto-detection
Profiles are JSON-typed for supports — the picker reads each profile's `enable_support` field and annotates the option with `has_supports: true/false`. The agent's *Preset?* prompt also carries a `supports_status` that pre-warns the user before the *Supports?* question:

- `"self"` → preset already encodes supports; "Add supports" is a no-op for them
- `"<variant_name>"` → if user picks "Add supports", workflow auto-promotes to this same-source sibling and emits a `preset_promoted` event
- `null` → no same-source supports sibling exists (or multiple ambiguous candidates). Workflow emits a `warning` event with `kind:no_supports_variant` and slices without supports — agent surfaces it before the user trusts the preview

Why "same-source exactly one"? Snapmaker stock has multiple Support flavors at the same layer height (`0.20 Support`, `0.20 Support W`, `0.20 Bambu Support W`) — auto-promote can't pick one; the user has to.

### Why build your own (vs. just importing the examples)

Profile-as-data-from-real-prints means every setting is *physics-validated* — it produced a completed print on actual hardware. But the validation is environment-specific:

- Different bed surface (smooth PEI, garolite, glass) → different first-layer temp / bed temp / Z-offset
- Different filament brand → different optimal nozzle temp (PETG ranges 230–260°C across brands)
- Different tool assignment → e.g. your PLA is in extruder0, mine in extruder2
- Different exhaust/enclosure → affects warping defaults

Importing someone else's profiles is fine as a starting point; running them as gospel on a different setup will give you mediocre prints.

### Build per-extruder, per-filament profiles from your own print history

This is the recipe used to bootstrap the included community profiles. It
only takes one good print per filament-type-per-extruder slot.

**The fastest path — one command:**

```bash
python3 tools/extract_profiles_from_printer.py
```

That connects to your U1 (via `SNAPMAKER_U1_HOST` / `.env`), pulls the 5
most recent G-codes, runs the extractor against each, and drops process +
filament JSONs into `profiles/from-printer/` — with multi-tool metadata
sliced down to the actual tool each print used (so the filament profile
for a T1 PETG print isn't polluted by T0/T2/T3 settings).

Tweaks: `--list` to see what's on the printer first, `--file "<exact gcode>"`
to pick a specific one, `--limit N` to grab more, `--vendor SUNLU` to
override the often-generic vendor field, `--output-dir <path>` to write
elsewhere.

**The longhand recipe** — same outcome, manual steps:

1. **Print once with Snapmaker's defaults** — get a clean part, no warping/stringing/under-extrusion, on your bed surface and filament. Just enough to call it "good enough to use as a baseline."
2. **List successful prints via Moonraker**:
   ```
   curl http://YOUR_U1:7125/server/files/list?root=gcodes
   ```
3. **Download the G-code** and parse the `; key = value` metadata block at the top. The key ones:
   ```
   ; filament_type, filament_settings_id
   ; print_settings_id
   ; layer_height, first_layer_height
   ; nozzle_temperature, first_layer_temperature
   ; bed_temperature, first_layer_bed_temperature
   ; curr_bed_type
   ; sparse_infill_density, wall_loops
   ; nozzle_diameter
   ```
4. **Build a flattened process JSON** (see `examples/profiles/community_merged_*.json` for shape) and a matching filament JSON (see `examples/profiles/community_generic_petg_*.json`). Name them with the extruder + filament so you don't confuse yourself: e.g. `myprinter_extruder1_sunlu_black_petg.json`.

   **Or run the included extractor** to do steps 3-4 in one go:
   ```bash
   python3 tools/extract_profile_from_gcode.py my_good_print.gcode \
       --process-out  profiles/myprinter_extruder1_petg_process.json \
       --filament-out profiles/myprinter_extruder1_sunlu_black_petg_filament.json \
       --process-name  "My 0.20 PETG Extruder1" \
       --filament-name "My PETG Extruder1" \
       --vendor SUNLU --brand-label "SUNLU Black"
   ```
   It parses the slicer's `; key = value` metadata block, emits a flat process JSON + a list-shaped filament JSON in Snapmaker Orca's expected shape, and lets you override `filament_vendor` (G-code often says "Generic"). Pass `--metadata-only` to inspect the raw parsed keys without writing files.
5. **Track per-extruder mapping in `u1_tool_material_map.json`** so the toolmap gate enforces correct slot assignment:
   ```json
   {
     "tools": {
       "extruder":  { "material": "PLA",   "label": "Polymaker PolyLite Black" },
       "extruder1": { "material": "PETG",  "label": "SUNLU Black PETG" },
       "extruder2": { "material": "PETG",  "label": "HF White PETG" },
       "extruder3": { "material": "PLA",   "label": "Polymaker PolyLite Grey" }
     }
   }
   ```

The toolmap gate (`u1_toolmap.py`) then prevents you from accidentally slicing a job for PETG and uploading it against the slot loaded with PLA.

### Reference: example community profiles in `examples/profiles/`

The 13 profiles used during development live in `examples/profiles/` as a shape reference. They're MIT-licensed and show what a working community-tuned profile looks like for the U1. **Do not use them as defaults** — they assume Textured PEI + specific filament brands. If you happen to share that setup, copy them into `profiles/user/` and they'll appear in the picker.

The naming convention so you can see the pattern:

| Pattern | Meaning |
|---|---|
| `community_016_optimal_*` | 0.16mm layer, optimal preset, process profile |
| `community_020_strength_*` | 0.20mm layer, strength preset (6 walls, 25% infill) |
| `community_*_supports` | + tree/auto supports enabled |
| `community_*_gyroid` | + gyroid infill pattern |
| `community_*_fuzzy_external` | + fuzzy skin on outer walls |
| `community_generic_petg_*` | Filament profile for PETG |
| `community_*_sunlu_black_*` | SUNLU brand-specific (240°C first layer) |
| `community_*_hf_white_*` | High-flow white PETG variant |
| `community_merged_*` | **Flattened** — works for headless CLI slicing |
| `community_*_override` | Inherits from official — GUI only |

Diff against the official Snapmaker preset chain is ~93% identical; deltas are tuning choices that came from real prints (lower prime-tower waste, arachne walls, brand-specific PETG temps).

Use them as **templates** to copy + modify for your own setup. Don't blindly import.

| File | Type | Use case |
|---|---|---|
| `community_merged_016_optimal_u1_textured_pei.json` | process | **Start here.** Flattened 0.16 Optimal, no inheritance — works headless |
| `community_016_optimal_u1_textured_pei.json` | process | Standalone 0.16 Optimal |
| `community_016_optimal_u1_textured_pei_override.json` | process | Inherits-from-official override |
| `community_016_optimal_*_fuzzy_external*.json` | process | Fuzzy/staggered seam variants |
| `community_020_strength_u1_textured_pei.json` | process | 0.20 Strength preset |
| `community_020_strength_supports_*.json` | process | Strength + supports |
| `community_020_strength_gyroid*.json` | process | Strength with gyroid infill |
| `community_generic_petg_u1_textured_pei.json` | filament | Generic PETG (255°C first layer) |
| `community_generic_petg_sunlu_black_*.json` | filament | SUNLU Black PETG (240°C first layer) |
| `community_generic_petg_hf_white_*.json` | filament | High-flow White PETG |

Diff against official ≈ 93% identical; deltas are documented tuning choices, not regressions.

### Importing profiles into OrcaSlicer (GUI)

1. Open OrcaSlicer → top-right gear → "Configuration / Profiles"
2. Drag-and-drop the desired `.json` file into the profiles panel, OR copy to the system config directory for your slicer:
   - **Upstream OrcaSlicer** (recommended): `~/.config/OrcaSlicer/system/Snapmaker/process/` (or `filament/`) on Linux/macOS; `%APPDATA%\OrcaSlicer\system\Snapmaker\process\` on Windows
   - **Snapmaker fork** (if you're using `snapmaker-orca` instead): `~/.config/SnapmakerOrca/system/Snapmaker/process/` / `%APPDATA%\Snapmaker_Orca\system\Snapmaker\process\`
3. Restart OrcaSlicer
4. Select the Community profile from the dropdown when slicing

## Headless slicing setup (no GUI / scripted)

Use this if you're slicing from CLI in a container, CI pipeline, or agent workflow.

### Use upstream OrcaSlicer, not the Snapmaker fork

> **Important**: use **upstream [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer)
> v2.4.0+**, not Snapmaker's fork. Snapmaker upstreamed the U1 vendor profile
> into upstream OrcaSlicer 2.4.0, so it has full U1 support — and its CLI is
> the better-supported headless path. The Snapmaker fork's Windows CLI has
> been observed to segfault when slicing with these profiles (verified
> `snapmaker-orca v2.3.4` Windows can exit code `-1073741819` on some models).

### Install — Linux (extracted AppImage)

```bash
# Download upstream OrcaSlicer Linux AppImage
wget https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v2.4.0/OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.0.AppImage \
  -O ~/orcaslicer.AppImage
chmod +x ~/orcaslicer.AppImage

# Extract instead of mounting (containers without FUSE)
mkdir -p ~/orcaslicer-install && cd ~/orcaslicer-install
~/orcaslicer.AppImage --appimage-extract
# Creates ./squashfs-root/

# Some minimal distros are missing GUI/runtime libs Orca expects.
# If you hit "libGL.so.1 not found" or similar:
mkdir local-libs && cd local-libs
apt-get download libgl1 libegl1 libxkbcommon0 libwayland-client0 libnss3 \
                 libasound2 libgtk-3-0 libdbus-1-3 libsecret-1-0
for d in *.deb; do dpkg-deb -x "$d" .; done
```

### Install — Windows (portable zip, no installer needed)

```powershell
# Download upstream OrcaSlicer Windows portable
Invoke-WebRequest -Uri https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v2.4.0/OrcaSlicer_Windows_V2.4.0_portable.zip `
    -OutFile $env:TEMP\OrcaSlicer.zip
Expand-Archive $env:TEMP\OrcaSlicer.zip -DestinationPath $env:TEMP\orca240

# The CLI binary lives at $env:TEMP\orca240\orca-slicer.exe
```

### Slice a single STL — the 3-profile chain

Headless slicing needs **three** profiles in a specific load order:

1. **Machine** — the printer definition (this repo bundles a flattened standalone copy)
2. **Process** — layer height, walls, infill, supports
3. **Filament** — material, temps, retraction

> **Pass each profile via its own `--load-settings` flag** (not one flag with
> semicolon-separated paths). Both forms are documented in OrcaSlicer, but
> the dual-flag form is the one verified-working in our test runs (Hermes
> Windows) and avoids quoting foot-guns on PowerShell.

```bash
# Linux
ORCA=$HOME/orcaslicer-install
PROFILES=$(pwd)/profiles

LD_LIBRARY_PATH="$ORCA/local-libs/usr/lib/x86_64-linux-gnu:$ORCA/squashfs-root/usr/lib:$ORCA/squashfs-root/usr/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH" \
  $ORCA/squashfs-root/bin/orca-slicer \
  --load-settings "$PROFILES/machine/snapmaker_u1_0_4_nozzle.json" \
  --load-settings "$PROFILES/community_merged_016_optimal_u1_textured_pei.json" \
  --load-filaments "$PROFILES/community_generic_petg_u1_textured_pei.json" \
  --outputdir ./output \
  --slice 0 \
  my_model.stl
```

```powershell
# Windows (PowerShell)
& "$env:TEMP\orca240\orca-slicer.exe" `
  --load-settings "profiles\machine\snapmaker_u1_0_4_nozzle.json" `
  --load-settings "profiles\community_merged_016_optimal_u1_textured_pei.json" `
  --load-filaments "profiles\community_generic_petg_u1_textured_pei.json" `
  --outputdir .\output `
  --slice 0 `
  my_model.stl
```

Sliced G-code lands in `./output/plate_1.gcode`.

> **Why the bundled machine profile?** Upstream Orca's bundled U1 profile
> inherits from `fdm_U1` → `fdm_toolchanger` → `fdm_klipper`. Loading the
> bundled vendor copy via CLI requires Orca to find every parent in its
> install resources, which is fragile across platforms. The repo's
> `profiles/machine/snapmaker_u1_0_4_nozzle.json` is **fully flattened**
> — every inherited field merged into one standalone file. Headless CLI
> sees one file, gets the complete machine definition, no resolution
> magic needed. Derived from upstream OrcaSlicer's `Snapmaker/machine/`
> vendor profiles (AGPL-3.0, contributed by Snapmaker).

### Headless profile-loading pitfall (READ THIS)

OrcaSlicer's bundled Snapmaker process profiles **do not always resolve inheritance correctly via CLI**. Symptoms seen in testing:

- `filament_settings_id` says PETG but `filament_type` becomes PLA → wrong temps
- Layer-height preset of 0.16 produces G-code with `layer_height = 0.2`
- Bed/nozzle temps default to PLA-safe values regardless of selected filament

**Workaround**: use profiles whose inheritance chain CLI can resolve. Three good options today:

- `tools/extract_profiles_from_printer.py` writes **flat process JSONs from your successful prints** (no inheritance) — physics-validated AND CLI-safe by construction. Best default.
- `tools/fetch_snapmaker_profiles.py` pulls Snapmaker's upstream stock — CLI resolves these against the bundled OrcaSlicer install when the install matches the stock branch.
- `examples/profiles/community_merged_*` (in `examples/`) is the legacy flat-profile shape; if you're handwriting your own, follow that pattern. The `_override` variants only work in the GUI where Orca resolves the official base profile. The same flatness logic applies to the bundled machine profile above.

### Pre-print orientation review

Before you slice, ask the question every operator forgets: *is this the right
orientation, and where will it need supports?* The orientation renderer
gives you a 4-panel image showing isometric / front / side / top views with
all downward-facing triangles highlighted in orange — those are the faces a
slicer will warn about.

```bash
pip install Pillow numpy  # one-time (same deps as the thumbnail tool)

python3 tools/render_stl_orientation.py model.stl \
    --out orientation.png \
    --title "Orbital sander vacuum attachment"
```

Output is a single PNG with header text (bounding-box dims, Z range, count
of overhang triangles) and the 4 views. Tunable via `--overhang-threshold`
if your slicer/material is more or less paranoid than the default (-0.3 ≈
17° below horizontal).

### Add a Snapmaker-app preview thumbnail

OrcaSlicer's CLI path doesn't render thumbnails (GUI-only — verified with `--debug 5`, no GL/xvfb workaround helps). Without them, the Snapmaker app shows a generic icon for every print. Use the included tool to splice PrusaSlicer/Orca-format thumbnail blocks into the G-code post-slice:

```bash
pip install Pillow numpy  # one-time

python3 tools/gcode_inject_thumbnail.py \
    --stl my_model.stl --gcode output/plate_1.gcode \
    --sizes 48x48,300x300 --in-place
```

Runs an isometric projection of the STL through PIL (Lambertian-shaded triangles, painter's algorithm), base64-encodes the PNGs, and splices `; thumbnail begin … ; thumbnail end` blocks into the G-code header. Idempotent — re-running replaces existing blocks, not stacks them. Moonraker + Snapmaker app parse them as standard previews.

### Validate G-code before upload

```bash
grep -E '^; (filament_type|layer_height|first_layer_temperature|bed_temperature) ' output/plate_1.gcode
```

Expected output for the merged 0.16 Optimal PETG profile:

```
; filament_type = PETG
; layer_height = 0.16
; first_layer_temperature = 255
; bed_temperature = 80
```

If any of those are wrong, the CLI didn't load your profiles correctly — fix before uploading.

## Running the tests

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pytest
pip install Pillow numpy   # only needed for the thumbnail-injector tests
pytest -v
```

703 tests covering: config resolution (incl. 3-tier data-dir, `.env`
auto-loader with quoted/commented/walk-up edge cases, import-without-config
regression lock, and a smoke-runner that exercises every script's `main()`
to catch leftover undefined refs), material gate (incl. fail-closed on
corrupt map), upload pre-checks, G-code metadata parsing, print-history
ledger (incl. atomic-write contract + tmpfile cleanup on failure), profile
extraction (incl. multi-tool slice handling — `PETG;PETG;PLA;PLA` →
right value for the actual tool), thumbnail injection, upload-time
thumbnail wiring, status-probe `safe_to_upload` parity with the actual
upload gate, preflight `--host` override correctness, STL parsing + view
rotations + overhang detection + 4-view orientation sheet rendering,
bundled machine-profile completeness (standalone, klipper gcode flavor,
4 extruders, required slicing fields), printer-side profile extraction
(Moonraker list + download mocked, friendly errors).

Tests use mocked Moonraker responses — no real printer required. The
thumbnail-injection tests `importorskip` PIL/numpy, so they're harmless
to omit if you only want to run the safety-script tests.

## Release validation

Each tagged release is install-validated end-to-end before publish — clone,
test suite, script-help smoke, and the active-print upload-gate safety
check (the latter against a mocked Moonraker so no live printer is
touched). The validation surfaces install/docs gaps a fresh-clone user
would hit.

| Tag | Tooling | Platform |
|---|---|---|
| v1.0.0 (initial) | manual + 94 pytest tests | Linux (Hermes container) |
| v1.0.1 | Hermes (local agent) running Qwopus3.6-27B-Coder-GGUF:Q4_K_M on Ollama | Windows (Git Bash + Python 3.11) |
| v1.1.0 | 126 pytest tests + visual review against the orbital-sander STL | Linux (Hermes container) |
| v1.1.1 | Hermes cold-style live run on Windows; full headless slice + thumbnail inject against shoehorn.stl via upstream OrcaSlicer v2.4.0 | Windows (Python 3.11 + native CLI) |
| v1.1.2 | Cold-pass doc fixes + new regenerate_machine_profile.py helper (135 tests) | Linux (Hermes container) |
| v1.2.0 | New printer-side extractor with multi-tool slice; live-tested against the U1 (extracted from "Dazzling Uusam_PETG_25m58s.gcode") | Linux (Hermes container) + real U1 |
| v1.3.0 | Cavity LED auto-on for camera captures + 5-minute auto-dim after print finish (`u1_led.photo_wrap()`); 151 pytest tests | Linux (Hermes container) |
| **v1.4.2** | End-to-end slice workflow (`u1_slice_workflow.py`) with 10-step staged Q&A flow + bundled Hermes skill installable via `hermes skills install bbolinger/snapmaker-u1-toolkit/skills/3d-printer-slicing-automation`. Render-equals-slice rotation fix verified by Kabsch alignment on the EGO String Trimmer holder. Wrong-extruder G-code rewrite closes a safety bug surfaced by the camera-gated start gate during live test (T0 → T&lt;chosen&gt; in start/end blocks while preserving multi-tool cooling commands). 172 pytest tests | Linux (Hermes container) + real U1 |

Findings from the v1.0.1 validation drove every change in that release —
see the [v1.0.1 commit](https://github.com/bbolinger/snapmaker-u1-toolkit/commit/ccdeaef)
for the per-finding breakdown.

## Known limitations / design notes

1. **Single-printer scope**: scripts assume one U1. Multi-printer would need namespacing in the config + per-printer state dirs.
2. **Cron / always-on cadence**: the watchdog, last-layer, and history scripts are written to be cron-driven (typically every 1/5/20 min). They keep state on disk and are idempotent across runs, but they're not daemonized — your scheduler (cron, systemd timer, Hermes' cron, etc.) owns the cadence.
3. **U1 firmware coupling**: tested against Snapmaker U1 firmware on the version that ships Moonraker on port 7125. Other Snapmaker models, or future firmware revisions that change the `print_task_config` / `filament_detect` object shape, may surface field gaps. The `references/` docs capture what the current firmware does emit — start there if you're debugging a field-shape mismatch.

## License

MIT — see `LICENSE`.

## Contributing

PRs welcome, especially:
- Additional reference docs as new firmware behaviors are reverse-engineered
- Material gate enhancements (multi-tool prints, prime-tower extruder assignment)
- Multi-printer support (namespacing config + per-printer state dirs)

Please run `pytest` before submitting — all tests should pass. See
[CONTRIBUTING.md](CONTRIBUTING.md) for setup, conventions, and the
safety-model rules that PRs need to respect.

## Acknowledgments

These scripts were developed and validated against a real Snapmaker U1 over weeks of daily use. Real prints span single models and multi-part kits, generic and brand-specific PETG plus PLA, supports and no-supports, and run times from ~25 minutes to ~12 hours. The safety gate has both blocked a real material mismatch before any heat and carried full prints to completion, driven live over Telegram by a local model.

**Philosophy**: profiles should be YOUR profiles, extracted from YOUR successful prints, mapped to YOUR extruders. The included `profiles/` directory shows the *shape* of those files but is environment-specific. The toolmap gate enforces per-extruder material assignment so wrong-slot mistakes don't waste filament.

Bug reports and PRs from other U1 owners welcome — especially the profile-extraction methodology being tried on different setups (smooth PEI, glass beds, other PETG brands, multi-tool configurations).
