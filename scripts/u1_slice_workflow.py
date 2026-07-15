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


def _bootstrap_env() -> dict:
    """os.environ copy without PYTHONPATH/PYTHONHOME (escape hatch:
    U1_KEEP_PYTHONPATH=1). A foreign PYTHONPATH is how Hermes Desktop
    poisoned python3=3.13 with a 3.11 venv's compiled Pillow (install
    report 2026-07-10): bare `import PIL` passed, `from PIL import Image`
    died in _imaging. Candidates probed or re-exec'd under the same
    poison fail identically, so every bootstrap subprocess runs sanitized."""
    env = os.environ.copy()
    if env.get('U1_KEEP_PYTHONPATH', '').strip().lower() not in (
            '1', 'true', 'yes', 'on'):
        env.pop('PYTHONPATH', None)
        env.pop('PYTHONHOME', None)
    return env


def _check_python_has_deps(python_path: str, deps: tuple = ('numpy', 'PIL')) -> bool:
    """Return True iff `python_path` can import every dep without error."""
    try:
        proc = subprocess.run(
            [python_path, '-c', 'import numpy; from PIL import Image, ImageDraw'],
            capture_output=True, text=True, timeout=10, env=_bootstrap_env(),
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _reexec(python_path: str) -> None:
    """Continue under `python_path` with the sanitized env. POSIX: execve
    (true process replacement, the caller keeps our stdout/stderr).
    Windows: subprocess + exit with the child's code — execv there does NOT
    replace the process, it abandons the console while the child keeps
    writing, garbling agent-captured output."""
    env = _bootstrap_env()
    env['U1_BOOTSTRAP_REEXEC'] = '1'  # loop guard for the self-retry path
    argv = [python_path, __file__, *sys.argv[1:]]
    if os.name == 'nt':
        proc = subprocess.run(argv, env=env)
        sys.exit(proc.returncode)
    os.execve(python_path, argv, env)


def _ensure_compat_python() -> None:
    """If the current interpreter lacks numpy/PIL, find a known-good Python
    that has them and re-exec self with it. Exit with a clear error if none
    of the candidates work."""
    # Fast path: current interpreter has the deps. Import the COMPILED Pillow
    # entry (Image/ImageDraw), not just the `PIL` package -- a mismatched
    # interpreter can import bare PIL yet fail to load the _imaging C extension,
    # so `import PIL` alone passes a broken install (live on Windows Hermes
    # Desktop with a 3.13/3.11 PYTHONPATH mix, install report 2026-07-10). Catch
    # broad exceptions, since a broken extension can surface as more than
    # ImportError.
    try:
        import numpy  # noqa: F401
        from PIL import Image, ImageDraw  # noqa: F401
        return
    except Exception:
        pass

    # Identify which deps are actually missing (for the error message)
    missing = []
    try:
        import numpy  # noqa: F401
    except Exception:
        missing.append('numpy')
    try:
        from PIL import Image, ImageDraw  # noqa: F401
    except Exception:
        missing.append('pillow')

    # A poisoned PYTHONPATH alone can produce exactly this failure while the
    # interpreter itself is fine. Retry SELF with it cleared before hunting
    # other interpreters (skipped after one re-exec — the loop guard proves
    # the env wasn't the problem).
    if (os.environ.get('U1_BOOTSTRAP_REEXEC') != '1'
            and os.environ.get('PYTHONPATH')
            and _check_python_has_deps(sys.executable)):
        print(
            f'[env] current python lacks {", ".join(missing)} under the '
            f'inherited PYTHONPATH; retrying with it cleared '
            f'(U1_KEEP_PYTHONPATH=1 keeps it)',
            file=sys.stderr,
        )
        _reexec(sys.executable)

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
        str(root / 'venv' / 'Scripts' / 'python.exe'),   # project venv (Windows)
        str(root / '.venv' / 'Scripts' / 'python.exe'),  # hidden venv (Windows)
        '/opt/homebrew/bin/python3',             # macOS Homebrew (Apple Silicon — default for M-series)
        '/usr/local/bin/python3',                # macOS Homebrew (Intel) — legacy install path
    ])

    for cand in candidates:
        if not Path(cand).exists():
            continue
        if _check_python_has_deps(cand):
            # Continue under the working interpreter (sanitized env — the
            # same PYTHONPATH that broke us would break the candidate too).
            # The agent that spawned us still gets the workflow's output.
            print(
                f'[env] current python lacks {", ".join(missing)}; '
                f'switching to {cand}',
                file=sys.stderr,
            )
            _reexec(cand)

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
    if os.environ.get('PYTHONPATH'):
        msg += [
            f'',
            f'NOTE: PYTHONPATH is set ({os.environ["PYTHONPATH"][:200]}).',
            f'A foreign PYTHONPATH can shadow working installs; the checks',
            f'above already ran with it cleared (U1_KEEP_PYTHONPATH=1 keeps it).',
        ]
    print('\n'.join(msg), file=sys.stderr)
    sys.exit(2)


if __name__ == '__main__':
    _ensure_compat_python()

# === After env check passes, do the rest of the imports ===
import argparse, json, re, time
from typing import Any

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
TOOLS=ROOT/'tools'
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(TOOLS))
from _stl_render import parse_stl, bbox  # type: ignore
from u1_orient import orient_model, DEFAULT_ORCA, orca_env
from u1_profile_picker import list_profiles
from u1_upload_gcode import parse_gcode_metadata
import u1_profile_picker as upp
from render_slice_review import first_layer_bbox as parse_first_layer_bbox
import u1_audit


def _resolve_operator(args) -> str:
    """v3a: resolve operator identity for audit + approval rows.

    Priority: --operator CLI flag > U1_OPERATOR env > 'unknown:cli'.
    Never returns None or empty string. Operator strings are short labels
    like 'telegram:brent' / 'cli:local' / 'harness:gemma'.

    Live harness regression 2026-06-28: U1_OPERATOR is loaded from
    /opt/data/.env via u1_config's lazy dotenv loader. That loader fires
    on the first get_data_dir() call — which on the --fresh path runs
    AFTER _resolve_operator. Force the dotenv load here so env-based
    operator identity always lands."""
    cli = getattr(args, 'operator', None)
    if cli:
        return str(cli).strip()
    try:
        import u1_config
        u1_config._load_dotenv_if_present()
    except Exception:
        pass
    env = os.environ.get('U1_OPERATOR', '').strip()
    if env:
        return env
    return 'unknown:cli'


def _audit(request_id: str, event: str, operator: str, **details):
    """Wrapper around u1_audit.append that never raises out of the workflow.

    Audit is observability — a failed disk write here must not break the
    slice. (Same try/except pattern used for the H5 write_request sites in
    Phase 2.) Returns the written record or None on failure."""
    try:
        return u1_audit.append(request_id, event, operator=operator, **details)
    except Exception:
        return None


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


# =============================================================================
# v1.6 (A) — pre-slice Orca mesh-topology analysis
# =============================================================================
# Surfaces Orca's real overhang verdict ("floating cantilever" / "floating
# regions" / overhang-tagged layer count) DURING the analysis phase, before
# the operator commits to a slice. Replaces the conservative-stupid
# face-angle metric used pre-v1.6. See docs/v1.6-design.md for the design,
# the empirical validation, and the specific override values.

# Empirically-validated draft-profile overrides (v1.6 study, 5 fixtures ×
# 5 variants vs ground-truth production slice). PERFECT warning_category
# match + overhang_pct within ±0.4pp of ground truth + 2× faster than
# the production slice. KEEPS production layer_height — that's what
# makes overhang_pct commit-predictive.
_V16_DRAFT_OVERRIDES = {
    'wall_loops': '1',
    'sparse_infill_density': '0%',
    'top_shell_layers': '0',
    'bottom_shell_layers': '0',
    'gcode_thumbnails': '0',
    'enable_support': '0',
}

# Per-plate compute cap (seconds). Defends against pathological huge
# meshes. wall_mount_auto at 765 production layers ran in 16s; benchmark
# fixtures all under 5s. 30s gives headroom + still keeps the analysis
# phase responsive.
_V16_MSTPP_SECS = 30
# Subprocess-level timeout — Orca may hang separately from --mstpp.
_V16_DRAFT_TIMEOUT_SECS = 60
# Clean threshold (warning_category=CLEAN AND overhang_pct below this).
_V16_CLEAN_OVERHANG_THRESHOLD_PCT = 10.0


def _categorize_orca_warning(warning_text: str | None) -> str:
    """Map Orca's raw warning_message string to a stable category.
    See docs/v1.6-design.md for the empirical evidence behind these
    category names (validated on 4 real-world fixtures incl. Snapmaker's
    own 3DBenchy reference print).
    """
    txt = (warning_text or '').strip().lower()
    if not txt or txt == 'null':
        return 'CLEAN'
    if 'floating cantilever' in txt:
        return 'CANTILEVER'
    if 'floating region' in txt:
        return 'FLOATING_REGIONS'
    if 'overhang' in txt:
        return 'OVERHANG_FLAGGED'
    return 'UNKNOWN'


def _count_overhang_layers(gcode_path: Path) -> tuple[int, int]:
    """Count layers containing at least one ;TYPE:Overhang wall segment.
    Returns (overhang_layers, total_layers). Both zero if gcode missing/empty.
    """
    if not gcode_path.exists():
        return 0, 0
    overhang_layers: set[int] = set()
    cur_layer = 0
    try:
        with gcode_path.open() as f:
            for line in f:
                if line.startswith(';LAYER_CHANGE'):
                    cur_layer += 1
                elif cur_layer and line.startswith(';TYPE:Overhang wall'):
                    overhang_layers.add(cur_layer)
    except OSError:
        return 0, 0
    return len(overhang_layers), cur_layer


def _materialize_draft_profile(production_process: Path, dest_dir: Path) -> Path:
    """Build a draft process profile = production + v1.6 compute-skipping
    overrides. Caller passes the FULL (flattened, if needed) production
    process profile path. Writes to <dest_dir>/draft_process.json."""
    src = json.loads(production_process.read_text())
    src.update(_V16_DRAFT_OVERRIDES)
    dest = dest_dir / 'draft_process.json'
    dest.write_text(json.dumps(src))
    return dest


def _draft_slice_analysis(
    stl: Path,
    out_dir: Path,
    production_process: Path,
    filament: Path,
    orca_bin: Path = DEFAULT_ORCA,
) -> dict[str, Any]:
    """Run a fast Orca slice for mesh-topology analysis. Never raises.

    Returns:
      {
        'category': CLEAN | CANTILEVER | FLOATING_REGIONS | OVERHANG_FLAGGED | UNKNOWN | DRAFT_FAILED,
        'warning_text': raw Orca warning_message string (or empty),
        'overhang_layers': int,
        'total_layers': int,
        'overhang_pct': float,           # 0.0 on failure
        'elapsed_ms': int,
        'clean': bool,                    # True iff CLEAN AND < threshold
        'error': str (only on DRAFT_FAILED),
      }

    On Orca failure, returns category=DRAFT_FAILED with the error captured.
    The caller should surface the v1.5.x face-angle metric as fallback and
    flag the failure prominently — never silently fabricate.
    """
    machine = machine_profile_for_orca(orca_bin)
    draft_dir = out_dir / 'draft' / stl.stem
    draft_dir.mkdir(parents=True, exist_ok=True)
    draft_profile = _materialize_draft_profile(production_process, draft_dir)
    cmd = [
        str(orca_bin),
        '--load-settings', f'{machine};{draft_profile}',
        '--load-filaments', str(filament),
        '--outputdir', str(draft_dir),
        '--mstpp', str(_V16_MSTPP_SECS),
        '--slice', '0',
        str(stl),
    ]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=orca_env(orca_bin), timeout=_V16_DRAFT_TIMEOUT_SECS,
        )
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError) as e:
        return {
            'category': 'DRAFT_FAILED',
            'error': f'{type(e).__name__}: {e}',
            'overhang_pct': 0.0,
            'overhang_layers': 0,
            'total_layers': 0,
            'elapsed_ms': int((time.monotonic() - t0) * 1000),
            'clean': False,
        }
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    # Parse result.json — Orca emits it even on some failure modes
    result_path = draft_dir / 'result.json'
    warning_text = ''
    if result_path.exists():
        try:
            res = json.loads(result_path.read_text())
            plate = (res.get('sliced_plates') or [{}])[0]
            warning_text = (plate.get('warning_message') or '').strip()
        except (json.JSONDecodeError, OSError):
            pass

    # Parse gcode for overhang layers
    gcode_path = draft_dir / 'plate_1.gcode'
    overhang_layers, total_layers = _count_overhang_layers(gcode_path)
    overhang_pct = round(100 * overhang_layers / max(1, total_layers), 1) if total_layers else 0.0

    # If Orca returned non-zero AND we got no parseable result, mark failed
    if proc.returncode != 0 and not result_path.exists():
        return {
            'category': 'DRAFT_FAILED',
            'error': f'Orca rc={proc.returncode}: {(proc.stdout or "")[-300:]}',
            'overhang_pct': 0.0,
            'overhang_layers': 0,
            'total_layers': 0,
            'elapsed_ms': elapsed_ms,
            'clean': False,
        }

    category = _categorize_orca_warning(warning_text)
    clean = category == 'CLEAN' and overhang_pct < _V16_CLEAN_OVERHANG_THRESHOLD_PCT
    return {
        'category': category,
        'warning_text': warning_text,
        'overhang_layers': overhang_layers,
        'total_layers': total_layers,
        'overhang_pct': overhang_pct,
        'elapsed_ms': elapsed_ms,
        'clean': clean,
    }


def _compose_orient_note(
    source_draft: dict[str, Any],
    auto_draft: dict[str, Any] | None,
    auto_skip_reason: str | None = None,
) -> str:
    """Plain-language summary for the operator about Orca's verdict.
    Surfaced verbatim in the orient need_input event's `note` field. Agent
    surfaces this verbatim to the operator. No risk math required of the LLM.

    `auto_skip_reason` differentiates WHY auto_draft is None:
      - 'source_clean'   → as-authored is clean (clean-case skip)
      - 'auto_identical' → Orca's auto-orient produced the same bbox as
                           the source, so the second draft would be
                           redundant
      - 'auto_failed'    → auto-orient computation failed earlier in
                           the analysis phase
      - None             → auto_draft is present (or no skip applied)
    """
    def _phrase(d: dict[str, Any], label: str) -> str:
        cat = d.get('category')
        pct = d.get('overhang_pct', 0.0)
        if cat == 'CLEAN':
            return f"{label} is clean (Orca: no warnings, {pct:.0f}% of layers tagged overhang)"
        if cat == 'CANTILEVER':
            return f"{label} has a floating-cantilever warning ({pct:.0f}% of layers tagged overhang)"
        if cat == 'FLOATING_REGIONS':
            return f"{label} has a floating-regions warning ({pct:.0f}% of layers tagged overhang) — Orca suggests re-orient or enable supports"
        if cat == 'OVERHANG_FLAGGED':
            return f"{label} has Orca-flagged overhang concerns ({pct:.0f}% of layers tagged)"
        if cat == 'DRAFT_FAILED':
            return f"{label} draft slice failed ({d.get('error', 'unknown error')[:120]}) — falling back to face-angle estimate"
        return f"{label} draft slice returned UNKNOWN warning category"

    src_phrase = _phrase(source_draft, 'As-authored')
    if auto_draft is not None:
        return f"{src_phrase}. {_phrase(auto_draft, 'Auto-orient')}."
    # auto_draft is None — explain why
    if auto_skip_reason == 'auto_identical':
        return f"{src_phrase}. Auto-orient found no better rotation (same orientation as as-authored)."
    if auto_skip_reason == 'auto_failed':
        return f"{src_phrase}. Auto-orient analysis failed — pick as-authored, or re-run with --orient auto to retry."
    # Default = source_clean (the original clean-case skip)
    return f"{src_phrase}. Auto-orient not analyzed (as-authored is clean — no need to re-orient). Pick option 2 to force a comparison."


def _decide_orient_recommendation(
    source_draft: dict[str, Any],
    auto_draft: dict[str, Any] | None,
) -> str:
    """Return 'asauthored' or 'auto' based on the Orca-verdict tier.

    Priority:
      1. If as-authored is clean (and auto skipped or non-clean) → asauthored
      2. If as-authored failed/risky AND auto is clean → auto
      3. If both risky, prefer the one with lower overhang_pct (tiebreak
         to asauthored — no weird rotation needed)
    """
    src_clean = source_draft.get('clean', False)
    if auto_draft is None:
        # Source was clean; auto was skipped. Recommend source.
        return 'asauthored'
    auto_clean = auto_draft.get('clean', False)
    if src_clean and not auto_clean:
        return 'asauthored'
    if not src_clean and auto_clean:
        return 'auto'
    # Both clean or both risky — prefer lower overhang_pct (tiebreak to as-authored)
    if source_draft.get('overhang_pct', 100.0) <= auto_draft.get('overhang_pct', 100.0):
        return 'asauthored'
    return 'auto'


def orient_verdict(model: Path, out_dir: Path, production_process: Path,
                   filament: Path, down_vec: Any = None,
                   orca_bin: Path = DEFAULT_ORCA) -> dict[str, Any]:
    """Shared single-model orientation verdict — the "fancy bit".

    Draft-slices the as-authored pose and — ONLY when that pose has overhangs —
    the auto-oriented pose, then returns Orca's real recommendation + a
    plain-language note. Data-driven (proven 2026-07-03: on a floating-regions
    model this catches 3%→0% and flips the recommendation to auto). Both the
    single-model path and the unified kit-of-1 path call this so the verdict is
    one source of truth. Never raises. Returns::

        {'recommendation': 'auto'|'asauthored',
         'note': str|None, 'ok': bool, 'error': str (only when not ok)}
    """
    try:
        src_res = orient_model(model, out_dir, orient='asauthored', down_vec=None)
        source_stl = out_dir / 'source.stl'
        Path(src_res['oriented_stl']).rename(source_stl)
        auto_stl = source_stl
        auto_res = orient_model(model, out_dir, orient='auto', down_vec=down_vec)
        cand = out_dir / 'auto_oriented.stl'
        Path(auto_res['oriented_stl']).rename(cand)
        if _bboxes_differ(source_stl, cand):
            auto_stl = cand
        fil = _materialize_flat_filament(filament, out_dir)
        source_draft = _draft_slice_analysis(
            source_stl, out_dir, production_process, fil, orca_bin=orca_bin)
        auto_draft = None
        skip_reason: str | None = None
        # Cheap-when-it-can-be: only draft the SECOND pose when the first has
        # overhangs worth fixing (a clean source needs no comparison).
        if source_draft.get('clean'):
            skip_reason = 'source_clean'
        elif auto_stl == source_stl:
            skip_reason = 'auto_identical'
        else:
            auto_draft = _draft_slice_analysis(
                auto_stl, out_dir, production_process, fil, orca_bin=orca_bin)
        rec = _decide_orient_recommendation(source_draft, auto_draft)
        note = _compose_orient_note(source_draft, auto_draft, skip_reason)
        return {'recommendation': rec, 'note': note, 'ok': True}
    except Exception as e:  # never break the caller's flow over an analysis miss
        return {'recommendation': 'asauthored', 'note': None, 'ok': False,
                'error': f'{type(e).__name__}: {e}'[:200]}


# =============================================================================
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
    more flag + the option's value.

    v2.0 Phase 2: includes --request-id so agent re-invocations pin to the
    same on-disk request. Recovery via content hash still works without it
    but explicit --request-id is more robust against context loss + faster
    (no hash recompute, no directory scan)."""
    parts = ['python3', script_path, _shell_quote(model_path), '--json-events']
    # Pin request_id so chained next_command invocations all hit the same
    # request folder. This is what makes the agent's flow context-loss-resistant.
    if getattr(args, 'request_id', None):
        parts += ['--request-id', args.request_id]
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


def orca_resources_root(orca_bin: Path) -> Path | None:
    """Locate Orca's bundled resources/ dir from the executable location.

    Two layouts exist in the wild:
      Windows portable zip:   <dir>/orca-slicer.exe  +  <dir>/resources/
      Linux AppImage extract: <squashfs-root>/bin/orca-slicer  +
                              <squashfs-root>/resources/

    The old lookup hardcoded orca_bin.parents[1]/resources (AppImage-only).
    On Windows portable that reaches the directory ABOVE the Orca dir, misses
    the bundled vendor profiles, and the inherits-chain flatteners can't
    resolve upstream parents — Orca then silently falls back to PLA defaults
    (reproduced on a real Windows desktop 2026-07-10: upstream PETG leaf
    profile produced filament_type=PLA gcode; the upload gate caught it, but
    the slice itself was wrong). Returns None when neither layout matches
    (wrapper/shim binaries, test harnesses) so callers fall through to their
    well-known absolute candidates."""
    try:
        rp = orca_bin.resolve()
    except OSError:
        rp = orca_bin
    for cand in (rp.parent / 'resources',            # Windows portable
                 rp.parent.parent / 'resources'):    # AppImage: <root>/bin/<exe>
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
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
    resources = orca_resources_root(orca_bin)
    vendor_root_candidates = (
        [resources / 'profiles' / 'Snapmaker'] if resources is not None else []
    ) + [
        Path('/opt/data/tools/orcaslicer/squashfs-root/resources/profiles/Snapmaker'),
        Path('/appdata/hermes/tools/orcaslicer/squashfs-root/resources/profiles/Snapmaker'),
    ]
    for vendor_root in vendor_root_candidates:
        try:
            if not vendor_root.exists():
                continue
        except OSError:
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
    # Primary path: orca_bin's own resources tree (portable OR AppImage, via
    # orca_resources_root). Fallbacks for the test-harness/wrapper case where
    # the binary isn't inside a real Orca tree.
    resources = orca_resources_root(orca_bin)
    vendor_root_candidates = (
        [resources / 'profiles' / 'Snapmaker'] if resources is not None else []
    ) + [
        Path('/opt/data/tools/orcaslicer/squashfs-root/resources/profiles/Snapmaker'),
        Path('/appdata/hermes/tools/orcaslicer/squashfs-root/resources/profiles/Snapmaker'),
    ]
    for vendor_root in vendor_root_candidates:
        try:
            if not vendor_root.exists():
                continue
        except OSError:
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
    # The toolkit's plate previews (top-down footprint + 3D iso) are built by
    # parsing M486 object markers out of the sliced gcode, which Orca only emits
    # when exclude_object is on. Stock/user presets carry exclude_object=1, but a
    # profile extracted from a print's gcode metadata (profiles/from-printer) can
    # lack it, so slicing with one produced a gcode WITHOUT M486 -> the iso view
    # dropped and the footprint degraded (live 2026-07-15, Brent). Force it on
    # for every slice so the previews never depend on the picked profile carrying
    # it. This function always runs before a slice, so it is the single
    # materialization point. (M486 object exclusion is standard + already on for
    # stock/user prints; enabling it universally has no downside.)
    data['exclude_object'] = '1'
    # Audit trail — record the override so anyone inspecting the temp
    # profile knows why it exists.
    data.setdefault('_u1_workflow_notes', []).append(
        f'enable_support overridden to {"1" if enable_support else "0"} per user Supports? answer'
    )
    data['_u1_workflow_notes'].append(
        'exclude_object forced to 1 so the sliced gcode carries M486 object '
        'markers (the toolkit builds its plate previews from them)')
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = process_path.stem + ('__force_supports' if enable_support else '__no_supports')
    temp = out_dir / f'{stem}.json'
    temp.write_text(json.dumps(data, indent=2))
    return temp


# Advanced per-run overrides (v2.3). Keys the operator may override from the
# form's Advanced screen; everything else in the process profile is preserved.
# Same mechanism as apply_supports_override: Orca has no CLI override flags, so
# the reliable path is a flattened, self-contained temp process JSON.
ADVANCED_OVERRIDE_KEYS = (
    'sparse_infill_density', 'sparse_infill_pattern', 'wall_loops',
    'brim_type', 'fuzzy_skin', 'support_type',
)


def apply_profile_overrides(process_path: Path, overrides: dict[str, str],
                            out_dir: Path) -> Path:
    """Materialize a temp process profile with the operator's advanced
    overrides applied (infill density/pattern, wall loops, brim, fuzzy skin).

    Only keys in ADVANCED_OVERRIDE_KEYS are honored — the form layer maps
    operator choices to these; anything else is dropped defensively. Values
    are written as strings in Orca's own formats ('30%', '3', 'gyroid',
    'auto_brim', 'external'). Composes with apply_supports_override: each call
    flattens its input, so chaining temp profiles stays self-contained.

    Returns process_path unchanged when no honored overrides remain.
    """
    honored = {k: str(v) for k, v in (overrides or {}).items()
               if k in ADVANCED_OVERRIDE_KEYS and v not in (None, '')}
    if not honored:
        return process_path
    data = _flatten_process_profile(process_path)
    data.update(honored)
    data.setdefault('_u1_workflow_notes', []).append(
        'advanced overrides applied per operator form answers: '
        + ', '.join(f'{k}={v}' for k, v in sorted(honored.items()))
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    temp = out_dir / f'{process_path.stem}__adv.json'
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

    Primary path: orca_bin's own resources tree via orca_resources_root
    (handles both the Linux AppImage and Windows portable layouts). Works
    when orca_bin IS the real Orca binary (production slice path).

    Fallbacks: well-known absolute locations on this host. Needed when
    orca_bin is a wrapper/shim (test harness, /opt/orca-via-* style)
    that isn't inside a real Orca tree — without these, the function
    falls back to ROOT/'profiles/machine/snapmaker_u1_0_4_nozzle.json'
    which carries a stale 'MyToolChanger 0.4 nozzle - Copy'
    printer_settings_id that Orca then rejects as incompatible with
    stock process profiles (verified live 2026-06-26)."""
    resources = orca_resources_root(orca_bin)
    candidates = (
        [resources / 'profiles/Snapmaker/machine/Snapmaker U1 (0.4 nozzle).json']
        if resources is not None else []
    ) + [
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


def _real_upload(gcode: Path, on_collision: str | None,
                 material: str | None = None) -> dict[str, Any]:
    """Drive u1_upload_gcode.py with the audit 2026-06-26 return-code contract.

    Codes:
      0  upload + post-upload validation OK
      2  upload BLOCKED before contact (gcode metadata fails, no storage, ...)
      3  upload SUCCEEDED but post-upload validation produced blockers
      4  Moonraker transport failed; no printer-side file confirmed
      5  filename collision unresolved (when on_collision=None)

    Workflow's human_summary derives from the actual contract, not from
    "rc != 0 = no file." Reads the latest_upload_result.json artifact for
    granular truth (moonraker_upload_ok / remote_metadata_ok).

    ``material`` (Brent live-test 2026-07-01): passed as ``--material``
    to u1_upload_gcode.py so its filament_type-vs-requested check compares
    the gcode's material against what the WORKFLOW actually chose. Without
    this, u1_upload_gcode.py falls back to its own hardcoded PETG default,
    which false-flags any non-PETG print (e.g. T3/PLA) with a bogus
    'filament_type does not include PETG: PLA' blocker.
    """
    cmd = [sys.executable, str(HERE / 'u1_upload_gcode.py'), str(gcode)]
    if on_collision:
        cmd.extend(['--on-collision', on_collision])
    if material:
        cmd.extend(['--material', str(material)])
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
    # v2.1.0: detect a multi-part kit (a zip holding >1 STL) and redirect to
    # the kit workflow. The single-STL flow below is entirely unchanged. This
    # is the gate-detection principle: the SCRIPT detects the kit; the agent
    # just runs the emitted command (one entrypoint to call, no zip-inspection
    # judgment asked of a small model).
    _kit_err = None
    try:
        import u1_kit
        # The agent sometimes retypes the upload path and mangles the human
        # suffix (a '+' becomes '_'); the doc-hash prefix is stable, so recover
        # the real file BEFORE detection (a mangled path exists as no file at
        # all, so detection would see an empty/absent archive).
        model = u1_kit.resolve_upload_path(model)
        _is_kit = u1_kit.is_multi_part_archive(model)
    except Exception as _kit_exc:
        _is_kit = False
        _kit_err = f'{type(_kit_exc).__name__}: {_kit_exc}'
    # A zip that is MISSING or UNREADABLE must NOT fall into the single-STL flow
    # (which would hand the zip itself to the single-model parser and fail with a
    # confusing "unsupported model file"). The agent occasionally mangles the
    # upload name so the path no longer exists; resolve_upload_path above recovers
    # most of those, but if it still cannot be found or inspected, say so plainly.
    # A zip that EXISTS with a single object is a valid kit-of-one and routes on.
    if str(model).lower().endswith('.zip') and (_kit_err is not None
                                                or not Path(model).exists()):
        emit({'stage': 'kit_detection_failed',
              'error': _kit_err or f'archive not found: {model}',
              'instruction': ('Could not read this zip archive. The upload name '
                              'may have been mistyped; check the file exists, '
                              'then surface this to the operator. Do NOT fall '
                              'back to the single-STL flow.')},
             args.json_events)
        return {'phase': 'kit_detection_failed', 'model': str(model)}
    # v2.2 UNIFIED FLOW: every model routes to the kit workflow. A single STL is
    # a kit-of-1 — the kit workflow ingests a lone STL as one part, and Phase 1
    # gave it the single-model orientation verdict — so it handles single AND
    # multi with one code path (button form → one bed-clear decision → detached
    # gate). The staged single-STL flow below is retired (delegation is now
    # unconditional). --operator is baked ONLY when explicit on this CLI so a
    # test-flavored operator stays sticky across the chain (Fence 1);
    # env-resolved identity stays env-resolved (replay-safe).
    from u1_runtime_paths import (script_shell_path as _script_shell_path,
                                  shell_path as _shell_path,
                                  python_cmd as _python_cmd)
    _kit_cmd = (f'{_python_cmd()} {_script_shell_path("u1_kit_workflow.py")} '
                f'{_shell_quote(_shell_path(model))} --json-events')
    _cli_op = getattr(args, 'operator', None)
    if _cli_op:
        _kit_cmd += f' --operator {_shell_quote(str(_cli_op))}'
    _nozzle = getattr(args, 'nozzle', None)
    if _nozzle and str(_nozzle) != '0.4':
        _kit_cmd += f' --nozzle {_shell_quote(str(_nozzle))}'
    _reason = ('Archive contains multiple STLs — a multi-part kit.' if _is_kit
               else 'Single model — the unified workflow handles it as a kit of one.')
    emit({'stage': 'kit_detected',
          'reason': _reason,
          'command': _kit_cmd,
          'instruction': ('Run this command via terminal. It drives the whole '
                          'job: it emits a form to fill (button UX) — or a '
                          'need_input to answer in the text fallback — then a '
                          'plate preview + a fresh bed photo + ONE bed-clear '
                          'decision. Follow its events exactly (per the skill). '
                          'Do NOT slice, extract, or run any gate command '
                          'yourself.')},
         args.json_events)
    return {'phase': 'kit_redirect', 'command': _kit_cmd, 'model': str(model)}


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
                         "Passed through to u1_upload_gcode.py. Default = unset -> helper detects collision "
                         "and emits a filename_collision need_input prompt; re-run with this flag to commit.")
    ap.add_argument('--no-live-material', action='store_true', help='Do not query live material state; use supplied/headless option')
    ap.add_argument('--out-dir', type=Path,
                    help='LEGACY override of the output dir. Tests + direct CLI use that wants a specific path. '
                         'When set, workflow STILL writes a request.json in there. Otherwise output lands in '
                         '<data_dir>/requests/<request_id>/ (the v2.0 default).')
    ap.add_argument('--request-id', type=str, default=None,
                    help='v2.0 Phase 2: resume an in-flight print request by ID. If the id has no on-disk state, '
                         'workflow fails loud (no silent half-state). When unset, workflow recovers the most-recent '
                         'request whose model_hash matches this STL, or generates a fresh request_id if none.')
    ap.add_argument('--fresh', action='store_true',
                    help='v2.0 Phase 2: ignore any prior in-flight request for this STL and start a brand-new one. '
                         'Useful when operator wants to restart the slicing decision from scratch.')
    ap.add_argument('--operator', type=str, default=None,
                    help='v2.0 Phase 3a: operator identity for audit/approval rows '
                         '(e.g. "telegram:brent", "cli:local", "harness:gemma"). '
                         'Falls back to env var U1_OPERATOR. When neither is set, '
                         'audit rows stamp "unknown:<source>" where source is inferred from invocation.')
    ap.add_argument('--cancel', action='store_true')
    a=ap.parse_args(argv); res=run_workflow(a)
    if not a.json_events: print(json.dumps(res, indent=2))
    return 0
if __name__=='__main__': raise SystemExit(main())
