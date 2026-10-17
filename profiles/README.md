# profiles/

The toolkit's profile picker scans the subdirectories here. As of v1.5.0, **no profiles ship with the repo by default** — each subdir is populated locally by the user.

## Layout

| Subdir | Populated by | Purpose | Picker priority |
|---|---|---|---|
| `from-printer/` | `python3 tools/extract_profiles_from_printer.py` | Extracted from the user's actual recent G-codes (Moonraker history). Reflects what they've successfully printed. | Highest |
| `user/` | The operator, manually | Hand-tuned profiles + overrides | Middle |
| `snapmaker-stock/` | `python3 tools/fetch_snapmaker_profiles.py` | Snapmaker's official upstream U1 profiles, pulled from the Snapmaker/OrcaSlicer GitHub repo. | Lowest (universal baseline) |
| `machine/` | Ships in the repo | Snapmaker U1 (0.4 nozzle) machine definition — runtime fallback if the bundled Orca install can't resolve its vendor profile. | n/a (not a picker source) |

All three picker subdirs are listed in `.gitignore`. They're per-user, not redistributed.

## Bootstrapping

A fresh install will have empty subdirs. The toolkit fails closed with a helpful error in that state. To populate:

```bash
# Pull Snapmaker's official U1 baseline (~217 files):
python3 tools/fetch_snapmaker_profiles.py

# Pull your actual recent prints (Moonraker host must be reachable):
python3 tools/extract_profiles_from_printer.py
```

You can run both. The picker dedupes by profile value (slug), with the higher-priority source winning collisions.

Extracted profiles carry the print's speed, acceleration and jerk settings
alongside layers, walls and infill, and are stamped with the extractor
version that wrote them. Profiles from an older extractor are fetched again
on the next run (or the next form build), so a `from-printer/` populated by
a release before this one heals itself; nothing needs deleting by hand.

## See also

- `examples/profiles/` — Brent's personal community profiles, kept as **examples** for handwriting reference. Do not use as defaults; they're tuned for a specific filament/plate combo.
- `tools/fetch_snapmaker_profiles.py` — fetcher source + filter rules.
- `tools/extract_profile_from_gcode.py` / `tools/extract_profiles_from_printer.py` — extraction.
