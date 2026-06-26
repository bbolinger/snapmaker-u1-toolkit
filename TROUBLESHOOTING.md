# Troubleshooting

Common failure modes when running the toolkit, organized by workflow phase. Each entry: **what you see → what's actually happening → how to fix.**

If your issue isn't here, the workflow's JSON event stream (`--json-events`) tells you exactly which stage it failed at — open an issue with the events from that run.

---

## Setup phase

### `setup_required` event with `kind:"no_profiles"`

**Symptom:**
```json
{"stage":"setup_required","kind":"no_profiles",
 "message":"No profiles found in profiles/{from-printer,user,snapmaker-stock}. ..."}
```

The workflow exits cleanly without rendering anything.

**Cause:** v1.5.0 ships with an empty profile picker. The directory layout exists but is unpopulated until you run the setup scripts.

**Fix:**
```bash
# Pull Snapmaker's official U1 baseline (~217 files):
python3 tools/fetch_snapmaker_profiles.py

# Extract from your printer's recent print history (Moonraker host must
# be reachable; falls back gracefully if not):
python3 tools/extract_profiles_from_printer.py
```

Run both. The picker prefers extracted profiles (physics-validated on your hardware) over Snapmaker stock (universal baseline). See README's "[Profile sources (v1.5.0)](README.md#profile-sources-v150)" section for details.

### `u1_slice_workflow.py needs numpy + PIL`

**Symptom:** Workflow exits with a clear error block listing Python interpreters it tried.

**Cause:** The workflow's auto-detection couldn't find a Python with both `numpy` and `Pillow` installed. It tries in order: `$U1_TOOLKIT_PYTHON`, `/opt/hermes/.venv/bin/python`, `<repo>/venv/bin/python`, `<repo>/.venv/bin/python`, `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`.

**Fix:** the error message tells you which paths it tried. Either install deps on one of those, or set `U1_TOOLKIT_PYTHON` to an interpreter that has them:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
export U1_TOOLKIT_PYTHON=$PWD/venv/bin/python
```

Verify:

```bash
$U1_TOOLKIT_PYTHON -c 'import numpy, PIL; print("ok")'
```

### `RuntimeError: profile '020_strength' not found in any source`

**Symptom:** Profile lookup fails for a name that worked in v1.4.x.

**Cause:** v1.5.0 changed the picker's value field from the community naming convention (`020_strength`) to a fully-slugified form derived from the actual filename. Snapmaker stock's `0.20 Strength @Snapmaker U1 (0.4 nozzle).json` resolves to slug `0_20_strength_snapmaker_u1_0_4_nozzle`, not `020_strength`.

**Fix:** list what your picker actually has, then use one of those slugs:

```bash
python3 scripts/u1_profile_picker.py
# Pick a value from the output, then:
python3 scripts/u1_slice_workflow.py model.3mf \
    --profile 0_20_strength_snapmaker_u1_0_4_nozzle \
    ...
```

You can also pass the original human-readable name; `normalize_value()` canonicalizes both paths the same way:

```bash
--profile "0.20 Strength @Snapmaker U1 (0.4 nozzle)"
```

---

## Slice phase

### Snapmaker stock profile silently slices wrong settings

**Symptom:** Slice succeeds, but the G-code's `print_settings_id` doesn't match what you picked, OR the slice uses default values instead of your preset's tuned ones (wrong layer height, wrong infill, etc.).

**Cause:** Snapmaker stock process profiles use OrcaSlicer's `inherits` chain — `0.20 Strength @Snapmaker U1 (0.4 nozzle).json` declares `"inherits": "fdm_process_U1_0.20"`. CLI loading needs Orca's vendor resources to resolve that chain. If your installed Orca version drifts from the upstream Snapmaker fork's version, the parent profile may not exist where Orca looks for it, and Orca silently falls back to defaults.

**Fix:** three options, in order of reliability:

1. **Use extracted profiles** (`profiles/from-printer/`). The extractor writes FLAT JSONs — no inheritance, every setting resolved from the source G-code. Physics-validated and CLI-safe by construction.
   ```bash
   python3 tools/extract_profiles_from_printer.py
   ```

2. **Upgrade Orca** to a version matching the Snapmaker upstream branch (currently `Snapmaker/OrcaSlicer:main`). The stock fetcher pulls from `main`; your Orca should match.

3. **Handwrite a flattened version** of the stock profile. Open the stock JSON, manually copy every field from the inherited parent into the child JSON, drop the `inherits` field. Use `examples/profiles/community_merged_*.json` as the shape reference. Save in `profiles/user/`.

After v1.4.2 the workflow **surfaces** `slice.metadata.print_settings_id` in `slice_summary.txt` (next to the gcode); the **agent** (or you, if you're driving the CLI) should inspect it and compare against the preset you requested. If the names don't match, treat the slice as authoritative about what Orca actually did — Orca silently fell back to a different profile.

### `warning` event with `kind:"no_supports_variant"`

**Symptom:**
```json
{"stage":"warning","kind":"no_supports_variant",
 "message":"user picked 'Add supports' but no _supports variant of '016_optimal' exists; slicing WITHOUT supports ..."}
```

The slice proceeds but generates no supports.

**Cause:** You picked "Add supports" at the *Supports?* question, but your chosen preset doesn't have a same-source `_supports` sibling that the workflow can auto-promote to. Snapmaker stock often has multiple Support variants at the same layer height (Support / Support W / Bambu Support W); the workflow can't pick between them.

**Fix:** re-run the workflow and pick a `_supports` preset directly at the *Preset?* question. For 0.4 nozzle:

| Need | Pick this preset |
|---|---|
| 0.20mm strength + supports | `0_20_support_w_snapmaker_u1_0_4_nozzle` (Snapmaker's tree-style W supports) |
| 0.20mm strength + Bambu-style supports | `0_20_bambu_support_w_snapmaker_u1_0_4_nozzle` |
| 0.20mm + plain supports | `0_20_support_snapmaker_u1_0_4_nozzle` |
| 0.16mm optimal + supports | No stock variant exists upstream. Three options: (1) drop down to `0_20_support_w_snapmaker_u1_0_4_nozzle` and accept 0.20mm layers; (2) open the 0.16 Optimal profile in Orca GUI, enable supports, save under a new name, then drop the exported JSON into `profiles/user/`; (3) print once at 0.16mm + supports from the GUI, then run `tools/extract_profiles_from_printer.py` to capture that combined profile into `profiles/from-printer/` |

If you're on extracted profiles, the variant is whatever you sliced with originally — re-extract from a print that DID use supports.

### `warning` event with `kind:"slicer_warning"`

**Symptom:**
```json
{"stage":"warning","kind":"slicer_warning",
 "messages":["WARNING: floating cantilever on Object_X", ...],
 "count":N}
```

**Cause:** Orca flagged geometric concerns during slicing — floating cantilevers, overhang regions exceeding tolerance, etc. Slice succeeded, but Orca thinks parts of the model may not print well as-sliced.

**Fix:** **don't auto-block** — these are advisory, not fatal. The slice produced valid G-code. But surface every message to the operator before they accept the preview render. If a warning names a model region:

1. Look at the source/auto render — the **orange highlighting** flags the same downward-facing faces Orca is warning about
2. Consider re-orienting (the *Orientation?* question's lower-tier recommendation may help)
3. Or accept the warning and slice with supports to address the cantilever

The orange-highlight signal and Orca's `slicer_warning` are different angles on the same underlying problem; they should agree more often than not.

### Orca CLI not found

**Symptom:**
```
FileNotFoundError: [Errno 2] No such file or directory:
'/opt/data/tools/orcaslicer/squashfs-root/bin/orca-slicer'
```

**Cause:** Orca's CLI isn't at the path the toolkit expects. Default path is the Hermes-container layout (`/opt/data/tools/orcaslicer/squashfs-root/bin/orca-slicer`); on other setups it'll be somewhere else.

**Fix:** override via env var. See README's "[Headless slicing setup](README.md#headless-slicing-setup-no-gui--scripted)" section. For the slice workflow:

```bash
export ORCA_SLICER_BIN=/path/to/your/orca-slicer
```

For the AppImage-extract pattern (substitute your actual downloaded AppImage name — OrcaSlicer's release filenames have churned across versions):

```bash
cd ~/Downloads
chmod +x OrcaSlicer-*.AppImage
./OrcaSlicer-*.AppImage --appimage-extract
mv squashfs-root ~/orca-slicer
export ORCA_SLICER_BIN=$HOME/orca-slicer/bin/orca-slicer
```

---

## Upload + start phase

### Wrong-tool catch at start gate (2026-06-24 EGO incident pattern)

**Symptom:** The camera-gated start gate refuses to start the print. It tells you the G-code's initial-extruder setup doesn't match the tool you picked.

**Cause:** Orca's CLI sometimes emits G-code that uses T0 as `{initial_extruder}` even when the loaded filament profile was T1. The v1.4.2 `rewrite_gcode_for_tool()` post-processor fixes this — but only when the workflow IS the one running Orca. If you bypassed the workflow and called `orca-slicer` directly, you got the wrong G-code and the start gate is now blocking it.

**Fix:**
1. **Don't bypass the workflow.** This is exactly why `u1_slice_workflow.py` exists.
2. If you must run Orca directly, run `python3 scripts/u1_slice_workflow.py` instead — it does the rewriter pass + the Snapmaker thumbnail injection (v1.4.3 fix) + the JSON event schema.
3. If you've already sliced and you don't want to re-slice, manually edit the G-code's first ~100 lines: replace `{initial_extruder}` with your tool index (T1 → `T1`, etc.), and update the heater commands (`M104 T*`, `M109 T*`) to match.

The gate exists specifically to prevent heating the wrong nozzle. It's working as designed.

### Camera-gated start gate refused for bed-clear

**Symptom:** Gate refuses to start, saying it can't confirm the bed is clear.

**Cause:** One of:
- Camera snapshot is stale (>60s old)
- LED was off so the snapshot is too dark to assess
- Snapshot URL is unreachable

**Fix:** the gate captures a **fresh LED-on photo** every time. If it's failing:

1. Verify `u1_camera.py photo` works:
   ```bash
   python3 scripts/u1_camera.py photo
   ```
   Should print a path to a JPG. Open it; should be a bright cavity shot.

2. If LED stays dark: verify `u1_led.py status` reports the LED is wired:
   ```bash
   python3 scripts/u1_led.py status
   ```
   If LED isn't configured, the photo wrapper logs to stderr but the gate proceeds with whatever ambient light exists.

3. If the photo is reachable but the gate still refuses, look at the captured photo manually. The default is `--bed-clear cancel` — if you actually want to start, re-run with `--bed-clear start` on `u1_print_start_gate.py` (only when YOU have eyes on the bed).

### Moonraker connection failure

**Symptom:** `urllib.error.URLError` or connection-refused when running any Moonraker-touching command (`u1_status.py`, `u1_print_history.py`, `u1_material_picker.py --json`, etc.).

**Cause:** Either Moonraker isn't running on the U1, your host can't reach the U1's port 7125, or the toolkit's config has the wrong host.

**Fix:**

1. Verify config:
   ```bash
   python3 -c "from u1_config import get_u1_host, get_u1_port; print(get_u1_host(), get_u1_port())"
   ```

2. Verify reachability:
   ```bash
   curl http://<U1_HOST>:7125/printer/info
   ```
   Should return JSON. If it times out, fix the LAN issue.

3. Set the host explicitly if config is wrong:
   ```bash
   export SNAPMAKER_U1_HOST=192.168.1.100
   ```

   Or create `<data-dir>/u1_config.json` with `{"host":"192.168.1.100", "port":7125}`. See README's "[Configuration](README.md#configuration)" section.

---

## Agent / Skill side (Hermes etc.)

### Telegram drops your `.stl` / `.3mf` attachment; agent surfaces stale data

**Symptom:** You send a fresh `.stl` (or `.3mf`) to a Telegram-bridged agent, and instead of running `u1_slice_workflow.py` against it, the agent surfaces an orient prompt with dimensions or render paths from a *previously sliced* file in the same conversation.

**Cause:** Hermes' Telegram gateway has a hardcoded document mimetype allowlist (`gateway/platforms/base.py:SUPPORTED_DOCUMENT_TYPES` — 17 mimetypes including `.pdf`, `.zip`, `.json`, etc., but NOT `.stl` or `.3mf`). When the gateway sees an `.stl`, it replaces the attachment with a text message — `"Unsupported document type '.stl'. Supported types: ..."` — and never calls `download_attachment`. The agent now sees a chat where you said "slice this" with no real file attached, and a quantized local model under the v1.5.2 next_command flow will sometimes fabricate the orient prompt from prior-conversation state instead of refusing.

**Fixes (any one):**

1. **Zip the STL and resend** — `.zip` is on the allowlist. Drop the .stl into a zip, send. The agent gets a real attachment path and can extract / process it.
2. **Drop the file on disk and send the path as text** — e.g., upload to `/opt/data/snapmaker_u1/incoming/cable_holder_vcd.stl` (anywhere the Hermes container can reach), then send the path as a plain Telegram message. The agent uses it verbatim as the workflow's positional arg.
3. **Send a download URL** — Drive / Dropbox / GitHub raw — any publicly accessible link. The agent can `curl` it down before invoking the workflow.

**Upstream fix:** add `".stl": "model/stl"` and `".3mf": "model/3mf"` to Hermes' `SUPPORTED_DOCUMENT_TYPES`. Tracking issue: https://github.com/NousResearch/hermes-agent/issues/53249

### Skill rules silently disappear mid-session

**Symptom:** The deployed `/opt/data/skills/hardware-automation/3d-printer-slicing-automation/SKILL.md` has changed since it was last installed. Specifically the "Token-efficient operation" section is missing — or other sections look truncated.

**Cause:** This has happened TWICE on live runs (v1.4.4 and v1.4.6 sessions). Hermes's slim container silently rewrites the deployed SKILL.md mid-session, deleting whole unrelated sections. The next `deploy_to_runtime.sh` clobbers the wrecked file with the workspace version, hiding the corruption from git history.

**Fix:**
1. **Treat `/opt/data/skills/.../SKILL.md` as read-only.** Never edit it directly from inside a Hermes session.
2. All skill edits go through `/opt/data/workspaces/snapmaker-u1-toolkit/skills/3d-printer-slicing-automation/`, then `deploy_to_runtime.sh`.
3. If you notice the deployed skill looks wrong: redeploy. The workspace is the source of truth.

The skill itself documents this pattern in its YOU MUST NOT section. Agents that try to "remember a lesson" by patching the skill mid-session are the failure case.

### Agent stacks questions instead of asking one per turn

**Symptom:** The agent asks "orientation? + tool? + preset?" in one message; the user replies with one answer; the agent picks defaults for the others.

**Cause:** Agent didn't run the pre-flight acknowledgement at the top of the skill, OR the agent is operating from a stale copy of the skill that pre-dates the per-turn rule.

**Fix:**
1. Re-deploy the skill: `bash deploy_to_runtime.sh` (the workspace version requires per-turn).
2. If you can prompt-engineer, prepend: "Before invoking u1_slice_workflow.py: acknowledge the staged-question rule from the skill."
3. Re-run the slice cycle. The collected answers from a stacked-question session aren't trustworthy — start over.

### Agent mentions renders/files by name but doesn't actually attach them

**Symptom:** Agent's reply says something like *"I'll attach both renders for you to review: 1. Source as-authored render, 2. Auto-oriented render"* — but no image appears in your Telegram thread. Or worse: *"I cannot directly send files through this interface"* when you know Hermes has sent you files before.

**Cause:** Hermes' Telegram (and other platform) gateway uses **bare-path-in-text auto-extraction**, NOT a tool parameter. Verified in `gateway/platforms/base.py:extract_local_files()` (Hermes 0.15.2). The function's docstring, condensed:

> *(paraphrased)* Detect bare local file paths in response text for native delivery. Matches absolute paths (`/...`) and tilde paths (`~/`) ending in common image, video, audio, or document extensions. Validates each candidate with `os.path.isfile()` to avoid URL false positives. Paths inside fenced code blocks (` ``` `) and inline code (`` ` ``) are ignored so code samples aren't mutilated.

(Read the full unedited docstring at `gateway/platforms/base.py:2479` — the canonical extension list, the URL exclusion regex, and the file-vs-document dispatch all live there.)

So the LLM must include the absolute path **as plain prose** — not paraphrased to a filename, not wrapped in backticks, not in a fenced code block, not as a tool-call `files=[...]` parameter. The gateway scans the text reply, validates each candidate is an existing file, and dispatches the attachment.

Most skill writers (myself included on the first pass of this one) mistake Hermes' mechanism for a tool parameter like `reply --files [...]`. That's wrong — there's no such parameter the agent needs to invoke. The gateway does the work.

**Fix in your skill instruction:**

- ❌ "Attach the file via your reply tool's files parameter"
- ❌ "Use your platform's image-send tool with the path"
- ✅ "Emit the ABSOLUTE PATH bare in your reply text — on its own line or naturally in a sentence, NOT inside backticks or code fences. Hermes' gateway auto-attaches paths it finds."

**Verify this is what your skill says** if you're hitting this symptom. The agent isn't refusing — it's following instructions that don't match how Hermes actually delivers files.

### Agent says "multiple versions of skill due to backups"

**Symptom:** Agent's first reply is something like *"I see there are multiple versions of the 3d-printer-slicing-automation skill due to backups. I'll need to use the most recent one from the hardware-automation category."* The agent burns a turn (or several) trying to disambiguate before doing anything useful.

**Cause:** This is the **v1.4.x → v1.5.0 upgrade pollution**. Pre-v1.5.0 deploys of this toolkit left their skill backups as siblings of the live skill — inside the Hermes skill category dir Hermes scans for installed skills. After several deploys, the category dir accumulates `3d-printer-slicing-automation.bak-mirror-<stamp>` directories that look like (incorrect) sibling skill versions.

**Fix:** v1.5.0+ deploys auto-migrate these. On your next `bash deploy_to_runtime.sh` you'll see:

```
Migrated pre-v1.5.0 skill backups (relocated out of the live category dir):
  /opt/data/skills/hardware-automation/3d-printer-slicing-automation.bak-mirror-20260623-205421 → /opt/data/skills_backups/hardware-automation/...
  ...
```

Backups are still preserved — just outside Hermes' skill-scan path. No data lost.

**Manual cleanup (if you're not running the v1.5.0 deploy script):**

```bash
# Replace SKILL_DIR with your actual path:
SKILL_DIR=/opt/data/skills/hardware-automation/3d-printer-slicing-automation
mkdir -p "$(dirname "$(dirname "$SKILL_DIR")")_backups/$(basename "$(dirname "$SKILL_DIR")")"
mv "$SKILL_DIR".bak-mirror-* "$(dirname "$(dirname "$SKILL_DIR")")_backups/$(basename "$(dirname "$SKILL_DIR")")/"
```

Then restart Hermes (`docker restart hermes-agent-stack` on the typical layout) so the skill scanner refreshes.

### Agent fabricates verification ("Kabsch score 50732")

**Symptom:** Agent reports running tests/verification with specific numbers, but those tests/numbers don't appear in the actual test output. The agent's "checklist" cites import paths or files that don't exist.

**Cause:** Known confabulation failure mode — Hermes (and similar agents) will write plausible-looking "verification checklists" with imports of nonexistent modules and fabricated test scores. The output reads like real verification.

**Fix:**
1. **Verify every claim against ground truth.** Grep the codebase for cited imports/paths/functions. If they don't exist, the agent confabulated.
2. The skill's YOU MUST NOT section names this pattern explicitly. Agents that follow the skill don't do this.
3. If you catch this, surface it to whoever's driving the agent. The fix is upstream — don't trust agent-written verification.

---

## Filing issues

If you hit a failure mode not covered here:

1. Re-run with `--json-events` to capture the full event stream.
2. Save the events + the workflow's stdout/stderr.
3. Note: workflow version (from `u1_slice_workflow.py --help`), Orca version (`orca-slicer --version`), Python version, OS.
4. Open an issue at https://github.com/bbolinger/snapmaker-u1-toolkit/issues with all of the above.
