#!/usr/bin/env python3
"""Canonical Snapmaker U1 end-to-end slice workflow.

The workflow owns the 10-step operator flow: triage -> orientation -> material
-> profile -> render -> supports -> slice -> preview -> upload-only/start choice
-> camera-gated start. It intentionally prefers upload-only and fail-closed start.
"""
from __future__ import annotations

# Bootstrap: env check happens BEFORE the heavy numpy/PIL-dependent imports
# below. If the current interpreter is missing those deps, we try a list of
# known-good Python paths (env var, Hermes' bundled venv, local project venv)
# and re-exec with the first one that works. Only if NONE of those have the
# deps do we fail with a clear, actionable error.
#
# Why: the workflow's hard requirements are numpy + PIL (via _stl_render,
# u1_orient, render_slice_review). When invoked via `python3 u1_slice_workflow.py`
# the system python often lacks them, but the user's environment (Hermes venv,
# a project venv they made) usually does. Auto-detection means users don't
# need to know which python to use; the workflow finds one that works.
import os, sys, subprocess
from pathlib import Path


def _check_python_has_deps(python_path: str, deps: tuple = ('numpy', 'PIL')) -> bool:
    """Return True iff `python_path` can import every dep without error."""
    try:
        proc = subprocess.run(
            [python_path, '-c', f'import {", ".join(deps)}'],
            capture_output=True, text=True, timeout=10,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _ensure_compat_python() -> None:
    """If the current interpreter lacks numpy/PIL, find a known-good Python
    that has them and re-exec self with it. Exit with a clear error if none
    of the candidates work."""
    # Fast path: current interpreter has the deps
    try:
        import numpy  # noqa: F401
        import PIL    # noqa: F401
        return
    except ImportError:
        pass

    # Identify which deps are actually missing (for the error message)
    missing = []
    for dep in ('numpy', 'PIL'):
        try:
            __import__(dep)
        except ImportError:
            missing.append('pillow' if dep == 'PIL' else dep)

    # Candidate Python paths, in priority order. The U1_TOOLKIT_PYTHON env
    # var wins so users can override on any host without code changes.
    here = Path(__file__).resolve().parent
    root = here.parent
    candidates = []
    env_override = os.environ.get('U1_TOOLKIT_PYTHON')
    if env_override:
        candidates.append(env_override)
    candidates.extend([
        '/opt/hermes/.venv/bin/python',         # Hermes-bundled venv (common host)
        str(root / 'venv' / 'bin' / 'python'),  # project-local venv
        str(root / '.venv' / 'bin' / 'python'), # uv/poetry-style hidden venv
        '/opt/homebrew/bin/python3',             # macOS Homebrew (Apple Silicon — default for M-series)
        '/usr/local/bin/python3',                # macOS Homebrew (Intel) — legacy install path
    ])

    for cand in candidates:
        if not Path(cand).exists():
            continue
        if _check_python_has_deps(cand):
            # Re-exec with the working interpreter. execv replaces this process,
            # so the agent that spawned us still gets stdout/stderr of the
            # workflow. Pass the same script + the same argv tail.
            print(
                f'[env] current python lacks {", ".join(missing)}; '
                f'switching to {cand}',
                file=sys.stderr,
            )
            os.execv(cand, [cand, __file__, *sys.argv[1:]])
            # execv does not return on success

    # Nothing worked — print a clear, actionable error and exit non-zero.
    msg = [
        f'ERROR: u1_slice_workflow.py needs numpy + PIL (Pillow).',
        f'Missing on the current interpreter ({sys.executable}): {", ".join(missing)}',
        f'',
        f'Tried these alternative Python interpreters (none had the deps):',
    ]
    for c in candidates:
        msg.append(f'  - {c}  ({"exists" if Path(c).exists() else "not found"})')
    msg += [
        f'',
        f'Fix one of these:',
        f'  1. Install into your current interpreter:',
        f'     {sys.executable} -m pip install numpy pillow',
        f'  2. Point the workflow at an interpreter that has them:',
        f'     export U1_TOOLKIT_PYTHON=/path/to/python',
        f'  3. Create a project venv (recommended for new users):',
        f'     cd {Path(__file__).resolve().parent.parent}',
        f'     python3 -m venv venv && venv/bin/pip install numpy pillow',
    ]
    print('\n'.join(msg), file=sys.stderr)
    sys.exit(2)


if __name__ == '__main__':
    _ensure_compat_python()

# === After env check passes, do the rest of the imports ===
import argparse, json, re, shutil, time
from typing import Any

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
TOOLS=ROOT/'tools'
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(TOOLS))
from _stl_render import parse_stl, bbox  # type: ignore
from u1_orient import orient_model, DEFAULT_ORCA, orca_env
from u1_profile_picker import list_profiles
from u1_material_picker import query_material_options, status_to_options
from u1_upload_gcode import parse_gcode_metadata
from render_slice_review import render_slice_review, pick_recommended_orient
import u1_profile_picker as upp
from render_slice_review import first_layer_bbox as parse_first_layer_bbox

DEFAULT_OUT_BASE=ROOT/'artifacts'/'slice_workflow'

# Mirrored events file for harness/recovery. Set by run_workflow once out_dir
# is known. Both --json-events stdout AND the human-readable mode write to
# this file, so the file is always a complete audit trail regardless of how
# the caller invoked us. Used by tests/harness/drive_via_hermes.py to score
# acceptance criteria against the event stream even when Hermes' chat -Q
# mode suppresses tool output from its own stdout.
_EVENTS_FILE: Path | None = None


def emit(obj: dict[str,Any], json_events: bool=False):
    if json_events: print(json.dumps(obj), flush=True)
    else:
        stage=obj.get('stage','event'); print(f'[{stage}] '+', '.join(f'{k}={v}' for k,v in obj.items() if k!='stage'))
    if _EVENTS_FILE is not None:
        try:
            with _EVENTS_FILE.open('a') as f:
                f.write(json.dumps(obj, default=str) + '\n')
        except Exception:
            pass  # mirroring is observability — never break the workflow on disk-write failure


# v1.5.2 (2026-06-26): next_command per option. The workflow is the source
# of truth for "what to run next" — the agent never synthesizes commands
# from chat memory. Each need_input event's options carry a `next_command`
# field with the literal bash invocation the agent tool-calls when the
# operator picks that option. Gemma4-26b and even smaller models can do
# this — it's a copy, not a synthesis.

def _shell_quote(s: str) -> str:
    """Single-quote for shell, escaping embedded single quotes."""
    if not s:
        return "''"
    if all(c.isalnum() or c in '@/.,_-:=' for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def _cmd_prefix(script_path: str, model_path: str, args) -> str:
    """Build the cumulative invocation prefix from args that are already set.
    Each subsequent need_input's next_command extends this prefix with one
    more flag + the option's value."""
    parts = ['python3', script_path, _shell_quote(model_path), '--json-events']
    if getattr(args, 'orient', None):
        parts += ['--orient', args.orient]
    if getattr(args, 'tool', None):
        parts += ['--tool', args.tool]
    if getattr(args, 'material', None):
        parts += ['--material', _shell_quote(args.material)]
    if getattr(args, 'profile', None):
        parts += ['--profile', _shell_quote(args.profile)]
    if getattr(args, 'supports', None):
        parts += ['--supports', args.supports]
    if getattr(args, 'nozzle', None) and args.nozzle != '0.4':
        parts += ['--nozzle', args.nozzle]
    return ' '.join(parts)

def triage_stl(stl: Path)->dict[str,Any]:
    tris=parse_stl(stl); xmin,xmax,ymin,ymax,zmin,zmax=bbox(tris)
    vol=(xmax-xmin)*(ymax-ymin)*(zmax-zmin)/1000.0
    return {'dims_mm':[round(xmax-xmin,2), round(ymax-ymin,2), round(zmax-zmin,2)], 'tris': int(tris.shape[0]), 'bbox_volume_cm3': round(vol,2)}

def choose_default(options: list[dict[str,Any]], supplied: str|None=None):
    if supplied:
        for o in options:
            if supplied == o.get('value') or supplied.lower() in str(o.get('label','')).lower(): return o.get('value')
        return supplied
    for o in options:
        if o.get('recommended'): return o.get('value')
    return options[0].get('value') if options else None


def promote_to_supports_variant(profile_value: str) -> str | None:
    """Pre-v1.5.1 behavior — superseded by apply_supports_override below.

    Kept temporarily for backward compatibility with the v1.4.x supports
    plumbing (look for a sibling _supports preset). Not used by the
    workflow's commit phase anymore — the binary force-on/force-off
    override (apply_supports_override) replaces the promotion approach
    because it doesn't depend on having a matching _supports sibling
    available. Will be removed in v1.5.2 if no consumer surfaces."""
    all_opts = list_profiles()
    picked = next((o for o in all_opts if o['value'].lower() == profile_value.lower()), None)
    if picked is None or picked.get('has_supports'):
        return None
    same_source_with_supports = [
        o for o in all_opts
        if o.get('source') == picked.get('source') and o.get('has_supports')
    ]
    if len(same_source_with_supports) == 1:
        return same_source_with_supports[0]['value']
    return None


def _flatten_filament_profile(filament_path: Path, orca_bin: Path | None = None) -> dict[str, Any]:
    """Walk a filament profile's inherits chain and merge into a self-contained
    dict. Same shape as _flatten_process_profile but searches the vendor
    filament/ subdir.

    Why: live-test failure 2026-06-25 showed that --load-filaments pointing
    at our profiles/snapmaker-stock/filament/ files made Orca silently fail
    to resolve the inherits chain. Snapmaker PETG @U1.json carries only
    compatible_printers + inherits='Snapmaker PETG @U1 base'. Orca loaded
    that but couldn't find the parent (it's NOT in Orca's configured data
    dir, only in our profile tree), so the resolved filament had NO
    filament_type or bed temperatures — Orca then fell back to its
    hardcoded PLA defaults. Gcode metadata stamped filament_type=PLA,
    first_layer_bed_temperature=45 (a PLA temp), and the U1 upload gate
    rejected it on the PETG/PLA mismatch.

    Flatten resolves the chain ourselves before handing the temp to Orca,
    so all filament_type / temperature fields are present and Orca can't
    fall back to defaults."""
    visited: set[str] = set()
    chain: list[dict[str, Any]] = []
    cur_path: Path | None = filament_path
    unresolved_inherits: str | None = None

    if orca_bin is None:
        orca_bin = DEFAULT_ORCA
    vendor_dirs: list[Path] = []
    vendor_root_candidates = [
        orca_bin.resolve().parents[1] / 'resources' / 'profiles' / 'Snapmaker',
        Path('/opt/data/tools/orcaslicer/squashfs-root/resources/profiles/Snapmaker'),
        Path('/appdata/hermes/tools/orcaslicer/squashfs-root/resources/profiles/Snapmaker'),
    ]
    for vendor_root in vendor_root_candidates:
        try:
            if not vendor_root.exists():
                continue
        except (OSError, IndexError):
            continue
        if vendor_root not in vendor_dirs:
            vendor_dirs.append(vendor_root)
        for sub in ('filament', 'process', 'machine'):
            cand = vendor_root / sub
            if cand.is_dir() and cand not in vendor_dirs:
                vendor_dirs.append(cand)

    while cur_path is not None and cur_path.exists():
        try:
            cur = json.loads(cur_path.read_text())
        except (OSError, json.JSONDecodeError):
            break
        chain.append(cur)
        parent_name = cur.get('inherits') or ''
        if not parent_name or parent_name in visited:
            unresolved_inherits = None
            break
        visited.add(parent_name)
        parent_path: Path | None = None
        search_dirs: list[Path] = [cur_path.parent]
        if cur_path.parent.parent.exists():
            search_dirs.extend(p for p in cur_path.parent.parent.iterdir() if p.is_dir())
        search_dirs.extend(vendor_dirs)
        for d in search_dirs:
            cand = d / f'{parent_name}.json'
            if cand.is_file():
                parent_path = cand
                break
        if parent_path is None:
            unresolved_inherits = parent_name
            break
        cur_path = parent_path
        unresolved_inherits = None

    merged: dict[str, Any] = {}
    for layer in reversed(chain):
        merged.update(layer)
    if unresolved_inherits is not None:
        merged['inherits'] = unresolved_inherits
    else:
        merged.pop('inherits', None)
    return merged


def _materialize_flat_filament(filament_path: Path, out_dir: Path, orca_bin: Path | None = None) -> Path:
    """Flatten a filament profile into a self-contained temp in out_dir,
    so Orca can load it from any path without needing to resolve inheritance
    from data dirs. Returns the temp path.

    Also propagates `hot_plate_temp` to `textured_plate_temp` / `eng_plate_temp`
    / `cool_plate_temp` (and their initial_layer variants) when those are
    zero/missing. Snapmaker's stock filament profiles only set hot_plate_temp,
    but process profiles often pick a different bed_type (Textured PEI,
    Engineering Plate). Without this propagation, Orca writes
    first_layer_bed_temperature=0 to the gcode metadata, which then trips
    the U1 upload gate's PETG temp check. Caught live 2026-06-25 round 5."""
    data = _flatten_filament_profile(filament_path, orca_bin=orca_bin)

    def _zero_or_missing(v: Any) -> bool:
        if v is None:
            return True
        if isinstance(v, list):
            return not v or str(v[0]).strip() in ('0', '0.0', '')
        return str(v).strip() in ('0', '0.0', '')

    hot = data.get('hot_plate_temp')
    if hot and not _zero_or_missing(hot):
        for k in ('textured_plate_temp', 'eng_plate_temp', 'cool_plate_temp'):
            if _zero_or_missing(data.get(k)):
                data[k] = hot
    hot_initial = data.get('hot_plate_temp_initial_layer')
    if hot_initial and not _zero_or_missing(hot_initial):
        for k in ('textured_plate_temp_initial_layer',
                  'eng_plate_temp_initial_layer',
                  'cool_plate_temp_initial_layer'):
            if _zero_or_missing(data.get(k)):
                data[k] = hot_initial

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f'{filament_path.stem}__flat.json'
    out.write_text(json.dumps(data, indent=2))
    return out


def _flatten_process_profile(process_path: Path, orca_bin: Path | None = None) -> dict[str, Any]:
    """Walk the `inherits` chain and merge each layer into a self-contained
    dict. Most-derived (leaf) wins on conflicts.

    Parent lookup order:
      1. Same dir as the current layer
      2. Sibling subdirs under the current layer's grandparent (Snapmaker's
         stock layout: process/, filament/, machine/ are siblings)
      3. Orca vendor dir: <orca_bin>/../../resources/profiles/Snapmaker/
         and its process/ subdir (for chains like
         fdm_process_U1_0.20 → fdm_process_common which only live in the
         appimage's bundled vendor dir, not in our profiles/snapmaker-stock/)

    Critical correctness note (cold review 2026-06-25): if the chain
    terminates with an UNRESOLVED `inherits` (parent name we couldn't
    find), preserve that `inherits` field on the merged result so Orca's
    own resolver can take over at slice time. Stripping inherits when
    the chain isn't fully flattened produces a broken profile worse than
    the original."""
    visited: set[str] = set()
    chain: list[dict[str, Any]] = []
    cur_path: Path | None = process_path
    unresolved_inherits: str | None = None

    if orca_bin is None:
        orca_bin = DEFAULT_ORCA
    vendor_dirs: list[Path] = []
    # Primary path: orca_bin's own squashfs-root tree. Works in production
    # where orca_bin IS the real binary. Fallbacks for the test-harness/
    # wrapper case where parents[1] isn't a squashfs-root.
    vendor_root_candidates = [
        orca_bin.resolve().parents[1] / 'resources' / 'profiles' / 'Snapmaker',
        Path('/opt/data/tools/orcaslicer/squashfs-root/resources/profiles/Snapmaker'),
        Path('/appdata/hermes/tools/orcaslicer/squashfs-root/resources/profiles/Snapmaker'),
    ]
    for vendor_root in vendor_root_candidates:
        try:
            if not vendor_root.exists():
                continue
        except (OSError, IndexError):
            continue
        if vendor_root not in vendor_dirs:
            vendor_dirs.append(vendor_root)
        for sub in ('process', 'filament', 'machine'):
            cand = vendor_root / sub
            if cand.is_dir() and cand not in vendor_dirs:
                vendor_dirs.append(cand)

    while cur_path is not None and cur_path.exists():
        try:
            cur = json.loads(cur_path.read_text())
        except (OSError, json.JSONDecodeError):
            break
        chain.append(cur)
        parent_name = cur.get('inherits') or ''
        if not parent_name or parent_name in visited:
            unresolved_inherits = None
            break
        visited.add(parent_name)
        # Look up the parent by name. Same dir → sibling subdirs → vendor.
        parent_path: Path | None = None
        search_dirs: list[Path] = [cur_path.parent]
        if cur_path.parent.parent.exists():
            search_dirs.extend(p for p in cur_path.parent.parent.iterdir() if p.is_dir())
        search_dirs.extend(vendor_dirs)
        for d in search_dirs:
            cand = d / f'{parent_name}.json'
            if cand.is_file():
                parent_path = cand
                break
        if parent_path is None:
            # Couldn't resolve — preserve the unresolved name so Orca's own
            # resolver can take over at slice time (it has its own profile
            # search paths). Stripping it would leave a broken profile.
            unresolved_inherits = parent_name
            break
        cur_path = parent_path
        unresolved_inherits = None

    merged: dict[str, Any] = {}
    for layer in reversed(chain):
        merged.update(layer)
    if unresolved_inherits is not None:
        merged['inherits'] = unresolved_inherits
    else:
        merged.pop('inherits', None)
    return merged


def apply_supports_override(process_path: Path, enable_support: bool, out_dir: Path) -> Path:
    """Materialize a temp process profile JSON with enable_support
    overridden to the user's binary choice.

    v1.5.1 Supports? redesign: the user's answer (Supports / No supports)
    wins over whatever the picked preset declares. The temp file lives in
    the slice's out_dir so it's auto-cleaned with the rest of the artifacts.

    Why temp file vs CLI flag: Orca's headless slicer doesn't expose a
    --enable-support override flag. The reliable path is patching the
    process JSON in place. The inheritance chain is flattened into the
    temp so the temp is self-contained — no dependency on Orca finding
    the parent profiles from a non-source path at slice time
    (cold-review F17 mitigation 2026-06-25).

    Returns the temp file path."""
    data = _flatten_process_profile(process_path)
    data['enable_support'] = '1' if enable_support else '0'
    # Audit trail — record the override so anyone inspecting the temp
    # profile knows why it exists.
    data.setdefault('_u1_workflow_notes', []).append(
        f'enable_support overridden to {"1" if enable_support else "0"} per user Supports? answer'
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = process_path.stem + ('__force_supports' if enable_support else '__no_supports')
    temp = out_dir / f'{stem}.json'
    temp.write_text(json.dumps(data, indent=2))
    return temp


def last_used_per_tool(nozzle: str | None = None, history_path: Path | None = None) -> dict[str, str]:
    """Return a {tool_id: print_settings_id} map for the most recent print on
    each tool that matches the given nozzle.

    Cold-review G16: analysis-phase history lookup happens BEFORE the user
    picks a tool (args.tool is None at that phase). last_used_print_settings_id
    with tool=None returns the single most-recent print across ALL tools,
    which can mislead a multi-tool user. This helper emits the per-tool
    breakdown so the agent can show "T0 last used X, T1 last used Y" and the
    user picks knowing the full picture. The single-most-recent helper stays
    for the picker's recommendation seed; the per-tool map is supplementary."""
    try:
        from u1_config import get_data_dir
        path = history_path if history_path else (get_data_dir() / 'print_history.json')
        if not path.exists():
            return {}
        data = json.loads(path.read_text())
    except Exception:
        return {}
    records = data.get('records', []) if isinstance(data, dict) else []
    if not records:
        return {}
    nz = (nozzle or '').strip().lower()
    nz_token = f'({nz} nozzle)' if nz else ''
    by_tool: dict[str, tuple[str, str]] = {}  # tool -> (timestamp, psid)
    for rec in records:
        if nz_token:
            printer_id = (rec.get('printer_settings_id') or '').lower()
            if nz_token not in printer_id:
                continue
        active_field = rec.get('active_tool')
        if isinstance(active_field, dict):
            tool = (active_field.get('tool') or '').strip()
        else:
            tool = str(active_field or '').strip()
        if not tool:
            continue
        ts = rec.get('last_seen_at') or rec.get('completed_at') or ''
        psid = rec.get('print_settings_id') or ''
        if not psid:
            continue
        prev = by_tool.get(tool)
        if prev is None or ts > prev[0]:
            by_tool[tool] = (ts, psid)
    return {tool: psid for tool, (_, psid) in by_tool.items()}


def last_used_print_settings_id(tool: str | None = None, nozzle: str | None = None, history_path: Path | None = None) -> str | None:
    """Read print_history.json and return the most recent successful print's
    print_settings_id for the given tool/nozzle.

    Operator request (v1.5.1 live test): is it pulling in the last used presets for the
    selected nozzle". Yes, now. Filters records to those matching the
    nozzle (printer_settings_id contains '(N.N nozzle)') and optionally
    the tool (active_tool == 'T1' etc.); sorts by last_seen_at DESC; returns
    the first record's print_settings_id.

    Returns None if no history file, no matching records, or any read error
    (fail-soft: missing history is normal, shouldn't break the slice flow)."""
    try:
        from u1_config import get_data_dir
        path = history_path if history_path else (get_data_dir() / 'print_history.json')
        if not path.exists():
            return None
        data = json.loads(path.read_text())
    except Exception:
        return None
    records = data.get('records', []) if isinstance(data, dict) else []
    if not records:
        return None
    nz = (nozzle or '').strip().lower()
    # Cold-review F7: gate by `if nz`, not `if nz_token` — nz_token is
    # '( nozzle)' (truthy) when nozzle is empty, which silently dropped every
    # record because no printer_settings_id contains that literal. With this
    # fix, an empty nozzle = no nozzle filter (caller wants any tool match).
    nz_token = f'({nz} nozzle)' if nz else ''
    # Cold-review F15: simpler — empty-string after strip().lower() coerces
    # to None via the trailing `or None`. No redundant `if tool else None`
    # wrapper around the same expression.
    tool_str = (tool or '').strip().lower() or None

    def _matches(rec: dict) -> bool:
        if nz_token:
            printer_id = (rec.get('printer_settings_id') or '').lower()
            if nz_token not in printer_id:
                return False
        if tool_str:
            # active_tool is a dict in real records ({'tool': 'T1', ...}), but
            # may be a bare string in older records / extracted fixtures.
            active_field = rec.get('active_tool')
            if isinstance(active_field, dict):
                active = (active_field.get('tool') or '').lower()
            else:
                active = (active_field or '').lower()
            if active != tool_str:
                return False
        return True

    matching = [r for r in records if _matches(r)]
    if not matching:
        return None
    matching.sort(key=lambda r: r.get('last_seen_at') or r.get('completed_at') or '', reverse=True)
    return matching[0].get('print_settings_id')


def profile_path(profile: str) -> Path:
    """Resolve user's preset choice to a process-profile path.

    Uses upp.normalize_value() so that user-typed names like
    `0.20 Strength @Snapmaker U1 (0.4 nozzle)` resolve to the same slug
    the picker emits in opt['value'] — no second slug-normalization path
    to drift from profile_id.

    Supports promotion is handled upstream in run_workflow() before
    reaching this point — by the time we're here, the profile name
    is already either a literal supports preset or a deliberate
    non-supports preset. No supports-fallback logic in profile_path."""
    requested = upp.normalize_value(str(profile))
    for opt in list_profiles():
        if opt['value'] == requested:
            return Path(opt['path'])
    # No literal match → fail closed by raising. The workflow's pre-slice
    # phase emits the helpful 'no profiles found' setup_required event
    # before reaching this point in practice.
    raise RuntimeError(
        f"profile {profile!r} not found in any source. "
        "Run `tools/fetch_snapmaker_profiles.py` to bundle Snapmaker stock, "
        "or `tools/extract_profiles_from_printer.py` to extract from your printer's history."
    )

def filament_path(material: str, nozzle: str = '0.4') -> Path:
    """Find a filament profile JSON matching `material` (e.g. 'PETG', 'PLA').

    Scans the same multi-source dirs as list_profiles, but for filament
    profiles instead of process. snapmaker-stock uses a `filament/` subdir;
    extract_profile_from_gcode writes `*_filament.json` next to its
    process JSON. Match is filename-substring (case-insensitive).

    `nozzle` (e.g. '0.4') gates by nozzle compatibility. v1.5.0-dev live
    test caught the wrong-nozzle silent fallback: filament_path('PETG')
    was returning 'Snapmaker PETG HF @U1 0.2 nozzle.json', whose
    `compatible_printers` field lists only the 0.2 nozzle machine. Orca
    rejected it on a 0.4-nozzle slice and fell back to the machine's
    default_filament_profile ('Snapmaker PLA'), stamping the gcode with
    `filament_type=PLA` — which then tripped the upload gate's PETG check.

    Filter rules (mirror of u1_profile_picker._nozzle_matches):
      * Stem encodes matching nozzle ('(0.4 nozzle)' or '_0_4_nozzle') → keep
      * Stem encodes a DIFFERENT nozzle → drop
      * Stem has no 'nozzle' token → keep (generic/multi-nozzle profile)

    Returns the first hit by source priority. Raises RuntimeError when no
    source has a matching filament — same shape as profile_path's
    failure mode, so the workflow surfaces a uniform setup error."""
    material_norm = material.lower()
    # Token-boundary match so 'PETG' doesn't match 'PETG-CF' / 'PETG-GF' but
    # DOES match both Snapmaker-stock convention ('Generic PETG @U1 ...',
    # space-bounded) and extracted-profile convention ('my_petg_filament',
    # underscore-bounded). Caught live 2026-06-25 — old substring match
    # returned 'Snapmaker PETG-CF @U1.json' for a PETG slice; \b alone
    # treated underscore as a word char so it missed '_petg_' boundaries.
    material_re = re.compile(rf'(?:^|[\s_]){re.escape(material_norm)}(?:$|[\s_])')
    nz = nozzle.strip().lower()
    nz_token = f'({nz} nozzle)'
    nz_slug = f'_{nz.replace(".", "_")}_nozzle'

    def _nozzle_ok(stem_lower: str) -> bool:
        # Filament naming: bare ' N.N nozzle' (no parens). Process naming:
        # parenthesized '(N.N nozzle)'. Extracted profiles: '_N_N_nozzle'.
        if nz_token in stem_lower or nz_slug in stem_lower or f' {nz} nozzle' in stem_lower:
            return True
        return 'nozzle' not in stem_lower

    def _compatible_with_nozzle(path: Path) -> bool:
        """Cold-review F13: read the filament JSON's `compatible_printers`
        field and reject if it explicitly lists machines that don't match
        our nozzle. Empty/missing field = compatible with all (the common
        case for user-extracted profiles). Catches the same fallback-to-PLA
        risk as filename heuristic but for off-convention names where the
        stem doesn't encode a nozzle but compatible_printers does."""
        try:
            with path.open('r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return True  # fail-soft: can't read = don't filter
        compat = data.get('compatible_printers')
        if not compat:
            return True
        for entry in compat:
            if nz_token in str(entry).lower():
                return True
        return False

    for label, source_dir in upp.DEFAULT_SOURCES:
        if not source_dir.exists():
            continue
        # Cold-review F14: dedupe via resolved path; the two rglobs below
        # otherwise re-scan the same files when filament/ subdir entries
        # also match the '*_filament.json' pattern at source_dir root.
        candidates: list[Path] = []
        seen: set[Path] = set()
        filament_subdir = source_dir / 'filament'
        if filament_subdir.is_dir():
            for p in filament_subdir.rglob('*.json'):
                rp = p.resolve()
                if rp not in seen:
                    seen.add(rp)
                    candidates.append(p)
        for p in source_dir.rglob('*_filament.json'):
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                candidates.append(p)
        # Prefer U1-tagged + skip the @base / @U1 base / @U1 base2 inheritance
        # bases (Orca will follow the inherits chain from a U1-tuned variant).
        # 'base' is the broad Orca convention marker for inheritance-only
        # profiles; the older '@base'-only check missed '@U1 base.json' and
        # '@U1 base2.json' siblings that were being returned as if loadable.
        for path in candidates:
            stem = path.stem.lower()
            if not material_re.search(stem):
                continue
            if 'base' in stem:
                continue
            if not _nozzle_ok(stem):
                continue
            if not _compatible_with_nozzle(path):
                continue
            return path
        # No U1-tuned hit — fall back to any material-matching file
        # (including @base / U1 base — Orca's inheritance will still resolve
        # the right settings, but only if nothing concrete matched first).
        for path in candidates:
            stem = path.stem.lower()
            if not material_re.search(stem):
                continue
            if not _nozzle_ok(stem):
                continue
            if not _compatible_with_nozzle(path):
                continue
            return path
    raise RuntimeError(
        f"no filament profile found for material {material!r} compatible with "
        f"{nozzle!r} nozzle. Run `tools/fetch_snapmaker_profiles.py` to bundle "
        "Snapmaker stock filaments, or `tools/extract_profiles_from_printer.py` "
        "to extract from your printer's history."
    )

_WARN_GEOMETRY_TOKENS = ('floating cantilever', 'floating region', 'overhang')


def parse_orca_warnings(text: str) -> list[str]:
    """Filter Orca's stdout for geometric concern warnings (floating
    cantilever / floating region / overhang).

    Requires BOTH a warning/error severity marker AND a geometry token —
    earlier versions matched 'overhang' alone, which had a false-positive
    risk on info/debug lines that mention 'overhang' in passing
    (e.g. progress reports, success messages). Severity gate stops those
    from leaking into the slicer_warning event."""
    warnings = []
    for line in text.splitlines():
        low = line.lower()
        if 'warning' not in low and 'error' not in low:
            continue
        if not any(t in low for t in _WARN_GEOMETRY_TOKENS):
            continue
        clean = line.strip()
        if clean and clean not in warnings:
            warnings.append(clean)
    return warnings

def _tool_to_index(tool) -> int:
    """Parse 'T1' / '1' / 'extruder1' / 'extruder' (== 0) into the integer slot index."""
    s = str(tool).strip().lower()
    if s in ('', 'none', 'extruder'):
        return 0
    if s.startswith('t'):
        s = s[1:]
    if s.startswith('extruder'):
        s = s[len('extruder'):]
    try:
        return int(s) if s else 0
    except ValueError:
        return 0

def inject_snapmaker_thumbnails(gcode: Path, source_stl: Path, sizes: str = '48x48,300x300') -> dict:
    """Inject Snapmaker-format thumbnail blocks into the sliced G-code so the
    U1 touchscreen shows a preview instead of a generic icon. Uses the bundled
    tools/gcode_inject_thumbnail.py.

    Default sizes match Snapmaker's own Orca profile — the U1 machine JSON in
    Snapmaker/OrcaSlicer declares `thumbnails: 48x48/PNG, 300x300/PNG`.
    OrcaSlicer's CLI doesn't emit thumbnail blocks (GUI-only code path), so
    without this injection step every headless-sliced print lands on the U1
    with a generic icon. Live-verified on the U1 touchscreen 2026-06-24.

    Fail-soft: if PIL/numpy missing, STL malformed, or any other error,
    returns {'ok': False, 'error': ...} so the surrounding slice still ships.
    The slice is more important than the preview image.
    """
    try:
        from gcode_inject_thumbnail import main as inject_main  # bundled tool
        rc = inject_main([
            '--stl', str(source_stl),
            '--gcode', str(gcode),
            '--sizes', sizes,
            '--in-place',
        ])
        return {'ok': rc == 0, 'sizes': sizes, 'returncode': rc}
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {e}', 'sizes': sizes}

def rewrite_gcode_for_tool(gcode: Path, tool_idx: int) -> int:
    """Orca's --load-filaments puts the filament into slot 0, so generated
    gcode references T0 in start/end blocks even when the user picked T1+.
    This rewrites T0 -> T<tool_idx> throughout the file, while preserving
    multi-tool slot-literal commands like 'M104 S0 T0 A0' / 'M104 S0 T1 A0'
    which target each slot individually (those are not initial-extruder refs).
    Returns the number of lines rewritten."""
    if tool_idx == 0:
        return 0
    text = gcode.read_text()
    # Match lines like 'M104 S0 T0 A0' or 'M104 S0 T1 A0' (multi-tool slot ops)
    multi_tool = re.compile(r'^M\d+\s+S\d+\s+T\d+\s+A\d+\b')
    t0 = re.compile(r'\bT0\b')
    out=[]
    changed=0
    for line in text.split('\n'):
        if multi_tool.match(line):
            out.append(line)
            continue
        new, n = t0.subn(f'T{tool_idx}', line)
        if n:
            changed += 1
        out.append(new)
    gcode.write_text('\n'.join(out))
    return changed

def machine_profile_for_orca(orca_bin: Path = DEFAULT_ORCA) -> Path:
    """Resolve the Snapmaker U1 (0.4 nozzle) vendor machine profile.

    Primary path: orca_bin.resolve().parents[1] / resources/profiles/...
    This works when orca_bin IS the real Orca binary inside its
    squashfs-root tree (production slice path).

    Fallbacks: well-known absolute locations on this host. Needed when
    orca_bin is a wrapper/shim (test harness, /opt/orca-via-* style)
    whose parents[1] isn't the Orca tree — without these, the function
    falls back to ROOT/'profiles/machine/snapmaker_u1_0_4_nozzle.json'
    which carries a stale 'MyToolChanger 0.4 nozzle - Copy'
    printer_settings_id that Orca then rejects as incompatible with
    stock process profiles (verified live 2026-06-26)."""
    candidates = [
        orca_bin.resolve().parents[1] / 'resources/profiles/Snapmaker/machine/Snapmaker U1 (0.4 nozzle).json',
        Path('/opt/data/tools/orcaslicer/squashfs-root/resources/profiles/Snapmaker/machine/Snapmaker U1 (0.4 nozzle).json'),
        Path('/appdata/hermes/tools/orcaslicer/squashfs-root/resources/profiles/Snapmaker/machine/Snapmaker U1 (0.4 nozzle).json'),
    ]
    for c in candidates:
        if c.exists():
            return c
    return ROOT/'profiles/machine/snapmaker_u1_0_4_nozzle.json'

def real_orca_slice(oriented_stl: Path, out_gcode: Path, tool: str, material: str, profile: str, orca_bin: Path = DEFAULT_ORCA, nozzle: str = '0.4', process_path_override: Path | None = None)->dict[str,Any]:
    out_gcode.parent.mkdir(parents=True, exist_ok=True)
    machine=machine_profile_for_orca(orca_bin)
    # process_path_override lets the caller pass an already-resolved process
    # JSON path (e.g. the temp profile produced by apply_supports_override).
    # When provided, it bypasses the picker lookup so a temp file outside any
    # source dir is loadable.
    process = process_path_override if process_path_override else profile_path(profile)
    # Flatten the filament's inherits chain into a temp file. Without this,
    # Orca's --load-filaments silently failed to resolve 'Snapmaker PETG @U1'
    # → 'Snapmaker PETG @U1 base' → 'fdm_filament_petg' inheritance when
    # the input path was in our profiles/snapmaker-stock/ tree (NOT Orca's
    # data dir). Result: gcode stamped filament_type=PLA, bed_temp=45, upload
    # gate rejected. Caught live 2026-06-25 in round 5 of testing.
    filament_resolved = filament_path(material, nozzle=nozzle)
    filament = _materialize_flat_filament(filament_resolved, out_gcode.parent, orca_bin=orca_bin)
    cmd=[
        str(orca_bin),
        '--load-settings', f'{machine};{process}',
        '--load-filaments', str(filament),
        '--outputdir', str(out_gcode.parent),
        '--slice', '0',
        str(oriented_stl),
    ]
    before={p.resolve() for p in out_gcode.parent.glob('*.gcode')}
    proc=subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=orca_env(orca_bin), timeout=600)
    after=sorted(out_gcode.parent.glob('*.gcode'), key=lambda p: p.stat().st_mtime, reverse=True)
    produced=next((p for p in after if p.resolve() not in before), after[0] if after else None)
    if proc.returncode != 0 or produced is None or produced.stat().st_size == 0:
        raise RuntimeError(f'Orca slice failed rc={proc.returncode}: {proc.stdout[-4000:]}')
    if produced.resolve() != out_gcode.resolve():
        if out_gcode.exists(): out_gcode.unlink()
        produced.rename(out_gcode)
    # Rewrite T0 -> T<chosen> for non-default tool picks. Orca's --load-filaments
    # always loads the filament into slot 0, so the generated start/end blocks
    # reference T0 even when the user picked T1+. Without this rewrite, the
    # printer would heat and use the wrong extruder — a real safety issue
    # caught by the camera-gated start during the 2026-06-24 live test.
    tool_idx = _tool_to_index(tool)
    tool_rewrites = rewrite_gcode_for_tool(out_gcode, tool_idx)
    # Inject Snapmaker-format thumbnails so the U1 touchscreen shows a preview
    # instead of a generic icon. Sizes match the U1 machine profile in
    # Snapmaker/OrcaSlicer. OrcaSlicer's CLI itself never emits thumbnail
    # blocks (GUI-only render path), so without this step every headless print
    # lands on the U1 with no preview. Fail-soft — preview is nice-to-have.
    thumbnails = inject_snapmaker_thumbnails(out_gcode, oriented_stl)
    info=parse_gcode_metadata(out_gcode)
    meta=info.get('metadata', {})
    flb=parse_first_layer_bbox(out_gcode)
    return {
        'gcode': str(out_gcode),
        'cmd': cmd,
        'profiles': {'machine': str(machine), 'process': str(process), 'filament': str(filament)},
        'returncode': proc.returncode,
        'warnings': parse_orca_warnings(proc.stdout),
        'stdout_tail': proc.stdout[-4000:],
        'tool_idx': tool_idx,
        'tool_rewrites': tool_rewrites,
        'thumbnails': thumbnails,
        'metadata': meta,
        'first_layer_bbox': flb,
        'time': meta.get('estimated printing time (normal mode)') or meta.get('estimated printing time'),
        'weight_g': meta.get('filament used [g]') or meta.get('total filament used [g]'),
    }

def upload_only(gcode: Path, dry_run: bool=True)->dict[str,Any]:
    # v1.5.1 P9: human_summary + host_path keys close the dry-run fabrication hole.
    # Without them, agents seeing `dry_run: True` invented plausible "tool gate
    # blocked the upload" / "filament mismatch" explanations from chat memory
    # (live-test failure 2026-06-25). human_summary = verbatim text the agent
    # surfaces; host_path = explicit "this is on the Hermes host, NOT the printer"
    # so the agent can't claim the file is on printer storage.
    if dry_run:
        return {
            'print_started': False,
            'print_queued': False,
            'dry_run': True,
            'host_path': str(gcode),
            'path': str(gcode),  # back-compat with consumers that key on 'path'
            'human_summary': (
                'DRY-RUN ONLY — no file sent to printer. The gcode exists at '
                f'{gcode} on the Hermes host filesystem; it has NOT been uploaded '
                'to the U1\'s onboard storage. Moonraker was not contacted. '
                'To actually upload, re-run with --live-upload. Do NOT claim the '
                'upload succeeded or describe state on the printer — none of '
                'that happened.'
            ),
        }
    return _real_upload(gcode, on_collision=None)


def _real_upload(gcode: Path, on_collision: str | None) -> dict[str, Any]:
    """Drive u1_upload_gcode.py with the audit 2026-06-26 return-code contract.

    Codes:
      0  upload + post-upload validation OK
      2  upload BLOCKED before contact (gcode metadata fails, no storage, ...)
      3  upload SUCCEEDED but post-upload validation produced blockers
      4  Moonraker transport failed; no printer-side file confirmed
      5  filename collision unresolved (when on_collision=None)

    Workflow's human_summary derives from the actual contract, not from
    "rc != 0 = no file." Reads the latest_upload_result.json artifact for
    granular truth (moonraker_upload_ok / remote_metadata_ok)."""
    cmd = [sys.executable, str(HERE / 'u1_upload_gcode.py'), str(gcode)]
    if on_collision:
        cmd.extend(['--on-collision', on_collision])
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    rc = proc.returncode

    # Read the granular result the helper just wrote
    granular: dict[str, Any] = {}
    try:
        from u1_config import get_data_dir
        artifact = get_data_dir() / 'latest_upload_result.json'
        if artifact.exists():
            granular = json.loads(artifact.read_text())
    except Exception:
        granular = {}

    # Collision (rc=5 with helper packet on stdout)
    if rc == 5:
        return {
            'print_started': False,
            'dry_run': False,
            'returncode': rc,
            'host_path': str(gcode),
            'output': proc.stdout[-4000:],
            'filename_collision': True,
            'target_filename': gcode.name,
            'human_summary': (
                f'A file named {gcode.name} already exists on the U1. The workflow '
                'will ask you what to do: timestamped rename (recommended), '
                'overwrite existing, or cancel. No upload has been performed yet.'
            ),
        }
    # User-cancelled collision (rc=6, cold review F9 2026-06-26).
    if rc == 6:
        return {
            'print_started': False,
            'dry_run': False,
            'returncode': rc,
            'host_path': str(gcode),
            'output': proc.stdout[-4000:],
            'cancelled': True,
            'cancelled_reason': 'user picked Cancel at filename collision prompt',
            'human_summary': (
                'Upload cancelled by operator at the filename collision prompt. '
                f'The existing {gcode.name} on the U1 is unchanged. No upload was '
                'performed this round.'
            ),
        }

    # Transport failure (rc=4: Moonraker upload itself failed)
    if rc == 4:
        return {
            'print_started': False,
            'dry_run': False,
            'returncode': rc,
            'host_path': str(gcode),
            'output': proc.stdout[-4000:],
            'moonraker_upload_ok': False,
            'remote_metadata_ok': False,
            'human_summary': (
                'Moonraker upload transport failed — no file confirmed on the U1. '
                'See output field for the actual error from u1_upload_gcode.py — '
                'surface it verbatim, do not paraphrase or invent a cause.'
            ),
        }

    # Pre-flight blocked (rc=2: gcode metadata gate, storage, etc.)
    if rc == 2:
        return {
            'print_started': False,
            'dry_run': False,
            'returncode': rc,
            'host_path': str(gcode),
            'output': proc.stdout[-4000:],
            'human_summary': (
                'Upload BLOCKED before contacting the printer. The gcode '
                'failed a pre-flight check (printer_id / filament_type / storage '
                'space). See output field for the actual blocker — surface it '
                'verbatim, do not paraphrase or invent a cause.'
            ),
        }

    # Cold-review defense (2026-06-26): be explicit about rc=0 vs rc=3, and
    # treat ANY unexpected rc as transport failure rather than silently
    # falling into the success-with-warnings branch (which is what an
    # unhandled exception's rc=1 would otherwise do).
    if rc not in (0, 3):
        return {
            'print_started': False,
            'dry_run': False,
            'returncode': rc,
            'host_path': str(gcode),
            'output': proc.stdout[-4000:],
            'moonraker_upload_ok': False,
            'remote_metadata_ok': False,
            'human_summary': (
                f'Upload helper exited with unexpected returncode={rc}. No file '
                'confirmed on the U1. See output field for the actual error from '
                'u1_upload_gcode.py — surface it verbatim, do not paraphrase.'
            ),
        }

    # rc=0 (clean) or rc=3 (post-upload warning). In BOTH cases the file
    # reached the printer; the warning is about post-upload state, not transport.
    moonraker_upload_ok = granular.get('moonraker_upload_ok', rc == 0)
    remote_metadata_ok = granular.get('remote_metadata_ok', rc == 0)
    uploaded = granular.get('uploaded_filename') or granular.get('uploaded') or gcode.name
    collision_policy = granular.get('collision_policy')
    post_upload_blockers = granular.get('post_upload_blockers', [])
    post_upload_warnings = granular.get('post_upload_warnings', [])

    if rc == 0:
        summary = f'Upload to U1 via Moonraker succeeded. File is on the printer storage as {uploaded!r}.'
        if collision_policy:
            summary += f' (Collision policy: {collision_policy}.)'
        if post_upload_warnings:
            summary += ' Post-upload warnings: ' + '; '.join(post_upload_warnings)
    else:  # rc == 3
        summary = (
            f'Upload SUCCEEDED — file IS on the printer storage as {uploaded!r}. '
            'Post-upload validation produced state observations '
            f'(blockers: {"; ".join(post_upload_blockers)}), but the file reached '
            'the U1 successfully. Surface the blockers; do not claim the upload failed.'
        )

    result: dict[str, Any] = {
        'print_started': False,
        'dry_run': False,
        'returncode': rc,
        'output': proc.stdout[-4000:],
        'host_path': str(gcode),
        'moonraker_upload_ok': moonraker_upload_ok,
        'remote_metadata_ok': remote_metadata_ok,
        'post_upload_validation_ok': granular.get('post_upload_validation_ok', rc == 0),
        'uploaded_filename': uploaded,
        'target_filename': granular.get('target_filename', gcode.name),
        'filename_already_existed': granular.get('filename_already_existed', False),
        'collision_policy': collision_policy,
        'post_upload_blockers': post_upload_blockers,
        'post_upload_warnings': post_upload_warnings,
        'human_summary': summary,
    }
    if rc == 0:
        try:
            from u1_upload_gcode import query_moonraker_metadata
            from u1_config import get_u1_host, get_u1_port
            meta = query_moonraker_metadata(get_u1_host(), get_u1_port(), uploaded)
            if meta:
                result['moonraker_metadata'] = meta
        except Exception:
            pass
    return result

def _bbox_dims(stl_path: Path) -> tuple[float, float, float]:
    """Return (x_span, y_span, z_span) of an STL's bounding box. Used to detect
    whether an auto-orient rotation actually changed the geometry vs identity."""
    verts = parse_stl(stl_path).reshape(-1, 3)
    return (
        float(verts[:, 0].max() - verts[:, 0].min()),
        float(verts[:, 1].max() - verts[:, 1].min()),
        float(verts[:, 2].max() - verts[:, 2].min()),
    )

def _bboxes_differ(stl_a: Path, stl_b: Path, tol_mm: float = 0.5) -> bool:
    """True iff the two STLs have meaningfully different bbox dimensions in
    position — i.e., a rotation was applied that changed the per-axis dims
    by >tol_mm. Position-aware: an axis swap like (80, 163, 140) vs (80, 140,
    163) IS a different orientation even though the set of dimensions is
    identical, so we compare X→X, Y→Y, Z→Z. Used to decide whether the
    auto-oriented render is worth showing as a separate image from the
    source-as-authored render."""
    try:
        a = _bbox_dims(stl_a)
        b = _bbox_dims(stl_b)
        return any(abs(a[i] - b[i]) > tol_mm for i in range(3))
    except Exception:
        return True  # if we can't compare, err toward "show both"

def _trim_option_payload(opts: list[dict[str, Any]], keep_keys: tuple[str, ...] = ('label', 'value', 'recommended', 'material', 'loaded', 'supports_status', 'source', 'has_supports')) -> list[dict[str, Any]]:
    """Strip large/internal fields from need_input option payloads. Notably
    drops 'path' from profile options (multi-KB file paths the agent doesn't
    need — workflow resolves by value internally). Token-saving for --json-events
    consumers; reduces typical preset event from ~3KB to ~500B."""
    return [{k: v for k, v in o.items() if k in keep_keys} for o in opts]

def write_slice_summary(out_dir: Path, slice_res: dict[str, Any]) -> Path:
    """Write a terse text summary alongside the gcode. Agents should read this
    instead of re-parsing the gcode (gcode reads inline 12KB of base64 thumbnail
    data on every read; this is ~300 bytes)."""
    meta = slice_res.get('metadata', {})
    moonraker = (slice_res.get('moonraker_metadata') or {}) if isinstance(slice_res.get('moonraker_metadata'), dict) else {}
    summary_path = out_dir / 'slice_summary.txt'
    lines = [
        f"time         = {slice_res.get('time', '?')}",
        f"weight_g     = {slice_res.get('weight_g', '?')}",
        f"layer_count  = {moonraker.get('layer_count', '?')}",
        f"layer_height = {meta.get('layer_height', '?')}",
        f"profile      = {meta.get('print_settings_id', '?')}",
        f"material     = {meta.get('filament_type', '?')}",
        f"tool_idx     = {slice_res.get('tool_idx', '?')}",
        f"tool_rewrites= {slice_res.get('tool_rewrites', 0)}",
        f"thumbnails   = {slice_res.get('thumbnails', {}).get('ok', False)}",
        f"warnings     = {', '.join(slice_res.get('warnings', [])) or 'none'}",
        f"gcode        = {slice_res.get('gcode', '?')}",
    ]
    summary_path.write_text('\n'.join(lines) + '\n')
    return summary_path

def run_workflow(args)->dict[str,Any]:
    """v1.4.6 flow: dual-render (source + auto-oriented if different) BEFORE
    asking questions. User sees both orientation options visually before
    answering, so the slice happens once with informed input — no re-do
    cycle. Slice + preview only happen when --yes / --upload-only set,
    i.e., after the agent collected user answers and re-invoked.

    Three phases:
      ANALYSIS — always: triage + render source + render auto (if different)
      DECISION — emit all need_input events (orient/tool/preset/supports)
      COMMIT (only if --yes or --upload-only): slice + preview + summary + upload

    Without --yes, the workflow exits after DECISION. The agent collects
    user answers across turns, then re-invokes with --yes and the flag set
    for each answer.
    """
    model=Path(args.model).resolve()
    ts=time.strftime('%Y%m%d-%H%M%S')
    out_dir=(Path(args.out_dir) if args.out_dir else DEFAULT_OUT_BASE/model.stem.replace(' ','_')/ts).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    # Mirror every emit() to <out_dir>/events.jsonl so harness + agent
    # have a recoverable audit trail independent of stdout capture.
    global _EVENTS_FILE
    _EVENTS_FILE = out_dir / 'events.jsonl'

    # === ANALYSIS PHASE ===
    # Always create the source-as-authored STL (no rotation applied).
    # orient_model writes to a fixed 'oriented.stl' in out_dir, so we rename
    # to 'source.stl' to free that name for the auto-orient pass below.
    source_res = orient_model(model, out_dir, orient='asauthored', down_vec=None)
    source_stl = out_dir / 'source.stl'
    Path(source_res['oriented_stl']).rename(source_stl)
    emit({'stage':'triage', **triage_stl(source_stl)}, args.json_events)

    # v1.5.2: only emit renders when the operator hasn't picked orient yet
    # (args.orient is None). Once picked, the renders are operator-noise — they
    # were the basis for the orient decision; nothing new to look at after.
    # Gemma4-26b in run 8 surfaced render paths on every turn because the
    # workflow re-emitted them every call. Source of truth fix: workflow
    # withholds the emit, agent has nothing to surface.
    _emit_renders = args.orient is None
    source_render = out_dir/'source_as_authored.png'
    source_review = render_slice_review(source_stl, source_render, title='Source mesh — as authored (no rotation)')
    if _emit_renders:
        emit({'stage':'render','image':str(source_render),'kind':'source_as_authored',
              'overhang_area_pct': source_review['overhang_area_pct'],
              'supports_tier': source_review['supports_tier']}, args.json_events)

    # Auto-orient render: produce + emit when args.orient is None (first call —
    # operator needs both renders to compare) OR explicit 'auto'. Skip when
    # 'asauthored' is set — auto-orient never used in that branch.
    auto_stl: Path = source_stl
    auto_orient_meta: dict[str, Any] | None = None
    # Default: recommend auto-orient. When both orientations get rendered
    # below (bbox-differ case), this may flip to 'asauthored' if the
    # as-authored pose has a strictly lower supports_tier.
    recommended_orient = 'auto'
    recommendation_reason: str | None = None
    if args.orient in (None, 'auto'):
        try:
            auto_res = orient_model(model, out_dir, orient='auto', down_vec=args.down_vec)
            # Same rename trick — orient_model wrote to oriented.stl; rename so we
            # don't clobber source on next workflow phase / re-invocation.
            candidate_auto_stl = out_dir / 'auto_oriented.stl'
            Path(auto_res['oriented_stl']).rename(candidate_auto_stl)
            auto_orient_meta = auto_res
            if _bboxes_differ(source_stl, candidate_auto_stl):
                auto_stl = candidate_auto_stl
                auto_render = out_dir/'auto_oriented.png'
                auto_review = render_slice_review(auto_stl, auto_render, title='Auto-oriented (Orca cost-optimal)')
                if _emit_renders:
                    emit({'stage':'render','image':str(auto_render),'kind':'auto_oriented',
                          'overhang_area_pct': auto_review['overhang_area_pct'],
                          'supports_tier': auto_review['supports_tier']}, args.json_events)
                recommended_orient, recommendation_reason = pick_recommended_orient(
                    source_review['supports_tier'], auto_review['supports_tier'])
                # Aspect-ratio warning: if auto is dramatically taller than source's smallest axis,
                # surface a hint that the user might prefer as-authored or a different rotation.
                src_dims = _bbox_dims(source_stl); auto_dims = _bbox_dims(auto_stl)
                emit({
                    'stage':'orient_analysis',
                    'source_dims_mm': list(src_dims),
                    'auto_dims_mm': list(auto_dims),
                    'auto_down_vec': auto_res.get('down_vec'),
                    'source_overhang_area_pct': source_review['overhang_area_pct'],
                    'auto_overhang_area_pct': auto_review['overhang_area_pct'],
                    'source_supports_tier': source_review['supports_tier'],
                    'auto_supports_tier': auto_review['supports_tier'],
                    'recommended_orient': recommended_orient,
                    'recommendation_reason': recommendation_reason,
                    'note': (
                        f"Auto picked Z={auto_dims[2]:.0f}mm; source Z={src_dims[2]:.0f}mm. "
                        f"Auto height is {auto_dims[2]/max(src_dims[2],0.01):.1f}× source — "
                        "consider as-authored if taller print = unwanted."
                    ) if auto_dims[2] > src_dims[2] * 1.5 else None,
                }, args.json_events)
        except Exception as e:
            # Auto-orient failed (Orca missing on review host etc.) — fail-soft.
            # Source is still available; user can choose as-authored or notes.
            # Flip the orient prompt's default to as-authored so the user
            # doesn't pick "Auto-orient (recommended)" and re-trigger the same
            # failure on the next invocation — that's an infinite loop trap.
            recommended_orient = 'asauthored'
            recommendation_reason = (
                f"auto-orient failed ({type(e).__name__}); falling back to "
                "as-authored — re-picking auto would just re-fail")
            emit({'stage':'orient_analysis',
                  'error': f'auto-orient unavailable: {type(e).__name__}: {e}',
                  'recommended_orient': recommended_orient,
                  'recommendation_reason': recommendation_reason}, args.json_events)

    # === DECISION PHASE ===
    # Material options first — query live state if possible, else fall back to supplied.
    try:
        mat_opts=query_material_options(requested_material=args.material) if not args.no_live_material else []
    except Exception:
        mat_opts=[]
    if not mat_opts:
        mat_opts=[{'label':f'{args.tool or "T1"}: {args.material or "PETG"} (supplied/headless)', 'value':args.tool or 'T1', 'material': args.material or 'PETG', 'loaded': None, 'recommended': True}]
    # History-aware recommendation: ask print_history.json what the user last
    # printed on this tool/nozzle. The picker uses that to flag the matching
    # preset as previously_used + dominate the recommendation score. Fail-soft
    # — empty history is the common new-user case, no need to surface.
    history_psid = last_used_print_settings_id(tool=args.tool, nozzle=args.nozzle)
    prof_opts=list_profiles(
        class_hint=args.class_hint or model.stem,
        nozzle=args.nozzle,
        history_print_settings_id=history_psid,
    )
    # Cold-review F18 + G16: always emit history_hint with the per-tool
    # breakdown so the agent has accurate info even when no tool is picked
    # yet (analysis phase: args.tool is None → history_psid is the
    # most-recent-ANY-tool, which can mismatch the eventual chosen tool).
    # tool_filtered tells the agent whether the headline psid was
    # tool-scoped or any-tool. per_tool gives the full breakdown so the
    # agent can show the right preset after the user picks Filament.
    per_tool = last_used_per_tool(nozzle=args.nozzle)
    if history_psid:
        installed_match = any(o.get('previously_used') for o in prof_opts)
        if installed_match:
            emit({'stage': 'history_hint',
                  'last_used_print_settings_id': history_psid,
                  'installed': True,
                  'tool_filtered': bool(args.tool),
                  'per_tool': per_tool}, args.json_events)
        else:
            emit({'stage': 'history_hint',
                  'last_used_print_settings_id': history_psid,
                  'installed': False,
                  'tool_filtered': bool(args.tool),
                  'per_tool': per_tool,
                  'message': (
                      f"Your last print on this tool/nozzle used preset "
                      f"{history_psid!r}, but it isn't installed in the picker. "
                      "Either pick something close from the options, or copy the "
                      "JSON into profiles/user/ before slicing."
                  )}, args.json_events)
    else:
        emit({'stage': 'history_hint',
              'last_used_print_settings_id': None,
              'installed': False,
              'tool_filtered': bool(args.tool),
              'per_tool': per_tool,
              'message': (
                  "No prior prints recorded for this tool/nozzle in "
                  "print_history.json. Recommendation falls back to class/height "
                  "heuristics; surface that to the user instead of inventing a "
                  "previously-used preset."
              )}, args.json_events)
    # Fail-fast for the empty-picker case: better to emit a structured
    # setup_required event right here than let the workflow stumble into
    # profile_path's RuntimeError after rendering. The agent surfaces this
    # to the user with the right "run fetch/extract" guidance.
    if not prof_opts:
        emit({
            'stage': 'setup_required',
            'kind': 'no_profiles',
            'message': (
                "No profiles found in profiles/{from-printer,user,snapmaker-stock}. "
                "Run `python3 tools/fetch_snapmaker_profiles.py` to bundle Snapmaker's "
                "official U1 stock, or `python3 tools/extract_profiles_from_printer.py` "
                "to extract profiles from your printer's recent print history."
            ),
            'missing_sources': [str(d) for _, d in upp.DEFAULT_SOURCES if not d.exists()],
        }, args.json_events)
        return {'phase': 'setup_required', 'out_dir': str(out_dir)}
    # Annotate each preset with its supports relationship so the agent can
    # pre-warn the user before they pick "Add supports" at the next prompt.
    # 'self' = preset already enables supports (read from JSON's
    # enable_support field — works for Snapmaker stock + extracted +
    # community alike); '<name>' = workflow would promote to a same-source
    # sibling on --supports supports; null = no supports variant available
    # in the same source.
    for opt in prof_opts:
        if opt.get('has_supports'):
            opt['supports_status'] = 'self'
        else:
            opt['supports_status'] = promote_to_supports_variant(opt['value'])

    # Audit #6 (2026-06-25): only emit need_input events at analysis phase.
    # At commit phase (--yes provided), all answers are in args; re-emitting
    # the prompts produces stale noise the agent has to filter out.
    _is_analysis_phase = not (args.yes or args.upload_only)
    # v1.5.2: compute the per-call script + model path here so they're
    # visible in both analysis-phase emits AND the COMMIT-phase collision
    # emit (which is structurally outside _is_analysis_phase).
    SCRIPT_PATH = str(Path(__file__).resolve())
    MODEL_PATH = str(model)

    if _is_analysis_phase:
        # Orientation option enrichment (audit #9): include compact dimensions
        # + overhang descriptor so the user can decide without re-reading
        # orient_analysis. Falls back to bare labels when bbox-differ is false.
        _auto_rec = recommended_orient == 'auto'
        _as_authored_rec = recommended_orient == 'asauthored'

        def _dims_text(stl: Path | None) -> str:
            if not stl or not stl.exists():
                return ''
            try:
                d = _bbox_dims(stl)
                return f'{d[0]:.0f}×{d[1]:.0f}×{d[2]:.0f}mm'
            except Exception:
                return ''

        _src_dims = _dims_text(source_stl)
        _auto_dims = _dims_text(auto_stl) if auto_stl != source_stl else ''
        _src_tier = source_review['supports_tier']
        _auto_tier = ''
        try:
            if auto_stl != source_stl:
                _auto_tier = (render_slice_review.__wrapped__ if hasattr(render_slice_review, '__wrapped__') else lambda *a, **k: {})  # noqa: F841
        except Exception:
            pass
        # Pull auto_tier from orient_analysis (already computed above) instead
        # of re-rendering. The orient_analysis event was emitted with this data.
        # If auto_stl == source_stl, no orient_analysis is emitted; no auto tier.
        if auto_stl != source_stl and 'auto_review' in dir():
            _auto_tier = locals().get('auto_review', {}).get('supports_tier', '')

        def _auto_label() -> str:
            base = 'Auto-orient (recommended)' if _auto_rec else 'Auto-orient'
            if _auto_dims:
                base += f' — {_auto_dims}'
                if _auto_tier:
                    base += f', {_auto_tier} overhang'
            return base

        def _as_authored_label() -> str:
            base = 'As-authored (recommended — lower overhangs)' if _as_authored_rec else 'As-authored'
            if _src_dims:
                base += f' — {_src_dims}'
                if _src_tier:
                    base += f', {_src_tier} overhang'
            return base

        # v1.5.2 (2026-06-26): emit ONE need_input at a time — whichever is
        # still None — with per-option next_command. Workflow is the source
        # of truth for "what to run next"; agent just copies the string.
        # Sequential flow: orient → tool/material (paired) → preset → supports → upload.
        prefix = _cmd_prefix(SCRIPT_PATH, MODEL_PATH, args)

        if args.orient is None:
            _orient_prompt = {'stage': 'need_input', 'key': 'orient', 'prompt': 'Orientation?', 'options': [
                {'label': _auto_label(), 'value': 'auto', 'recommended': _auto_rec,
                 'next_command': f'{prefix} --orient auto'},
                {'label': _as_authored_label(), 'value': 'asauthored', 'recommended': _as_authored_rec,
                 'next_command': f'{prefix} --orient asauthored'},
            ]}
            if _as_authored_rec and recommendation_reason:
                _orient_prompt['note'] = recommendation_reason
            emit(_orient_prompt, args.json_events)
            emit({'stage':'awaiting_input','need':'orient'}, args.json_events)
            return {'phase':'analysis_complete','out_dir': str(out_dir),'source_stl': str(source_stl),
                    'source_render': str(source_render),
                    'auto_oriented_stl': str(auto_stl) if auto_stl != source_stl else None}

        if args.tool is None or args.material is None:
            # Tool + material are paired: each option's value is the tool slug
            # AND carries the material. The next_command sets BOTH flags so the
            # agent doesn't have to track the material separately.
            _tool_opts = []
            for opt in _trim_option_payload(mat_opts):
                mat = opt.get('material') or 'PETG'
                tool_v = opt.get('value', 'T1')
                _tool_opts.append({**opt,
                                   'next_command': f'{prefix} --tool {tool_v} --material {_shell_quote(mat)}'})
            emit({'stage':'need_input','key':'tool','prompt':'Toolhead & filament?',
                  'options':_tool_opts}, args.json_events)
            emit({'stage':'awaiting_input','need':'tool'}, args.json_events)
            return {'phase':'analysis_complete','out_dir': str(out_dir),'source_stl': str(source_stl),
                    'source_render': str(source_render),
                    'auto_oriented_stl': str(auto_stl) if auto_stl != source_stl else None}

        if args.profile is None:
            _preset_opts = []
            for opt in _trim_option_payload(prof_opts[:8]):
                slug = opt.get('value', '')
                _preset_opts.append({**opt,
                                     'next_command': f'{prefix} --profile {_shell_quote(slug)}'})
            emit({'stage':'need_input','key':'preset','prompt':'Print preset (process profile)?',
                  'options':_preset_opts,
                  'total_available': len(prof_opts),
                  'truncated': len(prof_opts) > 8,
                  'note': (
                      f'Showing the {min(8, len(prof_opts))} highest-scoring presets out of {len(prof_opts)} for this nozzle. '
                      'You can also type a preset name or substring (e.g. "0.16 fine", "support w", "strength") — the workflow will resolve it. '
                      'Or reply "list" to see all options.'
                  ) if len(prof_opts) > 8 else None}, args.json_events)
            emit({'stage':'awaiting_input','need':'preset'}, args.json_events)
            return {'phase':'analysis_complete','out_dir': str(out_dir),'source_stl': str(source_stl),
                    'source_render': str(source_render),
                    'auto_oriented_stl': str(auto_stl) if auto_stl != source_stl else None}

        if args.supports is None:
            emit({'stage':'need_input','key':'supports','prompt':'Supports?','options':[
                {'label':'Supports','value':'supports',
                 'next_command': f'{prefix} --supports supports'},
                {'label':'No supports','value':'no_supports',
                 'next_command': f'{prefix} --supports no_supports'},
                {'label':'Ask about overhang','value':'overhangs',
                 'next_command': f'{prefix} --supports overhangs'},
            ]}, args.json_events)
            emit({'stage':'awaiting_input','need':'supports'}, args.json_events)
            return {'phase':'analysis_complete','out_dir': str(out_dir),'source_stl': str(source_stl),
                    'source_render': str(source_render),
                    'auto_oriented_stl': str(auto_stl) if auto_stl != source_stl else None}

        # All 4 prompts answered, but --yes not set → ask Upload?. Each option
        # routes through --upload-decision so the post-COMMIT next_action
        # event knows which path to emit (Stage 1 or just "done").
        if not (args.yes or args.upload_only):
            _commit_upload     = f'{prefix} --upload-only --live-upload --yes --upload-decision upload'
            _commit_with_stage = f'{prefix} --upload-only --live-upload --yes --upload-decision upload_start'
            emit({'stage':'need_input','key':'upload','prompt':'Upload?','options':[
                {'label':'Upload only (print=false)','value':'upload','recommended':True,
                 'next_command': _commit_upload},
                {'label':'Upload + start gate','value':'upload_start',
                 'next_command': _commit_with_stage},
                {'label':'Cancel','value':'cancel',
                 'next_command': None},
            ]}, args.json_events)

    # === COMMIT PHASE === (only runs when --yes / --upload-only present)
    # Without --yes, we exit after DECISION so the agent can collect answers
    # across user turns without burning a real slice on speculation.
    if not (args.yes or args.upload_only):
        emit({'stage':'awaiting_input','note':'no slice performed — re-invoke with --yes plus collected answers'}, args.json_events)
        return {
            'phase':'analysis_complete',
            'out_dir': str(out_dir),
            'source_stl': str(source_stl),
            'source_render': str(source_render),
            'auto_oriented_stl': str(auto_stl) if auto_stl != source_stl else None,
        }

    # v1.5.2: with None defaults for collected answers, fall back to sane
    # values in the COMMIT phase so direct-CLI users + tests don't break.
    if args.supports is None: args.supports = 'no_supports'
    if args.orient is None: args.orient = 'auto'
    tool=choose_default(mat_opts, args.tool) or 'T1'
    material=args.material or mat_opts[0].get('material','PETG')
    profile=choose_default(prof_opts, args.profile) or '020_strength'

    # P5: fail-fast pre-validation. If the resolved profile slug isn't in
    # the freshly-listed pickable profiles, surface the mismatch BEFORE
    # slicing — agents have been known to recommend a preset from chat
    # memory that's no longer in the picker (e.g. v1.4.x community
    # profiles moved to examples/). Cheaper to fail here than after a
    # render + Orca invocation that'll RuntimeError in profile_path().
    _resolved = upp.normalize_value(str(profile))
    _all_slugs = {o['value'] for o in list_profiles()}
    if _resolved not in _all_slugs:
        nearby = [s for s in _all_slugs if _resolved.split('_')[0] in s][:5]
        emit({'stage':'setup_required','kind':'profile_not_in_picker',
              'requested': str(profile), 'resolved_slug': _resolved,
              'message': (f"profile {profile!r} (slug {_resolved!r}) not found in any source. "
                          "Likely recommended from history but not currently installed. "
                          "Either pick a slug from the Preset? need_input event or copy "
                          "the missing JSON into profiles/user/."),
              'nearby_slugs': nearby}, args.json_events)
        return {'phase':'setup_required','out_dir': str(out_dir)}

    # v1.5.1 Supports? plumbing: the user's binary answer wins over the
    # preset's enable_support state. If user picked 'supports' or 'no_supports'
    # we materialize a temp process profile JSON with the field overridden
    # and pass that to Orca instead of the picker's original. If user picked
    # 'overhangs' (= "ask about overhang"), the workflow can't run the slice
    # until the agent re-prompts — emit a hint and exit at decision phase.
    #
    # The toolkit invokes Orca without a supports CLI flag — supports state
    # flows through the resolved profile's `enable_support` field. So the
    # temp profile is the final say on whether the slice has supports.
    process_path_resolved = profile_path(profile)
    if args.supports == 'overhangs':
        emit({'stage':'awaiting_input',
              'note': ("user picked 'Ask about overhang' — workflow won't slice "
                       "until the agent surfaces overhang_area_pct + supports_tier "
                       "from the orient_analysis event, then re-asks Supports? "
                       "with a concrete supports / no_supports answer.")},
             args.json_events)
        return {'phase':'awaiting_user_supports_decision','out_dir': str(out_dir)}
    if args.supports in ('supports', 'no_supports'):
        enable = args.supports == 'supports'
        process_path_resolved = apply_supports_override(process_path_resolved, enable, out_dir)
        emit({'stage':'supports_override',
              'enable_support': '1' if enable else '0',
              'process_path': str(process_path_resolved),
              'reason': f"user picked '{'Supports' if enable else 'No supports'}' — "
                       'apply_supports_override materialized a temp profile with '
                       f'enable_support={"1" if enable else "0"}'},
             args.json_events)

    chosen_stl = auto_stl if args.orient == 'auto' else source_stl
    gcode=out_dir/(model.stem.replace(' ','_')+'_plate_1.gcode')
    # Cold review 2026-06-26: skip re-slicing on collision-resolution re-runs.
    # First run writes gcode + slice_res.json before exiting at the collision
    # prompt. Operator picks rename/overwrite, agent re-invokes with
    # --on-collision <answer> + --out-dir <same_dir>; we reload the cached
    # slice instead of paying ~50-100s for another Orca call. Force a re-slice
    # by passing a fresh --out-dir.
    slice_cache = out_dir / 'slice_res.json'
    if args.on_collision and gcode.exists() and slice_cache.exists():
        slice_res = json.loads(slice_cache.read_text())
        emit({'stage': 'slice_reused',
              'gcode': str(gcode),
              'note': ('Skipped re-slicing — reusing artifacts from the prior '
                       'run that exited at the filename-collision prompt. Pass '
                       'a fresh --out-dir to force a re-slice.')},
             args.json_events)
    else:
        emit({'stage':'slicing'}, args.json_events)
        slice_res=real_orca_slice(chosen_stl, gcode, str(tool), str(material), str(profile), nozzle=args.nozzle, process_path_override=process_path_resolved)
        try:
            slice_cache.write_text(json.dumps(slice_res, default=str))
        except Exception:
            pass  # cache is an optimization — don't break the slice if disk write fails
    # Surface Orca-emitted slicer warnings (floating cantilever, overhang
    # regions, etc.) as a discrete event BEFORE the preview render. Same
    # event shape as the supports plumbing's warning events — kind names
    # the bucket, messages carries the list. The summary stage still
    # carries warnings too, for backward-compat with consumers that
    # already key on it; this event is the "surface prominently" signal
    # for the agent.
    if slice_res.get('warnings'):
        emit({'stage':'warning','kind':'slicer_warning',
              'messages':list(slice_res['warnings']),
              'count':len(slice_res['warnings']),
              'note':("Orca flagged geometric concerns in the sliced output "
                      "(floating cantilever, overhang region, etc.). Review "
                      "with the user before they trust the preview.")},
             args.json_events)
    preview=out_dir/'preview.png'
    review=render_slice_review(chosen_stl, preview, gcode=gcode, title='Final preview from oriented STL + G-code')
    emit({'stage':'render','image':str(preview),'kind':'preview'}, args.json_events)
    # Write slice_summary.txt — terse text artifact the agent should read
    # instead of re-parsing the gcode (which inlines thumbnail base64 blobs).
    summary_path = write_slice_summary(out_dir, slice_res)
    # P6: derive width/depth (mm) from the raw bbox so the agent doesn't have
    # to render it as the four-number tuple. bbox = (xmin, xmax, ymin, ymax).
    _flb = review['first_layer_bbox']
    _flw = round(_flb[1] - _flb[0], 1) if _flb and len(_flb) >= 2 else None
    _fld = round(_flb[3] - _flb[2], 1) if _flb and len(_flb) >= 4 else None
    emit({'stage':'summary',
          'time':slice_res['time'],
          'weight_g':slice_res['weight_g'],
          'warnings':slice_res['warnings'],
          'first_layer_bbox':_flb,
          'first_layer_width_mm': _flw,
          'first_layer_depth_mm': _fld,
          'summary_file': str(summary_path),
         }, args.json_events)
    if args.cancel:
        emit({'stage':'cancelled'}, args.json_events); return {'cancelled': True, 'out_dir': str(out_dir)}
    if args.upload_only or args.yes:
        # Audit 2026-06-26: pass the operator's collision-resolution answer
        # through to the helper. None = no answer yet → helper detects
        # collision + emits prompt (returncode 5).
        if not args.live_upload:
            up = upload_only(gcode, dry_run=True)
        else:
            up = _real_upload(gcode, on_collision=args.on_collision)
        emit({'stage':'uploaded', **up}, args.json_events)
        # If the helper detected a collision and no resolution was supplied,
        # emit a structured need_input prompt + exit so the agent surfaces it.
        if up.get('cancelled'):
            # F9: rc=6 from helper means the user explicitly cancelled at the
            # collision prompt. Emit a cancelled stage event and stop —
            # don't re-prompt.
            emit({'stage': 'cancelled',
                  'reason': up.get('cancelled_reason', 'upload cancelled')},
                 args.json_events)
            return {'cancelled': True, 'out_dir': str(out_dir)}
        if up.get('filename_collision'):
            # Cold review F3 (2026-06-26): don't pre-compute a timestamp in the
            # label. The helper picks one at upload time; pre-computing here
            # would mislead the operator with a name that's not what actually
            # ends up on the printer.
            # v1.5.2: collision options carry per-option next_command. The
            # workflow already wrote slice_res.json to out_dir, so the agent
            # just runs the option's command — no synthesis, no risk of
            # missing --out-dir and triggering a wasteful re-slice.
            _collision_prefix = f'{_cmd_prefix(SCRIPT_PATH, MODEL_PATH, args)} --upload-only --live-upload --yes --upload-decision {args.upload_decision} --out-dir {_shell_quote(str(out_dir))}'
            emit({
                'stage': 'need_input',
                'key': 'filename_collision',
                'prompt': 'Filename collision?',
                'options': [
                    {'label': 'Upload with timestamped name (UTC stamp added at upload time)',
                     'value': 'rename', 'recommended': True,
                     'next_command': f'{_collision_prefix} --on-collision rename'},
                    {'label': f'Overwrite existing {up["target_filename"]}',
                     'value': 'overwrite',
                     'next_command': f'{_collision_prefix} --on-collision overwrite'},
                    {'label': 'Cancel', 'value': 'cancel',
                     'next_command': None},
                ],
                'note': up['human_summary'],
                'out_dir': str(out_dir),
                'resume_hint': ('Each option\'s next_command already includes '
                                '--out-dir + --on-collision. Tool-call verbatim. '
                                'The workflow will emit slice_reused on the next '
                                'turn to confirm the cached slice was reused.'),
            }, args.json_events)
            return {'phase': 'awaiting_collision_resolution', 'out_dir': str(out_dir)}
        # Audit #7 (2026-06-25): readiness_card consolidates the final
        # decision-relevant facts for the agent's pre-start narrative.
        # Especially: the CHOSEN orientation's overhang tier (not whichever
        # orientation was scored higher) so the agent surfaces the actual
        # print-risk for the user's decision, not the abstract one.
        _chosen_orient_tier = (
            source_review['supports_tier'] if args.orient == 'asauthored'
            else (locals().get('auto_review', {}).get('supports_tier') or source_review['supports_tier'])
        )
        _chosen_overhang_pct = (
            source_review['overhang_area_pct'] if args.orient == 'asauthored'
            else (locals().get('auto_review', {}).get('overhang_area_pct') or source_review['overhang_area_pct'])
        )
        # Audit response (round 11): use printer storage filename (basename)
        # in the start command. Moonraker's /printer/print/start looks up by
        # storage name, not host path. Passing the host path produced HTTP 400.
        # Cold review F10 (2026-06-26): if a collision was resolved as rename,
        # the actual storage name has the timestamp suffix — pull from the
        # helper's reported uploaded_filename, not the original gcode name.
        _printer_filename = up.get('uploaded_filename') or gcode.name
        _tool_idx = slice_res.get('tool_idx', 0)
        _start_extruder = 'extruder' if _tool_idx == 0 else f'extruder{_tool_idx}'
        _stage1_cmd = (
            f'python3 /opt/data/scripts/u1_print_start_gate.py {_printer_filename} '
            f'--intended-tool {_start_extruder} --requested-material {_shell_quote(str(material))}'
        )
        emit({
            'stage': 'readiness_card',
            'orient': args.orient,
            'orient_supports_tier': _chosen_orient_tier,
            'orient_overhang_area_pct': _chosen_overhang_pct,
            'tool': str(tool),
            'material': str(material),
            'profile': str(profile),
            'supports_override': args.supports,
            'first_layer_width_mm': _flw,
            'first_layer_depth_mm': _fld,
            'gcode_host_path': str(gcode),
            'printer_storage_filename': _printer_filename,
            'uploaded': up,
            'start_gate_stage1_command': _stage1_cmd,
            'next_step_if_starting': (
                f"Stage 1: run start_gate_stage1_command. The gate captures a "
                f"REAL bed photo + writes an approval token. Surface the photo "
                f"to the operator; they say yes/no. If yes, re-run with "
                f"--bed-clear start --approval-token <token-from-stage-1>."
            ),
            'warning_if_overhang_risky': (
                f"Chosen orientation has {_chosen_overhang_pct:.1f}% overhang ({_chosen_orient_tier} tier). "
                'Surface this before the start question if no_supports was picked.'
                if args.supports == 'no_supports' and _chosen_orient_tier in ('heavy', 'very heavy')
                else None
            ),
        }, args.json_events)

        # v1.5.2 (2026-06-26): emit a next_action_required event AFTER the
        # readiness_card so the agent's flow stays "tool-call the command
        # the workflow handed me" — same shape as the per-option
        # next_command in the slice loop. Gemma4-26b skipped Stage 1 in
        # harness run 6 because the readiness_card was descriptive rather
        # than imperative. This event is imperative: agent SHOULD just
        # tool-call command. No synthesis, no decision tree.
        if args.upload_decision == 'upload_start':
            emit({
                'stage': 'next_action_required',
                'reason': ('Operator chose "Upload + start gate" at Upload?. '
                           'Run Stage 1 to capture a real bed photo + approval token. '
                           'This call NEVER starts the print — only the photo+token. '
                           'The operator then visually approves the photo before Stage 2.'),
                'command': _stage1_cmd,
            }, args.json_events)
        else:
            # 'upload' (no start) or fallback — workflow is complete here.
            emit({
                'stage': 'complete',
                'reason': ('Operator chose "Upload only" at Upload?. File is on the '
                           'printer; no Stage 1 photo is needed. Workflow is done — '
                           'tell the operator the upload finished and stop.'),
            }, args.json_events)
    return {'out_dir': str(out_dir), 'oriented_stl': str(chosen_stl), 'source_render': str(source_render), 'preview': str(preview), 'gcode': str(gcode), 'slice': slice_res, 'summary_file': str(summary_path)}

def main(argv=None)->int:
    ap=argparse.ArgumentParser(description='Canonical U1 slice workflow')
    ap.add_argument('model'); ap.add_argument('--json-events', action='store_true'); ap.add_argument('--yes', action='store_true')
    # v1.5.2 (2026-06-26): defaults are None so the workflow can distinguish
    # "operator hasn't answered yet" from "operator picked the default". This
    # is the foundation of the next_command-per-option flow: workflow emits
    # one need_input at a time (whichever is still None), and each option
    # carries its own complete next_command so the agent never synthesizes
    # the command from chat-memory.
    ap.add_argument('--orient', choices=['auto','asauthored'], default=None); ap.add_argument('--down-vec', nargs=3, type=float)
    ap.add_argument('--tool', default=None); ap.add_argument('--material', default=None); ap.add_argument('--profile', default=None); ap.add_argument('--class-hint')
    ap.add_argument('--supports', choices=['supports','no_supports','overhangs'], default=None,
                    help="Binary supports override applied to the picked preset at slice time. "
                         "'supports' = force enable_support=1 (temp profile). "
                         "'no_supports' = force enable_support=0. "
                         "'overhangs' = workflow exits at decision phase; agent surfaces orient_analysis and re-asks.")
    ap.add_argument('--nozzle', default='0.4', help="Nozzle size used by the U1 (default 0.4). Filters the preset picker so wrong-nozzle profiles don't clutter the list.")
    ap.add_argument('--upload-only', action='store_true'); ap.add_argument('--live-upload', action='store_true', help='Actually call Moonraker upload helper; default is dry-run/no printer touch')
    # v1.5.2: explicit upload-decision slug so the workflow can route the
    # post-COMMIT next_action_required event (Stage 1 vs done). Defaults to
    # 'upload' (no Stage 1) for direct-CLI users + tests.
    ap.add_argument('--upload-decision', choices=['upload', 'upload_start'], default='upload',
                    help='Whether operator chose Stage-1 path (upload_start) or upload-only (upload).')
    ap.add_argument('--on-collision', choices=['rename', 'overwrite', 'cancel'], default=None,
                    help="Operator's resolution if the target storage filename already exists on the U1. "
                         "Passed through to u1_upload_gcode.py. Default = unset → helper detects collision "
                         "and emits a filename_collision need_input prompt; re-run with this flag to commit.")
    ap.add_argument('--no-live-material', action='store_true', help='Do not query live material state; use supplied/headless option')
    ap.add_argument('--out-dir', type=Path); ap.add_argument('--cancel', action='store_true')
    a=ap.parse_args(argv); res=run_workflow(a)
    if not a.json_events: print(json.dumps(res, indent=2))
    return 0
if __name__=='__main__': raise SystemExit(main())
