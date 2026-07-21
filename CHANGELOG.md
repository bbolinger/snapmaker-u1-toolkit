# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.8.0] — 2026-07-20

### Added

- **Print multiple copies, dialed exactly.** The copies question is now a +/-
  stepper you dial from 1 to 50, with +5 and -5 buttons to jump in fives,
  instead of a fixed list that stopped at 9.
- **First-layer nozzle temperature.** A separate control sets a hotter or cooler
  first layer for bed adhesion without changing the rest of the print. It uses
  the same per-material safe range as the main nozzle, and only the initial
  layer moves when you touch it.
- **Full brim options.** The brim control expands from off/auto to the U1
  slicer's complete set: off, outer, auto, and mouse ears.

### Fixed

- **The kit form keeps working after a Hermes update.** Updating Hermes could
  silently break the form with a "callback not registered" error, because the
  update replaced a gateway file the form depended on. The form now re-wires
  itself on every message from the toolkit's own plugin, so an update no longer
  takes it down. The print-start safety flow is unchanged.
- **A flaky bed photo no longer aborts a print start.** The printer's camera
  occasionally returns a truncated or incomplete frame; the toolkit now retries
  and accepts only a complete image. If it genuinely cannot get one, it still
  refuses to start rather than proceed blind.
- **Re-slicing a kit no longer strands an old confirm prompt.** When a re-slice
  reused the same request and its bed photo failed, an earlier bed-clear prompt
  could linger and reject your next "yes". A fresh slice now clears any pending
  prompt, which only tightens the gate.

## [2.7.0] — 2026-07-16

### Added

- **Bed and nozzle temperature per print.** Running custom filament on a stock
  profile, you can now dial the nozzle and bed temperature in the kit form. Each
  value is gated to a per-material safe range sourced from Snapmaker's own U1
  presets and the printer's hardware limits, and a low or off bed is always
  allowed for a cool or cold plate.
- **A +/- stepper for every numeric control.** Temperature, infill, walls, and
  top and bottom shells are one-row steppers you dial to an exact value instead
  of picking from a fixed list. The header shows the profile's current value;
  tap it to keep it.

### Changed

- Pattern and the on/off controls stay as buttons, since they are not numbers.
  Cleaned up the print-head label. The filament override is applied at slice
  time and the print-start safety flow is unchanged.

## [2.6.0] — 2026-07-15

### Added

- **Advanced options, organized by category.** From Review, "Advanced options"
  opens a short menu (Strength & shells, First layer & adhesion, Surface finish,
  Supports), and each category opens a page with just its controls instead of
  one long screen. The Supports category appears only when supports are on.
- **New slicing controls.** Separate top and bottom shell layers,
  one-wall-on-top, and raft, alongside the existing infill, pattern, walls,
  brim, and fuzzy skin.

### Changed

- **Each setting shows the profile's current value.** A header reads "Infill:
  keep profile (25%)" so you know what you would be changing, and the value
  follows whichever profile you pick.
- **Compact layout.** Each setting is a titled header with its options laid out
  three across, with no repeated labels and no truncation.
- **Reset and review.** "Reset all to profile" clears every tweak at once, and
  Review shows a one-line summary of what changed. The profile step gained a
  Continue button, so a pre-selected profile confirms with a tap instead of
  paging to the next set.

## [2.5.2] — 2026-07-14

### Fixed

- **Both plate images come back for profiles learned from your prints.** When you
  sliced with a profile pulled from a recent print (new in 2.5.0), the
  confirm-to-print card showed only one plate image and it looked different.
  Those profiles were missing OrcaSlicer's object-labeling setting, which the
  toolkit's plate previews (the top-down footprint and the 3D view) are built
  from, so the 3D view dropped and the footprint fell back to a plainer render.
  The toolkit now enables object labeling on every slice, so both plate images
  render no matter which profile you pick.

## [2.5.0] — 2026-07-14

### Added

- **Profiles from your recent prints.** The print-profile picker now offers
  profiles pulled automatically from what you have actually printed, not just the
  bundled Snapmaker presets. When you send a kit, the toolkit reads the settings
  from your recent prints on the printer and offers them as selectable profiles,
  named the way your slicer named them. It fetches only the small settings block
  from each print, never the whole G-code, so a large print costs about a 1 MB
  transfer instead of the full file. Reprints of the same object collapse to a
  single entry, and a captured print that matches a profile you already have is
  shown once.

### Changed

- **The kit options form pre-selects the profile you last printed with.** Instead
  of opening with no profile chosen, the form now starts on your most recently
  used profile, so a normal print is one tap shorter. You can still change it.

---

## [2.4.4] — 2026-07-14

### Fixed

- **The kit options screen now shows the filament that is loaded right now.** The
  tool and filament picker read a snapshot of the loaded spools that was only
  refreshed when a print started or a file was uploaded. If you swapped a spool
  (colour or material) between jobs, the next kit's picker could still show the
  previous spool, such as an old colour, even though the printer already knew
  about the change. The picker now reads the printer's current filament at the
  moment it builds the screen, so what you see matches what is loaded. If the
  printer cannot be reached for a moment, it falls back to the last known
  snapshot rather than showing nothing. The start-time safety check already read
  the live state and was never affected.

---

## [2.4.3] — 2026-07-13

The kit options form is now driven by the toolkit itself, from end to end. It is
one more place where getting a print started no longer depends on a small local
model relaying a fussy detail exactly right.

### Changed

- **The toolkit renders and collects the kit options form directly.** When you
  send a kit, the toolkit now shows the options form (parts, tool, orientation,
  supports, and the advanced settings), collects your answers, slices, and
  returns the readiness card as one deterministic step. Before this, the model
  had to ask for the form itself, and a small local model would sometimes garble
  that request and stall the job. The model no longer types the form step at all,
  so it cannot garble it. The plate previews, the fresh bed photo, the review
  document, and the final bed-clear approval prompt all reach you unchanged, and
  the safety gate is untouched.

### Fixed

- **A mistyped kit upload name is recovered before the job gives up.** The model
  occasionally retypes an uploaded file's name and mangles the suffix (a `+`
  becomes `_`), so the path points at nothing. The kit step now recovers the real
  file by its stable upload id before failing, the same recovery the rest of the
  workflow already had, so the kit ingests as normal. A genuinely missing or
  ambiguous name is left alone rather than guessed.
- **The first taps on a freshly shown form register.** On a brand-new options
  form, a very fast first tap could land the instant the buttons appeared, before
  the form finished registering, and would flash without checking the box. Early
  taps are now held for a moment and applied once the form is ready.

---

## [2.4.2] — 2026-07-13

Reliability fixes for driving the printer with a small local model that
occasionally garbles its own output.

### Fixed

- **Leaked chat-template tokens are stripped from the reply.** The local models
  this toolkit is documented to run (gemma4 over Ollama) are imported with a
  bare passthrough template, so the model intermittently leaks its
  reasoning-channel delimiters (`<channel|>`, `<|tool_call|>` ...) into the
  visible message, most often as a `thought <channel|>` prefix. A new sanitizer
  removes the known control tokens from the outbound reply on every turn, so the
  operator sees clean text regardless of model or Ollama endpoint. It never
  touches the safety gate, a structured tool call, or a file path, and a clean
  reply passes through unchanged. Validated on real hardware and against the
  live transcript store.
- **A mistyped upload name is recovered by its stable id.** The model sometimes
  retypes an uploaded file's name and mangles the human-readable suffix (a `+`
  becomes `_`), so the path it passes points at nothing. The workflow then
  mis-read the missing archive as a single model, handed the zip to the
  single-model parser, and failed with a confusing "unsupported model file". The
  upload's stable `doc_<hash>` prefix now recovers the real file, so the kit
  ingests as normal. A zip that is genuinely missing or unreadable surfaces a
  clear message instead of silently falling into the single-model path; a zip
  holding a single object is still handled as a kit-of-one.

---

## [2.4.1] — 2026-07-12

Native Windows support, so the print operator runs on a Windows Hermes
Desktop box and not only on Linux, plus a fix that makes the plate, bed
photo, and review doc attach reliably on a brand-new kit.

### Windows (experimental)

- **Runs on Windows Hermes Desktop.** OrcaSlicer resource discovery, the
  runtime script directory, and the config location all resolve per platform
  instead of assuming a Linux layout, and the bootstrap and installer detect
  and adapt to the Windows Hermes environment.
- **Real cross-platform file locking.** The exclusive lock uses the native
  primitive on each platform with no silent fallback, so two runs can never
  slice into each other.
- **First run guides setup.** It prompts for the printer address and warns
  early when the operator binding is unset, instead of failing quietly later.
  Upload transport and config precedence were corrected against real Windows
  live runs. The docs carry a support matrix of what is validated.

### Attachments

- **Images attach every time on a fresh kit.** The plate previews, a fresh
  bed photo, and the pre-print review doc used to be attached by echoing the
  paths the model produced, so a model that dropped a digit from a path broke
  the attachment. The gateway now arms the attachment from the workflow's own
  output and injects the correct paths into the reply, so the operator always
  sees the plate, a fresh bed photo, and the review document before confirming
  a print.

### Hardening

- One shared resolver for the pending-print marker across every consumer.
- Emitted shell commands use quoted, shell-safe paths.
- Fixes to the background reaper's liveness check and a claim-read race found
  during the Windows port.
- Expanded regression and real-slice test coverage.

The model-free print-start safety boundary (single-use confirm, a grace window
with a working CANCEL, and fail-closed on an undeliverable prompt) is unchanged.

---

## [2.4.0] — 2026-07-11

Card attachments now come through as real files, and the reprint CANCEL button
works.

### Reliable card attachments

The readiness, bed-clear, and reprint cards used to depend on the model echoing
the image and document paths back verbatim so Hermes would attach them. When the
model garbled a path, the bed photo and plate previews arrived as raw text
instead of images, and the review doc did not arrive at all. The workflow now
hands the real file paths to a delivery hook directly, so the plate previews and
bed photo attach as photos and the review doc attaches as a file, no matter what
the model types.

### Reprint CANCEL button fixed

The countdown CANCEL button did not work on a reprint: its handler only
registered after the session had shown an interactive form, and a reprint shows
no form, so the button had nothing behind it and the print could not be aborted
from it. It now registers on every incoming message, so the button is live
before any countdown. (Also shipped on its own as v2.3.2.)

### Hardening

- The attachment marker is validated before anything is delivered: only a real,
  non-symlink file with a known artifact name inside the request directory is
  sent, so a forged marker cannot make Hermes send an arbitrary local file.
- Marker consumption is atomic and single-use, and a marker with a missing or
  malformed timestamp is rejected.

### Install

- The Hermes installer now installs the `snapmaker_u1` plugin (which carries the
  attachment hook) alongside the `u1-form` plugin, and the README documents the
  full install sequence. Without it, a fresh clone would not load the new hook.
- The interpreter check probes the compiled Pillow extension rather than the
  bare package, so a broken install is caught early instead of crashing mid-run.

---

## [2.3.0] — 2026-07-07

Three operator-requested features plus one structural safety change born
from a live incident during release testing: the agent model fired the
emitted confirm command itself, starting a print no operator approved. The
start trigger now lives where model behavior cannot reach it.

### Safety

- **The operator's YES is model-free.** The bed-clear prompt no longer
  hands the agent any confirm command — the workflow arms an on-disk
  marker and a gateway hook redeems the operator's literal YES message by
  running the confirm itself. The model is handed nothing it could fire:
  accidental or instruction-following command relay — the failure that
  happened live — is structurally gone. (In the default same-user
  deployment this is not a boundary against a deliberately malicious agent
  with terminal access; every start still runs the audited gate, the
  operator countdown, and the one-tap cancel, and SAFETY.md describes the
  user-separation that makes it a hard boundary.) The
  single-use token, nonce, and revision/hash binding underneath are
  unchanged. With multiple pending starts, a bare YES refuses and asks
  for `yes <code>`: a print start never guesses.
- **Cancel gained a second route.** An operator message that arrives as a
  mid-turn interrupt bypasses gateway hooks (how the incident's CANCEL
  was lost), so the agent may now relay `--grace-cancel` — a command that
  can only ever stop a pending start. Capability is asymmetric by design:
  the model can help cancel, and is never handed a way to help start.
- **Review hardening on the same boundary.** The armed YES window is
  opaque (no command, no token — the hook builds its own fixed invocation
  and never executes anything read from a file) and bound to the
  operator's identity, refusing a YES from anyone else or when identity
  can't be resolved. The stable-tier agent rules were rewritten to match
  (they still taught the old command-relay flow — the strongest prompt
  layer contradicting the boundary), with a repo test that fails if any
  legacy start phrase reappears in a model-facing file. Reprint now binds
  the printer-side file's size and timestamp at upload and re-checks them
  at the confirmed yes, so a same-name overwrite can't ride a review
  through the gate. One installer ships both gateway hooks with receipts
  and a `--verify` mode, and an end-to-end test proves every advanced
  override lands in the sliced gcode. SAFETY.md states the boundary
  honestly: the agent is handed nothing it could fire, and full capability
  separation calls for running the gateway under a separate user.

### Added

- **Reprint.** Say "reprint" (no file needed) and the workflow lists your
  recent successful uploads with their model, head, and material. Pick one
  and the flow goes straight to the bed-clear decision: no re-slicing, the
  gcode already on the printer is reused, the original previews and review
  document are re-surfaced alongside a fresh bed photo, and the start gate
  runs with the same drift checks as a new job (the reviewed revision and
  gcode hash must still match). Each listed option carries a single-use
  pick token, so a stale or replayed message cannot start anything.
- **Advanced settings screen.** The form's Review screen gains one
  `Advanced settings` button opening a single optional screen: infill
  density (10-50%), infill pattern (grid / gyroid / honeycomb / triangles /
  cubic), wall loops (2-4), brim (off / auto), fuzzy skin, and support
  style (tree vs grid, when supports are on). Every field defaults to the
  profile's own value, so skipping the screen is exactly the old behavior.
  Overrides are applied as a flattened temp process profile (the same
  mechanism supports already used), persisted on the request, audited, and
  flagged in the review document's settings sweep before you confirm.
  Text mode accepts the same choices (`infill 30%`, `gyroid`, `walls 3`,
  `brim off`, `fuzzy`, `tree supports`).
- **Quantity.** Single-part jobs gain a Quantity choice on the setup screen
  (1-9 copies). Copies are packed onto the plate by the normal arranger, so
  the plate previews show every instance and jobs too large for one bed
  split into multiple plates exactly like a kit. Text mode: `x3`, `qty 3`,
  or `3 copies`.

### Fixed

- **Reprint confirm no longer re-ingests the original archive.** The YES
  turn on a reprint routes directly to the start gate; previously it tried
  to recover the original upload (long since cleaned from the cache) and
  failed at the finish line.
- **Reprint records its review moment.** The reprint turn re-surfaces the
  plan previews and review document with a fresh bed photo, and now writes
  the same revision-and-hash-bound readiness record a new job gets, so the
  start gate's drift check passes for honest reprints and still refuses if
  anything changed underneath.
- **Advanced screen readability.** Option buttons are self-describing
  ("Infill 30%", "Walls: 3") instead of bare values, and Review exposes
  the screen through a single button plus a summary of non-default picks.

### Changed

- **Skill guidance hardened from live incidents:** on any relayed command
  error or refusal the agent surfaces the message verbatim and stops (no
  self-directed diagnosis or recovery), and a CANCEL during the grace
  window is executed by a model-free gateway hook — the agent runs nothing.

---

## [2.2.2] — 2026-07-06

Safety hardening plus two operator-reported UX bugs, verified live on real
hardware (gemma4-26b over Telegram): a full kit ran form → slice → previews →
bed-clear → grace window → operator cancel, with every fix engaged.

### Safety

- **Stage-2 nonce consumption fails CLOSED.** Any failure while consuming the
  single-use start nonce (lock, read, or write error) now refuses the start
  instead of authorizing it. The previous best-effort fallback returned success
  on error, which is exactly backwards for a safety gate.
- **The material-mismatch override enforces its interactive-terminal requirement
  at the point the override is applied**, not only in the CLI entrypoint, so a
  direct programmatic caller cannot honor the override without a TTY either.
- **Each detached start-gate launch gets a unique run id** for its state marker
  and log, so overlapping invocations for one request can never cross-talk or
  misattribute a grace window.
- **Form answers are claimed before they are read** (atomic rename first), so
  concurrent redeems of one submission cannot both act on it.

### Fixed

- **Duplicate copies of one model now all render in the 3D plate view.** The
  renderer keyed geometry by base model name, which collapsed copies into one
  part while the top-down drew both; geometry is now keyed by the full per-part
  instance id, with copies sharing a color.
- **Re-editing a completed form no longer traps the operator.** Editing a field
  from the Review screen and advancing used to march forward through the
  remaining screens (or dead-end with no way to confirm); it now returns
  straight to Review.
- **A duplicated redeem command re-surfaces the bed-clear prompt** (same
  still-valid confirm token) instead of re-rendering a fresh form — a relayed
  duplicate is now a harmless no-op.

### Changed

- **The `snapmaker_u1` gateway plugin is now tracked in-repo** (`plugin/`,
  installed with `pip install -e ./plugin/`). It auto-loads the slicing skill
  when a 3D-model attachment arrives and wraps `next_action_required` output in
  a hard directive so a small model tool-calls the command verbatim. It was
  previously an untracked local directory — runtime-critical but invisible to
  version control, which let it silently disappear and take the whole
  attachment-to-skill flow with it.

---

## [2.2.1] — 2026-07-06

A safety-hardening and preview-fidelity patch on top of v2.2.0, closing several
integrity gaps in the approval boundary plus a live-caught render bug. Verified end to end on real
hardware (gemma4-26b over Telegram): a full kit sliced, showed the corrected 3D
view, passed the gate, and printed, with the new hardening engaged underneath.

### Safety

- **Material-mismatch override is no longer forgeable by an agent.** The
  `--accept-material-mismatch` override claimed to require an operator phrase but
  silently defaulted the provenance, so an agent holding a valid bed-clear nonce
  could bypass the material gate and the audit row would call it an operator
  override with no mechanical proof. The override is now refused unless invoked
  from a real interactive terminal (an agent-mediated / workflow-subprocess start
  has no TTY and cannot fake one) **and** `--operator-text` is supplied;
  provenance is never defaulted. It is now a deliberate CLI-only escape hatch for
  an operator physically at the machine.
- **Detached start gate reports the real state, not an inference.** The gate runs
  detached to survive the tool-call timeout; previously the parent inferred "grace
  window started" purely from the child still being alive after 25s, so a child
  stalled in a pre-grace check (Moonraker query, camera, I/O) or heading to a late
  refusal was reported as a healthy grace. The child now writes an explicit
  `stage2_gate_state.json` marker (`grace_started` / `started`) which the parent
  polls; an unresolved stall surfaces as an honest `gate_state_unknown` event
  instead of a false grace.
- **Single-use token and Stage-2 nonce consumption are now concurrency-safe.** A
  double-tap "yes", gateway retry, or duplicate delivery could previously consume
  the same confirm-token or Stage-2 nonce twice (read-then-unlink / read-validate-
  write with no lock). The confirm-token is now claimed by atomic rename before
  reading, and the nonce is consumed under a per-request file lock with a re-check
  inside the lock. Exactly one start proceeds.

### Fixed

- **3D plate view is built from the real sliced gcode, not a divergent packer.**
  The isometric companion view was parsed from Orca's `--export-stl` output, which
  uses a different (buggy) packer than `--slice`, so it rendered a garbled,
  overlapping layout that flatly disagreed with the (correct) top-down footprint.
  It is now built from the **same** sliced-gcode M486 outer walls as the
  footprint (each part's mid-body boundary extruded to its real height, drawn with
  an elevated top-down projection that preserves the footprint's orientation), so
  the two views corroborate by construction: same parts, same colors, same
  positions, with height added. Verified live against the real arrangement.
- **Detached gate logs are per-invocation** (`stage2_gate_<pid>.log`), so a retry
  can't truncate a live gate's diagnostics.

### Changed

- Removed the dead arranged-STL isometric renderer (superseded by the gcode one)
  and pointed its test at the live path. Corrected a `v2.2.1`-labeled comment that
  actually described v2.2.0 behavior. Added adversarial + concurrency tests for
  all of the above.

---

## [2.2.0] — 2026-07-05

Every safety-critical claim below was verified **live on hardware** (gemma4-26b
over Telegram), not only in tests — including a single session that both
**refused** a real material mismatch and then ran a **full matching-material
print to completion**.

### Safety

- **Material-mismatch gate now ENFORCES a refusal (was detect-only).** Live-caught
  2026-07-05: sliced PETG, physically swapped to PLA, approved — the print
  *started*. Root cause: `u1_toolmap.py` returned exit `0` even when it had found
  a blocking gate (it correctly detected and printed the mismatch), and
  `run_tool_gate()` decides pass/fail purely on the exit code — so the material
  check had been detect-and-print only, never enforcing. Fixed: the probe exits
  non-zero when a requested material/tool gate is blocking. Verified live end to
  end — PETG-requested vs PLA-loaded now refuses at Stage 2
  (`stage2_preflight_blocked`), the print never starts, and the operator is told
  why; matching material still passes. Added enforcement-layer tests (the prior
  tests only exercised detection, one layer above the bug). The wrong-*extruder*
  gcode-preamble check was unaffected and always enforced.

### Added

- **Unified single-STL + multi-part-kit flow.** A lone `.stl`/`.3mf` is now
  handled as a "kit of one" through the same path as a multi-part zip — one
  entrypoint, one form, one safety boundary. Removed the entire parallel
  single-STL staged flow (~1000 lines) so there is one code path to reason
  about, and single models gain everything kits had (consolidated form,
  composite preview, review doc) for free. Orca's real orientation verdict now
  rides in the form for a single model, so the recommended pose is Orca's actual
  call, not a face-angle approximation.
- **Two corroborating plate previews.** Alongside the top-down footprint traced
  from the *sliced gcode*, the readiness card now also emits an **isometric 3D
  render of the actual arranged, oriented parts** (`kit_plate_isometric`), each
  part in a distinct hue for a multi-part plate. Two views built from different
  data sources — when they agree, you are seeing the truth.

### Fixed

- **Re-printing a model no longer fails on a filename collision.** When printer
  filenames dropped their `doc_<hash>_` prefix (for readability), re-printing the
  same model produced the same base name as a *prior* request's upload — which
  the kit flow only auto-overwrote for its own request, so a cross-request
  collision dead-ended as `kit_upload_failed`. Now any non-own collision uploads
  with a timestamp suffix (`<name>_<ts>.gcode`): the first print keeps the clean
  name, a re-print never fails and never clobbers a different job. Proven on the
  real printer.
- **Last-layer photo missed on fast-finishing prints.** A print whose tail
  extruded between two 1-minute monitor ticks could transition
  `printing → complete` without any poll landing inside the last-layer window,
  silently losing the completion photo. The watcher now also catches the
  `printing → terminal` transition for the same job and captures a fallback
  photo. (No prior test coverage existed for this cron; added.)
- **Detached start gate — grace window survives the tool-call timeout.** The
  ~120s pre-start grace/cancel window ran inside the agent's terminal call and
  could be killed by the ~60s tool timeout mid-grace. The workflow now runs the
  Stage-2 gate detached so the full cancel window always completes.
- **Form redeem no longer relays a manglable id.** The bed-clear confirm and
  form redeem derive their token/id from the request instead of routing a
  random hex string through the model (which small models corrupted, stalling
  with "form id mismatch"). `--confirm-start` / `--redeem-pending-form`.
- **No crash when the bed camera is unreachable at the bed-clear step** — the
  upload-only fallback path referenced undefined locals (`NameError`); now
  degrades cleanly to offering upload-without-start.
- **CI green on a fresh environment.** A live-adapter test hard-failed under CI
  (`python-telegram-bot` is an optional runtime dep, not in `requirements.txt`);
  it now `importorskip`s cleanly, matching how the adapter treats the dep.
- **Test suite could DM the operator a real "print starting" notification**
  (live 2026-07-02, "spam every time you run the suite"). `test_u1_config`
  loads the real `/opt/data/.env` into `os.environ`, leaking
  `U1_GRACE_NOTIFY_CMD`; later start-path tests resolved it and the notify
  script defaults `HERMES_BIN`→`hermes` + dest→`telegram`, sending to the real
  chat. conftest now: stubs `HERMES_BIN` + shadows `hermes` on PATH (the suite
  is structurally unable to reach Telegram; any attempt is logged), defaults
  the grace window to 0 so non-grace tests skip it (no notify, no 120s sleep —
  the suite also got ~13× faster), and scrubs the notify env per test.
- **False "print starting" notification loop** (live 2026-07-02). A confused
  agent that invoked the print-start flow with a placeholder/nonexistent
  filename (gpt-5.5 looping on `x.gcode` / `wall_mount.gcode` from the skill
  examples) fired a real operator "print starting in 120s" notification each
  time — grace-period-paced, ~every 2 minutes, for over an hour — for prints
  that could never happen. Two guards in `u1_print_start_gate`:
  - **Fence 2:** the gcode must exist in the printer's storage (Moonraker
    `files/metadata`) BEFORE the grace window + notification fire. A
    confirmed-absent file (404) is a fast, silent refusal
    (`gate_refused_file_missing`) — no notification. Fails open on a flaky
    metadata query so a transient error never blocks a real print.
  - **Loop guard:** a per-request cap (4) on grace notifications. A loop on a
    *real* uploaded file (which the existence check can't catch) trips it and
    the DM is suppressed (`pre_start_grace_notify_suppressed_loop_guard`); the
    grace WAIT still runs, so the cancel safety net is untouched.

### Changed

- **Skill rewritten imperative-first and corrected to the unified flow.** The
  body described the retired single-STL staged mechanism (events the code no
  longer emits); it now matches what the unified workflow actually emits, opens
  with an act-by-calling-tools directive, and shed ~7KB. Verified not to regress
  gemma4's tool-calling. All safety rules (anti-fabrication, the single bed-clear
  boundary, verbatim command relay) preserved.
- **README brought to v2.2 reality** + a new **"Always-on print monitoring"**
  section documenting the three no-agent cron jobs (first/last-layer + post-resume
  photos, quiet health watchdog, print-history ledger) that run with no LLM in
  the loop — surfacing a capability that existed but was buried.
- **Form UX v2.2.1 — fewer, clearer screens (operator feedback 2026-07-02).**
  - **Setup screen: print head + orientation + supports on ONE screen.** The
    renderer gained a `group` model — fields sharing a group render together as
    labelled blocks under one shared `Next ➜`. Grouped single-selects behave as
    radios (tap marks, doesn't advance); ungrouped ones (profile) still advance
    on tap. Step counter counts screens, so a group is one step.
  - **Print head carries the filament.** When the live tool map is present, the
    head screen shows each head's loaded material + colour
    (`Head 2 (T1) — PETG ⚫ black`, read from `printer_reported`), and picking
    the head sets the material — the separate Material screen is gone. Falls
    back to generic T0–T3 + a Material screen when offline. The start gate still
    physically re-verifies loaded material at print time.
  - **Action → two submit verbs** on the review card (`⬆ Upload only` /
    `▶ Upload + Start`) instead of its own screen.
  - **Single-part kit skips the Parts screen** (nothing to choose).
  - **Profile labels** drop the `@Snapmaker U1 (0.4 nozzle)` suffix that every
    entry shared.
  - Step counter now counts only the screens the operator taps through.

### Fixed

- **Telegram form taps never reached the form handler** (live 2026-07-02).
  The plugin swapped `_handle_callback_query` on the adapter class, but PTB
  captured the bound method at `connect()` — the swap was invisible, every
  tap routed to Hermes' native dispatcher, and forms sat untouched until the
  600s timeout, after which the agent fell back to free-text questioning.
  The patch now registers its own pattern-scoped `CallbackQueryHandler`
  (group −11, `ApplicationHandlerStop`) on the live PTB application at
  `send_form` time — timing-independent, reconnect-safe, and the native
  dispatcher is never touched.
- **`form_id` could start with `-`** (`token_urlsafe` alphabet), turning
  `--form-answers-from <id>` into an argparse flag. New ids are
  alphanumeric (`f` + hex); `next_command` now uses the
  `--form-answers-from=<id>` form so legacy persisted ids keep working.
- **Multi-select affordance** (operator feedback): action row is now
  `Select all / Clear / Next ➜` (was `All / None / ✅ Done`), the header
  shows `(n of N selected)` or `(none picked → all N)`, plus a
  "tap to toggle ✔" hint — so the checkbox behavior is discoverable and
  "Done" no longer reads as submit.
- **Skill contract:** on form timeout/cancel/error the agent must resume
  the staged one-line text flow — never dump all fields as free-text
  questions in a single message.

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
- **Form mode + answers-file handoff.** `--interaction-mode form` (or
  `U1_INTERACTION_MODE=form`) emits one consolidated `kit_form` event with
  `form_schema` + a single-form `form_id`; the Hermes/Telegram button
  adapter writes the collected answers to
  `<U1_FORM_ANSWERS_DIR>/<form_id>.json` at the GATEWAY, and the workflow
  redeems the file via `--form-answers-from <form_id>` — single-use
  (consumed on read), bound to the persisted form id (mismatch refused,
  file preserved for the rightful redeemer). Answer content never passes
  through the model in either direction; the model relays one opaque id,
  the same trust level as `--pending-nonce`. Staged text flow unchanged
  and still the default — buttons are for reliability, not a requirement.
- **hermes-agent 0.18 compatibility** ("The Judgment Release" moved
  platform adapters into a plugin system; the Telegram class moved from
  `gateway.platforms.telegram.TelegramPlatform` to a plugin-loaded
  `TelegramAdapter`). Solved structurally by the plugin rebuild below:
  the adapter patch never imports a class by module path anymore — it
  patches the live instance's class — so adapter relocations stop being
  breaking events. `SendResult` construction degrades to a duck-typed
  stand-in if `gateway/platforms/base.py` ever moves, and a missing
  `_handle_callback_query` hook point skips the patch loudly instead of
  raising.
- **Hermes integration rebuilt as a first-party plugin**
  ([`adapters/hermes/plugin/`](adapters/hermes/plugin/)), replacing the
  tools/-drop + monkey-patch approach after three visibility fixes in a
  row proved wrong against the real package. Root causes, all verified in
  hermes-agent 0.18 source: (1) platform agents get a per-toolset
  allowlist and bare-composite configs enable toolsets by
  subset-inference — a runtime-registered toolset never qualifies, and
  joining `clarify` evicts clarify itself; plugin-provided toolsets are
  the first-party path (auto-enabled per platform, no inference, operator
  can toggle in `hermes tools`). (2) Generic registry dispatch passes
  handlers no callback and no agent — the gateway's run.py patch now
  publishes its per-turn form callback into `tools.form_gateway` keyed by
  `agent.session_id`, exactly what dispatch hands the handler. (3) The
  Telegram adapter class loads under two module names — import-side
  patching can hit the copy the gateway never instantiates; the plugin's
  `pre_gateway_dispatch` hook patches `type()` of the live adapter
  instances in `gateway.adapters` instead. `install.py` now deploys +
  enables the plugin, removes pre-plugin layout files, replaces the
  run.py block in place on upgrades, and verifies the real invariant in
  the venv (clarify held AND form offered on a bare-composite config).
- **Real-package regression tests**
  ([`tests/test_hermes_real_package.py`](tests/test_hermes_real_package.py)):
  run against an actual hermes-agent tree (`U1_HERMES_AGENT_SRC`; CI
  skips) — baseline bug reproduction, the no-eviction superset invariant
  (`with_plugin == baseline | {"form"}`), schema delivery through
  `get_tool_definitions`, clean plugin load, and the run.py patcher
  against the real `gateway/run.py`. Exists because this bug class is
  invisible to hermetic tests.

---

## [2.1.0] — 2026-07-02

**Multi-part kit support, the pre-start grace period with model-free
Telegram cancel, and a hardened safety boundary** — the rc1 feature set
(see rc1/rc2 entries below) shipped after extensive hardening and full live
verification on real hardware.

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

Hardening of rc1. A deep pass across the kit workflow, the safety gate and
grace-cancel, the form/arrange/kit scripts, and the adapters found bugs that
contradicted rc1's own safety claims; all release blockers are fixed here. No
schema changes.

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

Doc-only patch cleaning up the v2.0.0 release notes. No runtime or schema changes; no test additions; safe to apply by `git pull` alone.

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

---

## Appendix: release validation history (v1.x era)

Preserved from the README. Since v2.x, per-release validation evidence is
documented in each release entry above.

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
