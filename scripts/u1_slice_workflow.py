#!/usr/bin/env python3
"""Canonical Snapmaker U1 end-to-end slice workflow.

The workflow owns the 10-step operator flow: triage -> orientation -> material
-> profile -> render -> supports -> slice -> preview -> upload-only/start choice
-> camera-gated start. It intentionally prefers upload-only and fail-closed start.
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys, time
from pathlib import Path
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
from render_slice_review import render_slice_review
from render_slice_review import first_layer_bbox as parse_first_layer_bbox

DEFAULT_OUT_BASE=ROOT/'artifacts'/'slice_workflow'

def emit(obj: dict[str,Any], json_events: bool=False):
    if json_events: print(json.dumps(obj), flush=True)
    else:
        stage=obj.get('stage','event'); print(f'[{stage}] '+', '.join(f'{k}={v}' for k,v in obj.items() if k!='stage'))

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

PROFILE_FILES={
    '020_strength': ROOT/'profiles/community_020_strength_u1_textured_pei.json',
    '0.20 strength': ROOT/'profiles/community_020_strength_u1_textured_pei.json',
    '020_strength_supports': ROOT/'profiles/community_020_strength_supports_u1_textured_pei.json',
    '016_optimal': ROOT/'profiles/community_merged_016_optimal_u1_textured_pei.json',
    '0.16 optimal': ROOT/'profiles/community_merged_016_optimal_u1_textured_pei.json',
}
FILAMENT_FILES={
    'PETG': ROOT/'profiles/community_generic_petg_u1_textured_pei.json',
}

def profile_path(profile: str, supports: bool=False) -> Path:
    """Resolve user's preset choice to a process-profile path.

    Previous implementation hardcoded a 4-key dict and silently fell back to
    plain `020_strength` when a user picked `020_strength_gyroid` etc. Now we
    consult the same profile picker the workflow uses to present options, so
    any value the picker returns resolves correctly.
    """
    requested = str(profile).lower().strip().replace(' ', '_').replace('mm', '')
    requested = requested.replace('.', '').strip('_')
    for opt in list_profiles():
        if opt['value'].lower() == requested:
            return Path(opt['path'])
    if supports:
        # User passed a non-supports value but asked for supports — find the
        # corresponding _supports variant.
        base = requested.replace('_supports', '')
        for opt in list_profiles():
            v = opt['value'].lower()
            if base and base in v and 'supports' in v:
                return Path(opt['path'])
    # Legacy hardcoded lookups as last-resort fallback for backwards compat.
    return PROFILE_FILES.get(requested) or PROFILE_FILES.get(str(profile).lower()) or ROOT/'profiles/community_020_strength_u1_textured_pei.json'

def filament_path(material: str) -> Path:
    return FILAMENT_FILES.get(str(material).upper(), ROOT/'profiles/community_generic_petg_u1_textured_pei.json')

def parse_orca_warnings(text: str) -> list[str]:
    warnings=[]
    for line in text.splitlines():
        low=line.lower()
        if any(token in low for token in ('floating cantilever','floating region','overhang')):
            clean=line.strip()
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
    vendor=orca_bin.resolve().parents[1] / 'resources/profiles/Snapmaker/machine/Snapmaker U1 (0.4 nozzle).json'
    if vendor.exists():
        return vendor
    return ROOT/'profiles/machine/snapmaker_u1_0_4_nozzle.json'

def real_orca_slice(oriented_stl: Path, out_gcode: Path, tool: str, material: str, profile: str, supports: bool=False, orca_bin: Path = DEFAULT_ORCA)->dict[str,Any]:
    out_gcode.parent.mkdir(parents=True, exist_ok=True)
    machine=machine_profile_for_orca(orca_bin)
    process=profile_path(profile, supports=supports)
    filament=filament_path(material)
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
    if dry_run:
        return {'print_started': False, 'print_queued': False, 'dry_run': True, 'path': str(gcode)}
    cmd=[sys.executable, str(HERE/'u1_upload_gcode.py'), str(gcode)]
    proc=subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    result: dict[str, Any] = {'print_started': False, 'returncode': proc.returncode, 'output': proc.stdout[-4000:]}
    # On successful upload, augment with Moonraker's rich metadata (estimated_time
    # in seconds, filament_used_mm, slicer/version, uuid, etc). The hand-parser
    # remains the source of truth for safety IDs (print_settings_id etc) — this
    # is purely additive UI/history data. See query_moonraker_metadata docstring.
    if proc.returncode == 0:
        try:
            from u1_upload_gcode import query_moonraker_metadata  # local import to avoid hard dep cycle
            from u1_config import get_u1_host, get_u1_port
            meta = query_moonraker_metadata(get_u1_host(), get_u1_port(), gcode.name)
            if meta:
                result['moonraker_metadata'] = meta
        except Exception:
            pass  # fail-soft: enrichment is nice-to-have, not load-bearing
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

def _trim_option_payload(opts: list[dict[str, Any]], keep_keys: tuple[str, ...] = ('label', 'value', 'recommended', 'material', 'loaded')) -> list[dict[str, Any]]:
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

    # === ANALYSIS PHASE ===
    # Always create the source-as-authored STL (no rotation applied).
    # orient_model writes to a fixed 'oriented.stl' in out_dir, so we rename
    # to 'source.stl' to free that name for the auto-orient pass below.
    source_res = orient_model(model, out_dir, orient='asauthored', down_vec=None)
    source_stl = out_dir / 'source.stl'
    Path(source_res['oriented_stl']).rename(source_stl)
    emit({'stage':'triage', **triage_stl(source_stl)}, args.json_events)

    # Always render the source as authored
    source_render = out_dir/'source_as_authored.png'
    render_slice_review(source_stl, source_render, title='Source mesh — as authored (no rotation)')
    emit({'stage':'render','image':str(source_render),'kind':'source_as_authored'}, args.json_events)

    # If user wants auto-orient (default), also produce + render the auto-oriented version
    # IF it differs from source. Identity-rotations are detected via bbox-dim compare
    # and skipped — no need for two identical images.
    auto_stl: Path = source_stl
    auto_orient_meta: dict[str, Any] | None = None
    if args.orient == 'auto':
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
                render_slice_review(auto_stl, auto_render, title='Auto-oriented (Orca cost-optimal)')
                emit({'stage':'render','image':str(auto_render),'kind':'auto_oriented'}, args.json_events)
                # Aspect-ratio warning: if auto is dramatically taller than source's smallest axis,
                # surface a hint that the user might prefer as-authored or a different rotation.
                src_dims = _bbox_dims(source_stl); auto_dims = _bbox_dims(auto_stl)
                emit({
                    'stage':'orient_analysis',
                    'source_dims_mm': list(src_dims),
                    'auto_dims_mm': list(auto_dims),
                    'auto_down_vec': auto_res.get('down_vec'),
                    'note': (
                        f"Auto picked Z={auto_dims[2]:.0f}mm; source Z={src_dims[2]:.0f}mm. "
                        f"Auto height is {auto_dims[2]/max(src_dims[2],0.01):.1f}× source — "
                        "consider as-authored if taller print = unwanted."
                    ) if auto_dims[2] > src_dims[2] * 1.5 else None,
                }, args.json_events)
        except Exception as e:
            # Auto-orient failed (Orca missing on review host etc.) — fail-soft.
            # Source is still available; user can choose as-authored or notes.
            emit({'stage':'orient_analysis','error': f'auto-orient unavailable: {type(e).__name__}: {e}'}, args.json_events)

    # === DECISION PHASE ===
    # Material options first — query live state if possible, else fall back to supplied.
    try:
        mat_opts=query_material_options(requested_material=args.material) if not args.no_live_material else []
    except Exception:
        mat_opts=[]
    if not mat_opts:
        mat_opts=[{'label':f'{args.tool or "T1"}: {args.material or "PETG"} (supplied/headless)', 'value':args.tool or 'T1', 'material': args.material or 'PETG', 'loaded': None, 'recommended': True}]
    prof_opts=list_profiles(class_hint=args.class_hint or model.stem)

    # All four questions emitted in the same phase. Agent should present them
    # together (or in sequence) with both renders already visible to the user.
    # Option payloads are trimmed — path strings stripped from profile options
    # to keep need_input events compact for token economy.
    emit({'stage':'need_input','key':'orient','prompt':'Orientation?','options':[
        {'label':'Auto-orient (recommended)','value':'auto','recommended':True},
        {'label':'As-authored','value':'asauthored'},
        {'label':'I have notes','value':'notes'},
    ]}, args.json_events)
    emit({'stage':'need_input','key':'tool','prompt':'Filament?','options':_trim_option_payload(mat_opts)}, args.json_events)
    emit({'stage':'need_input','key':'preset','prompt':'Preset?','options':_trim_option_payload(prof_opts[:8])}, args.json_events)
    emit({'stage':'need_input','key':'supports','prompt':'Supports?','options':[
        {'label':'Auto-orient handled it','value':'auto','recommended':True},
        {'label':'Add supports','value':'supports'},
        {'label':'Show me overhangs','value':'overhangs'},
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

    tool=choose_default(mat_opts, args.tool) or 'T1'
    material=args.material or mat_opts[0].get('material','PETG')
    profile=choose_default(prof_opts, args.profile) or '020_strength'
    chosen_stl = auto_stl if args.orient == 'auto' else source_stl
    gcode=out_dir/(model.stem.replace(' ','_')+'_plate_1.gcode')
    emit({'stage':'slicing'}, args.json_events)
    slice_res=real_orca_slice(chosen_stl, gcode, str(tool), str(material), str(profile), supports=False)
    preview=out_dir/'preview.png'
    review=render_slice_review(chosen_stl, preview, gcode=gcode, title='Final preview from oriented STL + G-code')
    emit({'stage':'render','image':str(preview),'kind':'preview'}, args.json_events)
    # Write slice_summary.txt — terse text artifact the agent should read
    # instead of re-parsing the gcode (which inlines thumbnail base64 blobs).
    summary_path = write_slice_summary(out_dir, slice_res)
    emit({'stage':'summary',
          'time':slice_res['time'],
          'weight_g':slice_res['weight_g'],
          'warnings':slice_res['warnings'],
          'first_layer_bbox':review['first_layer_bbox'],
          'summary_file': str(summary_path),
         }, args.json_events)
    if args.cancel:
        emit({'stage':'cancelled'}, args.json_events); return {'cancelled': True, 'out_dir': str(out_dir)}
    if args.upload_only or args.yes:
        up=upload_only(gcode, dry_run=not args.live_upload)
        emit({'stage':'uploaded', **up}, args.json_events)
    else:
        emit({'stage':'need_input','key':'upload','options':[
            {'label':'Upload only','value':'upload','recommended':True},
            {'label':'Upload + start','value':'upload_start'},
            {'label':'Cancel','value':'cancel'},
        ]}, args.json_events)
    return {'out_dir': str(out_dir), 'oriented_stl': str(chosen_stl), 'source_render': str(source_render), 'preview': str(preview), 'gcode': str(gcode), 'slice': slice_res, 'summary_file': str(summary_path)}

def main(argv=None)->int:
    ap=argparse.ArgumentParser(description='Canonical U1 slice workflow')
    ap.add_argument('model'); ap.add_argument('--json-events', action='store_true'); ap.add_argument('--yes', action='store_true')
    ap.add_argument('--orient', choices=['auto','asauthored'], default='auto'); ap.add_argument('--down-vec', nargs=3, type=float)
    ap.add_argument('--tool', default=None); ap.add_argument('--material', default='PETG'); ap.add_argument('--profile', default=None); ap.add_argument('--class-hint')
    ap.add_argument('--upload-only', action='store_true'); ap.add_argument('--live-upload', action='store_true', help='Actually call Moonraker upload helper; default is dry-run/no printer touch')
    ap.add_argument('--no-live-material', action='store_true', help='Do not query live material state; use supplied/headless option')
    ap.add_argument('--out-dir', type=Path); ap.add_argument('--cancel', action='store_true')
    a=ap.parse_args(argv); res=run_workflow(a)
    if not a.json_events: print(json.dumps(res, indent=2))
    return 0
if __name__=='__main__': raise SystemExit(main())
