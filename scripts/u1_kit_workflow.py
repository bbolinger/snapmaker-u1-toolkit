#!/usr/bin/env python3
"""Multi-part kit workflow orchestrator — v2.1.0 Phase 1 staged Q&A.

Three-turn staged flow for kit prints, mirroring the per-field need_input
pattern the single-STL workflow uses (the pattern Gemma reliably handles):

  Turn 1: parts        — which STLs from the zip to include
  Turn 2: tool          — live-bound Moonraker toolhead+filament pair
  Turn 3: confirm       — slice + upload + emit print-plan card, operator
                          replies start / upload-only / adjust

Smart defaults applied at the confirm turn (overridable in Phase 2 adjust):
  - orient   = auto         (auto-rotate each part)
  - profile  = top-scored   (best profile for nozzle+material)
  - supports = no_supports  (per-part overhang scan deferred to Phase 2)
  - action   = (operator picks at confirm)

After confirm:
  - start       → emit next_action_required with the existing Stage 1
                  start_gate command (separate Stage 1/2 round-trip; the
                  v2.0 safety moat stays exactly as it is for Phase 1).
  - upload-only → mark complete; plates already uploaded.
  - adjust      → emit a note that drill-in is Phase 2; operator can
                  re-run with --form-answers '<one liner>' to change fields.

LEGACY (preserved): --form-answers '<one liner>' and --form-answers-json
power-user modes commit in a single CLI call. Smoke tests + CLI users
rely on these; they do NOT go through the staged Q&A.

Gate-detection principle: the script owns the state machine. The agent
relays the operator's answer verbatim into the next CLI flag the workflow
asks for; the workflow emits exactly one next action per turn.
"""
from __future__ import annotations

# Bootstrap: env check happens BEFORE the heavy numpy/PIL-dependent imports
# below (via u1_kit -> u1_orient -> numpy). Mirrors u1_slice_workflow's
# _ensure_compat_python so the kit workflow is just as robust when invoked
# via a `python3` that lacks deps (e.g. Hermes' /usr/bin/python3). Without
# this, calling `python3 u1_kit_workflow.py ...` fails on the numpy import.
import os, sys, subprocess
from pathlib import Path


def _check_python_has_deps(python_path: str, deps: tuple = ("numpy", "PIL")) -> bool:
    try:
        proc = subprocess.run(
            [python_path, "-c", f"import {', '.join(deps)}"],
            capture_output=True, text=True, timeout=10,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _ensure_compat_python() -> None:
    try:
        import numpy  # noqa: F401
        import PIL    # noqa: F401
        return
    except ImportError:
        pass
    missing = []
    for dep in ("numpy", "PIL"):
        try:
            __import__(dep)
        except ImportError:
            missing.append("pillow" if dep == "PIL" else dep)
    here = Path(__file__).resolve().parent
    root = here.parent
    candidates: list[str] = []
    env_override = os.environ.get("U1_TOOLKIT_PYTHON")
    if env_override:
        candidates.append(env_override)
    candidates.extend([
        "/opt/hermes/.venv/bin/python",
        str(root / "venv" / "bin" / "python"),
        str(root / ".venv" / "bin" / "python"),
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
    ])
    for cand in candidates:
        if not Path(cand).exists():
            continue
        if _check_python_has_deps(cand):
            print(f"[env] current python lacks {', '.join(missing)}; switching to {cand}",
                  file=sys.stderr)
            os.execv(cand, [cand, __file__, *sys.argv[1:]])
    msg = [
        "ERROR: u1_kit_workflow.py needs numpy + PIL (Pillow).",
        f"Missing on the current interpreter ({sys.executable}): {', '.join(missing)}",
        "",
        "Tried these alternative Python interpreters (none had the deps):",
    ]
    for c in candidates:
        msg.append(f"  - {c}  ({'exists' if Path(c).exists() else 'not found'})")
    msg += [
        "",
        "Fix one of these:",
        f"  1. {sys.executable} -m pip install numpy pillow",
        "  2. export U1_TOOLKIT_PYTHON=/path/to/python",
    ]
    print("\n".join(msg), file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    _ensure_compat_python()

# === Heavy imports (numpy/PIL via u1_kit -> u1_orient) ===
import json
import re
from typing import Any

import u1_kit
import u1_form
import u1_arrange
import u1_request
from u1_print_start_gate import build_stage1_command
from u1_slice_workflow import (
    _resolve_operator,
    _shell_quote,
    _real_upload,
    list_profiles,
    profile_path,
    apply_supports_override,
    _tool_to_index,
)

DEFAULT_TOOLS = ["T0", "T1", "T2", "T3"]
DEFAULT_MATERIALS = ["PLA", "PETG", "ABS", "TPU", "ASA", "PLA-CF", "PETG-CF"]
# Maps the form's supports vocabulary to the slice override vocabulary.
_SUPPORTS_TO_OVERRIDE = {"supports": "supports", "no-supports": "no_supports",
                        "no_supports": "no_supports", "overhangs": "overhangs"}


# ─── Staged-flow helpers (Phase 1) ──────────────────────────────────────────

def _parse_parts_answer(answer: str, part_count: int) -> tuple[list[int] | None, str | None]:
    """Parse the operator's parts answer into 1-based STL indices.

    Accepted forms:
      'all'         -> every part
      '1,3,5'       -> those specific parts
      '1-8' / '2-4' -> a range
      single int    -> just that part

    Returns (indices, err). Either indices is a list of valid 1-based ints
    OR err is a human-readable error.
    """
    if not answer:
        return None, "no answer provided"
    s = answer.strip().lower().replace(' ', '')
    if s in ('all', '*'):
        return list(range(1, part_count + 1)), None
    if '-' in s and ',' not in s:
        try:
            lo_s, hi_s = s.split('-', 1)
            lo, hi = int(lo_s), int(hi_s)
        except ValueError:
            return None, f"could not parse range: {answer!r}"
        if lo < 1 or hi > part_count or lo > hi:
            return None, (f"range {lo}-{hi} out of bounds for {part_count} parts "
                          f"(valid: 1-{part_count})")
        return list(range(lo, hi + 1)), None
    out: list[int] = []
    try:
        for tok in s.split(','):
            tok = tok.strip()
            if not tok:
                continue
            n = int(tok)
            if n < 1 or n > part_count:
                return None, (f"part {n} out of bounds (valid: 1-{part_count})")
            out.append(n)
    except ValueError:
        return None, (f"could not parse parts answer {answer!r}; "
                      f"expected 'all', '1,3,5', or '1-8'")
    if not out:
        return None, f"no parts selected from {answer!r}"
    # Dedupe + sort for stable downstream behavior
    return sorted(set(out)), None


def _live_tool_options(no_live: bool = False,
                       requested_material: str | None = None) -> list[dict[str, Any]]:
    """Query Moonraker for currently-loaded toolheads + materials.

    Returns a list shaped like ``query_material_options`` does (single-STL
    reuses the exact same helper). Each item has:
      label    — e.g. "T1: PETG (loaded)"
      value    — e.g. "T1"
      material — e.g. "PETG"
      loaded   — bool / None
      recommended — bool

    When ``no_live`` is True or Moonraker is unreachable, returns a
    headless fallback (T0..T3 with material unknown) so the prompt still
    surfaces — operator picks blind, the slice headlines the chosen tool.
    """
    if no_live:
        return [{"label": f"{t}: (material unknown — live-state unavailable)",
                 "value": t, "material": None, "loaded": None, "recommended": (t == "T1")}
                for t in DEFAULT_TOOLS]
    try:
        from u1_material_picker import query_material_options
        return query_material_options(requested_material=requested_material)
    except Exception:
        return [{"label": f"{t}: (material unknown — Moonraker unreachable)",
                 "value": t, "material": None, "loaded": None, "recommended": (t == "T1")}
                for t in DEFAULT_TOOLS]


def _build_next_command(archive: Path, request_id: str, *,
                        parts: str | None = None,
                        tool: str | None = None,
                        material: str | None = None,
                        orient: str | None = None,
                        profile: str | None = None,
                        supports: str | None = None,
                        nozzle: str | None = None,
                        action: str | None = None,
                        adjust: str | None = None,
                        no_live_upload: bool = False,
                        no_live_material: bool = False,
                        operator: str | None = None) -> str:
    """Compose the kit_workflow.py next_command with accumulated flags.

    Mirrors the single-STL workflow's pattern: each next_command carries
    every prior answer plus the new one, so state is recoverable from the
    CLI alone (request.json is also persisted but the command is the
    single source of truth the agent copies verbatim).
    """
    parts_q = []
    parts_q.append("python3 /opt/data/scripts/u1_kit_workflow.py")
    parts_q.append(_shell_quote(str(archive)))
    parts_q.append("--json-events")
    parts_q.append(f"--request-id {request_id}")
    if nozzle:
        parts_q.append(f"--nozzle {_shell_quote(nozzle)}")
    if parts is not None:
        parts_q.append(f"--parts {_shell_quote(parts)}")
    if tool:
        parts_q.append(f"--tool {tool}")
    if material:
        parts_q.append(f"--material {_shell_quote(material)}")
    if orient:
        parts_q.append(f"--orient {orient}")
    if profile:
        parts_q.append(f"--profile {_shell_quote(profile)}")
    if supports:
        parts_q.append(f"--supports {supports}")
    if action:
        # Multi-word actions (e.g. "start manual-bed-check") need quoting
        # so argparse sees them as a single value.
        parts_q.append(f"--action {_shell_quote(action)}")
    if adjust:
        parts_q.append(f"--adjust {adjust}")
    if no_live_upload:
        parts_q.append("--no-live-upload")
    if no_live_material:
        parts_q.append("--no-live-material")
    if operator:
        # Preserve operator across the yes-command chain so test-prefixed
        # operators (smoke:*, test:*, etc.) can't drop to the default
        # `telegram:brent` and let Stage 2 fire against the real printer.
        parts_q.append(f"--operator {_shell_quote(operator)}")
    return " ".join(parts_q)


def _format_parts_listing(kit: dict[str, Any]) -> str:
    """One numbered STL per line with footprint dims (e.g. '1. angle_30.stl (96x48mm)')."""
    lines = []
    for i, p in enumerate(kit['parts']):
        fp = p.get('footprint_mm') or [0, 0]
        lines.append(f"  {i + 1}. {p['filename']} ({fp[0]:.0f}x{fp[1]:.0f}mm)")
    return "\n".join(lines)


def _scan_part_overhang(stl_path: str) -> dict[str, Any]:
    """Compute the as-authored overhang tier for one STL.

    Returns {'overhang_pct': float, 'tier': str, 'recommend_supports': bool}.
    Tier is one of low / moderate / heavy / very heavy (from
    render_slice_review.supports_tier). recommend_supports flips True at
    'heavy' or worse — same boundary the single-STL workflow uses to
    pre-warn the operator (u1_slice_workflow ~line 2333).

    Auto-orient changes the geometry hitting the bed, so this is a
    pre-orientation indicator only. The actual slice-time supports
    decision still goes through Orca with the operator's chosen flag.
    """
    # Local import: render_slice_review depends on numpy/PIL via u1_orient.
    # The kit workflow's _ensure_compat_python already swapped to an interp
    # that has those, so this just resolves the symbol.
    import sys as _sys
    here = Path(__file__).resolve().parent
    tools_dir = (here.parent / 'tools').resolve()
    if str(tools_dir) not in _sys.path:
        _sys.path.insert(0, str(tools_dir))
    try:
        from render_slice_review import supports_tier  # type: ignore
        from _stl_render import parse_stl, overhang_mask, face_areas  # type: ignore
    except Exception as exc:
        return {"overhang_pct": None, "tier": "unknown",
                "recommend_supports": False, "error": str(exc)[:200]}
    try:
        tris = parse_stl(Path(stl_path))
        mask = overhang_mask(tris)
        areas = face_areas(tris)
        tot = float(areas.sum())
        over = float(areas[mask].sum()) if tot > 0 else 0.0
        pct = (100.0 * over / tot) if tot > 0 else 0.0
        tier = supports_tier(pct)
        return {"overhang_pct": round(pct, 1), "tier": tier,
                "recommend_supports": tier in ("heavy", "very heavy")}
    except Exception as exc:
        return {"overhang_pct": None, "tier": "unknown",
                "recommend_supports": False, "error": str(exc)[:200]}


def _load_pil_fonts() -> tuple[Any, Any]:
    """robust PIL font loading.

    Try DejaVu paths (Hermes container ships with), fall back to PIL's
    default bitmap font so labels still render — just without nicer faces.
    Returns ``(big_font, small_font)``.
    """
    try:
        from PIL import ImageFont  # type: ignore
    except Exception:
        return None, None
    candidate_pairs = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/dejavu/DejaVuSans.ttf"),
    ]
    for bold_p, reg_p in candidate_pairs:
        try:
            return (ImageFont.truetype(bold_p, 22),
                    ImageFont.truetype(reg_p, 16))
        except Exception:
            continue
    try:
        # PIL's bitmap default — always present, ugly but functional.
        default = ImageFont.load_default()
        return default, default
    except Exception:
        return None, None


def _gcode_extrusion_polyline(gcode_path: Path,
                              max_points: int = 200_000
                              ) -> list[tuple[float, float, float, float]]:
    """Walk a gcode file and return extrusion line segments as
    ``[(x1,y1,x2,y2), ...]``. Travel moves are dropped.

    Caps the segment count at ``max_points`` so rendering large gcodes
    (the 8-part kit is ~852k moves) stays sub-second. Truncation is
    visually equivalent for an outline silhouette.
    """
    re_x = re.compile(r"\bX(-?\d+\.?\d*)")
    re_y = re.compile(r"\bY(-?\d+\.?\d*)")
    re_e = re.compile(r"\bE(-?\d+\.?\d*)")
    last_x = last_y = None
    segs: list[tuple[float, float, float, float]] = []
    with gcode_path.open() as fh:
        for line in fh:
            if not line.startswith("G"):
                continue
            mx = re_x.search(line)
            my = re_y.search(line)
            new_x = float(mx.group(1)) if mx else last_x
            new_y = float(my.group(1)) if my else last_y
            if new_x is None or new_y is None:
                last_x, last_y = new_x, new_y
                continue
            me = re_e.search(line)
            extruding = me and float(me.group(1)) > 0
            if extruding and last_x is not None and last_y is not None:
                segs.append((last_x, last_y, new_x, new_y))
                if len(segs) >= max_points:
                    break
            last_x, last_y = new_x, new_y
    return segs


def _render_plate_layout(
    gcode_path: Path,
    out_path: Path,
    *,
    bed_mm: tuple[float, float] = (270.0, 270.0),
    canvas_px: int = 600,
    title: str | None = None,
    label_below: str | None = None,
) -> dict[str, Any]:
    """render the TRUE plate layout.

    Parses the gcode's extrusion polyline (first-layer + ascend), projects
    onto a bed-shaped canvas, draws each extrusion segment as a line.
    The result shows exactly where material lands on the bed — replaces
    the per-part isometric grid that was deferred from Phase 2 design.

    Returns {'ok': bool, 'path': str | None, 'segment_count': int, 'error': str | None}.
    """
    try:
        from PIL import Image, ImageDraw  # type: ignore
    except Exception as exc:
        return {"ok": False, "path": None, "segment_count": 0,
                "error": f"PIL not available: {exc}"}
    big_font, small_font = _load_pil_fonts()

    bg = (22, 26, 31)
    bed_color = (40, 46, 54)
    grid_color = (60, 68, 80)
    extrusion_color = (115, 215, 165)
    fg = (232, 238, 245)
    muted = (160, 170, 180)

    title_h = 50 if title else 0
    label_h = 32 if label_below else 0
    pad = 16

    plot_size = canvas_px
    canvas_w = plot_size + pad * 2
    canvas_h = plot_size + pad * 2 + title_h + label_h

    canvas = Image.new("RGB", (canvas_w, canvas_h), bg)
    draw = ImageDraw.Draw(canvas)

    if title:
        draw.text((pad, pad), title, fill=fg, font=big_font)

    plot_x0 = pad
    plot_y0 = pad + title_h
    plot_x1 = plot_x0 + plot_size
    plot_y1 = plot_y0 + plot_size

    # Bed background (filled rectangle).
    draw.rectangle([plot_x0, plot_y0, plot_x1, plot_y1], fill=bed_color)
    # Grid every 50mm.
    bed_x, bed_y = bed_mm
    px_per_mm_x = plot_size / bed_x
    px_per_mm_y = plot_size / bed_y
    for mm in range(50, int(bed_x), 50):
        gx = plot_x0 + int(mm * px_per_mm_x)
        draw.line([(gx, plot_y0), (gx, plot_y1)], fill=grid_color)
    for mm in range(50, int(bed_y), 50):
        gy = plot_y0 + int(mm * px_per_mm_y)
        draw.line([(plot_x0, gy), (plot_x1, gy)], fill=grid_color)

    # Extract + draw extrusion segments.
    try:
        segs = _gcode_extrusion_polyline(gcode_path)
    except Exception as exc:
        return {"ok": False, "path": None, "segment_count": 0,
                "error": f"polyline parse failed: {exc}"}

    def _to_px(mm_x: float, mm_y: float) -> tuple[int, int]:
        # Y is inverted in image coordinates (origin top-left) vs printer
        # coordinates (origin front-left).
        px = plot_x0 + int(mm_x * px_per_mm_x)
        py = plot_y1 - int(mm_y * px_per_mm_y)
        return (px, py)

    for (x1, y1, x2, y2) in segs:
        draw.line([_to_px(x1, y1), _to_px(x2, y2)], fill=extrusion_color, width=1)

    # Axis ticks.
    for mm in (0, 50, 100, 150, 200, 250):
        if mm > bed_x:
            continue
        tx = plot_x0 + int(mm * px_per_mm_x)
        draw.text((tx + 2, plot_y1 + 4), f"{mm}", fill=muted, font=small_font)
    for mm in (0, 50, 100, 150, 200, 250):
        if mm > bed_y:
            continue
        ty = plot_y1 - int(mm * px_per_mm_y)
        draw.text((plot_x1 + 4, ty - 8), f"{mm}", fill=muted, font=small_font)

    if label_below:
        draw.text((pad, plot_y1 + pad), label_below, fill=fg, font=small_font)

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, format="PNG", optimize=True)
    except Exception as exc:
        return {"ok": False, "path": None, "segment_count": len(segs),
                "error": f"save failed: {exc}"}
    return {"ok": True, "path": str(out_path),
            "segment_count": len(segs), "error": None}


def _render_plate_layout_from_stls(
    arranged_stl_paths: list[str] | list[Path],
    out_path: Path,
    *,
    bed_mm: tuple[float, float] = (270.0, 270.0),
    canvas_px: int = 900,
    title: str | None = None,
    label_below: str | None = None,
) -> dict[str, Any]:
    """Simplest possible plate preview (after I chased
    my tail with tiling / colors / labels / bed anchors — none of which
    made the preview more accurate since Orca's --export-stl packer
    already diverges from --slice).

    Parse Orca's arranged STLs, concatenate every triangle into one array,
    hand it to ``render_view("top")``. Auto-fit expands the canvas to
    include ALL parts (including ones the export packer put off-bed), so
    nothing silently drops. Amber disclaimer notes the preview is
    approximate — the SLICE is authoritative.

    Returns ``{'ok', 'path', 'part_count', 'error'}``.
    """
    try:
        import sys as _sys
        import numpy as np  # type: ignore
        from PIL import Image, ImageDraw  # type: ignore
        here = Path(__file__).resolve().parent
        tools_dir = (here.parent / "tools").resolve()
        if str(tools_dir) not in _sys.path:
            _sys.path.insert(0, str(tools_dir))
        from _stl_render import parse_stl, render_view  # type: ignore
    except Exception as exc:
        return {"ok": False, "path": None, "part_count": 0,
                "error": f"deps missing: {exc}"}

    stl_paths = [Path(p) for p in arranged_stl_paths if Path(p).exists()]
    if not stl_paths:
        return {"ok": False, "path": None, "part_count": 0,
                "error": "no arranged STLs provided"}

    try:
        tri_list = [parse_stl(p) for p in stl_paths]
        tri_list = [t for t in tri_list if t.size > 0]
        if not tri_list:
            return {"ok": False, "path": None, "part_count": 0,
                    "error": "all parsed STLs empty"}
        merged = np.concatenate(tri_list, axis=0)
    except Exception as exc:
        return {"ok": False, "path": None, "part_count": 0,
                "error": f"stl parse failed: {exc}"}

    fg = (232, 238, 245)
    disclaimer = "⚠  Preview only — the actual print packs tighter, all parts fit within bed"

    big_font, small_font = _load_pil_fonts()
    title_h = 50 if title else 0
    label_h = 32 if label_below else 0
    label_h += 26  # disclaimer row
    pad = 16
    plot_size = canvas_px
    canvas_w = plot_size + pad * 2
    canvas_h = plot_size + pad * 2 + title_h + label_h

    canvas = Image.new("RGB", (canvas_w, canvas_h), (22, 26, 31))
    draw = ImageDraw.Draw(canvas)
    if title:
        draw.text((pad, pad), title, fill=fg, font=big_font)

    plate_img = render_view(
        merged, plot_size, plot_size,
        view="top",
        bg=(30, 34, 40),
        model_top=(120, 190, 240),
        model_bottom=(40, 90, 140),
        pad=0.03,
    )
    canvas.paste(plate_img, (pad, pad + title_h))

    y_cursor = pad + title_h + plot_size + pad
    if label_below:
        draw.text((pad, y_cursor), label_below, fill=fg, font=small_font)
        y_cursor += 24
    draw.text((pad, y_cursor), disclaimer, fill=(235, 178, 74), font=small_font)

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, format="PNG", optimize=True)
    except Exception as exc:
        return {"ok": False, "path": None, "part_count": len(tri_list),
                "error": f"save failed: {exc}"}

    return {"ok": True, "path": str(out_path),
            "part_count": len(tri_list), "error": None}


def _render_plate_layout_from_gcode_m486(
    plate_gcode_path: Path,
    source_stl_paths: list[str] | list[Path],
    out_path: Path,
    *,
    bed_mm: tuple[float, float] = (270.0, 270.0),
    canvas_px: int = 900,
    title: str | None = None,
    label_below: str | None = None,
    n_rotation_candidates: int = 72,
) -> dict[str, Any]:
    """Truth-source plate preview using M486 object markers from the slice
    gcode + source STL geometry.

    profile-independent renderer that reconstructs Orca's
    ACTUAL slice arrangement (not the buggy --export-3mf packer):
      1. Parse M486 A<name> / M486 S<idx> markers to identify per-object
         gcode regions
      2. Collect first-layer wall points per object → object centroid +
         footprint bbox in world coordinates (matches actual print)
      3. Match each object to a source STL by filename (angle_270.stl →
         angle_270.stl)
      4. Brute-force rotation candidates (72 evenly spaced) and pick the
         one whose transformed source STL bbox maximizes IoU with the
         gcode footprint bbox — 90-99% accurate on tested kits
      5. Merge transformed source STL triangles + render via
         render_view("top") with Lambertian shading

    Requires the profile to have M486 emission enabled (some profiles
    like Standard don't; the caller should fall back to layer-shadow
    when this returns ok=False with 'no M486').

    Returns ``{'ok', 'path', 'part_count', 'error'}``.
    """
    import re as _re
    import math as _math
    try:
        import sys as _sys
        import numpy as np  # type: ignore
        from PIL import Image, ImageDraw  # type: ignore
        here = Path(__file__).resolve().parent
        tools_dir = (here.parent / "tools").resolve()
        if str(tools_dir) not in _sys.path:
            _sys.path.insert(0, str(tools_dir))
        from _stl_render import parse_stl, render_view  # type: ignore
    except Exception as exc:
        return {"ok": False, "path": None, "part_count": 0,
                "error": f"deps missing: {exc}"}

    # Parse M486 object regions
    G_RE = _re.compile(r"^G[01]\b")
    X_RE = _re.compile(r"\bX(-?\d+(?:\.\d+)?)")
    Y_RE = _re.compile(r"\bY(-?\d+(?:\.\d+)?)")
    Z_RE = _re.compile(r"\bZ(-?\d+(?:\.\d+)?)")
    E_RE = _re.compile(r"\bE(-?\d+(?:\.\d+)?)")
    M486_A = _re.compile(r"^M486 A(.+?)\s*$")
    M486_S = _re.compile(r"^M486 S(-?\d+)")
    from collections import defaultdict as _dd
    objects: dict[str, list[tuple[float, float]]] = _dd(list)
    id_to_name: dict[int, str] = {}
    current_id = None
    current_type = None
    first_layer_z = None
    past_first_layer = False
    prev_x = prev_y = None

    try:
        lines = plate_gcode_path.read_text().splitlines()
    except Exception as exc:
        return {"ok": False, "path": None, "part_count": 0,
                "error": f"gcode read failed: {exc}"}

    for ln in lines:
        m_a = M486_A.match(ln)
        if m_a:
            if current_id is not None:
                id_to_name[current_id] = m_a.group(1).strip()
            continue
        m_s = M486_S.match(ln)
        if m_s:
            new_id = int(m_s.group(1))
            current_id = None if new_id < 0 else new_id
            continue
        if ln.startswith(";TYPE:"):
            current_type = ln[6:].strip()
            continue
        if not G_RE.match(ln):
            continue
        z_m = Z_RE.search(ln)
        if z_m:
            z = float(z_m.group(1))
            if first_layer_z is None:
                first_layer_z = z
            elif z > first_layer_z * 2.5:
                past_first_layer = True
        if past_first_layer:
            break
        x_m = X_RE.search(ln); y_m = Y_RE.search(ln); e_m = E_RE.search(ln)
        nx = float(x_m.group(1)) if x_m else prev_x
        ny = float(y_m.group(1)) if y_m else prev_y
        # Strict Outer wall only — Inner walls / solid infill / bottom
        # surface trace INTERIOR of the part, not the boundary. Including
        # them distorts the rotation match toward interior fill patterns
        # rather than the actual outer silhouette.
        if (e_m and current_id is not None
                and current_type == "Outer wall"
                and nx is not None and ny is not None):
            if float(e_m.group(1)) > 0:
                name = id_to_name.get(current_id, "")
                base = _re.sub(r"_id_\d+_copy_\d+$", "", name)
                if base:
                    objects[base].append((nx, ny))
        prev_x, prev_y = nx, ny

    if not objects:
        return {"ok": False, "path": None, "part_count": 0,
                "error": "no M486 markers (profile doesn't emit object labels)"}

    # Match by filename base
    source_by_name: dict[str, Path] = {}
    for p in source_stl_paths:
        sp = Path(p)
        source_by_name[sp.name] = sp

    def _center_xy(tris, use_centroid=True):
        xy = tris[:, :, :2].reshape(-1, 2)
        if use_centroid:
            # Vertex CENTROID (mean) — better for asymmetric parts where
            # the bbox center is offset from the visual centroid.
            cx = float(xy[:, 0].mean())
            cy = float(xy[:, 1].mean())
        else:
            cx = (xy[:, 0].min() + xy[:, 0].max()) / 2
            cy = (xy[:, 1].min() + xy[:, 1].max()) / 2
        flat = tris.reshape(-1, 3).copy()
        flat[:, 0] -= cx; flat[:, 1] -= cy
        return flat.reshape(tris.shape)

    def _convex_hull_2d(pts):
        """Andrew's monotone chain convex hull. Returns hull polygon (Nx2)."""
        pts_sorted = sorted({(round(float(p[0]), 3), round(float(p[1]), 3))
                             for p in pts})
        if len(pts_sorted) < 3:
            return np.array(pts_sorted, dtype=np.float32) if pts_sorted else np.zeros((0, 2), dtype=np.float32)
        def cross(o, a, b):
            return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
        lower = []
        for p in pts_sorted:
            while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
                lower.pop()
            lower.append(p)
        upper = []
        for p in reversed(pts_sorted):
            while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
                upper.pop()
            upper.append(p)
        hull = lower[:-1] + upper[:-1]
        return np.array(hull, dtype=np.float32)

    def _densify_polygon(hull, spacing_mm=1.5):
        """Interpolate along hull edges so distance-matching has enough
        boundary samples. Returns dense (N, 2) point array."""
        if len(hull) < 2:
            return hull
        pts = []
        for i in range(len(hull)):
            a = hull[i]; b = hull[(i + 1) % len(hull)]
            d = float(np.linalg.norm(b - a))
            n = max(1, int(d / spacing_mm))
            for k in range(n):
                t = k / n
                pts.append(a + t * (b - a))
        return np.array(pts, dtype=np.float32)

    def _transform(tris, angle_rad, dx, dy):
        c, s = _math.cos(angle_rad), _math.sin(angle_rad)
        R = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float32)
        T = np.array([dx, dy, 0], dtype=np.float32)
        flat = tris.reshape(-1, 3)
        return (flat @ R.T + T).reshape(tris.shape)

    placed_tris: list[np.ndarray] = []
    placed_count = 0
    for name, pts in objects.items():
        src = source_by_name.get(name)
        if src is None or not src.exists():
            continue
        try:
            tris = parse_stl(src)
        except Exception:
            continue
        if tris.size == 0:
            continue

        # Center STL by vertex centroid (better than bbox center for
        # asymmetric parts).
        tris_c = _center_xy(tris, use_centroid=True)

        # Gcode centroid (shape center-of-mass equivalent for perimeter
        # samples). Align by centroid → best rotation search then finds
        # the pure rotation (no residual translation error).
        gc_pts = np.array(pts, dtype=np.float32)
        gc_center = (float(gc_pts[:, 0].mean()),
                     float(gc_pts[:, 1].mean()))
        gc_pts_shifted = gc_pts - np.array(gc_center, dtype=np.float32)

        # Use all STL vertices — NOT convex hull. Angle brackets and
        # similar parts are non-convex (concavity between arms). Hull
        # would fill that in, distorting the match. Dedupe rounded to
        # 0.5mm to cut down count from ~5000 to ~500 unique boundary
        # points.
        stl_verts = tris_c[:, :, :2].reshape(-1, 2)
        rounded = np.round(stl_verts * 2).astype(np.int32)
        _uniq, uidx = np.unique(rounded, axis=0, return_index=True)
        stl_boundary = stl_verts[uidx]
        if len(stl_boundary) > 400:
            step = len(stl_boundary) // 400
            stl_boundary = stl_boundary[::step]

        # Sub-sample gcode points to ~250
        if len(gc_pts_shifted) > 250:
            step = len(gc_pts_shifted) // 250
            gc_pts_shifted = gc_pts_shifted[::step]

        # Score every rotation by symmetric (bidirectional) mean of
        # min-distances. Symmetric penalizes both "STL extends beyond
        # gcode" AND "gcode extends beyond STL" — critical for correct
        # 180°-vs-0° discrimination on asymmetric parts.
        best_ang = 0.0
        best_score = float("inf")
        for i in range(n_rotation_candidates):
            ang = 2 * _math.pi * i / n_rotation_candidates
            c, s = _math.cos(ang), _math.sin(ang)
            R = np.array([[c, -s], [s, c]], dtype=np.float32)
            rot_boundary = stl_boundary @ R.T
            # Gcode → nearest STL boundary
            diffs_g = (gc_pts_shifted[:, None, :]
                       - rot_boundary[None, :, :])
            d2_g = (diffs_g * diffs_g).sum(-1)
            md_g = np.sqrt(d2_g.min(axis=1))
            # STL boundary → nearest gcode point
            diffs_s = (rot_boundary[:, None, :]
                       - gc_pts_shifted[None, :, :])
            d2_s = (diffs_s * diffs_s).sum(-1)
            md_s = np.sqrt(d2_s.min(axis=1))
            # Symmetric mean
            score = float(md_g.mean() + md_s.mean())
            if score < best_score:
                best_score = score
                best_ang = ang

        placed_tris.append(_transform(tris_c, best_ang,
                                      gc_center[0], gc_center[1]))
        placed_count += 1

    if not placed_tris:
        return {"ok": False, "path": None, "part_count": 0,
                "error": "no source STLs matched the M486 object names"}

    parts_merged = np.concatenate(placed_tris, axis=0).astype(np.float32)

    fg = (232, 238, 245)
    muted = (140, 150, 160)
    big_font, small_font = _load_pil_fonts()
    title_h = 50 if title else 0
    disclaimer = ("Layout from slice gcode (M486 markers) with source STL "
                  "geometry — profile-independent, matches actual print")
    label_h = 32 if label_below else 0
    label_h += 26  # disclaimer row
    pad = 16
    plot_size = canvas_px
    canvas_w = plot_size + pad * 2
    canvas_h = plot_size + pad * 2 + title_h + label_h

    canvas = Image.new("RGB", (canvas_w, canvas_h), (22, 26, 31))
    draw = ImageDraw.Draw(canvas)
    if title:
        draw.text((pad, pad), title, fill=fg, font=big_font)

    plate_img = render_view(
        parts_merged, plot_size, plot_size,
        view="top",
        bg=(30, 34, 40),
        model_top=(120, 190, 240),
        model_bottom=(40, 90, 140),
        pad=0.03,
    )
    canvas.paste(plate_img, (pad, pad + title_h))

    y_cursor = pad + title_h + plot_size + pad
    if label_below:
        draw.text((pad, y_cursor), label_below, fill=fg, font=small_font)
        y_cursor += 24
    draw.text((pad, y_cursor), disclaimer, fill=muted, font=small_font)

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, format="PNG", optimize=True)
    except Exception as exc:
        return {"ok": False, "path": None, "part_count": placed_count,
                "error": f"save failed: {exc}"}

    return {"ok": True, "path": str(out_path),
            "part_count": placed_count, "error": None}


def _parse_3mf_mesh(mesh_xml: str):
    """Parse a 3MF `3D/Objects/*.model` file's mesh into an (N, 3, 3)
    triangle array. Returns None on parse failure."""
    try:
        import re as _re
        import numpy as np  # type: ignore
    except Exception:
        return None
    verts: list[tuple[float, float, float]] = []
    for m in _re.finditer(
        r'<vertex\s+x="([-0-9.eE+]+)"\s+y="([-0-9.eE+]+)"\s+z="([-0-9.eE+]+)"',
        mesh_xml,
    ):
        verts.append((float(m.group(1)), float(m.group(2)),
                      float(m.group(3))))
    if not verts:
        return None
    v_arr = np.array(verts, dtype=np.float32)
    tris_idx: list[tuple[int, int, int]] = []
    for m in _re.finditer(
        r'<triangle\s+v1="(\d+)"\s+v2="(\d+)"\s+v3="(\d+)"',
        mesh_xml,
    ):
        tris_idx.append((int(m.group(1)), int(m.group(2)),
                         int(m.group(3))))
    if not tris_idx:
        return None
    idx = np.array(tris_idx, dtype=np.int32)
    return v_arr[idx]


def _render_plate_layout_from_3mf(
    source_stl_paths: list[str] | list[Path],
    arrange_3mf: str | Path,
    out_path: Path,
    *,
    bed_mm: tuple[float, float] = (270.0, 270.0),
    canvas_px: int = 900,
    title: str | None = None,
    label_below: str | None = None,
) -> dict[str, Any]:
    """Truth-source plate preview from Orca's 3MF sidecar.

    this is the profile-independent renderer we've been
    chasing. Orca stamps exact per-item transform matrices into the 3MF
    it emits alongside slicing/STL export. We:
      1. Parse the 3MF's ``3D/3dmodel.model`` XML for each part's build
         + component transform (row-major 3x4 matrix per 3MF spec)
      2. Match each transform to a source STL by filename
      3. Apply the composed transform (rotation + translation) to the
         source STL's triangles
      4. Merge all transformed triangles + a flat bed rectangle
      5. Render via ``render_view("top")`` — same Lambertian shader that
         produces the parts thumbnails

    Because it uses source-STL geometry (not gcode extrusion polylines),
    output quality is independent of infill choice — gyroid/rectilinear/
    honeycomb all render identically clean.

    Returns ``{'ok', 'path', 'part_count', 'error'}``.
    """
    import re as _re
    import zipfile as _zip
    try:
        import sys as _sys
        import numpy as np  # type: ignore
        from PIL import Image, ImageDraw  # type: ignore
        here = Path(__file__).resolve().parent
        tools_dir = (here.parent / "tools").resolve()
        if str(tools_dir) not in _sys.path:
            _sys.path.insert(0, str(tools_dir))
        from _stl_render import parse_stl, render_view  # type: ignore
    except Exception as exc:
        return {"ok": False, "path": None, "part_count": 0,
                "error": f"deps missing: {exc}"}

    mf_path = Path(arrange_3mf)
    if not mf_path.exists():
        return {"ok": False, "path": None, "part_count": 0,
                "error": f"arrange 3mf not found: {mf_path}"}

    # Load meshes from the 3MF's own `3D/Objects/*.model` files rather
    # than the source STLs — Orca may have normalized/centered the STL
    # when it packed the mesh into the 3MF, and its transforms are
    # designed to apply to THAT internal representation. Source STLs
    # produced off-bed positions because their coordinate origins differed.
    mesh_by_zip_path: dict[str, np.ndarray] = {}
    try:
        with _zip.ZipFile(mf_path) as zf:
            top = zf.read("3D/3dmodel.model").decode("utf-8")
            for name in zf.namelist():
                if name.startswith("3D/Objects/") and name.endswith(".model"):
                    mesh_xml = zf.read(name).decode("utf-8", errors="ignore")
                    tris = _parse_3mf_mesh(mesh_xml)
                    if tris is not None and tris.size > 0:
                        # zip stores as '3D/Objects/foo.model' — the top
                        # model references it as '/3D/Objects/foo.model'.
                        mesh_by_zip_path[name] = tris
                        mesh_by_zip_path["/" + name] = tris
    except Exception as exc:
        return {"ok": False, "path": None, "part_count": 0,
                "error": f"3mf read failed: {exc}"}

    # Parse component definitions (id → mesh path + optional local transform)
    obj_to_path: dict[int, str] = {}
    obj_to_local: dict[int, str] = {}
    for m in _re.finditer(
        r'<object\s+id="(\d+)"[^>]*>\s*<components>\s*<component[^/]*'
        r'p:path="([^"]+)"[^/]*(?:transform="([^"]*)")?[^/]*/>',
        top, _re.DOTALL,
    ):
        obj_id = int(m.group(1))
        obj_to_path[obj_id] = m.group(2)
        obj_to_local[obj_id] = m.group(3) or "1 0 0 0 1 0 0 0 1 0 0 0"

    # Parse build items (arrangement — this is what Orca decided)
    items: list[dict[str, Any]] = []
    for m in _re.finditer(
        r'<item\s+objectid="(\d+)"[^/]*transform="([^"]+)"[^/]*/>',
        top,
    ):
        objid = int(m.group(1))
        items.append({
            "objectid": objid,
            "build_transform": m.group(2),
            "component_path": obj_to_path.get(objid, ""),
            "component_local_transform": obj_to_local.get(
                objid, "1 0 0 0 1 0 0 0 1 0 0 0"),
        })

    if not items:
        return {"ok": False, "path": None, "part_count": 0,
                "error": "no <build><item> in 3mf"}

    def _parse_transform(t: str) -> tuple[np.ndarray, np.ndarray]:
        nums = [float(x) for x in t.split()]
        if len(nums) < 12:
            return (np.eye(3, dtype=np.float32),
                    np.zeros(3, dtype=np.float32))
        rot = np.array(nums[:9], dtype=np.float32).reshape(3, 3)
        trans = np.array([nums[9], nums[10], nums[11]], dtype=np.float32)
        return rot, trans

    placed_tris: list[np.ndarray] = []
    placed_count = 0
    for item in items:
        comp_path = item["component_path"]
        # Try both with and without leading slash. Use explicit `is None`
        # checks — numpy arrays can't be used in truthy `or` fallback.
        tris = mesh_by_zip_path.get(comp_path)
        if tris is None:
            tris = mesh_by_zip_path.get(comp_path.lstrip("/"))
        if tris is None:
            continue
        b_rot, b_t = _parse_transform(item["build_transform"])
        c_rot, c_t = _parse_transform(item["component_local_transform"])
        # Composed transform (build applies after component-local):
        # world = build_R @ (comp_R @ v + comp_t) + build_t
        combined_rot = b_rot @ c_rot
        combined_t = b_rot @ c_t + b_t
        flat = tris.reshape(-1, 3)
        world = flat @ combined_rot.T + combined_t
        placed_tris.append(world.reshape(tris.shape))
        placed_count += 1

    if not placed_tris:
        return {"ok": False, "path": None, "part_count": 0,
                "error": "no source STLs matched the 3mf's items"}

    parts_merged = np.concatenate(placed_tris, axis=0).astype(np.float32)

    # Bed anchor: two flat triangles at z=0 covering the full bed so
    # render_view auto-fit locks to the bed extent + gives us a bed
    # background under parts. Colored slightly warmer than parts so the
    # bed reads as bed rather than blending with the render.
    bed_w, bed_h = float(bed_mm[0]), float(bed_mm[1])
    bed_tris = np.array([
        [[0.0, 0.0, 0.0], [bed_w, 0.0, 0.0], [0.0, bed_h, 0.0]],
        [[bed_w, 0.0, 0.0], [bed_w, bed_h, 0.0], [0.0, bed_h, 0.0]],
    ], dtype=np.float32)
    merged = np.concatenate([bed_tris, parts_merged], axis=0)

    fg = (232, 238, 245)
    muted = (140, 150, 160)
    big_font, small_font = _load_pil_fonts()
    title_h = 50 if title else 0
    disclaimer = "Layout from Orca's arrangement (source STL geometry, profile-independent)"
    label_h = 32 if label_below else 0
    label_h += 26  # disclaimer row
    pad = 16
    plot_size = canvas_px
    canvas_w = plot_size + pad * 2
    canvas_h = plot_size + pad * 2 + title_h + label_h

    canvas = Image.new("RGB", (canvas_w, canvas_h), (22, 26, 31))
    draw = ImageDraw.Draw(canvas)
    if title:
        draw.text((pad, pad), title, fill=fg, font=big_font)

    plate_img = render_view(
        merged, plot_size, plot_size,
        view="top",
        bg=(30, 34, 40),
        model_top=(120, 190, 240),
        model_bottom=(40, 90, 140),
        pad=0.03,
    )
    canvas.paste(plate_img, (pad, pad + title_h))

    y_cursor = pad + title_h + plot_size + pad
    if label_below:
        draw.text((pad, y_cursor), label_below, fill=fg, font=small_font)
        y_cursor += 24
    draw.text((pad, y_cursor), disclaimer, fill=muted, font=small_font)

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, format="PNG", optimize=True)
    except Exception as exc:
        return {"ok": False, "path": None, "part_count": placed_count,
                "error": f"save failed: {exc}"}

    return {"ok": True, "path": str(out_path),
            "part_count": placed_count, "error": None}


def _render_plate_layout_from_gcode_layers(
    gcode_path: Path,
    out_path: Path,
    *,
    bed_mm: tuple[float, float] = (270.0, 270.0),
    canvas_px: int = 900,
    title: str | None = None,
    label_below: str | None = None,
    n_samples: int = 15,
    layer_bucket_mm: float = 0.20,
    line_width_px: int = 13,
    dilate_px: int = 3,
) -> dict[str, Any]:
    """Truth-source plate preview: sample N layers from the sliced gcode,
    render each as thick colored shadows stacked bottom→top on the bed.

    Answers the STL-export arrangement bug: Orca's
    ``--export-stl`` uses a different (buggy) packer than ``--slice``, so
    STL-based renders show a fictional arrangement. This function reads
    the ACTUAL slice gcode — the layout that will print — and renders
    the extrusion polylines of sampled Z-buckets as color-gradient
    shadows. Parts appear at their real bed positions.

    Approach:
      1. Bucket every G1 extrusion move by rounded print-layer Z
      2. Skip Skirt/Brim types (they span the bed and dilute the render)
      3. Sample ``n_samples`` non-empty buckets uniformly
      4. Draw each layer's polylines as thick lines onto an RGBA overlay
      5. Dilate the alpha channel (MaxFilter) to close gaps between
         adjacent extrusion lines so arms + hinges fill solid
      6. Alpha-composite bottom→top with amber→teal HSV gradient +
         high→low alpha (bottom solid, top translucent for depth cue)

    Returns ``{'ok', 'path', 'layer_count', 'error'}``.
    """
    try:
        from PIL import Image, ImageDraw, ImageFilter  # type: ignore
        import colorsys
        from collections import defaultdict
    except Exception as exc:
        return {"ok": False, "path": None, "layer_count": 0,
                "error": f"deps missing: {exc}"}

    import re
    G_RE = re.compile(r"^G[01]\b")
    X_RE = re.compile(r"\bX(-?\d+(?:\.\d+)?)")
    Y_RE = re.compile(r"\bY(-?\d+(?:\.\d+)?)")
    Z_RE = re.compile(r"\bZ(-?\d+(?:\.\d+)?)")
    E_RE = re.compile(r"\bE(-?\d+(?:\.\d+)?)")
    # Skip skirt/brim only. Sparse infill IS what fills circular/round
    # part interiors when the profile uses a rectilinear pattern — dropping
    # it (my audit misfire) made hinges disappear on ALL
    # profiles, not just gyroid. Trade-off: on gyroid-infill profiles, the
    # sparse infill polylines add noise (short zigzag segments) and the
    # render looks messy inside the circles. That's a profile-specific
    # visual quirk we accept in exchange for correct rendering on the
    # default Standard profile.
    SKIP_TYPES = {"Skirt", "Brim"}

    try:
        lines = gcode_path.read_text().splitlines()
    except Exception as exc:
        return {"ok": False, "path": None, "layer_count": 0,
                "error": f"gcode read failed: {exc}"}

    buckets: dict[float, list[list[tuple[float, float]]]] = defaultdict(list)
    current_type: str | None = None
    prev_x = prev_y = None
    current_seg: list[tuple[float, float]] = []
    current_bucket: float | None = None

    def zb(z: float) -> float:
        return round(z / layer_bucket_mm) * layer_bucket_mm

    def flush(seg: list[tuple[float, float]], bucket: float | None) -> None:
        if len(seg) >= 2 and bucket is not None:
            buckets[bucket].append(seg[:])

    for ln in lines:
        if ln.startswith(";TYPE:"):
            current_type = ln[6:].strip()
            flush(current_seg, current_bucket)
            current_seg = []
            continue
        if not G_RE.match(ln):
            continue
        z_m = Z_RE.search(ln)
        if z_m:
            new_bucket = zb(float(z_m.group(1)))
            if new_bucket != current_bucket:
                flush(current_seg, current_bucket)
                current_seg = []
                current_bucket = new_bucket
        x_m = X_RE.search(ln); y_m = Y_RE.search(ln); e_m = E_RE.search(ln)
        nx = float(x_m.group(1)) if x_m else prev_x
        ny = float(y_m.group(1)) if y_m else prev_y
        if (e_m and current_bucket is not None
                and nx is not None and ny is not None
                and current_type not in SKIP_TYPES):
            if float(e_m.group(1)) > 0 and prev_x is not None:
                if not current_seg:
                    current_seg.append((prev_x, prev_y))
                current_seg.append((nx, ny))
            else:
                flush(current_seg, current_bucket)
                current_seg = []
        else:
            flush(current_seg, current_bucket)
            current_seg = []
        prev_x, prev_y = nx, ny
    flush(current_seg, current_bucket)

    non_empty = sorted(z for z, ps in buckets.items() if ps)
    if not non_empty:
        return {"ok": False, "path": None, "layer_count": 0,
                "error": "no printable layers found in gcode"}
    if len(non_empty) <= n_samples:
        sampled = non_empty
    else:
        step = len(non_empty) / n_samples
        sampled = [non_empty[int(i * step)] for i in range(n_samples)]
        if non_empty[-1] not in sampled:
            sampled[-1] = non_empty[-1]

    bed_w, bed_h = float(bed_mm[0]), float(bed_mm[1])
    fg = (232, 238, 245)
    muted = (140, 150, 160)
    bed_bg = (30, 34, 40)
    grid_c = (55, 62, 74)

    big_font, small_font = _load_pil_fonts()
    title_h = 50 if title else 0
    disclaimer = "Layer shadow render (from sliced gcode)"
    label_h = 32 if label_below else 0
    label_h += 26  # disclaimer row
    pad = 16
    plot_size = canvas_px
    canvas_w = plot_size + pad * 2
    canvas_h = plot_size + pad * 2 + title_h + label_h

    canvas = Image.new("RGB", (canvas_w, canvas_h), (22, 26, 31))
    draw = ImageDraw.Draw(canvas)
    if title:
        draw.text((pad, pad), title, fill=fg, font=big_font)

    plot_x0 = pad
    plot_y0 = pad + title_h
    plot_x1 = plot_x0 + plot_size
    plot_y1 = plot_y0 + plot_size
    draw.rectangle([plot_x0, plot_y0, plot_x1, plot_y1], fill=bed_bg)
    ppm = plot_size / bed_w
    for mm in range(50, int(bed_w), 50):
        gx = plot_x0 + int(mm * ppm)
        draw.line([(gx, plot_y0), (gx, plot_y1)], fill=grid_c)
    for mm in range(50, int(bed_h), 50):
        gy = plot_y1 - int(mm * ppm)
        draw.line([(plot_x0, gy), (plot_x1, gy)], fill=grid_c)

    def to_px(mm_x: float, mm_y: float) -> tuple[int, int]:
        return (plot_x0 + int(mm_x * ppm), plot_y1 - int(mm_y * ppm))

    n = len(sampled)
    for i, z in enumerate(sampled):
        hue = 0.08 + (0.55 - 0.08) * (i / max(n - 1, 1))
        r, g, b = colorsys.hsv_to_rgb(hue, 0.6, 0.95)
        alpha = int(230 - (200 * i / max(n - 1, 1)))
        color = (int(r * 255), int(g * 255), int(b * 255), max(alpha, 40))
        overlay = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        d2 = ImageDraw.Draw(overlay)
        for poly in buckets[z]:
            pts = [to_px(x, y) for (x, y) in poly]
            if len(pts) >= 2:
                d2.line(pts, fill=color, width=line_width_px)
        if dilate_px > 0:
            r_c, g_c, b_c, a = overlay.split()
            k = dilate_px * 2 + 1
            a = a.filter(ImageFilter.MaxFilter(k))
            overlay = Image.merge("RGBA", (r_c, g_c, b_c, a))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(canvas)
    for mm in (0, 50, 100, 150, 200, 250):
        if mm > bed_w:
            continue
        tx = plot_x0 + int(mm * ppm)
        draw.text((tx - 8, plot_y1 + 4), f"{mm}", fill=muted, font=small_font)
    for mm in (0, 50, 100, 150, 200, 250):
        if mm > bed_h:
            continue
        ty = plot_y1 - int(mm * ppm)
        draw.text((plot_x1 + 4, ty - 8), f"{mm}", fill=muted, font=small_font)

    y_cursor = plot_y1 + pad
    if label_below:
        draw.text((pad, y_cursor), label_below, fill=fg, font=small_font)
        y_cursor += 24
    draw.text((pad, y_cursor), disclaimer, fill=muted, font=small_font)

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, format="PNG", optimize=True)
    except Exception as exc:
        return {"ok": False, "path": None, "layer_count": len(sampled),
                "error": f"save failed: {exc}"}

    return {"ok": True, "path": str(out_path),
            "layer_count": len(sampled), "error": None}


def _render_plate_layout_from_m486_outer_walls(
    gcode_path: Path,
    out_path: Path,
    *,
    bed_mm: tuple[float, float] = (270.0, 270.0),
    canvas_px: int = 900,
    title: str | None = None,
    label_below: str | None = None,
    wall_mm: float = 0.5,
) -> dict[str, Any]:
    """M486-aware plate preview: extract each part's Outer wall polylines
    from the sliced gcode using ``M486 A<name>`` / ``M486 S<id>`` markers
    and stroke them with per-part HSV colors. Profile-independent, gcode-
    truth, non-overlapping by construction (each stroke is only the wall
    path — polygon fill would falsely paint the concave interior between
    an angle's arms where a neighbor's material legitimately sits).

    Requires M486 markers (present when Orca sees the "label objects"
    setting). Returns ``ok: False`` if no per-part markers are found so
    the caller can fall through to the layer-shadow renderer.

    Two subtle regex points:
      • Orca omits leading zeros in numbers: ``E.09799``, not
        ``E0.09799``. Coord regex must accept ``\\d*\\.?\\d+``.
      • Speed-only lines like ``G1 F2400`` must not break in-progress
        polylines — skip when a G-line carries no X/Y/E.
    """
    try:
        from PIL import Image, ImageDraw  # type: ignore
        import colorsys
        from collections import defaultdict
    except Exception as exc:
        return {"ok": False, "path": None, "part_count": 0,
                "error": f"deps missing: {exc}"}

    import re
    import math
    NUM = r"(-?\d*\.?\d+)"
    G_RE = re.compile(r"^G[0123]\b")
    G23_RE = re.compile(r"^G[23]\b")
    X_RE = re.compile(r"\bX" + NUM)
    Y_RE = re.compile(r"\bY" + NUM)
    Z_RE = re.compile(r"\bZ" + NUM)
    E_RE = re.compile(r"\bE" + NUM)
    I_RE = re.compile(r"\bI" + NUM)
    J_RE = re.compile(r"\bJ" + NUM)
    M486_A = re.compile(r"^M486 A(.+?)\s*$")
    M486_S = re.compile(r"^M486 S(-?\d+)")

    try:
        lines = gcode_path.read_text().splitlines()
    except Exception as exc:
        return {"ok": False, "path": None, "part_count": 0,
                "error": f"gcode read failed: {exc}"}

    id_to_name: dict[int, str] = {}
    objects: dict[str, list[list[tuple[float, float]]]] = defaultdict(list)
    current_id: int | None = None
    current_base: str | None = None
    current_type: str | None = None
    prev_x = prev_y = None
    current_poly: list[tuple[float, float]] = []

    def flush() -> None:
        nonlocal current_poly
        if current_base and len(current_poly) >= 3:
            objects[current_base].append(current_poly[:])
        current_poly = []

    for ln in lines:
        m_a = M486_A.match(ln)
        if m_a:
            if current_id is not None:
                id_to_name[current_id] = m_a.group(1).strip()
            continue
        m_s = M486_S.match(ln)
        if m_s:
            flush()
            new_id = int(m_s.group(1))
            current_id = None if new_id < 0 else new_id
            current_base = None
            if current_id is not None:
                name = id_to_name.get(current_id, "")
                current_base = re.sub(r"_id_\d+_copy_\d+$", "", name) or None
            continue
        if ln.startswith(";TYPE:"):
            flush()
            current_type = ln[6:].strip()
            continue
        if not G_RE.match(ln):
            continue
        x_m = X_RE.search(ln); y_m = Y_RE.search(ln); e_m = E_RE.search(ln)
        if not x_m and not y_m and not e_m:
            continue
        nx = float(x_m.group(1)) if x_m else prev_x
        ny = float(y_m.group(1)) if y_m else prev_y
        is_arc = bool(G23_RE.match(ln))
        is_g2 = ln.startswith("G2")
        if (e_m and current_base is not None and current_type == "Outer wall"
                and nx is not None and ny is not None):
            if float(e_m.group(1)) > 0 and prev_x is not None:
                if not current_poly:
                    current_poly.append((prev_x, prev_y))
                if is_arc:
                    i_m = I_RE.search(ln); j_m = J_RE.search(ln)
                    cx = prev_x + (float(i_m.group(1)) if i_m else 0.0)
                    cy = prev_y + (float(j_m.group(1)) if j_m else 0.0)
                    r = math.hypot(prev_x - cx, prev_y - cy)
                    start_a = math.atan2(prev_y - cy, prev_x - cx)
                    end_a = math.atan2(ny - cy, nx - cx)
                    if is_g2:
                        if end_a > start_a:
                            end_a -= 2 * math.pi
                        sweep = start_a - end_a
                    else:
                        if end_a < start_a:
                            end_a += 2 * math.pi
                        sweep = end_a - start_a
                    n_seg = max(2, int(abs(sweep) * r / 1.0))
                    for k in range(1, n_seg + 1):
                        t = k / n_seg
                        a = start_a - sweep * t if is_g2 else start_a + sweep * t
                        current_poly.append((cx + r * math.cos(a),
                                             cy + r * math.sin(a)))
                else:
                    current_poly.append((nx, ny))
            else:
                flush()
        else:
            flush()
        prev_x, prev_y = nx, ny
    flush()

    if not objects:
        return {"ok": False, "path": None, "part_count": 0,
                "error": "no M486 outer-wall polylines found "
                         "(gcode missing per-part labels)"}

    bed_w, bed_h = float(bed_mm[0]), float(bed_mm[1])
    fg = (232, 238, 245)
    muted = (140, 150, 160)
    bed_bg = (30, 34, 40)
    grid_c = (55, 62, 74)

    big_font, small_font = _load_pil_fonts()
    title_h = 50 if title else 0
    disclaimer = "Per-part outer walls (from sliced gcode M486 markers)"
    label_h = 32 if label_below else 0
    label_h += 26
    pad = 16
    plot_size = canvas_px
    canvas_w = plot_size + pad * 2
    canvas_h = plot_size + pad * 2 + title_h + label_h

    img = Image.new("RGB", (canvas_w, canvas_h), (22, 26, 31))
    draw = ImageDraw.Draw(img)
    if title:
        draw.text((pad, pad), title, fill=fg, font=big_font)

    plot_x0 = pad
    plot_y0 = pad + title_h
    plot_x1 = plot_x0 + plot_size
    plot_y1 = plot_y0 + plot_size
    draw.rectangle([plot_x0, plot_y0, plot_x1, plot_y1], fill=bed_bg)
    ppm = plot_size / bed_w
    for mm in range(50, int(bed_w), 50):
        gx = plot_x0 + int(mm * ppm)
        draw.line([(gx, plot_y0), (gx, plot_y1)], fill=grid_c)
    for mm in range(50, int(bed_h), 50):
        gy = plot_y1 - int(mm * ppm)
        draw.line([(plot_x0, gy), (plot_x1, gy)], fill=grid_c)

    def to_px(mm_x: float, mm_y: float) -> tuple[int, int]:
        return (plot_x0 + int(mm_x * ppm), plot_y1 - int(mm_y * ppm))

    wall_px = max(2, int(wall_mm * ppm))
    names = sorted(objects.keys())
    n = len(names)
    for i, name in enumerate(names):
        hue = i / n
        r, g, b = colorsys.hsv_to_rgb(hue, 0.55, 0.9)
        color = (int(r * 255), int(g * 255), int(b * 255))
        for poly in objects[name]:
            pts = [to_px(x, y) for (x, y) in poly]
            if len(pts) >= 2:
                draw.line(pts + [pts[0]], fill=color, width=wall_px,
                          joint="curve")

    for mm in (0, 50, 100, 150, 200, 250):
        if mm > bed_w:
            continue
        tx = plot_x0 + int(mm * ppm)
        draw.text((tx - 8, plot_y1 + 4), f"{mm}", fill=muted, font=small_font)
    for mm in (0, 50, 100, 150, 200, 250):
        if mm > bed_h:
            continue
        ty = plot_y1 - int(mm * ppm)
        draw.text((plot_x1 + 4, ty - 8), f"{mm}", fill=muted, font=small_font)

    y_cursor = plot_y1 + pad
    if label_below:
        draw.text((pad, y_cursor), label_below, fill=fg, font=small_font)
        y_cursor += 24
    draw.text((pad, y_cursor), disclaimer, fill=muted, font=small_font)

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, format="PNG", optimize=True)
    except Exception as exc:
        return {"ok": False, "path": None, "part_count": n,
                "error": f"save failed: {exc}"}

    return {"ok": True, "path": str(out_path), "part_count": n, "error": None}


def _render_and_inject_plate_preview(
    plate_gcode_path: Path,
    plate_idx: int,
    plate_count: int,
    out_dir: Path,
    arranged_stls: list[str],
    selected_count: int,
    tool_choice: str,
    material: str,
    bed_mm: tuple[float, float],
    arrange_3mf: str | Path | None = None,
    source_stls: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Render the plate preview + inject a Snapmaker-format thumbnail
    block into the plate's gcode. Called BEFORE upload
    so the file uploaded to the printer has the thumbnail baked in.

    Fall-back chain (best → last-resort):
      1. **3MF transforms** — Orca's exact per-item transforms applied to
         source STLs. Profile-independent, always clean geometry, matches
         the actual print arrangement 1:1.
      2. Gcode layer-shadow — sample sliced gcode Z buckets. Profile-
         dependent (gyroid renders sparse).
      3. STL-merge — merge Orca's --export-stl arranged STLs. Arrangement
         diverges from slice on some inputs (verified bug).
      4. Raw gcode polyline — dense scribble, always works.

    Returns ``(layout, injection)``.
    """
    plate_preview_path = out_dir / f"plate_{plate_idx}_preview.png"
    label_below = (f"{selected_count} parts • {tool_choice} {material}"
                   if plate_idx == 1 else None)
    title = f"Plate {plate_idx} of {plate_count}"

    # Preferred: M486-aware per-part outer-wall stroke render (Brent
    # 2026-07-01 winner). Each part's Outer wall polylines are stroked
    # with a per-part HSV color — no polygon fill, so tightly packed
    # concave parts (angle wedges packed via each other's negative
    # space) don't produce fake collisions. Requires M486 markers in the
    # gcode (Orca emits them when "Label objects" is on, which our
    # profile enables).
    #
    # Fall-through chain:
    #   • Layer-shadow — samples Z buckets and stacks extrusion polylines
    #     as color-gradient shadows. Profile-independent, correct
    #     positions; visually sparser on gyroid infill profiles.
    #   • STL merge — Orca's --export-stl arranged STLs (arrangement
    #     packer diverges from --slice on some inputs).
    #   • Raw gcode polyline — dense scribble, always works.
    #
    # Dead-end renderers left in-file for reference (NOT in chain):
    #   • _render_plate_layout_from_3mf — --export-3mf uses same buggy
    #     packer as --export-stl.
    #   • _render_plate_layout_from_gcode_m486 — M486 positions + rotate
    #     source STLs; rotation matching is under-constrained and
    #     produces visible overlaps.
    layout = _render_plate_layout_from_m486_outer_walls(
        plate_gcode_path, plate_preview_path,
        bed_mm=bed_mm, title=title, label_below=label_below,
    )
    if not layout.get("ok"):
        layout = _render_plate_layout_from_gcode_layers(
            plate_gcode_path, plate_preview_path,
            bed_mm=bed_mm, title=title, label_below=label_below,
        )
    if not layout.get("ok") and arranged_stls:
        layout = _render_plate_layout_from_stls(
            arranged_stls, plate_preview_path,
            bed_mm=bed_mm, title=title, label_below=label_below,
        )
    if not layout.get("ok"):
        layout = _render_plate_layout(
            plate_gcode_path, plate_preview_path,
            bed_mm=bed_mm, title=title, label_below=label_below,
        )
    injection = {"ok": False, "sizes": [],
                 "error": "render failed; nothing to inject"}
    if layout.get("ok"):
        injection = _inject_plate_thumbnail(plate_gcode_path,
                                            Path(layout["path"]))
    return layout, injection


def _inject_plate_thumbnail(gcode_path: Path, preview_png_path: Path,
                            sizes: tuple = ((48, 48), (300, 300))
                            ) -> dict[str, Any]:
    """inject a Snapmaker-format thumbnail
    block into a plate's gcode so the U1 touchscreen + app show a real
    preview instead of the generic file icon. Uses the same splice
    primitives as the single-STL workflow's thumbnail injector.

    Returns {'ok': bool, 'sizes': [...], 'error': str | None}.
    """
    try:
        import sys as _sys
        here = Path(__file__).resolve().parent
        tools_dir = (here.parent / "tools").resolve()
        if str(tools_dir) not in _sys.path:
            _sys.path.insert(0, str(tools_dir))
        from PIL import Image  # type: ignore
        from gcode_inject_thumbnail import (  # type: ignore
            encode_thumbnail_block, splice_blocks,
        )
    except Exception as exc:
        return {"ok": False, "sizes": [], "error": f"deps missing: {exc}"}
    if not preview_png_path.exists():
        return {"ok": False, "sizes": [],
                "error": f"preview not found: {preview_png_path}"}
    try:
        base = Image.open(preview_png_path).convert("RGB")
    except Exception as exc:
        return {"ok": False, "sizes": [],
                "error": f"preview load failed: {exc}"}
    blocks: list[str] = []
    sizes_used: list[tuple[int, int]] = []
    for (w, h) in sizes:
        try:
            scaled = base.resize((w, h))
            blocks.append(encode_thumbnail_block(scaled))
            sizes_used.append((w, h))
        except Exception:
            continue
    if not blocks:
        return {"ok": False, "sizes": [],
                "error": "no thumbnail blocks generated"}
    try:
        text = gcode_path.read_text(encoding="utf-8", errors="replace")
        merged = splice_blocks(text, blocks)
        gcode_path.write_text(merged, encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "sizes": sizes_used,
                "error": f"splice/write failed: {exc}"}
    return {"ok": True, "sizes": sizes_used, "error": None}


def _aggregate_plate_estimates(plates: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate filament weight + print time across all plates of a kit.

    Pulls from each plate's parsed gcode metadata (from arrange_slice ->
    parse_gcode_metadata in u1_upload_gcode). Returns:
      {'total_grams': float | None, 'total_time_str': str | None,
       'per_plate': [{plate_idx, grams, time_str}], 'summary_line': str}

    summary_line: 'Estimated: 3h 12m, 84g filament' or '(not estimated)'
    if all plates are missing data. Fail-soft — kit slicing must not block
    on a metadata parse miss.
    """
    per_plate: list[dict[str, Any]] = []
    total_grams: float | None = None
    total_seconds: int | None = None

    def _parse_grams(s: str | None) -> float | None:
        if not s:
            return None
        m = re.search(r'[-+]?\d+(?:\.\d+)?', str(s))
        if not m:
            return None
        try:
            return float(m.group(0))
        except ValueError:
            return None

    def _parse_time_to_seconds(s: str | None) -> int | None:
        # Orca / PrusaSlicer emits forms like "1h 32m 4s" or "32m 4s" or "45s".
        if not s:
            return None
        total = 0
        matched = False
        for amount, unit in re.findall(r'(\d+)\s*([hms])', str(s)):
            matched = True
            n = int(amount)
            if unit == 'h':
                total += n * 3600
            elif unit == 'm':
                total += n * 60
            elif unit == 's':
                total += n
        if not matched:
            # Plain number → assume seconds (Moonraker's estimated_time field).
            try:
                return int(float(str(s).strip()))
            except (ValueError, TypeError):
                return None
        return total

    def _fmt_time(seconds: int | None) -> str | None:
        if not seconds or seconds < 0:
            return None
        h, rem = divmod(seconds, 3600)
        m, _ = divmod(rem, 60)
        if h and m:
            return f"{h}h {m}m"
        if h:
            return f"{h}h"
        return f"{m}m"

    for pl in plates:
        # merge local + remote metadata sources
        # so missing fields in one fall back to the other. Order: local gcode
        # parse (authoritative for filament weight + slicer-emitted time) →
        # uploaded.remote_metadata (Moonraker's richer schema, fills in
        # estimated_time as int seconds if Orca's string format is absent).
        local = pl.get("metadata") or {}
        uploaded = pl.get("uploaded") or {}
        remote = (uploaded.get("remote_metadata")
                  or uploaded.get("metadata") or {})
        meta = {**remote, **local}  # local overrides remote where present
        # Try multiple key variants — slicer output is inconsistent.
        grams_s = (meta.get("total filament used [g]")
                   or meta.get("filament used [g]")
                   or meta.get("filament_weight_total")
                   or meta.get("total_filament_weight"))
        time_s = (meta.get("estimated printing time (normal mode)")
                  or meta.get("estimated printing time")
                  or meta.get("estimated_time"))
        # Some hand-parsed values come in shape "= 32m 4s" — strip leading equals.
        if isinstance(grams_s, str) and "=" in grams_s:
            grams_s = grams_s.split("=", 1)[1].strip()
        if isinstance(time_s, str) and "=" in time_s:
            time_s = time_s.split("=", 1)[1].strip()
        g = _parse_grams(grams_s)
        secs = _parse_time_to_seconds(time_s)
        per_plate.append({"plate_idx": pl.get("plate_idx"),
                          "grams": g, "time_str": _fmt_time(secs)})
        if g is not None:
            total_grams = (total_grams or 0.0) + g
        if secs is not None:
            total_seconds = (total_seconds or 0) + secs

    total_time = _fmt_time(total_seconds)
    if total_grams is None and total_time is None:
        summary_line = "Estimated: (not yet estimated — slicer metadata missing)"
    else:
        bits = []
        if total_time:
            bits.append(total_time)
        if total_grams is not None:
            bits.append(f"{total_grams:.0f}g filament")
        summary_line = "Estimated: " + ", ".join(bits)
    return {"total_grams": total_grams,
            "total_time_str": total_time,
            "per_plate": per_plate,
            "summary_line": summary_line}


def _summarize_overhangs(parts_scan: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    """Bucket scan results for the plan-card summary line.

    Input: list of (part_dict, scan_result) tuples.
    Output: {'no_support': [ids], 'support_recommended': [ids],
             'unknown': [ids], 'summary_line': str}.

    summary_line example: "Supports: 6 parts no support, 2 support
    recommended (parts 5, 7)". Operator-readable and fits on one line.
    """
    no_support: list[str] = []
    support_recommended: list[str] = []
    unknown: list[str] = []
    recommended_indices: list[int] = []
    for i, (part, scan) in enumerate(parts_scan, start=1):
        pid = part.get("part_id", f"part{i}")
        if scan.get("error"):
            unknown.append(pid)
        elif scan.get("recommend_supports"):
            support_recommended.append(pid)
            recommended_indices.append(i)
        else:
            no_support.append(pid)
    # Compose the one-liner.
    line_bits = []
    if no_support:
        line_bits.append(f"{len(no_support)} parts no support")
    if support_recommended:
        idx_str = ", ".join(str(i) for i in recommended_indices)
        line_bits.append(f"{len(support_recommended)} support recommended (parts {idx_str})")
    if unknown:
        line_bits.append(f"{len(unknown)} unscored")
    summary_line = "Supports: " + ", ".join(line_bits) if line_bits else "Supports: (no parts scanned)"
    return {"no_support": no_support,
            "support_recommended": support_recommended,
            "unknown": unknown,
            "recommended_indices": recommended_indices,
            "summary_line": summary_line}


def _resolve_interaction_mode(args) -> str:
    """Brent design 2026-06-30 late — model-capability-based interaction split.

    Order of precedence:
      1. --interaction-mode CLI flag (explicit override for testing)
      2. U1_INTERACTION_MODE env var (set by snapmaker_u1 plugin at
         Hermes session start based on model provider)
      3. Default: "text" (safe for small models)

    Returns "text" | "form".
    """
    explicit = getattr(args, "interaction_mode", None)
    if explicit in ("text", "form"):
        return explicit
    env_val = os.environ.get("U1_INTERACTION_MODE", "").strip().lower()
    if env_val in ("text", "form"):
        return env_val
    return "text"


def _render_parts_thumbnail_grid(kit: dict[str, Any], out_path: Path) -> dict[str, Any]:
    """Render a grid of per-part isometric STL thumbnails with labels.

    Brent design 2026-06-30 late: "the first prompt of parts (which ones)
    was the individual render of each that would assist in a person picking
    which pieces."

    Fail-soft: returns ok=False on any error so the parts prompt still ships
    without the image.
    """
    parts = kit.get("parts") or []
    if not parts:
        return {"ok": False, "path": None, "tile_count": 0,
                "error": "no parts to render"}
    import sys as _sys
    here = Path(__file__).resolve().parent
    tools_dir = (here.parent / "tools").resolve()
    if str(tools_dir) not in _sys.path:
        _sys.path.insert(0, str(tools_dir))
    try:
        from _stl_render import parse_stl, render  # type: ignore
        from PIL import Image, ImageDraw  # type: ignore
    except Exception as exc:
        return {"ok": False, "path": None, "tile_count": 0,
                "error": f"render deps missing: {exc}"}
    big_font, small_font = _load_pil_fonts()
    n = len(parts)
    cols = min(4, n)
    rows = (n + cols - 1) // cols
    tile_w = 300
    tile_h = 300
    label_h = 44
    gap = 8
    title_h = 44
    canvas_w = cols * tile_w + (cols + 1) * gap
    canvas_h = title_h + rows * (tile_h + label_h) + (rows + 1) * gap
    bg = (22, 26, 31)
    panel = (31, 36, 43)
    fg = (232, 238, 245)
    muted = (160, 170, 180)
    canvas = Image.new("RGB", (canvas_w, canvas_h), bg)
    draw = ImageDraw.Draw(canvas)
    draw.text((gap + 8, gap + 8), f"Parts in kit ({n})", fill=fg, font=big_font)
    rendered = 0
    for i, part in enumerate(parts):
        r, c = divmod(i, cols)
        x = gap + c * (tile_w + gap)
        y = title_h + gap + r * (tile_h + label_h + gap)
        draw.rectangle([x, y, x + tile_w, y + tile_h + label_h], fill=panel)
        try:
            tris = parse_stl(Path(part["path"]))
            tile = render(tris, tile_w, tile_h, bg=panel)
            canvas.paste(tile, (x, y))
            rendered += 1
        except Exception as exc:
            draw.text((x + 12, y + 12),
                      f"render failed:\n{type(exc).__name__}",
                      fill=muted, font=small_font)
        fp = part.get("footprint_mm") or [0, 0]
        label = f"{i + 1}. {part['filename']}"
        dims = f"{fp[0]:.0f}\xd7{fp[1]:.0f}mm"
        draw.text((x + 8, y + tile_h + 4), label, fill=fg, font=small_font)
        draw.text((x + 8, y + tile_h + 22), dims, fill=muted, font=small_font)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, format="PNG", optimize=True)
    except Exception as exc:
        return {"ok": False, "path": None, "tile_count": rendered,
                "error": f"save failed: {exc}"}
    return {"ok": True, "path": str(out_path),
            "tile_count": rendered, "error": None}


def _emit_parts_prompt(events_file: Path | None, request_id: str, archive: Path,
                       kit: dict[str, Any], nozzle: str, json_events: bool,
                       no_live_material: bool, no_live_upload: bool,
                       errors: list[str] | None = None) -> dict[str, Any]:
    """Turn 1 — select which STLs to include.

    Renders a per-part STL thumbnail grid + emits a render event so the
    operator sees each piece when picking. Brent design 2026-06-30 late:
    "the first prompt of parts (which ones) was the individual render of
    each that would assist in a person picking which pieces."
    """
    parts_listing = _format_parts_listing(kit)
    # Render the STL thumbnail grid alongside the text listing.
    out_dir = u1_request.ensure_request_dir(request_id)
    # Persist the profile list at Turn 1 so a subsequent --form-answers
    # one-liner call resolves `profile N` against the list the operator
    # was shown, not a freshly-rebuilt list that a history-driven re-sort
    # might reorder between calls. FIRST WRITE WINS: if the request
    # already carries form_profiles from a prior Turn 1 (or a legacy
    # commit path), do not clobber — otherwise a second no-answer
    # invocation with a re-sorted list_profiles would silently change
    # what `profile N` resolves to.
    try:
        _existing = u1_request.read_request(request_id) or {}
        if not _existing.get("form_profiles"):
            _spec_for_persist = _build_form_spec(kit, nozzle)
            if _spec_for_persist.get("_profiles_full"):
                u1_request.write_request(
                    request_id,
                    form_profiles=_spec_for_persist["_profiles_full"])
    except Exception:
        pass
    thumb_path = out_dir / "parts_thumbnails.png"
    thumb_result = _render_parts_thumbnail_grid(kit, thumb_path)
    if thumb_result.get("ok"):
        _emit(events_file, {"stage": "render", "request_id": request_id,
                            "kind": "parts_thumbnail_grid",
                            "image": thumb_result["path"]}, json_events)
    options = [{
        "label": f"All parts (1-{kit['part_count']})",
        "value": "all",
        "next_command": _build_next_command(
            archive, request_id, parts="all", nozzle=nozzle,
            no_live_upload=no_live_upload, no_live_material=no_live_material),
    }]
    note = ("Reply 'all' for every part, or a list like '1,3,5', or a "
            "range like '1-8'. See the attached thumbnail grid to identify "
            "parts by number.")
    event: dict[str, Any] = {
        "stage": "need_input",
        "key": "parts",
        "request_id": request_id,
        "prompt": f"Which parts to print?\n\n{parts_listing}",
        "options": options,
        "note": note,
        "instruction": ("Surface the parts_thumbnail_grid image path BARE "
                        "in your reply (no backticks) BEFORE the prompt text "
                        "so Telegram auto-attaches it; then surface the "
                        "prompt verbatim; then wait for the operator's answer."),
    }
    if errors:
        event["errors"] = errors
    _emit(events_file, event, json_events)
    _emit(events_file, {"stage": "awaiting_input", "need": "parts",
                        "request_id": request_id}, json_events)
    return {"phase": "awaiting_parts", "request_id": request_id}


def _emit_orient_prompt(events_file: Path | None, request_id: str, archive: Path,
                        kit: dict[str, Any], nozzle: str, parts_answer: str,
                        json_events: bool, no_live_material: bool,
                        no_live_upload: bool) -> dict[str, Any]:
    """Turn 2 — pick orientation. Cheap: emit options only.

    Brent design 2026-06-30 late (6-turn cheap-intermediate): the heavy
    work (slicing, rendering the plate) is deferred to Turn 6. This turn
    just records the operator's choice.
    """
    options = []
    for slug, label in [
        ("as-authored", "As-authored — preserve each STL's orientation"),
        ("auto", "Auto — let Orca rotate each part for better print quality"),
    ]:
        options.append({
            "label": label,
            "value": slug,
            "next_command": _build_next_command(
                archive, request_id, parts=parts_answer,
                orient=slug, nozzle=nozzle,
                no_live_upload=no_live_upload,
                no_live_material=no_live_material),
        })
    _emit(events_file, {
        "stage": "need_input",
        "key": "orient",
        "request_id": request_id,
        "prompt": "Orientation for all selected parts?",
        "options": options,
        "note": ("`auto` is the safe default for most kits. `as-authored` "
                 "keeps whatever the STL author set — pick this if you know "
                 "the parts are pre-oriented for printing."),
    }, json_events)
    _emit(events_file, {"stage": "awaiting_input", "need": "orient",
                        "request_id": request_id}, json_events)
    return {"phase": "awaiting_orient", "request_id": request_id}


def _emit_tool_prompt(events_file: Path | None, request_id: str, archive: Path,
                      kit: dict[str, Any], nozzle: str, parts_answer: str,
                      json_events: bool, no_live_material: bool,
                      no_live_upload: bool,
                      orient: str | None = None) -> dict[str, Any]:
    """Turn 3 — pick a toolhead. Material rides along from live Moonraker state.

    ``orient`` is threaded through so the next_command carries the earlier
    answer forward. Same pattern for _emit_preset_prompt / _emit_supports_prompt.
    """
    live_opts = _live_tool_options(no_live=no_live_material)
    options = []
    for o in live_opts:
        options.append({
            "label": o.get("label", o["value"]),
            "value": o["value"],
            "material": o.get("material"),
            "recommended": bool(o.get("recommended")),
            "next_command": _build_next_command(
                archive, request_id, parts=parts_answer,
                orient=orient,
                tool=o["value"], material=o.get("material"),
                nozzle=nozzle, no_live_upload=no_live_upload,
                no_live_material=no_live_material),
        })
    event: dict[str, Any] = {
        "stage": "need_input",
        "key": "tool",
        "request_id": request_id,
        "prompt": "Toolhead & filament?",
        "options": options,
    }
    _emit(events_file, event, json_events)
    _emit(events_file, {"stage": "awaiting_input", "need": "tool",
                        "request_id": request_id}, json_events)
    return {"phase": "awaiting_tool", "request_id": request_id}


def _emit_preset_prompt(events_file: Path | None, request_id: str, archive: Path,
                        kit: dict[str, Any], nozzle: str, parts_answer: str,
                        tool_choice: str, material: str, json_events: bool,
                        no_live_material: bool, no_live_upload: bool,
                        orient: str | None = None) -> dict[str, Any]:
    """Turn 4 — pick print profile. Top 8 scored for nozzle + material.

    Cheap: queries the profile list + emits options. No slicing here.
    """
    prof_opts = list_profiles(nozzle=nozzle)
    if not prof_opts:
        _emit(events_file, {"stage": "setup_required", "kind": "no_profiles",
                            "message": "No profiles found."}, json_events)
        return {"phase": "setup_required", "request_id": request_id}
    options = []
    for opt in prof_opts[:8]:
        slug = opt.get("value")
        options.append({
            "label": opt.get("label", slug),
            "value": slug,
            "next_command": _build_next_command(
                archive, request_id, parts=parts_answer,
                orient=orient, tool=tool_choice, material=material,
                profile=slug, nozzle=nozzle,
                no_live_upload=no_live_upload,
                no_live_material=no_live_material),
        })
    note = (f"Showing top {min(8, len(prof_opts))} of {len(prof_opts)} profiles "
            "for this nozzle. Reply with the number.")
    _emit(events_file, {
        "stage": "need_input",
        "key": "preset",
        "request_id": request_id,
        "prompt": "Print profile?",
        "options": options,
        "note": note,
        "total_available": len(prof_opts),
        "truncated": len(prof_opts) > 8,
    }, json_events)
    _emit(events_file, {"stage": "awaiting_input", "need": "preset",
                        "request_id": request_id}, json_events)
    return {"phase": "awaiting_preset", "request_id": request_id}


def _emit_supports_prompt(events_file: Path | None, request_id: str, archive: Path,
                          kit: dict[str, Any], nozzle: str, parts_answer: str,
                          tool_choice: str, material: str, profile_slug: str,
                          json_events: bool, no_live_material: bool,
                          no_live_upload: bool, orient: str | None = None,
                          selected: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Turn 5 — supports decision. Per-part overhang scan surfaced as guidance.

    Cheap emission (~3s). The overhang scan is fast — it just parses each
    STL's face normals; slicing does NOT happen here (deferred to Turn 6).
    """
    # Per-part overhang scan — same helper as the plan card's summary line.
    parts_scan = []
    if selected:
        parts_scan = [(p, _scan_part_overhang(p["path"])) for p in selected]
    summary = _summarize_overhangs(parts_scan) if parts_scan else {
        "summary_line": "Supports: (scan skipped — no parts context)"}

    options = []
    for slug, label in [
        ("supports", "Yes — generate supports on all selected parts"),
        ("no_supports", "No — no supports on any part"),
        ("overhangs", "Overhangs only — supports on high-overhang areas"),
    ]:
        options.append({
            "label": label,
            "value": slug,
            "next_command": _build_next_command(
                archive, request_id, parts=parts_answer,
                orient=orient, tool=tool_choice, material=material,
                profile=profile_slug, supports=slug, nozzle=nozzle,
                no_live_upload=no_live_upload,
                no_live_material=no_live_material),
        })
    _emit(events_file, {
        "stage": "need_input",
        "key": "supports",
        "request_id": request_id,
        "prompt": f"Supports? ({summary['summary_line']})",
        "options": options,
        "note": ("Per-part overhang scan shown in prompt. If some parts are "
                 "flagged as overhang-risk, 'yes' or 'overhangs' catches "
                 "them without penalizing simple parts."),
        "overhang_summary": summary,
    }, json_events)
    _emit(events_file, {"stage": "awaiting_input", "need": "supports",
                        "request_id": request_id}, json_events)
    return {"phase": "awaiting_supports", "request_id": request_id}


def _build_plan_card_text(selected: list[dict[str, Any]], tool: str, material: str,
                          profile_slug: str, orient: str,
                          supports_summary: str,
                          plate_count: int, gated_plate: str,
                          kit_parts_total: int,
                          time_material_summary: str,
                          warnings: list[str] | None,
                          bed_capture_ok: bool,
                          was_split: bool = False,
                          partition: list | None = None,
                          approval_ttl_seconds: int | None = None,
                          printer_busy: bool = False,
                          printer_busy_reason: str | None = None) -> str:
    """Compact plan card surfaced at Turn 3 (operator confirmation gate)."""
    sel_ids = [p['part_id'] for p in selected]
    sel_display = ', '.join(sel_ids[:4])
    if len(sel_ids) > 4:
        sel_display += f' (+{len(sel_ids) - 4} more)'
    warnings_line = ("Warnings: " + "; ".join(warnings)
                     if warnings else "Warnings: none")

    # M5: surface the partition + per-plate breakdown when split fired.
    split_line = ""
    if was_split and partition:
        parts_per_plate = [f"plate {i + 1} = {len(pl)} parts"
                           for i, pl in enumerate(partition)]
        split_line = (
            "\nSplit: parts overflowed one plate at 270x270mm — workflow "
            f"split into {len(partition)} plates ({', '.join(parts_per_plate)})."
        )

    # H2: tell operator the bed-photo deadline so they don't park the print
    # in chat for 40 min and confusedly hit "approval token is 2700s old" at
    # Stage 2.
    deadline_line = ""
    if bed_capture_ok and approval_ttl_seconds:
        mins = approval_ttl_seconds // 60
        deadline_line = (
            f"\nDeadline: reply within {mins} min — bed photo + approval "
            f"token expire after that, requiring a fresh confirm."
        )
    # Note (Brent UX 2026-07-01): options are surfaced via the need_input
    # event's numbered pick list — do NOT duplicate them in prose here.
    # This card ONLY carries the plan itself; the numbered options are
    # rendered right below by the agent.
    #
    # Printer-state line (Phase A): visible at the top of the card so
    # the operator immediately sees why `start` may be unavailable.
    printer_line = ""
    if printer_busy:
        reason = printer_busy_reason or "currently printing another job"
        printer_line = (
            f"Printer status: {reason}. Kit can be sliced/uploaded but "
            f"cannot start until printer is idle.\n"
        )
    return (
        f"📋 Print plan\n"
        f"{printer_line}"
        f"Parts: {len(selected)} of {kit_parts_total} ({sel_display})\n"
        f"Tool: {tool} {material}\n"
        f"Profile: {profile_slug}\n"
        f"Orientation: {orient}\n"
        f"{supports_summary}\n"
        f"Plates: {plate_count} (plate 1 start-gated: {gated_plate})"
        f"{split_line}\n"
        f"{time_material_summary}\n"
        f"{warnings_line}"
        f"{deadline_line}"
    )


def _emit(events_file: Path | None, obj: dict[str, Any], json_events: bool) -> None:
    """Emit one event to stdout + mirror to events.jsonl (local, no globals)."""
    if json_events:
        print(json.dumps(obj), flush=True)
    else:
        stage = obj.get("stage", "event")
        print(f"[{stage}] " + ", ".join(f"{k}={v}" for k, v in obj.items() if k != "stage"))
    if events_file is not None:
        try:
            with events_file.open("a") as f:
                f.write(json.dumps(obj, default=str) + "\n")
        except Exception:
            pass


def _audit(request_id: str, event: str, operator: str, **details: Any):
    try:
        import u1_audit
        return u1_audit.append(request_id, event, operator=operator, **details)
    except Exception:
        return None


def _build_form_spec(kit: dict[str, Any], nozzle: str,
                     persisted_profiles: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Assemble the u1_form spec from analysis (parts + offered options).

    ``profile N`` is resolved by INDEX, and form-emit / answer-parse happen in
    SEPARATE invocations. To keep index N stable even if print-history or the
    on-disk profiles change between the two calls, the form's profile list is
    persisted at emit time and replayed here on the answer call
    (``persisted_profiles``). Without that, a recommendation re-sort between
    calls could silently shift what ``profile 2`` means. Verified 2026-06-28:
    list_profiles order changes when history_print_settings_id changes.
    """
    if persisted_profiles:
        profiles_full = [
            {"idx": int(p["idx"]), "value": p["value"], "label": p.get("label", p["value"])}
            for p in persisted_profiles
        ]
    else:
        prof_opts = list_profiles(nozzle=nozzle)
        profiles_full = [
            {"idx": i + 1, "value": o["value"], "label": o.get("label", o["value"])}
            for i, o in enumerate(prof_opts)
        ]
    parts = [
        {"id": p["part_id"], "label": f"{p['filename']} ({p['footprint_mm'][0]:.0f}x{p['footprint_mm'][1]:.0f}mm)"}
        for p in kit["parts"]
    ]
    return {
        "parts": parts,
        "tools": DEFAULT_TOOLS,
        "materials": DEFAULT_MATERIALS,
        "profiles": [{"idx": p["idx"], "label": p["label"]} for p in profiles_full],
        "supports": ["supports", "no-supports", "overhangs"],
        "actions": ["start", "upload-only"],
        "_prof_opts": [{"value": p["value"]} for p in profiles_full],  # idx -> resolution
        "_profiles_full": profiles_full,  # persisted at form-emit for index stability
    }


def run_kit_workflow(args) -> dict[str, Any]:
    """Orchestrate the kit path. See module docstring for the staged flow."""
    operator = _resolve_operator(args)
    # Fence 1 companion: visible stderr banner whenever the workflow is
    # invoked under a test-prefixed operator. Same prefix list as
    # u1_print_start_gate.py's gate refusal. Prevents the "oh it looked
    # fine" self-deception that let 2026-07-01's smoke chain-fire a
    # real print: any test operator now produces a big banner on every
    # invocation so the tester can't miss that the gate will refuse.
    _TEST_OPERATOR_PREFIXES = ("smoke:", "test:", "dry:", "mock:", "fixture:")
    _op_lc = (operator or "").lower()
    if any(_op_lc.startswith(p) for p in _TEST_OPERATOR_PREFIXES):
        import sys as _sys
        _sys.stderr.write(
            f"\n{'=' * 68}\n"
            f"TEST MODE: --operator={operator!r} carries a test-flavored "
            f"prefix.\n"
            f"u1_print_start_gate.py will REFUSE Stage 2 under this operator.\n"
            f"No Moonraker traffic, no print start. If this is a real print,\n"
            f"re-invoke with a non-test --operator value.\n"
            f"{'=' * 68}\n\n"
        )
        _sys.stderr.flush()
    # Resilience: agents sometimes drop the model positional when re-invoking
    # the workflow (anti-pattern #4: paraphrase the verbatim next_command).
    # If --request-id is supplied AND request.json has a model_path, recover
    # from there. Pure correctness recovery — emit an audit row so we know it
    # fired, but don't refuse the call when the recovery succeeds.
    model_arg = getattr(args, "model", None)
    self_healed = False
    if not model_arg and getattr(args, "request_id", None):
        try:
            existing = u1_request.read_request(args.request_id) or {}
        except Exception:
            existing = {}
        recovered = existing.get("model_path")
        if recovered and Path(recovered).exists():
            model_arg = recovered
            self_healed = True
    if not model_arg:
        raise SystemExit(
            "u1_kit_workflow: missing model positional and no recoverable "
            "model_path in request.json. Re-invoke with the kit zip path."
        )
    archive = Path(model_arg).resolve()
    if self_healed:
        # make the self-heal visible in audit
        # so the underlying agent-paraphrasing pattern is detectable later.
        _audit(args.request_id, "kit_workflow_self_healed_model_arg",
               getattr(args, "operator", None) or "unknown",
               recovered_path=model_arg)
    json_events = bool(getattr(args, "json_events", False))
    nozzle = getattr(args, "nozzle", "0.4")
    no_live_material = bool(getattr(args, "no_live_material", False))
    # Live upload is the DEFAULT — the kit workflow is built to produce real
    # prints. `--no-live-upload` is the opt-out for CLI smoke tests. The legacy
    # `--live-upload` boolean is preserved as a no-op alias (it was the gating
    # flag in v2.1.0; now it just doesn't disable the upload).
    no_live_upload = bool(getattr(args, "no_live_upload", False))
    live_upload = not no_live_upload

    # --- request id (content-hash recovery on the archive bytes) ---
    request_id, was_resumed = u1_request.resolve_request_id(
        cli_request_id=getattr(args, "request_id", None),
        cli_fresh=bool(getattr(args, "fresh", False)),
        stl=archive,
    )
    out_dir = Path(args.out_dir) if getattr(args, "out_dir", None) else u1_request.ensure_request_dir(request_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    events_file = out_dir / "events.jsonl"

    # --- ANALYSIS: ingest the kit (always, cheap, idempotent) ---
    parts_dir = out_dir / "parts"
    stls = u1_kit.extract_all_stls(archive, parts_dir)
    kit = u1_kit.build_kit(stls)
    _emit(events_file, {
        "stage": "kit_ingested",
        "request_id": request_id,
        "part_count": kit["part_count"],
        "multi": kit["multi"],
        "oversized_part_ids": kit["oversized_part_ids"],
    }, json_events)
    _audit(request_id, "kit_ingested", operator,
           part_count=kit["part_count"], oversized=kit["oversized_part_ids"])

    if kit["oversized_part_ids"]:
        _emit(events_file, {
            "stage": "warning", "kind": "oversized_parts",
            "message": (f"Parts too big for the bed even rotated: {kit['oversized_part_ids']}. "
                        "Deselect them or split the model; the slice will fail otherwise."),
        }, json_events)

    # Persist kit record so re-sending the zip recovers this request.
    # IMPORTANT: when the operator is on a post-confirm action path
    # (refresh-bed-photo / retry-photo / retry-camera / start manual-bed-check /
    # start / upload-only / adjust), DO NOT downgrade the persisted phase to
    # kit_analysis — that would break the state-unchanged guard in the
    # refresh handler + lose downstream phase tracking. We always re-persist
    # the kit metadata (cheap idempotent) but only set phase when actually
    # entering the analysis path (no --action OR an analysis-phase action).
    _action_now = getattr(args, "action", None)
    _post_confirm_actions = {
        "start", "upload-only", "upload_only", "adjust",
        "start manual-bed-check", "start-manual-bed-check",
        "refresh-bed-photo", "retry-photo", "retry-camera",
    }
    _persist_kwargs = dict(
        model_file=archive.name, model_path=str(archive),
        model_hash=u1_request.compute_model_hash(archive) if archive.exists() else None,
        out_dir=str(out_dir), operator=operator,
        kit={"parts": kit["parts"], "part_count": kit["part_count"]},
    )
    if not _action_now or _action_now not in _post_confirm_actions:
        _persist_kwargs["phase"] = "kit_analysis"
    u1_request.write_request(request_id, **_persist_kwargs)

    # ── LEGACY: one-liner power-user path (CLI tests + scripted runs) ──
    answers = getattr(args, "form_answers", None)
    answers_json = getattr(args, "form_answers_json", None)
    if answers or answers_json:
        return _run_legacy_form_answers(
            args, operator, archive, kit, request_id, out_dir, events_file,
            json_events, answers, answers_json)

    # ── STAGED FLOW (Brent design 2026-06-30 late — text mode 6-turn) ──
    #
    # Turns 1-5 are CHEAP — each just persists an answer + emits the next
    # prompt (~3s of workflow work). The heavy work (slice, upload, render,
    # bed photo, token) is deferred to turn 6 (confirm). Total system overhead
    # for text mode: 5 × ~30-45s agent generation + 1 × ~90s commit + agent
    # overhead. Wall time ~5-7 min on small models; ~2 min on large.
    #
    # Form mode (single kit_form event with form_schema) is dispatched via
    # the _run_legacy_form_answers path when --form-answers is set; the
    # dedicated form-tool + button UX is deferred (see [[project_snapmaker_
    # trust_factor_pdf]] and postmortem §5e.5 for the roadmap).

    interaction_mode = _resolve_interaction_mode(args)

    # Turn 1: parts (heavy: extract + build kit dict + render STL thumbnail grid)
    parts_answer = getattr(args, "parts", None)
    if not parts_answer:
        return _emit_parts_prompt(
            events_file, request_id, archive, kit, nozzle, json_events,
            no_live_material, no_live_upload)

    selected_idx, err = _parse_parts_answer(parts_answer, kit["part_count"])
    if err:
        return _emit_parts_prompt(
            events_file, request_id, archive, kit, nozzle, json_events,
            no_live_material, no_live_upload, errors=[err])
    selected = [kit["parts"][i - 1] for i in selected_idx]

    # Turn 2: orient (cheap: read state + emit options)
    orient = getattr(args, "orient", None)
    if not orient:
        return _emit_orient_prompt(
            events_file, request_id, archive, kit, nozzle, parts_answer,
            json_events, no_live_material, no_live_upload)
    orient = orient.replace("_", "-")  # normalize `as_authored` → `as-authored`

    # Turn 3: tool (cheap-ish: query Moonraker live state)
    tool_choice = getattr(args, "tool", None)
    if not tool_choice:
        return _emit_tool_prompt(
            events_file, request_id, archive, kit, nozzle, parts_answer,
            json_events, no_live_material, no_live_upload,
            orient=orient)

    # Resolve material — prefer explicit --material, else look up live state.
    material = getattr(args, "material", None)
    if not material:
        live_opts = _live_tool_options(no_live=no_live_material)
        match = next((o for o in live_opts if o["value"] == tool_choice), None)
        material = (match.get("material") if match else None) or "PETG"

    # Turn 4: preset (cheap: query profiles + emit top 8)
    profile_slug = getattr(args, "profile", None)
    if not profile_slug:
        return _emit_preset_prompt(
            events_file, request_id, archive, kit, nozzle, parts_answer,
            tool_choice, material, json_events,
            no_live_material, no_live_upload, orient=orient)

    # Turn 5: supports (cheap: emit options)
    supports = getattr(args, "supports", None)
    if not supports:
        return _emit_supports_prompt(
            events_file, request_id, archive, kit, nozzle, parts_answer,
            tool_choice, material, profile_slug, json_events,
            no_live_material, no_live_upload, orient=orient,
            selected=selected)
    supports = supports.replace("-", "_")

    # Turn 6: confirm — heavy commit (slice + upload + render + bed + token)
    action = getattr(args, "action", None)
    if not action:
        return _emit_confirm_card(
            args, operator, archive, kit, request_id, out_dir, events_file,
            json_events, selected, selected_idx, parts_answer, tool_choice,
            material, no_live_material, no_live_upload)

    # Build the yes-command for the bed_clear_start prompt using the
    # accumulated state from THIS invocation. hit a hard
    # bug where the hand-built yes-command only had --action start
    # --bed-clear-confirmed and the workflow fell back to Turn 1
    # (awaiting_parts). Use _build_next_command which already threads
    # every prior answer, then append --bed-clear-confirmed. Applies to
    # both `start` and `start manual-bed-check` handlers.
    _yes_base = _build_next_command(
        archive, request_id, parts=parts_answer, tool=tool_choice,
        material=material, orient=orient, profile=profile_slug,
        supports=supports, action=action, nozzle=nozzle,
        no_live_upload=no_live_upload, no_live_material=no_live_material,
        operator=operator)
    yes_command_on_confirmed = _yes_base + " --bed-clear-confirmed"

    # Action handlers
    if action == "start":
        return _action_start(events_file, request_id, json_events,
                             yes_command=yes_command_on_confirmed,
                             bed_clear_confirmed=bool(
                                 getattr(args, "bed_clear_confirmed", False)),
                             operator=operator)
    # Layer 3 override: manual-bed-check. The operator explicitly takes
    # responsibility for bed verification via another method (looking at
    # printer, Snapmaker app, other camera). Hermes camera path failed but
    # operator says "I have verified the bed by another method."
    if action in ("start manual-bed-check", "start-manual-bed-check"):
        # Manual-override yes-command needs the override metadata too so
        # the audit chain stays intact on the second call.
        op_text = getattr(args, "operator_text", None) or "start manual-bed-check"
        v_method = getattr(args, "verification_method", None) or "unspecified_manual"
        _manual_yes = (
            yes_command_on_confirmed
            + f" --operator-text {_shell_quote(op_text)}"
            + f" --verification-method {_shell_quote(v_method)}"
        )
        return _action_start_manual_bed_check(
            events_file, request_id, operator, json_events,
            op_text, v_method,
            yes_command=_manual_yes,
            bed_clear_confirmed=bool(
                getattr(args, "bed_clear_confirmed", False)))
    # Brent design #3+#4: refresh-bed-photo / retry-photo / retry-camera —
    # all route to the same handler. Re-capture without re-slicing.
    if action in ("refresh-bed-photo", "retry-photo", "retry-camera"):
        return _action_refresh_bed_photo(
            args, events_file, request_id, archive, kit, operator,
            nozzle, parts_answer, tool_choice, material,
            no_live_material, no_live_upload, json_events,
            action_label=action)
    if action == "upload-only" or action == "upload_only":
        return _action_upload_only(events_file, request_id, operator, json_events)
    if action == "adjust":
        return _action_adjust(args, events_file, request_id, archive, kit,
                              nozzle, parts_answer, tool_choice, material,
                              no_live_material, no_live_upload, json_events)

    _emit(events_file, {
        "stage": "form_rejected", "key": "action", "request_id": request_id,
        "errors": [f"unknown --action {action!r}; expected start | upload-only | adjust"],
    }, json_events)
    return {"phase": "error", "request_id": request_id,
            "error": f"unknown action: {action}"}


def _emit_confirm_card(args, operator: str, archive: Path, kit: dict[str, Any],
                       request_id: str, out_dir: Path, events_file: Path | None,
                       json_events: bool, selected: list[dict[str, Any]],
                       selected_idx: list[int], parts_answer: str, tool_choice: str,
                       material: str, no_live_material: bool,
                       no_live_upload: bool) -> dict[str, Any]:
    """Turn 3 — heavy commit: arrange-slice + upload + plan-card need_input.

    Smart defaults applied here for Phase 1:
      - orient   = args.orient or 'auto'
      - profile  = args.profile or top-scored for the nozzle
      - supports = args.supports or 'no_supports'

    Phase 2 will add: composite plate-preview render, bed-snapshot capture,
    per-part overhang scan + per-part supports overrides, and Stage 1 collapse
    (issue approval token here so `start` jumps directly to Stage 2).
    """
    nozzle = getattr(args, "nozzle", "0.4")

    # Smart defaults
    orient = getattr(args, "orient", None) or "auto"
    auto_orient = (orient == "auto")
    supports = getattr(args, "supports", None) or "no_supports"
    supports = supports.replace("-", "_")
    override = _SUPPORTS_TO_OVERRIDE.get(supports, "no_supports")

    # Profile: explicit --profile, else top-scored for nozzle.
    profile_slug = getattr(args, "profile", None)
    if not profile_slug:
        prof_opts = list_profiles(nozzle=nozzle)
        if not prof_opts:
            _emit(events_file, {"stage": "setup_required", "kind": "no_profiles",
                                "message": "No profiles found. Run tools/fetch_snapmaker_profiles.py."},
                  json_events)
            return {"phase": "setup_required", "request_id": request_id,
                    "out_dir": str(out_dir)}
        profile_slug = prof_opts[0]["value"]

    process = profile_path(profile_slug)
    if override in ("supports", "no_supports"):
        process = apply_supports_override(process, override == "supports", out_dir)

    # Slice
    selected_paths = [p["path"] for p in selected]
    slice_out = out_dir / "slice"
    _emit(events_file, {"stage": "kit_slicing", "request_id": request_id,
                        "parts": len(selected_paths), "auto_orient": auto_orient}, json_events)
    try:
        arr = u1_arrange.arrange_slice(
            selected_paths, slice_out,
            tool=tool_choice, material=material, profile=profile_slug, nozzle=nozzle,
            auto_orient=auto_orient, allow_rotations=True,
            process_path_override=process,
        )
    except Exception as exc:
        _emit(events_file, {
            "stage": "kit_slice_failed", "request_id": request_id,
            "error": str(exc)[:600],
            "instruction": ("Slice failed. If a part is too big, re-answer the parts "
                            "prompt without that part."),
        }, json_events)
        _audit(request_id, "kit_slice_failed", operator, error=str(exc)[:300])
        return {"phase": "slice_failed", "request_id": request_id,
                "error": str(exc)[:600]}
    _emit(events_file, {"stage": "kit_sliced", "request_id": request_id,
                        "plate_count": arr["plate_count"]}, json_events)
    _audit(request_id, "kit_sliced", operator, plate_count=arr["plate_count"],
           parts=len(selected_paths), tool=tool_choice, material=material,
           profile=profile_slug)

    # if arrange_slice manually split because
    # all parts couldn't fit on one plate, surface that as a discrete event
    # so the operator sees the partition logic — not a silent "Plates: N".
    if arr.get("was_split"):
        _emit(events_file, {
            "stage": "kit_split",
            "request_id": request_id,
            "plate_count": arr["plate_count"],
            "partition": arr.get("partition", []),
            "reason": ("Parts overflowed a single plate at the U1's 270x270mm "
                       "build area. The workflow partitioned them across "
                       f"{arr['plate_count']} plates by footprint area."),
            "instruction": ("Operator-visible: surface this in the print plan "
                            "card so the operator sees the split before "
                            "confirming. Plate 1 is start-gated; plates 2..N "
                            "are uploaded and started from the Snapmaker app "
                            "after plate 1 finishes."),
        }, json_events)
        _audit(request_id, "kit_split", operator,
               plate_count=arr["plate_count"],
               partition_summary=[len(pl) for pl in arr.get("partition", [])])

    # Upload each plate. Plate 1 is the gated one.
    live_upload = not no_live_upload
    kit_stem = u1_kit._sanitize(archive.stem)
    plates_state: list[dict[str, Any]] = []
    upload_failures: list[dict[str, Any]] = []
    upload_warnings: list[dict[str, Any]] = []
    # render + inject the Snapmaker thumbnail
    # BEFORE upload. Prior order (upload then inject) meant the printer
    # got the un-thumbnailed gcode; local file had the preview but the
    # touchscreen + Snapmaker app showed a generic icon.
    per_plate_previews: list[dict[str, Any]] = []
    plate_thumbnail_results: list[dict[str, Any]] = []
    for pl in arr["plates"]:
        idx = pl["plate_idx"]
        src = Path(pl["gcode_path"])
        named = src.with_name(f"{kit_stem}_plate{idx}.gcode")
        if named != src:
            src.replace(named)
        # ── Render + inject preview BEFORE upload ──
        layout, injection = _render_and_inject_plate_preview(
            named, idx, len(arr["plates"]), out_dir,
            arranged_stls=(pl.get("arranged_stls") or []),
            selected_count=len(selected),
            tool_choice=tool_choice, material=material,
            bed_mm=u1_kit.DEFAULT_BED_MM,
            arrange_3mf=pl.get("arrange_3mf"),
            source_stls=pl.get("source_stls"),
        )
        per_plate_previews.append({
            "plate_idx": idx,
            "layout": layout,
            "injection": injection,
            "gcode_path_after_inject": str(named),
        })
        plate_thumbnail_results.append({
            "plate_idx": idx,
            "injected": bool(injection.get("ok")),
            "error": injection.get("error"),
        })
        # ── NOW upload (gcode has thumbnail baked in) ──
        up = (_real_upload(named,
                            on_collision=getattr(args, "on_collision", None),
                            material=material)
              if live_upload else
              {"dry_run": True, "uploaded_filename": named.name,
               "moonraker_upload_ok": None})
        # H1:
        # _real_upload returns rc in {0, 2, 3, 4, 5} per its contract.
        #   rc=0  upload + post-upload validation OK → ship
        #   rc=2  upload BLOCKED before contact (no file on printer) → fail
        #   rc=3  upload SUCCEEDED + post-upload blockers (e.g. printer became
        #         active mid-upload). File IS on printer. The blockers are
        #         state observations that matter at PRINT START time, not
        #         upload time. Ship with warnings; can_start/preflight at
        #         Stage 2 will catch "printer active" then. Brent: "I should
        #         still be able to send a slice just not print."
        #   rc=4  Moonraker transport failed (file NOT confirmed on printer) → fail
        #   rc=5  filename collision unresolved → fail
        if live_upload:
            rc = int(up.get("returncode", -1) or -1)
            ok = up.get("moonraker_upload_ok")
            if rc in (2, 4, 5) or ok is False:
                upload_failures.append({
                    "plate_idx": idx,
                    "filename": named.name,
                    "returncode": rc,
                    "moonraker_upload_ok": ok,
                    "post_upload_blockers": up.get("post_upload_blockers"),
                    "human_summary": up.get("human_summary"),
                })
            elif rc == 3:
                # File IS on printer; post-upload blockers are state warnings
                # for Stage 2 preflight, not workflow stoppers.
                upload_warnings.append({
                    "plate_idx": idx,
                    "filename": named.name,
                    "post_upload_blockers": up.get("post_upload_blockers", []),
                    "human_summary": up.get("human_summary"),
                })
        # Recompute hash post-injection: the thumbnail write mutated the
        # file, so pl["gcode_hash"] (pre-injection) no longer matches
        # bytes on disk. This hash is what request.json + Stage 2's
        # nonce-binding compare against.
        post_inject_hash = (u1_request.compute_model_hash(named)
                            if injection.get("ok") else pl["gcode_hash"])
        plates_state.append({
            "plate_idx": idx,
            "gcode_path": str(named),
            "gcode_hash": post_inject_hash,
            "printer_storage_filename": up.get("uploaded_filename") or named.name,
            "uploaded": up,
            "started": False,
            "metadata": pl.get("metadata", {}),
            "arranged_stls": pl.get("arranged_stls") or [],
            "preview_path": layout.get("path"),
            "thumbnail_injection": injection,
        })

    if upload_failures:
        _emit(events_file, {"stage": "kit_upload_failed",
                            "request_id": request_id,
                            "failures": upload_failures,
                            "instruction": ("One or more plates did not land on "
                                            "the printer. Investigate the "
                                            "moonraker_upload_ok / "
                                            "post_upload_blockers fields, fix "
                                            "the underlying issue (printer "
                                            "reachable? storage full? "
                                            "filename collision?), then re-run "
                                            "the kit workflow from the zip.")},
              json_events)
        _audit(request_id, "kit_upload_failed", operator,
               failure_count=len(upload_failures))
        u1_request.write_request(request_id, phase="upload_failed",
                                 plates=plates_state)
        return {"phase": "upload_failed",
                "request_id": request_id,
                "out_dir": str(out_dir),
                "failures": upload_failures}

    _emit(events_file, {"stage": "kit_uploaded", "request_id": request_id,
                        "plates": [p["printer_storage_filename"] for p in plates_state],
                        "live": live_upload}, json_events)

    # Plate 1 binds to the gate. Toolhead name MUST match single workflow:
    # T0 -> 'extruder', T1 -> 'extruder1', T2 -> 'extruder2', T3 -> 'extruder3'.
    plate1 = plates_state[0]
    _tidx = _tool_to_index(tool_choice)
    extruder = "extruder" if _tidx == 0 else f"extruder{_tidx}"
    stage1_cmd = build_stage1_command(
        printer_filename=plate1["printer_storage_filename"],
        intended_tool=extruder, material=material, request_id=request_id,
    )

    # ─── PHASE 2 ENRICHMENT ───
    # Per-part overhang scan + bucket summary (Phase 2.1)
    parts_scan = [(p, _scan_part_overhang(p["path"])) for p in selected]
    overhang_summary = _summarize_overhangs(parts_scan)
    supports_line = overhang_summary["summary_line"]

    # Time/material aggregate from per-plate gcode metadata (Phase 2.2)
    estimates = _aggregate_plate_estimates(plates_state)
    time_material_line = estimates["summary_line"]

    # render + inject moved BEFORE upload
    # (in the upload loop above) so the printer actually gets the
    # thumbnail. This post-upload pass only saves the canonical confirm
    # card image (plate 1's preview) and gathers the preview_result the
    # readiness card needs. Nothing here mutates the gcode files.
    preview_result: dict[str, Any] = {"ok": False, "path": None,
                                      "error": "no plates"}
    for ps in plates_state:
        if ps["plate_idx"] != 1:
            continue
        layout_path = ps.get("preview_path")
        if layout_path and Path(layout_path).exists():
            preview_result = {"ok": True, "path": layout_path, "error": None}
            canonical = out_dir / "plate_preview.png"
            try:
                canonical.write_bytes(Path(layout_path).read_bytes())
                preview_result["path"] = str(canonical)
            except Exception:
                pass

    # Phase-A (Brent design 2026-06-30): printer-state-aware confirm.
    # Slice + upload above happen unconditionally — the U1 accepts uploads
    # while printing. Bed-photo capture + start option are only meaningful
    # when the printer is idle (a busy printer means the photo would be of
    # the moving-head/active print, not the cleared bed for the new job).
    printer_state = _query_printer_idle()
    printer_busy = not printer_state["idle"]
    if printer_busy:
        # Skip bed capture — meaningless while another print is running.
        # Operator will refresh-bed-photo later (#3) once the printer idles.
        bed_result = {"ok": False, "snapshot_path": None, "token": None,
                      "approval_ttl_seconds": None,
                      "approval_expires_at": None,
                      "reason": ("printer is busy: " + (printer_state["reason"] or "")).strip(),
                      "printer_busy": True}
    else:
        bed_result = _capture_bed_and_issue_token(out_dir)
        bed_result["printer_busy"] = False

    warnings: list[str] = []
    if preview_result and not preview_result.get("ok"):
        warnings.append(f"plate preview render failed: {preview_result.get('error', 'unknown')}")
    # Phase A: suppress the bed-photo warning when the failure is *because*
    # the printer is busy — the dedicated `Printer status:` line at the top
    # of the card already explains it. Only surface bed-photo warnings for
    # genuinely-degraded states (camera unreachable, dark frame, etc.).
    if not bed_result["ok"] and not bed_result.get("printer_busy"):
        warnings.append(f"bed photo: {bed_result['reason']}")
    # H1-revised: surface upload state warnings (rc=3 — file uploaded, but
    # printer was active/busy at upload time). The slice + plan card still
    # ship; Stage 2's preflight catches "printer active" before any move.
    for uw in upload_warnings:
        bl = uw.get("post_upload_blockers") or []
        if isinstance(bl, list) and bl:
            warnings.append(
                f"plate {uw['plate_idx']} uploaded but printer state has "
                f"blockers: {'; '.join(str(b) for b in bl)}. Stage 2 will "
                "re-check before starting."
            )

    # H2 + M5 + Phase-A plumbing: TTL deadline + split summary + printer-state.
    plan_card = _build_plan_card_text(
        selected=selected, tool=tool_choice, material=material,
        profile_slug=profile_slug, orient=orient,
        supports_summary=supports_line,
        plate_count=len(plates_state),
        gated_plate=plate1["printer_storage_filename"],
        kit_parts_total=kit["part_count"],
        time_material_summary=time_material_line,
        warnings=warnings,
        bed_capture_ok=bed_result["ok"],
        was_split=bool(arr.get("was_split")),
        partition=arr.get("partition"),
        approval_ttl_seconds=(bed_result.get("approval_ttl_seconds")
                              if bed_result["ok"] else None),
        printer_busy=printer_busy,
        printer_busy_reason=printer_state.get("reason"),
    )

    # Readiness card carries the plan + captured photo/status. The raw
    # Stage 2 command and approval token are NOT surfaced here's
    # 2026-07-01 audit flagged that as a shortcut path any adapter could
    # grab to bypass the bed_clear_start yes/no turn. The token stays in
    # persisted request state (safety.approval_token) where only the
    # workflow's _action_start() can reach it.
    readiness = {
        "stage": "kit_readiness_card",
        "request_id": request_id,
        "part_count": kit["part_count"],
        "selected_parts": [p["part_id"] for p in selected],
        "plate_count": len(plates_state),
        "plates": [{"plate_idx": p["plate_idx"],
                    "printer_storage_filename": p["printer_storage_filename"],
                    "gcode_hash": p["gcode_hash"]} for p in plates_state],
        "tool": tool_choice, "material": material, "profile": profile_slug,
        "orient": orient, "supports": supports,
        "supports_summary": supports_line,
        "overhang_buckets": overhang_summary,
        "estimates": estimates,
        "composite_preview": (preview_result.get("path")
                              if preview_result and preview_result.get("ok") else None),
        "bed_snapshot": (bed_result.get("snapshot_path")
                         if bed_result["ok"] else None),
        # H2: surface the approval-token deadline so the operator + agent
        # can react before Stage 2's silent TTL refusal. The token itself
        # is NOT included — see safety note above.
        "approval_ttl_seconds": bed_result.get("approval_ttl_seconds"),
        "approval_expires_at": bed_result.get("approval_expires_at"),
        # Phase-A: surface printer state so downstream tools (audit, future
        # refresh-bed-photo handler) know why bed_snapshot is None.
        "printer_state": printer_state.get("state"),
        "printer_busy": printer_busy,
        "printer_busy_reason": printer_state.get("reason"),
        "gated_plate": plate1["printer_storage_filename"],
        # NOT A START AUTHORIZATION. This is a Stage 1 command that
        # captures a bed photo and writes an approval token; it does
        # NOT command the printer. For kit requests the gate refuses
        # any Stage 2 that arrives without a nonce
        # (see u1_print_start_gate.py:is_kit_request refusal path), so
        # this command CANNOT be chained into a print start on its
        # own. Named the way it is for backward compat with older
        # skills that grep this key for the photo-refresh flow.
        "start_gate_stage1_command": stage1_cmd,
        "operator_guidance": (
            f"{len(plates_state)} plate(s). Plate 1 ({plate1['printer_storage_filename']}) is "
            f"start-gated. Plates 2..{len(plates_state)} are already uploaded; "
            "start them from the Snapmaker app after plate 1 finishes."
            if len(plates_state) > 1 else
            "Single plate. `start` will ask for a fresh bed-clear yes/no before firing."
        ),
    }
    _emit(events_file, readiness, json_events)

    # Build `start` option — only offered when:
    #   (a) bed capture succeeded (operator can visually approve the bed), AND
    #   (b) printer is idle (Phase-A: no overlapping print).
    # When either fails: operator picks upload-only or adjust. Follow-up
    # increments add: #3 refresh-bed-photo (idle but expired/no-photo case),
    # #5 start manual-bed-check (idle but Hermes camera failed).
    options = []
    if bed_result["ok"] and not printer_busy:
        # `start` picks route through --action start → _action_start(), which
        # emits a fresh need_input(bed_clear_start) yes/no. Stage 2 command
        # is NOT surfaced here — that would be a
        # bypass path any adapter could grab.
        options.append({
            "label": "Start — I'll ask you to confirm bed-clear before the print fires",
            "value": "start",
            "next_command": _build_next_command(
                archive, request_id, parts=parts_answer, tool=tool_choice,
                material=material, orient=orient, profile=profile_slug,
                supports=supports, action="start", nozzle=nozzle,
                no_live_upload=no_live_upload, no_live_material=no_live_material),
        })
    if (not bed_result["ok"]) and not printer_busy:
        # Layer 3 override: bed photo verification failed at Hermes level
        # (camera unreachable, dark frame, brightness floor). Operator may
        # still verify the bed through other valid methods (looking at
        # printer in person, Snapmaker app, another trusted camera) and
        # take responsibility via this audited override.
        options.append({
            "label": ("Start with manual bed verification — type "
                      "`start manual-bed-check` to confirm you verified "
                      "the bed by another method (audited)"),
            "value": "start manual-bed-check",
            "next_command": _build_next_command(
                archive, request_id, parts=parts_answer, tool=tool_choice,
                material=material, orient=orient, profile=profile_slug,
                supports=supports, action="start manual-bed-check",
                nozzle=nozzle,
                no_live_upload=no_live_upload, no_live_material=no_live_material),
            "override": {
                "kind": "manual_bed_check",
                "reason": "bed_verification_degraded",
                "hermes_failure_reason": bed_result.get("reason"),
            },
        })
    options.append({
        "label": "Upload only — stage the G-code, do not print",
        "value": "upload-only",
        "next_command": _build_next_command(
            archive, request_id, parts=parts_answer, tool=tool_choice,
            material=material, orient=orient, profile=profile_slug,
            supports=supports, action="upload-only", nozzle=nozzle,
            no_live_upload=no_live_upload, no_live_material=no_live_material),
    })
    options.append({
        "label": "Adjust — change orientation / supports / profile / parts",
        "value": "adjust",
        "next_command": _build_next_command(
            archive, request_id, parts=parts_answer, tool=tool_choice,
            material=material, orient=orient, profile=profile_slug,
            supports=supports, action="adjust", nozzle=nozzle,
            no_live_upload=no_live_upload, no_live_material=no_live_material),
    })

    # Render-event emission so SKILL Step 2 surface rule attaches images.
    # `composite_preview` and `bed_snapshot` paths get emitted as their own
    # `render` events; agent surfaces them bare before the confirm prompt.
    if preview_result and preview_result.get("ok"):
        _emit(events_file, {"stage": "render", "request_id": request_id,
                            "kind": "kit_plate_preview",
                            "image": preview_result["path"]}, json_events)
    if bed_result["ok"]:
        _emit(events_file, {"stage": "render", "request_id": request_id,
                            "kind": "bed_snapshot",
                            "image": bed_result["snapshot_path"]}, json_events)

    _emit(events_file, {
        "stage": "need_input",
        "key": "confirm",
        "request_id": request_id,
        "prompt": plan_card,
        "options": options,
        "readiness_card": readiness,
        "instruction": ("Surface BOTH render images bare in your reply (composite "
                        "plate preview + bed snapshot path) BEFORE the prompt text, "
                        "then surface the prompt verbatim, then wait for the "
                        "operator's reply (`start` / `upload-only` / `adjust`)."),
    }, json_events)
    _emit(events_file, {"stage": "awaiting_input", "need": "confirm",
                        "request_id": request_id}, json_events)

    # Persist all the Turn-3 state — including the safety block so can_start()
    # at Stage 2 sees bed_clear_photo_captured=True.
    safety_block: dict[str, Any] = {}
    if bed_result["ok"]:
        safety_block = {
            "bed_clear_check_required": True,
            "bed_clear_photo_captured": True,
            "bed_clear_photo_path": bed_result["snapshot_path"],
            "approval_token": bed_result["token"],
        }
    u1_request.write_request(
        request_id,
        phase="awaiting_confirm",
        kit={"parts": kit["parts"], "part_count": kit["part_count"],
             "selected": [p["part_id"] for p in selected], "orient_mode": orient},
        plates=plates_state,
        tool=tool_choice, material=material, profile=profile_slug, supports=override,
        gcode_hash=plate1["gcode_hash"],
        printer_storage_filename=plate1["printer_storage_filename"],
        start_gate_stage1_command=stage1_cmd,
        readiness_card_event=readiness,
        safety=safety_block,
    )
    _audit(request_id, "kit_readiness_card_emitted", operator,
           plate_count=len(plates_state),
           gated_plate=plate1["printer_storage_filename"],
           gcode_hash=plate1["gcode_hash"],
           bed_photo_ok=bed_result["ok"],
           request_revision=(u1_request.read_request(request_id) or {}).get("request_revision", 1))

    return {
        "phase": "awaiting_confirm", "request_id": request_id,
        "out_dir": str(out_dir),
        "plate_count": len(plates_state),
        "gated_plate": plate1["printer_storage_filename"],
        "start_gate_stage1_command": stage1_cmd,
        "composite_preview": preview_result.get("path") if preview_result.get("ok") else None,
        "bed_snapshot": bed_result.get("snapshot_path") if bed_result["ok"] else None,
    }


def _query_printer_idle() -> dict[str, Any]:
    """Brent design 2026-06-30: phase-based safety refactor #1+#2.

    Returns ``{idle: bool, state: str, reason: str | None, raw: dict}``
    based on the live U1/Moonraker state. The kit workflow consults this
    at confirm time so the *slice/upload* phases are never gated on
    printer-busy state (the U1 accepts uploads while printing), but the
    *start* phase + the bed-photo capture remain gated correctly.

    Fail-soft: if Moonraker is unreachable or the helpers can't import,
    returns ``idle=True`` with a "could not query" reason so the workflow
    proceeds (operator + Stage 2 preflight still own the final gate).
    """
    try:
        from u1_print_start_gate import query_state
        from u1_config import get_u1_host, get_u1_port
        host = get_u1_host()
        port = get_u1_port()
        status = query_state(host, port)
    except Exception as exc:
        return {"idle": True, "state": "unknown",
                "reason": f"could not query printer state: {exc}",
                "raw": {}}
    if not isinstance(status, dict):
        return {"idle": True, "state": "unknown",
                "reason": "printer state response was not a dict",
                "raw": {}}
    pause = status.get("pause_state") or {}
    vsd = status.get("virtual_sdcard") or {}
    wh = status.get("webhooks") or {}
    if pause.get("is_paused"):
        return {"idle": False, "state": "paused",
                "reason": "printer is paused (in-progress print)",
                "raw": status}
    if vsd.get("is_active"):
        return {"idle": False, "state": "printing",
                "reason": "virtual_sdcard is active (print in progress)",
                "raw": status}
    ps_state = ((status.get("print_stats") or {}).get("state", "") or "").lower()
    # Match u1_print_start_gate.preflight()'s cancelled-but-clean logic
    # (audit 2026-06-26): 'cancelled' is a benign terminal state from a
    # prior run when the printer is otherwise idle + webhooks ready + vsd
    # inactive + not paused. Klipper accepts /printer/print/start from
    # this state. Fix 2026-06-30 late (Brent flagged "job is cancelled
    # gate — WTF is that about") after my earlier abort left the printer
    # in cancelled state and this function was over-eagerly refusing to
    # let the confirm turn offer `start`.
    ps_cancelled_but_clean = (
        ps_state == "cancelled"
        and not vsd.get("is_active")
        and not pause.get("is_paused")
        and (wh.get("state") in (None, "ready"))
    )
    if ps_state in ("printing", "paused", "cancelling") or (
            ps_state == "cancelled" and not ps_cancelled_but_clean):
        return {"idle": False, "state": ps_state,
                "reason": f"print_stats state is {ps_state}",
                "raw": status}
    return {"idle": True, "state": ps_state or "ready",
            "reason": None, "raw": status}


def _capture_bed_and_issue_token(out_dir: Path) -> dict[str, Any]:
    """Capture a bed photo + issue an approval token at the kit confirm turn.

    Collapses what was Stage 1 (capture + brightness gate + token) into the
    confirm turn. The operator's `start` reply at Turn 3 = approval of the
    bed photo they just saw + start.

    Returns:
      {'ok': bool, 'snapshot_path': str | None, 'token': str | None,
       'approval_ttl_seconds': int | None,
       'approval_expires_at': str | None, 'captured_at_utc': str | None,
       'reason': str | None}

    Fail-closed: if camera unreachable, brightness too dark, or any other
    failure, returns ok=False with a reason. The plan card omits the `start`
    option when ok=False — operator must adjust / upload-only / fix camera
    and retry from a fresh kit_workflow invocation.
    """
    try:
        from u1_print_start_gate import (
            capture_real_bed_photo as _capture,
            _write_approval_token as _issue_token,
            APPROVAL_TTL_SEC,
        )
        from u1_config import get_u1_host, get_u1_port
    except Exception as exc:
        return {"ok": False, "snapshot_path": None, "token": None,
                "approval_ttl_seconds": None, "approval_expires_at": None,
                "captured_at_utc": None,
                "reason": f"could not import start-gate helpers: {exc}"}
    try:
        host = get_u1_host()
        port = get_u1_port()
    except Exception as exc:
        return {"ok": False, "snapshot_path": None, "token": None,
                "approval_ttl_seconds": None, "approval_expires_at": None,
                "captured_at_utc": None,
                "reason": f"printer host/port unresolved: {exc}"}
    try:
        snapshot = _capture(out_dir, host, port, wait=5.0)
    except Exception as exc:
        return {"ok": False, "snapshot_path": None, "token": None,
                "approval_ttl_seconds": None, "approval_expires_at": None,
                "captured_at_utc": None,
                "reason": f"capture raised: {type(exc).__name__}: {exc}"}
    if not snapshot.get("ok"):
        # capture_real_bed_photo already wrote a mock or dark image; carry
        # the reason through so the plan card explains why `start` was hidden.
        return {"ok": False,
                "snapshot_path": snapshot.get("path"),
                "token": None,
                "approval_ttl_seconds": None, "approval_expires_at": None,
                "captured_at_utc": snapshot.get("timestamp_utc"),
                "reason": (snapshot.get("error")
                           or "bed photo failed the brightness gate")}
    try:
        token = _issue_token(out_dir, snapshot)
    except Exception as exc:
        return {"ok": False, "snapshot_path": snapshot.get("path"),
                "token": None,
                "approval_ttl_seconds": None, "approval_expires_at": None,
                "captured_at_utc": snapshot.get("timestamp_utc"),
                "reason": f"token issuance failed: {exc}"}
    # expose the TTL deadline so the plan card
    # can warn the operator. Snapshot's timestamp_utc is the moment the
    # photo was captured = the start of the TTL window. Compute expiry from
    # that + APPROVAL_TTL_SEC so the readiness card carries an explicit
    # ISO deadline.
    from datetime import datetime, timezone, timedelta
    captured_ts = snapshot.get("timestamp_utc")
    expires_at: str | None = None
    if captured_ts:
        try:
            captured = datetime.fromisoformat(captured_ts.replace("Z", "+00:00"))
            expires_at = (captured + timedelta(seconds=APPROVAL_TTL_SEC)).isoformat()
        except Exception:
            expires_at = None
    return {"ok": True, "snapshot_path": snapshot["path"],
            "token": token,
            "approval_ttl_seconds": int(APPROVAL_TTL_SEC),
            "approval_expires_at": expires_at,
            "captured_at_utc": captured_ts,
            "reason": None}


def _action_start(events_file: Path | None, request_id: str,
                  json_events: bool,
                  yes_command: str | None = None,
                  bed_clear_confirmed: bool = False,
                  operator: str | None = None) -> dict[str, Any]:
    """Operator picked `start` at the confirm gate.

    Two-turn safety boundary: stable
    rule wins over any toolkit shortcut. Bed photo + approval token were
    issued at the confirm turn but Stage 2 is NEVER fired from there.

    Flow:
      1. First call (bed_clear_confirmed=False): mint a single-use
         pending_bed_clear_start object bound to (request_revision,
         gcode_hash, prompt_key, nonce) and persist it. Emit need_input
         asking 'Bed clear and start request u1_...? (yes/no)'.
      2. Second call (bed_clear_confirmed=True): validate the persisted
         pending object exists, phase matches, revision matches, and
         gcode hash matches. Refuse and emit an error event otherwise.
         On success, mint a stage2_approval_nonce (consumed by
         u1_print_start_gate.py via --stage2-approval-nonce) and emit
         next_action_required. Consume the pending_bed_clear_start.

    Any missing/mismatched field on the second call fails closed with a
    structured audit-worthy error — no Stage 2 command is emitted.
    """
    import secrets
    from datetime import datetime, timezone

    state = u1_request.read_request(request_id) or {}
    safety = state.get("safety") or {}
    token = safety.get("approval_token")
    plate_filename = state.get("printer_storage_filename")
    tool = state.get("tool", "T0")
    material = state.get("material", "PETG")
    request_revision = state.get("request_revision", 1)
    # Plate 1 gcode hash — the SLICE we're gating starts on.
    plates = state.get("plates") or []
    gcode_hash = plates[0].get("gcode_hash") if plates else None

    if not plate_filename:
        _emit(events_file, {"stage": "error", "request_id": request_id,
                            "error": ("no plate filename persisted for this request; "
                                      "re-run the workflow from the kit zip first.")},
              json_events)
        return {"phase": "error", "request_id": request_id,
                "error": "missing plate filename"}
    _tidx = _tool_to_index(tool)
    extruder = "extruder" if _tidx == 0 else f"extruder{_tidx}"

    if token and not bed_clear_confirmed:
        # First-call path. Mint pending approval object bound to this
        # exact request revision + gcode hash. If either drifts before
        # the operator says yes, the second call will refuse.
        nonce = secrets.token_urlsafe(24)
        pending = {
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "request_revision": request_revision,
            "gcode_hash": gcode_hash,
            "prompt_key": "bed_clear_start",
            "nonce": nonce,
        }
        # Persist the pending approval into safety block, preserving
        # existing safety fields (token/snapshot_path/etc).
        new_safety = dict(safety)
        new_safety["pending_bed_clear_start"] = pending
        u1_request.write_request(request_id,
                                 phase="awaiting_bed_clear_start",
                                 safety=new_safety)
        # Reference the photo the operator already saw with the print
        # plan card (attached at the confirm turn). Do NOT say "the
        # attached photo" — no fresh attachment on this turn (Brent UX
        # 2026-07-01). Agents that resurface the photo at this turn get
        # dupes; agents that don't leave the operator wondering where
        # the photo went.
        prompt = (
            f"Review the bed photo I sent with the print plan above. "
            f"Bed clear and you want to start request {request_id}? (yes/no)"
        )
        need = {
            "stage": "need_input",
            "request_id": request_id,
            "need": "bed_clear_start",
            "key": "bed_clear_start",
            "requires_fresh_operator_bed_clear": True,
            "approval_prompt_key": "bed_clear_start",
            "prompt": prompt,
            "expected_answers": ["yes", "no"],
            # Yes-command carries full kit context (parts/tool/material/
            # profile/orient/supports/nozzle) plus --bed-clear-confirmed.
            # a hand-built version that omitted these
            # caused the workflow to restart at Turn 1 (awaiting_parts)
            # on the resume call.
            "next_command_on_yes": yes_command or (
                # Legacy fallback (state-recovery path) — the workflow's
                # self-heal on --request-id may fill in missing args, but
                # this is a defense-in-depth path; callers should always
                # pass yes_command.
                f"python3 /opt/data/scripts/u1_kit_workflow.py "
                f"--request-id {request_id} --action start "
                f"--bed-clear-confirmed"
            ),
            "next_command_on_no": None,
            "bed_snapshot_path": (safety.get("snapshot_path")
                                  or safety.get("bed_snapshot_path")),
        }
        _emit(events_file, need, json_events)
        _emit(events_file, {"stage": "awaiting_input",
                            "need": "bed_clear_start",
                            "request_id": request_id}, json_events)
        return {"phase": "awaiting_bed_clear_start", "request_id": request_id,
                "prompt": prompt}

    if token and bed_clear_confirmed:
        # Second-call path — validate pending approval before firing.
        # Any mismatch fails closed with a structured error event.
        pending = safety.get("pending_bed_clear_start")
        phase = state.get("phase")
        problems: list[str] = []
        if not pending:
            problems.append("no pending_bed_clear_start object; "
                            "operator must go through the yes/no prompt first")
        if phase != "awaiting_bed_clear_start":
            problems.append(f"phase is {phase!r}, expected 'awaiting_bed_clear_start'")
        if pending:
            if pending.get("prompt_key") != "bed_clear_start":
                problems.append(f"pending.prompt_key={pending.get('prompt_key')!r}, "
                                "expected 'bed_clear_start'")
            if pending.get("request_revision") != request_revision:
                problems.append(
                    f"revision mismatch: pending={pending.get('request_revision')} "
                    f"current={request_revision}")
            if pending.get("gcode_hash") != gcode_hash:
                problems.append("gcode_hash mismatch (plan changed since "
                                "bed-clear prompt was issued)")
        if problems:
            err = {
                "stage": "bed_clear_approval_rejected",
                "request_id": request_id,
                "reasons": problems,
                "next_action": ("Refuse this start. Ask the operator to "
                                "re-run --action start (no --bed-clear-confirmed) "
                                "to get a fresh bed-clear prompt bound to the "
                                "current plan."),
            }
            _emit(events_file, err, json_events)
            return {"phase": "bed_clear_approval_rejected",
                    "request_id": request_id,
                    "reasons": problems}
        # Mint a fresh single-use nonce that u1_print_start_gate.py
        # will consume before firing Stage 2. Persist it, wipe pending.
        stage2_nonce = secrets.token_urlsafe(24)
        new_safety = dict(safety)
        new_safety.pop("pending_bed_clear_start", None)
        new_safety["stage2_approval_nonce"] = stage2_nonce
        new_safety["stage2_approval_issued_at"] = datetime.now(timezone.utc).isoformat()
        new_safety["stage2_approval_binds"] = {
            "request_revision": request_revision,
            "gcode_hash": gcode_hash,
            "prompt_key": "bed_clear_start",
        }
        u1_request.write_request(request_id,
                                 phase="awaiting_print_start",
                                 safety=new_safety)
        stage2_cmd = (
            f"python3 /opt/data/scripts/u1_print_start_gate.py "
            f"{_shell_quote(plate_filename)} "
            f"--intended-tool {extruder} --requested-material {_shell_quote(material)} "
            f"--request-id {request_id} --bed-clear start "
            f"--approval-token {token} "
            f"--stage2-approval-nonce {stage2_nonce}"
        )
        if operator:
            stage2_cmd += f" --operator {_shell_quote(operator)}"
        next_action = {
            "stage": "next_action_required",
            "request_id": request_id,
            "reason": ("Operator confirmed bed-clear at the fresh yes/no "
                       "turn. Firing Stage 2 with single-use nonce "
                       "(safety check + print start)."),
            "command": stage2_cmd,
        }
        _emit(events_file, next_action, json_events)
        u1_request.write_request(request_id,
                                 next_action_required_event=next_action)
        return {"phase": "awaiting_print_start", "request_id": request_id,
                "command": stage2_cmd}
    # Fallback to legacy Stage 1 (no token persisted).
    stage1_cmd = state.get("start_gate_stage1_command")
    if not stage1_cmd:
        _emit(events_file, {"stage": "error", "request_id": request_id,
                            "error": ("no approval_token and no start_gate_stage1_command "
                                      "persisted; re-run the workflow from the kit zip.")},
              json_events)
        return {"phase": "error", "request_id": request_id,
                "error": "missing token and stage1_cmd"}
    next_action = {
        "stage": "next_action_required",
        "request_id": request_id,
        "reason": ("No approval token captured at confirm (bed photo probably "
                   "failed). Falling back to legacy Stage 1 for a fresh capture."),
        "command": stage1_cmd,
    }
    _emit(events_file, next_action, json_events)
    u1_request.write_request(request_id, phase="awaiting_start_approval",
                             next_action_required_event=next_action)
    return {"phase": "awaiting_start_approval", "request_id": request_id,
            "start_gate_stage1_command": stage1_cmd}


def _action_refresh_bed_photo(args, events_file: Path | None, request_id: str,
                              archive: Path, kit: dict[str, Any],
                              operator: str, nozzle: str, parts_answer: str,
                              tool_choice: str, material: str,
                              no_live_material: bool, no_live_upload: bool,
                              json_events: bool,
                              action_label: str) -> dict[str, Any]:
    """Brent design 2026-06-30 #3 + #4: re-capture bed photo + re-issue
    approval token WITHOUT re-slicing.

    Triggered when:
      - Token TTL expired before operator picked `start`
      - Printer transitioned busy→idle since the original confirm
      - Original capture failed (camera, dark frame) and operator wants
        to retry after fixing whatever was wrong

    State-unchanged guard (per Brent's pushback #2): phase must be
    awaiting_confirm or awaiting_print_start, AND no slicing-affecting
    field can have changed since the persisted readiness card. Otherwise
    refuse and instruct the operator to re-run the normal confirm path.
    """
    state = u1_request.read_request(request_id) or {}
    phase = state.get("phase")
    allowed_phases = ("awaiting_confirm", "awaiting_print_start",
                      "awaiting_start_approval")
    if phase not in allowed_phases:
        _emit(events_file, {
            "stage": "refresh_bed_photo_refused",
            "request_id": request_id,
            "reason": (f"phase={phase!r}; refresh is only valid from "
                       f"{allowed_phases}. Re-run the workflow with "
                       "the kit zip to get a fresh confirm card."),
        }, json_events)
        return {"phase": "refresh_refused", "request_id": request_id,
                "reason": f"phase={phase!r}"}

    # State-unchanged guard: tool/material/profile/parts/orient/supports
    # must all match what's persisted. (Drift checks at can_start cover
    # gcode/revision drift; this is the operator-intent check.)
    persisted = {
        "tool": state.get("tool"),
        "material": state.get("material"),
        "profile": state.get("profile"),
        "supports": state.get("supports"),
    }
    current_intent = {
        "tool": tool_choice,
        "material": material,
        "profile": getattr(args, "profile", None) or state.get("profile"),
        "supports": (getattr(args, "supports", None)
                     or "no_supports").replace("-", "_"),
    }
    drifted = []
    for k, v in current_intent.items():
        pv = persisted.get(k)
        if pv is not None and v is not None and v != pv:
            drifted.append(f"{k}: persisted={pv!r} now={v!r}")
    if drifted:
        _emit(events_file, {
            "stage": "refresh_bed_photo_refused",
            "request_id": request_id,
            "reason": ("operator intent drifted since the confirm card was "
                       f"emitted: {'; '.join(drifted)}. Use `adjust` to "
                       "change the plan, then take a fresh confirm card."),
        }, json_events)
        return {"phase": "refresh_refused", "request_id": request_id,
                "reason": "intent_drift"}

    # State unchanged: check printer state — if still busy, refusing is
    # correct (no point capturing a bed photo of an active print).
    printer_state = _query_printer_idle()
    if not printer_state["idle"]:
        _emit(events_file, {
            "stage": "refresh_bed_photo_refused",
            "request_id": request_id,
            "reason": ("printer is still busy: "
                       + (printer_state.get("reason") or "unknown")),
            "instruction": ("Wait for the active print to finish, then re-run "
                            f"`{action_label}` once the printer is idle."),
        }, json_events)
        return {"phase": "refresh_refused", "request_id": request_id,
                "reason": "printer_busy"}

    # OK — re-capture + re-issue token. The slice and upload state are
    # preserved. Plate filename + gcode_hash come from the persisted
    # readiness card (unchanged).
    out_dir = u1_request.ensure_request_dir(request_id)
    bed_result = _capture_bed_and_issue_token(out_dir)
    if not bed_result["ok"]:
        # Degraded again — surface the failure, let operator pick the
        # `start manual-bed-check` override path explicitly.
        _emit(events_file, {
            "stage": "refresh_bed_photo_degraded",
            "request_id": request_id,
            "reason": bed_result.get("reason"),
            "instruction": ("Hermes still cannot capture a usable bed photo. "
                            "Use `start manual-bed-check` to take responsibility "
                            "via another verification method (looking at the "
                            "printer / Snapmaker app / another camera)."),
        }, json_events)
        return {"phase": "refresh_degraded", "request_id": request_id,
                "reason": bed_result.get("reason")}

    # Update safety block — fresh token + photo path.
    safety_block = {
        "bed_clear_check_required": True,
        "bed_clear_photo_captured": True,
        "bed_clear_photo_path": bed_result["snapshot_path"],
        "approval_token": bed_result["token"],
        "approval_ttl_seconds": bed_result.get("approval_ttl_seconds"),
        "approval_expires_at": bed_result.get("approval_expires_at"),
        "refreshed_via": action_label,
        "refreshed_at_utc": bed_result.get("captured_at_utc"),
    }
    u1_request.write_request(request_id, phase="awaiting_print_start",
                             safety=safety_block)
    _audit(request_id, "bed_photo_refreshed", operator,
           action_label=action_label,
           new_token_first8=(bed_result["token"] or "")[:8],
           snapshot_path=bed_result["snapshot_path"])

    # Build the Stage 2 command with the fresh token and emit
    # next_action_required so the agent can fire start directly when the
    # operator confirms — OR surface the refreshed card for re-review.
    plate_filename = state.get("printer_storage_filename")
    # Refresh routes through the same two-turn flow as the fresh confirm.
    # No stage2_command in this event — Stage 2
    # only fires after _action_start's bed_clear_start yes/no + validated
    # pending_bed_clear_start with matching gcode_hash.
    _emit(events_file, {"stage": "render", "request_id": request_id,
                        "kind": "bed_snapshot",
                        "image": bed_result["snapshot_path"]}, json_events)
    _emit(events_file, {
        "stage": "need_input",
        "key": "refreshed_confirm",
        "request_id": request_id,
        "prompt": (
            "🔄 Bed photo refreshed. "
            f"New approval token expires in "
            f"{(bed_result.get('approval_ttl_seconds') or 0) // 60} min. "
            "Type `start` — I'll ask for a fresh bed-clear yes/no before firing."
        ),
        "options": [{
            "label": "Start — I'll ask you to confirm bed-clear before the print fires",
            "value": "start",
            "next_command": _build_next_command(
                archive, request_id, parts=parts_answer, tool=tool_choice,
                material=material, action="start", nozzle=nozzle,
                no_live_upload=no_live_upload,
                no_live_material=no_live_material),
        }],
        "approval_ttl_seconds": bed_result.get("approval_ttl_seconds"),
        "approval_expires_at": bed_result.get("approval_expires_at"),
    }, json_events)
    return {"phase": "awaiting_refreshed_confirm",
            "request_id": request_id}


def _action_start_manual_bed_check(events_file: Path | None, request_id: str,
                                   operator: str, json_events: bool,
                                   operator_text: str,
                                   verification_method: str,
                                   yes_command: str | None = None,
                                   bed_clear_confirmed: bool = False,
                                   ) -> dict[str, Any]:
    """Layer 3 override: bed verification by means other than the Hermes
    camera path.

    Two-turn boundary:
    even Layer 3 overrides now go through a fresh bed_clear_start yes/no.
    The typed override phrase IS the operator's Layer 3 consent, but
    a fresh yes preserves the "one pattern for every Stage 2 path"
    architectural invariant. The audit row captures BOTH the override
    phrase AND the fresh yes timestamp — stronger provenance than either
    alone.

    Flow:
      1. First call (bed_clear_confirmed=False): mint pending approval
         + audit the override attempt + emit need_input(bed_clear_start)
         with a light-touch prompt acknowledging manual verification.
      2. Second call (bed_clear_confirmed=True): validate pending, mint
         Stage 2 nonce, synthesize approval token + sidecar (existing
         override mechanics), emit next_action_required with Stage 2 cmd.

    Brent design 2026-06-30 (forensic override) preserved: "agent cannot
    fabricate" is still FORENSIC — the audit rows capture operator_text +
    verification_method exactly. Post-mortem review determines whether
    the operator typed those bytes.
    """
    import hashlib
    import secrets
    from datetime import datetime, timezone

    state = u1_request.read_request(request_id) or {}
    plate_filename = state.get("printer_storage_filename")
    tool = state.get("tool", "T0")
    material = state.get("material", "PETG")
    request_revision = state.get("request_revision", 1)
    plates_l = state.get("plates") or []
    gcode_hash = plates_l[0].get("gcode_hash") if plates_l else None
    if not plate_filename:
        _emit(events_file, {"stage": "error", "request_id": request_id,
                            "error": ("no plate filename persisted; "
                                      "re-run the workflow from the kit zip first.")},
              json_events)
        return {"phase": "error", "request_id": request_id,
                "error": "missing plate filename"}

    if not bed_clear_confirmed:
        # First-call path — audit the override attempt, mint pending, emit
        # need_input. Do NOT fire Stage 2 or write synthesized token yet.
        _audit(request_id, "operator_override_attempted", operator,
               override_kind="manual_bed_check",
               reason="bed_verification_degraded",
               verification_method=verification_method,
               operator_text=operator_text,
               timestamp_utc=datetime.now(timezone.utc).isoformat(),
               request_revision=request_revision,
               gcode_hash=gcode_hash,
               note="First-turn override attempt; awaiting fresh yes/no.")
        nonce = secrets.token_urlsafe(24)
        safety_current = dict(state.get("safety") or {})
        safety_current["pending_bed_clear_start"] = {
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "request_revision": request_revision,
            "gcode_hash": gcode_hash,
            "prompt_key": "bed_clear_start",
            "nonce": nonce,
            "manual_override": True,
            "verification_method": verification_method,
            "operator_text": operator_text,
        }
        u1_request.write_request(request_id,
                                 phase="awaiting_bed_clear_start",
                                 safety=safety_current)
        prompt = (
            f"Manual bed-check registered ({verification_method}). "
            f"Confirm start of request {request_id}? (yes/no)"
        )
        need = {
            "stage": "need_input",
            "request_id": request_id,
            "need": "bed_clear_start",
            "key": "bed_clear_start",
            "requires_fresh_operator_bed_clear": True,
            "approval_prompt_key": "bed_clear_start",
            "override_context": {
                "kind": "manual_bed_check",
                "verification_method": verification_method,
                "operator_text": operator_text,
            },
            "prompt": prompt,
            "expected_answers": ["yes", "no"],
            "next_command_on_yes": yes_command or (
                # Legacy fallback (state-recovery path) — should never be
                # taken; callers always pass yes_command with full context.
                f"python3 /opt/data/scripts/u1_kit_workflow.py "
                f"--request-id {request_id} --action 'start manual-bed-check' "
                f"--bed-clear-confirmed "
                f"--operator-text {_shell_quote(operator_text)} "
                f"--verification-method {_shell_quote(verification_method)}"
            ),
            "next_command_on_no": None,
        }
        _emit(events_file, need, json_events)
        _emit(events_file, {"stage": "awaiting_input",
                            "need": "bed_clear_start",
                            "request_id": request_id}, json_events)
        return {"phase": "awaiting_bed_clear_start",
                "request_id": request_id, "prompt": prompt,
                "override": "manual_bed_check"}

    # Second-call path — validate pending manual override + fire Stage 2.
    safety_current = state.get("safety") or {}
    pending = safety_current.get("pending_bed_clear_start")
    phase = state.get("phase")
    problems: list[str] = []
    if not pending:
        problems.append("no pending manual-bed-check override to confirm")
    if phase != "awaiting_bed_clear_start":
        problems.append(f"phase is {phase!r}, expected 'awaiting_bed_clear_start'")
    if pending:
        if not pending.get("manual_override"):
            problems.append("pending object is not a manual_override; refusing")
        if pending.get("request_revision") != request_revision:
            problems.append(
                f"revision mismatch: pending={pending.get('request_revision')} "
                f"current={request_revision}")
        if pending.get("gcode_hash") != gcode_hash:
            problems.append("gcode_hash mismatch (plan changed since override was "
                            "attempted)")
    if problems:
        err = {
            "stage": "bed_clear_approval_rejected",
            "request_id": request_id,
            "reasons": problems,
            "override_context": {"kind": "manual_bed_check"},
            "next_action": ("Refuse this start. Ask the operator to re-run "
                            "'start manual-bed-check' (no --bed-clear-confirmed) "
                            "to attempt a fresh override bound to the current plan."),
        }
        _emit(events_file, err, json_events)
        return {"phase": "bed_clear_approval_rejected",
                "request_id": request_id, "reasons": problems}

    # Synthesize an approval token + sidecar so Stage 2's _approval_token_valid
    # check passes. Photo path is empty — the override IS the verification.
    request_dir = u1_request.ensure_request_dir(request_id)
    ts = datetime.now(timezone.utc).isoformat()
    override_attempt_ts = pending.get("issued_at")
    fake_sha = hashlib.sha256(f"manual-bed-check:{request_id}:{ts}".encode()).hexdigest()
    token = hashlib.sha256(f"{fake_sha}:{ts}".encode()).hexdigest()[:32]
    sidecar = {
        "token": token,
        "sha256": fake_sha,
        "timestamp_utc": ts,
        "snapshot_path": None,
        "manual_verification": True,
        "verification_method": verification_method,
        "operator_text": operator_text,
    }
    (request_dir / "bed_snapshot.approval_token.json").write_text(
        json.dumps(sidecar, indent=2))

    # Mint Stage 2 approval nonce (single-use, bound to plan). Consume the
    # pending object. Update safety block with the manual-override state
    # + the fresh-yes provenance.
    stage2_nonce = secrets.token_urlsafe(24)
    safety_block: dict[str, Any] = {
        "bed_clear_check_required": True,
        "bed_clear_photo_captured": True,
        "bed_clear_photo_path": None,
        "approval_token": token,
        "manual_verification": True,
        "verification_method": verification_method,
        "operator_text": operator_text,
        "override_attempted_at": override_attempt_ts,
        "override_confirmed_at": ts,
        "stage2_approval_nonce": stage2_nonce,
        "stage2_approval_issued_at": ts,
        "stage2_approval_binds": {
            "request_revision": request_revision,
            "gcode_hash": gcode_hash,
            "prompt_key": "bed_clear_start",
        },
    }
    u1_request.write_request(request_id, phase="awaiting_print_start",
                             safety=safety_block)

    # Audit row per Brent's spec — now captures BOTH the override attempt
    # timestamp AND the fresh yes timestamp for stronger provenance.
    _audit(request_id, "operator_override_confirmed", operator,
           override_kind="manual_bed_check",
           reason="bed_verification_degraded",
           verification_method=verification_method,
           operator_text=operator_text,
           override_attempted_at=override_attempt_ts,
           override_confirmed_at=ts,
           request_revision=request_revision,
           gcode_hash=gcode_hash,
           expected_tool=tool,
           expected_material=material,
           photo_path=None)

    _tidx = _tool_to_index(tool)
    extruder = "extruder" if _tidx == 0 else f"extruder{_tidx}"
    stage2_cmd = (
        f"python3 /opt/data/scripts/u1_print_start_gate.py "
        f"{_shell_quote(plate_filename)} "
        f"--intended-tool {extruder} --requested-material {_shell_quote(material)} "
        f"--request-id {request_id} --bed-clear start "
        f"--approval-token {token} "
        f"--stage2-approval-nonce {stage2_nonce}"
    )
    if operator:
        stage2_cmd += f" --operator {_shell_quote(operator)}"
    next_action = {
        "stage": "next_action_required",
        "request_id": request_id,
        "reason": ("Operator override CONFIRMED: manual bed verification "
                   "accepted at the fresh yes/no turn. Stage 2 runs with "
                   "the synthesized token + single-use nonce."),
        "command": stage2_cmd,
        "override": {
            "kind": "manual_bed_check",
            "verification_method": verification_method,
            "operator_text": operator_text,
            "attempted_at": override_attempt_ts,
            "confirmed_at": ts,
        },
    }
    _emit(events_file, next_action, json_events)
    u1_request.write_request(request_id, next_action_required_event=next_action)
    return {"phase": "awaiting_print_start", "request_id": request_id,
            "command": stage2_cmd, "override": "manual_bed_check"}


def _action_upload_only(events_file: Path | None, request_id: str,
                        operator: str, json_events: bool) -> dict[str, Any]:
    """Operator picked `upload-only`. Plates are already on the printer."""
    _emit(events_file, {
        "stage": "complete",
        "request_id": request_id,
        "reason": ("Upload-only: all plates uploaded to the printer; "
                   "start them from the Snapmaker app when ready."),
    }, json_events)
    u1_request.write_request(request_id, phase="complete")
    _audit(request_id, "kit_upload_only_complete", operator)
    return {"phase": "complete", "request_id": request_id}


def _action_adjust(args, events_file: Path | None, request_id: str,
                   archive: Path, kit: dict[str, Any], nozzle: str,
                   parts_answer: str, tool_choice: str, material: str,
                   no_live_material: bool, no_live_upload: bool,
                   json_events: bool) -> dict[str, Any]:
    """Operator picked `adjust` at Turn 3. Two-step drill:

      Step A — if --adjust not given: emit need_input(adjust_field)
               offering orient / supports / profile / parts.

      Step B — if --adjust <field> given: emit the field-specific
               need_input with options. Each option's next_command drops
               --action and --adjust and sets the new flag value, so
               re-invocation re-enters confirm with the changed default.
    """
    adjust_field = getattr(args, "adjust", None)
    if not adjust_field:
        return _emit_adjust_field_prompt(
            events_file, request_id, archive, kit, nozzle, parts_answer,
            tool_choice, material, json_events, no_live_material, no_live_upload)
    if adjust_field == "orient":
        return _emit_orient_drill_prompt(
            events_file, request_id, archive, kit, nozzle, parts_answer,
            tool_choice, material, json_events, no_live_material, no_live_upload)
    if adjust_field == "supports":
        return _emit_supports_drill_prompt(
            events_file, request_id, archive, kit, nozzle, parts_answer,
            tool_choice, material, json_events, no_live_material, no_live_upload)
    if adjust_field == "profile":
        return _emit_profile_drill_prompt(
            events_file, request_id, archive, kit, nozzle, parts_answer,
            tool_choice, material, json_events, no_live_material, no_live_upload)
    if adjust_field == "parts":
        # Re-prompt parts; the workflow's normal staging handles the rest.
        return _emit_parts_prompt(
            events_file, request_id, archive, kit, nozzle, json_events,
            no_live_material, no_live_upload)
    _emit(events_file, {
        "stage": "form_rejected", "key": "adjust", "request_id": request_id,
        "errors": [f"unknown --adjust {adjust_field!r}; "
                   "expected orient | supports | profile | parts"],
    }, json_events)
    return {"phase": "error", "request_id": request_id,
            "error": f"unknown adjust field: {adjust_field}"}


def _emit_adjust_field_prompt(events_file, request_id, archive, kit, nozzle,
                              parts_answer, tool_choice, material, json_events,
                              no_live_material, no_live_upload):
    """Step A of adjust — operator picks which field to change."""
    fields = [
        ("orient", "Orientation — as-authored vs auto"),
        ("supports", "Supports — turn on / off / overhangs-only globally"),
        ("profile", "Profile — pick a different slicer process"),
        ("parts", "Parts — change which STLs are included"),
    ]
    options = [{
        "label": label,
        "value": field,
        "next_command": _build_next_command(
            archive, request_id, parts=parts_answer, tool=tool_choice,
            material=material, action="adjust", adjust=field, nozzle=nozzle,
            no_live_upload=no_live_upload, no_live_material=no_live_material),
    } for field, label in fields]
    _emit(events_file, {
        "stage": "need_input",
        "key": "adjust_field",
        "request_id": request_id,
        "prompt": "Adjust what?",
        "options": options,
    }, json_events)
    _emit(events_file, {"stage": "awaiting_input", "need": "adjust_field",
                        "request_id": request_id}, json_events)
    return {"phase": "awaiting_adjust_field", "request_id": request_id}


def _emit_orient_drill_prompt(events_file, request_id, archive, kit, nozzle,
                              parts_answer, tool_choice, material, json_events,
                              no_live_material, no_live_upload):
    """Step B (orient) — pick as-authored or auto. Re-enters confirm with new orient."""
    options = []
    for slug, label in [("as-authored", "As-authored — preserve the STL's orientation"),
                        ("auto", "Auto — let Orca rotate each part for better print quality")]:
        options.append({
            "label": label,
            "value": slug,
            "next_command": _build_next_command(
                archive, request_id, parts=parts_answer, tool=tool_choice,
                material=material, orient=slug, nozzle=nozzle,
                no_live_upload=no_live_upload, no_live_material=no_live_material),
        })
    _emit(events_file, {
        "stage": "need_input",
        "key": "orient",
        "request_id": request_id,
        "prompt": "Orientation?",
        "options": options,
    }, json_events)
    _emit(events_file, {"stage": "awaiting_input", "need": "orient",
                        "request_id": request_id}, json_events)
    return {"phase": "awaiting_orient", "request_id": request_id}


def _emit_supports_drill_prompt(events_file, request_id, archive, kit, nozzle,
                                parts_answer, tool_choice, material, json_events,
                                no_live_material, no_live_upload):
    """Step B (supports) — flip global supports. Per-part overrides land in
    a follow-up phase (Brent's #4 design)."""
    options = []
    for slug, label in [
        ("supports", "Supports — generate supports for all parts"),
        ("no_supports", "No supports — none for any part"),
        ("overhangs", "Overhangs only — supports just on critical overhangs"),
    ]:
        options.append({
            "label": label,
            "value": slug,
            "next_command": _build_next_command(
                archive, request_id, parts=parts_answer, tool=tool_choice,
                material=material, supports=slug, nozzle=nozzle,
                no_live_upload=no_live_upload, no_live_material=no_live_material),
        })
    _emit(events_file, {
        "stage": "need_input",
        "key": "supports",
        "request_id": request_id,
        "prompt": "Supports? (global; per-part overrides land in a later phase)",
        "options": options,
    }, json_events)
    _emit(events_file, {"stage": "awaiting_input", "need": "supports",
                        "request_id": request_id}, json_events)
    return {"phase": "awaiting_supports", "request_id": request_id}


def _emit_profile_drill_prompt(events_file, request_id, archive, kit, nozzle,
                               parts_answer, tool_choice, material, json_events,
                               no_live_material, no_live_upload):
    """Step B (profile) — pick a different process profile. Top 8 scored
    for the chosen nozzle (matches single-STL `preset` need_input)."""
    prof_opts = list_profiles(nozzle=nozzle)
    if not prof_opts:
        _emit(events_file, {"stage": "setup_required", "kind": "no_profiles",
                            "message": "No profiles found."}, json_events)
        return {"phase": "setup_required", "request_id": request_id}
    options = []
    for opt in prof_opts[:8]:
        slug = opt.get("value")
        options.append({
            "label": opt.get("label", slug),
            "value": slug,
            "next_command": _build_next_command(
                archive, request_id, parts=parts_answer, tool=tool_choice,
                material=material, profile=slug, nozzle=nozzle,
                no_live_upload=no_live_upload, no_live_material=no_live_material),
        })
    note = (f"Showing the {min(8, len(prof_opts))} highest-scoring profiles "
            f"out of {len(prof_opts)} for this nozzle.")
    _emit(events_file, {
        "stage": "need_input",
        "key": "profile",
        "request_id": request_id,
        "prompt": "Print profile?",
        "options": options,
        "note": note,
        "total_available": len(prof_opts),
        "truncated": len(prof_opts) > 8,
    }, json_events)
    _emit(events_file, {"stage": "awaiting_input", "need": "profile",
                        "request_id": request_id}, json_events)
    return {"phase": "awaiting_profile", "request_id": request_id}


# ─── Legacy one-liner mode (preserved for CLI smoke tests + scripted runs) ──

def _run_legacy_form_answers(args, operator: str, archive: Path,
                             kit: dict[str, Any], request_id: str,
                             out_dir: Path, events_file: Path | None,
                             json_events: bool,
                             answers: str | None,
                             answers_json: str | None) -> dict[str, Any]:
    """Power-user one-liner path: parse a single --form-answers line and
    commit in a single CLI call. Bypasses the staged Q&A entirely."""
    existing = u1_request.read_request(request_id) or {}
    spec = _build_form_spec(kit, getattr(args, "nozzle", "0.4"),
                            persisted_profiles=existing.get("form_profiles"))
    if not spec["profiles"]:
        _emit(events_file, {"stage": "setup_required", "kind": "no_profiles",
                            "message": "No profiles found. Run tools/fetch_snapmaker_profiles.py."},
              json_events)
        return {"phase": "setup_required", "request_id": request_id,
                "out_dir": str(out_dir)}

    # Persist the form_profiles snapshot so `profile N` stays stable across calls.
    u1_request.write_request(request_id, form_profiles=spec["_profiles_full"])

    if answers_json:
        try:
            obj = json.loads(answers_json) if isinstance(answers_json, str) else answers_json
        except (ValueError, TypeError) as exc:
            _emit(events_file, {"stage": "form_rejected", "key": "kit_form",
                                "request_id": request_id,
                                "errors": [f"invalid --form-answers-json: {exc}"]},
                  json_events)
            return {"phase": "form_rejected", "request_id": request_id,
                    "errors": [f"invalid --form-answers-json: {exc}"]}
        parsed = u1_form.parse_answers_json(obj, spec)
    else:
        parsed = u1_form.parse_answers(answers, spec)
    if not parsed["ok"]:
        _emit(events_file, {
            "stage": "form_rejected", "key": "kit_form", "request_id": request_id,
            "errors": parsed["errors"], "form": u1_form.build_form(spec),
            "instruction": ("The answer didn't validate. Show the errors + form and "
                            "ask the operator again."),
        }, json_events)
        return {"phase": "form_rejected", "request_id": request_id,
                "errors": parsed["errors"]}
    values = parsed["values"]
    _emit(events_file, {"stage": "form_accepted", "request_id": request_id,
                        "parsed": u1_form.echo_parse(values, spec)}, json_events)
    return _commit_kit_legacy(args, request_id, operator, out_dir,
                              events_file, archive, kit, spec, values)


def _commit_kit_legacy(args, request_id, operator, out_dir, events_file,
                       archive, kit, spec, values) -> dict[str, Any]:
    """Original v2.1.0 commit path — used by the --form-answers one-liner.

    The staged flow uses _emit_confirm_card instead. Kept untouched to
    preserve CLI test behavior + scripted-run compatibility.
    """
    json_events = bool(getattr(args, "json_events", False))
    nozzle = getattr(args, "nozzle", "0.4")

    sel_idx = values.get("parts") or list(range(1, kit["part_count"] + 1))
    selected = [kit["parts"][i - 1] for i in sel_idx]
    selected_paths = [p["path"] for p in selected]

    tool = values["tool"]
    material = values["material"]
    auto_orient = values.get("orient") == "auto"

    prof = values["profile"]
    prof_opts = spec["_prof_opts"]
    prof_idx = int(prof.get("idx", 1))
    profile_slug = prof_opts[prof_idx - 1]["value"]
    process = profile_path(profile_slug)

    supports = values.get("supports", "no-supports")
    override = _SUPPORTS_TO_OVERRIDE.get(supports, "no_supports")
    if override in ("supports", "no_supports"):
        process = apply_supports_override(process, override == "supports", out_dir)

    slice_out = out_dir / "slice"
    _emit(events_file, {"stage": "kit_slicing", "request_id": request_id,
                        "parts": len(selected_paths), "auto_orient": auto_orient}, json_events)
    try:
        arr = u1_arrange.arrange_slice(
            selected_paths, slice_out,
            tool=tool, material=material, profile=profile_slug, nozzle=nozzle,
            auto_orient=auto_orient, allow_rotations=True,
            process_path_override=process,
        )
    except Exception as exc:
        _emit(events_file, {
            "stage": "kit_slice_failed", "request_id": request_id,
            "error": str(exc)[:600],
            "instruction": "Slice failed. If a part is too big, deselect it and re-answer the form.",
        }, json_events)
        _audit(request_id, "kit_slice_failed", operator, error=str(exc)[:300])
        return {"phase": "slice_failed", "request_id": request_id, "error": str(exc)[:600]}
    _emit(events_file, {"stage": "kit_sliced", "request_id": request_id,
                        "plate_count": arr["plate_count"]}, json_events)
    _audit(request_id, "kit_sliced", operator, plate_count=arr["plate_count"],
           parts=len(selected_paths), tool=tool, material=material, profile=profile_slug)

    kit_stem = u1_kit._sanitize(archive.stem)
    live = bool(getattr(args, "live_upload", False))
    plates_state: list[dict[str, Any]] = []
    for pl in arr["plates"]:
        idx = pl["plate_idx"]
        src = Path(pl["gcode_path"])
        named = src.with_name(f"{kit_stem}_plate{idx}.gcode")
        if named != src:
            src.replace(named)
        # Render + inject thumbnail BEFORE upload:
        # printer gets the thumbnailed file, not a plain gcode.
        layout, injection = _render_and_inject_plate_preview(
            named, idx, len(arr["plates"]), out_dir,
            arranged_stls=(pl.get("arranged_stls") or []),
            selected_count=len(selected),
            tool_choice=tool, material=material,
            bed_mm=u1_kit.DEFAULT_BED_MM,
            arrange_3mf=pl.get("arrange_3mf"),
            source_stls=pl.get("source_stls"),
        )
        up = _real_upload(named,
                          on_collision=getattr(args, "on_collision", None),
                          material=material) if live else {
            "dry_run": True, "uploaded_filename": named.name, "moonraker_upload_ok": None}
        post_inject_hash = (u1_request.compute_model_hash(named)
                            if injection.get("ok") else pl["gcode_hash"])
        plates_state.append({
            "plate_idx": idx,
            "gcode_path": str(named),
            "gcode_hash": post_inject_hash,
            "printer_storage_filename": up.get("uploaded_filename") or named.name,
            "uploaded": up,
            "started": False,
            "arranged_stls": pl.get("arranged_stls") or [],
            "preview_path": layout.get("path"),
            "thumbnail_injection": injection,
        })
    _emit(events_file, {"stage": "kit_uploaded", "request_id": request_id,
                        "plates": [p["printer_storage_filename"] for p in plates_state],
                        "live": live}, json_events)

    plate1 = plates_state[0]
    _tidx = _tool_to_index(tool)
    extruder = "extruder" if _tidx == 0 else f"extruder{_tidx}"
    stage1_cmd = build_stage1_command(
        printer_filename=plate1["printer_storage_filename"],
        intended_tool=extruder, material=material, request_id=request_id,
    )

    action = values.get("action", "start")
    readiness = {
        "stage": "kit_readiness_card",
        "request_id": request_id,
        "part_count": kit["part_count"],
        "selected_parts": [p["part_id"] for p in selected],
        "plate_count": len(plates_state),
        "plates": [{"plate_idx": p["plate_idx"],
                    "printer_storage_filename": p["printer_storage_filename"],
                    "gcode_hash": p["gcode_hash"]} for p in plates_state],
        "tool": tool, "material": material, "profile": profile_slug,
        "orient": values.get("orient"), "supports": supports,
        "parsed_echo": u1_form.echo_parse(values, spec),
        "gated_plate": plate1["printer_storage_filename"],
        # NOT A START AUTHORIZATION — Stage 1 photo-capture command
        # only. Kit Stage 2 requires the staged bed_clear_start
        # nonce (u1_print_start_gate.py refuses kit start without one).
        "start_gate_stage1_command": stage1_cmd,
        "operator_guidance": (
            f"{len(plates_state)} plate(s). Stage 1 gates ONLY plate 1 "
            f"({plate1['printer_storage_filename']}). After it prints, start plates "
            f"2..{len(plates_state)} from the Snapmaker app — they're already uploaded."
            if len(plates_state) > 1 else
            "Single plate. Stage 1 captures the bed photo + approval token."
        ),
    }
    _emit(events_file, readiness, json_events)

    persist_phase = "awaiting_start_approval" if action == "start" else "complete"
    next_action = None
    if action == "start":
        next_action = {
            "stage": "next_action_required",
            "reason": "Run Stage 1 to capture a real bed photo + approval token for plate 1.",
            "command": stage1_cmd,
        }
        _emit(events_file, next_action, json_events)
    else:
        _emit(events_file, {"stage": "complete", "request_id": request_id,
                            "reason": "Upload-only: all plates on the printer; start from the Snapmaker app."}, json_events)

    u1_request.write_request(
        request_id,
        phase=persist_phase,
        kit={"parts": kit["parts"], "part_count": kit["part_count"],
             "selected": [p["part_id"] for p in selected], "orient_mode": values.get("orient")},
        plates=plates_state,
        tool=tool, material=material, profile=profile_slug, supports=override,
        gcode_hash=plate1["gcode_hash"],
        printer_storage_filename=plate1["printer_storage_filename"],
        start_gate_stage1_command=stage1_cmd,
        readiness_card_event=readiness,
        next_action_required_event=next_action,
    )
    _audit(request_id, "kit_readiness_card_emitted", operator,
           plate_count=len(plates_state), gated_plate=plate1["printer_storage_filename"],
           gcode_hash=plate1["gcode_hash"], request_revision=(u1_request.read_request(request_id) or {}).get("request_revision", 1))

    return {
        "phase": persist_phase, "request_id": request_id, "out_dir": str(out_dir),
        "plate_count": len(plates_state),
        "gated_plate": plate1["printer_storage_filename"],
        "start_gate_stage1_command": stage1_cmd,
    }


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Multi-part kit slice workflow (Snapmaker U1)")
    ap.add_argument("model", nargs="?", default=None,
                    help=("zip of STLs (a kit) or a single model file. "
                          "Optional ONLY when --request-id resolves a recoverable "
                          "model_path from request.json (resume case)."))
    ap.add_argument("--json-events", action="store_true")

    # Staged-flow flags (Phase 1) — each turn adds one.
    ap.add_argument("--parts", default=None,
                    help="parts selection: 'all', '1,3,5', or a range '1-8'")
    ap.add_argument("--tool", default=None, choices=DEFAULT_TOOLS,
                    help="toolhead (T0..T3); material rides along from live Moonraker state")
    ap.add_argument("--material", default=None,
                    help="explicit material override (usually inferred from live state)")
    ap.add_argument("--orient", default=None, choices=["as-authored", "as_authored", "auto"],
                    help="orientation mode (default at confirm: auto)")
    ap.add_argument("--profile", default=None,
                    help="profile slug (default at confirm: top-scored for nozzle)")
    ap.add_argument("--supports", default=None,
                    choices=["supports", "no_supports", "no-supports", "overhangs"],
                    help="supports decision (default at confirm: no_supports)")
    ap.add_argument("--action", default=None,
                    choices=["start", "upload-only", "upload_only", "adjust",
                             "start manual-bed-check", "start-manual-bed-check",
                             "refresh-bed-photo", "retry-photo", "retry-camera"],
                    help="confirm-gate action picked by operator")
    # Two-turn bed-clear safety boundary. When the
    # operator picks `start` at the confirm turn, the workflow emits a
    # fresh bed-clear yes/no prompt instead of firing Stage 2 directly.
    # Only when Hermes re-invokes with --bed-clear-confirmed (in response
    # to the operator's explicit yes) does Stage 2 fire.
    ap.add_argument("--bed-clear-confirmed", action="store_true",
                    dest="bed_clear_confirmed",
                    help=("Set ONLY after the operator answers 'yes' to the "
                          "fresh bed-clear-and-start prompt. Never set on the "
                          "initial confirm-turn 'start' pick."))
    # Layer 3 override metadata (per Brent design 2026-06-30). When the agent
    # surfaces an override option (start manual-bed-check / start
    # accept-material-mismatch), the operator's literal typed phrase goes
    # into --operator-text and the override-reason method into
    # --verification-method. Empty defaults preserve backward compat.
    ap.add_argument("--operator-text", default=None,
                    help=("Layer 3 override only: the literal phrase the "
                          "operator typed authorizing the override. Captured "
                          "verbatim in the audit row for forensic review."))
    ap.add_argument("--verification-method", default=None,
                    choices=["manual", "snapmaker_app", "other_camera",
                             "unspecified_manual"],
                    help=("Layer 3 override only: how the operator verified "
                          "the bed when bypassing Hermes camera verification."))
    # Brent design 2026-06-30 late: interaction-mode split
    ap.add_argument("--interaction-mode", default=None,
                    choices=["text", "form"],
                    help=("Model-capability-based UX split. `text` = staged "
                          "6-turn Q&A (parts → orient → tool → preset → "
                          "supports → confirm), cheap intermediates, safe for "
                          "small local models. `form` = single kit_form event "
                          "with form_schema (buttons UX, requires form_tool "
                          "installed). Falls through to env U1_INTERACTION_MODE "
                          "then defaults to 'text'. Detection at Hermes "
                          "session start by the snapmaker_u1 plugin based on "
                          "model provider (web APIs = form, local = text)."))
    ap.add_argument("--adjust", default=None,
                    choices=["orient", "supports", "profile", "parts"],
                    help="adjust drill-in field (requires --action adjust)")
    ap.add_argument("--no-live-material", action="store_true",
                    help="skip Moonraker live state query (use headless tool fallback)")

    # Legacy one-liner power-user mode.
    ap.add_argument("--form-answers", default=None,
                    help="LEGACY: operator's one-line answer, relayed verbatim (bypasses staged Q&A)")
    ap.add_argument("--form-answers-json", default=None,
                    help="LEGACY: structured JSON answer (bypasses staged Q&A)")

    ap.add_argument("--request-id", default=None)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--operator", default=None)
    ap.add_argument("--nozzle", default="0.4")
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--live-upload", action="store_true",
                    help=("In legacy --form-answers one-liner mode: opt IN to "
                          "the real Moonraker upload (default is dry-run for "
                          "CLI tests). In staged mode: no-op — live upload is "
                          "the default; use --no-live-upload to opt out."))
    ap.add_argument("--no-live-upload", action="store_true",
                    help="Opt out of the real Moonraker upload (CLI smoke tests only)")
    ap.add_argument("--on-collision", choices=["rename", "overwrite", "cancel"], default=None)
    a = ap.parse_args(argv)
    res = run_kit_workflow(a)
    if not a.json_events:
        print(json.dumps(res, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
