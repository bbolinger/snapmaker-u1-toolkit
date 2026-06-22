# Snapmaker U1 Toolkit

Read-only and gated-write automation scripts for the [Snapmaker U1](https://snapmaker.com/snapmaker-u1) multi-tool 3D printer, talking over its Moonraker/Klipper-compatible LAN API.

**Built for safety-staged operation**: read state → slice → upload-only → operator approval → start. The dangerous bits (`start print`, `cancel`, movement) are always gated on explicit operator confirmation.

## What's in here

| Script | What it does |
|---|---|
| `u1_config.py` | Centralized host/port resolution (env > JSON > default) |
| `u1_camera.py` | Camera capture via Snapmaker-specific websocket `camera.start_monitor` |
| `u1_toolmap.py` | Multi-tool material gate — declared vs detected material check |
| `u1_preflight.py` | Combined Moonraker state + camera freshness packet for "is it safe to start?" |
| `u1_upload_gcode.py` | Upload-only (`print_started=false`) with gates: idle state + tool/material match |
| `u1_last_layer_watch.py` | Watch active print for final-layer event, snap photo |
| `u1_print_watchdog.py` | Quiet 20-min health watcher with cooldown to avoid notification spam |
| `u1_print_history.py` | Append-only JSONL print ledger + canonical upserted JSON |
| `snapmaker_u1_status.py` | Read-only status probe |
| `snapmaker_u1_snapshot.py` | Websocket camera trigger helper |
| `tools/extract_profile_from_gcode.py` | One-shot extractor — turn a successful G-code into Snapmaker Orca process + filament JSONs |
| `tools/gcode_inject_thumbnail.py` | Add Snapmaker-app preview thumbnails to headless-sliced G-code (PIL renderer + base64 splice) |
| `tools/render_stl_orientation.py` | Pre-print orientation review — 4-view PNG (isometric, front, side, top) with overhang faces highlighted in orange |

## Safety model

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

## Quick start

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

## Profile templates

**The included profiles are REFERENCE EXAMPLES, not "the right" profiles for your U1.**

`profiles/` contains 13 Snapmaker Orca JSONs derived from one operator's successful prints. They demonstrate the *shape* of a per-extruder + per-filament profile but they're tuned for that specific environment: one bed surface (Textured PEI), one bed temp, certain filament brands (SUNLU PETG, HF White PETG), specific tool assignments.

**The real value is the methodology**: extract YOUR profiles from YOUR successful prints, mapped to YOUR extruders and YOUR filaments. Here's why and how.

### Why build your own (vs. just importing these)

Profile-as-data-from-real-prints means every setting is *physics-validated* — it produced a completed print on actual hardware. But the validation is environment-specific:

- Different bed surface (smooth PEI, garolite, glass) → different first-layer temp / bed temp / Z-offset
- Different filament brand → different optimal nozzle temp (PETG ranges 230–260°C across brands)
- Different tool assignment → e.g. your PLA is in extruder0, mine in extruder2
- Different exhaust/enclosure → affects warping defaults

Importing someone else's profiles is fine as a starting point; running them as gospel on a different setup will give you mediocre prints.

### Build per-extruder, per-filament profiles from your own print history

This is the recipe Hermes used. It only takes one good print per filament-type-per-extruder slot:

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
4. **Build a flattened process JSON** (see `profiles/community_merged_*.json` for shape) and a matching filament JSON (see `profiles/community_generic_petg_*.json`). Name them with the extruder + filament so you don't confuse yourself: e.g. `myprinter_extruder1_sunlu_black_petg.json`.

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

### About the included profile files

The 13 profiles in `profiles/` follow this naming convention so you can see the pattern:

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
> 2026-06-22 on `snapmaker-orca v2.3.4` Windows, exit code `-1073741819`).

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
> Windows smoke, 2026-06-22) and avoids quoting foot-guns on PowerShell.

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

**Workaround**: use the flattened `community_merged_*` process profile in this repo. It pre-resolves the full inheritance chain so CLI loading gets exact values. The `_override` variants only work in the GUI where Orca resolves the official base profile. The same logic applies to the bundled machine profile above.

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

135 tests covering: config resolution (incl. 3-tier data-dir, `.env`
auto-loader with quoted/commented/walk-up edge cases, import-without-config
regression lock, and a smoke-runner that exercises every script's `main()`
to catch leftover undefined refs), material gate (incl. fail-closed on
corrupt map), upload pre-checks, G-code metadata parsing, print-history
ledger (incl. atomic-write contract + tmpfile cleanup on failure), profile
extraction, thumbnail injection, upload-time thumbnail wiring, status-probe
`safe_to_upload` parity with the actual upload gate, preflight `--host`
override correctness, STL parsing + view rotations + overhang detection +
4-view orientation sheet rendering, bundled machine-profile completeness
(standalone, klipper gcode flavor, 4 extruders, required slicing fields).

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
| **v1.1.1** | Hermes cold-style live run on Windows; full headless slice + thumbnail inject against shoehorn.stl via upstream OrcaSlicer v2.4.0 | Windows (Python 3.11 + native CLI) |

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

These scripts were developed against a single U1 over its first ~3 days. Real-world print validation across 4+ distinct workflows (single + multi-tool, generic + brand-specific PETG, support + no-support, ~25min and ~6h prints).

**Philosophy**: profiles should be YOUR profiles, extracted from YOUR successful prints, mapped to YOUR extruders. The included `profiles/` directory shows the *shape* of those files but is environment-specific. The toolmap gate enforces per-extruder material assignment so wrong-slot mistakes don't waste filament.

Bug reports and PRs from other U1 owners welcome — especially the profile-extraction methodology being tried on different setups (smooth PEI, glass beds, other PETG brands, multi-tool configurations).
