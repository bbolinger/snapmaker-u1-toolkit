# Snapmaker U1 profile/tool fidelity fixes — v1.4.2

> **v1.5.0 disclaimer:** profile paths referenced below (`community_020_strength_*`, `community_016_optimal_*`, etc.) lived in `profiles/` in v1.4.x — they moved to `examples/profiles/` in v1.5.0 when the picker was rewritten around three real sources (`from-printer/`, `user/`, `snapmaker-stock/`). The v1.4.2 fixes themselves (`profile_path` via `list_profiles()`, `rewrite_gcode_for_tool()`) are still active code. See `README.md#profile-sources-v150` for the current path semantics.

Session learning captured from the EGO String Trimmer holder workflow.

## Failure modes observed

1. **Profile fallthrough:** the workflow offered `020_strength_gyroid`, but `profile_path()` only knew a short hardcoded map and silently resolved to plain `020_strength`.
2. **Wrong tool in generated G-code:** selecting T1 black PETG still produced Orca start-block lines that referenced T0, for example `M104 T0 S140`, `T0`, `M104 T0 S165`, `M109 T0 S165`.

Both are safety-relevant because the UI choice, metadata, and printer behavior can diverge.

## Runtime fixes deployed

- `profile_path()` now consults `u1_profile_picker.list_profiles()` for resolution.
- `rewrite_gcode_for_tool()` post-processes Orca output after slicing:
  - rewrites initial-extruder `T0` references to `T<chosen_idx>` for non-default tools;
  - preserves slot-literal multi-tool cooling commands such as `M104 S0 T<n> A0`.
- `real_orca_slice()` returns `tool_idx` and `tool_rewrites` for debugging.

## Smoke checks to run after slicing/deploy

```bash
cd /opt/data/workspaces/snapmaker-u1-toolkit
/opt/hermes/.venv/bin/python -m pytest -q
```

Expected in the v1.4.2 session: `172 passed`.

Runtime profile resolution probe:

```python
import sys
sys.path.insert(0, '/opt/data/scripts')
from u1_slice_workflow import profile_path
for p in ['020_strength_gyroid','020_strength_gyroid_supports','020_strength','016_optimal']:
    print(p, '->', profile_path(p).name)
```

Expected mappings:

- `020_strength_gyroid` → `community_020_strength_gyroid_u1_textured_pei.json`
- `020_strength_gyroid_supports` → `community_020_strength_gyroid_supports_u1_textured_pei.json`
- `020_strength` → `community_020_strength_u1_textured_pei.json`
- `016_optimal` → `community_016_optimal_u1_textured_pei.json`

## Start-gate nuance

Moonraker may still report `toolhead.extruder=extruder` early in warmup even when the corrected file is heating T1/`extruder1`. Do not treat that field alone as proof of wrong-tool use. Verify the corrected G-code start block and live heater targets; fail closed if either disagrees.
