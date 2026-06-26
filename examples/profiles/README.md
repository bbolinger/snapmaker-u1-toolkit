# Example community profiles

These OrcaSlicer profile JSONs are the personal-system templates I (Brent / `bbolinger`) used during early development of the toolkit. They worked on **my** U1 with **my** filaments on **my** Textured PEI plate — they are **NOT** safe defaults to apply to anyone else's printer.

They live here as **examples**, for two purposes:

1. **Reference shape** — you can crack them open to see what fields a Snapmaker U1 process or filament profile looks like in JSON form. Useful when you're handwriting your own.
2. **Last-resort defaults** — if you're running the toolkit against my exact setup, you can copy any of these into `profiles/user/` and they'll appear in the picker.

## How to actually get profiles

Don't use these as your primary profile source. Instead, run one or both of:

```bash
# Pull Snapmaker's official U1 stock profiles from the upstream GitHub repo.
python3 tools/fetch_snapmaker_profiles.py

# Extract the profiles you've actually been printing with successfully
# from your printer's recent G-code history (Moonraker required).
python3 tools/extract_profiles_from_printer.py
```

Both write to `profiles/<source>/` directories that `list_profiles()` scans automatically. The picker prioritizes:

1. `profiles/from-printer/` — your real successful prints
2. `profiles/user/` — your hand-tuned profiles
3. `profiles/snapmaker-stock/` — Snapmaker official baseline

## Why this exists as a warning

In v1.4.x the toolkit shipped these community profiles as the **default** in `profiles/`. That meant a new user running the workflow with no setup would silently slice with my filament settings, my bed type, my plate temps. If their setup differed, the print could fail at any stage — bed adhesion, nozzle clog, layer separation, even a heater hit if the filament didn't match.

v1.5.0 moves them here so the toolkit ships with an **empty** picker, fails closed with a helpful error message, and points the user at the fetch/extract scripts.
