# Building U1 profiles

Where the toolkit looks for slicer profiles, why it ships empty, and how to
build per-extruder, per-filament profiles from your own successful prints.

[← Back to the README](../README.md)

---

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


---

[← Back to the README](../README.md)
