#!/usr/bin/env python3
"""Render the actual oriented mesh used for slicing plus optional first-layer footprint from G-code."""
from __future__ import annotations
import argparse, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from PIL import Image, ImageDraw, ImageFont
from _stl_render import parse_stl, render_view, bbox, overhang_mask, face_areas

_NUM=r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)'
MOVE_RE=re.compile(rf'\bG0?1\b.*\bX({_NUM})\b.*\bY({_NUM})\b', re.I)
Z_RE=re.compile(rf'\bZ({_NUM})\b', re.I)

def first_layer_bbox(gcode: Path, max_z: float=0.5):
    pts=[]; z=0.0
    for line in gcode.read_text(errors='replace').splitlines():
        m=Z_RE.search(line)
        if m: z=float(m.group(1))
        mm=MOVE_RE.search(line)
        if mm and z <= max_z:
            pts.append((float(mm.group(1)), float(mm.group(2))))
    if not pts: return None
    xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
    return (min(xs), max(xs), min(ys), max(ys))

def _font(size):
    try: return ImageFont.truetype('DejaVuSans.ttf', size)
    except Exception: return ImageFont.load_default()

_TIER_ORDER = ("low", "moderate", "heavy", "very heavy")

def tier_index(tier: str) -> int:
    """Numeric severity of a supports_tier label. Lower index = less burden.
    Unknown strings raise ValueError so a typo in the workflow can't silently
    decay to 'low'."""
    return _TIER_ORDER.index(tier)


def pick_recommended_orient(source_tier: str, auto_tier: str) -> tuple[str, str]:
    """Pick which orientation to recommend given supports tiers from both
    renders. Returns (orient, reason) where orient is 'auto' or 'asauthored'.

    Strict-better-tier rule: as-authored wins only when its tier is strictly
    lower than auto's. Ties stay with auto because Orca's auto-orient also
    optimizes total material cost beyond just supports — overriding it on a
    tie would discard Orca's broader judgement."""
    if tier_index(source_tier) < tier_index(auto_tier):
        return ('asauthored',
                f"as-authored supports tier ({source_tier}) is lower than "
                f"auto-orient ({auto_tier}) — auto-orient picked a pose with "
                "more downward-facing area")
    if auto_tier == source_tier:
        return ('auto',
                f"auto-orient supports tier ({auto_tier}) matches as-authored ({source_tier})")
    return ('auto',
            f"auto-orient supports tier ({auto_tier}) is lower than as-authored ({source_tier})")


def supports_tier(overhang_area_pct: float) -> str:
    """Qualitative supports-burden tier from the % of mesh surface area
    facing downward (i.e. `overhang_area_pct` from area-weighted
    overhang_mask). Returns one of: 'low', 'moderate', 'heavy', 'very heavy'.

    Boundaries (5/15/30%) are chosen so a clean part with just its bottom
    face flagged sits in 'low', and a part dominated by downward surfaces
    lands in 'heavy'/'very heavy'. We deliberately publish a tier label —
    not a numeric "X% extra filament" — because the chain from overhang
    area to slicer-emitted support cost depends on infill, support style,
    speed, etc. that this tool doesn't know."""
    p = max(0.0, float(overhang_area_pct))
    if p < 5: return "low"
    if p < 15: return "moderate"
    if p < 30: return "heavy"
    return "very heavy"

def render_slice_review(stl: Path, out: Path, gcode: Path|None=None, title: str|None=None) -> dict:
    tris=parse_stl(stl)
    mask=overhang_mask(tris)             # threshold=-0.3 ≈ 17° below horizontal
    areas=face_areas(tris)
    n_over=int(mask.sum()); n_tot=int(tris.shape[0])
    tot_area=float(areas.sum()); over_area=float(areas[mask].sum()) if n_tot else 0.0
    over_pct=(100.0*over_area/tot_area) if tot_area > 0 else 0.0
    tier=supports_tier(over_pct)
    W,H=1800,1200; bg=(22,26,31); panel=(31,36,43); text=(232,238,245); muted=(160,170,180); green=(80,220,130); orange=(255,135,62)
    img=Image.new('RGB',(W,H),bg); d=ImageDraw.Draw(img); f=_font(24); small=_font(18)
    xmin,xmax,ymin,ymax,zmin,zmax=bbox(tris)
    d.text((24,20), (title or 'Slice review').upper(), fill=text, font=f)
    d.text((24,55), f'DIMS {xmax-xmin:.1f} x {ymax-ymin:.1f} x {zmax-zmin:.1f} mm   Z MIN {zmin:.2f}', fill=muted, font=small)
    labels=[('ISOMETRIC - ORIENTED STL', 'iso'), ('SIDE ON PRINTER - ORIENTED STL', 'side'), ('TOP VIEW - ORIENTED STL', 'top')]
    pw=(W-96)//3; ph=760
    for i,(lab,view) in enumerate(labels):
        x=24+i*(pw+24); y=100
        d.rectangle([x,y,x+pw,y+ph], fill=panel); d.text((x+12,y+8), lab, fill=muted, font=small)
        v=render_view(tris, pw, ph-40, view=view, overhang_flags=mask, bg=panel)
        img.paste(v,(x,y+40))
    flb=None
    if gcode and gcode.exists():
        flb=first_layer_bbox(gcode)
    y=900
    d.text((24,y), 'FIRST-LAYER FOOTPRINT (from G-code): ' + (str(tuple(round(v,2) for v in flb)) if flb else 'not available'), fill=green if flb else muted, font=small)
    d.text((24,y+28), f'OVERHANGS (orange): {over_pct:.1f}% of mesh area  ({n_over}/{n_tot} faces, >17° below horizontal)', fill=orange if n_over else muted, font=small)
    d.text((24,y+56), f'SUPPORTS LIKELY NEEDED: {tier.upper()}  (qualitative tier; Orca decides actual support placement)', fill=muted, font=small)
    out.parent.mkdir(parents=True, exist_ok=True); img.save(out, format='PNG', optimize=True)
    return {
        'image': str(out),
        'stl_bbox': [xmin,xmax,ymin,ymax,zmin,zmax],
        'first_layer_bbox': flb,
        'overhang_faces': n_over,
        'total_faces': n_tot,
        'overhang_area_pct': round(over_pct, 2),
        'supports_tier': tier,
    }

def main(argv=None):
    ap=argparse.ArgumentParser(); ap.add_argument('stl', type=Path); ap.add_argument('--gcode', type=Path); ap.add_argument('--out', type=Path, required=True); ap.add_argument('--title')
    a=ap.parse_args(argv); res=render_slice_review(a.stl,a.out,a.gcode,a.title); print(res['image']); return 0
if __name__=='__main__': raise SystemExit(main())
